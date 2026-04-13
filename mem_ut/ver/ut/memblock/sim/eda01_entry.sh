#!/bin/bash

set -euo pipefail

SIM_DIR="/nfs/home/lixiangrui/work/memblock_ut/mem_ut/ver/ut/memblock/sim"
PROJECT_ROOT="$(cd "$SIM_DIR/../../../../../" && pwd)"

TARGET="${1:-}"
if [[ -z "$TARGET" ]]; then
    echo "usage: $0 <make-target> [make assignments...]" >&2
    exit 1
fi
shift

cd "$SIM_DIR"
export MEMBLOCK_PROJECT="${MEMBLOCK_PROJECT:-$PROJECT_ROOT}"
exec make "$TARGET" "$@"
