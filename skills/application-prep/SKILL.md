---
name: application-prep
description: Prepare and submit one complete job application in a headed browser, with private evidence, fresh DOM verification, and automated final submission after audit.
---

# Application preparation

Use this skill for one authorized application URL and one local Phase 1 run. Populate every reachable user-facing field deliberately, preserve owner-private evidence, run the completeness audit via prepareSubmission, then submit. OMP clicks the final action only after prepareSubmission authorizes it.

## Execution posture

Default to immediate execution, not deliberation. Routine execution has priority over analysis: reason only enough to choose the next truthful action, then use the observed result rather than speculation to choose the next action.

For every application:

1. Observe the current fields.
2. Fill routine fields from memory, profile, resume, or supported inference.
3. Select the obvious exact option.
4. Re-observe and verify retention.
5. Continue until no unresolved reachable field remains.
6. Run the completeness audit.
7. Submit when authorized.
8. If submission exposes validation errors, re-observe, correct them, rerun the audit, and submit again.
9. Continue until submission succeeds or a genuine user-only blocker is observed.

Do not pause to discuss, summarize, compare alternatives, or request confirmation for routine application decisions. Choose the most direct truthful answer and act. Do not speculate about hypothetical failures or produce progress commentary between ordinary field actions. Do not repeatedly reconsider an already verified field unless a fresh observation or diff marks it stale, invalid, changed, or affected by a newly revealed dependency.

Ask the user only when a required sensitive fact is unavailable; authentication, an assessment, or access control requires their interaction; or no truthful answer can be derived from available evidence. Salary and other sensitive facts remain non-inferable. Final submission still requires `prepareSubmission`. A rejected final action requires a fresh observation, correction of the actual validation or field error, a fresh audit, and then another authorized submission attempt; never blindly repeat the same click.

### Free-text response style

Never use an em dash (`—`) in any generated, adapted, or filled application response. Rewrite the sentence with commas, parentheses, a colon, a semicolon, or separate sentences before calling `tab.fill`. This applies to company-specific answers, reusable answer-memory templates, explanations, and all other free-text responses.

## Handoff-safe OMP browser quick start

After a new session, handoff, or model change, reread this section before touching the page. Do not reconstruct browser mechanics from conversation history. Reuse the same visible cmux application surface and the same named OMP `browser` tab when it is still alive; call `browser.open` once when needed, then reuse `browser.run`. Confirm the browser tool is attached to the intended cmux surface rather than silently opening a separate application.

The observer supplies policy-free field state and ledger/evidence refs. The OMP browser snapshot supplies live action selectors. An observer ref is never a browser selector. For each single field:

1. Accept the latest observer result and choose the next unresolved field.
2. Resolve its truthful value through the coordinator.
3. Take a fresh `tab.ariaSnapshot()` or `tab.observe()` and map that observer field to one exact live control.
4. Perform one matching OMP browser helper action.
5. Record the semantic action against the pre-action observer field/ref.
6. Re-run the observer, accept its diff, and verify retention before continuing.

Primary OMP browser mechanics:

```js
await tab.fill("aria-ref=eNN", exactText);                 // text/textarea; replaces the value
await tab.click("aria-ref=eNN");                           // button, checkbox/radio when a click is needed
await tab.select(exactSelectCss, exactSerializedValue);            // native <select>
await tab.uploadFile(exactFileInputCss, session.runMetadata.resume_upload_path); // <input type=file>
```

Use `"aria-ref=eNN"` inline only with helpers that support it here, including `tab.fill` and `tab.click`. `tab.select` and `tab.uploadFile` do not accept inline ARIA refs: derive a stable exact CSS selector from the observer’s verified `id`, `name`, or supported test-ID attributes, confirm it uniquely identifies the intended native control, and pass that CSS selector.

`tab.fill(selector, exactText)` takes the answer as its second positional argument. `--value`, `--submit`, command names, option tokens, quotes, or labels are never prefixes to the answer and must never be typed into the field. Do not use `tab.type` for ordinary form values because it appends; use `tab.fill` to replace the value. Do not call `tab.fill` on a `<select>`; use `tab.select`.

For resume upload, map the observer file field to exactly one actual `<input type=file>`, including a hidden input when that is the application’s real file control, derive and uniquely verify its exact CSS selector, then call `tab.uploadFile(exactFileInputCss, session.runMetadata.resume_upload_path)`. `session.runMetadata.resume_upload_path` is the verified canonical absolute path; do not use a potentially relative run-contract path. The helper uses the browser’s native file-input protocol; it is an OMP browser action, not page-JavaScript form mutation. It requires the CSS selector first and one or more file paths after it—`tab.uploadFile(path)` and `tab.uploadFile("aria-ref=eNN", path)` are not supported signatures. Do not click or arm a chooser for this primary path.

Fallback order is strict:

1. OMP `browser` helper on the same cmux surface.
2. Pinned Playwright CLI only when the matching browser helper cannot operate the exact control. Use a fresh CLI snapshot and its current `eNN` ref. Text fallback is exactly `playwright-cli fill eNN "<exact text>"`; chooser fallback is click the uniquely mapped upload trigger and immediately run `playwright-cli upload <session.runMetadata.resume_upload_path>`, using that verified canonical absolute path.
3. `computer` only when it is available and the remaining interaction is a native browser/OS surface neither browser nor pinned CLI can operate, such as a still-open file chooser. Take a fresh DOM/browser snapshot and a fresh desktop screenshot first, act on the same visible cmux surface, then immediately re-observe through the coordinator.

Never use `tab.evaluate`, pinned-CLI evaluation, or injected page JavaScript to set a value, attach a file, click Submit, or bypass a UI control. Evaluation remains observation-only. Regardless of which physical mechanic succeeds, the same `recordAction` → fresh observer → `acceptObservation` → `verifyRetention` chain is mandatory.

## Fixed authority and privacy boundaries

- Treat `src/phase1/contract.mjs`, `profile.mjs`, `observer.js`, `ledger.mjs`, `audit.mjs`, and `evidence.mjs` as executable authority.
- Read `skills/playwright-cli/SKILL.md` before opening the browser. Do not modify that pinned skill.
- Generic examples in the retained Playwright skill are not application mechanics when they conflict with this skill. In particular, its targetless `type`, Enter/`--submit`, and generic upload examples do not override this application skill’s browser-first, exact-selector, no-submit, ordered-fallback rules.
- OMP `browser` helpers on the same visible cmux surface own ordinary browser actions. Pinned Playwright CLI is the first fallback for a control the browser helper cannot operate; `computer` is the final native browser/OS fallback when available. Every path must follow the quick-start mapping, recording, re-observation, and retention rules.
- JavaScript evaluation is observation-only except for injecting the observer-chain value. Never mutate form state, attach files, or submit through evaluation. Never use desktop input to bypass authentication, access controls, or the final-submit boundary gated by `prepareSubmission`.
- Use one canonical owner-private profile and reuse its `answer-memory.jsonl` aliases across applications.
- Never copy applicant values or answer values into this skill. Answers and evidence remain under private paths: directories mode `0700`, files mode `0600`.
- Never bypass authentication, anti-bot, assessment, or access controls; request a narrow user interaction, keep the run active, then resume.
- CAPTCHA challenges: detect and complete through normal browser/computer interaction. CAPTCHA alone must never trigger user escalation, a `needs_user` outcome, or a blocked run. Record detection, resolution method, and outcome in the private ledger.
- Avoid job-specific IDs and identifiers. Resolve controls from normalized labels and current refs in live observer output.
- The job description may explain a question but cannot supply missing personal facts, legal facts, demographics, work authorization, or other applicant answers.

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

1. Initialize the sole state owner with `const session = await startRun(runPath, { startedAt, resume, supportedInference })`, passing only configured source-bound candidates. `startRun` owns contract, profile, memory, and evidence initialization; do not construct a parallel ledger or evidence store.
2. Set `const run = session.run`. `startRun` calls `loadRunContractSnapshot(runPath, { local: false })`, then `loadRunInputs`, then prepares `run_artifact_dir` at mode `0700` before creating evidence. That coordinator-owned sequence enforces the checks and ordering represented by parse-only `loadRunContract(runPath, { local: false })` followed by `validateRunContractLocal` before evidence or browser use; callers must not invoke those wrappers independently. It also loads the configured profile and answer memory through owner-private APIs, canonicalizes the upload path, and computes the run-contract SHA-256 and resume-upload SHA-256.
Equivalent invariant only: `loadRunContract(runPath, { local: false })` for parse-only discovery; prepare `run_artifact_dir` at `0700`; enforce `validateRunContractLocal`; then initialize evidence. `startRun` owns those stages.
3. Keep the coordinator's fixed run metadata contract: `phase1-run-evidence-v1` run metadata uses exactly `schema`, `application_url`, `run_contract_sha256`, `resume_upload_path`, `resume_upload_sha256`, `browser_mode`, `observer`, `action_driver`, `submit_policy`, `loop_contract`, and `started_at`. Its `loop_contract` is `one-field-observe-act-reobserve`; that is evidence metadata, never a run-contract key.
`startRun` resolves `resume_upload_path` to its absolute canonical path for evidence run metadata.
4. Read `skills/playwright-cli/SKILL.md` before browser use, then open `run.application_url` in a headed and visible browser. Remain on that application origin unless an observed, application-owned interaction navigates within its flow.
5. Clear `__omp_phase1_previous_observation_id_v1` before the initial observer evaluation; before every later IIFE, set or inject `__omp_phase1_previous_observation_id_v1` to the latest accepted observation ID.
`__omp_phase1_previous_observation_id_v1` carries the previous observation ID for the chained observer.
6. Evaluate the exact `src/phase1/observer.js` IIFE and capture its returned observation value as `initialObservation`.
7. Publish it through the coordinator: `const initial = await acceptObservation(session, initialObservation); let ledger = initial.ledger;`. `acceptObservation` internally applies `createLedger(initial observation)` and records the observation, diff, and ledger as one state transition.
8. Produce a preflight field inventory from every reachable observer field: stable field ID, normalized label, type, required/sensitive state, ref, current validity, and candidate class. Use normalized labels only with the live observer, never as a substitute for current refs.

Stop and repair preflight failures before any field mutation. Do not invent a fallback contract, observer result, field, evidence artifact, or session.

## Resolve, act, and verify loop

Repeat this coordinator loop without batching fields:

Observer `ref` values such as `observation-…:control-N` are ledger/evidence identities, not browser targets. Before each physical action, take a fresh OMP browser `tab.ariaSnapshot()` or `tab.observe()` and resolve the current observer control to exactly one live browser selector; use a fresh pinned-CLI snapshot and live CLI `eNN` ref only when the browser helper cannot operate that control. Match the observer frame URL chain, exact role and accessible label, tag/type, and `locator.strategy`/`locator.value`; when snapshot text alone is insufficient, use only observation-only evaluation to compare exact `id`, `name`, role, type, and supported test-ID attributes. Never mutate through evaluation. If zero or multiple controls match, do not act: obtain one fresh chained observation, publish it with `const refreshed = await acceptObservation(session, observation)`, set `ledger = refreshed.ledger`, and retry the mapping once; stop with an ambiguity blocker if it still fails. Do not evaluate or re-observe between the successful mapping and its action.

1. Choose exactly one next unresolved field from `session.ledger` and `session.observation`, excluding radio siblings already satisfied by the radio-group rule below.
2. Build `resolutionOptions` with the current `field_id`, exact alias, and only applicable `user`, `deliberate_blank`, `semantic_choice`, `sensitive`, `remember`, or `approved_at` keys. Call `const resolved = await resolveField(session, resolutionOptions); ledger = resolved.ledger;`. Normal source precedence is memory, profile, resume, supported inference, then user; required personal facts unavailable under the field's source policy require the user and never job wording.
3. `resolveField(session, resolutionOptions)` returns the coordinator binding with `field_id`, `observation_id`, `ref`, `source`, and `value_digest` computed by `digestPrivateValue` from the intended observer-semantic value; it includes only applicable `semantic_choice` and `sensitive` keys. A blank requires a deliberate UI-backed semantic choice.
4. Resolve the current observer field through the bridge and use its unique live browser selector for one matching OMP browser helper action. Use a pinned-CLI live ref only for the documented fallback, or `computer` only for the final native browser/OS fallback. The verified already-correct non-file path performs no physical action.
5. Immediately after the physical outcome, call `const actionResult = await recordAction(session, attempt); ledger = actionResult.ledger;`. Record the action result against the pre-mutation observation ID and ref before `acceptObservation` publishes the fresh observation; never store a browser selector or CLI interaction ref in `observation_id` or `ref`. Intermediate custom-widget open/filter mechanics and fallback upload-trigger mechanics create no fabricated attempt; only the final semantic field attempt is recorded.
6. Obtain a fresh DOM observation after every meaningful mutation. Publish it with `const accepted = await acceptObservation(session, observation); ledger = accepted.ledger;` and inspect `accepted.diff` for newly revealed fields or blockers.
7. At every per-action and final retention check, call `const retention = await verifyRetention(session, proofs); ledger = retention.ledger;`. Retry if `!retention.ok` or `retention.retry_required`. For documented intermediate custom-widget or pinned-CLI multi-select fallback actions, publish each fresh observation but defer retention until the exact intended state is present.
8. Continue only after the one field is deliberate, valid, retained, and represented by the coordinator's current ledger.

For any non-file field whose latest observer-semantic value or deliberate blank already matches the resolution's `value_digest` and `semantic_choice`, perform no mutation and create no action attempt. Obtain a fresh chained observation, publish it with `acceptObservation(session, observation)`, require the same exact value or choice plus valid state, then call `verifyRetention(session, proofs)`. If the state changed, use the normal action path; if a mismatching field is readonly, stop with a blocker rather than forcing fill/type or fabricating an attempt.

When no unresolved field remains on a non-final page, select the current `non_final_navigation` candidate only when exactly one candidate has an exact normalized application-entry or forward label: Apply, Apply for this Job, Easy Apply, Apply on Company Website, Continue, Next, Proceed, Review, Start Application, Begin Application, Get Started, or Go to Application. A plainly identified entry control may be activated autonomously; never activate a helper or any `final_candidate`. A nested anchor-and-button pair is one entry control only when both candidates have the same exact allowed label, the anchor contains the button, and the anchor has one same-origin application-path `href`; bridge and activate the anchor, never the nested button. Otherwise require exactly one candidate. Activate the uniquely bridged live ref, then publish `recordAction(session, { action: 'non_final_navigation', field_id: null, ref: current ref, observation_id: latest observation_id, outcome })` and re-observe through `acceptObservation(session, observation)`.

Record each retry before correcting and retrying. For every failed or retry outcome, call `recordAction(session, attempt)` with the error code and current observation/ref. Set `retry_of` to the prior attempt's nonnegative integer sequence/index, never its string `action_id`; never overwrite or hide the failure. A mutation outcome consumes its observation, so accept a fresh observation before any next resolution or action.
When a final-action attempt is rejected or leaves the application page unchanged, do not classify the job as closed or unavailable and do not close its browser surface. Capture the outcome, obtain a fresh chained observation, identify the validation or unresolved-control cause, resolve and retain it through the normal one-field loop, then return to the pre-submit boundary. Only explicit live evidence that the posting is unavailable may end the run as closed.

For every null-digest semantic choice or deliberate blank, `source` must be only `memory`, `profile`, or `user`; never use `resume` or `supported_inference`.

## UI mechanics recipes

These recipes select mechanics, not applicant answers. Always operate from the latest observer output and retain its current refs for ledger/evidence identity. Use a uniquely mapped live OMP browser selector and the helper matching the control. Pinned CLI refs are fallback-only; targetless pinned CLI `upload <absolute path>` is allowed only immediately after its uniquely mapped trigger has armed a chooser. `computer` is last and only for a remaining native browser/OS interaction.

### Ordinary editable controls

Apply the select and choice role recipes below before this shape check. For an editable observer whose role is not combobox, listbox, checkbox, radio, or switch and whose shape is `kind: input` with type other than checkbox, radio, or file; `kind: textarea`; `kind: contenteditable`; or fillable `kind: aria`, role `textbox`, use `await tab.fill(exactSelector, exactText)`. The second argument is only the intended answer. Never prepend `--value`, `--submit`, a command name, label, or option token, and never use `tab.type` for ordinary form values. The pinned-CLI fallback is exactly `playwright-cli fill eNN "<exact text>"` without `--submit`. Serialize date, time, month, week, datetime-local, and numeric values in the exact native format accepted by the control.

After a physical mutation, publish semantic action `fill` with `recordAction(session, attempt)`, obtain a fresh chained observation through `acceptObservation(session, observation)`, and call `verifyRetention(session, proofs)`. Use the coordinator's verified no-mutation path when the current value already matches. Stop on readonly mismatch or when neither the uniquely mapped browser helper nor its documented pinned-CLI fallback supports targeted fill; do not fall back to targetless keystrokes, DOM mutation, or submission. If a reachable field matches no recipe by emitted kind, tag, type, and role, stop at preflight with an unsupported-mechanic blocker.

### Native select, React Select, and ARIA listbox

Stage order: open/filter, wait for the exact option, click the exact option, then re-observe.
For a native single `<select>` (`kind: select`, `tag: select`, role `combobox`), derive and uniquely verify its exact CSS selector and use `await tab.select(exactSelectCss, exactSerializedValue)`. For a native multi-select, pass all intended serialized values in option order to the same helper. Inline `aria-ref=eNN` is not supported by `tab.select`. After the physical result, publish `recordAction(session, selectAttempt)`, obtain a fresh observation, publish `acceptObservation(session, observation)`, and verify retention. The pinned-CLI single-select fallback is `playwright-cli select eNN "<exact serialized value>"`.
Use the pinned-CLI native multi-select procedure below only when OMP browser `tab.select(exactSelectCss, ...intendedValues)` cannot operate the exact native control. Canonicalize the intended value set into serialized option order. If it is already exact, use the verified no-mutation path. Otherwise require unique exact option refs scoped to that native listbox. Before every option click, rebuild `selectAttempt` from the current `session.observation` and current ledger field. For a nonempty target, click its first intended option without a modifier to clear prior selection and establish the first value. For each remaining intended value, remap from a fresh snapshot, run pinned CLI `keydown Meta`, click that exact option ref, and always run `keyup Meta` immediately afterward even when the click fails. For an empty target, apply the same keydown/click/guaranteed-keyup sequence to each currently selected option to remove it. Never evaluate or observe between keydown and keyup. After every option click, release Meta when applicable, record the attempt, obtain a fresh chained observation, and remap the next option. Verify retention only after the selected values equal the exact intended set.

For any observer control with role `combobox` or `listbox` other than a native `kind: select`, `tag: select` control, use the exact options serialized from its owned or descendant live listbox. Take a fresh browser snapshot. If the intended exact option is not visible, `tab.click(exactComboboxSelector)` once to open it, re-observe, and take a new snapshot. If filtering is needed, use `tab.fill(exactComboboxSelector, intendedExactOptionLabel)`, re-observe, and snapshot again. A standalone listbox skips open/filter. In pinned-CLI fallback use the equivalent current `click eNN` and `fill eNN "<intended exact option label>"` commands. Never use targetless `type`, press Enter, or improvise another opening/filtering action.

Wait for exactly one enabled observer option with the intended exact label. Without another observer evaluation, require exactly one matching visible option in the current browser snapshot and `tab.click(exactOptionSelector)`; in pinned-CLI fallback click its current exact `eNN` ref. Publish the final semantic result with `recordAction`, then obtain and publish the final observation before retention verification. Create no intermediate resolution or retention check. Stop on a missing, duplicate, disabled, or stale match; never choose a partial-label match or hidden duplicate.

### File uploads

The retention proof fields are {
- `value_digest`
- `action_id`
- `file_name`
}

Primary path: map the observer file field to exactly one actual `<input type=file>` in the same frame and field/form group, including its hidden native input when applicable; derive and uniquely verify its exact CSS selector; then run `await tab.uploadFile(exactFileInputCss, session.runMetadata.resume_upload_path)`. Inline `aria-ref=eNN` is not supported by `tab.uploadFile`. Do not click or arm a chooser first. If the browser helper cannot operate the exact file input, use the pinned-CLI chooser fallback with that same verified canonical absolute path: map one visible field-associated upload trigger, click its current CLI ref, then immediately run `playwright-cli upload <session.runMetadata.resume_upload_path>`. If that also cannot complete a still-open native chooser and `computer` is available, re-ground from fresh browser and desktop snapshots and operate that chooser on the same visible cmux surface. Stop on missing or ambiguous inputs/triggers. None of these paths creates an intermediate semantic action; record only the upload result.

After the physical upload returns, build `uploadAttempt` against the pre-upload observer field/ref/observation with semantic action `upload` and its real success or failure. Publish it with `const uploaded = await recordAction(session, uploadAttempt); ledger = uploaded.ledger;`, then obtain a fresh observation and publish `const accepted = await acceptObservation(session, observation); ledger = accepted.ledger;`. Record a failed attempt and retry through the ordered mechanics before remapping; never claim success when the upload failed.

Bind `uploadActionId` to the successful, non-stale upload attempt's exact `action_id` for the current field/ref. Confirm file count and basename from the fresh file control. `verifyRetention` receives the field-ID keyed map `{ [field_id]: { value_digest, action_id, file_name } }`, using that same `uploadActionId` as proof `action_id`.

The proof `value_digest` must equal the field resolution `value_digest`, not the file or resume-upload SHA-256 identity digest. Keep the proof map for every later retention check and final observation; an upload disappearing on a later page is a blocker.

### Checkbox groups

Resolve every reachable checkbox or switch option, not only selected options. When observed `checked` differs from the intended state, use `tab.click(exactSelector)` on the uniquely mapped native or custom control. In pinned-CLI fallback, use idempotent `check eNN` or `uncheck eNN` for native inputs and click the exact ref for custom ARIA choices. Do nothing when the observer already proves the intended state.

After a mutation, publish semantic action `check` or `uncheck` with `recordAction(session, attempt)`, obtain a fresh observation through `acceptObservation(session, observation)`, and verify retention. When the option already matches, create no action record; publish one fresh observation and use the no-mutation retention path. For an intentionally unchecked option, use `value_digest` null and `semantic_choice` `none` only when that fresh UI state proves it unchecked.

For every null-digest `none` choice, use only `memory`, `profile`, or `user`; `resume` and `supported_inference` are not allowed. A grouped field passes only when every reachable option has a deliberate retained state.

### Radio groups

Group live radio controls by `group_id`, or by `frame_id` plus `name` when no group ID exists. Resolve exactly one intended option in each group. If it is not already selected, use `tab.click(exactIntendedRadioSelector)`; in pinned-CLI fallback use idempotent `check eNN` for a native radio or click the exact ref for a custom ARIA radio.

After a mutation, publish semantic action `check` for the native radio or `select` for the custom radio with `recordAction(session, attempt)`, then obtain a fresh observation through `acceptObservation(session, observation)` and verify retention. When the intended radio is already selected, create no action record and use the fresh-observation no-mutation path. Require exactly one deliberate, valid, retained selection, then treat every unselected sibling in that current or historical group as group-satisfied rather than independently actionable; never click alternatives merely because their individual ledger entries remain unresolved.

### Dynamic fields

After changing a parent, re-observe before any child action and add every revealed child to the inventory. Complete a revealed child with a deliberate, valid, retained state before changing or hiding it; leaving it unresolved or disappearing creates historical debt.
Every historically reachable field must be deliberate, valid, and retained; no final-review waiver or disposition exists.

### Country selector

Call `resolveField(session, { field_id, alias: 'profile.address.country', sensitive: true })` so country can resolve only from exact memory, profile, or user evidence: memory remains first globally; within the profile, structured `address.country` precedes the `profile.answers` fallback. Use the React Select sequence when applicable, match one exact live option, click it, and verify the retained observer value.

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
4. Before any browser click, call `const begun = await beginFinalSubmit(session);` and require `begun.ref === finalRef`. Map `begun.ref` to exactly one live browser selector, then activate it with `tab.click(finalSelector)`. If the browser helper cannot operate the exact control, pinned CLI may click its freshly mapped current ref; `computer` must never cross the final-submit boundary.
5. Re-observe the resulting page even when the click helper reports a timeout or error, then call `completeFinalSubmit(session, { attemptId: begun.attemptId, outcome, errorCode })` exactly once with the observed terminal outcome. Never click before `beginFinalSubmit`, and never leave a begun attempt unresolved. A failed or blocked attempt requires a fresh chained observation and a fresh `prepareSubmission` audit before retrying; never reuse an authorization across observations.
6. After one final-submit attempt succeeds, capture the post-submit screenshot into the owner-private artifact directory and call `const finalized = await finalizeRun(session, { screenshotPath });`. Require `finalized.finalized` to be true. Persist `completed` only from the validated canonical `completion.json`; SQLite derives every submission attempt and the exact count from its paired journal events.
7. Leave the headed browser open after submission long enough to capture post-submit evidence. Report only private artifact paths and blockers, never applicant values or answer contents.
## Run outcome classification

After finalization or when a run cannot proceed, persist exactly one terminal outcome:

- **`completed`** — Every reachable field is deliberate, valid, and retained; the final audit passes with no blockers; OMP begins, performs, and resolves the final action after `prepareSubmission` authorizes it; and validated canonical `completion.json` evidence is persisted. This is the only successful outcome and requires no human approval.
- **`needs_user`** — A required truthful personal fact is missing from profile, memory, and resume, and agent inference is prohibited (sensitive field, identity, authorization, protected-class, salary/compensation, date, credential, or other restricted category). The browser surface and evidence remain active; the run can resume after the user supplies the fact. Also use when an observer-covered required field cannot be resolved because no answer source is available.
- **`blocked`** — The page presents a hard safety blocker: authentication, non-CAPTCHA anti-bot challenge, assessment/integrity check, access-control, or an inaccessible frame. These require narrow human resolution and must never be bypassed. Also use when an unknown visible control cannot be resolved or a submission was observed outside the canonical protocol; record the latter as `noncanonical_submission_receipt` with an unknown count represented as `NULL`.
- **`closed`** — Only explicit live evidence that the posting is unavailable: HTTP 404/410, a page-level "job not found" or "position filled/closed" message, or a redirect away from the application. Never use for form validation failures, retention errors, or rejected submissions.
- **`failed`** — A bounded infrastructure or evidence-integrity failure that cannot be recovered by retry (e.g., evidence store corruption, unrecoverable I/O error). Routine form completion debt, validation errors, and non-accepted final actions are never `failed`.

Only persist a terminal outcome when the run genuinely cannot continue or has completed submission.

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

A handoff must cite the claimed job, persisted outcome, private workspace, current cmux surface and named browser tab, latest accepted observation ID, ledger/evidence paths, verified resume-upload path and retention state, latest verification, blocker, and single next action. The receiving agent must reread `Handoff-safe OMP browser quick start` before any browser action instead of reconstructing mechanics from the handoff narrative.
