# coding=utf-8
"""
MemBlock 统一内存模型。

该模型统一接管：
  1. `outer buffer` / `uncache` TileLink 端口
  2. `dcache client` TileLink-C 端口
  3. `mem_to_ooo.intWriteback` load 数据校验
  4. store shadow / drain 观测

在线校验只针对 load：
  - load writeback 到达时先缓存结果
  - 等待测试侧通知该 load 已提交 (`lqDeq`) 后，再按 ROB 边界推进 `goldenmem`
  - 仅在 commit 视图上比较 load 结果

store 不做在线 compare：
  - 通过 SQ shadow、storeAddr/storeData/storeMask 观测 store 元数据
  - 通过 `io_sbuffer` 与 outer Put 记录最终 drain 写出
  - 测试结束后统一校验 drain-out 数据与最终 `goldenmem` 一致
"""

from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass

from model.ref_memory import RefMemory
from model.transport_responder import TransportResponder


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
DEFAULT_OUTER_DELAY = 100
DEFAULT_DCACHE_DELAY_MIN = 32
DEFAULT_DCACHE_DELAY_MAX = 100
DEFAULT_DELAY_SEED = 20260330
STORE_DATA_WIDTH_BYTES = 16


def _signal_value(signal, default=None):
    try:
        return int(signal.value)
    except AttributeError:
        return default


@dataclass(frozen=True)
class RobIndex:
    """ROB 索引键。"""

    flag: int
    value: int


@dataclass
class ExpectedLoad:
    """一笔待校验的 load。"""

    rob_idx: RobIndex
    pdest: int
    addr: int
    size: int
    mask: int
    expected_data: int | None = None
    expected_int_wen: int = 1
    expect_exception: bool = False


@dataclass
class ObservedLoadWriteback:
    """已观测、待在 commit 边界上比对的 load writeback。"""

    data: int
    pdest: int
    int_wen: int
    exception_bits: list[int]


@dataclass
class PendingStore:
    """按 SQ slot 跟踪的一笔 store。"""

    sq_idx: int
    rob_idx: RobIndex | None = None
    addr: int | None = None
    data: int | None = None
    mask: int = 0
    width_bytes: int = STORE_DATA_WIDTH_BYTES
    allocated: bool = False
    addr_valid: bool = False
    data_valid: bool = False
    committed: bool = False
    completed: bool = False
    nc: bool = False
    mmio: bool = False
    mem_back_type_mm: bool = False
    has_exception: bool = False
    retired: bool = False

    @property
    def ready_for_retire(self) -> bool:
        return (
            self.committed
            and self.addr_valid
            and self.data_valid
            and self.addr is not None
            and self.data is not None
            and self.mask != 0
            and not self.mmio
            and not self.has_exception
        )


class MemoryModel:
    """MemBlock load compare + store drain 统一模型。"""

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
    ) -> None:
        self.dut = dut
        self.outer_a = outer_a
        self.outer_d = outer_d
        self.dcache_a = dcache_a
        self.dcache_b = dcache_b
        self.dcache_c = dcache_c
        self.dcache_d = dcache_d
        self.dcache_e = dcache_e
        self.writebacks = list(writebacks or [])
        self.store_data_inputs = list(store_data_inputs or [])
        self.store_addr_inputs = list(store_addr_inputs or [])
        self.store_mask_inputs = list(store_mask_inputs or [])
        self.store_addr_re_inputs = list(store_addr_re_inputs or [])
        self.sq_shadow_entries = list(sq_shadow_entries or [])
        self.sbuffer_writes = list(sbuffer_writes or [])
        self.rob_size = rob_size
        self.store_queue_size = store_queue_size
        self.pending_stores: dict[int, PendingStore] = {}
        self.drain_log: list[dict] = []

        self.ref_memory = RefMemory()
        self.memory = self.ref_memory.storage
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
            self.drain_log,
            outer_delay=outer_delay,
            grant_delay_min=grant_delay_min,
            grant_delay_max=grant_delay_max,
            release_ack_delay=release_ack_delay,
            delay_seed=delay_seed,
        )
        self._cycle = 0
        self._runtime_reset_active = False

        self._expected_loads = defaultdict(deque)
        self._observed_load_writebacks = defaultdict(deque)
        self._issued_loads = deque()
        self._committed_load_budget = defaultdict(int)
        self._completed_rob_indices = deque()
        self._prev_sq_commit_ptr: tuple[int, int] | None = None
        self._prev_sq_deq_ptr: tuple[int, int] | None = None

        self.strict_writeback_check = True
        self.completed_loads = 0
        self.writeback_events = 0
        self.failed_transactions = 0
        self.sbuffer_drain_count = 0

        self.drive_idle()

    def attach_writebacks(self, writebacks) -> None:
        self.writebacks = list(writebacks)
        self.drive_writeback_ready()

    def drive_idle(self) -> None:
        self.transport.drive_idle()
        self.drive_writeback_ready()

    def drive_writeback_ready(self) -> None:
        for bundle in self.writebacks:
            if hasattr(bundle, "set_ready"):
                bundle.set_ready(1)

    def reset_runtime_state(self) -> None:
        self.transport.reset_runtime_state()
        self._expected_loads.clear()
        self._observed_load_writebacks.clear()
        self._issued_loads.clear()
        self._committed_load_budget.clear()
        self._completed_rob_indices.clear()
        self._prev_sq_commit_ptr = None
        self._prev_sq_deq_ptr = None
        self.pending_stores.clear()
        self.drain_log.clear()
        self.drive_idle()

    def reset(self) -> None:
        self.ref_memory.clear()
        self.transport.reset()
        self.completed_loads = 0
        self.writeback_events = 0
        self.failed_transactions = 0
        self.sbuffer_drain_count = 0
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

    def expect_load(
        self,
        rob_idx_flag: int,
        rob_idx_value: int,
        pdest: int,
        addr: int,
        size: int = 8,
        mask: int | None = None,
    ) -> ExpectedLoad:
        width_mask = (1 << size) - 1
        effective_mask = width_mask if mask is None else mask
        expected = ExpectedLoad(
            rob_idx=RobIndex(flag=rob_idx_flag, value=rob_idx_value),
            pdest=pdest,
            addr=addr,
            size=size,
            mask=effective_mask,
        )
        self._expected_loads[expected.rob_idx].append(expected)
        return expected

    def note_load_issued(self, rob_idx_flag: int, rob_idx_value: int) -> None:
        self._issued_loads.append(RobIndex(flag=rob_idx_flag, value=rob_idx_value))

    def note_store_allocated(
        self,
        sq_idx_flag: int,
        sq_idx_value: int,
        rob_idx_flag: int,
        rob_idx_value: int,
    ) -> None:
        del sq_idx_flag
        store = self.pending_stores.setdefault(sq_idx_value, PendingStore(sq_idx=sq_idx_value))
        store.allocated = True
        store.rob_idx = RobIndex(flag=rob_idx_flag, value=rob_idx_value)

    def note_load_commits(self, commit_count: int) -> None:
        for _ in range(max(0, int(commit_count))):
            if not self._issued_loads:
                return
            rob_idx = self._issued_loads.popleft()
            self._committed_load_budget[rob_idx] += 1
            self._try_complete_loads(rob_idx)

    @property
    def outstanding_expected_count(self) -> int:
        return sum(len(items) for items in self._expected_loads.values())

    @property
    def outstanding_transaction_count(self) -> int:
        return self.transport.outstanding_transaction_count

    @property
    def stats(self) -> dict:
        return {
            **self.transport.stats,
            "outstanding_expected_count": self.outstanding_expected_count,
            "completed_loads": self.completed_loads,
            "writeback_events": self.writeback_events,
            "pending_store_count": len(self.pending_stores),
            "drain_log_count": len(self.drain_log),
            "sbuffer_drain_count": self.sbuffer_drain_count,
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

    def on_memory_edge(self, cycle: int) -> None:
        self._cycle = cycle
        self.transport.on_memory_edge(cycle)

    def after_cycle(self) -> None:
        self.drive_writeback_ready()
        if self._in_reset():
            if not self._runtime_reset_active:
                self.reset_runtime_state()
            self._runtime_reset_active = True
            return

        self._runtime_reset_active = False
        self._observe_store_events()
        self.check_writebacks()

    def check_writebacks(self) -> None:
        for bundle in self.writebacks:
            if not getattr(bundle, "connected", lambda name: False)("valid"):
                continue
            if bundle.read("valid", 0) == 0:
                continue
            if bundle.connected("ready") and bundle.read("ready", 0) == 0:
                continue

            self.writeback_events += 1
            if bundle.connected("isFromLoadUnit") and bundle.read("isFromLoadUnit", 0) == 0:
                self._observe_store_writeback(bundle)
                continue
            if bundle.connected("toRob_valid") and bundle.read("toRob_valid", 0) == 0:
                continue
            if (
                not bundle.connected("data_0")
                or not bundle.connected("pdest")
                or not bundle.connected("intWen")
                or not bundle.connected("robIdx_flag")
                or not bundle.connected("robIdx_value")
            ):
                self._observe_store_writeback(bundle)
                continue
            if bundle.read("intWen", 0) == 0:
                continue

            rob_idx = RobIndex(
                flag=bundle.read("robIdx_flag", 0),
                value=bundle.read("robIdx_value", 0),
            )
            if not self._expected_loads[rob_idx]:
                if self.strict_writeback_check:
                    raise AssertionError(f"观测到未登记的 load writeback: robIdx={rob_idx}")
                self._completed_rob_indices.append(rob_idx)
                continue

            self._observed_load_writebacks[rob_idx].append(
                ObservedLoadWriteback(
                    data=bundle.read("data_0", 0),
                    pdest=bundle.read("pdest", 0),
                    int_wen=bundle.read("intWen", 0),
                    exception_bits=bundle.read_exception_bits(),
                )
            )
            self._completed_rob_indices.append(rob_idx)
            self._try_complete_loads(rob_idx)

    def _observe_store_writeback(self, bundle) -> None:
        if not bundle.connected("sqIdx_value") or not bundle.connected("robIdx_value"):
            return
        sq_idx = bundle.read("sqIdx_value", -1)
        if sq_idx < 0:
            return
        store = self.pending_stores.setdefault(sq_idx, PendingStore(sq_idx=sq_idx))
        store.allocated = True
        if bundle.connected("robIdx_flag") and bundle.connected("robIdx_value"):
            store.rob_idx = RobIndex(
                flag=bundle.read("robIdx_flag", 0),
                value=bundle.read("robIdx_value", 0),
            )
        store.completed = True

    def drain_completed_robs(self) -> list[RobIndex]:
        completed = list(self._completed_rob_indices)
        self._completed_rob_indices.clear()
        return completed

    def finalize_and_check_drain(self) -> dict:
        expected_visible_store_count = sum(1 for store in self.pending_stores.values() if store.ready_for_retire)
        self._retire_all_ready_stores()
        if expected_visible_store_count and not self.drain_log:
            raise AssertionError("存在已提交 store，但测试结束时未观测到任何 drain 写出事件")

        drained_memory = {}
        touched_addresses = set()
        for event in self.drain_log:
            self._apply_masked_write_to_dict(
                drained_memory,
                event["addr"],
                event["data"],
                event["mask"],
                event["width_bytes"],
            )
            for byte_idx in range(event["width_bytes"]):
                if (event["mask"] >> byte_idx) & 0x1:
                    touched_addresses.add(event["addr"] + byte_idx)

        mismatches = []
        for addr in sorted(touched_addresses):
            if drained_memory.get(addr, 0) != self.memory.get(addr, 0):
                mismatches.append(
                    (
                        addr,
                        self.memory.get(addr, 0),
                        drained_memory.get(addr, 0),
                    )
                )
        if mismatches:
            addr, expected, observed = mismatches[0]
            raise AssertionError(
                "drain 数据与 goldenmem 不一致: "
                f"addr=0x{addr:x}, expected=0x{expected:x}, observed=0x{observed:x}"
            )

        return {
            "drain_event_count": len(self.drain_log),
            "touched_byte_count": len(touched_addresses),
        }

    def _try_complete_loads(self, rob_idx: RobIndex) -> None:
        while (
            self._committed_load_budget.get(rob_idx, 0) > 0
            and self._expected_loads.get(rob_idx)
            and self._observed_load_writebacks.get(rob_idx)
        ):
            expected = self._expected_loads[rob_idx].popleft()
            observed = self._observed_load_writebacks[rob_idx].popleft()
            self._committed_load_budget[rob_idx] -= 1
            if self._committed_load_budget[rob_idx] == 0:
                del self._committed_load_budget[rob_idx]

            self._retire_stores_before_boundary(expected.rob_idx)
            expected_data = (
                expected.expected_data
                if expected.expected_data is not None
                else self.read_masked(expected.addr, expected.mask, width_bytes=expected.size)
            )
            if observed.pdest != expected.pdest:
                raise AssertionError(
                    f"load writeback pdest 不匹配: robIdx={rob_idx}, expected={expected.pdest}, observed={observed.pdest}"
                )
            if observed.int_wen != expected.expected_int_wen:
                raise AssertionError(
                    "load writeback intWen 不匹配: "
                    f"robIdx={rob_idx}, expected={expected.expected_int_wen}, observed={observed.int_wen}"
                )
            if observed.data != expected_data:
                raise AssertionError(
                    "load writeback data 不匹配: "
                    f"robIdx={rob_idx}, addr=0x{expected.addr:x}, "
                    f"expected=0x{expected_data:x}, observed=0x{observed.data:x}"
                )
            if any(observed.exception_bits) and not expected.expect_exception:
                raise AssertionError(
                    f"load writeback 异常位非 0: robIdx={rob_idx}, exceptionVec={observed.exception_bits}"
                )
            self.completed_loads += 1
            if not self._expected_loads[rob_idx]:
                del self._expected_loads[rob_idx]
            if not self._observed_load_writebacks.get(rob_idx):
                self._observed_load_writebacks.pop(rob_idx, None)

    def _retire_stores_before_boundary(self, load_rob_idx: RobIndex) -> None:
        candidates = []
        boundary = self._rob_rank(load_rob_idx)
        for store in self.pending_stores.values():
            if not store.retired and store.ready_for_retire and store.rob_idx is not None:
                if self._rob_rank(store.rob_idx) <= boundary:
                    candidates.append(store)
        candidates.sort(key=lambda item: (self._rob_rank(item.rob_idx), item.sq_idx))
        for store in candidates:
            self._retire_store(store)
            store.retired = True

    def _retire_all_ready_stores(self) -> None:
        candidates = [store for store in self.pending_stores.values() if not store.retired and store.ready_for_retire]
        candidates.sort(
            key=lambda item: (
                self._rob_rank(item.rob_idx) if item.rob_idx is not None else -1,
                item.sq_idx,
            )
        )
        for store in candidates:
            self._retire_store(store)
            store.retired = True

    def _retire_store(self, store: PendingStore) -> None:
        aligned_addr = store.addr & ~(store.width_bytes - 1)
        effective_mask = store.mask
        byte_offset = store.addr & (store.width_bytes - 1)
        effective_data = store.data << (byte_offset * 8)
        if byte_offset and (effective_mask & 0x1):
            effective_mask <<= byte_offset
        self.apply_masked_write(
            aligned_addr,
            effective_data,
            effective_mask & ((1 << store.width_bytes) - 1),
            store.width_bytes,
        )

    def _observe_store_events(self) -> None:
        self._observe_sq_commit_ptr()
        self._observe_sq_deq_ptr()
        self._observe_sq_shadow()
        self._observe_store_addr()
        self._observe_store_addr_re()
        self._observe_store_mask()
        self._observe_store_data()
        self._observe_sbuffer_writes()

    def _read_ptr(self, prefix: str) -> tuple[int, int] | None:
        flag_signal = getattr(self.dut, f"{prefix}_flag", None)
        value_signal = getattr(self.dut, f"{prefix}_value", None)
        if flag_signal is None or value_signal is None:
            return None
        return (int(flag_signal.value), int(value_signal.value))

    def _ptr_iter(self, prev: tuple[int, int], curr: tuple[int, int], size: int) -> list[int]:
        prev_abs = prev[0] * size + prev[1]
        curr_abs = curr[0] * size + curr[1]
        if curr_abs < prev_abs:
            curr_abs += size * 2
        distance = curr_abs - prev_abs
        _, ptr_value = prev
        indices = []
        for _ in range(distance):
            indices.append(ptr_value)
            ptr_value += 1
            if ptr_value >= size:
                ptr_value = 0
        return indices

    def _observe_sq_commit_ptr(self) -> None:
        curr = self._read_ptr("MemBlock_inner_lsq_io_sqCommitPtr")
        if curr is None:
            return
        if self._prev_sq_commit_ptr is None:
            self._prev_sq_commit_ptr = curr
            return
        if curr != self._prev_sq_commit_ptr:
            for sq_idx in self._ptr_iter(self._prev_sq_commit_ptr, curr, self.store_queue_size):
                store = self.pending_stores.setdefault(sq_idx, PendingStore(sq_idx=sq_idx))
                store.allocated = True
                store.committed = True
        self._prev_sq_commit_ptr = curr

    def _observe_sq_deq_ptr(self) -> None:
        curr = self._read_ptr("io_mem_to_ooo_sqDeqPtr")
        if curr is None:
            curr = self._read_ptr("MemBlock_inner_lsq_io_sqDeqPtr")
        if curr is None:
            return
        if self._prev_sq_deq_ptr is None:
            self._prev_sq_deq_ptr = curr
            return
        if curr != self._prev_sq_deq_ptr:
            for sq_idx in self._ptr_iter(self._prev_sq_deq_ptr, curr, self.store_queue_size):
                store = self.pending_stores.setdefault(sq_idx, PendingStore(sq_idx=sq_idx))
                store.completed = True
        self._prev_sq_deq_ptr = curr

    def _observe_sq_shadow(self) -> None:
        for sq_idx, entry in enumerate(self.sq_shadow_entries):
            if not entry.connected("allocated"):
                continue
            allocated = bool(entry.read("allocated", 0))
            rob_idx = RobIndex(
                flag=entry.read("robIdx_flag", 0),
                value=entry.read("robIdx_value", 0),
            )
            store = self.pending_stores.get(sq_idx)
            if store is None or (allocated and store.rob_idx is not None and store.rob_idx != rob_idx):
                store = PendingStore(sq_idx=sq_idx, rob_idx=rob_idx if allocated else None)
                self.pending_stores[sq_idx] = store

            store.allocated = allocated
            if allocated:
                store.rob_idx = rob_idx
                store.addr_valid = bool(entry.read("addrvalid", store.addr_valid))
                store.data_valid = bool(entry.read("datavalid", store.data_valid))
                store.committed = bool(entry.read("committed", store.committed))
                store.completed = bool(entry.read("completed", store.completed))
                store.nc = bool(entry.read("nc", store.nc))
            else:
                store.allocated = False

    def _observe_store_addr(self) -> None:
        for lane in self.store_addr_inputs:
            if lane.read("valid", 0) == 0:
                continue
            sq_idx = lane.read("sqIdx_value", 0)
            store = self.pending_stores.setdefault(sq_idx, PendingStore(sq_idx=sq_idx))
            store.allocated = True
            store.addr = lane.read("paddr", store.addr if store.addr is not None else 0)
            store.addr_valid = not bool(lane.read("miss", 0))
            if lane.connected("mask"):
                store.mask = lane.read("mask", store.mask)
            store.nc = bool(lane.read("nc", store.nc))

    def _observe_store_addr_re(self) -> None:
        for lane_idx, lane in enumerate(self.store_addr_re_inputs):
            should_update = True
            if lane.connected("updateAddrValid"):
                should_update = lane.read("updateAddrValid", 0) != 0
            elif not any(
                lane.connected(name) and lane.read(name, 0) != 0
                for name in ("nc", "mmio", "memBackTypeMM", "hasException")
            ):
                should_update = False
            if not should_update:
                continue
            sq_idx = lane.read("sqIdx_value", -1)
            if sq_idx < 0 and lane_idx < len(self.store_addr_inputs):
                sq_idx = self.store_addr_inputs[lane_idx].read("sqIdx_value", -1)
            if sq_idx < 0:
                continue
            store = self.pending_stores.setdefault(sq_idx, PendingStore(sq_idx=sq_idx))
            store.allocated = True
            store.addr_valid = True
            if lane.connected("nc"):
                store.nc = bool(lane.read("nc", store.nc))
            store.mmio = bool(lane.read("mmio", store.mmio))
            store.mem_back_type_mm = bool(lane.read("memBackTypeMM", store.mem_back_type_mm))
            store.has_exception = bool(lane.read("hasException", store.has_exception))

    def _observe_store_mask(self) -> None:
        for lane in self.store_mask_inputs:
            if lane.read("valid", 0) == 0:
                continue
            sq_idx = lane.read("sqIdx_value", 0)
            store = self.pending_stores.setdefault(sq_idx, PendingStore(sq_idx=sq_idx))
            store.allocated = True
            store.mask = lane.read("mask", store.mask)

    def _observe_store_data(self) -> None:
        for lane in self.store_data_inputs:
            if lane.read("valid", 0) == 0:
                continue
            sq_idx = lane.read("sqIdx_value", 0)
            store = self.pending_stores.setdefault(sq_idx, PendingStore(sq_idx=sq_idx))
            store.allocated = True
            store.data = lane.read("data", store.data if store.data is not None else 0)
            store.data_valid = True
            store.width_bytes = STORE_DATA_WIDTH_BYTES

    def _observe_sbuffer_writes(self) -> None:
        for lane_idx, lane in enumerate(self.sbuffer_writes):
            if lane.read("valid", 0) == 0:
                continue
            if lane.connected("ready") and lane.read("ready", 0) == 0:
                continue
            if lane.connected("vecValid") and lane.read("vecValid", 1) == 0:
                continue
            if lane.read("wline", 0):
                raise AssertionError("当前 MemoryModel 暂不支持 wline store drain 校验")

            self.sbuffer_drain_count += 1
            self.drain_log.append(
                {
                    "channel": "sbuffer",
                    "lane": lane_idx,
                    "addr": lane.read("addr", 0),
                    "data": lane.read("data", 0),
                    "mask": lane.read("mask", 0),
                    "width_bytes": 16,
                    "cycle": self._cycle,
                }
            )

    def _in_reset(self) -> bool:
        return bool(_signal_value(self.dut.reset, 0) or _signal_value(self.dut.io_reset_backend, 0))

    def _drive_request_readies(self) -> None:
        self.outer_a.ready.value = 1
        self.dcache_a.ready.value = 1
        self.dcache_c.ready.value = 1
        self.dcache_e.ready.value = 1

    def _capture_outer_request(self) -> None:
        if not (self.outer_a.valid.value and self.outer_a.ready.value):
            return

        opcode = int(self.outer_a.bits_opcode.value)
        size = int(self.outer_a.bits_size.value)
        source = int(self.outer_a.bits_source.value)
        addr = int(self.outer_a.bits_address.value)
        width_bytes = max(1, 1 << size)

        self.outer_request_count += 1
        if opcode == TL_A_GET:
            data = self.read(addr, width_bytes)
            self.enqueue_outer_response(
                opcode=TL_D_ACCESS_ACK_DATA,
                size=size,
                source=source,
                request_source=source,
                data=data,
                corrupt=0,
            )
            return

        if opcode in (TL_A_PUT_FULL, TL_A_PUT_PARTIAL):
            self.outer_write_request_count += 1
            mask = int(self.outer_a.bits_mask.value)
            if opcode == TL_A_PUT_FULL:
                mask = (1 << width_bytes) - 1
            self.drain_log.append(
                {
                    "channel": "outer",
                    "addr": addr,
                    "data": int(self.outer_a.bits_data.value),
                    "mask": mask,
                    "width_bytes": width_bytes,
                    "cycle": self._cycle,
                }
            )
            self.enqueue_outer_response(
                opcode=TL_D_ACCESS_ACK,
                size=size,
                source=source,
                request_source=source,
                data=0,
                corrupt=0,
            )
            return

        raise AssertionError(f"outer buffer 未支持的请求 opcode={opcode}")

    def _capture_dcache_requests(self) -> None:
        if self.dcache_a.valid.value and self.dcache_a.ready.value:
            self.dcache_a_request_count += 1
            self._handle_dcache_a()

        if self.dcache_c.valid.value and self.dcache_c.ready.value:
            self.dcache_c_request_count += 1
            self._handle_dcache_c()

        if self.dcache_e.valid.value and self.dcache_e.ready.value:
            self.dcache_e_request_count += 1
            self._handle_dcache_e()

    def _handle_dcache_a(self) -> None:
        opcode = int(self.dcache_a.bits_opcode.value)
        if opcode != TL_A_ACQUIRE_BLOCK:
            raise AssertionError(f"load-only 场景仅支持 AcquireBlock，观测到 A.opcode={opcode}")

        block_addr = int(self.dcache_a.bits_address.value) & ~(DEFAULT_CACHELINE_BYTES - 1)
        size = int(self.dcache_a.bits_size.value)
        source = int(self.dcache_a.bits_source.value)
        is_keyword = int(self.dcache_a.bits_echo_isKeyword.value)
        line = self.read_cacheline(block_addr, DEFAULT_CACHELINE_BYTES)
        beats = [
            int.from_bytes(line[idx: idx + DEFAULT_DCACHE_BEAT_BYTES], "little")
            for idx in range(0, DEFAULT_CACHELINE_BYTES, DEFAULT_DCACHE_BEAT_BYTES)
        ]
        if is_keyword:
            beats = list(reversed(beats))

        sink = self._alloc_sink()
        delay = self._sample_dcache_delay()
        self._inflight_grants[sink] = {"source": source, "opcode": opcode}
        self._pending_d.append(
            {
                "release_cycle": self._cycle + delay,
                "beats": deque(
                    [
                        {
                            "opcode": TL_D_GRANT_DATA,
                            "param": 0,
                            "size": size,
                            "source": source,
                            "request_channel": "a",
                            "request_source": source,
                            "sink": sink,
                            "denied": 0,
                            "echo_isKeyword": is_keyword,
                            "data": beat,
                            "corrupt": 0,
                        }
                        for beat in beats
                    ]
                ),
            }
        )

    def _handle_dcache_c(self) -> None:
        opcode = int(self.dcache_c.bits_opcode.value)
        if opcode in (TL_C_PROBE_ACK, TL_C_PROBE_ACK_DATA):
            return
        if opcode in (TL_C_RELEASE, TL_C_RELEASE_DATA):
            self._pending_d.append(
                {
                    "release_cycle": self._cycle + self.release_ack_delay,
                    "beats": deque(
                        [
                            {
                                "opcode": TL_D_RELEASE_ACK,
                                "param": 0,
                                "size": int(self.dcache_c.bits_size.value),
                                "source": int(self.dcache_c.bits_source.value),
                                "request_channel": "c",
                                "request_source": int(self.dcache_c.bits_source.value),
                                "sink": 0,
                                "denied": 0,
                                "echo_isKeyword": int(self.dcache_c.bits_echo_isKeyword.value),
                                "data": 0,
                                "corrupt": 0,
                            }
                        ]
                    ),
                }
            )
            return
        raise AssertionError(f"未支持的 DCache C 通道 opcode={opcode}")

    def _handle_dcache_e(self) -> None:
        sink = int(self.dcache_e.bits_sink.value)
        if sink not in self._inflight_grants:
            raise AssertionError(f"观测到未知的 GrantAck sink={sink}")
        del self._inflight_grants[sink]

    def _service_outer_d(self) -> None:
        if self._active_outer_d is None and self._pending_outer_d:
            if self._pending_outer_d[0]["release_cycle"] <= self._cycle:
                self._active_outer_d = self._pending_outer_d.popleft()

        if self._active_outer_d is None:
            self.outer_d.drive_idle()
            return

        response = self._active_outer_d
        if response["source"] != response["request_source"]:
            raise AssertionError(
                "outer D 通道 source 必须与前序 A 通道请求 source 一致: "
                f"request_source={response['request_source']}, d_source={response['source']}"
            )
        self.outer_d.valid.value = 1
        self.outer_d.bits_opcode.value = response["opcode"]
        self.outer_d.bits_param.value = response["param"]
        self.outer_d.bits_size.value = response["size"]
        self.outer_d.bits_source.value = response["source"]
        self.outer_d.bits_sink.value = response["sink"]
        self.outer_d.bits_denied.value = response["denied"]
        self.outer_d.bits_data.value = response["data"]
        self.outer_d.bits_corrupt.value = response["corrupt"]

        if self.outer_d.ready.value:
            self.outer_response_count += 1
            self._active_outer_d = None

    def _service_b_channel(self) -> None:
        if self._active_b is None and self._pending_b:
            if self._pending_b[0]["release_cycle"] <= self._cycle:
                self._active_b = self._pending_b.popleft()

        if self._active_b is None:
            self.dcache_b.drive_idle()
            return

        response = self._active_b
        self.dcache_b.valid.value = 1
        self.dcache_b.bits_opcode.value = response["opcode"]
        self.dcache_b.bits_param.value = response["param"]
        self.dcache_b.bits_size.value = response["size"]
        self.dcache_b.bits_source.value = response["source"]
        self.dcache_b.bits_address.value = response["address"]
        self.dcache_b.bits_mask.value = response["mask"]
        self.dcache_b.bits_data.value = response["data"]
        self.dcache_b.bits_corrupt.value = response["corrupt"]

        if self.dcache_b.ready.value:
            self.dcache_b_response_count += 1
            self._active_b = None

    def _service_d_channel(self) -> None:
        if self._active_d is None and self._pending_d:
            if self._pending_d[0]["release_cycle"] <= self._cycle:
                self._active_d = self._pending_d.popleft()

        if self._active_d is None:
            self.dcache_d.drive_idle()
            return

        beat = self._active_d["beats"][0]
        if beat.get("request_channel") == "a" and beat["source"] != beat["request_source"]:
            raise AssertionError(
                "dcache D 通道 source 必须与前序 A 通道请求 source 一致: "
                f"request_source={beat['request_source']}, d_source={beat['source']}"
            )
        self.dcache_d.valid.value = 1
        self.dcache_d.bits_opcode.value = beat["opcode"]
        self.dcache_d.bits_param.value = beat["param"]
        self.dcache_d.bits_size.value = beat["size"]
        self.dcache_d.bits_source.value = beat["source"]
        self.dcache_d.bits_sink.value = beat["sink"]
        self.dcache_d.bits_denied.value = beat["denied"]
        self.dcache_d.bits_echo_isKeyword.value = beat["echo_isKeyword"]
        self.dcache_d.bits_data.value = beat["data"]
        self.dcache_d.bits_corrupt.value = beat["corrupt"]

        if self.dcache_d.ready.value:
            self.dcache_d_response_count += 1
            self._active_d["beats"].popleft()
            if not self._active_d["beats"]:
                self._active_d = None

    def _alloc_sink(self) -> int:
        sink = self._next_sink
        self._next_sink = (self._next_sink + 1) & 0x3FF
        if self._next_sink == 0:
            self._next_sink = 1
        return sink

    def _rob_rank(self, rob_idx: RobIndex | None) -> int:
        if rob_idx is None:
            return -1
        return rob_idx.flag * self.rob_size + rob_idx.value

    def _apply_masked_write_to_dict(self, backing: dict, addr: int, data: int, mask: int, width_bytes: int) -> None:
        for byte_idx in range(width_bytes):
            if (mask >> byte_idx) & 0x1:
                backing[addr + byte_idx] = (data >> (byte_idx * 8)) & 0xFF
