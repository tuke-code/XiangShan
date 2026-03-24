# coding=utf-8
"""
tests/ 子目录 pytest 配置

将上层 MemBlock/ 目录加入路径，使测试文件可以直接导入
MemBlock_api、MemBlock_env、MemBlock_function_coverage_def。
"""
import sys
import os

_PARENT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _PARENT not in sys.path:
    sys.path.insert(0, _PARENT)

# 引入上层 conftest 中的 fixture（dut / env）
pytest_plugins = [
    "MemBlock_api",   # 提供 dut fixture
    "MemBlock_env",   # 提供 env fixture
]
