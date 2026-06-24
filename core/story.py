"""
story.py ── 故事配置加载器
============================
读取 stories/xxx.yaml，提供故事配置访问。

新增：lora_ref 支持
  角色可以用 lora_ref: "gundam_aerial" 引用 loras/ 目录下的文件，
  触发词、文件名、强度自动合并，故事 YAML 里不再重复写触发词。
  故事 YAML 里写的值优先级高于 loras/ 里的值（可以局部覆盖）。
"""

from pathlib import Path
from typing import Optional
import yaml


class StoryConfig:

    def __init__(self, story_path: str):
        self.path = story_path
        with open(story_path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)

        self.title      = data["title"]
        self.series     = data.get("series", "default")
        self.pages      = data.get("pages", [])
        self.scenes     = data.get("scene_templates", {})
        self.default_theme_path: str = data.get("theme", "themes/gundam.yaml")

        # 加载角色，自动解析 lora_ref
        raw_chars = data.get("characters", {})
        self.characters = {
            name: self._resolve_lora(name, cfg)
            for name, cfg in raw_chars.items()
        }

        meta = data.get("meta", {})
        self.premise = meta.get("premise", "")

    def _resolve_lora(self, char_name: str, char_cfg: dict) -> dict:
        """
        如果角色有 lora_ref 字段，从 loras/<ref>.yaml 读取并合并。
        故事 YAML 里的值优先（可局部覆盖 LoRA 库里的默认值）。
        没有 lora_ref 时直接返回原始配置（向后兼容旧故事文件）。
        """
        ref = char_cfg.get("lora_ref")
        if not ref:
            return char_cfg

        # 优先在 loras/ 根目录找，再递归扫子目录（如 loras/flux/、loras/sd15/）
        lora_path = Path("loras") / f"{ref}.yaml"
        if not lora_path.exists():
            # 递归查找
            matches = list(Path("loras").rglob(f"{ref}.yaml"))
            if matches:
                lora_path = matches[0]
            else:
                print(f"  [警告] LoRA 库文件不存在: loras/**/{ref}.yaml，使用故事里的原始配置")
                return char_cfg

        with open(lora_path, encoding="utf-8") as f:
            lora_data = yaml.safe_load(f)

        # 基础字段从 LoRA 库读取
        merged = {
            "lora":          lora_data.get("file", ""),
            "strength":      lora_data.get("strength", 1.0),
            "trigger_solo":  lora_data.get("trigger_solo", ""),
            "trigger_multi": lora_data.get("trigger_multi", ""),
        }

        # 故事 YAML 里写了就覆盖（局部定制优先）
        for key in ("lora", "strength", "trigger_solo", "trigger_multi"):
            if key in char_cfg:
                merged[key] = char_cfg[key]

        # 保留故事里其他字段（desc, key_features, ref_image 等）
        for key, val in char_cfg.items():
            if key not in merged and key != "lora_ref":
                merged[key] = val

        return merged

    @property
    def story_id(self) -> str:
        return Path(self.path).stem

    @property
    def is_long_story(self) -> bool:
        """v2.3：判断是否长篇副线程
        任一满足即为长篇：
          - yaml 顶层有 _long_story: true
          - story_id 以 long_ 开头
          - pages 数量 > 30
          - 任一 page 含 _skip_llm_alignment 或 _hold 标记
        """
        # 优先看 yaml 顶层标记
        try:
            import yaml as _yaml
            with open(self.path, encoding="utf-8") as _f:
                _data = _yaml.safe_load(_f)
            if _data.get("_long_story"):
                return True
        except Exception:
            pass

        # story_id 前缀
        if self.story_id.startswith("long_"):
            return True

        # pages 数量
        if len(self.pages) > 30:
            return True

        # page 标记
        for p in self.pages:
            if p.get("_skip_llm_alignment") or p.get("_hold"):
                return True

        return False

    def get_page(self, page_num: int) -> Optional[dict]:
        return next((p for p in self.pages if p["page"] == page_num), None)

    def get_scene(self, scene_type: str) -> dict:
        return self.scenes.get(scene_type, {})

    def char_features(self, char_names: list[str]) -> str:
        features = []
        for name in char_names:
            f = self.characters.get(name, {}).get("key_features", "")
            if f:
                features.append(f)
        return " / ".join(features)

    def ip_registry(self) -> dict[str, str]:
        result = {}
        for name, cfg in self.characters.items():
            ref = cfg.get("ref_image", "")
            if ref and Path(ref).exists():
                result[name] = ref
        return result

    def save_ref_image(self, char_name: str, image_path: str):
        with open(self.path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
        if char_name in data.get("characters", {}):
            data["characters"][char_name]["ref_image"] = image_path
            with open(self.path, "w", encoding="utf-8") as f:
                yaml.dump(data, f, allow_unicode=True,
                          default_flow_style=False, sort_keys=False)
            if char_name in self.characters:
                self.characters[char_name]["ref_image"] = image_path

    def __repr__(self):
        return f"<StoryConfig: {self.title} ({len(self.pages)}页)>"
