# coding=utf-8
"""
ROB 建模相关功能覆盖收集器。

覆盖模型的目标不是“证明 ROB 模型完整”，而是把 `rob_model.md` 中已经
能够依赖真实 DUT 观测到的行为，以及当前模型仍然存在的缺口，统一挂到
函数覆盖报告中。

设计原则：
  1. 以 DUT monitor / DUT 导出信号为主，不根据 testcase 名称手工打点。
  2. 允许借助 scoreboard 中由 DUT 观测得到的 shadow / drain / compare 状态。
  3. 对当前无法建模的项，单独放入 `KnownModelGaps` 组，避免与正向功能点混淆。
"""

from __future__ import annotations

from dataclasses import dataclass

import toffee.funcov as fc


SCALAR_ACCESS_BYTES = 8


@dataclass
class _ValueBox:
    value: int = 0


@dataclass(frozen=True)
class _RetiredStoreRecord:
    sq_idx: int
    rob_rank: int
    addr: int
    mask: int
    width_bytes: int
    mmio: bool
    nc: bool
    touched_bytes: frozenset[int]


def _read_signal(signal, default: int = 0) -> int:
    try:
        return int(signal.value)
    except Exception:
        return int(default)


def _rob_rank(rob_size: int, rob_idx_flag: int | None, rob_idx_value: int | None) -> int:
    if rob_idx_flag is None or rob_idx_value is None:
        return -1
    return int(rob_idx_flag) * int(rob_size) + int(rob_idx_value)


def _normalize_store_window(addr: int | None, mask: int, width_bytes: int) -> tuple[int, int, frozenset[int]] | None:
    if addr is None or width_bytes <= 0:
        return None
    aligned_addr = int(addr) & ~(int(width_bytes) - 1)
    byte_offset = int(addr) & (int(width_bytes) - 1)
    effective_mask = int(mask)
    if byte_offset and (effective_mask & 0x1):
        effective_mask <<= byte_offset
    effective_mask &= (1 << int(width_bytes)) - 1
    touched_bytes = frozenset(
        aligned_addr + byte_idx
        for byte_idx in range(int(width_bytes))
        if (effective_mask >> byte_idx) & 0x1
    )
    return aligned_addr, effective_mask, touched_bytes


def _access_bytes(addr: int, size_bytes: int) -> frozenset[int]:
    return frozenset(int(addr) + byte_idx for byte_idx in range(max(1, int(size_bytes))))


class RobCoverageCollector:
    """收集 ROB 半模型与真实 DUT 交互相关的功能覆盖。"""

    def __init__(self, env) -> None:
        self.env = env
        self._known_model_gaps = self._detect_known_model_gaps()
        self._boxes: dict[str, _ValueBox] = {}
        self._processed_drain_count = 0
        self._seen_load_writeback_keys = set()
        self._seen_retired_store_keys = set()
        self._retired_store_records: list[_RetiredStoreRecord] = []
        self._mmio_store_records: list[_RetiredStoreRecord] = []
        self._drain_touched_bytes = set()
        self._mmio_drain_overlap_observed = False
        self._prev_lq_deq_ptr = None
        self._prev_sq_deq_ptr = None
        self._groups = self._build_groups()
        self._initialize_static_gaps()

    def _detect_known_model_gaps(self) -> tuple[str, ...]:
        gaps = []
        if not getattr(self.env.commit_agent, "models_pending_ptr_next", False):
            gaps.append("pending_ptr_next_not_modelled")
        if not getattr(self.env.commit_agent, "models_commit_bool", False):
            gaps.append("commit_bool_not_modelled")
        if not getattr(self.env.commit_agent, "models_mixed_commit_packet", False):
            gaps.append("mixed_lcommit_scommit_not_modelled")
        gaps.extend(
            (
                "redirect_cancel_not_modelled",
                "backend_feedback_credit_not_modelled",
            )
        )
        return tuple(gaps)

    def _build_groups(self) -> list:
        observed = fc.CovGroup("MemBlock.ROB.ObservedBehavior")
        observed.add_watch_point(
            self._box("store_commit_frontier_observed"),
            {"committed_store_seen": fc.Eq(1)},
            name="store_commit_frontier_observed",
        )
        observed.add_watch_point(
            self._box("load_commit_budget_observed"),
            {"lq_deq_seen": fc.Eq(1)},
            name="load_commit_budget_observed",
        )
        observed.add_watch_point(
            self._box("load_writeback_observed"),
            {"load_wb_seen": fc.Eq(1)},
            name="load_writeback_observed",
        )
        observed.add_watch_point(
            self._box("flushsb_command_observed"),
            {"flushsb_seen": fc.Eq(1)},
            name="flushsb_command_observed",
        )
        observed.add_watch_point(
            self._box("sbuffer_drain_observed"),
            {"sbuffer_drain_seen": fc.Eq(1)},
            name="sbuffer_drain_observed",
        )
        observed.add_watch_point(
            self._box("outer_drain_observed"),
            {"outer_put_seen": fc.Eq(1)},
            name="outer_drain_observed",
        )
        observed.add_watch_point(
            self._box("mmio_busy_observed"),
            {"mmio_busy_seen": fc.Eq(1)},
            name="mmio_busy_observed",
        )
        observed.add_watch_point(
            self._box("mmio_store_shadow_observed"),
            {"mmio_store_seen": fc.Eq(1)},
            name="mmio_store_shadow_observed",
        )
        observed.add_watch_point(
            self._box("replay_event_observed"),
            {"replay_seen": fc.Eq(1)},
            name="replay_event_observed",
        )
        observed.add_watch_point(
            self._box("memory_violation_observed"),
            {"violation_seen": fc.Eq(1)},
            name="memory_violation_observed",
        )
        observed.add_watch_point(
            self._box("raw_nuke_query_observed"),
            {"raw_query_seen": fc.Eq(1)},
            name="raw_nuke_query_observed",
        )
        observed.add_watch_point(
            self._box("rar_nuke_query_observed"),
            {"rar_query_seen": fc.Eq(1)},
            name="rar_nuke_query_observed",
        )
        observed.add_watch_point(
            self._box("rar_nuke_response_observed"),
            {"rar_nuke_seen": fc.Eq(1)},
            name="rar_nuke_response_observed",
        )
        observed.add_watch_point(
            self._box("release_hint_observed"),
            {"release_seen": fc.Eq(1)},
            name="release_hint_observed",
        )
        observed.add_watch_point(
            self._box("backend_feedback_observed"),
            {"sqdeq_or_ptr_seen": fc.Eq(1)},
            name="backend_feedback_observed",
        )

        current = fc.CovGroup("MemBlock.ROB.CurrentModel")
        current.add_watch_point(
            self._box("same_addr_store_then_load_observed"),
            {"same_addr_store_load_seen": fc.Eq(1)},
            name="same_addr_store_then_load_observed",
        )
        current.add_watch_point(
            self._box("overwrite_store_then_load_observed"),
            {"overwrite_store_load_seen": fc.Eq(1)},
            name="overwrite_store_then_load_observed",
        )
        current.add_watch_point(
            self._box("unrelated_store_then_load_observed"),
            {"unrelated_store_load_seen": fc.Eq(1)},
            name="unrelated_store_then_load_observed",
        )
        current.add_watch_point(
            self._box("mixed_load_store_window_observed"),
            {"mixed_ldst_seen": fc.Eq(1)},
            name="mixed_load_store_window_observed",
        )
        current.add_watch_point(
            self._box("cacheable_store_flush_drain_observed"),
            {"cacheable_flush_drain_seen": fc.Eq(1)},
            name="cacheable_store_flush_drain_observed",
        )
        current.add_watch_point(
            self._box("nc_store_flush_drain_observed"),
            {"nc_flush_drain_seen": fc.Eq(1)},
            name="nc_store_flush_drain_observed",
        )
        current.add_watch_point(
            self._box("mmio_store_excluded_from_drain_observed"),
            {"mmio_excluded_seen": fc.Eq(1)},
            name="mmio_store_excluded_from_drain_observed",
        )
        current.add_watch_point(
            self._box("non_mem_blocker_inserted_observed"),
            {"non_mem_insert_seen": fc.Eq(1)},
            name="non_mem_blocker_inserted_observed",
        )
        current.add_watch_point(
            self._box("non_mem_blocker_blocks_frontier_observed"),
            {"non_mem_block_seen": fc.Eq(1)},
            name="non_mem_blocker_blocks_frontier_observed",
        )
        current.add_watch_point(
            self._box("non_mem_blocker_released_observed"),
            {"non_mem_release_seen": fc.Eq(1)},
            name="non_mem_blocker_released_observed",
        )
        current.add_watch_point(
            self._box("non_mem_release_resumes_commit_observed"),
            {"non_mem_resume_seen": fc.Eq(1)},
            name="non_mem_release_resumes_commit_observed",
        )
        current.add_watch_point(
            self._box("store_token_without_ready_observed"),
            {"store_token_without_ready_seen": fc.Eq(1)},
            name="store_token_without_ready_observed",
        )
        current.add_watch_point(
            self._box("store_ready_without_token_observed"),
            {"store_ready_without_token_seen": fc.Eq(1)},
            name="store_ready_without_token_observed",
        )
        current.add_watch_point(
            self._box("store_ready_and_token_commit_observed"),
            {"store_ready_and_token_commit_seen": fc.Eq(1)},
            name="store_ready_and_token_commit_observed",
        )
        current.add_watch_point(
            self._box("older_unready_store_blocks_younger_mem_observed"),
            {"store_blocks_younger_seen": fc.Eq(1)},
            name="older_unready_store_blocks_younger_mem_observed",
        )
        current.add_watch_point(
            self._box("store_readiness_resumes_frontier_observed"),
            {"store_readiness_resume_seen": fc.Eq(1)},
            name="store_readiness_resumes_frontier_observed",
        )

        gap_observed = fc.CovGroup("MemBlock.ROB.GapObserved")
        gap_observed.add_watch_point(
            self._box("gap_mmio_busy_without_commit_bool"),
            {"mmio_busy_gap_seen": fc.Eq(1)},
            name="gap_mmio_busy_without_commit_bool",
        )
        gap_observed.add_watch_point(
            self._box("gap_backend_feedback_without_model"),
            {"backend_feedback_gap_seen": fc.Eq(1)},
            name="gap_backend_feedback_without_model",
        )
        gap_observed.add_watch_point(
            self._box("gap_replay_without_redirect_cancel_model"),
            {"replay_gap_seen": fc.Eq(1)},
            name="gap_replay_without_redirect_cancel_model",
        )
        gap_observed.add_watch_point(
            self._box("gap_mixed_commit_window_without_packet_model"),
            {"mixed_commit_gap_seen": fc.Eq(1)},
            name="gap_mixed_commit_window_without_packet_model",
        )

        known_gaps = fc.CovGroup("MemBlock.ROB.KnownModelGaps")
        for gap_name in self._known_model_gaps:
            known_gaps.add_watch_point(
                self._box(f"known_gap_{gap_name}"),
                {gap_name: fc.Eq(1)},
                name=f"known_gap_{gap_name}",
            )

        return [observed, current, gap_observed, known_gaps]

    def _initialize_static_gaps(self) -> None:
        for gap_name in self._known_model_gaps:
            self._latch(f"known_gap_{gap_name}")

    def _box(self, name: str) -> _ValueBox:
        if name not in self._boxes:
            self._boxes[name] = _ValueBox()
        return self._boxes[name]

    def _latch(self, name: str, value: int = 1) -> None:
        self._box(name).value = max(int(value), int(self._box(name).value))

    def _ptr_tuple(self, flag_signal, value_signal):
        if flag_signal is None or value_signal is None:
            return None
        return (_read_signal(flag_signal, 0), _read_signal(value_signal, 0))

    def sample(self) -> None:
        self._sample_store_shadow()
        self._sample_mem_status()
        self._sample_flush_inputs()
        self._sample_writebacks()
        self._sample_drain_log()
        self._sample_replay_related()
        self._sample_rob_agent()
        for group in self._groups:
            group.sample()

    def finalize(self) -> None:
        self._sample_store_shadow()
        self._sample_drain_log()
        self._sample_mem_status()
        self._sample_rob_agent()
        if self._box("flushsb_command_observed").value and self._box("sbuffer_drain_observed").value:
            self._latch("cacheable_store_flush_drain_observed")
        if self._box("flushsb_command_observed").value and self._box("outer_drain_observed").value:
            self._latch("nc_store_flush_drain_observed")
        if self._box("mmio_store_shadow_observed").value and not self._mmio_drain_overlap_observed:
            self._latch("mmio_store_excluded_from_drain_observed")
        for group in self._groups:
            group.sample()

    def all_groups(self) -> tuple:
        return tuple(self._groups)

    def as_dict(self) -> list[dict]:
        return [group.as_dict() for group in self._groups]

    def _sample_store_shadow(self) -> None:
        pending_stores = getattr(self.env.memory, "pending_stores", {})
        for sq_idx, store in pending_stores.items():
            if getattr(store, "committed", False):
                self._latch("store_commit_frontier_observed")
            if getattr(store, "mmio", False):
                self._latch("mmio_store_shadow_observed")
                normalized_mmio = _normalize_store_window(store.addr, store.mask, store.width_bytes)
                if normalized_mmio is not None:
                    aligned_addr, effective_mask, touched_bytes = normalized_mmio
                    mmio_record = _RetiredStoreRecord(
                        sq_idx=int(sq_idx),
                        rob_rank=_rob_rank(
                            self.env.config.rob_size,
                            None if store.rob_idx is None else store.rob_idx.flag,
                            None if store.rob_idx is None else store.rob_idx.value,
                        ),
                        addr=int(aligned_addr),
                        mask=int(effective_mask),
                        width_bytes=int(store.width_bytes),
                        mmio=True,
                        nc=bool(store.nc),
                        touched_bytes=touched_bytes,
                    )
                    if mmio_record not in self._mmio_store_records:
                        self._mmio_store_records.append(mmio_record)

            if not getattr(store, "retired", False):
                continue
            store_key = (
                int(sq_idx),
                None if store.rob_idx is None else int(store.rob_idx.flag),
                None if store.rob_idx is None else int(store.rob_idx.value),
                None if store.addr is None else int(store.addr),
                int(getattr(store, "mask", 0)),
                int(getattr(store, "data", 0) or 0),
            )
            if store_key in self._seen_retired_store_keys:
                continue
            normalized = _normalize_store_window(store.addr, store.mask, store.width_bytes)
            if normalized is None:
                continue
            aligned_addr, effective_mask, touched_bytes = normalized
            record = _RetiredStoreRecord(
                sq_idx=int(sq_idx),
                rob_rank=_rob_rank(
                    self.env.config.rob_size,
                    None if store.rob_idx is None else store.rob_idx.flag,
                    None if store.rob_idx is None else store.rob_idx.value,
                ),
                addr=int(aligned_addr),
                mask=int(effective_mask),
                width_bytes=int(store.width_bytes),
                mmio=bool(store.mmio),
                nc=bool(store.nc),
                touched_bytes=touched_bytes,
            )
            self._retired_store_records.append(record)
            self._seen_retired_store_keys.add(store_key)
            if record.mmio:
                self._mmio_store_records.append(record)

    def _sample_mem_status(self) -> None:
        if _read_signal(self.env.mem_status.lqDeq, 0) > 0:
            self._latch("load_commit_budget_observed")

        if _read_signal(self.env.mem_status.sqDeq, 0) > 0:
            self._latch("backend_feedback_observed")
            self._latch("gap_backend_feedback_without_model")

        current_lq_ptr = self._ptr_tuple(
            getattr(self.env.mem_status, "lqDeqPtr_flag", None),
            getattr(self.env.mem_status, "lqDeqPtr_value", None),
        )
        current_sq_ptr = self._ptr_tuple(
            getattr(self.env.mem_status, "sqDeqPtr_flag", None),
            getattr(self.env.mem_status, "sqDeqPtr_value", None),
        )
        if self._prev_lq_deq_ptr is not None and current_lq_ptr is not None and current_lq_ptr != self._prev_lq_deq_ptr:
            self._latch("backend_feedback_observed")
            self._latch("gap_backend_feedback_without_model")
        if self._prev_sq_deq_ptr is not None and current_sq_ptr is not None and current_sq_ptr != self._prev_sq_deq_ptr:
            self._latch("backend_feedback_observed")
            self._latch("gap_backend_feedback_without_model")
        self._prev_lq_deq_ptr = current_lq_ptr
        self._prev_sq_deq_ptr = current_sq_ptr

        if _read_signal(self.env.lsq_status.mmioBusy, 0):
            self._latch("mmio_busy_observed")
            if not getattr(self.env.commit_agent, "models_commit_bool", False):
                self._latch("gap_mmio_busy_without_commit_bool")

        if _read_signal(self.env.mem_status.sbIsEmpty, 0) and self._box("flushsb_command_observed").value:
            if self._box("sbuffer_drain_observed").value:
                self._latch("cacheable_store_flush_drain_observed")
            if self._box("outer_drain_observed").value:
                self._latch("nc_store_flush_drain_observed")

        if _read_signal(self.env.mem_status.memoryViolation_valid, 0):
            self._latch("memory_violation_observed")
            self._latch("replay_event_observed")
            self._latch("gap_replay_without_redirect_cancel_model")

        if (
            not getattr(self.env.commit_agent, "models_mixed_commit_packet", False)
            and _read_signal(getattr(self.env.dut, "io_ooo_to_mem_lsqio_scommit", None), 0) > 0
            and _read_signal(self.env.mem_status.lqDeq, 0) > 0
        ):
            self._latch("gap_mixed_commit_window_without_packet_model")

    def _sample_flush_inputs(self) -> None:
        if _read_signal(getattr(self.env.dut, "io_ooo_to_mem_flushSb", None), 0):
            self._latch("flushsb_command_observed")

    def _sample_writebacks(self) -> None:
        for lane, bundle in enumerate(self.env.writeback):
            if not bundle.connected("valid") or bundle.read("valid", 0) == 0:
                continue
            if bundle.connected("ready") and bundle.read("ready", 0) == 0:
                continue
            if bundle.connected("isFromLoadUnit") and bundle.read("isFromLoadUnit", 0) == 0:
                continue
            if bundle.connected("toRob_valid") and bundle.read("toRob_valid", 0) == 0:
                continue
            if bundle.connected("intWen") and bundle.read("intWen", 0) == 0:
                continue
            load_key = (
                int(getattr(self.env.memory, "_cycle", 0)),
                lane,
                bundle.read("robIdx_flag", 0),
                bundle.read("robIdx_value", 0),
                bundle.read("debug_paddr", -1),
            )
            if load_key in self._seen_load_writeback_keys:
                continue
            self._seen_load_writeback_keys.add(load_key)
            self._latch("load_writeback_observed")

            load_rank = _rob_rank(
                self.env.config.rob_size,
                bundle.read("robIdx_flag", None),
                bundle.read("robIdx_value", None),
            )
            older_retired = [store for store in self._retired_store_records if store.rob_rank <= load_rank]
            if older_retired:
                self._latch("mixed_load_store_window_observed")

            load_paddr = bundle.read("debug_paddr", None) if bundle.connected("debug_paddr") else None
            if load_paddr is None:
                continue

            load_bytes = _access_bytes(int(load_paddr), SCALAR_ACCESS_BYTES)
            overlapping = [store for store in older_retired if store.touched_bytes & load_bytes]
            if overlapping:
                self._latch("same_addr_store_then_load_observed")
            if len(overlapping) >= 2:
                self._latch("overwrite_store_then_load_observed")
            if older_retired and not overlapping:
                self._latch("unrelated_store_then_load_observed")

    def _sample_drain_log(self) -> None:
        drain_log = getattr(self.env.memory, "drain_log", [])
        while self._processed_drain_count < len(drain_log):
            event = drain_log[self._processed_drain_count]
            self._processed_drain_count += 1
            normalized = _normalize_store_window(
                event.get("addr"),
                event.get("mask", 0),
                event.get("width_bytes", 0),
            )
            if normalized is not None:
                _, _, touched_bytes = normalized
                self._drain_touched_bytes.update(touched_bytes)
                for record in self._mmio_store_records:
                    if record.touched_bytes & touched_bytes:
                        self._mmio_drain_overlap_observed = True
                        break
            if event.get("channel") == "sbuffer":
                self._latch("sbuffer_drain_observed")
                if self._box("flushsb_command_observed").value:
                    self._latch("cacheable_store_flush_drain_observed")
            if event.get("channel") == "outer":
                self._latch("outer_drain_observed")
                if self._box("flushsb_command_observed").value:
                    self._latch("nc_store_flush_drain_observed")

    def _sample_replay_related(self) -> None:
        raw_query_seen = False
        rar_query_seen = False
        rar_nuke_seen = False
        replay_hint = False
        for idx in range(self.env.config.load_pipeline_width):
            raw_prefix = f"MemBlock_inner_lsq_io_ldu_rawNukeQuery_{idx}_req"
            rar_prefix = f"MemBlock_inner_lsq_io_ldu_rarNukeQuery_{idx}_req"
            rar_resp_prefix = f"MemBlock_inner_lsq_io_ldu_rarNukeQuery_{idx}_resp"
            replay_prefix = f"MemBlock_inner_lsq_io_replay_{idx}"
            nc_prefix = f"MemBlock_inner_lsq_io_ncOut_{idx}"
            ldu_prefix = f"MemBlock_inner_lsq_io_ldu_ldin_{idx}"
            if _read_signal(getattr(self.env.dut, f"{raw_prefix}_valid", None), 0):
                raw_query_seen = True
            if _read_signal(getattr(self.env.dut, f"{rar_prefix}_valid", None), 0):
                rar_query_seen = True
            if (
                _read_signal(getattr(self.env.dut, f"{rar_resp_prefix}_valid", None), 0)
                and _read_signal(getattr(self.env.dut, f"{rar_resp_prefix}_bits_nuke", None), 0)
            ):
                rar_nuke_seen = True
            if _read_signal(getattr(self.env.dut, f"{replay_prefix}_valid", None), 0):
                replay_hint = True
            if _read_signal(getattr(self.env.dut, f"{nc_prefix}_valid", None), 0):
                replay_hint = True
            if (
                _read_signal(getattr(self.env.dut, f"{ldu_prefix}_valid", None), 0)
                and _read_signal(getattr(self.env.dut, f"{ldu_prefix}_bits_isLoadReplay", None), 0)
            ):
                replay_hint = True

        if raw_query_seen:
            self._latch("raw_nuke_query_observed")
        if rar_query_seen:
            self._latch("rar_nuke_query_observed")
        if rar_nuke_seen:
            self._latch("rar_nuke_response_observed")
        if replay_hint:
            self._latch("replay_event_observed")
            self._latch("gap_replay_without_redirect_cancel_model")

        if _read_signal(getattr(self.env.dut, "MemBlock_inner_lsq_io_release_valid", None), 0):
            self._latch("release_hint_observed")

    def _sample_rob_agent(self) -> None:
        stats = getattr(getattr(self.env, "rob_agent", None), "stats", {})
        if stats.get("rob_non_mem_insert_count", 0) > 0:
            self._latch("non_mem_blocker_inserted_observed")
        if stats.get("rob_non_mem_blocked_cycle_count", 0) > 0:
            self._latch("non_mem_blocker_blocks_frontier_observed")
        if stats.get("rob_non_mem_release_count", 0) > 0:
            self._latch("non_mem_blocker_released_observed")
        if stats.get("rob_non_mem_resume_count", 0) > 0:
            self._latch("non_mem_release_resumes_commit_observed")
        if stats.get("rob_store_token_without_ready_count", 0) > 0:
            self._latch("store_token_without_ready_observed")
        if stats.get("rob_store_ready_without_token_count", 0) > 0:
            self._latch("store_ready_without_token_observed")
        if stats.get("rob_store_ready_and_token_commit_count", 0) > 0:
            self._latch("store_ready_and_token_commit_observed")
        if stats.get("rob_store_blocks_younger_count", 0) > 0:
            self._latch("older_unready_store_blocks_younger_mem_observed")
        if stats.get("rob_store_readiness_resume_count", 0) > 0:
            self._latch("store_readiness_resumes_frontier_observed")
