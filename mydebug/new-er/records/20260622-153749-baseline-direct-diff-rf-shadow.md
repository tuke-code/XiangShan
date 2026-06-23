# Baseline Direct Integer Diff RF Shadow Debug Record

## Summary

- Date: 2026-06-22 15:37:49 CST
- Owner: Codex
- Testcase: `ready-to-run/microbench.bin`
- Result: fail, no final hit-good-trap
- Final trap line: none; run aborted in Difftest

## Prior History

- Prior records read:
  - `mydebug/new-er/records/20260622-131325-baseline-microbench-direct-diff.md`
  - `mydebug/new-er/records/20260622-144213-baseline-commit-writeback-owner.md`
- Reused observations: integer commit data for direct Difftest must come from the integer RF writeback path, and the RF owner path is `intRegion` rather than a cross-region merge.
- Rejected old hypotheses: the owner-only fix removed the previous Backend merge assertion, but same-cycle RF writeback bypass alone did not solve commits whose RF writeback occurred before the commit cycle.

## Command

```bash
CCACHE_DIR=/tmp/ccache-codex-review env -u LD_PRELOAD JAVA_TOOL_OPTIONS=-Djdk.net.hosts.file=/tmp/codex-hosts \
  python3 scripts/xiangshan.py ready-to-run/microbench.bin --emulator verilator --config MinimalConfig --threads 1 --trace-fst --max-instr 2000000 --timeout 1800 \
  > mydocs/new-er/task27/logs/baseline-run-microbench-after-owner-fix.log 2>&1
```

## Configuration

- enable: false in `MinimalConfig`
- observeOnly: false
- trackEntries: not enabled for this baseline run
- earlyFreeWidth: not enabled for this baseline run
- conservative redirect policy: not enabled for this baseline run
- commit hash: `79a1e0e0e` plus dirty working tree
- difftest commit hash: `1631dcd53` plus dirty working tree

## Artifacts

- stdout: `mydocs/new-er/task27/logs/baseline-run-microbench-after-owner-fix.log`
- stderr: same combined command log
- wave: `build/2026-06-22-15-21-43_1.fst`
- counters: none useful; failure occurs in baseline-disabled direct Difftest before observe-only comparison
- extra logs: `mydocs/new-er/task27/logs/baseline-build-after-owner-fix.log`

## Symptom

- First bad cycle: Difftest reports abort at `cycleCnt = 2105`
- PC: DUT commit line reports `pc = 0x0000000080000080`
- ROB index: `0x023`
- logical register: `sp` / `x2`
- physical register: not isolated in this record
- Difftest or assertion message: `sp different ... right = 0x000000008000f080, wrong = 0x0000000000000000`
- Other symptom: DUT commit line for instruction `0x0000f117` reports commit data `0x0`

## Wave Notes

- Signals inspected: generated `WbDataPath.sv` direct integer RF writeback event wiring, `CtrlBlock.sv` delayed `intCommitWriteback`, and `Rob.sv` `dtCommitWdata_0` selection.
- Relevant cycle window: around instruction count 38 and cycle count 2105.
- Producer state: generated RTL shows `WbDataPath` emits `intCommitWriteback_*_valid` from `toIntPreg_*_wen`, so the event represents a real integer RF write.
- Consumer/readDone state: not applicable; ER is disabled.
- ROB/ST state: generated RTL still allowed `dtCommitWdata_0` to fall back to `debug_exuData_35` when no same-cycle RF writeback for the committing ROB index was present.
- Free-list/suppress state: not applicable; ER is disabled.
- Difftest state: `debug_exuData_35` can be written from ordinary ROB writeback lanes whose data is zero for this AUIPC path, while the correct architectural integer value was visible on the RF writeback path in an earlier cycle.

## Hypothesis

- hypothesis: direct integer Difftest needs a ROB-side write-data shadow updated by delayed integer RF writeback events, not only a same-cycle commit bypass.
- supporting evidence: the failing commit is not guaranteed to occur in the same cycle as the RF writeback; generated `Rob.sv` shows fallback to `debug_exuData` when no matching same-cycle `intCommitWriteback` exists. Existing source inspection shows `debug_exuData` is updated from `WriteBackRobBundle.data`, which can be zero for FU paths that still write the integer RF with correct data.
- evidence against: none found after focused tests distinguished same-cycle bypass from earlier RF-writeback retention.

## Root Cause

- root cause: Round 42 initially fixed only same-cycle direct integer commit-data bypass. When an integer RF writeback occurs before commit, ROB direct Difftest still falls back to `debug_exuData`, which is not a reliable integer architectural write-data source for every integer-producing FU path.
- confidence: high; focused regression now reproduces the missing retention case and passes after adding the RF-writeback shadow.

## Fix

- patch or commit: pending commit in Round 42
- files changed: `src/main/scala/xiangshan/backend/rob/RobBundles.scala`, `src/main/scala/xiangshan/backend/rob/Rob.scala`, `src/test/scala/xiangshan/backend/IntEarlyReleaseRobTest.scala`
- reason the fix addresses the root cause: `RobIntDiffOps.updateWriteDataShadow` records delayed integer RF writeback data by ROB index. ROB direct integer Difftest commit data now reads this RF-sourced shadow and still applies same-cycle RF writeback bypass for commit/writeback coincidences.

## Validation

- command: `CCACHE_DIR=/tmp/ccache-codex-review env -u LD_PRELOAD JAVA_TOOL_OPTIONS=-Djdk.net.hosts.file=/tmp/codex-hosts mill -i xiangshan.test.testOnly xiangshan.backend.IntEarlyReleaseRobTest -- -z "retain direct integer diff write data"`
- result: passed, 1 test succeeded
- command: `CCACHE_DIR=/tmp/ccache-codex-review env -u LD_PRELOAD JAVA_TOOL_OPTIONS=-Djdk.net.hosts.file=/tmp/codex-hosts mill -i xiangshan.test.testOnly xiangshan.backend.IntEarlyReleaseRobTest -- -z "select direct integer diff write data"`
- result: passed, 1 test succeeded
- command: `CCACHE_DIR=/tmp/ccache-codex-review env -u LD_PRELOAD JAVA_TOOL_OPTIONS=-Djdk.net.hosts.file=/tmp/codex-hosts mill -i xiangshan.test.testOnly xiangshan.backend.IntEarlyReleaseRobTest`
- result: passed, 16 tests succeeded
- command: `CCACHE_DIR=/tmp/ccache-codex-review env -u LD_PRELOAD JAVA_TOOL_OPTIONS=-Djdk.net.hosts.file=/tmp/codex-hosts python3 scripts/xiangshan.py ready-to-run/microbench.bin --emulator verilator --config MinimalConfig --threads 1 --trace-fst --max-instr 2000000 --timeout 1800`
- result: passed, with `MicroBench PASS` and final `HIT GOOD TRAP at pc = 0x80003a4e`
- command: `CCACHE_DIR=/tmp/ccache-codex-review env -u LD_PRELOAD JAVA_TOOL_OPTIONS=-Djdk.net.hosts.file=/tmp/codex-hosts python3 scripts/xiangshan.py ready-to-run/microbench.bin --emulator verilator --config IntERObserveOnlyMinimalConfig --threads 1 --trace-fst --max-instr 2000000 --timeout 1800`
- result: passed after clean observe-only rebuild, with `MicroBench PASS` and final `HIT GOOD TRAP at pc = 0x80003a4e`
- hit-good-trap classification: pass for both baseline and observe-only task27 smoke.
- remaining risk: none for RF-writeback data retention in the task27 smoke; larger emu-basics coverage remains a later task.

## Next Action

- next action: rebuild `MinimalConfig`, rerun baseline microbench, then run observe-only comparison if baseline reaches final `hit-good-trap`.
