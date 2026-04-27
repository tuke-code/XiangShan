# coding=utf-8
"""
MemBlock 标量 load width/mask directed。
"""

import pytest

from request_apis import expect_load, send_load
from sequences import ResetEnvSequence
from transactions import LoadTxn


SCALAR_LOAD_WIDTH_BASE_ADDR = 0x80002000
SCALAR_LOAD_WIDTH_PRELOAD = 0x11223344D566F788
SCALAR_LOAD_WIDTH_CASES = (
    ("byte", 1, 0x01, 0xFFFFFFFFFFFFFF88),
    ("half", 2, 0x03, 0xFFFFFFFFFFFFF788),
    ("word", 4, 0x0F, 0xFFFFFFFFD566F788),
    ("doubleword", 8, 0xFF, 0x11223344D566F788),
)


@pytest.mark.parametrize("size_name,size,mask,expected_data", SCALAR_LOAD_WIDTH_CASES)
def test_api_MemBlock_scalar_load_width_and_mask_match_writeback(env, size_name, size, mask, expected_data):
    """
    标量 `lb/lh/lw/ld` 应按 `size/mask` 产生对应写回位型。
    """

    completed_before = env.get_completed_load_count()
    state = ResetEnvSequence(
        require_issue_lanes=(0,),
        require_lq_ready=True,
    ).run(env)
    load_addr = SCALAR_LOAD_WIDTH_BASE_ADDR + (size << 4)
    env.preload_u64(load_addr, SCALAR_LOAD_WIDTH_PRELOAD)

    txn = LoadTxn(
        req_id=0xA0 + size,
        addr=load_addr,
        lq_ptr=state.next_lq_ptr,
        sq_ptr=state.sq_ptr,
        size=size,
        mask=mask,
    )

    send_load(env, txn)
    expect_load(env, txn)
    writeback = env.wait_load_writeback_observed(
        rob_idx=txn.rob_idx,
        data=expected_data,
        expected_fp_wen=False,
        max_cycles=200,
    )
    assert writeback["int_wen"] == 1, f"{size_name} integer load 未写向 Int RF"
    assert writeback["fp_wen"] == 0, f"{size_name} integer load 不应写向 FP RF"
    assert writeback["data"] == expected_data, f"{size_name} integer load 写回数据不匹配"

    env.drain_writebacks(max_cycles=200)
    assert env.get_completed_load_count() == completed_before + 1, f"{size_name} integer load 未完成 compare"
    env.assert_no_outstanding()
