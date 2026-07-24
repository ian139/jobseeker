#!/usr/bin/env sh
set -eu

REPO_ROOT=$PWD
ROOT=$(mktemp -d "./.jobs-assistant-wheel-smoke.XXXXXX")
ROOT=$(cd "$ROOT" && pwd)
WORK_ROOT="$ROOT/work"
DATA_ROOT="$WORK_ROOT/data"
RESUME_ROOT="$WORK_ROOT/resume"
DIST_ROOT="$ROOT/dist"
PACKAGE_ROOT="$ROOT/package"
mkdir -p "$DATA_ROOT" "$RESUME_ROOT" "$DIST_ROOT" "$PACKAGE_ROOT"
chmod 700 "$DATA_ROOT" "$RESUME_ROOT"
cleanup() {
  rm -rf "$ROOT"
}
trap cleanup EXIT INT TERM

if command -v uv >/dev/null 2>&1; then
  :
else
  PYTHON_BIN="${PYTHON_BIN:-.venv/bin/python}"
  CLI_BIN="${CLI_BIN:-.venv/bin/jobs-assistant}"
  "$PYTHON_BIN" -m pytest
  "$CLI_BIN" --help >/dev/null
  exit 0
fi

python3 - "$RESUME_ROOT/Main_Resume.pdf" <<'PY'
from pathlib import Path
import sys

output = Path(sys.argv[1])
objects = [
    b"<< /Type /Catalog /Pages 2 0 R >>",
    b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
    b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] >>",
]
payload = bytearray(b"%PDF-1.4\n")
offsets = [0]
for index, obj in enumerate(objects, 1):
    offsets.append(len(payload))
    payload.extend(f"{index} 0 obj\n".encode("ascii"))
    payload.extend(obj)
    payload.extend(b"\nendobj\n")
xref_offset = len(payload)
payload.extend(f"xref\n0 {len(objects) + 1}\n".encode("ascii"))
payload.extend(b"0000000000 65535 f \n")
for offset in offsets[1:]:
    payload.extend(f"{offset:010d} 00000 n \n".encode("ascii"))
payload.extend(f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref_offset}\n%%EOF\n".encode("ascii"))
output.write_bytes(payload)
PY
chmod 600 "$RESUME_ROOT/Main_Resume.pdf"

cp "$REPO_ROOT/pyproject.toml" "$PACKAGE_ROOT/"
cp -R "$REPO_ROOT/src" "$PACKAGE_ROOT/src"
(
  cd "$PACKAGE_ROOT"
  uv build --wheel --out-dir "$DIST_ROOT" >/dev/null
)
set -- "$DIST_ROOT"/*.whl
WHEEL=$1

uv run --isolated --no-project --with "$WHEEL" python - <<'PY'
from importlib import resources

package = resources.files("jobs_assistant")
assert (package / "puppeteer_runner.js").is_file()
policy = (package / "safety_policy.json").read_text(encoding="utf-8")
assert '"version"' in policy
PY

uv run --isolated --no-project --with "$WHEEL" jobs-assistant --help >/dev/null
uv run --isolated --no-project --with "$WHEEL" jobs-assistant autofill --help >/dev/null

(
  cd "$WORK_ROOT"
  DATABASE_URL=data/jobs.sqlite3 \
    uv run --isolated --no-project --with "$WHEEL" jobs-assistant --db data/jobs.sqlite3 init-db >/dev/null
  DATABASE_URL=data/jobs.sqlite3 \
  JOBS_ASSISTANT_PUPPETEER_ROOT="$REPO_ROOT/node_modules" \
    uv run --isolated --no-project --with "$WHEEL" jobs-assistant \
      --db data/jobs.sqlite3 autofill \
      --limit 1 \
      --ats greenhouse >"$ROOT/result.json"
)

python3 - "$ROOT/result.json" "$DATA_ROOT" <<'PY'
import json
import stat
import sys
from pathlib import Path

result = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
assert result == {"results": []}
data_root = Path(sys.argv[2])
assert stat.S_IMODE(data_root.stat().st_mode) == 0o700
assert stat.S_IMODE((data_root / "jobs.sqlite3").stat().st_mode) & 0o077 == 0
assert stat.S_IMODE((data_root / "application-runs").stat().st_mode) == 0o700
assert not (data_root / "protected-runtime").exists()
PY

echo '{"ok":true,"smoke":"wheel"}'
