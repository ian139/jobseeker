import assert from 'node:assert/strict';
import test from 'node:test';

import {
  ANSWER_SOURCES,
  answerSourceIsAllowed,
  createLedger,
  digestPrivateValue,
  diffObservations,
  mergeObservation,
  recordActionBatch,
  recordActionAttempt,
  recordResolution,
  resolveFinalSubmitAttempt,
  validateLedger,
  validateObservation,
  verifyRetention,
} from '../src/phase1/ledger.mjs';
import { auditCompletion } from '../src/phase1/audit.mjs';

const SNAPSHOT = 'a'.repeat(64);

function frame() {
  return {
    id: 'frame-main',
    parent_id: null,
    url: 'https://example.test/app',
    origin: 'https://example.test',
    accessible: true,
  };
}

function control(stableId, overrides = {}) {
  return {
    ref: `ref-${stableId}`,
    stable_id: stableId,
    group_id: null,
    kind: 'text',
    tag: 'input',
    type: 'text',
    role: 'textbox',
    label: `Question ${stableId}`,
    name: stableId,
    description: null,
    locator: null,
    frame_id: 'frame-main',
    visible: true,
    enabled: true,
    required: true,
    readonly: false,
    disabled: false,
    value: `value-${stableId}`,
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

function finalCandidate() {
  return control('final-review', {
    ref: 'ref-final-review',
    stable_id: 'final-review',
    kind: 'button',
    tag: 'button',
    type: null,
    role: 'button',
    label: 'Review application',
    name: null,
    required: false,
    value: null,
    value_present: false,
    candidate: { class: 'final_candidate', reason: 'ready for OMP submission' },
  });
}

function nonFinalCandidate() {
  return control('continue-action', {
    ref: 'ref-continue-action',
    stable_id: 'continue-action',
    kind: 'button',
    tag: 'button',
    type: 'button',
    role: 'button',
    label: 'Continue',
    name: null,
    required: false,
    value: null,
    value_present: false,
    candidate: { class: 'non_final_navigation', reason: 'continue to next application page' },
  });
}

function blocker(code) {
  const labels = {
    access_control: 'Visible access-control UI',
    captcha: 'Visible CAPTCHA or anti-bot challenge',
  };
  return {
    code,
    label: labels[code] ?? 'Visible access-control UI',
    frame_id: 'frame-main',
    visible: true,
  };
}

function observation(id, controls, previous = null, blockers = []) {
  return {
    schema: 'phase1-observation-v1',
    observation_id: id,
    previous_observation_id: previous,
    observed_at: `2026-07-24T00:00:${id.slice(-1).padStart(2, '0')}.000Z`,
    url: 'https://example.test/app',
    title: 'Synthetic application',
    snapshot_sha256: SNAPSHOT,
    frames: [frame()],
    controls,
    blockers,
  };
}

function answer(ledger, fieldId, observationId, ref, value, extra = {}) {
  return recordResolution(ledger, {
    field_id: fieldId,
    observation_id: observationId,
    ref,
    source: 'user',
    value_digest: digestPrivateValue(value),
    ...extra,
  });
}

function fieldById(ledger, fieldId) {
  const field = ledger.fields.find((item) => item.field_id === fieldId);
  assert.ok(field, `missing field ${fieldId}`);
  return field;
}

test('accepts the observer v1 live shape and rejects unknown keys', () => {
  const select = control('select-field', {
    kind: 'select',
    tag: 'select',
    type: 'select',
    role: 'combobox',
    required: false,
    value: null,
    value_present: false,
    selected: [],
    options: [{ value: null, label: null, disabled: false, selected: false }],
    file: null,
  });
  const current = observation('obs-1', [select, finalCandidate()]);

  assert.equal(validateObservation(current), true);
  const ledger = createLedger(current);
  validateLedger(ledger);
  assert.equal(fieldById(ledger, 'select-field').latest_state.validity.aria_invalid, null);
  assert.deepEqual(fieldById(ledger, 'select-field').latest_state.selected, []);
  assert.deepEqual(fieldById(ledger, 'select-field').latest_state.option_states, [{
    label: null,
    selected: false,
    disabled: false,
  }]);
  assert.deepEqual(fieldById(ledger, 'select-field').latest_state.file, {
    accept: null,
    count: 0,
    present: false,
  });
  assert.throws(() => validateObservation({ ...current, answer: true }), /unknown key/);
});

test('merges the initial observation into a ledger with stable field identity', () => {
  const initial = observation('obs-1', [
    control('alpha', { value: 'alpha-answer' }),
    finalCandidate(),
  ]);
  const ledger = mergeObservation(createLedger(), initial);

  validateLedger(ledger);
  assert.equal(ledger.latest_observation_id, 'obs-1');
  assert.deepEqual(ledger.observation_ids, ['obs-1']);
  assert.deepEqual(ledger.diffs, [{
    schema: 'phase1-diff-v1',
    from_observation_id: null,
    to_observation_id: 'obs-1',
    added: [{ field_id: 'alpha', ref: 'ref-alpha', kind: 'text' }],
    removed: [],
    changed: [],
    blockers_added: [],
    blockers_removed: [],
  }]);
  assert.deepEqual(fieldById(ledger, 'alpha').ref_history, [{
    observation_id: 'obs-1',
    ref: 'ref-alpha',
  }]);
  assert.equal(ledger.fields.some((field) => field.field_id === 'final-review'), false);
});

test('reveals, disappears, and reappears controls without losing stable identity', () => {
  const first = observation('obs-1', [
    control('alpha', { ref: 'ref-alpha', value: 'alpha-answer' }),
    finalCandidate(),
  ]);
  let ledger = createLedger(first);
  let result = verifyRetention(
    answer(ledger, 'alpha', 'obs-1', 'ref-alpha', 'alpha-answer'),
    first,
  );
  assert.equal(result.ok, true);
  ledger = result.ledger;

  const revealed = observation('obs-2', [
    control('beta', { ref: 'ref-beta', value: 'beta-answer' }),
    finalCandidate(),
  ], 'obs-1');
  ledger = mergeObservation(ledger, revealed);
  assert.equal(fieldById(ledger, 'alpha').present_in_latest_observation, false);
  assert.equal(fieldById(ledger, 'beta').last_revealed_observation_id, 'obs-2');
  let audit = auditCompletion(ledger, revealed);
  assert.equal(audit.passed, false);
  assert.ok(audit.unresolved_field_ids.includes('beta'));
  assert.ok(audit.revealed_field_ids.includes('beta'));

  result = verifyRetention(
    answer(ledger, 'beta', 'obs-2', 'ref-beta', 'beta-answer'),
    revealed,
  );
  assert.equal(result.ok, true);
  ledger = result.ledger;
  assert.equal(auditCompletion(ledger, revealed).passed, true);

  const returned = observation('obs-3', [
    control('alpha', { ref: 'ref-alpha-returned', value: 'alpha-answer' }),
    finalCandidate(),
  ], 'obs-2');
  ledger = mergeObservation(ledger, returned);
  const returnedAlpha = fieldById(ledger, 'alpha');
  assert.equal(returnedAlpha.present_in_latest_observation, true);
  assert.equal(returnedAlpha.retained, false);
  assert.equal(returnedAlpha.last_revealed_observation_id, 'obs-3');
  assert.equal(fieldById(ledger, 'beta').present_in_latest_observation, false);
  assert.deepEqual(returnedAlpha.ref_history, [
    { observation_id: 'obs-1', ref: 'ref-alpha' },
    { observation_id: 'obs-3', ref: 'ref-alpha-returned' },
  ]);
  assert.throws(() => recordResolution(ledger, {
    field_id: 'alpha',
    observation_id: 'obs-3',
    ref: 'ref-alpha',
    source: 'user',
    value_digest: digestPrivateValue('alpha-answer'),
  }), /stale/i);
  result = verifyRetention(
    answer(ledger, 'alpha', 'obs-3', 'ref-alpha-returned', 'alpha-answer'),
    returned,
  );
  assert.equal(result.ok, true);
  audit = auditCompletion(result.ledger, returned);
  assert.equal(audit.passed, true);
});

test('emits stable diff output for additions, removals, changes, and blockers', () => {
  const before = observation('obs-1', [
    control('keep', { value: 'synthetic-before-value' }),
    control('removed'),
    finalCandidate(),
  ], null, [blocker('access_control')]);
  const after = observation('obs-2', [
    control('keep', {
      ref: 'ref-keep-new',
      group_id: 'moved-group',
      value: null,
      value_present: false,
    }),
    control('added'),
    finalCandidate(),
  ], 'obs-1', [blocker('captcha')]);

  const diff = diffObservations(before, after);
  assert.equal(diff.schema, 'phase1-diff-v1');
  assert.equal(diff.from_observation_id, 'obs-1');
  assert.equal(diff.to_observation_id, 'obs-2');
  assert.deepEqual(diff.added, [{ field_id: 'added', ref: 'ref-added', kind: 'text' }]);
  assert.deepEqual(diff.removed, [{ field_id: 'removed', ref: 'ref-removed', kind: 'text' }]);
  assert.deepEqual(diff.changed[0].field_id, 'keep');
  assert.deepEqual(diff.changed[0].changes.map((change) => change.property), ['ref', 'group_id', 'latest_state']);
  assert.deepEqual(diff.blockers_added, ['captcha']);
  assert.deepEqual(diff.blockers_removed, ['access_control']);
  assert.equal(JSON.stringify(diff).includes('synthetic-before-value'), false);
});

test('rejects stale observation chains, resolutions, and action references', () => {
  const first = observation('obs-1', [control('alpha'), finalCandidate()]);
  const ledger = createLedger(first);
  const wrongNext = observation('obs-2', [control('alpha')], 'obs-old');

  assert.throws(() => mergeObservation(ledger, wrongNext), /stale|next observation/i);
  assert.throws(() => diffObservations(first, wrongNext), /stale|chain/i);
  assert.throws(() => recordResolution(ledger, {
    field_id: 'alpha',
    observation_id: 'obs-1',
    ref: 'old-ref',
    source: 'user',
    value_digest: digestPrivateValue('alpha'),
  }), /stale/i);

  const staleObservation = observation('obs-0', [control('alpha'), finalCandidate()]);
  const staleRetention = verifyRetention(ledger, staleObservation);
  assert.equal(staleRetention.ok, false);
  assert.equal(staleRetention.errors[0].code, 'STALE_OBSERVATION');

  assert.throws(() => recordActionAttempt(ledger, {
    action_id: 'stale-action',
    action: 'fill',
    field_id: 'alpha',
    observation_id: 'obs-1',
    ref: 'old-ref',
    outcome: 'failed',
    error_code: 'stale-reference',
  }), /stale|latest reachable/i);
});

test('records routine fill batches atomically against one observation', () => {
  const current = observation('obs-1', [
    control('alpha'),
    control('beta'),
    control('gamma'),
    finalCandidate(),
  ]);
  const initial = createLedger(current);
  const batch = recordActionBatch(initial, [
    {
      action: 'fill',
      field_id: 'alpha',
      observation_id: 'obs-1',
      ref: 'ref-alpha',
      outcome: 'succeeded',
    },
    {
      action: 'fill',
      field_id: 'beta',
      observation_id: 'obs-1',
      ref: 'ref-beta',
      outcome: 'succeeded',
    },
  ]);

  assert.deepEqual(batch.action_attempts.map((action) => action.action_id), ['action-1', 'action-2']);
  assert.deepEqual(batch.action_attempts.map((action) => action.observation_id), ['obs-1', 'obs-1']);
  assert.throws(() => recordActionAttempt(batch, {
    action: 'fill',
    field_id: 'gamma',
    observation_id: 'obs-1',
    ref: 'ref-gamma',
    outcome: 'succeeded',
  }), /consumed|reobserve/i);

  const failed = recordActionBatch(initial, [
    {
      action: 'fill',
      field_id: 'alpha',
      observation_id: 'obs-1',
      ref: 'ref-alpha',
      outcome: 'succeeded',
    },
    {
      action: 'fill',
      field_id: 'beta',
      observation_id: 'obs-1',
      ref: 'ref-beta',
      outcome: 'failed',
      error_code: 'synthetic-failure',
    },
  ]);
  assert.deepEqual(failed.action_attempts.map((action) => action.outcome), ['succeeded', 'failed']);
  assert.deepEqual(failed.fields.find((field) => field.field_id === 'beta').retry_notes, ['synthetic-failure']);
  validateLedger(failed);
});

test('rejects invalid batch items before publishing or consuming the source ledger', () => {
  const current = observation('obs-1', [
    control('alpha'),
    control('beta'),
    control('gamma'),
    finalCandidate(),
  ]);
  const initial = createLedger(current);
  const valid = {
    action: 'fill',
    field_id: 'alpha',
    observation_id: 'obs-1',
    ref: 'ref-alpha',
    outcome: 'succeeded',
  };

  assert.throws(() => recordActionBatch(initial, [
    valid,
    {
      action: 'select',
      field_id: 'beta',
      observation_id: 'obs-1',
      ref: 'ref-beta',
      outcome: 'succeeded',
    },
  ]), /only routine fill/i);
  assert.equal(initial.action_attempts.length, 0);

  assert.throws(() => recordActionBatch(initial, [
    valid,
    { ...valid, field_id: 'alpha', ref: 'ref-alpha' },
  ]), /distinct/i);
  assert.throws(() => recordActionBatch(initial, [
    valid,
    { ...valid, field_id: 'beta', observation_id: 'obs-old', ref: 'ref-beta' },
  ]), /current/i);
  assert.throws(() => recordActionBatch(initial, [
    { ...valid, outcome: 'attempted' },
    { ...valid, field_id: 'beta', ref: 'ref-beta' },
  ]), /succeed|terminal/i);
  const uncertain = recordActionBatch(initial, [
    valid,
    { ...valid, field_id: 'beta', ref: 'ref-beta', outcome: 'attempted' },
  ]);
  assert.deepEqual(uncertain.action_attempts.map((action) => action.outcome), ['succeeded', 'attempted']);
  assert.throws(() => recordActionBatch(initial, [
    valid,
    {
      action: 'fill',
      field_id: 'final-review',
      observation_id: 'obs-1',
      ref: 'ref-final-review',
      outcome: 'succeeded',
    },
  ]), /unknown field|non-final/i);

  const consumed = recordActionAttempt(initial, valid);
  assert.throws(() => recordActionBatch(consumed, [
    {
      action: 'fill',
      field_id: 'beta',
      observation_id: 'obs-1',
      ref: 'ref-beta',
      outcome: 'succeeded',
    },
    {
      action: 'fill',
      field_id: 'gamma',
      observation_id: 'obs-1',
      ref: 'ref-gamma',
      outcome: 'succeeded',
    },
  ]), /consumed|reobserve/i);
});


test('accepts the declared answer-source precedence and rejects unknown sources', () => {
  const current = observation('obs-1', [control('source-field', { value: 'source-value' }), finalCandidate()]);
  let ledger = createLedger(current);
  assert.deepEqual(ANSWER_SOURCES, [
    'memory',
    'profile',
    'resume',
    'agent_inference',
    'user',
  ]);

  const inferenceMetadata = {
    inference_rationale_digest: 'b'.repeat(64),
    inference_evidence_digests: {
      resume_sha256: 'c'.repeat(64),
      job_description_sha256: 'd'.repeat(64),
    },
  };

  for (const source of ANSWER_SOURCES) {
    assert.equal(answerSourceIsAllowed(source), true);
    ledger = recordResolution(ledger, {
      field_id: 'source-field',
      observation_id: 'obs-1',
      ref: 'ref-source-field',
      source,
      value_digest: digestPrivateValue(`synthetic-${source}`),
      ...(source === 'agent_inference' ? inferenceMetadata : {}),
    });
    assert.equal(fieldById(ledger, 'source-field').answer_source, source);
  }
  assert.equal(answerSourceIsAllowed('untrusted'), false);
  assert.throws(() => recordResolution(ledger, {
    field_id: 'source-field',
    observation_id: 'obs-1',
    ref: 'ref-source-field',
    source: 'untrusted',
    value_digest: digestPrivateValue('synthetic-untrusted'),
  }), /invalid answer source/i);
});

test('treats one retained radio selection as a complete group', () => {
  const radioA = control('radio-a', {
    ref: 'ref-radio-a',
    kind: 'radio',
    tag: 'input',
    type: 'radio',
    role: 'radio',
    group_id: 'work-mode',
    name: 'work-mode',
    checked: false,
    value: 'remote',
    value_present: false,
  });
  const radioB = control('radio-b', {
    ref: 'ref-radio-b',
    kind: 'radio',
    tag: 'input',
    type: 'radio',
    role: 'radio',
    group_id: 'work-mode',
    name: 'work-mode',
    checked: true,
    value: 'onsite',
    value_present: true,
  });
  const current = observation('obs-1', [radioA, radioB, finalCandidate()]);
  let ledger = createLedger(current);
  const result = verifyRetention(
    answer(ledger, 'radio-b', 'obs-1', 'ref-radio-b', 'onsite'),
    current,
  );
  assert.equal(result.ok, true);
  ledger = result.ledger;
  const audit = auditCompletion(ledger, current);
  assert.equal(audit.passed, true);
  assert.deepEqual(audit.unresolved_field_ids, []);
  assert.deepEqual(audit.unretained_field_ids, []);
});

test('requires deliberate retained state for every grouped checkbox option', () => {
  const checkboxA = control('check-a', {
    ref: 'ref-check-a',
    kind: 'checkbox',
    type: 'checkbox',
    role: 'checkbox',
    group_id: 'consents',
    name: 'consents',
    checked: true,
    value: true,
    value_present: true,
  });
  const checkboxB = control('check-b', {
    ref: 'ref-check-b',
    kind: 'checkbox',
    type: 'checkbox',
    role: 'checkbox',
    group_id: 'consents',
    name: 'consents',
    checked: false,
    value: false,
    value_present: false,
  });
  const current = observation('obs-1', [checkboxA, checkboxB, finalCandidate()]);
  let ledger = createLedger(current);
  let result = verifyRetention(
    answer(ledger, 'check-a', 'obs-1', 'ref-check-a', true),
    current,
  );
  assert.equal(result.ok, true);
  ledger = result.ledger;
  let audit = auditCompletion(ledger, current);
  assert.equal(audit.passed, false);
  assert.ok(audit.unresolved_field_ids.includes('check-b'));

  ledger = recordResolution(ledger, {
    field_id: 'check-b',
    observation_id: 'obs-1',
    ref: 'ref-check-b',
    source: 'user',
    value_digest: null,
    semantic_choice: 'none',
  });
  result = verifyRetention(ledger, current);
  assert.equal(result.ok, true);
  ledger = result.ledger;
  audit = auditCompletion(ledger, current);
  assert.equal(audit.passed, true);
  assert.equal(fieldById(ledger, 'check-b').answer_state, 'blank');
  assert.equal(fieldById(ledger, 'check-b').retained, true);
});

test('retains an uploaded file only after a fresh observation proves it remains', () => {
  const empty = control('resume-file', {
    ref: 'ref-resume-file-empty',
    kind: 'input',
    tag: 'input',
    type: 'file',
    role: 'textbox',
    value: null,
    value_present: false,
    file: { accept: ['application/pdf'], count: 0, names: [] },
  });
  const first = observation('obs-1', [empty, finalCandidate()]);
  const proofDigest = digestPrivateValue('synthetic-upload-proof');
  let ledger = createLedger(first);
  ledger = recordActionAttempt(ledger, {
    action_id: 'upload-resume',
    action: 'upload',
    field_id: 'resume-file',
    observation_id: 'obs-1',
    ref: 'ref-resume-file-empty',
    outcome: 'succeeded',
  });

  const uploaded = observation('obs-2', [control('resume-file', {
    ref: 'ref-resume-file-uploaded',
    kind: 'input',
    tag: 'input',
    type: 'file',
    role: 'textbox',
    value: null,
    value_present: true,
    file: { accept: ['application/pdf'], count: 1, names: ['synthetic.pdf'] },
  }), finalCandidate()], 'obs-1');
  ledger = mergeObservation(ledger, uploaded);
  ledger = recordResolution(ledger, {
    field_id: 'resume-file',
    observation_id: 'obs-2',
    ref: 'ref-resume-file-uploaded',
    source: 'resume',
    value_digest: proofDigest,
  });
  let result = verifyRetention(ledger, uploaded, {
    'resume-file': {
      field_id: 'resume-file',
      value_digest: proofDigest,
      action_id: 'upload-resume',
      file_name: 'synthetic.pdf',
      source_sha256: 'a'.repeat(64),
      observation_id: 'obs-2',
      container_identity: 'resume-file',
      committed_method: 'native_file_list',
    },
  });
  assert.equal(result.ok, true);
  assert.equal(fieldById(result.ledger, 'resume-file').retained, true);

  const gone = observation('obs-3', [control('resume-file', {
    ref: 'ref-resume-file-new',
    kind: 'input',
    tag: 'input',
    type: 'file',
    role: 'textbox',
    value: null,
    value_present: false,
    file: { accept: ['application/pdf'], count: 0, names: [] },
  }), finalCandidate()], 'obs-2');
  ledger = mergeObservation(result.ledger, gone);
  result = verifyRetention(ledger, gone, {
    'resume-file': {
      field_id: 'resume-file',
      value_digest: proofDigest,
      action_id: 'upload-resume',
      file_name: 'synthetic.pdf',
      source_sha256: 'a'.repeat(64),
      observation_id: 'obs-3',
      container_identity: 'resume-file',
      committed_method: 'native_file_list',
    },
  });
  assert.equal(result.ok, false);
  assert.ok(result.errors.some((error) => error.code === 'INVALID_PROOF'));
  assert.equal(fieldById(result.ledger, 'resume-file').retained, false);
});

test('reports failed validation then passes after a corrected observation', () => {
  const invalid = observation('obs-1', [control('alpha', {
    ref: 'ref-alpha',
    value: 'alpha-answer',
    validity: { valid: false, aria_invalid: true, message: 'required' },
  }), finalCandidate()]);
  let ledger = createLedger(invalid);
  let result = verifyRetention(
    answer(ledger, 'alpha', 'obs-1', 'ref-alpha', 'alpha-answer'),
    invalid,
  );
  assert.equal(result.ok, false);
  assert.ok(result.errors.some((error) => error.code === 'INVALID_FIELD'));
  assert.ok(result.retry_notes.includes('alpha:validation-error'));

  const recovered = observation('obs-2', [control('alpha', {
    ref: 'ref-alpha-recovered',
    value: 'alpha-answer',
    validity: { valid: true, aria_invalid: null, message: null },
  }), finalCandidate()], 'obs-1');
  ledger = mergeObservation(result.ledger, recovered);
  assert.equal(fieldById(ledger, 'alpha').retained, false);
  result = verifyRetention(
    answer(ledger, 'alpha', 'obs-2', 'ref-alpha-recovered', 'alpha-answer'),
    recovered,
  );
  assert.equal(result.ok, true);
  assert.equal(fieldById(result.ledger, 'alpha').valid, true);
  assert.equal(fieldById(result.ledger, 'alpha').retained, true);
  assert.equal(auditCompletion(result.ledger, recovered).passed, true);
});

test('allows only UI-backed optional and sensitive deliberate choices', () => {
  const current = observation('obs-1', [
    control('optional-field', {
      kind: 'select',
      tag: 'select',
      type: null,
      role: 'combobox',
      required: false,
      value: 'not_applicable',
      value_present: true,
      selected: ['not_applicable'],
      options: [{ value: 'not_applicable', label: 'Not applicable', disabled: false, selected: true }],
    }),
    control('sensitive-field', {
      kind: 'select',
      tag: 'select',
      type: null,
      role: 'combobox',
      required: true,
      value: 'prefer_not_to_answer',
      value_present: true,
      selected: ['prefer_not_to_answer'],
      options: [{ value: 'prefer_not_to_answer', label: 'Prefer not to answer', disabled: false, selected: true }],
      label: 'Sensitive synthetic question',
    }),
    finalCandidate(),
  ]);
  let ledger = createLedger(current);
  assert.throws(() => recordResolution(ledger, {
    field_id: 'optional-field',
    observation_id: 'obs-1',
    ref: 'ref-optional-field',
    source: 'user',
    value_digest: null,
  }), /value digest or deliberate semantic choice/i);
  ledger = recordResolution(ledger, {
    field_id: 'optional-field',
    observation_id: 'obs-1',
    ref: 'ref-optional-field',
    source: 'user',
    value_digest: null,
    semantic_choice: 'not_applicable',
  });
  ledger = recordResolution(ledger, {
    field_id: 'sensitive-field',
    observation_id: 'obs-1',
    ref: 'ref-sensitive-field',
    source: 'user',
    value_digest: null,
    semantic_choice: 'prefer_not_to_answer',
    sensitive: true,
  });
  const result = verifyRetention(ledger, current);
  assert.equal(result.ok, true);
  assert.equal(fieldById(result.ledger, 'optional-field').answer_state, 'blank');
  assert.equal(fieldById(result.ledger, 'optional-field').optional, true);
  assert.equal(fieldById(result.ledger, 'sensitive-field').answer_state, 'blank');
  assert.equal(fieldById(result.ledger, 'sensitive-field').sensitive, true);
  assert.equal(auditCompletion(result.ledger, current).passed, true);
});
 
test('retains user-evidenced blanks for optional empty controls', () => {
  const current = observation('obs-1', [
    control('optional-empty-text', {
      kind: 'input',
      tag: 'input',
      type: 'text',
      role: 'textbox',
      required: false,
      value: null,
      value_present: false,
      options: [],
    }),
    control('optional-empty-file', {
      kind: 'input',
      tag: 'input',
      type: 'file',
      role: 'textbox',
      required: false,
      value: null,
      value_present: false,
      file: { accept: [], count: 0, names: [] },
      options: [],
    }),
    finalCandidate(),
  ]);
  let ledger = createLedger(current);
  for (const fieldId of ['optional-empty-text', 'optional-empty-file']) {
    ledger = recordResolution(ledger, {
      field_id: fieldId,
      observation_id: 'obs-1',
      ref: `ref-${fieldId}`,
      source: 'user',
      value_digest: null,
      semantic_choice: 'blank',
    });
  }
  const result = verifyRetention(ledger, current);
  assert.equal(result.ok, true);
  assert.equal(fieldById(result.ledger, 'optional-empty-text').retained, true);
  assert.equal(fieldById(result.ledger, 'optional-empty-file').retained, true);
  assert.equal(auditCompletion(result.ledger, current).passed, true);
});

test('blocks completion on observation blockers and unknown visible candidates', () => {
  const unknown = control('unknown-action', {
    kind: 'button',
    tag: 'button',
    type: 'button',
    role: 'button',
    label: 'Unclassified synthetic action',
    name: null,
    value: null,
    value_present: false,
    candidate: { class: 'unknown', reason: 'unclassified visible action' },
  });
  const current = observation('obs-1', [
    control('alpha', { value: 'alpha-answer' }),
    unknown,
    finalCandidate(),
  ], null, [blocker('captcha')]);
  let ledger = createLedger(current);
  let result = verifyRetention(
    answer(ledger, 'alpha', 'obs-1', 'ref-alpha', 'alpha-answer'),
    current,
  );
  assert.equal(result.ok, true);
  ledger = result.ledger;
  assert.deepEqual(ledger.unknown_candidates, [{
    stable_id: 'unknown-action',
    ref: 'ref-unknown-action',
    observation_id: 'obs-1',
    reason: 'unclassified visible action',
  }]);
  const audit = auditCompletion(ledger, current);
  assert.equal(audit.passed, false);
  assert.ok(audit.blockers.some((item) => item.code === 'observation-blocker:captcha'));
  assert.ok(audit.blockers.some((item) => item.code === 'unknown-control'));
});

test('requires a final candidate or an explicit final-review boundary', () => {
  const current = observation('obs-1', [control('alpha', { value: 'alpha-answer' })]);
  let ledger = createLedger(current);
  let result = verifyRetention(
    answer(ledger, 'alpha', 'obs-1', 'ref-alpha', 'alpha-answer'),
    current,
  );
  assert.equal(result.ok, true);
  ledger = result.ledger;
  let audit = auditCompletion(ledger, current);
  assert.equal(audit.passed, false);
  assert.deepEqual(audit.final_candidate_refs, []);
  assert.ok(audit.blockers.some((item) => item.code === 'no-final-boundary'));

  audit = auditCompletion(ledger, current, { final_review_boundary: true });
  assert.equal(audit.passed, true);
  assert.equal(audit.final_review_boundary, true);
});

test('counts final-submit begin events and resolves terminal outcomes', () => {
  const current = observation('obs-1', [
    control('alpha', { value: 'alpha-answer' }),
    finalCandidate(),
    nonFinalCandidate(),
  ]);
  let ledger = createLedger(current);
  let result = verifyRetention(
    answer(ledger, 'alpha', 'obs-1', 'ref-alpha', 'alpha-answer'),
    current,
  );
  assert.equal(result.ok, true);
  ledger = result.ledger;
  ledger = recordActionAttempt(ledger, {
    action: 'non_final_navigation',
    observation_id: 'obs-1',
    ref: 'ref-continue-action',
    outcome: 'succeeded',
  });
  assert.equal(ledger.action_attempts[0].retry_of, null);
  assert.equal(ledger.submit_action_count, 0);
  validateLedger(ledger);
  assert.equal(auditCompletion(ledger, current).passed, true);
  assert.throws(() => recordActionAttempt(ledger, {
    action_id: 'negative-retry',
    action: 'fill',
    field_id: 'alpha',
    observation_id: 'obs-1',
    ref: 'ref-alpha',
    outcome: 'retry',
    retry_of: -1,
  }), /bounded integer/i);
  assert.throws(() => recordActionAttempt(ledger, {
    action_id: 'generic-submit',
    action: 'submit',
    observation_id: 'obs-1',
    outcome: 'attempted',
  }), /invalid action/i);

  ledger = recordActionAttempt(ledger, {
    action_id: 'final-submit',
    action: 'final_submit',
    observation_id: 'obs-1',
    outcome: 'attempted',
  });
  assert.equal(ledger.submit_action_count, 1);
  assert.throws(() => recordActionAttempt(ledger, {
    action_id: 'direct-success',
    action: 'final_submit',
    observation_id: 'obs-1',
    outcome: 'succeeded',
  }), /begin|resolve/i);
  ledger = resolveFinalSubmitAttempt(ledger, {
    action_id: 'final-submit',
    outcome: 'succeeded',
    error_code: null,
  });
  assert.equal(ledger.submit_action_count, 1);
  assert.equal(ledger.action_attempts.at(-1).outcome, 'succeeded');
  const audit = auditCompletion(ledger, current);
  assert.equal(audit.passed, true);
  assert.equal(audit.submit_action_count, 1);
  assert.ok(!audit.blockers.some((item) => item.code === 'submit-action-recorded'));
  assert.throws(() => resolveFinalSubmitAttempt(ledger, {
    action_id: 'final-submit',
    outcome: 'failed',
    error_code: null,
  }), /already resolved|duplicate/i);
  assert.throws(() => resolveFinalSubmitAttempt(ledger, {
    action_id: 'missing',
    outcome: 'failed',
    error_code: null,
  }), /unknown/i);
});
