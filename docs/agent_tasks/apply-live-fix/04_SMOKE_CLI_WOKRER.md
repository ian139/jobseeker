Context:

[AGENTS.md](http://AGENTS.md) and OMP_ORCA_[WORKFLOW.md](http://WORKFLOW.md) are mandatory project policy. We need executable proof that live Playwright mode fills a disposable application form and stops before final submit.

Target:

- scraper/tests/test_apply_[pipeline.py](http://pipeline.py) or a new focused test file if existing style supports it

- scraper/tests/fixtures/apply_pages/

- CLI wiring for `job-sync apply-dry-run --live` only if needed for testability

- scraper/pyproject.toml only if test markers/dependencies need adjustment

Change:

Add a local/disposable live Playwright smoke test or smoke command that exercises the full observer → resolver → executor → runner loop without using real job boards or live LLM network calls.

The fixture should simulate a multi-step application:

Page 1:

- first name

- last name

- email

- phone

- LinkedIn

- portfolio

- resume upload

- Continue button

Page 2:

- select field

- radio field

- checkbox field

- one non-sensitive field answerable by fake LLM

- Continue button

Final page:

- final Submit button

The smoke should assert:

- deterministic fields filled

- fake LLM field filled

- resume upload attempted only with configured resume path or safely mocked

- Continue clicked

- final Submit not clicked

- terminal status is `dry_run_ready`

- page snapshots and resolver outputs are persisted or inspectable

Also test CLI/config behavior:

- LLM configured via `OLLAMA_CLOUD_API_KEY` or `OLLAMA_API_KEY`

- `--no-llm` disables LLM with explicit reason

- missing key yields explicit `llm_not_configured`

Non-goals:

Do not use external job sites.

Do not require live Ollama calls.

Do not weaken safety policy.

Do not change TheirStack ingestion.

Ownership:

You own smoke fixture/test and narrow CLI config testability only.

Development rule:

Add failing smoke/config test first, then implement smallest patch needed.

Acceptance:

- There is a repeatable local proof that live-style Playwright flow fills and advances to final-submit boundary.

- The test does not click final submit.

- The test does not depend on network LLM.

- Failures are specific enough to diagnose observer/resolver/executor.

Verification:

Run:

cd scraper

.venv/bin/python -m pytest scraper/tests/test_apply_[pipeline.py](http://pipeline.py) -k "live or smoke or cli"

Report:

1. Files changed

2. Test-first evidence

3. Verification run

4. Result

5. Unresolved risks