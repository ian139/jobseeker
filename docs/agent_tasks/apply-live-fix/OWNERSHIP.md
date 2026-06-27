# Apply Live Fix Worker Ownership

## Coordinator owns

- Overall plan

- File ownership enforcement

- Shared contracts

- CLI wiring decisions

- Integration of worker diffs

- Final verification

- Container verification

Coordinator-only unless explicitly delegated:

- `scraper/src/apply_pipeline/contracts.py`

- `scraper/pyproject.toml`

- `scraper/src/db/schema.sql`

- CLI entrypoint files

## Worker A — Observer

Owns:

- `scraper/src/apply_pipeline/observer.py`

- `scraper/tests/apply_pipeline/test_observer.py`

- observer-related static fixtures

Goal:

Make PageSnapshot extraction reliable enough for resolver/executor.

## Worker B — Resolver / LLM

Owns:

- `scraper/src/apply_pipeline/resolver.py`

- `scraper/tests/apply_pipeline/test_resolver_llm.py`

Goal:

Prevent premature `needs_review`; make deterministic + eligible LLM resolution work.

## Worker C — Executor / Runner

Owns:

- `scraper/src/apply_pipeline/executor.py`

- `scraper/src/apply_pipeline/runner.py`

- `scraper/src/apply_pipeline/policy.py` only for narrow guard fixes

- `scraper/tests/apply_pipeline/test_executor_runner.py`

Goal:

Actually execute safe actions before terminal handoff.

## Worker D — Smoke / CLI

Owns:

- `scraper/tests/apply_pipeline/test_live_smoke.py`

- smoke fixtures under `scraper/tests/fixtures/apply_pages/`

- CLI config only if required and approved by coordinator

Goal:

Prove local Playwright live mode fills a disposable form and stops before final submit.

## Rules

- Workers may read any file.

- Workers may edit only owned files.

- Workers must write/update focused failing tests first.

- Workers must not create more worktrees.

- Workers must not merge their own work.

- Coordinator reviews all worker diffs before integration.