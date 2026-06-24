"""
mask_templates.py v2.6
======================
Regional Prompter mask 模板生成器。

为 v2.6 多角色生图路径提供:
  - 几种预设 mask 模板(left/right、对视、过肩、前后景)
  - 根据 focal_subject 关键词自动选模板
  - 生成 PNG 文件供 ComfyUI 加载

设计原则:
  - 完全用算法生成(numpy + PIL),不依赖外部资源文件
  - 缓存到 tmp 目录,避免重复生成
  - 每个模板返回一对互补 mask(left_mask + right_mask),它们的和 = 全图
  - 边界处可以有"融合带"(像 face_to_face 中间留 4% 让两区域 cross-attention 融合)

ComfyUI 用法:
  ComfyUI-ppm 的 Attention Couple 节点期待 mask 作为 LATENT 或 MASK 输入,
  我们用 LoadImage → ImageToMask → AttentionCouple 这条链路。
  生成的 PNG 是黑白图,白色=该区域生效,黑色=不生效。
"""

from pathlib import Path
from typing import Optional


# ════════════════════════════════════════════════════════════════
# 模板定义
# ════════════════════════════════════════════════════════════════
# 每个模板是 (left_region_ratio, right_region_ratio, blend_band_pct)
#   - left_region_ratio: 左角色占画面左侧比例 (0.0-1.0)
#   - right_region_ratio: 右角色占画面右侧比例 (0.0-1.0)
#   - blend_band_pct: 中央融合带占整图宽度的比例 (0.0-0.10)
# 三者关系:left + right + blend ≤ 1.0(允许中间留白让 FLUX 自由处理)

MASK_TEMPLATES = {
    # 标准左右对半 — 最通用,对话/对视场景
    "left_right": {
        "left_ratio": 0.48,
        "right_ratio": 0.48,
        "blend_band": 0.04,   # 中间 4% 融合带
        "description": "标准左右对半 50/50",
    },

    # 面对面亲密镜 — 比 left_right 融合带更宽,适合双人特写
    "face_to_face": {
        "left_ratio": 0.45,
        "right_ratio": 0.45,
        "blend_band": 0.10,   # 中间 10% 让两人面部边界自然融合
        "description": "面对面对视(中心融合带宽)",
    },

    # 左肩后镜 — 左角色占 70%(肩+背),右角色 30%(脸特写)
    "over_shoulder_left": {
        "left_ratio": 0.65,
        "right_ratio": 0.30,
        "blend_band": 0.05,
        "description": "左角色肩后,右角色脸特写",
    },

    # 镜像
    "over_shoulder_right": {
        "left_ratio": 0.30,
        "right_ratio": 0.65,
        "blend_band": 0.05,
        "description": "右角色肩后,左角色脸特写",
    },

    # 前后景关系 — 左侧前景占主体,右侧背景人物
    "foreground_background": {
        "left_ratio": 0.60,
        "right_ratio": 0.35,
        "blend_band": 0.05,
        "description": "前景主体 + 背景人物",
    },
}


# ════════════════════════════════════════════════════════════════
# 自动选模板
# ════════════════════════════════════════════════════════════════

def choose_mask_template(page_cfg: dict) -> str:
    """根据 focal_subject 和 _mask_hint 选 mask 模板。
    
    优先级:
      1. page_cfg._mask_hint (focal_director 显式指定)
      2. focal_subject 关键词推断
      3. 默认 left_right
    """
    sb = page_cfg.get("_prebuilt_storyboard") or {}
    hint = (page_cfg.get("_mask_hint")
            or sb.get("_mask_hint")
            or "").strip().lower()
    if hint in MASK_TEMPLATES:
        return hint

    focal = (sb.get("focal_subject") or
             page_cfg.get("focal_subject") or "").lower()

    # 关键词推断
    if any(kw in focal for kw in ["facing each other", "face to face",
                                    "looking at each other",
                                    "their lips", "trembling lips"]):
        return "face_to_face"
    if "over shoulder" in focal or "from behind" in focal:
        # 默认左肩,如果有明确"right shoulder"再翻转
        if "right shoulder" in focal or "right side" in focal:
            return "over_shoulder_right"
        return "over_shoulder_left"
    if ("foreground" in focal and "background" in focal) or \
       "behind in" in focal or "in the distance" in focal:
        return "foreground_background"

    return "left_right"


# ════════════════════════════════════════════════════════════════
# mask 生成 — 算法绘制 + 缓存
# ════════════════════════════════════════════════════════════════

def generate_mask_pair(template: str, width: int = 1536,
                        height: int = 1536,
                        cache_dir: Optional[Path] = None) -> tuple:
    """生成一对 mask PNG 文件,返回 (left_mask_path, right_mask_path)。
    
    缓存策略:同样的 (template, width, height) 只生成一次。
    """
    if template not in MASK_TEMPLATES:
        print(f"  [mask] 未知模板 {template},fallback 到 left_right")
        template = "left_right"

    if cache_dir is None:
        cache_dir = Path("/tmp/v260_masks")
    cache_dir = Path(cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)

    left_path = cache_dir / f"{template}_{width}x{height}_left.png"
    right_path = cache_dir / f"{template}_{width}x{height}_right.png"

    if left_path.exists() and right_path.exists():
        return str(left_path), str(right_path)

    # 用 PIL 绘制(避免引入 numpy 依赖)
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        print(f"  [mask] PIL 未安装,无法生成 mask")
        return "", ""

    cfg = MASK_TEMPLATES[template]
    left_ratio = cfg["left_ratio"]
    right_ratio = cfg["right_ratio"]
    blend_band = cfg["blend_band"]

    # 计算边界
    left_end_px = int(width * left_ratio)
    right_start_px = width - int(width * right_ratio)
    blend_width = int(width * blend_band)

    # 左 mask:左 left_end_px 像素纯白,中间融合带渐变,其余黑
    left_img = Image.new("L", (width, height), 0)
    left_draw = ImageDraw.Draw(left_img)
    # 主体区:纯白
    left_draw.rectangle([0, 0, left_end_px, height], fill=255)
    # 融合带:线性渐变 255 → 0
    blend_start = left_end_px
    blend_stop = min(left_end_px + blend_width, width)
    for x in range(blend_start, blend_stop):
        progress = (x - blend_start) / max(1, blend_stop - blend_start)
        alpha = int(255 * (1.0 - progress))
        left_draw.line([(x, 0), (x, height)], fill=alpha)
    left_img.save(left_path)

    # 右 mask:右 right_ratio 区纯白,左侧融合带渐变,其余黑
    right_img = Image.new("L", (width, height), 0)
    right_draw = ImageDraw.Draw(right_img)
    right_draw.rectangle([right_start_px, 0, width, height], fill=255)
    # 融合带:线性渐变 0 → 255
    blend_start = max(0, right_start_px - blend_width)
    blend_stop = right_start_px
    for x in range(blend_start, blend_stop):
        progress = (x - blend_start) / max(1, blend_stop - blend_start)
        alpha = int(255 * progress)
        right_draw.line([(x, 0), (x, height)], fill=alpha)
    right_img.save(right_path)

    return str(left_path), str(right_path)


# ════════════════════════════════════════════════════════════════
# 调试/查看
# ════════════════════════════════════════════════════════════════

def describe_template(template: str) -> dict:
    """查询模板配置(给日志/调试用)。"""
    return MASK_TEMPLATES.get(template, MASK_TEMPLATES["left_right"])


def list_templates() -> list:
    return list(MASK_TEMPLATES.keys())
