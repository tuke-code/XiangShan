# coding=utf-8
"""
RefMemory: MemBlock 黄金内存抽象。
"""

import random

from transactions import scalar_store_size_bytes_from_mask


class RefMemory:
    """维护字节级黄金内存状态。"""

    def __init__(self) -> None:
        self.storage: dict[int, int] = {}

    def clear(self) -> None:
        self.storage.clear()

    def clone(self) -> "RefMemory":
        cloned = RefMemory()
        cloned.storage = dict(self.storage)
        return cloned

    def preload_bytes(self, base_addr: int, data: bytes) -> None:
        for offset, value in enumerate(data):
            self.storage[base_addr + offset] = value & 0xFF

    def preload_u64(self, addr: int, value: int) -> None:
        self.preload_bytes(addr, int(value & ((1 << 64) - 1)).to_bytes(8, "little"))

    def fill_random(self, addr_start: int, addr_end: int, seed: int, line_bytes: int = 64) -> None:
        if addr_end < addr_start:
            raise ValueError("addr_end 不能小于 addr_start")
        rng = random.Random(seed)
        start = addr_start - (addr_start % line_bytes)
        end = addr_end + ((line_bytes - (addr_end % line_bytes)) % line_bytes)
        for base in range(start, end, line_bytes):
            self.preload_bytes(
                base,
                bytes(rng.getrandbits(8) for _ in range(line_bytes)),
            )

    def read(self, addr: int, size: int) -> int:
        value = 0
        for offset in range(size):
            value |= (self.storage.get(addr + offset, 0) & 0xFF) << (offset * 8)
        return value

    def read_masked(self, addr: int, mask: int, width_bytes: int = 8) -> int:
        value = 0
        out_offset = 0
        for byte_idx in range(width_bytes):
            if (mask >> byte_idx) & 0x1:
                byte = self.storage.get(addr + byte_idx, 0) & 0xFF
                value |= byte << (out_offset * 8)
                out_offset += 1
        return value

    def read_cacheline(self, block_addr: int, line_bytes: int = 64) -> bytes:
        return bytes(self.storage.get(block_addr + idx, 0) & 0xFF for idx in range(line_bytes))

    def apply_masked_write(self, addr: int, data: int, mask: int, width_bytes: int) -> None:
        for byte_idx in range(width_bytes):
            if (mask >> byte_idx) & 0x1:
                self.storage[addr + byte_idx] = (data >> (byte_idx * 8)) & 0xFF

    def apply_store(self, addr: int, data: int, mask: int = 0xFF) -> None:
        normalized_mask = int(mask) & 0xFF
        width_bytes = scalar_store_size_bytes_from_mask(normalized_mask)
        self.apply_masked_write(addr, data, normalized_mask, width_bytes)

    def with_masked_write(self, addr: int, data: int, mask: int, width_bytes: int) -> "RefMemory":
        predicted = self.clone()
        predicted.apply_masked_write(addr, data, mask, width_bytes)
        return predicted

    def with_store(self, addr: int, data: int, mask: int = 0xFF) -> "RefMemory":
        predicted = self.clone()
        predicted.apply_store(addr, data, mask)
        return predicted
