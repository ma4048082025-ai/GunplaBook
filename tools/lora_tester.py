"""
lora_tester.py ── LoRA Checkpoint 自动对比测试（v3.1.4）
=====================================================
v3.1.4 修复（关键修正之前 v3.1.1 的错误判断）：
  之前 v3.1.1 的结论"ComfyUI 只接受正斜杠"**是错的**。
  通过查询 ComfyUI 的 /object_info/LoraLoader 证实：
    - ComfyUI 在 Windows 服务器上用反斜杠存 LoRA 名（'flux\\xxx.safetensors'）
    - 做 prompt validation 时精确字符串匹配
    - Linux 服务器上才是正斜杠
  正确做法：查 ComfyUI 实际的名字列表，精确匹配。
  改动：
  - 新增 _resolve_lora_name_with_comfy()：通过 /object_info/LoraLoader 查真实名字
  - _generate_test_image 调用前先 resolve，拿到服务器精准认可的字符串
  - 根据 resolved 结果反向设 COMFY_SERVER_OS，防止 renderer 二次转换破坏结果
  - 缓存 ComfyUI LoRA 列表（进程内），避免每次生图都请求一次
  - 找不到匹配时用 difflib 给出最接近的候选，便于诊断

v3.1.3 增强：
  - _make_ssh 加指数退避重试（默认 5 次，3-6-12-24-48 秒间隔）
    应对跨境 SSH 偶发的 banner 失败 / connection closed / EOFError
  - banner_timeout 从 15s 延长到 30s（跨境网络更宽松）

v3.1.2 修复：
  - _ssh_copy_to_comfyui 拆分 mkdir 和 copy 命令
  - copy 通配符改为 {char_name}*.safetensors，避免混复制其他角色
  - copy 加 /Y 防止同名询问挂起
  - 失败时输出详细 stdout/stderr 便于诊断
  - 支持传入 file_names 精确复制

v3.1.1 【已被 v3.1.4 撤销】
  原以为 ComfyUI 只接受正斜杠，强行把 COMFY_SERVER_OS 切成 'linux'。
  实际相反——反斜杠才是 Windows ComfyUI 的原生格式。正确做法见 v3.1.4。

v3.1 修复：
  - 适配 lora_trainer.py 的版本化输出目录 output_v{N}/（之前只扫 output/，找不到产物）
  - find_checkpoints 自动挑 round 编号最大的 output_v* 目录扫描
  - SSH 和本地两种扫描路径都做了适配
  - 找不到 output_v* 时回退到老的 output/ 目录（向后兼容）

v3 新增（多 session 支持）：
  - 适配 lora_trainer.py v3 的时间戳 output_name 命名（{char}_flux_lora_r{timestamp}-stepN）
  - _parse_checkpoint_info() 解析新旧两种命名，返回 (session, step, sort_key)
  - TestResult/TestTracker 加 session 字段，断点续测联合 key 去重
  - generate_report() 多 session 模式显示 Session 列，便于对比续训效果
  - apply_test_result() 优先用 best_filename 精确匹配，不再靠 step 猜
  - 新增 --session 参数过滤（默认扫所有 session，可选 'latest' / 具体名 / '' legacy）

v2 原有：
  - find_checkpoints() 支持远程 SSH 扫描
  - save_test_result() 测试结果持久化到 test_results/roundN.json
  - apply_test_result() 测试通过后自动更新 story yaml + loras/flux/角色.yaml
  - generate_report() 扩展 pose 弱点分析

复用项目现有能力：
  1. renderer.generate_and_wait + comfy_generate_flux  → 生成测试图
  2. quality.LlavaGate                                 → 自动评分
  3. config.resolve_lora_path                          → 本地/远程路径透明化

用法（日常——零变化）：
  # 对比所有 checkpoint（包括所有 session）
  python lora_tester.py --story stories/haunted_inn.yaml --char innkeeper_ghost

  # 只测指定 step（同 step 有多 session 时都测，报告里 session 列区分）
  python lora_tester.py --story ... --char innkeeper_ghost --steps 500,1000,1500

  # 对推荐 checkpoint 做 strength 精调
  python lora_tester.py --story ... --char innkeeper_ghost --mode strength --best-step 1000

  # 强制重新测试
  python lora_tester.py --story ... --char innkeeper_ghost --force

  # 测试完自动写回 yaml（通过后才写）
  python lora_tester.py --story ... --char innkeeper_ghost --apply

用法（v3 新增——多 session 场景）：
  # 只测最新一次 session 的产物（快速验证续训结果）
  python lora_tester.py --story ... --char innkeeper_ghost --session latest

  # 只测指定 session（精细对比某次续训）
  python lora_tester.py --story ... --char innkeeper_ghost --session r20260422_1408

  # 只测旧的 legacy 命名（和续训产物做基准对比）
  python lora_tester.py --story ... --char innkeeper_ghost --session ""

输出：
  lora_workspace/{story}/{char}/test_results/
    ├── roundN.json                         ← 结构化测试结果（trainer round2 读这里）
    ├── report.txt                          ← 人类可读对比报告
    ├── test_status.json                    ← 断点续测状态（含 session 字段）
    ├── step00500_s0.8_front_close.png              ← legacy 命名图片
    ├── r20260422_1408_step00500_s0.8_front_close.png  ← 新 session 图片
    └── ...
"""

import argparse
import json
import random
import time
from datetime import datetime
from pathlib import Path
from dataclasses import dataclass
from typing import Optional

import paramiko
import yaml

from config import COMFY_SERVER, REMOTE_SSH, resolve_lora_path, COMFY_LORA_PREFIX
from renderer import generate_and_wait, comfy_generate_flux
from gpu_guard import gpu_guard


# ════════════════════════════════════════════════════════════════
# 测试 Prompt 模板
# ════════════════════════════════════════════════════════════════

def _build_test_prompts(char_cfg: dict, style_prefix: str) -> list[dict]:
    base_desc = (
        char_cfg.get("trigger_solo") or
        char_cfg.get("trigger_multi") or
        char_cfg.get("desc", "")
    )
    return [
        {
            "id": "front_close",
            "label": "正脸特写",
            "pose_type": "正脸",
            "prompt": f"{style_prefix}{base_desc}, "
                      f"facing camera directly, upper body portrait, "
                      f"warm candlelight, ancient chinese inn interior, "
                      f"high quality, detailed face",
            "weight": 1.5,
        },
        {
            "id": "three_quarter",
            "label": "三分侧面",
            "pose_type": "正脸",
            "prompt": f"{style_prefix}{base_desc}, "
                      f"three-quarter view, turning slightly to the side, "
                      f"cold blue moonlight, misty night atmosphere, "
                      f"medium shot, high quality",
            "weight": 1.2,
        },
        {
            "id": "side_profile",
            "label": "纯侧面",
            "pose_type": "侧面",
            "prompt": f"{style_prefix}{base_desc}, "
                      f"side profile view, facing left, "
                      f"dramatic side lighting, deep shadows, "
                      f"full body, ancient courtyard, high quality",
            "weight": 1.0,
        },
        {
            "id": "distant_wide",
            "label": "远景全身",
            "pose_type": "远景",
            "prompt": f"{style_prefix}{base_desc}, "
                      f"wide shot, full body, standing in misty mountain path, "
                      f"small figure in vast landscape, "
                      f"cold atmosphere, traditional chinese landscape, high quality",
            "weight": 0.8,
        },
        {
            "id": "action_dynamic",
            "label": "动态动作",
            "pose_type": "动作",
            "prompt": f"{style_prefix}{base_desc}, "
                      f"dynamic pose, reaching forward with one hand, "
                      f"hair flowing in wind, dramatic backlight, "
                      f"full body, ancient temple steps, high quality",
            "weight": 1.0,
        },
    ]


# ════════════════════════════════════════════════════════════════
# 状态追踪（断点续测）—— v3：加 session 字段
# ════════════════════════════════════════════════════════════════

@dataclass
class TestResult:
    checkpoint: str
    step: int
    strength: float
    prompt_id: str
    prompt_label: str
    image_path: str
    score: float
    feedback: str = ""
    session: str = ""          # v3: session 前缀，"" 表示旧 legacy 命名


class TestTracker:
    def __init__(self, status_path: Path):
        self.path = status_path
        self.results: list[dict] = []
        self._load()

    def _load(self):
        if self.path.exists():
            try:
                self.results = json.loads(self.path.read_text(encoding="utf-8"))
            except Exception:
                self.results = []

    def _save(self):
        self.path.write_text(
            json.dumps(self.results, ensure_ascii=False, indent=2),
            encoding="utf-8")

    def is_done(self, step: int, strength: float, prompt_id: str,
                session: str = "") -> bool:
        """
        判断某个测试组合是否已完成。
        联合 key: (session, step, strength, prompt_id)
        旧记录没有 session 字段，get() 返回 "" 对应 legacy 命名。
        """
        return any(
            r["step"] == step
            and r.get("session", "") == session
            and abs(r["strength"] - strength) < 0.01
            and r["prompt_id"] == prompt_id
            for r in self.results
        )

    def record(self, result: TestResult):
        self.results.append({
            "checkpoint":   result.checkpoint,
            "step":         result.step,
            "session":      result.session,        # v3
            "strength":     result.strength,
            "prompt_id":    result.prompt_id,
            "prompt_label": result.prompt_label,
            "image_path":   result.image_path,
            "score":        result.score,
            "feedback":     result.feedback,
        })
        self._save()

    def clear(self):
        self.results = []
        self._save()


# ════════════════════════════════════════════════════════════════
# Checkpoint 发现 —— v3：支持多 session 命名
# ════════════════════════════════════════════════════════════════

# Windows 路径常量（与 lora_trainer.py 对齐）
_WIN_WORKSPACE   = "C:/flux_train/workspace"
_WIN_COMFY_LORAS = "H:/ComfyUI/ComfyUI_windows_portable/ComfyUI/models/loras/flux"


def _make_ssh(max_retries: int = 5, retry_delay: float = 3.0) -> paramiko.SSHClient:
    """
    建立 SSH 连接，带自动重试。

    v3.1.3 新增：
      远程 SSH 服务器（通常跨境线路）容易偶发：
        - Connection closed by <host>
        - Error reading SSH protocol banner
        - EOFError
        - TimeoutError
      这些都是服务端临时拒绝（fail2ban / MaxStartups 抖动等），重试几次即可。

      采用指数退避：每次失败后等待时间翻倍，避免重试本身加剧服务端压力。

    参数：
      max_retries:  最大尝试次数（含首次），默认 5
      retry_delay:  首次重试间隔秒数，默认 3.0；之后每次翻倍（3, 6, 12, 24...）

    抛出：
      最后一次失败时抛出原始异常
    """
    last_exc = None
    delay = retry_delay
    for attempt in range(1, max_retries + 1):
        try:
            ssh = paramiko.SSHClient()
            ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            ssh.connect(
                REMOTE_SSH["host"], port=REMOTE_SSH["port"],
                username=REMOTE_SSH["username"], password=REMOTE_SSH["password"],
                timeout=15,
                banner_timeout=30,    # 给 banner 协商更长时间（默认 15s 跨境不够）
                auth_timeout=20,
            )
            if attempt > 1:
                print(f"  [SSH] 第 {attempt} 次尝试连接成功")
            return ssh
        except (paramiko.ssh_exception.SSHException,
                EOFError, TimeoutError, ConnectionResetError,
                OSError) as e:
            last_exc = e
            if attempt < max_retries:
                print(f"  [SSH] 第 {attempt}/{max_retries} 次尝试失败: {type(e).__name__}: {e}")
                print(f"        {delay:.0f} 秒后重试...")
                time.sleep(delay)
                delay *= 2   # 指数退避
            else:
                print(f"  [SSH] 已尝试 {max_retries} 次均失败")

    # 最后一次仍然失败，抛出原异常
    raise last_exc if last_exc else RuntimeError("SSH 连接失败，未知原因")


def _ssh_list_dir(ssh: paramiko.SSHClient, win_dir: str,
                  name_filter: str = "") -> list[str]:
    """列出 Windows 目录下的 .safetensors 文件名"""
    win_dir_bs = win_dir.replace("/", "\\")
    cmd = f'dir /B "{win_dir_bs}\\*.safetensors" 2>nul'
    try:
        _, stdout, _ = ssh.exec_command(cmd, timeout=15)
        output = stdout.read().decode(errors="ignore").strip()
        if not output:
            return []
        files = [f.strip() for f in output.splitlines() if f.strip()]
        if name_filter:
            files = [f for f in files if name_filter in f]
        return files
    except Exception as e:
        print(f"  [SSH] 列目录失败 {win_dir}: {e}")
        return []


def _ssh_copy_to_comfyui(ssh: paramiko.SSHClient,
                         output_dir: str, char_name: str,
                         file_names: list[str] = None) -> bool:
    """
    把 output_v*/ 的 .safetensors 复制到 ComfyUI loras/flux/。

    v3.1.2 修复：
      - 分离 mkdir 和 copy 命令（之前 mkdir&&copy 在目录已存在时会中断）
      - 按 char_name 过滤通配符（之前 *.safetensors 会复制所有 角色 的文件）
      - 支持指定 file_names 列表，只复制需要的文件（可选，None 则按 char_name 全复制）
      - 复制后验证目标目录是否真的有文件
      - 失败时输出更详细的诊断信息

    参数：
      file_names:  要复制的文件名列表（不含路径）。None 表示复制所有 char_name 相关文件
    """
    src = output_dir.replace("/", "\\")
    dst = _WIN_COMFY_LORAS.replace("/", "\\")

    # Step 1: 确保目标目录存在（不用 && 连接，避免 mkdir 失败影响 copy）
    mkdir_cmd = f'if not exist "{dst}" mkdir "{dst}"'
    try:
        _, stdout, _ = ssh.exec_command(mkdir_cmd, timeout=10)
        stdout.channel.recv_exit_status()  # 等完成，忽略 exit code
    except Exception as e:
        print(f"  !! 创建目标目录失败: {e}")
        return False

    # Step 2: 决定要复制哪些文件
    if file_names:
        print(f"  [复制] {len(file_names)} 个指定文件: {src} → {dst}")
        copy_pattern = None  # 用精确文件名逐个复制
    else:
        print(f"  [复制] 所有 {char_name} 相关: {src}\\{char_name}*.safetensors → {dst}")
        copy_pattern = f'{char_name}*.safetensors'

    # Step 3: 执行复制
    try:
        if copy_pattern:
            # 批量模式：用通配符 + 角色名前缀过滤
            # /Y 覆盖同名不询问，防止挂起
            cmd = f'copy /Y "{src}\\{copy_pattern}" "{dst}\\"'
            _, stdout, stderr = ssh.exec_command(cmd, timeout=120)
            exit_code = stdout.channel.recv_exit_status()
            out = stdout.read().decode(errors="ignore").strip()
            err = stderr.read().decode(errors="ignore").strip()

            if exit_code == 0:
                # Windows copy 成功会输出 "N 个文件已复制"
                print(f"  ✓ 复制完成：{out or '无输出'}")
                return True
            else:
                print(f"  !! copy 命令 exit_code={exit_code}")
                if out:
                    print(f"     stdout: {out}")
                if err:
                    print(f"     stderr: {err}")
                return False
        else:
            # 精确模式：逐个复制，避免通配符问题
            ok_count = 0
            for fname in file_names:
                cmd = f'copy /Y "{src}\\{fname}" "{dst}\\"'
                _, stdout, stderr = ssh.exec_command(cmd, timeout=30)
                exit_code = stdout.channel.recv_exit_status()
                if exit_code == 0:
                    ok_count += 1
                else:
                    err = stderr.read().decode(errors="ignore").strip()
                    print(f"    ✗ {fname}: {err or 'exit_code=' + str(exit_code)}")
            print(f"  ✓ 复制完成：{ok_count}/{len(file_names)} 个成功")
            return ok_count > 0

    except Exception as e:
        print(f"  !! 复制异常: {e}")
        return False


def _ssh_find_latest_round_dir(ssh: paramiko.SSHClient, char_dir: str) -> Optional[str]:
    """
    扫描 char_dir 下的 output_v* 目录，返回 round 编号最大的那个完整路径。

    char_dir: 形如 C:/flux_train/workspace/haunted_inn/scholar
    返回:    C:/flux_train/workspace/haunted_inn/scholar/output_v3
             找不到任何 output_v* 时返回 None
    """
    import re
    char_dir_bs = char_dir.replace("/", "\\")
    cmd = f'dir /B /AD "{char_dir_bs}\\output_v*" 2>nul'
    try:
        _, stdout, _ = ssh.exec_command(cmd, timeout=10)
        output = stdout.read().decode(errors="ignore").strip()
        if not output:
            return None
        max_round = -1
        max_dir = None
        for d in output.splitlines():
            d = d.strip()
            m = re.match(r"output_v(\d+)$", d)
            if m:
                n = int(m.group(1))
                if n > max_round:
                    max_round = n
                    max_dir = d
        if max_dir:
            return f"{char_dir}/{max_dir}"
        return None
    except Exception as e:
        print(f"  [SSH] 扫描 output_v* 目录失败: {e}")
        return None


def _local_find_latest_round_dir(char_workspace: Path) -> Optional[Path]:
    """
    本地版：扫描 char_workspace 下的 output_v* 目录，返回 round 最大的。
    找不到返回 None。
    """
    import re
    if not char_workspace.exists():
        return None
    candidates = []
    for d in char_workspace.iterdir():
        if not d.is_dir():
            continue
        m = re.match(r"output_v(\d+)$", d.name)
        if m:
            candidates.append((int(m.group(1)), d))
    if not candidates:
        return None
    candidates.sort(key=lambda x: x[0])
    return candidates[-1][1]


def _parse_checkpoint_info(filename: str, char_name: str) -> Optional[dict]:
    """
    从文件名解析 checkpoint 信息，支持新旧两种命名格式。

    支持的命名：
      旧:     {char}_flux_lora-step00000500.safetensors
              → {"session": "", "step": 500, "sort_key": 500}
      新:     {char}_flux_lora_r20260422_1408-step00000500.safetensors
              → {"session": "r20260422_1408", "step": 500, "sort_key": 大基数+...}
      最终:   {char}_flux_lora.safetensors
              → {"session": "", "step": 99999, "sort_key": 极大值, "is_final": True}

    返回 None 表示文件名不匹配此角色。

    sort_key 设计：
      - 旧 checkpoint: sort_key = step
      - 新 checkpoint: sort_key = 10^12 + 时间戳数字 * 10^6 + step
        → 保证任何新 session 都排在旧 session 之后
        → 同 session 内按 step 升序
    """
    base = filename.replace(".safetensors", "")
    prefix = f"{char_name}_flux_lora"

    # 最终版（无 step 后缀）
    if base == prefix:
        return {
            "session":  "",
            "step":     99999,
            "sort_key": 999_999_999_999_999,
            "is_final": True,
        }

    # 必须有 -step 后缀
    if "-step" not in base:
        return None

    try:
        left, step_str = base.rsplit("-step", 1)
        step = int(step_str)
    except (ValueError, IndexError):
        return None

    # 判断 session 类型
    if left == prefix:
        # 旧命名：{char}_flux_lora-stepN
        session  = ""
        sort_key = step
    elif left.startswith(prefix + "_r"):
        # 新命名：{char}_flux_lora_r20260422_1408-stepN
        session = left[len(prefix) + 1:]           # "r20260422_1408"
        ts_digits = session[1:].replace("_", "")   # "202604221408"
        try:
            ts_num = int(ts_digits)
            sort_key = 1_000_000_000_000 + ts_num * 1_000_000 + step
        except ValueError:
            sort_key = step
    else:
        # 不是这个角色的文件
        return None

    return {
        "session":  session,
        "step":     step,
        "sort_key": sort_key,
        "is_final": False,
    }


def _parse_step(filename: str, char_name: str) -> int:
    """向后兼容的简化接口。老代码直接调用的保留不破坏。"""
    info = _parse_checkpoint_info(filename, char_name)
    return info["step"] if info else 0


def find_checkpoints(char_name: str, story_id: str,
                     lora_dir: str = "",
                     target_steps: list[int] = None,
                     session_filter: Optional[str] = None) -> list[dict]:
    """
    查找角色的 checkpoint 文件。

    扫描顺序（优先级从高到低）：
      1. Mac 本地 lora_workspace/{story}/{char}/output_v{N}/  → 自动挑 round 最大的
         （找不到则回退到 lora_workspace/{story}/{char}/output/）
      2. H:/ComfyUI/.../loras/flux/                          → ComfyUI 直接可用
      3. C:/flux_train/workspace/{story}/{char}/output_v{N}/ → 自动挑最新 round
         （找不到则回退到 .../output/）

    v3.1 改动：
      - 适配 lora_trainer.py 的版本化输出 output_v{N}（按 round 隔离训练产物）
      - 自动选择 round 编号最大的目录扫描
      - 同时本地和 SSH 扫描都做这个适配

    参数：
      lora_dir:       手动指定本地扫描目录（覆盖自动选择）
      target_steps:   按文件名中的 step 数字过滤（不是累积步数）
      session_filter:
        None    = 扫所有 session（默认）
        ""      = 只返回旧 legacy 命名
        "latest"= 只返回最新 session（时间戳最大的那个）
        "r..."  = 只返回指定 session

    返回字典列表，每项含：
      step, session, sort_key, filename, path, comfy_ref, is_remote

    去重：(session, step) 联合 key，不同 session 同 step 都保留。
    排序：按 sort_key 升序（旧的在前、新的在后；同 session 内按 step）。
    """
    # ── 默认本地目录：自动挑最新 round（output_v*），fallback 到 output/ ─────
    if not lora_dir:
        char_workspace = Path(f"lora_workspace/{story_id}/{char_name}")
        latest_round_dir = _local_find_latest_round_dir(char_workspace)
        if latest_round_dir:
            lora_dir = str(latest_round_dir)
        else:
            lora_dir = str(char_workspace / "output")

    results   = []
    seen_keys = set()   # (session, step) 联合去重

    def _add(fname: str, path: str, is_remote: bool, comfy_ref: str):
        """解析 + 去重加入 results"""
        info = _parse_checkpoint_info(fname, char_name)
        if info is None or info["step"] <= 0:
            return
        key = (info["session"], info["step"])
        if key in seen_keys:
            return
        seen_keys.add(key)
        results.append({
            "step":       info["step"],
            "session":    info["session"],
            "sort_key":   info["sort_key"],
            "filename":   fname,
            "path":       path,
            "comfy_ref":  comfy_ref,
            "is_remote":  is_remote,
        })

    # ── 1. Mac 本地扫描 ───────────────────────────────────────
    local_dir = Path(lora_dir)
    local_files = list(local_dir.glob("*.safetensors")) if local_dir.exists() else []
    local_files = [f for f in local_files if char_name in f.name]

    if local_files:
        print(f"  [本地] {local_dir.name}/ 找到 {len(local_files)} 个 .safetensors")
        for f in sorted(local_files):
            _add(f.name, str(f), False, f"{COMFY_LORA_PREFIX}/{f.name}")
    else:
        # ── 2. 本地无文件 → SSH 扫描 Windows ─────────────────────
        print(f"  [本地] 无 checkpoint（{local_dir.name}/ {'存在但为空' if local_dir.exists() else '不存在'}）")
        print(f"  [SSH] 连接 Windows 扫描...")

        try:
            ssh = _make_ssh()
        except Exception as e:
            print(f"  !! SSH 连接失败: {e}")
            return []

        # 自动找最新 round 目录（output_v*），找不到回退到 output/
        char_dir = f"{_WIN_WORKSPACE}/{story_id}/{char_name}"
        win_output_dir = _ssh_find_latest_round_dir(ssh, char_dir)
        if win_output_dir is None:
            win_output_dir = f"{char_dir}/output"
            print(f"  [SSH] 未找到 output_v* 目录，回退扫描 {win_output_dir}")
        else:
            round_label = win_output_dir.rsplit("/", 1)[1]
            print(f"  [SSH] 使用最新 round: {round_label}  ({win_output_dir})")

        win_comfy_dir = _WIN_COMFY_LORAS

        print(f"  [SSH] 扫描 ComfyUI loras/flux/ ...")
        comfy_files = _ssh_list_dir(ssh, win_comfy_dir, char_name)

        print(f"  [SSH] 扫描 {win_output_dir.rsplit('/', 1)[1]}/ ...")
        output_files = _ssh_list_dir(ssh, win_output_dir, char_name)

        print(f"  [SSH] ComfyUI loras/flux/: {len(comfy_files)} 个  |  {win_output_dir.rsplit('/', 1)[1]}/: {len(output_files)} 个")

        # output/ 有文件但 ComfyUI 没 → 询问复制
        if output_files and not comfy_files:
            print(f"\n  output/ 有 {len(output_files)} 个 checkpoint，但 ComfyUI loras/flux/ 没有。")
            print(f"  自动复制到 ComfyUI？(y=复制  n=跳过) > ", end="", flush=True)
            try:
                import tty, termios, sys as _sys
                fd = _sys.stdin.fileno()
                old = termios.tcgetattr(fd)
                tty.setraw(fd)
                ch = _sys.stdin.read(1)
                termios.tcsetattr(fd, termios.TCSADRAIN, old)
            except Exception:
                ch = input().strip().lower()
                ch = ch[0] if ch else "n"
            print(ch)

            if ch in ("y", "Y", "\r", "\n"):
                ok = _ssh_copy_to_comfyui(ssh, win_output_dir, char_name)
                if ok:
                    comfy_files = _ssh_list_dir(ssh, win_comfy_dir, char_name)
                    print(f"  [SSH] 复制后 ComfyUI loras/flux/: {len(comfy_files)} 个")

        ssh.close()

        if comfy_files:
            for fname in sorted(comfy_files):
                _add(fname, f"{win_comfy_dir}/{fname}", True,
                     f"{COMFY_LORA_PREFIX}/{fname}")
        elif output_files:
            print(f"  ⚠ 使用 output/ 路径，ComfyUI 可能无法直接加载。建议先复制到 loras/flux/")
            for fname in sorted(output_files):
                _add(fname, f"{win_output_dir}/{fname}", True,
                     f"{COMFY_LORA_PREFIX}/{fname}")
        else:
            print(f"  !! 两个目录均无 checkpoint 文件")
            return []

    # ── target_steps 过滤 ─────────────────────────────────────
    if target_steps:
        results = [r for r in results if r["step"] in target_steps]

    # ── session_filter 过滤 ───────────────────────────────────
    if session_filter is not None:
        if session_filter == "latest":
            new_ckpts = [r for r in results if r["session"]]
            if new_ckpts:
                latest_session = max(new_ckpts, key=lambda x: x["sort_key"])["session"]
                results = [r for r in results if r["session"] == latest_session]
                print(f"  [Filter] 只保留最新 session: {latest_session}")
            else:
                print(f"  [Filter] --session latest 无新 session，保留 legacy")
        else:
            results = [r for r in results if r["session"] == session_filter]
            label = session_filter if session_filter else "legacy"
            print(f"  [Filter] 只保留 session: {label}")

    # ── 按 sort_key 排序 ─────────────────────────────────────
    results.sort(key=lambda x: x["sort_key"])

    if results:
        labels = []
        for r in results:
            if r["session"]:
                labels.append(f"{r['session']}/step{r['step']}")
            else:
                if r["step"] >= 99999:
                    labels.append("final")
                else:
                    labels.append(f"step{r['step']}")
        print(f"  [Checkpoint] 找到 {len(results)} 个: {labels}")
    else:
        print(f"  [Checkpoint] 过滤后无匹配")

    return results


# ════════════════════════════════════════════════════════════════
# 核心：生图 + 评分
# ════════════════════════════════════════════════════════════════

def _resolve_lora_name_with_comfy(requested: str) -> str:
    """
    把 'flux/xxx.safetensors' 解析成 ComfyUI 当前服务器认可的那个精确名字。

    v3.1.4 修复：
      ComfyUI 在 Windows 上枚举 LoRA 时保留原生反斜杠（'flux\\xxx.safetensors'），
      做 prompt validation 时精确字符串匹配。Linux/Mac 服务器则是正斜杠。
      我们在 Mac 端永远用正斜杠描述 LoRA，这里统一转成服务器认可的形式。

    策略：
      1. 查 ComfyUI 的 /object_info/LoraLoader，拿到真实名字列表（带缓存）
      2. 原样命中 → 直接返回
      3. 正反斜杠互换再命中 → 返回互换后的
      4. 还找不到 → 返回原值（让下游报错时能给出 ComfyUI 的原始信息）

    缓存：进程内单次加载。训练新 LoRA 并复制到 ComfyUI 后想强制刷新，
          调用 _invalidate_lora_cache()；或者重启 ComfyUI + 重启 tester。
    """
    global _LORA_LIST_CACHE
    if _LORA_LIST_CACHE is None:
        try:
            import urllib.request
            import json as _json
            url = f"{COMFY_SERVER}/object_info/LoraLoader"
            with urllib.request.urlopen(url, timeout=10) as resp:
                data = _json.loads(resp.read().decode("utf-8"))
            _LORA_LIST_CACHE = data["LoraLoader"]["input"]["required"]["lora_name"][0]
        except Exception as e:
            print(f"  [resolve_lora] 无法获取 ComfyUI LoRA 列表: {e}")
            return requested   # 获取失败，原样返回交给下游处理

    valid = _LORA_LIST_CACHE

    # 1. 精确命中
    if requested in valid:
        return requested

    # 2. 正反斜杠互换
    if "/" in requested:
        swapped = requested.replace("/", "\\")
    elif "\\" in requested:
        swapped = requested.replace("\\", "/")
    else:
        swapped = requested
    if swapped != requested and swapped in valid:
        return swapped

    # 3. 找不到 → 尝试给出提示，但返回原值让下游报精确错误
    import difflib
    near = difflib.get_close_matches(requested, valid, n=3, cutoff=0.5)
    if near:
        print(f"  [resolve_lora] 找不到 {requested!r}，相近的：")
        for n in near:
            print(f"    {n}")
    else:
        print(f"  [resolve_lora] 找不到 {requested!r}，且无相近匹配")
        print(f"    请重启 ComfyUI 刷新 LoRA 列表，或检查 {COMFY_SERVER}/object_info/LoraLoader")
    return requested


def _invalidate_lora_cache():
    """清空本地 LoRA 列表缓存，下次调用 _resolve_lora_name_with_comfy 时重新拉取"""
    global _LORA_LIST_CACHE
    _LORA_LIST_CACHE = None


# 模块级缓存
_LORA_LIST_CACHE: Optional[list] = None


def _generate_test_image(prompt: str, negative: str,
                         lora_path: str, strength: float,
                         theme, prefix: str, save_dir: str,
                         seed: int = 42) -> Optional[str]:
    """
    生成测试图。

    路径处理（v3.1.4）：
      ComfyUI 在 Windows 服务器上用反斜杠存 LoRA 名字，在 Linux 上用正斜杠。
      我们查询 ComfyUI 实际的名字列表（/object_info/LoraLoader），精准匹配
      服务器认可的字符串，再传给 workflow。避免 config.comfy_path 的"猜 OS"
      逻辑可能出错的情况。

      同时保留 v3.1.1 的 COMFY_SERVER_OS 切换作为兜底——防止 renderer 里的
      _fix_model_paths 又把我们 resolve 好的名字改回去。
    """
    # 先把 lora_path 转成 ComfyUI 认可的真实名字
    resolved_lora = _resolve_lora_name_with_comfy(lora_path)
    if resolved_lora != lora_path:
        print(f"  [resolve_lora] {lora_path!r} → {resolved_lora!r}")

    params = {
        "positive":        prompt,
        "negative":        negative or "modern, western, low quality, blurry, watermark",
        "lora":            resolved_lora,
        "lora_strength":   strength,
        "lora2":           None,
        "lora2_strength":  0.0,
        "cfg":             3.5,
        "steps":           20,
        "sampler":         "euler",
        "seed":            seed,
        "prefix":          prefix,
        "upscale_denoise": 0.42,
        "ip_ref":          None,
        "face_ref":        None,
        "_faceid_model":   "",
        "_faceid_vision":  "",
        "_checkpoint":     theme.model.checkpoint,
        "_ipadapter":      theme.model.ipadapter,
        "_clip_vision":    theme.model.clip_vision,
        "_unet":           getattr(theme.model, "unet",  "") or "",
        "_clip1":          getattr(theme.model, "clip1", "") or "",
        "_clip2":          getattr(theme.model, "clip2", "") or "",
        "_vae":            getattr(theme.model, "vae",   "") or "",
        "depth_ref":       None,
        "pose_action":     None,
    }
    gpu_guard.wait_if_needed("LoRA 测试生图")

    # 判断 resolved_lora 的分隔符，反向设置 COMFY_SERVER_OS 防止 renderer
    # 的 _fix_model_paths 再次"修正"（两个修复合在一起就会翻两次反而错）
    #
    # 逻辑：
    #   resolved 是 '\' → 说明服务器要反斜杠 → COMFY_SERVER_OS='windows'
    #                      此时 renderer 会把 '/' 转 '\'，但 resolved 已经是 '\'，
    #                      不会二次转换（replace('/','\\') 对纯反斜杠字符串无效）
    #   resolved 是 '/' → 说明服务器要正斜杠 → COMFY_SERVER_OS='linux'
    #                      renderer 会把 '\' 转 '/'，对 resolved 也无影响
    import config as _cfg
    _orig_os = getattr(_cfg, "COMFY_SERVER_OS", None)
    if "\\" in resolved_lora:
        _cfg.COMFY_SERVER_OS = "windows"
    else:
        _cfg.COMFY_SERVER_OS = "linux"

    try:
        files = generate_and_wait(comfy_generate_flux, params, prefix, save_dir)
    finally:
        if _orig_os is not None:
            _cfg.COMFY_SERVER_OS = _orig_os

    return files[-1] if files else None


# ════════════════════════════════════════════════════════════════
# 对比报告生成 —— v3：多 session 支持
# ════════════════════════════════════════════════════════════════

def generate_report(tracker: TestTracker,
                    test_prompts: list[dict],
                    report_path: Path,
                    checkpoints: list[dict] = None) -> dict:
    """
    生成对比报告。

    v3 改动：
      - 分组 key 增加 session，区分不同 session 的同 step
      - 多 session 存在时报告里显示 Session 列
      - 新增 best_filename / best_session 字段，apply 时精确匹配

    checkpoints 参数：传入 find_checkpoints() 的返回值，用于回填 filename 到 summary。
                    不传也能工作（best_filename 会置空，apply 回退到 step 匹配）。
    """
    results = tracker.results
    if not results:
        print("  无测试结果")
        return {}

    # filename 查找表 (session, step) → filename
    fname_lookup = {}
    if checkpoints:
        for c in checkpoints:
            fname_lookup[(c.get("session", ""), c["step"])] = c["filename"]

    # 按 (session, step, strength) 分组
    groups = {}
    for r in results:
        key = (r.get("session", ""), r["step"], r["strength"])
        groups.setdefault(key, []).append(r)

    prompt_weights   = {p["id"]: p.get("weight", 1.0) for p in test_prompts}
    prompt_pose_type = {p["id"]: p.get("pose_type", "其他") for p in test_prompts}

    summary = []
    for (session, step, strength), items in sorted(groups.items(),
                                                    key=lambda x: (x[0][0], x[0][1], x[0][2])):
        scores_by_prompt   = {item["prompt_id"]: item["score"] for item in items}
        feedback_by_prompt = {item["prompt_id"]: item.get("feedback", "") for item in items}

        weighted_sum = sum(scores_by_prompt.get(pid, 0) * prompt_weights.get(pid, 1.0)
                           for pid in scores_by_prompt)
        weight_total = sum(prompt_weights.get(pid, 1.0) for pid in scores_by_prompt)
        avg = weighted_sum / weight_total if weight_total > 0 else 0.0
        distant_score = scores_by_prompt.get("distant_wide", -1)

        pose_scores: dict[str, list[float]] = {}
        for pid, score in scores_by_prompt.items():
            pt = prompt_pose_type.get(pid, "其他")
            pose_scores.setdefault(pt, []).append(score)
        pose_avg = {pt: round(sum(v) / len(v), 2) for pt, v in pose_scores.items()}

        weak_poses = [pt for pt, sc in pose_avg.items() if sc < 7.0]

        all_feedback = " ".join(feedback_by_prompt.values()).lower()
        issues = []
        for kw, label in [
            ("face", "脸部不稳定"), ("gender", "性别错误"),
            ("blurry", "画面模糊"), ("overfit", "过拟合"),
            ("hand", "手部变形"), ("inconsistent", "服饰不一致"),
        ]:
            if kw in all_feedback:
                issues.append(label)

        summary.append({
            "session":       session,
            "step":          step,
            "filename":      fname_lookup.get((session, step), ""),
            "strength":      strength,
            "scores":        scores_by_prompt,
            "weighted_avg":  round(avg, 2),
            "distant_score": distant_score,
            "pose_avg":      pose_avg,
            "weak_poses":    weak_poses,
            "issues":        issues,
        })

    # ── 生成报告文本 ──────────────────────────────────────────
    prompt_ids    = [p["id"]    for p in test_prompts]
    prompt_labels = [p["label"] for p in test_prompts]

    # 判断是否需要显示 Session 列（存在非空 session 或多种 session 混合时显示）
    sessions_present = set(s["session"] for s in summary)
    show_session = any(s for s in sessions_present) or len(sessions_present) > 1

    lines = ["=" * 80, "  LoRA Checkpoint 对比报告", "=" * 80]

    if show_session:
        header = f"  {'Session':<18} {'Step':>7} {'Str':>4}"
    else:
        header = f"  {'Checkpoint':<20} {'Str':>4}"
    for label in prompt_labels:
        header += f"  {label:>6}"
    header += f"  {'加权均':>6}  {'状态'}"
    lines += [header, "  " + "─" * (len(header) - 2)]

    best_entry = None
    best_avg   = 0.0

    for s in summary:
        if s["step"] >= 99999:
            step_display = "final"
        else:
            step_display = f"{s['step']:05d}"

        if show_session:
            session_label = s["session"] if s["session"] else "(legacy)"
            row = f"  {session_label:<18} {step_display:>7} {s['strength']:>4.1f}"
        else:
            step_label_short = "final" if s["step"] >= 99999 else f"step{s['step']:05d}"
            row = f"  {step_label_short:<20} {s['strength']:>4.1f}"

        for pid in prompt_ids:
            score = s["scores"].get(pid, -1)
            row += f"  {score:>6.1f}" if score >= 0 else f"  {'N/A':>6}"
        row += f"  {s['weighted_avg']:>6.2f}"

        if s["distant_score"] >= 0 and s["distant_score"] < 6.5:
            row += "  ⚠过拟合"
        elif s["weighted_avg"] > best_avg and (
                s["distant_score"] < 0 or s["distant_score"] >= 6.5):
            best_avg   = s["weighted_avg"]
            best_entry = s
        lines.append(row)

    # best_entry 行加推荐标记
    if best_entry:
        for i, line in enumerate(lines):
            if "─" in line:
                continue
            step_str = "final" if best_entry["step"] >= 99999 else f"{best_entry['step']:05d}"
            if step_str not in line:
                continue
            if f"{best_entry['strength']:.1f}" not in line:
                continue
            if show_session:
                session_label = best_entry["session"] if best_entry["session"] else "(legacy)"
                if session_label not in line:
                    continue
            lines[i] = line + "  ← 推荐"
            break

    lines.append("  " + "─" * (len(header) - 2))

    if best_entry:
        if best_entry["step"] >= 99999:
            best_label = "final"
        elif best_entry["session"]:
            best_label = f"{best_entry['session']}/step{best_entry['step']:05d}"
        else:
            best_label = f"step{best_entry['step']:05d}"

        lines += [
            f"\n  推荐: {best_label}  strength={best_entry['strength']}",
            f"  加权平均分: {best_entry['weighted_avg']:.2f}",
        ]
        if best_entry["filename"]:
            lines.append(f"  文件名: {best_entry['filename']}")
        if best_entry["distant_score"] >= 0:
            lines.append(f"  远景分数: {best_entry['distant_score']:.1f} (>=6.5 无过拟合)")
        if best_entry["weak_poses"]:
            lines.append(f"  弱势 pose: {', '.join(best_entry['weak_poses'])}")
        if best_entry["issues"]:
            lines.append(f"  问题: {', '.join(best_entry['issues'])}")

    lines.append("")
    report_text = "\n".join(lines)
    report_path.write_text(report_text, encoding="utf-8")
    print(report_text)

    # 整体结论
    if best_entry:
        avg = best_entry["weighted_avg"]
        if avg >= 8.0 and not best_entry["weak_poses"]:
            overall = "passed"
            recommendation = "approved"
        elif avg >= 7.0:
            overall = "partial"
            recommendation = "retrain_partial"
        else:
            overall = "failed"
            recommendation = "retrain_full"
    else:
        overall = "failed"
        recommendation = "retrain_full"
        best_entry = {"step": 0, "session": "", "filename": "",
                      "strength": 0.8, "weighted_avg": 0,
                      "pose_avg": {}, "weak_poses": [], "issues": []}

    return {
        "best_step":        best_entry["step"],
        "best_session":     best_entry.get("session", ""),      # v3 新增
        "best_filename":    best_entry.get("filename", ""),     # v3 新增（最可靠）
        "best_strength":    best_entry["strength"],
        "best_avg":         best_entry["weighted_avg"],
        "overall":          overall,
        "recommendation":   recommendation,
        "weak_poses":       best_entry.get("weak_poses", []),
        "issues":           best_entry.get("issues", []),
        "pose_scores":      best_entry.get("pose_avg", {}),
        "_summary":         summary,
    }


# ════════════════════════════════════════════════════════════════
# 测试结果持久化
# ════════════════════════════════════════════════════════════════

def _latest_round(workspace: Path) -> int:
    """返回 test_results/ 下已有的最大轮次号，无则返回 0。"""
    tr_dir = workspace / "test_results"
    if not tr_dir.exists():
        return 0
    nums = []
    for p in tr_dir.glob("round*.json"):
        try:
            nums.append(int(p.stem.replace("round", "")))
        except ValueError:
            pass
    return max(nums) if nums else 0


def save_test_result(workspace: Path,
                     char_name: str,
                     report: dict,
                     checkpoints: list[dict],
                     round_num: int | None = None):
    """
    把测试结论持久化到 test_results/roundN.json。
    trainer round2 通过读这个文件了解上轮弱点。

    v3：ckpt_detail 的 key 用 "session/stepN" 格式区分多 session。
    """
    if not report:
        return

    tr_dir = workspace / "test_results"
    tr_dir.mkdir(parents=True, exist_ok=True)

    if round_num is None:
        round_num = _latest_round(workspace) + 1

    # checkpoint 详情：key 包含 session 以避免混淆
    ckpt_detail = {}
    for s in report.get("_summary", []):
        step = s["step"]
        session = s.get("session", "")
        if step >= 99999:
            step_key = "final"
        elif session:
            step_key = f"{session}/step{step:05d}"
        else:
            step_key = f"step{step:05d}"
        ckpt_detail[step_key] = {
            "session":     session,
            "step":        step,
            "filename":    s.get("filename", ""),
            "strength":    s["strength"],
            "score":       s["weighted_avg"],
            "pose_scores": s.get("pose_avg", {}),
            "issues":      s.get("issues", []),
        }

    # best_ckpt 匹配：优先 filename，回退 (session, step)
    best_filename = report.get("best_filename", "")
    best_session  = report.get("best_session", "")
    best_step     = report["best_step"]

    best_ckpt = None
    if best_filename:
        best_ckpt = next((c for c in checkpoints if c["filename"] == best_filename), None)
    if not best_ckpt:
        best_ckpt = next((c for c in checkpoints
                         if c["step"] == best_step
                         and c.get("session", "") == best_session), None)
    if not best_ckpt:
        best_ckpt = next((c for c in checkpoints if c["step"] == best_step), {})

    record = {
        "round":            round_num,
        "char":             char_name,
        "tested_at":        datetime.now().strftime("%Y-%m-%d %H:%M"),
        "overall":          report["overall"],
        "recommendation":   report["recommendation"],
        "best_checkpoint":  best_ckpt.get("filename", ""),
        "best_session":     best_ckpt.get("session", ""),       # v3
        "best_comfy_ref":   best_ckpt.get("comfy_ref", ""),
        "best_strength":    report["best_strength"],
        "best_avg":         report["best_avg"],
        "pose_scores":      report["pose_scores"],
        "weak_poses":       report["weak_poses"],
        "issues":           report["issues"],
        "checkpoints":      ckpt_detail,
        "retrain_focus":    report["weak_poses"],
        "retrain_strategy": report["recommendation"],
    }

    out_path = tr_dir / f"round{round_num}.json"
    out_path.write_text(json.dumps(record, ensure_ascii=False, indent=2),
                        encoding="utf-8")
    print(f"  [测试记录] 已保存 → {out_path}")
    return out_path


# ════════════════════════════════════════════════════════════════
# 结果应用：更新 story yaml + loras/flux/角色.yaml
# ════════════════════════════════════════════════════════════════

def apply_test_result(story_path: str,
                      char_name: str,
                      report: dict,
                      checkpoints: list[dict],
                      lora_registry_dir: str = "loras/flux"):
    """
    测试通过（overall != failed）后，把最佳 checkpoint 写回：
      1. story yaml → characters.char.lora / lora_strength / lora_status
      2. loras/flux/char.yaml → file / strength / notes

    v3：优先用 best_filename 精确匹配 checkpoint，避免多 session 歧义。
    兼容旧 report（无 best_filename 时回退到 best_step 匹配）。

    lora_status 语义：
      pending  → 训练完成，未测试或测试未通过
      approved → 测试通过，pipeline 可用
      rejected → 多轮测试均失败，需重新训练
    """
    if report.get("overall") == "failed":
        print(f"  [应用] 测试未通过（failed），不写回 yaml")
        print(f"  建议: {report.get('recommendation', 'retrain_full')}")
        return False

    # ── checkpoint 匹配：三级回退 ──────────────────────────────
    best_filename = report.get("best_filename", "")
    best_session  = report.get("best_session", "")
    best_step     = report["best_step"]

    best_ckpt = None
    # 1. 精确 filename 匹配（v3 首选）
    if best_filename:
        best_ckpt = next((c for c in checkpoints if c["filename"] == best_filename), None)
    # 2. (session, step) 匹配
    if not best_ckpt:
        best_ckpt = next((c for c in checkpoints
                         if c["step"] == best_step
                         and c.get("session", "") == best_session), None)
    # 3. 仅 step 匹配（兼容旧 report 无 session 字段）
    if not best_ckpt:
        best_ckpt = next((c for c in checkpoints if c["step"] == best_step), None)

    if not best_ckpt:
        print(f"  !! 找不到对应 checkpoint")
        print(f"     best_filename={best_filename}")
        print(f"     best_session={best_session}  best_step={best_step}")
        return False

    lora_ref      = best_ckpt["comfy_ref"]
    best_strength = report["best_strength"]
    status        = "approved" if report["overall"] == "passed" else "pending"

    # 用 best_ckpt 实际信息生成 label（确保准确）
    actual_step    = best_ckpt["step"]
    actual_session = best_ckpt.get("session", "")
    if actual_step >= 99999:
        step_label = "final"
    elif actual_session:
        step_label = f"{actual_session}/step{actual_step:05d}"
    else:
        step_label = f"step{actual_step:05d}"

    # ── 1. 更新 story yaml ────────────────────────────────────
    story_file = Path(story_path)
    if story_file.exists():
        with open(story_file, "r", encoding="utf-8") as f:
            story_data = yaml.safe_load(f)

        chars = story_data.setdefault("characters", {})
        if char_name in chars:
            chars[char_name]["lora"]          = lora_ref
            chars[char_name]["lora_strength"] = best_strength
            chars[char_name]["lora_status"]   = status
            with open(story_file, "w", encoding="utf-8") as f:
                yaml.dump(story_data, f, allow_unicode=True,
                          default_flow_style=False, sort_keys=False)
            print(f"  [Story YAML] {char_name}.lora → {lora_ref} "
                  f"(strength={best_strength}, status={status})")
        else:
            print(f"  [Story YAML] 角色 {char_name} 不存在，跳过")
    else:
        print(f"  [Story YAML] 文件不存在: {story_path}")

    # ── 2. 更新 loras/flux/角色.yaml ──────────────────────────
    reg_dir  = Path(lora_registry_dir)
    reg_file = reg_dir / f"{char_name}.yaml"

    if reg_file.exists():
        with open(reg_file, "r", encoding="utf-8") as f:
            reg_data = yaml.safe_load(f) or {}
    else:
        reg_dir.mkdir(parents=True, exist_ok=True)
        reg_data = {
            "name":       char_name,
            "base_model": "flux",
        }

    reg_data["file"]     = lora_ref
    reg_data["strength"] = best_strength
    reg_data["status"]   = status
    reg_data["notes"]    = (
        f"FLUX LoRA, {datetime.now().strftime('%Y-%m-%d')} 测试完成. "
        f"推荐 {step_label} checkpoint, strength {best_strength}. "
        f"加权均分 {report['best_avg']:.2f}."
        + (f" 弱势: {', '.join(report['weak_poses'])}." if report["weak_poses"] else "")
    )

    with open(reg_file, "w", encoding="utf-8") as f:
        yaml.dump(reg_data, f, allow_unicode=True,
                  default_flow_style=False, sort_keys=False)
    print(f"  [LoRA 注册表] {reg_file} 已更新 (status={status})")

    if report["weak_poses"]:
        print(f"  ⚠ 弱势 pose: {', '.join(report['weak_poses'])}  "
              f"→ 建议运行 lora_trainer.py round2 针对性补训")

    return True


# ════════════════════════════════════════════════════════════════
# 主测试流程
# ════════════════════════════════════════════════════════════════

def run_checkpoint_test(story, theme, char_name: str,
                        target_steps: list[int] = None,
                        strengths: list[float] = None,
                        force: bool = False,
                        fixed_seed: int = 12345,
                        auto_apply: bool = False,
                        session_filter: Optional[str] = None):
    char_cfg = story.characters.get(char_name)
    if not char_cfg:
        print(f"  !! 角色 [{char_name}] 不存在")
        return {}

    if target_steps is None:
        target_steps = [300, 500, 700, 1000, 1500, 99999]
    if strengths is None:
        strengths = [0.8]

    checkpoints = find_checkpoints(char_name, story.story_id,
                                   target_steps=target_steps,
                                   session_filter=session_filter)
    if not checkpoints:
        print(f"  !! 未找到 {char_name} 的 checkpoint 文件")
        print(f"  本地检查: lora_workspace/{story.story_id}/{char_name}/output_v*/")
        print(f"  远程检查: C:/flux_train/workspace/{story.story_id}/{char_name}/output_v*/")
        return {}

    workspace = Path(f"lora_workspace/{story.story_id}/{char_name}")
    test_dir  = workspace / "test_results"
    test_dir.mkdir(parents=True, exist_ok=True)

    tracker = TestTracker(test_dir / "test_status.json")
    if force:
        tracker.clear()
        print(f"  已清除旧测试结果")

    print(f"\n{'='*58}")
    print(f"  LoRA 对比测试: {char_name}")
    # 友好显示 checkpoint 列表
    ckpt_labels = []
    for c in checkpoints:
        if c["step"] >= 99999:
            ckpt_labels.append("final")
        elif c["session"]:
            ckpt_labels.append(f"{c['session']}/step{c['step']}")
        else:
            ckpt_labels.append(f"step{c['step']}")
    print(f"  Checkpoints: {ckpt_labels}")
    print(f"  Strengths: {strengths}")
    print(f"{'='*58}")

    style_prefix = theme.prompts.style_prefix or ""
    test_prompts = _build_test_prompts(char_cfg, style_prefix)
    negative     = theme.prompts.negative_prefix or ""

    total_tests = len(checkpoints) * len(strengths) * len(test_prompts)
    skipped = generated = 0
    print(f"\n  总测试数: {total_tests} "
          f"({len(checkpoints)} ckpt × {len(strengths)} str × {len(test_prompts)} prompt)")

    # ── Phase A：生成所有测试图 ───────────────────────────────
    for ckpt in checkpoints:
        step      = ckpt["step"]
        session   = ckpt.get("session", "")
        lora_ref  = ckpt["comfy_ref"]

        # step_label 用于日志 + 图片文件名（含 session 防止不同 session 图片互相覆盖）
        if step >= 99999:
            step_label = "final"
        elif session:
            step_label = f"{session}_step{step:05d}"
        else:
            step_label = f"step{step:05d}"

        for strength in strengths:
            for tp in test_prompts:
                pid = tp["id"]
                if tracker.is_done(step, strength, pid, session=session):
                    skipped += 1
                    continue

                prefix = f"{step_label}_s{strength:.1f}_{pid}"
                print(f"\n  [{generated+skipped+1}/{total_tests}] "
                      f"{step_label} s={strength} {tp['label']}")

                img_path = _generate_test_image(
                    prompt=tp["prompt"], negative=negative,
                    lora_path=lora_ref, strength=strength,
                    theme=theme, prefix=prefix,
                    save_dir=str(test_dir), seed=fixed_seed,
                )

                if not img_path:
                    print(f"    ✗ 生图失败")
                    tracker.record(TestResult(
                        checkpoint=ckpt["filename"], step=step, session=session,
                        strength=strength, prompt_id=pid,
                        prompt_label=tp["label"],
                        image_path="", score=0.0, feedback="生图失败",
                    ))
                    continue

                generated += 1
                print(f"    ✓ {Path(img_path).name}")
                tracker.record(TestResult(
                    checkpoint=ckpt["filename"], step=step, session=session,
                    strength=strength, prompt_id=pid,
                    prompt_label=tp["label"],
                    image_path=img_path, score=-1.0,
                ))
                time.sleep(2)

    if skipped:
        print(f"\n  跳过已测试: {skipped}  新生成: {generated}")

    # ── Phase B：统一评分 ────────────────────────────────────
    unscored = [r for r in tracker.results if r["score"] < 0 and r["image_path"]]
    if unscored:
        print(f"\n{'='*58}")
        print(f"  Phase B：统一评分（{len(unscored)} 张）")
        print(f"{'='*58}")

        gpu_guard.free_comfyui_vram()
        time.sleep(5)

        from quality import LlavaGate, QualityContext
        gate = LlavaGate(
            threshold=0.0,
            vision_model=theme.model.vision_model,
            review_focus="古风人物画质，角色辨识度，服饰风格一致性，构图完整性",
        )
        gate.warmup()

        for i, r in enumerate(unscored):
            if not Path(r["image_path"]).exists():
                r["score"] = 0.0
                r["feedback"] = "文件不存在"
                continue
            ctx = QualityContext(
                page_num=1, page_title=r["prompt_label"],
                characters=[], char_features="",
                image_type="solo_character",
            )
            result = gate.evaluate(r["image_path"], ctx)
            r["score"]    = result.score
            r["feedback"] = result.feedback
            print(f"  [{i+1}/{len(unscored)}] {Path(r['image_path']).name}: "
                  f"{result.score:.1f}  {result.feedback[:50]}")

        gate.free_model()
        score_map = {r["image_path"]: (r["score"], r["feedback"]) for r in unscored}
        for item in tracker.results:
            if item["image_path"] in score_map:
                item["score"], item["feedback"] = score_map[item["image_path"]]
        tracker._save()

    # ── Phase C：生成报告（带 checkpoints 以便回填 filename）──
    print(f"\n{'='*58}")
    report = generate_report(tracker, test_prompts, test_dir / "report.txt",
                             checkpoints=checkpoints)

    # ── Phase D：持久化 roundN.json ──────────────────────────
    round_num = _latest_round(workspace) + 1
    save_test_result(workspace, char_name, report, checkpoints, round_num)

    # ── Phase E：写回 yaml ────────────────────────────────────
    if report and (report.get("best_step") or report.get("best_filename")):
        if auto_apply:
            apply_test_result(story.path, char_name, report, checkpoints)
        else:
            print(f"\n  写回 story yaml + lora 注册表？(y=写回  n=跳过) > ", end="", flush=True)
            try:
                import tty, termios, sys
                fd = sys.stdin.fileno()
                old = termios.tcgetattr(fd)
                tty.setraw(fd)
                ch = sys.stdin.read(1)
                termios.tcsetattr(fd, termios.TCSADRAIN, old)
            except Exception:
                ch = input().strip().lower()
                ch = ch[0] if ch else "n"
            print(ch)
            if ch in ("y", "Y"):
                apply_test_result(story.path, char_name, report, checkpoints)
            else:
                print(f"  跳过写回。手动写回参考：")
                # 优先用 filename 精确匹配
                bfn = report.get("best_filename", "")
                if bfn:
                    ckpt = next((c for c in checkpoints if c["filename"] == bfn), None)
                else:
                    ckpt = next((c for c in checkpoints
                                if c["step"] == report.get("best_step")
                                and c.get("session", "") == report.get("best_session", "")),
                               None)
                if ckpt:
                    print(f"    lora: {ckpt['comfy_ref']}")
                    print(f"    lora_strength: {report['best_strength']}")

    return report


def run_strength_test(story, theme, char_name: str,
                      best_step: int,
                      strengths: list[float] = None,
                      force: bool = False,
                      fixed_seed: int = 12345,
                      auto_apply: bool = False,
                      session_filter: Optional[str] = None):
    """
    对指定 step 做 strength 精调。

    v3：
      - 如果存在多个 session 的同 step，用 session_filter 精确定位
      - 不指定 session_filter 时，默认测所有匹配 step 的 checkpoint（可能多个）
      - 推荐搭配 --session latest 用：只测最新 session 的 best_step
    """
    if strengths is None:
        strengths = [0.5, 0.6, 0.7, 0.8, 0.9, 1.0]
    return run_checkpoint_test(
        story, theme, char_name,
        target_steps=[best_step],
        strengths=strengths,
        force=force,
        fixed_seed=fixed_seed,
        auto_apply=auto_apply,
        session_filter=session_filter,
    )


# ════════════════════════════════════════════════════════════════
# CLI 入口
# ════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        description="LoRA Checkpoint 自动对比测试 v3（多 session 支持）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例（日常用法，零变化）：
  python lora_tester.py --story stories/haunted_inn.yaml --char scholar
  python lora_tester.py --story ... --char scholar --steps 500,1000,1500
  python lora_tester.py --story ... --char scholar --mode strength --best-step 1000
  python lora_tester.py --story ... --char scholar --apply

示例（v3 新增，多 session 场景）：
  # 只测最新续训的产物（快速验证续训效果）
  python lora_tester.py --story ... --char scholar --session latest

  # 对比续训 vs 旧 session（分两次跑然后看报告）
  python lora_tester.py --story ... --char scholar --session ""          # 测旧
  python lora_tester.py --story ... --char scholar --session r20260422_1408   # 测新

  # 指定 session + strength 精调
  python lora_tester.py --story ... --char scholar --mode strength \\
      --best-step 500 --session r20260422_1408
        """
    )
    parser.add_argument("--story",     required=True)
    parser.add_argument("--char",      required=True)
    parser.add_argument("--mode",      default="checkpoint",
                        choices=["checkpoint", "strength"])
    parser.add_argument("--steps",     default=None,
                        help="指定 step 列表（文件名里的 step 数字，不是累积），逗号分隔")
    parser.add_argument("--best-step", type=int, default=1000)
    parser.add_argument("--strengths", default=None,
                        help="指定 strength 列表，如 0.6,0.7,0.8,0.9")
    parser.add_argument("--force",     action="store_true", help="清除旧结果重新测试")
    parser.add_argument("--seed",      type=int, default=12345)
    parser.add_argument("--apply",     action="store_true",
                        help="测试完自动写回 story yaml + lora 注册表")
    parser.add_argument("--session",   default=None,
                        help="过滤 session（v3 新增）。"
                             "不指定=扫所有；'latest'=最新 session；"
                             "具体名如 'r20260422_1408'=该 session；"
                             "空字符串 ''=只测旧 legacy 命名")
    args = parser.parse_args()

    from comfy_script.runtime import load
    load(COMFY_SERVER)

    from story import StoryConfig
    from theme import ThemeConfig

    story = StoryConfig(args.story)
    theme = ThemeConfig(story.default_theme_path)

    target_steps = ([int(x.strip()) for x in args.steps.split(",")]
                    if args.steps else None)
    strengths    = ([float(x.strip()) for x in args.strengths.split(",")]
                    if args.strengths else None)

    if args.mode == "checkpoint":
        run_checkpoint_test(
            story, theme, args.char,
            target_steps=target_steps,
            strengths=strengths,
            force=args.force,
            fixed_seed=args.seed,
            auto_apply=args.apply,
            session_filter=args.session,
        )
    elif args.mode == "strength":
        run_strength_test(
            story, theme, args.char,
            best_step=args.best_step,
            strengths=strengths,
            force=args.force,
            fixed_seed=args.seed,
            auto_apply=args.apply,
            session_filter=args.session,
        )


if __name__ == "__main__":
    main()
