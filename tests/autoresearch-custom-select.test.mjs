import assert from 'node:assert/strict';
import test from 'node:test';

import {
  ActionPlanError,
  ACTION_RESULT_SCHEMA,
  createBrowserActionPlan,
  validateBrowserActionResult,
} from '../src/phase1/action-plan.mjs';
import {
  Phase1StaleReferenceError,
  createLedger,
  digestObservedValue,
  mergeObservation,
  recordActionAttempt,
  recordResolution,
  verifyRetention,
} from '../src/phase1/ledger.mjs';

const DIGEST = 'a'.repeat(64);
const FIELD_ID = 'degree';
const CONTROL_REFERENCE = 'obs-1:degree';
const STABLE_ID = FIELD_ID;
const PROVIDER_VALUE = 'provider-42';
const COMMITTED_LABEL = 'Data Science';
const FIXED_TIME = '2026-08-04T00:00:01.000Z';

function frame() {
  return {
    id: 'frame-main',
    parent_id: null,
    url: 'https://example.test/application',
    origin: 'https://example.test',
    accessible: true,
  };
}

function option(value, label, selected) {
  return { value, label, disabled: false, selected };
}

function customSelectControl({
  observationId,
  ref = `${observationId}:degree`,
  value = PROVIDER_VALUE,
  valuePresent = true,
  selected = [PROVIDER_VALUE],
  options = [
    option(PROVIDER_VALUE, COMMITTED_LABEL, true),
    option('provider-7', 'Computer Science', false),
  ],
} = {}) {
  return {
    ref,
    stable_id: STABLE_ID,
    group_id: null,
    kind: 'combobox',
    tag: 'input',
    type: 'text',
    role: 'combobox',
    label: 'Degree',
    name: FIELD_ID,
    description: null,
    locator: { strategy: 'id', value: FIELD_ID, role: 'combobox', name: FIELD_ID },
    frame_id: 'frame-main',
    visible: true,
    enabled: true,
    required: true,
    readonly: false,
    disabled: false,
    value,
    value_present: valuePresent,
    checked: null,
    selected,
    options,
    validity: { valid: true, aria_invalid: null, message: null },
    file: null,
    candidate: { class: 'field', reason: 'visible custom select field' },
  };
}

function observation(id, control, previousObservationId = null) {
  return {
    schema: 'phase1-observation-v1',
    observation_id: id,
    previous_observation_id: previousObservationId,
    observed_at: FIXED_TIME,
    url: 'https://example.test/application',
    title: 'Synthetic application',
    snapshot_sha256: DIGEST,
    frames: [frame()],
    controls: [control],
    blockers: [],
  };
}

function decision() {
  return {
    observationId: 'obs-1',
    fieldId: FIELD_ID,
    controlReference: CONTROL_REFERENCE,
    fieldPolicy: 'qualification',
    proposedAnswer: PROVIDER_VALUE,
    answerSource: 'user',
    evidenceReferences: [],
    inferenceRationaleDigest: null,
    inferenceEvidenceDigests: null,
    proposedAction: 'select_option',
    expectedRetainedState: PROVIDER_VALUE,
    modelTier: 'standard',
    confidence: 1,
    reasonCode: 'synthetic_exact_option',
    reobservationRequired: true,
    automaticSubmissionEligible: false,
  };
}

function initialFixture() {
  const initial = observation('obs-1', customSelectControl({ observationId: 'obs-1', ref: CONTROL_REFERENCE }));
  let ledger = createLedger(initial);
  ledger = recordResolution(ledger, {
    field_id: FIELD_ID,
    observation_id: initial.observation_id,
    ref: CONTROL_REFERENCE,
    source: 'user',
    value_digest: digestObservedValue(initial.controls[0], PROVIDER_VALUE),
  });
  const plan = createBrowserActionPlan({
    observation: initial,
    ledger,
    decisions: [decision()],
    answerAliases: { [FIELD_ID]: { alias: 'synthetic_degree', value: PROVIDER_VALUE } },
    optionMatches: { [FIELD_ID]: { option_text: COMMITTED_LABEL, option_value: PROVIDER_VALUE } },
    driver: 'omp_browser',
    createdAt: FIXED_TIME,
    ats: 'synthetic',
  });
  return { initial, ledger, plan };
}

function successfulResult(plan, postObservation) {
  return validateBrowserActionResult({
    schema: ACTION_RESULT_SCHEMA,
    plan_id: plan.plan_id,
    post_observation_id: postObservation.observation_id,
    outcomes: [{
      action_id: plan.actions[0].action_id,
      outcome: 'succeeded',
      error_code: null,
      driver: 'omp_browser',
      selected_option_text: COMMITTED_LABEL,
    }],
  }, plan, postObservation);
}

function retentionAfterResult(fixture, postObservation) {
  const result = successfulResult(fixture.plan, postObservation);
  const afterAction = recordActionAttempt(fixture.ledger, result.attempts[0]);
  const afterObservation = mergeObservation(afterAction, postObservation);
  return { result, ledger: afterObservation, retention: verifyRetention(afterObservation, postObservation) };
}

test('rejects a succeeded custom-select result when the fresh observation has no committed option', () => {
  const fixture = initialFixture();
  const post = observation('obs-2', customSelectControl({
    observationId: 'obs-2',
    value: PROVIDER_VALUE,
    selected: [],
    options: [
      option(PROVIDER_VALUE, COMMITTED_LABEL, false),
      option('provider-7', 'Computer Science', false),
    ],
  }), 'obs-1');

  assert.throws(
    () => successfulResult(fixture.plan, post),
    (error) => error instanceof ActionPlanError && error.code === 'OPTION_SELECTION_UNCOMMITTED',
  );
  assert.equal(post.previous_observation_id, fixture.plan.observation_id);
});

test('accepts a fresh rerender only when committed custom-select label and provider value are exact', () => {
  const fixture = initialFixture();
  const post = observation('obs-2', customSelectControl({
    observationId: 'obs-2',
    ref: 'obs-2:degree-rerender',
    value: PROVIDER_VALUE,
    selected: [PROVIDER_VALUE],
    options: [
      option(PROVIDER_VALUE, COMMITTED_LABEL, true),
      option('provider-7', 'Computer Science', false),
    ],
  }), 'obs-1');

  const { result, retention } = retentionAfterResult(fixture, post);
  assert.equal(result.attempts[0].action, 'select');
  assert.equal(post.controls[0].options.find((item) => item.selected)?.label, COMMITTED_LABEL);
  assert.equal(post.controls[0].options.find((item) => item.selected)?.value, PROVIDER_VALUE);
  assert.equal(retention.ok, true);
  assert.equal(retention.ledger.fields[0].latest_ref, 'obs-2:degree-rerender');
});

test('rejects stale custom-select references and unchained post-observations', () => {
  const fixture = initialFixture();
  const stalePost = observation('obs-2', customSelectControl({ observationId: 'obs-2' }), 'obs-other');

  assert.throws(
    () => successfulResult(fixture.plan, stalePost),
    /POST_OBSERVATION_CHAIN/u,
  );
  assert.throws(
    () => mergeObservation(fixture.ledger, stalePost),
    (error) => error instanceof Phase1StaleReferenceError,
  );
  assert.throws(
    () => recordResolution(fixture.ledger, {
      field_id: FIELD_ID,
      observation_id: 'obs-1',
      ref: 'stale-degree-ref',
      source: 'user',
      value_digest: digestObservedValue(fixture.initial.controls[0], PROVIDER_VALUE),
    }),
    (error) => error instanceof Phase1StaleReferenceError,
  );
});
