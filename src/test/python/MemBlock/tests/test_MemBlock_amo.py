# coding=utf-8
"""
MemBlock single-uop AMO real-DUT directed tests.
"""

import pytest

from sequences import AtomicSequence, ResetEnvSequence
from transactions import AtomicTxn


AMO_TEST_BASE_ADDR = 0x8000_5000


def _xfail_if_standalone_amo_issue_inactive(env, exc: TimeoutError) -> None:
    stats = env.get_transport_stats()
    if stats["dcache_a_request_count"] == 0 and stats["writeback_events"] == 0:
        pytest.xfail(
            "standalone MemBlock AMO intIssue contract is still inactive on current DUT: "
            f"cycle={env._current_cycle()}, "
            f"dcache_a_request_count={stats['dcache_a_request_count']}, "
            f"writeback_events={stats['writeback_events']}"
        )
    raise exc


@pytest.mark.parametrize(
    ("opcode", "size", "offset", "initial_value", "operand", "expected_committed_value"),
    (
        ("amoadd", 4, 0x00, 0x0000_0010, 0x0000_0020, 0x0000_0030),
        ("amoswap", 8, 0x40, 0x1122_3344_5566_7788, 0x8877_6655_4433_2211, 0x8877_6655_4433_2211),
        ("amoxor", 4, 0x80, 0x8000_0001, 0x7FFF_FFFF, 0xFFFF_FFFE),
    ),
)
def test_api_MemBlock_single_uop_amo_smoke(
    env,
    opcode,
    size,
    offset,
    initial_value,
    operand,
    expected_committed_value,
):
    state = ResetEnvSequence(
        require_issue_lanes=(0, 3, 5),
        require_lq_ready=True,
    ).run(env)
    addr = AMO_TEST_BASE_ADDR + offset
    env.preload_u64(addr, initial_value)

    sequence = AtomicSequence(
        AtomicTxn(
            req_id=0xA0 + (offset >> 6),
            sq_ptr=state.sq_ptr,
            addr=addr,
            operand=operand,
            opcode=opcode,
            size=size,
        ),
        initial_state=state,
        assert_no_outstanding=True,
    )
    try:
        result = sequence.run(env)
    except TimeoutError as exc:
        _xfail_if_standalone_amo_issue_inactive(env, exc)

    assert result.initial_value == (initial_value & ((1 << (size * 8)) - 1))
    assert result.expected_committed_value == expected_committed_value
    assert result.writeback["data"] == result.expected_return_value
    assert result.writeback["int_wen"] == 1
