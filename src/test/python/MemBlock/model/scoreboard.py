# coding=utf-8
"""
Scoreboard: load compare + store retire/drain 校验。
"""

from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass

from transactions import (
    CACHELINE_BYTES,
    CBO_ZERO_STORE_OPCODE,
    LSU_OP_CBO_ZERO,
    SCALAR_STORE_OPCODE,
    normalized_store_opcode,
)


STORE_DATA_WIDTH_BYTES = 16
FP_LOAD_BOX_FILL_BY_SIZE = {
    2: (48, 0xFFFF),
    4: (32, 0xFFFFFFFF),
    8: (0, 0xFFFFFFFFFFFFFFFF),
}


def _sign_extend_to_xlen(value: int, bits: int, xlen: int = 64) -> int:
    sign_bit = 1 << (int(bits) - 1)
    mask = (1 << int(bits)) - 1
    normalized = int(value) & mask
    if normalized & sign_bit:
        normalized |= ((1 << int(xlen)) - 1) ^ mask
    return normalized & ((1 << int(xlen)) - 1)


def _normalized_store_window(addr: int | None, mask: int, width_bytes: int) -> tuple[int, int, frozenset[int]] | None:
    if addr is None or width_bytes <= 0:
        return None
    aligned_addr = int(addr) & ~(int(width_bytes) - 1)
    byte_offset = int(addr) & (int(width_bytes) - 1)
    effective_mask = int(mask)
    if byte_offset and (effective_mask & 0x1):
        effective_mask <<= byte_offset
    effective_mask &= (1 << int(width_bytes)) - 1
    touched_bytes = frozenset(
        aligned_addr + byte_idx
        for byte_idx in range(int(width_bytes))
        if (effective_mask >> byte_idx) & 0x1
    )
    return aligned_addr, effective_mask, touched_bytes


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
    expected_fp_wen: int = 0
    expect_exception: bool = False


@dataclass
class ObservedLoadWriteback:
    """已观测、待在 commit 边界上比对的 load writeback。"""

    data: int
    pdest: int
    int_wen: int
    fp_wen: int
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
    opcode: str = SCALAR_STORE_OPCODE
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
    request_addr: int | None = None
    request_data: int | None = None
    request_mask: int | None = None
    request_opcode: str = SCALAR_STORE_OPCODE

    @property
    def is_cbo_zero(self) -> bool:
        return self.opcode == CBO_ZERO_STORE_OPCODE or self.request_opcode == CBO_ZERO_STORE_OPCODE

    @property
    def ready_for_retire(self) -> bool:
        retired_boundary = self.completed if self.is_cbo_zero else self.committed
        return (
            retired_boundary
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

    def __init__(self, ref_memory, *, rob_size: int, store_queue_size: int) -> None:
        self.ref_memory = ref_memory
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

    def reset_runtime_state(self) -> None:
        self._expected_loads.clear()
        self._observed_load_writebacks.clear()
        self._issued_loads.clear()
        self._committed_load_budget.clear()
        self._completed_rob_indices.clear()
        self.pending_stores.clear()
        self.drain_log.clear()

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

    def peek_expected_load(self, rob_idx) -> ExpectedLoad | None:
        if rob_idx is None:
            return None
        target_flag = int(getattr(rob_idx, "flag"))
        target_value = int(getattr(rob_idx, "value"))
        for key, queue in self._expected_loads.items():
            if int(key.flag) == target_flag and int(key.value) == target_value:
                return queue[0] if queue else None
        return None

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
        width_mask = (1 << size) - 1
        effective_mask = width_mask if mask is None else mask
        normalized_fp_wen = int(fp_wen)
        if normalized_fp_wen not in (0, 1):
            raise ValueError(f"fp_wen must be 0 or 1, got {fp_wen}")
        expected = ExpectedLoad(
            rob_idx=RobIndex(flag=rob_idx_flag, value=rob_idx_value),
            pdest=pdest,
            addr=addr,
            size=size,
            mask=effective_mask,
            expected_int_wen=0 if normalized_fp_wen else 1,
            expected_fp_wen=normalized_fp_wen,
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

    def note_store_request(self, *, sq_idx: int, addr: int, data: int, mask: int, opcode: str = SCALAR_STORE_OPCODE) -> None:
        store = self.pending_stores.setdefault(sq_idx, PendingStore(sq_idx=sq_idx))
        store.allocated = True
        store.request_addr = int(addr)
        store.request_data = int(data)
        store.request_mask = int(mask) & 0xFF
        store.request_opcode = normalized_store_opcode(opcode)

    def note_load_commits(self, commit_count: int) -> None:
        for _ in range(max(0, int(commit_count))):
            if not self._issued_loads:
                return
            rob_idx = self._issued_loads.popleft()
            self._committed_load_budget[rob_idx] += 1
            self._try_complete_loads(rob_idx)

    def observe_load_writeback(
        self,
        *,
        data: int,
        pdest: int,
        int_wen: int,
        fp_wen: int,
        rob_idx_flag: int,
        rob_idx_value: int,
        exception_bits: list[int],
    ) -> None:
        self.writeback_events += 1
        if int_wen == 0 and fp_wen == 0:
            return

        rob_idx = RobIndex(flag=rob_idx_flag, value=rob_idx_value)
        if not self._expected_loads[rob_idx]:
            if self.strict_writeback_check:
                raise AssertionError(f"观测到未登记的 load writeback: robIdx={rob_idx}")
            self._completed_rob_indices.append(rob_idx)
            return

        self._observed_load_writebacks[rob_idx].append(
            ObservedLoadWriteback(
                data=data,
                pdest=pdest,
                int_wen=int_wen,
                fp_wen=fp_wen,
                exception_bits=exception_bits,
            )
        )
        self._completed_rob_indices.append(rob_idx)
        self._try_complete_loads(rob_idx)

    def _expected_load_data(self, expected: ExpectedLoad) -> int:
        raw_data = (
            expected.expected_data
            if expected.expected_data is not None
            else self.ref_memory.read_masked(expected.addr, expected.mask, width_bytes=expected.size)
        )
        if not expected.expected_fp_wen:
            return _sign_extend_to_xlen(raw_data, expected.size * 8)

        if expected.size not in FP_LOAD_BOX_FILL_BY_SIZE:
            raise AssertionError(
                f"fp load compare 不支持的 size: robIdx={expected.rob_idx}, size={expected.size}"
            )
        fill_bits, low_mask = FP_LOAD_BOX_FILL_BY_SIZE[expected.size]
        if fill_bits == 0:
            return int(raw_data) & low_mask
        return ((1 << fill_bits) - 1) << (expected.size * 8) | (int(raw_data) & low_mask)

    def observe_store_writeback(
        self,
        *,
        sq_idx: int,
        rob_idx_flag: int | None = None,
        rob_idx_value: int | None = None,
    ) -> None:
        self.writeback_events += 1
        if sq_idx < 0:
            return
        store = self.pending_stores.setdefault(sq_idx, PendingStore(sq_idx=sq_idx))
        store.allocated = True
        if rob_idx_flag is not None and rob_idx_value is not None:
            store.rob_idx = RobIndex(flag=rob_idx_flag, value=rob_idx_value)
        store.completed = True

    def mark_store_committed(self, sq_idx: int) -> None:
        store = self.pending_stores.setdefault(sq_idx, PendingStore(sq_idx=sq_idx))
        store.allocated = True
        store.committed = True

    def mark_store_completed(self, sq_idx: int) -> None:
        store = self.pending_stores.setdefault(sq_idx, PendingStore(sq_idx=sq_idx))
        store.committed = True
        store.completed = True

    def observe_sq_shadow_entry(
        self,
        *,
        sq_idx: int,
        allocated: bool,
        rob_idx_flag: int,
        rob_idx_value: int,
        addrvalid: bool,
        datavalid: bool,
        committed: bool,
        completed: bool,
        nc: bool,
    ) -> None:
        rob_idx = RobIndex(flag=rob_idx_flag, value=rob_idx_value)
        store = self.pending_stores.get(sq_idx)
        if store is None or (allocated and store.rob_idx is not None and store.rob_idx != rob_idx):
            store = PendingStore(sq_idx=sq_idx, rob_idx=rob_idx if allocated else None)
            self.pending_stores[sq_idx] = store

        store.allocated = allocated
        if allocated:
            store.rob_idx = rob_idx
            store.addr_valid = store.addr_valid or bool(addrvalid)
            store.data_valid = store.data_valid or bool(datavalid)
            # 新 DUT 下 shadow 的 committed 位可能弱于 deq / writeback 事实源；
            # 一旦 store 被观测为 committed 或 completed，就不应再回退。
            store.committed = store.committed or bool(committed) or bool(completed)
            store.completed = store.completed or bool(completed)
            store.nc = bool(nc)

    def observe_store_addr(
        self,
        *,
        sq_idx: int,
        paddr: int,
        miss: bool,
        mask: int | None = None,
        nc: bool | None = None,
    ) -> None:
        store = self.pending_stores.setdefault(sq_idx, PendingStore(sq_idx=sq_idx))
        store.allocated = True
        store.addr = paddr
        store.addr_valid = not bool(miss)
        if mask is not None:
            store.mask = mask
        if nc is not None:
            store.nc = bool(nc)

    def observe_store_addr_re(
        self,
        *,
        sq_idx: int,
        nc: bool | None = None,
        mmio: bool = False,
        mem_back_type_mm: bool = False,
        has_exception: bool = False,
    ) -> None:
        store = self.pending_stores.setdefault(sq_idx, PendingStore(sq_idx=sq_idx))
        store.allocated = True
        store.addr_valid = True
        if nc is not None:
            store.nc = bool(nc)
        store.mmio = bool(mmio)
        store.mem_back_type_mm = bool(mem_back_type_mm)
        store.has_exception = bool(has_exception)

    def observe_store_mask(self, *, sq_idx: int, mask: int) -> None:
        store = self.pending_stores.setdefault(sq_idx, PendingStore(sq_idx=sq_idx))
        store.allocated = True
        store.mask = mask

    def observe_store_data(
        self,
        *,
        sq_idx: int,
        data: int,
        width_bytes: int = STORE_DATA_WIDTH_BYTES,
        fu_op_type: int | None = None,
    ) -> None:
        store = self.pending_stores.setdefault(sq_idx, PendingStore(sq_idx=sq_idx))
        store.allocated = True
        store.data = data
        store.data_valid = True
        store.width_bytes = width_bytes
        if fu_op_type is not None and int(fu_op_type) == LSU_OP_CBO_ZERO:
            store.opcode = CBO_ZERO_STORE_OPCODE

    def observe_sbuffer_write(
        self,
        *,
        lane_idx: int,
        addr: int,
        data: int,
        mask: int,
        width_bytes: int = STORE_DATA_WIDTH_BYTES,
    ) -> None:
        self.sbuffer_drain_count += 1
        self.drain_log.append(
            {
                "channel": "sbuffer",
                "lane": lane_idx,
                "addr": addr,
                "data": data,
                "mask": mask,
                "width_bytes": width_bytes,
                "cycle": self._cycle,
            }
        )

    def observe_sbuffer_wline(
        self,
        *,
        lane_idx: int,
        addr: int,
        line_bytes: int = CACHELINE_BYTES,
    ) -> None:
        self.sbuffer_drain_count += 1
        block_addr = int(addr) & ~(int(line_bytes) - 1)
        self.drain_log.append(
            {
                "channel": "sbuffer",
                "lane": lane_idx,
                "addr": block_addr,
                "data": 0,
                "mask": (1 << int(line_bytes)) - 1,
                "width_bytes": int(line_bytes),
                "cycle": self._cycle,
                "kind": CBO_ZERO_STORE_OPCODE,
                "wline": True,
            }
        )

    def drain_completed_robs(self) -> list[RobIndex]:
        completed = list(self._completed_rob_indices)
        self._completed_rob_indices.clear()
        return completed

    def finalize_and_check_drain(self) -> dict[str, int]:
        expected_visible_store_count = sum(1 for store in self.pending_stores.values() if store.ready_for_retire)
        self._retire_all_ready_stores()
        if expected_visible_store_count and not self.drain_log:
            raise AssertionError("存在已提交 store，但测试结束时未观测到任何 drain 写出事件")

        mmio_touched_bytes = set()
        for store in self.pending_stores.values():
            if not (
                store.mmio
                and store.committed
                and store.addr_valid
                and store.data_valid
                and store.addr is not None
                and store.data is not None
                and store.mask != 0
            ):
                continue
            normalized = _normalized_store_window(store.addr, store.mask, store.width_bytes)
            if normalized is not None:
                _, _, touched_bytes = normalized
                mmio_touched_bytes.update(touched_bytes)

        drained_memory = {}
        touched_addresses = set()
        for event in self._normalized_drain_events():
            self._apply_masked_write_to_dict(
                drained_memory,
                event["addr"],
                event["data"],
                event["mask"],
                event["width_bytes"],
                skipped_addresses=mmio_touched_bytes,
            )
            for byte_idx in range(event["width_bytes"]):
                if (event["mask"] >> byte_idx) & 0x1:
                    byte_addr = event["addr"] + byte_idx
                    if byte_addr not in mmio_touched_bytes:
                        touched_addresses.add(byte_addr)

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
            expected_data = self._expected_load_data(expected)
            if observed.pdest != expected.pdest:
                raise AssertionError(
                    f"load writeback pdest 不匹配: robIdx={rob_idx}, expected={expected.pdest}, observed={observed.pdest}"
                )
            if observed.int_wen != expected.expected_int_wen:
                raise AssertionError(
                    "load writeback intWen 不匹配: "
                    f"robIdx={rob_idx}, expected={expected.expected_int_wen}, observed={observed.int_wen}"
                )
            if observed.fp_wen != expected.expected_fp_wen:
                raise AssertionError(
                    "load writeback fpWen 不匹配: "
                    f"robIdx={rob_idx}, expected={expected.expected_fp_wen}, observed={observed.fp_wen}"
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
        if store.request_opcode == CBO_ZERO_STORE_OPCODE and store.request_addr is not None:
            self.ref_memory.apply_cbo_zero(store.request_addr)
            return
        if store.request_addr is not None and store.request_data is not None and store.request_mask is not None:
            self.ref_memory.apply_store(store.request_addr, store.request_data, store.request_mask)
            return
        if store.is_cbo_zero and store.addr is not None:
            self.ref_memory.apply_cbo_zero(store.addr)
            return
        normalized = _normalized_store_window(store.addr, store.mask, store.width_bytes)
        if normalized is None:
            return
        aligned_addr, effective_mask, _ = normalized
        byte_offset = store.addr & (store.width_bytes - 1)
        effective_data = store.data << (byte_offset * 8)
        self.ref_memory.apply_masked_write(
            aligned_addr,
            effective_data,
            effective_mask,
            store.width_bytes,
        )

    def _normalized_drain_events(self) -> list[dict]:
        normalized = []
        idx = 0
        while idx < len(self.drain_log):
            if idx + 1 < len(self.drain_log):
                paired = self._normalize_cross_16b_sbuffer_pair(self.drain_log[idx], self.drain_log[idx + 1])
                if paired is not None:
                    normalized.extend(paired)
                    idx += 2
                    continue
            normalized.append(dict(self.drain_log[idx]))
            idx += 1
        return normalized

    def _normalize_cross_16b_sbuffer_pair(self, first: dict, second: dict) -> list[dict] | None:
        width_bits = STORE_DATA_WIDTH_BYTES
        if first.get("channel") != "sbuffer" or second.get("channel") != "sbuffer":
            return None
        if first.get("cycle") != second.get("cycle"):
            return None
        if int(first.get("addr", 0)) + width_bits != int(second.get("addr", 0)):
            return None
        if int(first.get("data", 0)) != int(second.get("data", 0)):
            return None
        if int(first.get("mask", 0)) != int(second.get("mask", 0)):
            return None

        mask = int(first.get("mask", 0))
        low_part = 0
        for bit_idx in range(width_bits):
            if ((mask >> bit_idx) & 0x1) == 0:
                break
            low_part |= 1 << bit_idx

        high_part = 0
        for bit_idx in range(width_bits - 1, -1, -1):
            if ((mask >> bit_idx) & 0x1) == 0:
                break
            high_part |= 1 << bit_idx

        if (
            low_part == 0
            or high_part == 0
            or low_part == mask
            or high_part == mask
            or (low_part | high_part) != mask
        ):
            return None

        lower = dict(first)
        upper = dict(second)
        lower["mask"] = high_part
        upper["mask"] = low_part
        return [lower, upper]

    def _rob_rank(self, rob_idx: RobIndex | None) -> int:
        if rob_idx is None:
            return -1
        return rob_idx.flag * self.rob_size + rob_idx.value

    def _apply_masked_write_to_dict(
        self,
        backing: dict,
        addr: int,
        data: int,
        mask: int,
        width_bytes: int,
        skipped_addresses: set[int] | None = None,
    ) -> None:
        for byte_idx in range(width_bytes):
            if (mask >> byte_idx) & 0x1:
                byte_addr = addr + byte_idx
                if skipped_addresses is not None and byte_addr in skipped_addresses:
                    continue
                backing[byte_addr] = (data >> (byte_idx * 8)) & 0xFF
