# Baseline Commit Writeback Owner Debug Record

## Summary

- Date: 2026-06-22 14:42:13 CST
- Owner: Codex
- Testcase: `ready-to-run/microbench.bin`
- Result: fail, no final hit-good-trap
- Final trap line: none; simulator aborted on assertion

## Prior History

- Prior records read: `mydebug/new-er/records/20260622-131325-baseline-microbench-direct-diff.md`
- Reused observations: the previous record established that ROB direct integer Difftest commit data must be sourced from the integer RF writeback path, not stale ROB debug data.
- Rejected old hypotheses: the new failure is not the previous AUIPC `sp` mismatch. The run aborts before any instruction commits because a new Backend-level merge assertion fires.

## Command

```bash
CCACHE_DIR=/tmp/ccache-codex-review env -u LD_PRELOAD JAVA_TOOL_OPTIONS=-Djdk.net.hosts.file=/tmp/codex-hosts \
  python3 scripts/xiangshan.py ready-to-run/microbench.bin --emulator verilator --config MinimalConfig --threads 1 --trace-fst --max-instr 2000000 --timeout 1800 \
  > mydocs/new-er/task27/logs/baseline-run-microbench-after-rf-diff-fix.log 2>&1
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

- stdout: `mydocs/new-er/task27/logs/baseline-run-microbench-after-rf-diff-fix.log`
- stderr: same combined command log
- wave: `build/2026-06-22-14-42-13_1.fst`
- counters: none useful; failure occurs before observe-only counter comparison
- extra logs: `mydocs/new-er/task27/logs/baseline-build-after-rf-diff-fix.log`

## Symptom

- First bad cycle: simulator reports `cycleCnt = 315`
- PC: abort reports `pc = 0xfffeef3d9cd6d773`
- ROB index: not applicable; abort is a Backend wiring assertion before commit
- logical register: not applicable
- physical register: not applicable
- Difftest or assertion message: `Backend integer commit writeback merge observed multiple writers for one RF writeback port`
- Other symptom: `Core-0 instrCnt = 0`, so the failure blocks baseline smoke before architectural comparison.

## Wave Notes

- Signals inspected: Backend/Region/WbDataPath source wiring and generated assertion owner rather than a detailed data waveform window.
- Relevant cycle window: assertion at the first observed same-lane local writeback overlap, before architectural commit.
- Producer state: each `Region` instantiates a local `WbDataPath`, and each local `WbDataPath` builds an integer writeback arbiter with the global integer RF writeback lane shape.
- Consumer/readDone state: not applicable for the failing assertion; ER is disabled.
- ROB/ST state: not applicable for the failing assertion.
- Free-list/suppress state: not applicable; ER is disabled.
- Difftest state: the direct integer commit writeback event was incorrectly treated as a merge of all local region `intWbArbiterOut` lanes. Current XiangShan integer RF owner wiring forwards `intRegion.io.toIntPreg` to dispatch/busytable and `intRegion.io.fromIntWb`; FP/vector local integer-lane observations are not separate global integer RF write ports.

## Hypothesis

- hypothesis: direct integer Difftest commit writeback and ER producer-ready events should follow the global integer RF owner region, not merge same-shaped local WbDataPath lanes from all regions.
- supporting evidence: `Backend.scala` already uses `intRegion.io.toIntPreg` as the only integer RF writeback owner for dispatch/busytable and integer writeback feedback. The failing merge assertion assumed cross-region same lane valid is illegal, but local WbDataPath lane numbers are local interface lanes, not a globally arbitrated RF port after all regions are combined.
- evidence against: none found after source tracing. `mergeReadDoneRegions` remains a different path because readDone lanes are indexed by global issue dequeue lane, not integer RF writeback port ownership.

## Root Cause

- root cause: Round 42's RF-writeback direct-Difftest bypass added a cross-region merge for `intCommitWriteback` and `intERProducerReady`. That merge asserted at most one valid per lane across all regions, but current XiangShan exposes same-shaped local integer writeback lanes from each region while only the integer region owns the global integer RF writeback path used by dispatch/busytable.
- confidence: high; focused regression now verifies owner-only selection and the source grep confirms the old cross-region merge helpers are gone.

## Fix

- patch or commit: pending commit in Round 42
- files changed: `src/main/scala/xiangshan/backend/Backend.scala`, `src/test/scala/xiangshan/backend/IntEarlyReleaseDataPathTest.scala`
- reason the fix addresses the root cause: `BackendIntEROps.connectIntRfOwnerCommitWriteback` and `connectIntRfOwnerProducerReady` connect the CtrlBlock direct integer writeback events from `intRegion` only. The test drives conflicting non-owner local region events and verifies they are ignored rather than merged or asserted.

## Validation

- command: `CCACHE_DIR=/tmp/ccache-codex-review env -u LD_PRELOAD JAVA_TOOL_OPTIONS=-Djdk.net.hosts.file=/tmp/codex-hosts mill -i xiangshan.test.testOnly xiangshan.backend.IntEarlyReleaseDataPathTest -- -z "select integer RF owner"`
- result: passed, 1 test succeeded
- command: `CCACHE_DIR=/tmp/ccache-codex-review env -u LD_PRELOAD JAVA_TOOL_OPTIONS=-Djdk.net.hosts.file=/tmp/codex-hosts mill -i xiangshan.test.testOnly xiangshan.backend.IntEarlyReleaseDataPathTest`
- result: passed, 11 tests succeeded
- command: `CCACHE_DIR=/tmp/ccache-codex-review env -u LD_PRELOAD JAVA_TOOL_OPTIONS=-Djdk.net.hosts.file=/tmp/codex-hosts mill -i xiangshan.test.testOnly xiangshan.backend.IntEarlyReleaseRobTest -- -z "select direct integer diff write data from same-cycle writeback"`
- result: passed, 1 test succeeded
- command: `CCACHE_DIR=/tmp/ccache-codex-review env -u LD_PRELOAD JAVA_TOOL_OPTIONS=-Djdk.net.hosts.file=/tmp/codex-hosts python3 scripts/xiangshan.py ready-to-run/microbench.bin --emulator verilator --config MinimalConfig --threads 1 --trace-fst --max-instr 2000000 --timeout 1800`
- result: passed after the later RF-shadow fix, with `HIT GOOD TRAP at pc = 0x80003a4e`
- command: `CCACHE_DIR=/tmp/ccache-codex-review env -u LD_PRELOAD JAVA_TOOL_OPTIONS=-Djdk.net.hosts.file=/tmp/codex-hosts python3 scripts/xiangshan.py ready-to-run/microbench.bin --emulator verilator --config IntERObserveOnlyMinimalConfig --threads 1 --trace-fst --max-instr 2000000 --timeout 1800`
- result: passed after clean observe-only rebuild, with `HIT GOOD TRAP at pc = 0x80003a4e`
- hit-good-trap classification: pass for both baseline and observe-only task27 smoke.
- remaining risk: none for the owner-only merge assertion; future config switches still need clean rebuild discipline as recorded in `20260623-120600-observe-only-config-cache.md`.

## Next Action

- next action: rebuild `MinimalConfig`, rerun baseline microbench, then build and run `IntERObserveOnlyMinimalConfig` for observe-only comparison.
