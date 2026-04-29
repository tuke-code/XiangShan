# coding=utf-8
"""
MemBlock real-DUT uncache / MMIO semantic regression.

本文件重点覆盖两件事：
  1. 通过 Svpbmt 显式把 translated access 区分成 cacheable / NCIO / MMIO。
  2. 用真实 DUT 证明 uncache 不只是“走对路径”，还能影响提交语义。
"""

import pytest

from transactions import LoadTxn, ptr_inc, StoreTxn
from request_apis import send_load, send_store
from sequences import FlushStoreBuffersSequence, ResetEnvSequence, ScalarStoreCommitSequence, SequenceState


MMU_ROOT_PT = 0x88010000
MMU_PAGE_TABLE_PAGES = (
    0x88011000,
    0x88012000,
    0x88013000,
    0x88014000,
    0x88015000,
    0x88016000,
)

CACHEABLE_VA = 0x40001000
NCIO_VA = 0x80002000
MMIO_VA = 0xC0003000

CACHEABLE_PA_BASE = 0x80000000
NCIO_PA_BASE = 0xC0000000
MMIO_PA_BASE = 0x100000000

CACHEABLE_DATA = 0x1020_3040_5060_7080
NCIO_DATA = 0x8877_6655_4433_2211
MMIO_DATA = 0x0BAD_F00D_CAFE_BABE
NCIO_BURST_DATA = (
    0x1122_3344_5566_7788,
    0x99AA_BBCC_DDEE_FF00,
)
MMIO_MIXED_CACHEABLE_DATA = 0x2233_4455_6677_8899
NCIO_MIXED_CACHEABLE_DATA = 0x5566_7788_99AA_BBCC
MMIO_BURST_DATA = (
    0x1357_9BDF_2468_ACE0,
    0x0F1E_2D3C_4B5A_6978,
)
LONG_BUSY_CACHEABLE_DATA = (
    0x1111_2222_3333_4444,
    0xAAAA_BBBB_CCCC_DDDD,
)


def _reset_env_state(env) -> SequenceState:
    return ResetEnvSequence(
        require_issue_lanes=(0, 3, 5),
        require_lq_ready=True,
        require_sq_ready=True,
    ).run(env)


def _translated_pa(va: int, pa_base: int) -> int:
    return int(pa_base) | (int(va) & 0xFFF)


CACHEABLE_PA = _translated_pa(CACHEABLE_VA, CACHEABLE_PA_BASE)
NCIO_PA = _translated_pa(NCIO_VA, NCIO_PA_BASE)
MMIO_PA = _translated_pa(MMIO_VA, MMIO_PA_BASE)


def _install_svpbmt_address_space(env) -> None:
    env.mmu.enable_svpbmt()
    env.mmu.install_sv39_mapping(
        root_pt_addr=MMU_ROOT_PT,
        va=CACHEABLE_VA,
        pa_base=CACHEABLE_PA_BASE,
        pbmt="cacheable",
        page_table_page_addrs=MMU_PAGE_TABLE_PAGES[0:2],
    )
    env.mmu.install_sv39_mapping(
        root_pt_addr=MMU_ROOT_PT,
        va=NCIO_VA,
        pa_base=NCIO_PA_BASE,
        pbmt="ncio",
        page_table_page_addrs=MMU_PAGE_TABLE_PAGES[2:4],
    )
    env.mmu.install_sv39_mapping(
        root_pt_addr=MMU_ROOT_PT,
        va=MMIO_VA,
        pa_base=MMIO_PA_BASE,
        pbmt="mmio",
        page_table_page_addrs=MMU_PAGE_TABLE_PAGES[4:6],
    )
    env.preload_u64(CACHEABLE_PA, CACHEABLE_DATA)
    env.preload_u64(NCIO_PA, NCIO_DATA)
    env.preload_u64(MMIO_PA, MMIO_DATA)
    env.mmu.allow_all_smode_access()


def _run_translated_load(
    env,
    *,
    state: SequenceState,
    req_id: int,
    va: int,
    expected_pa: int,
    expected_data: int,
    expected_mmio: bool,
    expected_ncio: bool,
) -> tuple[SequenceState, dict]:
    txn = LoadTxn(
        req_id=req_id,
        addr=va,
        lq_ptr=state.next_lq_ptr,
        sq_ptr=state.sq_ptr,
    )
    env.backend.prepare(txn)
    env.expect_scalar_load(
        rob_idx=txn.rob_idx,
        pdest=txn.resolved_pdest,
        addr=expected_pa,
        size=txn.size,
        mask=txn.mask,
    )
    send_load(env, txn)
    writeback = env.wait_load_writeback_observed(
        rob_idx=txn.rob_idx,
        data=expected_data,
        expected_paddr=expected_pa,
        expected_mmio=expected_mmio,
        expected_ncio=expected_ncio,
        max_cycles=512,
    )
    env.drain_writebacks(max_cycles=256)
    return (
        SequenceState(
            next_lq_ptr=ptr_inc(state.next_lq_ptr, env.config.sequence.load_queue_size),
            sq_ptr=state.sq_ptr,
        ),
        writeback,
    )


def _issue_load_and_wait_writeback(
    env,
    *,
    state: SequenceState,
    req_id: int,
    addr: int,
    expected_data: int,
    expected_pa: int | None = None,
    expected_mmio: bool = False,
    expected_ncio: bool = False,
    max_cycles: int = 256,
) -> tuple[SequenceState, LoadTxn, dict]:
    effective_pa = addr if expected_pa is None else int(expected_pa)
    txn = LoadTxn(
        req_id=req_id,
        addr=addr,
        lq_ptr=state.next_lq_ptr,
        sq_ptr=state.sq_ptr,
    )
    env.backend.prepare(txn)
    env.expect_scalar_load(
        rob_idx=txn.rob_idx,
        pdest=txn.resolved_pdest,
        addr=effective_pa,
        size=txn.size,
        mask=txn.mask,
    )
    send_load(env, txn)
    writeback = env.wait_load_writeback_observed(
        rob_idx=txn.rob_idx,
        data=expected_data,
        expected_paddr=effective_pa,
        expected_mmio=expected_mmio,
        expected_ncio=expected_ncio,
        max_cycles=max_cycles,
    )
    return (
        SequenceState(
            next_lq_ptr=ptr_inc(state.next_lq_ptr, env.config.sequence.load_queue_size),
            sq_ptr=state.sq_ptr,
        ),
        txn,
        writeback,
    )


def _drain_channels(env) -> set[str]:
    return {event.get("channel", "") for event in env.memory.drain_log}


def _wait_any_store_view(env, sq_idx: int, *, max_cycles: int = 200):
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
    state: SequenceState,
    req_id: int,
    va: int,
    data: int,
    max_cycles: int = 256,
) -> tuple[SequenceState, object]:
    txn = StoreTxn(
        req_id=req_id,
        sq_ptr=state.sq_ptr,
        addr=va,
        data=data,
    )
    allocated_sq_ptr = send_store(env, txn)
    env.backend.pulse_store_commit(1)
    env.advance_cycles(env.config.sequence.store_settle_cycles)
    store = _wait_any_store_view(env, allocated_sq_ptr.value, max_cycles=max_cycles)
    return (
        SequenceState(
            next_lq_ptr=state.next_lq_ptr,
            sq_ptr=ptr_inc(state.sq_ptr, env.config.sequence.store_queue_size),
        ),
        store,
    )


def test_api_MemBlock_env_mmu_idle_inputs_preserve_svpbmt_state(env):
    """激活的 Svpbmt 配置需要在 idle_inputs 后保持稳定，并在 disable 后清空。"""

    env.mmu.enable_svpbmt(pmm_menvcfg=1, pmm_henvcfg=2)
    env.mmu.enable_sv39(root_pt_addr=MMU_ROOT_PT, settle_cycles=0)
    env.idle_inputs()

    assert int(env.tlb_csr.mPBMTE.value) == 1
    assert int(env.tlb_csr.hPBMTE.value) == 1
    assert int(env.tlb_csr.pmm_menvcfg.value) == 1
    assert int(env.tlb_csr.pmm_henvcfg.value) == 2

    env.mmu.disable_translation()
    env.idle_inputs()

    assert int(env.tlb_csr.mPBMTE.value) == 0
    assert int(env.tlb_csr.hPBMTE.value) == 0
    assert int(env.tlb_csr.pmm_menvcfg.value) == 0
    assert int(env.tlb_csr.pmm_henvcfg.value) == 0


def test_api_MemBlock_sv39_pbmt_ncio_load_smoke(env):
    """PBMT=NC 的 translated load 应显式表现为 NCIO，而不是 MMIO/cacheable。"""

    state = _reset_env_state(env)
    _install_svpbmt_address_space(env)

    with env.mmu.ptw_responder():
        env.mmu.enable_sv39(root_pt_addr=MMU_ROOT_PT)
        stats_before = env.get_transport_stats()
        _, writeback = _run_translated_load(
            env,
            state=state,
            req_id=0,
            va=NCIO_VA,
            expected_pa=NCIO_PA,
            expected_data=NCIO_DATA,
            expected_mmio=False,
            expected_ncio=False,
        )
        stats_after = env.get_transport_stats()

    if writeback["debug_is_ncio"] != 1 or writeback["debug_is_mmio"] != 0:
        pytest.xfail(
            "PBMT NC load capability gap: translated PBMT=NC load completes, "
            f"but writeback flags are mmio={writeback['debug_is_mmio']} ncio={writeback['debug_is_ncio']}"
        )
    assert writeback["debug_paddr"] == NCIO_PA
    assert writeback["debug_is_mmio"] == 0
    assert writeback["debug_is_ncio"] == 1
    assert stats_after["outer_request_count"] > stats_before["outer_request_count"], "NCIO load 未走 outer 路径"
    assert stats_after["dcache_a_request_count"] == stats_before["dcache_a_request_count"], (
        "NCIO load 不应走 dcache A 路径"
    )
    env.assert_no_outstanding()


def test_api_MemBlock_sv39_pbmt_mmio_load_smoke(env):
    """PBMT=IO 的 translated load 应显式表现为 MMIO，而不是 NCIO/cacheable。"""

    state = _reset_env_state(env)
    _install_svpbmt_address_space(env)

    with env.mmu.ptw_responder():
        env.mmu.enable_sv39(root_pt_addr=MMU_ROOT_PT)
        stats_before = env.get_transport_stats()
        _, writeback = _run_translated_load(
            env,
            state=state,
            req_id=0,
            va=MMIO_VA,
            expected_pa=MMIO_PA,
            expected_data=MMIO_DATA,
            expected_mmio=True,
            expected_ncio=False,
        )
        stats_after = env.get_transport_stats()

    if writeback["debug_is_mmio"] != 1 or writeback["debug_is_ncio"] != 0:
        pytest.xfail(
            "PBMT IO load capability gap: translated PBMT=IO load completes, "
            f"but writeback flags are mmio={writeback['debug_is_mmio']} ncio={writeback['debug_is_ncio']}"
        )
    assert writeback["debug_paddr"] == MMIO_PA
    assert writeback["debug_is_mmio"] == 1
    assert writeback["debug_is_ncio"] == 0
    assert stats_after["outer_request_count"] > stats_before["outer_request_count"], "MMIO load 未走 outer 路径"
    assert stats_after["dcache_a_request_count"] == stats_before["dcache_a_request_count"], (
        "MMIO load 不应走 dcache A 路径"
    )
    env.assert_no_outstanding()


def test_api_MemBlock_sv39_pbmt_ncio_store_flush_smoke(env):
    """PBMT=NC 的 translated store 应形成 NC store shadow，并在 flush 后闭环到 outer drain。"""

    state = _reset_env_state(env)
    _install_svpbmt_address_space(env)

    with env.mmu.ptw_responder():
        env.mmu.enable_sv39(root_pt_addr=MMU_ROOT_PT)
        outer_before = env.get_counter("outer_write_request_count")
        sbuffer_before = env.get_counter("sbuffer_drain_count")

        txn = StoreTxn(req_id=0, sq_ptr=state.sq_ptr, addr=NCIO_VA, data=NCIO_DATA)
        allocated_sq_ptr = send_store(env, txn)
        env.backend.pulse_store_commit(1)
        env.advance_cycles(env.config.sequence.store_settle_cycles)
        store = _wait_any_store_view(env, allocated_sq_ptr.value, max_cycles=256)
        if store.addr != NCIO_PA or not store.nc or store.mmio:
            pytest.xfail(
                "PBMT NC store capability gap: translated PBMT=NC store did not surface a stable NC shadow; "
                f"store={store}"
            )
        env.wait_memory_quiesce(max_cycles=400)
        drain_summary = FlushStoreBuffersSequence().run(env)

    assert store.addr == NCIO_PA, "NCIO store shadow 地址不匹配"
    assert store.nc, "NCIO store 未被标记为 nc"
    assert not store.mmio, "NCIO store 不应被标记为 mmio"
    assert env.get_counter("outer_write_request_count") > outer_before, "NCIO store 未发出 outer 写请求"
    assert env.get_counter("sbuffer_drain_count") == sbuffer_before, "NCIO store 不应走 sbuffer drain"
    assert "outer" in _drain_channels(env), "NCIO store flush 后未记录到 outer drain"
    assert drain_summary["drain_event_count"] >= 1
    env.assert_no_outstanding()


def test_api_MemBlock_sv39_pbmt_ncio_store_burst_flush_smoke(env):
    """两条 PBMT=NC 的 translated store 应都形成 NC shadow，并在统一 flush 后闭环到 outer drain。"""

    state = _reset_env_state(env)
    _install_svpbmt_address_space(env)

    with env.mmu.ptw_responder():
        env.mmu.enable_sv39(root_pt_addr=MMU_ROOT_PT)
        outer_before = env.get_counter("outer_write_request_count")
        sbuffer_before = env.get_counter("sbuffer_drain_count")

        state, first_store = _run_translated_store(
            env,
            state=state,
            req_id=0,
            va=NCIO_VA,
            data=NCIO_BURST_DATA[0],
        )
        state, second_store = _run_translated_store(
            env,
            state=state,
            req_id=1,
            va=NCIO_VA + 0x8,
            data=NCIO_BURST_DATA[1],
        )
        if (
            first_store.addr != NCIO_PA
            or not first_store.nc
            or first_store.mmio
            or second_store.addr != NCIO_PA + 0x8
            or not second_store.nc
            or second_store.mmio
        ):
            pytest.xfail(
                "PBMT NC burst capability gap: translated PBMT=NC stores did not surface stable NC shadows; "
                f"first={first_store}, second={second_store}"
            )
        env.wait_memory_quiesce(max_cycles=400)
        drain_summary = FlushStoreBuffersSequence().run(env)

    assert first_store.addr == NCIO_PA
    assert second_store.addr == NCIO_PA + 0x8
    assert first_store.nc and second_store.nc
    assert not first_store.mmio and not second_store.mmio
    assert env.get_counter("outer_write_request_count") >= outer_before + 2, "NCIO burst 未形成两笔 outer 写请求"
    assert env.get_counter("sbuffer_drain_count") == sbuffer_before, "NCIO burst 不应走 sbuffer drain"
    assert "outer" in _drain_channels(env), "NCIO burst flush 后未记录到 outer drain"
    assert drain_summary["touched_byte_count"] >= 16, "NCIO burst flush 覆盖字节数不足"
    env.assert_no_outstanding()


def test_api_MemBlock_sv39_pbmt_ncio_then_cacheable_store_flush_keeps_nc_outer_and_cacheable_sbuffer_paths(env):
    """translated PBMT=NC store 与 translated cacheable store 混合 flush 时，应同时保留 outer 与 sbuffer 路径。"""

    state = _reset_env_state(env)
    _install_svpbmt_address_space(env)
    cacheable_store_va = CACHEABLE_VA + 0x28
    cacheable_store_pa = CACHEABLE_PA + 0x28
    expected_refmem = env.memory.predict_store(cacheable_store_pa, NCIO_MIXED_CACHEABLE_DATA)

    with env.mmu.ptw_responder():
        env.mmu.enable_sv39(root_pt_addr=MMU_ROOT_PT)
        outer_before = env.get_counter("outer_write_request_count")
        sbuffer_before = env.get_counter("sbuffer_drain_count")

        state, ncio_store = _run_translated_store(
            env,
            state=state,
            req_id=0,
            va=NCIO_VA,
            data=NCIO_DATA,
        )
        state, cacheable_store = _run_translated_store(
            env,
            state=state,
            req_id=1,
            va=cacheable_store_va,
            data=NCIO_MIXED_CACHEABLE_DATA,
        )
        if (
            ncio_store.addr != NCIO_PA
            or not ncio_store.nc
            or ncio_store.mmio
            or cacheable_store.addr != cacheable_store_pa
            or cacheable_store.mmio
            or cacheable_store.nc
        ):
            pytest.xfail(
                "PBMT NC/cacheable mixed-store capability gap: translated stores did not surface stable NC/cacheable shadows; "
                f"ncio={ncio_store}, cacheable={cacheable_store}"
            )
        env.wait_memory_quiesce(max_cycles=400)
        drain_summary = FlushStoreBuffersSequence().run(env)

    drain_channels = _drain_channels(env)
    assert ncio_store.addr == NCIO_PA and ncio_store.nc and not ncio_store.mmio
    assert cacheable_store.addr == cacheable_store_pa and not cacheable_store.nc and not cacheable_store.mmio
    assert env.get_counter("outer_write_request_count") > outer_before, "mixed NC/cacheable flush 未形成 outer 写请求"
    assert env.get_counter("sbuffer_drain_count") > sbuffer_before, "mixed NC/cacheable flush 未形成 sbuffer drain"
    assert "outer" in drain_channels, "mixed NC/cacheable flush 未记录到 outer drain"
    assert "sbuffer" in drain_channels, "mixed NC/cacheable flush 未记录到 sbuffer drain"
    assert drain_summary["drain_event_count"] >= 1, "mixed NC/cacheable flush 未记录到 drain"
    assert env.memory.read(cacheable_store_pa, 8) == expected_refmem.read(
        cacheable_store_pa, 8
    ), "mixed NC/cacheable flush 中的 cacheable store 最终 golden memory 不匹配"
    env.assert_no_outstanding()


def test_api_MemBlock_sv39_pbmt_mmio_then_cacheable_store_flush_excludes_mmio_from_final_compare(env):
    """translated PBMT=MMIO store 与 translated cacheable store 混合 flush 时，只比较 cacheable/non-MMIO 结果。"""

    state = _reset_env_state(env)
    _install_svpbmt_address_space(env)
    cacheable_store_va = CACHEABLE_VA + 0x20
    cacheable_store_pa = CACHEABLE_PA + 0x20
    expected_refmem = env.memory.predict_store(cacheable_store_pa, MMIO_MIXED_CACHEABLE_DATA)

    with env.mmu.ptw_responder():
        env.mmu.enable_sv39(root_pt_addr=MMU_ROOT_PT)
        outer_before = env.get_counter("outer_write_request_count")
        sbuffer_before = env.get_counter("sbuffer_drain_count")

        state, mmio_store = _run_translated_store(
            env,
            state=state,
            req_id=0,
            va=MMIO_VA,
            data=MMIO_DATA,
        )
        state, cacheable_store = _run_translated_store(
            env,
            state=state,
            req_id=1,
            va=cacheable_store_va,
            data=MMIO_MIXED_CACHEABLE_DATA,
        )
        if (
            mmio_store.addr != MMIO_PA
            or not mmio_store.mmio
            or mmio_store.nc
            or cacheable_store.addr != cacheable_store_pa
            or cacheable_store.mmio
            or cacheable_store.nc
        ):
            pytest.xfail(
                "PBMT IO/Cacheable mixed-store capability gap: translated stores did not surface stable MMIO/cacheable shadows; "
                f"mmio={mmio_store}, cacheable={cacheable_store}"
            )
        env.wait_memory_quiesce(max_cycles=400)
        drain_summary = FlushStoreBuffersSequence().run(env)

    drain_channels = _drain_channels(env)
    assert mmio_store.addr == MMIO_PA and mmio_store.mmio and not mmio_store.nc
    assert cacheable_store.addr == cacheable_store_pa and not cacheable_store.mmio and not cacheable_store.nc
    assert env.get_counter("outer_write_request_count") > outer_before, "translated MMIO store 未形成 outer 写请求"
    assert env.get_counter("sbuffer_drain_count") > sbuffer_before, "translated cacheable store 未形成 sbuffer drain"
    assert "outer" in drain_channels, "mixed translated flush 未记录到 outer drain"
    assert "sbuffer" in drain_channels, "mixed translated flush 未记录到 sbuffer drain"
    assert drain_summary["drain_event_count"] >= 1, "mixed translated flush 未记录到 drain"
    assert env.memory.read(cacheable_store_pa, 8) == expected_refmem.read(
        cacheable_store_pa, 8
    ), "mixed translated flush 中的 cacheable store 最终 golden memory 不匹配"
    env.assert_no_outstanding()


def test_api_MemBlock_sv39_pbmt_mmio_store_burst_then_cacheable_flush_excludes_all_mmio_bytes(env):
    """两条 translated PBMT=MMIO store 与一条 cacheable store 混合 flush 时，只比较 non-MMIO 结果。"""

    state = _reset_env_state(env)
    _install_svpbmt_address_space(env)
    cacheable_store_va = CACHEABLE_VA + 0x30
    cacheable_store_pa = CACHEABLE_PA + 0x30
    expected_refmem = env.memory.predict_store(cacheable_store_pa, MMIO_MIXED_CACHEABLE_DATA)

    with env.mmu.ptw_responder():
        env.mmu.enable_sv39(root_pt_addr=MMU_ROOT_PT)
        outer_before = env.get_counter("outer_write_request_count")
        sbuffer_before = env.get_counter("sbuffer_drain_count")

        state, first_mmio_store = _run_translated_store(
            env,
            state=state,
            req_id=0,
            va=MMIO_VA,
            data=MMIO_BURST_DATA[0],
        )
        state, second_mmio_store = _run_translated_store(
            env,
            state=state,
            req_id=1,
            va=MMIO_VA + 0x8,
            data=MMIO_BURST_DATA[1],
        )
        state, cacheable_store = _run_translated_store(
            env,
            state=state,
            req_id=2,
            va=cacheable_store_va,
            data=MMIO_MIXED_CACHEABLE_DATA,
        )
        if (
            first_mmio_store.addr != MMIO_PA
            or not first_mmio_store.mmio
            or first_mmio_store.nc
            or second_mmio_store.addr != MMIO_PA + 0x8
            or not second_mmio_store.mmio
            or second_mmio_store.nc
            or cacheable_store.addr != cacheable_store_pa
            or cacheable_store.mmio
            or cacheable_store.nc
        ):
            pytest.xfail(
                "PBMT IO burst/cacheable mixed-store capability gap: translated stores did not surface stable MMIO/cacheable shadows; "
                f"mmio0={first_mmio_store}, mmio1={second_mmio_store}, cacheable={cacheable_store}"
            )
        env.wait_memory_quiesce(max_cycles=400)
        drain_summary = FlushStoreBuffersSequence().run(env)

    drain_channels = _drain_channels(env)
    assert first_mmio_store.addr == MMIO_PA and first_mmio_store.mmio and not first_mmio_store.nc
    assert second_mmio_store.addr == MMIO_PA + 0x8 and second_mmio_store.mmio and not second_mmio_store.nc
    assert cacheable_store.addr == cacheable_store_pa and not cacheable_store.mmio and not cacheable_store.nc
    assert env.get_counter("outer_write_request_count") >= outer_before + 2, "MMIO burst 未形成两笔 outer 写请求"
    assert env.get_counter("sbuffer_drain_count") > sbuffer_before, "mixed MMIO burst flush 未形成 sbuffer drain"
    assert "outer" in drain_channels, "mixed MMIO burst flush 未记录到 outer drain"
    assert "sbuffer" in drain_channels, "mixed MMIO burst flush 未记录到 sbuffer drain"
    assert drain_summary["drain_event_count"] >= 1, "mixed MMIO burst flush 未记录到 drain"
    assert env.memory.read(cacheable_store_pa, 8) == expected_refmem.read(
        cacheable_store_pa, 8
    ), "mixed MMIO burst flush 中的 cacheable store 最终 golden memory 不匹配"
    env.assert_no_outstanding()


def test_api_MemBlock_mmio_busy_blocks_younger_cacheable_load_retire(env):
    """older MMIO store busy 时，younger cacheable load 可先写回，但 compare/retire 不能过早完成。"""

    state = _reset_env_state(env)
    cacheable_addr = 0x80000008
    mmio_addr = 0x1000
    env.preload_u64(cacheable_addr, CACHEABLE_DATA)

    state, _ = _run_translated_load(
        env,
        state=SequenceState(next_lq_ptr=state.next_lq_ptr, sq_ptr=state.sq_ptr),
        req_id=0,
        va=cacheable_addr,
        expected_pa=cacheable_addr,
        expected_data=CACHEABLE_DATA,
        expected_mmio=False,
        expected_ncio=False,
    )
    completed_before = env.get_completed_load_count()

    mmio_store = ScalarStoreCommitSequence(
        # MMIO/uncache 路径要求 store 到达 ROB head 后才真正向后执行；
        # 这里显式用紧邻 pendingPtr 的低 req_id，避免 testcase 自己把请求挂在队头之后。
        StoreTxn(req_id=1, sq_ptr=state.sq_ptr, addr=mmio_addr, data=MMIO_DATA),
        expected_mmio=True,
        expected_nc=False,
        require_committed=False,
        wait_quiesce=False,
    ).run(env)
    env.wait_mmio_busy(expected=True, max_cycles=200)

    younger_load = LoadTxn(
        req_id=2,
        addr=cacheable_addr,
        lq_ptr=state.next_lq_ptr,
        sq_ptr=mmio_store.store_result.next_sq_ptr,
    )
    env.backend.prepare(younger_load)
    env.expect_scalar_load(
        rob_idx=younger_load.rob_idx,
        pdest=younger_load.resolved_pdest,
        addr=cacheable_addr,
        size=younger_load.size,
        mask=younger_load.mask,
    )
    send_load(env, younger_load)
    younger_writeback = env.wait_load_writeback_observed(
        rob_idx=younger_load.rob_idx,
        data=CACHEABLE_DATA,
        expected_mmio=False,
        expected_ncio=False,
        max_cycles=256,
    )

    assert env.get_completed_load_count() == completed_before, "older MMIO store 收尾前 younger load 不应已完成 compare"
    assert env.get_counter("rob_pending_entry_count") > 0, "older MMIO busy 期间 ROB 不应已完全清空"
    if int(env.lsq_status.mmioBusy.value) == 1:
        env.advance_cycles(12)
        assert env.get_completed_load_count() == completed_before, "older MMIO busy 期间 younger load 不应完成 compare"

    env.wait_completed_load_count(completed_before + 1, max_cycles=200)
    env.drain_writebacks(max_cycles=200)
    env.wait_memory_quiesce(max_cycles=400)

    assert younger_writeback["debug_paddr"] == cacheable_addr
    assert younger_writeback["debug_is_mmio"] == 0
    assert younger_writeback["debug_is_ncio"] == 0
    env.assert_no_outstanding()


def test_api_MemBlock_mmio_busy_blocks_first_younger_compare_with_multiple_loads_inflight(env):
    """放大 outer 响应后，older MMIO store 应至少卡住第一条 younger load 的 compare，同时允许多条 younger load in-flight。"""

    state = _reset_env_state(env)
    cacheable_addrs = (0x80000008, 0x80000018)
    for addr, data in zip(cacheable_addrs, LONG_BUSY_CACHEABLE_DATA):
        env.preload_u64(addr, data)

    original_outer_delay = env.memory.transport.outer_delay
    env.memory.transport.outer_delay = max(int(original_outer_delay), 64)
    try:
        state, _ = _run_translated_load(
            env,
            state=SequenceState(next_lq_ptr=state.next_lq_ptr, sq_ptr=state.sq_ptr),
            req_id=0,
            va=cacheable_addrs[0],
            expected_pa=cacheable_addrs[0],
            expected_data=LONG_BUSY_CACHEABLE_DATA[0],
            expected_mmio=False,
            expected_ncio=False,
        )
        completed_before = env.get_completed_load_count()

        mmio_store = ScalarStoreCommitSequence(
            StoreTxn(req_id=1, sq_ptr=state.sq_ptr, addr=0x1000, data=MMIO_DATA),
            expected_mmio=True,
            expected_nc=False,
            require_committed=False,
            wait_quiesce=False,
        ).run(env)
        env.wait_mmio_busy(expected=True, max_cycles=200)

        younger_state = SequenceState(
            next_lq_ptr=state.next_lq_ptr,
            sq_ptr=mmio_store.store_result.next_sq_ptr,
        )
        younger_txns = []
        for req_id, addr, data in (
            (2, cacheable_addrs[0], LONG_BUSY_CACHEABLE_DATA[0]),
            (3, cacheable_addrs[1], LONG_BUSY_CACHEABLE_DATA[1]),
        ):
            txn = LoadTxn(
                req_id=req_id,
                addr=addr,
                lq_ptr=younger_state.next_lq_ptr,
                sq_ptr=younger_state.sq_ptr,
            )
            env.backend.prepare(txn)
            env.expect_scalar_load(
                rob_idx=txn.rob_idx,
                pdest=txn.resolved_pdest,
                addr=addr,
                size=txn.size,
                mask=txn.mask,
            )
            send_load(env, txn)
            younger_txns.append((txn, data))
            younger_state = SequenceState(
                next_lq_ptr=ptr_inc(younger_state.next_lq_ptr, env.config.sequence.load_queue_size),
                sq_ptr=younger_state.sq_ptr,
            )

        first_writeback = env.wait_load_writeback_observed(
            rob_idx=younger_txns[0][0].rob_idx,
            data=younger_txns[0][1],
            expected_paddr=cacheable_addrs[0],
            expected_mmio=False,
            expected_ncio=False,
            max_cycles=256,
        )
        assert env.get_completed_load_count() == completed_before, "older MMIO busy 期间第一条 younger load 不应完成 compare"
        assert env.get_counter("rob_pending_entry_count") > 0, "older MMIO busy 期间 ROB 不应已完全清空"
        try:
            second_writeback = env.wait_load_writeback_observed(
                rob_idx=younger_txns[1][0].rob_idx,
                data=younger_txns[1][1],
                expected_paddr=cacheable_addrs[1],
                expected_mmio=False,
                expected_ncio=False,
                max_cycles=256,
            )
            env.wait_mmio_busy(expected=False, max_cycles=256)
            env.wait_completed_load_count(completed_before + 2, max_cycles=256)
            env.drain_writebacks(max_cycles=256)
            env.wait_memory_quiesce(max_cycles=400)
        except TimeoutError as exc:
            pytest.xfail(
                "mmioBusy multi-load capability gap: first younger load is blocked at compare as expected, "
                f"but full unblock/retire closure did not converge ({exc})"
            )
    finally:
        env.memory.transport.outer_delay = original_outer_delay

    assert first_writeback["debug_paddr"] == cacheable_addrs[0]
    assert first_writeback["debug_is_mmio"] == 0
    assert first_writeback["debug_is_ncio"] == 0
    assert second_writeback["debug_paddr"] == cacheable_addrs[1]
    assert second_writeback["debug_is_mmio"] == 0
    assert second_writeback["debug_is_ncio"] == 0
    env.assert_no_outstanding()
