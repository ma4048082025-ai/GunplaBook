"""
compositor.py ── 多角色合成模块
================================
流程：单独生图 → rembg 抠图 → Pillow 合成 → ComfyUI img2img 融合

不变原则：
  这个文件不出现任何与主题相关的字符串。
  布局预设从 ThemeConfig.layout_for() 读取。
  提示词前缀从 ThemeConfig.prompts 读取。
"""

import io
import logging
import random
import subprocess
import time
import urllib.parse
import urllib.request
from pathlib import Path

import requests
from PIL import Image, ImageFilter

from config import COMFY_SERVER, OUT_DIR, PROXIES, REMBG_SERVER
logging.getLogger("comfy_script").setLevel(logging.ERROR)


# ════════════════════════════════════════
# Step 1：构建单角色生图参数
# ════════════════════════════════════════
def make_solo_params(base_params: dict, char_name: str,
                     characters: dict, theme) -> dict:
    """
    派生单角色专用参数。
    solo_background 从主题包读取（不同主题背景色不同）。
    """
    char = characters[char_name]
    p    = theme.prompts
    solo = base_params.copy()

    solo["positive"] = (
        p.positive_prefix
        + char.get("trigger_solo", char.get("trigger_multi", ""))
        + f", solo, ({p.solo_background}:1.3), full body visible, "
          "standing pose, centered in frame, "
          "no other characters, 4k, sharp focus, simple clean background"
    )
    solo["negative"] = (
        p.negative_prefix
        + "multiple characters, other characters, complex background, "
          "gradient background, scenery, crowd, "
          "blown out background, cropped, cut off, partial body"
    )
    solo["lora"]           = char["lora"]
    solo["lora_strength"]  = char.get("strength", 1.0)
    solo["lora2"]          = None
    solo["lora2_strength"] = 0.0
    solo["ip_ref"]         = None
    solo["prefix"]         = base_params["prefix"] + f"_{char_name}_solo"
    return solo


# ════════════════════════════════════════
# Step 2：rembg 抠图
# ════════════════════════════════════════
def _rembg_via_http(image_path: str) -> bytes | None:
    try:
        with open(image_path, "rb") as f:
            r = requests.post(
                REMBG_SERVER,
                files={"file": (Path(image_path).name, f, "image/png")},
                timeout=60,
                proxies=PROXIES,
            )
        return r.content if r.status_code == 200 else None
    except requests.exceptions.ConnectionError:
        print(f"  [rembg] Server 未连接，降级本地模式")
        return None
    except Exception as e:
        print(f"  [rembg] 异常: {e}，降级本地模式")
        return None


def extract_with_rembg(image_path: str) -> str:
    out_path = str(Path(image_path).with_suffix("")) + "_nobg.png"

    png_bytes = _rembg_via_http(image_path)
    if png_bytes:
        result = Image.open(io.BytesIO(png_bytes)).convert("RGBA")
        alpha  = result.split()[3].filter(ImageFilter.GaussianBlur(2))
        result.putalpha(alpha)
        result.save(out_path, "PNG")
        print(f"  [抠图/HTTP] → {Path(out_path).name}")
        return out_path

    try:
        from rembg import remove
        img    = Image.open(image_path).convert("RGBA")
        result = remove(img)
        alpha  = result.split()[3].filter(ImageFilter.GaussianBlur(2))
        result.putalpha(alpha)
        result.save(out_path, "PNG")
        print(f"  [抠图/本地] → {Path(out_path).name}")
        return out_path
    except ImportError:
        raise ImportError(
            "rembg 不可用。请启动 Docker server：\n"
            "  docker run -d --name rembg_server --restart unless-stopped \\\n"
            "    -p 7777:7777 danielgatis/rembg s --host 0.0.0.0 --port 7777"
        )


# ════════════════════════════════════════
# 人工确认检查点
# ════════════════════════════════════════
def _solo_review_checkpoint(char_name: str, img_path: str) -> str | None:
    try:
        subprocess.Popen(["open", img_path])
    except Exception:
        pass
    print(f"\n  ┌─ solo 确认：{char_name} {'─'*30}")
    print(f"  │  {Path(img_path).name}")
    print(f"  │  回车=OK  r=重生成  s=跳过  q=放弃此页")
    print(f"  └{'─'*42}")
    choice = input(f"  [{char_name}] > ").strip().lower()
    if choice in ("r", "retry"):
        return "retry"
    if choice in ("q", "quit"):
        return None
    return img_path


# ════════════════════════════════════════
# Step 3：Pillow 合成
# ════════════════════════════════════════
def composite_scene(bg_path: str, char_paths: list,
                    layout: list[dict], output_path: str) -> str:
    """
    将已抠图的角色贴到背景上。
    layout 从主题包的 ThemeConfig.layout_for(scene_type) 获取，
    不再接受 scene_type 字符串——布局规则由调用方决定，不由这里决定。
    """
    bg     = Image.open(bg_path).convert("RGBA")
    W, H   = bg.size
    canvas = bg.copy()

    for char_path, pos in reversed(list(zip(char_paths, layout))):
        char  = Image.open(char_path).convert("RGBA")
        new_h = int(H * pos["scale"])
        new_w = int(char.width * (new_h / char.height))
        char  = char.resize((new_w, new_h), Image.LANCZOS)
        x     = min(int(W * pos["px"]), W - new_w)
        y     = min(int(H * pos["py"]), H - new_h)
        canvas.paste(char, (x, y), char)

    canvas.convert("RGB").save(output_path, "PNG", quality=95)
    print(f"  [合成] → {Path(output_path).name}")
    return output_path


# ════════════════════════════════════════
# Step 4：ComfyUI img2img 融合重绘
# ════════════════════════════════════════
def _upload_to_comfy(local_path: str) -> str:
    p = Path(local_path)
    with open(local_path, "rb") as f:
        r = requests.post(
            f"{COMFY_SERVER}/upload/image",
            files={"image": (p.name, f, "image/png")},
            timeout=30,
            proxies=PROXIES,
        )
    r.raise_for_status()
    return r.json()["name"]


def _download_with_retry(url: str, dest: str, retries: int = 3):
    for attempt in range(retries):
        try:
            urllib.request.urlretrieve(url, dest)
            return
        except Exception as e:
            if Path(dest).exists():
                Path(dest).unlink()
            if attempt < retries - 1:
                print(f"  [下载] 失败({attempt+1}/{retries})，重试: {e}")
                time.sleep(2)
            else:
                raise


def comfy_harmonize(params: dict, composite_path: str,
                    theme, timeout: int = 360,
                    denoise_override: float = None) -> str:
    """
    低 denoise img2img 融合。
    harmonize 的附加词和 denoise 值从主题包读取，不硬编码。
    denoise_override: 不为 None 时覆盖主题包的 harmonize_denoise（供重试时加强融合）
    """
    p       = theme.prompts
    g       = theme.generation
    denoise = denoise_override if denoise_override is not None else g.harmonize_denoise
    server_name = _upload_to_comfy(composite_path)

    # 融合词从主题包读取
    harmonize_pos = params["positive"] + ", " + p.harmonize_append
    harmonize_neg = params["negative"] + ", " + p.harmonize_negative

    # 模型名从 params 读取（params 由 orchestrator 从主题包注入）
    checkpoint = params.get("_checkpoint", "")
    if not checkpoint:
        raise ValueError("params 中缺少 _checkpoint，请检查 orchestrator 的 build_prompts")

    model_ref = ["3", 0]
    clip_ref  = ["3", 1]

    wf: dict = {
        "1": {"class_type": "CheckpointLoaderSimple",
              "inputs": {"ckpt_name": checkpoint}},
        "2": {"class_type": "CLIPSetLastLayer",
              "inputs": {"clip": ["1", 1], "stop_at_clip_layer": -2}},
        "3": {"class_type": "LoraLoader",
              "inputs": {
                  "model": ["1", 0], "clip": ["2", 0],
                  "lora_name":      params["lora"],
                  "strength_model": round(params["lora_strength"] * 0.8, 2),
                  "strength_clip":  round(params["lora_strength"] * 0.8, 2),
              }},
    }

    if params.get("lora2"):
        wf["3b"] = {"class_type": "LoraLoader",
                    "inputs": {
                        "model": ["3", 0], "clip": ["3", 1],
                        "lora_name":      params["lora2"],
                        "strength_model": round(params.get("lora2_strength", 0.5) * 0.6, 2),
                        "strength_clip":  round(params.get("lora2_strength", 0.5) * 0.6, 2),
                    }}
        model_ref = ["3b", 0]
        clip_ref  = ["3b", 1]

    wf.update({
        "4":  {"class_type": "CLIPTextEncode",
               "inputs": {"clip": clip_ref, "text": harmonize_pos}},
        "5":  {"class_type": "CLIPTextEncode",
               "inputs": {"clip": clip_ref, "text": harmonize_neg}},
        "6":  {"class_type": "LoadImage",
               "inputs": {"image": server_name}},
        "7":  {"class_type": "VAEEncode",
               "inputs": {"pixels": ["6", 0], "vae": ["1", 2]}},
        "8":  {"class_type": "KSampler",
               "inputs": {
                   "model":        model_ref,
                   "seed":         params["seed"],
                   "steps":        g.harmonize_steps,
                   "cfg":          params["cfg"],
                   "sampler_name": "dpmpp_2m",
                   "scheduler":    "karras",
                   "positive":     ["4", 0],
                   "negative":     ["5", 0],
                   "latent_image": ["7", 0],
                   "denoise":      denoise,
               }},
        "9":  {"class_type": "VAEDecode",
               "inputs": {"samples": ["8", 0], "vae": ["1", 2]}},
        "10": {"class_type": "SaveImage",
               "inputs": {
                   "images":          ["9", 0],
                   "filename_prefix": params["prefix"] + "_harmonized",
               }},
    })

    r = requests.post(
        f"{COMFY_SERVER}/prompt",
        json={"prompt": wf},
        timeout=30,
        proxies=PROXIES,
    )
    r.raise_for_status()
    prompt_id = r.json().get("prompt_id", "")
    print(f"  [融合] prompt_id={prompt_id[:12]}...")

    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(3)
        try:
            hist  = requests.get(
                f"{COMFY_SERVER}/history/{prompt_id}",
                timeout=10, proxies=PROXIES,
            ).json()
            if prompt_id not in hist:
                continue
            entry      = hist[prompt_id]
            status_str = entry.get("status", {}).get("status_str", "")
            if status_str == "error":
                print(f"  [融合] ComfyUI 报错，返回原合成图")
                return composite_path
            for node_out in entry.get("outputs", {}).values():
                for img in node_out.get("images", []):
                    fname = img["filename"]
                    if "harmonized" not in fname:
                        continue
                    save_path = Path(OUT_DIR) / fname
                    ps = urllib.parse.urlencode(
                        {"filename": fname, "type": "output"})
                    _download_with_retry(
                        f"{COMFY_SERVER}/view?{ps}", str(save_path))
                    print(f"  [融合] 完成 → {fname}")
                    return str(save_path)
        except Exception as e:
            print(f"  [融合] 轮询: {e}")

    print(f"  [融合] 超时，返回原合成图")
    return composite_path


# ════════════════════════════════════════
# 主入口
# ════════════════════════════════════════
def multi_char_pipeline(base_params: dict, char_names: list,
                        scene_type: str, scene_cfg: dict,
                        out_dir: str, characters: dict,
                        theme, interactive: bool = True,
                        return_composite: bool = False) -> str | tuple | None:
    """
    多角色合成流水线（同步函数）。
    布局预设从 theme.layout_for(scene_type) 获取。
    提示词前缀从 theme.prompts 获取。
    return_composite=True 时返回 (harmonized_path, composite_path) 元组，
    供上层在质量失败时重新 harmonize 而无需重生成角色。
    """
    from renderer import generate_and_wait, comfy_generate

    # 过滤纯风格增强角色（规则来自主题包）
    real_chars = [c for c in char_names if c not in theme.style_only_chars]
    if len(real_chars) < 2:
        print(f"  [多角色] 独立角色 < 2（{real_chars}），降级")
        return None

    chars_to_use = real_chars[:2]
    Path(out_dir).mkdir(parents=True, exist_ok=True)
    char_images: list[str] = []

    # Step 1+2：逐角色生图 + 抠图
    for char_name in chars_to_use:
        print(f"\n  [多角色] ── {char_name} ──")
        solo_p    = make_solo_params(base_params, char_name, characters, theme)
        final_img = None

        for retry in range(3):
            if retry > 0:
                solo_p["seed"] = random.randint(10000, 99999)
                print(f"  重新生图 seed={solo_p['seed']}")

            saved = generate_and_wait(
                comfy_generate, solo_p,
                solo_p["prefix"], out_dir, timeout=600)
            if not saved:
                continue

            solo_img = max(saved, key=lambda p: Path(p).stat().st_mtime)

            if interactive:
                decision = _solo_review_checkpoint(char_name, solo_img)
                if decision is None:
                    return None
                if decision == "retry":
                    continue

            final_img = solo_img
            break

        if not final_img:
            print(f"  [多角色] {char_name} 多次失败 → 降级")
            return None

        char_images.append(extract_with_rembg(final_img))

    if len(char_images) < 2:
        return None

    # Step 3a：生成背景（提示词从主题包读取）
    print(f"\n  [多角色] ── 背景生图 ──")
    p    = theme.prompts
    bg_p = base_params.copy()
    bg_p["positive"] = (
        p.positive_prefix
        + "(deep space:1.4), (starfield:1.3), (nebula:1.2), "
        + scene_cfg.get("scene", "cinematic background")
        + ", " + p.background_append
    )
    bg_p["negative"] = p.negative_prefix + p.background_negative
    bg_p["lora2"]    = None
    bg_p["ip_ref"]   = None
    bg_p["prefix"]   = base_params["prefix"] + "_bg"

    bg_saved = generate_and_wait(
        comfy_generate, bg_p, bg_p["prefix"], out_dir, timeout=600)
    if not bg_saved:
        return None
    bg_path = max(bg_saved, key=lambda p: Path(p).stat().st_mtime)

    # Step 3b：合成（布局从主题包读取）
    layout         = theme.layout_for(scene_type)
    composite_path = str(Path(out_dir) / f"{base_params['prefix']}_composite.png")
    composite_scene(bg_path, char_images, layout, composite_path)

    # Step 4：融合（参数从主题包读取）
    print(f"\n  [多角色] ── img2img 融合 ──")
    result = comfy_harmonize(base_params, composite_path, theme)
    if return_composite:
        return result, composite_path
    return result

import random
