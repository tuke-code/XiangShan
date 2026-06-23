# Observe-Only Config Cache Debug Record

## Summary

- Date: 2026-06-23 12:06:00 CST
- Owner: Codex
- Testcase: observe-only emulator build for `ready-to-run/microbench.bin`
- Result: build wrapper returned exit 0, but the artifact was stale until a clean rebuild was run
- Final trap line: not applicable; this was detected before accepting an observe-only run

## Prior History

- Prior records read:
  - `mydebug/new-er/records/20260622-131325-baseline-microbench-direct-diff.md`
  - `mydebug/new-er/records/20260622-144213-baseline-commit-writeback-owner.md`
  - `mydebug/new-er/records/20260622-153749-baseline-direct-diff-rf-shadow.md`
- Reused observations: task27 must not accept observe-only evidence from a stale baseline binary, especially after direct-Difftest fixes changed backend and Difftest wiring.
- Rejected old hypotheses: this was not a Difftest mismatch or ER counter issue. The run had not started yet; the problem was stale build selection.

## Command

```bash
CCACHE_DIR=/tmp/ccache-codex-review env -u LD_PRELOAD JAVA_TOOL_OPTIONS=-Djdk.net.hosts.file=/tmp/codex-hosts \
  python3 scripts/xiangshan.py --build --emulator verilator --config IntERObserveOnlyMinimalConfig --threads 1 --make-threads 8 --trace-fst --timeout 3600 \
  > mydocs/new-er/task27/logs/observe-only-build.log 2>&1
```

## Configuration

- enable: intended true through `IntERObserveOnlyMinimalConfig`
- observeOnly: intended true through `IntERObserveOnlyMinimalConfig`
- trackEntries: default `IntEarlyReleaseParams.trackEntries`
- earlyFreeWidth: default, functionally gated off by observe-only mode
- conservative redirect policy: true through `WithIntEarlyReleaseObserveOnly`
- commit hash: `79a1e0e0e4` plus dirty working tree
- difftest commit hash: `1631dcd53e` plus dirty working tree

## Artifacts

- stdout: `mydocs/new-er/task27/logs/observe-only-build.log.gz`
- stderr: same combined command log
- wave: none; build-only issue
- counters: none; build-only issue
- extra logs:
  - `mydocs/new-er/task27/logs/clean-before-observe-only-build.log.gz`
  - `mydocs/new-er/task27/logs/observe-only-build-clean.log.gz`
  - `build/generated-src/difftest_profile.json`

## Symptom

- First bad cycle: not applicable
- PC: not applicable
- ROB index: not applicable
- logical register: not applicable
- physical register: not applicable
- Difftest or assertion message: none
- Other symptom: `observe-only-build.log` completed in about 2 seconds and `build/generated-src/difftest_profile.json` still recorded `MinimalConfig` after the first observe-only build command.

## Wave Notes

- Signals inspected: none; build artifact metadata was inspected instead.
- Relevant cycle window: not applicable.
- Producer state: not applicable.
- Consumer/readDone state: not applicable.
- ROB/ST state: not applicable.
- Free-list/suppress state: not applicable.
- Difftest state: generated profile still named `MinimalConfig`, proving the binary was not valid observe-only evidence.

## Hypothesis

- hypothesis: the build target does not include the `CONFIG` value in the dependency key, so switching `--config` can reuse an existing generated RTL and emulator.
- supporting evidence: `observe-only-build.log` said `CONFIG=IntERObserveOnlyMinimalConfig`, but `build/generated-src/difftest_profile.json` still recorded `MinimalConfig` and no Chisel elaboration occurred in that first build log.
- evidence against: none after `python3 scripts/xiangshan.py --clean` followed by the same observe-only build regenerated RTL and the profile then recorded `IntERObserveOnlyMinimalConfig`.

## Root Cause

- root cause: current Makefile generation targets are file-based and do not force regeneration when only `CONFIG` changes. `build/rtl/SimTop.sv` can remain up to date even though a different Scala config is requested.
- confidence: high.

## Fix

- patch or commit: no source change in this round
- files changed: documentation only
- reason the fix addresses the root cause: the accepted task27 flow explicitly runs `python3 scripts/xiangshan.py --clean` before switching from `MinimalConfig` to `IntERObserveOnlyMinimalConfig`, then verifies the generated profile records the observe-only config.

## Validation

- command:

```bash
CCACHE_DIR=/tmp/ccache-codex-review env -u LD_PRELOAD JAVA_TOOL_OPTIONS=-Djdk.net.hosts.file=/tmp/codex-hosts \
  python3 scripts/xiangshan.py --clean --timeout 3600 \
  > mydocs/new-er/task27/logs/clean-before-observe-only-build.log 2>&1

CCACHE_DIR=/tmp/ccache-codex-review env -u LD_PRELOAD JAVA_TOOL_OPTIONS=-Djdk.net.hosts.file=/tmp/codex-hosts \
  python3 scripts/xiangshan.py --build --emulator verilator --config IntERObserveOnlyMinimalConfig --threads 1 --make-threads 8 --trace-fst --timeout 3600 \
  > mydocs/new-er/task27/logs/observe-only-build-clean.log 2>&1
```

- result: clean exit 0, clean observe-only build exit 0, `build/generated-src/difftest_profile.json` records `IntERObserveOnlyMinimalConfig`.
- hit-good-trap classification: subsequent observe-only microbench run reached final `HIT GOOD TRAP at pc = 0x80003a4e`.
- remaining risk: future local config-switch builds should either clean first or add a build-system guard that makes config changes invalidate generated RTL.

## Next Action

- next action: keep using clean rebuilds when switching emulator configs in this task lane; consider a separate build-system improvement later if repeated config switching remains error-prone.
