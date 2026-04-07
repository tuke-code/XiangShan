# coding=utf-8
"""
request_apis should route active control through env.backend.
"""

from transactions import LoadTxn, QueuePtr, StoreTxn

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

    def issue_scalar_std(self, req_id, sq_ptr, data, lane: int = 5) -> None:
        self.calls.append(("issue_scalar_std", req_id, sq_ptr, data, lane))

    def issue_scalar_sta(self, req_id, sq_ptr, addr, lane: int = 3) -> None:
        self.calls.append(("issue_scalar_sta", req_id, sq_ptr, addr, lane))

    def send_load(self, txn) -> None:
        self.calls.append(("send_load", txn))

    def send_store(self, txn):
        self.calls.append(("send_store", txn))
        return QueuePtr(flag=1, value=9)


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
    issue_scalar_std(env, req_id=8, sq_ptr=sq_ptr, data=0x55, lane=6)
    issue_scalar_sta(env, req_id=9, sq_ptr=sq_ptr, addr=0x2000, lane=3)

    assert returned_sq_ptr == QueuePtr(flag=0, value=4)
    assert env.backend.calls[0] == ("wait_load_enq_ready", 19)
    assert env.backend.calls[1] == ("wait_store_enq_ready", 21)
    assert env.backend.calls[2][0] == "enqueue_scalar_load"
    assert env.backend.calls[3][0] == "enqueue_scalar_store"
    assert env.backend.calls[4][0] == "issue_scalar_load"
    assert env.backend.calls[5][0] == "issue_scalar_std"
    assert env.backend.calls[6][0] == "issue_scalar_sta"


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

    assert env.backend.calls[0] == ("send_load", load_txn)
    assert env.backend.calls[1] == ("send_store", store_txn)
    assert result == QueuePtr(flag=1, value=9)
