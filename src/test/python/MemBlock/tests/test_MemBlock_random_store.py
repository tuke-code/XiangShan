# coding=utf-8
"""
MemBlock 真实 DUT store 冒烟测试。

当前版本先覆盖两条最小真实路径：
  1. 单条 MMIO store: 验证 `STD -> STA -> SQ -> uncache/outer`
  2. 单条 cacheable store: 验证 `STD -> STA -> SQ -> sbuffer -> flushSb`

与 `test_memory_model_store_logic.py` 不同，本文件不直接 mock `MemoryModel`，
而是通过真实 DUT 端口驱动 enqueue / issue，并复用 `MemBlockEnv` 的在线观测口做校验。
"""

from dataclasses import dataclass


FU_TYPE_STU = 1 << 17
LSU_OP_SD = 0x3
RESET_CYCLES = 20
BACKEND_RESET_SYNC_CYCLES = 200
STORE_PIPELINE_SETTLE_CYCLES = 4
STORE_ENQ_PORT = 0
STA_LANE = 3
STD_LANE = 5
MMIO_STORE_ADDR = 0x1000
CACHEABLE_STORE_ADDR = 0x80000008


@dataclass(frozen=True)
class QueuePtr:
    """环形队列指针。"""

    flag: int
    value: int


def _wait_backend_reset_deassert(env, must_observe_assert: bool) -> None:
    observed_assert = not must_observe_assert

    for _ in range(BACKEND_RESET_SYNC_CYCLES):
        backend_reset = int(env.dut.io_reset_backend.value)
        if backend_reset:
            observed_assert = True
        elif observed_assert:
            return
        env.Step(1)

    raise TimeoutError("等待 `io_reset_backend` 解复位超时")


def _reset_env_and_state(env) -> QueuePtr:
    env.reset(cycles=RESET_CYCLES, settle_cycles=1)
    _wait_backend_reset_deassert(env, must_observe_assert=True)

    assert env.dut.reset.value == 0, "解复位后 `reset` 仍为高"
    assert int(env.dut.io_reset_backend.value) == 0, "解复位后 `io_reset_backend` 仍为高"
    assert env.lsq_status.sqCanAccept.value == 1, "解复位后 `sqCanAccept` 未恢复为 1"

    return QueuePtr(flag=0, value=0)


def _wait_lsq_store_enq_ready(env, max_cycles: int = 200) -> None:
    for _ in range(max_cycles):
        if int(env.dut.io_reset_backend.value):
            raise RuntimeError("等待 `enqLsq` ready 时 backend 进入 reset")
        if env.lsq_enq_meta.canAccept.value and env.lsq_status.sqCanAccept.value:
            return
        env.Step(1)
    raise TimeoutError("等待 store `enqLsq` ready 超时")


def _enqueue_scalar_store(env, req_id: int, sq_ptr: QueuePtr) -> QueuePtr:
    _wait_lsq_store_enq_ready(env)

    dut = env.dut
    req = env.lsq_enq_req[STORE_ENQ_PORT]

    env.lsq_enq_meta.need_alloc[STORE_ENQ_PORT].value = 2
    req.valid.value = 1
    req.bits_fuType.value = FU_TYPE_STU
    req.bits_fuOpType.value = LSU_OP_SD
    req.bits_rfWen.value = 0
    req.bits_lastUop.value = 1
    req.bits_pdest.value = 0
    req.bits_robIdx_flag.value = (req_id >> 9) & 0x1
    req.bits_robIdx_value.value = req_id & 0x1FF
    req.bits_lqIdx_flag.value = 0
    req.bits_lqIdx_value.value = 0
    req.bits_sqIdx_flag.value = sq_ptr.flag
    req.bits_sqIdx_value.value = sq_ptr.value
    req.bits_numLsElem.value = 1
    dut.io_ooo_to_mem_enqLsq_req_0_bits_uopIdx.value = 0

    env.Step(1)

    allocated_sq_ptr = QueuePtr(
        flag=int(env.lsq_enq_resp[STORE_ENQ_PORT].sqIdx_flag.value),
        value=int(env.lsq_enq_resp[STORE_ENQ_PORT].sqIdx_value.value),
    )
    env.idle_inputs()
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


def _issue_scalar_std(env, req_id: int, sq_ptr: QueuePtr, data: int, lane: int = STD_LANE) -> None:
    def _drive() -> None:
        issue = env.issue[lane]
        issue.valid.value = 1
        issue.bits_fuType.value = FU_TYPE_STU
        issue.bits_fuOpType.value = LSU_OP_SD
        issue.bits_src_0.value = data
        issue.bits_robIdx_flag.value = (req_id >> 9) & 0x1
        issue.bits_robIdx_value.value = req_id & 0x1FF
        issue.bits_sqIdx_flag.value = sq_ptr.flag
        issue.bits_sqIdx_value.value = sq_ptr.value

    _issue_until_fire(env, lane, _drive)


def _issue_scalar_sta(env, req_id: int, sq_ptr: QueuePtr, addr: int, lane: int = STA_LANE) -> None:
    def _drive() -> None:
        issue = env.issue[lane]
        issue.valid.value = 1
        issue.bits_fuType.value = FU_TYPE_STU
        issue.bits_fuOpType.value = LSU_OP_SD
        issue.bits_src_0.value = addr
        issue.bits_robIdx_flag.value = (req_id >> 9) & 0x1
        issue.bits_robIdx_value.value = req_id & 0x1FF
        issue.bits_sqIdx_flag.value = sq_ptr.flag
        issue.bits_sqIdx_value.value = sq_ptr.value

        env.dut.io_ooo_to_mem_intIssue_3_0_bits_imm.value = 0
        env.dut.io_ooo_to_mem_intIssue_3_0_bits_isFirstIssue.value = 1
        env.dut.io_ooo_to_mem_intIssue_3_0_bits_pdest.value = 0
        env.dut.io_ooo_to_mem_intIssue_3_0_bits_rfWen.value = 0
        env.dut.io_ooo_to_mem_intIssue_3_0_bits_isRVC.value = 0
        env.dut.io_ooo_to_mem_intIssue_3_0_bits_ftqIdx_flag.value = 0
        env.dut.io_ooo_to_mem_intIssue_3_0_bits_ftqIdx_value.value = req_id & 0x3F
        env.dut.io_ooo_to_mem_intIssue_3_0_bits_ftqOffset.value = 0
        env.dut.io_ooo_to_mem_intIssue_3_0_bits_storeSetHit.value = 0
        env.dut.io_ooo_to_mem_intIssue_3_0_bits_ssid.value = 0

    _issue_until_fire(env, lane, _drive)


def _store_data_low64_matches(store, expected_data: int) -> bool:
    if store.data is None:
        return False
    return (int(store.data) & ((1 << 64) - 1)) == (expected_data & ((1 << 64) - 1))


def _wait_pending_store_materialized(
    env,
    sq_ptr: QueuePtr,
    expected_addr: int,
    expected_data: int,
    expected_mmio: bool | None = None,
    require_committed: bool = False,
    max_cycles: int = 200,
):
    for _ in range(max_cycles):
        store = env.memory.pending_stores.get(sq_ptr.value)
        if (
            store is not None
            and store.allocated
            and store.addr == expected_addr
            and _store_data_low64_matches(store, expected_data)
            and store.mask != 0
            and (expected_mmio is None or store.mmio == expected_mmio)
            and (not require_committed or store.committed)
        ):
            return store
        env.Step(1)

    store = env.memory.pending_stores.get(sq_ptr.value)
    raise AssertionError(
        "等待 pending store shadow 超时: "
        f"sqIdx={sq_ptr.value}, store={store}"
    )


def _wait_for_counter_growth(env, attr_name: str, baseline: int, max_cycles: int = 200) -> int:
    for _ in range(max_cycles):
        current = int(getattr(env.memory, attr_name))
        if current > baseline:
            return current
        env.Step(1)
    raise TimeoutError(f"等待 `{attr_name}` 增长超时")


def _wait_for_memory_quiesce(env, max_cycles: int = 200) -> None:
    for _ in range(max_cycles):
        if env.memory.outstanding_transaction_count == 0:
            return
        env.Step(1)
    raise TimeoutError("等待 MemoryModel 事务收敛超时")


def test_api_MemBlock_single_mmio_store_smoke(env):
    """
    单条 MMIO store 真实 DUT 冒烟。

    检查点：
      - store 能通过真实 DUT 的 `enqLsq -> STD -> STA -> SQ` 建立 shadow 元数据
      - PMA 会将 `< 0x80000000` 地址判为 MMIO，并走 outer/uncache 写请求
      - MMIO store 不应经由 sbuffer drain
    """

    req_id = 0
    store_data = 0x1122334455667788

    sq_ptr = _reset_env_and_state(env)
    outer_writes_before = env.memory.outer_write_request_count
    sbuffer_drains_before = env.memory.sbuffer_drain_count

    allocated_sq_ptr = _enqueue_scalar_store(env, req_id, sq_ptr)
    _issue_scalar_std(env, req_id, allocated_sq_ptr, store_data)
    _issue_scalar_sta(env, req_id, allocated_sq_ptr, MMIO_STORE_ADDR)

    store = _wait_pending_store_materialized(
        env,
        allocated_sq_ptr,
        expected_addr=MMIO_STORE_ADDR,
        expected_data=store_data,
        expected_mmio=True,
    )
    _wait_for_counter_growth(env, "outer_write_request_count", outer_writes_before, max_cycles=300)
    env.pulse_store_commit(1)
    env.Step(STORE_PIPELINE_SETTLE_CYCLES)
    _wait_for_memory_quiesce(env, max_cycles=300)

    assert store.mmio, "MMIO store 未被 store shadow 标记为 mmio"
    assert store.addr == MMIO_STORE_ADDR, "MMIO store shadow 地址不匹配"
    assert _store_data_low64_matches(store, store_data), "MMIO store shadow 数据不匹配"
    assert env.memory.sbuffer_drain_count == sbuffer_drains_before, "MMIO store 不应进入 sbuffer"
    assert env.memory.outer_write_request_count > outer_writes_before, "MMIO store 未发出 outer 写请求"
    env.check_no_outstanding_transactions()


def test_api_MemBlock_single_cacheable_store_flush_smoke(env):
    """
    单条 cacheable store 真实 DUT 冒烟。

    检查点：
      - store 被真实 DUT 提交到 sbuffer
      - 结尾显式触发 `flushSb`
      - `drain_log` 与最终 goldenmem 在被覆盖字节上完全一致
    """

    req_id = 0
    store_data = 0xAABBCCDDEEFF0011

    sq_ptr = _reset_env_and_state(env)
    outer_writes_before = env.memory.outer_write_request_count
    sbuffer_drains_before = env.memory.sbuffer_drain_count

    allocated_sq_ptr = _enqueue_scalar_store(env, req_id, sq_ptr)
    _issue_scalar_std(env, req_id, allocated_sq_ptr, store_data)
    _issue_scalar_sta(env, req_id, allocated_sq_ptr, CACHEABLE_STORE_ADDR)

    store = _wait_pending_store_materialized(
        env,
        allocated_sq_ptr,
        expected_addr=CACHEABLE_STORE_ADDR,
        expected_data=store_data,
        expected_mmio=False,
        require_committed=True,
        max_cycles=300,
    )
    _wait_for_counter_growth(env, "sbuffer_drain_count", sbuffer_drains_before, max_cycles=300)

    drain_summary = env.flush_store_buffers_and_wait(max_cycles=400, settle_cycles=STORE_PIPELINE_SETTLE_CYCLES)

    assert not store.mmio, "cacheable store 被错误标记为 mmio"
    assert store.addr == CACHEABLE_STORE_ADDR, "cacheable store shadow 地址不匹配"
    assert _store_data_low64_matches(store, store_data), "cacheable store shadow 数据不匹配"
    assert env.memory.sbuffer_drain_count > sbuffer_drains_before, "cacheable store 未进入 sbuffer"
    assert env.memory.outer_write_request_count == outer_writes_before, "cacheable store 不应走 outer/mmio 写路径"
    assert drain_summary["drain_event_count"] > 0, "flushSb 后未记录到任何 drain 事件"
    assert drain_summary["touched_byte_count"] >= 8, "drain 覆盖字节数异常"
    env.check_no_outstanding_transactions()
