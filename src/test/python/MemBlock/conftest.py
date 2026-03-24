# coding=utf-8
"""
MemBlock 验证环境 pytest 全局配置。

本文件负责：
  1. 将 `build-memblock/pylib` 加入 Python 搜索路径
  2. 将当前目录加入 Python 搜索路径
  3. 确保 `data/` 目录存在，供波形和覆盖率文件使用
"""

import os
import sys


_TEST_ROOT = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.abspath(os.path.join(_TEST_ROOT, "..", "..", "..", ".."))
_PYLIB_PATH = os.path.join(_REPO_ROOT, "build-memblock", "pylib")
_DATA_DIR = os.path.join(_TEST_ROOT, "data")

for _path in (_PYLIB_PATH, _TEST_ROOT):
    if _path not in sys.path:
        sys.path.insert(0, _path)

os.makedirs(_DATA_DIR, exist_ok=True)
