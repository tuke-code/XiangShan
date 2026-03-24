# coding=utf-8
"""
MemBlock 基础测试环境。

本文件实现：
  1. 基于 `toffee.Bundle` 的核心端口封装
  2. 面向 LSU 核心闭环的外部依赖 Mock
  3. `MemBlockEnv` 顶层测试环境
  4. `env` fixture
"""

import os
import sys
from collections import deque

import pytest


_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.abspath(os.path.join(_HERE, "..", "..", "..", ".."))
_PYLIB = os.path.join(_REPO_ROOT, "build-memblock", "pylib")

for _path in (_PYLIB, _HERE):
    if _path not in sys.path:
        sys.path.insert(0, _path)


from toffee import Bundle, BundleList, Signal, SignalList, Signals


LOAD_PIPELINE_WIDTH = 3
LSQ_ENQ_PORTS = 8
INT_ISSUE_PORTS = 7
INT_WRITEBACK_PORTS = 7


class RedirectBundle(Bundle):
    """流水线重定向输入接口。"""

    valid = Signal()
    bits_level, bits_robIdx_flag, bits_robIdx_value = Signals(3)

    def drive_idle(self) -> None:
        self.valid.value = 0
        self.bits_level.value = 0
        self.bits_robIdx_flag.value = 0
        self.bits_robIdx_value.value = 0


class LsqEnqMetaBundle(Bundle):
    """LSQ 分配元信息接口。"""

    canAccept = Signal()
    need_alloc = SignalList("needAlloc_#", LSQ_ENQ_PORTS)

    def drive_idle(self) -> None:
        for signal in self.need_alloc:
            signal.value = 0


class LsqEnqReqBundle(Bundle):
    """单路 LSQ 入队请求接口。"""

    valid = Signal()
    bits_fuType = Signal()
    bits_fuOpType = Signal()
    bits_rfWen = Signal()
    bits_vpu_vstart = Signal()
    bits_vpu_vl = Signal()
    bits_lastUop = Signal()
    bits_pdest = Signal()
    bits_robIdx_flag = Signal()
    bits_robIdx_value = Signal()
    bits_lqIdx_flag = Signal()
    bits_lqIdx_value = Signal()
    bits_sqIdx_flag = Signal()
    bits_sqIdx_value = Signal()
    bits_numLsElem = Signal()
    bits_exception_vec = SignalList("bits_exceptionVec_#", 24)

    def drive_idle(self) -> None:
        self.valid.value = 0
        self.bits_fuType.value = 0
        self.bits_fuOpType.value = 0
        self.bits_rfWen.value = 0
        self.bits_vpu_vstart.value = 0
        self.bits_vpu_vl.value = 0
        self.bits_lastUop.value = 0
        self.bits_pdest.value = 0
        self.bits_robIdx_flag.value = 0
        self.bits_robIdx_value.value = 0
        self.bits_lqIdx_flag.value = 0
        self.bits_lqIdx_value.value = 0
        self.bits_sqIdx_flag.value = 0
        self.bits_sqIdx_value.value = 0
        self.bits_numLsElem.value = 0
        for signal in self.bits_exception_vec:
            signal.value = 0


class LsqEnqRespBundle(Bundle):
    """单路 LSQ 入队响应接口。"""

    lqIdx_flag, lqIdx_value, sqIdx_flag, sqIdx_value = Signals(4)


class IntIssueBundle(Bundle):
    """整数 issue 单 lane 接口。"""

    ready = Signal()
    valid = Signal()
    bits_fuType = Signal()
    bits_fuOpType = Signal()
    bits_src_0 = Signal()
    bits_robIdx_flag = Signal()
    bits_robIdx_value = Signal()
    bits_sqIdx_flag = Signal()
    bits_sqIdx_value = Signal()

    def drive_idle(self) -> None:
        self.valid.value = 0
        self.bits_fuType.value = 0
        self.bits_fuOpType.value = 0
        self.bits_src_0.value = 0
        self.bits_robIdx_flag.value = 0
        self.bits_robIdx_value.value = 0
        self.bits_sqIdx_flag.value = 0
        self.bits_sqIdx_value.value = 0


class IntWritebackBundle(Bundle):
    """整数 writeback 单 lane 接口。"""

    ready = Signal()
    valid = Signal()
    bits_robIdx_flag = Signal()
    bits_robIdx_value = Signal()


class MemStatusBundle(Bundle):
    """`mem_to_ooo` 中 env 常用的状态输出。"""

    lqDeq = Signal()
    sqDeq = Signal()
    lqDeqPtr_flag = Signal()
    lqDeqPtr_value = Signal()
    sqDeqPtr_flag = Signal()
    sqDeqPtr_value = Signal()
    sbIsEmpty = Signal()
    memoryViolation_valid = Signal()
    memoryViolation_bits_level = Signal()
    memoryViolation_bits_robIdx_flag = Signal()
    memoryViolation_bits_robIdx_value = Signal()
    memoryViolation_bits_target = Signal()


class LsqStatusBundle(Bundle):
    """LSQ 输出状态。"""

    vaddr = Signal()
    gpaddr = Signal()
    isForVSnonLeafPTE = Signal()
    lqCanAccept = Signal()
    sqCanAccept = Signal()
    mmio = SignalList("mmio_#", LOAD_PIPELINE_WIDTH)


class OuterTLABundle(Bundle):
    """MemBlock 对外的 TL-A 请求接口。"""

    ready = Signal()
    valid = Signal()
    bits_opcode = Signal()
    bits_param = Signal()
    bits_size = Signal()
    bits_source = Signal()
    bits_address = Signal()
    bits_mask = Signal()
    bits_data = Signal()
    bits_corrupt = Signal()
    bits_user_memBackType_MM = Signal()
    bits_user_memPageType_NC = Signal()


class OuterTLDBundle(Bundle):
    """MemBlock 对外的 TL-D 响应接口。"""

    ready = Signal()
    valid = Signal()
    bits_opcode = Signal()
    bits_param = Signal()
    bits_size = Signal()
    bits_source = Signal()
    bits_sink = Signal()
    bits_denied = Signal()
    bits_data = Signal()
    bits_corrupt = Signal()

    def drive_idle(self) -> None:
        self.valid.value = 0
        self.bits_opcode.value = 0
        self.bits_param.value = 0
        self.bits_size.value = 0
        self.bits_source.value = 0
        self.bits_sink.value = 0
        self.bits_denied.value = 0
        self.bits_data.value = 0
        self.bits_corrupt.value = 0


class DcacheClientABundle(Bundle):
    """DCache TL-C A 通道。"""

    ready = Signal()
    valid = Signal()
    bits_opcode = Signal()
    bits_param = Signal()
    bits_size = Signal()
    bits_source = Signal()
    bits_address = Signal()
    bits_mask = Signal()
    bits_data = Signal()
    bits_corrupt = Signal()
    bits_user_alias = Signal()
    bits_user_memPageType_NC = Signal()
    bits_user_memBackType_MM = Signal()
    bits_user_vaddr = Signal()
    bits_user_reqSource = Signal()
    bits_user_needHint = Signal()
    bits_echo_isKeyword = Signal()


class DcacheClientBBundle(Bundle):
    """DCache TL-C B 通道。"""

    ready = Signal()
    valid = Signal()
    bits_opcode = Signal()
    bits_param = Signal()
    bits_size = Signal()
    bits_source = Signal()
    bits_address = Signal()
    bits_mask = Signal()
    bits_data = Signal()
    bits_corrupt = Signal()

    def drive_idle(self) -> None:
        self.valid.value = 0
        self.bits_opcode.value = 0
        self.bits_param.value = 0
        self.bits_size.value = 0
        self.bits_source.value = 0
        self.bits_address.value = 0
        self.bits_mask.value = 0
        self.bits_data.value = 0
        self.bits_corrupt.value = 0


class DcacheClientCBundle(Bundle):
    """DCache TL-C C 通道。"""

    ready = Signal()
    valid = Signal()
    bits_opcode = Signal()
    bits_param = Signal()
    bits_size = Signal()
    bits_source = Signal()
    bits_address = Signal()
    bits_data = Signal()
    bits_corrupt = Signal()
    bits_user_alias = Signal()
    bits_user_memPageType_NC = Signal()
    bits_user_memBackType_MM = Signal()
    bits_user_vaddr = Signal()
    bits_user_reqSource = Signal()
    bits_user_needHint = Signal()
    bits_echo_isKeyword = Signal()


class DcacheClientDBundle(Bundle):
    """DCache TL-C D 通道。"""

    ready = Signal()
    valid = Signal()
    bits_opcode = Signal()
    bits_param = Signal()
    bits_size = Signal()
    bits_source = Signal()
    bits_sink = Signal()
    bits_denied = Signal()
    bits_echo_isKeyword = Signal()
    bits_data = Signal()
    bits_corrupt = Signal()

    def drive_idle(self) -> None:
        self.valid.value = 0
        self.bits_opcode.value = 0
        self.bits_param.value = 0
        self.bits_size.value = 0
        self.bits_source.value = 0
        self.bits_sink.value = 0
        self.bits_denied.value = 0
        self.bits_echo_isKeyword.value = 0
        self.bits_data.value = 0
        self.bits_corrupt.value = 0


class DcacheClientEBundle(Bundle):
    """DCache TL-C E 通道。"""

    ready = Signal()
    valid = Signal()
    bits_sink = Signal()


class MockOuterBufferMemory:
    """外部内存缓冲区简化模型。"""

    def __init__(self, tl_a: OuterTLABundle, tl_d: OuterTLDBundle, default_delay: int = 8):
        self.tl_a = tl_a
        self.tl_d = tl_d
        self.default_delay = default_delay
        self._pending = deque()
        self._active = None
        self._cycle = 0
        self.request_count = 0
        self.response_count = 0

    def reset(self) -> None:
        self._pending.clear()
        self._active = None
        self.tl_d.drive_idle()

    def enqueue_response(
        self,
        opcode: int = 4,
        param: int = 0,
        size: int = 6,
        source: int = 0,
        sink: int = 0,
        denied: int = 0,
        data: int = 0,
        corrupt: int = 0,
        delay_cycles: int | None = None,
    ) -> None:
        delay = self.default_delay if delay_cycles is None else delay_cycles
        self._pending.append(
            {
                "release_cycle": self._cycle + delay,
                "opcode": opcode,
                "param": param,
                "size": size,
                "source": source,
                "sink": sink,
                "denied": denied,
                "data": data,
                "corrupt": corrupt,
            }
        )

    def on_outer_buffer_edge(self, cycle: int) -> None:
        self._cycle = cycle
        self.tl_a.ready.value = 1

        if self.tl_a.valid.value and self.tl_a.ready.value:
            self.request_count += 1
            resp_opcode = 4 if self.tl_a.bits_opcode.value == 4 else 5
            self.enqueue_response(
                opcode=resp_opcode,
                size=self.tl_a.bits_size.value,
                source=self.tl_a.bits_source.value,
            )

        if self._active is None and self._pending and self._pending[0]["release_cycle"] <= cycle:
            self._active = self._pending.popleft()

        if self._active is None:
            self.tl_d.drive_idle()
            return

        self.tl_d.valid.value = 1
        self.tl_d.bits_opcode.value = self._active["opcode"]
        self.tl_d.bits_param.value = self._active["param"]
        self.tl_d.bits_size.value = self._active["size"]
        self.tl_d.bits_source.value = self._active["source"]
        self.tl_d.bits_sink.value = self._active["sink"]
        self.tl_d.bits_denied.value = self._active["denied"]
        self.tl_d.bits_data.value = self._active["data"]
        self.tl_d.bits_corrupt.value = self._active["corrupt"]

        if self.tl_d.ready.value:
            self.response_count += 1
            self._active = None

    @property
    def stats(self) -> dict:
        return {
            "request_count": self.request_count,
            "response_count": self.response_count,
            "pending_count": len(self._pending),
            "active_count": 1 if self._active is not None else 0,
        }


class MockDcacheClient:
    """DCache TL-C 外部一致性管理器简化模型。"""

    def __init__(
        self,
        chan_a: DcacheClientABundle,
        chan_b: DcacheClientBBundle,
        chan_c: DcacheClientCBundle,
        chan_d: DcacheClientDBundle,
        chan_e: DcacheClientEBundle,
        default_delay: int = 4,
    ):
        self.chan_a = chan_a
        self.chan_b = chan_b
        self.chan_c = chan_c
        self.chan_d = chan_d
        self.chan_e = chan_e
        self.default_delay = default_delay
        self._cycle = 0
        self._pending_b = deque()
        self._pending_d = deque()
        self._active_b = None
        self._active_d = None
        self.a_request_count = 0
        self.c_request_count = 0
        self.e_request_count = 0
        self.b_response_count = 0
        self.d_response_count = 0

    def reset(self) -> None:
        self._pending_b.clear()
        self._pending_d.clear()
        self._active_b = None
        self._active_d = None
        self.chan_b.drive_idle()
        self.chan_d.drive_idle()

    def enqueue_b_response(
        self,
        opcode: int = 0,
        param: int = 0,
        size: int = 6,
        source: int = 0,
        address: int = 0,
        mask: int = 0,
        data: int = 0,
        corrupt: int = 0,
        delay_cycles: int | None = None,
    ) -> None:
        delay = self.default_delay if delay_cycles is None else delay_cycles
        self._pending_b.append(
            {
                "release_cycle": self._cycle + delay,
                "opcode": opcode,
                "param": param,
                "size": size,
                "source": source,
                "address": address,
                "mask": mask,
                "data": data,
                "corrupt": corrupt,
            }
        )

    def enqueue_d_response(
        self,
        opcode: int = 1,
        param: int = 0,
        size: int = 6,
        source: int = 0,
        sink: int = 0,
        denied: int = 0,
        echo_isKeyword: int = 0,
        data: int = 0,
        corrupt: int = 0,
        delay_cycles: int | None = None,
    ) -> None:
        delay = self.default_delay if delay_cycles is None else delay_cycles
        self._pending_d.append(
            {
                "release_cycle": self._cycle + delay,
                "opcode": opcode,
                "param": param,
                "size": size,
                "source": source,
                "sink": sink,
                "denied": denied,
                "echo_isKeyword": echo_isKeyword,
                "data": data,
                "corrupt": corrupt,
            }
        )

    def on_dcache_client_edge(self, cycle: int) -> None:
        self._cycle = cycle
        self.chan_a.ready.value = 1
        self.chan_c.ready.value = 1
        self.chan_e.ready.value = 1

        if self.chan_a.valid.value and self.chan_a.ready.value:
            self.a_request_count += 1
        if self.chan_c.valid.value and self.chan_c.ready.value:
            self.c_request_count += 1
        if self.chan_e.valid.value and self.chan_e.ready.value:
            self.e_request_count += 1

        self._service_b_channel()
        self._service_d_channel()

    def _service_b_channel(self) -> None:
        if self._active_b is None and self._pending_b and self._pending_b[0]["release_cycle"] <= self._cycle:
            self._active_b = self._pending_b.popleft()

        if self._active_b is None:
            self.chan_b.drive_idle()
            return

        self.chan_b.valid.value = 1
        self.chan_b.bits_opcode.value = self._active_b["opcode"]
        self.chan_b.bits_param.value = self._active_b["param"]
        self.chan_b.bits_size.value = self._active_b["size"]
        self.chan_b.bits_source.value = self._active_b["source"]
        self.chan_b.bits_address.value = self._active_b["address"]
        self.chan_b.bits_mask.value = self._active_b["mask"]
        self.chan_b.bits_data.value = self._active_b["data"]
        self.chan_b.bits_corrupt.value = self._active_b["corrupt"]

        if self.chan_b.ready.value:
            self.b_response_count += 1
            self._active_b = None

    def _service_d_channel(self) -> None:
        if self._active_d is None and self._pending_d and self._pending_d[0]["release_cycle"] <= self._cycle:
            self._active_d = self._pending_d.popleft()

        if self._active_d is None:
            self.chan_d.drive_idle()
            return

        self.chan_d.valid.value = 1
        self.chan_d.bits_opcode.value = self._active_d["opcode"]
        self.chan_d.bits_param.value = self._active_d["param"]
        self.chan_d.bits_size.value = self._active_d["size"]
        self.chan_d.bits_source.value = self._active_d["source"]
        self.chan_d.bits_sink.value = self._active_d["sink"]
        self.chan_d.bits_denied.value = self._active_d["denied"]
        self.chan_d.bits_echo_isKeyword.value = self._active_d["echo_isKeyword"]
        self.chan_d.bits_data.value = self._active_d["data"]
        self.chan_d.bits_corrupt.value = self._active_d["corrupt"]

        if self.chan_d.ready.value:
            self.d_response_count += 1
            self._active_d = None

    @property
    def stats(self) -> dict:
        return {
            "a_request_count": self.a_request_count,
            "c_request_count": self.c_request_count,
            "e_request_count": self.e_request_count,
            "b_response_count": self.b_response_count,
            "d_response_count": self.d_response_count,
            "pending_b_count": len(self._pending_b),
            "pending_d_count": len(self._pending_d),
            "active_b_count": 1 if self._active_b is not None else 0,
            "active_d_count": 1 if self._active_d is not None else 0,
        }


class MemBlockEnv:
    """MemBlock 顶层测试环境。"""

    def __init__(self, dut) -> None:
        self.dut = dut

        self.redirect = RedirectBundle.from_prefix("io_redirect_").bind(dut)

        self.lsq_enq_meta = LsqEnqMetaBundle.from_prefix("io_ooo_to_mem_enqLsq_").bind(dut)
        self.lsq_enq_req = BundleList(LsqEnqReqBundle, "io_ooo_to_mem_enqLsq_req_#_", LSQ_ENQ_PORTS)
        self.lsq_enq_resp = BundleList(LsqEnqRespBundle, "io_ooo_to_mem_enqLsq_resp_#_", LSQ_ENQ_PORTS)
        for bundle in self.lsq_enq_req:
            bundle.bind(dut)
        for bundle in self.lsq_enq_resp:
            bundle.bind(dut)

        self.issue = BundleList(IntIssueBundle, "io_ooo_to_mem_intIssue_#_0_", INT_ISSUE_PORTS)
        self.writeback = BundleList(IntWritebackBundle, "io_mem_to_ooo_intWriteback_#_0_", INT_WRITEBACK_PORTS)
        for bundle in self.issue:
            bundle.bind(dut)
        for bundle in self.writeback:
            bundle.bind(dut)

        self.mem_status = MemStatusBundle.from_dict(
            {
                "lqDeq": "io_mem_to_ooo_lqDeq",
                "sqDeq": "io_mem_to_ooo_sqDeq",
                "lqDeqPtr_flag": "io_mem_to_ooo_lqDeqPtr_flag",
                "lqDeqPtr_value": "io_mem_to_ooo_lqDeqPtr_value",
                "sqDeqPtr_flag": "io_mem_to_ooo_sqDeqPtr_flag",
                "sqDeqPtr_value": "io_mem_to_ooo_sqDeqPtr_value",
                "sbIsEmpty": "io_mem_to_ooo_sbIsEmpty",
                "memoryViolation_valid": "io_mem_to_ooo_memoryViolation_valid",
                "memoryViolation_bits_level": "io_mem_to_ooo_memoryViolation_bits_level",
                "memoryViolation_bits_robIdx_flag": "io_mem_to_ooo_memoryViolation_bits_robIdx_flag",
                "memoryViolation_bits_robIdx_value": "io_mem_to_ooo_memoryViolation_bits_robIdx_value",
                "memoryViolation_bits_target": "io_mem_to_ooo_memoryViolation_bits_target",
            }
        ).bind(dut)

        self.lsq_status = LsqStatusBundle.from_prefix("io_mem_to_ooo_lsqio_").bind(dut)

        self.outer_tl_a = OuterTLABundle.from_prefix("auto_inner_buffers_out_a_").bind(dut)
        self.outer_tl_d = OuterTLDBundle.from_prefix("auto_inner_buffers_out_d_").bind(dut)

        self.dcache_a = DcacheClientABundle.from_prefix("auto_inner_dcache_client_out_a_").bind(dut)
        self.dcache_b = DcacheClientBBundle.from_prefix("auto_inner_dcache_client_out_b_").bind(dut)
        self.dcache_c = DcacheClientCBundle.from_prefix("auto_inner_dcache_client_out_c_").bind(dut)
        self.dcache_d = DcacheClientDBundle.from_prefix("auto_inner_dcache_client_out_d_").bind(dut)
        self.dcache_e = DcacheClientEBundle.from_prefix("auto_inner_dcache_client_out_e_").bind(dut)

        self.mock_outer_buffer = MockOuterBufferMemory(self.outer_tl_a, self.outer_tl_d)
        self.mock_dcache_client = MockDcacheClient(
            self.dcache_a,
            self.dcache_b,
            self.dcache_c,
            self.dcache_d,
            self.dcache_e,
        )

        self.dut.StepRis(self.mock_outer_buffer.on_outer_buffer_edge)
        self.dut.StepRis(self.mock_dcache_client.on_dcache_client_edge)

        self.dut.reset.value = 0
        self.dut.io_hartId.value = 0
        self.idle_inputs()

    def idle_inputs(self) -> None:
        """将 env 管理的输入口恢复到空闲值。"""

        self.redirect.drive_idle()
        self.lsq_enq_meta.drive_idle()
        for bundle in self.lsq_enq_req:
            bundle.drive_idle()
        for bundle in self.issue:
            bundle.drive_idle()
        self.outer_tl_d.drive_idle()
        self.dcache_b.drive_idle()
        self.dcache_d.drive_idle()

    def Step(self, cycles: int = 1) -> None:
        """推进 DUT 时钟。"""

        if cycles < 0:
            raise ValueError(f"cycles 必须非负，当前值为 {cycles}")
        self.dut.Step(cycles)

    def reset(self, cycles: int = 10, settle_cycles: int = 5) -> None:
        """执行同步复位，并清空 mock 内部状态。"""

        self.idle_inputs()
        self.mock_outer_buffer.reset()
        self.mock_dcache_client.reset()
        self.dut.reset.value = 1
        self.Step(cycles)
        self.dut.reset.value = 0
        self.idle_inputs()
        self.Step(settle_cycles)

    def inject_outer_d_response(self, delay_cycles: int = 0, **kwargs) -> None:
        """向 outer buffer D 通道注入一笔响应。"""

        self.mock_outer_buffer.enqueue_response(delay_cycles=delay_cycles, **kwargs)

    def inject_dcache_b_response(self, delay_cycles: int = 0, **kwargs) -> None:
        """向 DCache B 通道注入一笔响应。"""

        self.mock_dcache_client.enqueue_b_response(delay_cycles=delay_cycles, **kwargs)

    def inject_dcache_d_response(self, delay_cycles: int = 0, **kwargs) -> None:
        """向 DCache D 通道注入一笔响应。"""

        self.mock_dcache_client.enqueue_d_response(delay_cycles=delay_cycles, **kwargs)

    def clear_callbacks(self) -> None:
        """移除 env 注册的 StepRis 回调。"""

        self.dut.xclock.RemoveStepRisCbByDesc(self.mock_outer_buffer.on_outer_buffer_edge.__name__)
        self.dut.xclock.RemoveStepRisCbByDesc(self.mock_dcache_client.on_dcache_client_edge.__name__)

    def Finish(self) -> None:
        """释放 DUT 资源。"""

        self.dut.Finish()


@pytest.fixture(scope="function")
def env(dut):
    """MemBlockEnv fixture。"""

    return MemBlockEnv(dut)
