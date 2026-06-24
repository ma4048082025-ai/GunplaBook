"""
revision_printer.py ── 修订报告打印 v2.4
==================================================
v2.4 改动:
  - print_revision_report 加 auto_fixes 参数
  - 新增 _print_auto_fixes_section
  - 5 个 reviewer 显示(原 4 + coherence)
"""

_C = {
    "reset":  "\033[0m", "bold":   "\033[1m", "dim":    "\033[2m",
    "red":    "\033[31m", "green":  "\033[32m", "yellow": "\033[33m",
    "blue":   "\033[34m", "cyan":   "\033[36m", "gray":   "\033[90m",
}

_REVIEWER_LABELS = {
    "narrative": "叙事完整性",
    "visual":    "画面连贯性",
    "flux":      "FLUX 友好度",
    "dialogue":  "叙述层污染",
    "coherence": "画面对齐",   # v2.4 新增
    "unknown":   "未知",
}

_AUTO_FIX_LABELS = {
    "narration_dedup":        "📉 narration 剥离 dialogue",
    "narration_dedup_2":      "📉 narration 二次剥离",
    "speaker_attribution":    "🎙 speaker 归属修正",
    "dual_lead_rewrite":      "👥 双 lead 改为泛指",
    "dual_lead_warning_2":    "⚠️ 双 lead 未解决",
    "adjacent_similar":       "🔁 相邻 narration 相似",
    "adjacent_similar_2":     "🔁 二次相邻相似",
    "no_op_filtered":         "🗑 no-op patch 拒绝",
    "hold_narration_strip":   "✂️ hold 旁白剥离重叠",
    "hold_narration_strip_2": "✂️ hold 旁白二次剥离",
}


def _truncate(text, max_len=80):
    if not text:
        return ""
    text = str(text).replace("\n", " ").strip()
    if len(text) > max_len:
        return text[:max_len - 1] + "…"
    return text


def _print_auto_fixes_section(chapter_id, auto_fixes, C):
    """打印 coordinator 自动修复段落"""
    if not auto_fixes:
        return
    pre_fixes  = [f for f in auto_fixes if f.get("phase") == "pre"]
    post_fixes = [f for f in auto_fixes if f.get("phase") == "post"]

    print()
    print(f"{C['cyan']}{'═' * 60}{C['reset']}")
    print(f"{C['bold']}{C['cyan']}  {chapter_id} 协调器修复报告{C['reset']}")
    print(f"  前置: {len(pre_fixes)} 项 / 后置: {len(post_fixes)} 项")
    print(f"{C['cyan']}{'─' * 60}{C['reset']}")

    for fix in auto_fixes:
        ftype = fix.get("type", "?")
        label = _AUTO_FIX_LABELS.get(ftype, ftype)
        shot_idx = fix.get("shot_idx", "?")
        if isinstance(shot_idx, list):
            shot_str = "/".join(
                f"sh{i+1:02d}" if isinstance(i, int) else str(i)
                for i in shot_idx)
        elif isinstance(shot_idx, int):
            shot_str = f"sh{shot_idx+1:02d}"
        else:
            shot_str = str(shot_idx)

        phase_label = "前置" if fix.get("phase") == "pre" else "后置"
        color = C['blue'] if fix.get("phase") == "pre" else C['yellow']
        print(f"  {color}[{phase_label}] {shot_str}{C['reset']}  {label}")
        action = fix.get("action", "")
        if action:
            print(f"      {C['gray']}↪ {_truncate(action, 100)}{C['reset']}")
        before = fix.get("before", "")
        after = fix.get("after", "")
        if before:
            print(f"      {C['red']}- {_truncate(before, 80)}{C['reset']}")
        if after:
            print(f"      {C['green']}+ {_truncate(after, 80)}{C['reset']}")
    print(f"{C['cyan']}{'═' * 60}{C['reset']}")


def print_revision_report(chapter_id, shots, revision_log,
                           auto_fixes=None, use_color=True):
    """打印章节审稿报告。v2.4 新增 auto_fixes。"""
    C = _C if use_color else {k: "" for k in _C}

    # v2.4: 先打 coordinator 修复
    if auto_fixes:
        _print_auto_fixes_section(chapter_id, auto_fixes, C)

    applied = [r for r in revision_log if r.get("status") == "applied"]
    rejected = [r for r in revision_log if r.get("status") == "rejected"]

    by_reviewer = {}
    for r in applied:
        rv = r.get("reviewer", "unknown")
        by_reviewer.setdefault(rv, []).append(r)

    print()
    print(f"{C['cyan']}{'═' * 60}{C['reset']}")
    print(f"{C['bold']}{C['cyan']}  章节 {chapter_id} 审稿报告{C['reset']}")

    summary_parts = []
    for rv in ("narrative", "visual", "flux", "dialogue", "coherence"):
        n = len(by_reviewer.get(rv, []))
        if n > 0:
            label = _REVIEWER_LABELS.get(rv, rv)
            summary_parts.append(f"{label}={n}")
    summary = ", ".join(summary_parts) if summary_parts else "无修订"

    n_shots_revised = len(set(r["shot_id"] for r in applied))
    print(f"  共 {len(shots)} 镜头,{len(applied)} 项修订涉及 {n_shots_revised} 个 shot")
    print(f"  分布:{summary}")
    if rejected:
        print(f"  {C['yellow']}⚠ {len(rejected)} 项修订被拒绝{C['reset']}")
    print(f"{C['cyan']}{'─' * 60}{C['reset']}")

    if not applied and not rejected:
        print(f"  {C['green']}✓ 所有 shot 通过审稿{C['reset']}")
        print(f"{C['cyan']}{'═' * 60}{C['reset']}\n")
        return

    by_shot = {}
    for r in applied:
        sid = r.get("shot_id", "?")
        by_shot.setdefault(sid, []).append(r)

    for shot_id in sorted(by_shot.keys()):
        revisions = by_shot[shot_id]
        print()
        print(f"{C['yellow']}⚠️  {chapter_id}-{shot_id}{C['reset']}  "
              f"{C['gray']}({len(revisions)} 项修订){C['reset']}")
        for r in revisions:
            reviewer_label = _REVIEWER_LABELS.get(r.get("reviewer"), r.get("reviewer", "?"))
            field = r.get("field", "?")
            print(f"   {C['blue']}[{reviewer_label}]{C['reset']} {C['bold']}{field}{C['reset']}")
            reason = r.get("reason", "")
            if reason:
                print(f"      {C['gray']}↪ {_truncate(reason, 100)}{C['reset']}")
            before = r.get("before", "")
            after  = r.get("after", "")
            if before:
                print(f"      {C['red']}- {_truncate(before, 90)}{C['reset']}")
            if after and not isinstance(after, list):
                print(f"      {C['green']}+ {_truncate(after, 90)}{C['reset']}")
            elif isinstance(after, list):
                print(f"      {C['green']}+ <数组,{len(after)} 项>{C['reset']}")

    if rejected:
        print()
        print(f"{C['gray']}─── 被拒绝的修订 ───{C['reset']}")
        for r in rejected:
            print(f"  {C['gray']}  • {r.get('shot_id', '?')}.{r.get('field', '?')} "
                  f"({r.get('reviewer', '?')}): {_truncate(r.get('reason', ''), 60)}{C['reset']}")

    revised_shot_ids = set(by_shot.keys())
    all_shot_ids = {f"sh{i+1:02d}" for i in range(len(shots))}
    untouched = all_shot_ids - revised_shot_ids
    if untouched:
        print()
        sample = sorted(untouched)
        if len(sample) <= 8:
            print(f"  {C['green']}✓ 通过:{' / '.join(sample)}{C['reset']}")
        else:
            print(f"  {C['green']}✓ 通过:{len(untouched)} 个 shot 无需修订{C['reset']}")

    print(f"{C['cyan']}{'═' * 60}{C['reset']}\n")


def print_overall_summary(chapter_summaries, use_color=True):
    """全片审稿总结。v2.4 加 coherence。"""
    C = _C if use_color else {k: "" for k in _C}
    if not chapter_summaries:
        return

    print()
    print(f"{C['cyan']}{'═' * 60}{C['reset']}")
    print(f"{C['bold']}{C['cyan']}  全片审稿总结{C['reset']}")
    print(f"{C['cyan']}{'─' * 60}{C['reset']}")

    total_shots = sum(s["n_shots"] for s in chapter_summaries)
    total_revisions = sum(s["n_revisions"] for s in chapter_summaries)
    total_reviewer = {}
    for s in chapter_summaries:
        for rv, n in s.get("by_reviewer", {}).items():
            total_reviewer[rv] = total_reviewer.get(rv, 0) + n

    print(f"  共 {len(chapter_summaries)} 章 / {total_shots} 镜头")
    print(f"  总修订数: {total_revisions} 项")
    if total_revisions > 0:
        print(f"  修订占比: {total_revisions / max(total_shots, 1) * 100:.1f}%")
        print(f"  问题分布:")
        for rv in ("narrative", "visual", "flux", "dialogue", "coherence"):
            n = total_reviewer.get(rv, 0)
            if n > 0:
                label = _REVIEWER_LABELS.get(rv, rv)
                bar = "█" * min(int(n / max(total_revisions, 1) * 30), 30)
                print(f"    {label:14s} {n:3d}  {C['blue']}{bar}{C['reset']}")

    print()
    print(f"  {C['bold']}修订最多的 5 章:{C['reset']}")
    sorted_chs = sorted(chapter_summaries, key=lambda x: -x["n_revisions"])
    for ch in sorted_chs[:5]:
        if ch["n_revisions"] == 0:
            continue
        print(f"    {ch['id']}: {ch['n_revisions']} 修订 / {ch['n_shots']} 镜头")

    print(f"{C['cyan']}{'═' * 60}{C['reset']}\n")