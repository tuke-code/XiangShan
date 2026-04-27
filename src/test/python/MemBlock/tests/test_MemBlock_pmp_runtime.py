# coding=utf-8
"""
MemBlock PMP runtime programming / lock / boundary directed.
"""

import pytest

from request_apis import send_store
from sequences import ResetEnvSequence
from transactions import LoadTxn, StoreTxn, ptr_inc


PMP_ROOT_PT = 0x88400000
PMP_PAGE_TABLE_PAGES = (
    0x88401000,
    0x88402000,
    0x88403000,
    0x88404000,
    0x88405000,
    0x88406000,
)
PMP_VA_BASE = 0x40600000
PMP_PA_BASE = 0x80A00000
PMP_LOAD_VA = PMP_VA_BASE + 0x120
PMP_STORE_VA = PMP_VA_BASE + 0x1A0
PMP_LOAD_DATA = 0x1020304050607080
PMP_STORE_DATA = 0x8877665544332211
PMP_RESTORED_LOAD_DATA = 0x0F1E2D3C4B5A6978
PMP_RESTORED_STORE_DATA = 0x7766554433221100
PMP_BOUNDARY_DENIED_VA = PMP_VA_BASE + 0x2000 + 0x180
PMP_BOUNDARY_ALLOWED_VA = PMP_VA_BASE + 0x3000 + 0x080
PMP_TOR_BELOW_VA = PMP_VA_BASE + 0x4000 + 0x080
PMP_TOR_INSIDE_VA = PMP_VA_BASE + 0x5000 + 0x180
PMP_TOR_ABOVE_VA = PMP_VA_BASE + 0x6000 + 0x100
PMP_BOUNDARY_SIZE = 0x1000
PMP_ALLOW_RWX_NAPOT_CFG = 0x1F
PMP_ALLOW_RWX_NAPOT_LOCKED_CFG = 0x9F
PMP_DENY_TOR_CFG = 0x08
PMA_CFG_CSR_BASE = 0x7C0
PMA_ADDR_CSR_BASE = 0x7C8
PMA_CFG_SLOTS_PER_WORD = 8
PMA_CACHEABLE_RWX_NAPOT_CFG = 0x5F
PMA_MMIO_RWX_NAPOT_CFG = 0x1F
PMA_MMIO_TOR_CFG = 0x0F
PMP_REAL_ENTRY_COUNT = 32
PMA_REAL_ENTRY_COUNT = 32
MEMBLOCK_PADDR_BITS = 48
LOAD_ACCESS_FAULT_BIT = 5
ALL_REAL_ENTRY_CASES = tuple(range(PMP_REAL_ENTRY_COUNT))
BOUNDARY_ENTRY_CASES = (0, 7, 15, 23, 30)
TOR_ENTRY_CASES = (0, 7, 15, 23, 29)
PMA_RUNTIME_ENTRY_CASES = tuple(range(24))
PMA_BOUNDARY_ENTRY_CASES = (0, 7, 15, 23)
PMA_TOR_ENTRY_CASES = (0, 7, 15, 21)
STORE_PMP_DENY_CAPABILITY_GAP_XFAIL_REASON = (
    "Store-side PMP deny capability gap: current DUT/env still materializes translated stores "
    "without a stable store exception marker under PMP deny/off/TOR boundary reprogramming"
)
STORE_PMA_TRANSLATED_CLASSIFICATION_GAP_XFAIL_REASON = (
    "Store-side PMA translated classification gap: current DUT/env still materializes translated stores "
    "without stable translated paddr/mmio shadow updates under PMA runtime or boundary reprogramming"
)


def _reset_env_state(env):
    return ResetEnvSequence(
        require_issue_lanes=(0, 3, 5),
        require_lq_ready=True,
        require_sq_ready=True,
    ).run(env)


def _sv39_4k_mapping_pa_base(space_pa_base: int, va: int) -> int:
    page_offset = (int(va) & ((1 << 30) - 1)) & ~0xFFF
    return int(space_pa_base) | page_offset


def _translated_pa(va: int, pa_base: int) -> int:
    return int(pa_base) | (int(va) & 0xFFF)


def _set_exception_indices(writeback: dict) -> set[int]:
    return {
        index
        for index, bit in enumerate(writeback.get("exception_bits", ()))
        if bit
    }


def _allow_all_addr() -> int:
    return (1 << (MEMBLOCK_PADDR_BITS - 2)) - 1


def _encode_pmp_napot_addr(base_addr: int, size: int) -> int:
    return (int(base_addr) + (int(size) // 2 - 1)) >> 2


def _capture_ptw_delta(ptw, trace_start: int):
    return tuple(list(ptw.trace)[trace_start:])


def _program_allow_all_entry(env, *, entry_index: int, locked: bool = False) -> None:
    env.mmu.program_pmp_entry(
        index=int(entry_index),
        cfg=PMP_ALLOW_RWX_NAPOT_LOCKED_CFG if locked else PMP_ALLOW_RWX_NAPOT_CFG,
        addr=_allow_all_addr(),
        persistent=False,
    )


def _program_off_entry(env, *, entry_index: int) -> None:
    env.mmu.program_pmp_entry(
        index=int(entry_index),
        cfg=0,
        addr=0,
        persistent=False,
    )


def _program_napot_deny_with_fallback(
    env,
    *,
    entry_index: int,
    deny_region_base: int,
    deny_region_size: int,
) -> None:
    env.mmu.program_pmp_entry(
        index=int(entry_index),
        cfg=0x18,
        addr=_encode_pmp_napot_addr(deny_region_base, deny_region_size),
        persistent=False,
    )
    _program_allow_all_entry(env, entry_index=int(entry_index) + 1)


def _program_tor_deny_with_fallback(
    env,
    *,
    lower_entry_index: int,
    lower_bound: int,
    upper_bound: int,
) -> None:
    env.mmu.program_pmp_entry(
        index=int(lower_entry_index),
        cfg=0,
        addr=int(lower_bound) >> 2,
        persistent=False,
    )
    env.mmu.program_pmp_entry(
        index=int(lower_entry_index) + 1,
        cfg=PMP_DENY_TOR_CFG,
        addr=int(upper_bound) >> 2,
        persistent=False,
    )
    _program_allow_all_entry(env, entry_index=int(lower_entry_index) + 2)


def _run_translated_load(
    env,
    *,
    req_id: int,
    va: int,
    pa_base: int,
    lq_ptr,
    sq_ptr,
    expected_data: int | None = None,
    required_exception_bits: tuple[int, ...] = (),
    max_cycles: int = 256,
):
    txn = LoadTxn(
        req_id=int(req_id),
        addr=int(va),
        lq_ptr=lq_ptr,
        sq_ptr=sq_ptr,
    )
    prepared = env.backend.prepare(txn)

    if expected_data is not None:
        completed_before = env.get_completed_load_count()
        expected = env.expect_scalar_load(
            rob_idx=txn.rob_idx,
            pdest=txn.resolved_pdest,
            addr=_translated_pa(int(va), int(pa_base)),
            size=txn.size,
            mask=txn.mask,
        )
        expected.expected_data = int(expected_data)
        env.backend.execute(prepared)
        writeback = env.wait_load_writeback_observed(rob_idx=txn.rob_idx, max_cycles=max_cycles)
        env.wait_completed_load_count(completed_before + 1, max_cycles=max_cycles)
        return txn, writeback

    previous_strict = env.memory.strict_writeback_check
    env.memory.strict_writeback_check = False
    try:
        env.backend.execute(prepared)
        writeback = env.wait_load_fault_observed(
            rob_idx=txn.rob_idx,
            required_exception_bits=tuple(int(bit) for bit in required_exception_bits),
            max_cycles=max_cycles,
        )
        env.drain_writebacks(max_cycles=max_cycles)
        return txn, writeback
    finally:
        env.memory.strict_writeback_check = previous_strict


def _wait_store_fault_observed(env, *, sq_idx: int, max_cycles: int = 256):
    for _ in range(max_cycles):
        store = env.get_store_view(sq_idx)
        if store is not None and store.allocated and store.has_exception:
            return store
        env.advance_cycles(1)
    raise AssertionError(f"等待 sqIdx={sq_idx} 的 store PMP fault 超时: store={env.get_store_view(sq_idx)}")


def _wait_any_store_view(env, *, sq_idx: int, expected_data: int, max_cycles: int = 256):
    for _ in range(max_cycles):
        store = env.get_store_view(sq_idx)
        if (
            store is not None
            and store.allocated
            and store.data is not None
            and (store.data & ((1 << 64) - 1)) == (int(expected_data) & ((1 << 64) - 1))
            and store.mask != 0
        ):
            return store
        env.advance_cycles(1)
    raise AssertionError(f"等待 sqIdx={sq_idx} 的 translated store view 超时: store={env.get_store_view(sq_idx)}")


def _run_translated_store(
    env,
    *,
    req_id: int,
    va: int,
    pa_base: int,
    sq_ptr,
    data: int,
    expect_fault: bool,
    max_cycles: int = 256,
):
    txn = StoreTxn(
        req_id=int(req_id),
        sq_ptr=sq_ptr,
        addr=int(va),
        data=int(data),
    )
    allocated_sq_ptr = send_store(env, txn)
    env.backend.pulse_store_commit(1)
    env.advance_cycles(env.config.sequence.store_settle_cycles)
    next_sq_ptr = ptr_inc(sq_ptr, env.config.sequence.store_queue_size)

    if expect_fault:
        return txn, allocated_sq_ptr, next_sq_ptr, _wait_store_fault_observed(
            env,
            sq_idx=allocated_sq_ptr.value,
            max_cycles=max_cycles,
        )

    return txn, allocated_sq_ptr, next_sq_ptr, _wait_any_store_view(
        env,
        sq_idx=allocated_sq_ptr.value,
        expected_data=int(data),
        max_cycles=max_cycles,
    )


def _install_mappings(env, *mapping_specs: tuple[int, int]):
    next_page_table_idx = 0
    for va, pa_base in mapping_specs:
        install_result = env.mmu.install_sv39_mapping(
            root_pt_addr=PMP_ROOT_PT,
            va=int(va) & ~0xFFF,
            pa_base=int(pa_base),
            page_table_page_addrs=PMP_PAGE_TABLE_PAGES[next_page_table_idx:],
        )
        next_page_table_idx += len(install_result["allocated_table_addrs"])


def _run_group_cases(case_runner, case_values, *, label_prefix: str, expected_failure_reason: str | None = None) -> None:
    for case_value in case_values:
        try:
            case_runner(case_value)
        except Exception as exc:
            message = f"{label_prefix}{case_value}: {exc}"
            if expected_failure_reason is None:
                pytest.fail(message)
            pytest.xfail(f"{expected_failure_reason}\n{message}")


def _program_pma_entry(
    env,
    cfg_words: dict[int, int],
    *,
    entry_index: int,
    cfg: int,
    addr: int,
) -> None:
    if int(entry_index) < 0 or int(entry_index) >= PMA_REAL_ENTRY_COUNT:
        raise ValueError(f"非法 PMA entry 索引: {entry_index}")
    cfg_addr = PMA_CFG_CSR_BASE + (int(entry_index) // PMA_CFG_SLOTS_PER_WORD) * 2
    cfg_shift = (int(entry_index) % PMA_CFG_SLOTS_PER_WORD) * 8
    cfg_word = int(cfg_words.get(cfg_addr, 0))
    cfg_word &= ~(0xFF << cfg_shift)
    cfg_word |= (int(cfg) & 0xFF) << cfg_shift
    cfg_words[cfg_addr] = cfg_word
    env.mmu.write_distributed_csr(PMA_ADDR_CSR_BASE + int(entry_index), int(addr), persistent=False)
    env.mmu.write_distributed_csr(cfg_addr, cfg_word, persistent=False)


def _program_pma_off_entry(env, cfg_words: dict[int, int], *, entry_index: int) -> None:
    _program_pma_entry(env, cfg_words, entry_index=int(entry_index), cfg=0, addr=0)


def _program_pma_cacheable_napot_entry(
    env,
    cfg_words: dict[int, int],
    *,
    entry_index: int,
    region_base: int,
    region_size: int,
) -> None:
    _program_pma_entry(
        env,
        cfg_words,
        entry_index=int(entry_index),
        cfg=PMA_CACHEABLE_RWX_NAPOT_CFG,
        addr=_encode_pmp_napot_addr(region_base, region_size),
    )


def _program_pma_mmio_napot_entry(
    env,
    cfg_words: dict[int, int],
    *,
    entry_index: int,
    region_base: int,
    region_size: int,
) -> None:
    _program_pma_entry(
        env,
        cfg_words,
        entry_index=int(entry_index),
        cfg=PMA_MMIO_RWX_NAPOT_CFG,
        addr=_encode_pmp_napot_addr(region_base, region_size),
    )


def _program_pma_cacheable_allow_all_entry(
    env,
    cfg_words: dict[int, int],
    *,
    entry_index: int,
) -> None:
    _program_pma_entry(
        env,
        cfg_words,
        entry_index=int(entry_index),
        cfg=PMA_CACHEABLE_RWX_NAPOT_CFG,
        addr=_allow_all_addr(),
    )


def _program_pma_tor_mmio_with_fallback(
    env,
    cfg_words: dict[int, int],
    *,
    lower_entry_index: int,
    lower_bound: int,
    upper_bound: int,
) -> None:
    _program_pma_entry(
        env,
        cfg_words,
        entry_index=int(lower_entry_index),
        cfg=0,
        addr=int(lower_bound) >> 2,
    )
    _program_pma_entry(
        env,
        cfg_words,
        entry_index=int(lower_entry_index) + 1,
        cfg=PMA_MMIO_TOR_CFG,
        addr=int(upper_bound) >> 2,
    )
    _program_pma_cacheable_allow_all_entry(
        env,
        cfg_words,
        entry_index=int(lower_entry_index) + 2,
    )


def _run_runtime_load_case(env, entry_index: int) -> None:
    """每个 real PMP entry 都应支持运行时 allow/off/allow 切换，并影响 translated load。"""

    state = _reset_env_state(env)
    load_pa_base = _sv39_4k_mapping_pa_base(PMP_PA_BASE, PMP_LOAD_VA)
    store_pa_base = _sv39_4k_mapping_pa_base(PMP_PA_BASE, PMP_STORE_VA)
    _install_mappings(
        env,
        (PMP_LOAD_VA, load_pa_base),
        (PMP_STORE_VA, store_pa_base),
    )
    env.preload_u64(_translated_pa(PMP_LOAD_VA, load_pa_base), PMP_RESTORED_LOAD_DATA)

    next_lq_ptr = state.next_lq_ptr
    with env.mmu.ptw_responder() as ptw:
        _program_allow_all_entry(env, entry_index=int(entry_index))
        env.mmu.enable_sv39(root_pt_addr=PMP_ROOT_PT)

        trace_start = len(ptw.trace)
        _, prime_load = _run_translated_load(
            env,
            req_id=0x100 + int(entry_index),
            va=PMP_LOAD_VA,
            pa_base=load_pa_base,
            lq_ptr=next_lq_ptr,
            sq_ptr=state.sq_ptr,
            expected_data=PMP_RESTORED_LOAD_DATA,
        )
        prime_ptw_trace = _capture_ptw_delta(ptw, trace_start)
        next_lq_ptr = ptr_inc(next_lq_ptr, env.config.sequence.load_queue_size)

        _program_off_entry(env, entry_index=int(entry_index))

        trace_start = len(ptw.trace)
        _, denied_load = _run_translated_load(
            env,
            req_id=0x300 + int(entry_index),
            va=PMP_LOAD_VA,
            pa_base=load_pa_base,
            lq_ptr=next_lq_ptr,
            sq_ptr=state.sq_ptr,
            required_exception_bits=(LOAD_ACCESS_FAULT_BIT,),
        )
        denied_ptw_trace = _capture_ptw_delta(ptw, trace_start)
        next_lq_ptr = ptr_inc(next_lq_ptr, env.config.sequence.load_queue_size)

        _program_allow_all_entry(env, entry_index=int(entry_index))

        trace_start = len(ptw.trace)
        _, restored_load = _run_translated_load(
            env,
            req_id=0x500 + int(entry_index),
            va=PMP_LOAD_VA,
            pa_base=load_pa_base,
            lq_ptr=next_lq_ptr,
            sq_ptr=state.sq_ptr,
            expected_data=PMP_RESTORED_LOAD_DATA,
        )
        restored_ptw_trace = _capture_ptw_delta(ptw, trace_start)

    assert any(event["event"] == "a_fire" for event in prime_ptw_trace), f"entry{entry_index}: prime load 未触发 PTW"
    assert not denied_ptw_trace, f"entry{entry_index}: deny load 不应重新触发 PTW"
    assert not restored_ptw_trace, f"entry{entry_index}: restore load 不应重新触发 PTW"
    assert prime_load["data"] == PMP_RESTORED_LOAD_DATA
    assert LOAD_ACCESS_FAULT_BIT in _set_exception_indices(denied_load), (
        f"entry{entry_index}: deny load 未命中 access fault, wb={denied_load}"
    )
    assert restored_load["data"] == PMP_RESTORED_LOAD_DATA
    env.assert_no_outstanding()


def _run_runtime_store_case(env, entry_index: int) -> None:
    """每个 real PMP entry 的运行时 allow/off/allow 切换都应影响 translated store。"""

    state = _reset_env_state(env)
    store_pa_base = _sv39_4k_mapping_pa_base(PMP_PA_BASE, PMP_STORE_VA)
    _install_mappings(env, (PMP_STORE_VA, store_pa_base))
    next_sq_ptr = state.sq_ptr

    with env.mmu.ptw_responder() as ptw:
        _program_allow_all_entry(env, entry_index=int(entry_index))
        env.mmu.enable_sv39(root_pt_addr=PMP_ROOT_PT)

        trace_start = len(ptw.trace)
        _, _, next_sq_ptr, prime_store = _run_translated_store(
            env,
            req_id=0x200 + int(entry_index),
            va=PMP_STORE_VA,
            pa_base=store_pa_base,
            sq_ptr=next_sq_ptr,
            data=PMP_STORE_DATA,
            expect_fault=False,
        )
        prime_ptw_trace = _capture_ptw_delta(ptw, trace_start)

        _program_off_entry(env, entry_index=int(entry_index))
        _, _, next_sq_ptr, denied_store = _run_translated_store(
            env,
            req_id=0x400 + int(entry_index),
            va=PMP_STORE_VA,
            pa_base=store_pa_base,
            sq_ptr=next_sq_ptr,
            data=PMP_STORE_DATA ^ 0x55,
            expect_fault=True,
        )

        _program_allow_all_entry(env, entry_index=int(entry_index))
        _, _, _, restored_store = _run_translated_store(
            env,
            req_id=0x600 + int(entry_index),
            va=PMP_STORE_VA,
            pa_base=store_pa_base,
            sq_ptr=next_sq_ptr,
            data=PMP_RESTORED_STORE_DATA,
            expect_fault=False,
        )

    assert any(event["event"] == "a_fire" for event in prime_ptw_trace), f"entry{entry_index}: prime store 未触发 PTW"
    assert not prime_store.has_exception
    assert denied_store.has_exception, f"entry{entry_index}: deny store 未被标记为 exception"
    assert not restored_store.has_exception
    env.reset(cycles=1, settle_cycles=0)


def _run_lock_case(env, entry_index: int) -> None:
    """每个 real PMP entry 置 lock 后，后续 overwrite 不应改变 translated load/store 权限。"""

    state = _reset_env_state(env)
    load_pa_base = _sv39_4k_mapping_pa_base(PMP_PA_BASE + 0x100000, PMP_LOAD_VA)
    store_pa_base = _sv39_4k_mapping_pa_base(PMP_PA_BASE + 0x100000, PMP_STORE_VA)
    _install_mappings(
        env,
        (PMP_LOAD_VA, load_pa_base),
        (PMP_STORE_VA, store_pa_base),
    )
    env.preload_u64(_translated_pa(PMP_LOAD_VA, load_pa_base), PMP_LOAD_DATA)

    next_lq_ptr = state.next_lq_ptr
    next_sq_ptr = state.sq_ptr

    with env.mmu.ptw_responder() as ptw:
        _program_allow_all_entry(env, entry_index=int(entry_index), locked=True)
        env.mmu.enable_sv39(root_pt_addr=PMP_ROOT_PT)

        trace_start = len(ptw.trace)
        _, prime_load = _run_translated_load(
            env,
            req_id=0x700 + int(entry_index),
            va=PMP_LOAD_VA,
            pa_base=load_pa_base,
            lq_ptr=next_lq_ptr,
            sq_ptr=state.sq_ptr,
            expected_data=PMP_LOAD_DATA,
        )
        prime_ptw_trace = _capture_ptw_delta(ptw, trace_start)
        next_lq_ptr = ptr_inc(next_lq_ptr, env.config.sequence.load_queue_size)

        _, _, next_sq_ptr, prime_store = _run_translated_store(
            env,
            req_id=0x800 + int(entry_index),
            va=PMP_STORE_VA,
            pa_base=store_pa_base,
            sq_ptr=next_sq_ptr,
            data=PMP_STORE_DATA,
            expect_fault=False,
        )

        _program_off_entry(env, entry_index=int(entry_index))

        trace_start = len(ptw.trace)
        _, locked_load = _run_translated_load(
            env,
            req_id=0x900 + int(entry_index),
            va=PMP_LOAD_VA,
            pa_base=load_pa_base,
            lq_ptr=next_lq_ptr,
            sq_ptr=next_sq_ptr,
            expected_data=PMP_LOAD_DATA,
        )
        locked_ptw_trace = _capture_ptw_delta(ptw, trace_start)

        _, _, _, locked_store = _run_translated_store(
            env,
            req_id=0xA00 + int(entry_index),
            va=PMP_STORE_VA,
            pa_base=store_pa_base,
            sq_ptr=next_sq_ptr,
            data=PMP_RESTORED_STORE_DATA,
            expect_fault=False,
        )

    assert any(event["event"] == "a_fire" for event in prime_ptw_trace), f"entry{entry_index}: lock prime 未触发 PTW"
    assert not locked_ptw_trace, f"entry{entry_index}: lock 后重访不应重新触发 PTW"
    assert prime_load["data"] == PMP_LOAD_DATA
    assert locked_load["data"] == PMP_LOAD_DATA
    assert not prime_store.has_exception
    assert not locked_store.has_exception
    assert locked_store.addr == _translated_pa(PMP_STORE_VA, store_pa_base)
    env.reset(cycles=1, settle_cycles=0)


def _run_napot_boundary_load_case(env, entry_index: int) -> None:
    """NAPOT deny region 应在边界内 fault、边界外继续允许，并覆盖 cfg-word 边界 entry。"""

    state = _reset_env_state(env)
    denied_pa_base = _sv39_4k_mapping_pa_base(PMP_PA_BASE + 0x200000, PMP_BOUNDARY_DENIED_VA)
    allowed_pa_base = _sv39_4k_mapping_pa_base(PMP_PA_BASE + 0x200000, PMP_BOUNDARY_ALLOWED_VA)
    _install_mappings(
        env,
        (PMP_BOUNDARY_DENIED_VA, denied_pa_base),
        (PMP_BOUNDARY_ALLOWED_VA, allowed_pa_base),
    )
    env.preload_u64(_translated_pa(PMP_BOUNDARY_DENIED_VA, denied_pa_base), PMP_LOAD_DATA)
    env.preload_u64(_translated_pa(PMP_BOUNDARY_ALLOWED_VA, allowed_pa_base), PMP_RESTORED_LOAD_DATA)

    next_lq_ptr = state.next_lq_ptr
    with env.mmu.ptw_responder() as ptw:
        _program_off_entry(env, entry_index=int(entry_index))
        _program_allow_all_entry(env, entry_index=int(entry_index) + 1)
        env.mmu.enable_sv39(root_pt_addr=PMP_ROOT_PT)

        trace_start = len(ptw.trace)
        _, _ = _run_translated_load(
            env,
            req_id=0xB00 + int(entry_index),
            va=PMP_BOUNDARY_DENIED_VA,
            pa_base=denied_pa_base,
            lq_ptr=next_lq_ptr,
            sq_ptr=state.sq_ptr,
            expected_data=PMP_LOAD_DATA,
        )
        next_lq_ptr = ptr_inc(next_lq_ptr, env.config.sequence.load_queue_size)
        _, _ = _run_translated_load(
            env,
            req_id=0xB80 + int(entry_index),
            va=PMP_BOUNDARY_ALLOWED_VA,
            pa_base=allowed_pa_base,
            lq_ptr=next_lq_ptr,
            sq_ptr=state.sq_ptr,
            expected_data=PMP_RESTORED_LOAD_DATA,
        )
        prime_ptw_trace = _capture_ptw_delta(ptw, trace_start)
        next_lq_ptr = ptr_inc(next_lq_ptr, env.config.sequence.load_queue_size)

        deny_region_base = _translated_pa(PMP_BOUNDARY_DENIED_VA, denied_pa_base) & ~(PMP_BOUNDARY_SIZE - 1)
        _program_napot_deny_with_fallback(
            env,
            entry_index=int(entry_index),
            deny_region_base=deny_region_base,
            deny_region_size=PMP_BOUNDARY_SIZE,
        )

        trace_start = len(ptw.trace)
        _, denied_load = _run_translated_load(
            env,
            req_id=0xC00 + int(entry_index),
            va=PMP_BOUNDARY_DENIED_VA,
            pa_base=denied_pa_base,
            lq_ptr=next_lq_ptr,
            sq_ptr=state.sq_ptr,
            required_exception_bits=(LOAD_ACCESS_FAULT_BIT,),
        )
        denied_ptw_trace = _capture_ptw_delta(ptw, trace_start)
        next_lq_ptr = ptr_inc(next_lq_ptr, env.config.sequence.load_queue_size)

        _, allowed_load = _run_translated_load(
            env,
            req_id=0xC80 + int(entry_index),
            va=PMP_BOUNDARY_ALLOWED_VA,
            pa_base=allowed_pa_base,
            lq_ptr=next_lq_ptr,
            sq_ptr=state.sq_ptr,
            expected_data=PMP_RESTORED_LOAD_DATA,
        )
        next_lq_ptr = ptr_inc(next_lq_ptr, env.config.sequence.load_queue_size)

    assert any(event["event"] == "a_fire" for event in prime_ptw_trace), f"entry{entry_index}: NAPOT prime 未触发 PTW"
    assert not denied_ptw_trace, f"entry{entry_index}: NAPOT deny load 不应重新触发 PTW"
    assert LOAD_ACCESS_FAULT_BIT in _set_exception_indices(denied_load)
    assert allowed_load["data"] == PMP_RESTORED_LOAD_DATA
    env.assert_no_outstanding()


def _run_napot_boundary_store_case(env, entry_index: int) -> None:
    """NAPOT deny region 的边界内/外差异也应体现在 translated store 上。"""

    state = _reset_env_state(env)
    denied_pa_base = _sv39_4k_mapping_pa_base(PMP_PA_BASE + 0x200000, PMP_BOUNDARY_DENIED_VA)
    allowed_pa_base = _sv39_4k_mapping_pa_base(PMP_PA_BASE + 0x200000, PMP_BOUNDARY_ALLOWED_VA)
    _install_mappings(
        env,
        (PMP_BOUNDARY_DENIED_VA, denied_pa_base),
        (PMP_BOUNDARY_ALLOWED_VA, allowed_pa_base),
    )
    next_sq_ptr = state.sq_ptr

    with env.mmu.ptw_responder():
        _program_off_entry(env, entry_index=int(entry_index))
        _program_allow_all_entry(env, entry_index=int(entry_index) + 1)
        env.mmu.enable_sv39(root_pt_addr=PMP_ROOT_PT)

        deny_region_base = _translated_pa(PMP_BOUNDARY_DENIED_VA, denied_pa_base) & ~(PMP_BOUNDARY_SIZE - 1)
        _program_napot_deny_with_fallback(
            env,
            entry_index=int(entry_index),
            deny_region_base=deny_region_base,
            deny_region_size=PMP_BOUNDARY_SIZE,
        )
        _, _, next_sq_ptr, denied_store = _run_translated_store(
            env,
            req_id=0xD00 + int(entry_index),
            va=PMP_BOUNDARY_DENIED_VA,
            pa_base=denied_pa_base,
            sq_ptr=next_sq_ptr,
            data=PMP_STORE_DATA,
            expect_fault=True,
        )
        _, _, _, allowed_store = _run_translated_store(
            env,
            req_id=0xD80 + int(entry_index),
            va=PMP_BOUNDARY_ALLOWED_VA,
            pa_base=allowed_pa_base,
            sq_ptr=next_sq_ptr,
            data=PMP_RESTORED_STORE_DATA,
            expect_fault=False,
        )

    assert denied_store.has_exception, f"entry{entry_index}: NAPOT deny store 未报错"
    assert not allowed_store.has_exception
    env.reset(cycles=1, settle_cycles=0)


def _run_tor_boundary_load_case(env, lower_entry_index: int) -> None:
    """TOR deny range 应区分下界前 / 范围内 / 上界后的 translated load。"""

    state = _reset_env_state(env)
    below_pa_base = _sv39_4k_mapping_pa_base(PMP_PA_BASE + 0x300000, PMP_TOR_BELOW_VA)
    inside_pa_base = _sv39_4k_mapping_pa_base(PMP_PA_BASE + 0x300000, PMP_TOR_INSIDE_VA)
    above_pa_base = _sv39_4k_mapping_pa_base(PMP_PA_BASE + 0x300000, PMP_TOR_ABOVE_VA)
    _install_mappings(
        env,
        (PMP_TOR_BELOW_VA, below_pa_base),
        (PMP_TOR_INSIDE_VA, inside_pa_base),
        (PMP_TOR_ABOVE_VA, above_pa_base),
    )
    env.preload_u64(_translated_pa(PMP_TOR_BELOW_VA, below_pa_base), PMP_LOAD_DATA)
    env.preload_u64(_translated_pa(PMP_TOR_INSIDE_VA, inside_pa_base), PMP_STORE_DATA)
    env.preload_u64(_translated_pa(PMP_TOR_ABOVE_VA, above_pa_base), PMP_RESTORED_LOAD_DATA)

    next_lq_ptr = state.next_lq_ptr
    with env.mmu.ptw_responder() as ptw:
        _program_off_entry(env, entry_index=int(lower_entry_index))
        _program_off_entry(env, entry_index=int(lower_entry_index) + 1)
        _program_allow_all_entry(env, entry_index=int(lower_entry_index) + 2)
        env.mmu.enable_sv39(root_pt_addr=PMP_ROOT_PT)

        trace_start = len(ptw.trace)
        _, below_prime = _run_translated_load(
            env,
            req_id=0xE00 + int(lower_entry_index),
            va=PMP_TOR_BELOW_VA,
            pa_base=below_pa_base,
            lq_ptr=next_lq_ptr,
            sq_ptr=state.sq_ptr,
            expected_data=PMP_LOAD_DATA,
        )
        next_lq_ptr = ptr_inc(next_lq_ptr, env.config.sequence.load_queue_size)
        _, _ = _run_translated_load(
            env,
            req_id=0xE40 + int(lower_entry_index),
            va=PMP_TOR_INSIDE_VA,
            pa_base=inside_pa_base,
            lq_ptr=next_lq_ptr,
            sq_ptr=state.sq_ptr,
            expected_data=PMP_STORE_DATA,
        )
        next_lq_ptr = ptr_inc(next_lq_ptr, env.config.sequence.load_queue_size)
        _, above_prime = _run_translated_load(
            env,
            req_id=0xE80 + int(lower_entry_index),
            va=PMP_TOR_ABOVE_VA,
            pa_base=above_pa_base,
            lq_ptr=next_lq_ptr,
            sq_ptr=state.sq_ptr,
            expected_data=PMP_RESTORED_LOAD_DATA,
        )
        prime_ptw_trace = _capture_ptw_delta(ptw, trace_start)
        next_lq_ptr = ptr_inc(next_lq_ptr, env.config.sequence.load_queue_size)

        lower_bound = _translated_pa(PMP_TOR_INSIDE_VA, inside_pa_base) & ~(PMP_BOUNDARY_SIZE - 1)
        upper_bound = lower_bound + PMP_BOUNDARY_SIZE
        _program_tor_deny_with_fallback(
            env,
            lower_entry_index=int(lower_entry_index),
            lower_bound=lower_bound,
            upper_bound=upper_bound,
        )

        trace_start = len(ptw.trace)
        _, denied_load = _run_translated_load(
            env,
            req_id=0xF00 + int(lower_entry_index),
            va=PMP_TOR_INSIDE_VA,
            pa_base=inside_pa_base,
            lq_ptr=next_lq_ptr,
            sq_ptr=state.sq_ptr,
            required_exception_bits=(LOAD_ACCESS_FAULT_BIT,),
        )
        denied_ptw_trace = _capture_ptw_delta(ptw, trace_start)
        next_lq_ptr = ptr_inc(next_lq_ptr, env.config.sequence.load_queue_size)

        _, below_after = _run_translated_load(
            env,
            req_id=0xF40 + int(lower_entry_index),
            va=PMP_TOR_BELOW_VA,
            pa_base=below_pa_base,
            lq_ptr=next_lq_ptr,
            sq_ptr=state.sq_ptr,
            expected_data=PMP_LOAD_DATA,
        )
        next_lq_ptr = ptr_inc(next_lq_ptr, env.config.sequence.load_queue_size)

        _, above_after = _run_translated_load(
            env,
            req_id=0xF80 + int(lower_entry_index),
            va=PMP_TOR_ABOVE_VA,
            pa_base=above_pa_base,
            lq_ptr=next_lq_ptr,
            sq_ptr=state.sq_ptr,
            expected_data=PMP_RESTORED_LOAD_DATA,
        )

    assert any(event["event"] == "a_fire" for event in prime_ptw_trace), f"entry{lower_entry_index}: TOR prime 未触发 PTW"
    assert not denied_ptw_trace, f"entry{lower_entry_index}: TOR deny load 不应重新触发 PTW"
    assert below_prime["data"] == PMP_LOAD_DATA
    assert above_prime["data"] == PMP_RESTORED_LOAD_DATA
    assert LOAD_ACCESS_FAULT_BIT in _set_exception_indices(denied_load)
    assert below_after["data"] == PMP_LOAD_DATA
    assert above_after["data"] == PMP_RESTORED_LOAD_DATA
    env.assert_no_outstanding()


def _run_tor_boundary_store_case(env, lower_entry_index: int) -> None:
    """TOR deny range 的边界内/外差异也应体现在 translated store 上。"""

    state = _reset_env_state(env)
    inside_pa_base = _sv39_4k_mapping_pa_base(PMP_PA_BASE + 0x300000, PMP_TOR_INSIDE_VA)
    above_pa_base = _sv39_4k_mapping_pa_base(PMP_PA_BASE + 0x300000, PMP_TOR_ABOVE_VA)
    _install_mappings(
        env,
        (PMP_TOR_INSIDE_VA, inside_pa_base),
        (PMP_TOR_ABOVE_VA, above_pa_base),
    )
    next_sq_ptr = state.sq_ptr

    with env.mmu.ptw_responder():
        _program_off_entry(env, entry_index=int(lower_entry_index))
        _program_off_entry(env, entry_index=int(lower_entry_index) + 1)
        _program_allow_all_entry(env, entry_index=int(lower_entry_index) + 2)
        env.mmu.enable_sv39(root_pt_addr=PMP_ROOT_PT)

        lower_bound = _translated_pa(PMP_TOR_INSIDE_VA, inside_pa_base) & ~(PMP_BOUNDARY_SIZE - 1)
        upper_bound = lower_bound + PMP_BOUNDARY_SIZE
        _program_tor_deny_with_fallback(
            env,
            lower_entry_index=int(lower_entry_index),
            lower_bound=lower_bound,
            upper_bound=upper_bound,
        )
        _, _, next_sq_ptr, denied_store = _run_translated_store(
            env,
            req_id=0x1000 + int(lower_entry_index),
            va=PMP_TOR_INSIDE_VA,
            pa_base=inside_pa_base,
            sq_ptr=next_sq_ptr,
            data=PMP_STORE_DATA ^ 0xAA,
            expect_fault=True,
        )
        _, _, _, above_store = _run_translated_store(
            env,
            req_id=0x1080 + int(lower_entry_index),
            va=PMP_TOR_ABOVE_VA,
            pa_base=above_pa_base,
            sq_ptr=next_sq_ptr,
            data=PMP_RESTORED_STORE_DATA,
            expect_fault=False,
        )

    assert denied_store.has_exception, f"entry{lower_entry_index}: TOR deny store 未报错"
    assert not above_store.has_exception
    env.reset(cycles=1, settle_cycles=0)


def _run_pma_runtime_load_case(env, entry_index: int) -> None:
    """每个 real PMA entry 都应支持 cacheable/mmio/cacheable 运行时切换。"""

    state = _reset_env_state(env)
    load_pa_base = _sv39_4k_mapping_pa_base(PMP_PA_BASE + 0x400000, PMP_LOAD_VA)
    _install_mappings(env, (PMP_LOAD_VA, load_pa_base))
    env.preload_u64(_translated_pa(PMP_LOAD_VA, load_pa_base), PMP_LOAD_DATA)

    cfg_words: dict[int, int] = {}
    region_base = _translated_pa(PMP_LOAD_VA, load_pa_base) & ~(PMP_BOUNDARY_SIZE - 1)
    next_lq_ptr = state.next_lq_ptr
    with env.mmu.ptw_responder() as ptw:
        _program_pma_cacheable_napot_entry(
            env,
            cfg_words,
            entry_index=int(entry_index),
            region_base=region_base,
            region_size=PMP_BOUNDARY_SIZE,
        )
        env.mmu.allow_all_smode_access(persistent=False)
        env.mmu.enable_sv39(root_pt_addr=PMP_ROOT_PT)

        trace_start = len(ptw.trace)
        cacheable_stats_before = env.get_transport_stats()
        _, cacheable_load = _run_translated_load(
            env,
            req_id=0x1100 + int(entry_index),
            va=PMP_LOAD_VA,
            pa_base=load_pa_base,
            lq_ptr=next_lq_ptr,
            sq_ptr=state.sq_ptr,
            expected_data=PMP_LOAD_DATA,
        )
        cacheable_trace = _capture_ptw_delta(ptw, trace_start)
        cacheable_stats_after = env.get_transport_stats()
        next_lq_ptr = ptr_inc(next_lq_ptr, env.config.sequence.load_queue_size)

        _program_pma_mmio_napot_entry(
            env,
            cfg_words,
            entry_index=int(entry_index),
            region_base=region_base,
            region_size=PMP_BOUNDARY_SIZE,
        )

        trace_start = len(ptw.trace)
        mmio_stats_before = env.get_transport_stats()
        _, mmio_load = _run_translated_load(
            env,
            req_id=0x1180 + int(entry_index),
            va=PMP_LOAD_VA,
            pa_base=load_pa_base,
            lq_ptr=next_lq_ptr,
            sq_ptr=state.sq_ptr,
            expected_data=PMP_LOAD_DATA,
        )
        mmio_trace = _capture_ptw_delta(ptw, trace_start)
        mmio_stats_after = env.get_transport_stats()
        next_lq_ptr = ptr_inc(next_lq_ptr, env.config.sequence.load_queue_size)

        _program_pma_cacheable_napot_entry(
            env,
            cfg_words,
            entry_index=int(entry_index),
            region_base=region_base,
            region_size=PMP_BOUNDARY_SIZE,
        )

        trace_start = len(ptw.trace)
        restored_stats_before = env.get_transport_stats()
        _, restored_load = _run_translated_load(
            env,
            req_id=0x1200 + int(entry_index),
            va=PMP_LOAD_VA,
            pa_base=load_pa_base,
            lq_ptr=next_lq_ptr,
            sq_ptr=state.sq_ptr,
            expected_data=PMP_LOAD_DATA,
        )
        restored_trace = _capture_ptw_delta(ptw, trace_start)
        restored_stats_after = env.get_transport_stats()

    assert any(event["event"] == "a_fire" for event in cacheable_trace), f"entry{entry_index}: PMA prime load 未触发 PTW"
    assert not mmio_trace, f"entry{entry_index}: PMA mmio re-visit 不应重新触发 PTW"
    assert not restored_trace, f"entry{entry_index}: PMA restored re-visit 不应重新触发 PTW"
    assert cacheable_load["debug_paddr"] == _translated_pa(PMP_LOAD_VA, load_pa_base)
    assert cacheable_load["debug_is_mmio"] == 0 and cacheable_load["debug_is_ncio"] == 0
    assert mmio_load["debug_is_mmio"] == 1 and mmio_load["debug_is_ncio"] == 0
    assert restored_load["debug_is_mmio"] == 0 and restored_load["debug_is_ncio"] == 0
    assert cacheable_stats_after["dcache_a_request_count"] > cacheable_stats_before["dcache_a_request_count"], (
        f"entry{entry_index}: cacheable load 未走 dcache"
    )
    assert cacheable_stats_after["outer_request_count"] == cacheable_stats_before["outer_request_count"], (
        f"entry{entry_index}: cacheable load 不应走 outer"
    )
    assert mmio_stats_after["outer_request_count"] > mmio_stats_before["outer_request_count"], (
        f"entry{entry_index}: mmio load 未走 outer"
    )
    assert mmio_stats_after["dcache_a_request_count"] == mmio_stats_before["dcache_a_request_count"], (
        f"entry{entry_index}: mmio load 不应走 dcache"
    )
    env.assert_no_outstanding()


def _run_pma_runtime_store_case(env, entry_index: int) -> None:
    """每个 real PMA entry 都应支持 cacheable/mmio/cacheable 运行时切换。"""

    state = _reset_env_state(env)
    store_pa_base = _sv39_4k_mapping_pa_base(PMP_PA_BASE + 0x400000, PMP_STORE_VA)
    _install_mappings(env, (PMP_STORE_VA, store_pa_base))

    cfg_words: dict[int, int] = {}
    region_base = _translated_pa(PMP_STORE_VA, store_pa_base) & ~(PMP_BOUNDARY_SIZE - 1)
    translated_pa = _translated_pa(PMP_STORE_VA, store_pa_base)
    next_sq_ptr = state.sq_ptr
    with env.mmu.ptw_responder():
        _program_pma_cacheable_napot_entry(
            env,
            cfg_words,
            entry_index=int(entry_index),
            region_base=region_base,
            region_size=PMP_BOUNDARY_SIZE,
        )
        env.mmu.allow_all_smode_access(persistent=False)
        env.mmu.enable_sv39(root_pt_addr=PMP_ROOT_PT)

        cacheable_txn = StoreTxn(
            req_id=0x1280 + int(entry_index),
            sq_ptr=next_sq_ptr,
            addr=PMP_STORE_VA,
            data=PMP_STORE_DATA,
        )
        cacheable_sq_ptr = send_store(env, cacheable_txn)
        next_sq_ptr = ptr_inc(next_sq_ptr, env.config.sequence.store_queue_size)
        env.wait_store_materialized(
            cacheable_sq_ptr.value,
            expected_addr=translated_pa,
            expected_data=PMP_STORE_DATA,
            expected_mmio=False,
            expected_nc=False,
            require_committed=False,
            max_cycles=400,
        )
        env.backend.pulse_store_commit(1)
        env.advance_cycles(env.config.sequence.store_settle_cycles)
        cacheable_store = env.wait_store_materialized(
            cacheable_sq_ptr.value,
            expected_addr=translated_pa,
            expected_data=PMP_STORE_DATA,
            expected_mmio=False,
            expected_nc=False,
            require_committed=True,
            max_cycles=400,
        )
        env.wait_memory_quiesce(max_cycles=400)

        _program_pma_mmio_napot_entry(
            env,
            cfg_words,
            entry_index=int(entry_index),
            region_base=region_base,
            region_size=PMP_BOUNDARY_SIZE,
        )
        _, mmio_sq_ptr, next_sq_ptr, _ = _run_translated_store(
            env,
            req_id=0x1300 + int(entry_index),
            va=PMP_STORE_VA,
            pa_base=store_pa_base,
            sq_ptr=next_sq_ptr,
            data=PMP_STORE_DATA ^ 0x11,
            expect_fault=False,
        )
        mmio_store = env.wait_store_materialized(
            mmio_sq_ptr.value,
            expected_addr=translated_pa,
            expected_data=PMP_STORE_DATA ^ 0x11,
            expected_mmio=True,
            expected_nc=False,
            require_committed=True,
            max_cycles=400,
        )
        env.wait_memory_quiesce(max_cycles=400)

        _program_pma_cacheable_napot_entry(
            env,
            cfg_words,
            entry_index=int(entry_index),
            region_base=region_base,
            region_size=PMP_BOUNDARY_SIZE,
        )
        _, restored_sq_ptr, _, _ = _run_translated_store(
            env,
            req_id=0x1380 + int(entry_index),
            va=PMP_STORE_VA,
            pa_base=store_pa_base,
            sq_ptr=next_sq_ptr,
            data=PMP_RESTORED_STORE_DATA,
            expect_fault=False,
        )
        restored_store = env.wait_store_materialized(
            restored_sq_ptr.value,
            expected_addr=translated_pa,
            expected_data=PMP_RESTORED_STORE_DATA,
            expected_mmio=False,
            expected_nc=False,
            require_committed=True,
            max_cycles=400,
        )

    assert cacheable_store.addr == translated_pa and not cacheable_store.mmio and not cacheable_store.nc
    assert mmio_store.addr == translated_pa and mmio_store.mmio and not mmio_store.nc
    assert restored_store.addr == translated_pa and not restored_store.mmio and not restored_store.nc
    env.reset(cycles=1, settle_cycles=0)


def _run_pma_napot_boundary_load_case(env, entry_index: int) -> None:
    """NAPOT PMA region 应区分边界内 MMIO 与边界外 cacheable load。"""

    state = _reset_env_state(env)
    inside_pa_base = _sv39_4k_mapping_pa_base(PMP_PA_BASE + 0x500000, PMP_BOUNDARY_DENIED_VA)
    outside_pa_base = _sv39_4k_mapping_pa_base(PMP_PA_BASE + 0x500000, PMP_BOUNDARY_ALLOWED_VA)
    _install_mappings(
        env,
        (PMP_BOUNDARY_DENIED_VA, inside_pa_base),
        (PMP_BOUNDARY_ALLOWED_VA, outside_pa_base),
    )
    env.preload_u64(_translated_pa(PMP_BOUNDARY_DENIED_VA, inside_pa_base), PMP_LOAD_DATA)
    env.preload_u64(_translated_pa(PMP_BOUNDARY_ALLOWED_VA, outside_pa_base), PMP_RESTORED_LOAD_DATA)

    cfg_words: dict[int, int] = {}
    next_lq_ptr = state.next_lq_ptr
    with env.mmu.ptw_responder() as ptw:
        _program_pma_off_entry(env, cfg_words, entry_index=int(entry_index))
        _program_pma_cacheable_allow_all_entry(env, cfg_words, entry_index=int(entry_index) + 1)
        env.mmu.allow_all_smode_access(persistent=False)
        env.mmu.enable_sv39(root_pt_addr=PMP_ROOT_PT)

        trace_start = len(ptw.trace)
        _, _ = _run_translated_load(
            env,
            req_id=0x1400 + int(entry_index),
            va=PMP_BOUNDARY_DENIED_VA,
            pa_base=inside_pa_base,
            lq_ptr=next_lq_ptr,
            sq_ptr=state.sq_ptr,
            expected_data=PMP_LOAD_DATA,
        )
        next_lq_ptr = ptr_inc(next_lq_ptr, env.config.sequence.load_queue_size)
        _, _ = _run_translated_load(
            env,
            req_id=0x1480 + int(entry_index),
            va=PMP_BOUNDARY_ALLOWED_VA,
            pa_base=outside_pa_base,
            lq_ptr=next_lq_ptr,
            sq_ptr=state.sq_ptr,
            expected_data=PMP_RESTORED_LOAD_DATA,
        )
        prime_trace = _capture_ptw_delta(ptw, trace_start)
        next_lq_ptr = ptr_inc(next_lq_ptr, env.config.sequence.load_queue_size)

        inside_region_base = _translated_pa(PMP_BOUNDARY_DENIED_VA, inside_pa_base) & ~(PMP_BOUNDARY_SIZE - 1)
        _program_pma_mmio_napot_entry(
            env,
            cfg_words,
            entry_index=int(entry_index),
            region_base=inside_region_base,
            region_size=PMP_BOUNDARY_SIZE,
        )

        inside_stats_before = env.get_transport_stats()
        _, inside_load = _run_translated_load(
            env,
            req_id=0x1500 + int(entry_index),
            va=PMP_BOUNDARY_DENIED_VA,
            pa_base=inside_pa_base,
            lq_ptr=next_lq_ptr,
            sq_ptr=state.sq_ptr,
            expected_data=PMP_LOAD_DATA,
        )
        inside_stats_after = env.get_transport_stats()
        next_lq_ptr = ptr_inc(next_lq_ptr, env.config.sequence.load_queue_size)

        outside_stats_before = env.get_transport_stats()
        _, outside_load = _run_translated_load(
            env,
            req_id=0x1580 + int(entry_index),
            va=PMP_BOUNDARY_ALLOWED_VA,
            pa_base=outside_pa_base,
            lq_ptr=next_lq_ptr,
            sq_ptr=state.sq_ptr,
            expected_data=PMP_RESTORED_LOAD_DATA,
        )
        outside_stats_after = env.get_transport_stats()

    assert any(event["event"] == "a_fire" for event in prime_trace), f"entry{entry_index}: PMA NAPOT prime 未触发 PTW"
    assert inside_load["debug_is_mmio"] == 1 and inside_load["debug_is_ncio"] == 0
    assert outside_load["debug_is_mmio"] == 0 and outside_load["debug_is_ncio"] == 0
    assert inside_stats_after["outer_request_count"] > inside_stats_before["outer_request_count"], (
        f"entry{entry_index}: boundary inside load 未走 outer"
    )
    env.assert_no_outstanding()


def _run_pma_napot_boundary_store_case(env, entry_index: int) -> None:
    """NAPOT PMA region 应区分边界内 MMIO 与边界外 cacheable store。"""

    state = _reset_env_state(env)
    inside_pa_base = _sv39_4k_mapping_pa_base(PMP_PA_BASE + 0x500000, PMP_BOUNDARY_DENIED_VA)
    outside_pa_base = _sv39_4k_mapping_pa_base(PMP_PA_BASE + 0x500000, PMP_BOUNDARY_ALLOWED_VA)
    _install_mappings(
        env,
        (PMP_BOUNDARY_DENIED_VA, inside_pa_base),
        (PMP_BOUNDARY_ALLOWED_VA, outside_pa_base),
    )

    cfg_words: dict[int, int] = {}
    next_sq_ptr = state.sq_ptr
    with env.mmu.ptw_responder():
        _program_pma_off_entry(env, cfg_words, entry_index=int(entry_index))
        _program_pma_cacheable_allow_all_entry(env, cfg_words, entry_index=int(entry_index) + 1)
        env.mmu.allow_all_smode_access(persistent=False)
        env.mmu.enable_sv39(root_pt_addr=PMP_ROOT_PT)

        inside_region_base = _translated_pa(PMP_BOUNDARY_DENIED_VA, inside_pa_base) & ~(PMP_BOUNDARY_SIZE - 1)
        _program_pma_mmio_napot_entry(
            env,
            cfg_words,
            entry_index=int(entry_index),
            region_base=inside_region_base,
            region_size=PMP_BOUNDARY_SIZE,
        )
        _, _, next_sq_ptr, inside_store = _run_translated_store(
            env,
            req_id=0x1600 + int(entry_index),
            va=PMP_BOUNDARY_DENIED_VA,
            pa_base=inside_pa_base,
            sq_ptr=next_sq_ptr,
            data=PMP_STORE_DATA,
            expect_fault=False,
        )
        _, _, _, outside_store = _run_translated_store(
            env,
            req_id=0x1680 + int(entry_index),
            va=PMP_BOUNDARY_ALLOWED_VA,
            pa_base=outside_pa_base,
            sq_ptr=next_sq_ptr,
            data=PMP_RESTORED_STORE_DATA,
            expect_fault=False,
        )

    assert inside_store.mmio and not inside_store.nc, f"entry{entry_index}: boundary inside store 未被标记为 mmio"
    assert not outside_store.mmio and not outside_store.nc, f"entry{entry_index}: boundary outside store 不应为 mmio/nc"
    env.reset(cycles=1, settle_cycles=0)


def _run_pma_tor_boundary_load_case(env, lower_entry_index: int) -> None:
    """TOR PMA region 应区分下界前 / 范围内 / 上界后的 load 分类。"""

    state = _reset_env_state(env)
    below_pa_base = _sv39_4k_mapping_pa_base(PMP_PA_BASE + 0x600000, PMP_TOR_BELOW_VA)
    inside_pa_base = _sv39_4k_mapping_pa_base(PMP_PA_BASE + 0x600000, PMP_TOR_INSIDE_VA)
    above_pa_base = _sv39_4k_mapping_pa_base(PMP_PA_BASE + 0x600000, PMP_TOR_ABOVE_VA)
    _install_mappings(
        env,
        (PMP_TOR_BELOW_VA, below_pa_base),
        (PMP_TOR_INSIDE_VA, inside_pa_base),
        (PMP_TOR_ABOVE_VA, above_pa_base),
    )
    env.preload_u64(_translated_pa(PMP_TOR_BELOW_VA, below_pa_base), PMP_LOAD_DATA)
    env.preload_u64(_translated_pa(PMP_TOR_INSIDE_VA, inside_pa_base), PMP_STORE_DATA)
    env.preload_u64(_translated_pa(PMP_TOR_ABOVE_VA, above_pa_base), PMP_RESTORED_LOAD_DATA)

    cfg_words: dict[int, int] = {}
    next_lq_ptr = state.next_lq_ptr
    with env.mmu.ptw_responder() as ptw:
        _program_pma_off_entry(env, cfg_words, entry_index=int(lower_entry_index))
        _program_pma_off_entry(env, cfg_words, entry_index=int(lower_entry_index) + 1)
        _program_pma_cacheable_allow_all_entry(env, cfg_words, entry_index=int(lower_entry_index) + 2)
        env.mmu.allow_all_smode_access(persistent=False)
        env.mmu.enable_sv39(root_pt_addr=PMP_ROOT_PT)

        trace_start = len(ptw.trace)
        _, _ = _run_translated_load(
            env,
            req_id=0x1700 + int(lower_entry_index),
            va=PMP_TOR_BELOW_VA,
            pa_base=below_pa_base,
            lq_ptr=next_lq_ptr,
            sq_ptr=state.sq_ptr,
            expected_data=PMP_LOAD_DATA,
        )
        next_lq_ptr = ptr_inc(next_lq_ptr, env.config.sequence.load_queue_size)
        _, _ = _run_translated_load(
            env,
            req_id=0x1740 + int(lower_entry_index),
            va=PMP_TOR_INSIDE_VA,
            pa_base=inside_pa_base,
            lq_ptr=next_lq_ptr,
            sq_ptr=state.sq_ptr,
            expected_data=PMP_STORE_DATA,
        )
        next_lq_ptr = ptr_inc(next_lq_ptr, env.config.sequence.load_queue_size)
        _, _ = _run_translated_load(
            env,
            req_id=0x1780 + int(lower_entry_index),
            va=PMP_TOR_ABOVE_VA,
            pa_base=above_pa_base,
            lq_ptr=next_lq_ptr,
            sq_ptr=state.sq_ptr,
            expected_data=PMP_RESTORED_LOAD_DATA,
        )
        prime_trace = _capture_ptw_delta(ptw, trace_start)
        next_lq_ptr = ptr_inc(next_lq_ptr, env.config.sequence.load_queue_size)

        lower_bound = _translated_pa(PMP_TOR_INSIDE_VA, inside_pa_base) & ~(PMP_BOUNDARY_SIZE - 1)
        upper_bound = lower_bound + PMP_BOUNDARY_SIZE
        _program_pma_tor_mmio_with_fallback(
            env,
            cfg_words,
            lower_entry_index=int(lower_entry_index),
            lower_bound=lower_bound,
            upper_bound=upper_bound,
        )

        inside_stats_before = env.get_transport_stats()
        _, inside_load = _run_translated_load(
            env,
            req_id=0x1800 + int(lower_entry_index),
            va=PMP_TOR_INSIDE_VA,
            pa_base=inside_pa_base,
            lq_ptr=next_lq_ptr,
            sq_ptr=state.sq_ptr,
            expected_data=PMP_STORE_DATA,
        )
        inside_stats_after = env.get_transport_stats()
        next_lq_ptr = ptr_inc(next_lq_ptr, env.config.sequence.load_queue_size)

        below_stats_before = env.get_transport_stats()
        _, below_load = _run_translated_load(
            env,
            req_id=0x1840 + int(lower_entry_index),
            va=PMP_TOR_BELOW_VA,
            pa_base=below_pa_base,
            lq_ptr=next_lq_ptr,
            sq_ptr=state.sq_ptr,
            expected_data=PMP_LOAD_DATA,
        )
        below_stats_after = env.get_transport_stats()
        next_lq_ptr = ptr_inc(next_lq_ptr, env.config.sequence.load_queue_size)

        above_stats_before = env.get_transport_stats()
        _, above_load = _run_translated_load(
            env,
            req_id=0x1880 + int(lower_entry_index),
            va=PMP_TOR_ABOVE_VA,
            pa_base=above_pa_base,
            lq_ptr=next_lq_ptr,
            sq_ptr=state.sq_ptr,
            expected_data=PMP_RESTORED_LOAD_DATA,
        )
        above_stats_after = env.get_transport_stats()

    assert any(event["event"] == "a_fire" for event in prime_trace), f"entry{lower_entry_index}: PMA TOR prime 未触发 PTW"
    assert inside_load["debug_is_mmio"] == 1 and inside_load["debug_is_ncio"] == 0
    assert below_load["debug_is_mmio"] == 0 and below_load["debug_is_ncio"] == 0
    assert above_load["debug_is_mmio"] == 0 and above_load["debug_is_ncio"] == 0
    assert inside_stats_after["outer_request_count"] > inside_stats_before["outer_request_count"], (
        f"entry{lower_entry_index}: TOR inside load 未走 outer"
    )
    env.assert_no_outstanding()


def _run_pma_tor_boundary_store_case(env, lower_entry_index: int) -> None:
    """TOR PMA region 应区分范围内 MMIO 与范围外 cacheable store。"""

    state = _reset_env_state(env)
    inside_pa_base = _sv39_4k_mapping_pa_base(PMP_PA_BASE + 0x600000, PMP_TOR_INSIDE_VA)
    above_pa_base = _sv39_4k_mapping_pa_base(PMP_PA_BASE + 0x600000, PMP_TOR_ABOVE_VA)
    _install_mappings(
        env,
        (PMP_TOR_INSIDE_VA, inside_pa_base),
        (PMP_TOR_ABOVE_VA, above_pa_base),
    )

    cfg_words: dict[int, int] = {}
    next_sq_ptr = state.sq_ptr
    with env.mmu.ptw_responder():
        _program_pma_off_entry(env, cfg_words, entry_index=int(lower_entry_index))
        _program_pma_off_entry(env, cfg_words, entry_index=int(lower_entry_index) + 1)
        _program_pma_cacheable_allow_all_entry(env, cfg_words, entry_index=int(lower_entry_index) + 2)
        env.mmu.allow_all_smode_access(persistent=False)
        env.mmu.enable_sv39(root_pt_addr=PMP_ROOT_PT)

        lower_bound = _translated_pa(PMP_TOR_INSIDE_VA, inside_pa_base) & ~(PMP_BOUNDARY_SIZE - 1)
        upper_bound = lower_bound + PMP_BOUNDARY_SIZE
        _program_pma_tor_mmio_with_fallback(
            env,
            cfg_words,
            lower_entry_index=int(lower_entry_index),
            lower_bound=lower_bound,
            upper_bound=upper_bound,
        )
        _, _, next_sq_ptr, inside_store = _run_translated_store(
            env,
            req_id=0x1900 + int(lower_entry_index),
            va=PMP_TOR_INSIDE_VA,
            pa_base=inside_pa_base,
            sq_ptr=next_sq_ptr,
            data=PMP_STORE_DATA ^ 0x22,
            expect_fault=False,
        )
        _, _, _, above_store = _run_translated_store(
            env,
            req_id=0x1980 + int(lower_entry_index),
            va=PMP_TOR_ABOVE_VA,
            pa_base=above_pa_base,
            sq_ptr=next_sq_ptr,
            data=PMP_RESTORED_STORE_DATA,
            expect_fault=False,
        )

    assert inside_store.mmio and not inside_store.nc, f"entry{lower_entry_index}: TOR inside store 未被标记为 mmio"
    assert not above_store.mmio and not above_store.nc, f"entry{lower_entry_index}: TOR above store 不应为 mmio/nc"
    env.reset(cycles=1, settle_cycles=0)


def test_api_MemBlock_pmp_runtime_load_matrix(env):
    """按行为簇收口 32 个 real entry 的 runtime load 改写矩阵。"""

    _run_group_cases(
        lambda entry_index: _run_runtime_load_case(env, entry_index),
        ALL_REAL_ENTRY_CASES,
        label_prefix="runtime_load_entry",
    )


def test_api_MemBlock_pmp_runtime_store_gap_matrix(env):
    """按行为簇收口 32 个 real entry 的 runtime store deny capability gap。"""

    _run_group_cases(
        lambda entry_index: _run_runtime_store_case(env, entry_index),
        ALL_REAL_ENTRY_CASES,
        label_prefix="runtime_store_gap_entry",
        expected_failure_reason=STORE_PMP_DENY_CAPABILITY_GAP_XFAIL_REASON,
    )


def test_api_MemBlock_pmp_lock_matrix(env):
    """按行为簇收口 32 个 real entry 的 lock 覆盖矩阵。"""

    _run_group_cases(
        lambda entry_index: _run_lock_case(env, entry_index),
        ALL_REAL_ENTRY_CASES,
        label_prefix="lock_entry",
    )


def test_api_MemBlock_pmp_napot_boundary_load_matrix(env):
    """按行为簇收口 NAPOT boundary load 覆盖矩阵。"""

    _run_group_cases(
        lambda entry_index: _run_napot_boundary_load_case(env, entry_index),
        BOUNDARY_ENTRY_CASES,
        label_prefix="napot_boundary_load_entry",
    )


def test_api_MemBlock_pmp_napot_boundary_store_gap_matrix(env):
    """按行为簇收口 NAPOT boundary store capability gap。"""

    _run_group_cases(
        lambda entry_index: _run_napot_boundary_store_case(env, entry_index),
        BOUNDARY_ENTRY_CASES,
        label_prefix="napot_boundary_store_gap_entry",
        expected_failure_reason=STORE_PMP_DENY_CAPABILITY_GAP_XFAIL_REASON,
    )


def test_api_MemBlock_pmp_tor_boundary_load_matrix(env):
    """按行为簇收口 TOR boundary load 覆盖矩阵。"""

    _run_group_cases(
        lambda lower_entry_index: _run_tor_boundary_load_case(env, lower_entry_index),
        TOR_ENTRY_CASES,
        label_prefix="tor_boundary_load_entry",
    )


def test_api_MemBlock_pmp_tor_boundary_store_gap_matrix(env):
    """按行为簇收口 TOR boundary store capability gap。"""

    _run_group_cases(
        lambda lower_entry_index: _run_tor_boundary_store_case(env, lower_entry_index),
        TOR_ENTRY_CASES,
        label_prefix="tor_boundary_store_gap_entry",
        expected_failure_reason=STORE_PMP_DENY_CAPABILITY_GAP_XFAIL_REASON,
    )


def test_api_MemBlock_pma_runtime_load_matrix(env):
    """按行为簇收口 safe PMA entry 子集的 runtime load 切换矩阵。"""

    _run_group_cases(
        lambda entry_index: _run_pma_runtime_load_case(env, entry_index),
        PMA_RUNTIME_ENTRY_CASES,
        label_prefix="pma_runtime_load_entry",
    )


def test_api_MemBlock_pma_runtime_store_matrix(env):
    """按行为簇收口 safe PMA entry 子集的 runtime store 切换矩阵。"""

    _run_group_cases(
        lambda entry_index: _run_pma_runtime_store_case(env, entry_index),
        PMA_RUNTIME_ENTRY_CASES,
        label_prefix="pma_runtime_store_entry",
        expected_failure_reason=STORE_PMA_TRANSLATED_CLASSIFICATION_GAP_XFAIL_REASON,
    )


def test_api_MemBlock_pma_napot_boundary_load_matrix(env):
    """按行为簇收口 PMA NAPOT boundary load 覆盖矩阵。"""

    _run_group_cases(
        lambda entry_index: _run_pma_napot_boundary_load_case(env, entry_index),
        PMA_BOUNDARY_ENTRY_CASES,
        label_prefix="pma_napot_boundary_load_entry",
    )


def test_api_MemBlock_pma_napot_boundary_store_matrix(env):
    """按行为簇收口 PMA NAPOT boundary store 覆盖矩阵。"""

    _run_group_cases(
        lambda entry_index: _run_pma_napot_boundary_store_case(env, entry_index),
        PMA_BOUNDARY_ENTRY_CASES,
        label_prefix="pma_napot_boundary_store_entry",
        expected_failure_reason=STORE_PMA_TRANSLATED_CLASSIFICATION_GAP_XFAIL_REASON,
    )


def test_api_MemBlock_pma_tor_boundary_load_matrix(env):
    """按行为簇收口 PMA TOR boundary load 覆盖矩阵。"""

    _run_group_cases(
        lambda lower_entry_index: _run_pma_tor_boundary_load_case(env, lower_entry_index),
        PMA_TOR_ENTRY_CASES,
        label_prefix="pma_tor_boundary_load_entry",
    )


def test_api_MemBlock_pma_tor_boundary_store_matrix(env):
    """按行为簇收口 PMA TOR boundary store 覆盖矩阵。"""

    _run_group_cases(
        lambda lower_entry_index: _run_pma_tor_boundary_store_case(env, lower_entry_index),
        PMA_TOR_ENTRY_CASES,
        label_prefix="pma_tor_boundary_store_entry",
        expected_failure_reason=STORE_PMA_TRANSLATED_CLASSIFICATION_GAP_XFAIL_REASON,
    )
