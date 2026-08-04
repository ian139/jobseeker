# Historical Computer-Use Branch Notes (Non-Authoritative)

> **Historical record only.** This document describes an isolated visual-first experiment and its lessons. It is not an active implementation contract, does not authorize browser actions, and must not override `TODO.md` or `skills/application-prep/SKILL.md`. The current checkout is DOM-first.

## Active contract summary

The active application path owns one DOM-led field ledger and one serialized action stream. Its interaction hierarchy is:

```text
fresh DOM observation
  -> OMP browser helper
  -> pinned control-specific browser mechanic
  -> freshly grounded visual/computer interaction when both browser paths cannot operate the exact control
  -> fresh DOM observation
```

A screenshot and visual/computer input are bounded mechanic fallbacks on the same visible browser surface. They never create a second ledger, answer source, selector authority, submission decision, or visual adapter. Ground the fallback from a fresh DOM/browser snapshot and fresh screenshot immediately before acting, then obtain a fresh DOM observation before diagnosing, retrying, or continuing.

### Automatic CAPTCHA rule

CAPTCHA is handled automatically with the OMP `browser` or `computer` tool on the visible surface. Re-ground with fresh DOM and browser/screenshot state before and after the interaction, and record the detection, method, and outcome in the private ledger. CAPTCHA alone never produces user escalation, a `needs_user` outcome, or a blocked run.

### DOM-owned final submission boundary

The current accepted DOM observation supplies the exact `finalRef`. The coordinator must call `prepareSubmission({ finalRef })`, then `beginFinalSubmit`, and require the returned ref to equal that same DOM `finalRef`. Only the OMP `browser` helper or pinned control-specific mechanic may activate that exact ref. Visual/computer input must never cross the final-submit boundary. Re-observe after the attempt and complete it through the coordinator exactly once.

Answer resolution remains separate from UI mechanics and uses this fixed precedence:

```text
memory -> profile -> resume -> agent_inference -> user
```

`agent_inference` is evidence-backed and carries `inferenceRationaleDigest` plus `inferenceEvidenceDigests` with lowercase 64-hex `resumeSha256` and `jobDescriptionSha256` values. All other sources carry null inference metadata. It is not permitted for protected facts. Applicant values and private evidence remain in owner-private storage.

## Historical experiment status

The `computer-use` branch was an isolated replacement prototype. Its tests and fixtures were useful design evidence, but they were never proof of a live headed-browser run, field retention, or submission. Do not merge its visual-only interaction model into the current DOM-first path.

## Historical lessons retained

- A performed action is not a verified action. Any action, timeout, or error invalidates the prior observation for action planning; a fresh observation is required.
- Retention evidence must bind the later observation to the semantic action and expected state. Uploads also retain the visible file basename.
- Stale observations, stale targets, changed surfaces, and stale digests fail closed rather than being repaired heuristically.
- Final authorization must bind to the exact current DOM final ref, not to a generic submit label.
- Public evidence may contain bounded identifiers, digests, basenames, outcomes, and screenshot identities, but never applicant values, resume text, authentication state, raw job payloads, or screenshots.
- One run owns one job, one ledger, and one serialized action stream. Browser mechanics do not create duplicate state models.

## What the experiment ruled out

These are historical conclusions, not active APIs:

- Treating cached pixels, selectors, or stale observations as current state.
- Running a second browser-control stack beside the active OMP browser path.
- Treating tool completion as visual retention proof.
- Authorizing a generic submit action instead of the exact current DOM final ref.
- Inferring sensitive applicant facts.
- Treating fixtures, historical artifacts, or unit tests as proof of a live application or submission.
- Keeping retired aliases, compatibility shims, or duplicate interaction paths after a clean cutover.

## Verification lesson from the experiment

The historical tests covered malformed visual observations, target bounds, stale chains, retention, private-file defenses, and submission journaling. The active production gate is the current DOM observation, ledger, browser action hierarchy, retention chain, completeness audit, DOM `finalRef` authorization, and fresh post-action observation. This historical file supplies context only; it is never an active authority.
