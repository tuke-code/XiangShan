# Memory & Data Streams IO

Integrate standard ELF binaries and dump complex verification matrices.

## 1. Loading Executables
Inject bare metal applications directly into the mapped simulation realms.

```text
# Overlay a unified application binary into PMEM_BASE (Core Memory space)
(XiangShan) xload /abs/path/to/application.bin

# Load firmware payloads directly onto Flash structures
(XiangShan) xflash /abs/path/to/bootrom_flash.bin

# Wipe all flash modifications cleanly
(XiangShan) xreset_flash
```

## 2. Dynamic Memory Tweaking
```text
# Force an absolute manipulation of specific memory regions
# Format: address, length, data_payload
(XiangShan) xmem_write 0x80000000 4 0xdeadbeef

# Substitute raw instructions via pipeline NOP filling 
(XiangShan) xnop_insert 0x80000000 4
```

## 3. Extracting and Saving State (Snapshots / Coredumps)
```text
# Export arbitrary simulation bounds as a flattened .bin file
(XiangShan) xexport_bin /abs/path/to/mem_dump.bin
(XiangShan) xexport_flash /abs/path/to/flash_dump.bin

# Rip out exactly just the active memory arrays
(XiangShan) xexport_ram /abs/path/to/ram_dump.bin
```

## 4. Arithmetic Data Utilities
```text
(XiangShan) xbytes_to_bin 01 02 03 04
(XiangShan) xbytes2number 01 02 03 04
(XiangShan) xnumber2bytes 0x12345678 4
```
