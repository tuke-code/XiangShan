# coding=utf-8
"""
MemBlockEnv 冒烟测试。

这些测试只验证 env 自身、Bundle 绑定和 MemoryModel 的工作状态，
不依赖 MemBlock 的具体功能实现。
"""

import pytest
import MemBlock_api
from transactions import QueuePtr, VectorMemTxn

def test_api_MemBlock_env_create(env):
    """验证 env、dut 和 MemoryModel 能正常创建。"""

    assert env is not None
    assert env.dut is not None
    assert env.memory is not None
    assert env.mock_outer_buffer is not None
    assert env.mock_dcache_client is not None
    assert env.mock_csr is not None
    assert env.mmu is not None
    assert env.csr_agent is not None
    assert env.commit_agent is not None
    assert env.lsq_agent is not None
    assert env.issue_agent is not None
    assert env.rob_agent is not None
    assert env.backend is not None
    assert env.config.rob_size == 512
    assert env.memory.strict_writeback_check is True


def test_api_MemBlock_env_backend_facade_wires_existing_agents(env):
    """验证统一 backend facade 复用了现有 agent。"""

    assert env.backend.lsq is env.lsq_agent
    assert env.backend.issue is env.issue_agent
    assert env.backend.vector_issue is env.vector_issue_agent
    assert env.backend.rob is env.rob_agent
    assert env.backend.commit is env.commit_agent


def test_api_MemBlock_env_has_core_bundles(env):
    """验证核心 Bundle 分组齐全。"""

    assert hasattr(env, "redirect")
    assert hasattr(env, "tlb_csr")
    assert hasattr(env, "csr_ctrl")
    assert hasattr(env, "lsq_enq_meta")
    assert hasattr(env, "lsq_enq_req")
    assert hasattr(env, "lsq_enq_resp")
    assert hasattr(env, "issue")
    assert hasattr(env, "vector_issue")
    assert hasattr(env, "writeback")
    assert hasattr(env, "vector_writeback")
    assert hasattr(env, "store_data_inputs")
    assert hasattr(env, "store_addr_inputs")
    assert hasattr(env, "store_mask_inputs")
    assert hasattr(env, "store_addr_re_inputs")
    assert hasattr(env, "sbuffer_writes")
    assert hasattr(env, "sq_shadow_entries")
    assert hasattr(env, "mem_status")
    assert hasattr(env, "lsq_status")
    assert hasattr(env, "outer_tl_a")
    assert hasattr(env, "outer_tl_d")
    assert hasattr(env, "dcache_a")
    assert hasattr(env, "dcache_b")
    assert hasattr(env, "dcache_c")
    assert hasattr(env, "dcache_d")
    assert hasattr(env, "dcache_e")
    assert len(env.lsq_enq_req) == env.config.lsq_enq_ports
    assert len(env.lsq_enq_resp) == env.config.lsq_enq_ports
    assert len(env.issue) == env.config.int_issue_ports
    assert len(env.vector_issue) == 2
    assert len(env.writeback) == env.config.int_writeback_ports
    assert len(env.vector_writeback) == 2
    assert len(env.store_data_inputs) == env.config.store_pipeline_width
    assert len(env.store_addr_inputs) == env.config.store_pipeline_width
    assert len(env.store_mask_inputs) == env.config.store_pipeline_width
    assert len(env.store_addr_re_inputs) == env.config.store_pipeline_width
    assert len(env.sbuffer_writes) == env.config.sbuffer_write_ports
    assert len(env.sq_shadow_entries) == env.config.store_queue_size


def test_api_MemBlock_env_advance_cycles_and_reset(env):
    """验证新 clock facade 能正常复位并推进时钟。"""

    env.reset(cycles=2, settle_cycles=5)
    env.advance_cycles(3)
    env.reset(cycles=2, settle_cycles=1)
    env.advance_cycles(2)
    assert env.dut.reset.value == 0


def test_api_MemBlock_env_lsq_enq_bundle_access(env):
    """验证 LSQ 入队请求和元信息封装。"""

    env.lsq_enq_meta.need_alloc[0].value = 1
    env.lsq_enq_req[0].valid.value = 1
    env.lsq_enq_req[0].bits_fuType.value = 0x12
    env.lsq_enq_req[0].bits_uopIdx.value = 0x3
    env.lsq_enq_req[0].bits_robIdx_value.value = 0x11
    env.lsq_enq_req[0].bits_lqIdx_value.value = 0x22
    env.lsq_enq_req[0].bits_sqIdx_value.value = 0x7
    env.advance_cycles(1)
    env.idle_inputs()
    env.advance_cycles(1)
    assert env.lsq_enq_req[0].valid.value == 0
    _ = env.lsq_enq_meta.canAccept.value
    _ = env.lsq_enq_resp[0].lqIdx_value.value


def test_api_MemBlock_env_issue_bundle_access(env):
    """验证 7 路 issue 端口可访问。"""

    for idx, bundle in enumerate(env.issue):
        bundle.valid.value = 1
        bundle.bits_fuOpType.value = idx + 1
        bundle.bits_src_0.value = idx + 2
        bundle.bits_robIdx_value.value = idx + 3
        bundle.bits_sqIdx_value.value = idx + 4
    env.advance_cycles(1)
    env.idle_inputs()
    env.advance_cycles(1)
    for bundle in env.issue:
        assert bundle.valid.value == 0
        _ = bundle.ready.value


def test_api_MemBlock_env_mem_status_access(env):
    """验证 mem_to_ooo 相关状态口可读。"""

    env.advance_cycles(1)
    _ = env.mem_status.lqDeq.value
    _ = env.mem_status.sqDeq.value
    _ = env.mem_status.lqDeqPtr_value.value
    _ = env.mem_status.sqDeqPtr_value.value
    _ = env.mem_status.sbIsEmpty.value
    _ = env.mem_status.memoryViolation_valid.value
    _ = env.lsq_status.lqCanAccept.value
    _ = env.lsq_status.sqCanAccept.value
    _ = env.lsq_status.mmioBusy.value


def test_api_MemBlock_env_commit_agent_exports_rob_state(env):
    """验证 commit agent 暴露了 ROB packet 与五元组能力状态。"""

    env.advance_cycles(1)
    packet = env.commit_agent.latest_commit_packet
    assert packet is not None
    assert hasattr(env.commit_agent.pending_ptr, "flag")
    assert hasattr(env.commit_agent.pending_ptr_next, "value")
    support = env.commit_agent.signal_support
    assert support["pending_ptr"] is True
    assert isinstance(env.commit_agent.models_pending_ptr_next, bool)
    assert isinstance(env.commit_agent.models_commit_bool, bool)
    assert isinstance(env.commit_agent.models_mixed_commit_packet, bool)


def test_api_MemBlock_env_backend_note_load_completed_advances_pending_ptr(env):
    """验证 backend facade 能通过 load issued/completed 推进 ROB head。"""

    env.backend.note_load_issued(0, 1)
    assert env.rob_agent.stats["rob_pending_entry_count"] == 1
    env.backend.note_load_completed(0, 1)
    env.rob_agent.advance()
    env.rob_agent.drive()
    packet = env.commit_agent.latest_commit_packet
    assert packet.commit is True
    assert packet.lcommit == 1
    assert packet.pending_ptr_before.value == 0
    assert packet.pending_ptr_next.value == 1
    env.rob_agent.advance()
    assert env.commit_agent.pending_ptr.flag == 0
    assert env.commit_agent.pending_ptr.value == 1


def test_api_MemBlock_env_backend_note_store_allocated_updates_state(env):
    """验证 backend facade 能更新 store shadow 与 ROB pending entry。"""

    env.backend.note_store_allocated(
        sq_idx_flag=0,
        sq_idx_value=7,
        rob_idx_flag=1,
        rob_idx_value=0x12,
    )
    store = env.get_store_view(7)
    assert store is not None
    assert store.allocated is True
    assert store.rob_idx_flag == 1
    assert store.rob_idx_value == 0x12
    assert env.rob_agent.stats["rob_pending_entry_count"] == 1


def test_api_MemBlock_env_backend_non_mem_blocker_controls_rob(env):
    """验证 backend facade 能插入并 release non-mem blocker。"""

    env.backend.insert_non_mem_blocker(0, 6)
    assert env.rob_agent.stats["rob_non_mem_insert_count"] == 1
    env.backend.release_non_mem_blocker(0, 6)
    assert env.rob_agent.stats["rob_non_mem_release_count"] == 1


def test_api_MemBlock_env_backend_mark_store_commit_ready_updates_rob(env):
    """验证 backend facade 能按 SQ 指针更新 store commit readiness。"""

    env.backend.note_store_allocated(
        sq_idx_flag=0,
        sq_idx_value=8,
        rob_idx_flag=0,
        rob_idx_value=0x21,
    )
    env.backend.mark_store_commit_ready(0, 8, ready=True)
    env.backend.queue_store_commit(1)
    env.rob_agent.advance()
    env.rob_agent.drive()
    assert env.commit_agent.latest_commit_packet.commit is True
    assert env.commit_agent.latest_commit_packet.scommit == 1


def test_api_MemBlock_env_cleanup_removes_legacy_control_helpers(env):
    """验证旧的 env.note_*/pulse_* 与 `Step()` 公共控制入口已清理。"""

    assert not hasattr(env, "note_load_issued")
    assert not hasattr(env, "note_store_allocated")
    assert not hasattr(env, "note_load_completed")
    assert not hasattr(env, "pulse_store_commit")
    assert not hasattr(env, "Step")
    assert not hasattr(MemBlock_api, "api_MemBlock_reset")
    assert not hasattr(MemBlock_api, "api_MemBlock_step")


def test_api_MemBlock_env_outer_buffer_mock_ready(env):
    """验证 MemoryModel 会持续驱动 outer TL-A ready。"""

    env.advance_cycles(1)
    assert env.outer_tl_a.ready.value == 1
    stats = env.get_transport_stats()
    assert stats["pending_outer_d_count"] == 0
    assert stats["active_outer_d_count"] == 0


def test_api_MemBlock_env_outer_buffer_mock_response_queue(env):
    """验证 MemoryModel 支持排队注入 outer D 响应。"""

    env.inject_outer_d_response(delay_cycles=3, opcode=4, source=2, data=0x55AA)
    assert env.get_transport_stats()["pending_outer_d_count"] == 1
    env.advance_cycles(3)
    stats = env.get_transport_stats()
    assert stats["pending_outer_d_count"] + stats["active_outer_d_count"] <= 1
    assert env.outer_tl_a.ready.value == 1


def test_api_MemBlock_env_dcache_client_mock_ready(env):
    """验证 MemoryModel 会持续驱动 dcache ready 信号。"""

    env.advance_cycles(1)
    assert env.dcache_a.ready.value == 1
    assert env.dcache_c.ready.value == 1
    assert env.dcache_e.ready.value == 1


def test_api_MemBlock_env_dcache_client_mock_response_queue(env):
    """验证 MemoryModel 支持 B/D 通道排队注入。"""

    env.inject_dcache_b_response(delay_cycles=2, opcode=6, source=1, address=0x1000)
    env.inject_dcache_d_response(delay_cycles=2, opcode=1, source=1, sink=3, data=0x1234)
    stats = env.get_transport_stats()
    assert stats["pending_b_count"] == 1
    assert stats["pending_d_count"] == 1
    env.advance_cycles(2)
    stats = env.get_transport_stats()
    assert stats["pending_b_count"] + stats["active_b_count"] <= 1
    assert stats["pending_d_count"] + stats["active_d_count"] <= 1


def test_api_MemBlock_env_memory_preload_access(env):
    """验证 MemoryModel preload/read 接口可用。"""

    env.preload_u64(0x1000, 0x1122334455667788)
    assert env.memory.read(0x1000, 8) == 0x1122334455667788


def test_api_MemBlock_env_facade_counter_access(env):
    """验证 env facade 能返回计数与完成数量。"""

    env.advance_cycles(1)
    assert env.get_counter("outer_request_count") >= 0
    assert env.get_completed_load_count() == 0


def test_api_MemBlock_env_vector_frontdoor_facades_exist(env):
    """验证向量 Phase 1 front-door 组件已挂到 env。"""

    assert env.vector_issue_agent is not None
    assert env.vector_backend is not None
    assert env.vector_monitor is not None
    assert env.memory.vector is not None


def test_api_MemBlock_env_assert_no_outstanding_includes_vector_expectations(env):
    """验证 `env.assert_no_outstanding()` 会检查 vector outstanding。"""

    txn = VectorMemTxn(
        req_id=0x81,
        is_load=True,
        opcode_class="unit_stride",
        base_addr=0x1000,
        lq_ptr=QueuePtr(flag=0, value=1),
        sq_ptr=QueuePtr(flag=0, value=2),
        vl=2,
        element_count=2,
    )
    env.memory.vector.expect_load(txn)

    with pytest.raises(AssertionError):
        env.assert_no_outstanding()

    env.memory.vector.mark_completed(txn.req_id)
    env.assert_no_outstanding()


def test_api_MemBlock_env_idle_inputs_restores_default(env):
    """验证 idle_inputs 会清理 env 管理的输入端口。"""

    env.redirect.valid.value = 1
    env.lsq_enq_meta.need_alloc[3].value = 2
    env.issue[1].valid.value = 1
    env.outer_tl_d.valid.value = 1
    env.dcache_b.valid.value = 1
    env.dcache_d.valid.value = 1
    env.dut.io_ooo_to_mem_flushSb.value = 1
    env.idle_inputs()
    assert env.redirect.valid.value == 0
    assert env.lsq_enq_meta.need_alloc[3].value == 0
    assert env.issue[1].valid.value == 0
    assert env.outer_tl_d.valid.value == 0
    assert env.dcache_b.valid.value == 0
    assert env.dcache_d.valid.value == 0
    assert env.dut.io_ooo_to_mem_flushSb.value == 0


def test_api_MemBlock_env_csr_mock_default_m_mode(env):
    """验证 CSR mock 默认配置为非虚拟化 M-mode。"""

    assert env.tlb_csr.priv_virt.value == 0
    assert env.tlb_csr.priv_virt_changed.value == 0
    assert env.tlb_csr.priv_imode.value == 3
    assert env.tlb_csr.priv_dmode.value == 3


def test_api_MemBlock_env_flush_store_buffers_noop(env):
    """空闲状态下 sfence+flushSb 应能快速返回并完成 drain 校验。"""

    env.reset(cycles=2, settle_cycles=5)
    observed_barriers = []

    def _record_barrier():
        observed_barriers.append(
            (
                int(env.dut.io_ooo_to_mem_sfence_valid.value),
                int(env.dut.io_ooo_to_mem_flushSb.value),
            )
        )

    env.add_after_step_callback(_record_barrier)
    try:
        result = env.flush_store_buffers_and_wait(max_cycles=20, settle_cycles=1)
    finally:
        env.remove_after_step_callback(_record_barrier)
    assert (1, 1) in observed_barriers
    assert env.dut.io_ooo_to_mem_sfence_valid.value == 0
    assert env.dut.io_ooo_to_mem_flushSb.value == 0
    assert result["drain_event_count"] == 0


def test_api_MemBlock_env_async_after_step_callback_runs(env):
    """验证 async after-step callback 会在 env 内核拍后被执行。"""

    observed_cycles = []

    async def _async_sample():
        observed_cycles.append(env._current_cycle())

    env.add_after_step_callback(_async_sample)
    try:
        env.advance_cycles(2)
    finally:
        env.remove_after_step_callback(_async_sample)

    assert observed_cycles == [1, 2]
