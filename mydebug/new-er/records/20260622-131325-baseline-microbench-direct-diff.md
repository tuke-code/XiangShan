# Baseline Microbench Direct Diff Debug Record

## Summary

- Date: 2026-06-22 13:13:25 CST
- Owner: Codex
- Testcase: `ready-to-run/microbench.bin`
- Result: fail, no final hit-good-trap
- Final trap line: none; run aborted in Difftest

## Prior History

- Prior records read: none
- Reused observations: none
- Rejected old hypotheses: none

## Command

```bash
CCACHE_DIR=/tmp/ccache-codex-review env -u LD_PRELOAD JAVA_TOOL_OPTIONS=-Djdk.net.hosts.file=/tmp/codex-hosts \
  python3 scripts/xiangshan.py ready-to-run/microbench.bin --emulator verilator --config MinimalConfig --threads 1 --trace-fst --max-instr 2000000 --timeout 1800
```

## Configuration

- enable: false in `MinimalConfig`
- observeOnly: false
- trackEntries: not enabled for this baseline run
- earlyFreeWidth: not enabled for this baseline run
- conservative redirect policy: not enabled for this baseline run
- commit hash: `79a1e0e0e4` plus dirty working tree
- difftest commit hash: `1631dcd53e26` plus dirty working tree

## Artifacts

- stdout: `mydocs/new-er/task27/logs/baseline-run-microbench.log`
- stderr: same combined command log
- wave: `build/2026-06-22-12-58-32_1.fst`
- counters: none useful; failure occurs before observe-only counter comparison
- extra logs: converted local VCD at `mydebug/new-er/artifacts/20260622-130000-baseline-microbench-direct-diff/waves/baseline-microbench.vcd` for local inspection only

## Symptom

- First bad cycle: Difftest reports abort at `cycleCnt = 2105`
- PC: DUT commit line reports `pc = 0x0000000080000080`
- ROB index: `0x023`
- logical register: `sp` / `x2`
- physical register: not isolated in this record
- Difftest or assertion message: `sp different ... right = 0x000000008000f080, wrong = 0x0000000000000000`
- Other symptom: DUT commit line for instruction `0x0000f117` reports commit data `0x0`

## Wave Notes

- Signals inspected: ROB public commit/writeback declarations, generated `Rob.sv` direct-diff selection logic, and integer RF writeback path source.
- Relevant cycle window: around the abort at instruction count 38 and cycle count 2105.
- Producer state: source inspection shows `JumpUnit` produces AUIPC result through the integer RF writeback path.
- Consumer/readDone state: not applicable; ER disabled.
- ROB/ST state: direct integer Difftest commit data originally came from ROB-side `debug_exuData`; after the first local bypass patch, generated RTL showed bypass logic from `io_exuWriteback_*_bits_data`.
- Free-list/suppress state: not applicable; ER disabled.
- Difftest state: the first bypass patch did not fix emulator behavior because some `WriteBackRobBundle.data` lanes are generated as constant zero for FU classes that still write the integer RF with real data.

## Hypothesis

- hypothesis: direct integer Difftest commit data must be sourced from the integer RF writeback path, not the ROB writeback data path.
- supporting evidence: `debug_exuData` is a register updated on EXU writeback, while `dtCommitWdata` reads `debug_exuData(ptr)` for the committing entry; if AUIPC writes back in the same cycle it commits, the direct shadow sees the old zero. Generated RTL for the first bypass patch proved the bypass existed, but it selected `io_exuWriteback_*_bits_data`, and several matching ROB writeback lanes legally carry `0` while the integer RF writeback bundle carries the actual result.
- evidence against: none after the focused test reproduced old ROB-writeback selection returning zero while RF-writeback selection returns `0x8000f080`.

## Root Cause

- root cause: ROB direct integer Difftest commit data read registered `debug_exuData` without a same-cycle integer RF writeback bypass. The first bypass used `WriteBackRobBundle.data`, which is not guaranteed to equal the integer RF writeback value for every integer-producing FU path; AUIPC through the JumpUnit path is the observed failing case.
- confidence: high; focused regression now distinguishes old ROB-writeback selection from RF-writeback selection for the same ROB index.

## Fix

- patch or commit: pending commit in Round 42
- files changed: `src/main/scala/xiangshan/backend/IntEarlyReleaseBundles.scala`, `src/main/scala/xiangshan/backend/Bundles.scala`, `src/main/scala/xiangshan/backend/datapath/WbArbiter.scala`, `src/main/scala/xiangshan/backend/Region.scala`, `src/main/scala/xiangshan/backend/Backend.scala`, `src/main/scala/xiangshan/backend/CtrlBlock.scala`, `src/main/scala/xiangshan/backend/rob/RobBundles.scala`, `src/main/scala/xiangshan/backend/rob/Rob.scala`, `src/test/scala/xiangshan/backend/IntEarlyReleaseRobTest.scala`
- reason the fix addresses the root cause: `WbDataPath` emits an `IntCommitWriteback` event from the integer RF writeback arbiter output, `Backend` merges the per-region lanes, `CtrlBlock` delays and redirect-filters that event in the same timing class as ROB writeback, and ROB direct Difftest selects matching same-cycle RF writeback data by full ROB pointer before falling back to stored `debug_exuData`.

## Validation

- command: `mill -i xiangshan.test.testOnly xiangshan.backend.IntEarlyReleaseRobTest -- -z "select direct integer diff write data from same-cycle writeback"`
- result: passed, 1 test succeeded
- command: `mill -i xiangshan.test.testOnly xiangshan.backend.IntEarlyReleaseRobTest`
- result: passed, 15 tests succeeded
- command: `mill -i difftest.test.testOnly difftest.PreprocessTest`
- result: passed, 6 tests succeeded
- command: `CCACHE_DIR=/tmp/ccache-codex-review env -u LD_PRELOAD JAVA_TOOL_OPTIONS=-Djdk.net.hosts.file=/tmp/codex-hosts python3 scripts/xiangshan.py ready-to-run/microbench.bin --emulator verilator --config MinimalConfig --threads 1 --trace-fst --max-instr 2000000 --timeout 1800`
- result: passed after the owner-only and RF-shadow follow-up fixes, with `MicroBench PASS` and final `HIT GOOD TRAP at pc = 0x80003a4e`
- command: `CCACHE_DIR=/tmp/ccache-codex-review env -u LD_PRELOAD JAVA_TOOL_OPTIONS=-Djdk.net.hosts.file=/tmp/codex-hosts python3 scripts/xiangshan.py ready-to-run/microbench.bin --emulator verilator --config IntERObserveOnlyMinimalConfig --threads 1 --trace-fst --max-instr 2000000 --timeout 1800`
- result: passed after clean observe-only rebuild, with `MicroBench PASS` and final `HIT GOOD TRAP at pc = 0x80003a4e`
- hit-good-trap classification: pass for both baseline and observe-only task27 smoke.
- remaining risk: larger emu-basics coverage remains a later task.

## Next Action

- next action: rebuild the `MinimalConfig` emulator and rerun `ready-to-run/microbench.bin` to verify that direct integer Difftest no longer aborts on the AUIPC `sp` result.
