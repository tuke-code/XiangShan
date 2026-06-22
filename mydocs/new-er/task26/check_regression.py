#!/usr/bin/env python3
from pathlib import Path
import sys


DOC = Path(__file__).with_name("affected-submodule-regression.md")


REQUIRED = [
    "# Task26 Affected Submodule Regression",
    "Round 41 covers the task26 gate",
    "xiangshan.backend.IntSparseUCATest",
    "xiangshan.backend.IntEarlyReleaseBundlesTest",
    "xiangshan.backend.IntEarlyReleaseFreeListTest",
    "xiangshan.backend.IntEarlyReleaseDataPathTest",
    "xiangshan.backend.IntEarlyReleaseRobTest",
    "difftest.PreprocessTest",
    "Run completed in 41 minutes, 10 seconds.",
    "5 suites completed, 0 aborted.",
    "71 tests run.",
    "71 succeeded.",
    "0 failed, 0 canceled, 0 ignored, 0 pending.",
    "Run completed in 13 seconds, 125 milliseconds.",
    "1 suite completed, 0 aborted.",
    "5 tests run.",
    "5 succeeded.",
    "mill -i xiangshan.test.compile",
    "mill -i difftest.test.compile",
    "Exit code 0.",
    "No task26 regression failed",
    "no new record under `mydebug/new-er/` was required",
    "does not replace the later observe-only or functional emulator gates",
    "The Difftest submodule was not changed by this round.",
]


FORBIDDEN = [
    "task26 complete without " "difftest",
    "task26 complete without " "compile",
    "emulator gate " "complete",
]


def main() -> int:
    text = DOC.read_text(encoding="utf-8")
    missing = [needle for needle in REQUIRED if needle not in text]
    forbidden = [needle for needle in FORBIDDEN if needle in text]

    if missing:
        for needle in missing:
            print(f"missing required marker: {needle}", file=sys.stderr)
        return 1

    if forbidden:
        for needle in forbidden:
            print(f"forbidden marker present: {needle}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
