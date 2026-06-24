"""
coordinator.py ── reviewer 前置/后置确定性协调器 (v2.4.0 新增)
============================================================
作用:在分项 LLM reviewer 跑之前和跑之后做检查,解决两类问题:
  1. 跨 reviewer 的协同(narration 去重需要在 reviewer 改完后再跑一次)
  2. reviewer 自己跑错的兜底(no-op patch、相邻重复、双 lead 翻车)

设计原则:
  - 90% 确定性逻辑(字符串匹配 / 正则 / 相似度)
  - 仅在"双 lead 同框"自动改写时调用一次轻量 LLM(batch 模式)
  - 任何失败不阻塞主流程(LLM 失败 → 性别泛指回退 → "the other person" 兜底)

返回数据契约:
  pre_check(shots, chapter, story_meta) -> {
    "auto_fixes":   [...],   # 已应用的自动修复记录(用于打印)
    "suspect_shots": {       # 提示 reviewer 重点关注
      "narrative": [sh_idx, ...], ...
    }
  }
"""

import re
import json
from difflib import SequenceMatcher


# ════════════════════════════════════════════════════════════════
# 工具:角色英文名映射 + 性别映射
# ════════════════════════════════════════════════════════════════

def _build_lead_en_map(story_meta: dict) -> dict:
    """
    构建 lead 角色中文名 → 英文名候选 list 的映射。
    返回 {"陈远征": ["Chen Yuanzheng", ...], ...}
    """
    raw_chars = story_meta.get("characters")
    if raw_chars is None:
        raw_chars = []

    chars_list = []
    if isinstance(raw_chars, list):
        chars_list = raw_chars
    elif isinstance(raw_chars, dict):
        chars_list = [{"name": k, **(v if isinstance(v, dict) else {})}
                      for k, v in raw_chars.items()]

    leads_map = {}
    for c in chars_list:
        if not isinstance(c, dict):
            continue
        name = c.get("name", "")
        role = (c.get("role", "lead") or "lead").lower()
        if not name or role != "lead":
            continue

        en_candidates = []
        if c.get("en_name"):
            en_candidates.append(c["en_name"].strip())
        if c.get("trigger_solo"):
            ts = c["trigger_solo"].strip()
            for suffix in ("_solo", "_face", "_portrait"):
                if ts.endswith(suffix):
                    ts = ts[:-len(suffix)]
                    break
            parts = ts.split("_")
            en_candidates.append(" ".join(p.capitalize() for p in parts))

        if not en_candidates:
            en_candidates.append(name)

        seen = set()
        unique = []
        for cand in en_candidates:
            if cand and cand not in seen:
                seen.add(cand)
                unique.append(cand)
        leads_map[name] = unique
    return leads_map


def _build_lead_gender_map(story_meta: dict) -> dict:
    """中文名 → gender (male/female/unknown)"""
    raw_chars = story_meta.get("characters") or []
    chars_list = raw_chars if isinstance(raw_chars, list) else []
    gender_map = {}
    for c in chars_list:
        if not isinstance(c, dict):
            continue
        name = c.get("name")
        gender = (c.get("gender") or "").lower()
        if name and gender in ("male", "female"):
            gender_map[name] = gender
        elif name:
            gender_map[name] = "unknown"
    return gender_map


# ════════════════════════════════════════════════════════════════
# 检查 1: 双 lead 同框扫描
# ════════════════════════════════════════════════════════════════

def _scan_dual_leads_in_focal(focal_subject: str,
                              leads_en_map: dict) -> list:
    """
    扫 focal_subject 中出现了几个 lead。返回命中的中文角色名列表(去重,保持顺序)。

    匹配策略:
      - 英文名 ≥ 4 字符,用 \\b 单词边界匹配,大小写不敏感
      - 中文名直接 in 检查(中文无大小写问题)
      - 短英文名(< 4 字符,如 "Lin")跳过,避免误伤
    """
    if not focal_subject:
        return []
    text = focal_subject
    hits = []
    for cn_name, en_list in leads_en_map.items():
        matched = False
        for en in en_list:
            if not en:
                continue
            # 中文名匹配
            if any(ord(ch) > 127 for ch in en):
                if en in text:
                    matched = True
                    break
                continue
            # 英文名:必须 ≥ 4 字符 + 单词边界
            if len(en) >= 4 and re.search(
                    r'\b' + re.escape(en) + r'\b', text, re.IGNORECASE
            ):
                matched = True
                break
        if matched:
            hits.append(cn_name)
    return hits


# ── 双 lead 改写策略 ─────────────────────────────────────────
# C: LLM batch 重写(优先,质量最高)
# B: 性别泛指回退("the male teammate" / "the female teammate")
# A: 终极兜底("the other person")
# ─────────────────────────────────────────────────────────────

def _gender_to_generic(gender: str) -> str:
    """
    性别 → 泛指英文。
    male → "the male companion"
    female → "the female companion"
    unknown → "the other person"
    """
    if gender == "male":
        return "the male companion"
    if gender == "female":
        return "the female companion"
    return "the other person"


def _rewrite_focal_fallback(focal_subject: str,
                            removed_leads_en: list,
                            removed_leads_gender: list) -> str:
    """
    B + A 回退:把 removed leads 的英文名换成性别泛指,
    性别 unknown 时用 "the other person"。

    removed_leads_en:    [["Lin Hongying"], ...]  每个 lead 的英文名候选
    removed_leads_gender: ["female", ...]         每个 lead 的性别

    两个 list 顺序一一对应。
    """
    text = focal_subject
    for ens, gender in zip(removed_leads_en, removed_leads_gender):
        replacement = _gender_to_generic(gender)
        for en in ens:
            if not en or len(en) < 4:
                continue
            if any(ord(ch) > 127 for ch in en):
                text = text.replace(en, replacement)
                continue
            text = re.sub(
                r'\b' + re.escape(en) + r'\b',
                replacement, text, flags=re.IGNORECASE
            )
    return text


def _llm_batch_rewrite_dual_leads(dual_lead_shots: list,
                                  leads_en_map: dict,
                                  gender_map: dict,
                                  chapter: dict) -> dict:
    """
    C 方案:批量调 LLM 重写本章所有双 lead 同框的 focal_subject。

    输入 dual_lead_shots: [{"shot_idx": i, "focal": "...",
                            "kept": "陈远征", "removed": ["林红英"]}, ...]
    返回 {shot_idx: new_focal}; LLM 失败返回空 dict,上层应回退到 B/A
    """
    if not dual_lead_shots:
        return {}

    try:
        from config import LLM_API_KEY, LLM_BASE_URL, LLM_MODEL
        from langchain_openai import ChatOpenAI
        from langchain_core.messages import HumanMessage
    except ImportError as e:
        print(f"  [coordinator] LLM 模块导入失败,回退性别泛指: {e}")
        return {}

    # 构造 prompt
    items_text = []
    for s in dual_lead_shots:
        items_text.append(
            f'  - shot_idx={s["shot_idx"]}\n'
            f'    focal_subject: "{s["focal"]}"\n'
            f'    keep_lead: {s["kept"]} (英文名 {leads_en_map.get(s["kept"], [s["kept"]])[0]})\n'
            f'    remove_leads: {s["removed"]} '
            f'(性别 {[gender_map.get(r, "unknown") for r in s["removed"]]})'
        )

    prompt = f"""你是 FLUX prompt 优化师。以下 focal_subject 违反了"单 lead 原则"——
一个画面里出现了多个有 LoRA 的主角,会导致 FLUX 生图翻车。

任务:把每个 focal_subject 改写成单 lead 镜头,移除的 lead 用以下泛指替换:
  - male  → "the male companion"
  - female → "the female companion"
  - unknown → "the other person"

【铁律】
1. 保留 keep_lead 的英文名不动
2. 移除 remove_leads 的英文名,用对应性别泛指替代
3. 保留原 focal_subject 的剩余画面要素(光照、构图、动作、道具)
4. 改写后的英文要自然,不能机械直译
5. 只返回 JSON 字典 {{"shot_idx": "new focal_subject", ...}},不要任何 markdown 或解释

【示例】
原:"Chen Yuanzheng gripping Lin Hongying's wrist, blood beads on watch"
keep=陈远征, remove=[林红英](female)
改:"Chen Yuanzheng gripping the female companion's wrist, blood beads on watch"

【待改写的 shots】
{chr(10).join(items_text)}

只返回严格 JSON 字典,key 是 shot_idx 字符串,value 是改写后的 focal_subject。"""

    try:
        llm = ChatOpenAI(model=LLM_MODEL, api_key=LLM_API_KEY,
                         base_url=LLM_BASE_URL, temperature=0.1)
        full = ""
        for chunk in llm.stream([HumanMessage(content=prompt)]):
            full += chunk.content
    except Exception as e:
        print(f"  [coordinator] LLM 调用失败,回退性别泛指: {e}")
        return {}

    # 抠 JSON
    text = full
    if "```" in text:
        m = re.search(r'```(?:json)?\s*(\{.*?\})\s*```', text, re.DOTALL)
        if m:
            text = m.group(1)
        else:
            text = text.replace("```json", "").replace("```", "")
    try:
        parsed = json.loads(text.strip())
    except json.JSONDecodeError:
        print(f"  [coordinator] LLM 返回 JSON 解析失败,回退性别泛指")
        return {}

    if not isinstance(parsed, dict):
        return {}

    # 校验:返回的每个 new_focal 不能仍含 removed lead 名(检测幻觉)
    result = {}
    for s in dual_lead_shots:
        key = str(s["shot_idx"])
        new_focal = parsed.get(key) or parsed.get(s["shot_idx"])
        if not isinstance(new_focal, str) or not new_focal.strip():
            continue
        # 校验:没把要移除的 lead 还留着
        leftover = False
        for r in s["removed"]:
            for en in leads_en_map.get(r, []):
                if len(en) >= 4 and re.search(
                        r'\b' + re.escape(en) + r'\b', new_focal, re.IGNORECASE
                ):
                    leftover = True
                    break
            if leftover:
                break
        if leftover:
            print(f"  [coordinator] LLM 改写后仍含 removed lead,sh{s['shot_idx'] + 1:02d} 回退")
            continue
        result[s["shot_idx"]] = new_focal.strip()
    return result


# ════════════════════════════════════════════════════════════════
# 检查 2: narration 包 dialogue.text
# ════════════════════════════════════════════════════════════════

_QUOTE_VARIANTS_FMT = [
    '"{}"', "'{}'",
    '\u201c{}\u201d',  # 中文左右双引号
    '\u2018{}\u2019',  # 中文左右单引号
    '\u300c{}\u300d',  # 直角括号「」
    '\u300e{}\u300f',  # 直角括号『』
]

# 字符串 strip 用的引号字符集(全部唯一,无重复)
_QUOTE_CHARS_SET = (
    '"\''  # ASCII
    '\u201c\u201d\u2018\u2019'  # 中文引号
    '\u300c\u300d\u300e\u300f'  # 直角括号
)


def _strip_dialogue_from_narration(shot: dict) -> bool:
    """从 narration 中剥离 dialogue.text。返回是否实际改动。"""
    narration = shot.get("narration", "")
    dialogue = shot.get("dialogue", [])
    if not narration or not dialogue:
        return False
    original = narration

    for dl in dialogue:
        if not isinstance(dl, dict):
            continue
        text = (dl.get("text") or "").strip()
        if not text:
            continue
        variants = [fmt.format(text) for fmt in _QUOTE_VARIANTS_FMT]
        variants.append(text)
        for v in variants:
            if v and v in narration:
                narration = narration.replace(v, "")
                break

    # 清理:引号字符 strip + 标点 strip + 双逗号合并
    narration = narration.strip(_QUOTE_CHARS_SET + " ,。、!?\t\n")
    narration = re.sub(r'^[,。、!?\s]+', '', narration)
    narration = re.sub(r'[,、\s]+$', '', narration)
    narration = re.sub(r'[,。]{2,}', '。', narration)
    narration = narration.strip()

    if narration != original.strip():
        shot["narration"] = narration
        return True
    return False


# ════════════════════════════════════════════════════════════════
# 检查 3: 相邻 narration 相似度
# ════════════════════════════════════════════════════════════════

def _narration_similarity(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a.strip(), b.strip()).ratio()


def _find_similar_adjacent(shots: list, threshold: float = 0.7) -> list:
    """返回 [(idx_a, idx_b, similarity), ...]。只检测,不修改。"""
    pairs = []
    for i in range(len(shots) - 1):
        a = shots[i].get("narration", "") or ""
        b = shots[i + 1].get("narration", "") or ""
        if len(a.strip()) < 8 or len(b.strip()) < 8:
            continue
        sim = _narration_similarity(a, b)
        if sim >= threshold:
            pairs.append((i, i + 1, sim))
    return pairs


# ════════════════════════════════════════════════════════════════
# 检查 3b (v2.4.1): 相邻 narration 真正去重(会写回!)
# ════════════════════════════════════════════════════════════════

def _dedup_adjacent_narration(shots: list, threshold: float = 0.85,
                              window: int = 3) -> list:
    """
    narration 去重(v2.4.2:窗口去重,可抓隔句重复)。

    旧版只比 shots[i] 与 shots[i+1],隔一句/两句的重复抓不到
    (典型:同一 segment 被拆成 4 镜,sh04 把 sh02+sh03 整段抄回来)。
    新版对每个镜头,跟它前面 window 个镜头组成的"已说内容"逐一比对,
    把所有已说过的子串一次剥掉。

    判据说明:窗口去重不能用 SequenceMatcher 相似度 —— 隔句重复时
    当前镜还含别的内容,整体 ratio 会很低。所以这里用【子串包含】作
    唯一判据:前面某镜 narration 去标点后 >=8 字,整段出现在当前镜
    去标点形态里 → 判定重复。相似度仅作辅助(完全近似的相邻镜)。

    策略:
      - 把当前镜里所有"已说过"的子串剥掉,得到 remainder(增量内容)。
      - remainder 仍有实质内容(>=6 字) → narration 改为 remainder。
      - remainder 过短 → 普通镜转 extend hold;cutaway 镜只清空
        narration(画面保留,不引入新状态)。

    返回 [{shot_idx, before, after, action}, ...] 修复记录。
    """
    fixes = []

    def _strip_punct(s: str) -> str:
        # 覆盖中英标点 + 破折号 + 省略号(—…－~ 等连接符也要剥,
        # 否则 '——前方...' 这种残留会让子串匹配失败)
        return re.sub(
            r'[，。、！？,.!?\s"\'\u201c\u201d\u2018\u2019'
            r'\u2014\u2015\u2026\u2025\-~·]', '', s)

    for i in range(1, len(shots)):
        nxt = shots[i]
        b = (nxt.get("narration") or "").strip()
        if len(b) < 8:
            continue

        # extend hold / silent_beat 不单独配音,跳过
        nxt_is_extend = (nxt.get("_hold") and
                         (nxt.get("_hold_type") or "extend") == "extend")
        if nxt_is_extend or nxt.get("silent_beat"):
            continue
        nxt_is_cutaway = (nxt.get("_hold") and
                          (nxt.get("_hold_type") or "") == "cutaway")

        # 收集窗口内"前面的镜头"作为已说内容来源
        win_start = max(0, i - window)
        prev_shots = []
        for k in range(win_start, i):
            pa = (shots[k].get("narration") or "").strip()
            if len(pa) >= 8:
                prev_shots.append((k, pa))
        if not prev_shots:
            continue

        # 逐一剥离:窗口内每个前镜的 narration,若其去标点形态被当前镜
        # 去标点形态包含,就从当前镜里把对应原文子串剥掉。
        remainder = b
        hit_from = []          # 命中的前镜 idx,用于报告
        best_sim = 0.0
        for k, pa in prev_shots:
            sim = _narration_similarity(pa, remainder)
            best_sim = max(best_sim, sim)
            pa_core = _strip_punct(pa)
            rem_core = _strip_punct(remainder)
            # 双向包含判定:
            #   前镜整段在当前镜里(当前镜抄了前镜)→ 剥前镜那段
            #   当前镜整段在前镜里(当前镜是前镜的子句)→ 当前镜整个是重复
            overlap_core = None
            if len(pa_core) >= 8 and pa_core in rem_core:
                overlap_core = pa_core
            elif len(rem_core) >= 8 and rem_core in pa_core:
                overlap_core = rem_core
            if overlap_core:
                # 在带标点的 remainder 上剥离实际重叠原文
                overlap = _longest_common_substring(pa, remainder)
                if overlap and len(_strip_punct(overlap)) >= 8:
                    remainder = remainder.replace(overlap, "", 1)
                    hit_from.append(k)
            # 高相似度的相邻镜(近义改写)也处理
            elif sim >= threshold:
                overlap = _longest_common_substring(pa, remainder)
                if overlap and len(overlap) >= 6:
                    remainder = remainder.replace(overlap, "", 1)
                    hit_from.append(k)

        if not hit_from:
            continue

        # 标点清理
        remainder = re.sub(
            r'^[,，。、!！?？\s"\'\u201c\u201d\u2018\u2019]+', '', remainder)
        remainder = re.sub(
            r'[,，、\s"\'\u201c\u201d\u2018\u2019]+$', '', remainder)
        remainder = re.sub(r'[,，。]{2,}', '。', remainder).strip()

        hit_desc = "/".join(f"sh{k+1:02d}" for k in hit_from)

        if nxt_is_cutaway:
            # cutaway 有独立画面 —— 但"独立画面 + 空旁白 + 空对话"在节奏里
            # 仍然是 4 秒空镜,会引起字幕错位和观感卡顿。
            # v2.4.4:分诊处理
            #   - remainder 还有内容 → 缩减为增量内容(画面 + 增量旁白)
            #   - remainder 空 + 有 dialogue → 保留为 cutaway(角色对话支撑这一镜)
            #   - remainder 空 + dialogue 也空 → narration 留空,由 _purge_empty_shots 删除
            nxt["narration"] = remainder
            if len(remainder) >= 6:
                fixes.append({
                    "shot_idx": i, "before": b[:80],
                    "after": remainder[:80],
                    "action": f"narration 与窗口内 {hit_desc} 重复,"
                              f"cutaway 缩减为增量内容(画面保留)",
                })
            else:
                nxt["narration"] = ""
                nxt_has_dialogue = bool(nxt.get("dialogue"))
                if nxt_has_dialogue:
                    fixes.append({
                        "shot_idx": i, "before": b[:80],
                        "after": "(narration 清空,cutaway 画面 + dialogue 保留)",
                        "action": f"narration 与窗口内 {hit_desc} 重复,"
                                  f"cutaway 清空 narration(有 dialogue 支撑)",
                    })
                else:
                    fixes.append({
                        "shot_idx": i, "before": b[:80],
                        "after": "(narration 清空,无 dialogue,将被 _purge_empty_shots 删除)",
                        "action": f"narration 与窗口内 {hit_desc} 重复 且 无 dialogue → "
                                  f"标记为空镜头,post_check 末尾会删除",
                    })
            continue

        if len(remainder) < 6:
            # 普通镜完全重复 → 转 extend hold
            nxt["narration"] = ""
            nxt["_hold"] = True
            nxt["_hold_type"] = "extend"
            if nxt.get("_hold_source_page") is None:
                nxt["_hold_source_page"] = shots[i - 1].get("page")
            fixes.append({
                "shot_idx": i, "before": b[:80],
                "after": "(转 extend hold,复用前镜画面,不单独配音)",
                "action": f"narration 与窗口内 {hit_desc} 几乎完全重复,"
                          f"转 extend hold 避免旁白重复",
            })
        else:
            nxt["narration"] = remainder
            fixes.append({
                "shot_idx": i, "before": b[:80],
                "after": remainder[:80],
                "action": f"narration 与窗口内 {hit_desc} 重复,"
                          f"缩减为增量内容",
            })
    return fixes


# ════════════════════════════════════════════════════════════════
# 检查 3c (v2.4.1): 字数时长护栏
# ════════════════════════════════════════════════════════════════

# 中文 TTS 估算语速(字/秒)。实测可在 4.0~5.0 间调。
CHARS_PER_SEC = 4.5
# 单镜旁白预估时长上限:超过则建议拆 cutaway
SINGLE_SHOT_SEC_WARN = 8.0
# extend hold 链合并后预估时长上限:超过则建议中间插 cutaway
EXTEND_CHAIN_SEC_WARN = 12.0


def _estimate_narration_sec(narration: str) -> float:
    """由 narration 字数估算音频秒数。去标点后按 CHARS_PER_SEC 折算。"""
    if not narration:
        return 0.0
    body = re.sub(r'[，。、！？,.!?\s"\'\u201c\u201d\u2018\u2019]', '',
                  narration)
    if not body:
        return 0.0
    return len(body) / CHARS_PER_SEC


def _duration_guard(shots: list) -> list:
    """
    字数时长护栏。只检测、只 warning,不自动改结构。
    (拆图 / 插 cutaway 需要理解剧情,是分镜大师 LLM 的活。)

    返回 [{shot_idx, est_sec, kind, action}, ...] 警告记录。
      kind = "single_shot_long"  单镜旁白过长
      kind = "extend_chain_long" extend 链合并后过长
    """
    warnings = []

    # 1) 单镜旁白过长
    for i, shot in enumerate(shots):
        if shot.get("silent_beat"):
            continue
        est = _estimate_narration_sec(shot.get("narration") or "")
        # 每个 shot 同时写一个诊断字段,供分镜大师下一轮参考
        shot["_audio_dur_estimate"] = round(est, 1)
        if est > SINGLE_SHOT_SEC_WARN:
            warnings.append({
                "shot_idx": i,
                "est_sec": round(est, 1),
                "kind": "single_shot_long",
                "action": f"单镜旁白预估 {est:.1f}s (>{SINGLE_SHOT_SEC_WARN}s),"
                          f"建议分镜大师拆成 主图 + cutaway,避免一张图停太久",
            })

    # 2) extend hold 链合并后过长
    #    一条 extend 链 = 一个非 hold 主镜 + 跟在后面连续的 extend hold 镜
    i = 0
    n = len(shots)
    while i < n:
        shot = shots[i]
        is_extend = (shot.get("_hold") and
                     (shot.get("_hold_type") or "extend") == "extend")
        if is_extend:
            i += 1
            continue
        # 找以 i 为主镜的 extend 链
        chain = [i]
        j = i + 1
        while j < n:
            s_j = shots[j]
            if (s_j.get("_hold") and
                    (s_j.get("_hold_type") or "extend") == "extend"):
                chain.append(j)
                j += 1
            else:
                break
        if len(chain) > 1:
            total = sum(_estimate_narration_sec(shots[k].get("narration") or "")
                        for k in chain)
            if total > EXTEND_CHAIN_SEC_WARN:
                warnings.append({
                    "shot_idx": chain[0],
                    "est_sec": round(total, 1),
                    "kind": "extend_chain_long",
                    "action": f"extend 链(sh{chain[0]+1:02d}~sh{chain[-1]+1:02d}, "
                              f"{len(chain)} 镜)合并预估 {total:.1f}s "
                              f"(>{EXTEND_CHAIN_SEC_WARN}s),建议中间插一个 cutaway "
                              f"换图,而非一张图死 hold",
                })
        i = j if len(chain) > 1 else i + 1

    return warnings


# ════════════════════════════════════════════════════════════════
# 检查 3d (v2.4.1): silent_beat 字段兜底校验
# ════════════════════════════════════════════════════════════════

# 静默镜头缺失 intended_duration_sec 时的兜底秒数
DEFAULT_SILENT_SEC = 3.0


def _validate_silent_beats(shots: list) -> list:
    """
    校验 silent_beat 镜头的字段完整性,做兜底:
      - silent_beat=true 但 intended_duration_sec 缺失/<=0 → 填 DEFAULT_SILENT_SEC
      - silent_beat=true 但 narration / dialogue 非空 → 清空(静默镜头不配音)
    同时检测"非法空 narration":narration 空 + 非 hold + 非 silent_beat。

    返回 [{shot_idx, kind, action}, ...]。
      kind = "silent_dur_filled"   补了默认时长
      kind = "silent_audio_cleared" 清掉了静默镜头里的人声字段
      kind = "illegal_empty_narration" 非法空旁白(报错,不自动改)
    """
    fixes = []
    for i, shot in enumerate(shots):
        is_silent = bool(shot.get("silent_beat"))
        is_hold = bool(shot.get("_hold"))
        narration = (shot.get("narration") or "").strip()
        dialogue = shot.get("dialogue") or []

        if is_silent:
            dur = shot.get("intended_duration_sec")
            try:
                dur = float(dur) if dur is not None else 0.0
            except (TypeError, ValueError):
                dur = 0.0
            if dur <= 0:
                shot["intended_duration_sec"] = DEFAULT_SILENT_SEC
                fixes.append({
                    "shot_idx": i, "kind": "silent_dur_filled",
                    "action": f"silent_beat 缺 intended_duration_sec,"
                              f"兜底 {DEFAULT_SILENT_SEC}s",
                })
            if narration or dialogue:
                shot["narration"] = ""
                shot["dialogue"] = []
                fixes.append({
                    "shot_idx": i, "kind": "silent_audio_cleared",
                    "action": "silent_beat 镜头不应有 narration/dialogue,已清空",
                })
            continue

        # 非 silent 镜头:空 narration + 空 dialogue + 非 hold → 非法
        # (v2.4.4 起这种镜头不再仅 warning,而是在 post_check 末尾被 _purge_empty_shots 删除)
        if not narration and not dialogue and not is_hold:
            fixes.append({
                "shot_idx": i, "kind": "illegal_empty_narration",
                "action": "镜头无 narration 无 dialogue 且非 hold/silent_beat,"
                          "将在 post_check 末尾被清理删除",
            })
    return fixes


# ════════════════════════════════════════════════════════════════
# v2.4.4: 无声镜头清理 ── 删除"啥也没有"的镜头
# ════════════════════════════════════════════════════════════════
#
# 背景:经过 14.3 剥离 / _dedup_adjacent_narration / reviewer 修改后,
# 偶尔会产生 narration 空 + dialogue 空 + 非 silent_beat 的镜头。
# 这种镜头在 producer 那里会生出一个"无声 4 秒 KB clip",观感上是
# 视频里突然卡了一下没声音(BGM 还在,但缺人声很违和)。
#
# 决策(经过跟用户讨论,见 2026-05 修复日志):
#   - 这种镜头本来就是"分镜大师凑数"的产物,删掉
#   - cutaway 类型的镜头如果有 dialogue → 保留(画面 + 角色对话仍有价值)
#   - cutaway 但 dialogue 也空 → 也删(独立画面但无任何声音,等同空镜)
#   - 默认行为:删
#
# 一致性维护:
#   - 删一个 page 后,后续所有以它为 _hold_source_page 的镜头要重定向
#     (指向被删页之前最近的非 hold 镜头,即真正存在的"源页")
#   - 这一步必须在所有 shot_idx 引用都不再使用之后做(也就是 post_check 末尾)

def _purge_empty_shots(shots: list) -> tuple:
    """
    扫描并删除"无声镜头"。

    删除条件(全部满足):
      1. narration 为空
      2. dialogue 为空
      3. silent_beat 不为 true
      4. _hold 不为 true(extend hold 没声音是合理的;cutaway 见下)

    cutaway 特殊处理:
      - cutaway 有独立画面,但旁白和对话都空 → 仍然是"无声镜头",删
      - 这是跟用户讨论后的决策:cutaway 画面就算 LLM 标了独立,
        没有声音支撑的画面在节奏里就是干扰

    返回 (cleaned_shots, deleted_info_list)
      deleted_info_list = [
        {"page": int, "title": str, "reason": str, "was_hold_type": str|None},
        ...
      ]
    """
    deleted = []
    kept = []
    deleted_pages = set()   # 用于后续重定向

    for shot in shots:
        narration = (shot.get("narration") or "").strip()
        dialogue = shot.get("dialogue") or []
        is_silent = bool(shot.get("silent_beat"))
        is_hold = bool(shot.get("_hold"))
        hold_type = (shot.get("_hold_type") or "").strip().lower()

        # 应保留的情况
        if narration:
            kept.append(shot)
            continue
        if dialogue:
            kept.append(shot)
            continue
        if is_silent:
            # silent_beat 是合法的"留白镜头",保留
            kept.append(shot)
            continue
        # extend hold 镜头本身 narration 应该为空(它的音频已被并入前镜),保留
        if is_hold and hold_type == "extend":
            kept.append(shot)
            continue

        # 到这里:narration 空 + dialogue 空 + 非 silent
        # 可能是:普通镜被剥光 / cutaway 被剥光 / 未标 hold_type 的奇怪 hold
        reason = "narration 空 + dialogue 空 + 非 silent_beat"
        if is_hold:
            reason += f" + _hold={hold_type or '(未指定)'}(无声 hold 无意义)"

        deleted.append({
            "page": shot.get("page"),
            "title": shot.get("title", ""),
            "reason": reason,
            "was_hold_type": hold_type or None,
        })
        if shot.get("page") is not None:
            deleted_pages.add(shot["page"])

    # ── 一致性维护:重定向 _hold_source_page ──
    # 如果有任何镜头的 _hold_source_page 指向被删页,
    # 把它重定向到"被删页之前最近的、仍存在的非 hold 主镜"。
    if deleted_pages:
        # 建一个"page → 在 kept 列表里的索引"映射,便于查找前驱
        kept_pages_sorted = sorted(
            (s.get("page"), idx) for idx, s in enumerate(kept)
            if s.get("page") is not None)
        page_to_kept_idx = {p: idx for p, idx in kept_pages_sorted}
        kept_page_list = [p for p, _ in kept_pages_sorted]

        def find_predecessor_page(orig_page: int):
            """找 orig_page 之前最近的、kept 里的、非 hold 的主页 page。"""
            # 从 orig_page 倒着找
            for p in reversed(kept_page_list):
                if p >= orig_page:
                    continue
                sh = kept[page_to_kept_idx[p]]
                if not sh.get("_hold"):
                    return p
            return None

        for shot in kept:
            src = shot.get("_hold_source_page")
            if src is None:
                continue
            if src in deleted_pages:
                new_src = find_predecessor_page(src)
                if new_src is not None:
                    shot["_hold_source_page"] = new_src
                    # 加一条 revision_note 留痕
                    notes = shot.setdefault("_revision_notes", [])
                    notes.append({
                        "reviewer": "purge_empty",
                        "field": "_hold_source_page",
                        "issue": f"原 source page {src} 被清理删除,重定向到 {new_src}",
                        "before": src,
                        "after": new_src,
                    })
                else:
                    # 找不到合法前驱 → 取消 hold(变成独立镜头)
                    shot["_hold"] = False
                    shot["_hold_type"] = ""
                    shot["_hold_source_page"] = None
                    notes = shot.setdefault("_revision_notes", [])
                    notes.append({
                        "reviewer": "purge_empty",
                        "field": "_hold",
                        "issue": f"原 source page {src} 被清理删除,且找不到合法前驱 → 取消 hold",
                        "before": True,
                        "after": False,
                    })

    return kept, deleted


# ════════════════════════════════════════════════════════════════
# 检查 4: cutaway 引号术语 speaker 归属
# ════════════════════════════════════════════════════════════════

def _detect_unattributed_quote_in_narration(narration: str) -> str:
    """
    检测 narration 里是否有"叙述层引号术语"(无说话动词指向)。
    返回提取的引号内容,没找到返回空字符串。
    """
    patterns = [
        r'"([^"]+)"',
        r"'([^']+)'",
        r'\u201c([^\u201d]+)\u201d',  # 中文双引号
        r'\u2018([^\u2019]+)\u2019',  # 中文单引号
        r'\u300c([^\u300d]+)\u300d',  # 直角括号
    ]
    # 整句扫,若有说话动词就跳过(那是显式对话)
    speaker_verbs = r'(说|道|喊|叫|吼|低语|呢喃|嘟囔|嘀咕)'
    if re.search(speaker_verbs, narration):
        return ""

    for pattern in patterns:
        matches = re.findall(pattern, narration)
        for content in matches:
            content = content.strip()
            if not content or len(content) > 20:
                continue
            return content
    return ""


def _detect_lead_in_focal(focal_subject: str, leads_en_map: dict) -> str:
    """focal_subject 主要描绘哪个 lead。多个 lead 时返回空。"""
    hits = _scan_dual_leads_in_focal(focal_subject, leads_en_map)
    if len(hits) == 1:
        return hits[0]
    return ""


def _fix_quote_speaker_attribution(shot: dict,
                                   leads_en_map: dict) -> tuple:
    """
    cutaway 镜头 + 画面是某 lead + narration 有引号术语 + speaker=narrator
    → speaker 改为该 lead。
    """
    hold_type = shot.get("_hold_type") or ""
    if hold_type != "cutaway":
        return False, ""

    narration = shot.get("narration", "")
    focal = shot.get("focal_subject", "")
    dialogue = shot.get("dialogue", [])

    quote = _detect_unattributed_quote_in_narration(narration)
    if not quote:
        return False, ""

    lead = _detect_lead_in_focal(focal, leads_en_map)
    if not lead:
        return False, ""

    for i, dl in enumerate(dialogue):
        if not isinstance(dl, dict):
            continue
        text = (dl.get("text") or "").strip()
        speaker = (dl.get("speaker") or "").strip()
        if text and quote in text and speaker == "narrator":
            shot["dialogue"][i]["speaker"] = lead
            return True, f"speaker: narrator → {lead} (引号术语 '{quote}' 应归画面角色)"
    return False, ""


# ════════════════════════════════════════════════════════════════
# 检查 5 (v2.4 新增): hold 镜头 narration 污染检测
# ════════════════════════════════════════════════════════════════

def _longest_common_substring(s1: str, s2: str) -> str:
    """返回两个字符串最长公共子串。空串处理友好。"""
    if not s1 or not s2:
        return ""
    m, n = len(s1), len(s2)
    # 用滚动数组节省内存
    prev = [0] * (n + 1)
    longest = 0
    end_pos = 0
    for i in range(1, m + 1):
        curr = [0] * (n + 1)
        for j in range(1, n + 1):
            if s1[i - 1] == s2[j - 1]:
                curr[j] = prev[j - 1] + 1
                if curr[j] > longest:
                    longest = curr[j]
                    end_pos = i
        prev = curr
    return s1[end_pos - longest:end_pos]


def _strip_hold_narration_pollution(shots: list) -> list:
    """
    检测 hold 镜头 narration 跟上一镜的重叠,自动剥离。

    返回 [{shot_idx, overlap, action}, ...] 修复记录。

    策略:
      - hold 镜头 narration 里含上一镜 narration ≥ 8 字的子串 → 剥离
      - 剥离后清理孤立标点
    """
    fixes = []
    for i, shot in enumerate(shots):
        if not shot.get("_hold"):
            continue
        if i == 0:
            continue
        prev_narr = (shots[i - 1].get("narration") or "").strip()
        curr_narr = (shot.get("narration") or "").strip()
        if not prev_narr or not curr_narr:
            continue
        overlap = _longest_common_substring(prev_narr, curr_narr)
        if not overlap or len(overlap) < 8:
            continue
        # 剥离 overlap 部分
        new_narr = curr_narr.replace(overlap, "", 1)
        # 清理孤立标点
        new_narr = re.sub(r'^[,。、!?\s"\'\u201c\u201d\u2018\u2019]+', '', new_narr)
        new_narr = re.sub(r'[,、\s"\'\u201c\u201d\u2018\u2019]+$', '', new_narr)
        new_narr = re.sub(r'[,。]{2,}', '。', new_narr).strip()
        if new_narr and new_narr != curr_narr:
            shot["narration"] = new_narr
            fixes.append({
                "shot_idx": i,
                "overlap": overlap[:60],
                "before": curr_narr[:80],
                "after": new_narr[:80],
            })
    return fixes

# ════════════════════════════════════════════════════════════════
# 主入口 1: pre_check
# ════════════════════════════════════════════════════════════════

def pre_check(shots: list, chapter: dict, story_meta: dict) -> dict:
    """
    reviewer 跑之前的预检 + 自动修复。
    """
    leads_en_map = _build_lead_en_map(story_meta)
    gender_map = _build_lead_gender_map(story_meta)
    auto_fixes = []
    suspect = {
        "narrative": set(), "visual": set(),
        "flux": set(), "dialogue": set(),
    }

    # ── 修复 1: cutaway 引号术语 speaker 归属 ──
    for i, shot in enumerate(shots):
        changed, desc = _fix_quote_speaker_attribution(shot, leads_en_map)
        if changed:
            auto_fixes.append({
                "phase": "pre", "shot_idx": i, "type": "speaker_attribution",
                "action": desc,
            })

    # ── 修复 2: narration 剥离 dialogue.text ──
    for i, shot in enumerate(shots):
        if _strip_dialogue_from_narration(shot):
            auto_fixes.append({
                "phase": "pre", "shot_idx": i, "type": "narration_dedup",
                "action": "stripped dialogue.text from narration",
            })



    # ── 修复 3: 双 lead 同框(C → B → A 三级回退)──
    # 收集所有命中
    dual_lead_shots = []
    for i, shot in enumerate(shots):
        focal = shot.get("focal_subject", "")
        hits = _scan_dual_leads_in_focal(focal, leads_en_map)
        if len(hits) >= 2:
            dual_lead_shots.append({
                "shot_idx": i, "focal": focal,
                "kept": hits[0], "removed": hits[1:],
            })

    # C: 一次 LLM batch
    llm_result = {}
    if dual_lead_shots:
        llm_result = _llm_batch_rewrite_dual_leads(
            dual_lead_shots, leads_en_map, gender_map, chapter)

    # 应用 + 回退
    for s in dual_lead_shots:
        i = s["shot_idx"]
        old_focal = s["focal"]
        new_focal = llm_result.get(i, "")
        strategy = "llm_rewrite"
        if not new_focal:
            # B: 性别泛指
            removed_en_list = [leads_en_map.get(r, []) for r in s["removed"]]
            removed_gender_list = [gender_map.get(r, "unknown") for r in s["removed"]]
            new_focal = _rewrite_focal_fallback(
                old_focal, removed_en_list, removed_gender_list)
            strategy = "gender_generic"
            # 若性别都 unknown 那就是 A:"the other person"
            if all(g == "unknown" for g in removed_gender_list):
                strategy = "the_other_person"

        if new_focal and new_focal != old_focal:
            shots[i]["focal_subject"] = new_focal
            # 清洗 visible_characters,只留 kept
            vis = shots[i].get("visible_characters", []) or []
            shots[i]["visible_characters"] = [s["kept"]] if s["kept"] in vis else []
            auto_fixes.append({
                "phase": "pre", "shot_idx": i, "type": "dual_lead_rewrite",
                "action": f"[{strategy}] removed {s['removed']}, kept {s['kept']}",
                "before": old_focal[:120],
                "after": new_focal[:120],
            })
            suspect["flux"].add(i)
            suspect["dialogue"].add(i)

    # ── v2.4.1: silent_beat 字段兜底校验(放在最前,后续逻辑依赖它)──
    sb_fixes = _validate_silent_beats(shots)
    for f in sb_fixes:
        phase_type = f["kind"]
        auto_fixes.append({
            "phase": "pre", "shot_idx": f["shot_idx"],
            "type": phase_type, "action": f["action"],
        })
        if phase_type == "illegal_empty_narration":
            suspect["narrative"].add(f["shot_idx"])

    # ── v2.4.1: 相邻 narration 高度相似 → 真正去重(会写回)──
    dedup_fixes = _dedup_adjacent_narration(shots, threshold=0.85)
    for f in dedup_fixes:
        auto_fixes.append({
            "phase": "pre", "shot_idx": f["shot_idx"],
            "type": "adjacent_narration_dedup",
            "action": f["action"],
            "before": f["before"], "after": f["after"],
        })
        suspect["narrative"].add(f["shot_idx"])

    # ── 标记: 中度相似(0.7~0.85),只提示 reviewer,不自动改 ──
    similar = _find_similar_adjacent(shots, threshold=0.7)
    for a, b, sim in similar:
        if sim >= 0.85:
            continue  # 已被上面修复
        suspect["narrative"].add(a)
        suspect["narrative"].add(b)

    # ── v2.4.1: 字数时长护栏(只 warning,不改结构)──
    dur_warnings = _duration_guard(shots)
    for w in dur_warnings:
        auto_fixes.append({
            "phase": "pre", "shot_idx": w["shot_idx"],
            "type": "duration_guard",
            "action": w["action"],
        })

    # ── v2.4: hold 镜头 narration 污染剥离 ──
    hold_fixes = _strip_hold_narration_pollution(shots)
    for f in hold_fixes:
        auto_fixes.append({
            "phase": "pre", "shot_idx": f["shot_idx"],
            "type": "hold_narration_strip",
            "action": f"剥离与上一镜重叠的子串: '{f['overlap']}'",
            "before": f["before"],
            "after": f["after"],
        })
        suspect["narrative"].add(f["shot_idx"])

    return {
        "auto_fixes": auto_fixes,
        "suspect_shots": {k: sorted(v) for k, v in suspect.items()},
    }


# ════════════════════════════════════════════════════════════════
# 主入口 2: post_check
# ════════════════════════════════════════════════════════════════

def post_check(shots: list, revision_log: list,
               chapter: dict, story_meta: dict) -> tuple:
    """
    reviewer 跑完后的二次检查。返回 (shots, new_log, auto_fixes)。
    """
    leads_en_map = _build_lead_en_map(story_meta)
    auto_fixes = []

    # ── 1. no-op patch 过滤(revision_log 里 before == after)──
    new_log = []
    for r in revision_log:
        if r.get("status") != "applied":
            new_log.append(r)
            continue
        if isinstance(r.get("after"), (list, dict)):
            new_log.append(r)
            continue
        before = str(r.get("before", "") or "").strip()
        after = str(r.get("after", "") or "").strip()
        if before and after and before == after:
            r_new = dict(r)
            r_new["status"] = "rejected"
            r_new["reason"] = "post-check: no-op patch (before == after)"
            new_log.append(r_new)
            auto_fixes.append({
                "phase": "post", "shot_idx": r.get("shot_id", "?"),
                "type": "no_op_filtered",
                "action": f"reviewer={r.get('reviewer')}, field={r.get('field')}",
            })
        else:
            new_log.append(r)

    # ── 2. narration 二次剥离 ──
    for i, shot in enumerate(shots):
        if _strip_dialogue_from_narration(shot):
            auto_fixes.append({
                "phase": "post", "shot_idx": i, "type": "narration_dedup_2",
                "action": "reviewer 改完后 narration 又含 dialogue.text,二次剥离",
            })

    # ── 3. 二次相邻 narration 去重(reviewer 改完后可能又撞,会写回)──
    dedup_fixes_2 = _dedup_adjacent_narration(shots, threshold=0.85)
    for f in dedup_fixes_2:
        auto_fixes.append({
            "phase": "post", "shot_idx": f["shot_idx"],
            "type": "adjacent_narration_dedup_2",
            "action": "reviewer 改完后仍有相邻重复," + f["action"],
            "before": f["before"], "after": f["after"],
        })

    # ── 3b. hold 镜头 narration 二次剥离(真正的 post 阶段)──
    for f in _strip_hold_narration_pollution(shots):
        auto_fixes.append({
            "phase": "post", "shot_idx": f["shot_idx"],
            "type": "hold_narration_strip_2",
            "action": f"reviewer 后仍有 hold 污染,二次剥离: '{f['overlap']}'",
            "before": f["before"], "after": f["after"],
        })

    # ── 4. 二次双 lead 扫描(v2.9.3 语义升级)
    # v2.6 之前: 双 lead 同框 = bug (focal 拆不开,FLUX 画双胞胎),需要警告
    # v2.6 之后: 双 lead 同框 = feature,走多角色 PuLID + Regional Prompter 路径
    # 升级后语义: 仅当检测到双 lead **且 v2.6 必备字段(_pulid_chars + characters)
    #            未配齐** 才警告;字段齐全视为已走 v2.6 路径,完全 silent(不 append)。
    for i, shot in enumerate(shots):
        hits = _scan_dual_leads_in_focal(shot.get("focal_subject", ""),
                                         leads_en_map)
        if len(hits) < 2:
            continue

        # 检查 v2.6 多角色路径必备字段是否齐全
        pulid_chars = shot.get("_pulid_chars") or []
        sb_chars = shot.get("characters") or []
        # _region_prompts 是双人对视/对话镜的强烈推荐(focal_director 会产出),
        # 但 v2.6 router 对它是"宽容策略",没有也能走 v2.6,所以不强制要求。
        v260_ready = (
            isinstance(pulid_chars, list) and len(pulid_chars) >= 2
            and isinstance(sb_chars, list) and len(sb_chars) >= 2
        )

        if v260_ready:
            # 已走 v2.6 路径,完全 silent 不报警(避免 printer 噪音)
            continue

        # v2.6 字段未配齐 → 真警告,提示手动审视或检查 focal_director
        missing_parts = []
        if not (isinstance(pulid_chars, list) and len(pulid_chars) >= 2):
            missing_parts.append(f"_pulid_chars({len(pulid_chars) if isinstance(pulid_chars, list) else 0})")
        if not (isinstance(sb_chars, list) and len(sb_chars) >= 2):
            missing_parts.append(f"characters({len(sb_chars) if isinstance(sb_chars, list) else 0})")
        auto_fixes.append({
            "phase": "post", "shot_idx": i, "type": "dual_lead_warning_2",
            "action": (f"双 lead {hits} 但 v2.6 字段缺失[{', '.join(missing_parts)}],"
                       f"建议检查 focal_director 输出或手动审视"),
            "before": shot.get("focal_subject", "")[:80],
        })

    # ── 5. v2.7 新增: 旁白去重增强 ──
    from narration_dedup_v2 import run_dedup_v2

    dedup_v2_result = run_dedup_v2(shots)
    for f in dedup_v2_result["narr_dlg_overlap"]:
        auto_fixes.append({
            "phase":    "post",
            "shot_idx": f["shot_idx"],
            "type":     "narration_dialogue_overlap",
            "action":   f["reason"],
        })
    for f in dedup_v2_result["window_wide_dedup"]:
        auto_fixes.append({
            "phase":    "post",
            "shot_idx": f["shot_idx"],
            "type":     f["type"],
            "action":   f["reason"],
        })
    for f in dedup_v2_result["purge_empty"]:
        auto_fixes.append({
            "phase":    "post",
            "shot_idx": f["shot_idx"],
            "type":     "purge_empty_after_dedup",
            "action":   f["reason"],
        })

    # ── 6. v2.7 scene_audio 兜底: 空 ambient 按 bgm_mood 填默认 ──
    from core.audio.ambient_continuity import DEFAULT_AMBIENT_BY_MOOD
    for i, shot in enumerate(shots):
        sa = shot.get("scene_audio") or {"ambient": [], "sfx": []}
        if not sa.get("ambient"):
            mood = shot.get("bgm_mood", "tension")
            default = DEFAULT_AMBIENT_BY_MOOD.get(mood, [])
            if default:
                sa["ambient"] = default
                shot["scene_audio"] = sa
                auto_fixes.append({
                    "phase": "post",
                    "shot_idx": i,
                    "type": "ambient_default_fill",
                    "action": f"ambient 空 按 mood={mood} 填默认 {default}",
                })

    # ── 6. v2.7 新增: BGM mood 全局裁决 ──
    from core.bgm_mood_resolver import resolve_bgm_moods

    bgm_fixes = resolve_bgm_moods(
        shots, story_meta, chapter,
        theme_name=story_meta.get("theme", "")
    )
    for f in bgm_fixes:
        auto_fixes.append({
            "phase":    "post",
            "shot_idx": f["shot_idx"],
            "type":     "bgm_mood_resolve",
            "action":   f"bgm_mood: {f['before']} → {f['after']} ({f['reason']})",
        })

    # ── 7. (v2.4.4) 无声镜头清理 ────────────────────────────────
    # 这是 post_check 的最后一步,因为它会改变 shots 长度,
    # 必须在所有以 shot_idx 引用的检查跑完之后做。
    # 删除条件:narration 空 + dialogue 空 + 非 silent_beat + 非 extend hold。
    # 见 _purge_empty_shots 的详细注释。
    # 注意: 在增强去重处理后再次清理可能变空的镜头
    cleaned_shots, deleted = _purge_empty_shots(shots)
    if deleted:
        print(f"  [coordinator/purge] 清理 {len(deleted)} 个无声镜头:")
        for d in deleted:
            print(f"    - p{d['page']} ({d['title']}): {d['reason']}")
            auto_fixes.append({
                "phase": "post",
                "type": "empty_shot_purged",
                "page": d["page"],
                "title": d["title"],
                "action": f"清理无声镜头: {d['reason']}",
                "was_hold_type": d["was_hold_type"],
            })
        shots = cleaned_shots

    return shots, new_log, auto_fixes
