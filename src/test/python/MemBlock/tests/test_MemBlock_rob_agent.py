# coding=utf-8
"""
ROB agent unit tests.
"""

import pytest

from agents.rob_agent import RobAgent


def test_api_MemBlock_rob_agent_single_load_packet():
    """单条 load 完成后应生成 lcommit packet。"""

    agent = RobAgent(dut=None, rob_size=8)
    agent.note_load_issued(0, 1)
    agent.note_load_completed(0, 1)
    agent.drive()
    packet = agent.latest_commit_packet
    assert packet.commit
    assert packet.lcommit == 1
    assert packet.scommit == 0
    assert packet.pending_ptr_before.value == 0
    assert packet.pending_ptr_next.value == 1
    agent.advance()
    assert agent.pending_ptr.value == 1


def test_api_MemBlock_rob_agent_store_requires_ready_and_token():
    """store 只有在 ready 与 token 同时满足时才能提交。"""

    agent = RobAgent(dut=None, rob_size=8)
    agent.note_store_allocated(0, 1, sq_idx_flag=0, sq_idx_value=3)
    agent.mark_store_data_ready(0, 3)
    agent.queue_store_commit(1)
    agent.drive()
    blocked_packet = agent.latest_commit_packet
    assert not blocked_packet.commit
    assert blocked_packet.blocked_by == "store_not_ready"
    agent.advance()

    agent.mark_store_addr_ready(0, 3)
    agent.queue_store_commit(1)
    agent.drive()
    packet = agent.latest_commit_packet
    assert packet.commit
    assert packet.lcommit == 0
    assert packet.scommit == 1
    assert packet.pending_ptr_next.value == 1


def test_api_MemBlock_rob_agent_mixed_commit_packet():
    """同拍 older load + younger ready store 应形成 mixed commit packet。"""

    agent = RobAgent(dut=None, rob_size=8)
    agent.note_load_issued(0, 1)
    agent.note_store_allocated(0, 2, sq_idx_flag=0, sq_idx_value=5)
    agent.note_load_completed(0, 1)
    agent.mark_store_data_ready(0, 5)
    agent.mark_store_addr_ready(0, 5)
    agent.queue_store_commit(1)
    agent.drive()
    packet = agent.latest_commit_packet
    assert packet.commit
    assert packet.lcommit == 1
    assert packet.scommit == 1
    assert len(packet.committed_entries) == 2
    assert packet.pending_ptr_next.value == 2


def test_api_MemBlock_rob_agent_head_store_blocks_younger_when_unready():
    """older unready store 不应被 token 放行，也不应允许 younger 越过。"""

    agent = RobAgent(dut=None, rob_size=8)
    agent.note_store_allocated(0, 1, sq_idx_flag=0, sq_idx_value=7)
    agent.note_load_issued(0, 2)
    agent.note_load_completed(0, 2)
    agent.queue_store_commit(1)
    agent.drive()
    packet = agent.latest_commit_packet
    assert not packet.commit
    assert packet.lcommit == 0
    assert packet.scommit == 0
    assert packet.blocked_by == "store_not_ready"
    assert packet.pending_ptr_before == packet.pending_ptr_next
    assert agent.stats["rob_store_blocks_younger_count"] == 1


def test_api_MemBlock_rob_agent_non_mem_blocker_blocks_and_releases_frontier():
    """non-mem blocker release 前阻塞 frontier，release 后恢复推进。"""

    agent = RobAgent(dut=None, rob_size=8)
    agent.note_load_issued(0, 1)
    agent.note_load_completed(0, 1)
    agent.note_non_mem_issued(0, 2)
    agent.note_store_allocated(0, 3, sq_idx_flag=0, sq_idx_value=4)
    agent.mark_store_data_ready(0, 4)
    agent.mark_store_addr_ready(0, 4)
    agent.queue_store_commit(1)
    agent.drive()
    packet = agent.latest_commit_packet
    assert packet.commit
    assert packet.lcommit == 1
    assert packet.scommit == 0
    agent.advance()

    agent.queue_store_commit(1)
    agent.drive()
    blocked_packet = agent.latest_commit_packet
    assert not blocked_packet.commit
    assert blocked_packet.blocked_by == "non_mem_blocked"
    agent.advance()

    agent.release_non_mem(0, 2)
    agent.queue_store_commit(1)
    agent.drive()
    resumed_packet = agent.latest_commit_packet
    assert resumed_packet.commit
    assert resumed_packet.lcommit == 0
    assert resumed_packet.scommit == 1
    assert agent.stats["rob_non_mem_resume_count"] == 1


def test_api_MemBlock_rob_agent_explicit_store_ready_override_can_release_entry():
    """显式 ready 接口可在未走 addr/data 聚合时直接放行 store。"""

    agent = RobAgent(dut=None, rob_size=8)
    agent.note_store_allocated(0, 1, sq_idx_flag=1, sq_idx_value=6)
    agent.mark_store_commit_ready(1, 6, ready=True)
    agent.queue_store_commit(1)
    agent.drive()
    packet = agent.latest_commit_packet
    assert packet.commit
    assert packet.scommit == 1
    assert agent.stats["rob_store_explicit_ready_count"] == 1


def test_api_MemBlock_rob_agent_ready_store_without_token_stays_blocked():
    """ready store 在没有 token 时仍不得提交。"""

    agent = RobAgent(dut=None, rob_size=8)
    agent.note_store_allocated(0, 1, sq_idx_flag=0, sq_idx_value=2)
    agent.mark_store_data_ready(0, 2)
    agent.mark_store_addr_ready(0, 2)
    agent.drive()
    packet = agent.latest_commit_packet
    assert not packet.commit
    assert packet.blocked_by == "store_token_unavailable"
    assert agent.stats["rob_store_ready_without_token_count"] == 1


def test_api_MemBlock_rob_agent_completion_before_issue_is_buffered():
    """先看到完成事件时，后续 issue 应继承 orphan completion。"""

    agent = RobAgent(dut=None, rob_size=8)
    agent.note_load_completed(0, 3)
    agent.note_load_issued(0, 3)
    agent.drive()
    packet = agent.latest_commit_packet
    assert packet.commit
    assert packet.lcommit == 1


def test_api_MemBlock_rob_agent_can_seed_pending_ptr_at_wrap_boundary():
    agent = RobAgent(dut=None, rob_size=8)

    agent.set_pending_ptr(type("Ptr", (), {"flag": 1, "value": 7})())

    assert agent.pending_ptr.flag == 1
    assert agent.pending_ptr.value == 7
    assert agent.pending_ptr_next == agent.pending_ptr
    assert agent.latest_commit_packet.pending_ptr_before == agent.pending_ptr


def test_api_MemBlock_rob_agent_rejects_pending_ptr_seed_with_outstanding_entries():
    agent = RobAgent(dut=None, rob_size=8)
    agent.note_load_issued(0, 1)

    with pytest.raises(RuntimeError, match="outstanding entries exist"):
        agent.set_pending_ptr(type("Ptr", (), {"flag": 0, "value": 7})())
