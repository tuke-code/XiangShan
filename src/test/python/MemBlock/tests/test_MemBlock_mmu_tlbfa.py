# coding=utf-8
"""
MemBlock TLBFA-oriented Sv39 directed matrix。
"""

from __future__ import annotations

from dataclasses import dataclass

from request_apis import send_load, send_store
from sequences import (
    GStageSv39Mapping,
    MmuAccessSpec,
    MmuSv39AddressSpaceConfig,
    MmuSv39AddressSpaceInstallSequence,
    MmuSv39FenceMatrixSequence,
    ResetEnvSequence,
    ScalarLoadBatchSameCycleSequence,
    Sv39Mapping,
    TwoStageSv39AddressSpaceConfig,
    TwoStageSv39AddressSpaceInstallSequence,
    TranslatedU64MemoryPreload,
)
from transactions import LoadTxn, StoreTxn, ptr_inc


TLBFA_ROOT_PT = 0x88600000
TLBFA_ALT_ROOT_PT = 0x88700000
TLBFA_PAGE_TABLE_PAGES = tuple(0x88601000 + index * 0x1000 for index in range(6))
TLBFA_ALT_PAGE_TABLE_PAGES = tuple(0x88701000 + index * 0x1000 for index in range(6))
TLBFA_VA_A = 0x40401000
TLBFA_VA_B = 0x40402000
TLBFA_PA_SPACE_A = 0x80600000
TLBFA_PA_SPACE_B = 0x80700000
TLBFA_ALT_PA_SPACE = 0x80800000
TLBFA_HPA_SPACE = 0x80900000
TLBFA_GPA_SPACE = 0x20040000
TLBFA_DATA_A = 0x1122334455667788
TLBFA_DATA_B = 0x8877665544332211
TLBFA_ALT_DATA = 0x13579BDF2468ACE0
TLBFA_ASID = 0x21
TLBFA_VS_ROOT_PT = 0x88800000
TLBFA_G_ROOT_PT = 0x88900000
TLBFA_VS_PAGE_TABLE_PAGES = (
    0x88801000,
    0x88802000,
    0x88803000,
    0x88804000,
)
TLBFA_G_PAGE_TABLE_PAGES = tuple(0x88904000 + index * 0x1000 for index in range(8))
TLBFA_VMID = 0x2B
SV39_PTE_X = 1 << 3
SV39_PTE_U = 1 << 4
SV39_PTE_G = 1 << 5


@dataclass(frozen=True)
class _ManualSv39AccessResult:
    txn: LoadTxn
    expected_pa: int
    writeback: dict
    ptw_trace: tuple[dict, ...]

    @property
    def miss_observed(self) -> bool:
        return bool(self.ptw_trace)


def _reset_env_state(env):
    return ResetEnvSequence(
        require_issue_lanes=(0,),
        require_lq_ready=True,
        require_sq_ready=True,
    ).run(env)


def _sv39_4k_mapping_pa_base(space_pa_base: int, addr: int) -> int:
    page_offset = (int(addr) & ((1 << 30) - 1)) & ~0xFFF
    return int(space_pa_base) | page_offset


def _build_sv39_config(
    *,
    root_pt_addr: int,
    page_table_page_addrs: tuple[int, ...],
    entries: tuple[tuple[int, int, int], ...],
) -> MmuSv39AddressSpaceConfig:
    return MmuSv39AddressSpaceConfig(
        root_pt_addr=root_pt_addr,
        mappings=tuple(
            Sv39Mapping(va=int(va) & ~0xFFF, pa_base=_sv39_4k_mapping_pa_base(space_pa_base, va))
            for va, _data, space_pa_base in entries
        ),
        page_table_page_addrs=page_table_page_addrs,
        translated_preloads=tuple(
            TranslatedU64MemoryPreload(va=int(va), data=int(data))
            for va, data, _space_pa_base in entries
        ),
    )


def _run_manual_sv39_access(env, *, ptw, va: int, expected_pa: int, expected_data: int, req_id: int, lq_ptr, sq_ptr):
    completed_before = env.get_completed_load_count()
    txn = LoadTxn(req_id=int(req_id), addr=int(va), lq_ptr=lq_ptr, sq_ptr=sq_ptr)
    env.backend.prepare(txn)
    expected = env.expect_scalar_load(
        rob_idx=txn.rob_idx,
        pdest=txn.resolved_pdest,
        addr=int(expected_pa),
        size=txn.size,
        mask=txn.mask,
    )
    expected.expected_data = int(expected_data)
    trace_start = len(ptw.trace)
    send_load(env, txn)
    writeback = env.wait_load_writeback_observed(
        rob_idx=txn.rob_idx,
        data=int(expected_data),
        max_cycles=256,
    )
    env.wait_completed_load_count(completed_before + 1, max_cycles=256)
    return _ManualSv39AccessResult(
        txn=txn,
        expected_pa=int(expected_pa),
        writeback=writeback,
        ptw_trace=tuple(list(ptw.trace)[trace_start:]),
    )


def _patch_leaf_pte(env, *, leaf_pte_addr: int, set_bits: int = 0) -> int:
    patched = int(env.memory.read(int(leaf_pte_addr), 8)) | int(set_bits)
    env.preload_u64(int(leaf_pte_addr), patched)
    return patched


def _combined_cross_page_u64(low_word: int, high_word: int, *, first_page_tail_bytes: int) -> int:
    low_bytes = int(low_word).to_bytes(8, "little")[8 - int(first_page_tail_bytes):]
    high_bytes = int(high_word).to_bytes(8, "little")[: 8 - int(first_page_tail_bytes)]
    return int.from_bytes(low_bytes + high_bytes, "little")


def _wait_any_store_view(env, sq_idx: int, *, max_cycles: int = 256):
    def _store_ready():
        store = env.get_store_view(sq_idx)
        if store is not None and store.allocated and store.data is not None and store.mask != 0:
            return store
        return None

    return env.wait_until(
        _store_ready,
        max_cycles=max_cycles,
        timeout_message=f"等待 sqIdx={sq_idx} 的 store view 收敛超时",
    )


def _run_translated_store(
    env,
    *,
    sq_ptr,
    req_id: int,
    va: int,
    expected_pa: int,
    data: int,
    sta_lane: int = 3,
    std_lane: int = 5,
    expected_nc: bool | None = None,
    max_cycles: int = 256,
):
    txn = StoreTxn(
        req_id=int(req_id),
        sq_ptr=sq_ptr,
        addr=int(va),
        data=int(data),
        sta_lane=int(sta_lane),
        std_lane=int(std_lane),
    )
    allocated_sq_ptr = send_store(env, txn)
    env.backend.pulse_store_commit(1)
    env.advance_cycles(env.config.sequence.store_settle_cycles)
    store_view = _wait_any_store_view(env, allocated_sq_ptr.value, max_cycles=max_cycles)
    next_sq_ptr = ptr_inc(sq_ptr, env.config.sequence.store_queue_size)
    return txn, allocated_sq_ptr, next_sq_ptr, store_view


def _run_expected_load_batch(env, *, txns, expected_paddrs, expected_datas, max_cycles: int = 512):
    completed_before = env.get_completed_load_count()
    for txn, expected_pa in zip(txns, expected_paddrs):
        env.backend.prepare(txn)
        env.expect_scalar_load(
            rob_idx=txn.rob_idx,
            pdest=txn.resolved_pdest,
            addr=int(expected_pa),
            size=txn.size,
            mask=txn.mask,
        )
    ScalarLoadBatchSameCycleSequence(txns, max_cycles=max_cycles).run(env)
    writebacks = tuple(
        env.wait_load_writeback_observed(
            rob_idx=txn.rob_idx,
            data=int(expected_data),
            max_cycles=max_cycles,
        )
        for txn, expected_data in zip(txns, expected_datas)
    )
    env.wait_completed_load_count(completed_before + len(txns), max_cycles=max_cycles)
    env.drain_writebacks(max_cycles=max_cycles)
    return writebacks


def _build_two_stage_store_config():
    g_mappings = [
        GStageSv39Mapping(gpa=TLBFA_VS_ROOT_PT, pa_base=TLBFA_VS_ROOT_PT),
        *(GStageSv39Mapping(gpa=addr, pa_base=addr) for addr in TLBFA_VS_PAGE_TABLE_PAGES),
    ]
    translated_preloads = []
    vs_mappings = []
    for va in (TLBFA_VA_A, TLBFA_VA_B):
        gpa_base = _sv39_4k_mapping_pa_base(TLBFA_GPA_SPACE, va)
        hpa_base = _sv39_4k_mapping_pa_base(TLBFA_HPA_SPACE, gpa_base)
        vs_mappings.append(Sv39Mapping(va=int(va), pa_base=gpa_base))
        g_mappings.append(GStageSv39Mapping(gpa=gpa_base, pa_base=hpa_base))
        translated_preloads.append(TranslatedU64MemoryPreload(va=int(va) + 0x40, data=0))
    return TwoStageSv39AddressSpaceConfig(
        vs_root_pt_addr=TLBFA_VS_ROOT_PT,
        g_root_pt_addr=TLBFA_G_ROOT_PT,
        vs_mappings=tuple(vs_mappings),
        g_mappings=tuple(g_mappings),
        vs_page_table_page_addrs=TLBFA_VS_PAGE_TABLE_PAGES,
        g_page_table_page_addrs=TLBFA_G_PAGE_TABLE_PAGES,
        translated_preloads=tuple(translated_preloads),
    )


def test_api_MemBlock_mmu_sv39_sfence_all_remiss_matrix(env):
    """全局 `sfence.vma` 需要打出 miss -> hit -> remiss 的最小矩阵。"""

    state = _reset_env_state(env)
    config = _build_sv39_config(
        root_pt_addr=TLBFA_ROOT_PT,
        page_table_page_addrs=TLBFA_PAGE_TABLE_PAGES,
        entries=((TLBFA_VA_A + 0x8, TLBFA_DATA_A, TLBFA_PA_SPACE_A),),
    )
    result = MmuSv39FenceMatrixSequence(
        config=config,
        initial_state=state,
        warmup_specs=(
            MmuAccessSpec(req_id=0, va=TLBFA_VA_A + 0x8, expected_data=TLBFA_DATA_A),
            MmuAccessSpec(req_id=1, va=TLBFA_VA_A + 0x8, expected_data=TLBFA_DATA_A),
        ),
        reprobe_specs=(MmuAccessSpec(req_id=2, va=TLBFA_VA_A + 0x8, expected_data=TLBFA_DATA_A),),
        asid=TLBFA_ASID,
    ).run(env)

    assert result.warmup_accesses[0].miss_observed
    assert not result.warmup_accesses[1].miss_observed
    assert result.reprobe_accesses[0].miss_observed
    assert result.reprobe_accesses[0].writeback["data"] == TLBFA_DATA_A
    env.assert_no_outstanding()


def test_api_MemBlock_mmu_sv39_sfence_specific_addr_preserves_other_translation(env):
    """`sfence.vma(addr, all asid)` 只应清掉目标页，而非无关页。"""

    state = _reset_env_state(env)
    config = _build_sv39_config(
        root_pt_addr=TLBFA_ROOT_PT,
        page_table_page_addrs=TLBFA_PAGE_TABLE_PAGES,
        entries=(
            (TLBFA_VA_A + 0x8, TLBFA_DATA_A, TLBFA_PA_SPACE_A),
            (TLBFA_VA_B + 0x8, TLBFA_DATA_B, TLBFA_PA_SPACE_B),
        ),
    )
    result = MmuSv39FenceMatrixSequence(
        config=config,
        initial_state=state,
        warmup_specs=(
            MmuAccessSpec(req_id=0, va=TLBFA_VA_A + 0x8, expected_data=TLBFA_DATA_A),
            MmuAccessSpec(req_id=1, va=TLBFA_VA_B + 0x8, expected_data=TLBFA_DATA_B),
        ),
        reprobe_specs=(
            MmuAccessSpec(req_id=2, va=TLBFA_VA_A + 0x8, expected_data=TLBFA_DATA_A),
            MmuAccessSpec(req_id=3, va=TLBFA_VA_B + 0x8, expected_data=TLBFA_DATA_B),
        ),
        asid=TLBFA_ASID,
        fence_addr=TLBFA_VA_A,
    ).run(env)

    assert all(access.miss_observed for access in result.warmup_accesses)
    assert result.reprobe_accesses[0].miss_observed
    assert not result.reprobe_accesses[1].miss_observed
    env.assert_no_outstanding()


def test_api_MemBlock_mmu_sv39_sfence_specific_asid_remiss_smoke(env):
    """`sfence.vma(all addr, asid)` 需要把当前 ASID 的已建 translation 清成 re-miss。"""

    state = _reset_env_state(env)
    config = _build_sv39_config(
        root_pt_addr=TLBFA_ROOT_PT,
        page_table_page_addrs=TLBFA_PAGE_TABLE_PAGES,
        entries=((TLBFA_VA_A + 0x18, TLBFA_DATA_A, TLBFA_PA_SPACE_A),),
    )
    result = MmuSv39FenceMatrixSequence(
        config=config,
        initial_state=state,
        warmup_specs=(MmuAccessSpec(req_id=0, va=TLBFA_VA_A + 0x18, expected_data=TLBFA_DATA_A),),
        reprobe_specs=(MmuAccessSpec(req_id=1, va=TLBFA_VA_A + 0x18, expected_data=TLBFA_DATA_A),),
        asid=TLBFA_ASID,
        fence_asid=TLBFA_ASID,
    ).run(env)

    assert result.warmup_accesses[0].miss_observed
    assert result.reprobe_accesses[0].miss_observed
    env.assert_no_outstanding()


def test_api_MemBlock_mmu_sv39_sfence_specific_addr_specific_asid_remiss_smoke(env):
    """`sfence.vma(addr, asid)` 需要命中最细粒度的 selective flush 路径。"""

    state = _reset_env_state(env)
    config = _build_sv39_config(
        root_pt_addr=TLBFA_ROOT_PT,
        page_table_page_addrs=TLBFA_PAGE_TABLE_PAGES,
        entries=((TLBFA_VA_B + 0x18, TLBFA_DATA_B, TLBFA_PA_SPACE_B),),
    )
    result = MmuSv39FenceMatrixSequence(
        config=config,
        initial_state=state,
        warmup_specs=(MmuAccessSpec(req_id=0, va=TLBFA_VA_B + 0x18, expected_data=TLBFA_DATA_B),),
        reprobe_specs=(MmuAccessSpec(req_id=1, va=TLBFA_VA_B + 0x18, expected_data=TLBFA_DATA_B),),
        asid=TLBFA_ASID,
        fence_addr=TLBFA_VA_B,
        fence_asid=TLBFA_ASID,
    ).run(env)

    assert result.warmup_accesses[0].miss_observed
    assert result.reprobe_accesses[0].miss_observed
    env.assert_no_outstanding()


def test_api_MemBlock_mmu_sv39_root_switch_same_va_refills_new_translation(env):
    """同一 VA 在 root-A/root-B 间切换时不应残留旧 translation。"""

    state = _reset_env_state(env)
    config_a = _build_sv39_config(
        root_pt_addr=TLBFA_ROOT_PT,
        page_table_page_addrs=TLBFA_PAGE_TABLE_PAGES,
        entries=((TLBFA_VA_A + 0x28, TLBFA_DATA_A, TLBFA_PA_SPACE_A),),
    )
    config_b = _build_sv39_config(
        root_pt_addr=TLBFA_ALT_ROOT_PT,
        page_table_page_addrs=TLBFA_ALT_PAGE_TABLE_PAGES,
        entries=((TLBFA_VA_A + 0x28, TLBFA_ALT_DATA, TLBFA_ALT_PA_SPACE),),
    )
    install_a = MmuSv39AddressSpaceInstallSequence(config_a).run(env)
    install_b = MmuSv39AddressSpaceInstallSequence(config_b).run(env)
    next_lq_ptr = state.next_lq_ptr

    env.mmu.allow_all_smode_access(persistent=False)
    with env.mmu.ptw_responder() as ptw:
        env.mmu.enable_sv39(root_pt_addr=TLBFA_ROOT_PT, asid=TLBFA_ASID)
        first = _run_manual_sv39_access(
            env,
            ptw=ptw,
            va=TLBFA_VA_A + 0x28,
            expected_pa=install_a.translated_pa_for(TLBFA_VA_A + 0x28),
            expected_data=TLBFA_DATA_A,
            req_id=0,
            lq_ptr=next_lq_ptr,
            sq_ptr=state.sq_ptr,
        )
        next_lq_ptr = ptr_inc(next_lq_ptr, env.config.sequence.load_queue_size)
        second = _run_manual_sv39_access(
            env,
            ptw=ptw,
            va=TLBFA_VA_A + 0x28,
            expected_pa=install_a.translated_pa_for(TLBFA_VA_A + 0x28),
            expected_data=TLBFA_DATA_A,
            req_id=1,
            lq_ptr=next_lq_ptr,
            sq_ptr=state.sq_ptr,
        )
        next_lq_ptr = ptr_inc(next_lq_ptr, env.config.sequence.load_queue_size)
        env.mmu.enable_sv39(root_pt_addr=TLBFA_ALT_ROOT_PT, asid=TLBFA_ASID)
        switched = _run_manual_sv39_access(
            env,
            ptw=ptw,
            va=TLBFA_VA_A + 0x28,
            expected_pa=install_b.translated_pa_for(TLBFA_VA_A + 0x28),
            expected_data=TLBFA_ALT_DATA,
            req_id=2,
            lq_ptr=next_lq_ptr,
            sq_ptr=state.sq_ptr,
        )

    assert first.miss_observed
    assert not second.miss_observed
    assert switched.miss_observed
    assert switched.writeback["data"] == TLBFA_ALT_DATA
    env.assert_no_outstanding()


def test_api_MemBlock_mmu_sv39_store_dual_units_refill_smoke(env):
    """单阶段 translated store 需要命中两个 store requestor，并携带不同 S1 refill 元数据。"""

    state = ResetEnvSequence(
        require_issue_lanes=(3, 4, 5, 6),
        require_sq_ready=True,
    ).run(env)
    install_a = env.mmu.install_sv39_mapping(
        root_pt_addr=TLBFA_ROOT_PT,
        va=TLBFA_VA_A,
        pa_base=_sv39_4k_mapping_pa_base(TLBFA_PA_SPACE_A, TLBFA_VA_A),
        page_table_page_addrs=TLBFA_PAGE_TABLE_PAGES[0:2],
    )
    install_b = env.mmu.install_sv39_mapping(
        root_pt_addr=TLBFA_ROOT_PT,
        va=TLBFA_VA_B,
        pa_base=_sv39_4k_mapping_pa_base(TLBFA_PA_SPACE_B, TLBFA_VA_B),
        page_table_page_addrs=TLBFA_PAGE_TABLE_PAGES[2:4],
    )
    _patch_leaf_pte(env, leaf_pte_addr=install_a["leaf_pte_addr"], set_bits=SV39_PTE_X | SV39_PTE_U | SV39_PTE_G)
    env.mmu.allow_all_smode_access(persistent=False)

    with env.mmu.ptw_responder() as ptw:
        env.mmu.enable_sv39(root_pt_addr=TLBFA_ROOT_PT, asid=TLBFA_ASID)
        _, _, next_sq_ptr, first_store = _run_translated_store(
            env,
            sq_ptr=state.sq_ptr,
            req_id=0x80,
            va=TLBFA_VA_A + 0x18,
            expected_pa=_sv39_4k_mapping_pa_base(TLBFA_PA_SPACE_A, TLBFA_VA_A + 0x18),
            data=TLBFA_DATA_A,
            sta_lane=3,
            std_lane=5,
        )
        _, _, _, second_store = _run_translated_store(
            env,
            sq_ptr=next_sq_ptr,
            req_id=0x81,
            va=TLBFA_VA_B + 0x28,
            expected_pa=_sv39_4k_mapping_pa_base(TLBFA_PA_SPACE_B, TLBFA_VA_B + 0x28),
            data=TLBFA_DATA_B,
            sta_lane=4,
            std_lane=6,
        )
        ptw_events = tuple(ptw.trace)

    assert first_store.data == TLBFA_DATA_A
    assert not first_store.has_exception
    assert second_store.data == TLBFA_DATA_B
    assert not second_store.has_exception
    assert sum(1 for event in ptw_events if event["event"] == "a_fire") >= 1
    env.reset(cycles=1, settle_cycles=0)


def test_api_MemBlock_mmu_h_two_stage_store_dual_units_smoke(env):
    """两阶段 translated store 需要把 VMID/S2 refill 路径打到两个 store requestor。"""

    state = ResetEnvSequence(
        require_issue_lanes=(3, 4, 5, 6),
        require_sq_ready=True,
    ).run(env)
    config = _build_two_stage_store_config()
    install_result = TwoStageSv39AddressSpaceInstallSequence(config).run(env)
    env.mmu.allow_all_smode_access(persistent=False)

    with env.mmu.ptw_responder() as ptw:
        env.mmu.enable_two_stage_sv39(
            vs_root_pt_addr=TLBFA_VS_ROOT_PT,
            g_root_pt_addr=TLBFA_G_ROOT_PT,
            vs_asid=TLBFA_ASID,
            vmid=TLBFA_VMID,
        )
        _, _, next_sq_ptr, first_store = _run_translated_store(
            env,
            sq_ptr=state.sq_ptr,
            req_id=0x90,
            va=TLBFA_VA_A + 0x40,
            expected_pa=install_result.resolve_two_stage_pa(TLBFA_VA_A + 0x40),
            data=TLBFA_DATA_A ^ 0x55,
            sta_lane=3,
            std_lane=5,
        )
        _, _, _, second_store = _run_translated_store(
            env,
            sq_ptr=next_sq_ptr,
            req_id=0x91,
            va=TLBFA_VA_B + 0x40,
            expected_pa=install_result.resolve_two_stage_pa(TLBFA_VA_B + 0x40),
            data=TLBFA_DATA_B ^ 0xAA,
            sta_lane=4,
            std_lane=6,
        )
        ptw_events = tuple(ptw.trace)

    assert first_store.data == (TLBFA_DATA_A ^ 0x55)
    assert second_store.data == (TLBFA_DATA_B ^ 0xAA)
    assert not first_store.has_exception
    assert not second_store.has_exception
    env.reset(cycles=1, settle_cycles=0)


def test_api_MemBlock_mmu_sv39_load_three_lanes_same_cycle_miss_hit_matrix(env):
    """三条 load lane 同拍访问应覆盖 load-side TLB 的三个 requestor，并区分 miss/hit。"""

    state = ResetEnvSequence(
        require_issue_lanes=(0, 1, 2),
        require_lq_ready=True,
        require_sq_ready=True,
    ).run(env)
    config = _build_sv39_config(
        root_pt_addr=TLBFA_ROOT_PT,
        page_table_page_addrs=TLBFA_PAGE_TABLE_PAGES,
        entries=(
            (TLBFA_VA_A + 0x08, TLBFA_DATA_A, TLBFA_PA_SPACE_A),
            (TLBFA_VA_A + 0x108, TLBFA_DATA_A ^ 0x100, TLBFA_PA_SPACE_A),
            (TLBFA_VA_B + 0x208, TLBFA_DATA_B, TLBFA_PA_SPACE_B),
        ),
    )
    install_result = MmuSv39AddressSpaceInstallSequence(config).run(env)
    env.mmu.allow_all_smode_access(persistent=False)

    first_batch = (
        LoadTxn(req_id=0xA0, addr=TLBFA_VA_A + 0x08, lq_ptr=state.next_lq_ptr, sq_ptr=state.sq_ptr, issue_lane=0),
        LoadTxn(
            req_id=0xA1,
            addr=TLBFA_VA_A + 0x108,
            lq_ptr=ptr_inc(state.next_lq_ptr, env.config.sequence.load_queue_size),
            sq_ptr=state.sq_ptr,
            issue_lane=1,
        ),
        LoadTxn(
            req_id=0xA2,
            addr=TLBFA_VA_B + 0x208,
            lq_ptr=ptr_inc(state.next_lq_ptr, env.config.sequence.load_queue_size, step=2),
            sq_ptr=state.sq_ptr,
            issue_lane=2,
        ),
    )
    second_batch = (
        LoadTxn(
            req_id=0xA3,
            addr=TLBFA_VA_A + 0x08,
            lq_ptr=ptr_inc(state.next_lq_ptr, env.config.sequence.load_queue_size, step=3),
            sq_ptr=state.sq_ptr,
            issue_lane=0,
        ),
        LoadTxn(
            req_id=0xA4,
            addr=TLBFA_VA_A + 0x108,
            lq_ptr=ptr_inc(state.next_lq_ptr, env.config.sequence.load_queue_size, step=4),
            sq_ptr=state.sq_ptr,
            issue_lane=1,
        ),
        LoadTxn(
            req_id=0xA5,
            addr=TLBFA_VA_B + 0x208,
            lq_ptr=ptr_inc(state.next_lq_ptr, env.config.sequence.load_queue_size, step=5),
            sq_ptr=state.sq_ptr,
            issue_lane=2,
        ),
    )
    expected_paddrs = (
        install_result.translated_pa_for(TLBFA_VA_A + 0x08),
        install_result.translated_pa_for(TLBFA_VA_A + 0x108),
        install_result.translated_pa_for(TLBFA_VA_B + 0x208),
    )
    expected_datas = (TLBFA_DATA_A, TLBFA_DATA_A ^ 0x100, TLBFA_DATA_B)

    with env.mmu.ptw_responder() as ptw:
        env.mmu.enable_sv39(root_pt_addr=TLBFA_ROOT_PT, asid=TLBFA_ASID)
        _run_expected_load_batch(
            env,
            txns=first_batch,
            expected_paddrs=expected_paddrs,
            expected_datas=expected_datas,
        )
        miss_trace = tuple(ptw.trace)
        hit_trace_start = len(ptw.trace)
        hit_writebacks = _run_expected_load_batch(
            env,
            txns=second_batch,
            expected_paddrs=expected_paddrs,
            expected_datas=expected_datas,
        )
        hit_trace = tuple(list(ptw.trace)[hit_trace_start:])

    assert len(miss_trace) > 0
    assert len(hit_trace) == 0
    assert tuple(writeback["data"] for writeback in hit_writebacks) == expected_datas
    env.assert_no_outstanding()


def test_api_MemBlock_mmu_h_two_stage_load_three_lanes_same_cycle_smoke(env):
    """two-stage 三 lane 同拍 load 需要把 load-side TLB 的 `s2xlate/requestor2` 路径稳定打热。"""

    state = ResetEnvSequence(
        require_issue_lanes=(0, 1, 2),
        require_lq_ready=True,
        require_sq_ready=True,
    ).run(env)
    entries = (
        (TLBFA_VA_A + 0x48, TLBFA_DATA_A ^ 0x10),
        (TLBFA_VA_A + 0x148, TLBFA_DATA_A ^ 0x20),
        (TLBFA_VA_B + 0x248, TLBFA_DATA_B ^ 0x30),
    )
    resolved = {}
    g_mappings = [
        GStageSv39Mapping(gpa=TLBFA_VS_ROOT_PT, pa_base=TLBFA_VS_ROOT_PT),
        *(GStageSv39Mapping(gpa=addr, pa_base=addr) for addr in TLBFA_VS_PAGE_TABLE_PAGES),
    ]
    for va, data in entries:
        gpa_base = _sv39_4k_mapping_pa_base(TLBFA_GPA_SPACE, va)
        hpa_base = _sv39_4k_mapping_pa_base(TLBFA_HPA_SPACE, gpa_base)
        resolved[int(va)] = {"data": int(data), "gpa_base": gpa_base, "hpa_base": hpa_base}
        g_mappings.append(GStageSv39Mapping(gpa=gpa_base, pa_base=hpa_base))

    config = TwoStageSv39AddressSpaceConfig(
        vs_root_pt_addr=TLBFA_VS_ROOT_PT,
        g_root_pt_addr=TLBFA_G_ROOT_PT,
        vs_mappings=tuple(
            Sv39Mapping(va=int(va) & ~0xFFF, pa_base=resolved[int(va)]["gpa_base"]) for va, _ in entries
        ),
        g_mappings=tuple(g_mappings),
        vs_page_table_page_addrs=TLBFA_VS_PAGE_TABLE_PAGES,
        g_page_table_page_addrs=TLBFA_G_PAGE_TABLE_PAGES,
        translated_preloads=tuple(
            TranslatedU64MemoryPreload(va=int(va), data=resolved[int(va)]["data"]) for va, _ in entries
        ),
    )
    install_result = TwoStageSv39AddressSpaceInstallSequence(config).run(env)
    env.mmu.allow_all_smode_access(persistent=False)

    first_batch = (
        LoadTxn(req_id=0xB0, addr=entries[0][0], lq_ptr=state.next_lq_ptr, sq_ptr=state.sq_ptr, issue_lane=0),
        LoadTxn(
            req_id=0xB1,
            addr=entries[1][0],
            lq_ptr=ptr_inc(state.next_lq_ptr, env.config.sequence.load_queue_size),
            sq_ptr=state.sq_ptr,
            issue_lane=1,
        ),
        LoadTxn(
            req_id=0xB2,
            addr=entries[2][0],
            lq_ptr=ptr_inc(state.next_lq_ptr, env.config.sequence.load_queue_size, step=2),
            sq_ptr=state.sq_ptr,
            issue_lane=2,
        ),
    )
    second_batch = (
        LoadTxn(
            req_id=0xB3,
            addr=entries[0][0],
            lq_ptr=ptr_inc(state.next_lq_ptr, env.config.sequence.load_queue_size, step=3),
            sq_ptr=state.sq_ptr,
            issue_lane=0,
        ),
        LoadTxn(
            req_id=0xB4,
            addr=entries[1][0],
            lq_ptr=ptr_inc(state.next_lq_ptr, env.config.sequence.load_queue_size, step=4),
            sq_ptr=state.sq_ptr,
            issue_lane=1,
        ),
        LoadTxn(
            req_id=0xB5,
            addr=entries[2][0],
            lq_ptr=ptr_inc(state.next_lq_ptr, env.config.sequence.load_queue_size, step=5),
            sq_ptr=state.sq_ptr,
            issue_lane=2,
        ),
    )
    expected_paddrs = tuple(install_result.resolve_two_stage_pa(int(va)) for va, _ in entries)
    expected_datas = tuple(resolved[int(va)]["data"] for va, _ in entries)

    with env.mmu.ptw_responder() as ptw:
        env.mmu.enable_two_stage_sv39(
            vs_root_pt_addr=TLBFA_VS_ROOT_PT,
            g_root_pt_addr=TLBFA_G_ROOT_PT,
            vs_asid=TLBFA_ASID,
            vmid=TLBFA_VMID,
        )
        _run_expected_load_batch(
            env,
            txns=first_batch,
            expected_paddrs=expected_paddrs,
            expected_datas=expected_datas,
        )
        miss_trace = tuple(ptw.trace)
        hit_trace_start = len(ptw.trace)
        hit_writebacks = _run_expected_load_batch(
            env,
            txns=second_batch,
            expected_paddrs=expected_paddrs,
            expected_datas=expected_datas,
        )
        hit_trace = tuple(list(ptw.trace)[hit_trace_start:])

    assert len(miss_trace) > 0
    assert len(hit_trace) == 0
    assert tuple(writeback["data"] for writeback in hit_writebacks) == expected_datas
    env.assert_no_outstanding()
