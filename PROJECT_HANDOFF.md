# Jobs Assistant: Full Conceptual Handoff

## Purpose of this document

This is a handoff for someone who is going to start fresh rather than continue the repository line by line. It explains what the project was trying to become, which approaches were attempted, how the major parts fit together, and which ideas are worth carrying forward.

It is intentionally more conceptual than implementation-oriented. File names are included so a future agent can locate the relevant evidence, but the goal is not to recreate every class, table, validation rule, or test.

> **Current execution note:** This document is historical context, not the active application/browser mechanics. Before any live application action, use `TODO.md` Phase 1 and reread `skills/application-prep/SKILL.md` section **Handoff-safe OMP browser quick start**. The active order is the OMP `browser` tool on the visible CMUX browser surface, then a control-specific pinned-CLI fallback, then the OMP `computer` tool only for a remaining native browser/OS interaction. Text is `tab.fill(selector, exactText)` with no `--value` prefix. Resume upload is `tab.uploadFile(exactFileInputCss, session.runMetadata.resume_upload_path)` after uniquely verifying the real file input's exact CSS selector. Historical Puppeteer, human-review-only, targetless typing, and pre-audit submission descriptions below are not current operating instructions.

### Active-tree correction — 2026-07-28

The historical status table below does not describe the current implementation. `TODO.md` remains authoritative. The active Phase 3 tree now has:

- a fail-closed Greenhouse/Ashby registry and normalized payload extractor in `src/phase1/platforms.mjs`;
- migration `005-platform-job-snapshots.sql`, which refuses active-run rebuilds, preserves terminal history, and quarantines every pre-migration nonterminal row pending supported re-ingestion;
- supported ingestion, canonical URL/source deduplication, bound queue reads, and unsupported-row quarantine in `src/phase1/job-source.mjs`;
- atomic claims fenced by the complete normalized snapshot, including the source-posted timestamp, in `src/phase1/backlog-runner.mjs`;
- recovery-first exact description → offline canonical resume → manifest/PDF validation → exact claim → private workspace composition in `src/phase1/preparation.mjs`, with description-digest-keyed staging and persisted answer-memory recovery fencing;
- frozen Greenhouse/Ashby action plans for exact fills, uploads, native choices, staged custom-combobox opening/exact selection, and checkbox transitions, with final submission still excluded until audit authorization.

Deterministic code owns source classification, description extraction, queue eligibility/binding, resume generation, control mechanics, retention, and audit. OMP/model reasoning is limited to oversight/diagnosis and allowed evidence-backed unresolved response content. Contract-level evidence is in `tests/platforms.test.mjs` and `tests/job-source-preparation.test.mjs`; the unchecked live gates in `TODO.md` are still not complete.

## Contents

- [Status statement](#status-statement-use-this-instead-of-the-repositorys-older-claims)
- [1. Overall product idea](#1-the-overall-product-idea)
- [2. Repository evolution](#2-how-the-repository-evolved)
- [3. Scraper, job getter, and backlog](#3-scraper-job-getter-and-backlog)
- [4. Application applier](#4-application-applier)
- [5. Resume generation](#5-resume-generation)
- [6. Shared infrastructure](#6-shared-infrastructure)
- [7. Architecture lessons](#7-architecture-lessons-across-the-whole-project)
- [8. Recommended fresh-start boundary](#8-recommended-fresh-start-product-boundary)
- [9. File map](#9-file-map-for-a-future-agent)
- [10. Final handoff summary](#10-final-handoff-summary)

## Status statement: use this instead of the repository's older claims

The repository's README, roadmap, and tests often describe features as active, complete, or supported. Those statements record intended behavior and prior engineering checkpoints. They are not the current status authority.

For this handoff, the authoritative status is:

| Area | Current status for handoff |
|---|---|
| TheirStack scraper/job getter | **Working** |
| Canonical standalone `resume-generate` | **Working** |
| Application applier | **Not working; there are many errors involved with it** |
| Generic JSON/API feed importer | Present as an implemented experiment; not independently declared working here |
| Backlog inspection and maintenance commands | Present as supporting code; not independently declared working here |
| `jobs-assistant resume-generate` application-service resume path | Preserved second implementation; not the working canonical generator |
| OMP/RPC application coordinator | Part of the non-working applier effort |
| Historical scraper/applier/web/outreach implementations | Archived or ignored reference material, not active products |

The distinction matters. A large test suite can prove that individual contracts were considered, but it does not prove that a live browser workflow, external service, or complete product currently works.

---

# 1. The overall product idea

The project was meant to be a local job-search assistant with three main stages:

```text
Find jobs
  -> clean and store them in a local backlog
  -> tailor a resume for a selected job
  -> prepare, audit, and submit an application with an OMP agent
```

The product was deliberately local-first:

- job records live in SQLite;
- resumes, profiles, descriptions, screenshots, and application evidence stay on the owner's machine;
- external services are used only at explicit boundaries;
- paid TheirStack calls require explicit authorization;
- final application submission is authorized by the completeness audit and automated by OMP.

The repository gradually became several products living beside one another:

1. a TheirStack-based job getter;
2. a normalized feed importer;
3. a SQLite backlog and audit layer;
4. a canonical standalone resume generator;
5. a second, application-service resume generator;
6. a guarded Greenhouse/Lever applier attempt;
7. a persistent OMP/RPC coordinator around that applier;
8. packaging, container, browser, test, and artifact infrastructure.

The key handoff lesson is that these pieces should not all be treated as one proven system. The two proven anchors identified for this handoff are the TheirStack job getter and the standalone resume generator. Everything else should be evaluated independently before reuse.

---

# 2. How the repository evolved

The history is useful because it explains why there are duplicate paths and why the current application area is so large.

## Early direction: broad job-search product

The first version began as a scraper. Later work added or merged:

- resume generation;
- a job applier;
- outreach and contact tracking;
- matching and scoring;
- a web interface;
- job detail routing;
- scheduled/background behavior;
- public JSON ingestion.

That direction produced a broad `job-scraper`-style product with UI, scheduler, storage, matching, outreach, resume, and application responsibilities. The historical version can be found under `rpc/archive/old-applier/`.

## First simplification

The repository was later minimized around a smaller local workflow:

- obtain job records;
- normalize and deduplicate them;
- store them in SQLite;
- process one job at a time;
- authorize final submission only after a complete observation-bound audit, then let OMP submit.

The historical `rpc/archive/REBUILD_PROMPT.md` captures the earlier simplification. The active policy retains credit-safe ingestion, a backlog, explicit page observation, field resolution, evidence, and adds audited OMP-agent submission.

## Older Playwright application attempt

An intermediate `job-sync` implementation used a simpler application pipeline:

```text
observe page
  -> resolve fields
  -> apply policy
  -> execute allowed actions
  -> persist the run
  -> authorize and submit through OMP
```

This version used Playwright and separated observer, resolver, policy, executor, and runner modules. It survives under `rpc/archive/old-scraper/src/apply_pipeline/`. It is useful as a record of the original conceptual split, not as active code.

## Current root rewrite

The active source tree was consolidated under `src/jobs_assistant/`. It retained the small backlog and ingestion concepts, but the application side was rebuilt around:

- exact Greenhouse and Lever route rules;
- a Python-to-Node browser protocol;
- Puppeteer;
- deterministic safety checks;
- private run artifacts;
- a durable SQLite state machine;
- optional model-assisted resolution;
- a headed browser handoff;
- an additional persistent OMP/RPC coordinator.

Many commits then hardened narrow application concerns: stale elements, networking, browser process ownership, disabled controls, continuation buttons, multi-selects, screenshots, action evidence, and recovery.

## Separate canonical resume generator

A deterministic standalone resume generator was added later and then deliberately isolated from the application-service resume code. This is the working resume generator referred to in this handoff.

## Current repository shape

The maintained source is at the root under `src/jobs_assistant/`. Several local directories are ignored and should not be mistaken for alternate active implementations:

- `scraper/` contains protected runtime data and empty legacy source directories;
- `rpc/` contains a stale duplicate workspace snapshot, build products, and archives;
- `build/`, `.venv/`, caches, and `*.egg-info` are generated material;
- `data/` contains private runtime databases and artifacts.

Start from the root package. Use ignored and archived directories only to understand history.

---

# 3. Scraper, job getter, and backlog

## 3.1 Goal

The scraper side was intended to create a useful local queue of jobs without repeatedly reviewing the same listing or wasting paid search credits.

The conceptual responsibilities are:

1. describe the kind of job being sought;
2. query a source;
3. validate the source response;
4. normalize records into one internal shape;
5. remove poor matches and duplicates;
6. store the result in SQLite;
7. expose a small backlog for later resume or application work.

The working job-getter path is TheirStack.

## 3.2 Current source-of-truth files

The important files are:

- `src/jobs_assistant/theirstack.py` — TheirStack search profiles, preview, paid fetching, validation, filtering, pagination, and normalization;
- `src/jobs_assistant/contracts.py` — normalized job data shapes;
- `src/jobs_assistant/backlog.py` — job upsert, deduplication, queue ordering, counts, listing, and archive behavior;
- `src/jobs_assistant/db.py` — SQLite schema, URL identity, sync audits, checkpoints, and database helpers;
- `src/jobs_assistant/job_source.py` — generic JSON/API feed normalization;
- `src/jobs_assistant/cli.py` — command-line entry points;
- `docs/ingestion.md` — the clearest operator-level description.

The old `scraper/src/...` directories are empty. The actual maintained implementation is the root Python package.

## 3.3 TheirStack: the working job getter

### Search profiles

The implementation represents a search as a named source profile rather than scattering search terms through commands. The profiles include:

- a broad early-career computer-science search;
- a similar non-co-op search;
- a fall co-op software/data search;
- a default profile with only shared exclusions.

These source profiles describe jobs. They are not applicant profiles and should never be passed into resume or application fields.

### Shared search quality rules

The search payload tries to favor:

- open jobs;
- direct employers;
- United States roles;
- recently posted or discovered jobs;
- software, data, infrastructure, and related early-career work.

It also tries to exclude:

- senior and management titles;
- recruiting and sales roles;
- excessive experience requirements;
- clearance requirements;
- commission-heavy roles;
- profile-specific mismatches such as co-op jobs in the non-co-op profile.

The idea is to spend filtering effort before jobs reach the backlog.

### Credit-safe preview

TheirStack supports a preview mode designed to avoid paid full-record retrieval. The preview:

- asks for blurred company data;
- requests only one result;
- asks for the total match count;
- does not retrieve complete descriptions;
- does not persist jobs.

This lets the user tune a search before authorizing a paid fetch.

A subtle limitation is that an ATS filter cannot truly be applied during preview because blurred results do not provide the final application URL. A preview can say how many jobs match the search, but it cannot prove how many have a valid Greenhouse or Lever route.

### Paid sync

The paid sync requires an explicit flag or environment authorization. This is an important product boundary: a normal command should not silently consume credits.

The paid flow is conceptually:

```text
build search payload
  -> make one paid page request
  -> validate its response shape
  -> continue pagination when required
  -> validate the full aggregate
  -> normalize jobs
  -> optionally filter by exact ATS route
  -> choose jobs
  -> upsert them into SQLite
  -> record a sync audit
```

The current implementation deliberately does not automatically retry paid requests. A timeout or ambiguous response may already have consumed credits, so replaying automatically could spend money twice. Preview requests can be retried because the preview shape is treated as credit-safe.

Pagination is capped. If a later page is malformed or inconsistent, the aggregate is not partially written. Requests already made may still have consumed credits, but the backlog is not left with a misleading half-sync.

### ATS modes

The sync has three conceptual modes:

- `auto` keeps the historical broad ingestion behavior and can retain jobs with arbitrary application URLs;
- `greenhouse` keeps only jobs whose URL passes the exact Greenhouse route policy;
- `lever` keeps only jobs whose URL passes the exact Lever route policy.

Pinned ATS filtering happens before company-level deduplication. This avoids selecting an ineligible job for a company and accidentally discarding an eligible one from the same company.

The `auto` mode uses an incremental checkpoint so later syncs can focus on newly discovered jobs. Pinned ATS modes intentionally re-fetch the latest requested window rather than advancing the same kind of checkpoint. That avoids making a valid ATS job permanently unreachable because a limited paid page happened to contain mostly rejected URLs.

The tradeoff is cost: repeated pinned runs can repeatedly spend credits on the same raw window.

### One job per company

TheirStack results are normally reduced to at most one role per company. The selector derives a company identity, then favors:

1. the configured role priority;
2. the more recent posting;
3. stable source order as a final tie-breaker.

This is a backlog-quality choice, not a universal truth. It was intended to avoid flooding the queue with many near-identical roles from one employer.

### Compatibility command

`job-scrape` is not a separate scraper. It is a compatibility entry point into the same TheirStack sync implementation. A fresh system should keep only one underlying implementation even if it exposes multiple command names.

## 3.4 Normalization

Sources use different field names. The normalizer tries to map common alternatives into one job shape:

- source job ID;
- title;
- company;
- listing/application URL;
- description;
- location;
- remote status;
- posting date;
- original raw source payload.

A job must have at least one stable identity: a source ID or a usable URL.

The raw payload is retained privately for provenance and debugging, but backlog display commands omit it.

## 3.5 Deduplication and identity

The backlog uses two identity layers:

1. the same source plus the same source job ID;
2. the canonicalized URL.

URL canonicalization removes noise such as fragments, common tracking parameters, default HTTPS ports, and an unnecessary trailing slash. Scheme and host are normalized.

This means the same listing can update an existing row instead of appearing as a new job on every sync.

The general lesson is worth keeping: preserve the source's identifier when available, but also maintain a source-independent URL identity for duplicate detection.

## 3.6 SQLite backlog

The backlog is intentionally small in concept. A job row contains normalized public fields, the private raw payload, timestamps, and a status such as:

- `queued`;
- `in_progress`;
- `archived`.

A separate sync record captures the source, selected profile/mode, counts, checkpoint, completion state, and an error string. Expected failures generally use fixed public labels, while unexpected TheirStack exceptions may be stored as exception text.

The application and generated-resume tables grew much larger later, but the ingestion/backlog concept itself remains simple.

### Write behavior

Batch upserts were designed to be atomic. If one record in a validated batch cannot be stored, earlier records should not remain committed accidentally.

The implementation also supports savepoints so a batch can participate in a larger transaction, such as combining job updates with a terminal sync audit.

### Read behavior

Backlog list and show operations were designed as read-only views that:

- do not create a missing database;
- do not claim a job;
- do not open a browser;
- do not print the raw source payload;
- return deterministic ordering and bounded descriptions.

Archiving is a separate explicit mutation. It is compare-and-set style: all selected rows must still be queued or the entire archive request is rejected.

## 3.7 Generic feed import: another method that was tried

The project also implemented a normalized feed importer. It accepts either:

- a local JSON file; or
- `GET /v1/jobs` from a configured base URL.

Supported envelopes include a top-level list or an object containing `jobs` or `data`.

This route reuses normalization and SQLite identity logic, but it does not apply the full TheirStack search quality rules, one-per-company selection, or ATS route filtering. It is better understood as a fixture/backfill/import boundary than as the proven job getter.

A dry-run mode was added to simulate production normalization and deduplication against an in-memory copy of the database. The idea was to show what would be inserted or updated without changing the configured database.

For this handoff, this code is present but is not independently labeled working.

## 3.8 Historical scraper approaches

The older `job-sync` implementation under `rpc/archive/old-scraper/` tried:

- a dry-run that could build payloads or request a free preview count;
- a paid `sync-once` path;
- an external job-source importer with pagination and query options;
- a single co-op-oriented search profile;
- a global checkpoint;
- optional retention of multiple jobs per company;
- paid HTTP retries.

The current root implementation changed several of those decisions:

- multiple named source profiles;
- paid requests are not automatically retried;
- stricter response-envelope validation;
- ATS-specific modes;
- filtering before company dedupe;
- a revised source-aware schema;
- queued/backlog status;
- atomic audits and dry-run simulation.

The archived database schema is not compatible with the current root schema. Old databases should not be attached to current code without an explicit migration.

## 3.9 Scraper/backlog limitations to remember

- Paid syncs can consume credits even when the command later fails.
- Pinned ATS modes can repeatedly re-fetch and repay for the same window.
- Preview counts cannot be truly ATS-filtered.
- `auto` can store URLs the applier would never support.
- One-per-company may hide a second role a user would have preferred.
- Generic feeds receive much less quality filtering than TheirStack.
- Canonical URL identity helps with duplicates but does not prove two source records are semantically identical.
- Source jobs and descriptions are private data even when the search itself is generic.

## 3.10 What to keep in a fresh system

Keep these ideas:

- preview before paid fetch;
- explicit paid authorization;
- no automatic retry of ambiguous paid requests;
- typed normalization at the source boundary;
- source ID plus canonical URL deduplication;
- validate a complete batch before writing;
- atomic job updates plus sync audit;
- private raw payload retention;
- a small queue with explicit status transitions;
- separate source-search profiles from applicant profiles.

Avoid restoring the ignored `scraper/` package or the archived schema. The maintained concept is already represented more clearly in the root package.

---

# 4. Application applier

## 4.1 Status

The applier is **not working and has many errors involved with it**.

That is the complete current status claim. The rest of this section explains what was attempted and what the architecture was meant to accomplish. It should not be read as a claim that the live workflow succeeds.

## 4.2 Goal

The historical applier targeted one queued application at a time. The active goal is:

1. take one queued job with a direct Greenhouse or Lever URL;
2. open the public application page;
3. observe the fields and controls;
4. fill fields from explicit applicant data and evidence-backed inference;
5. upload the configured resume;
6. move through application pages;
7. save enough evidence to review what happened;
8. run an observation-bound completeness and retention audit;
9. authorize one current final-control ref and let OMP submit it;
10. capture the submission outcome and post-submit evidence.

The active submission invariant is:

> OMP may perform `final_submit` only for the exact current control ref authorized by a successful completeness and retention audit. Failed attempts require a fresh observation and fresh authorization; completion requires exactly one successful submission.

## 4.3 Five application approaches were tried

### Attempt 1: broad monolithic application product

The oldest `rpc/archive/old-applier/` version bundled a Playwright applier with the TheirStack scraper, FastAPI web interface, scheduler, matching engine, LLM integration, outreach, resume upload, and public-feed support. This explored the broad end-to-end product idea, but tightly coupled too many responsibilities and was intentionally abandoned during the later minimization.

### Attempt 2: `job-sync` Playwright pipeline

The archived `rpc/archive/old-scraper/src/apply_pipeline/` version used a comparatively direct split:

- observer — convert a page into a normalized description;
- resolver — decide answers from profile/resume/job context;
- policy — block sensitive, final, or unsafe actions;
- executor — locate and perform approved actions;
- runner — repeat the loop and persist progress;
- runs/backlog — store application state.

This was conceptually clean, but the historical material describes scaffold and live-mode limitations. It was superseded and should not be revived as active code.

### Attempt 3: removed first-principles applier

`rpc/archive/minimized-20260706/applier/` preserves another distinct generation removed during the July 2026 minimization. It had its own observer, deterministic resolver, LLM adapter, guarded executor, runner, review sampler, live smoke, and tests. Its configured-resume and observation-bound ideas remain useful, but the archive explicitly marks it non-runnable and says it was too unsettled for active development.

### Attempt 4: guarded direct Puppeteer workflow

The current direct workflow lives mainly in `src/jobs_assistant/application.py` and calls the Puppeteer browser adapter directly.

It tried to make every action observation-bound:

```text
claim job
  -> validate ATS route
  -> open private run directory
  -> start browser
  -> observe page
  -> resolve safe answers
  -> plan one action
  -> persist evidence
  -> execute one action
  -> observe again
  -> repeat
  -> audit and authorize the final control
  -> submit through OMP and capture the outcome
```

Only one mutation is intended per observation. After any change, the page must be observed again so stale element IDs or changed options are not reused.

### Attempt 5: persistent OMP/RPC coordinator

A second orchestration layer was added around the guarded workflow:

- `application-rpc` exposes a local JSON-lines service;
- OMP owns a long-running coordinator process;
- OMP can see only a small set of application-specific host tools;
- SQLite stores request IDs, lifecycle state, events, and action sequence numbers;
- requests are intended to be idempotent and deadline-bound;
- browser ownership, cancellation, handoff, and cleanup are reconciled durably.

This was meant to allow an agent to reason across multiple pages while keeping browser mutations behind deterministic host checks.

It also multiplied the number of boundaries that must agree. The RPC/OMP layer is part of the non-working applier, not an independently working service.

## 4.4 Intended architecture by responsibility

### ATS adapters

`src/jobs_assistant/ats.py` contains the Greenhouse and Lever adapter concepts. The intent was to share:

- applicant facts;
- resume fact extraction;
- field answer validation;
- required-field handling;
- deterministic resolution behavior.

The ATS-specific part should be route rules, selectors, and platform quirks rather than a completely separate workflow.

Greenhouse was the first adapter. Lever was added as the second adapter behind the same workflow. The active replacement adds observation-bound OMP submission after audit.

### Route and safety policy

`src/jobs_assistant/safety.py` and `safety_policy.json` describe supported route families and sensitive/final descriptors.

The current attempt used very exact routes. It tried to reject:

- unknown hosts;
- credentials embedded in URLs;
- fragments and unexpected query parameters;
- private or local destinations;
- cross-job redirects;
- final-looking paths or controls;
- unsupported frames;
- unsafe forms and methods;
- authentication, CAPTCHA, and assessment pages.

This was meant to prevent a model or changing page from turning one authorized application into arbitrary browsing.

### Browser adapter

`src/jobs_assistant/browser_adapter.py` is the Python side. `puppeteer_runner.js` is the Node/Puppeteer side.

They communicate using a length-prefixed JSON protocol. The adapter attempted to handle:

- process startup and ownership;
- route validation;
- bounded commands and responses;
- observations;
- field fills, selects, checks, and uploads;
- safe non-final control activation;
- screenshots;
- headed handoff;
- error normalization;
- process cleanup.

There is intentionally no generic `evaluate JavaScript` or `submit` command exposed to the application planner.

### Page observation

The observer tries to turn a live DOM into a bounded, immutable page snapshot containing:

- page and frame identity;
- fields and their kinds;
- labels and names;
- values and validity;
- options;
- required/disabled/hidden state;
- buttons and navigation candidates;
- blockers;
- final-like controls.

Elements receive observation-scoped identities. A new observation makes old identities stale.

### Resolution

The resolver was designed to use a strict precedence order:

1. explicit opt-out preferences;
2. explicit ATS-specific configured field answers;
3. explicit profile facts and unambiguous matching resume facts;
4. user preference mappings;
5. optional model assistance for unresolved safe fields.

The job description was context about the job, not evidence about the applicant. The resume was source material, not a license to infer any answer. Sensitive, legal, financial, protected-class, authentication, CAPTCHA, and assessment answers were not to be guessed.

Optional model output was treated as a proposal. It had to match a schema and be checked again against the current field, allowed value type, available options, privacy rules, and safety policy.

### Planning and execution

A plan could contain field actions, a resume upload, or a safe continuation control. The same facts were rechecked immediately before execution.

The design tried to prevent:

- using a stale field after the page changed;
- clicking a lookalike final button;
- selecting an option that no longer exists;
- targeting a hidden or disabled field;
- letting the model invent a selector;
- changing a value without proving it was retained;
- continuing to another job or host;
- making background network requests outside the approved policy.

### Evidence

Each run was intended to receive a private directory under an application artifact root. Potential evidence includes:

- the claimed job snapshot;
- the staged resume input;
- the job description;
- observations;
- plans;
- attempted and completed actions;
- unresolved fields;
- filled state;
- screenshots;
- browser failure and cleanup records;
- review-session and handoff records;
- a manifest containing relative names and hashes.

The evidence system was meant to answer: What page was seen? What action was authorized? What happened? What remained for the human?

### Human handoff

The intended successful ending was a headed browser left at the final review/submit stage. Evidence had to be committed before ownership was released.

The software would then stop owning the browser. The person would review, fill anything left manual, decide whether to submit, close the window, and later record the outcome.

## 4.5 Persistent RPC concept

The RPC layer exposed four lifecycle concepts:

- start a run;
- inspect status;
- resume after a permitted manual pause;
- cancel an active run.

It did not expose a submit operation.

Internally, OMP was limited to eight high-level tools:

- observe;
- fill a field;
- select an option;
- set a checkbox/radio;
- upload the configured resume;
- activate a safe non-final control;
- capture a screenshot;
- prepare human handoff.

The intent was sound: the agent reasons at a high level while the host retains deterministic authority. However, the implementation also had to coordinate JSON framing, deadlines, request replay, browser ownership, process groups, tool-call identity, SQLite state, evidence state, cancellation, and handoff finalization.

## 4.6 Why this became difficult

The applier spans many independently stateful systems:

- live third-party HTML;
- browser behavior;
- Node/Puppeteer;
- Python async orchestration;
- SQLite transactions;
- filesystem artifacts;
- optional external model calls;
- OMP process and tool protocols;
- a headed GUI window owned by a human at the end.

A change in any layer can invalidate assumptions in another. The code therefore accumulated overlapping validation and recovery logic.

The main application workflow alone is thousands of lines. The Node browser owner, Python adapter, RPC coordinator, OMP bridge, database state machine, route policy, preferences, profiles, artifacts, and tests add tens of thousands more. This size is not itself proof of a design flaw, but it makes live compatibility and cross-layer consistency hard to establish.

## 4.7 Error areas represented in the attempt

The user's status is simply that the applier is not working and has many errors. The repository shows the kinds of errors the design anticipated:

### Setup and input errors

- missing browser or Node dependencies;
- no headed display;
- invalid or mismatched ATS URL;
- invalid profile or preferences;
- ambiguous resume facts;
- changed input hashes;
- unsafe file paths, permissions, or symlinks;
- artifact-root collisions;
- database schema or claim-state conflicts.

### Browser and process errors

- launch and handshake timeouts;
- malformed or unframed protocol responses;
- browser process identity mismatches;
- unexpected process exit;
- cleanup that cannot prove the browser closed;
- detached handoff ownership uncertainty.

### Navigation and network errors

- redirecting to another route or job;
- DNS/address changes;
- unexpected assets or network calls;
- oversized traffic;
- popup or service-worker behavior;
- navigation timeout;
- unsafe continuation behavior.

### Observation and action errors

- unstable pages;
- oversized observations;
- duplicate or ambiguous fields;
- hidden, readonly, or disabled controls;
- stale observations;
- changed options;
- failed hit testing;
- values not retained after a fill;
- no progress after a click;
- unsupported controls;
- fields that resemble sensitive or final actions.

### Model and coordinator errors

- missing credentials;
- provider or model mismatch;
- malformed output;
- unknown field IDs;
- unsafe or privacy-leaking answers;
- deadline or cancellation races;
- duplicate/conflicting request IDs;
- uncertain durable commits;
- lost child processes;
- tool registry or prompt mismatch;
- handoff reconciliation failures.

These branches show substantial defensive effort. They also show why deterministic fixtures and contract tests do not automatically translate into a working live application flow.

## 4.8 What did not establish success

The repository contains extensive application tests, but many use fake browsers, fake processes, fake coordinators, and local fixtures. Live Puppeteer tests are opt-in and commonly skipped unless environment flags are supplied. Container checks are headless packaging checks and cannot prove a physical headed handoff.

Roadmap checkboxes, fixture success, and historical test counts should therefore be read as records of what was attempted, not proof that the applier works now.

## 4.9 What to keep from the applier idea

Keep these concepts in a fresh design:

- final submission is automated by OMP only after an observation-bound completeness audit;
- one job per run;
- exact applicant data is the source of truth;
- job text is not applicant evidence;
- resume upload is explicit and singular;
- observe before acting;
- bind actions to the observation that authorized them;
- re-observe after each meaningful page mutation;
- keep ATS-specific behavior behind adapters;
- persist a small, reviewable evidence record;
- leave unresolved sensitive fields for a human;
- end in a headed review state.

## 4.10 What not to carry forward initially

Do not start a fresh implementation by rebuilding all of these at once:

- persistent RPC lifecycle;
- native OMP child-process protocol;
- exactly-once tool-call semantics;
- detached browser ownership recovery;
- complex two-phase artifact/database handoff reconciliation;
- generalized cross-page navigation;
- every historical route and control edge case.

A better fresh-start sequence is:

1. one ATS;
2. one deterministic local fixture;
3. one direct browser process;
4. observe a page;
5. fill a small set of explicit safe fields;
6. upload one configured resume;
7. stop at final review;
8. persist a concise evidence bundle;
9. prove that live path;
10. add the second ATS;
11. add model assistance only after deterministic behavior is reliable;
12. add RPC/agent coordination only if a real operational need remains.

The archived Playwright pipeline may be useful for its simple separation of responsibilities, but its code and schemas should not be copied wholesale.

---

# 5. Resume generation

## 5.1 The critical distinction

There are two different resume generators with similar names.

### Working canonical generator

```text
resume-generate
```

Implemented by:

- `src/jobs_assistant/resume_generator_command.py`;
- `src/jobs_assistant/resume_generator.py`;
- `src/jobs_assistant/resume_advisor.py` for optional ranking advice;
- `resume/generator/profile.json`;
- `resume/generator/Resume.tex`;
- `resume/generator/SKILL.md`.

Default output:

```text
data/generated-resumes-generator/
```

This is the working resume generator.

### Preserved application-service generator

```text
jobs-assistant resume-generate
```

Implemented by:

- `src/jobs_assistant/resume.py`;
- `src/jobs_assistant/resume_service.py`;
- `src/jobs_assistant/resume_artifacts.py`;
- generated-resume tables in `db.py`;
- the main CLI.

Default output:

```text
data/generated-resumes/
```

This is a separate preserved implementation and is not the canonical working generator for this handoff.

Their profile schemas, renderers, artifacts, cache identities, database behavior, and APIs are incompatible. Do not point them at one another's inputs or output roots.

## 5.2 Goal of the canonical standalone generator

The standalone generator tries to produce a job-specific one-page resume without inventing candidate facts.

Its governing idea is:

- the profile is the only source of candidate claims;
- the job description is used only to rank and select those claims;
- unsupported requirements are not converted into experience;
- the output should be ATS-readable, dense, and one page;
- every result should be reproducible and traceable to its inputs.

## 5.3 Inputs

The canonical workflow uses four main inputs:

1. a queued job from the SQLite backlog;
2. a structured candidate profile;
3. a LaTeX template;
4. a governing skill/policy document.

It opens the backlog read-only. Selecting a job for resume generation does not claim it or change its status.

The structured profile contains explicitly modeled material such as:

- contact and links;
- education;
- experience;
- leadership;
- projects;
- skills;
- source references;
- verification notes;
- graduation rules;
- open questions.

The profile is treated as a controlled evidence catalog rather than freeform prose.

## 5.4 Selection and matching

The optimizer reads the job title and description, infers a broad role family, and extracts relevant terms and requirements.

It then scores existing profile material against those terms. The exact scoring is less important than the conceptual rule: ranking can change which supported claims are shown, but it cannot create a new claim.

The generator gives special attention to:

- role title and high-priority requirements;
- supported technologies and skills;
- relevant experience bullets;
- relevant projects;
- education/coursework when appropriate;
- explicit graduation rules.

It tries to select a coherent combination rather than simply stuffing every matching keyword into the page.

## 5.5 Rendering

The selected plan is inserted into a LaTeX template at required markers. Profile text is escaped so candidate content is not treated as LaTeX commands.

The generator then invokes a bounded compiler:

- Tectonic when available;
- otherwise `pdflatex`;
- or an explicitly supplied compiler.

External compilation is time- and size-bounded. Compiler identity is included in the result fingerprint.

## 5.6 One-page fitting

One-page output is a hard goal, not an informal preference.

The generator compiles and measures the PDF. If it overflows, it trims content in a deterministic priority order, removing lower-priority material before higher-value evidence. If the first result is too sparse, it can try to add supported content back while staying within one page.

The resulting PDF is inspected with `pypdf` to confirm:

- a readable PDF was produced;
- it is one page;
- text can be extracted;
- rendered content corresponds to the selected supported claims.

This compile-measure-adjust loop is one of the strongest ideas in the project because it verifies the actual artifact rather than assuming that source text will fit.

## 5.7 Optional model advisory

The standalone generator can optionally ask an Ollama-hosted model for ranking hints.

The model is not allowed to write resume text. It may only return identifiers for already known claims or coursework. Unknown, duplicate, malformed, oversized, or failed responses are discarded, and deterministic generation continues.

This makes the model advisory rather than authoritative. The generator remains usable without model access.

## 5.8 Fingerprinting and cache

The generator hashes the important inputs:

- job snapshot;
- profile bytes;
- template bytes;
- governing skill bytes;
- compiler identity;
- algorithm/advisory identity where relevant.

Those hashes form a fingerprint. An existing fingerprint directory is reused only after its manifest and artifacts are validated. Published identities are not silently overwritten.

This provides reproducibility: the same validated inputs can reuse the same result, while a changed job, profile, template, policy, compiler, or generation algorithm produces a new identity.

## 5.9 Output artifacts

Each successful standalone generation publishes exactly five private files:

1. `resume.tex` — editable generated LaTeX;
2. `resume.pdf` — the verified one-page PDF;
3. `optimization.json` — selected claims, matching, and audit information;
4. `job_description.txt` — the exact job description snapshot;
5. `manifest.json` — input and artifact hashes.

The output is placed under a job-specific, fingerprint-specific directory inside `data/generated-resumes-generator/`.

The standalone generator does not:

- create a generated-resume database row;
- change the backlog status;
- attach the PDF to an application;
- open a browser;
- submit anything.

## 5.10 The second resume approach that was tried

The application-service generator used a different concept. Instead of a strict structured resume profile and LaTeX template, it accepted:

- an application profile JSON;
- a source resume in PDF, text, or Markdown;
- a job snapshot;
- an optional description override.

It attempted to:

1. extract non-sensitive candidate claims;
2. score them against the job;
3. create a strict resume JSON document with citations;
4. validate that every claim was grounded;
5. render a built-in one-page PDF;
6. store a generated-resume lifecycle in SQLite;
7. persist a larger private provenance bundle;
8. make the result available to the application workflow by ID.

Its artifacts include request, claims, response, validation, scoring, resume JSON, PDF, and manifest. Its directories use generated UUID-style run identities, not the standalone fingerprint layout.

This path duplicated useful concepts—grounding, hashes, private artifacts, one-page output—but it is not interchangeable with the standalone generator.

## 5.11 Why the two resume systems should not be casually merged

They disagree at almost every boundary:

| Concern | Standalone canonical | Application-service path |
|---|---|---|
| Candidate input | Structured generator profile | Application profile plus source resume |
| Job model | Simple read-only resume job | Hashed application-oriented job snapshot |
| Renderer | LaTeX plus external compiler | Built-in JSON-to-PDF renderer |
| Persistence | Fingerprint directories only | SQLite lifecycle plus UUID run directories |
| Output bundle | Exactly five files | Larger provenance bundle |
| Optional model | Ranking existing IDs only | No standalone advisor integration |
| Backlog effect | Read-only | Job remains queued, but generated-resume rows are written |
| Application integration | None | Can be bound to an application run |

A future system should select one source of truth. If application integration is needed, write a small explicit adapter around the canonical artifact rather than overlaying types, renaming one implementation, or sharing output roots.

## 5.12 What to keep in a fresh system

Keep these canonical resume ideas:

- structured, explicit candidate evidence;
- job description used only for matching;
- no invented claims;
- deterministic selection as the baseline;
- model advice limited to known IDs;
- actual compile-and-measure one-page enforcement;
- PDF text extraction checks;
- input fingerprinting;
- immutable, hash-verified artifact bundles;
- read-only backlog selection;
- complete separation from application submission.

If simplifying, retain the working standalone generator first. Add a narrow application adapter later only if the applier itself becomes reliable.

---

# 6. Shared infrastructure

## 6.1 Python package

The root project is a Python 3.11+ setuptools package with locked `uv` dependencies. Runtime Python dependencies are intentionally small: mainly `httpx` for HTTP and `pypdf` for PDF inspection.

It exposes three command families:

- `jobs-assistant` — main multi-command CLI;
- `job-scrape` — compatibility entry point to TheirStack sync;
- `resume-generate` — canonical standalone resume generator.

The main CLI grew to own ingestion, backlog, application, review, preferences, RPC, and the application-service resume commands. In a fresh system, consider splitting user-facing command groups while preserving one shared domain implementation.

## 6.2 Node/Puppeteer package

The browser path has a separate `package.json` with pinned Puppeteer, Bun, and OMP packages. It installs a managed Chrome and exposes browser smoke/verification scripts.

This runtime exists for the applier, which is non-working. It is not needed for the TheirStack getter or canonical resume generation.

## 6.3 Docker and Compose

The Docker image combines Python, Node/Bun, and a headless Chromium shell. Compose:

- runs as the host user's numeric UID/GID;
- mounts `data/` read/write;
- mounts `resume/` read-only;
- uses a private temporary HOME;
- forwards only documented environment variables;
- defaults to a help command.

The container was designed mainly for packaging and headless smoke checks. It does not prove a headed browser handoff on macOS or another desktop host.

For a fresh start, separate container verification of ingestion/resume from host-only verification of a headed application workflow.

## 6.4 Local private data

Sensitive local paths include:

- SQLite databases and sidecars;
- source job payloads and descriptions;
- applicant profiles;
- source and generated resumes;
- application observations and screenshots;
- model requests/responses containing applicant context;
- browser/runtime ownership data.

The repository attempts to use owner-private directories and files, commonly `0700` for directories and `0600` for files, with no-symlink and ownership checks at important boundaries.

This review did not inspect runtime SQLite rows, applicant profile contents, resume contents, or generated artifacts. A future agent should preserve that discipline unless explicitly authorized to operate on the data.

## 6.5 Tests

The test tree is heavily contract-oriented:

- ingestion and backlog tests;
- feed and TheirStack tests;
- canonical resume generator tests;
- application-service resume tests;
- application planner/workflow tests;
- RPC/OMP tests;
- artifact and database tests;
- Puppeteer adapter tests;
- CLI smoke tests.

This is valuable as a specification of intended edge cases. It is not a current working-status certificate. In particular:

- many application tests use doubles;
- browser integration requires explicit environment flags;
- physical headed handoff checks require a human;
- container smoke is headless;
- old roadmap test counts describe prior checkpoints.

A fresh agent should use tests to recover contracts, then establish a new live reproduction for any subsystem before calling it working.

## 6.6 Documentation and orchestration

The repository contains detailed guides for ingestion, resume generation, application drafts, RPC, and operations. It also contains an OMP workflow document requiring explicit plans, task ownership, focused tests, parent integration, and container checks.

These documents are useful design evidence. Some status wording conflicts with this handoff and should eventually be corrected if the repository continues. For example, the root README and TODO call the applier active or complete, while the current authoritative status is that it is not working and has many errors.

## 6.7 Generated and ignored workspace material

Do not confuse the following with maintained source:

- `.venv/` — local Python environment;
- `build/` and `*.egg-info` — packaging output;
- `.pytest_cache/` and `__pycache__/` — caches;
- `rpc/` — ignored workspace snapshot/archive area;
- `scraper/` — ignored legacy/local data area with empty visible source directories;
- `data/` — runtime databases and private artifacts;
- screenshots under source or runtime roots — evidence/debug artifacts, not product code.

The active source-of-truth is the tracked root package, root tests, root docs, and root packaging files.

---

# 7. Architecture lessons across the whole project

## 7.1 Good ideas that repeated across subsystems

Several principles were consistently valuable:

### Explicit boundaries

- paid versus free requests;
- source profile versus applicant profile;
- job facts versus candidate facts;
- resume generation versus application attachment;
- non-final browser actions versus final submission;
- private artifacts versus public summaries.

### Deterministic core, side effects at edges

The strongest code tries to normalize, validate, select, score, and plan using pure data before invoking HTTP, SQLite, filesystem, compiler, model, or browser effects.

### Provenance

Raw source payloads, input hashes, job description snapshots, manifests, and action evidence were all attempts to make results explainable and reproducible.

### Fail closed around expensive or dangerous actions

- do not replay ambiguous paid requests;
- do not invent resume claims;
- do not target stale browser elements;
- do not expose arbitrary browser scripting;
- authorize OMP final submission only for the current audited control ref.

## 7.2 Where complexity accumulated

The repository often encoded every discovered edge case directly into the active implementation. This was manageable in ingestion and resume generation because their inputs and outputs are comparatively bounded. It became much harder in the applier because live browser state is open-ended.

The project also created parallel representations of similar concepts:

- two resume profile contracts;
- two resume generators;
- multiple application pipeline generations;
- direct and RPC application orchestration;
- root and ignored duplicate snapshots;
- historical and current database schemas.

A fresh system should reduce the number of authoritative representations.

## 7.3 Evidence should match the claim

Different claims require different proof:

- a pure normalizer can be supported by focused tests;
- a paid API integration needs a controlled real request and observed output;
- a PDF generator needs a real compiled and inspected PDF;
- a browser applier needs a live headed run on a supported page;
- an OMP submission needs a current pre-submit audit, a journaled final action, and post-submit evidence;
- submission safety needs adversarial authorization, stale-ref, retry, and evidence tests.

The repository sometimes treated broad contract coverage as if it proved the whole live workflow. The fresh start should avoid that.

---

# 8. Recommended fresh-start product boundary

A new agent should not recreate this repository wholesale. Start from the product outcomes.

## Phase A: retain the two working anchors

### TheirStack getter

Keep one command path that:

- previews safely;
- requires paid authorization;
- fetches without automatic paid replay;
- normalizes and deduplicates;
- writes a small local backlog;
- records a simple sync audit.

### Canonical resume generator

Keep one command path that:

- selects a queued job read-only;
- uses the structured candidate profile;
- ranks only existing evidence;
- compiles and verifies a one-page PDF;
- publishes the five canonical artifacts.

## Phase B: define a minimal shared backlog contract

The shared contract needs only:

- stable job ID;
- source and source ID;
- canonical URL;
- title/company/location;
- description;
- posting/seen times;
- queue status;
- private source payload.

Do not add application or generated-resume lifecycle fields to the job row itself.

## Phase C: decide whether application drafting is still wanted

If yes, treat it as a new product, not a repair-by-assumption.

First acceptance target:

> On one controlled Greenhouse fixture, a headed browser fills explicit fields, uploads a configured resume, persists concise evidence, authorizes the current final control after a complete audit, performs one OMP submission, and captures its outcome.

Only after that works should the system add:

- live Greenhouse compatibility;
- Lever;
- multi-page navigation;
- optional model assistance;
- durable retries/recovery;
- an external RPC coordinator.

## Phase D: connect resume and application with an adapter

Do not revive the second resume generator merely to feed the applier. Let the canonical generator publish a stable artifact descriptor, then let an application run explicitly select that PDF and verify its hash.

The adapter can be small:

```text
canonical resume artifact
  -> verify manifest and PDF hash
  -> stage one owned copy for application run
```

No profile-schema merger is required.

## Phase E: simplify operator surfaces

A fresh CLI could be organized conceptually as:

```text
jobs preview
jobs sync
jobs list/show/archive
resume generate
apply draft
apply review
```

The names matter less than having one implementation per concept.

---

# 9. File map for a future agent

## Start here

- `PROJECT_HANDOFF.md` — this conceptual handoff;
- `README.md` — current documented product surface, with stale applier status caveat;
- `AGENTS.md` — repository policy and protected-data rules;
- `TODO.md` — historical roadmap/checkpoints, not current status proof;
- `OMP_CMUX_WORKFLOW.md` — development orchestration conventions.

## Working TheirStack/job-getter concept

- `src/jobs_assistant/theirstack.py`;
- `src/jobs_assistant/contracts.py`;
- `src/jobs_assistant/backlog.py`;
- ingestion portions of `src/jobs_assistant/db.py`;
- ingestion portions of `src/jobs_assistant/cli.py`;
- `docs/ingestion.md`;
- `tests/test_theirstack_sync.py`;
- `tests/test_backlog_ingestion.py`.

## Generic feed experiment

- `src/jobs_assistant/job_source.py`;
- feed portions of `src/jobs_assistant/cli.py`;
- `tests/test_job_source.py`;
- feed cases in `tests/test_cli_smoke.py`.

## Working canonical resume generator

- `src/jobs_assistant/resume_generator_command.py`;
- `src/jobs_assistant/resume_generator.py`;
- `src/jobs_assistant/resume_advisor.py`;
- `resume/generator/SKILL.md`;
- `resume/generator/profile.json`;
- `resume/generator/Resume.tex`;
- `docs/resume-generation.md`;
- `tests/test_resume_generator.py`;
- `tests/test_resume_advisor.py`.

## Preserved second resume implementation

- `src/jobs_assistant/resume.py`;
- `src/jobs_assistant/resume_service.py`;
- `src/jobs_assistant/resume_artifacts.py`;
- generated-resume portions of `src/jobs_assistant/db.py` and `cli.py`;
- `tests/test_resume.py`;
- `tests/test_resume_service.py`;
- `tests/test_resume_artifacts.py`;
- `tests/test_resume_db.py`;
- `tests/test_resume_cli.py`.

## Non-working applier design/reference

- `src/jobs_assistant/application.py`;
- `src/jobs_assistant/ats.py`;
- `src/jobs_assistant/safety.py`;
- `src/jobs_assistant/safety_policy.json`;
- `src/jobs_assistant/browser_adapter.py`;
- `src/jobs_assistant/puppeteer_runner.js`;
- `src/jobs_assistant/application_profiles.py`;
- `src/jobs_assistant/application_preferences.py`;
- `src/jobs_assistant/artifacts.py`;
- application portions of `src/jobs_assistant/db.py` and `cli.py`;
- `docs/application-drafts.md`;
- related `tests/test_application_*` and `tests/test_puppeteer_adapter.py` files.

## Non-working RPC/OMP application attempt

- `src/jobs_assistant/application_rpc.py`;
- `src/jobs_assistant/application_rpc_contracts.py`;
- `src/jobs_assistant/omp_rpc.py`;
- `docs/application-rpc.md`;
- `tests/test_application_rpc.py`;
- `tests/test_application_rpc_contracts.py`;
- `tests/test_omp_rpc.py`.

## Historical reference only

- `rpc/archive/README.md`;
- `rpc/archive/REBUILD_PROMPT.md`;
- `rpc/archive/minimized-20260706/applier/`;
- `rpc/archive/old-scraper/`;
- `rpc/archive/old-applier/`;
- ignored `scraper/` and `rpc/` workspace directories.

## Infrastructure

- `pyproject.toml` and `uv.lock`;
- `package.json` and `package-lock.json`;
- `Dockerfile` and `docker-compose.yml`;
- `.env.example`;
- `scripts/smoke.sh`;
- `scripts/container-smoke.sh`;
- `docs/operations.md`.

---

# 10. Final handoff summary

The repository contains a serious attempt to build a private, evidence-driven job-search pipeline. Its strongest usable concepts are at the two ends that have bounded inputs and outputs:

- TheirStack obtains, filters, normalizes, deduplicates, and stores jobs with explicit credit controls.
- The standalone resume generator selects only supported candidate evidence, compiles a one-page resume, validates the artifact, and publishes a reproducible bundle.

The middle application layer attempted far more: live ATS observation, deterministic and model-assisted resolution, browser safety, process ownership, durable evidence, headed browser operation, and a persistent agent coordinator. Its observation, provenance, and action-binding ideas are valuable; the old applier itself is not working and has many errors involved with it.

For a fresh start, do not recreate the breadth of the repository. Preserve the working TheirStack and canonical resume paths, keep the backlog contract small, and treat application automation as a narrowly proven workflow. Carry forward explicit data, provenance, observation before action, private evidence, and OMP submission authorized by a current completeness audit—without carrying forward every layer of accumulated machinery.

## Review basis

This handoff was produced from the maintained root source, focused tests as design evidence, documentation, packaging/container files, repository history, and archived implementation references. Protected SQLite contents, applicant profile contents, resumes, generated artifacts, and application evidence were not opened or modified. No runtime status was inferred from roadmap checkboxes or unexecuted tests. The working/non-working labels in this document follow the status supplied for this handoff.