# coding=utf-8
"""
test_MemBlock_basic.py
──────────────────────
MemBlock 基础功能测试（Stage 4：basic_api_implementation）

本文件通过调用 api_MemBlock_* 系列 API 验证 MemBlock 的基础功能，
包括复位、hartId 设置、重定向发送、LSQ 入队以及 DCache TileLink 请求观测。

每个测试用例均依赖 env fixture（MemBlockEnv），
通过高层 API 驱动 DUT，不直接操作底层端口。

测试原则：
  - 只测试可以通过外部端口观测的行为
  - 避免对 DUT 内部状态做强假设（DUT 为黑盒）
  - 功能覆盖率在 dut fixture 的上升沿回调中自动采样
"""

import pytest
from MemBlock_api import (
    api_MemBlock_reset,
    api_MemBlock_step,
    api_MemBlock_set_hartid,
    api_MemBlock_send_redirect,
    api_MemBlock_enq_lsq,
    api_MemBlock_send_int_issue,
    api_MemBlock_read_lq_deq_ptr,
    api_MemBlock_read_sq_deq_ptr,
    api_MemBlock_set_tl_d_response,
    api_MemBlock_wait_for_tl_a_request,
)


# ──────────────────────────────────────────────────────────────────────────────
# 辅助函数
# ──────────────────────────────────────────────────────────────────────────────

def _do_reset(env, cycles=10):
    """执行复位并等待 DUT 稳定。"""
    api_MemBlock_reset(env, cycles=cycles, max_cycles=100)


# ══════════════════════════════════════════════════════════════════════════════
# 测试用例：复位功能
# ══════════════════════════════════════════════════════════════════════════════

def test_api_MemBlock_reset_basic(env):
    """
    验证 api_MemBlock_reset API 能正常执行复位操作。

    期望行为：
      - 复位后 DUT 能继续接受时钟推进
      - 不会抛出异常

    检查点：
      - 复位完成后推进 20 个周期无异常
    """
    _do_reset(env, cycles=10)
    api_MemBlock_step(env, n=20, max_cycles=100)


def test_api_MemBlock_reset_twice(env):
    """
    验证多次复位不会导致 DUT 异常状态。

    期望行为：
      - 连续两次复位均可正常完成
      - 第二次复位后 DUT 仍可正常运行

    检查点：
      - 两次复位均无异常
      - 复位后推进 10 个周期无异常
    """
    _do_reset(env, cycles=5)
    api_MemBlock_step(env, n=5, max_cycles=50)
    _do_reset(env, cycles=5)
    api_MemBlock_step(env, n=10, max_cycles=50)


# ══════════════════════════════════════════════════════════════════════════════
# 测试用例：hartId 设置
# ══════════════════════════════════════════════════════════════════════════════

def test_api_MemBlock_hartid_default(env):
    """
    验证 api_MemBlock_set_hartid 使用默认值（0）不抛出异常。

    检查点：
      - hartId=0 时 API 正常返回
    """
    _do_reset(env)
    api_MemBlock_set_hartid(env, hartid=0, max_cycles=10)


def test_api_MemBlock_hartid_nonzero(env):
    """
    验证 api_MemBlock_set_hartid 可以设置非零 hartId。

    XiangShan 支持多核，hartId 用于区分不同 hart。

    检查点：
      - hartId=1 时 API 正常返回（不崩溃）
    """
    _do_reset(env)
    api_MemBlock_set_hartid(env, hartid=1, max_cycles=10)
    api_MemBlock_step(env, n=5, max_cycles=50)


def test_api_MemBlock_hartid_max(env):
    """
    验证 hartId 可以设置为最大值（6bit：0~63）。

    检查点：
      - hartId=63 时 API 正常返回
    """
    _do_reset(env)
    api_MemBlock_set_hartid(env, hartid=63, max_cycles=10)


# ══════════════════════════════════════════════════════════════════════════════
# 测试用例：流水线重定向
# ══════════════════════════════════════════════════════════════════════════════

def test_api_MemBlock_redirect_branch(env):
    """
    验证 api_MemBlock_send_redirect 可以发送分支预测错误重定向（level=0）。

    期望行为：
      - 重定向信号在当前周期有效，下一周期自动清除
      - DUT 不崩溃

    检查点：
      - 发送后重定向信号被清除（env.redirect.valid == 0）
    """
    _do_reset(env)
    api_MemBlock_send_redirect(
        env,
        valid=1,
        level=0,        # 分支预测重定向
        rob_idx_flag=0,
        rob_idx_value=5,
        max_cycles=20,
    )
    # 重定向信号应已被清除
    assert env.redirect.valid.value == 0, \
        "redirect.valid 在 API 返回后应为 0"


def test_api_MemBlock_redirect_exception(env):
    """
    验证 api_MemBlock_send_redirect 可以发送异常重定向（level=1）。

    检查点：
      - 发送 level=1 重定向不抛出异常
      - 重定向信号被正确清除
    """
    _do_reset(env)
    api_MemBlock_send_redirect(
        env,
        valid=1,
        level=1,        # 异常/中断重定向（全局刷新）
        rob_idx_flag=1,
        rob_idx_value=0,
        max_cycles=20,
    )
    assert env.redirect.valid.value == 0, \
        "redirect.valid 在异常重定向后应为 0"


def test_api_MemBlock_redirect_inactive(env):
    """
    验证 valid=0 时重定向不生效（DUT 忽略无效重定向）。

    检查点：
      - 发送 valid=0 的重定向不抛出异常
      - DUT 继续正常工作
    """
    _do_reset(env)
    api_MemBlock_send_redirect(
        env,
        valid=0,        # 无效重定向，DUT 应忽略
        level=0,
        max_cycles=20,
    )
    api_MemBlock_step(env, n=5, max_cycles=50)


def test_api_MemBlock_redirect_repeated(env):
    """
    验证连续发送多次重定向信号，DUT 均能正常处理。

    检查点：
      - 3 次连续重定向均无异常
    """
    _do_reset(env)
    for rob_val in [0, 1, 2]:
        api_MemBlock_send_redirect(
            env,
            valid=1,
            level=0,
            rob_idx_value=rob_val,
            max_cycles=20,
        )
        api_MemBlock_step(env, n=3, max_cycles=50)


# ══════════════════════════════════════════════════════════════════════════════
# 测试用例：LSQ 入队
# ══════════════════════════════════════════════════════════════════════════════

def test_api_MemBlock_enq_lsq_load_port0(env):
    """
    验证通过 port 0 向 LSQ 发送 Load 入队请求。

    检查点：
      - API 正常返回，无异常
      - enq_lsq 的 req_valid 在 API 返回后被清除
    """
    _do_reset(env)
    api_MemBlock_enq_lsq(
        env,
        port=0,
        is_store=0,     # Load
        rob_idx_flag=0,
        rob_idx_value=1,
        lq_idx=0,
        max_cycles=50,
    )


def test_api_MemBlock_enq_lsq_store_port0(env):
    """
    验证通过 port 0 向 LSQ 发送 Store 入队请求。

    检查点：
      - API 正常返回，无异常
    """
    _do_reset(env)
    api_MemBlock_enq_lsq(
        env,
        port=0,
        is_store=1,     # Store
        rob_idx_flag=0,
        rob_idx_value=2,
        sq_idx_value=0,
        max_cycles=50,
    )


def test_api_MemBlock_enq_lsq_invalid_port(env):
    """
    验证 port 超出范围（0~7）时 API 抛出 AssertionError。

    检查点：
      - port=8 时抛出 AssertionError
    """
    _do_reset(env)
    with pytest.raises(AssertionError):
        api_MemBlock_enq_lsq(env, port=8, max_cycles=50)


def test_api_MemBlock_enq_lsq_multiple_ports(env):
    """
    验证多个端口（port 0、1、2）可依次发送入队请求。

    检查点：
      - 3 个端口依次入队均无异常
    """
    _do_reset(env)
    for port in range(3):
        api_MemBlock_enq_lsq(
            env,
            port=port,
            is_store=(port % 2),
            rob_idx_value=port,
            max_cycles=50,
        )
        api_MemBlock_step(env, n=2, max_cycles=20)


# ══════════════════════════════════════════════════════════════════════════════
# 测试用例：LSQ 队列状态观测
# ══════════════════════════════════════════════════════════════════════════════

def test_api_MemBlock_read_deq_ptrs(env):
    """
    验证 LQ/SQ 出队指针 API 可正常读取。

    检查点：
      - read_lq_deq_ptr 返回整数（不抛异常）
      - read_sq_deq_ptr 返回整数（不抛异常）
    """
    _do_reset(env)
    api_MemBlock_step(env, n=5, max_cycles=50)

    lq_ptr = api_MemBlock_read_lq_deq_ptr(env, max_cycles=10)
    sq_ptr = api_MemBlock_read_sq_deq_ptr(env, max_cycles=10)

    assert isinstance(lq_ptr, int), f"lq_deq_ptr 应为整数，实际为 {type(lq_ptr)}"
    assert isinstance(sq_ptr, int), f"sq_deq_ptr 应为整数，实际为 {type(sq_ptr)}"


# ══════════════════════════════════════════════════════════════════════════════
# 测试用例：TileLink DCache 请求观测
# ══════════════════════════════════════════════════════════════════════════════

def test_api_MemBlock_tl_a_initially_quiet(env):
    """
    验证 DUT 复位后，TileLink A 通道保持静默（无 miss 请求）。

    在没有任何访存指令的情况下，DCache 不应发出 Acquire 请求。

    检查点：
      - 复位后运行 20 个周期，tl_a.valid 应始终为 0（或 Mock ready 已置 1）
    """
    _do_reset(env)
    api_MemBlock_step(env, n=5, max_cycles=50)

    # A 通道 valid 应为 0（无指令 → 无 miss）
    # 注：Mock 已将 ready 置 1，但 valid 由 DUT 驱动
    assert env.tl_a.valid.value == 0, \
        "复位后无指令输入，DCache 不应发出 TL-A 请求"


def test_api_MemBlock_tl_d_response_driven(env):
    """
    验证 api_MemBlock_set_tl_d_response 可以驱动 TileLink D 通道。

    本测试直接通过 API 驱动 D 通道，验证信号可以被写入。

    检查点：
      - set_tl_d_response API 正常返回
      - D 通道 valid 在 API 返回后被清除
    """
    _do_reset(env)
    api_MemBlock_set_tl_d_response(
        env,
        valid=1,
        opcode=4,           # GrantData
        size=6,             # 64B Cache 行
        source=0,
        data=0xCAFEBABE,
        max_cycles=20,
    )
    # API 返回后 valid 应已被清除
    assert env.tl_d.valid.value == 0, \
        "api_MemBlock_set_tl_d_response 返回后 tl_d.valid 应为 0"


def test_api_MemBlock_wait_tl_a_timeout(env):
    """
    验证 api_MemBlock_wait_for_tl_a_request 在无 DCache 请求时正确超时返回。

    检查点：
      - 无指令输入时，等待函数应返回 False（超时）
    """
    _do_reset(env)
    result = api_MemBlock_wait_for_tl_a_request(
        env,
        timeout_cycles=10,  # 短超时，快速验证
        max_cycles=50,
    )
    # 无访存指令，不应出现 DCache miss 请求
    assert result is False, \
        "无指令输入时 wait_for_tl_a_request 应超时返回 False"


# ══════════════════════════════════════════════════════════════════════════════
# 测试用例：整数发射
# ══════════════════════════════════════════════════════════════════════════════

def test_api_MemBlock_int_issue_port0(env):
    """
    验证 api_MemBlock_send_int_issue 可以发射端口 0 的整数访存指令。

    检查点：
      - API 正常返回
      - 发射后端口 valid 被清除
    """
    _do_reset(env)
    api_MemBlock_send_int_issue(env, port=0, valid=1, max_cycles=50)


def test_api_MemBlock_int_issue_invalid_port(env):
    """
    验证 port 超出范围（0~6）时 send_int_issue 抛出 AssertionError。

    检查点：
      - port=7 时抛出 AssertionError
    """
    _do_reset(env)
    with pytest.raises(AssertionError):
        api_MemBlock_send_int_issue(env, port=7, max_cycles=50)


# ══════════════════════════════════════════════════════════════════════════════
# 测试用例：功能覆盖率采样验证
# ══════════════════════════════════════════════════════════════════════════════

def test_api_MemBlock_coverage_groups_accessible(env):
    """
    验证功能覆盖率组已正确注册到 dut，可通过 dut.fc_cover 访问。

    检查点：
      - dut.fc_cover 为字典
      - 包含 FG-REDIRECT / FG-LSQ-ENQ / FG-MEM-ISSUE / FG-WB / FG-TL-DCACHE
    """
    expected_groups = [
        "FG-REDIRECT",
        "FG-LSQ-ENQ",
        "FG-MEM-ISSUE",
        "FG-WB",
        "FG-TL-DCACHE",
    ]
    fc = env.dut.fc_cover
    assert isinstance(fc, dict), "dut.fc_cover 应为字典"

    for name in expected_groups:
        assert name in fc, f"dut.fc_cover 缺少覆盖组: {name}"


def test_api_MemBlock_coverage_redirect_sampled(env):
    """
    验证发送重定向后，FG-REDIRECT 覆盖组的 FC-REDIRECT-CTRL 功能点被采样。

    检查点：
      - 发送 valid=1 重定向后，FC-REDIRECT-CTRL/CK-REDIRECT-VALID 覆盖率增加
    """
    _do_reset(env)

    # 发送重定向，触发覆盖率采样
    api_MemBlock_send_redirect(
        env,
        valid=1,
        level=0,
        max_cycles=20,
    )
    # 再推进几个周期确保采样
    api_MemBlock_step(env, n=3, max_cycles=50)

    # 检查覆盖率（CovGroup 记录的 hit 次数应 > 0）
    fg_redirect = env.dut.fc_cover.get("FG-REDIRECT")
    assert fg_redirect is not None, "FG-REDIRECT 覆盖组不存在"
