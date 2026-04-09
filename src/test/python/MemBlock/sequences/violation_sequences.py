# coding=utf-8
"""
MemBlock replay / violation related sequences.
"""

from dataclasses import dataclass

from request_apis import LoadTxn, QueuePtr, enqueue_scalar_store, expect_load, issue_scalar_sta, issue_scalar_std, ptr_inc, send_load

from .memblock_sequences import (
    SequenceState,
    _resolve_replay_drain_cycles,
    _wait_completed_load_count,
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
