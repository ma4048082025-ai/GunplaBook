"""
focal_director.py
=================
分镜阶段的"总把关大师" — focal_subject 字段的唯一作者。

设计目的:
  历史上 flux / coherence / dialogue 三个 reviewer 都改 focal_subject,
  顺序覆盖导致 prompt 被叠加污染(medieval / open mouths / 元指令 等)。
  
  v2.5: focal_subject 改为单一出口 —— 此 director 综合所有上下文,一次性
  写定 focal_subject。其他 reviewer 只产 issues 不直接 patch focal。

职责(独占):
  1. focal_subject 字段的所有改动权(包括微调、重写、保留)
  2. 决定每镜启用哪些角色的 PuLID(写到 _pulid_chars)
  3. 处理 FLUX 不擅长画的元素(影子/抽象动作/数量精确等),自动改写
  4. 综合其他 reviewer 报告的 issues

输入:
  shots, chapter, story_meta + theme profile
  其他 reviewer 已经产出的 issues(可读不可执行)

输出:
  {issues, patches, reviewer: "focal_director"}
  patches 中 field 全部是 "focal_subject" 或 "_pulid_chars"

接口:
  跟其他 reviewer 完全一致:def focal_director(shots, chapter, story_meta) -> dict
  这样 run_all_reviewers 的调度代码不需要改。

智能体接入(future):
  本模块依赖 _call_llm 跟其他 reviewer 共享(在 reviewers.py),
  将来要换智能体只需要替换 _call_llm 实现,本模块无感。
  详见 FOCAL_DIRECTOR_CONTRACT.md。
"""

import json
import re
from typing import Optional


# ════════════════════════════════════════════════════════════════
# 核心 prompt
# ════════════════════════════════════════════════════════════════

FOCAL_DIRECTOR_PROMPT = """你是分镜阶段的【总导演】。你的唯一职责:为每一镜重写或微调 focal_subject。

═══════════════════════════════════════════════════════════════
你掌握的信息(其他 reviewer 不掌握的)
═══════════════════════════════════════════════════════════════

【故事整体】
标题: {title}
时代/地点: {era}  
概念: {concept}
视觉色调: {visual_palette}

【角色谱(已生成定妆的角色可用 PuLID 注入)】
{character_summary}

【其他 reviewer 报告的 issues(参考,不必逐条服从)】
{other_issues}

═══════════════════════════════════════════════════════════════
你的核心准则
═══════════════════════════════════════════════════════════════

1.【时代锚定】用故事真实时代,绝不乱填。本主题自然的时代锚词:
   {natural_era}
   绝对禁止的词(LLM 常乱填):
   {forbidden_era}
   如果某镜的 focal_subject 含禁用词,必须改写。

2.【FLUX 不擅长的元素】检测到这些 narration 元素,主动改写 focal:
   {flux_unfriendly_patterns}

3.【prompt 卫生】focal_subject 绝对不能含:
   {blacklist}
   遇到这些词,改写或剥离。

4.【长度控制】focal_subject 30-80 词。超长的 focal 抢主体,FLUX 会失焦。

5.【主体清晰】focal 必须明确说"画什么主体":
   ❌ "scene continues from: <长串前镜描述>"  → FLUX 会画前镜内容
   ❌ "Lin Xiaohe's shadow on the ground"  → FLUX 画的是林小荷本人
   ✅ 直接说画面主体,不写"scene continues from"
   ✅ 影子用 "elongated humanoid shadow + no person in frame"

6.【角色识别 + PuLID 注入 —— 以 render_characters 为唯一准则 ⭐v2.11】
   每镜的 render_characters 字段告诉你"画面实际出现谁",严格照它办:

   ★ render_characters = "空[]——纯景物/道具/部位特写":
     这是纯景物/道具/身体部位特写镜(如"手部特写""信封特写""空镜")。
     - focal_subject 【绝对禁止】出现任何角色英文名或面部锚点(凤眼/眉疤/发型等)
     - _pulid_chars 【必须】留空
     - 即使 narration 提到某人,也只画 narration 描述的那个【物/部位】
     ❌ narration"他抓铁索的手,小指缺半截" + render=空
        → 错误:"Han Yanqing (phoenix eyes, scar) gripping cable"(画成脸)
     ✅ → 正确:"a calloused hand gripping iron cable, missing half of little
           finger, extreme closeup, weathered skin"(只画手)

   ★ render_characters = 1 个角色名:
     画这个角色的脸/上半身。focal 用 en_name + 面部锚点,_pulid_chars 标这个角色。

   ★ render_characters = 2+ 个角色名:见规则 7(多角色)。

   ★ render_characters = "未知(老格式...)":
     字段缺失(老数据),退回旧判断:narration 明确画脸/上半身才注锚点,
     道具/部位特写不注。

7.【多角色镜专项处理 v2.12 ⭐】
   当 render_characters 有 2+ 个角色(对视/互动/同框):
   你【只】负责 focal_subject 文本质量 + _mask_hint, 【不要】碰角色名单字段
   (characters / visible_characters / render_characters 全部由程序派生,
    你写了也会被权限矩阵拒)。

   (c) focal_subject 里每个角色后【紧跟视觉锚点】(发型+神态+服饰),
       让 FLUX 有两套锚点可区分:
       ❌ "Pei Shisha tracing Shen Jinqiong's lips"  (两人画成同一张脸)
       ✅ "left: Pei Shisha (long dark hair, cold gaze);
           right: Shen Jinqiong (short topknot, wide-eyed)"
       锚点从角色 face_features / character_archetypes 取。

   (e) _mask_hint 字段(可选, 提示下游 mask 模板):
       face_to_face / left_right / over_shoulder_left /
       over_shoulder_right / foreground_background

   (f) ★ 构图中性化(防脸对脸贴额头):
       【禁止】写把两脸拉一起的构图词:
       ❌ looking up at / facing each other / leaning close /
          foreheads touching / close-up of the two / gazing at
       ✅ 中性并置 + 各自锚点 + 各自区域(left: / right:)

8.【default 不动】如果当前 focal 已经合理,severity 标 "skip",不必硬改。

═══════════════════════════════════════════════════════════════
输入:本章的所有 shots
═══════════════════════════════════════════════════════════════

{shots_dump}

═══════════════════════════════════════════════════════════════
输出格式(严格 JSON)
═══════════════════════════════════════════════════════════════

{{
  "issues": [
    {{"shot_id": "sh01", "type": "era_pollution|sensory_word|flux_unfriendly|character_unclear|too_long|multichar_no_anchor|other",
     "description": "..."}}
  ],
  "patches": [
    {{
      "shot_id": "sh01",
      "field": "focal_subject",
      "before": "原 focal",
      "after":  "改写后 focal(30-80 词,英文)",
      "reason": "为什么改",
      "severity": "critical|minor|skip"
    }},
    {{
      "shot_id": "sh01",
      "field": "_pulid_chars",
      "before": null,
      "after": ["陈援朝"],
      "reason": "本镜画陈援朝面部特写,启用 PuLID"
    }},
    /* v2.12: 你【只】产 focal_subject / _pulid_chars / _mask_hint 三类 patch。
       角色名单字段(characters / visible_characters / render_characters)由程序
       从 render_characters 派生, 你写了会被丢弃。_region_prompts 已废弃
       (下游读 render_characters 里的 en/region)。 */
    /* v2.6 多角色镜: 只给 _mask_hint, 角色区分靠 focal_subject 里的视觉锚点 */
    {{
      "shot_id": "sh02",
      "field": "_mask_hint",
      "before": null,
      "after": "face_to_face",
      "reason": "两人面对面,中心融合带(left_right/over_shoulder_left/over_shoulder_right/foreground_background 任选)"
    }}
  ]
}}

只返回 JSON,不要任何代码块标记,不要其他说明。"""


# ════════════════════════════════════════════════════════════════
# 主入口 — 跟其他 reviewer 同接口
# ════════════════════════════════════════════════════════════════

def focal_director(shots, chapter, story_meta) -> dict:
    """v2.5 总把关大师。focal_subject + _pulid_chars 的唯一作者。

    接口跟其他 reviewer 完全一致,returns {issues, patches, reviewer}。

    内部步骤:
      1. 加载主题 profile
      2. 收集其他 reviewer 产出的 issues(从 story_meta["other_issues"] 读,
         若没有则跳过)
      3. 拼 prompt,调 LLM
      4. 解析 patches
      5. diff 卫士(长度、黑名单、禁用 era 词二次清洗)
    """
    if not shots:
        return {"issues": [], "patches": [], "reviewer": "focal_director"}

    # 1. 加载 theme profile
    profile = _load_theme_profile_for_story(story_meta)

    # 2. 收集其他 reviewer 的 issues(可选)
    other_issues = story_meta.get("_collected_other_issues", [])

    # 3. 构造 prompt
    prompt = _build_director_prompt(shots, chapter, story_meta, profile, other_issues)

    # 4. 调 LLM(跟其他 reviewer 共享 _call_llm,智能体接入时换它)
    try:
        from reviewers import _call_llm, _extract_json
    except ImportError:
        try:
            from .reviewers import _call_llm, _extract_json
        except ImportError:
            return {"issues": [], "patches": [], "reviewer": "focal_director",
                    "skipped": "import_error"}

    try:
        raw = _call_llm(prompt, temperature=0.2)
    except Exception as e:
        return {"issues": [], "patches": [], "reviewer": "focal_director",
                "skipped": f"llm_error: {e}"}

    parsed = _extract_json(raw)
    if not parsed:
        return {"issues": [], "patches": [], "reviewer": "focal_director",
                "skipped": "json_parse_failed"}

    # 5. diff 卫士:清洗所有 patch 的 after 字段
    raw_patches = parsed.get("patches", []) or []
    clean_patches = []
    # v2.12: focal_director 只能产这三类 field 的 patch。
    #   角色名单(characters/visible_characters/render_characters)由程序派生,
    #   _region_prompts 已废弃。越权 patch 直接丢, 双保险(prompt + 代码)。
    _FD_ALLOWED = {"focal_subject", "_pulid_chars", "_mask_hint"}
    for p in raw_patches:
        fld = p.get("field", "")
        base_fld = fld.split("[")[0].split(".")[0]   # 兼容 dialogue[0].x 形式
        if base_fld not in _FD_ALLOWED:
            print(f"  [focal_director] 丢弃越权 patch field={fld!r} "
                  f"(shot={p.get('shot_id','?')})")
            continue
        if fld != "focal_subject":
            # _pulid_chars / _mask_hint 类型直接放行
            clean_patches.append(p)
            continue
        after = p.get("after") or ""
        cleaned, dropped = _sanitize_focal(after, profile)
        if dropped:
            print(f"  [focal_director|sanitize] {p.get('shot_id')} 剥掉: {dropped}")
        if cleaned != after:
            p = {**p, "after": cleaned}
        clean_patches.append(p)

    return {
        "issues": parsed.get("issues", []) or [],
        "patches": clean_patches,
        "reviewer": "focal_director",
    }


# ════════════════════════════════════════════════════════════════
# 工具:加载主题 profile
# ════════════════════════════════════════════════════════════════

def _load_theme_profile_for_story(story_meta: dict):
    """从 story_meta 推导 theme_id,加载对应 profile。"""
    try:
        from theme_profile import load_profile
    except ImportError:
        try:
            from .theme_profile import load_profile
        except ImportError:
            # 找不到 theme_profile 时,返回 mock 让 director 走通用 fallback
            class _Mock:
                theme_id = "default"
                natural_era_words = []
                forbidden_era_words = []
                style_base = ""
                flux_unfriendly_patterns = []
                blacklist_keywords = []
                character_archetypes = {}
            return _Mock()

    theme_id = (story_meta.get("theme_id") or
                story_meta.get("outline", {}).get("theme_id") or
                "default")
    return load_profile(theme_id)


# ════════════════════════════════════════════════════════════════
# 工具:构造 prompt
# ════════════════════════════════════════════════════════════════

def _build_director_prompt(shots, chapter, story_meta, profile, other_issues) -> str:
    """拼 prompt。"""
    # 角色摘要
    chars = story_meta.get("characters", []) or []
    char_lines = []
    for c in chars:
        if isinstance(c, dict):
            role = c.get("_role") or c.get("role", "?")
            name = c.get("name") or "?"
            en_name = c.get("en_name", "")
            has_portrait = bool(c.get("portrait_ref") or c.get("_portrait_ref"))
            features = c.get("face_features") or c.get("key_features", "")
            portrait_mark = " [✓PuLID可用]" if has_portrait else ""
            char_lines.append(
                f"  [{role}] {name} (en={en_name}): {features}{portrait_mark}"
            )
    char_summary = "\n".join(char_lines) or "(无角色谱)"

    # 其他 reviewer 的 issues
    issue_lines = []
    for it in other_issues[:20]:  # 限制条数
        rv = it.get("reviewer", "?")
        desc = it.get("description") or str(it)[:100]
        sid = it.get("shot_id", "?")
        issue_lines.append(f"  [{rv}|{sid}] {desc}")
    other_issues_str = "\n".join(issue_lines) or "(其他 reviewer 暂无 issues)"

    # shots 摘要(focal director 关心 focal 本身,不需要太多细节)
    shots_lines = []
    for i, s in enumerate(shots):
        sid = f"sh{i+1:02d}"
        focal = s.get("focal_subject", "")
        narr = s.get("narration", "")
        st = s.get("shot_type", "")
        chars_in_shot = s.get("characters") or []
        visible_chars_in_shot = s.get("visible_characters") or []  # 新增：获取 visible_characters
        # v2.11: render_characters = 画面实际出现谁(唯一真相)。
        #   区分三态:
        #     有值      → 画这些角色(注脸/PuLID)
        #     空列表 [] → 纯景物/道具/部位特写 → 禁注脸禁 PuLID
        #     字段缺失  → 老格式, 降级按 narration 判断(不能当纯景物)
        _rc_raw = s.get("render_characters", "__MISSING__")
        if _rc_raw == "__MISSING__":
            render_str = "未知(老格式,请按 narration 判断是否露脸)"
        else:
            _rc_names = []
            for rc in (_rc_raw or []):
                if isinstance(rc, dict) and rc.get("name"):
                    _rc_names.append(rc["name"])
                elif isinstance(rc, str) and rc.strip():
                    _rc_names.append(rc.strip())
            if _rc_names:
                render_str = ",".join(_rc_names)
            else:
                render_str = "空[]——纯景物/道具/部位特写,禁注脸禁PuLID"
        dialogue = s.get("dialogue") or []
        dlg_str = ""
        if dialogue:
            dlg_str = " | dialogue=" + ",".join(
                f"{d.get('speaker','?')}:{d.get('text','')[:30]}" for d in dialogue)
        chars_str = ",".join(chars_in_shot) if chars_in_shot else "无"
        visible_str = ",".join(visible_chars_in_shot) if visible_chars_in_shot else "无"
        shots_lines.append(
            f"\n--- {sid} ---\n"
            f"shot_type: {st}\n"
            f"render_characters: {render_str}\n"
            f"characters: {chars_str}\n"
            f"visible_characters: {visible_str}\n"
            f"narration: {narr}\n"
            f"focal_subject: {focal}{dlg_str}"
        )
    shots_dump = "\n".join(shots_lines)

    # 时代信息
    era = (story_meta.get("era") or
           story_meta.get("outline", {}).get("era") or
           story_meta.get("concept", "")[:60] or
           "未指定")

    natural_era = ", ".join(profile.natural_era_words) or "(未限定)"
    forbidden_era = ", ".join(profile.forbidden_era_words) or "(无)"

    # FLUX 不擅长画的元素
    if profile.flux_unfriendly_patterns:
        flux_unfriendly = "\n".join(
            f"  - 包含 \"{kw}\" → {strategy}"
            for kw, strategy in profile.flux_unfriendly_patterns
        )
    else:
        flux_unfriendly = "  (无主题特定模式)"

    blacklist = ", ".join(profile.blacklist_keywords) or "(无)"

    return FOCAL_DIRECTOR_PROMPT.format(
        title=story_meta.get("title", ""),
        era=era,
        concept=story_meta.get("concept", "")[:120],
        visual_palette=story_meta.get("visual_palette", ""),
        character_summary=char_summary,
        other_issues=other_issues_str,
        natural_era=natural_era,
        forbidden_era=forbidden_era,
        flux_unfriendly_patterns=flux_unfriendly,
        blacklist=blacklist,
        shots_dump=shots_dump,
    )


# ════════════════════════════════════════════════════════════════
# 工具:diff 卫士(LLM 偶尔违反规则时的兜底)
# ════════════════════════════════════════════════════════════════

def _sanitize_focal(focal: str, profile) -> tuple:
    """对 focal_subject 做最后清洗。返回 (清洗后, 被剥掉的段)。

    清洗策略(按段切,有命中的整段剥):
      1. 黑名单词
      2. 禁用 era 词(profile.forbidden_era_words)
      3. 长度超 90 词(粗截断)
    """
    if not focal:
        return focal, []

    blacklist = list(profile.blacklist_keywords or [])
    blacklist += list(profile.forbidden_era_words or [])
    blacklist = [b.lower() for b in blacklist]

    segments = focal.split(",")
    kept = []
    dropped = []
    for seg in segments:
        seg_low = seg.lower()
        hit = None
        for bad in blacklist:
            if bad and bad in seg_low:
                hit = bad
                break
        if hit:
            dropped.append((seg.strip(), hit))
        else:
            kept.append(seg)
    cleaned = ",".join(kept).strip().strip(",").strip()
    cleaned = re.sub(r"\s*,\s*,+", ", ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned)

    # 长度兜底(只在严重超长时硬截)
    words = cleaned.split()
    if len(words) > 90:
        cleaned = " ".join(words[:80])
        dropped.append(("(长度超 90 词,硬截 80)", "len_guard"))

    return cleaned, dropped