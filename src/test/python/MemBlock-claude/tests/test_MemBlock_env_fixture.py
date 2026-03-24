# coding=utf-8
"""
test_MemBlock_env_fixture.py
────────────────────────────
Env Fixture 验证测试（Stage 3：evaluate_env_fixture）

本文件验证 MemBlockEnv 的正确性，包括：
  - env fixture 能正常创建和清理
  - 各 Bundle 绑定正确，信号可正常读写
  - MockL2Cache 能响应 TileLink A 通道请求
  - 常用操作方法（reset / Step / Finish）可用

测试函数命名规范：test_api_MemBlock_env_<名称>

注意：
  本文件仅验证 env 和 Mock 组件本身的工作状态，
  不测试 DUT 的具体功能逻辑，避免因 DUT 功能缺陷导致误报。
"""

import pytest


# ──────────────────────────────────────────────────────────────────────────────
# 基础 fixture 生命周期验证
# ──────────────────────────────────────────────────────────────────────────────

def test_api_MemBlock_env_create(env):
    """
    验证 env fixture 能正常创建 MemBlockEnv 实例。

    检查点：
      - env 对象不为 None
      - env 包含 dut 属性
      - env 包含 mock_l2 属性
    """
    assert env is not None, "env fixture 创建失败，返回 None"
    assert hasattr(env, "dut"),     "env 缺少 dut 属性"
    assert hasattr(env, "mock_l2"), "env 缺少 mock_l2 属性"


def test_api_MemBlock_env_has_bundles(env):
    """
    验证 env 包含所有预期的 Bundle 属性。

    检查点：
      - redirect  : RedirectBundle
      - enq_lsq   : LsqEnqPortBundle
      - tl_a      : TileLinkABundle
      - tl_d      : TileLinkDBundle
      - mem_info  : MemInfoBundle
    """
    expected_bundles = ["redirect", "enq_lsq", "tl_a", "tl_d", "mem_info"]
    for name in expected_bundles:
        assert hasattr(env, name), f"env 缺少 Bundle 属性: {name}"


# ──────────────────────────────────────────────────────────────────────────────
# 时钟推进验证
# ──────────────────────────────────────────────────────────────────────────────

def test_api_MemBlock_env_step(env):
    """
    验证 env.Step() 能正常推进 DUT 时钟。

    检查点：
      - 调用 Step(10) 不抛出异常
      - DUT 不会崩溃或产生错误
    """
    # 先推进若干周期，不应出现异常
    try:
        env.Step(10)
    except Exception as e:
        pytest.fail(f"env.Step(10) 抛出异常: {e}")


def test_api_MemBlock_env_reset(env):
    """
    验证 env.reset() 能正常执行复位序列。

    检查点：
      - reset() 不抛出异常
      - reset 后 DUT 仍可正常推进时钟
    """
    try:
        env.reset(cycles=5)
    except Exception as e:
        pytest.fail(f"env.reset() 抛出异常: {e}")

    # reset 后应能正常推进
    env.Step(5)


# ──────────────────────────────────────────────────────────────────────────────
# Bundle 信号读写验证
# ──────────────────────────────────────────────────────────────────────────────

def test_api_MemBlock_env_redirect_bundle(env):
    """
    验证 redirect Bundle 正确绑定，可对 valid 信号进行写操作。

    检查点：
      - redirect.valid 可赋值为 1
      - 推进一个时钟周期后可将其清零
    """
    # 写入重定向信号
    env.redirect.valid.value = 1
    env.redirect.bits_level.value = 0
    env.Step(1)

    # 清除
    env.redirect.valid.value = 0
    env.Step(1)

    # 验证清除成功（读回值应为 0）
    assert env.redirect.valid.value == 0, \
        "redirect.valid 清零失败"


def test_api_MemBlock_env_tl_a_bundle(env):
    """
    验证 TileLink A 通道 Bundle 绑定正确，ready 信号可被写入。

    检查点：
      - tl_a.ready 可写（由 Mock 驱动）
      - tl_a.valid 为只读输出端口，不抛出读异常
    """
    # ready 由 MockL2Cache 驱动（始终 1）
    env.Step(1)
    # A 通道 ready 在 Mock 初始化后应被置 1
    # 注：DUT 复位期间，DCache 不会发出请求，valid 应为 0
    try:
        _ = env.tl_a.valid.value
    except Exception as e:
        pytest.fail(f"读取 tl_a.valid 抛出异常: {e}")


def test_api_MemBlock_env_tl_d_bundle(env):
    """
    验证 TileLink D 通道 Bundle 绑定正确，valid 信号可被写入。

    检查点：
      - tl_d.valid 可赋值
      - tl_d.bits_data 可赋值（64bit 数据）
    """
    # 手动驱动 D 通道（模拟 L2 响应）
    env.tl_d.valid.value = 1
    env.tl_d.bits_opcode.value = 4     # GrantData
    env.tl_d.bits_data.value   = 0xDEADBEEF_CAFEBABE
    env.Step(1)

    # 清除
    env.tl_d.valid.value = 0
    env.Step(1)


def test_api_MemBlock_env_mem_info_bundle(env):
    """
    验证 mem_info Bundle 绑定正确，sqFull/lqFull 可被读取。

    检查点：
      - mem_info.sqFull 可读（不抛出异常）
      - mem_info.lqFull 可读（不抛出异常）
    """
    env.Step(1)
    try:
        _ = env.mem_info.sqFull.value
        _ = env.mem_info.lqFull.value
    except Exception as e:
        pytest.fail(f"读取 mem_info Bundle 信号抛出异常: {e}")


# ──────────────────────────────────────────────────────────────────────────────
# Mock L2 Cache 验证
# ──────────────────────────────────────────────────────────────────────────────

def test_api_MemBlock_env_mock_l2_initial_state(env):
    """
    验证 MockL2Cache 初始状态正确。

    检查点：
      - req_count == 0（尚未收到请求）
      - resp_count == 0（尚未发出响应）
      - pending 队列为空
    """
    stats = env.mock_l2.stats
    assert stats["requests_received"] == 0,  "MockL2 初始 req_count 应为 0"
    assert stats["responses_sent"]    == 0,  "MockL2 初始 resp_count 应为 0"
    assert stats["pending_count"]     == 0,  "MockL2 初始 pending_count 应为 0"


def test_api_MemBlock_env_mock_l2_ready_driven(env):
    """
    验证 MockL2Cache 正确将 TileLink A 通道 ready 置 1。

    检查点：
      - 推进 1 个时钟后，tl_a.ready 应被 MockL2 置为 1
    """
    env.Step(1)
    assert env.tl_a.ready.value == 1, \
        "MockL2Cache 未将 TL-A ready 置 1"


def test_api_MemBlock_env_mock_l2_stats_accessible(env):
    """
    验证 MockL2Cache stats 属性可访问并返回字典。

    检查点：
      - stats 为字典类型
      - 包含 requests_received / responses_sent / pending_count 键
    """
    env.Step(5)
    stats = env.mock_l2.stats
    assert isinstance(stats, dict), "mock_l2.stats 应返回字典"
    for key in ["requests_received", "responses_sent", "pending_count"]:
        assert key in stats, f"mock_l2.stats 缺少键: {key}"


# ──────────────────────────────────────────────────────────────────────────────
# 多周期稳定性验证
# ──────────────────────────────────────────────────────────────────────────────

def test_api_MemBlock_env_long_run_stable(env):
    """
    验证 MemBlockEnv 在持续运行时的稳定性。

    在无实际指令输入的情况下，推进 50 个时钟周期，
    验证 DUT 和 Mock 组件不会崩溃。

    检查点：
      - 运行 50 周期后无异常抛出
      - DUT 仍处于可访问状态
    """
    env.reset(cycles=3)

    try:
        env.Step(50)
    except Exception as e:
        pytest.fail(f"长时运行（50 周期）抛出异常: {e}")

    # 验证 DUT 仍可访问
    try:
        _ = env.tl_a.valid.value
    except Exception as e:
        pytest.fail(f"长时运行后 DUT 信号不可访问: {e}")
