# coding=utf-8
"""
Unified backend-facing facade for MemBlock env.
"""

from dataclasses import replace

from transactions import (
    BackendSendPlan,
    BackendSendResult,
    EnqueueLoadStep,
    EnqueueStoreStep,
    IssueCyclePlan,
    IssueOp,
    LoadTxn,
    NonMemBlockerStep,
    QueuePtr,
    StoreCommitReadyStep,
    StoreCommitStep,
    StoreRef,
    StoreTxn,
)


class BackendFacade:
    """Coordinate backend-facing active agents behind one semantic API."""

    def __init__(self, env) -> None:
        self.env = env
        self.lsq = env.lsq_agent
        self.issue = env.issue_agent
        self.rob = env.rob_agent
        self.commit = env.commit_agent

    def wait_load_enq_ready(self, max_cycles: int = 200) -> None:
        self.lsq.wait_load_enq_ready(max_cycles=max_cycles)

    def wait_store_enq_ready(self, max_cycles: int = 200) -> None:
        self.lsq.wait_store_enq_ready(max_cycles=max_cycles)

    def enqueue_load(self, req_id: int, lq_ptr, sq_ptr, enq_port: int = 0) -> None:
        self.lsq.enqueue_scalar_load(req_id, lq_ptr, sq_ptr, enq_port=enq_port)

    def enqueue_scalar_load(self, req_id: int, lq_ptr, sq_ptr, enq_port: int = 0) -> None:
        self.enqueue_load(req_id, lq_ptr, sq_ptr, enq_port=enq_port)

    def enqueue_store(self, req_id: int, sq_ptr, enq_port: int = 0):
        return self.lsq.enqueue_scalar_store(req_id, sq_ptr, enq_port=enq_port)

    def enqueue_scalar_store(self, req_id: int, sq_ptr, enq_port: int = 0):
        return self.enqueue_store(req_id, sq_ptr, enq_port=enq_port)

    def _resolve_issue_cycle(self, plan: IssueCyclePlan, result: BackendSendResult) -> IssueCyclePlan:
        resolved_ops = []
        for op in plan.ops:
            resolved_sq_ptr = result.resolve_sq_ptr(op.sq_ptr)
            resolved_ops.append(replace(op, sq_ptr=resolved_sq_ptr))
        return IssueCyclePlan(ops=tuple(resolved_ops), max_cycles=plan.max_cycles)

    def execute(self, plan: BackendSendPlan) -> BackendSendResult:
        store_ptrs: dict[StoreRef, QueuePtr] = {}
        result = BackendSendResult(store_ptrs=store_ptrs)
        for step in plan.steps:
            if isinstance(step, EnqueueLoadStep):
                self.enqueue_load(
                    req_id=step.req_id,
                    lq_ptr=step.lq_ptr,
                    sq_ptr=step.sq_ptr,
                    enq_port=step.enq_port,
                )
            elif isinstance(step, EnqueueStoreStep):
                allocated_sq_ptr = self.enqueue_store(
                    req_id=step.req_id,
                    sq_ptr=step.sq_ptr,
                    enq_port=step.enq_port,
                )
                if step.ref is not None:
                    store_ptrs[step.ref] = allocated_sq_ptr
            elif isinstance(step, IssueCyclePlan):
                resolved_cycle = self._resolve_issue_cycle(step, result)
                self.issue.issue_cycle(resolved_cycle)
                self._note_issue_cycle_store_progress(resolved_cycle)
            elif isinstance(step, StoreCommitStep):
                self.step_commit(count=step.count, cycles=step.cycles)
            elif isinstance(step, NonMemBlockerStep):
                if step.action == "insert":
                    self.insert_non_mem_blocker(
                        rob_idx_flag=step.rob_idx_flag,
                        rob_idx_value=step.rob_idx_value,
                    )
                else:
                    self.release_non_mem_blocker(
                        rob_idx_flag=step.rob_idx_flag,
                        rob_idx_value=step.rob_idx_value,
                    )
            elif isinstance(step, StoreCommitReadyStep):
                resolved_sq_ptr = result.resolve_sq_ptr(step.sq_ptr)
                self.mark_store_commit_ready(
                    sq_idx_flag=resolved_sq_ptr.flag,
                    sq_idx_value=resolved_sq_ptr.value,
                    ready=step.ready,
                )
            else:
                raise TypeError(f"unsupported backend send step: {type(step)!r}")
        return result

    def send(self, request):
        if isinstance(request, BackendSendPlan):
            return self.execute(request)
        if isinstance(request, LoadTxn):
            self.execute(
                BackendSendPlan.from_steps(
                    EnqueueLoadStep.from_txn(request),
                    IssueCyclePlan.from_ops(IssueOp.load_from_txn(request)),
                )
            )
            return None
        if isinstance(request, StoreTxn):
            store_ref = StoreRef(name=f"store_{request.req_id}")
            result = self.execute(
                BackendSendPlan.from_steps(
                    EnqueueStoreStep.from_txn(request, ref=store_ref),
                    IssueCyclePlan.from_ops(
                        IssueOp.std(
                            req_id=request.req_id,
                            sq_ptr=store_ref,
                            data=request.data,
                            lane=request.std_lane,
                            mask=request.mask,
                        )
                    ),
                    IssueCyclePlan.from_ops(
                        IssueOp.sta(
                            req_id=request.req_id,
                            sq_ptr=store_ref,
                            addr=request.addr,
                            lane=request.sta_lane,
                            mask=request.mask,
                        )
                    ),
                )
            )
            allocated_sq_ptr = result.resolve_sq_ptr(store_ref)
            if hasattr(self.env.memory, "note_store_request"):
                self.env.memory.note_store_request(
                    sq_idx=allocated_sq_ptr.value,
                    addr=request.addr,
                    data=request.data,
                    mask=request.mask,
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
        store_set_hit: int = 0,
        load_wait_bit: int = 0,
        load_wait_strict: int = 0,
        wait_for_rob_idx_flag: int | None = None,
        wait_for_rob_idx_value: int | None = None,
    ) -> None:
        self.issue_scalar_load(
            req_id=req_id,
            addr=addr,
            lq_ptr=lq_ptr,
            sq_ptr=sq_ptr,
            lane=lane,
            store_set_hit=store_set_hit,
            load_wait_bit=load_wait_bit,
            load_wait_strict=load_wait_strict,
            wait_for_rob_idx_flag=wait_for_rob_idx_flag,
            wait_for_rob_idx_value=wait_for_rob_idx_value,
        )

    def issue_scalar_load(
        self,
        req_id: int,
        addr: int,
        lq_ptr,
        sq_ptr,
        lane: int = 0,
        store_set_hit: int = 0,
        load_wait_bit: int = 0,
        load_wait_strict: int = 0,
        wait_for_rob_idx_flag: int | None = None,
        wait_for_rob_idx_value: int | None = None,
    ) -> None:
        self.issue.issue_cycle(
            IssueCyclePlan.from_ops(
                IssueOp(
                    kind="load",
                    req_id=req_id,
                    lane=lane,
                    sq_ptr=sq_ptr,
                    addr=addr,
                    lq_ptr=lq_ptr,
                    store_set_hit=store_set_hit,
                    load_wait_bit=load_wait_bit,
                    load_wait_strict=load_wait_strict,
                    wait_for_rob_idx_flag=wait_for_rob_idx_flag,
                    wait_for_rob_idx_value=wait_for_rob_idx_value,
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
        self.issue.issue_cycle(
            IssueCyclePlan(
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
        )

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

    def issue_std(self, req_id: int, sq_ptr, data: int, lane: int = 5, mask: int = 0xFF) -> None:
        self.issue.issue_cycle(
            IssueCyclePlan.from_ops(
                IssueOp.std(req_id=req_id, sq_ptr=sq_ptr, data=data, lane=lane, mask=mask)
            )
        )

    def issue_scalar_std(self, req_id: int, sq_ptr, data: int, lane: int = 5, mask: int = 0xFF) -> None:
        self.issue_std(req_id, sq_ptr, data, lane=lane, mask=mask)

    def issue_sta(self, req_id: int, sq_ptr, addr: int, lane: int = 3, mask: int = 0xFF) -> None:
        self.issue.issue_cycle(
            IssueCyclePlan.from_ops(
                IssueOp.sta(req_id=req_id, sq_ptr=sq_ptr, addr=addr, lane=lane, mask=mask)
            )
        )

    def issue_scalar_sta(self, req_id: int, sq_ptr, addr: int, lane: int = 3, mask: int = 0xFF) -> None:
        self.issue_sta(req_id, sq_ptr, addr, lane=lane, mask=mask)

    def send_load(self, txn: LoadTxn) -> None:
        self.send(txn)

    def send_load_batch_same_cycle(self, txns, max_cycles: int = 50) -> None:
        txns = tuple(txns)
        self.execute(
            BackendSendPlan.from_steps(
                *(EnqueueLoadStep.from_txn(txn) for txn in txns),
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
                *(EnqueueLoadStep.from_txn(txn) for txn in txns),
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

    def note_load_issued(self, rob_idx_flag: int, rob_idx_value: int) -> None:
        self.commit.note_load_issued(rob_idx_flag, rob_idx_value)
        self.env.memory.note_load_issued(rob_idx_flag, rob_idx_value)

    def note_store_allocated(
        self,
        sq_idx_flag: int,
        sq_idx_value: int,
        rob_idx_flag: int,
        rob_idx_value: int,
    ) -> None:
        self.commit.note_store_allocated(
            rob_idx_flag,
            rob_idx_value,
            sq_idx_flag=sq_idx_flag,
            sq_idx_value=sq_idx_value,
        )
        self.env.memory.note_store_allocated(
            sq_idx_flag=sq_idx_flag,
            sq_idx_value=sq_idx_value,
            rob_idx_flag=rob_idx_flag,
            rob_idx_value=rob_idx_value,
        )

    def note_load_completed(self, rob_idx_flag: int, rob_idx_value: int) -> None:
        self.commit.note_load_completed(rob_idx_flag, rob_idx_value)

    def insert_non_mem_blocker(self, rob_idx_flag: int, rob_idx_value: int) -> None:
        self.commit.note_non_mem_issued(rob_idx_flag, rob_idx_value)

    def release_non_mem_blocker(self, rob_idx_flag: int, rob_idx_value: int) -> None:
        self.commit.release_non_mem(rob_idx_flag, rob_idx_value)

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
