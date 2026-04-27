# coding=utf-8
"""
Unified backend-facing facade for MemBlock env.
"""

from dataclasses import replace

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
        self.reset_runtime_state()

    def reset_runtime_state(self) -> None:
        self._next_rob_idx = RobIndex(flag=0, value=0)

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
        self.issue.issue_cycle(
            IssueCyclePlan.from_ops(
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
        )

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
        self.issue.issue_cycle(
            IssueCyclePlan.from_ops(
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
        )

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

    def send_cbo_zero(self, txn: StoreTxn):
        if not txn.is_cbo_zero:
            raise ValueError("send_cbo_zero requires a StoreTxn with opcode='cbo_zero'")
        return self.send(txn)

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
