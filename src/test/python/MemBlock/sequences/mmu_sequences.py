# coding=utf-8
"""
MemBlock MMU-oriented reusable sequences.
"""

from dataclasses import dataclass

from transactions import LoadTxn, ptr_inc
from transactions import BackendSendPlan, EnqueueLoadStep, IssueCyclePlan, IssueOp
from request_apis import send_load

from .memblock_sequences import (
    SequenceState,
    _resolve_replay_drain_cycles,
    _snapshot_transport_stats,
    _wait_completed_load_count,
)


def _resolve_sv39_gigapage_pa(va: int, pa_base: int) -> int:
    return int(pa_base) | (int(va) & ((1 << 30) - 1))


SV39_PTE_V = 1 << 0
SV39_PTE_R = 1 << 1
SV39_PTE_W = 1 << 2
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
class Sv39GigapageMapping:
    va: int
    pa_base: int


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
    mappings: tuple[Sv39GigapageMapping, ...]
    translated_preloads: tuple[TranslatedU64MemoryPreload, ...] = ()
    bare_preloads: tuple[U64MemoryPreload, ...] = ()


@dataclass(frozen=True)
class MmuSv39AddressSpaceInstallSequenceResult:
    root_pt_addr: int
    mappings: tuple[Sv39GigapageMapping, ...]
    translated_preloads: tuple[U64MemoryPreload, ...]
    bare_preloads: tuple[U64MemoryPreload, ...]

    def translated_pa_for(self, va: int) -> int:
        for mapping in self.mappings:
            if (int(mapping.va) >> 30) == (int(va) >> 30):
                return _resolve_sv39_gigapage_pa(int(va), int(mapping.pa_base))
        raise KeyError(f"地址空间 0x{self.root_pt_addr:x} 未配置 va=0x{int(va):x} 的 1GiB gigapage mapping")


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
            Sv39GigapageMapping(va=int(mapping.va), pa_base=int(mapping.pa_base))
            for mapping in self.config.mappings
        )
        result = MmuSv39AddressSpaceInstallSequenceResult(
            root_pt_addr=int(self.config.root_pt_addr),
            mappings=mappings,
            translated_preloads=(),
            bare_preloads=tuple(
                U64MemoryPreload(addr=int(preload.addr), data=int(preload.data))
                for preload in self.config.bare_preloads
            ),
        )

        for mapping in mappings:
            env.mmu.install_sv39_gigapage_mapping(
                root_pt_addr=result.root_pt_addr,
                va=mapping.va,
                pa_base=mapping.pa_base,
            )

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


def _sv39_fault_leaf_pte(*, pa_base: int, pte_mode: str) -> int:
    if int(pa_base) & ((1 << 30) - 1):
        raise ValueError(f"SV39 1GiB leaf PTE 需要 1GiB 对齐物理地址: 0x{int(pa_base):x}")
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


def _install_sv39_fault_mapping(env, *, root_pt_addr: int, va: int, pa_base: int, pte_mode: str) -> None:
    vpn2 = (int(va) >> 30) & 0x1FF
    pte_addr = int(root_pt_addr) + vpn2 * 8
    env.preload_u64(pte_addr, _sv39_fault_leaf_pte(pa_base=pa_base, pte_mode=pte_mode))


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
    ) -> tuple[LoadTxn, dict]:
        _install_sv39_fault_mapping(
            env,
            root_pt_addr=self.root_pt_addr,
            va=self.va,
            pa_base=self.pa_base,
            pte_mode=pte_mode,
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
            translated_pa = _resolve_sv39_gigapage_pa(self.va, self.pa_base)
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
        translated_pa = _resolve_sv39_gigapage_pa(self.va, self.pa_base)
        next_lq_ptr = self.initial_state.next_lq_ptr
        prime_txn = None
        prime_writeback = None
        prime_trace = ()

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
        denied_va: int,
        allowed_va: int,
        pa_base: int,
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
        self.denied_va = int(denied_va)
        self.allowed_va = int(allowed_va)
        self.pa_base = int(pa_base)
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
        denied_pa = _resolve_sv39_gigapage_pa(self.denied_va, self.pa_base)
        allowed_pa = _resolve_sv39_gigapage_pa(self.allowed_va, self.pa_base)
        deny_region_base = denied_pa & ~(self.deny_region_size - 1)
        if allowed_pa >= deny_region_base and allowed_pa < (deny_region_base + self.deny_region_size):
            raise ValueError(
                "allowed_va 落入 deny region: "
                f"allowed_va=0x{self.allowed_va:x}, allowed_pa=0x{allowed_pa:x}, "
                f"deny_region=[0x{deny_region_base:x},0x{deny_region_base + self.deny_region_size:x})"
            )

        env.mmu.install_sv39_gigapage_mapping(
            root_pt_addr=self.root_pt_addr,
            va=self.denied_va,
            pa_base=self.pa_base,
        )
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
