"""
renderer_video.py ── Wan 2.2 I2V-A14B Image-to-Video (ComfyUI)
=================================================================
14B 双专家方案（HighNoise + LowNoise）+ lightx2v 蒸馏 LoRA。
原生 16fps，低 CFG（蒸馏要求 cfg=1）。

可选增强（自动探测，缺则降级）：
  - RIFE 帧插值 16fps → 32fps（需 ComfyUI-Frame-Interpolation）
  - ESRGAN 2× 放大 832×480 → 1664×960（需 RealESRGAN 模型）

模型清单（必须）：
  diffusion_models/
    - Wan2.2-I2V-A14B-HighNoise-Q3_K_S.gguf
    - Wan2.2-I2V-A14B-LowNoise-Q3_K_S.gguf
  text_encoders/
    - umt5-xxl-encoder-Q5_K_M.gguf
  vae/
    - Wan2.1_VAE.safetensors          ← 14B 用 2.1 VAE，不是 2.2！
  clip_vision/
    - clip_vision_vit_h.safetensors
  loras/
    - lightx2v_I2V_14B_480p_cfg_step_distill_rank64_bf16.safetensors

ComfyUI 插件：
  - ComfyUI-GGUF              (UnetLoaderGGUF, CLIPLoaderGGUF)
  - ComfyUI-VideoHelperSuite  (VHS_VideoCombine)
  - ComfyUI-Frame-Interpolation [可选] (RIFE VFI)
  - ComfyUI 本体最新 Nightly  (WanImageToVideo)

用法：
  from renderer_video import generate_video_clip

  clip = generate_video_clip(
      image_path = "book_output/haunted_inn/page03_00058_.png",
      prompt     = "ghost woman slowly turns head, hair flowing in wind",
      out_dir    = "book_output/haunted_inn/",
      page_num   = 3,
      model_size = "14B-fast",      # 当前唯一推荐档位
      seed       = 42,
  )
"""
import logging
import warnings

# 抑制 comfy_script 的 _watch 噪音（NoneType._set_node_progress）
class _SuppressComfyWatch(logging.Filter):
    def filter(self, record):
        msg = record.getMessage()
        if "_set_node_progress" in msg or "Failed to watch" in msg:
            return False
        return True

# 给所有可能的 logger 都装上过滤器
for name in ["comfy_script", "comfy_script.runtime", "asyncio", "root"]:
    logging.getLogger(name).addFilter(_SuppressComfyWatch())

# stderr 直接输出的部分用 warnings 抑制
warnings.filterwarnings("ignore", message=".*_set_node_progress.*")

import time
from pathlib import Path
from typing import Optional

from config import COMFY_SERVER, GENERATE_TIMEOUT, POLL_INTERVAL, PROXIES
from renderer import (
    upload_image, get_all_prompt_ids, discover_new_prompt_id,
    wait_for_prompt, get_history_filenames, _fix_model_paths,
)

import requests


# ── 模型文件名常量 ────────────────────────────────────────

WAN_14B_HIGH = "Wan2.2-I2V-A14B-HighNoise-Q3_K_S.gguf"
WAN_14B_LOW  = "Wan2.2-I2V-A14B-LowNoise-Q3_K_S.gguf"
WAN_14B_VAE  = "Wan2.1_VAE.safetensors"
WAN_TEXT_ENC = "umt5-xxl-encoder-Q5_K_M.gguf"
WAN_CLIP_VIS = "clip_vision_vit_h.safetensors"
WAN_LIGHTX2V = "lightx2v_I2V_14B_480p_cfg_step_distill_rank64_bf16.safetensors"

# 后处理模型（可选）
#ESRGAN_MODEL = "RealESRGAN_x4plus_anime_6B.pth" 。##更适合动漫
ESRGAN_MODEL = "4x-UltraSharp.pth"
RIFE_CKPT    = "rife49.pth"


# ── 模型预设 ──────────────────────────────────────────────
# 命名规则：14B-{档位}
#   fast    : 480p 6 步，本地 3060 主力，单 clip 9-13 分钟（含 RIFE+ESRGAN）
#   quality : 留位，未来 4090 / 云端用，本地暂不开放
#
# 关键点：
#   - cfg=1.0 是 lightx2v 蒸馏强约束，写错会糊
#   - fps=16 是模型原生输出（启用 RIFE 后变 32）
#   - shift=5.0 平衡运动幅度和细节

WAN_MODELS = {
    "14B-fast": {
        "width":         832,
        "height":        480,
        "total_steps":   6,
        "high_end":      3,
        "cfg":           1.0,
        "shift":         5.0,
        "num_frames":    81,
        "fps_in":        16,
        "sampler":       "uni_pc",
        "scheduler":     "simple",
        "lora_strength": 1.0,
        # 后处理（自动探测，没插件就跳过）
        "use_rife":      True,
        "rife_multiplier": 2,
        "use_esrgan":    True,
    },
    # 竖版（抖音 / 快手），同档不同尺寸
    "14B-fast-vertical": {
        "width":         480,
        "height":        832,
        "total_steps":   6,
        "high_end":      3,
        "cfg":           1.0,
        "shift":         5.0,
        "num_frames":    81,
        "fps_in":        16,
        "sampler":       "uni_pc",
        "scheduler":     "simple",
        "lora_strength": 1.0,
        "use_rife":      True,
        "rife_multiplier": 2,
        "use_esrgan":    True,
    },
}


# ── ComfyUI 能力探测 ──────────────────────────────────────

_COMFY_CAPS_CACHE: Optional[set] = None


def _comfy_capabilities() -> set:
    """
    查询 ComfyUI /object_info，返回所有可用 class_type 的集合。
    用于运行时探测可选节点（RIFE / ESRGAN）。
    缓存在进程内。
    """
    global _COMFY_CAPS_CACHE
    if _COMFY_CAPS_CACHE is not None:
        return _COMFY_CAPS_CACHE
    try:
        r = requests.get(f"{COMFY_SERVER}/object_info",
                         timeout=10, proxies=PROXIES)
        if r.status_code == 200:
            _COMFY_CAPS_CACHE = set(r.json().keys())
        else:
            _COMFY_CAPS_CACHE = set()
    except Exception as e:
        print(f"  [wan] object_info 查询失败（功能探测降级）: {e}")
        _COMFY_CAPS_CACHE = set()
    return _COMFY_CAPS_CACHE


def _resolve_model_name(loader_class: str, input_key: str,
                        target: str) -> str:
    """
    大小写无关的文件名解析。
    例：磁盘上叫 "Wan2.2_VAE.safetensors"，代码里写 "wan2.2_vae.safetensors"，
    本函数会自动找回真实文件名。

    失败（找不到）时返回 target 本身，让 ComfyUI 报清晰的错误。
    """
    try:
        r = requests.get(f"{COMFY_SERVER}/object_info/{loader_class}",
                         timeout=10, proxies=PROXIES)
        if r.status_code != 200:
            return target
        info = r.json().get(loader_class, {})
        choices = info["input"]["required"][input_key][0]
        if not isinstance(choices, list):
            return target
        for c in choices:
            if c.lower() == target.lower():
                return c
        # 再尝试模糊匹配（忽略后缀）
        target_stem = target.lower().rsplit(".", 1)[0]
        for c in choices:
            if c.lower().rsplit(".", 1)[0] == target_stem:
                return c
    except Exception:
        pass
    return target


# ── ComfyUI Workflow（14B 双专家 + 后处理）─────────────────

def _build_wan_14b_workflow(image_server_name: str,
                            prompt: str,
                            cfg_dict: dict,
                            seed: int = 42) -> tuple[dict, int]:
    """
    构建 14B 双专家 workflow。
    返回 (workflow, final_fps) 元组。final_fps 是 VHS 实际写出的 fps。

    流程：
      LoadImage ─┬─► CLIPVisionEncode ─┐
                 │                       ▼
                 └──────────────────► WanImageToVideo (8) ─► [pos, neg, latent]
                                                                │
      UnetGGUF(High) ─► LoRA ─► ModelSamplingSD3 ──► KSamplerAdv (9a, 0→3)
                                                                │
      UnetGGUF(Low)  ─► LoRA ─► ModelSamplingSD3 ──► KSamplerAdv (9b, 3→6)
                                                                │
                                                       VAEDecode (10)
                                                                │
                                            [可选] RIFE VFI (10b, fps×2)
                                                                │
                                            [可选] ESRGAN 2× (10c)
                                                                │
                                                  VHS_VideoCombine (11)
    """
    caps = _comfy_capabilities()

    # 解析模型名（大小写自适应）
    high_name = _resolve_model_name("UnetLoaderGGUF", "unet_name", WAN_14B_HIGH)
    low_name  = _resolve_model_name("UnetLoaderGGUF", "unet_name", WAN_14B_LOW)
    vae_name  = _resolve_model_name("VAELoader",      "vae_name",  WAN_14B_VAE)
    cv_name   = _resolve_model_name("CLIPVisionLoader","clip_name", WAN_CLIP_VIS)
    txt_name  = _resolve_model_name("CLIPLoaderGGUF", "clip_name", WAN_TEXT_ENC)
    lora_name = _resolve_model_name("LoraLoaderModelOnly",
                                     "lora_name",  WAN_LIGHTX2V)

    w           = cfg_dict["width"]
    h           = cfg_dict["height"]
    total       = cfg_dict["total_steps"]
    high_end    = cfg_dict["high_end"]
    cfg_val     = cfg_dict["cfg"]
    shift       = cfg_dict["shift"]
    num_frames  = cfg_dict["num_frames"]
    fps_in      = cfg_dict["fps_in"]
    sampler     = cfg_dict["sampler"]
    scheduler   = cfg_dict["scheduler"]
    lora_str    = cfg_dict["lora_strength"]

    # 探测可选后处理节点
    rife_avail   = ("RIFE VFI" in caps) and cfg_dict.get("use_rife", False)
    esrgan_avail = ("ImageUpscaleWithModel" in caps
                    and "UpscaleModelLoader" in caps
                    and cfg_dict.get("use_esrgan", False))

    workflow: dict = {
        # ── HighNoise 专家链 ──
        "1a": {"class_type": "UnetLoaderGGUF",
               "inputs": {"unet_name": high_name}},
        "1b": {"class_type": "LoraLoaderModelOnly",
               "inputs": {"model": ["1a", 0],
                          "lora_name": lora_name,
                          "strength_model": lora_str}},
        "1c": {"class_type": "ModelSamplingSD3",
               "inputs": {"model": ["1b", 0], "shift": shift}},

        # ── LowNoise 专家链 ──
        "2a": {"class_type": "UnetLoaderGGUF",
               "inputs": {"unet_name": low_name}},
        "2b": {"class_type": "LoraLoaderModelOnly",
               "inputs": {"model": ["2a", 0],
                          "lora_name": lora_name,
                          "strength_model": lora_str}},
        "2c": {"class_type": "ModelSamplingSD3",
               "inputs": {"model": ["2b", 0], "shift": shift}},

        # ── 文本/VAE/起始帧 ──
        "3":  {"class_type": "CLIPLoaderGGUF",
               "inputs": {"clip_name": txt_name, "type": "wan"}},
        "4":  {"class_type": "VAELoader",
               "inputs": {"vae_name": vae_name}},
        "5":  {"class_type": "LoadImage",
               "inputs": {"image": image_server_name}},

        # ── CLIP Vision（14B 必需）──
        "8a": {"class_type": "CLIPVisionLoader",
               "inputs": {"clip_name": cv_name}},
        "8b": {"class_type": "CLIPVisionEncode",
               "inputs": {"clip_vision": ["8a", 0],
                          "image":       ["5", 0],
                          "crop":        "none"}},

        # ── 文本编码 ──
        "6":  {"class_type": "CLIPTextEncode",
               "inputs": {"text": prompt, "clip": ["3", 0]}},
        "7":  {"class_type": "CLIPTextEncode",
               "inputs": {
                   "text": (
                       "色调艳丽, 过曝, 静态, 细节模糊不清, 字幕, 风格, 作品, "
                       "画作, 画面, 静止, 整体发灰, 最差质量, 低质量, "
                       "JPEG压缩残留, 丑陋的, 残缺的, 多余的手指, "
                       "画得不好的手部, 画得不好的脸部, 畸形的, 毁容的, "
                       "形态畸形的肢体, 手指融合, 静止不动的画面, "
                       "杂乱的背景, 三条腿, 背景人很多, 倒着走"),
                   "clip": ["3", 0]}},

        # ── WanImageToVideo ──
        "8": {"class_type": "WanImageToVideo",
              "inputs": {"positive": ["6", 0], "negative": ["7", 0],
                         "vae":      ["4", 0],
                         "clip_vision_output": ["8b", 0],
                         "start_image": ["5", 0],
                         "width": w, "height": h,
                         "length": num_frames, "batch_size": 1}},

        # ── 双 KSampler ──
        "9a": {"class_type": "KSamplerAdvanced",
               "inputs": {"model": ["1c", 0],
                          "positive": ["8", 0], "negative": ["8", 1],
                          "latent_image": ["8", 2],
                          "noise_seed": seed, "add_noise": "enable",
                          "steps": total, "cfg": cfg_val,
                          "sampler_name": sampler, "scheduler": scheduler,
                          "start_at_step": 0, "end_at_step": high_end,
                          "return_with_leftover_noise": "enable"}},
        "9b": {"class_type": "KSamplerAdvanced",
               "inputs": {"model": ["2c", 0],
                          "positive": ["8", 0], "negative": ["8", 1],
                          "latent_image": ["9a", 0],
                          "noise_seed": seed, "add_noise": "disable",
                          "steps": total, "cfg": cfg_val,
                          "sampler_name": sampler, "scheduler": scheduler,
                          "start_at_step": high_end, "end_at_step": total,
                          "return_with_leftover_noise": "disable"}},

        # ── VAE 解码 ──
        "10": {"class_type": "VAEDecode",
               "inputs": {"samples": ["9b", 0], "vae": ["4", 0]}},
    }

    # ── 可选：RIFE 帧插值 ──
    images_node: list = ["10", 0]
    final_fps = fps_in

    if rife_avail:
        mult = cfg_dict.get("rife_multiplier", 2)
        workflow["10b"] = {
            "class_type": "RIFE VFI",
            "inputs": {
                "frames": images_node,
                "ckpt_name": RIFE_CKPT,
                "clear_cache_after_n_frames": 10,
                "multiplier": mult,
                "fast_mode": True,
                "ensemble": True,
                "scale_factor": 1.0,
            },
        }
        images_node = ["10b", 0]
        final_fps = fps_in * mult
        print(f"  [wan] ✓ RIFE 已启用 ({fps_in} → {final_fps} fps)")
    else:
        print(f"  [wan] ⚠ RIFE 未安装，输出原生 {fps_in} fps（producer 会兜底归一化）")

    # ── 可选：ESRGAN 2× 放大 ──
    if esrgan_avail:
        esrgan_name = _resolve_model_name("UpscaleModelLoader",
                                          "model_name", ESRGAN_MODEL)
        workflow["10c"] = {
            "class_type": "UpscaleModelLoader",
            "inputs": {"model_name": esrgan_name},
        }
        workflow["10d"] = {
            "class_type": "ImageUpscaleWithModel",
            "inputs": {
                "upscale_model": ["10c", 0],
                "image":         images_node,
            },
        }
        # 4x 模型要缩回 2x，避免 producer 端再次缩放
        workflow["10e"] = {
            "class_type": "ImageScale",
            "inputs": {
                "image": ["10d", 0],
                "upscale_method": "lanczos",
                "width":  w * 2,
                "height": h * 2,
                "crop":   "disabled",
            },
        }
        images_node = ["10e", 0]
        print(f"  [wan] ✓ ESRGAN 2× 已启用 ({w}×{h} → {w*2}×{h*2})")
    else:
        print(f"  [wan] ⚠ ESRGAN 未启用，输出原生 {w}×{h}")

    # ── 视频输出 ──
    workflow["11"] = {
        "class_type": "VHS_VideoCombine",
        "inputs": {
            "images":          images_node,
            "frame_rate":      final_fps,
            "loop_count":      0,
            "filename_prefix": "wan_14b_i2v",
            "format":          "video/h264-mp4",
            "pingpong":        False,
            "save_output":     True,
        },
    }

    return workflow, final_fps


# ── 提交 + 下载 ──────────────────────────────────────────

def _submit_workflow(workflow: dict) -> str:
    payload = {"prompt": workflow}
    r = requests.post(f"{COMFY_SERVER}/prompt",
                      json=payload, timeout=30, proxies=PROXIES)
    if r.status_code != 200:
        raise RuntimeError(f"ComfyUI 提交失败: {r.status_code} {r.text}")
    return r.json().get("prompt_id", "")


def _download_video_outputs(outputs: dict, prefix: str,
                            save_dir: str) -> list:
    Path(save_dir).mkdir(parents=True, exist_ok=True)
    saved = []
    for node_id, node_output in outputs.items():
        for vid in node_output.get("gifs", []):
            fname = vid.get("filename", "")
            subfolder = vid.get("subfolder", "")
            if not fname:
                continue

            save_path = Path(save_dir) / f"{prefix}_{fname}"
            if save_path.exists():
                saved.append(str(save_path))
                continue

            params = {"filename": fname, "type": "output"}
            if subfolder:
                params["subfolder"] = subfolder
            url = f"{COMFY_SERVER}/view?{requests.compat.urlencode(params)}"

            for attempt in range(3):
                try:
                    response = requests.get(url, timeout=60, proxies=PROXIES)
                    if response.status_code == 200:
                        with open(save_path, "wb") as f:
                            f.write(response.content)
                        print(f"  [video] 已保存: {save_path.name}")
                        saved.append(str(save_path))
                        break
                except Exception as e:
                    print(f"  [video] 下载失败({attempt+1}/3): {e}")
                    time.sleep(3)
    return saved


# ── 对外接口 ─────────────────────────────────────────────

def generate_video_clip(image_path: str,
                        prompt: str,
                        out_dir: str,
                        page_num: int,
                        model_size: str = "14B-fast",
                        seed: int = 42,
                        timeout: int = 1800) -> Optional[str]:
    """
    生成一个 Wan 2.2 I2V-A14B AI 视频 clip。

    参数:
      image_path: 起始帧图片路径（本地路径，会上传到 ComfyUI）
      prompt:     视频运动描述
      out_dir:    输出目录
      page_num:   页码（用于命名）
      model_size: "14B-fast" (默认) / "14B-fast-vertical" (竖版)
      seed:       随机种子
      timeout:    超时秒数（含 RIFE+ESRGAN 建议 1200s）

    返回:
      视频文件路径；失败返回 None
    """
    cfg_dict = WAN_MODELS.get(model_size)
    if not cfg_dict:
        print(f"  [wan] 不支持的模型: {model_size}，"
              f"可选: {list(WAN_MODELS.keys())}")
        return None

    print(f"  [wan] Wan 2.2 I2V-A14B [{model_size}]")
    print(f"  [wan] 起始帧: {Path(image_path).name}")
    print(f"  [wan] Prompt: {prompt[:80]}{'...' if len(prompt) > 80 else ''}")
    print(f"  [wan] 分辨率: {cfg_dict['width']}×{cfg_dict['height']}, "
          f"帧数: {cfg_dict['num_frames']} @ {cfg_dict['fps_in']}fps, "
          f"步数: {cfg_dict['total_steps']} (high {cfg_dict['high_end']} + "
          f"low {cfg_dict['total_steps'] - cfg_dict['high_end']})")

    # 上传起始帧
    try:
        server_name = upload_image(image_path)
        print(f"  [wan] 已上传: {server_name}")
    except Exception as e:
        print(f"  [wan] 上传失败: {e}")
        return None

    prefix = f"page{page_num:02d}_wan14b"
    workflow, final_fps = _build_wan_14b_workflow(
        server_name, prompt, cfg_dict, seed)

    # 提交
    try:
        prompt_id = _submit_workflow(workflow)
        if not prompt_id:
            print(f"  [wan] 提交失败：未获得 prompt_id")
            return None
        print(f"  [wan] prompt_id: {prompt_id[:16]}...")
    except Exception as e:
        print(f"  [wan] 提交失败: {e}")
        return None

    # 等待完成
    print(f"  [wan] 等待中（超时 {timeout}s）...")
    result = wait_for_prompt(prompt_id, timeout=timeout)

    if result["status"] == "error":
        msg = result["message"]
        print(f"  [wan] ComfyUI 错误: {msg}")
        if "WanImageToVideo" in msg:
            print(f"  → 节点不存在: ComfyUI 需要更新到最新 Nightly")
        if "UnetLoaderGGUF" in msg or "CLIPLoaderGGUF" in msg:
            print(f"  → GGUF 节点不存在: 装 ComfyUI-GGUF (city96)")
        if "VHS_VideoCombine" in msg:
            print(f"  → 视频节点不存在: 装 ComfyUI-VideoHelperSuite")
        if "RIFE VFI" in msg:
            print(f"  → RIFE 节点不存在: 装 ComfyUI-Frame-Interpolation")
        if any(n in msg for n in [WAN_14B_HIGH, WAN_14B_LOW]):
            print(f"  → 14B 模型缺失: {WAN_14B_HIGH}/{WAN_14B_LOW}")
        if WAN_14B_VAE in msg or "vae_name" in msg.lower():
            print(f"  → VAE 缺失（14B 用 Wan2.1_VAE，不是 Wan2.2）")
        if "clip_vision" in msg.lower():
            print(f"  → CLIP Vision 缺失: {WAN_CLIP_VIS}")
        if "out of memory" in msg.lower() or "oom" in msg.lower():
            print(f"  → 显存不足: 关掉 use_esrgan 或减小 num_frames")
        return None

    if result["status"] == "timeout":
        print(f"  [wan] 超时 (>{timeout}s)")
        return None

    # 下载视频
    saved = _download_video_outputs(result["outputs"], prefix, out_dir)
    if saved:
        print(f"  [wan] ✓ 视频生成完成: {Path(saved[0]).name} "
              f"(@ {final_fps}fps)")
        return saved[0]
    print(f"  [wan] 未找到视频输出")
    return None
