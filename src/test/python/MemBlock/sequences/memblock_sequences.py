# coding=utf-8
"""
MemBlock 可复用测试 sequence。
"""

from collections import deque
from contextlib import contextmanager
from dataclasses import dataclass

from transactions import (
    BackendSendPlan,
    EnqueueLoadCyclePlan,
    IssueCyclePlan,
    IssueOp,
    LoadTxn,
    ptr_inc,
    QueuePtr,
    RobIndex,
    StoreTxn,
)

from request_apis import (
    enqueue_scalar_store,
    expect_load,
    issue_scalar_sta,
    issue_scalar_std,
    reset_env_and_wait_backend,
    send_load,
    send_store,
)


@dataclass(frozen=True)
class SequenceState:
    next_lq_ptr: QueuePtr
    sq_ptr: QueuePtr


@dataclass(frozen=True)
class ScalarLoadSequenceResult:
    txn: object
    next_lq_ptr: QueuePtr
    completed_load_count: int


@dataclass(frozen=True)
class ScalarStoreSequenceResult:
    txn: object
    allocated_sq_ptr: QueuePtr
    next_sq_ptr: QueuePtr
    store_view: object


@dataclass(frozen=True)
class ScalarLoadBurstSequenceResult:
    final_state: SequenceState
    completed_load_count: int
    transport_stats_before: dict[str, int]
    transport_stats_after: dict[str, int]


@dataclass(frozen=True)
class ScalarLoadSaturationSequenceResult:
    issued_loads: tuple[LoadTxn, ...]
    final_state: SequenceState
    completed_load_count: int
    transport_stats_before: dict[str, int]
    transport_stats_after: dict[str, int]


@dataclass(frozen=True)
class ScalarLoadBatchSameCycleSequenceResult:
    issued_loads: tuple[LoadTxn, ...]
    final_state: SequenceState
    completed_load_count: int


@dataclass(frozen=True)
class ScalarLoadBatchWithStaSequenceResult:
    issued_loads: tuple[LoadTxn, ...]
    final_state: SequenceState
    completed_load_count: int


@dataclass(frozen=True)
class ScalarStoreCommitSequenceResult:
    store_result: ScalarStoreSequenceResult
    committed_store_view: object


@dataclass(frozen=True)
class ScalarStoreThenLoadSequenceResult:
    store_result: ScalarStoreSequenceResult
    load_result: ScalarLoadSequenceResult
    final_state: SequenceState


@dataclass(frozen=True)
class ScalarStorePairThenLoadSequenceResult:
    first_store_result: ScalarStoreSequenceResult
    second_store_result: ScalarStoreSequenceResult
    load_result: ScalarLoadSequenceResult
    final_state: SequenceState


@dataclass(frozen=True)
class ScalarStoreFlushSequenceResult:
    store_result: ScalarStoreSequenceResult
    drain_summary: dict
    outer_write_delta: int
    sbuffer_drain_delta: int


@dataclass(frozen=True)
class ScalarMixedTrafficSequenceResult:
    final_state: SequenceState
    total_stores: int
    total_loads: int
    completed_load_count: int
    drain_summary: dict | None


@dataclass(frozen=True)
class ScalarForwardFailReplaySequenceResult:
    store_sq_ptr: QueuePtr
    replay_event: dict
    sq_forward_event: dict
    load_debug_trace: tuple[dict, ...]
    load_result: ScalarLoadSequenceResult
    final_state: SequenceState
    committed_store_view: object


@dataclass(frozen=True)
class ScalarBankConflictReplaySequenceResult:
    issued_loads: tuple[LoadTxn, ...]
    replay_events: tuple[dict | None, ...]
    load_debug_events: tuple[dict, ...]
    load_debug_traces: tuple[tuple[dict, ...], ...]
    final_state: SequenceState
    completed_load_count: int


@dataclass(frozen=True)
class ScalarCacheMissReplaySequenceResult:
    load_result: ScalarLoadSequenceResult
    replay_event: dict
    final_state: SequenceState
    transport_stats_before: dict[str, int]
    transport_stats_after: dict[str, int]


@dataclass(frozen=True)
class ScalarNcReplaySequenceResult:
    load_result: ScalarLoadSequenceResult
    replay_event: dict
    final_state: SequenceState
    transport_stats_before: dict[str, int]
    transport_stats_after: dict[str, int]


def _snapshot_transport_stats(env) -> dict[str, int]:
    return {key: int(value) for key, value in env.get_transport_stats().items()}


def _wait_store_addr_observed(env, sq_idx: int, expected_addr: int, max_cycles: int = 200):
    return env.wait_store_addr_observed(
        sq_idx=sq_idx,
        expected_addr=expected_addr,
        max_cycles=max_cycles,
    )


def _resolve_replay_drain_cycles(env, drain_cycles: int | None) -> int:
    if drain_cycles is not None:
        return int(drain_cycles)
    return max(int(env.config.sequence.load_drain_cycles), 1024)


def _wait_completed_load_count(env, target_count: int, max_cycles: int = 200) -> int:
    return env.wait_completed_load_count(target_count=target_count, max_cycles=max_cycles)


def _read_optional_any_signal(env, signal_names, default=None):
    for signal_name in signal_names:
        signal = getattr(env.dut, signal_name, None)
        if signal is not None:
            return int(signal.value)
    return default


def _sample_tlb_debug(env) -> dict[str, int | None]:
    return {
        "rob_head_tlb_replay": _read_optional_any_signal(
            env,
            ("MemBlock_inner_lsq_io_debugTopDown_robHeadTlbReplay",),
            default=None,
        ),
        "rob_head_tlb_miss": _read_optional_any_signal(
            env,
            ("MemBlock_inner_lsq_io_debugTopDown_robHeadTlbMiss",),
            default=None,
        ),
        "tlb_replay_all": _read_optional_any_signal(
            env,
            ("MemBlock_inner_lsq_io_tlb_hint_resp_bits_replay_all",),
            default=None,
        ),
    }


def _sample_l2_tlb_port(env) -> dict[str, int | None]:
    return {
        "req_valid": _read_optional_any_signal(env, ("io_l2_tlb_req_req_valid",), default=None),
        "req_vaddr": _read_optional_any_signal(env, ("io_l2_tlb_req_req_bits_vaddr",), default=None),
        "req_fullva": _read_optional_any_signal(env, ("io_l2_tlb_req_req_bits_fullva",), default=None),
        "req_cmd": _read_optional_any_signal(env, ("io_l2_tlb_req_req_bits_cmd",), default=None),
        "req_memidx_is_ld": _read_optional_any_signal(env, ("io_l2_tlb_req_req_bits_memidx_is_ld",), default=None),
        "req_memidx_is_st": _read_optional_any_signal(env, ("io_l2_tlb_req_req_bits_memidx_is_st",), default=None),
        "req_memidx_idx": _read_optional_any_signal(env, ("io_l2_tlb_req_req_bits_memidx_idx",), default=None),
        "resp_ready": _read_optional_any_signal(env, ("io_l2_tlb_req_resp_ready",), default=None),
        "resp_valid": _read_optional_any_signal(env, ("io_l2_tlb_req_resp_valid",), default=None),
        "resp_miss": _read_optional_any_signal(env, ("io_l2_tlb_req_resp_bits_miss",), default=None),
        "resp_paddr0": _read_optional_any_signal(env, ("io_l2_tlb_req_resp_bits_paddr_0",), default=None),
    }


def _sample_writeback_lanes(env) -> tuple[dict, ...]:
    lanes = []
    for lane, bundle in enumerate(env.writeback):
        if not bundle.connected("valid") or bundle.read("valid", 0) == 0:
            continue
        lanes.append(
            {
                "lane": lane,
                "valid": bundle.read("valid", 0),
                "ready": bundle.read("ready", 0) if bundle.connected("ready") else None,
                "to_rob_valid": bundle.read("toRob_valid", 0) if bundle.connected("toRob_valid") else None,
                "is_from_load": bundle.read("isFromLoadUnit", 0) if bundle.connected("isFromLoadUnit") else None,
                "rob_idx_flag": bundle.read("robIdx_flag", 0) if bundle.connected("robIdx_flag") else None,
                "rob_idx_value": bundle.read("robIdx_value", 0) if bundle.connected("robIdx_value") else None,
                "lq_idx_flag": bundle.read("lqIdx_flag", 0) if bundle.connected("lqIdx_flag") else None,
                "lq_idx_value": bundle.read("lqIdx_value", 0) if bundle.connected("lqIdx_value") else None,
                "sq_idx_flag": bundle.read("sqIdx_flag", 0) if bundle.connected("sqIdx_flag") else None,
                "sq_idx_value": bundle.read("sqIdx_value", 0) if bundle.connected("sqIdx_value") else None,
                "pdest": bundle.read("pdest", 0) if bundle.connected("pdest") else None,
                "data": bundle.read("data_0", 0) if bundle.connected("data_0") else None,
                "int_wen": bundle.read("intWen", 0) if bundle.connected("intWen") else None,
                "debug_vaddr": bundle.read("debug_vaddr", 0) if bundle.connected("debug_vaddr") else None,
                "debug_paddr": bundle.read("debug_paddr", 0) if bundle.connected("debug_paddr") else None,
                "debug_is_mmio": bundle.read("debug_isMMIO", 0) if bundle.connected("debug_isMMIO") else None,
                "debug_is_ncio": bundle.read("debug_isNCIO", 0) if bundle.connected("debug_isNCIO") else None,
                "exception_bits": bundle.read_exception_bits() if hasattr(bundle, "read_exception_bits") else None,
            }
        )
    return tuple(lanes)


@contextmanager
def _capture_writeback_trace(env, *, max_events: int = 16):
    trace = deque(maxlen=max(1, int(max_events)))

    def _sample() -> None:
        cycle = env._current_cycle()
        for lane, bundle in enumerate(env.writeback):
            if not bundle.connected("valid") or bundle.read("valid", 0) == 0:
                continue
            trace.append(
                {
                    "cycle": cycle,
                    "lane": lane,
                    "ready": bundle.read("ready", 0) if bundle.connected("ready") else None,
                    "to_rob_valid": bundle.read("toRob_valid", 0) if bundle.connected("toRob_valid") else None,
                    "is_from_load": bundle.read("isFromLoadUnit", 0) if bundle.connected("isFromLoadUnit") else None,
                    "rob_idx_flag": bundle.read("robIdx_flag", 0) if bundle.connected("robIdx_flag") else None,
                    "rob_idx_value": bundle.read("robIdx_value", 0) if bundle.connected("robIdx_value") else None,
                    "sq_idx_value": bundle.read("sqIdx_value", 0) if bundle.connected("sqIdx_value") else None,
                    "pdest": bundle.read("pdest", 0) if bundle.connected("pdest") else None,
                    "data": bundle.read("data_0", 0) if bundle.connected("data_0") else None,
                    "int_wen": bundle.read("intWen", 0) if bundle.connected("intWen") else None,
                    "exception_bits": bundle.read_exception_bits() if hasattr(bundle, "read_exception_bits") else None,
                }
            )

    env.add_after_step_callback(_sample)
    try:
        yield trace
    finally:
        env.remove_after_step_callback(_sample)


def _sample_sq_forward_events(env) -> tuple[dict, ...]:
    events = []
    for lane in range(env.config.load_pipeline_width):
        prefix = f"MemBlock_inner_lsq_io_forward_{lane}_s2Resp"
        if not env._read_optional_dut_signal(f"{prefix}_valid"):
            continue
        events.append(
            {
                "cycle": env._current_cycle(),
                "lane": lane,
                "valid": env._read_optional_dut_signal(f"{prefix}_valid"),
                "forward_invalid": env._read_optional_dut_signal(f"{prefix}_bits_forwardInvalid"),
                "match_invalid": env._read_optional_dut_signal(f"{prefix}_bits_matchInvalid"),
                "addr_invalid_valid": env._read_optional_dut_signal(f"{prefix}_bits_addrInvalid_valid"),
                "addr_invalid_flag": env._read_optional_dut_signal(f"{prefix}_bits_addrInvalid_bits_flag"),
                "addr_invalid_value": env._read_optional_dut_signal(f"{prefix}_bits_addrInvalid_bits_value"),
                "data_invalid_valid": env._read_optional_dut_signal(f"{prefix}_bits_dataInvalid_valid"),
                "data_invalid_flag": env._read_optional_dut_signal(f"{prefix}_bits_dataInvalid_bits_flag"),
                "data_invalid_value": env._read_optional_dut_signal(f"{prefix}_bits_dataInvalid_bits_value"),
                "forward_mask": tuple(
                    env._read_optional_dut_signal(f"{prefix}_bits_forwardMask_{index}")
                    for index in range(16)
                ),
                "forward_data": tuple(
                    env._read_optional_dut_signal(f"{prefix}_bits_forwardData_{index}")
                    for index in range(16)
                ),
            }
        )
    return tuple(events)


def sample_sq_forward_events(env) -> tuple[dict, ...]:
    """采样当前拍所有有效的 SQ/SBuffer forward 响应。"""

    return _sample_sq_forward_events(env)


def sample_sbuffer_forward_events(env) -> tuple[dict, ...]:
    """采样当前拍所有有效的 Sbuffer forward 响应。"""

    events = []
    for lane in range(env.config.load_pipeline_width):
        prefix = f"MemBlock_inner_sbuffer_io_forward_{lane}_s2Resp"
        if not env._read_optional_dut_signal(f"{prefix}_valid"):
            continue
        events.append(
            {
                "cycle": env._current_cycle(),
                "lane": lane,
                "valid": env._read_optional_dut_signal(f"{prefix}_valid"),
                "match_invalid": env._read_optional_dut_signal(f"{prefix}_bits_matchInvalid"),
                "forward_mask": tuple(
                    env._read_optional_dut_signal(f"{prefix}_bits_forwardMask_{index}")
                    for index in range(16)
                ),
                "forward_data": tuple(
                    env._read_optional_dut_signal(f"{prefix}_bits_forwardData_{index}")
                    for index in range(16)
                ),
            }
        )
    return tuple(events)


def _wait_sq_forward_event(
    env,
    *,
    lane: int | None = None,
    expected_sq_idx: int | None = None,
    expected_data_invalid_valid: int | None = None,
    expected_match_invalid: int | None = None,
    expected_forward_invalid: int | None = None,
    require_forward_mask: bool = False,
    max_cycles: int = 200,
):
    for _ in range(max_cycles):
        for event in _sample_sq_forward_events(env):
            if lane is not None and event["lane"] != int(lane):
                continue
            if expected_sq_idx is not None and event["data_invalid_value"] != int(expected_sq_idx):
                continue
            if expected_data_invalid_valid is not None and event["data_invalid_valid"] != int(expected_data_invalid_valid):
                continue
            if expected_match_invalid is not None and event["match_invalid"] != int(expected_match_invalid):
                continue
            if expected_forward_invalid is not None and event["forward_invalid"] != int(expected_forward_invalid):
                continue
            if require_forward_mask and not any(int(bit) for bit in event["forward_mask"]):
                continue
            return event
        env.advance_cycles(1)

    raise TimeoutError(
        "等待 SQ forward 事件超时: "
        f"lane={lane}, sqIdx={expected_sq_idx}, dataInvalid={expected_data_invalid_valid}, "
        f"matchInvalid={expected_match_invalid}, forwardInvalid={expected_forward_invalid}, "
        f"requireForwardMask={int(bool(require_forward_mask))}"
    )


def wait_sbuffer_forward_event(
    env,
    *,
    lane: int | None = None,
    expected_match_invalid: int | None = None,
    max_cycles: int = 200,
):
    """等待某个 load lane 上的 Sbuffer forward 响应。"""

    for _ in range(max_cycles):
        for event in sample_sbuffer_forward_events(env):
            if lane is not None and event["lane"] != int(lane):
                continue
            if expected_match_invalid is not None and event["match_invalid"] != int(expected_match_invalid):
                continue
            return event
        env.advance_cycles(1)

    raise TimeoutError(
        "等待 Sbuffer forward 事件超时: "
        f"lane={lane}, matchInvalid={expected_match_invalid}"
    )


def wait_sq_forward_event(
    env,
    *,
    lane: int | None = None,
    expected_sq_idx: int | None = None,
    expected_data_invalid_valid: int | None = None,
    expected_match_invalid: int | None = None,
    expected_forward_invalid: int | None = None,
    require_forward_mask: bool = False,
    max_cycles: int = 200,
):
    """等待某个 load lane 上的 SQ/SBuffer forward 响应。"""

    return _wait_sq_forward_event(
        env,
        lane=lane,
        expected_sq_idx=expected_sq_idx,
        expected_data_invalid_valid=expected_data_invalid_valid,
        expected_match_invalid=expected_match_invalid,
        expected_forward_invalid=expected_forward_invalid,
        max_cycles=max_cycles,
        require_forward_mask=require_forward_mask,
    )


@contextmanager
def _capture_load_debug_trace(env, *, max_events: int = 64):
    trace = deque(maxlen=max(1, int(max_events)))

    def _sample() -> None:
        debug_state = env.sample_load_debug_state()
        for lane_state in debug_state["lanes"]:
            stage_rob_ids = (
                lane_state["s1_rob_idx_value"],
                lane_state["s2_rob_idx_value"],
                lane_state["s3_rob_idx_value"],
            )
            if all(value < 0 for value in stage_rob_ids) and not lane_state["replay_cause_mask"]:
                continue
            trace.append(dict(lane_state))

    env.add_after_step_callback(_sample)
    try:
        yield trace
    finally:
        env.remove_after_step_callback(_sample)


def _load_debug_lane_matches(
    lane_state: dict,
    *,
    lane: int | None = None,
    rob_idx_value: int | None = None,
    required_replay_causes=(),
    forbidden_replay_causes=(),
    s2_is_bank_conflict: int | None = None,
    s2_is_forward_fail: int | None = None,
    s3_is_replay: int | None = None,
) -> bool:
    if lane is not None and lane_state["lane"] != int(lane):
        return False
    if rob_idx_value is not None and int(rob_idx_value) not in {
        lane_state["s1_rob_idx_value"],
        lane_state["s2_rob_idx_value"],
        lane_state["s3_rob_idx_value"],
    }:
        return False

    replay_causes = set(lane_state["replay_causes"])
    if any(cause not in replay_causes for cause in required_replay_causes):
        return False
    if any(cause in replay_causes for cause in forbidden_replay_causes):
        return False
    if s2_is_bank_conflict is not None and lane_state["s2_is_bank_conflict"] != int(s2_is_bank_conflict):
        return False
    if s2_is_forward_fail is not None and lane_state["s2_is_forward_fail"] != int(s2_is_forward_fail):
        return False
    if s3_is_replay is not None and lane_state["s3_is_replay"] != int(s3_is_replay):
        return False
    return True


def _wait_load_debug_event(
    env,
    *,
    lane: int | None = None,
    rob_idx_value: int | None = None,
    required_replay_causes=(),
    forbidden_replay_causes=(),
    s2_is_bank_conflict: int | None = None,
    s2_is_forward_fail: int | None = None,
    s3_is_replay: int | None = None,
    max_cycles: int = 200,
):
    recent = deque(maxlen=16)
    for _ in range(max_cycles):
        debug_state = env.sample_load_debug_state()
        for lane_state in debug_state["lanes"]:
            recent.append(dict(lane_state))
            if _load_debug_lane_matches(
                lane_state,
                lane=lane,
                rob_idx_value=rob_idx_value,
                required_replay_causes=required_replay_causes,
                forbidden_replay_causes=forbidden_replay_causes,
                s2_is_bank_conflict=s2_is_bank_conflict,
                s2_is_forward_fail=s2_is_forward_fail,
                s3_is_replay=s3_is_replay,
            ):
                return dict(lane_state)
        env.advance_cycles(1)

    raise TimeoutError(
        "等待 load debug 事件超时: "
        f"lane={lane}, rob={rob_idx_value}, causes={tuple(required_replay_causes)}, recent={tuple(recent)}"
    )


def _wait_sq_matchinvalid_and_violation(
    env,
    *,
    lane: int,
    expected_sq_idx: int,
    rob_idx,
    max_cycles: int = 200,
):
    sq_event = None
    violation_event = None
    replay_events = []

    for _ in range(max_cycles):
        for event in _sample_sq_forward_events(env):
            if event["lane"] != int(lane):
                continue
            if event["data_invalid_valid"] != 1 or event["match_invalid"] != 1:
                continue
            if event["data_invalid_value"] != int(expected_sq_idx):
                continue
            sq_event = event

        replay_state = env.sample_replay_state()
        for event in replay_state["events"]:
            if event.get("rob_idx_flag") == int(rob_idx.flag) and event.get("rob_idx_value") == int(rob_idx.value):
                replay_events.append(dict(event))

        memory_violation = replay_state["memory_violation"]
        if (
            memory_violation["valid"]
            and memory_violation["rob_idx_flag"] == int(rob_idx.flag)
            and memory_violation["rob_idx_value"] == int(rob_idx.value)
        ):
            violation_event = dict(memory_violation)

        if sq_event is not None and violation_event is not None:
            dcache_miss_signal = _read_optional_any_signal(
                env,
                (
                    f"_inner_dcache_io_lsu_load_{lane}_resp_bits_miss",
                    f"MemBlock_inner_dcache_io_lsu_load_{lane}_resp_bits_miss",
                ),
                default=None,
            )
            return sq_event, violation_event, tuple(replay_events), dcache_miss_signal

        env.advance_cycles(1)

    raise TimeoutError(
        "等待 SQ dataInvalid+matchInvalid 与 memoryViolation 超时: "
        f"lane={lane}, sqIdx={expected_sq_idx}, rob=({rob_idx_flag},{rob_idx_value})"
    )


class ResetEnvSequence:
    def __init__(
        self,
        *,
        require_issue_lanes=(),
        require_lq_ready: bool = False,
        require_sq_ready: bool = False,
        reset_cycles: int | None = None,
        settle_cycles: int | None = None,
        seed_wrap_boundary: bool = True,
        initial_rob_idx: RobIndex | None = None,
    ) -> None:
        self.require_issue_lanes = tuple(require_issue_lanes)
        self.require_lq_ready = require_lq_ready
        self.require_sq_ready = require_sq_ready
        self.reset_cycles = reset_cycles
        self.settle_cycles = settle_cycles
        self.seed_wrap_boundary = seed_wrap_boundary
        self.initial_rob_idx = initial_rob_idx

    def run(self, env) -> SequenceState:
        reset_cycles = env.config.sequence.reset_cycles if self.reset_cycles is None else self.reset_cycles
        settle_cycles = env.config.sequence.reset_settle_cycles if self.settle_cycles is None else self.settle_cycles
        reset_env_and_wait_backend(
            env,
            reset_cycles=reset_cycles,
            settle_cycles=settle_cycles,
            require_issue_lanes=self.require_issue_lanes,
            require_lq_ready=self.require_lq_ready,
            require_sq_ready=self.require_sq_ready,
        )
        initial_rob_idx = self.initial_rob_idx
        if initial_rob_idx is None and self.seed_wrap_boundary:
            initial_rob_idx = RobIndex(flag=0, value=int(env.config.rob_size) - 1)
        if initial_rob_idx is not None:
            env.backend.set_next_rob_idx(initial_rob_idx)
            env.backend.set_commit_frontier(initial_rob_idx)
        return SequenceState(
            next_lq_ptr=QueuePtr(flag=0, value=0),
            sq_ptr=QueuePtr(flag=0, value=0),
        )


class ScalarLoadSequence:
    def __init__(
        self,
        txn,
        *,
        drain_cycles: int | None = None,
        expected_completed_loads: int | None = None,
        load_queue_size: int | None = None,
        assert_no_outstanding: bool = False,
    ) -> None:
        self.txn = txn
        self.drain_cycles = drain_cycles
        self.expected_completed_loads = expected_completed_loads
        self.load_queue_size = load_queue_size
        self.assert_no_outstanding = assert_no_outstanding

    def run(self, env) -> ScalarLoadSequenceResult:
        drain_cycles = env.config.sequence.load_drain_cycles if self.drain_cycles is None else self.drain_cycles
        load_queue_size = env.config.sequence.load_queue_size if self.load_queue_size is None else self.load_queue_size
        send_load(env, self.txn)
        expect_load(env, self.txn)
        env.drain_writebacks(max_cycles=drain_cycles)
        completed = env.get_completed_load_count()
        if self.expected_completed_loads is not None:
            assert completed == self.expected_completed_loads, (
                f"load req_id={self.txn.req_id} 未在限定周期内完成"
            )
        if self.assert_no_outstanding:
            env.assert_no_outstanding()
        return ScalarLoadSequenceResult(
            txn=self.txn,
            next_lq_ptr=ptr_inc(self.txn.lq_ptr, load_queue_size),
            completed_load_count=completed,
        )


class ScalarLoadBurstSequence:
    def __init__(
        self,
        requests,
        *,
        initial_state: SequenceState,
        drain_cycles: int | None = None,
        size: int = 8,
        mask: int = 0xFF,
        enq_port: int = 0,
        issue_lane: int = 0,
        assert_no_outstanding: bool = False,
    ) -> None:
        self.requests = list(requests)
        self.initial_state = initial_state
        self.drain_cycles = drain_cycles
        self.size = size
        self.mask = mask
        self.enq_port = enq_port
        self.issue_lane = issue_lane
        self.assert_no_outstanding = assert_no_outstanding

    def run(self, env) -> ScalarLoadBurstSequenceResult:
        state = self.initial_state
        completed_before = env.get_completed_load_count()
        stats_before = _snapshot_transport_stats(env)

        for idx, (req_id, addr) in enumerate(self.requests):
            result = ScalarLoadSequence(
                LoadTxn(
                    req_id=req_id,
                    addr=addr,
                    lq_ptr=state.next_lq_ptr,
                    sq_ptr=state.sq_ptr,
                    size=self.size,
                    mask=self.mask,
                    enq_port=self.enq_port,
                    issue_lane=self.issue_lane,
                ),
                drain_cycles=self.drain_cycles,
                expected_completed_loads=completed_before + idx + 1,
            ).run(env)
            state = SequenceState(
                next_lq_ptr=result.next_lq_ptr,
                sq_ptr=state.sq_ptr,
            )

        if self.assert_no_outstanding:
            env.assert_no_outstanding()

        return ScalarLoadBurstSequenceResult(
            final_state=state,
            completed_load_count=env.get_completed_load_count(),
            transport_stats_before=stats_before,
            transport_stats_after=_snapshot_transport_stats(env),
        )


class ScalarLoadSaturationSequence:
    def __init__(
        self,
        requests,
        *,
        initial_state: SequenceState,
        drain_cycles: int | None = None,
        size: int = 8,
        mask: int = 0xFF,
        batch_width: int | None = None,
        assert_no_outstanding: bool = False,
    ) -> None:
        self.requests = tuple((int(req_id), int(addr)) for req_id, addr in requests)
        self.initial_state = initial_state
        self.drain_cycles = drain_cycles
        self.size = int(size)
        self.mask = int(mask)
        self.batch_width = None if batch_width is None else int(batch_width)
        self.assert_no_outstanding = assert_no_outstanding

    def run(self, env) -> ScalarLoadSaturationSequenceResult:
        if not self.requests:
            raise ValueError("load saturation sequence requires at least one request")

        configured_width = min(int(env.config.load_pipeline_width), int(env.config.lsq_enq_ports))
        batch_width = configured_width if self.batch_width is None else int(self.batch_width)
        if batch_width <= 0:
            raise ValueError(f"load saturation sequence requires positive batch width: {batch_width}")
        if batch_width > configured_width:
            raise ValueError(
                f"load saturation sequence batch width exceeds supported load input width: "
                f"batch_width={batch_width}, supported={configured_width}"
            )

        drain_cycles = env.config.sequence.load_drain_cycles if self.drain_cycles is None else int(self.drain_cycles)
        state = self.initial_state
        completed_before = env.get_completed_load_count()
        stats_before = _snapshot_transport_stats(env)
        issued_loads = []

        for batch_start in range(0, len(self.requests), batch_width):
            batch_requests = self.requests[batch_start : batch_start + batch_width]
            txns = []
            for slot, (req_id, addr) in enumerate(batch_requests):
                load_txn = LoadTxn(
                    req_id=req_id,
                    addr=addr,
                    lq_ptr=ptr_inc(state.next_lq_ptr, env.config.sequence.load_queue_size, step=slot),
                    sq_ptr=state.sq_ptr,
                    size=self.size,
                    mask=self.mask,
                    enq_port=slot,
                    issue_lane=slot,
                )
                txns.append(load_txn)

            result = ScalarLoadBatchSameCycleSequence(tuple(txns)).run(env)
            for load_txn in txns:
                expect_load(env, load_txn)
            issued_loads.extend(txns)
            state = result.final_state

        target_completed = completed_before + len(self.requests)
        _wait_completed_load_count(env, target_completed, max_cycles=drain_cycles)
        env.drain_writebacks(max_cycles=drain_cycles)
        if self.assert_no_outstanding:
            env.assert_no_outstanding()

        return ScalarLoadSaturationSequenceResult(
            issued_loads=tuple(issued_loads),
            final_state=state,
            completed_load_count=env.get_completed_load_count(),
            transport_stats_before=stats_before,
            transport_stats_after=_snapshot_transport_stats(env),
        )


class ScalarLoadBatchSameCycleSequence:
    """Scenario-level wrapper for a same-cycle multi-load backend plan."""

    def __init__(
        self,
        txns,
        *,
        max_cycles: int = 50,
        assert_no_outstanding: bool = False,
    ) -> None:
        self.txns = tuple(txns)
        self.max_cycles = int(max_cycles)
        self.assert_no_outstanding = assert_no_outstanding

    def run(self, env) -> ScalarLoadBatchSameCycleSequenceResult:
        if not self.txns:
            raise ValueError("same-cycle load batch requires at least one transaction")

        env.backend.execute(
            BackendSendPlan.from_steps(
                EnqueueLoadCyclePlan.from_txns(*self.txns),
                IssueCyclePlan(
                    ops=tuple(IssueOp.load_from_txn(txn) for txn in self.txns),
                    max_cycles=self.max_cycles,
                ),
            )
        )
        if self.assert_no_outstanding:
            env.assert_no_outstanding()
        return ScalarLoadBatchSameCycleSequenceResult(
            issued_loads=self.txns,
            final_state=SequenceState(
                next_lq_ptr=ptr_inc(self.txns[-1].lq_ptr, env.config.sequence.load_queue_size),
                sq_ptr=self.txns[-1].sq_ptr,
            ),
            completed_load_count=env.get_completed_load_count(),
        )


class ScalarLoadBatchWithStaSequence:
    """Scenario-level wrapper for same-cycle multi-load plus one STA."""

    def __init__(
        self,
        txns,
        *,
        sta_req_id: int,
        sta_sq_ptr,
        sta_addr: int,
        sta_lane: int = 3,
        sta_mask: int = 0xFF,
        max_cycles: int = 50,
        assert_no_outstanding: bool = False,
    ) -> None:
        self.txns = tuple(txns)
        self.sta_req_id = int(sta_req_id)
        self.sta_sq_ptr = sta_sq_ptr
        self.sta_addr = int(sta_addr)
        self.sta_lane = int(sta_lane)
        self.sta_mask = int(sta_mask)
        self.max_cycles = int(max_cycles)
        self.assert_no_outstanding = assert_no_outstanding

    def run(self, env) -> ScalarLoadBatchWithStaSequenceResult:
        if not self.txns:
            raise ValueError("same-cycle load+STA batch requires at least one load transaction")

        env.backend.execute(
            BackendSendPlan.from_steps(
                EnqueueLoadCyclePlan.from_txns(*self.txns),
                IssueCyclePlan(
                    ops=tuple(IssueOp.load_from_txn(txn) for txn in self.txns)
                    + (
                        IssueOp.sta(
                            req_id=self.sta_req_id,
                            sq_ptr=self.sta_sq_ptr,
                            addr=self.sta_addr,
                            lane=self.sta_lane,
                            mask=self.sta_mask,
                        ),
                    ),
                    max_cycles=self.max_cycles,
                ),
            )
        )
        if self.assert_no_outstanding:
            env.assert_no_outstanding()
        return ScalarLoadBatchWithStaSequenceResult(
            issued_loads=self.txns,
            final_state=SequenceState(
                next_lq_ptr=ptr_inc(self.txns[-1].lq_ptr, env.config.sequence.load_queue_size),
                sq_ptr=self.txns[-1].sq_ptr,
            ),
            completed_load_count=env.get_completed_load_count(),
        )


class ScalarStoreSequence:
    def __init__(
        self,
        txn,
        *,
        expected_addr: int | None = None,
        expected_mmio: bool | None = None,
        expected_nc: bool | None = None,
        require_committed: bool = False,
        materialize_cycles: int | None = None,
        store_queue_size: int | None = None,
    ) -> None:
        self.txn = txn
        self.expected_addr = expected_addr
        self.expected_mmio = expected_mmio
        self.expected_nc = expected_nc
        self.require_committed = require_committed
        self.materialize_cycles = materialize_cycles
        self.store_queue_size = store_queue_size

    def run(self, env) -> ScalarStoreSequenceResult:
        materialize_cycles = (
            env.config.sequence.store_materialize_cycles
            if self.materialize_cycles is None
            else self.materialize_cycles
        )
        store_queue_size = env.config.sequence.store_queue_size if self.store_queue_size is None else self.store_queue_size
        expected_addr = self.txn.addr if self.expected_addr is None else int(self.expected_addr)
        allocated_sq_ptr = send_store(env, self.txn)
        store_view = env.wait_store_materialized(
            allocated_sq_ptr.value,
            expected_addr=expected_addr,
            expected_data=self.txn.data,
            expected_mmio=self.expected_mmio,
            expected_nc=self.expected_nc,
            require_committed=self.require_committed,
            max_cycles=materialize_cycles,
        )
        return ScalarStoreSequenceResult(
            txn=self.txn,
            allocated_sq_ptr=allocated_sq_ptr,
            next_sq_ptr=ptr_inc(self.txn.sq_ptr, store_queue_size),
            store_view=store_view,
        )


class ScalarStoreCommitSequence:
    def __init__(
        self,
        txn,
        *,
        expected_addr: int | None = None,
        expected_mmio: bool | None = None,
        expected_nc: bool | None = None,
        require_committed: bool = False,
        materialize_cycles: int | None = None,
        settle_cycles: int | None = None,
        wait_quiesce: bool = False,
        quiesce_cycles: int = 300,
    ) -> None:
        self.txn = txn
        self.expected_addr = expected_addr
        self.expected_mmio = expected_mmio
        self.expected_nc = expected_nc
        self.require_committed = require_committed
        self.materialize_cycles = materialize_cycles
        self.settle_cycles = settle_cycles
        self.wait_quiesce = wait_quiesce
        self.quiesce_cycles = quiesce_cycles

    def run(self, env) -> ScalarStoreCommitSequenceResult:
        # 真实 DUT 中 store 的 committed 由 ROB 提交边界和 scommit 共同驱动，
        # 不能在发完 STA/STD 后直接假定 store 已 committed。
        settle_cycles = env.config.sequence.store_settle_cycles if self.settle_cycles is None else self.settle_cycles
        store_result = ScalarStoreSequence(
            self.txn,
            expected_addr=self.expected_addr,
            expected_mmio=self.expected_mmio,
            expected_nc=self.expected_nc,
            require_committed=False,
            materialize_cycles=self.materialize_cycles,
        ).run(env)
        env.backend.pulse_store_commit(1)
        if settle_cycles > 0:
            env.advance_cycles(settle_cycles)
        if self.require_committed:
            expected_addr = self.txn.addr if self.expected_addr is None else int(self.expected_addr)
            env.wait_store_materialized(
                store_result.allocated_sq_ptr.value,
                expected_addr=expected_addr,
                expected_data=self.txn.data,
                expected_mmio=self.expected_mmio,
                expected_nc=self.expected_nc,
                require_committed=True,
                max_cycles=(
                    env.config.sequence.store_materialize_cycles
                    if self.materialize_cycles is None
                    else self.materialize_cycles
                ),
            )
        if self.wait_quiesce:
            env.wait_memory_quiesce(max_cycles=self.quiesce_cycles)
        return ScalarStoreCommitSequenceResult(
            store_result=store_result,
            committed_store_view=env.get_store_view(store_result.allocated_sq_ptr.value),
        )


class ScalarStoreThenLoadSequence:
    def __init__(
        self,
        store_txn,
        *,
        initial_lq_ptr: QueuePtr,
        load_req_id: int,
        load_addr: int | None = None,
        expected_mmio: bool | None = None,
        require_committed: bool = True,
        materialize_cycles: int | None = None,
        drain_cycles: int | None = None,
        size: int = 8,
        mask: int = 0xFF,
        assert_no_outstanding: bool = False,
    ) -> None:
        self.store_txn = store_txn
        self.initial_lq_ptr = initial_lq_ptr
        self.load_req_id = load_req_id
        self.load_addr = load_addr
        self.expected_mmio = expected_mmio
        self.require_committed = require_committed
        self.materialize_cycles = materialize_cycles
        self.drain_cycles = drain_cycles
        self.size = size
        self.mask = mask
        self.assert_no_outstanding = assert_no_outstanding

    def run(self, env) -> ScalarStoreThenLoadSequenceResult:
        completed_before = env.get_completed_load_count()
        committed_store = ScalarStoreCommitSequence(
            self.store_txn,
            expected_mmio=self.expected_mmio,
            require_committed=self.require_committed,
            materialize_cycles=self.materialize_cycles,
        ).run(env)
        store_result = ScalarStoreSequenceResult(
            txn=committed_store.store_result.txn,
            allocated_sq_ptr=committed_store.store_result.allocated_sq_ptr,
            next_sq_ptr=committed_store.store_result.next_sq_ptr,
            store_view=(
                committed_store.committed_store_view
                if committed_store.committed_store_view is not None
                else committed_store.store_result.store_view
            ),
        )
        load_result = ScalarLoadSequence(
            LoadTxn(
                req_id=self.load_req_id,
                addr=self.store_txn.addr if self.load_addr is None else self.load_addr,
                lq_ptr=self.initial_lq_ptr,
                sq_ptr=store_result.next_sq_ptr,
                size=self.size,
                mask=self.mask,
            ),
            drain_cycles=self.drain_cycles,
            expected_completed_loads=completed_before + 1,
        ).run(env)
        final_state = SequenceState(
            next_lq_ptr=load_result.next_lq_ptr,
            sq_ptr=store_result.next_sq_ptr,
        )
        if self.assert_no_outstanding:
            env.assert_no_outstanding()
        return ScalarStoreThenLoadSequenceResult(
            store_result=store_result,
            load_result=load_result,
            final_state=final_state,
        )


class ScalarStorePairThenLoadSequence:
    def __init__(
        self,
        first_store_txn,
        second_store_txn,
        *,
        initial_lq_ptr: QueuePtr,
        load_req_id: int,
        load_addr: int | None = None,
        expected_mmio: bool | None = False,
        require_committed: bool = True,
        materialize_cycles: int | None = None,
        commit_settle_cycles: int | None = None,
        drain_cycles: int | None = None,
        size: int = 8,
        mask: int = 0xFF,
        assert_no_outstanding: bool = False,
    ) -> None:
        self.first_store_txn = first_store_txn
        self.second_store_txn = second_store_txn
        self.initial_lq_ptr = initial_lq_ptr
        self.load_req_id = load_req_id
        self.load_addr = load_addr
        self.expected_mmio = expected_mmio
        self.require_committed = require_committed
        self.materialize_cycles = materialize_cycles
        self.commit_settle_cycles = commit_settle_cycles
        self.drain_cycles = drain_cycles
        self.size = size
        self.mask = mask
        self.assert_no_outstanding = assert_no_outstanding

    def run(self, env) -> ScalarStorePairThenLoadSequenceResult:
        completed_before = env.get_completed_load_count()
        commit_settle_cycles = (
            env.config.sequence.store_settle_cycles
            if self.commit_settle_cycles is None
            else self.commit_settle_cycles
        )

        first_store_result = ScalarStoreSequence(
            self.first_store_txn,
            expected_mmio=self.expected_mmio,
            require_committed=False,
            materialize_cycles=self.materialize_cycles,
        ).run(env)
        env.backend.pulse_store_commit(1)
        if commit_settle_cycles > 0:
            env.advance_cycles(commit_settle_cycles)
        if self.require_committed:
            env.wait_store_materialized(
                first_store_result.allocated_sq_ptr.value,
                expected_addr=self.first_store_txn.addr,
                expected_data=self.first_store_txn.data,
                expected_mmio=self.expected_mmio,
                require_committed=True,
                max_cycles=(
                    env.config.sequence.store_materialize_cycles
                    if self.materialize_cycles is None
                    else self.materialize_cycles
                ),
            )

        second_store_result = ScalarStoreSequence(
            self.second_store_txn,
            expected_mmio=self.expected_mmio,
            require_committed=False,
            materialize_cycles=self.materialize_cycles,
        ).run(env)
        env.backend.pulse_store_commit(1)
        if commit_settle_cycles > 0:
            env.advance_cycles(commit_settle_cycles)
        if self.require_committed:
            env.wait_store_materialized(
                second_store_result.allocated_sq_ptr.value,
                expected_addr=self.second_store_txn.addr,
                expected_data=self.second_store_txn.data,
                expected_mmio=self.expected_mmio,
                require_committed=True,
                max_cycles=(
                    env.config.sequence.store_materialize_cycles
                    if self.materialize_cycles is None
                    else self.materialize_cycles
                ),
            )

        load_result = ScalarLoadSequence(
            LoadTxn(
                req_id=self.load_req_id,
                addr=self.second_store_txn.addr if self.load_addr is None else self.load_addr,
                lq_ptr=self.initial_lq_ptr,
                sq_ptr=second_store_result.next_sq_ptr,
                size=self.size,
                mask=self.mask,
            ),
            drain_cycles=self.drain_cycles,
            expected_completed_loads=completed_before + 1,
        ).run(env)
        final_state = SequenceState(
            next_lq_ptr=load_result.next_lq_ptr,
            sq_ptr=second_store_result.next_sq_ptr,
        )
        if self.assert_no_outstanding:
            env.assert_no_outstanding()
        return ScalarStorePairThenLoadSequenceResult(
            first_store_result=ScalarStoreSequenceResult(
                txn=first_store_result.txn,
                allocated_sq_ptr=first_store_result.allocated_sq_ptr,
                next_sq_ptr=first_store_result.next_sq_ptr,
                store_view=env.get_store_view(first_store_result.allocated_sq_ptr.value),
            ),
            second_store_result=ScalarStoreSequenceResult(
                txn=second_store_result.txn,
                allocated_sq_ptr=second_store_result.allocated_sq_ptr,
                next_sq_ptr=second_store_result.next_sq_ptr,
                store_view=env.get_store_view(second_store_result.allocated_sq_ptr.value),
            ),
            load_result=load_result,
            final_state=final_state,
        )


class FlushStoreBuffersSequence:
    def __init__(self, *, max_cycles: int | None = None, settle_cycles: int | None = None) -> None:
        self.max_cycles = max_cycles
        self.settle_cycles = settle_cycles

    def run(self, env) -> dict:
        max_cycles = env.config.sequence.store_flush_cycles if self.max_cycles is None else self.max_cycles
        settle_cycles = env.config.sequence.store_settle_cycles if self.settle_cycles is None else self.settle_cycles
        return env.flush_store_buffers_and_wait(
            max_cycles=max_cycles,
            settle_cycles=settle_cycles,
        )


class ScalarStoreFlushSequence:
    def __init__(
        self,
        txn,
        *,
        expected_mmio: bool | None = False,
        require_committed: bool = True,
        materialize_cycles: int | None = None,
        wait_for_sbuffer_drain: bool = True,
        sbuffer_wait_cycles: int = 300,
        max_cycles: int | None = None,
        settle_cycles: int | None = None,
        assert_no_outstanding: bool = False,
    ) -> None:
        self.txn = txn
        self.expected_mmio = expected_mmio
        self.require_committed = require_committed
        self.materialize_cycles = materialize_cycles
        self.wait_for_sbuffer_drain = wait_for_sbuffer_drain
        self.sbuffer_wait_cycles = sbuffer_wait_cycles
        self.max_cycles = max_cycles
        self.settle_cycles = settle_cycles
        self.assert_no_outstanding = assert_no_outstanding

    def run(self, env) -> ScalarStoreFlushSequenceResult:
        outer_writes_before = env.get_counter("outer_write_request_count")
        sbuffer_drains_before = env.get_counter("sbuffer_drain_count")
        committed_store = ScalarStoreCommitSequence(
            self.txn,
            expected_mmio=self.expected_mmio,
            require_committed=self.require_committed,
            materialize_cycles=self.materialize_cycles,
            settle_cycles=self.settle_cycles,
        ).run(env)
        store_result = ScalarStoreSequenceResult(
            txn=committed_store.store_result.txn,
            allocated_sq_ptr=committed_store.store_result.allocated_sq_ptr,
            next_sq_ptr=committed_store.store_result.next_sq_ptr,
            store_view=(
                committed_store.committed_store_view
                if committed_store.committed_store_view is not None
                else committed_store.store_result.store_view
            ),
        )
        if self.wait_for_sbuffer_drain:
            env.wait_counter_growth(
                "sbuffer_drain_count",
                sbuffer_drains_before,
                max_cycles=self.sbuffer_wait_cycles,
            )
        drain_summary = FlushStoreBuffersSequence(
            max_cycles=self.max_cycles,
            settle_cycles=self.settle_cycles,
        ).run(env)
        if self.assert_no_outstanding:
            env.assert_no_outstanding()
        return ScalarStoreFlushSequenceResult(
            store_result=store_result,
            drain_summary=drain_summary,
            outer_write_delta=env.get_counter("outer_write_request_count") - outer_writes_before,
            sbuffer_drain_delta=env.get_counter("sbuffer_drain_count") - sbuffer_drains_before,
        )


class ScalarMixedTrafficSequence:
    def __init__(
        self,
        operations,
        *,
        initial_state: SequenceState,
        store_req_id: int = 0,
        first_load_req_id: int = 1,
        expected_store_mmio: bool | None = False,
        require_store_committed: bool = True,
        load_drain_cycles: int | None = None,
        store_materialize_cycles: int | None = None,
        flush: bool = True,
        flush_max_cycles: int | None = None,
        flush_settle_cycles: int | None = None,
        assert_no_outstanding: bool = False,
    ) -> None:
        self.operations = list(operations)
        self.initial_state = initial_state
        self.store_req_id = store_req_id
        self.first_load_req_id = first_load_req_id
        self.expected_store_mmio = expected_store_mmio
        self.require_store_committed = require_store_committed
        self.load_drain_cycles = load_drain_cycles
        self.store_materialize_cycles = store_materialize_cycles
        self.flush = flush
        self.flush_max_cycles = flush_max_cycles
        self.flush_settle_cycles = flush_settle_cycles
        self.assert_no_outstanding = assert_no_outstanding

    def run(self, env) -> ScalarMixedTrafficSequenceResult:
        state = self.initial_state
        completed_before = env.get_completed_load_count()
        total_stores = 0
        total_loads = 0
        next_load_req_id = self.first_load_req_id
        next_store_req_id = self.store_req_id
        drain_summary = None

        for op_kind, addr, maybe_data in self.operations:
            if op_kind == "store":
                store_result = ScalarStoreCommitSequence(
                    StoreTxn(
                        req_id=next_store_req_id,
                        sq_ptr=state.sq_ptr,
                        addr=addr,
                        data=int(maybe_data),
                    ),
                    expected_mmio=self.expected_store_mmio,
                    require_committed=self.require_store_committed,
                    materialize_cycles=self.store_materialize_cycles,
                ).run(env).store_result
                state = SequenceState(
                    next_lq_ptr=state.next_lq_ptr,
                    sq_ptr=store_result.next_sq_ptr,
                )
                total_stores += 1
                next_store_req_id += 1
                continue

            if op_kind != "load":
                raise ValueError(f"unsupported op_kind: {op_kind}")

            load_result = ScalarLoadSequence(
                LoadTxn(
                    req_id=next_load_req_id,
                    addr=addr,
                    lq_ptr=state.next_lq_ptr,
                    sq_ptr=state.sq_ptr,
                    size=8,
                    mask=0xFF,
                ),
                drain_cycles=self.load_drain_cycles,
                expected_completed_loads=completed_before + total_loads + 1,
            ).run(env)
            state = SequenceState(
                next_lq_ptr=load_result.next_lq_ptr,
                sq_ptr=state.sq_ptr,
            )
            total_loads += 1
            next_load_req_id += 1

        if self.flush:
            drain_summary = FlushStoreBuffersSequence(
                max_cycles=self.flush_max_cycles,
                settle_cycles=self.flush_settle_cycles,
            ).run(env)
        if self.assert_no_outstanding:
            env.assert_no_outstanding()
        return ScalarMixedTrafficSequenceResult(
            final_state=state,
            total_stores=total_stores,
            total_loads=total_loads,
            completed_load_count=env.get_completed_load_count(),
            drain_summary=drain_summary,
        )


class ScalarForwardFailReplaySequence:
    def __init__(
        self,
        store_txn,
        *,
        initial_lq_ptr: QueuePtr,
        load_req_id: int,
        replay_wait_cycles: int = 200,
        materialize_cycles: int = 200,
        drain_cycles: int | None = None,
        size: int = 8,
        mask: int = 0xFF,
        assert_no_outstanding: bool = False,
    ) -> None:
        self.store_txn = store_txn
        self.initial_lq_ptr = initial_lq_ptr
        self.load_req_id = load_req_id
        self.replay_wait_cycles = replay_wait_cycles
        self.materialize_cycles = materialize_cycles
        self.drain_cycles = drain_cycles
        self.size = size
        self.mask = mask
        self.assert_no_outstanding = assert_no_outstanding

    def run(self, env) -> ScalarForwardFailReplaySequenceResult:
        completed_before = env.get_completed_load_count()
        env.backend.prepare(self.store_txn)
        store_sq_ptr = enqueue_scalar_store(
            env,
            req_id=self.store_txn.req_id,
            sq_ptr=self.store_txn.sq_ptr,
            enq_port=self.store_txn.enq_port,
            rob_idx=self.store_txn.rob_idx,
        )
        issue_scalar_sta(
            env,
            req_id=self.store_txn.req_id,
            sq_ptr=store_sq_ptr,
            addr=self.store_txn.addr,
            lane=self.store_txn.sta_lane,
            rob_idx=self.store_txn.rob_idx,
            ftq_idx_flag=self.store_txn.resolved_ftq_idx_flag,
            ftq_idx_value=self.store_txn.resolved_ftq_idx_value,
        )
        _wait_store_addr_observed(env, store_sq_ptr.value, self.store_txn.addr, max_cycles=self.materialize_cycles)

        load_txn = LoadTxn(
            req_id=self.load_req_id,
            addr=self.store_txn.addr,
            lq_ptr=self.initial_lq_ptr,
            sq_ptr=ptr_inc(self.store_txn.sq_ptr, env.config.sequence.store_queue_size),
            size=self.size,
            mask=self.mask,
            store_set_hit=1,
        )
        with _capture_load_debug_trace(env, max_events=64) as load_debug_trace:
            send_load(env, load_txn)
            expect_load(env, load_txn)
            sq_forward_event = _wait_sq_forward_event(
                env,
                lane=load_txn.issue_lane,
                expected_sq_idx=store_sq_ptr.value,
                expected_data_invalid_valid=1,
                expected_match_invalid=0,
                expected_forward_invalid=0,
                max_cycles=self.replay_wait_cycles,
            )
            replay_event = env.wait_replay_event(
                cause="FF",
                rob_idx=load_txn.rob_idx,
                max_cycles=self.replay_wait_cycles,
            )

        issue_scalar_std(
            env,
            req_id=self.store_txn.req_id,
            sq_ptr=store_sq_ptr,
            data=self.store_txn.data,
            lane=self.store_txn.std_lane,
            rob_idx=self.store_txn.rob_idx,
            ftq_idx_flag=self.store_txn.resolved_ftq_idx_flag,
            ftq_idx_value=self.store_txn.resolved_ftq_idx_value,
        )
        env.backend.pulse_store_commit(1)
        committed_store_view = env.wait_store_materialized(
            store_sq_ptr.value,
            expected_addr=self.store_txn.addr,
            expected_data=self.store_txn.data,
            expected_mmio=False,
            require_committed=True,
            max_cycles=self.materialize_cycles,
        )
        env.drain_writebacks(max_cycles=_resolve_replay_drain_cycles(env, self.drain_cycles))
        completed = env.get_completed_load_count()
        assert completed == completed_before + 1, f"forward-fail replay load req_id={load_txn.req_id} 未完成"
        load_result = ScalarLoadSequenceResult(
            txn=load_txn,
            next_lq_ptr=ptr_inc(load_txn.lq_ptr, env.config.sequence.load_queue_size),
            completed_load_count=completed,
        )
        final_state = SequenceState(
            next_lq_ptr=load_result.next_lq_ptr,
            sq_ptr=ptr_inc(self.store_txn.sq_ptr, env.config.sequence.store_queue_size),
        )
        if self.assert_no_outstanding:
            env.assert_no_outstanding()
        return ScalarForwardFailReplaySequenceResult(
            store_sq_ptr=store_sq_ptr,
            replay_event=replay_event,
            sq_forward_event=sq_forward_event,
            load_debug_trace=tuple(
                event
                for event in load_debug_trace
                if load_txn.rob_idx_value in {
                    event["s1_rob_idx_value"],
                    event["s2_rob_idx_value"],
                    event["s3_rob_idx_value"],
                }
            ),
            load_result=load_result,
            final_state=final_state,
            committed_store_view=committed_store_view,
        )


class ScalarBankConflictReplaySequence:
    def __init__(
        self,
        *,
        lead_load_txn: LoadTxn,
        victim_load_txns: tuple[LoadTxn, ...] = (),
        replay_wait_cycles: int = 200,
        drain_cycles: int | None = None,
        assert_no_outstanding: bool = False,
    ) -> None:
        self.lead_load_txn = lead_load_txn
        self.victim_load_txns = tuple(victim_load_txns)
        if not self.victim_load_txns:
            raise ValueError("bank conflict sequence 至少需要一个 victim load")
        self.replay_wait_cycles = int(replay_wait_cycles)
        self.drain_cycles = drain_cycles
        self.assert_no_outstanding = assert_no_outstanding

    def run(self, env) -> ScalarBankConflictReplaySequenceResult:
        issued_loads = (self.lead_load_txn, *self.victim_load_txns)
        issue_lanes = [int(txn.issue_lane) for txn in issued_loads]
        if len(set(issue_lanes)) != len(issue_lanes):
            raise ValueError(f"bank conflict sequence 需要所有 load 使用不同 issue lane: lanes={issue_lanes}")

        completed_before = env.get_completed_load_count()
        lead_expected = expect_load(env, self.lead_load_txn)
        victim_expected_pairs = [
            (victim_load_txn, expect_load(env, victim_load_txn))
            for victim_load_txn in self.victim_load_txns
        ]

        with (
            _capture_load_debug_trace(env, max_events=96) as load_debug_trace,
            _capture_writeback_trace(env, max_events=64) as writeback_trace,
        ):
            ScalarLoadBatchSameCycleSequence(issued_loads).run(env)
            load_debug_events = []
            replay_events = []
            for victim_load_txn in self.victim_load_txns:
                load_debug_events.append(
                    _wait_load_debug_event(
                        env,
                        rob_idx_value=victim_load_txn.rob_idx_value,
                        required_replay_causes=("BC",),
                        forbidden_replay_causes=("FF", "NK", "RAW", "RAR"),
                        max_cycles=self.replay_wait_cycles,
                    )
                )
                try:
                    replay_events.append(
                        env.wait_replay_event(
                            rob_idx=victim_load_txn.rob_idx,
                            max_cycles=self.replay_wait_cycles,
                        )
                    )
                except TimeoutError:
                    replay_events.append(None)

            def _trace_has_writeback(*, rob_idx, data):
                for event in writeback_trace:
                    if event.get("int_wen") != 1:
                        continue
                    if event.get("rob_idx_flag") != rob_idx.flag or event.get("rob_idx_value") != rob_idx.value:
                        continue
                    if data is not None and event.get("data") != data:
                        continue
                    return True
                return False

            if not _trace_has_writeback(rob_idx=self.lead_load_txn.rob_idx, data=lead_expected.expected_data):
                env.wait_load_writeback_observed(
                    rob_idx=self.lead_load_txn.rob_idx,
                    data=lead_expected.expected_data,
                    max_cycles=_resolve_replay_drain_cycles(env, self.drain_cycles),
                )
            for victim_load_txn, victim_expected in victim_expected_pairs:
                if not _trace_has_writeback(rob_idx=victim_load_txn.rob_idx, data=victim_expected.expected_data):
                    env.wait_load_writeback_observed(
                        rob_idx=victim_load_txn.rob_idx,
                        data=victim_expected.expected_data,
                        max_cycles=_resolve_replay_drain_cycles(env, self.drain_cycles),
                    )
            completed = _wait_completed_load_count(
                env,
                completed_before + len(issued_loads),
                max_cycles=_resolve_replay_drain_cycles(env, self.drain_cycles),
            )

        final_state = SequenceState(
            next_lq_ptr=ptr_inc(issued_loads[-1].lq_ptr, env.config.sequence.load_queue_size),
            sq_ptr=self.lead_load_txn.sq_ptr,
        )
        if self.assert_no_outstanding:
            env.assert_no_outstanding()

        return ScalarBankConflictReplaySequenceResult(
            issued_loads=issued_loads,
            replay_events=tuple(replay_events),
            load_debug_events=tuple(load_debug_events),
            load_debug_traces=tuple(
                tuple(
                    event
                    for event in load_debug_trace
                    if victim_load_txn.rob_idx_value in {
                        event["s1_rob_idx_value"],
                        event["s2_rob_idx_value"],
                        event["s3_rob_idx_value"],
                    }
                )
                for victim_load_txn in self.victim_load_txns
            ),
            final_state=final_state,
            completed_load_count=completed,
        )


class ScalarCacheMissReplaySequence:
    def __init__(
        self,
        txn,
        *,
        replay_wait_cycles: int = 200,
        drain_cycles: int | None = None,
        expected_completed_loads: int | None = None,
        assert_no_outstanding: bool = False,
    ) -> None:
        self.txn = txn
        self.replay_wait_cycles = replay_wait_cycles
        self.drain_cycles = drain_cycles
        self.expected_completed_loads = expected_completed_loads
        self.assert_no_outstanding = assert_no_outstanding

    def run(self, env) -> ScalarCacheMissReplaySequenceResult:
        stats_before = _snapshot_transport_stats(env)
        send_load(env, self.txn)
        expect_load(env, self.txn)
        replay_event = env.wait_replay_event(
            cause="DM",
            rob_idx=self.txn.rob_idx,
            max_cycles=self.replay_wait_cycles,
        )
        env.drain_writebacks(max_cycles=_resolve_replay_drain_cycles(env, self.drain_cycles))
        completed = env.get_completed_load_count()
        if self.expected_completed_loads is not None:
            assert completed == self.expected_completed_loads, f"cache-miss replay load req_id={self.txn.req_id} 未完成"
        load_result = ScalarLoadSequenceResult(
            txn=self.txn,
            next_lq_ptr=ptr_inc(self.txn.lq_ptr, env.config.sequence.load_queue_size),
            completed_load_count=completed,
        )
        final_state = SequenceState(
            next_lq_ptr=load_result.next_lq_ptr,
            sq_ptr=self.txn.sq_ptr,
        )
        if self.assert_no_outstanding:
            env.assert_no_outstanding()
        return ScalarCacheMissReplaySequenceResult(
            load_result=load_result,
            replay_event=replay_event,
            final_state=final_state,
            transport_stats_before=stats_before,
            transport_stats_after=_snapshot_transport_stats(env),
        )


class ScalarNcReplaySequence:
    def __init__(
        self,
        txn,
        *,
        replay_wait_cycles: int = 200,
        drain_cycles: int | None = None,
        expected_completed_loads: int | None = None,
        assert_no_outstanding: bool = False,
    ) -> None:
        self.txn = txn
        self.replay_wait_cycles = replay_wait_cycles
        self.drain_cycles = drain_cycles
        self.expected_completed_loads = expected_completed_loads
        self.assert_no_outstanding = assert_no_outstanding

    def run(self, env) -> ScalarNcReplaySequenceResult:
        stats_before = _snapshot_transport_stats(env)
        send_load(env, self.txn)
        expect_load(env, self.txn)
        replay_event = env.wait_nc_replay_or_nc_out(
            rob_idx=self.txn.rob_idx,
            max_cycles=self.replay_wait_cycles,
        )
        env.drain_writebacks(max_cycles=_resolve_replay_drain_cycles(env, self.drain_cycles))
        completed = env.get_completed_load_count()
        if self.expected_completed_loads is not None:
            assert completed == self.expected_completed_loads, f"nc replay load req_id={self.txn.req_id} 未完成"
        load_result = ScalarLoadSequenceResult(
            txn=self.txn,
            next_lq_ptr=ptr_inc(self.txn.lq_ptr, env.config.sequence.load_queue_size),
            completed_load_count=completed,
        )
        final_state = SequenceState(
            next_lq_ptr=load_result.next_lq_ptr,
            sq_ptr=self.txn.sq_ptr,
        )
        if self.assert_no_outstanding:
            env.assert_no_outstanding()
        return ScalarNcReplaySequenceResult(
            load_result=load_result,
            replay_event=replay_event,
            final_state=final_state,
            transport_stats_before=stats_before,
            transport_stats_after=_snapshot_transport_stats(env),
        )


from .mmu_sequences import (
    MmuPrimeLoadSpec,
    MmuSv39ActivateSequence,
    MmuSv39ActivateSequenceResult,
    MmuSv39AddressSpaceConfig,
    MmuSv39AddressSpaceInstallSequence,
    MmuSv39AddressSpaceInstallSequenceResult,
    Sv39GigapageMapping,
    TranslatedU64MemoryPreload,
    U64MemoryPreload,
)
from .violation_sequences import (
    ScalarRarViolationSequence,
    ScalarRarViolationSequenceResult,
    ScalarRawReplaySequence,
    ScalarRawReplaySequenceResult,
    ScalarSqDataInvalidMatchInvalidTriggerSequence,
    ScalarSqDataInvalidMatchInvalidTriggerSequenceResult,
)
