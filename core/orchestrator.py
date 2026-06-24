"""
orchestrator.py ── FLUX-native 决策引擎 v3
============================================
v3 改动（基于 v2）：
  - inject_shot_into_scene_prompt 现在返回 (positive, extra_negative) tuple
  - _build_final_params 里 extra_negative 追加进 negative prompt
  - 其他逻辑不变，接口兼容 pipeline.py
"""

import json
import operator
import re
from pathlib import Path
from typing import Annotated, Optional, TypedDict

from langchain_openai import ChatOpenAI
from langchain_core.messages import HumanMessage

from feedback import clean_json


class PageState(TypedDict):
    page_config:      dict
    messages:         Annotated[list, operator.add]
    decision:         dict
    generated:        bool
    feedback_context: Optional[dict]


# ════════════════════════════════════════════════════════════════
# 核心函数一：旁白 → 英文场景描述
# ════════════════════════════════════════════════════════════════

def _build_alignment_from_prebuilt(page_cfg: dict, scene_cfg: dict) -> dict:
    """
    v2.3 副线程快速通道：直接从 _prebuilt_storyboard 拼装结果，不调 LLM。
    用于长篇——分镜大师已经精心写好了 focal_subject，再让 orchestrator
    用 LLM 翻译 narration 会破坏它。这里直接用 prebuilt 作为 scene_prompt。
    """
    board = page_cfg.get("_prebuilt_storyboard", {}) or {}
    narration = page_cfg.get("narration", "")
    scene_desc = scene_cfg.get("scene", "")
    focal = (board.get("focal_subject") or "").strip()
    must_haves = board.get("visual_must_haves", []) or []
    prev_anchor = page_cfg.get("_prev_shot_anchor", "").strip()
    shot_type = board.get("shot_type", "medium")

    # v2.3.6.1：fast track 自己拼完整 scene_prompt，不再调 inject_shot_into_scene_prompt
    # 旧版调 inject 会让 focal/must_haves/prev_anchor 重复 2-3 次，
    # 导致 token 占满、关键词权重稀释（FLUX CLIP-L 段 77 token 限制）。
    # 这里去重重写，并显式拼景别提示词。
    #
    # v2.3.6：段落重排——FLUX CLIP-L 硬截断 77 token，style_prefix 已占 ~33 token。
    # 把 must_haves 的 mood（情绪词）提到最前，确保 "dawning horror" 这类
    # 情绪关键词落在窗口内，不再被截掉。物件 attributes 留尾部（截断损失最小）。

    _SHOT_HINTS = {
        "extreme_closeup": "extreme close-up shot",
        "closeup":         "close-up shot",
        "medium_closeup":  "medium close-up",
        "medium":          "medium shot",
        "wide":            "wide shot",
        "extreme_wide":    "extreme wide shot, establishing shot",
    }

    # must_haves 拆成 mood（情绪）和 attributes（物件），分别去重
    seen = set()
    mood_phrases = []
    attr_phrases = []
    for mh in must_haves[:3]:
        if not isinstance(mh, dict):
            continue
        mood_v = (mh.get("mood") or "").strip()
        if mood_v and mood_v.lower() not in seen:
            seen.add(mood_v.lower())
            mood_phrases.append(mood_v)
        attr_v = (mh.get("attributes") or "").strip()
        if attr_v and attr_v.lower() not in seen:
            seen.add(attr_v.lower())
            attr_phrases.append(attr_v)

    parts = []
    # 1. 情绪/氛围词——提到最前，确保进 CLIP-L 77-token 窗口
    if mood_phrases:
        parts.append(", ".join(mood_phrases))

    # 2. 主体焦点（分镜大师写好的 focal_subject）
    if focal:
        parts.append(focal)

    # 3. 前镜锚（场景延续）——降到 focal 之后
    if prev_anchor:
        parts.append(f"continuing scene from {prev_anchor}")

    # 4. 景别提示（focal 里已描述过景别就不重复）
    shot_hint = _SHOT_HINTS.get(shot_type, "")
    if shot_hint and shot_hint.lower() not in (focal or "").lower():
        parts.append(shot_hint)

    # 5. 物件 attributes——留尾部
    if attr_phrases:
        parts.append(", ".join(attr_phrases))

    scene_prompt = ", ".join(p for p in parts if p)
    if not scene_prompt:
        scene_prompt = scene_desc[:200] if scene_desc else narration

    # negative_add：从 must_haves[].exclude 拼，也去重
    seen_neg = set()
    negative_parts = []
    for mh in must_haves[:3]:
        if not isinstance(mh, dict):
            continue
        ex = (mh.get("exclude") or "").strip()
        ex_lower = ex.lower()
        if ex and ex_lower not in seen_neg:
            seen_neg.add(ex_lower)
            negative_parts.append(ex)
    negative_add = ", ".join(negative_parts)

    return {
        "scene_prompt":      scene_prompt,
        "negative_add":      negative_add,
        "reasoning":         "[v2.3 快速通道 v2.3.6.1 去重] focal+shot+must_haves 已拼好，不再 inject",
        "shot_type":         shot_type,
        "visual_must_haves": must_haves,
    }


def analyze_narration_alignment(page_cfg: dict, scene_cfg: dict,
                                theme, story,
                                board_entry: Optional[dict] = None) -> dict:
    """
    返回：
    {
        "scene_prompt":      str,   # 已注入景别/焦点/物体属性
        "negative_add":      str,   # 已汇总 must_haves[].exclude
        "reasoning":         str,
        "shot_type":         str,
        "visual_must_haves": list,  # 结构体列表，透传给 QualityContext
    }
    """
    from config import LLM_API_KEY, LLM_BASE_URL, LLM_MODEL

    # v2.3 副线程快速通道：长篇 to_pipeline 标记了 _skip_llm_alignment
    if page_cfg.get("_skip_llm_alignment") and \
            page_cfg.get("_prebuilt_storyboard", {}).get("focal_subject"):
        result = _build_alignment_from_prebuilt(page_cfg, scene_cfg)
        print(f"  [旁白→场景] p{page_cfg.get('page','?')} ✓  [v2.3 快速通道]")
        print(f"  [旁白→场景] {result['reasoning']}")
        if result.get("negative_add"):
            print(f"  [旁白→场景] 排除: {result['negative_add'][:80]}")
        return result

    narration  = page_cfg.get("narration", "")
    title      = page_cfg.get("title", "")
    chars      = page_cfg.get("characters", [])
    image_type = scene_cfg.get("image_type", "")
    scene_desc = scene_cfg.get("scene", "")

    char_descriptions = []
    for cname in chars:
        c    = story.characters.get(cname, {})
        desc = c.get("trigger_solo") or c.get("trigger_multi") or c.get("desc", "")
        if desc:
            char_descriptions.append(desc.strip())

    shot_type  = board_entry.get("shot_type",         "medium") if board_entry else "medium"
    must_haves = board_entry.get("visual_must_haves",  [])      if board_entry else []

    default = {
        "scene_prompt":      scene_desc[:200] if scene_desc else narration,
        "negative_add":      "",
        "reasoning":         "分析跳过，使用场景默认描述",
        "shot_type":         shot_type,
        "visual_must_haves": must_haves,
    }

    # 纯背景：直接注入景别，返回
    if image_type == "background_only":
        default["reasoning"] = "background_only 场景，直接使用场景模板"
        if board_entry:
            from storyboard import inject_shot_into_scene_prompt
            new_pos, extra_neg = inject_shot_into_scene_prompt(
                default["scene_prompt"], board_entry)
            default["scene_prompt"] = new_pos
            default["negative_add"] = extra_neg
        return default

    # ── 分镜上下文（有 board_entry 才拼）─────────────────────
    shot_context = ""
    if board_entry:
        focal_subject    = board_entry.get("focal_subject",    "")
        composition_hint = board_entry.get("composition_hint", "")
        transition_from  = board_entry.get("transition_from",  "")

        # 汇总 must_haves 给 LLM 看（让它写 scene_prompt 时有意识地包含这些元素）
        must_lines = []
        for m in must_haves:
            if isinstance(m, dict) and m.get("element"):
                line = f"  • {m['element']}"
                if m.get("attributes"):
                    line += f"（{m['attributes']}）"
                if m.get("state"):
                    line += f" — 状态：{m['state']}"
                must_lines.append(line)
        must_str = "\n".join(must_lines) if must_lines else "  无"

        shot_context = f"""
【分镜师已规划（必须严格执行）】
景别：{shot_type}
视觉焦点：{focal_subject}
构图提示：{composition_hint}
与上页关联：{transition_from if transition_from else '无'}

本页图中必须出现的元素（含属性/状态要求）：
{must_str}

景别描述要求（scene_prompt 必须体现）：
- extreme_wide   → 人物极小，环境占画面 80% 以上
- wide           → 人物全身可见，环境为主
- medium         → 人物上半身，背景简化
- closeup        → 人物面部特写，背景虚化
- extreme_closeup → 局部特写（眼睛/手/道具），不出现完整人脸
不可降级为"全身站立"或"两人合照"。
"""

    char_context = ""
    if char_descriptions:
        char_context = "\n出场角色外观描述：\n" + "\n".join(
            f"- {d}" for d in char_descriptions)

    multi_instruction = ""
    if len(chars) > 1:
        multi_instruction = (
            f"\n【多角色场景】画面有 {len(chars)} 个角色，"
            f"必须在描述中明确两人位置关系（on the left / on the right / facing each other）。\n"
        )

    prompt = f"""你是漫画分镜专家，把以下旁白转化为适合 FLUX 生图模型的英文场景描述。

页面标题：{title}
旁白内容：{narration}
场景参考：{scene_desc[:150]}{char_context}{multi_instruction}{shot_context}

【核心任务】
写一段完整的英文场景描述（50-80个英文单词），要求：
1. 严格按照分镜师规划的景别写
2. 精确描述"必须出现的元素"的外观属性和当前状态（最重要）
3. 包含旁白里的具体视觉元素和动作
4. 包含光线和氛围词
5. 使用自然语言句子，不要用逗号堆砌标签

只返回JSON：
{{
  "scene_prompt": "英文场景描述，50-80词，自然语言句子",
  "negative_add": "需要排除的视觉元素，英文，10词内，没有则留空",
  "reasoning": "中文，一句话：提取了哪些关键视觉元素，景别是否已体现"
}}"""

    try:
        llm = ChatOpenAI(
            model    = LLM_MODEL,
            api_key  = LLM_API_KEY,
            base_url = LLM_BASE_URL,
        )
        full_content = ""
        for chunk in llm.stream([HumanMessage(content=prompt)]):
            full_content += chunk.content

        t = clean_json(full_content)
        if not t.strip().startswith("{"):
            import re as _re
            m = _re.search(r'\{.*?\}', t, _re.DOTALL)
            t = m.group() if m else "{}"
        parsed = json.loads(t)

        result = {**default, **parsed}

        # v3 核心：注入景别 + 物体属性到正向，exclude 到负向
        if board_entry:
            from storyboard import inject_shot_into_scene_prompt
            new_pos, extra_neg = inject_shot_into_scene_prompt(
                result["scene_prompt"], board_entry)
            result["scene_prompt"] = new_pos
            # 合并：LLM 输出的 negative_add + must_haves 的 exclude
            llm_neg = result.get("negative_add", "").strip()
            if llm_neg and extra_neg:
                result["negative_add"] = llm_neg + ", " + extra_neg
            elif extra_neg:
                result["negative_add"] = extra_neg

        result["shot_type"]         = shot_type
        result["visual_must_haves"] = must_haves

        print(f"  [旁白→场景] p{page_cfg['page']} ✓  [{shot_type}]")
        print(f"  [旁白→场景] {result.get('reasoning', '')}")
        if must_haves:
            elements = [m.get("element","?") if isinstance(m,dict) else str(m)
                        for m in must_haves]
            print(f"  [旁白→场景] 必须有: {elements}")
        if result.get("negative_add"):
            print(f"  [旁白→场景] 排除: {result['negative_add'][:80]}")
        return result

    except Exception as e:
        print(f"  [旁白→场景] 失败: {e}，使用默认场景描述")
        return default


# ════════════════════════════════════════════════════════════════
# 核心函数二：重生时按反馈改写场景描述 (v2.6 重写)
# ════════════════════════════════════════════════════════════════
#
# 历史问题(v2.5 及之前):
#   旧实现把质量门反馈整段塞给 LLM,要求"改写场景描述,修复问题"+"50-80 词"。
#   实战翻车场景:
#     - 反馈"面部模糊" → LLM 加 'every pore and stubble visible, precise texture'
#       → FLUX 把侧脸叙事镜变成证件照式怼脸特写,叙事关系消失
#     - 反馈"环境细节不足" → LLM 加 'institutional scent of disinfectant,
#       polished concrete floor' → 气味污染 prompt embedding + 把恐怖场景
#       改成现代医院走廊
#     - 反馈"画面不够紧凑" → LLM 加 'maintaining precise composition,
#       improving environmental credibility' → 元指令被 FLUX 当画面内容画
#     - 反馈"七具尸体不像活人" → LLM 加 'rigid limbs, hollow eye sockets'
#       → 现代尸体被画成石像怪/僵尸,完全颠覆原叙事
#
# 根因诊断:
#   1. 旧 prompt 模板没禁止"添加新概念" → LLM 想当然往里加
#   2. 旧模板鼓励"50-80 词" → 比原 prompt 还长,增量都是污染
#   3. 旧模板没区分"面部/手部缺陷" vs "叙事元素缺失" —— 前者根本不应该改 prompt,
#      应该靠换 seed + negative 解决,旧实现一律重写 prompt
#   4. 没看原图,只看反馈文字 —— LLM 容易过度补偿
#
# 新设计:
#   1. 分诊:面部/手部/构图等"渲染层缺陷" → 完全不改 prompt(纯靠 seed+negative)
#   2. 叙事元素缺失类才允许微调,且用极严格的白名单模板:
#      - 只能在原 prompt 基础上"插入缺失元素"或"加强已有元素"
#      - 禁止添加感官词(气味/温度/听觉/触感)
#      - 禁止添加元指令(maintaining / improving / precise / accurate)
#      - 禁止添加反向修饰(weathered but intact, dark yet warm 这种)
#      - 长度增量不超过原 prompt 的 30%
#   3. diff 卫士兜底:改写后扫黑名单词,命中则剥离;长度超限直接回退原 prompt

# v2.6 黑名单:这些词进 FLUX prompt 会污染语义,LLM 改写时绝对不许添加
_PROMPT_BLACKLIST_KEYWORDS = (
    # 感官非视觉词(FLUX 是文生图,只懂视觉,加这些只污染 embedding)
    "scent", "smell", "odor", "aroma", "fragrance",
    "sound", "noise", "echo", "silence",
    "temperature", "warm air", "cold air", "humid",
    "lingering", "wafting",
    # 元指令词(LLM 自言自语,FLUX 会当内容画)
    "maintaining", "maintain", "preserving", "preserve",
    "improving", "improve", "enhancing", "enhance",
    "credibility", "consistency", "coherence",
    "precise composition", "accurate", "purposeful",
    "while improving", "while maintaining",
    # 反向修饰陷阱(FLUX 不擅长理解"but/yet/while"逆否,容易抓后半)
    "but structurally", "yet still", "while still",
    "but intact", "but unchanged",
    # 过度细节词(把叙事镜推成证件照特写的元凶)
    "every pore", "each strand", "every wrinkle",
    "ultra detailed face", "hyper-detailed skin",
    "showing every detail of",
)

# 渲染层缺陷 tag —— 这些问题不该改 scene prompt,纯靠 seed/negative 解决
_RENDERING_DEFECT_TAGS = frozenset({
    "bad_hands", "face_deformed", "too_blurry", "bad_anatomy",
    "low_quality", "bad_face", "deformed_face",
    "extra_fingers", "missing_fingers", "wrong_anatomy",
})

# 叙事缺陷 tag —— 这些问题改 scene prompt 可能有用(谨慎)
_NARRATIVE_DEFECT_TAGS = frozenset({
    "narration_mismatch", "figure_missing",
    "element_missing", "wrong_scene", "wrong_era",
})


def _classify_feedback_tags(tags: list) -> str:
    """
    把 tags 列表分类成 'rendering' / 'narrative' / 'mixed' / 'unknown'。

    决策意义:
      rendering → 完全不改 scene prompt,return 原 prompt(交给 seed+negative)
      narrative → 允许走 LLM 改写,但用严格白名单模板
      mixed     → 走 narrative 路径(因为渲染缺陷的部分反正会被 seed/negative 兜)
      unknown   → 保守起见,不改 prompt
    """
    if not tags:
        return "unknown"
    has_render = any(t in _RENDERING_DEFECT_TAGS for t in tags)
    has_narr   = any(t in _NARRATIVE_DEFECT_TAGS for t in tags)
    if has_narr and has_render:
        return "mixed"
    if has_narr:
        return "narrative"
    if has_render:
        return "rendering"
    return "unknown"


def _sanitize_rewritten_prompt(new_prompt: str, original_prompt: str) -> tuple:
    """
    diff 卫士:对 LLM 改写后的 prompt 做最后清洗。

    返回 (清洗后 prompt, 警告列表)。

    四道检查(由温和到严厉):
      1. 长度卫士:新 prompt 不超过原 prompt 1.3 倍长度 → 否则回退原 prompt
      2. 黑名单卫士:扫 _PROMPT_BLACKLIST_KEYWORDS,命中的【整段从句/词组】剥掉
      3. 元指令前缀卫士:扫 'while/maintaining/improving' 开头的尾巴从句剥掉
      4. 兜底:清洗后 prompt 太短(< 原 0.3 倍) → 回退原 prompt
    """
    warnings = []
    if not new_prompt or not new_prompt.strip():
        return original_prompt, ["LLM 返回空,回退原 prompt"]

    cleaned = new_prompt.strip().strip('"').strip("'")

    # 1. 长度卫士: 1.3 倍率【且】绝对增量超过 60 字才回退。
    # 单条件不够稳:短 prompt(30字)加一句合法补充就会冲破 1.3x,但绝对增量并不大。
    # 双条件:必须既倍率超、又增量大,才算"添油加醋过度"。
    # 60 字 ≈ 1-2 句污染描述的典型长度。
    orig_len = len(original_prompt)
    new_len  = len(cleaned)
    growth   = new_len - orig_len
    if new_len > orig_len * 1.3 and growth > 60:
        warnings.append(
            f"长度增长 {new_len}>{orig_len}*1.3 且增量 {growth}>60,"
            f"LLM 添油加醋过度 → 回退原 prompt")
        return original_prompt, warnings

    # 2/3. 黑名单逐词扫(case-insensitive)。命中时把"该词所在的逗号/句号分隔段"整段剥掉,
    # 而非只剥单词 —— 因为坏词通常带着一串污染修饰("polished concrete floor with purposeful
    # scuff marks" 整段都要走)。
    segments = re.split(r"([,.;])", cleaned)
    # 注意 re.split 带捕获组会把分隔符也留下,我们重新拼回去时保留
    kept = []
    dropped_segments = []
    for seg in segments:
        if seg in (",", ".", ";"):
            kept.append(seg)
            continue
        seg_low = seg.lower()
        hit = None
        for bad in _PROMPT_BLACKLIST_KEYWORDS:
            if bad in seg_low:
                hit = bad
                break
        if hit:
            dropped_segments.append((seg.strip(), hit))
        else:
            kept.append(seg)

    cleaned2 = "".join(kept).strip()
    # 清掉首尾残留的标点
    cleaned2 = cleaned2.strip(",.; ").strip()
    # 清掉相邻的重复逗号
    cleaned2 = re.sub(r"\s*,\s*,+", ", ", cleaned2)
    cleaned2 = re.sub(r"\s+", " ", cleaned2)

    if dropped_segments:
        for seg, bad in dropped_segments:
            warnings.append(f"剥掉黑名单段(命中 '{bad}'): '{seg[:60]}'")

    # 4. 清洗后过短兜底
    if len(cleaned2) < orig_len * 0.3:
        warnings.append(
            f"清洗后过短 {len(cleaned2)}<{orig_len}*0.3 → 回退原 prompt")
        return original_prompt, warnings

    return cleaned2, warnings


# v2.6 重生 prompt 改写模板 —— 极严格的白名单式指令
_RETRY_PROMPT_TEMPLATE = """你正在调整一段 FLUX 文生图的场景描述(英文),用于"重生"质量未达标的画面。

═══════════════════════════════════════════════════════════════
原场景描述(必须保留 90% 以上原文,在此基础上做最小化修改):
{scene_prompt}
═══════════════════════════════════════════════════════════════

质量门反馈:
- 上次得分: {last_score:.1f}/10
- 问题标签: {tags_str}
- 详细评价: {feedback_str}

═══════════════════════════════════════════════════════════════
⚠️ 改写硬规矩 —— 违反任意一条,改写都是失败的:

1. 【最小修改原则】只在原描述基础上补充【明确缺失的视觉元素】,
   不要重写整段、不要换措辞、不要"优化表达"。
   原文里说"slightly ajar"就保留"slightly ajar",不要改成
   "weathered but structurally intact, slightly ajar"。

2. 【禁止添加新概念】
   - 不许加感官非视觉词:scent / smell / sound / temperature / lingering
   - 不许加元指令词:maintaining / improving / preserving / precise composition
   - 不许加反向修饰:"but structurally intact" / "yet still" / "while still"
   - 不许加"every pore" / "each strand" 这种把构图推成证件照特写的过度细节词

3. 【长度限制】改写后不许超过原描述 1.3 倍长度,且绝对增量不超过 60 字。
   宁可不补,也不要画蛇添足。原文 30 词,改写最多在合适位置插一句短补充。

4. 【面部/手部缺陷问题】如果反馈只是"面部模糊"/"手部畸形"这类渲染缺陷,
   说明 prompt 没问题、是 seed/采样问题 —— 直接原文返回,不要动。
   绝对不要加 "every pore visible" / "precise hands" 这种词,
   它们会把叙事镜推成怼脸特写。

5. 【叙事元素缺失】反馈说"X 元素缺失" → 在原文合适位置插入对应元素的
   简洁英文短语(2-5 词),如:"covered in dense spiderwebs" /
   "seven fresh scratches on stone"。插入,不要重写。

只返回改写后的英文描述,不要 JSON、不要解释、不要引号包裹。
如果你认为原文已经够好不需要改,直接原样返回原文。"""


def _retry_scene_prompt(scene_prompt: str, feedback_ctx: dict,
                        llm: ChatOpenAI) -> str:
    """v2.6: 分诊 + 严格白名单 + diff 卫士 三层防御。"""
    tags         = feedback_ctx.get("tags", []) or []
    tags_str     = "、".join(tags) or "无"
    feedback_str = feedback_ctx.get("feedback_text", "")
    last_score   = feedback_ctx.get("last_score", -1.0)

    # ── 分诊:渲染缺陷不动 prompt ──────────────────────
    category = _classify_feedback_tags(tags)
    if category == "rendering":
        print(f"  [重生] 反馈类型=渲染缺陷({tags_str}) → 保留原 scene prompt,"
              f"靠 seed+negative 解决")
        return scene_prompt
    if category == "unknown":
        print(f"  [重生] 反馈类型=未知 ({tags_str}) → 保守起见保留原 scene prompt")
        return scene_prompt

    # ── 叙事缺陷类:允许 LLM 改写,但用严格白名单 ──────
    prompt = _RETRY_PROMPT_TEMPLATE.format(
        scene_prompt = scene_prompt,
        last_score   = last_score,
        tags_str     = tags_str,
        feedback_str = feedback_str,
    )

    try:
        full_content = ""
        for chunk in llm.stream([HumanMessage(content=prompt)]):
            full_content += chunk.content
        raw_new = full_content.strip()

        # ── diff 卫士:黑名单 + 长度 + 兜底 ───────────
        sanitized, warns = _sanitize_rewritten_prompt(raw_new, scene_prompt)
        for w in warns:
            print(f"  [重生|sanitize] ⚠ {w}")

        if sanitized == scene_prompt:
            print(f"  [重生] 改写后清洗回退到原 prompt(说明 LLM 改坏了)")
        else:
            # 打 diff,便于调试
            print(f"  [重生] 场景描述已改写(类别={category})")
            print(f"  [重生|diff] 原 {len(scene_prompt)}字 → 新 {len(sanitized)}字")
        return sanitized
    except Exception as e:
        print(f"  [重生] 改写失败: {e},保留原描述")
        return scene_prompt


# ════════════════════════════════════════════════════════════════
# 核心函数三：组装 final_params + prompt 落盘
# ════════════════════════════════════════════════════════════════

def _select_mood_style(theme, page_cfg: dict) -> str:
    """v2.5.2: 按 page 的 bgm_mood 选 mood_style,实现"画面解绑"。
    
    向后兼容三层 fallback:
      1. theme yaml 的 storyboard.mood_styles[mood] (新机制,推荐)
      2. theme yaml 的 storyboard.mood_styles.default
      3. theme.prompts.style_prefix (旧机制,完全不变)
    
    这样旧 theme yaml(没写 storyboard 段的)行为 100% 不变。
    """
    mood = (page_cfg.get("bgm_mood") or "").strip().lower()
    
    # 尝试从 theme 拿 storyboard 段(我们在 yaml 里新增的)
    sb = getattr(theme, "storyboard", None) or {}
    if isinstance(sb, dict):
        mood_styles = sb.get("mood_styles") or {}
        if mood and mood in mood_styles:
            return mood_styles[mood]
        if "default" in mood_styles:
            return mood_styles["default"]
    
    # 终极 fallback:沿用原 style_prefix(零回归)
    return theme.prompts.style_prefix or ""


def _build_final_params(page_cfg: dict, scene_cfg: dict,
                        scene_prompt: str, negative_add: str,
                        theme, story) -> dict:

    # v2.5.2: style_prefix 按 mood 选择,旧 theme yaml 行为不变
    style_prefix = _select_mood_style(theme, page_cfg)
    positive     = style_prefix + scene_prompt

    neg_prefix = theme.prompts.negative_prefix or ""
    scene_neg  = scene_cfg.get("neg_add", "")
    negative   = neg_prefix + scene_neg
    # negative_add 已包含 must_haves[].exclude（由 analyze_narration_alignment 合并好的）
    if negative_add:
        negative = negative.rstrip(", ") + ", " + negative_add

    cfg     = scene_cfg.get("cfg",     theme.generation.default_cfg)
    steps   = scene_cfg.get("steps",   theme.generation.default_steps)
    sampler = scene_cfg.get("sampler", "euler")

    is_flux = bool(getattr(theme.model, "unet", ""))
    if is_flux:
        cfg     = max(1.0, min(5.0, float(cfg)))
        steps   = max(15,  min(30,  int(steps)))
        if sampler not in ("euler", "euler_ancestral"):
            sampler = "euler"

    chars     = page_cfg.get("characters", [])
    main_char = story.characters.get(chars[0], {}) if chars else {}
    lora      = main_char.get("lora", "") or ""
    lora_s    = main_char.get("lora_strength", main_char.get("strength", 1.0))

    print(f"  [参数] p{page_cfg['page']}  CFG={cfg}  Steps={steps}  Sampler={sampler}")

    _log_prompt(page_cfg, positive, negative, cfg, steps, sampler, lora, lora_s)

    return {
        "positive":        positive,
        "negative":        negative,
        "lora":            lora,
        "lora_strength":   lora_s,
        "lora2":           None,
        "lora2_strength":  0.0,
        "cfg":             cfg,
        "steps":           steps,
        "sampler":         sampler,
        "seed":            page_cfg.get("seed", 42),
        "prefix":          f"page{page_cfg['page']:02d}",
        "upscale_denoise": theme.generation.upscale_denoise,
        "ip_ref":          None,
        "face_ref":        None,
        "_faceid_model":   "",
        "_faceid_vision":  "",
        "_checkpoint":     theme.model.checkpoint,
        "_ipadapter":      theme.model.ipadapter,
        "_clip_vision":    theme.model.clip_vision,
        "_unet":           getattr(theme.model, "unet",  "") or "",
        "_clip1":          getattr(theme.model, "clip1", "") or "",
        "_clip2":          getattr(theme.model, "clip2", "") or "",
        "_vae":            getattr(theme.model, "vae",   "") or "",
        "depth_ref":       None,
        "pose_action":     page_cfg.get("action", None),
    }


# v2.3 分级日志：当前活动 story_id（pipeline 启动时设置）
_CURRENT_STORY_ID = None


def set_current_story_id(sid: str):
    """v2.3：pipeline 启动时调一次，让 _log_prompt 能写到 logs/prompts/<sid>/"""
    global _CURRENT_STORY_ID
    _CURRENT_STORY_ID = sid


def _log_prompt(page_cfg: dict, positive: str, negative: str,
                cfg, steps, sampler, lora, lora_s):
    from datetime import datetime

    page_num = page_cfg["page"]
    title    = page_cfg.get("title", "")

    sep = "─" * 52
    print(f"\n  {sep}")
    print(f"  [PROMPT] p{page_num} {title}")
    print(f"  [POS] {positive}")
    print(f"  [NEG] {negative}")
    print(f"  {sep}")

    log_entry = {
        "time":     datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "page":     page_num,
        "title":    title,
        "cfg":      cfg,
        "steps":    steps,
        "sampler":  sampler,
        "lora":     lora,
        "lora_s":   lora_s,
        "positive": positive,
        "negative": negative,
    }

    # 老版日志（向下兼容）
    log_path = Path("./prompt_log.json")
    existing = []
    if log_path.exists():
        try:
            existing = json.loads(log_path.read_text(encoding="utf-8"))
            if not isinstance(existing, list):
                existing = []
        except Exception:
            existing = []
    existing.append(log_entry)
    try:
        log_path.write_text(
            json.dumps(existing, ensure_ascii=False, indent=2),
            encoding="utf-8"
        )
    except Exception as e:
        print(f"  [prompt_log] 写入失败（非致命）: {e}")

    # v2.3 分级日志：写一份到 logs/prompts/<story_id>/
    try:
        sid = (page_cfg.get("_story_id") or
               _CURRENT_STORY_ID or
               "unknown")
        try:
            from _log_utils import LogArchive
        except ImportError:
            try:
                from tools.long_writer._log_utils import LogArchive
            except ImportError:
                LogArchive = None
        if LogArchive:
            arc = LogArchive(sid)
            arc.write_prompt(
                page_num     = page_num,
                page_title   = title,
                positive     = positive,
                negative     = negative,
                cfg          = cfg,
                steps        = steps,
                sampler      = sampler,
                lora         = str(lora or ""),
                lora_strength= float(lora_s or 0.0),
                extra        = {
                    "_skip_llm_alignment": page_cfg.get("_skip_llm_alignment", False),
                    "_hold":               page_cfg.get("_hold", False),
                    "_source_chapter":     page_cfg.get("_source_chapter", ""),
                },
            )
    except Exception:
        # 分级日志失败不影响主流程
        pass


# ════════════════════════════════════════════════════════════════
# 对外接口
# ════════════════════════════════════════════════════════════════

class _FluxAgent:
    def __init__(self, theme, story):
        self.theme = theme
        self.story = story
        from config import LLM_API_KEY, LLM_BASE_URL, LLM_MODEL
        self._llm = ChatOpenAI(
            model    = LLM_MODEL,
            api_key  = LLM_API_KEY,
            base_url = LLM_BASE_URL,
        )

    def invoke(self, state: dict) -> dict:
        page_cfg  = state["page_config"]
        fb_ctx    = state.get("feedback_context") or {}
        scene_cfg = self.story.get_scene(page_cfg.get("scene_type", ""))

        overrides    = page_cfg.get("agent_overrides", {})
        scene_prompt = overrides.get("scene_prompt", "")
        negative_add = overrides.get("negative_add", "")

        if fb_ctx and scene_prompt:
            print(f"  [Agent] 重生模式，根据反馈改写场景描述...")
            scene_prompt = _retry_scene_prompt(scene_prompt, fb_ctx, self._llm)

        final_params = _build_final_params(
            page_cfg, scene_cfg, scene_prompt, negative_add,
            self.theme, self.story)

        return {
            **state,
            "decision": {
                "scene_prompt": scene_prompt,
                "final_params": final_params,
            },
        }


def build_decision_agent(theme, story) -> _FluxAgent:
    return _FluxAgent(theme, story)
