# OMP workflowz development workflow

Use this workflow for every OMP coordinator and workflowz subagent in this repository. `AGENTS.md` is the source of truth and is auto-loaded for agents launched from this workspace; do not ask the user to restate it. “OMP workflowz” means OMP's in-session task/subagent orchestration runtime, not a Python runtime package or a second browser-control path.

## Active product boundary

The active workflow includes backlog ingestion and the rebuilt guarded Greenhouse+Lever draft loop in `src/jobs_assistant/`. Archived applier implementations are reference-only. Extend the active typed contracts and Puppeteer browser-adapter boundary; do not revive archived runners or add another browser-control path. Greenhouse was first and remains supported; Lever is the active second adapter, constrained to exact direct `jobs.lever.co`/`jobs.eu.lever.co` company/canonical-lowercase-UUID routes (optional `/apply`, no query or fragment). Automation stops after durable review evidence and before every final submission; both adapters use the same route, network, safe-action, artifact, and no-submit gates.

## Always-on rules

- Start from the repository root so OMP sees `AGENTS.md` automatically.
- Treat `AGENTS.md` as mandatory context for every prompt, worker assignment, and review.
- Start every non-trivial feature, bug fix, refactor, or research task with an explicit approved plan.
- Track every implementation, test, review, and documentation assignment in OMP workflowz before dispatching it.
- Keep the parent worktree as integration authority. Assign disjoint file ownership to parallel workers; serialize only real API or schema dependencies.
- The parent compares behavior, diff size, tests, maintainability, and policy fit before accepting worker output.
- Use test-first development: write or update the focused failing test before implementation.
- Prefer functional core, explicit service boundaries, typed data, and side effects at service edges.
- Keep containers as the default runtime; host execution is developer convenience only.
- Run final verification through the containerized path before marking work ready to push.
- Never use model review as a substitute for tests, smoke checks, or container evidence.

- Every browser mutation or action MUST pass a deterministic allow/deny gate against the current observed page/frame snapshot before execution. LLM output MUST be schema- and safety-validated before it can influence an action; raw model output MUST never drive browser mutations.
- Sensitive, legal, protected-class, financial, authentication, CAPTCHA, and assessment questions MUST never be inferred or automated; they are manual stop points. Inference is limited to safe non-sensitive noncanonical fields with an explicit deterministic source of truth.

## Metrics-first task design

Before launching workers, convert the user's goal into measurable targets. Prefer existing Prometheus/Grafana dashboards, PromQL queries, service health checks, CLI counters, test assertions, and markdown task files over broad tool exploration.

Every non-trivial plan should define:

- The goal metric and why it represents success.
- The target threshold or invariant.
- The measurement source: Prometheus query, Grafana panel/dashboard, test command, CLI output, fixture assertion, log field, or markdown checklist.
- The baseline, if known or cheaply measurable.
- The stop condition: what evidence lets the parent accept or reject a child implementation.

Use skills/tools narrowly: first make the goal specific in markdown, then use tools to gather only the evidence needed to hit or verify the metric. Mandatory harness-required skills/tools still apply.

## Parent/coordinator startup

Start one OMP coordinator from the repository root. Use OMP workflowz/task orchestration to create role-appropriate subagents in the current parent worktree; do not create a parallel terminal-control convention.

The coordinator records the complete task contract before dispatch:

- exact target files or symbols;
- forbidden files and non-goals;
- owner role;
- metric, target invariant, and measurement source;
- stop condition;
- focused failing test and verification command;
- final report requirements.

The coordinator keeps the parent worktree as integration authority, dispatches independent ownership in parallel, and waits for each task's final report before accepting it.

## Planning checklist

During `/plan`, the parent coordinator must record:

1. User goal and acceptance criteria.
2. Goal metrics: Prometheus/Grafana query or panel when available; otherwise test, CLI, fixture, log, or markdown checklist metric.
3. Target threshold/invariant, baseline if known, and stop condition.
4. Affected files, services, commands, and container targets.
5. The test to write or update first.
6. The OMP `workflowz` subtasks, each with target files, non-goals, owner, acceptance metric, and verification command.
7. Which subtasks need child worktrees, and which need multiple competing child worktrees.
8. File ownership before any worker edits.
9. Risks, service contracts, environment variables, and verification commands.

## Worker model roles

Use the current role aliases from `AGENTS.md` as the source of truth:

- `PLAN` for coordinator planning and decomposition.
- `TASK` for implementation and test workers.
- `ADVISOR` for independent read-only safety and architecture review.
- `COMMIT` only for low-risk documentation or status synthesis after executable gates pass.

Do not hard-code stale provider model names in worker assignments. Role bindings and reasoning levels belong in `AGENTS.md`.

## Tracked workflowz subtasks

Every worker assignment must start as an OMP workflowz task. A valid task has:

- One clear objective.
- Exact files or symbols owned by the worker.
- Explicit non-goals and forbidden files.
- A goal metric, target threshold, measurement source, and stop condition.
- A focused failing test or observable behavior to prove first.
- A focused verification command the worker may run.
- One final report listing files changed, metric movement, test-first evidence, verification, blockers, and risks.

Do not launch an implementation worker from an informal terminal prompt when an OMP task can carry the same contract.

## Parallel tracked workers

Create the widest safe wave of OMP tasks with disjoint file ownership. Workers skip formatters and project-wide suites; the parent runs integration gates once. If workers share a dependency, encode it in the workflow and dispatch only when the prerequisite contract is established.

## Parent selection and integration

Worker output is a patch proposal, not automatic acceptance. The parent coordinator must:

1. Inspect every child diff.
2. Compare verification output and failing-test evidence.
3. Prefer the smallest implementation that satisfies the contract and preserves repository conventions.
4. Integrate only the selected patch or selected pieces into the parent worktree.
5. Reject unrelated edits, broad cleanup, stale scaffolding, and policy violations.
6. Rerun focused verification in the parent before final verification.

The active parent worktree is the integration authority. Workers may edit only their explicitly disjoint ownership; coordinate any overlap before editing.

## Worker prompt contract

Every worker prompt must include:

```text
Context:
AGENTS.md is mandatory project policy and is already available in this workspace. Follow it without asking the user to restate it.

Workflowz:
This prompt implements OMP `workflowz` subtask <id/title>. Do not expand scope beyond that subtask.

Metric:
<Prometheus/Grafana query or markdown/test/CLI/log metric>.
Target:
<threshold, invariant, or exact outcome>.
Stop condition:
<evidence that proves this subtask is done or should be rejected>.

Target:
<exact files/symbols>.

Change:
<specific behavior to add/fix>.

Non-goals:
Do not edit <files/subsystems>.
Do not do broad cleanup.
Do not create a new worktree unless explicitly assigned a sub-worktree.

Ownership:
You own only <files>.
Do not touch anything else.

Development rule:
Add or update the focused failing test first, then implement the smallest passing patch.

Acceptance:
<focused checks or observable behavior tied to the metric target>.

Verification:
Run <specific focused test, metric query, dashboard check, or command>. Do not run project-wide gates unless assigned.

Report:
1. Files changed
2. Metric baseline/result or explicit "not applicable"
3. Test-first evidence
4. Verification run
5. Result
6. Unresolved risks
```

## Verification gate
Before compose, existing host data MUST be owner-private: run `mkdir -p data`, `chmod 0700 data`, and `find data -type f -exec chmod 0600 {} +`. Do not start compose while the data directory or files are group/world accessible.

For completed feature work, run the focused test and the container path that covers the changed service:

```bash
docker compose build
docker compose up -d
# docker compose exec <service> <focused test command>
docker compose ps
docker compose down
```

If the repository uses another container runner, use the project-native equivalent. If container verification cannot run, state why and do not present the change as ready to push.