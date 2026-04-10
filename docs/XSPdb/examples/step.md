# Step & Execution Control Examples

This document demonstrates how to advance the XiangShan hardware simulation and step through software instructions.

## 1. Hardware Cycle Stepping (`xstep`)
The simulation advances based on hardware clock cycles.

```text
# Advance the simulation by 10 clock cycles
(XiangShan) xstep 10
[Info] step 10 cycles complete

# For continuous execution, provide a large number (interrupt anytime with Ctrl-C)
(XiangShan) xstep 100000000
```

## 2. Instruction Stepping (`xistep`)
Focus on the architectural software state by advancing until instructions are successfully committed.

```text
# Run until the next instruction is committed
(XiangShan) xistep 1

# Execute 5 instructions continuously
(XiangShan) xistep 5
```
> **Note:** `xistep` relies on the difftest interface's `get_commit`. If the pipeline encounters long stalls (e.g., branch misses or memory fetches), the cycle count spanned by one `xistep` may be significant.

## 3. Checking Execution State (`xpc`)
Check the real-time PC (Program Counter) state across all ways of the superscalar execution.

```text
# Query the commit PC and architectural instruction hex
(XiangShan) xpc
PC[0]: 0x80000000    Instr: 0x00000093
PC[1]: 0x80000004    Instr: 0x0182b283
...
```
