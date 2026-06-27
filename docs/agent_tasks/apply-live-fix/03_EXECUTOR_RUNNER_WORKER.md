Context:

[AGENTS.md](http://AGENTS.md) and OMP_ORCA_[WORKFLOW.md](http://WORKFLOW.md) are mandatory project policy. The application pipeline should fill safe fields and advance pages, but it currently appears to open Playwright and hand off almost immediately.

Target:

- scraper/src/apply_pipeline/[executor.py](http://executor.py)

- scraper/src/apply_pipeline/[runner.py](http://runner.py)

- scraper/src/apply_pipeline/[policy.py](http://policy.py) only if guard logic requires a narrow additive fix

- scraper/tests/test_apply_[pipeline.py](http://pipeline.py)

Change:

Ensure executor and runner actually execute safe resolver decisions before terminal handoff. The runner should not finish a page as `needs_review` until observer + deterministic resolver + eligible LLM resolver have all completed and safe actions have been attempted when available.

Executor must support guarded:

- text/textarea fill

- select option

- radio/checkbox check

- typeahead/contenteditable where existing locator metadata supports it

- configured resume upload only

- safe Continue/Next/Apply navigation

- final submit refusal

Runner must:

- persist page snapshot

- persist resolver output

- log planned actions

- execute safe actions

- re-observe after navigation/action

- stop at final submit boundary with `dry_run_ready`

- produce precise terminal reason codes

Non-goals:

Do not edit observer or resolver unless coordinator explicitly expands scope.

Do not click final submit.

Do not bypass blockers.

Do not add board-specific automation.

Ownership:

You own [executor.py](http://executor.py), [runner.py](http://runner.py), narrowly [policy.py](http://policy.py) if needed, and executor/runner tests.

Development rule:

Add or update focused failing tests first.

Acceptance:

- Fake page test proves executor fills all supported safe field kinds and clicks Continue.

- Final submit is never clicked.

- Unknown field ID fails closed.

- Disabled navigation fails closed with structured reason.

- Runner test proves safe actions are executed before `needs_review`.

- Action logs are serializable and persisted or returned for run storage.

Verification:

Run:

cd scraper

.venv/bin/python -m pytest scraper/tests/test_apply_[pipeline.py](http://pipeline.py) -k "executor or runner"

Report:

1. Files changed

2. Test-first evidence

3. Verification run

4. Result

5. Unresolved risks