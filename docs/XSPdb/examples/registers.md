# Architectural Register Management

Before a test scenario actually initiates, setting up a reproducible hardware environment is extremely important. 

## 1. Bulk Restoration from Files
Instead of typing commands one by one, XSPdb supports capturing entire thread contexts through standard descriptor text files.

```text
# Evaluate the file to check format validity
(XiangShan) xparse_reg_file /abs/path/to/regs.txt

# Overwrite current state using the context payload
(XiangShan) xload_reg_file /abs/path/to/regs.txt
```

## 2. Individual Architectural Tuning
Allows quick manipulations when investigating corner execution paths.
```text
# Modify an integer core unit register (e.g. x1/ra)
(XiangShan) xset_ireg t1 0x1234

# Overwrite IEEE 754 Floating-Point registers directly
(XiangShan) xset_freg ft1 0x3f800000

# Hijack and reset the physical Program Counter (MPC) natively:
(XiangShan) xset_mpc 0x80000000
```

## 3. Flash Constants Overview
Inspect initial boot configuration boundaries.
```text
# Analyze what core flash boot structures represent
(XiangShan) xlist_flash_iregs
(XiangShan) xlist_flash_fregs
(XiangShan) xlist_freg_map
```
