"""
core/__init__.py
流程层：pipeline / orchestrator / quality / renderer 等。
被 run.py 和 tools/ 调用。

sys.path 注入：
  - 自身目录       → core 内文件互相平铺 import
  - infra/ 目录    → core 文件可以 from config import ...
  - 项目根目录     → 兼容某些可能从根目录 import 的写法
"""
import sys
import os

_self_dir  = os.path.dirname(__file__)
_root_dir  = os.path.dirname(_self_dir)
_infra_dir = os.path.join(_root_dir, "infra")

for p in (_infra_dir, _self_dir, _root_dir):
    if p not in sys.path and os.path.isdir(p):
        sys.path.insert(0, p)
