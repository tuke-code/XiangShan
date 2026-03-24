# coding=utf-8
"""
tests/ 子目录 pytest 配置。

将上层 `MemBlock/` 目录加入路径，并启用其中定义的 fixture。
"""

import os
import sys


_PARENT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _PARENT not in sys.path:
    sys.path.insert(0, _PARENT)


pytest_plugins = [
    "MemBlock_api",
    "MemBlock_env",
]
