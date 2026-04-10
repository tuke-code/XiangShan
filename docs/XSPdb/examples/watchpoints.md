# Watchpoints & Immediate Visibility

Unlike breakpoints which halt the entire application, Watchpoints simply passively report changes asynchronously to the terminal logger, ideal for tracing state machines and MMIO registers.

## 1. General Signal Monitoring (`xwatch`)
Automatically output a log entry over the console whenever the target signal's logic state flips.

```text
# Enable real-time console tracing for an internal timer
(XiangShan) xwatch SimTop_top.SimTop.timer

# During stepping, the console will spontaneously print out transitions:
(XiangShan) xstep 100

# Stop tracing
(XiangShan) xunwatch SimTop_top.SimTop.timer
```

## 2. Commit PC Watchdogs (`xwatch_commit_pc`)
Useful for investigating control-flow and branch-prediction corruption. It enforces an automatic break condition once the engine hits a designated target PC at the final commit phase.

```text
# Configure the architectural front-end to break only when PC execution reaches exactly here:
(XiangShan) xwatch_commit_pc 0x80000004

(XiangShan) xstep 10000
(XiangShan) xunwatch_commit_pc 0x80000004
```

## 3. Quick Peek & Modification
Sometimes you don't need a persistent watch hook; a single query suffices.

```text
# Fetch the immediate literal value of any hierarchical register/wire:
(XiangShan) xprint SimTop_top.SimTop.timer

# Dynamically override/inject a value to bypass logic loops:
(XiangShan) xset SimTop_top.SimTop.timer 123
```
