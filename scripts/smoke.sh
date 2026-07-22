#!/usr/bin/env sh
set -eu

REPO_ROOT=$PWD
ROOT=$(mktemp -d "./.jobs-assistant-wheel-smoke.XXXXXX")
ROOT=$(cd "$ROOT" && pwd)
WORK_ROOT="$ROOT/work"
DATA_ROOT="$WORK_ROOT/data"
RESUME_ROOT="$WORK_ROOT/resume"
DIST_ROOT="$ROOT/dist"
mkdir -p "$DATA_ROOT" "$RESUME_ROOT" "$DIST_ROOT"
chmod 700 "$DATA_ROOT" "$RESUME_ROOT"
cleanup() {
  rm -rf "$ROOT"
}
trap cleanup EXIT INT TERM

cp "$REPO_ROOT/resume/profile.json" "$RESUME_ROOT/profile.json"
cp "$REPO_ROOT/resume/Resume.tex" "$RESUME_ROOT/Resume.tex"
chmod 600 "$RESUME_ROOT/profile.json" "$RESUME_ROOT/Resume.tex"

seed_resume_job() {
  python3 - "$1" <<'PY'
import sqlite3
import sys

db_path = sys.argv[1]
now = "2026-01-01T00:00:00Z"
with sqlite3.connect(db_path) as connection:
    assert connection.execute("SELECT count(*) FROM jobs").fetchone() == (0,)
    connection.execute(
        """
        INSERT INTO jobs (
            source, source_job_id, canonical_url, title, company, location,
            remote, posted_at, discovered_at, description, status, raw_json,
            first_seen_at, last_seen_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "wheel-smoke",
            "wheel-smoke-job-1",
            "https://example.test/jobs/wheel-smoke-job-1",
            "Software Engineering Spring Co-op",
            "Smoke Systems",
            "Remote",
            1,
            now,
            now,
            "Spring co-op requirements: Python, Docker, Kubernetes, SQL, and JavaScript.",
            "queued",
            "{}",
            now,
            now,
        ),
    )
PY
}

verify_resume_result() {
  python3 - "$1" "$2" <<'PY'
import json
import sqlite3
import stat
import sys
from pathlib import Path

result = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
data_root = Path(sys.argv[2])
assert set(result) == {"results"}
assert len(result["results"]) == 1
row = result["results"][0]
assert row["pages"] == 1
assert row["graduation_date"] == "May 2027"
artifact_ref = Path(row["artifact_ref"])
assert not artifact_ref.is_absolute() and ".." not in artifact_ref.parts
artifact_dir = data_root / "generated-resumes" / artifact_ref
expected = {
    "resume.tex",
    "resume.pdf",
    "optimization.json",
    "job_description.txt",
    "manifest.json",
}
children = list(artifact_dir.iterdir())
assert {path.name for path in children} == expected
assert len(children) == 5
assert stat.S_IMODE(artifact_dir.stat().st_mode) == 0o700
for path in children:
    assert path.is_file() and not path.is_symlink()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
assert (artifact_dir / "resume.pdf").read_bytes().startswith(b"%PDF-")
with sqlite3.connect(data_root / "jobs.sqlite3") as connection:
    assert connection.execute("SELECT status FROM jobs").fetchall() == [("queued",)]
    assert connection.execute("SELECT count(*) FROM application_runs").fetchone() == (0,)
PY
}

if command -v uv >/dev/null 2>&1; then
  :
else
  PYTHON_BIN="${PYTHON_BIN:-.venv/bin/python}"
  CLI_BIN="${CLI_BIN:-.venv/bin/jobs-assistant}"
  RESUME_CLI_BIN="${RESUME_CLI_BIN:-.venv/bin/resume-generate}"
  "$PYTHON_BIN" -m pytest
  "$CLI_BIN" --help >/dev/null
  "$RESUME_CLI_BIN" --help >/dev/null
  "$CLI_BIN" --db "$DATA_ROOT/jobs.sqlite3" init-db >/dev/null
  seed_resume_job "$DATA_ROOT/jobs.sqlite3"
  "$RESUME_CLI_BIN" \
    --db "$DATA_ROOT/jobs.sqlite3" \
    --profile "$RESUME_ROOT/profile.json" \
    --template "$RESUME_ROOT/Resume.tex" \
    --output-root "$DATA_ROOT/generated-resumes" \
    --limit 1 >"$ROOT/resume-result.json"
  verify_resume_result "$ROOT/resume-result.json" "$DATA_ROOT"
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

uv build --wheel --out-dir "$DIST_ROOT" >/dev/null
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
uv run --isolated --no-project --with "$WHEEL" resume-generate --help >/dev/null
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

seed_resume_job "$DATA_ROOT/jobs.sqlite3"
(
  cd "$WORK_ROOT"
  uv run --isolated --no-project --with "$WHEEL" resume-generate \
    --db data/jobs.sqlite3 \
    --profile "$RESUME_ROOT/profile.json" \
    --template "$RESUME_ROOT/Resume.tex" \
    --output-root data/generated-resumes \
    --limit 1 >"$ROOT/resume-result.json"
)
verify_resume_result "$ROOT/resume-result.json" "$DATA_ROOT"

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
