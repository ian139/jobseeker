#!/usr/bin/env bash
set -euo pipefail

export LANG=C
export LC_ALL=C
export TZ=UTC

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT_DIR"

node --test \
  tests/ats-adapter.test.mjs \
  tests/backlog-runner.test.mjs \
  tests/contract-profile.test.mjs \
  tests/observer.test.mjs \
  tests/selector.test.mjs \
  tests/session.test.mjs \
  >/dev/null

export PYTHONHASHSEED=0
export RESUME_ADVISORY_ENABLED=0
unset OLLAMA_API_KEY OLLAMA_CLOUD_API_KEY

node benchmarks/platform-pipeline-benchmark.mjs
