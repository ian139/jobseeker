# Jobs Automation Rebuild

This roadmap is the active scope and safety authority. `PROJECT_HANDOFF.md` is historical context only. `skills/application-prep/SKILL.md` is the canonical operating procedure for an application run. A backlog authorization permits supervised operation, but it does not satisfy an unchecked live gate.

**Current posture:** Phase 2 resume generation and the durable Phase 3 lifecycle repair are historical implementation evidence. Phase 1 visual computer use is the active cutover contract. No live Phase 1 or Phase 3 submission gate is complete until a current headed session produces the required screenshot identities, visual observations, retention proofs, audited submission journal, and post-submit evidence. Process exactly one active job at a time.

## How to use this file

1. Read the active phase and the application skill before changing implementation or starting a run.
2. Treat every unchecked item as work or evidence still required. Do not infer live completion from source code, a fixture, a unit test, a mocked surface, a private historical report, or backlog authorization.
3. Preserve applicant data, answer values, screenshots, resume text, job payloads, and authentication state under owner-private paths. Public notes contain only bounded IDs, digests, basenames, and outcomes.
4. Keep OMP as the operator and reasoning loop. Deterministic modules may expose narrow contracts, but a custom daemon, RPC coordinator, policy engine, or alternate interaction stack must not become the product loop.
5. Keep the one-active-run invariant across claims, leases, recovery, and submission. A `needs_user` run remains active and resumable.
6. Record the exact observation and action evidence required by the run-v2 contract. Never mark a live gate complete without direct current proof.

## Product end state

```text
job source
  -> normalized SQLite backlog
  -> deterministic one-page source-backed resume
  -> persistent supervised OMP session with one active run
  -> phase1-run-v2 private inputs
  -> fresh screenshot of the current visible headed browser
  -> Codex or Gemini visual analysis
  -> visual target ledger and truthful answer resolution
  -> coordinate/keyboard computer actions
  -> fresh screenshot after every mutation
  -> image-based retention and completeness audit
  -> two-phase audited final submission
  -> private evidence and durable outcome
```

The application target is one concrete job per run. The run continues through non-final navigation, validation repair, and newly revealed targets until it is complete, needs a truthful user fact, reaches an external access boundary, or has explicit evidence that the posting is unavailable.

## Decisions that apply to every phase

- **One job per application run.** Phase 1 receives one direct URL or one atomically claimed backlog job. Starting preparation does not require a separate per-job approval.
- **Screenshot-first state.** A fresh desktop screenshot containing the current visible browser is captured before each visual decision. The screenshot is analyzed by the configured Codex or Gemini provider and bound to a surface identity and SHA-256.
- **Single action driver.** `action_driver` is fixed to `omp_computer`. Permitted operations are `click`, `type_text`, `press_key`, `scroll`, and `upload_file`, performed from screenshot coordinates or deliberate keyboard input. There is no alternate action path.
- **Fresh image after mutation.** After every computer action, including an error or timeout, capture a new screenshot and obtain a chained visual observation before another action, diagnosis, or retry.
- **Visual target ledger.** Bind plans and evidence to `target_id`, `targetId`, and `finalTargetId`. Do not use stale target identities after a new observation. Keep the ledger immutable and preserve diffs and action history.
- **Truthful answers only.** Resolve in order `memory -> profile -> resume -> agent_inference -> user`. Inference may transform source-backed non-sensitive facts only. Never infer identity, authorization, protected-class, salary or compensation, dates, credentials, medical, legal, or other sensitive facts.
- **No early handoff.** Optional fields, validation failures, or an unresolved target are not success. Continue when recoverable; ask one precise question for a missing truthful fact, save it to private answer memory, and resume the same run.
- **Two-phase final submission.** The current visual `final_candidate` must pass completeness and image-based retention audit. `prepareSubmission(session, { finalTargetId })` authorizes that exact target; `beginFinalSubmit` records the attempt before the computer click; a fresh image follows; `completeFinalSubmit` resolves the attempt exactly once. A retry needs a new observation and new authorization.
- **Evidence is private and bounded.** Store screenshot identities, observation IDs, target IDs, action IDs, SHA-256 digests, file basenames, source classes, and outcomes. Never store raw applicant values in evidence.
- **Live evidence must match the claim.** Resume success requires a real compiled and inspected PDF. Application success requires a real headed screenshot chain and audited post-submit evidence. Pipeline success requires the persistent OMP loop to process a real SQLite row.

## Assets to retain

These resume assets are unrelated to the application interaction cutover and remain canonical:

- `Archive/resume_generator.py` — deterministic resume selector, renderer, compile/measure loop, and artifact publisher.
- `Archive/resume_generator_command.py` — existing invocation and job-selection adapter used as reference.
- `Archive/resume_advisor.py` — optional advisory ranking limited to known evidence IDs.
- `Archive/resume/generator/SKILL.md` — governing resume policy and fingerprinting role.
- `Archive/resume/generator/profile.json` — structured applicant evidence catalog; private.
- `Archive/resume/generator/Resume.tex` — current LaTeX form and style.
- `Archive/resume/Main_Resume.pdf` — private visual reference for the form.

`Archive/resume.py` and `Archive/resume_artifacts.py` belong to an incompatible second resume implementation. Do not merge it into the canonical generator.

---

# Phase 1 — Screenshot-first application run

## Goal

Given one application URL, a read-only job-description snapshot, and at least one applicant-evidence input, an OMP agent uses a fresh screenshot of the current visible headed browser, Codex or Gemini visual analysis, and coordinate/keyboard computer actions to complete every reachable application target, upload the configured resume, repair validation, and submit only through the audited two-phase boundary.

## Required run parameters

The owner-private run contract is machine-readable and contains:

| Parameter | Required value or meaning |
| --- | --- |
| `schema` | Fixed to `phase1-run-v2`. |
| `application_url` | One application URL selected directly or from the backlog. |
| `job_description_path` | Read-only local job snapshot; context only, never applicant evidence. |
| `applicant_profile_path` | Optional private profile with reusable facts and verified answers. |
| `source_resume_path` | Optional source resume used as applicant evidence. |
| `resume_upload_path` | Canonical PDF path to upload during this run. |
| `answer_memory_path` | Private appendable verified-answer memory. |
| `run_artifact_dir` | Private destination for screenshot identities, ledger, journal, and evidence. |
| `browser_mode` | Fixed to `headed`; use the current visible browser surface. |
| `perception_driver` | Fixed to `image_agent_v1`. |
| `action_driver` | Fixed to `omp_computer`. |
| `model_provider` | Required `codex` or `gemini`; examples use `codex`. |
| `submit_policy` | Fixed to `omp_agent`. |

At least one of `applicant_profile_path` and `source_resume_path` is required. `resume_upload_path` may point to the source resume, but it must be canonicalized and privately verified before use. Do not add a legacy observer, action driver, or loop parameter to this contract.

## Visual observation contract

Every accepted image analysis uses `schema: phase1-visual-observation-v1` and these top-level keys:

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

`surface` is:

```text
{ surface_id, url, title, screenshot_sha256, viewport: { width, height } }
```

`agent` is `{ provider, model }`; provider must equal the run's `model_provider`. Every target is:

```text
{
  target_id, field_id, group_id, kind, label, description, bounds,
  visible, enabled, required, readonly, value_state, checked, selected,
  options, validation, file, candidate, confidence
}
```

Use integer screenshot-pixel `bounds` with positive width and height wholly inside the declared viewport. `value_state` is one of `blank`, `present`, `selected`, or `unknown`. `validation` is `{ valid, message_present }` with nullable `valid`; `file` is null or `{ present, file_name }` where `file_name` is a nullable basename. `candidate` is `{ class, reason }` and its class is `field`, `non_final_navigation`, `final_candidate`, or `unknown`. The observation contains no raw applicant values or hidden interface metadata.

The transport owns the visible surface and exposes only `captureView`, `analyzeView`, and `performAction`. The action set is exactly:

- `click`, using coordinates inside a current target's bounds;
- `type_text`, using deliberate text input into the current target;
- `press_key`, using an explicit key on the current surface;
- `scroll`, using a bounded visual scroll action;
- `upload_file`, using the privately verified canonical file path.

## Answer and target policy

Resolve every target from the exact precedence `memory -> profile -> resume -> agent_inference -> user`. Store each supplied user answer in owner-private answer memory before continuing. An inferred answer must carry private rationale and source-evidence digests. Never infer restricted personal, legal, financial, medical, demographic, authorization, credential, date, or compensation facts. Ask only for a missing truthful fact or an external authentication/access-control interaction.

A target is complete only when it has a deliberate state, the current image shows that state retained, the current target is visible and valid or intentionally blank, and all dependent targets revealed by it are inventoried and resolved. For an upload, retain only the action ID, screenshot identity, and verified file basename/hash; do not persist file contents or applicant values in public evidence. Optional targets still require a deliberate retained state when reachable.

## Required visual loop

1. Recover an existing active run before claiming anything new. Enforce `max_active_jobs = 1`.
2. Validate the exact job, private profile/memory, job snapshot, and resume-upload identity. Keep the headed browser visible on the current application surface.
3. Capture a fresh desktop screenshot containing the current visible browser. Bind its SHA-256 and surface identity to the observation request.
4. Ask the configured Codex or Gemini provider for a bounded `phase1-visual-observation-v1`. Validate provider, schema, bounds, image identity, and privacy before accepting it.
5. Merge the observation and diff into the immutable visual target ledger. Preserve prior evidence while replacing only the current visual state.
6. Resolve answers and choose a conservative plan. Batch only independent routine targets; use a single action for newly revealed/dependent targets, invalid or retry work, uploads, choices, widgets, navigation, blockers, and final submission.
7. Record the action intent against the current `observation_id` and `target_id`, then call `performAction` once. Stop immediately on an unexpected result.
8. Capture a fresh screenshot after every action, including failures and timeouts. Analyze it as the chained next observation, update the ledger, and verify image-based retention with `{ action_id, visually_confirmed, file_name? }` for every attempted target.
9. Retry only a failed or stale target after the fresh observation. Newly revealed targets become historical ledger obligations and must be completed before further navigation.
10. When no unresolved field target remains, activate one current `non_final_navigation` candidate and return to step 3. Never treat an ambiguous or unknown candidate as navigation authority.
11. On the final surface, capture a final fresh screenshot, accept its visual observation, verify every reachable target and retention proof, and require exactly one current `final_candidate`.
12. Call `prepareSubmission(session, { finalTargetId })` for that current candidate. Require authorization and matching current observation/target identity.
13. Call `beginFinalSubmit` before any submit action. Require its returned target binding to equal the authorized `finalTargetId`, then perform the authorized coordinate click through `omp_computer`.
14. Capture and analyze a fresh post-action screenshot even when the computer action reports an error. Call `completeFinalSubmit` exactly once with the observed outcome. A failure returns to step 3 and requires a new audit and authorization.
15. After one successful completion, publish private post-submit screenshot identity and completion evidence, finalize the run, persist its terminal outcome, and inspect the backlog again.

## Work checklist

Implementation and live proof are distinct. Keep every unobserved live item unchecked:

- [ ] Implement and verify the phase1-run-v2 contract and fixed values.
- [ ] Implement screenshot capture and SHA-256 surface binding for the current visible headed browser.
- [ ] Implement Codex/Gemini visual observation validation and immutable target ledger updates.
- [ ] Implement coordinate/keyboard computer actions and the exact five-action adapter boundary.
- [ ] Implement fresh-image-after-action retention proofs without raw applicant values.
- [ ] Implement conservative visual planning, answer precedence, retry, and one-active-run recovery.
- [ ] Implement the completeness audit and current visual final-candidate binding.
- [ ] Implement and exercise `prepareSubmission -> beginFinalSubmit -> performAction -> fresh image -> completeFinalSubmit`.
- [ ] Publish private post-submit evidence and durable terminal persistence.
- [ ] Demonstrate one complete real application run below.

## Phase 1 live exit gate

Phase 1 is complete only when one current headed live run directly demonstrates every item below. None of these items is complete from a test, fixture, historical artifact, or backlog authorization:

- [ ] The run uses `schema: phase1-run-v2`, `browser_mode: headed`, `perception_driver: image_agent_v1`, `action_driver: omp_computer`, `model_provider: codex|gemini`, and `submit_policy: omp_agent`.
- [ ] The exact job and private evidence inputs are bound to one run, and one active run is enforced.
- [ ] A fresh screenshot of the current visible browser is captured before each visual decision and each screenshot identity validates.
- [ ] A Codex or Gemini observation validates as `phase1-visual-observation-v1` with valid surface, viewport, target bounds, blockers, and no raw applicant values.
- [ ] Every reachable application target, including optional and evidence-supported sensitive targets, has a deliberate, valid, visually retained state.
- [ ] Dynamic targets and non-final navigation encountered during the run are completed with a fresh image after every mutation.
- [ ] The configured resume upload succeeds and private evidence records its verified basename/hash and retention proof.
- [ ] The latest image contains exactly the current final candidate authorized by `prepareSubmission(session, { finalTargetId })`.
- [ ] `beginFinalSubmit` records the authorized attempt before the computer click; `completeFinalSubmit` resolves that same attempt exactly once.
- [ ] Post-submit screenshot identity, completion evidence, and the paired submission-attempt journal record exactly one success.
- [ ] The headed browser remains available long enough to capture post-submit evidence, and the run finalizes only from that evidence.

No current live proof is recorded here. The prior private preparation report predates run-v2 visual observation and audited submission, so it cannot satisfy this gate.

## Explicitly out of scope

- scraping, job ranking, or broad cross-site compatibility;
- resume tailoring outside the canonical generator;
- a custom application CLI, daemon, RPC service, or second orchestrator;
- bypassing authentication, assessments, anti-bot challenges, or access controls;
- storing raw applicant values in public evidence;
- multiple active application runs before sequential recovery and ownership are proven.

## Phase 1 kickoff prompt

> Read `skills/application-prep/SKILL.md` and the active private run record. Recover the existing run first or claim one queued job with `max_active_jobs = 1`. Use `phase1-run-v2`: capture a fresh screenshot of the current visible headed browser, ask the configured Codex or Gemini provider for `phase1-visual-observation-v1`, resolve truthful answers into the visual target ledger, and use only `omp_computer` coordinate/keyboard actions. Capture and analyze a fresh image after every action, retain every target with image proof, and submit only through `prepareSubmission({ finalTargetId }) -> beginFinalSubmit -> performAction -> fresh image -> completeFinalSubmit`. Finalize private evidence before inspecting the backlog again.

---

# Phase 2 — Canonical one-page resume generator

## Goal

Given a job description, structured applicant facts, and the current resume, generate the strongest truthful job-specific resume that preserves the current LaTeX form, compiles to exactly one page, fills the page densely, and remains reproducible from its inputs.

## Canonical decisions

- Promote `Archive/resume_generator.py`; do not activate the incompatible second JSON-to-PDF generator.
- Keep the structured profile as the source of candidate claims. Reconcile facts from the source resume deliberately; job text supplies context, never applicant evidence.
- Preserve `Archive/resume/generator/Resume.tex` structure, typography, margins, section styling, and markers.
- Preserve `Archive/resume/generator/SKILL.md` as governing policy and include its digest in each generation identity.
- Keep deterministic ranking and the resume selector as the baseline. Optional model advice may rank only known evidence IDs.
- Enforce one page by compiling, measuring the PDF, and trimming or expanding supported material in deterministic order.
- Preserve source IDs and provenance for every selected claim. Never invent or inflate a skill, employer, title, date, metric, impact, or responsibility.
- Publish the private five-file bundle: `resume.tex`, `resume.pdf`, `optimization.json`, `job_description.txt`, and `manifest.json`.

## Phase 2 live gate

- [x] A real job description and local applicant evidence produce editable LaTeX and a compiled PDF.
- [x] The PDF has exactly one page and extractable text.
- [x] The output preserves the current resume form and style.
- [x] Selected content is denser and job-relevant while every claim remains traceable.
- [x] Identical inputs reproduce the same validated fingerprinted result.
- [x] Material input changes create a distinct artifact identity.
- [x] Exactly the five canonical private artifacts are published and hashed.

These checks concern resume generation only and do not prove Phase 1 application completion.

---

# Phase 3 — SQLite backlog and persistent visual application loop

## Goal

Ingest normalized jobs into SQLite, keep a persistent supervised OMP session watching the backlog, atomically claim one queued job, generate or reuse its verified resume, execute the Phase 1 screenshot-first workflow, and persist the canonical outcome before inspecting the backlog again.

## Minimal SQLite contract

Keep ingestion, resume artifacts, and application runs separate.

### `jobs`

At minimum: stable ID, source, source job ID, canonical application URL, title, company, location, description, discovered/updated timestamps, queue status, and private raw source payload. Deduplicate by source identity and canonical URL.

### `application_runs`

At minimum: run ID, job ID, claim owner/time, lifecycle state, current visual ledger/evidence path, selected resume artifact identity, last progress time, and concise failure or targeted-input state. This table owns application lifecycle.

### `resume_artifacts`

At minimum: job ID, generator fingerprint, PDF path/hash, manifest path/hash, and creation time. The row points to the canonical Phase 2 bundle.

## Required lifecycle

```text
queued
  -> claimed
  -> applying
  -> needs_input      (run remains active; exact user fact or external challenge only)
  -> completed        (audit passed; audited submission succeeded and is recorded)
     or skipped
```

Operational failures remain active and retryable until diagnosed. Claims and status transitions are atomic so restarts cannot duplicate a run. `completed` requires validated post-submit visual evidence.

## Persistent OMP operating model

OMP is the long-running orchestrator. A supervised workspace may contain:

- a control pane with the active contract, job ID, and loop state;
- the headed browser pane owned by the current run;
- a concise SQLite/run inspection pane with private artifact paths;
- a review workspace for a completed pre-submit surface, never mistaken for an active fill loop.

Start with `max_active_jobs = 1`. `recoverOrClaimBacklogRun` recovers an existing active run first and claims a new job only when no active run exists. Scripts may provide deterministic source, database, resume, screenshot, or observation operations; they do not own the loop.

The persistent loop is:

```text
recover-or-claim
  -> validate exact job and resume binding
  -> capture/analyze fresh current-browser screenshot
  -> resolve visual targets and perform conservative computer actions
  -> capture/analyze fresh image, retain, and repair
  -> audit current final candidate
  -> begin/click/complete audited submission
  -> persist private evidence and canonical outcome
  -> inspect backlog again
```

If no work exists, the session waits for a bounded interval or explicit wake signal. A `needs_input` run remains bound to its workspace and resumes after the required user fact or narrow external interaction.

## Phase 3 runtime parameters

| Parameter | Meaning |
| --- | --- |
| `db_path` | Local SQLite database. |
| `source_adapter` | Selected Phase 3 source implementation. |
| `source_credentials` | Private source credentials, if required. |
| `applicant_profile_path` | Optional private application profile and verified answers. |
| `source_resume_path` | Optional source resume; at least one evidence input is required. |
| `resume_profile_path` | Canonical structured resume evidence. |
| `resume_template_path` | Retained `Resume.tex`. |
| `resume_skill_path` | Retained resume policy skill. |
| `artifact_root` | Private per-job resume and application evidence root. |
| `poll_interval_seconds` | Bounded idle wait before queue inspection. |
| `max_active_jobs` | Fixed to `1` until sequential recovery is proven. |
| `schema` | `phase1-run-v2` for each application run. |
| `browser_mode` | `headed`. |
| `perception_driver` | `image_agent_v1`. |
| `action_driver` | `omp_computer`. |
| `model_provider` | `codex` or `gemini`. |
| `submit_policy` | `omp_agent`. |

## Phase 3 work checklist

- [ ] Define one normalized job contract independent of the source adapter.
- [ ] Select and implement the first real source adapter.
- [ ] Create minimal SQLite migrations for jobs, resume artifacts, and application runs.
- [ ] Normalize URLs and deduplicate source records without exposing the private payload.
- [ ] Implement atomic claim, release, lease, and recovery semantics for one queued job.
- [ ] Bind the claimed job, private evidence, verified resume, and phase1-run-v2 contract to one workspace.
- [ ] Generate or reuse a verified one-page resume before application actions.
- [ ] Execute the screenshot-first visual loop and persist progress after fresh-image retention.
- [ ] Mark `completed` only after the audited two-phase submission succeeds and post-submit evidence validates.
- [ ] Demonstrate the source-to-backlog-to-resume-to-application flow on one real queued job.

## Phase 3 live exit gate

Keep every item unchecked until a current supervised run directly proves it:

- [ ] A real source inserts normalized, deduplicated jobs into SQLite.
- [ ] A persistent OMP loop notices a newly queued job without a one-shot command.
- [ ] The job is claimed exactly once and its durable state survives an intentional restart.
- [ ] The canonical generator creates or reuses a verified one-page resume for that job.
- [ ] The application run uses the exact phase1-run-v2 values and one active workspace.
- [ ] Fresh current-browser screenshots, Codex or Gemini observations, target identities, and SHA-256 bindings are recorded throughout.
- [ ] Every reachable target is deliberate, valid, and visually retained after each mutation.
- [ ] The upload basename/hash and image-based retention proof match the selected private artifact.
- [ ] The current visual final candidate is authorized by `prepareSubmission({ finalTargetId })`.
- [ ] `beginFinalSubmit` records the attempt before the authorized computer click, and `completeFinalSubmit` resolves it exactly once.
- [ ] Private post-submit image evidence validates, SQLite derives `completed`, and the actual attempt count is persisted.
- [ ] The headed browser remains available for post-submit evidence and OMP returns to backlog inspection.

No item above is satisfied by historical records, source code, tests, or backlog authorization alone.

## Phase 3 kickoff prompt

> Use `skills/application-prep/SKILL.md` as the canonical procedure and the active durable run record as state. Call `recoverOrClaimBacklogRun` with `max_active_jobs = 1`; use the exact phase1-run-v2 parameters; capture a fresh screenshot of the current visible headed browser; ask Codex or Gemini for visual observation; act only through `omp_computer`; capture a fresh image after every mutation; retain with image proof; and complete `prepareSubmission({ finalTargetId }) -> beginFinalSubmit -> click -> fresh image -> completeFinalSubmit` before persisting the canonical outcome. Do not mark any unchecked live gate complete without current evidence.
