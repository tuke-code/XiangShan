# coding=utf-8
"""
MemBlock 向量 store 的真实 DUT regression 用例。
"""

import pytest

from sequences import ResetEnvSequence, VectorStoreSequence
from transactions import VectorMemTxn


VECTOR_STORE_ADDR_BASE = 0x80004200
VECTOR_STORE_MASKED_ADDR_BASE = VECTOR_STORE_ADDR_BASE + 0x80
VECTOR_STORE_VSTART_ADDR_BASE = VECTOR_STORE_ADDR_BASE + 0x100

DUTBUG_VECTOR_STORE_DATA_PATH_DISCONNECTED = "DUTBUG-vector-store-data-path-disconnected"
DUT_SRC_MAIN_VECTOR_STORE_DATA_PATH_DISCONNECTED = "03bc924c72cb055ccb8146a2eecd750ead0b4d7b"
DUTBUG_VECTOR_STORE_DATA_PATH_DISCONNECTED_XFAIL_REASON = (
    f"{DUTBUG_VECTOR_STORE_DATA_PATH_DISCONNECTED} "
    f"(dut-src-main={DUT_SRC_MAIN_VECTOR_STORE_DATA_PATH_DISCONNECTED}): "
    "vector store reaches completion metadata, but MemBlock keeps "
    "`vsSplit(i).io.vstd.get := DontCare`, so SQ observes zero store data and flushSb never drains"
)


def _reset_env_and_state(env):
    return ResetEnvSequence(require_sq_ready=True).run(env)


def _known_vector_store_bug_observed(
    store_view,
    transport_stats,
    memory_stats,
    flush_exc,
    drain_log,
    *,
    expected_addr: int,
) -> bool:
    del memory_stats, drain_log
    return (
        flush_exc is not None
        and store_view is not None
        and store_view.allocated
        and store_view.addr == expected_addr
        and store_view.data == 0
        and transport_stats["dcache_a_request_count"] == 0
        and transport_stats["dcache_d_response_count"] == 0
    )


def test_api_MemBlock_vector_unit_stride_store_flush_regression(env):
    """
    验证 unit-stride vector store 的最终 flush/drain 语义。

    当前真实 DUT 已能给出 vector completion metadata，但在已知缺口存在时会表现为：
    SQ 可见条目地址、data 始终为 0、无 dcache transport、flushSb 超时不 drain。
    """

    state = _reset_env_and_state(env)
    txn = VectorMemTxn(
        req_id=0x130,
        is_load=False,
        opcode_class="unit_stride",
        base_addr=VECTOR_STORE_ADDR_BASE,
        lq_ptr=state.next_lq_ptr,
        sq_ptr=state.sq_ptr,
        vl=2,
        element_count=2,
        sew_bits=32,
        store_data=(0xDEADBEEF, 0x12345678),
        enq_port=0,
        issue_port=0,
    )

    result = VectorStoreSequence(txn).run(env)
    vector_result = result.vector_result
    expected_memory = result.expected

    assert vector_result.completed and not vector_result.trapped
    assert vector_result.observed_vl == 2
    assert vector_result.observed_writebacks, "vector store 未返回 completion/writeback metadata"
    assert vector_result.observed_writebacks[-1]["is_vec_load"] == 0
    assert vector_result.observed_writebacks[-1]["is_strided"] == 0

    env.backend.pulse_store_commit(1)
    first_store = env.wait_store_addr_observed(state.sq_ptr.value, VECTOR_STORE_ADDR_BASE, max_cycles=100)

    assert first_store is not None and first_store.allocated, "vector store 未在 SQ 中形成可观测条目"
    assert first_store.addr == VECTOR_STORE_ADDR_BASE
    assert first_store.mask == 0xFF

    flush_exc = None
    drain_summary = None
    try:
        drain_summary = env.flush_store_buffers_and_wait(max_cycles=200, settle_cycles=2)
    except TimeoutError as exc:
        flush_exc = exc

    transport_stats = env.get_transport_stats()
    memory_stats = env.memory.stats
    observed_store = env.get_store_view(state.sq_ptr.value)
    if _known_vector_store_bug_observed(
        observed_store,
        transport_stats,
        memory_stats,
        flush_exc,
        env.memory.drain_log,
        expected_addr=VECTOR_STORE_ADDR_BASE,
    ):
        pytest.xfail(
            f"{DUTBUG_VECTOR_STORE_DATA_PATH_DISCONNECTED_XFAIL_REASON}; "
            f"store_view={observed_store!r}; transport={transport_stats!r}; memory={memory_stats!r}; "
            f"flush_error={flush_exc!r}"
        )

    if flush_exc is not None:
        raise flush_exc

    assert drain_summary is not None
    assert drain_summary["drain_event_count"] >= 1, "vector store flush 后未记录到 drain 事件"
    assert env.memory.read(VECTOR_STORE_ADDR_BASE, 8) == expected_memory.read(VECTOR_STORE_ADDR_BASE, 8), (
        "vector store flush 后的最终内存结果不匹配"
    )
    env.assert_no_outstanding()


def test_api_MemBlock_vector_unit_stride_store_masked_inactive_flush_regression(env):
    """
    验证带 inactive element 的 unit-stride vector store 仍能形成 SQ 可观测元数据。

    目标是把 `vecInactive` / mask 相关控制流送进 NewStoreQueue；
    如果命中已知 vector store drain bug，则仅在精确 flush 条件上 xfail。
    """

    state = _reset_env_and_state(env)
    txn = VectorMemTxn(
        req_id=0x131,
        is_load=False,
        opcode_class="unit_stride",
        base_addr=VECTOR_STORE_MASKED_ADDR_BASE,
        lq_ptr=state.next_lq_ptr,
        sq_ptr=state.sq_ptr,
        vl=4,
        element_count=4,
        sew_bits=32,
        vstart=0,
        mask_bits=(1, 0, 1, 0),
        vm=False,
        store_data=(0x11112222, 0x33334444, 0x55556666, 0x77778888),
        enq_port=0,
        issue_port=0,
    )

    result = VectorStoreSequence(txn).run(env)
    vector_result = result.vector_result
    expected_memory = result.expected

    assert vector_result.completed and not vector_result.trapped
    assert vector_result.observed_vl == 4
    assert vector_result.observed_writebacks, "masked vector store 未返回 completion/writeback metadata"
    assert vector_result.observed_writebacks[-1]["is_vec_load"] == 0

    env.backend.pulse_store_commit(1)
    first_store = env.wait_store_addr_observed(state.sq_ptr.value, VECTOR_STORE_MASKED_ADDR_BASE, max_cycles=100)
    assert first_store is not None and first_store.allocated, "masked vector store 未在 SQ 中形成可观测条目"
    assert first_store.addr == VECTOR_STORE_MASKED_ADDR_BASE

    flush_exc = None
    drain_summary = None
    try:
        drain_summary = env.flush_store_buffers_and_wait(max_cycles=200, settle_cycles=2)
    except TimeoutError as exc:
        flush_exc = exc

    transport_stats = env.get_transport_stats()
    memory_stats = env.memory.stats
    observed_store = env.get_store_view(state.sq_ptr.value)
    if _known_vector_store_bug_observed(
        observed_store,
        transport_stats,
        memory_stats,
        flush_exc,
        env.memory.drain_log,
        expected_addr=VECTOR_STORE_MASKED_ADDR_BASE,
    ):
        pytest.xfail(
            f"{DUTBUG_VECTOR_STORE_DATA_PATH_DISCONNECTED_XFAIL_REASON}; "
            f"store_view={observed_store!r}; transport={transport_stats!r}; memory={memory_stats!r}; "
            f"flush_error={flush_exc!r}; scenario=masked-inactive"
        )

    if flush_exc is not None:
        raise flush_exc

    assert drain_summary is not None
    assert drain_summary["drain_event_count"] >= 1, "masked vector store flush 后未记录到 drain 事件"
    assert env.memory.read(VECTOR_STORE_MASKED_ADDR_BASE, 16) == expected_memory.read(VECTOR_STORE_MASKED_ADDR_BASE, 16), (
        "masked vector store flush 后的最终内存结果不匹配"
    )
    env.assert_no_outstanding()


def test_api_MemBlock_vector_unit_stride_store_nonzero_vstart_flush_regression(env):
    """
    验证 nonzero `vstart` 的 unit-stride vector store 控制流。

    目标是命中 vector store 的 skipped-element / late-element 提交窗口；
    如果最终落在已知 drain 缺口，则保持精确 xfail。
    """

    state = _reset_env_and_state(env)
    txn = VectorMemTxn(
        req_id=0x132,
        is_load=False,
        opcode_class="unit_stride",
        base_addr=VECTOR_STORE_VSTART_ADDR_BASE,
        lq_ptr=state.next_lq_ptr,
        sq_ptr=state.sq_ptr,
        vl=4,
        element_count=4,
        sew_bits=32,
        vstart=2,
        store_data=(0x89ABCDEF, 0x01234567, 0x76543210, 0xFEDCBA98),
        enq_port=0,
        issue_port=0,
    )

    result = VectorStoreSequence(txn).run(env)
    vector_result = result.vector_result
    expected_memory = result.expected

    assert vector_result.completed and not vector_result.trapped
    assert vector_result.observed_vl == 4
    assert vector_result.observed_writebacks, "nonzero-vstart vector store 未返回 completion/writeback metadata"
    assert vector_result.observed_writebacks[-1]["is_vec_load"] == 0

    env.backend.pulse_store_commit(1)
    first_store = env.wait_store_addr_observed(state.sq_ptr.value, VECTOR_STORE_VSTART_ADDR_BASE, max_cycles=100)
    assert first_store is not None and first_store.allocated, "nonzero-vstart vector store 未在 SQ 中形成可观测条目"
    assert first_store.addr == VECTOR_STORE_VSTART_ADDR_BASE

    flush_exc = None
    drain_summary = None
    try:
        drain_summary = env.flush_store_buffers_and_wait(max_cycles=200, settle_cycles=2)
    except TimeoutError as exc:
        flush_exc = exc

    transport_stats = env.get_transport_stats()
    memory_stats = env.memory.stats
    observed_store = env.get_store_view(state.sq_ptr.value)
    if _known_vector_store_bug_observed(
        observed_store,
        transport_stats,
        memory_stats,
        flush_exc,
        env.memory.drain_log,
        expected_addr=VECTOR_STORE_VSTART_ADDR_BASE,
    ):
        pytest.xfail(
            f"{DUTBUG_VECTOR_STORE_DATA_PATH_DISCONNECTED_XFAIL_REASON}; "
            f"store_view={observed_store!r}; transport={transport_stats!r}; memory={memory_stats!r}; "
            f"flush_error={flush_exc!r}; scenario=nonzero-vstart"
        )

    if flush_exc is not None:
        raise flush_exc

    assert drain_summary is not None
    assert drain_summary["drain_event_count"] >= 1, "nonzero-vstart vector store flush 后未记录到 drain 事件"
    assert env.memory.read(VECTOR_STORE_VSTART_ADDR_BASE, 16) == expected_memory.read(VECTOR_STORE_VSTART_ADDR_BASE, 16), (
        "nonzero-vstart vector store flush 后的最终内存结果不匹配"
    )
    env.assert_no_outstanding()
