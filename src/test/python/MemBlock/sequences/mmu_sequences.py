# coding=utf-8
"""
MemBlock MMU-oriented reusable sequences.
"""

from __future__ import annotations

from collections import deque
from contextlib import contextmanager
from dataclasses import dataclass

from transactions import LoadTxn, ptr_inc
from transactions import BackendSendPlan, EnqueueLoadStep, IssueCyclePlan, IssueOp
from request_apis import send_load

from .memblock_sequences import (
    SequenceState,
    _resolve_replay_drain_cycles,
    _sample_l2_tlb_port,
    _sample_tlb_debug,
    _snapshot_transport_stats,
    _wait_completed_load_count,
)


SV39_PAGE_SIZE_4K = 1 << 12
SV39_PAGE_SIZE_NAME_4K = "4k"


def _normalize_sv39_page_size(page_size: str | int) -> int:
    if isinstance(page_size, str):
        normalized = page_size.strip().lower()
        aliases = {
            "4k": SV39_PAGE_SIZE_4K,
            "4kb": SV39_PAGE_SIZE_4K,
            "4096": SV39_PAGE_SIZE_4K,
        }
        if normalized not in aliases:
            raise ValueError(f"未知 Sv39 page size: {page_size}")
        return aliases[normalized]
    value = int(page_size)
    if value != SV39_PAGE_SIZE_4K:
        raise ValueError(f"未知 Sv39 page size: {page_size}")
    return value


def _resolve_sv39_pa(va: int, *, pa_base: int, page_size: str | int = SV39_PAGE_SIZE_NAME_4K) -> int:
    page_size_bytes = _normalize_sv39_page_size(page_size)
    return int(pa_base) | (int(va) & (page_size_bytes - 1))


SV39_PTE_V = 1 << 0
SV39_PTE_R = 1 << 1
SV39_PTE_W = 1 << 2
SV39_PTE_X = 1 << 3
SV39_PTE_A = 1 << 6
SV39_PTE_D = 1 << 7

LOAD_ACCESS_FAULT_BIT = 5
LOAD_PAGE_FAULT_BIT = 13
LOAD_GUEST_PAGE_FAULT_BIT = 21

PMP_MODE_ALLOW = "allow"
PMP_MODE_DENY = "deny"

PTE_MODE_NORMAL = "normal"
PTE_MODE_PAGE_FAULT = "page_fault"


@dataclass(frozen=True)
class Sv39Mapping:
    va: int
    pa_base: int
    page_size: str | int = SV39_PAGE_SIZE_NAME_4K
    pbmt: str | int = "cacheable"


@dataclass(frozen=True)
class U64MemoryPreload:
    addr: int
    data: int


@dataclass(frozen=True)
class TranslatedU64MemoryPreload:
    va: int
    data: int


@dataclass(frozen=True)
class MmuSv39AddressSpaceConfig:
    root_pt_addr: int
    mappings: tuple[Sv39Mapping, ...]
    page_table_page_addrs: tuple[int, ...] = ()
    translated_preloads: tuple[TranslatedU64MemoryPreload, ...] = ()
    bare_preloads: tuple[U64MemoryPreload, ...] = ()


@dataclass(frozen=True)
class MmuSv39AddressSpaceInstallSequenceResult:
    root_pt_addr: int
    mappings: tuple[Sv39Mapping, ...]
    page_table_page_addrs: tuple[int, ...]
    translated_preloads: tuple[U64MemoryPreload, ...]
    bare_preloads: tuple[U64MemoryPreload, ...]

    def translated_pa_for(self, va: int) -> int:
        for mapping in self.mappings:
            page_size_bytes = _normalize_sv39_page_size(mapping.page_size)
            if (int(mapping.va) >> 12) == (int(va) >> 12):
                return _resolve_sv39_pa(int(va), pa_base=int(mapping.pa_base), page_size=page_size_bytes)
        raise KeyError(f"地址空间 0x{self.root_pt_addr:x} 未配置 va=0x{int(va):x} 的 Sv39 4KB mapping")


@dataclass(frozen=True)
class GStageSv39Mapping:
    gpa: int
    pa_base: int
    page_size: str | int = SV39_PAGE_SIZE_NAME_4K
    pbmt: str | int = "cacheable"


@dataclass(frozen=True)
class TwoStageSv39AddressSpaceConfig:
    vs_root_pt_addr: int
    g_root_pt_addr: int
    vs_mappings: tuple[Sv39Mapping, ...]
    g_mappings: tuple[GStageSv39Mapping, ...]
    vs_page_table_page_addrs: tuple[int, ...] = ()
    g_page_table_page_addrs: tuple[int, ...] = ()
    translated_preloads: tuple[TranslatedU64MemoryPreload, ...] = ()
    bare_preloads: tuple[U64MemoryPreload, ...] = ()


@dataclass(frozen=True)
class InstallSequenceResult:
    vs_root_pt_addr: int
    g_root_pt_addr: int
    vs_mappings: tuple[Sv39Mapping, ...]
    g_mappings: tuple[GStageSv39Mapping, ...]
    vs_page_table_page_addrs: tuple[int, ...]
    g_page_table_page_addrs: tuple[int, ...]
    translated_preloads: tuple[U64MemoryPreload, ...]
    bare_preloads: tuple[U64MemoryPreload, ...]

    @staticmethod
    def _resolve_mapping(addr: int, mappings: tuple, *, addr_attr: str, pa_attr: str, label: str) -> int:
        for mapping in mappings:
            page_size_bytes = _normalize_sv39_page_size(getattr(mapping, "page_size"))
            base_addr = int(getattr(mapping, addr_attr))
            if (base_addr >> 12) == (int(addr) >> 12):
                return _resolve_sv39_pa(int(addr), pa_base=int(getattr(mapping, pa_attr)), page_size=page_size_bytes)
        raise KeyError(f"{label} 未配置 addr=0x{int(addr):x} 的 Sv39 4KB mapping")

    def resolve_vs_stage_gpa(self, va: int) -> int:
        return self._resolve_mapping(int(va), self.vs_mappings, addr_attr="va", pa_attr="pa_base", label="VS-stage")

    def resolve_g_stage_pa(self, gpa: int) -> int:
        return self._resolve_mapping(int(gpa), self.g_mappings, addr_attr="gpa", pa_attr="pa_base", label="G-stage")

    def resolve_two_stage_pa(self, va: int) -> int:
        return self.resolve_g_stage_pa(self.resolve_vs_stage_gpa(int(va)))


@dataclass(frozen=True)
class TwoStageLoadAccessResult:
    txn: LoadTxn
    va: int
    expected_gpa: int
    expected_pa: int
    writeback: dict
    ptw_trace: tuple[dict, ...]
    transport_stats_before: dict[str, int]
    transport_stats_after: dict[str, int]
    fault_event: dict | None = None

    @property
    def miss_observed(self) -> bool:
        return bool(self.ptw_trace)


@dataclass(frozen=True)
class MmuTwoStageLoadSequenceResult:
    final_state: SequenceState
    install_result: InstallSequenceResult
    accesses: tuple[TwoStageLoadAccessResult, ...]


@dataclass(frozen=True)
class MmuTwoStageFenceSequenceResult:
    final_state: SequenceState
    install_result: InstallSequenceResult
    warmup_access: TwoStageLoadAccessResult
    reprobe_access: TwoStageLoadAccessResult


@dataclass(frozen=True)
class MmuTwoStageFaultSequenceResult:
    final_state: SequenceState
    install_result: InstallSequenceResult
    access: TwoStageLoadAccessResult


@dataclass(frozen=True)
class MmuPrimeLoadSpec:
    req_id: int
    va: int
    expected_pa: int
    expected_data: int


@dataclass(frozen=True)
class MmuSv39ActivateSequenceResult:
    final_state: SequenceState
    completed_load_count: int
    prime_loads: tuple[LoadTxn, ...]
    prime_writebacks: tuple[dict, ...]


@dataclass(frozen=True)
class MmuDtlbPageSpec:
    page_va: int
    pa_base: int
    data: int
    offset: int = 0x8
    pbmt: str | int = "cacheable"

    @property
    def load_va(self) -> int:
        return int(self.page_va) + int(self.offset)

    @property
    def expected_pa(self) -> int:
        return int(self.pa_base) + int(self.offset)


@dataclass(frozen=True)
class MmuDtlbProbeEvent:
    cycle: int
    tlb_debug: dict[str, int | None]
    l2_tlb_port: dict[str, int | None]
    first_miss_events: tuple[dict[str, int], ...]


@dataclass(frozen=True)
class MmuDtlbAccessResult:
    txn: LoadTxn
    va: int
    expected_pa: int
    expected_data: int
    writeback: dict
    ptw_trace: tuple[dict, ...]
    probe_events: tuple[MmuDtlbProbeEvent, ...]
    ptw_a_fire_count: int
    ptw_d_fire_count: int
    tlb_first_miss_seen: bool
    l2_tlb_req_seen: bool
    first_l2_tlb_req: dict[str, int | None] | None

    @property
    def miss_observed(self) -> bool:
        return bool(
            self.tlb_first_miss_seen
            or self.l2_tlb_req_seen
            or self.ptw_a_fire_count
            or self.ptw_d_fire_count
        )


@dataclass(frozen=True)
class MmuDtlbReplacementSequenceResult:
    final_state: SequenceState
    completed_load_count: int
    dtlb_capacity: int
    install_result: MmuSv39AddressSpaceInstallSequenceResult
    prime_accesses: tuple[MmuDtlbAccessResult, ...]
    rehit_accesses: tuple[MmuDtlbAccessResult, ...]
    overflow_accesses: tuple[MmuDtlbAccessResult, ...]
    reprobe_accesses: tuple[MmuDtlbAccessResult, ...]
    reprobe_access: MmuDtlbAccessResult


@dataclass(frozen=True)
class MmuFaultingScalarLoadSequenceResult:
    final_state: SequenceState
    prime_txn: LoadTxn | None
    prime_writeback: dict | None
    main_txn: LoadTxn
    main_writeback: dict
    prime_ptw_trace: tuple[dict, ...]
    main_ptw_trace: tuple[dict, ...]
    transport_stats_before_main: dict[str, int]
    transport_stats_after_main: dict[str, int]


@dataclass(frozen=True)
class MmuPmpRegionLoadRecoverySequenceResult:
    final_state: SequenceState
    prime_txn: LoadTxn
    prime_writeback: dict
    denied_txn: LoadTxn
    denied_writeback: dict
    allowed_txn: LoadTxn
    allowed_writeback: dict
    restored_txn: LoadTxn
    restored_writeback: dict
    prime_ptw_trace: tuple[dict, ...]
    denied_ptw_trace: tuple[dict, ...]
    allowed_ptw_trace: tuple[dict, ...]
    restored_ptw_trace: tuple[dict, ...]
    transport_stats_before_denied: dict[str, int]
    transport_stats_after_denied: dict[str, int]
    denied_replay_state: dict


class MmuSv39AddressSpaceInstallSequence:
    def __init__(self, config: MmuSv39AddressSpaceConfig) -> None:
        self.config = config

    def run(self, env) -> MmuSv39AddressSpaceInstallSequenceResult:
        mappings = tuple(
            Sv39Mapping(
                va=int(mapping.va),
                pa_base=int(mapping.pa_base),
                page_size=_normalize_sv39_page_size(mapping.page_size),
                pbmt=mapping.pbmt,
            )
            for mapping in self.config.mappings
        )
        result = MmuSv39AddressSpaceInstallSequenceResult(
            root_pt_addr=int(self.config.root_pt_addr),
            mappings=mappings,
            page_table_page_addrs=tuple(int(addr) for addr in self.config.page_table_page_addrs),
            translated_preloads=(),
            bare_preloads=tuple(
                U64MemoryPreload(addr=int(preload.addr), data=int(preload.data))
                for preload in self.config.bare_preloads
            ),
        )

        next_page_table_idx = 0
        for mapping in mappings:
            install_result = env.mmu.install_sv39_mapping(
                root_pt_addr=result.root_pt_addr,
                va=mapping.va,
                pa_base=mapping.pa_base,
                page_size=mapping.page_size,
                pbmt=mapping.pbmt,
                page_table_page_addrs=result.page_table_page_addrs[next_page_table_idx:],
            )
            next_page_table_idx += len(install_result["allocated_table_addrs"])

        translated_preloads = []
        for preload in self.config.translated_preloads:
            translated_pa = result.translated_pa_for(preload.va)
            env.preload_u64(translated_pa, preload.data)
            translated_preloads.append(U64MemoryPreload(addr=translated_pa, data=int(preload.data)))

        for preload in result.bare_preloads:
            env.preload_u64(preload.addr, preload.data)

        return MmuSv39AddressSpaceInstallSequenceResult(
            root_pt_addr=result.root_pt_addr,
            mappings=result.mappings,
            page_table_page_addrs=result.page_table_page_addrs,
            translated_preloads=tuple(translated_preloads),
            bare_preloads=result.bare_preloads,
        )


class MmuSv39ActivateSequence:
    def __init__(
        self,
        *,
        root_pt_addr: int,
        initial_state: SequenceState,
        prime_loads: tuple[MmuPrimeLoadSpec, ...] = (),
        settle_cycles: int = 4,
    ) -> None:
        self.root_pt_addr = int(root_pt_addr)
        self.initial_state = initial_state
        self.prime_loads = tuple(prime_loads)
        self.settle_cycles = int(settle_cycles)

    def run(self, env) -> MmuSv39ActivateSequenceResult:
        next_lq_ptr = self.initial_state.next_lq_ptr
        completed_loads = env.get_completed_load_count()
        prime_txns = []
        prime_writebacks = []

        env.mmu.enable_sv39(root_pt_addr=self.root_pt_addr, settle_cycles=self.settle_cycles)

        for spec in self.prime_loads:
            txn = LoadTxn(
                req_id=spec.req_id,
                addr=spec.va,
                lq_ptr=next_lq_ptr,
                sq_ptr=self.initial_state.sq_ptr,
            )
            env.backend.prepare(txn)
            env.expect_scalar_load(
                rob_idx=txn.rob_idx,
                pdest=txn.resolved_pdest,
                addr=spec.expected_pa,
                size=txn.size,
                mask=txn.mask,
            )
            env.backend.execute(
                BackendSendPlan.from_steps(
                    EnqueueLoadStep.from_txn(txn),
                    IssueCyclePlan.from_ops(IssueOp.load_from_txn(txn)),
                )
            )
            writeback = env.wait_load_writeback_observed(
                rob_idx=txn.rob_idx,
                data=spec.expected_data,
                max_cycles=_resolve_replay_drain_cycles(env, None),
            )
            completed_loads = _wait_completed_load_count(
                env,
                completed_loads + 1,
                max_cycles=_resolve_replay_drain_cycles(env, None),
            )
            prime_txns.append(txn)
            prime_writebacks.append(writeback)
            next_lq_ptr = ptr_inc(next_lq_ptr, env.config.sequence.load_queue_size)

        return MmuSv39ActivateSequenceResult(
            final_state=SequenceState(next_lq_ptr=next_lq_ptr, sq_ptr=self.initial_state.sq_ptr),
            completed_load_count=completed_loads,
            prime_loads=tuple(prime_txns),
            prime_writebacks=tuple(prime_writebacks),
        )


class TwoStageSv39AddressSpaceInstallSequence:
    def __init__(self, config: TwoStageSv39AddressSpaceConfig) -> None:
        self.config = config

    def run(self, env) -> InstallSequenceResult:
        vs_mappings = tuple(
            Sv39Mapping(
                va=int(mapping.va),
                pa_base=int(mapping.pa_base),
                page_size=_normalize_sv39_page_size(mapping.page_size),
                pbmt=mapping.pbmt,
            )
            for mapping in self.config.vs_mappings
        )
        g_mappings = tuple(
            GStageSv39Mapping(
                gpa=int(mapping.gpa),
                pa_base=int(mapping.pa_base),
                page_size=_normalize_sv39_page_size(mapping.page_size),
                pbmt=mapping.pbmt,
            )
            for mapping in self.config.g_mappings
        )

        vs_install_result = MmuSv39AddressSpaceInstallSequence(
            MmuSv39AddressSpaceConfig(
                root_pt_addr=int(self.config.vs_root_pt_addr),
                mappings=vs_mappings,
                page_table_page_addrs=tuple(int(addr) for addr in self.config.vs_page_table_page_addrs),
            )
        ).run(env)

        next_g_page_table_idx = 0
        for mapping in g_mappings:
            install_result = env.mmu.install_g_sv39_mapping(
                root_pt_addr=int(self.config.g_root_pt_addr),
                gpa=int(mapping.gpa),
                pa_base=int(mapping.pa_base),
                page_size=mapping.page_size,
                pbmt=mapping.pbmt,
                page_table_page_addrs=tuple(int(addr) for addr in self.config.g_page_table_page_addrs)[
                    next_g_page_table_idx:
                ],
            )
            next_g_page_table_idx += len(install_result["allocated_table_addrs"])

        result = InstallSequenceResult(
            vs_root_pt_addr=int(self.config.vs_root_pt_addr),
            g_root_pt_addr=int(self.config.g_root_pt_addr),
            vs_mappings=vs_install_result.mappings,
            g_mappings=g_mappings,
            vs_page_table_page_addrs=tuple(int(addr) for addr in self.config.vs_page_table_page_addrs),
            g_page_table_page_addrs=tuple(int(addr) for addr in self.config.g_page_table_page_addrs),
            translated_preloads=(),
            bare_preloads=tuple(
                U64MemoryPreload(addr=int(preload.addr), data=int(preload.data))
                for preload in self.config.bare_preloads
            ),
        )

        translated_preloads = []
        for preload in self.config.translated_preloads:
            translated_pa = result.resolve_two_stage_pa(int(preload.va))
            env.preload_u64(translated_pa, int(preload.data))
            translated_preloads.append(U64MemoryPreload(addr=translated_pa, data=int(preload.data)))

        for preload in result.bare_preloads:
            env.preload_u64(preload.addr, preload.data)

        return InstallSequenceResult(
            vs_root_pt_addr=result.vs_root_pt_addr,
            g_root_pt_addr=result.g_root_pt_addr,
            vs_mappings=result.vs_mappings,
            g_mappings=result.g_mappings,
            vs_page_table_page_addrs=result.vs_page_table_page_addrs,
            g_page_table_page_addrs=result.g_page_table_page_addrs,
            translated_preloads=tuple(translated_preloads),
            bare_preloads=result.bare_preloads,
        )


class MmuVsStageLoadSequence:
    def __init__(
        self,
        *,
        root_pt_addr: int,
        asid: int,
        va: int,
        gpa_base: int,
        page_table_page_addrs: tuple[int, ...],
        initial_state: SequenceState,
        req_id: int,
        expected_data: int,
        settle_cycles: int = 4,
        response_delay_cycles: int = 1,
    ) -> None:
        self.root_pt_addr = int(root_pt_addr)
        self.asid = int(asid)
        self.va = int(va)
        self.gpa_base = int(gpa_base)
        self.page_table_page_addrs = tuple(int(addr) for addr in page_table_page_addrs)
        self.initial_state = initial_state
        self.req_id = int(req_id)
        self.expected_data = int(expected_data)
        self.settle_cycles = int(settle_cycles)
        self.response_delay_cycles = int(response_delay_cycles)

    def run(self, env) -> MmuTwoStageLoadSequenceResult:
        vs_install_result = MmuSv39AddressSpaceInstallSequence(
            MmuSv39AddressSpaceConfig(
                root_pt_addr=self.root_pt_addr,
                mappings=(Sv39Mapping(va=self.va & ~(SV39_PAGE_SIZE_4K - 1), pa_base=self.gpa_base),),
                page_table_page_addrs=self.page_table_page_addrs,
            )
        ).run(env)
        translated_pa = vs_install_result.translated_pa_for(self.va)
        env.preload_u64(translated_pa, self.expected_data)
        install_result = InstallSequenceResult(
            vs_root_pt_addr=self.root_pt_addr,
            g_root_pt_addr=0,
            vs_mappings=vs_install_result.mappings,
            g_mappings=(),
            vs_page_table_page_addrs=self.page_table_page_addrs,
            g_page_table_page_addrs=(),
            translated_preloads=(U64MemoryPreload(addr=translated_pa, data=self.expected_data),),
            bare_preloads=(),
        )
        env.mmu.allow_all_smode_access(persistent=False)
        with env.mmu.ptw_responder(response_delay_cycles=self.response_delay_cycles) as ptw:
            env.mmu.enable_vs_sv39(root_pt_addr=self.root_pt_addr, asid=self.asid, settle_cycles=self.settle_cycles)
            access = _run_one_two_stage_access(
                env,
                ptw=ptw,
                install_result=install_result,
                va=self.va,
                lq_ptr=self.initial_state.next_lq_ptr,
                sq_ptr=self.initial_state.sq_ptr,
                req_id=self.req_id,
                expected_data=self.expected_data,
                two_stage=False,
            )
        return MmuTwoStageLoadSequenceResult(
            final_state=SequenceState(
                next_lq_ptr=ptr_inc(self.initial_state.next_lq_ptr, env.config.sequence.load_queue_size),
                sq_ptr=self.initial_state.sq_ptr,
            ),
            install_result=install_result,
            accesses=(access,),
        )


def _run_one_two_stage_access(
    env,
    *,
    ptw,
    install_result: InstallSequenceResult,
    va: int,
    lq_ptr,
    sq_ptr,
    req_id: int,
    expected_data: int | None,
    required_exception_bits: tuple[int, ...] = (),
    two_stage: bool = True,
) -> TwoStageLoadAccessResult:
    trace_start = len(ptw.trace)
    transport_stats_before = _snapshot_transport_stats(env)
    txn = LoadTxn(req_id=int(req_id), addr=int(va), lq_ptr=lq_ptr, sq_ptr=sq_ptr)
    env.backend.prepare(txn)
    expected_gpa = install_result.resolve_vs_stage_gpa(int(va))
    expected_pa = install_result.resolve_two_stage_pa(int(va)) if two_stage else expected_gpa

    if expected_data is not None:
        completed_before = env.get_completed_load_count()
        env.expect_scalar_load(
            rob_idx=txn.rob_idx,
            pdest=txn.resolved_pdest,
            addr=expected_pa,
            size=txn.size,
            mask=txn.mask,
        )
        env.backend.execute(
            BackendSendPlan.from_steps(
                EnqueueLoadStep.from_txn(txn),
                IssueCyclePlan.from_ops(IssueOp.load_from_txn(txn)),
            )
        )
        writeback = env.wait_load_writeback_observed(
            rob_idx=txn.rob_idx,
            max_cycles=_resolve_replay_drain_cycles(env, None),
        )
        _wait_completed_load_count(
            env,
            completed_before + 1,
            max_cycles=_resolve_replay_drain_cycles(env, None),
        )
        fault_event = None
    else:
        previous_strict = env.memory.strict_writeback_check
        env.memory.strict_writeback_check = False
        try:
            env.backend.execute(
                BackendSendPlan.from_steps(
                    EnqueueLoadStep.from_txn(txn),
                    IssueCyclePlan.from_ops(IssueOp.load_from_txn(txn)),
                )
            )
            fault_event = env.wait_load_fault_observed(
                rob_idx=txn.rob_idx,
                required_exception_bits=required_exception_bits,
                max_cycles=_resolve_replay_drain_cycles(env, None),
            )
            env.drain_writebacks(max_cycles=_resolve_replay_drain_cycles(env, None))
            writeback = fault_event
        finally:
            env.memory.strict_writeback_check = previous_strict

    return TwoStageLoadAccessResult(
        txn=txn,
        va=int(va),
        expected_gpa=expected_gpa,
        expected_pa=expected_pa,
        writeback=writeback,
        ptw_trace=tuple(list(ptw.trace)[trace_start:]),
        transport_stats_before=transport_stats_before,
        transport_stats_after=_snapshot_transport_stats(env),
        fault_event=fault_event,
    )


class MmuTwoStageLoadSequence:
    def __init__(
        self,
        *,
        config: TwoStageSv39AddressSpaceConfig,
        va: int,
        initial_state: SequenceState,
        req_id_base: int = 0,
        expected_data: int,
        repeat_count: int = 1,
        vs_asid: int = 0,
        vmid: int = 0,
        settle_cycles: int = 4,
        response_delay_cycles: int = 1,
    ) -> None:
        self.config = config
        self.va = int(va)
        self.initial_state = initial_state
        self.req_id_base = int(req_id_base)
        self.expected_data = int(expected_data)
        self.repeat_count = int(repeat_count)
        self.vs_asid = int(vs_asid)
        self.vmid = int(vmid)
        self.settle_cycles = int(settle_cycles)
        self.response_delay_cycles = int(response_delay_cycles)

    def run(self, env) -> MmuTwoStageLoadSequenceResult:
        install_result = TwoStageSv39AddressSpaceInstallSequence(self.config).run(env)
        next_lq_ptr = self.initial_state.next_lq_ptr
        accesses = []

        env.mmu.allow_all_smode_access(persistent=False)
        with env.mmu.ptw_responder(response_delay_cycles=self.response_delay_cycles) as ptw:
            env.mmu.enable_two_stage_sv39(
                vs_root_pt_addr=int(self.config.vs_root_pt_addr),
                g_root_pt_addr=int(self.config.g_root_pt_addr),
                vs_asid=self.vs_asid,
                vmid=self.vmid,
                settle_cycles=self.settle_cycles,
            )
            for index in range(self.repeat_count):
                access = _run_one_two_stage_access(
                    env,
                    ptw=ptw,
                    install_result=install_result,
                    va=self.va,
                    lq_ptr=next_lq_ptr,
                    sq_ptr=self.initial_state.sq_ptr,
                    req_id=self.req_id_base + index,
                    expected_data=self.expected_data,
                )
                accesses.append(access)
                next_lq_ptr = ptr_inc(next_lq_ptr, env.config.sequence.load_queue_size)

        return MmuTwoStageLoadSequenceResult(
            final_state=SequenceState(next_lq_ptr=next_lq_ptr, sq_ptr=self.initial_state.sq_ptr),
            install_result=install_result,
            accesses=tuple(accesses),
        )


class MmuTwoStageFenceSequence:
    def __init__(
        self,
        *,
        config: TwoStageSv39AddressSpaceConfig,
        va: int,
        initial_state: SequenceState,
        req_id_base: int = 0,
        expected_data: int,
        fence_kind: str,
        vs_asid: int = 0,
        vmid: int = 0,
        settle_cycles: int = 4,
        response_delay_cycles: int = 1,
        fence_specific_id: int | None = None,
    ) -> None:
        self.config = config
        self.va = int(va)
        self.initial_state = initial_state
        self.req_id_base = int(req_id_base)
        self.expected_data = int(expected_data)
        self.fence_kind = str(fence_kind)
        self.vs_asid = int(vs_asid)
        self.vmid = int(vmid)
        self.settle_cycles = int(settle_cycles)
        self.response_delay_cycles = int(response_delay_cycles)
        self.fence_specific_id = None if fence_specific_id is None else int(fence_specific_id)

    def run(self, env) -> MmuTwoStageFenceSequenceResult:
        load_result = MmuTwoStageLoadSequence(
            config=self.config,
            va=self.va,
            initial_state=self.initial_state,
            req_id_base=self.req_id_base,
            expected_data=self.expected_data,
            repeat_count=1,
            vs_asid=self.vs_asid,
            vmid=self.vmid,
            settle_cycles=self.settle_cycles,
            response_delay_cycles=self.response_delay_cycles,
        ).run(env)

        warmup_access = load_result.accesses[0]
        with env.mmu.ptw_responder(response_delay_cycles=self.response_delay_cycles) as ptw:
            if self.fence_kind == "hfence_vvma":
                env.mmu.pulse_hfence_vvma(
                    rs2=self.fence_specific_id is not None,
                    asid=0 if self.fence_specific_id is None else self.fence_specific_id,
                )
            elif self.fence_kind == "hfence_gvma":
                env.mmu.pulse_hfence_gvma(
                    rs2=self.fence_specific_id is not None,
                    vmid=0 if self.fence_specific_id is None else self.fence_specific_id,
                )
            else:
                raise ValueError(f"未知 two-stage fence kind: {self.fence_kind}")
            reprobe_access = _run_one_two_stage_access(
                env,
                ptw=ptw,
                install_result=load_result.install_result,
                va=self.va,
                lq_ptr=load_result.final_state.next_lq_ptr,
                sq_ptr=load_result.final_state.sq_ptr,
                req_id=self.req_id_base + 1,
                expected_data=self.expected_data,
            )

        return MmuTwoStageFenceSequenceResult(
            final_state=SequenceState(
                next_lq_ptr=ptr_inc(load_result.final_state.next_lq_ptr, env.config.sequence.load_queue_size),
                sq_ptr=load_result.final_state.sq_ptr,
            ),
            install_result=load_result.install_result,
            warmup_access=warmup_access,
            reprobe_access=reprobe_access,
        )


class MmuTwoStageFaultSequence:
    def __init__(
        self,
        *,
        config: TwoStageSv39AddressSpaceConfig,
        va: int,
        initial_state: SequenceState,
        req_id: int,
        vs_asid: int = 0,
        vmid: int = 0,
        settle_cycles: int = 4,
        response_delay_cycles: int = 1,
        required_exception_bits: tuple[int, ...] = (),
        pmp_mode: str = PMP_MODE_ALLOW,
        fault_stage: str | None = None,
    ) -> None:
        self.config = config
        self.va = int(va)
        self.initial_state = initial_state
        self.req_id = int(req_id)
        self.vs_asid = int(vs_asid)
        self.vmid = int(vmid)
        self.settle_cycles = int(settle_cycles)
        self.response_delay_cycles = int(response_delay_cycles)
        self.required_exception_bits = tuple(int(bit) for bit in required_exception_bits)
        self.pmp_mode = str(pmp_mode)
        self.fault_stage = None if fault_stage is None else str(fault_stage)

    def run(self, env) -> MmuTwoStageFaultSequenceResult:
        install_result = TwoStageSv39AddressSpaceInstallSequence(self.config).run(env)
        if self.fault_stage is not None:
            if self.fault_stage == "vs":
                for mapping in self.config.vs_mappings:
                    if (int(mapping.va) >> 12) != (self.va >> 12):
                        continue
                    leaf_pte_addr = env.mmu.install_vs_sv39_mapping(
                        root_pt_addr=int(self.config.vs_root_pt_addr),
                        va=int(mapping.va),
                        gpa_base=int(mapping.pa_base),
                        page_size=mapping.page_size,
                        pbmt=mapping.pbmt,
                        page_table_page_addrs=tuple(int(addr) for addr in self.config.vs_page_table_page_addrs),
                    )["leaf_pte_addr"]
                    env.preload_u64(int(leaf_pte_addr), 0)
                    break
                else:
                    raise KeyError(f"未找到 va=0x{self.va:x} 对应的 VS-stage mapping")
            elif self.fault_stage == "g":
                target_gpa = install_result.resolve_vs_stage_gpa(self.va)
                for mapping in self.config.g_mappings:
                    if (int(mapping.gpa) >> 12) != (target_gpa >> 12):
                        continue
                    leaf_pte_addr = env.mmu.install_g_sv39_mapping(
                        root_pt_addr=int(self.config.g_root_pt_addr),
                        gpa=int(mapping.gpa),
                        pa_base=int(mapping.pa_base),
                        page_size=mapping.page_size,
                        pbmt=mapping.pbmt,
                        page_table_page_addrs=tuple(int(addr) for addr in self.config.g_page_table_page_addrs),
                    )["leaf_pte_addr"]
                    env.preload_u64(int(leaf_pte_addr), 0)
                    break
                else:
                    raise KeyError(f"未找到 gpa=0x{target_gpa:x} 对应的 G-stage mapping")
            else:
                raise ValueError(f"未知 two-stage fault_stage: {self.fault_stage}")

        if self.pmp_mode == PMP_MODE_ALLOW:
            env.mmu.allow_all_smode_access(persistent=False)
        elif self.pmp_mode == PMP_MODE_DENY:
            env.mmu.program_pmp_entry(index=0, cfg=0, addr=0, persistent=False)
        else:
            raise ValueError(f"未知 PMP mode: {self.pmp_mode}")

        with env.mmu.ptw_responder(response_delay_cycles=self.response_delay_cycles) as ptw:
            env.mmu.enable_two_stage_sv39(
                vs_root_pt_addr=int(self.config.vs_root_pt_addr),
                g_root_pt_addr=int(self.config.g_root_pt_addr),
                vs_asid=self.vs_asid,
                vmid=self.vmid,
                settle_cycles=self.settle_cycles,
            )
            access = _run_one_two_stage_access(
                env,
                ptw=ptw,
                install_result=install_result,
                va=self.va,
                lq_ptr=self.initial_state.next_lq_ptr,
                sq_ptr=self.initial_state.sq_ptr,
                req_id=self.req_id,
                expected_data=None,
                required_exception_bits=self.required_exception_bits,
            )

        return MmuTwoStageFaultSequenceResult(
            final_state=SequenceState(
                next_lq_ptr=ptr_inc(self.initial_state.next_lq_ptr, env.config.sequence.load_queue_size),
                sq_ptr=self.initial_state.sq_ptr,
            ),
            install_result=install_result,
            access=access,
        )


def _sample_tlb_first_miss_events(env) -> tuple[dict[str, int], ...]:
    events = []
    for lane in range(env.config.load_pipeline_width):
        prefix = f"io_debug_ls_debugLsInfo_{lane}"
        first_miss = env._read_optional_dut_signal(f"{prefix}_s1_isTlbFirstMiss", None)
        if first_miss in (None, 0):
            continue
        events.append(
            {
                "lane": lane,
                "s1_rob_idx": env._read_optional_dut_signal(f"{prefix}_s1_robIdx", -1),
                "s2_rob_idx": env._read_optional_dut_signal(f"{prefix}_s2_robIdx", -1),
                "s3_rob_idx": env._read_optional_dut_signal(f"{prefix}_s3_robIdx", -1),
            }
        )
    return tuple(events)


def _tlb_probe_has_activity(
    tlb_debug: dict[str, int | None],
    l2_tlb_port: dict[str, int | None],
    first_miss_events: tuple[dict[str, int], ...],
) -> bool:
    if first_miss_events:
        return True
    if any(value not in (None, 0) for value in tlb_debug.values()):
        return True
    return any(
        l2_tlb_port.get(signal_name) not in (None, 0)
        for signal_name in ("req_valid", "resp_valid", "resp_miss")
    )


@contextmanager
def _capture_tlb_probe_events(env, *, max_events: int = 64):
    trace = deque(maxlen=max(1, int(max_events)))

    def _sample() -> None:
        tlb_debug = _sample_tlb_debug(env)
        l2_tlb_port = _sample_l2_tlb_port(env)
        first_miss_events = _sample_tlb_first_miss_events(env)
        if not _tlb_probe_has_activity(tlb_debug, l2_tlb_port, first_miss_events):
            return
        trace.append(
            MmuDtlbProbeEvent(
                cycle=env._current_cycle(),
                tlb_debug=dict(tlb_debug),
                l2_tlb_port=dict(l2_tlb_port),
                first_miss_events=tuple(dict(event) for event in first_miss_events),
            )
        )

    env.add_after_step_callback(_sample)
    try:
        yield trace
    finally:
        env.remove_after_step_callback(_sample)


class MmuDtlbReplacementSequence:
    def __init__(
        self,
        *,
        root_pt_addr: int,
        page_table_page_addrs: tuple[int, ...],
        page_specs: tuple[MmuDtlbPageSpec, ...],
        initial_state: SequenceState,
        dtlb_capacity: int = 4,
        settle_cycles: int = 4,
        response_delay_cycles: int = 1,
        max_cycles: int | None = None,
    ) -> None:
        self.root_pt_addr = int(root_pt_addr)
        self.page_table_page_addrs = tuple(int(addr) for addr in page_table_page_addrs)
        self.page_specs = tuple(page_specs)
        self.initial_state = initial_state
        self.dtlb_capacity = int(dtlb_capacity)
        self.settle_cycles = int(settle_cycles)
        self.response_delay_cycles = int(response_delay_cycles)
        self.max_cycles = max_cycles

        if self.dtlb_capacity < 1:
            raise ValueError("dtlb_capacity 必须大于 0")
        if len(self.page_specs) < self.dtlb_capacity + 1:
            raise ValueError("page_specs 至少需要包含 dtlb_capacity + 1 个不同页")

        for spec in self.page_specs:
            if int(spec.page_va) & (SV39_PAGE_SIZE_4K - 1):
                raise ValueError(f"DTLB page spec 需要 4KB 对齐虚拟页基址: 0x{int(spec.page_va):x}")
            if int(spec.pa_base) & (SV39_PAGE_SIZE_4K - 1):
                raise ValueError(f"DTLB page spec 需要 4KB 对齐物理页基址: 0x{int(spec.pa_base):x}")
            if not 0 <= int(spec.offset) < SV39_PAGE_SIZE_4K:
                raise ValueError(f"DTLB page spec offset 超出 4KB 页内范围: {int(spec.offset)}")

    def _run_one_access(self, env, *, ptw, spec: MmuDtlbPageSpec, lq_ptr, completed_loads: int, req_id: int):
        load_va = int(spec.load_va)
        expected_pa = int(spec.expected_pa)
        expected_data = int(spec.data)
        txn = LoadTxn(
            req_id=int(req_id),
            addr=load_va,
            lq_ptr=lq_ptr,
            sq_ptr=self.initial_state.sq_ptr,
        )
        max_cycles = _resolve_replay_drain_cycles(env, self.max_cycles)

        env.backend.prepare(txn)
        expected = env.expect_scalar_load(
            rob_idx=txn.rob_idx,
            pdest=txn.resolved_pdest,
            addr=expected_pa,
            size=txn.size,
            mask=txn.mask,
        )
        expected.expected_data = expected_data

        ptw_trace_start = len(ptw.trace)
        with _capture_tlb_probe_events(env) as probe_trace:
            send_load(env, txn)
            writeback = env.wait_load_writeback_observed(
                rob_idx=txn.rob_idx,
                data=expected_data,
                max_cycles=max_cycles,
            )

        completed_loads = _wait_completed_load_count(
            env,
            completed_loads + 1,
            max_cycles=max_cycles,
        )
        ptw_trace = tuple(list(ptw.trace)[ptw_trace_start:])
        probe_events = tuple(probe_trace)
        first_l2_tlb_req = next(
            (
                dict(event.l2_tlb_port)
                for event in probe_events
                if event.l2_tlb_port.get("req_valid") not in (None, 0)
            ),
            None,
        )

        return (
            MmuDtlbAccessResult(
                txn=txn,
                va=load_va,
                expected_pa=expected_pa,
                expected_data=expected_data,
                writeback=writeback,
                ptw_trace=ptw_trace,
                probe_events=probe_events,
                ptw_a_fire_count=sum(1 for event in ptw_trace if event.get("event") == "a_fire"),
                ptw_d_fire_count=sum(1 for event in ptw_trace if event.get("event") == "d_fire"),
                tlb_first_miss_seen=any(event.first_miss_events for event in probe_events),
                l2_tlb_req_seen=any(event.l2_tlb_port.get("req_valid") not in (None, 0) for event in probe_events),
                first_l2_tlb_req=first_l2_tlb_req,
            ),
            ptr_inc(lq_ptr, env.config.sequence.load_queue_size),
            completed_loads,
        )

    def run(self, env) -> MmuDtlbReplacementSequenceResult:
        install_sequence = MmuSv39AddressSpaceInstallSequence(
            MmuSv39AddressSpaceConfig(
                root_pt_addr=self.root_pt_addr,
                mappings=tuple(
                    Sv39Mapping(
                        va=int(spec.page_va),
                        pa_base=int(spec.pa_base),
                        pbmt=spec.pbmt,
                    )
                    for spec in self.page_specs
                ),
                page_table_page_addrs=self.page_table_page_addrs,
                translated_preloads=tuple(
                    TranslatedU64MemoryPreload(va=int(spec.load_va), data=int(spec.data))
                    for spec in self.page_specs
                ),
            )
        )
        install_result = install_sequence.run(env)
        env.mmu.allow_all_smode_access()

        next_lq_ptr = self.initial_state.next_lq_ptr
        completed_loads = env.get_completed_load_count()
        prime_accesses = []
        rehit_accesses = []
        overflow_accesses = []
        prime_specs = self.page_specs[:self.dtlb_capacity]
        overflow_specs = self.page_specs[self.dtlb_capacity:]

        with env.mmu.ptw_responder(response_delay_cycles=self.response_delay_cycles) as ptw:
            env.mmu.enable_sv39(root_pt_addr=self.root_pt_addr, settle_cycles=self.settle_cycles)

            for req_id, spec in enumerate(prime_specs):
                access_result, next_lq_ptr, completed_loads = self._run_one_access(
                    env,
                    ptw=ptw,
                    spec=spec,
                    lq_ptr=next_lq_ptr,
                    completed_loads=completed_loads,
                    req_id=req_id,
                )
                prime_accesses.append(access_result)

            for req_id, spec in enumerate(prime_specs, start=len(prime_specs)):
                access_result, next_lq_ptr, completed_loads = self._run_one_access(
                    env,
                    ptw=ptw,
                    spec=spec,
                    lq_ptr=next_lq_ptr,
                    completed_loads=completed_loads,
                    req_id=req_id,
                )
                rehit_accesses.append(access_result)

            next_req_id = 2 * len(prime_specs)
            for spec in overflow_specs:
                access_result, next_lq_ptr, completed_loads = self._run_one_access(
                    env,
                    ptw=ptw,
                    spec=spec,
                    lq_ptr=next_lq_ptr,
                    completed_loads=completed_loads,
                    req_id=next_req_id,
                )
                overflow_accesses.append(access_result)
                next_req_id += 1
            reprobe_accesses = []
            reprobe_access = None
            for spec in prime_specs:
                access_result, next_lq_ptr, completed_loads = self._run_one_access(
                    env,
                    ptw=ptw,
                    spec=spec,
                    lq_ptr=next_lq_ptr,
                    completed_loads=completed_loads,
                    req_id=next_req_id,
                )
                reprobe_accesses.append(access_result)
                next_req_id += 1
                if access_result.miss_observed:
                    reprobe_access = access_result
                    break

        if reprobe_access is None:
            raise AssertionError("DTLB 容量溢出后未观测到旧页重新 miss/refill，无法证明 replacement")

        return MmuDtlbReplacementSequenceResult(
            final_state=SequenceState(next_lq_ptr=next_lq_ptr, sq_ptr=self.initial_state.sq_ptr),
            completed_load_count=completed_loads,
            dtlb_capacity=self.dtlb_capacity,
            install_result=install_result,
            prime_accesses=tuple(prime_accesses),
            rehit_accesses=tuple(rehit_accesses),
            overflow_accesses=tuple(overflow_accesses),
            reprobe_accesses=tuple(reprobe_accesses),
            reprobe_access=reprobe_access,
        )


def _sv39_fault_leaf_pte(*, pa_base: int, pte_mode: str) -> int:
    if int(pa_base) & (SV39_PAGE_SIZE_4K - 1):
        raise ValueError(f"SV39 4KB leaf PTE 需要 4KB 对齐物理地址: 0x{int(pa_base):x}")
    if pte_mode == PTE_MODE_NORMAL:
        return (
            ((int(pa_base) >> 12) << 10)
            | SV39_PTE_V
            | SV39_PTE_R
            | SV39_PTE_W
            | SV39_PTE_A
            | SV39_PTE_D
        )
    if pte_mode == PTE_MODE_PAGE_FAULT:
        return 0
    raise ValueError(f"未知 PTE mode: {pte_mode}")


def _write_sv39_fault_mapping(
    env,
    *,
    root_pt_addr: int,
    va: int,
    pa_base: int,
    pte_mode: str,
    page_table_page_addrs: tuple[int, ...],
) -> None:
    install_result = env.mmu.install_sv39_mapping(
        root_pt_addr=int(root_pt_addr),
        va=int(va) & ~(SV39_PAGE_SIZE_4K - 1),
        pa_base=int(pa_base),
        page_size=SV39_PAGE_SIZE_NAME_4K,
        page_table_page_addrs=tuple(int(addr) for addr in page_table_page_addrs),
    )
    env.preload_u64(int(install_result["leaf_pte_addr"]), _sv39_fault_leaf_pte(pa_base=pa_base, pte_mode=pte_mode))


def _configure_pmp_mode(env, pmp_mode: str) -> None:
    if pmp_mode == PMP_MODE_ALLOW:
        env.mmu.allow_all_smode_access(persistent=False)
        return
    if pmp_mode == PMP_MODE_DENY:
        env.mmu.program_pmp_entry(index=0, cfg=0, addr=0, persistent=False)
        return
    raise ValueError(f"未知 PMP mode: {pmp_mode}")


def _wait_load_exception_writeback_observed(
    env,
    *,
    rob_idx_flag: int,
    rob_idx_value: int,
    required_exception_bits: tuple[int, ...] = (),
    max_cycles: int = 200,
) -> dict:
    required_bits = tuple(int(bit) for bit in required_exception_bits)

    for _ in range(max_cycles):
        for lane, bundle in enumerate(env.writeback):
            if not bundle.connected("valid") or bundle.read("valid", 0) == 0:
                continue
            if bundle.connected("ready") and bundle.read("ready", 0) == 0:
                continue
            if bundle.connected("isFromLoadUnit") and bundle.read("isFromLoadUnit", 0) == 0:
                continue
            if bundle.connected("toRob_valid") and bundle.read("toRob_valid", 0) == 0:
                continue
            if bundle.read("robIdx_flag", 0) != int(rob_idx_flag):
                continue
            if bundle.read("robIdx_value", 0) != int(rob_idx_value):
                continue

            exception_bits = bundle.read_exception_bits() if hasattr(bundle, "read_exception_bits") else []
            if not any(exception_bits):
                continue
            if any(bit >= len(exception_bits) or exception_bits[bit] == 0 for bit in required_bits):
                continue

            return {
                "cycle": env._current_cycle(),
                "lane": lane,
                "rob_idx_flag": bundle.read("robIdx_flag", 0),
                "rob_idx_value": bundle.read("robIdx_value", 0),
                "lq_idx_flag": bundle.read("lqIdx_flag", 0),
                "lq_idx_value": bundle.read("lqIdx_value", 0),
                "pdest": bundle.read("pdest", 0),
                "data": bundle.read("data_0", 0),
                "int_wen": bundle.read("intWen", 0),
                "exception_bits": exception_bits,
            }
        env.advance_cycles(1)

    raise TimeoutError(
        "等待异常 load writeback 观测超时: "
        f"rob=({rob_idx_flag},{rob_idx_value}), required_exception_bits={required_bits}"
    )


class MmuFaultingScalarLoadSequence:
    def __init__(
        self,
        *,
        root_pt_addr: int,
        page_table_page_addrs: tuple[int, ...],
        va: int,
        pa_base: int,
        initial_state: SequenceState,
        main_req_id: int,
        size: int,
        mask: int,
        main_pte_mode: str,
        main_pmp_mode: str,
        prime_req_id: int | None = None,
        prime_pte_mode: str | None = None,
        prime_pmp_mode: str | None = None,
        prime_expected_data: int | None = None,
        settle_cycles: int = 4,
        response_delay_cycles: int = 1,
        max_cycles: int | None = None,
        required_main_exception_bits: tuple[int, ...] = (),
    ) -> None:
        self.root_pt_addr = int(root_pt_addr)
        self.page_table_page_addrs = tuple(int(addr) for addr in page_table_page_addrs)
        self.va = int(va)
        self.pa_base = int(pa_base)
        self.initial_state = initial_state
        self.main_req_id = int(main_req_id)
        self.size = int(size)
        self.mask = int(mask)
        self.main_pte_mode = str(main_pte_mode)
        self.main_pmp_mode = str(main_pmp_mode)
        self.prime_req_id = None if prime_req_id is None else int(prime_req_id)
        self.prime_pte_mode = self.main_pte_mode if prime_pte_mode is None else str(prime_pte_mode)
        self.prime_pmp_mode = self.main_pmp_mode if prime_pmp_mode is None else str(prime_pmp_mode)
        self.prime_expected_data = None if prime_expected_data is None else int(prime_expected_data)
        self.settle_cycles = int(settle_cycles)
        self.response_delay_cycles = int(response_delay_cycles)
        self.max_cycles = max_cycles
        self.required_main_exception_bits = tuple(int(bit) for bit in required_main_exception_bits)

    def _run_one_load(
        self,
        env,
        *,
        req_id: int,
        lq_ptr,
        pte_mode: str,
        pmp_mode: str,
        expected_data: int | None,
        required_exception_bits: tuple[int, ...],
        refresh_fault_mapping: bool,
    ) -> tuple[LoadTxn, dict]:
        if refresh_fault_mapping:
            _write_sv39_fault_mapping(
                env,
                root_pt_addr=self.root_pt_addr,
                va=self.va,
                pa_base=self.pa_base,
                pte_mode=pte_mode,
                page_table_page_addrs=self.page_table_page_addrs,
            )
            env.mmu.pulse_sfence()
        _configure_pmp_mode(env, pmp_mode)

        txn = LoadTxn(
            req_id=req_id,
            addr=self.va,
            lq_ptr=lq_ptr,
            sq_ptr=self.initial_state.sq_ptr,
            size=self.size,
            mask=self.mask,
        )
        max_cycles = _resolve_replay_drain_cycles(env, self.max_cycles)
        prepared = env.backend.prepare(txn)

        if expected_data is not None:
            translated_pa = _resolve_sv39_pa(self.va, pa_base=self.pa_base)
            completed_before = env.get_completed_load_count()
            expected = env.expect_scalar_load(
                rob_idx=txn.rob_idx,
                pdest=txn.resolved_pdest,
                addr=translated_pa,
                size=txn.size,
                mask=txn.mask,
            )
            expected.expected_data = expected_data
            env.backend.execute(prepared)
            writeback = env.wait_load_writeback_observed(
                rob_idx=txn.rob_idx,
                data=expected_data,
                max_cycles=max_cycles,
            )
            _wait_completed_load_count(env, completed_before + 1, max_cycles=max_cycles)
            return txn, writeback

        previous_strict = env.memory.strict_writeback_check
        env.memory.strict_writeback_check = False
        try:
            env.backend.execute(prepared)
            writeback = _wait_load_exception_writeback_observed(
                env,
                rob_idx_flag=txn.rob_idx_flag,
                rob_idx_value=txn.rob_idx_value,
                required_exception_bits=required_exception_bits,
                max_cycles=max_cycles,
            )
            env.drain_writebacks(max_cycles=max_cycles)
            return txn, writeback
        finally:
            env.memory.strict_writeback_check = previous_strict

    def run(self, env) -> MmuFaultingScalarLoadSequenceResult:
        translated_pa = _resolve_sv39_pa(self.va, pa_base=self.pa_base)
        next_lq_ptr = self.initial_state.next_lq_ptr
        prime_txn = None
        prime_writeback = None
        prime_trace = ()
        initial_pte_mode = self.prime_pte_mode if self.prime_req_id is not None else self.main_pte_mode

        _write_sv39_fault_mapping(
            env,
            root_pt_addr=self.root_pt_addr,
            va=self.va,
            pa_base=self.pa_base,
            pte_mode=initial_pte_mode,
            page_table_page_addrs=self.page_table_page_addrs,
        )

        if self.prime_expected_data is not None:
            env.preload_u64(translated_pa, self.prime_expected_data)

        with env.mmu.ptw_responder(response_delay_cycles=self.response_delay_cycles) as ptw:
            env.mmu.enable_sv39(root_pt_addr=self.root_pt_addr, settle_cycles=self.settle_cycles)

            if self.prime_req_id is not None:
                prime_trace_start = len(ptw.trace)
                prime_txn, prime_writeback = self._run_one_load(
                    env,
                    req_id=self.prime_req_id,
                    lq_ptr=next_lq_ptr,
                    pte_mode=self.prime_pte_mode,
                    pmp_mode=self.prime_pmp_mode,
                    expected_data=self.prime_expected_data,
                    required_exception_bits=(),
                    refresh_fault_mapping=False,
                )
                prime_trace = tuple(list(ptw.trace)[prime_trace_start:])
                next_lq_ptr = ptr_inc(next_lq_ptr, env.config.sequence.load_queue_size)

            main_trace_start = len(ptw.trace)
            transport_stats_before_main = _snapshot_transport_stats(env)
            main_txn, main_writeback = self._run_one_load(
                env,
                req_id=self.main_req_id,
                lq_ptr=next_lq_ptr,
                pte_mode=self.main_pte_mode,
                pmp_mode=self.main_pmp_mode,
                expected_data=None,
                required_exception_bits=self.required_main_exception_bits,
                refresh_fault_mapping=(
                    self.prime_req_id is not None and self.main_pte_mode != self.prime_pte_mode
                ),
            )
            transport_stats_after_main = _snapshot_transport_stats(env)
            main_trace = tuple(list(ptw.trace)[main_trace_start:])

        return MmuFaultingScalarLoadSequenceResult(
            final_state=SequenceState(
                next_lq_ptr=ptr_inc(next_lq_ptr, env.config.sequence.load_queue_size),
                sq_ptr=self.initial_state.sq_ptr,
            ),
            prime_txn=prime_txn,
            prime_writeback=prime_writeback,
            main_txn=main_txn,
            main_writeback=main_writeback,
            prime_ptw_trace=prime_trace,
            main_ptw_trace=main_trace,
            transport_stats_before_main=transport_stats_before_main,
            transport_stats_after_main=transport_stats_after_main,
        )


class MmuPmpRegionLoadRecoverySequence:
    def __init__(
        self,
        *,
        root_pt_addr: int,
        page_table_page_addrs: tuple[int, ...],
        denied_va: int,
        allowed_va: int,
        denied_pa_base: int,
        allowed_pa_base: int,
        initial_state: SequenceState,
        prime_req_id: int,
        denied_req_id: int,
        allowed_req_id: int,
        restored_req_id: int,
        denied_data: int,
        allowed_data: int,
        deny_region_size: int = 0x1000,
        settle_cycles: int = 4,
        response_delay_cycles: int = 1,
        max_cycles: int | None = None,
        denied_exception_bits: tuple[int, ...] = (LOAD_ACCESS_FAULT_BIT,),
    ) -> None:
        self.root_pt_addr = int(root_pt_addr)
        self.page_table_page_addrs = tuple(int(addr) for addr in page_table_page_addrs)
        self.denied_va = int(denied_va)
        self.allowed_va = int(allowed_va)
        self.denied_pa_base = int(denied_pa_base)
        self.allowed_pa_base = int(allowed_pa_base)
        self.initial_state = initial_state
        self.prime_req_id = int(prime_req_id)
        self.denied_req_id = int(denied_req_id)
        self.allowed_req_id = int(allowed_req_id)
        self.restored_req_id = int(restored_req_id)
        self.denied_data = int(denied_data)
        self.allowed_data = int(allowed_data)
        self.deny_region_size = int(deny_region_size)
        self.settle_cycles = int(settle_cycles)
        self.response_delay_cycles = int(response_delay_cycles)
        self.max_cycles = max_cycles
        self.denied_exception_bits = tuple(int(bit) for bit in denied_exception_bits)

    def _run_translated_load(
        self,
        env,
        *,
        req_id: int,
        va: int,
        lq_ptr,
        expected_pa: int,
        expected_data: int | None = None,
        required_exception_bits: tuple[int, ...] = (),
    ) -> tuple[LoadTxn, dict]:
        txn = LoadTxn(
            req_id=int(req_id),
            addr=int(va),
            lq_ptr=lq_ptr,
            sq_ptr=self.initial_state.sq_ptr,
        )
        max_cycles = _resolve_replay_drain_cycles(env, self.max_cycles)
        prepared = env.backend.prepare(txn)

        if expected_data is not None:
            completed_before = env.get_completed_load_count()
            expected = env.expect_scalar_load(
                rob_idx=txn.rob_idx,
                pdest=txn.resolved_pdest,
                addr=int(expected_pa),
                size=txn.size,
                mask=txn.mask,
            )
            expected.expected_data = int(expected_data)
            env.backend.execute(prepared)
            writeback = env.wait_load_writeback_observed(
                rob_idx=txn.rob_idx,
                data=int(expected_data),
                max_cycles=max_cycles,
            )
            _wait_completed_load_count(env, completed_before + 1, max_cycles=max_cycles)
            return txn, writeback

        previous_strict = env.memory.strict_writeback_check
        env.memory.strict_writeback_check = False
        try:
            env.backend.execute(prepared)
            writeback = _wait_load_exception_writeback_observed(
                env,
                rob_idx_flag=txn.rob_idx_flag,
                rob_idx_value=txn.rob_idx_value,
                required_exception_bits=required_exception_bits,
                max_cycles=max_cycles,
            )
            env.drain_writebacks(max_cycles=max_cycles)
            return txn, writeback
        finally:
            env.memory.strict_writeback_check = previous_strict

    def run(self, env) -> MmuPmpRegionLoadRecoverySequenceResult:
        denied_pa = _resolve_sv39_pa(self.denied_va, pa_base=self.denied_pa_base)
        allowed_pa = _resolve_sv39_pa(self.allowed_va, pa_base=self.allowed_pa_base)
        deny_region_base = denied_pa & ~(self.deny_region_size - 1)
        if allowed_pa >= deny_region_base and allowed_pa < (deny_region_base + self.deny_region_size):
            raise ValueError(
                "allowed_va 落入 deny region: "
                f"allowed_va=0x{self.allowed_va:x}, allowed_pa=0x{allowed_pa:x}, "
                f"deny_region=[0x{deny_region_base:x},0x{deny_region_base + self.deny_region_size:x})"
            )

        next_page_table_idx = 0
        denied_install = env.mmu.install_sv39_mapping(
            root_pt_addr=self.root_pt_addr,
            va=self.denied_va & ~(SV39_PAGE_SIZE_4K - 1),
            pa_base=self.denied_pa_base,
            page_table_page_addrs=self.page_table_page_addrs[next_page_table_idx:],
        )
        next_page_table_idx += len(denied_install["allocated_table_addrs"])
        allowed_install = env.mmu.install_sv39_mapping(
            root_pt_addr=self.root_pt_addr,
            va=self.allowed_va & ~(SV39_PAGE_SIZE_4K - 1),
            pa_base=self.allowed_pa_base,
            page_table_page_addrs=self.page_table_page_addrs[next_page_table_idx:],
        )
        next_page_table_idx += len(allowed_install["allocated_table_addrs"])
        env.preload_u64(denied_pa, self.denied_data)
        env.preload_u64(allowed_pa, self.allowed_data)

        next_lq_ptr = self.initial_state.next_lq_ptr
        max_cycles = _resolve_replay_drain_cycles(env, self.max_cycles)

        with env.mmu.ptw_responder(response_delay_cycles=self.response_delay_cycles) as ptw:
            env.mmu.program_pmp_entry(index=0, cfg=0, addr=0, persistent=False)
            env.mmu.allow_all_smode_access(index=1, persistent=False)
            env.mmu.enable_sv39(root_pt_addr=self.root_pt_addr, settle_cycles=self.settle_cycles)

            prime_trace_start = len(ptw.trace)
            prime_txn, prime_writeback = self._run_translated_load(
                env,
                req_id=self.prime_req_id,
                va=self.denied_va,
                lq_ptr=next_lq_ptr,
                expected_pa=denied_pa,
                expected_data=self.denied_data,
            )
            next_lq_ptr = ptr_inc(next_lq_ptr, env.config.sequence.load_queue_size)
            _, _ = self._run_translated_load(
                env,
                req_id=self.prime_req_id | 0x100,
                va=self.allowed_va,
                lq_ptr=next_lq_ptr,
                expected_pa=allowed_pa,
                expected_data=self.allowed_data,
            )
            prime_ptw_trace = tuple(list(ptw.trace)[prime_trace_start:])
            next_lq_ptr = ptr_inc(next_lq_ptr, env.config.sequence.load_queue_size)

            env.mmu.program_pmp_deny_region(
                base_addr=deny_region_base,
                size=self.deny_region_size,
                index=0,
                persistent=False,
            )

            denied_trace_start = len(ptw.trace)
            transport_stats_before_denied = _snapshot_transport_stats(env)
            denied_txn, denied_writeback = self._run_translated_load(
                env,
                req_id=self.denied_req_id,
                va=self.denied_va,
                lq_ptr=next_lq_ptr,
                expected_pa=denied_pa,
                expected_data=None,
                required_exception_bits=self.denied_exception_bits,
            )
            transport_stats_after_denied = _snapshot_transport_stats(env)
            denied_ptw_trace = tuple(list(ptw.trace)[denied_trace_start:])
            denied_replay_state = env.sample_replay_state()
            next_lq_ptr = ptr_inc(next_lq_ptr, env.config.sequence.load_queue_size)

            allowed_trace_start = len(ptw.trace)
            allowed_txn, allowed_writeback = self._run_translated_load(
                env,
                req_id=self.allowed_req_id,
                va=self.allowed_va,
                lq_ptr=next_lq_ptr,
                expected_pa=allowed_pa,
                expected_data=self.allowed_data,
            )
            allowed_ptw_trace = tuple(list(ptw.trace)[allowed_trace_start:])
            next_lq_ptr = ptr_inc(next_lq_ptr, env.config.sequence.load_queue_size)

            env.mmu.program_pmp_entry(index=0, cfg=0, addr=0, persistent=False)

            restored_trace_start = len(ptw.trace)
            restored_txn, restored_writeback = self._run_translated_load(
                env,
                req_id=self.restored_req_id,
                va=self.denied_va,
                lq_ptr=next_lq_ptr,
                expected_pa=denied_pa,
                expected_data=self.denied_data,
            )
            restored_ptw_trace = tuple(list(ptw.trace)[restored_trace_start:])
            next_lq_ptr = ptr_inc(next_lq_ptr, env.config.sequence.load_queue_size)

        env.drain_writebacks(max_cycles=max_cycles)

        return MmuPmpRegionLoadRecoverySequenceResult(
            final_state=SequenceState(next_lq_ptr=next_lq_ptr, sq_ptr=self.initial_state.sq_ptr),
            prime_txn=prime_txn,
            prime_writeback=prime_writeback,
            denied_txn=denied_txn,
            denied_writeback=denied_writeback,
            allowed_txn=allowed_txn,
            allowed_writeback=allowed_writeback,
            restored_txn=restored_txn,
            restored_writeback=restored_writeback,
            prime_ptw_trace=prime_ptw_trace,
            denied_ptw_trace=denied_ptw_trace,
            allowed_ptw_trace=allowed_ptw_trace,
            restored_ptw_trace=restored_ptw_trace,
            transport_stats_before_denied=transport_stats_before_denied,
            transport_stats_after_denied=transport_stats_after_denied,
            denied_replay_state=denied_replay_state,
        )
