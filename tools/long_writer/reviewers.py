"""
reviewers.py ── 分镜审稿员 v2.4
=========================================
5 个独立 LLM 审稿员 + coordinator 协调器。

5 个 reviewer:
  - narrative  : narration 完整性
  - visual     : 镜头语言
  - flux       : focal_subject 4 要素 + 文字回避
  - dialogue   : 叙述层污染 + speaker 归属 (v2.4 解锁 dialogue[N].speaker)
  - coherence  : focal vs narration 忠实度 + 常识逻辑 (v2.4 新增)

v2.4 主要变化:
  1. apply_patches 入口加 no-op 过滤
  2. 解锁 dialogue[N].speaker 子字段(仅 dialogue reviewer)
  3. hold 镜头 narration 不可改(narrative reviewer 失效)
  4. 新增 coherence reviewer
  5. run_all_reviewers 接入 coordinator pre/post,返回三元组
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import json
import re
from typing import Optional


# ════════════════════════════════════════════════════════════════
# 字段白名单
# ════════════════════════════════════════════════════════════════

# ════════════════════════════════════════════════════════════════
# 字段权限矩阵 v2.11 ── 一件事一个出口 (消除九龙治水)
# ════════════════════════════════════════════════════════════════
# 设计原则:每个字段只有一个 reviewer 能写, 其他只能产 issue。
# 程序层(integrity/dedup, 在 coordinator/独立模块)先跑确定性处理,
# LLM reviewer 只处理程序处理不了的语义问题。
#
# 字段 → 唯一出口:
#   narration         → narrative (信息/事实) + narration_flow (语义流), 见下分工
#   dialogue/speaker  → 程序 integrity 独占 (引号归属是确定性的, 不让 LLM 抢)
#   focal_subject 等  → focal_director 独占
#   visual_must_haves → flux 独占
#   shot_type/转场     → visual 独占
#
# ⚠️ narration 的两家分工 (narrative + narration_flow 都能写, 但维度不重叠):
#   narrative      : 信息完整性 + 事实修正 (info_loss / fact_error)
#   narration_flow : 纯语义流 —— 主语明确 / 时空桥接 / 节奏均衡
#                    (已砍掉"重复检测"和"引号归属", 见 narration_flow_reviewer.py)
#   两者不会改同一类问题, 顺序上 narrative 先 / flow 后 (flow 看到 narrative 改完的结果)
NARRATIVE_FIELD_WHITELIST = {"narration"}
VISUAL_FIELD_WHITELIST    = {"shot_type", "transition_in", "kb_direction"}
# v2.5: focal_subject 改动权交给 focal_director,以下 reviewer 只产 issues
FLUX_FIELD_WHITELIST      = {"visual_must_haves"}
# v2.11: dialogue reviewer 不再写 dialogue —— 引号归属由程序 integrity 独占。
#        dialogue reviewer 降级为只报 issue (它擅长发现"台词被画成画面"这类问题,
#        但 speaker 归属是确定性的, 该程序做, 不该 LLM 三方混战)。
DIALOGUE_FIELD_WHITELIST  = {"visual_must_haves"}
COHERENCE_FIELD_WHITELIST = set()   # v2.5: 改产 issue,不直接 patch
# v2.12: 收权 —— characters/visible_characters/_region_prompts 移出。
#   这些是程序派生字段(storyboard_characters 产出 + to_pipeline 派生),
#   focal_director 不该写它们(会覆盖派生的正确值)。
#   _region_prompts 已废弃(新 router 读 render_characters 里的 en/region)。
FOCAL_DIRECTOR_FIELD_WHITELIST = {"focal_subject", "_pulid_chars", "_mask_hint"}
# v2.11: narration_flow 砍掉 dialogue 写权 (引号归属归 integrity)。
#        只保留 narration —— 且只改"主语/时空/节奏"三类语义问题 (维度1/2/5)。
NARRATION_FLOW_FIELD_WHITELIST = {"narration"}

REVIEW_PROMPT_VERSION = "v2.11.0"


# ════════════════════════════════════════════════════════════════
# LLM 调用工具
# ════════════════════════════════════════════════════════════════

def _call_llm(prompt: str, temperature: float = 0.2,
              model: Optional[str] = None) -> str:
    """统一 LLM 调用。失败抛异常。"""
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

    llm = ChatOpenAI(model=use_model, api_key=LLM_API_KEY,
                     base_url=LLM_BASE_URL, temperature=temperature)
    full = ""
    for chunk in llm.stream([HumanMessage(content=prompt)]):
        full += chunk.content
    return full


def _extract_json(text: str) -> Optional[dict]:
    """从 LLM 输出抠 JSON。"""
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


def _shots_to_review_format(shots: list) -> str:
    """shots → reviewer prompt 可读的格式"""
    lines = []
    for i, s in enumerate(shots):
        sh_id = f"sh{i+1:02d}"
        hold_mark = " [HOLD]" if s.get("_hold") else ""
        lines.append(f"[{sh_id}]{hold_mark} shot_type={s.get('shot_type', '?')} "
                     f"transition_in={s.get('transition_in', '?')} "
                     f"kb_direction={s.get('kb_direction', '?')}")
        lines.append(f"      narration: {s.get('narration', '')}")
        lines.append(f"      focal_subject: {s.get('focal_subject', '')}")
        haves = s.get('visual_must_haves', [])
        if haves:
            for h in haves:
                if isinstance(h, dict):
                    lines.append(f"      must_have: {h.get('element', '')} | "
                                 f"exclude: {h.get('exclude', '')}")
        dl = s.get('dialogue', [])
        if dl:
            for d in dl:
                if isinstance(d, dict):
                    lines.append(f"      dialogue: [{d.get('speaker', '?')}] "
                                 f"{d.get('text', '')}")
        lines.append("")
    return "\n".join(lines)


# ════════════════════════════════════════════════════════════════
# Reviewer 1: narrative
# ════════════════════════════════════════════════════════════════

NARRATIVE_PROMPT = """你是叙事编辑。检查分镜师改写后的旁白有没有【事实错误】或【顺序错乱】。

【你的职责 (v2.11 收窄)】
对比【原文】和【shots 的 narration】, 只找两类问题:
1. 事实改写错误:原文是 A,改写成 B (人名/地点/时间/数字 改错了)
2. 顺序错乱:原文 A→B→C 在 shots 里变成 A→C→B

【⚠️ 不归你管的事 (其他模块已处理, 不要重复)】
- 信息丢失 (道具/动作漏掉) → 程序 narration_integrity 已对账处理, 你不要管
- 引号台词归属 (把引号挪到 dialogue) → 程序 narration_integrity 已处理, 你绝不碰 dialogue
- 主语不明 / 缺过渡 / 节奏 → narration_flow reviewer 管
- focal_subject / shot_type / 长度 → 其他审稿员管
你【只】修 narration 里的"事实写错"和"顺序错乱"。

【⚠️ 你绝对不可以改的】
- 标记为 [HOLD] 的镜头 narration 一律不动 (hold 是精心设计的新增旁白)
- 不要把引号台词塞进 narration

【输出格式】严格 JSON:
{{
  "issues": [
    {{
      "shot_id": "sh03",
      "type": "fact_error" | "order_error",
      "description": "..."
    }}
  ],
  "patches": [
    {{
      "shot_id": "sh03",
      "field": "narration",
      "before": "...",
      "after": "...",
      "reason": "30 字内"
    }}
  ]
}}

只允许修改 field=narration。修订要保留简洁性(≤60字)。
【重要】只在确实写错事实时才 patch, before 必须跟原内容不同 (不要产出 before==after 的空操作)。
没问题返回 {{"issues": [], "patches": []}}。

═══════════════════════════════════════════════════════════════

【原文】
{body}

【shots 当前状态({n_shots} 镜头)】
{shots_dump}

只返回 JSON。"""


def review_narrative(shots, chapter, story_meta):
    if not shots:
        return {"issues": [], "patches": [], "reviewer": "narrative"}
    body = chapter.get("body", "")
    if not body:
        return {"issues": [], "patches": [], "reviewer": "narrative",
                "skipped": "no_body"}
    prompt = NARRATIVE_PROMPT.format(
        body=body[:3000], n_shots=len(shots),
        shots_dump=_shots_to_review_format(shots))
    try:
        raw = _call_llm(prompt, temperature=0.2)
    except Exception as e:
        return {"issues": [], "patches": [], "reviewer": "narrative",
                "skipped": f"llm_error: {e}"}
    parsed = _extract_json(raw)
    if not parsed:
        return {"issues": [], "patches": [], "reviewer": "narrative",
                "skipped": "json_parse_failed"}
    return {"issues": parsed.get("issues", []) or [],
            "patches": parsed.get("patches", []) or [],
            "reviewer": "narrative"}


# ════════════════════════════════════════════════════════════════
# Reviewer 2: visual
# ════════════════════════════════════════════════════════════════

VISUAL_PROMPT = """你是镜头组接师。检查分镜的镜头语言是否专业,相邻镜头是否自然衔接。

【你的职责】
1. 景别堆叠:连续 3 个以上 shot 用同一 shot_type → 节奏死板
2. transition 错配:
   - match_cut 用在毫无相似构图的两镜头之间
   - eyeline_match 但前后没有"看的人/被看的物"关系
   - whip_pan 用在情绪平缓段落

【⚠️ 关于 hold 的特殊规则】
hold 是分镜大师精心设计的"画面延续+旁白叠加"。
你**绝对不可以**把已有的 hold 改成其他 transition——分镜大师那么标是有理由的。

只在以下情况建议改 hold:
  (a) 章节首镜头被标了 hold(错的,应该 fade_in)
  (b) 全章 hold 比例超过 30%(节奏过死)
也不要主动把其他 transition 改成 hold。

3. kb_direction 冲突:
   - shot_type=closeup 但用 pan_left
   - 连续两个相同方向的 pan
4. 章节首镜头错配:
   - hook/setup 首镜头不用 fade_in
   - 章节中部出现 fade_in

【你不该管的】
- narration / focal_subject / visual_must_haves

【输出格式】严格 JSON:
{{
  "issues": [...],
  "patches": [
    {{
      "shot_id": "sh02",
      "field": "shot_type" | "transition_in" | "kb_direction",
      "before": "...",
      "after": "...",
      "reason": "..."
    }}
  ]
}}

约束:
  transition_in ∈ {{fade_in, match_cut, push_in, pull_out, eyeline_match, whip_pan, j_cut, hold}}
  kb_direction ∈ {{zoom_in, zoom_out, pan_left, pan_right, pan_up}}
  shot_type    ∈ {{extreme_wide, wide, medium, closeup, extreme_closeup}}

═══════════════════════════════════════════════════════════════

【章节信息】
arc_role: {arc_role}  tone: {tone}  位置: {chapter_position}

【shots 当前状态({n_shots} 镜头)】
{shots_dump}

只返回 JSON。"""


def review_visual(shots, chapter, story_meta):
    if not shots:
        return {"issues": [], "patches": [], "reviewer": "visual"}
    prompt = VISUAL_PROMPT.format(
        arc_role=chapter.get("arc_role", "rising"),
        tone=chapter.get("tone", "tension"),
        chapter_position=story_meta.get("chapter_position", "unknown"),
        n_shots=len(shots),
        shots_dump=_shots_to_review_format(shots))
    try:
        raw = _call_llm(prompt, temperature=0.2)
    except Exception as e:
        return {"issues": [], "patches": [], "reviewer": "visual",
                "skipped": f"llm_error: {e}"}
    parsed = _extract_json(raw)
    if not parsed:
        return {"issues": [], "patches": [], "reviewer": "visual",
                "skipped": "json_parse_failed"}
    return {"issues": parsed.get("issues", []) or [],
            "patches": parsed.get("patches", []) or [],
            "reviewer": "visual"}


# ════════════════════════════════════════════════════════════════
# Reviewer 3: flux
# ════════════════════════════════════════════════════════════════

FLUX_PROMPT = """你是 FLUX 图像模型 prompt 专家。检查每个 shot 的 focal_subject 是否能让 FLUX 生成专业级画面。

【FLUX 的弱点】
1. 文字易翻车:任何"票/信/书页/招牌/血字"上的文字
2. 主体抽象时画面无氛围:仅写"man on sofa"会出现棚式空洞画面
3. 负向词不全 → 现代物品污染

【⚠️ v2.5 重要变更:focal_subject 改动权统一给 focal_director】

你【不再直接 patch focal_subject】。你的任务是【发现问题、报告 issue】,
focal_director 会综合所有 issue 一次性写定 focal_subject。

【你应做 (v2.11 收窄: 聚焦你真正能改的)】
1. 你唯一能直接改的字段是 visual_must_haves[].exclude (文字回避)。
   扫描涉及文字介质 (信件/书/票/招牌/印章) 的镜头, 若 exclude 没排除文字,
   就 patch 加上 "readable text, written characters"。
2. focal_subject 的缺陷 (缺主体/缺光照) 只在【确实严重】时报 1 条 issue 给
   focal_director, 不要每镜都报。
   ⚠️ 不要报 "缺构图角度" 这类 issue —— 构图角度由 focal_director/shot_type 管,
      你报了也没人据此修, 只是噪音。

【你不应做】
- 直接 patch focal_subject(白名单已禁用,即使你输出 patch 也会被丢弃)
- 直接 patch shot_type / transition / narration
- 不要为每个镜头都报 issue (只报真正严重且你管得着的)

【输出格式】严格 JSON:
{{
  "issues": [
    {{"shot_id": "sh01", "type": "missing_subject|missing_lighting|missing_text_exclude|other",
      "description": "具体问题描述"}}
  ],
  "patches": [
    {{
      "shot_id": "sh01",
      "field": "visual_must_haves[N].exclude",
      "before": "...",
      "after": "...",
      "reason": "..."
    }}
  ]
}}

═══════════════════════════════════════════════════════════════

【故事视觉色调】
{visual_palette}

【shots 当前状态({n_shots} 镜头)】
{shots_dump}

只返回 JSON。"""


def review_flux(shots, chapter, story_meta):
    if not shots:
        return {"issues": [], "patches": [], "reviewer": "flux"}
    prompt = FLUX_PROMPT.format(
        visual_palette=story_meta.get("visual_palette", "未指定"),
        n_shots=len(shots),
        shots_dump=_shots_to_review_format(shots))
    try:
        raw = _call_llm(prompt, temperature=0.2)
    except Exception as e:
        return {"issues": [], "patches": [], "reviewer": "flux",
                "skipped": f"llm_error: {e}"}
    parsed = _extract_json(raw)
    if not parsed:
        return {"issues": [], "patches": [], "reviewer": "flux",
                "skipped": "json_parse_failed"}
    return {"issues": parsed.get("issues", []) or [],
            "patches": parsed.get("patches", []) or [],
            "reviewer": "flux"}


# ════════════════════════════════════════════════════════════════
# Reviewer 4: dialogue (v2.4 增强:speaker 归属)
# ════════════════════════════════════════════════════════════════

DIALOGUE_PROMPT = """你是镜头审查员。你【只】做一件事:
发现"角色台词被错误地写进了画面描述"(dialogue_painted_as_scene)。

【你只报这一类 issue, 不产任何 patch】
当某句【角色说的话】被塞进 narration 或 focal_subject 当画面描述,
而没放进 dialogue 字段时, 报一条 issue。

⚠️ 不归你管的事 (其他模块已处理, 你不要碰):
- dialogue.speaker 归属 / 引号迁移 → 程序 narration_integrity 独占
  你【绝对不要】产 speaker patch (会被权限矩阵拒, 白浪费)
- narration 事实 → narrative reviewer
- 重复 / 空镜 → 程序 dedup
你【只】报 dialogue_painted_as_scene issue, 【不产 patch】。

【已建模角色】
{characters_meta}

【本章 shots】(共 {n_shots} 镜)
{shots_dump}

【章节正文参考】
{body}

【输出】只输出 JSON:
{{
  "issues": [
    {{"shot_id": "sh03", "type": "dialogue_painted_as_scene",
      "description": "20字内: 哪句台词被画成了画面"}}
  ],
  "patches": []
}}
没问题返回 {{"issues": [], "patches": []}}。
现在严格按 JSON 输出。"""


def review_dialogue(shots, chapter, story_meta):
    if not shots:
        return {"issues": [], "patches": [], "reviewer": "dialogue"}
    body = chapter.get("body", "")
    if '"' not in body and '\u201c' not in body and '\u2018' not in body:
        return {"issues": [], "patches": [], "reviewer": "dialogue",
                "skipped": "no_quotes_in_body"}

    # 构造 character meta
    raw_chars = story_meta.get("characters", [])
    char_lines = []
    if isinstance(raw_chars, list):
        for c in raw_chars:
            if isinstance(c, dict) and c.get("name"):
                role = c.get("role", "lead")
                char_lines.append(f"- {c['name']} (role={role})")
    char_lines.append("- narrator (旁白)")
    char_lines.append("- narrator_quote (说书人代述)")
    characters_meta = "\n".join(char_lines) if char_lines else "未提供"

    prompt = DIALOGUE_PROMPT.format(
        body=body[:3000], n_shots=len(shots),
        shots_dump=_shots_to_review_format(shots),
        characters_meta=characters_meta)
    try:
        raw = _call_llm(prompt, temperature=0.2)
    except Exception as e:
        return {"issues": [], "patches": [], "reviewer": "dialogue",
                "skipped": f"llm_error: {e}"}
    parsed = _extract_json(raw)
    if not parsed:
        return {"issues": [], "patches": [], "reviewer": "dialogue",
                "skipped": "json_parse_failed"}
    return {"issues": parsed.get("issues", []) or [],
            "patches": parsed.get("patches", []) or [],
            "reviewer": "dialogue"}


# ════════════════════════════════════════════════════════════════
# Reviewer 5: coherence (v2.4 新增)
# ════════════════════════════════════════════════════════════════

COHERENCE_PROMPT = """你是分镜对齐与常识审查员。检查每个 shot 的画面 (focal_subject) 是否
同时满足两个维度:
  A. 忠实度:画面忠实表现 narration 的关键要素
  B. 常识逻辑:画面在物理/剧情逻辑上合理

═══════════════════════════════════════════════════════════════
维度 A:忠实度(focal_subject vs narration)
═══════════════════════════════════════════════════════════════

【核心判断】
1. 关键名词对应:narration 出现的关键物体/场所/角色,focal_subject 必须涵盖
2. 关键动作一致:narration 的动作 (挖/扒/砍/抱) 跟 focal_subject 的动作 (uncovering/digging) 不能错位
3. 关键状态吻合:narration 说"白骨"focal 不能说"完整人物"

【⚠️ 多焦点 narration 的处理(v2.4 重要)】
一段 narration 可能同时有多个画面要素都要画,例如:
  narration: "周建军劈开棺盖,戏服裹白骨,水袖抖开。"
  → 三个画面要素:周建军(动作主体)+ 白骨+戏服(棺中)+ 水袖抖动

正确做法不是"只画一个,忽略其他",而是【主次分层】:
  - 找出 narration 的【视觉重点】(通常是最后出现的、最戏剧化的要素)
  - 主焦点画清楚
  - 次焦点作为背景/边缘元素出现
  
✅ 正确 focal:
  "opened coffin revealing red opera costume draped over white skeletal bones,
   long water sleeves unfurled mid-air with motion blur, gold embroidery,
   Zhou Jianjun's blurred silhouette stepping back at frame edge"
  (棺中是主焦点,周建军作为模糊边缘元素)

❌ 错误 focal:
  "coffin lid being lifted, red opera costume with gold embroidery"
  (漏了"白骨"和"水袖抖动",信息缺失)

【典型病例】
病例 1(实际项目 ch01-sh01):
  narration: "周建军扒开潮湿的黄土,棺材红漆在夕阳下泛着血光"
  focal: "Zhou Jianjun uncovering a coffin lid"
  ❌ 病:"扒开黄土" vs "uncovering coffin lid" 是两个不同动作,narration 说棺材半埋在土里
  ✅ 修:"Zhou Jianjun crouching to brush dirt off a half-buried red-lacquered coffin,
         red paint glinting in setting sun, autumn 1985 Beijing construction site"

病例 2(实际项目 ch01-sh03):
  narration: "周建军劈开棺盖,戏服裹白骨,水袖哗啦啦抖开。"
  focal: "coffin lid being lifted, red opera costume with gold embroidery"
  ❌ 病:漏画"白骨"导致 FLUX 画了活人,且没有"抖动"
  ✅ 修(见上方多焦点例)

═══════════════════════════════════════════════════════════════
维度 B:常识逻辑
═══════════════════════════════════════════════════════════════

【核心判断】
画面描绘的内容在物理/常识/剧情逻辑上必须合理。

【典型常识违背】
1. 矛盾状态:
   - narration 说"死人"focal 写"活人站立" → 矛盾
   - narration 说"白骨"focal 写"完整身体" → 矛盾
   - narration 说"暴雨"focal 写"晴朗" → 矛盾

2. 物理不合理:
   - 棺材刚挖出来应该半埋土里,不是摆在地面
   - 1985 年北京工地不会出现古装道士
   - extra 配角的服装应该跟时代/场景一致

3. 隐含角色错位:
   - focal 出现 narration 没提的人物 → 删掉或改为环境元素
   - 例:narration 只说周建军,focal 出现"another man in the distance" → 删

【典型病例】
病例 3(实际项目 ch01-sh02):
  narration: '"头儿,这梅花瓣儿..."工人老赵话音未落,棺材缝里"咯吱"窜出一缕白毛'
  focal: "Old Zhao pointing at a coffin seam, a wisp of white hair emerging,
          dim construction site lighting"
  ❌ 病:没说时代 / 没说老赵穿什么 / 没说现场有棺材 → FLUX 画了古装道士在监牢
  ✅ 修:"Old Zhao in 1985 blue cotton work jacket and cap, weathered middle-aged
          face, pointing at a coffin seam where a wisp of white hair emerges,
          earthen pit around the coffin, dusk light, Beijing construction site"

═══════════════════════════════════════════════════════════════
维度 C(v2.4.3 新增):FLUX 不擅长画的画面元素 —— prompt 改写陷阱
═══════════════════════════════════════════════════════════════

【核心问题】
某些"X 的影子/剪影/倒影/残影"这类抽象、间接的画面元素,
FLUX 训练集里几乎没有"只画影子不画 X 本人"的样本,
导致 FLUX 把 X 本人也画出来,叙事张力崩塌。

【FLUX 画不好的高危元素清单】
1. "X 的影子" (X's shadow):FLUX 一定会把 X 也画出来
2. "X 的剪影" (X's silhouette):同上
3. "X 的倒影" (X's reflection):同上
4. "从 X 里冒出来的 Y" (Y emerging from X):FLUX 不擅长画"非物理实体冒出实体"
5. "看不见的 X" (invisible X):FLUX 不懂"看不见"
6. "X 的回忆/幻觉" (X's memory/hallucination):FLUX 当成实体画

【正确改写策略】
不要只说"X's shadow",要明确说"只画影子不画 X":
  ❌ "Lin Xiaohe's shadow on the ground, ghostly arm emerging from it"
     → FLUX 把林小荷本人也画进去
  ✅ "elongated humanoid shadow stretched across cracked stone floor,
      a translucent ghostly arm rising from the shadow's surface,
      ONLY THE SHADOW VISIBLE — no person in frame,
      low angle ground-level shot, dim moonlight"

关键改写元素:
  - 主体明确:"elongated humanoid shadow"(独立的影子形状,不挂 X 的所有格)
  - 排他声明:"only the shadow visible — no person in frame"
  - 低机位线索:"low angle ground-level shot"(暗示视角集中在地面)
  - exclude 字段加:"person in frame, full figure, character body"

【典型病例】
病例 4(实际项目 ch03-sh10):
  narration: "姑娘脚边影子正在蠕动,多出的那条鬼臂高举考古铲"
  focal: "Lin Xiaohe's shadow on the ground, a ghostly arm emerging from it,
          holding an archaeological shovel..."
  ❌ 病:"Lin Xiaohe's shadow" 导致 FLUX 把林小荷本人画出来,变成
         "白衣女郎漂浮空中举铲"的奇幻插画,叙事张力(影子里冒鬼手)消失。
  ✅ 修:"elongated humanoid shadow writhing across moonlit stone floor,
         translucent ghostly arm rising from within the shadow,
         clutching an old archaeological shovel with bloody flesh on blade,
         ONLY SHADOW AND ARM VISIBLE — no person or face in frame,
         low ground-level angle, dim moonlight"
  并把 visual_must_haves[].exclude 加入:
         "person, full body, character face, woman standing"

═══════════════════════════════════════════════════════════════
维度 D(v2.4.3 新增):prompt 卫生 —— FLUX 是文生图,不画"非视觉"内容
═══════════════════════════════════════════════════════════════

【硬规矩】focal_subject 是给 FLUX 的视觉 prompt,以下词一概不许出现:

1. 感官非视觉词(FLUX 看不懂,只会污染 embedding):
   - scent / smell / odor (气味)
   - sound / noise / echo (声音)
   - temperature / warm air / cold air (温度)
   - lingering / wafting (扩散感)

2. 元指令词(reviewer 自言自语,FLUX 会当画面内容画):
   - maintaining / preserving / improving / enhancing
   - credibility / consistency / precise composition

3. 反向修饰(FLUX 不擅长理解逆否):
   - "X but Y" / "X yet Y" / "X while still Y"

【典型病例】
病例 5(实际项目 ch03-sh10 真实翻车):
  原 focal: "...flesh stuck to the blade, dim moonlight casting eerie highlights"
  reviewer 改后: "...sound of the shovel blade"  ← 加了"声音"词
  ❌ 病:FLUX 看不懂"声音",这种词只会污染 prompt embedding,反而抢占主焦点
  ✅ 修:用视觉化的词替代:"shovel blade trembling mid-air, motion blur on blade tip"
         (用动作模糊代替"声音")

═══════════════════════════════════════════════════════════════

【你的职责】
对每个 shot 同时做 A + B 检查:
1. 列出 narration 关键要素 → 检查 focal 是否覆盖
2. 找出 focal 描绘的画面在常识上有没有矛盾或隐含错误
3. 如有问题,提供修订 focal_subject

【⚠️ 你不该管的】
- focal_subject 自身的 4 要素质量(flux 管)
- narration 是否完整(narrative 管)
- 文字回避(flux 管)
- 双 lead 同框(coordinator 处理)
- 镜头语言(visual 管)
- 标记为 [HOLD] 的镜头跳过(它复用上一镜画面,focal 改了也不生成)

【关键约束】
1. 修订必须保留原 focal_subject 不冲突的画面要素(光照、构图、衣物质感)
2. 30-80 词
3. 英文输出
4. 不主动加 narration 没有的角色(extra 除外)

【⚠️ v2.5 重要变更】
你【不再直接 patch focal_subject】。
你的任务是【发现问题、报告 issue】,focal_director 会综合你的 issue 一次性改写 focal。

【输出格式】严格 JSON(只 issues,patches 留空):
{{
  "issues": [
    {{
      "shot_id": "sh03",
      "dimension": "fidelity" | "commonsense",
      "type": "action_mismatch" | "missing_element" | "extra_element" | "state_contradiction" | "physical_implausible" | "era_mismatch" | "flux_unfriendly_element",
      "description": "narration 说 X,原 focal 说 Y,且 X 是 FLUX 难画的元素(如 X 的影子)",
      "suggested_strategy": "(可选)推荐 focal_director 的改写方向"
    }}
  ],
  "patches": []
}}

═══════════════════════════════════════════════════════════════

【故事背景(用于常识判断)】
visual_palette: {visual_palette}
故事概念: {concept}

【shots 当前状态({n_shots} 镜头)】
{shots_dump}

只返回 JSON。"""


def review_coherence(shots, chapter, story_meta):
    """v2.4 新增:focal vs narration 忠实度 + 常识逻辑
    v2.4.3 新增:patch 卫士 — 过滤 LLM 偶尔违反 D 维度规则的污染 patch。
    """
    if not shots:
        return {"issues": [], "patches": [], "reviewer": "coherence"}
    prompt = COHERENCE_PROMPT.format(
        visual_palette=story_meta.get("visual_palette", "未指定"),
        concept=story_meta.get("concept", "未指定"),
        n_shots=len(shots),
        shots_dump=_shots_to_review_format(shots))
    try:
        raw = _call_llm(prompt, temperature=0.2)
    except Exception as e:
        return {"issues": [], "patches": [], "reviewer": "coherence",
                "skipped": f"llm_error: {e}"}
    parsed = _extract_json(raw)
    if not parsed:
        return {"issues": [], "patches": [], "reviewer": "coherence",
                "skipped": "json_parse_failed"}

    # v2.4.3 patch 卫士: LLM 偶尔会违反维度 D 的 prompt 卫生规则,加感官词到 focal_subject
    raw_patches = parsed.get("patches", []) or []
    clean_patches = []
    for p in raw_patches:
        after_text = (p.get("after") or "")
        new_after, dropped = _sanitize_focal_patch(after_text)
        if dropped:
            print(f"  [coherence|sanitize] shot={p.get('shot_id')} "
                  f"剥掉污染词: {dropped}")
            p = {**p, "after": new_after}
        clean_patches.append(p)

    return {"issues": parsed.get("issues", []) or [],
            "patches": clean_patches,
            "reviewer": "coherence"}


# v2.4.3: focal_subject patch 卫士 — 跟 orchestrator._sanitize_rewritten_prompt
# 同理但更轻量(reviewer 阶段的 patch 通常是短补充,不做长度卫士,只做黑名单)
_FOCAL_BLACKLIST = (
    # 感官非视觉
    "scent", "smell", "odor",
    "sound of", "noise of", "echo of", "lingering",
    "temperature", "warm air", "cold air",
    # 元指令
    "maintaining", "preserving", "improving", "enhancing",
    "credibility", "precise composition",
    # 反向修饰
    "but structurally", "yet still", "while still",
)


def _sanitize_focal_patch(after_text: str) -> tuple:
    """
    扫 focal_subject patch 的 after 字段,剥掉黑名单段。
    返回 (清洗后文本, 被剥掉的片段列表)。

    剥粒度:按逗号切段,命中黑名单词的整段剥掉(因为坏词通常带一串污染修饰)。
    """
    if not after_text:
        return after_text, []
    segments = after_text.split(",")
    kept = []
    dropped = []
    for seg in segments:
        seg_low = seg.lower()
        hit = None
        for bad in _FOCAL_BLACKLIST:
            if bad in seg_low:
                hit = bad
                break
        if hit:
            dropped.append((seg.strip(), hit))
        else:
            kept.append(seg)
    cleaned = ",".join(kept).strip().strip(",").strip()
    # 清相邻逗号
    cleaned = re.sub(r"\s*,\s*,+", ", ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned, dropped


# ════════════════════════════════════════════════════════════════
# Reviewer 注册表
# ════════════════════════════════════════════════════════════════

# v2.5 新增:focal_director 导入(独立模块,跟 reviewer 同接口)
try:
    from focal_director import focal_director as _focal_director_fn
except ImportError:
    try:
        from .focal_director import focal_director as _focal_director_fn
    except ImportError:
        _focal_director_fn = None
        print("  [reviewers] ⚠ focal_director 模块不可用,focal_subject 将不被任何 reviewer 修改")

# v2.5: focal_director 必须在所有其他 reviewer 之后跑(它要看 issues)
# Python 3.7+ dict 保持插入顺序,所以这里的顺序就是跑的顺序
ALL_REVIEWERS = {
    "narrative":      review_narrative,
    "visual":         review_visual,
    "flux":           review_flux,        # v2.5: 只产 issues,不动 focal
    "dialogue":       review_dialogue,    # v2.5: 改 dialogue.speaker,不动 focal
    "coherence":      review_coherence,   # v2.5: 只产 issues,不动 focal
}
# 只有 focal_director 模块可用时才注册
if _focal_director_fn is not None:
    ALL_REVIEWERS["focal_director"] = _focal_director_fn   # v2.5: focal 唯一作者


REVIEWER_FIELD_WHITELIST = {
    "narrative":      NARRATIVE_FIELD_WHITELIST,
    "visual":         VISUAL_FIELD_WHITELIST,
    "flux":           FLUX_FIELD_WHITELIST,
    "dialogue":       DIALOGUE_FIELD_WHITELIST,
    "coherence":      COHERENCE_FIELD_WHITELIST,
    "focal_director": FOCAL_DIRECTOR_FIELD_WHITELIST,
    "narration_flow": NARRATION_FLOW_FIELD_WHITELIST,
}


# ════════════════════════════════════════════════════════════════
# Patch 应用器(v2.4 重写)
# ════════════════════════════════════════════════════════════════

def _shot_id_to_index(shot_id):
    m = re.match(r'sh(\d+)', shot_id or "")
    if not m:
        return None
    return int(m.group(1)) - 1


def apply_patches(shots, review_results, story_meta=None):
    """
    应用所有 reviewer 的 patches。v2.4 改动:
      1. 入口加 no-op 过滤
      2. hold 镜头 narration 不可改(任何 reviewer)
      3. dialogue 字段只允许 dialogue reviewer 改 dialogue[N].speaker 子路径
    v2.4.1 改动:
      4. dialogue[N].speaker 的新值必须落在合法白名单内
         (已建模角色中文名 / narrator / narrator_quote),
         拒绝英文名、拒绝未建模角色 —— 否则 to_pipeline 查不到 voice。
    """
    revision_log = []

    # v2.4.1: 构造 speaker 合法值白名单。
    # 必须是 outline 建模过的角色【中文名】,或两个旁白伪角色。
    _speaker_whitelist = {"narrator", "narrator_quote"}
    if story_meta:
        _raw_chars = story_meta.get("characters", []) or []
        if isinstance(_raw_chars, list):
            for _c in _raw_chars:
                if isinstance(_c, dict) and _c.get("name"):
                    _speaker_whitelist.add(str(_c["name"]).strip())
        elif isinstance(_raw_chars, dict):
            for _k in _raw_chars:
                _speaker_whitelist.add(str(_k).strip())

    for review in review_results:
        reviewer = review.get("reviewer", "unknown")
        whitelist = REVIEWER_FIELD_WHITELIST.get(reviewer, set())
        patches = review.get("patches", []) or []

        for patch in patches:
            if not isinstance(patch, dict):
                continue
            sh_id = patch.get("shot_id", "")
            field = patch.get("field", "")
            after = patch.get("after")
            reason = patch.get("reason", "")
            before = patch.get("before", "")

            # v2.4: no-op patch 过滤
            # v2.9.3: 完全 silent 不写 revision_log,避免上轮日志里"被拒绝的修订"
            #         清单出现 19 条 transition_in no-op 这种噪音。no-op 既不是
            #         bug 也不是有意义的修订,直接 skip 即可。
            # v2.11: 补 severity=skip 显式跳过 (focal_director 用 severity 标"无需改")
            if patch.get("severity") == "skip":
                continue
            if (not isinstance(after, (list, dict))
                and not isinstance(before, (list, dict))
                and str(before or "").strip() == str(after or "").strip()
                and str(before or "").strip()):
                continue

            idx = _shot_id_to_index(sh_id)
            if idx is None or idx < 0 or idx >= len(shots):
                revision_log.append({
                    "reviewer": reviewer, "shot_id": sh_id,
                    "field": field, "status": "rejected",
                    "reason": "shot_id 解析失败或越界",
                })
                continue

            base_field = field.split(".")[0].split("[")[0]
            if base_field not in whitelist:
                revision_log.append({
                    "reviewer": reviewer, "shot_id": sh_id,
                    "field": field, "status": "rejected",
                    "reason": f"{reviewer} 不允许改 {base_field}",
                })
                continue

            # v2.3.2: _hold_type 保护
            if base_field == "_hold_type":
                revision_log.append({
                    "reviewer": reviewer, "shot_id": sh_id,
                    "field": field, "status": "rejected",
                    "reason": "v2.3.2: _hold_type 不可改",
                })
                continue

            shot = shots[idx]

            # v2.4: hold 镜头 narration 不可改
            if field == "narration" and shot.get("_hold"):
                revision_log.append({
                    "reviewer": reviewer, "shot_id": sh_id,
                    "field": field, "status": "rejected",
                    "reason": "v2.4: hold 镜头 narration 由分镜大师决定",
                })
                continue

            # v2.4: dialogue 字段精细化保护
            if base_field == "dialogue":
                if reviewer != "dialogue":
                    revision_log.append({
                        "reviewer": reviewer, "shot_id": sh_id,
                        "field": field, "status": "rejected",
                        "reason": "v2.4: 仅 dialogue reviewer 可改 dialogue",
                    })
                    continue
                m = re.match(r'dialogue\[(\d+)\]\.speaker$', field)
                if not m:
                    revision_log.append({
                        "reviewer": reviewer, "shot_id": sh_id,
                        "field": field, "status": "rejected",
                        "reason": "v2.4: 仅允许 dialogue[N].speaker 子路径",
                    })
                    continue
                dl_list = shot.get("dialogue", []) or []
                dl_idx = int(m.group(1))
                if dl_idx < 0 or dl_idx >= len(dl_list):
                    revision_log.append({
                        "reviewer": reviewer, "shot_id": sh_id,
                        "field": field, "status": "rejected",
                        "reason": f"dialogue 索引 {dl_idx} 越界",
                    })
                    continue
                new_speaker = str(after).strip()
                if not new_speaker:
                    revision_log.append({
                        "reviewer": reviewer, "shot_id": sh_id,
                        "field": field, "status": "rejected",
                        "reason": "speaker 不能为空",
                    })
                    continue
                # v2.4.1: speaker 白名单校验。新值必须是已建模角色中文名 /
                # narrator / narrator_quote。拒绝英文名(如 Sun Laoguai)和
                # 未建模角色 —— 否则 to_pipeline 用中文名查 voice 会失败,
                # 该角色丢失专属配音。story_meta 缺失时跳过此校验(向下兼容)。
                if story_meta and new_speaker not in _speaker_whitelist:
                    revision_log.append({
                        "reviewer": reviewer, "shot_id": sh_id,
                        "field": field, "status": "rejected",
                        "reason": f"speaker '{new_speaker}' 非法 —— 必须是已建模"
                                  f"角色中文名 / narrator / narrator_quote。"
                                  f"(英文名会导致下游 voice 查找失败)",
                    })
                    continue
                old_speaker = dl_list[dl_idx].get("speaker", "")
                # v2.4.1: speaker 已正确归属角色时,不允许降级回 narrator。
                # 把已建模角色改成 narrator 会丢失角色配音,属于倒退。
                # 例外:old 是 narrator/narrator_quote 之间互转,或本就是
                # 未建模名(修正)—— 这些放行。
                if (new_speaker == "narrator"
                        and old_speaker
                        and old_speaker in _speaker_whitelist
                        and old_speaker not in ("narrator", "narrator_quote")):
                    revision_log.append({
                        "reviewer": reviewer, "shot_id": sh_id,
                        "field": field, "status": "rejected",
                        "reason": f"speaker '{old_speaker}' 已正确归属角色,"
                                  f"不允许降级为 narrator(会丢失角色配音)。"
                                  f"如确属旁白叙述,应在分镜大师源头修正",
                    })
                    continue
                dl_list[dl_idx]["speaker"] = new_speaker
                revision_log.append({
                    "reviewer": reviewer, "shot_id": sh_id,
                    "field": field, "status": "applied",
                    "before": old_speaker, "after": new_speaker,
                    "reason": reason,
                })
                shot.setdefault("_revision_notes", []).append({
                    "reviewer": reviewer, "field": field,
                    "issue": reason, "before": old_speaker, "after": new_speaker,
                })
                continue

            # 应用其他字段
            try:
                if field == "narration":
                    shot["narration"] = str(after).strip() if after else shot.get("narration", "")
                elif field == "shot_type":
                    if after in ("extreme_wide", "wide", "medium", "closeup", "extreme_closeup"):
                        shot["shot_type"] = after
                    else:
                        revision_log.append({
                            "reviewer": reviewer, "shot_id": sh_id,
                            "field": field, "status": "rejected",
                            "reason": f"无效 shot_type: {after}",
                        })
                        continue
                elif field == "transition_in":
                    valid = ("fade_in", "match_cut", "push_in", "pull_out",
                             "eyeline_match", "whip_pan", "j_cut", "hold")
                    if after not in valid:
                        revision_log.append({
                            "reviewer": reviewer, "shot_id": sh_id,
                            "field": field, "status": "rejected",
                            "reason": f"无效 transition_in: {after}",
                        })
                        continue
                    current = shot.get("transition_in", "")
                    if current == "hold" and after != "hold":
                        if idx == 0:
                            shot["transition_in"] = after
                        else:
                            revision_log.append({
                                "reviewer": reviewer, "shot_id": sh_id,
                                "field": field, "status": "rejected",
                                "reason": "v2.3: 不允许把 hold 改成 " + after,
                            })
                            continue
                    else:
                        shot["transition_in"] = after
                elif field == "kb_direction":
                    valid = ("zoom_in", "zoom_out", "pan_left", "pan_right", "pan_up")
                    if after in valid:
                        shot["kb_direction"] = after
                    else:
                        revision_log.append({
                            "reviewer": reviewer, "shot_id": sh_id,
                            "field": field, "status": "rejected",
                            "reason": f"无效 kb_direction: {after}",
                        })
                        continue
                elif field == "focal_subject":
                    shot["focal_subject"] = str(after).strip() if after else shot.get("focal_subject", "")
                elif field == "visual_must_haves":
                    existing = list(shot.get("visual_must_haves", []))

                    def _norm_have(h):
                        if isinstance(h, dict) and h.get("element"):
                            return {
                                "element":    str(h.get("element", "")).strip(),
                                "attributes": str(h.get("attributes", "")).strip(),
                                "state":      str(h.get("state", "")).strip(),
                                "mood":       str(h.get("mood", "")).strip(),
                                "exclude":    str(h.get("exclude", "")).strip(),
                            }
                        if isinstance(h, str) and h.strip():
                            return {"element": h.strip(), "attributes": "",
                                    "state": "", "mood": "", "exclude": ""}
                        return None

                    if isinstance(after, list):
                        clean = [n for n in [_norm_have(h) for h in after[:3]] if n]
                        shot["visual_must_haves"] = clean
                    elif isinstance(after, dict):
                        n = _norm_have(after)
                        if n:
                            existing.append(n)
                            shot["visual_must_haves"] = existing[:3]
                        else:
                            revision_log.append({
                                "reviewer": reviewer, "shot_id": sh_id,
                                "field": field, "status": "rejected",
                                "reason": "dict 缺少 element 字段",
                            })
                            continue
                    else:
                        revision_log.append({
                            "reviewer": reviewer, "shot_id": sh_id,
                            "field": field, "status": "rejected",
                            "reason": "visual_must_haves 类型不支持",
                        })
                        continue
                elif field.startswith("visual_must_haves["):
                    m = re.match(r'visual_must_haves\[(\d+)\]\.(\w+)', field)
                    if not m:
                        revision_log.append({
                            "reviewer": reviewer, "shot_id": sh_id,
                            "field": field, "status": "rejected",
                            "reason": "visual_must_haves 子路径格式错",
                        })
                        continue
                    vmh_idx = int(m.group(1))
                    sub_field = m.group(2)
                    if sub_field not in ("element", "attributes", "state", "mood", "exclude"):
                        revision_log.append({
                            "reviewer": reviewer, "shot_id": sh_id,
                            "field": field, "status": "rejected",
                            "reason": f"未知子字段 {sub_field}",
                        })
                        continue
                    vmh = shot.get("visual_must_haves", []) or []
                    if vmh_idx < 0 or vmh_idx >= len(vmh):
                        revision_log.append({
                            "reviewer": reviewer, "shot_id": sh_id,
                            "field": field, "status": "rejected",
                            "reason": f"visual_must_haves[{vmh_idx}] 越界",
                        })
                        continue
                    # v2.11: exclude 字段去重 (防止 "modern, modern, clear text" 累积)
                    new_val = str(after).strip()
                    if sub_field == "exclude" and new_val:
                        seen_w = set()
                        deduped_w = []
                        for w in new_val.split(","):
                            w = w.strip()
                            if w and w.lower() not in seen_w:
                                seen_w.add(w.lower())
                                deduped_w.append(w)
                        new_val = ", ".join(deduped_w)
                    vmh[vmh_idx][sub_field] = new_val
                elif field == "characters":
                    # v2.7: focal_director 补全出场角色名
                    if isinstance(after, list):
                        clean = [
                            str(c).strip() for c in after
                            if isinstance(c, str) and c.strip()
                        ]
                        # 去重保序
                        seen = set()
                        dedup = []
                        for c in clean:
                            if c not in seen:
                                seen.add(c)
                                dedup.append(c)
                        shot["characters"] = dedup
                    elif isinstance(after, str):
                        # 容错: 单个字符串当 list 处理
                        shot["characters"] = [after.strip()] if after.strip() else []
                    else:
                        revision_log.append({
                            "reviewer": reviewer, "shot_id": sh_id,
                            "field": field, "status": "rejected",
                            "reason": f"characters 必须是 list,得到 {type(after).__name__}",
                        })
                        continue
                elif field == "visible_characters":
                    # v2.10: focal_director 补全可见角色名
                    if isinstance(after, list):
                        clean = [
                            str(c).strip() for c in after
                            if isinstance(c, str) and c.strip()
                        ]
                        # 去重保序
                        seen = set()
                        dedup = []
                        for c in clean:
                            if c not in seen:
                                seen.add(c)
                                dedup.append(c)
                        shot["visible_characters"] = dedup
                    elif isinstance(after, str):
                        # 容错: 单个字符串当 list 处理
                        shot["visible_characters"] = [after.strip()] if after.strip() else []
                    else:
                        revision_log.append({
                            "reviewer": reviewer, "shot_id": sh_id,
                            "field": field, "status": "rejected",
                            "reason": f"visible_characters 必须是 list,得到 {type(after).__name__}",
                        })
                        continue

                elif field == "_pulid_chars":
                    # v2.7: focal_director 指定本镜启用 PuLID 的角色
                    if isinstance(after, list):
                        clean = [
                            str(c).strip() for c in after
                            if isinstance(c, str) and c.strip()
                        ]
                        shot["_pulid_chars"] = clean
                    else:
                        revision_log.append({
                            "reviewer": reviewer, "shot_id": sh_id,
                            "field": field, "status": "rejected",
                            "reason": f"_pulid_chars 必须是 list",
                        })
                        continue
                elif field == "_region_prompts":
                    # v2.7: focal_director 指定多角色的 region prompt
                    if isinstance(after, list):
                        clean = []
                        for rp in after:
                            if not isinstance(rp, dict):
                                continue
                            if not rp.get("character"):
                                continue
                            clean.append({
                                "character": str(rp.get("character", "")).strip(),
                                "en_name":   str(rp.get("en_name", "")).strip(),
                                "region":    str(rp.get("region", "")).strip(),
                                "prompt":    str(rp.get("prompt", "")).strip(),
                            })
                        shot["_region_prompts"] = clean
                    else:
                        revision_log.append({
                            "reviewer": reviewer, "shot_id": sh_id,
                            "field": field, "status": "rejected",
                            "reason": f"_region_prompts 必须是 list",
                        })
                        continue
                elif field == "_mask_hint":
                    # v2.7: focal_director 指定 mask 模板
                    # v2.9.3: 白名单从硬编码改为动态导入 mask_templates,确保
                    #         跟实际支持的模板永远对齐(以前 5 个里只有 1 个有效)。
                    try:
                        from core.mask_templates import MASK_TEMPLATES
                        valid_hints = set(MASK_TEMPLATES.keys()) | {""}
                    except ImportError:
                        # 兜底:用 v2.6+ 实际支持的 5 个 + 空串
                        valid_hints = {
                            "left_right", "face_to_face",
                            "over_shoulder_left", "over_shoulder_right",
                            "foreground_background", "",
                        }
                    if isinstance(after, str) and after.strip() in valid_hints:
                        shot["_mask_hint"] = after.strip()
                    else:
                        revision_log.append({
                            "reviewer": reviewer, "shot_id": sh_id,
                            "field": field, "status": "rejected",
                            "reason": (f"无效 _mask_hint: {after} "
                                       f"(合法值: {sorted(v for v in valid_hints if v)})"),
                        })
                        continue
                else:
                    revision_log.append({
                        "reviewer": reviewer, "shot_id": sh_id,
                        "field": field, "status": "rejected",
                        "reason": f"未实现 field {field}",
                    })
                    continue
            except Exception as e:
                revision_log.append({
                    "reviewer": reviewer, "shot_id": sh_id,
                    "field": field, "status": "rejected",
                    "reason": f"应用失败: {e}",
                })
                continue

            revision_log.append({
                "reviewer": reviewer, "shot_id": sh_id,
                "field": field, "status": "applied",
                "before": before, "after": after, "reason": reason,
            })
            shot.setdefault("_revision_notes", []).append({
                "reviewer": reviewer, "field": field,
                "issue": reason, "before": before, "after": after,
            })

    return shots, revision_log


# ════════════════════════════════════════════════════════════════
# 主入口:run_all_reviewers (v2.4 接入 coordinator)
# ════════════════════════════════════════════════════════════════

def _check_render_characters_chapter(shots, story_meta):
    """整章 render_characters 程序化校验 (v2.10)。

    不修改数据, 只检测并打印问题, 供人工观察/补 outline:
      1. 未建模角色名 (不在 story_meta.characters 里)
      2. 出镜角色缺英文描述 (en 空 → FLUX 画不像)

    依赖 storyboard_characters.build_character_index; 模块缺失则跳过。
    """
    try:
        try:
            from storyboard_characters import build_character_index
        except ImportError:
            from .storyboard_characters import build_character_index
    except ImportError:
        return  # 模块不在, 静默跳过

    char_index = build_character_index(story_meta)
    all_known = char_index["all"]
    en_map = char_index["en"]

    unmodeled = {}     # name -> 出现次数
    missing_en = {}    # name -> 出现次数
    for s in shots:
        rc = s.get("render_characters") or []
        if not isinstance(rc, list):
            continue
        for ch in rc:
            name = ch.get("name", "") if isinstance(ch, dict) else str(ch)
            name = (name or "").strip()
            if not name:
                continue
            if name not in all_known:
                unmodeled[name] = unmodeled.get(name, 0) + 1
            elif not en_map.get(name):
                # 已建模但角色表没写 en
                missing_en[name] = missing_en.get(name, 0) + 1

    if unmodeled:
        items = ", ".join(f"{n}×{c}" for n, c in unmodeled.items())
        print(f"  [render_check] ⚠ render_characters 含未建模角色: {items} "
              f"(建议补到 outline.characters)")
    if missing_en:
        items = ", ".join(f"{n}×{c}" for n, c in missing_en.items())
        print(f"  [render_check] ⚠ 出镜角色缺英文描述(en): {items} "
              f"(FLUX 对中文识别差, 建议在 outline 给这些角色补 en 字段)")
    if not unmodeled and not missing_en:
        n_multi = sum(1 for s in shots
                      if isinstance(s.get("render_characters"), list)
                      and len(s.get("render_characters")) >= 2)
        print(f"  [render_check] ✓ render_characters 校验通过 "
              f"(多角色镜 {n_multi} 个)")


def run_all_reviewers(shots, chapter, story_meta, enabled_reviewers=None):
    """
    v2.4 返回三元组 (shots, revision_log, auto_fixes_log)
    """
    if enabled_reviewers is None:
        enabled_reviewers = list(ALL_REVIEWERS.keys())

    # v2.4 flag 检查
    try:
        from config import ENABLE_V240_COORDINATOR
        coord_enabled = bool(ENABLE_V240_COORDINATOR)
    except (ImportError, AttributeError):
        coord_enabled = False

    # 日志档案
    log_archive = None
    sid = story_meta.get("story_id") or story_meta.get("title", "unknown")
    if sid:
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

    ch_id = chapter.get("id", "ch??")
    all_auto_fixes = []
    suspect_shots = {}

    # v2.4: pre_check
    if coord_enabled:
        try:
            # 双路径 import:script 模式用 `from coordinator`,
            # module 模式(python -m)用 `from .coordinator`
            try:
                from coordinator import pre_check
            except ImportError:
                from .coordinator import pre_check
            pre_result = pre_check(shots, chapter, story_meta)
            all_auto_fixes.extend(pre_result.get("auto_fixes", []))
            suspect_shots = pre_result.get("suspect_shots", {})
            n_pre = len(pre_result.get("auto_fixes", []))
            if n_pre:
                print(f"  [coordinator/pre] {n_pre} 项自动修复")
        except ImportError as e:
            print(f"  [coordinator/pre] 导入失败: {e}")
        except Exception as e:
            print(f"  [coordinator/pre] 异常: {e}")

    # 跑 reviewer
    # v2.5: 收集前置 reviewer 的 issues,在 focal_director 执行前注入到 story_meta
    review_results = []
    collected_issues_for_director = []   # 给 focal_director 读
    for name in enabled_reviewers:
        fn = ALL_REVIEWERS.get(name)
        if not fn:
            print(f"  [审稿] 未知审稿员: {name}")
            continue

        # v2.5: focal_director 跑之前,把前置 reviewer 的 issues 注入 story_meta
        # 这样 director 能看到 flux/coherence/dialogue 报告的问题再综合判断
        if name == "focal_director":
            story_meta = {**story_meta,
                          "_collected_other_issues": collected_issues_for_director}

        print(f"  [审稿] {name} 检查中...", end="", flush=True)
        import time as _time
        _t0 = _time.time()
        try:
            result = fn(shots, chapter, story_meta)
        except Exception as e:
            print(f" ❌ 异常: {e}")
            continue
        _duration_ms = int((_time.time() - _t0) * 1000)
        n_issues = len(result.get("issues", []))
        n_patches = len(result.get("patches", []))
        skip = result.get("skipped")
        if skip:
            print(f" ⏭ 跳过 ({skip})")
        else:
            print(f" {n_issues} 问题 / {n_patches} 修订")
        review_results.append(result)

        # v2.5: 累计 issues 给 focal_director 看(focal_director 自己产的 issues 不再收集)
        if name != "focal_director":
            for it in result.get("issues", []) or []:
                if isinstance(it, dict):
                    # 给 issue 加来源标签,方便 director 区分
                    collected_issues_for_director.append({**it, "reviewer": name})
                else:
                    # 字符串型 issue(visual reviewer 有些是字符串)
                    collected_issues_for_director.append(
                        {"reviewer": name, "description": str(it)})

        if log_archive:
            try:
                log_archive.write(
                    stage=f"reviewer.{name}", chapter_id=ch_id,
                    input={"n_shots": len(shots),
                           "shots_summary": [
                               {"shot_type": s.get("shot_type", ""),
                                "narration": (s.get("narration", "") or "")[:60]}
                               for s in shots[:5]]},
                    output={"issues": result.get("issues", [])[:8],
                            "patches": result.get("patches", [])[:8],
                            "skipped": skip},
                    decision={"n_issues": n_issues, "n_patches": n_patches},
                    duration_ms=_duration_ms,
                )
            except Exception:
                pass

    # ── v2.9.4: narration_flow 全章视野旁白流审稿 ──────────────────
    # 默认 off,需在 config.py 设 ENABLE_NARRATION_FLOW_REVIEWER = True 才跑。
    # 字段白名单 = {"narration"} (方案 A,见 NARRATION_FLOW_FIELD_WHITELIST 注释)。
    # 跑在所有 reviewer 之后、apply_patches 之前,这样它能看到 reviewer 修订后的
    # narration 串成全章流,做最后一道连贯性把关。
    try:
        try:
            from narration_flow_reviewer import (
                review_narration_flow, is_enabled as _flow_enabled,
            )
        except ImportError:
            from .narration_flow_reviewer import (
                review_narration_flow, is_enabled as _flow_enabled,
            )
        if _flow_enabled():
            print(f"  [审稿] narration_flow 检查中...", end="", flush=True)
            import time as _time
            _t0 = _time.time()
            flow_result = review_narration_flow(shots, chapter, story_meta)
            _duration_ms = int((_time.time() - _t0) * 1000)
            n_issues = len(flow_result.get("issues", []))
            n_patches = len(flow_result.get("patches", []))
            skip = flow_result.get("skipped")
            if skip:
                print(f" ⏭ 跳过 ({skip})")
            else:
                print(f" {n_issues} 问题 / {n_patches} 修订")
            review_results.append(flow_result)
            if log_archive:
                try:
                    log_archive.write(
                        stage="reviewer.narration_flow", chapter_id=ch_id,
                        input={"n_shots": len(shots)},
                        output={"issues": flow_result.get("issues", [])[:8],
                                "patches": flow_result.get("patches", [])[:8],
                                "skipped": skip},
                        decision={"n_issues": n_issues, "n_patches": n_patches},
                        duration_ms=_duration_ms,
                    )
                except Exception:
                    pass
    except ImportError:
        pass   # narration_flow_reviewer.py 不存在则静默跳过
    except Exception as e:
        print(f"  [审稿] narration_flow 异常: {e}")

    shots, revision_log = apply_patches(shots, review_results, story_meta)

    # ── v2.11: narration_integrity 信息守恒 (程序层, 引号归属唯一出口) ──────
    # 在所有 LLM reviewer + apply_patches 之后跑, 对账最终结果:
    #   Layer 1+2 (程序): 抽取原文关键信息(引号/道具/悬念/事件) → 对账缺失
    #   引号迁移 (程序):  narration 里的引号自动归 dialogue (确定性, 不让 LLM 抢)
    #   Layer 3 (按需1次LLM): 仍有 critical 缺失时定向补回
    # 这是"信息守恒发现"和"引号归属"的唯一出口, narrative/flow 不再碰这两件事。
    try:
        try:
            from narration_integrity import (
                is_enabled as _integ_enabled,
                extract_chapter_must_preserve, enforce_integrity, repair_with_llm,
            )
        except ImportError:
            from .narration_integrity import (
                is_enabled as _integ_enabled,
                extract_chapter_must_preserve, enforce_integrity, repair_with_llm,
            )
        if _integ_enabled():
            print(f"  [审稿] narration_integrity 信息守恒检查中...", end="", flush=True)
            must_preserve = extract_chapter_must_preserve(chapter, story_meta)
            audit = enforce_integrity(shots, must_preserve)   # 程序迁移引号(in-place)
            for line in audit.to_log_lines():
                print()
                print(line, end="")
            print()
            # Layer 3: 仍有 critical 缺失 → 定向 LLM 修复 (可选, 由 config 控制)
            try:
                from config import ENABLE_INTEGRITY_LLM_REPAIR
                _repair_on = bool(ENABLE_INTEGRITY_LLM_REPAIR)
            except (ImportError, AttributeError):
                _repair_on = False
            if _repair_on and audit.has_critical_missing():
                print(f"  [integrity] 调 LLM 定向修复 ({len(audit.missing)} 项缺失)")
                shots, _ok = repair_with_llm(shots, audit, story_meta)
            if log_archive:
                try:
                    log_archive.write(
                        stage="reviewer.narration_integrity", chapter_id=ch_id,
                        input={"n_shots": len(shots)},
                        output={"missing": [(m.seg_id, m.category, m.content)
                                            for m in audit.missing][:12],
                                "auto_fixed": audit.auto_fixed[:12]},
                        decision={"n_missing": len(audit.missing),
                                  "n_auto_fixed": len(audit.auto_fixed)},
                    )
                except Exception:
                    pass
    except ImportError:
        pass   # narration_integrity.py 不存在则静默跳过
    except Exception as e:
        print(f"  [审稿] narration_integrity 异常(忽略): {e}")

    # ── v2.10: render_characters 程序化校验 (不调 LLM, 只检查并打印) ──
    # 检查两件事 (交接 MD 第三节):
    #   1. render_characters 含未建模角色名 (storyboard 应已剔除, 这里兜底报告)
    #   2. 出镜角色缺英文描述 en (outline 没写 en → FLUX 画不像, 预警补 outline)
    try:
        _check_render_characters_chapter(shots, story_meta)
    except Exception as e:
        print(f"  [render_check] 异常(忽略): {e}")

    # v2.4: post_check
    if coord_enabled:
        try:
            try:
                from coordinator import post_check
            except ImportError:
                from .coordinator import post_check
            shots, revision_log, post_fixes = post_check(
                shots, revision_log, chapter, story_meta)
            all_auto_fixes.extend(post_fixes)
            if post_fixes:
                print(f"  [coordinator/post] {len(post_fixes)} 项二次检查")
        except ImportError as e:
            print(f"  [coordinator/post] 导入失败: {e}")
        except Exception as e:
            print(f"  [coordinator/post] 异常: {e}")

    return shots, revision_log, all_auto_fixes