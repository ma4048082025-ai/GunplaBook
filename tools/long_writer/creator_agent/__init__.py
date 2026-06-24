"""
creator_agent
=============
长文写作的对话式精炼引擎。

通用对话循环 + 不同写作阶段的 facet (outline / chapter / 将来 storyboard)。

接入说明见 CREATOR_AGENT_CONTRACT.md。
"""

from .agent import CreatorAgent
from .engine import LLMEngine, SimpleLLMEngine, AgentResponse
from .facets.base import Facet, Suggestion, Modification

__all__ = [
    "CreatorAgent",
    "LLMEngine", "SimpleLLMEngine", "AgentResponse",
    "Facet", "Suggestion", "Modification",
]
