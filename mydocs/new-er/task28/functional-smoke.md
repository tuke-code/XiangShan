# Functional Int ER Smoke

## Summary

- Date: 2026-06-23 CST
- Config: `IntERFunctionalMinimalConfig`
- Result: pass
- Final trap: `HIT GOOD TRAP at pc = 0x80003a4e`
- Functional activity: integer early free is enabled and exercised.
- Scope: first functional smoke for task28 before expanding the larger emu-basics matrix.

## Build

The emulator was rebuilt after a clean because config-only switches can otherwise reuse stale generated RTL.
The commands below wrote raw `.log` files. The committed evidence archives are the matching
`mydocs/new-er/task28/logs/*.log.gz` files.

```bash
CCACHE_DIR=/tmp/ccache-codex-review env -u LD_PRELOAD JAVA_TOOL_OPTIONS=-Djdk.net.hosts.file=/tmp/codex-hosts \
  python3 scripts/xiangshan.py --clean --timeout 3600 \
  > mydocs/new-er/task28/logs/clean-before-functional-rebuild-after-uca-fix.log 2>&1

CCACHE_DIR=/tmp/ccache-codex-review env -u LD_PRELOAD JAVA_TOOL_OPTIONS=-Djdk.net.hosts.file=/tmp/codex-hosts \
  python3 scripts/xiangshan.py --build --emulator verilator --config IntERFunctionalMinimalConfig \
  --threads 1 --make-threads 8 --trace-fst --timeout 3600 \
  > mydocs/new-er/task28/logs/functional-build-after-uca-fix.log 2>&1
```

Build result:

- clean command: exit 0
- build command: exit 0
- build evidence: `mydocs/new-er/task28/logs/functional-build-after-uca-fix.log.gz`
- generated profile: `build/generated-src/difftest_profile.json` records `IntERFunctionalMinimalConfig`
- emulator mtime: `2026-06-23 15:30:16 +0800`

## Run

```bash
CCACHE_DIR=/tmp/ccache-codex-review env -u LD_PRELOAD JAVA_TOOL_OPTIONS=-Djdk.net.hosts.file=/tmp/codex-hosts \
  python3 scripts/xiangshan.py ready-to-run/microbench.bin --emulator verilator \
  --config IntERFunctionalMinimalConfig --threads 1 --trace-fst \
  --max-instr 2000000 --timeout 1800 \
  > mydocs/new-er/task28/logs/functional-run-microbench-after-uca-fix.log 2>&1
```

Run result:

- command exit code: 0
- run evidence: `mydocs/new-er/task28/logs/functional-run-microbench-after-uca-fix.log.gz`
- workload line: `MicroBench PASS`
- final trap line: `Core 0: HIT GOOD TRAP at pc = 0x80003a4e`
- final counters line: `Core-0 instrCnt = 326374, cycleCnt = 335202, IPC = 0.973664`
- seed: 1869

## ER Counters

Key counters from `functional-run-microbench-after-uca-fix.log.gz`:

| Counter | Value |
|---------|-------|
| `int_er_me_freelist_early_free_req` | 2224 |
| `int_er_me_freelist_early_free_merged` | 2224 |
| `int_er_uc_early_free_opportunity` | 2224 |
| `int_er_uc_early_free` | 2224 |
| `int_er_uc_commit_suppress` | 2222 |
| `int_er_uc_read_done_dec` | 6798 |
| `int_er_uc_guard_dec` | 2529 |
| `int_er_uc_saturated_fallback` | 1 |
| `int_er_uc_gen_mismatch` | 464 |
| `int_er_uc_redirect_kill` | 51872 |
| `int_er_rob_raw_read_done` | 205034 |
| `int_er_rob_read_done_accept` | 191077 |
| `int_er_rob_read_done_fallback` | 175905 |
| `int_er_rob_read_done_stale` | 13953 |
| `int_er_rob_read_done_duplicate` | 4 |
| `int_er_datapath_tracked_read_obs` | 205649 |
| `int_er_datapath_read_done` | 17636 |
| `int_er_datapath_fallback` | 187398 |

The free-list counters show that the run did not merely observe opportunities: the integer free list accepted 2224 functional early-free requests, and UCA suppress logic was active at commit.

## Follow-Up

- Continue with broader emu-basics coverage using the same clean-before-config-switch rule.
- If any testcase does not reach final `hit-good-trap`, read `mydebug/new-er/` history before debugging and append a new debug record.
