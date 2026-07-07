# Archive Index

This directory contains full code snapshots from the earlier incarnation of the project. The active, maintained application lives at the repository root (`src/`, `tests/`, `pyproject.toml`, etc.). Everything under `archive/` is **reference-only** — helpful for understanding design decisions and what was tried before, but not active code. Do not import from, extend, or run these files.

---

## REBUILD_PROMPT.md

| Field | Content |
|---|---|
| **Source** | First-principles rebuild prompt generated from the archived codebases (old-scraper, old-applier, notes, prompts). |
| **Status** | Reference document only. Describes the north-star design that the active repo was rebuilt toward. |
| **Why it mattered** | Captured the core design intent (local developer-operated assistant, not mass auto-apply) and defined exactly which patterns to keep (observer/resolver/executor split, credit-safe TheirStack ingestion, guarded actions) and which to discard (monolithic web UI, 24-hour daemon, broad matching/scoring engine, board-specific Playwright templates, Outreach/BotDog workflows). Served as the single-page specification for the rebuild effort. |
| **Valuable pieces preserved** | The observer/resolver/executor architectural split; normalized PageSnapshot contract; strict resolver JSON with `needs_review` refusal for unknown/sensitive fields; credit-safe TheirStack preview-before-paid pattern; deterministic SQLite dedupe; terminal run statuses (`dry_run_ready`, `needs_review`, `blocked`, `failed`); the explicit "stop before final submit" rule. |
| **Do not reuse blindly because** | The prompt is a specification, not code. The active `src/` was built from these principles but with fresh implementation. Also, the split suggested between scraper and applier services was collapsed into a single package in the active repo. |

---

## minimized-20260706/applier/

| Field | Content |
|---|---|
| **Source** | The active first-principles applier modules removed during the 2026-07-06 minimization: observer, resolver, LLM adapter, executor, runner, review sampler, live smoke, and their tests. |
| **Status** | Reference-only, non-runnable extracted code. The root `contracts.py` and `db.py` dependencies were minimized after extraction. |
| **Why it mattered** | Preserves the immediate pre-minimization applier behavior: static page observation, deterministic answer resolution, guarded execution, run persistence, and review/failure sampling. |
| **Valuable pieces preserved** | No-final-submit boundary, sensitive/manual field refusal, configured-resume-only upload, action attempt records, and the observer/resolver/executor/runner split. |
| **Do not reuse blindly because** | The implementation was explicitly judged unsettled/bad for active development. Rebuild future applier work through OMP `workflowz` with fresh contracts and tests. |

---

## old-scraper/

| Field | Content |
|---|---|
| **Source** | Last active `job-sync` Python package: TheirStack job ingestion, SQLite backlog, and application pipeline (observer/resolver/executor/runner/policy). |
| **Status** | Superseded by `src/` at the repository root. All code here is frozen and read-only. |
| **Why it mattered** | This was the final working snapshot before the root-codebase refactor. It contained the real TheirStack HTTP client (`src/theirstack/`), job sync and dedupe logic (`src/sync/jobs.py`), the full application pipeline (`src/apply_pipeline/` — observer, resolver, executor, runner, policy, contracts, backlog, LLM adapter), and SQLite schema (`src/db/schema.sql`). Tests covered queries, sync, pipeline helpers, and job-source import. |
| **Valuable pieces preserved** | TheirStack query payload building (`src/theirstack/queries.py`); credit-safe preview/count logic before paid fetches; deterministic dedupe by TheirStack job ID or canonical URL; SQLite schema design (`jobs`, `sync_runs`, `application_runs`, `application_pages`); normalized PageSnapshot and RunDecision contracts; policy guardrail logic for final-submit and sensitive-field refusal; run-loop persistence helpers; failure-sampling logic; the `--live`, `--headed`, `--manual-handoff` dry-run CLI concepts. |
| **Do not reuse blindly because** | The code was restructured into a cleaner package layout at `src/` with updated CLI entrypoints and simplified dependencies. The scraper sub-package (theirstack/, sync/, db/) and apply_pipeline were merged under a single namespace. Environment variable names, import paths, and CLI commands all changed. Porting without adaptation would break. |

---

## old-applier/

| Field | Content |
|---|---|
| **Source** | Older monolithic `job-scraper` archive — a Playwright-based TheirStack job scraper with FastAPI web UI, 24-hour daemon scheduler, matching engine, LLM integration, outreach module, and monolithic applier. |
| **Status** | Fully superseded. Retained only as behavioral evidence and historical reference. |
| **Why it mattered** | This was the first attempt and proved several concepts: SQLite-backed job storage with dedupe; TheirStack API integration; Playwright-based form filling; LLM-based answer resolution; credit accounting for TheirStack fetches; and the idea of a daemon-driven 24-hour sync cycle. It also contained the original applicant resume data. |
| **Valuable pieces preserved** | `archive/old-applier/data/Main_Resume.pdf` — applicant resume data, preserved as-is. TheirStack credit-safety approach (preview before paid fetch). The concept of application packs (serialized run records). The outreach contact management model. |
| **Do not reuse blindly because** | This version was a monolith: a single `job_scraper` package bundling FastAPI web UI, scheduler daemon, Playwright applier, LLM resolver, matching engine, outreach module, resume uploader, and public JSON importer into one tight dependency graph. Key classes were large (86 KB `web.py`, 89 KB `matching.py`, 31 KB `storage.py`). It scheduled mass-sync daemons, included BotDog outreach automation, and had a broad matching/scoring engine — all explicitly excluded from the active rebuild. The active repo extracted only the ingestion and application-pipeline cores. The config, env vars, entrypoint names, and directory layout bear no resemblance to the current `src/`. |

---

## notes/

| Field | Content |
|---|---|
| **Source** | Archived research notes captured during development. Currently contains `scraper-theirstack.md`. |
| **Status** | Reference-only. Not updated and not part of the active codebase. |
| **Why it mattered** | Documented theirstack-scraper setup, operation (preview-count, run-once, daemon), BotDog outreach workflow, credit/checkpointing strategy, and source-domain filter configuration. Captured rationale for 24-hour puller over webhooks (intentional choice). |
| **Valuable pieces preserved** | TheirStack API key setup instructions; preview-count credit-safety approach (`blur_company_data: true`, `include_total_results: true`, `limit: 1`); the 10-minute discovery-time overlap for checkpointing; BotDog outreach queue/manual-send pattern; the list of common ATS domain filters. |
| **Do not reuse blindly because** | The notes describe the old monolithic `job-scraper` CLI commands (`job-scraper daemon`, `job-scraper outreach`, `job-scraper run-once`) that no longer exist in the active codebase. TheirStack setup remains conceptually useful but the actual CLI flags and config paths changed. The BotDog outreach workflow was intentionally excluded from the rebuild. |

---

## prompts/

| Field | Content |
|---|---|
| **Source** | Prompts and handoff templates used during the rebuild, notably `repo-handoff.md`. |
| **Status** | Template reference only. Not consumed by the active application. |
| **Why it mattered** | Provided the structured prompt format for delegating subagent work during the rebuild: goal, allowed files, forbidden files, development rules (test-first), acceptance criteria, verification command. |
| **Valuable pieces preserved** | The prompt template pattern (goal / allowed-files / forbidden-files / test-first / acceptance / verification). The structure itself is a reusable template for agent work decomposition. |
| **Do not reuse blindly because** | The template contains placeholder variables ({{repo}}, {{worktree}}, {{goal}}, {{allowed_files}}, {{forbidden_files}}, {{acceptance}}, {{devloop_command}}) that must be filled in per task. It refers to project conventions (AGENTS.md, OMP_ORCA_WORKFLOW.md) that the user must confirm still apply. The test-first and sub-worktree instructions match the rebuild phase and may not suit maintenance work. |