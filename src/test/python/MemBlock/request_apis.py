# coding=utf-8
"""
MemBlock 真实 DUT 请求发送公共 API。
"""

from transactions import LoadTxn, QueuePtr, StoreTxn


FU_TYPE_LDU = 1 << 16
FU_TYPE_STU = 1 << 17
LSU_OP_LD = 0x3
LSU_OP_SD = 0x3

DEFAULT_RESET_CYCLES = 20
DEFAULT_BACKEND_RESET_SYNC_CYCLES = 200
DEFAULT_STORE_ENQ_PORT = 0
DEFAULT_LOAD_ISSUE_LANE = 0
DEFAULT_STA_LANE = 3
DEFAULT_STD_LANE = 5


def _set_optional_signal(dut, signal_name: str, value: int) -> None:
    signal = getattr(dut, signal_name, None)
    if signal is not None:
        signal.value = value


def ptr_inc(ptr: QueuePtr, size: int, step: int = 1) -> QueuePtr:
    flag = ptr.flag
    value = ptr.value
    for _ in range(step):
        value += 1
        if value == size:
            value = 0
            flag ^= 0x1
    return QueuePtr(flag=flag, value=value)


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
    env.reset(cycles=reset_cycles, settle_cycles=settle_cycles)
    wait_backend_reset_deassert(env, must_observe_assert=True)

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
) -> None:
    env.backend.enqueue_scalar_load(req_id, lq_ptr, sq_ptr, enq_port=enq_port)


def enqueue_scalar_store(
    env,
    req_id: int,
    sq_ptr: QueuePtr,
    enq_port: int = DEFAULT_STORE_ENQ_PORT,
) -> QueuePtr:
    return env.backend.enqueue_scalar_store(req_id, sq_ptr, enq_port=enq_port)


def send_load(env, txn: LoadTxn) -> None:
    """按标准时序发送一笔标量 load。"""

    env.backend.send_load(txn)


def expect_load(env, txn: LoadTxn):
    """登记一笔 load 事务的期望结果。"""

    return env.expect_scalar_load(
        req_id=txn.req_id,
        pdest=txn.resolved_pdest,
        addr=txn.addr,
        size=txn.size,
        mask=txn.mask,
    )


def send_store(env, txn: StoreTxn) -> QueuePtr:
    """按标准时序发送一笔标量 store。"""

    return env.backend.send_store(txn)


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
    store_set_hit: int = 0,
    load_wait_bit: int = 0,
    load_wait_strict: int = 0,
    wait_for_rob_idx_flag: int | None = None,
    wait_for_rob_idx_value: int | None = None,
) -> None:
    env.backend.issue_scalar_load(
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


def issue_scalar_std(
    env,
    req_id: int,
    sq_ptr: QueuePtr,
    data: int,
    lane: int = DEFAULT_STD_LANE,
) -> None:
    env.backend.issue_scalar_std(req_id, sq_ptr, data, lane=lane)


def issue_scalar_sta(
    env,
    req_id: int,
    sq_ptr: QueuePtr,
    addr: int,
    lane: int = DEFAULT_STA_LANE,
) -> None:
    env.backend.issue_scalar_sta(req_id, sq_ptr, addr, lane=lane)
