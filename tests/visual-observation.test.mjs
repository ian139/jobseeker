import assert from 'node:assert/strict';
import test from 'node:test';

import {
  createVisualObservation,
  immutableObservation,
  screenshotDigest,
  validateObservation,
  VISUAL_OBSERVATION_SCHEMA,
} from '../src/phase1/visual-observation.mjs';

const DIGEST = 'a'.repeat(64);

function target(targetId, overrides = {}) {
  return {
    target_id: targetId,
    field_id: targetId,
    group_id: null,
    kind: 'text',
    label: `Question ${targetId}`,
    description: null,
    bounds: { x: 16, y: 24, width: 320, height: 44 },
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
    confidence: 0.98,
    ...overrides,
  };
}

function finalTarget(targetId = 'final-submit') {
  return target(targetId, {
    field_id: null,
    kind: 'button',
    label: 'Submit application',
    required: false,
    value_state: 'blank',
    candidate: { class: 'final_candidate', reason: 'current final action' },
  });
}

function observation(observationId, targets, previousObservationId = null, blockers = []) {
  return {
    schema: VISUAL_OBSERVATION_SCHEMA,
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

test('accepts a bounded visual observation and freezes a private copy', () => {
  const input = observation('observation-1', [target('name'), finalTarget()]);
  const accepted = createVisualObservation(input);

  assert.equal(validateObservation(accepted), true);
  assert.equal(accepted.schema, 'phase1-visual-observation-v1');
  assert.notEqual(accepted, input);
  assert.equal(Object.isFrozen(accepted), true);
  assert.equal(Object.isFrozen(accepted.surface.viewport), true);
  assert.equal(Object.isFrozen(accepted.targets[0].bounds), true);
  input.targets[0].label = 'changed outside observation';
  assert.equal(accepted.targets[0].label, 'Question name');

  const detached = immutableObservation(input);
  assert.notEqual(detached, input);
  assert.equal(Object.isFrozen(detached.targets[0]), true);
});

test('rejects unknown keys, wrong schema, and malformed nested records', () => {
  const current = observation('observation-1', [target('name'), finalTarget()]);

  assert.throws(() => validateObservation({ ...current, extra: true }), /observation\.extra: unknown key/);
  assert.throws(() => validateObservation({ ...current, schema: 'phase1-observation-v1' }), /unexpected schema/);
  assert.throws(() => validateObservation({
    ...current,
    surface: { ...current.surface, viewport: { ...current.surface.viewport, depth: 1 } },
  }), /viewport\.depth: unknown key/);
  assert.throws(() => validateObservation({
    ...current,
    targets: [target('name', { candidate: { class: 'field', reason: null, extra: true } }), finalTarget()],
  }), /candidate\.extra: unknown key/);
});

test('enforces screenshot viewport bounds and confidence limits', () => {
  const current = observation('observation-1', [target('name'), finalTarget()]);

  assert.throws(() => validateObservation({
    ...current,
    targets: [target('name', { bounds: { x: 1000, y: 24, width: 320, height: 44 } }), finalTarget()],
  }), /bounds exceed/);
  assert.throws(() => validateObservation({
    ...current,
    targets: [target('name', { bounds: { x: 0, y: 0, width: 0, height: 44 } }), finalTarget()],
  }), /expected a bounded integer/);
  assert.throws(() => validateObservation({
    ...current,
    targets: [target('name', { confidence: 1.1 }), finalTarget()],
  }), /confidence.*number from 0 through 1/);
  assert.throws(() => validateObservation({
    ...current,
    surface: { ...current.surface, viewport: { width: 0, height: 720 } },
  }), /viewport\.width.*bounded integer/);
});

test('keeps candidate identities unique and classifies blockers without hidden data', () => {
  const current = observation('observation-1', [
    target('name'),
    target('unknown-action', {
      field_id: null,
      kind: 'button',
      label: 'Unclassified action',
      required: false,
      value_state: 'blank',
      candidate: { class: 'unknown', reason: 'needs visual review' },
    }),
    finalTarget(),
  ], null, ['captcha', { code: 'access-control', message: 'Visible gate', visible: true }]);

  assert.equal(validateObservation(current), true);
  assert.deepEqual(current.blockers, ['captcha', { code: 'access-control', message: 'Visible gate', visible: true }]);
  assert.throws(() => validateObservation({
    ...current,
    targets: [target('name'), target('name'), finalTarget()],
  }), /duplicate target identity/);
  assert.throws(() => validateObservation({
    ...current,
    targets: [target('name', { target_id: 'name-new' }), target('other', { field_id: 'name' }), finalTarget()],
  }), /duplicate field identity/);
});

test('validates file names, options, selected values, and supported model providers', () => {
  const upload = target('resume', {
    kind: 'file_upload',
    label: 'Resume upload',
    value_state: 'blank',
    file: { present: false, file_name: null },
  });
  const select = target('work-mode', {
    kind: 'select',
    required: false,
    value_state: 'selected',
    selected: ['remote'],
    options: [{ label: 'Remote', selected: true, disabled: false }],
  });
  const current = observation('observation-1', [upload, select, finalTarget()]);
  assert.equal(validateObservation(current), true);

  assert.throws(() => validateObservation({
    ...current,
    targets: [target('resume', {
      kind: 'file_upload',
      value_state: 'blank',
      file: { present: false, file_name: '../resume.pdf' },
    }), select, finalTarget()],
  }), /safe file basename/);
  assert.throws(() => validateObservation({
    ...current,
    agent: { provider: 'other', model: 'vision-test' },
  }), /unsupported model provider/);
  assert.throws(() => validateObservation({
    ...current,
    targets: [target('work-mode', { selected: [1] }), upload, finalTarget()],
  }), /boolean, string array, or null/);
});

test('computes SHA-256 digests for visual bytes', () => {
  assert.equal(screenshotDigest(Buffer.from('visual bytes')), '65c8a3c3f8b241dc275bb1bfeafcb4358394afbfe3af71dd31f9f1fc863e277e');
  assert.match(screenshotDigest('visual bytes'), /^[a-f0-9]{64}$/);
});
