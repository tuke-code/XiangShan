# coding=utf-8
"""
MemBlock 事务对象定义。
"""

from dataclasses import dataclass


SCALAR_STORE_MASK_TO_SIZE_BYTES = {
    0x01: 1,
    0x03: 2,
    0x0F: 4,
    0xFF: 8,
}
SCALAR_STORE_SIZE_BYTES_TO_FU_OP_TYPE = {
    1: 0x0,
    2: 0x1,
    4: 0x2,
    8: 0x3,
}


def scalar_store_size_bytes_from_mask(mask: int) -> int:
    """Decode scalar store width from the byte mask carried by StoreTxn/IssueOp."""

    normalized_mask = int(mask) & 0xFF
    size_bytes = SCALAR_STORE_MASK_TO_SIZE_BYTES.get(normalized_mask)
    if size_bytes is None:
        raise ValueError(
            "unsupported scalar store mask "
            f"{int(mask):#x}; expected one of {sorted(SCALAR_STORE_MASK_TO_SIZE_BYTES)}"
        )
    return size_bytes


def scalar_store_fu_op_type_from_mask(mask: int) -> int:
    return SCALAR_STORE_SIZE_BYTES_TO_FU_OP_TYPE[scalar_store_size_bytes_from_mask(mask)]


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
    store_set_hit: int = 0
    load_wait_bit: int = 0
    load_wait_strict: int = 0
    wait_for_rob_idx_flag: int | None = None
    wait_for_rob_idx_value: int | None = None

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

    @property
    def size_bytes(self) -> int:
        return scalar_store_size_bytes_from_mask(self.mask)

    @property
    def fu_op_type(self) -> int:
        return scalar_store_fu_op_type_from_mask(self.mask)


@dataclass(frozen=True)
class StoreRef:
    """Symbolic handle for a store SQ pointer allocated during plan execution."""

    name: str


@dataclass(frozen=True)
class EnqueueLoadStep:
    """One load enqueue action inside a backend send plan."""

    req_id: int
    lq_ptr: QueuePtr
    sq_ptr: QueuePtr
    enq_port: int = 0

    @classmethod
    def from_txn(cls, txn: LoadTxn) -> "EnqueueLoadStep":
        return cls(
            req_id=txn.req_id,
            lq_ptr=txn.lq_ptr,
            sq_ptr=txn.sq_ptr,
            enq_port=txn.enq_port,
        )


@dataclass(frozen=True)
class EnqueueStoreStep:
    """One store enqueue action inside a backend send plan."""

    req_id: int
    sq_ptr: QueuePtr
    enq_port: int = 0
    ref: StoreRef | None = None

    @classmethod
    def from_txn(cls, txn: StoreTxn, *, ref: StoreRef | None = None) -> "EnqueueStoreStep":
        return cls(
            req_id=txn.req_id,
            sq_ptr=txn.sq_ptr,
            enq_port=txn.enq_port,
            ref=ref,
        )


@dataclass(frozen=True)
class IssueOp:
    """One issue-lane action in a single issue cycle."""

    kind: str
    req_id: int
    lane: int
    sq_ptr: QueuePtr | StoreRef
    addr: int | None = None
    data: int | None = None
    lq_ptr: QueuePtr | None = None
    mask: int = 0xFF
    store_set_hit: int = 0
    load_wait_bit: int = 0
    load_wait_strict: int = 0
    wait_for_rob_idx_flag: int | None = None
    wait_for_rob_idx_value: int | None = None

    def __post_init__(self) -> None:
        if self.kind not in {"load", "sta", "std"}:
            raise ValueError(f"unsupported issue op kind: {self.kind}")
        if self.kind == "load":
            if self.addr is None or self.lq_ptr is None:
                raise ValueError("load issue op requires `addr` and `lq_ptr`")
        elif self.kind == "sta":
            if self.addr is None:
                raise ValueError("STA issue op requires `addr`")
            scalar_store_size_bytes_from_mask(self.mask)
        elif self.kind == "std":
            if self.data is None:
                raise ValueError("STD issue op requires `data`")
            scalar_store_size_bytes_from_mask(self.mask)

    @classmethod
    def load_from_txn(cls, txn: LoadTxn) -> "IssueOp":
        return cls(
            kind="load",
            req_id=txn.req_id,
            lane=txn.issue_lane,
            sq_ptr=txn.sq_ptr,
            addr=txn.addr,
            lq_ptr=txn.lq_ptr,
            store_set_hit=txn.store_set_hit,
            load_wait_bit=txn.load_wait_bit,
            load_wait_strict=txn.load_wait_strict,
            wait_for_rob_idx_flag=txn.wait_for_rob_idx_flag,
            wait_for_rob_idx_value=txn.wait_for_rob_idx_value,
        )

    @classmethod
    def sta(
        cls,
        *,
        req_id: int,
        sq_ptr: QueuePtr | StoreRef,
        addr: int,
        lane: int = 3,
        mask: int = 0xFF,
    ) -> "IssueOp":
        return cls(kind="sta", req_id=req_id, lane=lane, sq_ptr=sq_ptr, addr=addr, mask=mask)

    @classmethod
    def std(
        cls,
        *,
        req_id: int,
        sq_ptr: QueuePtr | StoreRef,
        data: int,
        lane: int = 5,
        mask: int = 0xFF,
    ) -> "IssueOp":
        return cls(kind="std", req_id=req_id, lane=lane, sq_ptr=sq_ptr, data=data, mask=mask)

    @property
    def store_size_bytes(self) -> int:
        return scalar_store_size_bytes_from_mask(self.mask)

    @property
    def store_fu_op_type(self) -> int:
        return scalar_store_fu_op_type_from_mask(self.mask)


@dataclass(frozen=True)
class IssueCyclePlan:
    """All issue operations that must fire in the same cycle."""

    ops: tuple[IssueOp, ...]
    max_cycles: int = 50

    def __post_init__(self) -> None:
        if not self.ops:
            raise ValueError("issue cycle plan requires at least one op")
        lanes = [int(op.lane) for op in self.ops]
        if len(set(lanes)) != len(lanes):
            raise ValueError(f"issue cycle plan requires unique lanes: {lanes}")

    @classmethod
    def from_ops(cls, *ops: IssueOp, max_cycles: int = 50) -> "IssueCyclePlan":
        return cls(ops=tuple(ops), max_cycles=max_cycles)


@dataclass(frozen=True)
class StoreCommitStep:
    """Commit pulse action inside a backend send plan."""

    count: int = 1
    cycles: int = 1


@dataclass(frozen=True)
class NonMemBlockerStep:
    """Inject or release a ROB-side non-mem blocker inside a backend send plan."""

    action: str
    rob_idx_flag: int
    rob_idx_value: int

    def __post_init__(self) -> None:
        if self.action not in {"insert", "release"}:
            raise ValueError(f"unsupported non-mem blocker action: {self.action}")

    @classmethod
    def insert(cls, *, rob_idx_flag: int, rob_idx_value: int) -> "NonMemBlockerStep":
        return cls(action="insert", rob_idx_flag=rob_idx_flag, rob_idx_value=rob_idx_value)

    @classmethod
    def release(cls, *, rob_idx_flag: int, rob_idx_value: int) -> "NonMemBlockerStep":
        return cls(action="release", rob_idx_flag=rob_idx_flag, rob_idx_value=rob_idx_value)


@dataclass(frozen=True)
class StoreCommitReadyStep:
    """Set the effective ROB-side commit readiness for a store entry."""

    sq_ptr: QueuePtr | StoreRef
    ready: bool = True


@dataclass(frozen=True)
class BackendSendPlan:
    """Ordered enqueue/issue/commit script executed by BackendFacade."""

    steps: tuple[object, ...]

    def __post_init__(self) -> None:
        if not self.steps:
            raise ValueError("backend send plan requires at least one step")
        for step in self.steps:
            if not isinstance(
                step,
                (
                    EnqueueLoadStep,
                    EnqueueStoreStep,
                    IssueCyclePlan,
                    StoreCommitStep,
                    NonMemBlockerStep,
                    StoreCommitReadyStep,
                ),
            ):
                raise TypeError(f"unsupported backend send step: {type(step)!r}")

    @classmethod
    def from_steps(cls, *steps: object) -> "BackendSendPlan":
        return cls(steps=tuple(steps))


@dataclass(frozen=True)
class BackendSendResult:
    """Execution-time products created while running a backend send plan."""

    store_ptrs: dict[StoreRef, QueuePtr]

    def resolve_sq_ptr(self, sq_ptr: QueuePtr | StoreRef) -> QueuePtr:
        if isinstance(sq_ptr, QueuePtr):
            return sq_ptr
        return self.store_ptrs[sq_ptr]
