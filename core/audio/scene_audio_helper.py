"""
core/audio/scene_audio_helper.py ── pages → mixer 输入辅助
============================================================
producer 调 mixer 时需要把 pages 里的 scene_audio 字段抽出来。
本模块就一个函数: extract_audio_meta_from_pages(pages)

不依赖现有代码,纯函数。
"""

from __future__ import annotations
from typing import Optional


def extract_audio_meta_from_pages(
    pages: list[dict],
    page_durations: list[float],
) -> dict:
    """
    从 pages 里抽出三层音频信息,供 producer 调 mixer 使用。

    pages 每项至少包含:
      - bgm_mood        : str (用于兜底默认 ambient)
      - scene_audio     : {ambient: [str], sfx: [{id, at, volume_db, important}]}
                          缺失视为空
      - narration_emotion: str (默认 "")
      - silent_beat     : bool

    Returns:
      {
        "pages_ambient":  [[str], ...]          每页 ambient 列表
        "pages_sfx":      [[dict], ...]         每页 sfx 列表
        "pages_mood":     [str, ...]            每页 bgm_mood
        "pages_emotion":  [str, ...]            每页 narration_emotion
      }
    """
    pages_ambient = []
    pages_sfx = []
    pages_mood = []
    pages_emotion = []

    for p in pages:
        sa = p.get("scene_audio") or {}
        amb = sa.get("ambient") or []
        sfx = sa.get("sfx") or []

        # 防御性: 列表内容标准化
        amb_clean = [str(x) for x in amb if isinstance(x, str) and x.strip()]
        sfx_clean = []
        for s in sfx:
            if not isinstance(s, dict):
                continue
            sid = str(s.get("id", "")).strip()
            if not sid:
                continue
            sfx_clean.append({
                "id":        sid,
                "at":        float(s.get("at", 0.0) or 0.0),
                "volume_db": float(s.get("volume_db", -6.0) or -6.0),
                "important": bool(s.get("important", False)),
            })

        pages_ambient.append(amb_clean)
        pages_sfx.append(sfx_clean)
        pages_mood.append(str(p.get("bgm_mood", "tension")))
        pages_emotion.append(str(p.get("narration_emotion", "")))

    return {
        "pages_ambient": pages_ambient,
        "pages_sfx":     pages_sfx,
        "pages_mood":    pages_mood,
        "pages_emotion": pages_emotion,
    }


# ════════════════════════════════════════════════════════════════
# 整片合成辅助: 一站式调用
# ════════════════════════════════════════════════════════════════
# producer 调它,producer 就只用关心"传 pages 进来,拿到 ambient.mp3 + sfx 事件列表"

def build_ambient_and_sfx_for_episode(
    pages: list[dict],
    page_durations: list[float],
    out_ambient_path: str,
) -> tuple[bool, list]:
    """
    一站式: 输入 pages + 时长, 输出
      - ambient mp3 (已平滑、已交叉淡化)
      - sfx 事件列表 (整片绝对时刻)

    Args:
        pages:             page_cfg 列表
        page_durations:    每页实际时长(秒)
        out_ambient_path:  ambient mp3 输出路径

    Returns:
        (ambient_ok: bool, sfx_events: list[SfxAbsoluteEvent])
    """
    from core.audio.ambient_continuity import (
        smooth_ambient_sequence,
        fill_default_ambient,
        build_ambient_track,
        collect_sfx_events,
    )

    meta = extract_audio_meta_from_pages(pages, page_durations)

    # ambient: 按 mood 兜底 → 平滑 → 拼接
    pages_amb_filled = fill_default_ambient(
        meta["pages_ambient"], meta["pages_mood"])
    segments = smooth_ambient_sequence(pages_amb_filled, page_durations)
    total_dur = sum(page_durations)
    ambient_ok = build_ambient_track(segments, total_dur, out_ambient_path)

    # sfx: 收集事件
    sfx_events = collect_sfx_events(meta["pages_sfx"], page_durations)

    print(f"  [scene_audio] ambient 段={len(segments)} sfx 事件={len(sfx_events)}")
    return ambient_ok, sfx_events
