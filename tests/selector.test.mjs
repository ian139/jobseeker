import assert from 'node:assert/strict';
import test from 'node:test';

import {
  ANSWER_SOURCES_BY_POLICY,
  classifyFieldPolicy,
  selectNextApplicationWork,
  selectSafeApplicationBatch,
  SelectorError,
} from '../src/phase1/selector.mjs';

function control(id, overrides = {}) {
  return {
    ref: `ref-${id}`,
    stable_id: id,
    kind: 'input',
    tag: 'input',
    type: 'text',
    role: 'textbox',
    label: id,
    visible: true,
    enabled: true,
    required: false,
    readonly: false,
    disabled: false,
    value_present: false,
    options: [],
    validity: { valid: true, aria_invalid: false },
    candidate: { class: 'field' },
    ...overrides,
  };
}

function field(id, observationId = 'obs-1', overrides = {}) {
  return {
    field_id: id,
    latest_ref: `ref-${id}`,
    latest_observation_id: observationId,
    present_in_latest_observation: true,
    reachable: true,
    required: false,
    answer_state: 'unresolved',
    retained: false,
    valid: false,
    ...overrides,
  };
}

function observation(controls, observationId = 'obs-1') {
  return { observation_id: observationId, controls };
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

function plan(controls, fields, options = {}, observationId = 'obs-1', ledgerOverrides = {}) {
  return selectSafeApplicationBatch({
    observation: observation(controls, observationId),
    ledger: ledger(fields, observationId, ledgerOverrides),
    ...options,
  });
}

function next(controls, fields, options = {}, observationId = 'obs-1', ledgerOverrides = {}) {
  return selectNextApplicationWork({
    observation: observation(controls, observationId),
    ledger: ledger(fields, observationId, ledgerOverrides),
    ...options,
  });
}

test('classifies explicit, protected, subjective, qualification, and conservative policies', () => {
  assert.equal(classifyFieldPolicy({ field_policy: 'hard-fact', label: 'anything' }), 'hard_fact');
  assert.equal(classifyFieldPolicy({ label: 'Are you legally authorized to work?' }), 'legal');
  assert.equal(classifyFieldPolicy({ label: 'What is your gender identity?' }), 'demographic');
  assert.equal(classifyFieldPolicy({ label: 'Email address' }), 'identity');
  assert.equal(classifyFieldPolicy({ label: 'Why do you want this role?' }), 'subjective');
  assert.equal(classifyFieldPolicy({ label: 'Years of programming experience' }), 'qualification');
  assert.deepEqual(ANSWER_SOURCES_BY_POLICY.subjective, [
    'memory',
    'profile_verified',
    'profile_user_attested',
    'resume',
    'agent_inference',
    'user',
  ]);
  assert.deepEqual(ANSWER_SOURCES_BY_POLICY.legal, ['memory', 'profile_verified', 'profile_user_attested', 'resume', 'user']);
});

test('batches only independent ordinary fields', () => {
  const controls = [
    control('resume-answer', { required: true, label: 'Work history' }),
    control('memory-answer', { required: true, label: 'Preferred location' }),
    control('profile-answer', { required: true, label: 'Technical skills' }),
  ];
  const fields = controls.map((item) => field(item.stable_id, 'obs-1', {
    required: true,
    policy: 'qualification',
  }));
  const result = plan(controls, fields);

  assert.equal(result.mode, 'batch');
  assert.deepEqual(result.units.map((unit) => unit.fieldId), [
    'memory-answer',
    'profile-answer',
    'resume-answer',
  ]);
  assert.ok(result.units.every((unit) => unit.allowedAnswerSources.includes('memory')));
  assert.deepEqual(Object.keys(result.units[0]).sort(), [
    'allowedActions',
    'allowedAnswerSources',
    'allowedOptions',
    'controlReference',
    'escalationPermitted',
    'fieldId',
    'fieldPolicy',
    'observationId',
    'reobservationRequired',
    'requiredModelTier',
  ]);
});

test('rejects non-canonical configured answer sources', () => {
  assert.throws(
    () => plan(
      [control('field-1', { required: true, label: 'Preferred location' })],
      [field('field-1', 'obs-1', { required: true, policy: 'qualification' })],
      {
        fieldPolicies: {
          'field-1': { allowedAnswerSources: ['configured_default'] },
        },
      },
    ),
    (error) => error.code === 'E_SELECTOR_POLICY',
  );
});



test('keeps hazardous fields and controls single', () => {
  const cases = [
    {
      name: 'invalid',
      control: control('invalid', { label: 'Years of experience' }),
      field: field('invalid', 'obs-1', {
        policy: 'qualification',
        answer_state: 'answered',
        retained: true,
        valid: false,
        latest_state: { value_present: true, validity: { valid: false } },
      }),
    },
    {
      name: 'dependency',
      control: control('dependent', { label: 'Years of experience', dependency: true }),
      field: field('dependent', 'obs-1', { policy: 'qualification' }),
    },
    {
      name: 'upload',
      control: control('resume', {
        required: true,
        kind: 'file_upload',
        type: 'file',
        file: { count: 0, names: [] },
        label: 'Resume',
      }),
      field: field('resume', 'obs-1', { required: true, policy: 'qualification' }),
    },
    {
      name: 'select',
      control: control('choice', {
        required: true,
        kind: 'select',
        tag: 'select',
        role: 'combobox',
        options: ['One', 'Two'],
      }),
      field: field('choice', 'obs-1', { required: true, policy: 'qualification' }),
    },
  ];

  for (const item of cases) {
    const result = plan([item.control], [item.field]);
    assert.equal(result.mode, 'single', item.name);
    assert.equal(result.units.length, 1, item.name);
  }
});

test('orders required ordinary and newly revealed work and suppresses retained fields', () => {
  const ordinary = control('ordinary', { required: true, label: 'Current qualification' });
  const revealed = control('revealed', { required: true, label: 'New qualification' });
  const retained = control('retained', { label: 'Already answered' });
  const result = next(
    [revealed, retained, ordinary],
    [
      field('ordinary', 'obs-2', { required: true, policy: 'qualification' }),
      field('revealed', 'obs-2', { required: true, last_revealed_observation_id: 'obs-2' }),
      field('retained', 'obs-2', { retained: true, valid: true, answer_state: 'answered' }),
    ],
    {},
    'obs-2',
  );
  assert.equal(result.fieldId, 'ordinary');
});

test('returns continuation and final controls with null field IDs', () => {
  const continuation = control('continue', {
    kind: 'button',
    tag: 'button',
    role: 'button',
    label: 'Continue to review',
    candidate: { class: 'non_final_navigation' },
  });
  const final = control('submit', {
    kind: 'button',
    tag: 'button',
    type: 'submit',
    role: 'button',
    label: 'Submit application',
    candidate: { class: 'final_candidate' },
  });
  const continuationResult = next([continuation, final], [], { submissionReady: true });
  assert.equal(continuationResult.fieldId, null);
  assert.equal(continuationResult.controlReference, 'ref-continue');
  assert.deepEqual(continuationResult.allowedActions, ['navigate']);

  const finalResult = next([final], [], { submissionReady: true });
  assert.equal(finalResult.fieldId, null);
  assert.equal(finalResult.controlReference, 'ref-submit');
  assert.deepEqual(finalResult.allowedActions, ['click']);
  assert.equal(next([final], [], { submissionReady: false }), null);
});

test('rejects stale observations, malformed controls, and oversized batches', () => {
  assert.throws(
    () => next(
      [control('field')],
      [field('field', 'old-observation')],
      {},
      'new-observation',
    ),
    (error) => error instanceof SelectorError && error.code === 'E_SELECTOR_STALE',
  );
  assert.throws(
    () => next([null], []),
    (error) => error instanceof SelectorError && error.code === 'E_SELECTOR_INPUT',
  );
  assert.throws(
    () => plan(
      [control('a'), control('b'), control('c'), control('d')],
      ['a', 'b', 'c', 'd'].map((id) => field(id)),
      { maxBatchSize: 4 },
    ),
    /maxBatchSize.*1 through 3/i,
  );
});

test('returns deeply immutable output without mutating the observation input', () => {
  const source = {
    observation: observation([control('immutable', { label: 'Technical skills' })]),
    ledger: ledger([field('immutable')]),
  };
  const before = JSON.stringify(source);
  const result = next(source.observation.controls, source.ledger.fields, {});
  assert.equal(JSON.stringify(source), before);
  assert.equal(Object.isFrozen(result), true);
  assert.equal(Object.isFrozen(result.allowedActions), true);
  assert.equal(Object.isFrozen(result.allowedAnswerSources), true);
  assert.throws(() => result.allowedActions.push('fill_text'), TypeError);
});
