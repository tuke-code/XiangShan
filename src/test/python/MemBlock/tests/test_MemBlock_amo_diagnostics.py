# coding=utf-8
"""
MemBlock single-uop AMO internal diagnostic tests on real DUT.
"""

from MemBlock_env import _read_signal_int
from sequences import ResetEnvSequence
from transactions import AtomicTxn


AMO_DIAG_ADDR = 0x8000_6000
TAG_ARRAY_READY_TIMEOUT = 400


def _read_optional_internal_signal(dut, signal_name: str, default: int | None = 0) -> int | None:
    getter = getattr(dut, "GetInternalSignal", None)
    if getter is None:
        return default
    signal = getter(signal_name)
    if signal is None:
        return default
    return int(signal.value)


def _sample_atomic_mainpipe_state(env) -> dict:
    env.refresh_comb()
    read = lambda name, default=None: _read_optional_internal_signal(env.dut, name, default)
    return {
        "cycle": env._current_cycle(),
        "sb_is_empty": _read_signal_int(env.mem_status.sbIsEmpty, 0),
        "atomic_req_valid": read("mainPipe.io_atomic_req_valid", None),
        "atomic_req_ready": read("mainPipe.io_atomic_req_ready", None),
        "store_req_valid": read("mainPipe.io_store_req_valid", None),
        "store_req_ready": read("mainPipe.io_store_req_ready", None),
        "probe_req_valid": read("mainPipe.io_probe_req_valid", None),
        "probe_req_ready": read("mainPipe.io_probe_req_ready", None),
        "refill_req_valid": read("mainPipe.io_refill_req_valid", None),
        "refill_req_ready": read("mainPipe.io_refill_req_ready", None),
        "mainpipe_req_ready": read("mainPipe.req_ready", None),
        "tag_read_ready": read("mainPipe.io_tag_read_ready", None),
        "tag_read_valid": read("mainPipe.io_tag_read_valid", None),
        "tag_write_intend": read("mainPipe.io_tag_write_intend", None),
        "meta_read_valid": read("mainPipe.io_meta_read_valid", None),
        "data_readline_ready": read("mainPipe.io_data_readline_ready", None),
        "data_readline_valid": read("mainPipe.io_data_readline_valid", None),
        "s1_ready": read("mainPipe.s1_ready", None),
        "s2_ready": read("mainPipe.s2_ready", None),
        "s3_ready": read("mainPipe.s3_ready", None),
        "s1_s0_set_conflict": read("mainPipe.s1_s0_set_conflict", None),
        "s2_s0_set_conflict": read("mainPipe.s2_s0_set_conlict", None),
        "s3_s0_set_conflict": read("mainPipe.s3_s0_set_conflict", None),
        "tag_array_read_ready": read("tagArray.io_read_3_ready", None),
        "tag_array_write_valid": read("tagArray.io_write_valid", None),
        "tag_array_bank0_rst_cnt": read("tagArray.array_0.tag_arrays_0.rst_cnt", None),
        "tag_array_bank0_wen": read("tagArray.array_0.tag_arrays_0.wen", None),
        "sbuffer_dcache_req_valid": read("inner_sbuffer.io_dcache_req_valid", None),
        "sbuffer_dcache_req_ready": read("inner_sbuffer.io_dcache_req_ready", None),
        "sbuffer_state": read("inner_sbuffer.sbuffer_state", None),
    }


def _collect_atomic_mainpipe_trace(env, cycles: int = 1) -> list[dict]:
    samples = []
    total_cycles = max(0, int(cycles))
    for idx in range(total_cycles):
        samples.append(_sample_atomic_mainpipe_state(env))
        if idx + 1 < total_cycles:
            env.advance_cycles(1)
    return samples


def _start_single_uop_amo(env):
    state = ResetEnvSequence(
        require_issue_lanes=(0, 3, 5),
        require_lq_ready=True,
    ).run(env)
    env.preload_u64(AMO_DIAG_ADDR, 0x10)
    txn = AtomicTxn(
        req_id=0xC0,
        sq_ptr=state.sq_ptr,
        addr=AMO_DIAG_ADDR,
        operand=0x20,
        opcode="amoadd",
        size=4,
    )
    env.backend.send_atomic(txn)
    return txn


def _wait_until_tag_array_read_ready(env, max_cycles: int = TAG_ARRAY_READY_TIMEOUT) -> dict:
    for _ in range(max_cycles):
        sample = _sample_atomic_mainpipe_state(env)
        if sample["tag_array_read_ready"] == 1:
            return sample
        env.advance_cycles(1)
    raise TimeoutError(f"tagArray.io_read_3_ready 在 {max_cycles} 拍内未拉高")


def test_api_MemBlock_single_uop_amo_internal_trace_shows_mainpipe_req_ready_stuck_low(env):
    _start_single_uop_amo(env)
    trace = _collect_atomic_mainpipe_trace(env, 40)

    atomic_phase = [sample for sample in trace if sample["atomic_req_valid"] == 1]
    assert atomic_phase, "未观测到 atomic req 拉高，无法诊断 dcache ready 反压"

    assert all(sample["atomic_req_ready"] == 0 for sample in atomic_phase)
    assert all(sample["mainpipe_req_ready"] == 0 for sample in atomic_phase)
    assert all(sample["tag_read_ready"] == 0 for sample in atomic_phase)
    assert all(sample["tag_read_valid"] == 1 for sample in atomic_phase)
    assert all(sample["meta_read_valid"] == 1 for sample in atomic_phase)
    assert all(sample["data_readline_ready"] == 1 for sample in atomic_phase)
    assert all(sample["s1_ready"] == 1 for sample in atomic_phase)
    assert all(sample["s2_ready"] == 1 for sample in atomic_phase)
    assert all(sample["s3_ready"] == 1 for sample in atomic_phase)
    assert all(sample["s1_s0_set_conflict"] == 0 for sample in atomic_phase)
    assert all(sample["s2_s0_set_conflict"] == 0 for sample in atomic_phase)
    assert all(sample["s3_s0_set_conflict"] == 0 for sample in atomic_phase)


def test_api_MemBlock_single_uop_amo_internal_trace_excludes_store_probe_refill_contention(env):
    _start_single_uop_amo(env)
    trace = _collect_atomic_mainpipe_trace(env, 40)

    atomic_phase = [sample for sample in trace if sample["atomic_req_valid"] == 1]
    assert atomic_phase, "未观测到 atomic req 拉高，无法判断 mainPipe 仲裁来源"

    assert all(sample["sb_is_empty"] == 1 for sample in atomic_phase)
    assert all(sample["store_req_valid"] == 0 for sample in atomic_phase)
    assert all(sample["probe_req_valid"] == 0 for sample in atomic_phase)
    assert all(sample["refill_req_valid"] == 0 for sample in atomic_phase)
    assert all(sample["sbuffer_dcache_req_valid"] == 0 for sample in atomic_phase)
    assert all(sample["sbuffer_state"] == 0 for sample in atomic_phase)


def test_api_MemBlock_single_uop_amo_internal_trace_points_to_tag_array_init_window(env):
    _start_single_uop_amo(env)
    trace = _collect_atomic_mainpipe_trace(env, 40)

    atomic_phase = [sample for sample in trace if sample["atomic_req_valid"] == 1]
    assert atomic_phase, "未观测到 atomic req 拉高，无法判断 tagArray 是否参与反压"

    assert all(sample["tag_write_intend"] == 0 for sample in atomic_phase)
    assert all(sample["tag_array_read_ready"] == 0 for sample in atomic_phase)
    assert all(sample["tag_array_write_valid"] == 0 for sample in atomic_phase)
    assert all(sample["tag_array_bank0_wen"] == 1 for sample in atomic_phase)

    rst_cnts = [sample["tag_array_bank0_rst_cnt"] for sample in atomic_phase]
    assert all(cnt is not None for cnt in rst_cnts)
    assert rst_cnts == sorted(rst_cnts), "tagArray bank0 rst_cnt 应在 AMO 被阻塞期间单调递增"
    assert rst_cnts[0] < rst_cnts[-1], "tagArray bank0 rst_cnt 未前进，无法证明正处于初始化写窗口"


def test_api_MemBlock_single_uop_amo_ready_recovers_after_tag_array_init(env):
    state = ResetEnvSequence(
        require_issue_lanes=(0, 3, 5),
        require_lq_ready=True,
    ).run(env)
    ready_sample = _wait_until_tag_array_read_ready(env)
    assert ready_sample["tag_array_bank0_wen"] == 0

    env.preload_u64(AMO_DIAG_ADDR, 0x10)
    env.backend.send_atomic(
        AtomicTxn(
            req_id=0xC1,
            sq_ptr=state.sq_ptr,
            addr=AMO_DIAG_ADDR,
            operand=0x20,
            opcode="amoadd",
            size=4,
        )
    )
    trace = _collect_atomic_mainpipe_trace(env, 8)

    assert any(sample["tag_read_ready"] == 1 for sample in trace)
    assert any(sample["atomic_req_ready"] == 1 for sample in trace)

    env.advance_cycles(32)
    stats = env.get_transport_stats()
    assert stats["dcache_a_request_count"] >= 1
    assert stats["writeback_events"] >= 1
