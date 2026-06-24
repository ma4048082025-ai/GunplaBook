"""
creator_agent.agent
===================
通用对话循环。
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .engine import LLMEngine, AgentResponse
from .facets.base import Facet, Modification


# ════════════════════════════════════════════════════════════════
# 控制命令
# ════════════════════════════════════════════════════════════════
CTRL_COMMANDS = {
    "/done":  "完成,保存并退出",
    "/back":  "撤销最近一次采纳的修改",
    "/save":  "立即保存当前 state",
    "/show":  "展示当前完整 state",
    "/hint":  "再让 LLM 提几条改进建议",
    "/help":  "显示帮助",
    "/quit":  "中止,不保存最后未确认的修改",
}

# 这些裸词等同于对应的 /xxx 控制命令
_BARE_TO_CMD = {
    "done": "/done", "quit": "/quit", "exit": "/quit",
    "help": "/help", "show": "/show", "save": "/save",
    "back": "/back", "hint": "/hint", "提示": "/hint",
    "完成": "/done", "退出": "/quit", "保存": "/save",
}


def _normalize_cmd(s: str) -> str:
    """全角斜杠 + 裸词归一化。"""
    s = s.strip().replace("／", "/")
    if s.startswith("/"):
        return s.lower()
    if s.lower() in _BARE_TO_CMD:
        return _BARE_TO_CMD[s.lower()]
    return s


# ════════════════════════════════════════════════════════════════
# 主类
# ════════════════════════════════════════════════════════════════

class CreatorAgent:
    """对话式写作引导器。"""

    def __init__(self, facet: Facet, engine: LLMEngine,
                 max_turns: int = 50,
                 auto_save: bool = True,
                 show_initial_hints: bool = True):
        self.facet = facet
        self.engine = engine
        self.max_turns = max_turns
        self.auto_save = auto_save
        self.show_initial_hints = show_initial_hints

        self.state: dict = {}
        self.history: list = []
        self.state_snapshots: list = []
        self.story_id: str = ""

    # ────────────────────────────────────────────────────────
    # 主入口
    # ────────────────────────────────────────────────────────

    def run(self, initial_source: Optional[Path] = None,
            story_id: str = "") -> dict:
        # 1. load state
        self.state = self.facet.load_state(initial_source)
        self.story_id = story_id or self.state.get("story_id", "")

        # 2. 开场 + 帮助
        self._print_banner()
        self._print(self.facet.initial_prompt_for_user(self.state), color="agent")

        # 2.5 主动列出建议(新增)
        if self.show_initial_hints:
            self._show_suggestions()

        # 3. 主循环
        turn = 0
        while turn < self.max_turns:
            turn += 1
            user_msg = self._read_user_input()
            if user_msg is None:
                self._print("\n  (中止)", color="warn")
                return self.state

            # 控制命令(支持裸词和全角)
            normalized = _normalize_cmd(user_msg)
            if normalized.startswith("/"):
                if self._handle_ctrl_command(normalized):
                    break
                continue

            # 普通对话
            self.history.append({"role": "user", "content": user_msg})
            response = self.engine.respond_to_user(
                state=self.state,
                facet=self.facet,
                user_msg=user_msg,
                history=self.history[:-1],
            )

            self._print(response.message, color="agent")
            self.history.append({"role": "agent", "content": response.message})

            if response.proposed_modification:
                accepted = self._prompt_accept_modification(
                    response.proposed_modification)
                if accepted is not None:
                    # accepted 可能是 LLM 原始的 modification,
                    # 也可能是 _resolve_candidate 后的单选定稿
                    self._apply_and_save(accepted)

            if response.done:
                self._print(
                    "\n  (LLM 提示对话可以结束;输入 /done 确认,或继续讨论)",
                    color="hint")

        return self._finalize()

    # ────────────────────────────────────────────────────────
    # 主动展示建议
    # ────────────────────────────────────────────────────────

    def _show_suggestions(self):
        """让 LLM 看 state,提 3-5 条建议,展示给用户。"""
        try:
            suggestions = self.engine.propose_suggestions(self.state, self.facet)
        except Exception as e:
            self._print(f"  (生成建议失败,直接对话也行: {e})", color="hint")
            return
        if not suggestions:
            self._print("  (暂无建议,直接告诉我你想改什么吧)", color="hint")
            return
        print()
        for i, s in enumerate(suggestions, 1):
            print(f"  {i}. \033[1m{s.label}\033[0m")
            print(f"     {s.rationale}")
            if s.focus_area:
                print(f"     (涉及: {s.focus_area})")
            print()
        print("  你可以说\"按第 N 条改\",提出自己的想法,或问任何问题。")
        print("  输入 /help 看所有控制命令。")

    # ────────────────────────────────────────────────────────
    # 控制命令处理
    # ────────────────────────────────────────────────────────

    def _handle_ctrl_command(self, cmd: str) -> bool:
        cmd = cmd.strip().lower()

        if cmd in ("/done", "/quit"):
            if cmd == "/quit":
                self._print("  (退出,不保存最后未确认修改)", color="warn")
            return True

        if cmd == "/help":
            print()
            for c, desc in CTRL_COMMANDS.items():
                print(f"  {c:8s}  {desc}")
            print()
            print("  也可以直接输入 done / save / back / help (无需 /)")
            print()
            return False

        if cmd == "/show":
            print()
            print(self.facet.serialize_state(self.state))
            print()
            return False

        if cmd == "/save":
            self._save()
            return False

        if cmd == "/hint":
            self._show_suggestions()
            return False

        if cmd == "/back":
            if not self.state_snapshots:
                self._print("  (没有可撤销的修改)", color="warn")
            else:
                self.state = self.state_snapshots.pop()
                self._save()
                self._print("  ✓ 已撤销上一次修改", color="ok")
            return False

        self._print(f"  未知命令 {cmd!r},输入 /help 查看帮助", color="warn")
        return False

    # ────────────────────────────────────────────────────────
    # 修改采纳
    # ────────────────────────────────────────────────────────

    def _detect_candidate_list(self, modif: Modification):
        """检测 ops 是不是同字段被写多次(LLM 在列候选,不是定稿)。

        如果检测到,返回 (field, [value1, value2, ...]) 让 agent 让用户选。
        否则返回 None。
        """
        if not modif.ops or len(modif.ops) < 2:
            return None
        fields = [op.get("field", "") for op in modif.ops]
        if len(set(fields)) != 1:
            return None   # 字段不同,不是候选列表
        field = fields[0]
        values = [op.get("value") for op in modif.ops]
        # 必须每个 value 都不同,否则可能是 LLM 重复 op,不是候选
        if len(set(str(v) for v in values)) < 2:
            return None
        return field, values

    def _resolve_candidate(self, field: str, values: list,
                           description: str) -> Optional[Modification]:
        """LLM 列了候选,让用户挑一个。返回单选后的 Modification 或 None。"""
        print()
        print("  ── LLM 列了候选,但还没定稿。请挑一个: ──")
        print(f"  字段: {field}")
        for i, v in enumerate(values, 1):
            print(f"    {i}) {v}")
        print(f"    0) 都不要,继续聊")
        while True:
            ans = input(f"  选 0-{len(values)} > ").strip()
            if ans == "0":
                return None
            if ans.isdigit() and 1 <= int(ans) <= len(values):
                chosen = values[int(ans)-1]
                return Modification(
                    description=f"{description} (用户从候选中选: {chosen})",
                    ops=[{"action": "set", "field": field, "value": chosen}],
                )
            print(f"  请输入 0-{len(values)}")

    def _prompt_accept_modification(self, modif: Modification) -> Optional[Modification]:
        """问用户要不要采纳。返回:
        - None: 不采纳
        - Modification: 采纳的 modification (可能跟原 modif 一样,也可能被 resolve 过)
        """
        # 先检测是不是 LLM 误把候选写成 ops
        cand = self._detect_candidate_list(modif)
        if cand:
            field, values = cand
            resolved = self._resolve_candidate(field, values, modif.description)
            return resolved   # 可能是 None (用户选 0) 或新的 single-op Modification

        # 正常 modification,常规采纳流程
        print()
        print("  ── 修改建议 ──")
        print(f"  {modif.description}")
        for op in modif.ops:
            v = op.get("value")
            v_str = ""
            if isinstance(v, (str, int, float, bool)):
                v_str = f" = {v!r}"
            print(f"    • {op.get('action')}: {op.get('field')}{v_str}")
        while True:
            ans = input("  [a]采纳 / [r]我再想想 / [n]不要 > ").strip().lower()
            if ans in ("a", "采纳", "y", "yes"):
                return modif
            if ans in ("n", "不要", "no", "r", "再想想"):
                return None
            print("  请输入 a / r / n")

    def _apply_and_save(self, modif: Modification):
        import copy
        self.state_snapshots.append(copy.deepcopy(self.state))

        try:
            self.state = self.facet.apply_modification(self.state, modif)
            self._print(f"  ✓ 已应用: {modif.description}", color="ok")
            if self.auto_save:
                self._save()
        except Exception as e:
            self.state = self.state_snapshots.pop()
            self._print(f"  ⚠ 应用失败,已回滚: {e}", color="warn")

    def _save(self):
        try:
            main_path = self.facet.save_state(self.state, self.story_id)
            self.facet.save_version_snapshot(self.state, self.story_id)
            self._print(f"  ✓ 已保存: {main_path}", color="ok")
        except Exception as e:
            self._print(f"  ⚠ 保存失败: {e}", color="warn")

    # ────────────────────────────────────────────────────────
    # 收尾
    # ────────────────────────────────────────────────────────

    def _finalize(self) -> dict:
        self._save()
        print()
        print("  " + "─" * 56)
        print(f"  ✓ 对话结束。最终状态:")
        print()
        print(self.facet.state_summary(self.state))
        print()
        return self.state

    # ────────────────────────────────────────────────────────
    # IO 辅助
    # ────────────────────────────────────────────────────────

    def _read_user_input(self) -> Optional[str]:
        try:
            line = input("\n  > ")
            # 清洗 surrogate(防止终端 input 把字节假装成字符串塞进来)
            try:
                line.encode('utf-8')
            except UnicodeEncodeError:
                line = line.encode('utf-8', 'surrogateescape').decode('utf-8', 'replace')
            line = line.strip()
            return line if line else self._read_user_input()
        except (EOFError, KeyboardInterrupt):
            return None

    _COLOR_CODES = {
        "agent": "\033[36m",
        "ok":    "\033[32m",
        "warn":  "\033[33m",
        "hint":  "\033[90m",
        "reset": "\033[0m",
    }

    def _print(self, text: str, color: str = "agent"):
        c = self._COLOR_CODES.get(color, "")
        r = self._COLOR_CODES["reset"]
        prefix = "  " if color == "agent" else ""
        print(f"{prefix}{c}{text}{r}")

    def _print_banner(self):
        print()
        print("  " + "═" * 56)
        print(f"  {self.facet.name} 对话式精炼")
        print("  " + "═" * 56)
        print()
        print("  你可以直接说话(\"加个配角\"、\"标题改成X\"、\"按第2条建议改\"),")
        print("  也可以用控制命令:")
        print("    /help  /show  /save  /back  /hint  /done  /quit")
        print("  (裸词 done/save/back/help 也行,不用打斜杠)")
        print()
