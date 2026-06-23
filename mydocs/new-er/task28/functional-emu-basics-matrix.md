# Functional Int ER Emu-Basics Matrix

## Scope

This document records the Round 1 functional run for int early register release using
`IntERFunctionalMinimalConfig`.

Build evidence:

- clean before config-sensitive rebuild: `mydocs/new-er/task28/round1/logs/clean-before-rab-expanded-diff-rebuild.log.gz`
- functional build log: `mydocs/new-er/task28/round1/logs/functional-matrix-build-after-rab-expanded-diff.log.gz`
- generated profile: `build/generated-src/difftest_profile.json` contains `IntERFunctionalMinimalConfig`
- build mode: Verilator, `--trace-fst`, `--threads 8`, `--make-threads 8`
- local CI deviation: `--numa` was not used because this shell lacks the required `psutil` package
- local CI deviation: `--config IntERFunctionalMinimalConfig` was used intentionally; CI default config is not the ER target
- local CI deviation: BOLT PGO was unavailable and the build log records fallback to instrumentation-based PGO

The final emulator was FST-capable. Per-workload wave directories were created under
`mydocs/new-er/task28/round1/waves/`. Zero-exit workloads did not emit new FST files; the earlier
`riscv-tests` debug failure did emit `mydocs/new-er/task28/round1/waves/riscv-tests/2026-06-23-16-41-17_1.fst`
and is documented in `mydebug/new-er/records/20260623-180517-riscv-tests-rv64um-p-mul.md`.

## Classification Rules

- Exit code must be 0 for every workload.
- Workloads that normally terminate by trap are classified by the final `HIT GOOD TRAP` line.
- `rvh-test` style intermediate `FAILED` text is not a failure when the final result reaches `HIT GOOD TRAP`.
- `povray` is run with `--max-instr 5000000` in emu-basics. It is classified by exit code 0 and final
  `EXCEEDING CYCLE/INSTR LIMIT`, not by good trap.
- No new system-level failure occurred after the RAB-expanded direct integer Difftest fix, so no new
  `mydebug/new-er/records/` entry was required during the final matrix sweep.

## Matrix

| Workload | Command | Exit | Seed | Final classification | Evidence |
|----------|---------|------|------|----------------------|----------|
| `cputest` | `python3 scripts/xiangshan.py --wave-dump mydocs/new-er/task28/round1/waves/cputest --threads 8 --ci cputest` | 0 | 4487 | 33 final good traps | `mydocs/new-er/task28/round1/matrix/cputest/stdout.log.gz`, `stderr.log.gz`, `exit_code.txt` |
| `riscv-tests` | `python3 scripts/xiangshan.py --wave-dump mydocs/new-er/task28/round1/waves/riscv-tests-rerun --threads 8 --ci riscv-tests --rvtest /nfs/home/share/ci-workloads/riscv-tests` | 0 | 7834 | 117 final good traps | `mydocs/new-er/task28/round1/matrix/riscv-tests-rerun/stdout.log.gz`, `stderr.log.gz`, `exit_code.txt` |
| `misc-tests` | `python3 scripts/xiangshan.py --wave-dump mydocs/new-er/task28/round1/waves/misc-tests --threads 8 --ci misc-tests` | 0 | 7070 | 17 final good traps; includes `rvh_test.bin` with intermediate `FAILED` text and final good trap | `mydocs/new-er/task28/round1/matrix/misc-tests/stdout.log.gz`, `stderr.log.gz`, `exit_code.txt` |
| `rvh-tests` | `python3 scripts/xiangshan.py --wave-dump mydocs/new-er/task28/round1/waves/rvh-tests --threads 8 --ci rvh-tests` | 0 | 7414 | 5 final good traps; `rvh_test.bin` reached final good trap despite intermediate `FAILED` text | `mydocs/new-er/task28/round1/matrix/rvh-tests/stdout.log.gz`, `stderr.log.gz`, `exit_code.txt` |
| `microbench` | `python3 scripts/xiangshan.py --wave-dump mydocs/new-er/task28/round1/waves/microbench --threads 8 --ci microbench` | 0 | 900 | `HIT GOOD TRAP at pc = 0x80003a4e`; `instrCnt = 326376`, `cycleCnt = 335693`, `IPC = 0.972245` | `mydocs/new-er/task28/round1/matrix/microbench/stdout.log.gz`, `stderr.log.gz`, `exit_code.txt` |
| `coremark` | `python3 scripts/xiangshan.py --wave-dump mydocs/new-er/task28/round1/waves/coremark --threads 8 --ci coremark` | 0 | 1701 | `HIT GOOD TRAP at pc = 0x80001cbc`; `instrCnt = 3210836`, `cycleCnt = 2097524`, `IPC = 1.530774` | `mydocs/new-er/task28/round1/matrix/coremark/stdout.log.gz`, `stderr.log.gz`, `exit_code.txt` |
| `linux-hello-opensbi` | `python3 scripts/xiangshan.py --wave-dump mydocs/new-er/task28/round1/waves/linux-hello-opensbi --threads 8 --ci linux-hello-opensbi` | 0 | 1227 | `Hello, XiangShan!`; `HIT GOOD TRAP at pc = 0x100a0`; `instrCnt = 14448775`, `cycleCnt = 10634621`, `IPC = 1.358654` | `mydocs/new-er/task28/round1/matrix/linux-hello-opensbi/stdout.log.gz`, `stderr.log.gz`, `exit_code.txt` |
| `iopmp-test` | `python3 scripts/xiangshan.py --wave-dump mydocs/new-er/task28/round1/waves/iopmp-test --threads 8 --ci iopmp-test --no-diff` | 0 | 4559 | `HIT GOOD TRAP at pc = 0x8000047c`; `instrCnt = 4934`, `cycleCnt = 31308`, `IPC = 0.157596` | `mydocs/new-er/task28/round1/matrix/iopmp-test/stdout.log.gz`, `stderr.log.gz`, `exit_code.txt` |
| `povray` | `python3 scripts/xiangshan.py --wave-dump mydocs/new-er/task28/round1/waves/povray --threads 8 --ci povray --max-instr 5000000 --gcpt-restore-bin /nfs/home/share/ci-workloads/fix-gcpt/gcpt.bin` | 0 | 1104 | Expected instruction limit: `EXCEEDING CYCLE/INSTR LIMIT at pc = 0x4979a`; `instrCnt = 5000001`, `cycleCnt = 5366181`, `IPC = 0.931762` | `mydocs/new-er/task28/round1/matrix/povray/stdout.log.gz`, `stderr.log.gz`, `exit_code.txt` |
| `copy_and_run` | `python3 scripts/xiangshan.py --wave-dump mydocs/new-er/task28/round1/waves/copy_and_run --threads 8 --flash ready-to-run/copy_and_run.bin ready-to-run/microbench.bin` | 0 | 7102 | `copy finish`; `HIT GOOD TRAP at pc = 0x80001ca0`; `instrCnt = 352563`, `cycleCnt = 996623`, `IPC = 0.353758` | `mydocs/new-er/task28/round1/matrix/copy_and_run/stdout.log.gz`, `stderr.log.gz`, `exit_code.txt` |
| `f16_test` | `python3 scripts/xiangshan.py --wave-dump mydocs/new-er/task28/round1/waves/f16_test --threads 8 --ci f16_test` | 0 | 565 | 12 final good traps across F16 subtests | `mydocs/new-er/task28/round1/matrix/f16_test/stdout.log.gz`, `stderr.log.gz`, `exit_code.txt` |
| `zcb-test` | `python3 scripts/xiangshan.py --wave-dump mydocs/new-er/task28/round1/waves/zcb-test --threads 8 --ci zcb-test` | 0 | 7021 | `test pass`; `HIT GOOD TRAP at pc = 0x8000053a`; `instrCnt = 32101`, `cycleCnt = 30910`, `IPC = 1.038531` | `mydocs/new-er/task28/round1/matrix/zcb-test/stdout.log.gz`, `stderr.log.gz`, `exit_code.txt` |

## Debugged Regression

The first full `riscv-tests` attempt exposed a direct integer Difftest issue in `rv64um-p-mul`.
The root cause was compressed ROB commit granularity in the direct integer architectural shadow:
one compressed ROB entry can carry multiple integer destinations, and the old shadow was keyed only
by ROB index. The fix keys stored write data by `(robIdx, pdest)` and updates direct integer
architectural shadow state from RAB-expanded commit lanes.

Evidence:

- debug record: `mydebug/new-er/records/20260623-180517-riscv-tests-rv64um-p-mul.md`
- focused rerun: `mydocs/new-er/task28/round1/matrix/riscv-tests-rv64um-p-mul-rerun/stdout.log.gz`
- focused result: exit 0, `HIT GOOD TRAP at pc = 0x8000055c`
- full rerun: `mydocs/new-er/task28/round1/matrix/riscv-tests-rerun/stdout.log.gz`
- full rerun result: exit 0, 117 final good traps

## ER Counter Visibility

No ER-named perf lines were emitted in the final matrix stdout/stderr logs. The available final
counters for single-workload rows are the emulator-reported `instrCnt`, `cycleCnt`, and `IPC` shown
above. Earlier functional smoke evidence still records functional ER activity counters:
`mydocs/new-er/task28/functional-smoke.md`.

## Artifact Notes

- Matrix stdout/stderr logs and build logs were gzip-compressed after classification to keep the
  evidence tree manageable. Use `gzip -dc <stdout.log.gz>` / `gzip -dc <stderr.log.gz>` or `zgrep`
  for inspection.
- The `mydocs/new-er/task28/round1/matrix/riscv-tests/` directory is retained as the original
  failing attempt. The accepted matrix row is `riscv-tests-rerun`.
- The `mydocs/new-er/task28/round1/waves/riscv-tests/` directory contains the FST from the original
  `rv64um-p-mul` failure; the final rerun did not emit a failure wave.
