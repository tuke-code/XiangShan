# coding=utf-8
"""
MemBlock 统一内存模型。

当前 `MemoryModel` 主要承担顶层组装职责：
  1. 组合 `RefMemory`
  2. 组合 `TransportResponder`
  3. 组合 `Scoreboard`
  4. 对外保留兼容 facade
"""

from __future__ import annotations

from monitors.store_monitor import StoreMonitor
from monitors.writeback_monitor import WritebackMonitor
from model.ref_memory import RefMemory
from model.scoreboard import (
    ExpectedLoad,
    PendingStore,
    RobIndex,
    Scoreboard,
)
from model.transport_responder import TransportResponder
from model.vector_memory_model import VectorMemoryModel


TL_A_PUT_FULL = 0
TL_A_PUT_PARTIAL = 1
TL_A_GET = 4
TL_A_ACQUIRE_BLOCK = 6
TL_A_ACQUIRE_PERM = 7

TL_C_PROBE_ACK = 4
TL_C_PROBE_ACK_DATA = 5
TL_C_RELEASE = 6
TL_C_RELEASE_DATA = 7

TL_D_ACCESS_ACK = 0
TL_D_ACCESS_ACK_DATA = 1
TL_D_GRANT = 4
TL_D_GRANT_DATA = 5
TL_D_RELEASE_ACK = 6

DEFAULT_CACHELINE_BYTES = 64
DEFAULT_DCACHE_BEAT_BYTES = 32
DEFAULT_OUTER_DELAY = 4
DEFAULT_DCACHE_DELAY_MIN = 2
DEFAULT_DCACHE_DELAY_MAX = 8
DEFAULT_DELAY_SEED = 20260330


def _signal_value(signal, default=None):
    try:
        return int(signal.value)
    except AttributeError:
        return default


class MemoryModel:
    """MemBlock transport + scoreboard 统一 facade。"""

    def __init__(
        self,
        dut,
        outer_a,
        outer_d,
        dcache_a,
        dcache_b,
        dcache_c,
        dcache_d,
        dcache_e,
        writebacks=None,
        store_data_inputs=None,
        store_addr_inputs=None,
        store_mask_inputs=None,
        store_addr_re_inputs=None,
        sq_shadow_entries=None,
        sbuffer_writes=None,
        outer_delay: int = DEFAULT_OUTER_DELAY,
        grant_delay_min: int = DEFAULT_DCACHE_DELAY_MIN,
        grant_delay_max: int = DEFAULT_DCACHE_DELAY_MAX,
        release_ack_delay: int = 1,
        delay_seed: int = DEFAULT_DELAY_SEED,
        rob_size: int = 512,
        store_queue_size: int = 56,
        strict_writeback_check: bool = True,
    ) -> None:
        self.dut = dut
        self.outer_a = outer_a
        self.outer_d = outer_d
        self.dcache_a = dcache_a
        self.dcache_b = dcache_b
        self.dcache_c = dcache_c
        self.dcache_d = dcache_d
        self.dcache_e = dcache_e
        self.rob_size = rob_size
        self.store_queue_size = store_queue_size
        self._cycle = 0
        self._runtime_reset_active = False

        self.ref_memory = RefMemory()
        self.memory = self.ref_memory.storage
        self.scoreboard = Scoreboard(
            self.ref_memory,
            rob_size=rob_size,
            store_queue_size=store_queue_size,
        )
        self.writeback_monitor = WritebackMonitor(writebacks, self.scoreboard)
        self.store_monitor = StoreMonitor(
            dut,
            self.scoreboard,
            store_data_inputs=store_data_inputs,
            store_addr_inputs=store_addr_inputs,
            store_mask_inputs=store_mask_inputs,
            store_addr_re_inputs=store_addr_re_inputs,
            sq_shadow_entries=sq_shadow_entries,
            sbuffer_writes=sbuffer_writes,
            store_queue_size=store_queue_size,
        )
        self.transport = TransportResponder(
            dut,
            outer_a,
            outer_d,
            dcache_a,
            dcache_b,
            dcache_c,
            dcache_d,
            dcache_e,
            self.ref_memory,
            self.scoreboard.drain_log,
            outer_delay=outer_delay,
            grant_delay_min=grant_delay_min,
            grant_delay_max=grant_delay_max,
            release_ack_delay=release_ack_delay,
            delay_seed=delay_seed,
        )
        self.scoreboard.strict_writeback_check = bool(strict_writeback_check)
        self.vector = VectorMemoryModel(self.ref_memory)

        self.drive_idle()

    @property
    def writebacks(self):
        return self.writeback_monitor.writebacks

    @property
    def pending_stores(self) -> dict[int, PendingStore]:
        return self.scoreboard.pending_stores

    @property
    def drain_log(self) -> list[dict]:
        return self.scoreboard.drain_log

    @property
    def strict_writeback_check(self) -> bool:
        return self.scoreboard.strict_writeback_check

    @strict_writeback_check.setter
    def strict_writeback_check(self, value: bool) -> None:
        self.scoreboard.strict_writeback_check = bool(value)

    @property
    def completed_loads(self) -> int:
        return self.scoreboard.completed_loads

    @property
    def writeback_events(self) -> int:
        return self.scoreboard.writeback_events

    @property
    def failed_transactions(self) -> int:
        return self.scoreboard.failed_transactions

    @property
    def sbuffer_drain_count(self) -> int:
        return self.scoreboard.sbuffer_drain_count

    def attach_writebacks(self, writebacks) -> None:
        self.writeback_monitor.attach_writebacks(writebacks)

    def drive_idle(self) -> None:
        self.transport.drive_idle()
        self.writeback_monitor.drive_ready()

    def drive_writeback_ready(self) -> None:
        self.writeback_monitor.drive_ready()

    def reset_runtime_state(self) -> None:
        self.transport.reset_runtime_state()
        self.scoreboard.reset_runtime_state()
        self.store_monitor.reset_runtime_state()
        self.vector.reset_runtime_state()
        self.drive_idle()

    def reset(self) -> None:
        self.ref_memory.clear()
        self.transport.reset()
        self.scoreboard.reset()
        self.reset_runtime_state()

    def preload_bytes(self, base_addr: int, data: bytes) -> None:
        self.ref_memory.preload_bytes(base_addr, data)

    def preload_u64(self, addr: int, value: int) -> None:
        self.ref_memory.preload_u64(addr, value)

    def fill_random(
        self,
        addr_start: int,
        addr_end: int,
        seed: int,
        line_bytes: int = DEFAULT_CACHELINE_BYTES,
    ) -> None:
        self.ref_memory.fill_random(addr_start, addr_end, seed, line_bytes=line_bytes)

    def read(self, addr: int, size: int) -> int:
        return self.ref_memory.read(addr, size)

    def read_masked(self, addr: int, mask: int, width_bytes: int = 8) -> int:
        return self.ref_memory.read_masked(addr, mask, width_bytes=width_bytes)

    def read_cacheline(self, block_addr: int, line_bytes: int = DEFAULT_CACHELINE_BYTES) -> bytes:
        return self.ref_memory.read_cacheline(block_addr, line_bytes=line_bytes)

    def apply_masked_write(self, addr: int, data: int, mask: int, width_bytes: int) -> None:
        self.ref_memory.apply_masked_write(addr, data, mask, width_bytes)

    def apply_store(self, addr: int, data: int, mask: int = 0xFF) -> None:
        self.ref_memory.apply_store(addr, data, mask)

    def apply_cbo_zero(self, addr: int, line_bytes: int = DEFAULT_CACHELINE_BYTES) -> None:
        self.ref_memory.apply_cbo_zero(addr, line_bytes=line_bytes)

    def fork_ref_memory(self) -> RefMemory:
        return self.ref_memory.clone()

    def predict_store(self, addr: int, data: int, mask: int = 0xFF) -> RefMemory:
        return self.ref_memory.with_store(addr, data, mask)

    def predict_cbo_zero(self, addr: int, line_bytes: int = DEFAULT_CACHELINE_BYTES) -> RefMemory:
        return self.ref_memory.with_cbo_zero(addr, line_bytes=line_bytes)

    def expect_load(
        self,
        rob_idx_flag: int,
        rob_idx_value: int,
        pdest: int,
        addr: int,
        size: int = 8,
        mask: int | None = None,
        fp_wen: int = 0,
    ) -> ExpectedLoad:
        return self.scoreboard.expect_load(
            rob_idx_flag=rob_idx_flag,
            rob_idx_value=rob_idx_value,
            pdest=pdest,
            addr=addr,
            size=size,
            mask=mask,
            fp_wen=fp_wen,
        )

    def note_load_issued(self, rob_idx_flag: int, rob_idx_value: int) -> None:
        self.scoreboard.note_load_issued(rob_idx_flag, rob_idx_value)

    def note_store_allocated(
        self,
        sq_idx_flag: int,
        sq_idx_value: int,
        rob_idx_flag: int,
        rob_idx_value: int,
    ) -> None:
        self.scoreboard.note_store_allocated(
            sq_idx_flag=sq_idx_flag,
            sq_idx_value=sq_idx_value,
            rob_idx_flag=rob_idx_flag,
            rob_idx_value=rob_idx_value,
        )

    def note_store_request(
        self,
        *,
        sq_idx: int,
        addr: int,
        data: int,
        mask: int,
        opcode: str = "scalar",
    ) -> None:
        self.scoreboard.note_store_request(
            sq_idx=sq_idx,
            addr=addr,
            data=data,
            mask=mask,
            opcode=opcode,
        )

    def note_load_commits(self, commit_count: int) -> None:
        self.scoreboard.note_load_commits(commit_count)

    @property
    def outstanding_expected_count(self) -> int:
        return self.scoreboard.outstanding_expected_count + self.vector.outstanding_expected_count

    @property
    def outstanding_transaction_count(self) -> int:
        return self.transport.outstanding_transaction_count

    @property
    def stats(self) -> dict:
        return {
            **self.transport.stats,
            **self.scoreboard.stats,
            "vector_outstanding_expected_count": self.vector.outstanding_expected_count,
            "outstanding_expected_count": self.outstanding_expected_count,
        }

    @property
    def outer_request_count(self) -> int:
        return self.transport.outer_request_count

    @property
    def outer_response_count(self) -> int:
        return self.transport.outer_response_count

    @property
    def outer_write_request_count(self) -> int:
        return self.transport.outer_write_request_count

    @property
    def dcache_a_request_count(self) -> int:
        return self.transport.dcache_a_request_count

    @property
    def dcache_c_request_count(self) -> int:
        return self.transport.dcache_c_request_count

    @property
    def dcache_e_request_count(self) -> int:
        return self.transport.dcache_e_request_count

    @property
    def dcache_b_response_count(self) -> int:
        return self.transport.dcache_b_response_count

    @property
    def dcache_d_response_count(self) -> int:
        return self.transport.dcache_d_response_count

    def enqueue_outer_response(
        self,
        opcode: int = TL_D_ACCESS_ACK_DATA,
        param: int = 0,
        size: int = 3,
        source: int = 0,
        request_source: int | None = None,
        sink: int = 0,
        denied: int = 0,
        data: int = 0,
        corrupt: int = 0,
        delay_cycles: int | None = None,
    ) -> None:
        self.transport.enqueue_outer_response(
            opcode=opcode,
            param=param,
            size=size,
            source=source,
            request_source=request_source,
            sink=sink,
            denied=denied,
            data=data,
            corrupt=corrupt,
            delay_cycles=delay_cycles,
        )

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
        self.transport.enqueue_b_response(
            opcode=opcode,
            param=param,
            size=size,
            source=source,
            address=address,
            mask=mask,
            data=data,
            corrupt=corrupt,
            delay_cycles=delay_cycles,
        )

    def enqueue_d_response(
        self,
        opcode: int = TL_D_GRANT_DATA,
        param: int = 0,
        size: int = 6,
        source: int = 0,
        request_channel: str = "a",
        request_source: int | None = None,
        sink: int = 0,
        denied: int = 0,
        echo_is_keyword: int = 0,
        data: int = 0,
        corrupt: int = 0,
        delay_cycles: int | None = None,
    ) -> None:
        self.transport.enqueue_d_response(
            opcode=opcode,
            param=param,
            size=size,
            source=source,
            request_channel=request_channel,
            request_source=request_source,
            sink=sink,
            denied=denied,
            echo_is_keyword=echo_is_keyword,
            data=data,
            corrupt=corrupt,
            delay_cycles=delay_cycles,
        )

    def set_dcache_client_ready_override(
        self,
        *,
        a_ready: int | bool | None = None,
        c_ready: int | bool | None = None,
        e_ready: int | bool | None = None,
    ) -> None:
        self.transport.set_request_ready_override("dcache_a", a_ready)
        self.transport.set_request_ready_override("dcache_c", c_ready)
        self.transport.set_request_ready_override("dcache_e", e_ready)

    def clear_dcache_client_ready_override(self) -> None:
        self.transport.set_request_ready_override("dcache_a", None)
        self.transport.set_request_ready_override("dcache_c", None)
        self.transport.set_request_ready_override("dcache_e", None)

    def capture_on_rise(self, cycle: int) -> None:
        self._cycle = cycle
        self.scoreboard.set_cycle(cycle)
        self.transport.capture_on_rise(cycle)

    def drive_pre_step(self, cycle: int | None = None) -> None:
        if cycle is not None:
            self._cycle = int(cycle)
        self.transport.drive_pre_step(self._cycle)

    def after_cycle(self) -> None:
        self.drive_writeback_ready()
        if self._in_reset():
            if not self._runtime_reset_active:
                self.reset_runtime_state()
            self._runtime_reset_active = True
            return

        self._runtime_reset_active = False
        self.store_monitor.after_cycle()
        self.writeback_monitor.after_cycle()

    def check_writebacks(self) -> None:
        self.writeback_monitor.after_cycle()

    def drain_completed_robs(self) -> list[RobIndex]:
        return self.scoreboard.drain_completed_robs()

    def finalize_and_check_drain(self) -> dict:
        return self.scoreboard.finalize_and_check_drain()

    def _in_reset(self) -> bool:
        return bool(_signal_value(self.dut.reset, 0) or _signal_value(self.dut.io_reset_backend, 0))
