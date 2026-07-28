#!/usr/bin/env bash
set -euo pipefail

export LANG=C
export LC_ALL=C
export TZ=UTC

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT_DIR"

node --test \
  tests/contract-profile.test.mjs \
  tests/selector.test.mjs \
  tests/session.test.mjs \
  >/dev/null

node benchmarks/pipeline-payload-benchmark.mjs
