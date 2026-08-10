import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';

import {
  ACTION_PLAN_SCHEMA,
  ACTION_RESULT_SCHEMA,
  LEGACY_ACTION_PLAN_SCHEMA,
  createBrowserActionPlan,
  validateBrowserActionPlan,
  validateBrowserActionResult,
} from '../src/phase1/action-plan.mjs';
import {
  createLedger,
  digestObservedValue,
  digestPrivateValue,
  recordResolution,
} from '../src/phase1/ledger.mjs';

const DIGEST = 'a'.repeat(64);
const FILE_DIGEST = 'b'.repeat(64);

function frame() {
  return {
    id: 'frame-main',
    parent_id: null,
    url: 'https://example.test/app',
    origin: 'https://example.test',
    accessible: true,
  };
}

function control(fieldId, overrides = {}) {
  return {
    ref: `ref-${fieldId}`,
    stable_id: fieldId,
    group_id: null,
    kind: 'text',
    tag: 'input',
    type: 'text',
    role: 'textbox',
    label: `Question ${fieldId}`,
    name: fieldId,
    description: null,
    locator: { strategy: 'id', value: fieldId, role: null, name: null },
    frame_id: 'frame-main',
    visible: true,
    enabled: true,
    required: true,
    readonly: false,
    disabled: false,
    value: `current-${fieldId}`,
    value_present: true,
    checked: null,
    selected: null,
    options: [],
    validity: { valid: true, aria_invalid: null, message: null },
    file: null,
    candidate: { class: 'field', reason: 'visible user-facing field control' },
    ...overrides,
  };
}

function observation(id, controls, previous = null) {
  return {
    schema: 'phase1-observation-v1',
    observation_id: id,
    previous_observation_id: previous,
    observed_at: `2026-08-04T00:00:${id === 'obs-1' ? '01' : '02'}.000Z`,
    url: 'https://example.test/app',
    title: 'Synthetic application',
    snapshot_sha256: DIGEST,
    frames: [frame()],
    controls,
    blockers: [],
  };
}

function decision(fieldId, action, proposedAnswer, overrides = {}) {
  return {
    observationId: 'obs-1',
    fieldId,
    controlReference: `ref-${fieldId}`,
    fieldPolicy: 'qualification',
    proposedAnswer,
    answerSource: 'user',
    evidenceReferences: [],
    inferenceRationaleDigest: null,
    inferenceEvidenceDigests: null,
    proposedAction: action,
    expectedRetainedState: proposedAnswer,
    modelTier: 'standard',
    confidence: 0.9,
    reasonCode: 'test_answer',
    reobservationRequired: true,
    automaticSubmissionEligible: false,
    ...overrides,
  };
}

function resolvedLedger(current, resolutions) {
  let ledger = createLedger(current);
  for (const resolution of resolutions) {
    const control = current.controls.find((item) => item.stable_id === resolution.field_id);
    const field = ledger.fields.find((item) => item.field_id === resolution.field_id);
    const value = resolution.value;
    ledger = recordResolution(ledger, {
      field_id: resolution.field_id,
      observation_id: current.observation_id,
      ref: control.ref,
      source: 'user',
      value_digest: resolution.semantic_choice ? null : digestObservedValue(control, value),
      ...(resolution.semantic_choice ? { semantic_choice: resolution.semantic_choice } : {}),
    });
    assert.equal(ledger.fields.find((item) => item.field_id === field.field_id).latest_ref, control.ref);
  }
  return ledger;
}

function textPlan(overrides = {}) {
  const current = observation('obs-1', [control('name')]);
  const ledger = resolvedLedger(current, [{ field_id: 'name', value: 'Ada Lovelace' }]);
  return createBrowserActionPlan({
    observation: current,
    ledger,
    decisions: [decision('name', 'fill_text', 'Ada Lovelace')],
    answerAliases: { name: { alias: 'full_name', value: 'Ada Lovelace' } },
    optionMatches: {},
    driver: 'omp_browser',
    ...overrides,
  });
}

function resultFor(plan, outcomes, post = null) {
  return validateBrowserActionResult({
    schema: ACTION_RESULT_SCHEMA,
    plan_id: plan.plan_id,
    post_observation_id: post?.observation_id ?? 'obs-2',
    outcomes,
  }, plan, post ?? observation('obs-2', [], 'obs-1'));
}
function customSelectPlan() {
  const current = observation('obs-1', [control('role', {
    kind: 'input',
    type: 'text',
    role: 'combobox',
    value: null,
    value_present: false,
    options: [{ value: 'eng', label: 'Engineering', disabled: false, selected: false }],
  })]);
  const ledger = resolvedLedger(current, [{ field_id: 'role', value: 'eng' }]);
  return createBrowserActionPlan({
    observation: current,
    ledger,
    decisions: [decision('role', 'select_option', 'eng')],
    answerAliases: { role: { alias: 'role', value: 'eng' } },
    optionMatches: { role: { option_text: 'Engineering', option_value: 'eng' } },
    driver: 'omp_browser',
  });
}
function uploadPlan(overrides = {}) {
  const current = observation('obs-1', [control('resume', {
    kind: 'file',
    type: 'file',
    role: 'input',
    value: null,
    value_present: false,
    file: { accept: null, count: 0, names: [] },
  })]);
  const ledger = resolvedLedger(current, [{ field_id: 'resume', value: '/tmp/resume.pdf' }]);
  return createBrowserActionPlan({
    observation: current,
    ledger,
    decisions: [decision('resume', 'upload_file', '/tmp/resume.pdf')],
    answerAliases: {},
    optionMatches: {},
    resumeUpload: { path: '/tmp/resume.pdf', sha256: FILE_DIGEST },
    driver: 'omp_browser',
    ...overrides,
  });
}

function committedUploadPost(fileState, { fieldId = 'resume' } = {}) {
  return observation('obs-2', [control(fieldId, {
    kind: 'file',
    type: 'file',
    role: 'input',
    value: null,
    value_present: false,
    file: fileState,
  })], 'obs-1');
}


function legacyPlan(plan, { twoStep = false, filterFirst = false } = {}) {
  const historical = structuredClone(plan);
  historical.schema = LEGACY_ACTION_PLAN_SCHEMA;
  for (const action of historical.actions) {
    for (const step of action.steps) {
      delete step.wait_after;
      delete step.reobserve_after;
    }
  }
  const action = historical.actions[0];
  if (twoStep) {
    const [query, option] = action.steps.slice(1);
    action.steps = [
      {
        ...query,
        sequence: 1,
        value: '',
        normalized_action: { ...query.normalized_action, value: '' },
      },
      { ...option, sequence: 2 },
    ];
  } else if (filterFirst) {
    const [open, query, option] = action.steps;
    action.steps = [
      { ...query, sequence: 1 },
      { ...open, sequence: 2 },
      option,
    ];
  }
  return historical;
}

function resultInput(plan, outcomes, postObservation) {
  return {
    schema: ACTION_RESULT_SCHEMA,
    plan_id: plan.plan_id,
    post_observation_id: postObservation.observation_id,
    outcomes,
  };
}

test('schema and runtime expose strict plan/result roots', async () => {
  const schema = JSON.parse(await readFile(new URL('../schemas/browser-action-plan.schema.json', import.meta.url), 'utf8'));
  assert.deepEqual(schema.$defs.plan.properties.fallback_order.items.enum, [
    'omp_browser',
    'playwright_cli',
    'computer',
  ]);
  assert.deepEqual(schema.$defs.action.properties.semantic_action.enum, [
    'fill_text',
    'clear',
    'select_option',
    'toggle',
    'upload_file',
  ]);
  assert.deepEqual(schema.$defs.normalizedClear.required, [
    'action',
    'observationId',
    'fieldId',
    'controlReference',
  ]);
  assert.equal(
    schema.$defs.step.properties.value.oneOf[0].$ref,
    '#/$defs/possiblyEmptyString',
  );
  assert.equal(schema.$defs.plan.properties.schema.const, 'phase1-browser-action-plan-v2');
  assert.equal(ACTION_PLAN_SCHEMA, 'phase1-browser-action-plan-v2');
  assert.equal(LEGACY_ACTION_PLAN_SCHEMA, 'phase1-browser-action-plan-v1');
  assert.equal(ACTION_RESULT_SCHEMA, 'phase1-browser-action-result-v1');
});

test('creates a fill plan with exact private step arguments and normalized action', () => {
  const plan = textPlan();
  const step = plan.actions[0].steps[0];
  assert.equal(plan.mode, 'single_action');
  assert.equal(plan.actions[0].control_reference, 'ref-name');
  assert.equal(step.selector, '[id="name"]');
  assert.equal(step.value, 'Ada Lovelace');
  assert.equal(step.option_value, null);
  assert.equal(step.file_path, null);
  assert.equal(step.option_text, null);
  assert.equal(step.exact, true);
  assert.deepEqual(step.normalized_action, {
    action: 'fill_text',
    observationId: 'obs-1',
    fieldId: 'name',
    controlReference: 'ref-name',
    value: 'Ada Lovelace',
  });
});

test('creates a unique placeholder attribute selector', () => {
  const current = observation('obs-1', [control('location', {
    locator: {
      strategy: 'placeholder',
      value: 'Start typing...',
      role: 'combobox',
      name: null,
    },
  })]);
  const ledger = resolvedLedger(current, [{ field_id: 'location', value: 'Exact City' }]);
  const plan = createBrowserActionPlan({
    observation: current,
    ledger,
    decisions: [decision('location', 'fill_text', 'Exact City')],
    answerAliases: { location: { alias: 'location', value: 'Exact City' } },
    optionMatches: {},
    driver: 'omp_browser',
  });
  assert.equal(plan.actions[0].steps[0].selector, '[placeholder="Start typing..."]');
});

test('supports clear, toggle, native select, custom select, and upload actions', () => {
  const native = control('country', {
    kind: 'select',
    tag: 'select',
    type: 'select',
    role: 'combobox',
    value: 'us',
    options: [
      { value: 'us', label: 'United States', disabled: false, selected: true },
      { value: 'ca', label: 'Canada', disabled: false, selected: false },
    ],
  });
  const custom = control('department', {
    kind: 'input',
    type: 'text',
    role: 'combobox',
    value: '',
    value_present: false,
    options: [
      { value: '', label: 'Engineering', disabled: false, selected: false },
      { value: '', label: 'Sales', disabled: false, selected: false },
    ],
  });
  const check = control('consent', {
    kind: 'checkbox',
    type: 'checkbox',
    role: 'checkbox',
    value: null,
    value_present: false,
    checked: true,
  });
  const blank = control('optional', {
    required: false,
    value: '',
    value_present: false,
  });
  const file = control('resume', {
    kind: 'file',
    type: 'file',
    role: 'input',
    value: null,
    value_present: false,
    file: { accept: null, count: 0, names: [] },
  });
  const current = observation('obs-1', [native, custom, check, blank, file]);
  let ledger = resolvedLedger(current, [
    { field_id: 'country', value: 'us' },
    { field_id: 'department', value: 'Engineering' },
    { field_id: 'consent', value: true },
    { field_id: 'resume', value: '/tmp/resume.pdf' },
    { field_id: 'optional', value: null, semantic_choice: 'blank' },
  ]);
  const plans = [
    createBrowserActionPlan({
      observation: current,
      ledger,
      decisions: [decision('country', 'select_option', 'us')],
      answerAliases: { country: { alias: 'country', value: 'us' } },
      optionMatches: { country: { option_text: 'United States', option_value: 'us' } },
      driver: 'omp_browser',
    }),
    createBrowserActionPlan({
      observation: current,
      ledger,
      decisions: [decision('department', 'select_option', 'Engineering')],
      answerAliases: { department: { alias: 'department', value: 'Engineering' } },
      optionMatches: { department: { option_text: 'Engineering', option_value: 'Engineering' } },
      driver: 'omp_browser',
    }),
    createBrowserActionPlan({
      observation: current,
      ledger,
      decisions: [decision('consent', 'toggle', true)],
      answerAliases: { consent: { alias: 'consent', value: true } },
      optionMatches: {},
      driver: 'omp_browser',
    }),
    createBrowserActionPlan({
      observation: current,
      ledger,
      decisions: [decision('optional', 'clear', null)],
      answerAliases: { optional: { alias: 'blank', value: null } },
      optionMatches: {},
      driver: 'omp_browser',
    }),
    createBrowserActionPlan({
      observation: current,
      ledger,
      decisions: [decision('resume', 'upload_file', '/tmp/resume.pdf')],
      answerAliases: {},
      optionMatches: {},
      resumeUpload: { path: '/tmp/resume.pdf', sha256: FILE_DIGEST },
      driver: 'omp_browser',
    }),
  ];
  assert.equal(plans[0].actions[0].steps[0].helper, 'select');
  assert.equal(plans[1].actions[0].steps.length, 3);
  assert.equal(plans[1].actions[0].steps[0].helper, 'click');
  assert.equal(plans[1].actions[0].steps[1].helper, 'fill');
  assert.equal(plans[1].actions[0].steps[2].helper, 'click_exact_option');
  assert.equal(plans[1].actions[0].steps[2].option_value, 'Engineering');
  assert.equal(plans[2].actions[0].steps[0].normalized_action.checked, true);
  assert.equal(plans[3].actions[0].retention.kind, 'semantic_blank');
  assert.equal(plans[3].actions[0].steps[0].value, '');
  assert.deepEqual(plans[3].actions[0].steps[0].normalized_action, {
    action: 'clear',
    observationId: 'obs-1',
    fieldId: 'optional',
    controlReference: 'ref-optional',
  });
  assert.equal(plans[4].actions[0].retention.artifact_sha256, FILE_DIGEST);
  assert.equal(plans[4].actions[0].retention.expected_value_digest, ledger.fields.find((field) => field.field_id === 'resume').value_digest);
});
test('plans an exact custom option from a bounded external catalog before the flyout opens', () => {
  const current = observation('obs-1', [control('school', {
    kind: 'input',
    type: 'text',
    role: 'combobox',
    value: null,
    value_present: false,
    options: [],
  })]);
  const ledger = resolvedLedger(current, [{ field_id: 'school', value: '42' }]);
  const plan = createBrowserActionPlan({
    observation: current,
    ledger,
    decisions: [decision('school', 'select_option', '42')],
    answerAliases: { school: { alias: 'school', value: '42' } },
    optionMatches: { school: { option_text: 'Exact School', option_value: '42' } },
    driver: 'omp_browser',
  });
  assert.equal(plan.actions[0].steps.length, 3);
  assert.equal(plan.actions[0].steps[0].helper, 'click');
  assert.equal(plan.actions[0].steps[1].value, 'Exact School');
  assert.equal(plan.actions[0].steps[1].normalized_action.action, 'fill_text');
  assert.equal(plan.actions[0].steps[2].helper, 'click_exact_option');
  assert.equal(plan.actions[0].steps[2].option_text, 'Exact School');
  assert.throws(() => createBrowserActionPlan({
    observation: observation('obs-1', [control('school', {
      kind: 'select',
      tag: 'select',
      type: 'select',
      role: 'combobox',
      options: [],
    })]),
    ledger,
    decisions: [decision('school', 'select_option', '42')],
    answerAliases: { school: { alias: 'school', value: '42' } },
    optionMatches: { school: { option_text: 'Exact School', option_value: '42' } },
    driver: 'omp_browser',
  }));
});


test('opens a custom combobox before filling its option query', () => {
  const current = observation('obs-1', [control('role', {
    kind: 'input',
    type: 'text',
    role: 'combobox',
    value: null,
    value_present: false,
    options: [{ value: 'eng', label: 'Engineering', disabled: false, selected: false }],
  })]);
  const ledger = resolvedLedger(current, [{ field_id: 'role', value: 'eng' }]);
  const plan = createBrowserActionPlan({
    observation: current,
    ledger,
    decisions: [decision('role', 'select_option', 'eng')],
    answerAliases: { role: { alias: 'role', value: 'eng' } },
    optionMatches: { role: { option_text: 'Engineering', option_value: 'eng' } },
    driver: 'omp_browser',
  });
  const steps = plan.actions[0].steps;
  assert.deepEqual(steps.map((step) => step.helper), ['click', 'fill', 'click_exact_option']);
  assert.deepEqual(steps.map((step) => step.sequence), [1, 2, 3]);
  assert.deepEqual(steps.map((step) => step.normalized_action.action), ['click', 'fill_text', 'click']);
  assert.deepEqual(steps.map((step) => step.normalized_action.fieldId), ['role', 'role', 'role']);
});

test('creates an independent two-action fill batch and rejects mixed batches', () => {
  const current = observation('obs-1', [control('first'), control('second')]);
  const ledger = resolvedLedger(current, [
    { field_id: 'first', value: 'one' },
    { field_id: 'second', value: 'two' },
  ]);
  const plan = createBrowserActionPlan({
    observation: current,
    ledger,
    decisions: [decision('first', 'fill_text', 'one'), decision('second', 'fill_text', 'two')],
    answerAliases: {
      first: { alias: 'first', value: 'one' },
      second: { alias: 'second', value: 'two' },
    },
    optionMatches: {},
    driver: 'omp_browser',
  });
  assert.equal(plan.mode, 'fill_batch');
  assert.equal(plan.actions.length, 2);
  assert.throws(() => createBrowserActionPlan({
    observation: current,
    ledger,
    decisions: [decision('first', 'fill_text', 'one'), decision('second', 'toggle', true)],
    answerAliases: {
      first: { alias: 'first', value: 'one' },
      second: { alias: 'second', value: true },
    },
    optionMatches: {},
    driver: 'omp_browser',
  }));
});

test('enforces aliases, options, locator uniqueness, and digest binding', () => {
  const current = observation('obs-1', [control('name')]);
  const ledger = resolvedLedger(current, [{ field_id: 'name', value: 'Ada' }]);
  const base = {
    observation: current,
    ledger,
    decisions: [decision('name', 'fill_text', 'Ada')],
    answerAliases: { name: { alias: 'full_name', value: 'Ada' } },
    optionMatches: {},
    driver: 'omp_browser',
  };
  assert.throws(() => createBrowserActionPlan({ ...base, answerAliases: { name: { alias: 'full_name', value: 'wrong' } } }));
  assert.throws(() => createBrowserActionPlan({ ...base, answerAliases: { name: { alias: 'full_name', value: 'Ada' }, extra: { alias: 'x', value: 'x' } } }));
  assert.throws(() => createBrowserActionPlan({ ...base, decisions: [decision('name', 'final_submit', null)] }));
  const duplicateLocator = observation('obs-1', [control('name'), control('other', {
    locator: { strategy: 'id', value: 'name', role: null, name: null },
  })]);
  const duplicateLedger = resolvedLedger(duplicateLocator, [{ field_id: 'name', value: 'Ada' }]);
  assert.throws(() => createBrowserActionPlan({ ...base, observation: duplicateLocator, ledger: duplicateLedger }));
  const unknown = { ...base, answerAliases: { name: { alias: 'full_name', value: 'Ada' } }, extra: true };
  assert.throws(() => createBrowserActionPlan(unknown));
});

test('requires current observation, field/ref bindings, and a computer screenshot', () => {
  const plan = textPlan();
  const current = observation('obs-1', [control('name')]);
  const ledger = resolvedLedger(current, [{ field_id: 'name', value: 'Ada Lovelace' }]);
  assert.throws(() => validateBrowserActionPlan({ ...plan, observation_id: 'obs-old' }, { observation: current, ledger }));
  assert.throws(() => createBrowserActionPlan({
    observation: current,
    ledger,
    decisions: [decision('name', 'fill_text', 'Ada Lovelace', { controlReference: 'ref-old' })],
    answerAliases: { name: { alias: 'full_name', value: 'Ada Lovelace' } },
    optionMatches: {},
    driver: 'omp_browser',
  }));
  assert.throws(() => createBrowserActionPlan({
    observation: current,
    ledger,
    decisions: [decision('name', 'fill_text', 'Ada Lovelace')],
    answerAliases: { name: { alias: 'full_name', value: 'Ada Lovelace' } },
    optionMatches: {},
    driver: 'computer',
  }));
  assert.throws(() => validateBrowserActionPlan({ ...plan, fallback_order: ['omp_browser', 'omp_browser', 'computer'] }));
});

test('result validation returns ordered ledger-ready attempts and immutable receipts', () => {
  const plan = textPlan();
  const post = observation('obs-2', [control('name')], 'obs-1');
  const result = resultFor(plan, [{
    action_id: plan.actions[0].action_id,
    outcome: 'succeeded',
    error_code: null,
    driver: 'omp_browser',
    selected_option_text: null,
  }], post);
  assert.deepEqual(result.attempts, [{
    action_id: plan.actions[0].action_id,
    action: 'fill',
    field_id: 'name',
    observation_id: 'obs-1',
    ref: 'ref-name',
    outcome: 'succeeded',
    retry_of: null,
    error_code: null,
    source_sha256: null,
  }]);
  assert.deepEqual(result.formatted_values, [{ field_id: 'name', answer_alias: 'full_name', value: 'Ada Lovelace' }]);
  assert.deepEqual(result.upload_proofs, {});
  assert.equal(Object.isFrozen(result), true);
  assert.equal(Object.isFrozen(result.attempts), true);
  assert.equal(Object.isFrozen(result.attempts[0]), true);
  assert.throws(() => { result.attempts[0].outcome = 'failed'; }, TypeError);
});

test('result enforces success/error semantics, exact select text, chain, and batch prefixes', () => {
  const single = textPlan();
  const post = observation('obs-2', [control('name')], 'obs-1');
  const success = {
    action_id: single.actions[0].action_id,
    outcome: 'succeeded',
    error_code: null,
    driver: 'omp_browser',
    selected_option_text: null,
  };
  assert.throws(() => resultFor(single, [{ ...success, error_code: 'unexpected' }], post));
  assert.throws(() => resultFor(single, [{ ...success, outcome: 'failed' }], post));
  assert.throws(() => resultFor(single, [{ ...success, selected_option_text: 'unexpected' }], post));
  assert.throws(() => resultFor(single, [{ ...success }], observation('obs-3', [control('name')], 'obs-old')));

  const current = observation('obs-1', [control('first'), control('second'), control('third')]);
  const ledger = resolvedLedger(current, [
    { field_id: 'first', value: 'one' },
    { field_id: 'second', value: 'two' },
    { field_id: 'third', value: 'three' },
  ]);
  const batch = createBrowserActionPlan({
    observation: current,
    ledger,
    decisions: [
      decision('first', 'fill_text', 'one'),
      decision('second', 'fill_text', 'two'),
      decision('third', 'fill_text', 'three'),
    ],
    answerAliases: {
      first: { alias: 'first', value: 'one' },
      second: { alias: 'second', value: 'two' },
      third: { alias: 'third', value: 'three' },
    },
    optionMatches: {},
    driver: 'omp_browser',
  });
  const prefix = [
    {
      action_id: batch.actions[0].action_id,
      outcome: 'succeeded',
      error_code: null,
      driver: 'omp_browser',
      selected_option_text: null,
    },
    {
      action_id: batch.actions[1].action_id,
      outcome: 'failed',
      error_code: 'transport_failure',
      driver: 'playwright_cli',
      selected_option_text: null,
    },
  ];
  const prefixResult = resultFor(batch, prefix, post);
  assert.equal(prefixResult.attempts.length, 2);
  assert.equal(prefixResult.attempts[1].outcome, 'failed');
  assert.throws(() =>
    resultFor(batch, [prefix[0], { ...prefix[1], outcome: 'attempted', error_code: null }], post));
  assert.throws(() => resultFor(batch, [prefix[1], prefix[0]], post));
});
test('rejects v1 plans by default but validates them in historical mode', () => {
  const legacy = legacyPlan(textPlan());
  assert.throws(
    () => validateBrowserActionPlan(legacy),
    (error) => error.code === 'INVALID_SCHEMA',
  );
  const accepted = validateBrowserActionPlan(legacy, { historical: true });
  assert.deepEqual(accepted, legacy);
  assert.equal(Object.hasOwn(accepted.actions[0].steps[0], 'wait_after'), false);
  assert.equal(Object.isFrozen(accepted), true);

  for (const twoStep of [false, true]) {
    const custom = legacyPlan(customSelectPlan(), { twoStep });
    const validated = validateBrowserActionPlan(custom, { historical: true });
    assert.deepEqual(
      validated.actions[0].steps.map((step) => step.helper),
      twoStep
        ? ['fill', 'click_exact_option']
        : ['click', 'fill', 'click_exact_option'],
    );
  }
  const filterFirst = legacyPlan(customSelectPlan(), { filterFirst: true });
  assert.deepEqual(
    validateBrowserActionPlan(filterFirst, { historical: true })
      .actions[0].steps.map((step) => step.helper),
    ['fill', 'click', 'click_exact_option'],
  );
});

test('requires bounded wait and reobserve metadata for current v2 custom selects', () => {
  const plan = customSelectPlan();
  const missingWait = structuredClone(plan);
  delete missingWait.actions[0].steps[1].wait_after;
  assert.throws(
    () => validateBrowserActionPlan(missingWait),
    (error) => error.code === 'MISSING_KEY',
  );
  const missingReobserve = structuredClone(plan);
  delete missingReobserve.actions[0].steps[2].reobserve_after;
  assert.throws(
    () => validateBrowserActionPlan(missingReobserve),
    (error) => error.code === 'MISSING_KEY',
  );
});

test('historical results skip only committed selection while v2 rejects uncommitted success', () => {
  const current = customSelectPlan();
  const post = observation('obs-2', [control('role', {
    kind: 'input',
    type: 'text',
    role: 'combobox',
    options: [{ value: 'eng', label: 'Engineering', disabled: false, selected: false }],
  })], 'obs-1');
  const outcome = {
    action_id: current.actions[0].action_id,
    outcome: 'succeeded',
    error_code: null,
    driver: 'omp_browser',
    selected_option_text: 'Engineering',
  };
  assert.throws(
    () => validateBrowserActionResult(resultInput(current, [outcome], post), current, post),
    (error) => error.code === 'OPTION_SELECTION_UNCOMMITTED',
  );

  const historical = legacyPlan(current, { twoStep: true });
  const receipt = validateBrowserActionResult(
    resultInput(historical, [{ ...outcome, action_id: historical.actions[0].action_id }], post),
    historical,
    post,
    { historical: true },
  );
  assert.equal(receipt.attempts[0].outcome, 'succeeded');
  assert.throws(
    () => validateBrowserActionResult(
      resultInput(historical, [{ ...outcome, action_id: 'wrong-action' }], post),
      historical,
      post,
      { historical: true },
    ),
    (error) => error.code === 'OUTCOME_ORDER',
  );
  assert.throws(
    () => validateBrowserActionResult(
      resultInput(historical, [{ ...outcome, error_code: 'unexpected' }], post),
      historical,
      post,
      { historical: true },
    ),
    (error) => error.code === 'SUCCESS_ERROR_CODE',
  );
});

test('v2 upload success requires the planned basename committed in post-observation file metadata', () => {
  const plan = uploadPlan();
  const success = {
    action_id: plan.actions[0].action_id,
    outcome: 'succeeded',
    error_code: null,
    driver: 'omp_browser',
    selected_option_text: null,
  };
  const post = committedUploadPost({ accept: null, count: 1, names: ['resume.pdf'] });
  const result = resultFor(plan, [success], post);
  assert.deepEqual(result.upload_proofs, {
    resume: {
      field_id: 'resume',
      action_id: plan.actions[0].action_id,
      value_digest: plan.actions[0].retention.expected_value_digest,
      file_name: 'resume.pdf',
      source_sha256: FILE_DIGEST,
      observation_id: 'obs-2',
      container_identity: 'resume',
      committed_method: 'native_file_list',
    },
  });
  assert.equal(result.formatted_values.length, 0);
  assert.deepEqual(result.attempts, [{
    action_id: plan.actions[0].action_id,
    action: 'upload',
    field_id: 'resume',
    observation_id: 'obs-1',
    ref: 'ref-resume',
    outcome: 'succeeded',
    retry_of: null,
    error_code: null,
    source_sha256: 'b'.repeat(64),
  }]);

  const historical = legacyPlan(uploadPlan());
  const postWithoutFile = observation('obs-2', [], 'obs-1');
  const receipt = validateBrowserActionResult(
    resultInput(historical, [{ ...success, action_id: historical.actions[0].action_id }], postWithoutFile),
    historical,
    postWithoutFile,
    { historical: true },
  );
  assert.deepEqual(receipt.upload_proofs, {
    resume: {
      field_id: 'resume',
      action_id: historical.actions[0].action_id,
      value_digest: historical.actions[0].retention.expected_value_digest,
      file_name: 'resume.pdf',
      source_sha256: FILE_DIGEST,
      observation_id: 'obs-2',
      container_identity: 'resume',
      committed_method: 'rendered_container',
    },
  });

  const nativePost = committedUploadPost({ accept: null, count: 1, names: ['resume.pdf'] });
  const nativeReceipt = validateBrowserActionResult(
    resultInput(historical, [{ ...success, action_id: historical.actions[0].action_id }], nativePost),
    historical,
    nativePost,
    { historical: true },
  );
  assert.equal(nativeReceipt.upload_proofs.resume.committed_method, 'native_file_list');
});

test('semantic upload receipt records rendered-container commitment', () => {
  const plan = uploadPlan();
  const post = committedUploadPost({
    accept: null,
    count: 1,
    names: ['resume.pdf'],
    committed_method: 'rendered_container',
  });
  const result = resultFor(plan, [{
    action_id: plan.actions[0].action_id,
    outcome: 'succeeded',
    error_code: null,
    driver: 'omp_browser',
    selected_option_text: null,
  }], post);
  assert.equal(result.upload_proofs.resume.committed_method, 'rendered_container');
  assert.equal(result.upload_proofs.resume.observation_id, 'obs-2');
  assert.equal(result.upload_proofs.resume.container_identity, 'resume');
});

test('v2 upload success fails closed on missing, wrong, or ambiguous committed file state', () => {
  const plan = uploadPlan();
  const success = {
    action_id: plan.actions[0].action_id,
    outcome: 'succeeded',
    error_code: null,
    driver: 'omp_browser',
    selected_option_text: null,
  };
  assert.throws(
    () => resultFor(plan, [success], observation('obs-2', [], 'obs-1')),
    (error) => error.code === 'POST_CONTROL_BINDING',
  );
  assert.throws(
    () => resultFor(plan, [success], observation('obs-2', [control('other')], 'obs-1')),
    (error) => error.code === 'POST_CONTROL_BINDING',
  );
  const uncommitted = [
    null,
    { accept: null, count: 0, names: [] },
    { accept: null, count: 1, names: ['cover.pdf'] },
    { accept: null, count: 1, names: ['resume.pdf', 'cover.pdf'] },
    { accept: null, count: 2, names: ['resume.pdf', 'resume.pdf'] },
    { accept: null, count: 1, names: [] },
    { accept: null, count: 0, names: ['resume.pdf'] },
  ];
  for (const fileState of uncommitted) {
    assert.throws(
      () => resultFor(plan, [success], committedUploadPost(fileState)),
      (error) => error.code === 'UPLOAD_FILE_NOT_COMMITTED',
    );
  }
});

test('failed upload outcomes need no committed file and emit no proof', () => {
  const plan = uploadPlan();
  const result = resultFor(plan, [{
    action_id: plan.actions[0].action_id,
    outcome: 'failed',
    error_code: 'upload_rejected',
    driver: 'omp_browser',
    selected_option_text: null,
  }], observation('obs-2', [], 'obs-1'));
  assert.equal(result.attempts[0].outcome, 'failed');
  assert.deepEqual(result.upload_proofs, {});
});
