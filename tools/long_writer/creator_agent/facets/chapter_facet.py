"""
.chapter_facet
==================================
chapter 阶段的对话式精炼。

输入: scripts/<story_id>_outline.yaml (必须先有 outline)
输出: scripts/<story_id>.md (人类可读全文)
      └→ 后续调用 chapter_writer.write_all_chapters(sync_from_md=True)
         把 .md 同步成 segments.yaml,再走 doctor 审稿。

工作流:
  - 加载 outline,列出所有 chapter
  - 用户选一章(或 LLM 引导用户从 hook 章开始)
  - 进入"章节级对话":
      * 如果该章 .md 已有正文 → 用户对正文提改进意见
      * 如果该章是空白 → LLM 先问关键情节点,再写初稿
  - 对话采纳的改动直接改 self.state["chapters_md"][ch_id]
  - 用户 /done 或 cli 退出后,save_state 把所有章节合并写回 .md
  - cli 退出后调 chapter_writer 同步成 segments.yaml + 跑 doctor
"""

import json
import re
from copy import deepcopy
from pathlib import Path
from typing import Optional

from .base import Facet, Suggestion, Modification,get_scripts_dir


# CHAPTER_RUBRIC 已废弃:v2.4.7 改为从 narrative_profile 动态读取。
# 章节正文评估的"通用维度"(调子/节奏/人物动作/视觉钩子/对话密度)对
# 多数题材都成立,只是侧重不同。我们这里用一份"通用框架",再加上
# theme-specific 的额外提醒(从 narrative.story_safety + visual_hook_focus)。
_CHAPTER_RUBRIC_COMMON = """
评估一章正文时,你看这 5 个维度:

1. 调子 (tone) - 是否匹配 outline 里指定的 tone? 题材应有的氛围是否到位?
2. 节奏 (pacing) - hook 章铺垫不拖、climax 章有"安静瞬间"、其他章节张弛得当
3. 人物动作 (character action) - 角色行为是否符合 outline 里的人设?台词语气符合身份?
4. 视觉钩子 (visual hooks) - 段落有没有给镜头大师可拍的具体画面? 抽象描写过多不利后续生图
5. 对话密度 (dialogue density) - 对话和叙述比例合理? 对话是否推进情节、而非纯闲聊?
"""


def _chapter_rubric_text(state: dict) -> str:
    """根据 outline 的 theme_id 生成章节评估 rubric。"""

    from long_writer.narrative_profile import load_narrative
    outline = state.get("outline", {}) or {}
    theme_id = outline.get("theme_id", "default")
    n = load_narrative(theme_id)

    extra = []
    if n.visual_hook_focus:
        extra.append(f"题材视觉指引: {n.visual_hook_focus}")
    if n.story_safety:
        extra.append(f"题材安全约束: {n.story_safety}")
    if n.forbidden_tones:
        extra.append(f"严禁出现的 tone: {' / '.join(n.forbidden_tones)}")

    extra_block = ""
    if extra:
        extra_block = "\n【题材特殊提醒】\n" + "\n".join(f"  - {e}" for e in extra)

    return _CHAPTER_RUBRIC_COMMON + extra_block


# ════════════════════════════════════════════════════════════════
# ChapterFacet
# ════════════════════════════════════════════════════════════════

class ChapterFacet(Facet):
    name = "chapter"
    description = "章节正文的对话式精炼"

    # 初始化参数(在 cli 调 run 之前 set)
    only_chapter: Optional[str] = None   # 只精修某一章 "ch01" / None 全章

    # ─── 状态 IO ──────────────────────────────────

    def load_state(self, source_path: Optional[Path]) -> dict:
        """source_path 必须是 outline.yaml 路径。"""
        if not source_path:
            raise ValueError("chapter facet 必须传入 outline.yaml 路径")

        import yaml
        with open(source_path, "r", encoding="utf-8") as f:
            outline = yaml.safe_load(f)


        story_id = outline["story_id"]
        md_path = get_scripts_dir() / f"{story_id}.md"

        # 读取已有的 md(如果有)
        chapters_md = {}
        if md_path.exists():
            text = md_path.read_text(encoding="utf-8")
            # 按 "## " 切章节
            sections = re.split(r"^## ", text, flags=re.MULTILINE)
            for sec in sections[1:]:   # 第 0 段是标题前的内容
                lines = sec.split("\n", 1)
                if len(lines) < 2:
                    continue
                header = lines[0].strip()
                body = lines[1].strip()
                # 取 ch_id 从 outline 里匹配
                # header 通常是 "ch01 [hook] eerie 28字  青铜眨眼" 这种格式
                m = re.match(r"^(ch\d+)\b", header)
                if m:
                    chapters_md[m.group(1)] = body

        return {
            "outline": outline,
            "story_id": story_id,
            "chapters_md": chapters_md,
            # 当前精修哪一章:None=尚未选定 / "ch01" 等
            "current_chapter_id": self.only_chapter,
        }

    def save_state(self, state: dict, story_id: str) -> Path:
        """把所有章节合并写回 scripts/<story_id>.md(跟 chapter_writer 契约一致)。"""
        from .base import get_scripts_dir
        outline = state["outline"]
        chapters_md = state["chapters_md"]
        scripts_dir = get_scripts_dir()  # ← 替换
        md_path = scripts_dir / f"{story_id}.md"

        lines = [f"# {outline['title']}", "", outline.get("premise", ""), ""]
        for ch in outline.get("chapters", []):
            ch_id = ch.get("id", "")
            header = (f"{ch_id} [{ch.get('arc_role', '?')}] "
                      f"{ch.get('tone', '?')} "
                      f"{ch.get('target_words', '?')}字  "
                      f"{ch.get('title', '?')}")
            lines.append(f"## {header}")
            lines.append("")
            body = chapters_md.get(ch_id, "").strip()
            if body:
                lines.append(body)
            else:
                lines.append(f"(尚未写作)")
            lines.append("")

        md_path.write_text("\n".join(lines), encoding="utf-8")
        return md_path

    # ─── 序列化 ────────────────────────────────────

    def serialize_state(self, state: dict) -> str:
        """给 LLM 看的当前章节进度。"""
        outline = state["outline"]
        chapters_md = state["chapters_md"]
        curr = state.get("current_chapter_id")

        lines = []
        lines.append(f"故事: {outline['title']}")
        lines.append(f"钩子: {outline.get('premise', '')}")
        lines.append("")
        lines.append("章节进度:")
        for ch in outline.get("chapters", []):
            ch_id = ch.get("id", "")
            has = "✓" if chapters_md.get(ch_id) else "✗"
            mark = " ← 正在精修" if ch_id == curr else ""
            lines.append(f"  {has} {ch_id} [{ch.get('arc_role')}] "
                         f"{ch.get('target_words')}字  "
                         f"{ch.get('title')}{mark}")

        if curr and chapters_md.get(curr):
            lines.append("")
            lines.append(f"────  {curr} 当前正文  ────")
            lines.append(chapters_md[curr])
            lines.append("──────────────────────")

        return "\n".join(lines)

    def state_summary(self, state: dict) -> str:
        chapters_md = state["chapters_md"]
        outline = state["outline"]
        n_chapters = len(outline.get("chapters", []))
        n_written = sum(1 for ch in outline.get("chapters", [])
                        if chapters_md.get(ch.get("id", "")))
        return (f"  故事: {outline['title']}\n"
                f"  进度: {n_written}/{n_chapters} 章已写")

    # ─── prompt 构造 ──────────────────────────────

    def build_system_prompt(self, state: dict) -> str:
        outline = state["outline"]
        curr = state.get("current_chapter_id")

        ctx = self.serialize_state(state)
        ch_info = ""
        if curr:
            ch = next((c for c in outline.get("chapters", [])
                       if c.get("id") == curr), None)
            if ch:
                ch_info = f"\n当前正在精修: {curr}\n  arc_role: {ch.get('arc_role')}\n  tone: {ch.get('tone')}\n  目标字数: {ch.get('target_words')}\n  章节梗概: {ch.get('synopsis', '')}\n"

        # v2.4.7: identity 等都按 theme 从 narrative_profile 注入

        from long_writer.narrative_profile import load_narrative
        n = load_narrative(outline.get("theme_id", "default"))

        return f"""{n.identity}。你正在跟创作者讨论某一章的正文。
风格基准: {n.benchmark}
节奏指引: {n.pacing_guide}

{_chapter_rubric_text(state)}

你的工作:
  1. 根据 outline 和章节标记,帮创作者把这一章写得更好
  2. 写作时严格遵守章节的 arc_role / tone / 目标字数
  3. 如果用户要求"改某段","加对话"等,给出具体修改建议
  4. 修改 chapter 正文时,proposed_modification.ops 中:
       field 用 "chapters_md.<ch_id>" (整章替换)
       action 用 "set"
       value 是新的章节全文(中文,markdown 自由,但不含 "## " 标题)

约束:
  - 一次只动一章,不要同时改多章
  - 改动后正文必须接近目标字数(±20%)
  - 保留 outline 里的人名/地名/关键道具

{ctx}
{ch_info}
"""

    def build_suggestions_prompt(self, state: dict) -> str:
        outline = state["outline"]
        chapters_md = state["chapters_md"]
        curr = state.get("current_chapter_id")

        if not curr:
            # 还没选定章节 → 建议下一步该写哪章
            return f"""创作者要开始写章节正文了。
故事大纲:
{outline.get('title')}
{outline.get('premise')}

章节列表:
{chr(10).join(f"  {ch.get('id')} [{ch.get('arc_role')}] {ch.get('target_words')}字  {ch.get('title')}" for ch in outline.get('chapters', []))}

已写:{', '.join(k for k, v in chapters_md.items() if v) or '无'}

建议 3 条创作者应该考虑的下一步。每条 JSON 格式:
{{"label": "建议标题", "rationale": "为什么这样做", "focus_area": "ch01"}}

只返回 JSON 数组。"""

        # 已选定章节,看正文给建议
        body = chapters_md.get(curr, "")
        ch = next((c for c in outline.get("chapters", [])
                   if c.get("id") == curr), {})

        return f"""你正在审视一章正文,从下面 5 个维度找 3 条最值得改的点。

{_chapter_rubric_text(state)}

章节信息:
  arc_role: {ch.get('arc_role')}
  tone: {ch.get('tone')}
  目标字数: {ch.get('target_words')}
  当前字数: {len(body)}

正文:
─────────────────────
{body if body else '(尚未写作)'}
─────────────────────

请用 JSON 数组返回 3 条建议,格式同 outline facet。
只返回 JSON,不要其他文字。"""

    def parse_suggestions(self, raw: str) -> list[Suggestion]:
        cleaned = raw.strip()
        if cleaned.startswith("```"):
            lines = cleaned.split("\n")
            cleaned = "\n".join(lines[1:-1] if lines[-1].startswith("```")
                               else lines[1:])
        try:
            data = json.loads(cleaned)
        except json.JSONDecodeError:
            return []
        return [Suggestion(
            label=d.get("label", "")[:30],
            rationale=d.get("rationale", ""),
            focus_area=d.get("focus_area", ""),
        ) for d in data]

    # ─── apply_modification ───────────────────────

    _ALLOWED_FIELD_PREFIXES = ("chapters_md.", "current_chapter_id")

    def apply_modification(self, state: dict, modif: Modification) -> dict:
        new_state = deepcopy(state)
        for op in modif.ops:
            action = op.get("action", "set")
            field = op.get("field", "")
            value = op.get("value")

            if not any(field.startswith(p) for p in self._ALLOWED_FIELD_PREFIXES):
                raise ValueError(
                    f"字段 {field!r} 不在 chapter facet 允许的修改范围内")

            if field == "current_chapter_id":
                new_state["current_chapter_id"] = value
            elif field.startswith("chapters_md."):
                ch_id = field[len("chapters_md."):]
                if action == "set":
                    new_state["chapters_md"][ch_id] = value
                elif action == "delete":
                    new_state["chapters_md"].pop(ch_id, None)
                else:
                    raise ValueError(f"action {action!r} 不适用于 chapters_md")
            else:
                raise ValueError(f"未识别的 field {field!r}")

        return new_state

    # ─── 入口提示 ─────────────────────────────────

    def initial_prompt_for_user(self, state: dict) -> str:
        outline = state["outline"]
        chapters_md = state["chapters_md"]
        curr = state.get("current_chapter_id")
        n_written = sum(1 for ch in outline.get("chapters", [])
                        if chapters_md.get(ch.get("id", "")))
        n_total = len(outline.get("chapters", []))

        if curr:
            ch = next((c for c in outline.get("chapters", [])
                       if c.get("id") == curr), None)
            has_body = bool(chapters_md.get(curr))
            if has_body:
                return f"""我们要精修 {curr}「{ch.get('title')}」。
已有 {len(chapters_md.get(curr, ''))} 字正文,目标 {ch.get('target_words')} 字。

你想从哪里开始? 比如:
  - "整体看一下,有什么大问题?"
  - "第 X 段不满意,帮我改"
  - "节奏太慢/太快"
  - "对白不像 [角色名]"
或者直接告诉我具体诉求。"""
            return f"""我们要写 {curr}「{ch.get('title')}」。
arc_role={ch.get('arc_role')}, tone={ch.get('tone')}, 目标 {ch.get('target_words')} 字。

在动笔前,我想跟你聊几个关键点:
  1. 这一章主要发生的事件是什么? (从 synopsis 看是: {ch.get('synopsis', '?')})
  2. 章节开头要不要直接进入紧张? 还是先铺一段环境?
  3. 这一章要留什么悬念给下一章?

你想从哪个开始聊?"""

        # 没指定章节
        return f"""故事 "{outline['title']}" 有 {n_total} 章,已写 {n_written} 章。

你想:
  1. 从头开始,先写 ch01
  2. 跳到某一章("我们先写 ch03")
  3. 整体看现有章节哪里要改

告诉我你想做什么。"""
