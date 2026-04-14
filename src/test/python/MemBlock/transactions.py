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
VECTOR_OPCODE_CLASS_TO_LOAD_FU_OP_TYPE = {
    "unit_stride": 0b01_00_00000,
    "stride": 0b01_10_00000,
}
VECTOR_OPCODE_CLASS_TO_STORE_FU_OP_TYPE = {
    "unit_stride": 0b10_00_00000,
    "stride": 0b10_10_00000,
}

# Match the one-hot ordering in `backend/fu/FuType.scala`.
FU_TYPE_VLDU = 1 << 31
FU_TYPE_VSTU = 1 << 32


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


def vector_fu_op_type(*, is_load: bool, opcode_class: str) -> int:
    if is_load:
        mapping = VECTOR_OPCODE_CLASS_TO_LOAD_FU_OP_TYPE
    else:
        mapping = VECTOR_OPCODE_CLASS_TO_STORE_FU_OP_TYPE
    try:
        return mapping[opcode_class]
    except KeyError as exc:
        raise ValueError(f"unsupported vector opcode class: {opcode_class}") from exc


def vector_fu_type(*, is_load: bool) -> int:
    return FU_TYPE_VLDU if is_load else FU_TYPE_VSTU


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
class VectorMemTxn:
    """Vector memory transaction carried through enqueue + vecIssue."""

    req_id: int
    is_load: bool
    opcode_class: str
    base_addr: int
    lq_ptr: QueuePtr
    sq_ptr: QueuePtr
    vl: int
    element_count: int
    sew_bits: int = 32
    stride: int = 0
    vstart: int = 0
    mask_bits: tuple[int, ...] | None = None
    store_data: tuple[int, ...] | None = None
    enq_port: int = 0
    issue_port: int = 0
    num_ls_elem: int | None = None
    pdest: int | None = None
    pdest_vl: int = 0
    lmul: int = 0
    vmask: int = (1 << 128) - 1
    nf: int = 0
    veew: int = 0
    vm: bool = True
    vuop_idx: int = 0
    last_uop: bool = True
    is_vleff: bool = False
    src_0: int | None = None
    src_1: int | None = None
    src_2: int = 0
    src_3: int = 0
    expected_exception: str | None = None

    def __post_init__(self) -> None:
        if self.opcode_class not in {"unit_stride", "stride"}:
            raise ValueError(f"unsupported vector opcode class: {self.opcode_class}")
        if self.sew_bits not in {8, 16, 32, 64}:
            raise ValueError(f"unsupported vector SEW: {self.sew_bits}")
        if self.vl < 0 or self.vstart < 0 or self.element_count <= 0:
            raise ValueError("vl/vstart/element_count must be non-negative and element_count > 0")
        if self.vstart > self.element_count:
            raise ValueError("vstart must not exceed element_count")
        if self.vuop_idx < 0 or self.vuop_idx > 0x7F:
            raise ValueError("vuop_idx must be in range [0, 127]")
        if self.mask_bits is not None and len(self.mask_bits) < self.element_count:
            raise ValueError("mask_bits must cover all modeled elements")
        if self.store_data is not None and len(self.store_data) < self.element_count:
            raise ValueError("store_data must cover all modeled elements")
        if not self.is_load and self.store_data is None:
            raise ValueError("vector stores require `store_data`")

    @property
    def rob_idx_flag(self) -> int:
        return (self.req_id >> 9) & 0x1

    @property
    def rob_idx_value(self) -> int:
        return self.req_id & 0x1FF

    @property
    def resolved_pdest(self) -> int:
        return self.req_id % 128 if self.pdest is None else int(self.pdest)

    @property
    def size_bytes(self) -> int:
        return self.sew_bits // 8

    @property
    def resolved_num_ls_elem(self) -> int:
        if self.num_ls_elem is not None:
            return int(self.num_ls_elem)
        return int(self.element_count)

    @property
    def fu_type(self) -> int:
        return vector_fu_type(is_load=self.is_load)

    @property
    def fu_op_type(self) -> int:
        return vector_fu_op_type(is_load=self.is_load, opcode_class=self.opcode_class)

    @property
    def issue_src_0(self) -> int:
        return int(self.base_addr if self.src_0 is None else self.src_0)

    @property
    def issue_src_1(self) -> int:
        if self.src_1 is not None:
            return int(self.src_1)
        if self.opcode_class == "stride":
            return int(self.stride)
        return 0

    @property
    def resolved_vuop_idx(self) -> int:
        return int(self.vuop_idx)


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
class EnqueueLoadCyclePlan:
    """All load enqueue actions that must fire in the same cycle."""

    steps: tuple[EnqueueLoadStep, ...]
    max_cycles: int = 200

    def __post_init__(self) -> None:
        if not self.steps:
            raise ValueError("load enqueue cycle plan requires at least one step")
        ports = [int(step.enq_port) for step in self.steps]
        if len(set(ports)) != len(ports):
            raise ValueError(f"load enqueue cycle plan requires unique ports: {ports}")

    @classmethod
    def from_steps(cls, *steps: EnqueueLoadStep, max_cycles: int = 200) -> "EnqueueLoadCyclePlan":
        return cls(steps=tuple(steps), max_cycles=max_cycles)

    @classmethod
    def from_txns(cls, *txns: LoadTxn, max_cycles: int = 200) -> "EnqueueLoadCyclePlan":
        return cls(
            steps=tuple(
                EnqueueLoadStep(
                    req_id=txn.req_id,
                    lq_ptr=txn.lq_ptr,
                    sq_ptr=txn.sq_ptr,
                    enq_port=index,
                )
                for index, txn in enumerate(txns)
            ),
            max_cycles=max_cycles,
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
class VectorEnqueueStep:
    """One vector enqueue action inside a backend send plan."""

    txn: VectorMemTxn

    @classmethod
    def from_txn(cls, txn: VectorMemTxn) -> "VectorEnqueueStep":
        return cls(txn=txn)


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
class VectorIssueStep:
    """One vector issue action inside a backend send plan."""

    txn: VectorMemTxn
    max_cycles: int = 50

    @classmethod
    def from_txn(cls, txn: VectorMemTxn, max_cycles: int = 50) -> "VectorIssueStep":
        return cls(txn=txn, max_cycles=max_cycles)


@dataclass(frozen=True)
class VectorWaitStep:
    """Wait for a vector request to complete or trap."""

    req_id: int
    event: str = "complete_or_trap"
    max_cycles: int = 200

    def __post_init__(self) -> None:
        if self.event not in {"complete", "trap", "complete_or_trap"}:
            raise ValueError(f"unsupported vector wait event: {self.event}")


@dataclass(frozen=True)
class VectorElementAccess:
    """Element-level reference access generated from a vector memory op."""

    element_idx: int
    active: bool
    is_tail: bool
    is_prestart: bool
    addr: int
    size_bytes: int
    expected_load_data: int | None = None
    store_data: int | None = None
    should_access_memory: bool = False
    field_idx: int = 0


@dataclass(frozen=True)
class VectorMemResult:
    """Observed completion summary for a vector memory request."""

    req_id: int
    completed: bool
    trapped: bool
    observed_vl: int | None = None
    observed_vstart: int | None = None
    observed_requests: tuple[dict, ...] = ()
    observed_writebacks: tuple[dict, ...] = ()
    observed_exception: dict | None = None


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
                    EnqueueLoadCyclePlan,
                    EnqueueStoreStep,
                    VectorEnqueueStep,
                    IssueCyclePlan,
                    VectorIssueStep,
                    StoreCommitStep,
                    NonMemBlockerStep,
                    StoreCommitReadyStep,
                    VectorWaitStep,
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
    vector_results: dict[int, VectorMemResult] | None = None

    def resolve_sq_ptr(self, sq_ptr: QueuePtr | StoreRef) -> QueuePtr:
        if isinstance(sq_ptr, QueuePtr):
            return sq_ptr
        return self.store_ptrs[sq_ptr]

    def get_vector_result(self, req_id: int) -> VectorMemResult:
        if self.vector_results is None:
            raise KeyError(req_id)
        return self.vector_results[req_id]
