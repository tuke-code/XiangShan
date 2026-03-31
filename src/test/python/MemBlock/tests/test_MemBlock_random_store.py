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
import random


FU_TYPE_LDU = 1 << 16
FU_TYPE_STU = 1 << 17
LSU_OP_LD = 0x3
LSU_OP_SD = 0x3
RESET_CYCLES = 20
BACKEND_RESET_SYNC_CYCLES = 200
STORE_PIPELINE_SETTLE_CYCLES = 4
PER_REQUEST_DRAIN_CYCLES = 400
STORE_ENQ_PORT = 0
LOAD_ISSUE_LANE = 0
STA_LANE = 3
STD_LANE = 5
VIRTUAL_LOAD_QUEUE_SIZE = 72
VIRTUAL_STORE_QUEUE_SIZE = 56
MMIO_STORE_ADDR = 0x1000
CACHEABLE_STORE_ADDR = 0x80000008
RANDOM_MIXED_SEED = 20260331
RANDOM_MIXED_OPS = 12
RANDOM_ADDR_POOL = [
    CACHEABLE_STORE_ADDR,
    CACHEABLE_STORE_ADDR + 0x8,
    CACHEABLE_STORE_ADDR + 0x10,
    CACHEABLE_STORE_ADDR + 0x18,
]


@dataclass(frozen=True)
class QueuePtr:
    """环形队列指针。"""

    flag: int
    value: int


def _ptr_inc(ptr: QueuePtr, size: int, step: int = 1) -> QueuePtr:
    flag = ptr.flag
    value = ptr.value
    for _ in range(step):
        value += 1
        if value == size:
            value = 0
            flag ^= 0x1
    return QueuePtr(flag=flag, value=value)


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


def _wait_lsq_load_enq_ready(env, max_cycles: int = 200) -> None:
    for _ in range(max_cycles):
        if int(env.dut.io_reset_backend.value):
            raise RuntimeError("等待 load `enqLsq` ready 时 backend 进入 reset")
        if env.lsq_enq_meta.canAccept.value and env.lsq_status.lqCanAccept.value:
            return
        env.Step(1)
    raise TimeoutError("等待 load `enqLsq` ready 超时")


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


def _enqueue_scalar_load(env, req_id: int, lq_ptr: QueuePtr, sq_ptr: QueuePtr) -> None:
    _wait_lsq_load_enq_ready(env)

    dut = env.dut
    req = env.lsq_enq_req[STORE_ENQ_PORT]

    env.lsq_enq_meta.need_alloc[STORE_ENQ_PORT].value = 1
    req.valid.value = 1
    req.bits_fuType.value = FU_TYPE_LDU
    req.bits_fuOpType.value = LSU_OP_LD
    req.bits_rfWen.value = 1
    req.bits_lastUop.value = 1
    req.bits_pdest.value = req_id % 64
    req.bits_robIdx_flag.value = (req_id >> 9) & 0x1
    req.bits_robIdx_value.value = req_id & 0x1FF
    req.bits_lqIdx_flag.value = lq_ptr.flag
    req.bits_lqIdx_value.value = lq_ptr.value
    req.bits_sqIdx_flag.value = sq_ptr.flag
    req.bits_sqIdx_value.value = sq_ptr.value
    req.bits_numLsElem.value = 1
    dut.io_ooo_to_mem_enqLsq_req_0_bits_uopIdx.value = 0

    env.Step(1)
    env.idle_inputs()


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


def _issue_scalar_load(env, req_id: int, lq_ptr: QueuePtr, sq_ptr: QueuePtr, addr: int, lane: int = LOAD_ISSUE_LANE) -> None:
    def _drive() -> None:
        issue = env.issue[lane]
        issue.valid.value = 1
        issue.bits_fuType.value = FU_TYPE_LDU
        issue.bits_fuOpType.value = LSU_OP_LD
        issue.bits_src_0.value = addr
        issue.bits_robIdx_flag.value = (req_id >> 9) & 0x1
        issue.bits_robIdx_value.value = req_id & 0x1FF
        issue.bits_sqIdx_flag.value = sq_ptr.flag
        issue.bits_sqIdx_value.value = sq_ptr.value

        env.dut.io_ooo_to_mem_intIssue_0_0_bits_imm.value = 0
        env.dut.io_ooo_to_mem_intIssue_0_0_bits_pdest.value = req_id % 64
        env.dut.io_ooo_to_mem_intIssue_0_0_bits_rfWen.value = 1
        env.dut.io_ooo_to_mem_intIssue_0_0_bits_pc.value = 0x80000000 + req_id * 4
        env.dut.io_ooo_to_mem_intIssue_0_0_bits_ftqIdx_flag.value = 0
        env.dut.io_ooo_to_mem_intIssue_0_0_bits_ftqIdx_value.value = req_id & 0x3F
        env.dut.io_ooo_to_mem_intIssue_0_0_bits_ftqOffset.value = 0
        env.dut.io_ooo_to_mem_intIssue_0_0_bits_loadWaitBit.value = 0
        env.dut.io_ooo_to_mem_intIssue_0_0_bits_waitForRobIdx_flag.value = 0
        env.dut.io_ooo_to_mem_intIssue_0_0_bits_waitForRobIdx_value.value = 0
        env.dut.io_ooo_to_mem_intIssue_0_0_bits_storeSetHit.value = 0
        env.dut.io_ooo_to_mem_intIssue_0_0_bits_loadWaitStrict.value = 0
        env.dut.io_ooo_to_mem_intIssue_0_0_bits_ssid.value = 0
        env.dut.io_ooo_to_mem_intIssue_0_0_bits_lqIdx_flag.value = lq_ptr.flag
        env.dut.io_ooo_to_mem_intIssue_0_0_bits_lqIdx_value.value = lq_ptr.value

    _issue_until_fire(env, lane, _drive)
    env.note_load_issued((req_id >> 9) & 0x1, req_id & 0x1FF)


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


def _issue_and_check_scalar_load(
    env,
    req_id: int,
    lq_ptr: QueuePtr,
    sq_ptr: QueuePtr,
    addr: int,
    expected_completed_loads: int,
) -> QueuePtr:
    _enqueue_scalar_load(env, req_id, lq_ptr, sq_ptr)
    _issue_scalar_load(env, req_id, lq_ptr, sq_ptr, addr)
    env.memory.expect_load(
        rob_idx_flag=(req_id >> 9) & 0x1,
        rob_idx_value=req_id & 0x1FF,
        pdest=req_id % 64,
        addr=addr,
        size=8,
        mask=0xFF,
    )
    env.drain_writebacks(max_cycles=PER_REQUEST_DRAIN_CYCLES)
    assert env.memory.completed_loads == expected_completed_loads, (
        f"load req_id={req_id} 未在限定周期内完成"
    )
    return _ptr_inc(lq_ptr, VIRTUAL_LOAD_QUEUE_SIZE)


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


def test_api_MemBlock_single_cacheable_store_then_load_same_addr(env):
    """
    单条 cacheable store 后接同地址 load。

    检查点：
      - older store 已 materialize 并处于 committed 状态
      - younger load 通过真实 DUT 完成返回
      - load compare 会在 commit-boundary 视图上看到该 store 的写入值
    """

    store_req_id = 0
    load_req_id = 1
    store_data = 0x8877665544332211

    sq_ptr = _reset_env_and_state(env)
    lq_ptr = QueuePtr(flag=0, value=0)

    allocated_sq_ptr = _enqueue_scalar_store(env, store_req_id, sq_ptr)
    _issue_scalar_std(env, store_req_id, allocated_sq_ptr, store_data)
    _issue_scalar_sta(env, store_req_id, allocated_sq_ptr, CACHEABLE_STORE_ADDR)

    store = _wait_pending_store_materialized(
        env,
        allocated_sq_ptr,
        expected_addr=CACHEABLE_STORE_ADDR,
        expected_data=store_data,
        expected_mmio=False,
        require_committed=True,
        max_cycles=300,
    )

    lq_ptr = _issue_and_check_scalar_load(
        env,
        req_id=load_req_id,
        lq_ptr=lq_ptr,
        sq_ptr=_ptr_inc(sq_ptr, VIRTUAL_STORE_QUEUE_SIZE),
        addr=CACHEABLE_STORE_ADDR,
        expected_completed_loads=1,
    )

    assert store.committed, "older store 未进入 committed 状态"
    assert store.addr == CACHEABLE_STORE_ADDR, "store shadow 地址不匹配"
    assert _store_data_low64_matches(store, store_data), "store shadow 数据不匹配"
    assert lq_ptr.value == 1, "load queue 指针未按预期前移"
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


def test_api_MemBlock_small_mixed_load_store_random(env):
    """
    小规模真实 DUT mixed load/store random。

    策略：
      - 地址池限定在 cacheable 空间
      - store 使用固定 `robIdx=0`，保证可及时进入 committed
      - load 使用递增 `robIdx`，逐条完成在线 compare
      - 结尾统一 `flushSb`，检查最终 drain 与 goldenmem 一致
    """

    rng = random.Random(RANDOM_MIXED_SEED)
    sq_ptr = _reset_env_and_state(env)
    lq_ptr = QueuePtr(flag=0, value=0)
    next_load_req_id = 1
    total_stores = 0
    total_loads = 0

    for addr in RANDOM_ADDR_POOL:
        env.memory.preload_u64(addr, rng.getrandbits(64))

    operations = [
        ("store", RANDOM_ADDR_POOL[0], rng.getrandbits(64)),
        ("load", RANDOM_ADDR_POOL[0], None),
    ]
    for _ in range(RANDOM_MIXED_OPS - len(operations)):
        if rng.random() < 0.45:
            operations.append(("store", rng.choice(RANDOM_ADDR_POOL), rng.getrandbits(64)))
        else:
            operations.append(("load", rng.choice(RANDOM_ADDR_POOL), None))

    for op_kind, addr, maybe_data in operations:
        if op_kind == "store":
            store_data = int(maybe_data)
            allocated_sq_ptr = _enqueue_scalar_store(env, 0, sq_ptr)
            _issue_scalar_std(env, 0, allocated_sq_ptr, store_data)
            _issue_scalar_sta(env, 0, allocated_sq_ptr, addr)
            _wait_pending_store_materialized(
                env,
                allocated_sq_ptr,
                expected_addr=addr,
                expected_data=store_data,
                expected_mmio=False,
                require_committed=True,
                max_cycles=300,
            )
            total_stores += 1
            sq_ptr = _ptr_inc(sq_ptr, VIRTUAL_STORE_QUEUE_SIZE)
            continue

        lq_ptr = _issue_and_check_scalar_load(
            env,
            req_id=next_load_req_id,
            lq_ptr=lq_ptr,
            sq_ptr=sq_ptr,
            addr=addr,
            expected_completed_loads=total_loads + 1,
        )
        total_loads += 1
        next_load_req_id += 1

    drain_summary = env.flush_store_buffers_and_wait(
        max_cycles=PER_REQUEST_DRAIN_CYCLES,
        settle_cycles=STORE_PIPELINE_SETTLE_CYCLES,
    )

    assert total_stores > 0, "mixed random 未生成任何 store"
    assert total_loads > 0, "mixed random 未生成任何 load"
    assert env.memory.completed_loads == total_loads, "mixed random load 未全部完成 compare"
    assert env.memory.sbuffer_drain_count > 0, "mixed random 未观测到任何 sbuffer drain"
    assert drain_summary["drain_event_count"] > 0, "mixed random flush 后未记录到 drain 事件"
    env.check_no_outstanding_transactions()
