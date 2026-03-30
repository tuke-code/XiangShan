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
INT_WRITEBACK_PORTS = 7
PERF_SAMPLE_PERIOD = 8
HISTORY_LIMIT = 48
EVENT_BACKLOG_LIMIT = 200
TRACE_EVENT_LIMIT = 16
TRACE_RECORD_LIMIT = 96


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


def _decode_cause_mask(mask: int) -> str | None:
    for idx, label in enumerate(CAUSE_LABELS):
        if mask & (1 << idx):
            return label
    return None


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

    def _has_signal(self, name: str) -> bool:
        return getattr(self.dut, name, None) is not None

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

    def _read_direct_vlq(self) -> dict[str, Any]:
        prefix = "MemBlock_inner_lsq_loadQueue_virtualLoadQueue"
        available = self._has_signal(f"{prefix}_allocated_0")
        entries = []
        if available:
            for idx in range(VLQ_SIZE):
                allocated = self._read(f"{prefix}_allocated_{idx}")
                entries.append(
                    {
                        "index": idx,
                        "allocated": allocated,
                        "request": {
                            "source": "direct",
                            "lq_idx": {"flag": 0, "value": idx},
                            "rob_idx": self._read_ptr(f"{prefix}_robIdx_{idx}"),
                            "uop_idx": self._read(f"{prefix}_uopIdx_{idx}"),
                            "is_vec": self._read(f"{prefix}_isvec_{idx}"),
                        }
                        if allocated
                        else None,
                    }
                )
        return {"available": available, "entries": entries}

    def _read_direct_lqr(self) -> dict[str, Any]:
        prefix = "MemBlock_inner_lsq_loadQueue_loadQueueReplay"
        available = self._has_signal(f"{prefix}_allocated_0")
        entries = []
        if available:
            for idx in range(LQR_SIZE):
                allocated = self._read(f"{prefix}_allocated_{idx}")
                cause_mask = self._read(f"{prefix}_cause_{idx}")
                entries.append(
                    {
                        "index": idx,
                        "allocated": allocated,
                        "scheduled": self._read(f"{prefix}_scheduled_{idx}"),
                        "blocking": self._read(f"{prefix}_blocking_{idx}"),
                        "cause": _decode_cause_mask(cause_mask),
                        "detail": {
                            "source": "direct",
                            "sched_index": idx,
                            "cause": _decode_cause_mask(cause_mask),
                            "lq_idx": self._read_ptr(f"{prefix}_uop_{idx}_lqIdx"),
                            "sq_idx": self._read_ptr(f"{prefix}_uop_{idx}_sqIdx"),
                            "rob_idx": self._read_ptr(f"{prefix}_uop_{idx}_robIdx"),
                            "uop_idx": self._read(f"{prefix}_uop_{idx}_uopIdx"),
                            "pdest": self._read(f"{prefix}_uop_{idx}_pdest"),
                            "vaddr": self._read(f"{prefix}_debug_vaddr_{idx}"),
                            "is_vec": self._read(f"{prefix}_vecReplay_{idx}_isvec"),
                            "is_128bit": self._read(f"{prefix}_vecReplay_{idx}_is128bit"),
                        }
                        if allocated
                        else None,
                    }
                )
        return {"available": available, "entries": entries}

    def _read_direct_sq(self) -> dict[str, Any]:
        prefix = "MemBlock_inner_lsq_storeQueue"
        available = self._has_signal(f"{prefix}_allocated_0")
        entries = []
        if available:
            for idx in range(SQ_SIZE):
                allocated = self._read(f"{prefix}_allocated_{idx}")
                entries.append(
                    {
                        "index": idx,
                        "allocated": allocated,
                        "addrvalid": self._read(f"{prefix}_addrvalid_{idx}"),
                        "datavalid": self._read(f"{prefix}_datavalid_{idx}"),
                        "committed": self._read(f"{prefix}_committed_{idx}"),
                        "completed": self._read(f"{prefix}_completed_{idx}"),
                        "nc": self._read(f"{prefix}_nc_{idx}"),
                        "is_vec": self._read(f"{prefix}_isVec_{idx}"),
                        "detail": {
                            "source": "direct",
                            "sq_idx": {"flag": 0, "value": idx},
                            "rob_idx": self._read_ptr(f"{prefix}_uop_{idx}_robIdx"),
                            "uop_idx": self._read(f"{prefix}_uop_{idx}_uopIdx"),
                            "pdest": self._read(f"{prefix}_uop_{idx}_pdest"),
                            "nc": self._read(f"{prefix}_nc_{idx}"),
                            "is_vec": self._read(f"{prefix}_isVec_{idx}"),
                        }
                        if allocated
                        else None,
                    }
                )
        return {"available": available, "entries": entries}

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
        writeback_lanes = []
        for idx in range(INT_WRITEBACK_PORTS):
            prefix = f"io_mem_to_ooo_intWriteback_{idx}_0"
            writeback_lanes.append(
                {
                    "lane": idx,
                    "valid": self._read(f"{prefix}_valid"),
                    "ready": self._read(f"{prefix}_ready"),
                    "rob_idx": self._read_ptr(f"{prefix}_bits_robIdx"),
                    "lq_idx": self._read_ptr(f"{prefix}_bits_lqIdx"),
                    "sq_idx": self._read_ptr(f"{prefix}_bits_sqIdx"),
                    "pdest": self._read(f"{prefix}_bits_pdest"),
                    "data": self._read(f"{prefix}_bits_data_0"),
                    "int_wen": self._read(f"{prefix}_bits_intWen"),
                    "is_from_load_unit": self._read(f"{prefix}_bits_isFromLoadUnit"),
                    "is_mmio": self._read(f"{prefix}_bits_debug_isMMIO"),
                    "is_ncio": self._read(f"{prefix}_bits_debug_isNCIO"),
                    "paddr": self._read(f"{prefix}_bits_debug_paddr"),
                    "vaddr": self._read(f"{prefix}_bits_debug_vaddr"),
                }
            )

        direct = {
            "vlq": self._read_direct_vlq(),
            "lqr": self._read_direct_lqr(),
            "sq": self._read_direct_sq(),
        }

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
            "writeback_lanes": writeback_lanes,
            "direct": direct,
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
        self.direct_state_available = {"vlq": False, "sq": False, "replay_entries": False}
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
        self.trace_records: dict[str, dict[str, Any]] = {}
        self.trace_order = deque()
        self.trace_aliases: dict[str, str] = {}
        self.pending_trace_keys: set[str] = set()
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

    def _ptr_alias(self, name: str, ptr: dict[str, Any] | None) -> str | None:
        if ptr is None:
            return None
        if "flag" not in ptr or "value" not in ptr:
            return None
        return f"{name}:{ptr['flag']}:{ptr['value']}"

    def _trace_alias_candidates(self, detail: dict[str, Any]) -> list[str]:
        aliases = []
        sched_index = detail.get("sched_index")
        if sched_index is not None and int(sched_index) >= 0:
            aliases.append(f"sched:{int(sched_index)}")
        for name in ("lq_idx", "sq_idx", "rob_idx"):
            alias = self._ptr_alias(name, detail.get(name))
            if alias is not None:
                aliases.append(alias)
        rob_idx = detail.get("rob_idx")
        uop_idx = detail.get("uop_idx")
        if rob_idx is not None and "flag" in rob_idx and "value" in rob_idx and uop_idx is not None:
            aliases.append(f"rob_uop:{rob_idx['flag']}:{rob_idx['value']}:{uop_idx}")
        return aliases

    def _preferred_trace_id(self, detail: dict[str, Any]) -> str:
        aliases = self._trace_alias_candidates(detail)
        if aliases:
            return aliases[0]
        return f"cycle:{self.cycle}:{len(self.trace_records)}"

    def _merge_trace_detail(self, target: dict[str, Any], detail: dict[str, Any]) -> None:
        for key, value in detail.items():
            if value is not None:
                target[key] = copy.deepcopy(value)

    def _get_or_create_trace(self, detail: dict[str, Any]) -> dict[str, Any]:
        trace_id = None
        for alias in self._trace_alias_candidates(detail):
            trace_id = self.trace_aliases.get(alias)
            if trace_id is not None and trace_id in self.trace_records:
                break
        if trace_id is None or trace_id not in self.trace_records:
            trace_id = self._preferred_trace_id(detail)
            trace = {
                "trace_id": trace_id,
                "first_cycle": self.cycle,
                "last_cycle": self.cycle,
                "status": "new",
                "source": detail.get("source"),
                "cause": detail.get("cause"),
                "completed": False,
                "identifiers": {},
                "detail": {},
                "events": [],
            }
            self.trace_records[trace_id] = trace
            self.trace_order.append(trace_id)
            while len(self.trace_records) > TRACE_RECORD_LIMIT:
                oldest = self.trace_order.popleft()
                self.trace_records.pop(oldest, None)
                self.trace_aliases = {alias: key for alias, key in self.trace_aliases.items() if key != oldest}
        trace = self.trace_records[trace_id]
        trace["last_cycle"] = self.cycle
        trace["source"] = detail.get("source") or trace.get("source")
        trace["cause"] = detail.get("cause") or trace.get("cause")
        identifiers = trace["identifiers"]
        for key in ("sched_index", "lq_idx", "sq_idx", "rob_idx", "uop_idx", "pdest"):
            if detail.get(key) is not None:
                identifiers[key] = copy.deepcopy(detail[key])
        self._merge_trace_detail(trace["detail"], detail)
        for alias in self._trace_alias_candidates(trace["detail"]):
            self.trace_aliases[alias] = trace_id
        return trace

    def _append_trace_event(self, detail: dict[str, Any], event_type: str, status: str, summary: str) -> None:
        trace = self._get_or_create_trace(detail)
        signature = (event_type, summary, detail.get("source"), detail.get("cause"), self.cycle)
        if trace["events"]:
            last_event = trace["events"][-1]
            if tuple(last_event.get("signature", ())) == signature:
                return
        event = {
            "cycle": self.cycle,
            "type": event_type,
            "status": status,
            "source": detail.get("source"),
            "cause": detail.get("cause"),
            "summary": summary,
            "detail": copy.deepcopy(detail),
            "signature": signature,
        }
        trace["events"].append(event)
        trace["events"] = trace["events"][-TRACE_EVENT_LIMIT:]
        trace["status"] = status
        if status in {"writeback", "retired"}:
            trace["completed"] = True
        self.pending_trace_keys.add(trace["trace_id"])

    def _trace_label(self, trace: dict[str, Any]) -> str:
        identifiers = trace.get("identifiers", {})
        sched = identifiers.get("sched_index")
        if sched is not None:
            return f"sched {sched}"
        lq_idx = identifiers.get("lq_idx")
        if isinstance(lq_idx, dict):
            return f"lq {lq_idx.get('value', '--')}"
        rob_idx = identifiers.get("rob_idx")
        if isinstance(rob_idx, dict):
            suffix = identifiers.get("uop_idx")
            if suffix is not None:
                return f"rob {rob_idx.get('value', '--')}.{suffix}"
            return f"rob {rob_idx.get('value', '--')}"
        return trace["trace_id"]

    def _trace_matches_filters(self, trace: dict[str, Any], filters: dict[str, Any] | None) -> bool:
        if not filters:
            return True
        identifiers = trace.get("identifiers", {})
        detail = trace.get("detail", {})
        sched = filters.get("sched_index")
        if sched and str(identifiers.get("sched_index")) != str(sched):
            return False
        lq_idx = filters.get("lq_idx")
        if lq_idx and str((identifiers.get("lq_idx") or {}).get("value")) != str(lq_idx):
            return False
        rob_idx = filters.get("rob_idx")
        if rob_idx and str((identifiers.get("rob_idx") or {}).get("value")) != str(rob_idx):
            return False
        cause = filters.get("cause")
        if cause and str(trace.get("cause") or detail.get("cause") or "").lower() != str(cause).lower():
            return False
        source = filters.get("source")
        if source and str(trace.get("source") or detail.get("source") or "").lower() != str(source).lower():
            return False
        state = filters.get("state")
        if state and str(trace.get("status") or "").lower() != str(state).lower():
            return False
        return True

    def matching_traces(self, filters: dict[str, Any] | None, only_pending: bool = False) -> list[dict[str, Any]]:
        records = []
        pending = self.pending_trace_keys if only_pending else None
        for trace_id in reversed(self.trace_order):
            trace = self.trace_records.get(trace_id)
            if trace is None:
                continue
            if pending is not None and trace_id not in pending:
                continue
            if self._trace_matches_filters(trace, filters):
                records.append(trace)
        return records

    def _public_trace(self, trace: dict[str, Any]) -> dict[str, Any]:
        events = []
        for event in trace["events"]:
            event_view = dict(event)
            event_view.pop("signature", None)
            events.append(copy.deepcopy(event_view))
        return {
            "trace_id": trace["trace_id"],
            "label": self._trace_label(trace),
            "first_cycle": trace["first_cycle"],
            "last_cycle": trace["last_cycle"],
            "status": trace["status"],
            "source": trace.get("source"),
            "cause": trace.get("cause"),
            "completed": trace["completed"],
            "identifiers": copy.deepcopy(trace["identifiers"]),
            "detail": copy.deepcopy(trace["detail"]),
            "events": events,
        }

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

    def _apply_direct_vlq(self, direct: dict[str, Any]) -> None:
        self.direct_state_available["vlq"] = bool(direct.get("available"))
        if not self.direct_state_available["vlq"]:
            return
        for idx, src in enumerate(direct.get("entries", [])):
            entry = self.vlq_entries[idx]
            if src.get("allocated"):
                request = copy.deepcopy(src.get("request"))
                entry.update(
                    {
                        "allocated": True,
                        "committed": False,
                        "age": entry["age"] or self.cycle,
                        "request": request,
                        "retire_at": None,
                    }
                )
            else:
                entry.update({"allocated": False, "committed": False, "age": 0, "request": None, "retire_at": None})

    def _apply_direct_lqr(self, direct: dict[str, Any]) -> None:
        self.direct_state_available["replay_entries"] = bool(direct.get("available"))
        if not self.direct_state_available["replay_entries"]:
            return
        for idx, src in enumerate(direct.get("entries", [])):
            entry = self.lqr_entries[idx]
            if src.get("allocated"):
                entry.update(
                    {
                        "allocated": True,
                        "scheduled": bool(src.get("scheduled")),
                        "blocking": bool(src.get("blocking")),
                        "cause": src.get("cause"),
                        "lane": entry.get("lane"),
                        "source": "direct",
                        "detail": copy.deepcopy(src.get("detail")),
                        "retire_at": None,
                    }
                )
            else:
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

    def _apply_direct_sq(self, direct: dict[str, Any]) -> None:
        self.direct_state_available["sq"] = bool(direct.get("available"))
        if not self.direct_state_available["sq"]:
            return
        for idx, src in enumerate(direct.get("entries", [])):
            entry = self.sq_entries[idx]
            if src.get("allocated"):
                entry.update(
                    {
                        "allocated": True,
                        "addrvalid": bool(src.get("addrvalid")),
                        "datavalid": bool(src.get("datavalid")),
                        "committed": bool(src.get("committed")),
                        "mmio": False,
                        "nc": bool(src.get("nc")),
                        "age": entry["age"] or self.cycle,
                        "detail": copy.deepcopy(src.get("detail")),
                        "retire_at": None,
                    }
                )
            else:
                entry.update(
                    {
                        "allocated": False,
                        "addrvalid": False,
                        "datavalid": False,
                        "committed": False,
                        "mmio": False,
                        "nc": False,
                        "age": 0,
                        "detail": None,
                        "retire_at": None,
                    }
                )

    def update(self, raw: dict[str, Any]) -> None:
        self.cycle = raw["cycle"]
        self.pending_events = []
        self.pending_trace_keys = set()
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
                        self._append_trace_event(entry["request"], "enq", "enqueued", f"enq lane={idx}")
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
                    if entry["request"] is not None:
                        retire_detail = copy.deepcopy(entry["request"])
                        retire_detail["source"] = "retire"
                        retire_detail["lq_idx"] = {"flag": 0, "value": idx}
                        self._append_trace_event(retire_detail, "retire", "retired", f"lq retire entry={idx}")
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
                self._append_trace_event(replay_lane, "replay_fire", "scheduled", f"replay fire lane={lane['lane']}")
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
                self._append_trace_event(lane_view, "ldu_replay", "blocking", f"ldu replay lane={lane['lane']}")
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
                self._append_trace_event(
                    lane_view,
                    "nc_fire" if lane["ready"] else "nc_wait",
                    "scheduled" if lane["ready"] else "blocking",
                    f"nc lane={lane['lane']}",
                )
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

        for lane in raw.get("writeback_lanes", []):
            if not lane["valid"]:
                continue
            wb_detail = {
                "source": "writeback",
                "lane": lane["lane"],
                "rob_idx": lane["rob_idx"],
                "lq_idx": lane["lq_idx"],
                "sq_idx": lane["sq_idx"],
                "pdest": lane["pdest"],
                "paddr": lane["paddr"],
                "vaddr": lane["vaddr"],
                "int_wen": lane["int_wen"],
                "is_mmio": lane["is_mmio"],
                "is_ncio": lane["is_ncio"],
                "is_from_load_unit": lane["is_from_load_unit"],
            }
            self._append_trace_event(wb_detail, "writeback", "writeback", f"wb lane={lane['lane']}")

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

        direct = raw.get("direct", {})
        self._apply_direct_vlq(direct.get("vlq", {}))
        self._apply_direct_lqr(direct.get("lqr", {}))
        self._apply_direct_sq(direct.get("sq", {}))

        self._prev = copy.deepcopy(raw)

    def load_reset_snapshot(self, raw: dict[str, Any]) -> None:
        """Seed non-shadow state after DUT reset without reconstructing queue activity."""
        self.reset()
        self.cycle = raw["cycle"]
        self.overview = copy.deepcopy(raw["overview"])
        self.topdown = copy.deepcopy(raw["debug_topdown"])
        self.perf_values = list(raw["perf_values"])
        direct = raw.get("direct", {})
        self._apply_direct_vlq(direct.get("vlq", {}))
        self._apply_direct_lqr(direct.get("lqr", {}))
        self._apply_direct_sq(direct.get("sq", {}))
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

    def topic_metadata(self, topic: str) -> tuple[str, list[str]]:
        if topic == "overview":
            return "direct", []
        if topic == "vlq":
            if self.direct_state_available["vlq"]:
                return "direct", []
            return "derived", ["virtual load queue entries are reconstructed from enq/lqDeqPtr activity"]
        if topic == "sq":
            if self.direct_state_available["sq"]:
                return "direct", []
            return "derived", ["store queue entries are reconstructed from enq/store addr/store data signals"]
        if topic == "replay":
            if self.direct_state_available["replay_entries"]:
                return "mixed", ["replay queue entries are direct, but replay lane activity/history is still aggregated from io_replay/io_ldu_ldin/io_ncOut"]
            return "mixed", ["replay entries are aggregated from io_replay/io_ldu_ldin/io_ncOut rather than a direct replay queue state array"]
        if topic == "violations":
            return "derived", ["RAR/RAW entries are reconstructed from counters, release and lane side effects"]
        if topic == "perf":
            return "mixed", ["perf bars/history mix direct perf counters with derived replay/violation counts"]
        if topic == "events":
            return "mixed", ["event stream is synthesized from observed lane activity and shadow queue transitions"]
        if topic == "traces":
            return "mixed", ["trace timeline is synthesized from enq/replay/ncOut/writeback/deq observations"]
        raise KeyError(f"unknown topic {topic}")

    def topic_payload(self, topic: str, service_state: dict[str, Any]) -> dict[str, Any]:
        data_quality, degraded_reasons = self.topic_metadata(topic)
        if topic == "overview":
            return {
                "cycle": self.cycle,
                "running": service_state["running"],
                "step_cycles": service_state["step_cycles"],
                "tick_ms": service_state["tick_ms"],
                "overview": self.overview,
                "data_quality": data_quality,
                "degraded_reasons": degraded_reasons,
                "degraded_features": [
                    feature
                    for feature, available in (
                        ("vlq-shadow", self.direct_state_available["vlq"]),
                        ("sq-shadow", self.direct_state_available["sq"]),
                        ("replay-shadow", self.direct_state_available["replay_entries"]),
                    )
                    if not available
                ]
                + ["violations-shadow"],
            }
        if topic == "vlq":
            allocated = sum(1 for entry in self.vlq_entries if entry["allocated"])
            avg_latency = 0
            if self.vlq_stats["enq"] > 0:
                avg_latency = round(self.vlq_stats["total_latency"] / self.vlq_stats["enq"])
            return {
                "cycle": self.cycle,
                "is_shadow": not self.direct_state_available["vlq"],
                "size": VLQ_SIZE,
                "allocated": allocated,
                "entries": copy.deepcopy(self.vlq_entries),
                "data_quality": data_quality,
                "degraded_reasons": degraded_reasons,
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
                "is_shadow": not self.direct_state_available["sq"],
                "size": SQ_SIZE,
                "allocated": allocated,
                "entries": copy.deepcopy(self.sq_entries),
                "data_quality": data_quality,
                "degraded_reasons": degraded_reasons,
                "stats": copy.deepcopy(self.sq_stats),
            }
        if topic == "replay":
            allocated = sum(1 for entry in self.lqr_entries if entry["allocated"])
            return {
                "cycle": self.cycle,
                "entries_quality": "direct" if self.direct_state_available["replay_entries"] else "derived",
                "size": LQR_SIZE,
                "allocated": allocated,
                "entries": copy.deepcopy(self.lqr_entries),
                "lanes": copy.deepcopy(self.replay_lanes),
                "ldu_lanes": copy.deepcopy(self.ldu_lanes),
                "nc_out_lanes": copy.deepcopy(self.nc_out_lanes),
                "data_quality": data_quality,
                "degraded_reasons": degraded_reasons,
                "cause_counts": copy.deepcopy(self.replay_cause_counts),
                "topdown": copy.deepcopy(self.topdown),
                "replay_all": service_state["last_raw"].get("tlb_hint", {}).get("replay_all", 0) if service_state["last_raw"] else 0,
            }
        if topic == "violations":
            return {
                "cycle": self.cycle,
                "is_degraded": True,
                "data_quality": data_quality,
                "degraded_reasons": degraded_reasons,
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
                "data_quality": data_quality,
                "degraded_reasons": degraded_reasons,
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
                "data_quality": data_quality,
                "degraded_reasons": degraded_reasons,
                "events": self.consume_pending_events(),
            }
        if topic == "traces":
            filters = copy.deepcopy(service_state.get("trace_filters") or {})
            traces = [self._public_trace(trace) for trace in self.matching_traces(filters)]
            return {
                "cycle": self.cycle,
                "filters": filters,
                "data_quality": data_quality,
                "degraded_reasons": degraded_reasons,
                "active_requests": traces[:40],
                "total_matching": len(traces),
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
            "traces": TopicState(min_interval=1),
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
