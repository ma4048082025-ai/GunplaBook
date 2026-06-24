# Creator Agent 引擎接入契约 v0.1

> 本文档说明:**如何把你的 ziv_agent_v5 (或任何其他智能体框架) 接入 long_writer 的对话式写作流程**。
>
> 核心承诺:接入新引擎只需要写**一个文件**(实现 `LLMEngine` 接口)。所有 facet、cli、命令行参数、产物路径都不需要改动,下游 chapter_writer / script_doctors / long_storyboard / coordinator / producer 完全无感。

---

## 一、总体架构

```
┌──────────────────────────────────────────────────────┐
│  cli.py                                              │
│  python -m tools.long_writer.cli outline --interactive    │
│                  ↓                                   │
│  ┌──────────────────────────────────────────────┐   │
│  │  CreatorAgent (agent.py)                     │   │
│  │  - 通用对话循环                              │   │
│  │  - 控制命令处理 (/done /back /save /show)    │   │
│  │  - state 快照 (/back 撤销用)                 │   │
│  └──────────────────────────────────────────────┘   │
│           ↓                  ↓                       │
│  ┌──────────────────┐  ┌────────────────────────┐  │
│  │  LLMEngine       │  │  Facet                 │  │
│  │  - 抽象基类       │  │  - 抽象基类             │  │
│  │                  │  │  - OutlineFacet        │  │
│  │  ★ 接入点 ★      │  │  - ChapterFacet        │  │
│  │  (你要替换的)     │  │  - (将来) StoryboardFacet│  │
│  └──────────────────┘  └────────────────────────┘  │
└──────────────────────────────────────────────────────┘
```

`LLMEngine` 和 `Facet` 是两个垂直分工:
- **Engine**: 怎么"思考"(LLM 调用 / 多步推理 / 工具使用)
- **Facet**: 思考"什么"(写作阶段的领域知识、产物契约)

要接你的智能体,只动 Engine。

---

## 二、LLMEngine 接口

`creator_agent.engine.LLMEngine` 是抽象基类。子类必须实现两个方法:

### 2.1 `propose_suggestions(state, facet) -> list[Suggestion]`

**何时被调用**: 对话循环开始前(或用户主动要求 "再给我看看建议"),引擎主动检视当前 state,提 3-5 条改进建议。

**参数**:
- `state: dict` — 当前 state(facet 决定 schema)
- `facet: Facet` — 当前阶段的 facet,提供 `build_suggestions_prompt(state)` 等帮助方法

**返回**: `list[Suggestion]`,每个 Suggestion:
- `label: str` — 简短标签,显示给用户挑(≤ 20 字)
- `rationale: str` — 详细说明
- `focus_area: str` — 涉及的字段/区域

**典型实现**(参考 `SimpleLLMEngine.propose_suggestions`):
```python
def propose_suggestions(self, state, facet):
    prompt = facet.build_suggestions_prompt(state)
    raw = self.your_agent.run(prompt)   # ← 这里换成你的智能体调用
    return facet.parse_suggestions(raw)
```

### 2.2 `respond_to_user(state, facet, user_msg, history) -> AgentResponse`

**何时被调用**: 用户在对话循环里输入一条消息后。

**参数**:
- `state: dict`
- `facet: Facet`
- `user_msg: str` — 用户当轮发言
- `history: list[dict]` — 历史对话,格式 `[{"role": "user"|"agent", "content": str}, ...]`

**返回**: `AgentResponse`(`creator_agent.engine.AgentResponse`):
- `message: str` — 给用户看的自然语言回应
- `proposed_modification: Modification | None` — LLM 想做的修改(如果有的话),用户会被问要不要采纳
- `done: bool` — LLM 是否认为对话该结束(用户最终决定)

**典型实现**(参考 `SimpleLLMEngine.respond_to_user`):
```python
def respond_to_user(self, state, facet, user_msg, history):
    system_prompt = facet.build_system_prompt(state)
    directive = facet.build_response_directive()   # 告诉 LLM JSON 格式
    raw = self.your_agent.chat(
        system=system_prompt,
        history=history,
        user_msg=user_msg + "\n\n" + directive,
    )
    return facet.parse_agent_response(raw)   # 解析为 AgentResponse
```

---

## 三、Modification 数据契约

`AgentResponse.proposed_modification` 是 `Modification` 对象,定义在 `creator_agent.facets.base`:

```python
@dataclass
class Modification:
    description: str    # 一句话说人话 ("加配角老张头")
    ops: list           # 具体操作列表
```

`ops` 列表每一项是 dict:
```python
{
    "action": "set" | "append" | "delete",
    "field":  "title" | "chapters[2].title" | "characters" | ...,
    "value":  <任何 JSON 可表达的值>     # delete 时省略
}
```

**字段路径语法**:
- `title` → 顶层字段
- `chapters[2]` → list 第 3 个元素
- `chapters[2].title` → 嵌套字段
- `characters[0].name` → 嵌套字段

**字段白名单**: 每个 facet 定义 `_ALLOWED_FIELD_PREFIXES`,只允许修改白名单内的字段。修改 `story_id` 之类的关键标识会被拒绝。

---

## 四、Facet 接口 (你通常不需要碰)

`creator_agent.facets.base.Facet` 是抽象基类,已有实现:
- `OutlineFacet`: 大纲精炼
- `ChapterFacet`: 章节正文精炼

将来增加 `StoryboardFacet`(分镜精炼)只需要写一个新 facet 文件,**不动 engine 和 cli**。

如果你想自定义 facet,实现这些方法:
- `load_state / save_state`        — 状态磁盘 IO
- `serialize_state / state_summary`— 给 LLM/用户看的格式
- `build_system_prompt / build_suggestions_prompt`
- `parse_suggestions / parse_agent_response`
- `apply_modification`
- `initial_prompt_for_user`

---

## 五、ziv_agent_v5 接入示例

假设你的 ziv_agent 主入口是这样:

```python
# ziv_agent_v5/main.py 或类似
from agent.core import Agent
agent = Agent(profile="creative_writer")
result = agent.run(user_input="...", context={...})
```

那么接入只需要新写一个文件:

```python
# tools/long_writer/creator_agent/engine_ziv.py
"""ZivAgent 引擎适配器"""

from creator_agent.engine import LLMEngine, AgentResponse

class ZivAgentEngine(LLMEngine):
    def __init__(self, profile="creative_writer"):
        # 这里 import 你的 ziv_agent
        from ziv_agent_v5.agent.core import Agent
        self._agent = Agent(profile=profile)

    def propose_suggestions(self, state, facet):
        prompt = facet.build_suggestions_prompt(state)
        # 你的 agent 可能返回更结构化的对象,转成 raw 字符串再让 facet 解析
        raw = self._agent.run(
            user_input=prompt,
            context={"state": state, "stage": facet.name},
        )
        # 如果你的 agent 已经返回 list[Suggestion] 就直接 return
        return facet.parse_suggestions(raw if isinstance(raw, str) else str(raw))

    def respond_to_user(self, state, facet, user_msg, history):
        system_prompt = facet.build_system_prompt(state)
        directive = facet.build_response_directive()

        raw = self._agent.run(
            user_input=user_msg + "\n\n" + directive,
            context={
                "system": system_prompt,
                "history": history,
                "state": state,
                "stage": facet.name,
            },
        )
        return facet.parse_agent_response(raw if isinstance(raw, str) else str(raw))
```

然后 cli 里加一个 flag 让用户选 engine:

```python
# cli.py 修改 cmd_outline / cmd_chapters
def cmd_outline(args):
    if args.interactive:
        # 选 engine
        if args.engine == "ziv":
            from long_writer.creator_agent.engine_ziv import ZivAgentEngine
            engine = ZivAgentEngine(profile="creative_writer")
        else:
            from long_writer.creator_agent import SimpleLLMEngine
            engine = SimpleLLMEngine()

        from long_writer.creator_agent import CreatorAgent
        from long_writer.creator_agent.facets import OutlineFacet
        facet = OutlineFacet()
        facet.seed_concept = args.concept
        # ...
        agent = CreatorAgent(facet=facet, engine=engine)
        agent.run()
```

**就这些。** 所有现有 facet、所有现有 cli 子命令、所有控制命令、所有产物路径,完全不动。

---

## 六、能力扩展点(智能体可以做的额外事)

你的智能体相比 `SimpleLLMEngine`,可能有以下能力。这些都可以在 engine 实现里发挥,但不需要修改 facet/agent:

### 6.1 多步推理

`SimpleLLMEngine.respond_to_user` 是单轮调用。你的 agent 可以做:
- LLM 看完用户输入 → 觉得需要先搜资料 → 调工具 → 再思考 → 才回应

这些都封装在 `ZivAgentEngine.respond_to_user` 内部,外层 `CreatorAgent` 看不见过程,只接收最终 `AgentResponse`。

### 6.2 长期记忆 / profile

你的 ziv_agent 有 `memory.py / store.py / profiles/`,可以记录:
- 用户偏好(喜欢的恐怖风格、避雷的元素)
- 历史作品风格
- 多次会话间的状态

`facet` 给的 `state` 只是当前作品的精炼对象;**用户跨作品的偏好可以由你的 agent 自己持久化**,不通过 `state` 流转。这是干净的分层。

### 6.3 工具调用

你的 ziv_agent 有 `tools/`(comfyui, code, web, office, ...)可以在写作精炼时调用:
- web 搜:用户问"魏公村真的有古井吗?"→ 搜真实历史
- comfyui:对话中即时出一张氛围图给用户看,辅助决策
- file:读取参考资料

这些都在 engine 内部进行,**facet 完全无感**。

---

## 七、向后兼容承诺

接入新引擎后,以下保持不变:

| 不变的事 | 含义 |
|---|---|
| 命令行参数 | `python -m ... outline --concept ... --interactive` 仍然这样调 |
| 默认 engine | 不加 `--engine` 时仍用 `SimpleLLMEngine`(直接调 ChatOpenAI) |
| 产物路径 | `scripts/<story_id>_outline.yaml`、`scripts/<story_id>.md` 仍然这样 |
| 下游对接 | `chapter_writer / script_doctors / long_storyboard / to_pipeline / coordinator / producer` 一行不动 |
| 控制命令 | `/done /back /save /show /help /quit` 行为一致 |

---

## 八、当前实现状态

| 组件 | 状态 |
|---|---|
| `CreatorAgent` (agent.py) | ✓ 完成 |
| `LLMEngine` 抽象 | ✓ 完成 |
| `SimpleLLMEngine` (默认) | ✓ 完成 |
| `OutlineFacet` | ✓ 完成 |
| `ChapterFacet` | ✓ 完成 |
| `StoryboardFacet` | ✗ 待做(将来你接入智能体后做) |
| cli `--interactive` flag | ✓ outline / chapters 已加 |
| 单元测试 | ✓ 17 项全过 |
| `ZivAgentEngine` 适配器 | ✗ 待你接入(本文档第五节有示例) |

---

## 九、对接 storyboard 阶段的设想(将来工作)

`StoryboardFacet` 将来会做什么:
- 加载 `scripts/<id>_segments.yaml` 或 `scripts/<id>_storyboard.yaml`
- 让用户和 LLM 讨论:镜头分配 / 视觉效果 / 节奏 / 镜头级精修
- 产出 `scripts/<id>_storyboard.yaml`,跟现有 `long_storyboard` 契约一致
- 之后跑 `to_pipeline.convert_to_pipeline()` 进入 stories/

这个 facet 跟 outline/chapter facet 是同构的,做法一样:实现 base.Facet 的几个方法即可。**不需要碰 cli、agent、engine**。

---

## 十、问题与反馈

如果接入过程中发现:
- `LLMEngine` 接口不够用,需要新方法
- facet 提供的辅助方法不够,LLM 没法精确产出 ops

请反馈,我们可以在 v0.2 加新方法,但**保持向后兼容**(已实现的 SimpleLLMEngine / OutlineFacet / ChapterFacet 不破)。
