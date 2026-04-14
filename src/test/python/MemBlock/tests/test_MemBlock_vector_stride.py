# coding=utf-8
"""
MemBlock 向量 stride load 的真实 DUT smoke 用例。
"""

from sequences import ResetEnvSequence, VectorLoadSequence
from transactions import VectorMemTxn


VECTOR_ADDR_BASE = 0x80004100


def _reset_env_and_state(env):
    return ResetEnvSequence(require_lq_ready=True).run(env)


def _pack_expected_low_bits(expectation) -> int:
    packed = 0
    shift = 0
    for access in expectation.accesses:
        if not access.should_access_memory:
            continue
        packed |= int(access.expected_load_data) << shift
        shift += access.size_bytes * 8
    return packed


def _select_vector_data_writeback(vector_result) -> dict:
    for writeback in vector_result.observed_writebacks:
        if writeback["vec_wen"]:
            return writeback
    return vector_result.observed_writebacks[0]


def test_api_MemBlock_vector_stride_load_smoke(env):
    """验证正 stride vector load 能完成，并在 writeback 元数据中体现 strided 语义。"""

    state = _reset_env_and_state(env)
    values = (0xAABBCCDD, 0xEEFF0011)
    stride = 8
    for idx, value in enumerate(values):
        env.memory.preload_bytes(VECTOR_ADDR_BASE + idx * stride, value.to_bytes(4, "little"))

    result = VectorLoadSequence(
        VectorMemTxn(
            req_id=0x102,
            is_load=True,
            opcode_class="stride",
            base_addr=VECTOR_ADDR_BASE,
            stride=stride,
            lq_ptr=state.next_lq_ptr,
            sq_ptr=state.sq_ptr,
                vl=2,
                element_count=2,
                sew_bits=32,
                issue_port=0,
                enq_port=0,
            )
    ).run(env)

    vector_result = result.vector_result
    data_writeback = _select_vector_data_writeback(vector_result)
    transport_stats = env.get_transport_stats()
    expected_low_bits = _pack_expected_low_bits(result.expected)

    assert vector_result.completed and not vector_result.trapped
    assert data_writeback["vec_wen"] == 1, f"未观测到 vec data writeback: {vector_result.observed_writebacks!r}"
    assert data_writeback["is_vec_load"] == 1
    assert data_writeback["is_strided"] == 1
    assert vector_result.observed_vl == 2
    assert data_writeback["data"] == expected_low_bits
    assert transport_stats["dcache_a_request_count"] >= 1
    assert transport_stats["dcache_d_response_count"] >= 2
    assert transport_stats["last_dcache_a_block_address"] in {VECTOR_ADDR_BASE, VECTOR_ADDR_BASE + 64}
    env.assert_no_outstanding()
