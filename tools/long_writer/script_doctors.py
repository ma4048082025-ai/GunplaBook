"""
script_doctors.py ── 编剧大师（v2.4 智能体重构）
==================================================
v2.4 相对 v2.3 的核心变化（四步合一）：

  Step 1 ── 元守则（DOCTOR_META_PRINCIPLES）
    所有 D 层 doctor prompt 共享一段"医师守则"，
    明确"默认不改、克制、看到锚点先停手"。
    根本上抑制"为修订而修订"的倾向。

  Step 2 ── severity + confidence 字段 + 修改预算
    每个 patch 必须自带：
      severity:   critical | minor | cosmetic
      confidence: 0.0 - 1.0
    应用前按阈值过滤；每章每 doctor 有 PATCH_QUOTA。
    低置信度的修订不应用，只写入 review queue。

  Step 3 ── 章级 context probe（_probe_chapter_context）
    每章在跑 D 层 doctor 之前，先做一次"章级体检"：
    LLM 自己读本章前 1500 字，识别：
      - 主时空 / 嵌套时空（戏中戏）
      - 视角人物
      - 角色关系
      - 风格基调
      - 锚点（不可改的字符串）
    取代了 v2.3 那段"if 民国 in title"的硬编码 era 推断。
    任何"双时空、穿越、戏中戏"作品都能正确识别。

  Step 4 ── shared_findings（doctor 间会诊）
    每个 doctor 跑完后把识别到的 anchor 累积到共享对象，
    后跑的 doctor 看得到前面医生的发现，
    apply 时检查 patch 是否破坏任何累积的锚点 → 自动拒绝。

接口完全兼容 v2.3：
  run_all_doctors(chapters_data, outline,
                   enabled_doctors=None,
                   enable_structural=True,
                   story_id=None,
                   only_chapter=None) -> (revised_chapters, revision_log)

chapter_writer.py 那边一行不用动。
"""

import copy
import hashlib
import json
import re
import time
from pathlib import Path
from typing import Optional


SCRIPTS_DIR = Path("scripts")

DOCTOR_PROMPT_VERSION = "v2.4"


# ════════════════════════════════════════════════════════════════
# 配置：阈值、配额、字段白名单
# ════════════════════════════════════════════════════════════════

# 每个 doctor 每章最多应用的 patch 数（按 severity 排序，超出者截断）
PATCH_QUOTA = {
    "continuity": 5,
    "logic":      3,
    "rhythm":     5,
    "dialogue":   8,
}

# 低于此 confidence 的 patch 不应用
MIN_CONFIDENCE = 0.7

# severity 排序权重
_SEVERITY_RANK = {"critical": 3, "minor": 2, "cosmetic": 1}

# 默认应用门槛：cosmetic 级别的修订不自动应用，只记录到 review queue
MIN_SEVERITY_TO_APPLY = "minor"


D_LAYER_FIELDS = {
    "body.replace_text",
    "body.replace_quote",
}
A_LAYER_FIELDS = {
    "chapters[N].body.move_paragraph",
    "chapters[N].body.merge_paragraphs",
    "chapters[N-1].body.append",
    "chapters[N].body.prepend",
}

DOCTOR_FIELD_WHITELIST = {
    "continuity": D_LAYER_FIELDS,
    "logic":      D_LAYER_FIELDS,
    "rhythm":     D_LAYER_FIELDS,
    "dialogue":   D_LAYER_FIELDS,
    "structural": A_LAYER_FIELDS,
}


# ════════════════════════════════════════════════════════════════
# LLM 调用工具
# ════════════════════════════════════════════════════════════════

def _call_llm(prompt: str, temperature: float = 0.2,
              model: Optional[str] = None) -> tuple:
    """返回 (full_text, model_name, duration_ms)。失败抛异常。"""
    from config import LLM_API_KEY, LLM_BASE_URL, LLM_MODEL
    from langchain_openai import ChatOpenAI
    from langchain_core.messages import HumanMessage

    use_model = model
    if not use_model:
        try:
            from config import LLM_REVIEW_MODEL
            use_model = LLM_REVIEW_MODEL or LLM_MODEL
        except ImportError:
            use_model = LLM_MODEL

    start = time.time()
    llm = ChatOpenAI(model=use_model, api_key=LLM_API_KEY,
                     base_url=LLM_BASE_URL, temperature=temperature)
    full = ""
    for chunk in llm.stream([HumanMessage(content=prompt)]):
        full += chunk.content
    duration_ms = int((time.time() - start) * 1000)
    return full, use_model, duration_ms


def _extract_json(text: str) -> Optional[dict]:
    try:
        from feedback import clean_json
        text = clean_json(text)
    except Exception:
        pass

    if "```" in text:
        m = re.search(r'```(?:json)?\s*(\{.*?\})\s*```', text, re.DOTALL)
        if m:
            text = m.group(1)
        else:
            text = text.replace("```json", "").replace("```", "")

    try:
        return json.loads(text.strip())
    except json.JSONDecodeError:
        return None


# ════════════════════════════════════════════════════════════════
# Step 1 ── 医师元守则（注入到所有 D 层 prompt）
# ════════════════════════════════════════════════════════════════

DOCTOR_META_PRINCIPLES = """【医师守则——优先于具体职责】
1. 你是审稿员，不是改稿员。原文若已自洽（人物清楚、情节通顺、读者读得懂），就**不要修改**。
2. 修改的默认动作是"不改"。只有当原文存在明确硬伤（违背设定 / 逻辑断裂 / 明显违和），才出 patch。
3. 风格、口味、文采上的差异不是错误。作者用"小林"是亲切，用"翌日"是文气——这是**选择**，不是病。
4. 看到陌生的专有名词、数字、年份、地名，**默认它是作者刻意安排的锚点**，不要碰。
5. 当你拿不准是不是错时，写进 issues（提醒），不要写进 patches（修改）。
6. 改之前先问自己：这一改，是让故事更准确，还是只是让文字更平均？后者就别改。

【本章语境（章首扫描结果，已由独立 probe 模型识别）】
{context_block}

【先前医生已识别的锚点（绝对不可在 after 中消失或改写）】
{prior_anchors_block}

【先前医生发现的关键问题（你不必重复修订相同问题）】
{prior_findings_block}

【输出约束】
- 每个 patch 必须包含字段：severity（critical/minor/cosmetic）+ confidence（0.0-1.0）。
- 不要输出 cosmetic 级别 + confidence < 0.7 的 patch（脚本会自动过滤）。
- 本 doctor 总 patch 数不超过 {quota} 处；多于此数请按重要性精挑前 {quota} 项。
- 修订后保留原 anchors 完整（before 中出现的 anchor 必须在 after 中原样保留）。
"""


def _render_context_block(ctx: dict) -> str:
    """把 chapter_context dict 渲染成给 doctor 看的多行文本"""
    if not ctx:
        return "（章级体检失败或被跳过，请按通用原则审稿，对不熟悉的元素保持克制）"
    lines = []
    if ctx.get("scene_era"):
        lines.append(f"  - 主时空：{ctx['scene_era']}")
    if ctx.get("embedded_era"):
        lines.append(f"  - 嵌套时空：{ctx['embedded_era']}（仅在引号内的戏文/回忆/嵌套叙事中生效；其他地方按主时空处理）")
    if ctx.get("narrator_pov"):
        lines.append(f"  - 视角人物：{ctx['narrator_pov']}")
    if ctx.get("main_relations"):
        lines.append(f"  - 角色关系：{ctx['main_relations']}")
    if ctx.get("register"):
        lines.append(f"  - 风格基调：{ctx['register']}")
    return "\n".join(lines) or "（无识别结果）"


def _render_anchors_block(anchors: list) -> str:
    if not anchors:
        return "  （暂无）"
    return "\n".join(f"  - {a}" for a in anchors[:20])


def _render_prior_findings_block(findings: list) -> str:
    if not findings:
        return "  （无）"
    lines = []
    for f in findings[:8]:
        d = f.get("doctor", "?")
        s = f.get("summary", "")[:80]
        lines.append(f"  - [{d}] {s}")
    return "\n".join(lines)


# ════════════════════════════════════════════════════════════════
# Step 3 ── 章级 context probe
# ════════════════════════════════════════════════════════════════

CONTEXT_PROBE_PROMPT = """你是叙事语境分析器。读下面这一章的开头部分，识别本章的语境信息。
**只识别，不评论，不修订。**

【你要识别的内容】
1. scene_era      ── 本章**主时空**（具体到年代/地区，例："1992年北京"、"民国上海法租界"、"近未来"）
2. embedded_era   ── 是否有**嵌套时空**？戏中戏、回忆段、梦境、文献引用——这些可能用不同的语言风格。
                     例："牡丹亭戏文（明代南曲风）"。如果没有，填 null。
3. narrator_pov   ── 主要视角人物（姓名 + 一两个字身份描述，例："林明远（考古队员）"）
4. main_relations ── 主要角色之间的社会关系（例："队员间为同事，关系亲近，使用现代口语昵称"）
5. register       ── 整体风格基调（例："现代写实+少量古典戏文"、"民国白话夹文言"、"赛博朋克冷硬"）
6. anchors        ── 不可改的字符串列表。**这是最重要的输出**。
                     包括但不限于：
                     - 具体年份/日期（"1992.10.15"、"光绪三十年"）
                     - 角色之间的特定称呼（"小林"、"老张头"、"杜探长"）
                     - 专有地名（"魏公村"、"霞飞路31号"）
                     - 关键道具名（"红绣鞋"、"《牡丹亭·离魂》"）
                     - 任何看起来"作者刻意安排"的字符串
                     给出 5-15 个，按重要性排序。
7. anchor_reason  ── 为什么这些是锚点（一句话总结，便于审计）

【判断原则】
- 主时空是"读者大部分时间所处的时空"，**不是整部作品的设定**。
- 戏中戏的语言（如戏词）是嵌套时空，主时空不变。
- anchors 只列出现在本章正文里的、肉眼可见的具体字符串，不要列抽象概念。

【输出】严格 JSON（不要 markdown 代码块、不要解释）：
{{
  "scene_era":      "...",
  "embedded_era":   null 或 "...",
  "narrator_pov":   "...",
  "main_relations": "...",
  "register":       "...",
  "anchors":        ["...", "...", "..."],
  "anchor_reason":  "..."
}}

═══════════════════════════════════════════════════════════════

【全片设定（仅供参考，不必照抄）】
标题：{title}
钩子：{premise}
核心概念：{concept}

【本章信息】
ID: {ch_id}    arc_role: {arc_role}    标题：{ch_title}

【本章正文（前 1500 字）】
{body_head}

只返回 JSON。"""


def _probe_chapter_context(chapter: dict, outline: dict,
                            cache_dir: Path,
                            log_archive=None) -> dict:
    """
    章级体检——让 LLM 读本章开头，识别语境信息。
    返回 chapter_context dict（即使失败也返回最小可用的 dict）。
    """
    body = chapter.get("body", "") or ""
    if not body.strip():
        return {}

    # 缓存
    sig = "|".join([
        f"version={DOCTOR_PROMPT_VERSION}",
        "stage=probe",
        f"ch_id={chapter.get('id', '')}",
        f"body_hash={hashlib.md5(body.encode('utf-8')).hexdigest()[:8]}",
    ])
    cache_key = hashlib.md5(sig.encode("utf-8")).hexdigest()[:12]
    cache_file = cache_dir / f"{chapter.get('id', 'noid')}_probe_{cache_key}.json"

    if cache_file.exists():
        try:
            cached = json.loads(cache_file.read_text(encoding="utf-8"))
            if isinstance(cached, dict):
                cached["_cached"] = True
                return cached
        except Exception:
            pass

    prompt = CONTEXT_PROBE_PROMPT.format(
        title=outline.get("title", ""),
        premise=outline.get("premise", "")[:200],
        concept=outline.get("concept", "")[:300],
        ch_id=chapter.get("id", ""),
        arc_role=chapter.get("arc_role", ""),
        ch_title=chapter.get("title", ""),
        body_head=body[:1500],
    )

    try:
        raw, model, duration_ms = _call_llm(prompt, temperature=0.0)
    except Exception as e:
        return {"_skipped": f"probe_llm_error: {e}"}

    parsed = _extract_json(raw) or {}

    # 兜底：保证字段存在
    ctx = {
        "scene_era":      parsed.get("scene_era") or "",
        "embedded_era":   parsed.get("embedded_era"),
        "narrator_pov":   parsed.get("narrator_pov") or "",
        "main_relations": parsed.get("main_relations") or "",
        "register":       parsed.get("register") or "",
        "anchors":        [str(a) for a in (parsed.get("anchors") or []) if a],
        "anchor_reason":  parsed.get("anchor_reason") or "",
    }

    if log_archive:
        try:
            log_archive.write(
                stage="doctor.probe",
                chapter_id=chapter.get("id"),
                input={"body_preview": body[:200]},
                output={"context": ctx},
                decision={"n_anchors": len(ctx["anchors"])},
                model=model,
                duration_ms=duration_ms,
            )
        except Exception:
            pass

    try:
        cache_file.write_text(json.dumps(ctx, ensure_ascii=False, indent=2),
                              encoding="utf-8")
    except Exception:
        pass

    return ctx


# ════════════════════════════════════════════════════════════════
# Step 4 ── SharedFindings：跨 doctor 共享发现
# ════════════════════════════════════════════════════════════════

class SharedFindings:
    """
    跨 doctor 共享的发现池。
    - per_chapter_context: 每章一份 chapter_context（来自 probe）
    - chapter_anchors: 每章累积的锚点（probe + 各 doctor 的 issues 中识别）
    - chapter_findings: 每章每 doctor 的简短摘要（防止后续 doctor 重复修订）
    """

    def __init__(self):
        self.per_chapter_context: dict = {}
        self.chapter_anchors: dict = {}
        self.chapter_findings: dict = {}

    def set_context(self, ch_id: str, ctx: dict):
        self.per_chapter_context[ch_id] = ctx or {}
        # probe 识别的 anchors 默认进入累积池
        self.chapter_anchors.setdefault(ch_id, [])
        for a in (ctx or {}).get("anchors", []):
            if a and a not in self.chapter_anchors[ch_id]:
                self.chapter_anchors[ch_id].append(a)

    def get_context(self, ch_id: str) -> dict:
        return self.per_chapter_context.get(ch_id, {})

    def get_anchors(self, ch_id: str) -> list:
        return list(self.chapter_anchors.get(ch_id, []))

    def add_finding(self, ch_id: str, doctor: str,
                    issues: list, new_anchors: Optional[list] = None):
        """记录某 doctor 的发现摘要 + 它额外识别的锚点"""
        # 累积 anchors
        if new_anchors:
            ch_anchors = self.chapter_anchors.setdefault(ch_id, [])
            for a in new_anchors:
                if a and a not in ch_anchors:
                    ch_anchors.append(a)
        # 累积 findings
        summary = ""
        if issues:
            top = issues[0] if isinstance(issues, list) else {}
            summary = (top.get("description") or "")[:80]
        self.chapter_findings.setdefault(ch_id, []).append({
            "doctor": doctor, "summary": summary,
        })

    def get_findings(self, ch_id: str) -> list:
        return list(self.chapter_findings.get(ch_id, []))

    def all_anchors_dict(self) -> dict:
        """给 structural doctor 用：返回 {ch_id: [anchors]}"""
        return {k: list(v) for k, v in self.chapter_anchors.items()}


# ════════════════════════════════════════════════════════════════
# Doctor 1: continuity ── 一致性
# ════════════════════════════════════════════════════════════════

CONTINUITY_PROMPT = """你是叙事一致性审稿员。对比【本章正文】和【全片设定】，找出人物、地点、时间、关键道具的不一致。

{meta_principles}

【你的职责】
1. 人物名称：本章中的角色名是否与大纲/前章一致？
2. 地点：地点描述是否前后一致？
3. 时间：时间线是否合理？
4. 关键道具：前章出现过的道具，本章描述是否吻合？
5. 角色身份：身份描述前后是否一致？

【你不该管的】
- 句式优美程度（rhythm doctor 管）
- 因果逻辑（logic doctor 管）
- 台词自然度（dialogue doctor 管）

【输出格式】严格 JSON（不要 markdown 代码块）：
{{
  "issues": [
    {{
      "type": "name_mismatch | location_mismatch | time_inconsistent | prop_inconsistent | role_mismatch",
      "description": "具体问题描述（中文，30字内）",
      "anchor_candidate": "（可选）本条问题中涉及的、应当作为锚点保护的字符串"
    }}
  ],
  "patches": [
    {{
      "field": "body.replace_text",
      "before": "本章中错误的原文片段（10-30 字精确串）",
      "after":  "修订后的文字",
      "reason": "简短理由（30字内）",
      "severity":   "critical | minor | cosmetic",
      "confidence": 0.0
    }}
  ]
}}

只允许 field=body.replace_text。修订后字数应与修订前接近。
没问题返回 {{"issues": [], "patches": []}}。

═══════════════════════════════════════════════════════════════

【全片设定】
标题：{title}
人物：
{characters_block}
章节列表：
{chapters_outline}

【已写章节摘要（用作上下文）】
{prev_chapters_summary}

【本章】
ID: {ch_id}  arc_role: {arc_role}  tone: {tone}
标题：{ch_title}
正文：
{body}

只返回 JSON。"""


def _doctor_continuity(chapter, outline, prev_chapters_summary,
                       chapter_context, prior_anchors, prior_findings,
                       quota):
    chars_block = "\n".join(
        f"  - {c['name']}: {c.get('desc', '')}"
        for c in outline.get("characters", [])[:6]
    )
    chapters_outline = "\n".join(
        f"  {c['id']} [{c.get('arc_role', '?')}] {c['title']}: {c.get('summary', '')[:80]}"
        for c in outline.get("chapters", [])[:12]
    )

    meta = DOCTOR_META_PRINCIPLES.format(
        context_block=_render_context_block(chapter_context),
        prior_anchors_block=_render_anchors_block(prior_anchors),
        prior_findings_block=_render_prior_findings_block(prior_findings),
        quota=quota,
    )

    return CONTINUITY_PROMPT.format(
        meta_principles=meta,
        title=outline.get("title", ""),
        characters_block=chars_block,
        chapters_outline=chapters_outline,
        prev_chapters_summary=prev_chapters_summary[:1500] if prev_chapters_summary else "（本章为首章）",
        ch_id=chapter.get("id", ""),
        arc_role=chapter.get("arc_role", ""),
        tone=chapter.get("tone", "tension"),
        ch_title=chapter.get("title", ""),
        body=chapter.get("body", "")[:4000],
    )


# ════════════════════════════════════════════════════════════════
# Doctor 2: logic ── 逻辑通顺
# ════════════════════════════════════════════════════════════════

LOGIC_PROMPT = """你是叙事逻辑审稿员。检查本章正文是否存在因果断裂、逻辑跳跃或自相矛盾。

{meta_principles}

【你的职责】
1. 因果断裂：A 发生了，B 紧接着发生，但 B 不是 A 的合理后果（缺过渡）
2. 时空跳跃：前一句在 X 地，下一句在 Y 地但没交代怎么过去的
3. 自相矛盾：本章前面说"门是关的"，后面说"他从开着的门进去"
4. 动作不合理：人物动作违反物理或常识
5. 信息泄露：还没揭秘的信息被提前说出

【你不该管的】
- 人名/地名一致性（continuity 管）
- 句子优美度（rhythm 管）
- 台词自然度（dialogue 管）

【输出格式】严格 JSON：
{{
  "issues": [
    {{
      "type": "causal_break | spatial_jump | self_contradiction | implausible_action | info_leak",
      "description": "...",
      "anchor_candidate": "（可选）"
    }}
  ],
  "patches": [
    {{
      "field": "body.replace_text",
      "before": "原文片段（10-30 字精确串）",
      "after":  "修订后",
      "reason": "...",
      "severity":   "critical | minor | cosmetic",
      "confidence": 0.0
    }}
  ]
}}

修订原则：
- 优先添加过渡句而非删除内容
- 因果断裂用"于是""之所以""恰好"等连接词
- 时空跳跃可加一句过渡描写
- 字数变化不超过 ±15%

═══════════════════════════════════════════════════════════════

【本章】
ID: {ch_id}  arc_role: {arc_role}
标题：{ch_title}
正文：
{body}

只返回 JSON。"""


def _doctor_logic(chapter, outline, prev_chapters_summary,
                  chapter_context, prior_anchors, prior_findings,
                  quota):
    meta = DOCTOR_META_PRINCIPLES.format(
        context_block=_render_context_block(chapter_context),
        prior_anchors_block=_render_anchors_block(prior_anchors),
        prior_findings_block=_render_prior_findings_block(prior_findings),
        quota=quota,
    )
    return LOGIC_PROMPT.format(
        meta_principles=meta,
        ch_id=chapter.get("id", ""),
        arc_role=chapter.get("arc_role", ""),
        ch_title=chapter.get("title", ""),
        body=chapter.get("body", "")[:4000],
    )


# ════════════════════════════════════════════════════════════════
# Doctor 3: rhythm ── 节奏 / 冗余
# ════════════════════════════════════════════════════════════════

RHYTHM_PROMPT = """你是文字节奏审稿员。检查本章正文是否有冗余、重复或节奏失衡。

{meta_principles}

【你的职责】
1. 冗余表达："她哭了。她流着眼泪。她抽泣着。"——三句表达同一意思
2. 形容词堆叠："巨大的、狰狞的、恐怖的、丑陋的怪兽"——形容词过多
3. 节奏失衡：开篇 200 字内还在描写环境（hook 章节应立刻出悬念）
4. 关键时刻拖沓：高潮戏用了平淡叙述，应该短句
5. 旁白与对话比例失衡：单段对话过长（超过 100 字一段）

【你不该管的】
- 因果逻辑（logic 管）
- 人物一致（continuity 管）
- 台词年代/身份（dialogue 管）

【特别提醒】
风格选择不是冗余。作者用"翌日"是文气、用"雪白的"是描写——这是**口味**，不是病。
只有真正的重复、真正多余的形容词、真正的密度失衡才出 patch。

【输出格式】严格 JSON：
{{
  "issues": [
    {{ "type": "redundancy | adj_pile | pacing_off | climax_sag | dialogue_block",
       "description": "...",
       "anchor_candidate": "（可选）" }}
  ],
  "patches": [
    {{
      "field": "body.replace_text",
      "before": "原文冗余片段",
      "after":  "精简后",
      "reason": "...",
      "severity":   "critical | minor | cosmetic",
      "confidence": 0.0
    }}
  ]
}}

修订原则：
- 删冗余：3 句变 1 句
- 高潮戏：长句切短句
- 总字数变化不超过 ±15%

═══════════════════════════════════════════════════════════════

【本章】
ID: {ch_id}  arc_role: {arc_role}（决定节奏要求：hook 紧、setup 缓、climax 急、resolution 留白）
标题：{ch_title}
目标字数：{target_words}（本章实际：{actual_words}）
正文：
{body}

只返回 JSON。"""


def _doctor_rhythm(chapter, outline, prev_chapters_summary,
                   chapter_context, prior_anchors, prior_findings,
                   quota):
    body = chapter.get("body", "")
    meta = DOCTOR_META_PRINCIPLES.format(
        context_block=_render_context_block(chapter_context),
        prior_anchors_block=_render_anchors_block(prior_anchors),
        prior_findings_block=_render_prior_findings_block(prior_findings),
        quota=quota,
    )
    return RHYTHM_PROMPT.format(
        meta_principles=meta,
        ch_id=chapter.get("id", ""),
        arc_role=chapter.get("arc_role", ""),
        ch_title=chapter.get("title", ""),
        target_words=chapter.get("target_words", 0),
        actual_words=len(body),
        body=body[:4000],
    )


# ════════════════════════════════════════════════════════════════
# Doctor 4: dialogue ── 台词自然度
# ════════════════════════════════════════════════════════════════

DIALOGUE_PROMPT = """你是台词审稿员。检查本章中所有"…"包裹的对话是否自然、符合人物身份和**当前场景的时空**。

{meta_principles}

═══════════════════════════════════════════════════════════════
⚠️ v2.3.3 关键限制：dialogue 医生只修"错误"，不修"风格"
═══════════════════════════════════════════════════════════════
你是修错的医生，不是改稿的编辑。作者的语言节奏、标点选择、口语习惯，
是他的风格，不是你的修改对象。

✅ 可以改（属于"错误"）：
  - 错别字、拼音错误、不通顺的语法
  - 角色身份明显错误的台词（古代人说"OK"、清朝人说"加油"）
  - 跟前文事实矛盾的台词（前文说他不识字，这里他朗读古文）
  - 时空错位的称谓（民国戏中说"亲"，现代戏中说"在下"）

❌ 严禁改（属于"风格"）：
  - 标点选择：省略号、破折号、感叹号——这是节奏
  - 语气词、口头禅、结巴重复——这是性格
  - 句子长短、断句方式——这是腔调
  - 任何理由是"X 比 Y 更自然/更可能/更简洁/更符合常理"的改动

⚠️ 自检：如果你想改的理由是这几种之一，**立即停止**：
  - "在 XX 氛围下，人更可能直接说，而不是用省略号"
  - "在紧张时，X 比 Y 更自然"
  - "去掉省略号可以让句子更流畅"
  - "工人不会这样说话"
  这些都是审美僭越，是作者的选择，不是错误。

例 1（不能改）：
  原文："周、周哥..."   "她脖子...转了！"
  ❌ 改成："周哥..." "她脖子转了！"（理由："更直接更自然"）
  这是工人受惊时的结巴，是作者刻画恐惧的手法，**保留原样**。

例 2（应该改）：
  原文：（1992 年北京）"翌日清晨，本官前去查案。"
  ✅ 改成："第二天一早，我去查案。"
  理由：1992 年的中国人不会说"翌日""本官"——这是时空错位，是错误。

═══════════════════════════════════════════════════════════════

【你的职责】
1. 时空违和：本章【主时空】下出现了不该有的语汇
   - 例：现代戏中说话像"翌日"、"罢了"
   - 例：民国戏中说"OK"、"加油"
2. 身份违和：探长说话像现代警察、书生说话像现代大学生
3. 信息密度异常：单句台词信息过多到反常（不是单句太长就改，是反常才改）
4. 称呼错误：违反【本章语境】中给出的"角色关系"

【关键提醒（避免误诊）】
- **嵌套时空**（戏中戏、回忆段、文献引用）的语言风格遵循它**自己**的年代，不是主时空。
  例：1992 年北京背景下，戏台上唱的《牡丹亭》戏文用明代南曲风**完全正确**，不要去"现代化"。
- 角色之间的称呼，以【本章语境】中"角色关系"描述为准。
  亲密同事互称"小林""老张"是**自然口语**，不要改成"林先生""张先生"。
- 看到具体年份/日期、专有人名地名（即【先前医生已识别的锚点】），**绝对不要在 after 中删改**。

【你不该管的】
- 旁白叙述（rhythm/logic 管）
- 对话情节（continuity 管）
- 台词的"自然度"（这是审美，除非真的违和到错误的程度，否则不归你管）

【severity 标定（v2.3.3 收紧）】
- critical：清朝人说英语、跟前文事实矛盾——必须改
- minor：时空错位但勉强能听懂、称谓违和——值得改
- cosmetic：节奏可以更紧、用词可以更准——**这是审美僭越，不要输出**

如果你犹豫某 patch 是 minor 还是 cosmetic，统一标 cosmetic（脚本会自动丢弃）。

【输出格式】严格 JSON：
{{
  "issues": [
    {{ "type": "era_mismatch | identity_mismatch | wrong_address",
       "description": "...",
       "anchor_candidate": "（可选）" }}
  ],
  "patches": [
    {{
      "field": "body.replace_quote",
      "before": "原台词（含引号）",
      "after":  "修订后台词（含引号）",
      "reason": "...",
      "severity":   "critical | minor | cosmetic",
      "confidence": 0.0
    }}
  ]
}}

═══════════════════════════════════════════════════════════════

【本章】
ID: {ch_id}
标题：{ch_title}
正文：
{body}

只返回 JSON。"""


def _doctor_dialogue(chapter, outline, prev_chapters_summary,
                     chapter_context, prior_anchors, prior_findings,
                     quota):
    body = chapter.get("body", "")
    # 任一种引号都算有台词
    if not any(q in body for q in ('"', '"', '"', "'", "「", "『")):
        return None  # 没台词跳过

    meta = DOCTOR_META_PRINCIPLES.format(
        context_block=_render_context_block(chapter_context),
        prior_anchors_block=_render_anchors_block(prior_anchors),
        prior_findings_block=_render_prior_findings_block(prior_findings),
        quota=quota,
    )
    return DIALOGUE_PROMPT.format(
        meta_principles=meta,
        ch_id=chapter.get("id", ""),
        ch_title=chapter.get("title", ""),
        body=body[:4000],
    )


# ════════════════════════════════════════════════════════════════
# A 层 Doctor: structural ── 跨章结构编辑
# ════════════════════════════════════════════════════════════════

STRUCTURAL_PROMPT = """你是全片结构编辑。看完所有章节摘要，判断章节衔接是否流畅、节奏是否合理、有无段落需要调整顺序。

【医师守则】
1. 默认动作是"不改"。只有衔接处明显割裂，才出 patch。
2. 不动情节，不动设定，只在接缝处微调。
3. 整体修订数 ≤ 章节数的 1/3。

【全片各章语境（已由 probe 识别，不要混淆主时空）】
{per_chapter_contexts}

【全片锚点池（绝对不可改的字符串）】
{global_anchors}

【你的职责】
1. 章节衔接：相邻两章首尾是否自然
2. 节奏起伏：rising → climax → falling 是否清晰
3. 信息分布：关键悬念是否过早泄露 / 过晚交代
4. 跨章过渡：是否需要在某章末尾加一句过渡，或某章开头加一句衔接

【你不该管的】
- 句子层面（D 层 doctors 管）
- 单章内的逻辑/冗余（D 层 doctors 管）

【特别提醒】
不同章节可能在**不同时空**。给某章末尾追加过渡时，必须用**那一章的主时空**的语言风格，
不要把民国章的口吻接到 1992 章上。

【输出格式】严格 JSON：
{{
  "issues": [...],
  "patches": [
    {{
      "field": "chapters[ch01].body.append",
      "before": "",
      "after":  "在本章结尾追加的过渡句（30字内，符合本章主时空）",
      "reason": "为下一章 hook 做铺垫",
      "severity":   "critical | minor | cosmetic",
      "confidence": 0.0
    }}
  ]
}}

可用 field（A 层）：
  - chapters[chXX].body.append   章末追加（30 字内）
  - chapters[chXX].body.prepend  章首插入（30 字内）

═══════════════════════════════════════════════════════════════

【全片信息】
标题：{title}
{outline_block}

【各章摘要 + 首尾片段】
{chapters_summaries}

只返回 JSON。"""


def _doctor_structural(chapters_data, outline, shared: SharedFindings):
    chapters_summaries_lines = []
    for ch in chapters_data:
        body = ch.get("body", "")
        if not body:
            continue
        head = body[:120].replace("\n", " ")
        tail = body[-120:].replace("\n", " ")
        chapters_summaries_lines.append(
            f"【{ch['id']} [{ch.get('arc_role', '?')}] {ch.get('title', '')}】\n"
            f"  开头: {head}…\n"
            f"  结尾: …{tail}\n"
        )

    outline_block = "\n".join(
        f"  {c['id']} [{c.get('arc_role', '?')}] {c['title']}"
        for c in outline.get("chapters", [])
    )

    # 每章语境
    per_ch_lines = []
    for ch in chapters_data:
        ctx = shared.get_context(ch["id"])
        if not ctx:
            continue
        line = f"  {ch['id']}: 主时空={ctx.get('scene_era','?')}"
        if ctx.get("embedded_era"):
            line += f"; 嵌套={ctx['embedded_era']}"
        per_ch_lines.append(line)
    per_chapter_contexts = "\n".join(per_ch_lines) or "  （未识别）"

    # 全片锚点池
    all_anchors = []
    for ch_id, anchors in shared.all_anchors_dict().items():
        for a in anchors:
            label = f"{a}  [来自 {ch_id}]"
            if label not in all_anchors:
                all_anchors.append(label)
    global_anchors = _render_anchors_block(all_anchors[:30])

    return STRUCTURAL_PROMPT.format(
        title=outline.get("title", ""),
        outline_block=outline_block,
        chapters_summaries="\n".join(chapters_summaries_lines),
        per_chapter_contexts=per_chapter_contexts,
        global_anchors=global_anchors,
    )


# ════════════════════════════════════════════════════════════════
# Step 2 ── Patch 过滤器（severity / confidence / quota）
# ════════════════════════════════════════════════════════════════

def _patch_severity_rank(patch: dict) -> int:
    return _SEVERITY_RANK.get(patch.get("severity", "cosmetic"), 1)


def _filter_patches_by_threshold(patches: list, doctor: str,
                                  revision_log: list,
                                  ch_id: Optional[str] = None) -> list:
    """
    应用 severity / confidence / quota 三道过滤：
    - severity 低于 MIN_SEVERITY_TO_APPLY 的丢弃
    - confidence 低于 MIN_CONFIDENCE 的丢弃
    - 剩下的按 (severity, confidence) 倒序排序，截取前 quota 个
    被拒绝的写进 revision_log（status=rejected）
    """
    quota = PATCH_QUOTA.get(doctor, 5)
    min_rank = _SEVERITY_RANK.get(MIN_SEVERITY_TO_APPLY, 2)

    accepted = []
    for p in patches:
        if not isinstance(p, dict):
            continue
        sev = p.get("severity", "cosmetic")
        conf = p.get("confidence")
        try:
            conf = float(conf) if conf is not None else 0.0
        except (TypeError, ValueError):
            conf = 0.0

        sev_rank = _SEVERITY_RANK.get(sev, 1)
        if sev_rank < min_rank:
            revision_log.append({
                "doctor": doctor, "ch_id": ch_id,
                "field": p.get("field", ""), "status": "rejected",
                "before": p.get("before", "")[:60],
                "after":  p.get("after", "")[:60],
                "reason": f"severity={sev} 低于阈值 {MIN_SEVERITY_TO_APPLY}",
            })
            continue
        if conf < MIN_CONFIDENCE:
            revision_log.append({
                "doctor": doctor, "ch_id": ch_id,
                "field": p.get("field", ""), "status": "rejected",
                "before": p.get("before", "")[:60],
                "after":  p.get("after", "")[:60],
                "reason": f"confidence={conf:.2f} 低于阈值 {MIN_CONFIDENCE}",
            })
            continue
        accepted.append(p)

    # 按 severity 高 → confidence 高 排序，截取前 quota
    accepted.sort(
        key=lambda p: (_patch_severity_rank(p),
                       float(p.get("confidence", 0) or 0)),
        reverse=True,
    )
    if len(accepted) > quota:
        for p in accepted[quota:]:
            revision_log.append({
                "doctor": doctor, "ch_id": ch_id,
                "field": p.get("field", ""), "status": "rejected",
                "before": p.get("before", "")[:60],
                "after":  p.get("after", "")[:60],
                "reason": f"超出 {doctor} 的 patch 配额 {quota}",
            })
        accepted = accepted[:quota]

    return accepted


# ════════════════════════════════════════════════════════════════
# Step 4 ── Anchor 守门
# ════════════════════════════════════════════════════════════════

def _violates_anchor(before: str, after: str, anchors: list) -> Optional[str]:
    """
    检查 patch 是否破坏锚点：
    若 before 中出现某 anchor，但 after 中该 anchor 消失或被改写，则违规。
    返回被破坏的 anchor 字符串；否则返回 None。
    """
    if not before or not anchors:
        return None
    for a in anchors:
        if not a:
            continue
        if a in before and a not in after:
            return a
    return None


# ════════════════════════════════════════════════════════════════
# Patch 应用器
# ════════════════════════════════════════════════════════════════

def _apply_d_patches(chapter: dict, patches: list, doctor: str,
                      revision_log: list, anchors: list) -> dict:
    """
    把 D 层 patches 应用到 chapter.body。
    新增：按 anchors 守门，被破坏的 patch 自动拒绝。
    返回修订后的 chapter（深拷贝）。
    """
    ch_revised = copy.deepcopy(chapter)
    body = ch_revised.get("body", "")
    whitelist = DOCTOR_FIELD_WHITELIST.get(doctor, set())

    for patch in patches:
        if not isinstance(patch, dict):
            continue
        field = patch.get("field", "")
        before = str(patch.get("before", "")).strip()
        after = str(patch.get("after", "")).strip()
        reason = patch.get("reason", "")
        severity = patch.get("severity", "?")
        confidence = patch.get("confidence", "?")

        if field not in whitelist:
            revision_log.append({
                "doctor": doctor, "ch_id": chapter.get("id"),
                "field": field, "status": "rejected",
                "reason": f"{doctor} 不允许字段 {field}",
            })
            continue

        if not before:
            revision_log.append({
                "doctor": doctor, "ch_id": chapter.get("id"),
                "field": field, "status": "rejected",
                "reason": "before 为空",
            })
            continue

        if before not in body:
            revision_log.append({
                "doctor": doctor, "ch_id": chapter.get("id"),
                "field": field, "status": "rejected",
                "reason": f"原文中找不到 before（首40字: {before[:40]}）",
            })
            continue

        # ─── Anchor 守门 ───
        violated = _violates_anchor(before, after, anchors)
        if violated:
            revision_log.append({
                "doctor": doctor, "ch_id": chapter.get("id"),
                "field": field, "status": "rejected",
                "before": before[:60], "after": after[:60],
                "reason": f"破坏锚点：{violated}",
            })
            continue

        # 应用替换（只替换第一处）
        body = body.replace(before, after, 1)

        revision_log.append({
            "doctor": doctor, "ch_id": chapter.get("id"),
            "field": field, "status": "applied",
            "before": before, "after": after, "reason": reason,
            "severity": severity, "confidence": confidence,
        })

    ch_revised["body"] = body
    ch_revised["word_count"] = len(body)

    # 持久化修订记录到章节本身
    notes = ch_revised.setdefault("_doctor_revisions", [])
    for r in revision_log:
        if r.get("ch_id") == chapter.get("id") and r.get("status") == "applied" \
                and r.get("doctor") == doctor:
            notes.append({
                "doctor": r["doctor"],
                "field":  r["field"],
                "before": r["before"][:100],
                "after":  r["after"][:100],
                "reason": r["reason"],
                "severity":   r.get("severity"),
                "confidence": r.get("confidence"),
            })

    return ch_revised


def _apply_a_patches(chapters_data: list, patches: list,
                      revision_log: list,
                      shared: SharedFindings) -> list:
    """
    把 A 层 patches 应用到 chapters_data。
    新增：用每章的 anchors 守门 append/prepend 的内容
    （防止追加内容覆盖锚点，虽然 append/prepend 是新增不是替换，
    但仍检查 after 中是否包含 anchors 应有的"主时空"标志，做软约束）。
    """
    revised = copy.deepcopy(chapters_data)
    by_id = {c["id"]: c for c in revised}

    for patch in patches:
        if not isinstance(patch, dict):
            continue
        field = patch.get("field", "")
        after = str(patch.get("after", "")).strip()
        reason = patch.get("reason", "")
        severity = patch.get("severity", "?")
        confidence = patch.get("confidence", "?")

        # severity / confidence 过滤（A 层用同套阈值）
        sev_rank = _SEVERITY_RANK.get(severity, 1)
        try:
            conf = float(confidence) if confidence not in ("?", None) else 0.0
        except (TypeError, ValueError):
            conf = 0.0
        if sev_rank < _SEVERITY_RANK.get(MIN_SEVERITY_TO_APPLY, 2):
            revision_log.append({
                "doctor": "structural", "ch_id": None,
                "field": field, "status": "rejected",
                "reason": f"severity={severity} 低于阈值",
            })
            continue
        if conf < MIN_CONFIDENCE:
            revision_log.append({
                "doctor": "structural", "ch_id": None,
                "field": field, "status": "rejected",
                "reason": f"confidence={conf:.2f} 低于阈值",
            })
            continue

        m = re.match(r'chapters\[(\w+)\]\.body\.(append|prepend)', field)
        if not m:
            revision_log.append({
                "doctor": "structural", "ch_id": None,
                "field": field, "status": "rejected",
                "reason": "字段路径无法解析",
            })
            continue

        target_id, action = m.group(1), m.group(2)
        if target_id not in by_id:
            revision_log.append({
                "doctor": "structural", "ch_id": target_id,
                "field": field, "status": "rejected",
                "reason": f"找不到章节 {target_id}",
            })
            continue
        if not after:
            revision_log.append({
                "doctor": "structural", "ch_id": target_id,
                "field": field, "status": "rejected",
                "reason": "after 为空",
            })
            continue

        # 软守门：append/prepend 的 after 中如果出现了"其他章节专属的锚点"
        # 而不是本章自己的锚点，就拒绝（防止把民国章的过渡接到 1992 章上）
        ch_anchors_self = set(shared.get_anchors(target_id))
        ch_anchors_others = set()
        for other_id, other_anchors in shared.all_anchors_dict().items():
            if other_id == target_id:
                continue
            for a in other_anchors:
                if a not in ch_anchors_self:
                    ch_anchors_others.add(a)
        leaked = [a for a in ch_anchors_others if a in after]
        if leaked:
            revision_log.append({
                "doctor": "structural", "ch_id": target_id,
                "field": field, "status": "rejected",
                "reason": f"过渡内容混入了其他章节的锚点：{leaked[:3]}",
            })
            continue

        ch = by_id[target_id]
        body = ch.get("body", "")
        if action == "append":
            new_body = body.rstrip() + "\n\n" + after
        else:  # prepend
            new_body = after + "\n\n" + body.lstrip()
        ch["body"] = new_body
        ch["word_count"] = len(new_body)

        notes = ch.setdefault("_doctor_revisions", [])
        notes.append({
            "doctor": "structural",
            "field":  field,
            "before": "",
            "after":  after,
            "reason": reason,
            "severity":   severity,
            "confidence": confidence,
        })
        revision_log.append({
            "doctor": "structural", "ch_id": target_id,
            "field": field, "status": "applied",
            "after": after[:80], "reason": reason,
            "severity": severity, "confidence": confidence,
        })

    return revised


# ════════════════════════════════════════════════════════════════
# 缓存
# ════════════════════════════════════════════════════════════════

def _doctor_cache_key(chapter: dict, doctor: str) -> str:
    """单章 doctor 缓存 key"""
    body = chapter.get("body", "")
    sig = "|".join([
        f"version={DOCTOR_PROMPT_VERSION}",
        f"doctor={doctor}",
        f"ch_id={chapter.get('id', '')}",
        f"body_hash={hashlib.md5(body.encode('utf-8')).hexdigest()[:8]}",
    ])
    return hashlib.md5(sig.encode("utf-8")).hexdigest()[:12]


def _structural_cache_key(chapters_data: list, outline: dict) -> str:
    bodies = "|".join(c.get("body", "") for c in chapters_data)
    sig = "|".join([
        f"version={DOCTOR_PROMPT_VERSION}",
        "doctor=structural",
        f"title={outline.get('title', '')}",
        f"bodies_hash={hashlib.md5(bodies.encode('utf-8')).hexdigest()[:8]}",
    ])
    return hashlib.md5(sig.encode("utf-8")).hexdigest()[:12]


# ════════════════════════════════════════════════════════════════
# 单 doctor 执行
# ════════════════════════════════════════════════════════════════

D_LAYER_DOCTORS = {
    "continuity": _doctor_continuity,
    "logic":      _doctor_logic,
    "rhythm":     _doctor_rhythm,
    "dialogue":   _doctor_dialogue,
}

# 推荐顺序：先确定事实（continuity/logic），再修饰（rhythm/dialogue）
# 这样后跑的 doctor 能看到前面识别的锚点
RECOMMENDED_DOCTOR_ORDER = ["continuity", "logic", "rhythm", "dialogue"]


def _run_d_doctor(chapter: dict, outline: dict, doctor: str,
                   prev_chapters_summary: str,
                   shared: SharedFindings,
                   cache_dir: Path,
                   log_archive=None) -> dict:
    """跑单个 D 层 doctor"""
    ch_id = chapter.get("id", "")
    cache_key = _doctor_cache_key(chapter, doctor)
    cache_file = cache_dir / f"{ch_id}_{doctor}_{cache_key}.json"

    if cache_file.exists():
        try:
            cached = json.loads(cache_file.read_text(encoding="utf-8"))
            if isinstance(cached, dict) and "patches" in cached:
                cached["_cached"] = True
                return cached
        except Exception:
            pass

    builder = D_LAYER_DOCTORS[doctor]
    chapter_context = shared.get_context(ch_id)
    prior_anchors = shared.get_anchors(ch_id)
    prior_findings = shared.get_findings(ch_id)
    quota = PATCH_QUOTA.get(doctor, 5)

    prompt = builder(
        chapter, outline, prev_chapters_summary,
        chapter_context, prior_anchors, prior_findings, quota,
    )
    if prompt is None:
        # dialogue doctor 在没台词时返回 None
        result = {"doctor": doctor, "issues": [], "patches": [],
                  "skipped": "no_dialogue_in_body"}
        try:
            cache_file.write_text(json.dumps(result, ensure_ascii=False, indent=2),
                                  encoding="utf-8")
        except Exception:
            pass
        return result

    try:
        raw, model, duration_ms = _call_llm(prompt, temperature=0.2)
    except Exception as e:
        return {"doctor": doctor, "issues": [], "patches": [],
                "skipped": f"llm_error: {e}"}

    parsed = _extract_json(raw)
    if not parsed:
        return {"doctor": doctor, "issues": [], "patches": [],
                "skipped": "json_parse_failed",
                "raw": raw[:500]}

    result = {
        "doctor":  doctor,
        "issues":  parsed.get("issues", []) or [],
        "patches": parsed.get("patches", []) or [],
    }

    if log_archive:
        try:
            log_archive.write(
                stage=f"doctor.{doctor}",
                chapter_id=ch_id,
                input={"body_preview": chapter.get("body", "")[:300],
                       "body_len": len(chapter.get("body", "")),
                       "prior_anchors": prior_anchors[:10],
                       "context": chapter_context},
                output={"issues": result["issues"][:5],
                        "patches": result["patches"][:5]},
                decision={"n_issues": len(result["issues"]),
                          "n_patches": len(result["patches"])},
                model=model,
                duration_ms=duration_ms,
            )
        except Exception:
            pass

    try:
        cache_file.write_text(json.dumps(result, ensure_ascii=False, indent=2),
                              encoding="utf-8")
    except Exception:
        pass

    return result


def _run_structural_doctor(chapters_data: list, outline: dict,
                            shared: SharedFindings,
                            cache_dir: Path,
                            log_archive=None) -> dict:
    cache_key = _structural_cache_key(chapters_data, outline)
    cache_file = cache_dir / f"structural_{cache_key}.json"

    if cache_file.exists():
        try:
            cached = json.loads(cache_file.read_text(encoding="utf-8"))
            if isinstance(cached, dict) and "patches" in cached:
                cached["_cached"] = True
                return cached
        except Exception:
            pass

    prompt = _doctor_structural(chapters_data, outline, shared)
    try:
        raw, model, duration_ms = _call_llm(prompt, temperature=0.3)
    except Exception as e:
        return {"doctor": "structural", "issues": [], "patches": [],
                "skipped": f"llm_error: {e}"}

    parsed = _extract_json(raw)
    if not parsed:
        return {"doctor": "structural", "issues": [], "patches": [],
                "skipped": "json_parse_failed",
                "raw": raw[:500]}

    result = {
        "doctor":  "structural",
        "issues":  parsed.get("issues", []) or [],
        "patches": parsed.get("patches", []) or [],
    }

    if log_archive:
        try:
            log_archive.write(
                stage="doctor.structural",
                chapter_id=None,
                input={"n_chapters": len(chapters_data),
                       "all_anchors": shared.all_anchors_dict()},
                output={"issues": result["issues"][:5],
                        "patches": result["patches"][:5]},
                decision={"n_issues": len(result["issues"]),
                          "n_patches": len(result["patches"])},
                model=model,
                duration_ms=duration_ms,
            )
        except Exception:
            pass

    try:
        cache_file.write_text(json.dumps(result, ensure_ascii=False, indent=2),
                              encoding="utf-8")
    except Exception:
        pass

    return result


# ════════════════════════════════════════════════════════════════
# 总入口：跑全部 doctors
# ════════════════════════════════════════════════════════════════

def run_all_doctors(chapters_data: list,
                    outline: dict,
                    enabled_doctors: Optional[list] = None,
                    enable_structural: bool = True,
                    story_id: Optional[str] = None,
                    only_chapter: Optional[str] = None) -> tuple:
    """
    跑所有启用的 doctor。接口与 v2.3 完全兼容。

    chapters_data:     章节数据列表
    outline:           大纲
    enabled_doctors:   D 层启用列表，None=全开（按 RECOMMENDED_DOCTOR_ORDER 排序）
    enable_structural: 是否启用 A 层
    story_id:          用于日志归档
    only_chapter:      只跑某章

    返回 (修订后的 chapters_data, revision_log)
    """
    if enabled_doctors is None:
        enabled_doctors = list(RECOMMENDED_DOCTOR_ORDER)
    else:
        # 按推荐顺序重排（保留用户指定的子集，但顺序由我们决定）
        enabled_doctors = [d for d in RECOMMENDED_DOCTOR_ORDER if d in enabled_doctors]

    sid = story_id or outline.get("story_id", "unknown")
    cache_dir = SCRIPTS_DIR / f"_{sid}_doctor_cache"
    cache_dir.mkdir(parents=True, exist_ok=True)

    # 日志档案（兼容多种 import 路径）
    log_archive = None
    try:
        from _log_utils import LogArchive
        log_archive = LogArchive(sid)
    except ImportError:
        try:
            from ._log_utils import LogArchive
            log_archive = LogArchive(sid)
        except Exception:
            pass
    except Exception:
        pass

    revision_log = []
    revised_chapters = list(chapters_data)
    shared = SharedFindings()

    print(f"\n{'='*55}")
    print(f"  编剧大师 {DOCTOR_PROMPT_VERSION}")
    print(f"  D 层启用: {', '.join(enabled_doctors)}")
    print(f"  A 层启用: {'是' if enable_structural else '否'}")
    print(f"  阈值: confidence ≥ {MIN_CONFIDENCE}, severity ≥ {MIN_SEVERITY_TO_APPLY}")
    print(f"  配额: {PATCH_QUOTA}")
    if only_chapter:
        print(f"  仅审 {only_chapter}")
    print(f"{'='*55}\n")

    # ─── 第 0 步：每章 context probe ───
    print("  ── Step 0: 章级语境体检 ──")
    for ch in revised_chapters:
        if not ch.get("body"):
            continue
        if only_chapter and ch.get("id") != only_chapter:
            continue
        ctx = _probe_chapter_context(ch, outline, cache_dir, log_archive)
        shared.set_context(ch["id"], ctx)
        cached_mark = " [cache]" if ctx.get("_cached") else ""
        skipped = ctx.get("_skipped")
        if skipped:
            print(f"    {ch['id']:6s} ⏭ {skipped}")
        else:
            era = ctx.get("scene_era", "?")
            n_anchors = len(ctx.get("anchors", []))
            print(f"    {ch['id']:6s} 主时空={era}; 锚点={n_anchors} 个{cached_mark}")
    print()

    # ─── D 层：逐章逐 doctor ───
    new_chapters = []
    for i, ch in enumerate(revised_chapters):
        if not ch.get("body"):
            new_chapters.append(ch)
            continue

        if only_chapter and ch.get("id") != only_chapter:
            new_chapters.append(ch)
            continue

        prev_summary = ""
        if i > 0:
            prev_bodies = [c.get("body", "")[:300] for c in new_chapters[-2:]]
            prev_summary = "\n---\n".join(prev_bodies)

        print(f"  [{i+1}/{len(revised_chapters)}] {ch['id']} {ch.get('title', '')}")
        print(f"           原文 {len(ch.get('body', ''))} 字")

        ch_revised = ch
        for doctor in enabled_doctors:
            print(f"    [doctor] {doctor:12s} ", end="", flush=True)
            result = _run_d_doctor(ch_revised, outline, doctor, prev_summary,
                                    shared, cache_dir, log_archive)
            cached_mark = " [cache]" if result.get("_cached") else ""
            skipped = result.get("skipped")
            n_issues = len(result.get("issues", []))
            n_patches_raw = len(result.get("patches", []))

            if skipped:
                print(f"⏭ {skipped}{cached_mark}")
                continue

            # 阈值过滤 + 配额
            filtered = _filter_patches_by_threshold(
                result.get("patches", []),
                doctor=doctor,
                revision_log=revision_log,
                ch_id=ch_revised.get("id"),
            )
            n_filtered = len(filtered)

            print(f"{n_issues} 问题 / {n_patches_raw} 候选 → {n_filtered} 应用{cached_mark}")

            # 把 doctor 识别的 anchor_candidate 累积进 shared
            new_anchors = []
            for issue in result.get("issues", []) or []:
                if isinstance(issue, dict):
                    a = issue.get("anchor_candidate")
                    if a:
                        new_anchors.append(str(a))
            shared.add_finding(ch_revised["id"], doctor,
                                result.get("issues", []),
                                new_anchors=new_anchors)

            if filtered:
                anchors_for_chapter = shared.get_anchors(ch_revised["id"])
                ch_revised = _apply_d_patches(ch_revised, filtered, doctor,
                                                revision_log,
                                                anchors_for_chapter)

        new_chapters.append(ch_revised)

    revised_chapters = new_chapters

    # ─── A 层：全片 ───
    if enable_structural:
        print(f"\n  [structural] 全片结构编辑 ", end="", flush=True)
        result = _run_structural_doctor(revised_chapters, outline,
                                          shared, cache_dir, log_archive)
        cached_mark = " [cache]" if result.get("_cached") else ""
        skipped = result.get("skipped")
        n_patches = len(result.get("patches", []))
        if skipped:
            print(f"⏭ {skipped}{cached_mark}")
        else:
            print(f"{n_patches} 跨章修订{cached_mark}")
            if result.get("patches"):
                revised_chapters = _apply_a_patches(revised_chapters,
                                                      result["patches"],
                                                      revision_log,
                                                      shared)

    # ─── 总结 ───
    applied = sum(1 for r in revision_log if r["status"] == "applied")
    rejected = sum(1 for r in revision_log if r["status"] == "rejected")
    by_doctor = {}
    for r in revision_log:
        if r["status"] == "applied":
            by_doctor[r["doctor"]] = by_doctor.get(r["doctor"], 0) + 1

    # 按拒绝原因分类（前 5）
    reject_reasons = {}
    for r in revision_log:
        if r["status"] == "rejected":
            key = r.get("reason", "?")[:40]
            reject_reasons[key] = reject_reasons.get(key, 0) + 1

    print(f"\n  ── 编剧大师总结 ──")
    print(f"  应用 {applied} 项 / 拒绝 {rejected} 项")
    for d, n in sorted(by_doctor.items(), key=lambda x: -x[1]):
        print(f"    {d:12s}  {n}")
    if reject_reasons:
        print(f"  ── 拒绝原因 Top 5 ──")
        for reason, n in sorted(reject_reasons.items(), key=lambda x: -x[1])[:5]:
            print(f"    {n:3d}× {reason}")
    print(f"{'='*55}\n")

    return revised_chapters, revision_log


# ════════════════════════════════════════════════════════════════
# 终端打印
# ════════════════════════════════════════════════════════════════

def print_revisions(revision_log: list, max_show: int = 30,
                     use_color: bool = True):
    """打印修订报告（紧凑版）"""
    C = {"reset": "\033[0m", "bold": "\033[1m",
         "red": "\033[31m", "green": "\033[32m",
         "yellow": "\033[33m", "blue": "\033[34m", "gray": "\033[90m"}
    if not use_color:
        C = {k: "" for k in C}

    applied = [r for r in revision_log if r["status"] == "applied"]
    rejected = [r for r in revision_log if r["status"] == "rejected"]

    if not applied and not rejected:
        print(f"  {C['green']}✓ 所有章节通过审稿，无需修订{C['reset']}")
        return

    print(f"\n{C['bold']}── 编剧大师修订报告 ──{C['reset']}")
    for r in applied[:max_show]:
        ch = r.get("ch_id", "?")
        d = r.get("doctor", "?")
        before = (r.get("before", "") or "")[:60]
        after = (r.get("after", "") or "")[:60]
        reason = r.get("reason", "")
        sev = r.get("severity", "")
        conf = r.get("confidence", "")
        sev_conf = ""
        if sev or conf:
            sev_conf = f" [{sev}/{conf}]"
        print(f"\n  {C['yellow']}⚠ {ch} [{d}]{sev_conf}{C['reset']}  {C['gray']}{reason}{C['reset']}")
        if before:
            print(f"    {C['red']}- {before}{C['reset']}")
        if after:
            print(f"    {C['green']}+ {after}{C['reset']}")

    if len(applied) > max_show:
        print(f"\n  …还有 {len(applied) - max_show} 项修订（详见 yaml 的 _doctor_revisions）")

    if rejected:
        print(f"\n  {C['gray']}─── 被拒绝 ({len(rejected)}) ───{C['reset']}")
        for r in rejected[:8]:
            print(f"    • {r.get('ch_id')}.{r.get('field')} ({r.get('doctor')}): {r.get('reason')}")
