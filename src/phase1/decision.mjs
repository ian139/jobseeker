import { fileURLToPath } from 'node:url';

export const APPLICATION_DECISION_SCHEMA_PATH = fileURLToPath(
  new URL('../../schemas/application-decision.schema.json', import.meta.url),
);
export const DECISION_SCHEMA_PATH = APPLICATION_DECISION_SCHEMA_PATH;
export const SCHEMA_PATH = APPLICATION_DECISION_SCHEMA_PATH;
export const APPLICATION_DECISION_SCHEMA_FILE = APPLICATION_DECISION_SCHEMA_PATH;
export const schemaPath = APPLICATION_DECISION_SCHEMA_PATH;

export const FIELD_POLICIES = Object.freeze([
  'subjective',
  'qualification',
  'legal',
  'demographic',
  'identity',
  'hard_fact',
]);
export const FIELD_POLICY_VALUES = FIELD_POLICIES;
export const FIELD_POLICY = Object.freeze({
  SUBJECTIVE: 'subjective',
  QUALIFICATION: 'qualification',
  LEGAL: 'legal',
  DEMOGRAPHIC: 'demographic',
  IDENTITY: 'identity',
  HARD_FACT: 'hard_fact',
});
export const FIELD_POLICY_ENUM = FIELD_POLICY;

export const DECISION_MODES = Object.freeze([
  'exact_memory',
  'profile_evidence',
  'resume_evidence',
  'supported_inference',
  'best_effort_inference',
  'configured_default',
  'configured_decline',
  'require_user',
]);
export const DECISION_MODE_VALUES = DECISION_MODES;
export const ANSWER_SOURCES = DECISION_MODES;
export const ANSWER_SOURCE_VALUES = ANSWER_SOURCES;
export const DECISION_MODE = Object.freeze({
  EXACT_MEMORY: 'exact_memory',
  PROFILE_EVIDENCE: 'profile_evidence',
  RESUME_EVIDENCE: 'resume_evidence',
  SUPPORTED_INFERENCE: 'supported_inference',
  BEST_EFFORT_INFERENCE: 'best_effort_inference',
  CONFIGURED_DEFAULT: 'configured_default',
  CONFIGURED_DECLINE: 'configured_decline',
  REQUIRE_USER: 'require_user',
});
export const DECISION_MODE_ENUM = DECISION_MODE;
export const ANSWER_SOURCE = DECISION_MODE;
export const ANSWER_SOURCE_ENUM = ANSWER_SOURCE;

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
export const ACTIONS = PROPOSED_ACTIONS;
export const PROPOSED_ACTION_VALUES = PROPOSED_ACTIONS;
export const ACTION = Object.freeze({
  FILL_TEXT: 'fill_text',
  CLEAR: 'clear',
  SELECT_OPTION: 'select_option',
  TOGGLE: 'toggle',
  UPLOAD_FILE: 'upload_file',
  CLICK: 'click',
  OPEN_DIALOG: 'open_dialog',
  CLOSE_DIALOG: 'close_dialog',
  NAVIGATE: 'navigate',
  WAIT: 'wait',
  REOBSERVE: 'reobserve',
});
export const ACTION_ENUM = ACTION;

export const MODEL_TIERS = Object.freeze([
  'cheap',
  'standard',
  'strong',
  'highest',
]);
export const MODEL_TIER_VALUES = MODEL_TIERS;
export const MODEL_TIER = Object.freeze({
  CHEAP: 'cheap',
  STANDARD: 'standard',
  STRONG: 'strong',
  HIGHEST: 'highest',
});
export const MODEL_TIER_ENUM = MODEL_TIER;
export const APPLICATION_DECISION_SCHEMA = APPLICATION_DECISION_SCHEMA_PATH;

export const LEGACY_SOURCE_ALIASES = Object.freeze({
  memory: 'exact_memory',
  profile: 'profile_evidence',
  resume: 'resume_evidence',
  agent_inference: 'supported_inference',
  user: 'require_user',
});
export const SOURCE_ALIASES = LEGACY_SOURCE_ALIASES;

const FORBIDDEN_CONTROL_CHARACTERS = /[\u0000-\u001f\u007f]/u;
export const MODEL_TIER_ALIASES = Object.freeze({
  'highest-inference': 'highest',
  highest_inference: 'highest',
});

export const DECISION_KEYS = Object.freeze([
  'observationId',
  'fieldId',
  'controlReference',
  'fieldPolicy',
  'proposedAnswer',
  'answerSource',
  'decisionMode',
  'evidenceReferences',
  'inferenceRationaleDigest',
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

const DECISION_KEY_SET = new Set(DECISION_KEYS);
const FIELD_POLICY_SET = new Set(FIELD_POLICIES);
const DECISION_MODE_SET = new Set(DECISION_MODES);
const PROPOSED_ACTION_SET = new Set(PROPOSED_ACTIONS);
const MODEL_TIER_SET = new Set(MODEL_TIERS);

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
const MUTATING_ACTIONS = new Set([
  'fill_text',
  'clear',
  'select_option',
  'toggle',
  'upload_file',
]);
const INFERENCE_MODES = new Set(['supported_inference', 'best_effort_inference']);
const EVIDENCE_MODES = new Set([
  'exact_memory',
  'profile_evidence',
  'resume_evidence',
  'supported_inference',
  'best_effort_inference',
]);
const SENSITIVE_POLICIES = new Set(['legal', 'demographic', 'identity']);
const SAFE_INFERENCE_POLICIES = new Set(['subjective', 'qualification']);

const SOURCE_MODE_COMPATIBILITY = Object.freeze({
  exact_memory: Object.freeze(new Set(['exact_memory'])),
  profile_evidence: Object.freeze(new Set(['profile_evidence', 'supported_inference'])),
  resume_evidence: Object.freeze(new Set(['resume_evidence', 'supported_inference'])),
  supported_inference: Object.freeze(new Set(['supported_inference'])),
  best_effort_inference: Object.freeze(new Set(['best_effort_inference'])),
  configured_default: Object.freeze(new Set(['configured_default'])),
  configured_decline: Object.freeze(new Set(['configured_decline'])),
  require_user: Object.freeze(new Set(['require_user'])),
});

const POLICY_ALLOWED_SOURCES = Object.freeze({
  subjective: Object.freeze(new Set(DECISION_MODES)),
  qualification: Object.freeze(new Set(DECISION_MODES)),
  legal: Object.freeze(new Set([
    'exact_memory',
    'configured_default',
    'configured_decline',
    'require_user',
  ])),
  demographic: Object.freeze(new Set([
    'exact_memory',
    'configured_default',
    'configured_decline',
    'require_user',
  ])),
  identity: Object.freeze(new Set([
    'exact_memory',
    'configured_default',
    'configured_decline',
    'require_user',
  ])),
  hard_fact: Object.freeze(new Set([
    'exact_memory',
    'configured_default',
    'configured_decline',
    'require_user',
  ])),
});

export const POLICY_ALLOWED_SOURCES_BY_POLICY = POLICY_ALLOWED_SOURCES;
export const SOURCE_MODE_MATRIX = SOURCE_MODE_COMPATIBILITY;

export class ApplicationDecisionValidationError extends TypeError {
  constructor(code, location = '$', message = null) {
    const suffix = location ? `:${location}` : '';
    super(message ?? `${code}${suffix}`);
    this.name = 'ApplicationDecisionValidationError';
    this.code = code;
    this.location = location;
  }
}
export const DecisionValidationError = ApplicationDecisionValidationError;
export const ValidationError = ApplicationDecisionValidationError;
export const DecisionError = ApplicationDecisionValidationError;

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
  if (typeof value !== 'string' || (nonEmpty && value.length === 0) || value.length > max || FORBIDDEN_CONTROL_CHARACTERS.test(value)) {
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
  const canonical = LEGACY_SOURCE_ALIASES[value] ?? value;
  if (!DECISION_MODE_SET.has(canonical)) fail('E_DECISION_SOURCE', location);
  return canonical;
}

function normalizeMode(value, location) {
  requireString(value, location, { max: 64, code: 'E_DECISION_MODE' });
  if (!DECISION_MODE_SET.has(value)) fail('E_DECISION_MODE', location);
  return value;
}

function normalizeModelTier(value, mode, location) {
  requireString(value, location, { max: 64, code: 'E_DECISION_MODEL_TIER' });
  const canonical = MODEL_TIER_ALIASES[value] ?? value;
  if (!MODEL_TIER_SET.has(canonical)) fail('E_DECISION_MODEL_TIER', location);
  if (MODEL_TIER_ALIASES[value] !== undefined && mode !== 'best_effort_inference') {
    fail('E_DECISION_MODEL_TIER', location);
  }
  return canonical;
}

function validateJsonValue(value, location = '$', depth = 0, seen = new Set()) {
  if (value === null || typeof value === 'string' || typeof value === 'boolean') {
    if (typeof value === 'string' && value.length > MAX_ANSWER_STRING_LENGTH) {
      fail('E_DECISION_VALUE_BOUNDS', location);
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
      if (key.length === 0 || key.length > MAX_IDENTIFIER_LENGTH || FORBIDDEN_CONTROL_CHARACTERS.test(key)) {
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
    return Array.isArray(right) && left.length === right.length && left.every((item, index) => jsonEqual(item, right[index]));
  }
  if (isPlainObject(left) && isPlainObject(right)) {
    const leftKeys = Object.keys(left).sort();
    const rightKeys = Object.keys(right).sort();
    return leftKeys.length === rightKeys.length && leftKeys.every((key, index) => key === rightKeys[index] && jsonEqual(left[key], right[key]));
  }
  return false;
}

function contextValue(context, ...keys) {
  for (const key of keys) {
    if (context !== undefined && context !== null && hasOwn(context, key) && context[key] !== undefined) return context[key];
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
    'allowedModes',
    'allowed_modes',
    'allowedDecisionModes',
    'allowed_decision_modes',
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
    ['currentObservationId', 'latestObservationId', 'latest_observation_id', 'current_observation_id', 'observationId', 'observation_id', 'id'],
  );
}

function currentFieldId(context) {
  return nestedContextValue(
    context,
    ['currentField', 'field'],
    ['currentFieldId', 'latestFieldId', 'latest_field_id', 'current_field_id', 'fieldId', 'field_id', 'id', 'stable_id'],
  );
}

function currentControlReference(context) {
  return nestedContextValue(
    context,
    ['currentControl', 'control'],
    ['currentControlReference', 'latestControlReference', 'latest_control_reference', 'current_control_reference', 'controlReference', 'control_reference', 'ref', 'reference'],
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
  if (controlReference !== undefined && decision.controlReference !== null && decision.controlReference !== controlReference) {
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
  const allowedSources = contextValue(context, 'allowedSources', 'allowed_sources', 'allowedAnswerSources', 'allowed_answer_sources');
  if (allowedSources !== undefined) {
    const allowed = normalizedSet(allowedSources, '$context.allowedSources', normalizeSource);
    if (!allowed.has(decision.answerSource)) fail('E_DECISION_SOURCE_NOT_ALLOWED', 'answerSource');
  }
  const allowedModes = contextValue(context, 'allowedModes', 'allowed_modes', 'allowedDecisionModes', 'allowed_decision_modes');
  if (allowedModes !== undefined) {
    const allowed = normalizedSet(allowedModes, '$context.allowedModes', normalizeMode);
    if (!allowed.has(decision.decisionMode)) fail('E_DECISION_MODE_NOT_ALLOWED', 'decisionMode');
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

function sourceAndModeAreCompatible(source, mode) {
  return SOURCE_MODE_COMPATIBILITY[source]?.has(mode) === true;
}

function assertPolicyRestrictions(decision) {
  if (INFERENCE_MODES.has(decision.decisionMode) && SENSITIVE_POLICIES.has(decision.fieldPolicy)) {
    fail('E_DECISION_INFERENCE_POLICY', 'decisionMode');
  }
  if (decision.decisionMode === 'best_effort_inference' && decision.modelTier !== 'highest') {
    fail('E_DECISION_BEST_EFFORT_TIER', 'modelTier');
  }
  if (decision.fieldPolicy === 'hard_fact' && decision.decisionMode === 'best_effort_inference') {
    fail('E_DECISION_INFERENCE_POLICY', 'decisionMode');
  }
  const allowed = POLICY_ALLOWED_SOURCES[decision.fieldPolicy];
  if (!allowed.has(decision.answerSource) || !allowed.has(decision.decisionMode)) {
    fail('E_DECISION_POLICY_SOURCE', 'fieldPolicy');
  }
  if (!sourceAndModeAreCompatible(decision.answerSource, decision.decisionMode)) {
    fail('E_DECISION_SOURCE_MODE', 'decisionMode');
  }
  if (INFERENCE_MODES.has(decision.decisionMode) && !SAFE_INFERENCE_POLICIES.has(decision.fieldPolicy)) {
    fail('E_DECISION_INFERENCE_POLICY', 'decisionMode');
  }
}
function assertEvidenceRequirements(decision) {
  const inference = INFERENCE_MODES.has(decision.decisionMode) || INFERENCE_MODES.has(decision.answerSource);
  const rationale = decision.inferenceRationaleDigest;
  if (inference) {
    if (typeof rationale !== 'string' || !SHA256_HEX_PATTERN.test(rationale)) {
      fail('E_DECISION_RATIONALE_REQUIRED', 'inferenceRationaleDigest');
    }
    if (decision.evidenceReferences.length === 0) fail('E_DECISION_EVIDENCE_REQUIRED', 'evidenceReferences');
  } else if (rationale !== undefined && rationale !== null) {
    fail('E_DECISION_RATIONALE_FORBIDDEN', 'inferenceRationaleDigest');
  }
  if (EVIDENCE_MODES.has(decision.decisionMode) && decision.evidenceReferences.length === 0) {
    fail('E_DECISION_EVIDENCE_REQUIRED', 'evidenceReferences');
  }
  if (decision.decisionMode === 'configured_default' || decision.decisionMode === 'configured_decline') {
    if (decision.evidenceReferences.length > 0 && decision.evidenceReferences.some((item) => item.length === 0)) {
      fail('E_DECISION_EVIDENCE', 'evidenceReferences');
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
        || context?.options !== undefined || context?.allowedOptions !== undefined || context?.allowed_options !== undefined) {
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
    if (value !== undefined && proposed.includes(value) && (option.disabled === true || option.enabled === false)) {
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
  if (decision.proposedAction === 'select_option' && kind !== undefined && !['select', 'combobox', 'listbox', 'input'].includes(String(kind).toLowerCase())) {
    fail('E_DECISION_ACTION_CONTROL', 'proposedAction');
  }
  if (decision.proposedAction === 'toggle' && kind !== undefined && !['checkbox', 'radio', 'switch', 'input'].includes(String(kind).toLowerCase())) {
    fail('E_DECISION_ACTION_CONTROL', 'proposedAction');
  }
  if (decision.proposedAction === 'upload_file' && kind !== undefined && !['file', 'input'].includes(String(kind).toLowerCase()) && String(control.type ?? '').toLowerCase() !== 'file') {
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
  if (decision.proposedAction === 'select_option' && !(typeof answer === 'string' || (Array.isArray(answer) && answer.every((item) => typeof item === 'string' || typeof item === 'number')))) {
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
    if (current?.retained === true
        && current.valid === true
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

function normalizeDecision(input) {
  const decision = requireObject(input, '$');
  rejectUnknownKeys(decision, DECISION_KEY_SET, '$');
  for (const key of DECISION_KEYS) {
    if (key === 'inferenceRationaleDigest') continue;
    if (!hasOwn(decision, key)) fail('E_DECISION_REQUIRED', key);
  }

  const normalized = {
    observationId: requireString(decision.observationId, 'observationId'),
    fieldId: requireNullableString(decision.fieldId, 'fieldId'),
    controlReference: requireNullableString(decision.controlReference, 'controlReference'),
    fieldPolicy: requireString(decision.fieldPolicy, 'fieldPolicy', { max: 64, code: 'E_DECISION_POLICY' }),
    proposedAnswer: cloneJsonValue(decision.proposedAnswer),
    answerSource: normalizeSource(decision.answerSource, 'answerSource'),
    decisionMode: normalizeMode(decision.decisionMode, 'decisionMode'),
    evidenceReferences: normalizeEvidenceReferences(decision.evidenceReferences),
    inferenceRationaleDigest: decision.inferenceRationaleDigest,
    proposedAction: requireString(decision.proposedAction, 'proposedAction', { max: 64, code: 'E_DECISION_ACTION' }),
    expectedRetainedState: cloneJsonValue(decision.expectedRetainedState),
    modelTier: decision.modelTier,
    confidence: requireFiniteNumber(decision.confidence, 'confidence'),
    reasonCode: requireString(decision.reasonCode, 'reasonCode', { max: MAX_REASON_CODE_LENGTH, code: 'E_DECISION_REASON_CODE' }),
    reobservationRequired: requireBoolean(decision.reobservationRequired, 'reobservationRequired'),
    automaticSubmissionEligible: requireBoolean(decision.automaticSubmissionEligible, 'automaticSubmissionEligible'),
  };
  if (!hasOwn(decision, 'inferenceRationaleDigest')) delete normalized.inferenceRationaleDigest;
  if (hasOwn(decision, 'inferenceRationaleDigest') && normalized.inferenceRationaleDigest === undefined) {
    fail('E_DECISION_RATIONALE', 'inferenceRationaleDigest');
  }
  if (!REASON_CODE_PATTERN.test(normalized.reasonCode)) fail('E_DECISION_REASON_CODE', 'reasonCode');

  if (!FIELD_POLICY_SET.has(normalized.fieldPolicy)) fail('E_DECISION_POLICY', 'fieldPolicy');
  if (!PROPOSED_ACTION_SET.has(normalized.proposedAction)) fail('E_DECISION_ACTION', 'proposedAction');
  if (normalized.confidence < 0 || normalized.confidence > 1) fail('E_DECISION_CONFIDENCE', 'confidence');
  validateJsonValue(normalized.proposedAnswer, 'proposedAnswer');
  validateJsonValue(normalized.expectedRetainedState, 'expectedRetainedState');
  normalized.modelTier = normalizeModelTier(normalized.modelTier, normalized.decisionMode, 'modelTier');
  if (normalized.inferenceRationaleDigest !== undefined && normalized.inferenceRationaleDigest !== null) {
    requireString(normalized.inferenceRationaleDigest, 'inferenceRationaleDigest', {
      max: 64,
      code: 'E_DECISION_RATIONALE',
    });
    if (!SHA256_HEX_PATTERN.test(normalized.inferenceRationaleDigest)) fail('E_DECISION_RATIONALE', 'inferenceRationaleDigest');
  }
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

export const validateDecision = validateApplicationDecision;
export const validate = validateApplicationDecision;

export function isApplicationDecision(input, context = undefined) {
  try {
    validateApplicationDecision(input, context);
    return true;
  } catch (error) {
    if (error instanceof ApplicationDecisionValidationError) return false;
    throw error;
  }
}

export function sourceForLegacyName(source) {
  return normalizeSource(source, 'source');
}

export function isDecisionMode(value) {
  return typeof value === 'string' && DECISION_MODE_SET.has(value);
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
