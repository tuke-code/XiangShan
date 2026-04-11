# coding=utf-8
"""
request_apis should route active control through env.backend.
"""

import pytest

from agents.backend_facade import BackendFacade
from transactions import (
    BackendSendPlan,
    EnqueueLoadStep,
    EnqueueStoreStep,
    IssueCyclePlan,
    IssueOp,
    LoadTxn,
    QueuePtr,
    StoreRef,
    StoreTxn,
    scalar_store_fu_op_type_from_mask,
)

from request_apis import (
    enqueue_scalar_load,
    enqueue_scalar_store,
    issue_scalar_load,
    issue_scalar_sta,
    issue_scalar_std,
    send_load,
    send_store,
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

    def enqueue_scalar_load(self, req_id, lq_ptr, sq_ptr, enq_port: int = 0) -> None:
        self.calls.append(("enqueue_scalar_load", req_id, lq_ptr, sq_ptr, enq_port))

    def enqueue_scalar_store(self, req_id, sq_ptr, enq_port: int = 0):
        self.calls.append(("enqueue_scalar_store", req_id, sq_ptr, enq_port))
        return QueuePtr(flag=sq_ptr.flag ^ 1, value=sq_ptr.value + 1)

    def issue_scalar_load(self, req_id, addr, lq_ptr, sq_ptr, **kwargs) -> None:
        self.calls.append(("issue_scalar_load", req_id, addr, lq_ptr, sq_ptr, kwargs))

    def issue_scalar_std(self, req_id, sq_ptr, data, lane: int = 5, mask: int = 0xFF) -> None:
        self.calls.append(("issue_scalar_std", req_id, sq_ptr, data, lane, mask))

    def issue_scalar_sta(self, req_id, sq_ptr, addr, lane: int = 3, mask: int = 0xFF) -> None:
        self.calls.append(("issue_scalar_sta", req_id, sq_ptr, addr, lane, mask))

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


class _FakeLsqAgent:
    def __init__(self) -> None:
        self.calls = []

    def wait_load_enq_ready(self, max_cycles: int = 200) -> None:
        self.calls.append(("wait_load_enq_ready", max_cycles))

    def wait_store_enq_ready(self, max_cycles: int = 200) -> None:
        self.calls.append(("wait_store_enq_ready", max_cycles))

    def enqueue_scalar_load(self, req_id, lq_ptr, sq_ptr, enq_port: int = 0) -> None:
        self.calls.append(("enqueue_scalar_load", req_id, lq_ptr, sq_ptr, enq_port))

    def enqueue_scalar_store(self, req_id, sq_ptr, enq_port: int = 0):
        allocated = QueuePtr(flag=sq_ptr.flag ^ 1, value=sq_ptr.value + 1)
        self.calls.append(("enqueue_scalar_store", req_id, sq_ptr, enq_port, allocated))
        return allocated


class _FakeIssueAgent:
    def __init__(self) -> None:
        self.calls = []

    def issue_cycle(self, plan) -> None:
        self.calls.append(("issue_cycle", plan))


class _FakeCommitAgent:
    def __init__(self) -> None:
        self.calls = []

    def note_load_issued(self, rob_idx_flag: int, rob_idx_value: int) -> None:
        self.calls.append(("note_load_issued", rob_idx_flag, rob_idx_value))

    def note_store_allocated(self, rob_idx_flag: int, rob_idx_value: int) -> None:
        self.calls.append(("note_store_allocated", rob_idx_flag, rob_idx_value))

    def note_load_completed(self, rob_idx_flag: int, rob_idx_value: int) -> None:
        self.calls.append(("note_load_completed", rob_idx_flag, rob_idx_value))

    def queue_store_commit(self, count: int = 1) -> None:
        self.calls.append(("queue_store_commit", count))


class _FakeMemory:
    def __init__(self) -> None:
        self.calls = []

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
        self.rob_agent = object()
        self.commit_agent = _FakeCommitAgent()
        self.memory = _FakeMemory()
        self.run_async_calls = []

    def _run_async(self, marker) -> None:
        self.run_async_calls.append(marker)

    async def _await_cycles(self, cycles: int):
        return cycles

    def _flush_store_buffers_and_wait_impl(self, max_cycles: int = 200, settle_cycles: int = 4) -> dict:
        return {"max_cycles": max_cycles, "settle_cycles": settle_cycles}


class _FakeEnv:
    def __init__(self) -> None:
        self.backend = _FakeBackend()


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
    assert env.backend.calls[5] == ("issue_scalar_std", 8, sq_ptr, 0x55, 6, 0x0F)
    assert env.backend.calls[6] == ("issue_scalar_sta", 9, sq_ptr, 0x2000, 3, 0x03)


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

    send_load(env, load_txn)
    result = send_store(env, store_txn)

    assert env.backend.calls[0] == ("send", load_txn)
    assert env.backend.calls[1] == ("send", store_txn)
    assert result == QueuePtr(flag=1, value=11)


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
    assert [op.kind for op in env.backend.calls[0][1].steps[-1].ops] == ["load", "load"]
    assert [op.kind for op in env.backend.calls[1][1].steps[-1].ops] == ["load", "load", "sta"]
    assert env.backend.calls[1][1].steps[-1].ops[-1].mask == 0x0F


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


def test_api_issue_op_rejects_non_scalar_store_mask():
    with pytest.raises(ValueError):
        IssueOp.std(req_id=1, sq_ptr=QueuePtr(0, 1), data=0x11, lane=5, mask=0x05)


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


def test_api_issue_cycle_plan_rejects_duplicate_lanes():
    with pytest.raises(ValueError):
        IssueCyclePlan.from_ops(
            IssueOp.std(req_id=1, sq_ptr=QueuePtr(0, 1), data=0x11, lane=5),
            IssueOp.sta(req_id=2, sq_ptr=QueuePtr(0, 2), addr=0x1000, lane=5),
        )
