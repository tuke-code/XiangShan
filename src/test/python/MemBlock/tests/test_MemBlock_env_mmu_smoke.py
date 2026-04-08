# coding=utf-8
"""
MemBlockEnv MMU/PTW/DTLB smoke。
"""

from request_apis import LoadTxn, send_load
from sequences.memblock_sequences import ResetEnvSequence


MMU_ROOT_PT = 0x88000000
MMU_VA = 0x40001000
MMU_PA_BASE = 0x80000000
MMU_DATA = 0x13579BDF2468ACE0


def _reset_env_state(env):
    return ResetEnvSequence(
        require_issue_lanes=(0,),
        require_lq_ready=True,
        require_sq_ready=True,
    ).run(env)


def test_api_MemBlock_env_mmu_idle_inputs_preserve_sv39_state(env):
    """激活的 MMU facade 需要在 idle_inputs 后稳定重放 satp/S-mode 状态。"""

    env.mmu.enable_sv39(root_pt_addr=MMU_ROOT_PT, settle_cycles=0)
    env.idle_inputs()

    assert int(env.tlb_csr.satp_mode.value) == 8
    assert int(env.tlb_csr.satp_ppn.value) == (MMU_ROOT_PT >> 12)
    assert int(env.tlb_csr.priv_imode.value) == 1
    assert int(env.tlb_csr.priv_dmode.value) == 1
    assert int(env.tlb_csr.satp_changed.value) == 0

    env.mmu.disable_translation()
    env.idle_inputs()

    assert int(env.tlb_csr.priv_imode.value) == 3
    assert int(env.tlb_csr.priv_dmode.value) == 3
    assert int(env.tlb_csr.satp_mode.value) == 0


def test_api_MemBlock_env_mmu_sv39_ptw_smoke(env):
    """真实 DUT 下跑通一条 Sv39 + PTW + DTLB + cacheable load 的基础闭环。"""

    state = _reset_env_state(env)
    translated_pa = MMU_PA_BASE | (MMU_VA & ((1 << 30) - 1))
    env.mmu.install_sv39_gigapage_mapping(
        root_pt_addr=MMU_ROOT_PT,
        va=MMU_VA,
        pa_base=MMU_PA_BASE,
    )
    env.preload_u64(translated_pa, MMU_DATA)
    env.mmu.allow_all_smode_access()

    with env.mmu.ptw_responder() as ptw:
        env.mmu.enable_sv39(root_pt_addr=MMU_ROOT_PT)
        txn = LoadTxn(
            req_id=0,
            addr=MMU_VA,
            lq_ptr=state.next_lq_ptr,
            sq_ptr=state.sq_ptr,
        )
        env.expect_scalar_load(
            req_id=txn.req_id,
            pdest=txn.resolved_pdest,
            addr=translated_pa,
            size=txn.size,
            mask=txn.mask,
        )
        send_load(env, txn)
        writeback = env.wait_load_writeback_observed(
            rob_idx_flag=txn.rob_idx_flag,
            rob_idx_value=txn.rob_idx_value,
            data=MMU_DATA,
            max_cycles=512,
        )

    env.drain_writebacks(max_cycles=256)
    stats = env.get_transport_stats()
    ptw_events = tuple(ptw.trace)

    assert writeback["data"] == MMU_DATA
    assert env.get_completed_load_count() == 1
    assert any(event["event"] == "a_fire" for event in ptw_events)
    assert sum(1 for event in ptw_events if event["event"] == "d_fire") == 2
    assert stats["dcache_a_request_count"] > 0
    assert stats["dcache_d_response_count"] > 0
    assert stats["outer_request_count"] == 0
    assert env._read_optional_dut_signal("io_dcacheError_ecc_error_valid") == 0
