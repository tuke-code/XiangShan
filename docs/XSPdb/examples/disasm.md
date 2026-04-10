# Disassembly Utilities

Understand what raw bytes represent in RISC-V human-readable mnemonic terminology. It uses `spike-dasm` primarily and falls back to `capstone`.

## 1. Memory vs. Flash Regions
```text
# Disassemble 16 bytes directly from Main RAM starting at PMEM_BASE (typically 0x80000000)
(XiangShan) xdasm 0x80000000 16

# Disassemble directly from the boot Flash ROM address space (typically 0x10000000)
(XiangShan) xdasmflash 0x10000000 16
```

## 2. Direct Raw Bytes Translation
You can pass custom hex arrays or integers to reverse-engineer unknown code blocks.

```text
# Supply raw standard bytes
(XiangShan) xdasmbytes 13 05 00 93
> 0x0: 93000513  addi a0, zero, 0x6d0

# Test a solitary execution word logic
(XiangShan) xdasmnumber 0x93000513 
> 0x0: 93000513  addi a0, zero, 0x6d0
```

## 3. Disassembly Hot-Reload
XSPdb heavily caches code paths during stepping for UI acceleration. If you dynamically modified the underlying program stream during runtime, you need to manually refresh the cache.

```text
(XiangShan) xclear_dasm_cache
```
