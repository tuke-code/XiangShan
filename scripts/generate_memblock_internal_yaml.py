#!/usr/bin/env python3
"""Generate MemBlock picker internal yaml with direct LSQ queue state scopes."""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from pathlib import Path

from generate_picker_internal_yaml import extract_ports


VLQ_SIZE = 72
LQR_SIZE = 72
SQ_SIZE = 56


@dataclass
class ScopeNode:
    signals: list[str] = field(default_factory=list)
    children: dict[str, "ScopeNode"] = field(default_factory=dict)


def add_scope(root: dict[str, ScopeNode], scope: str, signals: list[str]) -> None:
    parts = scope.split(".")
    if not parts:
        raise ValueError("empty scope")
    node = root.setdefault(parts[0], ScopeNode())
    for part in parts[1:]:
        node = node.children.setdefault(part, ScopeNode())
    node.signals.extend(signals)


def emit_scope(name: str, node: ScopeNode, depth: int = 0) -> list[str]:
    lines: list[str] = []
    indent = "  " * depth
    if depth == 0:
        lines.append(f"{name}:")
    else:
        lines.append(f"{indent}- {name}:")
    child_indent = "  " * (depth + 1)
    for signal in node.signals:
        lines.append(f'{child_indent}- "{signal}"')
    for child_name, child_node in node.children.items():
        lines.extend(emit_scope(child_name, child_node, depth + 1))
    return lines


def bit_series(name: str, count: int) -> list[str]:
    return [f"reg {name}_{idx}" for idx in range(count)]


def value_series(name: str, width: str, count: int) -> list[str]:
    return [f"reg {width} {name}_{idx}" for idx in range(count)]


def ptr_series(name: str, width: str, count: int) -> list[str]:
    signals = []
    for idx in range(count):
        signals.append(f"reg {name}_{idx}_flag")
        signals.append(f"reg {width} {name}_{idx}_value")
    return signals


def build_lqr_signals() -> list[str]:
    signals = [
        *bit_series("allocated", LQR_SIZE),
        *bit_series("scheduled", LQR_SIZE),
        *bit_series("blocking", LQR_SIZE),
        *value_series("cause", "[12:0]", LQR_SIZE),
        *value_series("debug_vaddr", "[49:0]", LQR_SIZE),
    ]
    for idx in range(LQR_SIZE):
        signals.extend(
            [
                f"reg vecReplay_{idx}_isvec",
                f"reg [7:0] vecReplay_{idx}_elemIdx",
                f"reg [2:0] vecReplay_{idx}_alignedType",
                f"reg [3:0] vecReplay_{idx}_mbIndex",
                f"reg [7:0] vecReplay_{idx}_elemIdxInsideVd",
                f"reg [3:0] vecReplay_{idx}_reg_offset",
                f"reg [15:0] vecReplay_{idx}_mask",
                f"reg [7:0] uop_{idx}_pdest",
                f"reg uop_{idx}_robIdx_flag",
                f"reg [8:0] uop_{idx}_robIdx_value",
                f"reg uop_{idx}_lqIdx_flag",
                f"reg [6:0] uop_{idx}_lqIdx_value",
                f"reg uop_{idx}_sqIdx_flag",
                f"reg [5:0] uop_{idx}_sqIdx_value",
            ]
        )
    return signals


def generate_yaml(wrapper_path: Path) -> str:
    ports = extract_ports(wrapper_path.read_text(), "LsqWrapper")
    root: dict[str, ScopeNode] = {}
    add_scope(
        root,
        "MemBlock.inner_lsq",
        [f"wire {name}" if width is None else f"wire {width} {name}" for name, width in ports],
    )
    add_scope(
        root,
        "MemBlock.inner_lsq.loadQueue.virtualLoadQueue",
        [
            "reg deqPtr_r_flag",
            "reg [6:0] deqPtr_r_value",
            *bit_series("allocated", VLQ_SIZE),
            *ptr_series("robIdx", "[8:0]", VLQ_SIZE),
            *value_series("uopIdx", "[6:0]", VLQ_SIZE),
            *bit_series("isvec", VLQ_SIZE),
        ],
    )
    add_scope(root, "MemBlock.inner_lsq.loadQueue.loadQueueReplay", build_lqr_signals())

    lines: list[str] = []
    for root_name, root_node in root.items():
        lines.extend(emit_scope(root_name, root_node))
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wrapper", required=True, help="LsqWrapper.sv path")
    parser.add_argument("--output", required=True, help="YAML output path")
    args = parser.parse_args()

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(generate_yaml(Path(args.wrapper)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
