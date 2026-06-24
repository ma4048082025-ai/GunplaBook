# 音频工程部署方略

> 给未来的自己:这是 GunplaBook 从"业余 TTS 配音"升级到"电影级音频"的全路径。
> 不要一次性全做。按 Phase 顺序推进,每完成一个 Phase 自己听一集,再决定下一步。
> 中途任何时候卡住,翻到对应 Phase 的"卡点诊断"段落。

---

## 0. 阅读顺序 / 心态准备

**不要直接跳到代码。** 先把这段读完。

这份方略**不会**让你做完之后立刻变成迪士尼。它会让你的成片从"明显是 AI 生成的"变成"看起来挺像电影的小制作"。差距继续往上推就要真混音师 + 专业声优了,那是另一个量级的投入。

**核心心态三条**:

1. **听完一集再做下一步。** 任何凭想象做的优化都是浪费时间。
2. **每个 Phase 独立可发布。** 即使只做完 Phase 1,你的成品已经比现在好。
3. **遇到不可救的层,跳过去做下一层。** Phase 3 卡住不要影响 Phase 4。

---

## 1. 五层世界(必读基础)

```
L5  BGM         情感方向     ← "该感觉什么"
L4  人声        故事内容     ← "发生了什么"
L3  SFX         此刻动作     ← "这一刻怎么了"
L2  Ambient     世界存在     ← "我们在哪里"
L1  Room Tone   听觉真实     ← (听不见,但少了出戏)
```

**响度层级**(mixer.py 已硬编码):

```
L4 voice         0 dB   基准
L3 sfx_key      -3 dB   重要 SFX
L3 sfx_default  -6 dB   普通 SFX
L5 bgm         -15 dB
L2 ambient     -22 dB
L1 room_tone   -40 dB
```

**五条混音铁律**:
1. 人声永远最响
2. 音量层级有固定关系(见上)
3. 人声开口时 BGM 自动让步(ducking)
4. 突发 SFX 不超过 -3 dB(削顶)
5. 整集 loudnorm -14 LUFS

**这五条铁律 mixer.py 已经全部实现**。你不用从零写,只需要把素材按 MixInput 传进去。

---

## 2. Phase 路线图(总览)

| Phase | 主题 | 工作量 | 听感提升 | 必做? |
|---|---|---|---|---|
| 1 | 人声升级 | 1 天 | ⭐⭐⭐⭐⭐ | ✅ 必做 |
| 2 | 拼音修复彻底落地 | 半天 | ⭐⭐ | ✅ 必做 |
| 3 | mixer 接入 producer | 1 天 | ⭐⭐⭐⭐ | ✅ 必做 |
| 4 | SFX 词表 + 分镜大师契约 | 2 天 | ⭐⭐⭐⭐ | ✅ 必做 |
| 5 | Ambient 全片自动垫底 | 半天 | ⭐⭐⭐ | ✅ 必做 |
| 6 | BGM 词表扩到 7 类 | 1 天 | ⭐⭐⭐ | 推荐 |
| 7 | 人声空间感(简易混响) | 半天 | ⭐⭐⭐ | 推荐 |
| 8 | Room Tone 自动垫底 | 半天 | ⭐⭐ | 可选 |
| 9 | 评估 CosyVoice 2 替换 SoVITS | 2-3 天 | ⭐⭐⭐⭐ | 后期再评估 |

**总工程量**:必做项 5 天 + 推荐项 1.5 天 = **6.5 天** 能把音频从"业余"做到"准电影级"。

按周末晚上推进可行,**不需要请假专心搞**。

---

## 3. Phase 1:人声升级(最该先做)

**为什么先做这个**:听感差距最大,因为**人声是观众 80% 注意力所在**。换参考音之后你会"听到"音频工程真正在变。

### 步骤 1.1 换参考音(2 小时)

去 B 站搜你喜欢的鬼故事/有声书 UP 主,**截真人录音**,代替你现在的 Edge TTS 生成的样本。

操作:
- 每个角色 1 段,5-10 秒,**带感情的**
- 用 Audacity 切片,保存为 22050 Hz / 16-bit / 单声道 wav
- 文件命名仍按 voice_library.yaml 里的 key
- 替换 `refs/voices/library/*.wav`(覆盖原 Edge TTS 样本)
- **保持 .txt 同步**:每个 wav 旁边要有同名 .txt 写参考音的文本内容,SoVITS 需要

**截哪种内容**:
- 选 **平静叙述** 而非高亢戏剧化的片段(SoVITS 复制能量曲线,平静参考音更稳)
- 避免有背景音乐的片段(SoVITS 会学到 BGM,污染输出)
- 避免咳嗽/笑声/方言重音的片段

**测试**:
- SFTP 上传新参考音到 Win10 (`H:/voice_refs/library/`)
- 跑一个简单 SoVITS 请求,听输出
- 不满意就再截一段,重复

### 步骤 1.2 给分镜大师加情绪标签(可后置)

这一步**不急**。等 Phase 1.1 跑了一集听完,如果觉得"还是太平淡",再做这步。

storyboard.py 的输出加 `emotion` 字段(给每条 dialogue 和 narration),voice_engine.py 调 ffmpeg filter 做"情绪化妆"。

参考 `refs/audio/emotion_filters.yaml`(之前给你的)。

### 卡点诊断

- **SoVITS 输出还是平淡** → 参考音不够带感情。再去截一段更带情绪的。
- **SoVITS 输出抖音/失真** → 参考音质量差(采样率不对/带背景音)。重新截。
- **SoVITS 输出方言** → 参考音 UP 主有口音。换 UP 主。

---

## 4. Phase 2:拼音修复彻底落地(半天)

**目标**:解决"血泊" 等多音字读错。

### 步骤 2.1 部署 pronunciation_fix v2

- 用之前发的 `pronunciation_fix_v2.py`,覆盖原 `core/pronunciation_fix.py`
- 改 voice_engine.py:第 209 行删掉,在 218 行前加 `text = fix_pronunciation(text, engine="gpt_sovits")`,在 221 行前加 `text = fix_pronunciation(text, engine="edge_tts")`

### 步骤 2.2 验证 SoVITS 支持的拼音注入语法

不同 SoVITS fork 接受的语法不同,先在 Win10 上手动测:

```
输入A: 血泊
输入B: 血{xue4}泊
输入C: 血[xue4]泊
输入D: 血xue4泊
```

听哪个对了,把 `_FIXES` 里的 sovits 字段统一改成那个格式。

### 步骤 2.3 积累词典

跑一集,把所有听错的字记下来,每次加 3-5 条到 `_FIXES`。**两周后词典基本稳定**,后面只是偶尔补。

### 卡点诊断

- **edge_tts 路径仍读错** → 检查你的 voice_engine 里旁白(character=None)是不是真的走到了 _generate_edge。打印一下分支
- **sovits 拼音注入无效** → 步骤 2.2 没做,或者你的 SoVITS 不支持任何注入语法。Plan B:在 SoVITS G2P 词典里直接加 `血泊 -> xue4 po1`

---

## 5. Phase 3:mixer 接入 producer(1 天)

**目标**:把 `core/audio/mixer.py` 真正用起来,替换 producer 里现有的 BGM 叠加逻辑。

**为什么这个时间点做**:Phase 1+2 之后,人声本身可用了。这时候上 mixer 才有意义——否则垃圾进垃圾出。

### 步骤 3.1 让 producer 调 mixer(单页验证)

最小改动:在 producer 整片合成最后一步(`_overlay_bgm_track` 那里)**改用 mixer**。

具体怎么改:
- producer 已经能产出每页的 voice mp3(narration + dialogue amix 后)
- producer 已经能产出整片 voice 时间轴
- 改动:整片 voice 拼好后,**不要直接叠 BGM**,而是构造 MixInput,调 `mixer.mix(spec, final_path)`

第一版只用 voice + bgm 两层,验证 mixer 接入正常。

### 步骤 3.2 Ducking 调参

mixer 默认开启 ducking。听一集,如果觉得:
- **BGM 让位太狠** → 改 mixer.py 顶部 `DUCK_RATIO = 4.0`(从 6 降到 4)
- **BGM 让位不够** → `DUCK_RATIO = 8.0`
- **BGM 升回来太快/太突兀** → `DUCK_RELEASE_MS = 600`(慢一点)

### 步骤 3.3 loudnorm 影响检查

mixer 默认开 loudnorm 到 -14 LUFS。这会让所有镜头响度一致,但**第一次跑可能整体响度感觉变小**(因为 -14 是 YouTube 标准,比你之前的"未标准化"低)。

听完不要急着调,**先上传 YouTube 看后台的响度计量**。如果显示 -14 LUFS 附近,就是对的。

### 卡点诊断

- **mixer 跑很慢** → 正常,loudnorm 是两阶段处理 + 整片 ffmpeg。一集 5-10 分钟接受。如果觉得开发期太慢,临时 `enable_loudnorm=False`,只在发布前打开。
- **ducking 把人声也压了** → DUCK_THRESHOLD 太低。改成 0.1 试试。
- **某些镜头响度突然爆** → 你的 SFX 太响。检查 sfx 的 volume_db,不要超过 -3。

---

## 6. Phase 4:SFX 词表 + 分镜大师契约(2 天)

**目标**:让 L3 SFX 真正用起来。

### 步骤 4.1 下素材(半天)

按之前给的 `refs/sfx/registry.yaml` 去 freesound 下文件:
- ambient 子目录 15 个 × 3 文件 = 45 个
- stinger 子目录 23 个 × 2 文件 = 46 个
- transition 子目录 4 个 × 2 文件 = 8 个

**纪律**:
- 只下 CC0,不下 CC-BY(省 attribution 麻烦)
- wav 优先,mp3 也行
- 不要超过 50 个 stinger,**词表必须封闭**

### 步骤 4.2 改分镜大师 prompt(1 天)

在 `tools/long_writer/long_storyboard.py` 的 prompt 里加铁律 22(声画自洽) + scene_audio 字段说明 + sfx_menu(从 registry.yaml 提取出来的 id 列表)。

参考之前讨论过的字段格式:

```yaml
scene_audio:
  ambient: [rain_heavy, temple_interior]
  sfx:
    - id: lamp_drop_metal
      at: 4.5
      important: true
```

### 步骤 4.3 改 coordinator 校验(2 小时)

加 `_validate_scene_audio`,做硬规则校验:
- sfx_id ∈ registry → 否则删
- 每镜 sfx ≤ 2 → 否则截
- 整章 sfx ≤ 30 → 否则 warning

### 步骤 4.4 改 to_pipeline + producer 接通(2 小时)

- to_pipeline 透传 scene_audio 字段到 page
- producer 在整片 mixer 调用前,把所有 page 的 sfx 转成 `SfxEvent` 列表(at 时刻用 page 起始时刻 + 镜头内 at 偏移)

### 卡点诊断

- **分镜大师写出词表外 sfx** → coordinator 删并 warning,LLM 下次会学。如果反复犯,在 prompt 加更明确示例。
- **某个 sfx 太响** → 调 registry.yaml 里那条的 volume(全局生效),或 storyboard 里单镜覆盖。
- **sfx 触发时机不准** → 检查 at 是不是写错。看 producer 日志的 `[sfx]` 行。

---

## 7. Phase 5:Ambient 全片自动垫底(半天)

**目标**:每页都有 L2 ambient,即使分镜大师没写。

### 步骤 5.1 default_ambient_by_mood 自动填

coordinator 末尾加一条 auto_fix:
- 如果 page.scene_audio.ambient 是空 → 按 bgm_mood 从 registry.default_ambient_by_mood 随机选一个填进去

这样老 yaml 跑也能自动有 ambient。

### 步骤 5.2 producer 整片 ambient 轨

producer 在调 mixer 前,把每页的 ambient 拼成整片长 mp3(类似 _build_bgm_track):
- 连续同一 ambient 的页 → 合并段
- 段间交叉淡化 1.0 秒
- 总长 = 整片时长

调 mixer 时 `ambient_path` = 这个长 mp3。

### 步骤 5.3 听感调参

跑一集,如果觉得:
- ambient 太满 → 改 mixer.py 顶部 `LEVEL_AMBIENT = -25.0`(更低)
- ambient 听不见 → `LEVEL_AMBIENT = -19.0`

### 卡点诊断

- **页与页之间 ambient 切换突兀** → 交叉淡化时间太短。改成 1.5-2.0 秒。
- **某些 mood 没有默认 ambient** → 补 registry.yaml 里的 default_ambient_by_mood。

---

## 8. Phase 6:BGM 词表扩到 7 类(1 天)

**目标**:把 BGM 从 3 mood 扩到 7 类功能。

### 步骤 6.1 重构 refs/bgm/ 目录

```
refs/bgm/
├── ambient_dark/       ← 原 tension/ 大部分
├── tension_build/      ← 原 tension/ 推进型
├── climax_impact/      ← 原 climax/
├── melancholy_solo/    ← 原 melancholy/
├── mystery_explore/    ← 新增
├── serene_warm/        ← 新增
└── epic_majestic/      ← 新增
```

### 步骤 6.2 兼容层

`_list_bgm_files(mood)` 里加映射:
```python
LEGACY_MOOD_MAP = {
  "tension":    "ambient_dark",
  "climax":     "climax_impact",
  "melancholy": "melancholy_solo",
}
```

老 yaml 跑不挂。

### 步骤 6.3 分镜大师 prompt 更新

bgm_mood 合法值从 3 变 7,prompt 里举例每种用法。

### 卡点诊断

- **新 mood 没素材** → 临时让 LLM 不选这个 mood,等下完素材再开。
- **bgm 切换太频繁** → 在 coordinator 加 warning:连续 3 个 page 的 mood 不同。

---

## 9. Phase 7:人声空间感(半天)

**目标**:让旁白不像"贴脸广播",而像"有空间的叙述者"。

### 步骤 7.1 极简室内混响

在 voice_engine 输出 mp3 后,加一道:

```bash
ffmpeg -i in.mp3 -af "aecho=0.8:0.5:50:0.3" out.mp3
```

参数:50ms 早期反射 + 0.3 衰减 = 中等房间。

### 步骤 7.2 按场景调

可以根据 page 的场景类型选不同混响:
- 室内 → `aecho=0.8:0.5:50:0.3`
- 室外 → 不加(室外没回响)
- 大空间(教堂/山洞) → `aecho=0.8:0.5:120:0.5`

这一步是**可选的微调**。第一版统一加 50ms 室内混响即可。

### 卡点诊断

- **加完混响声音变浑** → 混响太重。降到 `aecho=0.6:0.4:40:0.2`。

---

## 10. Phase 8:Room Tone(半天,可选)

**目标**:L1 真实感。听不见,但少了出戏。

### 步骤 8.1 准备素材

去 freesound 下 5 段 60 秒长的:
- `indoor_quiet_room.wav`
- `outdoor_quiet_night.wav`
- `supernatural_drone.wav`
- ...

### 步骤 8.2 自动选

mixer 调用前,按整集的主场景类型选一个 room_tone_path。

这一步**99% 观众察觉不到**,但少了它你的成品就是"差一口气"。**做完前 7 个 phase 再说**。

---

## 11. Phase 9:CosyVoice 2 评估(后期)

**什么时候考虑**:Phase 1-8 全做完,听完 5 集,仍觉得"人声是天花板",才考虑。

**为什么不一开始就上**:CosyVoice 2 部署比 SoVITS 重(更大模型、更新依赖),先把音频工程其他层做完,人声层用 SoVITS 顶住,等其他层都到位再升人声。

**评估方法**:
- 本地部署 CosyVoice 2(3B)
- 用同一段文本对比 SoVITS vs CosyVoice 2 输出
- 如果差距明显,做迁移;不明显就不动

---

## 12. 关键纪律(每次推进前重读)

1. **每个 Phase 必须独立可发布**。Phase 3 卡了,Phase 4 不能开始。但 Phase 1 做完已经能发,Phase 2 做完更能发。

2. **配置集中**。所有"魔数"集中在 mixer.py 顶部的常量段。**不要散落在代码里**。

3. **不接 API**。整个方略全本地。CosyVoice 2 也是本地。哪天非要接付费 API,先停下来想清楚是不是真的必要。

4. **听完一集再做下一步**。任何凭想象的优化都是浪费。

5. **不要在分镜大师里硬塞太多约束**。如果某条规则让 LLM 频繁出错,**先松绑规则,再调 prompt**。LLM 不是无限能力的。

6. **mixer 是音频工程的灵魂**。所有素材进去,所有质量出来,都在它这一关。**它的代码不要随便改**,改之前先做单元测试(用合成素材验证响度层级)。

---

## 13. 验收清单(每个 Phase 完成时跑)

每完成一个 Phase,跑这个清单:

- [ ] 整集成片能正常播放,无破音、无静音段、无明显爆音
- [ ] 旁白每一句都能听清(说明 ducking + 层级正确)
- [ ] BGM 不抢戏(说明 ducking 工作)
- [ ] 整集响度均匀(YouTube 上传后看后台响度计)
- [ ] SFX 触发时机和画面动作对齐(误差 < 1 秒)
- [ ] 章节切换处不突兀(BGM 渐入渐出,ambient 交叉淡化)

不达标 → 不要推进下一 Phase,先修这个。

---

## 14. 紧急回滚方案

如果某个 Phase 上线后整集音频反而变差:

1. **立刻关掉对应功能**:
   - mixer ducking 太狠 → `enable_ducking=False`
   - loudnorm 影响听感 → `enable_loudnorm=False`
   - sfx 全爆 → coordinator 加 `max_sfx_per_chapter=0` 临时禁用
2. **回到上个 Phase 的代码版本**(git revert)
3. **重新评估改动**,不要在错误方向上加码

---

## 15. 最后一句话

整个方略的成功标准只有一个:**有人听完一集,问你"这是 AI 做的吗?"**

如果他问了,说明你做到了 80%。
如果他不问,说明你做到了 95%。
做到 100% 的人,会被请去做电影。

不必追求 100%。**做到 90% 的鬼故事频道在中文 YouTube 已经第一梯队**。

加油。
