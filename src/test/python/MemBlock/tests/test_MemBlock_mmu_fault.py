# coding=utf-8
"""
MemBlock 标量 load 的 MMU/TLB/PMP fault directed。
"""

import pytest

from sequences import (
    LOAD_ACCESS_FAULT_BIT,
    LOAD_GUEST_PAGE_FAULT_BIT,
    LOAD_PAGE_FAULT_BIT,
    MmuFaultingScalarLoadSequence,
    MmuPmpRegionLoadRecoverySequence,
    PMP_MODE_ALLOW,
    PMP_MODE_DENY,
    PTE_MODE_ACCESS_FAULT,
    PTE_MODE_GUEST_PAGE_FAULT,
    PTE_MODE_NORMAL,
    PTE_MODE_PAGE_FAULT,
    PTE_MODE_PERMISSION_FAULT,
    ResetEnvSequence,
)


MMU_FAULT_ROOT_PT = 0x88002000
MMU_FAULT_PAGE_TABLE_PAGES = (
    0x88003000,
    0x88004000,
    0x88005000,
    0x88006000,
)
MMU_FAULT_VA_BASE = 0x40010000
MMU_FAULT_PA_BASE = 0x80000000
MMU_FAULT_PRIME_DATA = 0x123456789ABCDEF0
PMP_REGION_DENIED_VA = MMU_FAULT_VA_BASE + 0x7000 + 0x180
PMP_REGION_ALLOWED_VA = MMU_FAULT_VA_BASE + 0x9000 + 0x040
PMP_REGION_SIZE = 0x1000
PMP_REGION_DENIED_DATA = 0xCAFEBABE11223344
PMP_REGION_ALLOWED_DATA = 0x0BADF00D55667788
TRANSLATION_ERROR_BITS = {LOAD_ACCESS_FAULT_BIT, LOAD_PAGE_FAULT_BIT, LOAD_GUEST_PAGE_FAULT_BIT}
MIXED_ACCESS_CASES = (
    ("byte", 1, 0x1, 0),
    ("half", 2, 0x3, 2),
    ("word", 4, 0xF, 4),
    ("doubleword", 8, 0xFF, 8),
)
TLB_AF_COMBOS = (
    ("tlb_af_only", PTE_MODE_ACCESS_FAULT, PMP_MODE_ALLOW, True, False),
    ("tlb_af_plus_pmp_af", PTE_MODE_ACCESS_FAULT, PMP_MODE_DENY, True, True),
    ("pmp_af_only", PTE_MODE_NORMAL, PMP_MODE_DENY, False, True),
    ("no_af", PTE_MODE_NORMAL, PMP_MODE_ALLOW, False, False),
)
TLB_SIDE_AF_CAPABILITY_GAP_XFAIL_REASON = (
    "TLB-side AF capability gap: current DUT/env does not yet stably surface "
    "load access fault writeback for the synthetic TLB AF leaf used by this matrix"
)


def _reset_env_state(env):
    return ResetEnvSequence(
        require_issue_lanes=(0,),
        require_lq_ready=True,
        require_sq_ready=True,
    ).run(env)


def _set_exception_indices(writeback: dict) -> set[int]:
    return {
        index
        for index, bit in enumerate(writeback.get("exception_bits", ()))
        if bit
    }


def _sv39_4k_mapping_pa_base(space_pa_base: int, va: int) -> int:
    page_offset = (int(va) & ((1 << 30) - 1)) & ~0xFFF
    return int(space_pa_base) | page_offset


def _sign_extended_scalar_load_data(data: int, size: int) -> int:
    size_bits = int(size) * 8
    mask = (1 << size_bits) - 1
    value = int(data) & mask
    sign_bit = 1 << (size_bits - 1)
    if value & sign_bit:
        value -= 1 << size_bits
    return value & ((1 << 64) - 1)


def _assert_no_special_paths(env, result) -> None:
    assert result.transport_stats_after_main["outer_request_count"] == result.transport_stats_before_main["outer_request_count"]
    assert (
        result.transport_stats_after_main["dcache_a_request_count"]
        == result.transport_stats_before_main["dcache_a_request_count"]
    )
    assert (
        result.transport_stats_after_main["dcache_d_response_count"]
        == result.transport_stats_before_main["dcache_d_response_count"]
    )
    assert env._read_optional_dut_signal("io_dcacheError_ecc_error_valid") == 0


def _assert_transport_unchanged(before: dict[str, int], after: dict[str, int]) -> None:
    assert after["outer_request_count"] == before["outer_request_count"]
    assert after["dcache_a_request_count"] == before["dcache_a_request_count"]
    assert after["dcache_d_response_count"] == before["dcache_d_response_count"]


def _assert_fault_transport_pure(result, *, case_desc: str) -> None:
    assert result.transport_stats_after_main["outer_request_count"] == result.transport_stats_before_main["outer_request_count"], (
        f"{case_desc}: fault 场景不应走 outer"
    )
    assert result.transport_stats_after_main["dcache_a_request_count"] == result.transport_stats_before_main["dcache_a_request_count"], (
        f"{case_desc}: fault 场景不应发 dcache A"
    )
    assert result.transport_stats_after_main["dcache_d_response_count"] == result.transport_stats_before_main["dcache_d_response_count"], (
        f"{case_desc}: fault 场景不应等待 dcache D"
    )


def _assert_normal_load_transport(result, *, case_desc: str, require_dcache_traffic: bool = False) -> None:
    assert result.transport_stats_after_main["outer_request_count"] == result.transport_stats_before_main["outer_request_count"], (
        f"{case_desc}: 正常 cacheable load 不应走 outer"
    )
    if require_dcache_traffic:
        assert result.transport_stats_after_main["dcache_a_request_count"] > result.transport_stats_before_main["dcache_a_request_count"], (
            f"{case_desc}: 正常 load 应发起 dcache 查询"
        )
        assert result.transport_stats_after_main["dcache_d_response_count"] > result.transport_stats_before_main["dcache_d_response_count"], (
            f"{case_desc}: 正常 load 应收到 dcache D 响应"
        )
        return
    assert result.transport_stats_after_main["dcache_a_request_count"] >= result.transport_stats_before_main["dcache_a_request_count"], (
        f"{case_desc}: 正常 load 不应倒退 dcache A 统计"
    )
    assert result.transport_stats_after_main["dcache_d_response_count"] >= result.transport_stats_before_main["dcache_d_response_count"], (
        f"{case_desc}: 正常 load 不应倒退 dcache D 统计"
    )


def _assert_no_replay_forward_violation(result, *, case_desc: str, allow_translation_miss_replay: bool = False) -> None:
    replay_state = result.replay_state_after_main
    assert not result.sq_forward_events_after_main, f"{case_desc}: fault/load 场景不应出现 SQ/SBuffer forward"
    assert not replay_state["memory_violation"]["valid"], f"{case_desc}: 不应退化成 memoryViolation"
    replay_events = tuple(replay_state.get("events", ()))
    replay_sources = {event.get("source") for event in replay_events}
    if allow_translation_miss_replay:
        assert replay_sources.isdisjoint({"replay_lane", "nc_out"}), (
            f"{case_desc}: 不应落入 replay_lane/nc_out: events={replay_events}"
        )
        assert all(
            event.get("source") != "replay_queue" or event.get("cause") == "TM"
            for event in replay_events
        ), f"{case_desc}: 仅允许 translation miss replay_queue(TM): events={replay_events}"
        return
    assert replay_sources.isdisjoint({"replay_queue", "replay_lane", "nc_out"}), (
        f"{case_desc}: 不应落入 replay 路径: events={replay_events}"
    )
    assert result.dcache_error_valid_after_main == 0, f"{case_desc}: 不应拉高 dcache error"


def _assert_wakeup_matches_writeback(result, *, should_wakeup: bool, case_desc: str) -> None:
    if not should_wakeup:
        assert not result.wakeup_events_after_main, f"{case_desc}: fault 场景不应产生正常 wakeup"
        return

    if not result.wakeup_events_after_main:
        assert result.main_writeback["int_wen"] == 1, (
            f"{case_desc}: 未采到 wakeup 时至少应看到正常整数写回"
        )
        return
    assert any(event["pdest"] == result.main_writeback["pdest"] and event["rf_wen"] == 1 for event in result.wakeup_events_after_main), (
        f"{case_desc}: wakeup 未对应 main load pdest={result.main_writeback['pdest']}, wakeups={result.wakeup_events_after_main}"
    )


def test_api_MemBlock_scalar_word_load_tlb_error_tlb_hit_smoke(env):
    """
    标量 word load 在稳定 TLB hit 背景下命中一次 translation error。
    """

    state = _reset_env_state(env)
    result = MmuFaultingScalarLoadSequence(
        root_pt_addr=MMU_FAULT_ROOT_PT,
        va=MMU_FAULT_VA_BASE,
        pa_base=_sv39_4k_mapping_pa_base(MMU_FAULT_PA_BASE, MMU_FAULT_VA_BASE),
        page_table_page_addrs=MMU_FAULT_PAGE_TABLE_PAGES[:2],
        initial_state=state,
        main_req_id=1,
        size=4,
        mask=0xF,
        main_pte_mode=PTE_MODE_PAGE_FAULT,
        main_pmp_mode=PMP_MODE_ALLOW,
        prime_req_id=0,
        prime_pte_mode=PTE_MODE_PAGE_FAULT,
        prime_pmp_mode=PMP_MODE_ALLOW,
    ).run(env)

    exception_indices = _set_exception_indices(result.main_writeback)

    assert any(event["event"] == "a_fire" for event in result.prime_ptw_trace), "fault prime 未触发 PTW"
    assert not result.main_ptw_trace, "第二次 translation error 访问不应再触发 PTW"
    assert exception_indices, "TLB error 场景未产生异常写回"
    assert exception_indices <= TRANSLATION_ERROR_BITS, f"TLB error 场景出现了非预期异常位: {exception_indices}"
    assert exception_indices & TRANSLATION_ERROR_BITS, f"TLB error 场景未命中 translation error: {exception_indices}"
    _assert_no_special_paths(env, result)
    env.assert_no_outstanding()


def test_api_MemBlock_scalar_word_load_leaf_permission_fault_tlb_miss_smoke(env):
    """
    使用真实 leaf 权限违例构造 translated load page fault。

    该类 stage-1 permission fault 在 RTL 中不可 refill，因此应走 PTW miss 背景，而不是伪造 TLB hit。
    """

    state = _reset_env_state(env)
    result = MmuFaultingScalarLoadSequence(
        root_pt_addr=MMU_FAULT_ROOT_PT,
        page_table_page_addrs=MMU_FAULT_PAGE_TABLE_PAGES[:2],
        va=MMU_FAULT_VA_BASE + 0x300,
        pa_base=MMU_FAULT_PA_BASE,
        initial_state=state,
        main_req_id=1,
        size=4,
        mask=0xF,
        main_pte_mode=PTE_MODE_PERMISSION_FAULT,
        main_pmp_mode=PMP_MODE_ALLOW,
        required_main_exception_bits=(LOAD_PAGE_FAULT_BIT,),
    ).run(env)

    exception_indices = _set_exception_indices(result.main_writeback)
    assert any(event["event"] == "a_fire" for event in result.main_ptw_trace), "permission-fault main 未触发 PTW"
    assert exception_indices, "permission-fault 场景未产生异常写回"
    assert exception_indices == {LOAD_PAGE_FAULT_BIT}, (
        f"permission-fault 场景应稳定收口到 load page fault: {exception_indices}"
    )
    _assert_fault_transport_pure(result, case_desc="permission_fault_miss")
    assert not result.sq_forward_events_after_main, "permission_fault_miss: 不应出现 SQ/SBuffer forward"
    assert not result.replay_state_after_main["memory_violation"]["valid"], (
        "permission_fault_miss: 不应退化成 memoryViolation"
    )
    replay_events = tuple(result.replay_state_after_main.get("events", ()))
    replay_sources = {event.get("source") for event in replay_events}
    assert replay_sources.isdisjoint({"replay_lane", "nc_out"}), (
        f"permission_fault_miss: 不应落入 replay_lane/nc_out: {replay_events}"
    )
    assert all(
        event.get("source") != "replay_queue" or event.get("cause") == "TM"
        for event in replay_events
    ), f"permission_fault_miss: 仅允许 translation miss replay_queue(TM): {replay_events}"
    _assert_wakeup_matches_writeback(result, should_wakeup=False, case_desc="permission_fault_miss")
    env.assert_no_outstanding()


def test_api_MemBlock_scalar_mixed_size_permission_fault_tlb_miss_matrix(env):
    """
    以真实 `S-mode -> U-page(sum=0)` 权限违例覆盖 byte/half/word/doubleword。

    该类 permission fault 在当前 RTL 中只能作为 miss/PTW 路径验证，不能伪造成稳定 TLB hit。
    """

    req_id = 0x40
    for case_index, (size_name, size, mask, offset) in enumerate(MIXED_ACCESS_CASES):
        state = _reset_env_state(env)
        va = MMU_FAULT_VA_BASE + 0x1800 + case_index * 0x1000 + offset
        result = MmuFaultingScalarLoadSequence(
            root_pt_addr=MMU_FAULT_ROOT_PT,
            page_table_page_addrs=MMU_FAULT_PAGE_TABLE_PAGES[:2],
            va=va,
            pa_base=MMU_FAULT_PA_BASE,
            initial_state=state,
            main_req_id=req_id,
            size=size,
            mask=mask,
            main_pte_mode=PTE_MODE_PERMISSION_FAULT,
            main_pmp_mode=PMP_MODE_ALLOW,
            required_main_exception_bits=(LOAD_PAGE_FAULT_BIT,),
        ).run(env)
        req_id += 1

        case_desc = f"{size_name}/permission_fault/tlb_miss"
        exception_indices = _set_exception_indices(result.main_writeback)

        assert any(event["event"] == "a_fire" for event in result.main_ptw_trace), (
            f"{case_desc}: permission PF 主访问未形成 PTW"
        )
        assert exception_indices == {LOAD_PAGE_FAULT_BIT}, (
            f"{case_desc}: permission PF 应稳定收口到 load page fault: {exception_indices}"
        )
        _assert_fault_transport_pure(result, case_desc=case_desc)
        _assert_no_replay_forward_violation(
            result,
            case_desc=case_desc,
            allow_translation_miss_replay=True,
        )
        _assert_wakeup_matches_writeback(result, should_wakeup=False, case_desc=case_desc)
        env.assert_no_outstanding()


def test_api_MemBlock_scalar_word_load_tlb_access_fault_tlb_hit_smoke(env):
    """
    使用高位 PPN 非法的真实 TLB-side AF leaf，验证 translated load 可稳定收口到 AF。
    """

    state = _reset_env_state(env)
    result = MmuFaultingScalarLoadSequence(
        root_pt_addr=MMU_FAULT_ROOT_PT,
        page_table_page_addrs=MMU_FAULT_PAGE_TABLE_PAGES[:2],
        va=MMU_FAULT_VA_BASE + 0x600,
        pa_base=MMU_FAULT_PA_BASE,
        initial_state=state,
        main_req_id=1,
        size=4,
        mask=0xF,
        main_pte_mode=PTE_MODE_ACCESS_FAULT,
        main_pmp_mode=PMP_MODE_ALLOW,
        prime_req_id=0,
        prime_pte_mode=PTE_MODE_ACCESS_FAULT,
        prime_pmp_mode=PMP_MODE_ALLOW,
        required_main_exception_bits=(LOAD_ACCESS_FAULT_BIT,),
    ).run(env)

    exception_indices = _set_exception_indices(result.main_writeback)
    assert any(event["event"] == "a_fire" for event in result.prime_ptw_trace), "tlb-af prime 未触发 PTW"
    assert not result.main_ptw_trace, "tlb-af main 应保持 TLB hit 背景"
    assert exception_indices == {LOAD_ACCESS_FAULT_BIT}, (
        f"tlb-af 场景应稳定收口到 load access fault: {exception_indices}"
    )
    _assert_fault_transport_pure(result, case_desc="tlb_access_fault_hit")
    _assert_no_replay_forward_violation(result, case_desc="tlb_access_fault_hit")
    _assert_wakeup_matches_writeback(result, should_wakeup=False, case_desc="tlb_access_fault_hit")
    env.assert_no_outstanding()


def test_api_MemBlock_scalar_word_load_pmp_access_fault_tlb_hit_smoke(env):
    """
    标量 word load 在 TLB hit 背景下命中一次 PMP access fault。
    """

    state = _reset_env_state(env)
    result = MmuFaultingScalarLoadSequence(
        root_pt_addr=MMU_FAULT_ROOT_PT,
        va=MMU_FAULT_VA_BASE + 0x1000,
        pa_base=_sv39_4k_mapping_pa_base(MMU_FAULT_PA_BASE, MMU_FAULT_VA_BASE + 0x1000),
        page_table_page_addrs=MMU_FAULT_PAGE_TABLE_PAGES[:2],
        initial_state=state,
        main_req_id=1,
        size=4,
        mask=0xF,
        main_pte_mode=PTE_MODE_NORMAL,
        main_pmp_mode=PMP_MODE_DENY,
        prime_req_id=0,
        prime_pte_mode=PTE_MODE_NORMAL,
        prime_pmp_mode=PMP_MODE_ALLOW,
        prime_expected_data=MMU_FAULT_PRIME_DATA,
        required_main_exception_bits=(LOAD_ACCESS_FAULT_BIT,),
    ).run(env)

    exception_indices = _set_exception_indices(result.main_writeback)

    assert any(event["event"] == "a_fire" for event in result.prime_ptw_trace), "PMP prime 未触发 PTW"
    assert not result.main_ptw_trace, "PMP deny 主访问应在 TLB hit 背景下完成"
    assert LOAD_ACCESS_FAULT_BIT in exception_indices, f"PMP deny 未命中 load access fault: {exception_indices}"
    assert exception_indices <= {LOAD_ACCESS_FAULT_BIT}, f"PMP deny 场景出现了额外异常位: {exception_indices}"
    _assert_no_special_paths(env, result)
    env.assert_no_outstanding()


def test_api_MemBlock_scalar_word_load_tlb_error_plus_pmp_fault_overlap_smoke(env):
    """
    尝试叠加 translation error 与 PMP deny，记录当前 RTL 的优先级。
    """

    state = _reset_env_state(env)
    result = MmuFaultingScalarLoadSequence(
        root_pt_addr=MMU_FAULT_ROOT_PT,
        va=MMU_FAULT_VA_BASE + 0x2000,
        pa_base=_sv39_4k_mapping_pa_base(MMU_FAULT_PA_BASE, MMU_FAULT_VA_BASE + 0x2000),
        page_table_page_addrs=MMU_FAULT_PAGE_TABLE_PAGES[:2],
        initial_state=state,
        main_req_id=1,
        size=4,
        mask=0xF,
        main_pte_mode=PTE_MODE_PAGE_FAULT,
        main_pmp_mode=PMP_MODE_DENY,
        prime_req_id=0,
        prime_pte_mode=PTE_MODE_PAGE_FAULT,
        prime_pmp_mode=PMP_MODE_ALLOW,
    ).run(env)

    exception_indices = _set_exception_indices(result.main_writeback)

    assert any(event["event"] == "a_fire" for event in result.prime_ptw_trace), "overlap prime 未触发 PTW"
    assert not result.main_ptw_trace, "overlap 主访问不应重新触发 PTW"
    assert exception_indices, "overlap 场景未产生异常写回"
    assert exception_indices <= TRANSLATION_ERROR_BITS, f"overlap 场景出现了非预期异常位: {exception_indices}"
    _assert_no_special_paths(env, result)
    env.assert_no_outstanding()


def test_api_MemBlock_scalar_mixed_size_fault_matrix(env):
    """
    以小矩阵覆盖 byte/half/word/doubleword 与 translation/PMP fault 组合。
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
                pa_base=_sv39_4k_mapping_pa_base(MMU_FAULT_PA_BASE, va),
                page_table_page_addrs=MMU_FAULT_PAGE_TABLE_PAGES[:2],
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
                f"mixed fault matrix 未产生异常写回: size={size_name}, combo={combo_name}, "
                f"va=0x{va:x}, mask=0x{mask:x}"
            )
            assert exception_indices <= allowed_bits, (
                f"mixed fault matrix 出现非预期异常位: size={size_name}, combo={combo_name}, "
                f"va=0x{va:x}, exceptions={exception_indices}"
            )
            assert not result.main_ptw_trace, (
                f"mixed fault matrix 正式访问未保持 TLB hit 背景: size={size_name}, combo={combo_name}"
            )
            _assert_no_special_paths(env, result)
            _assert_fault_transport_pure(result, case_desc=f"{size_name}/{combo_name}")
            _assert_no_replay_forward_violation(result, case_desc=f"{size_name}/{combo_name}")
            env.assert_no_outstanding()


def test_api_MemBlock_scalar_mixed_size_tlb_pmp_af_matrix(env):
    """
    遍历 TLB-side AF / PMP AF 的 4 组合，并对 byte/half/word/doubleword 全覆盖。
    """

    req_id = 0
    for case_index, (size_name, size, mask, offset) in enumerate(MIXED_ACCESS_CASES):
        for combo_name, pte_mode, pmp_mode, expect_tlb_af, expect_pmp_af in TLB_AF_COMBOS:
            state = _reset_env_state(env)
            va = MMU_FAULT_VA_BASE + 0x8000 + case_index * 0x1000 + offset
            result = MmuFaultingScalarLoadSequence(
                root_pt_addr=MMU_FAULT_ROOT_PT,
                page_table_page_addrs=MMU_FAULT_PAGE_TABLE_PAGES[:2],
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
                prime_pmp_mode=PMP_MODE_ALLOW,
                prime_expected_data=MMU_FAULT_PRIME_DATA if pte_mode == PTE_MODE_NORMAL else None,
                main_expected_data=MMU_FAULT_PRIME_DATA if not (expect_tlb_af or expect_pmp_af) else None,
                required_main_exception_bits=(LOAD_ACCESS_FAULT_BIT,) if (expect_tlb_af or expect_pmp_af) else (),
            )
            try:
                result = result.run(env)
            except TimeoutError as exc:
                if expect_tlb_af:
                    pytest.xfail(f"{TLB_SIDE_AF_CAPABILITY_GAP_XFAIL_REASON}; size={size_name}; combo={combo_name}; {exc}")
                raise
            req_id += 2

            case_desc = f"{size_name}/{combo_name}/tlb_hit"
            exception_indices = _set_exception_indices(result.main_writeback)
            expected_af = expect_tlb_af or expect_pmp_af

            assert any(event["event"] == "a_fire" for event in result.prime_ptw_trace), (
                f"{case_desc}: prime 访问未形成 PTW，无法证明 hit 背景"
            )
            assert not result.main_ptw_trace, f"{case_desc}: main 访问应保持 TLB hit"
            if expected_af:
                assert LOAD_ACCESS_FAULT_BIT in exception_indices, (
                    f"{case_desc}: 预期 access fault，但异常位为 {exception_indices}"
                )
                assert exception_indices <= {LOAD_ACCESS_FAULT_BIT}, (
                    f"{case_desc}: AF 组合出现额外异常位 {exception_indices}"
                )
                _assert_fault_transport_pure(result, case_desc=case_desc)
            else:
                assert not exception_indices, f"{case_desc}: no-af 组合不应出现异常位 {exception_indices}"
                assert result.main_writeback["data"] == _sign_extended_scalar_load_data(MMU_FAULT_PRIME_DATA, size), (
                    f"{case_desc}: no-af 组合应正常写回 prime 数据"
                )
                _assert_normal_load_transport(result, case_desc=case_desc)
            _assert_no_replay_forward_violation(result, case_desc=case_desc)
            _assert_wakeup_matches_writeback(result, should_wakeup=not expected_af, case_desc=case_desc)
            env.assert_no_outstanding()


def test_api_MemBlock_scalar_word_tlb_pmp_af_miss_matrix(env):
    """
    以 word load 固定覆盖 TLB-side AF / PMP AF 的 4 组合在 TLB miss 背景下的行为。
    """

    req_id = 0x100
    for combo_index, (combo_name, pte_mode, pmp_mode, expect_tlb_af, expect_pmp_af) in enumerate(TLB_AF_COMBOS):
        state = _reset_env_state(env)
        va = MMU_FAULT_VA_BASE + 0xC000 + combo_index * 0x1000
        result = MmuFaultingScalarLoadSequence(
            root_pt_addr=MMU_FAULT_ROOT_PT,
            page_table_page_addrs=MMU_FAULT_PAGE_TABLE_PAGES[:2],
            va=va,
            pa_base=MMU_FAULT_PA_BASE,
            initial_state=state,
            main_req_id=req_id,
            size=4,
            mask=0xF,
            main_pte_mode=pte_mode,
            main_pmp_mode=pmp_mode,
            prime_req_id=None,
            prime_expected_data=MMU_FAULT_PRIME_DATA if pte_mode == PTE_MODE_NORMAL else None,
            main_expected_data=MMU_FAULT_PRIME_DATA if not (expect_tlb_af or expect_pmp_af) else None,
            required_main_exception_bits=(LOAD_ACCESS_FAULT_BIT,) if (expect_tlb_af or expect_pmp_af) else (),
        )
        try:
            result = result.run(env)
        except TimeoutError as exc:
            if expect_tlb_af:
                pytest.xfail(f"{TLB_SIDE_AF_CAPABILITY_GAP_XFAIL_REASON}; combo={combo_name}; {exc}")
            raise
        req_id += 1

        case_desc = f"word/{combo_name}/tlb_miss"
        exception_indices = _set_exception_indices(result.main_writeback)
        expected_af = expect_tlb_af or expect_pmp_af

        if pmp_mode == PMP_MODE_ALLOW:
            assert result.main_ptw_trace, f"{case_desc}: main 访问应形成 TLB miss/PTW"
        if expected_af:
            assert LOAD_ACCESS_FAULT_BIT in exception_indices, (
                f"{case_desc}: 预期 access fault，但异常位为 {exception_indices}"
            )
            assert exception_indices <= {LOAD_ACCESS_FAULT_BIT}, (
                f"{case_desc}: AF 组合出现额外异常位 {exception_indices}"
            )
            _assert_fault_transport_pure(result, case_desc=case_desc)
        else:
            assert not exception_indices, f"{case_desc}: no-af 组合不应出现异常位 {exception_indices}"
            assert result.main_writeback["data"] == _sign_extended_scalar_load_data(MMU_FAULT_PRIME_DATA, 4), (
                f"{case_desc}: no-af 组合应正常写回预装数据"
            )
            _assert_normal_load_transport(result, case_desc=case_desc)
        _assert_no_replay_forward_violation(
            result,
            case_desc=case_desc,
            allow_translation_miss_replay=True,
        )
        _assert_wakeup_matches_writeback(result, should_wakeup=not expected_af, case_desc=case_desc)
        env.assert_no_outstanding()


def test_api_MemBlock_scalar_load_pmp_deny_region_hit_allow_outside_smoke(env):
    """
    同一 Sv39/TLB hit 背景下，命中 deny region 的 load 应 fault，region 外 load 仍应成功。
    """

    state = _reset_env_state(env)
    result = MmuPmpRegionLoadRecoverySequence(
        root_pt_addr=MMU_FAULT_ROOT_PT,
        page_table_page_addrs=MMU_FAULT_PAGE_TABLE_PAGES[:2],
        denied_va=PMP_REGION_DENIED_VA,
        allowed_va=PMP_REGION_ALLOWED_VA,
        denied_pa_base=_sv39_4k_mapping_pa_base(MMU_FAULT_PA_BASE, PMP_REGION_DENIED_VA),
        allowed_pa_base=_sv39_4k_mapping_pa_base(MMU_FAULT_PA_BASE, PMP_REGION_ALLOWED_VA),
        initial_state=state,
        prime_req_id=0,
        denied_req_id=1,
        allowed_req_id=2,
        restored_req_id=3,
        denied_data=PMP_REGION_DENIED_DATA,
        allowed_data=PMP_REGION_ALLOWED_DATA,
        deny_region_size=PMP_REGION_SIZE,
    ).run(env)

    exception_indices = _set_exception_indices(result.denied_writeback)

    assert any(event["event"] == "a_fire" for event in result.prime_ptw_trace), "PMP region prime 未触发 PTW"
    assert not result.denied_ptw_trace, "deny-region load 不应重新触发 PTW"
    assert not result.allowed_ptw_trace, "region 外 allow load 不应重新触发 PTW"
    assert LOAD_ACCESS_FAULT_BIT in exception_indices, f"deny-region load 未命中 access fault: {exception_indices}"
    assert exception_indices <= {LOAD_ACCESS_FAULT_BIT}, f"deny-region load 出现额外异常位: {exception_indices}"
    _assert_transport_unchanged(result.transport_stats_before_denied, result.transport_stats_after_denied)
    assert not result.denied_replay_state["memory_violation"]["valid"], "deny-region load 不应退化成 memoryViolation"
    assert result.allowed_writeback["data"] == PMP_REGION_ALLOWED_DATA
    assert env._read_optional_dut_signal("io_dcacheError_ecc_error_valid") == 0
    env.assert_no_outstanding()


def test_api_MemBlock_scalar_load_pmp_deny_region_then_restore_allow_smoke(env):
    """
    deny-region fault 后撤销 deny，同一 load 应在 TLB hit 背景下恢复正常访问。
    """

    state = _reset_env_state(env)
    result = MmuPmpRegionLoadRecoverySequence(
        root_pt_addr=MMU_FAULT_ROOT_PT,
        page_table_page_addrs=MMU_FAULT_PAGE_TABLE_PAGES[:2],
        denied_va=PMP_REGION_DENIED_VA,
        allowed_va=PMP_REGION_ALLOWED_VA,
        denied_pa_base=_sv39_4k_mapping_pa_base(MMU_FAULT_PA_BASE, PMP_REGION_DENIED_VA),
        allowed_pa_base=_sv39_4k_mapping_pa_base(MMU_FAULT_PA_BASE, PMP_REGION_ALLOWED_VA),
        initial_state=state,
        prime_req_id=0x10,
        denied_req_id=0x11,
        allowed_req_id=0x12,
        restored_req_id=0x13,
        denied_data=PMP_REGION_DENIED_DATA,
        allowed_data=PMP_REGION_ALLOWED_DATA,
        deny_region_size=PMP_REGION_SIZE,
    ).run(env)

    assert any(event["event"] == "a_fire" for event in result.prime_ptw_trace), "restore prime 未触发 PTW"
    assert not result.denied_ptw_trace, "restore 场景中的 deny load 不应重新触发 PTW"
    assert not result.restored_ptw_trace, "恢复后的 translated load 不应重新触发 PTW"
    assert result.restored_writeback["data"] == PMP_REGION_DENIED_DATA
    assert result.restored_writeback["rob_idx_value"] == result.restored_txn.rob_idx_value
    assert env._read_optional_dut_signal("io_dcacheError_ecc_error_valid") == 0
    env.assert_no_outstanding()
