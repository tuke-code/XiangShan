# Task26 Affected Submodule Regression

## Scope

Round 41 covers the task26 gate: run all new and affected module-level tests for the int early-release implementation before any observe-only or functional emulator testing.

The selected regression set follows the original task26 dependency list:

- task4 coverage: `xiangshan.backend.IntEarlyReleaseBundlesTest`
- task6 coverage: `xiangshan.backend.IntSparseUCATest`
- task11 coverage: `xiangshan.backend.IntEarlyReleaseFreeListTest`
- task14 coverage: `xiangshan.backend.IntEarlyReleaseBundlesTest`
- task16 coverage: `xiangshan.backend.IntEarlyReleaseDataPathTest`
- task20 coverage: `xiangshan.backend.IntEarlyReleaseRobTest`
- task23 coverage: `difftest.PreprocessTest`

No emulator command was run in this task26 record. That work remains reserved for later observe-only and functional emulator gates.

## Environment Workarounds

The local review shell still needs the existing environment workarounds recorded in the goal tracker:

```bash
printf '127.0.0.1 node007.bosccluster.com node007\n' > /tmp/codex-hosts
mkdir -p /tmp/ccache-codex-review
```

All Mill commands below used:

```bash
CCACHE_DIR=/tmp/ccache-codex-review env -u LD_PRELOAD JAVA_TOOL_OPTIONS=-Djdk.net.hosts.file=/tmp/codex-hosts
```

## XiangShan Submodule Regression

Command:

```bash
CCACHE_DIR=/tmp/ccache-codex-review env -u LD_PRELOAD JAVA_TOOL_OPTIONS=-Djdk.net.hosts.file=/tmp/codex-hosts \
  mill -i xiangshan.test.testOnly \
  xiangshan.backend.IntSparseUCATest \
  xiangshan.backend.IntEarlyReleaseBundlesTest \
  xiangshan.backend.IntEarlyReleaseFreeListTest \
  xiangshan.backend.IntEarlyReleaseDataPathTest \
  xiangshan.backend.IntEarlyReleaseRobTest
```

Result:

- Run completed in 41 minutes, 10 seconds.
- 5 suites completed, 0 aborted.
- 71 tests run.
- 71 succeeded.
- 0 failed, 0 canceled, 0 ignored, 0 pending.

Existing elaboration warnings were observed in Decode and Snapshot paths. They did not fail the command and were already present in prior accepted regression evidence.

## Difftest Preprocess Regression

Command:

```bash
CCACHE_DIR=/tmp/ccache-codex-review env -u LD_PRELOAD JAVA_TOOL_OPTIONS=-Djdk.net.hosts.file=/tmp/codex-hosts \
  mill -i difftest.test.testOnly difftest.PreprocessTest
```

Result:

- Run completed in 13 seconds, 125 milliseconds.
- 1 suite completed, 0 aborted.
- 5 tests run.
- 5 succeeded.
- 0 failed, 0 canceled, 0 ignored, 0 pending.

## Compile Gates

Command:

```bash
CCACHE_DIR=/tmp/ccache-codex-review env -u LD_PRELOAD JAVA_TOOL_OPTIONS=-Djdk.net.hosts.file=/tmp/codex-hosts \
  mill -i xiangshan.test.compile
```

Result:

- Exit code 0.

Command:

```bash
CCACHE_DIR=/tmp/ccache-codex-review env -u LD_PRELOAD JAVA_TOOL_OPTIONS=-Djdk.net.hosts.file=/tmp/codex-hosts \
  mill -i difftest.test.compile
```

Result:

- Exit code 0.

## Boundary Notes

- No task26 regression failed, so no blocking fix was needed in Round 41.
- No emulator failure occurred, so no new record under `mydebug/new-er/` was required.
- The task26 gate does not replace the later observe-only or functional emulator gates.
- The Difftest submodule was not changed by this round.
