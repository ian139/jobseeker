import assert from 'node:assert/strict';
import test from 'node:test';

import {
  classifyFailure,
  planRecovery,
  reconcileSubmission,
  canIssueFinalSubmit,
  validateRetryBudget,
} from '../src/phase1/recovery.mjs';
import { validateObservation } from '../src/phase1/visual-observation.mjs';

test('CAPTCHA remains automatic and recoverable', () => {
  const classification = classifyFailure({ code: 'captcha' });
  assert.equal(classification.failureClass, 'captcha');
  assert.equal(classification.terminal, false);
  assert.equal(classification.requiresUser, false);
  const plan = planRecovery({ code: 'captcha' });
  assert.equal(plan.retryAllowed, true);
  assert.equal(plan.step, 'reobserve');
  assert.equal(plan.strategy, 'captcha_resolution');
  assert.deepEqual(plan.steps.slice(0, 2), ['reobserve', 'retry_changed_strategy']);
});

test('retry budgets interpret maxAttempts as total attempts', () => {
  const budget = validateRetryBudget({ maxAttempts: 4 });
  assert.equal(budget.maxRetries, 3);
  assert.equal(budget.maxAttempts, 4);
  assert.throws(
    () => validateRetryBudget({ maxAttempts: 0 }),
    (error) => error.code === 'INVALID_RETRY_BUDGET',
  );
});

test('uncertain submission forbids another final submit until reconciliation', () => {
  const reconciliation = reconcileSubmission({ outcome: 'uncertain' });
  assert.equal(reconciliation.uncertain, true);
  assert.equal(canIssueFinalSubmit(reconciliation), false);
});

function captchaObservation() {
  return {
    schema: 'phase1-visual-observation-v1',
    observation_id: 'obs-2',
    previous_observation_id: 'obs-1',
    observed_at: '2026-07-26T00:00:05.000Z',
    surface: {
      surface_id: 'surface-1',
      url: 'https://example.invalid/confirmation',
      title: 'captcha required',
      screenshot_sha256: 'a'.repeat(64),
      viewport: { width: 1200, height: 800 },
    },
    agent: { provider: 'codex', model: 'fixture-model' },
    targets: [{
      target_id: 'final-target',
      field_id: null,
      group_id: null,
      kind: 'button',
      label: 'Submit application',
      description: null,
      bounds: { x: 480, y: 640, width: 240, height: 48 },
      visible: true,
      enabled: true,
      required: false,
      readonly: false,
      value_state: 'unknown',
      checked: null,
      selected: null,
      options: [],
      validation: { valid: true, message_present: false },
      file: null,
      candidate: { class: 'final_candidate', reason: 'fixture' },
      confidence: 0.99,
    }],
    blockers: [{
      code: 'captcha_required',
      message: 'CAPTCHA challenge required',
      visible: true,
    }],
  };
}
test('post-submit CAPTCHA remains recoverable instead of becoming access control', () => {
  const observation = captchaObservation();
  assert.equal(validateObservation(observation), true);
  const reconciliation = reconcileSubmission({
    action: 'final_submit',
    attemptId: 'attempt-1',
    observation,
  });
  assert.equal(reconciliation.status, 'failed');
  assert.equal(reconciliation.failureClass, 'captcha');
  assert.equal(reconciliation.terminal, false);
  assert.equal(reconciliation.retryAllowed, true);
  assert.equal(reconciliation.finalTargetId, 'final-target');
  assert.equal(reconciliation.finalSubmitAllowed, false);
});

test('CAPTCHA signal takes precedence over an HTTP access status', () => {
  const classification = classifyFailure({
    finalSubmit: true,
    status: 403,
    message: 'captcha challenge required',
  });
  assert.equal(classification.failureClass, 'captcha');
  assert.equal(classification.terminal, false);
  assert.equal(classification.requiresAccessControl, false);
});
