# coding=utf-8
"""
MemBlock 标量 load 的 MMU/TLB/PMP fault directed。
"""

from sequences import (
    LOAD_ACCESS_FAULT_BIT,
    LOAD_GUEST_PAGE_FAULT_BIT,
    LOAD_PAGE_FAULT_BIT,
    MmuFaultingScalarLoadSequence,
    MmuPmpRegionLoadRecoverySequence,
    PMP_MODE_ALLOW,
    PMP_MODE_DENY,
    PTE_MODE_NORMAL,
    PTE_MODE_PAGE_FAULT,
    ResetEnvSequence,
)


MMU_FAULT_ROOT_PT = 0x88002000
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


def test_api_MemBlock_scalar_word_load_tlb_error_tlb_hit_smoke(env):
    """
    标量 word load 在稳定 TLB hit 背景下命中一次 translation error。
    """

    state = _reset_env_state(env)
    result = MmuFaultingScalarLoadSequence(
        root_pt_addr=MMU_FAULT_ROOT_PT,
        va=MMU_FAULT_VA_BASE,
        pa_base=MMU_FAULT_PA_BASE,
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


def test_api_MemBlock_scalar_word_load_pmp_access_fault_tlb_hit_smoke(env):
    """
    标量 word load 在 TLB hit 背景下命中一次 PMP access fault。
    """

    state = _reset_env_state(env)
    result = MmuFaultingScalarLoadSequence(
        root_pt_addr=MMU_FAULT_ROOT_PT,
        va=MMU_FAULT_VA_BASE + 0x1000,
        pa_base=MMU_FAULT_PA_BASE,
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
        pa_base=MMU_FAULT_PA_BASE,
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
            env.assert_no_outstanding()


def test_api_MemBlock_scalar_load_pmp_deny_region_hit_allow_outside_smoke(env):
    """
    同一 Sv39/TLB hit 背景下，命中 deny region 的 load 应 fault，region 外 load 仍应成功。
    """

    state = _reset_env_state(env)
    result = MmuPmpRegionLoadRecoverySequence(
        root_pt_addr=MMU_FAULT_ROOT_PT,
        denied_va=PMP_REGION_DENIED_VA,
        allowed_va=PMP_REGION_ALLOWED_VA,
        pa_base=MMU_FAULT_PA_BASE,
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
        denied_va=PMP_REGION_DENIED_VA,
        allowed_va=PMP_REGION_ALLOWED_VA,
        pa_base=MMU_FAULT_PA_BASE,
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
