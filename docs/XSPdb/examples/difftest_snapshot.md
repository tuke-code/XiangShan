# Difftest Co-simulation & Snapshots

Difftest ensures the hardware logic accurately diverges and synchronizes dynamically against an established reference software emulation core (like NEMU/Spike).

## 1. State Extraction via XSPdb
When interactive verification reaches a mismatch zone, rather than parsing massive text logs, you can pull the architectural truth environment natively into Python.

```text
# Extract complex system representations natively to the variable workspace `ds`
(XiangShan) xexpdiffstate ds

# From this point forward inside the session, you can do Python interactions:
# Example (simulated): type `p ds.regs.csr.mepc` to dump NEMU's truth PC!
```

## 2. Simulation Environment Snapshots
Snapshotting handles saving and reviving deep simulator memory contexts using fork intervals.

This feature behaves entirely through out-of-band automation triggers outside the traditional `(XiangShan)` terminal loop.

```bash
# When instantiating the testing matrix runner, specify the fork intervals:
# The simulator saves periodic checkpoint environments dynamically while running
python3 scripts/emu.py --image /path/to/bin --fork-interval 10
```
