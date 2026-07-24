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
Container verification covers only packaging, non-root/UID/GID and bind mounts, headless Chromium, CLI help, and OMP-process startup/teardown.
It does not run a browser-backed coordinator: `application-rpc` is headed-only (`run.start` requires `headed: true`), and current Compose provides no `DISPLAY`.

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

- [x] Add read-only `backlog-list` inspection with validated status/limit filters, deterministic ordering, stable public JSON rows, total/pending counts, and no mutation/claim/network behavior. Coverage is in `tests/test_cli_smoke.py` and `tests/test_backlog_ingestion.py`.

- [x] Enforce `backlog-list` read-only SQLite access without creating missing databases, while preserving writable behavior for ingestion and application commands. Coverage is in `tests/test_cli_smoke.py` and `tests/test_application_claims.py`; command behavior is documented in `README.md`.

- [x] Add explicit `backlog-archive JOB_ID... --confirm` for queued-only atomic compare-and-set archival, including URL-less rows, rollback on conflicts, fixed redacted results, and no deletion/claim/browser/network behavior. Coverage is in `tests/test_backlog_ingestion.py` and `tests/test_cli_smoke.py`; command safety is documented in `README.md`.

- [x] Add exact, parameterized `--source` filtering to read-only `backlog-list`, with pre-connect validation, source-scoped counts, deterministic ordering, literal SQL-injection-safe matching, and unchanged no-source behavior. Coverage is in `tests/test_cli_smoke.py`; syntax and full package checks pass.

- [x] Expose import-feed source provenance through validated `--source`, preserving the `job_source` default while keeping distinct feed sources filterable in the backlog. Coverage is in `tests/test_cli_smoke.py` and `tests/test_job_source.py`; default, invalid, and injection-like source cases pass.

- [x] Add read-only `backlog-show JOB_ID` detail inspection with positive-ID pre-connect validation, deterministic parameterized lookup across statuses, bounded plain-text descriptions, raw-payload exclusion, and no mutation/network behavior. Coverage is in `tests/test_cli_smoke.py`; command safety is documented in `README.md`.

- [x] Harden generic `import-feed` envelope and record validation with fixed redacted `invalid_input` failures, pre-connect local validation, all-record validation before transactional upsert, and unchanged valid/HTTP behavior. Coverage is in `tests/test_cli_smoke.py` and `tests/test_job_source.py`.

- [x] Persist guarded private workflow screenshots at initial, blocker, and final/handoff stages with verified path/bytes/SHA metadata, bounded deduplication, durable failure handling, and unchanged no-submit behavior. Coverage is in `tests/test_application_workflow.py`; live adapter coverage remains environment-skipped, and blocker-stage indexing is regression-tested.

- [x] Extend read-only `backlog-list` with validated parameterized `--offset` pagination through 100,000 rows while preserving omitted-output compatibility, exact filters, deterministic ordering, stable counts, and no mutation/network/database creation. Coverage is in `tests/test_cli_smoke.py`; behavior is documented in `README.md`.

- [x] Harden native submit-typed non-final ATS continuation with explicit permit evidence, approved same-job route identity across re-observation, distinct unsafe-navigation/network failures, durable before/after evidence, and zero final-submit calls. Direct continuation assertions are in `tests/test_application_workflow.py`; semantic-signature and final-target schema coverage is in `tests/test_application_pipeline.py` and `tests/test_application_contracts.py`.

- [x] Promote validated read-only backlog listing query into a typed domain API (`list_backlog_jobs`) with exact status/source/limit/offset validation, deterministic ordering, stable counts, and no duplicate CLI SQL. Coverage is in `tests/test_backlog_ingestion.py` and `tests/test_cli_smoke.py`.

- [x] Audit database-backed generic `import-feed` runs with source/mode/count/completion metadata and fixed redacted failures. Job upserts and terminal success metadata commit atomically; rollback, audit-write, rollback-failure, and post-commit output fault injection preserve truthful durable state. Coverage is in `tests/test_cli_smoke.py`; full package, Puppeteer smoke, container smoke, lock, and CLI checks pass.

- [x] Add read-only `import-feed --dry-run` preflight for JSON and HTTP feeds using disposable in-memory SQLite simulation of production normalization/deduplication. Missing databases stay absent, existing databases and sync audits remain byte/state unchanged, counts cover all input, and normalized preview output is allow-listed and capped at 100. Coverage is in `tests/test_cli_smoke.py`; full package, Puppeteer smoke, container smoke, lock, CLI, and direct smoke checks pass.

- [x] Add read-only `autofill-review show RUN_ID` evidence inspection with positive-ID pre-connect validation, read-only SQLite, noncreating descriptor-relative artifact access, exact SHA-verified manifest filenames, producer-compatible staged evidence, strict bounded redacted projections, and no claim/review/browser/network mutation. Coverage is in `tests/test_cli_smoke.py`, `tests/test_application_claims.py`, and `tests/test_artifacts.py`; full package, Puppeteer smoke, container smoke, lock, CLI, and direct byte/state-invariance smoke checks pass.

- [x] Close the runnable browser-adapter verification gate with final action-time network quiet/revalidation, CDP-attributed post-mutation fetch/WebSocket/popup denial, exact implicit-favicon suppression, and independently verified detached owner/browser test cleanup. The host-only `npm run puppeteer-verify` gate runs every automated adapter check (including the supported-host headed diagnostic) and deselects only the physical headed review-window survival check; full package, Puppeteer smoke, container smoke, lock, and CLI checks pass.

- [x] Close the supplied live Greenhouse compatibility gap by making both CONNECT relay directions tolerate peer-close/write errors without crashing the protocol owner, while preserving authorization revalidation, byte budgets, terminal attribution, and transport-token isolation. Deterministic local RST coverage is in `tests/test_puppeteer_adapter.py::test_connect_relay_peer_close_does_not_crash_protocol_owner`; the live route was observed successfully after the fix and the independent Wave 28 safety review returned READY.

- [x] Deliver native `<input type="button">` ATS continuation with offline click permission, native value-bound live identity, final-like value denial, hostile-listener terminal/offline enforcement, and zero final-submit calls. Coverage is in `tests/test_puppeteer_adapter.py`, `tests/test_application_pipeline.py`, and `tests/test_application_workflow.py`; corrected independent Wave 29 safety and hostile-listener reviews returned READY.

- [x] Deliver native `<select multiple>` ATS autofill with immutable tuple values, native descriptor-based mutation, fail-closed prototype poisoning, isolated-realm self-test, deterministic TOCTOU drift detection via child-only nonce, select-only post-mutation network gating, conventional placeholder exemption, observation quarantine for invalid/ambiguous evidence, and zero final-submit calls. Coverage is in `tests/test_puppeteer_adapter.py`, `tests/test_application_contracts.py`, `tests/test_application_pipeline.py`, `tests/test_application_preferences.py`, `tests/test_application_profile.py`, and `tests/test_application_workflow.py`; full package (726 passed, 2 skipped), select-native-self-test, and request-guard-self-test (9/9) pass, and both corrected independent Wave 30 reviews returned READY.

- [x] Deliver guarded page-by-page ATS drafting at code checkpoint `b0598f4`:
      reobserve every page, scope resolver caches to the current controls, map
      profile/resume/job context through the validated LLM contract, dispatch
      only action-time-revalidated non-final controls, permit exact same-job
      anchor GET continuation behind route/network caps, and stop
      manual/blocked on ambiguity, sensitive fields, authentication,
      inherited disabled controls, unstable state, or oversized traffic.
      The integrated three-page smoke reached `review_ready` after two resolver
      pages and two continuations with zero final-submit calls. Full Python
      (722 passed, 59 skipped), full headed browser adapter (91 passed,
      2 skipped), lock, CLI, protocol, wheel, Compose, and isolated container
      checks pass; the independent Wave 31 safety review found no remaining
      actionable issue.

- [x] Close inherited native/ARIA-disabled actionability at code checkpoint
      `e4eea72`. Observation now propagates `aria-disabled="true"` ancestry,
      and action-time guards reject drift before text/checkbox/select mutation,
      event dispatch, staged upload, ordinary button dispatch, or same-job
      continuation. Uploads clean rejected immutable staging; continuation
      semantics, exact identity, visibility, hit testing, and disabled state
      are revalidated while the one-use navigation permit is absent, then the
      permit is created synchronously immediately before `page.goto`.
      Token-gated browser regressions cover native fieldset inheritance,
      direct ARIA state, ancestor state, fill/check/single-select/multi-select,
      upload cleanup/no-dispatch, and semantic/ARIA continuation drift.
      Full Python (722 passed, 67 skipped), full headed browser adapter
      (99 passed, 2 skipped), lock, CLI, Ruff, protocol/self-tests, wheel,
      Compose, and isolated container checks pass; two independent safety
      reviews found no remaining actionable issue. Puppeteer's upload and
      navigation CDP calls necessarily follow rather than share the renderer
      evaluation; the guard-to-command boundary is immediate, and no concrete
      bypass was established.

- [x] Bind same-job continuation permits to coordinator navigation provenance
      at code checkpoint `4c289db`. A permit now requires matching main-frame
      `frameStartedNavigating`, network request/loader/frame identifiers, a
      browser-initiated request, exact route identity, and committed
      destination before post-commit static assets are eligible. Renderer
      navigation intent revokes the permit terminally before transport.
      Token-gated regressions cover `location.assign`, programmatic anchor
      clicks, and meta refresh races with zero destination or final-submit
      requests; legitimate continuation and post-commit static loading remain
      allowed. Full Python (722 passed, 70 skipped), the repository browser
      gate (103 passed, 1 intentionally deselected), focused race checks,
      lock, CLI, Ruff, protocol/self-tests, wheel, and isolated container smoke
      pass. One initial all-adapter run had a transient headed emergency-cleanup
      failure; its exact parameter and the repository gate passed on rerun.
      Independent safety review found no remaining actionable issue.

## Open gaps and blockers

- [ ] Physical headed review-window survival remains a manual host gate requiring a benign human click and tab/window close. It is intentionally excluded by exact test name from `npm run puppeteer-verify`; no code path automates the physical action.
- [ ] Physical headed handoff survival and trusted-gesture activation remain manual host gates requiring a benign human click and tab/window close. Both exact tests are excluded from `npm run puppeteer-verify`; no code path automates either physical action.

- [ ] Live third-party ATS DOM/service-worker changes and real LLM streaming
      remain environment-dependent compatibility gates. Deterministic fixtures
      cover the safety contracts and fail closed, but provider compatibility
      still requires benign credentialed live checks.

## Requested future policy changes (not active)

The requested future direction is to expand the workflow beyond its current
safety scope. These entries record desired policy and design work only:
`AGENTS.md` and the existing deterministic gates remain authoritative until a
separate safety-policy decision is made and the resulting behavior is
implemented, adversarially tested, and approved.

- [ ] Allow arbitrary browser scripting and unsupported ATS sites. The intended
      outcome is to handle sites without dedicated Greenhouse or Lever adapters
      and to permit a broader browser action surface. This requires a separate
      policy decision and a generalized route, observation, action, and
      execution contract covering sandboxing, deterministic pre-mutation
      authorization, stale-state rejection, provenance, evidence, and the
      no-submit invariant. The current guarded executor cannot be bypassed.

- [ ] Allow credential, MFA, CAPTCHA, assessment, and protected-class
      workflows. The intended outcome is to continue applications that
      encounter these steps instead of always stopping. This requires a
      separate policy decision plus explicit consent, secret-handling,
      sensitive-data, source-of-truth, human-oversight, and audit contracts,
      followed by dedicated implementation and adversarial tests. Until then,
      these remain manual stop points; this entry does not authorize credential
      exposure, challenge bypass, inferred protected-class answers, or
      automated assessment completion.

## Historical source

Historical source snapshots are preserved in git history and are not part of the active working tree. New changes belong in the current guarded adapter path.

## Persistent OMP RPC draft workflow

Goal: prepare supported Greenhouse and Lever application drafts for human
review through one persistent OMP run. The guarded adapter remains the sole
browser-mutation authority and final submission is never automated.

Measurement: focused protocol/workflow/ownership tests, maintained browser
fixtures, native OMP launch evidence, private artifact manifests, CLI smoke,
and container checks. Target: at least 90% of maintained supported fixtures
reach their expected safe terminal state, with every mutation observation-
bound and evidenced.
Container checks in this workflow cover packaging, CLI help, and OMP-process lifecycle only; they do not start a browser-backed `application-rpc` workflow. The coordinator remains headed-only, and current Compose provides no `DISPLAY`.

- [x] 1. Define the persistent OMP run protocol.
- [x] 2. Expose the existing workflow through the RPC boundary.
- [x] 3. Enforce exclusive browser-session ownership.
- [x] 4. Define structured immutable observation tools.
- [x] 5. Define the high-level guarded browser tools.
- [x] 6. Preserve ATS route, frame, and network restrictions.
- [x] 7. Use the canonical candidate-profile contract.
- [x] 8. Restrict resume selection and upload.
- [x] 9. Separate deterministic resolution from model assistance.
- [x] 10. Define stable manual-intervention categories.
- [x] 11. Implement the one-action control loop.
- [x] 12. Persist a complete private run workspace.
- [x] 13. Extend durable application-run state.
- [x] 14. Stream and replay redacted progress events.
- [x] 15. Implement verified review-ready browser handoff.
- [x] 16. Record human-reported outcomes separately.
- [x] 17. Smoke-test supported application runs.
- [x] 18. Run repository and container verification.
      The full Docker Compose lifecycle and container smoke now pass; the
      remaining physical trusted-gesture and live-provider gates stay open.
The container portion is packaging/CLI/OMP-process smoke only; it does not exercise a browser-backed `application-rpc` run.

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
Headed handoff survival and trusted-gesture activation remain manual host checks:
use a physical benign click and close the review tab/window.
