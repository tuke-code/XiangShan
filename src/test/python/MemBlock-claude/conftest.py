# coding=utf-8
"""
MemBlock 验证环境 pytest 全局配置

本文件配置 pytest 运行所需的路径和插件，确保 DUT Python 库、
toffee 框架和验证环境模块均可被正常导入。
"""

import sys
import os

# ──────────────────────────────────────────────────────────────────────────────
# 路径配置：将 build-memblock/pylib 加入 Python 模块搜索路径
# ──────────────────────────────────────────────────────────────────────────────
_REPO_ROOT = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "..")
)
_PYLIB_PATH = os.path.join(_REPO_ROOT, "build-memblock", "pylib")
_TEST_ROOT  = os.path.dirname(__file__)

for _p in [_PYLIB_PATH, _TEST_ROOT]:
    if _p not in sys.path:
        sys.path.insert(0, _p)

# ──────────────────────────────────────────────────────────────────────────────
# 确保 data/ 目录存在（用于存放覆盖率和波形文件）
# ──────────────────────────────────────────────────────────────────────────────
_DATA_DIR = os.path.join(_TEST_ROOT, "data")
os.makedirs(_DATA_DIR, exist_ok=True)
