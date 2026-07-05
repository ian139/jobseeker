# jobs-assistant

A local, developer-operated job backlog and application dry-run assistant built from first principles.

**Project package:** `jobs_assistant` (installed as the `jobs-assistant` CLI).
**Active source:** `src/jobs_assistant/` — observer, resolver, executor, runner, backlog ingestion, TheirStack client, review sampler, and the CLI.
**Tests:** `tests/`.

## Safety invariants

- Never mass-submit applications.
- Never click final submit.
- Never infer sensitive, legal, identity, CAPTCHA, assessment, sign-in, payment, unknown, or manual-only answers.
- Prepare one queued job to the final-submit boundary, persist evidence, and stop for human review.
- Observer/resolver/executor/runner split keeps each responsibility isolated.

## Core modules

| Module | Role |
|---|---|
| `db` | SQLite schema, connection, run/page persistence |
| `backlog` | Backlog ingestion and dedupe |
| `theirstack` | Credit-safe TheirStack preview/paid-fetch boundary |
| `job_source` | Optional read-only `GET /v1/jobs` ingestion boundary |
| `observer` | Deterministic static-HTML normalized page snapshots |
| `resolver` | Strict guarded answer decisions |
| `llm` | Optional narrow LLM resolver adapter; fails closed when unconfigured |
| `executor` | Guarded fill/select/check/upload/non-final-click actions |
| `runner` | One-job dry-run orchestration; persists pages/actions, stops before final submit |
| `review` | Failure/review sampling |
| `cli` | `jobs-assistant` command-line entrypoint |

## Quick checks (from clean checkout, using committed `uv.lock`)

```bash
uv lock --check
uv run --frozen --extra dev python -m pytest
uv run --frozen jobs-assistant --help
```

## Without `uv`

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
.venv/bin/python -m pytest
.venv/bin/jobs-assistant --help
```

## Optional live Playwright smoke

```bash
# install live extras and browsers
pip install -e '.[live]'
python -m playwright install chromium
jobs-assistant live-smoke
```

## CLI commands

| Command | Description |
|---|---|
| `init-db` | Initialize the SQLite database |
| `import-feed` | Import normalized jobs from JSON file or `GET /v1/jobs` feed |
| `dry-run-static` | Run one queued job against static HTML files |
| `sample-failures` | Group failed/blocked/needs-review runs |
| `live-smoke` | Check optional Playwright live adapter availability |

## Dry-run

```bash
# Prepare one job to the final-submit boundary (static HTML fixture)
uv run --frozen jobs-assistant dry-run-static --job-id 1 --html page1.html page2.html --facts-json '{"name":"Test"}'
```

## Container smoke

```bash
docker compose build
docker compose up -d
docker compose ps
docker compose down
```

## Archives

Historical snapshots live under `archive/` as behavioral evidence only — do not modify:

- `archive/old-scraper/`: last active scraper/application-assistant snapshot.
- `archive/old-applier/`: older monolithic applier archive and applicant data.
- `archive/old-applier/data/Main_Resume.pdf`: archived resume — **do not modify**.
- `archive/REBUILD_PROMPT.md`: rebuild contract and archive map.
- `archive/notes/` and `archive/prompts/`: archived research notes and handoff material.

## First docs to read

- **AGENTS.md** — policy, architecture, safety, development rules, and OMP workflowz/cmux child-worktree rules (auto-loaded by OMP).
- **OMP_CMUX_WORKFLOW.md** — OMP workflowz + cmux reusable operating workflow.

## What not to touch

- `archive/` files — reference only, do not edit unless explicitly updating archive notes.
- `archive/old-applier/data/Main_Resume.pdf` — preserved applicant data; must not be modified.
- Active Python source under `src/jobs_assistant/` unless explicitly assigned.