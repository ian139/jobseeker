import assert from 'node:assert/strict';
import test from 'node:test';

import {
  classifyFieldPolicy,
  selectNextApplicationWork,
  selectSafeApplicationBatch,
  SelectorError,
} from '../src/phase1/selector.mjs';

const DIGEST = 'a'.repeat(64);

function control(id, overrides = {}) {
  return {
    ref: `ref-${id}`,
    stable_id: id,
    kind: 'input',
    tag: 'input',
    type: 'text',
    role: 'textbox',
    label: id,
    name: id,
    description: null,
    visible: true,
    enabled: true,
    required: false,
    readonly: false,
    disabled: false,
    value: null,
    value_present: false,
    checked: null,
    selected: null,
    options: [],
    validity: { valid: true, aria_invalid: null, message: null },
    file: null,
    candidate: { class: 'field', reason: null },
    ...overrides,
  };
}

function field(id, observationId, overrides = {}) {
  return {
    field_id: id,
    latest_ref: `ref-${id}`,
    latest_observation_id: observationId,
    present_in_latest_observation: true,
    reachable: true,
    visible: true,
    enabled: true,
    required: false,
    answer_state: 'unresolved',
    retained: false,
    valid: false,
    latest_state: {
      value_present: false,
      validity: { valid: true, aria_invalid: null },
    },
    ...overrides,
  };
}

function observation(controls, id = 'obs-1') {
  return {
    observation_id: id,
    controls,
  };
}

function ledger(fields = [], observationId = 'obs-1', overrides = {}) {
  return {
    latest_observation_id: observationId,
    fields,
    action_attempts: [],
    current_candidate_refs: [],
    ...overrides,
  };
}

function select(controls, fields = [], options = {}, observationId = 'obs-1') {
  return selectNextApplicationWork({
    observation: observation(controls, observationId),
    ledger: ledger(fields, observationId),
    ...options,
  });
}

function plan(controls, fields = [], options = {}, observationId = 'obs-1', ledgerOverrides = {}) {
  return selectSafeApplicationBatch({
    observation: observation(controls, observationId),
    ledger: ledger(fields, observationId, ledgerOverrides),
    ...options,
  });
}

test('classifies explicit, protected, subjective, qualification, and conservative policies', () => {
  assert.equal(classifyFieldPolicy({ field_policy: 'hard-fact', label: 'anything' }), 'hard_fact');
  assert.equal(classifyFieldPolicy({ label: 'Are you legally authorized to work in the US?' }), 'legal');
  assert.equal(classifyFieldPolicy({ label: 'What is your gender identity?' }), 'demographic');
  assert.equal(classifyFieldPolicy({ label: 'Email address' }), 'identity');
  assert.equal(classifyFieldPolicy({ label: 'Why do you want this role?' }), 'subjective');
  assert.equal(classifyFieldPolicy({ label: 'Years of programming experience' }), 'qualification');
  assert.equal(classifyFieldPolicy({ field_id: 'school', label: 'School', policy: 'qualification' }, {
    fieldPolicies: { school: 'hard_fact' },
  }), 'hard_fact');
});

test('batches ordered independent routine text fields with a small cap', () => {
  const controls = [
    control('c', { required: true, label: 'Technical skills' }),
    control('a', { required: true, label: 'Years of experience' }),
    control('d', { required: true, label: 'Work history' }),
    control('b', { required: true, label: 'Programming languages' }),
  ];
  const fields = ['a', 'b', 'c', 'd'].map((id) => field(id, 'obs-1', {
    required: true,
    policy: 'qualification',
  }));
  const result = plan(controls, fields);

  assert.equal(result.mode, 'batch');
  assert.equal(result.observationId, 'obs-1');
  assert.deepEqual(result.units.map((unit) => unit.fieldId), ['a', 'b', 'c']);
  assert.deepEqual(result.units.map((unit) => unit.controlReference), ['ref-a', 'ref-b', 'ref-c']);
  assert.ok(result.units.every((unit) => unit.allowedActions[0] === 'fill_text'));
});

test('keeps hazardous next work in an explicit single batch', () => {
  const cases = [
    {
      name: 'invalid',
      controls: [control('invalid', { required: true, label: 'Years of experience' })],
      fields: [field('invalid', 'obs-1', {
        required: true,
        policy: 'qualification',
        answer_state: 'answered',
        retained: true,
        valid: false,
        latest_state: { value_present: true, validity: { valid: false, aria_invalid: true } },
      })],
    },
    {
      name: 'retry',
      controls: [control('retry', { required: true, label: 'Years of experience' })],
      fields: [field('retry', 'obs-1', { required: true, policy: 'qualification' })],
      ledger: {
        action_attempts: [{
          action: 'fill_text',
          field_id: 'retry',
          observation_id: 'obs-1',
          ref: 'ref-retry',
          outcome: 'retry',
          stale_ref: false,
        }],
      },
    },
    {
      name: 'newly revealed',
      controls: [control('revealed', { required: true, label: 'Years of experience' })],
      fields: [field('revealed', 'obs-1', {
        required: true,
        policy: 'qualification',
        last_revealed_observation_id: 'obs-1',
      })],
    },
    {
      name: 'dependency',
      controls: [control('dependent', { required: true, label: 'Years of experience', dependency: true })],
      fields: [field('dependent', 'obs-1', { required: true, policy: 'qualification' })],
    },
    {
      name: 'file',
      controls: [control('resume', {
        required: true,
        kind: 'file_upload',
        type: 'file',
        label: 'Resume',
        file: { accept: ['.pdf'], count: 0, names: [] },
      })],
      fields: [field('resume', 'obs-1', { required: true, policy: 'qualification' })],
    },
    {
      name: 'custom widget',
      controls: [control('custom', {
        required: true,
        kind: 'aria',
        tag: 'div',
        role: 'textbox',
        label: 'Years of experience',
        native: false,
      })],
      fields: [field('custom', 'obs-1', { required: true, policy: 'qualification' })],
    },
    {
      name: 'choice',
      controls: [control('choice', {
        required: true,
        kind: 'select',
        tag: 'select',
        role: 'combobox',
        label: 'Years of experience',
        options: ['One', 'Two'],
      })],
      fields: [field('choice', 'obs-1', { required: true, policy: 'qualification' })],
    },
    {
      name: 'toggle',
      controls: [control('toggle', {
        required: true,
        type: 'checkbox',
        role: 'checkbox',
        label: 'Years of experience',
      })],
      fields: [field('toggle', 'obs-1', { required: true, policy: 'qualification' })],
    },
    {
      name: 'restricted policy',
      controls: [control('legal', { required: true, label: 'Are you legally authorized to work?' })],
      fields: [field('legal', 'obs-1', { required: true, policy: 'legal' })],
    },
    {
      name: 'model inference',
      controls: [control('inference', { label: 'Why do you want this role?' })],
      fields: [field('inference', 'obs-1')],
    },
    {
      name: 'user escalation',
      controls: [control('escalation', { required: true, label: 'Years of experience' })],
      fields: [field('escalation', 'obs-1', {
        required: true,
        policy: 'qualification',
        escalation_permitted: true,
      })],
    },
    {
      name: 'navigation',
      controls: [control('continue', {
        kind: 'button',
        tag: 'button',
        type: 'button',
        role: 'button',
        label: 'Continue to review',
        candidate: { class: 'non_final_navigation', reason: 'continue' },
      })],
    },
    {
      name: 'final',
      controls: [control('submit', {
        kind: 'button',
        tag: 'button',
        type: 'submit',
        role: 'button',
        label: 'Submit application',
        candidate: { class: 'final_candidate', reason: 'submit' },
      })],
      options: { submissionReady: true },
    },
  ];

  for (const item of cases) {
    const result = plan(item.controls, item.fields ?? [], item.options ?? {}, 'obs-1', item.ledger ?? {});
    assert.equal(result.mode, 'single', item.name);
    assert.equal(result.units.length, 1, item.name);
  }
});

test('selects invalid or rejected completed fields before all other work', () => {
  const invalid = control('invalid', { label: 'Preferred name' });
  const required = control('required', { required: true, label: 'Required answer' });
  const result = select(
    [invalid, required],
    [
      field('invalid', 'obs-1', {
        answer_state: 'answered',
        retained: true,
        valid: false,
        latest_state: { value_present: true, validity: { valid: false, aria_invalid: true } },
      }),
      field('required', 'obs-1', { required: true }),
    ],
  );
  assert.equal(result.fieldId, 'invalid');
  assert.deepEqual(result.allowedActions, ['fill_text']);
  assert.equal(result.fieldPolicy, 'identity');
});

test('selects ordinary required visible unresolved work before newly revealed fields', () => {
  const ordinary = control('ordinary', { required: true, label: 'Current qualification' });
  const revealed = control('revealed', { required: true, label: 'New required qualification' });
  const result = select(
    [ordinary, revealed],
    [
      field('ordinary', 'obs-2', { required: true }),
      field('revealed', 'obs-2', {
        required: true,
        last_revealed_observation_id: 'obs-2',
      }),
    ],
    {},
    'obs-2',
  );
  assert.equal(result.fieldId, 'ordinary');
  assert.equal(result.observationId, 'obs-2');
});

test('selects required newly revealed work before a required upload', () => {
  const revealed = control('revealed', { required: true, label: 'New required field' });
  const upload = control('resume', {
    required: true,
    kind: 'file_upload',
    type: 'file',
    role: 'button',
    label: 'Resume',
    file: { accept: '.pdf', count: 0, names: [] },
  });
  const result = select(
    [revealed, upload],
    [
      field('revealed', 'obs-2', { required: true, last_revealed_observation_id: 'obs-2' }),
      field('resume', 'obs-2', { required: true }),
    ],
    {},
    'obs-2',
  );
  assert.equal(result.fieldId, 'revealed');
});

test('selects required upload after ordinary and newly revealed fields', () => {
  const upload = control('resume', {
    required: true,
    kind: 'file_upload',
    type: 'file',
    label: 'Resume',
    file: { accept: '.pdf', count: 0, names: [] },
  });
  const result = select(
    [upload],
    [field('resume', 'obs-1', { required: true })],
  );
  assert.equal(result.fieldId, 'resume');
  assert.deepEqual(result.allowedActions, ['upload_file']);
  assert.deepEqual(result.allowedAnswerSources, ['resume_evidence']);
});

test('selects optional exact memory or configured default before optional inference', () => {
  const memory = control('memory', { label: 'Preferred work location' });
  const defaulted = control('defaulted', { label: 'Work authorization status' });
  const inferable = control('inferable', { label: 'Why are you interested in this role?' });
  const result = select(
    [memory, defaulted, inferable],
    [
      field('memory', 'obs-1'),
      field('defaulted', 'obs-1', { policy: 'legal' }),
      field('inferable', 'obs-1'),
    ],
    {
      answerMemory: [{ schema: 'answer-v1', alias: 'memory', value: 'Remote' }],
      configuredDefaults: { defaulted: 'decline' },
    },
  );
  assert.equal(result.fieldId, 'memory');
  assert.equal(result.configuredFallback, null);

  const defaultResult = select(
    [defaulted, inferable],
    [
      field('defaulted', 'obs-1', { policy: 'legal' }),
      field('inferable', 'obs-1'),
    ],
    { configuredDefaults: { defaulted: 'decline' } },
  );
  assert.equal(defaultResult.fieldId, 'defaulted');
  assert.equal(defaultResult.configuredFallback, 'decline');
  assert.deepEqual(defaultResult.allowedAnswerSources, [
    'exact_memory',
    'configured_default',
    'configured_decline',
    'require_user',
  ]);
});

test('selects optional inferable subjective work after exact/default work', () => {
  const controlValue = control('essay', { label: 'Tell us about a project you are proud of' });
  const result = select(
    [controlValue],
    [field('essay', 'obs-1')],
  );
  assert.equal(result.fieldId, 'essay');
  assert.equal(result.fieldPolicy, 'subjective');
  assert.equal(result.requiredModelTier, 'cheap');
  assert.equal(result.escalationPermitted, true);
  assert.deepEqual(result.allowedActions, ['fill_text']);
});

test('uses deterministic field-id tie breaks and suppresses retained valid fields', () => {
  const retained = control('a-retained', { label: 'Optional exact answer' });
  const selected = control('b-selected', { label: 'Optional exact answer' });
  const result = select(
    [selected, retained],
    [
      field('a-retained', 'obs-1', { retained: true, valid: true, answer_state: 'answered' }),
      field('b-selected', 'obs-1'),
    ],
    { answerMemory: { 'b-selected': 'answer' } },
  );
  assert.equal(result.fieldId, 'b-selected');

  const tied = select(
    [control('zeta'), control('alpha')],
    [field('zeta', 'obs-1'), field('alpha', 'obs-1')],
  );
  assert.equal(tied.fieldId, 'alpha');
});

test('does not repeat a successful current action but ignores stale action evidence', () => {
  const first = control('first', { label: 'First answer' });
  const second = control('second', { label: 'Second answer' });
  const result = selectNextApplicationWork({
    observation: observation([first, second]),
    ledger: ledger(
      [field('first', 'obs-1'), field('second', 'obs-1')],
      'obs-1',
      {
        action_attempts: [
          {
            action: 'fill',
            field_id: 'first',
            observation_id: 'obs-1',
            ref: 'ref-first',
            outcome: 'succeeded',
            stale_ref: false,
          },
          {
            action: 'fill',
            field_id: 'second',
            observation_id: 'obs-0',
            ref: 'old-second-ref',
            outcome: 'succeeded',
            stale_ref: true,
          },
        ],
      },
    ),
  });
  assert.equal(result.fieldId, 'second');
});

test('returns normalized continuation/review and final page actions with null field IDs', () => {
  const continuation = control('continue', {
    kind: 'button',
    tag: 'button',
    type: 'button',
    role: 'button',
    label: 'Continue to review',
    candidate: { class: 'non_final_navigation', reason: 'continue' },
  });
  const final = control('submit', {
    kind: 'button',
    tag: 'button',
    type: 'submit',
    role: 'button',
    label: 'Submit application',
    candidate: { class: 'final_candidate', reason: 'submit' },
  });
  const continuationResult = select(
    [continuation, final],
    [],
    { submissionReady: true },
  );
  assert.equal(continuationResult.fieldId, null);
  assert.equal(continuationResult.controlReference, 'ref-continue');
  assert.equal(continuationResult.fieldPolicy, null);
  assert.deepEqual(continuationResult.allowedActions, ['navigate']);

  const finalResult = select(
    [final],
    [],
    { submissionReady: true },
  );
  assert.equal(finalResult.fieldId, null);
  assert.equal(finalResult.controlReference, 'ref-submit');
  assert.deepEqual(finalResult.allowedActions, ['click']);

  assert.equal(select([final], [], { submissionReady: false }), null);
});

test('throws stable selector errors for stale observations and malformed controls', () => {
  assert.throws(
    () => selectNextApplicationWork({
      observation: observation([control('field')], 'obs-2'),
      ledger: ledger([field('field', 'obs-1')], 'obs-1'),
    }),
    (error) => error instanceof SelectorError && error.code === 'E_SELECTOR_STALE',
  );
  assert.throws(
    () => selectNextApplicationWork({
      observation: observation([control('field')]),
      ledger: ledger([field('field', 'obs-1', { latest_ref: 'old-ref' })]),
    }),
    (error) => error instanceof SelectorError && error.code === 'E_SELECTOR_STALE',
  );
  assert.throws(
    () => selectNextApplicationWork({ observation: { observation_id: 'obs-1', controls: [null] } }),
    (error) => error instanceof SelectorError && error.code === 'E_SELECTOR_INPUT',
  );
  assert.throws(
    () => plan(
      [control('alpha'), control('beta'), control('gamma'), control('delta')],
      ['alpha', 'beta', 'gamma', 'delta'].map((id) => field(id, 'obs-1')),
      { maxBatchSize: 4 },
    ),
    /maxBatchSize.*1 through 3/i,
  );
});

test('returns deeply immutable output without mutating snake_case inputs', () => {
  const controls = [control('immutable', { label: 'Why this role?' })];
  const fields = [field('immutable', 'obs-1')];
  const source = { observation: observation(controls), ledger: ledger(fields) };
  const before = JSON.stringify(source);
  const result = selectNextApplicationWork(source);
  assert.equal(JSON.stringify(source), before);
  assert.equal(Object.isFrozen(result), true);
  assert.equal(Object.isFrozen(result.allowedActions), true);
  assert.equal(Object.isFrozen(result.allowedAnswerSources), true);
  assert.throws(() => { result.allowedActions.push('fill'); }, TypeError);
  assert.deepEqual(result.allowedActions, ['fill_text']);
  assert.equal(DIGEST.length, 64);
});
