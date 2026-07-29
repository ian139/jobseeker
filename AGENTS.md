# Repository Guidelines

## Authority and current posture

This repository contains three related capabilities:

1. Phase 1 prepares and submits one application with screenshot-first computer use.
2. Phase 2 generates the canonical one-page resume from source-backed evidence.
3. Phase 3 stores jobs in SQLite and runs one supervised application at a time.

`TODO.md` is the active scope and safety authority. `PROJECT_HANDOFF.md` records history and design evidence; it never overrides the active contract. A backlog authorization permits supervised operation, but it is not proof of an unobserved live application or submission. Do not mark a live gate complete without direct evidence from the current visible browser.

## Architecture and data flow

```text
job source + SQLite backlog
  -> persistent supervised OMP session claims one job
  -> verified job-specific resume generation
  -> private phase1-run-v2 contract and applicant evidence
  -> fresh desktop screenshot of the current visible headed browser
  -> Codex or Gemini visual observation
  -> immutable visual target ledger and answer resolution
  -> coordinate/keyboard computer action
  -> fresh screenshot and visual retention proof
  -> completeness audit and current final-candidate authorization
  -> audited two-phase final submission
  -> private evidence and durable SQLite outcome
  -> next backlog inspection
```

The visible screenshot is the only source of interface state. The selected Codex or Gemini agent interprets the screenshot into `phase1-visual-observation-v1`; OMP computer use performs the resulting coordinate or keyboard action. There is one action driver, `omp_computer`, and no alternate interaction stack.

## Run-v2 contract

Every application run uses these fixed values:

| Key | Required value |
| --- | --- |
| `schema` | `phase1-run-v2` |
| `browser_mode` | `headed` |
| `perception_driver` | `image_agent_v1` |
| `action_driver` | `omp_computer` |
| `model_provider` | `codex` or `gemini` |
| `submit_policy` | `omp_agent` |

The contract also carries `application_url`, `job_description_path`, `resume_upload_path`, `answer_memory_path`, `run_artifact_dir`, and at least one of `applicant_profile_path` or `source_resume_path`. Paths and source material remain owner-private. A run owns one job; a persistent backlog session may have only one active run.

The visual observation schema is `phase1-visual-observation-v1`. Its required top-level keys are `schema`, `observation_id`, `previous_observation_id`, `observed_at`, `surface`, `agent`, `targets`, and `blockers`. `surface` is `{ surface_id, url, title, screenshot_sha256, viewport: { width, height } }`. `agent` is `{ provider, model }`, with provider `codex` or `gemini`.

Each target is:

```text
{
  target_id, field_id, group_id, kind, label, description, bounds,
  visible, enabled, required, readonly, value_state, checked, selected,
  options, validation, file, candidate, confidence
}
```

`bounds` contains integer screenshot pixels `{ x, y, width, height }` with positive dimensions entirely inside the viewport. `value_state` is `blank`, `present`, `selected`, or `unknown`. A visual observation never contains raw applicant values. `validation` is `{ valid, message_present }` with nullable `valid`; `file` is null or `{ present, file_name }` with a nullable basename; and `candidate` is `{ class, reason }` where class is `field`, `non_final_navigation`, `final_candidate`, or `unknown`.

## Visual operating loop

- Capture a fresh screenshot of the current visible browser before every analysis and action decision. Bind the screenshot SHA-256, surface identity, viewport, and observation identity together.
- Ask the configured Codex or Gemini provider to analyze only that image. Reject observations with mismatched image identity, stale surface identity, invalid bounds, unknown provider, or raw applicant values.
- Merge accepted observations into an immutable ledger. Rename all target bindings to `target_id`, `targetId`, and `finalTargetId`; never introduce the retired binding names.
- Resolve answers in exact order: `memory -> profile -> resume -> agent_inference -> user`. Inference may transform source-backed, non-sensitive facts only. Never infer identity, authorization, protected-class, compensation, date, credential, medical, legal, or other sensitive facts. Ask one precise question when truth cannot otherwise be established, save the answer in private memory, and resume the same run.
- Use only the visual target ledger. Choose conservative batches only for independent routine fields; newly revealed or dependent fields, invalid/retry work, uploads, choices, widgets, navigation, blockers, and final submission are single actions.
- Use `captureView`, `analyzeView`, and `performAction`. The adapter permits only `click`, `type_text`, `press_key`, `scroll`, and `upload_file`. Click coordinates come from current `bounds`; text and key actions are deliberate computer input on the same visible surface.
- After every `performAction`, including an error or timeout, capture a fresh image and obtain a chained visual observation before diagnosing, retrying, or taking another action. Retention is image-based: the later target state must match and include explicit proof `{ action_id, visually_confirmed, file_name? }`.
- Keep observations, diffs, action journal, retry records, retention proofs, screenshot identities, and final evidence immutable. Store no raw applicant values in evidence; publish only bounded digests, IDs, basenames, outcomes, and screenshot identities.
- Final submission is permitted only for the current visual `final_candidate`. Run the completeness and retention audit, call `prepareSubmission(session, { finalTargetId })`, require authorization for that exact target, then call `beginFinalSubmit` before the click. Perform the authorized computer click, capture a fresh image, and call `completeFinalSubmit` exactly once with the observed outcome. A failed attempt requires a new observation and new authorization. Finalize only after one successful audited attempt and post-submit image evidence.

## Important areas

| Path | Purpose |
| --- | --- |
| `src/phase1/contract.mjs` | Fixed run-v2 settings, secure local inputs, answer memory, and source precedence. |
| `src/phase1/profile.mjs` | Exact private applicant-profile schema and aliases. |
| `src/phase1/planner.mjs` | Visual application planning and conservative batch selection. |
| `src/phase1/ledger.mjs` | Immutable visual observations, diffs, resolutions, actions, and retention. |
| `src/phase1/audit.mjs` | Completeness, retention, and final-target authorization. |
| `src/phase1/evidence.mjs` | Owner-private screenshot identities, action journal, and completion evidence. |
| `src/phase1/backlog-runner.mjs` | Atomic claims, one-active-run enforcement, leases, recovery, and terminal persistence. |
| `tests/` | Node contract and regression tests for the active implementation. |
| `private/` | Git-ignored owner-private inputs and evidence; never expose or commit contents. |
| `Archive/` | Reference-only resume-generation code and applicant evidence. |

## Code conventions and safety

- Use ESM, two-space indentation, Node.js 22 or newer, and the dependency-free package surface.
- Reject unknown keys and malformed, oversized, non-canonical, symlinked, or permission-unsafe inputs with stable error codes. Keep fixed run values fail-closed.
- Return cloned, recursively frozen ledger and audit values. Preserve stable IDs and observation chains; never mutate caller-owned state.
- Keep raw applicant values under owner-private paths only. Public evidence uses SHA-256 digests, field IDs, source classes, basenames, screenshot hashes, and outcomes.
- Preserve regular-file and no-symlink checks, owner-only modes, bounded reads, canonical JSON, atomic no-replace publication, descriptor identity checks, and directory finalization.
- Secure contract/profile I/O and evidence publication are asynchronous at integration boundaries; the ledger and evidence records remain serialized and immutable.
- Do not bypass authentication, assessments, anti-bot challenges, or access controls. A CAPTCHA or other external challenge is a live interaction boundary; record only its private outcome and keep the run active when it needs the user.
- A rejected final action is not a closed job. Re-observe the current image, repair the actual unresolved or invalid target, retain it, and return to the audit boundary. Only explicit live evidence that the posting is unavailable may produce `closed`.

## Runtime and verification

Use Node.js 22 or newer and npm. Keep browser profiles, screenshots, resumes, answer memory, and evidence under owner-private, git-ignored paths. Do not add a runtime dependency merely to operate the visible browser.

Run the existing Node test suite and focused contract tests when validating source changes. For a live application change, the required proof is a real headed session: fresh screenshot chain, visual ledger, action journal, retention proofs, upload identity, final audit, audited submission journal, and post-submit screenshot. A unit pass never replaces that live gate. Keep logs free of applicant values, resume text, authentication state, raw job payloads, and screenshots.

Before delegating a field, diagnosis, or repair task, load the complete matching schema object from `schemas/` and pass it as the strict per-task output schema. Path-only reference metadata is not validation.
