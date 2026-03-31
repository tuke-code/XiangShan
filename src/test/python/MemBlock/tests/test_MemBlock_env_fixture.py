# coding=utf-8
"""
MemBlockEnv 冒烟测试。

这些测试只验证 env 自身、Bundle 绑定和 MemoryModel 的工作状态，
不依赖 MemBlock 的具体功能实现。
"""

import pytest

from MemBlock_api import api_MemBlock_reset, api_MemBlock_step


def test_api_MemBlock_env_create(env):
    """验证 env、dut 和 MemoryModel 能正常创建。"""

    assert env is not None
    assert env.dut is not None
    assert env.memory is not None
    assert env.mock_outer_buffer is not None
    assert env.mock_dcache_client is not None
    assert env.mock_csr is not None


def test_api_MemBlock_env_has_core_bundles(env):
    """验证核心 Bundle 分组齐全。"""

    assert hasattr(env, "redirect")
    assert hasattr(env, "tlb_csr")
    assert hasattr(env, "csr_ctrl")
    assert hasattr(env, "lsq_enq_meta")
    assert hasattr(env, "lsq_enq_req")
    assert hasattr(env, "lsq_enq_resp")
    assert hasattr(env, "issue")
    assert hasattr(env, "writeback")
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
    assert len(env.lsq_enq_req) == 8
    assert len(env.lsq_enq_resp) == 8
    assert len(env.issue) == 7
    assert len(env.writeback) == 7
    assert len(env.store_data_inputs) == 2
    assert len(env.store_addr_inputs) == 2
    assert len(env.store_mask_inputs) == 2
    assert len(env.store_addr_re_inputs) == 2
    assert len(env.sbuffer_writes) == 2
    assert len(env.sq_shadow_entries) == 56


def test_api_MemBlock_env_step_and_reset(env):
    """验证基础 API 和 env 方法都能正常推进时钟。"""

    api_MemBlock_reset(env, cycles=2, max_cycles=20)
    api_MemBlock_step(env, cycles=3, max_cycles=10)
    env.reset(cycles=2, settle_cycles=1)
    env.Step(2)
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
    env.Step(1)
    env.idle_inputs()
    env.Step(1)
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
    env.Step(1)
    env.idle_inputs()
    env.Step(1)
    for bundle in env.issue:
        assert bundle.valid.value == 0
        _ = bundle.ready.value


def test_api_MemBlock_env_mem_status_access(env):
    """验证 mem_to_ooo 相关状态口可读。"""

    env.Step(1)
    _ = env.mem_status.lqDeq.value
    _ = env.mem_status.sqDeq.value
    _ = env.mem_status.lqDeqPtr_value.value
    _ = env.mem_status.sqDeqPtr_value.value
    _ = env.mem_status.sbIsEmpty.value
    _ = env.mem_status.memoryViolation_valid.value
    _ = env.lsq_status.lqCanAccept.value
    _ = env.lsq_status.sqCanAccept.value
    _ = env.lsq_status.mmioBusy.value


def test_api_MemBlock_env_outer_buffer_mock_ready(env):
    """验证 MemoryModel 会持续驱动 outer TL-A ready。"""

    env.Step(1)
    assert env.outer_tl_a.ready.value == 1
    stats = env.get_transport_stats()
    assert stats["pending_outer_d_count"] == 0
    assert stats["active_outer_d_count"] == 0


def test_api_MemBlock_env_outer_buffer_mock_response_queue(env):
    """验证 MemoryModel 支持排队注入 outer D 响应。"""

    env.inject_outer_d_response(delay_cycles=3, opcode=4, source=2, data=0x55AA)
    assert env.get_transport_stats()["pending_outer_d_count"] == 1
    env.Step(3)
    stats = env.get_transport_stats()
    assert stats["pending_outer_d_count"] + stats["active_outer_d_count"] <= 1
    assert env.outer_tl_a.ready.value == 1


def test_api_MemBlock_env_dcache_client_mock_ready(env):
    """验证 MemoryModel 会持续驱动 dcache ready 信号。"""

    env.Step(1)
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
    env.Step(2)
    stats = env.get_transport_stats()
    assert stats["pending_b_count"] + stats["active_b_count"] <= 1
    assert stats["pending_d_count"] + stats["active_d_count"] <= 1


def test_api_MemBlock_env_memory_preload_access(env):
    """验证 MemoryModel preload/read 接口可用。"""

    env.preload_u64(0x1000, 0x1122334455667788)
    assert env.memory.read(0x1000, 8) == 0x1122334455667788


def test_api_MemBlock_env_facade_counter_access(env):
    """验证 env facade 能返回计数与完成数量。"""

    env.Step(1)
    assert env.get_counter("outer_request_count") >= 0
    assert env.get_completed_load_count() == 0


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

    api_MemBlock_reset(env, cycles=2, max_cycles=20)
    observed_barriers = []
    original_step = env.Step

    def _recording_step(cycles=1):
        observed_barriers.append(
            (
                int(env.dut.io_ooo_to_mem_sfence_valid.value),
                int(env.dut.io_ooo_to_mem_flushSb.value),
            )
        )
        return original_step(cycles)

    env.Step = _recording_step
    result = env.flush_store_buffers_and_wait(max_cycles=20, settle_cycles=1)
    env.Step = original_step
    assert (1, 1) in observed_barriers
    assert env.dut.io_ooo_to_mem_sfence_valid.value == 0
    assert env.dut.io_ooo_to_mem_flushSb.value == 0
    assert result["drain_event_count"] == 0
