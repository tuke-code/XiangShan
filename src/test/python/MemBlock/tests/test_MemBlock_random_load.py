# coding=utf-8
"""
MemBlock 随机 load 流量测试。

本文件覆盖两类 PMA 默认地址空间下的随机标量 load：
  1. `random_io`: 请求地址 `< 0x80000000`，应走 IO / uncache 路径
  2. `mem_io`: 请求地址 `> 0x80000000`，应走 cacheable / dcache 路径

各用例均按 `lsq enqueue -> issue` 的顺序发起 load：
  1. 先向 `enqLsq[0]` 提交一条 load，显式分配 `lqIdx`
  2. 再把同一条指令的 `lqIdx/sqIdx` 送到 `intIssue[0]`
  3. 通过 `MemoryModel` 预加载数据并校验 `mem_to_ooo.intWriteback`
"""

import random

from request_apis import (
    LoadTxn,
)
from sequences.memblock_sequences import (
    ResetEnvSequence,
    ScalarLoadBurstSequence,
    ScalarLoadSequence,
    SequenceState,
)


RANDOM_SEED = 20260324
RANDOM_LOAD_COUNT = 1000
IO_ADDR_LIMIT = 0x80000000
MEM_ADDR_BASE = 0x80000008
RANDOM_ADDR_WINDOW_WORDS = 1 << 20


def _reset_env_and_state(env) -> SequenceState:
    """
    执行一次完整复位，并同步重建测试侧状态。

    约束：
      - 用例开始前必须先经过一次 reset/deassert
      - 用例执行过程中若再次 reset，软件侧维护的索引状态必须同步清零
    """

    return ResetEnvSequence(
        require_issue_lanes=(0,),
        require_lq_ready=True,
    ).run(env)


def _random_addr(rng: random.Random, addr_base: int) -> int:
    return addr_base + (rng.randrange(0, RANDOM_ADDR_WINDOW_WORDS) << 3)


def _prepare_random_requests(env, addr_base: int) -> list[tuple[int, int]]:
    rng = random.Random(RANDOM_SEED)
    requests = []
    for req_id in range(RANDOM_LOAD_COUNT):
        addr = _random_addr(rng, addr_base)
        env.preload_u64(addr, rng.getrandbits(64))
        requests.append((req_id, addr))
    return requests


def _run_random_load_requests(env, requests: list[tuple[int, int]]):
    completed_before = env.get_completed_load_count()
    stream_state = _reset_env_and_state(env)
    result = ScalarLoadBurstSequence(
        requests,
        initial_state=stream_state,
    ).run(env)

    assert env.issue[0].ready.value == 1, "随机 load 流结束后 `issue[0]` 不再 ready"
    assert env.lsq_status.lqCanAccept.value == 1, "随机 load 流结束后 `lqCanAccept` 变为 0"
    assert env.get_completed_load_count() == completed_before + RANDOM_LOAD_COUNT, (
        "1000 个随机 load 未全部完成校验"
    )
    env.assert_no_outstanding()
    return result


def _assert_random_io_transport(stats_before: dict[str, int], stats_after: dict[str, int]) -> None:
    assert stats_after["outer_request_count"] > stats_before["outer_request_count"], (
        "random_io 场景未通过 outer/uncache 端口发出请求"
    )
    assert stats_after["dcache_a_request_count"] == stats_before["dcache_a_request_count"], (
        "random_io 场景不应通过 dcache A 端口发出请求"
    )
    assert stats_after["dcache_d_response_count"] == stats_before["dcache_d_response_count"], (
        "random_io 场景不应消耗 dcache D 响应"
    )
    assert stats_after["dcache_e_request_count"] == stats_before["dcache_e_request_count"], (
        "random_io 场景不应产生 dcache E 通道握手"
    )


def _assert_mem_io_transport(stats_before: dict[str, int], stats_after: dict[str, int]) -> None:
    assert stats_after["outer_request_count"] == stats_before["outer_request_count"], (
        "mem_io 场景不应通过 outer/uncache 端口发出请求"
    )
    assert stats_after["dcache_a_request_count"] > stats_before["dcache_a_request_count"], (
        "mem_io 场景未通过 dcache A 端口发出请求"
    )
    assert stats_after["dcache_d_response_count"] > stats_before["dcache_d_response_count"], (
        "mem_io 场景未观测到 dcache D 响应"
    )
    assert stats_after["dcache_e_request_count"] > stats_before["dcache_e_request_count"], (
        "mem_io 场景未完成 dcache GrantAck 握手"
    )


def test_api_MemBlock_single_preloaded_load_data_check(env):
    """
    预加载一笔 64-bit 数据，并检查单笔 load 写回结果。

    检查点：
      - `MemoryModel.preload_u64` 能向 backing store 预装数据
      - MemBlock 发起 load 后，`mem_to_ooo.intWriteback` 的 `robIdx/pdest/data` 与期望一致
    """

    req_id = 0
    load_addr = 0x1000
    expected_data = 0x1122334455667788

    stream_state = _reset_env_and_state(env)
    env.preload_u64(load_addr, expected_data)

    txn = LoadTxn(
        req_id=req_id,
        addr=load_addr,
        lq_ptr=stream_state.next_lq_ptr,
        sq_ptr=stream_state.sq_ptr,
        size=8,
        mask=0xFF,
    )
    ScalarLoadSequence(
        txn,
        drain_cycles=200,
        expected_completed_loads=1,
        assert_no_outstanding=True,
    ).run(env)


def test_api_MemBlock_random_io_1000_load_requests(env):
    """
    按 `lsq -> issue` 顺序持续推进 1000 个随机 IO/uncache load 请求。

    检查点：
      - 请求地址均 `< 0x80000000`
      - 请求经 outer/uncache 端口发出，而非 dcache A/D/E
      - 所有请求最终经 `mem_to_ooo.intWriteback` 正确写回
    """

    requests = _prepare_random_requests(env, addr_base=0)
    assert all(0 <= addr < IO_ADDR_LIMIT for _, addr in requests), "random_io 请求地址超出 IO 地址空间"
    result = _run_random_load_requests(env, requests)
    _assert_random_io_transport(result.transport_stats_before, result.transport_stats_after)


def test_api_MemBlock_mem_io_1000_load_requests(env):
    """
    按 `lsq -> issue` 顺序持续推进 1000 个随机 cacheable load 请求。

    检查点：
      - 请求地址均 `> 0x80000000`
      - 请求经 dcache A/D/E 通道完成，而非 outer/uncache
      - 所有请求最终经 `mem_to_ooo.intWriteback` 正确写回
    """

    requests = _prepare_random_requests(env, addr_base=MEM_ADDR_BASE)
    assert all(addr > IO_ADDR_LIMIT for _, addr in requests), "mem_io 请求地址未落入 cacheable 地址空间"
    result = _run_random_load_requests(env, requests)
    _assert_mem_io_transport(result.transport_stats_before, result.transport_stats_after)
