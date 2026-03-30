# coding=utf-8
"""
MemBlock LSQ Web UI backend helpers.

This module provides:
  1. Low-level DUT signal sampling for `MemBlock_inner_lsq`
  2. Shadow state tracking for UI-oriented queue views
  3. Topic payload generation for independent WebSocket endpoints
"""

from __future__ import annotations

import copy
import json
from collections import deque
from dataclasses import dataclass
from typing import Any


CAUSE_LABELS = ["MA", "TM", "FF", "DR", "DM", "WF", "BC", "RAR", "RAW", "NK", "MF"]
VLQ_SIZE = 72
LQR_SIZE = 72
SQ_SIZE = 56
LQRAR_SIZE = 72
LQRAW_SIZE = 32
LSQ_ENQ_PORTS = 8
REPLAY_PORTS = 3
STORE_ADDR_PORTS = 2
STORE_DATA_PORTS = 2
LOAD_PIPELINE_WIDTH = 3
PERF_SAMPLE_PERIOD = 8
HISTORY_LIMIT = 48
EVENT_BACKLOG_LIMIT = 200


def _json_key(payload: dict[str, Any]) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def _ptr_distance(prev: tuple[int, int], curr: tuple[int, int], size: int) -> int:
    prev_abs = prev[0] * size + prev[1]
    curr_abs = curr[0] * size + curr[1]
    if curr_abs < prev_abs:
        curr_abs += size * 2
    return curr_abs - prev_abs


def _ptr_iter(prev: tuple[int, int], curr: tuple[int, int], size: int) -> list[int]:
    distance = _ptr_distance(prev, curr, size)
    ptr_flag, ptr_value = prev
    indices = []
    for _ in range(distance):
        indices.append(ptr_value)
        ptr_value += 1
        if ptr_value >= size:
            ptr_value = 0
            ptr_flag ^= 0x1
    return indices


class SignalReader:
    """Read LSQ-related internal signals from DUT."""

    def __init__(self, dut) -> None:
        self.dut = dut

    def _read(self, name: str, default: int = 0) -> int:
        signal = getattr(self.dut, name, None)
        if signal is None:
            return default
        try:
            return int(signal.value)
        except Exception:
            return default

    def _read_ptr(self, prefix: str) -> dict[str, int]:
        return {
            "flag": self._read(f"{prefix}_flag"),
            "value": self._read(f"{prefix}_value"),
        }

    def _read_uop_ptr(self, prefix: str, name: str) -> dict[str, int]:
        return {
            "flag": self._read(f"{prefix}_{name}_flag"),
            "value": self._read(f"{prefix}_{name}_value"),
        }

    def read(self, cycle: int) -> dict[str, Any]:
        enq_req = []
        enq_resp = []
        need_alloc = []
        for idx in range(LSQ_ENQ_PORTS):
            req_prefix = f"MemBlock_inner_lsq_io_enq_req_{idx}"
            resp_prefix = f"MemBlock_inner_lsq_io_enq_resp_{idx}"
            need_alloc.append(self._read(f"MemBlock_inner_lsq_io_enq_needAlloc_{idx}"))
            enq_req.append(
                {
                    "valid": self._read(f"{req_prefix}_valid"),
                    "fu_type": self._read(f"{req_prefix}_bits_fuType"),
                    "fu_op_type": self._read(f"{req_prefix}_bits_fuOpType"),
                    "rob_idx": self._read_ptr(f"{req_prefix}_bits_robIdx"),
                    "lq_idx": self._read_ptr(f"{req_prefix}_bits_lqIdx"),
                    "sq_idx": self._read_ptr(f"{req_prefix}_bits_sqIdx"),
                    "num_ls_elem": self._read(f"{req_prefix}_bits_numLsElem"),
                }
            )
            enq_resp.append(
                {
                    "lq_idx": self._read_ptr(f"{resp_prefix}_lqIdx"),
                    "sq_idx": self._read_ptr(f"{resp_prefix}_sqIdx"),
                }
            )

        replay_lanes = []
        for idx in range(REPLAY_PORTS):
            prefix = f"MemBlock_inner_lsq_io_replay_{idx}"
            replay_lanes.append(
                {
                    "lane": idx,
                    "valid": self._read(f"{prefix}_valid"),
                    "ready": self._read(f"{prefix}_ready"),
                    "sched_index": self._read(f"{prefix}_bits_schedIndex", -1),
                    "tlb_miss": self._read(f"{prefix}_bits_tlbMiss"),
                    "lq_idx": self._read_uop_ptr(f"{prefix}_bits_uop", "lqIdx"),
                    "sq_idx": self._read_uop_ptr(f"{prefix}_bits_uop", "sqIdx"),
                    "rob_idx": self._read_uop_ptr(f"{prefix}_bits_uop", "robIdx"),
                    "uop_idx": self._read(f"{prefix}_bits_uop_uopIdx"),
                    "pdest": self._read(f"{prefix}_bits_uop_pdest"),
                    "vaddr": self._read(f"{prefix}_bits_vaddr"),
                    "is_vec": self._read(f"{prefix}_bits_isvec"),
                    "is_128bit": self._read(f"{prefix}_bits_is128bit"),
                    "mask": self._read(f"{prefix}_bits_mask"),
                }
            )

        store_data = []
        for idx in range(STORE_DATA_PORTS):
            prefix = f"MemBlock_inner_lsq_io_std_storeDataIn_{idx}"
            store_data.append(
                {
                    "lane": idx,
                    "valid": self._read(f"{prefix}_valid"),
                    "sq_idx": self._read_ptr(f"{prefix}_bits_sqIdx"),
                    "fu_type": self._read(f"{prefix}_bits_fuType"),
                    "fu_op_type": self._read(f"{prefix}_bits_fuOpType"),
                    "data": self._read(f"{prefix}_bits_data"),
                }
            )

        store_addr = []
        for idx in range(STORE_ADDR_PORTS):
            prefix = f"MemBlock_inner_lsq_io_sta_storeAddrIn_{idx}"
            store_addr.append(
                {
                    "lane": idx,
                    "sq_idx": self._read_uop_ptr(f"{prefix}_bits_uop", "sqIdx"),
                    "paddr": self._read(f"{prefix}_bits_paddr"),
                    "nc": self._read(f"{prefix}_bits_nc"),
                    "mmio": self._read(f"{prefix}_bits_miss"),
                }
            )

        store_addr_re = []
        for idx in range(STORE_ADDR_PORTS):
            prefix = f"MemBlock_inner_lsq_io_sta_storeAddrInRe_{idx}"
            store_addr_re.append(
                {
                    "lane": idx,
                    "update_addr_valid": self._read(f"{prefix}_updateAddrValid"),
                    "sq_idx": self._read_uop_ptr(f"{prefix}_uop", "sqIdx"),
                }
            )

        ldu_lanes = []
        for idx in range(LOAD_PIPELINE_WIDTH):
            prefix = f"MemBlock_inner_lsq_io_ldu_ldin_{idx}"
            cause_bits = {}
            for cause_idx, label in enumerate(CAUSE_LABELS):
                cause_bits[label] = self._read(f"{prefix}_bits_rep_info_cause_{cause_idx}")
            ldu_lanes.append(
                {
                    "lane": idx,
                    "valid": self._read(f"{prefix}_valid"),
                    "is_load_replay": self._read(f"{prefix}_bits_isLoadReplay"),
                    "sched_index": self._read(f"{prefix}_bits_schedIndex", -1),
                    "update_addr_valid": self._read(f"{prefix}_bits_updateAddrValid"),
                    "lq_idx": self._read_uop_ptr(f"{prefix}_bits_uop", "lqIdx"),
                    "sq_idx": self._read_uop_ptr(f"{prefix}_bits_uop", "sqIdx"),
                    "rob_idx": self._read_uop_ptr(f"{prefix}_bits_uop", "robIdx"),
                    "uop_idx": self._read(f"{prefix}_bits_uop_uopIdx"),
                    "paddr": self._read(f"{prefix}_bits_paddr"),
                    "vaddr": self._read(f"{prefix}_bits_fullva"),
                    "nc_with_data": self._read(f"{prefix}_bits_nc_with_data"),
                    "mmio": self._read(f"{prefix}_bits_mmio"),
                    "handled_by_mshr": self._read(f"{prefix}_bits_handledByMSHR"),
                    "addr_inv_sq_idx": self._read_ptr(f"{prefix}_bits_rep_info_addr_inv_sq_idx"),
                    "cause_bits": cause_bits,
                }
            )

        nc_out_lanes = []
        for idx in range(LOAD_PIPELINE_WIDTH):
            prefix = f"MemBlock_inner_lsq_io_ncOut_{idx}"
            nc_out_lanes.append(
                {
                    "lane": idx,
                    "valid": self._read(f"{prefix}_valid"),
                    "ready": self._read(f"{prefix}_ready"),
                    "sched_index": self._read(f"{prefix}_bits_schedIndex", -1),
                    "lq_idx": self._read_uop_ptr(f"{prefix}_bits_uop", "lqIdx"),
                    "sq_idx": self._read_uop_ptr(f"{prefix}_bits_uop", "sqIdx"),
                    "rob_idx": self._read_uop_ptr(f"{prefix}_bits_uop", "robIdx"),
                    "uop_idx": self._read(f"{prefix}_bits_uop_uopIdx"),
                    "vaddr": self._read(f"{prefix}_bits_vaddr"),
                    "paddr": self._read(f"{prefix}_bits_paddr"),
                    "is_vec": self._read(f"{prefix}_bits_isvec"),
                    "is_128bit": self._read(f"{prefix}_bits_is128bit"),
                }
            )

        perf_values = [self._read(f"MemBlock_inner_lsq_io_perf_{idx}_value") for idx in range(36)]

        return {
            "cycle": cycle,
            "overview": {
                "lq_can_accept": self._read("MemBlock_inner_lsq_io_lqCanAccept"),
                "sq_can_accept": self._read("MemBlock_inner_lsq_io_sqCanAccept"),
                "lq_empty": self._read("MemBlock_inner_lsq_io_lqEmpty"),
                "lq_full": self._read("MemBlock_inner_lsq_io_lqFull"),
                "sq_empty": self._read("MemBlock_inner_lsq_io_sqEmpty"),
                "sq_full": self._read("MemBlock_inner_lsq_io_sqFull"),
                "rar_valid_count": self._read("MemBlock_inner_lsq_io_rarValidCount"),
                "no_uops_issued": self._read("MemBlock_inner_lsq_io_noUopsIssued"),
                "load_misalign_full": self._read("MemBlock_inner_lsq_io_loadMisalignFull"),
                "lq_cancel_cnt": self._read("MemBlock_inner_lsq_io_lqCancelCnt"),
                "sq_cancel_cnt": self._read("MemBlock_inner_lsq_io_sqCancelCnt"),
            },
            "enq": {
                "can_accept": self._read("MemBlock_inner_lsq_io_enq_canAccept"),
                "need_alloc": need_alloc,
                "req": enq_req,
                "resp": enq_resp,
            },
            "lq": {
                "deq": self._read("MemBlock_inner_lsq_io_lqDeq"),
                "deq_ptr": self._read_ptr("MemBlock_inner_lsq_io_lqDeqPtr"),
            },
            "sq": {
                "deq": self._read("MemBlock_inner_lsq_io_sqDeq"),
                "deq_ptr": self._read_ptr("MemBlock_inner_lsq_io_sqDeqPtr"),
                "commit_ptr": self._read_ptr("MemBlock_inner_lsq_io_sqCommitPtr"),
                "commit_rob_idx": self._read_ptr("MemBlock_inner_lsq_io_sqCommitRobIdx"),
                "commit_uop_idx": self._read("MemBlock_inner_lsq_io_sqCommitUopIdx"),
            },
            "release": {
                "valid": self._read("MemBlock_inner_lsq_io_release_valid"),
                "paddr": self._read("MemBlock_inner_lsq_io_release_bits_paddr"),
            },
            "debug_topdown": {
                "rob_head_tlb_replay": self._read("MemBlock_inner_lsq_io_debugTopDown_robHeadTlbReplay"),
                "rob_head_tlb_miss": self._read("MemBlock_inner_lsq_io_debugTopDown_robHeadTlbMiss"),
                "rob_head_load_vio": self._read("MemBlock_inner_lsq_io_debugTopDown_robHeadLoadVio"),
                "rob_head_load_mshr": self._read("MemBlock_inner_lsq_io_debugTopDown_robHeadLoadMSHR"),
            },
            "tlb_hint": {
                "replay_all": self._read("MemBlock_inner_lsq_io_tlb_hint_resp_bits_replay_all"),
            },
            "replay_lanes": replay_lanes,
            "store_data": store_data,
            "store_addr": store_addr,
            "store_addr_re": store_addr_re,
            "ldu_lanes": ldu_lanes,
            "nc_out_lanes": nc_out_lanes,
            "perf_values": perf_values,
        }


@dataclass
class TopicState:
    last_key: str | None = None
    min_interval: int = 1
    last_cycle: int = -1


class LsqStateTracker:
    """Track UI-oriented shadow state derived from LSQ signals."""

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self.cycle = 0
        self.overview = {}
        self.vlq_entries = [
            {"index": idx, "allocated": False, "committed": False, "age": 0, "request": None, "retire_at": None}
            for idx in range(VLQ_SIZE)
        ]
        self.sq_entries = [
            {
                "index": idx,
                "allocated": False,
                "addrvalid": False,
                "datavalid": False,
                "committed": False,
                "mmio": False,
                "nc": False,
                "age": 0,
                "retire_at": None,
            }
            for idx in range(SQ_SIZE)
        ]
        self.lqr_entries = [
            {
                "index": idx,
                "allocated": False,
                "scheduled": False,
                "blocking": False,
                "cause": None,
                "lane": None,
                "source": None,
                "detail": None,
                "retire_at": None,
            }
            for idx in range(LQR_SIZE)
        ]
        self.rar_entries = [{"index": idx, "allocated": False, "released": False} for idx in range(LQRAR_SIZE)]
        self.raw_entries = [{"index": idx, "allocated": False, "waiting": False} for idx in range(LQRAW_SIZE)]
        self.replay_lanes = []
        self.ldu_lanes = []
        self.nc_out_lanes = []
        self.perf_values = [0] * 36
        self.replay_cause_counts = {label: 0 for label in [*CAUSE_LABELS, "NC"]}
        self.vlq_stats = {"enq": 0, "deq": 0, "total_latency": 0}
        self.sq_stats = {"fwd_req": 0, "fwd_succ": 0, "mmio": 0, "nc": 0}
        self.rar_stats = {"allocated": 0, "released": 0, "violations": 0}
        self.raw_stats = {"allocated": 0, "waiting": 0, "violations": 0}
        self.topdown = {}
        self.events = deque(maxlen=EVENT_BACKLOG_LIMIT)
        self.pending_events = []
        self.history = {
            "cycles": [],
            "load_throughput": [],
            "store_occupancy": [],
            "rar_violations": [],
            "raw_violations": [],
            "store_load_replay": [],
            "tlb_miss": [],
            "dcache_miss": [],
        }
        self._perf_sample_cycle = 0
        self._prev = None
        self._prev_lq_deq_ptr = (0, 0)
        self._prev_sq_commit_ptr = (0, 0)
        self._prev_sq_deq_ptr = (0, 0)
        self._release_cursor = 0
        self._raw_cursor = 0

    def _add_event(self, event_type: str, message: str, payload: dict[str, Any] | None = None) -> None:
        event = {
            "cycle": self.cycle,
            "type": event_type,
            "message": message,
        }
        if payload:
            event["payload"] = payload
        self.events.appendleft(event)
        self.pending_events.append(event)

    def _first_cause(self, lane: dict[str, Any]) -> str | None:
        for label in CAUSE_LABELS:
            if lane["cause_bits"].get(label):
                return label
        return None

    def _retire_entries(self) -> None:
        for entry in self.vlq_entries:
            if entry["retire_at"] is not None and entry["retire_at"] <= self.cycle:
                entry.update({"allocated": False, "committed": False, "age": 0, "request": None, "retire_at": None})
        for entry in self.sq_entries:
            if entry["retire_at"] is not None and entry["retire_at"] <= self.cycle:
                entry.update(
                    {
                        "allocated": False,
                        "addrvalid": False,
                        "datavalid": False,
                        "committed": False,
                        "mmio": False,
                        "nc": False,
                        "age": 0,
                        "retire_at": None,
                    }
                )
        for entry in self.lqr_entries:
            if entry["retire_at"] is not None and entry["retire_at"] <= self.cycle:
                entry.update(
                    {
                        "allocated": False,
                        "scheduled": False,
                        "blocking": False,
                        "cause": None,
                        "lane": None,
                        "source": None,
                        "detail": None,
                        "retire_at": None,
                    }
                )

    def _activate_lqr_entry(self, active_entries: set[int], entry_idx: int) -> dict[str, Any]:
        entry = self.lqr_entries[entry_idx]
        if entry_idx not in active_entries:
            entry.update(
                {
                    "allocated": True,
                    "scheduled": False,
                    "blocking": False,
                    "cause": None,
                    "lane": None,
                    "source": None,
                    "detail": None,
                    "retire_at": None,
                }
            )
            active_entries.add(entry_idx)
        return entry

    def _merge_entry_detail(self, entry: dict[str, Any], detail: dict[str, Any]) -> None:
        if entry.get("detail") is None:
            entry["detail"] = {}
        for key, value in detail.items():
            if value is not None:
                entry["detail"][key] = copy.deepcopy(value)

    def update(self, raw: dict[str, Any]) -> None:
        self.cycle = raw["cycle"]
        self.pending_events = []
        self.overview = copy.deepcopy(raw["overview"])
        self.topdown = copy.deepcopy(raw["debug_topdown"])
        self.perf_values = list(raw["perf_values"])
        self.replay_lanes = []
        self.ldu_lanes = []
        self.nc_out_lanes = []
        self._retire_entries()

        enq_can_accept = bool(raw["enq"]["can_accept"])
        for idx, req in enumerate(raw["enq"]["req"]):
            if not req["valid"] or not enq_can_accept:
                continue
            need_alloc = raw["enq"]["need_alloc"][idx]
            resp = raw["enq"]["resp"][idx]
            if need_alloc & 0x1:
                lq_idx = resp["lq_idx"]["value"]
                if 0 <= lq_idx < VLQ_SIZE:
                    entry = self.vlq_entries[lq_idx]
                    if not entry["allocated"]:
                        entry.update(
                            {
                                "allocated": True,
                                "committed": False,
                                "age": self.cycle,
                                "request": {
                                    "source": "enq",
                                    "lane": idx,
                                    "fu_type": req["fu_type"],
                                    "fu_op_type": req["fu_op_type"],
                                    "rob_idx": copy.deepcopy(req["rob_idx"]),
                                    "lq_idx": copy.deepcopy(resp["lq_idx"]),
                                    "sq_idx": copy.deepcopy(resp["sq_idx"]),
                                    "req_lq_idx": copy.deepcopy(req["lq_idx"]),
                                    "req_sq_idx": copy.deepcopy(req["sq_idx"]),
                                    "num_ls_elem": req["num_ls_elem"],
                                },
                                "retire_at": None,
                            }
                        )
                        self.vlq_stats["enq"] += 1
                        self._add_event("enqueue", f"VLQ shadow allocate entry={lq_idx}", {"entry": lq_idx})
            if need_alloc & 0x2:
                sq_idx = resp["sq_idx"]["value"]
                if 0 <= sq_idx < SQ_SIZE:
                    entry = self.sq_entries[sq_idx]
                    if not entry["allocated"]:
                        entry.update(
                            {
                                "allocated": True,
                                "addrvalid": False,
                                "datavalid": False,
                                "committed": False,
                                "age": self.cycle,
                                "retire_at": None,
                            }
                        )
                        self._add_event("enqueue", f"SQ shadow allocate entry={sq_idx}", {"entry": sq_idx})

        curr_lq_deq_ptr = (raw["lq"]["deq_ptr"]["flag"], raw["lq"]["deq_ptr"]["value"])
        if self._prev is not None and curr_lq_deq_ptr != self._prev_lq_deq_ptr:
            for idx in _ptr_iter(self._prev_lq_deq_ptr, curr_lq_deq_ptr, VLQ_SIZE):
                entry = self.vlq_entries[idx]
                if entry["allocated"]:
                    entry["committed"] = True
                    entry["retire_at"] = self.cycle + 1
                    self.vlq_stats["total_latency"] += max(0, self.cycle - entry["age"])
                self.vlq_stats["deq"] += 1
                self._add_event("enqueue", f"VLQ shadow retire entry={idx}", {"entry": idx})
        self._prev_lq_deq_ptr = curr_lq_deq_ptr

        curr_sq_commit_ptr = (raw["sq"]["commit_ptr"]["flag"], raw["sq"]["commit_ptr"]["value"])
        if self._prev is not None and curr_sq_commit_ptr != self._prev_sq_commit_ptr:
            for idx in _ptr_iter(self._prev_sq_commit_ptr, curr_sq_commit_ptr, SQ_SIZE):
                entry = self.sq_entries[idx]
                if entry["allocated"] and not entry["committed"]:
                    entry["committed"] = True
                    self._add_event("enqueue", f"SQ shadow commit entry={idx}", {"entry": idx})
        self._prev_sq_commit_ptr = curr_sq_commit_ptr

        curr_sq_deq_ptr = (raw["sq"]["deq_ptr"]["flag"], raw["sq"]["deq_ptr"]["value"])
        if self._prev is not None and curr_sq_deq_ptr != self._prev_sq_deq_ptr:
            for idx in _ptr_iter(self._prev_sq_deq_ptr, curr_sq_deq_ptr, SQ_SIZE):
                entry = self.sq_entries[idx]
                if entry["allocated"]:
                    entry["retire_at"] = self.cycle + 1
                self._add_event("enqueue", f"SQ shadow dequeue entry={idx}", {"entry": idx})
        self._prev_sq_deq_ptr = curr_sq_deq_ptr

        for lane in raw["store_addr_re"]:
            if not lane["update_addr_valid"]:
                continue
            sq_idx = lane["sq_idx"]["value"]
            if 0 <= sq_idx < SQ_SIZE:
                entry = self.sq_entries[sq_idx]
                entry["allocated"] = True
                entry["addrvalid"] = True

        for lane in raw["store_addr"]:
            sq_idx = lane["sq_idx"]["value"]
            if 0 <= sq_idx < SQ_SIZE and self.sq_entries[sq_idx]["allocated"]:
                self.sq_entries[sq_idx]["nc"] = bool(lane["nc"])
                self.sq_entries[sq_idx]["mmio"] = bool(lane["mmio"])

        for lane in raw["store_data"]:
            if not lane["valid"]:
                continue
            sq_idx = lane["sq_idx"]["value"]
            if 0 <= sq_idx < SQ_SIZE:
                entry = self.sq_entries[sq_idx]
                entry["allocated"] = True
                entry["datavalid"] = True
                self.sq_stats["fwd_req"] += 1
                if entry["addrvalid"]:
                    self.sq_stats["fwd_succ"] += 1
                if entry["mmio"]:
                    self.sq_stats["mmio"] += 1
                if entry["nc"]:
                    self.sq_stats["nc"] += 1

        active_lqr_entries = set()
        for lane in raw["replay_lanes"]:
            replay_lane = copy.deepcopy(lane)
            replay_lane["source"] = "replay"
            replay_lane["fire"] = bool(lane["valid"] and lane["ready"])
            cause = "TM" if lane["tlb_miss"] else None
            if cause is not None:
                self.replay_cause_counts[cause] += 1
            replay_lane["cause"] = cause
            self.replay_lanes.append(replay_lane)
            if lane["sched_index"] < 0:
                continue
            entry_idx = lane["sched_index"] % LQR_SIZE
            entry = self._activate_lqr_entry(active_lqr_entries, entry_idx)
            entry["scheduled"] = entry["scheduled"] or bool(lane["valid"] and lane["ready"])
            entry["blocking"] = entry["blocking"] or bool(lane["valid"] and not lane["ready"])
            entry["cause"] = cause or entry["cause"]
            entry["lane"] = lane["lane"]
            entry["source"] = "replay"
            self._merge_entry_detail(
                entry,
                {
                    "source": "replay",
                    "sched_index": lane["sched_index"],
                    "cause": cause,
                    "lane": lane["lane"],
                    "valid": lane["valid"],
                    "ready": lane["ready"],
                    "lq_idx": lane["lq_idx"],
                    "sq_idx": lane["sq_idx"],
                    "rob_idx": lane["rob_idx"],
                    "uop_idx": lane["uop_idx"],
                    "pdest": lane["pdest"],
                    "vaddr": lane["vaddr"],
                    "is_vec": lane["is_vec"],
                    "is_128bit": lane["is_128bit"],
                    "mask": lane["mask"],
                },
            )
            entry["retire_at"] = self.cycle + 2 if lane["valid"] and lane["ready"] else None
            if lane["valid"] and lane["ready"]:
                self._add_event(
                    "replay",
                    f"Replay lane={lane['lane']} fire schedIndex={lane['sched_index']}",
                    {"lane": lane["lane"], "sched_index": lane["sched_index"]},
                )

        for idx, entry in enumerate(self.lqr_entries):
            if idx not in active_lqr_entries and entry["allocated"] and entry["retire_at"] is None:
                entry["retire_at"] = self.cycle + 1

        raw_waiting = 0
        for lane in raw["ldu_lanes"]:
            cause = self._first_cause(lane)
            lane_view = copy.deepcopy(lane)
            lane_view["source"] = "ldu"
            lane_view["fire"] = bool(lane["valid"] and lane["is_load_replay"])
            lane_view["cause"] = cause
            self.ldu_lanes.append(lane_view)
            if lane["valid"] and lane["is_load_replay"] and cause is not None:
                self.replay_cause_counts[cause] += 1
                self._add_event(
                    "replay",
                    f"LDU lane={lane['lane']} replay cause=C_{cause}",
                    {"lane": lane["lane"], "cause": cause},
                )
                if cause == "RAW":
                    self.raw_stats["violations"] += 1
            if lane["valid"] and lane["is_load_replay"] and lane["sched_index"] >= 0:
                replay_lane = copy.deepcopy(lane_view)
                replay_lane["ready"] = None
                self.replay_lanes.append(replay_lane)
                entry_idx = lane["sched_index"] % LQR_SIZE
                entry = self._activate_lqr_entry(active_lqr_entries, entry_idx)
                entry["blocking"] = True
                entry["cause"] = cause or entry["cause"]
                entry["lane"] = lane["lane"]
                entry["source"] = "ldu"
                self._merge_entry_detail(
                    entry,
                    {
                        "source": "ldu",
                        "sched_index": lane["sched_index"],
                        "cause": cause,
                        "lane": lane["lane"],
                        "valid": lane["valid"],
                        "lq_idx": lane["lq_idx"],
                        "sq_idx": lane["sq_idx"],
                        "rob_idx": lane["rob_idx"],
                        "uop_idx": lane["uop_idx"],
                        "vaddr": lane["vaddr"],
                        "paddr": lane["paddr"],
                        "nc_with_data": lane["nc_with_data"],
                        "mmio": lane["mmio"],
                        "handled_by_mshr": lane["handled_by_mshr"],
                        "addr_inv_sq_idx": lane["addr_inv_sq_idx"],
                    },
                )
            if lane["valid"] and not lane["update_addr_valid"]:
                raw_waiting += 1

        for lane in raw.get("nc_out_lanes", []):
            lane_view = copy.deepcopy(lane)
            lane_view["source"] = "nc"
            lane_view["fire"] = bool(lane["valid"] and lane["ready"])
            lane_view["cause"] = "NC"
            self.nc_out_lanes.append(lane_view)
            if lane["valid"] or lane["sched_index"] >= 0:
                self.replay_lanes.append(copy.deepcopy(lane_view))
            if lane["valid"]:
                self.replay_cause_counts["NC"] += 1
            if lane["sched_index"] < 0:
                continue
            entry_idx = lane["sched_index"] % LQR_SIZE
            entry = self._activate_lqr_entry(active_lqr_entries, entry_idx)
            entry["scheduled"] = entry["scheduled"] or bool(lane["valid"] and lane["ready"])
            entry["blocking"] = entry["blocking"] or bool(lane["valid"] and not lane["ready"])
            entry["cause"] = entry["cause"] or "NC"
            entry["lane"] = lane["lane"]
            entry["source"] = "nc"
            self._merge_entry_detail(
                entry,
                {
                    "source": "nc",
                    "sched_index": lane["sched_index"],
                    "cause": "NC",
                    "lane": lane["lane"],
                    "valid": lane["valid"],
                    "ready": lane["ready"],
                    "lq_idx": lane["lq_idx"],
                    "sq_idx": lane["sq_idx"],
                    "rob_idx": lane["rob_idx"],
                    "uop_idx": lane["uop_idx"],
                    "vaddr": lane["vaddr"],
                    "paddr": lane["paddr"],
                    "is_vec": lane["is_vec"],
                    "is_128bit": lane["is_128bit"],
                },
            )
            entry["retire_at"] = self.cycle + 2 if lane["valid"] and lane["ready"] else entry["retire_at"]
            if lane["valid"] and lane["ready"]:
                self._add_event(
                    "replay",
                    f"NC lane={lane['lane']} fire schedIndex={lane['sched_index']}",
                    {"lane": lane["lane"], "sched_index": lane["sched_index"]},
                )

        rar_count = max(0, min(LQRAR_SIZE, int(raw["overview"]["rar_valid_count"])))
        for idx, entry in enumerate(self.rar_entries):
            entry["allocated"] = idx < rar_count
            if idx >= rar_count:
                entry["released"] = False
        self.rar_stats["allocated"] = rar_count

        if raw["release"]["valid"]:
            release_idx = self._release_cursor % max(1, rar_count or 1)
            release_idx = min(release_idx, LQRAR_SIZE - 1)
            self.rar_entries[release_idx]["allocated"] = True
            self.rar_entries[release_idx]["released"] = True
            self._release_cursor += 1
            self.rar_stats["released"] += 1
            self.rar_stats["violations"] += 1
            self._add_event("violation", f"RAR release observed paddr=0x{raw['release']['paddr']:x}")

        self.raw_stats["waiting"] = raw_waiting
        self.raw_stats["allocated"] = min(LQRAW_SIZE, raw_waiting)
        for idx, entry in enumerate(self.raw_entries):
            entry["allocated"] = idx < self.raw_stats["allocated"]
            entry["waiting"] = idx < self.raw_stats["waiting"]

        if raw["debug_topdown"]["rob_head_load_vio"]:
            self.raw_stats["violations"] += 1
        if raw["debug_topdown"]["rob_head_tlb_miss"]:
            self.replay_cause_counts["TM"] += 1
        if raw["debug_topdown"]["rob_head_load_mshr"]:
            self.replay_cause_counts["DM"] += 1

        if self.cycle - self._perf_sample_cycle >= PERF_SAMPLE_PERIOD:
            self.history["cycles"].append(self.cycle)
            self.history["load_throughput"].append(self.vlq_stats["enq"])
            self.history["store_occupancy"].append(sum(1 for entry in self.sq_entries if entry["allocated"]))
            self.history["rar_violations"].append(self.rar_stats["violations"])
            self.history["raw_violations"].append(self.raw_stats["violations"])
            self.history["store_load_replay"].append(self.replay_cause_counts["MA"])
            self.history["tlb_miss"].append(self.replay_cause_counts["TM"])
            self.history["dcache_miss"].append(self.replay_cause_counts["DM"])
            for key in self.history:
                self.history[key] = self.history[key][-HISTORY_LIMIT:]
            self._perf_sample_cycle = self.cycle

        self._prev = copy.deepcopy(raw)

    def load_reset_snapshot(self, raw: dict[str, Any]) -> None:
        """Seed non-shadow state after DUT reset without reconstructing queue activity."""
        self.reset()
        self.cycle = raw["cycle"]
        self.overview = copy.deepcopy(raw["overview"])
        self.topdown = copy.deepcopy(raw["debug_topdown"])
        self.perf_values = list(raw["perf_values"])
        self._prev = copy.deepcopy(raw)
        self._prev_lq_deq_ptr = (raw["lq"]["deq_ptr"]["flag"], raw["lq"]["deq_ptr"]["value"])
        self._prev_sq_commit_ptr = (raw["sq"]["commit_ptr"]["flag"], raw["sq"]["commit_ptr"]["value"])
        self._prev_sq_deq_ptr = (raw["sq"]["deq_ptr"]["flag"], raw["sq"]["deq_ptr"]["value"])

    def events_backlog(self) -> list[dict[str, Any]]:
        return list(self.events)

    def consume_pending_events(self) -> list[dict[str, Any]]:
        events = list(self.pending_events)
        self.pending_events = []
        return events

    def topic_payload(self, topic: str, service_state: dict[str, Any]) -> dict[str, Any]:
        if topic == "overview":
            return {
                "cycle": self.cycle,
                "running": service_state["running"],
                "step_cycles": service_state["step_cycles"],
                "tick_ms": service_state["tick_ms"],
                "overview": self.overview,
                "degraded_features": ["vlq-shadow", "sq-shadow", "violations-shadow"],
            }
        if topic == "vlq":
            allocated = sum(1 for entry in self.vlq_entries if entry["allocated"])
            avg_latency = 0
            if self.vlq_stats["enq"] > 0:
                avg_latency = round(self.vlq_stats["total_latency"] / self.vlq_stats["enq"])
            return {
                "cycle": self.cycle,
                "is_shadow": True,
                "size": VLQ_SIZE,
                "allocated": allocated,
                "entries": copy.deepcopy(self.vlq_entries),
                "stats": {
                    "enq": self.vlq_stats["enq"],
                    "deq": self.vlq_stats["deq"],
                    "avg_latency": avg_latency,
                },
            }
        if topic == "sq":
            allocated = sum(1 for entry in self.sq_entries if entry["allocated"])
            return {
                "cycle": self.cycle,
                "is_shadow": True,
                "size": SQ_SIZE,
                "allocated": allocated,
                "entries": copy.deepcopy(self.sq_entries),
                "stats": copy.deepcopy(self.sq_stats),
            }
        if topic == "replay":
            allocated = sum(1 for entry in self.lqr_entries if entry["allocated"])
            return {
                "cycle": self.cycle,
                "size": LQR_SIZE,
                "allocated": allocated,
                "entries": copy.deepcopy(self.lqr_entries),
                "lanes": copy.deepcopy(self.replay_lanes),
                "ldu_lanes": copy.deepcopy(self.ldu_lanes),
                "nc_out_lanes": copy.deepcopy(self.nc_out_lanes),
                "cause_counts": copy.deepcopy(self.replay_cause_counts),
                "topdown": copy.deepcopy(self.topdown),
                "replay_all": service_state["last_raw"].get("tlb_hint", {}).get("replay_all", 0) if service_state["last_raw"] else 0,
            }
        if topic == "violations":
            return {
                "cycle": self.cycle,
                "is_degraded": True,
                "rar": {
                    "size": LQRAR_SIZE,
                    "entries": copy.deepcopy(self.rar_entries),
                    "stats": copy.deepcopy(self.rar_stats),
                },
                "raw": {
                    "size": LQRAW_SIZE,
                    "entries": copy.deepcopy(self.raw_entries),
                    "stats": copy.deepcopy(self.raw_stats),
                },
                "topdown": copy.deepcopy(self.topdown),
            }
        if topic == "perf":
            return {
                "cycle": self.cycle,
                "values": list(self.perf_values),
                "history": copy.deepcopy(self.history),
                "bars": {
                    "rar": self.rar_stats["violations"],
                    "raw": self.raw_stats["violations"],
                    "ma": self.replay_cause_counts["MA"],
                    "tm": self.replay_cause_counts["TM"],
                    "dm": self.replay_cause_counts["DM"],
                },
            }
        if topic == "events":
            return {
                "cycle": self.cycle,
                "events": self.consume_pending_events(),
            }
        raise KeyError(f"unknown topic {topic}")


class TopicPublisher:
    """Track per-topic state for incremental WebSocket broadcasts."""

    def __init__(self) -> None:
        self.topics = {
            "overview": TopicState(min_interval=1),
            "vlq": TopicState(min_interval=1),
            "sq": TopicState(min_interval=1),
            "replay": TopicState(min_interval=1),
            "violations": TopicState(min_interval=1),
            "perf": TopicState(min_interval=PERF_SAMPLE_PERIOD),
            "events": TopicState(min_interval=1),
        }

    def should_publish(self, topic: str, payload: dict[str, Any]) -> bool:
        state = self.topics[topic]
        cycle = int(payload.get("cycle", 0))
        if cycle - state.last_cycle < state.min_interval:
            if topic != "events":
                return False
        if topic == "events":
            return bool(payload.get("events"))
        key = _json_key(payload)
        if key == state.last_key:
            return False
        state.last_key = key
        state.last_cycle = cycle
        return True

    def snapshot(self, topic: str, payload: dict[str, Any]) -> None:
        state = self.topics[topic]
        state.last_key = _json_key(payload)
        state.last_cycle = int(payload.get("cycle", 0))
