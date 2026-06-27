Context:

[AGENTS.md](http://AGENTS.md) and OMP_ORCA_[WORKFLOW.md](http://WORKFLOW.md) are mandatory project policy. Follow the observer/resolver/executor split. You are fixing the live application pipeline where Playwright opens pages but the system hands off before filling fields.

Target:

- scraper/src/apply_pipeline/[observer.py](http://observer.py)

- scraper/src/apply_pipeline/[contracts.py](http://contracts.py) only if observer output contracts need additive fields

- scraper/tests/test_apply_[pipeline.py](http://pipeline.py)

- scraper/tests/fixtures/apply_pages/ only if needed

Change:

Improve and test observer behavior so live/static pages produce useful PageSnapshot data for resolver/executor. The observer must capture visible form controls from the main page and frames:

- input text/email/tel/url/number

- textarea

- select with options

- radio groups

- checkbox groups

- file upload

- contenteditable/typeahead-like controls where practical

- labels from label[for], wrapping labels, aria-label, aria-labelledby, placeholder, nearby text

- required via required, aria-required, label markers

- disabled state

- visible validation errors/blockers

- actionable buttons/links, including Continue/Next/Apply and final Submit candidates

Non-goals:

Do not edit resolver, executor, runner, CLI, database, or TheirStack code.

Do not add host-specific automations.

Do not decide answers in the observer.

Do not call an LLM.

Ownership:

You own [observer.py](http://observer.py) and observer-focused tests/fixtures only.

Development rule:

Add or update the focused failing test first. Use static HTML fixtures where possible.

Acceptance:

- Observer returns stable field/button IDs.

- Observer includes labels, required flags, options, disabled state, and enough locator metadata for executor mapping.

- Observer does not use profile/resume facts.

- Observer test covers text, textarea, select, radio, checkbox, file, typeahead-like, disabled button, visible error, and final submit candidate.

- Existing tests still pass.

Verification:

Run:

cd scraper

.venv/bin/python -m pytest scraper/tests/test_apply_[pipeline.py](http://pipeline.py) -k "observer or snapshot"

Report:

1. Files changed

2. Test-first evidence

3. Verification run

4. Result

5. Unresolved risks