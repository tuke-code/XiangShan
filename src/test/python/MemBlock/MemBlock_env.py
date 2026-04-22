# coding=utf-8
"""
MemBlock 基础测试环境。

本文件实现：
  1. 基于 `toffee.Bundle` 的核心端口封装
  2. 面向 LSU 核心闭环的外部依赖 Mock
  3. `MemBlockEnv` 顶层测试环境
  4. `env` fixture
"""

import asyncio
import inspect
import os
import sys
from collections import defaultdict, deque
from contextlib import contextmanager
from dataclasses import dataclass

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

try:
    from toffee_test.reporter import set_func_coverage
except ImportError:
    def set_func_coverage(*_args, **_kwargs):
        return None


_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.abspath(os.path.join(_HERE, "..", "..", "..", ".."))
_PYLIB = os.path.join(_REPO_ROOT, "build-memblock", "pylib")
_TOFFEE_FALLBACK = os.path.abspath(
    os.path.join(_REPO_ROOT, "..", "XSV", "contrib", "unitychip", "toffee")
)

for _path in (_PYLIB, _HERE, _TOFFEE_FALLBACK):
    if _path not in sys.path:
        sys.path.insert(0, _path)


from toffee import Bundle, BundleList, Signal, SignalList, Signals

from agents.backend_facade import BackendFacade
from agents.commit_agent import CommitAgent
from agents.csr_agent import CsrAgent
from agents.issue_agent import IssueAgent
from agents.lsq_agent import LsqAgent
from agents.vector_backend_facade import VectorBackendFacade
from agents.vector_issue_agent import VectorIssueAgent
from env_config import DEFAULT_ENV_CONFIG, EnvConfig
from memory_model import MemoryModel
from model.rob_coverage import RobCoverageCollector
from monitors.mem_status_monitor import MemStatusMonitor
from monitors.vector_mem_monitor import VectorMemMonitor
from transactions import RobIndex, make_rob_index


LOAD_PIPELINE_WIDTH = DEFAULT_ENV_CONFIG.load_pipeline_width
LSQ_ENQ_PORTS = DEFAULT_ENV_CONFIG.lsq_enq_ports
INT_ISSUE_PORTS = DEFAULT_ENV_CONFIG.int_issue_ports
INT_WRITEBACK_PORTS = DEFAULT_ENV_CONFIG.int_writeback_ports
STORE_PIPELINE_WIDTH = DEFAULT_ENV_CONFIG.store_pipeline_width
SBUFFER_WRITE_PORTS = DEFAULT_ENV_CONFIG.sbuffer_write_ports
STORE_QUEUE_SIZE = DEFAULT_ENV_CONFIG.store_queue_size
ROB_SIZE = DEFAULT_ENV_CONFIG.rob_size
VEC_ISSUE_PORTS = 2
VEC_WRITEBACK_PORTS = 2
LOAD_REPLAY_CAUSE_LABELS = [
    "UNCACHE",
    "SMF",
    "MA",
    "TM",
    "FF",
    "DR",
    "DM",
    "WF",
    "BC",
    "RAR",
    "RAW",
    "NK",
    "MF",
]
TL_B_PROBE = 6
TL_A_GET = 4
TL_D_ACCESS_ACK_DATA = 1
SV39_MODE = 8
PRIV_MODE_S = 1
SV39_PTE_V = 1 << 0
SV39_PTE_R = 1 << 1
SV39_PTE_W = 1 << 2
SV39_PTE_A = 1 << 6
SV39_PTE_D = 1 << 7
SV39_PTE_PBMT_SHIFT = 61
SV39_PBMT_CACHEABLE = 0
SV39_PBMT_NC = 1
SV39_PBMT_IO = 2
PTW_BEAT_BYTES = 32
PMP_CFG_RWX_NAPOT = 0x1F
PMP_CFG_DENY_NAPOT = 0x18
PMP_CFG_CSR_BASE = 0x3A0
PMP_ADDR_CSR_BASE = 0x3B0
MEMBLOCK_PADDR_BITS = 48


def _read_signal_int(signal, default: int = 0) -> int:
    try:
        return int(signal.value)
    except Exception:
        return int(default)


@dataclass(frozen=True)
class StoreView:
    """对外暴露的 store 只读视图。"""

    sq_idx: int
    allocated: bool
    addr: int | None
    data: int | None
    mask: int
    committed: bool
    completed: bool
    mmio: bool
    nc: bool
    mem_back_type_mm: bool
    has_exception: bool
    retired: bool
    rob_idx_flag: int | None
    rob_idx_value: int | None


class PendingPtrDriver:
    """根据 load 完成情况推进 `io_ooo_to_mem_lsqio_pendingPtr`。"""

    def __init__(self, dut, rob_size: int = ROB_SIZE) -> None:
        self.dut = dut
        self.rob_size = rob_size
        self._signal_flag = getattr(dut, "io_ooo_to_mem_lsqio_pendingPtr_flag", None)
        self._signal_value = getattr(dut, "io_ooo_to_mem_lsqio_pendingPtr_value", None)
        self._signal_scommit = getattr(dut, "io_ooo_to_mem_lsqio_scommit", None)
        self.reset()

    def reset(self) -> None:
        self.pending_ptr = RobIndex(flag=0, value=0)
        self._issued = deque()
        self._completed = defaultdict(int)
        self._queued_scommit = 0
        self._active_scommit = 0

    def drive(self) -> None:
        if self._signal_flag is not None:
            self._signal_flag.value = self.pending_ptr.flag
        if self._signal_value is not None:
            self._signal_value.value = self.pending_ptr.value
        self._active_scommit = self._queued_scommit
        if self._signal_scommit is not None:
            self._signal_scommit.value = self._active_scommit
        self._queued_scommit = 0

    def queue_store_commit(self, count: int = 1) -> None:
        self._queued_scommit = max(0, int(count))

    def note_issued(self, rob_idx_flag: int, rob_idx_value: int) -> None:
        self._issued.append(("load", RobIndex(flag=rob_idx_flag, value=rob_idx_value)))

    def note_store_allocated(self, rob_idx_flag: int, rob_idx_value: int) -> None:
        self._issued.append(("store", RobIndex(flag=rob_idx_flag, value=rob_idx_value)))

    def note_completed(self, rob_idx_flag: int, rob_idx_value: int) -> None:
        self._completed[RobIndex(flag=rob_idx_flag, value=rob_idx_value)] += 1

    def advance(self) -> None:
        while self._issued:
            kind, head = self._issued[0]
            if kind == "store":
                if self._active_scommit == 0:
                    break
                self._active_scommit -= 1
            else:
                if self._completed[head] == 0:
                    break
                self._completed[head] -= 1
                if self._completed[head] == 0:
                    del self._completed[head]
            self._issued.popleft()
            self.pending_ptr = self._inc(self.pending_ptr)
        self._active_scommit = 0

    def _inc(self, ptr: RobIndex) -> RobIndex:
        value = ptr.value + 1
        flag = ptr.flag
        if value >= self.rob_size:
            value = 0
            flag ^= 0x1
        return RobIndex(flag=flag, value=value)


class EnvClockKernel:
    """在 env 内集中持有 DUT 时钟推进所有权。"""

    def __init__(self, dut) -> None:
        self.dut = dut

    async def step(self, cycles: int = 1) -> None:
        if cycles < 0:
            raise ValueError(f"cycles 必须非负，当前值为 {cycles}")
        for _ in range(cycles):
            self.dut.Step(1)

    def run(self, coro):
        return asyncio.run(coro)

    def close(self) -> None:
        return None


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
    bits_uopIdx = Signal()
    bits_robIdx_flag = Signal()
    bits_robIdx_value = Signal()
    bits_lqIdx_flag = Signal()
    bits_lqIdx_value = Signal()
    bits_sqIdx_flag = Signal()
    bits_sqIdx_value = Signal()
    bits_numLsElem = Signal()

    def drive_idle(self) -> None:
        self.valid.value = 0
        self.bits_fuType.value = 0
        self.bits_uopIdx.value = 0
        self.bits_robIdx_flag.value = 0
        self.bits_robIdx_value.value = 0
        self.bits_lqIdx_flag.value = 0
        self.bits_lqIdx_value.value = 0
        self.bits_sqIdx_flag.value = 0
        self.bits_sqIdx_value.value = 0
        self.bits_numLsElem.value = 0


class LsqEnqRespBundle(Bundle):
    """单路 LSQ 入队响应接口。"""

    lqIdx_flag, lqIdx_value, sqIdx_flag, sqIdx_value = Signals(4)


class IntIssueBundle(Bundle):
    """整数 issue 单 lane 接口。"""

    ready = Signal()
    valid = Signal()
    bits_fuOpType = Signal()
    bits_src_0 = Signal()
    bits_robIdx_flag = Signal()
    bits_robIdx_value = Signal()
    bits_sqIdx_flag = Signal()
    bits_sqIdx_value = Signal()

    def drive_idle(self) -> None:
        self.valid.value = 0
        self.bits_fuOpType.value = 0
        self.bits_src_0.value = 0
        self.bits_robIdx_flag.value = 0
        self.bits_robIdx_value.value = 0
        self.bits_sqIdx_flag.value = 0
        self.bits_sqIdx_value.value = 0


class IntWritebackBundle:
    """整数 writeback 单 lane 的可选信号封装。"""

    OPTIONAL_SIGNAL_CANDIDATES = {
        "ready": ("ready",),
        "valid": ("valid",),
        "bits_toRob_valid": ("bits_toRob_valid",),
        "bits_data_0": ("bits_data_0", "bits_toIntRf_bits"),
        "bits_pdest": ("bits_pdest",),
        "bits_intWen": ("bits_intWen", "bits_toIntRf_valid"),
        "bits_fpWen": ("bits_fpWen", "bits_toFpRf_valid"),
        "bits_robIdx_flag": ("bits_robIdx_flag", "bits_toRob_bits_robIdx_flag"),
        "bits_robIdx_value": ("bits_robIdx_value", "bits_toRob_bits_robIdx_value"),
        "bits_lqIdx_flag": ("bits_lqIdx_flag", "bits_toRob_bits_lqIdx_flag"),
        "bits_lqIdx_value": ("bits_lqIdx_value", "bits_toRob_bits_lqIdx_value"),
        "bits_sqIdx_flag": ("bits_sqIdx_flag", "bits_toRob_bits_sqIdx_flag"),
        "bits_sqIdx_value": ("bits_sqIdx_value", "bits_toRob_bits_sqIdx_value"),
        "bits_isFromLoadUnit": ("bits_isFromLoadUnit",),
        "bits_debug_isMMIO": ("bits_debug_isMMIO",),
        "bits_debug_isNCIO": ("bits_debug_isNCIO",),
        "bits_debug_isPerfCnt": ("bits_debug_isPerfCnt",),
        "bits_debug_paddr": ("bits_debug_paddr",),
        "bits_debug_vaddr": ("bits_debug_vaddr",),
    }

    def __init__(self, prefix: str, dut) -> None:
        self._prefix = prefix
        self._dut = dut
        for attr_name, suffixes in self.OPTIONAL_SIGNAL_CANDIDATES.items():
            setattr(self, attr_name, self._bind_optional(*suffixes))
        self.bits_exception_vec = [
            self._bind_optional(f"bits_exceptionVec_{idx}", f"bits_toRob_bits_exceptionVec_{idx}")
            for idx in range(24)
        ]

    def _bind_optional(self, *suffixes: str):
        for suffix in suffixes:
            signal = getattr(self._dut, f"{self._prefix}{suffix}", None)
            if signal is not None:
                return signal
        return None

    def _signal_attr(self, name: str) -> str:
        if name in {"ready", "valid"}:
            return name
        return f"bits_{name}"

    def connected(self, name: str) -> bool:
        signal = getattr(self, self._signal_attr(name), None)
        return signal is not None

    def read(self, name: str, default=None):
        signal = getattr(self, self._signal_attr(name), None)
        if signal is None:
            return default
        return int(signal.value)

    def set_ready(self, value: int = 1) -> None:
        if self.ready is not None:
            self.ready.value = value

    def read_exception_bits(self) -> list[int]:
        bits = []
        for signal in self.bits_exception_vec:
            bits.append(0 if signal is None else int(signal.value))
        return bits


class OptionalSignalBundle:
    """为内建调试/观测信号提供统一 `connected/read` 接口。"""

    SIGNAL_MAP = {}

    def __init__(self, prefix: str, dut) -> None:
        self._prefix = prefix
        self._dut = dut
        for attr_name, suffix in self.SIGNAL_MAP.items():
            setattr(self, attr_name, getattr(dut, f"{prefix}{suffix}", None))

    def connected(self, name: str) -> bool:
        return getattr(self, name, None) is not None

    def read(self, name: str, default=None):
        signal = getattr(self, name, None)
        if signal is None:
            return default
        return int(signal.value)


class VecIssueBundle(OptionalSignalBundle):
    SIGNAL_MAP = {
        "ready": "ready",
        "valid": "valid",
        "bits_fuType": "bits_fuType",
        "bits_fuOpType": "bits_fuOpType",
        "bits_src_0": "bits_src_0",
        "bits_src_1": "bits_src_1",
        "bits_src_2": "bits_src_2",
        "bits_src_3": "bits_src_3",
        "bits_vl": "bits_vl",
        "bits_robIdx_flag": "bits_robIdx_flag",
        "bits_robIdx_value": "bits_robIdx_value",
        "bits_pdest": "bits_pdest",
        "bits_pdestVl": "bits_pdestVl",
        "bits_vecWen": "bits_vecWen",
        "bits_v0Wen": "bits_v0Wen",
        "bits_vlWen": "bits_vlWen",
        "bits_vpu_vsew": "bits_vpu_vsew",
        "bits_vpu_vlmul": "bits_vpu_vlmul",
        "bits_vpu_vm": "bits_vpu_vm",
        "bits_vpu_vstart": "bits_vpu_vstart",
        "bits_vpu_vuopIdx": "bits_vpu_vuopIdx",
        "bits_vpu_lastUop": "bits_vpu_lastUop",
        "bits_vpu_vmask": "bits_vpu_vmask",
        "bits_vpu_nf": "bits_vpu_nf",
        "bits_vpu_veew": "bits_vpu_veew",
        "bits_vpu_isVleff": "bits_vpu_isVleff",
        "bits_ftqIdx_flag": "bits_ftqIdx_flag",
        "bits_ftqIdx_value": "bits_ftqIdx_value",
        "bits_ftqOffset": "bits_ftqOffset",
        "bits_numLsElem": "bits_numLsElem",
        "bits_lqIdx_flag": "bits_lqIdx_flag",
        "bits_lqIdx_value": "bits_lqIdx_value",
        "bits_sqIdx_flag": "bits_sqIdx_flag",
        "bits_sqIdx_value": "bits_sqIdx_value",
    }

    def write(self, name: str, value: int) -> None:
        signal = getattr(self, name, None)
        if signal is not None:
            signal.value = int(value)

    def drive_idle(self) -> None:
        for name in self.SIGNAL_MAP:
            if name == "ready":
                continue
            self.write(name, 0)


class VecWritebackBundle(OptionalSignalBundle):
    SIGNAL_MAP = {
        "ready": "ready",
        "valid": "valid",
        "data_0": "bits_data_0",
        "pdest": "bits_pdest",
        "pdestVl": "bits_pdestVl",
        "robIdx_flag": "bits_robIdx_flag",
        "robIdx_value": "bits_robIdx_value",
        "vecWen": "bits_vecWen",
        "v0Wen": "bits_v0Wen",
        "vlWen": "bits_vlWen",
        "vls_vpu_vstart": "bits_vls_vpu_vstart",
        "vls_vpu_vl": "bits_vls_vpu_vl",
        "vls_vpu_lastUop": "bits_vls_vpu_lastUop",
        "vls_isStrided": "bits_vls_isStrided",
        "vls_isVecLoad": "bits_vls_isVecLoad",
        "debug_isMMIO": "bits_debug_isMMIO",
        "debug_isNCIO": "bits_debug_isNCIO",
        "debug_paddr": "bits_debug_paddr",
        "debug_vaddr": "bits_debug_vaddr",
    }

    def __init__(self, prefix: str, dut) -> None:
        super().__init__(prefix, dut)
        self.bits_exception_vec = [
            getattr(dut, f"{prefix}bits_exceptionVec_{idx}", None)
            for idx in range(24)
        ]

    def set_ready(self, value: int = 1) -> None:
        if self.ready is not None:
            self.ready.value = int(value)

    def read_exception_bits(self) -> list[int]:
        return [0 if signal is None else int(signal.value) for signal in self.bits_exception_vec]


class StoreDataInputBundle(OptionalSignalBundle):
    SIGNAL_MAP = {
        "valid": "valid",
        "sqIdx_value": "bits_sqIdx_value",
        "fuType": "bits_fuType",
        "fuOpType": "bits_fuOpType",
        "data": "bits_data",
    }


class StoreAddrInputBundle(OptionalSignalBundle):
    SIGNAL_MAP = {
        "valid": "valid",
        "sqIdx_value": "bits_uop_sqIdx_value",
        "paddr": "bits_paddr",
        "mask": "bits_mask",
        "miss": "bits_miss",
        "nc": "bits_nc",
    }


class StoreMaskInputBundle(OptionalSignalBundle):
    SIGNAL_MAP = {
        "valid": "valid",
        "sqIdx_value": "bits_sqIdx_value",
        "mask": "bits_mask",
    }


class StoreAddrReInputBundle(OptionalSignalBundle):
    SIGNAL_MAP = {
        "updateAddrValid": "updateAddrValid",
        "sqIdx_value": "uop_sqIdx_value",
        "nc": "nc",
        "mmio": "mmio",
        "memBackTypeMM": "memBackTypeMM",
        "hasException": "hasException",
    }


class SbufferWriteBundle(OptionalSignalBundle):
    SIGNAL_MAP = {
        "ready": "ready",
        "valid": "valid",
        "addr": "bits_addr",
        "data": "bits_data",
        "mask": "bits_mask",
        "wline": "bits_wline",
        "vecValid": "bits_vecValid",
    }


class StoreQueueShadowEntry(OptionalSignalBundle):
    def __init__(self, index: int, dut) -> None:
        self._index = index
        self._dut = dut
        self.allocated = getattr(dut, f"MemBlock_inner_lsq_storeQueue_allocated_{index}", None)
        self.addrvalid = getattr(dut, f"MemBlock_inner_lsq_storeQueue_addrvalid_{index}", None)
        self.datavalid = getattr(dut, f"MemBlock_inner_lsq_storeQueue_datavalid_{index}", None)
        self.committed = getattr(dut, f"MemBlock_inner_lsq_storeQueue_committed_{index}", None)
        self.completed = getattr(dut, f"MemBlock_inner_lsq_storeQueue_completed_{index}", None)
        self.nc = getattr(dut, f"MemBlock_inner_lsq_storeQueue_nc_{index}", None)
        self.robIdx_flag = getattr(dut, f"MemBlock_inner_lsq_storeQueue_uop_{index}_robIdx_flag", None)
        self.robIdx_value = getattr(dut, f"MemBlock_inner_lsq_storeQueue_uop_{index}_robIdx_value", None)


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
    mmioBusy = Signal()
    lqCanAccept = Signal()
    sqCanAccept = Signal()


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


class TlbCsrBundle(Bundle):
    """MemBlock `tlbCsr` 输入接口。"""

    satp_mode = Signal()
    satp_asid = Signal()
    satp_ppn = Signal()
    satp_changed = Signal()
    vsatp_mode = Signal()
    vsatp_asid = Signal()
    vsatp_ppn = Signal()
    vsatp_changed = Signal()
    hgatp_mode = Signal()
    hgatp_vmid = Signal()
    hgatp_ppn = Signal()
    hgatp_changed = Signal()
    mbmc_BME = Signal()
    mbmc_CMODE = Signal()
    mbmc_BCLEAR = Signal()
    mbmc_BMA = Signal()
    priv_mxr = Signal()
    priv_sum = Signal()
    priv_vmxr = Signal()
    priv_vsum = Signal()
    priv_virt = Signal()
    priv_virt_changed = Signal()
    priv_spvp = Signal()
    priv_imode = Signal()
    priv_dmode = Signal()
    mPBMTE = Signal()
    hPBMTE = Signal()
    pmm_mseccfg = Signal()
    pmm_menvcfg = Signal()
    pmm_henvcfg = Signal()
    pmm_hstatus = Signal()
    pmm_senvcfg = Signal()


class CsrCtrlBundle(Bundle):
    """MemBlock `csrCtrl` 输入接口。"""

    pf_ctrl_l1I_pf_enable = Signal()
    pf_ctrl_l2_pf_enable = Signal()
    pf_ctrl_l1D_pf_enable = Signal()
    pf_ctrl_l1D_pf_train_on_hit = Signal()
    pf_ctrl_l1D_pf_enable_agt = Signal()
    pf_ctrl_l1D_pf_enable_pht = Signal()
    pf_ctrl_l1D_pf_active_threshold = Signal()
    pf_ctrl_l1D_pf_active_stride = Signal()
    pf_ctrl_l1D_pf_enable_stride = Signal()
    pf_ctrl_l2_pf_store_only = Signal()
    pf_ctrl_l2_pf_recv_enable = Signal()
    pf_ctrl_l2_pf_pbop_enable = Signal()
    pf_ctrl_l2_pf_vbop_enable = Signal()
    pf_ctrl_l2_pf_tp_enable = Signal()
    pf_ctrl_l2_pf_delay_latency = Signal()
    bp_ctrl_ubtbEnable = Signal()
    bp_ctrl_abtbEnable = Signal()
    bp_ctrl_mbtbEnable = Signal()
    bp_ctrl_tageEnable = Signal()
    bp_ctrl_scEnable = Signal()
    bp_ctrl_ittageEnable = Signal()
    sbuffer_timeout = Signal()
    ldld_vio_check_enable = Signal()
    cache_error_enable = Signal()
    uncache_write_outstanding_enable = Signal()
    hd_misalign_st_enable = Signal()
    power_down_enable = Signal()
    flush_l2_enable = Signal()
    distribute_csr_w_valid = Signal()
    distribute_csr_w_bits_addr = Signal()
    distribute_csr_w_bits_data = Signal()
    frontend_trigger_tUpdate_valid = Signal()
    frontend_trigger_tUpdate_bits_addr = Signal()
    frontend_trigger_tUpdate_bits_tdata_matchType = Signal()
    frontend_trigger_tUpdate_bits_tdata_select = Signal()
    frontend_trigger_tUpdate_bits_tdata_timing = Signal()
    frontend_trigger_tUpdate_bits_tdata_action = Signal()
    frontend_trigger_tUpdate_bits_tdata_chain = Signal()
    frontend_trigger_tUpdate_bits_tdata_execute = Signal()
    frontend_trigger_tUpdate_bits_tdata_store = Signal()
    frontend_trigger_tUpdate_bits_tdata_load = Signal()
    frontend_trigger_tUpdate_bits_tdata_tdata2 = Signal()
    frontend_trigger_tEnableVec_0 = Signal()
    frontend_trigger_tEnableVec_1 = Signal()
    frontend_trigger_tEnableVec_2 = Signal()
    frontend_trigger_tEnableVec_3 = Signal()
    frontend_trigger_debugMode = Signal()
    frontend_trigger_triggerCanRaiseBpExp = Signal()
    mem_trigger_tUpdate_valid = Signal()
    mem_trigger_tUpdate_bits_addr = Signal()
    mem_trigger_tUpdate_bits_tdata_matchType = Signal()
    mem_trigger_tUpdate_bits_tdata_select = Signal()
    mem_trigger_tUpdate_bits_tdata_timing = Signal()
    mem_trigger_tUpdate_bits_tdata_action = Signal()
    mem_trigger_tUpdate_bits_tdata_chain = Signal()
    mem_trigger_tUpdate_bits_tdata_store = Signal()
    mem_trigger_tUpdate_bits_tdata_load = Signal()
    mem_trigger_tUpdate_bits_tdata_tdata2 = Signal()
    mem_trigger_tEnableVec_0 = Signal()
    mem_trigger_tEnableVec_1 = Signal()
    mem_trigger_tEnableVec_2 = Signal()
    mem_trigger_tEnableVec_3 = Signal()
    mem_trigger_debugMode = Signal()
    mem_trigger_triggerCanRaiseBpExp = Signal()
    fsIsOff = Signal()


class _PtwTileLinkResponder:
    """为 MMU smoke 提供最小可用的 PTW TileLink D 通道响应。"""

    def __init__(self, env, *, response_delay_cycles: int = 1) -> None:
        self.env = env
        self.response_delay_cycles = max(0, int(response_delay_cycles))
        self._pending = deque()
        self._active = None
        self.trace = deque(maxlen=32)

    def attach(self) -> None:
        self._drive_idle()
        self.env.add_after_step_callback(self.after_step)

    def detach(self) -> None:
        self.env.remove_after_step_callback(self.after_step)
        self._pending.clear()
        self._active = None
        self._drive_idle()

    def after_step(self) -> None:
        if int(self.env.dut.reset.value) or int(self.env.dut.io_reset_backend.value):
            self._pending.clear()
            self._active = None
            self._drive_idle()
            return

        self._drive_a_ready()
        if self._a_fire():
            self._capture_request()

        if self._active is None and self._pending:
            if self._pending[0]["release_cycle"] <= self.env._current_cycle():
                self._active = self._pending.popleft()

        if self._active is None:
            self._drive_d_idle()
            return

        beat = self._active["beats"][0]
        self._drive_d_response(beat)
        if self._d_fire():
            self.trace.append(
                {
                    "cycle": self.env._current_cycle(),
                    "event": "d_fire",
                    "opcode": int(beat["opcode"]),
                    "size": int(beat["size"]),
                    "source": int(beat["source"]),
                    "beat_idx": int(beat["beat_idx"]),
                }
            )
            self._active["beats"].popleft()
            if not self._active["beats"]:
                self._active = None

    def _drive_a_ready(self) -> None:
        signal = getattr(self.env.dut, "auto_inner_ptw_to_l2_buffer_out_a_ready", None)
        if signal is not None:
            signal.value = 1

    def _a_fire(self) -> bool:
        valid = getattr(self.env.dut, "auto_inner_ptw_to_l2_buffer_out_a_valid", None)
        ready = getattr(self.env.dut, "auto_inner_ptw_to_l2_buffer_out_a_ready", None)
        return valid is not None and ready is not None and int(valid.value) and int(ready.value)

    def _d_fire(self) -> bool:
        valid = getattr(self.env.dut, "auto_inner_ptw_to_l2_buffer_out_d_valid", None)
        ready = getattr(self.env.dut, "auto_inner_ptw_to_l2_buffer_out_d_ready", None)
        return valid is not None and ready is not None and int(valid.value) and int(ready.value)

    def _capture_request(self) -> None:
        opcode = _read_signal_int(getattr(self.env.dut, "auto_inner_ptw_to_l2_buffer_out_a_bits_opcode", None), 0)
        size = _read_signal_int(getattr(self.env.dut, "auto_inner_ptw_to_l2_buffer_out_a_bits_size", None), 0)
        source = _read_signal_int(getattr(self.env.dut, "auto_inner_ptw_to_l2_buffer_out_a_bits_source", None), 0)
        address = _read_signal_int(getattr(self.env.dut, "auto_inner_ptw_to_l2_buffer_out_a_bits_address", None), 0)
        if opcode != TL_A_GET:
            raise NotImplementedError(f"暂不支持 PTW TL-A opcode={opcode}")

        transfer_bytes = max(PTW_BEAT_BYTES, 1 << int(size))
        beat_count = max(1, (transfer_bytes + PTW_BEAT_BYTES - 1) // PTW_BEAT_BYTES)
        beat_base = int(address) & ~(PTW_BEAT_BYTES - 1)
        beats = []
        for beat_idx in range(beat_count):
            line_addr = beat_base + beat_idx * PTW_BEAT_BYTES
            beat_data = int.from_bytes(self.env.memory.read_cacheline(line_addr, line_bytes=PTW_BEAT_BYTES), "little")
            beats.append(
                {
                    "opcode": TL_D_ACCESS_ACK_DATA,
                    "size": int(size),
                    "source": int(source),
                    "data": int(beat_data),
                    "beat_idx": beat_idx,
                }
            )

        self._pending.append(
            {
                "release_cycle": self.env._current_cycle() + self.response_delay_cycles,
                "beats": deque(beats),
            }
        )
        self.trace.append(
            {
                "cycle": self.env._current_cycle(),
                "event": "a_fire",
                "opcode": int(opcode),
                "size": int(size),
                "source": int(source),
                "address": int(address),
                "beat_base": int(beat_base),
                "beat_count": int(beat_count),
            }
        )

    def _drive_idle(self) -> None:
        self._drive_a_ready()
        self._drive_d_idle()

    def _drive_d_idle(self) -> None:
        d_valid = getattr(self.env.dut, "auto_inner_ptw_to_l2_buffer_out_d_valid", None)
        if d_valid is not None:
            d_valid.value = 0
        for signal_name in (
            "auto_inner_ptw_to_l2_buffer_out_d_bits_opcode",
            "auto_inner_ptw_to_l2_buffer_out_d_bits_param",
            "auto_inner_ptw_to_l2_buffer_out_d_bits_size",
            "auto_inner_ptw_to_l2_buffer_out_d_bits_source",
            "auto_inner_ptw_to_l2_buffer_out_d_bits_sink",
            "auto_inner_ptw_to_l2_buffer_out_d_bits_denied",
            "auto_inner_ptw_to_l2_buffer_out_d_bits_data",
            "auto_inner_ptw_to_l2_buffer_out_d_bits_corrupt",
        ):
            signal = getattr(self.env.dut, signal_name, None)
            if signal is not None:
                signal.value = 0

    def _drive_d_response(self, beat: dict) -> None:
        self.env.dut.auto_inner_ptw_to_l2_buffer_out_d_valid.value = 1
        self.env.dut.auto_inner_ptw_to_l2_buffer_out_d_bits_opcode.value = int(beat["opcode"])
        self.env.dut.auto_inner_ptw_to_l2_buffer_out_d_bits_param.value = 0
        self.env.dut.auto_inner_ptw_to_l2_buffer_out_d_bits_size.value = int(beat["size"])
        self.env.dut.auto_inner_ptw_to_l2_buffer_out_d_bits_source.value = int(beat["source"])
        self.env.dut.auto_inner_ptw_to_l2_buffer_out_d_bits_sink.value = 0
        self.env.dut.auto_inner_ptw_to_l2_buffer_out_d_bits_denied.value = 0
        self.env.dut.auto_inner_ptw_to_l2_buffer_out_d_bits_data.value = int(beat["data"])
        self.env.dut.auto_inner_ptw_to_l2_buffer_out_d_bits_corrupt.value = 0


@dataclass(frozen=True)
class _ActiveSv39State:
    root_pt_addr: int
    asid: int
    pbmte_enabled: bool = False
    pmm_menvcfg: int = 0
    pmm_henvcfg: int = 0


class MmuFacade:
    """集中管理 MemBlock MMU/PTW/DTLB smoke 所需的稳定控制面。"""

    def __init__(self, env) -> None:
        self.env = env
        self._active_sv39 = None
        self._persistent_csr_writes = {}
        self._pmp_cfg_words = {}
        self._ptw_responder = None
        self._svpbmt_enabled = False
        self._pmm_menvcfg = 0
        self._pmm_henvcfg = 0

    def reapply_inputs(self) -> None:
        if self._active_sv39 is None:
            return
        self._drive_sv39_root(
            root_pt_addr=self._active_sv39.root_pt_addr,
            asid=self._active_sv39.asid,
            satp_changed=0,
        )
        self._drive_svpbmt_state(
            enabled=self._active_sv39.pbmte_enabled,
            pmm_menvcfg=self._active_sv39.pmm_menvcfg,
            pmm_henvcfg=self._active_sv39.pmm_henvcfg,
        )

    async def reapply_after_reset_async(self) -> None:
        for addr, data in self._persistent_csr_writes.items():
            await self._pulse_distributed_csr_write_async(addr, data)
        if self._active_sv39 is not None:
            self.reapply_inputs()
            await self.pulse_sfence_async()

    def reapply_after_reset(self) -> None:
        self.env._run_async(self.reapply_after_reset_async())

    def _remember_csr_write(self, addr: int, data: int) -> None:
        self._persistent_csr_writes[int(addr)] = int(data)

    async def _pulse_distributed_csr_write_async(self, addr: int, data: int) -> None:
        self.env.csr_ctrl.distribute_csr_w_bits_addr.value = int(addr)
        self.env.csr_ctrl.distribute_csr_w_bits_data.value = int(data)
        self.env.csr_ctrl.distribute_csr_w_valid.value = 1
        await self.env._step_async(1)
        self.env.csr_ctrl.distribute_csr_w_valid.value = 0
        await self.env._step_async(1)

    def _pulse_distributed_csr_write(self, addr: int, data: int) -> None:
        self.env._run_async(self._pulse_distributed_csr_write_async(addr, data))

    def write_distributed_csr(self, addr: int, data: int, *, persistent: bool = False) -> None:
        if persistent:
            self._remember_csr_write(addr, data)
        self._pulse_distributed_csr_write(addr, data)

    def _drive_sv39_root(self, *, root_pt_addr: int, asid: int, satp_changed: int) -> None:
        self.env.tlb_csr.priv_virt.value = 0
        self.env.tlb_csr.priv_virt_changed.value = 0
        self.env.tlb_csr.priv_imode.value = PRIV_MODE_S
        self.env.tlb_csr.priv_dmode.value = PRIV_MODE_S
        self.env.tlb_csr.satp_mode.value = SV39_MODE
        self.env.tlb_csr.satp_asid.value = int(asid)
        self.env.tlb_csr.satp_ppn.value = int(root_pt_addr) >> 12
        self.env.tlb_csr.satp_changed.value = int(satp_changed)

    def _drive_svpbmt_state(self, *, enabled: bool, pmm_menvcfg: int = 0, pmm_henvcfg: int = 0) -> None:
        self.env.tlb_csr.mPBMTE.value = int(bool(enabled))
        self.env.tlb_csr.hPBMTE.value = int(bool(enabled))
        self.env.tlb_csr.pmm_menvcfg.value = int(pmm_menvcfg) & 0x3
        self.env.tlb_csr.pmm_henvcfg.value = int(pmm_henvcfg) & 0x3

    def enable_svpbmt(self, *, pmm_menvcfg: int = 0, pmm_henvcfg: int = 0) -> None:
        self._svpbmt_enabled = True
        self._pmm_menvcfg = int(pmm_menvcfg) & 0x3
        self._pmm_henvcfg = int(pmm_henvcfg) & 0x3
        self._drive_svpbmt_state(
            enabled=True,
            pmm_menvcfg=self._pmm_menvcfg,
            pmm_henvcfg=self._pmm_henvcfg,
        )

    def disable_svpbmt(self) -> None:
        self._svpbmt_enabled = False
        self._pmm_menvcfg = 0
        self._pmm_henvcfg = 0
        self._drive_svpbmt_state(enabled=False, pmm_menvcfg=0, pmm_henvcfg=0)

    async def enable_sv39_async(self, *, root_pt_addr: int, asid: int = 0, settle_cycles: int = 4) -> None:
        self._active_sv39 = _ActiveSv39State(
            root_pt_addr=int(root_pt_addr),
            asid=int(asid),
            pbmte_enabled=self._svpbmt_enabled,
            pmm_menvcfg=self._pmm_menvcfg,
            pmm_henvcfg=self._pmm_henvcfg,
        )
        self._drive_sv39_root(root_pt_addr=root_pt_addr, asid=asid, satp_changed=1)
        self._drive_svpbmt_state(
            enabled=self._active_sv39.pbmte_enabled,
            pmm_menvcfg=self._active_sv39.pmm_menvcfg,
            pmm_henvcfg=self._active_sv39.pmm_henvcfg,
        )
        await self.env._step_async(1)
        self.reapply_inputs()
        await self.pulse_sfence_async()
        if settle_cycles > 0:
            await self.env._step_async(settle_cycles)

    def enable_sv39(self, *, root_pt_addr: int, asid: int = 0, settle_cycles: int = 4) -> None:
        self.env._run_async(
            self.enable_sv39_async(
                root_pt_addr=root_pt_addr,
                asid=asid,
                settle_cycles=settle_cycles,
            )
        )

    def disable_translation(self) -> None:
        self._active_sv39 = None
        self._drive_translation_disabled()

    def _drive_translation_disabled(self) -> None:
        self.env.tlb_csr.satp_mode.value = 0
        self.env.tlb_csr.satp_asid.value = 0
        self.env.tlb_csr.satp_ppn.value = 0
        self.env.tlb_csr.satp_changed.value = 0
        self.env.tlb_csr.vsatp_mode.value = 0
        self.env.tlb_csr.vsatp_asid.value = 0
        self.env.tlb_csr.vsatp_ppn.value = 0
        self.env.tlb_csr.vsatp_changed.value = 0
        self.env.tlb_csr.hgatp_mode.value = 0
        self.env.tlb_csr.hgatp_vmid.value = 0
        self.env.tlb_csr.hgatp_ppn.value = 0
        self.env.tlb_csr.hgatp_changed.value = 0
        self.env.tlb_csr.priv_virt.value = 0
        self.env.tlb_csr.priv_virt_changed.value = 0
        self.env.tlb_csr.priv_imode.value = self.env.csr_agent.MODE_M
        self.env.tlb_csr.priv_dmode.value = self.env.csr_agent.MODE_M
        self._drive_svpbmt_state(enabled=False, pmm_menvcfg=0, pmm_henvcfg=0)

    async def pulse_sfence_async(self) -> None:
        if not hasattr(self.env.dut, "io_ooo_to_mem_sfence_valid"):
            return
        self.env.dut.io_ooo_to_mem_sfence_valid.value = 1
        self.env.dut.io_ooo_to_mem_sfence_bits_rs1.value = 0
        self.env.dut.io_ooo_to_mem_sfence_bits_rs2.value = 0
        self.env.dut.io_ooo_to_mem_sfence_bits_addr.value = 0
        self.env.dut.io_ooo_to_mem_sfence_bits_id.value = 0
        self.env.dut.io_ooo_to_mem_sfence_bits_hv.value = 0
        self.env.dut.io_ooo_to_mem_sfence_bits_hg.value = 0
        await self.env._step_async(1)
        self.env.dut.io_ooo_to_mem_sfence_valid.value = 0

    def pulse_sfence(self) -> None:
        self.env._run_async(self.pulse_sfence_async())

    @staticmethod
    def _sv39_pbmt_bits(pbmt: str | int) -> int:
        if isinstance(pbmt, str):
            normalized = pbmt.strip().lower()
            mapping = {
                "cacheable": SV39_PBMT_CACHEABLE,
                "default": SV39_PBMT_CACHEABLE,
                "nc": SV39_PBMT_NC,
                "ncio": SV39_PBMT_NC,
                "io": SV39_PBMT_IO,
                "mmio": SV39_PBMT_IO,
            }
            if normalized not in mapping:
                raise ValueError(f"unsupported Svpbmt kind: {pbmt}")
            return mapping[normalized]
        value = int(pbmt)
        if value not in {SV39_PBMT_CACHEABLE, SV39_PBMT_NC, SV39_PBMT_IO}:
            raise ValueError(f"unsupported Svpbmt value: {pbmt}")
        return value

    def sv39_gigapage_leaf_pte(self, pa_base: int, *, pbmt: str | int = "cacheable") -> int:
        if int(pa_base) & ((1 << 30) - 1):
            raise ValueError(f"SV39 1GiB leaf PTE 需要 1GiB 对齐物理地址: 0x{int(pa_base):x}")
        pbmt_bits = self._sv39_pbmt_bits(pbmt)
        return (
            ((int(pa_base) >> 12) << 10)
            | (pbmt_bits << SV39_PTE_PBMT_SHIFT)
            | SV39_PTE_V
            | SV39_PTE_R
            | SV39_PTE_W
            | SV39_PTE_A
            | SV39_PTE_D
        )

    def install_sv39_gigapage_mapping(
        self,
        *,
        root_pt_addr: int,
        va: int,
        pa_base: int,
        pbmt: str | int = "cacheable",
    ) -> int:
        vpn2 = (int(va) >> 30) & 0x1FF
        pte_addr = int(root_pt_addr) + vpn2 * 8
        self.env.preload_u64(pte_addr, self.sv39_gigapage_leaf_pte(pa_base, pbmt=pbmt))
        return pte_addr

    def program_pmp_entry(self, *, index: int, cfg: int, addr: int, persistent: bool = True) -> None:
        if int(index) < 0:
            raise ValueError(f"非法 PMP entry 索引: {index}")
        cfg_addr = PMP_CFG_CSR_BASE + (int(index) // 8) * 2
        cfg_shift = (int(index) % 8) * 8
        cfg_word = int(self._pmp_cfg_words.get(cfg_addr, 0))
        cfg_word &= ~(0xFF << cfg_shift)
        cfg_word |= (int(cfg) & 0xFF) << cfg_shift
        self._pmp_cfg_words[cfg_addr] = cfg_word
        self.write_distributed_csr(PMP_ADDR_CSR_BASE + int(index), int(addr), persistent=persistent)
        self.write_distributed_csr(cfg_addr, cfg_word, persistent=persistent)

    @staticmethod
    def _encode_pmp_napot_addr(*, base_addr: int, size: int) -> int:
        base_addr = int(base_addr)
        size = int(size)
        if size < 8 or (size & (size - 1)) != 0:
            raise ValueError(f"PMP NAPOT region size 必须是 >=8 的 2 次幂，当前为 {size}")
        if base_addr < 0:
            raise ValueError(f"PMP region base_addr 非法: {base_addr}")
        if base_addr & (size - 1):
            raise ValueError(
                f"PMP NAPOT region 需要按 region size 对齐: base=0x{base_addr:x}, size=0x{size:x}"
            )
        return (base_addr + (size // 2 - 1)) >> 2

    def program_pmp_deny_region(
        self,
        base_addr: int,
        size: int,
        *,
        index: int = 0,
        persistent: bool = True,
    ) -> None:
        self.program_pmp_entry(
            index=index,
            cfg=PMP_CFG_DENY_NAPOT,
            addr=self._encode_pmp_napot_addr(base_addr=int(base_addr), size=int(size)),
            persistent=persistent,
        )

    def allow_all_smode_access(self, *, index: int = 0, persistent: bool = True) -> None:
        max_napot_addr = (1 << (MEMBLOCK_PADDR_BITS - 2)) - 1
        self.program_pmp_entry(
            index=index,
            cfg=PMP_CFG_RWX_NAPOT,
            addr=max_napot_addr,
            persistent=persistent,
        )

    def attach_ptw_responder(self, *, response_delay_cycles: int = 1):
        if self._ptw_responder is not None:
            raise RuntimeError("PTW responder 已挂载，请先 detach")
        self._ptw_responder = _PtwTileLinkResponder(self.env, response_delay_cycles=response_delay_cycles)
        self._ptw_responder.attach()
        return self._ptw_responder

    def detach_ptw_responder(self) -> None:
        if self._ptw_responder is None:
            return
        self._ptw_responder.detach()
        self._ptw_responder = None

    @contextmanager
    def ptw_responder(self, *, response_delay_cycles: int = 1):
        responder = self.attach_ptw_responder(response_delay_cycles=response_delay_cycles)
        try:
            yield responder
        finally:
            self.detach_ptw_responder()


class MockCSRInterface:
    """MemBlock CSR 相关输入口 mock，默认工作在 M-mode。"""

    MODE_M = 3

    def __init__(self) -> None:
        self.tlb_csr = TlbCsrBundle.from_prefix("io_ooo_to_mem_tlbCsr_")
        self.csr_ctrl = CsrCtrlBundle.from_prefix("io_ooo_to_mem_csrCtrl_")

    def bind(self, dut):
        """绑定 CSR 相关 DUT 输入口。"""

        self.tlb_csr.bind(dut)
        self.csr_ctrl.bind(dut)
        return self

    def reset(self) -> None:
        """恢复默认 CSR 配置。"""

        self._drive_zero()
        self.set_m_mode()

    def set_m_mode(self) -> None:
        """默认配置为非虚拟化的 M-mode。"""

        self.tlb_csr.priv_virt.value = 0
        self.tlb_csr.priv_virt_changed.value = 0
        self.tlb_csr.priv_imode.value = self.MODE_M
        self.tlb_csr.priv_dmode.value = self.MODE_M

    def _drive_zero(self) -> None:
        for _, signal in self.tlb_csr.all_signals():
            signal.value = 0
        for _, signal in self.csr_ctrl.all_signals():
            signal.value = 0


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

    def __init__(self, dut, config: EnvConfig | None = None) -> None:
        self.dut = dut
        self.config = DEFAULT_ENV_CONFIG if config is None else config
        self._validate_structural_config()
        self._clock = EnvClockKernel(dut)
        self.rob_agent = None
        self.pending_ptr = None
        self.commit_agent = CommitAgent(self)

        self.redirect = RedirectBundle.from_prefix("io_redirect_").bind(dut)
        self.tlb_csr = TlbCsrBundle.from_prefix("io_ooo_to_mem_tlbCsr_").bind(dut)
        self.csr_ctrl = CsrCtrlBundle.from_prefix("io_ooo_to_mem_csrCtrl_").bind(dut)
        self.csr_agent = CsrAgent(self)
        self.mock_csr = self.csr_agent

        self.lsq_enq_meta = LsqEnqMetaBundle.from_prefix("io_ooo_to_mem_enqLsq_").bind(dut)
        self.lsq_enq_req = BundleList(LsqEnqReqBundle, "io_ooo_to_mem_enqLsq_req_#_", self.config.lsq_enq_ports)
        self.lsq_enq_resp = BundleList(LsqEnqRespBundle, "io_ooo_to_mem_enqLsq_resp_#_", self.config.lsq_enq_ports)
        for bundle in self.lsq_enq_req:
            bundle.bind(dut)
        for bundle in self.lsq_enq_resp:
            bundle.bind(dut)
        self.lsq_agent = LsqAgent(self)

        self.issue = BundleList(IntIssueBundle, "io_ooo_to_mem_intIssue_#_0_", self.config.int_issue_ports)
        for bundle in self.issue:
            bundle.bind(dut)
        self.issue_agent = IssueAgent(self)
        self.vector_issue = [
            VecIssueBundle(f"io_ooo_to_mem_vecIssue_{idx}_0_", dut)
            for idx in range(VEC_ISSUE_PORTS)
        ]
        self.vector_issue_agent = VectorIssueAgent(self)
        self.backend = BackendFacade(self)
        self.vector_backend = VectorBackendFacade(self)
        self.writeback = [
            IntWritebackBundle(f"io_mem_to_ooo_intWriteback_{idx}_0_", dut)
            for idx in range(self.config.int_writeback_ports)
        ]
        self.vector_writeback = [
            VecWritebackBundle(f"io_mem_to_ooo_vecWriteback_{idx}_0_", dut)
            for idx in range(VEC_WRITEBACK_PORTS)
        ]
        self.store_data_inputs = [
            StoreDataInputBundle(f"MemBlock_inner_lsq_io_std_storeDataIn_{idx}_", dut)
            for idx in range(self.config.store_pipeline_width)
        ]
        self.store_addr_inputs = [
            StoreAddrInputBundle(f"MemBlock_inner_lsq_io_sta_storeAddrIn_{idx}_", dut)
            for idx in range(self.config.store_pipeline_width)
        ]
        self.store_mask_inputs = [
            StoreMaskInputBundle(f"MemBlock_inner_lsq_io_sta_storeMaskIn_{idx}_", dut)
            for idx in range(self.config.store_pipeline_width)
        ]
        self.store_addr_re_inputs = [
            StoreAddrReInputBundle(f"MemBlock_inner_lsq_io_sta_storeAddrInRe_{idx}_", dut)
            for idx in range(self.config.store_pipeline_width)
        ]
        self.sbuffer_writes = [
            SbufferWriteBundle(f"MemBlock_inner_lsq_io_sbuffer_req_{idx}_", dut)
            for idx in range(self.config.sbuffer_write_ports)
        ]
        self.sq_shadow_entries = [
            StoreQueueShadowEntry(idx, dut)
            for idx in range(self.config.store_queue_size)
        ]

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

        self.memory = MemoryModel(
            dut,
            self.outer_tl_a,
            self.outer_tl_d,
            self.dcache_a,
            self.dcache_b,
            self.dcache_c,
            self.dcache_d,
            self.dcache_e,
            writebacks=self.writeback,
            store_data_inputs=self.store_data_inputs,
            store_addr_inputs=self.store_addr_inputs,
            store_mask_inputs=self.store_mask_inputs,
            store_addr_re_inputs=self.store_addr_re_inputs,
            sq_shadow_entries=self.sq_shadow_entries,
            sbuffer_writes=self.sbuffer_writes,
            outer_delay=self.config.transport.outer_delay,
            grant_delay_min=self.config.transport.grant_delay_min,
            grant_delay_max=self.config.transport.grant_delay_max,
            release_ack_delay=self.config.transport.release_ack_delay,
            delay_seed=self.config.transport.delay_seed,
            rob_size=self.config.rob_size,
            store_queue_size=self.config.store_queue_size,
            strict_writeback_check=self.config.strict_writeback_check,
        )
        self.mock_outer_buffer = self.memory
        self.mock_dcache_client = self.memory
        self.mem_status_monitor = MemStatusMonitor(self.mem_status, self.memory, self.commit_agent)
        self.vector_monitor = VectorMemMonitor(self, self.vector_writeback)
        self.backend.vector_monitor = self.vector_monitor
        self.mmu = MmuFacade(self)
        self._after_step_callbacks = []
        self._last_rar_query_req_by_lane = {}

        self.dut.StepRis(self.memory.capture_on_rise)

        self.dut.reset.value = 0
        self.dut.io_hartId.value = 0
        self.idle_inputs()

    def _validate_structural_config(self) -> None:
        expected = DEFAULT_ENV_CONFIG
        structural_fields = (
            "load_pipeline_width",
            "lsq_enq_ports",
            "int_issue_ports",
            "int_writeback_ports",
            "store_pipeline_width",
            "sbuffer_write_ports",
        )
        for field_name in structural_fields:
            if getattr(self.config, field_name) != getattr(expected, field_name):
                raise ValueError(
                    f"当前 MemBlockEnv 暂不支持动态修改 `{field_name}`，"
                    f" expected={getattr(expected, field_name)}, got={getattr(self.config, field_name)}"
                )

    def idle_inputs(self) -> None:
        """将 env 管理的输入口恢复到空闲值。"""

        self.csr_agent.reset()
        self.redirect.drive_idle()
        self.lsq_enq_meta.drive_idle()
        for bundle in self.lsq_enq_req:
            bundle.drive_idle()
        for bundle in self.issue:
            bundle.drive_idle()
        for bundle in self.vector_issue:
            bundle.drive_idle()
        if hasattr(self.dut, "io_ooo_to_mem_flushSb"):
            self.dut.io_ooo_to_mem_flushSb.value = 0
        if hasattr(self.dut, "io_ooo_to_mem_sfence_valid"):
            self.dut.io_ooo_to_mem_sfence_valid.value = 0
            self.dut.io_ooo_to_mem_sfence_bits_rs1.value = 0
            self.dut.io_ooo_to_mem_sfence_bits_rs2.value = 0
            self.dut.io_ooo_to_mem_sfence_bits_addr.value = 0
            self.dut.io_ooo_to_mem_sfence_bits_id.value = 0
            self.dut.io_ooo_to_mem_sfence_bits_hv.value = 0
            self.dut.io_ooo_to_mem_sfence_bits_hg.value = 0
        self.mmu.reapply_inputs()
        self.memory.drive_idle()
        self.vector_monitor.drive_ready()
        self.commit_agent.drive()

    def refresh_comb(self) -> None:
        """显式刷新组合逻辑，确保 drive 后的组合观测已收敛。"""

        refresh = getattr(self.dut, "RefreshComb", None)
        if refresh is not None:
            refresh()

    def _run_async(self, coro):
        return self._clock.run(coro)

    async def _step_async(self, cycles: int = 1) -> None:
        if cycles < 0:
            raise ValueError(f"cycles 必须非负，当前值为 {cycles}")
        for _ in range(cycles):
            self.commit_agent.drive()
            self.memory.drive_pre_step()
            await self._clock.step(1)
            self.memory.after_cycle()
            self.vector_monitor.sample()
            if int(self.dut.reset.value) or int(self.dut.io_reset_backend.value):
                self.commit_agent.reset()
                self.vector_monitor.reset_runtime_state()
                self.backend.reset_runtime_state()
            else:
                self.mem_status_monitor.after_cycle()
                self.commit_agent.advance()
            for callback in tuple(self._after_step_callbacks):
                result = callback()
                if inspect.isawaitable(result):
                    await result

    async def _await_cycles(self, cycles: int = 1) -> None:
        await self._step_async(cycles)

    async def _await_until(self, predicate, *, max_cycles: int, timeout_message: str):
        for _ in range(max_cycles):
            result = predicate()
            if result:
                return result
            await self._step_async(1)
        raise TimeoutError(timeout_message)

    async def _step_and_idle_async(self, cycles: int = 1) -> None:
        await self._step_async(cycles)
        self.idle_inputs()

    def advance_cycles(self, cycles: int = 1) -> None:
        """按 env 统一时钟语义推进若干周期。"""

        self._run_async(self._await_cycles(cycles))

    def wait_until(self, predicate, *, max_cycles: int = 200, timeout_message: str = "等待条件满足超时"):
        """使用 env 统一时钟语义轮询某个条件。"""

        return self._run_async(
            self._await_until(
                predicate,
                max_cycles=max_cycles,
                timeout_message=timeout_message,
            )
        )

    async def _reset_async(self, cycles: int = 10, settle_cycles: int = 5) -> None:
        self.idle_inputs()
        self.memory.reset_runtime_state()
        self.vector_monitor.reset_runtime_state()
        self.commit_agent.reset()
        self.backend.reset_runtime_state()
        self._last_rar_query_req_by_lane = {}
        self.dut.reset.value = 1
        await self._step_async(cycles)
        self.dut.reset.value = 0
        self.idle_inputs()
        await self.mmu.reapply_after_reset_async()
        await self._step_async(settle_cycles)

    def reset(self, cycles: int = 10, settle_cycles: int = 5) -> None:
        """执行同步复位，并清空 mock 内部状态。"""

        self._run_async(self._reset_async(cycles=cycles, settle_cycles=settle_cycles))

    def inject_outer_d_response(self, delay_cycles: int = 0, **kwargs) -> None:
        """向 outer buffer D 通道注入一笔响应。"""

        self.memory.enqueue_outer_response(delay_cycles=delay_cycles, **kwargs)

    def inject_dcache_b_response(self, delay_cycles: int = 0, **kwargs) -> None:
        """向 DCache B 通道注入一笔响应。"""

        self.memory.enqueue_b_response(delay_cycles=delay_cycles, **kwargs)

    def inject_dcache_probe(
        self,
        cacheline_addr: int,
        *,
        param: int = 0,
        size: int = 6,
        source: int = 0,
        delay_cycles: int = 0,
    ) -> None:
        """向 DCache B 通道注入一笔 Probe 请求。"""

        self.inject_dcache_b_response(
            delay_cycles=delay_cycles,
            opcode=TL_B_PROBE,
            param=param,
            size=size,
            source=source,
            address=int(cacheline_addr),
            mask=0,
            data=0,
            corrupt=0,
        )

    def inject_dcache_d_response(self, delay_cycles: int = 0, **kwargs) -> None:
        """向 DCache D 通道注入一笔响应。"""

        self.memory.enqueue_d_response(delay_cycles=delay_cycles, **kwargs)

    def preload_u64(self, addr: int, value: int) -> None:
        """向黄金内存预置一笔 64-bit 数据。"""

        self.memory.preload_u64(addr, value)

    def wait_backend_reset_deassert(self, *, must_observe_assert: bool, max_cycles: int = 200) -> None:
        """等待 `io_reset_backend` 先被拉高再解复位。"""

        observed_assert = not must_observe_assert

        def _backend_reset_observed():
            nonlocal observed_assert
            backend_reset = int(self.dut.io_reset_backend.value)
            if backend_reset:
                observed_assert = True
                return False
            if observed_assert:
                return True
            return False

        self.wait_until(
            _backend_reset_observed,
            max_cycles=max_cycles,
            timeout_message="等待 `io_reset_backend` 解复位超时",
        )

    def expect_scalar_load(
        self,
        *,
        addr: int,
        pdest: int | None = None,
        size: int = 8,
        mask: int = 0xFF,
        req_id: int | None = None,
        rob_idx: RobIndex | None = None,
        rob_idx_flag: int | None = None,
        rob_idx_value: int | None = None,
    ):
        """登记一笔标量 load 的期望结果。"""

        rob_idx = self._normalize_rob_idx_filter(
            rob_idx=rob_idx,
            rob_idx_flag=rob_idx_flag,
            rob_idx_value=rob_idx_value,
        )
        if rob_idx is None or pdest is None:
            detail = ""
            if req_id is not None:
                detail = f" (req_id={int(req_id)} is now debug-only and no longer implies rob_idx/pdest)"
            raise ValueError(f"expect_scalar_load requires explicit rob_idx/pdest{detail}")
        return self.memory.expect_load(
            rob_idx_flag=rob_idx.flag,
            rob_idx_value=rob_idx.value,
            pdest=pdest,
            addr=addr,
            size=size,
            mask=mask,
        )

    async def _wait_load_writeback_observed_async(
        self,
        *,
        rob_idx: RobIndex | None = None,
        rob_idx_flag: int | None = None,
        rob_idx_value: int | None = None,
        data: int | None = None,
        expected_mmio: bool | None = None,
        expected_ncio: bool | None = None,
        max_cycles: int = 200,
    ) -> dict:
        """等待某条 load writeback 在 intWriteback 口被观测到。"""

        normalized_rob_idx = self._normalize_rob_idx_filter(
            rob_idx=rob_idx,
            rob_idx_flag=rob_idx_flag,
            rob_idx_value=rob_idx_value,
        )

        for _ in range(max_cycles):
            for lane, bundle in enumerate(self.writeback):
                if not bundle.connected("valid") or bundle.read("valid", 0) == 0:
                    continue
                if bundle.connected("ready") and bundle.read("ready", 0) == 0:
                    continue
                if bundle.connected("isFromLoadUnit") and bundle.read("isFromLoadUnit", 0) == 0:
                    continue
                if bundle.connected("toRob_valid") and bundle.read("toRob_valid", 0) == 0:
                    continue

                event = {
                    "cycle": self._current_cycle(),
                    "lane": lane,
                    "rob_idx_flag": bundle.read("robIdx_flag", 0),
                    "rob_idx_value": bundle.read("robIdx_value", 0),
                    "lq_idx_flag": bundle.read("lqIdx_flag", 0),
                    "lq_idx_value": bundle.read("lqIdx_value", 0),
                    "pdest": bundle.read("pdest", 0),
                    "data": bundle.read("data_0", 0),
                    "int_wen": bundle.read("intWen", 0),
                    "debug_vaddr": bundle.read("debug_vaddr", 0) if bundle.connected("debug_vaddr") else None,
                    "debug_paddr": bundle.read("debug_paddr", 0) if bundle.connected("debug_paddr") else None,
                    "debug_is_mmio": bundle.read("debug_isMMIO", 0) if bundle.connected("debug_isMMIO") else None,
                    "debug_is_ncio": bundle.read("debug_isNCIO", 0) if bundle.connected("debug_isNCIO") else None,
                    "exception_bits": bundle.read_exception_bits() if hasattr(bundle, "read_exception_bits") else None,
                }
                if event["int_wen"] == 0:
                    continue
                if not self._event_matches_rob_idx(event, normalized_rob_idx):
                    continue
                if data is not None and event["data"] != int(data):
                    continue
                if expected_mmio is not None and bool(event["debug_is_mmio"]) != bool(expected_mmio):
                    continue
                if expected_ncio is not None and bool(event["debug_is_ncio"]) != bool(expected_ncio):
                    continue
                return event
            await self._step_async(1)

        raise TimeoutError(
            "等待 load writeback 观测超时: "
            f"rob={self._format_rob_idx(normalized_rob_idx)}, data={data}, "
            f"expected_mmio={expected_mmio}, expected_ncio={expected_ncio}"
        )

    def wait_load_writeback_observed(
        self,
        *,
        rob_idx: RobIndex | None = None,
        rob_idx_flag: int | None = None,
        rob_idx_value: int | None = None,
        data: int | None = None,
        expected_mmio: bool | None = None,
        expected_ncio: bool | None = None,
        max_cycles: int = 200,
    ) -> dict:
        """等待某条 load writeback 在 intWriteback 口被观测到。"""

        return self._run_async(
            self._wait_load_writeback_observed_async(
                rob_idx=rob_idx,
                rob_idx_flag=rob_idx_flag,
                rob_idx_value=rob_idx_value,
                data=data,
                expected_mmio=expected_mmio,
                expected_ncio=expected_ncio,
                max_cycles=max_cycles,
            )
        )

    def wait_vector_event(
        self,
        *,
        req_id: int,
        event: str = "complete_or_trap",
        max_cycles: int = 200,
    ):
        """等待某条向量请求完成或 trap。"""

        return self._run_async(
            self.vector_monitor.wait_event_async(
                req_id=req_id,
                event=event,
                max_cycles=max_cycles,
            )
        )

    def get_vector_result(self, req_id: int):
        """读取最近一次采集到的向量完成结果。"""

        return self.vector_monitor.get_result(req_id)

    def get_transport_stats(self) -> dict[str, int]:
        """返回当前传输与 compare 统计的快照。"""

        return {key: int(value) for key, value in self.memory.stats.items()}

    def get_completed_load_count(self) -> int:
        """返回已完成 compare 的 load 数量。"""

        return int(self.memory.completed_loads)

    def wait_completed_load_count(self, target_count: int, max_cycles: int = 200) -> int:
        """等待已完成 compare 的 load 数达到目标值。"""

        def _completed_load_count_matches():
            completed = self.get_completed_load_count()
            if completed >= int(target_count):
                return completed
            return None

        return self.wait_until(
            _completed_load_count_matches,
            max_cycles=max_cycles,
            timeout_message=f"等待 completed_load_count 达到 {target_count} 超时",
        )

    def get_counter(self, counter_name: str) -> int:
        """读取 env 导出的统计计数器。"""

        if hasattr(self.memory, counter_name):
            return int(getattr(self.memory, counter_name))
        if hasattr(self.commit_agent, "stats") and counter_name in self.commit_agent.stats:
            return int(self.commit_agent.stats[counter_name])
        stats = self.get_transport_stats()
        if counter_name in stats:
            return int(stats[counter_name])
        raise AttributeError(f"未知计数器 `{counter_name}`")

    def wait_mmio_busy(self, expected: bool = True, max_cycles: int = 200) -> int:
        """等待 `lsq_status.mmioBusy` 收敛到目标值。"""

        target = int(bool(expected))

        def _mmio_busy_matches():
            current = int(self.lsq_status.mmioBusy.value)
            if current == target:
                return current
            return None

        return self.wait_until(
            _mmio_busy_matches,
            max_cycles=max_cycles,
            timeout_message=f"等待 mmioBusy={target} 超时",
        )

    def _current_cycle(self) -> int:
        return int(getattr(self.memory, "_cycle", 0))

    def _normalize_rob_idx_filter(
        self,
        *,
        rob_idx: RobIndex | None = None,
        rob_idx_flag: int | None = None,
        rob_idx_value: int | None = None,
    ) -> RobIndex | None:
        return make_rob_index(
            rob_idx=rob_idx,
            rob_idx_flag=rob_idx_flag,
            rob_idx_value=rob_idx_value,
        )

    def _event_matches_rob_idx(self, event: dict, rob_idx: RobIndex | None) -> bool:
        if rob_idx is None:
            return True
        return (
            event.get("rob_idx_flag") == int(rob_idx.flag)
            and event.get("rob_idx_value") == int(rob_idx.value)
        )

    def _format_rob_idx(self, rob_idx: RobIndex | None) -> str:
        if rob_idx is None:
            return "(None,None)"
        return f"({int(rob_idx.flag)},{int(rob_idx.value)})"

    def _read_optional_dut_signal(self, signal_name: str, default: int = 0) -> int:
        return _read_signal_int(getattr(self.dut, signal_name, None), default)

    def _read_optional_internal_signal(self, signal_name: str, default: int | None = 0) -> int | None:
        getter = getattr(self.dut, "GetInternalSignal", None)
        if getter is None:
            return default
        signal = getter(signal_name)
        if signal is None:
            return default
        return int(signal.value)

    def sample_issue_accept_state(self) -> dict:
        """采样当前拍 issue lane 的 ready 与真实 load acceptance。"""

        self.refresh_comb()
        cycle = self._current_cycle()
        lanes = []
        sampled_lanes = min(int(self.config.load_pipeline_width), int(self.config.int_issue_ports))
        for idx in range(sampled_lanes):
            rar_accepted = self._read_optional_internal_signal(
                f"inner_lsq.loadQueue.loadQueueRAR.acceptedVec_{idx}",
                default=None,
            )
            raw_accepted = self._read_optional_internal_signal(
                f"inner_lsq.loadQueue.loadQueueRAW.acceptedVec_{idx}",
                default=None,
            )
            lanes.append(
                {
                    "cycle": cycle,
                    "lane": idx,
                    "ready": _read_signal_int(self.issue[idx].ready, 0),
                    "accepted": rar_accepted if rar_accepted is not None else raw_accepted,
                    "rar_accepted": rar_accepted,
                    "raw_accepted": raw_accepted,
                    "accepted_mismatch": (
                        None
                        if rar_accepted is None or raw_accepted is None
                        else int(rar_accepted != raw_accepted)
                    ),
                }
            )
        return {
            "cycle": cycle,
            "lanes": tuple(lanes),
        }

    def _normalize_replay_cause(self, cause: str | None) -> str | None:
        if cause is None:
            return None
        upper = str(cause).upper()
        if upper == "UNCACHE":
            return "NC"
        return upper

    def _decode_replay_cause_mask(self, cause_mask: int) -> str | None:
        mask = int(cause_mask)
        if mask == 0:
            return None
        for cause_idx, label in enumerate(LOAD_REPLAY_CAUSE_LABELS):
            if mask & (1 << cause_idx):
                return self._normalize_replay_cause(label)
        return None

    def _replay_event_matches(
        self,
        event: dict,
        *,
        cause: str | None = None,
        source: str | tuple[str, ...] | None = None,
        rob_idx: RobIndex | None = None,
        rob_idx_flag: int | None = None,
        rob_idx_value: int | None = None,
        sq_idx: int | None = None,
    ) -> bool:
        normalized_rob_idx = self._normalize_rob_idx_filter(
            rob_idx=rob_idx,
            rob_idx_flag=rob_idx_flag,
            rob_idx_value=rob_idx_value,
        )
        if cause is not None and self._normalize_replay_cause(event.get("cause")) != self._normalize_replay_cause(cause):
            return False
        if source is not None:
            sources = (source,) if isinstance(source, str) else tuple(source)
            if event.get("source") not in sources:
                return False
        if not self._event_matches_rob_idx(event, normalized_rob_idx):
            return False
        if sq_idx is not None and event.get("sq_idx_value") != int(sq_idx):
            return False
        return True

    def sample_replay_state(self) -> dict:
        """采样当前拍 replay 相关真实 DUT 状态。"""

        cycle = self._current_cycle()
        replay_lanes = []
        ldu_lanes = []
        nc_out_lanes = []
        replay_queue_entries = []
        events = []

        replay_queue_size = int(getattr(self.config.sequence, "load_queue_size", 72))
        replay_queue_by_index = {}
        for idx in range(replay_queue_size):
            allocated = self._read_optional_dut_signal(f"MemBlock_inner_lsq_loadQueue_loadQueueReplay_allocated_{idx}")
            if not allocated:
                continue
            replay_queue_entry = {
                "cycle": cycle,
                "source": "replay_queue",
                "lane": None,
                "sched_index": idx,
                "rob_idx_flag": self._read_optional_dut_signal(
                    f"MemBlock_inner_lsq_loadQueue_loadQueueReplay_uop_{idx}_robIdx_flag"
                ),
                "rob_idx_value": self._read_optional_dut_signal(
                    f"MemBlock_inner_lsq_loadQueue_loadQueueReplay_uop_{idx}_robIdx_value"
                ),
                "lq_idx_flag": self._read_optional_dut_signal(
                    f"MemBlock_inner_lsq_loadQueue_loadQueueReplay_uop_{idx}_lqIdx_flag"
                ),
                "lq_idx_value": self._read_optional_dut_signal(
                    f"MemBlock_inner_lsq_loadQueue_loadQueueReplay_uop_{idx}_lqIdx_value"
                ),
                "sq_idx_flag": self._read_optional_dut_signal(
                    f"MemBlock_inner_lsq_loadQueue_loadQueueReplay_uop_{idx}_sqIdx_flag"
                ),
                "sq_idx_value": self._read_optional_dut_signal(
                    f"MemBlock_inner_lsq_loadQueue_loadQueueReplay_uop_{idx}_sqIdx_value"
                ),
                "vaddr": self._read_optional_dut_signal(f"MemBlock_inner_lsq_loadQueue_loadQueueReplay_debug_vaddr_{idx}"),
                "paddr": None,
                "cause": self._decode_replay_cause_mask(
                    self._read_optional_dut_signal(f"MemBlock_inner_lsq_loadQueue_loadQueueReplay_cause_{idx}")
                ),
            }
            replay_queue_entries.append(replay_queue_entry)
            replay_queue_by_index[idx] = replay_queue_entry
            events.append(replay_queue_entry)

        for idx in range(self.config.load_pipeline_width):
            replay_prefix = f"MemBlock_inner_lsq_io_replay_{idx}"
            replay_valid = self._read_optional_dut_signal(f"{replay_prefix}_valid")
            replay_ready = self._read_optional_dut_signal(f"{replay_prefix}_ready")
            if replay_valid:
                replay_queue_idx = self._read_optional_dut_signal(f"{replay_prefix}_bits_replayQueueIdx", -1)
                replay_queue_entry = replay_queue_by_index.get(replay_queue_idx)
                replay_lane = {
                    "cycle": cycle,
                    "source": "replay_lane",
                    "lane": idx,
                    "valid": replay_valid,
                    "ready": replay_ready,
                    "sched_index": replay_queue_idx,
                    "rob_idx_flag": self._read_optional_dut_signal(f"{replay_prefix}_bits_uop_robIdx_flag"),
                    "rob_idx_value": self._read_optional_dut_signal(f"{replay_prefix}_bits_uop_robIdx_value"),
                    "lq_idx_flag": self._read_optional_dut_signal(f"{replay_prefix}_bits_uop_lqIdx_flag"),
                    "lq_idx_value": self._read_optional_dut_signal(f"{replay_prefix}_bits_uop_lqIdx_value"),
                    "sq_idx_flag": self._read_optional_dut_signal(f"{replay_prefix}_bits_uop_sqIdx_flag"),
                    "sq_idx_value": self._read_optional_dut_signal(f"{replay_prefix}_bits_uop_sqIdx_value"),
                    "vaddr": self._read_optional_dut_signal(f"{replay_prefix}_bits_vaddr"),
                    "paddr": None,
                    "cause": (
                        "NC"
                        if self._read_optional_dut_signal(f"{replay_prefix}_bits_ncReplay")
                        or self._read_optional_dut_signal(f"{replay_prefix}_bits_uncacheReplay")
                        else "DM"
                        if self._read_optional_dut_signal(f"{replay_prefix}_bits_forwardDChannel")
                        else "TM"
                        if self._read_optional_dut_signal(f"{replay_prefix}_bits_tlbMiss")
                        else None if replay_queue_entry is None else replay_queue_entry.get("cause")
                    ),
                }
                replay_lanes.append(replay_lane)
                events.append(replay_lane)

            ldu_prefix = f"MemBlock_inner_lsq_io_ldu_ldin_{idx}"
            ldu_valid = self._read_optional_dut_signal(f"{ldu_prefix}_valid")
            if ldu_valid and self._read_optional_dut_signal(f"{ldu_prefix}_bits_isLoadReplay"):
                cause = None
                for cause_idx, label in enumerate(LOAD_REPLAY_CAUSE_LABELS):
                    if self._read_optional_dut_signal(f"{ldu_prefix}_bits_rep_info_cause_{cause_idx}"):
                        cause = self._normalize_replay_cause(label)
                        break
                ldu_lane = {
                    "cycle": cycle,
                    "source": "ldu",
                    "lane": idx,
                    "valid": ldu_valid,
                    "sched_index": self._read_optional_dut_signal(f"{ldu_prefix}_bits_schedIndex", -1),
                    "rob_idx_flag": self._read_optional_dut_signal(f"{ldu_prefix}_bits_uop_robIdx_flag"),
                    "rob_idx_value": self._read_optional_dut_signal(f"{ldu_prefix}_bits_uop_robIdx_value"),
                    "lq_idx_flag": self._read_optional_dut_signal(f"{ldu_prefix}_bits_uop_lqIdx_flag"),
                    "lq_idx_value": self._read_optional_dut_signal(f"{ldu_prefix}_bits_uop_lqIdx_value"),
                    "sq_idx_flag": self._read_optional_dut_signal(f"{ldu_prefix}_bits_uop_sqIdx_flag"),
                    "sq_idx_value": self._read_optional_dut_signal(f"{ldu_prefix}_bits_uop_sqIdx_value"),
                    "vaddr": self._read_optional_dut_signal(f"{ldu_prefix}_bits_fullva"),
                    "paddr": self._read_optional_dut_signal(f"{ldu_prefix}_bits_paddr"),
                    "cause": cause,
                }
                ldu_lanes.append(ldu_lane)
                events.append(ldu_lane)

            nc_prefix = f"MemBlock_inner_lsq_io_ncOut_{idx}"
            nc_valid = self._read_optional_dut_signal(f"{nc_prefix}_valid")
            if nc_valid:
                nc_lane = {
                    "cycle": cycle,
                    "source": "nc_out",
                    "lane": idx,
                    "valid": nc_valid,
                    "ready": self._read_optional_dut_signal(f"{nc_prefix}_ready"),
                    "sched_index": self._read_optional_dut_signal(f"{nc_prefix}_bits_schedIndex", -1),
                    "rob_idx_flag": self._read_optional_dut_signal(f"{nc_prefix}_bits_uop_robIdx_flag"),
                    "rob_idx_value": self._read_optional_dut_signal(f"{nc_prefix}_bits_uop_robIdx_value"),
                    "lq_idx_flag": self._read_optional_dut_signal(f"{nc_prefix}_bits_uop_lqIdx_flag"),
                    "lq_idx_value": self._read_optional_dut_signal(f"{nc_prefix}_bits_uop_lqIdx_value"),
                    "sq_idx_flag": self._read_optional_dut_signal(f"{nc_prefix}_bits_uop_sqIdx_flag"),
                    "sq_idx_value": self._read_optional_dut_signal(f"{nc_prefix}_bits_uop_sqIdx_value"),
                    "vaddr": self._read_optional_dut_signal(f"{nc_prefix}_bits_vaddr"),
                    "paddr": self._read_optional_dut_signal(f"{nc_prefix}_bits_paddr"),
                    "cause": "NC",
                }
                nc_out_lanes.append(nc_lane)
                events.append(nc_lane)

        memory_violation = {
            "cycle": cycle,
            "source": "memory_violation",
            "valid": _read_signal_int(self.mem_status.memoryViolation_valid, 0),
            "level": _read_signal_int(self.mem_status.memoryViolation_bits_level, 0),
            "rob_idx_flag": _read_signal_int(self.mem_status.memoryViolation_bits_robIdx_flag, 0),
            "rob_idx_value": _read_signal_int(self.mem_status.memoryViolation_bits_robIdx_value, 0),
            "target": _read_signal_int(self.mem_status.memoryViolation_bits_target, 0),
            "cause": "VIOLATION",
        }
        if memory_violation["valid"]:
            events.append(memory_violation)

        return {
            "cycle": cycle,
            "replay_lanes": replay_lanes,
            "ldu_lanes": ldu_lanes,
            "nc_out_lanes": nc_out_lanes,
            "replay_queue_entries": replay_queue_entries,
            "memory_violation": memory_violation,
            "events": events,
        }

    def sample_load_debug_state(self) -> dict:
        """采样当前拍 load pipeline `debugLsInfo` 白盒调试口。"""

        cycle = self._current_cycle()
        lanes = []

        for idx in range(self.config.load_pipeline_width):
            prefix = f"io_debug_ls_debugLsInfo_{idx}"
            replay_causes = []
            replay_cause_mask = 0
            for cause_idx, label in enumerate(LOAD_REPLAY_CAUSE_LABELS):
                signal = getattr(self.dut, f"{prefix}_replayCause_{cause_idx}", None)
                if signal is None:
                    continue
                if _read_signal_int(signal, 0):
                    replay_cause_mask |= 1 << cause_idx
                    replay_causes.append(self._normalize_replay_cause(label))

            lanes.append(
                {
                    "cycle": cycle,
                    "lane": idx,
                    "s1_rob_idx_value": self._read_optional_dut_signal(f"{prefix}_s1_robIdx", -1),
                    "s2_rob_idx_value": self._read_optional_dut_signal(f"{prefix}_s2_robIdx", -1),
                    "s3_rob_idx_value": self._read_optional_dut_signal(f"{prefix}_s3_robIdx", -1),
                    "s2_is_bank_conflict": self._read_optional_dut_signal(f"{prefix}_s2_isBankConflict", 0),
                    "s2_is_forward_fail": self._read_optional_dut_signal(f"{prefix}_s2_isForwardFail", 0),
                    "s3_is_replay_fast": self._read_optional_dut_signal(f"{prefix}_s3_isReplayFast", 0),
                    "s3_is_replay_slow": self._read_optional_dut_signal(f"{prefix}_s3_isReplaySlow", 0),
                    "s3_is_replay": self._read_optional_dut_signal(f"{prefix}_s3_isReplay", 0),
                    "replay_cause_mask": replay_cause_mask,
                    "replay_causes": tuple(replay_causes),
                }
            )

        return {
            "cycle": cycle,
            "lanes": tuple(lanes),
        }

    def sample_load_issue_state(self) -> dict:
        """采样当前拍进入 load pipeline 的 `io_ldu_ldin_*` 输入。"""

        cycle = self._current_cycle()
        lanes = []
        for idx in range(self.config.load_pipeline_width):
            prefix = f"MemBlock_inner_lsq_io_ldu_ldin_{idx}"
            if not self._read_optional_dut_signal(f"{prefix}_valid", 0):
                continue
            lanes.append(
                {
                    "cycle": cycle,
                    "lane": idx,
                    "valid": 1,
                    "is_load_replay": self._read_optional_dut_signal(f"{prefix}_bits_isLoadReplay", 0),
                    "rob_idx_flag": self._read_optional_dut_signal(f"{prefix}_bits_uop_robIdx_flag", None),
                    "rob_idx_value": self._read_optional_dut_signal(f"{prefix}_bits_uop_robIdx_value", None),
                    "lq_idx_flag": self._read_optional_dut_signal(f"{prefix}_bits_uop_lqIdx_flag", None),
                    "lq_idx_value": self._read_optional_dut_signal(f"{prefix}_bits_uop_lqIdx_value", None),
                    "sq_idx_flag": self._read_optional_dut_signal(f"{prefix}_bits_uop_sqIdx_flag", None),
                    "sq_idx_value": self._read_optional_dut_signal(f"{prefix}_bits_uop_sqIdx_value", None),
                    "sched_index": self._read_optional_dut_signal(f"{prefix}_bits_schedIndex", -1),
                    "vaddr": self._read_optional_dut_signal(f"{prefix}_bits_fullva", None),
                    "paddr": self._read_optional_dut_signal(f"{prefix}_bits_paddr", None),
                }
            )
        return {
            "cycle": cycle,
            "lanes": tuple(lanes),
        }

    def sample_nuke_query_state(self) -> dict:
        """采样当前拍 RAW/RAR nuke query 的请求握手状态。"""

        cycle = self._current_cycle()
        raw_queries = []
        rar_queries = []
        rar_responses = []

        for kind, bucket in (("raw", raw_queries), ("rar", rar_queries)):
            for idx in range(self.config.load_pipeline_width):
                prefix = f"MemBlock_inner_lsq_io_ldu_{kind}NukeQuery_{idx}_req"
                valid = self._read_optional_dut_signal(f"{prefix}_valid")
                if not valid:
                    continue
                bucket.append(
                    {
                        "cycle": cycle,
                        "kind": kind.upper(),
                        "source": f"{kind}_nuke_query",
                        "lane": idx,
                        "valid": valid,
                        "ready": self._read_optional_dut_signal(f"{prefix}_ready"),
                        "rob_idx_flag": self._read_optional_dut_signal(f"{prefix}_bits_robIdx_flag"),
                        "rob_idx_value": self._read_optional_dut_signal(f"{prefix}_bits_robIdx_value"),
                        "lq_idx_flag": self._read_optional_dut_signal(f"{prefix}_bits_lqIdx_flag"),
                        "lq_idx_value": self._read_optional_dut_signal(f"{prefix}_bits_lqIdx_value"),
                        "sq_idx_flag": self._read_optional_dut_signal(f"{prefix}_bits_sqIdx_flag"),
                        "sq_idx_value": self._read_optional_dut_signal(f"{prefix}_bits_sqIdx_value"),
                        "paddr": self._read_optional_dut_signal(f"{prefix}_bits_paddr"),
                        "data_valid": self._read_optional_dut_signal(f"{prefix}_bits_dataValid"),
                        "nc": self._read_optional_dut_signal(f"{prefix}_bits_nc"),
                    }
                )
                if kind != "rar":
                    continue
                self._last_rar_query_req_by_lane[idx] = dict(bucket[-1])
                resp_prefix = f"MemBlock_inner_lsq_io_ldu_{kind}NukeQuery_{idx}_resp"
                rar_responses.append(
                    {
                        "cycle": cycle,
                        "kind": "RAR",
                        "source": "rar_nuke_response",
                        "lane": idx,
                        "resp_valid": self._read_optional_dut_signal(f"{resp_prefix}_valid"),
                        "nuke": self._read_optional_dut_signal(f"{resp_prefix}_bits_nuke"),
                    }
                )
            if kind == "rar":
                for idx in range(self.config.load_pipeline_width):
                    if idx < len(rar_responses):
                        continue
                    resp_prefix = f"MemBlock_inner_lsq_io_ldu_{kind}NukeQuery_{idx}_resp"
                    rar_responses.append(
                        {
                            "cycle": cycle,
                            "kind": "RAR",
                            "source": "rar_nuke_response",
                            "lane": idx,
                            "resp_valid": self._read_optional_dut_signal(f"{resp_prefix}_valid"),
                            "nuke": self._read_optional_dut_signal(f"{resp_prefix}_bits_nuke"),
                        }
                    )

        return {
            "cycle": cycle,
            "raw_queries": raw_queries,
            "rar_queries": rar_queries,
            "rar_responses": rar_responses,
        }

    def sample_release_state(self) -> dict:
        """采样当前拍 LSQ release 提示。"""

        return {
            "cycle": self._current_cycle(),
            "source": "release",
            "valid": self._read_optional_dut_signal("MemBlock_inner_lsq_io_release_valid"),
            "paddr": self._read_optional_dut_signal("MemBlock_inner_lsq_io_release_bits_paddr"),
        }

    async def _wait_replay_event_async(
        self,
        *,
        cause: str | None = None,
        source: str | tuple[str, ...] | None = None,
        rob_idx: RobIndex | None = None,
        rob_idx_flag: int | None = None,
        rob_idx_value: int | None = None,
        sq_idx: int | None = None,
        max_cycles: int = 200,
    ) -> dict:
        """等待一条匹配过滤条件的 replay 观测事件。"""

        normalized_rob_idx = self._normalize_rob_idx_filter(
            rob_idx=rob_idx,
            rob_idx_flag=rob_idx_flag,
            rob_idx_value=rob_idx_value,
        )

        for _ in range(max_cycles):
            replay_state = self.sample_replay_state()
            for event in replay_state["events"]:
                if self._replay_event_matches(
                    event,
                    cause=cause,
                    source=source,
                    rob_idx=normalized_rob_idx,
                    sq_idx=sq_idx,
                ):
                    return event
            await self._step_async(1)
        raise TimeoutError(
            "等待 replay 事件超时: "
            f"cause={cause}, source={source}, rob={self._format_rob_idx(normalized_rob_idx)}, sq_idx={sq_idx}"
        )

    def wait_replay_event(
        self,
        *,
        cause: str | None = None,
        source: str | tuple[str, ...] | None = None,
        rob_idx: RobIndex | None = None,
        rob_idx_flag: int | None = None,
        rob_idx_value: int | None = None,
        sq_idx: int | None = None,
        max_cycles: int = 200,
    ) -> dict:
        """等待一条匹配过滤条件的 replay 观测事件。"""

        return self._run_async(
            self._wait_replay_event_async(
                cause=cause,
                source=source,
                rob_idx=rob_idx,
                rob_idx_flag=rob_idx_flag,
                rob_idx_value=rob_idx_value,
                sq_idx=sq_idx,
                max_cycles=max_cycles,
            )
        )

    def wait_nc_replay_or_nc_out(
        self,
        *,
        rob_idx: RobIndex | None = None,
        rob_idx_flag: int | None = None,
        rob_idx_value: int | None = None,
        max_cycles: int = 200,
    ) -> dict:
        """等待 NC/uncache replay 或 `nc_out` 可见事件。"""

        return self.wait_replay_event(
            cause="NC",
            rob_idx=rob_idx,
            rob_idx_flag=rob_idx_flag,
            rob_idx_value=rob_idx_value,
            max_cycles=max_cycles,
        )

    async def _wait_release_event_async(
        self,
        *,
        paddr: int | None = None,
        cacheline_addr: int | None = None,
        line_bytes: int = 64,
        max_cycles: int = 200,
    ) -> dict:
        """等待 LSQ release 事件，可按 paddr 或 cacheline 过滤。"""

        normalized_paddr = None if paddr is None else int(paddr)
        normalized_line = None if cacheline_addr is None else int(cacheline_addr) & ~(int(line_bytes) - 1)

        for _ in range(max_cycles):
            event = self.sample_release_state()
            if not event["valid"]:
                await self._step_async(1)
                continue
            event_paddr = int(event["paddr"])
            if normalized_paddr is not None and event_paddr != normalized_paddr:
                await self._step_async(1)
                continue
            if normalized_line is not None and (event_paddr & ~(int(line_bytes) - 1)) != normalized_line:
                await self._step_async(1)
                continue
            return event
        detail = (
            f"paddr={normalized_paddr}, cacheline=0x{normalized_line:x}"
            if normalized_line is not None
            else f"paddr={normalized_paddr}"
        )
        raise TimeoutError(
            "等待 release 事件超时: "
            f"{detail}"
        )

    def wait_release_event(
        self,
        *,
        paddr: int | None = None,
        cacheline_addr: int | None = None,
        line_bytes: int = 64,
        max_cycles: int = 200,
    ) -> dict:
        """等待 LSQ release 事件，可按 paddr 或 cacheline 过滤。"""

        return self._run_async(
            self._wait_release_event_async(
                paddr=paddr,
                cacheline_addr=cacheline_addr,
                line_bytes=line_bytes,
                max_cycles=max_cycles,
            )
        )

    async def _wait_nuke_query_backpressure_async(
        self,
        *,
        kind: str,
        rob_idx: RobIndex | None = None,
        rob_idx_flag: int | None = None,
        rob_idx_value: int | None = None,
        sq_idx: int | None = None,
        max_cycles: int = 200,
    ) -> dict:
        """等待 RAW/RAR nuke query 出现 `valid && !ready`。"""

        normalized_kind = str(kind).strip().lower()
        if normalized_kind not in {"raw", "rar"}:
            raise ValueError(f"unsupported nuke query kind: {kind}")
        normalized_rob_idx = self._normalize_rob_idx_filter(
            rob_idx=rob_idx,
            rob_idx_flag=rob_idx_flag,
            rob_idx_value=rob_idx_value,
        )

        for _ in range(max_cycles):
            query_state = self.sample_nuke_query_state()
            queries = query_state["raw_queries"] if normalized_kind == "raw" else query_state["rar_queries"]
            for event in queries:
                if not event["valid"] or event["ready"]:
                    continue
                if not self._event_matches_rob_idx(event, normalized_rob_idx):
                    continue
                if sq_idx is not None and event["sq_idx_value"] != int(sq_idx):
                    continue
                return event
            await self._step_async(1)

        raise TimeoutError(
            "等待 nuke query backpressure 超时: "
            f"kind={normalized_kind}, rob={self._format_rob_idx(normalized_rob_idx)}, sq_idx={sq_idx}"
        )

    def wait_nuke_query_backpressure(
        self,
        *,
        kind: str,
        rob_idx: RobIndex | None = None,
        rob_idx_flag: int | None = None,
        rob_idx_value: int | None = None,
        sq_idx: int | None = None,
        max_cycles: int = 200,
    ) -> dict:
        """等待 RAW/RAR nuke query 出现 `valid && !ready`。"""

        return self._run_async(
            self._wait_nuke_query_backpressure_async(
                kind=kind,
                rob_idx=rob_idx,
                rob_idx_flag=rob_idx_flag,
                rob_idx_value=rob_idx_value,
                sq_idx=sq_idx,
                max_cycles=max_cycles,
            )
        )

    async def _wait_rar_nuke_response_async(
        self,
        *,
        rob_idx: RobIndex | None = None,
        rob_idx_flag: int | None = None,
        rob_idx_value: int | None = None,
        lq_idx_flag: int | None = None,
        lq_idx_value: int | None = None,
        max_cycles: int = 200,
    ) -> dict:
        """等待一条 RAR nuke response，并回填最近一次同 lane req 元信息。"""

        normalized_rob_idx = self._normalize_rob_idx_filter(
            rob_idx=rob_idx,
            rob_idx_flag=rob_idx_flag,
            rob_idx_value=rob_idx_value,
        )

        for _ in range(max_cycles):
            query_state = self.sample_nuke_query_state()

            for resp in query_state["rar_responses"]:
                if not resp["resp_valid"] or not resp["nuke"]:
                    continue
                event = dict(resp)
                event.update(self._last_rar_query_req_by_lane.get(resp["lane"], {}))
                event["resp_valid"] = resp["resp_valid"]
                event["nuke"] = resp["nuke"]
                if not self._event_matches_rob_idx(event, normalized_rob_idx):
                    continue
                if lq_idx_flag is not None and event.get("lq_idx_flag") != int(lq_idx_flag):
                    continue
                if lq_idx_value is not None and event.get("lq_idx_value") != int(lq_idx_value):
                    continue
                return event
            await self._step_async(1)

        raise TimeoutError(
            "等待 RAR nuke response 超时: "
            f"rob={self._format_rob_idx(normalized_rob_idx)}, lq=({lq_idx_flag},{lq_idx_value})"
        )

    def wait_rar_nuke_response(
        self,
        *,
        rob_idx: RobIndex | None = None,
        rob_idx_flag: int | None = None,
        rob_idx_value: int | None = None,
        lq_idx_flag: int | None = None,
        lq_idx_value: int | None = None,
        max_cycles: int = 200,
    ) -> dict:
        """等待一条 RAR nuke response，并回填最近一次同 lane req 元信息。"""

        return self._run_async(
            self._wait_rar_nuke_response_async(
                rob_idx=rob_idx,
                rob_idx_flag=rob_idx_flag,
                rob_idx_value=rob_idx_value,
                lq_idx_flag=lq_idx_flag,
                lq_idx_value=lq_idx_value,
                max_cycles=max_cycles,
            )
        )

    async def _collect_replay_window_async(
        self,
        cycles: int,
        *,
        cause: str | None = None,
        source: str | tuple[str, ...] | None = None,
        rob_idx: RobIndex | None = None,
        rob_idx_flag: int | None = None,
        rob_idx_value: int | None = None,
        sq_idx: int | None = None,
    ) -> list[dict]:
        """在一个时间窗口内收集 replay 观测事件。"""

        normalized_rob_idx = self._normalize_rob_idx_filter(
            rob_idx=rob_idx,
            rob_idx_flag=rob_idx_flag,
            rob_idx_value=rob_idx_value,
        )
        events = []
        seen = set()
        for step_idx in range(max(0, int(cycles))):
            replay_state = self.sample_replay_state()
            for event in replay_state["events"]:
                if not self._replay_event_matches(
                    event,
                    cause=cause,
                    source=source,
                    rob_idx=normalized_rob_idx,
                    sq_idx=sq_idx,
                ):
                    continue
                event_key = (
                    event.get("cycle"),
                    event.get("source"),
                    event.get("lane"),
                    event.get("sched_index"),
                    event.get("cause"),
                    event.get("rob_idx_flag"),
                    event.get("rob_idx_value"),
                    event.get("sq_idx_value"),
                )
                if event_key in seen:
                    continue
                seen.add(event_key)
                events.append(event)
            if step_idx != cycles - 1:
                await self._step_async(1)
        return events

    def collect_replay_window(
        self,
        cycles: int,
        *,
        cause: str | None = None,
        source: str | tuple[str, ...] | None = None,
        rob_idx: RobIndex | None = None,
        rob_idx_flag: int | None = None,
        rob_idx_value: int | None = None,
        sq_idx: int | None = None,
    ) -> list[dict]:
        """在一个时间窗口内收集 replay 观测事件。"""

        return self._run_async(
            self._collect_replay_window_async(
                cycles=cycles,
                cause=cause,
                source=source,
                rob_idx=rob_idx,
                rob_idx_flag=rob_idx_flag,
                rob_idx_value=rob_idx_value,
                sq_idx=sq_idx,
            )
        )

    def get_store_view(self, sq_idx: int) -> StoreView | None:
        """返回某个 SQ slot 的只读 store 视图。"""

        store = self.memory.pending_stores.get(int(sq_idx))
        if store is None:
            return None
        rob_idx_flag = None if store.rob_idx is None else int(store.rob_idx.flag)
        rob_idx_value = None if store.rob_idx is None else int(store.rob_idx.value)
        return StoreView(
            sq_idx=int(store.sq_idx),
            allocated=bool(store.allocated),
            addr=None if store.addr is None else int(store.addr),
            data=None if store.data is None else int(store.data),
            mask=int(store.mask),
            committed=bool(store.committed),
            completed=bool(store.completed),
            mmio=bool(store.mmio),
            nc=bool(store.nc),
            mem_back_type_mm=bool(store.mem_back_type_mm),
            has_exception=bool(store.has_exception),
            retired=bool(store.retired),
            rob_idx_flag=rob_idx_flag,
            rob_idx_value=rob_idx_value,
        )

    def wait_store_addr_observed(self, sq_idx: int, expected_addr: int, max_cycles: int = 200):
        """等待某个 store shadow 的地址字段收敛。"""

        def _store_addr_matches():
            store = self.get_store_view(sq_idx)
            if store is not None and store.allocated and store.addr == int(expected_addr):
                return store
            return None

        return self.wait_until(
            _store_addr_matches,
            max_cycles=max_cycles,
            timeout_message=f"等待 store 地址收敛超时: sqIdx={sq_idx}, addr=0x{int(expected_addr):x}",
        )

    async def _wait_store_materialized_async(
        self,
        sq_idx: int,
        *,
        expected_addr: int,
        expected_data: int,
        expected_mmio: bool | None = None,
        expected_nc: bool | None = None,
        require_committed: bool = False,
        max_cycles: int = 200,
    ) -> StoreView:
        """等待某个 SQ slot 的 store shadow 收敛。"""

        for _ in range(max_cycles):
            store = self.get_store_view(sq_idx)
            if (
                store is not None
                and store.allocated
                and store.addr == expected_addr
                and store.data is not None
                and (store.data & ((1 << 64) - 1)) == (expected_data & ((1 << 64) - 1))
                and store.mask != 0
                and (expected_mmio is None or store.mmio == expected_mmio)
                and (expected_nc is None or store.nc == expected_nc)
                and (not require_committed or store.committed)
            ):
                return store
            await self._step_async(1)

        store = self.get_store_view(sq_idx)
        raise AssertionError(
            "等待 pending store shadow 超时: "
            f"sqIdx={sq_idx}, store={store}"
        )

    def wait_store_materialized(
        self,
        sq_idx: int,
        *,
        expected_addr: int,
        expected_data: int,
        expected_mmio: bool | None = None,
        expected_nc: bool | None = None,
        require_committed: bool = False,
        max_cycles: int = 200,
    ) -> StoreView:
        """等待某个 SQ slot 的 store shadow 收敛。"""

        return self._run_async(
            self._wait_store_materialized_async(
                sq_idx=sq_idx,
                expected_addr=expected_addr,
                expected_data=expected_data,
                expected_mmio=expected_mmio,
                expected_nc=expected_nc,
                require_committed=require_committed,
                max_cycles=max_cycles,
            )
        )

    async def _wait_counter_growth_async(self, counter_name: str, baseline: int, max_cycles: int = 200) -> int:
        """等待某个计数器增长。"""

        for _ in range(max_cycles):
            current = self.get_counter(counter_name)
            if current > baseline:
                return current
            await self._step_async(1)
        raise TimeoutError(f"等待 `{counter_name}` 增长超时")

    def wait_counter_growth(self, counter_name: str, baseline: int, max_cycles: int = 200) -> int:
        """等待某个计数器增长。"""

        return self._run_async(
            self._wait_counter_growth_async(
                counter_name=counter_name,
                baseline=baseline,
                max_cycles=max_cycles,
            )
        )

    async def _wait_memory_quiesce_async(self, max_cycles: int = 200) -> None:
        """等待 MemoryModel 事务收敛。"""

        for _ in range(max_cycles):
            if self.memory.outstanding_transaction_count == 0:
                return
            await self._step_async(1)
        raise TimeoutError("等待 MemoryModel 事务收敛超时")

    def wait_memory_quiesce(self, max_cycles: int = 200) -> None:
        """等待 MemoryModel 事务收敛。"""

        self._run_async(self._wait_memory_quiesce_async(max_cycles=max_cycles))

    async def _drain_writebacks_async(self, max_cycles: int = 200) -> None:
        """推进若干周期直到待校验 writeback 全部收敛。"""

        for _ in range(max_cycles):
            if self.memory.outstanding_expected_count == 0 and self.memory.outstanding_transaction_count == 0:
                return
            await self._step_async(1)
        raise TimeoutError("等待 MemoryModel 收敛超时")

    def drain_writebacks(self, max_cycles: int = 200) -> None:
        """推进若干周期直到待校验 writeback 全部收敛。"""

        self._run_async(self._drain_writebacks_async(max_cycles=max_cycles))

    async def _flush_store_buffers_and_wait_impl_async(
        self,
        max_cycles: int = 200,
        settle_cycles: int = 4,
    ) -> dict:
        """触发 `sfence + flushSb`，并等待 sbuffer/uncache drain 结束。"""

        if not hasattr(self.dut, "io_ooo_to_mem_flushSb"):
            raise AttributeError("当前 DUT 未导出 `io_ooo_to_mem_flushSb`")

        if hasattr(self.dut, "io_ooo_to_mem_sfence_valid"):
            self.dut.io_ooo_to_mem_sfence_valid.value = 1
            self.dut.io_ooo_to_mem_sfence_bits_rs1.value = 0
            self.dut.io_ooo_to_mem_sfence_bits_rs2.value = 0
            self.dut.io_ooo_to_mem_sfence_bits_addr.value = 0
            self.dut.io_ooo_to_mem_sfence_bits_id.value = 0
            self.dut.io_ooo_to_mem_sfence_bits_hv.value = 0
            self.dut.io_ooo_to_mem_sfence_bits_hg.value = 0
        self.dut.io_ooo_to_mem_flushSb.value = 1
        await self._step_async(1)
        if hasattr(self.dut, "io_ooo_to_mem_sfence_valid"):
            self.dut.io_ooo_to_mem_sfence_valid.value = 0
        self.dut.io_ooo_to_mem_flushSb.value = 0

        for _ in range(max_cycles):
            if int(self.mem_status.sbIsEmpty.value):
                await self._step_async(settle_cycles)
                return self.memory.finalize_and_check_drain()
            await self._step_async(1)
        raise TimeoutError("等待 sbuffer drain 结束超时")

    def _flush_store_buffers_and_wait_impl(self, max_cycles: int = 200, settle_cycles: int = 4) -> dict:
        return self._run_async(
            self._flush_store_buffers_and_wait_impl_async(
                max_cycles=max_cycles,
                settle_cycles=settle_cycles,
            )
        )

    def flush_store_buffers_and_wait(self, max_cycles: int = 200, settle_cycles: int = 4) -> dict:
        """公开的 backend facade 排空入口。"""

        return self.backend.flush_store_buffers_and_wait(
            max_cycles=max_cycles,
            settle_cycles=settle_cycles,
        )

    def check_no_outstanding_transactions(self) -> None:
        """检查没有残留的 MemoryModel 事务与待校验项。"""

        assert self.memory.outstanding_expected_count == 0, "仍有未校验的 load writeback"
        assert self.memory.outstanding_transaction_count == 0, "仍有未完成的 TL 事务"

    def assert_no_outstanding(self) -> None:
        """公开的无残留事务断言。"""

        self.check_no_outstanding_transactions()

    def clear_callbacks(self) -> None:
        """移除 env 注册的 StepRis 回调。"""

        self.dut.xclock.RemoveStepRisCbByDesc(self.memory.on_memory_edge.__name__)

    def add_after_step_callback(self, callback) -> None:
        """注册在 env 每次推进一拍结束后执行的 Python 回调。"""

        if callback not in self._after_step_callbacks:
            self._after_step_callbacks.append(callback)

    def remove_after_step_callback(self, callback) -> None:
        """移除先前注册的 after-step 回调。"""

        self._after_step_callbacks = [cb for cb in self._after_step_callbacks if cb != callback]

    def Finish(self) -> None:
        """释放 DUT 资源。"""

        self.close()
        self.dut.Finish()

    def close(self) -> None:
        """释放 env 内部时钟内核资源。"""

        self._clock.close()


@pytest.fixture(scope="function")
def env(request, dut):
    """MemBlockEnv fixture。"""

    _env = MemBlockEnv(dut)
    _env.rob_coverage = RobCoverageCollector(_env)
    _env.add_after_step_callback(_env.rob_coverage.sample)
    try:
        yield _env
    finally:
        _env.remove_after_step_callback(_env.rob_coverage.sample)
        _env.rob_coverage.finalize()
        _env.close()
        set_func_coverage(request, list(_env.rob_coverage.all_groups()))
