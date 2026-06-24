# 旁白走 GPT-SoVITS 部署指南

> 解决问题:edge_tts 用同义词改字会让字幕和发音不一致(写"血泊"念"血滩")。
> 方案:让旁白也走 GPT-SoVITS,sovits 支持拼音夹注语法,
>      字幕保持"血泊"原文,SoVITS 接收"血{xue4}泊"念对正确读音。

---

## 一、改动总览

总共**两处**改动,加一次**手动验证**:

1. ✏️ 改 `refs/voice_library.yaml` —— `narrator` 默认配置(2 行)
2. ✅ 跑手动验证 —— 测 SoVITS 支持哪种拼音语法
3. ✏️ 改 `core/pronunciation_fix.py` —— 把 sovits 字段改成正确语法

---

## 二、改动 1:`refs/voice_library.yaml`

找到 `narrator` 这个角色配置(如果有的话)。可能长这样:

```yaml
narrator:
  _role: narrator
  voice:
    engine: edge_tts                   # ← 改这一行
    voice_id: zh-CN-YunjianNeural      # ← 改这一行(改成 ref_id)
```

改成:

```yaml
narrator:
  _role: narrator
  voice:
    engine: gpt_sovits                              # ← edge_tts → gpt_sovits
    ref_id: narrator_male_storyteller               # ← 用现成的库引用
```

**如果你想要不同的旁白音色**(比如悲剧用沉重男声),还可以改:

```yaml
narrator:
  _role: narrator
  voice:
    engine: gpt_sovits
    ref_id: narrator_male_grave         # 沉重男声
    # 或:
    # ref_id: narrator_female_calm      # 女声平静叙事
    # ref_id: narrator_male_storyteller # 说书人风格(默认)
```

这三个 `ref_id` 在你的 `voice_library.yaml` 底部都已经定义好了,直接用。

---

## 三、改动 2:手动验证 SoVITS 拼音语法

**这一步必须做**,因为不同 SoVITS fork 接受的拼音注入语法不同。

### 在 Win10 上手动测

1. 打开 SoVITS 的 WebUI 或直接调 API
2. 选 `narrator_male_storyteller` 作为参考音
3. 依次测下面 4 种输入,听哪个念对了:

```
输入 1: 血泊                  ← 基准(大概率读错)
输入 2: 血{xue4}泊            ← 最常见 fork 语法
输入 3: 血[xue4]泊            ← 部分 fork 语法
输入 4: 血xue4泊              ← 无括号直接夹拼音
```

**预期**:其中一种会念成"xuè pō",其他可能仍念错。

### 没一种对怎么办?

Plan B:**直接改 SoVITS 的 G2P 词典**。

找你 SoVITS 安装目录下的:
- `GPT-SoVITS/text/zh_normalization/` 或
- `GPT-SoVITS/text/chinese_dialect_lexicons/` 或
- `tools/text_segmentation/`

里面有 `pinyin_dict.txt` 类似文件。在文件末尾加:

```
血泊 xue4 po1
```

保存,重启 SoVITS 服务。

这是最底层的修法,**100% 工作**,但需要每加一个多音字都改词典。

---

## 四、改动 3:`core/pronunciation_fix.py`

假设你第三步测出 SoVITS 支持的是 `{xue4}` 大括号语法。

`pronunciation_fix.py` 里所有 fix 条目的 `sovits` 字段**已经是这个格式**:

```python
{"src": "血泊", "sovits": "血{xue4}泊", "edge": "血滩"},
```

**不用改**,直接跑。

### 如果 SoVITS 支持的是 `[xue4]` 或别的语法

需要批量改 `_FIXES` 列表。开个文件编辑器,**用查找替换**:

| 替换前 | 替换后 |
|---|---|
| `"{` | `"[`  ← 把所有"{"改成"[" |
| `}"` | `]"`  ← 把所有"}"改成"]" |

或者写个简单脚本:

```python
import re
with open("pronunciation_fix.py", "r", encoding="utf-8") as f:
    text = f.read()
# 把 sovits 字段里的 {xxx} 改成 [xxx]
text = re.sub(r'(?<=sovits[":\s]*"[^"]{0,30})\{(\w+)\}', r'[\1]', text)
with open("pronunciation_fix.py", "w", encoding="utf-8") as f:
    f.write(text)
```

---

## 五、验证流程

### 1. 重启 voice_engine 进程

如果 voice_engine 是常驻服务,重启;如果是按需调用,跑下一集时自动加载新配置。

### 2. 跑一集故事

挑一集**包含多音字**的故事(比如有"血泊"、"了断"、"还魂"这些词)。

### 3. 听感检验

听这一集的旁白部分(narrator),验证三件事:
- ✅ 念对了多音字(没有"血、xuè、bo"那种奇怪发音)
- ✅ 字幕和发音一致(字幕"血泊",念"xuè pō")
- ✅ 音色和之前 edge_tts 不一样(因为换了引擎)

### 4. 如果某个字仍念错

在 `_FIXES` 里加新条目,跑命令验证:

```bash
python core/pronunciation_fix.py "测试句子带新难字" gpt_sovits
```

看输出格式对不对,再重跑那一集。

---

## 六、性能影响

旁白从 Edge TTS 切到 SoVITS:
- **单句生成时间**:0.5s → 3-5s(SoVITS 比 Edge 慢 6-10 倍)
- **整集生成时间**:整片旁白 100 句的情况,从 1 分钟增加到 5-8 分钟
- **听感**:**显著提升**,因为有了情感曲线(SoVITS 跟随参考音的情绪)
- **稳定性**:Edge TTS 偶有 503,SoVITS 是本地不会;反过来 SoVITS 偶有生成异常需重试,SoVITS 服务本身要稳

**结论**:值得切。多花的 4-5 分钟换显著音质提升。

---

## 七、回滚方案

如果切完发现问题(比如 SoVITS 服务挂了/音色不喜欢/速度太慢):

只需要把 voice_library.yaml 里 `narrator` 改回:

```yaml
narrator:
  voice:
    engine: edge_tts
    voice_id: zh-CN-YunjianNeural
```

不影响其他角色的 sovits 配置。**5 秒回滚**。

---

## 八、一句话总结

**改两行 yaml + 测一次 SoVITS 语法 + 听一集验证**。
完成后字幕"血泊"念"xuè pō",再也没有"血、xuè、bo"那种诡异发音。
