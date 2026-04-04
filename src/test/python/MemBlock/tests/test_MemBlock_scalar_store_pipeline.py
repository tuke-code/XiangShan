# coding=utf-8
"""
MemBlock 标量 store pipeline 的定向真实 DUT 用例。
"""

from request_apis import QueuePtr, StoreTxn, ptr_inc
from sequences.memblock_sequences import (
    FlushStoreBuffersSequence,
    ResetEnvSequence,
    ScalarStoreCommitSequence,
    SequenceState,
)


STORE_ADDR_BASE = 0x80003000
STORE_ADDRS = [
    STORE_ADDR_BASE,
    STORE_ADDR_BASE + 0x8,
    STORE_ADDR_BASE + 0x40,
]


def _reset_env_and_state(env) -> SequenceState:
    return ResetEnvSequence(
        require_sq_ready=True,
    ).run(env)


def _store_data_low64_matches(store, expected_data: int) -> bool:
    if store is None or store.data is None:
        return False
    return (int(store.data) & ((1 << 64) - 1)) == (expected_data & ((1 << 64) - 1))


def test_api_MemBlock_two_cacheable_stores_flush_directed(env):
    """
    两条递增 robIdx 的 cacheable store，结尾统一 flush/drain。

    检查点：
      - 两条 store 均能在真实 ROB 提交建模下进入 committed
      - 结尾 flush 后成功观测到 sbuffer drain
      - drain 至少覆盖两笔 dword 写入
    """

    first_data = 0x0102_0304_0506_0708
    second_data = 0x1112_1314_1516_1718

    state = _reset_env_and_state(env)
    sbuffer_before = env.get_counter("sbuffer_drain_count")

    first_result = ScalarStoreCommitSequence(
        StoreTxn(req_id=0, sq_ptr=state.sq_ptr, addr=STORE_ADDRS[0], data=first_data),
        expected_mmio=False,
        require_committed=True,
        materialize_cycles=300,
    ).run(env)
    second_result = ScalarStoreCommitSequence(
        StoreTxn(
            req_id=1,
            sq_ptr=ptr_inc(state.sq_ptr, env.config.sequence.store_queue_size),
            addr=STORE_ADDRS[1],
            data=second_data,
        ),
        expected_mmio=False,
        require_committed=True,
        materialize_cycles=300,
    ).run(env)
    drain_summary = FlushStoreBuffersSequence(max_cycles=800).run(env)

    first_store = first_result.committed_store_view
    second_store = second_result.committed_store_view

    assert first_store is not None and first_store.committed, "第一条 cacheable store 未进入 committed"
    assert second_store is not None and second_store.committed, "第二条 cacheable store 未进入 committed"
    assert first_store.addr == STORE_ADDRS[0], "第一条 cacheable store 地址不匹配"
    assert second_store.addr == STORE_ADDRS[1], "第二条 cacheable store 地址不匹配"
    assert _store_data_low64_matches(first_store, first_data), "第一条 cacheable store 数据不匹配"
    assert _store_data_low64_matches(second_store, second_data), "第二条 cacheable store 数据不匹配"
    assert env.get_counter("sbuffer_drain_count") > sbuffer_before, "双 store flush 未触发 sbuffer drain"
    assert drain_summary["drain_event_count"] >= 1, "双 store flush 未记录到 drain 事件"
    assert drain_summary["touched_byte_count"] >= 16, "双 store flush 覆盖字节数不足"
    env.assert_no_outstanding()


def test_api_MemBlock_cross_line_two_cacheable_stores_flush_directed(env):
    """
    两条递增 robIdx 的 cacheable store，覆盖跨 cacheline 写出。

    检查点：
      - 两条 store 都能在 flush 前进入 committed
      - 统一 flush 后 drain 收口成功
      - 跨 cacheline 写出不会破坏最终一致性
    """

    data_words = [
        0x2222_3333_4444_5555,
        0xAAAA_BBBB_CCCC_DDDD,
    ]
    addrs = [STORE_ADDRS[0], STORE_ADDRS[2]]

    state = _reset_env_and_state(env)
    sq_ptr = state.sq_ptr
    committed_views = []

    for req_id, (addr, data_word) in enumerate(zip(addrs, data_words)):
        commit_result = ScalarStoreCommitSequence(
            StoreTxn(req_id=req_id, sq_ptr=sq_ptr, addr=addr, data=data_word),
            expected_mmio=False,
            require_committed=True,
            materialize_cycles=300,
        ).run(env)
        committed_views.append(commit_result.committed_store_view)
        sq_ptr = ptr_inc(sq_ptr, env.config.sequence.store_queue_size)

    drain_summary = FlushStoreBuffersSequence(max_cycles=800).run(env)

    for idx, (store, addr, data_word) in enumerate(zip(committed_views, addrs, data_words)):
        assert store is not None and store.committed, f"第 {idx} 条 store 未进入 committed"
        assert store.addr == addr, f"第 {idx} 条 store 地址不匹配"
        assert _store_data_low64_matches(store, data_word), f"第 {idx} 条 store 数据不匹配"

    assert drain_summary["drain_event_count"] >= 1, "跨线双 store flush 未记录到 drain 事件"
    assert drain_summary["touched_byte_count"] >= 16, "跨线双 store flush 覆盖字节数不足"
    env.assert_no_outstanding()
