# Agent Operating Notes

## Product direction

The active app is a local job scraping/backlog ingestion assistant with a guarded Greenhouse+Lever draft workflow.

Set-in-stone active features:

- scraper/backlog ingestion into SQLite;
- filtering and quality gates for source jobs;
- profile-shaped search/filter ideas;
- TheirStack preview/paid-fetch concepts and helpers, including the `job-scrape` entrypoint;
- optional normalized JSON/API feed import for fixtures and backfills.

The rebuilt applier is active for direct Greenhouse and Lever application URLs. It claims queued backlog jobs, combines the explicit applicant profile, configured resume, and optional job description, resolves supported safe fields, persists complete per-run evidence, and leaves an independently owned headed window ready for human review and manual submission. It never performs final submission.

Do not build on archived observer/resolver/executor/runner code. Extend the active `src/jobs_assistant/` contracts through OMP `workflowz`, focused tests, deterministic safety gates, and ATS adapters. Greenhouse was the first adapter; Lever is the active second adapter. Both remain behind the same route, network, safe-action, private-artifact, and no-submit gates.

## Current active workflow

```text
TheirStack / feed / scraper output
  ↓
Normalize source job
  ↓
Filtering and quality gates
  ↓
SQLite jobs backlog
  ↓
Sync/run metadata
```

Guarded Greenhouse+Lever draft workflow:

```text
SQLite queued job
  ↓
Open exact public ATS application URL
  ↓
Observe DOM/frames
  ↓
Resolve safe answers from profile/resume/job context
  ↓
Execute guarded non-final actions
  ↓
Persist review evidence
  ↓
Stop before final submit
```

The second workflow is active for direct Greenhouse and Lever routes. Lever is constrained to the exact `jobs.lever.co`/`jobs.eu.lever.co` company plus canonical lowercase UUID route (optional `/apply`), with no query, fragment, credentials, percent-encoding, or path escape. Unsupported ATS routes and unsafe or ambiguous cases remain manual/blocked.

## Safety policy

Hard rules:

- Every browser mutation or action (fill, click, select, upload, keypress, navigation, or script-triggered event) MUST pass a deterministic allow/deny gate against the current observed page/frame snapshot before execution. Hidden DOM assumptions, stale selectors, and LLM judgment at execution time are prohibited.
- LLM output MUST be schema-validated and safety-validated, then pass the same deterministic gate before it can influence any browser action. Raw model output MUST never drive a fill, click, navigation, upload, or other mutation.
- Never click, keypress, navigate, or script-trigger any final-submit, application-submit, mass-apply, bulk-submit, or equivalent terminal application action. Submit-like controls are stop points, not automation targets.
- Upload only the configured resume file unless the user explicitly changes policy.
- The applier loop MUST always stop after persisting review evidence and before any final submission. A completed run may leave a draft/application ready for human review and manual submission only; it must not submit, queue submission, schedule submission, or expose a helper that can submit multiple applications.
- Sensitive, legal, protected-class, financial, authentication, CAPTCHA, and assessment questions MUST never be inferred or automated; they are manual stop points.
- Inference is permitted only for safe, non-sensitive, noncanonical fields when the relevant source of truth is explicit and deterministic; it MUST NOT replace source-of-truth data or resolve ambiguity.
- Validation artifacts (`observation.json`, `plan.json`, `actions.json`, `filled_state.json`, `job_description.txt` when available, screenshots, fixtures, logs, user annotations) MUST be persisted per run so Greenhouse and Lever handling and preferences improve over time; never discard evidence that could inform future safety decisions.
- Treat destructive database cleanup as requiring a clear user instruction.

These rules govern the active implementation; historical implementations are preserved in git history only.


## Applicant/profile reference


- Resume file: `resume/Main_Resume.pdf`
- LinkedIn: `https://www.linkedin.com/in/ianrapko`
- Personal site: `https://immemorized.com`
- Profile JSON examples may live under `scraper/data/` or `data/`.

## Protected files and data

Do not touch casually:

- Runtime SQLite data under `data/` or `scraper/data/` unless explicitly instructed.
- Applicant profile/resume data except through documented profile workflows.

## Active code layout

Active source:

- `src/jobs_assistant/contracts.py`: ingestion/domain dataclasses.
- `src/jobs_assistant/db.py`: jobs/sync SQLite schema and helpers.
- `src/jobs_assistant/backlog.py`: queue upsert, dedupe, backlog counts.
- `src/jobs_assistant/theirstack.py`: TheirStack payload/client/sync helpers.
- `src/jobs_assistant/job_source.py`: JSON/API feed normalization and import.
- `src/jobs_assistant/application.py`: guarded browser-adapter/LLM-assisted autofill primitives and run persistence.
- `src/jobs_assistant/cli.py`: minimal CLI entrypoints, including `jobs-assistant` and `job-scrape`.
- `tests/`: focused scraper/ingestion/backlog/TheirStack/CLI/autofill tests.
- `scripts/smoke.sh`: repository smoke check script.

Resume command ownership:

- Top-level `resume-generate` uses
  `src/jobs_assistant/resume_generator.py` and
  `resume/generator/{profile.json,Resume.tex,SKILL.md}`; this is the canonical
  standalone resume generator.
- `jobs-assistant resume-generate` remains the preserved main-application
  service with an incompatible API and artifact contract. Do not overlay the
  two `resume.py` implementations; integrate through a deliberate adapter
  later.

- Its default artifacts live under `data/generated-resumes-generator/`, kept
  separate from the main application's `data/generated-resumes/` contract.

Historical applier source was removed from the working tree; use git history for reference.

## Development rules

- Keep business logic as pure functions over typed data where possible.
- Keep side effects at boundaries: CLI, HTTP, SQLite, filesystem.
- Add tests for every new filtering, ingestion, dedupe, profile, or TheirStack branch.
- Prefer boring schemas and explicit JSON/SQLite contracts.
- Do not add a second convention beside an existing one.
- Use the named OMP model roles as the source of truth: `DEFAULT` is the primary Terra xhigh orchestrator and `SLOW` is the alternate Sol minimal orchestrator mode. Sol xhigh is reserved exclusively for independent `ADVISOR` review. `SMOL` is Luna medium and `TASK` is Luna xhigh; global `task-high` supplies a Luna-high implementation lane. Terra supplies the middle `PLAN`, `VISION`, and `DESIGNER` specialist lanes; `COMMIT` and `TINY` are Luna medium.

## Model aliases

| Model | Role alias | Reasoning |
|---|---|---|
| `openai-codex/gpt-5.6-terra` | `DEFAULT` | xhigh |
| `openai-codex/gpt-5.6-sol` | `ADVISOR` | xhigh |
| `openai-codex/gpt-5.6-sol` | `SLOW` | minimal |
| `openai-codex/gpt-5.6-terra` | `VISION` | medium |
| `openai-codex/gpt-5.6-terra` | `PLAN` | medium |
| `openai-codex/gpt-5.6-terra` | `DESIGNER` | high |
| `openai-codex/gpt-5.6-luna` | `SMOL` | medium |
| `openai-codex/gpt-5.6-luna` | `TASK` | xhigh |
| `openai-codex/gpt-5.6-luna` | `task-high` | high |
| `openai-codex/gpt-5.6-luna` | `COMMIT` | medium |
| `openai-codex/gpt-5.6-luna` | `TINY` | medium |

## Metrics-first goals and minimal tooling

Every substantial task should name:

- goal;
- metric;
- target threshold or invariant;
- measurement source;
- stop condition.

When Prometheus/Grafana are not wired for the touched path, use focused tests, CLI output, fixture counts, SQLite queries, or markdown checklists as temporary metrics.

## OMP workflowz orchestration

Use OMP workflowz for non-trivial implementation work. The coordinator decomposes the approved plan into explicit subagent tasks and dispatches them through OMP's in-session orchestration/task runtime; do not introduce a second repository-specific terminal or worktree orchestration layer.

Every implementation, test, review, or documentation assignment must be tracked in the OMP workflow with explicit ownership and dependencies. Dispatch independent file slices in parallel and serialize only established API/schema dependencies.

Orchestration subtasks must include:

- exact target files/symbols;
- non-goals and forbidden files;
- acceptance criteria;
- focused verification command;
- owner/subagent role;
- report contract.

Role/model mapping for this project:

- Coordinator / integration owner: `DEFAULT` (`openai-codex/gpt-5.6-terra`, xhigh) or alternate `SLOW` (`openai-codex/gpt-5.6-sol`, minimal), or the current orchestrator session.
- Implementation and tester workers: `TASK` (`openai-codex/gpt-5.6-luna`, xhigh), `SMOL` (`openai-codex/gpt-5.6-luna`, medium), or global `task-high` (`openai-codex/gpt-5.6-luna`, high) for bounded high-reasoning work.
- Higher-thinking delegated planning or specialist work: Terra-backed `PLAN`, `VISION`, or `DESIGNER`.
- Safety/review worker: `ADVISOR` (`openai-codex/gpt-5.6-sol`, xhigh); Sol xhigh is advisor-only.
- Low-risk docs/status summarizer: `COMMIT` (`openai-codex/gpt-5.6-luna`, medium).

Worker completion contract:

- Each worker returns one final report for its assigned task.
- The report lists modified files, focused tests run, results, blockers, and remaining risks.
- Workers raise blocking questions through the OMP task channel instead of opening local interactive prompts.
- Workers do not send group lifecycle messages or redefine shared contracts without coordinator approval.

Delegate by need:

- Tester: high-signal tests and edge cases.
- reviewer: safety, quality, security, and scope review.
- task worker: implementation on explicit files.
- designer: UI/design work only.
- librarian: external API/library research.

The parent worktree is the integration authority. Compare worker reports/diffs, integrate only useful patches, reject unrelated edits, and verify in the parent.

Future applier revival must be decomposed through orchestration into at least:

1. source/backlog contract;
2. page observer contract;
3. resolver JSON contract;
4. executor safety contract;
5. persistence/output contract;
6. safety review and adversarial tests.


The orchestrated applier loop runs over durable artifacts, not a one-shot browser script:

```text
discover -> observe -> resolve -> execute guarded non-final actions -> persist evidence -> human review/manual submission
```

Practice rules:

- Use the Puppeteer browser-adapter boundary for page observation and guarded field actions; workers must not invent ad hoc browser-control paths outside observer/executor contracts. The current local install commands for the Puppeteer-backed path are `npm install` and `npm run install-browser`.
- Greenhouse was the first adapter; Lever is the active second adapter. Keep ATS-specific selectors, field normalization, and quirks behind the ATS adapter protocol so future ATS support can be added without changing resolver or safety contracts. Both active adapters use the same route, network, safe-action, private-artifact, and no-submit gates.
- Persist observed page fixtures/snapshots, resolver JSON, planned actions, rejected actions, filled-state evidence, `job_description.txt` when available, screenshots when available, and user preference annotations. These artifacts feed future adapter improvements and fixture tests; they are not permission to relax safety gates.

No worker may add final-submit behavior until a separate submit policy exists and is tested.
## Verification

From a clean checkout with the committed `uv.lock`:

```bash
uv lock --check
uv run --frozen --extra dev python -m pytest
uv run --frozen jobs-assistant --help
```

Container verification:

```bash
docker compose build
docker compose up -d
docker compose ps
docker compose down
```

Never claim verification unless the command was actually run.

## Containerization contract

Everything should be able to run in containers by default. Host execution is a developer convenience, not the only verified path.

For every CLI entrypoint or service boundary added or changed, ensure:

- dependencies are declared in project files;
- required environment variables have examples or safe defaults;
- tests run from a clean checkout;
- Docker/compose wiring is updated when runtime behavior changes.

## Review policy

Prefer executable evidence over subjective review.

Useful findings cite:

- failing tests;
- missing tests;
- broken contracts;
- unclear ownership boundaries;
- containerization gaps;
- safety-sensitive logic;
- diffs outside scope.

## Verification contract for completed tasks

Final reports must include:

1. Summary of behavior changed.
2. Files changed.
3. Tests/checks run.
4. Container commands actually run or skipped reason.
5. Known risks or skipped checks.
6. Suggested next patch, if any.
