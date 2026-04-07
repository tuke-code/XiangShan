# coding=utf-8
"""
ROB agent unit tests.
"""

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


def test_api_MemBlock_rob_agent_single_store_packet():
    """单条 store token 应生成 scommit packet。"""

    agent = RobAgent(dut=None, rob_size=8)
    agent.note_store_allocated(0, 1)
    agent.queue_store_commit(1)
    agent.drive()
    packet = agent.latest_commit_packet
    assert packet.commit
    assert packet.lcommit == 0
    assert packet.scommit == 1
    assert packet.pending_ptr_next.value == 1
    agent.advance()
    assert agent.pending_ptr.value == 1


def test_api_MemBlock_rob_agent_mixed_commit_packet():
    """同拍 older load + younger store 应形成 mixed commit packet。"""

    agent = RobAgent(dut=None, rob_size=8)
    agent.note_load_issued(0, 1)
    agent.note_store_allocated(0, 2)
    agent.note_load_completed(0, 1)
    agent.queue_store_commit(1)
    agent.drive()
    packet = agent.latest_commit_packet
    assert packet.commit
    assert packet.lcommit == 1
    assert packet.scommit == 1
    assert len(packet.committed_entries) == 2
    assert packet.pending_ptr_next.value == 2


def test_api_MemBlock_rob_agent_head_blocks_younger_entry():
    """head 未就绪时 younger entry 不应越过 frontier。"""

    agent = RobAgent(dut=None, rob_size=8)
    agent.note_store_allocated(0, 1)
    agent.note_load_issued(0, 2)
    agent.note_load_completed(0, 2)
    agent.drive()
    packet = agent.latest_commit_packet
    assert not packet.commit
    assert packet.lcommit == 0
    assert packet.scommit == 0
    assert packet.pending_ptr_before == packet.pending_ptr_next


def test_api_MemBlock_rob_agent_completion_before_issue_is_buffered():
    """先看到完成事件时，后续 issue 应继承 orphan completion。"""

    agent = RobAgent(dut=None, rob_size=8)
    agent.note_load_completed(0, 3)
    agent.note_load_issued(0, 3)
    agent.drive()
    packet = agent.latest_commit_packet
    assert packet.commit
    assert packet.lcommit == 1
