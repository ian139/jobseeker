# jobs-assistant roadmap

The rebuilt guarded Greenhouse+Lever drafting workflow is active alongside the local job backlog. The older applier snapshot remains reference-only; it is not the implementation used by the CLI.

## Active workflow

```text
TheirStack / source feed / scraper output
  ↓
Normalize to JobInput
  ↓
Filtering and quality gates
  ↓
SQLite jobs backlog
  ↓
autofill --ats auto (Greenhouse or Lever, selected by exact route)
  ↓
claim → observe → resolve → one safe action → persist evidence
  ↓
review_ready / manual / blocked / failed
  ↓
autofill-review list
  ↓
human review and (if desired) human submission
  ↓
autofill-review complete | retry
```

## Rebuilt Greenhouse+Lever workflow — complete

- [x] Keep the SQLite backlog schema small and stable.
- [x] Preserve canonical URL and source job ID deduplication.
- [x] Preserve raw source payloads in `raw_json`.
- [x] Preserve source-search profiles and TheirStack credit-safe preview.
- [x] Require explicit paid-fetch opt-in before credit-consuming calls.
- [x] Keep feed/fixture import for deterministic tests and backfills.
- [x] Keep filtering and quality gates explicit and testable.
- [x] Rebuild the guarded application workflow around the Greenhouse-first adapter path plus the active Lever second adapter.
- [x] Support approved public HTTPS Greenhouse hosted, embed, and `grnh.se` routes plus direct Lever `jobs.lever.co`/`jobs.eu.lever.co` company/canonical-lowercase-UUID routes with optional `/apply`; reject other ATS, private/local, auth, CAPTCHA, assessment, and unsafe routes.
- [x] Keep source profiles (`--source-profile`/`--profile`) separate from
      application-profile JSON, resume input, and resolver description input.
- [x] Fill only proven safe non-final fields and stage one owned resume per run;
      unresolved required or sensitive fields remain manual.
- [x] Persist mode-`0700` `run-<id>` evidence with immediate SHA-256
      verification, private screenshots, and bounded review annotations.
- [x] Preserve `legacy-run-<id>` references when migrating older run data.
- [x] Provide `autofill-review list`, `complete`, and explicit `retry` with
      latest-run and window-state checks.
- [x] Keep headed review host-only and independently alive after CLI exit;
      user tab close is the close action, with no user-facing timer or CDP
      attach/reconnect path.
- [x] Keep container execution headless, non-root, UID/GID mapped, and bound
      to existing `data` (read/write) and `resume` (read-only) directories.

## Hard boundary

No submit policy is being added: final submission is human-only. The CLI never
targets a final-submit control. `autofill-review complete --outcome submitted`
records a human action; it does not submit. This boundary must remain true for
every future adapter and preference feature.

## Delivered roadmap contracts and tests

- [x] Activate Lever as the second ATS adapter behind the same route, network, safe-action, private-artifact, and no-submit gates. Direct-host, canonical-UUID, `/apply`, parity, and workflow/no-submit coverage is in `tests/test_application_contracts.py`, `tests/test_application_profile.py`, `tests/test_puppeteer_adapter.py`, and `tests/test_application_workflow.py`.
- [x] Deliver v1 named application-profile presets with exact `schema_version`/`name`/`profile` validation, safe `field_answers`, explicit preset directory/name flags, source-profile separation, and exact-byte SHA-256 provenance. Coverage is in `tests/test_application_profile_presets.py` and `tests/test_cli_smoke.py`.
- [x] Deliver v1 user preferences with exact safe mappings, opt-outs, and review ordering; atomic `init`, `show`, set/remove mapping, set/remove opt-out, and set/remove review-order commands; deterministic precedence; redacted output; and sensitive/opaque exclusions. Coverage is in `tests/test_application_preferences.py`, `tests/test_application_workflow.py`, and `tests/test_cli_smoke.py`.

- [x] Deliver deterministic pinned TheirStack ATS ingestion filtering. Pinned
      Greenhouse and Lever syncs re-fetch the latest requested paid raw window
      on every invocation without an incremental checkpoint; filtering may
      yield fewer eligible jobs than the raw limit, and repeats deduplicate
      without pagination. Preserve the legacy `auto` checkpoint behavior.

- [x] Preserve normalized source location, remote status, and description metadata across feed normalization and backlog upserts, including supported aliases and malformed-value rejection. Coverage is in `tests/test_job_source.py`.
- [x] Reject unframed browser protocol responses and poison the session after a bad length prefix; coverage is in `tests/test_puppeteer_adapter.py`.

- [x] Reject malformed TheirStack/feed envelopes instead of silently accepting unsupported shapes; preserve strict source metadata normalization. Coverage is in `tests/test_job_source.py` and `tests/test_theirstack_sync.py`.
- [x] Harden browser framing and response envelopes with canonical length parsing, schema validation, poisoned sessions, and durable malformed-observation failure evidence. Coverage is in `tests/test_puppeteer_adapter.py` and `tests/test_application_workflow.py`.

- [x] Add guarded ATS location autofill from explicit profile facts, with safe-field validation and workflow coverage in `tests/test_application_profile.py`.

- [x] Thread the selected ATS policy through inference, planning, and safe-button eligibility so final-like controls remain rejected consistently across adapters. Coverage is in `tests/test_application_pipeline.py`.

- [x] Cover the Lever policy-aware button-only workflow, proving safe continuation actions use the selected ATS policy and remain non-final. Coverage is in `tests/test_application_workflow.py`.

- [x] Keep readiness and safe-click progress signatures generation-agnostic while retaining final-control membership, preventing stale semantic comparisons without treating re-observation IDs as progress. Coverage is in `tests/test_application_pipeline.py`.

- [x] Persist per-iteration guarded action evidence before mutation, including planned/rejected actions, ATS policy, no-submit invariant, and the exact observation ID; cover mutation-failure durability. Coverage is in `tests/test_application_workflow.py`.

- [x] Reject malformed field constraint types at the observation boundary while accepting typed nonnegative lengths and string constraint values. Coverage is in `tests/test_application_workflow.py`.

- [x] Link each guarded action plan to a private, canonical observation snapshot with target-generation/selector fidelity, SHA-256 verification, and mutation-failure retention. Coverage is in `tests/test_application_workflow.py` and `tests/test_application_pipeline.py`.

- [x] Add validated paid TheirStack pagination with deterministic multi-page aggregation, envelope/metadata consistency checks, pinned ATS filtering before global dedupe, preview credit safety, and no partial return on malformed later pages. Coverage is in `tests/test_theirstack_sync.py`.

- [x] Enforce the paid pagination safety cap deterministically when `total_results` is absent by counting successful page requests before issuing the next credit-consuming request. Coverage is in `tests/test_theirstack_sync.py`.

- [x] Complete configured resume uploads across valid re-observation, retaining the file without re-upload, resolving required-file state, and stopping review-ready with zero final submissions. Coverage is in `tests/test_application_workflow.py`.

- [x] Make batch backlog upserts atomic with caller-aware BEGIN/SAVEPOINT semantics, preserving public single-row commit behavior and rolling back earlier rows when a later item fails. Coverage is in `tests/test_backlog_ingestion.py`.

- [x] Allow runtime-backed non-final native submit continuations only through an explicit framed protocol flag, same-origin/no-form/no-navigation/no-sensitive gates, and final-target exclusion. Generic submit/input/role controls and final submission remain denied. Coverage is in `tests/test_application_workflow.py`, `tests/test_puppeteer_adapter.py`, and the Puppeteer smoke.

- [x] Normalize TheirStack `external_id` and `source_job_id` aliases after existing ID precedence, enabling stable URL-less ingestion and dedupe while retaining transactional rejection for malformed records. Coverage is in `tests/test_theirstack_sync.py`.

- [x] Permit guarded same-document Greenhouse/Lever SPA continuations only when approved route identity, HTTPS origin/frame chain, final-like exclusion, and zero post-click network activity all hold. Preserve distinct unsafe navigation/network failures and no-submit behavior. Coverage is in `tests/test_application_workflow.py` and `tests/test_puppeteer_adapter.py`.

## Reference-only history

The older minimized applier snapshot is retained for historical comparison:

```text
archive/minimized-20260706/applier/
```

It is not a runnable package snapshot and is not the active workflow. New
changes belong in the current guarded adapter path, not in that archive.

## Verification

```bash
npm install
npm run install-browser
npm run puppeteer-smoke
uv lock --check
uv run --frozen --extra dev python -m pytest tests/test_cli_smoke.py
sh scripts/container-smoke.sh
```

Run the complete Python suite with
`uv run --frozen --extra dev python -m pytest` when changing executable code.
Headed survival remains a manual host check: use a physical benign click and
close the review tab/window.
