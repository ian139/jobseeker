# Agent Operating Notes

## Default coordinator model

Every new feature worktree should have one advisor-enabled parent agent. The parent is the coordinator: it plans, splits work, assigns file ownership, reviews worker output, resolves conflicts, and runs final verification.

Launch the parent agent with:

```bash
omp --advisor --model "openai-codex/gpt-5.5" --thinking xhigh
```

The advisor is a guardrail for the parent agent. It should keep the plan, worker assignments, and final merge from drifting out of scope. Do not assume the advisor directly controls child terminals; the parent must inspect worker results and enforce boundaries.

## Worker allocation

Prefer smaller same-worktree worker terminals over nested worktrees. Use workers when a subtask has clear ownership and can finish without seeing another worker's result.

Good worker splits:

- one file or tightly related file pair
- one CLI command family
- one storage/schema area
- one focused test file
- one read-only review pass

Bad worker splits:

- "clean up everything"
- "make it better"
- overlapping edits to the same file
- tasks that require broad architectural decisions
- tasks that need another worker's unfinished output

Implementation workers may use either DeepSeek V4 or GPT-5.5 at medium thinking:

```bash
orca terminal create --worktree active --title "<specific-subtask>" --command 'omp --model "openrouter/deepseek/deepseek-v4-pro" --thinking medium' --json
orca terminal create --worktree active --title "<specific-subtask>" --command 'omp --model "openai-codex/gpt-5.5" --thinking medium' --json
```

Then wait for the worker and send a narrow assignment:

```bash
orca terminal wait --terminal <handle> --for tui-idle --timeout-ms 60000 --json
orca terminal send --terminal <handle> --text '<narrow task prompt>' --enter --json
```

## Coordinator rules

- Do not use `orca worktree create` for subtasks unless the user explicitly asks for another checkout.
- Keep one parent/coordinator per feature worktree.
- Write down file ownership before spawning workers.
- Spawn one worker per disjoint file, subsystem, or verification slice.
- Give each worker exact target files/symbols, explicit non-goals, and acceptance checks.
- Prefer read-only review workers before risky merge or broad refactor work.
- Parent owns final integration, conflict resolution, cleanup, and verification.
- Parent must review worker diffs before accepting them as complete.
- Workers should avoid broad cleanup and project-wide gates; the parent runs final verification once.
- Workers must not touch files assigned to another worker.
- If a worker needs a blocking decision, it should ask the parent/coordinator instead of guessing.
- If a worker drifts out of scope, the parent should stop using that output and either correct the prompt or redo the slice.

## Worker prompt template

```text
Target: <exact files/symbols>.
Change: <specific behavior to add/fix>.
Non-goals: do not edit <files/subsystems>.
Ownership: you own only <files>; do not touch anything else.
Acceptance: <focused checks or observable behavior>.
Stay in this worktree. Do not create a new worktree. Do not run broad cleanup.
Report files changed, verification run, and any unresolved risks.
```

## Parent handoff checklist

Before merging worker output, the parent must confirm:

- the worker stayed inside assigned ownership
- no unrelated files changed
- tests or focused checks match the assignment
- duplicate/conflicting implementations were reconciled
- final behavior works from the user's perspective
