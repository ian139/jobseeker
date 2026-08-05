import {
  ACTION_RESULT_SCHEMA,
  ActionPlanError,
  createBrowserActionPlan,
  validateBrowserActionPlan,
  validateBrowserActionResult,
} from '../../src/phase1/action-plan.mjs';
import {
  createLedger,
  digestObservedValue,
  recordResolution,
} from '../../src/phase1/ledger.mjs';

const SNAPSHOT_DIGEST = 'a'.repeat(64);
const OPTION_VALUE = 'eng';
const OPTION_LABEL = 'Engineering';
const FIELD_ID = 'role';
const CONTROL_REFERENCE = 'ref-role';
const PLAN_TIMESTAMP = '2026-08-04T00:00:01.000Z';
const POST_TIMESTAMP = '2026-08-04T00:00:02.000Z';
const AMBIGUOUS_TIMESTAMP = '2026-08-04T00:00:03.000Z';
const MAX_WAIT_MS = 10 * 60 * 1000;

class ExpectedBehaviorFailure extends Error {
  constructor(code) {
    super(code);
    this.name = 'ExpectedBehaviorFailure';
    this.code = code;
  }
}

function customControl({ options, value = null, valuePresent = false }) {
  return {
    ref: CONTROL_REFERENCE,
    stable_id: FIELD_ID,
    group_id: null,
    kind: 'input',
    tag: 'input',
    type: 'text',
    role: 'combobox',
    label: 'Role',
    name: FIELD_ID,
    description: null,
    locator: { strategy: 'id', value: FIELD_ID, role: null, name: null },
    frame_id: 'frame-main',
    visible: true,
    enabled: true,
    required: true,
    readonly: false,
    disabled: false,
    value,
    value_present: valuePresent,
    checked: null,
    selected: null,
    options,
    validity: { valid: true, aria_invalid: null, message: null },
    file: null,
    candidate: { class: 'field', reason: 'synthetic custom combobox' },
  };
}

function observation(observationId, observedAt, options, state = {}) {
  return {
    schema: 'phase1-observation-v1',
    observation_id: observationId,
    previous_observation_id: state.previousObservationId ?? null,
    observed_at: observedAt,
    url: 'https://example.test/form',
    title: 'Synthetic application form',
    snapshot_sha256: SNAPSHOT_DIGEST,
    frames: [{
      id: 'frame-main',
      parent_id: null,
      url: 'https://example.test/form',
      origin: 'https://example.test',
      accessible: true,
    }],
    controls: [customControl({
      options,
      value: state.value ?? null,
      valuePresent: state.valuePresent ?? false,
    })],
    blockers: [],
  };
}

function option(label = OPTION_LABEL, value = OPTION_VALUE, selected = false) {
  return { value, label, disabled: false, selected };
}

function decision(observationId) {
  return {
    observationId,
    fieldId: FIELD_ID,
    controlReference: CONTROL_REFERENCE,
    fieldPolicy: 'qualification',
    proposedAnswer: OPTION_VALUE,
    answerSource: 'user',
    evidenceReferences: [],
    inferenceRationaleDigest: null,
    inferenceEvidenceDigests: null,
    proposedAction: 'select_option',
    expectedRetainedState: OPTION_VALUE,
    modelTier: 'standard',
    confidence: 0.9,
    reasonCode: 'synthetic_option',
    reobservationRequired: true,
    automaticSubmissionEligible: false,
  };
}

function fixture({ observationId = 'obs-1', observedAt = PLAN_TIMESTAMP, options }) {
  const currentObservation = observation(observationId, observedAt, options);
  let ledger = createLedger(currentObservation);
  ledger = recordResolution(ledger, {
    field_id: FIELD_ID,
    observation_id: observationId,
    ref: CONTROL_REFERENCE,
    source: 'user',
    value_digest: digestObservedValue(currentObservation.controls[0], OPTION_VALUE),
  });
  return { currentObservation, ledger };
}

function planInput(currentObservation, ledger) {
  return {
    observation: currentObservation,
    ledger,
    decisions: [decision(currentObservation.observation_id)],
    answerAliases: { [FIELD_ID]: { alias: 'role', value: OPTION_VALUE } },
    optionMatches: { [FIELD_ID]: { option_text: OPTION_LABEL, option_value: OPTION_VALUE } },
    driver: 'omp_browser',
    createdAt: PLAN_TIMESTAMP,
    ats: 'synthetic',
  };
}

function walk(value, visit, seen = new Set()) {
  if (value === null || typeof value !== 'object' || seen.has(value)) return;
  seen.add(value);
  visit(value);
  if (Array.isArray(value)) {
    value.forEach((item) => walk(item, visit, seen));
  } else {
    Object.values(value).forEach((item) => walk(item, visit, seen));
  }
}

function hasBoundedWait(plan) {
  let found = false;
  walk(plan, (value) => {
    if (value.action === 'wait'
      && Number.isSafeInteger(value.timeoutMs)
      && value.timeoutMs > 0
      && value.timeoutMs <= MAX_WAIT_MS) {
      found = true;
    }
  });
  return found;
}

function hasFreshStabilization(plan) {
  let found = false;
  walk(plan, (value) => {
    if (value.action === 'reobserve') found = true;
  });
  return found;
}

function expectBehavior(code, assertion) {
  try {
    assertion();
  } catch (error) {
    if (error instanceof ExpectedBehaviorFailure && error.code === code) return code;
    throw error;
  }
  return null;
}

function expectActionPlanError(assertion, code) {
  try {
    assertion();
  } catch (error) {
    if (error instanceof ActionPlanError && error.code === code) return;
    throw error;
  }
  throw new Error(`expected ActionPlanError:${code}`);
}

function malformedResult(plan, postObservation) {
  return {
    schema: ACTION_RESULT_SCHEMA,
    plan_id: plan.plan_id,
    post_observation_id: postObservation.observation_id,
    outcomes: [{
      action_id: plan.actions[0].action_id,
      outcome: 'succeeded',
      error_code: null,
      driver: 'omp_browser',
      selected_option_text: OPTION_LABEL,
    }],
  };
}

function assertUncommittedSelectionRejected(plan, currentObservation) {
  const posts = [
    observation('obs-2-search', POST_TIMESTAMP, [option()], {
      previousObservationId: currentObservation.observation_id,
      value: OPTION_LABEL,
      valuePresent: true,
    }),
    observation('obs-2-empty', POST_TIMESTAMP, [option()], {
      previousObservationId: currentObservation.observation_id,
      value: null,
      valuePresent: false,
    }),
  ];
  for (const postObservation of posts) {
    try {
      validateBrowserActionResult(
        malformedResult(plan, postObservation),
        plan,
        postObservation,
      );
    } catch (error) {
      if (error instanceof ActionPlanError) continue;
      throw error;
    }
    throw new ExpectedBehaviorFailure('RESULT_UNCOMMITTED_SELECTION_ACCEPTED');
  }
}

export function evaluate() {
  const exact = fixture({ options: [option()] });
  const plan = createBrowserActionPlan(planInput(exact.currentObservation, exact.ledger));
  validateBrowserActionPlan(plan, {
    observation: exact.currentObservation,
    ledger: exact.ledger,
  });

  if (plan.actions[0].steps.length !== 3
    || plan.actions[0].steps[1].value !== OPTION_LABEL
    || plan.actions[0].steps[2].option_value !== OPTION_VALUE
    || plan.actions[0].steps[2].option_text !== OPTION_LABEL) {
    throw new Error('exact custom option did not remain canonical');
  }

  const ambiguous = fixture({
    observationId: 'obs-ambiguous',
    observedAt: AMBIGUOUS_TIMESTAMP,
    options: [option(), option(OPTION_LABEL, 'eng-duplicate')],
  });
  expectActionPlanError(
    () => createBrowserActionPlan(planInput(ambiguous.currentObservation, ambiguous.ledger)),
    'NON_UNIQUE_OPTION',
  );

  const diagnostics = [];
  let checks = 2;
  let failures = 0;

  checks += 1;
  if (!hasBoundedWait(plan)) {
    const diagnostic = expectBehavior('PLAN_ASYNC_OPTION_WAIT_MISSING', () => {
      throw new ExpectedBehaviorFailure('PLAN_ASYNC_OPTION_WAIT_MISSING');
    });
    if (diagnostic !== null) {
      failures += 1;
      diagnostics.push(diagnostic);
    }
  }

  checks += 1;
  if (!hasFreshStabilization(plan)) {
    const diagnostic = expectBehavior('PLAN_RERENDER_STABILIZATION_MISSING', () => {
      throw new ExpectedBehaviorFailure('PLAN_RERENDER_STABILIZATION_MISSING');
    });
    if (diagnostic !== null) {
      failures += 1;
      diagnostics.push(diagnostic);
    }
  }

  checks += 1;
  const diagnostic = expectBehavior('RESULT_UNCOMMITTED_SELECTION_ACCEPTED', () => {
    assertUncommittedSelectionRejected(plan, exact.currentObservation);
  });
  if (diagnostic !== null) {
    failures += 1;
    diagnostics.push(diagnostic);
  }

  return Object.freeze({
    name: 'custom-select-plan-result',
    checks,
    failures,
    diagnostics: Object.freeze(diagnostics),
  });
}
