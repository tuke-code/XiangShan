# coding=utf-8
"""
VirtualLoadQueue 覆盖率定向补强用例。

目标：
  1. 使用全部 8 个 enqueue 端口，命中 entryCanEnqSeq_4/5/6/7
  2. 高 commitCount，命中 commitCount > 2/3/4/5 分支
  3. redirect 取消在途 load，命中 needCancel / redirectCancelCount
"""

import pytest

from transactions import (
    BackendSendPlan,
    EnqueueLoadCyclePlan,
    EnqueueLoadStep,
    IssueCyclePlan,
    IssueOp,
    LoadTxn,
    ptr_inc,
)
from sequences import ResetEnvSequence
from sequences.memblock_sequences import SequenceState


CACHELINE_BYTES = 64
MEM_ADDR_BASE = 0x80000080


def _reset_and_state(env) -> SequenceState:
    return ResetEnvSequence(
        require_issue_lanes=(0, 1, 2),
        require_lq_ready=True,
    ).run(env)


def _addr(idx: int) -> int:
    return MEM_ADDR_BASE + idx * CACHELINE_BYTES


def _enqueue_only(env, txn) -> None:
    """仅 enqueue 一条 load，不 issue。"""
    env.backend.execute(
        BackendSendPlan.from_steps(
            EnqueueLoadCyclePlan.from_steps(
                EnqueueLoadStep.from_txn(txn),
                max_cycles=200,
            ),
        )
    )


def _issue_batch(env, txns: list) -> None:
    """同一周期 issue 一批 load（最多 3 条，lanes 0-2）。"""
    assert len(txns) <= 3, f"同拍最多 issue 3 条 load，实际 {len(txns)}"
    ops = []
    for lane, txn in enumerate(txns):
        txn.issue_lane = lane
        ops.append(IssueOp.load_from_txn(txn))
    env.backend.execute(
        BackendSendPlan.from_steps(
            IssueCyclePlan.from_ops(*ops, max_cycles=200),
        )
    )


# ═══════════════════════════════════════════════════════════════════
# Case A: 8 端口同拍 enqueue
# ═══════════════════════════════════════════════════════════════════


def test_vlq_eight_port_same_cycle_enqueue(env):
    """同一周期使用 8 个 enqueue 端口入队 8 条标量 load。"""

    state = _reset_and_state(env)

    loads = []
    for port in range(8):
        addr = _addr(port)
        env.preload_u64(addr, 0xDEAD0000 + port)
        loads.append(
            LoadTxn(
                req_id=0x10 + port,
                addr=addr,
                lq_ptr=ptr_inc(state.next_lq_ptr, env.config.sequence.load_queue_size, step=port),
                sq_ptr=state.sq_ptr,
                size=8,
                mask=0xFF,
                enq_port=port,
            )
        )

    # 同拍 enqueue 全部 8 条
    env.backend.execute(
        BackendSendPlan.from_steps(
            EnqueueLoadCyclePlan.from_steps(
                *(EnqueueLoadStep.from_txn(load) for load in loads),
                max_cycles=400,
            ),
        )
    )

    env.advance_cycles(1)
    vlq = env.sample_vlq_state()
    assert vlq["allocated_count"] >= 8, (
        f"VLQ 至少应有 8 个已分配条目，实际 {vlq['allocated_count']}"
    )

    # 分批 issue: lanes 0-2，每批 3 条
    batches = [loads[:3], loads[3:6], loads[6:]]
    for batch in batches:
        if not batch:
            continue
        _issue_batch(env, batch)

    for load in loads:
        env.expect_scalar_load(
            rob_idx=load.rob_idx, pdest=load.resolved_pdest, addr=load.addr
        )

    env.wait_completed_load_count(8, max_cycles=800)
    env.drain_writebacks(max_cycles=400)
    assert env.get_completed_load_count() == 8
    env.assert_no_outstanding()


# ═══════════════════════════════════════════════════════════════════
# Case B: 高 commitCount 多条目并发退休
# ═══════════════════════════════════════════════════════════════════


def test_vlq_many_loads_high_commit_count(env):
    """连续入队 16 条 cacheable load，批量 issue，一次性 drain。

    通过集中入队后统一 drain，观察 VLQ 多条目并发退休。"""

    from request_apis import send_load, expect_load

    state = _reset_and_state(env)
    num_loads = 16

    txns = []
    for i in range(num_loads):
        addr = _addr(i + 8)
        env.preload_u64(addr, 0xBEEF0000 + i)
        txn = LoadTxn(
            req_id=0x50 + i,
            addr=addr,
            lq_ptr=ptr_inc(state.next_lq_ptr, env.config.sequence.load_queue_size, step=i),
            sq_ptr=state.sq_ptr,
            size=8,
            mask=0xFF,
            enq_port=i % 8,
            issue_lane=i % 3,
        )
        send_load(env, txn)
        expect_load(env, txn)
        txns.append(txn)

    max_lq_deq = 0
    for _ in range(40):
        env.advance_cycles(4)
        vlq = env.sample_vlq_state()
        if vlq["lq_deq"] > max_lq_deq:
            max_lq_deq = vlq["lq_deq"]

    env.drain_writebacks(max_cycles=2000)
    assert env.get_completed_load_count() == num_loads, (
        f"应有 {num_loads} 条 load 完成 compare，"
        f"实际 {env.get_completed_load_count()}"
    )

    assert max_lq_deq >= 2, f"lqDeq 峰值应 >= 2，实际 {max_lq_deq}"
    env.assert_no_outstanding()


# ═══════════════════════════════════════════════════════════════════
# Case C: redirect 取消在途 load
# ═══════════════════════════════════════════════════════════════════


def test_vlq_redirect_cancels_inflight_loads(env):
    """先入队若干 load 但不 issue，确认 VLQ 中有条目后触发 redirect。"""

    state = _reset_and_state(env)

    # 入队 4 条 load，不 issue，让它们停留在 VLQ
    loads = []
    for i in range(4):
        addr = _addr(i + 48)
        env.preload_u64(addr, 0xCAFE0000 + i)
        txn = LoadTxn(
            req_id=0x60 + i,
            addr=addr,
            lq_ptr=ptr_inc(state.next_lq_ptr, env.config.sequence.load_queue_size, step=i),
            sq_ptr=state.sq_ptr,
            size=8,
            mask=0xFF,
            enq_port=i,
        )
        _enqueue_only(env, txn)
        env.expect_scalar_load(
            rob_idx=txn.rob_idx, pdest=txn.resolved_pdest, addr=txn.addr
        )
        loads.append(txn)

    env.advance_cycles(1)
    vlq_before = env.sample_vlq_state()
    assert vlq_before["allocated_count"] >= 4, (
        f"redirect 前 VLQ 至少应有 4 个条目，实际 {vlq_before['allocated_count']}"
    )
    assert vlq_before["lq_cancel_cnt"] == 0, "redirect 前 lqCancelCnt 应为 0"

    # 驱动 redirect 脉冲
    env.redirect.valid.value = 1
    env.redirect.bits_level.value = 0
    env.redirect.bits_robIdx_flag.value = 1
    env.redirect.bits_robIdx_value.value = 0
    env.advance_cycles(3)  # redirect 流水线: lastCycleRedirect -> lastLastCycleRedirect
    env.redirect.drive_idle()
    env.idle_inputs()
    env.advance_cycles(4)   # 等待取消生效

    vlq_after = env.sample_vlq_state()
    cancel_cnt = vlq_after["lq_cancel_cnt"]
    assert cancel_cnt > 0 or vlq_after["allocated_count"] < vlq_before["allocated_count"], (
        f"redirect 后应有取消事件: cancel_cnt={cancel_cnt}, "
        f"allocated before={vlq_before['allocated_count']} after={vlq_after['allocated_count']}"
    )

    # 重新 reset 清理状态
    _reset_and_state(env)
