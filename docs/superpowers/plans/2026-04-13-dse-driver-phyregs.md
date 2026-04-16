# DSE Driver Physical Register Writes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Update `dse-driver` so bank0 and bank1 programming also write `intphyregs` and `fpphyregs` into the new DSE control registers.

**Architecture:** Keep the driver structure unchanged and only un-comment the two physical-register writes in each bank-selection branch. Verify the change by rebuilding the driver and checking the resulting disassembly for the new register offsets.

**Tech Stack:** C, RISC-V bare-metal cross toolchain, objdump

---

### Task 1: Enable physical-register writes in the driver

**Files:**
- Modify: `dse-driver/dse.c`
- Test: `dse-driver/build/dse_1.txt`

- [ ] **Step 1: Write the failing check**

Run:

```bash
make -C dse-driver clean all BUILD_NUM=1
rg -n "1c0|1c8|1d0|1d8" dse-driver/build/dse_1.txt
```

Expected: no matches for the four physical-register bank offsets.

- [ ] **Step 2: Implement the minimal change**

In `dse-driver/dse.c`, enable:

```c
*(volatile uint64_t *)INTPHYREGS0_REG = *(volatile uint64_t *)INTPHYREGS_ADDR;
*(volatile uint64_t *)FPPHYREGS0_REG = *(volatile uint64_t *)FPPHYREGS_ADDR;
...
*(volatile uint64_t *)INTPHYREGS1_REG = *(volatile uint64_t *)INTPHYREGS_ADDR;
*(volatile uint64_t *)FPPHYREGS1_REG = *(volatile uint64_t *)FPPHYREGS_ADDR;
```

- [ ] **Step 3: Verify the fix**

Run:

```bash
make -C dse-driver clean all BUILD_NUM=1
rg -n "1c0|1c8|1d0|1d8" dse-driver/build/dse_1.txt
```

Expected: matches showing the driver now touches the physical-register bank offsets.
