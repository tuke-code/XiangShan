# coding=utf-8
"""
MemBlock 事务对象定义。
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class QueuePtr:
    """环形队列指针。"""

    flag: int
    value: int


@dataclass(frozen=True)
class LoadTxn:
    """标量 load 事务。"""

    req_id: int
    addr: int
    lq_ptr: QueuePtr
    sq_ptr: QueuePtr
    size: int = 8
    mask: int = 0xFF
    pdest: int | None = None
    enq_port: int = 0
    issue_lane: int = 0

    @property
    def rob_idx_flag(self) -> int:
        return (self.req_id >> 9) & 0x1

    @property
    def rob_idx_value(self) -> int:
        return self.req_id & 0x1FF

    @property
    def resolved_pdest(self) -> int:
        return self.req_id % 64 if self.pdest is None else int(self.pdest)


@dataclass(frozen=True)
class StoreTxn:
    """标量 store 事务。"""

    req_id: int
    sq_ptr: QueuePtr
    addr: int
    data: int
    mask: int = 0xFF
    enq_port: int = 0
    sta_lane: int = 3
    std_lane: int = 5

    @property
    def rob_idx_flag(self) -> int:
        return (self.req_id >> 9) & 0x1

    @property
    def rob_idx_value(self) -> int:
        return self.req_id & 0x1FF
