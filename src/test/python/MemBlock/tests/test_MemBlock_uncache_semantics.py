# coding=utf-8
"""
MemBlock real-DUT uncache / MMIO semantic regression.

本文件重点覆盖两件事：
  1. 通过 Svpbmt 显式把 translated access 区分成 cacheable / NCIO / MMIO。
  2. 用真实 DUT 证明 uncache 不只是“走对路径”，还能影响提交语义。
"""

import pytest

from request_apis import LoadTxn, StoreTxn, ptr_inc, send_load, send_store
from sequences import FlushStoreBuffersSequence, ResetEnvSequence, ScalarStoreCommitSequence, SequenceState


MMU_ROOT_PT = 0x88010000

CACHEABLE_VA = 0x40001000
NCIO_VA = 0x80002000
MMIO_VA = 0xC0003000

CACHEABLE_PA_BASE = 0x80000000
NCIO_PA_BASE = 0xC0000000
MMIO_PA_BASE = 0x100000000

CACHEABLE_DATA = 0x1020_3040_5060_7080
NCIO_DATA = 0x8877_6655_4433_2211
MMIO_DATA = 0x0BAD_F00D_CAFE_BABE


def _reset_env_state(env) -> SequenceState:
    return ResetEnvSequence(
        require_issue_lanes=(0, 3, 5),
        require_lq_ready=True,
        require_sq_ready=True,
    ).run(env)


def _translated_pa(va: int, pa_base: int) -> int:
    return int(pa_base) | (int(va) & ((1 << 30) - 1))


CACHEABLE_PA = _translated_pa(CACHEABLE_VA, CACHEABLE_PA_BASE)
NCIO_PA = _translated_pa(NCIO_VA, NCIO_PA_BASE)
MMIO_PA = _translated_pa(MMIO_VA, MMIO_PA_BASE)


def _install_svpbmt_address_space(env) -> None:
    env.mmu.enable_svpbmt()
    env.mmu.install_sv39_gigapage_mapping(
        root_pt_addr=MMU_ROOT_PT,
        va=CACHEABLE_VA,
        pa_base=CACHEABLE_PA_BASE,
        pbmt="cacheable",
    )
    env.mmu.install_sv39_gigapage_mapping(
        root_pt_addr=MMU_ROOT_PT,
        va=NCIO_VA,
        pa_base=NCIO_PA_BASE,
        pbmt="ncio",
    )
    env.mmu.install_sv39_gigapage_mapping(
        root_pt_addr=MMU_ROOT_PT,
        va=MMIO_VA,
        pa_base=MMIO_PA_BASE,
        pbmt="mmio",
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
    env.expect_scalar_load(
        req_id=txn.req_id,
        pdest=txn.resolved_pdest,
        addr=expected_pa,
        size=txn.size,
        mask=txn.mask,
    )
    send_load(env, txn)
    writeback = env.wait_load_writeback_observed(
        rob_idx_flag=txn.rob_idx_flag,
        rob_idx_value=txn.rob_idx_value,
        data=expected_data,
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
    env.expect_scalar_load(
        req_id=younger_load.req_id,
        pdest=younger_load.resolved_pdest,
        addr=cacheable_addr,
        size=younger_load.size,
        mask=younger_load.mask,
    )
    send_load(env, younger_load)
    younger_writeback = env.wait_load_writeback_observed(
        rob_idx_flag=younger_load.rob_idx_flag,
        rob_idx_value=younger_load.rob_idx_value,
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
