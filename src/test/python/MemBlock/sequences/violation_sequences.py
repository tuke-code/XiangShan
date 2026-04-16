# coding=utf-8
"""
MemBlock replay / violation related sequences.
"""

from collections import deque
from contextlib import contextmanager
from dataclasses import dataclass

from transactions import LoadTxn, ptr_inc, QueuePtr
from request_apis import (
    enqueue_scalar_store,
    expect_load,
    issue_scalar_sta,
    issue_scalar_std,
    send_load,
)
from transactions import (
    BackendSendPlan,
    EnqueueLoadStep,
    EnqueueStoreStep,
    IssueCyclePlan,
    IssueOp,
    StoreCommitStep,
    StoreRef,
)

from .memblock_sequences import (
    ScalarLoadBatchSameCycleSequence,
    SequenceState,
    _capture_load_debug_trace,
    _capture_writeback_trace,
    _load_debug_lane_matches,
    _resolve_replay_drain_cycles,
    _snapshot_transport_stats,
    _wait_load_debug_event,
    _wait_completed_load_count,
    _wait_sq_forward_event,
    _wait_sq_matchinvalid_and_violation,
    _wait_store_addr_observed,
)


@dataclass(frozen=True)
class ScalarRawReplaySequenceResult:
    store_sq_ptr: QueuePtr
    nuke_query_event: dict
    replay_event: dict
    committed_store_view: object
    final_state: SequenceState
    issued_loads: tuple[LoadTxn, ...]
    completed_load_count: int


@dataclass(frozen=True)
class ScalarRarViolationSequenceResult:
    fake_store_sq_ptr: QueuePtr
    older_load: LoadTxn
    younger_load: LoadTxn
    younger_writeback: dict
    older_writeback: dict
    release_event: dict
    rar_nuke_response: dict
    violation_event: dict | None
    final_state: SequenceState
    fake_store_view: object
    completed_load_count: int


@dataclass(frozen=True)
class ScalarSqDataInvalidMatchInvalidTriggerSequenceResult:
    tlb_prime_writeback: dict | None
    sq_forward_event: dict
    memory_violation: dict
    main_writeback_trace: tuple[dict, ...]
    main_load: LoadTxn
    store_sq_ptr: QueuePtr
    committed_store_view: object
    final_state: SequenceState
    transport_stats_before_main: dict[str, int]
    transport_stats_after_recovery: dict[str, int]
    replay_events: tuple[dict, ...]
    dcache_error_valid: int
    dcache_miss_signal: int | None


@dataclass(frozen=True)
class ScalarPipelineStldNukeSequenceResult:
    main_load: LoadTxn
    store_sq_ptr: QueuePtr
    load_debug_event: dict
    load_debug_trace: tuple[dict, ...]
    nk_observed: bool
    replay_events: tuple[dict, ...]
    nuke_query_trace: tuple[dict, ...]
    main_writeback: dict | None
    committed_store_view: object | None
    final_state: SequenceState
    completed_load_count: int


@dataclass(frozen=True)
class ScalarBankConflictSqDataInvalidNukeSequenceResult:
    lead_load: LoadTxn
    victim_load: LoadTxn
    store_sq_ptr: QueuePtr
    sq_forward_event: dict
    load_debug_event: dict
    load_debug_trace: tuple[dict, ...]
    nk_observed: bool
    replay_events: tuple[dict, ...]
    nuke_query_trace: tuple[dict, ...]
    lead_writeback: dict | None
    victim_writeback: dict | None
    committed_store_view: object | None
    final_state: SequenceState
    completed_load_count: int


@contextmanager
def _capture_replay_events(
    env,
    *,
    rob_idx,
    max_events: int = 64,
):
    trace = deque(maxlen=max(1, int(max_events)))
    seen = set()

    def _sample() -> None:
        replay_state = env.sample_replay_state()
        for event in replay_state["events"]:
            if event.get("rob_idx_flag") != int(rob_idx.flag) or event.get("rob_idx_value") != int(rob_idx.value):
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
            trace.append(dict(event))

    env.add_after_step_callback(_sample)
    try:
        yield trace
    finally:
        env.remove_after_step_callback(_sample)


@contextmanager
def _capture_nuke_query_trace(env, *, max_events: int = 64):
    trace = deque(maxlen=max(1, int(max_events)))
    seen = set()

    def _sample() -> None:
        query_state = env.sample_nuke_query_state()
        for bucket_name in ("raw_queries", "rar_queries", "rar_responses"):
            for event in query_state[bucket_name]:
                event_key = (
                    event.get("cycle"),
                    event.get("source"),
                    event.get("lane"),
                    event.get("rob_idx_flag"),
                    event.get("rob_idx_value"),
                    event.get("lq_idx_flag"),
                    event.get("lq_idx_value"),
                    event.get("sq_idx_flag"),
                    event.get("sq_idx_value"),
                    event.get("resp_valid"),
                    event.get("nuke"),
                )
                if event_key in seen:
                    continue
                seen.add(event_key)
                trace.append(dict(event))

    env.add_after_step_callback(_sample)
    try:
        yield trace
    finally:
        env.remove_after_step_callback(_sample)


def _wait_load_debug_stage(env, *, rob_idx_value: int, stage: str, max_cycles: int = 200) -> dict:
    stage_key = f"{stage}_rob_idx_value"
    if stage_key not in {"s1_rob_idx_value", "s2_rob_idx_value", "s3_rob_idx_value"}:
        raise ValueError(f"unsupported load debug stage: {stage}")

    def _probe():
        for lane_state in env.sample_load_debug_state()["lanes"]:
            if lane_state.get(stage_key) == int(rob_idx_value):
                return dict(lane_state)
        return None

    return env.wait_until(
        _probe,
        max_cycles=max_cycles,
        timeout_message=f"等待 load debug 命中 {stage} 超时: rob={rob_idx_value}",
    )


class ScalarRawReplaySequence:
    def __init__(
        self,
        store_txn,
        *,
        initial_lq_ptr: QueuePtr,
        load_addresses,
        first_load_req_id: int,
        nuke_wait_cycles: int = 200,
        replay_wait_cycles: int = 400,
        materialize_cycles: int = 200,
        drain_cycles: int | None = None,
        size: int = 8,
        mask: int = 0xFF,
        assert_no_outstanding: bool = False,
    ) -> None:
        self.store_txn = store_txn
        self.initial_lq_ptr = initial_lq_ptr
        self.load_addresses = tuple(int(addr) for addr in load_addresses)
        self.first_load_req_id = first_load_req_id
        self.nuke_wait_cycles = nuke_wait_cycles
        self.replay_wait_cycles = replay_wait_cycles
        self.materialize_cycles = materialize_cycles
        self.drain_cycles = drain_cycles
        self.size = size
        self.mask = mask
        self.assert_no_outstanding = assert_no_outstanding

    def run(self, env) -> ScalarRawReplaySequenceResult:
        if not self.load_addresses:
            raise ValueError("RAW replay sequence requires at least one younger load")

        completed_before = env.get_completed_load_count()
        store_queue_size = env.config.sequence.store_queue_size
        load_queue_size = env.config.sequence.load_queue_size

        env.backend.prepare(self.store_txn)
        store_sq_ptr = enqueue_scalar_store(
            env,
            req_id=self.store_txn.req_id,
            sq_ptr=self.store_txn.sq_ptr,
            enq_port=self.store_txn.enq_port,
            rob_idx=self.store_txn.rob_idx,
        )
        younger_sq_ptr = ptr_inc(self.store_txn.sq_ptr, store_queue_size)

        next_lq_ptr = self.initial_lq_ptr
        load_txns = []
        for load_idx, addr in enumerate(self.load_addresses):
            load_txn = LoadTxn(
                req_id=self.first_load_req_id + load_idx,
                addr=addr,
                lq_ptr=next_lq_ptr,
                sq_ptr=younger_sq_ptr,
                size=self.size,
                mask=self.mask,
            )
            send_load(env, load_txn)
            expect_load(env, load_txn)
            load_txns.append(load_txn)
            next_lq_ptr = ptr_inc(next_lq_ptr, load_queue_size)

        nuke_query_event = env.wait_nuke_query_backpressure(
            kind="raw",
            sq_idx=younger_sq_ptr.value,
            max_cycles=self.nuke_wait_cycles,
        )
        replay_event = env.wait_replay_event(
            cause="RAW",
            sq_idx=younger_sq_ptr.value,
            max_cycles=self.replay_wait_cycles,
        )

        issue_scalar_std(
            env,
            req_id=self.store_txn.req_id,
            sq_ptr=store_sq_ptr,
            data=self.store_txn.data,
            lane=self.store_txn.std_lane,
            rob_idx=self.store_txn.rob_idx,
            ftq_idx_flag=self.store_txn.resolved_ftq_idx_flag,
            ftq_idx_value=self.store_txn.resolved_ftq_idx_value,
        )
        issue_scalar_sta(
            env,
            req_id=self.store_txn.req_id,
            sq_ptr=store_sq_ptr,
            addr=self.store_txn.addr,
            lane=self.store_txn.sta_lane,
            rob_idx=self.store_txn.rob_idx,
            ftq_idx_flag=self.store_txn.resolved_ftq_idx_flag,
            ftq_idx_value=self.store_txn.resolved_ftq_idx_value,
        )
        env.backend.pulse_store_commit(1)
        committed_store_view = env.wait_store_materialized(
            store_sq_ptr.value,
            expected_addr=self.store_txn.addr,
            expected_data=self.store_txn.data,
            expected_mmio=False,
            require_committed=True,
            max_cycles=self.materialize_cycles,
        )
        env.drain_writebacks(max_cycles=_resolve_replay_drain_cycles(env, self.drain_cycles))
        completed = env.get_completed_load_count()
        assert completed == completed_before + len(load_txns), (
            f"RAW replay 场景未完成全部 younger loads: completed={completed}, "
            f"expected={completed_before + len(load_txns)}"
        )

        final_state = SequenceState(
            next_lq_ptr=next_lq_ptr,
            sq_ptr=younger_sq_ptr,
        )
        if self.assert_no_outstanding:
            env.assert_no_outstanding()

        return ScalarRawReplaySequenceResult(
            store_sq_ptr=store_sq_ptr,
            nuke_query_event=nuke_query_event,
            replay_event=replay_event,
            committed_store_view=committed_store_view,
            final_state=final_state,
            issued_loads=tuple(load_txns),
            completed_load_count=completed,
        )


class ScalarRarViolationSequence:
    def __init__(
        self,
        *,
        fake_store_txn,
        older_load_txn,
        younger_load_txn,
        release_new_value: int,
        probe_param: int = 2,
        replay_wait_cycles: int = 400,
        violation_wait_cycles: int = 200,
        materialize_cycles: int = 200,
        drain_cycles: int | None = None,
        assert_no_outstanding: bool = False,
    ) -> None:
        self.fake_store_txn = fake_store_txn
        self.older_load_txn = older_load_txn
        self.younger_load_txn = younger_load_txn
        self.release_new_value = int(release_new_value)
        self.probe_param = int(probe_param)
        self.replay_wait_cycles = replay_wait_cycles
        self.violation_wait_cycles = violation_wait_cycles
        self.materialize_cycles = materialize_cycles
        self.drain_cycles = drain_cycles
        self.assert_no_outstanding = assert_no_outstanding

    def run(self, env) -> ScalarRarViolationSequenceResult:
        completed_before = env.get_completed_load_count()
        cacheline_addr = self.younger_load_txn.addr & ~0x3F
        previous_strict = env.memory.strict_writeback_check
        env.memory.strict_writeback_check = False
        try:
            env.backend.prepare(self.fake_store_txn)
            fake_store_sq_ptr = enqueue_scalar_store(
                env,
                req_id=self.fake_store_txn.req_id,
                sq_ptr=self.fake_store_txn.sq_ptr,
                enq_port=self.fake_store_txn.enq_port,
                rob_idx=self.fake_store_txn.rob_idx,
            )
            issue_scalar_std(
                env,
                req_id=self.fake_store_txn.req_id,
                sq_ptr=fake_store_sq_ptr,
                data=self.fake_store_txn.data,
                lane=self.fake_store_txn.std_lane,
                rob_idx=self.fake_store_txn.rob_idx,
                ftq_idx_flag=self.fake_store_txn.resolved_ftq_idx_flag,
                ftq_idx_value=self.fake_store_txn.resolved_ftq_idx_value,
            )

            send_load(env, self.older_load_txn)
            expect_load(env, self.older_load_txn)
            env.wait_replay_event(
                cause="MA",
                rob_idx=self.older_load_txn.rob_idx,
                max_cycles=self.replay_wait_cycles,
            )

            send_load(env, self.younger_load_txn)
            younger_writeback = env.wait_load_writeback_observed(
                rob_idx=self.younger_load_txn.rob_idx,
                data=env.memory.read(self.younger_load_txn.addr, self.younger_load_txn.size),
                max_cycles=_resolve_replay_drain_cycles(env, self.drain_cycles),
            )

            env.preload_u64(self.younger_load_txn.addr, self.release_new_value)
            env.inject_dcache_probe(cacheline_addr, param=self.probe_param)
            release_event = env.wait_release_event(
                cacheline_addr=cacheline_addr,
                max_cycles=self.violation_wait_cycles,
            )

            issue_scalar_sta(
                env,
                req_id=self.fake_store_txn.req_id,
                sq_ptr=fake_store_sq_ptr,
                addr=self.fake_store_txn.addr,
                lane=self.fake_store_txn.sta_lane,
                rob_idx=self.fake_store_txn.rob_idx,
                ftq_idx_flag=self.fake_store_txn.resolved_ftq_idx_flag,
                ftq_idx_value=self.fake_store_txn.resolved_ftq_idx_value,
            )
            env.backend.pulse_store_commit(1)
            fake_store_view = env.wait_store_materialized(
                fake_store_sq_ptr.value,
                expected_addr=self.fake_store_txn.addr,
                expected_data=self.fake_store_txn.data,
                expected_mmio=False,
                require_committed=True,
                max_cycles=self.materialize_cycles,
            )

            rar_nuke_response = env.wait_rar_nuke_response(
                rob_idx=self.older_load_txn.rob_idx,
                lq_idx_flag=self.older_load_txn.lq_ptr.flag,
                lq_idx_value=self.older_load_txn.lq_ptr.value,
                max_cycles=self.violation_wait_cycles,
            )
            older_writeback = env.wait_load_writeback_observed(
                rob_idx=self.older_load_txn.rob_idx,
                data=self.release_new_value,
                max_cycles=_resolve_replay_drain_cycles(env, self.drain_cycles),
            )
            violation_candidates = env.collect_replay_window(
                min(16, self.violation_wait_cycles),
                source="memory_violation",
                rob_idx=self.older_load_txn.rob_idx,
            )
            violation_event = violation_candidates[0] if violation_candidates else None

            completed = _wait_completed_load_count(
                env,
                completed_before + 1,
                max_cycles=_resolve_replay_drain_cycles(env, self.drain_cycles),
            )

            final_state = SequenceState(
                next_lq_ptr=ptr_inc(self.younger_load_txn.lq_ptr, env.config.sequence.load_queue_size),
                sq_ptr=ptr_inc(self.fake_store_txn.sq_ptr, env.config.sequence.store_queue_size),
            )
            if self.assert_no_outstanding:
                env.assert_no_outstanding()

            return ScalarRarViolationSequenceResult(
                fake_store_sq_ptr=fake_store_sq_ptr,
                older_load=self.older_load_txn,
                younger_load=self.younger_load_txn,
                younger_writeback=younger_writeback,
                older_writeback=older_writeback,
                release_event=release_event,
                rar_nuke_response=rar_nuke_response,
                violation_event=violation_event,
                final_state=final_state,
                fake_store_view=fake_store_view,
                completed_load_count=completed,
            )
        finally:
            env.memory.strict_writeback_check = previous_strict


class ScalarPipelineStldNukeSequence:
    def __init__(
        self,
        *,
        store_txn,
        initial_lq_ptr: QueuePtr,
        load_req_id: int,
        replay_wait_cycles: int = 200,
        materialize_cycles: int = 200,
        drain_cycles: int | None = None,
        assert_no_outstanding: bool = False,
    ) -> None:
        self.store_txn = store_txn
        self.initial_lq_ptr = initial_lq_ptr
        self.load_req_id = int(load_req_id)
        self.replay_wait_cycles = int(replay_wait_cycles)
        self.materialize_cycles = int(materialize_cycles)
        self.drain_cycles = drain_cycles
        self.assert_no_outstanding = assert_no_outstanding

    def run(self, env) -> ScalarPipelineStldNukeSequenceResult:
        completed_before = env.get_completed_load_count()
        env.backend.prepare(self.store_txn)
        store_sq_ptr = enqueue_scalar_store(
            env,
            req_id=self.store_txn.req_id,
            sq_ptr=self.store_txn.sq_ptr,
            enq_port=self.store_txn.enq_port,
            rob_idx=self.store_txn.rob_idx,
        )
        issue_scalar_std(
            env,
            req_id=self.store_txn.req_id,
            sq_ptr=store_sq_ptr,
            data=self.store_txn.data,
            lane=self.store_txn.std_lane,
            rob_idx=self.store_txn.rob_idx,
            ftq_idx_flag=self.store_txn.resolved_ftq_idx_flag,
            ftq_idx_value=self.store_txn.resolved_ftq_idx_value,
        )

        main_load = LoadTxn(
            req_id=self.load_req_id,
            addr=self.store_txn.addr,
            lq_ptr=self.initial_lq_ptr,
            sq_ptr=ptr_inc(self.store_txn.sq_ptr, env.config.sequence.store_queue_size),
            store_set_hit=1,
        )
        main_expected = expect_load(env, main_load)

        with (
            _capture_load_debug_trace(env, max_events=96) as load_debug_trace,
            _capture_replay_events(
                env,
                rob_idx=main_load.rob_idx,
                max_events=64,
            ) as replay_trace,
            _capture_nuke_query_trace(env, max_events=64) as nuke_query_trace,
        ):
            send_load(env, main_load)
            issue_scalar_sta(
                env,
                req_id=self.store_txn.req_id,
                sq_ptr=store_sq_ptr,
                addr=self.store_txn.addr,
                lane=self.store_txn.sta_lane,
                rob_idx=self.store_txn.rob_idx,
                ftq_idx_flag=self.store_txn.resolved_ftq_idx_flag,
                ftq_idx_value=self.store_txn.resolved_ftq_idx_value,
            )
            load_debug_event = _wait_load_debug_event(
                env,
                rob_idx_value=main_load.rob_idx_value,
                required_replay_causes=("NK",),
                forbidden_replay_causes=("BC", "FF"),
                max_cycles=self.replay_wait_cycles,
            )

            env.backend.pulse_store_commit(1)
            main_expected.expected_data = self.store_txn.data
            main_writeback = env.wait_load_writeback_observed(
                rob_idx=main_load.rob_idx,
                data=self.store_txn.data,
                max_cycles=_resolve_replay_drain_cycles(env, self.drain_cycles),
            )
            completed = _wait_completed_load_count(
                env,
                completed_before + 1,
                max_cycles=_resolve_replay_drain_cycles(env, self.drain_cycles),
            )
            main_trace = tuple(
                event
                for event in load_debug_trace
                if main_load.rob_idx_value in {
                    event["s1_rob_idx_value"],
                    event["s2_rob_idx_value"],
                    event["s3_rob_idx_value"],
                }
            )

        committed_store_view = env.wait_store_materialized(
            store_sq_ptr.value,
            expected_addr=self.store_txn.addr,
            expected_data=self.store_txn.data,
            expected_mmio=False,
            require_committed=True,
            max_cycles=self.materialize_cycles,
        )
        final_state = SequenceState(
            next_lq_ptr=ptr_inc(main_load.lq_ptr, env.config.sequence.load_queue_size),
            sq_ptr=ptr_inc(self.store_txn.sq_ptr, env.config.sequence.store_queue_size),
        )
        if self.assert_no_outstanding:
            env.assert_no_outstanding()

        return ScalarPipelineStldNukeSequenceResult(
            main_load=main_load,
            store_sq_ptr=store_sq_ptr,
            load_debug_event=load_debug_event,
            load_debug_trace=main_trace,
            nk_observed=True,
            replay_events=tuple(replay_trace),
            nuke_query_trace=tuple(nuke_query_trace),
            main_writeback=main_writeback,
            committed_store_view=committed_store_view,
            final_state=final_state,
            completed_load_count=completed,
        )


class ScalarBankConflictSqDataInvalidNukeSequence:
    def __init__(
        self,
        *,
        store_txn,
        initial_lq_ptr: QueuePtr,
        lead_load_req_id: int,
        victim_load_req_id: int,
        lead_addr: int | None = None,
        replay_wait_cycles: int = 200,
        materialize_cycles: int = 200,
        drain_cycles: int | None = None,
        assert_no_outstanding: bool = False,
    ) -> None:
        self.store_txn = store_txn
        self.initial_lq_ptr = initial_lq_ptr
        self.lead_load_req_id = int(lead_load_req_id)
        self.victim_load_req_id = int(victim_load_req_id)
        self.lead_addr = None if lead_addr is None else int(lead_addr)
        self.replay_wait_cycles = int(replay_wait_cycles)
        self.materialize_cycles = int(materialize_cycles)
        self.drain_cycles = drain_cycles
        self.assert_no_outstanding = assert_no_outstanding

    def run(self, env) -> ScalarBankConflictSqDataInvalidNukeSequenceResult:
        completed_before = env.get_completed_load_count()
        env.backend.prepare(self.store_txn)
        store_sq_ptr = enqueue_scalar_store(
            env,
            req_id=self.store_txn.req_id,
            sq_ptr=self.store_txn.sq_ptr,
            enq_port=self.store_txn.enq_port,
            rob_idx=self.store_txn.rob_idx,
        )
        younger_sq_ptr = ptr_inc(self.store_txn.sq_ptr, env.config.sequence.store_queue_size)
        lead_addr = self.store_txn.addr + 0x40 if self.lead_addr is None else self.lead_addr
        lead_load = LoadTxn(
            req_id=self.lead_load_req_id,
            addr=lead_addr,
            lq_ptr=self.initial_lq_ptr,
            sq_ptr=younger_sq_ptr,
            issue_lane=0,
        )
        victim_load = LoadTxn(
            req_id=self.victim_load_req_id,
            addr=self.store_txn.addr,
            lq_ptr=ptr_inc(self.initial_lq_ptr, env.config.sequence.load_queue_size),
            sq_ptr=younger_sq_ptr,
            issue_lane=1,
            store_set_hit=1,
        )
        lead_expected = expect_load(env, lead_load)
        victim_expected = expect_load(env, victim_load)

        with (
            _capture_load_debug_trace(env, max_events=128) as load_debug_trace,
            _capture_replay_events(
                env,
                rob_idx=victim_load.rob_idx,
                max_events=64,
            ) as replay_trace,
            _capture_nuke_query_trace(env, max_events=64) as nuke_query_trace,
            _capture_writeback_trace(env, max_events=64) as writeback_trace,
        ):
            def _match_writeback(event, *, rob_idx, data):
                if event.get("int_wen") != 1:
                    return False
                if event.get("rob_idx_flag") != rob_idx.flag or event.get("rob_idx_value") != rob_idx.value:
                    return False
                if data is not None and event.get("data") != data:
                    return False
                return True

            ScalarLoadBatchSameCycleSequence((lead_load, victim_load)).run(env)
            issue_scalar_sta(
                env,
                req_id=self.store_txn.req_id,
                sq_ptr=store_sq_ptr,
                addr=self.store_txn.addr,
                lane=self.store_txn.sta_lane,
                rob_idx=self.store_txn.rob_idx,
                ftq_idx_flag=self.store_txn.resolved_ftq_idx_flag,
                ftq_idx_value=self.store_txn.resolved_ftq_idx_value,
            )
            sq_forward_event = _wait_sq_forward_event(
                env,
                expected_sq_idx=store_sq_ptr.value,
                expected_data_invalid_valid=1,
                expected_match_invalid=0,
                expected_forward_invalid=0,
                max_cycles=self.replay_wait_cycles,
            )
            # For this combo, issuing STD only after the transient pipeline NK is visible
            # makes the recovery path deterministic instead of collapsing into FF-only spin.
            _wait_load_debug_event(
                env,
                rob_idx_value=victim_load.rob_idx_value,
                required_replay_causes=("NK",),
                max_cycles=self.replay_wait_cycles,
            )
            load_debug_event = _wait_load_debug_event(
                env,
                rob_idx_value=victim_load.rob_idx_value,
                required_replay_causes=("BC", "FF"),
                forbidden_replay_causes=("RAW", "RAR"),
                max_cycles=self.replay_wait_cycles,
            )

            issue_scalar_std(
                env,
                req_id=self.store_txn.req_id,
                sq_ptr=store_sq_ptr,
                data=self.store_txn.data,
                lane=self.store_txn.std_lane,
                rob_idx=self.store_txn.rob_idx,
                ftq_idx_flag=self.store_txn.resolved_ftq_idx_flag,
                ftq_idx_value=self.store_txn.resolved_ftq_idx_value,
            )
            env.backend.pulse_store_commit(1)
            victim_expected.expected_data = self.store_txn.data
            completed = _wait_completed_load_count(
                env,
                completed_before + 2,
                max_cycles=_resolve_replay_drain_cycles(env, self.drain_cycles),
            )
            lead_writeback = next(
                (
                    event
                    for event in writeback_trace
                    if _match_writeback(
                        event,
                        rob_idx=lead_load.rob_idx,
                        data=lead_expected.expected_data,
                    )
                ),
                None,
            )
            victim_writeback = next(
                (
                    event
                    for event in writeback_trace
                    if _match_writeback(
                        event,
                        rob_idx=victim_load.rob_idx,
                        data=self.store_txn.data,
                    )
                ),
                None,
            )
            if lead_writeback is None or victim_writeback is None:
                raise TimeoutError(
                    "组合场景 writeback 观测不完整: "
                    f"lead={lead_writeback is not None}, victim={victim_writeback is not None}, "
                    f"trace={tuple(writeback_trace)}"
                )
            victim_trace = tuple(
                event
                for event in load_debug_trace
                if victim_load.rob_idx_value in {
                    event["s1_rob_idx_value"],
                    event["s2_rob_idx_value"],
                    event["s3_rob_idx_value"],
                }
            )
            nk_observed = any("NK" in event["replay_causes"] for event in victim_trace)

        committed_store_view = env.wait_store_materialized(
            store_sq_ptr.value,
            expected_addr=self.store_txn.addr,
            expected_data=self.store_txn.data,
            expected_mmio=False,
            require_committed=True,
            max_cycles=self.materialize_cycles,
        )
        final_state = SequenceState(
            next_lq_ptr=ptr_inc(victim_load.lq_ptr, env.config.sequence.load_queue_size),
            sq_ptr=younger_sq_ptr,
        )
        if self.assert_no_outstanding:
            env.assert_no_outstanding()

        return ScalarBankConflictSqDataInvalidNukeSequenceResult(
            lead_load=lead_load,
            victim_load=victim_load,
            store_sq_ptr=store_sq_ptr,
            sq_forward_event=sq_forward_event,
            load_debug_event=load_debug_event,
            load_debug_trace=victim_trace,
            nk_observed=nk_observed,
            replay_events=tuple(replay_trace),
            nuke_query_trace=tuple(nuke_query_trace),
            lead_writeback=lead_writeback,
            victim_writeback=victim_writeback,
            committed_store_view=committed_store_view,
            final_state=final_state,
            completed_load_count=completed,
        )


class ScalarSqDataInvalidMatchInvalidTriggerSequence:
    def __init__(
        self,
        *,
        main_va: int,
        main_pa: int,
        store_txn,
        main_load_req_id: int,
        initial_state: SequenceState,
        activate_root_pt_addr: int | None = None,
        tlb_prime_req_id: int | None = None,
        tlb_prime_va: int | None = None,
        tlb_prime_pa: int | None = None,
        tlb_prime_data: int | None = None,
        settle_cycles: int = 4,
        observation_cycles: int = 200,
        store_materialize_cycles: int = 200,
        post_violation_cycles: int = 8,
    ) -> None:
        self.main_va = int(main_va)
        self.main_pa = int(main_pa)
        self.store_txn = store_txn
        self.main_load_req_id = int(main_load_req_id)
        self.initial_state = initial_state
        self.activate_root_pt_addr = None if activate_root_pt_addr is None else int(activate_root_pt_addr)
        self.tlb_prime_req_id = None if tlb_prime_req_id is None else int(tlb_prime_req_id)
        self.tlb_prime_va = None if tlb_prime_va is None else int(tlb_prime_va)
        self.tlb_prime_pa = None if tlb_prime_pa is None else int(tlb_prime_pa)
        self.tlb_prime_data = None if tlb_prime_data is None else int(tlb_prime_data)
        self.settle_cycles = int(settle_cycles)
        self.observation_cycles = int(observation_cycles)
        self.store_materialize_cycles = int(store_materialize_cycles)
        self.post_violation_cycles = int(post_violation_cycles)

        tlb_prime_fields = (
            self.tlb_prime_req_id,
            self.tlb_prime_va,
            self.tlb_prime_pa,
            self.tlb_prime_data,
        )
        if any(field is not None for field in tlb_prime_fields) and any(field is None for field in tlb_prime_fields):
            raise ValueError("tlb_prime_* 参数需要同时提供")
        if self.tlb_prime_req_id is not None and self.activate_root_pt_addr is None:
            raise ValueError("配置 tlb_prime_* 时必须同时提供 activate_root_pt_addr")

    def run(self, env) -> ScalarSqDataInvalidMatchInvalidTriggerSequenceResult:
        store_queue_size = env.config.sequence.store_queue_size
        load_queue_size = env.config.sequence.load_queue_size
        completed_before_main = env.get_completed_load_count()

        env.backend.prepare(self.store_txn)
        store_sq_ptr = enqueue_scalar_store(
            env,
            req_id=self.store_txn.req_id,
            sq_ptr=self.store_txn.sq_ptr,
            enq_port=self.store_txn.enq_port,
            rob_idx=self.store_txn.rob_idx,
        )
        issue_scalar_sta(
            env,
            req_id=self.store_txn.req_id,
            sq_ptr=store_sq_ptr,
            addr=self.store_txn.addr,
            lane=self.store_txn.sta_lane,
            rob_idx=self.store_txn.rob_idx,
            ftq_idx_flag=self.store_txn.resolved_ftq_idx_flag,
            ftq_idx_value=self.store_txn.resolved_ftq_idx_value,
        )
        _wait_store_addr_observed(
            env,
            store_sq_ptr.value,
            self.main_va,
            max_cycles=self.store_materialize_cycles,
        )
        younger_sq_ptr = ptr_inc(store_sq_ptr, store_queue_size)
        next_lq_ptr = self.initial_state.next_lq_ptr
        tlb_prime_writeback = None

        if self.activate_root_pt_addr is not None:
            env.mmu.enable_sv39(root_pt_addr=self.activate_root_pt_addr, settle_cycles=self.settle_cycles)

        if self.tlb_prime_req_id is not None:
            tlb_prime_load = LoadTxn(
                req_id=self.tlb_prime_req_id,
                addr=self.tlb_prime_va,
                lq_ptr=next_lq_ptr,
                sq_ptr=younger_sq_ptr,
            )
            env.backend.prepare(tlb_prime_load)
            env.expect_scalar_load(
                rob_idx=tlb_prime_load.rob_idx,
                pdest=tlb_prime_load.resolved_pdest,
                addr=self.tlb_prime_pa,
                size=tlb_prime_load.size,
                mask=tlb_prime_load.mask,
            )
            env.backend.execute(
                BackendSendPlan.from_steps(
                    EnqueueLoadStep.from_txn(tlb_prime_load),
                    IssueCyclePlan.from_ops(IssueOp.load_from_txn(tlb_prime_load)),
                )
            )
            tlb_prime_writeback = env.wait_load_writeback_observed(
                rob_idx=tlb_prime_load.rob_idx,
                data=self.tlb_prime_data,
                max_cycles=_resolve_replay_drain_cycles(env, None),
            )
            completed_before_main = _wait_completed_load_count(
                env,
                completed_before_main + 1,
                max_cycles=_resolve_replay_drain_cycles(env, None),
            )
            next_lq_ptr = ptr_inc(next_lq_ptr, load_queue_size)

        main_load = LoadTxn(
            req_id=self.main_load_req_id,
            addr=self.main_va,
            lq_ptr=next_lq_ptr,
            sq_ptr=younger_sq_ptr,
        )
        env.backend.prepare(main_load)
        transport_stats_before_main = _snapshot_transport_stats(env)
        with (
            _capture_writeback_trace(env, max_events=32) as main_writeback_trace,
            _capture_replay_events(
                env,
                rob_idx=main_load.rob_idx,
                max_events=64,
            ) as replay_trace,
        ):
            env.backend.execute(
                BackendSendPlan.from_steps(
                    EnqueueLoadStep.from_txn(main_load),
                    IssueCyclePlan.from_ops(IssueOp.load_from_txn(main_load)),
                )
            )
            sq_forward_event, memory_violation, initial_replay_events, dcache_miss_signal = _wait_sq_matchinvalid_and_violation(
                env,
                lane=main_load.issue_lane,
                expected_sq_idx=store_sq_ptr.value,
                rob_idx=main_load.rob_idx,
                max_cycles=self.observation_cycles,
            )
            if self.post_violation_cycles > 0:
                env.advance_cycles(self.post_violation_cycles)

            issue_scalar_std(
                env,
                req_id=self.store_txn.req_id,
                sq_ptr=store_sq_ptr,
                data=self.store_txn.data,
                lane=self.store_txn.std_lane,
                rob_idx=self.store_txn.rob_idx,
                ftq_idx_flag=self.store_txn.resolved_ftq_idx_flag,
                ftq_idx_value=self.store_txn.resolved_ftq_idx_value,
            )
            replay_expected = env.expect_scalar_load(
                rob_idx=main_load.rob_idx,
                pdest=main_load.resolved_pdest,
                addr=self.main_pa,
                size=main_load.size,
                mask=main_load.mask,
            )
            replay_expected.expected_data = self.store_txn.data
            env.wait_load_writeback_observed(
                rob_idx=main_load.rob_idx,
                data=self.store_txn.data,
                max_cycles=_resolve_replay_drain_cycles(env, None),
            )
            _wait_completed_load_count(
                env,
                completed_before_main + 1,
                max_cycles=_resolve_replay_drain_cycles(env, None),
            )
            transport_stats_after_recovery = _snapshot_transport_stats(env)

        committed_store_view = env.wait_store_materialized(
            store_sq_ptr.value,
            expected_addr=self.main_va,
            expected_data=self.store_txn.data,
            expected_mmio=None,
            require_committed=True,
            max_cycles=self.store_materialize_cycles,
        )
        merged_replay_events = []
        seen_replay_keys = set()
        for event in tuple(initial_replay_events) + tuple(replay_trace):
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
            if event_key in seen_replay_keys:
                continue
            seen_replay_keys.add(event_key)
            merged_replay_events.append(dict(event))

        return ScalarSqDataInvalidMatchInvalidTriggerSequenceResult(
            tlb_prime_writeback=tlb_prime_writeback,
            sq_forward_event=sq_forward_event,
            memory_violation=memory_violation,
            main_writeback_trace=tuple(
                event
                for event in main_writeback_trace
                if event["rob_idx_flag"] == main_load.rob_idx_flag and event["rob_idx_value"] == main_load.rob_idx_value
            ),
            main_load=main_load,
            store_sq_ptr=store_sq_ptr,
            committed_store_view=committed_store_view,
            final_state=SequenceState(
                next_lq_ptr=ptr_inc(next_lq_ptr, load_queue_size),
                sq_ptr=younger_sq_ptr,
            ),
            transport_stats_before_main=transport_stats_before_main,
            transport_stats_after_recovery=transport_stats_after_recovery,
            replay_events=tuple(merged_replay_events),
            dcache_error_valid=env._read_optional_dut_signal("io_dcacheError_ecc_error_valid"),
            dcache_miss_signal=dcache_miss_signal,
        )
