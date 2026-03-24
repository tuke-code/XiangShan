# coding=utf-8
"""
MemBlock DUT Wrapper 与基础 API

本文件提供：
  1. create_dut(request)        ：DUT 实例工厂函数
  2. dut (pytest fixture)       ：管理 DUT 完整生命周期
  3. api_MemBlock_*             ：面向测试人员的高层次操作 API

API 命名规范：api_MemBlock_<功能描述>

使用示例：
  from MemBlock_api import api_MemBlock_reset, api_MemBlock_step

  def test_example(env):
      api_MemBlock_reset(env, max_cycles=100)
      api_MemBlock_step(env, n=10, max_cycles=100)
"""

import os
import sys
import pytest

# ──────────────────────────────────────────────────────────────────────────────
# 路径配置（若通过 conftest.py 已配置则此处幂等）
# ──────────────────────────────────────────────────────────────────────────────
_HERE      = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.abspath(os.path.join(_HERE, "..", "..", "..", ".."))
_PYLIB     = os.path.join(_REPO_ROOT, "build-memblock", "pylib")
for _p in [_PYLIB, _HERE]:
    if _p not in sys.path:
        sys.path.insert(0, _p)

from toffee_test.reporter import (
    get_file_in_tmp_dir,
    set_func_coverage,
    set_line_coverage,
    set_user_info,
    set_title_info,
)
from MemBlock_function_coverage_def import get_coverage_groups


# ──────────────────────────────────────────────────────────────────────────────
# 辅助路径函数
# ──────────────────────────────────────────────────────────────────────────────
def _current_path_file(file_name: str) -> str:
    """返回与本文件同目录下的 file_name 的绝对路径。"""
    return os.path.join(_HERE, file_name)


def get_coverage_data_path(request, new_path: bool) -> str:
    """
    获取代码行覆盖率数据文件路径。

    Args:
        request  : pytest request 对象（可为 None，此时使用 'MemBlock' 作为名称）。
        new_path : True 时生成新路径（测试开始时调用），
                   False 时获取已有路径（测试结束时调用）。

    Returns:
        str: 覆盖率文件的绝对路径（.dat 格式）。
    """
    tc_name = request.node.name if request is not None else "MemBlock"
    return get_file_in_tmp_dir(
        request,
        _current_path_file("data/"),
        f"{tc_name}.dat",
        new_path=new_path,
    )


def get_waveform_path(request, new_path: bool) -> str:
    """
    获取波形文件路径。

    Args:
        request  : pytest request 对象。
        new_path : True 时生成新路径，False 时获取已有路径。

    Returns:
        str: 波形文件的绝对路径（.fst 格式）。
    """
    tc_name = request.node.name if request is not None else "MemBlock"
    return get_file_in_tmp_dir(
        request,
        _current_path_file("data/"),
        f"{tc_name}.fst",
        new_path=new_path,
    )


# ──────────────────────────────────────────────────────────────────────────────
# DUT 创建函数
# ──────────────────────────────────────────────────────────────────────────────
def create_dut(request):
    """
    创建并初始化 MemBlock DUT 实例。

    本函数完成以下步骤：
      1. 从 build-memblock/pylib 导入 DUTMemBlock 类
      2. 实例化 DUT
      3. 配置代码行覆盖率输出文件
      4. 配置波形输出文件（FST 格式）
      5. 绑定时钟引脚（clock），使 dut.Step / dut.StepRis 正常工作

    Args:
        request: pytest request fixture 对象。

    Returns:
        DUTMemBlock: 已完成基本初始化的 DUT 实例。
    """
    try:
        import ucagent
        if ucagent.is_imp_test_template():
            from MemBlock import DUTMemBlock as _DUT
            return ucagent.get_fake_dut(_DUT)
    except ImportError:
        pass

    # 导入 DUT 类
    from MemBlock import DUTMemBlock

    # 实例化
    dut = DUTMemBlock()

    # 配置行覆盖率（必须在运行前设置，否则无法统计覆盖率）
    dut.SetCoverage(get_coverage_data_path(request, new_path=True))

    # 配置波形输出
    dut.SetWaveform(get_waveform_path(request, new_path=True))

    # 绑定时钟：MemBlock 是时序电路，必须初始化时钟
    dut.InitClock("clock")

    return dut


# ──────────────────────────────────────────────────────────────────────────────
# dut Fixture
# ──────────────────────────────────────────────────────────────────────────────
@pytest.fixture(scope="function")
def dut(request):
    """
    MemBlock DUT pytest fixture。

    管理 DUT 从创建到销毁的完整生命周期：
      - 创建 DUT 实例
      - 注册功能覆盖率组并在每个上升沿自动采样
      - yield DUT 供测试用例使用
      - teardown：收集功能覆盖率、行覆盖率，清理资源

    Scope: function（每个测试用例独立创建 DUT 实例）
    """
    # 1. 创建 DUT
    _dut = create_dut(request)

    # 2. 获取功能覆盖率组
    func_coverage_group = get_coverage_groups(_dut)

    # 3. 注册上升沿采样回调（每个时钟周期的上升沿自动采样功能覆盖率）
    _dut.StepRis(lambda _: [g.sample() for g in func_coverage_group])

    # 4. 将覆盖率字典挂载到 dut 实例，方便测试用例查询
    setattr(_dut, "fc_cover", {g.name: g for g in func_coverage_group})

    # 5. 将 DUT 交给测试用例
    yield _dut

    # ── teardown ──────────────────────────────────────────────────────────────
    # 6. 上报功能覆盖率
    set_func_coverage(request, func_coverage_group)

    # 7. 上报代码行覆盖率
    set_line_coverage(
        request,
        get_coverage_data_path(request, new_path=False),
        ignore=_current_path_file("MemBlock.ignore"),
    )

    # 8. 写入报告元数据
    set_user_info("MemBlock-Testbench-v0.1", "xiangshan@example.com")
    set_title_info("MemBlock Test Report")

    # 9. 清空覆盖率统计，释放 DUT 资源
    for g in func_coverage_group:
        g.clear()
    _dut.Finish()


# ══════════════════════════════════════════════════════════════════════════════
# 基础 API（api_MemBlock_* 前缀）
# ══════════════════════════════════════════════════════════════════════════════

def api_MemBlock_reset(env, cycles: int = 10, max_cycles: int = 200) -> None:
    """
    对 MemBlock DUT 执行同步复位操作。

    将 reset 信号拉高保持 ``cycles`` 个时钟周期，随后拉低并再推进
    若干周期，使 DUT 进入稳定初始状态。

    Args:
        env       : MemBlockEnv 实例（由 env fixture 提供）。
        cycles    : reset 信号保持高电平的时钟周期数，默认为 10。
        max_cycles: API 允许消耗的最大时钟周期数，默认为 200。

    Returns:
        None
    """
    dut = env.dut
    assert cycles < max_cycles, "reset cycles 不能超过 max_cycles"

    # 拉高 reset
    dut.reset.value = 1
    dut.Step(cycles)

    # 拉低 reset，再等待若干周期让内部状态稳定
    dut.reset.value = 0
    dut.Step(min(cycles, max_cycles - cycles))


def api_MemBlock_step(env, n: int = 1, max_cycles: int = 1000) -> None:
    """
    推进 DUT 运行 n 个时钟周期。

    这是最基础的时钟驱动 API，直接封装 dut.Step()。
    功能覆盖率采样由 dut fixture 在每个上升沿自动触发。

    Args:
        env       : MemBlockEnv 实例。
        n         : 推进的时钟周期数，默认为 1。
        max_cycles: 允许的最大周期数，超过则截断，默认为 1000。

    Returns:
        None
    """
    env.dut.Step(min(n, max_cycles))


def api_MemBlock_set_hartid(env, hartid: int = 0, max_cycles: int = 10) -> None:
    """
    设置 MemBlock 的硬件线程 ID（hartId）。

    hartId 用于多核场景下标识当前 hart，范围由参数位宽（6bit）决定。

    Args:
        env       : MemBlockEnv 实例。
        hartid    : 硬件线程 ID（0~63），默认为 0。
        max_cycles: 保留参数，确保 API 接口一致性，默认为 10。

    Returns:
        None
    """
    env.dut.io_hartId.value = hartid & 0x3F
    env.dut.Step(1)


def api_MemBlock_send_redirect(
    env,
    valid: int = 1,
    level: int = 0,
    rob_idx_flag: int = 0,
    rob_idx_value: int = 0,
    max_cycles: int = 20,
) -> None:
    """
    向 MemBlock 发送流水线重定向信号，模拟后端分支预测失败或异常触发。

    重定向信号在当前周期有效，下一个周期自动清除（pulse 模式）。

    Args:
        env          : MemBlockEnv 实例。
        valid        : 重定向有效标志，1 = 有效，默认为 1。
        level        : 刷新级别，0 = 分支预测错误，1 = 异常/中断，默认为 0。
        rob_idx_flag : ROB 索引高位 flag 字段，默认为 0。
        rob_idx_value: ROB 索引 value 字段（9bit），默认为 0。
        max_cycles   : API 允许消耗的最大时钟周期数，默认为 20。

    Returns:
        None
    """
    dut = env.dut

    # 驱动重定向端口
    dut.io_redirect_valid.value             = valid & 0x1
    dut.io_redirect_bits_level.value        = level & 0x1
    dut.io_redirect_bits_robIdx_flag.value  = rob_idx_flag & 0x1
    dut.io_redirect_bits_robIdx_value.value = rob_idx_value & 0x1FF

    # 保持一个周期
    dut.Step(1)

    # 清除（防止持续影响后续状态）
    dut.io_redirect_valid.value = 0
    dut.Step(min(1, max_cycles - 1))


def api_MemBlock_enq_lsq(
    env,
    port: int = 0,
    is_store: int = 0,
    rob_idx_flag: int = 0,
    rob_idx_value: int = 0,
    sq_idx_flag: int = 0,
    sq_idx_value: int = 0,
    lq_idx: int = 0,
    max_cycles: int = 50,
) -> None:
    """
    向 MemBlock 的 LSQ 发送入队分配请求（模拟后端 Dispatch 阶段）。

    XiangShan 支持 8 路同时入队（port 0~7），本 API 操作单个端口。
    请求在当前周期有效，下一周期自动清除。

    Args:
        env          : MemBlockEnv 实例。
        port         : LSQ 入队端口编号（0~7），默认为 0。
        is_store     : 1 = Store 指令，0 = Load 指令，默认为 0。
        rob_idx_flag : ROB 索引 flag，默认为 0。
        rob_idx_value: ROB 索引 value（10bit），默认为 0。
        sq_idx_flag  : SQ 索引 flag，默认为 0。
        sq_idx_value : SQ 索引 value（6bit），默认为 0。
        lq_idx       : LQ 索引（7bit），默认为 0。
        max_cycles   : 允许的最大时钟周期数，默认为 50。

    Returns:
        None

    Raises:
        AssertionError: port 超出范围（0~7）时抛出。
    """
    assert 0 <= port <= 7, f"LSQ 入队端口编号必须在 0~7 范围内，当前为 {port}"

    dut = env.dut
    p   = port  # 端口简写

    # 按端口号动态获取信号属性并赋值
    getattr(dut, f"io_ooo_to_mem_enqLsq_req_{p}_valid").value         = 1
    getattr(dut, f"io_ooo_to_mem_enqLsq_req_{p}_bits_isStore").value  = is_store & 0x1
    getattr(dut, f"io_ooo_to_mem_enqLsq_req_{p}_bits_robIdx_flag").value  = rob_idx_flag & 0x1
    getattr(dut, f"io_ooo_to_mem_enqLsq_req_{p}_bits_robIdx_value").value = rob_idx_value & 0x3FF
    getattr(dut, f"io_ooo_to_mem_enqLsq_req_{p}_bits_sqIdx_flag").value   = sq_idx_flag & 0x1
    getattr(dut, f"io_ooo_to_mem_enqLsq_req_{p}_bits_sqIdx_value").value  = sq_idx_value & 0x3F
    getattr(dut, f"io_ooo_to_mem_enqLsq_req_{p}_bits_lqIdx").value        = lq_idx & 0x7F

    # 保持一个周期
    dut.Step(1)

    # 清除 valid
    getattr(dut, f"io_ooo_to_mem_enqLsq_req_{p}_valid").value = 0
    dut.Step(min(1, max_cycles - 1))


def api_MemBlock_send_int_issue(
    env,
    port: int = 0,
    valid: int = 1,
    max_cycles: int = 50,
) -> None:
    """
    向 MemBlock 发射整数访存指令（模拟后端发射队列出队）。

    本 API 驱动指定端口的 valid 信号（握手协议），实际 bits 字段
    需在调用前通过底层方式设置，或使用后续专用 API。

    Args:
        env       : MemBlockEnv 实例。
        port      : 整数访存发射端口编号（0~6），默认为 0。
        valid     : 发射有效信号，1 = 有效，默认为 1。
        max_cycles: 最大等待周期数，默认为 50。

    Returns:
        None

    Raises:
        AssertionError: port 超出范围（0~6）时抛出。
    """
    assert 0 <= port <= 6, f"整数发射端口编号必须在 0~6 范围内，当前为 {port}"

    dut = env.dut
    getattr(dut, f"io_ooo_to_mem_intIssue_{port}_valid").value = valid & 0x1
    dut.Step(1)

    # 清除 valid
    getattr(dut, f"io_ooo_to_mem_intIssue_{port}_valid").value = 0
    dut.Step(min(1, max_cycles - 1))


def api_MemBlock_read_lq_deq_ptr(env, max_cycles: int = 10) -> int:
    """
    读取 Load Queue 出队指针（lqDeqPtr）。

    出队指针用于监控 LQ 的消费进度，可用于验证 Load 指令是否
    已被正确提交和出队。

    Args:
        env       : MemBlockEnv 实例。
        max_cycles: 保留参数，确保 API 接口一致性，默认为 10。

    Returns:
        int: 当前 LQ 出队指针的 value 字段（7bit）。
    """
    env.dut.Step(1)
    return env.dut.io_mem_to_ooo_lqDeqPtr_value.value


def api_MemBlock_read_sq_deq_ptr(env, max_cycles: int = 10) -> int:
    """
    读取 Store Queue 出队指针（sqDeqPtr）。

    出队指针用于监控 SQ 的消费进度，可用于验证 Store 指令是否
    已被提交出队。

    Args:
        env       : MemBlockEnv 实例。
        max_cycles: 保留参数，确保 API 接口一致性，默认为 10。

    Returns:
        int: 当前 SQ 出队指针的 value 字段（6bit）。
    """
    env.dut.Step(1)
    return env.dut.io_mem_to_ooo_sqDeqPtr_value.value


def api_MemBlock_set_tl_d_response(
    env,
    valid: int = 1,
    opcode: int = 4,
    param: int = 0,
    size: int = 6,
    source: int = 0,
    sink: int = 0,
    data: int = 0,
    max_cycles: int = 20,
) -> None:
    """
    驱动 TileLink D 通道，向 DCache 返回 L2 Cache 响应（模拟 L2 行为）。

    D 通道用于 L2 向 DCache 返回 Grant/GrantData 响应，
    本 API 将响应保持一个周期后自动清除。

    TileLink D 通道 opcode：
      4 = GrantData（带数据的授权，用于 AcquireBlock 响应）
      5 = Grant    （无数据的授权，用于 AcquirePerm 响应）
      6 = ReleaseAck（释放确认）

    Args:
        env       : MemBlockEnv 实例。
        valid     : D 通道有效标志，默认为 1。
        opcode    : TileLink D 通道操作码（3bit），默认为 4（GrantData）。
        param     : TileLink D 通道 param 字段（2bit），默认为 0。
        size      : 传输大小的 log2 值（3bit），默认为 6（64B Cache 行）。
        source    : 事务源 ID（4bit），默认为 0。
        sink      : 事务 sink ID（默认 0）。
        data      : 响应数据（64bit），默认为 0。
        max_cycles: 最大周期数，默认为 20。

    Returns:
        None
    """
    dut = env.dut

    # 驱动 D 通道
    dut.auto_inner_buffers_out_d_valid.value          = valid & 0x1
    dut.auto_inner_buffers_out_d_bits_opcode.value    = opcode & 0x7
    dut.auto_inner_buffers_out_d_bits_param.value     = param & 0x3
    dut.auto_inner_buffers_out_d_bits_size.value      = size & 0x7
    dut.auto_inner_buffers_out_d_bits_source.value    = source & 0xF
    dut.auto_inner_buffers_out_d_bits_sink.value      = sink & 0x1
    dut.auto_inner_buffers_out_d_bits_data.value      = data & 0xFFFFFFFFFFFFFFFF

    dut.Step(1)

    # 清除
    dut.auto_inner_buffers_out_d_valid.value = 0
    dut.Step(min(1, max_cycles - 1))


def api_MemBlock_wait_for_tl_a_request(
    env,
    timeout_cycles: int = 200,
    max_cycles: int = 300,
) -> bool:
    """
    等待 DCache 通过 TileLink A 通道发出访存请求（DCache miss 场景）。

    在 timeout_cycles 个时钟周期内轮询 TileLink A 通道的 valid 信号，
    一旦检测到有效请求即返回 True；超时则返回 False。

    Args:
        env           : MemBlockEnv 实例。
        timeout_cycles: 最大等待周期数，默认为 200。
        max_cycles    : API 允许消耗的最大总周期数，默认为 300。

    Returns:
        bool: True 表示检测到 TileLink A 通道请求，False 表示超时。
    """
    dut       = env.dut
    limit     = min(timeout_cycles, max_cycles)

    for _ in range(limit):
        dut.Step(1)
        if dut.auto_inner_buffers_out_a_valid.value == 1:
            return True

    return False
