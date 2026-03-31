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
    observed_assert = not must_observe_assert

    for _ in range(max_cycles):
        backend_reset = int(env.dut.io_reset_backend.value)
        if backend_reset:
            observed_assert = True
        elif observed_assert:
            return
        env.Step(1)

    raise TimeoutError("等待 `io_reset_backend` 解复位超时")


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
    for _ in range(max_cycles):
        if int(env.dut.io_reset_backend.value):
            raise RuntimeError("等待 load `enqLsq` ready 时 backend 进入 reset")
        if env.lsq_enq_meta.canAccept.value and env.lsq_status.lqCanAccept.value:
            return
        env.Step(1)
    raise TimeoutError("等待 load `enqLsq` ready 超时")


def wait_lsq_store_enq_ready(env, max_cycles: int = 200) -> None:
    for _ in range(max_cycles):
        if int(env.dut.io_reset_backend.value):
            raise RuntimeError("等待 store `enqLsq` ready 时 backend 进入 reset")
        if env.lsq_enq_meta.canAccept.value and env.lsq_status.sqCanAccept.value:
            return
        env.Step(1)
    raise TimeoutError("等待 store `enqLsq` ready 超时")


def enqueue_scalar_load(
    env,
    req_id: int,
    lq_ptr: QueuePtr,
    sq_ptr: QueuePtr,
    enq_port: int = DEFAULT_STORE_ENQ_PORT,
) -> None:
    wait_lsq_load_enq_ready(env)

    req = env.lsq_enq_req[enq_port]
    env.lsq_enq_meta.need_alloc[enq_port].value = 1
    req.valid.value = 1
    req.bits_fuType.value = FU_TYPE_LDU
    req.bits_uopIdx.value = req_id & 0x7F
    req.bits_robIdx_flag.value = (req_id >> 9) & 0x1
    req.bits_robIdx_value.value = req_id & 0x1FF
    req.bits_lqIdx_flag.value = lq_ptr.flag
    req.bits_lqIdx_value.value = lq_ptr.value
    req.bits_sqIdx_flag.value = sq_ptr.flag
    req.bits_sqIdx_value.value = sq_ptr.value
    req.bits_numLsElem.value = 1

    env.Step(1)
    env.idle_inputs()


def enqueue_scalar_store(
    env,
    req_id: int,
    sq_ptr: QueuePtr,
    enq_port: int = DEFAULT_STORE_ENQ_PORT,
) -> QueuePtr:
    wait_lsq_store_enq_ready(env)

    req = env.lsq_enq_req[enq_port]
    env.lsq_enq_meta.need_alloc[enq_port].value = 2
    req.valid.value = 1
    req.bits_fuType.value = FU_TYPE_STU
    req.bits_uopIdx.value = req_id & 0x7F
    req.bits_robIdx_flag.value = (req_id >> 9) & 0x1
    req.bits_robIdx_value.value = req_id & 0x1FF
    req.bits_lqIdx_flag.value = 0
    req.bits_lqIdx_value.value = 0
    req.bits_sqIdx_flag.value = sq_ptr.flag
    req.bits_sqIdx_value.value = sq_ptr.value
    req.bits_numLsElem.value = 1

    env.Step(1)
    allocated_sq_ptr = QueuePtr(
        flag=int(env.lsq_enq_resp[enq_port].sqIdx_flag.value),
        value=int(env.lsq_enq_resp[enq_port].sqIdx_value.value),
    )
    env.note_store_allocated(
        sq_idx_flag=allocated_sq_ptr.flag,
        sq_idx_value=allocated_sq_ptr.value,
        rob_idx_flag=(req_id >> 9) & 0x1,
        rob_idx_value=req_id & 0x1FF,
    )
    env.idle_inputs()
    return allocated_sq_ptr


def send_load(env, txn: LoadTxn) -> None:
    """按标准时序发送一笔标量 load。"""

    enqueue_scalar_load(
        env,
        req_id=txn.req_id,
        lq_ptr=txn.lq_ptr,
        sq_ptr=txn.sq_ptr,
        enq_port=txn.enq_port,
    )
    issue_scalar_load(
        env,
        req_id=txn.req_id,
        addr=txn.addr,
        lq_ptr=txn.lq_ptr,
        sq_ptr=txn.sq_ptr,
        lane=txn.issue_lane,
    )


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

    allocated_sq_ptr = enqueue_scalar_store(
        env,
        req_id=txn.req_id,
        sq_ptr=txn.sq_ptr,
        enq_port=txn.enq_port,
    )
    issue_scalar_std(
        env,
        req_id=txn.req_id,
        sq_ptr=allocated_sq_ptr,
        data=txn.data,
        lane=txn.std_lane,
    )
    issue_scalar_sta(
        env,
        req_id=txn.req_id,
        sq_ptr=allocated_sq_ptr,
        addr=txn.addr,
        lane=txn.sta_lane,
    )
    return allocated_sq_ptr


def _issue_until_fire(env, lane: int, drive_inputs, max_cycles: int = 50) -> None:
    issue = env.issue[lane]

    for _ in range(max_cycles):
        if int(env.dut.io_reset_backend.value):
            raise RuntimeError(f"等待 `issue[{lane}]` 握手时 backend 进入 reset")
        drive_inputs()
        if int(issue.ready.value):
            env.Step(1)
            env.idle_inputs()
            return
        env.Step(1)

    env.idle_inputs()
    raise TimeoutError(f"等待 `issue[{lane}]` 完成握手超时")


def issue_scalar_load(
    env,
    req_id: int,
    addr: int,
    lq_ptr: QueuePtr,
    sq_ptr: QueuePtr,
    lane: int = DEFAULT_LOAD_ISSUE_LANE,
) -> None:
    def _drive() -> None:
        issue = env.issue[lane]
        prefix = f"io_ooo_to_mem_intIssue_{lane}_0_bits_"
        issue.valid.value = 1
        issue.bits_fuOpType.value = LSU_OP_LD
        issue.bits_src_0.value = addr
        issue.bits_robIdx_flag.value = (req_id >> 9) & 0x1
        issue.bits_robIdx_value.value = req_id & 0x1FF
        issue.bits_sqIdx_flag.value = sq_ptr.flag
        issue.bits_sqIdx_value.value = sq_ptr.value

        _set_optional_signal(env.dut, f"{prefix}imm", 0)
        _set_optional_signal(env.dut, f"{prefix}pdest", req_id % 64)
        _set_optional_signal(env.dut, f"{prefix}rfWen", 1)
        _set_optional_signal(env.dut, f"{prefix}pc", 0x80000000 + req_id * 4)
        _set_optional_signal(env.dut, f"{prefix}ftqIdx_flag", 0)
        _set_optional_signal(env.dut, f"{prefix}ftqIdx_value", req_id & 0x3F)
        _set_optional_signal(env.dut, f"{prefix}ftqOffset", 0)
        _set_optional_signal(env.dut, f"{prefix}loadWaitBit", 0)
        _set_optional_signal(env.dut, f"{prefix}waitForRobIdx_flag", 0)
        _set_optional_signal(env.dut, f"{prefix}waitForRobIdx_value", 0)
        _set_optional_signal(env.dut, f"{prefix}storeSetHit", 0)
        _set_optional_signal(env.dut, f"{prefix}loadWaitStrict", 0)
        _set_optional_signal(env.dut, f"{prefix}ssid", 0)
        _set_optional_signal(env.dut, f"{prefix}lqIdx_flag", lq_ptr.flag)
        _set_optional_signal(env.dut, f"{prefix}lqIdx_value", lq_ptr.value)

    _issue_until_fire(env, lane, _drive)
    env.note_load_issued((req_id >> 9) & 0x1, req_id & 0x1FF)


def issue_scalar_std(
    env,
    req_id: int,
    sq_ptr: QueuePtr,
    data: int,
    lane: int = DEFAULT_STD_LANE,
) -> None:
    def _drive() -> None:
        issue = env.issue[lane]
        issue.valid.value = 1
        issue.bits_fuOpType.value = LSU_OP_SD
        issue.bits_src_0.value = data
        issue.bits_robIdx_flag.value = (req_id >> 9) & 0x1
        issue.bits_robIdx_value.value = req_id & 0x1FF
        issue.bits_sqIdx_flag.value = sq_ptr.flag
        issue.bits_sqIdx_value.value = sq_ptr.value

    _issue_until_fire(env, lane, _drive)


def issue_scalar_sta(
    env,
    req_id: int,
    sq_ptr: QueuePtr,
    addr: int,
    lane: int = DEFAULT_STA_LANE,
) -> None:
    def _drive() -> None:
        issue = env.issue[lane]
        prefix = f"io_ooo_to_mem_intIssue_{lane}_0_bits_"
        issue.valid.value = 1
        issue.bits_fuOpType.value = LSU_OP_SD
        issue.bits_src_0.value = addr
        issue.bits_robIdx_flag.value = (req_id >> 9) & 0x1
        issue.bits_robIdx_value.value = req_id & 0x1FF
        issue.bits_sqIdx_flag.value = sq_ptr.flag
        issue.bits_sqIdx_value.value = sq_ptr.value

        _set_optional_signal(env.dut, f"{prefix}imm", 0)
        _set_optional_signal(env.dut, f"{prefix}isFirstIssue", 1)
        _set_optional_signal(env.dut, f"{prefix}pdest", 0)
        _set_optional_signal(env.dut, f"{prefix}rfWen", 0)
        _set_optional_signal(env.dut, f"{prefix}isRVC", 0)
        _set_optional_signal(env.dut, f"{prefix}ftqIdx_flag", 0)
        _set_optional_signal(env.dut, f"{prefix}ftqIdx_value", req_id & 0x3F)
        _set_optional_signal(env.dut, f"{prefix}ftqOffset", 0)
        _set_optional_signal(env.dut, f"{prefix}storeSetHit", 0)
        _set_optional_signal(env.dut, f"{prefix}ssid", 0)

    _issue_until_fire(env, lane, _drive)
