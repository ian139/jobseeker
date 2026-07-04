# First-principles rebuild prompt

## What the archived project was trying to do

The archived project was trying to build a local, developer-operated job backlog and application-preparation assistant, not a mass auto-apply bot.

Target workflow:

- Store a local SQLite backlog of jobs.
- Open one queued job's application URL.
- Observe the page into a deterministic normalized DOM/page snapshot.
- Resolve answers with an LLM-first resolver using the snapshot plus explicit profile, resume, facts, job description, and policy context.
- Execute only guarded generic browser actions.
- Stop before final submit.
- Persist run status, page snapshots, resolver outputs, and action attempts for review.

Terminal run statuses were:

- `dry_run_ready`: ready at final submit; not submitted.
- `needs_review`: unknown, sensitive, or manual field.
- `blocked`: sign-in, CAPTCHA, no form, job gone, weird upload, or unsupported workflow.
- `failed`: browser, LLM, parser, executor, or navigation failure.

## Scraper / backlog sync, separately

Archived active paths:

- `archive/old-scraper/src/theirstack/`
- `archive/old-scraper/src/sync/jobs.py`
- `archive/old-scraper/src/db/schema.sql`

Purpose:

- Build credit-safe TheirStack preview payloads.
- Optionally call preview/count endpoints without paid fetches.
- Sync paid results only after explicit approval.
- Dedupe by TheirStack job ID or canonical URL.
- Import an optional read-only `GET /v1/jobs` feed at the ingestion boundary.

Keep important:

- Credit safety: `ENABLE_PAID_FETCH=true` required for paid sync.
- Boring SQLite tables: `jobs`, `sync_runs`.
- Deterministic parsing and dedupe.
- One-job-per-company default.
- Source-specific logic isolated at the ingestion boundary.

Not important for rebuild:

- Monolithic web UI.
- 24-hour daemon.
- Broad matching/scoring engine.
- Outreach/BotDog workflows.
- Brittle ATS/domain-specific scraping.
- Any code shape that couples ingestion to application execution.

## Auto-applier / application assistant, separately

Archived paths:

- Current application assistant: `archive/old-scraper/src/apply_pipeline/`
- Older monolithic applier: `archive/old-applier/src/job_scraper/applier.py`

Purpose:

Prepare one queued job up to the final-submit boundary without submitting.

Keep important:

- Observer/resolver/executor split.
- Normalized `PageSnapshot` fields/buttons/errors/blockers.
- Strict resolver JSON.
- `needs_review` for unknown, sensitive, or manual fields.
- Executor can only fill, select, check, upload the configured resume, and click non-final navigation.
- Persist `application_runs` and `application_pages`.
- Every browser action logged.

Not important for rebuild:

- Board-specific Playwright templates.
- Direct final-submit support.
- Inferring legal/sensitive answers.
- Bypassing auth, CAPTCHA, or assessments.
- Automatic mass runs.
- Large object/class hierarchies.
- UI proof that substitutes mocks or a wrong browser/profile for the actual target.

## Rebuild prompt to give a fresh engineer/agent

```text
Rebuild this repository from first principles as a local, developer-operated job application assistant. Do not port code blindly from the archive; use the archives only as behavioral evidence.

Product goal:
- Maintain a local SQLite backlog of jobs.
- Ingest jobs from TheirStack or another read-only source through a narrow ingestion boundary.
- Prepare individual applications to the final-submit boundary and stop for human review.
- Never mass-submit, never click final submit, and never answer sensitive/legal/unknown fields by inference.

Scraper/backlog service:
- Build credit-safe TheirStack preview/count payloads before any paid fetch.
- Require explicit paid-fetch enablement before syncing returned jobs.
- Store normalized jobs in SQLite with deterministic dedupe by source job ID or canonical URL.
- Keep source-specific code at the ingestion boundary; the application pipeline should consume normalized backlog rows only.
- Prefer small pure functions for parsing, canonicalization, query payload construction, and dedupe decisions.

Application assistant service:
- Observe pages deterministically into a normalized snapshot: fields, buttons, labels, required state, options, values, visibility, frame/selector metadata, visible errors, and blockers.
- Resolve answers from the normalized snapshot plus explicit profile/resume/job facts and policy. The resolver returns strict JSON only: answers, next button, submit button, review reasons, metadata.
- Refuse unknown, sensitive, legal, identity, CAPTCHA, assessment, sign-in, payment, or manual-only fields with `needs_review` or `blocked`; do not guess.
- Execute only guarded generic actions: fill text, select, check, upload the configured resume, and click policy-approved non-final navigation.
- Stop at final submit with `dry_run_ready`; never click it.
- Persist runs and page snapshots so failures can be sampled and policies improved without board-specific automations.

Architecture constraints:
- Functional core, side effects at boundaries: CLI/browser/filesystem/HTTP/SQLite/LLM adapters.
- Separate scraper, observer, resolver, executor, runner, persistence, and review/failure-sampling responsibilities.
- Keep contracts explicit and typed; prefer simple dataclasses/schemas over class hierarchies.
- Add focused tests for every policy branch, especially sensitive-field refusal, final-submit refusal, blocker handling, dedupe, and retry/terminal status behavior.
- Container-first: dependencies declared, safe env defaults, smoke checks for CLIs/services.

Rebuild in this order:
1. SQLite schema and tiny backlog ingestion core.
2. Credit-safe TheirStack preview/sync with tests.
3. Static-HTML observer fixtures and normalized snapshot contract.
4. Resolver contract with deterministic guardrails and optional LLM adapter behind a narrow interface.
5. Guarded executor with fake-target tests before live browser use.
6. One-job dry-run runner that records snapshots/actions and stops at final submit.
7. Failure sampler/review loop for improving generic policies.
8. Only then add live Playwright smoke checks and optional external feed import.
```

## Archive map

- `archive/old-scraper/`: last active `job-sync` scraper/application-assistant snapshot, including original README and TODO.
- `archive/old-applier/`: older monolithic `job-scraper`/Playwright applier archive.
- `archive/old-applier/data/Main_Resume.pdf`: archived applicant resume data.
- `archive/notes/`: archived research notes.
- `archive/prompts/`: archived handoff prompt material.
- `skills/SKILL.md`: root prompt guidance intentionally left in place.
- `README.md`: root index for the rebuilt active app and archive locations.
