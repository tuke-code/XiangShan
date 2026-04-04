# coding=utf-8
"""
MemBlock 可复用测试 sequence。
"""

from dataclasses import dataclass

from request_apis import (
    LoadTxn,
    QueuePtr,
    StoreTxn,
    enqueue_scalar_store,
    expect_load,
    issue_scalar_sta,
    issue_scalar_std,
    ptr_inc,
    reset_env_and_wait_backend,
    send_load,
    send_store,
)


@dataclass(frozen=True)
class SequenceState:
    next_lq_ptr: QueuePtr
    sq_ptr: QueuePtr


@dataclass(frozen=True)
class ScalarLoadSequenceResult:
    txn: object
    next_lq_ptr: QueuePtr
    completed_load_count: int


@dataclass(frozen=True)
class ScalarStoreSequenceResult:
    txn: object
    allocated_sq_ptr: QueuePtr
    next_sq_ptr: QueuePtr
    store_view: object


@dataclass(frozen=True)
class ScalarLoadBurstSequenceResult:
    final_state: SequenceState
    completed_load_count: int
    transport_stats_before: dict[str, int]
    transport_stats_after: dict[str, int]


@dataclass(frozen=True)
class ScalarStoreCommitSequenceResult:
    store_result: ScalarStoreSequenceResult
    committed_store_view: object


@dataclass(frozen=True)
class ScalarStoreThenLoadSequenceResult:
    store_result: ScalarStoreSequenceResult
    load_result: ScalarLoadSequenceResult
    final_state: SequenceState


@dataclass(frozen=True)
class ScalarStoreFlushSequenceResult:
    store_result: ScalarStoreSequenceResult
    drain_summary: dict
    outer_write_delta: int
    sbuffer_drain_delta: int


@dataclass(frozen=True)
class ScalarMixedTrafficSequenceResult:
    final_state: SequenceState
    total_stores: int
    total_loads: int
    completed_load_count: int
    drain_summary: dict | None


@dataclass(frozen=True)
class ScalarForwardFailReplaySequenceResult:
    store_sq_ptr: QueuePtr
    replay_event: dict
    load_result: ScalarLoadSequenceResult
    final_state: SequenceState
    committed_store_view: object


@dataclass(frozen=True)
class ScalarCacheMissReplaySequenceResult:
    load_result: ScalarLoadSequenceResult
    replay_event: dict
    final_state: SequenceState
    transport_stats_before: dict[str, int]
    transport_stats_after: dict[str, int]


@dataclass(frozen=True)
class ScalarNcReplaySequenceResult:
    load_result: ScalarLoadSequenceResult
    replay_event: dict
    final_state: SequenceState
    transport_stats_before: dict[str, int]
    transport_stats_after: dict[str, int]


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


def _snapshot_transport_stats(env) -> dict[str, int]:
    return {key: int(value) for key, value in env.get_transport_stats().items()}


def _wait_store_addr_observed(env, sq_idx: int, expected_addr: int, max_cycles: int = 200):
    for _ in range(max_cycles):
        store = env.get_store_view(sq_idx)
        if store is not None and store.allocated and store.addr == expected_addr:
            return store
        env.Step(1)
    raise TimeoutError(f"等待 store 地址收敛超时: sqIdx={sq_idx}, addr=0x{expected_addr:x}")


def _resolve_replay_drain_cycles(env, drain_cycles: int | None) -> int:
    if drain_cycles is not None:
        return int(drain_cycles)
    return max(int(env.config.sequence.load_drain_cycles), 1024)


def _wait_completed_load_count(env, target_count: int, max_cycles: int = 200) -> int:
    for _ in range(max_cycles):
        completed = env.get_completed_load_count()
        if completed >= int(target_count):
            return completed
        env.Step(1)
    raise TimeoutError(f"等待 completed_load_count 达到 {target_count} 超时")


class ResetEnvSequence:
    def __init__(
        self,
        *,
        require_issue_lanes=(),
        require_lq_ready: bool = False,
        require_sq_ready: bool = False,
        reset_cycles: int | None = None,
        settle_cycles: int | None = None,
    ) -> None:
        self.require_issue_lanes = tuple(require_issue_lanes)
        self.require_lq_ready = require_lq_ready
        self.require_sq_ready = require_sq_ready
        self.reset_cycles = reset_cycles
        self.settle_cycles = settle_cycles

    def run(self, env) -> SequenceState:
        reset_cycles = env.config.sequence.reset_cycles if self.reset_cycles is None else self.reset_cycles
        settle_cycles = env.config.sequence.reset_settle_cycles if self.settle_cycles is None else self.settle_cycles
        reset_env_and_wait_backend(
            env,
            reset_cycles=reset_cycles,
            settle_cycles=settle_cycles,
            require_issue_lanes=self.require_issue_lanes,
            require_lq_ready=self.require_lq_ready,
            require_sq_ready=self.require_sq_ready,
        )
        return SequenceState(
            next_lq_ptr=QueuePtr(flag=0, value=0),
            sq_ptr=QueuePtr(flag=0, value=0),
        )


class ScalarLoadSequence:
    def __init__(
        self,
        txn,
        *,
        drain_cycles: int | None = None,
        expected_completed_loads: int | None = None,
        load_queue_size: int | None = None,
        assert_no_outstanding: bool = False,
    ) -> None:
        self.txn = txn
        self.drain_cycles = drain_cycles
        self.expected_completed_loads = expected_completed_loads
        self.load_queue_size = load_queue_size
        self.assert_no_outstanding = assert_no_outstanding

    def run(self, env) -> ScalarLoadSequenceResult:
        drain_cycles = env.config.sequence.load_drain_cycles if self.drain_cycles is None else self.drain_cycles
        load_queue_size = env.config.sequence.load_queue_size if self.load_queue_size is None else self.load_queue_size
        send_load(env, self.txn)
        expect_load(env, self.txn)
        env.drain_writebacks(max_cycles=drain_cycles)
        completed = env.get_completed_load_count()
        if self.expected_completed_loads is not None:
            assert completed == self.expected_completed_loads, (
                f"load req_id={self.txn.req_id} 未在限定周期内完成"
            )
        if self.assert_no_outstanding:
            env.assert_no_outstanding()
        return ScalarLoadSequenceResult(
            txn=self.txn,
            next_lq_ptr=ptr_inc(self.txn.lq_ptr, load_queue_size),
            completed_load_count=completed,
        )


class ScalarLoadBurstSequence:
    def __init__(
        self,
        requests,
        *,
        initial_state: SequenceState,
        drain_cycles: int | None = None,
        size: int = 8,
        mask: int = 0xFF,
        enq_port: int = 0,
        issue_lane: int = 0,
        assert_no_outstanding: bool = False,
    ) -> None:
        self.requests = list(requests)
        self.initial_state = initial_state
        self.drain_cycles = drain_cycles
        self.size = size
        self.mask = mask
        self.enq_port = enq_port
        self.issue_lane = issue_lane
        self.assert_no_outstanding = assert_no_outstanding

    def run(self, env) -> ScalarLoadBurstSequenceResult:
        state = self.initial_state
        completed_before = env.get_completed_load_count()
        stats_before = _snapshot_transport_stats(env)

        for idx, (req_id, addr) in enumerate(self.requests):
            result = ScalarLoadSequence(
                LoadTxn(
                    req_id=req_id,
                    addr=addr,
                    lq_ptr=state.next_lq_ptr,
                    sq_ptr=state.sq_ptr,
                    size=self.size,
                    mask=self.mask,
                    enq_port=self.enq_port,
                    issue_lane=self.issue_lane,
                ),
                drain_cycles=self.drain_cycles,
                expected_completed_loads=completed_before + idx + 1,
            ).run(env)
            state = SequenceState(
                next_lq_ptr=result.next_lq_ptr,
                sq_ptr=state.sq_ptr,
            )

        if self.assert_no_outstanding:
            env.assert_no_outstanding()

        return ScalarLoadBurstSequenceResult(
            final_state=state,
            completed_load_count=env.get_completed_load_count(),
            transport_stats_before=stats_before,
            transport_stats_after=_snapshot_transport_stats(env),
        )


class ScalarStoreSequence:
    def __init__(
        self,
        txn,
        *,
        expected_mmio: bool | None = None,
        require_committed: bool = False,
        materialize_cycles: int | None = None,
        store_queue_size: int | None = None,
    ) -> None:
        self.txn = txn
        self.expected_mmio = expected_mmio
        self.require_committed = require_committed
        self.materialize_cycles = materialize_cycles
        self.store_queue_size = store_queue_size

    def run(self, env) -> ScalarStoreSequenceResult:
        materialize_cycles = (
            env.config.sequence.store_materialize_cycles
            if self.materialize_cycles is None
            else self.materialize_cycles
        )
        store_queue_size = env.config.sequence.store_queue_size if self.store_queue_size is None else self.store_queue_size
        allocated_sq_ptr = send_store(env, self.txn)
        store_view = env.wait_store_materialized(
            allocated_sq_ptr.value,
            expected_addr=self.txn.addr,
            expected_data=self.txn.data,
            expected_mmio=self.expected_mmio,
            require_committed=self.require_committed,
            max_cycles=materialize_cycles,
        )
        return ScalarStoreSequenceResult(
            txn=self.txn,
            allocated_sq_ptr=allocated_sq_ptr,
            next_sq_ptr=ptr_inc(self.txn.sq_ptr, store_queue_size),
            store_view=store_view,
        )


class ScalarStoreCommitSequence:
    def __init__(
        self,
        txn,
        *,
        expected_mmio: bool | None = None,
        require_committed: bool = False,
        materialize_cycles: int | None = None,
        settle_cycles: int | None = None,
        wait_quiesce: bool = False,
        quiesce_cycles: int = 300,
    ) -> None:
        self.txn = txn
        self.expected_mmio = expected_mmio
        self.require_committed = require_committed
        self.materialize_cycles = materialize_cycles
        self.settle_cycles = settle_cycles
        self.wait_quiesce = wait_quiesce
        self.quiesce_cycles = quiesce_cycles

    def run(self, env) -> ScalarStoreCommitSequenceResult:
        # 真实 DUT 中 store 的 committed 由 ROB 提交边界和 scommit 共同驱动，
        # 不能在发完 STA/STD 后直接假定 store 已 committed。
        settle_cycles = env.config.sequence.store_settle_cycles if self.settle_cycles is None else self.settle_cycles
        store_result = ScalarStoreSequence(
            self.txn,
            expected_mmio=self.expected_mmio,
            require_committed=False,
            materialize_cycles=self.materialize_cycles,
        ).run(env)
        env.pulse_store_commit(1)
        if settle_cycles > 0:
            env.Step(settle_cycles)
        if self.require_committed:
            env.wait_store_materialized(
                store_result.allocated_sq_ptr.value,
                expected_addr=self.txn.addr,
                expected_data=self.txn.data,
                expected_mmio=self.expected_mmio,
                require_committed=True,
                max_cycles=(
                    env.config.sequence.store_materialize_cycles
                    if self.materialize_cycles is None
                    else self.materialize_cycles
                ),
            )
        if self.wait_quiesce:
            env.wait_memory_quiesce(max_cycles=self.quiesce_cycles)
        return ScalarStoreCommitSequenceResult(
            store_result=store_result,
            committed_store_view=env.get_store_view(store_result.allocated_sq_ptr.value),
        )


class ScalarStoreThenLoadSequence:
    def __init__(
        self,
        store_txn,
        *,
        initial_lq_ptr: QueuePtr,
        load_req_id: int,
        load_addr: int | None = None,
        expected_mmio: bool | None = None,
        require_committed: bool = True,
        materialize_cycles: int | None = None,
        drain_cycles: int | None = None,
        size: int = 8,
        mask: int = 0xFF,
        assert_no_outstanding: bool = False,
    ) -> None:
        self.store_txn = store_txn
        self.initial_lq_ptr = initial_lq_ptr
        self.load_req_id = load_req_id
        self.load_addr = load_addr
        self.expected_mmio = expected_mmio
        self.require_committed = require_committed
        self.materialize_cycles = materialize_cycles
        self.drain_cycles = drain_cycles
        self.size = size
        self.mask = mask
        self.assert_no_outstanding = assert_no_outstanding

    def run(self, env) -> ScalarStoreThenLoadSequenceResult:
        completed_before = env.get_completed_load_count()
        committed_store = ScalarStoreCommitSequence(
            self.store_txn,
            expected_mmio=self.expected_mmio,
            require_committed=self.require_committed,
            materialize_cycles=self.materialize_cycles,
        ).run(env)
        store_result = ScalarStoreSequenceResult(
            txn=committed_store.store_result.txn,
            allocated_sq_ptr=committed_store.store_result.allocated_sq_ptr,
            next_sq_ptr=committed_store.store_result.next_sq_ptr,
            store_view=(
                committed_store.committed_store_view
                if committed_store.committed_store_view is not None
                else committed_store.store_result.store_view
            ),
        )
        load_result = ScalarLoadSequence(
            LoadTxn(
                req_id=self.load_req_id,
                addr=self.store_txn.addr if self.load_addr is None else self.load_addr,
                lq_ptr=self.initial_lq_ptr,
                sq_ptr=store_result.next_sq_ptr,
                size=self.size,
                mask=self.mask,
            ),
            drain_cycles=self.drain_cycles,
            expected_completed_loads=completed_before + 1,
        ).run(env)
        final_state = SequenceState(
            next_lq_ptr=load_result.next_lq_ptr,
            sq_ptr=store_result.next_sq_ptr,
        )
        if self.assert_no_outstanding:
            env.assert_no_outstanding()
        return ScalarStoreThenLoadSequenceResult(
            store_result=store_result,
            load_result=load_result,
            final_state=final_state,
        )


class FlushStoreBuffersSequence:
    def __init__(self, *, max_cycles: int | None = None, settle_cycles: int | None = None) -> None:
        self.max_cycles = max_cycles
        self.settle_cycles = settle_cycles

    def run(self, env) -> dict:
        max_cycles = env.config.sequence.store_flush_cycles if self.max_cycles is None else self.max_cycles
        settle_cycles = env.config.sequence.store_settle_cycles if self.settle_cycles is None else self.settle_cycles
        return env.flush_store_buffers_and_wait(
            max_cycles=max_cycles,
            settle_cycles=settle_cycles,
        )


class ScalarStoreFlushSequence:
    def __init__(
        self,
        txn,
        *,
        expected_mmio: bool | None = False,
        require_committed: bool = True,
        materialize_cycles: int | None = None,
        wait_for_sbuffer_drain: bool = True,
        sbuffer_wait_cycles: int = 300,
        max_cycles: int | None = None,
        settle_cycles: int | None = None,
        assert_no_outstanding: bool = False,
    ) -> None:
        self.txn = txn
        self.expected_mmio = expected_mmio
        self.require_committed = require_committed
        self.materialize_cycles = materialize_cycles
        self.wait_for_sbuffer_drain = wait_for_sbuffer_drain
        self.sbuffer_wait_cycles = sbuffer_wait_cycles
        self.max_cycles = max_cycles
        self.settle_cycles = settle_cycles
        self.assert_no_outstanding = assert_no_outstanding

    def run(self, env) -> ScalarStoreFlushSequenceResult:
        outer_writes_before = env.get_counter("outer_write_request_count")
        sbuffer_drains_before = env.get_counter("sbuffer_drain_count")
        committed_store = ScalarStoreCommitSequence(
            self.txn,
            expected_mmio=self.expected_mmio,
            require_committed=self.require_committed,
            materialize_cycles=self.materialize_cycles,
            settle_cycles=self.settle_cycles,
        ).run(env)
        store_result = ScalarStoreSequenceResult(
            txn=committed_store.store_result.txn,
            allocated_sq_ptr=committed_store.store_result.allocated_sq_ptr,
            next_sq_ptr=committed_store.store_result.next_sq_ptr,
            store_view=(
                committed_store.committed_store_view
                if committed_store.committed_store_view is not None
                else committed_store.store_result.store_view
            ),
        )
        if self.wait_for_sbuffer_drain:
            env.wait_counter_growth(
                "sbuffer_drain_count",
                sbuffer_drains_before,
                max_cycles=self.sbuffer_wait_cycles,
            )
        drain_summary = FlushStoreBuffersSequence(
            max_cycles=self.max_cycles,
            settle_cycles=self.settle_cycles,
        ).run(env)
        if self.assert_no_outstanding:
            env.assert_no_outstanding()
        return ScalarStoreFlushSequenceResult(
            store_result=store_result,
            drain_summary=drain_summary,
            outer_write_delta=env.get_counter("outer_write_request_count") - outer_writes_before,
            sbuffer_drain_delta=env.get_counter("sbuffer_drain_count") - sbuffer_drains_before,
        )


class ScalarMixedTrafficSequence:
    def __init__(
        self,
        operations,
        *,
        initial_state: SequenceState,
        store_req_id: int = 0,
        first_load_req_id: int = 1,
        expected_store_mmio: bool | None = False,
        require_store_committed: bool = True,
        load_drain_cycles: int | None = None,
        store_materialize_cycles: int | None = None,
        flush: bool = True,
        flush_max_cycles: int | None = None,
        flush_settle_cycles: int | None = None,
        assert_no_outstanding: bool = False,
    ) -> None:
        self.operations = list(operations)
        self.initial_state = initial_state
        self.store_req_id = store_req_id
        self.first_load_req_id = first_load_req_id
        self.expected_store_mmio = expected_store_mmio
        self.require_store_committed = require_store_committed
        self.load_drain_cycles = load_drain_cycles
        self.store_materialize_cycles = store_materialize_cycles
        self.flush = flush
        self.flush_max_cycles = flush_max_cycles
        self.flush_settle_cycles = flush_settle_cycles
        self.assert_no_outstanding = assert_no_outstanding

    def run(self, env) -> ScalarMixedTrafficSequenceResult:
        state = self.initial_state
        completed_before = env.get_completed_load_count()
        total_stores = 0
        total_loads = 0
        next_load_req_id = self.first_load_req_id
        next_store_req_id = self.store_req_id
        drain_summary = None

        for op_kind, addr, maybe_data in self.operations:
            if op_kind == "store":
                store_result = ScalarStoreCommitSequence(
                    StoreTxn(
                        req_id=next_store_req_id,
                        sq_ptr=state.sq_ptr,
                        addr=addr,
                        data=int(maybe_data),
                    ),
                    expected_mmio=self.expected_store_mmio,
                    require_committed=self.require_store_committed,
                    materialize_cycles=self.store_materialize_cycles,
                ).run(env).store_result
                state = SequenceState(
                    next_lq_ptr=state.next_lq_ptr,
                    sq_ptr=store_result.next_sq_ptr,
                )
                total_stores += 1
                next_store_req_id += 1
                continue

            if op_kind != "load":
                raise ValueError(f"unsupported op_kind: {op_kind}")

            load_result = ScalarLoadSequence(
                LoadTxn(
                    req_id=next_load_req_id,
                    addr=addr,
                    lq_ptr=state.next_lq_ptr,
                    sq_ptr=state.sq_ptr,
                    size=8,
                    mask=0xFF,
                ),
                drain_cycles=self.load_drain_cycles,
                expected_completed_loads=completed_before + total_loads + 1,
            ).run(env)
            state = SequenceState(
                next_lq_ptr=load_result.next_lq_ptr,
                sq_ptr=state.sq_ptr,
            )
            total_loads += 1
            next_load_req_id += 1

        if self.flush:
            drain_summary = FlushStoreBuffersSequence(
                max_cycles=self.flush_max_cycles,
                settle_cycles=self.flush_settle_cycles,
            ).run(env)
        if self.assert_no_outstanding:
            env.assert_no_outstanding()
        return ScalarMixedTrafficSequenceResult(
            final_state=state,
            total_stores=total_stores,
            total_loads=total_loads,
            completed_load_count=env.get_completed_load_count(),
            drain_summary=drain_summary,
        )


class ScalarForwardFailReplaySequence:
    def __init__(
        self,
        store_txn,
        *,
        initial_lq_ptr: QueuePtr,
        load_req_id: int,
        replay_wait_cycles: int = 200,
        materialize_cycles: int = 200,
        drain_cycles: int | None = None,
        size: int = 8,
        mask: int = 0xFF,
        assert_no_outstanding: bool = False,
    ) -> None:
        self.store_txn = store_txn
        self.initial_lq_ptr = initial_lq_ptr
        self.load_req_id = load_req_id
        self.replay_wait_cycles = replay_wait_cycles
        self.materialize_cycles = materialize_cycles
        self.drain_cycles = drain_cycles
        self.size = size
        self.mask = mask
        self.assert_no_outstanding = assert_no_outstanding

    def run(self, env) -> ScalarForwardFailReplaySequenceResult:
        completed_before = env.get_completed_load_count()
        store_sq_ptr = enqueue_scalar_store(
            env,
            req_id=self.store_txn.req_id,
            sq_ptr=self.store_txn.sq_ptr,
            enq_port=self.store_txn.enq_port,
        )
        issue_scalar_sta(
            env,
            req_id=self.store_txn.req_id,
            sq_ptr=store_sq_ptr,
            addr=self.store_txn.addr,
            lane=self.store_txn.sta_lane,
        )
        _wait_store_addr_observed(env, store_sq_ptr.value, self.store_txn.addr, max_cycles=self.materialize_cycles)

        load_txn = LoadTxn(
            req_id=self.load_req_id,
            addr=self.store_txn.addr,
            lq_ptr=self.initial_lq_ptr,
            sq_ptr=ptr_inc(self.store_txn.sq_ptr, env.config.sequence.store_queue_size),
            size=self.size,
            mask=self.mask,
            store_set_hit=1,
        )
        send_load(env, load_txn)
        expect_load(env, load_txn)
        replay_event = env.wait_replay_event(
            cause="FF",
            rob_idx_flag=load_txn.rob_idx_flag,
            rob_idx_value=load_txn.rob_idx_value,
            max_cycles=self.replay_wait_cycles,
        )

        issue_scalar_std(
            env,
            req_id=self.store_txn.req_id,
            sq_ptr=store_sq_ptr,
            data=self.store_txn.data,
            lane=self.store_txn.std_lane,
        )
        env.pulse_store_commit(1)
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
        assert completed == completed_before + 1, f"forward-fail replay load req_id={load_txn.req_id} 未完成"
        load_result = ScalarLoadSequenceResult(
            txn=load_txn,
            next_lq_ptr=ptr_inc(load_txn.lq_ptr, env.config.sequence.load_queue_size),
            completed_load_count=completed,
        )
        final_state = SequenceState(
            next_lq_ptr=load_result.next_lq_ptr,
            sq_ptr=ptr_inc(self.store_txn.sq_ptr, env.config.sequence.store_queue_size),
        )
        if self.assert_no_outstanding:
            env.assert_no_outstanding()
        return ScalarForwardFailReplaySequenceResult(
            store_sq_ptr=store_sq_ptr,
            replay_event=replay_event,
            load_result=load_result,
            final_state=final_state,
            committed_store_view=committed_store_view,
        )


class ScalarCacheMissReplaySequence:
    def __init__(
        self,
        txn,
        *,
        replay_wait_cycles: int = 200,
        drain_cycles: int | None = None,
        expected_completed_loads: int | None = None,
        assert_no_outstanding: bool = False,
    ) -> None:
        self.txn = txn
        self.replay_wait_cycles = replay_wait_cycles
        self.drain_cycles = drain_cycles
        self.expected_completed_loads = expected_completed_loads
        self.assert_no_outstanding = assert_no_outstanding

    def run(self, env) -> ScalarCacheMissReplaySequenceResult:
        stats_before = _snapshot_transport_stats(env)
        send_load(env, self.txn)
        expect_load(env, self.txn)
        replay_event = env.wait_replay_event(
            cause="DM",
            rob_idx_flag=self.txn.rob_idx_flag,
            rob_idx_value=self.txn.rob_idx_value,
            max_cycles=self.replay_wait_cycles,
        )
        env.drain_writebacks(max_cycles=_resolve_replay_drain_cycles(env, self.drain_cycles))
        completed = env.get_completed_load_count()
        if self.expected_completed_loads is not None:
            assert completed == self.expected_completed_loads, f"cache-miss replay load req_id={self.txn.req_id} 未完成"
        load_result = ScalarLoadSequenceResult(
            txn=self.txn,
            next_lq_ptr=ptr_inc(self.txn.lq_ptr, env.config.sequence.load_queue_size),
            completed_load_count=completed,
        )
        final_state = SequenceState(
            next_lq_ptr=load_result.next_lq_ptr,
            sq_ptr=self.txn.sq_ptr,
        )
        if self.assert_no_outstanding:
            env.assert_no_outstanding()
        return ScalarCacheMissReplaySequenceResult(
            load_result=load_result,
            replay_event=replay_event,
            final_state=final_state,
            transport_stats_before=stats_before,
            transport_stats_after=_snapshot_transport_stats(env),
        )


class ScalarNcReplaySequence:
    def __init__(
        self,
        txn,
        *,
        replay_wait_cycles: int = 200,
        drain_cycles: int | None = None,
        expected_completed_loads: int | None = None,
        assert_no_outstanding: bool = False,
    ) -> None:
        self.txn = txn
        self.replay_wait_cycles = replay_wait_cycles
        self.drain_cycles = drain_cycles
        self.expected_completed_loads = expected_completed_loads
        self.assert_no_outstanding = assert_no_outstanding

    def run(self, env) -> ScalarNcReplaySequenceResult:
        stats_before = _snapshot_transport_stats(env)
        send_load(env, self.txn)
        expect_load(env, self.txn)
        replay_event = env.wait_nc_replay_or_nc_out(
            rob_idx_flag=self.txn.rob_idx_flag,
            rob_idx_value=self.txn.rob_idx_value,
            max_cycles=self.replay_wait_cycles,
        )
        env.drain_writebacks(max_cycles=_resolve_replay_drain_cycles(env, self.drain_cycles))
        completed = env.get_completed_load_count()
        if self.expected_completed_loads is not None:
            assert completed == self.expected_completed_loads, f"nc replay load req_id={self.txn.req_id} 未完成"
        load_result = ScalarLoadSequenceResult(
            txn=self.txn,
            next_lq_ptr=ptr_inc(self.txn.lq_ptr, env.config.sequence.load_queue_size),
            completed_load_count=completed,
        )
        final_state = SequenceState(
            next_lq_ptr=load_result.next_lq_ptr,
            sq_ptr=self.txn.sq_ptr,
        )
        if self.assert_no_outstanding:
            env.assert_no_outstanding()
        return ScalarNcReplaySequenceResult(
            load_result=load_result,
            replay_event=replay_event,
            final_state=final_state,
            transport_stats_before=stats_before,
            transport_stats_after=_snapshot_transport_stats(env),
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

        store_sq_ptr = enqueue_scalar_store(
            env,
            req_id=self.store_txn.req_id,
            sq_ptr=self.store_txn.sq_ptr,
            enq_port=self.store_txn.enq_port,
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
        )
        issue_scalar_sta(
            env,
            req_id=self.store_txn.req_id,
            sq_ptr=store_sq_ptr,
            addr=self.store_txn.addr,
            lane=self.store_txn.sta_lane,
        )
        env.pulse_store_commit(1)
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
            fake_store_sq_ptr = enqueue_scalar_store(
                env,
                req_id=self.fake_store_txn.req_id,
                sq_ptr=self.fake_store_txn.sq_ptr,
                enq_port=self.fake_store_txn.enq_port,
            )
            issue_scalar_std(
                env,
                req_id=self.fake_store_txn.req_id,
                sq_ptr=fake_store_sq_ptr,
                data=self.fake_store_txn.data,
                lane=self.fake_store_txn.std_lane,
            )

            send_load(env, self.older_load_txn)
            expect_load(env, self.older_load_txn)
            env.wait_replay_event(
                cause="MA",
                rob_idx_flag=self.older_load_txn.rob_idx_flag,
                rob_idx_value=self.older_load_txn.rob_idx_value,
                max_cycles=self.replay_wait_cycles,
            )

            send_load(env, self.younger_load_txn)
            younger_writeback = env.wait_load_writeback_observed(
                rob_idx_flag=self.younger_load_txn.rob_idx_flag,
                rob_idx_value=self.younger_load_txn.rob_idx_value,
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
            )
            env.pulse_store_commit(1)
            fake_store_view = env.wait_store_materialized(
                fake_store_sq_ptr.value,
                expected_addr=self.fake_store_txn.addr,
                expected_data=self.fake_store_txn.data,
                expected_mmio=False,
                require_committed=True,
                max_cycles=self.materialize_cycles,
            )

            rar_nuke_response = env.wait_rar_nuke_response(
                rob_idx_flag=self.older_load_txn.rob_idx_flag,
                rob_idx_value=self.older_load_txn.rob_idx_value,
                lq_idx_flag=self.older_load_txn.lq_ptr.flag,
                lq_idx_value=self.older_load_txn.lq_ptr.value,
                max_cycles=self.violation_wait_cycles,
            )
            older_writeback = env.wait_load_writeback_observed(
                rob_idx_flag=self.older_load_txn.rob_idx_flag,
                rob_idx_value=self.older_load_txn.rob_idx_value,
                data=self.release_new_value,
                max_cycles=_resolve_replay_drain_cycles(env, self.drain_cycles),
            )
            violation_candidates = env.collect_replay_window(
                min(16, self.violation_wait_cycles),
                source="memory_violation",
                rob_idx_flag=self.older_load_txn.rob_idx_flag,
                rob_idx_value=self.older_load_txn.rob_idx_value,
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
