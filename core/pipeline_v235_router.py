"""
pipeline_v235_router.py ── v2.3.5 pipeline 路由助手（Redux + PuLid 双路径）
=============================================================
独立辅助模块，封装"page 是否走 Redux / PuLid"的判断 + 参考图路径解析。

让 pipeline.py 的改动最小化：只需要在生图入口加 ~10 行，
不需要在 pipeline.py 里写任何 Redux/PuLid 业务逻辑。

兼容 v2.3.4 旧名 pipeline_redux_router——保留 should_use_redux/resolve_ref_path/
inject_redux_params 三个旧函数名作为转发别名。

用法（pipeline.py 集成指南见末尾）。
"""

from pathlib import Path


# ════════════════════════════════════════════════════════════════
# Feature flag 检查
# ════════════════════════════════════════════════════════════════

def is_redux_enabled() -> bool:
    """读 config.ENABLE_V234_REDUX；未定义则关"""
    try:
        from config import ENABLE_V234_REDUX
        return bool(ENABLE_V234_REDUX)
    except (ImportError, AttributeError):
        return False


def is_pulid_enabled() -> bool:
    """读 config.ENABLE_V235_PULID；未定义则关"""
    try:
        from config import ENABLE_V235_PULID
        return bool(ENABLE_V235_PULID)
    except (ImportError, AttributeError):
        return False


def is_v234_path_enabled() -> bool:
    """任一增强分支开启时返回 True"""
    return is_redux_enabled() or is_pulid_enabled()


# ════════════════════════════════════════════════════════════════
# Redux 决策（v2.3.4）
# ════════════════════════════════════════════════════════════════

def should_use_redux(page_cfg: dict) -> bool:
    """
    判断本页是否应该走 Redux。
    满足所有条件才走：
      1. ENABLE_V234_REDUX = True
      2. page_cfg._visual_ref 字段存在且非空
      3. _visual_ref.from_page 合法
      4. _visual_ref.strength > 0
    """
    if not is_redux_enabled():
        return False
    vr = page_cfg.get("_visual_ref")
    if not isinstance(vr, dict):
        return False
    try:
        from_page = int(vr.get("from_page", 0))
        strength = float(vr.get("strength", 0))
    except (TypeError, ValueError):
        return False
    if from_page < 1 or strength <= 0:
        return False
    return True


def resolve_ref_path(page_cfg: dict, story_id: str,
                     out_dir: str = None) -> str:
    """
    根据 _visual_ref.from_page 找前镜图的 mac 本地路径。
    返回字符串路径；找不到返回 ""。
    """
    vr = page_cfg.get("_visual_ref", {}) or {}
    from_page = int(vr.get("from_page", 0))
    if from_page < 1:
        return ""

    if not out_dir:
        try:
            from config import OUT_DIR
        except (ImportError, AttributeError):
            OUT_DIR = "./book_output"
        out_dir = str(Path(OUT_DIR) / story_id)

    out_path = Path(out_dir)
    if not out_path.exists():
        return ""

    pattern = f"page{from_page:02d}_*.png"
    candidates = sorted(
        out_path.glob(pattern),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if not candidates:
        candidates = sorted(
            out_path.glob(f"page{from_page}_*.png"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
    if not candidates:
        return ""
    return str(candidates[0])


# ════════════════════════════════════════════════════════════════
# PuLid 决策（v2.3.5）
# ════════════════════════════════════════════════════════════════

# 按 shot_type 决定 PuLid 默认强度
# v2.3.6：整体下调——0.90+ 会把定妆照的脸部几何（含表情）锁死，
#         覆盖 prompt 想要的情绪表情。降档后身份仍稳定，表情有空间。
PULID_STRENGTH_BY_SHOT_TYPE = {
    "extreme_closeup": 0.82,   # was 0.95
    "closeup":         0.78,   # was 0.90
    "medium_closeup":  0.72,   # was 0.85
    "medium":          0.62,   # was 0.75
    "wide":            0.45,   # was 0.55
    "extreme_wide":    0.35,   # was 0.45
}

# v2.3.6：情绪强烈的页，在 shot_type 强度基础上再乘衰减系数
# 中性/微笑定妆照在高强度下会主动覆盖恐惧/震惊等表情 prompt，
# 命中以下关键词时按此系数衰减，把脸部主导权还给 prompt。
PULID_INTENSE_EMOTION_DECAY = 0.80
PULID_INTENSE_KEYWORDS = {
    "horror", "fear", "terrified", "terror", "shock", "shocked",
    "grief", "rage", "panic", "dread", "epiphanic", "realization",
    "widening", "frozen", "scream", "despair", "anguish",
}


def should_use_pulid(page_cfg: dict) -> bool:
    """
    判断本页是否应该走 PuLid。
    满足所有条件才走：
      1. ENABLE_V235_PULID = True
      2. page_cfg._portrait_refs 非空（且至少 1 个有有效 path）
      3. 镜头不是"角色背影/不可见脸"（暂用 shot_type 判断；将来可加更细规则）
    """
    if not is_pulid_enabled():
        return False
    refs = page_cfg.get("_portrait_refs") or []
    if not refs:
        return False
    # 过滤无效路径
    valid = [r for r in refs
             if isinstance(r, dict) and r.get("path")]
    if not valid:
        return False
    # 远景小人脸面积太小，PuLid 收益低（也避免脸糊不可识别）
    # 但为了一致性还是允许走（pipeline.py 不阻塞）
    return True


def resolve_portrait_ref(page_cfg: dict,
                          project_root: str = None) -> tuple:
    """
    从 page_cfg._portrait_refs 选一个角色的定妆照。

    策略：当前简单选第一个（即 page.characters 第一个 lead 角色）。
    将来可扩展：根据 focal_subject 文字命中度排序、根据画面占比排序等。

    返回 (path, character_name, strength)：
      - path: 绝对路径或 ""（找不到时）
      - character_name: 角色名（仅日志用）
      - strength: 按 shot_type 推算
    """
    refs = page_cfg.get("_portrait_refs") or []
    valid_refs = [r for r in refs
                  if isinstance(r, dict) and r.get("path")]
    if not valid_refs:
        return "", "", 0.0

    # 简单策略：选第一个
    chosen = valid_refs[0]
    rel_path = chosen["path"]
    char_name = chosen.get("character", "?")

    # 路径解析（相对 project_root）
    if not project_root:
        project_root = "."
    abs_path = rel_path
    if not Path(abs_path).is_absolute():
        abs_path = str(Path(project_root) / rel_path)

    if not Path(abs_path).exists():
        return "", char_name, 0.0

    # 强度按 shot_type 取
    sb = page_cfg.get("_prebuilt_storyboard", {}) or {}
    shot_type = sb.get("shot_type", "medium")
    strength = PULID_STRENGTH_BY_SHOT_TYPE.get(shot_type, 0.62)

    # v2.3.6：情绪强烈的页衰减身份锁定
    # 扫 must_haves[].mood 和 focal_subject，命中强情绪关键词就乘衰减系数，
    # 避免中性/微笑定妆照覆盖 prompt 里的表情。
    emo_text = ""
    for mh in (sb.get("visual_must_haves") or []):
        if isinstance(mh, dict):
            emo_text += " " + (mh.get("mood") or "")
    emo_text += " " + (sb.get("focal_subject") or "")
    emo_text = emo_text.lower()
    if any(k in emo_text for k in PULID_INTENSE_KEYWORDS):
        strength = round(strength * PULID_INTENSE_EMOTION_DECAY, 2)
        print(f"  [PuLid] 情绪强烈页，强度衰减 → {strength}")

    return abs_path, char_name, strength


# ════════════════════════════════════════════════════════════════
# 统一参数注入
# ════════════════════════════════════════════════════════════════

def inject_redux_params(params: dict, page_cfg: dict,
                        ref_path: str) -> dict:
    """v2.3.4 兼容：注入 Redux 参数"""
    new_params = dict(params)
    vr = page_cfg.get("_visual_ref", {}) or {}
    new_params["_visual_ref_path"] = ref_path
    new_params["_visual_ref_strength"] = float(vr.get("strength", 0.5))
    return new_params


def inject_pulid_params(params: dict, portrait_path: str,
                         character: str, strength: float) -> dict:
    """v2.3.5 新增：注入 PuLid 参数"""
    new_params = dict(params)
    new_params["_portrait_ref_path"] = portrait_path
    new_params["_portrait_ref_strength"] = float(strength)
    new_params["_portrait_character"] = character
    return new_params


def prepare_v234_params(params: dict, page_cfg: dict,
                         story_id: str,
                         project_root: str = None,
                         out_dir: str = None) -> tuple:
    """
    一站式：检查 Redux + PuLid 路径，注入参数，返回 (new_params, used_paths)。

    used_paths: list[str]，本次实际启用了哪些路径，例如 ["redux"] / ["pulid"] /
                ["redux", "pulid"] / [](都没用)
    """
    new_params = dict(params)
    used = []

    if should_use_redux(page_cfg):
        ref_path = resolve_ref_path(page_cfg, story_id, out_dir)
        if ref_path:
            new_params = inject_redux_params(new_params, page_cfg, ref_path)
            used.append("redux")
        else:
            print(f"  [Redux] p{page_cfg.get('page', '?')} 参考图找不到，跳过 Redux")

    if should_use_pulid(page_cfg):
        portrait_path, char_name, strength = resolve_portrait_ref(
            page_cfg, project_root)
        if portrait_path:
            new_params = inject_pulid_params(
                new_params, portrait_path, char_name, strength)
            used.append("pulid")
        else:
            print(f"  [PuLid] p{page_cfg.get('page', '?')} 定妆照找不到，跳过 PuLid")

    return new_params, used


# ════════════════════════════════════════════════════════════════
# pipeline.py 集成指南（10-12 行）
# ════════════════════════════════════════════════════════════════
#
# 改动位置：pipeline.py 里"生图入口"——调用 generate_and_wait(
# comfy_generate_flux, params, ...) 的那一行附近。
#
# ── 文件顶部 import 段加一行 ────────────────────────
#
#   from core.pipeline_v235_router import (
#       prepare_v234_params, is_v234_path_enabled,
#   )
#
# ── 在 generate_and_wait 调用前加几行 ───────────────
#
#   workflow_func = comfy_generate_flux   # 默认普通 FLUX
#   if is_v234_path_enabled():
#       new_params, used = prepare_v234_params(
#           params, page_cfg, self.story.story_id,
#           project_root=None,   # None=用 cwd
#           out_dir=None,        # None=用 config.OUT_DIR/<story_id>
#       )
#       if used:   # 至少 redux 或 pulid 启用了一个
#           from core.renderer import comfy_generate_flux_v234
#           params = new_params
#           workflow_func = comfy_generate_flux_v234
#           print(f"  [v235] p{page_cfg['page']} 启用路径: {used}")
#
#   prompt_id = generate_and_wait(workflow_func, params, ...)
#
# ── 关 flag 时 ──────────────────────────────────────
#
# is_v234_path_enabled() 返回 False，整个 if 块不进入，workflow_func 保持
# comfy_generate_flux，pipeline.py 行为跟改动前 100% 一致。


# ════════════════════════════════════════════════════════════════
# v2.3.4 旧 API 兼容（保留三个函数名让旧 pipeline.py 不挂）
# ════════════════════════════════════════════════════════════════

# should_use_redux / resolve_ref_path / inject_redux_params 已在上面定义
# 旧 v2.3.4 pipeline.py 的引用都能继续工作
