"""
core/audio/ambient_continuity.py ── 消除 PPT 顿挫感
============================================================
解决问题:
  分镜大师每镜独立产 scene_audio.ambient,导致镜头切换=音频切换,
  听感像 PPT。本模块做两件事:
  1. smooth_ambient_sequence: 把分散的 ambient 标注合并成"音频段",
     孤立的 ambient(<3 镜)归并到大段。
  2. build_ambient_track:     按音频段拼接成整片长 mp3,段间交叉淡化。

不依赖 mixer.py,可独立测试。
"""

from __future__ import annotations
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


# ════════════════════════════════════════════════════════════════
# 数据结构
# ════════════════════════════════════════════════════════════════

@dataclass
class AmbientSegment:
    """一段连续 ambient,跨多个 page。"""
    ambient_ids: list[str]       # 这段用哪些 ambient(支持多层叠加,通常 1-2 个)
    start_sec:   float           # 整片时间轴上的起始秒
    end_sec:     float           # 结束秒
    page_indices: list[int] = field(default_factory=list)  # 来自哪几页


# ════════════════════════════════════════════════════════════════
# 1. 平滑算法
# ════════════════════════════════════════════════════════════════

# 孤立段阈值: 连续少于 N 镜的 ambient 段会被合并到邻段
ISOLATED_THRESHOLD = 3

# 单镜默认时长(无 page_durations 时用,仅诊断用)
DEFAULT_SHOT_SEC = 5.0


def smooth_ambient_sequence(
    pages_ambient: list[list[str]],
    page_durations: list[float],
) -> list[AmbientSegment]:
    """
    把每页的 ambient 列表平滑成连续段。

    Args:
        pages_ambient:  [[ambient_id1, ...], ...] 每页的 ambient 标注
        page_durations: [dur, ...] 每页时长(秒),长度必须与 pages_ambient 相同

    Returns:
        list[AmbientSegment],按时间排序

    算法:
      1. 把空列表当成 ["__default__"](后续会被默认填充)
      2. 把"键"相同的连续页合并成段(键 = tuple(sorted(ambient_ids)))
      3. 短段(<3 镜)合并到前一段
    """
    assert len(pages_ambient) == len(page_durations), \
        f"长度不匹配 {len(pages_ambient)} vs {len(page_durations)}"
    if not pages_ambient:
        return []

    # Step 1: 转 key
    def _key(amb_list: list[str]) -> tuple:
        if not amb_list:
            return ("__empty__",)
        return tuple(sorted(amb_list))

    # Step 2: 合并连续相同
    raw_segs = []   # [(key, [page_idx, ...]), ...]
    cur_key = _key(pages_ambient[0])
    cur_pages = [0]
    for i in range(1, len(pages_ambient)):
        k = _key(pages_ambient[i])
        if k == cur_key:
            cur_pages.append(i)
        else:
            raw_segs.append((cur_key, cur_pages))
            cur_key = k
            cur_pages = [i]
    raw_segs.append((cur_key, cur_pages))

    # Step 3: 短段归并到前段(若无前段则归到后段)
    smoothed = []
    for key, pages in raw_segs:
        if not smoothed:
            smoothed.append((key, list(pages)))
            continue
        if len(pages) < ISOLATED_THRESHOLD:
            # 归并到前一段
            smoothed[-1] = (smoothed[-1][0], smoothed[-1][1] + pages)
        else:
            smoothed.append((key, list(pages)))

    # Step 4: 算出时间轴
    cum_starts = [0.0]
    for d in page_durations:
        cum_starts.append(cum_starts[-1] + d)

    segments = []
    for key, pages in smoothed:
        start = cum_starts[pages[0]]
        end = cum_starts[pages[-1] + 1]
        amb_ids = ([] if key == ("__empty__",) or key == ("__default__",)
                   else list(key))
        segments.append(AmbientSegment(
            ambient_ids=amb_ids,
            start_sec=start,
            end_sec=end,
            page_indices=pages,
        ))

    return segments


# ════════════════════════════════════════════════════════════════
# 2. 默认 ambient 填充(按 bgm_mood 兜底)
# ════════════════════════════════════════════════════════════════

DEFAULT_AMBIENT_BY_MOOD = {
    "tension":    ["wind_howl"],
    "climax":     ["thunder_distant"],
    "melancholy": ["rain_light"],
    "playful":    ["crickets_night"],
    "warm":       ["candle_flame"],
    "mystery":    ["low_drone"],
    "epic":       ["wind_howl"],
    "serene":     ["crickets_night"],
}


def fill_default_ambient(
    pages_ambient: list[list[str]],
    pages_mood: list[str],
) -> list[list[str]]:
    """页 ambient 为空时按 bgm_mood 填默认。原数据不变,返回新列表。"""
    filled = []
    for amb, mood in zip(pages_ambient, pages_mood):
        if amb:
            filled.append(list(amb))
        else:
            filled.append(list(DEFAULT_AMBIENT_BY_MOOD.get(mood, [])))
    return filled


# ════════════════════════════════════════════════════════════════
# 3. 拼接成整片长 mp3
# ════════════════════════════════════════════════════════════════

AMBIENT_ROOT = Path("refs/sfx/ambient")
AMBIENT_VOLUME = 0.10            # ambient 整体音量(线性,约 -20dB)
CROSSFADE_SEC = 1.5              # 段间交叉淡化时长
SAMPLE_RATE = 44100


def _pick_ambient_file(ambient_id: str, seed: int = 0) -> Optional[Path]:
    """从 refs/sfx/ambient/<id>/ 选一个文件。"""
    target_dir = AMBIENT_ROOT / ambient_id
    if not target_dir.exists():
        return None
    files = sorted(target_dir.glob("*.wav")) + sorted(target_dir.glob("*.mp3"))
    if not files:
        return None
    return files[seed % len(files)]


def _build_segment_clip(seg: AmbientSegment, out_path: str, seed: int) -> bool:
    """为一个 ambient 段生成对应长度的 mp3。多个 ambient_ids 用 amix 混。"""
    if not seg.ambient_ids:
        # 空段 → 输出静音
        cmd = [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-f", "lavfi",
            "-i", f"anullsrc=channel_layout=stereo:sample_rate={SAMPLE_RATE}",
            "-t", f"{seg.end_sec - seg.start_sec:.3f}",
            "-c:a", "libmp3lame", "-b:a", "128k",
            out_path,
        ]
        return subprocess.run(cmd, capture_output=True).returncode == 0

    files = [_pick_ambient_file(aid, seed) for aid in seg.ambient_ids]
    files = [f for f in files if f is not None]
    if not files:
        return False

    dur = seg.end_sec - seg.start_sec
    if len(files) == 1:
        # 单 ambient: 直接 loop 到时长
        cmd = [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-stream_loop", "-1", "-i", str(files[0]),
            "-t", f"{dur:.3f}",
            "-af",
            f"volume={AMBIENT_VOLUME},"
            f"afade=t=in:st=0:d={CROSSFADE_SEC},"
            f"afade=t=out:st={max(0, dur - CROSSFADE_SEC):.3f}:d={CROSSFADE_SEC}",
            "-c:a", "libmp3lame", "-b:a", "128k",
            out_path,
        ]
    else:
        # 多 ambient: amix 叠加(每个 loop 到 dur)
        inputs = []
        for f in files:
            inputs += ["-stream_loop", "-1", "-i", str(f)]
        filter_parts = []
        for i in range(len(files)):
            filter_parts.append(f"[{i}:a]atrim=duration={dur:.3f}[a{i}]")
        mix = "".join(f"[a{i}]" for i in range(len(files)))
        filter_parts.append(
            f"{mix}amix=inputs={len(files)}:duration=longest:normalize=0,"
            f"volume={AMBIENT_VOLUME},"
            f"afade=t=in:st=0:d={CROSSFADE_SEC},"
            f"afade=t=out:st={max(0, dur - CROSSFADE_SEC):.3f}:d={CROSSFADE_SEC}[out]"
        )
        cmd = (
            ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error"]
            + inputs
            + ["-filter_complex", ";".join(filter_parts),
               "-map", "[out]",
               "-t", f"{dur:.3f}",
               "-c:a", "libmp3lame", "-b:a", "128k",
               out_path]
        )
    return subprocess.run(cmd, capture_output=True).returncode == 0


def build_ambient_track(
    segments: list[AmbientSegment],
    total_dur: float,
    out_path: str,
) -> bool:
    """
    把 ambient 段拼成整片长 mp3,段间交叉淡化(由各段尾部 afade 实现,不用 acrossfade)。

    Returns: True 成功
    """
    if not segments:
        return False

    tmp_dir = Path(tempfile.mkdtemp(prefix="ambient_"))
    try:
        sub_paths = []
        for i, seg in enumerate(segments):
            sub_path = str(tmp_dir / f"amb_seg_{i:02d}.mp3")
            if _build_segment_clip(seg, sub_path, seed=i):
                sub_paths.append(sub_path)
                ids = ",".join(seg.ambient_ids) if seg.ambient_ids else "(silent)"
                print(f"  [ambient] seg {i}: {seg.start_sec:.1f}s-{seg.end_sec:.1f}s "
                      f"[{ids}]")
            else:
                print(f"  [ambient] seg {i} 生成失败")

        if not sub_paths:
            return False

        # concat
        list_file = tmp_dir / "concat.txt"
        with open(list_file, "w") as f:
            for sp in sub_paths:
                f.write(f"file '{Path(sp).resolve()}'\n")
        cmd = [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-f", "concat", "-safe", "0",
            "-i", str(list_file),
            "-c:a", "libmp3lame", "-b:a", "128k",
            out_path,
        ]
        result = subprocess.run(cmd, capture_output=True)
        if result.returncode != 0:
            return False
        print(f"  [ambient] ✓ 整片 ambient: {Path(out_path).name} ({total_dur:.1f}s)")
        return True
    finally:
        import shutil
        shutil.rmtree(tmp_dir, ignore_errors=True)


# ════════════════════════════════════════════════════════════════
# 4. SFX 防爆音 + 时间轴定位
# ════════════════════════════════════════════════════════════════

SFX_ROOT = Path("refs/sfx/stinger")
SFX_FADE_IN_MS = 30
SFX_FADE_OUT_MS = 80


@dataclass
class SfxAbsoluteEvent:
    """整片时间轴上的 SFX 事件(由 page-relative 转换得来)。"""
    path:      str
    at_sec:    float           # 整片时间轴上的触发时刻
    volume_db: float = -6.0
    important: bool  = False


def collect_sfx_events(
    pages_sfx: list[list[dict]],
    page_durations: list[float],
) -> list[SfxAbsoluteEvent]:
    """
    把每页的 scene_audio.sfx(相对镜头开始秒数)转成整片绝对时刻。

    Args:
        pages_sfx: [[{id, at, volume, important}, ...], ...] 每页 sfx 列表
                   at 是镜头内偏移秒数
        page_durations: 每页时长
    """
    cum = 0.0
    events = []
    for sfx_list, dur in zip(pages_sfx, page_durations):
        for sfx in sfx_list or []:
            sfx_id = sfx.get("id", "")
            if not sfx_id:
                continue
            sfx_path = _resolve_sfx_path(sfx_id)
            if sfx_path is None:
                print(f"  [sfx] ⚠ 找不到素材: {sfx_id}")
                continue
            at = float(sfx.get("at", 0.0))
            # 不超过本页时长
            at = max(0.0, min(at, dur - 0.3))
            events.append(SfxAbsoluteEvent(
                path=str(sfx_path),
                at_sec=cum + at,
                volume_db=float(sfx.get("volume_db", -6.0)),
                important=bool(sfx.get("important", False)),
            ))
        cum += dur
    return events


def _resolve_sfx_path(sfx_id: str) -> Optional[Path]:
    """在 refs/sfx/stinger/<id>/ 和 refs/sfx/transition/<id>/ 下找。"""
    for category in ("stinger", "transition"):
        target_dir = Path("refs/sfx") / category / sfx_id
        if target_dir.exists():
            files = sorted(target_dir.glob("*.wav")) + sorted(target_dir.glob("*.mp3"))
            if files:
                return files[0]
    return None


# ════════════════════════════════════════════════════════════════
# CLI 测试
# ════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    # 模拟 8 页的 ambient 序列(典型 PPT 感场景)
    pages = [
        ["rain_heavy"],
        ["temple_interior"],          # 孤立 1 镜,应合并
        ["rain_heavy"],
        ["rain_heavy"],
        ["rain_heavy"],
        ["graveyard_night"],
        ["graveyard_night"],
        ["graveyard_night"],
    ]
    durs = [5.0] * 8

    segs = smooth_ambient_sequence(pages, durs)
    print(f"原始 8 页 → 平滑后 {len(segs)} 段:")
    for s in segs:
        print(f"  {s.start_sec:.1f}s-{s.end_sec:.1f}s "
              f"ambient={s.ambient_ids} pages={s.page_indices}")
