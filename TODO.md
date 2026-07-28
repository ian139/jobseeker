# Jobs Automation Rebuild

This is the execution roadmap for the fresh implementation. `PROJECT_HANDOFF.md` is design history and evidence; this file defines the new build order and acceptance gates.

**Current phase:** Phase 3 backlog processing is active by explicit user authorization. Phase 2 generator proof and the durable Phase 3 lifecycle repair are complete, as verified by the lifecycle work record and current SQLite state. The remaining Phase 1 headed live-submission checklist entries stay evidence-gated and are not marked complete by this authorization. Process exactly one active job at a time through the persistent OMP session and do not mark unchecked live gates complete without direct evidence.

## How an OMP agent must use this file

1. For implementation or contract changes, read the active phase and consult `PROJECT_HANDOFF.md` only for unresolved history. For routine Phase 3 application startup, use the canonical application skill and active durable run record instead of rereading this roadmap or historical handoffs.
2. Turn every unchecked item in that phase into an execution plan. Do not reinterpret the phase into a smaller scaffold.
3. Work through the phase end to end. A fixture, unit test, mocked browser, or CLI success is not a substitute for the phase's live proof.
4. Keep OMP as the operator and reasoning loop. Deterministic libraries may expose narrow tools, but a custom CLI, daemon, RPC coordinator, or policy engine must not become the product loop.
5. Do not declare a phase complete until every exit-gate item for that phase has been directly observed, except where this roadmap explicitly records a user-authorized deferral or activation. The explicit Phase 3 activation permits supervised backlog operation while unchecked live gates remain completion evidence to obtain.
6. Preserve private applicant data locally. Do not place profile values, resume claims, field answers, or application screenshots in public logs or summaries.

## Product end state

```text
job source
  -> normalized SQLite backlog
  -> deterministic one-page LaTeX resume
  -> persistent OMP agent in a supervised CMUX-TUI workspace
  -> headed browser application session
  -> every user-facing application field resolved and verified
  -> OMP agent reviews and submits after the completeness audit passes
```

The system prepares complete applications and performs final submission after the completeness audit passes.

## Decisions that apply to every phase

- **One job per application run.** Phase 1 takes one agent-selected target from the run input or backlog. Starting preparation never requires separate per-job permission; the backlog itself does not exist until Phase 3.
- **No early handoff.** An error, an optional field, or a sensitive field is not a reason to declare the application ready. The agent continues observing, resolving, filling, and validating until the completion audit passes.
- **Every field means every user-facing application control.** Required and optional text fields, text areas, selects, comboboxes, radios, checkboxes, dates, uploads, and conditional controls must receive a deliberate, verified value or state. Hidden framework inputs, honeypots, disabled controls that cannot become active, and the final Submit control are not application questions; the final Submit control is handled only through `prepareSubmission`.
- **Sensitive fields are answerable, not automatically blocked.** If the local profile or answer memory contains a truthful stored answer, use it without asking again. If a personal, legal, demographic, financial, medical, work-authorization, or other factual answer is missing, ask the user one precise question, save the answer locally, and resume the same run. Never invent a personal fact.
- **Truth is the only content restriction.** At least one applicant-evidence input is required: a structured profile JSON, a source resume, or both. When both exist, stored profile values take precedence and the resume supplies supporting evidence. A job description supplies job context, never evidence that the applicant has a skill or fact.
- **Agent inference is the default.** When exact answer memory and profile aliases do not resolve a field, OMP may generate an answer from applicant facts in the source resume plus wording and requirements in the job description. `agent_inference` is an allowed answer source for every field shape, but each inferred answer must carry a rationale digest and the verified resume/job-description evidence digests and must be marked separately in the private ledger/evidence. Inference may transform supported facts but must never supply identity, dates, credentials, work authorization, protected-class answers, salary or compensation, or any other sensitive personal, legal, financial, or medical fact.
- **Final submission is automated by OMP after `prepareSubmission` authorizes it.** OMP identifies the final control, runs the completeness audit via `prepareSubmission`, durably begins the attempt with `beginFinalSubmit`, clicks the returned ref, and records the observed outcome with `completeFinalSubmit`. This is programmatic audit authorization and requires no human approval.
- **Direct browser operation replaces the old guarded applier.** Do not reactivate the archived Puppeteer/Python protocol, route allowlist, sensitive-field blocker, safety-policy stack, application RPC service, detached-browser recovery system, or handoff-on-uncertainty behavior. Reuse lessons, not that implementation.
- **Playwright observes; OMP acts.** The Playwright navigation skill and DOM observer describe the current page and verify retained state. The OMP `browser` tool on the attached CMUX-TUI browser pane is the primary action driver for clicking application-entry controls, filling, selecting, uploading, scrolling, and non-final navigation. Pinned Playwright CLI is control-specific fallback; the OMP `computer` tool is the last-resort native browser/OS fallback when available. Re-observe after every meaningful mutation.
- **Solve CAPTCHAs automatically.** Detect and complete CAPTCHA challenges using the OMP `browser` or `computer` tool on the attached CMUX-TUI browser pane. Re-ground with a fresh observer result and browser snapshot before and after each CAPTCHA interaction. CAPTCHA alone must never trigger user escalation, a `needs_user` outcome, or a blocked run. Record the CAPTCHA detection, resolution method, and outcome in the private ledger.
- **Live evidence must match the claim.** Browser success requires a real headed browser run on the selected application. Resume success requires a real compiled and inspected PDF. Pipeline success requires the persistent OMP loop to consume a real SQLite row.

## Assets to retain

These are the canonical resume baseline and must not be deleted while the new implementation is built:

- `Archive/resume_generator.py` — deterministic selector, renderer, compile/measure loop, and artifact publisher.
- `Archive/resume_generator_command.py` — existing invocation and job-selection adapter; useful as reference, not necessarily the final runtime surface.
- `Archive/resume_advisor.py` — optional advisory ranking limited to known evidence IDs.
- `Archive/resume/generator/SKILL.md` — governing resume policy; preserve its behavior and fingerprinting role.
- `Archive/resume/generator/profile.json` — structured applicant evidence catalog; private.
- `Archive/resume/generator/Resume.tex` — current LaTeX form and style.
- `Archive/resume/Main_Resume.pdf` — visual reference for the form; private.

`Archive/resume.py` and `Archive/resume_artifacts.py` belong to the incompatible second resume implementation. Do not activate or merge them into the canonical generator merely to connect the pipeline.

- `skills/playwright-cli/SKILL.md`, its complete `references/` directory, and `SOURCE.json` — exact Playwright CLI skill bundle retained from Playwright 1.60.0 in the sibling `../Jobs` project. `SOURCE.json` records the original package-relative path and SHA-256 for every retained source file. Read the skill before Phase 1 and keep the bundle together.

---

# Phase 1 — Complete and submit one application in a headed browser

## Goal

Given one application URL, a read-only job-description snapshot, and at least one applicant-evidence input (a local profile JSON, a source resume, or both), an OMP agent uses Playwright-backed DOM observation plus OMP `browser` tool actions on the attached CMUX-TUI browser pane to complete every user-facing field across the application flow, upload the configured resume, resolve validation errors and dynamically added controls, and stop only when the application is submitted.

The target is one concrete application, not a reusable ATS platform and not a backlog worker.

## Required inputs and parameters

The implementation may choose its file layout, but it must expose one machine-readable run contract containing these values:

| Parameter | Meaning |
|---|---|
| `application_url` | The single application page selected for this run from direct input or the backlog; no separate permission prompt is required. |
| `job_description_path` | Local read-only snapshot of the listing text, captured from the supplied job or provided by the user; it may guide wording but is never applicant evidence. |
| `applicant_profile_path` | Optional local JSON containing reusable facts, preferences, and stored answers; required only when no source resume is supplied. |
| `source_resume_path` | Optional current resume used as applicant evidence; required only when no profile JSON is supplied. |
| `resume_upload_path` | The exact PDF to upload during Phase 1. It may initially equal the source resume. |
| `answer_memory_path` | Local, appendable record of verified answers and field aliases learned during prior or current runs. |
| `run_artifact_dir` | Private location for observations, field ledger, screenshots, and completion evidence. |
| `browser_mode` | Headed and visible; the same session remains available for OMP review and submission. |
| `observer` | Playwright/DOM observer that emits a normalized snapshot without deciding answers. |
| `action_driver` | OMP `browser` tool on the attached CMUX-TUI browser pane, with pinned CLI and the OMP `computer` tool only as ordered fallbacks; never an autonomous Playwright form-filler. |
| `submit_policy` | Fixed to `omp_agent`. Submission is automated after audit. |

At least one of `applicant_profile_path` or `source_resume_path` is required; both are allowed. `resume_upload_path` remains separately required and may point to the same file as `source_resume_path`.

When a profile JSON is used, it must be able to represent at least contact details, address, links, education, employment, skills, availability, location/relocation preferences, compensation preferences, work authorization and sponsorship, voluntary demographic choices, reusable yes/no answers, and user-authored explanations. Values remain local and may be omitted until a page actually asks for them.

## Browser authority
### CMUX-TUI browser-pane attachment and lifecycle

The browser pane is an attachment to the CMUX-TUI runtime, not a second browser. One mux session owns one shared Chrome/CDP runtime, and each active job owns exactly one target in that runtime. The immutable binding is `{ muxSessionId, targetId, cdpUrl, profileMode: "persistent", userDataDir? }`; reject any target/session mismatch. `userDataDir` is optional but, when present, must be an owner-private session-scoped persistent profile. Never use the OS-default Chrome profile or `browser.ephemeral` for a durable application run.

Derive the configured CMUX-TUI CDP endpoint in this order: `CMUX_MUX_CDP_URL`, then `browser.cdp_url`, then configured discovery. Require a loopback `ws://` or `http://` endpoint with no credentials or fragment; reject `wss://` and every non-loopback endpoint. Do not implement a custom CDP client: OMP `browser` remains the action driver.

Attach exactly once with the OMP browser tool, selecting the job target on the shared runtime:

```json
{"action":"open","name":"job-<job-id>","app":{"cdp_url":"<cdpUrl>","target":"<targetId>"}}
```

This is the concrete `xd://browser` `browser.open` payload. After it succeeds, reuse the named tab through `browser.run`; do not call `browser.open` again for the same binding. Every request and result is fenced by `muxSessionId`, `targetId`, and `observationId`. Acceptance that an action was queued is not evidence that it took effect: after every queued action or batch, take a fresh OMP browser snapshot and observer result, accept that observation, and use it to prove retention before the next action. Close only the job target after the terminal outcome and evidence/database state are durably persisted; never close the shared Chrome runtime.

OMP acts by default, without per-job or per-action permission, to:

- inspect the page, DOM/ARIA state, labels, descriptions, options, validation messages, frames, and current values;
- click clearly identified application-entry controls, fill, select, scroll, check/uncheck, upload the configured resume, and activate non-final continuation controls;
- revise a field when page feedback shows that the prior value was rejected or semantically wrong;
- move through all non-final pages belonging to the same application;
- ask the user only for one missing factual answer or a required third-party access-control interaction, persist any factual answer, and continue the current browser session.

Bypassing an external access control remains the only permanent browser boundary for this phase.

## Field completion contract

A field is complete only when all of the following are true:

1. It has a deliberate answer or state derived through the resolution order below.
2. The page accepted the interaction and retained the value after blur/change.
3. The control is not currently reporting a validation error.
4. Any dependent fields revealed by that answer have been added to the field ledger and completed.
5. The answer source is recorded as profile, resume, prior stored answer, evidence-backed agent inference, or direct user answer.

For checkboxes, radios, and toggles, “complete” means an intentional verified state; it does not mean blindly enabling every option. For optional questions, blank is not a deliberate state unless the UI offers and the profile or answer memory supports “prefer not to answer,” “not applicable,” or an equivalent choice.

## Answer resolution order

Resolve each field in this order:

1. exact verified answer already stored for that question or site alias;
2. stored value or preference in the applicant profile;
3. unambiguous fact in the source resume;
4. evidence-backed agent inference generated from applicant facts in the source resume and wording or requirements in the job description;
5. one targeted user question only when the factual answer remains unknowable or inference is prohibited for that fact.

After step 5, persist the user's answer and immediately resume the loop. Do not convert an unknown answer into a generic handoff or mark the run complete.

## Required observe–infer–act loop

1. Open the supplied or agent-selected URL in a headed browser and wait for the page to stabilize.
2. Observe the current DOM. Inventory all visible/enabled user-facing controls, their labels, types, options, required state, current value, validation state, frame, and whether a control could be final submission.
3. Merge the observation into a field ledger. Preserve completed fields and add newly revealed fields.
4. Call `selectSafeApplicationBatch` for the latest observation. It may select up to three independent ordinary text controls; invalid/retry work, newly revealed or dependency-marked fields, uploads, custom widgets, choices/toggles, navigation, and final submission are always single-action units.
5. Resolve every planned answer using the precedence above before mutation. A multi-field batch proceeds only when every answer resolves deterministically from memory, profile, or resume. Agent inference, missing restricted facts, and user escalation return to single mode.
6. Map every planned field to one exact live control on the attached CMUX-TUI OMP `browser` pane and perform the interactions in order. Stop on the first non-success or unexpected browser state. Use pinned CLI only for a control-specific fallback and computer input only for a remaining native browser/OS interaction.
7. Publish all actually attempted routine fills atomically through `recordActionBatch`; publish a lone or hazardous action through `recordAction`. Evidence recording remains inside coordinator APIs.
8. Re-observe immediately after the batch or single action. Confirm every attempted value was retained, capture validation feedback, and detect DOM changes. Diagnose and retry only the failed/stale field.
9. When the current page has no unresolved fields, activate only the non-final Next/Continue control as a single action, then repeat from step 2.
10. At the final page, run a full completion audit across the ledger and current DOM. Stop only when every reachable application field is complete and the final Submit control is ready.
11. Leave the headed browser open, capture the completion report, and proceed through the audited submission protocol.

The loop must handle at least ordinary text inputs, textareas, native and custom selects, combobox/autocomplete widgets, radios, checkboxes, date/phone/address controls, file uploads, validation messages, conditional questions, and non-final multi-page navigation encountered by the chosen application.

## Minimal run evidence

Keep this intentionally smaller than the previous artifact/RPC system:

- application URL and start time;
- observation snapshots or normalized diffs sufficient to explain each action;
- field ledger with answer source and final verified state;
- exact uploaded-resume path and SHA-256;
- validation/retry notes;
- final screenshot and completion audit;
- explicit evidence of the submission action and outcome.

Do not build a database, event-sourcing layer, custom browser protocol, or distributed lifecycle for Phase 1.

## Work checklist

- [x] Read and verify the retained Playwright CLI skill bundle and its recorded hashes before browser work.
- [x] Define the local application-profile and answer-memory formats around the existing private profile/resume data.
- [x] Select one real application URL as the sole Phase 1 target.
- [x] Implement a normalized Playwright DOM observer for the controls actually present on that application.
- [x] Expose observation data to the OMP agent without embedding answer policy in the observer.
- [x] Drive all field interactions through the OMP `browser` tool on the attached CMUX-TUI browser pane, with pinned CLI and the OMP `computer` tool as ordered fallbacks.
- [x] Implement the field ledger and answer-source precedence.
- [x] Implement re-observation, DOM diffing, value-retention checks, and validation-error recovery.
- [x] Handle dynamically revealed fields and all non-final pages encountered by the selected application.
- [x] Upload the configured resume and verify the page retained the file.
- [x] Implement the completion auditor that includes optional and sensitive questions.
- [x] Submit the application and capture post-submit evidence.
- [x] Perform the live exit-gate run below.

## Explicitly out of scope

- scraping, TheirStack, feeds, job ranking, SQLite, or a backlog;
- resume tailoring or LaTeX generation;
- generalized Greenhouse/Lever adapters or broad cross-site compatibility;
- an application CLI as the primary operator;
- a persistent daemon, RPC service, or browser subprocess protocol;
- circumventing authentication, assessment, anti-bot, or access controls (completing presented CAPTCHA challenges through normal browser/computer interaction is required, not circumvention).

## Live exit gate

Phase 1 is complete only when one headed live run demonstrates all of these:

- [x] One real application is opened from an agent-selected or directly supplied URL without a separate permission prompt.
- [x] Every reachable user-facing field, including optional and evidence-supported sensitive fields, has a deliberate value/state.
- [x] Dynamic fields and non-final pages encountered in that application are completed.
- [x] The configured resume is uploaded and its identity is recorded.
- [x] No completed field has an outstanding validation error.
- [x] A final observation confirms values were retained.
- [ ] A concrete final Submit control ref appears in the latest observation, `prepareSubmission(session, { finalRef })` authorizes that exact ref, and `beginFinalSubmit(session)` durably records the attempt before OMP clicks its returned ref.
- [ ] `completeFinalSubmit` resolves every begun attempt; post-submit evidence records the screenshot and full paired submission-attempt journal with exactly one success.

The 2026-07-24 private run predates the automated-submission contract. Its
immutable report at
`private/phase1/vast-4696685006/strict-live/evidence/completion.json` proves the
33-field preparation audit only; it does not satisfy the current submission
exit gate. The current Node suite is the contract-level evidence until a new
headed live run captures post-submit evidence.

## OMP kickoff prompt

> Use `skills/application-prep/SKILL.md` as the canonical Phase 1 operating procedure. Start or recover the private run coordinator, attach once to the configured CMUX-TUI CDP endpoint with the `xd://browser` `browser.open` payload and exact target selection, then reuse `browser.run` on that target. Use `selectSafeApplicationBatch` to fill conservative independent routine text fields from one observation and retain them only after one fresh chained observation and browser snapshot. Keep newly revealed/dependency fields, invalid/retry work, uploads, widgets, choices, navigation, and submission single-action. Queue acceptance is not effect; only fresh observer/snapshot evidence proves retention. Consult pinned CLI guidance only after the exact browser helper fails. Submit only through `prepareSubmission -> beginFinalSubmit -> browser click -> fresh observation -> completeFinalSubmit -> finalizeRun`.

---

# Phase 2 — Finalize the canonical one-page LaTeX resume generator

## Goal

Given a job description, the structured facts about the applicant, and the current resume, generate the strongest truthful job-specific resume that preserves the current LaTeX form and visual style, compiles to exactly one page, fills the page densely without overflow, and remains reproducible from its inputs.

Phase 2 makes the existing deterministic generator active and reliable. It does not connect it to a backlog or browser.

The remaining Phase 1 live submission proof is explicitly deferred and does not block this phase. Phase 2 must still satisfy every generator checklist and live-generation item before Phase 3 begins.

## Required inputs and parameters

| Parameter | Meaning |
|---|---|
| `job_title`, `company`, `job_description` | Direct job snapshot; no SQLite dependency in this phase. |
| `profile_path` | Structured, source-backed applicant evidence catalog. |
| `source_resume_path` | Current resume used to reconcile facts and verify preserved content. |
| `template_path` | The retained `Resume.tex`, which defines the exact form and styling. |
| `skill_path` | The retained resume `SKILL.md`, loaded and fingerprinted on every generation. |
| `compiler` | Tectonic, `pdflatex`, or another explicitly identified compatible compiler. |
| `output_root` | Private destination for immutable generated artifacts. |

## Canonical implementation decisions

- Promote the deterministic baseline in `Archive/resume_generator.py`; do not replace it with the second JSON-to-PDF generator.
- Keep the structured profile as the source of candidate claims. Reconcile facts from the source resume into that catalog deliberately; do not treat freeform job text as candidate evidence.
- Preserve `Archive/resume/generator/Resume.tex` structure, typography, margins, section styling, and markers. Content selection may change; the resume's form must not drift.
- Preserve `Archive/resume/generator/SKILL.md` as the governing policy and include its digest in the generation identity. Any later policy change must be intentional and versioned.
- Keep deterministic ranking and selection as the baseline. Optional model/OMP advice may rank only known evidence IDs; generation must still succeed without it.
- Enforce one page by compiling, measuring the actual PDF, removing lower-value supported material in deterministic order when overfull, and adding the next-best supported material when sparse.
- Preserve source IDs/provenance for every selected claim. Do not invent or inflate a skill, employer, title, date, metric, impact, or responsibility.
- Publish the canonical five-file bundle: `resume.tex`, `resume.pdf`, `optimization.json`, `job_description.txt`, and `manifest.json`.
- Expose generation as a callable library/tool contract suitable for an OMP skill. A CLI may remain a diagnostic entry point, but it is not the future product loop.

## Work checklist

- [x] Promote the canonical generator, advisor, profile, template, and resume skill into one active implementation without merging the second generator.
- [x] Add a direct job-snapshot input path so Phase 2 accepts a job description without requiring the Phase 3 backlog.
- [x] Reconcile the current source resume and structured profile while preserving provenance and explicit open questions.
- [x] Verify deterministic role/requirement extraction, evidence ranking, and coherent section selection against a real target job description.
- [x] Preserve the exact LaTeX template form while inserting only selected source-backed content.
- [x] Verify bounded compiler invocation, PDF text extraction, and the deterministic compile–measure–trim/expand loop.
- [x] Verify fingerprints include the job, profile, source resume or its reconciled evidence identity, template, skill, compiler, and algorithm/advisor identity.
- [x] Publish and validate the immutable five-file artifact bundle.
- [x] Expose the generator through the retained resume skill/tool contract for later OMP use.
- [x] Perform the live exit-gate generation below.

## Explicitly out of scope

- SQLite backlog mutation or job claiming;
- browser automation, resume upload, or application state;
- a second rendering pipeline;
- model-authored unsupported claims;
- changing the current resume's visual form;
- application submission.

## Live exit gate

Phase 2 is complete only when a real generation demonstrates all of these:

- [x] A real job description and the local applicant evidence produce editable LaTeX and a compiled PDF.
- [x] The PDF has exactly one page and extractable text.
- [x] The output preserves the current resume's template, style, and section form.
- [x] The selected content is denser and job-relevant while every claim remains traceable to applicant evidence.
- [x] Re-running identical inputs reuses or reproduces the same validated fingerprinted result.
- [x] Changing a material input creates a distinct artifact identity.
- [x] Exactly the five canonical private artifacts are published and their hashes validate.

## OMP kickoff prompt

> Read `PROJECT_HANDOFF.md` resume-generation section and `TODO.md` Phase 2. Execute Phase 2 now; the remaining Phase 1 live submission proof is explicitly deferred and is not a prerequisite. Retain the deterministic generator, `Resume.tex`, structured profile, and resume `SKILL.md`; do not merge the incompatible second generator. Make generation accept a direct real job description, optimize only source-backed applicant facts, preserve the current resume form, and prove the result by compiling and inspecting an exactly one-page PDF. The OMP/model role is advisory over known evidence, not a source of claims. Finish with the validated five-file artifact bundle and direct evidence from a real generation.

---

# Phase 3 — Add the SQLite backlog and persistent OMP application loop

## Goal

Ingest normalized jobs from a selectable source into SQLite, keep a persistent OMP agent watching the backlog, atomically take one queued job at a time through the proven Phase 1 application workflow, and then insert the proven Phase 2 resume generator before browser filling so each application receives its verified job-specific resume.

Phase 3 has two ordered integration steps. Step A connects sourcing/backlog to Phase 1 with the existing resume. Step B adds Phase 2 resume generation. Do not combine both steps before Step A works.

**Active operating authority:** A persistent supervised OMP session may inspect, recover, claim, prepare, audit, and submit backlog applications one at a time. It must use the durable owner/lease lifecycle, job-specific preflight and resume binding, canonical Phase 1 evidence, and automated submission boundary. No separate per-job or per-action permission is required. Missing non-inferable facts and third-party access controls remain the only user-interaction boundaries.

## Source decision

The source is intentionally deferred until Phase 3. Implement one normalized adapter boundary, then choose the first real adapter based on available access:

1. TheirStack API, using the working concepts described in `PROJECT_HANDOFF.md`;
2. a user-owned scraper;
3. another explicitly selected source;
4. a manual/JSON seed adapter for controlled operation, not as proof that an external source works.

The source choice must not alter the backlog or application-worker contract.

## Minimal SQLite contract

Keep ingestion, resume artifacts, and application runs separate. Do not recreate the prior oversized schema.

### `jobs`

At minimum: stable ID, source, source job ID, canonical application URL, title, company, location, description, discovered/updated timestamps, queue status, and private raw source payload. Deduplicate by source identity and canonical URL.

### `application_runs`

At minimum: run ID, job ID, claim owner/time, lifecycle state, current field-ledger/evidence path, selected resume artifact identity, last progress time, and concise failure or targeted-input state. This table owns application lifecycle; do not overload the job row with browser details.

### `resume_artifacts`

At minimum: job ID, generator fingerprint, PDF path/hash, manifest path/hash, and creation time. The artifact row points to the canonical Phase 2 bundle; it is not a second generator lifecycle.

### Required lifecycle

```text
queued
  -> claimed
  -> applying
  -> needs_input      (run remains active; exact user fact or external challenge only)
  -> completed        (audit passed; OMP submission succeeded and is recorded)
     or skipped
```

Operational failures remain active/retryable until diagnosed. An error or sensitive field must not be relabeled `completed`. Claims and status transitions must be atomic so restarts do not duplicate an application run.

## Step A — Source, backlog, and Phase 1 worker

- [ ] Define one normalized job contract independent of the source adapter.
- [ ] Select and implement the first real source adapter; retain manual/JSON seeding only for controlled diagnosis.
- [ ] Create the minimal SQLite schema and migrations for jobs and application runs.
- [ ] Normalize URLs and deduplicate repeated source records without losing the private raw payload.
- [ ] Implement atomic claim/release/recovery semantics for one queued job at a time.
- [ ] Feed the claimed job's application URL, description, available applicant evidence (profile JSON, source resume, or both), upload resume, and run directory into the unchanged Phase 1 contract.
- [ ] Persist only enough progress for OMP to resume after interruption without marking incomplete fields as complete.
- [ ] Mark `completed` only after OMP performs an authorized successful `final_submit` and publishes post-submit evidence.
- [ ] Demonstrate the persistent OMP loop noticing and processing a newly queued real job.

## Step B — Insert the Phase 2 resume generator

- [ ] Before opening the browser, generate or reuse the canonical resume for the claimed job description.
- [ ] Validate the generator manifest, one-page PDF, and hashes before use.
- [ ] Record the canonical artifact identity in `resume_artifacts` and bind that identity to the application run.
- [ ] Stage one owned copy of the verified PDF for browser upload; do not create another resume format or renderer.
- [ ] Pass the staged job-specific PDF as `resume_upload_path` to the unchanged Phase 1 workflow.
- [ ] Verify from run evidence that the uploaded file hash matches the selected canonical artifact.
- [ ] Demonstrate the complete source-to-backlog-to-resume-to-browser flow on a real queued job.

## Persistent OMP and CMUX-TUI browser-pane operating model

OMP—not a custom CLI daemon—is the long-running orchestrator.

Recommended supervised layout:

- **Control pane:** the persistent OMP session, active phase contract, current job ID, and loop state.
- **Browser pane/tab:** the headed application target attached to the shared CMUX-TUI runtime and driven primarily through the OMP `browser` tool, with the OMP `computer` tool available only as native-UI fallback.
- **Inspection pane:** concise SQLite/job/run status and private artifact paths when diagnosis is needed.
- **Review workspace:** a completed pre-submit browser may be parked for OMP review without being mistaken for a failed or active fill loop.

Start with `max_active_jobs = 1`. Each CMUX-TUI mux session has one shared Chrome/CDP runtime and each active job has one target. Increase concurrency only after one-at-a-time recovery and target ownership are proven; separate mux sessions may isolate later concurrent jobs, but SQLite remains the source of durable claim ownership. Close only a target after persistence, never the shared runtime.

The persistent agent loop is:

```text
recover-or-claim
  -> validate the exact job and resume binding
  -> observe the headed application
  -> resolve and execute a safe batch of independent routine fields
  -> re-observe, retain, and repair
  -> audit
  -> begin/click/complete submission
  -> persist the canonical outcome
  -> inspect the backlog again
```

`recoverOrClaimBacklogRun` owns startup ordering: recover an existing active run first; only when none exists may it preflight and atomically claim one prepared job. `selectSafeApplicationBatch` may batch only conservative independent routine controls. Newly revealed/dependency controls, validation recovery, uploads, widgets, choices, navigation, and submission remain single-action units.

If no work exists, the OMP session waits and checks again using a configured interval or an explicit wake signal. Scripts may provide deterministic source, database, resume, or observation operations to the agent; they do not own the loop.

## Runtime parameters

| Parameter | Initial value/meaning |
|---|---|
| `db_path` | Local SQLite database. |
| `source_adapter` | Selected Phase 3 source implementation. |
| `source_credentials` | Private source-specific credentials, if required. |
| `applicant_profile_path` | Optional shared local application profile and verified stored answers; at least one application evidence input is required. |
| `source_resume_path` | Optional source resume used as application evidence; at least one application evidence input is required. |
| `resume_profile_path` | Canonical structured resume evidence. |
| `resume_template_path` | Retained `Resume.tex`. |
| `resume_skill_path` | Retained resume `SKILL.md`. |
| `playwright_skill_path` | Retained Playwright CLI skill bundle. |
| `artifact_root` | Private per-job resume and application evidence root. |
| `poll_interval_seconds` | Bounded idle wait before checking the queue again. |
| `max_active_jobs` | `1` until sequential end-to-end behavior is proven. |
| `submit_policy` | Always `omp_agent`. |

## Explicitly out of scope

- bypassing source fees, site access controls, or assessments;
- reviving the old RPC/OMP coordinator or custom browser protocol;
- multiple authoritative resume generators;
- broad concurrency before sequential recovery works;
- selecting every possible job source in the first implementation.

## Live exit gate

Phase 3 is complete only when the persistent supervised system demonstrates all of these:

- [ ] A real source inserts normalized, deduplicated jobs into SQLite.
- [ ] A running OMP loop notices a newly queued job without being manually invoked as a one-shot CLI command.
- [ ] The job is atomically claimed once and its durable state survives an intentional loop restart.
- [ ] The canonical generator creates or reuses a verified one-page resume for that exact job description.
- [ ] The application browser uploads the PDF whose hash matches the selected manifest.
- [ ] The Phase 1 loop completes and verifies every reachable application field before submission.
- [ ] `prepareSubmission(session, { finalRef })` authorizes the exact current final candidate ref, then `beginFinalSubmit(session)` durably records the attempt before the browser click.
- [ ] OMP clicks the returned ref, `completeFinalSubmit` records the observed outcome, canonical evidence is validated against that job, and SQLite derives `completed` plus the actual attempt count.
- [ ] The headed browser target remains available in the CMUX-TUI browser pane long enough for OMP to capture the submission outcome.
- [ ] OMP returns to backlog inspection after persistence succeeds.

## OMP kickoff prompt

> Use `skills/application-prep/SKILL.md` as the canonical operational procedure and the active durable run record as current state. Call `recoverOrClaimBacklogRun` with `max_active_jobs = 1`, then follow its safe-batch observe/act/re-observe loop through audited submission and durable persistence. Do not reread historical handoffs or expand a per-job checklist unless a concrete blocker or defect requires diagnosis.
