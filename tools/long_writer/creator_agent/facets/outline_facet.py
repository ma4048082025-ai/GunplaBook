"""
.outline_facet
==================================
outline 阶段的对话式精炼。

对话目标:
  从主题(或一个粗略 concept)出发,通过多轮对话产出符合
  long_writer.outline 契约的 outline.yaml。
"""

import json
import re
from copy import deepcopy
from pathlib import Path
from typing import Optional

from .base import Facet, Suggestion, Modification


# ════════════════════════════════════════════════════════════════
# Facet 评估 rubric
# ════════════════════════════════════════════════════════════════
# OUTLINE_RUBRIC 已废弃:v2.4.7 改为从 narrative_profile 动态读取
# 之前写死"评估一份恐怖故事大纲时"导致所有题材都被当恐怖评估
# 现在每次 build_*_prompt 时,通过 _rubric_text(state) 按 theme 拼出
# 对应题材的 rubric。
def _rubric_text(state: dict) -> str:
    """根据当前 state 的 theme_id 取对应的 rubric 文本。"""
    from long_writer.narrative_profile import load_narrative
    theme_id = state.get("theme_id", "default")
    n = load_narrative(theme_id)
    return f"评估这份大纲时,你看这 {len(n.rubric)} 个维度:\n\n" + n.rubric_text()

# ════════════════════════════════════════════════════════════════
# 字段操作工具
# ════════════════════════════════════════════════════════════════

# 中文标签 → 英文字段名 的翻译映射(给 LLM 兜底用)
_FIELD_TRANSLATIONS = {
    "标题": "title",
    "题目": "title",
    "name": "title",          # 顶层 name 可能指标题
    "钩子": "premise",
    "premise": "premise",
    "概要": "premise",
    "角色": "characters",
    "人物": "characters",
    "主角": "characters",
    "配角": "characters",
    "characters": "characters",
    "章节": "chapters",
    "chapters": "chapters",
    "视觉": "visual_palette",
    "调色": "visual_palette",
    "视觉调色": "visual_palette",
    "visual": "visual_palette",
    "visual_palette": "visual_palette",
    "bgm": "bgm_palette",
    "音乐": "bgm_palette",
    "BGM": "bgm_palette",
    "bgm_palette": "bgm_palette",
    "总字数": "total_words",
    "字数": "total_words",
    "total_words": "total_words",
    "章数": "chapters_count",
    "chapters_count": "chapters_count",
}


def _translate_field(field: str) -> str:
    """把 LLM 可能给的中文字段名翻译为英文 schema 字段名。

    例:
      '角色[0].name'  → 'characters[0].name'
      '角色.lead.陈远正' → 无法可靠映射,原样返回(会被白名单拒)
      '标题'          → 'title'
    """
    if not field:
        return field
    # 找到第一个 . 或 [ 之前的 token,翻译它,其余保留
    m = re.match(r"^([^.\[]+)(.*)$", field)
    if not m:
        return field
    head, rest = m.group(1), m.group(2)
    translated = _FIELD_TRANSLATIONS.get(head, head)
    return translated + rest


def _set_field(state: dict, path: str, value):
    parts = _parse_field_path(path)
    obj = state
    for p in parts[:-1]:
        obj = _step(obj, p)
    last = parts[-1]
    obj[last] = value


def _append_field(state: dict, path: str, value):
    parts = _parse_field_path(path)
    obj = state
    for p in parts:
        obj = _step(obj, p)
    if not isinstance(obj, list):
        raise ValueError(f"append 目标 {path} 不是 list (是 {type(obj).__name__})")
    obj.append(value)


def _delete_field(state: dict, path: str):
    parts = _parse_field_path(path)
    obj = state
    for p in parts[:-1]:
        obj = _step(obj, p)
    last = parts[-1]
    if isinstance(last, int):
        del obj[last]
    else:
        obj.pop(last, None)


def _parse_field_path(path: str) -> list:
    tokens = []
    for raw in path.split("."):
        m = re.match(r"^([^\[]+)((?:\[\d+\])*)$", raw.strip())
        if not m:
            tokens.append(raw)
            continue
        tokens.append(m.group(1))
        for ix in re.findall(r"\[(\d+)\]", m.group(2)):
            tokens.append(int(ix))
    return tokens


def _step(obj, key):
    return obj[key]


# 给 LLM 看的字段路径示例(写进 system_prompt)
_SCHEMA_GUIDE = """
当前大纲的 schema (你修改时必须用英文字段名,不要用中文):

  title                     → 故事标题
  premise                   → 一句话钩子
  visual_palette            → 视觉调色描述
  bgm_palette               → BGM 描述
  total_words               → 目标总字数
  characters                → 角色列表 (list)
    characters[i].name      → 角色第 i 个的中文名
    characters[i].role      → 'lead' / 'extra' / 'group'
    characters[i].gender    → 'male' / 'female'
    characters[i].age       → 整数
    characters[i].desc      → 中文描述
  chapters                  → 章节列表 (list)
    chapters[i].title       → 章节标题
    chapters[i].arc_role    → 'hook' / 'setup' / 'rising' / 'climax' / 'twist'
    chapters[i].tone        → 'eerie' / 'tension' / ...
    chapters[i].target_words→ 整数
    chapters[i].summary     → 章节梗概

修改 ops 的具体格式示例:

  改主角名字:
    {"action": "set", "field": "characters[0].name", "value": "陆砚舟"}

  改标题:
    {"action": "set", "field": "title", "value": "民国盗墓录"}

  改钩子:
    {"action": "set", "field": "premise", "value": "新的一句话钩子"}

  加配角:
    {"action": "append", "field": "characters",
     "value": {"name": "老张头", "role": "extra", "gender": "male",
               "age": 60, "desc": "30 年前参与封印鬼王的幸存者"}}

  改某一章字数:
    {"action": "set", "field": "chapters[0].target_words", "value": 200}

  删除某个角色:
    {"action": "delete", "field": "characters[3]"}

注意:
  - 一次修改可以包含多个 ops(用户说"接受 1-5 条建议"就生成多个 ops)
  - value 必须是完整的新值,不能省略
"""


# ════════════════════════════════════════════════════════════════
# OutlineFacet
# ════════════════════════════════════════════════════════════════

class OutlineFacet(Facet):
    name = "outline"
    description = "从主题脑暴到 outline.yaml 的对话式精炼"

    seed_concept: str = ""
    seed_theme: str = "chinese_horror_tales"
    seed_words: int = 4000
    seed_chapters: int = 10
    seed_series: str = "long_tales"

    # ─── 状态 IO ──────────────────────────────────

    def load_state(self, source_path: Optional[Path]) -> dict:
        if source_path:
            import yaml
            with open(source_path, "r", encoding="utf-8") as f:
                return yaml.safe_load(f)
        from long_writer.outline import generate_outline
        outline = generate_outline(
            concept        = self.seed_concept,
            total_words    = self.seed_words,
            chapters_count = self.seed_chapters,
            theme_id       = self.seed_theme,
            series         = self.seed_series,
        )
        # 兜底:确保 chapters_count 有值
        if "chapters_count" not in outline:
            outline["chapters_count"] = len(outline.get("chapters", []))
        return outline

    def save_state(self, state: dict, story_id: str) -> Path:
        import yaml
        from .base import get_scripts_dir
        sid = story_id or state.get("story_id", "long_unnamed")
        scripts_dir = get_scripts_dir()
        out_path = scripts_dir / f"{sid}_outline.yaml"
        with open(out_path, "w", encoding="utf-8") as f:
            yaml.dump(state, f, allow_unicode=True,
                      default_flow_style=False, sort_keys=False)
        return out_path

    # ─── 序列化 ────────────────────────────────────

    def serialize_state(self, state: dict) -> str:
        """给 LLM 看的紧凑文本格式。

        关键:每行同时标出英文字段路径,让 LLM 知道改的时候用哪个字段名。
        """
        lines = []
        lines.append(f"title: {state.get('title', '?')}")
        lines.append(f"premise: {state.get('premise', '?')}")
        lines.append(f"theme_id: {state.get('theme_id', '?')}  series: {state.get('series', '?')}")
        lines.append(f"total_words: {state.get('total_words', '?')}  chapters_count: {state.get('chapters_count', len(state.get('chapters', [])))}")
        lines.append(f"visual_palette: {state.get('visual_palette', '')}")
        lines.append(f"bgm_palette: {state.get('bgm_palette', '')}")
        lines.append("")
        chars = state.get("characters", [])
        lines.append(f"characters ({len(chars)}):")
        for i, c in enumerate(chars):
            lines.append(f"  characters[{i}] role={c.get('role', '?'):5s} "
                         f"name={c.get('name', '?')} "
                         f"age={c.get('age', '?')} "
                         f"gender={c.get('gender', '?')}")
            desc = c.get("desc") or c.get("description", "")
            if desc:
                lines.append(f"    desc: {desc}")
        lines.append("")
        chapters = state.get("chapters", [])
        lines.append(f"chapters ({len(chapters)}):")
        for i, ch in enumerate(chapters):
            lines.append(f"  chapters[{i}] id={ch.get('id', '?')} "
                         f"arc_role={ch.get('arc_role', '?'):10s} "
                         f"tone={ch.get('tone', '?'):8s} "
                         f"target_words={ch.get('target_words', '?')}  "
                         f"title={ch.get('title', '?')}")
            if ch.get("summary") or ch.get("synopsis"):
                lines.append(f"    summary: {ch.get('summary') or ch.get('synopsis')}")
        return "\n".join(lines)

    def state_summary(self, state: dict) -> str:
        chars = state.get("characters", [])
        n_leads = sum(1 for c in chars if c.get("role") == "lead")
        n_extras = sum(1 for c in chars if c.get("role") == "extra")
        return (f"  标题: {state.get('title', '?')}\n"
                f"  钩子: {state.get('premise', '?')[:80]}\n"
                f"  角色: {n_leads} 主角 + {n_extras} 配角\n"
                f"  章节: {len(state.get('chapters', []))} 章 / "
                f"{state.get('total_words', '?')} 字")

    # ─── prompt 构造 ──────────────────────────────

    def build_system_prompt(self, state: dict) -> str:
        # v2.4.7: identity / pacing / rubric 都从 narrative_profile 按 theme 注入

        from long_writer.narrative_profile import load_narrative
        n = load_narrative(state.get("theme_id", "default"))

        safety_block = ""
        if n.story_safety:
            safety_block = f"\n【内容安全约束】\n{n.story_safety}\n"

        return f"""{n.identity}。
你正在跟一位创作者讨论一份故事大纲。
风格基准: {n.benchmark}
叙事重点: {n.arc_emphasis}
节奏指引: {n.pacing_guide}
{safety_block}
你的职责:
  1. 引导创作者把大纲打磨得更符合此题材的最佳实践
  2. 用具体可执行的建议帮助创作者改进
  3. 不替创作者做最终决定 ── 你提选项,他选择

风格:
  - 简洁,不啰嗦
  - 用具体例子代替抽象建议
  - 一次只聚焦一两个问题
  - 听到用户表达明确意图就给出 proposed_modification

【重要】关于 proposed_modification:
  - 当用户说"按 1-5 都接受"等批量采纳时,在同一个 modification 的 ops 里
    生成多个操作,每条建议对应一个 op
  - field 必须用英文字段名(见下方 schema),不要用中文
  - value 必须包含完整的新值,不能省略
  - 如果你只是在提建议或追问,不要生成 proposed_modification

{_SCHEMA_GUIDE}

{_rubric_text(state)}

当前大纲:
─────────────────────────────────
{self.serialize_state(state)}
─────────────────────────────────
"""

    def build_suggestions_prompt(self, state: dict) -> str:
        # v2.4.7: rubric 按 theme 动态生成
        from long_writer.narrative_profile import load_narrative
        n = load_narrative(state.get("theme_id", "default"))

        return f"""你正在看一份大纲,从下面 {len(n.rubric)} 个维度找 3-5 条最有价值的改进点。
风格基准: {n.benchmark}

{_rubric_text(state)}

当前大纲:
{self.serialize_state(state)}

请用 JSON 数组返回,每条建议格式:
{{
  "label": "(≤ 20 字的标签)",
  "rationale": "(详细说明问题在哪、怎么改的具体例子)",
  "focus_area": "(涉及的英文字段名,如 'characters' / 'chapters[0]' / 'visual_palette' / 'title' / 'premise')"
}}

只返回 JSON 数组,不要其他文字,不要 ```json 围栏。"""

    def parse_suggestions(self, raw: str) -> "list[Suggestion]":
        cleaned = raw.strip()
        if cleaned.startswith("```"):
            lines = cleaned.split("\n")
            cleaned = "\n".join(lines[1:-1] if lines[-1].startswith("```")
                               else lines[1:])
        try:
            data = json.loads(cleaned)
        except json.JSONDecodeError:
            return []
        out = []
        for d in data:
            out.append(Suggestion(
                label=d.get("label", "")[:30],
                rationale=d.get("rationale", ""),
                focus_area=d.get("focus_area", ""),
            ))
        return out

    # ─── apply_modification ───────────────────────

    _ALLOWED_FIELD_PREFIXES = (
        "title", "premise", "characters", "chapters",
        "visual_palette", "bgm_palette",
        "total_words", "chapters_count",
    )

    def apply_modification(self, state: dict, modif: Modification) -> dict:
        new_state = deepcopy(state)
        applied = 0
        errors = []

        for op in modif.ops:
            action = op.get("action", "set")
            field = op.get("field", "")
            value = op.get("value")

            # ★ 兜底:把可能的中文字段名翻译成英文
            field_translated = _translate_field(field)
            if field_translated != field:
                print(f"  [facet] 字段翻译: {field!r} → {field_translated!r}")
                field = field_translated

            # 字段白名单
            if not any(field.startswith(p) for p in self._ALLOWED_FIELD_PREFIXES):
                errors.append(f"字段 {field!r} 不在允许范围")
                continue

            # value 缺失检查(对 set/append 而言)
            if action in ("set", "append") and value is None:
                errors.append(f"字段 {field!r} 的 {action} 操作缺少 value")
                continue

            try:
                if action == "set":
                    _set_field(new_state, field, value)
                elif action == "append":
                    _append_field(new_state, field, value)
                elif action == "delete":
                    _delete_field(new_state, field)
                else:
                    errors.append(f"未知操作 {action!r}")
                    continue
                applied += 1
            except Exception as e:
                errors.append(f"应用 {field!r} 失败: {e}")

        if applied == 0 and errors:
            raise ValueError("; ".join(errors))
        if errors:
            # 部分成功,打印警告但接受
            print(f"  [facet] 部分 ops 失败 (已应用 {applied}/{len(modif.ops)}):")
            for e in errors:
                print(f"    • {e}")
        return new_state

    # ─── 入口提示 ─────────────────────────────────

    def initial_prompt_for_user(self, state: dict) -> str:
        return f"""我刚根据你的 concept "{self.seed_concept}" 生成了一份大纲。

{self.serialize_state(state)}

我看了一下,觉得有几点值得讨论。让我先列给你看 ──"""
