"""
tools/__init__.py
工具脚本层：lora_trainer / lora_tester / story_writer / test_*。
独立运行，不在主 pipeline 里。

sys.path 注入：
  - core/  → 工具可以调用 quality / renderer / orchestrator
  - infra/ → 工具可以调用 config / registry
  - 自身目录 + 项目根目录
"""
import sys
import os

_self_dir  = os.path.dirname(__file__)
_root_dir  = os.path.dirname(_self_dir)
_core_dir  = os.path.join(_root_dir, "core")
_infra_dir = os.path.join(_root_dir, "infra")

for p in (_infra_dir, _core_dir, _self_dir, _root_dir):
    if p not in sys.path and os.path.isdir(p):
        sys.path.insert(0, p)
