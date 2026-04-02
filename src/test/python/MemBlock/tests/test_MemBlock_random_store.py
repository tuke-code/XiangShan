# coding=utf-8
"""
MemBlock 真实 DUT store 冒烟测试。

当前版本先覆盖两条最小真实路径：
  1. 单条 MMIO store: 验证 `STD -> STA -> SQ -> uncache/outer`
  2. 单条 cacheable store: 验证 `STD -> STA -> SQ -> sbuffer -> flushSb`

与 `test_memory_model_store_logic.py` 不同，本文件不直接 mock `MemoryModel`，
而是通过真实 DUT 端口驱动 enqueue / issue，并复用 `MemBlockEnv` 的在线观测口做校验。
"""

import random

from request_apis import (
    QueuePtr,
    StoreTxn,
)
from sequences.memblock_sequences import (
    ResetEnvSequence,
    ScalarMixedTrafficSequence,
    ScalarStoreCommitSequence,
    ScalarStoreFlushSequence,
    ScalarStoreThenLoadSequence,
    SequenceState,
)

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


def _reset_env_and_state(env) -> QueuePtr:
    return ResetEnvSequence(require_sq_ready=True).run(env).sq_ptr


def _store_data_low64_matches(store, expected_data: int) -> bool:
    if store.data is None:
        return False
    return (int(store.data) & ((1 << 64) - 1)) == (expected_data & ((1 << 64) - 1))


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
    outer_writes_before = env.get_counter("outer_write_request_count")
    sbuffer_drains_before = env.get_counter("sbuffer_drain_count")

    store_result = ScalarStoreCommitSequence(
        StoreTxn(req_id=req_id, sq_ptr=sq_ptr, addr=MMIO_STORE_ADDR, data=store_data),
        expected_mmio=True,
        settle_cycles=env.config.sequence.store_settle_cycles,
        wait_quiesce=True,
        quiesce_cycles=300,
    ).run(env)
    store = store_result.committed_store_view
    env.wait_counter_growth("outer_write_request_count", outer_writes_before, max_cycles=300)
    env.wait_memory_quiesce(max_cycles=300)

    assert store.mmio, "MMIO store 未被 store shadow 标记为 mmio"
    assert store.addr == MMIO_STORE_ADDR, "MMIO store shadow 地址不匹配"
    assert _store_data_low64_matches(store, store_data), "MMIO store shadow 数据不匹配"
    assert env.get_counter("sbuffer_drain_count") == sbuffer_drains_before, "MMIO store 不应进入 sbuffer"
    assert env.get_counter("outer_write_request_count") > outer_writes_before, "MMIO store 未发出 outer 写请求"
    env.assert_no_outstanding()


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

    initial_state = SequenceState(
        next_lq_ptr=QueuePtr(flag=0, value=0),
        sq_ptr=_reset_env_and_state(env),
    )

    result = ScalarStoreThenLoadSequence(
        StoreTxn(req_id=store_req_id, sq_ptr=initial_state.sq_ptr, addr=CACHEABLE_STORE_ADDR, data=store_data),
        initial_lq_ptr=initial_state.next_lq_ptr,
        load_req_id=load_req_id,
        expected_mmio=False,
        require_committed=True,
        materialize_cycles=300,
    ).run(env)
    store = result.store_result.store_view

    assert store.committed, "older store 未进入 committed 状态"
    assert store.addr == CACHEABLE_STORE_ADDR, "store shadow 地址不匹配"
    assert _store_data_low64_matches(store, store_data), "store shadow 数据不匹配"
    assert result.final_state.next_lq_ptr.value == 1, "load queue 指针未按预期前移"
    env.assert_no_outstanding()


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
    result = ScalarStoreFlushSequence(
        StoreTxn(req_id=req_id, sq_ptr=sq_ptr, addr=CACHEABLE_STORE_ADDR, data=store_data),
        expected_mmio=False,
        require_committed=True,
        materialize_cycles=300,
        sbuffer_wait_cycles=300,
        max_cycles=400,
        settle_cycles=env.config.sequence.store_settle_cycles,
    ).run(env)
    store = result.store_result.store_view
    drain_summary = result.drain_summary

    assert not store.mmio, "cacheable store 被错误标记为 mmio"
    assert store.addr == CACHEABLE_STORE_ADDR, "cacheable store shadow 地址不匹配"
    assert _store_data_low64_matches(store, store_data), "cacheable store shadow 数据不匹配"
    assert result.sbuffer_drain_delta > 0, "cacheable store 未进入 sbuffer"
    assert result.outer_write_delta == 0, "cacheable store 不应走 outer/mmio 写路径"
    assert drain_summary["drain_event_count"] > 0, "flushSb 后未记录到任何 drain 事件"
    assert drain_summary["touched_byte_count"] >= 8, "drain 覆盖字节数异常"
    env.assert_no_outstanding()


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
    initial_state = SequenceState(
        next_lq_ptr=QueuePtr(flag=0, value=0),
        sq_ptr=_reset_env_and_state(env),
    )

    for addr in RANDOM_ADDR_POOL:
        env.preload_u64(addr, rng.getrandbits(64))

    operations = [
        ("store", RANDOM_ADDR_POOL[0], rng.getrandbits(64)),
        ("load", RANDOM_ADDR_POOL[0], None),
    ]
    for _ in range(RANDOM_MIXED_OPS - len(operations)):
        if rng.random() < 0.45:
            operations.append(("store", rng.choice(RANDOM_ADDR_POOL), rng.getrandbits(64)))
        else:
            operations.append(("load", rng.choice(RANDOM_ADDR_POOL), None))

    result = ScalarMixedTrafficSequence(
        operations,
        initial_state=initial_state,
        store_req_id=0,
        first_load_req_id=1,
        expected_store_mmio=False,
        require_store_committed=True,
        store_materialize_cycles=300,
        flush=True,
        flush_max_cycles=env.config.sequence.store_flush_cycles,
        flush_settle_cycles=env.config.sequence.store_settle_cycles,
    ).run(env)

    assert result.total_stores > 0, "mixed random 未生成任何 store"
    assert result.total_loads > 0, "mixed random 未生成任何 load"
    assert env.get_completed_load_count() == result.total_loads, "mixed random load 未全部完成 compare"
    assert env.get_counter("sbuffer_drain_count") > 0, "mixed random 未观测到任何 sbuffer drain"
    assert result.drain_summary["drain_event_count"] > 0, "mixed random flush 后未记录到 drain 事件"
    env.assert_no_outstanding()
