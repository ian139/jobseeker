import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';

import {
  ANSWER_SOURCES,
  DECISION_MODES,
  FIELD_POLICIES,
  MODEL_TIERS,
  PROPOSED_ACTIONS,
  validateApplicationDecision,
} from '../src/phase1/decision.mjs';

const DIGEST = 'a'.repeat(64);

function decision(overrides = {}) {
  return {
    observationId: 'observation-1',
    fieldId: 'field-1',
    targetId: 'target-1',
    fieldPolicy: 'qualification',
    proposedAnswer: 'Software engineer',
    answerSource: 'resume_evidence',
    decisionMode: 'supported_inference',
    evidenceReferences: ['resume:sha256:' + DIGEST],
    observationScreenshotSha256: DIGEST,
    provider: 'codex',
    proposedAction: 'type_text',
    expectedRetainedState: { value_state: 'present', valid: true },
    modelTier: 'strong',
    confidence: 0.92,
    reasonCode: 'resume_supported',
    inferenceRationaleDigest: DIGEST,
    reobservationRequired: true,
    automaticSubmissionEligible: false,
    ...overrides,
  };
}

function context(overrides = {}) {
  return {
    observationId: 'observation-1',
    targetId: 'target-1',
    fieldId: 'field-1',
    observationScreenshotSha256: DIGEST,
    currentTarget: { targetId: 'target-1', fieldId: 'field-1', kind: 'text', visible: true, enabled: true },
    ...overrides,
  };
}

test('schema vocabularies match frozen visual runtime enums', async () => {
  const schema = JSON.parse(await readFile(new URL('../schemas/application-decision.schema.json', import.meta.url), 'utf8'));
  assert.deepEqual(schema.properties.fieldPolicy.enum, FIELD_POLICIES);
  assert.deepEqual(schema.properties.answerSource.enum, ANSWER_SOURCES);
  assert.deepEqual(schema.properties.decisionMode.enum, DECISION_MODES);
  assert.deepEqual(schema.properties.proposedAction.enum, PROPOSED_ACTIONS);
  assert.deepEqual(schema.properties.modelTier.enum, MODEL_TIERS);
  assert.deepEqual(schema.properties.provider.enum, ['codex', 'gemini']);
  assert.equal(schema.properties.targetId.oneOf.at(-1).type, 'null');
  assert.equal(Object.isFrozen(FIELD_POLICIES), true);
  assert.equal(Object.isFrozen(DECISION_MODES), true);
  assert.equal(Object.isFrozen(PROPOSED_ACTIONS), true);
});

test('valid screenshot-backed decision passes without mutating input', () => {
  const input = decision();
  const output = validateApplicationDecision(input, context());
  assert.deepEqual(output, input);
  assert.notEqual(output, input);
  assert.equal(output.observationScreenshotSha256, DIGEST);
  assert.equal(output.provider, 'codex');
});

test('targetless scrolling and key presses are allowed but targetless typing is rejected', () => {
  const scroll = validateApplicationDecision(decision({
    fieldId: null,
    targetId: null,
    proposedAnswer: { deltaY: 480 },
    proposedAction: 'scroll',
    answerSource: 'require_user',
    decisionMode: 'require_user',
    evidenceReferences: [],
    inferenceRationaleDigest: null,
  }));
  assert.equal(scroll.targetId, null);
  const press = validateApplicationDecision(decision({
    fieldId: null,
    targetId: null,
    proposedAnswer: 'TAB',
    proposedAction: 'press_key',
    answerSource: 'require_user',
    decisionMode: 'require_user',
    evidenceReferences: [],
    inferenceRationaleDigest: null,
  }));
  assert.equal(press.proposedAction, 'press_key');
  assert.throws(() => validateApplicationDecision(decision({ targetId: null })), (error) => error.code === 'E_DECISION_TARGET_REQUIRED');
});

test('inference restrictions remain conservative', () => {
  assert.throws(
    () => validateApplicationDecision(decision({ fieldPolicy: 'legal' })),
    (error) => error.code === 'E_DECISION_INFERENCE_POLICY',
  );
  assert.throws(
    () => validateApplicationDecision(decision({ answerSource: 'best_effort_inference', decisionMode: 'best_effort_inference', modelTier: 'strong' })),
    (error) => error.code === 'E_DECISION_BEST_EFFORT_TIER',
  );
});

test('automatic final submission requires current visual audit context', () => {
  const final = decision({
    fieldId: null,
    targetId: 'target-submit',
    proposedAnswer: null,
    proposedAction: 'click',
    answerSource: 'require_user',
    decisionMode: 'require_user',
    evidenceReferences: [],
    inferenceRationaleDigest: null,
    reobservationRequired: false,
    automaticSubmissionEligible: true,
  });
  const finalContext = context({
    fieldId: null,
    targetId: 'target-submit',
    currentTarget: { targetId: 'target-submit', fieldId: null, kind: 'final_candidate', visible: true, enabled: true },
    submissionEligible: true,
  });
  assert.throws(() => validateApplicationDecision(final, finalContext), (error) => error.code === 'E_DECISION_VISUAL_AUDIT');
  const accepted = validateApplicationDecision(final, {
    ...finalContext,
    visualAudit: { observationId: 'observation-1', screenshotSha256: DIGEST, finalTargetIds: ['target-submit'] },
  });
  assert.equal(accepted.automaticSubmissionEligible, true);
});
