# Agent Operating Notes

## Project Design Reference

Use `SCRAPER_UI.md` for product workflow, layout, information architecture, and UX behavior.

Apply that direction specifically to the job scraper frontend, including the local resume-prompt web UI in:

- `scraper/src/job_scraper/web.py`

## Architecture Bias: Microservices, Functional Core, Container First

Prefer a functional, service-oriented design over object-oriented architecture.

Default architectural direction:

- Split the system into as many small services as are justified by independently testable responsibilities.
- Prefer explicit service boundaries over broad shared modules or hidden coupling.
- Prefer functions, typed data structures, schemas, and plain modules over custom class hierarchies.
- Avoid OOP unless a framework, library, or external API requires it.
- If a class is unavoidable, keep it as a thin adapter around functional code.
- Keep business logic in pure or mostly pure functions that can be tested without the web server, scraper runtime, browser session, or database.
- Keep side effects at service boundaries: HTTP handlers, CLI entrypoints, filesystem access, network calls, database calls, queue consumers, and container startup.

For this project, default service boundaries should be considered around:

- job ingestion / scraping
- job description normalization and parsing
- resume parsing
- resume-to-job matching and scoring
- evidence extraction
- improvement recommendation generation
- report generation / UI serving
- persistence / cache / queue infrastructure

Do not force everything into one process for convenience if a service boundary would make testing, deployment, scaling, or failure isolation cleaner.

Do not create microservices for their own sake. Each service must have:

- a clear responsibility
- a documented input/output contract
- containerized execution
- focused tests
- a health check or equivalent smoke check
- an explicit owner in the implementation plan

## Containerization Contract

Everything must run in containers by default.

For every service or CLI entrypoint added or changed, the coordinator must ensure:

- it has a Dockerfile or is included in a shared Dockerfile target
- it is wired into `docker-compose.yml` or the project’s equivalent compose stack
- required environment variables are documented with safe defaults or examples
- dependencies are installed inside the container, not assumed from the host machine
- tests can run inside the container
- health checks or smoke checks exist for long-running services
- containers can be rebuilt from a clean checkout

Host-machine execution is allowed only as a developer convenience. It must not be the only verified path.

## Pre-Push Verification Gate

Do not push, merge, or mark work complete until the containerized path has been verified.

Minimum default gate:

```bash
docker compose build
docker compose up -d
# run the project’s focused test command inside the relevant service container
# examples:
# docker compose exec <service> pytest
# docker compose exec <service> uv run pytest
# docker compose exec <service> npm test
docker compose ps
docker compose down
```

If the repository uses a different container runner, use the project-native equivalent, but keep the same standard: build cleanly, start cleanly, test inside the container, then tear down cleanly.

Feature-specific verification must include at least one of:

- unit tests for pure functions
- contract tests between services
- integration tests through the containerized service boundary
- end-to-end smoke test through the UI or API
- regression test for the exact bug fixed

Never claim container verification unless the command was actually run.

## Review Policy: Metrics and Executable Evidence Over Subjective Review

Do not use model-based code review as a substitute for measurable verification.

External or advisor review is optional and should only be used when it produces concrete, actionable findings tied to one of:

- failing tests
- missing tests
- broken service contracts
- unclear ownership boundaries
- containerization gaps
- security-sensitive logic
- measurable complexity or maintainability risk
- diffs outside the assigned scope

Do not ask another model for a vague “code review” just to get a second opinion. A useful review must point to specific files, specific risks, and specific acceptance checks. Comments like “looks good,” “clean,” or “maintainable” are not evidence.

The parent/coordinator still must inspect diffs before merging worker output, but this inspection is a scope and verification check, not a replacement for tests.

## Autonomous Orchestration Policy

The coordinator is expected to actively orchestrate work, not merely describe how it could be done.

When the required tools are available (Orca, OMP, Git, shell), the coordinator should prefer performing orchestration actions over suggesting them.

For non-trivial tasks, the default workflow is:

1. Run `/plan`.
2. Determine whether parallel execution would reduce total completion time or improve isolation.
3. If beneficial, create worker terminals or worktrees without waiting for additional permission.
4. Assign explicit ownership to each worker.
5. Monitor worker progress.
6. Review all worker output.
7. Integrate, verify, and report results.

Assume permission to perform reversible development operations unless the user explicitly restricts them.

Examples of expected autonomous actions:

- creating feature worktrees
- creating worker terminals
- sending worker prompts
- waiting for workers
- collecting worker results
- running tests
- running linters
- inspecting git diffs
- merging completed worker output

Do **not** ask whether to perform these routine orchestration steps. Perform them when they support the user's stated objective.

Ask for confirmation only before:

- destructive operations (deleting branches, worktrees, files, databases)
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

Implementation workers may use either DeepSeek V4 Pro or GPT-5.5 at medium thinking.

DeepSeek worker:

```bash
orca terminal create --worktree active --title "<specific-subtask>" --command 'omp --model "openrouter/deepseek/deepseek-v4-pro" --thinking medium' --json

```

GPT-5.5 worker:

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
orca worktree create --name "<parent-feature>-<specific-subtask>" --json
orca terminal create --worktree "<parent-feature>-<specific-subtask>" --title "<specific-subtask>" --command 'omp --model "openrouter/deepseek/deepseek-v4-pro" --thinking medium' --json
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
Target: <exact files/symbols>.

Change:
<specific behavior to add/fix>.

Non-goals:
Do not edit <files/subsystems>.
Do not do broad cleanup.
Do not create a new worktree.

Ownership:
You own only <files>.
Do not touch anything else.

Acceptance:
<focused checks or observable behavior>.

Verification:
Run <specific command or focused test>.

Report:
1. Files changed
2. Verification run
3. Result
4. Unresolved risks

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

