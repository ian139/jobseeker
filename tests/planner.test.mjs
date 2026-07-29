import assert from 'node:assert/strict';
import test from 'node:test';

import {
  classifyFieldPolicy,
  planSafeVisualBatch,
  planVisualApplicationWork,
  PlannerError,
} from '../src/phase1/planner.mjs';

const DIGEST = 'a'.repeat(64);

function target(id, overrides = {}) {
  return {
    target_id: `target-${id}`,
    field_id: id,
    group_id: null,
    kind: 'text',
    label: id,
    description: null,
    bounds: { x: 1, y: 1, width: 100, height: 24 },
    visible: true,
    enabled: true,
    required: false,
    readonly: false,
    value_state: 'blank',
    checked: null,
    selected: null,
    options: [],
    validation: { valid: true, message_present: false },
    file: null,
    candidate: { class: 'field', reason: null },
    confidence: 0.99,
    ...overrides,
  };
}

function observation(targets, id = 'observation-1') {
  return {
    schema: 'phase1-visual-observation-v1',
    observation_id: id,
    previous_observation_id: null,
    observed_at: '2026-07-28T00:00:00.000Z',
    surface: {
      surface_id: 'surface-1',
      url: 'https://example.invalid/apply',
      title: 'Application',
      screenshot_sha256: DIGEST,
      viewport: { width: 1280, height: 720 },
    },
    agent: { provider: 'codex', model: 'vision-model' },
    targets,
    blockers: [],
  };
}

function ledger(targets = [], observationId = 'observation-1', overrides = {}) {
  return {
    latest_observation_id: observationId,
    targets,
    current_candidate_targets: [],
    action_attempts: [],
    ...overrides,
  };
}

function input(targets, fields = [], options = {}, observationId = 'observation-1') {
  return {
    observation: observation(targets, observationId),
    ledger: ledger(fields, observationId),
    ...options,
  };
}

test('classifies visual field policies conservatively', () => {
  assert.equal(classifyFieldPolicy(target('a', { label: 'Are you authorized to work?' })), 'legal');
  assert.equal(classifyFieldPolicy(target('b', { label: 'Gender identity' })), 'demographic');
  assert.equal(classifyFieldPolicy(target('c', { label: 'Email address' })), 'identity');
  assert.equal(classifyFieldPolicy(target('d', { label: 'Why this role?' })), 'subjective');
  assert.equal(classifyFieldPolicy(target('e', { label: 'Years of experience' })), 'qualification');
});

test('batches only independent ordinary text targets in deterministic order', () => {
  const targets = [
    target('c', { required: true, label: 'Technical skills' }),
    target('a', { required: true, label: 'Years of experience' }),
    target('b', { required: true, label: 'Programming languages' }),
    target('upload', { required: true, kind: 'file_upload', file: { present: false, file_name: null } }),
  ];
  const result = planSafeVisualBatch(input(targets, targets.map(({ field_id, target_id }) => ({ field_id, target_id, required: true }))));
  assert.equal(result.mode, 'batch');
  assert.deepEqual(result.units.map((unit) => unit.fieldId), ['a', 'b', 'c']);
  assert.deepEqual(result.units.map((unit) => unit.targetId), ['target-a', 'target-b', 'target-c']);
  assert.ok(result.units.every((unit) => unit.allowedActions[0] === 'type_text'));
});

test('prioritizes rejected work and never repeats a current successful target action', () => {
  const rejected = target('bad', { required: true, value_state: 'present', validation: { valid: false, message_present: true } });
  const ordinary = target('ordinary', { required: true });
  const result = planVisualApplicationWork(input([ordinary, rejected], [
    { field_id: 'bad', target_id: 'target-bad', retained: true, valid: false },
    { field_id: 'ordinary', target_id: 'target-ordinary', retained: false, valid: false },
  ], {
    ledger: ledger([], 'observation-1', {
      action_attempts: [{ observation_id: 'observation-1', target_id: 'target-ordinary', outcome: 'succeeded' }],
    }),
  }));
  assert.equal(result.targetId, 'target-bad');
});

test('requires a fresh visual audit before proposing a final target', () => {
  const final = target('submit', {
    field_id: null,
    kind: 'button',
    candidate: { class: 'final_candidate', reason: 'final submission' },
  });
  const base = input([final], [], { submissionReady: true });
  assert.equal(planVisualApplicationWork(base), null);
  const audited = planVisualApplicationWork({
    ...base,
    visualAudit: {
      observation_id: 'observation-1',
      screenshot_sha256: DIGEST,
      final_candidate_target_ids: ['target-submit'],
    },
  });
  assert.equal(audited.targetId, 'target-submit');
  assert.deepEqual(audited.allowedActions, ['click']);
});

test('rejects stale ledger bindings and returns immutable plans', () => {
  const staleInput = {
    observation: observation([target('a')], 'observation-1'),
    ledger: ledger([], 'observation-2'),
  };
  assert.throws(
    () => planVisualApplicationWork(staleInput),
    (error) => error instanceof PlannerError && error.code === 'E_PLANNER_STALE',
  );
  const plan = planVisualApplicationWork(input([target('a')], [{ field_id: 'a', target_id: 'target-a' }]));
  assert.equal(Object.isFrozen(plan), true);
  assert.equal(Object.isFrozen(plan.allowedActions), true);
});
