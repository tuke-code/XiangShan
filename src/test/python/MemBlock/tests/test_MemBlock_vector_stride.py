# coding=utf-8
"""
MemBlock 向量 stride load 的真实 DUT smoke 用例。
"""

from sequences import ResetEnvSequence, VectorLoadSequence
from transactions import VectorMemTxn


VECTOR_ADDR_BASE = 0x80004100


def _reset_env_and_state(env):
    return ResetEnvSequence(require_lq_ready=True).run(env)


def _preload_elements(env, base_addr: int, values, *, size_bytes: int, stride_bytes: int) -> None:
    for idx, value in enumerate(values):
        env.memory.preload_bytes(
            int(base_addr) + idx * int(stride_bytes),
            int(value).to_bytes(size_bytes, "little", signed=False),
        )


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


def _assert_stride_load_result(env, result, *, expected_data: int, expected_vl: int, expected_blocks: set[int]):
    vector_result = result.vector_result
    data_writeback = _select_vector_data_writeback(vector_result)
    transport_stats = env.get_transport_stats()

    assert vector_result.completed and not vector_result.trapped
    assert data_writeback["vec_wen"] == 1, f"未观测到 vec data writeback: {vector_result.observed_writebacks!r}"
    assert data_writeback["is_vec_load"] == 1
    assert data_writeback["is_strided"] == 1
    assert vector_result.observed_vl == expected_vl
    assert data_writeback["data"] == expected_data
    assert transport_stats["dcache_a_request_count"] >= 1
    assert transport_stats["dcache_d_response_count"] >= 2
    assert transport_stats["last_dcache_a_block_address"] in expected_blocks
    env.assert_no_outstanding()


def test_api_MemBlock_vector_stride_load_smoke(env):
    """验证正 stride vector load 能完成，并在 writeback 元数据中体现 strided 语义。"""

    state = _reset_env_and_state(env)
    values = (0xAABBCCDD, 0xEEFF0011)
    stride = 8
    _preload_elements(env, VECTOR_ADDR_BASE, values, size_bytes=4, stride_bytes=stride)

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

    _assert_stride_load_result(
        env,
        result,
        expected_data=_pack_expected_low_bits(result.expected),
        expected_vl=2,
        expected_blocks={VECTOR_ADDR_BASE, VECTOR_ADDR_BASE + 64},
    )


def test_api_MemBlock_vector_stride_zero_load_smoke(env):
    """验证 zero-stride vector load 会把同一地址的数据重复填入各 element 槽位。"""

    state = _reset_env_and_state(env)
    base_addr = VECTOR_ADDR_BASE + 0x40
    repeated_value = 0x13579BDF
    env.memory.preload_bytes(base_addr, repeated_value.to_bytes(4, "little"))

    result = VectorLoadSequence(
        VectorMemTxn(
            req_id=0x128,
            is_load=True,
            opcode_class="stride",
            base_addr=base_addr,
            stride=0,
            lq_ptr=state.next_lq_ptr,
            sq_ptr=state.sq_ptr,
            vl=4,
            element_count=4,
            sew_bits=32,
            issue_port=0,
            enq_port=0,
        )
    ).run(env)

    _assert_stride_load_result(
        env,
        result,
        expected_data=_pack_expected_low_bits(result.expected),
        expected_vl=4,
        expected_blocks={base_addr, base_addr + 64},
    )


def test_api_MemBlock_vector_stride_negative_load_smoke(env):
    """验证 negative-stride vector load 能按逆序地址展开并正确 merge。"""

    state = _reset_env_and_state(env)
    stride = -4
    base_addr = VECTOR_ADDR_BASE + 0x90
    values = (0xAAAAB001, 0xAAAAB002, 0xAAAAB003, 0xAAAAB004)
    preload_base = base_addr + stride * (len(values) - 1)
    _preload_elements(env, preload_base, values[::-1], size_bytes=4, stride_bytes=4)

    result = VectorLoadSequence(
        VectorMemTxn(
            req_id=0x129,
            is_load=True,
            opcode_class="stride",
            base_addr=base_addr,
            stride=stride,
            src_1=(1 << 64) + stride,
            lq_ptr=state.next_lq_ptr,
            sq_ptr=state.sq_ptr,
            vl=4,
            element_count=4,
            sew_bits=32,
            issue_port=0,
            enq_port=0,
        )
    ).run(env)

    _assert_stride_load_result(
        env,
        result,
        expected_data=_pack_expected_low_bits(result.expected),
        expected_vl=4,
        expected_blocks={preload_base & ~0x3F, base_addr & ~0x3F},
    )
