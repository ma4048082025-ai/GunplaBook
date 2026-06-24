# 演员库 v2.8 部署 + 使用指南

> 跨故事/跨题材复用角色定妆的"演员资产库"。
> 完全独立的子系统，**不破坏任何现有流程**。

---

## 一、是什么、解决什么

### 现状（v2.7）
- 每个故事独立生成定妆照
- 不同故事的"老村长"不一样
- 配角（extra）很多场景重复出现，每次都新生成

### 新设计（v2.8）
- 演员库存"通用角色形象"（驼背庙祝型、清纯村姑型、慈祥村长型...）
- 新故事可以**复用已有演员**（像电影选角）
- 也可以**新生成**（旧流程）
- **混合模式**：lead 新生成 + extra 用演员库

### 哲学
- **资产化**：定妆照是资产，不是一次性产物
- **解耦**：演员库独立维护，story 不强依赖
- **手动可控**：默认推荐 + 用户决定

---

## 二、目录结构

```
refs/actors/                              ← 演员库根目录
├── _index.yaml                            ← 全库索引(快速 list)
├── _tags_dictionary.yaml                  ← 关键词 → tag 映射(可编辑扩展)
├── elder_male/                            ← 老年男性
│   ├── elder_male_001/
│   │   ├── portrait.png                   ← 主图
│   │   ├── meta.yaml                      ← 演员属性
│   │   └── usage.log                      ← 使用记录(纯文本)
│   └── elder_male_002/
├── adult_male/                            ← 中年男性
├── young_male/                            ← 男青年
├── boy_child/                             ← 男幼童
├── elder_female/                          ← 老年女性
├── adult_female/                          ← 中年女性
├── young_female/                          ← 女青年
└── girl_child/                            ← 女幼童
```

---

## 三、8 个 Category（不再变）

| Category | Age 字段值 | Gender |
|---|---|---|
| `elder_male` | senior/elder/old | male |
| `adult_male` | adult/middle | male |
| `young_male` | young/teen | male |
| `boy_child` | child | male |
| `elder_female` | senior/elder/old | female |
| `adult_female` | adult/middle | female |
| `young_female` | young/teen | female |
| `girl_child` | child | female |

**outline 里 character 的 age 字段，按上表选**。

---

## 四、落盘步骤

```bash
# 1. 备份
cp tools/long_writer/cli.py         tools/long_writer/cli.py.backup_v2_7
cp tools/long_writer/portraits.py   tools/long_writer/portraits.py.backup_v2_7

# 2. 落新文件
cp ~/Downloads/v2_8_actor_library/actor_library.py    tools/long_writer/actor_library.py
cp ~/Downloads/v2_8_actor_library/actor_cli.py        tools/long_writer/actor_cli.py
cp ~/Downloads/v2_8_actor_library/cli.py              tools/long_writer/cli.py
cp ~/Downloads/v2_8_actor_library/portraits.py        tools/long_writer/portraits.py

# 3. 验证语法
python -c "import ast; [ast.parse(open(f).read()) for f in [
  'tools/long_writer/actor_library.py',
  'tools/long_writer/actor_cli.py',
  'tools/long_writer/cli.py',
  'tools/long_writer/portraits.py']]; print('OK')"

# 4. 验证 import 链
python -c "from tools.long_writer.actor_library import infer_category; print(infer_category({'gender':'male','age':'senior'}))"
# 应输出: elder_male
```

---

## 五、初始化（一次性，强烈推荐先做）

把你已有的 16 张定妆照入库：

```bash
# 先看看会入库哪些(不写盘)
python -m tools.long_writer.actor_cli pool --dry-run

# 没问题就真的入
python -m tools.long_writer.actor_cli pool
```

**预期输出**：

```
扫描 refs/character_portraits/ ...

=== 扫描报告 ===
  新增/将新增: 16
  跳过:        0
  错误:        0

新增演员:
  long_20260518_234509_狐王降世/张天师                → elder_male_001
     tags: taoist, mystical, long_beard
  long_20260518_234509_狐王降世/玉面姥姥             → elder_female_001
     tags: elder, ...
  long_20260518_234509_狐王降世/陈远正                → adult_male_001
  long_20260520_152021_彩窗泪影/沈烬穹                → young_male_001
  long_20260520_152021_彩窗泪影/老司钟                → elder_male_002
  long_20260520_152021_彩窗泪影/裴时砂                → young_female_001
  long_20260521_101238_沈小石的古镇奇遇/沈小石        → boy_child_001
  long_20260524_071415_钟鸣十三/沈墨白                → young_male_002
  long_20260524_071415_钟鸣十三/钟十三娘             → young_female_002
  long_20260524_071415_钟鸣十三/谢员外                → adult_male_002
  long_20260525_150849_古镇血镜/无面女                → young_female_003
  long_20260525_150849_古镇血镜/老庙祝                → elder_male_003
  long_20260525_150849_古镇血镜/陈远正                → adult_male_003
```

**演员库初版有了**。

---

## 六、日常使用流程

### 流程 A：新故事跑标准流程（不用演员库）

```bash
# 完全跟以前一样,什么都不需要改
python -m tools.long_writer.cli outline ...
python -m tools.long_writer.cli storyboard ...
python -m tools.long_writer.cli convert ...
python -m tools.long_writer.cli portraits stories/xxx.yaml -n 4
#                                                            ↑ 现在默认含 extras
python -m tools.long_writer.cli portraits_pick ...
```

**变化只有一处**：portraits 命令现在**默认给所有角色（含 extras）生成**。
如果只想给 lead 生成，加 `--no-extras`。

### 流程 B：新故事用演员库复用（推荐）

```bash
# Step 1-3: 标准流程
python -m tools.long_writer.cli outline ...
python -m tools.long_writer.cli storyboard ...
python -m tools.long_writer.cli convert ...

# Step 4: 查演员库推荐
python -m tools.long_writer.actor_cli suggest stories/xxx.yaml

# 输出例:
#   ━━ 老村长 (male/senior) ━━━━━━━━━━━━
#     desc: 看守荒庙的驼背老人
#     ★ elder_male_001 (驼背庙祝型) score=0.80
#       tags: temple_keeper, elder, hunched
#     ★ elder_male_003 (老庙祝) score=0.65
#       tags: temple_keeper, elder
#       
#   ━━ 沈小白 (male/young) ━━━━━━━━━━━━━
#     desc: 江南书生,清秀
#       young_male_001 (...) score=0.20
#     (无强匹配,建议新生成)

# Step 5: 决定每个角色用什么
# 5a. 复用演员
python -m tools.long_writer.actor_cli cast stories/xxx.yaml \
    --character 老村长 --actor elder_male_001

# 5b. 没演员的(沈小白)走 portraits 新生成
python -m tools.long_writer.cli portraits stories/xxx.yaml \
    --character 沈小白 -n 4
# 然后 portraits_pick

# Step 6: 之后流程不变
python run.py twophase ...
```

---

## 七、演员库管理命令

```bash
# 列表
python -m tools.long_writer.actor_cli list                          # 全部
python -m tools.long_writer.actor_cli list --category elder_male    # 一类
python -m tools.long_writer.actor_cli list --gender female          # 一性别
python -m tools.long_writer.actor_cli list --tag temple_keeper      # 一 tag

# 详情
python -m tools.long_writer.actor_cli show elder_male_001

# 手动注册一张图
python -m tools.long_writer.actor_cli register /path/to/image.png \
    --category elder_male --gender male --age senior \
    --name "驼背老和尚" \
    --tags "temple_keeper,buddhist,hunched,wrinkled" \
    --features "shaven head, deep wrinkles"

# 推荐
python -m tools.long_writer.actor_cli suggest stories/xxx.yaml
python -m tools.long_writer.actor_cli suggest stories/xxx.yaml --top 5

# 选角
python -m tools.long_writer.actor_cli cast stories/xxx.yaml \
    --character 老村长 --actor elder_male_001
# 默认 copy 模式(占空间但稳定)
# 加 --mode symlink 用软链(省空间)

# 从已有 portrait 反向入库
python -m tools.long_writer.actor_cli pool
python -m tools.long_writer.actor_cli pool --dry-run    # 只看不写
```

---

## 八、tag 库扩展

`refs/actors/_tags_dictionary.yaml` 是用户可编辑的。

例子：

```yaml
# 默认已经有 80+ 条
# 你想加新词:
"郎中":     ["doctor_traditional", "elder", "scholarly"]
"江湖术士": ["charlatan", "shifty"]
"老中医":   ["doctor_traditional", "elder"]
```

加完后`suggest` 立即生效，**无需重启**。

---

## 九、接口契约（前后兼容关键）

### story.yaml 字段不变

```yaml
characters:
  - name: 老庙祝
    role: extra
    portrait_ref: refs/character_portraits/long_xxx/老庙祝.png   # ← 路径,不变
    actor_id: elder_male_003                                       # ← v2.8 新增,可选(追溯用)
```

**`portrait_ref` 含义完全不变**。下游所有代码（to_pipeline / portraits_pick / pipeline_v260_router）**不需要任何改动**。

### `actor_id` 字段
- 是**追溯信息**，不影响生成
- 没有也能跑（pipeline 用 portrait_ref 路径就够）
- 有了可以反查"这个角色用的是哪个演员"

---

## 十、回滚

```bash
# 完全回到 v2.7
cp tools/long_writer/cli.py.backup_v2_7        tools/long_writer/cli.py
cp tools/long_writer/portraits.py.backup_v2_7  tools/long_writer/portraits.py

# 删除新模块(可选,不删也不影响)
rm tools/long_writer/actor_library.py
rm tools/long_writer/actor_cli.py

# refs/actors/ 目录可以留着(不影响任何旧流程)
```

---

## 十一、对其他子系统零冲突保证

| 子系统 | 是否受影响 | 原因 |
|---|---|---|
| 分镜大师 (long_storyboard) | ❌ 不受影响 | 不读 actors 目录 |
| reviewer 链 | ❌ 不受影响 | 同上 |
| coordinator | ❌ 不受影响 | 同上 |
| to_pipeline | ❌ 不受影响 | 只读 portrait_ref 路径 |
| pipeline (生图) | ❌ 不受影响 | 同上 |
| audio engine | ❌ 不受影响 | 不涉及图片 |
| mixer | ❌ 不受影响 | 同上 |
| v2.6 多角色路由 | ❌ 不受影响 | 看 portrait_ref 路径 |

**演员库是叶子节点系统**——只对外提供数据，不被其他系统依赖。

---

## 十二、未来扩展点

这一版**已经够用**。但留了以下扩展空间：

1. **演员画像批量增强**：用一张 portrait 跑 reference + IPAdapter 强化形象
2. **演员描述自动翻译**：把 identity_tags 翻成更地道的英文片段供 FLUX 用
3. **跨故事一致性检查**：自动检测"同个演员在两个故事中是否合理"
4. **演员评分**：用户对某个演员的"成片质量"打分，用作 suggest 排序权重

**这些都不影响当前架构**，未来加进 actor_library.py 即可。

---

完。
