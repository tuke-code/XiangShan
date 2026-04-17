# coding=utf-8
"""
MemBlock load-pipeline-oriented probe sequences.
"""

from __future__ import annotations

from collections import deque
from contextlib import contextmanager
from dataclasses import dataclass

from request_apis import (
    LoadTxn,
    StoreTxn,
    ptr_inc,
)
from transactions import (
    BackendSendPlan,
    EnqueueLoadCyclePlan,
    EnqueueLoadStep,
    EnqueueStoreStep,
    IssueCyclePlan,
    IssueOp,
    StoreCommitStep,
    StoreRef,
)

from .memblock_sequences import (
    SequenceState,
    _capture_load_debug_trace,
    _capture_writeback_trace,
    _resolve_replay_drain_cycles,
    _sample_sq_forward_events,
    _snapshot_transport_stats,
    _wait_completed_load_count,
)
from .violation_sequences import _capture_replay_events


BC_REPLAY_CAUSE_BIT = 8
REPLAY_CAUSE_BITS = (0, 1, 2, 3, 4, 5, 6, 8, 9, 10, 11)


@dataclass(frozen=True)
class LoadPipelineBankConflictEvent:
    cycle: int
    lane: int
    rob_idx: int
    bank_conflict: int
    dcache_first_miss: int
    replay_slow: int
    replay_bit_bc: int


@dataclass(frozen=True)
class LoadPipelineWakeupEvent:
    cycle: int
    lane: int
    pdest: int
    rf_wen: int


@dataclass(frozen=True)
class LoadPipelineFastReplayEvent:
    cycle: int
    lane: int
    rob_idx: int
    is_fast_replay: int
    is_slow_replay: int
    cancel: int
    ld_cancel: int
    replay_causes: tuple[int, ...]


@dataclass(frozen=True)
class LoadPipelineReplayQueueEvent:
    cycle: int
    source: str
    lane: int | None
    rob_idx_flag: int
    rob_idx_value: int
    sched_index: int | None
    cause: str | None


@dataclass(frozen=True)
class ScalarBankConflictLoadClusterSequenceResult:
    txns: tuple[LoadTxn, ...]
    writebacks: tuple[dict, ...]
    wakeups: tuple[LoadPipelineWakeupEvent, ...]
    bank_conflicts: tuple[LoadPipelineBankConflictEvent, ...]
    fast_replay_events: tuple[LoadPipelineFastReplayEvent, ...]
    replay_queue_events: tuple[LoadPipelineReplayQueueEvent, ...]
    sq_forward_events: tuple[dict, ...]
    violation_event: dict | None
    replay_events: tuple[dict, ...]
    transport_stats_before: dict[str, int]
    transport_stats_after: dict[str, int]
    completed_load_count: int


@dataclass(frozen=True)
class ScalarLateStaStoreLoadViolationSequenceResult:
    bank_conflict_result: ScalarBankConflictLoadClusterSequenceResult
    store_sq_ptr: object
    violation_event: dict
    younger_writeback: dict
    committed_store_view: object


@dataclass(frozen=True)
class ScalarFastReplayCancelledByReplayHiPrioSequenceResult:
    preemptor_kind: str
    preemptor_txns: tuple[LoadTxn, ...]
    preemptor_writebacks: tuple[dict, ...]
    bank_conflict_result: ScalarBankConflictLoadClusterSequenceResult
    expected_completed_load_count: int
    completed_load_count: int


def _sample_debug_ls_info(env, lane: int) -> dict:
    prefix = f"io_debug_ls_debugLsInfo_{lane}"
    return {
        "cycle": env._current_cycle(),
        "lane": lane,
        "s1_rob_idx": env._read_optional_dut_signal(f"{prefix}_s1_robIdx", -1),
        "s2_rob_idx": env._read_optional_dut_signal(f"{prefix}_s2_robIdx", -1),
        "s3_rob_idx": env._read_optional_dut_signal(f"{prefix}_s3_robIdx", -1),
        "s1_is_tlb_first_miss": env._read_optional_dut_signal(f"{prefix}_s1_isTlbFirstMiss", 0),
        "s2_is_bank_conflict": env._read_optional_dut_signal(f"{prefix}_s2_isBankConflict", 0),
        "s2_is_dcache_first_miss": env._read_optional_dut_signal(f"{prefix}_s2_isDcacheFirstMiss", 0),
        "s2_is_forward_fail": env._read_optional_dut_signal(f"{prefix}_s2_isForwardFail", 0),
        "s3_is_replay_fast": env._read_optional_dut_signal(f"{prefix}_s3_isReplayFast", 0),
        "s3_is_replay_slow": env._read_optional_dut_signal(f"{prefix}_s3_isReplaySlow", 0),
        "s3_is_replay": env._read_optional_dut_signal(f"{prefix}_s3_isReplay", 0),
        "replay_cause_bc": env._read_optional_dut_signal(f"{prefix}_replayCause_{BC_REPLAY_CAUSE_BIT}", 0),
        "replay_causes": tuple(
            env._read_optional_dut_signal(f"{prefix}_replayCause_{cause_bit}", 0)
            for cause_bit in REPLAY_CAUSE_BITS
        ),
        "ld_cancel": env._read_optional_dut_signal(f"io_mem_to_ooo_ldCancel_{lane}_ld2Cancel", 0),
    }


def _sample_wakeup_events(env) -> tuple[LoadPipelineWakeupEvent, ...]:
    events = []
    for lane in range(env.config.load_pipeline_width):
        valid = env._read_optional_dut_signal(f"io_mem_to_ooo_wakeup_{lane}_valid", 0)
        if not valid:
            continue
        events.append(
            LoadPipelineWakeupEvent(
                cycle=env._current_cycle(),
                lane=lane,
                pdest=env._read_optional_dut_signal(f"io_mem_to_ooo_wakeup_{lane}_bits_pdest", 0),
                rf_wen=env._read_optional_dut_signal(f"io_mem_to_ooo_wakeup_{lane}_bits_rfWen", 0),
            )
        )
    return tuple(events)


@contextmanager
def _capture_wakeup_trace(env, *, max_events: int = 64):
    trace = deque(maxlen=max(1, int(max_events)))

    def _sample() -> None:
        for event in _sample_wakeup_events(env):
            trace.append(event)

    env.add_after_step_callback(_sample)
    try:
        yield trace
    finally:
        env.remove_after_step_callback(_sample)


def _wait_for_bank_conflicts(env, *, rob_idxs: tuple[int, ...], max_cycles: int = 200) -> tuple[LoadPipelineBankConflictEvent, ...]:
    target_rob_idxs = {int(rob_idx) for rob_idx in rob_idxs}
    observed: list[LoadPipelineBankConflictEvent] = []
    seen = set()

    for _ in range(max_cycles):
        for lane in range(env.config.load_pipeline_width):
            info = _sample_debug_ls_info(env, lane)
            if info["s2_rob_idx"] not in target_rob_idxs:
                continue
            if not info["s2_is_bank_conflict"]:
                continue
            key = (info["lane"], info["s2_rob_idx"], info["cycle"])
            if key in seen:
                continue
            seen.add(key)
            observed.append(
                LoadPipelineBankConflictEvent(
                    cycle=info["cycle"],
                    lane=info["lane"],
                    rob_idx=info["s2_rob_idx"],
                    bank_conflict=info["s2_is_bank_conflict"],
                    dcache_first_miss=info["s2_is_dcache_first_miss"],
                    replay_slow=info["s3_is_replay_slow"],
                    replay_bit_bc=info["replay_cause_bc"],
                )
            )
        if observed:
            return tuple(observed)
        env.advance_cycles(1)
    raise TimeoutError(f"等待 bank conflict 超时: rob_idxs={sorted(target_rob_idxs)}")


def _wait_for_memory_violation(env, *, rob_idx_flag: int, rob_idx_value: int, max_cycles: int = 200) -> dict:
    return env.wait_replay_event(
        source="memory_violation",
        rob_idx_flag=rob_idx_flag,
        rob_idx_value=rob_idx_value,
        max_cycles=max_cycles,
    )


def _wait_for_any_memory_violation(env, *, rob_idx_pairs: tuple[tuple[int, int], ...], max_cycles: int = 200) -> dict:
    targets = {(int(flag), int(value)) for flag, value in rob_idx_pairs}
    for _ in range(max_cycles):
        replay_state = env.sample_replay_state()
        for event in replay_state["events"]:
            if event.get("source") != "memory_violation":
                continue
            key = (int(event.get("rob_idx_flag", 0)), int(event.get("rob_idx_value", -1)))
            if key in targets:
                return dict(event)
        env.advance_cycles(1)
    raise TimeoutError(f"等待任意 younger load 的 memoryViolation 超时: robs={sorted(targets)}")


def _sample_fast_replay_events(env, *, rob_idxs: tuple[int, ...]) -> tuple[LoadPipelineFastReplayEvent, ...]:
    target_rob_idxs = {int(rob_idx) for rob_idx in rob_idxs}
    events = []
    for lane in range(env.config.load_pipeline_width):
        info = _sample_debug_ls_info(env, lane)
        if info["s3_rob_idx"] not in target_rob_idxs:
            continue
        if not (info["s3_is_replay"] or info["s3_is_replay_fast"] or info["s3_is_replay_slow"] or info["ld_cancel"]):
            continue
        events.append(
            LoadPipelineFastReplayEvent(
                cycle=info["cycle"],
                lane=lane,
                rob_idx=info["s3_rob_idx"],
                is_fast_replay=info["s3_is_replay_fast"],
                is_slow_replay=info["s3_is_replay_slow"],
                cancel=info["s3_is_replay"],
                ld_cancel=info["ld_cancel"],
                replay_causes=info["replay_causes"],
            )
        )
    return tuple(events)


def _sample_replay_queue_events(env, *, rob_idxs: tuple[int, ...]) -> tuple[LoadPipelineReplayQueueEvent, ...]:
    target_rob_idxs = {int(rob_idx) for rob_idx in rob_idxs}
    replay_state = env.sample_replay_state()
    events = []
    for event in replay_state["events"]:
        if event.get("rob_idx_value") not in target_rob_idxs:
            continue
        if event.get("source") not in {"replay_queue", "replay_lane", "nc_out"}:
            continue
        events.append(
            LoadPipelineReplayQueueEvent(
                cycle=int(event.get("cycle", env._current_cycle())),
                source=str(event.get("source")),
                lane=event.get("lane"),
                rob_idx_flag=int(event.get("rob_idx_flag", 0)),
                rob_idx_value=int(event.get("rob_idx_value", -1)),
                sched_index=event.get("sched_index"),
                cause=event.get("cause"),
            )
        )
    return tuple(events)


def _wait_for_post_sta_recovery_event(env, *, rob_idx_value: int, max_cycles: int = 200) -> dict:
    recent = deque(maxlen=16)
    for _ in range(max_cycles):
        debug_state = env.sample_load_debug_state()
        for lane_state in debug_state["lanes"]:
            if int(rob_idx_value) not in {
                lane_state["s1_rob_idx_value"],
                lane_state["s2_rob_idx_value"],
                lane_state["s3_rob_idx_value"],
            }:
                continue
            recent.append(dict(lane_state))
            replay_causes = set(lane_state["replay_causes"])
            if "BC" in replay_causes and replay_causes - {"BC"}:
                return dict(lane_state)
        env.advance_cycles(1)
    raise TimeoutError(
        "等待 late-STA 后的恢复 replay 事件超时: "
        f"rob={rob_idx_value}, recent={tuple(recent)}"
    )


def _wait_for_post_bank_conflict_behavior(
    env,
    *,
    rob_idxs: tuple[int, ...],
    require_fast_replay: bool,
    max_cycles: int = 200,
) -> tuple[tuple[LoadPipelineFastReplayEvent, ...], tuple[LoadPipelineReplayQueueEvent, ...]]:
    target_rob_idxs = {int(rob_idx) for rob_idx in rob_idxs}
    fast_events: list[LoadPipelineFastReplayEvent] = []
    replay_queue_events: list[LoadPipelineReplayQueueEvent] = []
    seen_fast = set()
    seen_replay = set()

    for _ in range(max_cycles):
        for event in _sample_fast_replay_events(env, rob_idxs=tuple(target_rob_idxs)):
            key = (event.cycle, event.lane, event.rob_idx)
            if key in seen_fast:
                continue
            seen_fast.add(key)
            fast_events.append(event)

        for event in _sample_replay_queue_events(env, rob_idxs=tuple(target_rob_idxs)):
            key = (event.cycle, event.source, event.lane, event.rob_idx_value, event.sched_index)
            if key in seen_replay:
                continue
            seen_replay.add(key)
            replay_queue_events.append(event)

        if require_fast_replay:
            if any(event.is_fast_replay for event in fast_events):
                return tuple(fast_events), tuple(replay_queue_events)
        elif replay_queue_events:
            return tuple(fast_events), tuple(replay_queue_events)

        env.advance_cycles(1)

    expected = "fast replay" if require_fast_replay else "replay queue"
    raise TimeoutError(f"等待 {expected} 后继行为超时: rob_idxs={sorted(target_rob_idxs)}")


def _issue_loads_same_cycle(env, txns: tuple[LoadTxn, ...], max_cycles: int = 50) -> None:
    env.backend.execute(
        BackendSendPlan.from_steps(
            EnqueueLoadCyclePlan.from_txns(*txns, max_cycles=max_cycles),
            IssueCyclePlan.from_ops(
                *(IssueOp.load_from_txn(txn) for txn in txns),
                max_cycles=max_cycles,
            ),
        )
    )


def _collect_expected_load_writebacks(
    trace,
    *,
    txns: tuple[LoadTxn, ...],
    expected_data_by_rob: dict[tuple[int, int], int],
    label: str,
) -> tuple[dict, ...]:
    pending = {
        (int(txn.rob_idx_flag), int(txn.rob_idx_value)): int(expected_data_by_rob[(int(txn.rob_idx_flag), int(txn.rob_idx_value))])
        for txn in txns
    }
    matched = []

    for event in trace:
        if event.get("int_wen") != 1:
            continue
        if event.get("ready") == 0 or event.get("to_rob_valid") == 0 or event.get("is_from_load") == 0:
            continue
        rob_key = (int(event.get("rob_idx_flag", -1)), int(event.get("rob_idx_value", -1)))
        expected_data = pending.get(rob_key)
        if expected_data is None or int(event.get("data", 0)) != expected_data:
            continue
        matched.append(dict(event))
        del pending[rob_key]
        if not pending:
            return tuple(matched)

    raise TimeoutError(
        f"{label} writeback 观测不完整: missing={sorted(pending)}, trace={tuple(trace)}"
    )


def _prime_hot_cacheable_load(
    env,
    txn: LoadTxn,
    expected_addr: int,
    expected_data: int,
    *,
    wait_memory_quiesce_after_prime: bool = True,
    post_prime_settle_cycles: int = 32,
) -> object:
    completed_before = env.get_completed_load_count()
    prepared = env.backend.prepare(txn)
    env.expect_scalar_load(
        rob_idx=txn.rob_idx,
        pdest=txn.resolved_pdest,
        addr=expected_addr,
        size=txn.size,
        mask=txn.mask,
    )
    env.backend.execute(prepared)
    env.wait_load_writeback_observed(
        rob_idx=txn.rob_idx,
        data=expected_data,
        max_cycles=_resolve_replay_drain_cycles(env, None),
    )
    _wait_completed_load_count(
        env,
        completed_before + 1,
        max_cycles=_resolve_replay_drain_cycles(env, None),
    )
    if wait_memory_quiesce_after_prime:
        env.wait_memory_quiesce(max_cycles=_resolve_replay_drain_cycles(env, None))
    if post_prime_settle_cycles > 0:
        env.advance_cycles(post_prime_settle_cycles)
    verify_transport_before = _snapshot_transport_stats(env)
    verify_txn = LoadTxn(
        req_id=int(txn.req_id) | 0x40000000,
        addr=txn.addr,
        lq_ptr=ptr_inc(txn.lq_ptr, env.config.sequence.load_queue_size),
        sq_ptr=txn.sq_ptr,
        issue_lane=txn.issue_lane,
        size=txn.size,
        mask=txn.mask,
        store_set_hit=txn.store_set_hit,
    )
    verify_completed_before = env.get_completed_load_count()
    verify_prepared = env.backend.prepare(verify_txn)
    env.expect_scalar_load(
        rob_idx=verify_txn.rob_idx,
        pdest=verify_txn.resolved_pdest,
        addr=expected_addr,
        size=verify_txn.size,
        mask=verify_txn.mask,
    )
    env.backend.execute(verify_prepared)
    env.wait_load_writeback_observed(
        rob_idx=verify_txn.rob_idx,
        data=expected_data,
        max_cycles=_resolve_replay_drain_cycles(env, None),
    )
    _wait_completed_load_count(
        env,
        verify_completed_before + 1,
        max_cycles=_resolve_replay_drain_cycles(env, None),
    )
    verify_transport_after = _snapshot_transport_stats(env)
    assert verify_transport_after["dcache_a_request_count"] == verify_transport_before["dcache_a_request_count"], (
        f"cache warmup verify 仍触发 dcache miss: addr=0x{int(txn.addr):x}, "
        f"before={verify_transport_before['dcache_a_request_count']}, "
        f"after={verify_transport_after['dcache_a_request_count']}"
    )
    if wait_memory_quiesce_after_prime:
        env.wait_memory_quiesce(max_cycles=_resolve_replay_drain_cycles(env, None))
    if post_prime_settle_cycles > 0:
        env.advance_cycles(post_prime_settle_cycles)
    return ptr_inc(verify_txn.lq_ptr, env.config.sequence.load_queue_size)


class ScalarBankConflictLoadClusterSequence:
    def __init__(
        self,
        requests: tuple[tuple[int, int], ...],
        *,
        initial_state: SequenceState,
        issue_lanes: tuple[int, ...] = (0, 1, 2),
        store_set_hit_addrs: tuple[int, ...] = (),
        require_fast_replay: bool = True,
        wait_for_final_writebacks: bool = True,
        prime_cache_lines: bool = True,
    ) -> None:
        if len(requests) != len(issue_lanes):
            raise ValueError("requests 和 issue_lanes 数量必须一致")
        self.requests = tuple((int(req_id), int(addr)) for req_id, addr in requests)
        self.initial_state = initial_state
        self.issue_lanes = tuple(int(lane) for lane in issue_lanes)
        self.store_set_hit_addrs = {int(addr) for addr in store_set_hit_addrs}
        self.require_fast_replay = bool(require_fast_replay)
        self.wait_for_final_writebacks = bool(wait_for_final_writebacks)
        self.prime_cache_lines = bool(prime_cache_lines)

    def run(self, env) -> ScalarBankConflictLoadClusterSequenceResult:
        next_lq_ptr = self.initial_state.next_lq_ptr
        sq_ptr = self.initial_state.sq_ptr
        completed_before = env.get_completed_load_count()

        # Prime the cache lines first so the main cluster exercises dcache hit
        # timing rather than a cold-miss path.
        if self.prime_cache_lines:
            for prime_req_id, (req_id, addr) in enumerate(self.requests):
                data = env.memory.read(addr, 8)
                prime_txn = LoadTxn(
                    req_id=0x100 + prime_req_id,
                    addr=addr,
                    lq_ptr=next_lq_ptr,
                    sq_ptr=sq_ptr,
                    issue_lane=0,
                )
                next_lq_ptr = _prime_hot_cacheable_load(
                    env,
                    prime_txn,
                    expected_addr=addr,
                    expected_data=data,
                )
        completed_before_main = env.get_completed_load_count()

        txns = []
        expected_writebacks = []
        for index, (request, lane) in enumerate(zip(self.requests, self.issue_lanes)):
            req_id, addr = request
            txn = LoadTxn(
                req_id=req_id,
                addr=addr,
                lq_ptr=next_lq_ptr,
                sq_ptr=sq_ptr,
                issue_lane=lane,
                store_set_hit=1 if int(addr) in self.store_set_hit_addrs else 0,
            )
            txns.append(txn)
            expected_writebacks.append(env.memory.read(addr, 8))
            env.backend.prepare(txn)
            env.expect_scalar_load(
                rob_idx=txn.rob_idx,
                pdest=txn.resolved_pdest,
                addr=txn.addr,
                size=txn.size,
                mask=txn.mask,
            )
            next_lq_ptr = ptr_inc(next_lq_ptr, env.config.sequence.load_queue_size)

        transport_stats_before = _snapshot_transport_stats(env)
        with _capture_writeback_trace(env, max_events=32) as _trace, _capture_wakeup_trace(env, max_events=64) as wakeup_trace:
            _issue_loads_same_cycle(env, tuple(txns))
            bank_conflicts = _wait_for_bank_conflicts(
                env,
                rob_idxs=tuple(txn.rob_idx_value for txn in txns),
                max_cycles=_resolve_replay_drain_cycles(env, None),
            )
            fast_replay_events, replay_queue_events = _wait_for_post_bank_conflict_behavior(
                env,
                rob_idxs=tuple(txn.rob_idx_value for txn in txns),
                require_fast_replay=self.require_fast_replay,
                max_cycles=_resolve_replay_drain_cycles(env, None),
            )
            if self.wait_for_final_writebacks:
                writebacks = tuple(
                    env.wait_load_writeback_observed(
                        rob_idx=txn.rob_idx,
                        data=data,
                        max_cycles=_resolve_replay_drain_cycles(env, None),
                    )
                    for txn, data in zip(txns, expected_writebacks)
                )
            else:
                writebacks = ()
        if self.wait_for_final_writebacks:
            wakeups = tuple(
                next(
                    event
                    for event in wakeup_trace
                    if event.pdest == int(txn.resolved_pdest) and event.rf_wen
                )
                for txn in txns
            )
            completed_target = completed_before_main + len(txns)
            completed_after = _wait_completed_load_count(
                env,
                completed_target,
                max_cycles=_resolve_replay_drain_cycles(env, None),
            )
        else:
            wakeups = ()
            completed_after = env.get_completed_load_count()
        transport_stats_after = _snapshot_transport_stats(env)
        replay_state = env.sample_replay_state()
        matching_replays = tuple(
            event
            for event in replay_state["events"]
            if event.get("rob_idx_value") in {txn.rob_idx_value for txn in txns}
        )
        sq_forward_events = tuple(_sample_sq_forward_events(env))
        violation_event = None
        if replay_state["memory_violation"]["valid"]:
            violation_event = dict(replay_state["memory_violation"])

        return ScalarBankConflictLoadClusterSequenceResult(
            txns=tuple(txns),
            writebacks=writebacks,
            wakeups=wakeups,
            bank_conflicts=bank_conflicts,
            fast_replay_events=fast_replay_events,
            replay_queue_events=replay_queue_events,
            sq_forward_events=sq_forward_events,
            violation_event=violation_event,
            replay_events=matching_replays,
            transport_stats_before=transport_stats_before,
            transport_stats_after=transport_stats_after,
            completed_load_count=completed_after,
        )


class ScalarLateStaStoreLoadViolationSequence:
    def __init__(
        self,
        *,
        store_txn: StoreTxn,
        load_requests: tuple[tuple[int, int], ...],
        initial_state: SequenceState,
        issue_lanes: tuple[int, ...] = (0, 1, 2),
        settle_cycles_after_sta: int = 8,
    ) -> None:
        self.store_txn = store_txn
        self.load_requests = tuple((int(req_id), int(addr)) for req_id, addr in load_requests)
        self.initial_state = initial_state
        self.issue_lanes = tuple(int(lane) for lane in issue_lanes)
        self.settle_cycles_after_sta = int(settle_cycles_after_sta)

    def run(self, env) -> ScalarLateStaStoreLoadViolationSequenceResult:
        if len(self.load_requests) < 2:
            raise ValueError("late-STA bank-conflict 场景至少需要两条 load 请求")

        next_lq_ptr = self.initial_state.next_lq_ptr

        victim_req = None
        lead_req = None
        for request in self.load_requests:
            if request[1] == self.store_txn.addr and victim_req is None:
                victim_req = request
            elif request[1] != self.store_txn.addr and lead_req is None:
                lead_req = request
        if victim_req is None or lead_req is None:
            raise ValueError("late-STA bank-conflict 场景需要一条 same-addr victim 和一条不同地址 lead load")

        warmup_sq_ptr = ptr_inc(self.initial_state.sq_ptr, env.config.sequence.store_queue_size)
        warmup_lead = LoadTxn(
            req_id=0x180 + lead_req[0],
            addr=lead_req[1],
            lq_ptr=next_lq_ptr,
            sq_ptr=warmup_sq_ptr,
            issue_lane=0,
        )
        next_lq_ptr = _prime_hot_cacheable_load(
            env,
            warmup_lead,
            expected_addr=warmup_lead.addr,
            expected_data=env.memory.read(warmup_lead.addr, 8),
        )

        warmup_victim = LoadTxn(
            req_id=0x1C0 + victim_req[0],
            addr=victim_req[1],
            lq_ptr=next_lq_ptr,
            sq_ptr=warmup_sq_ptr,
            issue_lane=1,
        )
        next_lq_ptr = _prime_hot_cacheable_load(
            env,
            warmup_victim,
            expected_addr=warmup_victim.addr,
            expected_data=env.memory.read(warmup_victim.addr, 8),
        )
        store_ref = StoreRef(name=f"late_sta_store_{self.store_txn.req_id}")
        store_sq_ptr = env.backend.execute(
            BackendSendPlan.from_steps(
                EnqueueStoreStep.from_txn(self.store_txn, ref=store_ref),
            )
        )
        store_sq_ptr = store_sq_ptr.resolve_sq_ptr(store_ref)
        younger_sq_ptr = ptr_inc(store_sq_ptr, env.config.sequence.store_queue_size)
        lead_load = LoadTxn(
            req_id=lead_req[0],
            addr=lead_req[1],
            lq_ptr=next_lq_ptr,
            sq_ptr=younger_sq_ptr,
            issue_lane=0,
        )
        next_lq_ptr = ptr_inc(next_lq_ptr, env.config.sequence.load_queue_size)
        victim_load = LoadTxn(
            req_id=victim_req[0],
            addr=victim_req[1],
            lq_ptr=next_lq_ptr,
            sq_ptr=younger_sq_ptr,
            issue_lane=1,
            store_set_hit=1,
        )
        lead_expected_data = env.memory.read(lead_load.addr, 8)
        env.backend.prepare(lead_load)
        lead_expected = env.expect_scalar_load(
            rob_idx=lead_load.rob_idx,
            pdest=lead_load.resolved_pdest,
            addr=lead_load.addr,
            size=lead_load.size,
            mask=lead_load.mask,
        )
        lead_expected.expected_data = lead_expected_data
        env.backend.prepare(victim_load)
        victim_expected = env.expect_scalar_load(
            rob_idx=victim_load.rob_idx,
            pdest=victim_load.resolved_pdest,
            addr=victim_load.addr,
            size=victim_load.size,
            mask=victim_load.mask,
        )
        victim_expected.expected_data = self.store_txn.data
        completed_before_main = env.get_completed_load_count()
        transport_stats_before = _snapshot_transport_stats(env)

        with (
            _capture_load_debug_trace(env, max_events=128) as load_debug_trace,
            _capture_replay_events(
                env,
                rob_idx=victim_load.rob_idx,
                max_events=64,
            ) as replay_trace,
            _capture_writeback_trace(env, max_events=64) as writeback_trace,
        ):
            env.backend.execute(
                BackendSendPlan.from_steps(
                    EnqueueLoadCyclePlan.from_txns(lead_load, victim_load),
                    IssueCyclePlan.from_ops(
                        IssueOp.load_from_txn(lead_load),
                        IssueOp.load_from_txn(victim_load),
                    ),
                    IssueCyclePlan.from_ops(
                        IssueOp.std(
                            req_id=self.store_txn.req_id,
                            sq_ptr=store_sq_ptr,
                            data=self.store_txn.data,
                            lane=self.store_txn.std_lane,
                            mask=self.store_txn.mask,
                        )
                    ),
                    IssueCyclePlan.from_ops(
                        IssueOp.sta(
                            req_id=self.store_txn.req_id,
                            sq_ptr=store_sq_ptr,
                            addr=self.store_txn.addr,
                            lane=self.store_txn.sta_lane,
                            mask=self.store_txn.mask,
                        )
                    ),
                )
            )
            if self.settle_cycles_after_sta > 0:
                env.advance_cycles(self.settle_cycles_after_sta)

            env.backend.execute(BackendSendPlan.from_steps(StoreCommitStep()))
            commit_settle_cycles = int(getattr(env.config.sequence, "store_settle_cycles", 0))
            if commit_settle_cycles > 0:
                env.advance_cycles(commit_settle_cycles)
            completed_after = _wait_completed_load_count(
                env,
                completed_before_main + 2,
                max_cycles=_resolve_replay_drain_cycles(env, None),
            )

            def _match_writeback(event, *, rob_idx_flag, rob_idx_value, data):
                if event.get("int_wen") != 1:
                    return False
                if event.get("rob_idx_flag") != rob_idx_flag or event.get("rob_idx_value") != rob_idx_value:
                    return False
                return data is None or event.get("data") == data

            lead_writeback = next(
                (
                    event
                    for event in writeback_trace
                    if _match_writeback(
                        event,
                        rob_idx_flag=lead_load.rob_idx_flag,
                        rob_idx_value=lead_load.rob_idx_value,
                        data=lead_expected_data,
                    )
                ),
                None,
            )
            younger_writeback = next(
                (
                    event
                    for event in writeback_trace
                    if _match_writeback(
                        event,
                        rob_idx_flag=victim_load.rob_idx_flag,
                        rob_idx_value=victim_load.rob_idx_value,
                        data=self.store_txn.data,
                    )
                ),
                None,
            )
            if lead_writeback is None or younger_writeback is None:
                raise TimeoutError(
                    "late-STA bank-conflict 场景 writeback 观测不完整: "
                    f"lead={lead_writeback is not None}, victim={younger_writeback is not None}, "
                    f"trace={tuple(writeback_trace)}"
                )

        bank_conflicts = tuple(
            LoadPipelineBankConflictEvent(
                cycle=event["cycle"],
                lane=event["lane"],
                rob_idx=event["s2_rob_idx_value"],
                bank_conflict=1,
                dcache_first_miss=0,
                replay_slow=int(event["s3_is_replay_slow"]),
                replay_bit_bc=1 if "BC" in event["replay_causes"] else 0,
            )
            for event in load_debug_trace
            if event["s2_is_bank_conflict"] and event["s2_rob_idx_value"] >= 0
        )
        fast_replay_events = tuple(
            LoadPipelineFastReplayEvent(
                cycle=event["cycle"],
                lane=event["lane"],
                rob_idx=event["s3_rob_idx_value"],
                is_fast_replay=int(event["s3_is_replay_fast"]),
                is_slow_replay=int(event["s3_is_replay_slow"]),
                cancel=int(event["s3_is_replay"]),
                ld_cancel=0,
                replay_causes=(),
            )
            for event in load_debug_trace
            if event["s3_is_replay_fast"] and event["s3_rob_idx_value"] >= 0
        )
        transport_stats_after = _snapshot_transport_stats(env)
        violation_event = {
            "valid": 1,
            "source": "writeback",
            "cause": ("late_sta_recovery",),
            "rob_idx_flag": victim_load.rob_idx_flag,
            "rob_idx_value": victim_load.rob_idx_value,
            "lane": younger_writeback["lane"],
            "replay_causes": (),
        }
        committed_store_view = env.wait_store_materialized(
            store_sq_ptr.value,
            expected_addr=self.store_txn.addr,
            expected_data=self.store_txn.data,
            expected_mmio=False,
            require_committed=True,
            max_cycles=_resolve_replay_drain_cycles(env, None),
        )
        bank_conflict_result = ScalarBankConflictLoadClusterSequenceResult(
            txns=(lead_load, victim_load),
            writebacks=(lead_writeback, younger_writeback),
            wakeups=(),
            bank_conflicts=bank_conflicts,
            fast_replay_events=fast_replay_events,
            replay_queue_events=(),
            sq_forward_events=(),
            violation_event=None,
            replay_events=tuple(replay_trace),
            transport_stats_before=transport_stats_before,
            transport_stats_after=transport_stats_after,
            completed_load_count=completed_after,
        )

        return ScalarLateStaStoreLoadViolationSequenceResult(
            bank_conflict_result=bank_conflict_result,
            store_sq_ptr=store_sq_ptr,
            violation_event=violation_event,
            younger_writeback=younger_writeback,
            committed_store_view=committed_store_view,
        )


class ScalarFastReplayCancelledByReplayHiPrioSequence:
    def __init__(
        self,
        *,
        preemptor_kind: str,
        preemptor_requests: tuple[tuple[int, int, int], ...],
        bank_conflict_requests: tuple[tuple[int, int], ...],
        initial_state: SequenceState,
        issue_lanes: tuple[int, ...] = (0, 1, 2),
    ) -> None:
        self.preemptor_kind = str(preemptor_kind)
        self.preemptor_requests = tuple((int(req_id), int(addr), int(data)) for req_id, addr, data in preemptor_requests)
        self.bank_conflict_requests = tuple((int(req_id), int(addr)) for req_id, addr in bank_conflict_requests)
        self.initial_state = initial_state
        self.issue_lanes = tuple(int(lane) for lane in issue_lanes)

    def run(self, env) -> ScalarFastReplayCancelledByReplayHiPrioSequenceResult:
        next_lq_ptr = self.initial_state.next_lq_ptr
        sq_ptr = self.initial_state.sq_ptr
        completed_before = env.get_completed_load_count()
        preemptor_txns = []
        preemptor_expected_data = {}

        with _capture_writeback_trace(env, max_events=256) as writeback_trace:
            for req_id, addr, data in self.preemptor_requests:
                env.preload_u64(addr, data)
                txn = LoadTxn(
                    req_id=req_id,
                    addr=addr,
                    lq_ptr=next_lq_ptr,
                    sq_ptr=sq_ptr,
                )
                prepared = env.backend.prepare(txn)
                env.expect_scalar_load(
                    rob_idx=txn.rob_idx,
                    pdest=txn.resolved_pdest,
                    addr=txn.addr,
                    size=txn.size,
                    mask=txn.mask,
                )
                env.backend.execute(prepared)
                preemptor_txns.append(txn)
                preemptor_expected_data[(int(txn.rob_idx_flag), int(txn.rob_idx_value))] = int(data)
                next_lq_ptr = ptr_inc(next_lq_ptr, env.config.sequence.load_queue_size)

            bank_conflict_result = ScalarBankConflictLoadClusterSequence(
                self.bank_conflict_requests,
                initial_state=SequenceState(next_lq_ptr=next_lq_ptr, sq_ptr=sq_ptr),
                issue_lanes=self.issue_lanes,
                require_fast_replay=False,
                wait_for_final_writebacks=True,
                prime_cache_lines=False,
            ).run(env)

            total_expected = completed_before + len(preemptor_txns) + len(bank_conflict_result.txns)
            completed_load_count = _wait_completed_load_count(
                env,
                total_expected,
                max_cycles=_resolve_replay_drain_cycles(env, None) * 8,
            )
            env.drain_writebacks(max_cycles=_resolve_replay_drain_cycles(env, None) * 8)

        preemptor_writebacks = _collect_expected_load_writebacks(
            writeback_trace,
            txns=tuple(preemptor_txns),
            expected_data_by_rob=preemptor_expected_data,
            label=f"{self.preemptor_kind} preemptor",
        )

        return ScalarFastReplayCancelledByReplayHiPrioSequenceResult(
            preemptor_kind=self.preemptor_kind,
            preemptor_txns=tuple(preemptor_txns),
            preemptor_writebacks=preemptor_writebacks,
            bank_conflict_result=bank_conflict_result,
            expected_completed_load_count=total_expected,
            completed_load_count=completed_load_count,
        )
