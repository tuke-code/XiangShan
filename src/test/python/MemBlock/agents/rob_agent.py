# coding=utf-8
"""
ROB-LSQ forward model for MemBlock verification.
"""

from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass, field

from transactions import QueuePtr


@dataclass(frozen=True)
class RobIndex:
    """Lightweight ROB pointer used by the ROB agent."""

    flag: int
    value: int


@dataclass
class RobEntry:
    """ROB entry tracked by the MemBlock-side commit proxy."""

    rob_idx: RobIndex
    kind: str
    sq_ptr: QueuePtr | None = None
    issued: bool = True
    exec_completed: bool = False
    addr_ready: bool = False
    data_ready: bool = False
    commit_ready: bool = False
    explicit_commit_ready: bool | None = None


@dataclass(frozen=True)
class RobCommitPacket:
    """Structured commit packet derived from the ROB entry queue."""

    commit: bool
    lcommit: int
    scommit: int
    pending_ptr_before: RobIndex
    pending_ptr_after: RobIndex
    pending_ptr_next: RobIndex
    committed_entries: tuple[RobEntry, ...] = field(default_factory=tuple)
    blocked_by: str | None = None

    @staticmethod
    def empty(ptr: RobIndex) -> "RobCommitPacket":
        return RobCommitPacket(
            commit=False,
            lcommit=0,
            scommit=0,
            pending_ptr_before=ptr,
            pending_ptr_after=ptr,
            pending_ptr_next=ptr,
            committed_entries=(),
            blocked_by=None,
        )


class RobAgent:
    """Drive MemBlock ROB/LSQ interface from a ROB-aware proxy model."""

    def __init__(self, dut, rob_size: int) -> None:
        self.dut = dut
        self.rob_size = int(rob_size)
        self._signal_lcommit = getattr(dut, "io_ooo_to_mem_lsqio_lcommit", None)
        self._signal_scommit = getattr(dut, "io_ooo_to_mem_lsqio_scommit", None)
        self._signal_commit = getattr(dut, "io_ooo_to_mem_lsqio_commit", None)
        self._signal_pending_ptr_flag = getattr(dut, "io_ooo_to_mem_lsqio_pendingPtr_flag", None)
        self._signal_pending_ptr_value = getattr(dut, "io_ooo_to_mem_lsqio_pendingPtr_value", None)
        self._signal_pending_ptr_next_flag = getattr(dut, "io_ooo_to_mem_lsqio_pendingPtrNext_flag", None)
        self._signal_pending_ptr_next_value = getattr(dut, "io_ooo_to_mem_lsqio_pendingPtrNext_value", None)
        self.reset()

    @property
    def signal_support(self) -> dict[str, bool]:
        return {
            "lcommit": self._signal_lcommit is not None,
            "scommit": self._signal_scommit is not None,
            "commit": self._signal_commit is not None,
            "pending_ptr": self._signal_pending_ptr_flag is not None and self._signal_pending_ptr_value is not None,
            "pending_ptr_next": self._signal_pending_ptr_next_flag is not None and self._signal_pending_ptr_next_value is not None,
        }

    @property
    def supports_full_rob_lsqio(self) -> bool:
        return all(self.signal_support.values())

    @property
    def models_pending_ptr_next(self) -> bool:
        return True

    @property
    def models_commit_bool(self) -> bool:
        return True

    @property
    def models_mixed_commit_packet(self) -> bool:
        return True

    @property
    def stats(self) -> dict[str, int]:
        return {
            "rob_lcommit_driven_count": self.driven_lcommit_count,
            "rob_scommit_driven_count": self.driven_scommit_count,
            "rob_commit_cycle_count": self.commit_cycle_count,
            "rob_missing_signal_count": sum(0 if present else 1 for present in self.signal_support.values()),
            "rob_pending_entry_count": len(self._entries),
            "rob_non_mem_insert_count": self.non_mem_insert_count,
            "rob_non_mem_release_count": self.non_mem_release_count,
            "rob_non_mem_blocked_cycle_count": self.non_mem_blocked_cycle_count,
            "rob_non_mem_resume_count": self.non_mem_resume_count,
            "rob_store_addr_ready_count": self.store_addr_ready_count,
            "rob_store_data_ready_count": self.store_data_ready_count,
            "rob_store_explicit_ready_count": self.store_explicit_ready_count,
            "rob_store_ready_and_token_commit_count": self.store_ready_and_token_commit_count,
            "rob_store_token_without_ready_count": self.store_token_without_ready_count,
            "rob_store_ready_without_token_count": self.store_ready_without_token_count,
            "rob_store_readiness_block_count": self.store_readiness_block_count,
            "rob_store_readiness_resume_count": self.store_readiness_resume_count,
            "rob_store_blocks_younger_count": self.store_blocks_younger_count,
        }

    def reset(self) -> None:
        self.pending_ptr = RobIndex(flag=0, value=0)
        self.pending_ptr_next = self.pending_ptr
        self._entries: deque[RobEntry] = deque()
        self._orphan_load_completions: defaultdict[RobIndex, int] = defaultdict(int)
        self._orphan_non_mem_releases: defaultdict[RobIndex, int] = defaultdict(int)
        self._store_entries_by_sq: dict[tuple[int, int], RobEntry] = {}
        self._pending_store_state: dict[tuple[int, int], dict[str, object]] = {}
        self._queued_store_tokens = 0
        self._active_store_tokens = 0
        self._prepared_for_cycle = False
        self.latest_commit_packet = RobCommitPacket.empty(self.pending_ptr)
        self.driven_lcommit_count = 0
        self.driven_scommit_count = 0
        self.commit_cycle_count = 0
        self.non_mem_insert_count = 0
        self.non_mem_release_count = 0
        self.non_mem_blocked_cycle_count = 0
        self.non_mem_resume_count = 0
        self.store_addr_ready_count = 0
        self.store_data_ready_count = 0
        self.store_explicit_ready_count = 0
        self.store_ready_and_token_commit_count = 0
        self.store_token_without_ready_count = 0
        self.store_ready_without_token_count = 0
        self.store_readiness_block_count = 0
        self.store_readiness_resume_count = 0
        self.store_blocks_younger_count = 0
        self._resume_after_non_mem_block = False
        self._resume_after_store_block = False

    def set_pending_ptr(self, ptr) -> None:
        if self._entries:
            raise RuntimeError("cannot reset ROB pending_ptr while outstanding entries exist")
        flag = int(ptr.flag)
        value = int(ptr.value)
        if flag not in (0, 1):
            raise ValueError(f"ROB pending_ptr flag must be 0 or 1, got {flag}")
        if value < 0 or value >= self.rob_size:
            raise ValueError(f"ROB pending_ptr value out of range: {value}, rob_size={self.rob_size}")
        normalized = RobIndex(flag=flag, value=value)
        self.pending_ptr = normalized
        self.pending_ptr_next = normalized
        self.latest_commit_packet = RobCommitPacket.empty(normalized)
        self._prepared_for_cycle = False

    def note_load_issued(self, rob_idx_flag: int, rob_idx_value: int) -> None:
        rob_idx = RobIndex(flag=int(rob_idx_flag), value=int(rob_idx_value))
        entry = RobEntry(rob_idx=rob_idx, kind="load")
        if self._orphan_load_completions[rob_idx] > 0:
            entry.exec_completed = True
            entry.commit_ready = True
            self._orphan_load_completions[rob_idx] -= 1
            if self._orphan_load_completions[rob_idx] == 0:
                del self._orphan_load_completions[rob_idx]
        self._entries.append(entry)

    def note_store_allocated(
        self,
        rob_idx_flag: int,
        rob_idx_value: int,
        *,
        sq_idx_flag: int | None = None,
        sq_idx_value: int | None = None,
    ) -> None:
        rob_idx = RobIndex(flag=int(rob_idx_flag), value=int(rob_idx_value))
        sq_ptr = None
        if sq_idx_flag is not None and sq_idx_value is not None:
            sq_ptr = QueuePtr(flag=int(sq_idx_flag), value=int(sq_idx_value))
        entry = RobEntry(rob_idx=rob_idx, kind="store", sq_ptr=sq_ptr)
        if sq_ptr is not None:
            sq_key = self._sq_key(sq_ptr.flag, sq_ptr.value)
            self._store_entries_by_sq[sq_key] = entry
            pending_state = self._pending_store_state.pop(sq_key, None)
            if pending_state is not None:
                entry.addr_ready = bool(pending_state.get("addr_ready", False))
                entry.data_ready = bool(pending_state.get("data_ready", False))
                entry.explicit_commit_ready = pending_state.get("explicit_commit_ready")
                self._refresh_store_commit_ready(entry)
        self._entries.append(entry)

    def note_non_mem_issued(self, rob_idx_flag: int, rob_idx_value: int) -> None:
        rob_idx = RobIndex(flag=int(rob_idx_flag), value=int(rob_idx_value))
        entry = RobEntry(rob_idx=rob_idx, kind="non_mem")
        if self._orphan_non_mem_releases[rob_idx] > 0:
            entry.commit_ready = True
            self._orphan_non_mem_releases[rob_idx] -= 1
            if self._orphan_non_mem_releases[rob_idx] == 0:
                del self._orphan_non_mem_releases[rob_idx]
        self._entries.append(entry)
        self.non_mem_insert_count += 1

    def release_non_mem(self, rob_idx_flag: int, rob_idx_value: int) -> None:
        rob_idx = RobIndex(flag=int(rob_idx_flag), value=int(rob_idx_value))
        for entry in self._entries:
            if entry.kind == "non_mem" and entry.rob_idx == rob_idx and not entry.commit_ready:
                entry.commit_ready = True
                self.non_mem_release_count += 1
                return
        self.non_mem_release_count += 1
        self._orphan_non_mem_releases[rob_idx] += 1

    def note_load_completed(self, rob_idx_flag: int, rob_idx_value: int) -> None:
        rob_idx = RobIndex(flag=int(rob_idx_flag), value=int(rob_idx_value))
        for entry in self._entries:
            if entry.kind == "load" and entry.rob_idx == rob_idx and not entry.exec_completed:
                entry.exec_completed = True
                entry.commit_ready = True
                return
        self._orphan_load_completions[rob_idx] += 1

    def mark_store_addr_ready(self, sq_idx_flag: int, sq_idx_value: int) -> None:
        entry = self._lookup_store_entry(sq_idx_flag, sq_idx_value)
        if entry is None:
            pending_state = self._pending_store_state.setdefault(
                self._sq_key(sq_idx_flag, sq_idx_value),
                {"addr_ready": False, "data_ready": False, "explicit_commit_ready": None},
            )
            if pending_state["addr_ready"]:
                return
            pending_state["addr_ready"] = True
            self.store_addr_ready_count += 1
            return
        if not entry.addr_ready:
            entry.addr_ready = True
            self.store_addr_ready_count += 1
        self._refresh_store_commit_ready(entry)

    def mark_store_data_ready(self, sq_idx_flag: int, sq_idx_value: int) -> None:
        entry = self._lookup_store_entry(sq_idx_flag, sq_idx_value)
        if entry is None:
            pending_state = self._pending_store_state.setdefault(
                self._sq_key(sq_idx_flag, sq_idx_value),
                {"addr_ready": False, "data_ready": False, "explicit_commit_ready": None},
            )
            if pending_state["data_ready"]:
                return
            pending_state["data_ready"] = True
            self.store_data_ready_count += 1
            return
        if not entry.data_ready:
            entry.data_ready = True
            self.store_data_ready_count += 1
        self._refresh_store_commit_ready(entry)

    def mark_store_commit_ready(self, sq_idx_flag: int, sq_idx_value: int, ready: bool = True) -> None:
        entry = self._lookup_store_entry(sq_idx_flag, sq_idx_value)
        if entry is None:
            pending_state = self._pending_store_state.setdefault(
                self._sq_key(sq_idx_flag, sq_idx_value),
                {"addr_ready": False, "data_ready": False, "explicit_commit_ready": None},
            )
            pending_state["explicit_commit_ready"] = bool(ready)
            self.store_explicit_ready_count += 1
            return
        entry.explicit_commit_ready = bool(ready)
        self.store_explicit_ready_count += 1
        self._refresh_store_commit_ready(entry)

    def queue_store_commit(self, count: int = 1) -> None:
        self._queued_store_tokens = max(0, int(count))

    def drive(self) -> None:
        if not self._prepared_for_cycle:
            self._active_store_tokens = self._queued_store_tokens
            self._queued_store_tokens = 0
            self.latest_commit_packet = self._build_commit_packet(self._active_store_tokens)
            self.pending_ptr_next = self.latest_commit_packet.pending_ptr_next
            self._prepared_for_cycle = True

        self._drive_optional(self._signal_lcommit, self.latest_commit_packet.lcommit)
        self._drive_optional(self._signal_scommit, self.latest_commit_packet.scommit)
        self._drive_optional(self._signal_commit, int(self.latest_commit_packet.commit))
        self._drive_optional(self._signal_pending_ptr_flag, self.latest_commit_packet.pending_ptr_before.flag)
        self._drive_optional(self._signal_pending_ptr_value, self.latest_commit_packet.pending_ptr_before.value)
        self._drive_optional(self._signal_pending_ptr_next_flag, self.latest_commit_packet.pending_ptr_next.flag)
        self._drive_optional(self._signal_pending_ptr_next_value, self.latest_commit_packet.pending_ptr_next.value)

    def advance(self) -> None:
        if not self._prepared_for_cycle:
            self.latest_commit_packet = self._build_commit_packet(self._queued_store_tokens)

        packet = self.latest_commit_packet
        if packet.commit:
            self.driven_lcommit_count += int(packet.lcommit)
            self.driven_scommit_count += int(packet.scommit)
            self.commit_cycle_count += 1

        for _ in packet.committed_entries:
            if not self._entries:
                break
            entry = self._entries.popleft()
            if entry.kind == "store" and entry.sq_ptr is not None:
                self._store_entries_by_sq.pop(self._sq_key(entry.sq_ptr.flag, entry.sq_ptr.value), None)
        self.pending_ptr = packet.pending_ptr_after
        self.pending_ptr_next = self.pending_ptr
        self._active_store_tokens = 0
        self._prepared_for_cycle = False
        self.latest_commit_packet = RobCommitPacket.empty(self.pending_ptr)

    def _build_commit_packet(self, store_tokens: int) -> RobCommitPacket:
        pending_before = self.pending_ptr
        if not self._entries:
            return RobCommitPacket.empty(pending_before)

        lcommit = 0
        scommit = 0
        committed: list[RobEntry] = []
        remaining_store_tokens = max(0, int(store_tokens))
        blocked_by = None

        for index, entry in enumerate(self._entries):
            if entry.kind == "load":
                if not entry.exec_completed:
                    blocked_by = "load_not_ready"
                    break
                lcommit += 1
            elif entry.kind == "store":
                if not entry.commit_ready:
                    blocked_by = "store_not_ready"
                    self.store_readiness_block_count += 1
                    if remaining_store_tokens > 0:
                        self.store_token_without_ready_count += 1
                    if len(self._entries) > index + 1:
                        self.store_blocks_younger_count += 1
                    break
                if remaining_store_tokens <= 0:
                    blocked_by = "store_token_unavailable"
                    self.store_ready_without_token_count += 1
                    break
                remaining_store_tokens -= 1
                scommit += 1
            elif entry.kind == "non_mem":
                if not entry.commit_ready:
                    blocked_by = "non_mem_blocked"
                    self.non_mem_blocked_cycle_count += 1
                    break
            else:
                blocked_by = "unsupported_entry_kind"
                break
            committed.append(entry)

        commit_count = len(committed)
        if blocked_by == "non_mem_blocked":
            self._resume_after_non_mem_block = True
        if blocked_by == "store_not_ready":
            self._resume_after_store_block = True
        if commit_count > 0 and self._resume_after_non_mem_block and blocked_by != "non_mem_blocked":
            self.non_mem_resume_count += 1
            self._resume_after_non_mem_block = False
        if commit_count > 0 and self._resume_after_store_block and blocked_by != "store_not_ready":
            self.store_readiness_resume_count += 1
            self._resume_after_store_block = False
        if scommit > 0:
            self.store_ready_and_token_commit_count += scommit

        pending_after = self._inc_many(pending_before, commit_count)
        pending_next = pending_after if commit_count else pending_before
        return RobCommitPacket(
            commit=commit_count > 0,
            lcommit=lcommit,
            scommit=scommit,
            pending_ptr_before=pending_before,
            pending_ptr_after=pending_after,
            pending_ptr_next=pending_next,
            committed_entries=tuple(committed),
            blocked_by=blocked_by,
        )

    def _refresh_store_commit_ready(self, entry: RobEntry) -> None:
        if entry.kind != "store":
            return
        derived_ready = entry.addr_ready and entry.data_ready
        if entry.explicit_commit_ready is None:
            entry.commit_ready = derived_ready
        else:
            entry.commit_ready = bool(entry.explicit_commit_ready)

    def _lookup_store_entry(self, sq_idx_flag: int, sq_idx_value: int) -> RobEntry | None:
        return self._store_entries_by_sq.get(self._sq_key(sq_idx_flag, sq_idx_value))

    @staticmethod
    def _sq_key(flag: int, value: int) -> tuple[int, int]:
        return (int(flag), int(value))

    def _inc_many(self, ptr: RobIndex, count: int) -> RobIndex:
        result = ptr
        for _ in range(max(0, int(count))):
            result = self._inc(result)
        return result

    def _inc(self, ptr: RobIndex) -> RobIndex:
        value = int(ptr.value) + 1
        flag = int(ptr.flag)
        if value >= self.rob_size:
            value = 0
            flag ^= 0x1
        return RobIndex(flag=flag, value=value)

    @staticmethod
    def _drive_optional(signal, value: int) -> None:
        if signal is not None:
            signal.value = int(value)
