# Agent Operating Notes

## Orca worktree coordination

For feature worktrees, prefer smaller same-worktree worker terminals over creating nested worktrees.

Use this pattern from inside the feature worktree:

```bash
orca terminal create --worktree active --title "<specific-subtask>" --command "omp" --json
orca terminal wait --terminal <handle> --for tui-idle --timeout-ms 60000 --json
orca terminal send --terminal <handle> --text '<narrow task prompt>' --enter --json
```

Rules:

- Do not use `orca worktree create` for subtasks unless the user explicitly asks for another checkout.
- Parent agent owns task decomposition, file ownership, and final verification.
- Spawn one worker per disjoint file or subsystem.
- Give each worker exact target files/symbols and explicit non-goals.
- Workers should avoid broad cleanup and project-wide gates; the parent runs final verification.
- Workers must not touch files assigned to another worker.
- If a worker needs a blocking decision, it should ask the parent/coordinator instead of guessing.

Prompt template:

```text
Target: <exact files/symbols>.
Change: <specific behavior to add/fix>.
Non-goals: do not edit <files/subsystems>.
Acceptance: <focused checks or observable behavior>.
Stay in this worktree. Do not create a new worktree. Report files changed and verification.
```
