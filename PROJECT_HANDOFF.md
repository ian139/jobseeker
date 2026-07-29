# Jobs Assistant: Visual Computer-Use Handoff

## Purpose

This handoff preserves the useful product history while defining the current application boundary. It is historical context and design evidence, not a substitute for `TODO.md` or `skills/application-prep/SKILL.md`. Those files are the operating authority.

The repository combines a local job backlog, a deterministic resume generator, and a supervised OMP application workflow. Job records, resumes, applicant evidence, screenshots, answer memory, and run evidence are owner-private. External services remain explicit boundaries.

## Status statement

| Area | Handoff status |
| --- | --- |
| TheirStack job getter | Working historical anchor. |
| Canonical standalone resume generator | Working historical anchor. |
| SQLite backlog and maintenance | Supporting implementation; verify current state before relying on it. |
| Application workflow | Rebuilt around the screenshot-first run-v2 contract; current live proof remains required. |
| Persistent OMP loop | Durable lifecycle concepts are retained; a current visual end-to-end run is still required. |
| Historical application implementations | Reference-only; their interaction mechanics are retired. |
| Second resume implementation | Preserved as incompatible reference; not the canonical generator. |

A test suite, a private artifact, or a durable backlog record can prove a narrow contract. None proves that the current visible browser workflow or final submission succeeded without a fresh headed run.

## 1. Overall product idea

The intended local workflow is:

```text
find jobs
  -> normalize and store a private backlog
  -> select a job
  -> generate a truthful one-page resume
  -> prepare and audit one application with OMP
  -> submit through the current visual gate
  -> retain private evidence and durable outcome
```

The product is deliberately local-first:

- job records live in SQLite;
- resumes, profiles, job descriptions, screenshots, answer memory, and application evidence stay on the owner's machine;
- source calls and model calls occur only at explicit configured boundaries;
- paid job-source calls require explicit authorization;
- final submission is authorized by completeness and image-based retention audit, then performed by OMP.

The repository grew from several adjacent experiments: a job getter, a generic feed importer, a backlog, a canonical resume generator, a second resume path, and multiple application attempts. They are not one uniformly proven product. The job getter and canonical generator are the reliable anchors; application work must be verified against run-v2 evidence.

## 2. Repository evolution

Early work explored a broad job-search product with search, storage, matching, resume, outreach, and application responsibilities. That history explains duplicate paths and archived packages, but it does not define the current boundary.

The first simplification kept three local responsibilities:

1. obtain and normalize job records;
2. process one job at a time;
3. prepare, audit, and submit an application with durable evidence.

The current simplification is stricter about the application surface. It does not revive older interaction stacks or handoff assumptions. The only current interface observation is a fresh screenshot of the current visible headed browser. A configured Codex or Gemini provider converts that image into a bounded visual observation, and `omp_computer` performs coordinate or keyboard actions. Every action is followed by a fresh image.

Older application attempts remain useful for identifying edge cases, privacy boundaries, and failure classifications. They are not sources of interaction instructions, compatibility promises, or live completion claims.

## 3. Job getter, source, and backlog

### 3.1 Responsibilities

The job side should:

1. describe the search profile;
2. query an explicit source;
3. validate the response;
4. normalize records into one internal shape;
5. remove poor matches and duplicates;
6. store the result in SQLite;
7. expose a small queue for resume and application work.

The working historical source is TheirStack. A fresh source adapter must keep source identity, canonical URL identity, private raw payload, and credit-safe authorization separate.

### 3.2 Search quality

Source profiles describe jobs, not applicants. Shared filters favor open United States roles, direct employers, recent postings, and early-career software, data, and infrastructure work. They exclude senior or management titles, recruiting and sales roles, excessive experience requirements, clearance requirements, commission-heavy roles, and profile-specific mismatches.

TheirStack preview mode was designed to avoid paid retrieval while tuning search quality. It can report a bounded match count without persisting a job. A paid sync requires an explicit authorization, validates every page before publication, caps pagination, and avoids automatic replay of ambiguous paid requests.

Pinned source modes may retain only records whose application URL meets the requested route policy. Filtering precedes company-level deduplication. The one-role-per-company choice favors configured role priority, recency, and stable source order. This is a queue-quality policy, not a universal truth.

### 3.3 Normalization and persistence

The normalized job shape includes source job ID, title, company, application URL, description, location, remote status, discovered time, updated time, and source metadata. Canonical URL identity helps deduplicate records but does not prove that two source records are semantically identical. Raw source payloads and descriptions remain private.

The minimal durable model separates:

- `jobs`: source identity, canonical URL, job details, queue state, and private payload;
- `application_runs`: claim owner, lifecycle, visual ledger/evidence path, resume binding, progress, and targeted-input state;
- `resume_artifacts`: canonical generator fingerprint and private PDF/manifest identities.

Claims, leases, recovery, and terminal transitions must be atomic. The first operating limit is `max_active_jobs = 1`.

## 4. Canonical resume generation

### 4.1 Goal

The generator accepts a job description, structured applicant evidence, and the current resume. It produces a truthful job-specific resume that preserves the existing LaTeX form, compiles to one page, and is reproducible from fingerprinted inputs.

### 4.2 Governing principles

- `Archive/resume_generator.py` is the canonical implementation. Do not merge the incompatible second generator.
- The structured profile is the source of candidate claims. Reconcile the source resume into that catalog deliberately.
- Job text supplies requirements and wording context, never evidence that the applicant has a skill or fact.
- Preserve `Archive/resume/generator/Resume.tex` typography, margins, section styling, and form.
- Preserve the resume policy skill and include its digest in the generation identity.
- Deterministic ranking and the resume selector are the baseline. Optional model advice may rank only known evidence IDs.
- Compile and measure the actual PDF. If it overflows, trim lower-priority supported material in deterministic order; if sparse, add the next supported material without exceeding one page.
- Preserve source IDs for every selected claim. Never invent an employer, title, date, metric, credential, responsibility, or skill.
- Publish the private five-file bundle: `resume.tex`, `resume.pdf`, `optimization.json`, `job_description.txt`, and `manifest.json`.

### 4.3 Generator evidence

A real job description and local applicant evidence must produce editable LaTeX and an exactly one-page PDF with extractable text. The output must preserve the current form, remain traceable to source evidence, reproduce for identical inputs, change identity when a material input changes, and publish exactly the canonical private bundle.

## 5. Screenshot-first application workflow

### 5.1 Fixed run contract

Every application run uses:

```text
schema: phase1-run-v2
browser_mode: headed
perception_driver: image_agent_v1
action_driver: omp_computer
model_provider: codex | gemini
submit_policy: omp_agent
```

The run also binds one application URL, a read-only job snapshot, at least one private applicant-evidence input, an upload PDF, private answer memory, and a private artifact directory. One persistent OMP session may have only one active run.

### 5.2 Image observation

`phase1-visual-observation-v1` is produced only from a fresh screenshot of the current visible browser. It contains `schema`, `observation_id`, `previous_observation_id`, `observed_at`, `surface`, `agent`, `targets`, and `blockers`.

`surface` is `{ surface_id, url, title, screenshot_sha256, viewport: { width, height } }`; `agent` is `{ provider, model }`. A target contains:

```text
{ target_id, field_id, group_id, kind, label, description, bounds,
  visible, enabled, required, readonly, value_state, checked, selected,
  options, validation, file, candidate, confidence }
```

Bounds are integer screenshot pixels with positive dimensions within the viewport. Value state is `blank`, `present`, `selected`, or `unknown`. Validation carries nullable `valid` and `message_present`. A file carries only presence and nullable basename. Candidate class is `field`, `non_final_navigation`, `final_candidate`, or `unknown`. No raw applicant value is permitted in the observation or public evidence.

### 5.3 Action and retention loop

The computer-use adapter owns the current visible surface and exposes `captureView`, `analyzeView`, and `performAction`. Its action set is exactly `click`, `type_text`, `press_key`, `scroll`, and `upload_file`. Clicks use coordinates inside current image bounds; text and keys are deliberate computer input; upload paths are canonical private files.

The loop is:

```text
recover existing active run or claim one job
  -> validate private input and resume binding
  -> capture fresh current-browser screenshot
  -> analyze with Codex or Gemini
  -> accept immutable observation and update visual target ledger
  -> resolve answer and plan conservative action
  -> perform one action
  -> capture fresh screenshot and chained observation
  -> verify visual retention and repair if needed
  -> audit current final candidate
  -> authorize, begin, perform, and complete submission
  -> publish private evidence and durable outcome
```

Batch only independent routine fields. Newly revealed or dependent targets, invalid/retry work, uploads, choices, widgets, navigation, blockers, and submission remain single actions. After every action, including a timeout or error, capture a fresh screenshot before diagnosis or retry. Retention proof is `{ action_id, visually_confirmed, file_name? }`; evidence stores screenshot identities and digests, never raw answers.

Answer precedence is `memory -> profile -> resume -> agent_inference -> user`. Inference is limited to source-backed non-sensitive facts and carries private rationale and evidence digests. Missing sensitive, legal, financial, identity, authorization, credential, date, medical, or protected-class facts require one precise user answer and same-run continuation. External access boundaries require the user and must never be bypassed.

### 5.4 Audited final submission

A final action is valid only when the latest visual observation contains exactly the current `final_candidate` and all reachable targets are deliberate, valid, retained, and unblocked. Bind the authorization to `finalTargetId`:

1. call `prepareSubmission(session, { finalTargetId })` and require current authorization;
2. call `beginFinalSubmit` before the action and require its target binding matches;
3. perform the authorized computer click on the current target;
4. capture and analyze a fresh post-action screenshot even on error;
5. call `completeFinalSubmit` exactly once with the observed outcome;
6. on failure, obtain a new observation, repair, and reauthorize;
7. after one success, publish post-submit screenshot identity and finalize private evidence.

A rejected action is not a closed job. Only explicit live evidence that the posting is unavailable may produce `closed`. The successful terminal state requires one paired successful submission attempt and validated completion evidence.

## 6. Shared infrastructure and privacy

The secure file layer enforces regular-file and no-symlink checks, owner-only modes, bounded reads, canonical JSON, atomic no-replace publication, descriptor identity checks, and directory finalization. Contract/profile loading and evidence publication are asynchronous at boundaries; immutable ledger operations are serialized.

Public logs and summaries may contain only stable IDs, source classes, SHA-256 values, screenshot identities, basenames, and outcomes. Keep applicant values, answer text, job descriptions, resume text, screenshots, authentication state, and raw source payloads private.

Typed delegation must load the complete matching schema object from `schemas/` and pass it as the strict output schema. A path-only reference is not validation.

## 7. Architecture lessons

### 7.1 Keep responsibilities narrow

Source ingestion, backlog persistence, resume generation, application planning, visual observation, computer actions, ledger retention, audit, evidence, and durable lifecycle should remain separate. Each boundary needs one owner and one canonical identity.

### 7.2 Make evidence stronger than claims

A completed field needs a deliberate source, a current visual state, validation status, and retention proof. A successful application needs the current final-target authorization, a journaled two-phase attempt, and post-submit screenshot evidence. A passing narrow contract never substitutes for a current live observation.

### 7.3 Preserve recoverability

A `needs_user` run remains active with its private evidence and current workspace. A rejected final action returns to fresh observation and repair. Operational failures remain retryable until diagnosed. Only explicit unavailability closes a job.

### 7.4 Keep batching conservative

Routine independent targets can be grouped only when their answers are deterministic and the screenshot is current. Newly revealed, dependent, invalid, unusual, or final targets are processed singly. The image after each action is the boundary for the next decision.

## 8. Recommended fresh-start boundary

Keep the job getter, normalized SQLite contract, canonical resume generator, private evidence layer, visual planner, immutable ledger, audit, screenshot transport, and durable one-active-run lifecycle. Remove retired interaction stacks, duplicate coordinators, compatibility aliases, and unsupported fallbacks rather than carrying them forward.

The active application path should expose:

```text
contract/profile/memory
  -> screenshot transport
  -> Codex or Gemini visual observation
  -> planner.mjs visual target decisions
  -> omp_computer action
  -> screenshot retention
  -> audit and two-phase submission
  -> private evidence and durable outcome
```

This boundary is intentionally image-based. It does not depend on hidden page structure or an alternate interaction representation.

## 9. File map

| Area | Important paths |
| --- | --- |
| Run contract and profile | `src/phase1/contract.mjs`, `src/phase1/profile.mjs` |
| Visual planner | `src/phase1/planner.mjs` |
| Visual transport | `src/phase1/computer-use-adapter.mjs` |
| Ledger and audit | `src/phase1/ledger.mjs`, `src/phase1/audit.mjs` |
| Evidence | `src/phase1/evidence.mjs` |
| Backlog lifecycle | `src/phase1/backlog-runner.mjs`, `migrations/004-durable-active-runs.sql` |
| Strict task contracts | `schemas/application-decision.schema.json`, `schemas/target-diagnosis.schema.json`, `schemas/repair-result.schema.json` |
| Canonical resume | `Archive/resume_generator.py`, `Archive/resume/generator/Resume.tex`, `Archive/resume/generator/SKILL.md` |
| Active operating procedure | `skills/application-prep/SKILL.md` |
| Scope and live evidence | `TODO.md` |

Archived application paths and old workspace snapshots are references only. Verify every path and claim against the active tree before relying on it.

## 10. Final handoff summary

The durable value of this repository is the local job backlog, source-backed one-page resume generator, privacy-preserving evidence discipline, and one-active-run lifecycle. The current application contract is screenshot-first computer use: capture the current visible browser, analyze the image with Codex or Gemini, act with coordinates or keyboard, capture a fresh image after every mutation, retain through visual proof, and submit only through current final-target authorization followed by begin, action, and complete records.

No historical application artifact is current live proof. A new agent should read `TODO.md`, read the application skill, recover or claim one run, and leave every live gate unchecked until the required screenshot chain and audited post-submit evidence exist.
