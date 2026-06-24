# 完整部署清单(本周可落完)

> 把之前所有给过的东西、和这次给的所有东西,**按顺序**列清楚。
> 照着勾选,**不需要任何判断**,跟修 bug 一样一项项关掉。

---

## 总览

| 阶段 | 文件 | 类型 | 耗时 |
|---|---|---|---|
| A. 整理素材库 | `tools/audio_downloader.py` | 新增 | 已给 |
| B. 改 BGM 决策 | `core/bgm_mood_resolver.py` | 新增 | 已给 |
| B. 改 BGM 决策 | `tools/long_writer/long_storyboard.py` | 改 3 处 | 半小时 |
| B. 改 BGM 决策 | `tools/long_writer/coordinator.py` | 加 1 处调用 | 10 分钟 |
| C. 改去重 | `core/narration_dedup_v2.py` | 新增 | 已给 |
| C. 改去重 | `tools/long_writer/coordinator.py` | 加 1 处调用 | 10 分钟 |
| D. 改旁白 TTS | `refs/voice_library.yaml` | 改 2 行 | 1 分钟 |
| D. 改旁白 TTS | 验证 SoVITS 拼音语法 | 手动测 | 15 分钟 |
| E. 下素材 | freesound + YouTube | 跑脚本 | 1-3 天分散做 |
| F. 接 mixer | `core/audio/mixer.py` | 已给 | 等 E 做完再做 |

---

## 阶段 A. 整理素材库脚本(10 分钟)

### A.1 把 audio_downloader.py 放到 tools/

```bash
cp ~/Downloads/audio_downloader.py demo20260510/tools/
```

### A.2 跑一次 check

```bash
cd demo20260510
python -m tools.audio_downloader --check
```

会告诉你目前素材状态。**预期所有都缺**。

### A.3 跑一次 priority

```bash
python -m tools.audio_downloader --priority
```

会列出 10 个优先级最高的 SFX 下载任务。**先放着,A 阶段做完就行,具体下载留到 E**。

---

## 阶段 B. 修复 BGM 一刀切 tension(1 小时)

### B.1 把 bgm_mood_resolver.py 放到 core/

```bash
cp ~/Downloads/bgm_mood_resolver.py demo20260510/core/
```

### B.2 改 `tools/long_writer/long_storyboard.py` 第 51 行

**找到**(用 IDE 搜索 "VALID_BGM_MOODS"):

```python
VALID_BGM_MOODS = ("tension", "climax", "melancholy")
```

**改成**:

```python
VALID_BGM_MOODS = (
    "tension", "climax", "melancholy",
    "playful", "warm", "mystery", "epic", "serene",
)
```

### B.3 改 `tools/long_writer/long_storyboard.py` 第 1716 行

**找到**(用 IDE 搜索 "bgm = raw.get"):

```python
bgm = raw.get("bgm_mood", "tension")
if bgm not in VALID_BGM_MOODS:
    bgm = "tension"
```

**改成**(暂时保留 fallback tension,因为 resolver 会在 coordinator 重判):

```python
bgm = raw.get("bgm_mood", "tension")
if bgm not in VALID_BGM_MOODS:
    bgm = "tension"   # 临时 fallback,coordinator 会按规则重判
```

### B.4 改 `tools/long_writer/long_storyboard.py` 的示例镜头(可选,推荐)

**找到第 1374、1390、1416、1433、1459、1485 行附近** —— 都是 `"bgm_mood": "tension"`。

**让示例多样化**:

| 行号附近 | 改成 |
|---|---|
| 1374 | `"bgm_mood": "serene"` (开场介绍镜) |
| 1390 | `"bgm_mood": "tension"` (保留) |
| 1416 | `"bgm_mood": "mystery"` (探索/线索镜) |
| 1433 | `"bgm_mood": "tension"` (保留) |
| 1459 | `"bgm_mood": "climax"` (高潮镜) |
| 1485 | `"bgm_mood": "melancholy"` (尾声/抒情镜) |

这样 LLM 看到多样化示例,**更容易选合适的 mood**。

### B.5 改 `tools/long_writer/coordinator.py` —— 接入 resolver

**找到 post_check 函数**(用 IDE 搜索 `def post_check`),在末尾(_purge_empty_shots 调用之后)加:

```python
# v2.7 新增: BGM mood 全局裁决
from core.bgm_mood_resolver import resolve_bgm_moods

bgm_fixes = resolve_bgm_moods(
    shots, story_meta, chapter,
    theme_name=story_meta.get("theme", "")
)
for f in bgm_fixes:
    log.append({
        "phase":    "post",
        "shot_idx": f["shot_idx"],
        "type":     "bgm_mood_resolve",
        "action":   f"bgm_mood: {f['before']} → {f['after']} ({f['reason']})",
    })
```

(`story_meta` 和 `chapter` 参数:你 coordinator 应该已经能拿到,具体怎么传看现有签名。)

### B.6 验证

跑一集儿童剧:

```bash
python -m tools.long_writer.cli storyboard scripts/<某个儿童剧>_segments.yaml
```

打开输出 yaml,搜 `bgm_mood:`,应该看到多种 mood,**不再全是 tension**。

也可以单独跑 resolver 看效果(不改文件):

```bash
python core/bgm_mood_resolver.py scripts/<某个 yaml>
```

---

## 阶段 C. 修复旁白重复(1 小时)

### C.1 把 narration_dedup_v2.py 放到 core/

```bash
cp ~/Downloads/narration_dedup_v2.py demo20260510/core/
```

### C.2 改 `tools/long_writer/coordinator.py` —— 接入 dedup_v2

**找到 post_check 末尾**(在 B.5 加的 bgm_mood_resolve 调用之**前**),加:

```python
# v2.7 新增: 旁白去重增强
from narration_dedup_v2 import run_dedup_v2

dedup_v2_result = run_dedup_v2(shots)
for f in dedup_v2_result["narr_dlg_overlap"]:
    log.append({
        "phase": "post",
        "shot_idx": f["shot_idx"],
        "type": "narration_dialogue_overlap",
        "action": f["reason"],
    })
for f in dedup_v2_result["window_wide_dedup"]:
    log.append({
        "phase": "post",
        "shot_idx": f["shot_idx"],
        "type": f["type"],
        "action": f["reason"],
    })
for f in dedup_v2_result["purge_empty"]:
    log.append({
        "phase": "post",
        "shot_idx": f["shot_idx"],
        "type": "purge_empty_after_dedup",
        "action": f["reason"],
    })
```

### C.3 验证

跑一集**已经有旁白重复问题**的故事(比如沈小石那一集),看输出 yaml。
搜 `narration:`,前后对比:
- 重复的 narration 被剥离或清空
- 全空镜被删除
- 重复严重的镜转成 extend hold

---

## 阶段 D. 旁白走 sovits(20 分钟)

跟着 `voice_engine_patch_guide.md` 走。**两步**:

### D.1 改 voice_library.yaml

把 `narrator` 配置从 `engine: edge_tts` 改成 `engine: gpt_sovits` + `ref_id: narrator_male_storyteller`。

### D.2 验证 SoVITS 拼音语法

在 Win10 上手动测 `血泊` vs `血{xue4}泊` 哪个念对。
如果 `{xue4}` 不对,改试 `[xue4]` / `xue4` / 词典。

(详见 voice_engine_patch_guide.md)

---

## 阶段 E. 下载素材(分散 1-2 周做)

### E.1 优先级 SFX 10 个(30 分钟)

跑:
```bash
python -m tools.audio_downloader --priority
```

按它列出的顺序,一个个下:
- 点链接 → 选第一个 CC0 → 下载 → 改名 → 放对应文件夹

每个 3 分钟。**周末抽 30 分钟搞定**。

### E.2 BGM 8 类(1.5 小时)

跑:
```bash
python -m tools.audio_downloader --bgm
```

登 YouTube Audio Library,每类下 3-5 首。**周末抽 1.5 小时搞定**。

### E.3 剩余 SFX(分多次)

跑:
```bash
python -m tools.audio_downloader
```

一次下 5-10 个,**当休息时娱乐做**。一两周内补齐。

### E.4 (可选)参考音

按 voice_engine_patch_guide.md 提到的方法,B 站截 11 段真人参考音。
**可以无限期搁置**,Edge TTS 生成的参考音也能用。

---

## 阶段 F. 接 mixer(等 E 完成 80%)

把 `core/audio/mixer.py` 接入 producer 的整片合成。
详见之前给的 `AUDIO_DEPLOYMENT_ROADMAP.md` 第 5 节(Phase 3)。

**不急**,F 阶段是后期,等素材库基本到位再做。

---

## 验收清单

每完成一个阶段,勾这一项:

- [ ] **A**:`python -m tools.audio_downloader --check` 能跑,显示进度
- [ ] **B**:跑儿童剧 yaml,bgm_mood 不再全 tension
- [ ] **C**:跑沈小石 yaml,旁白重复明显减少
- [ ] **D**:跑包含"血泊"的故事,旁白念对了 + 字幕一致
- [ ] **E**:`--check` 显示 SFX 进度 >= 50%, BGM 进度 100%
- [ ] **F**:mixer 接入 producer,跑一集整片,响度均匀、BGM 不抢戏

---

## 关键纪律

1. **每个阶段独立可发布**。卡在某阶段不影响其他阶段已完成的部分。
2. **改前备份**。改任何文件之前 `cp file.py file.py.backupYYYYMMDD`。
3. **A → B → C → D 顺序做**,不要跳。
4. **E 可以穿插任意时刻做**(下素材是后台任务)。
5. **F 等 E 完成 80% 再做**。
6. **不顺利就回滚**。每个改动都是 git 一次 commit,出问题 revert 即可。

---

## 完整文件下载清单

部署前确认你手上有这些文件(都是之前对话中给过的):

| 文件 | 放到 | 状态 |
|---|---|---|
| `audio_downloader.py` | `tools/` | 这次给 |
| `bgm_mood_resolver.py` | `core/` | 这次给 |
| `narration_dedup_v2.py` | `core/` | 这次给 |
| `voice_engine_patch_guide.md` | 任意位置参考用 | 这次给 |
| `mixer.py` | `core/audio/` | 之前给 |
| `registry.yaml` | `refs/sfx/` | 之前给 |
| `emotion_filters.yaml` | `refs/audio/` | 之前给 |
| `pronunciation_fix.py v2` | `core/` | 之前给 |
| `AUDIO_DEPLOYMENT_ROADMAP.md` | 项目根 | 之前给 |
| `README.md` (audio) | `core/audio/` | 之前给 |
| `__init__.py` (audio) | `core/audio/` | 之前给 |

如果某份没有,翻之前对话回去找。

---

## 一句话

**本周末 4-5 小时**(B+C+D+部分 E),把音频从"业余 TTS 配音"做到"有混音工程的样子"。
剩下的素材积累和 mixer 接入可以**慢慢做**,**任何时候都可以打住**,
工程**任何阶段都是可发布的**。
