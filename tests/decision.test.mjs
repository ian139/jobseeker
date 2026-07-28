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
    controlReference: 'observation-1:control-1',
    fieldPolicy: 'qualification',
    proposedAnswer: 'Software engineer',
    answerSource: 'resume_evidence',
    decisionMode: 'supported_inference',
    evidenceReferences: ['resume:sha256:' + DIGEST],
    inferenceRationaleDigest: DIGEST,
    proposedAction: 'fill_text',
    expectedRetainedState: 'Software engineer',
    modelTier: 'strong',
    confidence: 0.92,
    reasonCode: 'resume_match',
    reobservationRequired: true,
    automaticSubmissionEligible: false,
    ...overrides,
  };
}

function context(overrides = {}) {
  return {
    currentObservationId: 'observation-1',
    currentFieldId: 'field-1',
    currentControlReference: 'observation-1:control-1',
    currentField: { retained: false, valid: false, value: null },
    currentControl: { kind: 'input', type: 'text', visible: true, enabled: true },
    allowedSources: ['resume_evidence', 'supported_inference'],
    allowedModes: ['supported_inference'],
    allowedActions: ['fill_text'],
    ...overrides,
  };
}

test('schema vocabularies are represented by frozen runtime enums', async () => {
  const schema = JSON.parse(await readFile(new URL('../schemas/application-decision.schema.json', import.meta.url), 'utf8'));
  assert.deepEqual(schema.properties.fieldPolicy.enum, FIELD_POLICIES);
  assert.deepEqual(schema.properties.answerSource.enum, ANSWER_SOURCES);
  assert.deepEqual(schema.properties.decisionMode.enum, DECISION_MODES);
  assert.deepEqual(schema.properties.proposedAction.enum, PROPOSED_ACTIONS);
  assert.deepEqual(schema.properties.modelTier.enum, MODEL_TIERS);
  assert.equal(Object.isFrozen(FIELD_POLICIES), true);
  assert.equal(Object.isFrozen(DECISION_MODES), true);
  assert.equal(Object.isFrozen(PROPOSED_ACTIONS), true);
  assert.equal(Object.isFrozen(MODEL_TIERS), true);
});

test('valid evidence-backed decision passes and is not mutated', () => {
  const input = decision();
  const output = validateApplicationDecision(input, context());
  assert.deepEqual(output, input);
  assert.notEqual(output, input);
});

test('legacy source names normalize to canonical source semantics', () => {
  const input = decision({
    answerSource: 'resume',
    decisionMode: 'supported_inference',
  });
  assert.equal(validateApplicationDecision(input).answerSource, 'resume_evidence');
});

test('exact memory can answer sensitive policy without inference metadata', () => {
  const output = validateApplicationDecision(decision({
    fieldPolicy: 'identity',
    proposedAnswer: 'Applicant',
    answerSource: 'exact_memory',
    decisionMode: 'exact_memory',
    evidenceReferences: ['memory:answer-1'],
    inferenceRationaleDigest: null,
    modelTier: 'cheap',
    reobservationRequired: false,
  }));
  assert.equal(output.decisionMode, 'exact_memory');
});

test('forbidden decisions fail closed', () => {
  assert.throws(
    () => validateApplicationDecision({ ...decision(), unexpected: true }),
    (error) => error.code === 'E_DECISION_UNKNOWN_KEY',
  );
  assert.throws(
    () => validateApplicationDecision(decision({ observationId: 'old-observation' }), context()),
    (error) => error.code === 'E_DECISION_STALE_CONTEXT',
  );
  assert.throws(
    () => validateApplicationDecision(decision({ fieldPolicy: 'legal' })),
    (error) => error.code === 'E_DECISION_INFERENCE_POLICY',
  );
  assert.throws(
    () => validateApplicationDecision(decision({
      answerSource: 'best_effort_inference',
      decisionMode: 'best_effort_inference',
      modelTier: 'strong',
    })),
    (error) => error.code === 'E_DECISION_BEST_EFFORT_TIER',
  );
  assert.throws(
    () => validateApplicationDecision(decision({ inferenceRationaleDigest: null })),
    (error) => error.code === 'E_DECISION_RATIONALE_REQUIRED',
  );
});

test('highest tier is mandatory for best-effort inference', () => {
  const output = validateApplicationDecision(decision({
    answerSource: 'best_effort_inference',
    decisionMode: 'best_effort_inference',
    modelTier: 'highest',
  }));
  assert.equal(output.modelTier, 'highest');
});

test('select options require current option membership and compatible controls', () => {
  const select = decision({
    proposedAnswer: 'Remote',
    answerSource: 'profile_evidence',
    decisionMode: 'profile_evidence',
    evidenceReferences: ['profile:answer-1'],
    inferenceRationaleDigest: null,
    proposedAction: 'select_option',
    expectedRetainedState: 'Remote',
    reobservationRequired: true,
  });
  assert.equal(validateApplicationDecision(select, context({
    allowedSources: ['profile_evidence'],
    allowedModes: ['profile_evidence'],
    allowedActions: ['select_option'],
    currentControl: {
      kind: 'select',
      visible: true,
      enabled: true,
      options: [{ value: 'Remote', disabled: false }],
    },
  })).proposedAnswer, 'Remote');
  assert.throws(
    () => validateApplicationDecision(select, context({
      allowedSources: ['profile_evidence'],
      allowedModes: ['profile_evidence'],
      allowedActions: ['select_option'],
      currentControl: {
        kind: 'select', visible: true, enabled: true, options: [{ value: 'On-site', disabled: false }],
      },
    })),
    (error) => error.code === 'E_DECISION_OPTION_MEMBERSHIP',
  );
});

test('duplicate retained states and ineligible submission are rejected', () => {
  assert.throws(
    () => validateApplicationDecision(decision({
      expectedRetainedState: 'Already retained',
    }), context({
      currentField: { retained: true, valid: true, value: 'Already retained' },
    })),
    (error) => error.code === 'E_DECISION_DUPLICATE_RETAINED_STATE',
  );
  assert.throws(
    () => validateApplicationDecision(decision({
      proposedAction: 'click',
      fieldId: 'field-1',
      controlReference: 'observation-1:control-1',
      automaticSubmissionEligible: true,
      reobservationRequired: false,
      answerSource: 'exact_memory',
      decisionMode: 'exact_memory',
      evidenceReferences: ['memory:submit'],
      inferenceRationaleDigest: null,
      proposedAnswer: null,
      expectedRetainedState: null,
    }), context({
      currentControl: { kind: 'button', type: 'submit', visible: true, enabled: true },
      allowedSources: ['exact_memory'],
      allowedModes: ['exact_memory'],
      allowedActions: ['click'],
    })),
    (error) => error.code === 'E_DECISION_SUBMISSION_INELIGIBLE',
  );
});
