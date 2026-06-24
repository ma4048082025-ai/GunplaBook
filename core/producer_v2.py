"""
producer_v2.py ── 视频生产器 v3.0（在 v2.2 基础上）
==========================================================================
v3.0 新增功能：

1. **BGM 分页混音**（替换 v2 的全程一首 BGM）
   - 从 motion_plan 读取每页的 bgm_mood
   - 同 mood 连续页用同一首 BGM；切 mood 时交叉淡化
   - 最终生成一条匹配视频时长的 BGM 长音轨，最后 overlay 到视频

2. **SRT 字幕生成**（硬字幕烧到画面）
   - narration 按句号/问号/感叹号切分，每句一行字幕
   - dialogue 每条独立一行字幕，前缀显示角色名（可关闭）
   - 字幕样式：白字 + 黑色描边 + 底部居中（抖音/小红书风）
   - 横屏底部 8% 处，竖屏底部 25% 处（避开人物）

3. **音频时间表精确记录**
   - _build_page_audio 同时返回每段音频的 [(start, end, text, speaker)]
   - 这是字幕和 BGM 切换的时间锚点

v2.2 保留功能：
  - scale + crop 抗抖 KB
  - 输入图自适应预放大
  - dialogue 多角色配音
  - clip fps + 分辨率归一化
  - _bench_clips.json 调试元数据

依赖：
  - FFmpeg ≥ 4.0（带 libass 字幕支持）
  - 字幕需要系统有中文字体（macOS 用 PingFang，Linux 用 Noto Sans CJK）
"""

import asyncio
import hashlib
import json
import random
import re
import subprocess
import tempfile

# v3.1：持久化缓存目录名（在 book_output/<story_id>/ 下）
PRODUCE_CACHE_DIR = "_cache"
from dataclasses import dataclass, field
from pathlib import Path
from datetime import datetime
from typing import Optional

# v2.7 音频工程: ambient + sfx 整片叠加
try:
    from core.audio.scene_audio_helper import build_ambient_and_sfx_for_episode
    from core.audio.ambient_continuity import SfxAbsoluteEvent
    _SCENE_AUDIO_AVAILABLE = True
except ImportError as _e:
    print(f"[producer] v2.7 scene_audio 模块不可用: {_e}")
    _SCENE_AUDIO_AVAILABLE = False

# ════════════════════════════════════════════════════════════════
# 平台配置
# ════════════════════════════════════════════════════════════════

@dataclass
class PlatformProfile:
    name:      str
    width:     int
    height:    int
    img_ratio: float
    fps:       int = 30
    # 字幕位置：底部到字幕中心的距离占视频高度的比例
    subtitle_bottom_ratio: float = 0.10
    # 字幕字号（绝对像素）
    subtitle_fontsize: int = 38


PROFILES = {
    "youtube":     PlatformProfile("youtube",     1920, 1080, 0.85,
                                    subtitle_bottom_ratio=0.10,
                                    subtitle_fontsize=44),
    "douyin":      PlatformProfile("douyin",      1080, 1920, 0.72,
                                    subtitle_bottom_ratio=0.22,
                                    subtitle_fontsize=52),
    "xiaohongshu": PlatformProfile("xiaohongshu", 1080, 1440, 0.75,
                                    subtitle_bottom_ratio=0.18,
                                    subtitle_fontsize=48),
}


# ── KB 抗抖参数 ──────────────────────────────────────────
KB_ZOOM_MAX     = 1.15
KB_PAN_RANGE    = 0.50
KB_INPUT_OVERSAMPLE = 1.20
KB_PREP_CACHE_DIR = "_kb_prep_cache"


# ── 镜头语言 → 淡入淡出映射（v3.1 新增）────────────────
# transition_in 字段由 long_writer v2.1 输出，描述这个镜头如何"进入"
# 不同的镜头语言对应不同的淡入淡出节奏，让画面切换有叙事意义
# (fade_in_duration, fade_out_duration) 单位：秒
TRANSITION_FADE_MAP = {
    "fade_in":       (0.50, 0.30),   # 章节开场，长淡入
    "match_cut":     (0.00, 0.30),   # 相似剪接，无淡入（无缝衔接）
    "push_in":       (0.15, 0.30),   # 推进感，短淡入
    "pull_out":      (0.30, 0.30),   # 拉远揭示
    "eyeline_match": (0.00, 0.30),   # 视线引导，无淡入（保持连贯）
    "whip_pan":      (0.05, 0.05),   # 甩镜，极短，紧张感
    "j_cut":         (0.00, 0.30),   # 声音先入
    "hold":          (0.00, 0.00),   # 延续上一镜头（阶段2会改为不生 clip）
}
DEFAULT_FADE = (0.30, 0.30)   # 旧 yaml 无 transition_in 时的默认值（与 v3.0 一致）


# ── BGM 配置 ─────────────────────────────────────────────
BGM_ROOT = Path("refs/bgm")
BGM_VALID_MOODS  = ("tension", "climax", "melancholy")
BGM_EXTENSIONS   = (".mp3", ".wav", ".m4a", ".ogg")
BGM_VOLUME       = 0.18    # BGM 相对人声音量
BGM_FADE         = 1.5     # 单页 BGM 淡入淡出
BGM_CROSS_FADE   = 2.0     # 跨 mood 交叉淡化


# ── 三轨音频配置 (v2.5) ────────────────────────────────────
# 三轨化：narration / dialogue / bgm 各自独立时间轴。
# BGM(轨3) 早就独立(在整片阶段 _overlay_bgm_track 叠加),
# 本配置只控制 narration(轨1) 和 dialogue(轨2) 的关系。
#
# 默认 False = 单轨串行(narration→dialogue,跟 v2.4.1 行为一致,零回归)
# 设 True  = 双轨独立时间轴 + amix 混音,允许 narration 和 dialogue 时间重叠
ENABLE_THREE_TRACK_AUDIO = True

# 三轨化开启后,dialogue 相对 narration 的时序策略:
#   "sequential"     dialogue 排在 narration 后面(只是用 amix 替代 concat,听感几乎不变)
#   "overlap_tail"   dialogue 提前 DIALOGUE_OVERLAP_SEC 秒,
#                    跟 narration 的尾部交叠(默认,听感更电影化)
#   "parallel"       dialogue 跟 narration 同时开始(全重叠,适合"心声"式独白,慎用)
DIALOGUE_TIMING_STRATEGY = "overlap_tail"

# overlap_tail 策略下,dialogue 早于 narration 结束多少秒开始
DIALOGUE_OVERLAP_SEC = 0.5

# amix 时各轨权重(narration 略响于 dialogue,听感更突出叙事)
AMIX_WEIGHT_NARRATION = 1.0
AMIX_WEIGHT_DIALOGUE  = 1.0


# ── 字幕配置 ─────────────────────────────────────────────
# 跨平台字体名（force_style 里 FontName 必须是系统已安装的字体）
DEFAULT_SUBTITLE_FONT = "Heiti SC"   # macOS 默认中文字体
# 字幕开关，可在 ProducerV2 构造时关闭
SUBTITLES_ENABLED_DEFAULT = True
# 是否在 dialogue 字幕前显示角色名（"鬼女：客官请进"）
SUBTITLE_SHOW_SPEAKER     = False
# 单行最大字数（超过自动断行）
SUBTITLE_MAX_CHARS_PER_LINE = 18


# ════════════════════════════════════════════════════════════════
# 字幕段数据结构
# ════════════════════════════════════════════════════════════════

@dataclass
class SubtitleSegment:
    """一段字幕：在最终视频中的绝对时间 + 文字"""
    start:   float          # 秒（相对最终视频）
    end:     float
    text:    str
    speaker: str = ""       # "narrator" / "innkeeper_ghost" 等


@dataclass
class PageAudioInfo:
    """一页音频的完整信息"""
    audio_path: str
    duration:   float
    # 每段台词在本页内的相对时间（秒，从 0 起）
    segments:   list = field(default_factory=list)  # [(rel_start, rel_end, text, speaker), ...]
    # v2.4.1: 静默镜头标记。True 表示本页无人声,duration 来自 intended_duration_sec。
    is_silent:  bool = False
    # v2.5 预留: 三轨化后,各轨独立音频路径。
    #   {"narration": path, "dialogue": path, "bgm": path}
    # 当前单轨阶段为空 dict;三轨化时由 _build_page_audio_tracks 填充。
    tracks:     dict = field(default_factory=dict)


# ════════════════════════════════════════════════════════════════
# BGM 工具函数
# ════════════════════════════════════════════════════════════════

def _list_bgm_files(mood: str) -> list:
    mood_dir = BGM_ROOT / mood
    if not mood_dir.exists():
        return []
    files = []
    for ext in BGM_EXTENSIONS:
        files.extend(mood_dir.glob(f"*{ext}"))
    return sorted(files)


def _pick_bgm(mood: str, seed: Optional[int] = None) -> Optional[Path]:
    """选 BGM；找不到 mood 时降级到 tension"""
    if mood not in BGM_VALID_MOODS:
        mood = "tension"

    files = _list_bgm_files(mood)
    if not files and mood != "tension":
        files = _list_bgm_files("tension")

    if not files:
        return None

    rng = random.Random(seed) if seed is not None else random
    return rng.choice(files)


# ════════════════════════════════════════════════════════════════
# 字幕工具函数
# ════════════════════════════════════════════════════════════════

def _split_narration(text: str) -> list:
    """
    把 narration 按中文/英文句号、问号、感叹号切分。
    保留标点符号在段尾。
    """
    if not text:
        return []
    # 用 re 切分但保留分隔符
    parts = re.split(r'([。！？.!?])', text)
    sentences = []
    cur = ""
    for p in parts:
        cur += p
        if p in "。！？.!?":
            s = cur.strip()
            if s:
                sentences.append(s)
            cur = ""
    if cur.strip():
        sentences.append(cur.strip())
    return sentences


def _wrap_subtitle_line(text: str, max_chars: int = SUBTITLE_MAX_CHARS_PER_LINE) -> str:
    """
    超长字幕按字符数硬断行（中文一字 = 一字符）。
    SRT 用 \n 表示换行。
    """
    if len(text) <= max_chars:
        return text
    lines = []
    cur = ""
    for ch in text:
        cur += ch
        if len(cur) >= max_chars:
            lines.append(cur)
            cur = ""
    if cur:
        lines.append(cur)
    return "\n".join(lines)


def _seconds_to_srt_time(seconds: float) -> str:
    """0.5 → '00:00:00,500'"""
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    ms = int((seconds - int(seconds)) * 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def _build_srt(segments: list) -> str:
    """
    把 [SubtitleSegment, ...] 渲染为 SRT 文件内容。
    """
    lines = []
    for i, seg in enumerate(segments, 1):
        text = _wrap_subtitle_line(seg.text)
        if SUBTITLE_SHOW_SPEAKER and seg.speaker and seg.speaker != "narrator":
            text = f"{seg.speaker}：{text}"
        lines.append(str(i))
        lines.append(f"{_seconds_to_srt_time(seg.start)} --> "
                     f"{_seconds_to_srt_time(seg.end)}")
        lines.append(text)
        lines.append("")
    return "\n".join(lines)


def _ffmpeg_subtitle_filter(srt_path: str, profile: PlatformProfile,
                             font_name: str = DEFAULT_SUBTITLE_FONT) -> str:
    """
    构造 ffmpeg subtitles 滤镜字符串，含 force_style。
    样式：白字 + 黑色描边 + 底部居中。
    """
    # MarginV：字幕到底部的边距（像素）
    margin_v = int(profile.height * profile.subtitle_bottom_ratio)
    fontsize = profile.subtitle_fontsize

    # ASS 字幕样式参数
    # PrimaryColour=&H00FFFFFF (white)
    # OutlineColour=&H00000000 (black outline)
    # BorderStyle=1 (outline + shadow)
    # Outline=3 (描边粗细)
    # Shadow=1 (阴影)
    # Alignment=2 (底部居中)
    style = (
        f"PlayResX={profile.width},"
        f"PlayResY={profile.height},"
        f"ScaledBorderAndShadow=yes,"
        f"FontName={font_name},"
        f"FontSize={fontsize},"
        f"PrimaryColour=&H00FFFFFF,"
        f"OutlineColour=&H00000000,"
        f"BorderStyle=1,"
        f"Outline=3,"
        f"Shadow=1,"
        f"Alignment=2,"
        f"MarginV={margin_v},"
        f"Bold=1"
    )
    # 注意：subtitles 滤镜的 srt 路径里冒号要转义（Windows 下的 C:）
    safe_path = srt_path.replace(":", "\\:").replace("'", "\\'")
    # 关键：original_size 强制 libass 使用视频真实分辨率作为参考坐标系
    return (f"subtitles='{safe_path}'"
            f":original_size={profile.width}x{profile.height}"
            f":force_style='{style}'")


# ════════════════════════════════════════════════════════════════
# ProducerV2 主类
# ════════════════════════════════════════════════════════════════

class ProducerV2:
    """从 APPROVED 资产 + motion_plan 生成最终视频"""

    def __init__(self, story, theme,
                 sovits_host: str = "",
                 bgm_path: Optional[str] = None,
                 enable_subtitles: bool = SUBTITLES_ENABLED_DEFAULT,
                 subtitle_font: str = DEFAULT_SUBTITLE_FONT):
        self.story    = story
        self.theme    = theme
        self.bgm_path = bgm_path   # 兼容旧版（全程单首 BGM 兜底）
        self.enable_subtitles = enable_subtitles
        self.subtitle_font    = subtitle_font

        from voice_engine import VoiceEngine
        prod = theme.production
        self.voice = VoiceEngine(
            story,
            sovits_host=sovits_host,
            default_voice=prod.narration_voice,
        )

    # ══════════════════════════════════════════════════════════
    # 旁白 / 对白音频（v3：返回 PageAudioInfo 含每段时间）
    # ══════════════════════════════════════════════════════════

    def _audio_cache_key(self, page_cfg: dict, pn: int) -> str:
        """
        计算页面音频缓存的 hash key。
        包含：narration 文字 + 所有 dialogue + 角色 voice 配置 hash。
        voice 配置改了（换声音）会自动失效缓存。
        """
        narration = page_cfg.get("narration",
                                 page_cfg.get("title", f"第{pn}页"))
        dialogue  = page_cfg.get("dialogue", []) or []
        # 收集本页用到的角色 voice 配置（影响合成结果）
        speakers_used = set(["narrator"])
        for dl in dialogue:
            sp = dl.get("speaker") or "narrator"
            speakers_used.add(sp)
        voice_sig = []
        for sp in sorted(speakers_used):
            cfg = self.voice._voice_configs.get(sp)
            if cfg:
                voice_sig.append(
                    f"{sp}:{cfg.engine}:{cfg.voice_id}:{cfg.ref_id}:{cfg.ref_audio}")
            else:
                voice_sig.append(f"{sp}:default")
        sig = "|".join([
            f"narr={narration}",
            *[f"d{i}={dl.get('speaker','')}:{dl.get('text','')}"
              for i, dl in enumerate(dialogue)],
            *voice_sig,
            # v2.5: 三轨化开关进 cache key。
            # 开/关切换时,旧缓存(单轨 concat 的 mp3)和新缓存(双轨 amix 的 mp3)
            # 不能互相污染 —— 同样的 narration+dialogue 在两种模式下混出来不同。
            # 同时把时序策略和 overlap 长度也并进去,改这两个参数能自动失效缓存。
            f"3trk={int(ENABLE_THREE_TRACK_AUDIO)}",
            f"strat={DIALOGUE_TIMING_STRATEGY if ENABLE_THREE_TRACK_AUDIO else 'n/a'}",
            f"olap={DIALOGUE_OVERLAP_SEC if ENABLE_THREE_TRACK_AUDIO else 0}",
        ])
        return hashlib.md5(sig.encode("utf-8")).hexdigest()[:12]

    def _try_load_audio_cache(self, cache_dir: Path, pn: int,
                              cache_key: str) -> "Optional[PageAudioInfo]":
        """尝试从缓存读取已生成的页面音频"""
        meta_path = cache_dir / f"audio_{pn:02d}_{cache_key}.meta.json"
        audio_path = cache_dir / f"audio_{pn:02d}_{cache_key}.mp3"
        if not (meta_path.exists() and audio_path.exists()):
            return None
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            # 反序列化 segments：[(start, end, text, speaker), ...]
            segments = [tuple(s) for s in meta["segments"]]
            return PageAudioInfo(
                audio_path=str(audio_path),
                duration=meta["duration"],
                segments=segments,
            )
        except Exception as e:
            print(f"  [cache] 音频缓存读取失败 p{pn}（将重生）: {e}")
            return None

    def _save_audio_cache(self, cache_dir: Path, pn: int, cache_key: str,
                          info: "PageAudioInfo"):
        """保存音频元数据 + 把音频文件复制到缓存路径"""
        meta_path = cache_dir / f"audio_{pn:02d}_{cache_key}.meta.json"
        cached_audio = cache_dir / f"audio_{pn:02d}_{cache_key}.mp3"
        # 音频文件可能本身就在 cache_dir 下（tmp_dir==cache_dir 时），那就直接重命名
        # 也可能不是，安全起见用 copy
        try:
            if Path(info.audio_path).resolve() != cached_audio.resolve():
                import shutil
                shutil.copy2(info.audio_path, cached_audio)
                # 让 info 指向缓存版本，下游用缓存路径
                info.audio_path = str(cached_audio)
        except Exception as e:
            print(f"  [cache] 音频文件存入缓存失败 p{pn}: {e}")
            return
        meta = {
            "duration": info.duration,
            "segments": [list(s) for s in info.segments],
        }
        meta_path.write_text(
            json.dumps(meta, ensure_ascii=False, indent=2),
            encoding="utf-8")

    async def _build_page_audio(self, page_cfg: dict,
                                tmp_dir: Path,
                                pn: int) -> PageAudioInfo:
        # ── v2.4.1: 静默镜头 —— 不调 TTS,时长由 intended_duration_sec 决定 ──
        # 契约见 AUDIO_FIELD_CONTRACT.md。silent_beat 镜头有独立画面、独立时长、
        # 有 BGM,但无人声。这里直接返回一个无音频的 PageAudioInfo。
        if page_cfg.get("silent_beat"):
            try:
                sil_dur = float(page_cfg.get("intended_duration_sec", 0.0) or 0.0)
            except (TypeError, ValueError):
                sil_dur = 0.0
            sil_dur = max(sil_dur, 2.0)   # 兜底下限
            print(f"  [silent-beat] p{pn} 静默镜头,无人声,画面停留 {sil_dur:.1f}s")
            return PageAudioInfo(
                audio_path="",          # 无音频文件;下游需判空
                duration=sil_dur,
                segments=[],
                is_silent=True,
            )

        narration = page_cfg.get("narration",
                                 page_cfg.get("title", f"第{pn}页"))
        dialogue  = page_cfg.get("dialogue", []) or []

        # ── v2.5.1 防御: 非法空状态自动救助 ─────────────────────
        # 正常情况下 coordinator 的 _purge_empty_shots 已经把这种镜头删了,
        # 但万一漏网(老 yaml / 跳过 coordinator / 数据迁移),
        # producer 不能崩,也不能把空字符串喂给 TTS。
        # 救助策略: 当 silent_beat 处理,画面停 2 秒,无人声,有 BGM。
        narr_stripped = (narration or "").strip()
        if (not narr_stripped
            and not dialogue
            and not page_cfg.get("silent_beat")
            and not page_cfg.get("_hold")):
            print(f"  ⚠ [defensive] p{pn} 非法空状态(无 narration/dialogue/hold/silent_beat) "
                  f"→ 兜底当 silent_beat,画面停 2.0s。建议上游 coordinator 清理。")
            return PageAudioInfo(
                audio_path="",
                duration=2.0,
                segments=[],
                is_silent=True,
            )

        # ── 缓存命中检查 ─────────────────────────────────────────
        cache_key = self._audio_cache_key(page_cfg, pn)
        cached = self._try_load_audio_cache(tmp_dir, pn, cache_key)
        if cached:
            print(f"  [cache] ✓ 音频缓存命中 p{pn} ({cached.duration:.1f}s, "
                  f"{len(cached.segments)}段)")
            return cached

        # ── v2.5 三轨化分流 ─────────────────────────────────────
        # 三轨化只在"既有 narration 又有 dialogue"的页生效 ——
        # 纯 narration 或纯 dialogue 只有一条音轨,没什么可"三轨"的,
        # 直接走原单轨逻辑,行为完全不变。
        if (ENABLE_THREE_TRACK_AUDIO
            and narration and narration.strip()
            and dialogue):
            return await self._build_page_audio_three_track(
                page_cfg, tmp_dir, pn, cache_key)

        # 没有对白：仅 narration
        if not dialogue:
            audio_path = str(tmp_dir / f"audio_{pn:02d}.mp3")
            duration = await self.voice.generate(
                narration, character="narrator", out_path=audio_path)
            # narration 按句切分作字幕段
            sentences = _split_narration(narration)
            segments = []
            if sentences:
                # 按字符数比例分配时间
                total_chars = sum(len(s) for s in sentences)
                t = 0.0
                for s in sentences:
                    span = duration * (len(s) / total_chars) if total_chars else duration
                    segments.append((t, t + span, s, "narrator"))
                    t += span
            else:
                segments.append((0.0, duration, narration, "narrator"))
            info = PageAudioInfo(
                audio_path=audio_path,
                duration=duration,
                segments=segments,
            )
            self._save_audio_cache(tmp_dir, pn, cache_key, info)
            return info

        # 有对白：narration（首段，可选）+ 每条 dialogue
        all_segments = []
        all_paths    = []
        cumulative   = 0.0
        gap = 0.4

        if narration:
            seg_path = str(tmp_dir / f"audio_{pn:02d}_00_narr.mp3")
            d = await self.voice.generate(
                narration, character="narrator", out_path=seg_path)
            # narration 内部再按句切（提高字幕可读性）
            sentences = _split_narration(narration)
            if len(sentences) > 1:
                total_chars = sum(len(s) for s in sentences)
                t = cumulative
                for s in sentences:
                    span = d * (len(s) / total_chars) if total_chars else d
                    all_segments.append((t, t + span, s, "narrator"))
                    t += span
            else:
                all_segments.append((cumulative, cumulative + d, narration, "narrator"))
            all_paths.append(seg_path)
            cumulative += d + gap

        for idx, dl in enumerate(dialogue, start=1):
            speaker = dl.get("speaker") or "narrator"
            text    = dl.get("text", "")
            if not text:
                continue
            seg_path = str(tmp_dir / f"audio_{pn:02d}_{idx:02d}_{speaker}.mp3")
            d = await self.voice.generate(
                text, character=speaker, out_path=seg_path)
            all_segments.append((cumulative, cumulative + d, text, speaker))
            all_paths.append(seg_path)
            cumulative += d + gap

        # cumulative 多算了最后一个 gap，扣除
        if all_paths:
            cumulative -= gap

        merged_path = str(tmp_dir / f"audio_{pn:02d}.mp3")
        self._concat_audio_with_gaps(all_paths, merged_path, gap=gap)

        info = PageAudioInfo(
            audio_path=merged_path,
            duration=cumulative,
            segments=all_segments,
        )
        self._save_audio_cache(tmp_dir, pn, cache_key, info)
        return info

    # ════════════════════════════════════════════════════════════
    # v2.5 音频三轨化
    # ════════════════════════════════════════════════════════════
    # 当前 _build_page_audio 在【单轨模式】下把 narration + dialogue
    # 串行拼成单条 mp3,BGM 整片另铺 —— 实为 "1.5 轨"。
    #
    # 三轨化目标: 三轨独立、各有时间轴、允许重叠:
    #   轨1 narration  旁白(说书人)   全片骨架
    #   轨2 dialogue   角色专属配音
    #   轨3 bgm        跨镜头平铺,按 bgm_mood 分段
    #
    # 现状(v2.5):
    #   - 轨3 BGM 一直独立 —— _build_bgm_track + _overlay_bgm_track,
    #     在整片合成阶段叠加,不进 PageAudioInfo.audio_path。
    #   - 轨1/轨2 由 ENABLE_THREE_TRACK_AUDIO 开关控制:
    #       False(默认) → 串行 concat,行为完全等同 v2.4.1(零回归)
    #       True        → 各自独立时间轴 → ffmpeg amix 混音 → 单 mp3
    #     混音后的最终单 mp3 仍写入 audio_path,下游(KB clip / video clip)
    #     不需要任何改动。同时 tracks 字典记录两条原始轨道的路径,
    #     供字幕渲染、调试、未来扩展(如导出 dialogue-only 版本)使用。
    #   - hold 链合并(_concat_audio_infos)按 audio_path 串接,不区分轨道
    #     —— 因为合并后的"前镜+后镜"画面是一张图,人声继续无缝叠,
    #     轨道边界已经在 amix 时融化。这一行为在两种模式下都正确。
    #
    # 契约 / coordinator / to_pipeline 完全不需要动 —— 纯 producer 内部升级。
    async def _build_page_audio_tracks(self, page_cfg: dict,
                                       tmp_dir: Path,
                                       pn: int) -> PageAudioInfo:
        """
        三轨化对外稳定入口。

        当前 v2.5: 直接委托给 _build_page_audio,后者按开关分流到
        单轨或双轨(narration+dialogue,BGM 独立)实现。
        外部调用方应优先使用本方法,以兼容未来三轨重构。
        """
        return await self._build_page_audio(page_cfg, tmp_dir, pn)

    async def _build_page_audio_three_track(self, page_cfg: dict,
                                            tmp_dir: Path,
                                            pn: int,
                                            cache_key: str) -> PageAudioInfo:
        """
        v2.5: narration / dialogue 双轨独立生成 + ffmpeg amix 混音。

        仅在 ENABLE_THREE_TRACK_AUDIO=True 且页面【既有 narration 又有 dialogue】
        时由 _build_page_audio 路由进来。其他情形仍走单轨原逻辑。

        流程:
          1. 轨1: 整段 narration 一次 TTS → narr.mp3
          2. 轨2: 每条 dialogue 一次 TTS → dlg_0X.mp3,串接成 dlg.mp3
                  (轨2 内部仍是顺序的,因为多个角色串台,不该同时讲话)
          3. 按 DIALOGUE_TIMING_STRATEGY 决定 dialogue 整体相对 narration
             的起始时间偏移
          4. ffmpeg amix(adelay + amix)把两轨混成一条 mp3
          5. PageAudioInfo.tracks = {"narration": ..., "dialogue": ...}
             PageAudioInfo.audio_path = 混音 mp3(给下游 KB clip 用)
             PageAudioInfo.segments = 按混音后的实际时间轴重算的字幕段

        BGM 不在这里管 —— 它由 _build_bgm_track 在整片阶段独立铺。
        """
        narration = page_cfg.get("narration", "")
        dialogue  = page_cfg.get("dialogue", []) or []

        # ── 轨1: narration ────────────────────────────────────
        narr_path = str(tmp_dir / f"audio_{pn:02d}_track1_narr.mp3")
        narr_dur = await self.voice.generate(
            narration, character="narrator", out_path=narr_path)
        # narration 内部按句切字幕段
        narr_sentences = _split_narration(narration)
        narr_segs_rel = []  # [(start, end, text, speaker)] 相对轨1 起点
        if len(narr_sentences) > 1:
            total_chars = sum(len(s) for s in narr_sentences)
            t = 0.0
            for s in narr_sentences:
                span = narr_dur * (len(s) / total_chars) if total_chars else narr_dur
                narr_segs_rel.append((t, t + span, s, "narrator"))
                t += span
        else:
            narr_segs_rel.append((0.0, narr_dur, narration, "narrator"))

        # ── 轨2: dialogue ─────────────────────────────────────
        # 多条 dialogue 内部仍串行(不同角色不该同时讲话),只是整体相对
        # narration 的偏移由策略决定。
        dlg_sub_paths = []
        dlg_segs_rel  = []  # [(start, end, text, speaker)] 相对轨2 起点
        dlg_cum = 0.0
        inner_gap = 0.4  # 同 v2.4.1 dialogue 之间的间隔
        for idx, dl in enumerate(dialogue, start=1):
            speaker = dl.get("speaker") or "narrator"
            text    = dl.get("text", "")
            if not text:
                continue
            seg_path = str(tmp_dir
                           / f"audio_{pn:02d}_track2_{idx:02d}_{speaker}.mp3")
            d = await self.voice.generate(
                text, character=speaker, out_path=seg_path)
            dlg_segs_rel.append((dlg_cum, dlg_cum + d, text, speaker))
            dlg_sub_paths.append(seg_path)
            dlg_cum += d + inner_gap
        if dlg_sub_paths:
            dlg_cum -= inner_gap  # 多算了最后一个尾隙

        if not dlg_sub_paths:
            # 防御: dialogue 全空(理论上 _build_page_audio 已挡,这里兜底)
            # 此时三轨化没意义,把 narration 当结果返回
            info = PageAudioInfo(
                audio_path=narr_path,
                duration=narr_dur,
                segments=narr_segs_rel,
                tracks={"narration": narr_path, "dialogue": ""},
            )
            self._save_audio_cache(tmp_dir, pn, cache_key, info)
            return info

        # 把 dialogue 多条子文件 concat 成轨2 完整 mp3
        dlg_path = str(tmp_dir / f"audio_{pn:02d}_track2_dlg.mp3")
        if len(dlg_sub_paths) == 1:
            # 单条 dialogue: 直接复制重编码,保持文件命名一致
            subprocess.run(
                ["ffmpeg", "-y", "-i", dlg_sub_paths[0],
                 "-c:a", "libmp3lame", "-b:a", "128k", dlg_path],
                check=True, capture_output=True)
        else:
            self._concat_audio_with_gaps(
                dlg_sub_paths, dlg_path, gap=inner_gap)
        dlg_total = dlg_cum

        # ── 时序策略: dialogue 相对 narration 的起始偏移 ────
        strategy = DIALOGUE_TIMING_STRATEGY
        if strategy == "parallel":
            dlg_offset = 0.0
        elif strategy == "overlap_tail":
            # dialogue 提前 DIALOGUE_OVERLAP_SEC 秒开始,跟 narration 尾部交叠
            dlg_offset = max(0.0, narr_dur - DIALOGUE_OVERLAP_SEC)
        else:  # "sequential" 或未知值
            # narration 完整播完后再开始 dialogue(听感等同 v2.4.1,但用 amix 而非 concat)
            dlg_offset = narr_dur + 0.4   # 0.4 = v2.4.1 narr↔dlg 的 gap

        # 总时长 = max(轨1 结束, 轨2 结束)
        total_dur = max(narr_dur, dlg_offset + dlg_total)

        # ── 轨1+轨2 amix 混音 ──────────────────────────────────
        merged_path = str(tmp_dir / f"audio_{pn:02d}.mp3")
        self._amix_tracks(
            tracks=[
                {"path": narr_path, "delay_sec": 0.0,
                 "weight": AMIX_WEIGHT_NARRATION},
                {"path": dlg_path,  "delay_sec": dlg_offset,
                 "weight": AMIX_WEIGHT_DIALOGUE},
            ],
            out_path=merged_path,
            total_dur=total_dur,
        )

        # ── 字幕段: 按混音后的绝对时间重算 ─────────────────────
        # 轨1 字幕段时间不变(从 0 起);轨2 字幕段时间加上 dlg_offset
        all_segments = list(narr_segs_rel)
        for s, e, text, speaker in dlg_segs_rel:
            all_segments.append(
                (s + dlg_offset, e + dlg_offset, text, speaker))
        # 按起始时间排序,便于字幕渲染按时间扫
        all_segments.sort(key=lambda x: x[0])

        print(f"  [3-track] p{pn} 轨1({narr_dur:.1f}s) + "
              f"轨2({dlg_total:.1f}s, offset={dlg_offset:.1f}s, "
              f"strategy={strategy}) → mix({total_dur:.1f}s)")

        info = PageAudioInfo(
            audio_path=merged_path,
            duration=total_dur,
            segments=all_segments,
            tracks={"narration": narr_path, "dialogue": dlg_path},
        )
        self._save_audio_cache(tmp_dir, pn, cache_key, info)
        return info

    @staticmethod
    def _amix_tracks(tracks: list, out_path: str, total_dur: float):
        """
        把多条音轨按各自起始延迟混成一条 mp3。
        tracks: [{"path": str, "delay_sec": float, "weight": float}, ...]

        实现:
          1. 每条轨先 adelay 到自己的起始时间
          2. amix inputs=N:weights=...:duration=longest 混音
          3. 最后 atrim 到 total_dur(防止 amix 的尾部静默无限延长)
        """
        if not tracks:
            return
        # 过滤掉文件不存在的轨(防御)
        valid = [t for t in tracks if t.get("path") and Path(t["path"]).exists()]
        if not valid:
            return
        if len(valid) == 1:
            # 单轨直接复制
            t = valid[0]
            delay_ms = int(t["delay_sec"] * 1000)
            af = []
            if delay_ms > 0:
                af.append(f"adelay={delay_ms}|{delay_ms}")
            af.append(f"atrim=0:{total_dur:.3f}")
            af_str = ",".join(af) if af else "anull"
            subprocess.run([
                "ffmpeg", "-y", "-i", t["path"],
                "-af", af_str,
                "-c:a", "libmp3lame", "-b:a", "192k",
                out_path,
            ], check=True, capture_output=True)
            return

        # 多轨 amix:
        # filter_complex:
        #   [0:a]adelay=A|A[a0]; [1:a]adelay=B|B[a1]; ...;
        #   [a0][a1]...amix=inputs=N:weights='w0 w1':duration=longest[mix];
        #   [mix]atrim=0:T[out]
        input_args = []
        filter_parts = []
        weight_strs = []
        amix_labels = []
        for i, t in enumerate(valid):
            input_args.extend(["-i", t["path"]])
            delay_ms = int(t.get("delay_sec", 0.0) * 1000)
            if delay_ms > 0:
                filter_parts.append(
                    f"[{i}:a]adelay={delay_ms}|{delay_ms}[a{i}]")
            else:
                # 即使无 delay,也走一遍 anull 让 label 统一
                filter_parts.append(f"[{i}:a]anull[a{i}]")
            amix_labels.append(f"[a{i}]")
            weight_strs.append(f"{t.get('weight', 1.0)}")
        amix_part = (
            f"{''.join(amix_labels)}"
            f"amix=inputs={len(valid)}:weights='{' '.join(weight_strs)}'"
            f":duration=longest:normalize=0[mix]"
        )
        atrim_part = f"[mix]atrim=0:{total_dur:.3f}[out]"
        filter_complex = ";".join([*filter_parts, amix_part, atrim_part])

        cmd = [
            "ffmpeg", "-y",
            *input_args,
            "-filter_complex", filter_complex,
            "-map", "[out]",
            "-c:a", "libmp3lame", "-b:a", "192k",
            out_path,
        ]
        try:
            subprocess.run(cmd, check=True, capture_output=True)
        except subprocess.CalledProcessError as ex:
            err_msg = ex.stderr.decode(errors="replace")[:500] if ex.stderr else ""
            print(f"  [amix] ffmpeg 失败: {err_msg}")
            print(f"  [amix] filter_complex: {filter_complex}")
            raise

    # ── v2.3.2 hold 镜头辅助方法 ──────────────────────────────────
    def _concat_audio_infos(self, audio_infos: list, tmp_dir,
                              page_num: int) -> tuple:
        """
        把多个 PageAudio 拼成一份，返回 (path, total_duration, merged_segments)。
        merged_segments 时间已按累加偏移调整。

        v2.5.1 字幕同步修复:
          - 旧版 cum_offset 累加用 (audio_duration + 0.15) 不论 audio 是否存在,
            导致 hold 链里夹空音频时,字幕段时间偏移多算了 N * 0.15 秒。
          - 新版:cum_offset 只在【实际拼入 mp3 的页】之间累加 gap;
                  total_dur 用 ffprobe 实测拼出来的 mp3 实际时长,
                  不再由公式推算。
        """
        from pathlib import Path as _P
        if len(audio_infos) == 1:
            ai = audio_infos[0]
            return ai.audio_path, ai.duration, list(ai.segments)

        out = _P(tmp_dir) / f"page{page_num:03d}_audio_extended.wav"
        # v2.4.1: silent 镜头 / 空音频不参与 concat(空路径会让 ffmpeg 报错),
        # 但其 duration 仍要计入总时长(画面要停那么久)。
        paths = [a.audio_path for a in audio_infos
                 if a.audio_path and _P(a.audio_path).exists()]
        if not paths:
            # 整条链都是静默 —— 无音频,返回空路径 + 累计时长(用各 audio 的 duration 加起来)
            total = sum(a.duration for a in audio_infos)
            return "", total, []

        # 实际拼接
        gap_between_real = 0.15
        self._concat_audio_with_gaps(paths, str(out), gap=gap_between_real)

        # ── v2.5.1: 字幕段时间偏移修正 ──
        # 关键洞察:字幕时间轴必须跟"实际 mp3 文件里音频的位置"对齐,
        # 不是跟"逻辑上想象的音频序列"对齐。
        # 实际 mp3 = paths 按顺序 concat 出来的,中间有 N-1 个 gap。
        # 字幕段应该挂在【对应 audio 在实际 mp3 里的起点】之后。
        merged_segments = []
        real_cum_offset = 0.0   # 在【实际拼出的 mp3】里的累计位置
        real_audio_idx = 0      # 实际拼入的 audio 计数

        for ai in audio_infos:
            ai_has_real_audio = (ai.audio_path
                                 and _P(ai.audio_path).exists())
            if ai_has_real_audio:
                # 这个 ai 的字幕段挂在 real_cum_offset 后
                for rel_s, rel_e, text, speaker in ai.segments:
                    merged_segments.append(
                        (real_cum_offset + rel_s,
                         real_cum_offset + rel_e,
                         text, speaker))
                # 累加这一段的实际时长
                real_cum_offset += ai.duration
                real_audio_idx += 1
                # 如果后面还有要拼的 audio,加 gap
                # (real_audio_idx 比有效 paths 总数小才加)
                if real_audio_idx < len(paths):
                    real_cum_offset += gap_between_real
            else:
                # 空音频镜头:不在拼出来的 mp3 里占任何位置 → 字幕段也不挂任何位置
                # 它本来就不该有字幕段(silent_beat 或空 narration)
                # 这里不动 real_cum_offset
                pass

        # ── total_dur:用 ffprobe 实测,不再公式推算 ──
        # 旧版 total_dur = sum(a.duration) + 0.15 * (len(audio_infos) - 1)
        # 在 hold 链夹空 audio 时会多算 (空镜数 * 0.15) 秒,导致 clip 时长虚标 → 字幕错位
        try:
            real_total_dur = self._get_video_duration(str(out))  # 这函数同时能测 mp3
            if real_total_dur <= 0:
                raise ValueError("ffprobe returned zero")
        except Exception:
            # 兜底:用 paths 总长 + 真实 gap 数
            real_total_dur = (
                sum(a.duration for a in audio_infos if a.audio_path
                    and _P(a.audio_path).exists())
                + gap_between_real * max(0, len(paths) - 1)
            )

        return str(out), real_total_dur, merged_segments

    @staticmethod
    def _get_extend_chain(pages: list, motion_plan: dict, start_idx: int) -> list:
        """
        从 pages[start_idx] 之后开始数，找出连续的 extend hold 页索引列表。
        遇到非 hold_skip 页就停。
        返回：跟在 start_idx 后面的 extend hold 页索引列表 [idx1, idx2, ...]
        """
        chain = []
        i = start_idx + 1
        while i < len(pages):
            mp_i = motion_plan.get(pages[i]["page"], {})
            if mp_i.get("motion") == "hold_skip":
                chain.append(i)
                i += 1
            else:
                break
        return chain

    # ── /v2.3.2 hold 辅助方法 ─────────────────────────────────────

    @staticmethod
    def _concat_audio_with_gaps(paths: list, out_path: str,
                                gap: float = 0.4):
        if len(paths) == 1:
            subprocess.run(["ffmpeg", "-y", "-i", paths[0],
                            "-c:a", "libmp3lame", "-b:a", "128k", out_path],
                           check=True, capture_output=True)
            return

        gap_path = str(Path(out_path).parent / f"_gap_{gap}.mp3")
        if not Path(gap_path).exists():
            subprocess.run([
                "ffmpeg", "-y", "-f", "lavfi",
                "-i", "anullsrc=r=44100:cl=stereo",
                "-t", str(gap), "-c:a", "libmp3lame", "-b:a", "128k",
                gap_path,
            ], check=True, capture_output=True)

        # v3.1：写绝对路径，避免 ffmpeg concat 因相对路径解析失败（exit 254）
        list_file = Path(out_path).with_suffix(".txt")
        gap_abs = str(Path(gap_path).resolve())
        with open(list_file, "w", encoding="utf-8") as f:
            for i, p in enumerate(paths):
                p_abs = str(Path(p).resolve())
                if i > 0:
                    f.write(f"file '{gap_abs}'\n")
                f.write(f"file '{p_abs}'\n")
        try:
            subprocess.run([
                "ffmpeg", "-y", "-f", "concat", "-safe", "0",
                "-i", str(list_file.resolve()),
                "-c:a", "libmp3lame", "-b:a", "128k",
                str(Path(out_path).resolve()),
            ], check=True, capture_output=True)
        except subprocess.CalledProcessError as ex:
            # 友好报错，方便定位
            err_msg = ex.stderr.decode(errors="replace")[:500] if ex.stderr else ""
            print(f"  [concat] ffmpeg 失败: {err_msg}")
            print(f"  [concat] list 文件保留: {list_file}")
            raise
        list_file.unlink(missing_ok=True)

    # ══════════════════════════════════════════════════════════
    # KB 输入图自适应预放大（v2.2 不变）
    # ══════════════════════════════════════════════════════════

    def _ensure_kb_input_size(self, img_path: str,
                               profile: PlatformProfile) -> str:
        try:
            r = subprocess.run(
                ["ffprobe", "-v", "quiet", "-print_format", "json",
                 "-show_streams", "-select_streams", "v:0", img_path],
                capture_output=True, text=True, check=True)
            s = json.loads(r.stdout)["streams"][0]
            w, h = s["width"], s["height"]
        except Exception:
            return img_path

        target_long = int(max(profile.width, profile.height)
                          * KB_INPUT_OVERSAMPLE)
        src_long    = max(w, h)
        if src_long >= target_long:
            return img_path

        scale_factor = target_long / src_long
        new_w = int(w * scale_factor)
        new_h = int(h * scale_factor)
        new_w -= (new_w % 2)
        new_h -= (new_h % 2)

        src_p = Path(img_path)
        cache_dir = src_p.parent / KB_PREP_CACHE_DIR
        cache_dir.mkdir(exist_ok=True)
        try:
            mtime = int(src_p.stat().st_mtime)
        except Exception:
            mtime = 0
        key = hashlib.md5(
            f"{img_path}|{mtime}|{new_w}x{new_h}".encode()).hexdigest()[:12]
        cached = cache_dir / f"{src_p.stem}_{key}.png"
        if cached.exists():
            return str(cached)

        print(f"  [kb-prep] 预放大 {src_p.name}: "
              f"{w}×{h} → {new_w}×{new_h} (×{scale_factor:.1f})")
        subprocess.run([
            "ffmpeg", "-y", "-i", img_path,
            "-vf", f"scale={new_w}:{new_h}:flags=lanczos",
            str(cached),
        ], check=True, capture_output=True)
        return str(cached)

    # ══════════════════════════════════════════════════════════
    # KB 滤镜（v2.2 不变）
    # ══════════════════════════════════════════════════════════

    def _kb_filter_scale_crop(self, direction: str, duration: float,
                               width: int, height: int,
                               fps: int) -> str:
        z_max = KB_ZOOM_MAX
        total_frames = max(int(duration * fps), 1)

        if direction == "zoom_in":
            return (
                f"fps={fps},"
                f"scale='{width}*(1+{z_max-1}*n/{total_frames})':"
                f"'{height}*(1+{z_max-1}*n/{total_frames})':"
                f"eval=frame:flags=lanczos,"
                f"crop={width}:{height},"
                f"scale={width}:{height}:flags=lanczos"
            )

        if direction == "zoom_out":
            return (
                f"fps={fps},"
                f"scale='{width}*({z_max}-{z_max-1}*n/{total_frames})':"
                f"'{height}*({z_max}-{z_max-1}*n/{total_frames})':"
                f"eval=frame:flags=lanczos,"
                f"crop={width}:{height},"
                f"scale={width}:{height}:flags=lanczos"
            )

        sw = int(width  * z_max)
        sh = int(height * z_max)
        sw -= sw % 2
        sh -= sh % 2

        x_range = sw - width
        y_range = sh - height

        prefix = f"fps={fps},scale={sw}:{sh}:flags=lanczos"
        suffix = f"scale={width}:{height}:flags=lanczos"

        if direction == "pan_left":
            x_start = x_range
            x_end   = x_range * (1 - KB_PAN_RANGE)
            return (f"{prefix},"
                    f"crop=w={width}:h={height}:"
                    f"x='{x_start}+({x_end}-{x_start})*t/{duration}':"
                    f"y='{y_range/2}',"
                    f"{suffix}")

        if direction == "pan_right":
            x_start = 0
            x_end   = x_range * KB_PAN_RANGE
            return (f"{prefix},"
                    f"crop=w={width}:h={height}:"
                    f"x='{x_start}+({x_end}-{x_start})*t/{duration}':"
                    f"y='{y_range/2}',"
                    f"{suffix}")

        if direction == "pan_up":
            y_start = y_range
            y_end   = y_range * (1 - KB_PAN_RANGE)
            return (f"{prefix},"
                    f"crop=w={width}:h={height}:"
                    f"x='{x_range/2}':"
                    f"y='{y_start}+({y_end}-{y_start})*t/{duration}',"
                    f"{suffix}")

        return (
            f"fps={fps},"
            f"scale='{width}*(1+{z_max-1}*n/{total_frames})':"
            f"'{height}*(1+{z_max-1}*n/{total_frames})':"
            f"eval=frame:flags=lanczos,"
            f"crop={width}:{height},"
            f"scale={width}:{height}:flags=lanczos"
        )

    # ══════════════════════════════════════════════════════════
    # 单页 clip 生成（v2.2 不变）
    # ══════════════════════════════════════════════════════════

    def _make_kb_clip(self, img_path: str, audio_path: str,
                      out_path: str, duration: float,
                      direction: str, profile: PlatformProfile,
                      transition_in: str = None) -> float:
        """v3.1: transition_in 控制淡入淡出节奏（match_cut 无缝、whip_pan 紧凑等）
        v2.4.1: audio_path 为空时（silent_beat 静默镜头）生成无音轨静音 clip。
        """
        src_path = self._ensure_kb_input_size(img_path, profile)

        # 根据 transition_in 选淡入淡出（旧 yaml 无此字段 → 默认与 v3.0 一致）
        fade_in_d, fade_out_d = TRANSITION_FADE_MAP.get(
            transition_in, DEFAULT_FADE)

        dur = max(duration + 0.3, 4.0)
        fout_st = max(0.0, dur - fade_out_d)

        vf = self._kb_filter_scale_crop(
            direction, dur, profile.width, profile.height, profile.fps)

        # 淡入：fade_in_d=0 时跳过滤镜（避免 0 时长 fade 出错）
        if fade_in_d > 0:
            vf += f",fade=in:st=0:d={fade_in_d}"
        if fade_out_d > 0:
            vf += f",fade=out:st={fout_st:.3f}:d={fade_out_d}"

        # v2.4.1: 静默镜头 —— 无音频输入,生成静音视频
        has_audio = bool(audio_path) and Path(audio_path).exists()
        if not has_audio:
            subprocess.run([
                "ffmpeg", "-y",
                "-loop", "1",
                "-i", src_path,
                "-vf", vf,
                "-r", str(profile.fps),
                "-c:v", "libx264", "-preset", "medium",
                "-t", f"{dur:.3f}",
                "-pix_fmt", "yuv420p",
                "-an",                    # 明确无音轨
                out_path,
            ], check=True, capture_output=True)
            # v2.5.2 字幕同步修复:用 ffprobe 实测 mp4 真实时长覆盖公式 dur,
            # 让全片字幕累加用的时间与拼片实际时长完全一致。
            # 静默分支用 -t 控制时长,实测值与公式值通常只差 ±1 帧;
            # 但为了和有音轨分支行为一致、彻底消除 fps 取整误差,统一实测。
            real_dur = self._get_video_duration(out_path)
            if real_dur <= 0:
                real_dur = dur   # ffprobe 失败兜底,回退到公式值
            print(f"  [clip/kb] {Path(out_path).name} "
                  f"({real_dur:.2f}s 实测, 目标 {dur:.2f}s, "
                  f"{direction}, 静默镜头无音轨, {profile.fps}fps)")
            return real_dur

        # 音频淡入淡出同步（音频淡入即使是 0 也要保留至少一点，避免咔哒声）
        af_a_in  = max(fade_in_d,  0.05)
        af_a_out = max(fade_out_d, 0.10)
        af_a_out_st = max(0.0, dur - af_a_out)

        subprocess.run([
            "ffmpeg", "-y",
            "-loop", "1",
            "-i", src_path,
            "-i", audio_path,
            "-vf", vf,
            "-r", str(profile.fps),
            "-c:v", "libx264", "-preset", "medium",
            "-c:a", "aac", "-b:a", "128k",
            "-af", f"afade=in:st=0:d={af_a_in},afade=out:st={af_a_out_st:.3f}:d={af_a_out}",
            "-t", f"{dur:.3f}",
            "-pix_fmt", "yuv420p",
            "-shortest",
            out_path,
        ], check=True, capture_output=True)
        # v2.5.2 字幕同步修复:
        #   关键问题 —— `-shortest` 让 ffmpeg 在最短的输入流(音频)结束时停止,
        #   导致 fade out 的 0.3s 黑场 tail 没被写入 mp4。
        #   实测 mp4 dur ≈ audio_dur,而公式 dur = audio_dur + 0.3,每页差 0.3s。
        #   累加偏差到 8 页就有 2.4s,表现为字幕严重滞后于画面。
        #   解决:跑完 ffmpeg 后立刻 ffprobe 拿真实时长,返回它而非公式值。
        #   字幕段时间表(audio_info.segments)基于 audio_dur 算的本来就准,
        #   总片时长累加用实测值,字幕就跟拼片精准对齐。
        real_dur = self._get_video_duration(out_path)
        if real_dur <= 0:
            real_dur = dur   # ffprobe 失败兜底,回退到公式值
        trans_label = transition_in or "default"
        print(f"  [clip/kb] {Path(out_path).name} "
              f"({real_dur:.2f}s 实测, 目标 {dur:.2f}s, "
              f"{direction}, fade={fade_in_d:.2f}/{fade_out_d:.2f}, "
              f"trans={trans_label}, {profile.fps}fps)")
        return real_dur

    def _make_video_clip(self, video_path: str, audio_path: str,
                         out_path: str, audio_duration: float,
                         profile: PlatformProfile) -> float:
        fade = 0.3
        vid_dur = self._get_video_duration(video_path)
        target_dur = max(vid_dur, audio_duration + 0.3, 4.0)

        vf_extra = ""
        if vid_dur < target_dur:
            pad_extra = target_dur - vid_dur
            print(f"  [video] 视频 {vid_dur:.1f}s < 目标 {target_dur:.1f}s，"
                  f"末帧定格 {pad_extra:.1f}s 补足")
            vf_extra = f",tpad=stop_mode=clone:stop_duration={pad_extra:.3f}"

        dur = target_dur
        fout = max(0.0, dur - fade)

        vf = (
            f"fps={profile.fps},"
            f"scale={profile.width}:{profile.height}:"
            f"force_original_aspect_ratio=decrease:flags=lanczos,"
            f"pad={profile.width}:{profile.height}:(ow-iw)/2:(oh-ih)/2:black,"
            f"fade=in:st=0:d={fade},fade=out:st={fout:.3f}:d={fade}"
            f"{vf_extra}"
        )

        # v2.x: 静默镜头 —— 与 _make_kb_clip 行为一致，无音频输入则生成静音视频
        has_audio = bool(audio_path) and Path(audio_path).exists()
        if not has_audio:
            subprocess.run([
                "ffmpeg", "-y",
                "-i", video_path,
                "-vf", vf,
                "-r", str(profile.fps),
                "-c:v", "libx264", "-preset", "medium",
                "-t", f"{dur:.3f}",
                "-pix_fmt", "yuv420p",
                "-an",  # 明确无音轨
                out_path,
            ], check=True, capture_output=True)
            real_dur = self._get_video_duration(out_path)
            if real_dur <= 0:
                real_dur = dur
            in_fps = self._get_video_fps(video_path)
            print(f"  [clip/video] {Path(out_path).name} "
                  f"({real_dur:.2f}s 实测, 目标 {dur:.2f}s, "
                  f"静默镜头无音轨, {in_fps}fps→{profile.fps}fps)")
            return real_dur

        # 有音频分支（原逻辑不变）
        subprocess.run([
            "ffmpeg", "-y",
            "-i", video_path,
            "-i", audio_path,
            "-map", "0:v:0", "-map", "1:a:0",  # 显式指定流，避免误取视频自带音轨
            "-vf", vf,
            "-r", str(profile.fps),
            "-c:v", "libx264", "-preset", "medium",
            "-c:a", "aac", "-b:a", "128k",
            "-af", f"afade=in:st=0:d={fade},afade=out:st={fout:.3f}:d={fade}",
            "-t", f"{dur:.3f}",
            "-pix_fmt", "yuv420p",
            out_path,
        ], check=True, capture_output=True)

        real_dur = self._get_video_duration(out_path)
        if real_dur <= 0:
            real_dur = dur
        in_fps = self._get_video_fps(video_path)
        print(f"  [clip/video] {Path(out_path).name} "
              f"({real_dur:.2f}s 实测, 目标 {dur:.2f}s, "
              f"{in_fps}fps→{profile.fps}fps)")
        return real_dur

    # ══════════════════════════════════════════════════════════
    # v3 新增：BGM 长音轨构建
    # ══════════════════════════════════════════════════════════

    def _build_bgm_track(self, page_durations: list, page_moods: list,
                         total_duration: float, out_path: str) -> bool:
        """
        生成一条匹配视频时长的 BGM 音轨。
        策略：同 mood 连续页用同一首；切 mood 时交叉淡化（cross fade）。

        参数：
          page_durations: [duration, ...] 每页时长（秒）
          page_moods:     [mood, ...] 每页 bgm_mood
          total_duration: 整片总时长
          out_path:       BGM 音轨输出 mp3 路径
        """
        if not page_durations:
            return False

        # 把连续相同 mood 的页合并成一个"音乐段"
        # segments = [(start, end, mood), ...]
        segments = []
        cum = 0.0
        cur_mood  = page_moods[0] if page_moods else "tension"
        seg_start = 0.0
        for i, (d, m) in enumerate(zip(page_durations, page_moods)):
            if m != cur_mood:
                segments.append((seg_start, cum, cur_mood))
                seg_start = cum
                cur_mood = m
            cum += d
        segments.append((seg_start, cum, cur_mood))

        if not segments:
            return False

        print(f"\n  [BGM/track] 音乐段划分:")
        for s, e, m in segments:
            print(f"    {s:6.1f}s → {e:6.1f}s  mood={m}")

        # 为每个音乐段挑一首 BGM，生成对应长度的子片段
        tmp_dir = Path(tempfile.mkdtemp(prefix="bgm_"))
        sub_clips = []
        for i, (s, e, mood) in enumerate(segments):
            bgm_file = _pick_bgm(mood, seed=i)
            if bgm_file is None:
                print(f"  [BGM/track] mood={mood} 无 BGM 可选，跳过此段")
                continue

            seg_dur = e - s
            sub_path = str(tmp_dir / f"bgm_seg_{i:02d}.mp3")
            # 循环 BGM 到目标时长，加单段淡入淡出
            cmd = [
                "ffmpeg", "-y",
                "-stream_loop", "-1",
                "-i", str(bgm_file),
                "-t", f"{seg_dur:.3f}",
                "-af",
                f"afade=t=in:st=0:d={BGM_FADE},"
                f"afade=t=out:st={max(0, seg_dur - BGM_FADE):.3f}:d={BGM_FADE},"
                f"volume={BGM_VOLUME}",
                "-c:a", "libmp3lame", "-b:a", "192k",
                sub_path,
            ]
            try:
                subprocess.run(cmd, check=True, capture_output=True)
                sub_clips.append((sub_path, seg_dur, mood, bgm_file.name))
                print(f"  [BGM/track] seg {i}: {bgm_file.name} ({seg_dur:.1f}s)")
            except subprocess.CalledProcessError as ex:
                print(f"  [BGM/track] seg {i} 失败: {ex.stderr.decode()[:200]}")

        if not sub_clips:
            return False

        # 拼接所有段（无交叉）— 第一版用简单 concat
        # （afade 已经处理了段尾淡出，听起来不会硬切；
        #   完美的 acrossfade 实现复杂，先简单可用）
        list_file = tmp_dir / "concat.txt"
        with open(list_file, "w") as f:
            for sp, _, _, _ in sub_clips:
                f.write(f"file '{Path(sp).resolve()}'\n")

        cmd = [
            "ffmpeg", "-y", "-f", "concat", "-safe", "0",
            "-i", str(list_file),
            "-c:a", "libmp3lame", "-b:a", "192k",
            out_path,
        ]
        try:
            subprocess.run(cmd, check=True, capture_output=True)
            print(f"  [BGM/track] ✓ 完整 BGM 音轨: {Path(out_path).name} "
                  f"({total_duration:.1f}s)")
            return True
        except subprocess.CalledProcessError as ex:
            print(f"  [BGM/track] concat 失败: {ex.stderr.decode()[:300]}")
            return False

    # ══════════════════════════════════════════════════════════
    # 主流程
    # ══════════════════════════════════════════════════════════

    def make_video(self, motion_plan: dict,
                   platform: str = "youtube",
                   out_dir: str = "") -> str:
        from registry import Registry

        profile = PROFILES.get(platform, PROFILES["youtube"])
        out_dir = out_dir or str(Path("book_output") / self.story.story_id)
        Path(out_dir).mkdir(parents=True, exist_ok=True)

        reg = Registry(self.story.path)
        approved = reg.all_approved(self.story.story_id)
        asset_map = {a.page_num: a for a in approved}

        n_video = sum(1 for v in motion_plan.values()
                      if v.get("motion") == "ai_video")
        print(f"\n{'=' * 55}")
        print(f"  视频生产 v3.0 (BGM分页+字幕): {self.story.title}")
        print(f"  平台: {profile.name}  尺寸: {profile.width}×{profile.height}"
              f" @ {profile.fps}fps")
        print(f"  页面: {len(self.story.pages)}  动态: {n_video}  "
              f"字幕: {'开' if self.enable_subtitles else '关'}")
        print(f"{'=' * 55}")

        # v3.1：持久化缓存目录（崩溃后重跑可复用）
        cache_dir = Path(out_dir) / PRODUCE_CACHE_DIR
        cache_dir.mkdir(parents=True, exist_ok=True)
        tmp = cache_dir
        print(f"  [cache] 缓存目录: {cache_dir}")

        clips           = []
        page_durations  = []     # 每页 clip 实际时长（用于 BGM 段划分）
        page_moods      = []     # 每页 bgm_mood
        all_subtitles   = []     # 全片字幕段（绝对时间）
        bench_meta      = {}
        total_dur = 0.0

        # v2.3.2：用索引循环以便前瞻 extend 链
        pages_list = list(self.story.pages)
        idx = 0
        while idx < len(pages_list):
            page_cfg = pages_list[idx]
            pn = page_cfg["page"]
            mp = motion_plan.get(pn, {"motion": "ken_burns",
                                       "kb_direction": "zoom_in"})

            # v2.3.2：extend hold 页已被前一主页吞掉，这里直接跳过
            if mp.get("motion") == "hold_skip":
                idx += 1
                continue

            asset = asset_map.get(pn)
            if not asset or not asset.exists():
                print(f"  p{pn} 无资产，跳过")
                idx += 1
                continue

            mood = mp.get("bgm_mood", "tension")
            narration = page_cfg.get("narration",
                                     page_cfg.get("title", f"第{pn}页"))

            # v2.3.2：前瞻后续 extend hold 链
            extend_chain = self._get_extend_chain(pages_list, motion_plan, idx)

            # hold 标记日志(v2.4.4:按 _hold_type 准确打 extend 或 cutaway,
            # 旧版无条件打 cutaway,误导诊断)
            hold_tag = ""
            if page_cfg.get("_hold"):
                src_p = page_cfg.get("_hold_source_page", "?")
                actual_hold_type = (page_cfg.get("_hold_type") or "extend").strip().lower()
                if actual_hold_type not in ("extend", "cutaway"):
                    actual_hold_type = "extend"
                hold_tag = f" [HOLD-{actual_hold_type}←p{src_p}]"
            if extend_chain:
                hold_tag += f" +{len(extend_chain)}extend"

            print(f"\n  ── p{pn}: {narration[:20]}...{hold_tag} "
                  f"({mp['motion']}, BGM={mood}) ──")

            # 音频生成（含每段时间表）
            audio_info_main = asyncio.run(
                self._build_page_audio(page_cfg, tmp, pn))

            # v2.3.2：合并后续 extend 页 audio
            if extend_chain:
                ext_audios = [audio_info_main]
                for ext_idx in extend_chain:
                    ext_page = pages_list[ext_idx]
                    ext_audio = asyncio.run(
                        self._build_page_audio(ext_page, tmp, ext_page["page"]))
                    ext_audios.append(ext_audio)
                    print(f"     [extend] p{ext_page['page']} audio 拼接 "
                          f"({ext_audio.duration:.1f}s)")
                merged_path, merged_dur, merged_segments = self._concat_audio_infos(
                    ext_audios, tmp, pn)

                # 构造合并后的 audio_info（仿 PageAudio）
                class _MergedAudio:
                    pass
                audio_info = _MergedAudio()
                audio_info.audio_path = merged_path
                audio_info.duration   = merged_dur
                audio_info.segments   = merged_segments
            else:
                audio_info = audio_info_main

            # ── clip 缓存 key：包含图像、音频、motion 决策、平台 ──
            audio_basename = Path(audio_info.audio_path).name
            asset_path_str = asset.path if asset else ""
            try:
                asset_mtime = int(Path(asset_path_str).stat().st_mtime) if asset_path_str else 0
            except Exception:
                asset_mtime = 0
            clip_sig = "|".join([
                f"asset={asset_path_str}",
                f"mtime={asset_mtime}",
                f"audio={audio_basename}",
                f"motion={mp.get('motion','')}",
                f"kb={mp.get('kb_direction','')}",
                f"video_tier={mp.get('video_tier','')}",
                f"transition={page_cfg.get('transition_in','')}",
                f"platform={profile.name}",
                f"size={profile.width}x{profile.height}",
                f"fps={profile.fps}",
            ])
            clip_key = hashlib.md5(clip_sig.encode("utf-8")).hexdigest()[:12]
            clip_path = str(tmp / f"clip_{pn:02d}_{clip_key}.mp4")

            # ── clip 缓存命中检查 ───────────────────────────────────
            if Path(clip_path).exists():
                try:
                    dur = self._get_video_duration(clip_path)
                    if dur > 0.5:  # 合法时长
                        print(f"  [cache] ✓ clip 缓存命中 p{pn} ({dur:.1f}s)")
                        bench_meta[f"clip_{pn:02d}"] = {
                            "motion":   mp.get("motion", "?"),
                            "src":      Path(asset_path_str).name if asset_path_str else "",
                            "kb_direction": mp.get("kb_direction", ""),
                            "dur":      round(dur, 2),
                            "bgm_mood": mood,
                            "from_cache": True,
                        }
                        # 收集字幕（即便从缓存复用，仍要按累计时间偏移）
                        for rel_s, rel_e, text, speaker in audio_info.segments:
                            all_subtitles.append(SubtitleSegment(
                                start   = total_dur + rel_s,
                                end     = total_dur + rel_e,
                                text    = text,
                                speaker = speaker,
                            ))
                        total_dur += dur
                        page_durations.append(dur)
                        page_moods.append(mood)
                        clips.append(clip_path)
                        # v2.3.2：缓存命中也要消耗 extend 链
                        idx += 1 + len(extend_chain) if extend_chain else 1
                        continue
                except Exception:
                    print(f"  [cache] clip 缓存损坏，重生 p{pn}")

            # v2.3.4：intended_duration_sec 加到 audio 时长上
            # 语义：音频播完后画面再静止 N 秒（铁律 17）
            # 字段不存在 / 0 时行为完全等同 v2.3.3（零回归风险）
            # v2.4.1: silent_beat 镜头特殊处理 —— intended_duration_sec 已是
            #         audio_info.duration 本身(见 _build_page_audio),不再叠加。
            intended_extra = float(page_cfg.get("intended_duration_sec", 0.0) or 0.0)
            if getattr(audio_info, "is_silent", False):
                effective_audio_dur = audio_info.duration   # 已含全部时长
                print(f"  [silent-beat] p{pn} 画面停留 {effective_audio_dur:.1f}s,无音轨")
            else:
                effective_audio_dur = audio_info.duration + intended_extra
                if intended_extra > 0:
                    print(f"  [intended-duration] p{pn} 在配音 {audio_info.duration:.1f}s "
                          f"后画面再停 {intended_extra:.1f}s")

            if mp["motion"] == "ai_video":
                video_file = self._find_video_clip(pn, out_dir)
                if video_file:
                    dur = self._make_video_clip(
                        video_file, audio_info.audio_path, clip_path,
                        effective_audio_dur, profile)
                    bench_meta[f"clip_{pn:02d}"] = {
                        "motion":  "ai_video",
                        "src":     Path(video_file).name,
                        "src_fps": self._get_video_fps(video_file),
                        "dur":     round(dur, 2),
                        "bgm_mood": mood,
                    }
                else:
                    print(f"  ⚠ p{pn} 未找到 AI 视频，降级到 KB")
                    dur = self._make_kb_clip(
                        asset.path, audio_info.audio_path, clip_path,
                        effective_audio_dur, "zoom_in", profile,
                        transition_in=page_cfg.get("transition_in"))
                    bench_meta[f"clip_{pn:02d}"] = {
                        "motion":   "ken_burns_fallback",
                        "src":      Path(asset.path).name,
                        "kb_direction": "zoom_in",
                        "dur":      round(dur, 2),
                        "bgm_mood": mood,
                    }
            else:
                direction = mp.get("kb_direction", "zoom_in")
                dur = self._make_kb_clip(
                    asset.path, audio_info.audio_path, clip_path,
                    effective_audio_dur, direction, profile,
                    transition_in=page_cfg.get("transition_in"))
                bench_meta[f"clip_{pn:02d}"] = {
                    "motion":       "ken_burns",
                    "src":          Path(asset.path).name,
                    "kb_direction": direction,
                    "dur":          round(dur, 2),
                    "bgm_mood":     mood,
                }

            # 收集字幕（把页内相对时间转换为全片绝对时间）
            for rel_s, rel_e, text, speaker in audio_info.segments:
                all_subtitles.append(SubtitleSegment(
                    start   = total_dur + rel_s,
                    end     = total_dur + rel_e,
                    text    = text,
                    speaker = speaker,
                ))

            total_dur += dur
            page_durations.append(dur)
            page_moods.append(mood)
            clips.append(clip_path)

            # v2.3.2：消耗 extend 链，跳过这些索引
            idx += 1 + len(extend_chain) if extend_chain else 1

        if not clips:
            print("  无片段生成")
            return ""

        # 写 bench 元数据
        bench_path = Path(out_dir) / "_bench_clips.json"
        bench_path.write_text(
            json.dumps(bench_meta, ensure_ascii=False, indent=2),
            encoding="utf-8")
        print(f"\n  [bench] 元数据已写入 {bench_path.name}")

        # ── concat 所有 clips ────────────────────────────────────
        ts = datetime.now().strftime("%Y%m%d_%H%M")
        out_name = f"{self.story.story_id}_{platform}_{ts}.mp4"
        out_path = str(Path(out_dir) / out_name)
        self._concat_clips(clips, out_path)

        # ── BGM 分页混音 ────────────────────────────────────────
        bgm_track_path = str(tmp / "bgm_full.mp3")
        bgm_ok = False
        if any(_list_bgm_files(m) for m in BGM_VALID_MOODS):
            bgm_ok = self._build_bgm_track(
                page_durations, page_moods, total_dur, bgm_track_path)
        else:
            print(f"\n  [BGM] refs/bgm/ 下无任何文件，跳过 BGM")

        # ── v2.7 Ambient 轨 + SFX 叠加(在 BGM 之后) ──────────────
        ambient_ok = False
        sfx_events = []
        ambient_track_path = str(tmp / "ambient_full.mp3")
        if _SCENE_AUDIO_AVAILABLE:
            try:
                ambient_ok, sfx_events = build_ambient_and_sfx_for_episode(
                    list(self.story.pages),
                    page_durations,
                    ambient_track_path,
                )
            except Exception as _e:
                print(f"\n  [scene_audio] 失败,降级跳过: {_e}")
                ambient_ok = False
                sfx_events = []

        # 叠加顺序: BGM → Ambient → SFX
        if bgm_ok:
            self._overlay_bgm_track(out_path, bgm_track_path)
        elif self.bgm_path and Path(self.bgm_path).exists():
            # 兼容旧版：传统单首 BGM 兜底
            self._overlay_bgm_legacy(out_path, self.bgm_path)

        if ambient_ok:
            self._overlay_ambient_track(out_path, ambient_track_path)

        if sfx_events:
            self._overlay_sfx_events(out_path, sfx_events, total_dur, str(tmp))

        # ── 字幕烧录（v3 新增）────────────────────────────────────
        if self.enable_subtitles and all_subtitles:
            srt_path = str(Path(out_dir) / f"{self.story.story_id}.srt")
            srt_content = _build_srt(all_subtitles)
            Path(srt_path).write_text(srt_content, encoding="utf-8")
            print(f"\n  [字幕] SRT 已生成: {Path(srt_path).name} "
                  f"({len(all_subtitles)} 段)")

            self._burn_subtitles(out_path, srt_path, profile)

        mm, ss = divmod(int(total_dur), 60)
        print(f"\n{'='*55}")
        print(f"  视频完成 → {out_path}")
        print(f"  时长: {mm}分{ss}秒  片段: {len(clips)}  字幕段: {len(all_subtitles)}")
        print(f"{'='*55}")
        return out_path

    # ══════════════════════════════════════════════════════════
    # 辅助
    # ══════════════════════════════════════════════════════════

    def _find_video_clip(self, page_num: int, out_dir: str) -> Optional[str]:
        patterns = [
            f"page{page_num:02d}_wan14b_*.mp4",
            f"page{page_num:02d}_wan_*.mp4",
            f"page{page_num:02d}_i2v_*.mp4",
            f"page{page_num:02d}_video_*.mp4",
        ]
        for pattern in patterns:
            matches = list(Path(out_dir).glob(pattern))
            if matches:
                latest = max(matches, key=lambda p: p.stat().st_mtime)
                print(f"  [video] 找到 AI clip: {latest.name}")
                return str(latest)
        return None

    def _concat_clips(self, clips: list, out_path: str):
        if len(clips) == 1:
            subprocess.run(["ffmpeg", "-y", "-i", clips[0],
                            "-c", "copy", out_path],
                           check=True, capture_output=True)
            return


        list_file = str(Path(out_path).parent / "clips.txt")
        with open(list_file, "w") as f:
            for c in clips:
                abs_path = str(Path(c).resolve())
                f.write(f"file '{abs_path}'\n")

        result = subprocess.run([
            "ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", list_file,
            "-c:v", "libx264",
            "-c:a", "aac", "-b:a", "128k",
            "-af", "acompressor=threshold=-18dB:ratio=4:attack=5:release=50",
            "-pix_fmt", "yuv420p",
            "-movflags", "+faststart",
            out_path,
        ], capture_output=True, text=True)
        if result.returncode != 0:
            print("===== FFMPEG STDERR =====")
            print(result.stderr)
            print("===== clips.txt =====")
            print(Path(list_file).read_text())
            raise subprocess.CalledProcessError(result.returncode, result.args)

    def _overlay_bgm_track(self, video_path: str, bgm_track_path: str):
        """v3：用预生成的 BGM 长音轨叠加"""
        tmp_out = video_path + ".bgm.mp4"
        try:
            subprocess.run([
                "ffmpeg", "-y", "-i", video_path, "-i", bgm_track_path,
                "-filter_complex",
                "[0:a][1:a]amix=inputs=2:duration=first:dropout_transition=0[out]",
                "-map", "0:v", "-map", "[out]",
                "-c:v", "copy",
                "-c:a", "aac", "-b:a", "128k",
                "-shortest",
                tmp_out,
            ], check=True, capture_output=True)
            import os
            os.replace(tmp_out, video_path)
            print(f"  [BGM] ✓ 已叠加分页 BGM 音轨")
        except Exception as e:
            print(f"  [BGM] 叠加失败（非致命）: {e}")
            Path(tmp_out).unlink(missing_ok=True)

    def _overlay_bgm_legacy(self, video_path: str, bgm_path: str,
                             bgm_volume: float = 0.15):
        """旧版兼容：单首 BGM 全程铺底（用于 self.bgm_path 兜底）"""
        tmp_out = video_path + ".bgm.mp4"
        try:
            subprocess.run([
                "ffmpeg", "-y", "-i", video_path, "-i", bgm_path,
                "-filter_complex",
                f"[1:a]volume={bgm_volume},aloop=loop=-1:size=2e+09[bgm];"
                f"[0:a][bgm]amix=inputs=2:duration=first:dropout_transition=3[out]",
                "-map", "0:v", "-map", "[out]",
                "-c:v", "copy",
                "-c:a", "aac", "-b:a", "128k",
                "-shortest",
                tmp_out,
            ], check=True, capture_output=True)
            import os
            os.replace(tmp_out, video_path)
            print(f"  [BGM] ✓ 已叠加单首 BGM (legacy, volume={bgm_volume})")
        except Exception as e:
            print(f"  [BGM] 叠加失败（非致命）: {e}")
            Path(tmp_out).unlink(missing_ok=True)


    def _overlay_ambient_track(self, video_path: str, ambient_path: str):
        """v2.7: 整片叠加 ambient 长音轨(在 BGM 之上)。"""
        tmp_out = video_path + ".amb.mp4"
        try:
            subprocess.run([
                "ffmpeg", "-y", "-i", video_path, "-i", ambient_path,
                "-filter_complex",
                "[0:a][1:a]amix=inputs=2:duration=first:normalize=0[aout]",
                "-map", "0:v", "-map", "[aout]",
                "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
                "-shortest", tmp_out,
            ], check=True, capture_output=True)
            import os
            os.replace(tmp_out, video_path)
            print(f"  [ambient] ✓ 整片叠加完成")
        except Exception as e:
            print(f"  [ambient] 叠加失败(非致命): {e}")
            Path(tmp_out).unlink(missing_ok=True)

    def _overlay_sfx_events(self, video_path: str, sfx_events: list,
                              total_dur: float, tmp_dir: str):
        """v2.7: 把所有 sfx 按时刻叠加到视频音轨上。"""
        if not sfx_events:
            return
        sfx_track_path = str(Path(tmp_dir) / "sfx_full.mp3")
        n = len(sfx_events)

        inputs = [
            "-f", "lavfi", "-t", f"{total_dur}",
            "-i", "anullsrc=channel_layout=stereo:sample_rate=44100",
        ]
        filter_parts = ["[0:a]anull[base]"]
        mix_inputs = ["[base]"]
        for i, ev in enumerate(sfx_events, start=1):
            inputs += ["-i", ev.path]
            delay_ms = max(0, int(ev.at_sec * 1000))
            vol = 0.5 if ev.important else 0.35
            filter_parts.append(
                f"[{i}:a]volume={vol},"
                f"aformat=channel_layouts=stereo:sample_rates=44100,"
                f"afade=t=in:st=0:d=0.03,"
                f"adelay={delay_ms}|{delay_ms}[s{i}]"
            )
            mix_inputs.append(f"[s{i}]")
        filter_parts.append(
            f"{''.join(mix_inputs)}amix=inputs={n+1}:"
            f"duration=longest:normalize=0[sfxmix]"
        )

        cmd = (
            ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error"]
            + inputs
            + ["-filter_complex", ";".join(filter_parts),
               "-map", "[sfxmix]",
               "-t", f"{total_dur}",
               "-c:a", "libmp3lame", "-b:a", "192k",
               sfx_track_path]
        )
        result = subprocess.run(cmd, capture_output=True)
        if result.returncode != 0:
            print(f"  [sfx] 生成 sfx 轨失败,跳过: {result.stderr.decode()[:200]}")
            return

        tmp_out = video_path + ".sfx.mp4"
        try:
            subprocess.run([
                "ffmpeg", "-y", "-i", video_path, "-i", sfx_track_path,
                "-filter_complex",
                "[0:a][1:a]amix=inputs=2:duration=first:normalize=0[aout]",
                "-map", "0:v", "-map", "[aout]",
                "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
                "-shortest", tmp_out,
            ], check=True, capture_output=True)
            import os
            os.replace(tmp_out, video_path)
            print(f"  [sfx] ✓ 叠加 {n} 个 SFX 事件")
        except Exception as e:
            print(f"  [sfx] 叠加失败(非致命): {e}")
            Path(tmp_out).unlink(missing_ok=True)

    def _burn_subtitles(self, video_path: str, srt_path: str,
                        profile: PlatformProfile):
        """v3：把 SRT 字幕烧到视频上（硬字幕）"""
        tmp_out = video_path + ".sub.mp4"
        sub_filter = _ffmpeg_subtitle_filter(srt_path, profile, self.subtitle_font)

        try:
            subprocess.run([
                "ffmpeg", "-y", "-i", video_path,
                "-vf", sub_filter,
                "-c:v", "libx264", "-preset", "medium",
                "-c:a", "copy",
                "-pix_fmt", "yuv420p",
                "-movflags", "+faststart",
                tmp_out,
            ], check=True, capture_output=True)
            import os
            os.replace(tmp_out, video_path)
            print(f"  [字幕] ✓ 已烧录硬字幕（{self.subtitle_font}）")
        except subprocess.CalledProcessError as e:
            print(f"  [字幕] 烧录失败: {e.stderr.decode()[:300]}")
            print(f"  [字幕] 提示：检查系统是否安装 '{self.subtitle_font}' 字体；")
            print(f"           SRT 文件保留在 {srt_path}，可手动用其他工具烧录")
            Path(tmp_out).unlink(missing_ok=True)

    @staticmethod
    def _get_video_duration(path: str) -> float:
        try:
            r = subprocess.run(
                ["ffprobe", "-v", "quiet", "-print_format", "json",
                 "-show_format", path],
                capture_output=True, text=True, check=True)
            return float(json.loads(r.stdout)["format"]["duration"])
        except Exception as e:
            print(f"  ⚠ ffprobe duration 失败 ({path}): {e}")
            return 5.0

    @staticmethod
    def _get_video_fps(path: str) -> float:
        try:
            r = subprocess.run(
                ["ffprobe", "-v", "quiet", "-print_format", "json",
                 "-show_streams", "-select_streams", "v:0", path],
                capture_output=True, text=True, check=True)
            streams = json.loads(r.stdout).get("streams", [])
            if not streams:
                return 0.0
            rate = streams[0].get("r_frame_rate", "0/1")
            num, den = rate.split("/")
            return round(float(num) / float(den), 2) if float(den) else 0.0
        except Exception:
            return 0.0
