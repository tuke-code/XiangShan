# coding=utf-8
"""
MemBlock backend primitive adapter / compatibility shim.

This module is intentionally narrow:
  1. thin backend-facing primitives used by reusable sequences
  2. reset/sync helpers that operate below scenario level
  3. transition wrappers kept for backward compatibility

New testcase authoring should prefer `sequences/` over composing flows here.
"""

from transactions import (
    AtomicTxn,
    BackendSendPlan,
    EnqueueLoadCyclePlan,
    EnqueueLoadStep,
    IssueCyclePlan,
    IssueOp,
    LoadTxn,
    ptr_inc,
    QueuePtr,
    RobIndex,
    StoreTxn,
    VectorMemTxn,
)


FU_TYPE_LDU = 1 << 16
FU_TYPE_STU = 1 << 17
LSU_OP_SD = 0x3

DEFAULT_RESET_CYCLES = 20
DEFAULT_BACKEND_RESET_SYNC_CYCLES = 200
DEFAULT_STORE_ENQ_PORT = 0
DEFAULT_LOAD_ISSUE_LANE = 0
DEFAULT_STA_LANE = 3
DEFAULT_STD_LANE = 5

__all__ = [
    "BackendSendPlan",
    "AtomicTxn",
    "EnqueueLoadCyclePlan",
    "EnqueueLoadStep",
    "IssueCyclePlan",
    "IssueOp",
    "LoadTxn",
    "ptr_inc",
    "QueuePtr",
    "RobIndex",
    "StoreTxn",
    "VectorMemTxn",
    "wait_backend_reset_deassert",
    "reset_env_and_wait_backend",
    "wait_lsq_load_enq_ready",
    "wait_lsq_store_enq_ready",
    "enqueue_scalar_load",
    "enqueue_scalar_store",
    "send_load",
    "send_load_batch_same_cycle",
    "send_load_batch_with_sta_same_cycle",
    "expect_load",
    "send_store",
    "send_atomic",
    "send_cbo",
    "send_cbo_zero",
    "send_vector_load",
    "send_vector_store",
    "issue_scalar_load",
    "issue_scalar_std",
    "issue_scalar_sta",
    "issue_atomic_std",
    "issue_atomic_sta",
]


def _split_rob_idx(rob_idx: RobIndex | None, rob_idx_flag: int | None, rob_idx_value: int | None) -> tuple[int | None, int | None]:
    if rob_idx is not None:
        return int(rob_idx.flag), int(rob_idx.value)
    return rob_idx_flag, rob_idx_value


def _set_optional_signal(dut, signal_name: str, value: int) -> None:
    signal = getattr(dut, signal_name, None)
    if signal is not None:
        signal.value = value

def wait_backend_reset_deassert(
    env,
    must_observe_assert: bool,
    max_cycles: int = DEFAULT_BACKEND_RESET_SYNC_CYCLES,
) -> None:
    env.wait_backend_reset_deassert(
        must_observe_assert=must_observe_assert,
        max_cycles=max_cycles,
    )


def reset_env_and_wait_backend(
    env,
    reset_cycles: int = DEFAULT_RESET_CYCLES,
    settle_cycles: int = 1,
    require_issue_lanes=(),
    require_lq_ready: bool = False,
    require_sq_ready: bool = False,
) -> None:
    env.reset(cycles=reset_cycles, settle_cycles=0)
    wait_backend_reset_deassert(env, must_observe_assert=True)
    if settle_cycles > 0:
        env.advance_cycles(settle_cycles)

    assert env.dut.reset.value == 0, "解复位后 `reset` 仍为高"
    assert int(env.dut.io_reset_backend.value) == 0, "解复位后 `io_reset_backend` 仍为高"

    for lane in require_issue_lanes:
        assert env.issue[lane].ready.value == 1, f"解复位后 `issue[{lane}]` 未恢复 ready"
    if require_lq_ready:
        assert env.lsq_status.lqCanAccept.value == 1, "解复位后 `lqCanAccept` 未恢复为 1"
    if require_sq_ready:
        assert env.lsq_status.sqCanAccept.value == 1, "解复位后 `sqCanAccept` 未恢复为 1"


def wait_lsq_load_enq_ready(env, max_cycles: int = 200) -> None:
    env.backend.wait_load_enq_ready(max_cycles=max_cycles)


def wait_lsq_store_enq_ready(env, max_cycles: int = 200) -> None:
    env.backend.wait_store_enq_ready(max_cycles=max_cycles)


def enqueue_scalar_load(
    env,
    req_id: int,
    lq_ptr: QueuePtr,
    sq_ptr: QueuePtr,
    enq_port: int = DEFAULT_STORE_ENQ_PORT,
    rob_idx: RobIndex | None = None,
    rob_idx_flag: int | None = None,
    rob_idx_value: int | None = None,
) -> None:
    normalized_flag, normalized_value = _split_rob_idx(rob_idx, rob_idx_flag, rob_idx_value)
    try:
        kwargs = {"enq_port": enq_port, "rob_idx_flag": rob_idx_flag, "rob_idx_value": rob_idx_value}
        if rob_idx is not None:
            kwargs["rob_idx"] = rob_idx
        env.backend.enqueue_scalar_load(
            req_id,
            lq_ptr,
            sq_ptr,
            **kwargs,
        )
    except TypeError as exc:
        if "rob_idx" not in str(exc):
            raise
        env.backend.enqueue_scalar_load(
            req_id,
            lq_ptr,
            sq_ptr,
            enq_port=enq_port,
            rob_idx_flag=normalized_flag,
            rob_idx_value=normalized_value,
        )


def enqueue_scalar_store(
    env,
    req_id: int,
    sq_ptr: QueuePtr,
    enq_port: int = DEFAULT_STORE_ENQ_PORT,
    rob_idx: RobIndex | None = None,
    rob_idx_flag: int | None = None,
    rob_idx_value: int | None = None,
) -> QueuePtr:
    normalized_flag, normalized_value = _split_rob_idx(rob_idx, rob_idx_flag, rob_idx_value)
    try:
        kwargs = {"enq_port": enq_port, "rob_idx_flag": rob_idx_flag, "rob_idx_value": rob_idx_value}
        if rob_idx is not None:
            kwargs["rob_idx"] = rob_idx
        return env.backend.enqueue_scalar_store(
            req_id,
            sq_ptr,
            **kwargs,
        )
    except TypeError as exc:
        if "rob_idx" not in str(exc):
            raise
        return env.backend.enqueue_scalar_store(
            req_id,
            sq_ptr,
            enq_port=enq_port,
            rob_idx_flag=normalized_flag,
            rob_idx_value=normalized_value,
        )


def send_load(env, txn: LoadTxn) -> None:
    """兼容入口：按标准时序发送一笔标量 load。"""

    env.backend.send(txn)


def send_load_batch_same_cycle(env, txns, max_cycles: int = 50) -> None:
    """兼容入口：enqueue 多笔标量 load，并按 strict 模式在同一拍完成 issue。

    Prefer `sequences.ScalarLoadBatchSameCycleSequence` for new testcase authoring.
    """

    txns = tuple(txns)
    env.backend.execute(
        BackendSendPlan.from_steps(
            EnqueueLoadCyclePlan.from_txns(*txns),
            IssueCyclePlan(
                ops=tuple(IssueOp.load_from_txn(txn) for txn in txns),
                max_cycles=max_cycles,
            ),
        )
    )


def send_load_batch_with_sta_same_cycle(
    env,
    txns,
    *,
    sta_req_id: int,
    sta_sq_ptr,
    sta_addr: int,
    sta_lane: int = DEFAULT_STA_LANE,
    sta_mask: int = 0xFF,
    max_cycles: int = 50,
) -> None:
    """兼容入口：enqueue 多笔标量 load，并与一条 `STA` 按 strict 模式在同一拍完成 issue。

    Prefer `sequences.ScalarLoadBatchWithStaSequence` for new testcase authoring.
    """

    txns = tuple(txns)
    env.backend.execute(
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
                        mask=sta_mask,
                    ),
                ),
                max_cycles=max_cycles,
            ),
        )
    )


def expect_load(env, txn: LoadTxn):
    """登记一笔 load 事务的期望结果。"""

    if txn.assigned_rob_idx is None:
        env.backend.prepare(txn)
    return env.expect_scalar_load(
        rob_idx=txn.rob_idx,
        pdest=txn.resolved_pdest,
        addr=txn.addr,
        size=txn.size,
        mask=txn.mask,
        fp_wen=txn.fp_wen,
    )


def send_store(env, txn: StoreTxn) -> QueuePtr:
    """兼容入口：按标准时序发送一笔标量 store。"""

    return env.backend.send(txn)


def send_atomic(env, txn: AtomicTxn) -> None:
    """兼容入口：发送一笔 single-uop AMO。"""

    send_fn = getattr(env.backend, "send_atomic", None)
    if send_fn is not None:
        return send_fn(txn)
    return env.backend.send(txn)


def send_cbo(env, txn: StoreTxn) -> QueuePtr:
    """发送一笔 CBO store。"""

    if not txn.is_cbo:
        raise ValueError("send_cbo requires a StoreTxn with a CBO opcode")
    send_fn = getattr(env.backend, "send_cbo", None)
    if send_fn is not None:
        return send_fn(txn)
    return env.backend.send(txn)


def send_cbo_zero(env, txn: StoreTxn) -> QueuePtr:
    """发送一笔 `cbo.zero` store。"""

    if not txn.is_cbo_zero:
        raise ValueError("send_cbo_zero requires a StoreTxn with opcode='cbo_zero'")
    return send_cbo(env, txn)


def send_vector_load(env, txn: VectorMemTxn):
    """兼容入口：按标准时序发送一笔向量 load。"""

    if not txn.is_load:
        raise ValueError("send_vector_load requires a vector load transaction")
    return env.vector_backend.send(txn)


def send_vector_store(env, txn: VectorMemTxn):
    """兼容入口：按标准时序发送一笔向量 store。"""

    if txn.is_load:
        raise ValueError("send_vector_store requires a vector store transaction")
    return env.vector_backend.send(txn)


def _issue_until_fire(env, lane: int, drive_inputs, max_cycles: int = 50) -> None:
    del env, lane, drive_inputs, max_cycles
    raise NotImplementedError("_issue_until_fire 已下沉到 IssueAgent")


def issue_scalar_load(
    env,
    req_id: int,
    addr: int,
    lq_ptr: QueuePtr,
    sq_ptr: QueuePtr,
    lane: int = DEFAULT_LOAD_ISSUE_LANE,
    size: int = 8,
    mask: int | None = None,
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
    normalized_flag, normalized_value = _split_rob_idx(rob_idx, rob_idx_flag, rob_idx_value)
    wait_flag, wait_value = _split_rob_idx(wait_for_rob_idx, wait_for_rob_idx_flag, wait_for_rob_idx_value)
    try:
        kwargs = {
            "lane": lane,
            "size": size,
            "mask": mask,
            "fp_wen": fp_wen,
            "store_set_hit": store_set_hit,
            "load_wait_bit": load_wait_bit,
            "load_wait_strict": load_wait_strict,
            "wait_for_rob_idx_flag": wait_for_rob_idx_flag,
            "wait_for_rob_idx_value": wait_for_rob_idx_value,
            "rob_idx_flag": rob_idx_flag,
            "rob_idx_value": rob_idx_value,
            "pdest": pdest,
            "ftq_idx_flag": ftq_idx_flag,
            "ftq_idx_value": ftq_idx_value,
            "pc": pc,
        }
        if wait_for_rob_idx is not None:
            kwargs["wait_for_rob_idx"] = wait_for_rob_idx
        if rob_idx is not None:
            kwargs["rob_idx"] = rob_idx
        env.backend.issue_scalar_load(
            req_id,
            addr,
            lq_ptr,
            sq_ptr,
            **kwargs,
        )
    except TypeError as exc:
        if "rob_idx" not in str(exc):
            raise
        env.backend.issue_scalar_load(
            req_id,
            addr,
            lq_ptr,
            sq_ptr,
            lane=lane,
            size=size,
            mask=mask,
            fp_wen=fp_wen,
            store_set_hit=store_set_hit,
            load_wait_bit=load_wait_bit,
            load_wait_strict=load_wait_strict,
            wait_for_rob_idx_flag=wait_flag,
            wait_for_rob_idx_value=wait_value,
            rob_idx_flag=normalized_flag,
            rob_idx_value=normalized_value,
            pdest=pdest,
            ftq_idx_flag=ftq_idx_flag,
            ftq_idx_value=ftq_idx_value,
            pc=pc,
        )


def issue_scalar_std(
    env,
    req_id: int,
    sq_ptr: QueuePtr,
    data: int,
    lane: int = DEFAULT_STD_LANE,
    mask: int = 0xFF,
    rob_idx: RobIndex | None = None,
    rob_idx_flag: int | None = None,
    rob_idx_value: int | None = None,
    ftq_idx_flag: int | None = None,
    ftq_idx_value: int | None = None,
) -> None:
    normalized_flag, normalized_value = _split_rob_idx(rob_idx, rob_idx_flag, rob_idx_value)
    try:
        kwargs = {
            "lane": lane,
            "mask": mask,
            "rob_idx_flag": rob_idx_flag,
            "rob_idx_value": rob_idx_value,
            "ftq_idx_flag": ftq_idx_flag,
            "ftq_idx_value": ftq_idx_value,
        }
        if rob_idx is not None:
            kwargs["rob_idx"] = rob_idx
        env.backend.issue_scalar_std(
            req_id,
            sq_ptr,
            data,
            **kwargs,
        )
    except TypeError as exc:
        if "rob_idx" not in str(exc):
            raise
        env.backend.issue_scalar_std(
            req_id,
            sq_ptr,
            data,
            lane=lane,
            mask=mask,
            rob_idx_flag=normalized_flag,
            rob_idx_value=normalized_value,
            ftq_idx_flag=ftq_idx_flag,
            ftq_idx_value=ftq_idx_value,
        )


def issue_scalar_sta(
    env,
    req_id: int,
    sq_ptr: QueuePtr,
    addr: int,
    lane: int = DEFAULT_STA_LANE,
    mask: int = 0xFF,
    rob_idx: RobIndex | None = None,
    rob_idx_flag: int | None = None,
    rob_idx_value: int | None = None,
    ftq_idx_flag: int | None = None,
    ftq_idx_value: int | None = None,
) -> None:
    normalized_flag, normalized_value = _split_rob_idx(rob_idx, rob_idx_flag, rob_idx_value)
    try:
        kwargs = {
            "lane": lane,
            "mask": mask,
            "rob_idx_flag": rob_idx_flag,
            "rob_idx_value": rob_idx_value,
            "ftq_idx_flag": ftq_idx_flag,
            "ftq_idx_value": ftq_idx_value,
        }
        if rob_idx is not None:
            kwargs["rob_idx"] = rob_idx
        env.backend.issue_scalar_sta(
            req_id,
            sq_ptr,
            addr,
            **kwargs,
        )
    except TypeError as exc:
        if "rob_idx" not in str(exc):
            raise
        env.backend.issue_scalar_sta(
            req_id,
            sq_ptr,
            addr,
            lane=lane,
            mask=mask,
            rob_idx_flag=normalized_flag,
            rob_idx_value=normalized_value,
            ftq_idx_flag=ftq_idx_flag,
            ftq_idx_value=ftq_idx_value,
        )


def issue_atomic_sta(env, txn: AtomicTxn) -> None:
    """兼容入口：仅发送 AMO 的 STA 半拍。"""

    issue_fn = getattr(env.backend, "issue_atomic_sta", None)
    if issue_fn is not None:
        return issue_fn(txn)
    raise AttributeError("env.backend does not expose issue_atomic_sta")


def issue_atomic_std(env, txn: AtomicTxn) -> None:
    """兼容入口：仅发送 AMO 的 STD 半拍。"""

    issue_fn = getattr(env.backend, "issue_atomic_std", None)
    if issue_fn is not None:
        return issue_fn(txn)
    raise AttributeError("env.backend does not expose issue_atomic_std")
