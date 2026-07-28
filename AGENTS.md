# Repository Guidelines

## Project Overview

This repository implements Phase 1 application execution, the completed Phase 2 per-job resume generator, and the active Phase 3 bounded-platform SQLite backlog workflow. Claimable platforms are exactly Greenhouse, Ashby, and explicitly host-verified employer routes. Phase 3 is explicitly authorized for persistent supervised OMP operation with exactly one active job. Deterministic code owns source/platform classification, normalized host-and-URL job binding, resume preparation, redirect reclassification, and platform action mechanics; OMP/model reasoning is limited to oversight/diagnosis and allowed unresolved response inference. Unchecked live gates in `TODO.md` remain required evidence for completion claims.

`TODO.md` is the scope and safety authority. `PROJECT_HANDOFF.md` is historical evidence only; verify its paths and claims against the active tree.

## Architecture & Data Flow

```text
Greenhouse/Ashby/verified-employer-host source payload + SQLite backlog
  -> exact platform classification and normalized host-bound snapshot
  -> deterministic verified job-specific resume generation
  -> atomic full-snapshot claim and private run workspace
  -> policy-free DOM observer
  -> deterministic platform action plan
  -> OMP browser action and fresh retention proof
  -> optional model inference for unresolved non-sensitive response content
  -> immutable ledger, completeness audit, and audited submission
  -> canonical private evidence + durable SQLite outcome
  -> next supported backlog inspection
```

- `src/phase1/contract.mjs` and `profile.mjs` validate fixed run settings, secure local inputs, applicant data, and append-only verified answer memory.
- `src/phase1/observer.js` is a self-contained page-context IIFE. It inventories controls, frames, blockers, values, validation, and final-action candidates; it must not choose answers or perform actions.
- `src/phase1/ledger.mjs` owns stable field identity, exact answer-source precedence, observation chains/diffs, deliberate grouped states, action references, and retention checks.
`src/phase1/audit.mjs` requires every active field to be deliberate, valid, retained, and current while rejecting unknown controls, blockers, stale refs, and incomplete submissions.
`src/phase1/evidence.mjs` publishes bounded, owner-private JSON/JSONL artifacts, file identities, an action journal, and a submission completion report.
OMP performs browser actions. Final submission is automated by OMP after the completeness audit passes.
- `src/phase1/backlog-runner.mjs` and `migrations/004-durable-active-runs.sql` own atomic claims, one-active-run enforcement, leases, restart recovery, same-run `needs_user` continuation, workspace binding, and canonical terminal persistence. OMP remains the persistent loop.
- `src/phase1/platforms.mjs` is the fail-closed three-platform registry, normalized payload extractor, deterministic redirect classifier, and action planner. Employer routes require an exact source-verified ASCII DNS host and bounded HTTPS career pathname; unknown widgets remain unresolved.
- `src/phase1/job-source.mjs` owns supported ingestion, canonical URL/source deduplication, normalized description hashes, host-bound queue reads, and unsupported-row quarantine. Forward migrations 005–007 add the platform snapshot and employer-host authority while refusing active-run rebuilds.
- `src/phase1/preparation.mjs` composes recovery-first operation with persisted answer-memory binding, optional new-claim-only `minimumJobId`, description-digest-keyed staging, offline canonical resume validation, full host-and-snapshot claims, and idempotent workspace creation.

## Key Directories

| Path | Purpose |
| --- | --- |
| `src/phase1/` | Active contracts, bounded source/platform pipeline, backlog preparation, observer, ledger, audit, and evidence implementation. |
| `tests/` | Active Node contract, ledger/audit, and evidence regression tests. |
| `private/` | Git-ignored, owner-private run inputs and evidence. Never expose or commit its contents. |
| `skills/playwright-cli/` | Retained Playwright CLI 1.60.0 guidance and SHA-256 provenance. |
| `Archive/` | Reference-only resume-generation code and applicant evidence; not the active Phase 1 runtime. |

## Development Commands

```sh
npm test
node --test tests/ledger-audit.test.mjs
node --test tests/contract-profile.test.mjs
node --test tests/evidence.test.mjs
node --test tests/platforms.test.mjs
node --test tests/job-source-preparation.test.mjs
node --check src/phase1/observer.js
```

There is no build step, linter, formatter, coverage threshold, Playwright test config, Docker workflow, or CI pipeline. Do not invent commands from historical documents.

## Code Conventions & Common Patterns

- **Modules and naming:** Use ESM and two-space indentation. Files use `.mjs`, except the injected observer IIFE in `observer.js`. Prefer `camelCase` functions/values, `PascalCase` error/store classes, and `UPPER_SNAKE_CASE` fixed schemas and limits.
- **Validation:** Reject unknown keys and malformed, oversized, non-canonical, symlinked, or permission-unsafe inputs with stable error codes. Keep fixed run values (`headed`, `playwright_dom_v1`, `omp_browser`, `omp_agent`) fail-closed.
- **State:** Return cloned, recursively frozen ledger/audit values. Preserve stable IDs, observation chains, and current refs; never mutate caller-owned state.
- **Privacy:** Store raw applicant values only under `private/`. Public evidence structures use SHA-256 value digests, field IDs, sources, and outcomes—not profile values.
- **Browser boundary:** Observation code may read DOM/ARIA state only. OMP resolves answers and performs each interaction. Never add JavaScript form mutation, submit calls, or answer policy to the observer.
- **Supported platforms:** Claimable work is restricted to exact Greenhouse/Ashby routes or bounded employer routes on one explicitly verified ASCII DNS host. Persist and fence the exact application host with the canonical URL. Never add a permissive hostname heuristic or let an unclassified queued row pass through a legacy claim path.
- **Model boundary:** Deterministic code owns URL/payload identity, description extraction, queue order/binding, resume generation, control/option mechanics, retention, and audit. Model use is restricted to oversight/diagnosis and evidence-backed non-sensitive response content after memory/profile/resume resolution fails.
- **CMUX-TUI browser-pane binding:** Resolve the configured endpoint from `CMUX_MUX_CDP_URL`, then `browser.cdp_url` or configured discovery. Require a loopback `ws://` or `http://` URL with no credentials or fragment; reject `wss://`, non-loopback endpoints, the OS-default Chrome profile, and `browser.ephemeral` for durable runs. Bind one immutable `{ muxSessionId, targetId, cdpUrl, profileMode: "persistent", userDataDir? }`; one CMUX-TUI mux session owns one shared Chrome/CDP runtime and each active job owns exactly one target. Reject target/session mismatches and fence every request/result with `muxSessionId`, `targetId`, and `observationId`.
- **OMP browser attachment:** Attach the named OMP browser tab once with `xd://browser` payload `{"action":"open","name":"job-<job-id>","app":{"cdp_url":"<cdpUrl>","target":"<targetId>"}}`, then reuse that tab through `browser.run`; do not create a second attachment. Close only the job target after durable persistence, never the shared Chrome/runtime.
- **OMP browser mechanics:** Use the OMP `browser` tool (`xd://browser`) on the attached CMUX-TUI browser pane as the primary action driver. Queue acceptance is not proof that an action took effect: after every queued action or batch, re-ground with a fresh observer result and browser snapshot, and use that observation ID to prove retention before acting again. Text entry is `tab.fill(selector, exactText)`—the answer is the second positional argument, and `--value` or any other option token must never be prefixed to it. Inline `"aria-ref=eNN"` selectors are supported for `tab.fill` and `tab.click`; native `tab.select` and `tab.uploadFile` require a uniquely verified exact CSS selector derived from observed control attributes. These helpers are browser actions, not page-JavaScript mutation. Use pinned Playwright CLI mechanics only when the browser helper cannot operate the exact control.
- **OMP computer-use fallback:** After both the OMP `browser` tool and the documented pinned-CLI mechanic cannot operate a native browser/OS interaction, use the OMP `computer` tool on the same attached CMUX-TUI browser pane, especially for a still-open upload chooser. Re-ground from a fresh DOM/snapshot and fresh desktop screenshot before every computer-use action; retain CMUX-TUI browser-pane observation and evidence as the source of truth. Never use computer input to bypass authentication, access controls, or the final-submit boundary (gated by `prepareSubmission` audit).

- **CAPTCHA handling:** Detect and complete CAPTCHA challenges automatically using the OMP `browser` or `computer` tool on the attached CMUX-TUI browser pane. Re-ground with a fresh observer result and browser snapshot before and after each CAPTCHA interaction. CAPTCHA alone must never trigger user escalation, a `needs_user` outcome, or a blocked run. Record the CAPTCHA detection, resolution method, and outcome in the private ledger.
- **Application autonomy:** Infer and execute routine application decisions from the backlog, run contract, profile, resume, job context, and current page without requesting per-job or per-action permission. This includes selecting the next eligible job, opening a clearly identified application-entry control such as Apply/Easy Apply/Apply on company website, choosing non-final navigation, resolving aliases, formatting supported answers, handling optional fields, and retrying recoverable validation failures.
- **Submission recovery:** A rejected or non-accepted final action does not establish that a job is closed or ineligible. Keep its CMUX-TUI target and browser pane active, re-observe the page, diagnose the actual validation or required-field cause, resolve and retain it, then rerun the preparation loop. Only explicit live evidence that the posting is unavailable may produce a closed outcome.
- **Answer precedence:** `memory -> profile -> resume -> agent_inference -> user`. When memory/profile lack an exact alias, agent inference may generate a non-sensitive answer from resume facts plus job-description context. Every inferred answer requires a rationale digest and verified resume/job-description evidence digests and is marked separately in private ledger/evidence. Never infer identity, authorization, protected-class, salary/compensation, date, credential, or other sensitive personal, legal, financial, or medical facts. Ask only when a truthful answer cannot be derived or a third-party authentication/access-control interaction is required. Every application answer the user provides in the main session must be saved in owner-private answer memory, with its exact question/site alias when available, for reuse and future reference. Persist the verified answer before resuming the same browser session.
- **Filesystem safety:** Preserve regular-file/no-symlink checks, owner-only modes, bounded reads, canonical JSON, atomic no-replace publication, descriptor identity checks, and submission finalization.
- **Async:** Secure contract/profile I/O and the evidence factory are async at integration boundaries; the evidence store and ledger are deliberately synchronous and serialized.

## Important Files

| File | Why it matters |
| --- | --- |
| `TODO.md` | Authoritative phase contract, live exit gates, and submission boundary. |
| `package.json` | Node `>=22`, dependency-free ESM package, and the active test command. |
| `src/phase1/contract.mjs` | Fixed run schema, secure file loading, answer memory, and source precedence. |
| `src/phase1/profile.mjs` | Exact applicant-profile schema and alias lookups. |
| `src/phase1/observer.js` | Page-context normalized DOM observer. |
| `src/phase1/ledger.mjs` | Observation, diff, resolution, action, and retention contracts. |
| `src/phase1/audit.mjs` | Final completeness and submission audit. |
| `src/phase1/evidence.mjs` | Private evidence store and completion finalizer. |
| `src/phase1/platforms.mjs` | Exact supported ATS registry, payload extraction, and frozen deterministic action plans. |
| `src/phase1/job-source.mjs` | Supported ingestion, normalized bound snapshots, queue reads, and quarantine. |
| `src/phase1/preparation.mjs` | Recovery-first description → resume → exact claim → workspace coordinator. |
| `src/phase1/backlog-runner.mjs` | Atomic one-active-run lifecycle, leases, exact claim fencing, and persistence. |
| `migrations/005-platform-job-snapshots.sql` through `007-bind-application-host.sql` | Snapshot columns, exact three-platform enum, employer-host binding, active-run guards, and safe legacy quarantine. |
| `tests/*.test.mjs` | Observable regression contracts for active Phase 1 behavior. |
| `skills/playwright-cli/SOURCE.json` | Retained Playwright bundle version and recorded hashes. |
| `schemas/application-decision.schema.json` | Canonical strict per-field decision output contract. |
| `schemas/control-diagnosis.schema.json` | Canonical read-only control diagnosis output contract. |
| `schemas/repair-result.schema.json` | Canonical bounded repair result output contract. |

- **Typed delegation:** Before delegating a field, diagnosis, or repair task, load the complete matching schema object from `schemas/` and pass it as the strict per-task `outputSchema`. Path-only `$ref` metadata is not validation.

## Runtime/Tooling Preferences

- Use Node.js 22 or newer and npm. Do not run this package under Bun; production behavior is verified with Node.
- The package has no runtime dependencies. Do not install Puppeteer/Playwright into this package merely to drive the live browser; the retained skill guides observation, while the OMP `browser` tool on the attached CMUX-TUI browser pane owns ordinary actions, pinned CLI is control-specific fallback, and the OMP `computer` tool is native-UI fallback.
- Keep browser profiles, screenshots, resumes, answer memory, and evidence under owner-private, git-ignored paths.
- The secure file implementation assumes POSIX ownership, modes, descriptor flags, and directory `fsync` behavior.

## Testing & QA

- `npm test` runs the active `node:test` suite. Add tests only for observable contracts and plausible regressions; keep fixtures deterministic and private-data free.
- For observer or browser behavior, a headed live smoke run is required. Record chained observations/diffs, action refs, the field ledger, retry/validation recovery, uploaded-resume identity, final screenshot/audit, and submission evidence.
- A passing unit suite does not replace the live exit gate. Leave the browser open on the final review boundary and activate the final Submit control only after prepareSubmission authorizes it.
- Keep test output free of applicant values, resume text, screenshots, authentication state, and raw job payloads.
