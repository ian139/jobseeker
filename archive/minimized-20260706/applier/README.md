# Minimized active applier archive — 2026-07-06

This folder contains the applier implementation removed from active `src/jobs_assistant/` during minimization.

## Status

Reference-only. Do not import from active code. This is not a runnable package snapshot.

## Contents

- `src/observer.py`: static HTML page snapshot extraction.
- `src/resolver.py`: deterministic answer resolver and sensitive/manual refusal rules.
- `src/llm.py`: narrow resolver prompt/LLM adapter contract.
- `src/executor.py`: guarded fill/select/check/upload/non-final-click actions.
- `src/runner.py`: static dry-run orchestration.
- `src/review.py`: failed/blocked/needs-review sampler.
- `src/live_smoke.py`: Playwright import smoke.
- `tests/`: tests archived with those modules.

## Important incompleteness

The archived files depended on root `src/jobs_assistant/contracts.py` and `src/jobs_assistant/db.py` as they existed before minimization. Those shared files were minimized in-place to active scraper/ingestion needs, so this archive intentionally does not run by itself.

Use it only as behavioral reference when rebuilding the applier through OMP `workflowz`.

## Safety behavior to preserve in any rebuild

- Never click final submit.
- Refuse sensitive/manual fields instead of guessing.
- Upload only the configured resume path.
- Persist page snapshots/resolver output/action attempts before human review.
- Treat CAPTCHA, sign-in, assessments, identity checks, and payment gates as blockers.

## Rebuild direction

Future applier work should be decomposed into workflowz subtasks:

1. page observation contract;
2. resolver strict JSON contract;
3. executor guardrails;
4. run persistence/output;
5. safety/adversarial tests;
6. optional live Playwright smoke.

No final-submit behavior belongs in the rebuild until a separate submit policy exists and passes tests.
