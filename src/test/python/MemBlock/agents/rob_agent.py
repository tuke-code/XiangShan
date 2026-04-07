# coding=utf-8
"""
ROB-LSQ forward model for MemBlock verification.
"""

from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass, field


@dataclass(frozen=True)
class RobIndex:
    """Lightweight ROB pointer used by the ROB agent."""

    flag: int
    value: int


@dataclass
class RobEntry:
    """Minimal mem-only ROB entry for Phase 1 commit packet generation."""

    rob_idx: RobIndex
    kind: str
    issued: bool = True
    exec_completed: bool = False
    store_commit_eligible: bool = False


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
        )


class RobAgent:
    """Drive MemBlock ROB/LSQ interface from a mem-only ROB proxy model."""

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
        }

    def reset(self) -> None:
        self.pending_ptr = RobIndex(flag=0, value=0)
        self.pending_ptr_next = self.pending_ptr
        self._entries: deque[RobEntry] = deque()
        self._orphan_load_completions: defaultdict[RobIndex, int] = defaultdict(int)
        self._queued_store_tokens = 0
        self._active_store_tokens = 0
        self._prepared_for_cycle = False
        self.latest_commit_packet = RobCommitPacket.empty(self.pending_ptr)
        self.driven_lcommit_count = 0
        self.driven_scommit_count = 0
        self.commit_cycle_count = 0

    def note_load_issued(self, rob_idx_flag: int, rob_idx_value: int) -> None:
        rob_idx = RobIndex(flag=int(rob_idx_flag), value=int(rob_idx_value))
        entry = RobEntry(rob_idx=rob_idx, kind="load")
        if self._orphan_load_completions[rob_idx] > 0:
            entry.exec_completed = True
            self._orphan_load_completions[rob_idx] -= 1
            if self._orphan_load_completions[rob_idx] == 0:
                del self._orphan_load_completions[rob_idx]
        self._entries.append(entry)

    def note_store_allocated(self, rob_idx_flag: int, rob_idx_value: int) -> None:
        rob_idx = RobIndex(flag=int(rob_idx_flag), value=int(rob_idx_value))
        self._entries.append(RobEntry(rob_idx=rob_idx, kind="store"))

    def note_load_completed(self, rob_idx_flag: int, rob_idx_value: int) -> None:
        rob_idx = RobIndex(flag=int(rob_idx_flag), value=int(rob_idx_value))
        for entry in self._entries:
            if entry.kind == "load" and entry.rob_idx == rob_idx and not entry.exec_completed:
                entry.exec_completed = True
                return
        self._orphan_load_completions[rob_idx] += 1

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
            if self._entries:
                self._entries.popleft()
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

        for entry in self._entries:
            if entry.kind == "load":
                if not entry.exec_completed:
                    break
                lcommit += 1
            elif entry.kind == "store":
                if remaining_store_tokens <= 0:
                    break
                remaining_store_tokens -= 1
                scommit += 1
            else:
                break
            committed.append(entry)

        commit_count = len(committed)
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
        )

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
