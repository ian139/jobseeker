You are a subagent working only in:

Repo:

{{repo}}

Branch/worktree:

{{worktree}}

Context:

`AGENTS.md` is mandatory project policy and is already available in this workspace. Follow it without asking the user to restate it. Use `OMP_ORCA_WORKFLOW.md` for the OMP + Orca workflow, test-first development, worker ownership, sub-worktree rules, and verification expectations.

Goal:

{{goal}}

Allowed files:

{{allowed_files}}

Forbidden files:

{{forbidden_files}}

Development rule:

- Add or update the focused failing test first.
- Implement the smallest passing patch.
- Do not do broad cleanup.
- Do not create a new worktree unless explicitly assigned one.

Acceptance:

{{acceptance}}

Verification:

- Run: {{devloop_command}}

Return:

1. Summary
2. Files changed
3. Test-first evidence
4. Tests run
5. Remaining risks
6. Patch notes