# 音频工程总集成指南 v2.7

> 把已下的 SFX 素材真正接入生产流水线。
> 本文档包含全部 patch 和落盘步骤。**按顺序做,不要跳**。

---

## 0. 落盘清单(先放文件,再改 patch)

| 文件 | 落到 | 类型 |
|---|---|---|
| ambient_continuity.py | `core/audio/ambient_continuity.py` | 新增 |
| scene_audio_helper.py | `core/audio/scene_audio_helper.py` | 新增 |

放完先跑一次自测确认不挂:

```bash
cd demo20260510
python core/audio/ambient_continuity.py
# 应该看到 "原始 8 页 → 平滑后 3 段:" 这种输出
```

---

## 1. PATCH A: long_storyboard.py 产 scene_audio 字段

### A.1 改 prompt 模板 —— 加铁律 23(声画自洽)

**位置**: long_storyboard.py 第 700 行附近(铁律 14.x 之后,铁律 15 之前找个合适位置插入)

```
铁律 23 (v2.7 新增):scene_audio 字段约束 ⚠️ 永久规则

每个镜头【必须】产出 scene_audio 字段,结构:

  "scene_audio": {
    "ambient": ["rain_heavy", "temple_interior"],  // 环境底床,0-2 个
    "sfx": [
      {"id": "lamp_drop_metal", "at": 4.5, "important": true}  // 事件音效,0-2 个
    ]
  }

【ambient】= 该镜头的"空间环境音",描述地点,不描述情绪。
合法 id(只能从这里选):
  rain_light, rain_heavy, wind_howl, thunder_distant, crickets_night,
  temple_interior, old_house_creak, graveyard_night, marketplace_chinese,
  well_echo, mountain_night, candle_flame, empty_corridor, low_drone,
  basement_drip

  规则:
    - 同一"场景"(3-8 个连续镜头共享同一空间)应该使用相同的 ambient。
      例:第 1-5 镜在墓园外 → 全部 ["graveyard_night"]
         第 6-10 镜进入墓室 → 全部 ["basement_drip", "low_drone"]
    - 不要每镜都换 ambient。ambient 标的是"地点",不是"情绪"。
    - 室内场景宁可写 ["temple_interior"] 也不要留空。

【sfx】= 该镜头精确编排的"点状事件音效"。
合法 id(只能从这里选):
  thunder_crack, bell_toll, small_bell_ring, glass_shatter,
  door_creak, door_slam, footstep_wood, footstep_stone,
  heartbeat_single, heartbeat_loop, breath_gasp, body_fall,
  paper_rustle, coin_drop, blood_drip, lamp_drop_metal,
  magic_shimmer, ghost_whisper, branch_snap, wind_gust,
  fabric_swish, cloth_tear, mirror_resonance

  规则:
    - at: 镜头内的偏移秒数(从 0 开始)。example: "at": 2.5 表示镜头开始 2.5 秒后触发
    - important: true 表示重要事件(雷击/玻璃碎/钟声),false/缺省=普通
    - 每镜 sfx 数量 ≤ 2(coordinator 会截断超出部分)
    - 整集 sfx 总数 ≤ 30
    - 只有 narration 提到的事件才标 sfx。narration 没写门吱呀就别加 door_creak。

【narration_emotion】= 旁白情绪标签(可选,默认 "neutral")
合法值:
  neutral, whisper, desperate, cold, trembling, eerie, grave, urgent
```

### A.2 改 6 个示例镜头 —— 加 scene_audio 字段示范

**位置**: long_storyboard.py 第 1364、1388、1404、1430、1446、1472 行附近

每个示例镜头的字典里,在 "bgm_mood" 那一行**前面**加 scene_audio:

**第 1364 示例(夏夜叙述镜)**:
```python
"narration": "夏夜闷热如蒸笼,法租界沉在霉味里。",
"dialogue": [],
"scene_audio": {
    "ambient": ["marketplace_chinese", "wind_howl"],
    "sfx": []
},
"narration_emotion": "neutral",
...
"bgm_mood": "serene",
```

**第 1388 示例(尸体检查镜)**:
```python
"narration": "林书白蹲下检查尸体。",
"scene_audio": {
    "ambient": ["empty_corridor"],
    "sfx": [{"id": "footstep_wood", "at": 0.5}]
},
"narration_emotion": "eerie",
...
"bgm_mood": "tension",
```

**第 1404 示例(对话主镜)**:
```python
"narration": "胡三娘冷笑着撩起旗袍下摆。",
"scene_audio": {
    "ambient": ["empty_corridor"],
    "sfx": [{"id": "fabric_swish", "at": 1.0}]
},
"narration_emotion": "cold",
...
"bgm_mood": "mystery",
```

**第 1430 示例(尸体血迹)**:
```python
"narration": "尸体的血迹一直渗到地板。",
"scene_audio": {
    "ambient": ["empty_corridor", "low_drone"],
    "sfx": [{"id": "blood_drip", "at": 2.0}]
},
"narration_emotion": "grave",
...
"bgm_mood": "tension",
```

**第 1446 示例(climax 对白)**:
```python
"narration": "",
"scene_audio": {
    "ambient": ["low_drone"],
    "sfx": [{"id": "glass_shatter", "at": 0.5, "important": true}]
},
"narration_emotion": "neutral",
...
"bgm_mood": "climax",
```

**第 1472 示例(melancholy 收尾)**:
```python
"narration": "",
"scene_audio": {
    "ambient": ["rain_light"],
    "sfx": [{"id": "bell_toll", "at": 3.0}]
},
"narration_emotion": "melancholy",
...
"bgm_mood": "melancholy",
```

### A.3 改 cleaned.append —— 透传新字段

**位置**: long_storyboard.py 第 1965 行(cleaned.append 字典)

**当前**(1965-1982 行):
```python
cleaned.append({
    "source_seg":           raw.get("source_seg", ""),
    ...
    "silent_beat":           raw_silent,
    "visual_ref":            visual_ref,
})
```

**改成**(在 visual_ref 那行**之后**、`})` 之前加 2 个字段):
```python
cleaned.append({
    "source_seg":           raw.get("source_seg", ""),
    ...
    "silent_beat":           raw_silent,
    "visual_ref":            visual_ref,
    # v2.7 新增: 音频工程字段
    "scene_audio":           _validate_scene_audio(raw.get("scene_audio")),
    "narration_emotion":     _validate_emotion(raw.get("narration_emotion", "")),
})
```

### A.4 加 2 个 validator 辅助函数

**位置**: long_storyboard.py 在 `_strip_dangling_residual` 函数后面(2000 行附近),加:

```python
# ────────────────────────────────────────────────────────────────────
# v2.7 scene_audio / narration_emotion validator
# ────────────────────────────────────────────────────────────────────

_VALID_AMBIENT_IDS = {
    "rain_light", "rain_heavy", "wind_howl", "thunder_distant",
    "crickets_night", "temple_interior", "old_house_creak",
    "graveyard_night", "marketplace_chinese", "well_echo",
    "mountain_night", "candle_flame", "empty_corridor",
    "low_drone", "basement_drip",
}

_VALID_SFX_IDS = {
    "thunder_crack", "bell_toll", "small_bell_ring", "glass_shatter",
    "door_creak", "door_slam", "footstep_wood", "footstep_stone",
    "heartbeat_single", "heartbeat_loop", "breath_gasp", "body_fall",
    "paper_rustle", "coin_drop", "blood_drip", "lamp_drop_metal",
    "magic_shimmer", "ghost_whisper", "branch_snap", "wind_gust",
    "fabric_swish", "cloth_tear", "mirror_resonance",
    # transition
    "whoosh", "riser", "sub_drop", "chapter_strike",
}

_VALID_EMOTIONS = {
    "neutral", "whisper", "desperate", "cold", "trembling",
    "eerie", "grave", "urgent",
}


def _validate_scene_audio(sa) -> dict:
    """校验 + 清洗 scene_audio 字段。非法 id 过滤,超出上限截断。"""
    if not isinstance(sa, dict):
        return {"ambient": [], "sfx": []}

    # ambient: 白名单过滤,最多 2 个
    raw_amb = sa.get("ambient") or []
    if not isinstance(raw_amb, list):
        raw_amb = []
    clean_amb = []
    for a in raw_amb[:2]:
        if isinstance(a, str) and a in _VALID_AMBIENT_IDS:
            clean_amb.append(a)

    # sfx: 白名单过滤,最多 2 个
    raw_sfx = sa.get("sfx") or []
    if not isinstance(raw_sfx, list):
        raw_sfx = []
    clean_sfx = []
    for s in raw_sfx[:2]:
        if not isinstance(s, dict):
            continue
        sid = s.get("id", "")
        if sid not in _VALID_SFX_IDS:
            continue
        clean_sfx.append({
            "id":        sid,
            "at":        float(s.get("at", 0.0) or 0.0),
            "important": bool(s.get("important", False)),
        })

    return {"ambient": clean_amb, "sfx": clean_sfx}


def _validate_emotion(emo: str) -> str:
    """校验 emotion 字段,非法的归为 neutral。"""
    if not isinstance(emo, str):
        return "neutral"
    emo = emo.strip().lower()
    return emo if emo in _VALID_EMOTIONS else "neutral"
```

---

## 2. PATCH B: to_pipeline.py 透传 scene_audio

**位置**: to_pipeline.py 第 526 行(`page = { ... }` 字典)

**改动**: 在 `"intended_duration_sec": ...,` 那一行**后面**,`}` **之前**加 2 个字段:

```python
            page = {
                "page":       page_num,
                ...
                "intended_duration_sec": float(shot.get("intended_duration_sec", 0.0) or 0.0),
                # v2.7 新增: 音频工程字段透传
                "scene_audio":       shot.get("scene_audio") or {"ambient": [], "sfx": []},
                "narration_emotion": shot.get("narration_emotion", "neutral"),
            }
```

就这两行。

---

## 3. PATCH C: producer_v2.py 接入 mixer/ambient/sfx

### C.1 在文件顶部 import 处加(约第 30 行)

```python
# v2.7 音频工程
try:
    from core.audio.scene_audio_helper import build_ambient_and_sfx_for_episode
    from core.audio.ambient_continuity import SfxAbsoluteEvent
    _SCENE_AUDIO_AVAILABLE = True
except ImportError as e:
    print(f"[producer] scene_audio 模块不可用: {e}")
    _SCENE_AUDIO_AVAILABLE = False
```

### C.2 改整片合成段 —— 加 ambient 轨 + sfx 叠加

**位置**: producer_v2.py 第 1586-1599 行(BGM 分页混音那段)

**当前**:
```python
        # ── BGM 分页混音（v3 核心）────────────────────────────────
        bgm_track_path = str(tmp / "bgm_full.mp3")
        bgm_ok = False
        if any(_list_bgm_files(m) for m in BGM_VALID_MOODS):
            bgm_ok = self._build_bgm_track(
                page_durations, page_moods, total_dur, bgm_track_path)
        else:
            print(f"\n  [BGM] refs/bgm/ 下无任何文件，跳过 BGM")

        if bgm_ok:
            self._overlay_bgm_track(out_path, bgm_track_path)
        elif self.bgm_path and Path(self.bgm_path).exists():
            self._overlay_bgm_legacy(out_path, self.bgm_path)
```

**改成**(下面整段替换):

```python
        # ── BGM 分页混音 ────────────────────────────────────────
        bgm_track_path = str(tmp / "bgm_full.mp3")
        bgm_ok = False
        if any(_list_bgm_files(m) for m in BGM_VALID_MOODS):
            bgm_ok = self._build_bgm_track(
                page_durations, page_moods, total_dur, bgm_track_path)
        else:
            print(f"\n  [BGM] refs/bgm/ 下无任何文件，跳过 BGM")

        # ── v2.7 Ambient 轨 + SFX 叠加 ───────────────────────────
        ambient_ok = False
        sfx_events = []
        ambient_track_path = str(tmp / "ambient_full.mp3")
        if _SCENE_AUDIO_AVAILABLE:
            try:
                ambient_ok, sfx_events = build_ambient_and_sfx_for_episode(
                    [pc for pc in self.story.pages],   # ← 这里传你的 page_cfg 列表
                    page_durations,
                    ambient_track_path,
                )
            except Exception as e:
                print(f"\n  [scene_audio] 失败,降级跳过: {e}")
                ambient_ok = False
                sfx_events = []

        # 叠加顺序: BGM → Ambient → SFX
        if bgm_ok:
            self._overlay_bgm_track(out_path, bgm_track_path)
        elif self.bgm_path and Path(self.bgm_path).exists():
            self._overlay_bgm_legacy(out_path, self.bgm_path)

        if ambient_ok:
            self._overlay_ambient_track(out_path, ambient_track_path)

        if sfx_events:
            self._overlay_sfx_events(out_path, sfx_events, total_dur, str(tmp))
```

### C.3 在 `_overlay_bgm_track` 函数下面(约 1687 行后)新增两个函数

```python
    def _overlay_ambient_track(self, video_path: str, ambient_path: str):
        """v2.7: 整片叠加 ambient 长音轨(在 BGM 之上)。"""
        tmp_out = video_path + ".amb.mp4"
        cmd = [
            "ffmpeg", "-y", "-i", video_path, "-i", ambient_path,
            "-filter_complex",
            "[0:a][1:a]amix=inputs=2:duration=first:normalize=0[aout]",
            "-map", "0:v", "-map", "[aout]",
            "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
            "-shortest", tmp_out,
        ]
        try:
            subprocess.run(cmd, check=True, capture_output=True)
            Path(tmp_out).replace(video_path)
            print(f"  [ambient] ✓ 叠加完成")
        except subprocess.CalledProcessError as ex:
            print(f"  [ambient] 叠加失败: {ex.stderr.decode()[:200]}")

    def _overlay_sfx_events(self, video_path: str, sfx_events: list,
                              total_dur: float, tmp_dir: str):
        """v2.7: 把所有 sfx 按时刻叠加到视频音轨上。"""
        if not sfx_events:
            return
        # 用 amix + adelay,所有 sfx 拼成一条 sfx_full.mp3
        sfx_track_path = str(Path(tmp_dir) / "sfx_full.mp3")
        n = len(sfx_events)

        inputs = [
            "-f", "lavfi",
            "-t", f"{total_dur}",
            "-i", "anullsrc=channel_layout=stereo:sample_rate=44100",
        ]
        filter_parts = ["[0:a]anull[base]"]
        mix_inputs = ["[base]"]
        for i, ev in enumerate(sfx_events, start=1):
            inputs += ["-i", ev.path]
            delay_ms = max(0, int(ev.at_sec * 1000))
            # 防爆音 fade
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
            print(f"  [sfx] 生成失败,跳过")
            return

        # 叠加到视频
        tmp_out = video_path + ".sfx.mp4"
        cmd = [
            "ffmpeg", "-y", "-i", video_path, "-i", sfx_track_path,
            "-filter_complex",
            "[0:a][1:a]amix=inputs=2:duration=first:normalize=0[aout]",
            "-map", "0:v", "-map", "[aout]",
            "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
            "-shortest", tmp_out,
        ]
        try:
            subprocess.run(cmd, check=True, capture_output=True)
            Path(tmp_out).replace(video_path)
            print(f"  [sfx] ✓ 叠加 {n} 个 SFX 事件")
        except subprocess.CalledProcessError as ex:
            print(f"  [sfx] 叠加失败: {ex.stderr.decode()[:200]}")
```

### C.4 重要: producer 拿到 pages 的方式

**第 C.2 步**里有这一行需要确认:
```python
[pc for pc in self.story.pages],
```

如果你的 producer 用的是 `self.story.pages` 拿 page_cfg 列表,**就照抄**。否则参考 `_build_page_audio` 里 `page_cfg` 是怎么来的,改成对应的源。

---

## 4. PATCH D: coordinator.py 加 scene_audio 校验

**位置**: coordinator.py 第 1200 行附近(`from core.bgm_mood_resolver import` 之后)

在 `resolve_bgm_moods` 调用**之前**加一段:

```python
    # ── v2.7 scene_audio 兜底: 空 ambient 按 bgm_mood 填默认 ──
    from core.audio.ambient_continuity import DEFAULT_AMBIENT_BY_MOOD
    for i, shot in enumerate(shots):
        sa = shot.get("scene_audio") or {"ambient": [], "sfx": []}
        if not sa.get("ambient"):
            mood = shot.get("bgm_mood", "tension")
            default = DEFAULT_AMBIENT_BY_MOOD.get(mood, [])
            if default:
                sa["ambient"] = default
                shot["scene_audio"] = sa
                log.append({
                    "phase":    "post",
                    "shot_idx": i,
                    "type":     "ambient_default_fill",
                    "action":   f"ambient 空 → 按 mood={mood} 填默认 {default}",
                })
```

---

## 5. 端到端跑通验证

### 5.1 不跑流水线,先单测两个新模块

```bash
# 测平滑算法
python core/audio/ambient_continuity.py
# 应输出: 原始 8 页 → 平滑后 3 段

# 测 page → meta 抽取
python -c "
from core.audio.scene_audio_helper import extract_audio_meta_from_pages
pages = [
    {'bgm_mood': 'tension', 'scene_audio': {'ambient': ['rain_heavy'], 'sfx': []}},
    {'bgm_mood': 'climax', 'scene_audio': {'ambient': [], 'sfx': [{'id': 'thunder_crack', 'at': 1.5, 'important': True}]}},
]
print(extract_audio_meta_from_pages(pages, [5.0, 5.0]))
"
```

### 5.2 跑一集小故事(任挑一集)

```bash
python run.py twophase stories/long_xxx.yaml
python run.py produce stories/long_xxx.yaml --platform douyin
```

**关键日志**:
- `[scene_audio] ambient 段=N sfx 事件=M`
- `[ambient] seg X: ...`
- `[sfx] ✓ 叠加 N 个 SFX 事件`

看到这三行 = 接入成功。

### 5.3 听感验证

打开生成的 mp4,听:
- [ ] 整片有持续环境底床(不再静默)
- [ ] 章节切换处 ambient 平滑过渡
- [ ] 雷/钟/玻璃在指定时刻触发
- [ ] 旁白能听清(不被 ambient 盖住)

---

## 6. 回滚

任何一步出问题:
```bash
# 单独关 ambient
# producer_v2.py 第 C.2 步那段把 ambient_ok = False 强制设 False 即可

# 完全回到 v2.6 行为
# 把 _SCENE_AUDIO_AVAILABLE = False 强制设 False
```

---

## 7. 部署顺序(强烈推荐这个顺序)

```
Day 1 上午:
  [ ] 落两个新文件到 core/audio/
  [ ] 跑 5.1 单测确认不挂

Day 1 下午:
  [ ] PATCH B (to_pipeline 透传) - 最简单,2 行
  [ ] PATCH D (coordinator 兜底) - 简单,1 段

Day 2 上午:
  [ ] PATCH A (storyboard 产 scene_audio)
  [ ] 跑一集 storyboard 命令,人工 grep 看 yaml 里有没有 scene_audio 字段

Day 2 下午:
  [ ] PATCH C (producer 接入 mixer/ambient/sfx) - 最大改动
  [ ] 跑 5.2 端到端
  [ ] 听 5.3 验收
```

**不要并行做 PATCH A/B/C/D**。一个一个来,每步跑通再下一步。

---

## 8. 关于 mixer.py

你可能注意到:**这次没用 mixer.py**。

为什么?因为你的 producer **已经有自己的整片合成流程**(`_build_bgm_track + _overlay_bgm_track`)。
mixer.py 是为"从零开始"设计的全栈混音器,**强行套上去要大改 producer**。

折中方案: 我用 mixer.py 的**核心算法**(响度层级、ambient 平滑、SFX 防爆音),
但**沿用你 producer 现有的 BGM 拼接 + overlay 模式**,只新加 ambient 和 sfx 两层。

这样 producer 改动最小,你的现有 BGM 逻辑零回归。
mixer.py 留着,等以后想完全重构整片合成时再用。

---

完。
