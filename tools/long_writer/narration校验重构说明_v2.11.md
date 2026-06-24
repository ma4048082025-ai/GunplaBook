# narration 校验机制重构 v2.11 — 一件事一个出口

> 解决"工具压工具 + 九龙治水":多个 reviewer 抢同一字段、给矛盾答案、刷屏噪音。
> 核心原则:**程序先跑(确定性),LLM 后跑(创作);每个字段只有一个出口。**

---

## 一、改了哪些文件

| 文件 | 改动 | 放哪 |
|---|---|---|
| reviewers.py | 权限矩阵收紧 + 接入 integrity + narrative 瘦身 + flux 降噪 + no-op/exclude 去重 | tools/long_writer/ |
| narration_flow_reviewer.py | 砍维度3/4,只留主语/时空/节奏 | tools/long_writer/ |
| narration_integrity.py | **内容不变**(只是确认接口,本来就写好) | tools/long_writer/ |

**long_storyboard.py / coordinator.py / focal_director.py 本轮不动。**

---

## 二、职责重新划分(消除重叠)

```
确定性层(程序,先跑,零LLM成本):
  ├─ 去重 + 空镜清理        → coordinator 里的 dedup_v2 (已接入,不动)
  └─ 信息守恒发现 + 引号归属 → narration_integrity (本轮接入)
        · Layer1/2: 抽取原文关键信息,对账缺失(程序)
        · 引号迁移: narration 里的引号自动归 dialogue(程序,唯一出口)
        · Layer3:  仍缺失时一次定向 LLM 补回(可选,config 控制)

LLM 层(看程序处理不了的语义,后跑):
  ├─ narrative      → 只管"事实写错 / 顺序错乱"(砍掉信息守恒+引号)
  ├─ narration_flow → 只管"主语 / 时空 / 节奏"(砍掉重复+引号)
  ├─ visual         → shot_type / 转场 / kb_direction
  ├─ flux           → visual_must_haves.exclude(砍掉 missing_angle 刷屏)
  ├─ dialogue       → 只报 issue(砍掉 dialogue 写权)
  ├─ coherence      → 只报 issue(原本就是)
  └─ focal_director → focal_subject 等(独占,不动)

诊断层(手动):
  └─ narration_flow_extractor(不动)
```

---

## 三、九龙治水的根治(对照之前日志的问题)

| 日志问题 | 根因 | 本轮如何根治 |
|---|---|---|
| B1 speaker 三方混战 | dialogue+narrative+flow 都想改 speaker | **引号归属 → integrity 程序独占**;dialogue 砍写权,narrative/flow 不碰 dialogue |
| B2 narration 双写 | narrative+flow 同改 narration | 分维度:narrative=事实,flow=主语/时空/节奏,**不重叠** |
| B3 no-op 假阳性 | before==after / severity=skip 算进 patch | apply_patches 补 `severity==skip` 静默跳过 |
| B4 missing_angle 刷屏 | flux 每镜报无权修的 issue | flux prompt 明确"不要报构图角度" |
| B5 exclude 重复(modern,modern) | flux 无脑追加 | apply_patches 写 exclude 时去重 |
| B7 coherence 重复报 sensory | coherence 已是只报 issue | 维持(focal_director 才改 focal) |

**关键机制**:权限矩阵在 apply_patches 层强制——不在白名单的 patch 直接拒绝。
矛盾 patch 从机制上不可能发生,不靠 prompt 祈祷 LLM 不打架。

---

## 四、字段 → 唯一出口对照表

| 字段/职责 | 唯一出口 | 类型 |
|---|---|---|
| dialogue / speaker (引号归属) | narration_integrity | 程序 |
| 重复检测 / 空镜 | coordinator dedup_v2 | 程序 |
| 信息守恒(发现缺失) | narration_integrity | 程序 |
| 信息守恒(补回) | integrity Layer3 | 1次LLM |
| narration 事实/顺序 | narrative | LLM |
| narration 主语/时空/节奏 | narration_flow | LLM |
| focal_subject 等 | focal_director | LLM |
| visual_must_haves | flux | LLM |
| shot_type/转场 | visual | LLM |

---

## 五、config 开关(新增/确认)

```python
# narration 信息守恒(程序层,风险低,建议开)
ENABLE_NARRATION_INTEGRITY = True
ENABLE_INTEGRITY_LLM_REPAIR = False   # Layer3 定向修复,先观察程序效果再开

# 旁白流语义审稿(LLM)
ENABLE_NARRATION_FLOW_REVIEWER = True
```

---

## 六、验证结果(都已跑通)

- integrity 引号迁移:"这是最后一套..."从 narration→dialogue,speaker 自动推断 ✓
- 权限矩阵:dialogue reviewer 改 speaker 被拒,speaker 保持原值 ✓
- exclude 去重:"clean,modern,modern,clear text,clear text"→"clean, modern, clear text" ✓
- severity=skip 静默跳过,不进 log 噪音 ✓
- 四文件语法 OK ✓

---

## 七、执行顺序(reviewer 链最终形态)

```
pre_check (coordinator: 机械整理)
  ↓
LLM reviewer 链: narrative → visual → flux → dialogue → coherence → focal_director
  ↓ (各产 patch/issue)
narration_flow (LLM: 主语/时空/节奏)
  ↓
apply_patches (权限矩阵强制 + no-op过滤 + exclude去重)
  ↓
narration_integrity (程序: 引号迁移 + 信息守恒对账 + 可选Layer3)
  ↓
render_characters 校验 (程序)
  ↓
post_check (coordinator: dedup_v2 去重 + 空镜清理)
```

确定性处理(integrity/dedup)夹在 LLM 链两端:LLM 改完语义,程序兜底确定性的事。

---

## 八、落地 + 回滚

```bash
cd tools/long_writer/
cp reviewers.py reviewers.py.bak
cp narration_flow_reviewer.py narration_flow_reviewer.py.bak
# narration_integrity.py 内容没变,但确保它在位

cp 下载的/reviewers.py .
cp 下载的/narration_flow_reviewer.py .

# config.py 加开关(见第五节)

# 跑验证,重点看日志:
#   [integrity] auto-fixed: quoted_dialogue ... (migrated narration→dialogue)  ← 引号归位
#   不再出现 speaker 三方给不同答案
#   flux 不再刷屏 missing_angle
python -m tools.long_writer.cli storyboard scripts/<id>_segments.yaml

# 回滚: cp *.bak 回去
```

---

## 九、本轮没解决(诚实标注)

- **C1 字数急刹车**:涉及拆镜策略判断,改不好会过度拆镜。建议单独一轮,先用 flow_extractor 观察哪些是真断裂。
- **focal_director 图问题(手画成脸/脸对脸)**:这是另一条线(图,不是 narration 校验),需改 focal_director.py。建议下一轮单独做,避免和本轮权限矩阵改动混在一起难定位。
