# coding=utf-8
"""
Vector memory reference model.
"""

from __future__ import annotations

from dataclasses import dataclass

from transactions import VectorElementAccess, VectorMemTxn


@dataclass(frozen=True)
class VectorLoadExpectation:
    """Reference expansion for one vector load request."""

    req_id: int
    accesses: tuple[VectorElementAccess, ...]


class VectorMemoryModel:
    """Reference-side expansion and outstanding tracking for vector memory ops."""

    def __init__(self, ref_memory) -> None:
        self.ref_memory = ref_memory
        self._pending_requests: dict[int, str] = {}

    def reset_runtime_state(self) -> None:
        self._pending_requests.clear()

    @property
    def outstanding_expected_count(self) -> int:
        return len(self._pending_requests)

    def expand(self, txn: VectorMemTxn) -> tuple[VectorElementAccess, ...]:
        accesses = []
        for element_idx in range(int(txn.element_count)):
            is_prestart = element_idx < int(txn.vstart)
            is_tail = element_idx >= int(txn.vl)
            mask_enabled = True
            if txn.mask_bits is not None:
                mask_enabled = bool(txn.mask_bits[element_idx])

            active = not is_prestart and not is_tail and mask_enabled
            if txn.opcode_class == "unit_stride":
                addr = int(txn.base_addr) + element_idx * int(txn.size_bytes)
            else:
                addr = int(txn.base_addr) + element_idx * int(txn.stride)

            expected_load_data = None
            store_data = None
            if active and txn.is_load:
                expected_load_data = self.ref_memory.read(addr, txn.size_bytes)
            if active and not txn.is_load:
                store_data = int(txn.store_data[element_idx])

            accesses.append(
                VectorElementAccess(
                    element_idx=element_idx,
                    active=active,
                    is_tail=is_tail,
                    is_prestart=is_prestart,
                    addr=addr,
                    size_bytes=txn.size_bytes,
                    expected_load_data=expected_load_data,
                    store_data=store_data,
                    should_access_memory=active,
                )
            )
        return tuple(accesses)

    def expect_load(self, txn: VectorMemTxn) -> VectorLoadExpectation:
        if not txn.is_load:
            raise ValueError("expect_load requires a vector load transaction")
        self._pending_requests[int(txn.req_id)] = "load"
        return VectorLoadExpectation(req_id=int(txn.req_id), accesses=self.expand(txn))

    def predict_store(self, txn: VectorMemTxn):
        if txn.is_load:
            raise ValueError("predict_store requires a vector store transaction")
        predicted = self.ref_memory.clone()
        for access in self.expand(txn):
            if not access.should_access_memory:
                continue
            predicted.apply_masked_write(
                access.addr,
                access.store_data,
                (1 << access.size_bytes) - 1,
                access.size_bytes,
            )
        self._pending_requests[int(txn.req_id)] = "store"
        return predicted

    def mark_completed(self, req_id: int) -> None:
        self._pending_requests.pop(int(req_id), None)
