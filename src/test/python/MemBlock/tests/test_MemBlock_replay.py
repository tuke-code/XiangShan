# coding=utf-8
"""
MemBlock 真实 DUT replay 场景测试。

当前覆盖三类首批稳定 replay：
  1. `FF`: store 地址已知、数据未就绪时，同地址 younger load 触发 forward-fail replay
  2. `DM`: cold cacheable load 触发 dcache miss replay
  3. `NC`: IO/uncache load 走 nc/outer 路径并出现 replay/nc_out 观测
"""

from request_apis import LoadTxn, QueuePtr, StoreTxn
from sequences.memblock_sequences import (
    FlushStoreBuffersSequence,
    ResetEnvSequence,
    ScalarCacheMissReplaySequence,
    ScalarForwardFailReplaySequence,
    ScalarNcReplaySequence,
    SequenceState,
)


FORWARD_FAIL_STORE_ADDR = 0x80000120
FORWARD_FAIL_STORE_DATA = 0xCAFEBABE11223344
CACHE_MISS_ADDR = 0x80000280
NC_REPLAY_ADDR = 0x2000
NC_REPLAY_DATA = 0x5566778899AABBCC


def _reset_env_and_state(env) -> SequenceState:
    return ResetEnvSequence(
        require_issue_lanes=(0, 3, 5),
        require_lq_ready=True,
        require_sq_ready=True,
    ).run(env)


def test_api_MemBlock_scalar_forward_fail_replay_smoke(env):
    """
    通过 `STA` 先于 `STD` 的真实 DUT 时序触发一次 `C_FF` replay。
    """

    initial_state = _reset_env_and_state(env)
    result = ScalarForwardFailReplaySequence(
        StoreTxn(
            req_id=0,
            sq_ptr=initial_state.sq_ptr,
            addr=FORWARD_FAIL_STORE_ADDR,
            data=FORWARD_FAIL_STORE_DATA,
        ),
        initial_lq_ptr=initial_state.next_lq_ptr,
        load_req_id=1,
        assert_no_outstanding=True,
    ).run(env)

    assert result.replay_event["cause"] == "FF", "FF replay 原因不匹配"
    assert result.replay_event["source"] in {"replay_queue", "replay_lane", "ldu"}, "FF replay 观测来源异常"
    assert result.replay_event["rob_idx_value"] == 1, "FF replay 的 load robIdx 不匹配"
    assert result.committed_store_view.committed, "FF replay 场景中的 store 未进入 committed"
    assert result.committed_store_view.addr == FORWARD_FAIL_STORE_ADDR, "FF replay store 地址不匹配"
    assert (int(result.committed_store_view.data) & ((1 << 64) - 1)) == FORWARD_FAIL_STORE_DATA

    drain_summary = FlushStoreBuffersSequence().run(env)
    assert drain_summary["drain_event_count"] > 0, "FF replay 场景未能在收尾阶段 drain store"
    env.assert_no_outstanding()


def test_api_MemBlock_scalar_cache_miss_replay_smoke(env):
    """
    单条 cold cacheable load 应先出现 `DM` replay，再完成正常写回。
    """

    initial_state = _reset_env_and_state(env)
    result = ScalarCacheMissReplaySequence(
        LoadTxn(
            req_id=0,
            addr=CACHE_MISS_ADDR,
            lq_ptr=initial_state.next_lq_ptr,
            sq_ptr=initial_state.sq_ptr,
        ),
        expected_completed_loads=1,
        assert_no_outstanding=True,
    ).run(env)

    assert result.replay_event["cause"] == "DM", "cache miss replay 原因不匹配"
    assert result.replay_event["source"] in {"replay_queue", "replay_lane", "ldu"}, "DM replay 观测来源异常"
    assert result.transport_stats_after["dcache_a_request_count"] > result.transport_stats_before["dcache_a_request_count"]
    assert result.transport_stats_after["dcache_d_response_count"] > result.transport_stats_before["dcache_d_response_count"]
    assert result.transport_stats_after["dcache_e_request_count"] > result.transport_stats_before["dcache_e_request_count"]
    assert result.transport_stats_after["outer_request_count"] == result.transport_stats_before["outer_request_count"]
    env.assert_no_outstanding()


def test_api_MemBlock_scalar_nc_replay_smoke(env):
    """
    单条 IO/uncache load 应观测到 `nc_out` 或 NC replay，并经 outer 路径完成。
    """

    initial_state = _reset_env_and_state(env)
    env.preload_u64(NC_REPLAY_ADDR, NC_REPLAY_DATA)
    result = ScalarNcReplaySequence(
        LoadTxn(
            req_id=0,
            addr=NC_REPLAY_ADDR,
            lq_ptr=initial_state.next_lq_ptr,
            sq_ptr=initial_state.sq_ptr,
        ),
        expected_completed_loads=1,
        assert_no_outstanding=True,
    ).run(env)

    assert result.replay_event["source"] in {"replay_queue", "replay_lane", "ldu", "nc_out"}, "NC replay 未出现在 NC 相关观测口"
    assert result.replay_event["cause"] == "NC", "NC replay 原因不匹配"
    assert result.transport_stats_after["outer_request_count"] > result.transport_stats_before["outer_request_count"]
    assert result.transport_stats_after["dcache_a_request_count"] == result.transport_stats_before["dcache_a_request_count"]
    env.assert_no_outstanding()


def test_api_MemBlock_small_replay_mix_smoke(env):
    """
    以固定模板串联 FF / DM / NC 三类 replay，验证真实 DUT 的小规模 replay 混合流。
    """

    state = _reset_env_and_state(env)

    ff_result = ScalarForwardFailReplaySequence(
        StoreTxn(
            req_id=0,
            sq_ptr=state.sq_ptr,
            addr=FORWARD_FAIL_STORE_ADDR,
            data=FORWARD_FAIL_STORE_DATA,
        ),
        initial_lq_ptr=state.next_lq_ptr,
        load_req_id=1,
    ).run(env)
    state = ff_result.final_state
    ff_drain_summary = FlushStoreBuffersSequence().run(env)
    assert ff_drain_summary["drain_event_count"] > 0, "mixed replay 中 FF 阶段的 store 未成功 drain"

    dm_result = ScalarCacheMissReplaySequence(
        LoadTxn(
            req_id=2,
            addr=CACHE_MISS_ADDR,
            lq_ptr=state.next_lq_ptr,
            sq_ptr=state.sq_ptr,
        ),
        expected_completed_loads=2,
    ).run(env)
    state = _reset_env_and_state(env)
    env.preload_u64(NC_REPLAY_ADDR, NC_REPLAY_DATA)
    nc_result = ScalarNcReplaySequence(
        LoadTxn(
            req_id=0,
            addr=NC_REPLAY_ADDR,
            lq_ptr=state.next_lq_ptr,
            sq_ptr=state.sq_ptr,
        ),
    ).run(env)

    causes = {ff_result.replay_event["cause"], dm_result.replay_event["cause"], nc_result.replay_event["cause"]}
    assert causes == {"FF", "DM", "NC"}, f"mixed replay 覆盖不完整: {causes}"
    assert nc_result.load_result.completed_load_count >= 1, "mixed replay 中 NC 阶段未完成 load compare"

    env.assert_no_outstanding()
