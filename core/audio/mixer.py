"""
core/audio/mixer.py ── 五层混音引擎(独立实现)
================================================================
把人声 / BGM / Ambient / SFX / Room Tone 五层素材融合成最终视频音轨。
不和 producer / voice_engine / storyboard 耦合。

设计原则:
  1. 输入: 文件路径(或 None,表示该层不存在)
  2. 输出: 最终混音 mp3 路径 + 时长
  3. 全 ffmpeg 实现,无外部 Python 音频库依赖
  4. 五条混音铁律编码为常量和滤镜链,改起来集中
  5. 单独可跑: python -m core.audio.mixer <config.json>

依赖:
  ffmpeg / ffprobe 必须在 PATH 中。

调试:
  环境变量 MIXER_DEBUG=1 → 打印 ffmpeg 命令
  环境变量 MIXER_KEEP_TMP=1 → 保留中间文件
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import shutil
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional


# ════════════════════════════════════════════════════════════════
# 五条混音铁律的数值(集中管理,改这里即可全局生效)
# ════════════════════════════════════════════════════════════════

# 铁律 2: 音量层级关系(dB,基准是人声=0dB)
LEVEL_VOICE        =   0.0    # L4 人声,基准
LEVEL_SFX_DEFAULT  =  -6.0    # L3 SFX 普通事件
LEVEL_SFX_KEY      =  -3.0    # L3 SFX 关键事件(用户标 important=true)
LEVEL_BGM          = -15.0    # L5 BGM
LEVEL_AMBIENT      = -22.0    # L2 Ambient
LEVEL_ROOM_TONE    = -40.0    # L1 Room Tone

# 铁律 3: Ducking 参数(人声开口时 BGM 自动降低)
DUCK_THRESHOLD     = 0.05     # 人声振幅超过此值认为是"开口"
DUCK_RATIO         = 6.0      # 压缩比 6:1
DUCK_ATTACK_MS     = 20       # 起音(BGM 降下来要多快)
DUCK_RELEASE_MS    = 400      # 释音(BGM 升回去要多慢,慢一点听感自然)
DUCK_MAKEUP_DB     = -3.0     # 补偿增益,让 ducking 后听感不闷

# 铁律 4: SFX 削顶上限
SFX_PEAK_LIMIT_DB  = -3.0     # SFX 最响不能超过 -3dB(避免炸耳朵)

# 铁律 5: 整片响度标准化(LUFS, YouTube 标准 -14)
LOUDNORM_TARGET_I  = -14.0    # 集成响度
LOUDNORM_TARGET_LRA = 11.0    # 响度范围
LOUDNORM_TARGET_TP =  -1.5    # 真峰值

# 共用参数
SAMPLE_RATE        = 44100
CHANNELS           = 2        # 立体声输出
OUTPUT_BITRATE     = "192k"


# ════════════════════════════════════════════════════════════════
# 数据结构
# ════════════════════════════════════════════════════════════════

@dataclass
class SfxEvent:
    """一个 SFX 触发点。"""
    path:       str            # 音效文件绝对路径
    at:         float          # 在最终时间轴上的触发时刻(秒)
    volume_db:  float = LEVEL_SFX_DEFAULT
    important:  bool  = False  # True → 用 LEVEL_SFX_KEY 而不是 default


@dataclass
class MixInput:
    """一次完整混音的所有输入。

    任何一层为 None 都跳过那一层。整集时长由 voice_path 决定;
    没有 voice 时由 bgm_path / ambient_path 任一非 None 的总时长决定。
    """
    # L4
    voice_path:     Optional[str] = None
    # L5
    bgm_path:       Optional[str] = None
    # L2
    ambient_path:   Optional[str] = None
    # L3
    sfx_events:     list = field(default_factory=list)
    # L1
    room_tone_path: Optional[str] = None

    # 是否启用 ducking(铁律 3)
    enable_ducking: bool = True
    # 是否启用最终 loudnorm(铁律 5)
    enable_loudnorm: bool = True
    # 强制最终时长(秒),None = 自动按 voice/bgm/ambient 最长者
    force_duration: Optional[float] = None


# ════════════════════════════════════════════════════════════════
# 工具函数
# ════════════════════════════════════════════════════════════════

def _debug_log(msg: str):
    if os.environ.get("MIXER_DEBUG"):
        print(f"[mixer/debug] {msg}")


def _run_ffmpeg(args: list[str], step: str = "") -> None:
    """跑 ffmpeg,失败抛异常带 stderr 详情。"""
    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error"] + args
    _debug_log(f"{step}: {' '.join(cmd)}")
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(
            f"ffmpeg failed at step '{step}':\n"
            f"  cmd: {' '.join(cmd)}\n"
            f"  stderr: {result.stderr}"
        )


def _audio_duration(path: str) -> float:
    """用 ffprobe 测音频时长(秒)。"""
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", path],
        capture_output=True, text=True
    )
    if result.returncode != 0:
        raise RuntimeError(f"ffprobe failed on {path}: {result.stderr}")
    return float(result.stdout.strip() or 0.0)


def _db_to_amplitude(db: float) -> float:
    """dB → 线性振幅。-6 dB ≈ 0.5。"""
    return 10.0 ** (db / 20.0)


# ════════════════════════════════════════════════════════════════
# 单层处理
# ════════════════════════════════════════════════════════════════

def _prepare_voice(src: str, dst: str, total_dur: float) -> None:
    """L4 人声: 调到基准音量,补到总时长(尾部补静音)。

    铁律 1: 人声不削峰、不限响度,作为后面所有层的参考。
    """
    # 人声层 = 原文件 + 静音补尾 + 标准音量
    args = [
        "-i", src,
        "-filter_complex",
        f"[0:a]volume={_db_to_amplitude(LEVEL_VOICE):.4f},"
        f"apad=pad_dur={total_dur}[out]",
        "-map", "[out]",
        "-t", f"{total_dur}",
        "-ac", str(CHANNELS), "-ar", str(SAMPLE_RATE),
        "-c:a", "libmp3lame", "-b:a", OUTPUT_BITRATE,
        dst,
    ]
    _run_ffmpeg(args, step="prepare_voice")


def _prepare_bgm(src: str, dst: str, total_dur: float) -> None:
    """L5 BGM: loop 到总时长,调到 BGM 音量层级。"""
    args = [
        "-stream_loop", "-1",
        "-i", src,
        "-filter_complex",
        f"[0:a]volume={_db_to_amplitude(LEVEL_BGM):.4f}[out]",
        "-map", "[out]",
        "-t", f"{total_dur}",
        "-ac", str(CHANNELS), "-ar", str(SAMPLE_RATE),
        "-c:a", "libmp3lame", "-b:a", OUTPUT_BITRATE,
        dst,
    ]
    _run_ffmpeg(args, step="prepare_bgm")


def _prepare_ambient(src: str, dst: str, total_dur: float) -> None:
    """L2 Ambient: loop 到总时长,调到 ambient 音量层级。"""
    args = [
        "-stream_loop", "-1",
        "-i", src,
        "-filter_complex",
        f"[0:a]volume={_db_to_amplitude(LEVEL_AMBIENT):.4f}[out]",
        "-map", "[out]",
        "-t", f"{total_dur}",
        "-ac", str(CHANNELS), "-ar", str(SAMPLE_RATE),
        "-c:a", "libmp3lame", "-b:a", OUTPUT_BITRATE,
        dst,
    ]
    _run_ffmpeg(args, step="prepare_ambient")


def _prepare_room_tone(src: str, dst: str, total_dur: float) -> None:
    """L1 Room Tone: loop 到总时长,极低音量。"""
    args = [
        "-stream_loop", "-1",
        "-i", src,
        "-filter_complex",
        f"[0:a]volume={_db_to_amplitude(LEVEL_ROOM_TONE):.4f}[out]",
        "-map", "[out]",
        "-t", f"{total_dur}",
        "-ac", str(CHANNELS), "-ar", str(SAMPLE_RATE),
        "-c:a", "libmp3lame", "-b:a", OUTPUT_BITRATE,
        dst,
    ]
    _run_ffmpeg(args, step="prepare_room_tone")


def _prepare_sfx_track(events: list[SfxEvent],
                        dst: str, total_dur: float) -> None:
    """L3 SFX: 把所有 SFX 按 `at` 时刻精确放置到一条空白轨上。

    实现:
      1. 创建一条 total_dur 长度的静音轨
      2. 对每个 event,用 adelay 把 SFX 推到 at*1000 ms
      3. amix 所有事件
      4. 铁律 4: 整轨削顶到 SFX_PEAK_LIMIT_DB
    """
    if not events:
        # 没有 sfx → 生成纯静音轨
        args = [
            "-f", "lavfi",
            "-i", f"anullsrc=channel_layout=stereo:sample_rate={SAMPLE_RATE}",
            "-t", f"{total_dur}",
            "-c:a", "libmp3lame", "-b:a", OUTPUT_BITRATE,
            dst,
        ]
        _run_ffmpeg(args, step="prepare_sfx_silence")
        return

    # 多输入构造
    input_args = []
    filter_parts = []
    mix_inputs = []

    # input 0: 静音底
    input_args += ["-f", "lavfi", "-t", f"{total_dur}",
                   "-i", f"anullsrc=channel_layout=stereo:sample_rate={SAMPLE_RATE}"]
    filter_parts.append("[0:a]anull[base]")
    mix_inputs.append("[base]")

    for i, ev in enumerate(events, start=1):
        input_args += ["-i", ev.path]
        delay_ms = max(0, int(ev.at * 1000))
        vol_db = LEVEL_SFX_KEY if ev.important else ev.volume_db
        vol_amp = _db_to_amplitude(vol_db)
        # 每个 sfx: 调音量 → 立体声化 → 延迟到位
        filter_parts.append(
            f"[{i}:a]volume={vol_amp:.4f},"
            f"aformat=channel_layouts=stereo:sample_rates={SAMPLE_RATE},"
            f"adelay={delay_ms}|{delay_ms}[s{i}]"
        )
        mix_inputs.append(f"[s{i}]")

    # amix + 削顶
    n = len(mix_inputs)
    mix_concat = "".join(mix_inputs)
    # 铁律 4: 削顶
    limit_amp = _db_to_amplitude(SFX_PEAK_LIMIT_DB)
    filter_parts.append(
        f"{mix_concat}amix=inputs={n}:duration=longest:normalize=0[mix];"
        f"[mix]alimiter=limit={limit_amp:.4f}[out]"
    )

    filter_complex = ";".join(filter_parts)
    args = (
        input_args
        + ["-filter_complex", filter_complex,
           "-map", "[out]",
           "-t", f"{total_dur}",
           "-ac", str(CHANNELS), "-ar", str(SAMPLE_RATE),
           "-c:a", "libmp3lame", "-b:a", OUTPUT_BITRATE,
           dst]
    )
    _run_ffmpeg(args, step="prepare_sfx_track")


# ════════════════════════════════════════════════════════════════
# 核心混音
# ════════════════════════════════════════════════════════════════

def _mix_with_ducking(voice: str, bgm: Optional[str],
                       ambient: Optional[str], sfx: Optional[str],
                       room: Optional[str],
                       dst: str, total_dur: float,
                       enable_ducking: bool) -> None:
    """五层叠加。BGM 用 sidechaincompress 跟随 voice 做 ducking。

    铁律 1: 人声在最上
    铁律 3: BGM 在人声开口时自动让位
    """
    inputs = []
    streams = []   # 用于最终 amix 的 stream label
    filter_parts = []

    inputs += ["-i", voice]
    voice_idx = len(inputs) // 2 - 1  # 当前 input 序号
    streams.append("[v_final]")
    # voice 不再处理,只取出来命名
    filter_parts.append(f"[{voice_idx}:a]anull[v_final]")

    if bgm:
        inputs += ["-i", bgm]
        bgm_idx = len(inputs) // 2 - 1
        if enable_ducking:
            # sidechaincompress: bgm 是被压缩的,voice 是侧链触发的
            # threshold/ratio/attack/release 都按 DUCK_* 常量
            filter_parts.append(
                f"[{voice_idx}:a]asplit=2[v_for_mix][v_sidechain]"
            )
            # 重新命名 voice 的最终引用
            filter_parts[0] = "[v_for_mix]anull[v_final]"
            makeup_amp = _db_to_amplitude(DUCK_MAKEUP_DB)
            filter_parts.append(
                f"[{bgm_idx}:a][v_sidechain]"
                f"sidechaincompress=threshold={DUCK_THRESHOLD}:"
                f"ratio={DUCK_RATIO}:"
                f"attack={DUCK_ATTACK_MS}:release={DUCK_RELEASE_MS},"
                f"volume={makeup_amp:.4f}[bgm_ducked]"
            )
            streams.append("[bgm_ducked]")
        else:
            filter_parts.append(f"[{bgm_idx}:a]anull[bgm_plain]")
            streams.append("[bgm_plain]")

    if ambient:
        inputs += ["-i", ambient]
        amb_idx = len(inputs) // 2 - 1
        filter_parts.append(f"[{amb_idx}:a]anull[amb_final]")
        streams.append("[amb_final]")

    if sfx:
        inputs += ["-i", sfx]
        sfx_idx = len(inputs) // 2 - 1
        filter_parts.append(f"[{sfx_idx}:a]anull[sfx_final]")
        streams.append("[sfx_final]")

    if room:
        inputs += ["-i", room]
        room_idx = len(inputs) // 2 - 1
        filter_parts.append(f"[{room_idx}:a]anull[room_final]")
        streams.append("[room_final]")

    # 最终 amix
    mix_concat = "".join(streams)
    n = len(streams)
    filter_parts.append(
        f"{mix_concat}amix=inputs={n}:duration=longest:normalize=0[final]"
    )
    filter_complex = ";".join(filter_parts)

    args = (
        inputs
        + ["-filter_complex", filter_complex,
           "-map", "[final]",
           "-t", f"{total_dur}",
           "-ac", str(CHANNELS), "-ar", str(SAMPLE_RATE),
           "-c:a", "libmp3lame", "-b:a", OUTPUT_BITRATE,
           dst]
    )
    _run_ffmpeg(args, step="mix_with_ducking")


def _loudnorm(src: str, dst: str) -> None:
    """铁律 5: 整片响度标准化到 YouTube 标准。"""
    args = [
        "-i", src,
        "-af",
        f"loudnorm=I={LOUDNORM_TARGET_I}:"
        f"LRA={LOUDNORM_TARGET_LRA}:"
        f"TP={LOUDNORM_TARGET_TP}",
        "-ac", str(CHANNELS), "-ar", str(SAMPLE_RATE),
        "-c:a", "libmp3lame", "-b:a", OUTPUT_BITRATE,
        dst,
    ]
    _run_ffmpeg(args, step="loudnorm")


# ════════════════════════════════════════════════════════════════
# 公开 API
# ════════════════════════════════════════════════════════════════

def mix(spec: MixInput, out_path: str) -> float:
    """主入口: 把 5 层素材融合成最终 mp3。

    Args:
        spec:     MixInput,五层素材路径 + 设置
        out_path: 最终 mp3 路径

    Returns:
        最终音频时长(秒)
    """
    if not spec.voice_path:
        raise ValueError("voice_path is required (L4 人声不能缺)")

    # 决定总时长
    if spec.force_duration is not None:
        total_dur = spec.force_duration
    else:
        candidates = [_audio_duration(spec.voice_path)]
        if spec.bgm_path:
            candidates.append(_audio_duration(spec.bgm_path))
        if spec.ambient_path:
            candidates.append(_audio_duration(spec.ambient_path))
        total_dur = max(candidates)

    print(f"[mixer] 总时长: {total_dur:.2f}s")

    tmp_dir = Path(tempfile.mkdtemp(prefix="mixer_"))
    try:
        # 1. 各层预处理(对齐时长 + 音量层级)
        voice_p   = str(tmp_dir / "L4_voice.mp3")
        _prepare_voice(spec.voice_path, voice_p, total_dur)

        bgm_p     = None
        ambient_p = None
        sfx_p     = None
        room_p    = None

        if spec.bgm_path:
            bgm_p = str(tmp_dir / "L5_bgm.mp3")
            _prepare_bgm(spec.bgm_path, bgm_p, total_dur)

        if spec.ambient_path:
            ambient_p = str(tmp_dir / "L2_ambient.mp3")
            _prepare_ambient(spec.ambient_path, ambient_p, total_dur)

        if spec.sfx_events:
            sfx_p = str(tmp_dir / "L3_sfx.mp3")
            _prepare_sfx_track(spec.sfx_events, sfx_p, total_dur)

        if spec.room_tone_path:
            room_p = str(tmp_dir / "L1_room.mp3")
            _prepare_room_tone(spec.room_tone_path, room_p, total_dur)

        print(f"[mixer] 各层预处理完成")

        # 2. 核心混音(ducking + 五层叠加)
        mixed_p = str(tmp_dir / "mixed.mp3")
        _mix_with_ducking(
            voice_p, bgm_p, ambient_p, sfx_p, room_p,
            mixed_p, total_dur, spec.enable_ducking,
        )
        print(f"[mixer] 五层叠加完成 (ducking={'on' if spec.enable_ducking else 'off'})")

        # 3. 整片响度标准化
        if spec.enable_loudnorm:
            _loudnorm(mixed_p, out_path)
            print(f"[mixer] loudnorm → {LOUDNORM_TARGET_I} LUFS")
        else:
            shutil.copy(mixed_p, out_path)

        final_dur = _audio_duration(out_path)
        print(f"[mixer] ✓ 完成: {out_path} ({final_dur:.2f}s)")
        return final_dur

    finally:
        if not os.environ.get("MIXER_KEEP_TMP"):
            shutil.rmtree(tmp_dir, ignore_errors=True)
        else:
            print(f"[mixer] 保留中间文件: {tmp_dir}")


# ════════════════════════════════════════════════════════════════
# CLI: python -m core.audio.mixer <config.json> <out.mp3>
# ════════════════════════════════════════════════════════════════

def _from_json(path: str) -> MixInput:
    """读 JSON 配置,转 MixInput。

    JSON 格式示例:
    {
      "voice_path": "/path/to/narration.mp3",
      "bgm_path": "/path/to/bgm.mp3",
      "ambient_path": "/path/to/rain.wav",
      "sfx_events": [
        {"path": "/path/to/thunder.wav", "at": 2.5, "important": true},
        {"path": "/path/to/bell.wav", "at": 8.0}
      ],
      "room_tone_path": null,
      "enable_ducking": true,
      "enable_loudnorm": true
    }
    """
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    events = [SfxEvent(**ev) for ev in data.get("sfx_events", [])]
    return MixInput(
        voice_path     = data.get("voice_path"),
        bgm_path       = data.get("bgm_path"),
        ambient_path   = data.get("ambient_path"),
        sfx_events     = events,
        room_tone_path = data.get("room_tone_path"),
        enable_ducking = data.get("enable_ducking", True),
        enable_loudnorm = data.get("enable_loudnorm", True),
        force_duration  = data.get("force_duration"),
    )


def _cli_main():
    import sys
    if len(sys.argv) < 3:
        print("Usage: python -m core.audio.mixer <config.json> <out.mp3>")
        print()
        print("config.json 示例:")
        print(json.dumps({
            "voice_path": "/path/narration.mp3",
            "bgm_path": "/path/bgm.mp3",
            "ambient_path": "/path/rain.wav",
            "sfx_events": [
                {"path": "/path/thunder.wav", "at": 2.5, "important": True}
            ],
            "enable_ducking": True,
            "enable_loudnorm": True,
        }, indent=2, ensure_ascii=False))
        sys.exit(1)
    cfg = _from_json(sys.argv[1])
    mix(cfg, sys.argv[2])


if __name__ == "__main__":
    _cli_main()
