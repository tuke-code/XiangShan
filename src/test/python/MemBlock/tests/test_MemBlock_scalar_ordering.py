# coding=utf-8
"""
MemBlock 标量 load/store 排序场景的定向真实 DUT 用例。
"""

from transactions import LoadTxn, ptr_inc, QueuePtr, StoreTxn
from sequences import (
    FlushStoreBuffersSequence,
    ResetEnvSequence,
    ScalarLoadSequence,
    ScalarMixedTrafficSequence,
    ScalarStorePairThenLoadSequence,
    ScalarStoreSequence,
    SequenceState,
)


CACHEABLE_ADDR_A = 0x80002000
CACHEABLE_ADDR_B = 0x80002008
CACHEABLE_ADDR_C = 0x80002010


def _reset_env_and_state(env) -> SequenceState:
    return ResetEnvSequence(
        require_issue_lanes=(0,),
        require_lq_ready=True,
        require_sq_ready=True,
    ).run(env)


def _store_data_low64_matches(store, expected_data: int) -> bool:
    if store.data is None:
        return False
    return (int(store.data) & ((1 << 64) - 1)) == (expected_data & ((1 << 64) - 1))


def test_api_MemBlock_two_cacheable_stores_then_load_same_addr(env):
    """
    两条同地址 cacheable store 后接同地址 load。

    检查点：
      - 两条 older store 都进入 committed 视图
      - younger load 在线 compare 应看到更新后的第二条 store 数据
      - 结尾 flush 后存在真实 sbuffer drain
    """

    first_data = 0x1111_2222_3333_4444
    second_data = 0x5555_6666_7777_8888

    state = _reset_env_and_state(env)
    expected_refmem = env.memory.predict_store(CACHEABLE_ADDR_A, first_data).with_store(
        CACHEABLE_ADDR_A,
        second_data,
    )
    result = ScalarStorePairThenLoadSequence(
        StoreTxn(req_id=0, sq_ptr=state.sq_ptr, addr=CACHEABLE_ADDR_A, data=first_data),
        StoreTxn(
            req_id=1,
            sq_ptr=ptr_inc(state.sq_ptr, env.config.sequence.store_queue_size),
            addr=CACHEABLE_ADDR_A,
            data=second_data,
        ),
        initial_lq_ptr=state.next_lq_ptr,
        load_req_id=2,
    ).run(env)
    first_store = env.get_store_view(result.first_store_result.allocated_sq_ptr.value)
    second_store = env.get_store_view(result.second_store_result.allocated_sq_ptr.value)
    drain_summary = FlushStoreBuffersSequence().run(env)

    assert first_store is not None and second_store is not None, "same-addr store 视图缺失"
    assert first_store.committed, "第一条 same-addr store 未进入 committed"
    assert second_store.committed, "第二条 same-addr store 未进入 committed"
    assert _store_data_low64_matches(first_store, first_data), "第一条 same-addr store 数据不匹配"
    assert _store_data_low64_matches(second_store, second_data), "第二条 same-addr store 数据不匹配"
    assert result.final_state.next_lq_ptr == QueuePtr(flag=0, value=1), "same-addr load 未按预期推进 LQ"
    assert result.final_state.sq_ptr == QueuePtr(flag=0, value=2), "same-addr store 未按预期推进 SQ"
    assert drain_summary["drain_event_count"] >= 1, "same-addr store flush 后未记录到 drain"
    assert env.memory.read(CACHEABLE_ADDR_A, 8) == expected_refmem.read(
        CACHEABLE_ADDR_A, 8
    ), "same-addr store flush 后最终 golden memory 不匹配"
    env.assert_no_outstanding()


def test_api_MemBlock_cacheable_store_then_unrelated_load(env):
    """
    cacheable store 后接无关地址 load。

    检查点：
      - 无关 load 仍能正常完成 compare
      - older store 在结尾 flush 后成功 drain
      - 无关地址的预装数据不会被 store 错误污染
    """

    unrelated_data = 0x1020_3040_5060_7080
    store_data = 0x8877_6655_4433_2211

    state = _reset_env_and_state(env)
    env.preload_u64(CACHEABLE_ADDR_B, unrelated_data)
    expected_refmem = env.memory.predict_store(CACHEABLE_ADDR_A, store_data)

    store_result = ScalarStoreSequence(
        StoreTxn(req_id=0, sq_ptr=state.sq_ptr, addr=CACHEABLE_ADDR_A, data=store_data),
        expected_mmio=False,
        require_committed=True,
        materialize_cycles=300,
    ).run(env)
    load_result = ScalarLoadSequence(
        txn=LoadTxn(
            req_id=1,
            addr=CACHEABLE_ADDR_B,
            lq_ptr=state.next_lq_ptr,
            sq_ptr=store_result.next_sq_ptr,
        ),
        expected_completed_loads=1,
    ).run(env)
    drain_summary = FlushStoreBuffersSequence().run(env)

    assert store_result.store_view.committed, "unrelated load 场景中的 store 未进入 committed"
    assert _store_data_low64_matches(store_result.store_view, store_data), "unrelated load 场景中的 store 数据不匹配"
    assert load_result.next_lq_ptr == QueuePtr(flag=0, value=1), "unrelated load 未按预期推进 LQ"
    assert drain_summary["drain_event_count"] >= 1, "unrelated load 场景 flush 后未记录到 drain"
    assert env.memory.read(CACHEABLE_ADDR_A, 8) == expected_refmem.read(
        CACHEABLE_ADDR_A, 8
    ), "unrelated load 场景中的 store 最终 golden memory 不匹配"
    assert env.memory.read(CACHEABLE_ADDR_B, 8) == expected_refmem.read(
        CACHEABLE_ADDR_B, 8
    ), "unrelated load 场景中的无关地址被意外污染"
    env.assert_no_outstanding()


def test_api_MemBlock_small_directed_mixed_ld_st_sequence(env):
    """
    小规模定向 mixed ld/st 场景。

    模板：
      - store A
      - load A
      - store B
      - load A
      - store A(覆盖)
      - load A
      - load B

    检查点：
      - mixed 流在线只比对 load
      - 结尾统一 flush/drain
      - 所有 store 最终均能通过 drain 收口
    """

    state = _reset_env_and_state(env)
    env.preload_u64(CACHEABLE_ADDR_A, 0x0)
    env.preload_u64(CACHEABLE_ADDR_B, 0x1234_5678_9ABC_DEF0)
    env.preload_u64(CACHEABLE_ADDR_C, 0x0BAD_F00D_1122_3344)

    operations = [
        ("store", CACHEABLE_ADDR_A, 0x0102_0304_0506_0708),
        ("load", CACHEABLE_ADDR_A, None),
        ("store", CACHEABLE_ADDR_B, 0x1111_2222_3333_4444),
        ("load", CACHEABLE_ADDR_A, None),
        ("store", CACHEABLE_ADDR_A, 0xAABB_CCDD_EEFF_0011),
        ("load", CACHEABLE_ADDR_A, None),
        ("load", CACHEABLE_ADDR_B, None),
    ]
    expected_refmem = env.memory.fork_ref_memory()
    for op_kind, addr, data in operations:
        if op_kind == "store":
            expected_refmem.apply_store(addr, data)

    result = ScalarMixedTrafficSequence(
        operations,
        initial_state=state,
        store_req_id=0,
        first_load_req_id=1,
        expected_store_mmio=False,
        require_store_committed=True,
        store_materialize_cycles=300,
        flush=True,
        assert_no_outstanding=True,
    ).run(env)

    assert result.total_stores == 3, "定向 mixed 场景 store 数量不匹配"
    assert result.total_loads == 4, "定向 mixed 场景 load 数量不匹配"
    assert result.completed_load_count == 4, "定向 mixed 场景并未完成全部 load compare"
    assert result.final_state.next_lq_ptr == QueuePtr(flag=0, value=4), "定向 mixed 场景 LQ 指针异常"
    assert result.final_state.sq_ptr == QueuePtr(flag=0, value=3), "定向 mixed 场景 SQ 指针异常"
    assert result.drain_summary is not None, "定向 mixed 场景缺少 flush/drain 收尾"
    assert result.drain_summary["drain_event_count"] >= 1, "定向 mixed 场景未记录到 drain"
    assert env.memory.read(CACHEABLE_ADDR_A, 8) == expected_refmem.read(
        CACHEABLE_ADDR_A, 8
    ), "定向 mixed 场景中的地址 A 最终 golden memory 不匹配"
    assert env.memory.read(CACHEABLE_ADDR_B, 8) == expected_refmem.read(
        CACHEABLE_ADDR_B, 8
    ), "定向 mixed 场景中的地址 B 最终 golden memory 不匹配"
    assert env.memory.read(CACHEABLE_ADDR_C, 8) == expected_refmem.read(
        CACHEABLE_ADDR_C, 8
    ), "定向 mixed 场景中的地址 C 不应被无关 store 污染"
