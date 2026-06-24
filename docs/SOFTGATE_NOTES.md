# SoftGate 软质量门 (v4)

> 落盘日期:2026-05-21
> 作者背景:从 LlavaGate 多次误伤 (p9 手部特写、p43 钥匙特写、p13 远景踢门) 的实战教训中提炼

---

## 一、为什么有 SoftGate

LlavaGate 作为"全维度审美仲裁者"频繁误伤好图。三种典型失败:

| 案例 | LlavaGate 判罚 | 实际问题 |
|---|---|---|
| p9 指尖透明特写 | hand_quality=4 一票否决 | 画面里压根没有手 (PuLID 拉回正脸像) |
| p43 钥匙特写 | face_score=2 触发修复 | 画面里压根没有脸 (背影+手特写) |
| p13 远景踢门 | figure_quality=9.9 通过 | 实际人物解剖崩坏,远景模板太宽容 |

**根因**:8B 视觉模型做"审美判断"不可靠,做"硬伤检测"还行。
质量门定位过宽,反而劣化系统。

---

## 二、SoftGate 设计哲学

**一句话**:质量门退守"硬伤检测",审美/构图/focus 交给分镜系统。

具体:

1. **只管硬伤** — 脸畸形、手畸形、多头多肢、水印
2. **检查前先问"有没有"** — 没有就跳过 (不评分不扣分)
3. **按主体类型分流尺子** — 人 vs 拟人动物 vs 真实动物 vs 机甲...
4. **接口零变更** — QualityResult 字段含义不变,旁路启用

---

## 三、Profile 设计

主题 yaml 里声明 `quality.profile`,决定 Stage 2 用什么尺子:

| Profile | 适用主题 | Stage 2 评分核心 |
|---|---|---|
| `human_realistic` | chinese_horror_tales, lyrical_storytelling, victorian_gothic_tales | 5 指人手、亚洲/欧式脸结构、严判 |
| `human_stylized` | 二次元/插画向 (待启用) | 5 指人手、二次元脸允许风格化变形 |
| `anthro_creature` | children_storybook, victorian_whimsical_children | 卡通脸对称即可、爪/掌形态完整、**不数指头** |
| `realistic_animal` | (未来动物主题) | 物种解剖、四肢数量、关节方向 |
| `mecha` | (未来机甲主题) | 沿用 PROMPT_SOLO_MECHA |
| `object_focus` | 物件特写多的主题 | 不评脸不评手,只查水印/截断 |
| `none` | 跳过硬伤检查 | 直接 pass (极端情况留口) |

**缺省值**:`human_realistic`。新主题忘了写也不会崩,只是按真人尺子量。

---

## 四、两阶段评分流程

**仅 image_type=solo_character 走两阶段**;远景/合成沿用 LlavaGate。

```
Stage 1: 存在性观察 (不评分,只问事实)
  输入:image_path
  输出:{face_visible, face_count, limbs_visible, limb_in_focus,
         shot_distance, main_subject_focus}

Stage 2: 按 profile 选评分尺子
  输入:image_path, stage1 结果, profile
  prompt 按 profile + stage1 动态拼装:
    - face 评什么 (人脸/卡通脸/物种脸)
    - limb 评什么 (5 指/爪掌/四肢)
    - 一票否决阈值 (按严格度)
  输出:维度评分 + pass/fail
```

Stage 1 失败 → 用保守默认值 (face=clear, limbs=two, focus=face)
Stage 2 失败 → passed=False, score=-1, feedback="评分失败"

---

## 五、字段命名

`hand_quality` 在 SoftGate 里语义上是 **肢端**质量 (手/爪/掌)。
保留这个 key 是为了 LlavaGate 模板兼容,日志里改用 `limbs_visible` 等
通用术语,避免拟人动物镜显示 "hand=4" 的语义错配。

---

## 六、启用方法

### 6.1 默认不启用 (保持现状)

```python
gate = make_gate(mode="auto", ...)   # 还是 LlavaGate
```

### 6.2 启用 SoftGate

```python
gate = make_gate(mode="soft", ...)
# 或者跟 BatchSelect 组合 (推荐):
gate = make_gate(mode="batch_soft", batch_n=2, ...)
```

### 6.3 主题 yaml 必须声明 profile (强烈推荐)

```yaml
# themes/<你的主题>.yaml
quality:
  threshold:              7.0
  review_focus:           "..."
  composite_review_focus: "..."
  profile:                human_realistic   # ← 新增
```

漏写 → 自动 fallback 到 `human_realistic` + 控制台 stderr 无警告
(因为 dataclass 默认值兜底)。

---

## 七、接口变更一览

### 改动的字段

| 字段 | 类型变更 | 行为变更 |
|---|---|---|
| `QualityContext.quality_profile` | 新增 str,默认 "human_realistic" | LlavaGate 不读;SoftGate 用它选尺子 |
| `QualityConfig.profile` | 新增 str,默认 "human_realistic" | 主题 yaml 里的 quality.profile |

### 没改的字段

- `QualityResult` 全字段、含义不变
- `QualityGate.evaluate()` 签名不变
- `BatchSelectGate / EnsembleGate / AutoAcceptGate / HumanGate` 一行不动
- 现有 LlavaGate 一行不改 (mode="auto" 完全保持现状)

### 一个语义微调 (零代码改动)

- `face_score = -1` 现在多了一种来源:**SoftGate 判定脸不可见**
- 自动让 `face_needs_repair` 属性返回 False (因为 `0 <= -1` 为假)
- 下游 orchestrator 不会触发面部修复重生

---

## 八、配套需改的文件清单

### 必改 (落盘范围内)

- ✅ `core/quality.py` — 新增 SoftGate 类 + QualityContext 加字段 + make_gate 加 soft mode
- ✅ `core/theme.py` — QualityConfig 加 profile 字段
- ✅ `themes/children_storybook.yaml` — 加 `quality.profile: anthro_creature`

### 必改 (落盘范围外 — 由 Ziv 自己 patch)

下面这些不在本次落盘内,因为我没看过完整源码,Ziv 自己 patch 更稳。

#### 8.1 `core/pipeline.py` — `_make_context` 注入 quality_profile

定位:`_make_context` 方法 (大约 line 409),`QualityContext(...)` 构造调用。

改动:在构造时多传一个参数:

```python
return QualityContext(
    page_num           = page_cfg["page"],
    page_title         = page_cfg["title"],
    characters         = page_cfg["characters"],
    char_features      = self.story.char_features(page_cfg["characters"]),
    attempt            = attempt,
    max_attempts       = max_att,
    image_type         = image_type,
    narration_keywords = must_haves,
    quality_profile    = self.theme.quality.profile,   # ← 新增这一行
)
```

#### 8.2 其他 themes/*.yaml — 加 `quality.profile`

按以下映射给每个主题加一行:

| 文件 | 加在 quality: 子段下 |
|---|---|
| `themes/chinese_horror_tales.yaml` | `profile: human_realistic` |
| `themes/lyrical_storytelling.yaml` | `profile: human_realistic` |
| `themes/victorian_gothic_tales.yaml` | `profile: human_realistic` |
| `themes/victorian_whimsical_children.yaml` | `profile: anthro_creature` |
| `themes/chinese_ghost_flux.yaml` | `profile: human_realistic` (即使要废弃,落盘期间它还在用) |

漏写不会崩,只是默认按 `human_realistic` 处理。

---

## 九、回滚方法

如果 SoftGate 上线后效果不理想:

1. **临时回滚**:把调用处的 `make_gate(mode="soft", ...)` 改回 `mode="auto"`,
   不需要改其他任何文件
2. **永久撤销**:删除 SoftGate 类即可。`QualityContext.quality_profile` 字段
   可以保留 (有默认值,不影响 LlavaGate)

完全无破坏性。

---

## 十、跟现有体系的关系

| 子系统 | 跟 SoftGate 的关系 |
|---|---|
| LlavaGate | 平级,不互通。auto vs soft 二选一。 |
| BatchSelectGate | 内嵌 LlavaGate 或 SoftGate。`mode="batch_soft"` = BatchSelect + SoftGate。 |
| EnsembleGate | 当前未跟 SoftGate 组合,需要时手动构造 |
| focal_director | SoftGate 不查 focus 一致性,这部分回归到分镜系统自己负责 |
| narration_keywords | SoftGate 忽略此字段,不再做软扣分 |
| feedback.py 重生 prompt 改写 | 透明 — orchestrator 看 issues 标签,SoftGate 照常输出标签 |
| param_learner 参数学习 | 透明 — 看的是 score 字段 |

---

## 十一、已知限制

1. **8B 视觉模型对 6 指/缺指的识别有上限** — 偶发漏检靠 BatchSelect 多图择优兜底
2. **双脸场景** (p19 那种) 当前仍按整图打一个 face_score,副脸花掉守不住 — 未来需接 InsightFace 单独评每张脸
3. **Stage 1 + Stage 2 两次调用 → 人物页评分时间翻倍** (单页约 +10s)
   - 整体收益:重生触发减少,40 页故事净增成本约 1-2 分钟
4. **Profile 是主题级别声明,一个故事内不会切换** — 一个故事里既有人又有动物时,
   只能按主体类型选一个最贴近的 profile,边缘情况会有误判
