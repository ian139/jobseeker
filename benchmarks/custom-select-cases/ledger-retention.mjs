import {
  ACTION_RESULT_SCHEMA,
  createBrowserActionPlan,
  validateBrowserActionResult,
} from '../../src/phase1/action-plan.mjs';
import {
  createLedger,
  digestObservedValue,
  mergeObservation,
  recordActionAttempt,
  recordResolution,
  verifyRetention,
} from '../../src/phase1/ledger.mjs';

const OBSERVATION_SCHEMA = 'phase1-observation-v1';
const EXPECTED_LABEL = 'Exact School';
const PROVIDER_VALUE = 'provider-school-42';
const SNAPSHOT_DIGEST = 'a'.repeat(64);
const FRAME = Object.freeze({
  id: 'frame-main',
  parent_id: null,
  url: 'https://example.test/application',
  origin: 'https://example.test',
  accessible: true,
});

function option({ selected = false } = {}) {
  return {
    value: PROVIDER_VALUE,
    label: EXPECTED_LABEL,
    disabled: false,
    selected,
  };
}

function customControl({
  ref = 'ref-school',
  value = null,
  valuePresent = value !== null && value !== '',
  selected = [],
  options = [option()],
} = {}) {
  return {
    ref,
    stable_id: 'school',
    group_id: null,
    kind: 'input',
    tag: 'input',
    type: 'text',
    role: 'combobox',
    label: 'School',
    name: 'school',
    description: 'Synthetic custom select',
    locator: { strategy: 'id', value: 'school', role: null, name: null },
    frame_id: 'frame-main',
    visible: true,
    enabled: true,
    required: true,
    readonly: false,
    disabled: false,
    value,
    value_present: valuePresent,
    checked: null,
    selected: [...selected],
    options: options.map((item) => ({ ...item })),
    validity: { valid: true, aria_invalid: null, message: null },
    file: null,
    candidate: { class: 'field', reason: 'synthetic custom select' },
  };
}

function unrelatedControl({ value = 'initial-other', ref = 'ref-other' } = {}) {
  return {
    ref,
    stable_id: 'other',
    group_id: null,
    kind: 'text',
    tag: 'input',
    type: 'text',
    role: 'textbox',
    label: 'Other synthetic answer',
    name: 'other',
    description: null,
    locator: { strategy: 'id', value: 'other', role: null, name: null },
    frame_id: 'frame-main',
    visible: true,
    enabled: true,
    required: true,
    readonly: false,
    disabled: false,
    value,
    value_present: value !== null && value !== '',
    checked: null,
    selected: null,
    options: [],
    validity: { valid: true, aria_invalid: null, message: null },
    file: null,
    candidate: { class: 'field', reason: 'unrelated synthetic field' },
  };
}

function observation(id, controls, previousObservationId = null) {
  const observedAt = {
    'obs-1': '2026-08-04T00:00:01.000Z',
    'obs-2': '2026-08-04T00:00:02.000Z',
    'obs-stale': '2026-08-04T00:00:03.000Z',
  }[id];
  if (observedAt === undefined) throw new Error(`unknown synthetic observation: ${id}`);
  return {
    schema: OBSERVATION_SCHEMA,
    observation_id: id,
    previous_observation_id: previousObservationId,
    observed_at: observedAt,
    url: 'https://example.test/application',
    title: 'Synthetic application',
    snapshot_sha256: SNAPSHOT_DIGEST,
    frames: [FRAME],
    controls,
    blockers: [],
  };
}

function selectDecision() {
  return {
    observationId: 'obs-1',
    fieldId: 'school',
    controlReference: 'ref-school',
    fieldPolicy: 'qualification',
    proposedAnswer: EXPECTED_LABEL,
    answerSource: 'user',
    evidenceReferences: [],
    inferenceRationaleDigest: null,
    inferenceEvidenceDigests: null,
    proposedAction: 'select_option',
    expectedRetainedState: EXPECTED_LABEL,
    modelTier: 'standard',
    confidence: 1,
    reasonCode: 'synthetic_exact_option',
    reobservationRequired: true,
    automaticSubmissionEligible: false,
  };
}

function resolution(fieldId, ref, control, value) {
  return {
    field_id: fieldId,
    observation_id: 'obs-1',
    ref,
    source: 'user',
    value_digest: digestObservedValue(control, value),
  };
}

function fixture({ includeUnrelated = false } = {}) {
  const initialSchool = customControl();
  const initialControls = [initialSchool];
  if (includeUnrelated) initialControls.push(unrelatedControl());
  const initial = observation('obs-1', initialControls);
  let ledger = createLedger(initial);
  ledger = recordResolution(ledger, resolution('school', initialSchool.ref, initialSchool, EXPECTED_LABEL));
  if (includeUnrelated) {
    const other = initial.controls.find((control) => control.stable_id === 'other');
    ledger = recordResolution(ledger, resolution('other', other.ref, other, 'expected-other'));
  }

  const plan = createBrowserActionPlan({
    observation: initial,
    ledger,
    decisions: [selectDecision()],
    answerAliases: { school: { alias: 'synthetic_school', value: EXPECTED_LABEL } },
    optionMatches: {
      school: { option_text: EXPECTED_LABEL, option_value: EXPECTED_LABEL },
    },
    driver: 'omp_browser',
    createdAt: '2026-08-04T00:00:10.000Z',
    ats: 'synthetic',
  });
  const action = plan.actions[0];
  if (plan.observation_id !== 'obs-1'
    || action.semantic_action !== 'select_option'
    || action.control_reference !== initialSchool.ref
    || action.steps.at(-1)?.option_text !== EXPECTED_LABEL
    || action.retention.kind !== 'normalized_option') {
    throw new Error('synthetic exact custom-option plan was not created');
  }
  return { initial, ledger, plan };
}

function resultFor(plan, postObservation) {
  return {
    schema: ACTION_RESULT_SCHEMA,
    plan_id: plan.plan_id,
    post_observation_id: postObservation.observation_id,
    outcomes: [{
      action_id: plan.actions[0].action_id,
      outcome: 'succeeded',
      error_code: null,
      driver: 'omp_browser',
      selected_option_text: EXPECTED_LABEL,
    }],
  };
}

function executeReceipt(base, postObservation) {
  const result = resultFor(base.plan, postObservation);
  const validation = validateBrowserActionResult(result, base.plan, postObservation);
  if (validation.attempts.length !== 1 || validation.attempts[0].outcome !== 'succeeded') {
    throw new Error('synthetic exact custom-option result was not successful');
  }
  const actionLedger = recordActionAttempt(base.ledger, validation.attempts[0]);
  const mergedLedger = mergeObservation(actionLedger, postObservation);
  return {
    result,
    validation,
    retention: verifyRetention(mergedLedger, postObservation),
  };
}

function fieldResult(retention, fieldId) {
  return retention.ledger.fields.find((field) => field.field_id === fieldId);
}

function hasErrorFor(retention, fieldId, code) {
  return retention.errors.some((error) => error.field_id === fieldId && error.code === code);
}

function staleChainRejected(base) {
  const post = observation(
    'obs-stale',
    [customControl({ value: EXPECTED_LABEL, selected: [PROVIDER_VALUE], options: [option({ selected: true })] })],
    'obs-old',
  );
  try {
    validateBrowserActionResult(resultFor(base.plan, post), base.plan, post);
  } catch (error) {
    if (error?.code !== 'POST_OBSERVATION_CHAIN') throw error;
    return true;
  }
  return false;
}

function evaluateGlobalRetention() {
  const base = fixture({ includeUnrelated: true });
  const post = observation('obs-2', [
    customControl({
      value: EXPECTED_LABEL,
      selected: [PROVIDER_VALUE],
      options: [option({ selected: true })],
    }),
    unrelatedControl({ value: 'wrong-other' }),
  ], 'obs-1');
  const receipt = executeReceipt(base, post);
  const actionField = fieldResult(receipt.retention, 'school');
  const unrelatedField = fieldResult(receipt.retention, 'other');
  return receipt.retention.ok === false
    && actionField?.retained === true
    && actionField?.valid === true
    && unrelatedField?.retained === false
    && receipt.retention.errors.length > 0
    && receipt.retention.errors.every((error) => error.field_id === 'other')
    && hasErrorFor(receipt.retention, 'other', 'VALUE_NOT_RETAINED');
}

export function evaluate() {
  const diagnostics = [];

  const queryBase = fixture();
  const queryPost = observation(
    'obs-2',
    [customControl({ value: EXPECTED_LABEL, selected: [], options: [] })],
    'obs-1',
  );
  const queryRetention = executeReceipt(queryBase, queryPost).retention;
  if (queryRetention.ok === true) diagnostics.push('RESULT_UNCOMMITTED_SELECTION_ACCEPTED');

  const committedBase = fixture();
  const committedPost = observation(
    'obs-2',
    [customControl({
      value: PROVIDER_VALUE,
      selected: [PROVIDER_VALUE],
      options: [option({ selected: true })],
    })],
    'obs-1',
  );
  const committedRetention = executeReceipt(committedBase, committedPost).retention;
  const committedField = fieldResult(committedRetention, 'school');
  if (committedRetention.ok !== true
    || committedField?.retained !== true
    || committedField?.valid !== true) {
    diagnostics.push('NORMALIZED_LABEL_VALUE_NOT_RETAINED');
  }

  const chainBase = fixture();
  if (!staleChainRejected(chainBase)) diagnostics.push('STALE_CHAIN_ACCEPTED');

  const refBase = fixture();
  const refPost = observation(
    'obs-2',
    [customControl({
      ref: 'ref-school-rerendered',
      value: EXPECTED_LABEL,
      selected: [PROVIDER_VALUE],
      options: [option({ selected: true })],
    })],
    'obs-1',
  );
  const refRetention = executeReceipt(refBase, refPost).retention;
  const refField = fieldResult(refRetention, 'school');
  if (refRetention.ok !== true || refField?.latest_ref !== 'ref-school-rerendered') {
    diagnostics.push('CURRENT_REF_NOT_RETAINED');
  }

  if (!evaluateGlobalRetention()) diagnostics.push('GLOBAL_RETENTION_MASKS_ACTION_SUCCESS');

  const frozenDiagnostics = Object.freeze(diagnostics);
  return Object.freeze({
    name: 'ledger-retention',
    checks: 5,
    failures: frozenDiagnostics.length,
    diagnostics: frozenDiagnostics,
  });
}
