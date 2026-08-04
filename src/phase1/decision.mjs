import { fileURLToPath } from 'node:url';

export const APPLICATION_DECISION_SCHEMA_PATH = fileURLToPath(
  new URL('../../schemas/application-decision.schema.json', import.meta.url),
);

export const FIELD_POLICIES = Object.freeze([
  'subjective',
  'qualification',
  'legal',
  'demographic',
  'identity',
  'hard_fact',
]);

export const ANSWER_SOURCES = Object.freeze([
  'memory',
  'profile',
  'resume',
  'agent_inference',
  'user',
]);

export const PROPOSED_ACTIONS = Object.freeze([
  'fill_text',
  'clear',
  'select_option',
  'toggle',
  'upload_file',
  'click',
  'open_dialog',
  'close_dialog',
  'navigate',
  'wait',
  'reobserve',
]);

export const MODEL_TIERS = Object.freeze([
  'cheap',
  'standard',
  'strong',
  'highest',
]);

export const DECISION_KEYS = Object.freeze([
  'observationId',
  'fieldId',
  'controlReference',
  'fieldPolicy',
  'proposedAnswer',
  'answerSource',
  'evidenceReferences',
  'inferenceRationaleDigest',
  'inferenceEvidenceDigests',
  'proposedAction',
  'expectedRetainedState',
  'modelTier',
  'confidence',
  'reasonCode',
  'reobservationRequired',
  'automaticSubmissionEligible',
]);

export const MAX_IDENTIFIER_LENGTH = 512;
export const MAX_REASON_CODE_LENGTH = 128;
export const REASON_CODE_PATTERN = /^[a-z][a-z0-9_.-]*$/u;
export const MAX_EVIDENCE_REFERENCE_LENGTH = 512;
export const MAX_EVIDENCE_REFERENCES = 64;
export const MAX_ANSWER_STRING_LENGTH = 64 * 1024;
export const MAX_JSON_ARRAY_ITEMS = 128;
export const MAX_JSON_OBJECT_PROPERTIES = 128;
export const MAX_JSON_DEPTH = 12;
export const SHA256_HEX_PATTERN = /^[a-f0-9]{64}$/u;

const FORBIDDEN_CONTROL_CHARACTERS = /[\u0000-\u001f\u007f]/u;
const DECISION_KEY_SET = new Set(DECISION_KEYS);
const FIELD_POLICY_SET = new Set(FIELD_POLICIES);
const ANSWER_SOURCE_SET = new Set(ANSWER_SOURCES);
const PROPOSED_ACTION_SET = new Set(PROPOSED_ACTIONS);
const MODEL_TIER_SET = new Set(MODEL_TIERS);
const EVIDENCE_REQUIRED_SOURCES = new Set(['memory', 'profile', 'resume']);
const PROTECTED_POLICIES = new Set(['legal', 'demographic', 'identity', 'hard_fact']);
const INFERENCE_EVIDENCE_KEYS = new Set(['resumeSha256', 'jobDescriptionSha256']);

const CONTROL_ACTIONS = new Set([
  'fill_text',
  'clear',
  'select_option',
  'toggle',
  'upload_file',
  'click',
  'open_dialog',
]);
const NO_CONTROL_ACTIONS = new Set(['close_dialog', 'navigate', 'wait', 'reobserve']);
const POLICY_ALLOWED_SOURCES = Object.freeze({
  subjective: ANSWER_SOURCE_SET,
  qualification: ANSWER_SOURCE_SET,
  legal: new Set(['memory', 'profile', 'user']),
  demographic: new Set(['memory', 'profile', 'user']),
  identity: new Set(['memory', 'profile', 'user']),
  hard_fact: new Set(['memory', 'profile', 'user']),
});

export class ApplicationDecisionValidationError extends TypeError {
  constructor(code, location = '$', message = null) {
    const suffix = location ? `:${location}` : '';
    super(message ?? `${code}${suffix}`);
    this.name = 'ApplicationDecisionValidationError';
    this.code = code;
    this.location = location;
  }
}

function fail(code, location = '$', message = null) {
  throw new ApplicationDecisionValidationError(code, location, message);
}

function isPlainObject(value) {
  if (value === null || typeof value !== 'object' || Array.isArray(value)) return false;
  const prototype = Object.getPrototypeOf(value);
  return prototype === Object.prototype || prototype === null;
}

function requireObject(value, location) {
  if (!isPlainObject(value)) fail('E_DECISION_OBJECT', location);
  return value;
}

function rejectUnknownKeys(value, allowed, location) {
  const unknown = Object.keys(value).filter((key) => !allowed.has(key)).sort();
  if (unknown.length > 0) fail('E_DECISION_UNKNOWN_KEY', `${location}.${unknown[0]}`);
}

function requireString(value, location, {
  max = MAX_IDENTIFIER_LENGTH,
  nonEmpty = true,
  code = 'E_DECISION_STRING',
} = {}) {
  if (typeof value !== 'string' || (nonEmpty && value.length === 0)
      || value.length > max || FORBIDDEN_CONTROL_CHARACTERS.test(value)) {
    fail(code, location);
  }
  return value;
}

function requireNullableString(value, location, options = {}) {
  if (value === null) return value;
  return requireString(value, location, options);
}

function requireBoolean(value, location) {
  if (typeof value !== 'boolean') fail('E_DECISION_BOOLEAN', location);
  return value;
}

function requireFiniteNumber(value, location) {
  if (typeof value !== 'number' || !Number.isFinite(value)) fail('E_DECISION_NUMBER', location);
  return value;
}

function requireArray(value, location) {
  if (!Array.isArray(value)) fail('E_DECISION_ARRAY', location);
  return value;
}

function hasOwn(value, key) {
  return Object.prototype.hasOwnProperty.call(value, key);
}

function normalizeSource(value, location) {
  requireString(value, location, { max: 64, code: 'E_DECISION_SOURCE' });
  if (!ANSWER_SOURCE_SET.has(value)) fail('E_DECISION_SOURCE', location);
  return value;
}

function normalizeModelTier(value, location) {
  requireString(value, location, { max: 64, code: 'E_DECISION_MODEL_TIER' });
  if (!MODEL_TIER_SET.has(value)) fail('E_DECISION_MODEL_TIER', location);
  return value;
}

function validateJsonValue(value, location = '$', depth = 0, seen = new Set()) {
  if (value === null || typeof value === 'string' || typeof value === 'boolean') {
    if (typeof value === 'string' && value.length > MAX_ANSWER_STRING_LENGTH) {
      fail('E_DECISION_VALUE_BOUNDS', location);
    }
    if (typeof value === 'string' && FORBIDDEN_CONTROL_CHARACTERS.test(value)) {
      fail('E_DECISION_VALUE', location);
    }
    return;
  }
  if (typeof value === 'number') {
    requireFiniteNumber(value, location);
    return;
  }
  if (typeof value !== 'object') fail('E_DECISION_VALUE', location);
  if (depth >= MAX_JSON_DEPTH) fail('E_DECISION_VALUE_BOUNDS', location);
  if (seen.has(value)) fail('E_DECISION_VALUE_CYCLE', location);
  seen.add(value);
  if (Array.isArray(value)) {
    if (value.length > MAX_JSON_ARRAY_ITEMS) fail('E_DECISION_VALUE_BOUNDS', location);
    for (let index = 0; index < value.length; index += 1) {
      validateJsonValue(value[index], `${location}[${index}]`, depth + 1, seen);
    }
  } else {
    if (!isPlainObject(value)) fail('E_DECISION_VALUE', location);
    const keys = Object.keys(value);
    if (keys.length > MAX_JSON_OBJECT_PROPERTIES) fail('E_DECISION_VALUE_BOUNDS', location);
    for (const key of keys) {
      if (key.length === 0 || key.length > MAX_IDENTIFIER_LENGTH
          || FORBIDDEN_CONTROL_CHARACTERS.test(key)) {
        fail('E_DECISION_VALUE', `${location}.${key}`);
      }
      validateJsonValue(value[key], `${location}.${key}`, depth + 1, seen);
    }
  }
  seen.delete(value);
}

function cloneJsonValue(value) {
  if (Array.isArray(value)) return value.map(cloneJsonValue);
  if (isPlainObject(value)) {
    return Object.fromEntries(Object.entries(value).map(([key, item]) => [key, cloneJsonValue(item)]));
  }
  return value;
}

function jsonEqual(left, right) {
  if (Object.is(left, right)) return true;
  if (typeof left !== typeof right || left === null || right === null) return false;
  if (Array.isArray(left)) {
    return Array.isArray(right)
      && left.length === right.length
      && left.every((item, index) => jsonEqual(item, right[index]));
  }
  if (isPlainObject(left) && isPlainObject(right)) {
    const leftKeys = Object.keys(left).sort();
    const rightKeys = Object.keys(right).sort();
    return leftKeys.length === rightKeys.length
      && leftKeys.every((key, index) => key === rightKeys[index] && jsonEqual(left[key], right[key]));
  }
  return false;
}

function contextValue(context, ...keys) {
  for (const key of keys) {
    if (context !== undefined && context !== null && hasOwn(context, key) && context[key] !== undefined) {
      return context[key];
    }
  }
  return undefined;
}

function nestedContextValue(context, objectKeys, valueKeys) {
  const direct = contextValue(context, ...valueKeys);
  if (direct !== undefined) return direct;
  for (const objectKey of objectKeys) {
    const nested = context?.[objectKey];
    if (nested === undefined || nested === null) continue;
    if (typeof nested === 'string') return nested;
    if (isPlainObject(nested)) {
      const value = contextValue(nested, ...valueKeys);
      if (value !== undefined) return value;
    }
  }
  return undefined;
}

function normalizeContext(context) {
  if (context === undefined || context === null) return {};
  const normalized = requireObject(context, '$context');
  const allowed = new Set([
    'observationId',
    'observation_id',
    'currentObservationId',
    'latestObservationId',
    'latest_observation_id',
    'current_observation_id',
    'currentObservation',
    'observation',
    'fieldId',
    'field_id',
    'currentFieldId',
    'latestFieldId',
    'latest_field_id',
    'current_field_id',
    'currentField',
    'field',
    'controlReference',
    'control_reference',
    'currentControlReference',
    'latestControlReference',
    'latest_control_reference',
    'current_control_reference',
    'currentControl',
    'control',
    'allowedSources',
    'allowed_sources',
    'allowedAnswerSources',
    'allowed_answer_sources',
    'allowedActions',
    'allowed_actions',
    'options',
    'allowedOptions',
    'allowed_options',
    'retainedState',
    'retained_state',
    'retainedStates',
    'retained_states',
    'submissionEligible',
    'submission_eligible',
    'automaticSubmissionEligible',
    'automatic_submission_eligible',
    'canSubmit',
    'can_submit',
    'finalSubmitEligible',
    'final_submit_eligible',
    'retentionComplete',
    'retention_complete',
    'auditPassed',
    'audit_passed',
    'finalSubmit',
    'final_submit',
  ]);
  rejectUnknownKeys(normalized, allowed, '$context');
  return normalized;
}

function currentObservationId(context) {
  return nestedContextValue(
    context,
    ['currentObservation', 'observation'],
    [
      'currentObservationId',
      'latestObservationId',
      'latest_observation_id',
      'current_observation_id',
      'observationId',
      'observation_id',
      'id',
    ],
  );
}

function currentFieldId(context) {
  return nestedContextValue(
    context,
    ['currentField', 'field'],
    [
      'currentFieldId',
      'latestFieldId',
      'latest_field_id',
      'current_field_id',
      'fieldId',
      'field_id',
      'id',
      'stable_id',
    ],
  );
}

function currentControlReference(context) {
  return nestedContextValue(
    context,
    ['currentControl', 'control'],
    [
      'currentControlReference',
      'latestControlReference',
      'latest_control_reference',
      'current_control_reference',
      'controlReference',
      'control_reference',
      'ref',
      'reference',
    ],
  );
}

function assertFreshContext(decision, context) {
  const observationId = currentObservationId(context);
  const fieldId = currentFieldId(context);
  const controlReference = currentControlReference(context);
  if (observationId !== undefined && decision.observationId !== observationId) {
    fail('E_DECISION_STALE_CONTEXT', 'observationId');
  }
  if (fieldId !== undefined && decision.fieldId !== null && decision.fieldId !== fieldId) {
    fail('E_DECISION_STALE_CONTEXT', 'fieldId');
  }
  if (controlReference !== undefined
      && decision.controlReference !== null
      && decision.controlReference !== controlReference) {
    fail('E_DECISION_STALE_CONTEXT', 'controlReference');
  }
}

function normalizedSet(values, location, normalizer) {
  requireArray(values, location);
  const result = new Set();
  for (let index = 0; index < values.length; index += 1) {
    result.add(normalizer(values[index], `${location}[${index}]`));
  }
  return result;
}

function assertContextAllowLists(decision, context) {
  const allowedSources = contextValue(
    context,
    'allowedSources',
    'allowed_sources',
    'allowedAnswerSources',
    'allowed_answer_sources',
  );
  if (allowedSources !== undefined) {
    const allowed = normalizedSet(allowedSources, '$context.allowedSources', normalizeSource);
    if (!allowed.has(decision.answerSource)) fail('E_DECISION_SOURCE_NOT_ALLOWED', 'answerSource');
  }
  const allowedActions = contextValue(context, 'allowedActions', 'allowed_actions');
  if (allowedActions !== undefined) {
    const allowed = normalizedSet(allowedActions, '$context.allowedActions', (value, location) => {
      requireString(value, location, { max: 64, code: 'E_DECISION_ACTION' });
      if (!PROPOSED_ACTION_SET.has(value)) fail('E_DECISION_ACTION', location);
      return value;
    });
    if (!allowed.has(decision.proposedAction)) fail('E_DECISION_ACTION_NOT_ALLOWED', 'proposedAction');
  }
}

function assertPolicyRestrictions(decision) {
  if (decision.answerSource === 'agent_inference' && PROTECTED_POLICIES.has(decision.fieldPolicy)) {
    fail('E_DECISION_INFERENCE_POLICY', 'answerSource');
  }
  const allowed = POLICY_ALLOWED_SOURCES[decision.fieldPolicy];
  if (!allowed.has(decision.answerSource)) fail('E_DECISION_POLICY_SOURCE', 'fieldPolicy');
}

function assertEvidenceRequirements(decision) {
  const inference = decision.answerSource === 'agent_inference';
  const rationale = decision.inferenceRationaleDigest;
  const evidenceDigests = decision.inferenceEvidenceDigests;
  if (inference) {
    if (typeof rationale !== 'string' || !SHA256_HEX_PATTERN.test(rationale)) {
      fail('E_DECISION_RATIONALE_REQUIRED', 'inferenceRationaleDigest');
    }
    if (evidenceDigests === null) {
      fail('E_DECISION_INFERENCE_EVIDENCE_REQUIRED', 'inferenceEvidenceDigests');
    }
    if (decision.evidenceReferences.length === 0) {
      fail('E_DECISION_EVIDENCE_REQUIRED', 'evidenceReferences');
    }
  } else {
    if (rationale !== null) {
      fail('E_DECISION_RATIONALE_FORBIDDEN', 'inferenceRationaleDigest');
    }
    if (evidenceDigests !== null) {
      fail('E_DECISION_INFERENCE_EVIDENCE_FORBIDDEN', 'inferenceEvidenceDigests');
    }
    if (EVIDENCE_REQUIRED_SOURCES.has(decision.answerSource)
        && decision.evidenceReferences.length === 0) {
      fail('E_DECISION_EVIDENCE_REQUIRED', 'evidenceReferences');
    }
  }
}

function controlKind(control) {
  if (!isPlainObject(control)) return undefined;
  return contextValue(control, 'kind', 'tag', 'type', 'role');
}

function controlOptions(context) {
  const direct = contextValue(context, 'options', 'allowedOptions', 'allowed_options');
  if (direct !== undefined) return direct;
  const control = context?.currentControl ?? context?.control;
  return isPlainObject(control) ? control.options : undefined;
}

function optionValue(option) {
  if (typeof option === 'string' || typeof option === 'number') return String(option);
  if (!isPlainObject(option)) return undefined;
  const value = contextValue(option, 'value', 'id', 'optionValue', 'option_value');
  return value === undefined || value === null ? undefined : String(value);
}

function assertOptionMembership(decision, context) {
  if (decision.proposedAction !== 'select_option') return;
  const options = controlOptions(context);
  if (options === undefined) {
    if (context?.currentControl !== undefined || context?.control !== undefined
        || context?.options !== undefined || context?.allowedOptions !== undefined
        || context?.allowed_options !== undefined) {
      fail('E_DECISION_OPTION_MEMBERSHIP', 'proposedAnswer');
    }
    return;
  }
  requireArray(options, '$context.options');
  const values = new Set(options.map(optionValue).filter((value) => value !== undefined));
  const proposed = Array.isArray(decision.proposedAnswer)
    ? decision.proposedAnswer.map((value) => String(value))
    : [String(decision.proposedAnswer)];
  if (proposed.some((value) => !values.has(value))) fail('E_DECISION_OPTION_MEMBERSHIP', 'proposedAnswer');
  for (const option of options) {
    if (!isPlainObject(option)) continue;
    const value = optionValue(option);
    if (value !== undefined && proposed.includes(value)
        && (option.disabled === true || option.enabled === false)) {
      fail('E_DECISION_OPTION_MEMBERSHIP', 'proposedAnswer');
    }
  }
}

function assertActionCompatibility(decision, context) {
  const needsControl = CONTROL_ACTIONS.has(decision.proposedAction);
  const hasControl = decision.fieldId !== null && decision.controlReference !== null;
  if (needsControl && !hasControl) fail('E_DECISION_ACTION_CONTROL', 'proposedAction');
  if (NO_CONTROL_ACTIONS.has(decision.proposedAction) && hasControl) {
    fail('E_DECISION_ACTION_CONTROL', 'proposedAction');
  }
  if (!needsControl || (context?.currentControl === undefined && context?.control === undefined)) return;
  const currentControl = context?.currentControl ?? context?.control;
  const control = requireObject(currentControl, '$context.currentControl');
  const disabled = control.disabled === true || control.enabled === false;
  const visible = control.visible;
  if (disabled || visible === false) fail('E_DECISION_CONTROL_UNAVAILABLE', 'controlReference');
  const kind = controlKind(control);
  if (decision.proposedAction === 'select_option' && kind !== undefined
      && !['select', 'combobox', 'listbox', 'input'].includes(String(kind).toLowerCase())) {
    fail('E_DECISION_ACTION_CONTROL', 'proposedAction');
  }
  if (decision.proposedAction === 'toggle' && kind !== undefined
      && !['checkbox', 'radio', 'switch', 'input'].includes(String(kind).toLowerCase())) {
    fail('E_DECISION_ACTION_CONTROL', 'proposedAction');
  }
  if (decision.proposedAction === 'upload_file' && kind !== undefined
      && !['file', 'input'].includes(String(kind).toLowerCase())
      && String(control.type ?? '').toLowerCase() !== 'file') {
    fail('E_DECISION_ACTION_CONTROL', 'proposedAction');
  }
}

function assertAnswerCompatibility(decision) {
  const answer = decision.proposedAnswer;
  if (decision.proposedAction === 'fill_text' || decision.proposedAction === 'upload_file') {
    if (typeof answer !== 'string') fail('E_DECISION_ACTION_ANSWER', 'proposedAnswer');
  }
  if (decision.proposedAction === 'toggle' && typeof answer !== 'boolean') {
    fail('E_DECISION_ACTION_ANSWER', 'proposedAnswer');
  }
  if (decision.proposedAction === 'select_option'
      && !(typeof answer === 'string'
        || (Array.isArray(answer) && answer.every((item) => typeof item === 'string' || typeof item === 'number')))) {
    fail('E_DECISION_ACTION_ANSWER', 'proposedAnswer');
  }
  if (decision.proposedAction === 'clear' && answer !== null && answer !== '') {
    fail('E_DECISION_ACTION_ANSWER', 'proposedAnswer');
  }
}

function retainedStateForField(context, fieldId) {
  const field = context?.currentField ?? context?.field;
  if (isPlainObject(field) && (fieldId === undefined || fieldId === null || currentFieldId(context) === fieldId)) {
    return field;
  }
  return undefined;
}

function retainedRecordMatches(record, decision) {
  if (!isPlainObject(record)) return false;
  const recordFieldId = contextValue(record, 'fieldId', 'field_id', 'stable_id');
  const recordControlReference = contextValue(record, 'controlReference', 'control_reference', 'ref');
  if (recordFieldId === undefined && recordControlReference === undefined) return false;
  if (recordFieldId !== undefined && recordFieldId !== decision.fieldId) return false;
  if (recordControlReference !== undefined && recordControlReference !== decision.controlReference) return false;
  if (record.retained !== undefined && record.retained !== true) return false;
  if (record.valid !== undefined && record.valid !== true) return false;
  if (record.outcome !== undefined && !['succeeded', 'retained', 'success'].includes(record.outcome)) return false;
  const state = contextValue(record, 'expectedRetainedState', 'retainedState', 'state', 'value');
  return state !== undefined && jsonEqual(state, decision.expectedRetainedState);
}

function assertNoDuplicateRetainedState(decision, context) {
  if (decision.fieldId === null) return;
  const field = retainedStateForField(context, decision.fieldId);
  if (field?.retained === true && field.valid === true) {
    const currentState = contextValue(field, 'value', 'retainedState', 'expectedRetainedState', 'latest_state');
    if (currentState !== undefined && jsonEqual(currentState, decision.expectedRetainedState)) {
      fail('E_DECISION_DUPLICATE_RETAINED_STATE', 'expectedRetainedState');
    }
  }
  const retainedState = contextValue(context, 'retainedState', 'retained_state');
  if (retainedState !== undefined) {
    const current = retainedStateForField(context, decision.fieldId);
    if (current?.retained === true && current.valid === true
        && jsonEqual(retainedState, decision.expectedRetainedState)) {
      fail('E_DECISION_DUPLICATE_RETAINED_STATE', 'expectedRetainedState');
    }
    if (isPlainObject(retainedState) && retainedRecordMatches(retainedState, decision)) {
      fail('E_DECISION_DUPLICATE_RETAINED_STATE', 'expectedRetainedState');
    }
  }
  const retainedStates = contextValue(context, 'retainedStates', 'retained_states');
  if (retainedStates !== undefined) {
    requireArray(retainedStates, '$context.retainedStates');
    if (retainedStates.some((record) => retainedRecordMatches(record, decision))) {
      fail('E_DECISION_DUPLICATE_RETAINED_STATE', 'expectedRetainedState');
    }
  }
}

function submissionIsEligible(context) {
  const explicit = contextValue(
    context,
    'submissionEligible',
    'submission_eligible',
    'automaticSubmissionEligible',
    'automatic_submission_eligible',
    'canSubmit',
    'can_submit',
    'finalSubmitEligible',
    'final_submit_eligible',
  );
  if (explicit === true) return true;
  if ((context?.finalSubmit === true || context?.final_submit === true)
      && (context?.retentionComplete === true || context?.retention_complete === true)
      && (context?.auditPassed === true || context?.audit_passed === true)) return true;
  return false;
}

function assertSubmissionEligibility(decision, context) {
  if (!decision.automaticSubmissionEligible) return;
  if (!submissionIsEligible(context)) fail('E_DECISION_SUBMISSION_INELIGIBLE', 'automaticSubmissionEligible');
  if (decision.proposedAction !== 'click') fail('E_DECISION_SUBMISSION_ACTION', 'automaticSubmissionEligible');
  if (decision.reobservationRequired) fail('E_DECISION_SUBMISSION_REOBSERVE', 'automaticSubmissionEligible');
  const control = context?.currentControl ?? context?.control;
  if (isPlainObject(control)
      && (control.final === false
        || control.finalSubmit === false
        || control.submission === false
        || control.isSubmission === false)) {
    fail('E_DECISION_SUBMISSION_CONTROL', 'automaticSubmissionEligible');
  }
}

function normalizeEvidenceReferences(value) {
  requireArray(value, 'evidenceReferences');
  if (value.length > MAX_EVIDENCE_REFERENCES) fail('E_DECISION_EVIDENCE_BOUNDS', 'evidenceReferences');
  const result = [];
  const seen = new Set();
  for (let index = 0; index < value.length; index += 1) {
    const item = requireString(value[index], `evidenceReferences[${index}]`, {
      max: MAX_EVIDENCE_REFERENCE_LENGTH,
      code: 'E_DECISION_EVIDENCE',
    });
    if (seen.has(item)) fail('E_DECISION_DUPLICATE_EVIDENCE', `evidenceReferences[${index}]`);
    seen.add(item);
    result.push(item);
  }
  return result;
}

function normalizeInferenceEvidenceDigests(value) {
  if (value === null) return null;
  const evidence = requireObject(value, 'inferenceEvidenceDigests');
  rejectUnknownKeys(evidence, INFERENCE_EVIDENCE_KEYS, 'inferenceEvidenceDigests');
  const normalized = {};
  for (const key of INFERENCE_EVIDENCE_KEYS) {
    if (!hasOwn(evidence, key)) {
      fail('E_DECISION_REQUIRED', `inferenceEvidenceDigests.${key}`);
    }
    const digest = requireString(evidence[key], `inferenceEvidenceDigests.${key}`, {
      max: 64,
      code: 'E_DECISION_INFERENCE_EVIDENCE',
    });
    if (!SHA256_HEX_PATTERN.test(digest)) {
      fail('E_DECISION_INFERENCE_EVIDENCE', `inferenceEvidenceDigests.${key}`);
    }
    normalized[key] = digest;
  }
  return normalized;
}

function normalizeDecision(input) {
  const decision = requireObject(input, '$');
  rejectUnknownKeys(decision, DECISION_KEY_SET, '$');
  for (const key of DECISION_KEYS) {
    if (!hasOwn(decision, key)) fail('E_DECISION_REQUIRED', key);
  }

  if (decision.inferenceRationaleDigest === undefined) {
    fail('E_DECISION_RATIONALE', 'inferenceRationaleDigest');
  }

  const normalized = {
    observationId: requireString(decision.observationId, 'observationId'),
    fieldId: requireNullableString(decision.fieldId, 'fieldId'),
    controlReference: requireNullableString(decision.controlReference, 'controlReference'),
    fieldPolicy: requireString(decision.fieldPolicy, 'fieldPolicy', { max: 64, code: 'E_DECISION_POLICY' }),
    proposedAnswer: cloneJsonValue(decision.proposedAnswer),
    answerSource: normalizeSource(decision.answerSource, 'answerSource'),
    evidenceReferences: normalizeEvidenceReferences(decision.evidenceReferences),
    inferenceRationaleDigest: decision.inferenceRationaleDigest,
    inferenceEvidenceDigests: normalizeInferenceEvidenceDigests(decision.inferenceEvidenceDigests),
    proposedAction: requireString(decision.proposedAction, 'proposedAction', { max: 64, code: 'E_DECISION_ACTION' }),
    expectedRetainedState: cloneJsonValue(decision.expectedRetainedState),
    modelTier: normalizeModelTier(decision.modelTier, 'modelTier'),
    confidence: requireFiniteNumber(decision.confidence, 'confidence'),
    reasonCode: requireString(decision.reasonCode, 'reasonCode', {
      max: MAX_REASON_CODE_LENGTH,
      code: 'E_DECISION_REASON_CODE',
    }),
    reobservationRequired: requireBoolean(decision.reobservationRequired, 'reobservationRequired'),
    automaticSubmissionEligible: requireBoolean(decision.automaticSubmissionEligible, 'automaticSubmissionEligible'),
  };

  if (normalized.inferenceRationaleDigest !== null) {
    requireString(normalized.inferenceRationaleDigest, 'inferenceRationaleDigest', {
      max: 64,
      code: 'E_DECISION_RATIONALE',
    });
    if (!SHA256_HEX_PATTERN.test(normalized.inferenceRationaleDigest)) {
      fail('E_DECISION_RATIONALE', 'inferenceRationaleDigest');
    }
  }
  if (!REASON_CODE_PATTERN.test(normalized.reasonCode)) fail('E_DECISION_REASON_CODE', 'reasonCode');
  if (!FIELD_POLICY_SET.has(normalized.fieldPolicy)) fail('E_DECISION_POLICY', 'fieldPolicy');
  if (!PROPOSED_ACTION_SET.has(normalized.proposedAction)) fail('E_DECISION_ACTION', 'proposedAction');
  if (normalized.confidence < 0 || normalized.confidence > 1) fail('E_DECISION_CONFIDENCE', 'confidence');
  validateJsonValue(normalized.proposedAnswer, 'proposedAnswer');
  validateJsonValue(normalized.expectedRetainedState, 'expectedRetainedState');

  if (normalized.proposedAction === 'navigate' && typeof normalized.proposedAnswer !== 'string') {
    fail('E_DECISION_ACTION_ANSWER', 'proposedAnswer');
  }
  return normalized;
}

export function normalizeApplicationDecision(input) {
  const normalized = normalizeDecision(input);
  assertPolicyRestrictions(normalized);
  assertEvidenceRequirements(normalized);
  return normalized;
}

export function validateApplicationDecision(input, context = undefined) {
  const normalized = normalizeApplicationDecision(input);
  const normalizedContext = normalizeContext(context);
  assertFreshContext(normalized, normalizedContext);
  assertContextAllowLists(normalized, normalizedContext);
  assertActionCompatibility(normalized, normalizedContext);
  assertAnswerCompatibility(normalized);
  assertOptionMembership(normalized, normalizedContext);
  assertNoDuplicateRetainedState(normalized, normalizedContext);
  assertSubmissionEligibility(normalized, normalizedContext);
  return normalized;
}

export function isApplicationDecision(input, context = undefined) {
  try {
    validateApplicationDecision(input, context);
    return true;
  } catch (error) {
    if (error instanceof ApplicationDecisionValidationError) return false;
    throw error;
  }
}

export function isFieldPolicy(value) {
  return typeof value === 'string' && FIELD_POLICY_SET.has(value);
}

export function isProposedAction(value) {
  return typeof value === 'string' && PROPOSED_ACTION_SET.has(value);
}

export function isModelTier(value) {
  return typeof value === 'string' && MODEL_TIER_SET.has(value);
}
