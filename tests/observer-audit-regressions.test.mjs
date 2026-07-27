import assert from 'node:assert/strict';
import test from 'node:test';

import {
  createLedger,
  digestPrivateValue,
  mergeObservation,
  recordResolution,
  recordActionAttempt,
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

function observation(id, controls, previous = null) {
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
    blockers: [],
  };
}

function resolve(ledger, fieldId, observationId, ref, value, source = 'user') {
  return recordResolution(ledger, {
    field_id: fieldId,
    observation_id: observationId,
    ref,
    source,
    value_digest: digestPrivateValue(value),
  });
}

test('audits role-radio controls as one selected group', () => {
  const radioA = control('radio-a', {
    kind: 'input',
    type: 'radio',
    role: 'radio',
    group_id: 'work-mode',
    name: 'work-mode',
    checked: false,
    value: 'remote',
    value_present: false,
  });
  const radioB = control('radio-b', {
    kind: 'input',
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
  const result = verifyRetention(resolve(ledger, 'radio-b', 'obs-1', 'ref-radio-b', 'onsite'), current);
  assert.equal(result.ok, true);
  ledger = result.ledger;

  const audit = auditCompletion(ledger, current);
  assert.equal(audit.passed, true);
  assert.deepEqual(audit.unresolved_field_ids, []);

  const partial = observation('obs-2', [radioB, finalCandidate()], 'obs-1');
  const partialLedger = mergeObservation(ledger, partial);
  assert.equal(auditCompletion(partialLedger, partial).passed, true);

  const disabledSibling = observation('obs-2', [{
    ...radioA,
    enabled: false,
    disabled: true,
  }, radioB, finalCandidate()], 'obs-1');
  const disabledSiblingLedger = mergeObservation(ledger, disabledSibling);
  assert.equal(auditCompletion(disabledSiblingLedger, disabledSibling).passed, true);

  const radioC = control('radio-c', {
    kind: 'input',
    type: 'radio',
    role: 'radio',
    group_id: 'work-mode',
    name: 'work-mode',
    checked: false,
    value: 'hybrid',
    value_present: false,
  });
  const expanded = observation('obs-2', [radioA, radioB, radioC, finalCandidate()], 'obs-1');
  const expandedLedger = mergeObservation(ledger, expanded);
  assert.equal(auditCompletion(expandedLedger, expanded).passed, true);
  const expandedGone = observation('obs-3', [finalCandidate()], 'obs-2');
  const expandedHistoricalLedger = mergeObservation(expandedLedger, expandedGone);
  assert.equal(auditCompletion(expandedHistoricalLedger, expandedGone).passed, true);

  const next = observation('obs-2', [finalCandidate()], 'obs-1');
  ledger = mergeObservation(ledger, next);
  const laterAudit = auditCompletion(ledger, next);
  assert.equal(laterAudit.passed, true);
  assert.deepEqual(laterAudit.unresolved_field_ids, []);
});

test('preserves explicit invalidity on an unselected radio option', () => {
  const invalid = control('radio-a', {
    kind: 'input',
    type: 'radio',
    role: 'radio',
    group_id: 'work-mode',
    name: 'work-mode',
    checked: false,
    value: 'remote',
    value_present: false,
    validity: { valid: false, aria_invalid: true, message: 'Invalid option' },
  });
  const selected = control('radio-b', {
    kind: 'input',
    type: 'radio',
    role: 'radio',
    group_id: 'work-mode',
    name: 'work-mode',
    checked: true,
    value: 'onsite',
    value_present: true,
  });
  const current = observation('obs-1', [invalid, selected, finalCandidate()]);
  let ledger = createLedger(current);
  const retained = verifyRetention(
    resolve(ledger, 'radio-b', 'obs-1', 'ref-radio-b', 'onsite'),
    current,
  );
  assert.equal(retained.ok, true);
  ledger = retained.ledger;
  const audit = auditCompletion(ledger, current);
  assert.equal(audit.passed, false);
  assert.ok(audit.invalid_field_ids.includes('radio-a'));
});

test('audits reused radio group identifiers as distinct active and historical groups', () => {
  const oldA = control('old-radio-a', {
    type: 'radio',
    role: 'radio',
    group_id: 'reused-group',
    checked: false,
    value: 'old-a',
    value_present: false,
  });
  const oldB = control('old-radio-b', {
    type: 'radio',
    role: 'radio',
    group_id: 'reused-group',
    checked: true,
    value: 'old-b',
    value_present: true,
  });
  const first = observation('obs-1', [oldA, oldB, finalCandidate()]);
  let ledger = createLedger(first);
  const retainedOld = verifyRetention(
    resolve(ledger, 'old-radio-b', 'obs-1', 'ref-old-radio-b', 'old-b'),
    first,
  );
  assert.equal(retainedOld.ok, true);
  ledger = retainedOld.ledger;

  const newA = control('new-radio-a', {
    type: 'radio',
    role: 'radio',
    group_id: 'reused-group',
    checked: false,
    value: 'new-a',
    value_present: false,
  });
  const newB = control('new-radio-b', {
    type: 'radio',
    role: 'radio',
    group_id: 'reused-group',
    checked: false,
    value: 'new-b',
    value_present: false,
  });
  const second = observation('obs-2', [newA, newB, finalCandidate()], 'obs-1');
  ledger = mergeObservation(ledger, second);
  const unresolvedAudit = auditCompletion(ledger, second);
  assert.equal(unresolvedAudit.passed, false);
  assert.ok(unresolvedAudit.unresolved_field_ids.includes('new-radio-a'));
  assert.ok(unresolvedAudit.unresolved_field_ids.includes('new-radio-b'));

  const absentWhileUnresolved = observation('obs-3', [finalCandidate()], 'obs-2');
  const unresolvedHistoricalLedger = mergeObservation(ledger, absentWhileUnresolved);
  const unresolvedHistoricalAudit = auditCompletion(
    unresolvedHistoricalLedger,
    absentWhileUnresolved,
  );
  assert.equal(unresolvedHistoricalAudit.passed, false);
  assert.ok(unresolvedHistoricalAudit.unresolved_field_ids.includes('new-radio-a'));
  assert.ok(unresolvedHistoricalAudit.unresolved_field_ids.includes('new-radio-b'));

  const third = observation('obs-3', [
    newA,
    { ...newB, checked: true, value_present: true },
    finalCandidate(),
  ], 'obs-2');
  ledger = mergeObservation(ledger, third);
  const retainedNew = verifyRetention(
    resolve(ledger, 'new-radio-b', 'obs-3', 'ref-new-radio-b', 'new-b'),
    third,
  );
  assert.equal(retainedNew.ok, true);
  const completedAudit = auditCompletion(retainedNew.ledger, third);
  assert.equal(completedAudit.passed, true);

  const fourth = observation('obs-4', [finalCandidate()], 'obs-3');
  const historicalLedger = mergeObservation(retainedNew.ledger, fourth);
  assert.equal(auditCompletion(historicalLedger, fourth).passed, true);
});

test('does not create historical debt for a never-reachable disabled field', () => {
  const disabled = control('conditional-disabled', {
    enabled: false,
    disabled: true,
    value: null,
    value_present: false,
    validity: { valid: false, aria_invalid: null, message: null },
  });
  const current = observation('obs-1', [disabled, finalCandidate()]);
  const ledger = createLedger(current);
  const audit = auditCompletion(ledger, current);
  assert.equal(audit.passed, true);
  assert.deepEqual(audit.unresolved_field_ids, []);
});

test('retains a completed field only when disabling preserves its value', () => {
  const first = observation('obs-1', [control('completed-field'), finalCandidate()]);
  let ledger = createLedger(first);
  const retained = verifyRetention(
    resolve(ledger, 'completed-field', 'obs-1', 'ref-completed-field', 'value-completed-field'),
    first,
  );
  assert.equal(retained.ok, true);
  ledger = retained.ledger;

  const disabled = observation('obs-2', [control('completed-field', {
    ref: 'ref-completed-field-disabled',
    enabled: false,
    disabled: true,
  }), finalCandidate()], 'obs-1');
  const disabledLedger = mergeObservation(ledger, disabled);
  const disabledField = disabledLedger.fields.find((field) => field.field_id === 'completed-field');
  assert.equal(disabledField.retained, true);
  assert.equal(disabledField.valid, true);
  assert.equal(auditCompletion(disabledLedger, disabled).passed, true);

  const disabledAgain = observation('obs-3', [control('completed-field', {
    ref: 'ref-completed-field-disabled-again',
    enabled: false,
    disabled: true,
  }), finalCandidate()], 'obs-2');
  const twiceDisabledLedger = mergeObservation(disabledLedger, disabledAgain);
  const twiceDisabledField = twiceDisabledLedger.fields.find(
    (field) => field.field_id === 'completed-field',
  );
  assert.equal(twiceDisabledField.retained, true);
  assert.equal(twiceDisabledField.valid, true);
  assert.equal(auditCompletion(twiceDisabledLedger, disabledAgain).passed, true);

  const changed = observation('obs-2', [control('completed-field', {
    ref: 'ref-completed-field-disabled',
    enabled: false,
    disabled: true,
    value: 'changed-after-completion',
  }), finalCandidate()], 'obs-1');
  const changedLedger = mergeObservation(ledger, changed);
  const changedAudit = auditCompletion(changedLedger, changed);
  assert.equal(changedAudit.passed, false);
  assert.ok(changedAudit.unretained_field_ids.includes('completed-field'));
});

test('retains a deliberate blank when its unchanged control becomes disabled', () => {
  const unchecked = control('optional-check', {
    kind: 'input',
    type: 'checkbox',
    role: 'checkbox',
    required: false,
    checked: false,
    value: false,
    value_present: false,
  });
  const first = observation('obs-1', [unchecked, finalCandidate()]);
  let ledger = createLedger(first);
  ledger = recordResolution(ledger, {
    field_id: 'optional-check',
    observation_id: 'obs-1',
    ref: 'ref-optional-check',
    source: 'user',
    value_digest: null,
    semantic_choice: 'none',
  });
  const retained = verifyRetention(ledger, first);
  assert.equal(retained.ok, true);

  const disabled = observation('obs-2', [{
    ...unchecked,
    ref: 'ref-optional-check-disabled',
    enabled: false,
    disabled: true,
  }, finalCandidate()], 'obs-1');
  const disabledLedger = mergeObservation(retained.ledger, disabled);
  const disabledField = disabledLedger.fields.find((field) => field.field_id === 'optional-check');
  assert.equal(disabledField.retained, true);
  assert.equal(disabledField.valid, true);
  assert.equal(auditCompletion(disabledLedger, disabled).passed, true);

  const disabledAgain = observation('obs-3', [{
    ...unchecked,
    ref: 'ref-optional-check-disabled-again',
    enabled: false,
    disabled: true,
  }, finalCandidate()], 'obs-2');
  const twiceDisabledLedger = mergeObservation(disabledLedger, disabledAgain);
  const twiceDisabledField = twiceDisabledLedger.fields.find(
    (field) => field.field_id === 'optional-check',
  );
  assert.equal(twiceDisabledField.retained, true);
  assert.equal(twiceDisabledField.valid, true);
  assert.equal(auditCompletion(twiceDisabledLedger, disabledAgain).passed, true);
});

test('blocks an unresolved reachable field after page navigation removes it', () => {
  const first = observation('obs-1', [control('prior-field'), finalCandidate()]);
  let ledger = createLedger(first);
  const second = observation('obs-2', [finalCandidate()], 'obs-1');
  ledger = mergeObservation(ledger, second);

  const audit = auditCompletion(ledger, second);
  assert.equal(audit.passed, false);
  assert.ok(audit.unresolved_field_ids.includes('prior-field'));
  assert.ok(audit.blockers.some((item) => item.code === 'unresolved-field' && item.field_id === 'prior-field'));
});

test('allows a proven non-file field to remain absent on a later page', () => {
  const first = observation('obs-1', [control('prior-field'), finalCandidate()]);
  let ledger = createLedger(first);
  const retained = verifyRetention(resolve(ledger, 'prior-field', 'obs-1', 'ref-prior-field', 'value-prior-field'), first);
  assert.equal(retained.ok, true);
  ledger = retained.ledger;

  const second = observation('obs-2', [finalCandidate()], 'obs-1');
  ledger = mergeObservation(ledger, second);
  const audit = auditCompletion(ledger, second);
  assert.equal(audit.passed, true);
});

test('blocks an answered file field when the later page omits its control', () => {
  const empty = control('resume-file', {
    kind: 'input',
    tag: 'input',
    type: 'file',
    role: 'textbox',
    value: null,
    value_present: false,
    file: { accept: ['application/pdf'], count: 0, names: [] },
  });
  const first = observation('obs-1', [empty, finalCandidate()]);
  let ledger = createLedger(first);
  const proofDigest = digestPrivateValue('resume-proof');
  ledger = recordActionAttempt(ledger, {
    action_id: 'upload-resume',
    action: 'upload',
    field_id: 'resume-file',
    observation_id: 'obs-1',
    ref: 'ref-resume-file',
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
    file: { accept: ['application/pdf'], count: 1, names: ['resume.pdf'] },
  }), finalCandidate()], 'obs-1');
  ledger = mergeObservation(ledger, uploaded);
  ledger = recordResolution(ledger, {
    field_id: 'resume-file',
    observation_id: 'obs-2',
    ref: 'ref-resume-file-uploaded',
    source: 'resume',
    value_digest: proofDigest,
  });
  const retained = verifyRetention(ledger, uploaded, {
    'resume-file': {
      value_digest: proofDigest,
      action_id: 'upload-resume',
      file_name: 'resume.pdf',
    },
  });
  assert.equal(retained.ok, true);
  ledger = retained.ledger;

  const second = observation('obs-3', [finalCandidate()], 'obs-2');
  ledger = mergeObservation(ledger, second);
  const audit = auditCompletion(ledger, second);
  assert.equal(audit.passed, false);
  assert.ok(audit.unretained_field_ids.includes('resume-file'));
  assert.ok(audit.blockers.some((item) => item.code === 'missing-file-field' && item.field_id === 'resume-file'));
});
