# coding=utf-8
"""Unified backend-facing facade for MemBlock env."""

from collections import deque
from dataclasses import dataclass, replace

from agents.issue_agent import _set_optional_signal

from transactions import (
    BackendPreparedPlan,
    BackendSendPlan,
    BackendSendResult,
    EnqueueLoadCyclePlan,
    EnqueueLoadStep,
    EnqueueStoreStep,
    IssueCyclePlan,
    IssueOp,
    LoadTxn,
    NonMemBlockerStep,
    QueuePtr,
    RobIndex,
    RobRef,
    StoreCommitReadyStep,
    StoreCommitStep,
    StoreRef,
    StoreTxn,
    VectorEnqueueStep,
    VectorIssueStep,
    VectorMemTxn,
    VectorWaitStep,
    make_rob_index,
)


RS_FEEDBACK_TYPE_TLB_MISS = 1


@dataclass
class _TrackedStoreReplay:
    sq_ptr: QueuePtr
    rob_idx: RobIndex | None = None
    req_id: int | None = None
    addr: int | None = None
    lane: int | None = None
    mask: int = 0xFF
    store_opcode: str = "scalar"
    ftq_idx_flag: int | None = None
    ftq_idx_value: int | None = None
    awaiting_feedback: bool = False
    replay_pending: bool = False
    auto_replay_count: int = 0
    released: bool = False
    cancelled: bool = False


class _BackendReplayCreditModel:
    """Backend half-model for scalar STA replay and LSQ credit tracking."""

    def __init__(self, env) -> None:
        self.env = env
        self._sq_size = int(getattr(env.config, "store_queue_size", 56))
        self._lq_size = int(getattr(getattr(env.config, "sequence", object()), "load_queue_size", 72))
        self.reset_runtime_state()

    @property
    def stats(self) -> dict[str, int]:
        lq_occ = len(self._tracked_loads)
        sq_occ = len(self._tracked_stores)
        return {
            "backend_feedback_observed_count": self.feedback_observed_count,
            "backend_feedback_tlb_miss_count": self.feedback_tlb_miss_count,
            "backend_retry_issue_count": self.retry_issue_count,
            "backend_retry_success_count": self.retry_success_count,
            "backend_sq_credit_release_count": self.sq_credit_release_count,
            "backend_lq_credit_release_count": self.lq_credit_release_count,
            "backend_replay_deferred_lane_busy_count": self.replay_deferred_lane_busy_count,
            "backend_replay_cancelled_count": self.replay_cancelled_count,
            "backend_replay_pending_count": len(self._replay_queue),
            "backend_active_store_count": len(self._tracked_stores),
            "backend_active_load_count": len(self._tracked_loads),
            "backend_sq_occupancy": sq_occ,
            "backend_lq_occupancy": lq_occ,
            "backend_sq_credits": max(0, self._sq_size - sq_occ),
            "backend_lq_credits": max(0, self._lq_size - lq_occ),
            "backend_credit_inconsistency_count": len(self._credit_errors),
        }

    def reset_runtime_state(self) -> None:
        self._tracked_stores: dict[tuple[int, int], _TrackedStoreReplay] = {}
        self._tracked_loads: dict[tuple[int, int], RobIndex | None] = {}
        self._replay_queue: deque[tuple[int, int]] = deque()
        self._replay_queued = set()
        self._owned_lane_signatures: dict[int, tuple[int, int, int, int, int, int, int]] = {}
        self._driven_replay_by_lane: dict[int, tuple[int, int]] = {}
        self._predicted_replay_fires: dict[int, tuple[int, int]] = {}
        self._prev_lq_deq_ptr: tuple[int, int] | None = None
        self._prev_sq_deq_ptr: tuple[int, int] | None = None
        self._credit_errors: list[str] = []
        self.feedback_observed_count = 0
        self.feedback_tlb_miss_count = 0
        self.retry_issue_count = 0
        self.retry_success_count = 0
        self.sq_credit_release_count = 0
        self.lq_credit_release_count = 0
        self.replay_deferred_lane_busy_count = 0
        self.replay_cancelled_count = 0

    def note_load_allocated(self, lq_ptr: QueuePtr, rob_idx: RobIndex | None = None) -> None:
        self._tracked_loads[self._ptr_key(lq_ptr.flag, lq_ptr.value)] = rob_idx

    def note_store_allocated(self, sq_ptr: QueuePtr, rob_idx: RobIndex | None = None) -> None:
        entry = self._tracked_stores.setdefault(self._ptr_key(sq_ptr.flag, sq_ptr.value), _TrackedStoreReplay(sq_ptr=sq_ptr))
        entry.rob_idx = rob_idx
        entry.released = False
        entry.cancelled = False

    def note_store_sta_issue(self, op: IssueOp) -> None:
        if not isinstance(op.sq_ptr, QueuePtr):
            raise TypeError(f"store STA issue requires a resolved SQ pointer, got {op.sq_ptr!r}")
        key = self._ptr_key(op.sq_ptr.flag, op.sq_ptr.value)
        entry = self._tracked_stores.setdefault(key, _TrackedStoreReplay(sq_ptr=op.sq_ptr))
        entry.rob_idx = make_rob_index(rob_idx_flag=op.resolved_rob_idx_flag, rob_idx_value=op.resolved_rob_idx_value)
        entry.req_id = int(op.req_id)
        entry.addr = int(op.addr)
        entry.lane = int(op.lane)
        entry.mask = int(op.mask)
        entry.store_opcode = str(op.store_opcode)
        entry.ftq_idx_flag = int(op.ftq_idx_flag) if op.ftq_idx_flag is not None else None
        entry.ftq_idx_value = int(op.ftq_idx_value) if op.ftq_idx_value is not None else None
        entry.awaiting_feedback = True
        entry.replay_pending = False
        self._drop_replay_key(key)

    def drive_pre_step(self) -> None:
        self._clear_owned_replay_lanes()
        self._driven_replay_by_lane = {}
        self._predicted_replay_fires = {}
        busy_lanes = set()
        for key in tuple(self._replay_queue):
            entry = self._tracked_stores.get(key)
            if entry is None or entry.released or entry.cancelled:
                self._drop_replay_key(key)
                continue
            if entry.addr is None or entry.lane is None or entry.rob_idx is None:
                continue
            lane = int(entry.lane)
            if lane in busy_lanes:
                continue
            if not self._lane_is_available(lane):
                self.replay_deferred_lane_busy_count += 1
                continue
            self._drive_sta_replay(entry)
            busy_lanes.add(lane)
            self._driven_replay_by_lane[lane] = key

    def capture_pre_step_handshake(self) -> None:
        self._predicted_replay_fires = {}
        for lane, key in tuple(self._driven_replay_by_lane.items()):
            issue = self.env.issue[int(lane)]
            if int(issue.valid.value) and int(issue.ready.value):
                self._predicted_replay_fires[int(lane)] = key

    def after_cycle(self) -> None:
        for lane, key in tuple(self._predicted_replay_fires.items()):
            del lane
            entry = self._tracked_stores.get(key)
            if entry is None or entry.released or entry.cancelled:
                self._drop_replay_key(key)
                continue
            entry.awaiting_feedback = True
            entry.replay_pending = False
            entry.auto_replay_count += 1
            self.retry_issue_count += 1
            self._drop_replay_key(key)
        self._predicted_replay_fires = {}
        self._driven_replay_by_lane = {}
        self._observe_feedback()
        self._observe_deq_ptrs()

    def is_idle(self) -> bool:
        return not self._replay_queue and not self._driven_replay_by_lane

    def assert_consistent(self) -> None:
        errors = list(self._credit_errors)
        if len(self._tracked_loads) > self._lq_size:
            errors.append(f"LQ occupancy overflow: active={len(self._tracked_loads)}, size={self._lq_size}")
        if len(self._tracked_stores) > self._sq_size:
            errors.append(f"SQ occupancy overflow: active={len(self._tracked_stores)}, size={self._sq_size}")
        if errors:
            raise AssertionError("; ".join(errors))

    def snapshot(self) -> dict:
        tracked_stores = []
        for key, entry in sorted(self._tracked_stores.items()):
            tracked_stores.append(
                {
                    "sq_idx_flag": key[0],
                    "sq_idx_value": key[1],
                    "rob_idx_flag": None if entry.rob_idx is None else int(entry.rob_idx.flag),
                    "rob_idx_value": None if entry.rob_idx is None else int(entry.rob_idx.value),
                    "req_id": entry.req_id,
                    "addr": entry.addr,
                    "lane": entry.lane,
                    "awaiting_feedback": bool(entry.awaiting_feedback),
                    "replay_pending": bool(entry.replay_pending),
                    "auto_replay_count": int(entry.auto_replay_count),
                    "released": bool(entry.released),
                    "cancelled": bool(entry.cancelled),
                }
            )
        return {
            "lq_credits": max(0, self._lq_size - len(self._tracked_loads)),
            "sq_credits": max(0, self._sq_size - len(self._tracked_stores)),
            "lq_occupancy": len(self._tracked_loads),
            "sq_occupancy": len(self._tracked_stores),
            "replay_queue": list(self._replay_queue),
            "driven_replay_by_lane": dict(self._driven_replay_by_lane),
            "tracked_stores": tracked_stores,
            "credit_errors": list(self._credit_errors),
            "stats": self.stats,
        }

    def _clear_owned_replay_lanes(self) -> None:
        for lane, signature in tuple(self._owned_lane_signatures.items()):
            issue = self.env.issue[int(lane)]
            if self._issue_signature(issue) == signature:
                issue.drive_idle()
                self._clear_sta_optional_fields(int(lane))
            del self._owned_lane_signatures[lane]

    def _lane_is_available(self, lane: int) -> bool:
        issue = self.env.issue[int(lane)]
        return int(issue.valid.value) == 0

    def _drive_sta_replay(self, entry: _TrackedStoreReplay) -> None:
        lane = int(entry.lane)
        issue = self.env.issue[lane]
        prefix = f"io_ooo_to_mem_intIssue_{lane}_0_bits_"
        issue.valid.value = 1
        issue.bits_fuOpType.value = IssueOp.sta(
            req_id=0 if entry.req_id is None else entry.req_id,
            sq_ptr=entry.sq_ptr,
            addr=entry.addr,
            lane=lane,
            mask=entry.mask,
            store_opcode=entry.store_opcode,
            rob_idx=entry.rob_idx,
            ftq_idx_flag=entry.ftq_idx_flag,
            ftq_idx_value=entry.ftq_idx_value,
        ).store_fu_op_type
        issue.bits_src_0.value = int(entry.addr)
        issue.bits_robIdx_flag.value = int(entry.rob_idx.flag)
        issue.bits_robIdx_value.value = int(entry.rob_idx.value)
        issue.bits_sqIdx_flag.value = int(entry.sq_ptr.flag)
        issue.bits_sqIdx_value.value = int(entry.sq_ptr.value)
        _set_optional_signal(self.env.dut, f"{prefix}imm", 0)
        _set_optional_signal(self.env.dut, f"{prefix}isFirstIssue", 0)
        _set_optional_signal(self.env.dut, f"{prefix}pdest", 0)
        _set_optional_signal(self.env.dut, f"{prefix}rfWen", 0)
        _set_optional_signal(self.env.dut, f"{prefix}isRVC", 0)
        _set_optional_signal(self.env.dut, f"{prefix}ftqIdx_flag", 0 if entry.ftq_idx_flag is None else int(entry.ftq_idx_flag))
        _set_optional_signal(self.env.dut, f"{prefix}ftqIdx_value", 0 if entry.ftq_idx_value is None else int(entry.ftq_idx_value))
        _set_optional_signal(self.env.dut, f"{prefix}ftqOffset", 0)
        _set_optional_signal(self.env.dut, f"{prefix}storeSetHit", 0)
        _set_optional_signal(self.env.dut, f"{prefix}ssid", 0)
        self._owned_lane_signatures[lane] = self._issue_signature(issue)

    def _clear_sta_optional_fields(self, lane: int) -> None:
        prefix = f"io_ooo_to_mem_intIssue_{lane}_0_bits_"
        _set_optional_signal(self.env.dut, f"{prefix}imm", 0)
        _set_optional_signal(self.env.dut, f"{prefix}isFirstIssue", 0)
        _set_optional_signal(self.env.dut, f"{prefix}pdest", 0)
        _set_optional_signal(self.env.dut, f"{prefix}rfWen", 0)
        _set_optional_signal(self.env.dut, f"{prefix}isRVC", 0)
        _set_optional_signal(self.env.dut, f"{prefix}ftqIdx_flag", 0)
        _set_optional_signal(self.env.dut, f"{prefix}ftqIdx_value", 0)
        _set_optional_signal(self.env.dut, f"{prefix}ftqOffset", 0)
        _set_optional_signal(self.env.dut, f"{prefix}storeSetHit", 0)
        _set_optional_signal(self.env.dut, f"{prefix}ssid", 0)

    def _observe_feedback(self) -> None:
        for bundle in tuple(getattr(self.env, "sta_iq_feedback", ())):
            if bundle.read("valid", 0) == 0:
                continue
            self.feedback_observed_count += 1
            if bundle.read("sourceType", -1) != RS_FEEDBACK_TYPE_TLB_MISS:
                continue
            self.feedback_tlb_miss_count += 1
            if bundle.read("sqIdx_flag", 0) not in (0, 1):
                continue
            key = self._ptr_key(bundle.read("sqIdx_flag", 0), bundle.read("sqIdx_value", -1))
            entry = self._tracked_stores.get(key)
            if entry is None or entry.released or entry.cancelled:
                continue
            entry.awaiting_feedback = False
            if bundle.read("hit", 0):
                if entry.auto_replay_count > 0:
                    self.retry_success_count += 1
                entry.replay_pending = False
                self._drop_replay_key(key)
                continue
            entry.replay_pending = True
            self._enqueue_replay_key(key)

    def _observe_deq_ptrs(self) -> None:
        current_lq_ptr = self._ptr_tuple(
            getattr(self.env.mem_status, "lqDeqPtr_flag", None),
            getattr(self.env.mem_status, "lqDeqPtr_value", None),
        )
        current_sq_ptr = self._ptr_tuple(
            getattr(self.env.mem_status, "sqDeqPtr_flag", None),
            getattr(self.env.mem_status, "sqDeqPtr_value", None),
        )
        if self._prev_lq_deq_ptr is not None and current_lq_ptr is not None and current_lq_ptr != self._prev_lq_deq_ptr:
            for lq_idx in self._ptr_iter(self._prev_lq_deq_ptr, current_lq_ptr, self._lq_size):
                self.lq_credit_release_count += 1
                self._tracked_loads.pop(self._ptr_key(0, lq_idx), None)
                self._tracked_loads.pop(self._ptr_key(1, lq_idx), None)
        if self._prev_sq_deq_ptr is not None and current_sq_ptr is not None and current_sq_ptr != self._prev_sq_deq_ptr:
            for sq_idx in self._ptr_iter(self._prev_sq_deq_ptr, current_sq_ptr, self._sq_size):
                self.sq_credit_release_count += 1
                self._release_store_by_sq_value(sq_idx)
        self._prev_lq_deq_ptr = current_lq_ptr
        self._prev_sq_deq_ptr = current_sq_ptr

    def _release_store_by_sq_value(self, sq_idx_value: int) -> None:
        for key in tuple(self._tracked_stores):
            if int(key[1]) != int(sq_idx_value):
                continue
            entry = self._tracked_stores.pop(key)
            entry.released = True
            if entry.replay_pending or entry.awaiting_feedback:
                entry.cancelled = True
                self.replay_cancelled_count += 1
            self._drop_replay_key(key)

    def _enqueue_replay_key(self, key: tuple[int, int]) -> None:
        if key in self._replay_queued:
            return
        self._replay_queue.append(key)
        self._replay_queued.add(key)

    def _drop_replay_key(self, key: tuple[int, int]) -> None:
        if key not in self._replay_queued:
            return
        self._replay_queued.discard(key)
        self._replay_queue = deque(item for item in self._replay_queue if item != key)

    @staticmethod
    def _ptr_key(flag: int, value: int) -> tuple[int, int]:
        return (int(flag), int(value))

    @staticmethod
    def _ptr_tuple(flag_signal, value_signal):
        if flag_signal is None or value_signal is None:
            return None
        try:
            return (int(flag_signal.value), int(value_signal.value))
        except Exception:
            return None

    @staticmethod
    def _ptr_iter(prev: tuple[int, int], curr: tuple[int, int], size: int) -> list[int]:
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

    @staticmethod
    def _issue_signature(issue) -> tuple[int, int, int, int, int, int, int]:
        return (
            int(issue.valid.value),
            int(issue.bits_fuOpType.value),
            int(issue.bits_src_0.value),
            int(issue.bits_robIdx_flag.value),
            int(issue.bits_robIdx_value.value),
            int(issue.bits_sqIdx_flag.value),
            int(issue.bits_sqIdx_value.value),
        )


class BackendFacade:
    """Coordinate backend-facing active agents behind one semantic API."""

    def __init__(self, env) -> None:
        self.env = env
        self.lsq = env.lsq_agent
        self.issue = env.issue_agent
        self.vector_issue = getattr(env, "vector_issue_agent", None)
        self.rob = env.rob_agent
        self.commit = env.commit_agent
        self.vector_monitor = getattr(env, "vector_monitor", None)
        self._feedback_model = _BackendReplayCreditModel(env)
        self.reset_runtime_state()

    def reset_runtime_state(self) -> None:
        self._next_rob_idx = RobIndex(flag=0, value=0)
        self._feedback_model.reset_runtime_state()

    @property
    def models_feedback_credit_replay(self) -> bool:
        return True

    @property
    def stats(self) -> dict[str, int]:
        return self._feedback_model.stats

    def _validate_rob_idx(self, rob_idx: RobIndex, *, field_name: str) -> RobIndex:
        flag = int(rob_idx.flag)
        value = int(rob_idx.value)
        if flag not in (0, 1):
            raise ValueError(f"{field_name} flag must be 0 or 1, got {flag}")
        if value < 0 or value >= int(self.env.config.rob_size):
            raise ValueError(f"{field_name} value out of range: {value}, rob_size={int(self.env.config.rob_size)}")
        return RobIndex(flag=flag, value=value)

    def _inc_rob_idx(self, rob_idx: RobIndex) -> RobIndex:
        value = int(rob_idx.value) + 1
        flag = int(rob_idx.flag)
        if value >= int(self.env.config.rob_size):
            value = 0
            flag ^= 0x1
        return RobIndex(flag=flag, value=value)

    def _allocate_rob_idx(self) -> RobIndex:
        rob_idx = self._next_rob_idx
        self._next_rob_idx = self._inc_rob_idx(rob_idx)
        return rob_idx

    def _abs_rob_index(self, rob_idx: RobIndex) -> int:
        return int(rob_idx.flag) * int(self.env.config.rob_size) + int(rob_idx.value)

    def _default_scalar_pdest(self, rob_idx: RobIndex) -> int:
        return self._abs_rob_index(rob_idx) % 64

    def _default_vector_pdest(self, rob_idx: RobIndex) -> int:
        return self._abs_rob_index(rob_idx) % 128

    def _default_ftq_idx(self, rob_idx: RobIndex) -> tuple[int, int]:
        return 0, self._abs_rob_index(rob_idx) & 0x3F

    def _default_pc(self, rob_idx: RobIndex) -> int:
        return 0x80000000 + self._abs_rob_index(rob_idx) * 4

    def _normalize_rob_idx_input(
        self,
        rob_idx: RobIndex | int | None = None,
        rob_idx_flag: int | None = None,
        rob_idx_value: int | None = None,
    ) -> RobIndex | None:
        legacy_positional_value = rob_idx_value
        legacy_positional_flag = rob_idx_flag
        if isinstance(rob_idx, int) and rob_idx_value is None and rob_idx_flag is not None:
            legacy_positional_value = int(rob_idx_flag)
            legacy_positional_flag = None
        return make_rob_index(
            rob_idx=rob_idx,
            rob_idx_flag=legacy_positional_flag,
            rob_idx_value=legacy_positional_value,
        )

    def set_next_rob_idx(
        self,
        rob_idx: RobIndex | None = None,
        *,
        rob_idx_flag: int | None = None,
        rob_idx_value: int | None = None,
    ) -> None:
        normalized_rob_idx = self._normalize_rob_idx_input(
            rob_idx=rob_idx,
            rob_idx_flag=rob_idx_flag,
            rob_idx_value=rob_idx_value,
        )
        if normalized_rob_idx is None:
            raise ValueError("set_next_rob_idx requires a rob_idx")
        self._next_rob_idx = self._validate_rob_idx(normalized_rob_idx, field_name="next ROB index")

    def set_commit_frontier(
        self,
        rob_idx: RobIndex | None = None,
        *,
        rob_idx_flag: int | None = None,
        rob_idx_value: int | None = None,
    ) -> None:
        normalized_rob_idx = self._normalize_rob_idx_input(
            rob_idx=rob_idx,
            rob_idx_flag=rob_idx_flag,
            rob_idx_value=rob_idx_value,
        )
        if normalized_rob_idx is None:
            raise ValueError("set_commit_frontier requires a rob_idx")
        self.rob.set_pending_ptr(self._validate_rob_idx(normalized_rob_idx, field_name="ROB commit frontier"))

    def wait_load_enq_ready(self, max_cycles: int = 200) -> None:
        self.lsq.wait_load_enq_ready(max_cycles=max_cycles)

    def wait_store_enq_ready(self, max_cycles: int = 200) -> None:
        self.lsq.wait_store_enq_ready(max_cycles=max_cycles)

    def enqueue_load(
        self,
        req_id: int,
        lq_ptr,
        sq_ptr,
        enq_port: int = 0,
        rob_idx: RobIndex | None = None,
        rob_idx_flag: int | None = None,
        rob_idx_value: int | None = None,
    ) -> None:
        normalized_rob_idx = self._normalize_rob_idx_input(
            rob_idx=rob_idx,
            rob_idx_flag=rob_idx_flag,
            rob_idx_value=rob_idx_value,
        )
        self.lsq.enqueue_scalar_load(
            req_id,
            lq_ptr,
            sq_ptr,
            enq_port=enq_port,
            rob_idx_flag=None if normalized_rob_idx is None else normalized_rob_idx.flag,
            rob_idx_value=None if normalized_rob_idx is None else normalized_rob_idx.value,
        )
        self._feedback_model.note_load_allocated(lq_ptr, normalized_rob_idx)

    def enqueue_scalar_load(
        self,
        req_id: int,
        lq_ptr,
        sq_ptr,
        enq_port: int = 0,
        rob_idx: RobIndex | None = None,
        rob_idx_flag: int | None = None,
        rob_idx_value: int | None = None,
    ) -> None:
        self.enqueue_load(
            req_id,
            lq_ptr,
            sq_ptr,
            enq_port=enq_port,
            rob_idx=rob_idx,
            rob_idx_flag=rob_idx_flag,
            rob_idx_value=rob_idx_value,
        )

    def enqueue_load_cycle(self, plan: EnqueueLoadCyclePlan) -> None:
        self.lsq.enqueue_load_cycle(plan)

    def enqueue_store(
        self,
        req_id: int,
        sq_ptr,
        enq_port: int = 0,
        rob_idx: RobIndex | None = None,
        rob_idx_flag: int | None = None,
        rob_idx_value: int | None = None,
    ):
        normalized_rob_idx = self._normalize_rob_idx_input(
            rob_idx=rob_idx,
            rob_idx_flag=rob_idx_flag,
            rob_idx_value=rob_idx_value,
        )
        return self.lsq.enqueue_scalar_store(
            req_id,
            sq_ptr,
            enq_port=enq_port,
            rob_idx_flag=None if normalized_rob_idx is None else normalized_rob_idx.flag,
            rob_idx_value=None if normalized_rob_idx is None else normalized_rob_idx.value,
        )

    def enqueue_scalar_store(
        self,
        req_id: int,
        sq_ptr,
        enq_port: int = 0,
        rob_idx: RobIndex | None = None,
        rob_idx_flag: int | None = None,
        rob_idx_value: int | None = None,
    ):
        return self.enqueue_store(
            req_id,
            sq_ptr,
            enq_port=enq_port,
            rob_idx=rob_idx,
            rob_idx_flag=rob_idx_flag,
            rob_idx_value=rob_idx_value,
        )

    def _resolve_issue_cycle(self, plan: IssueCyclePlan, result: BackendSendResult) -> IssueCyclePlan:
        resolved_ops = []
        for op in plan.ops:
            resolved_sq_ptr = result.resolve_sq_ptr(op.sq_ptr)
            resolved_ops.append(replace(op, sq_ptr=resolved_sq_ptr))
        return IssueCyclePlan(
            ops=tuple(resolved_ops),
            max_cycles=plan.max_cycles,
            handshake_mode=plan.handshake_mode,
        )

    def _rob_key(self, *, req_id: int | None = None, rob_ref: RobRef | None = None):
        if rob_ref is not None:
            return ("ref", rob_ref)
        if req_id is not None:
            return ("req", int(req_id))
        return None

    def _publish_rob(self, key, rob_idx: RobIndex, req_id_robs: dict[int, RobIndex], ref_robs: dict[RobRef, RobIndex]) -> None:
        key_kind, key_value = key
        if key_kind == "req":
            req_id_robs[int(key_value)] = rob_idx
        elif key_kind == "ref":
            ref_robs[key_value] = rob_idx

    def _bind_txn_runtime(self, txn, rob_idx: RobIndex) -> None:
        if hasattr(txn, "assigned_rob_idx"):
            txn.assigned_rob_idx = rob_idx

        if isinstance(txn, LoadTxn):
            if txn.pdest is None:
                txn.assigned_pdest = self._default_scalar_pdest(rob_idx)
            if txn.ftq_idx_flag is None:
                txn.assigned_ftq_idx_flag = 0
            if txn.ftq_idx_value is None:
                txn.assigned_ftq_idx_value = self._default_ftq_idx(rob_idx)[1]
            if txn.pc is None:
                txn.assigned_pc = self._default_pc(rob_idx)
        elif isinstance(txn, StoreTxn):
            if txn.ftq_idx_flag is None:
                txn.assigned_ftq_idx_flag = 0
            if txn.ftq_idx_value is None:
                txn.assigned_ftq_idx_value = self._default_ftq_idx(rob_idx)[1]
        elif isinstance(txn, VectorMemTxn):
            if txn.pdest is None:
                txn.assigned_pdest = self._default_vector_pdest(rob_idx)
            if txn.ftq_idx_flag is None:
                txn.assigned_ftq_idx_flag = 0
            if txn.ftq_idx_value is None:
                txn.assigned_ftq_idx_value = self._default_ftq_idx(rob_idx)[1]

    def _explicit_rob_idx(self, owner) -> RobIndex | None:
        assigned = getattr(owner, "assigned_rob_idx", None)
        if assigned is not None:
            return RobIndex(flag=int(assigned.flag), value=int(assigned.value))
        override = getattr(owner, "rob_idx_override", None)
        if override is not None:
            return RobIndex(flag=int(override.flag), value=int(override.value))
        if isinstance(owner, (LoadTxn, StoreTxn, VectorMemTxn)):
            return None
        return make_rob_index(
            rob_idx=getattr(owner, "rob_idx", None),
            rob_idx_flag=getattr(owner, "rob_idx_flag", None),
            rob_idx_value=getattr(owner, "rob_idx_value", None),
        )

    def _ensure_rob_idx(self, key, *, explicit: RobIndex | None, key_robs: dict, req_id_robs: dict[int, RobIndex], ref_robs: dict[RobRef, RobIndex]) -> RobIndex:
        if key in key_robs:
            rob_idx = key_robs[key]
            if explicit is not None and explicit != rob_idx:
                raise ValueError(f"conflicting robIdx binding for {key}: {rob_idx} vs {explicit}")
            return rob_idx
        rob_idx = explicit if explicit is not None else self._allocate_rob_idx()
        key_robs[key] = rob_idx
        self._publish_rob(key, rob_idx, req_id_robs, ref_robs)
        return rob_idx

    def _resolve_rob_target(self, target, *, key_robs: dict, req_id_robs: dict[int, RobIndex], ref_robs: dict[RobRef, RobIndex]) -> RobIndex:
        if isinstance(target, RobRef):
            key = self._rob_key(rob_ref=target)
            return self._ensure_rob_idx(key, explicit=None, key_robs=key_robs, req_id_robs=req_id_robs, ref_robs=ref_robs)
        if isinstance(target, int):
            key = self._rob_key(req_id=int(target))
            return self._ensure_rob_idx(key, explicit=None, key_robs=key_robs, req_id_robs=req_id_robs, ref_robs=ref_robs)
        if hasattr(target, "rob_ref") and getattr(target, "rob_ref") is not None:
            key = self._rob_key(rob_ref=getattr(target, "rob_ref"))
        elif hasattr(target, "req_id"):
            key = self._rob_key(req_id=int(getattr(target, "req_id")))
        else:
            raise TypeError(f"unsupported rob target: {target!r}")
        rob_idx = self._ensure_rob_idx(
            key,
            explicit=self._explicit_rob_idx(target),
            key_robs=key_robs,
            req_id_robs=req_id_robs,
            ref_robs=ref_robs,
        )
        self._bind_txn_runtime(target, rob_idx)
        return rob_idx

    def _prepare_issue_op(self, op: IssueOp, *, key_robs: dict, req_id_robs: dict[int, RobIndex], ref_robs: dict[RobRef, RobIndex]) -> IssueOp:
        key = self._rob_key(req_id=op.req_id, rob_ref=op.rob_ref)
        rob_idx = self._ensure_rob_idx(
            key,
            explicit=self._explicit_rob_idx(op),
            key_robs=key_robs,
            req_id_robs=req_id_robs,
            ref_robs=ref_robs,
        )
        if op.txn is not None:
            self._bind_txn_runtime(op.txn, rob_idx)

        wait_rob = op.wait_for_rob_idx
        if wait_rob is None and op.wait_for_rob is not None:
            wait_rob = self._resolve_rob_target(
                op.wait_for_rob,
                key_robs=key_robs,
                req_id_robs=req_id_robs,
                ref_robs=ref_robs,
            )

        ftq_idx_flag = op.ftq_idx_flag
        ftq_idx_value = op.ftq_idx_value
        if ftq_idx_flag is None:
            ftq_idx_flag = 0
        if ftq_idx_value is None:
            ftq_idx_value = self._default_ftq_idx(rob_idx)[1]

        pdest = op.pdest
        if op.kind == "load" and pdest is None:
            pdest = self._default_scalar_pdest(rob_idx)

        pc = op.pc
        if op.kind == "load" and pc is None:
            pc = self._default_pc(rob_idx)

        return replace(
            op,
            rob_idx_flag=int(rob_idx.flag),
            rob_idx_value=int(rob_idx.value),
            wait_for_rob_idx_flag=None if wait_rob is None else int(wait_rob.flag),
            wait_for_rob_idx_value=None if wait_rob is None else int(wait_rob.value),
            pdest=pdest,
            ftq_idx_flag=ftq_idx_flag,
            ftq_idx_value=ftq_idx_value,
            pc=pc,
        )

    def _prepare_plan(self, plan: BackendSendPlan) -> BackendPreparedPlan:
        key_robs: dict[tuple[str, object], RobIndex] = {}
        req_id_robs: dict[int, RobIndex] = {}
        ref_robs: dict[RobRef, RobIndex] = {}
        resolved_steps = []

        for step in plan.steps:
            if isinstance(step, EnqueueLoadStep):
                key = self._rob_key(req_id=step.req_id, rob_ref=step.rob_ref)
                rob_idx = self._ensure_rob_idx(
                    key,
                    explicit=self._explicit_rob_idx(step),
                    key_robs=key_robs,
                    req_id_robs=req_id_robs,
                    ref_robs=ref_robs,
                )
                if step.txn is not None:
                    self._bind_txn_runtime(step.txn, rob_idx)
                resolved_steps.append(replace(step, rob_idx_flag=rob_idx.flag, rob_idx_value=rob_idx.value))
            elif isinstance(step, EnqueueLoadCyclePlan):
                resolved_cycle_steps = []
                for load_step in step.steps:
                    key = self._rob_key(req_id=load_step.req_id, rob_ref=load_step.rob_ref)
                    rob_idx = self._ensure_rob_idx(
                        key,
                        explicit=self._explicit_rob_idx(load_step),
                        key_robs=key_robs,
                        req_id_robs=req_id_robs,
                        ref_robs=ref_robs,
                    )
                    if load_step.txn is not None:
                        self._bind_txn_runtime(load_step.txn, rob_idx)
                    resolved_cycle_steps.append(
                        replace(load_step, rob_idx_flag=rob_idx.flag, rob_idx_value=rob_idx.value)
                    )
                resolved_steps.append(replace(step, steps=tuple(resolved_cycle_steps)))
            elif isinstance(step, EnqueueStoreStep):
                key = self._rob_key(req_id=step.req_id, rob_ref=step.rob_ref)
                rob_idx = self._ensure_rob_idx(
                    key,
                    explicit=self._explicit_rob_idx(step),
                    key_robs=key_robs,
                    req_id_robs=req_id_robs,
                    ref_robs=ref_robs,
                )
                if step.txn is not None:
                    self._bind_txn_runtime(step.txn, rob_idx)
                resolved_steps.append(replace(step, rob_idx_flag=rob_idx.flag, rob_idx_value=rob_idx.value))
            elif isinstance(step, IssueCyclePlan):
                resolved_steps.append(
                    replace(
                        step,
                        ops=tuple(
                            self._prepare_issue_op(
                                op,
                                key_robs=key_robs,
                                req_id_robs=req_id_robs,
                                ref_robs=ref_robs,
                            )
                            for op in step.ops
                        ),
                    )
                )
            elif isinstance(step, VectorEnqueueStep):
                rob_idx = self._resolve_rob_target(
                    step.txn,
                    key_robs=key_robs,
                    req_id_robs=req_id_robs,
                    ref_robs=ref_robs,
                )
                self._bind_txn_runtime(step.txn, rob_idx)
                resolved_steps.append(step)
            elif isinstance(step, VectorIssueStep):
                rob_idx = self._resolve_rob_target(
                    step.txn,
                    key_robs=key_robs,
                    req_id_robs=req_id_robs,
                    ref_robs=ref_robs,
                )
                self._bind_txn_runtime(step.txn, rob_idx)
                resolved_steps.append(step)
            elif isinstance(step, NonMemBlockerStep):
                key = self._rob_key(req_id=step.req_id, rob_ref=step.rob_ref)
                if key is None:
                    resolved_steps.append(step)
                    continue
                rob_idx = self._ensure_rob_idx(
                    key,
                    explicit=self._explicit_rob_idx(step),
                    key_robs=key_robs,
                    req_id_robs=req_id_robs,
                    ref_robs=ref_robs,
                )
                resolved_steps.append(replace(step, rob_idx_flag=rob_idx.flag, rob_idx_value=rob_idx.value))
            else:
                resolved_steps.append(step)

        return BackendPreparedPlan(
            resolved_plan=BackendSendPlan.from_steps(*resolved_steps),
            req_id_robs=req_id_robs,
            ref_robs=ref_robs,
        )

    def prepare(self, request) -> BackendPreparedPlan:
        if isinstance(request, BackendSendPlan):
            return self._prepare_plan(request)
        if isinstance(request, LoadTxn):
            prepared = self._prepare_plan(
                BackendSendPlan.from_steps(
                    EnqueueLoadStep.from_txn(request),
                    IssueCyclePlan.from_ops(IssueOp.load_from_txn(request)),
                )
            )
            self._bind_txn_runtime(request, prepared.rob_idx_of(request))
            return prepared
        if isinstance(request, StoreTxn):
            store_ref = StoreRef(name=f"store_{request.req_id}")
            prepared = self._prepare_plan(
                BackendSendPlan.from_steps(
                    EnqueueStoreStep.from_txn(request, ref=store_ref),
                    IssueCyclePlan.from_ops(
                        IssueOp.std(
                            req_id=request.req_id,
                            sq_ptr=store_ref,
                            data=request.issue_data,
                            lane=request.std_lane,
                            mask=request.mask,
                            store_opcode=request.opcode,
                            rob_ref=request.rob_ref,
                        )
                    ),
                    IssueCyclePlan.from_ops(
                        IssueOp.sta(
                            req_id=request.req_id,
                            sq_ptr=store_ref,
                            addr=request.addr,
                            lane=request.sta_lane,
                            mask=request.mask,
                            store_opcode=request.opcode,
                            rob_ref=request.rob_ref,
                        )
                    ),
                )
            )
            self._bind_txn_runtime(request, prepared.rob_idx_of(request))
            return prepared
        if isinstance(request, VectorMemTxn):
            if not hasattr(self.env, "vector_backend"):
                raise RuntimeError("vector backend facade is not attached to env")
            return self._prepare_plan(self.env.vector_backend.default_plan(request))
        raise TypeError(f"unsupported backend prepare request: {type(request)!r}")

    def _register_vector_txn(self, txn: VectorMemTxn) -> None:
        if self.vector_monitor is None:
            return
        try:
            self.vector_monitor.register_req(txn.req_id, rob_idx=txn.rob_idx)
        except TypeError as exc:
            if "rob_idx" not in str(exc):
                raise
            self.vector_monitor.register_req(txn.req_id, txn.rob_idx.flag, txn.rob_idx.value)

    def _execute_prepared(self, prepared: BackendPreparedPlan) -> BackendSendResult:
        plan = prepared.resolved_plan
        store_ptrs: dict[StoreRef, QueuePtr] = {}
        vector_sq_ptrs: dict[int, QueuePtr] = {}
        vector_results = {}
        result = BackendSendResult(store_ptrs=store_ptrs, vector_results=vector_results)
        for step in plan.steps:
            if isinstance(step, EnqueueLoadStep):
                self.enqueue_load(
                    req_id=step.req_id,
                    lq_ptr=step.lq_ptr,
                    sq_ptr=step.sq_ptr,
                    enq_port=step.enq_port,
                    rob_idx=step.resolved_rob_idx,
                )
            elif isinstance(step, EnqueueLoadCyclePlan):
                self.enqueue_load_cycle(step)
                for load_step in step.steps:
                    self._feedback_model.note_load_allocated(load_step.lq_ptr, load_step.resolved_rob_idx)
            elif isinstance(step, EnqueueStoreStep):
                allocated_sq_ptr = self.enqueue_store(
                    req_id=step.req_id,
                    sq_ptr=step.sq_ptr,
                    enq_port=step.enq_port,
                    rob_idx=step.resolved_rob_idx,
                )
                if step.ref is not None:
                    store_ptrs[step.ref] = allocated_sq_ptr
            elif isinstance(step, VectorEnqueueStep):
                self._register_vector_txn(step.txn)
                allocated_sq_ptr = self.enqueue_vector_mem(step.txn)
                if allocated_sq_ptr is not None:
                    vector_sq_ptrs[step.txn.req_id] = allocated_sq_ptr
            elif isinstance(step, IssueCyclePlan):
                resolved_cycle = self._resolve_issue_cycle(step, result)
                self.issue.issue_cycle(resolved_cycle)
                self._note_issue_cycle_store_progress(resolved_cycle)
            elif isinstance(step, VectorIssueStep):
                resolved_txn = step.txn
                if step.txn.req_id in vector_sq_ptrs:
                    resolved_txn = replace(step.txn, sq_ptr=vector_sq_ptrs[step.txn.req_id])
                self.issue_vector_mem(resolved_txn, max_cycles=step.max_cycles)
            elif isinstance(step, StoreCommitStep):
                self.step_commit(count=step.count, cycles=step.cycles)
            elif isinstance(step, NonMemBlockerStep):
                if step.action == "insert":
                    self.insert_non_mem_blocker(rob_idx=step.rob_idx)
                else:
                    self.release_non_mem_blocker(rob_idx=step.rob_idx)
            elif isinstance(step, StoreCommitReadyStep):
                resolved_sq_ptr = result.resolve_sq_ptr(step.sq_ptr)
                self.mark_store_commit_ready(
                    sq_idx_flag=resolved_sq_ptr.flag,
                    sq_idx_value=resolved_sq_ptr.value,
                    ready=step.ready,
                )
            elif isinstance(step, VectorWaitStep):
                vector_results[step.req_id] = self.wait_vector_event(
                    req_id=step.req_id,
                    event=step.event,
                    max_cycles=step.max_cycles,
                )
            else:
                raise TypeError(f"unsupported backend send step: {type(step)!r}")
        return result

    def execute(self, plan: BackendSendPlan | BackendPreparedPlan) -> BackendSendResult:
        if isinstance(plan, BackendPreparedPlan):
            return self._execute_prepared(plan)
        return self._execute_prepared(self.prepare(plan))

    def send(self, request):
        if isinstance(request, (BackendSendPlan, BackendPreparedPlan)):
            return self.execute(request)
        if isinstance(request, VectorMemTxn):
            if not hasattr(self.env, "vector_backend"):
                raise RuntimeError("vector backend facade is not attached to env")
            return self.execute(self.prepare(request))
        if isinstance(request, LoadTxn):
            self.execute(self.prepare(request))
            return None
        if isinstance(request, StoreTxn):
            prepared = self.prepare(request)
            store_ref = next(
                step.ref
                for step in prepared.resolved_plan.steps
                if isinstance(step, EnqueueStoreStep) and step.ref is not None and step.req_id == request.req_id
            )
            result = self.execute(prepared)
            allocated_sq_ptr = result.resolve_sq_ptr(store_ref)
            if hasattr(self.env.memory, "note_store_request"):
                self.env.memory.note_store_request(
                    sq_idx=allocated_sq_ptr.value,
                    addr=request.addr,
                    data=request.issue_data,
                    mask=request.mask,
                    opcode=request.opcode,
                )
            return allocated_sq_ptr
        raise TypeError(f"unsupported backend send request: {type(request)!r}")

    def send_many(self, requests):
        return [self.send(request) for request in requests]

    def _note_issue_cycle_store_progress(self, plan: IssueCyclePlan) -> None:
        for op in plan.ops:
            if not isinstance(op.sq_ptr, QueuePtr):
                raise TypeError(f"unresolved SQ pointer in issue cycle: {op.sq_ptr!r}")
            if op.kind == "sta":
                self._feedback_model.note_store_sta_issue(op)
                self.commit.mark_store_addr_ready(op.sq_ptr.flag, op.sq_ptr.value)
            elif op.kind == "std":
                self.commit.mark_store_data_ready(op.sq_ptr.flag, op.sq_ptr.value)

    def issue_load(
        self,
        req_id: int,
        addr: int,
        lq_ptr,
        sq_ptr,
        lane: int = 0,
        size: int = 8,
        mask: int | None = None,
        fp_wen: int = 0,
        store_set_hit: int = 0,
        load_wait_bit: int = 0,
        load_wait_strict: int = 0,
        wait_for_rob_idx: RobIndex | None = None,
        wait_for_rob_idx_flag: int | None = None,
        wait_for_rob_idx_value: int | None = None,
        rob_idx: RobIndex | None = None,
        rob_idx_flag: int | None = None,
        rob_idx_value: int | None = None,
        pdest: int | None = None,
        ftq_idx_flag: int | None = None,
        ftq_idx_value: int | None = None,
        pc: int | None = None,
    ) -> None:
        self.issue_scalar_load(
            req_id=req_id,
            addr=addr,
            lq_ptr=lq_ptr,
            sq_ptr=sq_ptr,
            lane=lane,
            size=size,
            mask=mask,
            fp_wen=fp_wen,
            store_set_hit=store_set_hit,
            load_wait_bit=load_wait_bit,
            load_wait_strict=load_wait_strict,
            wait_for_rob_idx=wait_for_rob_idx,
            wait_for_rob_idx_flag=wait_for_rob_idx_flag,
            wait_for_rob_idx_value=wait_for_rob_idx_value,
            rob_idx=rob_idx,
            rob_idx_flag=rob_idx_flag,
            rob_idx_value=rob_idx_value,
            pdest=pdest,
            ftq_idx_flag=ftq_idx_flag,
            ftq_idx_value=ftq_idx_value,
            pc=pc,
        )

    def issue_scalar_load(
        self,
        req_id: int,
        addr: int,
        lq_ptr,
        sq_ptr,
        lane: int = 0,
        size: int = 8,
        mask: int | None = None,
        fp_wen: int = 0,
        store_set_hit: int = 0,
        load_wait_bit: int = 0,
        load_wait_strict: int = 0,
        wait_for_rob_idx: RobIndex | None = None,
        wait_for_rob_idx_flag: int | None = None,
        wait_for_rob_idx_value: int | None = None,
        rob_idx: RobIndex | None = None,
        rob_idx_flag: int | None = None,
        rob_idx_value: int | None = None,
        pdest: int | None = None,
        ftq_idx_flag: int | None = None,
        ftq_idx_value: int | None = None,
        pc: int | None = None,
    ) -> None:
        self.issue.issue_cycle(
            IssueCyclePlan.from_ops(
                IssueOp.load(
                    req_id=req_id,
                    addr=addr,
                    lq_ptr=lq_ptr,
                    sq_ptr=sq_ptr,
                    lane=lane,
                    size=size,
                    mask=mask,
                    fp_wen=fp_wen,
                    store_set_hit=store_set_hit,
                    load_wait_bit=load_wait_bit,
                    load_wait_strict=load_wait_strict,
                    wait_for_rob_idx=wait_for_rob_idx,
                    wait_for_rob_idx_flag=wait_for_rob_idx_flag,
                    wait_for_rob_idx_value=wait_for_rob_idx_value,
                    rob_idx=rob_idx,
                    rob_idx_flag=rob_idx_flag,
                    rob_idx_value=rob_idx_value,
                    pdest=pdest,
                    ftq_idx_flag=ftq_idx_flag,
                    ftq_idx_value=ftq_idx_value,
                    pc=pc,
                )
            )
        )

    def issue_load_batch_same_cycle(self, txns, max_cycles: int = 50) -> None:
        txns = tuple(txns)
        self.issue.issue_cycle(
            IssueCyclePlan(
                ops=tuple(IssueOp.load_from_txn(txn) for txn in txns),
                max_cycles=max_cycles,
            )
        )

    def issue_scalar_load_batch_same_cycle(self, txns, max_cycles: int = 50) -> None:
        self.issue_load_batch_same_cycle(txns, max_cycles=max_cycles)

    def issue_load_batch_with_sta_same_cycle(
        self,
        txns,
        *,
        sta_req_id: int,
        sta_sq_ptr,
        sta_addr: int,
        sta_lane: int = 3,
        sta_mask: int = 0xFF,
        max_cycles: int = 50,
    ) -> None:
        txns = tuple(txns)
        plan = IssueCyclePlan(
            ops=tuple(IssueOp.load_from_txn(txn) for txn in txns)
            + (
                IssueOp.sta(
                    req_id=sta_req_id,
                    sq_ptr=sta_sq_ptr,
                    addr=sta_addr,
                    lane=sta_lane,
                    mask=sta_mask,
                ),
            ),
            max_cycles=max_cycles,
        )
        self.issue.issue_cycle(plan)
        self._note_issue_cycle_store_progress(plan)

    def issue_scalar_load_batch_with_sta_same_cycle(
        self,
        txns,
        *,
        sta_req_id: int,
        sta_sq_ptr,
        sta_addr: int,
        sta_lane: int = 3,
        sta_mask: int = 0xFF,
        max_cycles: int = 50,
    ) -> None:
        self.issue_load_batch_with_sta_same_cycle(
            txns,
            sta_req_id=sta_req_id,
            sta_sq_ptr=sta_sq_ptr,
            sta_addr=sta_addr,
            sta_lane=sta_lane,
            sta_mask=sta_mask,
            max_cycles=max_cycles,
        )

    def issue_std(
        self,
        req_id: int,
        sq_ptr,
        data: int,
        lane: int = 5,
        mask: int = 0xFF,
        rob_idx: RobIndex | None = None,
        rob_idx_flag: int | None = None,
        rob_idx_value: int | None = None,
        ftq_idx_flag: int | None = None,
        ftq_idx_value: int | None = None,
    ) -> None:
        plan = IssueCyclePlan.from_ops(
            IssueOp.std(
                req_id=req_id,
                sq_ptr=sq_ptr,
                data=data,
                lane=lane,
                mask=mask,
                rob_idx=rob_idx,
                rob_idx_flag=rob_idx_flag,
                rob_idx_value=rob_idx_value,
                ftq_idx_flag=ftq_idx_flag,
                ftq_idx_value=ftq_idx_value,
            )
        )
        self.issue.issue_cycle(plan)
        self._note_issue_cycle_store_progress(plan)

    def issue_scalar_std(
        self,
        req_id: int,
        sq_ptr,
        data: int,
        lane: int = 5,
        mask: int = 0xFF,
        rob_idx: RobIndex | None = None,
        rob_idx_flag: int | None = None,
        rob_idx_value: int | None = None,
        ftq_idx_flag: int | None = None,
        ftq_idx_value: int | None = None,
    ) -> None:
        self.issue_std(
            req_id,
            sq_ptr,
            data,
            lane=lane,
            mask=mask,
            rob_idx=rob_idx,
            rob_idx_flag=rob_idx_flag,
            rob_idx_value=rob_idx_value,
            ftq_idx_flag=ftq_idx_flag,
            ftq_idx_value=ftq_idx_value,
        )

    def issue_sta(
        self,
        req_id: int,
        sq_ptr,
        addr: int,
        lane: int = 3,
        mask: int = 0xFF,
        rob_idx: RobIndex | None = None,
        rob_idx_flag: int | None = None,
        rob_idx_value: int | None = None,
        ftq_idx_flag: int | None = None,
        ftq_idx_value: int | None = None,
    ) -> None:
        plan = IssueCyclePlan.from_ops(
            IssueOp.sta(
                req_id=req_id,
                sq_ptr=sq_ptr,
                addr=addr,
                lane=lane,
                mask=mask,
                rob_idx=rob_idx,
                rob_idx_flag=rob_idx_flag,
                rob_idx_value=rob_idx_value,
                ftq_idx_flag=ftq_idx_flag,
                ftq_idx_value=ftq_idx_value,
            )
        )
        self.issue.issue_cycle(plan)
        self._note_issue_cycle_store_progress(plan)

    def issue_scalar_sta(
        self,
        req_id: int,
        sq_ptr,
        addr: int,
        lane: int = 3,
        mask: int = 0xFF,
        rob_idx: RobIndex | None = None,
        rob_idx_flag: int | None = None,
        rob_idx_value: int | None = None,
        ftq_idx_flag: int | None = None,
        ftq_idx_value: int | None = None,
    ) -> None:
        self.issue_sta(
            req_id,
            sq_ptr,
            addr,
            lane=lane,
            mask=mask,
            rob_idx=rob_idx,
            rob_idx_flag=rob_idx_flag,
            rob_idx_value=rob_idx_value,
            ftq_idx_flag=ftq_idx_flag,
            ftq_idx_value=ftq_idx_value,
        )

    def send_load(self, txn: LoadTxn) -> None:
        self.send(txn)

    def enqueue_vector_mem(self, txn: VectorMemTxn):
        return self.lsq.enqueue_vector_mem(txn)

    def issue_vector_mem(self, txn: VectorMemTxn, max_cycles: int = 50) -> None:
        if self.vector_issue is None:
            raise RuntimeError("vector issue agent is not attached to env")
        self.vector_issue.issue(txn, max_cycles=max_cycles)

    def wait_vector_event(self, *, req_id: int, event: str = "complete_or_trap", max_cycles: int = 200):
        if self.vector_monitor is None:
            raise RuntimeError("vector monitor is not attached to env")
        return self.env._run_async(
            self.vector_monitor.wait_event_async(
                req_id=req_id,
                event=event,
                max_cycles=max_cycles,
            )
        )

    def send_load_batch_same_cycle(self, txns, max_cycles: int = 50) -> None:
        txns = tuple(txns)
        self.execute(
            BackendSendPlan.from_steps(
                EnqueueLoadCyclePlan.from_txns(*txns),
                IssueCyclePlan(
                    ops=tuple(IssueOp.load_from_txn(txn) for txn in txns),
                    max_cycles=max_cycles,
                ),
            )
        )

    def send_load_batch_with_sta_same_cycle(
        self,
        txns,
        *,
        sta_req_id: int,
        sta_sq_ptr,
        sta_addr: int,
        sta_lane: int = 3,
        max_cycles: int = 50,
    ) -> None:
        txns = tuple(txns)
        self.execute(
            BackendSendPlan.from_steps(
                EnqueueLoadCyclePlan.from_txns(*txns),
                IssueCyclePlan(
                    ops=tuple(IssueOp.load_from_txn(txn) for txn in txns)
                    + (
                        IssueOp.sta(
                            req_id=sta_req_id,
                            sq_ptr=sta_sq_ptr,
                            addr=sta_addr,
                            lane=sta_lane,
                        ),
                    ),
                    max_cycles=max_cycles,
                ),
            )
        )

    def send_store(self, txn: StoreTxn):
        return self.send(txn)

    def send_cbo(self, txn: StoreTxn):
        if not txn.is_cbo:
            raise ValueError("send_cbo requires a StoreTxn with a CBO opcode")
        return self.send(txn)

    def send_cbo_zero(self, txn: StoreTxn):
        if not txn.is_cbo_zero:
            raise ValueError("send_cbo_zero requires a StoreTxn with opcode='cbo_zero'")
        return self.send_cbo(txn)

    def note_load_issued(
        self,
        rob_idx: RobIndex | None = None,
        rob_idx_flag: int | None = None,
        rob_idx_value: int | None = None,
    ) -> None:
        normalized_rob_idx = self._normalize_rob_idx_input(
            rob_idx=rob_idx,
            rob_idx_flag=rob_idx_flag,
            rob_idx_value=rob_idx_value,
        )
        if normalized_rob_idx is None:
            raise ValueError("note_load_issued requires a rob_idx")
        self.commit.note_load_issued(normalized_rob_idx.flag, normalized_rob_idx.value)
        self.env.memory.note_load_issued(normalized_rob_idx.flag, normalized_rob_idx.value)

    def note_store_allocated(
        self,
        sq_idx_flag: int,
        sq_idx_value: int,
        rob_idx: RobIndex | None = None,
        rob_idx_flag: int | None = None,
        rob_idx_value: int | None = None,
    ) -> None:
        normalized_rob_idx = self._normalize_rob_idx_input(
            rob_idx=rob_idx,
            rob_idx_flag=rob_idx_flag,
            rob_idx_value=rob_idx_value,
        )
        if normalized_rob_idx is None:
            raise ValueError("note_store_allocated requires a rob_idx")
        self.commit.note_store_allocated(
            normalized_rob_idx.flag,
            normalized_rob_idx.value,
            sq_idx_flag=sq_idx_flag,
            sq_idx_value=sq_idx_value,
        )
        self.env.memory.note_store_allocated(
            sq_idx_flag=sq_idx_flag,
            sq_idx_value=sq_idx_value,
            rob_idx_flag=normalized_rob_idx.flag,
            rob_idx_value=normalized_rob_idx.value,
        )
        self._feedback_model.note_store_allocated(
            QueuePtr(flag=int(sq_idx_flag), value=int(sq_idx_value)),
            normalized_rob_idx,
        )

    def note_load_completed(
        self,
        rob_idx: RobIndex | None = None,
        rob_idx_flag: int | None = None,
        rob_idx_value: int | None = None,
    ) -> None:
        normalized_rob_idx = self._normalize_rob_idx_input(
            rob_idx=rob_idx,
            rob_idx_flag=rob_idx_flag,
            rob_idx_value=rob_idx_value,
        )
        if normalized_rob_idx is None:
            raise ValueError("note_load_completed requires a rob_idx")
        self.commit.note_load_completed(normalized_rob_idx.flag, normalized_rob_idx.value)

    def insert_non_mem_blocker(
        self,
        rob_idx: RobIndex | None = None,
        rob_idx_flag: int | None = None,
        rob_idx_value: int | None = None,
    ) -> None:
        normalized_rob_idx = self._normalize_rob_idx_input(
            rob_idx=rob_idx,
            rob_idx_flag=rob_idx_flag,
            rob_idx_value=rob_idx_value,
        )
        if normalized_rob_idx is None:
            raise ValueError("insert_non_mem_blocker requires a rob_idx")
        self.commit.note_non_mem_issued(normalized_rob_idx.flag, normalized_rob_idx.value)

    def release_non_mem_blocker(
        self,
        rob_idx: RobIndex | None = None,
        rob_idx_flag: int | None = None,
        rob_idx_value: int | None = None,
    ) -> None:
        normalized_rob_idx = self._normalize_rob_idx_input(
            rob_idx=rob_idx,
            rob_idx_flag=rob_idx_flag,
            rob_idx_value=rob_idx_value,
        )
        if normalized_rob_idx is None:
            raise ValueError("release_non_mem_blocker requires a rob_idx")
        self.commit.release_non_mem(normalized_rob_idx.flag, normalized_rob_idx.value)

    def mark_store_commit_ready(self, sq_idx_flag: int, sq_idx_value: int, ready: bool = True) -> None:
        self.commit.mark_store_commit_ready(sq_idx_flag, sq_idx_value, ready=ready)

    def queue_store_commit(self, count: int = 1) -> None:
        self.commit.queue_store_commit(count)

    def step_commit(self, count: int = 1, cycles: int = 1) -> None:
        self.queue_store_commit(count)
        self.env._run_async(self.env._await_cycles(cycles))

    def pulse_store_commit(self, count: int = 1) -> None:
        self.step_commit(count=count, cycles=1)

    def flush_store_buffers_and_wait(self, max_cycles: int = 200, settle_cycles: int = 4) -> dict:
        return self.env._flush_store_buffers_and_wait_impl(
            max_cycles=max_cycles,
            settle_cycles=settle_cycles,
        )

    def drive_pre_step(self) -> None:
        self._feedback_model.drive_pre_step()

    def capture_pre_step_handshake(self) -> None:
        self._feedback_model.capture_pre_step_handshake()

    def after_cycle(self) -> None:
        self._feedback_model.after_cycle()

    def backend_state(self) -> dict:
        return self._feedback_model.snapshot()

    def wait_replay_idle(self, max_cycles: int = 200) -> None:
        self.env.wait_until(
            lambda: self._feedback_model.is_idle(),
            max_cycles=max_cycles,
            timeout_message="等待 backend replay 队列清空超时",
        )

    def assert_credit_consistent(self) -> None:
        self._feedback_model.assert_consistent()
