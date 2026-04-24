# coding=utf-8
"""
MemBlock 事务对象定义。
"""

from dataclasses import dataclass, field
from typing import Literal


SCALAR_STORE_MASK_TO_SIZE_BYTES = {
    0x01: 1,
    0x03: 2,
    0x0F: 4,
    0xFF: 8,
}
SCALAR_STORE_OPCODE = "scalar"
CBO_ZERO_STORE_OPCODE = "cbo_zero"
CACHELINE_BYTES = 64
LSU_OP_CBO_ZERO = 0x7
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


def normalized_store_opcode(opcode: str) -> str:
    normalized = str(opcode)
    if normalized not in {SCALAR_STORE_OPCODE, CBO_ZERO_STORE_OPCODE}:
        raise ValueError(
            f"unsupported store opcode: {opcode!r}; "
            f"expected one of {[SCALAR_STORE_OPCODE, CBO_ZERO_STORE_OPCODE]}"
        )
    return normalized


def store_size_bytes(*, opcode: str, mask: int) -> int:
    normalized_opcode = normalized_store_opcode(opcode)
    if normalized_opcode == CBO_ZERO_STORE_OPCODE:
        return CACHELINE_BYTES
    return scalar_store_size_bytes_from_mask(mask)


def store_fu_op_type(*, opcode: str, mask: int) -> int:
    normalized_opcode = normalized_store_opcode(opcode)
    if normalized_opcode == CBO_ZERO_STORE_OPCODE:
        return LSU_OP_CBO_ZERO
    return scalar_store_fu_op_type_from_mask(mask)


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


def legacy_rob_idx_flag(req_id: int) -> int:
    return (int(req_id) >> 9) & 0x1


def legacy_rob_idx_value(req_id: int) -> int:
    return int(req_id) & 0x1FF


def legacy_pdest(req_id: int, modulo: int) -> int:
    return int(req_id) % int(modulo)


def legacy_ftq_idx_value(req_id: int) -> int:
    return int(req_id) & 0x3F


def legacy_pc(req_id: int) -> int:
    return 0x80000000 + int(req_id) * 4


def _missing_runtime_binding(owner, field: str, *, remedy: str | None = None):
    owner_type = type(owner).__name__
    req_id = getattr(owner, "req_id", None)
    message = f"{owner_type}.{field} requires explicit runtime binding"
    if req_id is not None:
        message += f" (req_id={int(req_id)})"
    if remedy is None:
        remedy = "call env.backend.prepare()/send()/execute() first, or set the field explicitly"
    raise RuntimeError(f"{message}; {remedy}")


@dataclass(frozen=True)
class QueuePtr:
    """环形队列指针。"""

    flag: int
    value: int


def ptr_inc(ptr: QueuePtr, size: int, step: int = 1) -> QueuePtr:
    """Advance a ring-buffer queue pointer by `step` positions."""

    flag = ptr.flag
    value = ptr.value
    for _ in range(int(step)):
        value += 1
        if value == int(size):
            value = 0
            flag ^= 0x1
    return QueuePtr(flag=flag, value=value)


@dataclass(frozen=True)
class RobIndex:
    """ROB index used by env-managed allocation/runtime lookup."""

    flag: int
    value: int


@dataclass(frozen=True)
class RobRef:
    """Symbolic handle for a ROB entry allocated during plan preparation."""

    name: str


def make_rob_index(
    *,
    rob_idx: RobIndex | None = None,
    rob_idx_flag: int | None = None,
    rob_idx_value: int | None = None,
) -> RobIndex | None:
    """Normalize legacy `(flag, value)` pairs into the canonical `RobIndex`."""

    if rob_idx is not None:
        if isinstance(rob_idx, int):
            if rob_idx_flag is not None:
                raise ValueError("legacy positional rob_idx flag conflicts with keyword rob_idx_flag")
            if rob_idx_value is None:
                raise ValueError("legacy positional rob_idx flag requires rob_idx_value")
            return RobIndex(flag=int(rob_idx), value=int(rob_idx_value))
        normalized = RobIndex(flag=int(rob_idx.flag), value=int(rob_idx.value))
        if rob_idx_flag is not None and int(rob_idx_flag) != normalized.flag:
            raise ValueError(f"conflicting rob_idx_flag: {rob_idx_flag} vs {normalized.flag}")
        if rob_idx_value is not None and int(rob_idx_value) != normalized.value:
            raise ValueError(f"conflicting rob_idx_value: {rob_idx_value} vs {normalized.value}")
        return normalized
    if rob_idx_flag is None and rob_idx_value is None:
        return None
    if rob_idx_flag is None or rob_idx_value is None:
        raise ValueError("rob_idx requires both flag and value when provided as split fields")
    return RobIndex(flag=int(rob_idx_flag), value=int(rob_idx_value))


@dataclass
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
    wait_for_rob: object | None = None
    rob_ref: RobRef | None = None
    rob_idx_override_flag: int | None = None
    rob_idx_override_value: int | None = None
    ftq_idx_flag: int | None = None
    ftq_idx_value: int | None = None
    pc: int | None = None
    assigned_rob_idx_flag: int | None = field(default=None, repr=False, compare=False)
    assigned_rob_idx_value: int | None = field(default=None, repr=False, compare=False)
    assigned_pdest: int | None = field(default=None, repr=False, compare=False)
    assigned_ftq_idx_flag: int | None = field(default=None, repr=False, compare=False)
    assigned_ftq_idx_value: int | None = field(default=None, repr=False, compare=False)
    assigned_pc: int | None = field(default=None, repr=False, compare=False)

    @property
    def wait_for_rob_idx(self) -> RobIndex | None:
        return make_rob_index(
            rob_idx_flag=self.wait_for_rob_idx_flag,
            rob_idx_value=self.wait_for_rob_idx_value,
        )

    @wait_for_rob_idx.setter
    def wait_for_rob_idx(self, rob_idx: RobIndex | None) -> None:
        normalized = make_rob_index(rob_idx=rob_idx)
        self.wait_for_rob_idx_flag = None if normalized is None else int(normalized.flag)
        self.wait_for_rob_idx_value = None if normalized is None else int(normalized.value)

    @property
    def rob_idx_override(self) -> RobIndex | None:
        return make_rob_index(
            rob_idx_flag=self.rob_idx_override_flag,
            rob_idx_value=self.rob_idx_override_value,
        )

    @rob_idx_override.setter
    def rob_idx_override(self, rob_idx: RobIndex | None) -> None:
        normalized = make_rob_index(rob_idx=rob_idx)
        self.rob_idx_override_flag = None if normalized is None else int(normalized.flag)
        self.rob_idx_override_value = None if normalized is None else int(normalized.value)

    @property
    def assigned_rob_idx(self) -> RobIndex | None:
        return make_rob_index(
            rob_idx_flag=self.assigned_rob_idx_flag,
            rob_idx_value=self.assigned_rob_idx_value,
        )

    @assigned_rob_idx.setter
    def assigned_rob_idx(self, rob_idx: RobIndex | None) -> None:
        normalized = make_rob_index(rob_idx=rob_idx)
        self.assigned_rob_idx_flag = None if normalized is None else int(normalized.flag)
        self.assigned_rob_idx_value = None if normalized is None else int(normalized.value)

    @property
    def rob_idx(self) -> RobIndex:
        assigned = self.assigned_rob_idx
        if assigned is not None:
            return assigned
        override = self.rob_idx_override
        if override is not None:
            return override
        _missing_runtime_binding(self, "rob_idx", remedy="call env.backend.prepare()/send()/execute(), or set rob_idx_override")

    @property
    def rob_idx_flag(self) -> int:
        return int(self.rob_idx.flag)

    @property
    def rob_idx_value(self) -> int:
        return int(self.rob_idx.value)

    @property
    def resolved_pdest(self) -> int:
        if self.assigned_pdest is not None:
            return int(self.assigned_pdest)
        if self.pdest is not None:
            return int(self.pdest)
        _missing_runtime_binding(self, "resolved_pdest", remedy="call env.backend.prepare()/send()/execute(), or set pdest")

    @property
    def resolved_ftq_idx_flag(self) -> int:
        if self.assigned_ftq_idx_flag is not None:
            return int(self.assigned_ftq_idx_flag)
        if self.ftq_idx_flag is not None:
            return int(self.ftq_idx_flag)
        if self.assigned_ftq_idx_value is not None or self.ftq_idx_value is not None:
            return 0
        _missing_runtime_binding(self, "resolved_ftq_idx_flag", remedy="call env.backend.prepare()/send()/execute(), or set ftq_idx_flag/ftq_idx_value")

    @property
    def resolved_ftq_idx_value(self) -> int:
        if self.assigned_ftq_idx_value is not None:
            return int(self.assigned_ftq_idx_value)
        if self.ftq_idx_value is not None:
            return int(self.ftq_idx_value)
        _missing_runtime_binding(self, "resolved_ftq_idx_value", remedy="call env.backend.prepare()/send()/execute(), or set ftq_idx_value")

    @property
    def resolved_pc(self) -> int:
        if self.assigned_pc is not None:
            return int(self.assigned_pc)
        if self.pc is not None:
            return int(self.pc)
        _missing_runtime_binding(self, "resolved_pc", remedy="call env.backend.prepare()/send()/execute(), or set pc")


@dataclass
class StoreTxn:
    """标量 store 事务。"""

    req_id: int
    sq_ptr: QueuePtr
    addr: int
    data: int
    mask: int = 0xFF
    opcode: Literal["scalar", "cbo_zero"] = SCALAR_STORE_OPCODE
    enq_port: int = 0
    sta_lane: int = 3
    std_lane: int = 5
    rob_ref: RobRef | None = None
    rob_idx_override_flag: int | None = None
    rob_idx_override_value: int | None = None
    ftq_idx_flag: int | None = None
    ftq_idx_value: int | None = None
    assigned_rob_idx_flag: int | None = field(default=None, repr=False, compare=False)
    assigned_rob_idx_value: int | None = field(default=None, repr=False, compare=False)
    assigned_ftq_idx_flag: int | None = field(default=None, repr=False, compare=False)
    assigned_ftq_idx_value: int | None = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        normalized_store_opcode(self.opcode)
        if self.opcode == SCALAR_STORE_OPCODE:
            scalar_store_size_bytes_from_mask(self.mask)

    @classmethod
    def cbo_zero(
        cls,
        *,
        req_id: int,
        sq_ptr: QueuePtr,
        addr: int,
        enq_port: int = 0,
        sta_lane: int = 3,
        std_lane: int = 5,
        rob_ref: RobRef | None = None,
        rob_idx_override_flag: int | None = None,
        rob_idx_override_value: int | None = None,
        ftq_idx_flag: int | None = None,
        ftq_idx_value: int | None = None,
    ) -> "StoreTxn":
        return cls(
            req_id=req_id,
            sq_ptr=sq_ptr,
            addr=addr,
            data=0,
            mask=0xFF,
            opcode=CBO_ZERO_STORE_OPCODE,
            enq_port=enq_port,
            sta_lane=sta_lane,
            std_lane=std_lane,
            rob_ref=rob_ref,
            rob_idx_override_flag=rob_idx_override_flag,
            rob_idx_override_value=rob_idx_override_value,
            ftq_idx_flag=ftq_idx_flag,
            ftq_idx_value=ftq_idx_value,
        )

    @property
    def rob_idx_override(self) -> RobIndex | None:
        return make_rob_index(
            rob_idx_flag=self.rob_idx_override_flag,
            rob_idx_value=self.rob_idx_override_value,
        )

    @rob_idx_override.setter
    def rob_idx_override(self, rob_idx: RobIndex | None) -> None:
        normalized = make_rob_index(rob_idx=rob_idx)
        self.rob_idx_override_flag = None if normalized is None else int(normalized.flag)
        self.rob_idx_override_value = None if normalized is None else int(normalized.value)

    @property
    def assigned_rob_idx(self) -> RobIndex | None:
        return make_rob_index(
            rob_idx_flag=self.assigned_rob_idx_flag,
            rob_idx_value=self.assigned_rob_idx_value,
        )

    @assigned_rob_idx.setter
    def assigned_rob_idx(self, rob_idx: RobIndex | None) -> None:
        normalized = make_rob_index(rob_idx=rob_idx)
        self.assigned_rob_idx_flag = None if normalized is None else int(normalized.flag)
        self.assigned_rob_idx_value = None if normalized is None else int(normalized.value)

    @property
    def rob_idx(self) -> RobIndex:
        assigned = self.assigned_rob_idx
        if assigned is not None:
            return assigned
        override = self.rob_idx_override
        if override is not None:
            return override
        _missing_runtime_binding(self, "rob_idx", remedy="call env.backend.prepare()/send()/execute(), or set rob_idx_override")

    @property
    def rob_idx_flag(self) -> int:
        return int(self.rob_idx.flag)

    @property
    def rob_idx_value(self) -> int:
        return int(self.rob_idx.value)

    @property
    def resolved_ftq_idx_flag(self) -> int:
        if self.assigned_ftq_idx_flag is not None:
            return int(self.assigned_ftq_idx_flag)
        if self.ftq_idx_flag is not None:
            return int(self.ftq_idx_flag)
        if self.assigned_ftq_idx_value is not None or self.ftq_idx_value is not None:
            return 0
        _missing_runtime_binding(self, "resolved_ftq_idx_flag", remedy="call env.backend.prepare()/send()/execute(), or set ftq_idx_flag/ftq_idx_value")

    @property
    def resolved_ftq_idx_value(self) -> int:
        if self.assigned_ftq_idx_value is not None:
            return int(self.assigned_ftq_idx_value)
        if self.ftq_idx_value is not None:
            return int(self.ftq_idx_value)
        _missing_runtime_binding(self, "resolved_ftq_idx_value", remedy="call env.backend.prepare()/send()/execute(), or set ftq_idx_value")

    @property
    def size_bytes(self) -> int:
        return store_size_bytes(opcode=self.opcode, mask=self.mask)

    @property
    def fu_op_type(self) -> int:
        return store_fu_op_type(opcode=self.opcode, mask=self.mask)

    @property
    def issue_data(self) -> int:
        if self.opcode == CBO_ZERO_STORE_OPCODE:
            return 0
        return int(self.data)

    @property
    def is_cbo_zero(self) -> bool:
        return self.opcode == CBO_ZERO_STORE_OPCODE


@dataclass
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
    rob_ref: RobRef | None = None
    rob_idx_override_flag: int | None = None
    rob_idx_override_value: int | None = None
    ftq_idx_flag: int | None = None
    ftq_idx_value: int | None = None
    assigned_rob_idx_flag: int | None = field(default=None, repr=False, compare=False)
    assigned_rob_idx_value: int | None = field(default=None, repr=False, compare=False)
    assigned_pdest: int | None = field(default=None, repr=False, compare=False)
    assigned_ftq_idx_flag: int | None = field(default=None, repr=False, compare=False)
    assigned_ftq_idx_value: int | None = field(default=None, repr=False, compare=False)

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
    def rob_idx_override(self) -> RobIndex | None:
        return make_rob_index(
            rob_idx_flag=self.rob_idx_override_flag,
            rob_idx_value=self.rob_idx_override_value,
        )

    @rob_idx_override.setter
    def rob_idx_override(self, rob_idx: RobIndex | None) -> None:
        normalized = make_rob_index(rob_idx=rob_idx)
        self.rob_idx_override_flag = None if normalized is None else int(normalized.flag)
        self.rob_idx_override_value = None if normalized is None else int(normalized.value)

    @property
    def assigned_rob_idx(self) -> RobIndex | None:
        return make_rob_index(
            rob_idx_flag=self.assigned_rob_idx_flag,
            rob_idx_value=self.assigned_rob_idx_value,
        )

    @assigned_rob_idx.setter
    def assigned_rob_idx(self, rob_idx: RobIndex | None) -> None:
        normalized = make_rob_index(rob_idx=rob_idx)
        self.assigned_rob_idx_flag = None if normalized is None else int(normalized.flag)
        self.assigned_rob_idx_value = None if normalized is None else int(normalized.value)

    @property
    def rob_idx(self) -> RobIndex:
        assigned = self.assigned_rob_idx
        if assigned is not None:
            return assigned
        override = self.rob_idx_override
        if override is not None:
            return override
        _missing_runtime_binding(self, "rob_idx", remedy="call env.backend.prepare()/send()/execute(), or set rob_idx_override")

    @property
    def rob_idx_flag(self) -> int:
        return int(self.rob_idx.flag)

    @property
    def rob_idx_value(self) -> int:
        return int(self.rob_idx.value)

    @property
    def resolved_pdest(self) -> int:
        if self.assigned_pdest is not None:
            return int(self.assigned_pdest)
        if self.pdest is not None:
            return int(self.pdest)
        _missing_runtime_binding(self, "resolved_pdest", remedy="call env.backend.prepare()/send()/execute(), or set pdest")

    @property
    def resolved_ftq_idx_flag(self) -> int:
        if self.assigned_ftq_idx_flag is not None:
            return int(self.assigned_ftq_idx_flag)
        if self.ftq_idx_flag is not None:
            return int(self.ftq_idx_flag)
        if self.assigned_ftq_idx_value is not None or self.ftq_idx_value is not None:
            return 0
        _missing_runtime_binding(self, "resolved_ftq_idx_flag", remedy="call env.backend.prepare()/send()/execute(), or set ftq_idx_flag/ftq_idx_value")

    @property
    def resolved_ftq_idx_value(self) -> int:
        if self.assigned_ftq_idx_value is not None:
            return int(self.assigned_ftq_idx_value)
        if self.ftq_idx_value is not None:
            return int(self.ftq_idx_value)
        _missing_runtime_binding(self, "resolved_ftq_idx_value", remedy="call env.backend.prepare()/send()/execute(), or set ftq_idx_value")

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
    def resolved_mask_source(self) -> int:
        if self.src_3:
            return int(self.src_3)
        if self.vm or self.mask_bits is None:
            return 0
        mask = 0
        for element_idx in range(int(self.element_count)):
            if int(self.mask_bits[element_idx]):
                mask |= 1 << element_idx
        return mask

    @property
    def resolved_vmask(self) -> int:
        if self.vmask != (1 << 128) - 1 or self.mask_bits is None:
            return int(self.vmask)
        mask = 0
        for element_idx in range(int(self.element_count)):
            if int(self.mask_bits[element_idx]):
                mask |= 1 << element_idx
        return mask

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
    rob_ref: RobRef | None = None
    rob_idx_flag: int | None = None
    rob_idx_value: int | None = None
    txn: LoadTxn | None = field(default=None, repr=False, compare=False)

    @classmethod
    def from_txn(cls, txn: LoadTxn) -> "EnqueueLoadStep":
        return cls(
            req_id=txn.req_id,
            lq_ptr=txn.lq_ptr,
            sq_ptr=txn.sq_ptr,
            enq_port=txn.enq_port,
            rob_ref=txn.rob_ref,
            rob_idx_flag=None if txn.assigned_rob_idx is None else txn.assigned_rob_idx.flag,
            rob_idx_value=None if txn.assigned_rob_idx is None else txn.assigned_rob_idx.value,
            txn=txn,
        )

    @property
    def rob_idx(self) -> RobIndex | None:
        return make_rob_index(rob_idx_flag=self.rob_idx_flag, rob_idx_value=self.rob_idx_value)

    @property
    def resolved_rob_idx(self) -> RobIndex:
        rob_idx = self.rob_idx
        if rob_idx is not None:
            return rob_idx
        _missing_runtime_binding(self, "resolved_rob_idx", remedy="prepare the backend send plan first, or pass rob_idx explicitly")

    @property
    def resolved_rob_idx_flag(self) -> int:
        return int(self.resolved_rob_idx.flag)

    @property
    def resolved_rob_idx_value(self) -> int:
        return int(self.resolved_rob_idx.value)


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
                    rob_ref=txn.rob_ref,
                    rob_idx_flag=None if txn.assigned_rob_idx is None else txn.assigned_rob_idx.flag,
                    rob_idx_value=None if txn.assigned_rob_idx is None else txn.assigned_rob_idx.value,
                    txn=txn,
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
    rob_ref: RobRef | None = None
    rob_idx_flag: int | None = None
    rob_idx_value: int | None = None
    txn: StoreTxn | None = field(default=None, repr=False, compare=False)

    @classmethod
    def from_txn(cls, txn: StoreTxn, *, ref: StoreRef | None = None) -> "EnqueueStoreStep":
        return cls(
            req_id=txn.req_id,
            sq_ptr=txn.sq_ptr,
            enq_port=txn.enq_port,
            ref=ref,
            rob_ref=txn.rob_ref,
            rob_idx_flag=None if txn.assigned_rob_idx is None else txn.assigned_rob_idx.flag,
            rob_idx_value=None if txn.assigned_rob_idx is None else txn.assigned_rob_idx.value,
            txn=txn,
        )

    @property
    def rob_idx(self) -> RobIndex | None:
        return make_rob_index(rob_idx_flag=self.rob_idx_flag, rob_idx_value=self.rob_idx_value)

    @property
    def resolved_rob_idx(self) -> RobIndex:
        rob_idx = self.rob_idx
        if rob_idx is not None:
            return rob_idx
        _missing_runtime_binding(self, "resolved_rob_idx", remedy="prepare the backend send plan first, or pass rob_idx explicitly")

    @property
    def resolved_rob_idx_flag(self) -> int:
        return int(self.resolved_rob_idx.flag)

    @property
    def resolved_rob_idx_value(self) -> int:
        return int(self.resolved_rob_idx.value)


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
    store_opcode: Literal["scalar", "cbo_zero"] = SCALAR_STORE_OPCODE
    store_set_hit: int = 0
    load_wait_bit: int = 0
    load_wait_strict: int = 0
    wait_for_rob_idx_flag: int | None = None
    wait_for_rob_idx_value: int | None = None
    wait_for_rob: object | None = None
    rob_ref: RobRef | None = None
    rob_idx_flag: int | None = None
    rob_idx_value: int | None = None
    pdest: int | None = None
    ftq_idx_flag: int | None = None
    ftq_idx_value: int | None = None
    pc: int | None = None
    txn: LoadTxn | StoreTxn | None = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self.kind not in {"load", "sta", "std"}:
            raise ValueError(f"unsupported issue op kind: {self.kind}")
        if self.kind == "load":
            if self.addr is None or self.lq_ptr is None:
                raise ValueError("load issue op requires `addr` and `lq_ptr`")
        elif self.kind == "sta":
            if self.addr is None:
                raise ValueError("STA issue op requires `addr`")
            store_fu_op_type(opcode=self.store_opcode, mask=self.mask)
        elif self.kind == "std":
            if self.data is None:
                raise ValueError("STD issue op requires `data`")
            store_fu_op_type(opcode=self.store_opcode, mask=self.mask)

    @classmethod
    def load_from_txn(cls, txn: LoadTxn) -> "IssueOp":
        return cls.load(
            req_id=txn.req_id,
            addr=txn.addr,
            lq_ptr=txn.lq_ptr,
            sq_ptr=txn.sq_ptr,
            lane=txn.issue_lane,
            store_set_hit=txn.store_set_hit,
            load_wait_bit=txn.load_wait_bit,
            load_wait_strict=txn.load_wait_strict,
            wait_for_rob_idx=txn.wait_for_rob_idx,
            wait_for_rob=txn.wait_for_rob,
            rob_ref=txn.rob_ref,
            rob_idx=txn.assigned_rob_idx,
            pdest=txn.assigned_pdest if txn.assigned_pdest is not None else txn.pdest,
            ftq_idx_flag=txn.assigned_ftq_idx_flag if txn.assigned_ftq_idx_flag is not None else txn.ftq_idx_flag,
            ftq_idx_value=txn.assigned_ftq_idx_value if txn.assigned_ftq_idx_value is not None else txn.ftq_idx_value,
            pc=txn.assigned_pc if txn.assigned_pc is not None else txn.pc,
            txn=txn,
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
        store_opcode: Literal["scalar", "cbo_zero"] = SCALAR_STORE_OPCODE,
        rob_ref: RobRef | None = None,
        rob_idx: RobIndex | None = None,
        rob_idx_flag: int | None = None,
        rob_idx_value: int | None = None,
        ftq_idx_flag: int | None = None,
        ftq_idx_value: int | None = None,
        txn: StoreTxn | None = None,
    ) -> "IssueOp":
        normalized_rob_idx = make_rob_index(
            rob_idx=rob_idx,
            rob_idx_flag=rob_idx_flag,
            rob_idx_value=rob_idx_value,
        )
        return cls(
            kind="sta",
            req_id=req_id,
            lane=lane,
            sq_ptr=sq_ptr,
            addr=addr,
            mask=mask,
            store_opcode=store_opcode,
            rob_ref=rob_ref,
            rob_idx_flag=None if normalized_rob_idx is None else normalized_rob_idx.flag,
            rob_idx_value=None if normalized_rob_idx is None else normalized_rob_idx.value,
            ftq_idx_flag=ftq_idx_flag,
            ftq_idx_value=ftq_idx_value,
            txn=txn,
        )

    @classmethod
    def std(
        cls,
        *,
        req_id: int,
        sq_ptr: QueuePtr | StoreRef,
        data: int,
        lane: int = 5,
        mask: int = 0xFF,
        store_opcode: Literal["scalar", "cbo_zero"] = SCALAR_STORE_OPCODE,
        rob_ref: RobRef | None = None,
        rob_idx: RobIndex | None = None,
        rob_idx_flag: int | None = None,
        rob_idx_value: int | None = None,
        ftq_idx_flag: int | None = None,
        ftq_idx_value: int | None = None,
        txn: StoreTxn | None = None,
    ) -> "IssueOp":
        normalized_rob_idx = make_rob_index(
            rob_idx=rob_idx,
            rob_idx_flag=rob_idx_flag,
            rob_idx_value=rob_idx_value,
        )
        return cls(
            kind="std",
            req_id=req_id,
            lane=lane,
            sq_ptr=sq_ptr,
            data=data,
            mask=mask,
            store_opcode=store_opcode,
            rob_ref=rob_ref,
            rob_idx_flag=None if normalized_rob_idx is None else normalized_rob_idx.flag,
            rob_idx_value=None if normalized_rob_idx is None else normalized_rob_idx.value,
            ftq_idx_flag=ftq_idx_flag,
            ftq_idx_value=ftq_idx_value,
            txn=txn,
        )

    @classmethod
    def load(
        cls,
        *,
        req_id: int,
        addr: int,
        lq_ptr: QueuePtr,
        sq_ptr: QueuePtr | StoreRef,
        lane: int = 0,
        store_set_hit: int = 0,
        load_wait_bit: int = 0,
        load_wait_strict: int = 0,
        wait_for_rob_idx: RobIndex | None = None,
        wait_for_rob_idx_flag: int | None = None,
        wait_for_rob_idx_value: int | None = None,
        wait_for_rob: object | None = None,
        rob_ref: RobRef | None = None,
        rob_idx: RobIndex | None = None,
        rob_idx_flag: int | None = None,
        rob_idx_value: int | None = None,
        pdest: int | None = None,
        ftq_idx_flag: int | None = None,
        ftq_idx_value: int | None = None,
        pc: int | None = None,
        txn: LoadTxn | None = None,
    ) -> "IssueOp":
        normalized_rob_idx = make_rob_index(
            rob_idx=rob_idx,
            rob_idx_flag=rob_idx_flag,
            rob_idx_value=rob_idx_value,
        )
        normalized_wait = make_rob_index(
            rob_idx=wait_for_rob_idx,
            rob_idx_flag=wait_for_rob_idx_flag,
            rob_idx_value=wait_for_rob_idx_value,
        )
        return cls(
            kind="load",
            req_id=req_id,
            lane=lane,
            sq_ptr=sq_ptr,
            addr=addr,
            lq_ptr=lq_ptr,
            store_set_hit=store_set_hit,
            load_wait_bit=load_wait_bit,
            load_wait_strict=load_wait_strict,
            wait_for_rob_idx_flag=None if normalized_wait is None else normalized_wait.flag,
            wait_for_rob_idx_value=None if normalized_wait is None else normalized_wait.value,
            wait_for_rob=wait_for_rob,
            rob_ref=rob_ref,
            rob_idx_flag=None if normalized_rob_idx is None else normalized_rob_idx.flag,
            rob_idx_value=None if normalized_rob_idx is None else normalized_rob_idx.value,
            pdest=pdest,
            ftq_idx_flag=ftq_idx_flag,
            ftq_idx_value=ftq_idx_value,
            pc=pc,
            txn=txn,
        )

    @property
    def rob_idx(self) -> RobIndex | None:
        return make_rob_index(rob_idx_flag=self.rob_idx_flag, rob_idx_value=self.rob_idx_value)

    @property
    def wait_for_rob_idx(self) -> RobIndex | None:
        return make_rob_index(
            rob_idx_flag=self.wait_for_rob_idx_flag,
            rob_idx_value=self.wait_for_rob_idx_value,
        )

    @property
    def resolved_rob_idx(self) -> RobIndex:
        rob_idx = self.rob_idx
        if rob_idx is not None:
            return rob_idx
        _missing_runtime_binding(self, "resolved_rob_idx", remedy="prepare the backend issue op first, or pass rob_idx explicitly")

    @property
    def resolved_rob_idx_flag(self) -> int:
        return int(self.resolved_rob_idx.flag)

    @property
    def resolved_rob_idx_value(self) -> int:
        return int(self.resolved_rob_idx.value)

    @property
    def resolved_pdest(self) -> int:
        if self.pdest is not None:
            return int(self.pdest)
        _missing_runtime_binding(self, "resolved_pdest", remedy="prepare the backend issue op first, or pass pdest explicitly")

    @property
    def resolved_ftq_idx_flag(self) -> int:
        if self.ftq_idx_flag is not None:
            return int(self.ftq_idx_flag)
        if self.ftq_idx_value is not None:
            return 0
        _missing_runtime_binding(self, "resolved_ftq_idx_flag", remedy="prepare the backend issue op first, or pass ftq_idx_flag/ftq_idx_value explicitly")

    @property
    def resolved_ftq_idx_value(self) -> int:
        if self.ftq_idx_value is not None:
            return int(self.ftq_idx_value)
        _missing_runtime_binding(self, "resolved_ftq_idx_value", remedy="prepare the backend issue op first, or pass ftq_idx_value explicitly")

    @property
    def resolved_pc(self) -> int:
        if self.pc is not None:
            return int(self.pc)
        _missing_runtime_binding(self, "resolved_pc", remedy="prepare the backend issue op first, or pass pc explicitly")

    @property
    def store_size_bytes(self) -> int:
        return store_size_bytes(opcode=self.store_opcode, mask=self.mask)

    @property
    def store_fu_op_type(self) -> int:
        return store_fu_op_type(opcode=self.store_opcode, mask=self.mask)


@dataclass(frozen=True)
class IssueCyclePlan:
    """One issue batch, with either strict same-cycle or elastic per-lane handshake."""

    ops: tuple[IssueOp, ...]
    max_cycles: int = 50
    handshake_mode: Literal["strict", "elastic"] = "strict"

    def __post_init__(self) -> None:
        if not self.ops:
            raise ValueError("issue cycle plan requires at least one op")
        lanes = [int(op.lane) for op in self.ops]
        if len(set(lanes)) != len(lanes):
            raise ValueError(f"issue cycle plan requires unique lanes: {lanes}")
        if self.handshake_mode not in {"strict", "elastic"}:
            raise ValueError(
                f"unsupported issue cycle handshake mode: {self.handshake_mode!r}; "
                "expected 'strict' or 'elastic'"
            )

    @classmethod
    def from_ops(
        cls,
        *ops: IssueOp,
        max_cycles: int = 50,
        handshake_mode: Literal["strict", "elastic"] = "strict",
    ) -> "IssueCyclePlan":
        return cls(
            ops=tuple(ops),
            max_cycles=max_cycles,
            handshake_mode=handshake_mode,
        )


@dataclass(frozen=True)
class StoreCommitStep:
    """Commit pulse action inside a backend send plan."""

    count: int = 1
    cycles: int = 1


@dataclass(frozen=True)
class NonMemBlockerStep:
    """Inject or release a ROB-side non-mem blocker inside a backend send plan."""

    action: str
    rob_idx_flag: int | None
    rob_idx_value: int | None
    rob_ref: RobRef | None = None
    req_id: int | None = None

    def __post_init__(self) -> None:
        if self.action not in {"insert", "release"}:
            raise ValueError(f"unsupported non-mem blocker action: {self.action}")

    @classmethod
    def insert(
        cls,
        *,
        rob_idx: RobIndex | None = None,
        rob_idx_flag: int | None = None,
        rob_idx_value: int | None = None,
        rob_ref: RobRef | None = None,
        req_id: int | None = None,
    ) -> "NonMemBlockerStep":
        normalized = make_rob_index(
            rob_idx=rob_idx,
            rob_idx_flag=rob_idx_flag,
            rob_idx_value=rob_idx_value,
        )
        return cls(
            action="insert",
            rob_idx_flag=None if normalized is None else normalized.flag,
            rob_idx_value=None if normalized is None else normalized.value,
            rob_ref=rob_ref,
            req_id=req_id,
        )

    @classmethod
    def release(
        cls,
        *,
        rob_idx: RobIndex | None = None,
        rob_idx_flag: int | None = None,
        rob_idx_value: int | None = None,
        rob_ref: RobRef | None = None,
        req_id: int | None = None,
    ) -> "NonMemBlockerStep":
        normalized = make_rob_index(
            rob_idx=rob_idx,
            rob_idx_flag=rob_idx_flag,
            rob_idx_value=rob_idx_value,
        )
        return cls(
            action="release",
            rob_idx_flag=None if normalized is None else normalized.flag,
            rob_idx_value=None if normalized is None else normalized.value,
            rob_ref=rob_ref,
            req_id=req_id,
        )

    @property
    def rob_idx(self) -> RobIndex | None:
        return make_rob_index(rob_idx_flag=self.rob_idx_flag, rob_idx_value=self.rob_idx_value)


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


@dataclass(frozen=True)
class BackendPreparedPlan:
    """Preparation result with resolved ROB metadata and executable plan."""

    resolved_plan: BackendSendPlan
    req_id_robs: dict[int, RobIndex]
    ref_robs: dict[RobRef, RobIndex]

    def rob_idx_of(self, target) -> RobIndex:
        if isinstance(target, RobRef):
            return self.ref_robs[target]
        if hasattr(target, "assigned_rob_idx"):
            assigned_rob_idx = getattr(target, "assigned_rob_idx")
            if assigned_rob_idx is not None:
                return RobIndex(flag=int(assigned_rob_idx.flag), value=int(assigned_rob_idx.value))
        if isinstance(target, int):
            return self.req_id_robs[int(target)]
        req_id = getattr(target, "req_id", None)
        if req_id is not None and int(req_id) in self.req_id_robs:
            return self.req_id_robs[int(req_id)]
        raise KeyError(target)
