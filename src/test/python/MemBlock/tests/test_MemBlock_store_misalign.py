# coding=utf-8
"""
MemBlock store misalign 的定向真实 DUT 用例。
"""

import pytest

from transactions import LoadTxn, QueuePtr, StoreTxn
from sequences import (
    FlushStoreBuffersSequence,
    ResetEnvSequence,
    ScalarLoadSequence,
    ScalarStoreSequence,
    ScalarStoreCommitSequence,
    SequenceState,
)


STORE_MISALIGN_BASE = 0x80003400
LOW_WINDOW_ADDR = STORE_MISALIGN_BASE + 0x8
HIGH_WINDOW_ADDR = STORE_MISALIGN_BASE + 0x10
LOW_WINDOW_INIT = 0x1111_2222_3333_4444
HIGH_WINDOW_INIT = 0x5555_6666_7777_8888

CROSS_PAGE_BASE = 0x80004000
CROSS_PAGE_LOW_WINDOW_ADDR = CROSS_PAGE_BASE + 0xFF8
CROSS_PAGE_HIGH_WINDOW_ADDR = CROSS_PAGE_BASE + 0x1000
CROSS_PAGE_LOW_WINDOW_INIT = 0x0123_4567_89AB_CDEF
CROSS_PAGE_HIGH_WINDOW_INIT = 0xFEDC_BA98_7654_3210

DUTBUG_STORE_MISALIGN_CROSS_PAGE_FLUSH_STALL = "DUTBUG-store-misalign-cross-page-flush-stall"
DUT_SRC_MAIN_STORE_MISALIGN_CROSS_PAGE_FLUSH_STALL = "b52262f63d88313f5a153716e22e7d40fd89d831"
DUTBUG_STORE_MISALIGN_CROSS_PAGE_FLUSH_STALL_XFAIL_REASON = (
    f"{DUTBUG_STORE_MISALIGN_CROSS_PAGE_FLUSH_STALL} "
    f"(dut-src-main={DUT_SRC_MAIN_STORE_MISALIGN_CROSS_PAGE_FLUSH_STALL}): "
    "cross-page scalar store misalign reaches shadow/load-observable state, but flushSb cannot drain sbuffer to empty"
)


def _reset_env_and_state(env) -> SequenceState:
    return ResetEnvSequence(
        require_sq_ready=True,
    ).run(env)


def _run_cross_16b_store_misalign_case(
    env,
    *,
    store_addr: int,
    store_data: int,
    store_mask: int,
    min_touched_bytes: int,
):
    state = _reset_env_and_state(env)
    env.preload_u64(LOW_WINDOW_ADDR, LOW_WINDOW_INIT)
    env.preload_u64(HIGH_WINDOW_ADDR, HIGH_WINDOW_INIT)
    expected_refmem = env.memory.predict_store(store_addr, store_data, mask=store_mask)

    store_txn = StoreTxn(
        req_id=0,
        sq_ptr=state.sq_ptr,
        addr=store_addr,
        data=store_data,
        mask=store_mask,
    )
    low_split_bytes = min(store_txn.size_bytes, 16 - (store_addr & 0xF))
    expected_committed_mask = ((1 << low_split_bytes) - 1) << (store_addr & 0xF)
    commit_result = ScalarStoreCommitSequence(
        store_txn,
        expected_mmio=False,
        require_committed=True,
        materialize_cycles=300,
    ).run(env)
    committed_store = commit_result.committed_store_view
    assert committed_store is not None and committed_store.committed, "cross-16B misalign store 未进入 committed"
    assert committed_store.addr == store_addr, "cross-16B misalign store 地址不匹配"
    assert committed_store.mask == expected_committed_mask, "cross-16B misalign store 的首段 split mask 异常"

    low_load = ScalarLoadSequence(
        LoadTxn(
            req_id=1,
            addr=LOW_WINDOW_ADDR,
            lq_ptr=state.next_lq_ptr,
            sq_ptr=commit_result.store_result.next_sq_ptr,
        ),
        expected_completed_loads=1,
    ).run(env)
    high_load = ScalarLoadSequence(
        LoadTxn(
            req_id=2,
            addr=HIGH_WINDOW_ADDR,
            lq_ptr=low_load.next_lq_ptr,
            sq_ptr=commit_result.store_result.next_sq_ptr,
        ),
        expected_completed_loads=2,
    ).run(env)
    drain_summary = FlushStoreBuffersSequence(max_cycles=800).run(env)

    assert low_load.next_lq_ptr == QueuePtr(flag=0, value=1), "低窗口 overlap load 未按预期推进 LQ"
    assert high_load.next_lq_ptr == QueuePtr(flag=0, value=2), "高窗口 overlap load 未按预期推进 LQ"
    assert env.memory.read(LOW_WINDOW_ADDR, 8) == expected_refmem.read(LOW_WINDOW_ADDR, 8), (
        "cross-16B misalign 低窗口结果不匹配"
    )
    assert env.memory.read(HIGH_WINDOW_ADDR, 8) == expected_refmem.read(HIGH_WINDOW_ADDR, 8), (
        "cross-16B misalign 高窗口结果不匹配"
    )
    assert drain_summary["drain_event_count"] >= 1, "cross-16B misalign flush 后未记录到 drain 事件"
    assert drain_summary["touched_byte_count"] >= min_touched_bytes, "cross-16B misalign flush 覆盖字节数不足"
    env.assert_no_outstanding()


def _run_cross_page_store_misalign_case(
    env,
    *,
    store_addr: int,
    store_data: int,
    store_mask: int,
    min_touched_bytes: int,
):
    state = _reset_env_and_state(env)
    env.preload_u64(CROSS_PAGE_LOW_WINDOW_ADDR, CROSS_PAGE_LOW_WINDOW_INIT)
    env.preload_u64(CROSS_PAGE_HIGH_WINDOW_ADDR, CROSS_PAGE_HIGH_WINDOW_INIT)
    expected_refmem = env.memory.predict_store(store_addr, store_data, mask=store_mask)

    store_txn = StoreTxn(
        req_id=0,
        sq_ptr=state.sq_ptr,
        addr=store_addr,
        data=store_data,
        mask=store_mask,
    )
    low_split_bytes = min(store_txn.size_bytes, 0x1000 - (store_addr & 0xFFF))
    expected_committed_mask = ((1 << low_split_bytes) - 1) << (store_addr & 0xF)

    store_result = ScalarStoreSequence(
        store_txn,
        expected_mmio=False,
        require_committed=False,
        materialize_cycles=300,
    ).run(env)
    env.backend.pulse_store_commit(1)
    committed_store = env.get_store_view(store_result.allocated_sq_ptr.value)

    assert committed_store is not None and committed_store.completed, "cross-page misalign store 未形成可观测 shadow"
    assert committed_store.addr == store_addr, "cross-page misalign store 地址不匹配"
    assert committed_store.mask == expected_committed_mask, "cross-page misalign store 的首段 split mask 异常"

    low_load = ScalarLoadSequence(
        LoadTxn(
            req_id=1,
            addr=CROSS_PAGE_LOW_WINDOW_ADDR,
            lq_ptr=state.next_lq_ptr,
            sq_ptr=store_result.next_sq_ptr,
        ),
        expected_completed_loads=1,
    ).run(env)
    high_load = ScalarLoadSequence(
        LoadTxn(
            req_id=2,
            addr=CROSS_PAGE_HIGH_WINDOW_ADDR,
            lq_ptr=low_load.next_lq_ptr,
            sq_ptr=store_result.next_sq_ptr,
        ),
        expected_completed_loads=2,
    ).run(env)
    drain_summary = FlushStoreBuffersSequence(max_cycles=800).run(env)

    assert low_load.next_lq_ptr == QueuePtr(flag=0, value=1), "cross-page 低页 overlap load 未按预期推进 LQ"
    assert high_load.next_lq_ptr == QueuePtr(flag=0, value=2), "cross-page 高页 overlap load 未按预期推进 LQ"
    assert env.memory.read(CROSS_PAGE_LOW_WINDOW_ADDR, 8) == expected_refmem.read(CROSS_PAGE_LOW_WINDOW_ADDR, 8), (
        "cross-page misalign 低页窗口结果不匹配"
    )
    assert env.memory.read(CROSS_PAGE_HIGH_WINDOW_ADDR, 8) == expected_refmem.read(CROSS_PAGE_HIGH_WINDOW_ADDR, 8), (
        "cross-page misalign 高页窗口结果不匹配"
    )
    assert drain_summary["drain_event_count"] >= 1, "cross-page misalign flush 后未记录到 drain 事件"
    assert drain_summary["touched_byte_count"] >= min_touched_bytes, "cross-page misalign flush 覆盖字节数不足"
    env.assert_no_outstanding()


def test_api_MemBlock_store_misalign_sd_offset_d_cross_16b(env):
    """
    8B store 落在 16B 窗口尾部 +0xD，覆盖 3B/5B split。
    """

    _run_cross_16b_store_misalign_case(
        env,
        store_addr=STORE_MISALIGN_BASE + 0xD,
        store_data=0xA1A2_A3A4_A5A6_A7A8,
        store_mask=0xFF,
        min_touched_bytes=8,
    )


def test_api_MemBlock_store_misalign_sd_offset_e_cross_16b(env):
    """
    8B store 落在 16B 窗口尾部 +0xE，覆盖 2B/6B split。
    """

    _run_cross_16b_store_misalign_case(
        env,
        store_addr=STORE_MISALIGN_BASE + 0xE,
        store_data=0xB1B2_B3B4_B5B6_B7B8,
        store_mask=0xFF,
        min_touched_bytes=8,
    )


def test_api_MemBlock_store_misalign_sd_offset_f_cross_16b(env):
    """
    8B store 落在 16B 窗口尾部 +0xF，覆盖 1B/7B split。
    """

    _run_cross_16b_store_misalign_case(
        env,
        store_addr=STORE_MISALIGN_BASE + 0xF,
        store_data=0xC1C2_C3C4_C5C6_C7C8,
        store_mask=0xFF,
        min_touched_bytes=8,
    )


def test_api_MemBlock_store_misalign_sw_offset_d_cross_16b(env):
    """
    4B store 落在 16B 窗口尾部 +0xD，覆盖非 SD 宽度 split。
    """

    _run_cross_16b_store_misalign_case(
        env,
        store_addr=STORE_MISALIGN_BASE + 0xD,
        store_data=0xD1D2_D3D4,
        store_mask=0x0F,
        min_touched_bytes=4,
    )


def test_api_MemBlock_store_misalign_sh_offset_f_cross_16b(env):
    """
    2B store 落在 16B 窗口尾部 +0xF，覆盖最小宽度跨 16B split。
    """

    _run_cross_16b_store_misalign_case(
        env,
        store_addr=STORE_MISALIGN_BASE + 0xF,
        store_data=0xE1E2,
        store_mask=0x03,
        min_touched_bytes=2,
    )


@pytest.mark.xfail(reason=DUTBUG_STORE_MISALIGN_CROSS_PAGE_FLUSH_STALL_XFAIL_REASON, strict=True)
def test_api_MemBlock_store_misalign_sd_offset_d_cross_page(env):
    """
    8B store 落在页尾 +0xFFD，覆盖 cross-page 正常 split。
    """

    _run_cross_page_store_misalign_case(
        env,
        store_addr=CROSS_PAGE_BASE + 0xFFD,
        store_data=0x1122_3344_5566_7788,
        store_mask=0xFF,
        min_touched_bytes=8,
    )


@pytest.mark.xfail(reason=DUTBUG_STORE_MISALIGN_CROSS_PAGE_FLUSH_STALL_XFAIL_REASON, strict=True)
def test_api_MemBlock_store_misalign_sh_offset_f_cross_page(env):
    """
    2B store 落在页尾 +0xFFF，覆盖最小宽度 cross-page split。
    """

    _run_cross_page_store_misalign_case(
        env,
        store_addr=CROSS_PAGE_BASE + 0xFFF,
        store_data=0x99AA,
        store_mask=0x03,
        min_touched_bytes=2,
    )
