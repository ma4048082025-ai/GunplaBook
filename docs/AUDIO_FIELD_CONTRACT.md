# 镜头 / 音频字段契约 v2.5.1

> 这是分镜大师(LLM)、coordinator、to_pipeline、producer、voice_engine 之间的字段约定。
> **任何一方新增或改变字段语义,先改这里,再改代码。** 这是防止"分镜大师产出 producer 不认识的字段"的唯一保障。

---

## 一、镜头的三种"无人声"状态 —— 必须能区分

一个 shot 的 narration 为空,在旧系统里全是 bug。v2.4.1 起,空 narration 有三种**完全不同**的合法语义,靠下面字段区分:

| 状态 | 字段组合 | 含义 | 有独立图? | 有独立时长? | 有 BGM? | 配音? |
|---|---|---|---|---|---|---|
| **正常镜头** | `silent_beat=false` 且 narration 非空 | 标准镜头 | 是 | 跟随音频 | 是 | 旁白/对话 |
| **静默镜头** | `silent_beat=true` | 故意留白,给想象空间 | 是 | `intended_duration_sec` | 是 | 无 |
| **extend hold** | `_hold=true, _hold_type=extend` | 画面不变,旁白并入前镜 | 否(复用前镜图) | 并入前镜 | 是(前镜的) | 无(已并走) |

> **v2.5.1 起,"非法状态"(silent_beat=false 且 narration 空 且非 hold) 不再可能出现** ——
> coordinator 的 `_purge_empty_shots` 在 post_check 末尾会删除这种镜头。
> producer 的 `_build_page_audio` 入口也加了防御:万一漏网,自动按 silent_beat 兜底 2 秒。

判定优先级(producer / to_pipeline 按此顺序检查):
1. `_hold=true` → 走 hold 分支(extend 复用前图 / cutaway 正常生图)
2. 否则 `silent_beat=true` → 静默镜头分支
3. 否则 narration 非空 → 正常镜头
4. (废弃)空 narration + 空 dialogue + 非 hold + 非 silent → 已被 coordinator 删除,不会到达 producer

---

## 二、字段清单:产出方 / 消费方 / 空值语义

### `narration` (str)
- **产出**: 分镜大师 (long_storyboard)
- **消费**: to_pipeline → page.narration → producer → voice_engine(旁白 TTS)
- **空值**: 空字符串。仅在 `silent_beat=true` 或 extend hold 时合法;否则非法。
- **注意**: dialogue.text 不得重复出现在 narration 里(coordinator `_strip_dialogue_from_narration` 负责剥离)。

### `dialogue` (list[{speaker, text}])
- **产出**: 分镜大师
- **消费**: to_pipeline → page.dialogue → producer → voice_engine(角色 TTS)
- **空值**: 空 list。合法。
- **注意**: 与 narration 一起为空 + 非 hold + 非 silent_beat → 非法。

### `silent_beat` (bool) —— **v2.4.1 新增**
- **产出**: 分镜大师。决策依据:剧情需要留白(恐怖停顿、悬念、情绪沉淀)。
- **消费**: to_pipeline 透传 → producer。producer 见 true 则**跳过 TTS**,clip 时长 = `intended_duration_sec`。
- **空值/缺失**: 视为 `false`(向下兼容旧 storyboard)。
- **约束**: `silent_beat=true` 时 `intended_duration_sec` 必须 > 0(coordinator 兜底:缺失则填默认 3.0)。
- **约束**: `silent_beat=true` 时 narration 与 dialogue 都应为空(coordinator 兜底清空)。

### `intended_duration_sec` (float)
- **产出**: 分镜大师
- **消费**: producer
- **语义随 silent_beat 变化**:
  - `silent_beat=false`: **附加**时长 —— 音频播完后画面再静止 N 秒。`clip_dur = max(audio + 0.3 + N, 4.0)`
  - `silent_beat=true`: **全部**时长 —— 没有音频,clip 就是 N 秒。`clip_dur = max(N, 2.0)`
- **空值/缺失/0**: `silent_beat=false` 时等同无附加(零回归);`silent_beat=true` 时由 coordinator 兜底为 3.0。

### `_hold` (bool) / `_hold_type` ("extend" | "cutaway" | "")
- **产出**: 分镜大师,或 coordinator(旁白完全重复时把后镜转 `extend`)
- **消费**: to_pipeline / pipeline / producer
- **语义**:
  - `extend`: 画面**不换**,复用 `_hold_source_page` 的图;旁白并入前镜音频。无独立 clip(motion=hold_skip)。
  - `cutaway`: 画面**换**新图(反应/道具/环境),正常生图、正常评分、独立 clip。
- **空值**: `_hold=false` 时 `_hold_type=""`。未标 type 的旧 hold 默认 `extend`。

### `_audio_dur_estimate` (float) —— **v2.4.1 新增,仅诊断**
- **产出**: coordinator(由 narration 字数 ÷ 语速估算)
- **消费**: 仅打印 / warning,不进入正式流程。
- **用途**: 字数时长护栏。供分镜大师下一轮迭代参考。

---

## 三、时长公式总表(producer)

| 镜头类型 | clip 时长公式 |
|---|---|
| 正常 KB 镜头 | `max(audio_dur + 0.3 + intended_extra, 4.0)` |
| 正常 AI 视频 | `max(video_dur, audio_dur + 0.3 + intended_extra, 4.0)` |
| **静默镜头** | `max(intended_duration_sec, 2.0)` ← **v2.4.1 新增** |
| extend hold | 无独立 clip;音频并入 source_page,source clip 时长按合并后总音频算 |
| cutaway hold | 同正常 KB 镜头 |

语速常数: 中文 TTS ≈ `CHARS_PER_SEC = 4.5` 字/秒(coordinator 估算用,可调)。

---

## 四、字数时长护栏(coordinator)

- 估算: `est_sec = len(narration_去标点) / CHARS_PER_SEC`
- 阈值 `SINGLE_SHOT_SEC_WARN = 8.0`: 单镜旁白预估 > 8 秒 → warning,建议分镜大师拆成 主图 + cutaway。
- 阈值 `EXTEND_CHAIN_SEC_WARN = 12.0`: extend hold 链合并后预估 > 12 秒 → warning,建议中间插一个 cutaway 换图,而非死 hold。
- 护栏**只检测、只 warning,不自动改结构**(拆图/插镜需要理解剧情,是 LLM 的活)。

---

## 五、音频三轨化(v2.5 已实现)

### 5.1 状态

v2.5 起,producer 支持 narration / dialogue / bgm **三轨独立**,
其中:
- 轨3 `bgm`: 一直独立(`_build_bgm_track` + `_overlay_bgm_track`,
  在整片合成阶段叠加,不进 `PageAudioInfo.audio_path`)。
- 轨1 `narration` / 轨2 `dialogue`: 由 feature flag `ENABLE_THREE_TRACK_AUDIO`
  控制。默认 `False`,行为完全等同 v2.4.1 串行单轨,零回归。

### 5.2 开关与策略

`producer_v2.py` 顶部:

```
ENABLE_THREE_TRACK_AUDIO = False   # 总开关
DIALOGUE_TIMING_STRATEGY = "overlap_tail"
  # "sequential"   dialogue 在 narration 后面(amix 实现的串行,听感≈v2.4.1)
  # "overlap_tail" dialogue 提前 DIALOGUE_OVERLAP_SEC 秒交叠 narration 尾部(默认,电影化)
  # "parallel"     dialogue 跟 narration 同时开始(全重叠,适合心声式独白)
DIALOGUE_OVERLAP_SEC = 0.5
AMIX_WEIGHT_NARRATION = 1.0
AMIX_WEIGHT_DIALOGUE  = 1.0
```

### 5.3 路由规则

三轨化只在【既有 narration 又有 dialogue】的页生效。
- 纯 narration(无 dialogue): 走原单轨快路径,无任何变化。
- 纯 dialogue(无 narration): 走原单轨快路径。
- silent_beat / extend hold: 跟开关无关,行为同 v2.4.1。
- hold 链合并(`_concat_audio_infos`): 按 `audio_path` 串接,不区分轨道。
  合并后画面是一张图,人声继续无缝叠,轨道边界已在 amix 时融化。

### 5.4 字段语义

`PageAudioInfo`:
- `audio_path` (str): **最终混音 mp3**(narration + dialogue amix 后)。下游 KB clip / video clip 用这个,不需要改。
- `tracks` (dict): `{"narration": narr.mp3, "dialogue": dlg.mp3}`。
  仅三轨模式下填充,单轨模式为空 dict。供字幕渲染、调试、扩展使用。
- `segments` (list): 字幕段,**按混音后的绝对时间重算**。
  例: dlg_offset=4.5s 时,dialogue 段的 start 会是 4.5+rel_start。
- `duration` (float): `max(narration 结束, dialogue 结束)`。

### 5.5 缓存兼容

`_audio_cache_key` 已纳入 `3trk` / `strat` / `olap` 三个标记。
- 开关切换时,旧缓存(单轨 concat)与新缓存(双轨 amix)各自独立,不互相污染。
- 改 `DIALOGUE_TIMING_STRATEGY` 或 `DIALOGUE_OVERLAP_SEC` 会自动失效相关缓存。

### 5.6 不影响范围

契约 / coordinator / to_pipeline / voice_engine / pipeline 完全不动 ——
这是纯 producer 内部升级。外部调用方仍只需 `producer.make_video(...)`。

