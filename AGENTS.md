# Repository Guidelines

## Project Overview

This repository implements the Phase 1 application contracts and the completed Phase 2 per-job resume generator proof. The remaining Phase 1 headed live-submission exit gate is explicitly deferred and must not be marked complete; Phase 3 backlog automation has not started. Follow `TODO.md` for the current authorized scope.

`TODO.md` is the scope and safety authority. `PROJECT_HANDOFF.md` is historical evidence only; verify its paths and claims against the active tree.

## Architecture & Data Flow

```text
run contract + private profile/memory + resume
  -> policy-free DOM observer
  -> OMP answer resolution and browser action
  -> immutable ledger + observation diff + retention check
  -> completion audit + private evidence
  -> OMP agent review and automated submission
```

- `src/phase1/contract.mjs` and `profile.mjs` validate fixed run settings, secure local inputs, applicant data, and append-only verified answer memory.
- `src/phase1/observer.js` is a self-contained page-context IIFE. It inventories controls, frames, blockers, values, validation, and final-action candidates; it must not choose answers or perform actions.
- `src/phase1/ledger.mjs` owns stable field identity, exact answer-source precedence, observation chains/diffs, deliberate grouped states, action references, and retention checks.
`src/phase1/audit.mjs` requires every active field to be deliberate, valid, retained, and current while rejecting unknown controls, blockers, stale refs, and incomplete submissions.
`src/phase1/evidence.mjs` publishes bounded, owner-private JSON/JSONL artifacts, file identities, an action journal, and a submission completion report.
OMP performs browser actions. Final submission is automated by OMP after the completeness audit passes.

## Key Directories

| Path | Purpose |
| --- | --- |
| `src/phase1/` | Active Phase 1 contracts, observer, ledger, audit, and evidence implementation. |
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
node --check src/phase1/observer.js
```

There is no build step, linter, formatter, coverage threshold, Playwright test config, Docker workflow, or CI pipeline. Do not invent commands from historical documents.

## Code Conventions & Common Patterns

- **Modules and naming:** Use ESM and two-space indentation. Files use `.mjs`, except the injected observer IIFE in `observer.js`. Prefer `camelCase` functions/values, `PascalCase` error/store classes, and `UPPER_SNAKE_CASE` fixed schemas and limits.
- **Validation:** Reject unknown keys and malformed, oversized, non-canonical, symlinked, or permission-unsafe inputs with stable error codes. Keep fixed run values (`headed`, `playwright_dom_v1`, `omp_browser`, `omp_agent`) fail-closed.
- **State:** Return cloned, recursively frozen ledger/audit values. Preserve stable IDs, observation chains, and current refs; never mutate caller-owned state.
- **Privacy:** Store raw applicant values only under `private/`. Public evidence structures use SHA-256 value digests, field IDs, sources, and outcomes—not profile values.
- **Browser boundary:** Observation code may read DOM/ARIA state only. OMP resolves answers and performs each interaction. Never add JavaScript form mutation, submit calls, CAPTCHA bypasses, or answer policy to the observer.
- **OMP browser mechanics:** Use the OMP `browser` tool (`xd://browser`) on the same visible cmux surface as the primary action driver. Re-ground with a fresh observer result and browser snapshot before acting. Text entry is `tab.fill(selector, exactText)`—the answer is the second positional argument, and `--value` or any other option token must never be prefixed to it. Inline `"aria-ref=eNN"` selectors are supported for `tab.fill` and `tab.click`; native `tab.select` and `tab.uploadFile` require a uniquely verified exact CSS selector derived from observed control attributes. These helpers are browser actions, not page-JavaScript mutation. Use pinned Playwright CLI mechanics only when the browser helper cannot operate the exact control.
- **OMP computer-use fallback:** After both OMP browser and the documented pinned-CLI mechanic cannot operate a native browser/OS interaction, use the `computer` tool on the same visible cmux application surface, especially for a still-open upload chooser. Re-ground from a fresh DOM/snapshot and fresh desktop screenshot before every desktop action; retain cmux browser observation and evidence as the source of truth. Never use desktop input to bypass authentication, CAPTCHA, access controls, or the final-submit boundary (gated by prepareSubmission audit).
- **Application autonomy:** Infer and execute routine application decisions from the backlog, run contract, profile, resume, job context, and current page without requesting per-job or per-action permission. This includes selecting the next eligible job, opening a clearly identified application-entry control such as Apply/Easy Apply/Apply on company website, choosing non-final navigation, resolving aliases, formatting supported answers, handling optional fields, and retrying recoverable validation failures.
- **Submission recovery:** A rejected or non-accepted final action does not establish that a job is closed or ineligible. Keep its browser surface and run active, re-observe the page, diagnose the actual validation or required-field cause, resolve and retain it, then rerun the preparation loop. Only explicit live evidence that the posting is unavailable may produce a closed outcome.
- **Answer precedence:** `memory -> profile -> resume -> agent_inference -> user`. When memory/profile lack an exact alias, agent inference may generate a non-sensitive answer from resume facts plus job-description context. Every inferred answer requires a rationale digest and verified resume/job-description evidence digests and is marked separately in private ledger/evidence. Never infer identity, authorization, protected-class, salary/compensation, date, credential, or other sensitive personal, legal, financial, or medical facts. Ask only when a truthful answer cannot be derived or a third-party authentication/CAPTCHA/access-control interaction is required. Every application answer the user provides in the main session must be saved in owner-private answer memory, with its exact question/site alias when available, for reuse and future reference. Persist the verified answer before resuming the same browser session.
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
| `tests/*.test.mjs` | Observable regression contracts for active Phase 1 behavior. |
| `skills/playwright-cli/SOURCE.json` | Retained Playwright bundle version and recorded hashes. |

## Runtime/Tooling Preferences

- Use Node.js 22 or newer and npm. Do not run this package under Bun; production behavior is verified with Node.
- The package has no runtime dependencies. Do not install Puppeteer/Playwright into this package merely to drive the live browser; the retained skill guides observation, while the OMP `browser` tool on the cmux surface owns ordinary actions, pinned CLI is control-specific fallback, and `computer` is native-UI fallback.
- Keep browser profiles, screenshots, resumes, answer memory, and evidence under owner-private, git-ignored paths.
- The secure file implementation assumes POSIX ownership, modes, descriptor flags, and directory `fsync` behavior.

## Testing & QA

- `npm test` runs the active `node:test` suite. Add tests only for observable contracts and plausible regressions; keep fixtures deterministic and private-data free.
- For observer or browser behavior, a headed live smoke run is required. Record chained observations/diffs, action refs, the field ledger, retry/validation recovery, uploaded-resume identity, final screenshot/audit, and submission evidence.
- A passing unit suite does not replace the live exit gate. Leave the browser open on the final review boundary and activate the final Submit control only after prepareSubmission authorizes it.
- Keep test output free of applicant values, resume text, screenshots, authentication state, and raw job payloads.
