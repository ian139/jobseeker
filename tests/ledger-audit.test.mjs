import assert from 'node:assert/strict';
import test from 'node:test';

import {
  ANSWER_SOURCES,
  answerSourceIsAllowed,
  createLedger,
  digestPrivateValue,
  diffObservations,
  mergeObservation,
  recordActionAttempt,
  recordResolution,
  requiresReobservation,
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

function finalTarget(targetId = 'final-submit', overrides = {}) {
  return visualTarget(targetId, {
    field_id: null,
    kind: 'button',
    label: 'Submit application',
    required: false,
    value_state: 'blank',
    candidate: { class: 'final_candidate', reason: 'current final action' },
    ...overrides,
  });
}

function navigationTarget(targetId = 'continue-action') {
  return visualTarget(targetId, {
    field_id: null,
    kind: 'button',
    label: 'Continue',
    required: false,
    value_state: 'blank',
    candidate: { class: 'non_final_navigation', reason: 'next application page' },
  });
}

function unknownTarget(targetId = 'unknown-action') {
  return visualTarget(targetId, {
    field_id: null,
    kind: 'button',
    label: 'Unclassified action',
    required: false,
    value_state: 'blank',
    candidate: { class: 'unknown', reason: 'needs visual review' },
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

function answer(ledger, fieldId, observationId, targetId, value, source = 'user', extra = {}) {
  return recordResolution(ledger, {
    field_id: fieldId,
    observation_id: observationId,
    target_id: targetId,
    source,
    value_digest: digestPrivateValue(value),
    ...extra,
  });
}

function successfulMutation(ledger, fieldId, targetId, observationId, action = 'type_text', actionId = `${action}-${fieldId}`) {
  return recordActionAttempt(ledger, {
    action_id: actionId,
    action,
    field_id: fieldId,
    target_id: targetId,
    observation_id: observationId,
    outcome: 'succeeded',
  });
}

test('merges bounded visual observations and emits v2 ledger diffs', () => {
  const first = visualObservation('observation-1', [visualTarget('alpha'), visualTarget('beta'), finalTarget()]);
  validateObservation(first);
  let ledger = createLedger(first);
  validateLedger(ledger);

  assert.equal(ledger.schema, 'phase1-ledger-v2');
  assert.deepEqual(ledger.observation_ids, ['observation-1']);
  assert.deepEqual(ledger.targets.map((item) => item.field_id), ['alpha', 'beta']);
  assert.deepEqual(ledger.current_candidate_targets, [{
    target_id: 'final-submit',
    observation_id: 'observation-1',
    class: 'final_candidate',
  }]);
  assert.equal(ledger.diffs[0].schema, 'phase1-visual-diff-v1');
  assert.deepEqual(ledger.diffs[0].added, [
    { field_id: 'alpha', target_id: 'alpha', kind: 'text' },
    { field_id: 'beta', target_id: 'beta', kind: 'text' },
  ]);

  const second = visualObservation('observation-2', [
    visualTarget('alpha-new', { field_id: 'alpha', label: 'Changed question' }),
    visualTarget('gamma'),
    navigationTarget(),
    finalTarget(),
  ], 'observation-1', [{ code: 'captcha', message: 'Visible challenge', visible: true }]);
  ledger = mergeObservation(ledger, second);
  validateLedger(ledger);

  assert.equal(ledger.latest_observation_id, 'observation-2');
  assert.equal(field(ledger, 'beta').present_in_latest_observation, false);
  assert.deepEqual(field(ledger, 'alpha').target_history, [
    { observation_id: 'observation-1', target_id: 'alpha' },
    { observation_id: 'observation-2', target_id: 'alpha-new' },
  ]);
  assert.ok(ledger.diffs[1].changed.some((item) => item.field_id === 'alpha'));
  assert.deepEqual(ledger.diffs[1].removed, [{ field_id: 'beta', target_id: 'beta', kind: 'text' }]);
  assert.deepEqual(ledger.active_blockers, ['captcha']);
  assert.deepEqual(ledger.current_candidate_targets.map((item) => item.class), [
    'non_final_navigation',
    'final_candidate',
  ]);
  assert.equal(JSON.stringify(ledger).includes('private answer'), false);
});

test('rejects stale observation chains and stale target identities', () => {
  const first = visualObservation('observation-1', [visualTarget('alpha'), finalTarget()]);
  const ledger = createLedger(first);
  const wrongChain = visualObservation('observation-2', [visualTarget('alpha'), finalTarget()], 'other-observation');

  assert.throws(() => mergeObservation(ledger, wrongChain), /next image|stale/i);
  assert.throws(() => diffObservations(first, wrongChain), /chain|stale/i);
  assert.throws(() => mergeObservation(ledger, first), /already merged|stale/i);

  const changed = visualObservation('observation-2', [
    visualTarget('alpha-new', { field_id: 'alpha' }),
    finalTarget(),
  ], 'observation-1');
  const next = mergeObservation(ledger, changed);
  assert.throws(() => recordResolution(next, {
    field_id: 'alpha',
    observation_id: 'observation-2',
    target_id: 'alpha',
    source: 'user',
    value_digest: DIGEST,
  }), /stale/i);
  assert.throws(() => successfulMutation(next, 'alpha', 'alpha', 'observation-2'), /stale/i);
});

test('records answer sources, private digests, and inference evidence digests', () => {
  const fields = ANSWER_SOURCES.map((source) => visualTarget(`field-${source}`));
  let ledger = createLedger(visualObservation('observation-1', [...fields, finalTarget()]));
  const inference = {
    inference_rationale_digest: 'b'.repeat(64),
    inference_evidence_digests: {
      resume_sha256: 'c'.repeat(64),
      job_description_sha256: 'd'.repeat(64),
    },
  };

  for (const source of ANSWER_SOURCES) {
    assert.equal(answerSourceIsAllowed(source), true);
    ledger = answer(
      ledger,
      `field-${source}`,
      'observation-1',
      `field-${source}`,
      `private-${source}`,
      source,
      source === 'agent_inference' ? inference : {},
    );
  }
  validateLedger(ledger);
  assert.deepEqual(ledger.targets.map((item) => item.answer_source), ANSWER_SOURCES);
  assert.equal(field(ledger, 'field-agent_inference').inference_rationale_digest, inference.inference_rationale_digest);
  assert.deepEqual(field(ledger, 'field-agent_inference').inference_evidence_digests, inference.inference_evidence_digests);
  assert.equal(JSON.stringify(ledger).includes('private-memory'), false);
  assert.equal(field(ledger, 'field-user').value_digest, digestPrivateValue('private-user'));
  assert.equal(answerSourceIsAllowed('job-description'), false);
  assert.throws(() => answer(ledger, 'field-user', 'observation-1', 'field-user', 'next', 'job-description'), /invalid answer source/i);
  assert.throws(() => answer(ledger, 'field-user', 'observation-1', 'field-user', 'next', 'agent_inference'), /inference metadata required/i);
});

test('requires a fresh visual observation after a target mutation', () => {
  const first = visualObservation('observation-1', [visualTarget('alpha'), finalTarget()]);
  let ledger = answer(createLedger(first), 'alpha', 'observation-1', 'alpha', 'answer');
  ledger = verifyRetention(ledger, first).ledger;
  ledger = successfulMutation(ledger, 'alpha', 'alpha', 'observation-1');

  assert.equal(requiresReobservation(ledger), true);
  assert.equal(field(ledger, 'alpha').retained, false);
  const pending = verifyRetention(ledger, first);
  assert.equal(pending.ok, false);
  assert.ok(pending.errors.some((item) => item.code === 'MUTATION_PENDING'));

  const second = visualObservation('observation-2', [visualTarget('alpha'), finalTarget()], 'observation-1');
  ledger = mergeObservation(ledger, second);
  const retained = verifyRetention(ledger, second, {
    alpha: { action_id: 'type_text-alpha', visually_confirmed: true },
  });
  assert.equal(retained.ok, true);
  assert.equal(field(retained.ledger, 'alpha').retained, true);
  assert.equal(field(retained.ledger, 'alpha').valid, true);
});

test('retains a file answer only with a successful upload and visual proof', () => {
  const emptyFile = visualTarget('resume', {
    kind: 'file_upload',
    label: 'Resume upload',
    value_state: 'blank',
    file: { present: false, file_name: null },
  });
  const first = visualObservation('observation-1', [emptyFile, finalTarget()]);
  let ledger = answer(createLedger(first), 'resume', 'observation-1', 'resume', 'resume-bytes', 'resume');
  ledger = successfulMutation(ledger, 'resume', 'resume', 'observation-1', 'upload_file', 'upload-resume');

  const uploaded = visualObservation('observation-2', [visualTarget('resume', {
    kind: 'file_upload',
    label: 'Resume upload',
    value_state: 'present',
    file: { present: true, file_name: 'resume.pdf' },
  }), finalTarget()], 'observation-1');
  ledger = mergeObservation(ledger, uploaded);
  let result = verifyRetention(ledger, uploaded);
  assert.equal(result.ok, false);
  assert.ok(result.errors.some((item) => item.code === 'INVALID_PROOF'));
  result = verifyRetention(ledger, uploaded, {
    resume: { action_id: 'upload-resume', visually_confirmed: true, file_name: 'resume.pdf' },
  });
  assert.equal(result.ok, true);
  assert.equal(field(result.ledger, 'resume').retained, true);

  const gone = visualObservation('observation-3', [finalTarget()], 'observation-2');
  const absent = mergeObservation(result.ledger, gone);
  assert.equal(field(absent, 'resume').present_in_latest_observation, false);
  const audit = auditCompletion(absent, gone);
  assert.equal(audit.complete, false);
  assert.ok(audit.blockers.some((item) => item.code === 'missing-file-target'));
});

test('audits completeness, blockers, unknown targets, and final boundaries', () => {
  const first = visualObservation('observation-1', [
    visualTarget('name', { value_state: 'blank' }),
    unknownTarget(),
    finalTarget(),
  ], null, ['captcha']);
  let ledger = createLedger(first);
  ledger = recordResolution(ledger, {
    field_id: 'name',
    observation_id: 'observation-1',
    target_id: 'name',
    source: 'user',
    value_digest: null,
    semantic_choice: 'none',
  });
  const retained = verifyRetention(ledger, first);
  assert.equal(retained.ok, true);
  ledger = retained.ledger;
  let audit = auditCompletion(ledger, first);
  assert.equal(audit.complete, false);
  assert.ok(audit.blockers.some((item) => item.code === 'observation-blocker:captcha'));
  assert.ok(audit.blockers.some((item) => item.code === 'unknown-target'));

  const withoutFinal = visualObservation('observation-2', [visualTarget('name')], 'observation-1');
  ledger = mergeObservation(ledger, withoutFinal);
  audit = auditCompletion(ledger, withoutFinal, { final_review_boundary: true });
  assert.equal(audit.final_review_boundary, true);
  assert.equal(audit.complete, true);
});
