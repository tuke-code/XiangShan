# coding=utf-8
"""
request_apis should route active control through env.backend.
"""

import pytest

from agents.backend_facade import BackendFacade
from transactions import (
    BackendSendPlan,
    CBO_ZERO_STORE_OPCODE,
    EnqueueLoadCyclePlan,
    EnqueueLoadStep,
    EnqueueStoreStep,
    IssueCyclePlan,
    IssueOp,
    LSU_OP_CBO_ZERO,
    LoadTxn,
    NonMemBlockerStep,
    QueuePtr,
    RobIndex,
    RobRef,
    StoreCommitReadyStep,
    StoreRef,
    StoreTxn,
    VectorEnqueueStep,
    VectorIssueStep,
    VectorMemResult,
    VectorMemTxn,
    VectorWaitStep,
    scalar_store_fu_op_type_from_mask,
)

from request_apis import (
    enqueue_scalar_load,
    enqueue_scalar_store,
    issue_scalar_load,
    issue_scalar_sta,
    issue_scalar_std,
    send_cbo_zero,
    send_load,
    send_store,
    send_vector_load,
    send_vector_store,
    wait_lsq_load_enq_ready,
    wait_lsq_store_enq_ready,
)


class _FakeBackend:
    def __init__(self) -> None:
        self.calls = []

    def wait_load_enq_ready(self, max_cycles: int = 200) -> None:
        self.calls.append(("wait_load_enq_ready", max_cycles))

    def wait_store_enq_ready(self, max_cycles: int = 200) -> None:
        self.calls.append(("wait_store_enq_ready", max_cycles))

    def enqueue_scalar_load(
        self,
        req_id,
        lq_ptr,
        sq_ptr,
        enq_port: int = 0,
        rob_idx_flag: int | None = None,
        rob_idx_value: int | None = None,
    ) -> None:
        self.calls.append(("enqueue_scalar_load", req_id, lq_ptr, sq_ptr, enq_port, rob_idx_flag, rob_idx_value))

    def enqueue_scalar_store(
        self,
        req_id,
        sq_ptr,
        enq_port: int = 0,
        rob_idx_flag: int | None = None,
        rob_idx_value: int | None = None,
    ):
        self.calls.append(("enqueue_scalar_store", req_id, sq_ptr, enq_port, rob_idx_flag, rob_idx_value))
        return QueuePtr(flag=sq_ptr.flag ^ 1, value=sq_ptr.value + 1)

    def issue_scalar_load(self, req_id, addr, lq_ptr, sq_ptr, **kwargs) -> None:
        self.calls.append(("issue_scalar_load", req_id, addr, lq_ptr, sq_ptr, kwargs))

    def issue_scalar_std(self, req_id, sq_ptr, data, lane: int = 5, mask: int = 0xFF, **kwargs) -> None:
        self.calls.append(("issue_scalar_std", req_id, sq_ptr, data, lane, mask, kwargs))

    def issue_scalar_sta(self, req_id, sq_ptr, addr, lane: int = 3, mask: int = 0xFF, **kwargs) -> None:
        self.calls.append(("issue_scalar_sta", req_id, sq_ptr, addr, lane, mask, kwargs))

    def send_load(self, txn) -> None:
        self.calls.append(("send_load", txn))

    def send_store(self, txn):
        self.calls.append(("send_store", txn))
        return QueuePtr(flag=1, value=9)

    def send(self, txn):
        self.calls.append(("send", txn))
        return QueuePtr(flag=1, value=11) if isinstance(txn, StoreTxn) else None

    def execute(self, plan):
        self.calls.append(("execute", plan))


class _FakeVectorBackend:
    def __init__(self) -> None:
        self.calls = []

    def send(self, txn):
        self.calls.append(("send", txn))
        return "vector-result"


class _FakeLsqAgent:
    def __init__(self) -> None:
        self.calls = []

    def wait_load_enq_ready(self, max_cycles: int = 200) -> None:
        self.calls.append(("wait_load_enq_ready", max_cycles))

    def wait_store_enq_ready(self, max_cycles: int = 200) -> None:
        self.calls.append(("wait_store_enq_ready", max_cycles))

    def enqueue_scalar_load(self, req_id, lq_ptr, sq_ptr, enq_port: int = 0) -> None:
        self.calls.append(("enqueue_scalar_load", req_id, lq_ptr, sq_ptr, enq_port))

    def enqueue_load_cycle(self, plan) -> None:
        self.calls.append(("enqueue_load_cycle", plan))

    def enqueue_scalar_store(
        self,
        req_id,
        sq_ptr,
        enq_port: int = 0,
        rob_idx_flag: int | None = None,
        rob_idx_value: int | None = None,
    ):
        allocated = QueuePtr(flag=sq_ptr.flag ^ 1, value=sq_ptr.value + 1)
        self.calls.append(
            ("enqueue_scalar_store", req_id, sq_ptr, enq_port, allocated, rob_idx_flag, rob_idx_value)
        )
        return allocated

    def enqueue_vector_mem(self, txn):
        self.calls.append(("enqueue_vector_mem", txn))
        if txn.is_load:
            return None
        return QueuePtr(flag=txn.sq_ptr.flag ^ 1, value=txn.sq_ptr.value + 1)


class _FakeIssueAgent:
    def __init__(self) -> None:
        self.calls = []

    def issue_cycle(self, plan) -> None:
        self.calls.append(("issue_cycle", plan))


class _FakeVectorIssueAgent:
    def __init__(self) -> None:
        self.calls = []

    def issue(self, txn, max_cycles: int = 50) -> None:
        self.calls.append(("issue", txn, max_cycles))


class _FakeCommitAgent:
    def __init__(self) -> None:
        self.calls = []

    def note_load_issued(self, rob_idx_flag: int, rob_idx_value: int) -> None:
        self.calls.append(("note_load_issued", rob_idx_flag, rob_idx_value))

    def note_store_allocated(
        self,
        rob_idx_flag: int,
        rob_idx_value: int,
        *,
        sq_idx_flag: int | None = None,
        sq_idx_value: int | None = None,
    ) -> None:
        self.calls.append(("note_store_allocated", rob_idx_flag, rob_idx_value, sq_idx_flag, sq_idx_value))

    def note_load_completed(self, rob_idx_flag: int, rob_idx_value: int) -> None:
        self.calls.append(("note_load_completed", rob_idx_flag, rob_idx_value))

    def note_non_mem_issued(self, rob_idx_flag: int, rob_idx_value: int) -> None:
        self.calls.append(("note_non_mem_issued", rob_idx_flag, rob_idx_value))

    def release_non_mem(self, rob_idx_flag: int, rob_idx_value: int) -> None:
        self.calls.append(("release_non_mem", rob_idx_flag, rob_idx_value))

    def mark_store_addr_ready(self, sq_idx_flag: int, sq_idx_value: int) -> None:
        self.calls.append(("mark_store_addr_ready", sq_idx_flag, sq_idx_value))

    def mark_store_data_ready(self, sq_idx_flag: int, sq_idx_value: int) -> None:
        self.calls.append(("mark_store_data_ready", sq_idx_flag, sq_idx_value))

    def mark_store_commit_ready(self, sq_idx_flag: int, sq_idx_value: int, ready: bool = True) -> None:
        self.calls.append(("mark_store_commit_ready", sq_idx_flag, sq_idx_value, ready))

    def queue_store_commit(self, count: int = 1) -> None:
        self.calls.append(("queue_store_commit", count))


class _FakeRobAgent:
    def __init__(self) -> None:
        self.calls = []
        self._entries = []

    def set_pending_ptr(self, ptr) -> None:
        self.calls.append(("set_pending_ptr", ptr.flag, ptr.value))


class _FakeMemory:
    def __init__(self) -> None:
        self.calls = []
        self.vector = object()

    def note_load_issued(self, rob_idx_flag: int, rob_idx_value: int) -> None:
        self.calls.append(("note_load_issued", rob_idx_flag, rob_idx_value))

    def note_store_allocated(
        self,
        *,
        sq_idx_flag: int,
        sq_idx_value: int,
        rob_idx_flag: int,
        rob_idx_value: int,
    ) -> None:
        self.calls.append(("note_store_allocated", sq_idx_flag, sq_idx_value, rob_idx_flag, rob_idx_value))


class _FakeFacadeEnv:
    def __init__(self) -> None:
        self.lsq_agent = _FakeLsqAgent()
        self.issue_agent = _FakeIssueAgent()
        self.vector_issue_agent = _FakeVectorIssueAgent()
        self.rob_agent = _FakeRobAgent()
        self.commit_agent = _FakeCommitAgent()
        self.memory = _FakeMemory()
        self.vector_monitor = _FakeVectorMonitor()
        self.config = type("Config", (), {"rob_size": 512})()
        self.run_async_calls = []

    def _run_async(self, marker):
        self.run_async_calls.append(marker)
        return marker

    async def _await_cycles(self, cycles: int):
        return cycles

    def _flush_store_buffers_and_wait_impl(self, max_cycles: int = 200, settle_cycles: int = 4) -> dict:
        return {"max_cycles": max_cycles, "settle_cycles": settle_cycles}


class _FakeEnv:
    def __init__(self) -> None:
        self.backend = _FakeBackend()
        self.vector_backend = _FakeVectorBackend()


class _FakeVectorMonitor:
    def __init__(self) -> None:
        self.registrations = []

    def register_req(self, req_id: int, rob_idx_flag: int, rob_idx_value: int) -> None:
        self.registrations.append((req_id, rob_idx_flag, rob_idx_value))

    def wait_event_async(self, req_id: int, *, event: str = "complete_or_trap", max_cycles: int = 200):
        del max_cycles
        return VectorMemResult(
            req_id=req_id,
            completed=event != "trap",
            trapped=event == "trap",
        )


def test_api_request_apis_enqueue_and_issue_delegate_to_backend():
    env = _FakeEnv()
    lq_ptr = QueuePtr(flag=0, value=2)
    sq_ptr = QueuePtr(flag=1, value=3)

    wait_lsq_load_enq_ready(env, max_cycles=19)
    wait_lsq_store_enq_ready(env, max_cycles=21)
    enqueue_scalar_load(env, req_id=5, lq_ptr=lq_ptr, sq_ptr=sq_ptr, enq_port=1)
    returned_sq_ptr = enqueue_scalar_store(env, req_id=6, sq_ptr=sq_ptr, enq_port=2)
    issue_scalar_load(env, req_id=7, addr=0x1000, lq_ptr=lq_ptr, sq_ptr=sq_ptr, lane=4)
    issue_scalar_std(env, req_id=8, sq_ptr=sq_ptr, data=0x55, lane=6, mask=0x0F)
    issue_scalar_sta(env, req_id=9, sq_ptr=sq_ptr, addr=0x2000, lane=3, mask=0x03)

    assert returned_sq_ptr == QueuePtr(flag=0, value=4)
    assert env.backend.calls[0] == ("wait_load_enq_ready", 19)
    assert env.backend.calls[1] == ("wait_store_enq_ready", 21)
    assert env.backend.calls[2][0] == "enqueue_scalar_load"
    assert env.backend.calls[3][0] == "enqueue_scalar_store"
    assert env.backend.calls[4][0] == "issue_scalar_load"
    assert env.backend.calls[5] == (
        "issue_scalar_std",
        8,
        sq_ptr,
        0x55,
        6,
        0x0F,
        {
            "rob_idx_flag": None,
            "rob_idx_value": None,
            "ftq_idx_flag": None,
            "ftq_idx_value": None,
        },
    )
    assert env.backend.calls[6] == (
        "issue_scalar_sta",
        9,
        sq_ptr,
        0x2000,
        3,
        0x03,
        {
            "rob_idx_flag": None,
            "rob_idx_value": None,
            "ftq_idx_flag": None,
            "ftq_idx_value": None,
        },
    )


def test_api_request_apis_send_txns_delegate_to_backend():
    env = _FakeEnv()
    load_txn = LoadTxn(
        req_id=0x21,
        addr=0x3000,
        lq_ptr=QueuePtr(flag=0, value=1),
        sq_ptr=QueuePtr(flag=0, value=2),
    )
    store_txn = StoreTxn(
        req_id=0x22,
        sq_ptr=QueuePtr(flag=1, value=4),
        addr=0x4000,
        data=0xAABBCCDD,
    )
    cbo_txn = StoreTxn.cbo_zero(
        req_id=0x23,
        sq_ptr=QueuePtr(flag=0, value=7),
        addr=0x4040,
    )

    send_load(env, load_txn)
    result = send_store(env, store_txn)
    cbo_result = send_cbo_zero(env, cbo_txn)

    assert env.backend.calls[0] == ("send", load_txn)
    assert env.backend.calls[1] == ("send", store_txn)
    assert env.backend.calls[2] == ("send", cbo_txn)
    assert result == QueuePtr(flag=1, value=11)
    assert cbo_result == QueuePtr(flag=1, value=11)


def test_api_request_apis_issue_scalar_load_forwards_size_and_fp_wen():
    env = _FakeEnv()
    lq_ptr = QueuePtr(flag=0, value=2)
    sq_ptr = QueuePtr(flag=1, value=3)

    issue_scalar_load(
        env,
        req_id=7,
        addr=0x1000,
        lq_ptr=lq_ptr,
        sq_ptr=sq_ptr,
        lane=4,
        size=4,
        fp_wen=1,
    )

    assert env.backend.calls == [
        (
            "issue_scalar_load",
            7,
            0x1000,
            lq_ptr,
            sq_ptr,
            {
                "lane": 4,
                "size": 4,
                "fp_wen": 1,
                "store_set_hit": 0,
                "load_wait_bit": 0,
                "load_wait_strict": 0,
                "wait_for_rob_idx_flag": None,
                "wait_for_rob_idx_value": None,
                "rob_idx_flag": None,
                "rob_idx_value": None,
                "pdest": None,
                "ftq_idx_flag": None,
                "ftq_idx_value": None,
                "pc": None,
            },
        )
    ]


def test_api_request_apis_send_vector_txns_delegate_to_vector_backend():
    env = _FakeEnv()
    load_txn = VectorMemTxn(
        req_id=0x41,
        is_load=True,
        opcode_class="unit_stride",
        base_addr=0x3000,
        lq_ptr=QueuePtr(flag=0, value=1),
        sq_ptr=QueuePtr(flag=0, value=2),
        vl=2,
        element_count=2,
    )
    store_txn = VectorMemTxn(
        req_id=0x42,
        is_load=False,
        opcode_class="stride",
        base_addr=0x4000,
        stride=16,
        lq_ptr=QueuePtr(flag=0, value=1),
        sq_ptr=QueuePtr(flag=0, value=2),
        vl=2,
        element_count=2,
        store_data=(1, 2),
    )

    assert send_vector_load(env, load_txn) == "vector-result"
    assert send_vector_store(env, store_txn) == "vector-result"
    assert env.vector_backend.calls == [("send", load_txn), ("send", store_txn)]


def test_api_request_apis_batch_wrappers_delegate_to_backend_execute():
    env = _FakeEnv()
    txns = (
        LoadTxn(req_id=1, addr=0x1000, lq_ptr=QueuePtr(0, 1), sq_ptr=QueuePtr(0, 2), issue_lane=0),
        LoadTxn(req_id=2, addr=0x2000, lq_ptr=QueuePtr(0, 3), sq_ptr=QueuePtr(0, 2), issue_lane=1),
    )

    from request_apis import send_load_batch_same_cycle, send_load_batch_with_sta_same_cycle

    send_load_batch_same_cycle(env, txns, max_cycles=13)
    send_load_batch_with_sta_same_cycle(
        env,
        txns,
        sta_req_id=9,
        sta_sq_ptr=QueuePtr(1, 5),
        sta_addr=0x3000,
        sta_lane=3,
        sta_mask=0x0F,
        max_cycles=17,
    )

    assert env.backend.calls[0][0] == "execute"
    assert isinstance(env.backend.calls[0][1], BackendSendPlan)
    assert env.backend.calls[1][0] == "execute"
    assert isinstance(env.backend.calls[1][1], BackendSendPlan)
    assert isinstance(env.backend.calls[0][1].steps[0], EnqueueLoadCyclePlan)
    assert isinstance(env.backend.calls[1][1].steps[0], EnqueueLoadCyclePlan)
    assert [op.kind for op in env.backend.calls[0][1].steps[-1].ops] == ["load", "load"]
    assert [op.kind for op in env.backend.calls[1][1].steps[-1].ops] == ["load", "load", "sta"]
    assert env.backend.calls[0][1].steps[-1].handshake_mode == "strict"
    assert env.backend.calls[1][1].steps[-1].handshake_mode == "strict"
    assert env.backend.calls[1][1].steps[-1].ops[-1].mask == 0x0F
    assert [step.enq_port for step in env.backend.calls[0][1].steps[0].steps] == [0, 1]


def test_api_backend_facade_send_store_translates_to_enqueue_and_issue_cycles():
    env = _FakeFacadeEnv()
    backend = BackendFacade(env)
    txn = StoreTxn(
        req_id=0x22,
        sq_ptr=QueuePtr(flag=1, value=4),
        addr=0x4000,
        data=0xAABBCCDD,
    )

    allocated = backend.send(txn)

    assert allocated == QueuePtr(flag=0, value=5)
    assert env.lsq_agent.calls[0][:4] == ("enqueue_scalar_store", 0x22, QueuePtr(flag=1, value=4), 0)
    assert len(env.issue_agent.calls) == 2
    first_cycle = env.issue_agent.calls[0][1]
    second_cycle = env.issue_agent.calls[1][1]
    assert first_cycle.ops[0].kind == "std"
    assert first_cycle.ops[0].sq_ptr == QueuePtr(flag=0, value=5)
    assert first_cycle.ops[0].mask == 0xFF
    assert second_cycle.ops[0].kind == "sta"
    assert second_cycle.ops[0].sq_ptr == QueuePtr(flag=0, value=5)
    assert second_cycle.ops[0].mask == 0xFF
    assert ("mark_store_data_ready", 0, 5) in env.commit_agent.calls
    assert ("mark_store_addr_ready", 0, 5) in env.commit_agent.calls


def test_api_backend_facade_send_partial_store_keeps_mask_on_sta_and_std():
    env = _FakeFacadeEnv()
    backend = BackendFacade(env)
    txn = StoreTxn(
        req_id=0x23,
        sq_ptr=QueuePtr(flag=0, value=7),
        addr=0x5000,
        data=0x11223344,
        mask=0x0F,
    )

    allocated = backend.send(txn)

    assert allocated == QueuePtr(flag=1, value=8)
    first_cycle = env.issue_agent.calls[0][1]
    second_cycle = env.issue_agent.calls[1][1]
    assert first_cycle.ops[0].mask == 0x0F
    assert second_cycle.ops[0].mask == 0x0F


def test_api_backend_facade_send_cbo_zero_marks_issue_ops_with_cbo_opcode():
    env = _FakeFacadeEnv()
    backend = BackendFacade(env)
    txn = StoreTxn.cbo_zero(
        req_id=0x26,
        sq_ptr=QueuePtr(flag=0, value=9),
        addr=0x8040,
    )

    allocated = backend.send_cbo_zero(txn)

    assert allocated == QueuePtr(flag=1, value=10)
    first_cycle = env.issue_agent.calls[0][1]
    second_cycle = env.issue_agent.calls[1][1]
    assert first_cycle.ops[0].store_opcode == CBO_ZERO_STORE_OPCODE
    assert second_cycle.ops[0].store_opcode == CBO_ZERO_STORE_OPCODE
    assert first_cycle.ops[0].store_fu_op_type == LSU_OP_CBO_ZERO
    assert second_cycle.ops[0].store_fu_op_type == LSU_OP_CBO_ZERO


def test_api_scalar_store_mask_decodes_to_size_and_fu_op():
    txn = StoreTxn(
        req_id=0x24,
        sq_ptr=QueuePtr(flag=0, value=1),
        addr=0x6000,
        data=0xAA,
        mask=0x03,
    )

    assert txn.size_bytes == 2
    assert txn.fu_op_type == scalar_store_fu_op_type_from_mask(0x03)


def test_api_cbo_zero_store_txn_uses_cacheline_size_and_cbo_fu_op():
    txn = StoreTxn.cbo_zero(
        req_id=0x25,
        sq_ptr=QueuePtr(flag=0, value=1),
        addr=0x6040,
    )

    assert txn.size_bytes == 64
    assert txn.fu_op_type == LSU_OP_CBO_ZERO
    assert txn.issue_data == 0
    assert txn.is_cbo_zero


def test_api_issue_op_rejects_non_scalar_store_mask():
    with pytest.raises(ValueError):
        IssueOp.std(req_id=1, sq_ptr=QueuePtr(0, 1), data=0x11, lane=5, mask=0x05)


def test_api_issue_op_cbo_zero_uses_explicit_store_opcode():
    op = IssueOp.sta(
        req_id=1,
        sq_ptr=QueuePtr(0, 1),
        addr=0x8000,
        lane=3,
        store_opcode=CBO_ZERO_STORE_OPCODE,
    )

    assert op.store_fu_op_type == LSU_OP_CBO_ZERO


def test_api_backend_facade_execute_resolves_store_ref_in_issue_cycle():
    env = _FakeFacadeEnv()
    backend = BackendFacade(env)
    store_ref = StoreRef("older_store")
    plan = BackendSendPlan.from_steps(
        EnqueueStoreStep(req_id=7, sq_ptr=QueuePtr(flag=0, value=2), ref=store_ref),
        IssueCyclePlan.from_ops(
            IssueOp.std(req_id=7, sq_ptr=store_ref, data=0x55, lane=5),
            IssueOp.sta(req_id=7, sq_ptr=store_ref, addr=0x8000, lane=3),
        ),
    )

    result = backend.execute(plan)

    assert result.resolve_sq_ptr(store_ref) == QueuePtr(flag=1, value=3)
    issued_plan = env.issue_agent.calls[0][1]
    assert all(op.sq_ptr == QueuePtr(flag=1, value=3) for op in issued_plan.ops)
    assert ("mark_store_data_ready", 1, 3) in env.commit_agent.calls
    assert ("mark_store_addr_ready", 1, 3) in env.commit_agent.calls


def test_api_backend_facade_execute_routes_same_cycle_load_enqueue_plan():
    env = _FakeFacadeEnv()
    backend = BackendFacade(env)
    plan = BackendSendPlan.from_steps(
        EnqueueLoadCyclePlan.from_steps(
            EnqueueLoadStep(req_id=1, lq_ptr=QueuePtr(flag=0, value=1), sq_ptr=QueuePtr(flag=0, value=2), enq_port=0),
            EnqueueLoadStep(req_id=2, lq_ptr=QueuePtr(flag=0, value=2), sq_ptr=QueuePtr(flag=0, value=2), enq_port=1),
        ),
        IssueCyclePlan.from_ops(
            IssueOp.load_from_txn(LoadTxn(req_id=1, addr=0x1000, lq_ptr=QueuePtr(0, 1), sq_ptr=QueuePtr(0, 2), issue_lane=0)),
            IssueOp.load_from_txn(LoadTxn(req_id=2, addr=0x2000, lq_ptr=QueuePtr(0, 2), sq_ptr=QueuePtr(0, 2), issue_lane=1)),
        ),
    )

    backend.execute(plan)

    assert env.lsq_agent.calls[0][0] == "enqueue_load_cycle"
    assert [step.enq_port for step in env.lsq_agent.calls[0][1].steps] == [0, 1]
    assert env.issue_agent.calls[0][0] == "issue_cycle"


def test_api_backend_facade_execute_preserves_elastic_issue_cycle_mode():
    env = _FakeFacadeEnv()
    backend = BackendFacade(env)
    plan = BackendSendPlan.from_steps(
        EnqueueLoadCyclePlan.from_steps(
            EnqueueLoadStep(req_id=1, lq_ptr=QueuePtr(flag=0, value=1), sq_ptr=QueuePtr(flag=0, value=2), enq_port=0),
            EnqueueLoadStep(req_id=2, lq_ptr=QueuePtr(flag=0, value=2), sq_ptr=QueuePtr(flag=0, value=2), enq_port=1),
        ),
        IssueCyclePlan.from_ops(
            IssueOp.load_from_txn(LoadTxn(req_id=1, addr=0x1000, lq_ptr=QueuePtr(0, 1), sq_ptr=QueuePtr(0, 2), issue_lane=0)),
            IssueOp.load_from_txn(LoadTxn(req_id=2, addr=0x2000, lq_ptr=QueuePtr(0, 2), sq_ptr=QueuePtr(0, 2), issue_lane=1)),
            handshake_mode="elastic",
        ),
    )

    backend.execute(plan)

    assert env.issue_agent.calls[0][1].handshake_mode == "elastic"


def test_api_enqueue_load_cycle_plan_rejects_duplicate_ports():
    with pytest.raises(ValueError):
        EnqueueLoadCyclePlan.from_steps(
            EnqueueLoadStep(req_id=1, lq_ptr=QueuePtr(0, 1), sq_ptr=QueuePtr(0, 2), enq_port=0),
            EnqueueLoadStep(req_id=2, lq_ptr=QueuePtr(0, 2), sq_ptr=QueuePtr(0, 2), enq_port=0),
        )


def test_api_backend_facade_execute_routes_non_mem_blocker_steps():
    env = _FakeFacadeEnv()
    backend = BackendFacade(env)

    backend.execute(
        BackendSendPlan.from_steps(
            NonMemBlockerStep.insert(rob_idx=RobIndex(flag=0, value=9)),
            NonMemBlockerStep.release(rob_idx=RobIndex(flag=0, value=9)),
        )
    )

    assert env.commit_agent.calls == [
        ("note_non_mem_issued", 0, 9),
        ("release_non_mem", 0, 9),
    ]


def test_api_backend_facade_prepare_binds_load_txn_before_send():
    env = _FakeFacadeEnv()
    backend = BackendFacade(env)
    txn = LoadTxn(
        req_id=0x51,
        addr=0x1234,
        lq_ptr=QueuePtr(flag=0, value=1),
        sq_ptr=QueuePtr(flag=0, value=2),
    )

    prepared = backend.prepare(txn)

    assert txn.rob_idx == RobIndex(flag=0, value=0)
    assert txn.resolved_pdest == 0
    assert prepared.rob_idx_of(txn) == prepared.rob_idx_of(txn.req_id)


def test_api_issue_op_load_from_txn_preserves_fp_wen_and_size():
    txn = LoadTxn(
        req_id=0x61,
        addr=0x1234,
        lq_ptr=QueuePtr(flag=0, value=1),
        sq_ptr=QueuePtr(flag=0, value=2),
        size=4,
        mask=0x0F,
        fp_wen=1,
    )

    op = IssueOp.load_from_txn(txn)

    assert op.size == 4
    assert op.mask == 0x0F
    assert op.fp_wen == 1
    assert op.load_fu_op_type == 0x2


def test_api_backend_facade_issue_scalar_load_preserves_fp_contract():
    env = _FakeFacadeEnv()
    backend = BackendFacade(env)

    backend.issue_scalar_load(
        req_id=0x62,
        addr=0x5678,
        lq_ptr=QueuePtr(flag=0, value=3),
        sq_ptr=QueuePtr(flag=0, value=4),
        lane=2,
        size=2,
        fp_wen=1,
        rob_idx=RobIndex(flag=0, value=9),
        pdest=11,
        ftq_idx_flag=0,
        ftq_idx_value=5,
        pc=0x80000010,
    )

    issued_plan = env.issue_agent.calls[0][1]
    op = issued_plan.ops[0]
    assert op.kind == "load"
    assert op.size == 2
    assert op.fp_wen == 1
    assert op.load_fu_op_type == 0x1
    assert op.resolved_rob_idx == RobIndex(flag=0, value=9)


def test_api_transactions_require_runtime_binding_before_access():
    load_txn = LoadTxn(
        req_id=0x31,
        addr=0x1234,
        lq_ptr=QueuePtr(flag=0, value=1),
        sq_ptr=QueuePtr(flag=0, value=2),
    )
    store_txn = StoreTxn(
        req_id=0x32,
        sq_ptr=QueuePtr(flag=0, value=3),
        addr=0x5678,
        data=0xAA55,
    )
    vector_txn = VectorMemTxn(
        req_id=0x33,
        is_load=True,
        opcode_class="unit_stride",
        base_addr=0x8000,
        lq_ptr=QueuePtr(flag=0, value=4),
        sq_ptr=QueuePtr(flag=0, value=5),
        vl=2,
        element_count=2,
    )

    with pytest.raises(RuntimeError, match="LoadTxn\\.rob_idx requires explicit runtime binding"):
        _ = load_txn.rob_idx
    with pytest.raises(RuntimeError, match="LoadTxn\\.resolved_pdest requires explicit runtime binding"):
        _ = load_txn.resolved_pdest
    with pytest.raises(RuntimeError, match="StoreTxn\\.rob_idx requires explicit runtime binding"):
        _ = store_txn.rob_idx
    with pytest.raises(RuntimeError, match="StoreTxn\\.resolved_ftq_idx_value requires explicit runtime binding"):
        _ = store_txn.resolved_ftq_idx_value
    with pytest.raises(RuntimeError, match="VectorMemTxn\\.rob_idx requires explicit runtime binding"):
        _ = vector_txn.rob_idx
    with pytest.raises(RuntimeError, match="VectorMemTxn\\.resolved_pdest requires explicit runtime binding"):
        _ = vector_txn.resolved_pdest


def test_api_backend_facade_can_seed_allocator_wrap():
    env = _FakeFacadeEnv()
    backend = BackendFacade(env)
    backend.set_next_rob_idx(RobIndex(flag=0, value=511))
    first = LoadTxn(req_id=1, addr=0x1000, lq_ptr=QueuePtr(0, 1), sq_ptr=QueuePtr(0, 2))
    second = LoadTxn(req_id=2, addr=0x2000, lq_ptr=QueuePtr(0, 2), sq_ptr=QueuePtr(0, 2))

    backend.prepare(first)
    backend.prepare(second)

    assert first.rob_idx == RobIndex(flag=0, value=511)
    assert second.rob_idx == RobIndex(flag=1, value=0)
    assert first.resolved_pdest == 63
    assert second.resolved_pdest == 0


def test_api_backend_facade_can_seed_commit_frontier():
    env = _FakeFacadeEnv()
    backend = BackendFacade(env)

    backend.set_commit_frontier(RobIndex(flag=0, value=511))

    assert env.rob_agent.calls == [("set_pending_ptr", 0, 511)]


def test_api_backend_facade_prepare_resolves_wait_for_rob_ref():
    env = _FakeFacadeEnv()
    backend = BackendFacade(env)
    lead_ref = RobRef("lead")
    lead = LoadTxn(
        req_id=1,
        addr=0x1000,
        lq_ptr=QueuePtr(flag=0, value=1),
        sq_ptr=QueuePtr(flag=0, value=2),
        rob_ref=lead_ref,
    )
    follower = LoadTxn(
        req_id=2,
        addr=0x2000,
        lq_ptr=QueuePtr(flag=0, value=2),
        sq_ptr=QueuePtr(flag=0, value=2),
        issue_lane=1,
        wait_for_rob=lead_ref,
    )

    prepared = backend.prepare(
        BackendSendPlan.from_steps(
            EnqueueLoadCyclePlan.from_steps(
                EnqueueLoadStep.from_txn(lead),
                EnqueueLoadStep(
                    req_id=follower.req_id,
                    lq_ptr=follower.lq_ptr,
                    sq_ptr=follower.sq_ptr,
                    enq_port=1,
                    txn=follower,
                ),
            ),
            IssueCyclePlan.from_ops(IssueOp.load_from_txn(lead), IssueOp.load_from_txn(follower)),
        )
    )

    issue_cycle = prepared.resolved_plan.steps[1]
    assert issue_cycle.ops[1].wait_for_rob_idx == lead.rob_idx


def test_api_backend_facade_execute_resolves_store_ref_for_commit_ready_step():
    env = _FakeFacadeEnv()
    backend = BackendFacade(env)
    store_ref = StoreRef("ready_store")

    backend.execute(
        BackendSendPlan.from_steps(
            EnqueueStoreStep(req_id=7, sq_ptr=QueuePtr(flag=0, value=2), ref=store_ref),
            StoreCommitReadyStep(sq_ptr=store_ref, ready=True),
        )
    )

    assert ("mark_store_commit_ready", 1, 3, True) in env.commit_agent.calls


def test_api_backend_facade_vector_execute_uses_shared_plan_runtime():
    env = _FakeFacadeEnv()
    backend = BackendFacade(env)
    txn = VectorMemTxn(
        req_id=0x61,
        is_load=True,
        opcode_class="unit_stride",
        base_addr=0x8000,
        lq_ptr=QueuePtr(flag=0, value=3),
        sq_ptr=QueuePtr(flag=0, value=4),
        vl=2,
        element_count=2,
    )

    result = backend.execute(
        BackendSendPlan.from_steps(
            VectorEnqueueStep.from_txn(txn),
            VectorIssueStep.from_txn(txn, max_cycles=33),
            VectorWaitStep(req_id=txn.req_id, event="complete_or_trap", max_cycles=77),
        )
    )

    assert env.lsq_agent.calls == [("enqueue_vector_mem", txn)]
    assert env.vector_issue_agent.calls == [("issue", txn, 33)]
    assert env.vector_monitor.registrations == [(txn.req_id, txn.rob_idx.flag, txn.rob_idx.value)]
    assert result.get_vector_result(txn.req_id).req_id == txn.req_id


def test_api_issue_cycle_plan_rejects_duplicate_lanes():
    with pytest.raises(ValueError):
        IssueCyclePlan.from_ops(
            IssueOp.std(req_id=1, sq_ptr=QueuePtr(0, 1), data=0x11, lane=5),
            IssueOp.sta(req_id=2, sq_ptr=QueuePtr(0, 2), addr=0x1000, lane=5),
        )
