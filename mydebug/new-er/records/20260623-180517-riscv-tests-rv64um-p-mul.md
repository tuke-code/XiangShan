# RISC-V Tests rv64um-p-mul Debug Record

## Summary

- Date: 2026-06-23 18:05:17 CST
- Owner: Codex
- Testcase: `riscv-tests`, first failing subtest `/nfs/home/share/ci-workloads/riscv-tests/isa/build/rv64um-p-mul.bin`
- Result: fail before fix, focused workload pass after fix
- Final trap line: none for the failing subtest; run aborted in Difftest at `pc = 0x800000bc`

## Prior History

- Prior records read:
  - `mydebug/new-er/records/20260622-131325-baseline-microbench-direct-diff.md`
  - `mydebug/new-er/records/20260622-144213-baseline-commit-writeback-owner.md`
  - `mydebug/new-er/records/20260622-153749-baseline-direct-diff-rf-shadow.md`
  - `mydebug/new-er/records/20260623-120600-observe-only-config-cache.md`
  - `mydebug/new-er/records/20260623-153928-functional-uca-reuse.md`
- Reused observations: direct integer Difftest must not use `pregs_xrf + rat_xrf`, must source integer commit data from the integer RF writeback owner path, and must retain RF writeback data in a ROB-side shadow until commit. Config switches require `scripts/xiangshan.py --clean`.
- Rejected old hypotheses: this failure is not the previous UCA released-entry reuse assertion. It is also not a stale config artifact because the functional emulator build evidence records `IntERFunctionalMinimalConfig`. The data corruption is visible in the direct integer Difftest shadow path.

## Command

```bash
mkdir -p mydocs/new-er/task28/round1/matrix/riscv-tests mydocs/new-er/task28/round1/waves/riscv-tests
CCACHE_DIR=/tmp/ccache-codex-review env -u LD_PRELOAD JAVA_TOOL_OPTIONS=-Djdk.net.hosts.file=/tmp/codex-hosts \
  python3 scripts/xiangshan.py --wave-dump mydocs/new-er/task28/round1/waves/riscv-tests \
  --threads 8 --ci riscv-tests --rvtest /nfs/home/share/ci-workloads/riscv-tests \
  > mydocs/new-er/task28/round1/matrix/riscv-tests/stdout.log \
  2> mydocs/new-er/task28/round1/matrix/riscv-tests/stderr.log
```

## Configuration

- enable: true through `IntERFunctionalMinimalConfig`
- observeOnly: false
- trackEntries: default 16
- earlyFreeWidth: 1
- conservative redirect policy: true
- commit hash: `879555ff9` plus dirty working tree
- difftest commit hash: `0657aff7a`

## Artifacts

- stdout: `mydocs/new-er/task28/round1/matrix/riscv-tests/stdout.log.gz`
- stderr: `mydocs/new-er/task28/round1/matrix/riscv-tests/stderr.log.gz`
- wave: `mydocs/new-er/task28/round1/waves/riscv-tests/2026-06-23-16-41-17_1.fst`
- counters: embedded in stdout/stderr; failure occurs early before useful functional ER counter summary
- extra logs:
  - `mydocs/new-er/task28/round1/matrix/riscv-tests/exit_code.txt`
  - local converted VCD for inspection only: `/tmp/riscv_mul_fail.vcd`
  - copied emulator: `mydocs/new-er/task28/round1/waves/riscv-tests/emu`
  - original command redirected to `.log` files; committed artifacts are gzip archives of those logs

## Symptom

- First bad cycle: Difftest abort reports `cycleCnt = 2125`
- PC: `0x800000bc`
- ROB index: `0x24` in the wave window
- logical register: `x1/ra` and `x2/sp`
- physical register: `p10` and `p20` in the focused wave notes; exact physical names are from ROB direct-diff writeback metadata
- Difftest or assertion message:
  - `ra different at pc = 0x00800000bc, right = 0x0000000000007e00, wrong = 0x0000000006db6db7`
  - `sp different at pc = 0x00800000bc, right = 0x0000000006db6db7, wrong = 0x0000000000000000`
- Other symptom: `Core-0 instrCnt = 42, cycleCnt = 2125`; previous subtest `rv64ui-p-lb.bin` reached `HIT GOOD TRAP`.

## Wave Notes

- Signals inspected: ROB direct integer shadow, direct integer commit data, delayed integer RF writeback shadow, RAB commit metadata, and direct `xrf` outputs.
- Relevant cycle window: VCD timestamps around `#2123` through `#2125`.
- Producer state:
  - At `#2123`, two integer RF writebacks share `robIdx = 0x24`: one carries `0x7e00`, and another carries `0x6db6db7`.
  - This matches the test sequence that builds `ra = 0x7e00` and an intermediate `sp` value before the multiply check.
- Consumer/readDone state: not implicated; the failure is in Difftest architectural visibility, not UCA readDone accounting.
- ROB/ST state:
  - A compressed ROB entry can correspond to multiple RAB integer destination commits.
  - ROB commit lanes expose only `CommitWidth` compressed entries, while `Rab.diffCommits` expands destination commits with `CommitWidth * MaxUopSize` lanes.
- Free-list/suppress state: no duplicate free or suppress assertion is involved in this failure.
- Difftest state:
  - Before the fix, ROB direct integer write-data shadow was keyed only by `robIdx`, so the later writeback for the same compressed ROB entry overwrote the earlier one.
  - A partial `(robIdx, pdest)` shadow was necessary but not sufficient because direct architectural `xrf` update still used ROB compressed commit lanes instead of RAB-expanded destination commits.
  - The failing direct shadow wrote `x1` with `0x6db6db7` and left `x2` stale, matching the Difftest mismatch.

## Hypothesis

- hypothesis: ROB direct integer Difftest architectural shadow must update from RAB-expanded integer destination commits, keyed by `(robIdx, pdest)`, rather than from compressed ROB commit lanes keyed only by ROB index.
- supporting evidence:
  - `Rab.diffCommits` already provides per-destination commit information used by rename's architectural table.
  - The failing compressed ROB entry has two integer destinations and two integer RF writebacks with the same `robIdx`.
  - The direct shadow corruption exactly matches a single ROB-lane update choosing the wrong data and missing the second destination update.
- evidence against: none after the focused expanded-commit ChiselSim regression reproduced the required behavior and passed with the new selector/update path.

## Root Cause

- root cause: direct integer Difftest shadow state was updated at the compressed ROB commit granularity. That path cannot represent multiple integer architectural destinations inside one compressed ROB entry. The original write-data shadow was also keyed only by `robIdx`, allowing one writeback for a compressed entry to overwrite another.
- confidence: high.

## Fix

- patch or commit: dirty working-tree patch in this round; commit pending after system validation
- files changed:
  - `src/main/scala/xiangshan/Bundle.scala`
  - `src/main/scala/xiangshan/backend/IntEarlyReleaseBundles.scala`
  - `src/main/scala/xiangshan/backend/datapath/WbArbiter.scala`
  - `src/main/scala/xiangshan/backend/rob/Rab.scala`
  - `src/main/scala/xiangshan/backend/rob/Rob.scala`
  - `src/main/scala/xiangshan/backend/rob/RobBundles.scala`
  - `src/test/scala/xiangshan/backend/IntEarlyReleaseDataPathTest.scala`
  - `src/test/scala/xiangshan/backend/IntEarlyReleaseRobTest.scala`
- reason the fix addresses the root cause:
  - `IntCommitWriteback` now carries `pdest`.
  - ROB direct integer write-data shadow stores multiple `(pdest, data)` slots per ROB entry.
  - `DiffCommitIO` now carries RAB-side `robIdx`, and `RabCommitInfo` carries `moveSrcLReg`.
  - ROB direct integer architectural shadow updates from `rab.io.diffCommits` expanded destination lanes, selecting write data by `(robIdx, pdest)` and preserving move-elimination source semantics.

## Validation

- command:

```bash
CCACHE_DIR=/tmp/ccache-codex-review env -u LD_PRELOAD JAVA_TOOL_OPTIONS=-Djdk.net.hosts.file=/tmp/codex-hosts \
  mill -i xiangshan.test.testOnly xiangshan.backend.IntEarlyReleaseRobTest -- -z "expanded commits"
```

- result: passed, 1 test succeeded.
- command:

```bash
CCACHE_DIR=/tmp/ccache-codex-review env -u LD_PRELOAD JAVA_TOOL_OPTIONS=-Djdk.net.hosts.file=/tmp/codex-hosts \
  mill -i xiangshan.test.testOnly xiangshan.backend.IntEarlyReleaseRobTest
```

- result: passed, 18 tests succeeded.
- command:

```bash
CCACHE_DIR=/tmp/ccache-codex-review env -u LD_PRELOAD JAVA_TOOL_OPTIONS=-Djdk.net.hosts.file=/tmp/codex-hosts \
  mill -i xiangshan.test.testOnly xiangshan.backend.IntEarlyReleaseDataPathTest
```

- result: passed, 11 tests succeeded.
- command:

```bash
CCACHE_DIR=/tmp/ccache-codex-review env -u LD_PRELOAD JAVA_TOOL_OPTIONS=-Djdk.net.hosts.file=/tmp/codex-hosts \
  mill -i difftest.test.testOnly difftest.PreprocessTest
```

- result: passed, 6 tests succeeded.
- command:

```bash
CCACHE_DIR=/tmp/ccache-codex-review env -u LD_PRELOAD JAVA_TOOL_OPTIONS=-Djdk.net.hosts.file=/tmp/codex-hosts \
  mill -i xiangshan.test.testOnly xiangshan.backend.IntEarlyReleaseBundlesTest
```

- result: passed, 28 tests succeeded.
- command:

```bash
mkdir -p mydocs/new-er/task28/round1/matrix/riscv-tests-rv64um-p-mul-rerun mydocs/new-er/task28/round1/waves/riscv-tests-rv64um-p-mul-rerun
CCACHE_DIR=/tmp/ccache-codex-review env -u LD_PRELOAD JAVA_TOOL_OPTIONS=-Djdk.net.hosts.file=/tmp/codex-hosts \
  python3 scripts/xiangshan.py --wave-dump mydocs/new-er/task28/round1/waves/riscv-tests-rv64um-p-mul-rerun \
  --threads 8 /nfs/home/share/ci-workloads/riscv-tests/isa/build/rv64um-p-mul.bin \
  > mydocs/new-er/task28/round1/matrix/riscv-tests-rv64um-p-mul-rerun/stdout.log \
  2> mydocs/new-er/task28/round1/matrix/riscv-tests-rv64um-p-mul-rerun/stderr.log
```

- result: passed, exit code 0; final `Core 0: HIT GOOD TRAP at pc = 0x8000055c`.
- hit-good-trap classification: focused failing workload now passes by final hit-good-trap.
- remaining risk: the direct shadow update now iterates the full RAB diff lane shape. This is Difftest/basic-debug-only logic, but the full `riscv-tests` matrix rerun is still required before closing task28.

## Next Action

- next action: rerun the full `riscv-tests` matrix using the rebuilt `IntERFunctionalMinimalConfig` emulator, then continue the remaining emu-basics entries if the matrix reaches final hit-good-trap on every subtest.
