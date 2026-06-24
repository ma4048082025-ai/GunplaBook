"""
voice_library_builder.py v2 ── 声音库构建器（修复 mp3 假冒 wav）
=====================================================================
v2 修复：
  ★ Edge TTS 默认输出 mp3 数据流，v1 直接保存到 .wav 后缀产生"假 wav"
  ★ v2 流程：Edge TTS → 临时 mp3 → ffmpeg 转真 wav → 删除临时文件
  ★ wav 格式：32000Hz / 单声道 / 16bit PCM（GPT-SoVITS 推荐）
  ★ 验证：生成后检查时长 ≥ 5 秒（GPT-SoVITS 建议参考音频 5-10 秒）

依赖：ffmpeg / edge-tts / paramiko / pyyaml

用法：
  python -m tools.voice_library_builder              # 全部生成（含已生成的会跳过）
  python -m tools.voice_library_builder --voice young_male_scholar
  python -m tools.voice_library_builder --no-sync    # 不上传 Win10
  python -m tools.voice_library_builder --force      # 强制重新生成
  python -m tools.voice_library_builder --verify     # 只检查现有文件格式
  python -m tools.voice_library_builder --fix-existing  # 把现有"假wav"修成真wav
"""

import argparse
import asyncio
import hashlib
import json
import subprocess
from pathlib import Path

import yaml


LIBRARY_YAML = "refs/voice_library.yaml"
LIBRARY_DIR  = Path("refs/voices/library")
HASH_FILE    = LIBRARY_DIR / ".hashes.json"

REMOTE_LIBRARY_DIR = "H:/voice_refs/library/"

# GPT-SoVITS 推荐参数
TARGET_SAMPLE_RATE = 32000
TARGET_CHANNELS    = 1
MIN_DURATION_SEC   = 5.0      # 参考音频建议至少 5 秒


# ════════════════════════════════════════════════════════════════
# 工具函数
# ════════════════════════════════════════════════════════════════

def _hash_def(voice_def: dict) -> str:
    key_fields = ("voice_id", "style", "rate", "pitch", "sample_text")
    s = "|".join(str(voice_def.get(k, "")) for k in key_fields)
    return hashlib.md5(s.encode("utf-8")).hexdigest()[:12]


def _load_hashes() -> dict:
    if HASH_FILE.exists():
        try:
            return json.loads(HASH_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def _save_hashes(hashes: dict):
    LIBRARY_DIR.mkdir(parents=True, exist_ok=True)
    HASH_FILE.write_text(
        json.dumps(hashes, ensure_ascii=False, indent=2),
        encoding="utf-8")


def _audio_format(path: Path) -> dict:
    """读音频格式信息：codec/sample_rate/duration/channels"""
    if not path.exists():
        return {"valid": False, "reason": "文件不存在"}
    try:
        # 同时读 format（拿 duration）和 streams（拿 codec）
        r = subprocess.run(
            ["ffprobe", "-v", "quiet", "-print_format", "json",
             "-show_format", "-show_streams", "-select_streams", "a:0",
             str(path)],
            capture_output=True, text=True, check=True)
        data = json.loads(r.stdout)
        fmt_name  = data.get("format", {}).get("format_name", "?")
        duration  = float(data.get("format", {}).get("duration", 0))
        streams   = data.get("streams", [])
        codec     = streams[0].get("codec_name", "?") if streams else "?"
        sr        = int(streams[0].get("sample_rate", 0)) if streams else 0
        ch        = int(streams[0].get("channels", 0)) if streams else 0
        return {
            "valid":       True,
            "format":      fmt_name,
            "codec":       codec,
            "sample_rate": sr,
            "channels":    ch,
            "duration":    duration,
            "size_kb":     path.stat().st_size / 1024,
        }
    except Exception as e:
        return {"valid": False, "reason": str(e)}


def _is_real_wav(path: Path) -> bool:
    """检查是否是真正的 wav（不是 mp3 假冒）"""
    info = _audio_format(path)
    if not info.get("valid"):
        return False
    return info["codec"] in ("pcm_s16le", "pcm_s24le", "pcm_f32le", "pcm_s32le")


# ════════════════════════════════════════════════════════════════
# 单个声音生成
# ════════════════════════════════════════════════════════════════

async def _generate_one(voice_key: str, voice_def: dict) -> bool:
    """
    用 Edge TTS 生成 mp3，ffmpeg 转为真 wav，验证完整性。
    """
    try:
        import edge_tts
    except ImportError:
        print("  [build] ❌ 需要 edge-tts: pip install edge-tts")
        return False

    voice_id    = voice_def["voice_id"]
    rate        = voice_def.get("rate", "+0%")
    pitch       = voice_def.get("pitch", "+0Hz")
    sample_text = voice_def["sample_text"]

    final_wav   = LIBRARY_DIR / f"{voice_key}.wav"
    final_txt   = LIBRARY_DIR / f"{voice_key}.txt"
    tmp_mp3     = LIBRARY_DIR / f"_tmp_{voice_key}.mp3"

    # ── Step 1: Edge TTS → mp3 ────────────────────────────────
    try:
        comm = edge_tts.Communicate(
            sample_text,
            voice = voice_id,
            rate  = rate,
            pitch = pitch,
        )
        await comm.save(str(tmp_mp3))
    except Exception as e:
        print(f"  [build] ❌ Edge TTS 调用失败 [{voice_key}]: {e}")
        tmp_mp3.unlink(missing_ok=True)
        return False

    if not tmp_mp3.exists() or tmp_mp3.stat().st_size < 1000:
        print(f"  [build] ❌ [{voice_key}] Edge TTS 返回空数据")
        print(f"            可能是网络问题（Edge TTS 走 Microsoft Azure 海外）")
        print(f"            建议设置代理：export HTTP_PROXY=http://127.0.0.1:7890")
        tmp_mp3.unlink(missing_ok=True)
        return False

    # ── Step 2: ffmpeg mp3 → 真 wav ───────────────────────────
    try:
        subprocess.run([
            "ffmpeg", "-y",
            "-i", str(tmp_mp3),
            "-ar", str(TARGET_SAMPLE_RATE),
            "-ac", str(TARGET_CHANNELS),
            "-c:a", "pcm_s16le",       # 强制 PCM 16bit
            str(final_wav),
        ], check=True, capture_output=True)
    except subprocess.CalledProcessError as e:
        print(f"  [build] ❌ ffmpeg 转换失败 [{voice_key}]")
        print(f"            stderr: {e.stderr.decode()[:300]}")
        tmp_mp3.unlink(missing_ok=True)
        return False

    # ── Step 3: 验证 ───────────────────────────────────────────
    info = _audio_format(final_wav)
    if not info.get("valid"):
        print(f"  [build] ❌ [{voice_key}] 验证失败: {info.get('reason')}")
        tmp_mp3.unlink(missing_ok=True)
        return False

    duration = info["duration"]
    if duration < MIN_DURATION_SEC:
        print(f"  [build] ⚠ [{voice_key}] 时长 {duration:.1f}s < {MIN_DURATION_SEC}s "
              f"（GPT-SoVITS 建议 5-10 秒）")
        # 不阻断，但提示用户改 sample_text 加长

    # 写参考文字
    final_txt.write_text(sample_text, encoding="utf-8")

    # 清理临时文件
    tmp_mp3.unlink(missing_ok=True)

    print(f"  [build] ✓ {voice_key:30s} "
          f"({voice_id}, {duration:.1f}s, "
          f"{info['codec']} {info['sample_rate']}Hz, {info['size_kb']:.1f}KB)")
    return True


# ════════════════════════════════════════════════════════════════
# 验证现有文件
# ════════════════════════════════════════════════════════════════

def verify_library() -> dict:
    """检查所有现有 wav 是否是真 wav，返回 {key: status}"""
    print(f"\n  [verify] 检查 {LIBRARY_DIR}/")
    if not LIBRARY_DIR.exists():
        print(f"  [verify] 目录不存在")
        return {}

    results = {}
    wavs = sorted(LIBRARY_DIR.glob("*.wav"))
    for wav in wavs:
        info = _audio_format(wav)
        is_real = info.get("codec", "").startswith("pcm")
        status = "✓ real_wav" if is_real else f"✗ FAKE ({info.get('codec', '?')})"
        results[wav.stem] = {
            "is_real_wav": is_real,
            "codec":       info.get("codec"),
            "duration":    info.get("duration", 0),
            "sample_rate": info.get("sample_rate", 0),
        }
        print(f"  {status:20s}  {wav.name:35s}  "
              f"{info.get('duration', 0):.1f}s  "
              f"{info.get('sample_rate', 0)}Hz")
    return results


def fix_existing():
    """把现有的'假 wav'（实际是 mp3）转为真 wav"""
    print(f"\n  [fix] 修复现有的假 wav...")
    wavs = sorted(LIBRARY_DIR.glob("*.wav"))
    fixed = 0
    skipped = 0

    for wav in wavs:
        info = _audio_format(wav)
        codec = info.get("codec", "")

        if codec.startswith("pcm"):
            print(f"  [fix] = {wav.name} 已经是真 wav，跳过")
            skipped += 1
            continue

        # 是假 wav（mp3 内容），需要转换
        tmp_orig = wav.with_suffix(".wav.orig")
        wav.rename(tmp_orig)

        try:
            subprocess.run([
                "ffmpeg", "-y",
                "-i", str(tmp_orig),
                "-ar", str(TARGET_SAMPLE_RATE),
                "-ac", str(TARGET_CHANNELS),
                "-c:a", "pcm_s16le",
                str(wav),
            ], check=True, capture_output=True)
            tmp_orig.unlink()
            new_info = _audio_format(wav)
            print(f"  [fix] ✓ {wav.name} {codec} → pcm_s16le "
                  f"({new_info['duration']:.1f}s)")
            fixed += 1
        except subprocess.CalledProcessError as e:
            print(f"  [fix] ❌ {wav.name} 转换失败")
            tmp_orig.rename(wav)  # 恢复

    print(f"\n  [fix] 完成: {fixed} 修复, {skipped} 跳过")
    return fixed


# ════════════════════════════════════════════════════════════════
# 主流程
# ════════════════════════════════════════════════════════════════

async def build_library(only: str = None,
                         force: bool = False,
                         do_sync: bool = True):
    yaml_path = Path(LIBRARY_YAML)
    if not yaml_path.exists():
        print(f"  [build] ❌ 找不到 {LIBRARY_YAML}")
        return

    with open(yaml_path, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)

    voices = cfg.get("voices", {})
    if not voices:
        print(f"  [build] ❌ {LIBRARY_YAML} 中无 voices 定义")
        return

    LIBRARY_DIR.mkdir(parents=True, exist_ok=True)

    hashes = _load_hashes()
    new_hashes = dict(hashes)

    targets = [(k, v) for k, v in voices.items() if not only or k == only]
    if only and not targets:
        print(f"  [build] ❌ 找不到声音: {only}")
        return

    print(f"\n  [build] 共 {len(targets)} 个声音需要处理\n")

    succeeded = 0
    skipped   = 0
    failed    = 0

    for vkey, vdef in targets:
        h        = _hash_def(vdef)
        out_wav  = LIBRARY_DIR / f"{vkey}.wav"
        old_hash = hashes.get(vkey)

        # 幂等检查（且必须是真 wav）
        if (not force and out_wav.exists()
                and old_hash == h
                and _is_real_wav(out_wav)):
            print(f"  [build] = {vkey:30s} (unchanged + real_wav, skip)")
            skipped += 1
            continue

        ok = await _generate_one(vkey, vdef)
        if ok:
            succeeded += 1
            new_hashes[vkey] = h
        else:
            failed += 1

    _save_hashes(new_hashes)
    print(f"\n  [build] 完成: {succeeded} 生成, {skipped} 跳过, {failed} 失败")

    if do_sync and (succeeded > 0 or force):
        print(f"\n  [build] 同步到 Win10 ...")
        try:
            from sync_to_remote import sync_dir
            sync_dir(str(LIBRARY_DIR) + "/", REMOTE_LIBRARY_DIR)
        except Exception as e:
            print(f"  [build] ⚠ 同步失败: {e}")
            print(f"  [build]   重试: python -m tools.sync_to_remote "
                  f"refs/voices/library/ {REMOTE_LIBRARY_DIR}")
    elif not do_sync:
        print(f"\n  [build] 跳过同步（--no-sync）")


def main():
    parser = argparse.ArgumentParser(description="声音库构建器 v2")
    parser.add_argument("--voice",        default=None, help="只生成指定声音")
    parser.add_argument("--no-sync",      action="store_true", help="不同步到 Win10")
    parser.add_argument("--force",        action="store_true", help="强制重新生成")
    parser.add_argument("--verify",       action="store_true", help="只验证现有文件")
    parser.add_argument("--fix-existing", action="store_true",
                        help="把现有假 wav 修成真 wav（不调 Edge TTS）")
    args = parser.parse_args()

    if args.verify:
        verify_library()
        return

    if args.fix_existing:
        fix_existing()
        # 修完后同步
        if not args.no_sync:
            print(f"\n  [fix] 同步到 Win10 ...")
            try:
                from sync_to_remote import sync_dir
                sync_dir(str(LIBRARY_DIR) + "/", REMOTE_LIBRARY_DIR)
            except Exception as e:
                print(f"  [fix] ⚠ 同步失败: {e}")
        return

    asyncio.run(build_library(
        only    = args.voice,
        force   = args.force,
        do_sync = not args.no_sync,
    ))


if __name__ == "__main__":
    main()
