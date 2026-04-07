# coding=utf-8
"""
Unified backend-facing facade for MemBlock env.
"""

from transactions import LoadTxn, StoreTxn


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
        self.issue.issue_scalar_load(
            req_id,
            addr,
            lq_ptr,
            sq_ptr,
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
        self.issue_load(
            req_id,
            addr,
            lq_ptr,
            sq_ptr,
            lane=lane,
            store_set_hit=store_set_hit,
            load_wait_bit=load_wait_bit,
            load_wait_strict=load_wait_strict,
            wait_for_rob_idx_flag=wait_for_rob_idx_flag,
            wait_for_rob_idx_value=wait_for_rob_idx_value,
        )

    def issue_std(self, req_id: int, sq_ptr, data: int, lane: int = 5) -> None:
        self.issue.issue_scalar_std(req_id, sq_ptr, data, lane=lane)

    def issue_scalar_std(self, req_id: int, sq_ptr, data: int, lane: int = 5) -> None:
        self.issue_std(req_id, sq_ptr, data, lane=lane)

    def issue_sta(self, req_id: int, sq_ptr, addr: int, lane: int = 3) -> None:
        self.issue.issue_scalar_sta(req_id, sq_ptr, addr, lane=lane)

    def issue_scalar_sta(self, req_id: int, sq_ptr, addr: int, lane: int = 3) -> None:
        self.issue_sta(req_id, sq_ptr, addr, lane=lane)

    def send_load(self, txn: LoadTxn) -> None:
        self.enqueue_load(
            req_id=txn.req_id,
            lq_ptr=txn.lq_ptr,
            sq_ptr=txn.sq_ptr,
            enq_port=txn.enq_port,
        )
        self.issue_load(
            req_id=txn.req_id,
            addr=txn.addr,
            lq_ptr=txn.lq_ptr,
            sq_ptr=txn.sq_ptr,
            lane=txn.issue_lane,
            store_set_hit=txn.store_set_hit,
            load_wait_bit=txn.load_wait_bit,
            load_wait_strict=txn.load_wait_strict,
            wait_for_rob_idx_flag=txn.wait_for_rob_idx_flag,
            wait_for_rob_idx_value=txn.wait_for_rob_idx_value,
        )

    def send_store(self, txn: StoreTxn):
        allocated_sq_ptr = self.enqueue_store(
            req_id=txn.req_id,
            sq_ptr=txn.sq_ptr,
            enq_port=txn.enq_port,
        )
        self.issue_std(
            req_id=txn.req_id,
            sq_ptr=allocated_sq_ptr,
            data=txn.data,
            lane=txn.std_lane,
        )
        self.issue_sta(
            req_id=txn.req_id,
            sq_ptr=allocated_sq_ptr,
            addr=txn.addr,
            lane=txn.sta_lane,
        )
        return allocated_sq_ptr

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
        self.commit.note_store_allocated(rob_idx_flag, rob_idx_value)
        self.env.memory.note_store_allocated(
            sq_idx_flag=sq_idx_flag,
            sq_idx_value=sq_idx_value,
            rob_idx_flag=rob_idx_flag,
            rob_idx_value=rob_idx_value,
        )

    def note_load_completed(self, rob_idx_flag: int, rob_idx_value: int) -> None:
        self.commit.note_load_completed(rob_idx_flag, rob_idx_value)

    def queue_store_commit(self, count: int = 1) -> None:
        self.commit.queue_store_commit(count)

    def step_commit(self, count: int = 1, cycles: int = 1) -> None:
        self.queue_store_commit(count)
        self.env.Step(cycles)

    def pulse_store_commit(self, count: int = 1) -> None:
        self.step_commit(count=count, cycles=1)

    def flush_store_buffers_and_wait(self, max_cycles: int = 200, settle_cycles: int = 4) -> dict:
        return self.env._flush_store_buffers_and_wait_impl(
            max_cycles=max_cycles,
            settle_cycles=settle_cycles,
        )
