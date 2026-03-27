# coding=utf-8
"""
MemBlock 随机 load 流量测试。

本用例按 `lsq enqueue -> issue` 的顺序发起随机标量 load：
  1. 先向 `enqLsq[0]` 提交一条 load，显式分配 `lqIdx`
  2. 再把同一条指令的 `lqIdx/sqIdx` 送到 `intIssue[0]`
  3. 通过 `MemoryModel` 预加载数据并校验 `mem_to_ooo.intWriteback`
"""

from dataclasses import dataclass
import random


FU_TYPE_LDU = 1 << 16
LSU_OP_LD = 0x3
RANDOM_SEED = 20260324
RANDOM_LOAD_COUNT = 1000
VIRTUAL_LOAD_QUEUE_SIZE = 72
OBSERVE_WRITEBACK_CYCLES = 4
FINAL_DRAIN_CYCLES = 64
RESET_CYCLES = 20
BACKEND_RESET_SYNC_CYCLES = 200
PER_REQUEST_DRAIN_CYCLES = 256


@dataclass(frozen=True)
class QueuePtr:
    """环形队列指针。"""

    flag: int
    value: int


@dataclass(frozen=True)
class StreamState:
    """测试侧维护的可重建状态。"""

    next_lq_ptr: QueuePtr
    sq_ptr: QueuePtr


def _ptr_inc(ptr: QueuePtr, size: int, step: int = 1) -> QueuePtr:
    """按给定队列大小递增指针。"""

    flag = ptr.flag
    value = ptr.value
    for _ in range(step):
        value += 1
        if value == size:
            value = 0
            flag ^= 0x1
    return QueuePtr(flag=flag, value=value)


def _wait_backend_reset_deassert(env, must_observe_assert: bool) -> None:
    """
    使用 `io_reset_backend` 同步后端内部 reset。

    `io_reset_backend` 为 1 表示 backend 仍处于内部 reset。
    只有在观察到它完成一次 `1 -> 0` 后，测试流量才允许发起。
    """

    observed_assert = not must_observe_assert

    for _ in range(BACKEND_RESET_SYNC_CYCLES):
        backend_reset = int(env.dut.io_reset_backend.value)
        if backend_reset:
            observed_assert = True
        elif observed_assert:
            return
        env.Step(1)

    raise TimeoutError("等待 `io_reset_backend` 解复位超时")


def _reset_env_and_state(env) -> StreamState:
    """
    执行一次完整复位，并同步重建测试侧状态。

    约束：
      - 用例开始前必须先经过一次 reset/deassert
      - 用例执行过程中若再次 reset，软件侧维护的索引状态必须同步清零
    """

    env.reset(cycles=RESET_CYCLES, settle_cycles=1)
    _wait_backend_reset_deassert(env, must_observe_assert=True)

    assert env.dut.reset.value == 0, "解复位后 `reset` 仍为高"
    assert int(env.dut.io_reset_backend.value) == 0, "解复位后 `io_reset_backend` 仍为高"
    assert env.issue[0].ready.value == 1, "解复位后 `issue[0]` 未恢复 ready"
    assert env.lsq_status.lqCanAccept.value == 1, "解复位后 `lqCanAccept` 未恢复为 1"

    return StreamState(
        next_lq_ptr=QueuePtr(flag=0, value=0),
        sq_ptr=QueuePtr(flag=0, value=0),
    )


def _wait_lsq_enq_ready(env, max_cycles: int = 200) -> None:
    """等待 LSQ enqueue 端口可接受新 load。"""

    for _ in range(max_cycles):
        if int(env.dut.io_reset_backend.value):
            raise RuntimeError("等待 `enqLsq` ready 时 backend 进入 reset")
        if env.lsq_enq_meta.canAccept.value and env.lsq_status.lqCanAccept.value:
            return
        env.Step(1)
    raise TimeoutError("等待 `enqLsq` ready 超时")


def _wait_issue_ready(env, max_cycles: int = 200) -> None:
    """等待 issue 端口可接受新 load。"""

    for _ in range(max_cycles):
        if int(env.dut.io_reset_backend.value):
            raise RuntimeError("等待 `issue[0].ready` 时 backend 进入 reset")
        if env.issue[0].ready.value:
            return
        env.Step(1)
    raise TimeoutError("等待 `issue[0].ready` 超时")


def _enqueue_scalar_load(env, req_id: int, lq_ptr: QueuePtr, sq_ptr: QueuePtr) -> None:
    """先向 LSQ 分配一条标量 load。"""

    _wait_lsq_enq_ready(env)

    dut = env.dut
    req = env.lsq_enq_req[0]

    env.lsq_enq_meta.need_alloc[0].value = 1
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


def _issue_scalar_load(env, req_id: int, addr: int, lq_ptr: QueuePtr, sq_ptr: QueuePtr) -> None:
    """将已完成 LSQ 分配的 load 发到 `intIssue[0]`。"""

    _wait_issue_ready(env)

    dut = env.dut
    issue = env.issue[0]

    issue.valid.value = 1
    issue.bits_fuType.value = FU_TYPE_LDU
    issue.bits_fuOpType.value = LSU_OP_LD
    issue.bits_src_0.value = addr
    issue.bits_robIdx_flag.value = (req_id >> 9) & 0x1
    issue.bits_robIdx_value.value = req_id & 0x1FF
    issue.bits_sqIdx_flag.value = sq_ptr.flag
    issue.bits_sqIdx_value.value = sq_ptr.value

    dut.io_ooo_to_mem_intIssue_0_0_bits_imm.value = 0
    dut.io_ooo_to_mem_intIssue_0_0_bits_pdest.value = req_id % 64
    dut.io_ooo_to_mem_intIssue_0_0_bits_rfWen.value = 1
    dut.io_ooo_to_mem_intIssue_0_0_bits_pc.value = 0x80000000 + req_id * 4
    dut.io_ooo_to_mem_intIssue_0_0_bits_ftqIdx_flag.value = 0
    dut.io_ooo_to_mem_intIssue_0_0_bits_ftqIdx_value.value = req_id & 0x3F
    dut.io_ooo_to_mem_intIssue_0_0_bits_ftqOffset.value = 0
    dut.io_ooo_to_mem_intIssue_0_0_bits_loadWaitBit.value = 0
    dut.io_ooo_to_mem_intIssue_0_0_bits_waitForRobIdx_flag.value = 0
    dut.io_ooo_to_mem_intIssue_0_0_bits_waitForRobIdx_value.value = 0
    dut.io_ooo_to_mem_intIssue_0_0_bits_storeSetHit.value = 0
    dut.io_ooo_to_mem_intIssue_0_0_bits_loadWaitStrict.value = 0
    dut.io_ooo_to_mem_intIssue_0_0_bits_ssid.value = 0
    dut.io_ooo_to_mem_intIssue_0_0_bits_lqIdx_flag.value = lq_ptr.flag
    dut.io_ooo_to_mem_intIssue_0_0_bits_lqIdx_value.value = lq_ptr.value

    env.Step(1)
    env.note_load_issued((req_id >> 9) & 0x1, req_id & 0x1FF)
    env.idle_inputs()


def test_api_MemBlock_single_preloaded_load_data_check(env):
    """
    预加载一笔 64-bit 数据，并检查单笔 load 写回结果。

    检查点：
      - `MemoryModel.preload_u64` 能向 backing store 预装数据
      - MemBlock 发起 load 后，`mem_to_ooo.intWriteback` 的 `robIdx/pdest/data` 与期望一致
    """

    req_id = 0
    load_addr = 0x1000
    expected_data = 0x1122334455667788

    stream_state = _reset_env_and_state(env)
    env.memory.preload_u64(load_addr, expected_data)

    _enqueue_scalar_load(env, req_id, stream_state.next_lq_ptr, stream_state.sq_ptr)
    _issue_scalar_load(env, req_id, load_addr, stream_state.next_lq_ptr, stream_state.sq_ptr)
    env.memory.expect_load(
        rob_idx_flag=0,
        rob_idx_value=req_id,
        pdest=req_id % 64,
        addr=load_addr,
        size=8,
        mask=0xFF,
    )

    env.drain_writebacks(max_cycles=200)

    assert env.memory.completed_loads == 1, "单笔预加载 load 未完成数据校验"
    env.check_no_outstanding_transactions()


def test_api_MemBlock_random_1000_load_requests(env):
    """
    按 `lsq -> issue` 顺序持续推进 1000 个随机 load 请求。

    检查点：
      - 1000 个随机 load 请求均能按 LSQ 分配后再 issue 的顺序推进
      - `pendingPtr` 随 load 完成推进后，后续请求可继续执行
      - 连续流量结束后 `issue[0]` 与 `lqCanAccept` 仍保持可用
    """

    rng = random.Random(RANDOM_SEED)
    requests = []

    # 用例仅在开始前做一次 reset，同一轮流量中不再插入计划性 reset。
    stream_state = _reset_env_and_state(env)

    for req_id in range(RANDOM_LOAD_COUNT):
        addr = rng.randrange(0, 1 << 20) << 3
        env.memory.preload_u64(addr, rng.getrandbits(64))
        requests.append((req_id, addr))

    for req_id, random_addr in requests:
        assert int(env.dut.io_reset_backend.value) == 0, (
            f"连续流量过程中 backend 在请求 {req_id} 前重新进入 reset"
        )

        current_lq_ptr = stream_state.next_lq_ptr

        _enqueue_scalar_load(env, req_id, current_lq_ptr, stream_state.sq_ptr)
        _issue_scalar_load(env, req_id, random_addr, current_lq_ptr, stream_state.sq_ptr)
        env.memory.expect_load(
            rob_idx_flag=(req_id >> 9) & 0x1,
            rob_idx_value=req_id & 0x1FF,
            pdest=req_id % 64,
            addr=random_addr,
            size=8,
            mask=0xFF,
        )
        env.drain_writebacks(max_cycles=PER_REQUEST_DRAIN_CYCLES)
        assert env.memory.completed_loads == req_id + 1, f"请求 {req_id} 未在限定周期内完成"

        stream_state = StreamState(
            next_lq_ptr=_ptr_inc(stream_state.next_lq_ptr, VIRTUAL_LOAD_QUEUE_SIZE),
            sq_ptr=stream_state.sq_ptr,
        )

    assert env.issue[0].ready.value == 1, "随机 load 流结束后 `issue[0]` 不再 ready"
    assert env.lsq_status.lqCanAccept.value == 1, "随机 load 流结束后 `lqCanAccept` 变为 0"
    assert env.memory.completed_loads == RANDOM_LOAD_COUNT, "1000 个随机 load 未全部完成校验"
    env.check_no_outstanding_transactions()
