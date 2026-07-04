#!/usr/bin/env sh
set -eu
if command -v uv >/dev/null 2>&1; then
  uv run --frozen --extra dev python -m pytest
  uv run --frozen jobs-assistant --help >/dev/null
else
  PYTHON_BIN="${PYTHON_BIN:-.venv/bin/python}"
  CLI_BIN="${CLI_BIN:-.venv/bin/jobs-assistant}"
  "$PYTHON_BIN" -m pytest
  "$CLI_BIN" --help >/dev/null
fi
