"""
tools/long_writer/narration_integrity.py ── 旁白完整性保护层
================================================================
v1.0 (2026-05-28) — 解决"分镜大师丢关键信息"问题的程序化兜底

【设计哲学】
不再加 LLM reviewer 来"看着 LLM 别丢东西"——LLM 永远会丢。
改为:程序记得原文里有什么(must_preserve),程序在产出后比对,
缺失的关键信息通过【程序化迁移/补救】或【一次定向 LLM 修复】回填。

【三层防护】
  Layer 1 (抽取):  segment 原文 → must_preserve 清单
                    纯正则 + 极轻量 LLM(可选), 抽取引号/悬念/道具/事件
  Layer 2 (对账):  shots 产出后, 对照 must_preserve, 算缺失项
                    引号: 精确匹配; 道具/事件: 子串/同义匹配
  Layer 3 (补救):  缺失项分类处理
                    引号: 程序化迁移到最相关 shot 的 dialogue (零 LLM)
                    道具/事件: 喂给定向 LLM 修复 (按需调用)

【为什么这个文件存在,而不是塞进 long_storyboard.py 或 reviewers.py】
  - long_storyboard.py 已经 3000+ 行,不能再扩
  - reviewers.py 是 LLM reviewer 池, 本模块主体是【程序化】, 职责不同
  - 独立成文件便于单独启停 (feature flag)、单独验证、单独调参

【接入点】
  在 reviewers.py::run_all_reviewers 中:
    1. 调 reviewer 链之前: extract_must_preserve(segments) → 挂到 chapter
    2. apply_patches 完成之后: enforce_integrity(shots, must_preserve)
       → 返回 (修复后 shots, audit_report)
"""

from __future__ import annotations
import re
from dataclasses import dataclass, field
from typing import Optional


# ════════════════════════════════════════════════════════════════
# Feature flag
# ════════════════════════════════════════════════════════════════

def is_enabled() -> bool:
    """读 config.ENABLE_NARRATION_INTEGRITY, 默认 on (这一层风险极低)。

    关闭后整个模块退化为 no-op, run_all_reviewers 行为不变。
    """
    try:
        from config import ENABLE_NARRATION_INTEGRITY
        return bool(ENABLE_NARRATION_INTEGRITY)
    except (ImportError, AttributeError):
        return True  # 默认开,因为纯程序化,无副作用


# ════════════════════════════════════════════════════════════════
# 数据结构
# ════════════════════════════════════════════════════════════════

@dataclass
class MustPreserve:
    """单个 segment 的关键信息清单。

    分两级:
      critical: 丢了就报缺失, 进入补救流程
      preferred: 丢了只记日志, 不阻断
    """
    seg_id: str
    seg_text: str

    # —— Critical (4 类) ——
    quoted_dialogue: list[str] = field(default_factory=list)
    """引号对话原文 (不含引号本身)。例: ['还是老规矩啊。', '当你读到这封信...']"""

    suspense_hooks: list[str] = field(default_factory=list)
    """悬念短语 (以 ... 结尾、问句、未完成句)。例: ['当你读到...']"""

    plot_props: list[str] = field(default_factory=list)
    """剧情道具 (后续可能用到的物件首次引入)。例: ['牛皮信封', '钢琴键水印']"""

    key_events: list[str] = field(default_factory=list)
    """关键状态变化 (含标记动词的小句)。例: ['琴键铜钮从信封滑落']"""

    # —— Preferred ——
    character_entries: list[str] = field(default_factory=list)
    """角色入场动作 (X 推门/走来等)"""

    def critical_items(self) -> list[tuple[str, str]]:
        """返回所有 critical 项, 带类别标签。"""
        return [
            *[("quoted_dialogue", x) for x in self.quoted_dialogue],
            *[("suspense_hook", x) for x in self.suspense_hooks],
            *[("plot_prop", x) for x in self.plot_props],
            *[("key_event", x) for x in self.key_events],
        ]


# ════════════════════════════════════════════════════════════════
# Layer 1: 关键信息抽取
# ════════════════════════════════════════════════════════════════

# 引号对子的正则 (中英文双引号)
_QUOTE_RE = re.compile(r'"([^"]+)"|"([^"]+)"|"([^"]+)"')

# v2.12: 强调引号标记词 (刻字/题字/匾额/书名等场景, 非对话)
_EMPHASIS_MARKERS = ("刻", "写", "题", "印", "署", "字", "二字", "三字", "四字",
                     "牌", "匾", "碑", "签", "标", "《", "》", "曰", "名为", "叫做")


def _is_emphasis_quote(content: str, context: str) -> bool:
    """判断引号是否为强调引号(刻字/题字/书名), 而非对话。

    依据: 交接MD P2 — '树皮上刻着模糊的"韩秦"二字' 被误判成对话缺失。
    规则:
      - 内容带书名号 《》 → 强调
      - 内容短(≤4字) 且 上下文(前后各6字)含 刻/写/题/印/字 等标记词 → 强调
    """
    if "《" in content or "》" in content:
        return True
    if len(content) <= 4 and any(mk in context for mk in _EMPHASIS_MARKERS):
        return True
    return False

# 悬念结尾正则 (... 或 ? 或 ! 结尾的短句)
_SUSPENSE_TAIL_RE = re.compile(r'([^。!?…\n]{4,30}(?:\.{3,}|…+))')

# 标记动词 (剧情事件的关键动词)
_KEY_VERBS = {
    # 物件状态变化
    "滑落", "掉落", "坠落", "落下", "碎裂", "破碎", "断裂",
    "消失", "隐去", "浮现", "出现", "闪过", "闪现",
    "燃起", "熄灭", "爆出", "爆开", "渗出", "渗进",
    # 角色动作 (关键转折)
    "推开", "撞开", "踹开", "拽住", "抓住", "扑向",
    "回头", "转身", "睁眼", "闭眼", "倒下", "跪下",
}


def extract_must_preserve(seg_id: str, seg_text: str,
                          known_props: Optional[list[str]] = None) -> MustPreserve:
    """从 segment 原文抽出 must_preserve 清单。

    纯正则实现 (无 LLM 调用)。这一步要 100% 可靠、可解释、可调试。

    Args:
        seg_id:      段落编号 (seg01, seg02, ...)
        seg_text:    段原文
        known_props: 故事元信息里已声明的道具列表 (可选)
                     有了它, 道具识别准确率会高很多

    Returns:
        MustPreserve 数据类实例
    """
    mp = MustPreserve(seg_id=seg_id, seg_text=seg_text)

    # —— 1. 引号对话 ——
    # v2.12: 过滤"强调引号"(刻字/题字/书名), 它们不是对话。
    for m in _QUOTE_RE.finditer(seg_text):
        content = next((g for g in m.groups() if g), "").strip()
        if not content or len(content) < 2:
            continue
        # 取引号前后各 6 字上下文, 判断是否强调引号
        ctx_start = max(0, m.start() - 6)
        ctx_end = min(len(seg_text), m.end() + 6)
        context = seg_text[ctx_start:ctx_end]
        if _is_emphasis_quote(content, context):
            continue   # 刻字/题字, 不计入对话
        mp.quoted_dialogue.append(content)

    # —— 2. 悬念短语 ——
    # 注意:已经被引号包住的悬念句优先归到 quoted_dialogue, 这里只抓裸露的
    text_without_quotes = _QUOTE_RE.sub("", seg_text)
    for m in _SUSPENSE_TAIL_RE.finditer(text_without_quotes):
        hook = m.group(1).strip()
        if hook and len(hook) <= 30:
            mp.suspense_hooks.append(hook)

    # —— 3. 剧情道具 ——
    if known_props:
        # 优先用元信息里声明的道具列表 (准确率高)
        for prop in known_props:
            if prop and prop in seg_text:
                mp.plot_props.append(prop)
    # 注: 不做无监督的道具识别 (容易误识别) —— 没有 known_props 就靠 LLM 防线

    # —— 4. 关键事件 (含标记动词的小句) ——
    # 切句, 每句看是否含标记动词
    sentences = re.split(r'[。!?…\n]+', seg_text)
    for sent in sentences:
        sent = sent.strip()
        if not sent or len(sent) > 30:
            continue
        # 去掉引号内容 (那是对话, 不算事件)
        sent_clean = _QUOTE_RE.sub("", sent)
        for verb in _KEY_VERBS:
            if verb in sent_clean:
                # 保留含动词的最短小句
                # 找到动词位置, 取该动词所在的最短逗号片段
                parts = re.split(r'[,,;;]', sent)
                for part in parts:
                    if verb in part and 4 <= len(part.strip()) <= 25:
                        mp.key_events.append(part.strip())
                        break
                else:
                    if 4 <= len(sent.strip()) <= 25:
                        mp.key_events.append(sent.strip())
                break  # 一句话最多算一个 key_event

    # —— 5. 角色入场 (Preferred) ——
    # X + 入场动词 (推门/走进/走来/进来 等)
    entry_pattern = re.compile(
        r'([\u4e00-\u9fa5]{1,4})(?:的[\u4e00-\u9fa5]{1,6})?(?:推开|走进|进来|走来|跨入|踏进|来到)'
    )
    for m in entry_pattern.finditer(seg_text):
        # 抽出含主语的完整短句
        full_start = max(0, m.start() - 2)
        snippet = seg_text[full_start:m.end() + 8].strip()
        if snippet:
            mp.character_entries.append(snippet[:20])

    # 去重
    mp.quoted_dialogue = list(dict.fromkeys(mp.quoted_dialogue))
    mp.suspense_hooks = list(dict.fromkeys(mp.suspense_hooks))
    mp.plot_props = list(dict.fromkeys(mp.plot_props))
    mp.key_events = list(dict.fromkeys(mp.key_events))
    mp.character_entries = list(dict.fromkeys(mp.character_entries))

    return mp


def extract_chapter_must_preserve(chapter: dict,
                                   story_meta: dict) -> dict[str, MustPreserve]:
    """对整章所有 segment 跑一遍抽取。

    Returns:
        {seg_id: MustPreserve}
    """
    # 从 story_meta 收集已声明的道具/物件
    known_props = []
    for c in (story_meta.get("characters") or []):
        if isinstance(c, dict):
            # 角色的标志性物件
            features = c.get("key_features", "") or c.get("desc", "")
            if features:
                # 简单切词, 取长度 2-6 的名词性短语
                for token in re.findall(r'[\u4e00-\u9fa5]{2,6}', features):
                    if token not in known_props:
                        known_props.append(token)

    # 故事元信息里如果有显式的 key_props 字段, 加进来
    for p in (story_meta.get("key_props") or []):
        if isinstance(p, str) and p not in known_props:
            known_props.append(p)
        elif isinstance(p, dict):
            name = p.get("name") or p.get("anchor") or ""
            if name and name not in known_props:
                known_props.append(name)

    result = {}
    segments = chapter.get("segments", []) or []
    for i, seg in enumerate(segments):
        seg_id = f"seg{i+1:02d}"
        seg_text = seg if isinstance(seg, str) else (seg.get("text") if isinstance(seg, dict) else str(seg))
        result[seg_id] = extract_must_preserve(seg_id, seg_text, known_props)

    return result


# ════════════════════════════════════════════════════════════════
# Layer 2: 对账 (硬检查)
# ════════════════════════════════════════════════════════════════

# 简易同义/变形容忍 (针对中文)
def _fuzzy_match(needle: str, haystack: str) -> bool:
    """子串匹配, 允许少量字符变形。

    规则:
      1. 精确子串 → 命中
      2. needle 去标点空格后 在 haystack 去标点后 → 命中
      3. needle ≥4 字时, 内部任意连续 4 字在 haystack 中 → 命中 (容忍前后缀变化)
    """
    if not needle or not haystack:
        return False
    if needle in haystack:
        return True

    def strip_punct(s: str) -> str:
        return re.sub(r"""[\s,,。.!!??;;"'""''「」、\-—…]+""", "", s)

    n_clean = strip_punct(needle)
    h_clean = strip_punct(haystack)
    if n_clean and n_clean in h_clean:
        return True

    # 4 字滑窗
    if len(n_clean) >= 4:
        for i in range(len(n_clean) - 3):
            window = n_clean[i:i + 4]
            if window in h_clean:
                return True
    return False


@dataclass
class MissingItem:
    """一个缺失项。"""
    seg_id: str
    category: str       # quoted_dialogue / suspense_hook / plot_prop / key_event
    content: str
    severity: str       # critical | preferred


@dataclass
class IntegrityAudit:
    """整章的完整性对账报告。"""
    missing: list[MissingItem] = field(default_factory=list)
    auto_fixed: list[dict] = field(default_factory=list)
    """程序化已修复的项, 每项 {seg_id, category, content, action}"""

    def has_critical_missing(self) -> bool:
        return any(m.severity == "critical" for m in self.missing)

    def to_log_lines(self) -> list[str]:
        lines = []
        if not self.missing and not self.auto_fixed:
            lines.append("  [integrity] OK, 全部 critical 项已覆盖")
            return lines
        for fix in self.auto_fixed:
            lines.append(f"  [integrity] auto-fixed: {fix['category']} '{fix['content'][:20]}' ({fix['action']})")
        for m in self.missing:
            tag = "MISSING" if m.severity == "critical" else "skipped"
            lines.append(f"  [integrity] {tag}: [{m.seg_id}] {m.category} '{m.content[:30]}'")
        return lines


def _collect_shot_text(shot: dict) -> tuple[str, list[str]]:
    """从 shot 抽出 (narration_text, dialogue_texts)。"""
    narr = (shot.get("narration") or "").strip()
    dlgs = []
    for d in (shot.get("dialogue") or []):
        if isinstance(d, dict):
            t = (d.get("text") or "").strip()
            if t:
                dlgs.append(t)
    return narr, dlgs


def audit_segment_integrity(shots_of_seg: list[dict],
                            mp: MustPreserve) -> tuple[list[MissingItem], list[dict]]:
    """对单 segment 的 shots 做对账, 返回 (缺失列表, 已自动修复列表)。

    引号对话有特殊处理: 若发现引号内容出现在某 shot 的 narration 中(而不是
    dialogue), 直接程序化迁移到该 shot 的 dialogue 字段, 算"auto_fixed"。

    Args:
        shots_of_seg: 同 source_seg 的所有 shot
        mp:           该 segment 的 must_preserve

    Returns:
        (missing_items, auto_fixed_actions)
    """
    missing = []
    auto_fixed = []

    # —— 收集所有 shot 的文本 ——
    # all_narr: 所有 narration 拼起来
    # all_dlg:  所有 dialogue 拼起来
    # all_text: narration + dialogue 都拼起来
    all_narr = " ".join(_collect_shot_text(s)[0] for s in shots_of_seg)
    all_dlg_texts = [d for s in shots_of_seg for d in _collect_shot_text(s)[1]]
    all_dlg = " ".join(all_dlg_texts)
    all_text = all_narr + " " + all_dlg

    # —— 1. 引号对话 ——
    # 规则: 必须出现在 dialogue 字段; 若在 narration 出现则程序化迁移
    for q in mp.quoted_dialogue:
        # 在 dialogue 中 → OK
        if any(_fuzzy_match(q, d) for d in all_dlg_texts):
            continue
        # 不在 dialogue, 但在某 shot 的 narration 中 → 程序化迁移
        migrated = False
        for shot in shots_of_seg:
            narr = (shot.get("narration") or "")
            if not narr:
                continue
            # 用原引号样式扫一遍, 找到匹配的引号块
            quote_blocks = list(_QUOTE_RE.finditer(narr))
            for qm in quote_blocks:
                content = next((g for g in qm.groups() if g), "").strip()
                if _fuzzy_match(q, content) or _fuzzy_match(content, q):
                    # 迁移
                    speaker = _infer_speaker_for_quote(content, shot, narr)
                    new_narr = (narr[:qm.start()] + narr[qm.end():]).strip()
                    # 清理紧贴的标点/空格
                    new_narr = re.sub(r'\s*[,,]\s*$', '', new_narr).strip()
                    shot["narration"] = new_narr
                    shot.setdefault("dialogue", []).append({
                        "speaker": speaker,
                        "text": content,
                    })
                    auto_fixed.append({
                        "seg_id": mp.seg_id,
                        "category": "quoted_dialogue",
                        "content": content,
                        "action": f"migrated narration→dialogue (speaker={speaker})",
                    })
                    migrated = True
                    break
            if migrated:
                break
        if not migrated:
            missing.append(MissingItem(
                seg_id=mp.seg_id,
                category="quoted_dialogue",
                content=q,
                severity="critical",
            ))

    # —— 2. 悬念短语 ——
    # 出现在 narration 或 dialogue 任一处 → OK
    for hook in mp.suspense_hooks:
        if _fuzzy_match(hook, all_text):
            continue
        missing.append(MissingItem(
            seg_id=mp.seg_id,
            category="suspense_hook",
            content=hook,
            severity="critical",
        ))

    # —— 3. 剧情道具 ——
    # 道具名只需在某 shot 的 narration 或 visual_must_haves 中出现
    all_visuals = " ".join(
        str(s.get("visual_must_haves") or "") for s in shots_of_seg
    )
    text_with_visuals = all_text + " " + all_visuals
    for prop in mp.plot_props:
        if _fuzzy_match(prop, text_with_visuals):
            continue
        missing.append(MissingItem(
            seg_id=mp.seg_id,
            category="plot_prop",
            content=prop,
            severity="critical",
        ))

    # —— 4. 关键事件 ——
    for ev in mp.key_events:
        if _fuzzy_match(ev, all_narr):
            continue
        missing.append(MissingItem(
            seg_id=mp.seg_id,
            category="key_event",
            content=ev,
            severity="critical",
        ))

    # —— 5. 角色入场 (Preferred, 不阻断) ——
    for entry in mp.character_entries:
        if _fuzzy_match(entry, all_narr):
            continue
        missing.append(MissingItem(
            seg_id=mp.seg_id,
            category="character_entry",
            content=entry,
            severity="preferred",
        ))

    return missing, auto_fixed


def _infer_speaker_for_quote(quote_text: str, shot: dict, full_narr: str) -> str:
    """从 narration 上下文推断引号 speaker。

    简单启发式:
      1. 引号前最近的角色名 (visible_characters 内的) → 那个角色
      2. 引号前后含"道"/"说"/"问"/"喊"等 → 仍是上述角色
      3. 找不到 → narrator_quote
    """
    vc = shot.get("visible_characters") or []
    if not vc:
        return "narrator_quote"

    # 找引号在 narration 中的位置, 看前 20 字内提到哪个角色
    quote_pos = full_narr.find(quote_text[:6]) if len(quote_text) >= 6 else -1
    if quote_pos == -1:
        # fallback: 直接用第一个 visible_character
        return vc[0] if vc else "narrator_quote"

    prefix = full_narr[max(0, quote_pos - 20):quote_pos]
    for name in vc:
        if name and name in prefix:
            return name

    # 没找到, 但镜里只有一个 visible_character → 假定是 ta
    if len(vc) == 1:
        return vc[0]

    return "narrator_quote"


# ════════════════════════════════════════════════════════════════
# Layer 2 主入口: 整章对账
# ════════════════════════════════════════════════════════════════

def enforce_integrity(shots: list[dict],
                       must_preserve_by_seg: dict[str, MustPreserve]) -> IntegrityAudit:
    """对整章 shots 做完整性对账 + 程序化补救。

    流程:
      1. 按 source_seg 分组
      2. 对每组跑 audit_segment_integrity
      3. 引号对话的程序化迁移会【直接修改 shots】 (in-place)
      4. 剩余缺失项作为报告返回, 调用方决定:
         - 仅日志记录 → 继续
         - 触发 Layer 3 (LLM 定向修复) → 调 repair_with_llm

    Args:
        shots:                整章 shots (会被原地修改, 仅引号迁移会改)
        must_preserve_by_seg: extract_chapter_must_preserve 的输出

    Returns:
        IntegrityAudit 报告
    """
    audit = IntegrityAudit()

    # 按 source_seg 分组
    by_seg: dict[str, list[dict]] = {}
    for shot in shots:
        seg_id = shot.get("source_seg") or "seg??"
        by_seg.setdefault(seg_id, []).append(shot)

    for seg_id, mp in must_preserve_by_seg.items():
        shots_of_seg = by_seg.get(seg_id, [])
        if not shots_of_seg:
            # 整个 segment 都没产出 shot? 这是大问题, 但本模块不管, 上游处理
            continue

        missing, auto_fixed = audit_segment_integrity(shots_of_seg, mp)
        audit.missing.extend(missing)
        audit.auto_fixed.extend(auto_fixed)

    return audit


# ════════════════════════════════════════════════════════════════
# Layer 3: LLM 定向修复 (按需调用)
# ════════════════════════════════════════════════════════════════

REPAIR_PROMPT = """你是分镜修复编辑。下面这章 shots 经过对账, 发现【关键信息丢失】。
请只修复这些缺失项, 把它们补回 shots, 其他一律不动。

【修复规则】
1. 只修发现的 missing 项, 不改其他 shot
2. 优先复用已有 shot (扩 narration 或加 dialogue 项), 实在塞不下才新增 shot
3. 新增 shot 时, source_seg 必须跟缺失项的 seg_id 一致
4. narration 单镜 ≤60 字
5. 引号对话进 dialogue 字段, 不进 narration
6. 不要重写其他 shot 的 focal_subject / visual_must_haves
7. 输出 JSON: {{"patched_shots": [...全部 shots, 含未修改的...]}}

【已建模角色】(speaker 必须从这里选)
{characters_csv}

【当前 shots (已经过其他 reviewer)】
{shots_json}

【对账发现的缺失项 (按 seg 聚类)】
{missing_block}

输出严格 JSON, 不要 markdown。"""


def repair_with_llm(shots: list[dict],
                     audit: IntegrityAudit,
                     story_meta: dict,
                     max_tokens: int = 6000) -> tuple[list[dict], bool]:
    """对照 audit 缺失项, 调一次 LLM 定向修复。

    Args:
        shots:       整章 shots
        audit:       Layer 2 输出的 audit 报告
        story_meta:  故事元信息 (角色列表等)

    Returns:
        (repaired_shots, success): success=False 表示 LLM 失败, 调用方应保留原 shots
    """
    import json

    critical = [m for m in audit.missing if m.severity == "critical"]
    if not critical:
        return shots, True

    # 角色清单
    chars = story_meta.get("characters") or []
    if isinstance(chars, list) and chars and isinstance(chars[0], dict):
        char_names = [c.get("name", "") for c in chars if c.get("name")]
    else:
        char_names = []
    characters_csv = ", ".join(char_names) if char_names else "(无)"

    # 缺失项聚类
    missing_by_seg: dict[str, list[MissingItem]] = {}
    for m in critical:
        missing_by_seg.setdefault(m.seg_id, []).append(m)

    missing_lines = []
    for seg_id, items in missing_by_seg.items():
        missing_lines.append(f"\n[{seg_id}]")
        for it in items:
            missing_lines.append(f"  - {it.category}: '{it.content}'")
    missing_block = "\n".join(missing_lines)

    # shots 序列化 (精简, 只留核心字段, 省 token)
    slim_shots = []
    for s in shots:
        slim_shots.append({
            "source_seg": s.get("source_seg"),
            "title": s.get("title", ""),
            "narration": s.get("narration", ""),
            "dialogue": s.get("dialogue", []),
            "focal_subject": s.get("focal_subject", "")[:120],  # 截断省 token
            "visible_characters": s.get("visible_characters", []),
            "_hold_type": s.get("_hold_type", ""),
        })
    shots_json = json.dumps(slim_shots, ensure_ascii=False, indent=2)

    prompt = REPAIR_PROMPT.format(
        characters_csv=characters_csv,
        shots_json=shots_json,
        missing_block=missing_block,
    )

    try:
        from config import LLM_API_KEY, LLM_BASE_URL, LLM_MODEL
        from langchain_openai import ChatOpenAI
        from langchain_core.messages import HumanMessage
    except ImportError as e:
        print(f"  [integrity] LLM 修复跳过 (import 失败): {e}")
        return shots, False

    try:
        llm = ChatOpenAI(
            model=LLM_MODEL, api_key=LLM_API_KEY, base_url=LLM_BASE_URL,
            temperature=0.2,
            max_tokens=max_tokens,
            timeout=240,
        )
        full = ""
        for chunk in llm.stream([HumanMessage(content=prompt)]):
            full += chunk.content
    except Exception as e:
        print(f"  [integrity] LLM 修复失败: {e}")
        return shots, False

    # 解析 JSON
    text = full.strip()
    if "```" in text:
        m = re.search(r'```(?:json)?\s*(\{.*?\})\s*```', text, re.DOTALL)
        if m:
            text = m.group(1)
        else:
            text = text.replace("```json", "").replace("```", "").strip()

    try:
        parsed = json.loads(text)
        patched = parsed.get("patched_shots")
        if not isinstance(patched, list) or not patched:
            print(f"  [integrity] LLM 修复返回格式错误, 保留原 shots")
            return shots, False
        # 把修复后 shots 的核心字段 merge 回原 shots (而不是替换, 因为
        # slim_shots 丢了很多字段)
        merged = _merge_repaired_shots(shots, patched)
        return merged, True
    except (json.JSONDecodeError, KeyError) as e:
        print(f"  [integrity] LLM 修复 JSON 解析失败: {e}")
        return shots, False


def _merge_repaired_shots(original: list[dict],
                           repaired_slim: list[dict]) -> list[dict]:
    """把 LLM 返回的精简版 patched_shots merge 回原 shots。

    规则:
      - 原 shots 的 title 索引 → 找到 repaired_slim 同 title 项, 覆盖
        narration / dialogue 字段
      - repaired_slim 里多出来的 shot (新增的) → append 到原 shots 末尾
        (附带最小必填字段)
    """
    # 索引原 shots (按 title 或 narration 前 10 字)
    def shot_key(s):
        return s.get("title") or s.get("narration", "")[:20]

    orig_by_key = {shot_key(s): s for s in original}

    result = [dict(s) for s in original]  # 拷贝, 不动原数据
    for rep in repaired_slim:
        k = shot_key(rep)
        if k in orig_by_key:
            # 找到对应原 shot, 在 result 里更新
            for r in result:
                if shot_key(r) == k:
                    # 只覆盖 narration / dialogue (其他字段尊重原 shot)
                    if "narration" in rep:
                        r["narration"] = rep["narration"][:60]
                    if "dialogue" in rep:
                        r["dialogue"] = rep["dialogue"]
                    break
        else:
            # 新增 shot, 补全最小字段
            new_shot = {
                "source_seg": rep.get("source_seg", "seg??"),
                "title": rep.get("title", ""),
                "narration": (rep.get("narration") or "")[:60],
                "dialogue": rep.get("dialogue") or [],
                "focal_subject": rep.get("focal_subject", ""),
                "shot_type": "closeup",  # 默认
                "transition_in": "match_cut",
                "kb_direction": "zoom_in",
                "visual_must_haves": [],
                "bgm_mood": "tension",
                "dynamic": False,
                "visible_characters": rep.get("visible_characters") or [],
                "previous_shot_anchor": "",
                "_hold_type": "",
                "_added_by_integrity_repair": True,
            }
            result.append(new_shot)
    return result


# ════════════════════════════════════════════════════════════════
# 单元自测 (运行此文件直接 python narration_integrity.py)
# ════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    # 雨巷旧书 seg01 的还原测试
    seg01_text = (
        '橘红夕照渗进青石板缝里, 王二麻子蜷在修车摊前搓油污指节。'
        '"还是老规矩啊。"张三的蓝布褂扫过门楣, 牛皮信封簌簌抖落几星墨渍。'
    )

    mp = extract_must_preserve("seg01", seg01_text, known_props=["牛皮信封", "墨渍"])
    print(f"=== seg01 抽取结果 ===")
    print(f"quoted_dialogue: {mp.quoted_dialogue}")
    print(f"suspense_hooks:  {mp.suspense_hooks}")
    print(f"plot_props:      {mp.plot_props}")
    print(f"key_events:      {mp.key_events}")
    print(f"character_entries: {mp.character_entries}")
    print()

    seg04_text = (
        '李四颤抖着捏起信纸:"当你读到这封信..."他没敢念下去。'
        '琴键形状的铜钮从信封滑落, 叮地一声落在青石板上。'
    )
    mp4 = extract_must_preserve("seg04", seg04_text, known_props=["铜钮", "信封", "琴键"])
    print(f"=== seg04 抽取结果 ===")
    print(f"quoted_dialogue: {mp4.quoted_dialogue}")
    print(f"suspense_hooks:  {mp4.suspense_hooks}")
    print(f"plot_props:      {mp4.plot_props}")
    print(f"key_events:      {mp4.key_events}")
    print()

    # 模拟一个产出 shots, 故意丢东西
    test_shots = [
        {
            "title": "ch01-sh01",
            "source_seg": "seg01",
            "narration": "橘红夕照渗进青石板缝里, 王二麻子蜷在修车摊前搓油污指节。",
            "dialogue": [],
            "visible_characters": [],
        },
        {
            "title": "ch01-sh02",
            "source_seg": "seg01",
            "narration": '"还是老规矩啊。"张三跨进门来。',
            "dialogue": [],
            "visible_characters": ["张三"],
        },
        # 故意没有信封那一镜
    ]

    audit = enforce_integrity(test_shots, {"seg01": mp})
    print(f"=== 对账报告 ===")
    for line in audit.to_log_lines():
        print(line)
    print()
    print(f"=== shots 修改后状态 ===")
    for s in test_shots:
        print(f"[{s['title']}]")
        print(f"  narr: {s['narration']}")
        print(f"  dlg : {s['dialogue']}")
