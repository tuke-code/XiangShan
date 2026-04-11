# coding=utf-8
"""
MemBlock 标量 store pipeline 的定向真实 DUT 用例。
"""

from request_apis import LoadTxn, QueuePtr, StoreTxn, ptr_inc
from sequences import (
    FlushStoreBuffersSequence,
    ResetEnvSequence,
    ScalarLoadSequence,
    ScalarStoreCommitSequence,
    SequenceState,
)


STORE_ADDR_BASE = 0x80003000
STORE_ADDRS = [
    STORE_ADDR_BASE,
    STORE_ADDR_BASE + 0x8,
    STORE_ADDR_BASE + 0x40,
]
MISALIGNED_WINDOW_BASE = STORE_ADDR_BASE + 0x80
MISALIGNED_STORE_ADDR = MISALIGNED_WINDOW_BASE + 0x4
MISALIGNED_WINDOW_ADDRS = [
    MISALIGNED_WINDOW_BASE,
    MISALIGNED_WINDOW_BASE + 0x8,
]
PARTIAL_STORE_WINDOW_BASE = STORE_ADDR_BASE + 0xC0
PARTIAL_STORE_WINDOW_ADDRS = [
    PARTIAL_STORE_WINDOW_BASE,
    PARTIAL_STORE_WINDOW_BASE + 0x8,
]


def _reset_env_and_state(env) -> SequenceState:
    return ResetEnvSequence(
        require_sq_ready=True,
    ).run(env)


def _store_data_low64_matches(store, expected_data: int) -> bool:
    if store is None or store.data is None:
        return False
    return (int(store.data) & ((1 << 64) - 1)) == (expected_data & ((1 << 64) - 1))


def _apply_store_stimulus_to_ref_memory(refmem, *, addr: int, data: int, mask: int = 0xFF):
    refmem.apply_store(addr=addr, data=data, mask=mask)
    return refmem


def _commit_scalar_store(env, sq_ptr: QueuePtr, *, req_id: int, addr: int, data: int, mask: int = 0xFF):
    commit_result = ScalarStoreCommitSequence(
        StoreTxn(
            req_id=req_id,
            sq_ptr=sq_ptr,
            addr=addr,
            data=data,
            mask=mask,
        ),
        expected_mmio=False,
        require_committed=True,
        materialize_cycles=300,
    ).run(env)
    committed_store = commit_result.committed_store_view
    assert committed_store is not None and committed_store.committed, (
        f"store req_id={req_id} 未进入 committed"
    )
    return commit_result, ptr_inc(sq_ptr, env.config.sequence.store_queue_size)


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


def test_api_MemBlock_misaligned_store_dual_overlap_loads_directed(env):
    """
    一条 misaligned cacheable store 后接两个 overlap load。

    检查点：
      - misaligned store 能进入 committed
      - younger overlap load 能在 commit-boundary 视图上看到更新结果
      - store mask 不再是普通 aligned dword 的 `0xFF`
    """

    initial_low = 0x1111_2222_3333_4444
    initial_high = 0x5555_6666_7777_8888
    store_data = 0xA1A2_A3A4_A5A6_A7A8

    state = _reset_env_and_state(env)
    env.preload_u64(MISALIGNED_WINDOW_ADDRS[0], initial_low)
    env.preload_u64(MISALIGNED_WINDOW_ADDRS[1], initial_high)
    expected_refmem = env.memory.predict_store(MISALIGNED_STORE_ADDR, store_data)

    commit_result = ScalarStoreCommitSequence(
        StoreTxn(
            req_id=0,
            sq_ptr=state.sq_ptr,
            addr=MISALIGNED_STORE_ADDR,
            data=store_data,
        ),
        expected_mmio=False,
        require_committed=True,
        materialize_cycles=300,
    ).run(env)
    committed_store = commit_result.committed_store_view
    assert committed_store is not None and committed_store.committed, "misaligned store 未进入 committed"
    assert committed_store.addr == MISALIGNED_STORE_ADDR, "misaligned store 地址不匹配"
    assert committed_store.mask not in (0, 0xFF), "misaligned store 未形成预期的非 aligned mask"

    low_load = ScalarLoadSequence(
        LoadTxn(
            req_id=1,
            addr=MISALIGNED_WINDOW_ADDRS[0],
            lq_ptr=state.next_lq_ptr,
            sq_ptr=commit_result.store_result.next_sq_ptr,
        ),
        expected_completed_loads=1,
    ).run(env)
    high_load = ScalarLoadSequence(
        LoadTxn(
            req_id=2,
            addr=MISALIGNED_WINDOW_ADDRS[1],
            lq_ptr=low_load.next_lq_ptr,
            sq_ptr=commit_result.store_result.next_sq_ptr,
        ),
        expected_completed_loads=2,
    ).run(env)
    drain_summary = FlushStoreBuffersSequence(max_cycles=800).run(env)

    assert env.memory.read(MISALIGNED_WINDOW_ADDRS[0], 8) == expected_refmem.read(
        MISALIGNED_WINDOW_ADDRS[0], 8
    ), "misaligned store 的低 8B 视图不匹配"
    assert env.memory.read(MISALIGNED_WINDOW_ADDRS[1], 8) == expected_refmem.read(
        MISALIGNED_WINDOW_ADDRS[1], 8
    ), "misaligned store 的高 8B 视图不匹配"
    assert low_load.next_lq_ptr == QueuePtr(flag=0, value=1), "第一个 overlap load 未按预期推进 LQ"
    assert high_load.next_lq_ptr == QueuePtr(flag=0, value=2), "第二个 overlap load 未按预期推进 LQ"
    assert drain_summary["drain_event_count"] >= 1, "misaligned store flush 后未记录到 drain 事件"
    assert drain_summary["touched_byte_count"] >= 8, "misaligned store flush 覆盖字节数不足"
    env.assert_no_outstanding()


def test_api_MemBlock_partial_word_store_then_aligned_load_directed(env):
    """
    一条 4B partial store 后接整 8B load。

    检查点：
      - `StoreTxn.mask=0x0F` 能下沉为真实 partial store
      - committed store 的 mask 保持为 4B 宽度
      - younger 8B load 能在 commit-boundary 视图上看到 merge 后结果
    """

    initial_word = 0x1122_3344_5566_7788
    store_data = 0xA1A2_A3A4

    state = _reset_env_and_state(env)
    env.preload_u64(PARTIAL_STORE_WINDOW_BASE, initial_word)
    expected_refmem = env.memory.predict_store(
        PARTIAL_STORE_WINDOW_BASE,
        store_data,
        mask=0x0F,
    )

    commit_result = ScalarStoreCommitSequence(
        StoreTxn(
            req_id=0,
            sq_ptr=state.sq_ptr,
            addr=PARTIAL_STORE_WINDOW_BASE,
            data=store_data,
            mask=0x0F,
        ),
        expected_mmio=False,
        require_committed=True,
        materialize_cycles=300,
    ).run(env)
    committed_store = commit_result.committed_store_view
    assert committed_store is not None and committed_store.committed, "partial word store 未进入 committed"
    assert committed_store.mask == 0x0F, "partial word store 未保持 4B mask"

    load_result = ScalarLoadSequence(
        LoadTxn(
            req_id=1,
            addr=PARTIAL_STORE_WINDOW_BASE,
            lq_ptr=state.next_lq_ptr,
            sq_ptr=commit_result.store_result.next_sq_ptr,
        ),
        expected_completed_loads=1,
    ).run(env)
    drain_summary = FlushStoreBuffersSequence(max_cycles=800).run(env)

    assert load_result.next_lq_ptr == QueuePtr(flag=0, value=1), "partial word store 后的 load 未按预期推进 LQ"
    assert env.memory.read(PARTIAL_STORE_WINDOW_BASE, 8) == expected_refmem.read(
        PARTIAL_STORE_WINDOW_BASE, 8
    ), "partial word store merge 结果不匹配"
    assert drain_summary["touched_byte_count"] >= 4, "partial word store flush 覆盖字节数不足"
    env.assert_no_outstanding()


def test_api_MemBlock_partial_byte_store_high_offset_directed(env):
    """
    一条高偏移 1B partial store 后接整 8B load。

    检查点：
      - `StoreTxn.mask=0x01` 能驱动真实 byte store
      - 高偏移 byte store 不会被错误扩成整 dword 写
      - younger load 能看到单字节 merge 结果
    """

    initial_word = 0x0102_0304_0506_0708
    store_data = 0xAB
    store_addr = PARTIAL_STORE_WINDOW_BASE + 0x5

    state = _reset_env_and_state(env)
    env.preload_u64(PARTIAL_STORE_WINDOW_BASE, initial_word)
    expected_refmem = env.memory.predict_store(
        store_addr,
        store_data,
        mask=0x01,
    )

    commit_result = ScalarStoreCommitSequence(
        StoreTxn(
            req_id=0,
            sq_ptr=state.sq_ptr,
            addr=store_addr,
            data=store_data,
            mask=0x01,
        ),
        expected_mmio=False,
        require_committed=True,
        materialize_cycles=300,
    ).run(env)
    committed_store = commit_result.committed_store_view
    assert committed_store is not None and committed_store.committed, "high-offset byte store 未进入 committed"
    assert committed_store.mask == (1 << (store_addr & 0x7)), "high-offset byte store 未落在目标字节位置"

    load_result = ScalarLoadSequence(
        LoadTxn(
            req_id=1,
            addr=PARTIAL_STORE_WINDOW_BASE,
            lq_ptr=state.next_lq_ptr,
            sq_ptr=commit_result.store_result.next_sq_ptr,
        ),
        expected_completed_loads=1,
    ).run(env)
    drain_summary = FlushStoreBuffersSequence(max_cycles=800).run(env)

    assert load_result.next_lq_ptr == QueuePtr(flag=0, value=1), "high-offset byte store 后的 load 未按预期推进 LQ"
    assert env.memory.read(PARTIAL_STORE_WINDOW_BASE, 8) == expected_refmem.read(
        PARTIAL_STORE_WINDOW_BASE, 8
    ), "high-offset byte store merge 结果不匹配"
    assert drain_summary["touched_byte_count"] >= 1, "high-offset byte store flush 覆盖字节数不足"
    env.assert_no_outstanding()


def test_api_MemBlock_partial_byte_merge_same_dword_directed(env):
    """
    同一 dword 上连续执行多条 byte store，再由整 8B load 验证 merge 结果。

    检查点：
      - 多次 1B partial store 都能稳定进入 committed
      - 同地址多次 merge 后 younger full load 能看到拼接后的最终值
      - flush/drain 至少覆盖所有被写字节
    """

    initial_word = 0x8877_6655_4433_2211
    byte_updates = [
        (PARTIAL_STORE_WINDOW_BASE + 0x0, 0xAA),
        (PARTIAL_STORE_WINDOW_BASE + 0x2, 0xBB),
        (PARTIAL_STORE_WINDOW_BASE + 0x5, 0xCC),
        (PARTIAL_STORE_WINDOW_BASE + 0x7, 0xDD),
    ]

    state = _reset_env_and_state(env)
    env.preload_u64(PARTIAL_STORE_WINDOW_BASE, initial_word)

    sq_ptr = state.sq_ptr
    expected_refmem = env.memory.fork_ref_memory()
    committed_masks = []

    for req_id, (addr, data) in enumerate(byte_updates):
        commit_result, sq_ptr = _commit_scalar_store(
            env,
            sq_ptr,
            req_id=req_id,
            addr=addr,
            data=data,
            mask=0x01,
        )
        committed_store = commit_result.committed_store_view
        committed_masks.append(int(committed_store.mask))
        _apply_store_stimulus_to_ref_memory(
            expected_refmem,
            addr=addr,
            data=data,
            mask=0x01,
        )

    load_result = ScalarLoadSequence(
        LoadTxn(
            req_id=len(byte_updates),
            addr=PARTIAL_STORE_WINDOW_BASE,
            lq_ptr=state.next_lq_ptr,
            sq_ptr=sq_ptr,
        ),
        expected_completed_loads=1,
    ).run(env)
    drain_summary = FlushStoreBuffersSequence(max_cycles=800).run(env)

    assert committed_masks == [0x01, 0x04, 0x20, 0x80], "byte merge store 未落在预期字节 lane"
    assert load_result.next_lq_ptr == QueuePtr(flag=0, value=1), "byte merge 场景下 load 未按预期推进 LQ"
    assert env.memory.read(PARTIAL_STORE_WINDOW_BASE, 8) == expected_refmem.read(
        PARTIAL_STORE_WINDOW_BASE, 8
    ), "byte merge 后最终 dword 结果不匹配"
    assert drain_summary["touched_byte_count"] >= len(byte_updates), "byte merge flush 覆盖字节数不足"
    env.assert_no_outstanding()


def test_api_MemBlock_full_store_then_partial_overwrite_directed(env):
    """
    先 full store，再做高偏移 partial overwrite。

    检查点：
      - full store 与后续 partial overwrite 都能进入 committed
      - partial overwrite 只覆盖目标字节，不破坏其余旧值
      - younger full load 能看到 overwrite 后的最终视图
    """

    base_data = 0x1122_3344_5566_7788
    overwrite_addr = PARTIAL_STORE_WINDOW_BASE + 0x6
    overwrite_data = 0xEE

    state = _reset_env_and_state(env)
    expected_refmem = env.memory.fork_ref_memory()

    first_result, sq_ptr = _commit_scalar_store(
        env,
        state.sq_ptr,
        req_id=0,
        addr=PARTIAL_STORE_WINDOW_BASE,
        data=base_data,
    )
    _apply_store_stimulus_to_ref_memory(
        expected_refmem,
        addr=PARTIAL_STORE_WINDOW_BASE,
        data=base_data,
    )
    second_result, sq_ptr = _commit_scalar_store(
        env,
        sq_ptr,
        req_id=1,
        addr=overwrite_addr,
        data=overwrite_data,
        mask=0x01,
    )
    _apply_store_stimulus_to_ref_memory(
        expected_refmem,
        addr=overwrite_addr,
        data=overwrite_data,
        mask=0x01,
    )

    load_result = ScalarLoadSequence(
        LoadTxn(
            req_id=2,
            addr=PARTIAL_STORE_WINDOW_BASE,
            lq_ptr=state.next_lq_ptr,
            sq_ptr=sq_ptr,
        ),
        expected_completed_loads=1,
    ).run(env)
    drain_summary = FlushStoreBuffersSequence(max_cycles=800).run(env)

    assert first_result.committed_store_view.mask == 0xFF, "base full store mask 异常"
    assert second_result.committed_store_view.mask == (1 << (overwrite_addr & 0x7)), (
        "partial overwrite 未落在预期字节 lane"
    )
    assert load_result.next_lq_ptr == QueuePtr(flag=0, value=1), "overwrite 场景下 load 未按预期推进 LQ"
    assert env.memory.read(PARTIAL_STORE_WINDOW_BASE, 8) == expected_refmem.read(
        PARTIAL_STORE_WINDOW_BASE, 8
    ), "full store + partial overwrite 结果不匹配"
    assert drain_summary["touched_byte_count"] >= 8, "overwrite 场景 flush 覆盖字节数不足"
    env.assert_no_outstanding()


def test_api_MemBlock_interleaved_partial_stores_two_addresses_directed(env):
    """
    两个地址交织执行 partial-store，再分别用 full load 检查最终 merge 结果。

    检查点：
      - partial-store 不会因为地址交织而串扰
      - 两个地址都能形成各自独立的 merge 视图
      - 两笔 younger load 都能在 commit-boundary 视图上看到最终值
    """

    initial_words = (
        0x0102_0304_0506_0708,
        0x1112_1314_1516_1718,
    )
    partial_ops = [
        (PARTIAL_STORE_WINDOW_ADDRS[0] + 0x1, 0xAAAA, 0x03),
        (PARTIAL_STORE_WINDOW_ADDRS[1] + 0x0, 0xBB, 0x01),
        (PARTIAL_STORE_WINDOW_ADDRS[0] + 0x6, 0xCC, 0x01),
        (PARTIAL_STORE_WINDOW_ADDRS[1] + 0x4, 0xDDDD, 0x03),
    ]

    state = _reset_env_and_state(env)
    env.preload_u64(PARTIAL_STORE_WINDOW_ADDRS[0], initial_words[0])
    env.preload_u64(PARTIAL_STORE_WINDOW_ADDRS[1], initial_words[1])

    sq_ptr = state.sq_ptr
    expected_refmem = env.memory.fork_ref_memory()

    for req_id, (addr, data, mask) in enumerate(partial_ops):
        commit_result, sq_ptr = _commit_scalar_store(
            env,
            sq_ptr,
            req_id=req_id,
            addr=addr,
            data=data,
            mask=mask,
        )
        _apply_store_stimulus_to_ref_memory(
            expected_refmem,
            addr=addr,
            data=data,
            mask=mask,
        )

    first_load = ScalarLoadSequence(
        LoadTxn(
            req_id=len(partial_ops),
            addr=PARTIAL_STORE_WINDOW_ADDRS[0],
            lq_ptr=state.next_lq_ptr,
            sq_ptr=sq_ptr,
        ),
        expected_completed_loads=1,
    ).run(env)
    second_load = ScalarLoadSequence(
        LoadTxn(
            req_id=len(partial_ops) + 1,
            addr=PARTIAL_STORE_WINDOW_ADDRS[1],
            lq_ptr=first_load.next_lq_ptr,
            sq_ptr=sq_ptr,
        ),
        expected_completed_loads=2,
    ).run(env)
    drain_summary = FlushStoreBuffersSequence(max_cycles=800).run(env)

    assert first_load.next_lq_ptr == QueuePtr(flag=0, value=1), "第一个交织 partial-store load 未按预期推进 LQ"
    assert second_load.next_lq_ptr == QueuePtr(flag=0, value=2), "第二个交织 partial-store load 未按预期推进 LQ"
    assert env.memory.read(PARTIAL_STORE_WINDOW_ADDRS[0], 8) == expected_refmem.read(
        PARTIAL_STORE_WINDOW_ADDRS[0], 8
    ), (
        "交织 partial-store 后窗口 0 结果不匹配"
    )
    assert env.memory.read(PARTIAL_STORE_WINDOW_ADDRS[1], 8) == expected_refmem.read(
        PARTIAL_STORE_WINDOW_ADDRS[1], 8
    ), (
        "交织 partial-store 后窗口 1 结果不匹配"
    )
    assert drain_summary["touched_byte_count"] >= 6, "交织 partial-store flush 覆盖字节数不足"
    env.assert_no_outstanding()


def test_api_MemBlock_store_burst_then_interleaved_load_before_flush(env):
    """
    多条 cacheable store burst 后，先执行 younger load，再统一 flush/drain。

    检查点：
      - burst 中的 store 都能进入 committed
      - younger load 能在 flush 前完成 compare
      - 最终统一 flush 时能把整批 store 收口到 drain
    """

    burst_addrs = [
        STORE_ADDRS[0],
        STORE_ADDRS[1],
        STORE_ADDRS[2],
        STORE_ADDRS[2] + 0x8,
    ]
    burst_data = [
        0x0102_0304_0506_0708,
        0x1112_1314_1516_1718,
        0x2122_2324_2526_2728,
        0x3132_3334_3536_3738,
    ]

    state = _reset_env_and_state(env)
    sq_ptr = state.sq_ptr
    committed_views = []

    for req_id, (addr, data_word) in enumerate(zip(burst_addrs, burst_data)):
        commit_result = ScalarStoreCommitSequence(
            StoreTxn(req_id=req_id, sq_ptr=sq_ptr, addr=addr, data=data_word),
            expected_mmio=False,
            require_committed=True,
            materialize_cycles=300,
        ).run(env)
        committed_views.append(commit_result.committed_store_view)
        sq_ptr = ptr_inc(sq_ptr, env.config.sequence.store_queue_size)

    load_result = ScalarLoadSequence(
        LoadTxn(
            req_id=len(burst_data),
            addr=burst_addrs[2],
            lq_ptr=state.next_lq_ptr,
            sq_ptr=sq_ptr,
        ),
        expected_completed_loads=1,
    ).run(env)
    drain_summary = FlushStoreBuffersSequence(max_cycles=1000).run(env)

    for idx, (store, addr, data_word) in enumerate(zip(committed_views, burst_addrs, burst_data)):
        assert store is not None and store.committed, f"burst 中第 {idx} 条 store 未进入 committed"
        assert store.addr == addr, f"burst 中第 {idx} 条 store 地址不匹配"
        assert _store_data_low64_matches(store, data_word), f"burst 中第 {idx} 条 store 数据不匹配"

    assert load_result.next_lq_ptr == QueuePtr(flag=0, value=1), "interleaved load 未按预期推进 LQ"
    assert sq_ptr == ptr_inc(state.sq_ptr, env.config.sequence.store_queue_size, step=len(burst_data)), (
        "store burst 后 SQ 指针推进异常"
    )
    assert drain_summary["drain_event_count"] >= 1, "store burst 统一 flush 后未记录到 drain 事件"
    assert drain_summary["touched_byte_count"] >= 32, "store burst 统一 flush 覆盖字节数不足"
    env.assert_no_outstanding()
