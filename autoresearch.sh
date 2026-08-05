#!/usr/bin/env bash
set -euo pipefail

ROOT="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
export LANG=C
export LC_ALL=C
export TZ=UTC

exec node "$ROOT/benchmarks/custom-select-retention.mjs"
