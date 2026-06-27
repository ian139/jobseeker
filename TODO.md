# Application Pipeline Implementation Plan

## North star

Build a local job application assistant that prepares applications to the final-submit boundary and then stops. It is not a mass auto-apply system.

## OMP and Orca workflow

- [ ] Start OMP from the repository root so `AGENTS.md` is auto-loaded.
- [ ] Use `OMP_ORCA_WORKFLOW.md` for coordinator, Orca worker, and Orca dev workflow.
- [ ] Begin non-trivial work with `/plan`.
- [ ] Add or update the focused failing test before implementation.
- [ ] Use DeepSeek V4 Pro through Ollama Cloud for implementation workers: `omp --model "ollama-cloud/deepseek-v4-pro" --thinking medium`.
- [ ] Use sub-worktrees when isolation, competing patches, or verification workers improve safety.
- [ ] Verify completed feature work through the containerized path before marking it ready.

## Environment

The operator remains responsible for reviewing and clicking final submit.

## Required lifecycle

```text
SQLite job backlog
 ↓
Playwright opens apply URL
 ↓
Deterministic observer scans DOM/frames
 ↓
Normalized page snapshot
  fields: id, kind, label, required, options
  buttons: id, text, type, disabled
  errors
 ↓
LLM resolver
  input: page snapshot + profile + resume + facts + job description + policies
  output: JSON answers + nextButton + submitButton
 ↓
Guarded executor
  fills text/select/radio/checkbox/typeahead/file fields
  uploads resume only
  clicks only safe non-final navigation
  never clicks final submit
 ↓
Loop up to configured page limit
  observe → resolve → fill → advance
 ↓
Run result
```

## Non-negotiable safety rules

- Never click final submit.
- Never bypass CAPTCHA, sign-in, assessments, payment, identity, or email verification flows.
- Never infer sensitive answers: SSN, DOB, gender, race, ethnicity, disability, veteran status, signatures, legal attestations, work authorization if not explicitly known.
- Upload only the configured resume file.
- Prefer `needs_review` over guessing.
- Every browser action must be logged.
- Every page snapshot should be persisted for debugging.

## Status model

Terminal `application_runs.status` values:

- `dry_run_ready`: final-submit boundary reached; final submit was not clicked.
- `needs_review`: page is answerable only with manual/operator input.
- `blocked`: sign-in, CAPTCHA, no form, job gone, unsupported upload/workflow, disabled navigation.
- `failed`: browser, observer, resolver, executor, or navigation error.

Transient step status:

- `continue`: safe actions were planned/executed and the run loop should observe the next page.

`continue` must never be stored as a terminal `application_runs.status`.

## Current foundation already in repo

- `scraper/src/theirstack/`: TheirStack source query/client.
- `scraper/src/sync/jobs.py`: SQLite job backlog ingestion and dedupe.
- `scraper/src/db/schema.sql`: `jobs`, `sync_runs`, `application_runs`, `application_pages`.
- `scraper/src/apply_pipeline/contracts.py`: shared dataclasses/status contracts.
- `scraper/src/apply_pipeline/policy.py`: pure guarded action policy.
- `scraper/src/apply_pipeline/backlog.py`: backlog selection helpers.

## Phase 1 — Backlog and run records

Goal: reliably pick jobs and record attempts.

### Tasks

1. Add application-run storage helpers.
  - File: `scraper/src/apply_pipeline/runs.py`
  - Functions:
    - `start_application_run(connection, job_id, started_at) -> int`
    - `finish_application_run(connection, run_id, status, reason, final_url, actions) -> None`
    - `record_application_page(connection, run_id, page_index, snapshot, resolver_output=None) -> None`
2. Extend backlog selection.
  - File: `scraper/src/apply_pipeline/backlog.py`
  - Keep selecting rows from `jobs` with no terminal application run.
  - Add filters later only if needed: company, date, status, limit.
3. Add tests.
  - File: `scraper/tests/test_apply_pipeline.py`
  - Prove terminal statuses skip a job.
  - Prove failed runs do not permanently skip a job unless policy says so.
  - Prove page snapshots are persisted as JSON.

### Acceptance

- A job can be selected, run started, page snapshot persisted, run finished.
- Terminal run prevents reselection.
- `continue` is not accepted as a terminal DB status.

## Phase 2 — Deterministic observer

Goal: convert a live page into a normalized page snapshot without deciding answers.

### Tasks

1. Implement observer module.
  - File: `scraper/src/apply_pipeline/observer.py`
  - Inputs: Playwright page/frame handle.
  - Output: `PageSnapshot`.
  - Capture:
    - `input`, `textarea`, `select`, radio, checkbox, file, contenteditable/typeahead-like fields
    - visible labels via `label[for]`, parent labels, aria-label, placeholder, nearby text
    - required state via `required`, `aria-required`, label markers
    - options for selects/radios/checkbox groups
    - buttons/links that look actionable
    - visible errors/blockers
2. Add static HTML fixture tests first.
  - File: `scraper/tests/test_apply_pipeline.py`
  - Current tests use inline static HTML fixtures; split to `scraper/tests/fixtures/apply_pages/` only when fixture size justifies it.
  - Cover:
    - simple text fields
    - select options
    - radio/checkbox groups
    - file upload
    - iframe/frame form if practical
    - disabled buttons
    - visible validation errors
3. Add Playwright dependency only when implementing live observation.
  - Update `scraper/pyproject.toml`.
  - Add a smoke command or skip marker for browser-dependent tests if needed.

### Acceptance

- Observer returns stable IDs for fields/buttons.
- Observer does not use LLMs or profile facts.
- Static fixture tests pass deterministically.

## Phase 3 — Resolver contract

Goal: map known facts to answers using strict JSON, while refusing unknown/sensitive fields.

### Tasks

1. Define resolver input/output JSON schemas.
  - File: `scraper/src/apply_pipeline/resolver.py`
  - Inputs:
    - `PageSnapshot`
    - profile facts
    - resume facts
    - job description/raw job
    - policies
  - Output:
    - `answers: [{field_id, value}]`
    - `next_button_id | null`
    - `submit_button_id | null`
    - `needs_review: [reason]`
2. Add deterministic resolver helpers before any LLM adapter.
  - Match common fields: name, email, phone, location, LinkedIn, GitHub, portfolio.
  - Match resume upload field.
  - Refuse sensitive/legal/unknown fields.
3. Keep any future LLM adapter behind a narrow interface.
  - The adapter may accept only normalized JSON and return strict JSON.
  - It must not see raw browser handles.
4. Add tests.
  - Known fields resolve.
  - Unknown required fields produce `needs_review`.
  - Sensitive fields produce `needs_review`.
  - Final submit identification is preserved but not clicked.

### Acceptance

- Resolver output validates against schema.
- Unknown/sensitive questions are never guessed.
- No browser side effects exist in resolver code.

## Phase 4 — Guarded executor

Goal: safely perform fills and non-final navigation only.

### Tasks

1. Implement executor module.
  - File: `scraper/src/apply_pipeline/executor.py`
  - Input: Playwright page + `PageSnapshot` + `RunDecision` actions.
  - Supported actions:
    - fill text/textarea/typeahead
    - select options
    - check/uncheck checkbox/radio
    - upload configured resume file only
    - click safe non-final navigation
2. Enforce guards.
  - Reject final submit candidates.
  - Reject unknown field IDs.
  - Reject unsupported controls.
  - Reject file uploads except configured resume.
  - Stop on disabled navigation.
3. Add tests with fake page/action adapter.
  - Verify action sequence.
  - Verify final submit is never clicked.
  - Verify unknown action fails closed.

### Acceptance

- Executor can fill a synthetic form and click Continue.
- Executor refuses final submit.
- All actions are logged and serializable.

## Phase 5 — Run loop CLI

Goal: run the full dry-run application preparation loop for selected jobs.

### Tasks

1. Add CLI command.
  - Existing entrypoint: `job-sync` or new script name if renamed later.
  - Candidate command:
    ```bash
    .venv/bin/job-sync apply-dry-run --limit 3 --max-pages 6
    ```
2. Loop behavior.
  - Select next backlog jobs.
  - Start application run.
  - Open `canonical_url`/apply URL.
  - For each page:
    - observe
    - persist snapshot
    - resolve
    - plan guarded actions
    - if `continue`: execute actions and observe next page
    - if terminal: finish run
  - Stop at max pages with `needs_review`.
3. Results.
  - Print JSON summary:
    - jobs attempted
    - dry_run_ready
    - needs_review
    - blocked
    - failed
    - run IDs

### Acceptance

- A dry run can process one job without clicking final submit.
- `application_runs` and `application_pages` are populated.
- A terminal run prevents immediate duplicate processing.

## Phase 6 — Failure sampling and R&amp;D loop

Goal: make failures actionable without brittle per-board automation.

### Tasks

1. Add sampler command.
  ```bash
   .venv/bin/job-sync apply-sample-failures --status blocked --limit 10
  ```
2. Group by reason and board/apply host.
3. Produce review output:
  - run ID
  - job title/company
  - URL
  - blocker/reason
  - snapshot path or DB ID
4. Add hand-annotation support later:
  - policy notes per host
  - field label aliases
  - unsupported control notes

### Acceptance

- Operator can see representative blocked/needs-review pages.
- Fixes can be made as observer/resolver/executor policies, not one-off scripts.

## Phase 7 — External feed compatibility

Goal: optionally consume the friend’s job-source API without coupling the application pipeline to it.

### Tasks

1. Build read client for `GET /v1/jobs` only after base URL/key are configured.
2. Normalize `CareerSourceJob` into local `jobs` table or a staging table.
3. Map fields:
  - `external_id` → source external ID
  - `title` → title
  - `company` → company_name
  - `listing_url/apply_url` → canonical/apply URL
  - `date_posted` → posted_at
  - `raw_json` → raw_json
4. Do not implement push/ingest unless he provides a write API contract.

### Acceptance

- Can import/read his feed without changing observer/resolver/executor.
- Source-specific code stays at ingestion boundary.

## Recommended build order

1. Finish run storage helpers.
2. Build static observer fixtures.
3. Build deterministic resolver helper.
4. Build fake executor tests.
5. Wire one-job dry-run CLI.
6. Add live Playwright smoke.
7. Add R&amp;D failure sampler.

## Current implementation status

Completed:

- Phase 1 storage helpers exist in `scraper/src/apply_pipeline/runs.py`.
- Phase 1 schema exists in `scraper/src/db/schema.sql`.
- Backlog selection exists in `scraper/src/apply_pipeline/backlog.py`.
- Phase 2 static HTML/live-frame observer exists in `scraper/src/apply_pipeline/observer.py`.
- Phase 3 deterministic resolver exists in `scraper/src/apply_pipeline/resolver.py`.
- Phase 4 guarded fake-page and Playwright executor adapter exists in `scraper/src/apply_pipeline/executor.py` and `runner.py`.
- Phase 5 CLI runner exists:
  - `.venv/bin/job-sync apply-dry-run`
  - `.venv/bin/job-sync apply-dry-run --live`
  - `.venv/bin/job-sync apply-sample-failures`
- Phase 7 read-client/import command exists in `scraper/src/apply_pipeline/job_source.py` and `sync.jobs`.

Remaining hardening before broad real-world use:

- Live Playwright adapter smoke against a disposable local/static form page.
- Host-specific failure policy improvements driven by `apply-sample-failures` reason groups and apply hosts.
- Optional external feed pagination/batching after a real `JOB_SOURCE_BASE_URL` and `JOB_SOURCE_API_KEY` are configured.

Current CLI status:

- `apply-dry-run` without `--live` is a safe scaffold. It does not open a browser and records `failed`, so jobs are not permanently skipped.
- `apply-dry-run --live` opens Playwright, observes/resolves/executes guarded safe actions, persists every page, and stops before final submit; add `--manual-handoff` to keep the browser open on terminal statuses for manual review/editing.
- `import-job-source` reads an optional external `GET /v1/jobs` feed and imports normalized jobs at the ingestion boundary only.

## Verification gate after each phase

From `scraper/`:

```bash
.venv/bin/python -m pytest
```

For TheirStack query changes only:

```bash
.venv/bin/job-sync dry-run --call-api --posted-at-max-age-days 2
```

For paid TheirStack fetches, require explicit approval and set the limit from preview `total_results`.