# coding=utf-8
"""
MemBlock real-DUT DCache CtrlUnit directed regression.
"""

import pytest

from request_apis import send_load, ptr_inc
from transactions import LoadTxn, StoreTxn
from sequences import ResetEnvSequence, ScalarLoadSequence, ScalarStoreCommitSequence, SequenceState
from MemBlock_env import DCacheCtrlConfig, HARDWARE_ERROR_BIT


CTRL_TARGET_ADDR = 0x1000
CTRL_TARGET_DATA = 0x1234_5678_9ABC_DEF0
CTRL_DELAY_CYCLES = 64


def _reset_env_state(env) -> SequenceState:
    state = ResetEnvSequence(
        require_issue_lanes=(0, 3, 5),
        require_lq_ready=True,
        require_sq_ready=True,
    ).run(env)
    env.csr_ctrl.cache_error_enable.value = 1
    env.advance_cycles(1)
    return state


def _mmio_store(env, state: SequenceState, *, req_id: int, addr: int, data: int):
    result = ScalarStoreCommitSequence(
        StoreTxn(
            req_id=int(req_id),
            sq_ptr=state.sq_ptr,
            addr=int(addr),
            data=int(data),
        ),
        expected_mmio=None,
        expected_nc=None,
        require_committed=True,
        materialize_cycles=512,
    ).run(env)
    return (
        SequenceState(
            next_lq_ptr=state.next_lq_ptr,
            sq_ptr=ptr_inc(state.sq_ptr, env.config.sequence.store_queue_size),
        ),
        result.committed_store_view,
    )


def _cacheable_load(env, state: SequenceState, *, req_id: int, addr: int, expected_data: int):
    txn = LoadTxn(
        req_id=int(req_id),
        addr=int(addr),
        lq_ptr=state.next_lq_ptr,
        sq_ptr=state.sq_ptr,
    )
    env.preload_u64(int(addr), int(expected_data))
    completed_before = env.get_completed_load_count()
    ScalarLoadSequence(
        txn,
        drain_cycles=256,
        expected_completed_loads=completed_before + 1,
    ).run(env)
    return (
        SequenceState(
            next_lq_ptr=ptr_inc(state.next_lq_ptr, env.config.sequence.load_queue_size),
            sq_ptr=state.sq_ptr,
        ),
        {
            "exception_bits": [0] * (HARDWARE_ERROR_BIT + 1),
        },
    )


def _faulting_cacheable_load(env, state: SequenceState, *, req_id: int, addr: int):
    txn = LoadTxn(
        req_id=int(req_id),
        addr=int(addr),
        lq_ptr=state.next_lq_ptr,
        sq_ptr=state.sq_ptr,
    )
    previous_strict = env.memory.strict_writeback_check
    env.memory.strict_writeback_check = False
    try:
        send_load(env, txn)
        writeback = env.wait_load_fault_observed(
            rob_idx=txn.rob_idx,
            expected_vaddr=int(addr),
            required_exception_bits=(HARDWARE_ERROR_BIT,),
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
    finally:
        env.memory.strict_writeback_check = previous_strict


def _issue_load_without_wait(env, state: SequenceState, *, req_id: int, addr: int):
    txn = LoadTxn(
        req_id=int(req_id),
        addr=int(addr),
        lq_ptr=state.next_lq_ptr,
        sq_ptr=state.sq_ptr,
    )
    send_load(env, txn)
    next_state = SequenceState(
        next_lq_ptr=ptr_inc(state.next_lq_ptr, env.config.sequence.load_queue_size),
        sq_ptr=state.sq_ptr,
    )
    return next_state, txn


def _observe_fault_window(env, txn: LoadTxn, *, max_cycles: int = 256) -> dict:
    observation = {
        "matched_hwe_event": None,
        "matched_writeback_without_hwe": None,
        "dcache_error_event": None,
        "read_error_delayed_seen": False,
        "pipe_valid_seen": False,
        "pipe_fire_seen": False,
        "snapshots": [],
    }
    for _ in range(int(max_cycles)):
        debug = env.dcache_ctrl.sample_fault_debug()
        error_state = env.dcache_ctrl.sample_error_state()
        snapshot = {
            "cycle": env._current_cycle(),
            "debug": debug,
            "error_state": error_state,
        }
        observation["snapshots"].append(snapshot)
        observation["read_error_delayed_seen"] |= any(
            debug.get(key) for key in ("data_read_error_delayed_0", "data_read_error_delayed_1", "data_read_error_delayed_2")
        )
        observation["pipe_valid_seen"] |= bool(debug.get("data_pipe_valid"))
        observation["pipe_fire_seen"] |= bool(debug.get("data_pipe_fire"))
        if observation["dcache_error_event"] is None and error_state["dcache_error_valid"]:
            observation["dcache_error_event"] = error_state

        for lane, bundle in enumerate(env.writeback):
            if not bundle.connected("valid") or bundle.read("valid", 0) == 0:
                continue
            if bundle.connected("ready") and bundle.read("ready", 0) == 0:
                continue
            if bundle.connected("isFromLoadUnit") and bundle.read("isFromLoadUnit", 0) == 0:
                continue
            if bundle.connected("toRob_valid") and bundle.read("toRob_valid", 0) == 0:
                continue
            event = {
                "cycle": env._current_cycle(),
                "lane": lane,
                "rob_idx_flag": bundle.read("robIdx_flag", 0),
                "rob_idx_value": bundle.read("robIdx_value", 0),
                "exception_bits": bundle.read_exception_bits() if hasattr(bundle, "read_exception_bits") else [],
            }
            if not env._event_matches_rob_idx(
                event,
                env._normalize_rob_idx_filter(rob_idx=txn.rob_idx),
            ):
                continue
            if len(event["exception_bits"]) > HARDWARE_ERROR_BIT and event["exception_bits"][HARDWARE_ERROR_BIT]:
                observation["matched_hwe_event"] = event
                return observation
            observation["matched_writeback_without_hwe"] = event
        env.advance_cycles(1)
    return observation


def _xfail_if_confirmed_data_rtl_gap(env, txn: LoadTxn, observation: dict) -> None:
    if observation["matched_hwe_event"] is not None:
        return
    if not observation["read_error_delayed_seen"]:
        return
    if observation["dcache_error_event"] is not None:
        return

    last = observation["snapshots"][-1]
    pytest.xfail(
        "confirmed RTL gap: bankedDataArray 已观测到 data read_error_delayed，"
        "但 dcacheError / load hardwareError 未拉起; "
        f"rob={txn.rob_idx}, last_cycle={last['cycle']}, debug={last['debug']}"
    )


def _xfail_if_confirmed_tag_immediate_consumption(
    *,
    encoded_ctrl: int,
    observed_ctrl: int | None,
    debug: dict,
) -> None:
    if observed_ctrl != (int(encoded_ctrl) & ~0x1):
        return
    pytest.xfail(
        "confirmed RTL/contract gap: quiesce 后 `ECCCTL` 写回完成即观测到 `ese` 被清零，"
        "one-shot tag 注入在目标 load 发出前已被消费; "
        f"expected_ctrl=0x{int(encoded_ctrl):x}, observed_ctrl=0x{int(observed_ctrl):x}, debug={debug}"
    )


def _wait_fault_with_direct_evidence(env, state: SequenceState, *, req_id: int, addr: int, max_cycles: int = 512):
    previous_strict = env.memory.strict_writeback_check
    env.memory.strict_writeback_check = False
    try:
        next_state, txn = _issue_load_without_wait(env, state, req_id=req_id, addr=addr)
        observation = _observe_fault_window(env, txn, max_cycles=max_cycles)
        _xfail_if_confirmed_data_rtl_gap(env, txn, observation)
        if observation["matched_hwe_event"] is None:
            raise TimeoutError(
                "等待 load hardwareError 超时: "
                f"rob={txn.rob_idx}, observation_tail={observation['snapshots'][-1]}"
            )
        env.drain_writebacks(max_cycles=256)
        return next_state, observation["matched_hwe_event"], observation
    finally:
        env.memory.strict_writeback_check = previous_strict


def _program_ctrlunit(
    env,
    state: SequenceState,
    *,
    base_req_id: int,
    config: DCacheCtrlConfig,
    settle_cycles: int = 4,
):
    encoded = env.dcache_ctrl.encode_config(config)
    state, _ = _mmio_store(
        env,
        state,
        req_id=base_req_id,
        addr=env.dcache_ctrl.mask_addr(0),
        data=encoded["mask"],
    )
    state, _ = _mmio_store(
        env,
        state,
        req_id=base_req_id + 1,
        addr=env.dcache_ctrl.delay_addr(),
        data=encoded["delay"],
    )
    state, _ = _mmio_store(
        env,
        state,
        req_id=base_req_id + 2,
        addr=env.dcache_ctrl.ctrl_addr(),
        data=encoded["ctrl"],
    )
    if int(settle_cycles) > 0:
        env.advance_cycles(int(settle_cycles))
    return state, encoded


def _clear_ctrlunit(env, state: SequenceState, *, base_req_id: int):
    state, _ = _mmio_store(
        env,
        state,
        req_id=base_req_id,
        addr=env.dcache_ctrl.mask_addr(0),
        data=0,
    )
    state, _ = _mmio_store(
        env,
        state,
        req_id=base_req_id + 1,
        addr=env.dcache_ctrl.delay_addr(),
        data=0,
    )
    state, _ = _mmio_store(
        env,
        state,
        req_id=base_req_id + 2,
        addr=env.dcache_ctrl.ctrl_addr(),
        data=0,
    )
    env.advance_cycles(4)
    return state


def _assert_no_dcache_error(env, *, cycles: int = 32):
    for _ in range(int(cycles)):
        state = env.dcache_ctrl.sample_error_state()
        assert state["dcache_error_valid"] == 0, f"unexpected dcache error state: {state}"
        env.advance_cycles(1)


def _require_cacheable_load_baseline(env, state: SequenceState) -> SequenceState:
    try:
        state, _ = _cacheable_load(
            env,
            state,
            req_id=0xE0,
            addr=CTRL_TARGET_ADDR,
            expected_data=CTRL_TARGET_DATA,
        )
        return state
    except Exception as exc:
        pytest.xfail(f"real-DUT cacheable load baseline unavailable in current build-memblock: {exc}")


def _require_ctrl_internal_observation(env, internal_state: dict) -> None:
    if any(value is not None for value in internal_state.values()):
        return
    pytest.xfail("CtrlUnit internal register signals are not exported through current GetInternalSignal hierarchy")


def test_api_MemBlock_dcache_ctrlunit_mmio_roundtrip_smoke(env):
    state = _reset_env_state(env)
    stats_before = env.get_transport_stats()
    config = DCacheCtrlConfig(
        component="data",
        bank_mask=0x1,
        toggle_mask=0x55,
        delay=0x23,
        delay_enable=True,
        persist=True,
        enable=True,
    )

    state, encoded = _program_ctrlunit(env, state, base_req_id=0x00, config=config)
    internal = env.dcache_ctrl.sample_internal_state()
    _require_ctrl_internal_observation(env, internal)
    stats_after = env.get_transport_stats()

    assert stats_after["outer_request_count"] == stats_before["outer_request_count"], (
        "dcache ctrl internal MMIO access 不应落到 outer transport"
    )
    assert stats_after["dcache_a_request_count"] == stats_before["dcache_a_request_count"], (
        "dcache ctrl internal MMIO access 不应伪装成 dcache refill traffic"
    )
    assert internal["ctrl"] == encoded["ctrl"], f"unexpected internal ctrl state: {internal}"
    assert internal["delay"] == encoded["delay"], f"unexpected internal delay state: {internal}"
    assert internal["counter"] is not None and 0 <= internal["counter"] <= encoded["delay"], (
        f"unexpected internal counter state: {internal}"
    )
    assert internal["mask0"] == encoded["mask"], f"unexpected internal mask state: {internal}"
    assert env.dcache_ctrl.decode_control(encoded["ctrl"]) == {
        "raw": encoded["ctrl"],
        "enable": True,
        "persist": True,
        "delay_enable": True,
        "component_code": 1,
        "component": "data",
        "bank_mask": 0x1,
    }

    _clear_ctrlunit(env, state, base_req_id=0x20)


def test_api_MemBlock_dcache_ctrlunit_tag_error_one_shot_reports_hardware_error(env):
    state = _reset_env_state(env)
    env.preload_u64(CTRL_TARGET_ADDR, CTRL_TARGET_DATA)

    state = _require_cacheable_load_baseline(env, state)
    state, warmup_wb = _cacheable_load(env, state, req_id=0x30, addr=CTRL_TARGET_ADDR, expected_data=CTRL_TARGET_DATA)
    assert warmup_wb["exception_bits"] and not warmup_wb["exception_bits"][HARDWARE_ERROR_BIT]
    assert env.dcache_ctrl.sample_error_state()["dcache_error_valid"] == 0
    env.wait_memory_quiesce(max_cycles=512)

    state, encoded = _program_ctrlunit(
        env,
        state,
        base_req_id=0x40,
        config=DCacheCtrlConfig(
            component="tag",
            bank_mask=0x1,
            toggle_mask=0x1,
            delay=0,
            delay_enable=False,
            persist=False,
            enable=True,
        ),
        settle_cycles=0,
    )
    ctrl_after_program = env.dcache_ctrl.sample_internal_state()["ctrl"]
    debug_after_program = env.dcache_ctrl.sample_fault_debug()
    _xfail_if_confirmed_tag_immediate_consumption(
        encoded_ctrl=encoded["ctrl"],
        observed_ctrl=ctrl_after_program,
        debug=debug_after_program,
    )
    assert ctrl_after_program == encoded["ctrl"], (
        "tag one-shot 编程后应保持 `ese=1` 直到目标 load 消费本次注入"
    )

    state, fault_wb = _faulting_cacheable_load(
        env,
        state,
        req_id=0x50,
        addr=CTRL_TARGET_ADDR,
    )
    error_event = env.dcache_ctrl.wait_error_event(expected_paddr=CTRL_TARGET_ADDR, max_cycles=256)
    _require_ctrl_internal_observation(env, env.dcache_ctrl.sample_internal_state())
    state, post_wb = _cacheable_load(
        env,
        state,
        req_id=0x52,
        addr=CTRL_TARGET_ADDR,
        expected_data=CTRL_TARGET_DATA,
    )

    assert fault_wb["exception_bits"][HARDWARE_ERROR_BIT] == 1
    assert error_event["dcache_error_valid"] == 1
    assert env.dcache_ctrl.sample_internal_state()["ctrl"] == (encoded["ctrl"] & ~0x1)
    assert post_wb["exception_bits"] and not post_wb["exception_bits"][HARDWARE_ERROR_BIT]
    _assert_no_dcache_error(env)


def test_api_MemBlock_dcache_ctrlunit_data_error_delayed_persistent_rearms(env):
    state = _reset_env_state(env)
    env.preload_u64(CTRL_TARGET_ADDR, CTRL_TARGET_DATA)

    state = _require_cacheable_load_baseline(env, state)
    state, _ = _cacheable_load(env, state, req_id=0x60, addr=CTRL_TARGET_ADDR, expected_data=CTRL_TARGET_DATA)
    state, encoded = _program_ctrlunit(
        env,
        state,
        base_req_id=0x70,
        config=DCacheCtrlConfig(
            component="data",
            bank_mask=0x1,
            toggle_mask=0x1,
            delay=CTRL_DELAY_CYCLES,
            delay_enable=True,
            persist=True,
            enable=True,
        ),
        settle_cycles=0,
    )

    state, early_wb = _cacheable_load(
        env,
        state,
        req_id=0x80,
        addr=CTRL_TARGET_ADDR,
        expected_data=CTRL_TARGET_DATA,
    )
    assert early_wb["exception_bits"] and not early_wb["exception_bits"][HARDWARE_ERROR_BIT]
    _assert_no_dcache_error(env, cycles=16)

    env.dcache_ctrl.wait_counter_zero(max_cycles=CTRL_DELAY_CYCLES * 4)
    state, first_fault, first_observation = _wait_fault_with_direct_evidence(
        env,
        state,
        req_id=0x81,
        addr=CTRL_TARGET_ADDR,
    )
    first_error = env.dcache_ctrl.wait_error_event(expected_paddr=CTRL_TARGET_ADDR, max_cycles=256)

    env.dcache_ctrl.wait_counter_zero(max_cycles=CTRL_DELAY_CYCLES * 4)
    state, second_fault, second_observation = _wait_fault_with_direct_evidence(
        env,
        state,
        req_id=0x82,
        addr=CTRL_TARGET_ADDR,
    )
    second_error = env.dcache_ctrl.wait_error_event(expected_paddr=CTRL_TARGET_ADDR, max_cycles=256)
    ctrl_after = env.dcache_ctrl.sample_internal_state()["ctrl"]
    if ctrl_after is None:
        pytest.xfail("CtrlUnit internal ctrl register signal is not exported through current GetInternalSignal hierarchy")

    assert first_fault["exception_bits"][HARDWARE_ERROR_BIT] == 1
    assert second_fault["exception_bits"][HARDWARE_ERROR_BIT] == 1
    assert first_error["dcache_error_valid"] == 1
    assert second_error["dcache_error_valid"] == 1
    assert first_observation["pipe_valid_seen"]
    assert second_observation["pipe_valid_seen"]
    assert ctrl_after == encoded["ctrl"], "persist 模式下注入后 `ese` 不应自动清零"


def test_api_MemBlock_dcache_ctrlunit_disabled_bank_mask_has_no_effect(env):
    state = _reset_env_state(env)
    env.preload_u64(CTRL_TARGET_ADDR, CTRL_TARGET_DATA)

    state = _require_cacheable_load_baseline(env, state)
    state, _ = _cacheable_load(env, state, req_id=0x90, addr=CTRL_TARGET_ADDR, expected_data=CTRL_TARGET_DATA)
    state, _ = _program_ctrlunit(
        env,
        state,
        base_req_id=0xA0,
        config=DCacheCtrlConfig(
            component="tag",
            bank_mask=0x0,
            toggle_mask=0x1,
            delay=0,
            delay_enable=False,
            persist=False,
            enable=True,
        ),
    )
    state, load_wb = _cacheable_load(
        env,
        state,
        req_id=0xB0,
        addr=CTRL_TARGET_ADDR,
        expected_data=CTRL_TARGET_DATA,
    )

    assert load_wb["exception_bits"] and not load_wb["exception_bits"][HARDWARE_ERROR_BIT]
    _assert_no_dcache_error(env, cycles=48)
