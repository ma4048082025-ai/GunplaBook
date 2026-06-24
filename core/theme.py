"""
theme.py ── 主题配置加载器
============================
读取 themes/xxx.yaml，提供强类型的配置访问。

这是"主题包"和"流程代码"之间唯一的桥梁。
流程代码只从这里读主题相关参数，永远不直接读 config.py 的主题内容。

设计原则：
  - 所有字段都有合理默认值（防御性编程）
  - 字段名与 YAML 结构一一对应，不做魔法转换
  - 不包含任何流程逻辑

v2 新增：
  FeedbackConfig  — 主题包对 feedback.py 词汇的覆盖配置
  TagOverride     — 单个 tag 的 add_pos / add_neg 覆盖
  ThemeConfig.feedback — 供 feedback._merge_theme() 读取

v3 新增（配合 SoftGate）：
  QualityConfig.profile — 硬伤检测的主体类型 profile
    缺省 "human_realistic"，主题作者通过 yaml 显式声明覆盖。
    详见 core/SOFTGATE_NOTES.md
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional
import yaml


@dataclass
class ModelConfig:
    checkpoint:   str = ""
    ipadapter:    str = ""
    clip_vision:  str = ""
    text_model:   str = "qwen2.5:7b"
    vision_model: str = "llava:7b"
    # 以下是新加的 FLUX 专用字段
    unet: str = ""
    clip1: str = ""
    clip2: str = ""
    vae: str = ""
    faceid_model: str = ""
    faceid_vision: str = ""


@dataclass
class PromptsConfig:
    quality_prefix: str = ""  # 纯质量词，放最前
    style_prefix: str = ""  # 风格词，放叙事内容词之后
    positive_prefix: str = ""  # 向后兼容保留，优先用 quality+style
    negative_prefix:     str = ""
    solo_background:     str = "medium gray background"
    background_append:   str = "no characters, empty scene, 4k"
    background_negative: str = "characters, mecha, robot"
    harmonize_append:    str = "cinematic lighting, atmospheric"
    harmonize_negative:  str = "flat lighting, studio background"


@dataclass
class GenerationConfig:
    default_cfg:        float = 6.5
    default_steps:      int   = 30
    upscale_denoise:    float = 0.40
    harmonize_denoise:  float = 0.28
    harmonize_steps:    int   = 15


@dataclass
class AgentConfig:
    identity:          str = "你是漫画生成的参数决策专家"
    domain:            str = "漫画、插画"
    style_guidance:    str = ""
    extra_instruction: str = "cfg_adjustment 范围：-1.0 到 +1.0"


@dataclass
class QualityConfig:
    threshold:               float = 7.0
    review_focus:            str   = "画面质量和角色准确性"
    composite_review_focus:  str   = "两个机甲是否完整、光线是否统一、融合是否自然"
    # v4 SoftGate 新增：硬伤检测的主体类型 profile
    # 取值：human_realistic / human_stylized / anthro_creature /
    #       realistic_animal / mecha / object_focus / none
    # 缺省时按 human_realistic 处理（绝大多数现有主题都是真人/古风人物）
    # 详见 core/SOFTGATE_NOTES.md
    profile:                 str   = "human_realistic"


@dataclass
class ProductionConfig:
    narration_voice:         str   = "zh-CN-YunxiNeural"
    platform:                str   = "douyin"
    canvas_width:            int   = 1080
    canvas_height:           int   = 1920
    img_zone_ratio:          float = 0.72
    min_clip_duration:       float = 4.0
    fade_duration:           float = 0.4
    youtube_canvas_width:    int   = 1920
    youtube_canvas_height:   int   = 1080
    youtube_img_zone_ratio:  float = 0.85


# ── v2 新增 ──────────────────────────────────────────────────

@dataclass
class TagOverride:
    """
    单个 feedback tag 的词汇覆盖。
    只允许覆盖 add_pos / add_neg（字符串型提示词），
    数值型调整（steps_delta / cfg_delta 等）不可被主题覆盖。
    """
    add_pos: str = ""
    add_neg: str = ""


@dataclass
class FeedbackConfig:
    """
    主题包对 feedback.py 诊断系统的词汇覆盖。

    YAML 结构示例（themes/gundam.yaml）：
      feedback:
        subject_description: "高达机甲/机器人图片"
        tag_overrides:
          weak_character:
            add_pos: "white mecha body, blue accents, v-fin antenna"
          wrong_bg:
            add_pos: "deep space, starfield"
            add_neg: "indoor, ground, building"
          has_unwanted:
            add_neg: "human, pilot, person, driver"

    feedback.py 的 _merge_theme() 读取此对象，将主题词汇
    合并进 _BASE_TAG_TO_PARAMS，覆盖对应 tag 的默认提示词。
    """
    subject_description: str  = ""
    tag_overrides:       dict = field(default_factory=dict)  # tag -> TagOverride


# ─────────────────────────────────────────────────────────────

class ThemeConfig:
    """
    主题配置。从 YAML 文件加载，提供所有主题相关参数。

    使用方式：
      theme = ThemeConfig("themes/gundam.yaml")
      print(theme.model.checkpoint)
      print(theme.feedback.subject_description)
      layout = theme.layout_for("standoff")
    """

    def __init__(self, theme_path: str):
        self.path = theme_path
        with open(theme_path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)

        self.name        = data.get("name", "未命名主题")
        self.version     = data.get("version", "1.0")
        self.description = data.get("description", "")

        # 子配置
        self.model      = self._load_model(data.get("model", {}))
        self.prompts    = self._load_prompts(data.get("prompts", {}))
        self.generation = self._load_generation(data.get("generation", {}))
        self.agent      = self._load_agent(data.get("agent", {}))
        self.quality    = self._load_quality(data.get("quality", {}))
        self.production = self._load_production(data.get("production", {}))
        self.feedback   = self._load_feedback(data.get("feedback", {}))  # v2

        # 角色分类规则
        roles = data.get("roles", {})
        self.style_only_chars: set[str] = set(roles.get("style_only", []))

        # 多角色场景类型
        self.multi_scene_types: set[str] = set(
            data.get("multi_scene_types", ["multi"]))

        # 布局预设
        self._layouts: dict = data.get("layouts", {})

    # ── 布局 ──────────────────────────────────────────────

    def layout_for(self, scene_type: str) -> list[dict]:
        return (self._layouts.get(scene_type)
                or self._layouts.get("default")
                or [
                    {"px": 0.08, "py": 0.06, "scale": 0.68},
                    {"px": 0.48, "py": 0.16, "scale": 0.54},
                ])

    # ── Agent system prompt 构建 ──────────────────────────

    def build_agent_system_prompt(self) -> str:
        a = self.agent
        return f"""{a.identity}。
领域专长：{a.domain}。
风格指导：{a.style_guidance}

根据故事情节输出JSON格式的生图参数。只返回JSON，不要包含```json```或其他文字。
{{
  "scene_emphasis": "场景重点（英文，20词内）",
  "mood": "情绪关键词（英文）",
  "lighting": "光影关键词（英文）",
  "composition": "构图关键词（英文）",
  "extra_positive": "额外正向词（英文）",
  "extra_negative": "额外负向词（英文）",
  "cfg_adjustment": 0,
  "reasoning": "决策理由（中文，一句话）"
}}
{a.extra_instruction}"""

    # ── 加载辅助方法 ──────────────────────────────────────

    @staticmethod
    def _load_model(d: dict) -> ModelConfig:
        return ModelConfig(**{k: v for k, v in d.items()
                              if k in ModelConfig.__dataclass_fields__})

    @staticmethod
    def _load_prompts(d: dict) -> PromptsConfig:
        return PromptsConfig(**{k: v for k, v in d.items()
                                if k in PromptsConfig.__dataclass_fields__})

    @staticmethod
    def _load_generation(d: dict) -> GenerationConfig:
        return GenerationConfig(**{k: v for k, v in d.items()
                                   if k in GenerationConfig.__dataclass_fields__})

    @staticmethod
    def _load_agent(d: dict) -> AgentConfig:
        return AgentConfig(**{k: v for k, v in d.items()
                              if k in AgentConfig.__dataclass_fields__})

    @staticmethod
    def _load_quality(d: dict) -> QualityConfig:
        return QualityConfig(**{k: v for k, v in d.items()
                                if k in QualityConfig.__dataclass_fields__})

    @staticmethod
    def _load_production(d: dict) -> ProductionConfig:
        return ProductionConfig(**{k: v for k, v in d.items()
                                   if k in ProductionConfig.__dataclass_fields__})

    @staticmethod
    def _load_feedback(d: dict) -> FeedbackConfig:
        """
        解析 YAML 里的 feedback 字段。
        tag_overrides 里每个 tag 只允许 add_pos / add_neg 两个键。
        缺失或空值的主题包返回空 FeedbackConfig（feedback.py 用默认值）。
        """
        if not d:
            return FeedbackConfig()
        overrides_raw = d.get("tag_overrides", {})
        overrides = {}
        for tag, patch in overrides_raw.items():
            if isinstance(patch, dict):
                overrides[tag] = TagOverride(
                    add_pos=patch.get("add_pos", ""),
                    add_neg=patch.get("add_neg", ""),
                )
        return FeedbackConfig(
            subject_description=d.get("subject_description", ""),
            tag_overrides=overrides,
        )

    def __repr__(self):
        return f"<ThemeConfig: {self.name} v{self.version}>"
