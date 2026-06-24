"""
infra/__init__.py
基础设施层：config / registry / gpu_guard
被 core/ 和 tools/ 调用。

sys.path 注入：自身目录 + 项目根目录
让 infra/ 内文件可以平铺 import（如 from config import LLM_API_KEY）
"""
import sys
import os

_self_dir = os.path.dirname(__file__)
_root_dir = os.path.dirname(_self_dir)

if _self_dir not in sys.path:
    sys.path.insert(0, _self_dir)
if _root_dir not in sys.path:
    sys.path.insert(0, _root_dir)
