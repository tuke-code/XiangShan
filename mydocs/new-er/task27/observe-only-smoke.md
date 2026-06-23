# Int ER Observe-Only Emulator Smoke

## Summary

- Date: 2026-06-23 CST
- Scope: baseline-disabled and observe-only `ready-to-run/microbench.bin` smoke on the Verilator emulator.
- Baseline config: `MinimalConfig`
- Observe-only config: `IntERObserveOnlyMinimalConfig`
- Observe-only parameters from `src/main/scala/top/Configs.scala`: `enable = true`, `observeOnly = true`, `conservativeRedirectKill = true`.
- Functional early-free status: disabled by observe-only mode. The free-list early-free counters stayed at zero.
- Pass rule: final `HIT GOOD TRAP` is required. Both smoke runs reached final `HIT GOOD TRAP`.

## Commands

Common environment setup:

```bash
printf '127.0.0.1 node007.bosccluster.com node007\n' > /tmp/codex-hosts
mkdir -p /tmp/ccache-codex-review mydocs/new-er/task27/logs
```

The commands below wrote raw `.log` files. The committed evidence archives are the matching
`mydocs/new-er/task27/logs/*.log.gz` files, because raw emulator logs contain intentional
spacing and benchmark separator lines that are unsuitable for Git whitespace checking.

Baseline-disabled build:

```bash
CCACHE_DIR=/tmp/ccache-codex-review env -u LD_PRELOAD JAVA_TOOL_OPTIONS=-Djdk.net.hosts.file=/tmp/codex-hosts \
  python3 scripts/xiangshan.py --build --emulator verilator --config MinimalConfig --threads 1 --make-threads 8 --trace-fst --timeout 3600 \
  > mydocs/new-er/task27/logs/baseline-build-after-rf-shadow-fix.log 2>&1
```

Baseline-disabled run:

```bash
CCACHE_DIR=/tmp/ccache-codex-review env -u LD_PRELOAD JAVA_TOOL_OPTIONS=-Djdk.net.hosts.file=/tmp/codex-hosts \
  python3 scripts/xiangshan.py ready-to-run/microbench.bin --emulator verilator --config MinimalConfig --threads 1 --trace-fst --max-instr 2000000 --timeout 1800 \
  > mydocs/new-er/task27/logs/baseline-run-microbench-after-rf-shadow-fix.log 2>&1
```

Clean before switching emulator config:

```bash
CCACHE_DIR=/tmp/ccache-codex-review env -u LD_PRELOAD JAVA_TOOL_OPTIONS=-Djdk.net.hosts.file=/tmp/codex-hosts \
  python3 scripts/xiangshan.py --clean --timeout 3600 \
  > mydocs/new-er/task27/logs/clean-before-observe-only-build.log 2>&1
```

Observe-only build after clean:

```bash
CCACHE_DIR=/tmp/ccache-codex-review env -u LD_PRELOAD JAVA_TOOL_OPTIONS=-Djdk.net.hosts.file=/tmp/codex-hosts \
  python3 scripts/xiangshan.py --build --emulator verilator --config IntERObserveOnlyMinimalConfig --threads 1 --make-threads 8 --trace-fst --timeout 3600 \
  > mydocs/new-er/task27/logs/observe-only-build-clean.log 2>&1
```

Observe-only run:

```bash
CCACHE_DIR=/tmp/ccache-codex-review env -u LD_PRELOAD JAVA_TOOL_OPTIONS=-Djdk.net.hosts.file=/tmp/codex-hosts \
  python3 scripts/xiangshan.py ready-to-run/microbench.bin --emulator verilator --config IntERObserveOnlyMinimalConfig --threads 1 --trace-fst --max-instr 2000000 --timeout 1800 \
  > mydocs/new-er/task27/logs/observe-only-run-microbench.log 2>&1
```

## Results

| Run | Build result | Run result | Final trap | Instruction count | Cycle count |
|-----|--------------|------------|------------|-------------------|-------------|
| Baseline disabled | exit 0; `build/verilator-compile/emu` produced | exit 0; elapsed 453s | `HIT GOOD TRAP at pc = 0x80003a4e` | 326375 | 335525 |
| Observe-only | clean exit 0, build exit 0; `build/generated-src/difftest_profile.json` records `IntERObserveOnlyMinimalConfig` | exit 0; elapsed 464s | `HIT GOOD TRAP at pc = 0x80003a4e` | 326375 | 335525 |

Baseline evidence:

- `mydocs/new-er/task27/logs/baseline-run-microbench-after-rf-shadow-fix.log.gz`
- `MicroBench PASS`
- Final line excerpt: `HIT GOOD TRAP at pc = 0x80003a4e`
- Final count excerpt: `Core-0 instrCnt = 326375, cycleCnt = 335525, IPC = 0.972729`

Observe-only evidence:

- `mydocs/new-er/task27/logs/observe-only-build-clean.log.gz`
- `mydocs/new-er/task27/logs/observe-only-run-microbench.log.gz`
- `build/generated-src/difftest_profile.json` contains `--config`, `IntERObserveOnlyMinimalConfig`.
- `MicroBench PASS`
- Final line excerpt: `HIT GOOD TRAP at pc = 0x80003a4e`
- Final count excerpt: `Core-0 instrCnt = 326375, cycleCnt = 335525, IPC = 0.972729`

## Observe-Only Counters

The observe-only run collected ER activity while keeping functional free-list release inactive:

| Counter | Baseline disabled | Observe-only |
|---------|-------------------|--------------|
| `int_er_me_freelist_early_free_req` | 0 | 0 |
| `int_er_me_freelist_early_free_merged` | 0 | 0 |
| `int_er_uc_early_free_opportunity` | absent, ER disabled | 2355 |
| `int_er_uc_early_free` | absent, ER disabled | 0 |
| `int_er_uc_commit_suppress` | absent, ER disabled | 0 |
| `int_er_rename_alloc` | absent, ER disabled | 240362 |
| `int_er_rename_dest_track` | absent, ER disabled | 144344 |
| `int_er_rename_src_track` | absent, ER disabled | 218774 |
| `int_er_datapath_tracked_read_obs` | absent, ER disabled | 205862 |
| `int_er_rob_raw_read_done` | absent, ER disabled | 205258 |

Important observe-only lines:

- `int_er_uc_early_free_opportunity, 2355`
- `int_er_uc_early_free, 0`
- `int_er_uc_commit_suppress, 0`
- `int_er_me_freelist_early_free_req, 0`
- `int_er_me_freelist_early_free_merged, 0`

This is the expected separation for observe-only mode: the tracker, ROB validation, DataPath observation and opportunity counters are active, but the integer free-list does not receive functional early-free requests and conventional free suppression does not occur.

## Config Switch Note

The first observe-only build command without a clean returned exit 0 but did not regenerate RTL: `build/generated-src/difftest_profile.json` still recorded `MinimalConfig`. The root cause is the current Makefile dependency shape: `build/rtl/SimTop.sv` does not depend on the `CONFIG` value, so switching `--config` can reuse a stale RTL target unless the build directory is removed first.

That observation is recorded in `mydebug/new-er/records/20260623-120600-observe-only-config-cache.md`. The accepted observe-only evidence above is from the clean rebuild, and the generated profile after that rebuild records `IntERObserveOnlyMinimalConfig`.

## Debug History Used

Before handling the config-cache issue, the debug protocol was followed by reading:

- `mydebug/new-er/README.md`
- `mydebug/new-er/records/20260622-131325-baseline-microbench-direct-diff.md`
- `mydebug/new-er/records/20260622-144213-baseline-commit-writeback-owner.md`
- `mydebug/new-er/records/20260622-153749-baseline-direct-diff-rf-shadow.md`

The prior baseline direct-Difftest failures are validated by the successful baseline and observe-only smoke runs recorded here.
