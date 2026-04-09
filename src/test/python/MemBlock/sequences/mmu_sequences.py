# coding=utf-8
"""
MemBlock MMU-oriented reusable sequences.
"""

from dataclasses import dataclass

from request_apis import LoadTxn, ptr_inc, send_load

from .memblock_sequences import SequenceState, _resolve_replay_drain_cycles, _wait_completed_load_count


def _resolve_sv39_gigapage_pa(va: int, pa_base: int) -> int:
    return int(pa_base) | (int(va) & ((1 << 30) - 1))


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
            env.expect_scalar_load(
                req_id=txn.req_id,
                pdest=txn.resolved_pdest,
                addr=spec.expected_pa,
                size=txn.size,
                mask=txn.mask,
            )
            send_load(env, txn)
            writeback = env.wait_load_writeback_observed(
                rob_idx_flag=txn.rob_idx_flag,
                rob_idx_value=txn.rob_idx_value,
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
