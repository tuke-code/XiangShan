# Breakpoints & Trigger Examples

Accurately halting the simulation process is critical for debugging software/hardware interactions.

## 1. Simple Signal Breakpoints (`xbreak`)
Stop execution when a specific signal matches a given numerical value.

```text
# Valid condition operators: eq/==, ne/!=, gt/>, lt/<, ge/>=, le/<=, and ch (capture toggle transition)
# Example: Pause when timer reaches 10000
(XiangShan) xbreak SimTop_top.SimTop.timer eq 10000

# Simulation runs at full speed until the condition hits, then returns to the terminal
(XiangShan) xstep 100000

# Clear a specific normal breakpoint
(XiangShan) xunbreak SimTop_top.SimTop.timer

# Clear all active conventional breakpoints
(XiangShan) xunbreak all
```

## 2. Expression Engine Triggers (`xbreak_expr`)
Combines multiple signals and provides native C++ timeline evaluation for superior performance.

```text
# Trigger when PC hits a highly specific address AND an external signal is asserted:
(XiangShan) xbreak_expr SimTop_top.SimTop.cpu.commit_pc == 0x80001004 and sig_xx == 1

# [Time Window] Trigger if the timer reached 1000 at any point within the LAST 20 cycles:
# IMPORTANT: The syntax is within(cycles, expression)
(XiangShan) xbreak_expr within(20, SimTop_top.SimTop.timer == 1000)

# [Hold State] Trigger ONLY if an exception signal is held high for 4 consecutive cycles:
(XiangShan) xbreak_expr hold(4, SimTop_top.SimTop.rob.has_exception == 1)

# Clear an expression breakpoint using its auto-assigned ID
(XiangShan) xunbreak_expr xexpr-0
```
> For highly complex RISC-V specific instruction or cache triggers, see `xbreak_expr_examples.md`.

## 3. Complex Sequence FSM Triggers (`xbreak_fsm`)
For advanced multi-step debugging (e.g., event A happens, then event C, counting B occurrences).

```text
# Load and enable a custom State Machine detection script
(XiangShan) xbreak_fsm /abs/path/to/docs/XSPdb/examples/pc_sequence.fsm

# Check the current status and internal state of the FSM:
(XiangShan) xbreak_fsm_status
name=pc_sequence.fsm state=WAIT_PC_80 triggered=False

# Clear the FSM trigger
(XiangShan) xbreak_fsm_clear
```
