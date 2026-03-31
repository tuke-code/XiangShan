# coding=utf-8
"""
Scoreboard: load compare + store retire/drain 校验。
"""

from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass


STORE_DATA_WIDTH_BYTES = 16


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


class Scoreboard:
    """统一维护 load compare、store shadow 与 drain 收尾语义。"""

    def __init__(
        self,
        dut,
        ref_memory,
        *,
        writebacks=None,
        store_data_inputs=None,
        store_addr_inputs=None,
        store_mask_inputs=None,
        store_addr_re_inputs=None,
        sq_shadow_entries=None,
        sbuffer_writes=None,
        rob_size: int,
        store_queue_size: int,
    ) -> None:
        self.dut = dut
        self.ref_memory = ref_memory
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

        self._cycle = 0
        self.strict_writeback_check = True
        self.completed_loads = 0
        self.writeback_events = 0
        self.failed_transactions = 0
        self.sbuffer_drain_count = 0

        self._expected_loads = defaultdict(deque)
        self._observed_load_writebacks = defaultdict(deque)
        self._issued_loads = deque()
        self._committed_load_budget = defaultdict(int)
        self._completed_rob_indices = deque()
        self._prev_sq_commit_ptr: tuple[int, int] | None = None
        self._prev_sq_deq_ptr: tuple[int, int] | None = None

        self.drive_writeback_ready()

    def attach_writebacks(self, writebacks) -> None:
        self.writebacks = list(writebacks)
        self.drive_writeback_ready()

    def drive_writeback_ready(self) -> None:
        for bundle in self.writebacks:
            if hasattr(bundle, "set_ready"):
                bundle.set_ready(1)

    def reset_runtime_state(self) -> None:
        self._expected_loads.clear()
        self._observed_load_writebacks.clear()
        self._issued_loads.clear()
        self._committed_load_budget.clear()
        self._completed_rob_indices.clear()
        self._prev_sq_commit_ptr = None
        self._prev_sq_deq_ptr = None
        self.pending_stores.clear()
        self.drain_log.clear()
        self.drive_writeback_ready()

    def reset(self) -> None:
        self.completed_loads = 0
        self.writeback_events = 0
        self.failed_transactions = 0
        self.sbuffer_drain_count = 0
        self.reset_runtime_state()

    def set_cycle(self, cycle: int) -> None:
        self._cycle = cycle

    @property
    def stats(self) -> dict[str, int]:
        return {
            "outstanding_expected_count": self.outstanding_expected_count,
            "completed_loads": self.completed_loads,
            "writeback_events": self.writeback_events,
            "pending_store_count": len(self.pending_stores),
            "drain_log_count": len(self.drain_log),
            "sbuffer_drain_count": self.sbuffer_drain_count,
        }

    @property
    def outstanding_expected_count(self) -> int:
        return sum(len(items) for items in self._expected_loads.values())

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

    def after_cycle(self) -> None:
        self.drive_writeback_ready()
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

    def drain_completed_robs(self) -> list[RobIndex]:
        completed = list(self._completed_rob_indices)
        self._completed_rob_indices.clear()
        return completed

    def finalize_and_check_drain(self) -> dict[str, int]:
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
            if drained_memory.get(addr, 0) != self.ref_memory.storage.get(addr, 0):
                mismatches.append(
                    (
                        addr,
                        self.ref_memory.storage.get(addr, 0),
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
                else self.ref_memory.read_masked(expected.addr, expected.mask, width_bytes=expected.size)
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
        self.ref_memory.apply_masked_write(
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
                    "width_bytes": STORE_DATA_WIDTH_BYTES,
                    "cycle": self._cycle,
                }
            )

    def _rob_rank(self, rob_idx: RobIndex | None) -> int:
        if rob_idx is None:
            return -1
        return rob_idx.flag * self.rob_size + rob_idx.value

    def _apply_masked_write_to_dict(self, backing: dict, addr: int, data: int, mask: int, width_bytes: int) -> None:
        for byte_idx in range(width_bytes):
            if (mask >> byte_idx) & 0x1:
                backing[addr + byte_idx] = (data >> (byte_idx * 8)) & 0xFF
