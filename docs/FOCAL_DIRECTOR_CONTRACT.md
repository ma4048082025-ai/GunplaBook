# Focal Director 接入契约 v0.1

> 本文档说明:**focal_director(v2.5)是分镜阶段 focal_subject 字段的唯一作者**,以及如何用智能体框架替换它的引擎部分。
> 设计哲学跟 `CREATOR_AGENT_CONTRACT.md` 一致:**facet/接口稳定,引擎可换**。

---

## 一、为什么有 focal_director

历史问题:`flux / coherence / dialogue` 三个 reviewer 都改 `focal_subject`,顺序覆盖污染 prompt(medieval 硬塞 / 元指令 / 主体偏移)。生图阶段表现为:p29 红脸吼绿火、p30 张天师变红衣尖叫女子、p31 道士披发尖叫、p33 眼睛颜色过红——**主体失焦**。

v2.5 的核心架构改变:**`focal_subject` 改为单一出口,由 `focal_director` 综合所有上下文一次性写定**。其他 reviewer 仍存在,但**只产 issues 不直接 patch focal**。

```
旧 (v2.4):
  shots → narrative + visual + flux + dialogue + coherence
                                ↑       ↑          ↑
                          全部抢 focal_subject  → 顺序覆盖污染

新 (v2.5):
  shots → narrative + visual + flux + dialogue + coherence
                                ↓       ↓          ↓
                              issues  issues    issues
                                ↓       ↓          ↓
                          focal_director(综合判断) → 单一改写 focal
```

---

## 二、focal_director 的职责

**独占字段**:
- `focal_subject` (字段白名单 `FOCAL_DIRECTOR_FIELD_WHITELIST = {"focal_subject", "_pulid_chars"}`)
- `_pulid_chars` (角色 PuLID 注入列表)

**核心准则**(在 `FOCAL_DIRECTOR_PROMPT` 里):
1. **时代锚定** — 用故事真实时代,绝不乱填 "medieval"
2. **FLUX 不擅长元素改写** — "X 的影子" / "第 N 个 Y" 这类抽象表达自动改写
3. **prompt 卫生** — 黑名单词、感官非视觉词、元指令一律剥
4. **长度控制** — 30-80 词;超长卫士 90→80 硬截
5. **主体清晰** — 不允许 "scene continues from: <长串>" 污染主体
6. **角色识别 + PuLID 注入** — 看脸/上半身镜自动启用 PuLID
7. **default 不动** — 已经合理的 focal 不强改

---

## 三、解耦:ThemeProfile 系统

不同主题(中式恐怖 / 武侠 / 都市怪谈 / 科幻)的 FLUX 知识不同。`focal_director` 不硬编码主题知识,而是**注入 `ThemeProfile`**。

### 3.1 内置 profiles
`theme_profile.py::DEFAULT_PROFILES` 包含:
- `default` — 通用 fallback
- `chinese_horror_tales` — 中式恐怖(含 1980s/1990s/明清等 era 锚词,禁用 medieval/victorian/gothic)
- `wuxia` — 武侠(预留)

### 3.2 外置 profile 覆盖
不改代码,只放文件:
```yaml
# themes/<theme_id>/profile.yaml
theme_id: my_custom_theme
natural_era_words: [...]
forbidden_era_words: [...]
style_base: "..."
flux_unfriendly_patterns:
  - ["'s shadow", "改写为 humanoid shadow"]
blacklist_keywords: [...]
character_archetypes:
  道士: "elderly daoist priest, yellow robe, ..."
```

加载优先级:**外置 yaml > 内置 dict > default fallback**。

### 3.3 ThemeProfile 字段

| 字段 | 类型 | 用途 |
|---|---|---|
| `theme_id` | str | 标识 |
| `natural_era_words` | list[str] | LLM 该用的时代锚词 |
| `forbidden_era_words` | list[str] | 绝不能出现的词,卫士会剥 |
| `style_base` | str | 风格基座(每张图固定拼) |
| `flux_unfriendly_patterns` | list[(kw, strategy)] | "X 的影子" 等改写策略 |
| `blacklist_keywords` | list[str] | 全局黑名单(感官词、元指令) |
| `character_archetypes` | dict[str, str] | "道士" → 视觉描述 |

---

## 四、接口契约(其他 reviewer 不变)

`focal_director` 的函数签名跟其他 reviewer **完全一致**:

```python
def focal_director(shots: list, chapter: dict, story_meta: dict) -> dict:
    return {
        "issues":   [...],    # 它自己发现的问题
        "patches":  [...],    # field 限定为 focal_subject / _pulid_chars
        "reviewer": "focal_director",
        # 可选:
        "skipped":  "import_error | llm_error | json_parse_failed",
    }
```

这意味着 `run_all_reviewers` 的调度代码**不需要任何 if-else 特判**。它只是注册表 `ALL_REVIEWERS` 中的一项,**唯一的特殊性是它必须在所有其他 reviewer 之后跑**(为了看到前置 issues)。

### 4.1 issue 传递机制(v2.5 新增)

`run_all_reviewers` 跑到 `focal_director` 之前,会把所有前置 reviewer 的 `issues` 累计成 `story_meta["_collected_other_issues"]`,然后传入。每条 issue 自动加来源标签:

```python
{"reviewer": "flux", "shot_id": "sh06", "description": "缺时代描述", ...}
```

focal_director 在 prompt 里看到这些 issue,但**不必逐条服从**——它是综合判断者。

---

## 五、智能体接入(future)

跟 `creator_agent` 一样,`focal_director` 的 LLM 调用走 `reviewers._call_llm`(或者将来抽象成 LLMEngine)。要接入 ziv_agent_v5 时,有两种粒度:

### 方案 A:粗粒度替换(替整个 director)

写一个 `focal_director_ziv.py`:
```python
def focal_director_ziv(shots, chapter, story_meta) -> dict:
    """ZivAgent 版的 focal director。跟原版同接口。"""
    from ziv_agent_v5.agent.core import Agent
    agent = Agent(profile="storyboard_director")
    
    # 加载 theme profile(复用原版逻辑)
    from focal_director import _load_theme_profile_for_story, _sanitize_focal
    profile = _load_theme_profile_for_story(story_meta)
    
    # 构造请求
    raw = agent.run(
        user_input=f"分析以下分镜并改写 focal_subject: {shots_dump}",
        context={"theme_profile": profile.__dict__,
                 "other_issues": story_meta.get("_collected_other_issues", [])},
    )
    
    # 解析 + 走原版卫士(_sanitize_focal)
    ...
```

然后改 `reviewers.py` 的 ALL_REVIEWERS:
```python
if USE_ZIV_AGENT:
    from focal_director_ziv import focal_director_ziv
    ALL_REVIEWERS["focal_director"] = focal_director_ziv
```

### 方案 B:细粒度替换(只替换底层 LLM 调用)

将来 `reviewers._call_llm` 改成 `LLMEngine` 抽象,`focal_director` 不动,只换 engine。这条路跟 `creator_agent` 已经走的路完全相同。

**这两条路并行存在,你可以同时用**:
- outline / chapter 阶段:走 `creator_agent` 的 `LLMEngine` 抽象,智能体接管对话
- storyboard 阶段:走 `focal_director` 的整体替换或 LLMEngine 抽象

---

## 六、和其他子系统的关系

### 6.1 跟 coordinator 的关系
- coordinator 仍然做**机械整理**(14.3 剥离、空镜清理、`_hold_source_page` 重定向)
- coordinator **不动 focal_subject**(从来也没动过)
- focal_director 的 patches 会经过 `apply_patches` 走 coordinator 的 post_check

### 6.2 跟 orchestrator 重生机制的关系
- orchestrator(质量门重试)有自己的 `_sanitize_rewritten_prompt`
- focal_director 的卫士是**写入 yaml 前**的清洗
- orchestrator 的卫士是**重生 prompt 时**的清洗
- 两者各管一段,不冲突

### 6.3 跟 portraits/PuLID 的关系
- focal_director 通过 `_pulid_chars` 字段告诉下游"这一镜启用谁的 PuLID"
- 下游(pipeline / orchestrator)读 `_pulid_chars`,加载对应 `portrait_ref` 进 ComfyUI 工作流
- focal_director 知道哪些角色有 portrait(从 `story_meta.characters[].portrait_ref`)

---

## 七、当前实现状态

| 组件 | 状态 |
|---|---|
| `theme_profile.py` (ThemeProfile + 注册表) | ✓ 完成 |
| `focal_director.py` (主入口 + 卫士) | ✓ 完成 |
| `reviewers.py` (字段白名单 + ALL_REVIEWERS 注册) | ✓ 完成 |
| `reviewers.py` (issue 收集机制) | ✓ 完成 |
| 单元测试 | ✓ 14 项全过 |
| `chinese_horror_tales` profile | ✓ 完成 |
| `wuxia` profile | △ 骨架 |
| 外置 yaml 覆盖加载 | ✓ 完成 |
| ZivAgent 适配器 | ✗ 待你接入 |

---

## 八、向后兼容承诺

| 不变的事 | 含义 |
|---|---|
| reviewer 接口 | `def reviewer(shots, chapter, story_meta) -> {issues, patches, reviewer}` |
| `run_all_reviewers` 调用方 | long_storyboard 完全不知道 v2.5 的存在 |
| 产物 yaml schema | shots 多了可选字段 `_pulid_chars`,旧 yaml 仍能跑 |
| 下游 producer / coordinator / pipeline | 全部无感 |

---

## 九、问题与反馈

接入或观察新行为时,如果发现:
- focal_director 改写过于激进/保守 → 调 `FOCAL_DIRECTOR_PROMPT` 的核心准则
- 某主题的 era 词不准 → 调 `theme_profile.py` 的 `DEFAULT_PROFILES[<theme>]`,或写外置 yaml
- 卫士剥太多 → 调 `_sanitize_focal` 的黑名单逻辑

这些都是在 v0.2 可以加新方法/字段的范围,**保持向后兼容**(已有的 ALL_REVIEWERS 注册不破)。
