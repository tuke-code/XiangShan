# coding=utf-8
"""
MemBlock 标量 load pipeline 的定向真实 DUT 用例。
"""

from transactions import QueuePtr
from sequences import (
    ResetEnvSequence,
    ScalarLoadBurstSequence,
    SequenceState,
)


CACHEABLE_LOAD_BASE = 0x80001000
DIRECTED_LOAD_COUNT = 8


def _reset_env_and_state(env) -> SequenceState:
    return ResetEnvSequence(
        require_issue_lanes=(0,),
        require_lq_ready=True,
    ).run(env)


def test_api_MemBlock_small_cacheable_load_burst_directed(env):
    """
    小规模 cacheable load burst。

    检查点：
      - 请求地址均落在 cacheable 空间
      - 所有 load 均完成在线 compare
      - 流量经 dcache A/D/E，而非 outer/uncache
    """

    state = _reset_env_and_state(env)
    requests = []
    for req_id in range(DIRECTED_LOAD_COUNT):
        addr = CACHEABLE_LOAD_BASE + req_id * 8
        env.preload_u64(addr, 0x1111_0000_0000_0000 + req_id)
        requests.append((req_id, addr))

    stats_before = env.get_transport_stats()
    result = ScalarLoadBurstSequence(
        requests,
        initial_state=state,
        assert_no_outstanding=True,
    ).run(env)

    assert result.completed_load_count == DIRECTED_LOAD_COUNT, "定向 load burst 未完成全部 compare"
    assert result.final_state.next_lq_ptr == QueuePtr(flag=0, value=DIRECTED_LOAD_COUNT), "LQ 指针推进异常"
    assert result.transport_stats_after["dcache_a_request_count"] > stats_before["dcache_a_request_count"], (
        "定向 cacheable load 未通过 dcache A 发出请求"
    )
    assert result.transport_stats_after["dcache_d_response_count"] > stats_before["dcache_d_response_count"], (
        "定向 cacheable load 未观测到 dcache D 响应"
    )
    assert result.transport_stats_after["dcache_e_request_count"] > stats_before["dcache_e_request_count"], (
        "定向 cacheable load 未观测到 dcache E 握手"
    )
    assert result.transport_stats_after["outer_request_count"] == stats_before["outer_request_count"], (
        "定向 cacheable load 不应走 outer/uncache 路径"
    )
