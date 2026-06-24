"""
creator_agent.facets.base
=========================
Facet 是"写作阶段"的抽象。
"""

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


# ════════════════════════════════════════════════════════════════
# scripts 目录解析(所有 facet 共用)
# ════════════════════════════════════════════════════════════════
#
# 优先级:
#   1. 环境变量 LONG_WRITER_SCRIPTS_DIR (绝对路径,最优先)
#   2. cwd/scripts (跟 generate_outline 写入的位置一致)
#   3. <仓库根>/scripts (从本文件位置往上找到工程根)
#
# 一旦解析成功就缓存,避免重复 stat。
# ════════════════════════════════════════════════════════════════

_SCRIPTS_DIR_CACHE: Optional[Path] = None


def get_scripts_dir() -> Path:
    """返回应该写 outline.yaml / segments.yaml / .md 的目录。

    永远返回一个存在的目录(若不存在会创建)。
    """
    global _SCRIPTS_DIR_CACHE
    if _SCRIPTS_DIR_CACHE is not None:
        return _SCRIPTS_DIR_CACHE

    # 1. 环境变量
    env = os.environ.get("LONG_WRITER_SCRIPTS_DIR")
    if env:
        p = Path(env).expanduser().resolve()
        p.mkdir(parents=True, exist_ok=True)
        _SCRIPTS_DIR_CACHE = p
        return p

    # 2. cwd/scripts (这是 generate_outline 的默认行为,优先匹配以保持一致)
    cwd_scripts = Path.cwd() / "scripts"
    if cwd_scripts.exists():
        _SCRIPTS_DIR_CACHE = cwd_scripts.resolve()
        return _SCRIPTS_DIR_CACHE

    # 3. 从本文件位置往上找,找到"包含 scripts/ 子目录"的祖先
    #    base.py 在 tools/long_writer/creator_agent/facets/ 下
    here = Path(__file__).resolve()
    for parent in here.parents:
        cand = parent / "scripts"
        if cand.exists() and cand.is_dir():
            _SCRIPTS_DIR_CACHE = cand
            return cand

    # 4. 啥也没找到,在 cwd 下创建 scripts/
    cwd_scripts.mkdir(parents=True, exist_ok=True)
    _SCRIPTS_DIR_CACHE = cwd_scripts.resolve()
    return _SCRIPTS_DIR_CACHE


def get_backup_dir() -> Path:
    """返回版本副本目录,默认是 <scripts_dir>/版本副本/。"""
    p = get_scripts_dir() / "版本副本"
    p.mkdir(parents=True, exist_ok=True)
    return p


# ════════════════════════════════════════════════════════════════
# 数据契约 (engine 和 facet 共享)
# ════════════════════════════════════════════════════════════════

@dataclass
class Suggestion:
    """LLM 提的一条改进建议。"""
    label: str          # 简短标签,显示给用户挑选时用 (≤ 20 字)
    rationale: str      # 详细理由,展示给用户看
    focus_area: str = ""  # 可选,这条建议涉及的字段/区域


@dataclass
class Modification:
    """LLM 想做的一次具体修改。"""
    description: str
    ops: list = field(default_factory=list)


# ════════════════════════════════════════════════════════════════
# Facet 抽象基类
# ════════════════════════════════════════════════════════════════

class Facet:
    """写作阶段的抽象。"""

    name: str = "base"
    description: str = "写作阶段基类"

    # ─── 状态 IO ──────────────────────────────────

    def load_state(self, source_path: Optional[Path]) -> dict:
        raise NotImplementedError

    def save_state(self, state: dict, story_id: str) -> Path:
        raise NotImplementedError

    def save_version_snapshot(self, state: dict, story_id: str) -> Path:
        from datetime import datetime
        import yaml

        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_root = get_backup_dir()
        sid = story_id or "long_unnamed"
        backup_dir = backup_root / sid
        backup_dir.mkdir(parents=True, exist_ok=True)
        path = backup_dir / f"{ts}_{self.name}.yaml"
        with open(path, "w", encoding="utf-8") as f:
            yaml.dump(state, f, allow_unicode=True,
                      default_flow_style=False, sort_keys=False)
        return path

    # ─── 状态序列化 ────────────────────────────────

    def serialize_state(self, state: dict) -> str:
        raise NotImplementedError

    def state_summary(self, state: dict) -> str:
        raise NotImplementedError

    # ─── prompt 构造 ──────────────────────────────

    def build_system_prompt(self, state: dict) -> str:
        raise NotImplementedError

    def build_suggestions_prompt(self, state: dict) -> str:
        raise NotImplementedError

    def build_response_directive(self) -> str:
        return """请用 JSON 格式回应,严格遵循以下结构:

{
  "message": "给用户看的自然语言回应,可以包含问题、解释、建议",
  "proposed_modification": {
    "description": "对修改的简短人话描述",
    "ops": [
      {"action": "set", "field": "<字段路径>", "value": <新值>}
    ]
  },
  "done": false
}

规则:
  - 如果你只是在回答用户的问题或追问细节,proposed_modification 设为 null
  - 如果用户表达了明确意图(如"加配角""把章节拉长"),给出 proposed_modification
  - done 默认 false; 只有当用户明确说"够了""完成"且没有未完成的修改时才为 true
  - 只返回 JSON,不要加额外的说明或代码块标记

【关键约束 - 列候选 vs 给定稿】
  - 如果你给用户多个候选让他选(如"我给你 5 个名字: A/B/C/D/E,你选哪个?"),
    proposed_modification 必须为 null。等用户选定后,下一轮才给 modification。
  - 一个 modification 的 ops 里,每个 field 路径只能出现一次。
    不允许对同一字段(如 characters[0].name)写多次,那不是修改,那是列候选。
    如果你想让用户选,写 message 里用文字列,不要写进 ops。
  - 如果你已经决定了具体值,只给一个 op,value 就是那个具体值。"""

    # ─── LLM 输出解析 ─────────────────────────────

    def parse_suggestions(self, raw: str) -> "list[Suggestion]":
        raise NotImplementedError

    def parse_agent_response(self, raw: str):
        """把 LLM 返回的对话回应解析为 AgentResponse 对象。"""
        import json
        # 用相对导入,避免依赖顶层包名 'creator_agent' 是否在 sys.path
        from ..engine import AgentResponse

        cleaned = raw.strip()
        if cleaned.startswith("```"):
            lines = cleaned.split("\n")
            cleaned = "\n".join(lines[1:-1] if lines[-1].startswith("```")
                               else lines[1:])

        try:
            data = json.loads(cleaned)
        except json.JSONDecodeError:
            return AgentResponse(
                message=raw.strip(),
                proposed_modification=None,
                done=False,
            )

        modif = None
        if data.get("proposed_modification"):
            pm = data["proposed_modification"]
            modif = Modification(
                description=pm.get("description", ""),
                ops=pm.get("ops", []),
            )

        return AgentResponse(
            message=data.get("message", ""),
            proposed_modification=modif,
            done=bool(data.get("done", False)),
        )

    # ─── 修改应用 ─────────────────────────────────

    def apply_modification(self, state: dict, modif: Modification) -> dict:
        raise NotImplementedError

    # ─── 入口提示 ─────────────────────────────────

    def initial_prompt_for_user(self, state: dict) -> str:
        raise NotImplementedError
