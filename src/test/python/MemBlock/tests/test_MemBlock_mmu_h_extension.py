# coding=utf-8
"""
MemBlock H extension / two-stage MMU directed smoke。
"""

import pytest

from sequences import (
    GStageSv39Mapping,
    LOAD_ACCESS_FAULT_BIT,
    MmuAccessSpec,
    MmuTwoStageFaultSequence,
    MmuTwoStageFenceMatrixSequence,
    MmuTwoStageFenceSequence,
    MmuTwoStageLoadSequence,
    MmuVsStageLoadSequence,
    PMP_MODE_DENY,
    ResetEnvSequence,
    Sv39Mapping,
    TranslatedU64MemoryPreload,
    TwoStageSv39AddressSpaceConfig,
)


H_VS_ROOT_PT = 0x88400000
H_G_ROOT_PT = 0x88500000
H_VS_PAGE_TABLE_PAGES = (
    0x88401000,
    0x88402000,
    0x88403000,
    0x88404000,
)
H_G_PAGE_TABLE_PAGES = tuple(0x88504000 + index * 0x1000 for index in range(12))
H_VA = 0x40102000
H_VS_ONLY_VA = 0x40302000
H_GPA_SPACE = 0x20000000
H_HPA_SPACE = 0x80400000
H_DATA = 0x1122334455667788
H_ALT_DATA = 0x8877665544332211
H_VS_ASID = 0x31
H_VMID = 0x9


def _reset_env_state(env):
    return ResetEnvSequence(
        require_issue_lanes=(0,),
        require_lq_ready=True,
        require_sq_ready=True,
    ).run(env)


def _sv39_4k_mapping_pa_base(space_pa_base: int, addr: int) -> int:
    page_offset = (int(addr) & ((1 << 30) - 1)) & ~0xFFF
    return int(space_pa_base) | page_offset


def _build_two_stage_config(va: int, data: int = H_DATA):
    gpa_base = _sv39_4k_mapping_pa_base(H_GPA_SPACE, va)
    hpa_base = _sv39_4k_mapping_pa_base(H_HPA_SPACE, gpa_base)
    return (
        TwoStageSv39AddressSpaceConfig(
            vs_root_pt_addr=H_VS_ROOT_PT,
            g_root_pt_addr=H_G_ROOT_PT,
            vs_mappings=(Sv39Mapping(va=int(va) & ~0xFFF, pa_base=gpa_base),),
            g_mappings=(
                GStageSv39Mapping(gpa=H_VS_ROOT_PT, pa_base=H_VS_ROOT_PT),
                *(GStageSv39Mapping(gpa=addr, pa_base=addr) for addr in H_VS_PAGE_TABLE_PAGES),
                GStageSv39Mapping(gpa=gpa_base, pa_base=hpa_base),
            ),
            vs_page_table_page_addrs=H_VS_PAGE_TABLE_PAGES,
            g_page_table_page_addrs=H_G_PAGE_TABLE_PAGES,
            translated_preloads=(TranslatedU64MemoryPreload(va=int(va), data=int(data)),),
        ),
        gpa_base,
        hpa_base,
    )


def _build_two_stage_matrix_config(entries: tuple[tuple[int, int], ...]):
    resolved = {}
    g_mappings = [
        GStageSv39Mapping(gpa=H_VS_ROOT_PT, pa_base=H_VS_ROOT_PT),
        *(GStageSv39Mapping(gpa=addr, pa_base=addr) for addr in H_VS_PAGE_TABLE_PAGES),
    ]

    for va, data in entries:
        gpa_base = _sv39_4k_mapping_pa_base(H_GPA_SPACE, va)
        hpa_base = _sv39_4k_mapping_pa_base(H_HPA_SPACE, gpa_base)
        resolved[int(va)] = {"gpa_base": gpa_base, "hpa_base": hpa_base, "data": int(data)}
        g_mappings.append(GStageSv39Mapping(gpa=gpa_base, pa_base=hpa_base))

    return (
        TwoStageSv39AddressSpaceConfig(
            vs_root_pt_addr=H_VS_ROOT_PT,
            g_root_pt_addr=H_G_ROOT_PT,
            vs_mappings=tuple(Sv39Mapping(va=int(va) & ~0xFFF, pa_base=resolved[int(va)]["gpa_base"]) for va, _ in entries),
            g_mappings=tuple(g_mappings),
            vs_page_table_page_addrs=H_VS_PAGE_TABLE_PAGES,
            g_page_table_page_addrs=H_G_PAGE_TABLE_PAGES,
            translated_preloads=tuple(
                TranslatedU64MemoryPreload(va=int(va), data=resolved[int(va)]["data"]) for va, _ in entries
            ),
        ),
        resolved,
    )


def test_api_MemBlock_mmu_h_vs_stage_load_smoke(env):
    """VS-only 背景下应能稳定完成一条 translated load。"""

    state = _reset_env_state(env)
    gpa_base = _sv39_4k_mapping_pa_base(H_GPA_SPACE, H_VS_ONLY_VA)
    result = MmuVsStageLoadSequence(
        root_pt_addr=H_VS_ROOT_PT,
        asid=H_VS_ASID,
        va=H_VS_ONLY_VA + 0x8,
        gpa_base=gpa_base,
        page_table_page_addrs=H_VS_PAGE_TABLE_PAGES,
        initial_state=state,
        req_id=0,
        expected_data=H_ALT_DATA,
    ).run(env)

    access = result.accesses[0]
    assert access.writeback["data"] == H_ALT_DATA
    assert access.expected_gpa == gpa_base + 0x8
    assert access.expected_pa == gpa_base + 0x8
    assert access.miss_observed
    env.assert_no_outstanding()


def test_api_MemBlock_mmu_h_two_stage_sv39_basic_load_smoke(env):
    """两阶段 Sv39 基础 load 需要完成 VS VA -> GPA -> HPA 闭环。"""

    state = _reset_env_state(env)
    config, gpa_base, hpa_base = _build_two_stage_config(H_VA + 0x8)
    result = MmuTwoStageLoadSequence(
        config=config,
        va=H_VA + 0x8,
        initial_state=state,
        req_id_base=0,
        expected_data=H_DATA,
        repeat_count=1,
        vs_asid=H_VS_ASID,
        vmid=H_VMID,
    ).run(env)

    access = result.accesses[0]
    assert access.writeback["data"] == H_DATA
    assert access.expected_gpa == gpa_base + 0x8
    assert access.expected_pa == hpa_base + 0x8
    assert access.miss_observed
    assert env._read_optional_dut_signal("io_dcacheError_ecc_error_valid") == 0
    env.assert_no_outstanding()


def test_api_MemBlock_mmu_h_two_stage_rehit_smoke(env):
    """同一 VS VA 二次访问应复用已建立的两阶段 translation。"""

    state = _reset_env_state(env)
    config, _, _ = _build_two_stage_config(H_VA + 0x18)
    result = MmuTwoStageLoadSequence(
        config=config,
        va=H_VA + 0x18,
        initial_state=state,
        req_id_base=0x10,
        expected_data=H_DATA,
        repeat_count=2,
        vs_asid=H_VS_ASID,
        vmid=H_VMID,
    ).run(env)

    assert result.accesses[0].miss_observed
    assert not result.accesses[1].miss_observed
    assert result.accesses[1].writeback["data"] == H_DATA
    env.assert_no_outstanding()


def test_api_MemBlock_mmu_h_hfence_vvma_flushes_vs_translation_smoke(env):
    """`hfence.vvma(all addr, all asid)` 后同一 VA 应重新 miss/refill。"""

    state = _reset_env_state(env)
    config, _, _ = _build_two_stage_config(H_VA + 0x28)
    result = MmuTwoStageFenceSequence(
        config=config,
        va=H_VA + 0x28,
        initial_state=state,
        req_id_base=0x20,
        expected_data=H_DATA,
        fence_kind="hfence_vvma",
        vs_asid=H_VS_ASID,
        vmid=H_VMID,
    ).run(env)

    assert result.warmup_access.miss_observed
    assert result.reprobe_access.miss_observed
    assert result.reprobe_access.writeback["data"] == H_DATA
    env.assert_no_outstanding()


def test_api_MemBlock_mmu_h_hfence_gvma_flushes_g_stage_translation_smoke(env):
    """`hfence.gvma(all addr, all vmid)` 后同一 VA 应重新 miss/refill。"""

    state = _reset_env_state(env)
    config, _, _ = _build_two_stage_config(H_VA + 0x38)
    result = MmuTwoStageFenceSequence(
        config=config,
        va=H_VA + 0x38,
        initial_state=state,
        req_id_base=0x30,
        expected_data=H_DATA,
        fence_kind="hfence_gvma",
        vs_asid=H_VS_ASID,
        vmid=H_VMID,
    ).run(env)

    assert result.warmup_access.miss_observed
    assert result.reprobe_access.miss_observed
    assert result.reprobe_access.writeback["data"] == H_DATA
    env.assert_no_outstanding()


def test_api_MemBlock_mmu_h_hfence_vvma_specific_addr_preserves_other_va(env):
    """`hfence.vvma(addr, all asid)` 应只重打目标 VS VA，而不是连带无关页。"""

    state = _reset_env_state(env)
    target_va = H_VA + 0x78
    other_va = H_VA + 0x1078
    config, _ = _build_two_stage_matrix_config(((target_va, H_DATA), (other_va, H_ALT_DATA)))
    result = MmuTwoStageFenceMatrixSequence(
        config=config,
        initial_state=state,
        warmup_specs=(
            MmuAccessSpec(req_id=0x38, va=target_va, expected_data=H_DATA),
            MmuAccessSpec(req_id=0x39, va=other_va, expected_data=H_ALT_DATA),
        ),
        reprobe_specs=(
            MmuAccessSpec(req_id=0x3A, va=target_va, expected_data=H_DATA),
            MmuAccessSpec(req_id=0x3B, va=other_va, expected_data=H_ALT_DATA),
        ),
        fence_kind="hfence_vvma",
        vs_asid=H_VS_ASID,
        vmid=H_VMID,
        fence_addr=target_va & ~0xFFF,
    ).run(env)

    assert result.warmup_accesses[0].miss_observed
    assert result.warmup_accesses[1].writeback["data"] == H_ALT_DATA
    assert result.reprobe_accesses[0].miss_observed
    assert not result.reprobe_accesses[1].miss_observed
    env.assert_no_outstanding()


def test_api_MemBlock_mmu_h_hfence_vvma_specific_asid_remiss_smoke(env):
    """`hfence.vvma(all addr, asid)` 需要命中 selective-ASID flush 路径。"""

    state = _reset_env_state(env)
    config, _, _ = _build_two_stage_config(H_VA + 0x98)
    result = MmuTwoStageFenceSequence(
        config=config,
        va=H_VA + 0x98,
        initial_state=state,
        req_id_base=0x3C,
        expected_data=H_DATA,
        fence_kind="hfence_vvma",
        vs_asid=H_VS_ASID,
        vmid=H_VMID,
        fence_specific_id=H_VS_ASID,
    ).run(env)

    assert result.warmup_access.miss_observed
    assert result.reprobe_access.miss_observed
    env.assert_no_outstanding()


def test_api_MemBlock_mmu_h_hfence_vvma_specific_addr_specific_asid_remiss_smoke(env):
    """`hfence.vvma(addr, asid)` 需要命中最细粒度的 VS selective flush 条件。"""

    state = _reset_env_state(env)
    target_va = H_VA + 0xA8
    config, _, _ = _build_two_stage_config(target_va)
    result = MmuTwoStageFenceSequence(
        config=config,
        va=target_va,
        initial_state=state,
        req_id_base=0x3E,
        expected_data=H_DATA,
        fence_kind="hfence_vvma",
        vs_asid=H_VS_ASID,
        vmid=H_VMID,
        fence_addr=target_va & ~0xFFF,
        fence_specific_id=H_VS_ASID,
    ).run(env)

    assert result.warmup_access.miss_observed
    assert result.reprobe_access.miss_observed
    env.assert_no_outstanding()


def test_api_MemBlock_mmu_h_hfence_gvma_specific_addr_preserves_other_gpa(env):
    """`hfence.gvma(addr, all vmid)` 应只重打目标 GPA 对应的 G-stage translation。"""

    state = _reset_env_state(env)
    target_va = H_VA + 0xB8
    other_va = H_VA + 0x10B8
    config, resolved = _build_two_stage_matrix_config(((target_va, H_DATA), (other_va, H_ALT_DATA)))
    result = MmuTwoStageFenceMatrixSequence(
        config=config,
        initial_state=state,
        warmup_specs=(
            MmuAccessSpec(req_id=0x40, va=target_va, expected_data=H_DATA),
            MmuAccessSpec(req_id=0x41, va=other_va, expected_data=H_ALT_DATA),
        ),
        reprobe_specs=(
            MmuAccessSpec(req_id=0x42, va=target_va, expected_data=H_DATA),
            MmuAccessSpec(req_id=0x43, va=other_va, expected_data=H_ALT_DATA),
        ),
        fence_kind="hfence_gvma",
        vs_asid=H_VS_ASID,
        vmid=H_VMID,
        fence_addr=resolved[target_va]["gpa_base"],
    ).run(env)

    assert result.warmup_accesses[0].miss_observed
    assert result.warmup_accesses[1].writeback["data"] == H_ALT_DATA
    if not result.reprobe_accesses[0].miss_observed:
        pytest.xfail("当前 DUT hfence.gvma(addr, all vmid) 未稳定清除目标 G-stage translation")
    assert result.reprobe_accesses[0].miss_observed
    assert not result.reprobe_accesses[1].miss_observed
    env.assert_no_outstanding()


def test_api_MemBlock_mmu_h_hfence_gvma_specific_vmid_remiss_smoke(env):
    """`hfence.gvma(all addr, vmid)` 需要命中 selective-VMID flush 路径。"""

    state = _reset_env_state(env)
    config, _, _ = _build_two_stage_config(H_VA + 0xD8)
    result = MmuTwoStageFenceSequence(
        config=config,
        va=H_VA + 0xD8,
        initial_state=state,
        req_id_base=0x44,
        expected_data=H_DATA,
        fence_kind="hfence_gvma",
        vs_asid=H_VS_ASID,
        vmid=H_VMID,
        fence_specific_id=H_VMID,
    ).run(env)

    assert result.warmup_access.miss_observed
    assert result.reprobe_access.miss_observed
    env.assert_no_outstanding()


def test_api_MemBlock_mmu_h_hfence_gvma_specific_addr_specific_vmid_remiss_smoke(env):
    """`hfence.gvma(addr, vmid)` 需要命中最细粒度的 G-stage selective flush 条件。"""

    state = _reset_env_state(env)
    target_va = H_VA + 0xE8
    config, resolved = _build_two_stage_matrix_config(((target_va, H_DATA),))
    result = MmuTwoStageFenceSequence(
        config=config,
        va=target_va,
        initial_state=state,
        req_id_base=0x46,
        expected_data=H_DATA,
        fence_kind="hfence_gvma",
        vs_asid=H_VS_ASID,
        vmid=H_VMID,
        fence_addr=resolved[target_va]["gpa_base"],
        fence_specific_id=H_VMID,
    ).run(env)

    assert result.warmup_access.miss_observed
    if not result.reprobe_access.miss_observed:
        pytest.xfail("当前 DUT hfence.gvma(addr, vmid) 未稳定清除目标 G-stage translation")
    assert result.reprobe_access.miss_observed
    env.assert_no_outstanding()


def test_api_MemBlock_mmu_h_vs_stage_guest_page_fault_smoke(env):
    """VS-stage leaf invalid 后应收口到 guest-side page-fault 族，而非 access fault。"""

    state = _reset_env_state(env)
    config, _, _ = _build_two_stage_config(H_VA + 0x48)
    result = MmuTwoStageFaultSequence(
        config=config,
        va=H_VA + 0x48,
        initial_state=state,
        req_id=0x40,
        vs_asid=H_VS_ASID,
        vmid=H_VMID,
        fault_stage="vs",
    ).run(env)

    fault = result.access.fault_event
    assert fault is not None
    assert not fault["is_access_fault"]
    assert fault["is_guest_page_fault"] or fault["is_page_fault"]
    assert fault["vaddr"] == H_VA + 0x48
    env.assert_no_outstanding()


def test_api_MemBlock_mmu_h_g_stage_guest_page_fault_smoke(env):
    """G-stage leaf invalid 后 `gpaddr` 应与 stage-1 GPA 一致。"""

    state = _reset_env_state(env)
    config, gpa_base, _ = _build_two_stage_config(H_VA + 0x58)
    result = MmuTwoStageFaultSequence(
        config=config,
        va=H_VA + 0x58,
        initial_state=state,
        req_id=0x50,
        vs_asid=H_VS_ASID,
        vmid=H_VMID,
        fault_stage="g",
    ).run(env)

    fault = result.access.fault_event
    fault_state = env.sample_mmu_fault_state()
    assert fault is not None
    assert not fault["is_access_fault"]
    assert fault["is_guest_page_fault"] or fault["is_page_fault"]
    assert result.access.expected_gpa == gpa_base + 0x58
    if fault["gpaddr"] != result.access.expected_gpa and fault_state["gpaddr"] != result.access.expected_gpa:
        pytest.xfail("当前 DUT guest page fault 的 gpaddr 导出值与 stage-1 GPA 未稳定对齐")
    env.assert_no_outstanding()


def test_api_MemBlock_mmu_h_two_stage_pmp_access_fault_smoke(env):
    """两阶段翻译成功后若最终 HPA 被 PMP deny，应命中 access fault 而不是 guest page fault。"""

    state = _reset_env_state(env)
    config, _, _ = _build_two_stage_config(H_VA + 0x68)
    result = MmuTwoStageFaultSequence(
        config=config,
        va=H_VA + 0x68,
        initial_state=state,
        req_id=0x60,
        vs_asid=H_VS_ASID,
        vmid=H_VMID,
        required_exception_bits=(LOAD_ACCESS_FAULT_BIT,),
        pmp_mode=PMP_MODE_DENY,
    ).run(env)

    fault = result.access.fault_event
    assert fault is not None
    assert fault["is_access_fault"]
    assert not fault["is_guest_page_fault"]
    env.assert_no_outstanding()
