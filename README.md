# Jobs

Local tooling for building a developer-operated job backlog and preparing job applications up to the final-submit boundary. This repo is not a mass auto-apply bot: it can collect jobs, record application runs, observe forms, resolve known answers, and plan guarded actions, but the operator remains responsible for review and final submission.

## OMP and Orca agents

Use `OMP_ORCA_WORKFLOW.md` for OMP, Orca, and Orca dev execution in this repo. `AGENTS.md` is mandatory project policy and is auto-loaded when agents start from the repository root, so prompts do not need to restate it.

Default implementation workers use DeepSeek V4 Pro through Ollama Cloud:

```bash
omp --model "ollama-cloud/deepseek-v4-pro" --thinking medium
```

Start non-trivial work with `/plan`, write or update the focused failing test first, use sub-worktrees when isolation helps, and verify the finished change through the containerized path before marking it ready.

## What this project does

The active product direction is a local job-application assistant:

1. Store a SQLite backlog of jobs.
2. Open an application URL for a queued job.
3. Observe the page into a normalized snapshot: fields, buttons, visible errors, and blockers.
4. Resolve only known, allowed answers from profile/resume facts.
5. Execute guarded non-final actions, such as filling fields, selecting options, uploading the configured resume, and clicking safe Next/Continue navigation.
6. Stop at final submit or any unsafe/unknown condition.
7. Persist run status and page snapshots for review.

Current status: the ingestion, database schema, pure observer/resolver/executor helpers, run storage, failure sampler, external read-feed import, and dry-run runner are present. The default `apply-dry-run` command stays safe and records scaffold failures unless `--live` is supplied; live browser runs use Playwright when installed with the `live` extra and still stop before final submit.

## Safety boundaries

Hard rules enforced by project direction and policy code:

- Never mass-submit applications.
- Never click final submit.
- Never bypass sign-in, CAPTCHA, assessments, payment, identity, or email verification flows.
- Never infer sensitive answers such as SSN, date of birth, gender, race, ethnicity, disability, veteran status, signatures, CAPTCHA, or unknown legal attestations.
- Upload only the configured resume file.
- Prefer `needs_review` or `blocked` over guessing.
- Paid TheirStack fetches require explicit approval and `ENABLE_PAID_FETCH=true`.

Terminal application statuses are:

- `dry_run_ready`: final-submit boundary reached; final submit was not clicked.
- `needs_review`: manual/operator input is required.
- `blocked`: sign-in, CAPTCHA, job gone, unsupported flow, disabled navigation, or similar blocker.
- `failed`: browser, parser, resolver, executor, navigation, or scaffold failure.

## Key features

### TheirStack job backlog

- Builds credit-safe preview payloads for TheirStack search.
- Can call TheirStack preview/count endpoints without enabling paid fetches.
- Syncs returned jobs into SQLite only when paid fetches are explicitly enabled.
- Deduplicates by TheirStack job ID or canonical URL.
- Selects one job per company by default for paid sync.

### Application pipeline core

- `observer`: deterministic HTML/page observation into `PageSnapshot`.
- `resolver`: deterministic mapping from known facts to answers; refuses unknown required and sensitive fields.
- `executor`: guarded actions only; refuses final-submit clicks and unapproved file uploads.
- `runner`: records application runs and page snapshots, loops until terminal status or `max_pages`.
- `policy`: central safety checks for final submit, sensitive fields, blockers, and safe navigation.

### Persistence

SQLite tables live in `scraper/src/db/schema.sql`:

- `jobs`: local backlog.
- `sync_runs`: TheirStack sync history.
- `application_runs`: terminal application run outcomes.
- `application_pages`: per-page snapshots and resolver outputs.

## Repository layout

```text
.
├── AGENTS.md                    # operating notes and safety policy
├── todo.md                      # implementation plan and current status
├── README.md                    # this file
├── scraper/
│   ├── pyproject.toml           # Python package, dependencies, job-sync entrypoint
│   ├── Dockerfile               # container image for CLI/tests package install
│   ├── docker-compose.yml       # app + mounted SQLite data volume
│   ├── .env.example             # environment variable template
│   ├── src/
│   │   ├── theirstack/          # TheirStack query payloads and HTTP client
│   │   ├── sync/                # job-sync CLI, SQLite sync, dedupe
│   │   ├── db/schema.sql        # SQLite schema
│   │   └── apply_pipeline/      # observer/resolver/executor/runner/contracts
│   └── tests/                   # unit tests for sync, queries, and pipeline helpers
└── old/                         # archived code; not active entrypoints
```

## Setup

From a clean checkout, create a local virtual environment and copy the example environment file:

```bash
cd scraper
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
cp .env.example .env
```

For local host runs, edit `.env` before initializing the database so `JOB_SYNC_DB_PATH` is a host-relative path, not the container path from `.env.example`:

```bash
JOB_SYNC_DB_PATH=data/jobs.sqlite3
```

You can also remove `JOB_SYNC_DB_PATH` from `.env`; the host CLI then falls back to `data/jobs.sqlite3`.

Python 3.11 or newer is required; the Docker image currently uses Python 3.12. The declared runtime dependencies are `httpx` and `python-dotenv`; test dependency is `pytest`. Live browser dry runs additionally use the optional `live` extra, which installs Playwright.

Initialize the local SQLite database after that local DB path is set or unset:

```bash
cd scraper
.venv/bin/job-sync init-db
```

By default the CLI uses `data/jobs.sqlite3` unless `JOB_SYNC_DB_PATH` is set.

## Environment variables

Defined in `scraper/.env.example`:

```bash
THEIRSTACK_API_KEY=
ENABLE_PAID_FETCH=false
JOB_SYNC_DB_PATH=/app/data/jobs.sqlite3
THEIRSTACK_BASE_URL=https://api.theirstack.com
JOB_SOURCE_BASE_URL=
JOB_SOURCE_API_KEY=
OLLAMA_CLOUD_API_KEY=
OLLAMA_CLOUD_BASE_URL=https://ollama.com
OLLAMA_CLOUD_MODEL=deepseek-v4-pro
```

Host/local settings:

- `THEIRSTACK_API_KEY`: required only for API calls.
- `ENABLE_PAID_FETCH`: must be `true` before `sync-once` can make paid TheirStack requests.
- `JOB_SYNC_DB_PATH`: set to `data/jobs.sqlite3` for host runs, or leave it unset to use that same host default. Keep `/app/data/jobs.sqlite3` for Docker/compose only.
- `THEIRSTACK_BASE_URL`: defaults to `https://api.theirstack.com`.
- `JOB_SOURCE_BASE_URL` and `JOB_SOURCE_API_KEY`: required only for `import-job-source`, which reads `GET /v1/jobs` and imports normalized jobs into the local backlog.
- `OLLAMA_CLOUD_API_KEY` or `OLLAMA_API_KEY`: optional; when present, live apply dry runs call the Ollama Cloud OpenAI-compatible chat API after deterministic fact matching.
- `OLLAMA_CLOUD_BASE_URL`: defaults to `https://ollama.com`; the client posts to `/v1/chat/completions`.
- `OLLAMA_CLOUD_MODEL` or `DEEPSEEK_MODEL`: defaults to `deepseek-v4-pro`.

## TheirStack commands

Print preview payloads without calling the API:

```bash
cd scraper
.venv/bin/job-sync dry-run
```

Call the safe preview/count path. This should not consume paid fetch credits:

```bash
cd scraper
.venv/bin/job-sync dry-run --call-api --posted-at-max-age-days 2
```

Run a paid sync only after explicit approval. Use the preview result to choose the `--limit`; returned jobs can spend credits:

```bash
cd scraper
ENABLE_PAID_FETCH=true JOB_SYNC_DB_PATH=data/job_sync_test.sqlite3 \
.venv/bin/job-sync sync-once --limit <preview_total> --max-pages 1 --posted-at-max-age-days 2
```

Useful sync options:

```bash
.venv/bin/job-sync sync-once --profile fall_coop_swe_data --limit 25 --max-pages 1
.venv/bin/job-sync sync-once --allow-multiple-per-company --limit 25 --max-pages 1
```

## Application dry-run commands

Safe scaffold mode. This records attempted runs as `failed` with a reason explaining that live browser wiring is not being used, so jobs are not permanently skipped:

```bash
cd scraper
.venv/bin/job-sync apply-dry-run --limit 1 --max-pages 6
```

Experimental live mode. Install the optional dependency group and Playwright browser once before using it:

```bash
cd scraper
.venv/bin/python -m pip install -e '.[dev,live]'
.venv/bin/playwright install chromium
```

Then run. If `--profile-json` and `--resume` are omitted, the live dry run uses `AGENTS.md` applicant reference defaults: `Main_Resume.pdf`, LinkedIn, and personal site. If `OLLAMA_CLOUD_API_KEY` or `OLLAMA_API_KEY` is set, live mode also uses the optional Ollama Cloud DeepSeek resolver after deterministic fact matching; configure it with `OLLAMA_CLOUD_BASE_URL` and `OLLAMA_CLOUD_MODEL` (`deepseek-v4-pro` by default). Login/sign-in pages, password/code fields, CAPTCHA, and assessment blockers stop as `blocked` before filling or LLM resolution. The LLM receives only eligible unresolved non-sensitive field metadata, profile facts, and job description text; it does not read or upload the resume PDF as prompt context, so put resume-derived facts such as skills or summaries in `--profile-json` when you want them available for answer mapping.

```bash
cd scraper
OLLAMA_CLOUD_API_KEY=... .venv/bin/job-sync apply-dry-run --live --limit 1 --max-pages 6
```

Explicit profile/resume arguments override those defaults:

```bash
.venv/bin/job-sync apply-dry-run --live --limit 1 --max-pages 6 \
  --profile-json path/to/profile.json \
  --resume path/to/resume.pdf
```

Add `--headed` to show the browser, and add `--manual-handoff` to keep it open after `dry_run_ready`, `needs_review`, `blocked`, or `failed` so you can inspect/edit the page. Pressing Enter does not resume automation or submit anything; it ends inspection and closes the Playwright browser context. Use `--no-llm` to force deterministic-only resolution even when Ollama Cloud credentials are present:

```bash
.venv/bin/job-sync apply-dry-run --live --headed --manual-handoff --limit 1 --max-pages 6 \
  --profile-json path/to/profile.json \
  --resume path/to/resume.pdf
```

Profile JSON is a flat object of known facts, for example:

```json
{
  "full_name": "Ada Lovelace",
  "email": "ada@example.com",
  "phone": "+1 555 0100",
  "location": "Toronto, ON",
  "linkedin": "https://www.linkedin.com/in/ada",
  "github": "https://github.com/ada",
  "portfolio": "https://ada.example.com"
}
```

Use `--resume` to change the upload file. A `resume_path` value inside profile JSON is treated as a fact only; it does not change the guarded upload path.

Review recent application failures:

```bash
cd scraper
.venv/bin/job-sync apply-sample-failures --status blocked --limit 10
.venv/bin/job-sync apply-sample-failures --status failed --limit 10
```

Import from an optional external read-only feed after `JOB_SOURCE_BASE_URL` and `JOB_SOURCE_API_KEY` are configured:

```bash
cd scraper
.venv/bin/job-sync import-job-source --limit 100 --lane fall-coop --query "software engineer"
```

## Tests and verification

Run the Python test suite from `scraper/`:

```bash
cd scraper
.venv/bin/python -m pytest
```

For normal Python changes, this is the expected verification gate. For TheirStack query changes, also run the safe preview command:

```bash
cd scraper
.venv/bin/job-sync dry-run --call-api --posted-at-max-age-days 2
```

Do not claim container verification unless these commands were actually run:

```bash
cd scraper
docker compose build
docker compose up -d
docker compose ps
docker compose down
```

## Container use

The container setup lives under `scraper/`:

```bash
cd scraper
docker compose build
docker compose run --rm app job-sync dry-run
docker compose run --rm app job-sync init-db
```

The compose file mounts `scraper/data` into `/app/data` and sets `JOB_SYNC_DB_PATH=/app/data/jobs.sqlite3`. It defaults `ENABLE_PAID_FETCH=false`. The image does not bake in `AGENTS.md` or `Main_Resume.pdf`; non-live dry runs work without them, and live apply dry runs need explicit mounted profile/resume inputs or an app-visible applicant reference.
If `scraper/data/jobs.sqlite3` is an old database with a different schema, run container smoke checks against a fresh path instead of overwriting it:

```bash
docker compose run --rm -e JOB_SYNC_DB_PATH=/app/data/pipeline_smoke.sqlite3 app job-sync init-db
docker compose run --rm -e JOB_SYNC_DB_PATH=/app/data/pipeline_smoke.sqlite3 app job-sync dry-run
```

## Development notes

- Keep business logic pure where possible; keep side effects at CLI, HTTP, filesystem, browser, and SQLite boundaries.
- Add tests for new observer/resolver/executor branches.
- Do not add board-specific brittle automation before collecting failure samples and reasons.
- Do not move active code into `old/`; archived code is not active.

## Related docs

- `OMP_ORCA_WORKFLOW.md` has the reusable OMP + Orca development workflow.
- `AGENTS.md` has mandatory agent policy for architecture, orchestration, workers, and verification.
- `TODO.md` has the setup and usage checklist.
