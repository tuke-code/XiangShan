# coding=utf-8
"""
MemBlock LLPTW-oriented directed regression。
"""

from sequences import (
    GStageSv39Mapping,
    LOAD_ACCESS_FAULT_BIT,
    MmuAccessSpec,
    MmuAccessWave,
    MmuTwoStageFaultSequence,
    MmuTwoStageLoadSequence,
    MmuTwoStageWaveLoadSequence,
    PMP_MODE_DENY,
    ResetEnvSequence,
    Sv39Mapping,
    TranslatedU64MemoryPreload,
    TwoStageSv39AddressSpaceConfig,
)


LLPTW_VA_BASE = 0x40500000
LLPTW_ALT_VA_BASE = 0x40600000
LLPTW_GPA_SPACE_A = 0x20100000
LLPTW_HPA_SPACE_A = 0x80A00000
LLPTW_GPA_SPACE_B = 0x20200000
LLPTW_HPA_SPACE_B = 0x80B00000
LLPTW_VS_ROOT_PT_A = 0x88A00000
LLPTW_G_ROOT_PT_A = 0x88B00000
LLPTW_VS_ROOT_PT_B = 0x88C00000
LLPTW_G_ROOT_PT_B = 0x88D00000
LLPTW_VS_PAGE_TABLE_PAGES_A = (
    0x88A01000,
    0x88A02000,
    0x88A03000,
    0x88A04000,
)
LLPTW_G_PAGE_TABLE_PAGES_A = tuple(0x88B04000 + index * 0x1000 for index in range(12))
LLPTW_VS_PAGE_TABLE_PAGES_B = (
    0x88C01000,
    0x88C02000,
    0x88C03000,
    0x88C04000,
)
LLPTW_G_PAGE_TABLE_PAGES_B = tuple(0x88D04000 + index * 0x1000 for index in range(12))
LLPTW_VS_ASID = 0x35
LLPTW_VMID = 0xD
LLPTW_DATA_BASE = 0x1122334455667700


def _reset_env_state(env):
    return ResetEnvSequence(
        require_issue_lanes=(0,),
        require_lq_ready=True,
        require_sq_ready=True,
    ).run(env)


def _sv39_4k_mapping_pa_base(space_pa_base: int, addr: int) -> int:
    page_offset = (int(addr) & ((1 << 30) - 1)) & ~0xFFF
    return int(space_pa_base) | page_offset


def _build_two_stage_matrix_config(
    *,
    entries: tuple[tuple[int, int], ...],
    vs_root_pt_addr: int,
    g_root_pt_addr: int,
    vs_page_table_page_addrs: tuple[int, ...],
    g_page_table_page_addrs: tuple[int, ...],
    gpa_space: int,
    hpa_space: int,
) -> TwoStageSv39AddressSpaceConfig:
    resolved = {}
    g_mappings = [
        GStageSv39Mapping(gpa=int(vs_root_pt_addr), pa_base=int(vs_root_pt_addr)),
        *(GStageSv39Mapping(gpa=addr, pa_base=addr) for addr in vs_page_table_page_addrs),
    ]

    for va, data in entries:
        gpa_base = _sv39_4k_mapping_pa_base(gpa_space, va)
        hpa_base = _sv39_4k_mapping_pa_base(hpa_space, gpa_base)
        resolved[int(va)] = {"gpa_base": gpa_base, "hpa_base": hpa_base, "data": int(data)}
        g_mappings.append(GStageSv39Mapping(gpa=gpa_base, pa_base=hpa_base))

    return TwoStageSv39AddressSpaceConfig(
        vs_root_pt_addr=vs_root_pt_addr,
        g_root_pt_addr=g_root_pt_addr,
        vs_mappings=tuple(
            Sv39Mapping(va=int(va) & ~0xFFF, pa_base=resolved[int(va)]["gpa_base"]) for va, _ in entries
        ),
        g_mappings=tuple(g_mappings),
        vs_page_table_page_addrs=vs_page_table_page_addrs,
        g_page_table_page_addrs=g_page_table_page_addrs,
        translated_preloads=tuple(
            TranslatedU64MemoryPreload(va=int(va), data=resolved[int(va)]["data"]) for va, _ in entries
        ),
    )


def _build_default_config(entries: tuple[tuple[int, int], ...]) -> TwoStageSv39AddressSpaceConfig:
    return _build_two_stage_matrix_config(
        entries=entries,
        vs_root_pt_addr=LLPTW_VS_ROOT_PT_A,
        g_root_pt_addr=LLPTW_G_ROOT_PT_A,
        vs_page_table_page_addrs=LLPTW_VS_PAGE_TABLE_PAGES_A,
        g_page_table_page_addrs=LLPTW_G_PAGE_TABLE_PAGES_A,
        gpa_space=LLPTW_GPA_SPACE_A,
        hpa_space=LLPTW_HPA_SPACE_A,
    )


def _enable_bitmap_mode(env):
    env.tlb_csr.mbmc_BME.value = 1
    env.tlb_csr.mbmc_CMODE.value = 0
    env.tlb_csr.mbmc_BCLEAR.value = 0
    env.tlb_csr.mbmc_BMA.value = (1 << 58) - 1
    env.mmu.enable_svpbmt()


def test_api_MemBlock_mmu_llptw_six_entry_queue_pressure_smoke(env):
    """两拍灌入 6 条不同页的 translated load，应压满 LLPTW entry 队列而非停留在单请求路径。"""

    state = _reset_env_state(env)
    vas = tuple(LLPTW_VA_BASE + index * 0x1000 + 0x40 for index in range(6))
    entries = tuple((va, LLPTW_DATA_BASE + index) for index, va in enumerate(vas))
    result = MmuTwoStageWaveLoadSequence(
        config=_build_default_config(entries),
        initial_state=state,
        access_waves=(
            MmuAccessWave(
                access_specs=tuple(
                    MmuAccessSpec(req_id=index, va=vas[index], expected_data=LLPTW_DATA_BASE + index)
                    for index in range(3)
                )
            ),
            MmuAccessWave(
                access_specs=tuple(
                    MmuAccessSpec(req_id=3 + index, va=vas[3 + index], expected_data=LLPTW_DATA_BASE + 3 + index)
                    for index in range(3)
                ),
                wait_cycles_before_issue=1,
            ),
        ),
        vs_asid=LLPTW_VS_ASID,
        vmid=LLPTW_VMID,
        response_delay_cycles=12,
        max_cycles=512,
    ).run(env)

    assert len(result.accesses) == 6
    assert sum(1 for event in result.ptw_trace if event["event"] == "a_fire") >= 6
    assert all(access.writeback["data"] == LLPTW_DATA_BASE + index for index, access in enumerate(result.accesses))
    env.assert_no_outstanding()


def test_api_MemBlock_mmu_llptw_duplicate_wait_and_merge_smoke(env):
    """同一 VA 在 waiting / late-walk 窗口重入时应收敛到 LLPTW duplicate 路径，而非每次独立起新 walk。"""

    state = _reset_env_state(env)
    target_va = LLPTW_VA_BASE + 0x180
    other_vas = (
        LLPTW_VA_BASE + 0x1180,
        LLPTW_VA_BASE + 0x2180,
        LLPTW_VA_BASE + 0x3180,
        LLPTW_VA_BASE + 0x4180,
    )
    entries = (
        (target_va, LLPTW_DATA_BASE + 0x10),
        *((va, LLPTW_DATA_BASE + 0x20 + index) for index, va in enumerate(other_vas)),
    )
    result = MmuTwoStageWaveLoadSequence(
        config=_build_default_config(entries),
        initial_state=state,
        access_waves=(
            MmuAccessWave(access_specs=(MmuAccessSpec(req_id=0x20, va=target_va, expected_data=LLPTW_DATA_BASE + 0x10),)),
            MmuAccessWave(
                access_specs=(
                    MmuAccessSpec(req_id=0x21, va=target_va, expected_data=LLPTW_DATA_BASE + 0x10),
                    MmuAccessSpec(req_id=0x22, va=other_vas[0], expected_data=LLPTW_DATA_BASE + 0x20),
                    MmuAccessSpec(req_id=0x23, va=other_vas[1], expected_data=LLPTW_DATA_BASE + 0x21),
                ),
                wait_cycles_before_issue=1,
            ),
            MmuAccessWave(
                access_specs=(
                    MmuAccessSpec(req_id=0x24, va=target_va, expected_data=LLPTW_DATA_BASE + 0x10),
                    MmuAccessSpec(req_id=0x25, va=other_vas[2], expected_data=LLPTW_DATA_BASE + 0x22),
                    MmuAccessSpec(req_id=0x26, va=other_vas[3], expected_data=LLPTW_DATA_BASE + 0x23),
                ),
                wait_cycles_before_issue=10,
            ),
        ),
        vs_asid=LLPTW_VS_ASID,
        vmid=LLPTW_VMID,
        response_delay_cycles=6,
        max_cycles=768,
    ).run(env)

    ptw_a_fire_count = sum(1 for event in result.ptw_trace if event["event"] == "a_fire")
    target_writebacks = tuple(access.writeback["data"] for access in result.accesses if access.va == target_va)
    assert len(target_writebacks) == 3
    assert target_writebacks == (LLPTW_DATA_BASE + 0x10,) * 3
    assert ptw_a_fire_count >= 3
    assert ptw_a_fire_count < 18
    env.assert_no_outstanding()


def test_api_MemBlock_mmu_llptw_high_slot_duplicate_chain_smoke(env):
    """先用 3 条请求占住低位 entry，再把同一 VA 的主请求/duplicate 压到 entry3/4/5。"""

    state = _reset_env_state(env)
    dummy_vas = (
        LLPTW_VA_BASE + 0x520,
        LLPTW_VA_BASE + 0x1520,
        LLPTW_VA_BASE + 0x2520,
    )
    target_va = LLPTW_VA_BASE + 0x3520
    entries = (
        *((va, LLPTW_DATA_BASE + 0x50 + index) for index, va in enumerate(dummy_vas)),
        (target_va, LLPTW_DATA_BASE + 0x60),
    )
    result = MmuTwoStageWaveLoadSequence(
        config=_build_default_config(entries),
        initial_state=state,
        access_waves=(
            MmuAccessWave(
                access_specs=tuple(
                    MmuAccessSpec(req_id=0x80 + index, va=va, expected_data=LLPTW_DATA_BASE + 0x50 + index)
                    for index, va in enumerate(dummy_vas)
                )
            ),
            MmuAccessWave(
                access_specs=(MmuAccessSpec(req_id=0x83, va=target_va, expected_data=LLPTW_DATA_BASE + 0x60),),
                wait_cycles_before_issue=1,
            ),
            MmuAccessWave(
                access_specs=(MmuAccessSpec(req_id=0x84, va=target_va, expected_data=LLPTW_DATA_BASE + 0x60),),
                wait_cycles_before_issue=1,
            ),
            MmuAccessWave(
                access_specs=(MmuAccessSpec(req_id=0x85, va=target_va, expected_data=LLPTW_DATA_BASE + 0x60),),
                wait_cycles_before_issue=1,
            ),
        ),
        vs_asid=LLPTW_VS_ASID,
        vmid=LLPTW_VMID,
        response_delay_cycles=18,
        max_cycles=1024,
    ).run(env)

    target_writebacks = tuple(access.writeback["data"] for access in result.accesses if access.va == target_va)
    assert target_writebacks == (LLPTW_DATA_BASE + 0x60,) * 3
    env.assert_no_outstanding()


def test_api_MemBlock_mmu_llptw_bitmap_enable_smoke(env):
    """打开 `mbmc.BME` 后，基础 two-stage load 仍应可完成，用于试探 bitmap 相关状态是否可达。"""

    state = _reset_env_state(env)
    _enable_bitmap_mode(env)
    target_va = LLPTW_VA_BASE + 0x880
    result = MmuTwoStageLoadSequence(
        config=_build_default_config(((target_va, LLPTW_DATA_BASE + 0x70),)),
        va=target_va,
        initial_state=state,
        req_id_base=0x90,
        expected_data=LLPTW_DATA_BASE + 0x70,
        vs_asid=LLPTW_VS_ASID,
        vmid=LLPTW_VMID,
        response_delay_cycles=4,
    ).run(env)

    assert result.accesses[0].writeback["data"] == LLPTW_DATA_BASE + 0x70
    env.assert_no_outstanding()


def test_api_MemBlock_mmu_llptw_first_stage_guest_page_fault_smoke(env):
    """打掉 VS root 的 G-stage leaf，应落到 first-stage guest/page fault，而不是最终 data GPA fault。"""

    state = _reset_env_state(env)
    fault_va = LLPTW_VA_BASE + 0x508
    result = MmuTwoStageFaultSequence(
        config=_build_default_config(((fault_va, LLPTW_DATA_BASE + 0x30),)),
        va=fault_va,
        initial_state=state,
        req_id=0x40,
        vs_asid=LLPTW_VS_ASID,
        vmid=LLPTW_VMID,
        fault_stage="g",
        fault_target_gpa=LLPTW_VS_ROOT_PT_A,
    ).run(env)

    fault = result.access.fault_event
    assert fault is not None
    assert not fault["is_access_fault"]
    assert fault["is_guest_page_fault"] or fault["is_page_fault"]
    assert fault["vaddr"] == fault_va
    env.assert_no_outstanding()


def test_api_MemBlock_mmu_llptw_last_stage_guest_page_fault_smoke(env):
    """最终 data GPA 的 G-stage leaf invalid 时，应命中 last-stage guest/page fault 收口。"""

    state = _reset_env_state(env)
    fault_va = LLPTW_VA_BASE + 0x608
    result = MmuTwoStageFaultSequence(
        config=_build_default_config(((fault_va, LLPTW_DATA_BASE + 0x31),)),
        va=fault_va,
        initial_state=state,
        req_id=0x50,
        vs_asid=LLPTW_VS_ASID,
        vmid=LLPTW_VMID,
        fault_stage="g",
    ).run(env)

    fault = result.access.fault_event
    assert fault is not None
    assert not fault["is_access_fault"]
    assert fault["is_guest_page_fault"] or fault["is_page_fault"]
    assert fault["vaddr"] == fault_va
    env.assert_no_outstanding()


def test_api_MemBlock_mmu_llptw_final_stage_pmp_access_fault_smoke(env):
    """两阶段翻译成功后若最终 HPA 被 PMP deny，LLPTW 应收口到 access fault。"""

    state = _reset_env_state(env)
    fault_va = LLPTW_VA_BASE + 0x708
    result = MmuTwoStageFaultSequence(
        config=_build_default_config(((fault_va, LLPTW_DATA_BASE + 0x32),)),
        va=fault_va,
        initial_state=state,
        req_id=0x60,
        vs_asid=LLPTW_VS_ASID,
        vmid=LLPTW_VMID,
        pmp_mode=PMP_MODE_DENY,
        required_exception_bits=(LOAD_ACCESS_FAULT_BIT,),
    ).run(env)

    fault = result.access.fault_event
    assert fault is not None
    assert fault["is_access_fault"]
    assert not fault["is_guest_page_fault"]
    env.assert_no_outstanding()
