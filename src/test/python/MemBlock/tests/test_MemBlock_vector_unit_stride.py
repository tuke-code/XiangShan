# coding=utf-8
"""
MemBlock 向量 load 的真实 DUT smoke 用例。
"""

from sequences import ResetEnvSequence, VectorLoadSequence
from transactions import VectorMemTxn


VECTOR_ADDR_BASE = 0x80004000


def _reset_env_and_state(env):
    return ResetEnvSequence(require_lq_ready=True).run(env)


def _preload_elements(env, base_addr: int, values, *, size_bytes: int, stride_bytes: int | None = None) -> None:
    stride_bytes = size_bytes if stride_bytes is None else int(stride_bytes)
    for idx, value in enumerate(values):
        env.memory.preload_bytes(
            int(base_addr) + idx * stride_bytes,
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


def _pack_expected_element_slots(expectation) -> int:
    packed = 0
    for access in expectation.accesses:
        if not access.should_access_memory:
            continue
        shift = int(access.element_idx) * int(access.size_bytes) * 8
        packed |= int(access.expected_load_data) << shift
    return packed


def _select_vector_data_writeback(vector_result) -> dict:
    for writeback in vector_result.observed_writebacks:
        if writeback["vec_wen"]:
            return writeback
    return vector_result.observed_writebacks[0]


def _assert_vector_load_result(
    env,
    result,
    *,
    expected_data: int,
    expected_vl: int,
    expected_strided: int,
    expected_address: int | None = None,
    expected_block_addresses: set[int] | None = None,
):
    vector_result = result.vector_result
    data_writeback = _select_vector_data_writeback(vector_result)
    transport_stats = env.get_transport_stats()

    assert vector_result.completed and not vector_result.trapped
    assert data_writeback["vec_wen"] == 1, f"未观测到 vec data writeback: {vector_result.observed_writebacks!r}"
    assert data_writeback["is_vec_load"] == 1
    assert data_writeback["is_strided"] == expected_strided
    assert vector_result.observed_vl == expected_vl
    assert data_writeback["data"] == expected_data
    assert transport_stats["dcache_a_request_count"] >= 1
    assert transport_stats["dcache_d_response_count"] >= 2
    if expected_address is not None:
        assert transport_stats["last_dcache_a_address"] == expected_address
    if expected_block_addresses is not None:
        assert transport_stats["last_dcache_a_block_address"] in expected_block_addresses
    env.assert_no_outstanding()


def test_api_MemBlock_vector_unit_stride_load_smoke(env):
    """验证 unit-stride vector load 能经 `enqLsq + vecIssue` 完成并返回预期低位数据。"""

    state = _reset_env_and_state(env)

    values = (0x11223344, 0x55667788)
    _preload_elements(env, VECTOR_ADDR_BASE, values, size_bytes=4)

    result = VectorLoadSequence(
        VectorMemTxn(
            req_id=0x101,
            is_load=True,
            opcode_class="unit_stride",
            base_addr=VECTOR_ADDR_BASE,
            lq_ptr=state.next_lq_ptr,
            sq_ptr=state.sq_ptr,
                vl=2,
                element_count=2,
                sew_bits=32,
                issue_port=0,
                enq_port=0,
            )
    ).run(env)

    expected_low_bits = _pack_expected_low_bits(result.expected)
    _assert_vector_load_result(
        env,
        result,
        expected_data=expected_low_bits,
        expected_vl=2,
        expected_strided=0,
        expected_address=VECTOR_ADDR_BASE,
    )


def test_api_MemBlock_vector_unit_stride_load_nonzero_vstart_smoke(env):
    """验证 non-zero `vstart` 的 unit-stride load 会在 writeback data 中保留 prestart 空洞。"""

    state = _reset_env_and_state(env)

    values = (0x11111111, 0x22222222, 0x33333333, 0x44444444)
    _preload_elements(env, VECTOR_ADDR_BASE, values, size_bytes=4)

    result = VectorLoadSequence(
        VectorMemTxn(
            req_id=0x123,
            is_load=True,
            opcode_class="unit_stride",
            base_addr=VECTOR_ADDR_BASE,
            lq_ptr=state.next_lq_ptr,
            sq_ptr=state.sq_ptr,
            vl=4,
            element_count=4,
            sew_bits=32,
            vstart=1,
            issue_port=0,
            enq_port=0,
        )
    ).run(env)

    expected_slot_image = _pack_expected_element_slots(result.expected)
    _assert_vector_load_result(
        env,
        result,
        expected_data=expected_slot_image,
        expected_vl=4,
        expected_strided=0,
        expected_address=VECTOR_ADDR_BASE,
    )
    assert (_select_vector_data_writeback(result.vector_result)["data"] & 0xFFFFFFFF) == 0


def test_api_MemBlock_vector_unit_stride_load_wide_smoke(env):
    """验证 8x16b unit-stride load 可在同一 merge entry 中完成宽数据合并。"""

    state = _reset_env_and_state(env)
    values = (0x0101, 0x1212, 0x2323, 0x3434, 0x4545, 0x5656, 0x6767, 0x7878)
    _preload_elements(env, VECTOR_ADDR_BASE, values, size_bytes=2)

    result = VectorLoadSequence(
        VectorMemTxn(
            req_id=0x124,
            is_load=True,
            opcode_class="unit_stride",
            base_addr=VECTOR_ADDR_BASE,
            lq_ptr=state.next_lq_ptr,
            sq_ptr=state.sq_ptr,
            vl=8,
            element_count=8,
            sew_bits=16,
            issue_port=0,
            enq_port=0,
        )
    ).run(env)

    _assert_vector_load_result(
        env,
        result,
        expected_data=_pack_expected_low_bits(result.expected),
        expected_vl=8,
        expected_strided=0,
        expected_address=VECTOR_ADDR_BASE,
    )


def test_api_MemBlock_vector_unit_stride_load_cross_16b_unaligned_smoke(env):
    """验证跨 16B 且非对齐的 unit-stride load 仍能得到正确的 merge 数据。"""

    state = _reset_env_and_state(env)
    base_addr = VECTOR_ADDR_BASE + 0x3
    values = (0x1111, 0x2222, 0x3333, 0x4444, 0x5555, 0x6666, 0x7777, 0x8888)
    _preload_elements(env, base_addr, values, size_bytes=2)

    result = VectorLoadSequence(
        VectorMemTxn(
            req_id=0x125,
            is_load=True,
            opcode_class="unit_stride",
            base_addr=base_addr,
            lq_ptr=state.next_lq_ptr,
            sq_ptr=state.sq_ptr,
            vl=8,
            element_count=8,
            sew_bits=16,
            issue_port=0,
            enq_port=0,
        )
    ).run(env)

    _assert_vector_load_result(
        env,
        result,
        expected_data=_pack_expected_low_bits(result.expected),
        expected_vl=8,
        expected_strided=0,
        expected_block_addresses={VECTOR_ADDR_BASE, VECTOR_ADDR_BASE + 64},
    )


def test_api_MemBlock_vector_unit_stride_load_byte_dense_smoke(env):
    """验证 16x8b unit-stride load 能覆盖 byte-dense 的 merge 路径。"""

    state = _reset_env_and_state(env)
    values = (
        0x10, 0x21, 0x32, 0x43, 0x54, 0x65, 0x76, 0x87,
        0x98, 0xA9, 0xBA, 0xCB, 0xDC, 0xED, 0xFE, 0x0F,
    )
    _preload_elements(env, VECTOR_ADDR_BASE, values, size_bytes=1)

    result = VectorLoadSequence(
        VectorMemTxn(
            req_id=0x126,
            is_load=True,
            opcode_class="unit_stride",
            base_addr=VECTOR_ADDR_BASE,
            lq_ptr=state.next_lq_ptr,
            sq_ptr=state.sq_ptr,
            vl=16,
            element_count=16,
            sew_bits=8,
            issue_port=0,
            enq_port=0,
        )
    ).run(env)

    _assert_vector_load_result(
        env,
        result,
        expected_data=_pack_expected_low_bits(result.expected),
        expected_vl=16,
        expected_strided=0,
        expected_address=VECTOR_ADDR_BASE,
    )


def test_api_MemBlock_vector_unit_stride_load_checkerboard_mask_smoke(env):
    """验证 checkerboard mask 的 unit-stride load 会保留被屏蔽 element 的空槽。"""

    state = _reset_env_and_state(env)
    values = (0x1001, 0x2002, 0x3003, 0x4004, 0x5005, 0x6006, 0x7007, 0x8008)
    _preload_elements(env, VECTOR_ADDR_BASE, values, size_bytes=2)

    result = VectorLoadSequence(
        VectorMemTxn(
            req_id=0x12A,
            is_load=True,
            opcode_class="unit_stride",
            base_addr=VECTOR_ADDR_BASE,
            lq_ptr=state.next_lq_ptr,
            sq_ptr=state.sq_ptr,
            vl=8,
            element_count=8,
            sew_bits=16,
            mask_bits=(1, 0, 1, 0, 1, 0, 1, 0),
            vm=False,
            issue_port=0,
            enq_port=0,
        )
    ).run(env)

    expected_slot_image = _pack_expected_element_slots(result.expected)
    _assert_vector_load_result(
        env,
        result,
        expected_data=expected_slot_image,
        expected_vl=8,
        expected_strided=0,
        expected_address=VECTOR_ADDR_BASE,
    )
    assert (_select_vector_data_writeback(result.vector_result)["data"] >> 16) & 0xFFFF == 0


def test_api_MemBlock_vector_unit_stride_load_port1_smoke(env):
    """验证第二个 vector enqueue/issue 端口也能打通到 VLMergeBuffer。"""

    state = _reset_env_and_state(env)
    values = (0x89ABCDEF, 0x01234567)
    _preload_elements(env, VECTOR_ADDR_BASE + 0x20, values, size_bytes=4)

    result = VectorLoadSequence(
        VectorMemTxn(
            req_id=0x127,
            is_load=True,
            opcode_class="unit_stride",
            base_addr=VECTOR_ADDR_BASE + 0x20,
            lq_ptr=state.next_lq_ptr,
            sq_ptr=state.sq_ptr,
            vl=2,
            element_count=2,
            sew_bits=32,
            issue_port=1,
            enq_port=1,
        )
    ).run(env)

    _assert_vector_load_result(
        env,
        result,
        expected_data=_pack_expected_low_bits(result.expected),
        expected_vl=2,
        expected_strided=0,
        expected_block_addresses={VECTOR_ADDR_BASE},
    )
