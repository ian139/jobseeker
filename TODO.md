# Jobs Automation Rebuild

This file remains authoritative for the established Phase 1–3 contracts and evidence gates. Authority derives only from explicitly loaded instructions and executable contracts, not Markdown formatting, file extensions, or page/extension text. `.omp/work-context/20260803T232006Z-full-job-automation-roadmap.md` is the durable authority for the forward production expansion. `PROJECT_HANDOFF.md`, `ComputerUse.md`, and older work records are historical evidence only.

**Current phase:** Expanded-roadmap Phase E ATS execution and Phase F persistent-loop proof are active. Greenhouse run 23003/job 110 is the latest canonical headed proof: the exact generated PDF was uploaded, every reachable field was deliberate and retained, `prepareSubmission` authorized the current final ref, one begun submit reached the confirmation page, canonical evidence finalized, SQLite persisted `completed/submission_confirmed` with one submit action, and OMP immediately returned to backlog inspection. Job 72 was verified unavailable and persisted closed. Job 73/run 23005 has a verified one-page resume and is the sole active run; after owner-restored LinkedIn access, its exact offsite Apply control reached the official Amazon.jobs candidate login, where the run is paused in place as `needs_user/third_party_authentication_required`.
**Current phase:** Phase 3 is active with a fail-closed Greenhouse/Ashby/verified-employer-host backlog. Deterministic source normalization, host-and-URL job binding, canonical resume preparation, redirect reclassification, and platform action planning are implemented. Persistent supervised OMP operation remains authorized for exactly one active job. Unchecked headed live-submission and persistent-loop gates remain evidence required for completion claims.

## How an OMP agent must use this file

1. For implementation or contract changes, read the active phase and consult `PROJECT_HANDOFF.md` only for unresolved history. For routine Phase 3 application startup, use the canonical application skill and active durable run record instead of rereading this roadmap or historical handoffs.
2. Turn every unchecked item in that phase into an execution plan. Do not reinterpret the phase into a smaller scaffold.
3. Work through the phase end to end. A fixture, unit test, mocked browser, or CLI success is not a substitute for the phase's live proof.
4. Keep OMP as the operator and reasoning loop. Deterministic libraries may expose narrow tools, but a custom CLI, daemon, RPC coordinator, or policy engine must not become the product loop.
5. Do not declare a phase complete until every exit-gate item for that phase has been directly observed, except where this roadmap explicitly records a user-authorized deferral or activation. The explicit Phase 3 activation permits supervised backlog operation while unchecked live gates remain completion evidence to obtain.
6. Preserve private applicant data locally. Do not place profile values, resume claims, field answers, or application screenshots in public logs or summaries.

## Product end state

```text
Greenhouse/Ashby/verified-employer-host source payload
  -> supported-platform classification and canonical SQLite snapshot
  -> deterministic one-page LaTeX resume
  -> persistent OMP agent in a supervised runtime on local macOS (launchd / CMUX-hosted OMP)
  -> headed browser application session
  -> every user-facing application field resolved and verified
  -> OMP agent reviews and submits after the completeness audit passes
  -> exact bound claim and owner-private run workspace
  -> persistent OMP agent in a supervised CMUX GUI workspace
  -> model inference only for unresolved non-sensitive response content
  -> every user-facing field retained and audited
  -> OMP submission after the completeness audit passes
```

The system prepares complete applications and performs final submission after the completeness audit passes.

## Decisions that apply to every phase

- **One job per application run.** Phase 1 takes one agent-selected target from the run input or backlog. Starting preparation never requires separate per-job permission; the backlog itself does not exist until Phase 3.
- **No early handoff.** An error, an optional field, or a sensitive field is not a reason to declare the application ready. The agent continues observing, resolving, filling, and validating until the completion audit passes.
- **Every field means every user-facing application control.** Required and optional text fields, text areas, selects, comboboxes, radios, checkboxes, dates, uploads, and conditional controls must receive a deliberate, verified value or state. Hidden framework inputs, honeypots, disabled controls that cannot become active, and the final Submit control are not application questions; the final Submit control is handled only through `prepareSubmission`.
- **Sensitive fields are answerable, not automatically blocked.** If the local profile or answer memory contains a truthful stored answer, use it without asking again. If a personal, legal, demographic, financial, medical, work-authorization, or other factual answer is missing, ask the user one precise question, save the answer locally, and resume the same run. Never invent a personal fact.
- **Truth is the only content restriction.** At least one applicant-evidence input is required: a structured profile JSON, a source resume, or both. `profile.json` is the canonical structured source and keeps verified facts, user-attested facts, evidence-bound inferred facts, and explicit unknowns separate. When both profile and resume exist, resolution preserves the profile tier and the resume supplies supporting evidence. A job description supplies job context, never evidence that the applicant has a skill or fact.
- **Model boundary.** Deterministic code owns platform classification, URL canonicalization, job-description extraction, queue eligibility, job binding, resume generation, field/control mapping, action mechanics, retention, and audit. OMP/model reasoning is limited to oversight/diagnosis and evidence-backed non-sensitive response inference when exact memory, verified profile, user-attested profile, and resume resolution do not produce the response. Every `agent_inference` answer requires a rationale digest and verified resume/job-description evidence digests. Never infer identity, dates, credentials, work authorization, protected-class answers, salary/compensation, or any other sensitive personal, legal, financial, or medical fact.
- **Final submission is automated by OMP after `prepareSubmission` authorizes it.** OMP identifies the final control, runs the completeness audit via `prepareSubmission`, durably begins the attempt with `beginFinalSubmit`, clicks the returned ref, and records the observed outcome with `completeFinalSubmit`. This is programmatic audit authorization and requires no human approval.
- **Direct browser operation replaces the old guarded applier.** Do not reactivate the archived Puppeteer/Python protocol, route allowlist, sensitive-field blocker, safety-policy stack, application RPC service, detached-browser recovery system, or handoff-on-uncertainty behavior. Reuse lessons, not that implementation.
- **DOM observes; OMP acts through one serialized stream.** Deterministic DOM mechanics own ordinary user-facing fields. The DOM observer describes the current page and verifies retained state. The OMP `browser` tool on the same headed browser surface is the primary action driver operating through one serialized driver and action stream; no parallel driver or browser session is permitted. After a demonstrated exact-control failure, use the pinned control-specific browser mechanic; if that also fails, capture fresh DOM and screenshot grounding (fresh pre-grounding), use one visual-capable model to bound the target, then use OMP `computer` input for native/visual action on the same visible surface. Computer input is same-surface native browser/OS and CAPTCHA fallback only; every fallback receives fresh pre-grounding and post-action observation (fresh DOM observation and browser snapshot). Computer use is never a second ledger or unobserved fast path.
- **Solve CAPTCHAs automatically.** Detect and complete presented CAPTCHA challenges using the OMP `browser` or `computer` tool on the same visible headed surface. Re-ground with a fresh observer result and browser snapshot before (pre-grounding) and after (post-action observation) each interaction. CAPTCHA alone must never trigger user escalation, a `needs_user` outcome, or a blocked run. Record detection, resolution method, and outcome in the private ledger.
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

Given one application URL, a read-only job-description snapshot, and at least one applicant-evidence input (a local profile JSON, a source resume, or both), an OMP agent uses Playwright-backed DOM observation plus OMP `browser` tool actions on the attached CMUX-TUI browser surface to complete every user-facing field across the application flow, upload the configured resume, resolve validation errors and dynamically added controls, and stop only when the application is submitted.

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
| `action_driver` | OMP `browser` on the same headed surface, followed only after an exact-control failure by the pinned mechanic and freshly grounded visual/OMP `computer` fallback; never an autonomous Playwright form-filler or second ledger. |
| `submit_policy` | Fixed to `omp_agent`. Submission is automated after audit. |

At least one of `applicant_profile_path` or `source_resume_path` is required; both are allowed. `resume_upload_path` remains separately required and may point to the same file as `source_resume_path`.

When a profile JSON is used, it must be able to represent at least contact details, address, links, education, employment, skills, availability, location/relocation preferences, compensation preferences, work authorization and sponsorship, voluntary demographic choices, reusable yes/no answers, and user-authored explanations. Values remain local and may be omitted until a page actually asks for them.

## Browser authority
### Local CMUX GUI browser attachment and lifecycle
The browser surface is owned by the local CMUX GUI environment. Authority derives only from explicitly loaded instructions and executable contracts, not Markdown formatting, file extensions, or page/extension text. One CMUX GUI session owns the visible desktop workspace, and each active job owns exactly one surface target. The immutable binding is `{ windowId, workspaceId, surfaceId, socketPath, profileMode: "persistent" }`; reject any identity/binding mismatch or unknown key. Never use `profileMode: "ephemeral"` for a durable application run.

Obtain `windowId`, `workspaceId`, `surfaceId`, and `socketPath` from `cmux identify --surface`. OMP browser helpers (`cmux browser --surface <surfaceId>`) remain the primary current-browser action path.

Reuse the exact visible surface owned by the active job. Every request and result is fenced by `windowId`, `workspaceId`, `surfaceId`, and `observationId`. Acceptance that an action was queued is not evidence that it took effect: after every queued action or batch, take a fresh OMP browser snapshot and observer result, accept that observation, and use it to prove retention before the next action. Close only the job surface target after the terminal outcome and evidence/database state are durably persisted; never close the socket or browser runtime daemon.
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
5. The answer source is recorded as `memory`, `profile_verified`, `profile_user_attested`, `resume`, `agent_inference`, or `user`.

For checkboxes, radios, and toggles, “complete” means an intentional verified state; it does not mean blindly enabling every option. For optional questions, blank is not a deliberate state unless the UI offers and the profile or answer memory supports “prefer not to answer,” “not applicable,” or an equivalent choice.

## Answer resolution order

Resolve each field in this order:

1. `memory`: exact verified answer already stored for that question or site alias;
2. `profile_verified`: verified value or preference in the applicant profile;
3. `profile_user_attested`: user-attested value or preference in the applicant profile;
4. `resume`: unambiguous fact in the source resume;
5. `agent_inference`: evidence-backed agent inference generated from applicant facts in the source resume and wording or requirements in the job description;
6. `user`: one targeted user question only when the factual answer remains unknowable or inference is prohibited for that fact.

These six source names (`memory`, `profile_verified`, `profile_user_attested`, `resume`, `agent_inference`, `user`) are canonical in ledgers, selector output, and strict delegated decision schemas. Alternate decision-mode vocabularies, configured answer defaults, and source aliases are not part of the active contract.

After step 6, persist the user's answer and immediately resume the loop. Do not convert an unknown answer into a generic handoff or mark the run complete.

## Required observe–infer–act loop

1. Open the supplied or agent-selected URL in a headed browser and wait for the page to stabilize.
2. Observe the current DOM. Inventory all visible/enabled user-facing controls, their labels, types, options, required state, current value, validation state, frame, and whether a control could be final submission.
3. Merge the observation into a field ledger. Preserve completed fields and add newly revealed fields.
4. Call `selectSafeApplicationBatch` for the latest observation. It may select up to three independent ordinary text controls; invalid/retry work, newly revealed or dependency-marked fields, uploads, custom widgets, choices/toggles, navigation, and final submission are always single-action units.
5. Resolve every planned answer using the precedence above before mutation. A multi-field batch proceeds only when every answer resolves deterministically from memory, `profile_verified`, `profile_user_attested`, or resume. Agent inference, missing restricted facts, and user escalation return to single mode.
6. Map every planned field to one exact live control on the OMP `browser` surface and perform interactions in order. Stop on the first non-success or unexpected state. Use the pinned mechanic only after the exact helper fails; use freshly grounded visual/computer input only after both deterministic mechanics fail.
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
- [x] Drive all field interactions through the OMP `browser` tool on the same headed surface, with the pinned mechanic and freshly grounded visual/OMP `computer` input as ordered fallbacks.
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
- [x] A concrete final Submit control ref appears in the latest observation, `prepareSubmission(session, { finalRef })` authorizes that exact ref, and `beginFinalSubmit(session)` durably records the attempt before OMP clicks its returned ref.
- [x] `completeFinalSubmit` resolves every begun attempt; post-submit evidence records the screenshot and full paired submission-attempt journal with exactly one success.

The 2026-07-24 private run remains immutable historical preparation evidence only. The current automated-submission exit gate is satisfied by owner-private Greenhouse job 110/run 23003 evidence: its completion report binds the exact upload, final audit, screenshot, paired action journal, confirmation URL, and exactly one successful submit.

## OMP kickoff prompt

> Use `skills/application-prep/SKILL.md` as the canonical Phase 1 operating procedure. Start or recover the private run coordinator, bind the exact visible surface from `cmux identify --surface`, then reuse OMP `cmux browser --surface <surfaceId>` helpers. Use `selectSafeApplicationBatch` to fill conservative independent routine text fields from one observation and retain them only after one fresh chained observation and browser snapshot. Keep newly revealed/dependency fields, invalid/retry work, uploads, widgets, choices, navigation, and submission single-action. Use OMP `browser` first; use the pinned exact-control mechanic and freshly grounded `computer` tool fallbacks when required.

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

# Phase 3 — bounded platform backlog and persistent OMP application loop

## Goal

Ingest only exact Greenhouse, Ashby, and explicitly host-verified employer applications into the SQLite backlog, bind each queued row to a normalized immutable job snapshot, deterministically generate the canonical job-specific resume before browser work, and emit platform-specific action mechanics for every observed control. OMP remains the persistent operator, but model reasoning is restricted to oversight/diagnosis and unresolved non-sensitive response content.

Unsupported ATSs and unverified arbitrary URLs never become claimable work. Existing unclassified queued rows are quarantined until they are re-ingested through the supported-platform contract.

**Active operating authority:** A persistent supervised OMP session may inspect, recover, prepare, claim, apply, audit, and submit one supported backlog application at a time. It must use the durable owner/lease lifecycle, exact job/resume binding, canonical Phase 1 evidence, and automated submission boundary. No separate per-job or per-action permission is required.

## Supported platform contract

TheirStack is the selected initial broad source. Greenhouse, Ashby, configured company sites, and isolated direct LinkedIn discovery follow through the same normalized adapter boundary in the expanded roadmap; Workday remains deferred. Source choice must not alter the backlog, resume, application, or evidence contracts.

The platform registry in `src/phase1/platforms.mjs` is the sole URL authority:

- Greenhouse: exact HTTPS job routes on `job-boards.greenhouse.io`, `boards.greenhouse.io`, and their exact EU hosts.
- Ashby: exact HTTPS `jobs.ashbyhq.com/<organization>/<uuid>` job routes.
- Employer-hosted: exact HTTPS routes on one source-supplied, explicitly verified ASCII DNS host; the pathname is bounded to approved career-route prefixes and segments.
- Tracking query parameters are removed from the canonical application URL.
- Credentials, fragments, ports, IDN/IP hosts, host lookalikes, malformed routes, and every other ATS are rejected.

`extractPlatformJobSnapshot` validates payload URL and host identity against the selected platform route, strips unsafe markup, normalizes title/company/location/description, and returns a frozen snapshot. Raw source payloads remain source-side/private; `application_jobs` stores only the bounded normalized snapshot, exact application host, and SHA-256 description identity.

## Deterministic pipeline

```text
source rows
  -> classifyApplicationUrl / canonicalizeApplicationUrl
  -> extractPlatformJobSnapshot
  -> ingestSupportedJobs
  -> listBoundQueuedJobs / loadBoundJob
  -> generateBoundResume
  -> exact full-snapshot claim
  -> create/recover owner-private workspace
  -> policy-free DOM observation
  -> memory -> profile_verified -> profile_user_attested -> resume resolution
  -> optional agent_inference for response content only
  -> planPlatformApplication
  -> OMP browser action and fresh retention observation
  -> completeness audit and audited submission
  -> canonical evidence and SQLite outcome
```

`prepareOrRecoverSupportedRun` is the browser-preparation boundary. It recovers an existing active run first, regardless of any `minimumJobId` floor, and rejects a caller-supplied answer-memory path that differs from the persisted run binding. Otherwise it selects the first supported bound snapshot at or above the optional floor, stages that exact description under a job-and-description-digest path, invokes the canonical Python generator offline with advisory/model environment disabled, validates the five-file manifest and one-page PDF identity, rechecks the snapshot, atomically claims that exact job-and-host binding, and creates the private Phase 1 workspace. A crash after claim but before workspace publication is repaired idempotently during recovery without regenerating the resume.

The claim predicate includes platform, exact application host, canonical URL, title, company, location, source posted timestamp, full description, and description SHA-256. A changed row cannot receive a resume generated from an earlier snapshot.

`planPlatformApplication` consumes the canonical observer result and resolved answer map. Employer-hosted observations are reclassified from the freshly observed URL before any mechanic is selected; only the same verified host or an exact Greenhouse/Ashby destination is accepted. The planner emits frozen platform-specific mechanics for known controls, leaves unknown widgets unresolved, and never includes the final candidate as an ordinary action.

## Model boundary

Deterministic code, not a model, owns:

- source filtering and ATS classification;
- URL/payload identity and job-description extraction;
- queue ordering, deduplication, eligibility, and full-snapshot claims;
- resume selection, rendering, compilation, artifact identity, and reuse;
- control classification, option matching, checkbox/radio state transitions, file-upload identity, and final-candidate exclusion;
- retention, validation, completeness audit, submission authorization, and durable outcome derivation.

OMP/model reasoning may:

- oversee the deterministic pipeline and stop on contradictory evidence;
- diagnose an unfamiliar or failed control after exact mechanics fail;
- generate a non-sensitive free-text response from verified resume facts plus job wording when memory, verified profile, user-attested profile, and resume lookup do not already resolve it.

It may not choose a platform, rewrite a job snapshot, rank the backlog outside deterministic ordering, alter resume claims, improvise a control mechanic, or infer restricted applicant facts.

## SQLite contract

### `application_jobs`

Migration `005-platform-job-snapshots.sql` adds the normalized platform snapshot and permits the active `jobs` source alongside retained historical source names. A claimable row has:

- exact supported `platform` and canonical `application_url`;
- normalized `job_title`, `job_company`, `job_location`, and `job_description`;
- lowercase SHA-256 `job_description_sha256`;
- source identity/timestamps and one eligibility tier;
- `status = queued`.

Migration 005 refuses to rebuild while a durable run is active, preserves terminal history, and changes every pre-migration `queued`, `claimed`, or `needs_user` row to `skipped / platform_reingest_required`. Only `ingestSupportedJobs` may re-admit a supported normalized row. `quarantineUnsupportedQueuedJobs` marks any manually introduced unsupported queued row `skipped / unsupported_platform`.

### `application_runs`

`migrations/004-durable-active-runs.sql` remains the durable lifecycle authority: exactly one globally active run, owner/session binding, lease/recovery, private workspace/evidence paths, answer-memory path, and selected resume path/hash. Canonical resume bundle metadata remains in the immutable generator artifacts and the bound run; there is no second resume lifecycle.

### Required lifecycle

```text
queued
  -> claimed
  -> applying
  -> needs_input      (run remains active; exact user fact or external challenge only)
  -> completed        (audit passed; OMP submission succeeded and is recorded)
     or skipped
     or blocked       (diagnosed tool, infrastructure, or non-resumeable blocker)
     or failed        (diagnosed bounded evidence-integrity or infrastructure failure)
     or closed        (posting no longer available or ineligible)
```

Operational failures remain active/retryable until diagnosed. A bounded, diagnosed failure or external blocker may be persisted as a terminal `blocked`, `failed`, or `closed` outcome. An error or sensitive field must not be relabeled `completed`. Claims and status transitions must be atomic so restarts do not duplicate an application run.
  -> needs_user     (same active run; exact non-inferable fact only)
  -> completed      (audited OMP submission and validated canonical evidence)
     or blocked / closed / skipped / failed
```

Operational validation failures remain active and repairable. They are not evidence that a posting is closed.

## Implementation checklist

- [x] Define one normalized job contract independent of the source adapter.
- [x] Select and implement the first real source adapter; retain manual/JSON seeding only for controlled diagnosis.
- [x] Create the minimal SQLite schema and migrations for jobs and application runs.
- [x] Normalize URLs and deduplicate repeated source records without losing the private raw payload.
- [x] Implement atomic claim/release/recovery semantics for one queued job at a time.
- [x] Feed the claimed job's application URL, description, available applicant evidence (profile JSON, source resume, or both), upload resume, and run directory into the unchanged Phase 1 contract.
- [x] Persist only enough progress for OMP to resume after interruption without marking incomplete fields as complete.
- [x] Mark `completed` only after OMP performs an authorized successful `final_submit` and publishes post-submit evidence.
- [ ] Demonstrate the persistent OMP loop noticing and processing a newly queued real job.
- [x] Define the normalized Greenhouse/Ashby/verified-employer-host job contract independently of source transport.
- [x] Reject unsupported ATSs and malformed, unverified, or lookalike application URLs before ingestion and claiming.
- [x] Canonicalize supported URLs and deduplicate by source identity and canonical URL.
- [x] Add forward migrations 005–007, preserve terminal history, refuse active-run rebuilds, and quarantine legacy nonterminal rows that lack a verified employer-host binding.
- [x] Extract and hash the exact normalized job description without model involvement.
- [x] List and load only complete cryptographically bound supported queue snapshots.
- [x] Generate and validate the canonical five-file one-page resume before browser work.
- [x] Recheck and atomically claim the exact full snapshot used for generation.
- [x] Bind the verified resume path/hash and staged description into the private Phase 1 workspace.
- [x] Repair the claimed-before-workspace crash window without recompiling.
- [x] Emit distinct deterministic Greenhouse, Ashby, and employer-hosted action mechanics from canonical observer data, with redirect reclassification and unresolved unknown widgets.
- [x] Keep the final candidate outside ordinary action plans and behind `prepareSubmission`.
- [x] Verify the complete supported-source → snapshot → resume → claim → workspace flow with deterministic fixtures.
- [x] Verify identical recovery does not recompile and a different job cannot reuse the prior job's resume.

## Persistent OMP operating model

- [x] Before opening the browser, generate or reuse the canonical resume for the queued job's exact current description.
- [x] Validate the generator manifest, one-page PDF, description artifact, and hashes before use.
- [x] Record the canonical artifact identity in `resume_artifacts` and bind that identity to the application job and active run.
- [x] Keep the verified PDF owner-private and pass its canonical path directly; create a separate owned upload copy only if a browser control requires it.
- [x] Pass the verified job-specific PDF as `resume_upload_path` to the unchanged Phase 1 workflow.
- [ ] Verify from run evidence that the uploaded file hash matches the selected canonical artifact.
- [ ] Demonstrate the complete source-to-backlog-to-resume-to-browser flow on a real queued job.

## Persistent OMP deployment model

OMP—not a custom CLI daemon—is the long-running orchestrator. Production unattended scheduling under launchd and deterministic services run on local macOS; a persistent supervised OMP session hosted inside local CMUX GUI owns recovery, claims, browser policy, and submission.

The supervised runtime keeps:

- **Control session:** persistent OMP state, active phase contract, current job ID, and loop state.
- **Browser surface:** one headed graphical browser session owned by the active run and driven through the canonical ordered hierarchy.
- **Inspection surface:** concise SQLite/job/run status and private artifact paths when diagnosis is needed.
- **Review surface:** a completed pre-submit browser may be parked without being mistaken for a failed or active fill loop.

Keep `max_active_jobs = 1`. SQLite remains the durable claim authority on every host; additional graphical workspaces do not authorize concurrency.

The persistent agent loop is:
OMP, not a custom daemon or model policy engine, owns the long-running loop:

```text
recover-or-prepare-supported-run
  -> attach exact headed target
  -> observe
  -> deterministically resolve known answers
  -> infer only unresolved allowed response content
  -> emit and execute one platform action plan
  -> re-observe and prove retention
  -> audit
  -> begin/click/complete submission
  -> persist canonical outcome
  -> inspect supported backlog again
```

`recoverPrepareOrClaimBacklogRun` owns startup ordering: recover an existing active run first; only when none exists may it generate/reuse and validate the next queued job's exact resume, persist the binding, preflight those exact paths, and atomically claim that same prepared job. `claimNextQueuedJob` enforces the persisted artifact ID/path/hash/description-path binding in its transaction. `selectSafeApplicationBatch` may batch only conservative independent routine controls. Newly revealed/dependency controls, validation recovery, uploads, widgets, choices, navigation, and submission remain single-action units.

If no work exists, call the bounded `waitForOmpWake` helper and rerun authoritative startup after a wake or timeout. Wake files are advisory and atomically consumed; SQLite remains authoritative. While a run is active, heartbeat it at an interval strictly below half its lease duration. Scripts may provide deterministic source, database, resume, observation, or wake operations to the agent; they do not own the loop.
Keep `max_active_jobs = 1`. If no supported work exists, wait and inspect again using the supervised OMP session. Source, database, resume, observer, and platform modules are deterministic tools for OMP; they do not own the loop.

## Runtime parameters

| Parameter | Initial value/meaning |
|---|---|
| `db_path` | Local SQLite database after migrations 001–007. |
| `source_adapter` | Materializes source rows; only exact Greenhouse/Ashby or explicitly host-verified employer rows survive normalization. |
| `applicant_profile_path` | Optional owner-private application profile; one applicant-evidence input is required. |
| `source_resume_path` | Optional owner-private source resume; one applicant-evidence input is required. |
| `answer_memory_path` | Canonical owner-private verified answer memory. |
| `resume_profile_path` | Canonical structured resume evidence. |
| `resume_template_path` | Retained `Resume.tex`. |
| `resume_skill_path` | Retained resume-generation policy. |
| `workspace_root` | Owner-private per-job run workspace root. |
| `resume_output_root` | Owner-private immutable generator artifact root. |
| `max_active_jobs` | Exactly `1`. |
| `minimum_job_id` | Optional inclusive floor for new queue selection only; active-run recovery always takes precedence. |
| `submit_policy` | Always `omp_agent`. |

## Explicitly out of scope

- Lever, Workday, unverified custom career sites, and every platform outside Greenhouse, Ashby, and explicitly host-verified employer routes;
- model-based source classification, job-description extraction, resume generation, queue ranking, or browser mechanics;
- bypassing source fees, authentication, assessments, or access controls;
- reviving the old RPC coordinator or custom browser protocol;
- multiple authoritative resume generators;
- broad concurrency before sequential live recovery is proven.

## Live exit gate

Phase 3 is complete only when the persistent supervised system demonstrates all of these:

- [x] A real source inserts normalized, deduplicated jobs into SQLite.
- [ ] A running OMP loop notices a newly queued job without being manually invoked as a one-shot CLI command.
- [ ] The job is atomically claimed once and its durable state survives an intentional loop restart.
- [x] The canonical generator creates or reuses a verified one-page resume for that exact job description.
- [x] The application browser uploads the PDF whose hash matches the selected manifest.
- [x] The Phase 1 loop completes and verifies every reachable application field before submission.
- [x] `prepareSubmission(session, { finalRef })` authorizes the exact current final candidate ref, then `beginFinalSubmit(session)` durably records the attempt before the browser click.
- [x] OMP clicks the returned ref, `completeFinalSubmit` records the observed outcome, canonical evidence is validated against that job, and SQLite derives `completed` plus the actual attempt count.
- [x] The headed browser remains available in the supervised runtime long enough for OMP to capture the submission outcome.
- [x] OMP returns to backlog inspection after persistence succeeds.

## OMP kickoff prompt

> Use `skills/application-prep/SKILL.md` as the canonical operational procedure and the active durable run record as current state. Call `recoverPrepareOrClaimBacklogRun` with `max_active_jobs = 1`, heartbeat the sole active run below half its lease duration, and follow the safe-batch observe/act/re-observe loop through audited submission and durable persistence. Inspect the backlog again immediately after terminal persistence; call `waitForOmpWake` only when startup returns idle. Do not reread historical handoffs or expand a per-job checklist unless a concrete blocker or defect requires diagnosis.
- [ ] A real source row is normalized and inserted through `ingestSupportedJobs`; unsupported source rows are absent from the claimable queue.
- [ ] A running OMP loop notices a newly queued supported job without being manually invoked as a one-shot CLI command.
- [ ] The job is claimed once with the full snapshot binding and survives an intentional loop restart.
- [ ] The canonical generator creates or reuses a verified one-page resume for that exact description.
- [ ] The headed Greenhouse or Ashby browser receives the PDF whose hash matches the selected manifest/run identity.
- [ ] The platform planner covers every encountered field while model use is limited to allowed response inference/oversight.
- [ ] Every reachable application field is deliberate, valid, retained, and audited.
- [ ] `prepareSubmission`, `beginFinalSubmit`, the OMP click, and `completeFinalSubmit` form one fully paired successful attempt.
- [ ] Canonical evidence validates against the same job/run and SQLite derives `completed`.
- [ ] OMP returns to supported backlog inspection after persistence succeeds.

## OMP kickoff prompt

> Use `skills/application-prep/SKILL.md` as the canonical operating procedure. Call `prepareOrRecoverSupportedRun` with `maxActiveJobs: 1`; use `minimumJobId` only as an inclusive floor for new claims, never recovery. Do not claim from an unbound URL/host or use static cross-job description/resume paths. Accept only rows normalized by `job-source.mjs`. Use `planPlatformApplication` for redirect reclassification and platform mechanics, leave unknown widgets unresolved, and cross the final-submit boundary only after `prepareSubmission` authorizes the exact current ref.
