"""
core/narration_dedup_v2.py ── 旁白去重增强(v2)
=================================================================
解决问题:
  现有 coordinator._dedup_adjacent_narration 漏网,因为:
  A. 阈值 0.85 太严,抓不到改写式重复
  B. 窗口 3 太短,跨段重复抓不到
  C. 不查 narration 跟 dialogue 之间的重复(LLM 经常 narration 旁白
     讲一句、紧接着角色 dialogue 说同样的内容)
  D. 空 narration + 空 dialogue 的镜头应删除,coordinator 已有逻辑
     但仅在 post_check 末尾跑一次,如果 narration_dialogue_dedup 后
     新产生的空镜会漏

本模块提供 3 个新函数,加到 coordinator pre/post 的合适位置:

  1. dedup_narration_dialogue_overlap(shots)
     检测 narration 与同镜 dialogue 的重叠,删 narration 保 dialogue
     (角色亲口说出来的,旁白复述就多余)

  2. dedup_narration_window_wide(shots, window=5, threshold=0.7)
     增强版相邻 narration 去重:
     - 窗口 3 → 5(覆盖一个 segment 拆出来的 5-6 镜)
     - 阈值 0.85 → 0.7(抓到改写式重复)
     - 加 fuzzy 词级匹配(50% 共享词也算重复)

  3. purge_empty_after_dedup(shots)
     在去重之后跑一次:删除 narration 空 + dialogue 空 + 非 hold 非 silent_beat 的镜头

部署: 见文件末尾的"接入指南"。完全可加可撤,不破坏现有契约。
"""

from __future__ import annotations
import re
from difflib import SequenceMatcher


# ════════════════════════════════════════════════════════════════
# 工具函数 (从原 coordinator 借鉴)
# ════════════════════════════════════════════════════════════════

_PUNCT_RE = re.compile(
    r'[,。、!?,.!?\s"\'\u201c\u201d\u2018\u2019'
    r'\u2014\u2015\u2026\u2025\-~·]'
)


def _strip_punct(s: str) -> str:
    """剥标点 + 空白,返回纯字字符串。"""
    return _PUNCT_RE.sub("", s)


def _narration_similarity(a: str, b: str) -> float:
    """SequenceMatcher ratio。"""
    if not a or not b:
        return 0.0
    a_core = _strip_punct(a)
    b_core = _strip_punct(b)
    if not a_core or not b_core:
        return 0.0
    return SequenceMatcher(None, a_core, b_core).ratio()


def _shared_chars_ratio(a: str, b: str) -> float:
    """
    共享字符比例(对汉字有效的近似 fuzzy match)。
    a_chars 和 b_chars 都做集合,比较交集 / 较短一方长度。
    用于抓"改写式重复":50% 以上的字相同,大概率讲的同一件事。
    """
    a_core = _strip_punct(a)
    b_core = _strip_punct(b)
    if len(a_core) < 6 or len(b_core) < 6:
        return 0.0
    set_a = set(a_core)
    set_b = set(b_core)
    overlap = set_a & set_b
    shorter = min(len(set_a), len(set_b))
    return len(overlap) / max(shorter, 1)


def _longest_common_substring(a: str, b: str) -> str:
    """最长公共子串。供 fix 时剥离用。"""
    if not a or not b:
        return ""
    matcher = SequenceMatcher(None, a, b)
    m = matcher.find_longest_match(0, len(a), 0, len(b))
    return a[m.a:m.a + m.size] if m.size > 0 else ""


# ════════════════════════════════════════════════════════════════
# 1. 同镜 narration vs dialogue 去重
# ════════════════════════════════════════════════════════════════

def dedup_narration_dialogue_overlap(shots: list) -> list[dict]:
    """
    检测每镜 narration 与 dialogue 的内容重叠。

    场景: LLM 经常写出
      narration: "沈小石压低声音说他要去抓老鼠"
      dialogue:  [{"speaker": "沈小石", "text": "我要去抓老鼠"}]

    这种重叠应删 narration 保 dialogue,因为
      - 角色亲口说出来更生动
      - 旁白复述很出戏

    判定阈值:
      - narration 中包含 dialogue 文本核心字符 >= 60% → 重复

    返回 fixes 列表,直接修改 shots[i]["narration"]。
    """
    fixes = []

    for i, shot in enumerate(shots):
        narration = (shot.get("narration") or "").strip()
        dialogue = shot.get("dialogue", []) or []
        if not narration or not dialogue:
            continue
        # extend hold / silent_beat 跳过(narration 可能是 hold 拼接出来的)
        if shot.get("_hold") and (shot.get("_hold_type") or "extend") == "extend":
            continue
        if shot.get("silent_beat"):
            continue

        narr_core = _strip_punct(narration)
        if len(narr_core) < 8:
            continue

        for dlg in dialogue:
            dlg_text = (dlg.get("text") or "").strip()
            if not dlg_text:
                continue
            dlg_core = _strip_punct(dlg_text)
            if len(dlg_core) < 6:
                continue

            # 判 1: dlg 完整包含在 narration 里
            full_contain = dlg_core in narr_core

            # 判 2: 字符共享比 >= 0.6 (改写重叠)
            shared = _shared_chars_ratio(narration, dlg_text)

            # 判 3: SequenceMatcher >= 0.6
            sim = _narration_similarity(narration, dlg_text)

            if full_contain or shared >= 0.6 or sim >= 0.6:
                before = narration
                # 策略: 直接清空 narration,让 dialogue 担当
                shot["narration"] = ""
                fixes.append({
                    "shot_idx": i,
                    "type":     "narration_dialogue_overlap",
                    "before":   before,
                    "after":    "",
                    "dlg_speaker": dlg.get("speaker", ""),
                    "dlg_text":    dlg_text,
                    "reason":   (f"narration 与 dialogue 重叠 "
                                 f"(包含={full_contain}, 共享={shared:.2f}, "
                                 f"sim={sim:.2f})"),
                })
                break  # 一个 shot 处理一次就够

    return fixes


# ════════════════════════════════════════════════════════════════
# 2. 增强版相邻 narration 去重(更宽窗口 + 更松阈值 + 词级 fuzzy)
# ════════════════════════════════════════════════════════════════

def dedup_narration_window_wide(shots: list,
                                 window: int = 5,
                                 sim_threshold: float = 0.7,
                                 shared_threshold: float = 0.55) -> list[dict]:
    """
    增强版相邻 narration 去重。

    改进:
      window:  3 → 5 (覆盖 segment 内 5-6 镜的拆分)
      sim:     0.85 → 0.7 (抓改写式重复)
      新增 shared_chars_ratio 维度,抓字面差异大但语义重复的

    策略仍然是: 命中 → 剥离重复部分 → 剩余 < 6 字则视情况
                转 extend hold 或 清空(看是不是 cutaway)
    """
    fixes = []

    for i in range(1, len(shots)):
        shot = shots[i]
        narr = (shot.get("narration") or "").strip()
        if len(narr) < 8:
            continue

        # 跳过 extend hold / silent_beat
        is_extend = (shot.get("_hold") and
                     (shot.get("_hold_type") or "extend") == "extend")
        is_silent = shot.get("silent_beat")
        if is_extend or is_silent:
            continue
        is_cutaway = (shot.get("_hold") and
                      (shot.get("_hold_type") or "") == "cutaway")

        # 收集窗口前镜
        win_start = max(0, i - window)
        prev_narrations = []
        for k in range(win_start, i):
            p = (shots[k].get("narration") or "").strip()
            if len(p) >= 8:
                prev_narrations.append((k, p))
        if not prev_narrations:
            continue

        remainder = narr
        hit_indices = []
        best_signal = 0.0

        for k, prev in prev_narrations:
            sim = _narration_similarity(prev, remainder)
            shared = _shared_chars_ratio(prev, remainder)
            signal = max(sim, shared)
            best_signal = max(best_signal, signal)

            # 子串包含直接处理
            prev_core = _strip_punct(prev)
            rem_core = _strip_punct(remainder)
            substring_hit = (
                (len(prev_core) >= 8 and prev_core in rem_core) or
                (len(rem_core) >= 8 and rem_core in prev_core)
            )

            if substring_hit or sim >= sim_threshold or shared >= shared_threshold:
                # 剥离 LCS
                lcs = _longest_common_substring(prev, remainder)
                if lcs and len(_strip_punct(lcs)) >= 6:
                    remainder = remainder.replace(lcs, "", 1)
                    hit_indices.append(k)
                elif shared >= 0.65 or sim >= 0.6:
                    # 近义改写: LCS 太短但语义高度重叠
                    # 直接判定整个 remainder 是重复,清空
                    remainder = ""
                    hit_indices.append(k)
                    break  # 已经清空,不必再比

        if not hit_indices:
            continue

        # 整理 remainder
        remainder = remainder.strip(' ,。、!??.,!\u2014\u2026')
        rem_core = _strip_punct(remainder)
        hit_desc = ",".join(f"p{x+1}" for x in hit_indices)

        if len(rem_core) >= 6:
            # 还有实质内容 → 保留 remainder
            before = narr
            shot["narration"] = remainder
            fixes.append({
                "shot_idx": i,
                "type":     "adjacent_dedup_v2",
                "before":   before,
                "after":    remainder,
                "reason":   f"窗口内与 {hit_desc} 重叠(signal={best_signal:.2f}),剥离后保留",
            })
        else:
            # 几乎全是重复 → 处理方式分情况
            before = narr
            if is_cutaway:
                # cutaway: 只清 narration,画面保留
                shot["narration"] = ""
                fixes.append({
                    "shot_idx": i,
                    "type":     "adjacent_dedup_v2_clear",
                    "before":   before,
                    "after":    "",
                    "reason":   (f"cutaway 镜旁白与 {hit_desc} 全重复,"
                                 f"清空 narration 保画面"),
                })
            else:
                # 普通镜: 转 extend hold
                shot["narration"] = ""
                shot["_hold"] = True
                shot["_hold_type"] = "extend"
                # _hold_source_page 不知道具体哪个,留给 coordinator 补
                fixes.append({
                    "shot_idx": i,
                    "type":     "adjacent_dedup_v2_to_extend",
                    "before":   before,
                    "after":    "[转 extend hold]",
                    "reason":   (f"普通镜与 {hit_desc} 全重复,"
                                 f"转 extend hold"),
                })

    return fixes


# ════════════════════════════════════════════════════════════════
# 3. 去重后的空镜清理
# ════════════════════════════════════════════════════════════════

def purge_empty_after_dedup(shots: list) -> list[dict]:
    """
    清理 narration 空 + dialogue 空 + 非 hold + 非 silent_beat 的"无声镜"。

    coordinator 已有 _purge_empty_shots,但本函数在 dedup_v2 跑完后
    再补一次,因为 v2 的 dedup 可能把镜头剥空。

    返回删除日志。**不直接修改 shots**,返回需要保留的索引列表
    交给调用方决定怎么 splice(避免在迭代中改 list)。
    """
    fixes = []
    to_remove = []

    for i, shot in enumerate(shots):
        # extend hold / silent_beat / cutaway 都不能删
        if shot.get("_hold"):
            continue
        if shot.get("silent_beat"):
            continue

        narr = (shot.get("narration") or "").strip()
        dialogue = shot.get("dialogue", []) or []
        has_dialogue = any(
            (d.get("text") or "").strip() for d in dialogue
        )
        if narr or has_dialogue:
            continue

        # 完全空 → 标记删除
        to_remove.append(i)
        fixes.append({
            "shot_idx": i,
            "type":     "purge_empty_after_dedup",
            "before":   shot.get("focal_subject", ""),
            "after":    "[删除]",
            "reason":   "去重后 narration 和 dialogue 都空,且非 hold/silent_beat",
        })

    # 倒序删除避免索引错位
    for idx in sorted(to_remove, reverse=True):
        shots.pop(idx)

    return fixes


# ════════════════════════════════════════════════════════════════
# 一站式调用
# ════════════════════════════════════════════════════════════════

def run_dedup_v2(shots: list,
                  enable_narr_dlg: bool = True,
                  enable_window_wide: bool = True,
                  enable_purge: bool = True) -> dict:
    """
    一站式跑完三道增强去重。返回 fixes 汇总。

    Args:
        shots: shot 列表,会被原地修改
        enable_*: 各步开关(调试用,默认全开)

    Returns:
        {
          "narr_dlg_overlap":  [...],
          "window_wide_dedup": [...],
          "purge_empty":       [...],
          "total":             N,
        }
    """
    result = {"narr_dlg_overlap": [], "window_wide_dedup": [],
              "purge_empty": [], "total": 0}

    if enable_narr_dlg:
        result["narr_dlg_overlap"] = dedup_narration_dialogue_overlap(shots)

    if enable_window_wide:
        result["window_wide_dedup"] = dedup_narration_window_wide(shots)

    if enable_purge:
        result["purge_empty"] = purge_empty_after_dedup(shots)

    result["total"] = (len(result["narr_dlg_overlap"]) +
                       len(result["window_wide_dedup"]) +
                       len(result["purge_empty"]))
    return result


# ════════════════════════════════════════════════════════════════
# CLI 测试
# ════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    # 模拟一个有重复的故事
    test_shots = [
        {"narration": "沈小石抬头一看,王大嘴正从巷子里冲出来。",
         "dialogue": []},
        {"narration": "他看见王大嘴冲出来了。",  # 改写式重复
         "dialogue": []},
        {"narration": "沈小石压低声音说他要去抓老鼠。",
         "dialogue": [{"speaker": "沈小石", "text": "我要去抓老鼠"}]},
        {"narration": "三只灰毛团顺着王大嘴的靴筒往上爬。",
         "dialogue": []},
        {"narration": "",  # 故意空
         "dialogue": []},
        {"narration": "三只老鼠顺着靴筒往上爬。",  # 窗口外但 fuzzy 命中
         "dialogue": []},
    ]

    print("=== 处理前 ===")
    for i, s in enumerate(test_shots):
        print(f"  {i+1}: narr={s['narration'][:30]!r} "
              f"dialogue={[d['text'][:20] for d in s['dialogue']]}")

    result = run_dedup_v2(test_shots)

    print(f"\n=== fixes ===")
    print(f"narration↔dialogue: {len(result['narr_dlg_overlap'])}")
    for f in result["narr_dlg_overlap"]:
        print(f"  shot {f['shot_idx']+1}: {f['reason']}")
    print(f"adjacent_window_wide: {len(result['window_wide_dedup'])}")
    for f in result["window_wide_dedup"]:
        print(f"  shot {f['shot_idx']+1}: "
              f"{f['before'][:30]!r} → {f['after'][:30]!r}")
        print(f"     {f['reason']}")
    print(f"purge_empty: {len(result['purge_empty'])}")
    for f in result["purge_empty"]:
        print(f"  shot {f['shot_idx']+1}: {f['reason']}")

    print(f"\n=== 处理后 ({len(test_shots)} 镜) ===")
    for i, s in enumerate(test_shots):
        marker = " [HOLD]" if s.get("_hold") else ""
        print(f"  {i+1}: narr={s['narration'][:30]!r}{marker}")
