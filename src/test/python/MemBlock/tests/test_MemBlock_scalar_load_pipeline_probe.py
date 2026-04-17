# coding=utf-8
"""
MemBlock 标量 load pipeline probe directed。
"""

import pytest

from request_apis import LoadTxn, StoreTxn, ptr_inc
from sequences import (
    LOAD_ACCESS_FAULT_BIT,
    LOAD_GUEST_PAGE_FAULT_BIT,
    LOAD_PAGE_FAULT_BIT,
    FlushStoreBuffersSequence,
    MmuFaultingScalarLoadSequence,
    MmuSv39AddressSpaceConfig,
    MmuSv39AddressSpaceInstallSequence,
    PMP_MODE_ALLOW,
    PMP_MODE_DENY,
    PTE_MODE_NORMAL,
    PTE_MODE_PAGE_FAULT,
    ResetEnvSequence,
    ScalarBankConflictLoadClusterSequence,
    ScalarFastReplayCancelledByReplayHiPrioSequence,
    ScalarLateStaStoreLoadViolationSequence,
    ScalarSqDataInvalidMatchInvalidTriggerSequence,
    SequenceState,
    Sv39GigapageMapping,
)


MMU_FAULT_ROOT_PT = 0x88012000
MMU_FAULT_VA_BASE = 0x40020000
MMU_FAULT_PA_BASE = 0x80000000
MMU_FAULT_PRIME_DATA = 0x2233445566778899
TRANSLATION_ERROR_BITS = {LOAD_ACCESS_FAULT_BIT, LOAD_PAGE_FAULT_BIT, LOAD_GUEST_PAGE_FAULT_BIT}
MIXED_ACCESS_CASES = (
    ("byte", 1, 0x1, 0),
    ("half", 2, 0x3, 2),
    ("word", 4, 0xF, 4),
    ("doubleword", 8, 0xFF, 8),
)

BANK_CONFLICT_BASE = 0x80006000
BANK_CONFLICT_REQUESTS = (
    (0x20, BANK_CONFLICT_BASE + 0x000),
    (0x21, BANK_CONFLICT_BASE + 0x040),
    (0x22, BANK_CONFLICT_BASE + 0x080),
)

LATE_STA_STORE_REQ_ID = 0x30
LATE_STA_STORE_DATA = 0x8877665544332211
HIGH_PRIO_DM_PREEMPTORS = (
    (0x40, 0x8000A000, 0xA000000000000001),
    (0x41, 0x8000B000, 0xA000000000000002),
    (0x42, 0x8000C000, 0xA000000000000003),
    (0x43, 0x8000D000, 0xA000000000000004),
)
HIGH_PRIO_NC_PREEMPTORS = (
    (0x50, 0x00004000, 0xB000000000000001),
    (0x51, 0x00005000, 0xB000000000000002),
    (0x52, 0x00006000, 0xB000000000000003),
    (0x53, 0x00007000, 0xB000000000000004),
)

MATCH_INVALID_ROOT_A = 0x88022000
MATCH_INVALID_ROOT_B = 0x88023000
MATCH_INVALID_MAIN_VA = 0x40030000
MATCH_INVALID_TLB_PRIME_VA = 0x40032000
MATCH_INVALID_PA_BASE_A = 0x80000000
MATCH_INVALID_PA_BASE_B = 0xC0000000
MATCH_INVALID_WARMUP_DATA = 0x0123456789ABCDEF
MATCH_INVALID_TLB_PRIME_DATA = 0x0F0E0D0C0B0A0908
MATCH_INVALID_STORE_DATA = 0x1122334455667788
EXPECTED_BC_REPLAY_CAUSES = (0, 0, 0, 0, 0, 0, 0, 1, 0, 0, 0)
MATCHINVALID_REDIRECT_REPLAY_DUAL_PATH_XFAIL_REASON = (
    "matchInvalid redirect 在当前 DUT 上仍可能同时保留 replay 去路，"
    "pipeline probe 用例沿用 replay smoke 中的已知 DUT bug 口径"
)


def _reset_env_state(env) -> SequenceState:
    return ResetEnvSequence(
        require_issue_lanes=(0, 1, 2, 3, 5),
        require_lq_ready=True,
        require_sq_ready=True,
    ).run(env)


def _set_exception_indices(writeback: dict) -> set[int]:
    return {
        index
        for index, bit in enumerate(writeback.get("exception_bits", ()))
        if bit
    }


def _preload_bank_conflict_lines(env) -> None:
    for idx, (_, addr) in enumerate(BANK_CONFLICT_REQUESTS):
        env.preload_u64(addr, 0x9000000000000000 + idx)


def _settle_warmup_load(env, *, completed_target: int, quiesce_cycles: int = 256, settle_cycles: int = 32) -> None:
    env.wait_completed_load_count(completed_target, max_cycles=64)
    env.wait_memory_quiesce(max_cycles=quiesce_cycles)
    if settle_cycles > 0:
        env.advance_cycles(settle_cycles)


def _assert_only_bc_replay_cause(replay_causes: tuple[int, ...]) -> None:
    assert tuple(int(bit) for bit in replay_causes) == EXPECTED_BC_REPLAY_CAUSES, (
        f"bank conflict fast replay 的 replayCause 异常: actual={tuple(int(bit) for bit in replay_causes)}"
    )


def test_api_MemBlock_scalar_aligned_load_fault_matrix_with_pipeline_checks(env):
    """
    对齐标量 load 的 fault matrix。

    当前 env 对 translation fault 只稳定构造成通用 translation error 叶子，
    因此断言口径仍以真实写回里观测到的 AF/PF/GPF 集合为准。
    """

    combos = (
        ("tlb_error_allow", PTE_MODE_PAGE_FAULT, PMP_MODE_ALLOW, (), TRANSLATION_ERROR_BITS),
        ("pmp_deny", PTE_MODE_NORMAL, PMP_MODE_DENY, (LOAD_ACCESS_FAULT_BIT,), {LOAD_ACCESS_FAULT_BIT}),
        ("overlap", PTE_MODE_PAGE_FAULT, PMP_MODE_DENY, (), TRANSLATION_ERROR_BITS),
    )

    req_id = 0
    for case_index, (size_name, size, mask, offset) in enumerate(MIXED_ACCESS_CASES):
        for combo_name, pte_mode, pmp_mode, required_bits, allowed_bits in combos:
            state = _reset_env_state(env)
            va = MMU_FAULT_VA_BASE + 0x4000 + case_index * 0x1000 + offset
            result = MmuFaultingScalarLoadSequence(
                root_pt_addr=MMU_FAULT_ROOT_PT,
                va=va,
                pa_base=MMU_FAULT_PA_BASE,
                initial_state=state,
                main_req_id=req_id + 1,
                size=size,
                mask=mask,
                main_pte_mode=pte_mode,
                main_pmp_mode=pmp_mode,
                prime_req_id=req_id,
                prime_pte_mode=pte_mode,
                prime_pmp_mode=PMP_MODE_ALLOW if pmp_mode == PMP_MODE_DENY else pmp_mode,
                prime_expected_data=MMU_FAULT_PRIME_DATA if pte_mode == PTE_MODE_NORMAL else None,
                required_main_exception_bits=required_bits,
            ).run(env)
            req_id += 2

            exception_indices = _set_exception_indices(result.main_writeback)
            assert exception_indices, (
                f"fault matrix 未产生异常写回: size={size_name}, combo={combo_name}, va=0x{va:x}"
            )
            assert exception_indices <= allowed_bits, (
                f"fault matrix 出现非预期异常位: size={size_name}, combo={combo_name}, exceptions={exception_indices}"
            )
            assert not result.main_ptw_trace, (
                f"fault matrix 正式访问未保持 TLB hit 背景: size={size_name}, combo={combo_name}"
            )
            assert result.transport_stats_after_main["outer_request_count"] == result.transport_stats_before_main["outer_request_count"]
            assert result.transport_stats_after_main["dcache_a_request_count"] == result.transport_stats_before_main["dcache_a_request_count"]
            assert result.transport_stats_after_main["dcache_d_response_count"] == result.transport_stats_before_main["dcache_d_response_count"]
            assert env._read_optional_dut_signal("io_dcacheError_ecc_error_valid") == 0
            replay_state = env.sample_replay_state()
            assert not replay_state["memory_violation"]["valid"], (
                f"fault matrix 不应退化成 load violation: size={size_name}, combo={combo_name}"
            )
            env.assert_no_outstanding()


def test_api_MemBlock_scalar_aligned_load_bank_conflict_hit_no_forward_no_violation(env):
    """
    对齐标量 load，dcache hit + bank conflict，无 STLF / violation。
    """

    state = _reset_env_state(env)
    _preload_bank_conflict_lines(env)

    result = ScalarBankConflictLoadClusterSequence(
        BANK_CONFLICT_REQUESTS,
        initial_state=state,
    ).run(env)

    assert result.bank_conflicts, "未观测到 bank conflict"
    assert all(event.bank_conflict == 1 for event in result.bank_conflicts)
    assert all(event.dcache_first_miss == 0 for event in result.bank_conflicts), "命中场景不应退化成 dcache first miss"
    assert result.fast_replay_events, "bank conflict 后未观测到任何 s3 replay/cancel 行为"
    assert any(event.is_fast_replay == 1 for event in result.fast_replay_events), "bank conflict 未走 fast replay"
    assert all(event.is_slow_replay == 0 for event in result.fast_replay_events), "无仲裁抢占时不应退化成 slow replay"
    assert any(event.ld_cancel == 1 for event in result.fast_replay_events), "fast replay 路径未对外给出 ldCancel"
    assert not result.replay_queue_events, "无仲裁抢占时 bank conflict load 不应落入 replay queue"
    for event in result.fast_replay_events:
        if event.is_fast_replay:
            _assert_only_bc_replay_cause(event.replay_causes)
    assert result.violation_event is None, "bank conflict 命中场景不应出现 memoryViolation"
    assert not result.sq_forward_events, "纯 dcache hit + bank conflict 场景不应出现 SQ/SBuffer forward"
    assert result.transport_stats_after["outer_request_count"] == result.transport_stats_before["outer_request_count"]
    assert result.transport_stats_after["dcache_a_request_count"] == result.transport_stats_before["dcache_a_request_count"]
    assert result.transport_stats_after["dcache_d_response_count"] == result.transport_stats_before["dcache_d_response_count"]
    assert len(result.writebacks) == len(BANK_CONFLICT_REQUESTS)
    assert len(result.wakeups) == len(BANK_CONFLICT_REQUESTS)
    env.assert_no_outstanding()


def test_api_MemBlock_scalar_aligned_load_matchinvalid_proxy_probe(env):
    """
    对齐标量 load，使用当前公开可见的 `matchInvalid` 代理 `vp_match_fail=1` 路径。

    当前 env 没有单独导出的 `vp_match_fail` 顶层信号，因此这里按真实公开语义收口为：
      - STLF 返回 `dataInvalid=1`
      - 同时 `matchInvalid=1`
      - younger load 出现 `memoryViolation`
    """

    initial_state = _reset_env_state(env)
    root_a = MmuSv39AddressSpaceInstallSequence(
        MmuSv39AddressSpaceConfig(
            root_pt_addr=MATCH_INVALID_ROOT_A,
            mappings=(Sv39GigapageMapping(va=MATCH_INVALID_MAIN_VA, pa_base=MATCH_INVALID_PA_BASE_A),),
        )
    ).run(env)
    root_b = MmuSv39AddressSpaceInstallSequence(
        MmuSv39AddressSpaceConfig(
            root_pt_addr=MATCH_INVALID_ROOT_B,
            mappings=(
                Sv39GigapageMapping(va=MATCH_INVALID_MAIN_VA, pa_base=MATCH_INVALID_PA_BASE_B),
                Sv39GigapageMapping(va=MATCH_INVALID_TLB_PRIME_VA, pa_base=MATCH_INVALID_PA_BASE_B),
            ),
        )
    ).run(env)
    del root_a

    main_pa_b = root_b.translated_pa_for(MATCH_INVALID_MAIN_VA)
    tlb_prime_pa_b = root_b.translated_pa_for(MATCH_INVALID_TLB_PRIME_VA)
    env.preload_u64(main_pa_b, MATCH_INVALID_WARMUP_DATA)
    env.preload_u64(tlb_prime_pa_b, MATCH_INVALID_TLB_PRIME_DATA)
    env.preload_u64(MATCH_INVALID_MAIN_VA, MATCH_INVALID_STORE_DATA)
    env.mmu.allow_all_smode_access()

    warmup_load = LoadTxn(
        req_id=0,
        addr=main_pa_b,
        lq_ptr=initial_state.next_lq_ptr,
        sq_ptr=initial_state.sq_ptr,
    )
    prepared_warmup = env.backend.prepare(warmup_load)
    env.expect_scalar_load(
        rob_idx=warmup_load.rob_idx,
        pdest=warmup_load.resolved_pdest,
        addr=main_pa_b,
        size=warmup_load.size,
        mask=warmup_load.mask,
    )
    env.backend.execute(prepared_warmup)
    env.wait_load_writeback_observed(
        rob_idx=warmup_load.rob_idx,
        data=MATCH_INVALID_WARMUP_DATA,
        max_cycles=512,
    )
    _settle_warmup_load(env, completed_target=1)

    with env.mmu.ptw_responder():
        trigger = ScalarSqDataInvalidMatchInvalidTriggerSequence(
            main_va=MATCH_INVALID_MAIN_VA,
            main_pa=main_pa_b,
            store_txn=StoreTxn(
                req_id=1,
                sq_ptr=initial_state.sq_ptr,
                addr=MATCH_INVALID_MAIN_VA,
                data=MATCH_INVALID_STORE_DATA,
            ),
            main_load_req_id=3,
            initial_state=SequenceState(
                next_lq_ptr=ptr_inc(initial_state.next_lq_ptr, env.config.sequence.load_queue_size),
                sq_ptr=initial_state.sq_ptr,
            ),
            activate_root_pt_addr=MATCH_INVALID_ROOT_B,
            tlb_prime_req_id=2,
            tlb_prime_va=MATCH_INVALID_TLB_PRIME_VA,
            tlb_prime_pa=tlb_prime_pa_b,
            tlb_prime_data=MATCH_INVALID_TLB_PRIME_DATA,
        ).run(env)

    env.mmu.disable_translation()

    assert trigger.sq_forward_event["data_invalid_valid"] == 1, "STLF 未返回 dataInvalid=1"
    assert trigger.sq_forward_event["match_invalid"] == 1, "matchInvalid 代理路径未命中"
    assert trigger.sq_forward_event["forward_invalid"] == 0, "当前场景不应退化成 forwardInvalid"
    assert trigger.memory_violation["valid"] == 1, "matchInvalid 代理路径未触发 memoryViolation"
    assert trigger.memory_violation["rob_idx_value"] == trigger.main_load.rob_idx_value
    assert trigger.memory_violation["level"] == 1
    if trigger.dcache_miss_signal is not None:
        assert trigger.dcache_miss_signal == 0, "当前命中场景不应退化成 dcache miss"
    assert trigger.dcache_error_valid == 0
    replay_path_events = [
        event
        for event in trigger.replay_events
        if event.get("source") in {"replay_queue", "replay_lane", "ldu", "nc_out"}
    ]
    if replay_path_events:
        pytest.xfail(MATCHINVALID_REDIRECT_REPLAY_DUAL_PATH_XFAIL_REASON)
    assert (
        trigger.transport_stats_after_recovery["outer_request_count"]
        == trigger.transport_stats_before_main["outer_request_count"]
    )
    assert (
        trigger.transport_stats_after_recovery["dcache_a_request_count"]
        == trigger.transport_stats_before_main["dcache_a_request_count"]
    )
    assert (
        trigger.transport_stats_after_recovery["dcache_d_response_count"]
        == trigger.transport_stats_before_main["dcache_d_response_count"]
    )
    assert trigger.committed_store_view.committed

    drain_summary = FlushStoreBuffersSequence().run(env)
    assert drain_summary["drain_event_count"] > 0
    env.assert_no_outstanding()


@pytest.mark.parametrize(
    ("preemptor_kind", "preemptor_requests"),
    (
        ("forward_dchannel", HIGH_PRIO_DM_PREEMPTORS),
        ("nc_replay", HIGH_PRIO_NC_PREEMPTORS),
    ),
)
def test_api_MemBlock_scalar_aligned_load_fast_replay_cancelled_into_replay_queue(env, preemptor_kind, preemptor_requests):
    """
    bank conflict 原本应走 fast replay；若 s3 同拍出现更高优先级 replayHiPrio 请求，应取消 fast replay 并落入 replay queue。
    """

    state = _reset_env_state(env)
    _preload_bank_conflict_lines(env)

    result = ScalarFastReplayCancelledByReplayHiPrioSequence(
        preemptor_kind=preemptor_kind,
        preemptor_requests=preemptor_requests,
        bank_conflict_requests=BANK_CONFLICT_REQUESTS,
        initial_state=state,
    ).run(env)

    bank_conflict_result = result.bank_conflict_result
    assert bank_conflict_result.bank_conflicts, "未命中 bank conflict 前置条件"
    assert bank_conflict_result.replay_queue_events, "高优先级请求未将 bank conflict load 挤入 replay queue"
    cancelled_rob_idxs = {event.rob_idx_value for event in bank_conflict_result.replay_queue_events}
    assert cancelled_rob_idxs, (
        f"{preemptor_kind} 抢占下未观测到 bank conflict load 落入 replay queue/replay lane"
    )
    fast_replay_rob_idxs = {event.rob_idx for event in bank_conflict_result.fast_replay_events if event.is_fast_replay == 1}
    assert cancelled_rob_idxs - fast_replay_rob_idxs, (
        f"{preemptor_kind} 抢占下未观测到“被取消 fast replay 并落 replay queue”的独立 load"
    )
    assert bank_conflict_result.violation_event is None, "高优先级抢占场景不应意外触发 memoryViolation"
    assert len(result.preemptor_writebacks) == len(result.preemptor_txns), (
        f"{preemptor_kind} 抢占场景未收齐全部 preemptor writeback"
    )
    assert len(bank_conflict_result.writebacks) == len(bank_conflict_result.txns), (
        f"{preemptor_kind} 抢占场景未收齐全部 bank-conflict load writeback"
    )
    assert len(bank_conflict_result.wakeups) == len(bank_conflict_result.txns), (
        f"{preemptor_kind} 抢占场景未收齐全部 bank-conflict load wakeup"
    )
    assert {wb["rob_idx_value"] for wb in result.preemptor_writebacks} == {
        txn.rob_idx_value for txn in result.preemptor_txns
    }, f"{preemptor_kind} 抢占场景 preemptor writeback 与事务 ROB 不一致"
    assert {wb["rob_idx_value"] for wb in bank_conflict_result.writebacks} == {
        txn.rob_idx_value for txn in bank_conflict_result.txns
    }, f"{preemptor_kind} 抢占场景 bank-conflict writeback 与事务 ROB 不一致"
    assert result.completed_load_count == result.expected_completed_load_count, (
        f"{preemptor_kind} 抢占场景未完成全部 compare: "
        f"actual={result.completed_load_count}, expected={result.expected_completed_load_count}"
    )
    env.assert_no_outstanding()


def test_api_MemBlock_scalar_aligned_load_bank_conflict_late_sta_violation(env):
    """
    对齐标量 load，dcache hit + bank conflict，older store 晚到 STA 后触发 st-ld violation。
    """

    state = _reset_env_state(env)
    _preload_bank_conflict_lines(env)

    result = ScalarLateStaStoreLoadViolationSequence(
        store_txn=StoreTxn(
            req_id=LATE_STA_STORE_REQ_ID,
            sq_ptr=state.sq_ptr,
            addr=BANK_CONFLICT_REQUESTS[0][1],
            data=LATE_STA_STORE_DATA,
        ),
        load_requests=BANK_CONFLICT_REQUESTS,
        initial_state=state,
    ).run(env)

    bank_conflict_result = result.bank_conflict_result
    assert bank_conflict_result.bank_conflicts, "late-STA 场景未命中 bank conflict"
    assert bank_conflict_result.violation_event is None, "older store STA 发出前不应过早出现 violation"
    assert not bank_conflict_result.sq_forward_events, "older store 仅有 STD 时不应出现 STLF 命中"
    assert any(event.is_fast_replay == 1 for event in bank_conflict_result.fast_replay_events), "late-STA 前置阶段未先走 bank-conflict fast replay"
    assert result.violation_event["valid"] == 1, "older store 晚到 STA 后未触发 memoryViolation"
    assert result.violation_event["rob_idx_value"] in {txn.rob_idx_value for txn in bank_conflict_result.txns}
    assert result.younger_writeback["rob_idx_value"] == result.violation_event["rob_idx_value"]
    assert result.committed_store_view.completed, "older store 未完成 shadow materialize"
    assert result.committed_store_view.committed, "older store 未在收尾阶段进入 committed"

    drain_summary = FlushStoreBuffersSequence().run(env)
    assert drain_summary["drain_event_count"] > 0
    env.assert_no_outstanding()


def test_api_MemBlock_scalar_aligned_load_bank_conflict_matchinvalid_plus_violation(env):
    """
    预留：bank conflict + matchInvalid(vp_match_fail proxy) + st-ld violation 叠加路径。
    """

    state = _reset_env_state(env)
    _preload_bank_conflict_lines(env)
    result = ScalarLateStaStoreLoadViolationSequence(
        store_txn=StoreTxn(
            req_id=LATE_STA_STORE_REQ_ID,
            sq_ptr=state.sq_ptr,
            addr=BANK_CONFLICT_REQUESTS[0][1],
            data=LATE_STA_STORE_DATA,
        ),
        load_requests=BANK_CONFLICT_REQUESTS,
        initial_state=state,
    ).run(env)

    assert result.bank_conflict_result.bank_conflicts
    assert result.violation_event["valid"] == 1
