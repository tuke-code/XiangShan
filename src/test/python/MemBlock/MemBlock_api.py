# coding=utf-8
"""
MemBlock DUT 创建与基础 API。

本文件只提供环境层验证所需的最小接口：
  1. `create_dut(request)`：创建 DUT 实例
  2. `dut` fixture：管理 DUT 生命周期
"""

import os
import sys

try:
    import pytest
except ImportError:
    class _PytestStub:
        @staticmethod
        def fixture(*_args, **_kwargs):
            def _decorator(func):
                return func

            return _decorator

    pytest = _PytestStub()


_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.abspath(os.path.join(_HERE, "..", "..", "..", ".."))
_PYLIB = os.path.join(_REPO_ROOT, "build-memblock", "pylib")

for _path in (_PYLIB, _HERE):
    if _path not in sys.path:
        sys.path.insert(0, _path)


try:
    from toffee_test.reporter import (
        get_file_in_tmp_dir,
        set_line_coverage,
        set_title_info,
        set_user_info,
    )
except ImportError:
    def get_file_in_tmp_dir(_request, directory, file_name, new_path=True):
        os.makedirs(directory, exist_ok=True)
        return os.path.join(directory, file_name)

    def set_line_coverage(*_args, **_kwargs):
        return None

    def set_title_info(*_args, **_kwargs):
        return None

    def set_user_info(*_args, **_kwargs):
        return None


def _current_path_file(file_name: str) -> str:
    """返回与当前文件同目录的绝对路径。"""

    return os.path.join(_HERE, file_name)


def get_coverage_data_path(request, new_path: bool) -> str:
    """
    获取代码行覆盖率文件路径。

    Args:
        request: pytest request 对象。
        new_path (bool): True 时创建新路径，False 时获取既有路径。

    Returns:
        str: 覆盖率文件绝对路径。
    """

    tc_name = request.node.name if request is not None else "MemBlock"
    return get_file_in_tmp_dir(
        request,
        _current_path_file("data"),
        f"{tc_name}.dat",
        new_path=new_path,
    )


def get_waveform_path(request, new_path: bool) -> str:
    """
    获取波形文件路径。

    Args:
        request: pytest request 对象。
        new_path (bool): True 时创建新路径，False 时获取既有路径。

    Returns:
        str: 波形文件绝对路径。
    """

    tc_name = request.node.name if request is not None else "MemBlock"
    return get_file_in_tmp_dir(
        request,
        _current_path_file("data"),
        f"{tc_name}.fst",
        new_path=new_path,
    )


def create_dut(request):
    """
    创建 MemBlock DUT 实例。

    Args:
        request: pytest request 对象。

    Returns:
        DUTMemBlock: 已完成时钟、波形和覆盖率配置的 DUT 实例。
    """

    try:
        import ucagent

        if hasattr(ucagent, "is_imp_test_template") and ucagent.is_imp_test_template():
            from MemBlock import DUTMemBlock as _DUT

            return ucagent.get_fake_dut(_DUT)
    except ImportError:
        pass

    from MemBlock import DUTMemBlock

    dut = DUTMemBlock()
    dut.SetCoverage(get_coverage_data_path(request, new_path=True))
    dut.SetWaveform(get_waveform_path(request, new_path=True))
    dut.InitClock("clock")
    return dut


@pytest.fixture(scope="function")
def dut(request):
    """
    MemBlock DUT fixture。

    Scope 为 `function`，确保每个测试用例独占一个全新的 DUT 实例。
    """

    _dut = create_dut(request)
    yield _dut

    set_line_coverage(
        request,
        get_coverage_data_path(request, new_path=False),
        ignore=_current_path_file("MemBlock.ignore"),
    )
    set_user_info("MemBlock-Testbench-v0.2", "xiangshan@example.com")
    set_title_info("MemBlock Environment Test Report")
    _dut.Finish()
