# coding=utf-8
"""
LSQ Web UI backend unit tests.

These tests only exercise the Python-side tracker/publisher logic and do not
instantiate the MemBlock DUT.
"""

from copy import deepcopy

from lsq_webui_backend import LQRAW_SIZE, LQRAR_SIZE, LQR_SIZE, SQ_SIZE, VLQ_SIZE, LsqStateTracker, TopicPublisher


def _empty_lane_ptr():
    return {"flag": 0, "value": 0}


def _make_raw(cycle: int = 0):
    return {
        "cycle": cycle,
        "overview": {
            "lq_can_accept": 1,
            "sq_can_accept": 1,
            "lq_empty": 0,
            "lq_full": 0,
            "sq_empty": 0,
            "sq_full": 0,
            "rar_valid_count": 0,
            "no_uops_issued": 0,
            "load_misalign_full": 0,
            "lq_cancel_cnt": 0,
            "sq_cancel_cnt": 0,
        },
        "enq": {
            "can_accept": 1,
            "need_alloc": [0] * 8,
            "req": [
                {
                    "valid": 0,
                    "fu_type": 0,
                    "fu_op_type": 0,
                    "rob_idx": _empty_lane_ptr(),
                    "lq_idx": _empty_lane_ptr(),
                    "sq_idx": _empty_lane_ptr(),
                    "num_ls_elem": 0,
                }
                for _ in range(8)
            ],
            "resp": [
                {
                    "lq_idx": _empty_lane_ptr(),
                    "sq_idx": _empty_lane_ptr(),
                }
                for _ in range(8)
            ],
        },
        "lq": {"deq": 0, "deq_ptr": _empty_lane_ptr()},
        "sq": {
            "deq": 0,
            "deq_ptr": _empty_lane_ptr(),
            "commit_ptr": _empty_lane_ptr(),
            "commit_rob_idx": _empty_lane_ptr(),
            "commit_uop_idx": 0,
        },
        "release": {"valid": 0, "paddr": 0},
        "debug_topdown": {
            "rob_head_tlb_replay": 0,
            "rob_head_tlb_miss": 0,
            "rob_head_load_vio": 0,
            "rob_head_load_mshr": 0,
        },
        "tlb_hint": {"replay_all": 0},
        "replay_lanes": [
            {
                "lane": idx,
                "valid": 0,
                "ready": 0,
                "sched_index": -1,
                "tlb_miss": 0,
                "lq_idx": _empty_lane_ptr(),
                "sq_idx": _empty_lane_ptr(),
                "rob_idx": _empty_lane_ptr(),
                "uop_idx": 0,
                "pdest": 0,
                "vaddr": 0,
                "is_vec": 0,
                "is_128bit": 0,
                "mask": 0,
            }
            for idx in range(3)
        ],
        "store_data": [
            {"lane": idx, "valid": 0, "sq_idx": _empty_lane_ptr(), "fu_type": 0, "fu_op_type": 0, "data": 0}
            for idx in range(2)
        ],
        "store_addr": [
            {"lane": idx, "sq_idx": _empty_lane_ptr(), "paddr": 0, "nc": 0, "mmio": 0}
            for idx in range(2)
        ],
        "store_addr_re": [
            {"lane": idx, "update_addr_valid": 0, "sq_idx": _empty_lane_ptr()}
            for idx in range(2)
        ],
        "ldu_lanes": [
            {
                "lane": idx,
                "valid": 0,
                "is_load_replay": 0,
                "sched_index": -1,
                "update_addr_valid": 1,
                "lq_idx": _empty_lane_ptr(),
                "sq_idx": _empty_lane_ptr(),
                "rob_idx": _empty_lane_ptr(),
                "uop_idx": 0,
                "paddr": 0,
                "vaddr": 0,
                "nc_with_data": 0,
                "mmio": 0,
                "handled_by_mshr": 0,
                "addr_inv_sq_idx": _empty_lane_ptr(),
                "cause_bits": {name: 0 for name in ["MA", "TM", "FF", "DR", "DM", "WF", "BC", "RAR", "RAW", "NK", "MF"]},
            }
            for idx in range(3)
        ],
        "nc_out_lanes": [
            {
                "lane": idx,
                "valid": 0,
                "ready": 0,
                "sched_index": -1,
                "lq_idx": _empty_lane_ptr(),
                "sq_idx": _empty_lane_ptr(),
                "rob_idx": _empty_lane_ptr(),
                "uop_idx": 0,
                "vaddr": 0,
                "paddr": 0,
                "is_vec": 0,
                "is_128bit": 0,
            }
            for idx in range(3)
        ],
        "writeback_lanes": [
            {
                "lane": idx,
                "valid": 0,
                "ready": 1,
                "rob_idx": _empty_lane_ptr(),
                "lq_idx": _empty_lane_ptr(),
                "sq_idx": _empty_lane_ptr(),
                "pdest": 0,
                "data": 0,
                "int_wen": 0,
                "is_from_load_unit": 0,
                "is_mmio": 0,
                "is_ncio": 0,
                "paddr": 0,
                "vaddr": 0,
            }
            for idx in range(7)
        ],
        "direct": {
            "vlq": {"available": False, "entries": []},
            "lqr": {"available": False, "entries": []},
            "sq": {"available": False, "entries": []},
        },
        "perf_values": [0] * 36,
    }


def test_lsq_webui_tracker_builds_shadow_vlq_and_sq_entries():
    tracker = LsqStateTracker()

    raw = _make_raw(cycle=1)
    raw["enq"]["need_alloc"][0] = 0x3
    raw["enq"]["req"][0]["valid"] = 1
    raw["enq"]["req"][0]["fu_type"] = 0x12
    raw["enq"]["req"][0]["fu_op_type"] = 0x34
    raw["enq"]["req"][0]["rob_idx"] = {"flag": 1, "value": 9}
    raw["enq"]["req"][0]["num_ls_elem"] = 2
    raw["enq"]["resp"][0]["lq_idx"] = {"flag": 0, "value": 5}
    raw["enq"]["resp"][0]["sq_idx"] = {"flag": 0, "value": 7}
    raw["store_addr_re"][0]["update_addr_valid"] = 1
    raw["store_addr_re"][0]["sq_idx"] = {"flag": 0, "value": 7}
    raw["store_data"][0]["valid"] = 1
    raw["store_data"][0]["sq_idx"] = {"flag": 0, "value": 7}

    tracker.update(raw)

    assert tracker.vlq_entries[5]["allocated"] is True
    assert tracker.vlq_entries[5]["request"]["fu_type"] == 0x12
    assert tracker.vlq_entries[5]["request"]["fu_op_type"] == 0x34
    assert tracker.vlq_entries[5]["request"]["rob_idx"] == {"flag": 1, "value": 9}
    assert tracker.vlq_entries[5]["request"]["num_ls_elem"] == 2
    assert tracker.sq_entries[7]["allocated"] is True
    assert tracker.sq_entries[7]["addrvalid"] is True
    assert tracker.sq_entries[7]["datavalid"] is True
    assert tracker.vlq_stats["enq"] == 1


def test_lsq_webui_tracker_collects_replay_and_violation_data():
    tracker = LsqStateTracker()

    raw = _make_raw(cycle=4)
    raw["replay_lanes"][1]["valid"] = 1
    raw["replay_lanes"][1]["ready"] = 1
    raw["replay_lanes"][1]["sched_index"] = 9
    raw["replay_lanes"][1]["tlb_miss"] = 1
    raw["replay_lanes"][1]["vaddr"] = 0x123456
    raw["replay_lanes"][1]["pdest"] = 17
    raw["ldu_lanes"][0]["valid"] = 1
    raw["ldu_lanes"][0]["is_load_replay"] = 1
    raw["ldu_lanes"][0]["sched_index"] = 11
    raw["ldu_lanes"][0]["cause_bits"]["RAW"] = 1
    raw["ldu_lanes"][0]["update_addr_valid"] = 0
    raw["ldu_lanes"][0]["paddr"] = 0x80000040
    raw["nc_out_lanes"][2]["valid"] = 1
    raw["nc_out_lanes"][2]["ready"] = 1
    raw["nc_out_lanes"][2]["sched_index"] = 13
    raw["nc_out_lanes"][2]["paddr"] = 0x80000100
    raw["overview"]["rar_valid_count"] = 3
    raw["release"]["valid"] = 1
    raw["release"]["paddr"] = 0x1234

    tracker.update(raw)

    assert tracker.lqr_entries[9]["allocated"] is True
    assert tracker.lqr_entries[11]["allocated"] is True
    assert tracker.lqr_entries[11]["blocking"] is True
    assert tracker.lqr_entries[11]["source"] == "ldu"
    assert tracker.lqr_entries[11]["detail"]["paddr"] == 0x80000040
    assert tracker.lqr_entries[13]["allocated"] is True
    assert tracker.lqr_entries[13]["scheduled"] is True
    assert tracker.lqr_entries[13]["source"] == "nc"
    assert tracker.lqr_entries[13]["detail"]["paddr"] == 0x80000100
    assert tracker.lqr_entries[9]["detail"]["vaddr"] == 0x123456
    assert tracker.lqr_entries[9]["detail"]["pdest"] == 17
    assert tracker.replay_cause_counts["TM"] >= 1
    assert tracker.replay_cause_counts["RAW"] == 1
    assert tracker.replay_cause_counts["NC"] == 1
    assert tracker.raw_stats["waiting"] == 1
    assert tracker.rar_stats["allocated"] == 3
    assert tracker.rar_stats["released"] == 1
    assert any(lane["source"] == "ldu" and lane["sched_index"] == 11 for lane in tracker.replay_lanes)
    assert any(lane["source"] == "nc" and lane["sched_index"] == 13 for lane in tracker.replay_lanes)
    assert tracker.events_backlog()[0]["type"] in {"violation", "replay"}


def test_lsq_webui_tracker_uses_real_queue_sizes_and_ptr_deltas():
    tracker = LsqStateTracker()

    first = _make_raw(cycle=1)
    first["enq"]["need_alloc"][0] = 0x1
    first["enq"]["req"][0]["valid"] = 1
    first["enq"]["resp"][0]["lq_idx"] = {"flag": 0, "value": 5}
    tracker.update(first)

    second = _make_raw(cycle=2)
    second["lq"]["deq"] = 0
    second["lq"]["deq_ptr"] = {"flag": 0, "value": 1}
    second["sq"]["commit_ptr"] = {"flag": 0, "value": 1}
    second["sq"]["deq_ptr"] = {"flag": 0, "value": 1}
    tracker.update(second)

    assert len(tracker.vlq_entries) == VLQ_SIZE
    assert len(tracker.lqr_entries) == LQR_SIZE
    assert len(tracker.sq_entries) == SQ_SIZE
    assert len(tracker.rar_entries) == LQRAR_SIZE
    assert len(tracker.raw_entries) == LQRAW_SIZE
    assert tracker.vlq_stats["deq"] == 1
    assert tracker.vlq_entries[0]["allocated"] is False

    payload = tracker.topic_payload(
        "vlq",
        {"running": False, "step_cycles": 1, "tick_ms": 100, "last_raw": second},
    )
    assert payload["size"] == VLQ_SIZE
    assert payload["stats"]["deq"] == 1


def test_lsq_webui_tracker_reset_snapshot_clears_shadow_queues_and_counts():
    tracker = LsqStateTracker()

    active = _make_raw(cycle=7)
    active["replay_lanes"][0]["valid"] = 1
    active["replay_lanes"][0]["ready"] = 1
    active["replay_lanes"][0]["sched_index"] = 3
    active["ldu_lanes"][1]["valid"] = 1
    active["ldu_lanes"][1]["is_load_replay"] = 1
    active["ldu_lanes"][1]["sched_index"] = 5
    active["ldu_lanes"][1]["cause_bits"]["RAW"] = 1
    active["nc_out_lanes"][2]["valid"] = 1
    active["nc_out_lanes"][2]["ready"] = 1
    active["nc_out_lanes"][2]["sched_index"] = 7
    active["store_data"][0]["valid"] = 1
    active["store_data"][0]["sq_idx"] = {"flag": 0, "value": 4}
    active["store_addr_re"][0]["update_addr_valid"] = 1
    active["store_addr_re"][0]["sq_idx"] = {"flag": 0, "value": 4}
    tracker.update(active)

    reset_raw = _make_raw(cycle=0)
    reset_raw["overview"]["sq_empty"] = 1
    reset_raw["overview"]["lq_empty"] = 1
    reset_raw["replay_lanes"][0]["valid"] = 1
    reset_raw["replay_lanes"][0]["sched_index"] = 3
    reset_raw["store_data"][0]["valid"] = 1
    reset_raw["store_data"][0]["sq_idx"] = {"flag": 0, "value": 4}

    tracker.load_reset_snapshot(reset_raw)

    assert sum(1 for entry in tracker.sq_entries if entry["allocated"]) == 0
    assert sum(1 for entry in tracker.lqr_entries if entry["allocated"]) == 0
    assert tracker.sq_stats["fwd_req"] == 0
    assert tracker.replay_cause_counts["TM"] == 0
    assert tracker.replay_cause_counts["RAW"] == 0
    assert tracker.replay_cause_counts["NC"] == 0
    assert tracker.replay_lanes == []
    assert tracker.ldu_lanes == []
    assert tracker.nc_out_lanes == []


def test_lsq_webui_tracker_builds_trace_payload_and_quality_metadata():
    tracker = LsqStateTracker()

    first = _make_raw(cycle=1)
    first["enq"]["need_alloc"][0] = 0x1
    first["enq"]["req"][0]["valid"] = 1
    first["enq"]["req"][0]["rob_idx"] = {"flag": 1, "value": 9}
    first["enq"]["resp"][0]["lq_idx"] = {"flag": 0, "value": 5}
    tracker.update(first)

    second = _make_raw(cycle=2)
    second["replay_lanes"][0]["valid"] = 1
    second["replay_lanes"][0]["ready"] = 1
    second["replay_lanes"][0]["sched_index"] = 15
    second["replay_lanes"][0]["lq_idx"] = {"flag": 0, "value": 5}
    second["replay_lanes"][0]["rob_idx"] = {"flag": 1, "value": 9}
    second["writeback_lanes"][1]["valid"] = 1
    second["writeback_lanes"][1]["rob_idx"] = {"flag": 1, "value": 9}
    second["writeback_lanes"][1]["lq_idx"] = {"flag": 0, "value": 5}
    second["writeback_lanes"][1]["pdest"] = 23
    tracker.update(second)

    traces_payload = tracker.topic_payload("traces", {"trace_filters": {"lq_idx": "5"}})
    assert traces_payload["data_quality"] == "mixed"
    assert traces_payload["total_matching"] >= 1
    trace = traces_payload["active_requests"][0]
    assert trace["identifiers"]["lq_idx"] == {"flag": 0, "value": 5}
    assert {event["type"] for event in trace["events"]} >= {"enq", "replay_fire", "writeback"}
    assert trace["completed"] is True

    vlq_payload = tracker.topic_payload("vlq", {"running": False, "step_cycles": 1, "tick_ms": 100, "last_raw": second})
    assert vlq_payload["data_quality"] == "derived"
    assert vlq_payload["degraded_reasons"]


def test_lsq_webui_tracker_prefers_direct_queue_state_when_available():
    tracker = LsqStateTracker()

    raw = _make_raw(cycle=3)
    raw["enq"]["need_alloc"][0] = 0x3
    raw["enq"]["req"][0]["valid"] = 1
    raw["enq"]["resp"][0]["lq_idx"] = {"flag": 0, "value": 5}
    raw["enq"]["resp"][0]["sq_idx"] = {"flag": 0, "value": 7}
    raw["replay_lanes"][0]["valid"] = 1
    raw["replay_lanes"][0]["ready"] = 1
    raw["replay_lanes"][0]["sched_index"] = 9

    raw["direct"]["vlq"] = {
        "available": True,
        "entries": [
            {
                "index": idx,
                "allocated": idx == 12,
                "request": {
                    "source": "direct",
                    "lq_idx": {"flag": 0, "value": idx},
                    "rob_idx": {"flag": 1, "value": 21},
                    "uop_idx": 3,
                    "is_vec": 0,
                }
                if idx == 12
                else None,
            }
            for idx in range(VLQ_SIZE)
        ],
    }
    raw["direct"]["lqr"] = {
        "available": True,
        "entries": [
            {
                "index": idx,
                "allocated": idx == 18,
                "scheduled": idx == 18,
                "blocking": False,
                "cause": "DM",
                "detail": {
                    "source": "direct",
                    "sched_index": idx,
                    "cause": "DM",
                    "lq_idx": {"flag": 0, "value": 12},
                    "sq_idx": {"flag": 0, "value": 7},
                    "rob_idx": {"flag": 1, "value": 21},
                    "uop_idx": 3,
                    "pdest": 19,
                    "vaddr": 0x812340,
                    "is_vec": 0,
                    "is_128bit": 0,
                }
                if idx == 18
                else None,
            }
            for idx in range(LQR_SIZE)
        ],
    }
    raw["direct"]["sq"] = {
        "available": True,
        "entries": [
            {
                "index": idx,
                "allocated": idx == 22,
                "addrvalid": idx == 22,
                "datavalid": idx == 22,
                "committed": False,
                "completed": False,
                "nc": True,
                "is_vec": False,
                "detail": {
                    "source": "direct",
                    "sq_idx": {"flag": 0, "value": idx},
                    "rob_idx": {"flag": 1, "value": 21},
                    "uop_idx": 4,
                    "pdest": 8,
                    "nc": True,
                    "is_vec": False,
                }
                if idx == 22
                else None,
            }
            for idx in range(SQ_SIZE)
        ],
    }

    tracker.update(raw)

    assert tracker.vlq_entries[5]["allocated"] is False
    assert tracker.vlq_entries[12]["allocated"] is True
    assert tracker.vlq_entries[12]["request"]["rob_idx"] == {"flag": 1, "value": 21}
    assert tracker.lqr_entries[9]["allocated"] is False
    assert tracker.lqr_entries[18]["allocated"] is True
    assert tracker.lqr_entries[18]["cause"] == "DM"
    assert tracker.sq_entries[7]["allocated"] is False
    assert tracker.sq_entries[22]["allocated"] is True
    assert tracker.sq_entries[22]["nc"] is True

    vlq_payload = tracker.topic_payload("vlq", {"running": False, "step_cycles": 1, "tick_ms": 100, "last_raw": raw})
    sq_payload = tracker.topic_payload("sq", {"running": False, "step_cycles": 1, "tick_ms": 100, "last_raw": raw})
    replay_payload = tracker.topic_payload("replay", {"running": False, "step_cycles": 1, "tick_ms": 100, "last_raw": raw})

    assert vlq_payload["data_quality"] == "direct"
    assert sq_payload["data_quality"] == "direct"
    assert replay_payload["entries_quality"] == "direct"


def test_lsq_webui_topic_publisher_only_publishes_on_change():
    tracker = LsqStateTracker()
    publisher = TopicPublisher()
    raw = _make_raw(cycle=1)
    tracker.update(raw)

    state = {"running": False, "step_cycles": 1, "tick_ms": 100, "last_raw": raw}
    payload = tracker.topic_payload("overview", state)
    assert publisher.should_publish("overview", payload) is True
    assert publisher.should_publish("overview", payload) is False

    changed = deepcopy(raw)
    changed["cycle"] = 2
    changed["overview"]["lq_full"] = 1
    tracker.update(changed)
    payload = tracker.topic_payload("overview", {"running": False, "step_cycles": 1, "tick_ms": 100, "last_raw": changed})
    assert publisher.should_publish("overview", payload) is True
