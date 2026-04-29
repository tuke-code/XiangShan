# coding=utf-8
"""
MemBlock atomic/AMO reusable sequences.
"""

from dataclasses import dataclass

from transactions import AtomicTxn, LoadTxn, QueuePtr, ptr_inc

from .memblock_sequences import SequenceState


@dataclass(frozen=True)
class AtomicSequenceResult:
    txn: AtomicTxn
    initial_value: int
    expected_return_value: int
    expected_committed_value: int
    writeback: dict
    load_txn: LoadTxn
    final_state: SequenceState
    completed_load_count: int


class AtomicSequence:
    def __init__(
        self,
        txn: AtomicTxn,
        *,
        initial_state: SequenceState,
        load_req_id: int | None = None,
        load_issue_lane: int = 0,
        load_wait_cycles: int = 200,
        quiesce_cycles: int = 200,
        settle_cycles: int = 2,
        assert_no_outstanding: bool = False,
    ) -> None:
        self.txn = txn
        self.initial_state = initial_state
        self.load_req_id = load_req_id
        self.load_issue_lane = int(load_issue_lane)
        self.load_wait_cycles = int(load_wait_cycles)
        self.quiesce_cycles = int(quiesce_cycles)
        self.settle_cycles = int(settle_cycles)
        self.assert_no_outstanding = assert_no_outstanding

    def run(self, env) -> AtomicSequenceResult:
        env.backend.prepare_atomic(self.txn)

        initial_value = env.memory.read_masked(
            self.txn.addr,
            self.txn.store_mask,
            width_bytes=self.txn.size,
        )
        expected_return_value = self.txn.predict_return_value(initial_value)
        expected_committed_value = self.txn.predict_committed_value(initial_value)

        env.backend.send_atomic(self.txn)
        writeback = env.wait_int_writeback_by_rob_pdest(
            rob_idx=self.txn.rob_idx,
            pdest=self.txn.resolved_pdest,
            data=expected_return_value,
            require_from_load_unit=False,
            max_cycles=self.load_wait_cycles,
        )
        assert writeback["int_wen"] == 1, f"AMO req_id={self.txn.req_id} 未写回 Int RF"
        assert writeback["fp_wen"] == 0, f"AMO req_id={self.txn.req_id} 不应写回 FP RF"
        assert not any(writeback["exception_bits"] or ()), (
            f"AMO req_id={self.txn.req_id} 出现异常写回: {writeback['exception_bits']}"
        )

        env.wait_memory_quiesce(max_cycles=self.quiesce_cycles)
        if self.settle_cycles > 0:
            env.advance_cycles(self.settle_cycles)

        env.memory.apply_masked_write(
            self.txn.addr,
            expected_committed_value,
            self.txn.store_mask,
            width_bytes=self.txn.size,
        )

        completed_before = env.get_completed_load_count()
        load_txn = LoadTxn(
            req_id=self.txn.req_id + 0x1000 if self.load_req_id is None else int(self.load_req_id),
            addr=self.txn.addr,
            lq_ptr=self.initial_state.next_lq_ptr,
            sq_ptr=self.initial_state.sq_ptr,
            size=self.txn.size,
            mask=self.txn.store_mask,
            issue_lane=self.load_issue_lane,
        )
        env.backend.prepare(load_txn)
        env.expect_scalar_load(
            rob_idx=load_txn.rob_idx,
            pdest=load_txn.resolved_pdest,
            addr=load_txn.addr,
            size=load_txn.size,
            mask=load_txn.mask,
            fp_wen=load_txn.fp_wen,
        )
        env.backend.send(load_txn)
        completed_load_count = env.wait_completed_load_count(
            completed_before + 1,
            max_cycles=self.load_wait_cycles,
        )
        env.drain_writebacks(max_cycles=self.load_wait_cycles)

        final_state = SequenceState(
            next_lq_ptr=ptr_inc(load_txn.lq_ptr, int(env.config.sequence.load_queue_size)),
            sq_ptr=QueuePtr(flag=self.initial_state.sq_ptr.flag, value=self.initial_state.sq_ptr.value),
        )
        if self.assert_no_outstanding:
            env.assert_no_outstanding()

        return AtomicSequenceResult(
            txn=self.txn,
            initial_value=initial_value,
            expected_return_value=expected_return_value,
            expected_committed_value=expected_committed_value,
            writeback=writeback,
            load_txn=load_txn,
            final_state=final_state,
            completed_load_count=completed_load_count,
        )
