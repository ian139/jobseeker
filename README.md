# jobs-assistant

Minimal local job scraper/backlog ingestion assistant.

The active app is intentionally small. The set-in-stone active surface is:

- scraper/backlog ingestion into SQLite;
- filtering and quality gates around source jobs;
- profile-shaped search ideas for TheirStack;
- TheirStack credit-safe preview/paid-fetch helpers;
- optional JSON/API feed import for fixtures and backfills.

The applier remains a product concept, but the current observer/resolver/executor/runner implementation was archived as unsettled code. Future application automation should be rebuilt deliberately through OMP `workflowz`, not extended from the active minimal app by accident.

## Active package

**Package:** `jobs_assistant`
**CLI:** `jobs-assistant`
**Active source:** `src/jobs_assistant/`
**Active tests:** `tests/`

## Safety invariants

These remain product rules even while the applier is archived:

- Never mass-submit applications.
- Never click final submit.
- Never infer sensitive, legal, identity, CAPTCHA, assessment, sign-in, payment, unknown, or manual-only answers.
- Preserve human review/confirmation before any eventual submit workflow.
- Treat archived applier code as reference-only, not production behavior.

## Active modules

| Module | Role |
|---|---|
| `contracts` | Minimal ingestion/domain dataclasses: jobs, source jobs, sync metadata, credit estimates |
| `db` | SQLite jobs and sync-runs schema, connection helpers, canonical URL handling, raw job upsert helpers |
| `backlog` | Backlog upsert, dedupe, queue listing, backlog counts |
| `theirstack` | TheirStack payload builders, credit-safety checks, client, response sync helpers |
| `job_source` | Optional normalized `GET /v1/jobs` / JSON fixture import boundary |
| `cli` | Minimal command-line entrypoint |

## Active CLI commands

| Command | Description |
|---|---|
| `init-db` | Initialize the SQLite jobs/sync database |
| `import-feed` | Import normalized jobs from a JSON fixture or `GET /v1/jobs` feed |

TheirStack remains active as library code and product direction. A minimal `sync-theirstack` CLI is still roadmap work unless present in the active branch.

## Archived applier

The applier concept is set in stone, but the current active implementation was intentionally removed from the active package.

Archived first-principles applier files live under:

```text
archive/minimized-20260706/applier/
```

They include the prior observer/resolver/executor/runner/review/live-smoke source and tests. That archive is reference-only and not a runnable package snapshot; its source depended on the root `contracts.py` and `db.py` as they existed before minimization.

Older applier/scraper attempts remain under:

```text
archive/old-scraper/
archive/old-applier/
```

Do not revive archived applier code directly. Rebuild the applier as a workflowz-managed sequence with explicit contracts and tests:

```text
scraped job/backlog row
  -> observe application page
  -> resolve safe answers from profile/resume/job context
  -> execute guarded non-final actions
  -> write review evidence
  -> stop before final submit
```

## OMP workflowz practice

Use OMP `workflowz` for substantial work:

- define the goal, metric, target threshold, measurement source, and stop condition;
- split work into explicit subtasks with target files, non-goals, acceptance criteria, and verification commands;
- delegate relevant work to appropriate subagents by need: Tester for tests, reviewer for safety/quality, task workers for implementation, designer only for UI/design work;
- keep the parent worktree as integration authority;
- archive or reject unrelated implementation drift.

For future applier work, workflowz should assign separate subtasks for observation, resolver contract, executor guardrails, persistence/output, and safety review. No worker should own final-submit behavior until a submit policy exists and is tested.

## Quick checks

```bash
uv lock --check
uv run --frozen --extra dev python -m pytest
uv run --frozen jobs-assistant --help
```

## Container smoke

```bash
docker compose build
docker compose up -d
docker compose ps
docker compose down
```

## Archives

Historical snapshots live under `archive/` as reference only. Do not import from archived code in active modules.

Important archive entries:

- `archive/minimized-20260706/applier/`: the removed active applier implementation from minimization.
- `archive/old-scraper/`: previous scraper/application-assistant snapshot.
- `archive/old-applier/`: older monolithic applier and applicant data.
- `archive/old-applier/data/Main_Resume.pdf`: preserved applicant resume data; do not modify.
- `archive/REBUILD_PROMPT.md`: rebuild contract and archive map.
- `archive/notes/`, `archive/prompts/`: archived research and prompt templates.

## What not to touch casually

- `archive/` code and data.
- Runtime SQLite data under `data/` or `scraper/data/` unless explicitly instructed.
- Applicant profile/resume data except through documented profile workflows.
