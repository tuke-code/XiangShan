# coding=utf-8
"""
MemBlock 标量 store pipeline 的定向真实 DUT 用例。
"""

from collections import deque
from contextlib import contextmanager

import pytest

from request_apis import (
    enqueue_scalar_load,
    enqueue_scalar_store,
    expect_load,
    issue_scalar_load,
    issue_scalar_sta,
    issue_scalar_std,
    send_load,
)
from transactions import LoadTxn, ptr_inc, QueuePtr, StoreTxn
from sequences import (
    CboZeroFlushSequence,
    FlushStoreBuffersSequence,
    ResetEnvSequence,
    ScalarLoadSequence,
    ScalarStoreCommitSequence,
    ScalarStoreSequence,
    SequenceState,
    sample_sbuffer_forward_events,
    sample_sq_forward_events,
    wait_sq_forward_event,
)


STORE_ADDR_BASE = 0x80003000
STORE_ADDRS = [
    STORE_ADDR_BASE,
    STORE_ADDR_BASE + 0x8,
    STORE_ADDR_BASE + 0x40,
]
MISALIGNED_WINDOW_BASE = STORE_ADDR_BASE + 0x80
MISALIGNED_STORE_ADDR = MISALIGNED_WINDOW_BASE + 0x4
MISALIGNED_WINDOW_ADDRS = [
    MISALIGNED_WINDOW_BASE,
    MISALIGNED_WINDOW_BASE + 0x8,
]
PARTIAL_STORE_WINDOW_BASE = STORE_ADDR_BASE + 0xC0
PARTIAL_STORE_WINDOW_ADDRS = [
    PARTIAL_STORE_WINDOW_BASE,
    PARTIAL_STORE_WINDOW_BASE + 0x8,
]
CBO_ZERO_LINE_ADDR = STORE_ADDR_BASE + 0x100
SBUFFER_FORWARD_LINE_ADDR = STORE_ADDR_BASE + 0x140
SBUFFER_DATA_OFFSET_LINE_ADDR = STORE_ADDR_BASE + 0x180
SBUFFER_DATA_BURST_BASE = STORE_ADDR_BASE + 0x400
SBUFFER_DATA_ENTRY_MATRIX_BASE = STORE_ADDR_BASE + 0x800
STORE_RESIDENCY_TWO_WAVE_BASE = STORE_ADDR_BASE + 0xC00
STORE_CROSS16B_BURST_BASE = STORE_ADDR_BASE + 0x1000
DUTBUG_SQ_CROSS16B_BATCH_FLUSH_STALL = "DUTBUG-sq-cross16b-batch-flush-stall"
DUTBUG_SQ_CROSS16B_BATCH_FLUSH_STALL_XFAIL_REASON = (
    f"{DUTBUG_SQ_CROSS16B_BATCH_FLUSH_STALL}: "
    "batched committed cross-16B scalar stores reach sbuffer writeReq activity, but flushSb cannot drain sbuffer to empty"
)


def _reset_env_and_state(env, *, require_issue_lanes=(), require_lq_ready: bool = False) -> SequenceState:
    return ResetEnvSequence(
        require_issue_lanes=require_issue_lanes,
        require_lq_ready=require_lq_ready,
        require_sq_ready=True,
    ).run(env)


def _store_data_low64_matches(store, expected_data: int) -> bool:
    if store is None or store.data is None:
        return False
    return (int(store.data) & ((1 << 64) - 1)) == (expected_data & ((1 << 64) - 1))


def _apply_store_stimulus_to_ref_memory(refmem, *, addr: int, data: int, mask: int = 0xFF):
    refmem.apply_store(addr=addr, data=data, mask=mask)
    return refmem


def _read_sbuffer_probe(env, probe_name: str, *, default: int | None = None) -> int | None:
    value = env._read_optional_internal_signal(
        f"inner_sbuffer.{probe_name}",
        default=None,
    )
    if value is not None:
        return int(value)

    missing = -(1 << 62)
    value = env._read_optional_dut_signal(
        f"MemBlock_inner_sbuffer_{probe_name}",
        missing,
    )
    if value != missing:
        return int(value)
    return default


def _read_sbuffer_any_probe(env, probe_names, *, default: int | None = None) -> int | None:
    for probe_name in probe_names:
        value = _read_sbuffer_probe(env, probe_name, default=None)
        if value is not None:
            return int(value)
    return default


def _decode_wvec_bits(wvec: int | None) -> set[int]:
    if wvec is None:
        return set()
    value = int(wvec) & 0xFFFF
    return {bit for bit in range(16) if (value >> bit) & 0x1}


def _decode_mask_bits(mask: int | None) -> set[int]:
    if mask is None:
        return set()
    value = int(mask) & 0xFFFF
    return {bit for bit in range(16) if (value >> bit) & 0x1}


@contextmanager
def _capture_sbuffer_data_activity(env, *, max_events: int = 512):
    trace = deque(maxlen=max(1, int(max_events)))

    def _sample() -> None:
        event = {
            "cycle": env._current_cycle(),
            "write_req0_valid": _read_sbuffer_probe(env, "dataModule_io_writeReq_0_valid"),
            "write_req0_addr": _read_sbuffer_probe(env, "io_in_req_0_bits_addr"),
            "write_req0_wline": _read_sbuffer_probe(env, "io_in_req_0_bits_wline"),
            "write_req0_wvec": _read_sbuffer_any_probe(
                env,
                (
                    "dataModule_io_writeReq_0_bits_wvec",
                    "dataModule.io_writeReq_0_bits_wvec",
                ),
            ),
            "write_req0_mask": _read_sbuffer_probe(env, "io_in_req_0_bits_mask"),
            "write_lane0_valid": _read_sbuffer_probe(env, "accessIdx_0_valid_REG"),
            "write_lane0_id": _read_sbuffer_probe(env, "accessIdx_0_bits_r"),
            "write_req1_valid": _read_sbuffer_probe(env, "dataModule_io_writeReq_1_valid"),
            "write_req1_addr": _read_sbuffer_probe(env, "io_in_req_1_bits_addr"),
            "write_req1_wline": _read_sbuffer_probe(env, "io_in_req_1_bits_wline"),
            "write_req1_wvec": _read_sbuffer_any_probe(
                env,
                (
                    "dataModule_io_writeReq_1_bits_wvec",
                    "dataModule.io_writeReq_1_bits_wvec",
                ),
            ),
            "write_req1_mask": _read_sbuffer_probe(env, "io_in_req_1_bits_mask"),
            "write_lane1_valid": _read_sbuffer_probe(env, "accessIdx_1_valid_REG"),
            "write_lane1_id": _read_sbuffer_probe(env, "accessIdx_1_bits_r"),
            "mask_flush_valid": _read_sbuffer_probe(env, "io_dcache_main_pipe_hit_resp_valid"),
            "mask_flush_id": _read_sbuffer_probe(env, "io_dcache_main_pipe_hit_resp_bits_id"),
        }
        interesting = any(
            int(event.get(field) or 0)
            for field in (
                "write_req0_valid",
                "write_req1_valid",
                "write_lane0_valid",
                "write_lane1_valid",
                "write_req0_wline",
                "write_req1_wline",
                "mask_flush_valid",
            )
        )
        if interesting:
            trace.append(event)

    env.add_after_step_callback(_sample)
    try:
        yield trace
    finally:
        env.remove_after_step_callback(_sample)


def _summarize_sbuffer_data_activity(trace) -> dict:
    summary = {
        "write_event_count": 0,
        "write_req1_count": 0,
        "observed_write_offsets": set(),
        "write_lane_bits": set(),
        "write_req0_wvec_bits": set(),
        "write_req1_wvec_bits": set(),
        "write_req0_mask_bits": set(),
        "write_req1_mask_bits": set(),
        "write_req0_wvec_offset_pairs": set(),
        "write_req1_wvec_offset_pairs": set(),
        "write_req0_wvec_offset_mask_triples": set(),
        "write_req1_wvec_offset_mask_triples": set(),
        "mask_flush_count": 0,
        "mask_flush_lane_bits": set(),
        "wline_cycles": [],
    }
    for event in trace:
        if event["write_req0_valid"]:
            summary["write_event_count"] += 1
            if event["write_req0_addr"] is not None:
                offset = (int(event["write_req0_addr"]) >> 4) & 0x3
                summary["observed_write_offsets"].add(offset)
            else:
                offset = None
            if event["write_req0_wline"]:
                summary["wline_cycles"].append(int(event["cycle"]))
            active_bits = _decode_wvec_bits(event["write_req0_wvec"])
            active_mask_bits = _decode_mask_bits(event["write_req0_mask"])
            summary["write_req0_wvec_bits"].update(active_bits)
            summary["write_req0_mask_bits"].update(active_mask_bits)
            if offset is not None:
                summary["write_req0_wvec_offset_pairs"].update((bit, offset) for bit in active_bits)
                summary["write_req0_wvec_offset_mask_triples"].update(
                    (bit, offset, mask_bit)
                    for bit in active_bits
                    for mask_bit in active_mask_bits
                )
        if event["write_lane0_valid"] and event["write_lane0_id"] is not None:
            summary["write_lane_bits"].add(int(event["write_lane0_id"]) & 0xF)
        if event["write_req1_valid"]:
            summary["write_event_count"] += 1
            summary["write_req1_count"] += 1
            if event["write_req1_addr"] is not None:
                offset = (int(event["write_req1_addr"]) >> 4) & 0x3
                summary["observed_write_offsets"].add(offset)
            else:
                offset = None
            if event["write_req1_wline"]:
                summary["wline_cycles"].append(int(event["cycle"]))
            active_bits = _decode_wvec_bits(event["write_req1_wvec"])
            active_mask_bits = _decode_mask_bits(event["write_req1_mask"])
            summary["write_req1_wvec_bits"].update(active_bits)
            summary["write_req1_mask_bits"].update(active_mask_bits)
            if offset is not None:
                summary["write_req1_wvec_offset_pairs"].update((bit, offset) for bit in active_bits)
                summary["write_req1_wvec_offset_mask_triples"].update(
                    (bit, offset, mask_bit)
                    for bit in active_bits
                    for mask_bit in active_mask_bits
                )
        if event["write_lane1_valid"] and event["write_lane1_id"] is not None:
            summary["write_lane_bits"].add(int(event["write_lane1_id"]) & 0xF)
        if event["mask_flush_valid"]:
            summary["mask_flush_count"] += 1
            if event["mask_flush_id"] is not None:
                summary["mask_flush_lane_bits"].add(int(event["mask_flush_id"]) & 0xF)
    return summary


@contextmanager
def _capture_forward_activity(env, *, max_events: int = 256):
    trace = deque(maxlen=max(1, int(max_events)))

    def _sample() -> None:
        for event in sample_sq_forward_events(env):
            trace.append({"channel": "sq", **event})
        for event in sample_sbuffer_forward_events(env):
            trace.append({"channel": "sbuffer", **event})

    env.add_after_step_callback(_sample)
    try:
        yield trace
    finally:
        env.remove_after_step_callback(_sample)


def _find_forward_event(
    trace,
    *,
    channel: str,
    lane: int,
    expected_match_invalid: int = 0,
    require_forward_mask: bool = False,
):
    for event in trace:
        if event["channel"] != channel:
            continue
        if int(event["lane"]) != int(lane):
            continue
        if int(event.get("match_invalid", 0)) != int(expected_match_invalid):
            continue
        if require_forward_mask and not any(int(bit) for bit in event.get("forward_mask", ())):
            continue
        return event
    return None


def _summarize_forward_activity(trace) -> dict:
    summary = {
        "sq_event_count": 0,
        "sbuffer_event_count": 0,
        "sq_lanes": set(),
        "sbuffer_lanes": set(),
        "sq_mask_lanes": set(),
        "sbuffer_mask_lanes": set(),
        "sq_match_invalid_lanes": set(),
        "sbuffer_match_invalid_lanes": set(),
    }
    for event in trace:
        channel = event["channel"]
        lane = int(event["lane"])
        has_mask = any(int(bit) for bit in event.get("forward_mask", ()))
        match_invalid = int(event.get("match_invalid", 0))
        if channel == "sq":
            summary["sq_event_count"] += 1
            summary["sq_lanes"].add(lane)
            if has_mask:
                summary["sq_mask_lanes"].add(lane)
            if match_invalid:
                summary["sq_match_invalid_lanes"].add(lane)
        elif channel == "sbuffer":
            summary["sbuffer_event_count"] += 1
            summary["sbuffer_lanes"].add(lane)
            if has_mask:
                summary["sbuffer_mask_lanes"].add(lane)
            if match_invalid:
                summary["sbuffer_match_invalid_lanes"].add(lane)
    return {
        "sq_event_count": summary["sq_event_count"],
        "sbuffer_event_count": summary["sbuffer_event_count"],
        "sq_lanes": sorted(summary["sq_lanes"]),
        "sbuffer_lanes": sorted(summary["sbuffer_lanes"]),
        "sq_mask_lanes": sorted(summary["sq_mask_lanes"]),
        "sbuffer_mask_lanes": sorted(summary["sbuffer_mask_lanes"]),
        "sq_match_invalid_lanes": sorted(summary["sq_match_invalid_lanes"]),
        "sbuffer_match_invalid_lanes": sorted(summary["sbuffer_match_invalid_lanes"]),
    }


def _commit_scalar_store(env, sq_ptr: QueuePtr, *, req_id: int, addr: int, data: int, mask: int = 0xFF):
    commit_result = ScalarStoreCommitSequence(
        StoreTxn(
            req_id=req_id,
            sq_ptr=sq_ptr,
            addr=addr,
            data=data,
            mask=mask,
        ),
        expected_mmio=False,
        require_committed=True,
        materialize_cycles=300,
    ).run(env)
    committed_store = commit_result.committed_store_view
    assert committed_store is not None and committed_store.committed, (
        f"store req_id={req_id} 未进入 committed"
    )
    return commit_result, ptr_inc(sq_ptr, env.config.sequence.store_queue_size)


def _send_scalar_store_batch_then_commit(
    env,
    sq_ptr: QueuePtr,
    *,
    store_ops,
    req_id_base: int = 0,
    materialize_cycles: int = 300,
    commit_chunk: int = 2,
):
    store_results = []
    next_sq_ptr = sq_ptr
    for req_offset, (addr, data, mask) in enumerate(store_ops):
        store_result = ScalarStoreSequence(
            StoreTxn(
                req_id=req_id_base + req_offset,
                sq_ptr=next_sq_ptr,
                addr=addr,
                data=data,
                mask=mask,
            ),
            expected_mmio=False,
            require_committed=False,
            materialize_cycles=materialize_cycles,
        ).run(env)
        store_results.append(store_result)
        next_sq_ptr = store_result.next_sq_ptr

    remaining = len(store_results)
    while remaining > 0:
        current_chunk = min(max(1, int(commit_chunk)), remaining)
        env.backend.pulse_store_commit(current_chunk)
        remaining -= current_chunk
    env.advance_cycles(env.config.sequence.store_settle_cycles)

    committed_views = []
    for store_result in store_results:
        env.wait_store_materialized(
            store_result.allocated_sq_ptr.value,
            expected_addr=store_result.txn.addr,
            expected_data=store_result.txn.data,
            expected_mmio=False,
            require_committed=True,
            max_cycles=materialize_cycles,
        )
        committed_view = env.get_store_view(store_result.allocated_sq_ptr.value)
        assert committed_view is not None and committed_view.committed, (
            f"batch store req_id={store_result.txn.req_id} 未进入 committed"
        )
        committed_views.append(committed_view)

    return tuple(committed_views), next_sq_ptr


def _enqueue_scalar_store_batch(
    env,
    sq_ptr: QueuePtr,
    *,
    store_ops,
    req_id_base: int = 0,
    materialize_cycles: int = 300,
):
    store_results = []
    next_sq_ptr = sq_ptr
    for req_offset, (addr, data, mask) in enumerate(store_ops):
        store_result = ScalarStoreSequence(
            StoreTxn(
                req_id=req_id_base + req_offset,
                sq_ptr=next_sq_ptr,
                addr=addr,
                data=data,
                mask=mask,
            ),
            expected_mmio=False,
            require_committed=False,
            materialize_cycles=materialize_cycles,
        ).run(env)
        store_results.append(store_result)
        next_sq_ptr = store_result.next_sq_ptr
    return tuple(store_results), next_sq_ptr


def _make_sbuffer_data_wave_ops(*, base_addr: int, quarter: int, half: int):
    ops = []
    for entry in range(16):
        addr = base_addr + (entry * 0x40) + (quarter * 0x10) + (half * 0x8)
        data = (
            0x0102_0304_0506_0708
            ^ ((quarter + 1) << 56)
            ^ ((entry + 1) << 40)
            ^ (half << 32)
        )
        ops.append((addr, data, 0xFF))
    return tuple(ops)


def _extract_singleton_wvec_bit(summary: dict, *, field_name: str, context: str) -> int:
    bits = summary[field_name]
    assert len(bits) == 1, f"{context} 期望只命中单个 wvec bit，实际={sorted(bits)}"
    return next(iter(bits))


def _prewarm_sbuffer_entry_map(
    env,
    sq_ptr: QueuePtr,
    *,
    base_addr: int,
    line_count: int,
    req_id_base: int = 0,
    preload_seed: int = 5,
):
    expected_refmem = env.memory.fork_ref_memory()
    line_to_entry = {}
    entry_to_line = {}
    next_sq_ptr = sq_ptr

    for index in range(line_count):
        line_addr = base_addr + (index * 0x40)
        preload = bytes((((index + preload_seed) * 19) + byte_idx) & 0xFF for byte_idx in range(64))
        env.preload_bytes(line_addr, preload)

        with _capture_sbuffer_data_activity(env, max_events=32) as trace:
            _, next_sq_ptr = _commit_scalar_store(
                env,
                next_sq_ptr,
                req_id=req_id_base + index,
                addr=line_addr,
                data=0x1111_2222_3333_4444 ^ (index << 20),
            )
        summary = _summarize_sbuffer_data_activity(trace)
        entry = _extract_singleton_wvec_bit(
            summary,
            field_name="write_req0_wvec_bits",
            context=f"prewarm line_addr={line_addr:#x}",
        )
        line_to_entry[line_addr] = entry
        entry_to_line.setdefault(entry, line_addr)
        _apply_store_stimulus_to_ref_memory(
            expected_refmem,
            addr=line_addr,
            data=0x1111_2222_3333_4444 ^ (index << 20),
        )

    return {
        "next_sq_ptr": next_sq_ptr,
        "line_to_entry": line_to_entry,
        "entry_to_line": entry_to_line,
        "expected_refmem": expected_refmem,
    }


def _assert_forward_response_matches_partial_word(event: dict, *, store_data: int) -> None:
    expected_mask = (1, 1, 1, 1) + (0,) * 12
    expected_bytes = tuple((int(store_data) >> (8 * index)) & 0xFF for index in range(4))

    assert tuple(int(bit) for bit in event["forward_mask"]) == expected_mask, (
        f"partial-word forward mask 异常: actual={tuple(int(bit) for bit in event['forward_mask'])}"
    )
    assert tuple(int(byte) for byte in event["forward_data"][:4]) == expected_bytes, (
        f"partial-word forward data 低 4B 异常: actual={tuple(int(byte) for byte in event['forward_data'][:4])}"
    )


def _assert_forward_response_matches_expected_merge(
    event: dict,
    *,
    expected_data: int,
    required_prefix_bytes: int = 4,
) -> None:
    expected_bytes = tuple((int(expected_data) >> (8 * index)) & 0xFF for index in range(8))
    forward_mask = tuple(int(bit) for bit in event["forward_mask"])
    forward_data = tuple(int(byte) for byte in event["forward_data"])

    assert all(forward_mask[index] == 1 for index in range(required_prefix_bytes)), (
        f"forward mask 前 {required_prefix_bytes}B 未全部有效: actual={forward_mask}"
    )
    for index in range(8):
        if not forward_mask[index]:
            continue
        assert forward_data[index] == expected_bytes[index], (
            f"forward data byte{index} 异常: actual={forward_data[index]:#x}, "
            f"expected={expected_bytes[index]:#x}, mask={forward_mask}"
        )
    assert not any(forward_mask[index] for index in range(8, 16)), (
        f"8B 标量 load 的 sbuffer forward mask 不应超出前 8B: actual={forward_mask}"
    )


def _apply_masked_store_to_line_bytes(line_bytes: bytearray, *, line_addr: int, addr: int, data: int, mask: int) -> None:
    offset = int(addr) - int(line_addr)
    assert 0 <= offset < len(line_bytes), (
        f"store addr 越出目标 cacheline: addr={addr:#x}, line_addr={line_addr:#x}"
    )
    for byte_idx in range(8):
        if ((int(mask) >> byte_idx) & 0x1) == 0:
            continue
        line_bytes[offset + byte_idx] = (int(data) >> (8 * byte_idx)) & 0xFF


def _run_sbuffer_forward_entry_matrix(
    env,
    *,
    load_issue_lane: int,
    entry_update_specs,
    base_addr: int,
    req_id_base: int,
    preload_seed: int,
):
    for index, (target_entry, store_offset) in enumerate(entry_update_specs):
        state = _reset_env_and_state(
            env,
            require_issue_lanes=(0, 1, 2),
            require_lq_ready=True,
        )
        prewarm = _prewarm_sbuffer_entry_map(
            env,
            state.sq_ptr,
            base_addr=base_addr + (index * 0x400),
            line_count=max(6, int(target_entry) + 1),
            req_id_base=req_id_base + (index * 0x40),
            preload_seed=preload_seed + index,
        )
        sq_ptr = prewarm["next_sq_ptr"]
        lq_ptr = state.next_lq_ptr
        expected_refmem = prewarm["expected_refmem"]
        entry_to_line = prewarm["entry_to_line"]
        completed_target = env.get_completed_load_count()

        assert target_entry in entry_to_line, (
            f"预热阶段未获得 entry{target_entry} 映射: actual={sorted(entry_to_line)}"
        )
        target_line_addr = entry_to_line[target_entry]
        target_line_index = (target_line_addr - (base_addr + (index * 0x400))) // 0x40
        expected_target_line = bytearray(
            ((((target_line_index + preload_seed + index) * 19) + byte_idx) & 0xFF)
            for byte_idx in range(64)
        )
        _apply_masked_store_to_line_bytes(
            expected_target_line,
            line_addr=target_line_addr,
            addr=target_line_addr,
            data=0x1111_2222_3333_4444 ^ (target_line_index << 20),
            mask=0xFF,
        )
        store_addr = target_line_addr + int(store_offset)
        store_data = 0x91_92_93_94 ^ (int(load_issue_lane) << 24) ^ (int(target_entry) << 8) ^ index

        _, sq_ptr = _commit_scalar_store(
            env,
            sq_ptr,
            req_id=req_id_base + 0x100 + index,
            addr=store_addr,
            data=store_data,
            mask=0x0F,
        )
        _apply_masked_store_to_line_bytes(
            expected_target_line,
            line_addr=target_line_addr,
            addr=store_addr,
            data=store_data,
            mask=0x0F,
        )

        load_txn = LoadTxn(
            req_id=req_id_base + 0x200 + index,
            addr=store_addr,
            lq_ptr=lq_ptr,
            sq_ptr=sq_ptr,
            issue_lane=load_issue_lane,
        )
        env.backend.prepare(load_txn)
        enqueue_scalar_load(
            env,
            req_id=load_txn.req_id,
            lq_ptr=load_txn.lq_ptr,
            sq_ptr=load_txn.sq_ptr,
            enq_port=load_txn.enq_port,
            rob_idx=load_txn.rob_idx,
        )
        expect_load(env, load_txn)

        expected_load_data = int.from_bytes(
            expected_target_line[int(store_offset):int(store_offset) + 8],
            "little",
        )
        transport_stats_before = env.get_transport_stats()
        with _capture_forward_activity(env, max_events=128) as trace:
            issue_scalar_load(
                env,
                req_id=load_txn.req_id,
                addr=load_txn.addr,
                lq_ptr=load_txn.lq_ptr,
                sq_ptr=load_txn.sq_ptr,
                lane=load_txn.issue_lane,
                store_set_hit=load_txn.store_set_hit,
                load_wait_bit=load_txn.load_wait_bit,
                load_wait_strict=load_txn.load_wait_strict,
                wait_for_rob_idx=load_txn.wait_for_rob_idx,
                rob_idx=load_txn.rob_idx,
                pdest=load_txn.resolved_pdest,
                ftq_idx_flag=load_txn.resolved_ftq_idx_flag,
                ftq_idx_value=load_txn.resolved_ftq_idx_value,
                pc=load_txn.resolved_pc,
            )
            load_writeback = env.wait_load_writeback_observed(
                rob_idx=load_txn.rob_idx,
                data=expected_load_data,
                max_cycles=200,
            )
            completed_loads = env.wait_completed_load_count(completed_target + 1, max_cycles=64)
        transport_stats_after = env.get_transport_stats()

        sbuffer_forward_event = _find_forward_event(
            trace,
            channel="sbuffer",
            lane=load_issue_lane,
            expected_match_invalid=0,
            require_forward_mask=True,
        )
        assert sbuffer_forward_event is not None, (
            f"entry{target_entry} offset={store_offset:#x} 未观测到 lane{load_issue_lane} sbuffer forward: "
            f"{_summarize_forward_activity(trace)}"
        )
        _assert_forward_response_matches_expected_merge(
            sbuffer_forward_event,
            expected_data=expected_load_data,
            required_prefix_bytes=4,
        )
        assert load_writeback["data"] == expected_load_data, (
            f"entry{target_entry} offset={store_offset:#x} sbuffer forward 后的 load 写回数据不匹配"
        )
        assert completed_loads == completed_target + 1, (
            f"entry{target_entry} offset={store_offset:#x} 的 load 未按预期完成 compare"
        )
        assert transport_stats_after["outer_request_count"] == transport_stats_before["outer_request_count"], (
            f"entry{target_entry} offset={store_offset:#x} 的 sbuffer forward 不应退化成 outer/uncache 路径"
        )

        drain_summary = FlushStoreBuffersSequence(max_cycles=1000).run(env)
        assert env.read_cacheline(target_line_addr) == bytes(expected_target_line), (
            f"entry{target_entry} offset={store_offset:#x} 的 cacheline 最终结果不匹配: "
            f"line_addr={target_line_addr:#x}"
        )
        assert drain_summary["drain_event_count"] >= 1, (
            f"entry{target_entry} offset={store_offset:#x} 场景未记录到 drain 事件"
        )
        assert drain_summary["touched_byte_count"] >= 8, (
            f"entry{target_entry} offset={store_offset:#x} 场景 flush 覆盖字节数不足"
        )
        env.assert_no_outstanding()


def test_api_MemBlock_two_cacheable_stores_flush_directed(env):
    """
    两条递增 robIdx 的 cacheable store，结尾统一 flush/drain。

    检查点：
      - 两条 store 均能在真实 ROB 提交建模下进入 committed
      - 结尾 flush 后成功观测到 sbuffer drain
      - drain 至少覆盖两笔 dword 写入
    """

    first_data = 0x0102_0304_0506_0708
    second_data = 0x1112_1314_1516_1718

    state = _reset_env_and_state(env)
    sbuffer_before = env.get_counter("sbuffer_drain_count")

    first_result = ScalarStoreCommitSequence(
        StoreTxn(req_id=0, sq_ptr=state.sq_ptr, addr=STORE_ADDRS[0], data=first_data),
        expected_mmio=False,
        require_committed=True,
        materialize_cycles=300,
    ).run(env)
    second_result = ScalarStoreCommitSequence(
        StoreTxn(
            req_id=1,
            sq_ptr=ptr_inc(state.sq_ptr, env.config.sequence.store_queue_size),
            addr=STORE_ADDRS[1],
            data=second_data,
        ),
        expected_mmio=False,
        require_committed=True,
        materialize_cycles=300,
    ).run(env)
    drain_summary = FlushStoreBuffersSequence(max_cycles=800).run(env)

    first_store = first_result.committed_store_view
    second_store = second_result.committed_store_view

    assert first_store is not None and first_store.committed, "第一条 cacheable store 未进入 committed"
    assert second_store is not None and second_store.committed, "第二条 cacheable store 未进入 committed"
    assert first_store.addr == STORE_ADDRS[0], "第一条 cacheable store 地址不匹配"
    assert second_store.addr == STORE_ADDRS[1], "第二条 cacheable store 地址不匹配"
    assert _store_data_low64_matches(first_store, first_data), "第一条 cacheable store 数据不匹配"
    assert _store_data_low64_matches(second_store, second_data), "第二条 cacheable store 数据不匹配"
    assert env.get_counter("sbuffer_drain_count") > sbuffer_before, "双 store flush 未触发 sbuffer drain"
    assert drain_summary["drain_event_count"] >= 1, "双 store flush 未记录到 drain 事件"
    assert drain_summary["touched_byte_count"] >= 16, "双 store flush 覆盖字节数不足"
    env.assert_no_outstanding()


def test_api_MemBlock_cross_line_two_cacheable_stores_flush_directed(env):
    """
    两条递增 robIdx 的 cacheable store，覆盖跨 cacheline 写出。

    检查点：
      - 两条 store 都能在 flush 前进入 committed
      - 统一 flush 后 drain 收口成功
      - 跨 cacheline 写出不会破坏最终一致性
    """

    data_words = [
        0x2222_3333_4444_5555,
        0xAAAA_BBBB_CCCC_DDDD,
    ]
    addrs = [STORE_ADDRS[0], STORE_ADDRS[2]]

    state = _reset_env_and_state(env)
    sq_ptr = state.sq_ptr
    committed_views = []

    for req_id, (addr, data_word) in enumerate(zip(addrs, data_words)):
        commit_result = ScalarStoreCommitSequence(
            StoreTxn(req_id=req_id, sq_ptr=sq_ptr, addr=addr, data=data_word),
            expected_mmio=False,
            require_committed=True,
            materialize_cycles=300,
        ).run(env)
        committed_views.append(commit_result.committed_store_view)
        sq_ptr = ptr_inc(sq_ptr, env.config.sequence.store_queue_size)

    drain_summary = FlushStoreBuffersSequence(max_cycles=800).run(env)

    for idx, (store, addr, data_word) in enumerate(zip(committed_views, addrs, data_words)):
        assert store is not None and store.committed, f"第 {idx} 条 store 未进入 committed"
        assert store.addr == addr, f"第 {idx} 条 store 地址不匹配"
        assert _store_data_low64_matches(store, data_word), f"第 {idx} 条 store 数据不匹配"

    assert drain_summary["drain_event_count"] >= 1, "跨线双 store flush 未记录到 drain 事件"
    assert drain_summary["touched_byte_count"] >= 16, "跨线双 store flush 覆盖字节数不足"
    env.assert_no_outstanding()


def test_api_MemBlock_sbuffer_data_vword_offset_matrix_directed(env):
    """
    同一 cacheline 上覆盖 `vwordOffset=0/1/2/3` 的 partial-store/writeback 路径。

    检查点：
      - 四个 16B 分区都能形成真实 committed store
      - `SbufferData.io_writeReq_*_bits_vwordOffset[1:0]` 观测到 0/1/2/3 全部象限
      - younger load 在 flush 前能看到 merge 结果
      - flush 后整条 cacheline 与参考内存一致
    """

    partial_ops = [
        (SBUFFER_DATA_OFFSET_LINE_ADDR + 0x00, 0xA1, 0x01),
        (SBUFFER_DATA_OFFSET_LINE_ADDR + 0x11, 0xB2, 0x01),
        (SBUFFER_DATA_OFFSET_LINE_ADDR + 0x22, 0xC3C4, 0x03),
        (SBUFFER_DATA_OFFSET_LINE_ADDR + 0x34, 0xD5D6_D7D8, 0x0F),
    ]
    load_addrs = [SBUFFER_DATA_OFFSET_LINE_ADDR + (index * 0x10) for index in range(4)]

    state = _reset_env_and_state(env)
    env.preload_bytes(
        SBUFFER_DATA_OFFSET_LINE_ADDR,
        bytes(((index * 11) + 3) & 0xFF for index in range(64)),
    )
    expected_refmem = env.memory.fork_ref_memory()

    with _capture_sbuffer_data_activity(env) as trace:
        sq_ptr = state.sq_ptr
        for req_id, (addr, data, mask) in enumerate(partial_ops):
            _, sq_ptr = _commit_scalar_store(
                env,
                sq_ptr,
                req_id=req_id,
                addr=addr,
                data=data,
                mask=mask,
            )
            _apply_store_stimulus_to_ref_memory(
                expected_refmem,
                addr=addr,
                data=data,
                mask=mask,
            )

        next_lq_ptr = state.next_lq_ptr
        for completed_loads, load_addr in enumerate(load_addrs, start=1):
            load_result = ScalarLoadSequence(
                LoadTxn(
                    req_id=len(partial_ops) + completed_loads - 1,
                    addr=load_addr,
                    lq_ptr=next_lq_ptr,
                    sq_ptr=sq_ptr,
                ),
                expected_completed_loads=completed_loads,
            ).run(env)
            next_lq_ptr = load_result.next_lq_ptr

        drain_summary = FlushStoreBuffersSequence(max_cycles=1000).run(env)

    summary = _summarize_sbuffer_data_activity(trace)
    assert summary["write_event_count"] >= len(partial_ops), "SbufferData 未记录到足够的 writeReq 活动"
    assert summary["observed_write_offsets"] == {0, 1, 2, 3}, (
        f"SbufferData 未覆盖完整 vwordOffset 象限: actual={sorted(summary['observed_write_offsets'])}"
    )
    assert env.read_cacheline(SBUFFER_DATA_OFFSET_LINE_ADDR) == expected_refmem.read_cacheline(
        SBUFFER_DATA_OFFSET_LINE_ADDR,
        64,
    ), "vwordOffset 象限覆盖场景的 cacheline 最终结果不匹配"
    assert drain_summary["drain_event_count"] >= 1, "vwordOffset 象限覆盖场景未记录到 drain 事件"
    assert drain_summary["touched_byte_count"] >= 8, "vwordOffset 象限覆盖场景 flush 覆盖字节数不足"
    env.assert_no_outstanding()


@pytest.mark.xfail(
    reason="DUTBUG-sbuffer-entry5-targeted-merge-drain-corruption: targeted entry5 quarter3 byte3 merge hits the real writeReq_0 triple but final drain corrupts a prewarmed cacheline",
    strict=False,
)
def test_api_MemBlock_sbuffer_data_port0_line5_word3_byte3_directed(env):
    """
    定向覆盖 port0 在 line5 上的 quarter3 byte3 merge，并保留 line1/line5 的联合搜索入口。

    检查点：
      - 预热阶段能稳定把目标 cacheline 映射到 `wvec[5]`
      - 目标 4B store 在真实 `writeReq_0` 上命中 `(entry=5, offset=3, byte=3)`
      - flush 后所有触及 cacheline 与参考内存一致
    """

    target_triple = (5, 3, 3)
    attempt_specs = (
        {"base": SBUFFER_DATA_ENTRY_MATRIX_BASE + 0x3000, "line_count": 6, "seed": 11},
        {"base": SBUFFER_DATA_ENTRY_MATRIX_BASE + 0x3400, "line_count": 7, "seed": 13},
        {"base": SBUFFER_DATA_ENTRY_MATRIX_BASE + 0x3800, "line_count": 8, "seed": 17},
    )
    last_failure = None

    for attempt_id, spec in enumerate(attempt_specs):
        state = _reset_env_and_state(env)
        prewarm = _prewarm_sbuffer_entry_map(
            env,
            state.sq_ptr,
            base_addr=spec["base"],
            line_count=spec["line_count"],
            req_id_base=1000 + (attempt_id * 100),
            preload_seed=spec["seed"],
        )
        sq_ptr = prewarm["next_sq_ptr"]
        entry_to_line = prewarm["entry_to_line"]
        expected_refmem = prewarm["expected_refmem"]

        if 5 not in entry_to_line:
            last_failure = {
                "attempt": attempt_id,
                "reason": "missing_prewarm_entry5",
                "entries": sorted(entry_to_line),
            }
            continue

        observed_triples = set()
        candidate_line_addrs = tuple(line_addr for _, line_addr in sorted(entry_to_line.items()))
        operation_plans = (
            (5,),
            (0, 5),
            (5, 0),
            (3, 4, 5),
        )

        for plan_id, plan_entries in enumerate(operation_plans):
            plan_line_addrs = tuple(
                entry_to_line[entry]
                for entry in plan_entries
                if entry in entry_to_line
            )
            if not plan_line_addrs:
                continue

            for seq_id, line_addr in enumerate(plan_line_addrs):
                req_id = 2000 + (attempt_id * 100) + (plan_id * 16) + seq_id
                store_data = 0xD0_00_00_00 | (req_id & 0xFF)
                with _capture_sbuffer_data_activity(env, max_events=64) as trace:
                    _, sq_ptr = _commit_scalar_store(
                        env,
                        sq_ptr,
                        req_id=req_id,
                        addr=line_addr + 0x30,
                        data=store_data,
                        mask=0x0F,
                    )
                _apply_store_stimulus_to_ref_memory(
                    expected_refmem,
                    addr=line_addr + 0x30,
                    data=store_data,
                    mask=0x0F,
                )
                summary = _summarize_sbuffer_data_activity(trace)
                observed_triples.update(summary["write_req0_wvec_offset_mask_triples"])
                if target_triple in observed_triples:
                    break

            if target_triple in observed_triples:
                break

        drain_summary = FlushStoreBuffersSequence(max_cycles=1000).run(env)
        memory_ok = True
        for line_addr in candidate_line_addrs:
            if env.read_cacheline(line_addr) != expected_refmem.read_cacheline(line_addr, 64):
                memory_ok = False
                last_failure = {
                    "attempt": attempt_id,
                    "reason": "memory_mismatch",
                    "line_addr": f"{line_addr:#x}",
                    "observed_triples": sorted(observed_triples),
                }
                break

        if target_triple in observed_triples and drain_summary["drain_event_count"] >= 1 and memory_ok:
            env.assert_no_outstanding()
            return

        if last_failure is None or last_failure.get("attempt") != attempt_id:
            last_failure = {
                "attempt": attempt_id,
                "reason": "missing_target_triple",
                "observed_triples": sorted(observed_triples),
            }

    raise AssertionError(f"未命中 port0 entry5 offset3/byte3 目标组合: {last_failure}")


@pytest.mark.xfail(
    reason="DUTBUG-sbuffer-entry1-target-retention: prewarmed entry1 frequently drains or reallocates before the quarter3 byte3 merge completes; broader retries also expose drain corruption",
    strict=False,
)
def test_api_MemBlock_sbuffer_data_port0_line1_and_line5_word3_byte3_search(env):
    """
    预热 sbuffer entry 后，定向覆盖 port0 在 line1/line5 上的 quarter3 byte3 merge。

    检查点：
      - 预热阶段能稳定把目标 cacheline 映射到 `wvec[1]` 与 `wvec[5]`
      - 目标 4B store 在真实 `writeReq_0` 上命中 `(entry=1, offset=3, byte=3)` 与 `(entry=5, offset=3, byte=3)`
      - flush 后所有触及 cacheline 与参考内存一致
    """

    target_triples = {(1, 3, 3), (5, 3, 3)}
    attempt_specs = (
        {"base": SBUFFER_DATA_ENTRY_MATRIX_BASE + 0x3000, "line_count": 6, "seed": 11},
        {"base": SBUFFER_DATA_ENTRY_MATRIX_BASE + 0x3400, "line_count": 7, "seed": 13},
        {"base": SBUFFER_DATA_ENTRY_MATRIX_BASE + 0x3800, "line_count": 8, "seed": 17},
    )
    last_failure = None

    for attempt_id, spec in enumerate(attempt_specs):
        state = _reset_env_and_state(env)
        prewarm = _prewarm_sbuffer_entry_map(
            env,
            state.sq_ptr,
            base_addr=spec["base"],
            line_count=spec["line_count"],
            req_id_base=1000 + (attempt_id * 100),
            preload_seed=spec["seed"],
        )
        sq_ptr = prewarm["next_sq_ptr"]
        entry_to_line = prewarm["entry_to_line"]
        expected_refmem = prewarm["expected_refmem"]

        if 1 not in entry_to_line or 5 not in entry_to_line:
            last_failure = {
                "attempt": attempt_id,
                "reason": "missing_prewarm_entries",
                "entries": sorted(entry_to_line),
            }
            continue

        observed_triples = set()
        candidate_line_addrs = tuple(line_addr for _, line_addr in sorted(entry_to_line.items()))
        operation_plans = (
            (1, 5),
            (5, 1),
            (1, 5, 0, 2, 3, 4, 6, 7, 8),
            (5, 1, 0, 2, 3, 4, 6, 7, 8),
        )

        for plan_id, plan_entries in enumerate(operation_plans):
            plan_line_addrs = tuple(
                entry_to_line[entry]
                for entry in plan_entries
                if entry in entry_to_line
            )
            if not plan_line_addrs:
                continue

            for seq_id, line_addr in enumerate(plan_line_addrs):
                req_id = 2000 + (attempt_id * 100) + (plan_id * 16) + seq_id
                store_data = 0xD0_00_00_00 | (req_id & 0xFF)
                with _capture_sbuffer_data_activity(env, max_events=64) as trace:
                    _, sq_ptr = _commit_scalar_store(
                        env,
                        sq_ptr,
                        req_id=req_id,
                        addr=line_addr + 0x30,
                        data=store_data,
                        mask=0x0F,
                    )
                _apply_store_stimulus_to_ref_memory(
                    expected_refmem,
                    addr=line_addr + 0x30,
                    data=store_data,
                    mask=0x0F,
                )
                summary = _summarize_sbuffer_data_activity(trace)
                observed_triples.update(summary["write_req0_wvec_offset_mask_triples"])
                if target_triples <= observed_triples:
                    break

            if target_triples <= observed_triples:
                break

        drain_summary = FlushStoreBuffersSequence(max_cycles=1000).run(env)
        memory_ok = True
        for line_addr in candidate_line_addrs:
            if env.read_cacheline(line_addr) != expected_refmem.read_cacheline(line_addr, 64):
                memory_ok = False
                last_failure = {
                    "attempt": attempt_id,
                    "reason": "memory_mismatch",
                    "line_addr": f"{line_addr:#x}",
                    "observed_triples": sorted(observed_triples),
                }
                break

        if target_triples <= observed_triples and drain_summary["drain_event_count"] >= 1 and memory_ok:
            env.assert_no_outstanding()
            return

        if last_failure is None or last_failure.get("attempt") != attempt_id:
            last_failure = {
                "attempt": attempt_id,
                "reason": "missing_target_triples",
                "observed_triples": sorted(observed_triples),
            }

    raise AssertionError(f"未命中 port0 entry1/entry5 offset3/byte3 目标组合: {last_failure}")


@pytest.mark.xfail(
    reason="DUTBUG-sbuffer-targeted-deep-merge-drain-corruption: deep prewarmed entry merge hits targeted wvec pairs but drained cacheline bytes diverge from reference memory",
    strict=False,
)
def test_api_MemBlock_sbuffer_data_targeted_entry_merge_directed(env):
    """
    先建立 cacheline->wvec entry 映射，再定向让真实写路径命中 entry0/entry12。

    检查点：
      - 预热阶段能稳定观察到单发 store 对应的真实 `wvec` entry
      - 定向深 batch 让真实 `wvec` 命中 `(entry=0, offset=0)` 与 `(entry=12, offset=1)`
      - 最终 flush 后所有 cacheline 与参考内存一致
    """

    state = _reset_env_and_state(env)
    expected_refmem = env.memory.fork_ref_memory()
    prewarm_base = SBUFFER_DATA_ENTRY_MATRIX_BASE + 0x2000
    line_to_entry = {}
    entry_to_line = {}
    sq_ptr = state.sq_ptr

    for index in range(14):
        line_addr = prewarm_base + (index * 0x40)
        env.preload_bytes(
            line_addr,
            bytes((((index + 5) * 19) + byte_idx) & 0xFF for byte_idx in range(64)),
        )

        with _capture_sbuffer_data_activity(env, max_events=32) as trace:
            _, sq_ptr = _commit_scalar_store(
                env,
                sq_ptr,
                req_id=index,
                addr=line_addr,
                data=0x1111_2222_3333_4444 ^ (index << 20),
            )
        summary = _summarize_sbuffer_data_activity(trace)
        entry = _extract_singleton_wvec_bit(
            summary,
            field_name="write_req0_wvec_bits",
            context=f"prewarm line_addr={line_addr:#x}",
        )
        line_to_entry[line_addr] = entry
        entry_to_line.setdefault(entry, line_addr)
        _apply_store_stimulus_to_ref_memory(
            expected_refmem,
            addr=line_addr,
            data=0x1111_2222_3333_4444 ^ (index << 20),
        )

    assert 0 in entry_to_line, f"预热阶段未获得 entry0 映射: actual={sorted(entry_to_line)}"
    assert 12 in entry_to_line, f"预热阶段未获得 entry12 映射: actual={sorted(entry_to_line)}"

    entry0_line = entry_to_line[0]
    entry12_line = entry_to_line[12]
    helper_line = prewarm_base + 0x1000
    env.preload_bytes(
        helper_line,
        bytes(((0xA5 + byte_idx) & 0xFF) for byte_idx in range(64)),
    )
    batch_ops = (
        (helper_line + 0x00, 0xABCD_0000_0000_0001, 0xFF),
        (entry0_line + 0x00, 0x2233_4455_6677_8899, 0xFF),
        (helper_line + 0x10, 0xABCD_0000_0000_1001, 0xFF),
        (entry12_line + 0x10, 0x8899_AABB_CCDD_EEFF, 0xFF),
        (entry_to_line[1] + 0x00, 0x0101_0202_0303_0404, 0xFF),
        (entry_to_line[2] + 0x10, 0x0505_0606_0707_0808, 0xFF),
        (entry_to_line[3] + 0x00, 0x1111_1212_1313_1414, 0xFF),
        (entry_to_line[4] + 0x10, 0x1515_1616_1717_1818, 0xFF),
        (entry_to_line[5] + 0x00, 0x2121_2222_2323_2424, 0xFF),
        (entry_to_line[6] + 0x10, 0x2525_2626_2727_2828, 0xFF),
        (entry_to_line[7] + 0x00, 0x3131_3232_3333_3434, 0xFF),
        (entry_to_line[8] + 0x10, 0x3535_3636_3737_3838, 0xFF),
        (entry_to_line[9] + 0x00, 0x4141_4242_4343_4444, 0xFF),
        (entry_to_line[10] + 0x10, 0x4545_4646_4747_4848, 0xFF),
        (entry_to_line[11] + 0x00, 0x5151_5252_5353_5454, 0xFF),
        (entry_to_line[13] + 0x10, 0x5555_5656_5757_5858, 0xFF),
    )

    with _capture_sbuffer_data_activity(env, max_events=1024) as trace:
        _, sq_ptr = _send_scalar_store_batch_then_commit(
            env,
            sq_ptr,
            store_ops=batch_ops,
            req_id_base=200,
            commit_chunk=16,
        )
        for addr, data, mask in batch_ops:
            _apply_store_stimulus_to_ref_memory(
                expected_refmem,
                addr=addr,
                data=data,
                mask=mask,
            )

        drain_summary = FlushStoreBuffersSequence(max_cycles=1600).run(env)

    summary = _summarize_sbuffer_data_activity(trace)
    observed_pairs = summary["write_req0_wvec_offset_pairs"] | summary["write_req1_wvec_offset_pairs"]
    assert (0, 0) in observed_pairs, (
        f"未观测到 targeted entry0 offset0 命中: actual={sorted(observed_pairs)}"
    )
    assert (12, 1) in observed_pairs, (
        f"未观测到 targeted entry12 offset1 命中: actual={sorted(observed_pairs)}"
    )
    assert drain_summary["drain_event_count"] >= 1, "targeted entry merge 场景未记录到 drain 事件"

    touched_lines = {entry0_line, entry12_line, helper_line}
    for line_addr in touched_lines:
        assert env.read_cacheline(line_addr) == expected_refmem.read_cacheline(line_addr, 64), (
            f"targeted entry merge 场景的 cacheline {line_addr:#x} 最终结果不匹配"
        )
    env.assert_no_outstanding()


@pytest.mark.xfail(
    reason="DUTBUG-sbuffer-batched-commit-drain-corruption: wide multi-entry batched store commit corrupts drained cacheline bytes before flush convergence",
    strict=False,
)
def test_api_MemBlock_sbuffer_data_entry_quarter_matrix_batched_commit(env):
    """
    分 4 个 quarter 波次批量 commit 16 个 cacheline entry，补齐 SbufferData 的 entry/quarter/byte 组合。

    检查点：
      - 每个 sub-wave 中 16 笔 store 都能在统一 commit 后进入 committed
      - `SbufferData.io_writeReq_*_bits_wvec` 覆盖更宽的真实 entry 集合，`vwordOffset` 覆盖 0/1/2/3
      - 批量 commit 场景中第二写口 `writeReq_1` 出现真实活动
      - 每个 wave flush 后，16 条 cacheline 与参考内存一致
    """

    state = _reset_env_and_state(env)
    expected_refmem = env.memory.fork_ref_memory()

    line_addrs = [SBUFFER_DATA_ENTRY_MATRIX_BASE + (entry * 0x40) for entry in range(16)]
    for entry, line_addr in enumerate(line_addrs):
        env.preload_bytes(
            line_addr,
            bytes((((entry + 3) * 17) + byte_idx) & 0xFF for byte_idx in range(64)),
        )

    summary = {}
    with _capture_sbuffer_data_activity(env, max_events=8192) as trace:
        try:
            sq_ptr = state.sq_ptr
            req_id_base = 0

            for quarter in range(4):
                for half in range(2):
                    wave_ops = _make_sbuffer_data_wave_ops(
                        base_addr=SBUFFER_DATA_ENTRY_MATRIX_BASE,
                        quarter=quarter,
                        half=half,
                    )
                    committed_views, sq_ptr = _send_scalar_store_batch_then_commit(
                        env,
                        sq_ptr,
                        store_ops=wave_ops,
                        req_id_base=req_id_base,
                    )
                    req_id_base += len(wave_ops)

                    assert len(committed_views) == len(wave_ops), "batch commit 后 committed store 数量异常"
                    for store, (addr, data, _) in zip(committed_views, wave_ops):
                        assert store.addr == addr, "batch commit store 地址不匹配"
                        assert _store_data_low64_matches(store, data), "batch commit store 数据不匹配"
                        _apply_store_stimulus_to_ref_memory(
                            expected_refmem,
                            addr=addr,
                            data=data,
                        )

                    drain_summary = FlushStoreBuffersSequence(max_cycles=1600).run(env)
                    assert drain_summary["drain_event_count"] >= 1, (
                        f"quarter={quarter}, half={half} flush 未记录到 drain 事件"
                    )
        finally:
            summary = _summarize_sbuffer_data_activity(trace)

    for line_addr in line_addrs:
        assert env.read_cacheline(line_addr) == expected_refmem.read_cacheline(line_addr, 64), (
            f"entry/quarter batched commit 场景的 cacheline {line_addr:#x} 最终结果不匹配"
        )

    assert summary["observed_write_offsets"] == {0, 1, 2, 3}, (
        f"entry/quarter batched commit 未覆盖完整 vwordOffset 象限: actual={sorted(summary['observed_write_offsets'])}"
    )
    observed_write_entries = summary["write_req0_wvec_bits"] | summary["write_req1_wvec_bits"]
    assert len(observed_write_entries) >= 8, (
        f"entry/quarter batched commit 真实 wvec entry 覆盖不足: actual={sorted(observed_write_entries)}"
    )
    assert summary["write_req1_count"] > 0, "entry/quarter batched commit 未观测到第二写口活动"
    env.assert_no_outstanding()


@pytest.mark.xfail(
    reason="DUTBUG-cbo-zero-missing-wline-drain: cbo.zero reaches completed but does not emit observable wline drain within the current flush window",
    strict=False,
)
def test_api_MemBlock_cbo_zero_flush_zeroes_entire_cacheline(env):
    """
    一条 `cbo.zero` 命中 cacheable line。

    检查点：
      - `cbo.zero` 能 materialize/commit
      - flush 后记录到 `wline` drain
      - 最终整条 cacheline 被清零
    """

    state = _reset_env_and_state(env)
    env.preload_bytes(CBO_ZERO_LINE_ADDR, bytes(((index * 5) + 1) & 0xFF for index in range(64)))

    with _capture_sbuffer_data_activity(env) as trace:
        result = CboZeroFlushSequence(
            StoreTxn.cbo_zero(
                req_id=0,
                sq_ptr=state.sq_ptr,
                addr=CBO_ZERO_LINE_ADDR,
            ),
            assert_no_outstanding=True,
        ).run(env)

    store_view = result.store_result.store_view
    summary = _summarize_sbuffer_data_activity(trace)

    assert store_view is not None and store_view.completed, "`cbo.zero` 未进入 completed"
    assert store_view.is_cbo_zero, "store view 未标记为 `cbo.zero`"
    assert store_view.addr == CBO_ZERO_LINE_ADDR, "`cbo.zero` 地址不匹配"
    assert summary["wline_cycles"], "`cbo.zero` 未观测到 SbufferData.wline=1"
    assert result.drain_summary["drain_event_count"] >= 1, "`cbo.zero` flush 未记录到 drain 事件"
    assert any(event.get("wline") for event in env.memory.drain_log), "`cbo.zero` 未记录到 wline drain"
    assert env.read_cacheline(CBO_ZERO_LINE_ADDR) == bytes(64), "`cbo.zero` 未把整条 cacheline 清零"


@pytest.mark.xfail(
    reason="DUTBUG-cbo-zero-multi-entry-drain: multi-entry cbo.zero reaches committed/wline activity but flush does not fully converge to zeroed drain output",
    strict=False,
)
def test_api_MemBlock_sbuffer_data_cbo_zero_entry_matrix_directed(env):
    """
    16 条唯一 cacheline 的 `cbo.zero` 累积驻留在 sbuffer 中，覆盖 SbufferData 的多 entry `wline` 路径。

    检查点：
      - 16 条 `cbo.zero` 都能进入 committed
      - `SbufferData.wline` 与写 lane 覆盖扩展到完整 16 entry
      - flush 后整批 cacheline 应被清零
    """

    line_addrs = [SBUFFER_DATA_ENTRY_MATRIX_BASE + 0x1000 + (entry * 0x40) for entry in range(16)]
    state = _reset_env_and_state(env)

    for entry, line_addr in enumerate(line_addrs):
        env.preload_bytes(
            line_addr,
            bytes((((entry + 9) * 13) + byte_idx) & 0xFF for byte_idx in range(64)),
        )

    summary = {}
    with _capture_sbuffer_data_activity(env, max_events=2048) as trace:
        try:
            sq_ptr = state.sq_ptr
            committed_views = []
            for req_id, line_addr in enumerate(line_addrs):
                commit_result = ScalarStoreCommitSequence(
                    StoreTxn.cbo_zero(
                        req_id=req_id,
                        sq_ptr=sq_ptr,
                        addr=line_addr,
                    ),
                    expected_mmio=False,
                    require_committed=True,
                    materialize_cycles=300,
                ).run(env)
                committed_views.append(commit_result.committed_store_view)
                sq_ptr = ptr_inc(sq_ptr, env.config.sequence.store_queue_size)

            drain_summary = FlushStoreBuffersSequence(max_cycles=2000).run(env)
        finally:
            summary = _summarize_sbuffer_data_activity(trace)

    for idx, (store, line_addr) in enumerate(zip(committed_views, line_addrs)):
        assert store is not None and store.committed, f"第 {idx} 条 cbo.zero 未进入 committed"
        assert store.is_cbo_zero, f"第 {idx} 条 store 未标记为 cbo.zero"
        assert store.addr == line_addr, f"第 {idx} 条 cbo.zero 地址不匹配"

    assert summary["wline_cycles"], "multi-entry cbo.zero 未观测到 SbufferData.wline=1"
    observed_write_entries = summary["write_req0_wvec_bits"] | summary["write_req1_wvec_bits"]
    assert len(observed_write_entries) >= 8, (
        f"multi-entry cbo.zero 真实 wvec entry 覆盖不足: actual={sorted(observed_write_entries)}"
    )
    assert drain_summary["drain_event_count"] >= 1, "multi-entry cbo.zero flush 未记录到 drain 事件"
    for line_addr in line_addrs:
        assert env.read_cacheline(line_addr) == bytes(64), f"multi-entry cbo.zero 未把 cacheline {line_addr:#x} 清零"


def test_api_MemBlock_misaligned_store_dual_overlap_loads_directed(env):
    """
    一条 misaligned cacheable store 后接两个 overlap load。

    检查点：
      - misaligned store 能进入 committed
      - younger overlap load 能在 commit-boundary 视图上看到更新结果
      - store mask 不再是普通 aligned dword 的 `0xFF`
    """

    initial_low = 0x1111_2222_3333_4444
    initial_high = 0x5555_6666_7777_8888
    store_data = 0xA1A2_A3A4_A5A6_A7A8

    state = _reset_env_and_state(env)
    env.preload_u64(MISALIGNED_WINDOW_ADDRS[0], initial_low)
    env.preload_u64(MISALIGNED_WINDOW_ADDRS[1], initial_high)
    expected_refmem = env.memory.predict_store(MISALIGNED_STORE_ADDR, store_data)

    commit_result = ScalarStoreCommitSequence(
        StoreTxn(
            req_id=0,
            sq_ptr=state.sq_ptr,
            addr=MISALIGNED_STORE_ADDR,
            data=store_data,
        ),
        expected_mmio=False,
        require_committed=True,
        materialize_cycles=300,
    ).run(env)
    committed_store = commit_result.committed_store_view
    assert committed_store is not None and committed_store.committed, "misaligned store 未进入 committed"
    assert committed_store.addr == MISALIGNED_STORE_ADDR, "misaligned store 地址不匹配"
    assert committed_store.mask not in (0, 0xFF), "misaligned store 未形成预期的非 aligned mask"

    low_load = ScalarLoadSequence(
        LoadTxn(
            req_id=1,
            addr=MISALIGNED_WINDOW_ADDRS[0],
            lq_ptr=state.next_lq_ptr,
            sq_ptr=commit_result.store_result.next_sq_ptr,
        ),
        expected_completed_loads=1,
    ).run(env)
    high_load = ScalarLoadSequence(
        LoadTxn(
            req_id=2,
            addr=MISALIGNED_WINDOW_ADDRS[1],
            lq_ptr=low_load.next_lq_ptr,
            sq_ptr=commit_result.store_result.next_sq_ptr,
        ),
        expected_completed_loads=2,
    ).run(env)
    drain_summary = FlushStoreBuffersSequence(max_cycles=800).run(env)

    assert env.memory.read(MISALIGNED_WINDOW_ADDRS[0], 8) == expected_refmem.read(
        MISALIGNED_WINDOW_ADDRS[0], 8
    ), "misaligned store 的低 8B 视图不匹配"
    assert env.memory.read(MISALIGNED_WINDOW_ADDRS[1], 8) == expected_refmem.read(
        MISALIGNED_WINDOW_ADDRS[1], 8
    ), "misaligned store 的高 8B 视图不匹配"
    assert low_load.next_lq_ptr == QueuePtr(flag=0, value=1), "第一个 overlap load 未按预期推进 LQ"
    assert high_load.next_lq_ptr == QueuePtr(flag=0, value=2), "第二个 overlap load 未按预期推进 LQ"
    assert drain_summary["drain_event_count"] >= 1, "misaligned store flush 后未记录到 drain 事件"
    assert drain_summary["touched_byte_count"] >= 8, "misaligned store flush 覆盖字节数不足"
    env.assert_no_outstanding()


@pytest.mark.parametrize("load_issue_lane", (0, 1, 2), ids=("lane0", "lane1", "lane2"))
def test_api_MemBlock_partial_word_store_then_aligned_load_directed(env, load_issue_lane: int):
    """
    一条 4B partial store 后接整 8B younger load，覆盖 3 个 load lane。

    检查点：
      - `StoreTxn.mask=0x0F` 能下沉为真实 partial store
      - older store 在地址/数据都 ready 但尚未退休时保持为 4B store 视图
      - younger 8B load 在 `lane=0/1/2` 上都能命中真实 forward
      - forward 响应的 mask/data 与 partial-store 语义一致
      - younger 8B load 能读到 merge 后结果，且该结果先于 older store 收尾 commit/flush 被验证
    """

    initial_word = 0x1122_3344_5566_7788
    store_data = 0xA1A2_A3A4
    expected_load_data = 0x1122_3344_A1A2_A3A4

    state = _reset_env_and_state(
        env,
        require_issue_lanes=(0, 1, 2),
        require_lq_ready=True,
    )
    env.preload_u64(PARTIAL_STORE_WINDOW_BASE, initial_word)
    expected_refmem = env.memory.predict_store(
        PARTIAL_STORE_WINDOW_BASE,
        store_data,
        mask=0x0F,
    )

    completed_before = env.get_completed_load_count()
    store_txn = StoreTxn(
        req_id=0,
        sq_ptr=state.sq_ptr,
        addr=PARTIAL_STORE_WINDOW_BASE,
        data=store_data,
        mask=0x0F,
    )
    env.backend.prepare(store_txn)
    store_sq_ptr = enqueue_scalar_store(
        env,
        req_id=store_txn.req_id,
        sq_ptr=store_txn.sq_ptr,
        enq_port=store_txn.enq_port,
        rob_idx=store_txn.rob_idx,
    )
    issue_scalar_sta(
        env,
        req_id=store_txn.req_id,
        sq_ptr=store_sq_ptr,
        addr=store_txn.addr,
        lane=store_txn.sta_lane,
        mask=store_txn.mask,
        rob_idx=store_txn.rob_idx,
        ftq_idx_flag=store_txn.resolved_ftq_idx_flag,
        ftq_idx_value=store_txn.resolved_ftq_idx_value,
    )
    env.wait_store_addr_observed(
        sq_idx=store_sq_ptr.value,
        expected_addr=PARTIAL_STORE_WINDOW_BASE,
        max_cycles=300,
    )
    issue_scalar_std(
        env,
        req_id=store_txn.req_id,
        sq_ptr=store_sq_ptr,
        data=store_txn.data,
        lane=store_txn.std_lane,
        mask=store_txn.mask,
        rob_idx=store_txn.rob_idx,
        ftq_idx_flag=store_txn.resolved_ftq_idx_flag,
        ftq_idx_value=store_txn.resolved_ftq_idx_value,
    )
    store_view = env.wait_store_materialized(
        store_sq_ptr.value,
        expected_addr=PARTIAL_STORE_WINDOW_BASE,
        expected_data=store_data,
        expected_mmio=False,
        require_committed=False,
        max_cycles=300,
    )
    assert store_view is not None, "partial word store 未形成可前递的 store 视图"
    assert store_view.mask == 0x0F, "partial word store 未保持 4B mask"

    load_txn = LoadTxn(
        req_id=1,
        addr=PARTIAL_STORE_WINDOW_BASE,
        lq_ptr=state.next_lq_ptr,
        sq_ptr=ptr_inc(store_sq_ptr, env.config.sequence.store_queue_size),
        issue_lane=load_issue_lane,
    )
    transport_stats_before_load = env.get_transport_stats()
    send_load(env, load_txn)
    expect_load(env, load_txn)
    sq_forward_event = wait_sq_forward_event(
        env,
        lane=load_issue_lane,
        expected_data_invalid_valid=0,
        expected_match_invalid=0,
        expected_forward_invalid=0,
        require_forward_mask=True,
        max_cycles=200,
    )
    load_writeback = env.wait_load_writeback_observed(
        rob_idx=load_txn.rob_idx,
        data=expected_load_data,
        max_cycles=200,
    )
    completed_loads = env.wait_completed_load_count(completed_before + 1, max_cycles=64)
    transport_stats_after_load = env.get_transport_stats()
    env.backend.pulse_store_commit(1)
    env.advance_cycles(env.config.sequence.store_settle_cycles)
    committed_store = env.wait_store_materialized(
        store_sq_ptr.value,
        expected_addr=PARTIAL_STORE_WINDOW_BASE,
        expected_data=store_data,
        expected_mmio=False,
        require_committed=True,
        max_cycles=300,
    )
    drain_summary = FlushStoreBuffersSequence(max_cycles=800).run(env)

    assert sq_forward_event["lane"] == load_issue_lane, "partial word store 未命中目标 load lane 的 forward 响应"
    assert sq_forward_event["valid"] == 1, "partial word store 未产生有效 forward 响应"
    assert sq_forward_event["addr_invalid_valid"] == 0, "partial word store 正向 forward 不应返回 addrInvalid"
    assert sq_forward_event["data_invalid_valid"] == 0, "partial word store 正向 forward 不应返回 dataInvalid"
    assert sq_forward_event["match_invalid"] == 0, "partial word store 正向 forward 不应退化成 matchInvalid"
    assert sq_forward_event["forward_invalid"] == 0, "partial word store 正向 forward 不应退化成 forwardInvalid"
    _assert_forward_response_matches_partial_word(sq_forward_event, store_data=store_data)
    assert load_writeback["data"] == expected_load_data, "partial word store forward 后的 load 写回数据不匹配"
    assert completed_loads == completed_before + 1, "partial word store 后的 load 未按预期完成 compare"
    assert ptr_inc(load_txn.lq_ptr, env.config.sequence.load_queue_size) == QueuePtr(flag=0, value=1), (
        "partial word store 后的 load 未按预期推进 LQ"
    )
    assert committed_store is not None and committed_store.committed, "partial word store 收尾未进入 committed"
    assert transport_stats_after_load["outer_request_count"] == transport_stats_before_load["outer_request_count"], (
        "partial word store forward 不应走 outer/uncache 路径"
    )
    assert env.memory.read(PARTIAL_STORE_WINDOW_BASE, 8) == expected_refmem.read(
        PARTIAL_STORE_WINDOW_BASE, 8
    ), "partial word store merge 结果不匹配"
    assert drain_summary["touched_byte_count"] >= 4, "partial word store flush 覆盖字节数不足"
    env.assert_no_outstanding()


@pytest.mark.parametrize("load_issue_lane", (1, 2), ids=("lane1", "lane2"))
def test_api_MemBlock_sbuffer_forward_committed_partial_word_quarter_matrix_directed(
    env,
    load_issue_lane: int,
):
    """
    committed 但尚未 flush 的 partial-word store，在 lane1/lane2 上命中真实 sbuffer forward。

    检查点：
      - 4 个 quarter 的 committed partial-word store 都留在 sbuffer 中
      - younger load 在 `lane=1/2` 上命中 `MemBlock_inner_sbuffer_io_forward_*_s2Resp`
      - sbuffer forward 的 mask/data 与 4B partial-store 语义一致
      - load 完成前不走 outer drain，最终 flush 后 cacheline 与参考内存一致
    """

    line_addr = SBUFFER_FORWARD_LINE_ADDR + ((load_issue_lane - 1) * 0x80)
    initial_line = bytes(((0x31 + (index * 7)) & 0xFF) for index in range(64))
    quarter_store_data = (
        0xA1A2_A3A4,
        0xB1B2_B3B4,
        0xC1C2_C3C4,
        0xD1D2_D3D4,
    )
    delay_candidates = tuple(range(12))
    failure_records = []

    for quarter, store_data in enumerate(quarter_store_data):
        addr = line_addr + (quarter * 0x10)
        quarter_succeeded = False
        quarter_failures = []

        for commit_to_load_delay in delay_candidates:
            state = _reset_env_and_state(
                env,
                require_issue_lanes=(0, 1, 2),
                require_lq_ready=True,
            )
            env.preload_bytes(line_addr, initial_line)
            expected_refmem = env.memory.predict_store(addr, store_data, mask=0x0F)
            completed_before = env.get_completed_load_count()

            store_result = ScalarStoreSequence(
                StoreTxn(
                    req_id=quarter,
                    sq_ptr=state.sq_ptr,
                    addr=addr,
                    data=store_data,
                    mask=0x0F,
                ),
                expected_mmio=False,
                require_committed=False,
                materialize_cycles=300,
            ).run(env)
            assert store_result.store_view is not None, (
                f"quarter {quarter} partial-word store 未形成可观测 store 视图"
            )
            assert int(store_result.store_view.mask) == 0x0F, (
                f"quarter {quarter} partial-word store 未保持 4B mask"
            )

            load_txn = LoadTxn(
                req_id=0x80 + load_issue_lane * 0x10 + quarter,
                addr=addr,
                lq_ptr=state.next_lq_ptr,
                sq_ptr=store_result.next_sq_ptr,
                issue_lane=load_issue_lane,
            )
            env.backend.prepare(load_txn)
            enqueue_scalar_load(
                env,
                req_id=load_txn.req_id,
                lq_ptr=load_txn.lq_ptr,
                sq_ptr=load_txn.sq_ptr,
                enq_port=load_txn.enq_port,
                rob_idx=load_txn.rob_idx,
            )
            expect_load(env, load_txn)
            expected_load_data = expected_refmem.read(addr, 8)
            transport_stats_before = env.get_transport_stats()

            try:
                with _capture_forward_activity(env, max_events=128) as trace:
                    env.backend.pulse_store_commit(1)
                    if commit_to_load_delay > 0:
                        env.advance_cycles(commit_to_load_delay)
                    issue_scalar_load(
                        env,
                        req_id=load_txn.req_id,
                        addr=load_txn.addr,
                        lq_ptr=load_txn.lq_ptr,
                        sq_ptr=load_txn.sq_ptr,
                        lane=load_txn.issue_lane,
                        store_set_hit=load_txn.store_set_hit,
                        load_wait_bit=load_txn.load_wait_bit,
                        load_wait_strict=load_txn.load_wait_strict,
                        wait_for_rob_idx=load_txn.wait_for_rob_idx,
                        rob_idx=load_txn.rob_idx,
                        pdest=load_txn.resolved_pdest,
                        ftq_idx_flag=load_txn.resolved_ftq_idx_flag,
                        ftq_idx_value=load_txn.resolved_ftq_idx_value,
                        pc=load_txn.resolved_pc,
                    )
                    load_writeback = env.wait_load_writeback_observed(
                        rob_idx=load_txn.rob_idx,
                        data=expected_load_data,
                        max_cycles=200,
                    )
                    completed_loads = env.wait_completed_load_count(completed_before + 1, max_cycles=64)
                transport_stats_after = env.get_transport_stats()
            except TimeoutError as exc:
                quarter_failures.append(
                    {
                        "quarter": quarter,
                        "delay": commit_to_load_delay,
                        "error": str(exc),
                        "trace": _summarize_forward_activity(trace),
                    }
                )
                continue

            sbuffer_forward_event = _find_forward_event(
                trace,
                channel="sbuffer",
                lane=load_issue_lane,
                expected_match_invalid=0,
                require_forward_mask=True,
            )
            if sbuffer_forward_event is None:
                quarter_failures.append(
                    {
                        "quarter": quarter,
                        "delay": commit_to_load_delay,
                        "error": "missing sbuffer forward event",
                        "trace": _summarize_forward_activity(trace),
                    }
                )
                continue

            committed_store = env.wait_store_materialized(
                store_result.allocated_sq_ptr.value,
                expected_addr=addr,
                expected_data=store_data,
                expected_mmio=False,
                require_committed=True,
                max_cycles=300,
            )
            drain_summary = FlushStoreBuffersSequence(max_cycles=800).run(env)

            assert sbuffer_forward_event["lane"] == load_issue_lane, "sbuffer forward 未命中目标 load lane"
            assert sbuffer_forward_event["valid"] == 1, "sbuffer forward 响应无效"
            assert sbuffer_forward_event["match_invalid"] == 0, "正常 sbuffer forward 不应退化为 matchInvalid"
            assert any(int(bit) for bit in sbuffer_forward_event["forward_mask"]), (
                "sbuffer forward 未返回有效 forward mask"
            )
            _assert_forward_response_matches_partial_word(
                sbuffer_forward_event,
                store_data=store_data,
            )
            assert load_writeback["data"] == expected_load_data, "sbuffer forward 后的 load 写回数据不匹配"
            assert completed_loads == completed_before + 1, "sbuffer forward 后的 load 未按预期完成 compare"
            assert committed_store is not None and committed_store.committed, (
                "sbuffer forward 命中后，store 未进入 committed"
            )
            assert transport_stats_after["outer_request_count"] == transport_stats_before["outer_request_count"], (
                "sbuffer forward load 不应退化成 outer/uncache 路径"
            )
            assert env.read_cacheline(line_addr) == expected_refmem.read_cacheline(line_addr, 64), (
                "sbuffer forward quarter 场景的 cacheline 最终结果不匹配"
            )
            assert drain_summary["drain_event_count"] >= 1, "sbuffer forward quarter 场景未记录到 drain 事件"
            assert drain_summary["touched_byte_count"] >= 4, "sbuffer forward quarter 场景 flush 覆盖字节数不足"
            env.assert_no_outstanding()

            quarter_succeeded = True
            break

        failure_records.extend(quarter_failures)
        assert quarter_succeeded, (
            f"quarter {quarter} 未找到稳定的 sbuffer forward 窗口: "
            f"{quarter_failures}"
        )


@pytest.mark.parametrize("load_issue_lane", (1, 2), ids=("lane1", "lane2"))
def test_api_MemBlock_sbuffer_forward_entry2_partial_word_targeted(
    env,
    load_issue_lane: int,
):
    """
    先把目标 cacheline 真实预热到 sbuffer entry2，再在 lane1/lane2 上定向命中 entry2 forward。

    检查点：
      - 预热阶段能稳定拿到 `entry2 -> line_addr` 映射
      - 目标 quarter0 partial-word store 继续命中 `wvec[2]`
      - younger load 在 `lane=1/2` 上命中真实 sbuffer forward，覆盖 entry2 的 `vtag_matches`
    """

    prewarm_base = SBUFFER_FORWARD_LINE_ADDR + 0x200 + ((load_issue_lane - 1) * 0x400)
    preload_seed = 29 + load_issue_lane
    state = _reset_env_and_state(
        env,
        require_issue_lanes=(0, 1, 2),
        require_lq_ready=True,
    )
    prewarm = _prewarm_sbuffer_entry_map(
        env,
        state.sq_ptr,
        base_addr=prewarm_base,
        line_count=6,
        req_id_base=4000 + (load_issue_lane * 0x100),
        preload_seed=preload_seed,
    )
    sq_ptr = prewarm["next_sq_ptr"]
    entry_to_line = prewarm["entry_to_line"]

    assert 2 in entry_to_line, f"预热阶段未获得 entry2 映射: actual={sorted(entry_to_line)}"

    target_line_addr = entry_to_line[2]
    target_line_index = (target_line_addr - prewarm_base) // 0x40
    expected_target_line = bytearray(
        (((target_line_index + preload_seed) * 19) + byte_idx) & 0xFF
        for byte_idx in range(64)
    )
    _apply_masked_store_to_line_bytes(
        expected_target_line,
        line_addr=target_line_addr,
        addr=target_line_addr,
        data=0x1111_2222_3333_4444 ^ (target_line_index << 20),
        mask=0xFF,
    )
    store_data = 0x91_92_93_94
    completed_before = env.get_completed_load_count()
    addr = target_line_addr
    with _capture_sbuffer_data_activity(env, max_events=64) as trace:
        _, sq_ptr = _commit_scalar_store(
            env,
            sq_ptr,
            req_id=0x5000 + (load_issue_lane * 0x100),
            addr=addr,
            data=store_data,
            mask=0x0F,
        )
    _apply_masked_store_to_line_bytes(
        expected_target_line,
        line_addr=target_line_addr,
        addr=addr,
        data=store_data,
        mask=0x0F,
    )
    summary = _summarize_sbuffer_data_activity(trace)
    observed_wvec_bits = summary["write_req0_wvec_bits"] | summary["write_req1_wvec_bits"]
    observed_triples = (
        summary["write_req0_wvec_offset_mask_triples"]
        | summary["write_req1_wvec_offset_mask_triples"]
    )
    assert 2 in observed_wvec_bits, (
        f"entry2 partial-word merge 未观测到 entry2: actual={sorted(observed_wvec_bits)}"
    )
    assert (2, 0, 3) in observed_triples, (
        f"entry2 partial-word merge 未命中 entry2 quarter0 byte3: actual={sorted(observed_triples)}"
    )

    load_txn = LoadTxn(
        req_id=0x5800 + (load_issue_lane * 0x100),
        addr=addr,
        lq_ptr=state.next_lq_ptr,
        sq_ptr=sq_ptr,
        issue_lane=load_issue_lane,
    )
    env.backend.prepare(load_txn)
    enqueue_scalar_load(
        env,
        req_id=load_txn.req_id,
        lq_ptr=load_txn.lq_ptr,
        sq_ptr=load_txn.sq_ptr,
        enq_port=load_txn.enq_port,
        rob_idx=load_txn.rob_idx,
    )
    expect_load(env, load_txn)

    expected_load_data = int.from_bytes(expected_target_line[:8], "little")
    transport_stats_before = env.get_transport_stats()

    with _capture_forward_activity(env, max_events=128) as trace:
        issue_scalar_load(
            env,
            req_id=load_txn.req_id,
            addr=load_txn.addr,
            lq_ptr=load_txn.lq_ptr,
            sq_ptr=load_txn.sq_ptr,
            lane=load_txn.issue_lane,
            store_set_hit=load_txn.store_set_hit,
            load_wait_bit=load_txn.load_wait_bit,
            load_wait_strict=load_txn.load_wait_strict,
            wait_for_rob_idx=load_txn.wait_for_rob_idx,
            rob_idx=load_txn.rob_idx,
            pdest=load_txn.resolved_pdest,
            ftq_idx_flag=load_txn.resolved_ftq_idx_flag,
            ftq_idx_value=load_txn.resolved_ftq_idx_value,
            pc=load_txn.resolved_pc,
        )
        load_writeback = env.wait_load_writeback_observed(
            rob_idx=load_txn.rob_idx,
            data=expected_load_data,
            max_cycles=200,
        )
        completed_loads = env.wait_completed_load_count(completed_before + 1, max_cycles=64)
    transport_stats_after = env.get_transport_stats()

    sbuffer_forward_event = _find_forward_event(
        trace,
        channel="sbuffer",
        lane=load_issue_lane,
        expected_match_invalid=0,
        require_forward_mask=True,
    )
    assert sbuffer_forward_event is not None, (
        f"entry2 定向场景未观测到 lane{load_issue_lane} sbuffer forward: "
        f"{_summarize_forward_activity(trace)}"
    )
    _assert_forward_response_matches_expected_merge(
        sbuffer_forward_event,
        expected_data=expected_load_data,
    )
    assert load_writeback["data"] == expected_load_data, "entry2 sbuffer forward 后的 load 写回数据不匹配"
    assert completed_loads == completed_before + 1, "entry2 sbuffer forward 后的 load 未按预期完成 compare"
    assert transport_stats_after["outer_request_count"] == transport_stats_before["outer_request_count"], (
        "entry2 sbuffer forward load 不应退化成 outer/uncache 路径"
    )

    drain_summary = FlushStoreBuffersSequence(max_cycles=1000).run(env)

    assert env.read_cacheline(target_line_addr) == bytes(expected_target_line), (
        f"entry2 定向 sbuffer forward 场景的目标 cacheline 最终结果不匹配: line_addr={target_line_addr:#x}"
    )
    assert drain_summary["drain_event_count"] >= 1, "entry2 定向 sbuffer forward 场景未记录到 drain 事件"
    assert drain_summary["touched_byte_count"] >= 8, "entry2 定向 sbuffer forward 场景 flush 覆盖字节数不足"
    env.assert_no_outstanding()


@pytest.mark.parametrize("load_issue_lane", (1, 2), ids=("lane1", "lane2"))
def test_api_MemBlock_sbuffer_forward_multi_entry_matrix_directed(
    env,
    load_issue_lane: int,
):
    """
    单次预热 16 个 sbuffer entry，并在 lane1/lane2 上定向命中多个 entry 的 sbuffer forward。

    检查点：
      - 预热阶段能稳定建立 `entry -> line_addr` 映射
      - 多个目标 entry 都能在真实 DUT 上形成 committed update + younger load 的 sbuffer forward
      - 低半与高半窗口都被覆盖，避免 `forward_mask_candidate_reg_*` 只停留在单一字节分布
    """

    _run_sbuffer_forward_entry_matrix(
        env,
        load_issue_lane=load_issue_lane,
        entry_update_specs=(
            (2, 0x0),
            (3, 0x10),
            (4, 0x20),
            (5, 0x30),
        ),
        base_addr=SBUFFER_FORWARD_LINE_ADDR + 0x800 + ((load_issue_lane - 1) * 0x800),
        req_id_base=0x7000 + (load_issue_lane * 0x400),
        preload_seed=41 + load_issue_lane,
    )


def test_api_MemBlock_partial_byte_store_high_offset_directed(env):
    """
    一条高偏移 1B partial store 后接整 8B load。

    检查点：
      - `StoreTxn.mask=0x01` 能驱动真实 byte store
      - 高偏移 byte store 不会被错误扩成整 dword 写
      - younger load 能看到单字节 merge 结果
    """

    initial_word = 0x0102_0304_0506_0708
    store_data = 0xAB
    store_addr = PARTIAL_STORE_WINDOW_BASE + 0x5

    state = _reset_env_and_state(env)
    env.preload_u64(PARTIAL_STORE_WINDOW_BASE, initial_word)
    expected_refmem = env.memory.predict_store(
        store_addr,
        store_data,
        mask=0x01,
    )

    commit_result = ScalarStoreCommitSequence(
        StoreTxn(
            req_id=0,
            sq_ptr=state.sq_ptr,
            addr=store_addr,
            data=store_data,
            mask=0x01,
        ),
        expected_mmio=False,
        require_committed=True,
        materialize_cycles=300,
    ).run(env)
    committed_store = commit_result.committed_store_view
    assert committed_store is not None and committed_store.committed, "high-offset byte store 未进入 committed"
    assert committed_store.mask == (1 << (store_addr & 0x7)), "high-offset byte store 未落在目标字节位置"

    load_result = ScalarLoadSequence(
        LoadTxn(
            req_id=1,
            addr=PARTIAL_STORE_WINDOW_BASE,
            lq_ptr=state.next_lq_ptr,
            sq_ptr=commit_result.store_result.next_sq_ptr,
        ),
        expected_completed_loads=1,
    ).run(env)
    drain_summary = FlushStoreBuffersSequence(max_cycles=800).run(env)

    assert load_result.next_lq_ptr == QueuePtr(flag=0, value=1), "high-offset byte store 后的 load 未按预期推进 LQ"
    assert env.memory.read(PARTIAL_STORE_WINDOW_BASE, 8) == expected_refmem.read(
        PARTIAL_STORE_WINDOW_BASE, 8
    ), "high-offset byte store merge 结果不匹配"
    assert drain_summary["touched_byte_count"] >= 1, "high-offset byte store flush 覆盖字节数不足"
    env.assert_no_outstanding()


def test_api_MemBlock_partial_byte_merge_same_dword_directed(env):
    """
    同一 dword 上连续执行多条 byte store，再由整 8B load 验证 merge 结果。

    检查点：
      - 多次 1B partial store 都能稳定进入 committed
      - 同地址多次 merge 后 younger full load 能看到拼接后的最终值
      - flush/drain 至少覆盖所有被写字节
    """

    initial_word = 0x8877_6655_4433_2211
    byte_updates = [
        (PARTIAL_STORE_WINDOW_BASE + 0x0, 0xAA),
        (PARTIAL_STORE_WINDOW_BASE + 0x2, 0xBB),
        (PARTIAL_STORE_WINDOW_BASE + 0x5, 0xCC),
        (PARTIAL_STORE_WINDOW_BASE + 0x7, 0xDD),
    ]

    state = _reset_env_and_state(env)
    env.preload_u64(PARTIAL_STORE_WINDOW_BASE, initial_word)

    sq_ptr = state.sq_ptr
    expected_refmem = env.memory.fork_ref_memory()
    committed_masks = []

    for req_id, (addr, data) in enumerate(byte_updates):
        commit_result, sq_ptr = _commit_scalar_store(
            env,
            sq_ptr,
            req_id=req_id,
            addr=addr,
            data=data,
            mask=0x01,
        )
        committed_store = commit_result.committed_store_view
        committed_masks.append(int(committed_store.mask))
        _apply_store_stimulus_to_ref_memory(
            expected_refmem,
            addr=addr,
            data=data,
            mask=0x01,
        )

    load_result = ScalarLoadSequence(
        LoadTxn(
            req_id=len(byte_updates),
            addr=PARTIAL_STORE_WINDOW_BASE,
            lq_ptr=state.next_lq_ptr,
            sq_ptr=sq_ptr,
        ),
        expected_completed_loads=1,
    ).run(env)
    drain_summary = FlushStoreBuffersSequence(max_cycles=800).run(env)

    assert committed_masks == [0x01, 0x04, 0x20, 0x80], "byte merge store 未落在预期字节 lane"
    assert load_result.next_lq_ptr == QueuePtr(flag=0, value=1), "byte merge 场景下 load 未按预期推进 LQ"
    assert env.memory.read(PARTIAL_STORE_WINDOW_BASE, 8) == expected_refmem.read(
        PARTIAL_STORE_WINDOW_BASE, 8
    ), "byte merge 后最终 dword 结果不匹配"
    assert drain_summary["touched_byte_count"] >= len(byte_updates), "byte merge flush 覆盖字节数不足"
    env.assert_no_outstanding()


def test_api_MemBlock_partial_word_then_high_offset_byte_overwrite_directed(env):
    """
    先做 4B partial store，再做高偏移 1B overwrite，最后用整 8B load 验证。

    检查点：
      - 4B partial store 与高偏移 1B overwrite 都能进入 committed
      - 两条 partial store 的 mask 分别保持为 0x0F / 高偏移单字节
      - younger full load 能看到链式 merge 后的最终视图
    """

    initial_word = 0x1020_3040_5060_7080
    partial_word_data = 0xA1A2_A3A4
    overwrite_addr = PARTIAL_STORE_WINDOW_BASE + 0x7
    overwrite_data = 0xEE

    state = _reset_env_and_state(env)
    env.preload_u64(PARTIAL_STORE_WINDOW_BASE, initial_word)
    expected_refmem = env.memory.fork_ref_memory()

    first_result, sq_ptr = _commit_scalar_store(
        env,
        state.sq_ptr,
        req_id=0,
        addr=PARTIAL_STORE_WINDOW_BASE,
        data=partial_word_data,
        mask=0x0F,
    )
    _apply_store_stimulus_to_ref_memory(
        expected_refmem,
        addr=PARTIAL_STORE_WINDOW_BASE,
        data=partial_word_data,
        mask=0x0F,
    )
    second_result, sq_ptr = _commit_scalar_store(
        env,
        sq_ptr,
        req_id=1,
        addr=overwrite_addr,
        data=overwrite_data,
        mask=0x01,
    )
    _apply_store_stimulus_to_ref_memory(
        expected_refmem,
        addr=overwrite_addr,
        data=overwrite_data,
        mask=0x01,
    )

    load_result = ScalarLoadSequence(
        LoadTxn(
            req_id=2,
            addr=PARTIAL_STORE_WINDOW_BASE,
            lq_ptr=state.next_lq_ptr,
            sq_ptr=sq_ptr,
        ),
        expected_completed_loads=1,
    ).run(env)
    drain_summary = FlushStoreBuffersSequence(max_cycles=800).run(env)

    assert first_result.committed_store_view.mask == 0x0F, "链式 partial store 的首条 4B mask 异常"
    assert second_result.committed_store_view.mask == 0x80, "链式 partial store 的高偏移 1B mask 异常"
    assert load_result.next_lq_ptr == QueuePtr(flag=0, value=1), "链式 partial store 后的 load 未按预期推进 LQ"
    assert env.memory.read(PARTIAL_STORE_WINDOW_BASE, 8) == expected_refmem.read(
        PARTIAL_STORE_WINDOW_BASE, 8
    ), "4B partial + 高偏移 1B overwrite 的最终结果不匹配"
    assert drain_summary["touched_byte_count"] >= 5, "链式 partial store flush 覆盖字节数不足"
    env.assert_no_outstanding()


def test_api_MemBlock_sequential_partial_byte_lanes_same_dword_directed(env):
    """
    同一 dword 上按连续 lane 依次执行 1B partial store。

    检查点：
      - 依次命中低四个 byte lane，对应 mask 为 0x01/0x02/0x04/0x08
      - younger full load 能看到顺序 merge 后的最终 dword
      - flush/drain 至少覆盖四个被写字节
    """

    initial_word = 0x8877_6655_4433_2211
    byte_updates = [
        (PARTIAL_STORE_WINDOW_BASE + 0x0, 0xA1),
        (PARTIAL_STORE_WINDOW_BASE + 0x1, 0xB2),
        (PARTIAL_STORE_WINDOW_BASE + 0x2, 0xC3),
        (PARTIAL_STORE_WINDOW_BASE + 0x3, 0xD4),
    ]

    state = _reset_env_and_state(env)
    env.preload_u64(PARTIAL_STORE_WINDOW_BASE, initial_word)

    sq_ptr = state.sq_ptr
    expected_refmem = env.memory.fork_ref_memory()
    committed_masks = []

    for req_id, (addr, data) in enumerate(byte_updates):
        commit_result, sq_ptr = _commit_scalar_store(
            env,
            sq_ptr,
            req_id=req_id,
            addr=addr,
            data=data,
            mask=0x01,
        )
        committed_masks.append(int(commit_result.committed_store_view.mask))
        _apply_store_stimulus_to_ref_memory(
            expected_refmem,
            addr=addr,
            data=data,
            mask=0x01,
        )

    load_result = ScalarLoadSequence(
        LoadTxn(
            req_id=len(byte_updates),
            addr=PARTIAL_STORE_WINDOW_BASE,
            lq_ptr=state.next_lq_ptr,
            sq_ptr=sq_ptr,
        ),
        expected_completed_loads=1,
    ).run(env)
    drain_summary = FlushStoreBuffersSequence(max_cycles=800).run(env)

    assert committed_masks == [0x01, 0x02, 0x04, 0x08], "连续 byte lane partial store 未落在预期 mask"
    assert load_result.next_lq_ptr == QueuePtr(flag=0, value=1), "连续 byte lane 场景下 load 未按预期推进 LQ"
    assert env.memory.read(PARTIAL_STORE_WINDOW_BASE, 8) == expected_refmem.read(
        PARTIAL_STORE_WINDOW_BASE, 8
    ), "连续 byte lane partial store 后最终 dword 结果不匹配"
    assert drain_summary["touched_byte_count"] >= len(byte_updates), "连续 byte lane flush 覆盖字节数不足"
    env.assert_no_outstanding()


def test_api_MemBlock_full_store_then_partial_overwrite_directed(env):
    """
    先 full store，再做高偏移 partial overwrite。

    检查点：
      - full store 与后续 partial overwrite 都能进入 committed
      - partial overwrite 只覆盖目标字节，不破坏其余旧值
      - younger full load 能看到 overwrite 后的最终视图
    """

    base_data = 0x1122_3344_5566_7788
    overwrite_addr = PARTIAL_STORE_WINDOW_BASE + 0x6
    overwrite_data = 0xEE

    state = _reset_env_and_state(env)
    expected_refmem = env.memory.fork_ref_memory()

    first_result, sq_ptr = _commit_scalar_store(
        env,
        state.sq_ptr,
        req_id=0,
        addr=PARTIAL_STORE_WINDOW_BASE,
        data=base_data,
    )
    _apply_store_stimulus_to_ref_memory(
        expected_refmem,
        addr=PARTIAL_STORE_WINDOW_BASE,
        data=base_data,
    )
    second_result, sq_ptr = _commit_scalar_store(
        env,
        sq_ptr,
        req_id=1,
        addr=overwrite_addr,
        data=overwrite_data,
        mask=0x01,
    )
    _apply_store_stimulus_to_ref_memory(
        expected_refmem,
        addr=overwrite_addr,
        data=overwrite_data,
        mask=0x01,
    )

    load_result = ScalarLoadSequence(
        LoadTxn(
            req_id=2,
            addr=PARTIAL_STORE_WINDOW_BASE,
            lq_ptr=state.next_lq_ptr,
            sq_ptr=sq_ptr,
        ),
        expected_completed_loads=1,
    ).run(env)
    drain_summary = FlushStoreBuffersSequence(max_cycles=800).run(env)

    assert first_result.committed_store_view.mask == 0xFF, "base full store mask 异常"
    assert second_result.committed_store_view.mask == (1 << (overwrite_addr & 0x7)), (
        "partial overwrite 未落在预期字节 lane"
    )
    assert load_result.next_lq_ptr == QueuePtr(flag=0, value=1), "overwrite 场景下 load 未按预期推进 LQ"
    assert env.memory.read(PARTIAL_STORE_WINDOW_BASE, 8) == expected_refmem.read(
        PARTIAL_STORE_WINDOW_BASE, 8
    ), "full store + partial overwrite 结果不匹配"
    assert drain_summary["touched_byte_count"] >= 8, "overwrite 场景 flush 覆盖字节数不足"
    env.assert_no_outstanding()


def test_api_MemBlock_interleaved_partial_stores_two_addresses_directed(env):
    """
    两个地址交织执行 partial-store，再分别用 full load 检查最终 merge 结果。

    检查点：
      - partial-store 不会因为地址交织而串扰
      - 两个地址都能形成各自独立的 merge 视图
      - 两笔 younger load 都能在 commit-boundary 视图上看到最终值
    """

    initial_words = (
        0x0102_0304_0506_0708,
        0x1112_1314_1516_1718,
    )
    partial_ops = [
        (PARTIAL_STORE_WINDOW_ADDRS[0] + 0x1, 0xAAAA, 0x03),
        (PARTIAL_STORE_WINDOW_ADDRS[1] + 0x0, 0xBB, 0x01),
        (PARTIAL_STORE_WINDOW_ADDRS[0] + 0x6, 0xCC, 0x01),
        (PARTIAL_STORE_WINDOW_ADDRS[1] + 0x4, 0xDDDD, 0x03),
    ]

    state = _reset_env_and_state(env)
    env.preload_u64(PARTIAL_STORE_WINDOW_ADDRS[0], initial_words[0])
    env.preload_u64(PARTIAL_STORE_WINDOW_ADDRS[1], initial_words[1])

    sq_ptr = state.sq_ptr
    expected_refmem = env.memory.fork_ref_memory()

    for req_id, (addr, data, mask) in enumerate(partial_ops):
        commit_result, sq_ptr = _commit_scalar_store(
            env,
            sq_ptr,
            req_id=req_id,
            addr=addr,
            data=data,
            mask=mask,
        )
        _apply_store_stimulus_to_ref_memory(
            expected_refmem,
            addr=addr,
            data=data,
            mask=mask,
        )

    first_load = ScalarLoadSequence(
        LoadTxn(
            req_id=len(partial_ops),
            addr=PARTIAL_STORE_WINDOW_ADDRS[0],
            lq_ptr=state.next_lq_ptr,
            sq_ptr=sq_ptr,
        ),
        expected_completed_loads=1,
    ).run(env)
    second_load = ScalarLoadSequence(
        LoadTxn(
            req_id=len(partial_ops) + 1,
            addr=PARTIAL_STORE_WINDOW_ADDRS[1],
            lq_ptr=first_load.next_lq_ptr,
            sq_ptr=sq_ptr,
        ),
        expected_completed_loads=2,
    ).run(env)
    drain_summary = FlushStoreBuffersSequence(max_cycles=800).run(env)

    assert first_load.next_lq_ptr == QueuePtr(flag=0, value=1), "第一个交织 partial-store load 未按预期推进 LQ"
    assert second_load.next_lq_ptr == QueuePtr(flag=0, value=2), "第二个交织 partial-store load 未按预期推进 LQ"
    assert env.memory.read(PARTIAL_STORE_WINDOW_ADDRS[0], 8) == expected_refmem.read(
        PARTIAL_STORE_WINDOW_ADDRS[0], 8
    ), (
        "交织 partial-store 后窗口 0 结果不匹配"
    )
    assert env.memory.read(PARTIAL_STORE_WINDOW_ADDRS[1], 8) == expected_refmem.read(
        PARTIAL_STORE_WINDOW_ADDRS[1], 8
    ), (
        "交织 partial-store 后窗口 1 结果不匹配"
    )
    assert drain_summary["touched_byte_count"] >= 6, "交织 partial-store flush 覆盖字节数不足"
    env.assert_no_outstanding()


def test_api_MemBlock_store_burst_then_interleaved_load_before_flush(env):
    """
    多条 cacheable store burst 后，先执行 younger load，再统一 flush/drain。

    检查点：
      - burst 中的 store 都能进入 committed
      - younger load 能在 flush 前完成 compare
      - 最终统一 flush 时能把整批 store 收口到 drain
    """

    burst_addrs = [
        STORE_ADDRS[0],
        STORE_ADDRS[1],
        STORE_ADDRS[2],
        STORE_ADDRS[2] + 0x8,
    ]
    burst_data = [
        0x0102_0304_0506_0708,
        0x1112_1314_1516_1718,
        0x2122_2324_2526_2728,
        0x3132_3334_3536_3738,
    ]

    state = _reset_env_and_state(env)
    sq_ptr = state.sq_ptr
    committed_views = []

    for req_id, (addr, data_word) in enumerate(zip(burst_addrs, burst_data)):
        commit_result = ScalarStoreCommitSequence(
            StoreTxn(req_id=req_id, sq_ptr=sq_ptr, addr=addr, data=data_word),
            expected_mmio=False,
            require_committed=True,
            materialize_cycles=300,
        ).run(env)
        committed_views.append(commit_result.committed_store_view)
        sq_ptr = ptr_inc(sq_ptr, env.config.sequence.store_queue_size)

    load_result = ScalarLoadSequence(
        LoadTxn(
            req_id=len(burst_data),
            addr=burst_addrs[2],
            lq_ptr=state.next_lq_ptr,
            sq_ptr=sq_ptr,
        ),
        expected_completed_loads=1,
    ).run(env)
    drain_summary = FlushStoreBuffersSequence(max_cycles=1000).run(env)

    for idx, (store, addr, data_word) in enumerate(zip(committed_views, burst_addrs, burst_data)):
        assert store is not None and store.committed, f"burst 中第 {idx} 条 store 未进入 committed"
        assert store.addr == addr, f"burst 中第 {idx} 条 store 地址不匹配"
        assert _store_data_low64_matches(store, data_word), f"burst 中第 {idx} 条 store 数据不匹配"

    assert load_result.next_lq_ptr == QueuePtr(flag=0, value=1), "interleaved load 未按预期推进 LQ"
    assert sq_ptr == ptr_inc(state.sq_ptr, env.config.sequence.store_queue_size, step=len(burst_data)), (
        "store burst 后 SQ 指针推进异常"
    )
    assert drain_summary["drain_event_count"] >= 1, "store burst 统一 flush 后未记录到 drain 事件"
    assert drain_summary["touched_byte_count"] >= 32, "store burst 统一 flush 覆盖字节数不足"
    env.assert_no_outstanding()


def test_api_MemBlock_sbuffer_data_wide_burst_mask_flush_directed(env):
    """
    宽 sbuffer burst 后穿插 younger load，覆盖多 lane writeReq 与 mask-flush。

    检查点：
      - 多条不同 cacheline 的 store 能在 flush 前同时留在 sbuffer 中
      - `accessIdx_*` 与 `io_dcache_main_pipe_hit_resp_bits_id` 能覆盖较宽的 lane 集合
      - younger load 与最终 flush 后的内存结果保持一致
    """

    burst_ops = [
        (
            SBUFFER_DATA_BURST_BASE + (index * 0x40) + ((index % 4) * 0x10),
            0x0101_0101_0101_0101 * (index + 1),
        )
        for index in range(12)
    ]

    state = _reset_env_and_state(env)
    expected_refmem = env.memory.fork_ref_memory()

    with _capture_sbuffer_data_activity(env, max_events=1024) as trace:
        sq_ptr = state.sq_ptr
        for req_id, (addr, data) in enumerate(burst_ops):
            _, sq_ptr = _commit_scalar_store(
                env,
                sq_ptr,
                req_id=req_id,
                addr=addr,
                data=data,
            )
            _apply_store_stimulus_to_ref_memory(
                expected_refmem,
                addr=addr,
                data=data,
            )

        next_lq_ptr = state.next_lq_ptr
        for completed_loads, (addr, _) in enumerate(burst_ops, start=1):
            load_result = ScalarLoadSequence(
                LoadTxn(
                    req_id=len(burst_ops) + completed_loads - 1,
                    addr=addr,
                    lq_ptr=next_lq_ptr,
                    sq_ptr=sq_ptr,
                ),
                expected_completed_loads=completed_loads,
            ).run(env)
            next_lq_ptr = load_result.next_lq_ptr

        drain_summary = FlushStoreBuffersSequence(max_cycles=1200).run(env)

    summary = _summarize_sbuffer_data_activity(trace)

    for addr, _ in burst_ops:
        assert env.memory.read(addr, 8) == expected_refmem.read(addr, 8), (
            f"wide sbuffer burst 场景的地址 {addr:#x} 最终结果不匹配"
        )

    assert len(summary["write_lane_bits"]) >= 8, (
        f"wide sbuffer burst 写 lane 覆盖不足: actual={sorted(summary['write_lane_bits'])}"
    )
    assert summary["mask_flush_count"] > 0, "wide sbuffer burst 未观测到 mask-flush 活动"
    assert len(summary["mask_flush_lane_bits"]) >= 4, (
        f"wide sbuffer burst mask-flush lane 覆盖不足: actual={sorted(summary['mask_flush_lane_bits'])}"
    )
    assert drain_summary["drain_event_count"] >= 1, "wide sbuffer burst flush 未记录到 drain 事件"
    env.assert_no_outstanding()


def test_api_MemBlock_store_queue_two_wave_commit_frontier_residency_directed(env):
    """
    先入队两波 store，只提交第一波并先执行 younger load，再提交第二波统一 flush。

    检查点：
      - 第一波 committed 时，第二波 store 仍保留在 SQ 中但未 committed
      - younger load 能在 delayed-flush 窗口内完成第一波 compare
      - 第二波提交后，新增 cacheline 与 cross-16B partial 路径都能闭环到最终 drain
    """

    first_line = STORE_RESIDENCY_TWO_WAVE_BASE
    second_line = STORE_RESIDENCY_TWO_WAVE_BASE + 0x40
    preload_first_line = bytes(((index * 7) + 0x11) & 0xFF for index in range(64))
    preload_second_line = bytes(((index * 9) + 0x33) & 0xFF for index in range(64))
    wave1_ops = (
        (first_line + 0x00, 0x1112_1314_1516_1718, 0xFF),
        (first_line + 0x18, 0xA1A2_A3A4, 0x0F),
        (first_line + 0x30, 0x3132_3334_3536_3738, 0xFF),
    )
    wave2_ops = (
        (second_line + 0x08, 0x4142_4344_4546_4748, 0xFF),
        (second_line + 0x0D, 0xB1B2_B3B4, 0x0F),
        (second_line + 0x2F, 0xC5C6, 0x03),
    )
    sampled_first_wave_loads = (
        first_line + 0x00,
        first_line + 0x18,
        first_line + 0x30,
    )
    sampled_second_wave_loads = (
        second_line + 0x08,
        second_line + 0x10,
        second_line + 0x28,
        second_line + 0x30,
    )

    state = _reset_env_and_state(env)
    env.preload_bytes(first_line, preload_first_line)
    env.preload_bytes(second_line, preload_second_line)
    expected_first_wave_refmem = env.memory.fork_ref_memory()
    expected_final_refmem = env.memory.fork_ref_memory()

    for addr, data, mask in wave1_ops:
        _apply_store_stimulus_to_ref_memory(
            expected_first_wave_refmem,
            addr=addr,
            data=data,
            mask=mask,
        )
        _apply_store_stimulus_to_ref_memory(
            expected_final_refmem,
            addr=addr,
            data=data,
            mask=mask,
        )
    for addr, data, mask in wave2_ops:
        _apply_store_stimulus_to_ref_memory(
            expected_final_refmem,
            addr=addr,
            data=data,
            mask=mask,
        )

    store_results, sq_ptr = _enqueue_scalar_store_batch(
        env,
        state.sq_ptr,
        store_ops=(*wave1_ops, *wave2_ops),
        req_id_base=0,
    )
    env.backend.pulse_store_commit(len(wave1_ops))
    env.advance_cycles(env.config.sequence.store_settle_cycles)

    for store_result, (addr, data, _) in zip(store_results[: len(wave1_ops)], wave1_ops):
        env.wait_store_materialized(
            store_result.allocated_sq_ptr.value,
            expected_addr=addr,
            expected_data=data,
            expected_mmio=False,
            require_committed=True,
            max_cycles=env.config.sequence.store_materialize_cycles,
        )
    for store_result in store_results[len(wave1_ops) :]:
        pending_store = env.get_store_view(store_result.allocated_sq_ptr.value)
        assert pending_store is not None and pending_store.allocated, "第二波 store 未保留在 SQ 中"
        assert not pending_store.committed, "第二波 store 在第一波提交窗口内不应已 committed"

    with _capture_forward_activity(env, max_events=256) as trace:
        next_lq_ptr = state.next_lq_ptr
        for completed_loads, load_addr in enumerate(sampled_first_wave_loads, start=1):
            load_result = ScalarLoadSequence(
                LoadTxn(
                    req_id=100 + completed_loads - 1,
                    addr=load_addr,
                    lq_ptr=next_lq_ptr,
                    sq_ptr=sq_ptr,
                ),
                expected_completed_loads=completed_loads,
            ).run(env)
            next_lq_ptr = load_result.next_lq_ptr

    forward_summary = _summarize_forward_activity(trace)
    assert forward_summary["sq_event_count"] + forward_summary["sbuffer_event_count"] > 0, (
        f"第一波 committed/residency 场景未观测到任何 forward 活动: {forward_summary}"
    )

    env.backend.pulse_store_commit(len(wave2_ops))
    env.advance_cycles(env.config.sequence.store_settle_cycles)

    for store_result, (addr, data, _) in zip(store_results[len(wave1_ops) :], wave2_ops):
        env.wait_store_materialized(
            store_result.allocated_sq_ptr.value,
            expected_addr=addr,
            expected_data=data,
            expected_mmio=False,
            require_committed=True,
            max_cycles=env.config.sequence.store_materialize_cycles,
        )

    for completed_loads, load_addr in enumerate(
        sampled_second_wave_loads,
        start=len(sampled_first_wave_loads) + 1,
    ):
        load_result = ScalarLoadSequence(
            LoadTxn(
                req_id=100 + completed_loads - 1,
                addr=load_addr,
                lq_ptr=next_lq_ptr,
                sq_ptr=sq_ptr,
            ),
            expected_completed_loads=completed_loads,
        ).run(env)
        next_lq_ptr = load_result.next_lq_ptr

    drain_summary = FlushStoreBuffersSequence(max_cycles=1200).run(env)

    assert sq_ptr == ptr_inc(state.sq_ptr, env.config.sequence.store_queue_size, step=len(store_results)), (
        "两波 residency 场景的 SQ 指针推进异常"
    )
    assert env.read_cacheline(first_line) == expected_final_refmem.read_cacheline(first_line, 64), (
        "第一条 cacheline 的 delayed-flush 最终结果不匹配"
    )
    assert env.read_cacheline(second_line) == expected_final_refmem.read_cacheline(second_line, 64), (
        "第二条 cacheline 的 delayed-flush 最终结果不匹配"
    )
    assert env.memory.read(first_line, 8) == expected_first_wave_refmem.read(first_line, 8), (
        "第一波 residency 的头部 dword 最终值与首波预期不一致"
    )
    assert drain_summary["drain_event_count"] >= 1, "两波 residency 场景 flush 后未记录到 drain 事件"
    assert drain_summary["touched_byte_count"] >= 28, "两波 residency 场景 flush 覆盖字节数不足"
    env.assert_no_outstanding()


def test_api_MemBlock_cross16b_partial_store_burst_batched_commit_directed(env):
    """
    多条 cross-16B partial store 先批量入队/提交，再统一 delayed flush。

    检查点：
      - 多个 split store 能在同一 delayed-flush 窗口中稳定进入 committed
      - sbuffer data path 能观测到真实 writeReq 活动
      - 最终两条 cacheline 与参考内存完全一致
    """

    first_line = STORE_CROSS16B_BURST_BASE
    second_line = STORE_CROSS16B_BURST_BASE + 0x40
    env.preload_bytes(first_line, bytes(((index * 5) + 0x21) & 0xFF for index in range(64)))
    env.preload_bytes(second_line, bytes(((index * 13) + 0x07) & 0xFF for index in range(64)))
    expected_refmem = env.memory.fork_ref_memory()
    store_ops = (
        (first_line + 0x00, 0x5152_5354_5556_5758, 0xFF),
        (first_line + 0x0D, 0xA1A2_A3A4, 0x0F),
        (second_line + 0x00, 0x6162_6364_6566_6768, 0xFF),
        (second_line + 0x0E, 0xC1C2_C3C4_C5C6_C7C8, 0xFF),
    )
    state = _reset_env_and_state(env)
    for addr, data, mask in store_ops:
        _apply_store_stimulus_to_ref_memory(
            expected_refmem,
            addr=addr,
            data=data,
            mask=mask,
        )

    with _capture_sbuffer_data_activity(env, max_events=512) as trace:
        committed_views, sq_ptr = _send_scalar_store_batch_then_commit(
            env,
            state.sq_ptr,
            store_ops=store_ops,
            req_id_base=200,
            commit_chunk=2,
        )
        try:
            drain_summary = FlushStoreBuffersSequence(max_cycles=1200).run(env)
        except TimeoutError:
            pytest.xfail(DUTBUG_SQ_CROSS16B_BATCH_FLUSH_STALL_XFAIL_REASON)

    sbuffer_summary = _summarize_sbuffer_data_activity(trace)

    assert len(committed_views) == len(store_ops), "cross-16B 批量提交的 committed store 数量异常"
    assert all(store is not None and store.committed for store in committed_views), (
        "cross-16B 批量提交后存在未 committed 的 store"
    )
    assert sq_ptr == ptr_inc(state.sq_ptr, env.config.sequence.store_queue_size, step=len(store_ops)), (
        "cross-16B 批量提交场景的 SQ 指针推进异常"
    )
    assert sbuffer_summary["write_event_count"] >= len(store_ops), (
        f"cross-16B 批量提交场景的 sbuffer writeReq 活动不足: {sbuffer_summary}"
    )
    assert env.read_cacheline(first_line) == expected_refmem.read_cacheline(first_line, 64), (
        "cross-16B 批量提交后的第一条 cacheline 结果不匹配"
    )
    assert env.read_cacheline(second_line) == expected_refmem.read_cacheline(second_line, 64), (
        "cross-16B 批量提交后的第二条 cacheline 结果不匹配"
    )
    assert drain_summary["drain_event_count"] >= 1, "cross-16B 批量提交 flush 后未记录到 drain 事件"
    assert drain_summary["touched_byte_count"] >= 24, "cross-16B 批量提交 flush 覆盖字节数不足"
    env.assert_no_outstanding()
