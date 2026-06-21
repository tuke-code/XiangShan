#!/usr/bin/env python3
from pathlib import Path
import re
import sys


ROOT = Path(__file__).resolve().parent
DOC = ROOT / "assertion-perf-inventory.md"

REQUIRED_HEADINGS = [
    "# Int ER Task24 Assertion And Perf Inventory",
    "## Coverage Matrix",
    "## Remaining Task24 Work",
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


def fail(message: str) -> int:
    print(message, file=sys.stderr)
    return 1


def main() -> int:
    if not DOC.exists():
        return fail(f"missing inventory document: {DOC}")

    text = DOC.read_text(encoding="utf-8")
    lower = text.lower()

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

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
