"""
gpu_guard.py ── GPU 显存守卫
==============================
职责：
  1. 实时监测远程 3060（12GB）显存使用量
  2. 临近上限时自动 sleep，等显存释放后再继续
  3. 提供装饰器 / 上下文管理器两种用法，方便嵌入 pipeline

监测来源（按优先级）：
  A. ComfyUI /system_stats 接口（无需额外依赖，推荐）
  B. pynvml（需要 pip install pynvml，本地有 GPU 时可用）
  C. nvidia-smi subprocess（兜底）

用法：
  # 1. 在生图前检查
  from gpu_guard import gpu_guard
  gpu_guard.wait_if_needed()

  # 2. 装饰器
  @gpu_guard.guard
  def my_generate():
      ...

  # 3. 上下文管理器
  with gpu_guard:
      comfy_generate(params)

  # 4. 直接查询
  usage = gpu_guard.get_usage()
  print(f"已用 {usage.used_mb:.0f} MB / {usage.total_mb:.0f} MB")
"""

import time
import logging
from dataclasses import dataclass
from functools import wraps
from typing import Optional

log = logging.getLogger(__name__)

# ── 默认阈值（针对 3060 12GB）────────────────────────────
DEFAULT_WARN_MB    = 9_500   # 9.5 GB → 开始打印警告
DEFAULT_SLEEP_MB   = 10_500  # 10.5 GB → 触发 sleep
DEFAULT_RESUME_MB  = 8_000   # 8 GB 以下才继续
DEFAULT_POLL_S     = 8       # 每 8s 轮询一次
DEFAULT_MAX_WAIT_S = 300     # 最多等 5 分钟，否则强制继续（避免死锁）


@dataclass
class VramUsage:
    used_mb: float
    total_mb: float
    free_mb: float
    source: str   # "comfyui" / "pynvml" / "nvidia-smi" / "unknown"

    @property
    def used_pct(self) -> float:
        return self.used_mb / self.total_mb * 100 if self.total_mb else 0


# ── 读取方式 A：ComfyUI /system_stats ───────────────────

def _from_comfyui(server: str, proxies: dict = None) -> Optional[VramUsage]:
    try:
        import requests
        r = requests.get(
            f"{server}/system_stats",
            timeout=5,
            proxies=proxies or {},
        ).json()
        devices = r.get("devices", [])
        if not devices:
            return None
        d = devices[0]
        vram_total = d.get("vram_total", 0) / 1024 / 1024   # bytes → MB
        vram_free  = d.get("vram_free",  0) / 1024 / 1024
        if vram_total <= 0:
            return None
        used = vram_total - vram_free
        return VramUsage(
            used_mb  = used,
            total_mb = vram_total,
            free_mb  = vram_free,
            source   = "comfyui",
        )
    except Exception:
        return None


# ── 读取方式 B：pynvml ───────────────────────────────────

def _from_pynvml(gpu_index: int = 0) -> Optional[VramUsage]:
    try:
        import pynvml
        pynvml.nvmlInit()
        handle = pynvml.nvmlDeviceGetHandleByIndex(gpu_index)
        info   = pynvml.nvmlDeviceGetMemoryInfo(handle)
        return VramUsage(
            used_mb  = info.used  / 1024 / 1024,
            total_mb = info.total / 1024 / 1024,
            free_mb  = info.free  / 1024 / 1024,
            source   = "pynvml",
        )
    except Exception:
        return None


# ── 读取方式 C：nvidia-smi subprocess ───────────────────

def _from_nvidia_smi() -> Optional[VramUsage]:
    try:
        import subprocess
        out = subprocess.check_output(
            ["nvidia-smi",
             "--query-gpu=memory.used,memory.free,memory.total",
             "--format=csv,noheader,nounits"],
            timeout=10,
        ).decode().strip().split("\n")[0]
        parts = [float(x.strip()) for x in out.split(",")]
        used, free, total = parts
        return VramUsage(
            used_mb  = used,
            total_mb = total,
            free_mb  = free,
            source   = "nvidia-smi",
        )
    except Exception:
        return None


# ── 读取方式 D：SSH 到远端机器跑 nvidia-smi ──────────────
# 适配"mac 控制远端 win10"架构：代码在 mac、GPU 在 win10，
# 本地 pynvml / nvidia-smi 永远读不到。当 ComfyUI HTTP 不通时
# （ComfyUI 进程挂掉/端口不通），SSH 到那台真正有卡的机器上读。
# 凭证复用 config.REMOTE_SSH（与 sync_to_remote.py 同一套）。

def _from_ssh_nvidia_smi(ssh_cfg: dict = None) -> Optional[VramUsage]:
    if ssh_cfg is None:
        try:
            from config import REMOTE_SSH
            ssh_cfg = REMOTE_SSH
        except (ImportError, AttributeError):
            return None
    if not ssh_cfg:
        return None

    # 1. 彻底闭嘴：静默 paramiko 内部连接失败时强行打印的底层 Traceback
    import logging
    logging.getLogger("paramiko").setLevel(logging.ERROR)

    import paramiko
    ssh = None  # 初始化为 None，确保 finally 块能正确处理
    try:
        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())

        # 2. 核心完善：加入自动重试机制（模拟你连续尝试 5 次的操作）
        max_retries = 5
        connected = False

        for attempt in range(max_retries):
            try:
                ssh.connect(
                    ssh_cfg["host"],
                    port=ssh_cfg.get("port", 22),
                    username=ssh_cfg["username"],
                    password=ssh_cfg["password"],
                    timeout=4,  # 稍微缩短单次超时，方便快速进行下一次重试
                )
                connected = True
                break  # 撞大运成功！跳出重试循环
            except Exception as e:
                # 如果还没到最后一次机会，就等等再试
                if attempt < max_retries - 1:
                    # print(f"   SSH 尝试 {attempt + 1}/{max_retries} 失败，1秒后重试...") # 如果嫌烦可以注释掉这行
                    time.sleep(1)
                    continue
                else:
                    # 试了 5 次都彻底失败了，安静退出
                    return None

        if not connected:
            return None

        # 3. 连接成功后，正常执行 nvidia-smi 命令
        cmd = ("nvidia-smi --query-gpu=memory.used,memory.free,memory.total "
               "--format=csv,noheader,nounits")
        _stdin, stdout, _stderr = ssh.exec_command(cmd, timeout=15)
        out = stdout.read().decode().strip().split("\n")[0]
        if not out:
            return None
        parts = [float(x.strip()) for x in out.split(",")]
        used, free, total = parts
        if total <= 0:
            return None
        return VramUsage(
            used_mb=used,
            total_mb=total,
            free_mb=free,
            source="ssh-nvidia-smi",
        )
    except Exception:
        return None
    finally:
        if ssh is not None:
            try:
                ssh.close()
            except Exception:
                pass


# ── 主类 ────────────────────────────────────────────────

class GpuGuard:
    """
    线程安全的 GPU 显存守卫。
    单例模式，通过模块级 `gpu_guard` 访问。
    """

    def __init__(
        self,
        warn_mb:    float = DEFAULT_WARN_MB,
        sleep_mb:   float = DEFAULT_SLEEP_MB,
        resume_mb:  float = DEFAULT_RESUME_MB,
        poll_s:     int   = DEFAULT_POLL_S,
        max_wait_s: int   = DEFAULT_MAX_WAIT_S,
        comfy_server: str = "",
        proxies: dict     = None,
        gpu_index: int    = 0,
        enabled:   bool   = True,
    ):
        self.warn_mb      = warn_mb
        self.sleep_mb     = sleep_mb
        self.resume_mb    = resume_mb
        self.poll_s       = poll_s
        self.max_wait_s   = max_wait_s
        self.comfy_server = comfy_server
        self.proxies      = proxies or {}
        self.gpu_index    = gpu_index
        self.enabled      = enabled

        # 自动从 config 读取 comfy_server（如果存在）
        if not self.comfy_server:
            try:
                from config import COMFY_SERVER, PROXIES
                self.comfy_server = COMFY_SERVER
                self.proxies = PROXIES or {}
            except ImportError:
                pass

    # ── 显存读取 ───────────────────────────────────────

    def get_usage(self) -> Optional[VramUsage]:
        """
        按优先级尝试多种方式读取显存，返回第一个成功的结果。
          A. ComfyUI HTTP（主路，跨网读远端）
          B. 本地 pynvml（仅代码与 GPU 同机时有效）
          C. 本地 nvidia-smi（同上）
          D. SSH 远程 nvidia-smi（mac 控远端 win10 架构的兜底）
        """
        # 方式 A：ComfyUI（最推荐，HTTP 直接读目标机器）
        if self.comfy_server:
            u = _from_comfyui(self.comfy_server, self.proxies)
            if u:
                return u

        # 方式 B：pynvml（本地有 GPU 时）
        u = _from_pynvml(self.gpu_index)
        if u:
            return u

        # 方式 C：本地 nvidia-smi（本地有 GPU 时）
        u = _from_nvidia_smi()
        if u:
            return u

        # 方式 D：SSH 远程 nvidia-smi（A 失败时的兜底——ComfyUI 挂了
        # 但机器还在，SSH 仍能读到真实显存）
        u = _from_ssh_nvidia_smi()
        if u:
            return u

        return None

    def _log_usage(self, u: VramUsage, prefix: str = ""):
        bar_len  = 20
        filled   = int(u.used_pct / 100 * bar_len)
        bar      = "█" * filled + "░" * (bar_len - filled)
        print(
            f"  {prefix}[GPU {u.source}] "
            f"{bar} {u.used_mb:.0f}/{u.total_mb:.0f} MB "
            f"({u.used_pct:.1f}%)"
        )

    # ── 核心：等待显存释放 ────────────────────────────

    def wait_if_needed(self, label: str = "") -> bool:
        """
        检查当前显存，如超出阈值则阻塞等待。
        返回 True 表示等待过，False 表示直接通过。

        label: 调用点描述，用于日志打印（如 "生图前" / "评分前"）
        """
        if not self.enabled:
            return False

        # v2.3.6：读不到显存 = gpu_guard 失明。不再静默放行——
        # 先延时重试几轮（ComfyUI 可能只是短暂无响应/SSH 抖动），
        # 仍读不到则打醒目告警，让操作者知道"守卫失效、正在盲跑"，
        # 然后才放行（放行是为了不死锁，但必须看得见）。
        u = self.get_usage()
        if u is None:
            tag = f"[{label}] " if label else ""
            blind_retries = 3
            for k in range(blind_retries):
                print(f"  ⚠ {tag}GpuGuard 读不到显存，"
                      f"{self.poll_s}s 后重试 ({k+1}/{blind_retries})...")
                time.sleep(self.poll_s)
                u = self.get_usage()
                if u is not None:
                    print(f"  ✅ {tag}GpuGuard 恢复读数")
                    break
            if u is None:
                print(f"\n  {'!' * 50}")
                print(f"  ⚠ {tag}GpuGuard 失明：ComfyUI HTTP 与 SSH 均读不到显存")
                print(f"  ⚠ 可能原因：ComfyUI 进程已挂 / 8188 端口不通 /")
                print(f"     win10 SSH 不可达。本次跳过显存检查，盲跑放行。")
                print(f"  ⚠ 若后续频繁 OOM 或 timeout，请先排查远端 ComfyUI。")
                print(f"  {'!' * 50}\n")
                log.warning("[GpuGuard] 失明，盲跑放行")
                return False
            # 重试中途恢复了读数 → 继续走下面正常流程

        tag = f"[{label}] " if label else ""

        # 仅警告
        if self.warn_mb <= u.used_mb < self.sleep_mb:
            self._log_usage(u, prefix=f"  ⚠ {tag}显存偏高，")

        # 触发等待
        if u.used_mb >= self.sleep_mb:
            print(f"\n  🛑 {tag}显存临界 ({u.used_mb:.0f} MB ≥ {self.sleep_mb} MB)，等待释放...")
            self._log_usage(u)
            waited     = 0
            did_wait   = True
            while u.used_mb > self.resume_mb:
                if waited >= self.max_wait_s:
                    print(f"  ⚠ 等待超过 {self.max_wait_s}s，强制继续（避免死锁）")
                    break
                time.sleep(self.poll_s)
                waited += self.poll_s
                u = self.get_usage()
                if u is None:
                    break
                self._log_usage(u, prefix=f"  等待中({waited}s) ")
            if u and u.used_mb <= self.resume_mb:
                print(f"  ✅ 显存已释放（{u.used_mb:.0f} MB），继续运行")
            return did_wait

        return False

    # ── 强制释放 + 轮询确认（v2.3.6 新增）────────────

    def free_and_wait(self, label: str = "",
                      target_mb: float = None,
                      max_wait_s: int = None,
                      min_wait_s: int = 5) -> bool:
        """
        发 /free 卸载 ComfyUI 模型，然后轮询显存直到真正降下来。

        替代旧写法 `free_comfyui_vram() + time.sleep(3)`：
          - 旧写法只发请求、固定睡 3 秒，不知道显存到底降没降。
          - 新写法发完请求后轮询，显存早降早返回（不傻等），
            降不下来则明确报警——这种情况通常是 PuLid 的 InsightFace
            （onnxruntime CUDA session）不归 ComfyUI 模型管理器管，
            /free 清不掉它。

        参数：
          target_mb:  显存降到此值以下视为成功；默认用 self.resume_mb
          max_wait_s: 最多等多久；默认用 self.max_wait_s
          min_wait_s: 至少等多久（给 /free 异步回收起步时间）

        返回 True=显存已降到目标值，False=超时仍未降下来（已报警）
        """
        if not self.enabled:
            self.free_comfyui_vram()
            return True

        target_mb  = target_mb  if target_mb  is not None else self.resume_mb
        max_wait_s = max_wait_s if max_wait_s is not None else self.max_wait_s
        tag = f"[{label}] " if label else ""

        # 1) 发 /free（unload_models + free_memory 都带上，
        #    尽量命中 PuLid 插件可能挂在任一事件上的卸载回调）
        self.free_comfyui_vram(unload_models=True, free_memory=True)

        # 2) 起步等待，给异步回收一点时间
        time.sleep(min_wait_s)

        # 3) 轮询确认显存真的降下来
        waited = min_wait_s
        while True:
            u = self.get_usage()
            if u is None:
                print(f"  [GpuGuard] {tag}无法读显存，跳过等待")
                return False
            if u.used_mb <= target_mb:
                print(f"  ✅ {tag}显存已降到 {u.used_mb:.0f} MB "
                      f"(≤ {target_mb:.0f})，继续")
                return True
            if waited >= max_wait_s:
                # 降不下来——重点提示 InsightFace 嫌疑
                print(f"\n  ⚠ {tag}显存等待 {waited}s 仍偏高 "
                      f"({u.used_mb:.0f} MB > {target_mb:.0f})")
                print(f"  ⚠ /free 可能清不掉 PuLid 的 InsightFace 显存。")
                print(f"  ⚠ 若评分频繁 timeout，建议把 config 里")
                print(f"     PULID_INSIGHTFACE_PROVIDER 改为 \"CPU\"，")
                print(f"     或让生图与评分使用不同 GPU。")
                self._log_usage(u, prefix="  当前 ")
                return False
            self._log_usage(u, prefix=f"  {tag}等待释放({waited}s) ")
            time.sleep(self.poll_s)
            waited += self.poll_s

    # ── 强制 ComfyUI 释放显存 ─────────────────────────

    def free_comfyui_vram(self, unload_models: bool = True,
                          free_memory: bool = True):
        """
        调用 ComfyUI 的 /free 接口，主动卸载模型释放显存。
        在生图完成、评分开始之前调用效果最好。
        """
        if not self.comfy_server:
            return
        try:
            import requests
            payload = {
                "unload_models": unload_models,
                "free_memory":   free_memory,
            }
            r = requests.post(
                f"{self.comfy_server}/free",
                json=payload,
                timeout=10,
                proxies=self.proxies,
            )
            if r.status_code == 200:
                print(f"  [GpuGuard] ComfyUI 已卸载模型，释放显存")
            else:
                log.warning(f"[GpuGuard] /free 返回 {r.status_code}")
        except Exception as e:
            log.warning(f"[GpuGuard] 调用 /free 失败: {e}")

    # ── 上下文管理器 ──────────────────────────────────

    def __enter__(self):
        self.wait_if_needed(label="任务前")
        return self

    def __exit__(self, *args):
        pass

    # ── 装饰器 ────────────────────────────────────────

    def guard(self, label: str = ""):
        """
        用法：
          @gpu_guard.guard("生图前")
          def generate(...): ...
        """
        def decorator(fn):
            @wraps(fn)
            def wrapper(*args, **kwargs):
                self.wait_if_needed(label=label or fn.__name__)
                return fn(*args, **kwargs)
            return wrapper
        return decorator

    # ── 状态打印 ──────────────────────────────────────

    def status(self) -> str:
        u = self.get_usage()
        if u is None:
            return "[GpuGuard] 无法读取显存"
        return (
            f"[GpuGuard] {u.used_mb:.0f}/{u.total_mb:.0f} MB "
            f"({u.used_pct:.1f}%)  via {u.source}"
        )


# ── 模块级单例 ───────────────────────────────────────────

gpu_guard = GpuGuard(
    warn_mb    = DEFAULT_WARN_MB,
    sleep_mb   = DEFAULT_SLEEP_MB,
    resume_mb  = DEFAULT_RESUME_MB,
    poll_s     = DEFAULT_POLL_S,
    max_wait_s = DEFAULT_MAX_WAIT_S,
    enabled    = True,
)


# ── CLI 快速诊断 ─────────────────────────────────────────

if __name__ == "__main__":
    print("GPU 显存状态诊断")
    print("=" * 40)

    # 尝试三种读取方式
    for name, fn in [
        ("ComfyUI /system_stats", lambda: _from_comfyui(
            gpu_guard.comfy_server, gpu_guard.proxies)),
        ("pynvml",                lambda: _from_pynvml()),
        ("nvidia-smi",            lambda: _from_nvidia_smi()),
        ("SSH nvidia-smi",        lambda: _from_ssh_nvidia_smi()),
    ]:
        u = fn()
        if u:
            print(f"  [{name}] {u.used_mb:.0f}/{u.total_mb:.0f} MB "
                  f"({u.used_pct:.1f}%)")
        else:
            print(f"  [{name}] 不可用")

    print()
    print(gpu_guard.status())
    print()
    print(f"  warn_mb   = {gpu_guard.warn_mb} MB  (超过此值打印警告)")
    print(f"  sleep_mb  = {gpu_guard.sleep_mb} MB  (超过此值开始等待)")
    print(f"  resume_mb = {gpu_guard.resume_mb} MB  (低于此值继续运行)")
