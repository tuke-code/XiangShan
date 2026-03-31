# coding=utf-8
"""
StoreMonitor: 观测 store shadow / sbuffer / SQ 指针。
"""


class StoreMonitor:
    def __init__(
        self,
        dut,
        scoreboard,
        *,
        store_data_inputs=None,
        store_addr_inputs=None,
        store_mask_inputs=None,
        store_addr_re_inputs=None,
        sq_shadow_entries=None,
        sbuffer_writes=None,
        store_queue_size: int,
    ) -> None:
        self.dut = dut
        self.scoreboard = scoreboard
        self.store_data_inputs = list(store_data_inputs or [])
        self.store_addr_inputs = list(store_addr_inputs or [])
        self.store_mask_inputs = list(store_mask_inputs or [])
        self.store_addr_re_inputs = list(store_addr_re_inputs or [])
        self.sq_shadow_entries = list(sq_shadow_entries or [])
        self.sbuffer_writes = list(sbuffer_writes or [])
        self.store_queue_size = store_queue_size
        self._prev_sq_commit_ptr: tuple[int, int] | None = None
        self._prev_sq_deq_ptr: tuple[int, int] | None = None

    def reset_runtime_state(self) -> None:
        self._prev_sq_commit_ptr = None
        self._prev_sq_deq_ptr = None

    def after_cycle(self) -> None:
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
                self.scoreboard.mark_store_committed(sq_idx)
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
                self.scoreboard.mark_store_completed(sq_idx)
        self._prev_sq_deq_ptr = curr

    def _observe_sq_shadow(self) -> None:
        for sq_idx, entry in enumerate(self.sq_shadow_entries):
            if not entry.connected("allocated"):
                continue
            self.scoreboard.observe_sq_shadow_entry(
                sq_idx=sq_idx,
                allocated=bool(entry.read("allocated", 0)),
                rob_idx_flag=entry.read("robIdx_flag", 0),
                rob_idx_value=entry.read("robIdx_value", 0),
                addrvalid=bool(entry.read("addrvalid", 0)),
                datavalid=bool(entry.read("datavalid", 0)),
                committed=bool(entry.read("committed", 0)),
                completed=bool(entry.read("completed", 0)),
                nc=bool(entry.read("nc", 0)),
            )

    def _observe_store_addr(self) -> None:
        for lane in self.store_addr_inputs:
            if lane.read("valid", 0) == 0:
                continue
            self.scoreboard.observe_store_addr(
                sq_idx=lane.read("sqIdx_value", 0),
                paddr=lane.read("paddr", 0),
                miss=bool(lane.read("miss", 0)),
                mask=lane.read("mask", 0) if lane.connected("mask") else None,
                nc=bool(lane.read("nc", 0)) if lane.connected("nc") else None,
            )

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
            self.scoreboard.observe_store_addr_re(
                sq_idx=sq_idx,
                nc=bool(lane.read("nc", 0)) if lane.connected("nc") else None,
                mmio=bool(lane.read("mmio", 0)),
                mem_back_type_mm=bool(lane.read("memBackTypeMM", 0)),
                has_exception=bool(lane.read("hasException", 0)),
            )

    def _observe_store_mask(self) -> None:
        for lane in self.store_mask_inputs:
            if lane.read("valid", 0) == 0:
                continue
            self.scoreboard.observe_store_mask(
                sq_idx=lane.read("sqIdx_value", 0),
                mask=lane.read("mask", 0),
            )

    def _observe_store_data(self) -> None:
        for lane in self.store_data_inputs:
            if lane.read("valid", 0) == 0:
                continue
            self.scoreboard.observe_store_data(
                sq_idx=lane.read("sqIdx_value", 0),
                data=lane.read("data", 0),
            )

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
            self.scoreboard.observe_sbuffer_write(
                lane_idx=lane_idx,
                addr=lane.read("addr", 0),
                data=lane.read("data", 0),
                mask=lane.read("mask", 0),
            )
