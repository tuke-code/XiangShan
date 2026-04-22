# coding=utf-8
"""
MemBlock 标量 load pipeline 的定向真实 DUT 用例。
"""

from transactions import QueuePtr
from sequences import (
    ResetEnvSequence,
    ScalarLoadBurstSequence,
    ScalarLoadSaturationSequence,
    SequenceState,
)


CACHEABLE_LOAD_BASE = 0x80001000
DIRECTED_LOAD_COUNT = 8
SATURATION_LOAD_BASE = 0x80002000
SATURATION_LOAD_COUNT = 24
SATURATION_ADDR_STRIDE = 0x40


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


def test_api_MemBlock_back_to_back_cacheable_load_saturation(env):
    """
    连续多拍按 load pipeline 宽度打满 `enqLsq + intIssue` 的 cacheable load 饱和流。

    检查点：
      - 每拍按 `load_pipeline_width` 持续尝试 enqueue + issue 标量 load
      - `intIssue.ready` 出现反压时，未握手 lane 会在后续拍继续发射
      - 所有 load 均完成在线 compare
      - 流量经 dcache A/D/E，而非 outer/uncache
      - 场景结束后 LQ 指针、completed 计数和 outstanding 状态都正确
    """

    state = _reset_env_and_state(env)
    requests = []
    for req_id in range(SATURATION_LOAD_COUNT):
        addr = SATURATION_LOAD_BASE + req_id * SATURATION_ADDR_STRIDE
        env.preload_u64(addr, 0x5A5A_0000_0000_0000 + req_id)
        requests.append((0x100 + req_id, addr))

    stats_before = env.get_transport_stats()
    result = ScalarLoadSaturationSequence(
        requests,
        initial_state=state,
        assert_no_outstanding=True,
    ).run(env)

    assert len(result.issued_loads) == SATURATION_LOAD_COUNT, "饱和 load 流未发出全部请求"
    assert result.completed_load_count == SATURATION_LOAD_COUNT, "饱和 load 流未完成全部 compare"
    assert result.final_state.next_lq_ptr == QueuePtr(flag=0, value=SATURATION_LOAD_COUNT), "饱和 load 流 LQ 指针推进异常"
    assert env.issue[0].ready.value == 1, "饱和 load 流结束后 `issue[0]` 不再 ready"
    assert env.issue[1].ready.value == 1, "饱和 load 流结束后 `issue[1]` 不再 ready"
    assert env.issue[2].ready.value == 1, "饱和 load 流结束后 `issue[2]` 不再 ready"
    assert env.lsq_status.lqCanAccept.value == 1, "饱和 load 流结束后 `lqCanAccept` 变为 0"
    assert result.transport_stats_after["dcache_a_request_count"] > stats_before["dcache_a_request_count"], (
        "饱和 cacheable load 未通过 dcache A 发出请求"
    )
    assert result.transport_stats_after["dcache_d_response_count"] > stats_before["dcache_d_response_count"], (
        "饱和 cacheable load 未观测到 dcache D 响应"
    )
    assert result.transport_stats_after["dcache_e_request_count"] > stats_before["dcache_e_request_count"], (
        "饱和 cacheable load 未观测到 dcache E 握手"
    )
    assert result.transport_stats_after["outer_request_count"] == stats_before["outer_request_count"], (
        "饱和 cacheable load 不应走 outer/uncache 路径"
    )
