"""
theme_profile.py v2.5.2 — 合并版
================================
跟 themes/<theme_id>.yaml 合并为【单一真相源】。

历史变更:
  v0.1: 独立 profile.yaml,跟 themes/*.yaml 平行(造成两套配置混乱)
  v2.5.2: 读 themes/<theme_id>.yaml 的 storyboard 段,fallback 到内置默认

加载优先级:
  1. themes/<theme_id>.yaml :: storyboard 段(外置,新机制 ⭐)
  2. DEFAULT_PROFILES[theme_id](内置默认,向后兼容)
  3. DEFAULT_PROFILES["default"](终极兜底)
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


@dataclass
class ThemeProfile:
    """主题相关的画面常识库,服务 focal_director(LLM 阶段)。"""
    
    theme_id: str = "default"
    natural_era_words: list = field(default_factory=list)
    forbidden_era_words: list = field(default_factory=list)
    style_base: str = ""
    flux_unfriendly_patterns: list = field(default_factory=list)
    blacklist_keywords: list = field(default_factory=list)
    character_archetypes: dict = field(default_factory=dict)


# ════════════════════════════════════════════════════════════════
# 内置 fallback profiles
# ════════════════════════════════════════════════════════════════

DEFAULT_PROFILES = {
    
    "default": ThemeProfile(
        theme_id="default",
        blacklist_keywords=[
            "scent", "smell", "odor", "aroma",
            "sound of", "noise of", "echo of",
            "scent of", "smell of",
            "temperature", "lingering aroma",
            "maintaining", "preserving",
            "improving credibility", "enhancing realism",
            "precise composition",
            "but structurally intact", "yet still",
        ],
        flux_unfriendly_patterns=[
            ("'s shadow",
             "FLUX 会把 X 本人画进去。改写为独立的 humanoid shadow + "
             "明确 'no person in frame, shadow only'"),
            ("'s reflection",
             "FLUX 会把 X 本人画进去。改写为独立的 reflection in <surface>"),
            ("'s silhouette",
             "FLUX 会把 X 本人画进去。改写为独立的 silhouette"),
            ("invisible",
             "FLUX 不懂'看不见'。改写为 'absent / empty space / faint outline'"),
        ],
    ),
    
    "chinese_horror_tales": ThemeProfile(
        theme_id="chinese_horror_tales",
        natural_era_words=[
            "1980s mainland China", "1990s mainland China",
            "Republic of China era (1920s-1930s)",
            "ancient China", "Ming dynasty", "Qing dynasty", "Tang dynasty",
        ],
        forbidden_era_words=[
            "medieval", "medieval setting",
            "victorian", "gothic cathedral", "european castle",
        ],
        style_base=(
            "traditional chinese ink painting style fused with cinematic photography, "
            "dark moody atmosphere, deep shadows, muted color palette with crimson "
            "and indigo accents, mist and rain texture"
        ),
        blacklist_keywords=[
            "scent", "smell", "odor", "sound of", "noise of",
            "maintaining", "preserving", "but structurally intact",
            "medieval setting", "rough stone texture", "gothic", "european",
        ],
        flux_unfriendly_patterns=[
            ("'s shadow", "改写为 elongated humanoid shadow + no person in frame"),
            ("'s reflection",
             "改写为 distorted reflection visible in <surface>, no person directly in frame"),
            ("'s silhouette", "改写为独立 silhouette 描述"),
            ("invisible", "改写为 faint outline / empty space"),
            ("第九个影子",
             "FLUX 画不出'第 N 个'。改写为 'a vague humanoid shape moving within "
             "the blood pool, distorted reflection'"),
            ("七张脸", "FLUX 多人时容易乱。改写为 'multiple ghostly faces / "
                    "several distorted faces' 不强调具体数量"),
        ],
        character_archetypes={
            "道士": "elderly daoist priest, yellow ceremonial robe, white beard, "
                  "wooden staff or whisk broom, weathered face",
            "天师": "elderly celestial master, formal yellow robe with mystic symbols, "
                 "long white beard, traditional headpiece",
            "老者": "elderly chinese man, weathered face, simple cotton tunic",
            "穿越者": "young modern chinese man, contemporary casual clothes "
                  "(hoodie/jacket) that visibly contrast with the ancient setting",
        },
    ),
    
    "wuxia": ThemeProfile(
        theme_id="wuxia",
        natural_era_words=["Tang dynasty", "Song dynasty", "Ming dynasty",
                          "ancient martial arts era"],
        forbidden_era_words=["medieval", "european", "modern"],
        style_base=(
            "wuxia cinematic style, ink wash painting aesthetic, "
            "flowing robes, dramatic lighting, bamboo forest atmosphere"
        ),
        blacklist_keywords=["scent", "smell", "sound of", "maintaining", "preserving"],
    ),
}


def load_profile(theme_id: str, themes_dir: Optional[Path] = None) -> ThemeProfile:
    """v2.5.2 加载策略 — 优先级:
      1. themes/<theme_id>.yaml::storyboard 段(单一真相源)
      2. DEFAULT_PROFILES[theme_id]
      3. DEFAULT_PROFILES["default"]
    """
    if themes_dir is None:
        themes_dir = Path(__file__).parent.parent.parent / "themes"
    
    yaml_path = themes_dir / f"{theme_id}.yaml"
    if yaml_path.exists():
        try:
            import yaml
            with open(yaml_path, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
            sb = data.get("storyboard") or {}
            if sb:
                base = DEFAULT_PROFILES.get(theme_id) or DEFAULT_PROFILES["default"]
                return ThemeProfile(
                    theme_id=theme_id,
                    natural_era_words=sb.get("natural_era_words",
                                             list(base.natural_era_words)),
                    forbidden_era_words=sb.get("forbidden_era_words",
                                               list(base.forbidden_era_words)),
                    style_base=sb.get("style_base", base.style_base),
                    flux_unfriendly_patterns=[
                        tuple(p) if isinstance(p, (list, tuple)) else (str(p), "")
                        for p in sb.get("flux_unfriendly_patterns",
                                        list(base.flux_unfriendly_patterns))
                    ],
                    blacklist_keywords=sb.get("blacklist_keywords",
                                              list(base.blacklist_keywords)),
                    character_archetypes=sb.get("character_archetypes",
                                                dict(base.character_archetypes)),
                )
        except Exception as e:
            print(f"  [theme_profile] 加载 {yaml_path} 失败,fallback 到内置默认: {e}")
    
    if theme_id in DEFAULT_PROFILES:
        return DEFAULT_PROFILES[theme_id]
    
    return DEFAULT_PROFILES["default"]
