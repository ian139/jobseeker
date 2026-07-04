# Ohm workflowz + Orca development workflow

Use this workflow for every Ohm/OMP coordinator, Orca worker, and Orca dev session in this repository. `AGENTS.md` is the source of truth and is auto-loaded for agents launched from this workspace; do not ask the user to restate it.

## Always-on rules

- Start from the repository root so Ohm/OMP sees `AGENTS.md` automatically.
- Treat `AGENTS.md` as mandatory context for every prompt, worker assignment, child worktree, and review.
- Start every non-trivial feature, bug fix, refactor, or research task with `/plan`.
- Use Ohm `workflowz` as the subtask system: decompose the plan into explicit subtasks, assign ownership, track status, and capture acceptance criteria before launching workers.
- Use Orca as the execution and parallelization layer: each implementation worker that edits code should run in an Orca child worktree unless the coordinator documents why same-worktree execution is safer.
- For ambiguous or high-impact implementation work, create multiple Orca child worktrees for the same subtask and let them produce competing implementations. The parent worktree chooses the best patch after comparing behavior, diff size, tests, maintainability, and policy fit.
- Use test-first development: write or update the focused failing test before implementation.
- Prefer functional core, explicit service boundaries, typed data, and side effects at service edges.
- Keep containers as the default runtime; host execution is developer convenience only.
- Run final verification through the containerized path before marking work ready to push.
- Never use model review as a substitute for tests, smoke checks, or container evidence.

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

Create one feature worktree and one advisor-enabled parent coordinator for a new feature or risky fix:

```bash
orca worktree create --name "<short-feature-name>" --json
orca terminal create \
  --worktree "<short-feature-name>" \
  --title "coordinator" \
  --command 'omp --advisor --model "openai-codex/gpt-5.5" --thinking xhigh' \
  --json
orca terminal wait --terminal <handle> --for tui-idle --timeout-ms 60000 --json
```

Use `orca-dev` instead of `orca` when operating an Orca development build.

## Planning checklist

During `/plan`, the parent coordinator must record:

1. User goal and acceptance criteria.
2. Goal metrics: Prometheus/Grafana query or panel when available; otherwise test, CLI, fixture, log, or markdown checklist metric.
3. Target threshold/invariant, baseline if known, and stop condition.
4. Affected files, services, commands, and container targets.
5. The test to write or update first.
6. The Ohm `workflowz` subtasks, each with target files, non-goals, owner, acceptance metric, and verification command.
7. Which subtasks need Orca child worktrees, and which need multiple competing child worktrees.
8. File ownership before any worker edits.
9. Risks, service contracts, environment variables, and verification commands.

## Worker model default

Use DeepSeek V4 Pro through Ollama Cloud for implementation workers by default:

```bash
omp --model "ollama-cloud/deepseek-v4-pro" --thinking high
```

Use GPT-5.5 only when the task needs advisor-level coordination, unusually deep architecture work, or the DeepSeek/Ollama Cloud path is unavailable.

## Ohm workflowz subtasks

Every worker assignment must start as an Ohm `workflowz` subtask. A valid subtask has:

- One clear objective.
- Exact files or symbols owned by the worker.
- Explicit non-goals and forbidden files.
- A goal metric, target threshold, measurement source, and stop condition.
- A focused failing test or observable behavior to prove first.
- A verification command the worker may run.
- A report contract for files changed, metric movement, test-first evidence, verification, and risks.

Do not launch an implementation worker from an informal chat note when a `workflowz` subtask can carry the same contract.

## Orca child-worktree parallelization

Use Orca child worktrees for parallel implementation, isolation, and competing approaches:

```bash
orca worktree create --name "<parent-feature>-<specific-subtask>-a" --parent-worktree active --json
orca terminal create \
  --worktree "<parent-feature>-<specific-subtask>-a" \
  --title "<specific-subtask>-a" \
  --command 'omp --model "ollama-cloud/deepseek-v4-pro" --thinking high' \
  --json
orca terminal wait --terminal <handle> --for tui-idle --timeout-ms 60000 --json
orca terminal send --terminal <handle> --text '<workflowz subtask prompt>' --enter --json
```

For multiple competing implementations, repeat the child-worktree creation with suffixes such as `-a`, `-b`, and `-c`. Keep each child isolated; do not let sibling workers patch each other's worktrees.

## Parent selection and integration

Child-worktree output is a patch proposal, not an automatic merge. The parent coordinator must:

1. Inspect every child diff.
2. Compare verification output and failing-test evidence.
3. Prefer the smallest implementation that satisfies the contract and preserves repository conventions.
4. Integrate only the selected patch or selected pieces into the parent worktree.
5. Reject unrelated edits, broad cleanup, stale scaffolding, and policy violations.
6. Rerun focused verification in the parent before final verification.

Same-worktree workers are allowed only for read-only investigation, mechanical edits to disjoint files, or emergency fixes where child worktree overhead would increase risk. The parent must record that exception in the plan.

## Worker prompt contract

Every worker prompt must include:

```text
Context:
AGENTS.md is mandatory project policy and is already available in this workspace. Follow it without asking the user to restate it.

Workflowz:
This prompt implements Ohm `workflowz` subtask <id/title>. Do not expand scope beyond that subtask.

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

For completed feature work, run the focused test and the container path that covers the changed service:

```bash
docker compose build
docker compose up -d
# docker compose exec <service> <focused test command>
docker compose ps
docker compose down
```

If the repository uses another container runner, use the project-native equivalent. If container verification cannot run, state why and do not present the change as ready to push.