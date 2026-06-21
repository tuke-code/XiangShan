#!/usr/bin/env python3
from pathlib import Path
import re
import sys


ROOT = Path(__file__).resolve().parent
DOC = ROOT / "assertion-perf-inventory.md"
SPEC = ROOT.parent / "spec" / "int-er-sparse-uca-spec.md"

REQUIRED_HEADINGS = [
    "# Int ER Task24 Assertion And Perf Inventory",
    "## Coverage Matrix",
    "## Remaining Task24 Work",
    "## Focused Suite Sweep Evidence",
    "## Closure Gate",
]

REQUIRED_MODULES = [
    "UCA",
    "Rename",
    "MEFreeList",
    "ROB/ST",
    "DataPath",
    "Difftest",
]

REQUIRED_RISKS = [
    "underflow",
    "duplicate free",
    "generation mismatch",
    "wrong suppress identity",
    "wrong srcIdx",
    "wrong direct diff path",
    "fallback reasons",
    "producer ready",
    "redefiner non-speculative",
    "early-free",
    "suppress",
    "late events",
    "redirect kill",
]

REQUIRED_STATUS = [
    "covered",
    "open",
]

UNSUPPORTED_SCOPE_MARKERS = [
    "producer-not-ready is not a first-version fallback event",
    "int_er_fallback_producer_not_ready is deliberately unsupported",
    "ST does not combinationally query UCA produced-ready state",
    "pending interrupt/trap/flush is handled as an ST stop/no-guardDec condition",
    "int_er_fallback_pending_interrupt is deliberately unsupported",
]

FOCUSED_SWEEP_MARKERS = [
    "Round 39 focused sweep",
    "IntSparseUCATest",
    "IntEarlyReleaseBundlesTest",
    "IntEarlyReleaseFreeListTest",
    "IntEarlyReleaseDataPathTest",
    "IntEarlyReleaseRobTest",
    "PreprocessTest",
    "xiangshan.test.compile",
    "difftest.test.compile not required",
    "70 tests run, 70 succeeded",
    "5 tests run, 5 succeeded",
]


def fail(message: str) -> int:
    print(message, file=sys.stderr)
    return 1


def main() -> int:
    if not DOC.exists():
        return fail(f"missing inventory document: {DOC}")
    if not SPEC.exists():
        return fail(f"missing sparse UCA spec document: {SPEC}")

    text = DOC.read_text(encoding="utf-8")
    spec_text = SPEC.read_text(encoding="utf-8")
    lower = text.lower()
    combined = f"{text}\n{spec_text}"

    for heading in REQUIRED_HEADINGS:
        if heading not in text:
            return fail(f"missing required heading: {heading}")

    for module in REQUIRED_MODULES:
        if module.lower() not in lower:
            return fail(f"missing module coverage: {module}")

    for risk in REQUIRED_RISKS:
        if risk.lower() not in lower:
            return fail(f"missing risk coverage: {risk}")

    for status in REQUIRED_STATUS:
        if re.search(rf"\b{re.escape(status)}\b", lower) is None:
            return fail(f"missing inventory status keyword: {status}")

    if "task24 remains active" not in lower:
        return fail("inventory must state whether task24 remains active")

    for marker in UNSUPPORTED_SCOPE_MARKERS:
        if marker not in combined:
            return fail(f"missing unsupported fallback scope marker: {marker}")

    fallback_rows = [
        line for line in text.splitlines()
        if line.startswith("| fallback reasons |")
    ]
    if len(fallback_rows) != 1:
        return fail("inventory must contain exactly one fallback reasons row")
    fallback_row = fallback_rows[0]
    if "| covered |" not in fallback_row:
        return fail("fallback reasons row must be fully covered")
    if "open perf" in fallback_row.lower():
        return fail("fallback reasons row must not list an open perf gap")

    remaining_section = text.split("## Remaining Task24 Work", 1)[-1]
    stale_remaining = [
        "producer-not-ready",
        "pending-interrupt",
        "stable fallback reason names",
    ]
    for stale_text in stale_remaining:
        if stale_text in remaining_section:
            return fail(f"remaining task24 work still carries resolved fallback scope: {stale_text}")

    for marker in FOCUSED_SWEEP_MARKERS:
        if marker not in text:
            return fail(f"missing focused sweep evidence marker: {marker}")

    completed_sweep_phrase = "focused suite sweep" + " before task26"
    if completed_sweep_phrase in remaining_section:
        return fail("remaining task24 work still lists the completed focused sweep")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
