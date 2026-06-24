"""
tools/long_writer/narration_flow_reviewer.py ── 旁白流连贯性审稿
================================================================
独立 reviewer，作用域:整章 narration 串联起来的"故事流"。

不同于 narrative reviewer(只看信息完整性)、coherence reviewer
(看 focal_subject vs narration 忠实度),本 reviewer **只看一件事**:

  所有 shots 的 narration 串起来,能不能像短文一样独立读懂。

5 个检查维度,对应铁律 24:
  1. 主语明确(代词指代清楚)
  2. 时空桥接(场景切换有过渡词)
  3. 重复事件标明
  4. 关键转折用 dialogue
  5. 节奏均衡(字数差 ≤ 3 倍)

部署方式:
  默认 off,需要在 reviewers.py 的 run_reviewers 链最后追加调用。
  独立模块,不影响现有 reviewer。

为什么独立成文件:
  - reviewers.py 已经 1300+ 行,不再扩
  - 这个 reviewer 是"全章视野",跟 narrative 的"shot 级"职责不同
  - 可独立启停、独立调参,不污染主流程
"""

from __future__ import annotations
import json
import re
from typing import Optional


# ════════════════════════════════════════════════════════════════
# Feature flag
# ════════════════════════════════════════════════════════════════

def is_enabled() -> bool:
    """读 config.ENABLE_NARRATION_FLOW_REVIEWER, 默认 off."""
    try:
        from config import ENABLE_NARRATION_FLOW_REVIEWER
        return bool(ENABLE_NARRATION_FLOW_REVIEWER)
    except (ImportError, AttributeError):
        return False


# ════════════════════════════════════════════════════════════════
# Reviewer Prompt
# ════════════════════════════════════════════════════════════════

FLOW_PROMPT = """你是旁白流编辑。你的唯一任务: 检查整章 shots 的 narration
串起来能否像短文一样独立读懂。

【输入】你会看到这一章的 shots,每个 shot 有 narration 和 dialogue。

【你只管 3 个维度 —— 纯语义流, 程序做不了的事】

1. 主语明确
   每句的"他/她/它"是否指代清楚? 跨 shot 重新出现的角色首次提及
   应该用全名,不要直接"他"开头。
   - 反例: p3 narration "砚台墨汁起涟漪"(谁的砚台?)
   - 正例: p3 narration "沈墨白案头砚台墨汁起涟漪"

2. 时空桥接
   场景切换处(地点/时间变化)是否有桥接词("循着声响"/"片刻后"/
   "踏上石阶后")? 不要前一句在巷口,后一句直接到钟楼。
   - 反例: p4(巷口拽袖)→ p5(踩石阶,无过渡)
   - 正例: p5 加桥接 "他挣开手,循着钟声拾级而上"

3. 节奏均衡
   相邻两镜 narration 字数差不超过 3 倍。前镜 50 字,下镜 5 字会
   像急刹车。允许的修复方式: 给短镜适度展开,而不是缩短长镜。
   - 反例: p13(50 字)→ p14 "他指尖划过这行字"(7 字)
   - 正例: p14 改为 "他指尖划过这行字,墨香混着血腥"

【⚠️ 不归你管的事 (其他模块已处理, 你不要碰)】
- 重复检测/标"再次" → 程序 dedup 已处理, 不要改
- 引号对话归属 (把引号挪到 dialogue) → 程序 integrity 已处理, 不要碰 dialogue
- 信息完整性/事实修正 → narrative reviewer 已处理
你【只】改 narration 字段的"主语/时空/节奏"三类问题。

【输出】只输出 JSON,严格格式:

{{
  "issues": [
    {{
      "shot_id": "sh03",
      "dim": "subject_clarity" | "spatiotemporal" | "rhythm",
      "description": "20 字内说明"
    }}
  ],
  "patches": [
    {{
      "shot_id": "sh03",
      "field": "narration",
      "before": "原内容",
      "after": "改后内容",
      "reason": "30 字内"
    }}
  ]
}}

【关键约束】
- patches 只允许改 narration 字段, 绝不碰 dialogue (那是 integrity 的事)
- 改 narration 时保持 ≤60 字, 且【不要】塞引号对话
- 没问题返回 {{"issues": [], "patches": []}}
- patches 最多 5 条,只挑最影响理解的

═══════════════════════════════════════════════════════════════
【已建模角色】
{characters_csv}

【本章 shots】
{shots_block}

【整段 narration 流(把 shots 串起来,这是你的主要审查对象)】
{narration_flow}
═══════════════════════════════════════════════════════════════

现在,严格按 JSON 输出。"""


# ════════════════════════════════════════════════════════════════
# 主入口
# ════════════════════════════════════════════════════════════════

# v2.11: 砍掉 dialogue —— 引号归属由 narration_integrity 独占。
# flow_reviewer 只改 narration 的主语/时空/节奏。
FLOW_FIELD_WHITELIST = {"narration"}


def review_narration_flow(shots: list,
                            chapter: dict,
                            story_meta: dict) -> dict:
    """整章 narration 流审稿。

    Args:
        shots:       本章已经过其他 reviewer 修订的 shot 列表
        chapter:     chapter 元信息
        story_meta:  story 元信息

    Returns:
        {
          "issues":   [...],
          "patches":  [...],
          "reviewer": "narration_flow",
          "skipped":  Optional[str]  # 若跳过,说明原因
        }
    """
    if not is_enabled():
        return {"issues": [], "patches": [], "reviewer": "narration_flow",
                "skipped": "feature_flag_off"}

    if not shots:
        return {"issues": [], "patches": [], "reviewer": "narration_flow",
                "skipped": "no_shots"}

    # 已建模角色名(让 LLM 知道哪些 speaker 合法)
    chars = story_meta.get("characters") or []
    if isinstance(chars, list) and chars and isinstance(chars[0], dict):
        char_names = [c.get("name", "") for c in chars]
    elif isinstance(chars, dict):
        char_names = list(chars.keys())
    else:
        char_names = list(chars) if isinstance(chars, list) else []
    char_names = [c for c in char_names if c]
    characters_csv = ", ".join(char_names) if char_names else "(无)"

    # 构建 shots_block 和 narration_flow
    shots_block_lines = []
    flow_lines = []
    for s in shots:
        sh_id = s.get("title", "").split("-")[-1] if "-" in s.get("title", "") else f"sh??"
        narr = (s.get("narration") or "").strip()
        dlgs = s.get("dialogue", []) or []

        shots_block_lines.append(f"\n[{sh_id}]")
        if narr:
            shots_block_lines.append(f"  narration: {narr}")
        for d in dlgs:
            spk = d.get("speaker", "?")
            text = (d.get("text") or "").strip()
            shots_block_lines.append(f"  dialogue: [{spk}] {text}")

        # flow 只串 narration(主要审查对象)
        if narr:
            flow_lines.append(f"[{sh_id}] {narr}")
    shots_block = "\n".join(shots_block_lines)
    narration_flow = "\n".join(flow_lines)

    # 调 LLM
    try:
        from config import LLM_API_KEY, LLM_BASE_URL, LLM_MODEL
        from langchain_openai import ChatOpenAI
        from langchain_core.messages import HumanMessage
    except ImportError as e:
        return {"issues": [], "patches": [], "reviewer": "narration_flow",
                "skipped": f"import_failed: {e}"}

    prompt = FLOW_PROMPT.format(
        characters_csv=characters_csv,
        shots_block=shots_block,
        narration_flow=narration_flow,
    )

    try:
        llm = ChatOpenAI(
            model=LLM_MODEL, api_key=LLM_API_KEY, base_url=LLM_BASE_URL,
            temperature=0.3,
            max_tokens=4096,
            timeout=180,
        )
        full = ""
        for chunk in llm.stream([HumanMessage(content=prompt)]):
            full += chunk.content
    except Exception as e:
        print(f"  [narration_flow] LLM 调用失败,跳过: {e}")
        return {"issues": [], "patches": [], "reviewer": "narration_flow",
                "skipped": f"llm_error: {e}"}

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
    except json.JSONDecodeError as e:
        print(f"  [narration_flow] JSON 解析失败,跳过: {e}")
        return {"issues": [], "patches": [], "reviewer": "narration_flow",
                "skipped": f"json_parse_failed: {e}"}

    # 清洗 patches(白名单 + 截断 narration ≤60 字)
    raw_patches = parsed.get("patches", []) or []
    clean_patches = []
    for p in raw_patches[:5]:  # 最多 5 条
        if not isinstance(p, dict):
            continue
        field = p.get("field")
        if field not in FLOW_FIELD_WHITELIST:
            continue
        # narration 限长
        if field == "narration":
            after = str(p.get("after") or "").strip()
            if len(after) > 60:
                after = after[:60]
            p = {**p, "after": after}
        clean_patches.append(p)

    return {
        "issues":   parsed.get("issues", []) or [],
        "patches":  clean_patches,
        "reviewer": "narration_flow",
    }


# ════════════════════════════════════════════════════════════════
# 接入状态 (v2.9.4 已接入主流程)
# ════════════════════════════════════════════════════════════════
# 本 reviewer 已在 reviewers.py 的 run_all_reviewers 中正确接入:
# 跑在所有 5 个标准 reviewer 之后、apply_patches 之前,
# 让它看到 reviewer 修订后的 narration 串成全章流,做最后一道连贯性把关。
#
# 开关方式(在 config.py):
#     ENABLE_NARRATION_FLOW_REVIEWER = True
#
# 字段权限(reviewers.py 的 NARRATION_FLOW_FIELD_WHITELIST):
#     仅允许 patch narration 字段(方案 A 限制)
#     即使本 reviewer 输出了 dialogue 路径的 patch,会被 apply_patches 拒绝并
#     记入 revision_log,以便观测 LLM 想加多少 dialogue 项作为后续解锁决策依据。
#
# ── 历史:之前版本注释里的接入示例提到 _apply_patch_list 函数,
#         那是误指 —— reviewers.py 实际只有 apply_patches(shots, review_results),
#         本次 v2.9.4 接入是把 flow_result append 到 review_results 走统一路径。
# ════════════════════════════════════════════════════════════════
