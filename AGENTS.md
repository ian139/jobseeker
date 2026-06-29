# Agent Operating Notes

## Product direction

This repo is now centered on a local job-application assistant, not a mass auto-apply bot.

The target workflow:

```text
SQLite job backlog
  ↓
Playwright opens apply URL
  ↓
Deterministic observer scans DOM/frames
  ↓
Normalized DOM snapshot
  fields: id, kind, label, required, options, value, visible, frame, selector
  buttons: id, text, type, disabled, finalSubmitCandidate, visible, frame, selector
  errors/blockers
  no board-specific Playwright templates
  ↓
LLM-first resolver
  input: normalized snapshot + profile + resume + facts + job description + policies
  output: strict JSON answers + nextButton + submitButton + needsReview
  may choose safe initial Apply/Start navigation from observed buttons
  must refuse unknown/sensitive/legal answers instead of guessing
  ↓
Guarded generic executor
  reusable Playwright fill/select/check/upload/click only
  uploads resume only
  clicks policy-approved non-final Apply/Next/Continue navigation
  never clicks final submit
  ↓
Loop observe → resolve → fill → advance
  ↓
Run result
```

Run results:

- `dry_run_ready`: ready at final submit; not submitted.
- `needs_review`: unknown, sensitive, or manual field.
- `blocked`: sign-in, CAPTCHA, no form, job gone, weird upload, unsupported workflow.
- `failed`: browser, LLM, parser, executor, or navigation failure.

## Architectural split

Keep these responsibilities separate. Do not blur them for convenience.

### Observer: what exists on the page

- Input: browser page/frame state.
- Output: normalized page snapshot.
- No profile facts, no resume facts, no answer decisions.
- Deterministic and testable from static HTML fixtures where possible.

### Resolver: what answers should be used

- Input: normalized snapshot + profile + resume + facts + job description + policies.
- Output: strict JSON answers, next button, submit button, and uncertainty flags.
- Must refuse unknown/sensitive fields instead of guessing.
- Never performs browser actions.
- The in-program DeepSeek/Ollama resolver loads `skills/SKILL.md` as operational guidance for live proof/navigation while still returning strict JSON for guarded execution.

### Executor: allowed actions only

- Input: normalized snapshot + resolver JSON.
- Performs fills, safe uploads, and non-final navigation.
- Never clicks final submit.
- Must stop on final submit, CAPTCHA, sign-in, unsupported controls, or sensitive fields.

## Applicant reference

Use these as the user's default applicant facts for local dry-run preparation and resolver context. Do not infer sensitive or legal answers from them.

- Resume file: `Main_Resume.pdf`
- LinkedIn: `https://www.linkedin.com/in/ianrapko`
- Personal site: `https://immemorized.com`

## Safety policy

Hard rules:

- Never mass-submit applications.
- Never click final submit.
- Never answer sensitive fields by inference.
- Never bypass sign-in, CAPTCHA, assessments, or identity checks.
- Upload only the configured resume file unless the user explicitly changes policy.
- Treat any destructive database cleanup as requiring a clear user instruction.

Soft preference:

- Use minimal Playwright R&amp;D, not brittle per-board automations.
- Prefer deterministic observation and guarded generic execution.
- When a board fails, collect samples and reasons, then add targeted policies/fixtures.

## Code layout

Active code:

- `scraper/src/theirstack/`: TheirStack query/client code.
- `scraper/src/sync/`: SQLite backlog sync and dedupe.
- `scraper/src/db/schema.sql`: backlog and application-run schema.
- `scraper/src/apply_pipeline/`: application assistant contracts and pure helpers.
- `scraper/tests/`: unit tests for contracts, sync, and query behavior.

Archived code belongs under `old/`. Do not move active entrypoints or tests there.

## Development rules

- Keep business logic as pure functions over typed data where possible.
- Keep side effects at boundaries: CLI, browser, filesystem, HTTP, SQLite.
- Add tests for every new branch in observer/resolver/executor policy.
- Prefer boring schemas and explicit JSON contracts.
- Do not add a second convention beside an existing one.
- Use DeepSeek through Ollama Cloud as the provider for most tasking agents unless a task specifically needs another model family.

## Verification

For normal Python changes in `scraper/`:

```bash
.venv/bin/python -m pytest
```

For TheirStack preview changes, also run a safe preview:

```bash
.venv/bin/job-sync dry-run --call-api --posted-at-max-age-days 2
```

For paid fetches, only run after explicit user approval because returned jobs can spend credits:

```bash
ENABLE_PAID_FETCH=true JOB_SYNC_DB_PATH=data/job_sync_test.sqlite3 \
.venv/bin/job-sync sync-once --limit <preview_total> --max-pages 1 --posted-at-max-age-days 2
```

## OMP + Orca Workflow

Use `OMP_ORCA_WORKFLOW.md` as the reusable operating workflow for OMP coordinators, Orca workers, and Orca dev sessions in this repository. It exists so users do not need to restate "read AGENTS.md" in every prompt.

Mandatory workflow invariants:

- Launch OMP from the repository root so this `AGENTS.md` file is auto-loaded.
- Treat this file as always-on policy for every coordinator, worker, and review prompt.
- Use test-first development: add or update the focused failing test before implementation.
- Use DeepSeek V4 Pro through Ollama Cloud for implementation workers by default: `omp --model "ollama-cloud/deepseek-v4-pro" --thinking high`.
- Use `orca-dev` instead of `orca` only when operating an Orca development build; keep the same worktree, terminal, and verification workflow.

## Architecture Bias: Microservices, Functional Core, Container First

Prefer a functional, service-oriented design over object-oriented architecture.

- Split responsibilities only when the boundary is independently testable and useful.
- Prefer functions, typed data structures, schemas, and plain modules over class hierarchies.
- Keep business logic pure or mostly pure.
- Keep side effects at service boundaries: HTTP, CLI, browser, filesystem, network, database, queues.
- If a class is unavoidable, keep it as a thin adapter around functional code.

Candidate service boundaries:

- job ingestion / TheirStack sync
- job description normalization
- resume/profile/facts loading
- page observation
- answer resolution
- guarded execution
- failure sampling / review queue
- report/UI serving
- persistence/cache/queue infrastructure

Each real service boundary needs:

- documented input/output contract
- focused tests
- container or CLI execution path
- smoke check or health check for long-running processes

## Containerization contract

Everything should be able to run in containers by default. Host execution is a developer convenience, not the only verified path.

For every service or CLI entrypoint added or changed, ensure:

- dependencies are declared in project files
- required environment variables have examples or safe defaults
- tests can run from a clean checkout
- long-running services have a health check or smoke check
- Docker/compose wiring is updated when a service boundary needs it

Default container verification before merge/push when containers are in scope:

```bash
docker compose build
docker compose up -d
docker compose ps
docker compose down
```

Never claim container verification unless those commands were actually run.

## Review policy

Prefer executable evidence over subjective review.

Useful review findings cite:

- failing tests
- missing tests
- broken contracts
- unclear ownership boundaries
- containerization gaps
- security-sensitive logic
- measurable complexity
- diffs outside scope

Do not ask for vague “looks good” reviews. The coordinator owns scope review and verification.

## Orchestration policy

Use `OMP_ORCA_WORKFLOW.md` as the canonical workflow for planning, worker allocation, Orca worktrees, launch commands, task dispatch, handoff review, and parent/coordinator checklists. Keep this file focused on product, safety, architecture, containerization, and review policy.

## Verification Contract

Every completed task must include:

- Commands actually run
- Container build/start commands actually run
- Tests actually run inside containers when applicable
- Files modified
- Files intentionally not modified
- Services added, removed, or changed
- Service contracts added or changed
- Known risks
- Remaining TODOs

Never claim verification unless the command was executed.

If containerized verification cannot be run, explain why and do not present the work as ready to push.

## Final Response Requirements

At the end of a feature task, the parent must report:

1. Summary of behavior changed
2. Files changed
3. Tests/checks run
4. Known risks or skipped checks
5. Suggested next patch, if any

