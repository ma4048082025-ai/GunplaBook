from config import SOVITS_URL
"""
voice_engine.py ── 角色配音引擎 v2
======================================
v2 改动（基于 v1）：
  1. GPT-SoVITS API 改为 GET（与你测试通的方式一致）
  2. 新增 ref_id 字段（引用 voice_library.yaml 里的 key），自动解析 ref_audio + ref_text
  3. Mac 路径自动转换为 Win10 路径（PATH_MAP）
  4. 加 __main__ 入口，独立测试单个角色或一键测全部角色

YAML 配置示例（推荐方式）：
  characters:
    innkeeper_ghost:
      voice:
        engine: gpt_sovits
        ref_id: ghost_female_sorrowful   # ← 引用声音库

旧方式（向后兼容）：
  characters:
    innkeeper_ghost:
      voice:
        engine: gpt_sovits
        ref_audio: refs/voices/library/ghost_female_sorrowful.wav
        ref_text: "我等了好多年..."

独立测试：
  python -m core.voice_engine --story stories/xxx.yaml --char innkeeper_ghost --text "客官请进"
  python -m core.voice_engine --story stories/xxx.yaml --all
  python -m core.voice_engine --story stories/xxx.yaml --char-list
"""

import asyncio
import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import yaml
import requests

from pronunciation_fix import fix_pronunciation


# ════════════════════════════════════════════════════════════════
# 路径转换（Mac → Win10）
# ════════════════════════════════════════════════════════════════

# Mac 端逻辑路径 → Win10 端物理路径
VOICE_PATH_MAP = {
    "refs/voices/library":  "H:/voice_refs/library",
    "refs/voices/stories":  "H:/voice_refs/stories",
}


def _mac_to_win(mac_path: str) -> str:
    """
    Mac 路径转 Win10 路径。
    refs/voices/library/x.wav → H:/voice_refs/library/x.wav
    其他路径原样返回（假设已经是绝对路径或 Win10 能直接读）。
    """
    if not mac_path:
        return mac_path
    # 标准化分隔符
    p = mac_path.replace("\\", "/")
    for mac_prefix, win_prefix in VOICE_PATH_MAP.items():
        if p.startswith(mac_prefix):
            return p.replace(mac_prefix, win_prefix, 1)
    # 已经是 Win10 绝对路径
    return p


# ════════════════════════════════════════════════════════════════
# 声音库加载（解析 ref_id）
# ════════════════════════════════════════════════════════════════

_VOICE_LIBRARY_CACHE = None


def _load_voice_library() -> dict:
    """加载 refs/voice_library.yaml 到内存（懒加载 + 缓存）"""
    global _VOICE_LIBRARY_CACHE
    if _VOICE_LIBRARY_CACHE is not None:
        return _VOICE_LIBRARY_CACHE

    yaml_path = Path("refs/voice_library.yaml")
    if not yaml_path.exists():
        _VOICE_LIBRARY_CACHE = {}
        return {}

    with open(yaml_path, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    _VOICE_LIBRARY_CACHE = data.get("voices", {})
    return _VOICE_LIBRARY_CACHE


def _resolve_ref_id(ref_id: str) -> tuple:
    """
    根据 ref_id 解析参考音频路径和文字。
    返回 (mac_path, ref_text) 或 (None, None) 如果找不到。
    """
    library = _load_voice_library()
    if ref_id not in library:
        return None, None

    voice_def = library[ref_id]
    sample_text = voice_def.get("sample_text", "")

    mac_wav  = f"refs/voices/library/{ref_id}.wav"
    mac_txt  = f"refs/voices/library/{ref_id}.txt"

    # 优先读 .txt 文件（构建库时写入的实际文字）
    txt_path = Path(mac_txt)
    if txt_path.exists():
        try:
            sample_text = txt_path.read_text(encoding="utf-8").strip()
        except Exception:
            pass

    return mac_wav, sample_text


# ════════════════════════════════════════════════════════════════
# 数据类
# ════════════════════════════════════════════════════════════════

@dataclass
class VoiceConfig:
    engine:    str = "edge_tts"           # edge_tts | gpt_sovits
    voice_id:  str = "zh-CN-YunxiNeural"
    ref_audio: str = ""                   # Mac 路径
    ref_text:  str = ""
    language:  str = "zh"
    ref_id:    str = ""                   # 声音库引用 key（v2 新增）


# ════════════════════════════════════════════════════════════════
# VoiceEngine 主类
# ════════════════════════════════════════════════════════════════

class VoiceEngine:

    def __init__(self, story,
                 sovits_host: str = None,
                 default_voice: str = "zh-CN-YunxiNeural"):
        self.story         = story
        self.sovits_host   = sovits_host.rstrip("/") if sovits_host else ""
        self.default_voice = default_voice

        # 解析每个角色的 voice 配置
        self._voice_configs: dict[str, VoiceConfig] = {}
        for name, cfg in story.characters.items():
            voice_raw = cfg.get("voice", {})
            if not isinstance(voice_raw, dict) or not voice_raw:
                continue

            ref_id    = voice_raw.get("ref_id", "")
            ref_audio = voice_raw.get("ref_audio", "")
            ref_text  = voice_raw.get("ref_text", "")

            # ref_id 优先：从声音库自动填充
            if ref_id and not ref_audio:
                mac_path, lib_text = _resolve_ref_id(ref_id)
                if mac_path:
                    ref_audio = mac_path
                    ref_text  = ref_text or lib_text   # 故事 yaml 写了优先用故事的
                else:
                    print(f"  [voice] ⚠ 角色 {name} 的 ref_id={ref_id} "
                          f"在声音库中找不到，将降级到 edge_tts")

            self._voice_configs[name] = VoiceConfig(
                engine    = voice_raw.get("engine", "edge_tts"),
                voice_id  = voice_raw.get("voice_id", default_voice),
                ref_audio = ref_audio,
                ref_text  = ref_text,
                language  = voice_raw.get("language", "zh"),
                ref_id    = ref_id,
            )

        # 启动日志
        if self._voice_configs:
            sovits_chars = [n for n, v in self._voice_configs.items()
                            if v.engine == "gpt_sovits"]
            edge_chars   = [n for n, v in self._voice_configs.items()
                            if v.engine == "edge_tts"]
            print(f"  [voice] GPT-SoVITS 角色: {sovits_chars or '无'}")
            print(f"  [voice] Edge TTS 角色:   {edge_chars or '无'}")
            if sovits_chars and not self.sovits_host:
                print(f"  [voice] ⚠ GPT-SoVITS 角色配置存在，但未设置 sovits_host，将降级")

    # ── 主接口 ─────────────────────────────────────────────────

    async def generate(self, text: str,
                       character: Optional[str] = None,
                       out_path: str = "") -> float:
        """
        生成一行台词的音频。
        参数：
          text:      台词文字
          character: 角色名（None = 旁白，走 edge_tts）
          out_path:  输出文件路径
        返回：音频时长（秒）
        """
        # v2.4.1 空文本防御:edge_tts / sovits 喂空串会生成废文件并抛错。
        # 正常情况下空文本不该到这(silent_beat/extend hold 在 producer 就被拦),
        # 这里是纵深防御:旧 storyboard、手工编辑 yaml 等漏过来也不崩。
        if not text or not text.strip():
            print(f"  [voice] ⚠ 空文本,跳过 TTS,返回 0 时长: {out_path}")
            return 0.0
        voice_cfg = self._voice_configs.get(character) if character else None

        # 路由决策 —— 同时决定要走哪种拼音修正
        if (voice_cfg
                and voice_cfg.engine == "gpt_sovits"
                and self.sovits_host
                and voice_cfg.ref_audio):
            text = fix_pronunciation(text, engine="gpt_sovits")
            return await self._generate_sovits(text, voice_cfg, out_path)
        else:
            voice_id = voice_cfg.voice_id if voice_cfg else self.default_voice
            text = fix_pronunciation(text, engine="edge_tts")
            return await self._generate_edge(text, voice_id, out_path)

    # ── Edge TTS ───────────────────────────────────────────────

    async def _generate_edge(self, text: str, voice_id: str,
                             out_path: str) -> float:
        """v2：加 503/网络错误退避重试（最多 3 次）"""
        try:
            import edge_tts
        except ImportError:
            raise ImportError("pip install edge-tts")

        max_attempts = 3
        last_err = None
        for attempt in range(1, max_attempts + 1):
            try:
                comm = edge_tts.Communicate(text, voice_id)
                await comm.save(out_path)
                # 简单合法性校验
                if Path(out_path).exists() and Path(out_path).stat().st_size > 200:
                    self._trim_silence(out_path)
                    duration = self._audio_duration(out_path)
                    if attempt > 1:
                        print(f"  [voice/edge] ✓ 第{attempt}次成功 "
                              f"{Path(out_path).name}")
                    print(f"  [voice/edge] {Path(out_path).name} "
                          f"({len(text)}字, {duration:.1f}s, {voice_id})")
                    return duration
                else:
                    raise IOError(f"输出文件无效或过小: {out_path}")
            except Exception as e:
                last_err = e
                err_msg = str(e)[:120]
                if attempt < max_attempts:
                    wait = 5 * attempt   # 退避：5s / 10s
                    print(f"  [voice/edge] ⚠ 第{attempt}/{max_attempts}次失败"
                          f" ({err_msg})，{wait}s 后重试...")
                    await asyncio.sleep(wait)
                else:
                    print(f"  [voice/edge] ❌ 全部 {max_attempts} 次失败: {err_msg}")

        # 全部重试失败：抛出最后的错误（让上层决定是否兜底）
        raise last_err if last_err else RuntimeError("Edge TTS 多次失败")

    # ── GPT-SoVITS（GET API，与你测试通的版本一致）──────────────

    async def _generate_sovits(self, text: str,
                               voice_cfg: VoiceConfig,
                               out_path: str) -> float:
        """
        GPT-SoVITS GET API 调用（与你测试脚本一致）。

        参考音频路径自动从 Mac 转为 Win10：
          refs/voices/library/ghost.wav → H:/voice_refs/library/ghost.wav
        """
        # Mac → Win10 路径转换
        win_ref_audio = _mac_to_win(voice_cfg.ref_audio)

        params = {
            "refer_wav_path":  win_ref_audio,
            "prompt_text":     voice_cfg.ref_text,
            "prompt_language": voice_cfg.language,
            "text":            text,
            "text_language":   voice_cfg.language,
        }

        try:
            # 用 requests 而不是 aiohttp（GET 返回流式 wav，requests 同步处理更稳）
            # 包到 run_in_executor 里以保持 async 接口
            loop = asyncio.get_event_loop()
            response = await loop.run_in_executor(
                None,
                lambda: requests.get(self.sovits_host,
                                     params=params, timeout=120)
            )

            if response.status_code != 200:
                print(f"  [voice/sovits] API {response.status_code}: "
                      f"{response.text[:200]}")
                print(f"  [voice/sovits] 降级到 edge_tts")
                return await self._generate_edge(
                    text, self.default_voice, out_path)

            # 写入 wav
            tmp_wav = out_path
            if out_path.endswith(".mp3"):
                tmp_wav = out_path.replace(".mp3", "_raw.wav")
            with open(tmp_wav, "wb") as f:
                f.write(response.content)

            # 转 mp3（可选）
            if out_path.endswith(".mp3"):
                subprocess.run([
                    "ffmpeg", "-y", "-i", tmp_wav,
                    "-codec:a", "libmp3lame", "-b:a", "128k",
                    out_path,
                ], capture_output=True)
                Path(tmp_wav).unlink(missing_ok=True)

            self._trim_silence(out_path)
            duration = self._audio_duration(out_path)
            print(f"  [voice/sovits] {Path(out_path).name} "
                  f"({len(text)}字, {duration:.1f}s, "
                  f"ref={Path(voice_cfg.ref_audio).stem})")
            return duration

        except Exception as e:
            print(f"  [voice/sovits] 失败: {e}")
            print(f"  [voice/sovits] 降级到 edge_tts")
            return await self._generate_edge(
                text, self.default_voice, out_path)

    # ── 工具方法 ───────────────────────────────────────────────

    @staticmethod
    def _trim_silence(path: str):
        trimmed = path + ".trimmed"
        try:
            subprocess.run([
                "ffmpeg", "-y", "-i", path,
                "-af", "silenceremove=stop_periods=-1:stop_duration=0.3:stop_threshold=-40dB",
                trimmed,
            ], check=True, capture_output=True)
            import os
            os.replace(trimmed, path)
        except Exception:
            Path(trimmed).unlink(missing_ok=True)

    @staticmethod
    def _audio_duration(path: str) -> float:
        try:
            r = subprocess.run(
                ["ffprobe", "-v", "quiet", "-print_format", "json",
                 "-show_format", path],
                capture_output=True, text=True, check=True)
            return float(json.loads(r.stdout)["format"]["duration"])
        except Exception:
            return 4.0


# ════════════════════════════════════════════════════════════════
# 独立测试入口
# ════════════════════════════════════════════════════════════════

def _cli_main():
    """命令行入口，独立测试 voice_engine"""
    import argparse
    from story import StoryConfig

    parser = argparse.ArgumentParser(
        description="独立测试 voice_engine",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例：
  # 列出故事中所有角色及其 voice 配置
  python -m core.voice_engine --story stories/xxx.yaml --char-list

  # 测试单个角色
  python -m core.voice_engine --story stories/xxx.yaml --char innkeeper_ghost \\
                              --text "客官请进，今夜风大"

  # 一键测试所有角色（每个生成一个测试音频，自动播放第一个）
  python -m core.voice_engine --story stories/xxx.yaml --all
""")
    parser.add_argument("--story",  required=True, help="故事 YAML 路径")
    parser.add_argument("--char",   default=None,  help="角色名（留空=旁白）")
    parser.add_argument("--text",   default="测试音频，欢迎来到鬼故事的世界。",
                        help="测试文本")
    parser.add_argument("--out",    default="/tmp/voice_test.mp3", help="输出路径")
    parser.add_argument("--all",       action="store_true", help="测试所有角色")
    parser.add_argument("--char-list", action="store_true", help="列出角色配置")
    parser.add_argument("--no-play",   action="store_true", help="不自动播放")
    parser.add_argument("--sovits",
                        default=SOVITS_URL,
                        help="GPT-SoVITS 服务地址")
    args = parser.parse_args()

    story  = StoryConfig(args.story)
    engine = VoiceEngine(story, sovits_host=args.sovits)

    # ── 列出角色 ──────────────────────────────────────────────
    if args.char_list:
        print(f"\n  故事《{story.title}》的角色 voice 配置：\n")
        for name, vcfg in engine._voice_configs.items():
            print(f"  {name}")
            print(f"    engine:    {vcfg.engine}")
            if vcfg.ref_id:
                print(f"    ref_id:    {vcfg.ref_id}")
            if vcfg.ref_audio:
                print(f"    ref_audio: {vcfg.ref_audio}")
                print(f"               (Win10: {_mac_to_win(vcfg.ref_audio)})")
            if vcfg.ref_text:
                print(f"    ref_text:  {vcfg.ref_text[:40]}")
            if vcfg.engine == "edge_tts":
                print(f"    voice_id:  {vcfg.voice_id}")
            print()
        return

    # ── 一键测全部 ────────────────────────────────────────────
    async def run_all():
        chars  = list(story.characters.keys()) + [None]  # +None = 旁白
        first_out = None
        for cname in chars:
            label    = cname or "narrator"
            out      = f"/tmp/voice_test_{label}.mp3"
            test_txt = f"我是{cname or '旁白'}，{args.text}"
            try:
                duration = await engine.generate(test_txt, cname, out)
                print(f"  → {out} ({duration:.1f}s)\n")
                first_out = first_out or out
            except Exception as e:
                print(f"  [测试] {label} 失败: {e}\n")
        if first_out and not args.no_play:
            try:
                subprocess.Popen(["open", first_out])
            except Exception:
                pass

    # ── 单条测试 ──────────────────────────────────────────────
    async def run_one():
        duration = await engine.generate(args.text, args.char, args.out)
        print(f"\n  → {args.out} ({duration:.1f}s)")
        if not args.no_play:
            try:
                subprocess.Popen(["open", args.out])
            except Exception:
                pass

    if args.all:
        asyncio.run(run_all())
    else:
        asyncio.run(run_one())


if __name__ == "__main__":
    _cli_main()
