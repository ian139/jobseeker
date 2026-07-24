# Operations and troubleshooting

[Back to the project README](../README.md) · [Contributor orchestration](../OMP_CMUX_WORKFLOW.md)

This guide covers local setup, owner-private storage, host versus container browser execution, Compose lifecycle, failure artifacts, and safe verification. Run commands from the repository root.

## Safety boundary

The active workflow is deliberately narrow:

- Jobs are kept in a local SQLite backlog (`data/jobs.sqlite3`).
- Greenhouse and Lever workflows prepare application drafts only. Unsupported hosts, routes, or ATS policies fail closed.
- Every browser mutation must pass a deterministic gate against the current observation. Model output can influence only schema- and safety-validated safe fields; raw model output never drives a browser action.
- Sensitive, legal, protected-class, financial, authentication, CAPTCHA, and assessment fields stop for human handling.
- Final submission is always human-only. The browser adapter does not expose or click a final submit control.
- Observations, plans, actions, screenshots, staged inputs, and job descriptions are private local evidence. Do not paste them into issues, logs, chat, or support requests.

## Prerequisites and local setup

### Python and `uv`

Python 3.11 or newer and `uv` are required. Check the versions before syncing the locked environment:

```bash
python3 --version
uv --version
uv lock --check
uv sync --frozen --extra dev
```

`uv sync --frozen` refuses to change `uv.lock`. Keep the lock check in CI or local verification; do not work around a mismatch with an unlocked install.

### Node, Puppeteer, and Chromium

The local browser adapter uses the pinned Node dependencies in `package.json`. Install the Node dependencies and the browser explicitly:

```bash
node --version
npm --version
npm install
npm run install-browser
```

Use Node.js 22.12 or newer so every pinned OMP/Puppeteer dependency satisfies its runtime requirement. `npm run install-browser` installs the Puppeteer-managed Chrome for Testing in the local user cache. The application browser process is still subject to the route, network, observation, and deterministic action gates.

### Environment file

Copy the example, replace placeholders privately, and never commit the resulting file:

```bash
cp .env.example .env
chmod 600 .env
```

For host commands, either export the values or load them for one command with `uv run --env-file .env ...`. `.env.example` is documentation; it is not a Compose `env_file` and must not be injected wholesale into a container.

The variables are grouped as follows:

| Group | Variables | Use |
| --- | --- | --- |
| Compose bind-mount ownership | `HOST_UID`, `HOST_GID` | Numeric user/group for the Compose service. Set these from `id -u` and `id -g`; `HOST_UID=0` is rejected by the container smoke script. |
| SQLite and feed defaults | `DATABASE_URL`, `JOB_SOURCE_BASE_URL`, `JOB_SOURCE_API_KEY` | Local SQLite path and optional normalized `/v1/jobs` feed credentials. Blank feed values are valid when using only TheirStack. |
| TheirStack | `THEIRSTACK_API_KEY`, `THEIRSTACK_ENABLE_PAID_FETCH`, `THEIRSTACK_BASE_URL` | Preview/sync configuration. Paid fetching remains disabled unless the CLI receives `--paid-fetch` or the boolean is deliberately enabled. |
| Guarded inference | `OLLAMA_CLOUD_API_KEY`, `OLLAMA_CLOUD_BASE_URL`, `OLLAMA_CLOUD_MODEL`, `OLLAMA_CLOUD_THINK` | Optional, bounded inference for unresolved safe fields. Missing credentials leave those fields manual; deterministic profile/resume answers still work. |
| Optional OMP RPC authentication | `OMP_AUTH_BROKER_URL`, `OMP_AUTH_BROKER_TOKEN`, `OPENAI_API_KEY` | Authentication/configuration for an explicitly configured OMP RPC deployment. Keep blank unless that deployment is in use. |

Resume files, profile JSON or presets, applicant descriptions, preferences, artifact roots, and `--headed` are CLI arguments, not ambient environment variables. See [.env.example](../.env.example) for the authoritative placeholders and comments.

## Owner-private SQLite and data

Treat the backlog, resume, profile, preferences, and evidence as owner-private. Before a first Compose run, and after copying data from another machine, enforce restrictive modes:

```bash
mkdir -p data resume
find data resume -type d -exec chmod 700 {} +
find data resume -type f -exec chmod 600 {} +
```

Keep `data/jobs.sqlite3` and any SQLite sidecar files (`-wal`, `-shm`) owner-only. The application creates `data/application-runs/` and each `run-<id>/` evidence directory as mode `0700`; artifact files are mode `0600`. A resume mounted into a container is read-only, and the container smoke fixture uses a mode `0600` resume file.

Do not start Compose while `data` or its files are group/world accessible. Do not use a shared network drive, public web root, checked-in fixture, or temporary directory shared with other users for these paths. If permissions are wrong, stop the process, repair them, and rerun the checks above before retrying.

## Host headed versus container headless execution

### Host: headed review only

A headed application workflow belongs on the host where a display is available and the local Puppeteer browser was installed. On Linux, a usable `DISPLAY` is required; on macOS, use the local GUI session. Run from the repository root and keep the input paths private. The `--headed` flag is for the guarded review window and never enables final submission.

`autofill --headed` and a live `application-rpc` coordinator are host workflows, not container workflows. A human may inspect the prepared draft, complete fields that were intentionally left manual, and submit outside the adapter. That physical review and handoff is not an automated verification gate and is intentionally excluded from the command set below.

### Compose: headless smoke only

The image installs `chromium-headless-shell`, points Puppeteer at it, and sets the container-only no-sandbox flag through the guarded adapter boundary. Compose bind-mounts `./data` read/write and `./resume` read-only. The base service command is `--help`; it is not a headed application runner.

Never pass `--headed` to `docker compose`, never try to open a host review window from the container, and never treat a headless container run as proof of a physical handoff. Use `sh scripts/container-smoke.sh` for the supported end-to-end container check.

## Compose `HOST_UID`/`HOST_GID` lifecycle

Compose runs the service as the invoking owner’s numeric UID/GID so files created in the bind mount remain attributable to that owner. Set the values in the shell for every Compose invocation (shell variables override the example values):

```bash
export HOST_UID="$(id -u)"
export HOST_GID="$(id -g)"
test "$HOST_UID" -ne 0
test "$HOST_GID" -ge 0
```

A safe help-only lifecycle from the repository root is:

```bash
# Ensure owner-private host data before Docker can create files.
mkdir -p data resume
find data resume -type d -exec chmod 700 {} +
find data resume -type f -exec chmod 600 {} +

# Inspect interpolation without starting an application workflow.
docker compose config

# Build the pinned image and run only the service help command.
docker compose build
docker compose run --rm --no-deps jobs-assistant --help

# Remove the one-shot container/network when finished.
docker compose down --remove-orphans
```

Set application credentials in `.env` or in the invoking shell before commands that need them. `docker compose config` can render credential values; do not publish its output. The Compose file passes supported variables individually and does not use `.env.example` as an `env_file`.

If `HOST_UID` is zero, run as a non-root owner; `scripts/container-smoke.sh` intentionally exits with `root_uid_unsupported`. If a bind mount is not writable, repair the host directory ownership/modes and repeat the lifecycle rather than running the container as root.

## Browser failure artifacts

A browser failure after a run directory has been allocated is recorded under the private artifact root, normally `data/application-runs/run-<id>/`. The run manifest is `run.json`; it indexes artifacts by relative path, SHA-256 digest, iteration, and stage. A browser-operation failure is written as `browser_failure.json`; a failure while closing a session may additionally produce `browser_cleanup_failure.json`.

The failure record has a fixed, versioned shape:

```json
{
  "version": 1,
  "stage": "<stage>",
  "operation": "<operation>",
  "code": "<allowlisted-code>",
  "iteration": 1,
  "ats_policy": "greenhouse",
  "no_final_submit": true,
  "protocol": "length-prefixed-json-v1"
}
```

`code` is normalized to the browser adapter’s privacy-safe allowlist. Examples include `browser_preflight_error`, `browser_launch_timeout`, `navigation_timeout`, `unsafe_navigation_target`, `unsafe_network_attempt`, `observation_too_large`, and `artifact_error`; an unknown diagnostic becomes `browser_command_failed`. The record contains no raw exception text, page URL, DOM, network body, credential, token, resume content, or model response.

Other evidence remains separate and private. A run can contain `claim.json`, a staged `input/<basename>`, `job_description.txt`, `observation.json`, `plan.json`, `actions.json`, `filled_state.json`, per-iteration checkpoints, `review_session.json`, and `screenshots/`. The manifest’s top-level `no_final_submit` flag remains true. Public CLI results expose only bounded fields such as run ID, status, reason code, ATS, artifact reference, and window state; use `autofill-review list` or `show` for that projection rather than publishing private JSON.

Screenshots are also bounded and indexed by content digest. Each screenshot response contains a safe PNG path, `reference` (`screenshot:<sha256>`), byte count, SHA-256, `full_page`, `truncated`, pixel dimensions, and a deduplication flag. Only the `initial`, `after-reveal`, `blocker`, and `final` slots are accepted. A run may retain at most 10 distinct screenshots, each at most 20 MiB, with at most 50 MiB total. The browser runner also enforces bounded run/input file counts and sizes; a budget violation fails closed with `artifact_budget` or `file_budget`.

A failure before artifact allocation, or an artifact write that cannot be verified, may leave no `browser_failure.json` and may leave the public artifact reference empty. In that case the CLI emits only a fixed redacted error object; do not recover by reading process stderr into an issue or by disabling artifact validation. Paths are descriptor-confined, reject traversal and unsafe symlinks, and must stay under an owner-private artifact root.

## Safe verification

The following checks are safe to run from the repository root. They exercise setup, contracts, adapter behavior, and headless container behavior; none clicks a final submit control. They do **not** perform or claim a physical headed review, trusted-gesture handoff, manual field completion, or human submission.

```bash
# Lock and full Python suite.
uv lock --check
uv run --frozen --extra dev python -m pytest

# CLI surface checks (the two resume surfaces are intentionally distinct).
uv run --frozen jobs-assistant --help
uv run --frozen jobs-assistant autofill --help
uv run --frozen jobs-assistant application-rpc --help
uv run --frozen jobs-assistant autofill-review --help
uv run --frozen jobs-assistant resume-generate --help
uv run --frozen resume-generate --help

# Isolated package/wheel and safe no-job workflow smoke.
sh scripts/smoke.sh

# Node/Puppeteer protocol smoke.
npm run puppeteer-smoke

# Curated Puppeteer verification. Use this package script, not a raw
# integration pytest command; it excludes physical trusted-gesture tests.
npm run puppeteer-verify

# Compose build, ownership, and headless runtime smoke.
sh scripts/container-smoke.sh
```

Do not substitute a raw `pytest` invocation that selects physical handoff or trusted-gesture tests for `npm run puppeteer-verify`. Those checks require a human and a real headed window and are outside this operations guide.

## Troubleshooting without weakening safety

| Symptom | Safe response |
| --- | --- |
| `uv lock --check` fails | Reconcile `pyproject.toml` and `uv.lock`; keep `--frozen` for verification and do not silently relock in a deployment check. |
| Browser preflight or launch failure | Confirm Node/npm setup, run `npm run puppeteer-smoke`, and keep host headed runs on a host display. Do not switch a Compose run to `--headed`. |
| `artifact_root_error`, `artifact_error`, or privacy failure | Use a repository-local artifact root, repair mode `0700` directories and `0600` files, and rerun the safe smoke. Do not disable hash, path, or mode validation. |
| `unsafe_navigation_target` or `unsafe_network_attempt` | Treat the run as blocked. Verify the canonical Greenhouse/Lever route and source policy; do not retry with an unguarded browser or raw URL navigation. |
| `root_uid_unsupported` or bind-mount permission errors | Run Compose as a non-root owner, export matching `HOST_UID`/`HOST_GID`, repair host modes, and rerun `sh scripts/container-smoke.sh`. |
| A run is ready for review | Use `uv run --frozen jobs-assistant autofill-review list` and `show`. Any physical review, manual completion, and final submission remains outside automated verification and under human control. |

For contributor sequencing, ownership, and final container evidence, follow [OMP_CMUX_WORKFLOW.md](../OMP_CMUX_WORKFLOW.md). The project backlog and known follow-ups are in [TODO.md](../TODO.md). Return to the [project README](../README.md) for the documentation index.
