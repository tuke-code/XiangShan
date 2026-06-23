# Functional UCA Reuse Debug Record

## Summary

- Date: 2026-06-23 15:39:28 CST
- Owner: Codex
- Testcase: `ready-to-run/microbench.bin`
- Result: fail before fix, pass after fix
- Final trap line before fix: none; run aborted on an `IntSparseUCA` assertion
- Final trap line after fix: `Core 0: HIT GOOD TRAP at pc = 0x80003a4e`

## Prior History

- Prior records read:
  - `mydebug/new-er/records/20260622-131325-baseline-microbench-direct-diff.md`
  - `mydebug/new-er/records/20260622-144213-baseline-commit-writeback-owner.md`
  - `mydebug/new-er/records/20260622-153749-baseline-direct-diff-rf-shadow.md`
  - `mydebug/new-er/records/20260623-120600-observe-only-config-cache.md`
- Reused observations: direct integer Difftest commit data must use the integer RF owner path and a ROB-side write-data shadow; config switches require `scripts/xiangshan.py --clean`.
- Rejected old hypotheses: this was not stale generated RTL after the clean build, and it was not an integer direct-Difftest architectural mismatch. The run reached a late UCA assertion after many microbench kernels had already passed.

## Command

Failing run:

```bash
CCACHE_DIR=/tmp/ccache-codex-review env -u LD_PRELOAD JAVA_TOOL_OPTIONS=-Djdk.net.hosts.file=/tmp/codex-hosts \
  python3 scripts/xiangshan.py ready-to-run/microbench.bin --emulator verilator \
  --config IntERFunctionalMinimalConfig --threads 1 --trace-fst \
  --max-instr 2000000 --timeout 1800 \
  > mydocs/new-er/task28/logs/functional-run-microbench.log 2>&1
```

Validation run:

```bash
CCACHE_DIR=/tmp/ccache-codex-review env -u LD_PRELOAD JAVA_TOOL_OPTIONS=-Djdk.net.hosts.file=/tmp/codex-hosts \
  python3 scripts/xiangshan.py ready-to-run/microbench.bin --emulator verilator \
  --config IntERFunctionalMinimalConfig --threads 1 --trace-fst \
  --max-instr 2000000 --timeout 1800 \
  > mydocs/new-er/task28/logs/functional-run-microbench-after-uca-fix.log 2>&1
```

## Configuration

- enable: true through `IntERFunctionalMinimalConfig`
- observeOnly: false
- trackEntries: default 16
- earlyFreeWidth: 1
- conservative redirect policy: true
- commit hash: `2f3595e85` plus dirty working tree
- difftest commit hash: `0657aff7a`

## Artifacts

- failing stdout: `mydocs/new-er/task28/logs/functional-run-microbench.log.gz`
- failing stderr: same combined command log
- failing wave: `build/2026-06-23-14-43-26_309684.fst` was generated during the failure, then removed by the required clean rebuild before the final validation run
- validation stdout: `mydocs/new-er/task28/logs/functional-run-microbench-after-uca-fix.log.gz`
- validation stderr: same combined command log
- counters: embedded in `functional-run-microbench-after-uca-fix.log.gz`
- extra logs:
  - `mydocs/new-er/task28/logs/clean-before-functional-rebuild-after-uca-fix.log.gz`
  - `mydocs/new-er/task28/logs/functional-build-after-uca-fix.log.gz`

## Symptom

- First bad cycle: assertion reported near final `cycleCnt = 318500`
- PC: abort at `0x800021f6`
- ROB index: not isolated in the final record; the failing assertion came from rename-time redef tracking
- logical register: not isolated
- physical register: old `pdest` hit a released UCA entry after that physical register had been returned to the free list
- Difftest or assertion message: `IntSparseUCA redef matched an already released entry without an active owner`
- Other symptom: `Core-0 instrCnt = 321683, cycleCnt = 318500`

## Wave Notes

- Signals inspected: `backend.inner_ctrlBlock.rename.intUCA` entry state, `pdest`, `earlyFreeIssued`, and `redefMatchOH`.
- Relevant cycle window: around the abort near cycle 318500.
- Producer state: an old tracked entry had already moved to `releasedWaitCommit` and had issued early free.
- Consumer/readDone state: not the failing boundary; ROB readDone validation had already been active in the run.
- ROB/ST state: commit suppress identity remained precise and still used `trackId`, generation, old `pdest`, commit old `pdest`, and redefiner ROB index.
- Free-list/suppress state: the old `pdest` was already legally available for reallocation when the entry was in `releasedWaitCommit`.
- Difftest state: no Difftest mismatch was observed before the assertion.

## Hypothesis

- hypothesis: the rename-time assertion was too strong for sparse tracking. A released-only `pdest` hit without an active tracked owner can be legal after the physical register has been early-freed and reused by an untracked newer producer.
- supporting evidence: with finite tracking, allocation of the reused `pdest` can be untracked when all UCA entries are occupied or waiting for commit. A later redefiner of that untracked owner can present the same old `pdest` to UCA, but rename only has the old physical destination and cannot distinguish it from the older released entry.
- evidence against: none after the directed `trackEntries = 1` regression reproduced the assertion before the fix and passed after removing the assertion.

## Root Cause

- root cause: `IntSparseUCA` asserted whenever a rename-time redef probe matched a `releasedWaitCommit` entry without an active owner match. That check assumed the released physical register could not have been reallocated to an untracked owner before the old redefiner committed. Sparse UCA intentionally allows such untracked ownership, so the assertion rejected a legal reuse case.
- confidence: high.

## Fix

- patch or commit: pending in this round
- files changed:
  - `src/main/scala/xiangshan/backend/IntSparseUCA.scala`
  - `src/test/scala/xiangshan/backend/IntSparseUCATest.scala`
- reason the fix addresses the root cause: the rename-time released-only assertion was removed. Commit-time suppress remains identity-based and precise, so a true conventional-free suppress still requires matching tracked identity rather than a raw `pdest` hit.

## Validation

- command:

```bash
CCACHE_DIR=/tmp/ccache-codex-review env -u LD_PRELOAD JAVA_TOOL_OPTIONS=-Djdk.net.hosts.file=/tmp/codex-hosts \
  mill -i xiangshan.test.testOnly xiangshan.backend.IntSparseUCATest -- -z 'ignore a released pdest match when the newer owner is untracked'
```

- result: passed after the fix
- command:

```bash
CCACHE_DIR=/tmp/ccache-codex-review env -u LD_PRELOAD JAVA_TOOL_OPTIONS=-Djdk.net.hosts.file=/tmp/codex-hosts \
  mill -i xiangshan.test.testOnly xiangshan.backend.IntSparseUCATest
```

- result: passed, 17 tests succeeded
- command:

```bash
CCACHE_DIR=/tmp/ccache-codex-review env -u LD_PRELOAD JAVA_TOOL_OPTIONS=-Djdk.net.hosts.file=/tmp/codex-hosts \
  python3 scripts/xiangshan.py ready-to-run/microbench.bin --emulator verilator \
  --config IntERFunctionalMinimalConfig --threads 1 --trace-fst \
  --max-instr 2000000 --timeout 1800 \
  > mydocs/new-er/task28/logs/functional-run-microbench-after-uca-fix.log 2>&1
```

- result: passed with `MicroBench PASS`
- hit-good-trap classification: pass, final `HIT GOOD TRAP at pc = 0x80003a4e`
- remaining risk: broader emu-basics coverage is still needed after this smoke.

## Next Action

- next action: continue with broader functional emulator coverage using the same clean-before-config-switch rule.
