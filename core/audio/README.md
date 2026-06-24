# core/audio ── 音频工程

这个目录把"声音"相关的事情从 `producer_v2.py` 抽离,自成一个子系统。
半年后回来看代码,直接读这一份 README 就能上手。

---

## 1. 这里管什么

把视频"听起来像电影"的全部活儿:

| 层次 | 谁负责 | 文件 |
|---|---|---|
| **1. 人声**(旁白 + 角色对话 + 情绪化妆) | voice_engine + voice_emotion | `voice_engine.py` / `voice_emotion.py` |
| **2. BGM**(按 bgm_mood 分段铺音乐) | bgm_builder | `bgm_builder.py` |
| **3. Ambient**(按 scene_audio.ambient 铺环境底床) | ambient_builder | `ambient_builder.py` |
| **4. SFX**(按 scene_audio.sfx 精确触发音效) | sfx_builder | `sfx_builder.py` |
| **5. 混音**(把上面 4 层叠到一起) | mixer | `mixer.py` |

不管视频画面、不管 storyboard 字段产出、不管 reviewer。

---

## 2. 输入 / 输出

### 上游给我什么

`pages` 列表(每个 page 是 to_pipeline 产出的字典),关键字段:

```python
{
  "page": 5,
  "narration": "暴雨砸在彩绘玻璃上...",
  "narration_emotion": "eerie",          # v2.6 新增,默认 ""
  "dialogue": [
    {"speaker": "裴时砂", "text": "...", "emotion": "trembling"}
  ],
  "bgm_mood": "tension",
  "scene_audio": {                       # v2.6 新增,默认 {}
    "ambient": ["rain_heavy", "temple_interior"],
    "sfx": [
      {"id": "lamp_drop_metal", "at": 4.5, "volume": 0.7}
    ]
  },
  "silent_beat": false,
  "intended_duration_sec": 0.0,
  "_hold": false,
}
```

### 我给下游什么

一个最终混音过的 mp3,加每页 audio_info(下游字幕渲染要用):

```python
PageAudioInfo(
  audio_path = "tmp/page_05_final.mp3",  # narration+dialogue 已 amix
  duration   = 6.4,
  segments   = [SubtitleSegment(...), ...],
  tracks     = {"narration": "...", "dialogue": "..."},
)
```

整片合成时,producer 在 video 之上叠:bgm 长音轨 + ambient 长音轨 + sfx 长音轨。

---

## 3. 五层叠加顺序(关键)

```
  最终视频音轨 = video内嵌人声(narration+dialogue 已 amix)
              + BGM 长音轨        volume=0.18
              + Ambient 长音轨    volume=0.06~0.15
              + SFX 长音轨        volume=每条独立 0.3~0.8
```

混音公式上,**人声永远在最上**。BGM/Ambient/SFX 音量加起来不能盖过人声,
否则旁白听不清就完蛋。

调试时听感不对的优先级排查:
1. ambient 是不是叠太多 → 看 page.scene_audio.ambient 长度是否超过 2
2. BGM 是不是太响 → 调 `BGM_VOLUME`
3. 旁白是不是用了重 filter 被压低 → emotion 改 light 档

---

## 4. 配置文件

### `refs/sfx/registry.yaml`
封闭词表。**分镜大师只能从这里点菜**。
- ambient / stinger / transition 三大类
- 每个 sfx_id 对应 `refs/sfx/{category}/{id}/*.wav`
- 加新音效:放文件 → 加 entry → 重启流程

### `refs/audio/emotion_filters.yaml`
情绪滤镜定义。
- 8 种 emotion:neutral / whisper / desperate / cold / trembling / eerie / grave / urgent
- 每种对应一行 ffmpeg afilter 字符串
- 加新情绪:加 entry → 改分镜大师 prompt 加 enum

### `refs/voice_library.yaml`(已有,不动)
角色音色身份。**和 emotion 是两层**:
- 身份层:`ref_id`(angular_male / sorrowful_female / sinister_male...)
- 情绪层:`emotion`(给身份戴的滤镜面具)

---

## 5. 各模块状态

| 模块 | 状态 | 备注 |
|---|---|---|
| `contract.py` | ⏳ 待建 | dataclass: PageAudio / SceneAudio / SfxEvent |
| `voice_engine.py` | ⏳ 从 core/ 搬,加 emotion 参数 | 改动小 |
| `voice_emotion.py` | ⏳ 待建 | 读 emotion_filters.yaml,跑 ffmpeg |
| `bgm_builder.py` | ⏳ 从 producer_v2._build_bgm_track 抽出 | 几乎原样 |
| `ambient_builder.py` | ⏳ 待建 | 仿 bgm_builder,音量更低 |
| `sfx_builder.py` | ⏳ 待建 | 按 at 时刻 adelay + amix |
| `mixer.py` | ⏳ 待建 | 整片三层 overlay |

---

## 6. 测试入口

每个 builder 设计成可独立跑:

```bash
# 试一个 ambient 段
python -m core.audio.ambient_builder \
    --ambient rain_heavy --duration 10 --out /tmp/test_amb.mp3

# 试一个 sfx 触发
python -m core.audio.sfx_builder \
    --id thunder_crack --at 2.0 --total 5.0 --out /tmp/test_sfx.mp3

# 试一段 emotion 化妆
python -m core.audio.voice_emotion \
    --in narration.mp3 --emotion whisper --out narration_whisper.mp3

# 试一页完整混音
python -m core.audio.mixer page_05.json /tmp/page_05_final.mp3
```

---

## 7. 给未来自己的话

- **不要把音频字段塞回 producer_v2.py**。所有 audio 决策路过这个目录。
- **不要让 producer 做 NLP**。不要从 narration 文本里猜 sfx,让分镜大师标。
- **音量是配出来的不是算出来的**。registry.yaml 里的 volume 是起点,不是真理。听完一集再回来调。
- **加新 sfx 不要超过 80 个**。聊斋题材 50 个就够。再多 LLM 会迷失,coordinator 会卡。
- **emotion 加到 10 种封顶**。再多分镜大师选不准。
- **ambient 永远比 BGM 轻**。它是"空气",不是"音乐"。

---

## 8. 字段契约

完整字段定义见 `core/AUDIO_FIELD_CONTRACT.md`(根版本)。
本目录是契约的**消费侧实现**,以契约为准。契约改了,代码必跟。
