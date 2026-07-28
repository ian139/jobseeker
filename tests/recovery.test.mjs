import assert from 'node:assert/strict';
import test from 'node:test';

import {
  classifyFailure,
  planRecovery,
  reconcileSubmission,
  canIssueFinalSubmit,
  validateRetryBudget,
} from '../src/phase1/recovery.mjs';

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

test('post-submit CAPTCHA remains recoverable instead of becoming access control', () => {
  const reconciliation = reconcileSubmission({
    action: 'final_submit',
    attemptId: 'attempt-1',
    observation: {
      observation_id: 'obs-2',
      status: 'captcha_required',
    },
  });
  assert.equal(reconciliation.status, 'failed');
  assert.equal(reconciliation.failureClass, 'captcha');
  assert.equal(reconciliation.terminal, false);
  assert.equal(reconciliation.retryAllowed, true);
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
