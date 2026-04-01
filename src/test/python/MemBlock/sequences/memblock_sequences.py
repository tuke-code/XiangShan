# coding=utf-8
"""
MemBlock 最小 sequence 层。
"""

from dataclasses import dataclass

from request_apis import (
    QueuePtr,
    expect_load,
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
