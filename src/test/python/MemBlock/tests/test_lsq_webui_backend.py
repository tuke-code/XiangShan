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
        "perf_values": [0] * 36,
    }


def test_lsq_webui_tracker_builds_shadow_vlq_and_sq_entries():
    tracker = LsqStateTracker()

    raw = _make_raw(cycle=1)
    raw["enq"]["need_alloc"][0] = 0x3
    raw["enq"]["req"][0]["valid"] = 1
    raw["enq"]["resp"][0]["lq_idx"] = {"flag": 0, "value": 5}
    raw["enq"]["resp"][0]["sq_idx"] = {"flag": 0, "value": 7}
    raw["store_addr_re"][0]["update_addr_valid"] = 1
    raw["store_addr_re"][0]["sq_idx"] = {"flag": 0, "value": 7}
    raw["store_data"][0]["valid"] = 1
    raw["store_data"][0]["sq_idx"] = {"flag": 0, "value": 7}

    tracker.update(raw)

    assert tracker.vlq_entries[5]["allocated"] is True
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
    raw["ldu_lanes"][0]["valid"] = 1
    raw["ldu_lanes"][0]["is_load_replay"] = 1
    raw["ldu_lanes"][0]["cause_bits"]["RAW"] = 1
    raw["ldu_lanes"][0]["update_addr_valid"] = 0
    raw["overview"]["rar_valid_count"] = 3
    raw["release"]["valid"] = 1
    raw["release"]["paddr"] = 0x1234

    tracker.update(raw)

    assert tracker.lqr_entries[9]["allocated"] is True
    assert tracker.replay_cause_counts["TM"] >= 1
    assert tracker.replay_cause_counts["RAW"] == 1
    assert tracker.raw_stats["waiting"] == 1
    assert tracker.rar_stats["allocated"] == 3
    assert tracker.rar_stats["released"] == 1
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
