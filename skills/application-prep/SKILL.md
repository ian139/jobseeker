---
name: application-prep
description: Prepare and submit one complete job application in a headed browser, with private evidence, fresh DOM verification, and automated final submission after audit.
---

# Application preparation

Use this skill for one authorized application URL and one local Phase 1 run. Populate every reachable user-facing field deliberately, preserve owner-private evidence, run the completeness audit via prepareSubmission, then submit. OMP clicks the final action only after prepareSubmission authorizes it.

## Execution posture

Default to immediate execution, not deliberation. Routine execution has priority over analysis: reason only enough to choose the next truthful action, then use the observed result rather than speculation to choose the next action.

For every application, use the canonical operational path:

```text
recover-or-claim
  -> validate job and resume binding
  -> observe
  -> resolve and execute one safe batch of independent routine fields
  -> re-observe, retain, and repair
  -> audit
  -> begin/click/complete submission
  -> persist outcome
  -> next job
```

Use `selectSafeApplicationBatch` for the current observation. A safe batch may contain several independent routine fields; record each semantic action against the shared pre-action observation, then obtain one fresh chained observation and verify retention for every field in the batch. If any action produces unexpected navigation, validation, a blocker, or a DOM change visible to the browser snapshot, stop the batch immediately and re-observe.

Always use a single-action batch for a newly revealed or dependency-marked field, invalid/retry work, file upload, custom widget, select/choice/toggle, non-final navigation, final submission, or any control requiring diagnosis, model inference, or user input. Submission rejection also returns to a fresh observation and repair before another audit.

Do not pause to discuss, summarize, compare alternatives, or request confirmation for routine application decisions. Choose the most direct truthful answer and act. Do not speculate about hypothetical failures or produce progress commentary between ordinary field actions. Do not repeatedly reconsider an already verified field unless a fresh observation or diff marks it stale, invalid, changed, or affected by a newly revealed dependency.

Ask the user only when a required sensitive fact is unavailable; authentication, an assessment, or access control requires their interaction; or no truthful answer can be derived from available evidence. Salary and other sensitive facts remain non-inferable. Final submission still requires `prepareSubmission`. A rejected final action requires a fresh observation, correction of the actual validation or field error, a fresh audit, and then another authorized submission attempt; never blindly repeat the same click.

### Free-text response style

Never use an em dash (`—`) in any generated, adapted, or filled application response. Rewrite the sentence with commas, parentheses, a colon, a semicolon, or separate sentences before calling `tab.fill`. This applies to company-specific answers, reusable answer-memory templates, explanations, and all other free-text responses.

## Canonical OMP browser quick start

After a new session, handoff, or model change, read only this section plus the active durable run record before touching the page. Do not reread `PROJECT_HANDOFF.md` or the complete roadmap during routine startup, and do not reconstruct browser mechanics from conversation history. Reuse the same visible CMUX browser surface and named OMP `browser` tab when alive; call `browser.open` once only when needed, then reuse `browser.run`.

The observer supplies policy-free field state and ledger/evidence refs. The OMP `browser` snapshot supplies live action selectors. Observer refs are never browser selectors. For each planner batch:

The active action hierarchy is strict and serialized:

```text
fresh DOM observation
  -> OMP browser helper
  -> pinned control-specific browser mechanic
  -> freshly grounded visual/computer interaction when the browser paths cannot operate the exact control
  -> fresh DOM observation
```

The visual/computer step is a bounded mechanic fallback, not a second ledger, answer resolver, selector authority, or visual adapter. Re-ground with a fresh screenshot and the current DOM/browser state immediately before it, act on the same visible surface, then obtain the fresh DOM observation before any diagnosis, retry, or next action.

The final submission boundary is DOM-owned. Take `finalRef` only from the current accepted DOM observation, call `prepareSubmission({ finalRef })`, then `beginFinalSubmit` and require its returned ref to equal that exact `finalRef`. Only the OMP `browser` helper or the pinned control-specific mechanic may activate that exact final ref; the visual/computer fallback must never cross this boundary. Re-observe after the attempt and complete it through the coordinator.

CAPTCHA is automatic. Detect and complete it with the OMP `browser` or `computer` tool on the visible CMUX surface, re-grounding with fresh DOM and browser/screenshot state before and after the interaction. CAPTCHA alone never triggers user escalation, a `needs_user` outcome, or a blocked run. Do not drive the CAPTCHA through raw CDP page scripts, `connect_over_cdp`, or ad hoc browser-JS probes; those bypass the visible surface the user owns and frequently target the wrong browser instance. If the OMP browser tool cannot reach the CAPTCHA frame, use the pinned control-specific mechanic, then the OMP `computer` tool on the same visible surface, re-grounding before and after. A missing reCAPTCHA frame on a CDP target is evidence the probe is on the wrong page or browser, not that the challenge is absent from the visible form.

1. Call `selectSafeApplicationBatch` against the latest accepted observation and ledger.
2. Resolve each unit alias with `resolveField` before any browser mutation. A returned multi-unit candidate executes as a batch only after all unit aliases resolve to `memory`, `profile`, or `resume`; otherwise discard the candidate batch and process the first unit singly.
3. Take one fresh `tab.ariaSnapshot()` or `tab.observe()` and uniquely map every planned observer field to its live control.
4. Perform each planned helper action in order, recording each semantic outcome against the shared pre-action observation. Stop early on any unexpected state.
5. Re-run the observer once, accept its diff, and verify retention for every attempted field before planning another batch.

Primary OMP `browser` tool mechanics on the visible CMUX browser surface:

```js
await tab.fill("aria-ref=eNN", exactText);                 // text/textarea; replaces the value
await tab.click("aria-ref=eNN");                           // button, checkbox/radio when a click is needed
await tab.select(exactSelectCss, exactSerializedValue);            // native <select>
await tab.uploadFile(exactFileInputCss, session.runMetadata.resume_upload_path); // <input type=file>
```

Use `"aria-ref=eNN"` inline only with helpers that support it here, including `tab.fill` and `tab.click`. `tab.select` and `tab.uploadFile` do not accept inline ARIA refs: derive a stable exact CSS selector from the observer’s verified `id`, `name`, or supported test-ID attributes, confirm it uniquely identifies the intended native control, and pass that CSS selector.

For every planned custom combobox, use the single `executeCustomSelectOption` path in `src/phase1/custom-select.mjs`; never add field-specific option loops or matching rules. Supply its required `openMenu` callback with the planned `tab.click` followed by `tab.press(selector, "ArrowDown")`, its observation-only `readOptions` callback with the currently visible `[role=option]` records (`key`, rendered text, exposed option value, and disabled state), its one-shot `prepareOptions` callback with the planned `tab.fill(querySelector, exactQuery)`, and its `clickOption` callback with `tab.click` on the uniquely verified exact option selector. The executor always invokes that one generic open sequence, allows the async menu a bounded settle window, checks visible options before issuing the one-shot query, then resolves by normalized exact option text, normalized option value, and only then a unique stable word-boundary substring. Ambiguous, disabled, or timed-out matches fail closed without a click. The fresh observer and canonical retention check remain authoritative after every selection.

`tab.fill(selector, exactText)` takes the answer as its second positional argument. `--value`, `--submit`, command names, option tokens, quotes, or labels are never prefixes to the answer and must never be typed into the field. Do not use `tab.type` for ordinary form values because it appends; use `tab.fill` to replace the value. Do not call `tab.fill` on a `<select>`; use `tab.select`.

For resume upload, map the observer file field to exactly one actual `<input type=file>`, including a hidden input when that is the application’s real file control, derive and uniquely verify its exact CSS selector, then call `tab.uploadFile(exactFileInputCss, session.runMetadata.resume_upload_path)`. `session.runMetadata.resume_upload_path` is the verified canonical absolute path; do not use a potentially relative run-contract path. The helper uses the browser’s native file-input protocol; it is an OMP browser action, not page-JavaScript form mutation. It requires the CSS selector first and one or more file paths after it—`tab.uploadFile(path)` and `tab.uploadFile("aria-ref=eNN", path)` are not supported signatures. Do not click or arm a chooser for this primary path.

Fallback order is strict and follows the hierarchy above:

1. OMP `browser` helper on the visible CMUX browser surface.
2. Pinned Playwright CLI only when the matching browser helper cannot operate the exact control. Use a fresh CLI snapshot and its current `eNN` ref. Text fallback is exactly `playwright-cli fill eNN "<exact text>"`; chooser fallback is click the uniquely mapped upload trigger and immediately run `playwright-cli upload <session.runMetadata.resume_upload_path>`, using that verified canonical absolute path.
3. When neither browser path can operate the exact control, take a fresh DOM/browser snapshot and a fresh desktop screenshot, ground the bounded visual/computer interaction on those views, act on the same visible CMUX browser surface, and immediately obtain a fresh DOM observation. Do not invent a visual adapter or a second state model.

Never use `tab.evaluate`, pinned-CLI evaluation, or injected page JavaScript to set a value, attach a file, click Submit, or bypass a UI control. Evaluation remains observation-only. Regardless of which physical mechanic succeeds, record every semantic action; after a safe routine batch, perform one fresh observer → `acceptObservation` → batch retention chain. Controls excluded from batching retain the single-action chain.

## Fixed authority and privacy boundaries

- Treat `src/phase1/contract.mjs`, `profile.mjs`, `observer.js`, `ledger.mjs`, `audit.mjs`, and `evidence.mjs` as executable authority.
- Consult `skills/playwright-cli/SKILL.md` only when the primary OMP browser helper has concretely failed on an exact control. Do not load it during routine startup, and do not modify the pinned skill.
- Generic examples in the retained Playwright skill are not application mechanics when they conflict with this skill. In particular, its targetless `type`, Enter/`--submit`, and generic upload examples do not override this application skill’s browser-first, exact-selector, no-submit, ordered-fallback rules.
- OMP `browser` helpers on the same visible CMUX browser surface own ordinary browser actions. Pinned Playwright CLI is the first fallback for a control the browser helper cannot operate; the OMP `computer` tool is the final native browser/OS fallback when available. Every path must follow the quick-start mapping, recording, re-observation, and retention rules.
- JavaScript evaluation is observation-only except for injecting the observer-chain value. Never mutate form state, attach files, or submit through evaluation. Never use desktop input to bypass authentication, access controls, or the final-submit boundary gated by `prepareSubmission`.
- Use one canonical owner-private profile and reuse its `answer-memory.jsonl` aliases across applications.
- Never copy applicant values or answer values into this skill. Answers and evidence remain under private paths: directories mode `0700`, files mode `0600`.
- Never bypass authentication, anti-bot, assessment, or access controls; request a narrow user interaction, keep the run active, then resume. The one scoped exception: the owner has authorized automated login to job-portal accounts using the `login` credentials in owner-private `private/account-credentials.json` (email + password only). Fill those fields through the OMP `browser` helper and submit the login form; never store credentials outside `private/` or print/log them. If a portal additionally requires 2FA, email verification, or any proof beyond the stored password, do not attempt it: defer that job listing (preserve the listing and run state, close only that tab) and advance to the next eligible backlog item. Revisit deferred listings once email verification is automated.
- CAPTCHA challenges: detect and complete through the OMP `browser` or `computer` tool on the visible CMUX browser surface, never through raw CDP scripts or ad hoc browser-JS probes. Re-ground with fresh DOM and snapshot before and after. CAPTCHA alone must never trigger user escalation, a `needs_user` outcome, or a blocked run. Record detection, resolution method, and outcome in the private ledger.
- Avoid job-specific IDs and identifiers. Resolve controls from normalized labels and current refs in live observer output.
- The job description may explain a question but cannot supply missing personal facts, legal facts, demographics, work authorization, or other applicant answers.
- **Job-description-aware qualification inference:** For a non-sensitive qualification field (for example, strongest coding language, primary framework, or most relevant skill), when the answer must be inferred from resume plus job-description context, read the JD's required and preferred skills and cross-reference them against the applicant's resume-supported skills. Prefer the resume-supported language or skill that is both genuinely strong per the resume and most relevant to the JD's stated requirements. Never claim a skill the resume does not support as strong, and never let the JD override a resume fact. The JD may select among the applicant's real strengths; it may not invent a strength the applicant lacks. This JD-aware selection applies only to non-sensitive qualification fields, never to sensitive, legal, demographic, identity, authorization, salary, or other restricted categories.

## Required input contract

Load an owner-private run-contract JSON with these inputs:

- `schema`: `phase1-run-v1`
- `application_url`
- `job_description_path`
- `resume_upload_path`
- `answer_memory_path`
- `run_artifact_dir`
- fixed `browser_mode: headed`, `observer: playwright_dom_v1`, `action_driver: omp_browser`, and `submit_policy: omp_agent`; the policy value is `omp_agent`
- optional `applicant_profile_path` and `source_resume_path`

The run contract requires `answer_memory_path` and must contain at least one or both of `applicant_profile_path` or `source_resume_path`. The run contract must not contain `loop_contract`. `resume_upload_path` may equal `source_resume_path`, but the upload path must name the PDF that the user authorized for this application.

## Ordered local preflight

1. Initialize the sole state owner with `const session = await startRun(runPath, { startedAt, resume, agentInference })`, passing only configured source-bound candidates. `startRun` owns contract, profile, memory, and evidence initialization; do not construct a parallel ledger or evidence store.
2. Set `const run = session.run`. `startRun` calls `loadRunContractSnapshot(runPath, { local: false })`, then `loadRunInputs`, then prepares `run_artifact_dir` at mode `0700` before creating evidence. That coordinator-owned sequence enforces the checks and ordering represented by parse-only `loadRunContract(runPath, { local: false })` followed by `validateRunContractLocal` before evidence or browser use; callers must not invoke those wrappers independently. It also loads the configured profile and answer memory through owner-private APIs, canonicalizes the upload path, and computes the run-contract SHA-256 and resume-upload SHA-256.
Equivalent invariant only: `loadRunContract(runPath, { local: false })` for parse-only discovery; prepare `run_artifact_dir` at `0700`; enforce `validateRunContractLocal`; then initialize evidence. `startRun` owns those stages.
3. Keep the coordinator's fixed run metadata contract: `phase1-run-evidence-v1` run metadata uses exactly `schema`, `application_url`, `run_contract_sha256`, `resume_upload_path`, `resume_upload_sha256`, `browser_mode`, `observer`, `action_driver`, `submit_policy`, `loop_contract`, and `started_at`. New runs emit `safe-batch-observe-act-reobserve`. Recovery accepts immutable evidence that already carries the legacy `one-field-observe-act-reobserve` value; never rewrite an existing run's metadata.
`startRun` resolves `resume_upload_path` to its absolute canonical path for evidence run metadata.
4. Open `run.application_url` with the OMP `browser` tool in the headed visible CMUX surface. Remain on that application origin unless an observed, application-owned interaction navigates within its flow.
5. Clear `__omp_phase1_previous_observation_id_v1` before the initial observer evaluation; before every later IIFE, set or inject `__omp_phase1_previous_observation_id_v1` to the latest accepted observation ID.
`__omp_phase1_previous_observation_id_v1` carries the previous observation ID for the chained observer.
6. Evaluate the exact `src/phase1/observer.js` IIFE and capture its returned observation value as `initialObservation`.
7. Publish it through the coordinator: `const initial = await acceptObservation(session, initialObservation); let ledger = initial.ledger;`. `acceptObservation` internally applies `createLedger(initial observation)` and records the observation, diff, and ledger as one state transition.
8. Produce a preflight field inventory from every reachable observer field: stable field ID, normalized label, type, required/sensitive state, ref, current validity, and candidate class. Use normalized labels only with the live observer, never as a substitute for current refs.

Stop and repair preflight failures before any field mutation. Do not invent a fallback contract, observer result, field, evidence artifact, or session.
### Durable operator command path

Use `node src/phase1/application-cli.mjs` as the repository-owned process boundary for a live run. Do not build ad hoc `node -e` session wrappers or keep a second in-memory ledger. Commands are `accept-observation`, `plan`, `pending-plan`, `complete-action`, `verify-retention`, `prepare-submit`, `begin-submit`, `complete-submit`, and `finalize`; each accepts only the flags declared by `application-cli.mjs`. All request, plan, result, observation, proof, submit-attempt, and screenshot inputs remain in owner-private files. The `plan` request must satisfy `schemas/browser-action-request.schema.json`; the emitted plan and supplied result must satisfy `schemas/browser-action-plan.schema.json`.

Before replaying any browser action after interruption, run `pending-plan`. Receipt-first recovery consumes a durable completed result without replay; only the returned pending plan may be acted. For Greenhouse education controls, use the request's bounded `greenhouse_education` option resolver rather than embedding a job-specific option ID.

`prepare-submit` is an automated internal gate. When it returns `authorized: true`, immediately run `begin-submit`, activate only its returned ref, capture a fresh observation, and run `complete-submit`; do not request human confirmation or park for manual review.


## Resolve, act, and verify loop

Repeat this coordinator loop with `selectSafeApplicationBatch({ observation: session.observation, ledger: session.ledger })`.

Observer `ref` values such as `observation-…:control-N` are ledger/evidence identities, not browser targets. Before a batch, take a fresh OMP `browser` snapshot and map every planned observer control to exactly one live selector. If any mapping is missing or ambiguous, stop and replan that field as a single diagnosis unit.

1. Accept the planner's ordered batch. `mode: 'batch'` is the DEFAULT for independent ordinary text-like controls; `mode: 'single'` preserves the existing priority for every hazardous or ambiguous next unit. Batch whenever the current observation permits: prefer 2-3 independent stable text fields per `recordActionBatch` cycle instead of single-field cycles, so each fresh observation and retention check covers multiple fields. Never batch custom selects, native selects, uploads, checkboxes/radios, dependent/conditional fields, fields revealed by a parent answer, navigation, or any control that previously failed; those stay single.
2. Resolve each planned field alias with `resolveField` before any browser mutation. Execute a returned multi-unit candidate as a batch only after all unit aliases resolve to `memory`, `profile`, or `resume`. Otherwise discard the candidate batch and process the first unit singly, then replan. Invoke typed agents only for ambiguous agent inference, control diagnosis after a concrete mapping/action failure, or repository repair after defect classification.
3. Execute planned actions in order from the same fresh browser snapshot and stop on the first non-success. Publish every actually attempted routine fill atomically with `recordActionBatch(session, attempts)`: zero or more successful fills followed by at most one terminal attempted/failed/retry/blocked fill. A lone attempted fill uses `recordAction`. Evidence publication remains inside coordinator APIs, not a separate operator step.
4. Stop the remaining batch immediately if an action fails, browser state indicates navigation or validation, or a control can no longer be uniquely mapped.
5. Obtain one fresh chained DOM observation, publish it with `acceptObservation`, inspect the diff, and call `verifyRetention` with proofs covering every attempted field.
6. Retry only failed/stale fields. Any newly revealed field, invalid field, upload, widget, choice, navigation, or final candidate is processed singly before routine batching resumes.
7. Continue only when every attempted field is deliberate, valid, retained, and represented by the coordinator's current ledger.

For any non-file field whose latest observer-semantic value or deliberate blank already matches the resolution, perform no mutation and create no action attempt. Include it in the next fresh observation and retention proof. If it changed, use the normal planner path; if a mismatching field is readonly, stop with a blocker.

### Retention means committed DOM state, never input text

A field is retained only when the fresh observer reports a committed selection for that control: a selected option with the menu closed, a checked state, a value, or a deliberate blank proven by the UI. The search/filter input text of a combobox is never retention evidence; a select whose search input holds text but whose menu is closed with no committed option is empty. Never report, ledger, or rely on `value_present` derived from transient input text. If the observer or ledger claims retained while the visible DOM select shows a placeholder, the retention signal is wrong: stop, correct the observer/retention boundary, and only then continue.

### Bisect before building another mechanism

After two materially identical failed attempts on the same control class (for example, a custom select that closes without committing), do not write a third filling script. Run one stepwise bisection on a single representative control: open, fill, option visible, click option, menu close, re-render settle, fresh observer. Capture committed state (input value, `aria-selected`, selected option text, observer selected count, `value_present`) at every step. Fix the first boundary where the committed selection is lost, then apply the verified sequence to the remaining controls. A rejected final action is also a retention failure: re-scan every required control in the live DOM, not the ledger, because the ATS validates the actual form.

### No new per-field mechanisms

Any new helper script, selector scheme, or matching rule for an existing control class is a defect in the shared executor or observer, not a field fix. Route every custom select through `executeCustomSelectOption`; extend the shared executor or observer once, then re-run the same control across the form.


When no unresolved field remains on a non-final page, select the current `non_final_navigation` candidate only when exactly one candidate has an exact normalized application-entry or forward label: Apply, Apply for this Job, Easy Apply, Apply on Company Website, Continue, Next, Proceed, Review, Start Application, Begin Application, Get Started, or Go to Application. A plainly identified entry control may be activated autonomously; never activate a helper or any `final_candidate`. A nested anchor-and-button pair is one entry control only when both candidates have the same exact allowed label, the anchor contains the button, and the anchor has one same-origin application-path `href`; bridge and activate the anchor, never the nested button. Otherwise require exactly one candidate. Activate the uniquely bridged live ref, then publish `recordAction(session, { action: 'non_final_navigation', field_id: null, ref: current ref, observation_id: latest observation_id, outcome })` and re-observe through `acceptObservation(session, observation)`.

Record each retry before correcting and retrying. A failed routine fill at the end of a multi-action batch is the terminal item in the same atomic `recordActionBatch`; a standalone failure uses `recordAction(session, attempt)`. Set `retry_of` to the prior attempt's nonnegative integer sequence/index, never its string `action_id`; never overwrite or hide the failure. After a batch stops, accept a fresh observation before any further resolution or action.
When a final-action attempt is rejected or leaves the application page unchanged, do not classify the job as closed or unavailable and do not close its browser surface. Capture the outcome, obtain a fresh chained observation, and re-scan the live DOM for the actual validation or unresolved-control cause: Greenhouse and similar ATS validate the real form, so check every required control's committed state in the DOM (custom selects showing placeholders, empty required inputs, missing uploads), not the ledger's claims. Resolve and retain each found field through the planner's single-action repair mode, then return to the pre-submit boundary. Only explicit live evidence that the posting is unavailable may end the run as closed.

For every null-digest semantic choice or deliberate blank, `source` must be only `memory`, `profile`, or `user`; never use `resume` or agent inference.

## UI mechanics recipes

These recipes select mechanics, not applicant answers. Always operate from the latest observer output and retain its current refs for ledger/evidence identity. Use a uniquely mapped live OMP `browser` selector on the visible CMUX browser surface and the helper matching the control. Pinned CLI refs are fallback-only; targetless pinned CLI `upload <absolute path>` is allowed only immediately after its uniquely mapped trigger has armed a chooser. The OMP `computer` tool is last and only for a remaining native browser/OS interaction.

### Ordinary editable controls

Apply the select and choice role recipes below before this shape check. For an editable observer whose role is not combobox, listbox, checkbox, radio, or switch and whose shape is `kind: input` with type other than checkbox, radio, or file; `kind: textarea`; `kind: contenteditable`; or fillable `kind: aria`, role `textbox`, use `await tab.fill(exactSelector, exactText)`. The second argument is only the intended answer. Never prepend `--value`, `--submit`, a command name, label, or option token, and never use `tab.type` for ordinary form values. The pinned-CLI fallback is exactly `playwright-cli fill eNN "<exact text>"` without `--submit`. Serialize date, time, month, week, datetime-local, and numeric values in the exact native format accepted by the control.

After planner-approved routine fills, publish two or three successful semantic `fill` attempts atomically with `recordActionBatch`; a lone fill uses `recordAction`. Then obtain one fresh chained observation through `acceptObservation` and call `verifyRetention` for all attempted fields. Use the coordinator's verified no-mutation path when the current value already matches. Stop on readonly mismatch or when neither the uniquely mapped browser helper nor its documented pinned-CLI fallback supports targeted fill; do not fall back to targetless keystrokes, DOM mutation, or submission. If a reachable field matches no recipe by emitted kind, tag, type, and role, stop at preflight with an unsupported-mechanic blocker.

### Native select, React Select, and ARIA listbox

Stage order for a custom control is owned by the shared executor; native controls use their native helper directly.
For a native single `<select>` (`kind: select`, `tag: select`, role `combobox`), derive and uniquely verify its exact CSS selector and use `await tab.select(exactSelectCss, exactSerializedValue)`. For a native multi-select, pass all intended serialized values in option order to the same helper. Inline `aria-ref=eNN` is not supported by `tab.select`. After the physical result, publish `recordAction(session, selectAttempt)`, obtain a fresh observation, publish `acceptObservation(session, observation)`, and verify retention. The pinned-CLI single-select fallback is `playwright-cli select eNN "<exact serialized value>"`.
Use the pinned-CLI native multi-select procedure below only when OMP browser `tab.select(exactSelectCss, ...intendedValues)` cannot operate the exact native control. Canonicalize the intended value set into serialized option order. If it is already exact, use the verified no-mutation path. Otherwise require unique exact option refs scoped to that native listbox. Before every option click, rebuild `selectAttempt` from the current `session.observation` and current ledger field. For a nonempty target, click its first intended option without a modifier to clear prior selection and establish the first value. For each remaining intended value, remap from a fresh snapshot, run pinned CLI `keydown Meta`, click that exact option ref, and always run `keyup Meta` immediately afterward even when the click fails. For an empty target, apply the same keydown/click/guaranteed-keyup sequence to each currently selected option to remove it. Never evaluate or observe between keydown and keyup. After every option click, release Meta when applicable, record the attempt, obtain a fresh chained observation, and remap the next option. Verify retention only after the selected values equal the exact intended set.

For any observer control with role `combobox` or `listbox` other than a native `kind: select`, `tag: select` control, call `executeCustomSelectOption` exactly as defined in the canonical quick-start section. Its required callbacks are the only allowed custom-select execution path: `openMenu` performs the planned click plus targeted `ArrowDown`, `readOptions` inventories the currently visible enabled/disabled option records, `prepareOptions` performs at most one planned `tab.fill(querySelector, exactQuery)`, and `clickOption` clicks the executor-selected exact option selector. The executor alone applies normalized exact text, then normalized option value, then unique stable word-boundary substring matching. Do not add an exact-label loop, `waitForFunction` matcher, field-specific fallback, targetless keyboard action, alternate query helper, or alternate partial-label rule. Missing, duplicate, disabled, stale, and option-read/poll timeouts fail closed without a click. A callback deadline is an indeterminate action failure, not proof that no action occurred; obtain a fresh chained observation before a changed retry. After a successful physical selection, publish `recordAction`, obtain one fresh observation, publish `acceptObservation`, and verify retention.

### File uploads

The retention proof fields are {
- `value_digest`
- `action_id`
- `file_name`
}

Primary path: map the observer file field to exactly one actual `<input type=file>` in the same frame and field/form group, including its hidden native input when applicable; derive and uniquely verify its exact CSS selector; then run `await tab.uploadFile(exactFileInputCss, session.runMetadata.resume_upload_path)` through the OMP `browser` tool on the visible CMUX browser surface. Inline `aria-ref=eNN` is not supported by `tab.uploadFile`. Do not click or arm a chooser first. If the browser helper cannot operate the exact file input, use the pinned-CLI chooser fallback with that same verified canonical absolute path: map one visible field-associated upload trigger, click its current CLI ref, then immediately run `playwright-cli upload <session.runMetadata.resume_upload_path>`. If that also cannot complete a still-open native chooser and the OMP `computer` tool is available, re-ground from fresh browser and desktop snapshots and operate that chooser on the same visible CMUX browser surface. Stop on missing or ambiguous inputs/triggers. None of these paths creates an intermediate semantic action; record only the upload result.

After the physical upload returns, build `uploadAttempt` against the pre-upload observer field/ref/observation with semantic action `upload` and its real success or failure. Publish it with `const uploaded = await recordAction(session, uploadAttempt); ledger = uploaded.ledger;`, then obtain a fresh observation and publish `const accepted = await acceptObservation(session, observation); ledger = accepted.ledger;`. Record a failed attempt and retry through the ordered mechanics before remapping; never claim success when the upload failed.

Bind `uploadActionId` to the successful, non-stale upload attempt's exact `action_id` for the current field/ref. Confirm file count and basename from the fresh file control. `verifyRetention` receives the field-ID keyed map `{ [field_id]: { value_digest, action_id, file_name } }`, using that same `uploadActionId` as proof `action_id`.

The proof `value_digest` must equal the field resolution `value_digest`, not the file or resume-upload SHA-256 identity digest. Keep the proof map for every later retention check and final observation; an upload disappearing on a later page is a blocker.

### Checkbox groups

Resolve every reachable checkbox or switch option, not only selected options. When observed `checked` differs from the intended state, use `tab.click(exactSelector)` on the uniquely mapped native or custom control. In pinned-CLI fallback, use idempotent `check eNN` or `uncheck eNN` for native inputs and click the exact ref for custom ARIA choices. Do nothing when the observer already proves the intended state.

After a mutation, publish semantic action `check` or `uncheck` with `recordAction(session, attempt)`, obtain a fresh observation through `acceptObservation(session, observation)`, and verify retention. When the option already matches, create no action record; publish one fresh observation and use the no-mutation retention path. For an intentionally unchecked option, use `value_digest` null and `semantic_choice` `none` only when that fresh UI state proves it unchecked.

For every null-digest `none` choice, use only `memory`, `profile`, or `user`; `resume` and agent inference are not allowed. A grouped field passes only when every reachable option has a deliberate retained state.

### Radio groups

Group live radio controls by `group_id`, or by `frame_id` plus `name` when no group ID exists. Resolve exactly one intended option in each group. If it is not already selected, use `tab.click(exactIntendedRadioSelector)`; in pinned-CLI fallback use idempotent `check eNN` for a native radio or click the exact ref for a custom ARIA radio.

After a mutation, publish semantic action `check` for the native radio or `select` for the custom radio with `recordAction(session, attempt)`, then obtain a fresh observation through `acceptObservation(session, observation)` and verify retention. When the intended radio is already selected, create no action record and use the fresh-observation no-mutation path. Require exactly one deliberate, valid, retained selection, then treat every unselected sibling in that current or historical group as group-satisfied rather than independently actionable; never click alternatives merely because their individual ledger entries remain unresolved.

### Dynamic fields

After changing a parent, re-observe before any child action and add every revealed child to the inventory. Complete a revealed child with a deliberate, valid, retained state before changing or hiding it; leaving it unresolved or disappearing creates historical debt.
Every historically reachable field must be deliberate, valid, and retained; no final-review waiver or disposition exists.

### Country selector

Call `resolveField(session, { field_id, alias: 'profile.address.country', sensitive: true })` so country can resolve only from memory, profile, or user evidence: memory remains first globally; within the profile, structured `address.country` precedes the `profile.answers` entry. Use the React Select sequence when applicable, match one exact live option, click it, and verify the retained observer value.

### Relocation fields

Use the canonical private `profile.relocation` policy together with the verified job/application destination. The user is willing to relocate for every destination within the United States: answer an exact relocation-willingness question affirmatively when the destination is verified as US-based, including a generic willingness question on a verified US-based job. Answer negatively when the requested destination is explicitly outside the US. If the destination cannot be established from the job snapshot or current application, keep the field unresolved until the destination is known; never turn the scoped US policy into an unrestricted relocation answer. Do not infer relocation timing, assistance, or support requirements that the user did not provide.

### Salary and compensation fields

Treat every salary, compensation, pay, wage, or rate control as sensitive. Resolve it only from an exact `memory`, `profile`, or `user` value; never use resume facts, job-description text, market data, or agent inference. Require one exact dollar amount. For a text-capable control, serialize it as one `$` immediately followed by the numeric amount, with comma thousands separators and cents only when they are part of the supplied amount (for example, `$120,000`). For a native numeric control, use only the equivalent canonical digits accepted by that control because `$` and commas are invalid native numeric input; add no display text. In either case enter nothing else: no spaces, currency code, range, pay-period or unit suffix, explanatory prose, or negotiation qualifier. Re-observe and require retained validity after filling. If the available answer is a range, shorthand, or otherwise cannot be reduced to one exact supplied amount without choosing or inferring a value, keep the field unresolved and request the exact amount.

### Education fields

Treat school, degree, discipline, dates, and each repeated education row as separate fields. Use exact owner-private facts, select exact options, and re-inventory after adding or removing a row.

### Demographic selects

Call `resolveField(session, { field_id, alias, sensitive: true })` before source selection for every demographic control. Use only an explicit profile, memory, or user answer, including an explicit prefer-not-to-answer option; do not infer an answer from names, location, resume, or job description.

### Conditional questions

Answer the parent first, re-observe, inventory all newly reachable questions, and complete each child before further navigation. A hidden child remains historical ledger state and must already be deliberate, valid, and retained.

## Final audit and OMP submission

1. Capture `finalObservation` through the normal chained observer path and publish it with `const finalAccepted = await acceptObservation(session, finalObservation); ledger = finalAccepted.ledger;`.
2. Run `const retention = await verifyRetention(session, proofs); ledger = retention.ledger;` with the complete field-ID upload proof map. Stop unless `retention.ok` is true and `retention.retry_required` is false.
3. Select the visible Submit control's current ref from the final observation. Call `const authorized = await prepareSubmission(session, { finalRef });` and require `authorized.authorized` to be true and `authorized.authorizedFinalRef === finalRef`.
4. Before any browser click, call `const begun = await beginFinalSubmit(session);` and require `begun.ref === finalRef`. Map `begun.ref` to exactly one live browser selector, then activate it with `tab.click(finalSelector)` through the OMP `browser` tool on the visible CMUX browser surface. If the browser helper cannot operate the exact control, pinned CLI may click its freshly mapped current ref; the OMP `computer` tool must never cross the final-submit boundary.
5. Re-observe the resulting page even when the click helper reports a timeout or error, then call `completeFinalSubmit(session, { attemptId: begun.attemptId, outcome, errorCode })` exactly once with the observed terminal outcome. Never click before `beginFinalSubmit`, and never leave a begun attempt unresolved. A failed or blocked attempt requires a fresh chained observation and a fresh `prepareSubmission` audit before retrying; never reuse an authorization across observations.
6. After one final-submit attempt succeeds, capture the post-submit screenshot into the owner-private artifact directory and call `const finalized = await finalizeRun(session, { screenshotPath, finalUrl: postSubmitObservation.url });`. Require `finalized.finalized` to be true. Persist `completed` only from the validated canonical `completion.json`; SQLite derives every submission attempt and the exact count from its paired journal events.
7. Leave the headed browser open after submission long enough to capture post-submit evidence. Report only private artifact paths and blockers, never applicant values or answer contents.
## Run outcome classification

After finalization or when a run cannot proceed, persist exactly one terminal outcome:

- **`completed`** — Every reachable field is deliberate, valid, and retained; the final audit passes with no blockers; OMP begins, performs, and resolves the final action after `prepareSubmission` authorizes it; and validated canonical `completion.json` evidence is persisted. This is the only successful outcome and requires no human approval.
- **`needs_user`** — A required truthful personal fact is missing from profile, memory, and resume, and agent inference is prohibited (sensitive field, identity, authorization, protected-class, salary/compensation, date, credential, or other restricted category). The browser surface and evidence remain active; the run can resume after the user supplies the fact. Also use when an observer-covered required field cannot be resolved because no answer source is available.
- **`blocked`** — The page presents a hard safety blocker that cannot be resolved by the authorized owner credentials or by deferral: non-CAPTCHA anti-bot challenge, assessment/integrity check, or an inaccessible frame. These require narrow human resolution and must never be bypassed. Also use when an unknown visible control cannot be resolved or a submission was observed outside the canonical protocol; record the latter as `noncanonical_submission_receipt` with an unknown count represented as `NULL`. Authentication with the owner's stored login credentials is automated per the login policy; portals requiring 2FA/email verification beyond the stored password are deferred listings, not `blocked`.
- **`closed`** — Only explicit live evidence that the posting is unavailable: HTTP 404/410, a page-level "job not found" or "position filled/closed" message, or a redirect away from the application. Never use for form validation failures, retention errors, or rejected submissions.
- **`failed`** — A bounded infrastructure or evidence-integrity failure that cannot be recovered by retry (e.g., evidence store corruption, unrecoverable I/O error). Routine form completion debt, validation errors, and non-accepted final actions are never `failed`.

Only persist a terminal outcome when the run genuinely cannot continue or has completed submission.

## Authoritative persistent supervised loop

Run application work in one persistent, serialized OMP loop. Do not create a daemon, parallel job worker, second application state owner, or second browser loop.

1. On session startup, restart, or an advisory wake, call `recoverPrepareOrClaimBacklogRun`. It is the sole resume-preparing startup entry: recover the active run first; otherwise prepare and atomically claim one job. SQLite state is authoritative, not the wake file.
2. While a run is active, call `heartbeatActiveRun` on a recurring interval strictly shorter than half its lease duration. A stale owner or browser session must stop immediately when heartbeat fencing fails.
3. When a truthful required answer is unavailable, call `pauseRunForUser` so SQLite records `needs_user` while preserving the same active run, browser surface, ledger, evidence directory, and resume binding. After the verified answer is persisted, call `resumeNeedsUserRun` before browser work; never claim another job around the paused run.
4. Persist exactly one canonical terminal outcome through `persistTerminalOutcome`. After persistence succeeds, immediately call `recoverPrepareOrClaimBacklogRun` again and process the next eligible job without sleeping.
5. Only when startup returns `kind: 'idle'`, call `waitForOmpWake({ flagPath, pollIntervalMs, timeoutMs, signal })`. A returned wake means rerun startup; an idle timeout means rerun startup before waiting again. The wake is advisory and may be coalesced.
6. `consumeOmpWake` atomically claims one sentinel. Malformed, unknown-key, symlinked, or oversized wake data fails closed with `E_OMP_WAKE_INVALID` after only the claimed sentinel is removed. Reinspect SQLite after the failure; never derive application state from wake contents.

All final-submit gates, canonical evidence requirements, one-active-job enforcement, and visible CMUX browser ownership rules remain unchanged.

## After each job

After finalizing and persisting the terminal outcome:

1. Confirm evidence and database state are complete.
2. Record any newly supplied user answers in private answer memory.
3. Close only the completed job’s browser surface.
4. Inspect the queue and immediately begin the next eligible job.

Do not create a handoff merely because one job finished. Handoff only when:
- context has become noisy or unreliable;
- the browser/runtime must restart;
- the model or session is intentionally changing;
- an unresolved external blocker requires pausing the run.

A handoff must cite the claimed job, persisted outcome, private workspace, current visible CMUX browser surface and named OMP `browser` tab, latest accepted observation ID, ledger/evidence paths, verified resume-upload path and retention state, latest verification, blocker, and single next action. The receiving agent must reread `Handoff-safe OMP browser quick start` before any browser action instead of reconstructing mechanics from the handoff narrative.
