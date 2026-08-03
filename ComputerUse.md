# Computer-Use Branch Takeaways

## Status

The `computer-use` branch is an isolated replacement prototype. It must not be merged into `main`. Its unit and contract tests validate narrow invariants, but they do not replace a real headed-browser run with a fresh screenshot chain and post-submit evidence.

## Core design lessons

### 1. Treat the screenshot as the interface-state boundary

The browser's visible pixels—not the DOM, accessibility tree, selectors, or a cached model of the page—are the source of truth.

- `captureView` produces a screenshot bound to a surface identity, URL, title, viewport, and SHA-256 digest.
- `analyzeView` accepts only that image and produces a `phase1-visual-observation-v1` observation.
- Every observation is bound to the screenshot digest and surface identity it describes.
- Target bounds must be integer pixel rectangles with positive dimensions entirely inside the viewport.
- Observations contain target state and metadata, never raw applicant values.

This makes stale or mismatched perception detectable instead of silently acting on an obsolete page.

### 2. Keep one serialized perception/action loop

`src/phase1/computer-use-adapter.mjs` narrows browser operation to:

1. capture a fresh view;
2. analyze that view with the configured Codex or Gemini image agent;
3. merge the accepted observation into the immutable ledger;
4. choose an action from current visual targets;
5. perform one of `click`, `type_text`, `press_key`, `scroll`, or `upload_file` through OMP computer use;
6. capture and analyze a new screenshot before diagnosing, retrying, or continuing.

There should be one action driver and no selector, DOM, Playwright, daemon, RPC, or alternate interaction fallback. Multiple interaction stacks create conflicting state models and make audit evidence ambiguous.

### 3. A performed action is not a verified action

Every action—including an error or timeout—invalidates the prior observation for further action. The system must re-observe before deciding what happened.

Retention is visual:

- the later observation must contain the expected target state;
- the proof is tied to the action ID and later observation;
- uploads additionally retain the visible file basename;
- changed or missing target IDs fail closed;
- mutation-pending state blocks audit and submission.

This avoids treating tool success, focus changes, or assumed page behavior as proof that a field was actually updated.

### 4. Make final submission a two-phase audited transaction

A final-looking button is not enough. Submission authorization must be tied to the exact current visual target.

- `prepareSubmission` checks completeness, retention, the current observation, and `finalTargetId`.
- `beginFinalSubmit` records the attempt and consumes the authorization before the click.
- The click is performed through the same computer-use adapter.
- A fresh post-click screenshot and chained observation are mandatory.
- `completeFinalSubmit` records the observed outcome exactly once and rejects same-observation completion.
- Finalization requires exactly one successful audited attempt plus post-submit image evidence.

A failed or ambiguous click requires re-observation and new authorization; it must not reuse the previous approval.

### 5. Separate truth resolution from UI operation

Answers resolve in a fixed order:

`memory -> profile -> resume -> agent_inference -> user`

Inference is limited to source-backed, non-sensitive transformations and must carry rationale and evidence digests. Identity, authorization, protected-class, compensation, date, credential, medical, legal, and other sensitive facts must never be inferred. When the available sources cannot establish the truth, ask one precise question, save the answer privately, and resume the same run.

The visual ledger stores field IDs, source classes, and digests—not applicant values.

### 6. Preserve immutable identity chains

Observations, diffs, actions, retries, retention proofs, submission attempts, and screenshot identities form an append-only audit chain.

Important identities remain stable and explicit:

- one run owns one job;
- one persistent backlog session has at most one active run;
- each observation points to its predecessor;
- actions bind to the current observation and `target_id`;
- final authorization binds to the current `finalTargetId`;
- stale leases, stale observations, stale targets, and stale digests are rejected rather than repaired heuristically.

Recovery should classify stale state and require re-observation, not invent continuity.

### 7. Privacy and local-file safety are part of correctness

Private inputs and evidence require regular-file, canonical-path, no-symlink, owner-only permission, bounded-read, and descriptor-identity checks. Evidence publication should be atomic and no-replace. Public records should contain only bounded metadata such as SHA-256 digests, IDs, basenames, outcomes, and screenshot identities.

Logs and schemas must not leak applicant values, resume text, authentication state, raw job payloads, or screenshots.

### 8. Keep orchestration narrow and resumable

The useful subsystem boundaries are:

- secure run contract and applicant sources;
- backlog claim/lease lifecycle;
- resume generation;
- visual observation validation;
- conservative action planning;
- immutable ledger and retention;
- completeness/final-target audit;
- private evidence publication.

External challenges such as CAPTCHA remain explicit user-interaction boundaries. They should leave the run active in `needs_user` rather than being bypassed or incorrectly marked closed.

## What the experiment ruled out

- Hidden DOM or selector state as a second source of truth.
- Playwright or another browser-control stack beside OMP computer use.
- A custom daemon/RPC orchestrator for the supervised loop.
- Reusing stale observations after any action, failure, or timeout.
- Treating tool completion as visual retention proof.
- Authorizing a generic submit action instead of the exact current final target.
- Inferring sensitive applicant facts.
- Treating fixtures, historical artifacts, or unit tests as proof of a live application or submission.
- Keeping retired aliases, compatibility shims, and duplicate interaction paths after a clean cutover.

## Verification lesson

The branch's tests are valuable for malformed observations, out-of-bounds targets, stale chains, answer precedence, retention, symlink defenses, backlog recovery, and submission journaling. The production gate is stricter: a real headed session must show a fresh screenshot chain, accepted visual observations, action journal, retention proofs, upload identity, completeness audit, target-specific submission authorization, exactly one completed submission attempt, and a post-submit screenshot.
