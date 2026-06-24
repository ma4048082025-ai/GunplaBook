"""
creator_agent.engine
====================
LLMEngine 是一个抽象层:**你将来要接入的智能体框架替换这一层**。

为什么有这一层:
  - SimpleLLMEngine = 直接调 ChatOpenAI,跟现有 outline.py / chapter_writer.py
    用同一个 LLM (LLM_MODEL/LLM_BASE_URL/LLM_API_KEY)
  - 将来你接入 ziv_agent_v5 (或其他智能体框架)时,只写一个 ZivAgentEngine
    实现 LLMEngine 接口,把当前的 SimpleLLMEngine 替换掉即可
  - facets/agent.py 不需要任何修改

LLMEngine 必须实现的两件事:
  1. propose_suggestions(state, facet) → list[Suggestion]
     给当前状态 + facet 知识,产出"我建议你考虑这几点"
  2. respond_to_user(state, facet, user_msg, history) → AgentResponse
     用户说话后,回应 + 可能附带一个修改建议(待用户采纳)
"""

import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from .facets.base import Facet, Suggestion, Modification


# ════════════════════════════════════════════════════════════════
# 数据契约
# ════════════════════════════════════════════════════════════════

@dataclass
class AgentResponse:
    """LLM 回应用户的一次发言。"""
    message: str
    proposed_modification: "Optional[Modification]" = None
    done: bool = False


# ════════════════════════════════════════════════════════════════
# 工具函数: surrogate 清洗
# ════════════════════════════════════════════════════════════════

# 控制开关:跑出第一份诊断输出后,可以把它关掉(改成 False)避免噪音。
# 但 _scrub 兜底始终生效。
DIAG_SURROGATE = True


def _scrub(s):
    """把字符串里的 surrogate code point 清洗掉。

    surrogate (U+D800–U+DFFF) 不能合法地编进 UTF-8,常见来源:
      - 某些 yaml/json 文件被错误编码处理
      - LLM 返回的字节流没干净解码
      - 程序里用了 errors='surrogateescape' 解码二进制
    清洗策略:
      先用 surrogateescape 编回原始字节(把 \\udcXX 还原成 \\xXX),
      再用 utf-8 decode,errors='replace' 兜底把仍非法的换成 �。
    """
    if not isinstance(s, str):
        return s
    try:
        s.encode('utf-8')
        return s   # 干净的,直接返回
    except UnicodeEncodeError:
        return s.encode('utf-8', 'surrogateescape').decode('utf-8', 'replace')


def _diag_scan(label, content):
    """诊断:扫描一段文本是否含 surrogate,打印上下文。"""
    if not DIAG_SURROGATE or not isinstance(content, str):
        return
    try:
        content.encode('utf-8')
    except UnicodeEncodeError as e:
        lo = max(0, e.start - 80)
        hi = min(len(content), e.start + 80)
        before = content[lo:e.start]
        bad = content[e.start:e.start + 1]
        after = content[e.start + 1:hi]
        print(f"\n  [DIAG] {label} 位置 {e.start}/{len(content)} 有 surrogate")
        print(f"  [DIAG] 上下文: ...{before!r}")
        print(f"  [DIAG]   ★坏码点★: {bad!r} (U+{ord(bad):04X})")
        print(f"  [DIAG] 之后:   {after!r}...")


# ════════════════════════════════════════════════════════════════
# 抽象接口
# ════════════════════════════════════════════════════════════════

class LLMEngine:
    """对话引擎的抽象接口。"""

    def propose_suggestions(self, state, facet):
        raise NotImplementedError

    def respond_to_user(self, state, facet, user_msg, history):
        raise NotImplementedError


# ════════════════════════════════════════════════════════════════
# 默认实现 — 直接调 ChatOpenAI
# ════════════════════════════════════════════════════════════════

class SimpleLLMEngine(LLMEngine):
    """直接调 ChatOpenAI 的实现。"""

    def __init__(self, model=None, temperature=0.7):
        from config import LLM_API_KEY, LLM_BASE_URL, LLM_MODEL
        from langchain_openai import ChatOpenAI

        self._llm = ChatOpenAI(
            model=model or LLM_MODEL,
            api_key=LLM_API_KEY,
            base_url=LLM_BASE_URL,
            temperature=temperature,
        )

    # ────────────────────────────────────────────────────────
    # propose_suggestions
    # ────────────────────────────────────────────────────────

    def propose_suggestions(self, state, facet):
        from langchain_core.messages import HumanMessage

        prompt = facet.build_suggestions_prompt(state)
        _diag_scan("propose_suggestions prompt", prompt)
        prompt = _scrub(prompt)

        try:
            raw = self._llm.invoke([HumanMessage(content=prompt)]).content
        except Exception as e:
            print(f"  [engine] propose_suggestions 失败: {e}")
            return []
        raw = _scrub(raw)
        return facet.parse_suggestions(raw)

    # ────────────────────────────────────────────────────────
    # respond_to_user
    # ────────────────────────────────────────────────────────

    def respond_to_user(self, state, facet, user_msg, history):
        from langchain_core.messages import HumanMessage, AIMessage, SystemMessage

        system_text   = facet.build_system_prompt(state)
        directive     = facet.build_response_directive()

        # ─── 诊断: 把每段拼好的内容扫一遍 surrogate ───
        _diag_scan("system_prompt", system_text)
        for i, h in enumerate(history):
            _diag_scan(f"history[{i}].{h.get('role')}", h.get("content", ""))
        _diag_scan("user_msg", user_msg)
        _diag_scan("directive", directive)

        # ─── 清洗 ───
        system_text = _scrub(system_text)
        user_msg    = _scrub(user_msg)
        directive   = _scrub(directive)
        cleaned_history = [
            {"role": h["role"], "content": _scrub(h.get("content", ""))}
            for h in history
        ]

        # ─── 组装 messages ───
        msgs = [SystemMessage(content=system_text)]
        for h in cleaned_history:
            if h["role"] == "user":
                msgs.append(HumanMessage(content=h["content"]))
            else:
                msgs.append(AIMessage(content=h["content"]))
        msgs.append(HumanMessage(content=user_msg))
        msgs.append(HumanMessage(content=directive))

        try:
            raw = self._llm.invoke(msgs).content
        except Exception as e:
            print(f"  [engine] respond_to_user 失败: {e}")
            return AgentResponse(
                message=f"(LLM 调用失败: {e})",
                proposed_modification=None,
                done=False,
            )

        raw = _scrub(raw)
        return facet.parse_agent_response(raw)
