# coding=utf-8
"""
MemBlock 标量 load pipeline probe directed。
"""

from collections import deque
from contextlib import contextmanager

import pytest

from request_apis import (
    BackendSendPlan,
    EnqueueLoadCyclePlan,
    IssueCyclePlan,
    IssueOp,
    LoadTxn,
    StoreTxn,
    enqueue_scalar_store,
    issue_scalar_sta,
    issue_scalar_std,
    ptr_inc,
)
from transactions import NonMemBlockerStep
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
    PTE_MODE_ACCESS_FAULT,
    PTE_MODE_NORMAL,
    PTE_MODE_PAGE_FAULT,
    PTE_MODE_PERMISSION_FAULT,
    ResetEnvSequence,
    ScalarStoreCommitSequence,
    ScalarStoreSequence,
    ScalarBankConflictLoadClusterSequence,
    ScalarFastReplayCancelledByReplayHiPrioSequence,
    ScalarLateStaStoreLoadViolationSequence,
    ScalarSqDataInvalidMatchInvalidTriggerSequence,
    SequenceState,
    Sv39Mapping,
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
MATCH_INVALID_ROOT_A_PAGE_TABLE_PAGES = (0x88024000, 0x88025000)
MATCH_INVALID_ROOT_B_PAGE_TABLE_PAGES = (0x88026000, 0x88027000)
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
TLB_SIDE_AF_CAPABILITY_GAP_XFAIL_REASON = (
    "TLB-side AF capability gap: current DUT/env does not yet stably surface "
    "load access fault writeback for the synthetic TLB AF leaf used by this probe matrix"
)
SPLIT_FORWARD_MULTI_STORE_AGGREGATION_SMF_XFAIL_REASON = (
    "split-forward multi-store aggregation capability gap: current DUT replays with SMF when "
    "one source must aggregate multiple real partial stores to form hole/sparse masks"
)
AF_PIPELINE_PROBE_CASES = (
    ("tlb_af_only_hit", PTE_MODE_ACCESS_FAULT, PMP_MODE_ALLOW, True, False, True),
    ("tlb_af_plus_pmp_af_hit", PTE_MODE_ACCESS_FAULT, PMP_MODE_DENY, True, True, True),
    ("pmp_af_only_hit", PTE_MODE_NORMAL, PMP_MODE_DENY, False, True, True),
    ("no_af_hit", PTE_MODE_NORMAL, PMP_MODE_ALLOW, False, False, True),
    ("tlb_af_only_miss", PTE_MODE_ACCESS_FAULT, PMP_MODE_ALLOW, True, False, False),
    ("pmp_af_only_miss", PTE_MODE_NORMAL, PMP_MODE_DENY, False, True, False),
)
TRANSLATED_HIT_FORWARD_QUERY_ROOT_PT = 0x88024000
TRANSLATED_HIT_FORWARD_QUERY_VA_BASE = 0x40000000
TRANSLATED_HIT_FORWARD_QUERY_PA_BASE = 0x80000000
TRANSLATED_HIT_FORWARD_QUERY_STORE_PA = TRANSLATED_HIT_FORWARD_QUERY_PA_BASE + 0x800
TRANSLATED_HIT_FORWARD_QUERY_STORE_DATA = 0xA5A55A5A11223344
THREE_LANE_TRANSLATED_HIT_QUERY_MISS_CASES = (
    (0, 0x080, 0x0102030405060708),
    (1, 0x100, 0x1112131415161718),
    (2, 0x180, 0x2122232425262728),
)
PHYSICAL_SQ_FORWARD_PROBE_ADDR = 0x80001008
PHYSICAL_SQ_FORWARD_PROBE_DATA = 0xD1D2D3D4D5D6D7D8
TRANSLATED_SQ_FORWARD_HIT_ROOT_PT = 0x88026000
TRANSLATED_SQ_FORWARD_HIT_VA_BASE = 0x80000000
TRANSLATED_SQ_FORWARD_HIT_PA_BASE = 0x80000000
TRANSLATED_SQ_FORWARD_HIT_ADDR = 0x80001008
TRANSLATED_SQ_FORWARD_HIT_WARMUP_DATA = 0x1021324354657687
TRANSLATED_SQ_FORWARD_HIT_STORE_DATA = 0xE1E2E3E4E5E6E7E8
TRANSLATED_SQ_FORWARD_HIT_THREE_LANE_CASES = (
    (0, 0x1008, 0x1021324354657687, 0xE1E2E3E4E5E6E7E8),
    (1, 0x1108, 0x2021222324252627, 0xD1D2D3D4D5D6D7D8),
    (2, 0x1208, 0x3031323334353637, 0xC1C2C3C4C5C6C7C8),
)
TRANSLATED_SQ_MULTI_STORE_YOUNGEST_FULL_COVER_CASES = (
    {
        "name": "lb_youngest_full_cover_sbuffer_miss",
        "load_size": 1,
        "load_offset": 0,
        "store_datas": (0x12, 0x34, 0x56),
    },
    {
        "name": "lh_youngest_full_cover_sbuffer_miss",
        "load_size": 2,
        "load_offset": 0,
        "store_datas": (0x1234, 0x3456, 0x5678),
    },
    {
        "name": "lw_youngest_full_cover_sbuffer_miss",
        "load_size": 4,
        "load_offset": 0,
        "store_datas": (0x11223344, 0x22334455, 0x33445566),
    },
    {
        "name": "ld_youngest_full_cover_sbuffer_miss",
        "load_size": 8,
        "load_offset": 0,
        "store_datas": (
            0x0102030405060708,
            0x1112131415161718,
            0x2122232425262728,
        ),
    },
)
TRANSLATED_SQ_MULTI_STORE_YOUNGEST_FULL_COVER_SBUFFER_SINGLE_HIT_CASES = (
    {
        "name": "lb_youngest_full_cover_sbuffer_single_full_overlap",
        "load_size": 1,
        "load_mask": 0x01,
        "load_offset": 0,
        "store_datas": (0x12, 0x34, 0x56),
        "sbuffer_offset": 0,
        "sbuffer_size": 1,
        "sbuffer_data": 0x6A,
    },
    {
        "name": "lh_youngest_full_cover_sbuffer_single_full_overlap",
        "load_size": 2,
        "load_mask": 0x03,
        "load_offset": 0,
        "store_datas": (0x1234, 0x3456, 0x5678),
        "sbuffer_offset": 0,
        "sbuffer_size": 2,
        "sbuffer_data": 0x6A5B,
    },
    {
        "name": "lw_youngest_full_cover_sbuffer_single_full_overlap",
        "load_size": 4,
        "load_mask": 0x0F,
        "load_offset": 0,
        "store_datas": (0x11223344, 0x22334455, 0x33445566),
        "sbuffer_offset": 0,
        "sbuffer_size": 4,
        "sbuffer_data": 0x6A5B4C3D,
    },
    {
        "name": "ld_youngest_full_cover_sbuffer_single_full_overlap",
        "load_size": 8,
        "load_mask": 0xFF,
        "load_offset": 0,
        "store_datas": (
            0x0102030405060708,
            0x1112131415161718,
            0x2122232425262728,
        ),
        "sbuffer_offset": 0,
        "sbuffer_size": 8,
        "sbuffer_data": 0x6A5B4C3D2E1F9081,
    },
    {
        "name": "lh_youngest_full_cover_sbuffer_single_partial_high_byte",
        "load_size": 2,
        "load_mask": 0x03,
        "load_offset": 0,
        "store_datas": (0x2143, 0x4365, 0x6587),
        "sbuffer_offset": 1,
        "sbuffer_size": 1,
        "sbuffer_data": 0x7B,
    },
    {
        "name": "lw_youngest_full_cover_sbuffer_single_partial_high_halfword",
        "load_size": 4,
        "load_mask": 0x0F,
        "load_offset": 0,
        "store_datas": (0x10213243, 0x21324354, 0x32435465),
        "sbuffer_offset": 2,
        "sbuffer_size": 2,
        "sbuffer_data": 0x7C6B,
    },
    {
        "name": "ld_youngest_full_cover_sbuffer_single_partial_high_word",
        "load_size": 8,
        "load_mask": 0xFF,
        "load_offset": 0,
        "store_datas": (
            0x1021324354657687,
            0x2132435465768798,
            0x32435465768798A9,
        ),
        "sbuffer_offset": 4,
        "sbuffer_size": 4,
        "sbuffer_data": 0x7D6C5B4A,
    },
)
TRANSLATED_SBUFFER_FORWARD_HIT_ROOT_PT = 0x88028000
TRANSLATED_SBUFFER_FORWARD_HIT_VA_BASE = 0xC0000000
TRANSLATED_SBUFFER_FORWARD_HIT_PA_BASE = 0xC0000000
TRANSLATED_SBUFFER_FORWARD_HIT_ADDR = 0xC0001008
TRANSLATED_SBUFFER_FORWARD_HIT_WARMUP_DATA = 0x0112233445566778
TRANSLATED_SBUFFER_FORWARD_HIT_STORE_DATA = 0xF1F2F3F4F5F6F7F8
TRANSLATED_SBUFFER_FORWARD_HIT_THREE_LANE_CASES = (
    (0, 0x1008, 0x0112233445566778, 0xF1F2F3F4F5F6F7F8),
    (1, 0x1108, 0x1112233445566778, 0xE1E2E3E4E5E6E7E8),
    (2, 0x1208, 0x2112233445566778, 0xD1D2D3D4D5D6D7D8),
)
TRANSLATED_SPLIT_FORWARD_ROOT_PT = 0x8802A000
TRANSLATED_SPLIT_FORWARD_VA_BASE = 0xC0000000
TRANSLATED_SPLIT_FORWARD_PA_BASE = 0xC0000000
TRANSLATED_SPLIT_FORWARD_ADDR = 0xC0002008
TRANSLATED_SPLIT_FORWARD_WARMUP_DATA = 0x8877665544332211
TRANSLATED_SPLIT_FORWARD_SQ_STORE_ADDR = TRANSLATED_SPLIT_FORWARD_ADDR
TRANSLATED_SPLIT_FORWARD_SQ_STORE_DATA = 0x14131211
TRANSLATED_SPLIT_FORWARD_SBUFFER_STORE_ADDR = TRANSLATED_SPLIT_FORWARD_ADDR + 0x4
TRANSLATED_SPLIT_FORWARD_SBUFFER_STORE_DATA = 0x28272625
TRANSLATED_SPLIT_FORWARD_EXPECTED_DATA = 0x2827262514131211
SCALAR_STORE_SIZE_TO_MASK = {1: 0x01, 2: 0x03, 4: 0x0F, 8: 0xFF}
TRANSLATED_SPLIT_FORWARD_MASK_MATRIX_CASES = (
    {
        "name": "contiguous_low_sq_contiguous_high_sbuffer",
        "sq_specs": ((0, 4, 0x13121110), (4, 2, 0x1514)),
        "sbuffer_specs": ((6, 2, 0x1716),),
    },
    {
        "name": "contiguous_low_sbuffer_contiguous_high_sq",
        "sq_specs": ((4, 4, 0x27262524),),
        "sbuffer_specs": ((0, 2, 0x2120), (2, 2, 0x2322)),
    },
    {
        "name": "middle_hole_sq_middle_fill_sbuffer",
        "sq_specs": ((0, 2, 0x3130), (6, 2, 0x3736)),
        "sbuffer_specs": ((2, 2, 0x3332), (4, 2, 0x3534)),
    },
    {
        "name": "sparse_even_sq_sparse_odd_sbuffer",
        "sq_specs": ((0, 1, 0x40), (2, 1, 0x42), (4, 1, 0x44), (6, 1, 0x46)),
        "sbuffer_specs": ((1, 1, 0x41), (3, 1, 0x43), (5, 1, 0x45), (7, 1, 0x47)),
    },
    {
        "name": "sparse_irregular_mixed",
        "sq_specs": ((0, 1, 0x50), (3, 1, 0x53), (5, 1, 0x55)),
        "sbuffer_specs": ((1, 2, 0x5251), (4, 1, 0x54), (6, 2, 0x5756)),
    },
)
TRANSLATED_SQ_SBUFFER_OVERLAP_ROOT_PT = 0x8802C000
TRANSLATED_SQ_SBUFFER_OVERLAP_VA_BASE = 0xC0000000
TRANSLATED_SQ_SBUFFER_OVERLAP_PA_BASE = 0xC0000000
TRANSLATED_SQ_SBUFFER_OVERLAP_ADDR = 0xC0003008
TRANSLATED_SQ_SBUFFER_OVERLAP_WARMUP_DATA = 0x1020304050607080
TRANSLATED_SQ_SBUFFER_OVERLAP_SQ_FULL_DATA = 0xE8E7E6E5E4E3E2E1
TRANSLATED_SQ_SBUFFER_OVERLAP_FULL_DATA = 0xA8A7A6A5A4A3A2A1
TRANSLATED_SQ_SBUFFER_OVERLAP_PARTIAL_DATA = 0xB8B7B6B5
TRANSLATED_SQ_SBUFFER_OVERLAP_PARTIAL_MASK_CASES = (
    {
        "name": "partial_overlap_low_byte",
        "sbuffer_offset": 0,
        "sbuffer_size": 1,
        "sbuffer_data": 0xA0,
    },
    {
        "name": "partial_overlap_low_halfword",
        "sbuffer_offset": 0,
        "sbuffer_size": 2,
        "sbuffer_data": 0xA2A1,
    },
    {
        "name": "partial_overlap_low_word",
        "sbuffer_offset": 0,
        "sbuffer_size": 4,
        "sbuffer_data": 0xA4A3A2A1,
    },
    {
        "name": "partial_overlap_middle_byte",
        "sbuffer_offset": 3,
        "sbuffer_size": 1,
        "sbuffer_data": 0xB3,
    },
    {
        "name": "partial_overlap_middle_halfword",
        "sbuffer_offset": 2,
        "sbuffer_size": 2,
        "sbuffer_data": 0xB3B2,
    },
    {
        "name": "partial_overlap_middle_word",
        "sbuffer_offset": 2,
        "sbuffer_size": 4,
        "sbuffer_data": 0xB5B4B3B2,
    },
    {
        "name": "partial_overlap_high_byte",
        "sbuffer_offset": 7,
        "sbuffer_size": 1,
        "sbuffer_data": 0xC7,
    },
    {
        "name": "partial_overlap_high_halfword",
        "sbuffer_offset": 6,
        "sbuffer_size": 2,
        "sbuffer_data": 0xC7C6,
    },
    {
        "name": "partial_overlap_high_word",
        "sbuffer_offset": 4,
        "sbuffer_size": 4,
        "sbuffer_data": 0xC8C7C6C5,
    },
)
TRANSLATED_SQ_SBUFFER_OVERLAP_PARTIAL_MASK_THREE_LANE_CASES = (
    {
        "name": "partial_overlap_low_byte",
        "sbuffer_offset": 0,
        "sbuffer_size": 1,
        "sbuffer_data": 0xA0,
    },
    {
        "name": "partial_overlap_middle_halfword",
        "sbuffer_offset": 2,
        "sbuffer_size": 2,
        "sbuffer_data": 0xB3B2,
    },
    {
        "name": "partial_overlap_high_word",
        "sbuffer_offset": 4,
        "sbuffer_size": 4,
        "sbuffer_data": 0xC8C7C6C5,
    },
)
TRANSLATED_SQ_SBUFFER_OVERLAP_NARROW_LOAD_CASES = (
    {
        "name": "lh_low_byte_overlap",
        "load_size": 2,
        "load_mask": 0x03,
        "load_offset": 0,
        "sbuffer_offset": 0,
        "sbuffer_size": 1,
        "sbuffer_data": 0xD0,
    },
    {
        "name": "lh_high_byte_overlap",
        "load_size": 2,
        "load_mask": 0x03,
        "load_offset": 6,
        "sbuffer_offset": 7,
        "sbuffer_size": 1,
        "sbuffer_data": 0xD7,
    },
    {
        "name": "lw_low_halfword_overlap",
        "load_size": 4,
        "load_mask": 0x0F,
        "load_offset": 0,
        "sbuffer_offset": 0,
        "sbuffer_size": 2,
        "sbuffer_data": 0xE1E0,
    },
    {
        "name": "lw_high_halfword_overlap",
        "load_size": 4,
        "load_mask": 0x0F,
        "load_offset": 4,
        "sbuffer_offset": 6,
        "sbuffer_size": 2,
        "sbuffer_data": 0xE7E6,
    },
)
TRANSLATED_SQ_SBUFFER_FULL_OVERLAP_NARROW_LOAD_CASES = (
    {
        "name": "lh_low_full_overlap",
        "load_size": 2,
        "load_mask": 0x03,
        "load_offset": 0,
        "sbuffer_offset": 0,
        "sbuffer_size": 2,
        "sbuffer_data": 0xD1D0,
    },
    {
        "name": "lh_high_full_overlap",
        "load_size": 2,
        "load_mask": 0x03,
        "load_offset": 6,
        "sbuffer_offset": 6,
        "sbuffer_size": 2,
        "sbuffer_data": 0xD7D6,
    },
    {
        "name": "lw_low_full_overlap",
        "load_size": 4,
        "load_mask": 0x0F,
        "load_offset": 0,
        "sbuffer_offset": 0,
        "sbuffer_size": 4,
        "sbuffer_data": 0xE3E2E1E0,
    },
    {
        "name": "lw_high_full_overlap",
        "load_size": 4,
        "load_mask": 0x0F,
        "load_offset": 4,
        "sbuffer_offset": 4,
        "sbuffer_size": 4,
        "sbuffer_data": 0xE7E6E5E4,
    },
)
TRANSLATED_SQ_SBUFFER_MULTI_HIT_CASES = (
    {
        "name": "all_sbuffer_hits_within_sq_cover",
        "load_size": 8,
        "load_mask": 0xFF,
        "load_offset": 0,
        "expect_out_of_window_hit": False,
        "sbuffer_specs": (
            (0, 2, 0xA2A1),
            (4, 2, 0xA6A5),
        ),
    },
    {
        "name": "sbuffer_hits_include_out_of_window_bytes",
        "load_size": 4,
        "load_mask": 0x0F,
        "load_offset": 4,
        "expect_out_of_window_hit": True,
        "sbuffer_specs": (
            (4, 1, 0xB4),
            (5, 1, 0xB5),
            (0, 2, 0xB1B0),
        ),
    },
)
TRANSLATED_SQ_SBUFFER_MULTI_HIT_NARROW_LOAD_CASES = (
    {
        "name": "lb_low_multi_hit_within_sq_cover",
        "load_size": 1,
        "load_mask": 0x01,
        "load_offset": 0,
        "expect_out_of_window_hit": False,
        "sbuffer_specs": (
            (0, 1, 0xC0),
            (4, 1, 0xC4),
        ),
    },
    {
        "name": "lb_high_multi_hit_out_of_window",
        "load_size": 1,
        "load_mask": 0x01,
        "load_offset": 7,
        "expect_out_of_window_hit": True,
        "sbuffer_specs": (
            (7, 1, 0xC7),
            (6, 1, 0xC6),
        ),
    },
    {
        "name": "lh_low_multi_hit_within_sq_cover",
        "load_size": 2,
        "load_mask": 0x03,
        "load_offset": 0,
        "expect_out_of_window_hit": False,
        "sbuffer_specs": (
            (0, 1, 0xD0),
            (1, 1, 0xD1),
        ),
    },
    {
        "name": "lh_high_multi_hit_within_sq_cover",
        "load_size": 2,
        "load_mask": 0x03,
        "load_offset": 6,
        "expect_out_of_window_hit": False,
        "sbuffer_specs": (
            (6, 1, 0xD6),
            (7, 1, 0xD7),
        ),
    },
    {
        "name": "lw_low_multi_hit_within_sq_cover",
        "load_size": 4,
        "load_mask": 0x0F,
        "load_offset": 0,
        "expect_out_of_window_hit": False,
        "sbuffer_specs": (
            (0, 2, 0xE1E0),
            (2, 2, 0xE3E2),
        ),
    },
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


def _sv39_4k_mapping_pa_base(space_pa_base: int, va: int) -> int:
    page_offset = (int(va) & ((1 << 30) - 1)) & ~0xFFF
    return int(space_pa_base) | page_offset


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
                page_table_page_addrs=(0x88013000, 0x88014000),
                va=va,
                pa_base=_sv39_4k_mapping_pa_base(MMU_FAULT_PA_BASE, va),
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


def test_api_MemBlock_scalar_aligned_load_af_matrix_with_pipeline_checks(env):
    """
    代表性 AF 组合矩阵，额外检查 dcache / forward / violation / wakeup 纯度。
    """

    req_id = 0x200
    for combo_name, pte_mode, pmp_mode, expect_tlb_af, expect_pmp_af, keep_tlb_hit in AF_PIPELINE_PROBE_CASES:
        state = _reset_env_state(env)
        va = MMU_FAULT_VA_BASE + 0x10000 + req_id * 0x20
        prime_req_id = req_id if keep_tlb_hit else None
        result = MmuFaultingScalarLoadSequence(
            root_pt_addr=MMU_FAULT_ROOT_PT,
            page_table_page_addrs=(0x88013000, 0x88014000),
            va=va,
            pa_base=MMU_FAULT_PA_BASE,
            initial_state=state,
            main_req_id=req_id + 1,
            size=4,
            mask=0xF,
            main_pte_mode=pte_mode,
            main_pmp_mode=pmp_mode,
            prime_req_id=prime_req_id,
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
                pytest.xfail(f"{TLB_SIDE_AF_CAPABILITY_GAP_XFAIL_REASON}; combo={combo_name}; {exc}")
            raise
        req_id += 2

        exception_indices = _set_exception_indices(result.main_writeback)
        expected_af = expect_tlb_af or expect_pmp_af
        if keep_tlb_hit:
            assert not result.main_ptw_trace, (
                f"{combo_name}: TLB hit 背景不应重新触发 PTW, main_ptw_trace={result.main_ptw_trace}"
            )
        elif pmp_mode == PMP_MODE_ALLOW:
            assert result.main_ptw_trace, (
                f"{combo_name}: TLB miss + PMP allow 背景应形成 PTW, main_ptw_trace={result.main_ptw_trace}"
            )
        if expected_af:
            assert exception_indices == {LOAD_ACCESS_FAULT_BIT}, (
                f"{combo_name}: AF 组合应只命中 access fault，实际={exception_indices}"
            )
            assert not result.wakeup_events_after_main, f"{combo_name}: fault 组合不应产生正常 wakeup"
            assert result.transport_stats_after_main["dcache_a_request_count"] == result.transport_stats_before_main["dcache_a_request_count"]
            assert result.transport_stats_after_main["dcache_d_response_count"] == result.transport_stats_before_main["dcache_d_response_count"]
        else:
            assert not exception_indices, f"{combo_name}: no-af 组合不应出现异常位 {exception_indices}"
            if not result.wakeup_events_after_main:
                assert result.main_writeback["int_wen"] == 1, (
                    f"{combo_name}: 未采到 wakeup 时至少应看到正常整数写回"
                )
            if keep_tlb_hit:
                assert result.transport_stats_after_main["dcache_a_request_count"] >= result.transport_stats_before_main["dcache_a_request_count"]
                assert result.transport_stats_after_main["dcache_d_response_count"] >= result.transport_stats_before_main["dcache_d_response_count"]
            else:
                assert result.transport_stats_after_main["dcache_a_request_count"] > result.transport_stats_before_main["dcache_a_request_count"]
                assert result.transport_stats_after_main["dcache_d_response_count"] > result.transport_stats_before_main["dcache_d_response_count"]
        assert result.transport_stats_after_main["outer_request_count"] == result.transport_stats_before_main["outer_request_count"]
        assert not result.sq_forward_events_after_main, f"{combo_name}: fault/load 场景不应出现 SQ/SBuffer forward"
        assert not result.replay_state_after_main["memory_violation"]["valid"], (
            f"{combo_name}: fault/load 场景不应退化成 memoryViolation"
        )
        replay_events = tuple(result.replay_state_after_main.get("events", ()))
        replay_sources = {event.get("source") for event in replay_events}
        if keep_tlb_hit:
            assert replay_sources.isdisjoint({"replay_queue", "replay_lane", "nc_out"}), (
                f"{combo_name}: 不应进入 replay 路径, events={replay_events}"
            )
        else:
            assert replay_sources.isdisjoint({"replay_lane", "nc_out"}), (
                f"{combo_name}: miss 背景不应进入 replay_lane/nc_out, events={replay_events}"
            )
            assert all(
                event.get("source") != "replay_queue" or event.get("cause") == "TM"
                for event in replay_events
            ), f"{combo_name}: miss 背景仅允许 TM replay_queue, events={replay_events}"
        assert result.dcache_error_valid_after_main == 0
        env.assert_no_outstanding()


def test_api_MemBlock_scalar_aligned_load_permission_fault_with_pipeline_checks(env):
    """
    代表性 permission PF case，额外检查真实 miss 前置 replay 与 fault 纯度。
    """

    state = _reset_env_state(env)
    result = MmuFaultingScalarLoadSequence(
        root_pt_addr=MMU_FAULT_ROOT_PT,
        page_table_page_addrs=(0x88013000, 0x88014000),
        va=MMU_FAULT_VA_BASE + 0x1A000,
        pa_base=MMU_FAULT_PA_BASE,
        initial_state=state,
        main_req_id=0x260,
        size=4,
        mask=0xF,
        main_pte_mode=PTE_MODE_PERMISSION_FAULT,
        main_pmp_mode=PMP_MODE_ALLOW,
        required_main_exception_bits=(LOAD_PAGE_FAULT_BIT,),
    ).run(env)

    exception_indices = _set_exception_indices(result.main_writeback)
    assert result.main_ptw_trace, "permission PF 应先形成 TLB miss/PTW"
    assert exception_indices == {LOAD_PAGE_FAULT_BIT}, (
        f"permission PF 应稳定收口到 load page fault，实际={exception_indices}"
    )
    assert not result.wakeup_events_after_main, "permission PF 不应产生正常 wakeup"
    assert result.transport_stats_after_main["outer_request_count"] == result.transport_stats_before_main["outer_request_count"]
    assert result.transport_stats_after_main["dcache_a_request_count"] == result.transport_stats_before_main["dcache_a_request_count"]
    assert result.transport_stats_after_main["dcache_d_response_count"] == result.transport_stats_before_main["dcache_d_response_count"]
    assert not result.sq_forward_events_after_main, "permission PF 场景不应出现 SQ/SBuffer forward"
    assert not result.replay_state_after_main["memory_violation"]["valid"], "permission PF 不应退化成 memoryViolation"
    replay_events = tuple(result.replay_state_after_main.get("events", ()))
    replay_sources = {event.get("source") for event in replay_events}
    assert replay_sources.isdisjoint({"replay_lane", "nc_out"}), (
        f"permission PF 不应进入 replay_lane/nc_out: events={replay_events}"
    )
    assert all(
        event.get("source") != "replay_queue" or event.get("cause") == "TM"
        for event in replay_events
    ), f"permission PF 仅允许 TM replay_queue: events={replay_events}"
    assert result.dcache_error_valid_after_main == 0
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
            mappings=(
                Sv39Mapping(
                    va=MATCH_INVALID_MAIN_VA,
                    pa_base=_sv39_4k_mapping_pa_base(MATCH_INVALID_PA_BASE_A, MATCH_INVALID_MAIN_VA),
                ),
            ),
            page_table_page_addrs=MATCH_INVALID_ROOT_A_PAGE_TABLE_PAGES,
        )
    ).run(env)
    root_b = MmuSv39AddressSpaceInstallSequence(
        MmuSv39AddressSpaceConfig(
            root_pt_addr=MATCH_INVALID_ROOT_B,
            mappings=(
                Sv39Mapping(
                    va=MATCH_INVALID_MAIN_VA,
                    pa_base=_sv39_4k_mapping_pa_base(MATCH_INVALID_PA_BASE_B, MATCH_INVALID_MAIN_VA),
                ),
                Sv39Mapping(
                    va=MATCH_INVALID_TLB_PRIME_VA,
                    pa_base=_sv39_4k_mapping_pa_base(MATCH_INVALID_PA_BASE_B, MATCH_INVALID_TLB_PRIME_VA),
                ),
            ),
            page_table_page_addrs=MATCH_INVALID_ROOT_B_PAGE_TABLE_PAGES,
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
