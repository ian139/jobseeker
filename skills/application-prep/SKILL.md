---
name: application-prep
description: Prepare and submit one complete job application with screenshot-first OMP computer use, private evidence, visual retention, and audited final submission.
---

# Application preparation

Use this skill for one authorized application URL and one owner-private Phase 1 run. The run contract is `phase1-run-v2`. Capture the current visible headed browser as a fresh screenshot, ask the configured Codex or Gemini agent to analyze that image, use only coordinate or keyboard computer actions, capture a fresh image after every mutation, and submit only through the audited two-phase final gate.

## Execution posture

Execute routine work directly. Reason only enough to choose the next truthful action, then use the next fresh image rather than speculation. Do not pause for progress narration or per-action confirmation. Ask the user only for a missing truthful sensitive fact or a required external access interaction.

The canonical lifecycle is:

```text
recover-or-claim one run
  -> validate private job, evidence, memory, and resume binding
  -> capture fresh current-browser screenshot
  -> obtain Codex/Gemini visual observation
  -> resolve targets and answers
  -> perform conservative computer action
  -> capture fresh image and verify visual retention
  -> repair or continue through non-final navigation
  -> audit the current final candidate
  -> prepare, begin, act, and complete final submission
  -> finalize private evidence and durable outcome
```

There is one action driver. Do not introduce an alternate interaction path, compatibility alias, or hidden state source.

### Free-text response style

Never use an em dash (`—`) in any generated, adapted, or filled application response. Rewrite with a comma, parentheses, colon, semicolon, or separate sentences before `type_text`. This applies to company-specific answers, answer-memory templates, explanations, and every other free-text response.

## Handoff-safe quick start

After a new session, handoff, or model change, read this skill and the active private run record before touching the visible browser. Recover an existing active run first. Do not reconstruct state from conversation history. Reuse the same visible headed browser surface when it remains bound to the run.

Call the run coordinator with `max_active_jobs = 1`. Validate the exact job, contract, private profile, answer memory, job snapshot, resume-upload identity, and artifact directory before any computer action. If recovery finds an active run, continue that run; claim a new job only when no active run exists.

## Fixed run contract

Load an owner-private JSON contract with these exact values:

```text
schema: phase1-run-v2
browser_mode: headed
perception_driver: image_agent_v1
action_driver: omp_computer
model_provider: codex | gemini
submit_policy: omp_agent
```

The contract also contains:

- `application_url`, one application target for this run;
- `job_description_path`, a read-only job snapshot and context only;
- optional `applicant_profile_path` and `source_resume_path`, with at least one required;
- `resume_upload_path`, the canonical private PDF to upload;
- `answer_memory_path`, the private appendable verified-answer record;
- `run_artifact_dir`, the private evidence destination.

Reject unknown run keys, legacy run identifiers, invalid providers, non-headed mode, missing evidence, non-canonical paths, symlinks, unsafe permissions, and unverified upload files. Keep raw applicant values in private inputs only.

## Visual observation contract

The only interface observation is a fresh screenshot of the current visible browser. The configured `model_provider` produces `phase1-visual-observation-v1` from that image. The accepted observation has exactly these top-level keys:

```text
schema
observation_id
previous_observation_id
observed_at
surface
agent
targets
blockers
```

`surface` must be:

```text
{ surface_id, url, title, screenshot_sha256, viewport: { width, height } }
```

`agent` must be `{ provider, model }`, where provider is `codex` or `gemini` and matches the run contract. Each target must contain:

```text
{
  target_id, field_id, group_id, kind, label, description, bounds,
  visible, enabled, required, readonly, value_state, checked, selected,
  options, validation, file, candidate, confidence
}
```

Validate every observation before accepting it:

- `screenshot_sha256` matches the fresh image passed to the provider;
- `surface_id` matches the current visible surface and viewport dimensions;
- `observation_id` is new and `previous_observation_id` chains to the latest accepted image;
- `bounds` are integer screenshot pixels with positive dimensions fully inside the viewport;
- `value_state` is `blank`, `present`, `selected`, or `unknown`;
- `validation` is `{ valid, message_present }` with nullable `valid`;
- `file` is null or `{ present, file_name }` with nullable basename;
- `candidate.class` is `field`, `non_final_navigation`, `final_candidate`, or `unknown`;
- no raw applicant value or hidden interface metadata appears in the observation.

The visual target ledger binds all current plans and evidence to `target_id`. Cross-layer target names are `target_id`, `targetId`, and `finalTargetId` only. Never carry a target binding across a fresh observation unless the ledger explicitly proves it is still current.

## Answer resolution and privacy

Resolve every target in this exact order:

1. verified answer memory;
2. applicant profile;
3. source resume;
4. source-backed, non-sensitive `agent_inference`;
5. one precise user question.

An inferred answer requires a private rationale digest plus verified resume and job-description evidence digests. Inference may transform supported facts but must never supply identity, authorization, protected-class, salary or compensation, date, credential, medical, legal, financial, or other sensitive personal facts. The job description is context, not applicant evidence.

When the user supplies a fact, append it to owner-private answer memory before resuming the same run. Never copy applicant values, answer text, resume text, job descriptions, screenshots, authentication state, or raw payloads into this skill, public logs, or public evidence. Public evidence contains only IDs, source classes, digests, screenshot identities, file basenames, and outcomes. Keep directories at `0700` and files at `0600`.

Do not bypass authentication, assessments, anti-bot challenges, or access controls. A CAPTCHA or external challenge is handled only as an ordinary visible interaction when it is safely presented; if user participation is required, keep the run active, record a private blocker, and resume after the user interaction.

## Screenshot and computer-use adapter

The adapter owns the current visible surface and exposes only:

```text
captureView()
analyzeView(image, contract)
performAction(action)
```

`performAction` accepts exactly these action kinds:

```text
click
type_text
press_key
scroll
upload_file
```

Use a fresh image for every plan. Use `click` coordinates inside the current target `bounds`. Use `type_text` only for the intended answer, without command words or option prefixes. Use `press_key` only for an explicit keyboard key. Use bounded `scroll` to reveal more of the current surface. Use `upload_file` only with the privately verified canonical upload path.

After every `performAction`, including a timeout, exception, partial result, or apparent no-op, call `captureView` and obtain a chained `phase1-visual-observation-v1` before any further action, diagnosis, retry, or navigation. Do not batch actions across an unobserved mutation. The new screenshot is the authority for what changed.

## Resolve, act, and retain loop

Repeat these steps until the run reaches a terminal outcome:

1. **Recover or claim.** Recover an existing active run, or atomically claim one job with `max_active_jobs = 1`. Bind the workspace, job, resume artifact, and contract.
2. **Preflight.** Load private profile and answer memory, canonicalize the upload PDF, and verify the read-only job snapshot. Stop for any integrity or privacy failure.
3. **Capture.** Capture a fresh screenshot of the current visible headed browser. Record its `screenshot_sha256`, `surface_id`, viewport, and capture time.
4. **Analyze.** Ask the configured Codex or Gemini provider for a bounded visual observation and validate it against the image and current surface.
5. **Ledger.** Accept the chained observation immutably, record its diff, and inventory every visible reachable target and blocker.
6. **Resolve.** Apply answer precedence before mutation. A target with a matching retained state needs no computer action, but it still needs fresh-image evidence in the next accepted observation.
7. **Plan.** Use the visual planner. A conservative batch may contain only independent routine fields with deterministic answers. Process newly revealed or dependent targets, invalid/retry work, uploads, choices, widgets, navigation, blockers, and final submission as single actions.
8. **Act.** Record an action intent against the current `observation_id` and `target_id`, call `performAction` once, and stop on any unexpected result.
9. **Re-observe.** Capture a fresh image immediately after the action, analyze it, update the immutable ledger, and inspect blockers and validation.
10. **Retain.** For every attempted target, require later visual state plus explicit proof `{ action_id, visually_confirmed, file_name? }`. For uploads, keep the verified basename and hash in private evidence; never record file contents.
11. **Repair.** Retry only the failed or stale target from the new observation. Record failed attempts and `retry_of` links without overwriting history. Add every newly revealed target to the ledger and resolve it before moving on.
12. **Navigate.** When all current field targets are deliberate, valid, retained, and unblocked, choose exactly one unambiguous current `non_final_navigation` candidate and perform it as a single action. Then return to capture.

A target is not complete merely because an action returned successfully. It must be visible in a later image, have the intended deliberate state, be valid or intentionally blank, and have all dependent targets resolved. A rejected final action is not a closed job; return to capture, diagnose the actual blocker, repair, retain, and audit again. Only explicit live evidence that the posting is unavailable may produce `closed`.

## Final audit and two-phase submission

Submission crosses a strict audited boundary:

1. Capture a fresh final screenshot and accept its chained observation.
2. Verify every reachable target is deliberate, valid, retained, and unblocked. Verify all retention proofs and the upload basename/hash.
3. Require exactly one visible current target whose `candidate.class` is `final_candidate`. Set `finalTargetId` to its current `target_id`.
4. Call `prepareSubmission(session, { finalTargetId })`. Continue only when it authorizes that exact current target and observation.
5. Call `beginFinalSubmit` before any submit action. Require the returned target binding to equal the authorized `finalTargetId`; record the attempt ID.
6. Call `performAction` with an authorized `click` at the current final target coordinates. Do not submit by any other mechanism.
7. Capture a fresh screenshot even when the action reports an error or timeout. Analyze it as a chained observation and record the screenshot identity.
8. Call `completeFinalSubmit(session, { attemptId, outcome, errorCode })` exactly once for the begun attempt. Never leave a begun attempt unresolved.
9. If outcome is not successful, keep the run active, repair from the fresh observation, and require a new final audit and new `prepareSubmission` authorization before retrying.
10. After exactly one successful submission, publish private post-submit image identity and completion evidence, then finalize the run. Durable `completed` requires validated canonical completion evidence and the paired attempt journal.

The final audit authorizes a target identity, not a generic action. A stale image, changed surface, changed bounds, changed target identity, blocker, or mismatched screenshot hash invalidates authorization.

## Run outcomes

Persist only one terminal outcome when the run truly cannot continue or has completed:

- **`completed`**: all reachable targets are deliberate, valid, and visually retained; the current final candidate was authorized; begin, click, and complete records pair exactly once; and private post-submit evidence validates.
- **`needs_user`**: a truthful required fact is absent and inference is prohibited, or a required user-owned interaction is pending. Keep the browser surface, workspace, ledger, and evidence active for same-run continuation.
- **`blocked`**: an authentication, non-CAPTCHA anti-bot, assessment, integrity, or access-control boundary requires the user; an unknown visible target cannot be safely resolved; or submission occurred outside the canonical protocol. Do not bypass it.
- **`closed`**: explicit live evidence says the posting is unavailable, such as a not-found, filled, closed, or unavailable message. Never use this for validation errors, retention failures, or rejected submission.
- **`failed`**: a bounded unrecoverable infrastructure or evidence-integrity failure. Routine validation debt, retryable actions, and non-accepted final actions are not `failed`.

## Evidence and handoff

Keep the action journal, observation chain, diffs, target ledger, retry links, retention proofs, screenshot hashes, upload identity, final authorization, paired submission attempt, completion evidence, and durable outcome immutable. Store all raw values only in configured owner-private inputs.

A handoff must state the claimed job, persisted outcome or active state, private workspace, latest accepted observation ID, current surface identity, ledger/evidence paths, upload identity and retention state, latest verification, blocker, and one next action. The receiving agent rereads this skill and the active run record, captures a fresh image, and never reconstructs a target from a stale narrative.
