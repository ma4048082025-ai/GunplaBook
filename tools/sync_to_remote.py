"""
sync_to_remote.py ── Mac → Win10 文件同步工具
=================================================
基于 paramiko SFTP，用 config.REMOTE_SSH 凭证。

功能：
  - 把 Mac 上某目录同步到 Win10 上某路径
  - 已存在且大小一致的文件自动跳过（断点续传）
  - 自动创建远程不存在的目录
  - 进度打印

用法（命令行）：
  cd demo20260430
  python -m tools.sync_to_remote refs/voices/library/ H:/voice_refs/library/

用法（代码）：
  from tools.sync_to_remote import sync_dir
  sync_dir("refs/voices/library/", "H:/voice_refs/library/")
"""

import os
import sys
from pathlib import Path
from typing import Optional

import paramiko
from config import REMOTE_SSH


def _ensure_remote_dir(sftp: paramiko.SFTPClient, remote_path: str):
    """递归创建远程目录（H:/a/b/c 这种形式）"""
    # SFTP 只接受 unix 风格路径，但 Win10 OpenSSH 能识别 H:/xxx/yyy
    parts = remote_path.replace("\\", "/").rstrip("/").split("/")
    # 处理盘符（H:）
    cur = ""
    for i, p in enumerate(parts):
        if i == 0 and p.endswith(":"):
            cur = p + "/"
            continue
        cur = cur + p if cur.endswith("/") else cur + "/" + p
        try:
            sftp.stat(cur)
        except IOError:
            try:
                sftp.mkdir(cur)
                print(f"  [sync] mkdir {cur}")
            except IOError as e:
                # 可能是权限或路径问题，但下游 put 会再报，这里只警告
                print(f"  [sync] mkdir 失败（继续）: {cur} → {e}")


def sync_dir(local_dir: str, remote_dir: str,
             ssh_cfg: Optional[dict] = None,
             verbose: bool = True) -> int:
    """
    同步本地目录 → 远程目录。

    返回：实际上传的文件数（已跳过的不计入）

    参数：
      local_dir:  Mac 本地目录（如 "refs/voices/library/"）
      remote_dir: Win10 目录（如 "H:/voice_refs/library/"）
      ssh_cfg:    覆盖默认 SSH 配置（默认用 config.REMOTE_SSH）
      verbose:    是否打印细节
    """
    cfg = ssh_cfg or REMOTE_SSH

    local_path = Path(local_dir).resolve()
    if not local_path.exists():
        raise FileNotFoundError(f"本地目录不存在: {local_dir}")
    if not local_path.is_dir():
        raise NotADirectoryError(f"不是目录: {local_dir}")

    print(f"\n  [sync] {local_path}")
    print(f"  [sync]   ↓")
    print(f"  [sync] {cfg['host']}:{remote_dir}")

    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(
        cfg["host"],
        port     = cfg["port"],
        username = cfg["username"],
        password = cfg["password"],
        timeout  = 30,
    )
    sftp = ssh.open_sftp()

    # 确保远程根目录存在
    _ensure_remote_dir(sftp, remote_dir.rstrip("/"))

    uploaded = 0
    skipped  = 0
    failed   = 0

    for local_file in sorted(local_path.rglob("*")):
        if not local_file.is_file():
            continue
        # 跳过隐藏文件
        if local_file.name.startswith("."):
            continue

        rel       = local_file.relative_to(local_path)
        remote_fp = remote_dir.rstrip("/") + "/" + str(rel).replace("\\", "/")

        # 确保远程子目录存在
        if "/" in str(rel):
            _ensure_remote_dir(sftp, str(Path(remote_fp).parent).replace("\\", "/"))

        # 检查远程是否已存在且大小一致
        try:
            remote_stat = sftp.stat(remote_fp)
            if remote_stat.st_size == local_file.stat().st_size:
                skipped += 1
                if verbose:
                    print(f"  [sync] = {rel} (skipped, same size)")
                continue
        except IOError:
            pass  # 远程不存在，正常上传

        try:
            sftp.put(str(local_file), remote_fp)
            uploaded += 1
            if verbose:
                size_kb = local_file.stat().st_size / 1024
                print(f"  [sync] ↑ {rel} ({size_kb:.1f} KB)")
        except Exception as e:
            failed += 1
            print(f"  [sync] ✗ {rel} → {e}")

    sftp.close()
    ssh.close()

    print(f"\n  [sync] 完成: {uploaded} 上传, {skipped} 跳过, {failed} 失败\n")
    return uploaded


def test_remote_path(remote_path: str,
                     ssh_cfg: Optional[dict] = None) -> bool:
    """
    测试远程路径是否存在/可访问。
    返回 True 表示可访问。
    """
    cfg = ssh_cfg or REMOTE_SSH
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        ssh.connect(cfg["host"], port=cfg["port"],
                    username=cfg["username"], password=cfg["password"],
                    timeout=10)
        sftp = ssh.open_sftp()
        try:
            sftp.stat(remote_path)
            print(f"  [sync] ✓ 远程路径可访问: {remote_path}")
            return True
        except IOError:
            print(f"  [sync] ✗ 远程路径不存在: {remote_path}")
            return False
        finally:
            sftp.close()
            ssh.close()
    except Exception as e:
        print(f"  [sync] ✗ SSH 连接失败: {e}")
        return False


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("用法: python -m tools.sync_to_remote <本地目录> <远程目录>")
        print("例：python -m tools.sync_to_remote refs/voices/library/ H:/voice_refs/library/")
        sys.exit(1)

    sync_dir(sys.argv[1], sys.argv[2])
