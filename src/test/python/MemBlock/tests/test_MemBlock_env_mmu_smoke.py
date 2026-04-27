# coding=utf-8
"""
MemBlockEnv MMU/PTW/DTLB smoke。
"""

import pytest

from transactions import LoadTxn
from request_apis import send_load
from sequences import (
    GStageSv39Mapping,
    MmuDtlbPageSpec,
    MmuDtlbReplacementSequence,
    MmuSv39AddressSpaceConfig,
    MmuSv39AddressSpaceInstallSequence,
    ResetEnvSequence,
    Sv39Mapping,
    TwoStageSv39AddressSpaceConfig,
    TwoStageSv39AddressSpaceInstallSequence,
    TranslatedU64MemoryPreload,
)


MMU_ROOT_PT = 0x88000000
MMU_VA = 0x40001000
MMU_SECOND_VA = 0x40201000
MMU_PA_BASE = 0x80000000
MMU_DATA = 0x13579BDF2468ACE0
MMU_SECOND_DATA = 0x02468ACE13579BDF
MMU_PAGE_TABLE_PAGES = (
    0x88001000,
    0x88002000,
    0x88003000,
    0x88004000,
)
PMP_CFG_CSR_BASE = 0x3A0
PMP_ADDR_CSR_BASE = 0x3B0
PMP_DENY_NAPOT_CFG = 0x18
PMP_ALLOW_RWX_NAPOT_CFG = 0x1F
PMP_REGION_BASE = 0x80004000
PMP_REGION_SIZE = 0x1000
MEMBLOCK_PADDR_BITS = 48
MMU_DTLB_REPLACEMENT_ROOT_PT = 0x88100000
MMU_DTLB_CAPACITY = 4
MMU_DTLB_REPLACEMENT_SWEEP_PAGE_COUNT = 64
MMU_DTLB_REPLACEMENT_PAGE_TABLE_PAGES = tuple(
    0x88101000 + index * 0x1000 for index in range(MMU_DTLB_REPLACEMENT_SWEEP_PAGE_COUNT * 2)
)
MMU_DTLB_REPLACEMENT_PAGE_SPECS = tuple(
    MmuDtlbPageSpec(
        page_va=0x0040001000 + (index << 30),
        pa_base=0x81001000 + (index << 12),
        data=0x1111222233334444 + index,
    )
    for index in range(MMU_DTLB_REPLACEMENT_SWEEP_PAGE_COUNT)
)
MMU_VS_ROOT_PT = 0x88200000
MMU_G_ROOT_PT = 0x88300000
MMU_G_PAGE_TABLE_PAGES = (
    0x88304000,
    0x88305000,
    0x88306000,
    0x88307000,
)
MMU_GPA_BASE = 0x20000000
MMU_HPA_BASE = 0x80020000


def _reset_env_state(env):
    return ResetEnvSequence(
        require_issue_lanes=(0,),
        require_lq_ready=True,
        require_sq_ready=True,
    ).run(env)


def _capture_distributed_csr_writes(env, action):
    writes = []

    def _sample_csr_write():
        if int(env.csr_ctrl.distribute_csr_w_valid.value) == 0:
            return
        writes.append(
            (
                int(env.csr_ctrl.distribute_csr_w_bits_addr.value),
                int(env.csr_ctrl.distribute_csr_w_bits_data.value),
            )
        )

    env.add_after_step_callback(_sample_csr_write)
    try:
        action()
    finally:
        env.remove_after_step_callback(_sample_csr_write)
    return writes


def _encode_pmp_napot_addr(base_addr: int, size: int) -> int:
    return (int(base_addr) + (int(size) // 2 - 1)) >> 2


def _sv39_4k_mapping_pa_base(space_pa_base: int, va: int) -> int:
    page_offset = (int(va) & ((1 << 30) - 1)) & ~0xFFF
    return int(space_pa_base) | int(page_offset)


def _capture_sfence_pulses(env, action):
    events = []

    def _sample_sfence():
        if not hasattr(env.dut, "io_ooo_to_mem_sfence_valid"):
            return
        if int(env.dut.io_ooo_to_mem_sfence_valid.value) == 0:
            return
        events.append(
            {
                "rs1": int(env.dut.io_ooo_to_mem_sfence_bits_rs1.value),
                "rs2": int(env.dut.io_ooo_to_mem_sfence_bits_rs2.value),
                "addr": int(env.dut.io_ooo_to_mem_sfence_bits_addr.value),
                "id": int(env.dut.io_ooo_to_mem_sfence_bits_id.value),
                "hv": int(env.dut.io_ooo_to_mem_sfence_bits_hv.value),
                "hg": int(env.dut.io_ooo_to_mem_sfence_bits_hg.value),
            }
        )

    env.add_after_step_callback(_sample_sfence)
    try:
        action()
    finally:
        env.remove_after_step_callback(_sample_sfence)
    return events


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


def test_api_MemBlock_env_mmu_enable_vs_sv39_idle_inputs_preserve_state(env):
    """VS-only 背景需要在 idle_inputs 后稳定保活 `vsatp + priv_virt`。"""

    env.mmu.enable_vs_sv39(root_pt_addr=MMU_VS_ROOT_PT, asid=0x23, settle_cycles=0)
    env.idle_inputs()

    assert int(env.tlb_csr.satp_mode.value) == 0
    assert int(env.tlb_csr.vsatp_mode.value) == 8
    assert int(env.tlb_csr.vsatp_asid.value) == 0x23
    assert int(env.tlb_csr.vsatp_ppn.value) == (MMU_VS_ROOT_PT >> 12)
    assert int(env.tlb_csr.hgatp_mode.value) == 0
    assert int(env.tlb_csr.priv_virt.value) == 1
    assert int(env.tlb_csr.priv_virt_changed.value) == 0

    env.mmu.disable_translation()
    env.idle_inputs()

    assert int(env.tlb_csr.vsatp_mode.value) == 0
    assert int(env.tlb_csr.priv_virt.value) == 0


def test_api_MemBlock_env_mmu_two_stage_reapply_after_reset_preserves_state(env):
    """reset 后 `reapply_after_reset()` 需要重放 `vsatp/hgatp/priv_virt` 两阶段背景。"""

    env.mmu.enable_two_stage_sv39(
        vs_root_pt_addr=MMU_VS_ROOT_PT,
        g_root_pt_addr=MMU_G_ROOT_PT,
        vs_asid=0x11,
        vmid=0x7,
        settle_cycles=0,
    )
    env.reset(cycles=1, settle_cycles=0)

    assert int(env.tlb_csr.satp_mode.value) == 0
    assert int(env.tlb_csr.vsatp_mode.value) == 8
    assert int(env.tlb_csr.vsatp_asid.value) == 0x11
    assert int(env.tlb_csr.vsatp_ppn.value) == (MMU_VS_ROOT_PT >> 12)
    assert int(env.tlb_csr.hgatp_mode.value) == 8
    assert int(env.tlb_csr.hgatp_vmid.value) == 0x7
    assert int(env.tlb_csr.hgatp_ppn.value) == (MMU_G_ROOT_PT >> 12)
    assert int(env.tlb_csr.priv_virt.value) == 1
    assert int(env.tlb_csr.priv_virt_changed.value) == 0


def test_api_MemBlock_env_mmu_hfence_helpers_drive_expected_bits(env):
    """`hfence.vvma/gvma` helper 需要把 facade 语义正确翻译到 DUT bundle。"""

    events = _capture_sfence_pulses(
        env,
        lambda: (
            env.mmu.pulse_hfence_vvma(rs2=True, asid=0x44),
            env.mmu.pulse_hfence_gvma(rs1=True, rs2=True, addr=0x12345000, vmid=0x55),
        ),
    )

    assert events[0] == {"rs1": 1, "rs2": 0, "addr": 0, "id": 0x44, "hv": 1, "hg": 0}
    assert events[1] == {"rs1": 0, "rs2": 0, "addr": 0x12345000, "id": 0x55, "hv": 0, "hg": 1}


def test_api_MemBlock_env_mmu_sv39_ptw_smoke(env):
    """真实 DUT 下跑通一条 Sv39 + PTW + DTLB + cacheable load 的基础闭环。"""

    state = _reset_env_state(env)
    translated_pa = _sv39_4k_mapping_pa_base(MMU_PA_BASE, MMU_VA)
    env.mmu.install_sv39_mapping(
        root_pt_addr=MMU_ROOT_PT,
        va=MMU_VA,
        pa_base=translated_pa,
        page_table_page_addrs=MMU_PAGE_TABLE_PAGES,
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
        env.backend.prepare(txn)
        env.expect_scalar_load(
            rob_idx=txn.rob_idx,
            pdest=txn.resolved_pdest,
            addr=translated_pa,
            size=txn.size,
            mask=txn.mask,
        )
        send_load(env, txn)
        writeback = env.wait_load_writeback_observed(
            rob_idx=txn.rob_idx,
            data=MMU_DATA,
            max_cycles=512,
        )

    env.drain_writebacks(max_cycles=256)
    stats = env.get_transport_stats()
    ptw_events = tuple(ptw.trace)

    assert writeback["data"] == MMU_DATA
    assert env.get_completed_load_count() == 1
    assert sum(1 for event in ptw_events if event["event"] == "a_fire") >= 2
    assert sum(1 for event in ptw_events if event["event"] == "d_fire") >= 4
    assert stats["dcache_a_request_count"] > 0
    assert stats["dcache_d_response_count"] > 0
    assert stats["outer_request_count"] == 0
    assert env._read_optional_dut_signal("io_dcacheError_ecc_error_valid") == 0


def test_api_MemBlock_fetch_to_mem_itlb_ptw_smoke(env):
    """frontend `fetch_to_mem.itlb` 顶层请求口应可稳定握手，并在当前 build 下尽量观测后续 PTW/response。"""

    env.reset(cycles=1, settle_cycles=0)
    translated_pa = _sv39_4k_mapping_pa_base(MMU_PA_BASE, MMU_VA)
    install_result = env.mmu.install_sv39_mapping(
        root_pt_addr=MMU_ROOT_PT,
        va=MMU_VA,
        pa_base=translated_pa,
        page_table_page_addrs=MMU_PAGE_TABLE_PAGES,
    )
    env.preload_u64(
        install_result["leaf_pte_addr"],
        env.memory.read(install_result["leaf_pte_addr"], 8) | (1 << 3),
    )
    env.mmu.allow_all_smode_access()
    responses = []

    def _capture_itlb_response():
        if env.fetch_to_mem.itlb_resp.read("valid", 0) == 0:
            return
        if env.fetch_to_mem.itlb_resp.read("ready", 0) == 0:
            return
        responses.append(env.fetch_to_mem.itlb_resp.snapshot())

    env.add_after_step_callback(_capture_itlb_response)
    try:
        with env.mmu.ptw_responder() as ptw:
            env.mmu.enable_sv39(root_pt_addr=MMU_ROOT_PT)
            env.fetch_to_mem.set_resp_ready(1)
            env.fetch_to_mem.drive_itlb_request(vpn=MMU_VA >> 12, s2xlate=0)
            request = env.fetch_to_mem.wait_req_fire(max_cycles=64)
            env.fetch_to_mem.clear_itlb_request()
            env.advance_cycles(64)
            response = responses[-1] if responses else None
            ptw_events = tuple(ptw.trace)
    finally:
        env.fetch_to_mem.set_resp_ready(0)
        env.remove_after_step_callback(_capture_itlb_response)

    assert request["bits_vpn"] == (MMU_VA >> 12)
    assert request["bits_s2xlate"] == 0
    assert env.fetch_to_mem.itlb_resp.connected("valid")
    if ptw_events:
        assert sum(1 for event in ptw_events if event["event"] == "a_fire") >= 1
        assert sum(1 for event in ptw_events if event["event"] == "d_fire") >= 1
    if response is not None:
        assert response["valid"] == 1
        assert response["bits_s1_entry_v"] == 1
        assert response["bits_s2xlate"] == 0


def test_api_MemBlock_env_mmu_sv39_dtlb_fill_and_replacement(env):
    """跨大范围 Sv39 4KB 地址空间填满 DTLB，并证明容量溢出后旧项重新 miss/refill。"""

    state = _reset_env_state(env)
    result = MmuDtlbReplacementSequence(
        root_pt_addr=MMU_DTLB_REPLACEMENT_ROOT_PT,
        page_table_page_addrs=MMU_DTLB_REPLACEMENT_PAGE_TABLE_PAGES,
        page_specs=MMU_DTLB_REPLACEMENT_PAGE_SPECS,
        initial_state=state,
        dtlb_capacity=MMU_DTLB_CAPACITY,
    ).run(env)

    assert result.dtlb_capacity == MMU_DTLB_CAPACITY
    assert len(result.overflow_accesses) == MMU_DTLB_REPLACEMENT_SWEEP_PAGE_COUNT - MMU_DTLB_CAPACITY
    assert result.completed_load_count == (
        2 * MMU_DTLB_CAPACITY + len(result.overflow_accesses) + len(result.reprobe_accesses)
    )

    for access, spec in zip(result.prime_accesses, MMU_DTLB_REPLACEMENT_PAGE_SPECS[:MMU_DTLB_CAPACITY]):
        assert access.va == spec.load_va
        assert access.expected_pa == spec.expected_pa
        assert access.writeback["data"] == spec.data
        assert access.miss_observed

    for access, spec in zip(result.rehit_accesses, MMU_DTLB_REPLACEMENT_PAGE_SPECS[:MMU_DTLB_CAPACITY]):
        assert access.va == spec.load_va
        assert access.writeback["data"] == spec.data
        assert not access.miss_observed

    overflow_specs_by_va = {
        spec.load_va: spec for spec in MMU_DTLB_REPLACEMENT_PAGE_SPECS[MMU_DTLB_CAPACITY:]
    }
    assert all(access.va in overflow_specs_by_va for access in result.overflow_accesses)
    assert all(access.writeback["data"] == overflow_specs_by_va[access.va].data for access in result.overflow_accesses)
    assert all(
        access.expected_pa == overflow_specs_by_va[access.va].expected_pa
        for access in result.overflow_accesses
    )
    assert all(access.miss_observed for access in result.overflow_accesses)

    reprobe_specs_by_va = {
        spec.load_va: spec for spec in MMU_DTLB_REPLACEMENT_PAGE_SPECS[:MMU_DTLB_CAPACITY]
    }
    assert 1 <= len(result.reprobe_accesses) <= MMU_DTLB_CAPACITY
    assert all(access.va in reprobe_specs_by_va for access in result.reprobe_accesses)
    assert all(access.writeback["data"] == reprobe_specs_by_va[access.va].data for access in result.reprobe_accesses)
    assert all(
        access.expected_pa == reprobe_specs_by_va[access.va].expected_pa
        for access in result.reprobe_accesses
    )
    assert all(not access.miss_observed for access in result.reprobe_accesses[:-1])
    assert result.reprobe_access == result.reprobe_accesses[-1]
    assert result.reprobe_access.miss_observed
    combined_accesses = (*result.prime_accesses, *result.overflow_accesses, *result.reprobe_accesses)
    assert all(access.l2_tlb_req_seen == (access.first_l2_tlb_req is not None) for access in combined_accesses)
    l2_tlb_accesses = tuple(
        access
        for access in combined_accesses
        if access.first_l2_tlb_req is not None
    )
    if l2_tlb_accesses:
        first_l2_tlb_req = l2_tlb_accesses[0].first_l2_tlb_req
        assert first_l2_tlb_req is not None
        assert first_l2_tlb_req["req_valid"] == 1
        if first_l2_tlb_req.get("req_memidx_is_ld") is not None:
            assert first_l2_tlb_req["req_memidx_is_ld"] == 1
        if first_l2_tlb_req.get("req_memidx_is_st") is not None:
            assert first_l2_tlb_req["req_memidx_is_st"] == 0


def test_api_MemBlock_env_mmu_install_sv39_mapping_writes_expected_4k_tables(env):
    """4KB mapping helper 需要按需创建 non-leaf，并在共享上层页表时复用已有页表页。"""

    first_pa_base = _sv39_4k_mapping_pa_base(MMU_PA_BASE, MMU_VA)
    second_pa_base = _sv39_4k_mapping_pa_base(MMU_PA_BASE, MMU_SECOND_VA)

    first = env.mmu.install_sv39_mapping(
        root_pt_addr=MMU_ROOT_PT,
        va=MMU_VA,
        pa_base=first_pa_base,
        page_table_page_addrs=MMU_PAGE_TABLE_PAGES,
    )
    second = env.mmu.install_sv39_mapping(
        root_pt_addr=MMU_ROOT_PT,
        va=MMU_SECOND_VA,
        pa_base=second_pa_base,
        page_table_page_addrs=MMU_PAGE_TABLE_PAGES[2:],
    )

    assert first["allocated_table_addrs"] == MMU_PAGE_TABLE_PAGES[:2]
    assert second["allocated_table_addrs"] == (MMU_PAGE_TABLE_PAGES[2],)
    assert env.memory.read(first["root_pte_addr"], 8) == env.mmu.sv39_nonleaf_pte(MMU_PAGE_TABLE_PAGES[0])
    assert env.memory.read(first["l1_pte_addr"], 8) == env.mmu.sv39_nonleaf_pte(MMU_PAGE_TABLE_PAGES[1])
    assert env.memory.read(first["leaf_pte_addr"], 8) == env.mmu.sv39_leaf_pte(first_pa_base)
    assert env.memory.read(second["root_pte_addr"], 8) == env.mmu.sv39_nonleaf_pte(MMU_PAGE_TABLE_PAGES[0])
    assert env.memory.read(second["l1_pte_addr"], 8) == env.mmu.sv39_nonleaf_pte(MMU_PAGE_TABLE_PAGES[2])
    assert env.memory.read(second["leaf_pte_addr"], 8) == env.mmu.sv39_leaf_pte(second_pa_base)


def test_api_MemBlock_env_mmu_install_sv39_mapping_requires_page_table_pool(env):
    """4KB helper 需要显式页表页地址池，不能在 env 内隐式分配中间页表页。"""

    with pytest.raises(ValueError, match="缺少页表页地址池"):
        env.mmu.install_sv39_mapping(
            root_pt_addr=MMU_ROOT_PT,
            va=MMU_VA,
            pa_base=_sv39_4k_mapping_pa_base(MMU_PA_BASE, MMU_VA),
        )


def test_api_MemBlock_env_mmu_address_space_sequence_applies_4k_translated_preloads(env):
    """地址空间 sequence 应按 4KB mapping 解析 translated preload。"""

    first_pa_base = _sv39_4k_mapping_pa_base(MMU_PA_BASE, MMU_VA)
    second_pa_base = _sv39_4k_mapping_pa_base(MMU_PA_BASE, MMU_SECOND_VA)
    preload_va = MMU_VA + 0x8

    result = MmuSv39AddressSpaceInstallSequence(
        MmuSv39AddressSpaceConfig(
            root_pt_addr=MMU_ROOT_PT,
            mappings=(
                Sv39Mapping(va=MMU_VA, pa_base=first_pa_base),
                Sv39Mapping(va=MMU_SECOND_VA, pa_base=second_pa_base),
            ),
            page_table_page_addrs=MMU_PAGE_TABLE_PAGES,
            translated_preloads=(TranslatedU64MemoryPreload(va=preload_va, data=MMU_SECOND_DATA),),
        )
    ).run(env)

    assert result.translated_pa_for(preload_va) == first_pa_base + 0x8
    assert env.memory.read(first_pa_base + 0x8, 8) == MMU_SECOND_DATA


def test_api_MemBlock_env_mmu_install_g_sv39_mapping_and_two_stage_preload(env):
    """两阶段地址空间 sequence 需要把 translated preload 预置到最终 HPA。"""

    gpa_base = _sv39_4k_mapping_pa_base(MMU_GPA_BASE, MMU_VA)
    hpa_base = _sv39_4k_mapping_pa_base(MMU_HPA_BASE, gpa_base)
    preload_va = MMU_VA + 0x18

    result = TwoStageSv39AddressSpaceInstallSequence(
        TwoStageSv39AddressSpaceConfig(
            vs_root_pt_addr=MMU_VS_ROOT_PT,
            g_root_pt_addr=MMU_G_ROOT_PT,
            vs_mappings=(Sv39Mapping(va=MMU_VA, pa_base=gpa_base),),
            g_mappings=(GStageSv39Mapping(gpa=gpa_base, pa_base=hpa_base),),
            vs_page_table_page_addrs=MMU_PAGE_TABLE_PAGES,
            g_page_table_page_addrs=MMU_G_PAGE_TABLE_PAGES,
            translated_preloads=(TranslatedU64MemoryPreload(va=preload_va, data=MMU_SECOND_DATA),),
        )
    ).run(env)

    assert result.resolve_vs_stage_gpa(preload_va) == gpa_base + 0x18
    assert result.resolve_two_stage_pa(preload_va) == hpa_base + 0x18
    assert env.memory.read(hpa_base + 0x18, 8) == MMU_SECOND_DATA


def test_api_MemBlock_env_mmu_program_pmp_deny_region_smoke(env):
    """`program_pmp_deny_region()` 应稳定发出 PMP CSR 写并可与 allow-all 组合。"""

    writes = _capture_distributed_csr_writes(
        env,
        lambda: (
            env.mmu.program_pmp_deny_region(PMP_REGION_BASE, PMP_REGION_SIZE, index=0, persistent=False),
            env.mmu.allow_all_smode_access(index=1, persistent=False),
        ),
    )

    expected_region_addr = _encode_pmp_napot_addr(PMP_REGION_BASE, PMP_REGION_SIZE)
    expected_allow_all_addr = (1 << (MEMBLOCK_PADDR_BITS - 2)) - 1

    assert (PMP_ADDR_CSR_BASE, expected_region_addr) in writes
    assert (PMP_CFG_CSR_BASE, PMP_DENY_NAPOT_CFG) in writes
    assert (PMP_ADDR_CSR_BASE + 1, expected_allow_all_addr) in writes
    assert (PMP_CFG_CSR_BASE, (PMP_ALLOW_RWX_NAPOT_CFG << 8) | PMP_DENY_NAPOT_CFG) in writes
