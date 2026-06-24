"""
tools/sfx_smart_trim.py ── 智能 SFX 裁剪
================================================================
解决问题: 素材库存原始长音频(30-60 秒),用的时候按需裁出 N 秒。
        不需要 AI 模型,纯 ffmpeg + 信号处理。

核心算法(3 步):
  1. ffmpeg 算 RMS 响度曲线 (每 0.1 秒一个采样点)
  2. 滑动窗口找"最响的 N 秒"位置
  3. ffmpeg -ss start -t N 裁出来

特性:
  - 自动找峰值: 不再手工对齐起点
  - 一份素材多次复用: 同一文件可以切 0.5s/3s/8s 不同长度
  - 离线运行,零 API,纯本地

用法 1: 命令行
  python tools/sfx_smart_trim.py input.wav output.wav --duration 3
  python tools/sfx_smart_trim.py input.wav output.wav --duration 0.5 --mode peak

用法 2: import
  from sfx_smart_trim import smart_trim
  smart_trim("input.wav", "output.wav", duration=3.0, mode="peak")

模式说明:
  peak       (默认) 找最响的窗口,适合 stinger(炸雷、玻璃碎)
  loudest    同 peak,别名
  steady     找最"稳定不变"的窗口,适合 ambient(雨声、风声)
  head       从开头截(零智能,留作兜底)
"""

from __future__ import annotations
import subprocess
import sys
import json
import argparse
from pathlib import Path


# ════════════════════════════════════════════════════════════════
# 工具: 跑 ffmpeg / ffprobe
# ════════════════════════════════════════════════════════════════

def _audio_duration(path: str) -> float:
    """音频总时长。"""
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", path],
        capture_output=True, text=True
    )
    return float(result.stdout.strip())


def _extract_loudness_curve(path: str, hop_sec: float = 0.1) -> tuple[list[float], float]:
    """
    用 ffmpeg loudnorm 单遍统计 + showvolume 不行,用最朴素的:
    用 astats 切片到独立段。

    实现: 每 hop_sec 用 atrim 切一段,跑 volumedetect 拿 max_volume / mean_volume。
    简单但慢(N 次 ffmpeg 调用)。短文件可接受。

    返回: (rms_curve_db, actual_hop_sec)
    """
    total_dur = _audio_duration(path)
    n_segments = max(1, int(total_dur / hop_sec))
    actual_hop = total_dur / n_segments

    rms_values = []
    for i in range(n_segments):
        start = i * actual_hop
        cmd = [
            "ffmpeg", "-hide_banner", "-nostats", "-ss", f"{start}",
            "-i", path, "-t", f"{actual_hop}",
            "-af", "volumedetect", "-f", "null", "-",
        ]
        result = subprocess.run(cmd, capture_output=True, text=True)
        # 找 mean_volume
        mean_db = -120.0
        for line in result.stderr.split("\n"):
            if "mean_volume:" in line:
                try:
                    val = line.split("mean_volume:")[1].strip().split()[0]
                    mean_db = float(val)
                except (ValueError, IndexError):
                    pass
                break
        rms_values.append(mean_db)

    return rms_values, actual_hop


# ════════════════════════════════════════════════════════════════
# 算法: 找最响 N 秒窗口
# ════════════════════════════════════════════════════════════════

def _find_loudest_window(rms_curve: list[float],
                          hop_sec: float,
                          window_sec: float) -> tuple[float, float]:
    """
    滑动窗口找最响的 N 秒。

    Args:
        rms_curve:  响度曲线 (dB)
        hop_sec:    曲线时间分辨率(秒)
        window_sec: 想要的窗口长度(秒)

    Returns:
        (start_sec, end_sec) 最响窗口的起止时刻
    """
    if not rms_curve:
        return 0.0, window_sec

    win_size = max(1, int(round(window_sec / hop_sec)))
    if win_size >= len(rms_curve):
        return 0.0, len(rms_curve) * hop_sec

    # 把 -inf / 极低 dB 转为线性能量更好叠加
    # 但简单起见,直接 dB 求和也能找峰值位置
    best_sum = -float("inf")
    best_start = 0

    # 滑动窗口求和
    current_sum = sum(rms_curve[:win_size])
    best_sum = current_sum
    best_start = 0

    for i in range(1, len(rms_curve) - win_size + 1):
        current_sum = current_sum - rms_curve[i-1] + rms_curve[i+win_size-1]
        if current_sum > best_sum:
            best_sum = current_sum
            best_start = i

    start_sec = best_start * hop_sec
    end_sec = start_sec + window_sec
    return start_sec, end_sec


def _find_steady_window(rms_curve: list[float],
                         hop_sec: float,
                         window_sec: float) -> tuple[float, float]:
    """
    找最"稳定"的窗口 (RMS 变化最小)。适合 ambient。

    用窗口内 RMS 的标准差当评分,越小越稳定。
    """
    if not rms_curve:
        return 0.0, window_sec

    win_size = max(1, int(round(window_sec / hop_sec)))
    if win_size >= len(rms_curve):
        return 0.0, len(rms_curve) * hop_sec

    best_std = float("inf")
    best_start = 0
    best_mean = -float("inf")

    for i in range(0, len(rms_curve) - win_size + 1):
        window = rms_curve[i:i+win_size]
        mean = sum(window) / len(window)
        # 加权: 不仅要稳,还要有一定响度(不要全静音)
        if mean < -60:
            continue
        variance = sum((x - mean) ** 2 for x in window) / len(window)
        std = variance ** 0.5
        # 偏好低 std 且高 mean
        if std < best_std:
            best_std = std
            best_start = i
            best_mean = mean

    start_sec = best_start * hop_sec
    end_sec = start_sec + window_sec
    return start_sec, end_sec


# ════════════════════════════════════════════════════════════════
# 主函数
# ════════════════════════════════════════════════════════════════

def smart_trim(input_path: str, output_path: str,
                duration: float,
                mode: str = "peak",
                sample_rate: int = 44100,
                channels: int = 2,
                pre_pad: float = 0.0,
                hop_sec: float = 0.1,
                verbose: bool = True) -> dict:
    """
    智能裁剪音频。

    Args:
        input_path:    原始音频(支持 wav/mp3/ogg/flac)
        output_path:   输出 wav 路径
        duration:      想要的时长(秒)
        mode:          peak / steady / head
        sample_rate:   输出采样率,默认 44100
        channels:      输出声道数,默认 2(立体声)
        pre_pad:       在峰值前预留多少秒(让听感不那么"突然炸响")
                       例:duration=3, pre_pad=0.3 → 峰值前 0.3s + 后 2.7s
        hop_sec:       响度曲线时间分辨率,越小越精确但越慢

    Returns:
        info dict {
          "start_sec": float,
          "end_sec": float,
          "mode": str,
          "input_duration": float,
          "output_duration": float,
        }
    """
    input_path = str(input_path)
    output_path = str(output_path)

    # 1. 总时长
    total_dur = _audio_duration(input_path)
    if duration >= total_dur:
        if verbose:
            print(f"  [smart_trim] 想要 {duration}s 超过原长 {total_dur:.2f}s,"
                  f"直接全文件转格式")
        start, end = 0.0, total_dur
    elif mode == "head":
        # 头部截取
        start, end = 0.0, duration
    else:
        # 2. 算响度曲线
        if verbose:
            print(f"  [smart_trim] 分析响度曲线 ({total_dur:.1f}s, hop≈{hop_sec}s)...")
        rms_curve, actual_hop = _extract_loudness_curve(input_path, hop_sec=hop_sec)
        if not rms_curve:
            if verbose:
                print(f"  [smart_trim] ⚠ 响度曲线为空,降级 head 模式")
            start, end = 0.0, duration
        else:
            # 3. 找窗口 (用实际 hop)
            if mode in ("peak", "loudest"):
                start, end = _find_loudest_window(rms_curve, actual_hop, duration)
            elif mode == "steady":
                start, end = _find_steady_window(rms_curve, actual_hop, duration)
            else:
                raise ValueError(f"未知 mode: {mode}")

            # pre_pad: 起点往前挪一点,让峰值不在最开头
            if pre_pad > 0 and start > pre_pad:
                start -= pre_pad
                end -= pre_pad

            if verbose:
                print(f"  [smart_trim] 找到 {mode} 窗口: "
                      f"{start:.2f}s - {end:.2f}s")

    # 4. ffmpeg 裁剪 + 转格式
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-i", input_path,
        "-ss", f"{start}", "-t", f"{end - start}",
        "-ar", str(sample_rate),
        "-ac", str(channels),
        "-c:a", "pcm_s16le",
        output_path,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg 失败:\n{result.stderr}")

    final_dur = _audio_duration(output_path)
    if verbose:
        print(f"  [smart_trim] ✓ 输出: {output_path} ({final_dur:.2f}s)")

    return {
        "start_sec": start,
        "end_sec": end,
        "mode": mode,
        "input_duration": total_dur,
        "output_duration": final_dur,
    }


# ════════════════════════════════════════════════════════════════
# 批量包装: 按 registry 自动跑
# ════════════════════════════════════════════════════════════════

def batch_trim_from_registry(downloads_dir: str,
                              refs_dir: str = "refs/sfx",
                              registry_path: str = "refs/sfx/registry.yaml"):
    """
    扫 ~/Downloads,按文件名关键词匹配 registry 里的 sfx_id,
    自动 smart_trim 到对应文件夹。

    命名规则: 下载文件名包含 sfx_id 作为子串即可。
      例: 把 freesound 文件改名为 "thunder_crack_freesound717909.mp3"
          → 自动 trim 到 refs/sfx/stinger/thunder_crack/

    每个 sfx_id 的目标时长:
      ambient 类 → 30 秒(steady 模式)
      stinger 类 → 3 秒(peak 模式)
      transition → 2 秒(peak 模式)
    """
    import yaml

    with open(registry_path, "r", encoding="utf-8") as f:
        reg = yaml.safe_load(f)

    DURATION_BY_CATEGORY = {
        "ambient":    (30.0, "steady"),
        "stinger":    (3.0, "peak"),
        "transition": (2.0, "peak"),
    }

    downloads = list(Path(downloads_dir).glob("*"))
    audio_files = [f for f in downloads
                    if f.suffix.lower() in (".mp3", ".wav", ".ogg", ".flac")]

    matched = []
    for category, entries in reg.items():
        if category not in DURATION_BY_CATEGORY:
            continue
        duration, mode = DURATION_BY_CATEGORY[category]

        for sfx_id in entries:
            # 找包含此 id 的下载文件
            candidates = [f for f in audio_files
                          if sfx_id in f.stem.lower()]
            if not candidates:
                continue

            src = candidates[0]
            dst = Path(refs_dir) / category / sfx_id / f"{sfx_id}_01.wav"

            print(f"\n[{category}/{sfx_id}] {src.name}")
            try:
                info = smart_trim(str(src), str(dst),
                                  duration=duration, mode=mode)
                matched.append((sfx_id, info))
            except Exception as e:
                print(f"  ✗ 失败: {e}")

    print(f"\n=== 共处理 {len(matched)} 个 sfx ===")


# ════════════════════════════════════════════════════════════════
# CLI
# ════════════════════════════════════════════════════════════════

def _cli_main():
    parser = argparse.ArgumentParser(
        description="智能裁剪音频 - 自动找峰值/稳定段"
    )
    parser.add_argument("input", help="输入音频")
    parser.add_argument("output", nargs="?", help="输出 wav (单文件模式)")
    parser.add_argument("--duration", "-d", type=float, default=3.0,
                        help="目标时长(秒),默认 3")
    parser.add_argument("--mode", "-m", default="peak",
                        choices=["peak", "loudest", "steady", "head"],
                        help="peak=找最响(stinger),steady=找最稳(ambient)")
    parser.add_argument("--pre-pad", type=float, default=0.0,
                        help="峰值前预留秒数,默认 0")
    parser.add_argument("--batch", action="store_true",
                        help="批量模式: input 是 downloads 目录")
    parser.add_argument("--refs-dir", default="refs/sfx",
                        help="批量模式输出根目录")
    args = parser.parse_args()

    if args.batch:
        batch_trim_from_registry(args.input, args.refs_dir)
    else:
        if not args.output:
            print("单文件模式必须提供 output")
            sys.exit(1)
        info = smart_trim(args.input, args.output,
                          duration=args.duration,
                          mode=args.mode,
                          pre_pad=args.pre_pad)
        print(json.dumps(info, indent=2))


if __name__ == "__main__":
    _cli_main()
