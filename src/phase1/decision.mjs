import { fileURLToPath } from 'node:url';

export const APPLICATION_DECISION_SCHEMA_PATH = fileURLToPath(new URL('../../schemas/application-decision.schema.json', import.meta.url));
export const DECISION_SCHEMA_PATH = APPLICATION_DECISION_SCHEMA_PATH;
export const SCHEMA_PATH = APPLICATION_DECISION_SCHEMA_PATH;
export const APPLICATION_DECISION_SCHEMA_FILE = APPLICATION_DECISION_SCHEMA_PATH;
export const schemaPath = APPLICATION_DECISION_SCHEMA_PATH;
export const FIELD_POLICIES = Object.freeze(['subjective', 'qualification', 'legal', 'demographic', 'identity', 'hard_fact']);
export const FIELD_POLICY_VALUES = FIELD_POLICIES;
export const FIELD_POLICY = Object.freeze({ SUBJECTIVE: 'subjective', QUALIFICATION: 'qualification', LEGAL: 'legal', DEMOGRAPHIC: 'demographic', IDENTITY: 'identity', HARD_FACT: 'hard_fact' });
export const FIELD_POLICY_ENUM = FIELD_POLICY;
export const DECISION_MODES = Object.freeze(['exact_memory', 'profile_evidence', 'resume_evidence', 'supported_inference', 'best_effort_inference', 'configured_default', 'configured_decline', 'require_user']);
export const DECISION_MODE_VALUES = DECISION_MODES;
export const ANSWER_SOURCES = DECISION_MODES;
export const ANSWER_SOURCE_VALUES = ANSWER_SOURCES;
export const DECISION_MODE = Object.freeze({ EXACT_MEMORY: 'exact_memory', PROFILE_EVIDENCE: 'profile_evidence', RESUME_EVIDENCE: 'resume_evidence', SUPPORTED_INFERENCE: 'supported_inference', BEST_EFFORT_INFERENCE: 'best_effort_inference', CONFIGURED_DEFAULT: 'configured_default', CONFIGURED_DECLINE: 'configured_decline', REQUIRE_USER: 'require_user' });
export const DECISION_MODE_ENUM = DECISION_MODE;
export const ANSWER_SOURCE = DECISION_MODE;
export const ANSWER_SOURCE_ENUM = ANSWER_SOURCE;
export const PROPOSED_ACTIONS = Object.freeze(['click', 'type_text', 'press_key', 'scroll', 'upload_file']);
export const ACTIONS = PROPOSED_ACTIONS;
export const PROPOSED_ACTION_VALUES = PROPOSED_ACTIONS;
export const ACTION = Object.freeze({ CLICK: 'click', TYPE_TEXT: 'type_text', PRESS_KEY: 'press_key', SCROLL: 'scroll', UPLOAD_FILE: 'upload_file' });
export const ACTION_ENUM = ACTION;
export const MODEL_TIERS = Object.freeze(['cheap', 'standard', 'strong', 'highest']);
export const MODEL_TIER_VALUES = MODEL_TIERS;
export const MODEL_TIER = Object.freeze({ CHEAP: 'cheap', STANDARD: 'standard', STRONG: 'strong', HIGHEST: 'highest' });
export const MODEL_TIER_ENUM = MODEL_TIER;
export const APPLICATION_DECISION_SCHEMA = APPLICATION_DECISION_SCHEMA_PATH;
export const VISUAL_KINDS = Object.freeze(['text', 'textarea', 'email', 'phone', 'number', 'date', 'radio_group', 'checkbox', 'checkbox_group', 'native_select', 'custom_select', 'combobox', 'autocomplete', 'file_upload', 'button', 'link', 'dialog', 'unknown']);
export const PROVIDERS = Object.freeze(['codex', 'gemini']);

const FORBIDDEN_CONTROL_CHARACTERS = /[\u0000-\u001f\u007f]/u;
const SHA256_HEX_PATTERN = /^[a-f0-9]{64}$/u;
export const SHA256_PATTERN = SHA256_HEX_PATTERN;
export const DECISION_KEYS = Object.freeze(['observationId', 'fieldId', 'targetId', 'fieldPolicy', 'proposedAnswer', 'answerSource', 'decisionMode', 'evidenceReferences', 'observationScreenshotSha256', 'provider', 'proposedAction', 'expectedRetainedState', 'modelTier', 'confidence', 'reasonCode', 'inferenceRationaleDigest', 'reobservationRequired', 'automaticSubmissionEligible']);
export const MAX_IDENTIFIER_LENGTH = 512;
export const MAX_REASON_CODE_LENGTH = 128;
export const REASON_CODE_PATTERN = /^[a-z][a-z0-9_.-]*$/u;
export const MAX_EVIDENCE_REFERENCE_LENGTH = 512;
export const MAX_EVIDENCE_REFERENCES = 64;
export const MAX_ANSWER_STRING_LENGTH = 64 * 1024;
export const MAX_JSON_ARRAY_ITEMS = 128;
export const MAX_JSON_OBJECT_PROPERTIES = 128;
export const MAX_JSON_DEPTH = 12;
const DECISION_KEY_SET = new Set(DECISION_KEYS);
const FIELD_POLICY_SET = new Set(FIELD_POLICIES);
const DECISION_MODE_SET = new Set(DECISION_MODES);
const PROPOSED_ACTION_SET = new Set(PROPOSED_ACTIONS);
const MODEL_TIER_SET = new Set(MODEL_TIERS);
const PROVIDER_SET = new Set(PROVIDERS);
const INFERENCE_MODES = new Set(['supported_inference', 'best_effort_inference']);
const EVIDENCE_MODES = new Set(['exact_memory', 'profile_evidence', 'resume_evidence', 'supported_inference', 'best_effort_inference']);
const SAFE_INFERENCE_POLICIES = new Set(['subjective', 'qualification']);
const POLICY_ALLOWED_SOURCES = Object.freeze({
  subjective: new Set(DECISION_MODES), qualification: new Set(DECISION_MODES),
  legal: new Set(['exact_memory', 'configured_default', 'configured_decline', 'require_user']),
  demographic: new Set(['exact_memory', 'configured_default', 'configured_decline', 'require_user']),
  identity: new Set(['exact_memory', 'configured_default', 'configured_decline', 'require_user']),
  hard_fact: new Set(['exact_memory', 'configured_default', 'configured_decline', 'require_user']),
});
const SOURCE_MODE_COMPATIBILITY = Object.freeze({
  exact_memory: new Set(['exact_memory']), profile_evidence: new Set(['profile_evidence', 'supported_inference']),
  resume_evidence: new Set(['resume_evidence', 'supported_inference']), supported_inference: new Set(['supported_inference']),
  best_effort_inference: new Set(['best_effort_inference']), configured_default: new Set(['configured_default']),
  configured_decline: new Set(['configured_decline']), require_user: new Set(['require_user']),
});
export const POLICY_ALLOWED_SOURCES_BY_POLICY = POLICY_ALLOWED_SOURCES;
export const SOURCE_MODE_MATRIX = SOURCE_MODE_COMPATIBILITY;

export class ApplicationDecisionValidationError extends TypeError {
  constructor(code, location = '$', message = null) {
    super(message ?? `${code}:${location}`);
    this.name = 'ApplicationDecisionValidationError';
    this.code = code;
    this.location = location;
  }
}
export const DecisionValidationError = ApplicationDecisionValidationError;
export const ValidationError = ApplicationDecisionValidationError;
export const DecisionError = ApplicationDecisionValidationError;

function fail(code, location = '$', message = null) { throw new ApplicationDecisionValidationError(code, location, message); }
function isPlainObject(value) {
  if (value === null || typeof value !== 'object' || Array.isArray(value)) return false;
  const prototype = Object.getPrototypeOf(value);
  return prototype === Object.prototype || prototype === null;
}
function requireObject(value, location) { if (!isPlainObject(value)) fail('E_DECISION_OBJECT', location); return value; }
function rejectUnknownKeys(value, allowed, location) {
  const unknown = Object.keys(value).filter((key) => !allowed.has(key)).sort();
  if (unknown.length > 0) fail('E_DECISION_UNKNOWN_KEY', `${location}.${unknown[0]}`);
}
function requireString(value, location, max = MAX_IDENTIFIER_LENGTH, code = 'E_DECISION_STRING') {
  if (typeof value !== 'string' || value.length === 0 || value.length > max || FORBIDDEN_CONTROL_CHARACTERS.test(value)) fail(code, location);
  return value;
}
function nullableString(value, location) { return value === null ? null : requireString(value, location); }
function requireBoolean(value, location) { if (typeof value !== 'boolean') fail('E_DECISION_BOOLEAN', location); return value; }
function requireNumber(value, location) { if (typeof value !== 'number' || !Number.isFinite(value)) fail('E_DECISION_NUMBER', location); return value; }
function requireArray(value, location) { if (!Array.isArray(value)) fail('E_DECISION_ARRAY', location); return value; }
function hasOwn(value, key) { return Object.prototype.hasOwnProperty.call(value, key); }
function normalizeSource(value, location) { requireString(value, location, 64, 'E_DECISION_SOURCE'); if (!DECISION_MODE_SET.has(value)) fail('E_DECISION_SOURCE', location); return value; }
function normalizeMode(value, location) { requireString(value, location, 64, 'E_DECISION_MODE'); if (!DECISION_MODE_SET.has(value)) fail('E_DECISION_MODE', location); return value; }
function normalizeTier(value, location) { requireString(value, location, 64, 'E_DECISION_MODEL_TIER'); if (!MODEL_TIER_SET.has(value)) fail('E_DECISION_MODEL_TIER', location); return value; }
function cloneJsonValue(value) {
  if (Array.isArray(value)) return value.map(cloneJsonValue);
  if (isPlainObject(value)) return Object.fromEntries(Object.entries(value).map(([key, item]) => [key, cloneJsonValue(item)]));
  return value;
}
function validateJsonValue(value, location = '$', depth = 0, seen = new Set()) {
  if (value === null || typeof value === 'string' || typeof value === 'boolean') {
    if (typeof value === 'string' && value.length > MAX_ANSWER_STRING_LENGTH) fail('E_DECISION_VALUE_BOUNDS', location);
    return;
  }
  if (typeof value === 'number') { requireNumber(value, location); return; }
  if (typeof value !== 'object') fail('E_DECISION_VALUE', location);
  if (depth >= MAX_JSON_DEPTH || seen.has(value)) fail('E_DECISION_VALUE_BOUNDS', location);
  seen.add(value);
  if (Array.isArray(value)) {
    if (value.length > MAX_JSON_ARRAY_ITEMS) fail('E_DECISION_VALUE_BOUNDS', location);
    value.forEach((item, index) => validateJsonValue(item, `${location}[${index}]`, depth + 1, seen));
  } else {
    if (!isPlainObject(value) || Object.keys(value).length > MAX_JSON_OBJECT_PROPERTIES) fail('E_DECISION_VALUE_BOUNDS', location);
    for (const [key, item] of Object.entries(value)) {
      if (key.length === 0 || key.length > MAX_IDENTIFIER_LENGTH || FORBIDDEN_CONTROL_CHARACTERS.test(key)) fail('E_DECISION_VALUE', `${location}.${key}`);
      validateJsonValue(item, `${location}.${key}`, depth + 1, seen);
    }
  }
  seen.delete(value);
}
function jsonEqual(left, right) {
  if (Object.is(left, right)) return true;
  if (typeof left !== typeof right || left === null || right === null) return false;
  if (Array.isArray(left)) return Array.isArray(right) && left.length === right.length && left.every((item, index) => jsonEqual(item, right[index]));
  if (isPlainObject(left) && isPlainObject(right)) {
    const leftKeys = Object.keys(left).sort(); const rightKeys = Object.keys(right).sort();
    return leftKeys.length === rightKeys.length && leftKeys.every((key, index) => key === rightKeys[index] && jsonEqual(left[key], right[key]));
  }
  return false;
}
function contextValue(context, ...keys) {
  for (const key of keys) if (hasOwn(context, key) && context[key] !== undefined) return context[key];
  return undefined;
}

function nestedContextValue(context, objects, keys) {
  const direct = contextValue(context, ...keys);
  if (direct !== undefined) return direct;
  for (const objectKey of objects) {
    const nested = contextValue(context, objectKey);
    if (typeof nested === 'string') return nested;
    if (isPlainObject(nested)) {
      const value = contextValue(nested, ...keys);
      if (value !== undefined) return value;
    }
  }
  return undefined;
}

function normalizeContext(context) {
  if (context === undefined || context === null) return {};
  const normalized = requireObject(context, '$context');
  rejectUnknownKeys(normalized, new Set([
    'observationId', 'fieldId', 'targetId', 'observationScreenshotSha256',
    'currentObservation', 'observation', 'currentTarget', 'target',
    'allowedSources', 'allowedModes', 'allowedActions', 'options',
    'retainedState', 'retainedStates', 'submissionEligible', 'retentionComplete',
    'auditPassed', 'finalSubmit', 'visualAudit', 'currentVisualAudit',
  ]), '$context');
  return normalized;
}

function currentObservationId(context) {
  return nestedContextValue(context, ['currentObservation', 'observation'], ['observationId', 'observation_id', 'id']);
}

function currentFieldId(context) {
  return nestedContextValue(context, ['currentTarget', 'target'], ['fieldId', 'field_id']);
}

function currentTargetId(context) {
  return nestedContextValue(context, ['currentTarget', 'target'], ['targetId', 'target_id']);
}

function currentScreenshot(context) {
  const direct = nestedContextValue(context, ['currentObservation', 'observation'], ['observationScreenshotSha256', 'screenshotSha256', 'screenshot_sha256']);
  if (direct !== undefined) return direct;
  const observation = contextValue(context, 'currentObservation', 'observation');
  return isPlainObject(observation) && isPlainObject(observation.surface) ? observation.surface.screenshot_sha256 : undefined;
}

function currentTarget(context) {
  return contextValue(context, 'currentTarget', 'target');
}

function assertFreshContext(decision, context) {
  const observationId = currentObservationId(context);
  const fieldId = currentFieldId(context);
  const targetId = currentTargetId(context);
  const screenshot = currentScreenshot(context);
  if (observationId !== undefined && decision.observationId !== observationId) fail('E_DECISION_STALE_CONTEXT', 'observationId');
  if (fieldId !== undefined && decision.fieldId !== null && decision.fieldId !== fieldId) fail('E_DECISION_STALE_CONTEXT', 'fieldId');
  if (targetId !== undefined && decision.targetId !== null && decision.targetId !== targetId) fail('E_DECISION_STALE_CONTEXT', 'targetId');
  if (screenshot !== undefined && decision.observationScreenshotSha256 !== screenshot) fail('E_DECISION_STALE_CONTEXT', 'observationScreenshotSha256');
}

function normalizedSet(values, location, normalizer) {
  requireArray(values, location);
  return new Set(values.map((value, index) => normalizer(value, `${location}[${index}]`)));
}

function assertAllowLists(decision, context) {
  const allowedSources = contextValue(context, 'allowedSources');
  if (allowedSources !== undefined && !normalizedSet(allowedSources, '$context.allowedSources', normalizeSource).has(decision.answerSource)) fail('E_DECISION_SOURCE_NOT_ALLOWED', 'answerSource');
  const allowedModes = contextValue(context, 'allowedModes');
  if (allowedModes !== undefined && !normalizedSet(allowedModes, '$context.allowedModes', normalizeMode).has(decision.decisionMode)) fail('E_DECISION_MODE_NOT_ALLOWED', 'decisionMode');
  const allowedActions = contextValue(context, 'allowedActions');
  if (allowedActions !== undefined && !normalizedSet(allowedActions, '$context.allowedActions', (value, location) => {
    requireString(value, location, 64, 'E_DECISION_ACTION');
    if (!PROPOSED_ACTION_SET.has(value)) fail('E_DECISION_ACTION', location);
    return value;
  }).has(decision.proposedAction)) fail('E_DECISION_ACTION_NOT_ALLOWED', 'proposedAction');
}

function assertPolicyRestrictions(decision) {
  const inference = INFERENCE_MODES.has(decision.decisionMode) || INFERENCE_MODES.has(decision.answerSource);
  if (inference && !SAFE_INFERENCE_POLICIES.has(decision.fieldPolicy)) {
    fail('E_DECISION_INFERENCE_POLICY', 'decisionMode');
  }
  if (decision.decisionMode === 'best_effort_inference' && decision.modelTier !== 'highest') {
    fail('E_DECISION_BEST_EFFORT_TIER', 'modelTier');
  }
  const allowed = POLICY_ALLOWED_SOURCES[decision.fieldPolicy];
  if (!allowed.has(decision.answerSource) || !allowed.has(decision.decisionMode)) {
    fail('E_DECISION_POLICY_SOURCE', 'fieldPolicy');
  }
  if (!SOURCE_MODE_COMPATIBILITY[decision.answerSource].has(decision.decisionMode)) {
    fail('E_DECISION_SOURCE_MODE', 'decisionMode');
  }
}

function assertEvidenceRequirements(decision) {
  const inference = INFERENCE_MODES.has(decision.decisionMode) || INFERENCE_MODES.has(decision.answerSource);
  if (inference) {
    if (typeof decision.inferenceRationaleDigest !== 'string' || !SHA256_HEX_PATTERN.test(decision.inferenceRationaleDigest)) fail('E_DECISION_RATIONALE_REQUIRED', 'inferenceRationaleDigest');
    if (decision.evidenceReferences.length === 0) fail('E_DECISION_EVIDENCE_REQUIRED', 'evidenceReferences');
  } else if (decision.inferenceRationaleDigest !== undefined && decision.inferenceRationaleDigest !== null) {
    fail('E_DECISION_RATIONALE_FORBIDDEN', 'inferenceRationaleDigest');
  }
  if (EVIDENCE_MODES.has(decision.decisionMode) && decision.evidenceReferences.length === 0) fail('E_DECISION_EVIDENCE_REQUIRED', 'evidenceReferences');
}

function targetKind(target) {
  return isPlainObject(target) ? contextValue(target, 'kind', 'visualKind') : undefined;
}

function assertActionCompatibility(decision, context) {
  const targetRequired = !['scroll', 'press_key'].includes(decision.proposedAction);
  if (targetRequired && decision.targetId === null) fail('E_DECISION_TARGET_REQUIRED', 'targetId');
  if (!targetRequired && decision.targetId === null && decision.fieldId !== null) fail('E_DECISION_TARGET', 'fieldId');
  const target = currentTarget(context);
  if (target === undefined) return;
  const normalizedTarget = requireObject(target, '$context.currentTarget');
  if ((normalizedTarget.enabled === false) || normalizedTarget.visible === false) fail('E_DECISION_TARGET_UNAVAILABLE', 'targetId');
  const kind = targetKind(normalizedTarget);
  if (decision.proposedAction === 'type_text' && kind !== undefined && !VISUAL_KINDS.includes(kind)) fail('E_DECISION_ACTION_TARGET', 'proposedAction');
  if (decision.proposedAction === 'upload_file' && kind !== undefined && kind !== 'file_upload') fail('E_DECISION_ACTION_TARGET', 'proposedAction');
}

function assertAnswerCompatibility(decision) {
  if (decision.proposedAction === 'type_text' || decision.proposedAction === 'upload_file' || decision.proposedAction === 'press_key') {
    if (typeof decision.proposedAnswer !== 'string') fail('E_DECISION_ACTION_ANSWER', 'proposedAnswer');
  }
  if (decision.proposedAction === 'scroll' && !(decision.proposedAnswer === null || typeof decision.proposedAnswer === 'number' || isPlainObject(decision.proposedAnswer))) fail('E_DECISION_ACTION_ANSWER', 'proposedAnswer');
}
function retainedRecordMatches(record, decision) {
  if (!isPlainObject(record)) return false;
  if (record.fieldId !== undefined && record.fieldId !== decision.fieldId) return false;
  if (record.targetId !== undefined && record.targetId !== decision.targetId) return false;
  if (record.retained !== undefined && record.retained !== true) return false;
  if (record.valid !== undefined && record.valid !== true) return false;
  const state = record.expectedRetainedState ?? record.retainedState ?? record.state;
  return state !== undefined && jsonEqual(state, decision.expectedRetainedState);
}

function assertNoDuplicateRetainedState(decision, context) {
  if (decision.fieldId === null) return;
  const state = contextValue(context, 'retainedState');
  if (isPlainObject(state) && retainedRecordMatches(state, decision)) fail('E_DECISION_DUPLICATE_RETAINED_STATE', 'expectedRetainedState');
  const states = contextValue(context, 'retainedStates');
  if (states !== undefined) {
    requireArray(states, '$context.retainedStates');
    if (states.some((record) => retainedRecordMatches(record, decision))) fail('E_DECISION_DUPLICATE_RETAINED_STATE', 'expectedRetainedState');
  }
}

function visualAuditMatches(decision, context) {
  const audit = contextValue(context, 'visualAudit', 'currentVisualAudit');
  if (!isPlainObject(audit)) return false;
  if (audit.observationId !== decision.observationId || audit.screenshotSha256 !== decision.observationScreenshotSha256) return false;
  const ids = audit.finalTargetIds ?? audit.final_candidate_target_ids;
  return Array.isArray(ids) && ids.includes(decision.targetId);
}

function submissionIsEligible(context) {
  if (contextValue(context, 'submissionEligible') === true) return true;
  return contextValue(context, 'finalSubmit') === true && contextValue(context, 'retentionComplete') === true && contextValue(context, 'auditPassed') === true;
}

function assertSubmissionEligibility(decision, context) {
  const target = currentTarget(context);
  if (decision.proposedAction === 'click' && isPlainObject(target) && targetKind(target) === 'final_candidate' && !visualAuditMatches(decision, context)) fail('E_DECISION_VISUAL_AUDIT', 'observationScreenshotSha256');
  if (!decision.automaticSubmissionEligible) return;
  if (!submissionIsEligible(context)) fail('E_DECISION_SUBMISSION_INELIGIBLE', 'automaticSubmissionEligible');
  if (decision.proposedAction !== 'click') fail('E_DECISION_SUBMISSION_ACTION', 'automaticSubmissionEligible');
  if (decision.reobservationRequired) fail('E_DECISION_SUBMISSION_REOBSERVE', 'automaticSubmissionEligible');
  if (!visualAuditMatches(decision, context)) fail('E_DECISION_VISUAL_AUDIT', 'observationScreenshotSha256');
}

function normalizeEvidence(value) {
  requireArray(value, 'evidenceReferences');
  if (value.length > MAX_EVIDENCE_REFERENCES) fail('E_DECISION_EVIDENCE_BOUNDS', 'evidenceReferences');
  const seen = new Set();
  return value.map((item, index) => {
    const normalized = requireString(item, `evidenceReferences[${index}]`, MAX_EVIDENCE_REFERENCE_LENGTH, 'E_DECISION_EVIDENCE');
    if (seen.has(normalized)) fail('E_DECISION_DUPLICATE_EVIDENCE', `evidenceReferences[${index}]`);
    seen.add(normalized);
    return normalized;
  });
}
function normalizeDecision(input) {
  const decision = requireObject(input, '$');
  rejectUnknownKeys(decision, DECISION_KEY_SET, '$');
  for (const key of DECISION_KEYS) if (key !== 'inferenceRationaleDigest' && !hasOwn(decision, key)) fail('E_DECISION_REQUIRED', key);
  const normalized = {
    observationId: requireString(decision.observationId, 'observationId'),
    fieldId: nullableString(decision.fieldId, 'fieldId'),
    targetId: nullableString(decision.targetId, 'targetId'),
    fieldPolicy: requireString(decision.fieldPolicy, 'fieldPolicy', 64, 'E_DECISION_POLICY'),
    proposedAnswer: cloneJsonValue(decision.proposedAnswer),
    answerSource: normalizeSource(decision.answerSource, 'answerSource'),
    decisionMode: normalizeMode(decision.decisionMode, 'decisionMode'),
    evidenceReferences: normalizeEvidence(decision.evidenceReferences),
    observationScreenshotSha256: requireString(decision.observationScreenshotSha256, 'observationScreenshotSha256', 64, 'E_DECISION_SCREENSHOT'),
    provider: requireString(decision.provider, 'provider', 16, 'E_DECISION_PROVIDER'),
    proposedAction: requireString(decision.proposedAction, 'proposedAction', 64, 'E_DECISION_ACTION'),
    expectedRetainedState: cloneJsonValue(decision.expectedRetainedState),
    modelTier: normalizeTier(decision.modelTier, 'modelTier'),
    confidence: requireNumber(decision.confidence, 'confidence'),
    reasonCode: requireString(decision.reasonCode, 'reasonCode', MAX_REASON_CODE_LENGTH, 'E_DECISION_REASON_CODE'),
    reobservationRequired: requireBoolean(decision.reobservationRequired, 'reobservationRequired'),
    automaticSubmissionEligible: requireBoolean(decision.automaticSubmissionEligible, 'automaticSubmissionEligible'),
  };
  if (hasOwn(decision, 'inferenceRationaleDigest')) normalized.inferenceRationaleDigest = decision.inferenceRationaleDigest;
  if (!FIELD_POLICY_SET.has(normalized.fieldPolicy)) fail('E_DECISION_POLICY', 'fieldPolicy');
  if (!PROPOSED_ACTION_SET.has(normalized.proposedAction)) fail('E_DECISION_ACTION', 'proposedAction');
  if (!PROVIDER_SET.has(normalized.provider)) fail('E_DECISION_PROVIDER', 'provider');
  if (!SHA256_HEX_PATTERN.test(normalized.observationScreenshotSha256)) fail('E_DECISION_SCREENSHOT', 'observationScreenshotSha256');
  if (normalized.confidence < 0 || normalized.confidence > 1) fail('E_DECISION_CONFIDENCE', 'confidence');
  if (!REASON_CODE_PATTERN.test(normalized.reasonCode)) fail('E_DECISION_REASON_CODE', 'reasonCode');
  validateJsonValue(normalized.proposedAnswer, 'proposedAnswer');
  validateJsonValue(normalized.expectedRetainedState, 'expectedRetainedState');
  if (normalized.inferenceRationaleDigest !== undefined && normalized.inferenceRationaleDigest !== null) {
    requireString(normalized.inferenceRationaleDigest, 'inferenceRationaleDigest', 64, 'E_DECISION_RATIONALE');
    if (!SHA256_HEX_PATTERN.test(normalized.inferenceRationaleDigest)) fail('E_DECISION_RATIONALE', 'inferenceRationaleDigest');
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
  assertAllowLists(normalized, normalizedContext);
  assertActionCompatibility(normalized, normalizedContext);
  assertAnswerCompatibility(normalized);
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
