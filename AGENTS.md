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
  clicks non-final Next/Continue/Apply navigation
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

- Use minimal Playwright R&D, not brittle per-board automations.
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
- Use DeepSeek V4 Pro through Ollama Cloud for implementation workers by default: `omp --model "ollama-cloud/deepseek-v4-pro" --thinking medium`.
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

For non-trivial work:

1. Understand the goal and affected files.
2. Decide whether workers reduce risk or latency.
3. Assign explicit ownership if workers are used.
4. Integrate results.
5. Run the focused verification gate.

Use workers when:

- independent file ownership groups exist
- one worker can verify while another implements
- research can proceed independently
- risky refactors need independent checking

Avoid workers when:

- the change is a small one-file edit
- file ownership would overlap heavily
- coordination costs exceed implementation costs

Ask confirmation before:

- deleting branches, worktrees, files, or databases
- irreversible deployments
- publishing externally
- operations outside the project workspace
- actions requiring credentials or billing

## Worker Decision Heuristic

Do not optimize for the number of agents.

Spawn workers only when at least one of the following is true:

- Two or more independent file ownership groups exist.
- Estimated implementation time exceeds 15–20 minutes.
- One worker can write or run verification while another implements.
- Research can proceed independently of implementation.
- A risky refactor benefits from an independent verification pass.

Prefer 2–4 workers.

Avoid more than 6 workers unless the task naturally decomposes into many independent slices.

## Default Coordinator Model

Every new feature worktree should have exactly one advisor-enabled parent agent.

The parent agent is the coordinator. It owns:

- planning
- task splitting
- file ownership
- worker assignment
- worker review
- conflict resolution
- final verification

Launch the parent agent with:

```bash
omp --advisor --model "openai-codex/gpt-5.5" --thinking xhigh

```

The advisor is a guardrail for the parent. It helps keep plans, assignments, and final merges in scope.

Do not assume the advisor controls child terminals. The parent must inspect worker output and enforce boundaries.



## Planning Workflow

For every new feature, bug fix, refactor, or research task, begin with `/plan` unless the request is a clearly trivial one-file change.

The parent/coordinator agent owns planning.

The planning phase should:

1. Understand the user's goal.
2. Identify affected files, modules, and repositories.
3. Determine whether worker agents are beneficial.
4. Define file ownership before any implementation begins.
5. Identify risks, dependencies, and verification steps.
6. Decide whether the work should be completed by:
  - the coordinator alone, or
  - one or more worker agents.

After `/plan` completes:

- If the work is small, the coordinator may implement it directly.
- If the work naturally decomposes into independent slices, spawn worker agents only after ownership has been established.
- Do not spawn workers before completing the planning phase.

Do not repeatedly re-plan unless the task scope changes substantially.

The planning phase is expected for nearly all non-trivial work and should be treated as the default starting point.

## Worker Allocation

The coordinator may use either same-worktree worker terminals or sub-worktree workers.

Use same-worktree workers when:

- the task is small
- file ownership is completely disjoint
- workers only need read-only review
- edits are unlikely to conflict
- speed matters more than isolation

Use sub-worktree workers when:

- two or more implementation paths should be explored in parallel
- the task is risky or broad
- workers may need to run tests or modify overlapping project state
- independent patches should be reviewed before integration
- competing approaches should be compared
- isolation is more valuable than speed

Do not create sub-worktrees casually. Create them when they improve isolation, parallelism, or review quality.

For non-trivial tasks, the coordinator should explicitly decide during `/plan`:

- coordinator-only
- same-worktree workers
- sub-worktree workers
- mixed approach

Good worker splits:

- one file or tightly related file pair
- one CLI command family
- one storage/schema area
- one focused test file
- one read-only verification or contract-audit pass
- one isolated experimental implementation
- one competing patch approach

Bad worker splits:

- “clean up everything”
- “make it better”
- overlapping edits without sub-worktree isolation
- broad architectural decisions delegated to workers
- tasks that require another worker’s unfinished output

## Worker Launch Commands

Implementation workers default to DeepSeek V4 Pro through Ollama Cloud at medium thinking.

DeepSeek V4 Pro worker:

```bash
orca terminal create --worktree active --title "<specific-subtask>" --command 'omp --model "ollama-cloud/deepseek-v4-pro" --thinking medium' --json
```

GPT-5.5 fallback worker:

```bash
orca terminal create --worktree active --title "<specific-subtask>" --command 'omp --model "openai-codex/gpt-5.5" --thinking medium' --json
```

Then wait for the worker and send a narrow assignment:

```bash
orca terminal wait --terminal <handle> --for tui-idle --timeout-ms 60000 --json
orca terminal send --terminal <handle> --text '<narrow task prompt>' --enter --json

```

## Sub-Worktree Worker Launch Commands

Use sub-worktree workers when isolation is beneficial.

Default pattern:

```bash
orca worktree create --name "<parent-feature>-<specific-subtask>" --parent-worktree active --json
orca terminal create --worktree "<parent-feature>-<specific-subtask>" --title "<specific-subtask>" --command 'omp --model "ollama-cloud/deepseek-v4-pro" --thinking medium' --json
orca terminal wait --terminal <handle> --for tui-idle --timeout-ms 60000 --json
orca terminal send --terminal <handle> --text '<narrow task prompt>' --enter --json

```

The coordinator must treat sub-worktree output as a patch proposal, not automatically accepted work.

For every sub-worktree worker, the coordinator must:

1. inspect the worker diff
2. compare it against the assignment
3. reject unrelated edits
4. integrate the useful patch into the parent feature worktree
5. rerun focused verification in the parent worktree
6. only then continue to final verification

Workers in sub-worktrees must not merge their own work into the parent.

## Coordinator Rules

The parent/coordinator must:

1. Keep one parent per feature worktree.
2. Write down file ownership before spawning workers.
3. Spawn one worker per disjoint file, subsystem, or verification slice.
4. Give each worker exact target files, symbols, non-goals, and acceptance checks.
5. Prefer verification workers before risky merges or broad refactors.
6. Review worker diffs before accepting them.
7. Stop using worker output if the worker drifts out of scope.
8. Resolve conflicts and duplicate implementations itself.
9. Run final verification after worker output is integrated.
10. Confirm final behavior from the user’s perspective.

Workers must:

1. Stay in the active worktree.
2. Not create new worktrees.
3. Only edit assigned files.
4. Avoid broad cleanup.
5. Avoid project-wide gates unless explicitly assigned.
6. Ask the parent for blocking decisions instead of guessing.
7. Report changed files, verification run, and unresolved risks.

## Worker Prompt Template

```text
Context:
AGENTS.md is mandatory project policy and is already available in this workspace. Follow it without asking the user to restate it. Use OMP_ORCA_WORKFLOW.md for OMP + Orca execution details.

Target: <exact files/symbols>.

Change:
<specific behavior to add/fix>.

Non-goals:
Do not edit <files/subsystems>.
Do not do broad cleanup.
Do not create a new worktree unless explicitly assigned one.

Ownership:
You own only <files>.
Do not touch anything else.

Development rule:
Add or update the focused failing test first, then implement the smallest passing patch.

Acceptance:
<focused checks or observable behavior>.

Verification:
Run <specific command or focused test>. Do not run project-wide gates unless assigned.

Report:
1. Files changed
2. Test-first evidence
3. Verification run
4. Result
5. Unresolved risks

```



## Default New Worktree Workflow

For every new feature or risky fix, create one feature worktree first. Do not create nested worktrees for normal subtasks.

Default flow:

```bash
orca worktree create --name "<short-feature-name>" --json

orca terminal create --worktree "<short-feature-name>" --title "coordinator" --command 'omp --advisor --model "openai-codex/gpt-5.5" --thinking xhigh' --json

orca terminal wait --terminal <handle> --for tui-idle --timeout-ms 60000 --json
```

## Parent Handoff Checklist

Before merging worker output, the parent must confirm:

- worker stayed inside assigned ownership
- no unrelated files changed
- focused checks match the assignment
- tests or verification were actually run
- duplicate/conflicting implementations were reconciled
- final behavior works from the user’s perspective
- final diff is reviewed by the parent
- remaining risks are documented

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

