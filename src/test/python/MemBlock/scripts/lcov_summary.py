#!/usr/bin/env python3
"""
Summarize Verilator LCOV output for MemBlock.

`verilator_coverage -write-info` currently emits LCOV files with abundant `DA:`
records but may omit per-file `LF:`/`LH:` totals. This utility treats `DA`
records as the line-coverage source of truth and falls back to `BRDA` when
`BRH:`/`BRF:` summaries are unavailable.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
from pathlib import Path


def _pct(hit: int, total: int) -> float:
    return round((hit / total * 100.0), 4) if total else 0.0


def parse_lcov(path: Path) -> dict:
    line_hit = 0
    line_total = 0
    brda_hit = 0
    brda_total = 0

    for raw in path.read_text(errors="ignore").splitlines():
        if raw.startswith("DA:"):
            line_total += 1
            count = int(raw.split(",", 1)[1])
            if count > 0:
                line_hit += 1
        elif raw.startswith("BRDA:"):
            brda_total += 1
            count = raw.rsplit(",", 1)[1]
            if count != "-" and int(count) > 0:
                brda_hit += 1

    return {
        "source": str(path),
        "line": {
            "hit": line_hit,
            "total": line_total,
            "pct": _pct(line_hit, line_total),
            "derived_from": "DA" if line_total else "none",
        },
        "branch": {
            "hit": brda_hit,
            "total": brda_total,
            "pct": _pct(brda_hit, brda_total),
            "derived_from": "BRDA" if brda_total else "none",
        },
    }


def maybe_run_genhtml(merged_info: Path, output_dir: Path) -> None:
    genhtml = shutil.which("genhtml")
    if not genhtml:
        raise FileNotFoundError("genhtml not found in PATH")
    output_dir.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [genhtml, "--branch-coverage", str(merged_info), "-o", str(output_dir)],
        check=True,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Summarize LCOV merged.info")
    parser.add_argument("merged_info", type=Path, help="Path to LCOV merged.info")
    parser.add_argument(
        "--output-json",
        type=Path,
        help="Optional path to write JSON summary",
    )
    parser.add_argument(
        "--genhtml-output",
        type=Path,
        help="Optional directory to generate HTML coverage with genhtml",
    )
    args = parser.parse_args()

    summary = parse_lcov(args.merged_info)
    rendered = json.dumps(summary, indent=2, sort_keys=True)
    print(rendered)

    if args.output_json:
        args.output_json.parent.mkdir(parents=True, exist_ok=True)
        args.output_json.write_text(rendered + "\n")

    if args.genhtml_output:
        maybe_run_genhtml(args.merged_info, args.genhtml_output)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
