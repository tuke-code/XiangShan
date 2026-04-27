# coding=utf-8
"""
MemBlock 标量 fp load directed。
"""

import pytest

from request_apis import expect_load, send_load
from sequences import ResetEnvSequence
from transactions import LoadTxn


FP_LOAD_BASE_ADDR = 0x80001000
FP_LOAD_PRELOAD = 0x1122334455667788
FP_LOAD_CASES = (
    ("half", 2, 0x03, 0xFFFFFFFFFFFF7788),
    ("word", 4, 0x0F, 0xFFFFFFFF55667788),
    ("doubleword", 8, 0xFF, 0x1122334455667788),
)


@pytest.mark.parametrize("size_name,size,mask,expected_data", FP_LOAD_CASES)
def test_api_MemBlock_scalar_fp_load_writeback_boxed_data(env, size_name, size, mask, expected_data):
    """
    标量 `fpWen` load 应通过 load writeback 口写回 boxed bit-pattern。
    """

    completed_before = env.get_completed_load_count()
    state = ResetEnvSequence(
        require_issue_lanes=(0,),
        require_lq_ready=True,
    ).run(env)
    load_addr = FP_LOAD_BASE_ADDR + (size << 4)
    env.preload_u64(load_addr, FP_LOAD_PRELOAD)

    txn = LoadTxn(
        req_id=0x90 + size,
        addr=load_addr,
        lq_ptr=state.next_lq_ptr,
        sq_ptr=state.sq_ptr,
        size=size,
        mask=mask,
        fp_wen=1,
    )

    send_load(env, txn)
    expect_load(env, txn)
    writeback = env.wait_load_writeback_observed(
        rob_idx=txn.rob_idx,
        data=expected_data,
        expected_fp_wen=True,
        max_cycles=200,
    )
    assert writeback["fp_wen"] == 1, f"{size_name} fp load 未写向 FP RF"
    assert writeback["int_wen"] == 0, f"{size_name} fp load 不应同时写向 Int RF"
    assert writeback["data"] == expected_data, f"{size_name} fp load boxed 数据不匹配"

    env.drain_writebacks(max_cycles=200)
    assert env.get_completed_load_count() == completed_before + 1, f"{size_name} fp load 未完成 compare"
    env.assert_no_outstanding()
