#!/usr/bin/env python3
"""Generate picker internal-signal yaml from a SystemVerilog module header."""

from __future__ import annotations

import argparse
import re
from pathlib import Path


MODULE_RE = re.compile(r"module\s+(?P<name>\w+)\s*\(", re.MULTILINE)
PORT_RE = re.compile(
    r"^\s*(input|output|inout)\s+"
    r"(?:(?:wire|reg|logic)\s+)?"
    r"(?P<width>\[[^\]]+\]\s+)?"
    r"(?P<name>[A-Za-z_][A-Za-z0-9_$]*)\s*,?\s*$"
)


def extract_ports(text: str, module_name: str) -> list[tuple[str, str | None]]:
    module_match = MODULE_RE.search(text)
    if not module_match:
        raise ValueError(f"cannot find module declaration in input")
    if module_match.group("name") != module_name:
        raise ValueError(
            f"module name mismatch: expected {module_name}, found {module_match.group('name')}"
        )

    start = module_match.end()
    end = text.find(");", start)
    if end < 0:
        raise ValueError("cannot find end of module header")

    ports = []
    seen = set()
    for raw_line in text[start:end].splitlines():
        line = raw_line.split("//", 1)[0].strip()
        if not line:
            continue
        match = PORT_RE.match(line)
        if not match:
            raise ValueError(f"unsupported port declaration: {raw_line.rstrip()}")
        name = match.group("name")
        if name in seen:
            continue
        seen.add(name)
        width = match.group("width")
        ports.append((name, width.strip() if width else None))
    return ports


def emit_yaml(scope_name: str, ports: list[tuple[str, str | None]]) -> str:
    scope_parts = scope_name.split(".")
    lines = [f"{scope_parts[0]}:"]

    if len(scope_parts) == 1:
        signal_indent = "  "
    else:
        for depth, part in enumerate(scope_parts[1:], start=1):
            lines.append(f'{"  " * depth}- {part}:')
        signal_indent = "  " * len(scope_parts)

    for name, width in ports:
        signal = f"wire {name}" if width is None else f"wire {width} {name}"
        lines.append(f'{signal_indent}- "{signal}"')
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--module", required=True, help="Target module name")
    parser.add_argument(
        "--scope",
        help="Picker internal-signal scope name; defaults to module name",
    )
    parser.add_argument("--input", required=True, help="SystemVerilog input file")
    parser.add_argument("--output", required=True, help="YAML output path")
    args = parser.parse_args()

    input_path = Path(args.input)
    output_path = Path(args.output)

    ports = extract_ports(input_path.read_text(), args.module)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    scope_name = args.scope if args.scope else args.module
    output_path.write_text(emit_yaml(scope_name, ports))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
