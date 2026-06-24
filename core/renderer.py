"""
renderer.py ── ComfyUI 生图层
==============================
不变原则：
  这个文件不出现任何模型文件名。
  模型名称通过 params["_checkpoint"] / params["_ipadapter"] 注入，
  由 orchestrator.build_prompts() 从主题包写入。

职责：
  1. 提交 ComfyUI workflow
  2. 等待完成（prompt_id 轮询）
  3. 下载结果（带重试）

路径修正（v3）：
  generate_and_wait() 入口统一调用 _fix_model_paths()，
  自动将所有 ComfyUI 模型名称的路径分隔符标准化为目标服务器 OS。
  workflow 函数内部不再需要任何 fix_path() 调用。

v2 新增：
  comfy_reactor_swap()    — ReActor 人脸替换后处理（Phase 1，立即可用）
  comfy_faceid_generate() — FaceID IP-Adapter 生图（Phase 2，需要下载模型）
"""

import asyncio
import time
import urllib.request
import urllib.parse
from pathlib import Path

import requests

from config import (
    COMFY_SERVER, GENERATE_TIMEOUT, UPSCALE_TIMEOUT,
    POLL_INTERVAL, PROXIES, comfy_path,
)

# 上传缓存
_upload_cache: dict[str, str] = {}

# ── ComfyUI 模型名称字段（路径分隔符需随服务器 OS 标准化）──
# 注意：_source_path / ip_ref / face_ref 是本地文件，走 upload()，不在此列
_COMFY_MODEL_KEYS = (
    "lora", "lora2",
    "_checkpoint", "_ipadapter", "_clip_vision",
    "_faceid_model", "_faceid_vision",
    "_unet", "_clip1", "_clip2", "_vae",  # FLUX 新增
)


def _fix_model_paths(params: dict) -> dict:
    """
    统一入口：标准化 params 里所有 ComfyUI 模型名称的路径分隔符。

    在 generate_and_wait() 开头调用，所有 workflow 函数自动受益，
    无需在各 workflow 内部写 fix_path()。

    只处理 _COMFY_MODEL_KEYS 列出的字段，本地文件路径字段不动：
      - _source_path → 由 upload_image() 上传后 ComfyUI 用服务器名引用
      - ip_ref       → 由 get_or_upload_ref() 上传
      - face_ref     → 由 get_or_upload_ref() 上传
    """
    fixed = params.copy()
    for key in _COMFY_MODEL_KEYS:
        if fixed.get(key):
            original = fixed[key]
            fixed[key] = comfy_path(original)
            if fixed[key] != original:
                print(f"  [路径修正] {key}: '{original}' → '{fixed[key]}'")
    return fixed


def upload_image(local_path: str) -> str:
    p = Path(local_path)
    retries = 3
    for attempt in range(retries):
        try:
            with open(local_path, "rb") as f:
                r = requests.post(
                    f"{COMFY_SERVER}/upload/image",
                    files={"image": (p.name, f, "image/png")},
                    proxies=PROXIES,
                    timeout=30,
                )
            return r.json()["name"]
        except Exception as e:
            if attempt < retries - 1:
                wait = (attempt + 1) * 5
                print(f"  [上传] 失败({attempt + 1}/{retries})，等待{wait}s: {e}")
                time.sleep(wait)
            else:
                raise


def get_or_upload_ref(local_path: str) -> str:
    if local_path in _upload_cache:
        print(f"  参考图已缓存: {Path(local_path).name}")
        return _upload_cache[local_path]
    server_name = upload_image(local_path)
    _upload_cache[local_path] = server_name
    print(f"  参考图已上传: {Path(local_path).name} -> {server_name}")
    return server_name


# ── prompt_id 追踪 ─────────────────────────────────────

def get_all_prompt_ids() -> set:
    ids = set()
    try:
        h = requests.get(f"{COMFY_SERVER}/history",
                         timeout=10, proxies=PROXIES).json()
        ids.update(h.keys())
    except Exception:
        pass
    try:
        q = requests.get(f"{COMFY_SERVER}/queue",
                         timeout=10, proxies=PROXIES).json()
        for item in q.get("queue_running", []) + q.get("queue_pending", []):
            if len(item) > 1:
                ids.add(item[1])
    except Exception:
        pass
    return ids


def discover_new_prompt_id(known_ids: set, timeout: int = 30) -> str:
    start = time.time()
    while time.time() - start < timeout:
        new_ids = get_all_prompt_ids() - known_ids
        if new_ids:
            return new_ids.pop()
        time.sleep(0.5)
    return ""


def wait_for_prompt(prompt_id: str, timeout: int = 600) -> dict:
    start    = time.time()
    last_log = 0
    while time.time() - start < timeout:
        try:
            r = requests.get(
                f"{COMFY_SERVER}/history/{prompt_id}",
                timeout=10, proxies=PROXIES).json()
            if prompt_id in r:
                entry      = r[prompt_id]
                status_str = entry.get("status", {}).get("status_str", "")
                if status_str == "error":
                    messages = entry.get("status", {}).get("messages", [])
                    err_msg  = "ComfyUI 执行错误"
                    for msg in messages:
                        if isinstance(msg, list) and msg[0] == "execution_error":
                            detail  = msg[1] if isinstance(msg[1], dict) else {}
                            err_msg = detail.get("exception_message", str(msg[1]))
                            break
                    return {"status": "error", "message": err_msg}
                outputs = entry.get("outputs", {})
                if outputs:
                    return {"status": "success", "outputs": outputs}
        except Exception:
            pass
        elapsed = int(time.time() - start)
        if elapsed - last_log >= 30:
            print(f"  ... 已等待 {elapsed}s", flush=True)
            last_log = elapsed
        time.sleep(POLL_INTERVAL)
    return {"status": "timeout", "message": f"超过 {timeout}s"}


def get_history_filenames() -> set:
    existing = set()
    try:
        h = requests.get(f"{COMFY_SERVER}/history",
                         timeout=10, proxies=PROXIES).json()
        for d in h.values():
            for out in d.get("outputs", {}).values():
                for img in out.get("images", []):
                    existing.add(img["filename"])
    except Exception:
        pass
    return existing


def download_outputs(outputs, prefix, save_dir, existing_files=None):
    Path(save_dir).mkdir(parents=True, exist_ok=True)
    saved = []
    for node_output in outputs.values():
        for img in node_output.get("images", []):
            fname = img.get("filename", "")
            if not fname.startswith(prefix) or fname.endswith(".bak"):
                continue
            if existing_files and fname in existing_files:
                continue
            save_path = Path(save_dir) / fname

            if not save_path.exists():
                ps  = urllib.parse.urlencode({"filename": fname, "type": "output"})
                url = f"{COMFY_SERVER}/view?{ps}"

                for attempt in range(5):
                    try:
                        response = requests.get(url, timeout=30, proxies=PROXIES)
                        if response.status_code == 200:
                            with open(save_path, "wb") as f:
                                f.write(response.content)
                            print(f"  已保存: {save_path}")
                            saved.append(str(save_path))
                            break
                        else:
                            print(f"  HTTP {response.status_code} 错误: {fname}")
                    except Exception as e:
                        if save_path.exists():
                            save_path.unlink()
                        print(f"  下载失败({attempt + 1}/5): {fname}, 错误: {e}")
                        if attempt < 4:
                            time.sleep(3)
                else:
                    print(f"  !! 最终下载失败，跳过: {fname}")
            else:
                print(f"  已存在: {save_path}")
                saved.append(str(save_path))
    return saved


# ── 核心：generate_and_wait ────────────────────────────

def generate_and_wait(workflow_func, params: dict,
                      prefix: str, save_dir: str,
                      timeout: int = 600) -> list:
    # ★ 统一路径修正：所有 workflow 经此入口，模型名自动适配服务器 OS
    params = _fix_model_paths(params)

    existing_files = get_history_filenames()
    known_ids      = get_all_prompt_ids()

    asyncio.run(workflow_func(params))

    prompt_id = discover_new_prompt_id(known_ids, timeout=30)
    if not prompt_id:
        print(f"  !! 未捕获 prompt_id，任务提交失败，跳过等待")
        return []

    print(f"  prompt_id: {prompt_id[:16]}...")
    print(f"  等待中（超时{timeout}s）...", flush=True)
    result = wait_for_prompt(prompt_id, timeout=timeout)

    if result["status"] == "error":
        print(f"\n  !! ComfyUI 错误: {result['message']}")
        if "Resampler" in result["message"]:
            print(f"  → IP-Adapter 版本不匹配，运行 diagnose 命令")
        if "not in" in result["message"] and "sampler" in result["message"].lower():
            print(f"  → Sampler 名称不合法，检查 param_learner 的 EXPLORE_SAMPLERS")
        if "not in" in result["message"] and ("lora" in result["message"].lower()
                                               or "safetensors" in result["message"].lower()):
            print(f"  → LoRA 文件未找到，请检查:")
            print(f"    1. ComfyUI 服务器上文件是否存在")
            print(f"    2. YAML 里的路径是否与服务器目录结构一致")
            print(f"    3. 当前服务器 OS: COMFY_SERVER_OS='{__import__('config').COMFY_SERVER_OS}'")
        return []

    if result["status"] == "timeout":
        print(f"\n  !! 生成超时（>{timeout}s），跳过此次，换 seed 重试")
        return []

    print(f"  生成完成!")
    return download_outputs(result["outputs"], prefix, save_dir, existing_files)


# ── workflows ──────────────────────────────────────────
# 注意：以下所有 workflow 函数直接使用 params 里的值，
# 路径修正已在 generate_and_wait() 入口统一完成。

async def comfy_generate(params: dict):
    """
    普通生图 / IP-Adapter 生图。
    模型名称：params["_checkpoint"] / params["_ipadapter"] / params["_clip_vision"]
    """
    from comfy_script.runtime.nodes import (
        CheckpointLoaderSimple, CLIPSetLastLayer, LoraLoader,
        CLIPTextEncode, KSampler, VAEDecode, SaveImage,
        LoadImage, EmptyLatentImage,
    )
    import comfy_script.runtime.nodes as _nodes
    IPAdapterModelLoader = getattr(_nodes, "IPAdapterModelLoader", None)
    CLIPVisionLoader     = getattr(_nodes, "CLIPVisionLoader", None)
    IPAdapterAdvanced    = getattr(_nodes, "IPAdapterAdvanced", None)

    from comfy_script.runtime import Workflow

    checkpoint  = params.get("_checkpoint", "")
    ipadapter   = params.get("_ipadapter", "")
    clip_vision = params.get("_clip_vision", "")
    ip_ref      = params.get("ip_ref")

    if not checkpoint:
        raise ValueError(
            "params 缺少 _checkpoint。"
            "请检查 orchestrator.build_prompts() 是否正确注入主题模型配置。"
        )

    mode = "IP-Adapter" if ip_ref else "普通"
    print(f"  {mode}生图模式")

    with Workflow():
        model, clip, vae = CheckpointLoaderSimple(checkpoint)
        clip = CLIPSetLastLayer(clip, stop_at_clip_layer=-2)

        if params.get("lora"):
            model, clip = LoraLoader(
                model, clip,
                lora_name      = params["lora"],
                strength_model = params["lora_strength"],
                strength_clip  = params["lora_strength"])

        if params.get("lora2"):
            model, clip = LoraLoader(
                model, clip,
                lora_name      = params["lora2"],
                strength_model = params["lora2_strength"],
                strength_clip  = params["lora2_strength"])

        pos = CLIPTextEncode(params["positive"], clip)
        neg = CLIPTextEncode(params["negative"], clip)

        if ip_ref and ipadapter and clip_vision:
            if IPAdapterAdvanced is None:
                print("  [警告] IPAdapterAdvanced 节点未找到，跳过 IP-Adapter")
            else:
                ref_server = get_or_upload_ref(ip_ref)
                ref_img, _ = LoadImage(ref_server)
                ip_model   = IPAdapterModelLoader(ipadapter)
                clip_vis   = CLIPVisionLoader(clip_vision)
                model      = IPAdapterAdvanced(
                    model, ip_model, clip_vis, ref_img, pos,
                    weight=0.7, weight_type="linear",
                    combine_embeds="concat",
                    start_at=0.0, end_at=0.9,
                    embeds_scaling="V only",
                )

        latent       = EmptyLatentImage(1536, 1536, 1)
        sampler_name = params.get("sampler", "dpmpp_2m")
        scheduler    = params.get("scheduler", "karras")
        sampled      = KSampler(
            model, params["seed"], params["steps"], params["cfg"],
            sampler_name, scheduler, pos, neg, latent, 1.0,
        )
        image = VAEDecode(sampled, vae)
        SaveImage(image, params["prefix"])


async def comfy_generate_flux(params: dict):
    """
    FLUX GGUF 生图。
    与 comfy_generate 的主要区别：
      - 用 UnetLoaderGGUF 替代 CheckpointLoaderSimple
      - 用 DualCLIPLoader 加载 clip_l + t5xxl
      - 用 SamplerCustomAdvanced 替代 KSampler
      - CFG 范围 1-5，步数 15-25
      - 支持双人场景直接 prompt 描述（不需要 compositor）
    """
    import comfy_script.runtime.nodes as _nodes

    UnetLoaderGGUF = getattr(_nodes, "UnetLoaderGGUF", None)
    DualCLIPLoader = getattr(_nodes, "DualCLIPLoader", None)
    VAELoader = getattr(_nodes, "VAELoader", None)
    FluxGuidance = getattr(_nodes, "FluxGuidance", None)

    # 采样链节点（FLUX 专用）
    RandomNoise = getattr(_nodes, "RandomNoise", None)
    BasicGuider = getattr(_nodes, "BasicGuider", None)
    KSamplerSelect = getattr(_nodes, "KSamplerSelect", None)
    BasicScheduler = getattr(_nodes, "BasicScheduler", None)
    SamplerCustomAdvanced = getattr(_nodes, "SamplerCustomAdvanced", None)

    from comfy_script.runtime.nodes import (
        CLIPTextEncode, VAEDecode, SaveImage, EmptyLatentImage, LoraLoader,
    )
    from comfy_script.runtime import Workflow

    unet_name = params.get("_unet", "")
    clip1_name = params.get("_clip1", "clip_l.safetensors")
    clip2_name = params.get("_clip2", "t5xxl_fp8_e4m3fn.safetensors")
    vae_name = params.get("_vae", "ae.safetensors")

    if not unet_name:
        raise ValueError("params 缺少 _unet，请检查 theme yaml 的 unet 字段")

    # 节点缺失检查
    missing = [n for n, v in [
        ("UnetLoaderGGUF", UnetLoaderGGUF),
        ("DualCLIPLoader", DualCLIPLoader),
        ("SamplerCustomAdvanced", SamplerCustomAdvanced),
        ("BasicScheduler", BasicScheduler),
        ("KSamplerSelect", KSamplerSelect),
        ("RandomNoise", RandomNoise),
    ] if v is None]
    if missing:
        print(f"  [FLUX] 节点缺失: {missing}，降级到普通生图")
        return await comfy_generate(params)

    steps = int(params.get("steps", 20))
    cfg = float(params.get("cfg", 3.5))
    seed = int(params.get("seed", 42))
    sampler_name = params.get("sampler", "euler")
    # FLUX 支持的 sampler 别名映射
    _FLUX_SAMPLER_ALIAS = {
        "dpmpp_2m": "euler",
        "dpmpp_sde": "euler_ancestral",
        "dpmpp_2s_ancestral": "euler_ancestral",
        "ddim": "euler",
    }
    sampler_name = _FLUX_SAMPLER_ALIAS.get(sampler_name, sampler_name)

    print(f"  [FLUX] 生图模式  steps={steps}  cfg={cfg}  sampler={sampler_name}")

    with Workflow():
        # ── 模型加载 ──────────────────────────────────────────
        unet = UnetLoaderGGUF(unet_name)
        clip = DualCLIPLoader(clip1_name, clip2_name, "flux")
        vae = VAELoader(vae_name)

        # ── LoRA（FLUX 兼容 SD1.5 LoRA，但效果有差异）────────
        if params.get("lora"):
            unet, clip = LoraLoader(
                unet, clip,
                lora_name=params["lora"],
                strength_model=params.get("lora_strength", 1.0),
                strength_clip=params.get("lora_strength", 1.0),
            )
        if params.get("lora2"):
            unet, clip = LoraLoader(
                unet, clip,
                lora_name=params["lora2"],
                strength_model=params.get("lora2_strength", 0.8),
                strength_clip=params.get("lora2_strength", 0.8),
            )

        # ── Conditioning ──────────────────────────────────────
        pos = CLIPTextEncode(params["positive"], clip)
        neg = CLIPTextEncode(params["negative"], clip)

        # FluxGuidance：FLUX 的 CFG 注入方式
        if FluxGuidance:
            pos = FluxGuidance(pos, cfg)

        # ── 采样（SamplerCustomAdvanced）──────────────────────
        latent = EmptyLatentImage(1536, 1536, 1)
        noise = RandomNoise(seed)
        guider = BasicGuider(unet, pos)
        sampler = KSamplerSelect(sampler_name)
        sigmas = BasicScheduler(unet, "simple", steps, 1.0)

        output, denoised = SamplerCustomAdvanced(noise, guider, sampler, sigmas, latent)

        image = VAEDecode(output, vae)
        SaveImage(image, params["prefix"])


        SaveImage(image, params["prefix"])


# ════════════════════════════════════════════════════════════════
# v2.3.5：FLUX 统一多路径生图（Redux + PuLid + 两者组合）
# ════════════════════════════════════════════════════════════════
#
# 设计目标：相比 comfy_generate_flux，按 params 里的两个开关注入额外的
# Conditioning 段：
#
#   开关 1：_visual_ref_path 存在 → 注入 Redux 分支（锁场景/色调）
#     LoadImage(前镜图) → CLIPVisionEncode(sigclip) →
#     StyleModelApplyAdvanced(redux, pos, strength) → 新 pos
#
#   开关 2：_portrait_ref_path 存在 → 注入 PuLid 分支（锁角色脸）
#     LoadImage(定妆照) → PulidFluxInsightFaceLoader → ApplyPulidFlux(unet, ...)
#     → 新 unet
#
# 两个开关独立可组合。其他部分（VAE/LoRA/CFG/Sampler）完全照搬 comfy_generate_flux。
#
# 失败路径（任一）→ 自动降级 comfy_generate_flux：
#   - ComfyUI 缺关键节点（旧版本/未装插件）
#   - 参考图路径无效
#   - 上传失败
#   - 强度参数非法
#
# 兼容性：旧调用 comfy_generate_flux_redux 仍可用（作为别名转发到本函数）
# ════════════════════════════════════════════════════════════════

async def comfy_generate_flux_v260_multichar(params: dict):
    """v2.6 多角色多区域生图(Regional Prompter + 双 PuLID,空间 mask 版 v4)。

    跟 comfy_generate_flux_v234 同接口,但额外要求 params 中有:
      _v260_chars:        list[{character, en_name, path, strength, region, prompt}]
                          至少 2 个元素,每个含 portrait path
      _v260_left_mask:    str — 左 mask PNG 路径
      _v260_right_mask:   str — 右 mask PNG 路径
      _v260_mask_template: str — 模板名(仅日志用)

    依赖:
      - ComfyUI-ppm                 (AttentionCouplePPM 节点)
      - comfyui_pulid_flux_ll       (lldacing fork,attn_mask 真正工作)
        ★ 原版 balazik/ComfyUI-PuLID-Flux 的 attn_mask 已知不工作,本路径会单脸

    工作流(v4 核心):
      UnetLoaderGGUF → DualCLIP → VAE
                          ↓
      ApplyPulidFlux #1 (左角色, attn_mask=left_mask)   ← 空间隔离
                          ↓
      ApplyPulidFlux #2 (右角色, attn_mask=right_mask)  ← 空间隔离
                          ↓
      AttentionCouplePPM (左右 cond + mask 走 cross-attention 分区)
                          ↓
      KSampler → VAE Decode → SaveImage

    关键演进:
      v1: 双 PuLID 全程串行          → 融合脸
      v2: 时间步隔离                  → 单脸(PuLID 仍全图,后者覆盖前者)
      v3: PuLID + attn_mask          → 真正空间分区
      v4: ★ 所有 optional 参数全用 kwargs,避免 ComfyScript 把动态/optional 当位置参数丢弃

    任一节点缺失 / portrait 加载失败 → 降级到 comfy_generate_flux_v234(单 PuLID)。
    """
    print("=" * 60)
    print("  ★★★ V4 ATTN_MASK BUILD — IF YOU SEE THIS, V4 IS LOADED ★★★")
    print("=" * 60)
    print(f"  [renderer v260] params keys={[k for k in params if k.startswith('_v260')]}")

    import comfy_script.runtime.nodes as _nodes
    from pathlib import Path as _Path

    chars = params.get("_v260_chars") or []
    if len(chars) < 2:
        print(f"  [v260] _v260_chars 不足 2 个,降级到 v234")
        return await comfy_generate_flux_v234(params)

    # ── 节点存在性检查 ─────────────────────────────────
    UnetLoaderGGUF = getattr(_nodes, "UnetLoaderGGUF", None)
    DualCLIPLoader = getattr(_nodes, "DualCLIPLoader", None)
    VAELoader = getattr(_nodes, "VAELoader", None)
    FluxGuidance = getattr(_nodes, "FluxGuidance", None)
    RandomNoise = getattr(_nodes, "RandomNoise", None)
    BasicGuider = getattr(_nodes, "BasicGuider", None)
    KSamplerSelect = getattr(_nodes, "KSamplerSelect", None)
    BasicScheduler = getattr(_nodes, "BasicScheduler", None)
    SamplerCustomAdvanced = getattr(_nodes, "SamplerCustomAdvanced", None)
    LoadImage = getattr(_nodes, "LoadImage", None)
    ImageToMask = (getattr(_nodes, "ImageToMask", None)
                   or getattr(_nodes, "Image To Mask", None))
    SolidMask = getattr(_nodes, "SolidMask", None)
    PulidFluxModelLoader = (getattr(_nodes, "PulidFluxModelLoader", None)
                            or getattr(_nodes, "PulidModelLoader", None))
    PulidFluxInsightFaceLoader = (
        getattr(_nodes, "PulidFluxInsightFaceLoader", None)
        or getattr(_nodes, "PulidInsightFaceLoader", None))
    PulidFluxEvaClipLoader = (
        getattr(_nodes, "PulidFluxEvaClipLoader", None)
        or getattr(_nodes, "PulidEvaClipLoader", None))
    ApplyPulidFlux = (getattr(_nodes, "ApplyPulidFlux", None)
                      or getattr(_nodes, "ApplyPulid", None))

    AttentionCouple = (getattr(_nodes, "AttentionCouplePPM", None)
                       or getattr(_nodes, "AttentionCouple", None))

    basic_nodes = {
        "UnetLoaderGGUF": UnetLoaderGGUF,
        "DualCLIPLoader": DualCLIPLoader,
        "VAELoader": VAELoader,
        "ApplyPulidFlux": ApplyPulidFlux,
        "PulidFluxModelLoader": PulidFluxModelLoader,
        "AttentionCouple": AttentionCouple,
        "LoadImage": LoadImage,
        "ImageToMask": ImageToMask,
        "SolidMask": SolidMask,
    }
    missing = [n for n, v in basic_nodes.items() if v is None]
    if missing:
        print(f"  [v260] 关键节点缺失 {missing},降级到 v234")
        return await comfy_generate_flux_v234(params)

    # ── 上传 portrait + mask ──────────────────────
    char_servers = []
    for ch_info in chars:
        path = ch_info.get("path", "")
        if not path or not _Path(path).exists():
            print(f"  [v260] {ch_info.get('character','?')} portrait 不存在,降级 v234")
            return await comfy_generate_flux_v234(params)
        try:
            server_name = upload_image(path)
            char_servers.append({**ch_info, "_server": server_name})
        except Exception as e:
            print(f"  [v260] {ch_info.get('character','?')} portrait 上传失败({e}),降级")
            return await comfy_generate_flux_v234(params)

    left_mask_path = params.get("_v260_left_mask", "")
    right_mask_path = params.get("_v260_right_mask", "")
    if not (left_mask_path and right_mask_path
            and _Path(left_mask_path).exists()
            and _Path(right_mask_path).exists()):
        print(f"  [v260] mask 文件缺失,降级")
        return await comfy_generate_flux_v234(params)

    try:
        left_mask_server = upload_image(left_mask_path)
        right_mask_server = upload_image(right_mask_path)
    except Exception as e:
        print(f"  [v260] mask 上传失败({e}),降级")
        return await comfy_generate_flux_v234(params)

    # ── 模型常量 ───────────────────────────────────────
    try:
        from config import (PULID_MODEL_NAME, PULID_EVA_CLIP_NAME,
                             PULID_INSIGHTFACE_PROVIDER)
    except (ImportError, AttributeError):
        PULID_MODEL_NAME = "pulid_flux_v0.9.1.safetensors"
        PULID_EVA_CLIP_NAME = "EVA02_CLIP_L_336_psz14_s6B.pt"
        PULID_INSIGHTFACE_PROVIDER = "CPU"

    pulid_model_name = params.get("_pulid_model", PULID_MODEL_NAME)
    pulid_eva_name = params.get("_pulid_eva", PULID_EVA_CLIP_NAME)
    insight_provider = params.get("_pulid_insight_provider", PULID_INSIGHTFACE_PROVIDER)

    # v4: 空间 mask 隔离后两个 PuLID 互不干扰,strength 可以保留原值
    # (轻微 0.95 折扣只是给一点安全裕度)
    DUAL_PULID_STRENGTH_SCALE = 0.95

    template = params.get("_v260_mask_template", "left_right")
    char_summary = " + ".join(
        f"{c['character']}[{c['region']}]"
        f"(s={c['strength'] * DUAL_PULID_STRENGTH_SCALE:.2f})"
        for c in char_servers[:2]
    )
    print(f"  [v260 v4] 多角色路径 mask={template} | {char_summary} | attn_mask 模式")

    from comfy_script.runtime.nodes import (
        CLIPTextEncode, VAEDecode, SaveImage, EmptyLatentImage, LoraLoader,
    )
    from comfy_script.runtime import Workflow

    unet_name = params.get("_unet", "")
    clip1_name = params.get("_clip1", "clip_l.safetensors")
    clip2_name = params.get("_clip2", "t5xxl_fp8_e4m3fn.safetensors")
    vae_name = params.get("_vae", "ae.safetensors")
    if not unet_name:
        raise ValueError("params 缺少 _unet")

    steps = int(params.get("steps", 20))
    cfg = float(params.get("cfg", 3.5))
    seed = int(params.get("seed", 42))
    sampler_name = params.get("sampler", "euler")
    _FLUX_SAMPLER_ALIAS = {
        "dpmpp_2m": "euler", "dpmpp_sde": "euler_ancestral",
        "dpmpp_2s_ancestral": "euler_ancestral", "ddim": "euler",
    }
    sampler_name = _FLUX_SAMPLER_ALIAS.get(sampler_name, sampler_name)

    #global_prompt = params["positive"]
    global_neg = params["negative"]
    left_char = char_servers[0]
    right_char = char_servers[1]
    #left_region_prompt = (left_char.get("prompt") or "").strip()
    #right_region_prompt = (right_char.get("prompt") or "").strip()
    #global_prompt = (
    #        "two men standing side by side, both fully visible in frame, "
    #        "dual portrait composition, both faces clearly shown. "
    #        + params["positive"]
   # )
    #left_region_prompt = "Han Yanqing, narrow phoenix eyes, scar across left eyebrow, on the left side of the image"
    #right_region_prompt = "Shen Huaizhou, scholar, on the right side of the image"
    from core.v260_prompt_builder import build_v260_prompts

    global_prompt, left_region_prompt, right_region_prompt = build_v260_prompts(
        params, char_servers, template,  # template 就是 _v260_mask_template
    )
    if not left_region_prompt:
        left_region_prompt = (left_char.get("en_name")
                              or left_char["character"])
    if not right_region_prompt:
        right_region_prompt = (right_char.get("en_name")
                               or right_char["character"])

    with Workflow():
        unet = UnetLoaderGGUF(unet_name)
        clip = DualCLIPLoader(clip1_name, clip2_name, "flux")
        vae = VAELoader(vae_name)

        if params.get("lora"):
            unet, clip = LoraLoader(
                unet, clip,
                lora_name=params["lora"],
                strength_model=params.get("lora_strength", 1.0),
                strength_clip=params.get("lora_strength", 1.0),
            )
        if params.get("lora2"):
            unet, clip = LoraLoader(
                unet, clip,
                lora_name=params["lora2"],
                strength_model=params.get("lora2_strength", 0.8),
                strength_clip=params.get("lora2_strength", 0.8),
            )

        # ── 先加载 mask(PuLID 和 AttentionCouple 都要用) ──
        left_mask_img, _lm = LoadImage(left_mask_server)
        right_mask_img, _rm = LoadImage(right_mask_server)
        try:
            left_mask = ImageToMask(left_mask_img, "red")
        except TypeError:
            left_mask = ImageToMask(left_mask_img)
        try:
            right_mask = ImageToMask(right_mask_img, "red")
        except TypeError:
            right_mask = ImageToMask(right_mask_img)

        # ── ★ 双 PuLID 空间 mask 注入(v4 核心) ───────────
        # comfyui_pulid_flux_ll 的 ApplyPulidFlux 实际签名(确认过 pulidflux.py:241):
        #   apply_pulid_flux(model, pulid_flux, eva_clip, face_analysis, image,
        #                    weight, start_at, end_at,
        #                    attn_mask=None, options={}, unique_id=None)
        # 关键点:
        #   - attn_mask 是 optional,必须 kwargs 传
        #   - weight/start_at/end_at 虽是 required,这里也用 kwargs 一致化,
        #     避免 ComfyScript 在 optional 节点上的位置参数行为不一致
        pulid_model = PulidFluxModelLoader(pulid_model_name)
        eva_clip = PulidFluxEvaClipLoader()
        try:
            insight = PulidFluxInsightFaceLoader(insight_provider)
        except TypeError:
            insight = PulidFluxInsightFaceLoader()

        char_masks = [left_mask, right_mask]
        attn_mask_supported = True

        for idx, ch in enumerate(char_servers[:2]):
            portrait_img, _m = LoadImage(ch["_server"])
            scaled_strength = ch["strength"] * DUAL_PULID_STRENGTH_SCALE
            ch_mask = char_masks[idx]

            if attn_mask_supported:
                try:
                    unet = ApplyPulidFlux(
                        unet, pulid_model, eva_clip, insight,
                        portrait_img,
                        weight=scaled_strength,
                        start_at=0.0,
                        end_at=1.0,
                        attn_mask=ch_mask,
                    )
                    print(f"  [v260 v4] {ch['character']}[{ch.get('region','?')}] "
                          f"PuLID 注入 attn_mask(s={scaled_strength:.2f})")
                    continue
                except TypeError as e:
                    print(f"  [v260 v4] ApplyPulidFlux attn_mask 失败 ({e}),"
                          f"回退时间步隔离")
                    attn_mask_supported = False

            # Fallback:attn_mask 不支持时回退到时间步隔离 + 降权
            start_at = 0.0 if idx == 0 else 0.5
            end_at = 0.5 if idx == 0 else 1.0
            try:
                unet = ApplyPulidFlux(
                    unet, pulid_model, eva_clip, insight,
                    portrait_img,
                    weight=ch["strength"] * 0.65,
                    start_at=start_at,
                    end_at=end_at,
                )
            except TypeError:
                # 最后兜底:连 start_at/end_at kwargs 都不行,纯位置参数
                unet = ApplyPulidFlux(
                    unet, pulid_model, eva_clip, insight,
                    portrait_img, ch["strength"] * 0.65,
                )

        # ── Conditioning ────────────────────────────────
        base_pos = CLIPTextEncode(global_prompt, clip)
        cond_left = CLIPTextEncode(left_region_prompt, clip)
        cond_right = CLIPTextEncode(right_region_prompt, clip)

        if FluxGuidance:
            base_pos = FluxGuidance(base_pos, cfg)
            cond_left = FluxGuidance(cond_left, cfg)
            cond_right = FluxGuidance(cond_right, cfg)

        render_size = params.get("_v260_render_size", 1024)
        base_mask = SolidMask(1.0, render_size, render_size)

        # ── AttentionCouple 全 kwargs(v2 已验证形式 A 成功) ──
        try:
            unet = AttentionCouple(
                model=unet,
                base_cond=base_pos,
                base_mask=base_mask,
                cond_1=cond_left, mask_1=left_mask,
                cond_2=cond_right, mask_2=right_mask,
            )
        except Exception as e:
            print(f"  [v260 v4] AttentionCouple 失败 ({e}),降级到 v234")
            return await comfy_generate_flux_v234(params)

        # 采样链
        latent = EmptyLatentImage(render_size, render_size, 1)
        noise = RandomNoise(seed)
        guider = BasicGuider(unet, base_pos)
        sampler = KSamplerSelect(sampler_name)
        sigmas = BasicScheduler(unet, "simple", steps, 1.0)
        output, denoised = SamplerCustomAdvanced(
            noise, guider, sampler, sigmas, latent)

        image = VAEDecode(output, vae)
        SaveImage(image, params["prefix"])


async def comfy_generate_flux_v234(params: dict):
    """
    FLUX 统一多路径生图（v2.3.5）。支持 Redux + PuLid 任意组合。

    params 额外可选字段：
      _visual_ref_path:        mac 本地 PNG 路径（Redux 参考，前一镜图）
      _visual_ref_strength:    0.0-1.0
      _portrait_ref_path:      mac 本地 PNG 路径（PuLid 参考，角色定妆照）
      _portrait_ref_strength:  0.0-1.0
      _portrait_character:     角色名（仅日志用）

    任一前置条件不满足，自动降级到普通 FLUX 路径（零回归）。
    """
    import comfy_script.runtime.nodes as _nodes
    from pathlib import Path as _Path

    # ── 决定要走哪些分支 ─────────────────────────────────
    want_redux = bool(params.get("_visual_ref_path"))
    want_pulid = bool(params.get("_portrait_ref_path"))

    if not want_redux and not want_pulid:
        # 都不要 → 直接走普通 FLUX，避免后续节点检查/上传开销
        return await comfy_generate_flux(params)

    # ── 节点存在性检查（按需检查）──────────────────────
    # 基础 FLUX 节点
    UnetLoaderGGUF = getattr(_nodes, "UnetLoaderGGUF", None)
    DualCLIPLoader = getattr(_nodes, "DualCLIPLoader", None)
    VAELoader = getattr(_nodes, "VAELoader", None)
    FluxGuidance = getattr(_nodes, "FluxGuidance", None)
    RandomNoise = getattr(_nodes, "RandomNoise", None)
    BasicGuider = getattr(_nodes, "BasicGuider", None)
    KSamplerSelect = getattr(_nodes, "KSamplerSelect", None)
    BasicScheduler = getattr(_nodes, "BasicScheduler", None)
    SamplerCustomAdvanced = getattr(_nodes, "SamplerCustomAdvanced", None)

    flux_basic = {
        "UnetLoaderGGUF": UnetLoaderGGUF,
        "DualCLIPLoader": DualCLIPLoader,
        "SamplerCustomAdvanced": SamplerCustomAdvanced,
        "BasicScheduler": BasicScheduler,
        "KSamplerSelect": KSamplerSelect,
        "RandomNoise": RandomNoise,
    }
    missing_flux = [n for n, v in flux_basic.items() if v is None]
    if missing_flux:
        print(f"  [v234] FLUX 基础节点缺失 {missing_flux}，降级到普通生图")
        return await comfy_generate(params)

    # Redux 节点（仅 want_redux 时检查）
    if want_redux:
        StyleModelLoader = getattr(_nodes, "StyleModelLoader", None)
        CLIPVisionLoader = getattr(_nodes, "CLIPVisionLoader", None)
        CLIPVisionEncode = getattr(_nodes, "CLIPVisionEncode", None)
        LoadImage_R = getattr(_nodes, "LoadImage", None)
        StyleModelApply = (getattr(_nodes, "StyleModelApplyAdvanced", None)
                           or getattr(_nodes, "StyleModelApply", None))
        redux_nodes = {
            "StyleModelLoader": StyleModelLoader,
            "CLIPVisionLoader": CLIPVisionLoader,
            "CLIPVisionEncode": CLIPVisionEncode,
            "LoadImage": LoadImage_R,
            "StyleModelApply(Advanced)": StyleModelApply,
        }
        missing_redux = [n for n, v in redux_nodes.items() if v is None]
        if missing_redux:
            print(f"  [Redux] 节点缺失 {missing_redux}，关闭 Redux 分支")
            want_redux = False

    # PuLid 节点（仅 want_pulid 时检查）
    # PuLid for FLUX 是社区节点，节点名按主流插件命名（ComfyUI-PuLID-Flux-Enhanced）
    if want_pulid:
        PulidFluxModelLoader = (getattr(_nodes, "PulidFluxModelLoader", None)
                                or getattr(_nodes, "PulidModelLoader", None))
        PulidFluxInsightFaceLoader = (
            getattr(_nodes, "PulidFluxInsightFaceLoader", None)
            or getattr(_nodes, "PulidInsightFaceLoader", None))
        PulidFluxEvaClipLoader = (
            getattr(_nodes, "PulidFluxEvaClipLoader", None)
            or getattr(_nodes, "PulidEvaClipLoader", None))
        ApplyPulidFlux = (getattr(_nodes, "ApplyPulidFlux", None)
                          or getattr(_nodes, "ApplyPulid", None))
        LoadImage_P = getattr(_nodes, "LoadImage", None)
        pulid_nodes = {
            "PulidFluxModelLoader": PulidFluxModelLoader,
            "PulidFluxInsightFaceLoader": PulidFluxInsightFaceLoader,
            "PulidFluxEvaClipLoader": PulidFluxEvaClipLoader,
            "ApplyPulidFlux": ApplyPulidFlux,
            "LoadImage": LoadImage_P,
        }
        missing_pulid = [n for n, v in pulid_nodes.items() if v is None]
        if missing_pulid:
            print(f"  [PuLid] 节点缺失 {missing_pulid}，关闭 PuLid 分支")
            want_pulid = False

    # 两个分支都关掉 → 直接降级
    if not want_redux and not want_pulid:
        print(f"  [v234] 所有增强分支都不可用，降级到普通 FLUX")
        return await comfy_generate_flux(params)

    # ── 参考图上传 ──────────────────────────────────────
    visual_ref_server = None
    if want_redux:
        ref_path = params.get("_visual_ref_path", "")
        if not _Path(ref_path).exists():
            print(f"  [Redux] 参考图无效 '{ref_path}'，关闭 Redux 分支")
            want_redux = False
        else:
            try:
                visual_ref_server = upload_image(ref_path)
            except Exception as e:
                print(f"  [Redux] 参考图上传失败 ({e})，关闭 Redux 分支")
                want_redux = False

    portrait_ref_server = None
    if want_pulid:
        portrait_path = params.get("_portrait_ref_path", "")
        if not _Path(portrait_path).exists():
            print(f"  [PuLid] 定妆照无效 '{portrait_path}'，关闭 PuLid 分支")
            want_pulid = False
        else:
            try:
                portrait_ref_server = upload_image(portrait_path)
            except Exception as e:
                print(f"  [PuLid] 定妆照上传失败 ({e})，关闭 PuLid 分支")
                want_pulid = False

    # 上传都失败 → 降级
    if not want_redux and not want_pulid:
        return await comfy_generate_flux(params)

    # ── 强度校验 ────────────────────────────────────────
    redux_strength = 0.5
    if want_redux:
        try:
            redux_strength = float(params.get("_visual_ref_strength", 0.5))
        except (TypeError, ValueError):
            redux_strength = 0.5
        redux_strength = max(0.0, min(1.0, redux_strength))

    pulid_strength = 0.85
    if want_pulid:
        try:
            pulid_strength = float(params.get("_portrait_ref_strength", 0.85))
        except (TypeError, ValueError):
            pulid_strength = 0.85
        pulid_strength = max(0.0, min(1.0, pulid_strength))

    # ── 取模型常量 ──────────────────────────────────────
    try:
        from config import (REDUX_MODEL_NAME, REDUX_VISION_MODEL,
                             PULID_MODEL_NAME, PULID_EVA_CLIP_NAME,
                             PULID_INSIGHTFACE_PROVIDER)
    except (ImportError, AttributeError):
        REDUX_MODEL_NAME = "flux1-redux-dev.safetensors"
        REDUX_VISION_MODEL = "sigclip_vision_patch14_384.safetensors"
        PULID_MODEL_NAME = "pulid_flux_v0.9.1.safetensors"
        PULID_EVA_CLIP_NAME = "EVA02_CLIP_L_336_psz14_s6B.pt"
        PULID_INSIGHTFACE_PROVIDER = "CPU"

    redux_model_name = params.get("_redux_model", REDUX_MODEL_NAME)
    redux_vision_name = params.get("_redux_vision", REDUX_VISION_MODEL)
    pulid_model_name = params.get("_pulid_model", PULID_MODEL_NAME)
    pulid_eva_name = params.get("_pulid_eva", PULID_EVA_CLIP_NAME)
    insight_provider = params.get("_pulid_insight_provider", PULID_INSIGHTFACE_PROVIDER)

    # ── 状态日志 ────────────────────────────────────────
    path_label = []
    if want_redux:
        path_label.append(f"Redux(s={redux_strength:.2f})")
    if want_pulid:
        char_name = params.get("_portrait_character", "?")
        path_label.append(f"PuLid[{char_name}](s={pulid_strength:.2f})")
    print(f"  [v234] 路径: {' + '.join(path_label)}")

    # ── 加载 FLUX 工作流所需的节点（跟 comfy_generate_flux 一致）──
    from comfy_script.runtime.nodes import (
        CLIPTextEncode, VAEDecode, SaveImage, EmptyLatentImage, LoraLoader,
    )
    from comfy_script.runtime import Workflow

    unet_name = params.get("_unet", "")
    clip1_name = params.get("_clip1", "clip_l.safetensors")
    clip2_name = params.get("_clip2", "t5xxl_fp8_e4m3fn.safetensors")
    vae_name = params.get("_vae", "ae.safetensors")
    if not unet_name:
        raise ValueError("params 缺少 _unet，请检查 theme yaml 的 unet 字段")

    steps = int(params.get("steps", 20))
    cfg = float(params.get("cfg", 3.5))
    seed = int(params.get("seed", 42))
    sampler_name = params.get("sampler", "euler")
    _FLUX_SAMPLER_ALIAS = {
        "dpmpp_2m": "euler",
        "dpmpp_sde": "euler_ancestral",
        "dpmpp_2s_ancestral": "euler_ancestral",
        "ddim": "euler",
    }
    sampler_name = _FLUX_SAMPLER_ALIAS.get(sampler_name, sampler_name)

    # ── 构建工作流 ──────────────────────────────────────
    with Workflow():
        # FLUX 基础
        unet = UnetLoaderGGUF(unet_name)
        clip = DualCLIPLoader(clip1_name, clip2_name, "flux")
        vae = VAELoader(vae_name)

        # LoRA 链（照搬）
        if params.get("lora"):
            unet, clip = LoraLoader(
                unet, clip,
                lora_name=params["lora"],
                strength_model=params.get("lora_strength", 1.0),
                strength_clip=params.get("lora_strength", 1.0),
            )
        if params.get("lora2"):
            unet, clip = LoraLoader(
                unet, clip,
                lora_name=params["lora2"],
                strength_model=params.get("lora2_strength", 0.8),
                strength_clip=params.get("lora2_strength", 0.8),
            )

        # ── PuLid 注入（在 LoRA 之后、Conditioning 之前）─────
        # PuLid 修改的是 unet（不是 pos_cond），所以必须在 BasicGuider 之前接好
        if want_pulid:
            portrait_image, _mask_p = LoadImage_P(portrait_ref_server)
            pulid_model = PulidFluxModelLoader(pulid_model_name)
            eva_clip = PulidFluxEvaClipLoader()
            # InsightFace loader：不同插件签名略有差异，常见是只接 provider
            try:
                insight = PulidFluxInsightFaceLoader(insight_provider)
            except TypeError:
                # 老版本可能无参
                insight = PulidFluxInsightFaceLoader()
            # ApplyPulidFlux 签名（按 ComfyUI-PuLID-Flux-Enhanced 主流签名）：
            #   (model, pulid_flux, eva_clip, face_analysis, image, weight,
            #    start_at=0.0, end_at=1.0, ...)
            try:
                unet = ApplyPulidFlux(
                    unet, pulid_model, eva_clip, insight,
                    portrait_image, pulid_strength,
                )
            except TypeError as e:
                # 备用签名（不同插件版本）
                print(f"  [PuLid] ApplyPulidFlux 签名不匹配，尝试备用 ({e})")
                unet = ApplyPulidFlux(
                    unet, pulid_model, eva_clip, insight,
                    portrait_image, pulid_strength, 0.0, 1.0,
                )

        # ── Conditioning ────────────────────────────────────
        pos = CLIPTextEncode(params["positive"], clip)
        neg = CLIPTextEncode(params["negative"], clip)
        if FluxGuidance:
            pos = FluxGuidance(pos, cfg)

        # ── Redux 注入（在 pos 上加 style）────────────────────
        if want_redux:
            ref_image, _mask_r = LoadImage_R(visual_ref_server)
            clip_vision = CLIPVisionLoader(redux_vision_name)
            vision_out = CLIPVisionEncode(clip_vision, ref_image)
            style_model = StyleModelLoader(redux_model_name)
            try:
                pos = StyleModelApply(pos, style_model, vision_out, redux_strength)
            except TypeError:
                print(f"  [Redux] StyleModelApply 不支持 strength，使用默认全强度")
                pos = StyleModelApply(pos, style_model, vision_out)

        # 采样链
        latent = EmptyLatentImage(1536, 1536, 1)
        noise = RandomNoise(seed)
        guider = BasicGuider(unet, pos)
        sampler = KSamplerSelect(sampler_name)
        sigmas = BasicScheduler(unet, "simple", steps, 1.0)
        output, denoised = SamplerCustomAdvanced(
            noise, guider, sampler, sigmas, latent)

        image = VAEDecode(output, vae)
        SaveImage(image, params["prefix"])


# v2.3.4 旧函数名兼容（转发到统一函数，pipeline.py 旧引用照常工作）
async def comfy_generate_flux_redux(params: dict):
    """v2.3.4 兼容入口：转发到 v2.3.5 统一函数"""
    return await comfy_generate_flux_v234(params)


# ════════════════════════════════════════
# Phase 1：ReActor 人脸替换（后处理，立即可用）
# ════════════════════════════════════════

async def comfy_reactor_swap(params: dict):
    """
    ReActor 人脸替换后处理。
    _source_path / face_ref 是本地路径，经 upload_image() 上传后使用，
    无需路径分隔符修正（已在 generate_and_wait 入口跳过这两个字段）。
    """
    import comfy_script.runtime.nodes as _nodes
    ReActorFaceSwap = getattr(_nodes, "ReActorFaceSwap", None)
    if ReActorFaceSwap is None:
        raise RuntimeError(
            "ReActor 节点未安装。\n"
            "安装: cd ComfyUI/custom_nodes && "
            "git clone https://github.com/Gourieff/comfyui-reactor-node"
        )

    from comfy_script.runtime.nodes import LoadImage, SaveImage
    from comfy_script.runtime import Workflow

    source_path = params["_source_path"]
    face_ref    = params["face_ref"]
    prefix      = params.get("prefix", "reactor") + "_fx"

    print(f"  [ReActor] 换脸: {Path(source_path).name}")

    src_server  = upload_image(source_path)
    face_server = get_or_upload_ref(face_ref)

    with Workflow():
        src_img,  _ = LoadImage(src_server)
        face_img, _ = LoadImage(face_server)

        swapped = ReActorFaceSwap(
            input_image             = src_img,
            source_image            = face_img,
            enabled                 = True,
            swap_model              = "inswapper_128.onnx",
            facedetection           = "retinaface_resnet50",
            face_restore_model      = "CodeFormer",
            face_restore_visibility = 1.0,
            codeformer_weight       = 0.5,
            detect_gender_input     = "no",
            detect_gender_source    = "no",
            input_faces_index       = "0",
            source_faces_index      = "0",
            console_log_level       = 1,
        )
        SaveImage(swapped, prefix)


# ════════════════════════════════════════
# Phase 2：FaceID 生图
# ════════════════════════════════════════

async def comfy_faceid_generate(params: dict):
    """
    FaceID Plus v2 增强生图。路径修正已在 generate_and_wait 入口完成。
    """
    import comfy_script.runtime.nodes as _nodes
    IPAdapterFaceID   = getattr(_nodes, "IPAdapterFaceID",           None)
    InsightFaceLoader = getattr(_nodes, "IPAdapterInsightFaceLoader", None)
    IPAdapterModelLoader = getattr(_nodes, "IPAdapterModelLoader",   None)
    CLIPVisionLoader  = getattr(_nodes, "CLIPVisionLoader",           None)
    IPAdapterAdvanced = getattr(_nodes, "IPAdapterAdvanced",          None)

    if IPAdapterFaceID is None or InsightFaceLoader is None:
        print("  [FaceID] 节点未找到，降级到普通 IP-Adapter 生图")
        return await comfy_generate(params)

    faceid_model  = params.get("_faceid_model", "")
    faceid_vision = params.get("_faceid_vision", "")
    face_ref      = params.get("face_ref")

    if not face_ref or not faceid_model:
        print("  [FaceID] 缺少 face_ref 或 _faceid_model，降级到普通生图")
        return await comfy_generate(params)

    from comfy_script.runtime.nodes import (
        CheckpointLoaderSimple, CLIPSetLastLayer, LoraLoader,
        CLIPTextEncode, KSampler, VAEDecode, SaveImage,
        LoadImage, EmptyLatentImage,
    )
    from comfy_script.runtime import Workflow

    checkpoint  = params.get("_checkpoint", "")
    ipadapter   = params.get("_ipadapter", "")
    clip_vision = params.get("_clip_vision", "")
    ip_ref      = params.get("ip_ref")

    print(f"  [FaceID] FaceID Plus v2 双流生图模式")

    with Workflow():
        model, clip, vae = CheckpointLoaderSimple(checkpoint)
        clip = CLIPSetLastLayer(clip, stop_at_clip_layer=-2)

        if params.get("lora"):
            model, clip = LoraLoader(
                model, clip,
                lora_name      = params["lora"],
                strength_model = params["lora_strength"],
                strength_clip  = params["lora_strength"])

        if params.get("lora2"):
            model, clip = LoraLoader(
                model, clip,
                lora_name      = params["lora2"],
                strength_model = params["lora2_strength"],
                strength_clip  = params["lora2_strength"])

        pos = CLIPTextEncode(params["positive"], clip)
        neg = CLIPTextEncode(params["negative"], clip)

        if ip_ref and ipadapter and clip_vision and IPAdapterAdvanced:
            ref_server = get_or_upload_ref(ip_ref)
            ref_img, _ = LoadImage(ref_server)
            ip_model   = IPAdapterModelLoader(ipadapter)
            clip_vis   = CLIPVisionLoader(clip_vision)
            model      = IPAdapterAdvanced(
                model, ip_model, clip_vis, ref_img, pos,
                weight=0.5, weight_type="linear",
                combine_embeds="concat",
                start_at=0.0, end_at=0.9,
                embeds_scaling="V only",
            )

        face_server   = get_or_upload_ref(face_ref)
        face_img, _   = LoadImage(face_server)
        insight_face  = InsightFaceLoader("CPU")
        faceid_model_ = IPAdapterModelLoader(faceid_model)
        faceid_vis_   = CLIPVisionLoader(faceid_vision) if faceid_vision else None

        model = IPAdapterFaceID(
            model           = model,
            ipadapter       = faceid_model_,
            image           = face_img,
            insightface     = insight_face,
            weight          = 0.85,
            weight_faceidv2 = 1.0,
            weight_type     = "linear",
            combine_embeds  = "concat",
            start_at        = 0.0,
            end_at          = 1.0,
            clip_vision     = faceid_vis_,
        )

        latent       = EmptyLatentImage(1536, 1536, 1)
        sampler_name = params.get("sampler", "dpmpp_2m")
        scheduler    = params.get("scheduler", "karras")
        sampled      = KSampler(
            model, params["seed"], params["steps"], params["cfg"],
            sampler_name, scheduler, pos, neg, latent, 1.0,
        )
        image = VAEDecode(sampled, vae)
        SaveImage(image, params["prefix"])


async def comfy_upscale(params: dict):
    """
    高清放大。路径修正已在 generate_and_wait 入口完成。
    """
    from comfy_script.runtime.nodes import (
        CheckpointLoaderSimple, CLIPSetLastLayer, LoraLoader,
        CLIPTextEncode, LoadImage, ControlNetLoader,
        ControlNetApplyAdvanced, UpscaleModelLoader,
        ImageUpscaleWithModel, ImageScale,
        VAEEncode, KSampler, VAEDecode, SaveImage,
    )
    from comfy_script.runtime import Workflow

    checkpoint = params.get("_checkpoint", "")
    if not checkpoint:
        raise ValueError("params 缺少 _checkpoint")

    src    = params["_source_path"]
    prefix = params["prefix"] + "_hd"
    print(f"  高清放大: 768 → 1536")

    with Workflow():
        model, clip, vae = CheckpointLoaderSimple(checkpoint)
        clip = CLIPSetLastLayer(clip, stop_at_clip_layer=-2)

        if params.get("lora"):
            model, clip = LoraLoader(
                model, clip,
                lora_name      = params["lora"],
                strength_model = params["lora_strength"],
                strength_clip  = params["lora_strength"])

        if params.get("lora2"):
            model, clip = LoraLoader(
                model, clip,
                lora_name      = params["lora2"],
                strength_model = params["lora2_strength"],
                strength_clip  = params["lora2_strength"])

        srv_img      = upload_image(src)
        src_image, _ = LoadImage(srv_img)

        upscale_model = UpscaleModelLoader("4x-AnimeSharp.pth")
        upscaled_img  = ImageUpscaleWithModel(upscale_model, src_image)
        upscaled_img  = ImageScale(upscaled_img, "lanczos", 1536, 1536, "disabled")

        controlnet       = ControlNetLoader("controlnet_tile_sdxl.safetensors")
        pos              = CLIPTextEncode(params["positive"], clip)
        neg              = CLIPTextEncode(params["negative"], clip)
        pos_ctrl, neg_ctrl = ControlNetApplyAdvanced(
            positive=pos, negative=neg, control_net=controlnet,
            image=upscaled_img, strength=0.5,
            start_percent=0.0, end_percent=0.8, vae=vae,
        )
        latent  = VAEEncode(upscaled_img, vae)
        sampled = KSampler(
            model, params["seed"], 20, 6.5,
            "dpmpp_2m", "karras",
            pos_ctrl, neg_ctrl, latent,
            params.get("upscale_denoise", 0.40),
        )
        image = VAEDecode(sampled, vae)
        SaveImage(image, prefix)


# ── 诊断工具 ───────────────────────────────────────────

def check_models(theme=None):
    """检查 ComfyUI 上的模型。theme 传入时检查主题对应的模型。"""
    print(f"\n{'='*55}\n  模型检查\n{'='*55}")
    try:
        r = requests.get(f"{COMFY_SERVER}/object_info",
                         timeout=10, proxies=PROXIES).json()
    except Exception as e:
        print(f"  无法连接 ComfyUI: {e}")
        return

    checkpoint = theme.model.checkpoint if theme else "（未指定主题）"
    ipadapter  = theme.model.ipadapter  if theme else "（未指定主题）"

    try:
        ckpts    = (r.get("CheckpointLoaderSimple", {})
                    .get("input", {}).get("required", {})
                    .get("ckpt_name", [{}])[0] or [])
        has_ckpt = checkpoint in ckpts
        print(f"  Checkpoint:       {'OK' if has_ckpt else 'MISSING'} ({checkpoint})")
    except Exception:
        print(f"  Checkpoint: 检查失败")

    has_ip_node = "IPAdapterAdvanced" in r or "IPAdapterModelLoader" in r
    print(f"  IPAdapter节点:    {'OK' if has_ip_node else 'MISSING'}")

    has_reactor = "ReActorFaceSwap" in r
    print(f"  ReActor节点:      {'OK' if has_reactor else 'MISSING（人脸替换不可用）'}")

    has_faceid = "IPAdapterFaceID" in r
    print(f"  FaceID节点:       {'OK' if has_faceid else 'MISSING（降级到IP-Adapter）'}")

    try:
        cn_models = (r.get("ControlNetLoader", {})
                     .get("input", {}).get("required", {})
                     .get("control_net_name", [{}])[0] or [])
        has_tile  = "controlnet_tile_sdxl.safetensors" in cn_models
        print(f"  ControlNet Tile:  {'OK' if has_tile else 'MISSING'}")
    except Exception:
        print(f"  ControlNet: 检查失败")

    try:
        up_models = (r.get("UpscaleModelLoader", {})
                     .get("input", {}).get("required", {})
                     .get("model_name", [{}])[0] or [])
        has_up    = "4x-AnimeSharp.pth" in up_models
        print(f"  4x-AnimeSharp:    {'OK' if has_up else 'MISSING'}")
    except Exception:
        print(f"  Upscaler: 检查失败")

    print(f"\n{'='*55}")
