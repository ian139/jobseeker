import { createHash } from 'node:crypto';

export const VISUAL_OBSERVATION_SCHEMA = 'phase1-visual-observation-v1';
export const OBSERVATION_SCHEMA = VISUAL_OBSERVATION_SCHEMA;
const DIGEST = /^[a-f0-9]{64}$/;
const IDENTIFIER = /^(?!\s)[^\u0000-\u001f\u007f]{1,512}$/;
const BASENAME = /^(?!\.{1,2}$)(?!\s)[^/\\\u0000-\u001f\u007f]{1,255}$/;
const CANDIDATE_CLASSES = new Set(['field', 'non_final_navigation', 'final_candidate', 'unknown']);
const VALUE_STATES = new Set(['blank', 'present', 'selected', 'unknown']);
const PROVIDERS = new Set(['codex', 'gemini']);

export class VisualObservationError extends TypeError {
  constructor(message, code = 'VISUAL_OBSERVATION_INVALID') {
    super(message);
    this.name = 'VisualObservationError';
    this.code = code;
  }
}

function fail(path, message) {
  throw new VisualObservationError(`${path}: ${message}`);
}

function record(value, path) {
  if (value === null || typeof value !== 'object' || Array.isArray(value)) fail(path, 'expected an object');
}

function exact(value, allowed, path) {
  for (const key of Object.keys(value)) {
    if (!allowed.has(key)) fail(`${path}.${key}`, 'unknown key');
  }
}

function required(value, keys, path) {
  for (const key of keys) {
    if (!Object.prototype.hasOwnProperty.call(value, key)) fail(`${path}.${key}`, 'missing key');
  }
}

function string(value, path, { nullable = false, identifier = false, max = 4096 } = {}) {
  if (nullable && value === null) return;
  if (typeof value !== 'string') fail(path, 'expected a string');
  if (value.length > max || (identifier && !IDENTIFIER.test(value))) fail(path, 'invalid string');
}

function boolean(value, path, nullable = false) {
  if (nullable && value === null) return;
  if (typeof value !== 'boolean') fail(path, 'expected a boolean');
}

function integer(value, path, minimum = 0) {
  if (!Number.isSafeInteger(value) || value < minimum) fail(path, 'expected a bounded integer');
}

function array(value, path) {
  if (!Array.isArray(value)) fail(path, 'expected an array');
}

function clone(value) {
  if (Array.isArray(value)) return value.map(clone);
  if (value !== null && typeof value === 'object') {
    return Object.fromEntries(Object.entries(value).map(([key, item]) => [key, clone(item)]));
  }
  return value;
}

function deepFreeze(value) {
  if (value !== null && typeof value === 'object' && !Object.isFrozen(value)) {
    for (const child of Object.values(value)) deepFreeze(child);
    Object.freeze(value);
  }
  return value;
}

export function immutableObservation(value) {
  return deepFreeze(clone(value));
}

function validateViewport(value, path) {
  record(value, path);
  exact(value, new Set(['width', 'height']), path);
  required(value, ['width', 'height'], path);
  integer(value.width, `${path}.width`, 1);
  integer(value.height, `${path}.height`, 1);
}

function validateSurface(value, path) {
  record(value, path);
  exact(value, new Set(['surface_id', 'url', 'title', 'screenshot_sha256', 'viewport']), path);
  required(value, ['surface_id', 'url', 'title', 'screenshot_sha256', 'viewport'], path);
  string(value.surface_id, `${path}.surface_id`, { identifier: true });
  string(value.url, `${path}.url`, { max: 2048 });
  string(value.title, `${path}.title`, { max: 4096 });
  if (!DIGEST.test(value.screenshot_sha256)) fail(`${path}.screenshot_sha256`, 'expected SHA-256 digest');
  validateViewport(value.viewport, `${path}.viewport`);
}

function validateAgent(value, path) {
  record(value, path);
  exact(value, new Set(['provider', 'model']), path);
  required(value, ['provider', 'model'], path);
  string(value.provider, `${path}.provider`, { identifier: true });
  if (!PROVIDERS.has(value.provider)) fail(`${path}.provider`, 'unsupported model provider');
  string(value.model, `${path}.model`, { identifier: true, max: 256 });
}

function validateBounds(value, path, viewport) {
  record(value, path);
  exact(value, new Set(['x', 'y', 'width', 'height']), path);
  required(value, ['x', 'y', 'width', 'height'], path);
  integer(value.x, `${path}.x`);
  integer(value.y, `${path}.y`);
  integer(value.width, `${path}.width`, 1);
  integer(value.height, `${path}.height`, 1);
  if (value.x + value.width > viewport.width || value.y + value.height > viewport.height) {
    fail(path, 'bounds exceed the screenshot viewport');
  }
}

function validateOptions(value, path) {
  array(value, path);
  value.forEach((option, index) => {
    const itemPath = `${path}[${index}]`;
    record(option, itemPath);
    exact(option, new Set(['label', 'selected', 'disabled']), itemPath);
    required(option, ['label', 'selected', 'disabled'], itemPath);
    string(option.label, `${itemPath}.label`, { nullable: true, max: 1024 });
    boolean(option.selected, `${itemPath}.selected`);
    boolean(option.disabled, `${itemPath}.disabled`);
  });
}

function validateValidation(value, path) {
  record(value, path);
  exact(value, new Set(['valid', 'message_present']), path);
  required(value, ['valid', 'message_present'], path);
  boolean(value.valid, `${path}.valid`, true);
  boolean(value.message_present, `${path}.message_present`);
}

function validateFile(value, path) {
  if (value === null) return;
  record(value, path);
  exact(value, new Set(['present', 'file_name']), path);
  required(value, ['present', 'file_name'], path);
  boolean(value.present, `${path}.present`);
  if (value.file_name !== null && (typeof value.file_name !== 'string' || !BASENAME.test(value.file_name))) {
    fail(`${path}.file_name`, 'expected a safe file basename or null');
  }
}

function validateCandidate(value, path) {
  record(value, path);
  exact(value, new Set(['class', 'reason']), path);
  required(value, ['class', 'reason'], path);
  string(value.class, `${path}.class`, { identifier: true });
  if (!CANDIDATE_CLASSES.has(value.class)) fail(`${path}.class`, 'unsupported candidate class');
  string(value.reason, `${path}.reason`, { nullable: true, max: 2048 });
}

function validateSelected(value, path) {
  if (value === null || typeof value === 'boolean') return;
  if (!Array.isArray(value) || !value.every((item) => typeof item === 'string' && item.length <= 1024)) {
    fail(path, 'expected boolean, string array, or null');
  }
}

function validateTarget(target, path, viewport) {
  record(target, path);
  const keys = new Set([
    'target_id',
    'field_id',
    'group_id',
    'kind',
    'label',
    'description',
    'bounds',
    'visible',
    'enabled',
    'required',
    'readonly',
    'value_state',
    'checked',
    'selected',
    'options',
    'validation',
    'file',
    'candidate',
    'confidence',
  ]);
  exact(target, keys, path);
  required(target, [...keys], path);
  string(target.target_id, `${path}.target_id`, { identifier: true });
  string(target.field_id, `${path}.field_id`, { identifier: true, nullable: true });
  string(target.group_id, `${path}.group_id`, { identifier: true, nullable: true });
  string(target.kind, `${path}.kind`, { identifier: true });
  string(target.label, `${path}.label`, { nullable: true });
  string(target.description, `${path}.description`, { nullable: true });
  validateBounds(target.bounds, `${path}.bounds`, viewport);
  for (const key of ['visible', 'enabled', 'required', 'readonly']) boolean(target[key], `${path}.${key}`);
  string(target.value_state, `${path}.value_state`, { identifier: true });
  if (!VALUE_STATES.has(target.value_state)) fail(`${path}.value_state`, 'unsupported value state');
  boolean(target.checked, `${path}.checked`, true);
  validateSelected(target.selected, `${path}.selected`);
  validateOptions(target.options, `${path}.options`);
  validateValidation(target.validation, `${path}.validation`);
  validateFile(target.file, `${path}.file`);
  validateCandidate(target.candidate, `${path}.candidate`);
  if (typeof target.confidence !== 'number' || !Number.isFinite(target.confidence)
      || target.confidence < 0 || target.confidence > 1) {
    fail(`${path}.confidence`, 'expected a number from 0 through 1');
  }
}

function validateBlocker(value, path) {
  if (typeof value === 'string') {
    string(value, path, { identifier: true });
    return;
  }
  record(value, path);
  exact(value, new Set(['code', 'message', 'visible']), path);
  required(value, ['code', 'message', 'visible'], path);
  string(value.code, `${path}.code`, { identifier: true });
  string(value.message, `${path}.message`, { nullable: true });
  boolean(value.visible, `${path}.visible`);
}

export function validateObservation(observation) {
  record(observation, 'observation');
  const keys = new Set([
    'schema',
    'observation_id',
    'previous_observation_id',
    'observed_at',
    'surface',
    'agent',
    'targets',
    'blockers',
  ]);
  exact(observation, keys, 'observation');
  required(observation, [...keys], 'observation');
  if (observation.schema !== VISUAL_OBSERVATION_SCHEMA) fail('observation.schema', 'unexpected schema');
  string(observation.observation_id, 'observation.observation_id', { identifier: true });
  string(observation.previous_observation_id, 'observation.previous_observation_id', {
    identifier: true,
    nullable: true,
  });
  string(observation.observed_at, 'observation.observed_at', { max: 128 });
  if (!Number.isFinite(Date.parse(observation.observed_at))) fail('observation.observed_at', 'expected an ISO timestamp');
  validateSurface(observation.surface, 'observation.surface');
  validateAgent(observation.agent, 'observation.agent');
  array(observation.targets, 'observation.targets');
  const targetIds = new Set();
  const fieldIds = new Set();
  for (const [index, target] of observation.targets.entries()) {
    validateTarget(target, `observation.targets[${index}]`, observation.surface.viewport);
    if (targetIds.has(target.target_id)) fail(`observation.targets[${index}].target_id`, 'duplicate target identity');
    targetIds.add(target.target_id);
    if (target.field_id !== null) {
      if (fieldIds.has(target.field_id)) fail(`observation.targets[${index}].field_id`, 'duplicate field identity');
      fieldIds.add(target.field_id);
    }
  }
  array(observation.blockers, 'observation.blockers');
  observation.blockers.forEach((item, index) => validateBlocker(item, `observation.blockers[${index}]`));
  return true;
}

export function createVisualObservation(input) {
  validateObservation(input);
  return immutableObservation(input);
}

export function screenshotDigest(bytes) {
  const input = Buffer.isBuffer(bytes) ? bytes : Buffer.from(bytes);
  return createHash('sha256').update(input).digest('hex');
}

export function candidateClassIsAllowed(value) {
  return typeof value === 'string' && CANDIDATE_CLASSES.has(value);
}

export default validateObservation;
