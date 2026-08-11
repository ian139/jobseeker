import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';

import {
  ANSWER_SOURCES,
  FIELD_POLICIES,
  MODEL_TIERS,
  PROPOSED_ACTIONS,
  isApplicationDecision,
  validateApplicationDecision,
} from '../src/phase1/decision.mjs';

const DIGEST = 'a'.repeat(64);
const INFERENCE_EVIDENCE_DIGESTS = {
  resumeSha256: DIGEST,
  jobDescriptionSha256: DIGEST,
};

function decision(overrides = {}) {
  return {
    observationId: 'observation-1',
    fieldId: 'field-1',
    controlReference: 'observation-1:control-1',
    fieldPolicy: 'qualification',
    proposedAnswer: 'Software engineer',
    answerSource: 'resume',
    evidenceReferences: ['resume:sha256:' + DIGEST],
    inferenceRationaleDigest: null,
    inferenceEvidenceDigests: null,
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
    allowedSources: ['resume'],
    allowedActions: ['fill_text'],
    ...overrides,
  };
}

test('schema and runtime expose the same canonical vocabularies', async () => {
  const schema = JSON.parse(await readFile(new URL('../schemas/application-decision.schema.json', import.meta.url), 'utf8'));
  assert.deepEqual(schema.properties.fieldPolicy.enum, FIELD_POLICIES);
  assert.deepEqual(schema.properties.answerSource.enum, ANSWER_SOURCES);
  assert.deepEqual(schema.properties.proposedAction.enum, PROPOSED_ACTIONS);
  assert.deepEqual(schema.properties.modelTier.enum, MODEL_TIERS);
  assert.equal(Object.prototype.hasOwnProperty.call(schema.properties, 'decisionMode'), false);
  assert.deepEqual(Object.keys(schema.$defs.inferenceEvidenceDigests.properties), [
    'resumeSha256',
    'jobDescriptionSha256',
  ]);
  assert.deepEqual(ANSWER_SOURCES, ['memory', 'profile_verified', 'profile_user_attested', 'resume', 'agent_inference', 'user']);
  assert.equal(Object.isFrozen(ANSWER_SOURCES), true);
  assert.equal(Object.isFrozen(FIELD_POLICIES), true);
  assert.equal(Object.isFrozen(PROPOSED_ACTIONS), true);
  assert.equal(Object.isFrozen(MODEL_TIERS), true);
});

test('valid source-backed decision passes without mutation', () => {
  const input = decision();
  const output = validateApplicationDecision(input, context());
  assert.deepEqual(output, input);
  assert.notEqual(output, input);
});

test('all six canonical sources validate without a second source selector', () => {
  const sourceBacked = [
    ['memory', ['memory:answer-1']],
    ['profile_verified', ['profile_verified:answer-1']],
    ['profile_user_attested', ['profile_user_attested:answer-1']],
    ['resume', ['resume:answer-1']],
  ];
  for (const [answerSource, evidenceReferences] of sourceBacked) {
    const output = validateApplicationDecision(decision({ answerSource, evidenceReferences }));
    assert.equal(output.answerSource, answerSource);
    assert.equal(output.inferenceEvidenceDigests, null);
  }

  const inferred = validateApplicationDecision(decision({
    answerSource: 'agent_inference',
    evidenceReferences: ['resume:sha256:' + DIGEST, 'job:sha256:' + DIGEST],
    inferenceRationaleDigest: DIGEST,
    inferenceEvidenceDigests: INFERENCE_EVIDENCE_DIGESTS,
    modelTier: 'cheap',
  }));
  assert.equal(inferred.answerSource, 'agent_inference');
  assert.equal(inferred.modelTier, 'cheap');
  assert.deepEqual(inferred.inferenceEvidenceDigests, INFERENCE_EVIDENCE_DIGESTS);

  const user = validateApplicationDecision(decision({
    answerSource: 'user',
    evidenceReferences: [],
    inferenceRationaleDigest: null,
  }));
  assert.equal(user.answerSource, 'user');
  assert.equal(user.inferenceEvidenceDigests, null);
  const protectedResume = validateApplicationDecision(decision({
    fieldPolicy: 'hard_fact',
    answerSource: 'resume',
    evidenceReferences: ['resume:sha256:' + DIGEST],
  }));
  assert.equal(protectedResume.answerSource, 'resume');
});


test('inference requires both bound digests and is rejected for protected policies', () => {
  assert.throws(
    () => validateApplicationDecision(decision({
      answerSource: 'agent_inference',
      evidenceReferences: ['resume:sha256:' + DIGEST],
      inferenceRationaleDigest: null,
      inferenceEvidenceDigests: INFERENCE_EVIDENCE_DIGESTS,
    })),
    (error) => error.code === 'E_DECISION_RATIONALE_REQUIRED',
  );
  assert.throws(
    () => validateApplicationDecision(decision({
      answerSource: 'agent_inference',
      evidenceReferences: ['resume:sha256:' + DIGEST, 'job:sha256:' + DIGEST],
      inferenceRationaleDigest: DIGEST,
      inferenceEvidenceDigests: null,
    })),
    (error) => error.code === 'E_DECISION_INFERENCE_EVIDENCE_REQUIRED',
  );
  assert.throws(
    () => validateApplicationDecision(decision({
      answerSource: 'agent_inference',
      evidenceReferences: ['resume:sha256:' + DIGEST, 'job:sha256:' + DIGEST],
      inferenceRationaleDigest: DIGEST,
      inferenceEvidenceDigests: { resumeSha256: DIGEST },
    })),
    (error) => error.code === 'E_DECISION_REQUIRED',
  );
  assert.throws(
    () => validateApplicationDecision(decision({
      answerSource: 'agent_inference',
      evidenceReferences: ['resume:sha256:' + DIGEST, 'job:sha256:' + DIGEST],
      inferenceRationaleDigest: DIGEST,
      inferenceEvidenceDigests: {
        resumeSha256: DIGEST,
        jobDescriptionSha256: 'A'.repeat(64),
      },
    })),
    (error) => error.code === 'E_DECISION_INFERENCE_EVIDENCE',
  );
  assert.throws(
    () => validateApplicationDecision(decision({
      fieldPolicy: 'hard_fact',
      answerSource: 'agent_inference',
      evidenceReferences: ['resume:sha256:' + DIGEST, 'job:sha256:' + DIGEST],
      inferenceRationaleDigest: DIGEST,
      inferenceEvidenceDigests: INFERENCE_EVIDENCE_DIGESTS,
    })),
    (error) => error.code === 'E_DECISION_INFERENCE_POLICY',
  );
});

test('source evidence and non-inference metadata constraints are enforced', () => {
  assert.throws(
    () => validateApplicationDecision(decision({ evidenceReferences: [] })),
    (error) => error.code === 'E_DECISION_EVIDENCE_REQUIRED',
  );
  assert.throws(
    () => validateApplicationDecision(decision({ inferenceRationaleDigest: DIGEST })),
    (error) => error.code === 'E_DECISION_RATIONALE_FORBIDDEN',
  );
  assert.throws(
    () => validateApplicationDecision(decision({ inferenceEvidenceDigests: INFERENCE_EVIDENCE_DIGESTS })),
    (error) => error.code === 'E_DECISION_INFERENCE_EVIDENCE_FORBIDDEN',
  );
  assert.throws(
    () => validateApplicationDecision(decision({ answerSource: 'other', evidenceReferences: [] })),
    (error) => error.code === 'E_DECISION_SOURCE',
  );
});


test('select options require current option membership and compatible controls', () => {
  const select = decision({
    proposedAnswer: 'Remote',
    answerSource: 'profile_verified',
    evidenceReferences: ['profile_verified:answer-1'],
    proposedAction: 'select_option',
    expectedRetainedState: 'Remote',
  });
  assert.equal(validateApplicationDecision(select, context({
    allowedSources: ['profile_verified'],
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
      allowedSources: ['profile_verified'],
      allowedActions: ['select_option'],
      currentControl: {
        kind: 'select',
        visible: true,
        enabled: true,
        options: [{ value: 'On-site', disabled: false }],
      },
    })),
    (error) => error.code === 'E_DECISION_OPTION_MEMBERSHIP',
  );
});

test('unknown keys, stale context, duplicate retained state, and ineligible submission fail closed', () => {
  assert.throws(
    () => validateApplicationDecision({ ...decision(), unexpected: true }),
    (error) => error.code === 'E_DECISION_UNKNOWN_KEY',
  );
  assert.throws(
    () => validateApplicationDecision(decision({ observationId: 'old-observation' }), context()),
    (error) => error.code === 'E_DECISION_STALE_CONTEXT',
  );
  assert.throws(
    () => validateApplicationDecision(decision({ expectedRetainedState: 'Already retained' }), context({
      currentField: { retained: true, valid: true, value: 'Already retained' },
    })),
    (error) => error.code === 'E_DECISION_DUPLICATE_RETAINED_STATE',
  );
  assert.throws(
    () => validateApplicationDecision(decision({
      proposedAction: 'click',
      proposedAnswer: null,
      expectedRetainedState: null,
      fieldId: 'field-1',
      controlReference: 'observation-1:control-1',
      answerSource: 'memory',
      evidenceReferences: ['memory:submit'],
      automaticSubmissionEligible: true,
      reobservationRequired: false,
    }), context({
      allowedSources: ['memory'],
      allowedActions: ['click'],
      currentControl: { kind: 'button', type: 'submit', visible: true, enabled: true },
    })),
    (error) => error.code === 'E_DECISION_SUBMISSION_INELIGIBLE',
  );
});

test('boolean recognition follows the same runtime validator', () => {
  assert.equal(isApplicationDecision(decision(), context()), true);
  assert.equal(isApplicationDecision(decision({ answerSource: 'other' })), false);
});
