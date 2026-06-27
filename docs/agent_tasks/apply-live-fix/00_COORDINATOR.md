/plan

You are the parent/coordinator for the apply-live-fix feature.

[AGENTS.md](http://AGENTS.md) and OMP_ORCA_[WORKFLOW.md](http://WORKFLOW.md) are mandatory policy and already available from the repo root.

Goal:

Fix the live Playwright application pipeline so `job-sync apply-dry-run --live` observes the page, resolves known fields, uses eligible LLM inference when configured, executes safe fills/navigation, loops, and only hands off after deterministic and eligible LLM paths have been attempted.

Current bug:

The browser opens application pages, but almost every field is immediately handed off. Treat this as a pipeline bug, not acceptable conservative behavior, unless the page is genuinely blocked, sensitive, unsupported, or unanswerable after deterministic and eligible LLM resolution.

Use workers only after `/plan` establishes file ownership.

Read:

- `docs/agent_tasks/apply-live-fix/OWNERSHIP.md`

- `docs/agent_tasks/apply-live-fix/01_OBSERVER_WORKER.md`

- `docs/agent_tasks/apply-live-fix/02_RESOLVER_LLM_WORKER.md`

- `docs/agent_tasks/apply-live-fix/03_EXECUTOR_RUNNER_WORKER.md`

- `docs/agent_tasks/apply-live-fix/04_SMOKE_CLI_WORKER.md`

After planning, spawn sub-worktree workers if the ownership split is still valid.

Use DeepSeek V4 Pro workers through Ollama Cloud.

For each worker:

1. Create a sub-worktree.

2. Create a terminal in that sub-worktree.

3. Send the matching worker prompt file.

4. Wait for worker completion.

5. Inspect the diff.

6. Reject unrelated edits.

7. Integrate useful patches into the parent worktree.

8. Rerun focused verification in the parent worktree.

Do not allow workers to merge into the parent directly.

Final verification:

From `scraper/`:

```bash

.venv/bin/python -m pytest