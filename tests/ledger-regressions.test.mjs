import assert from 'node:assert/strict';
import test from 'node:test';

import {
  createLedger,
  digestPrivateValue,
  markTargetSensitive,
  mergeObservation,
  recordActionAttempt,
  recordActionBatch,
  recordResolution,
  validateLedger,
  validateObservation,
  verifyRetention,
} from '../src/phase1/ledger.mjs';
import { auditCompletion } from '../src/phase1/audit.mjs';

const DIGEST = 'a'.repeat(64);

function visualTarget(targetId, overrides = {}) {
  return {
    target_id: targetId,
    field_id: targetId,
    group_id: null,
    kind: 'text',
    label: `Question ${targetId}`,
    description: null,
    bounds: { x: 12, y: 18, width: 320, height: 44 },
    visible: true,
    enabled: true,
    required: true,
    readonly: false,
    value_state: 'present',
    checked: null,
    selected: null,
    options: [],
    validation: { valid: true, message_present: false },
    file: null,
    candidate: { class: 'field', reason: 'visible application field' },
    confidence: 0.99,
    ...overrides,
  };
}

function finalTarget(targetId = 'final-submit') {
  return visualTarget(targetId, {
    field_id: null,
    kind: 'button',
    label: 'Submit application',
    required: false,
    value_state: 'blank',
    candidate: { class: 'final_candidate', reason: 'current final action' },
  });
}

function visualObservation(observationId, targets, previousObservationId = null, blockers = []) {
  return {
    schema: 'phase1-visual-observation-v1',
    observation_id: observationId,
    previous_observation_id: previousObservationId,
    observed_at: '2026-07-28T00:00:00.000Z',
    surface: {
      surface_id: 'surface-application',
      url: 'https://example.invalid/apply',
      title: 'Synthetic application',
      screenshot_sha256: DIGEST,
      viewport: { width: 1280, height: 720 },
    },
    agent: { provider: 'codex', model: 'vision-test' },
    targets,
    blockers,
  };
}

function field(ledger, fieldId) {
  const result = ledger.targets.find((item) => item.field_id === fieldId);
  assert.ok(result, `missing field ${fieldId}`);
  return result;
}

function deliberateBlank(ledger, fieldId, observationId, targetId, choice = 'none') {
  return recordResolution(ledger, {
    field_id: fieldId,
    observation_id: observationId,
    target_id: targetId,
    source: 'user',
    value_digest: null,
    semantic_choice: choice,
  });
}

function answered(ledger, fieldId, observationId, targetId, source = 'user', extra = {}) {
  return recordResolution(ledger, {
    field_id: fieldId,
    observation_id: observationId,
    target_id: targetId,
    source,
    value_digest: digestPrivateValue(`answer-${fieldId}`),
    ...extra,
  });
}

test('tracks blocker deltas and candidate targets across chained images', () => {
  const first = visualObservation('observation-1', [visualTarget('alpha'), finalTarget()], null, [
    'captcha',
    { code: 'access-control', message: 'Visible gate', visible: true },
  ]);
  let ledger = createLedger(first);
  assert.deepEqual(ledger.active_blockers, ['access-control', 'captcha']);

  const second = visualObservation('observation-2', [visualTarget('alpha'), finalTarget()], 'observation-1', ['captcha']);
  ledger = mergeObservation(ledger, second);
  assert.deepEqual(ledger.active_blockers, ['captcha']);
  assert.deepEqual(ledger.diffs[1].blockers_removed, ['access-control']);

  const third = visualObservation('observation-3', [visualTarget('alpha')], 'observation-2');
  ledger = mergeObservation(ledger, third);
  assert.deepEqual(ledger.current_candidate_targets, []);
  validateLedger(ledger);
});

test('accepts only current target bindings and consumes images for mutations', () => {
  const first = visualObservation('observation-1', [visualTarget('alpha'), visualTarget('beta'), finalTarget()]);
  const ledger = createLedger(first);

  assert.throws(() => recordActionAttempt(ledger, {
    action: 'type_text',
    field_id: 'alpha',
    target_id: 'alpha',
    observation_id: 'observation-1',
    outcome: 'succeeded',
    unexpected: true,
  }), /unknown key/);
  assert.throws(() => recordActionAttempt(ledger, {
    action: 'type_text',
    field_id: 'alpha',
    target_id: 'old-alpha',
    observation_id: 'observation-1',
  }), /stale/i);
  assert.throws(() => recordActionAttempt(ledger, {
    action: 'final_submit',
    target_id: 'alpha',
    observation_id: 'observation-1',
  }), /candidate|stale/i);

  const mutated = recordActionAttempt(ledger, {
    action_id: 'type-alpha',
    action: 'type_text',
    field_id: 'alpha',
    target_id: 'alpha',
    observation_id: 'observation-1',
    outcome: 'succeeded',
  });
  assert.equal(field(mutated, 'alpha').retained, false);
  assert.throws(() => recordActionAttempt(mutated, {
    action: 'type_text',
    field_id: 'beta',
    target_id: 'beta',
    observation_id: 'observation-1',
  }), /consumed|observe again/i);
});

test('publishes bounded action batches atomically and rejects invalid members', () => {
  const first = visualObservation('observation-1', [visualTarget('alpha'), visualTarget('beta'), visualTarget('gamma'), finalTarget()]);
  const ledger = createLedger(first);
  const batch = recordActionBatch(ledger, [
    { action: 'type_text', field_id: 'alpha', target_id: 'alpha', observation_id: 'observation-1', outcome: 'succeeded' },
    { action: 'type_text', field_id: 'beta', target_id: 'beta', observation_id: 'observation-1', outcome: 'failed', error_code: 'input-rejected' },
  ]);
  assert.deepEqual(batch.action_attempts.map((item) => item.action_id), ['action-1', 'action-2']);
  assert.deepEqual(field(batch, 'beta').retry_notes, ['input-rejected']);
  validateLedger(batch);

  assert.throws(() => recordActionBatch(ledger, [
    { action: 'click', field_id: 'alpha', target_id: 'alpha', observation_id: 'observation-1', outcome: 'succeeded' },
    { action: 'type_text', field_id: 'beta', target_id: 'beta', observation_id: 'observation-1', outcome: 'succeeded' },
  ]), /type_text only/i);
  assert.throws(() => recordActionBatch(ledger, [
    { action: 'type_text', field_id: 'alpha', target_id: 'alpha', observation_id: 'observation-1', outcome: 'succeeded' },
    { action: 'type_text', field_id: 'alpha', target_id: 'alpha', observation_id: 'observation-1', outcome: 'succeeded' },
  ]), /distinct/i);
});

test('requires inference evidence and clears derived answers on sensitivity changes', () => {
  const first = visualObservation('observation-1', [visualTarget('inferred'), visualTarget('sensitive'), finalTarget()]);
  let ledger = createLedger(first);
  const metadata = {
    inference_rationale_digest: 'b'.repeat(64),
    inference_evidence_digests: {
      resume_sha256: 'c'.repeat(64),
      job_description_sha256: 'd'.repeat(64),
    },
  };
  assert.throws(() => answered(ledger, 'inferred', 'observation-1', 'inferred', 'agent_inference'), /inference metadata required/i);
  ledger = answered(ledger, 'inferred', 'observation-1', 'inferred', 'agent_inference', metadata);
  assert.equal(field(ledger, 'inferred').answer_source, 'agent_inference');
  ledger = markTargetSensitive(ledger, 'sensitive');
  assert.throws(() => answered(ledger, 'sensitive', 'observation-1', 'sensitive', 'agent_inference', metadata), /prohibited for sensitive/i);

  ledger = markTargetSensitive(ledger, 'inferred');
  assert.equal(field(ledger, 'inferred').sensitive, true);
  assert.equal(field(ledger, 'inferred').answer_state, 'unresolved');
  assert.equal(field(ledger, 'inferred').value_digest, null);
  assert.equal(field(ledger, 'inferred').inference_evidence_digests, null);
});

test('retains a selected radio group while preserving invalid visual states', () => {
  const radioA = visualTarget('radio-a', {
    kind: 'radio',
    group_id: 'work-mode',
    value_state: 'blank',
    checked: false,
  });
  const radioB = visualTarget('radio-b', {
    kind: 'radio',
    group_id: 'work-mode',
    value_state: 'selected',
    checked: true,
    label: 'On-site',
  });
  const first = visualObservation('observation-1', [radioA, radioB, finalTarget()]);
  let ledger = deliberateBlank(createLedger(first), 'radio-b', 'observation-1', 'radio-b');
  let retained = verifyRetention(ledger, first);
  assert.equal(retained.ok, true);
  ledger = retained.ledger;
  assert.equal(auditCompletion(ledger, first).complete, true);

  const invalid = visualObservation('observation-2', [
    visualTarget('radio-a', { kind: 'radio', group_id: 'work-mode', value_state: 'blank', checked: false }),
    visualTarget('radio-b', {
      kind: 'radio',
      group_id: 'work-mode',
      value_state: 'selected',
      checked: true,
      validation: { valid: false, message_present: true },
    }),
    finalTarget(),
  ], 'observation-1');
  ledger = mergeObservation(ledger, invalid);
  retained = verifyRetention(ledger, invalid);
  assert.equal(retained.ok, false);
  const audit = auditCompletion(retained.ledger, invalid);
  assert.equal(audit.complete, false);
  assert.ok(audit.invalid_field_ids.includes('radio-b'));
});

test('keeps dynamic targets bounded and blocks hidden or unknown actions', () => {
  const current = visualObservation('observation-1', [
    visualTarget('alpha'),
    visualTarget('hidden', { visible: false }),
    visualTarget('disabled', { enabled: false }),
    visualTarget('honeypot', { candidate: { class: 'field', reason: 'honeypot trap field' } }),
    visualTarget('unknown-action', {
      field_id: null,
      kind: 'button',
      required: false,
      value_state: 'blank',
      candidate: { class: 'unknown', reason: 'unclassified control' },
    }),
    finalTarget(),
  ]);
  const ledger = createLedger(current);
  assert.deepEqual(ledger.unknown_targets, [{
    target_id: 'unknown-action',
    observation_id: 'observation-1',
    reason: 'unclassified control',
  }]);
  assert.equal(ledger.current_candidate_targets.length, 1);
  const audit = auditCompletion(ledger, current);
  assert.equal(audit.complete, false);
  assert.ok(audit.blockers.some((item) => item.code === 'unknown-target'));
  assert.equal(audit.blockers.some((item) => item.target_id === 'honeypot'), false);

  assert.throws(() => validateObservation({
    ...current,
    targets: [visualTarget('alpha', { confidence: Number.NaN }), finalTarget()],
  }), /confidence/);
});
