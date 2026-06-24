"""
pipeline_v260_router.py ── v2.6 多角色路由助手 (render_characters 单字段版)
====================================================================
设计原则 (本次重写的核心):
  ★ 画面角色只认一个字段: render_characters (有序列表)
  ★ 路径选择只看数量: 1→单人(v235), ≥2→多人(v260), 0→纯景物
  ★ 不再有 _render_chars / _region_prompts / characters 多路 fallback
  ★ LoRA 触发名单是独立概念, 不复用本字段 (分镜侧自己算)

render_characters 期望格式 (storyboard 每个镜头):
  render_characters:
    - name: 韩砚清                                          # 必填, 中文名(查 portrait)
      en: "Han Yanqing, narrow phoenix eyes, scar"          # 强烈建议(FLUX 不认中文)
      region: left                                          # 可选, left/right/center, 缺省按顺序
    - name: 沈淮舟
      en: "Shen Huaizhou, young scholar in hanfu"
      region: right

兼容: render_characters 缺失时, 回退读老的 characters(list[str]) 兜底,
      但只在过渡期保留, 新分镜应统一产出 render_characters。

mask 尺寸固定 1024 (与 renderer 的 latent / SolidMask 三处必须同值)。
"""

from pathlib import Path


# ════════════════════════════════════════════════════════════════
# 常量
# ════════════════════════════════════════════════════════════════

# 远景细节不可见, 多角色 regional 收益低
_V260_SKIP_SHOT_TYPES = {"wide", "extreme_wide"}

# mask / latent 统一分辨率 (renderer 三处必须同值: 此处 + SolidMask + EmptyLatentImage)
_V260_RENDER_SIZE = 1024

# 默认区域分配顺序 (render_characters 没写 region 时按序取)
_DEFAULT_REGIONS = ["left", "right", "center", "background"]


# ════════════════════════════════════════════════════════════════
# Feature flag
# ════════════════════════════════════════════════════════════════

def is_v260_enabled() -> bool:
    """读 config.ENABLE_V260_REGIONAL; 未定义则关。"""
    try:
        from config import ENABLE_V260_REGIONAL
        return bool(ENABLE_V260_REGIONAL)
    except (ImportError, AttributeError):
        return False


# ════════════════════════════════════════════════════════════════
# 读取 render_characters (唯一真相字段)
# ════════════════════════════════════════════════════════════════

def get_render_characters(page_cfg: dict) -> list:
    """从 page_cfg 读 render_characters, 归一化成 list[dict]。

    返回 [{name, en, region}, ...], 顺序保持原样 (index 0 = 左)。
    每个元素保证有 name; en/region 可能为空字符串。

    兼容: 若没有 render_characters, 回退读老的 characters(list[str]),
          把每个字符串包成 {name, en:"", region:""}。
    """
    rc = page_cfg.get("render_characters")

    # 老格式兜底: characters 是 list[str]
    if rc is None:
        legacy = page_cfg.get("characters") or []
        if isinstance(legacy, list):
            rc = [{"name": c} for c in legacy if isinstance(c, str)]
        else:
            rc = []

    if not isinstance(rc, list):
        return []

    out = []
    for idx, item in enumerate(rc):
        if isinstance(item, dict):
            name = (item.get("name") or "").strip()
            if not name:
                continue
            out.append({
                "name": name,
                "en": (item.get("en") or "").strip(),
                "region": (item.get("region") or "").strip(),
            })
        elif isinstance(item, str):
            # render_characters 里混了纯字符串
            name = item.strip()
            if name:
                out.append({"name": name, "en": "", "region": ""})
    return out


# ════════════════════════════════════════════════════════════════
# 决策: 本页是否走多人 (v2.6) 路径
# ════════════════════════════════════════════════════════════════

def should_use_regional(page_cfg: dict, story=None) -> bool:
    """判断本页是否应该走 v2.6 多角色路径。

    条件 (全满足):
      1. flag 开
      2. render_characters >= 2 个
      3. shot_type 不是远景
      4. 这些角色里, 至少前 2 个都有有效 portrait_ref
         (缺则降级到单人路径, 由调用方走 v235)
    """
    if not is_v260_enabled():
        return False

    chars = get_render_characters(page_cfg)
    if len(chars) < 2:
        return False

    # shot_type 远景跳过
    sb = page_cfg.get("_prebuilt_storyboard") or {}
    shot_type = (sb.get("shot_type") or "").lower()
    if shot_type in _V260_SKIP_SHOT_TYPES:
        print(f"  [v260] p{page_cfg.get('page','?')} 远景({shot_type}),降级单人")
        return False

    # portrait 覆盖检查: 前 2 个角色必须都有 portrait
    portrait_refs = page_cfg.get("_portrait_refs") or []
    valid_names = {
        r.get("character") for r in portrait_refs
        if isinstance(r, dict) and r.get("path")
    }
    for ch in chars[:2]:
        if ch["name"] not in valid_names:
            print(f"  [v260] p{page_cfg.get('page','?')} 角色 {ch['name']} "
                  f"缺 portrait,降级单人")
            return False

    return True


# ════════════════════════════════════════════════════════════════
# 解析多角色 portrait 路径 + 强度 + region + en
# ════════════════════════════════════════════════════════════════

def resolve_multi_portrait_refs(page_cfg: dict, story=None,
                                  project_root: str = None) -> list:
    """解析 render_characters 里每个角色的完整渲染信息。

    返回 [{character, en_name, path, strength, region, prompt}, ...]
    顺序 = render_characters 顺序 (index 0 → 左)。
    只保留有有效 portrait 的角色。

    字段说明:
      character: 中文名
      en_name:   英文名/英文视觉描述 (来自 render_characters[].en)
      path:      portrait 绝对路径
      strength:  PuLID 强度 (按 shot_type, 借 v235 的表)
      region:    left/right/center (render_characters[].region 或按序)
      prompt:    region 描述 (= en_name, 给 renderer 的 build_v260_prompts 用)
    """
    chars = get_render_characters(page_cfg)
    portrait_refs = page_cfg.get("_portrait_refs") or []

    # name -> portrait_ref dict
    p_lookup = {
        r.get("character"): r for r in portrait_refs
        if isinstance(r, dict)
    }

    if not project_root:
        project_root = "."

    # 借 v235 的强度表
    try:
        from core.pipeline_v235_router import (
            PULID_STRENGTH_BY_SHOT_TYPE,
            PULID_INTENSE_EMOTION_DECAY,
            PULID_INTENSE_KEYWORDS,
        )
    except ImportError:
        PULID_STRENGTH_BY_SHOT_TYPE = {
            "extreme_closeup": 0.82, "closeup": 0.78,
            "medium_closeup": 0.72, "medium": 0.62,
            "wide": 0.45, "extreme_wide": 0.35,
        }
        PULID_INTENSE_EMOTION_DECAY = 0.80
        PULID_INTENSE_KEYWORDS = set()

    sb = page_cfg.get("_prebuilt_storyboard") or {}
    shot_type = (sb.get("shot_type") or "medium").lower()
    base_strength = PULID_STRENGTH_BY_SHOT_TYPE.get(shot_type, 0.62)

    # 情绪衰减
    emo_text = (sb.get("focal_subject") or "").lower()
    for mh in (sb.get("visual_must_haves") or []):
        if isinstance(mh, dict):
            emo_text += " " + (mh.get("mood") or "")
    if any(k in emo_text for k in PULID_INTENSE_KEYWORDS):
        base_strength = round(base_strength * PULID_INTENSE_EMOTION_DECAY, 2)

    resolved = []
    for idx, ch in enumerate(chars):
        name = ch["name"]
        p_ref = p_lookup.get(name)
        if not p_ref:
            continue
        rel_path = p_ref.get("path", "")
        if not rel_path:
            continue
        abs_path = rel_path
        if not Path(abs_path).is_absolute():
            abs_path = str(Path(project_root) / rel_path)
        if not Path(abs_path).exists():
            print(f"  [v260 WARNING] portrait not found: {abs_path!r}")
            continue
        print(f"  [v260 OK] portrait found: {abs_path!r}")

        region = ch["region"] or _DEFAULT_REGIONS[idx % len(_DEFAULT_REGIONS)]
        en_name = ch["en"]
        # prompt = en 描述 (renderer 的 build_v260_prompts 优先用 prompt)
        prompt = en_name

        resolved.append({
            "character": name,
            "en_name": en_name,
            "path": abs_path,
            "strength": base_strength,
            "region": region,
            "prompt": prompt,
        })

    return resolved


# ════════════════════════════════════════════════════════════════
# 主入口: 参数准备
# ════════════════════════════════════════════════════════════════

def prepare_v260_params(params: dict, page_cfg: dict,
                          story_id: str, story=None,
                          project_root: str = None) -> tuple:
    """v2.6 一站式参数注入。返回 (new_params, used_paths)。

    used_paths:
      ["regional", "pulid_multi"]  — 多人路径就绪
      []                            — 不足 2 角色 / mask 失败 (调用方 fallback 单人)
    """
    print(f"  [v260 debug] project_root={project_root!r}")
    new_params = dict(params)

    resolved = resolve_multi_portrait_refs(page_cfg, story, project_root)

    if len(resolved) < 2:
        print(f"  [v260] 有效 portrait 角色 < 2,降级单人")
        return new_params, []

    # mask 模板
    try:
        from core.mask_templates import choose_mask_template, generate_mask_pair
    except ImportError:
        try:
            from mask_templates import choose_mask_template, generate_mask_pair
        except ImportError:
            print("  [v260] mask_templates 不可用,降级")
            return new_params, []

    mask_template = choose_mask_template(page_cfg)

    # ★ mask 固定 1024 (与 renderer 的 latent/SolidMask 同值)
    left_mask, right_mask = generate_mask_pair(
        mask_template, width=_V260_RENDER_SIZE, height=_V260_RENDER_SIZE)
    if not left_mask or not right_mask:
        print(f"  [v260] mask 生成失败,降级")
        return new_params, []

    # 注入参数
    new_params["_v260_chars"] = resolved
    new_params["_v260_mask_template"] = mask_template
    new_params["_v260_left_mask"] = left_mask
    new_params["_v260_right_mask"] = right_mask
    new_params["_v260_render_size"] = _V260_RENDER_SIZE   # renderer 读这个建 latent

    # 兼容 v2.3.5 降级字段 (用第一个角色)
    first = resolved[0]
    new_params["_portrait_ref_path"] = first["path"]
    new_params["_portrait_ref_strength"] = first["strength"]
    new_params["_portrait_character"] = first["character"]

    print(f"  [v260 debug] chars={[c['character'] for c in resolved]}")
    print(f"  [v260 debug] regions={[c['region'] for c in resolved]}")
    print(f"  [v260 debug] mask_template={mask_template} size={_V260_RENDER_SIZE}")

    return new_params, ["regional", "pulid_multi"]


# ════════════════════════════════════════════════════════════════
# pipeline.py 集成指南 (不变)
# ════════════════════════════════════════════════════════════════
#
#   from core.pipeline_v260_router import (
#       prepare_v260_params, is_v260_enabled, should_use_regional,
#   )
#
#   workflow_func = comfy_generate_flux
#   if is_v260_enabled() and should_use_regional(page_cfg, self.story):
#       new_params, used = prepare_v260_params(
#           params, page_cfg, self.story.story_id,
#           story=self.story, project_root=None,
#       )
#       if "regional" in used:
#           from core.renderer import comfy_generate_flux_v260_multichar
#           params = new_params
#           workflow_func = comfy_generate_flux_v260_multichar
#           print(f"  [v260] p{page_cfg['page']} 多角色路径: {used}")
#   if workflow_func is comfy_generate_flux and is_v234_path_enabled():
#       new_params, used = prepare_v234_params(...)   # 单人路径
#       ...
