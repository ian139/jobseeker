Context:

[AGENTS.md](http://AGENTS.md) and OMP_ORCA_[WORKFLOW.md](http://WORKFLOW.md) are mandatory project policy. The current live pipeline appears to hand off almost every field after opening Playwright, suggesting resolver/LLM logic may be skipped or escalating too early.

Target:

- scraper/src/apply_pipeline/[resolver.py](http://resolver.py)

- scraper/src/apply_pipeline/[contracts.py](http://contracts.py) only if resolver output contracts need additive fields

- scraper/tests/test_apply_[pipeline.py](http://pipeline.py)

- any existing LLM/Ollama client module under scraper/src/apply_pipeline/ if present

Change:

Fix resolver flow so `needs_review` is not emitted prematurely. The resolver must:

1. deterministically fill known safe fields from profile/resume/job facts;

2. produce an explicit unresolved field list;

3. split unresolved fields into `eligible_for_llm` versus `needs_review`;

4. call the LLM adapter for eligible non-sensitive fields when configured and not disabled;

5. validate strict JSON output;

6. reject invalid field IDs and invalid schema;

7. merge deterministic and valid LLM answers;

8. return `continue`/actionable decisions when all required fields are answerable.

The LLM must receive normalized JSON only:

- page snapshot field metadata

- profile facts

- resume_summary / skills

- job description text

- policy constraints

The LLM must not receive raw Playwright handles.

Non-goals:

Do not edit observer/executor/runner unless the coordinator explicitly expands scope.

Do not weaken safety rules.

Do not infer sensitive/legal fields.

Do not add live network tests. Use fake LLM adapters in tests.

Ownership:

You own resolver code and resolver-focused tests only.

Development rule:

Add or update failing resolver tests first.

Acceptance:

- Known fields resolve deterministically.

- Unknown non-sensitive required fields become LLM-eligible, not immediate terminal handoff.

- Sensitive/legal/auth/CAPTCHA fields are never sent to LLM.

- Fake LLM response fills eligible fields.

- Bad LLM schema produces structured reason code.

- LLM not configured produces explicit `llm_not_configured`, not silent generic handoff.

- `--no-llm` behavior remains supported if CLI passes that flag into resolver config.

Verification:

Run:

cd scraper

.venv/bin/python -m pytest scraper/tests/test_apply_[pipeline.py](http://pipeline.py) -k "resolver or llm"

Report:

1. Files changed

2. Test-first evidence

3. Verification run

4. Result

5. Unresolved risks