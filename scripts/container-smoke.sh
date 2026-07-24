#!/usr/bin/env sh
set -eu
UID_VALUE="$(id -u)"
if [ "$UID_VALUE" -eq 0 ]; then
  echo '{"error":{"code":"root_uid_unsupported","message":"container smoke requires a nonzero invoking UID"}}' >&2
  exit 2
fi

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
OVERRIDE="$ROOT/compose.override.yml"
COMPOSE_PROJECT_NAME="jobs-assistant-smoke-$$"
cleanup() {
  docker compose -p "$COMPOSE_PROJECT_NAME" -f docker-compose.yml -f "$OVERRIDE" down --volumes --remove-orphans >/dev/null 2>&1 || true
  rm -rf "$ROOT"
}
trap cleanup EXIT INT TERM
GID_VALUE="$(id -g)"
SMOKE_THEIRSTACK_API_KEY='container-smoke-theirstack-value'
SMOKE_JOB_SOURCE_API_KEY='container-smoke-source-value'
SMOKE_OLLAMA_CLOUD_API_KEY='container-smoke-ollama-value'
SMOKE_OMP_AUTH_BROKER_URL='https://broker.example.test'
SMOKE_OMP_AUTH_BROKER_TOKEN='container-smoke-broker-value'
SMOKE_OPENAI_API_KEY='container-smoke-openai-value'
INTERPOLATED_CONFIG="$ROOT/interpolated-compose.json"
THEIRSTACK_API_KEY="$SMOKE_THEIRSTACK_API_KEY" \
JOB_SOURCE_API_KEY="$SMOKE_JOB_SOURCE_API_KEY" \
OLLAMA_CLOUD_API_KEY="$SMOKE_OLLAMA_CLOUD_API_KEY" \
OMP_AUTH_BROKER_URL="$SMOKE_OMP_AUTH_BROKER_URL" \
OMP_AUTH_BROKER_TOKEN="$SMOKE_OMP_AUTH_BROKER_TOKEN" \
OPENAI_API_KEY="$SMOKE_OPENAI_API_KEY" \
  HOST_UID="$UID_VALUE" HOST_GID="$GID_VALUE" \
  docker compose -p "$COMPOSE_PROJECT_NAME" -f docker-compose.yml config --format json >"$INTERPOLATED_CONFIG"
THEIRSTACK_API_KEY="$SMOKE_THEIRSTACK_API_KEY" \
JOB_SOURCE_API_KEY="$SMOKE_JOB_SOURCE_API_KEY" \
OLLAMA_CLOUD_API_KEY="$SMOKE_OLLAMA_CLOUD_API_KEY" \
OMP_AUTH_BROKER_URL="$SMOKE_OMP_AUTH_BROKER_URL" \
OMP_AUTH_BROKER_TOKEN="$SMOKE_OMP_AUTH_BROKER_TOKEN" \
OPENAI_API_KEY="$SMOKE_OPENAI_API_KEY" \
  python3 - "$INTERPOLATED_CONFIG" <<'PY'
import json
import os
import sys
from pathlib import Path

config = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
environment = config["services"]["jobs-assistant"]["environment"]
for key in (
    "THEIRSTACK_API_KEY",
    "JOB_SOURCE_API_KEY",
    "OLLAMA_CLOUD_API_KEY",
    "OMP_AUTH_BROKER_URL",
    "OMP_AUTH_BROKER_TOKEN",
    "OPENAI_API_KEY",
):
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
    "OMP_AUTH_BROKER_URL",
    "OMP_AUTH_BROKER_TOKEN",
    "OPENAI_API_KEY",
    "OLLAMA_CLOUD_MODEL",
    "OLLAMA_CLOUD_THINK",
):
    assert key in service["environment"]
volumes = service["volumes"]
assert len(volumes) == 2
data, resume = volumes
assert data["type"] == "bind"
assert Path(data["source"]).samefile(Path.cwd() / "data")
assert data["target"] == "/app/data"
assert data["bind"] == {"create_host_path": False}
assert resume["type"] == "bind"
assert Path(resume["source"]).samefile(Path.cwd() / "resume")
assert resume["target"] == "/app/resume"
assert resume["read_only"] is True
assert resume["bind"] == {"create_host_path": False}
PY

mkdir -p "$DATA_ROOT" "$RESUME_ROOT"
chmod 700 "$DATA_ROOT" "$RESUME_ROOT"
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

HOST_UID="$UID_VALUE" HOST_GID="$GID_VALUE" docker compose -p "$COMPOSE_PROJECT_NAME" -f docker-compose.yml -f "$OVERRIDE" run --rm -T --no-deps --entrypoint /app/.venv/bin/python jobs-assistant - <<'PY'
import asyncio
import os
from importlib import resources
from pathlib import Path

from jobs_assistant.browser_adapter import PuppeteerSession
from jobs_assistant.cli import _application_rpc_omp_launch_config, build_parser
from jobs_assistant.omp_rpc import OmpRpcProcess

package = resources.files("jobs_assistant")
assert (package / "puppeteer_runner.js").is_file()
assert (package / "safety_policy.json").is_file()
assert os.getuid() == int(os.environ["HOST_UID"])
assert os.environ["PUPPETEER_EXECUTABLE_PATH"] == "/usr/bin/chromium-headless-shell"
omp = Path(os.environ["JOBS_ASSISTANT_OMP_EXECUTABLE"])
assert omp.is_file() and os.access(omp, os.X_OK)
args = build_parser().parse_args(["application-rpc"])
launch_config = _application_rpc_omp_launch_config(args)
assert Path(launch_config.executable) == omp
assert PuppeteerSession.preflight(headed=False, timeout=15)["puppeteer"] == "24.43.1"

async def verify_omp_runtime():
    process = await OmpRpcProcess.launch(launch_config)
    try:
        assert process.verified
        assert not process.poisoned
    finally:
        await process.close()
    assert process.closed
    assert not process.poisoned

asyncio.run(verify_omp_runtime())
PY
HOST_UID="$UID_VALUE" HOST_GID="$GID_VALUE" docker compose -p "$COMPOSE_PROJECT_NAME" -f docker-compose.yml -f "$OVERRIDE" run --rm --no-deps jobs-assistant application-rpc --help

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

echo '{"ok":true,"smoke":"container"}'
