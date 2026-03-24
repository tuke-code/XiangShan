# coding=utf-8
"""
MemBlock 测试环境（Test Environment）

本文件定义：
  1. toffee.Bundle 子类   ：按功能组封装 DUT 端口（引脚抽象层）
  2. MockL2Cache          ：模拟 L2 Cache 对 DCache TileLink 请求的响应
  3. MemBlockEnv          ：顶层测试环境类，聚合所有 Bundle 和 Mock 组件
  4. env (pytest fixture) ：向测试用例提供 MemBlockEnv 实例

Bundle 说明：
  - 使用 from_prefix() 方式将 DUT 端口前缀映射到 Bundle 信号属性
  - Bundle 内的信号名 = DUT 端口名去掉前缀后的部分
  - toffee.Signal / toffee.Signals 无需指定位宽，bind 时自动获取

Mock 说明：
  MockL2Cache 通过 dut.StepRis 注册上升沿回调，
  监听 DCache 通过 TileLink A 通道发出的 AcquireBlock 请求，
  在固定延迟（默认 8 周期）后返回 GrantData 响应（D 通道）。
"""

import os
import sys
import pytest
from collections import deque

# ──────────────────────────────────────────────────────────────────────────────
# 路径配置（幂等）
# ──────────────────────────────────────────────────────────────────────────────
_HERE      = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.abspath(os.path.join(_HERE, "..", "..", "..", ".."))
_PYLIB     = os.path.join(_REPO_ROOT, "build-memblock", "pylib")
for _p in [_PYLIB, _HERE]:
    if _p not in sys.path:
        sys.path.insert(0, _p)

from toffee import Bundle, Signal, Signals


# ══════════════════════════════════════════════════════════════════════════════
# Bundle 定义：重定向接口
# ══════════════════════════════════════════════════════════════════════════════
class RedirectBundle(Bundle):
    """
    流水线重定向接口（io_redirect_*）。

    DUT 端口前缀：``io_redirect_``

    Signals:
        valid             : 重定向有效标志（输入）
        bits_level        : 刷新级别（0=分支预测，1=异常）
        bits_robIdx_flag  : ROB 索引高位 flag
        bits_robIdx_value : ROB 索引 value（9bit）
    """
    valid = Signal()
    bits_level, bits_robIdx_flag, bits_robIdx_value = Signals(3)


# ══════════════════════════════════════════════════════════════════════════════
# Bundle 定义：LSQ 入队接口（单端口模板）
# ══════════════════════════════════════════════════════════════════════════════
class LsqEnqPortBundle(Bundle):
    """
    LSQ 单端口入队请求/响应接口模板（io_ooo_to_mem_enqLsq_req_N_*）。

    由于 MemBlock 有 8 路入队端口，需要用 from_prefix 分别创建。

    Signals（请求侧）：
        req_valid              : 入队请求有效
        req_bits_isStore       : 1=Store，0=Load
        req_bits_isAMO         : 1=原子操作
        req_bits_robIdx_flag   : ROB 索引 flag
        req_bits_robIdx_value  : ROB 索引 value
        req_bits_sqIdx_flag    : SQ 索引 flag
        req_bits_sqIdx_value   : SQ 索引 value
        req_bits_lqIdx         : LQ 索引

    Signals（响应侧）：
        resp_sqIdx_flag  : 分配的 SQ 条目 flag
        resp_sqIdx_value : 分配的 SQ 条目 value
        resp_lqIdx       : 分配的 LQ 条目索引
    """
    # 请求侧
    (req_valid, req_bits_isStore, req_bits_isAMO,
     req_bits_robIdx_flag, req_bits_robIdx_value,
     req_bits_sqIdx_flag, req_bits_sqIdx_value,
     req_bits_lqIdx) = Signals(8)

    # 响应侧
    resp_sqIdx_flag, resp_sqIdx_value, resp_lqIdx = Signals(3)


# ══════════════════════════════════════════════════════════════════════════════
# Bundle 定义：TileLink A 通道（DCache → L2 请求）
# ══════════════════════════════════════════════════════════════════════════════
class TileLinkABundle(Bundle):
    """
    TileLink A 通道接口（auto_inner_buffers_out_a_*）。

    A 通道用于 DCache 向 L2 发送 AcquireBlock/AcquirePerm 请求。

    Signals:
        ready              : L2 ready（输入，由 Mock 驱动）
        valid              : DCache 请求有效（输出）
        bits_opcode        : TL 操作码（4=AcquireBlock, 5=AcquirePerm）
        bits_param         : 权限参数
        bits_size          : log2(传输大小)
        bits_source        : 事务源 ID
        bits_address       : 物理地址（48bit）
        bits_mask          : 字节掩码
        bits_data          : 写数据（64bit）
        bits_corrupt       : 数据损坏标志
    """
    ready = Signal()
    valid = Signal()
    bits_opcode, bits_param, bits_size, bits_source = Signals(4)
    bits_address = Signal()
    bits_mask, bits_data, bits_corrupt = Signals(3)


# ══════════════════════════════════════════════════════════════════════════════
# Bundle 定义：TileLink D 通道（L2 → DCache 响应）
# ══════════════════════════════════════════════════════════════════════════════
class TileLinkDBundle(Bundle):
    """
    TileLink D 通道接口（auto_inner_buffers_out_d_*）。

    D 通道用于 L2 向 DCache 返回 Grant/GrantData 响应。

    Signals:
        ready          : DCache ready（输出）
        valid          : L2 响应有效（输入，由 Mock 驱动）
        bits_opcode    : TL 操作码（4=GrantData, 5=Grant, 6=ReleaseAck）
        bits_param     : 参数（2bit）
        bits_size      : log2(传输大小)（3bit）
        bits_source    : 事务源 ID（4bit）
        bits_sink      : sink ID（用于 GrantAck）
        bits_denied    : 请求被拒绝标志
        bits_data      : 响应数据（64bit）
        bits_corrupt   : 数据损坏标志
    """
    ready = Signal()
    valid = Signal()
    bits_opcode, bits_param, bits_size = Signals(3)
    bits_source, bits_sink = Signals(2)
    bits_denied, bits_data, bits_corrupt = Signals(3)


# ══════════════════════════════════════════════════════════════════════════════
# Bundle 定义：内存状态信息接口
# ══════════════════════════════════════════════════════════════════════════════
class MemInfoBundle(Bundle):
    """
    内存子系统状态信息接口（io_mem_to_ooo_*Full 等）。

    Signals:
        sqFull       : Store Queue 满
        lqFull       : Load Queue 满
        dcacheMSHRFull : DCache MSHR 满
    """
    sqFull, lqFull = Signals(2)


# ══════════════════════════════════════════════════════════════════════════════
# Mock 组件：L2 Cache 模拟器
# ══════════════════════════════════════════════════════════════════════════════
class MockL2Cache:
    """
    L2 Cache 简化模拟器。

    行为描述：
      - 监听 TileLink A 通道（DCache 的 miss 请求）
      - 检测到 valid=1 且 ready=1（握手成功）的 AcquireBlock 请求时，
        记录事务（source、size）并排入延迟队列
      - 经过固定延迟（delay_cycles）个时钟周期后，在 D 通道发出 GrantData 响应
      - D 通道每次只响应一个事务，多个请求排队处理

    注意：
      - ready 信号由本 Mock 驱动（始终为 1，不模拟反压）
      - 本 Mock 不包含真实的 Cache 行数据，data 字段固定返回 0
    """

    def __init__(self, tl_a: TileLinkABundle, tl_d: TileLinkDBundle,
                 delay_cycles: int = 8):
        """
        初始化 MockL2Cache。

        Args:
            tl_a         : TileLink A 通道 Bundle（DCache→L2 请求）。
            tl_d         : TileLink D 通道 Bundle（L2→DCache 响应）。
            delay_cycles : 从收到请求到发出响应的模拟延迟，默认 8 周期。
        """
        self.tl_a         = tl_a
        self.tl_d         = tl_d
        self.delay_cycles = delay_cycles

        # 待处理的响应队列：每个元素为 (release_cycle, source, size)
        self._pending: deque = deque()
        self._current_cycle: int = 0

        # 统计计数
        self.req_count  = 0   # 收到的请求总数
        self.resp_count = 0   # 已发出的响应总数

    def on_clock_edge(self, cycle: int) -> None:
        """
        时钟上升沿回调（由 dut.StepRis 触发）。

        本方法在每个时钟周期执行以下操作：
          1. 更新内部时钟计数
          2. 将 A 通道 ready 始终置 1（不模拟反压）
          3. 检测 A 通道握手，若成功则将事务加入延迟队列
          4. 检查延迟队列，若有到期响应则驱动 D 通道

        Args:
            cycle: 当前时钟周期号（由 DUT 框架传入）。
        """
        self._current_cycle = cycle

        # 步骤1：A 通道 ready 始终置 1（简化模型，不反压）
        self.tl_a.ready.value = 1

        # 步骤2：检测 A 通道握手
        a_valid = self.tl_a.valid.value
        if a_valid == 1:
            # 握手成功：记录请求
            source = self.tl_a.bits_source.value
            size   = self.tl_a.bits_size.value
            opcode = self.tl_a.bits_opcode.value

            self.req_count += 1
            release_at = cycle + self.delay_cycles
            self._pending.append((release_at, source, size, opcode))

        # 步骤3：检查是否有到期的响应
        if self._pending:
            release_at, source, size, opcode = self._pending[0]
            if cycle >= release_at:
                self._pending.popleft()
                self._send_response(source, size, opcode)
                self.resp_count += 1
        else:
            # 无待响应事务，拉低 D 通道 valid
            self.tl_d.valid.value = 0

    def _send_response(self, source: int, size: int, opcode: int) -> None:
        """
        驱动 TileLink D 通道发出响应（单周期有效）。

        Args:
            source : 事务源 ID（与请求 A 通道的 source 对应）。
            size   : 传输大小的 log2 值。
            opcode : 请求操作码（4=AcquireBlock → 响应 GrantData）。
        """
        # AcquireBlock(4) → GrantData(4)；AcquirePerm(5) → Grant(5)
        resp_opcode = 4 if opcode == 4 else 5

        self.tl_d.valid.value          = 1
        self.tl_d.bits_opcode.value    = resp_opcode
        self.tl_d.bits_param.value     = 0   # toT（转为独占）
        self.tl_d.bits_size.value      = size
        self.tl_d.bits_source.value    = source
        self.tl_d.bits_sink.value      = 0
        self.tl_d.bits_denied.value    = 0
        self.tl_d.bits_data.value      = 0   # 简化：数据为 0
        self.tl_d.bits_corrupt.value   = 0

    def bind(self, dut) -> None:
        """
        将 Mock 组件绑定到 DUT，注册上升沿回调。

        Args:
            dut: DUTMemBlock 实例。
        """
        dut.StepRis(self.on_clock_edge)

    @property
    def stats(self) -> dict:
        """返回 Mock 统计信息字典。"""
        return {
            "requests_received": self.req_count,
            "responses_sent":    self.resp_count,
            "pending_count":     len(self._pending),
        }


# ══════════════════════════════════════════════════════════════════════════════
# 顶层测试环境类
# ══════════════════════════════════════════════════════════════════════════════
class MemBlockEnv:
    """
    MemBlock 顶层测试环境。

    聚合了所有 Bundle 引脚封装和 Mock 组件，提供高层次的
    操作接口供测试用例使用。

    属性：
        dut       : DUTMemBlock 实例
        redirect  : 重定向接口 Bundle（输入侧）
        enq_lsq   : LSQ 0 号入队端口 Bundle（输入侧）
        tl_a      : TileLink A 通道 Bundle（DCache→L2 请求，输出侧）
        tl_d      : TileLink D 通道 Bundle（L2→DCache 响应，输入侧）
        mem_info  : 内存状态信息 Bundle（输出侧）
        mock_l2   : L2 Cache 模拟器

    使用示例：
        def test_basic(env):
            env.reset()
            env.Step(100)
            assert env.tl_a.valid.value == 0  # 无 miss 请求
    """

    def __init__(self, dut) -> None:
        """
        初始化测试环境，绑定所有 Bundle 和 Mock 组件。

        Args:
            dut: DUTMemBlock 实例（来自 dut fixture）。
        """
        self.dut = dut

        # ── Bundle：重定向接口 ──────────────────────────────────────────────
        self.redirect = RedirectBundle.from_prefix("io_redirect_")
        self.redirect.bind(dut)
        self.redirect.set_all(0)

        # ── Bundle：LSQ 0 号入队端口 ────────────────────────────────────────
        self.enq_lsq = LsqEnqPortBundle.from_prefix("io_ooo_to_mem_enqLsq_")
        self.enq_lsq.bind(dut)
        self.enq_lsq.set_all(0)

        # ── Bundle：TileLink A 通道（DCache→L2）────────────────────────────
        self.tl_a = TileLinkABundle.from_prefix("auto_inner_buffers_out_a_")
        self.tl_a.bind(dut)

        # ── Bundle：TileLink D 通道（L2→DCache）────────────────────────────
        self.tl_d = TileLinkDBundle.from_prefix("auto_inner_buffers_out_d_")
        self.tl_d.bind(dut)
        self.tl_d.set_all(0)

        # ── Bundle：内存状态信息 ────────────────────────────────────────────
        self.mem_info = MemInfoBundle.from_dict({
            "sqFull": "io_mem_to_ooo_sqFull",
            "lqFull": "io_mem_to_ooo_lqFull",
        })
        self.mem_info.bind(dut)

        # ── Mock 组件：L2 Cache ─────────────────────────────────────────────
        self.mock_l2 = MockL2Cache(self.tl_a, self.tl_d, delay_cycles=8)
        self.mock_l2.bind(dut)

    # ── 通用操作方法 ──────────────────────────────────────────────────────────

    def reset(self, cycles: int = 10) -> None:
        """
        执行同步复位：将 reset 拉高 cycles 周期，再拉低稳定。

        Args:
            cycles: reset 保持高电平的周期数，默认为 10。
        """
        self.dut.reset.value = 1
        self.dut.Step(cycles)
        self.dut.reset.value = 0
        self.dut.Step(5)

    def Step(self, n: int = 1) -> None:
        """
        推进 DUT n 个时钟周期。

        功能覆盖率采样和 Mock 驱动均在此过程中自动触发。

        Args:
            n: 推进的时钟周期数，默认为 1。
        """
        self.dut.Step(n)

    def Finish(self) -> None:
        """
        清理 DUT 资源（通常由 dut fixture teardown 调用）。
        """
        self.dut.Finish()

    def clear_callbacks(self) -> None:
        """
        清除本 Env 向 DUT 注册的所有 StepRis 回调（Mock 组件）。

        在需要动态切换 Mock 配置时使用。
        """
        self.dut.xclock.RemoveStepRisCbByDesc(
            self.mock_l2.on_clock_edge.__name__
        )


# ══════════════════════════════════════════════════════════════════════════════
# env Fixture
# ══════════════════════════════════════════════════════════════════════════════
@pytest.fixture(scope="function")
def env(dut):
    """
    MemBlock 测试环境 pytest fixture。

    依赖 dut fixture（在 MemBlock_api.py 中定义）。
    为每个测试用例创建全新的 MemBlockEnv 实例。

    Scope: function（每个测试用例独立创建）

    Args:
        dut: DUTMemBlock 实例（来自 dut fixture）。

    Returns:
        MemBlockEnv: 已完成初始化的测试环境实例。
    """
    return MemBlockEnv(dut)
