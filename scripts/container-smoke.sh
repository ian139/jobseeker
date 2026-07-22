#!/usr/bin/env sh
set -eu

if ! command -v docker >/dev/null 2>&1; then
  echo '{"error":{"code":"docker_unavailable","message":"docker command is unavailable"}}' >&2
  exit 2
fi
if ! docker info >/dev/null 2>&1; then
  echo '{"error":{"code":"docker_unavailable","message":"docker daemon is unavailable"}}' >&2
  exit 2
fi

ROOT=$(mktemp -d "${TMPDIR:-/tmp}/jobs-assistant-container-smoke.XXXXXX")
DATA_ROOT="$ROOT/data"
RESUME_ROOT="$ROOT/resume"
RESUME_FILE="$RESUME_ROOT/Main_Resume.pdf"
PROFILE_FILE="$RESUME_ROOT/profile.json"
TEMPLATE_FILE="$RESUME_ROOT/Resume.tex"
DB_PATH="$DATA_ROOT/jobs.sqlite3"
OVERRIDE="$ROOT/compose.override.yml"
COMPOSE_PROJECT_NAME="jobs-assistant-smoke-$$"
cleanup() {
  docker compose -p "$COMPOSE_PROJECT_NAME" -f docker-compose.yml -f "$OVERRIDE" down --volumes --remove-orphans >/dev/null 2>&1 || true
  rm -rf "$ROOT"
}
trap cleanup EXIT INT TERM
UID_VALUE="$(id -u)"
GID_VALUE="$(id -g)"
SMOKE_THEIRSTACK_API_KEY='container-smoke-theirstack-value'
SMOKE_JOB_SOURCE_API_KEY='container-smoke-source-value'
SMOKE_OLLAMA_CLOUD_API_KEY='container-smoke-ollama-value'
INTERPOLATED_CONFIG="$ROOT/interpolated-compose.json"
THEIRSTACK_API_KEY="$SMOKE_THEIRSTACK_API_KEY" \
JOB_SOURCE_API_KEY="$SMOKE_JOB_SOURCE_API_KEY" \
OLLAMA_CLOUD_API_KEY="$SMOKE_OLLAMA_CLOUD_API_KEY" \
  HOST_UID="$UID_VALUE" HOST_GID="$GID_VALUE" \
  docker compose -p "$COMPOSE_PROJECT_NAME" -f docker-compose.yml config --format json >"$INTERPOLATED_CONFIG"
THEIRSTACK_API_KEY="$SMOKE_THEIRSTACK_API_KEY" \
JOB_SOURCE_API_KEY="$SMOKE_JOB_SOURCE_API_KEY" \
OLLAMA_CLOUD_API_KEY="$SMOKE_OLLAMA_CLOUD_API_KEY" \
  python3 - "$INTERPOLATED_CONFIG" <<'PY'
import json
import os
import sys
from pathlib import Path

config = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
environment = config["services"]["jobs-assistant"]["environment"]
for key in ("THEIRSTACK_API_KEY", "JOB_SOURCE_API_KEY", "OLLAMA_CLOUD_API_KEY"):
    assert environment[key] == os.environ[key]
PY
rm -f "$INTERPOLATED_CONFIG"
DEFAULT_CONFIG="$ROOT/default-compose.json"
HOST_UID="$UID_VALUE" HOST_GID="$GID_VALUE" docker compose -p "$COMPOSE_PROJECT_NAME" -f docker-compose.yml config --format json >"$DEFAULT_CONFIG"
python3 - "$DEFAULT_CONFIG" "$UID_VALUE" "$GID_VALUE" <<'PY'
import json
import sys
from pathlib import Path

config_path = Path(sys.argv[1])
uid, gid = sys.argv[2:4]
config = json.loads(config_path.read_text(encoding="utf-8"))
service = config["services"]["jobs-assistant"]
assert service["user"] == f"{uid}:{gid}"
assert service["tmpfs"] == [f"/home/app:uid={uid},gid={gid},mode=0700"]
assert service["environment"]["JOBS_ASSISTANT_CONTAINER_NO_SANDBOX"] == "1"
assert service["environment"]["HOME"] == "/home/app"
assert service["environment"]["XDG_CONFIG_HOME"] == "/home/app/.config"
assert service["environment"]["XDG_CACHE_HOME"] == "/home/app/.cache"
assert "env_file" not in service
for key in (
    "DATABASE_URL",
    "JOB_SOURCE_BASE_URL",
    "JOB_SOURCE_API_KEY",
    "THEIRSTACK_API_KEY",
    "THEIRSTACK_ENABLE_PAID_FETCH",
    "THEIRSTACK_BASE_URL",
    "OLLAMA_CLOUD_API_KEY",
    "OLLAMA_CLOUD_BASE_URL",
    "OLLAMA_CLOUD_MODEL",
    "OLLAMA_CLOUD_THINK",
):
    assert key in service["environment"]
volumes = service["volumes"]
assert len(volumes) == 2
data, resume = volumes
assert data == {
    "type": "bind",
    "source": str(Path.cwd() / "data"),
    "target": "/app/data",
    "bind": {"create_host_path": False},
}
assert resume == {
    "type": "bind",
    "source": str(Path.cwd() / "resume"),
    "target": "/app/resume",
    "read_only": True,
    "bind": {"create_host_path": False},
}
PY

mkdir -p "$DATA_ROOT" "$RESUME_ROOT"
chmod 700 "$DATA_ROOT" "$RESUME_ROOT"
cp "$PWD/resume/profile.json" "$PROFILE_FILE"
cp "$PWD/resume/Resume.tex" "$TEMPLATE_FILE"
chmod 600 "$PROFILE_FILE" "$TEMPLATE_FILE"
python3 - "$RESUME_FILE" <<'PY'
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
chmod 600 "$RESUME_FILE"

cat >"$OVERRIDE" <<EOF
services:
  jobs-assistant:
    user: "$UID_VALUE:$GID_VALUE"
    environment:
      DATABASE_URL: /app/data/jobs.sqlite3
      JOBS_ASSISTANT_PUPPETEER_ROOT: /app/node_modules
      HOST_UID: "$UID_VALUE"
      HOST_GID: "$GID_VALUE"
    volumes:
      - type: bind
        source: "$DATA_ROOT"
        target: /app/data
        read_only: false
        bind:
          create_host_path: false
      - type: bind
        source: "$RESUME_ROOT"
        target: /app/resume
        read_only: true
        bind:
          create_host_path: false
    command: ["autofill", "--limit", "1", "--ats", "greenhouse", "--artifact-root", "/app/data/application-runs"]
EOF

HOST_UID="$UID_VALUE" HOST_GID="$GID_VALUE" docker compose -p "$COMPOSE_PROJECT_NAME" -f docker-compose.yml -f "$OVERRIDE" up --build --abort-on-container-exit --exit-code-from jobs-assistant

HOST_UID="$UID_VALUE" HOST_GID="$GID_VALUE" docker compose -p "$COMPOSE_PROJECT_NAME" -f docker-compose.yml -f "$OVERRIDE" run --rm --no-deps --entrypoint python jobs-assistant -c 'import os; from importlib import resources; from jobs_assistant.browser_adapter import PuppeteerSession; p=resources.files("jobs_assistant"); assert (p / "puppeteer_runner.js").is_file(); assert (p / "safety_policy.json").is_file(); assert os.getuid() == int(os.environ["HOST_UID"]); assert os.environ["PUPPETEER_EXECUTABLE_PATH"] == "/usr/bin/chromium-headless-shell"; assert PuppeteerSession.preflight(headed=False, timeout=15)["puppeteer"] == "24.43.1"'

python3 - "$DATA_ROOT" "$RESUME_ROOT" <<'PY'
import sqlite3
import stat
import sys
from pathlib import Path

data_root = Path(sys.argv[1])
resume_root = Path(sys.argv[2])
assert stat.S_IMODE(data_root.stat().st_mode) == 0o700
assert stat.S_IMODE(resume_root.stat().st_mode) == 0o700
resume_file = resume_root / "Main_Resume.pdf"
assert stat.S_IMODE(resume_file.stat().st_mode) == 0o600
db_path = data_root / "jobs.sqlite3"
assert db_path.is_file()
assert stat.S_IMODE(db_path.stat().st_mode) & 0o077 == 0
with sqlite3.connect(db_path) as connection:
    assert connection.execute("SELECT count(*) FROM jobs").fetchone() == (0,)
    assert connection.execute("SELECT count(*) FROM application_runs").fetchone() == (0,)
assert (data_root / "application-runs").is_dir()
assert stat.S_IMODE((data_root / "application-runs").stat().st_mode) == 0o700
assert not (data_root / "protected-runtime").exists()
PY

python3 - "$DB_PATH" "$ROOT/resume-input.json" <<'PY'
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

db_path = Path(sys.argv[1])
snapshot_path = Path(sys.argv[2])
columns = (
    "id",
    "source",
    "source_job_id",
    "canonical_url",
    "title",
    "company",
    "location",
    "remote",
    "posted_at",
    "discovered_at",
    "description",
    "status",
    "raw_json",
    "first_seen_at",
    "last_seen_at",
)
now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
with sqlite3.connect(db_path) as connection:
    assert connection.execute("SELECT count(*) FROM jobs").fetchone() == (0,)
    cursor = connection.execute(
        """
        INSERT INTO jobs (
            source, source_job_id, canonical_url, title, company, location,
            remote, posted_at, discovered_at, description, status, raw_json,
            first_seen_at, last_seen_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "container-smoke",
            "container-smoke-job-1",
            "https://example.test/jobs/container-smoke-job-1",
            "Software Engineering Spring Co-op",
            "Smoke Systems",
            "Remote",
            1,
            now,
            now,
            "Spring co-op role building reliable Python services with Docker, Kubernetes, SQL, and JavaScript.",
            "queued",
            "{}",
            now,
            now,
        ),
    )
    job_id = int(cursor.lastrowid)
    row = connection.execute(
        f"SELECT {', '.join(columns)} FROM jobs WHERE id = ?",
        (job_id,),
    ).fetchone()
    assert row is not None
    snapshot = dict(zip(columns, row))
snapshot_path.write_text(json.dumps(snapshot, sort_keys=True), encoding="utf-8")
PY

HOST_UID="$UID_VALUE" HOST_GID="$GID_VALUE" docker compose -p "$COMPOSE_PROJECT_NAME" -f docker-compose.yml -f "$OVERRIDE" run --rm --no-deps --entrypoint resume-generate jobs-assistant \
  --db /app/data/jobs.sqlite3 \
  --profile /app/resume/profile.json \
  --template /app/resume/Resume.tex \
  --output-root /app/data/generated-resumes \
  --limit 1 \
  --compiler pdflatex >"$ROOT/resume-result.json"

python3 - "$DATA_ROOT" "$RESUME_ROOT" "$ROOT/resume-input.json" "$ROOT/resume-result.json" <<'PY'
import json
import re
import sqlite3
import stat
import sys
from pathlib import Path

data_root = Path(sys.argv[1])
resume_root = Path(sys.argv[2])
snapshot = json.loads(Path(sys.argv[3]).read_text(encoding="utf-8"))
result = json.loads(Path(sys.argv[4]).read_text(encoding="utf-8"))
assert set(result) == {"results"}
assert len(result["results"]) == 1
result_row = result["results"][0]
assert result_row["job_id"] == snapshot["id"]
assert result_row["pages"] == 1
assert result_row["graduation_date"] == "May 2027"
assert result_row["artifact_ref"].startswith(f"job-{snapshot['id']}/")

assert stat.S_IMODE((resume_root / "profile.json").stat().st_mode) == 0o600
assert stat.S_IMODE((resume_root / "Resume.tex").stat().st_mode) == 0o600
artifact_root = data_root / "generated-resumes"
assert stat.S_IMODE(artifact_root.stat().st_mode) == 0o700
job_dirs = [path for path in artifact_root.iterdir() if path.is_dir() and not path.is_symlink()]
assert len(job_dirs) == 1
job_dir = job_dirs[0]
assert job_dir.name == f"job-{snapshot['id']}"
assert stat.S_IMODE(job_dir.stat().st_mode) == 0o700
fingerprint_dirs = [path for path in job_dir.iterdir() if path.is_dir() and not path.is_symlink()]
assert len(fingerprint_dirs) == 1
artifact_dir = fingerprint_dirs[0]
assert re.fullmatch(r"[0-9a-f]{16}", artifact_dir.name)
assert stat.S_IMODE(artifact_dir.stat().st_mode) == 0o700
expected_artifacts = {
    "resume.tex",
    "resume.pdf",
    "optimization.json",
    "job_description.txt",
    "manifest.json",
}
children = list(artifact_dir.iterdir())
assert {path.name for path in children} == expected_artifacts
assert len(children) == len(expected_artifacts) == 5
for path in children:
    info = path.lstat()
    assert stat.S_ISREG(info.st_mode)
    assert stat.S_IMODE(info.st_mode) == 0o600

pdf_path = artifact_dir / "resume.pdf"
pdf = pdf_path.read_bytes()
assert pdf.startswith(b"%PDF-")


columns = (
    "id",
    "source",
    "source_job_id",
    "canonical_url",
    "title",
    "company",
    "location",
    "remote",
    "posted_at",
    "discovered_at",
    "description",
    "status",
    "raw_json",
    "first_seen_at",
    "last_seen_at",
)
with sqlite3.connect(data_root / "jobs.sqlite3") as connection:
    connection.row_factory = sqlite3.Row
    rows = connection.execute(
        f"SELECT {', '.join(columns)} FROM jobs"
    ).fetchall()
    assert len(rows) == 1
    actual = dict(rows[0])
    assert actual == snapshot
    assert actual["status"] == "queued"
    assert connection.execute("SELECT count(*) FROM application_runs").fetchone()[0] == 0
assert stat.S_IMODE((data_root / "jobs.sqlite3").stat().st_mode) & 0o077 == 0
assert stat.S_IMODE((data_root / "application-runs").stat().st_mode) == 0o700
assert not (data_root / "protected-runtime").exists()
PY

HOST_UID="$UID_VALUE" HOST_GID="$GID_VALUE" docker compose -p "$COMPOSE_PROJECT_NAME" -f docker-compose.yml -f "$OVERRIDE" run --rm --no-deps -T --entrypoint python jobs-assistant - <<'PY'
from pathlib import Path

from pypdf import PdfReader

pdf_paths = list(Path("/app/data/generated-resumes").glob("job-*/*/resume.pdf"))
assert len(pdf_paths) == 1
reader = PdfReader(str(pdf_paths[0]))
assert len(reader.pages) == 1
text = "\n".join(page.extract_text() or "" for page in reader.pages)
for expected in ("Ian Rapko", "May 2027", "Python"):
    assert expected in text
PY

echo '{"ok":true,"smoke":"container"}'
