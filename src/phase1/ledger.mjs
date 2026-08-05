import { createHash } from 'node:crypto';

export const LEDGER_SCHEMA = 'phase1-ledger-v1';
export const OBSERVATION_SCHEMA = 'phase1-observation-v1';
export const DIFF_SCHEMA = 'phase1-diff-v1';

export const ANSWER_SOURCES = Object.freeze([
  'memory',
  'profile',
  'resume',
  'agent_inference',
  'user',
]);

export const CANDIDATE_CLASSES = Object.freeze([
  'field',
  'non_final_navigation',
  'final_candidate',
  'unknown',
]);

const ANSWER_SOURCE_SET = new Set(ANSWER_SOURCES);
const CANDIDATE_CLASS_SET = new Set(CANDIDATE_CLASSES);
const SEMANTIC_CHOICES = new Set([
  'blank',
  'decline',
  'not_applicable',
  'prefer_not_to_answer',
  'none',
]);
const ACTIONS = new Set([
  'fill',
  'clear',
  'type',
  'select',
  'check',
  'uncheck',
  'upload',
  'remove_upload',
  'scroll',
  'non_final_navigation',
  'final_submit',
]);
const ACTION_OUTCOMES = new Set(['attempted', 'succeeded', 'failed', 'blocked', 'retry', 'stale']);
const FINAL_SUBMIT_TERMINAL_OUTCOMES = new Set(['succeeded', 'failed', 'blocked']);
const FIELD_MUTATION_ACTIONS = new Set([
  'fill',
  'clear',
  'type',
  'select',
  'check',
  'uncheck',
  'upload',
  'remove_upload',
]);
const NON_FINAL_NAVIGATION_ACTION = 'non_final_navigation';
const BASENAME_MAX_LENGTH = 255;
const BASENAME_PATTERN = /^(?!\.{1,2}$)(?!\s)[^/\\\u0000-\u001f\u007f]{1,255}$/;

function isFieldMutationAction(action) {
  return FIELD_MUTATION_ACTIONS.has(action);
}

function observationHasFieldMutation(ledger, observationId = ledger.latest_observation_id) {
  return ledger.action_attempts.some((action) =>
    action.observation_id === observationId &&
    isFieldMutationAction(action.action) &&
    !action.stale_ref);
}

function isTargetlessAction(action) {
  return action === 'scroll';
}

function isCandidateRefClass(value) {
  return value === 'non_final_navigation' || value === 'final_candidate' || value === 'unknown';
}

const DIGEST = /^[a-f0-9]{64}$/;
const IDENTIFIER = /^(?!\s)[^\u0000-\u001f\u007f]{1,512}$/;

const INFERENCE_EVIDENCE_KEYS = new Set(['resume_sha256', 'job_description_sha256']);
const LEDGER_KEYS = new Set([
  'schema',
  'latest_observation_id',
  'observation_ids',
  'fields',
  'diffs',
  'action_attempts',
  'submit_action_count',
  'unknown_candidates',
  'active_blockers',
  'current_candidate_refs',
]);
const FIELD_KEYS = new Set([
  'field_id',
  'kind',
  'role',
  'group_id',
  'label',
  'name',
  'description',
  'latest_ref',
  'latest_observation_id',
  'ref_history',
  'present_in_latest_observation',
  'reachable',
  'visible',
  'enabled',
  'required',
  'readonly',
  'optional',
  'sensitive',
  'final',
  'latest_state',
  'answer_state',
  'answer_source',
  'value_digest',
  'semantic_choice',
  'inference_rationale_digest',
  'inference_evidence_digests',
  'retained',
  'valid',
  'retry_notes',
  'revealed_observation_id',
  'last_revealed_observation_id',
]);
const STATE_KEYS = new Set([
  'value_present',
  'value_kind',
  'checked',
  'selected',
  'option_states',
  'validity',
  'file',
]);
const VALIDITY_KEYS = new Set(['valid', 'aria_invalid', 'has_message']);
const FILE_STATE_KEYS = new Set(['accept', 'count', 'present']);
const OPTION_STATE_KEYS = new Set(['label', 'selected', 'disabled']);
const REF_KEYS = new Set(['observation_id', 'ref']);
const DIFF_KEYS = new Set([
  'schema',
  'from_observation_id',
  'to_observation_id',
  'added',
  'removed',
  'changed',
  'blockers_added',
  'blockers_removed',
]);
const DIFF_ITEM_KEYS = new Set(['field_id', 'ref', 'kind']);
const DIFF_CHANGE_KEYS = new Set(['field_id', 'changes']);
const CHANGE_KEYS = new Set(['property', 'from', 'to']);
const ACTION_KEYS = new Set([
  'action_id',
  'action',
  'field_id',
  'observation_id',
  'ref',
  'outcome',
  'retry_of',
  'error_code',
  'stale_ref',
]);
const UNKNOWN_KEYS = new Set(['stable_id', 'ref', 'observation_id', 'reason']);
const CANDIDATE_REF_KEYS = new Set(['stable_id', 'ref', 'observation_id', 'class']);

export class Phase1SchemaError extends TypeError {
  constructor(message, code = 'SCHEMA_INVALID') {
    super(message);
    this.name = 'Phase1SchemaError';
    this.code = code;
  }
}

export class Phase1StaleReferenceError extends Error {
  constructor(message = 'observation or control reference is stale') {
    super(message);
    this.name = 'Phase1StaleReferenceError';
    this.code = 'STALE_REFERENCE';
  }
}

function fail(path, message, code = 'SCHEMA_INVALID') {
  throw new Phase1SchemaError(`${path}: ${message}`, code);
}

function isRecord(value) {
  return value !== null && typeof value === 'object' && !Array.isArray(value);
}

function assertRecord(value, path) {
  if (!isRecord(value)) fail(path, 'expected an object');
}

function assertExactKeys(value, allowed, path) {
  for (const key of Object.keys(value)) {
    if (!allowed.has(key)) fail(`${path}.${key}`, 'unknown key');
  }
}

function assertRequiredKeys(value, required, path) {
  for (const key of required) {
    if (!Object.prototype.hasOwnProperty.call(value, key)) fail(`${path}.${key}`, 'missing key');
  }
}

function assertString(value, path, { identifier = false, nullable = false } = {}) {
  if (nullable && value === null) return;
  if (typeof value !== 'string') fail(path, 'expected a string');
  if (identifier ? !IDENTIFIER.test(value) : value.length > 4096) {
    fail(path, 'invalid string');
  }
}
function assertBasename(value, path) {
  if (typeof value !== 'string' || value.length > BASENAME_MAX_LENGTH || !BASENAME_PATTERN.test(value)) {
    fail(path, 'expected a bounded file basename');
  }
}

function assertBoolean(value, path, nullable = false) {
  if (nullable && value === null) return;
  if (typeof value !== 'boolean') fail(path, 'expected a boolean');
}

function assertInteger(value, path, { minimum = 0 } = {}) {
  if (!Number.isSafeInteger(value) || value < minimum) fail(path, 'expected a bounded integer');
}

function assertArray(value, path) {
  if (!Array.isArray(value)) fail(path, 'expected an array');
}

function clone(value) {
  if (Array.isArray(value)) return value.map(clone);
  if (isRecord(value)) {
    const result = {};
    for (const [key, item] of Object.entries(value)) result[key] = clone(item);
    return result;
  }
  return value;
}

function freeze(value) {
  if (isRecord(value) || Array.isArray(value)) {
    for (const item of Object.values(value)) freeze(item);
    Object.freeze(value);
  }
  return value;
}

function immutable(value) {
  return freeze(clone(value));
}

function valueKind(value) {
  if (value === null) return 'null';
  if (Array.isArray(value)) return 'string[]';
  return typeof value;
}

function validateValue(value, path) {
  if (value === null || typeof value === 'string' || typeof value === 'boolean') return;
  if (Array.isArray(value) && value.every((item) => typeof item === 'string')) return;
  fail(path, 'expected string, boolean, string array, or null');
}

function validateLocator(value, path) {
  if (value === null) {
    return;
  }
  assertRecord(value, path);
  const allowed = new Set(['strategy', 'value', 'role', 'name']);
  assertExactKeys(value, allowed, path);
  assertRequiredKeys(value, [...allowed], path);
  assertString(value.strategy, `${path}.strategy`, { identifier: true, nullable: true });
  assertString(value.value, `${path}.value`, { nullable: true });
  assertString(value.role, `${path}.role`, { nullable: true });
  assertString(value.name, `${path}.name`, { nullable: true });
}

function validateFrame(value, path) {
  assertRecord(value, path);
  const allowed = new Set(['id', 'parent_id', 'url', 'origin', 'accessible']);
  assertExactKeys(value, allowed, path);
  assertRequiredKeys(value, [...allowed], path);
  assertString(value.id, `${path}.id`, { identifier: true });
  assertString(value.parent_id, `${path}.parent_id`, { identifier: true, nullable: true });
  assertString(value.url, `${path}.url`, { nullable: true });
  assertString(value.origin, `${path}.origin`, { nullable: true });
  assertBoolean(value.accessible, `${path}.accessible`);
}

function validateFrames(value, path) {
  assertArray(value, path);
  const ids = new Set();
  value.forEach((frame, index) => {
    const framePath = `${path}[${index}]`;
    validateFrame(frame, framePath);
    if (ids.has(frame.id)) fail(`${framePath}.id`, 'duplicate frame identity');
    ids.add(frame.id);
  });
}

function validateOptions(value, path) {
  assertArray(value, path);
  value.forEach((option, index) => {
    const optionPath = `${path}[${index}]`;
    assertRecord(option, optionPath);
    const allowed = new Set(['value', 'label', 'disabled', 'selected']);
    assertExactKeys(option, allowed, optionPath);
    assertRequiredKeys(option, [...allowed], optionPath);
    assertString(option.value, `${optionPath}.value`, { nullable: true });
    assertString(option.label, `${optionPath}.label`, { nullable: true });
    assertBoolean(option.disabled, `${optionPath}.disabled`);
    assertBoolean(option.selected, `${optionPath}.selected`);
  });
}
function validateSelected(value, path) {
  if (value === null) return;
  if (!Array.isArray(value) || !value.every((item) => typeof item === 'string')) {
    fail(path, 'expected string array or null');
  }
}


function validateValidity(value, path) {
  assertRecord(value, path);
  assertExactKeys(value, new Set(['valid', 'aria_invalid', 'message']), path);
  assertRequiredKeys(value, ['valid', 'aria_invalid', 'message'], path);
  assertBoolean(value.valid, `${path}.valid`);
  assertBoolean(value.aria_invalid, `${path}.aria_invalid`, true);
  assertString(value.message, `${path}.message`, { nullable: true });
}

function validateFile(value, path) {
  if (value === null) return;
  assertRecord(value, path);
  assertExactKeys(value, new Set(['accept', 'count', 'names']), path);
  assertRequiredKeys(value, ['accept', 'count', 'names'], path);
  if (value.accept !== null && typeof value.accept !== 'string' &&
      !(Array.isArray(value.accept) && value.accept.every((item) => typeof item === 'string'))) {
    fail(`${path}.accept`, 'expected string, string array, or null');
  }
  assertInteger(value.count, `${path}.count`);
  assertArray(value.names, `${path}.names`);
  value.names.forEach((item, index) => assertBasename(item, `${path}.names[${index}]`));
}

function validateCandidate(value, path) {
  assertRecord(value, path);
  assertExactKeys(value, new Set(['class', 'reason']), path);
  assertRequiredKeys(value, ['class', 'reason'], path);
  assertString(value.class, `${path}.class`);
  if (!CANDIDATE_CLASS_SET.has(value.class)) fail(`${path}.class`, 'unknown candidate class');
  assertString(value.reason, `${path}.reason`, { nullable: true });
}

function validateControl(control, path) {
  assertRecord(control, path);
  const allowed = new Set([
    'ref',
    'stable_id',
    'group_id',
    'kind',
    'tag',
    'type',
    'role',
    'label',
    'name',
    'description',
    'locator',
    'frame_id',
    'visible',
    'enabled',
    'required',
    'readonly',
    'disabled',
    'value',
    'value_present',
    'checked',
    'selected',
    'options',
    'validity',
    'file',
    'candidate',
  ]);
  assertExactKeys(control, allowed, path);
  assertRequiredKeys(control, [...allowed], path);
  assertString(control.ref, `${path}.ref`, { identifier: true });
  assertString(control.stable_id, `${path}.stable_id`, { identifier: true });
  assertString(control.group_id, `${path}.group_id`, { identifier: true, nullable: true });
  assertString(control.kind, `${path}.kind`, { identifier: true });
  assertString(control.tag, `${path}.tag`, { identifier: true, nullable: true });
  assertString(control.type, `${path}.type`, { identifier: true, nullable: true });
  for (const key of ['role', 'label', 'name', 'description']) {
    assertString(control[key], `${path}.${key}`, { nullable: true });
  }
  validateLocator(control.locator, `${path}.locator`);
  assertString(control.frame_id, `${path}.frame_id`, { identifier: true, nullable: true });
  assertBoolean(control.visible, `${path}.visible`);
  assertBoolean(control.enabled, `${path}.enabled`);
  assertBoolean(control.required, `${path}.required`);
  assertBoolean(control.readonly, `${path}.readonly`);
  assertBoolean(control.disabled, `${path}.disabled`);
  validateValue(control.value, `${path}.value`);
  assertBoolean(control.value_present, `${path}.value_present`);
  assertBoolean(control.checked, `${path}.checked`, true);
  validateSelected(control.selected, `${path}.selected`);
  validateOptions(control.options, `${path}.options`);
  validateValidity(control.validity, `${path}.validity`);
  validateFile(control.file, `${path}.file`);
  validateCandidate(control.candidate, `${path}.candidate`);
}

function validateBlocker(blocker, path) {
  assertRecord(blocker, path);
  const allowed = new Set(['code', 'label', 'frame_id', 'visible']);
  assertExactKeys(blocker, allowed, path);
  assertRequiredKeys(blocker, [...allowed], path);
  assertString(blocker.code, `${path}.code`, { identifier: true });
  assertString(blocker.label, `${path}.label`, { nullable: true });
  assertString(blocker.frame_id, `${path}.frame_id`, { identifier: true, nullable: true });
  assertBoolean(blocker.visible, `${path}.visible`);
}

export function validateObservation(observation) {
  assertRecord(observation, 'observation');
  const allowed = new Set([
    'schema',
    'observation_id',
    'previous_observation_id',
    'observed_at',
    'url',
    'title',
    'snapshot_sha256',
    'frames',
    'controls',
    'blockers',
  ]);
  assertExactKeys(observation, allowed, 'observation');
  assertRequiredKeys(
    observation,
    [
      'schema',
      'observation_id',
      'previous_observation_id',
      'observed_at',
      'url',
      'title',
      'snapshot_sha256',
      'frames',
      'controls',
      'blockers',
    ],
    'observation',
  );
  if (observation.schema !== OBSERVATION_SCHEMA) fail('observation.schema', 'unexpected schema');
  assertString(observation.observation_id, 'observation.observation_id', { identifier: true });
  assertString(observation.previous_observation_id, 'observation.previous_observation_id', {
    identifier: true,
    nullable: true,
  });
  assertString(observation.observed_at, 'observation.observed_at');
  assertString(observation.url, 'observation.url');
  assertString(observation.title, 'observation.title');
  if (!DIGEST.test(observation.snapshot_sha256)) fail('observation.snapshot_sha256', 'expected SHA-256 digest');
  validateFrames(observation.frames, 'observation.frames');
  assertArray(observation.controls, 'observation.controls');
  const refs = new Set();
  const stableIds = new Set();
  observation.controls.forEach((control, index) => {
    validateControl(control, `observation.controls[${index}]`);
    if (refs.has(control.ref)) fail(`observation.controls[${index}].ref`, 'duplicate ref');
    refs.add(control.ref);
    if (stableIds.has(control.stable_id)) fail(`observation.controls[${index}].stable_id`, 'duplicate stable identity');
    stableIds.add(control.stable_id);
  });
  assertArray(observation.blockers, 'observation.blockers');
  observation.blockers.forEach((blocker, index) => validateBlocker(blocker, `observation.blockers[${index}]`));
  return true;
}

function controlSnapshot(control) {
  return {
    value_present: control.value_present,
    value_kind: valueKind(control.value),
    checked: control.checked,
    selected: control.selected,
    option_states: control.options.map((option) => ({
      label: option.label,
      selected: option.selected,
      disabled: option.disabled,
    })),
    validity: {
      valid: control.validity.valid,
      aria_invalid: control.validity.aria_invalid,
      has_message: control.validity.message !== null && control.validity.message.length > 0,
    },
    file: control.file === null
      ? { accept: null, count: 0, present: false }
      : {
        accept: Array.isArray(control.file.accept) ? [...control.file.accept] : control.file.accept,
        count: control.file.count,
        present: control.file.count > 0,
      },
  };
}

function isHoneypot(control) {
  const text = `${control.candidate.reason ?? ''} ${control.name ?? ''} ${control.label ?? ''}`.toLowerCase();
  return /honeypot|trap[-_ ]?field|bot[-_ ]?field/.test(text);
}

function isReachableControl(control) {
  return control.candidate.class === 'field' && control.visible && control.enabled &&
    !control.disabled && !isHoneypot(control);
}

function normalizeBlocker(blocker) {
  if (typeof blocker === 'string') return blocker;
  return blocker.code;
}
function currentCandidateRefsFor(observation) {
  return observation.controls
    .filter((control) => control.candidate.class !== 'field' &&
      control.visible && control.enabled && !control.disabled && !isHoneypot(control))
    .map((control) => ({
      stable_id: control.stable_id,
      ref: control.ref,
      observation_id: observation.observation_id,
      class: control.candidate.class,
    }))
    .sort((left, right) => {
      const leftKey = `${left.ref}\u0000${left.stable_id}\u0000${left.class}`;
      const rightKey = `${right.ref}\u0000${right.stable_id}\u0000${right.class}`;
      return leftKey < rightKey ? -1 : leftKey > rightKey ? 1 : 0;
    });
}

function currentFilePresent(control) {
  return control.file !== null && control.file.count > 0;
}
function isLedgerFileField(field) {
  return field?.latest_state?.file?.accept !== null || field?.latest_state?.file?.present === true;
}
function disabledControlRetainsPriorState(previous, control) {
  if (previous === null || previous.present_in_latest_observation !== true ||
      previous.retained !== true || previous.valid !== true ||
      control.visible !== true ||
      (control.disabled !== true && control.enabled !== false) ||
      control.validity.valid !== true ||
      control.validity.aria_invalid === true ||
      isLedgerFileField(previous)) {
    return false;
  }
  if (previous.answer_state === 'answered') {
    return control.value_present === true &&
      previous.value_digest !== null &&
      digestObservedValue(control, control.value) === previous.value_digest;
  }
  if (previous.answer_state === 'blank') {
    return blankStateIsSupported(previous, control);
  }
  return false;
}


function fieldFromControl(control, observationId, previous = null) {
  const reachable = isReachableControl(control);
  const wasReachable = previous?.reachable === true;
  const newlyRevealed = reachable && (!previous || !wasReachable);
  const refChanged = previous !== null && previous.latest_ref !== control.ref;
  const disappearedThenReturned = previous !== null && previous.present_in_latest_observation === false && reachable;
  const fileDisappeared = previous?.answer_state === 'answered' &&
    isLedgerFileField(previous) &&
    !currentFilePresent(control);
  const retainsPriorState = (
    previous?.answer_state &&
    previous.answer_state !== 'unresolved' &&
    !refChanged &&
    !disappearedThenReturned &&
    !fileDisappeared
  ) || disabledControlRetainsPriorState(previous, control);
  const retained = retainsPriorState ? previous.retained : false;
  const valid = retainsPriorState ? previous.valid : false;
  const refs = previous ? previous.ref_history.map(clone) : [];
  if (!refs.some((item) => item.observation_id === observationId && item.ref === control.ref)) {
    refs.push({ observation_id: observationId, ref: control.ref });
  }
  return {
    field_id: control.stable_id,
    kind: control.kind,
    role: control.role ?? null,
    group_id: control.group_id ?? null,
    label: control.label ?? null,
    name: control.name ?? null,
    description: control.description ?? null,
    latest_ref: control.ref,
    latest_observation_id: observationId,
    ref_history: refs,
    present_in_latest_observation: true,
    reachable,
    visible: control.visible,
    enabled: control.enabled,
    required: control.required,
    readonly: control.readonly,
    optional: !control.required,
    sensitive: previous?.sensitive ?? false,
    final: false,
    latest_state: controlSnapshot(control),
    answer_state: previous?.answer_state ?? 'unresolved',
    answer_source: previous?.answer_source ?? null,
    value_digest: previous?.value_digest ?? null,
    inference_rationale_digest: previous?.inference_rationale_digest ?? null,
    inference_evidence_digests: previous?.inference_evidence_digests
      ? clone(previous.inference_evidence_digests)
      : null,
    semantic_choice: previous?.semantic_choice ?? null,
    retained,
    valid,
    retry_notes: previous ? [...previous.retry_notes] : [],
    revealed_observation_id: previous?.revealed_observation_id ?? (reachable ? observationId : null),
    last_revealed_observation_id: newlyRevealed || disappearedThenReturned
      ? observationId
      : (previous?.last_revealed_observation_id ?? (reachable ? observationId : null)),
  };
}

function markAbsent(field) {
  const clearFileProof = isLedgerFileField(field) && field.answer_state === 'answered';
  return {
    ...field,
    present_in_latest_observation: false,
    reachable: false,
    retained: clearFileProof ? false : field.retained,
    valid: clearFileProof ? false : field.valid,
  };
}

function compareValues(left, right) {
  if (Object.is(left, right)) return true;
  if (Array.isArray(left) && Array.isArray(right) && left.length === right.length) {
    return left.every((value, index) => compareValues(value, right[index]));
  }
  if (isRecord(left) && isRecord(right)) {
    const leftKeys = Object.keys(left);
    const rightKeys = Object.keys(right);
    return leftKeys.length === rightKeys.length && leftKeys.every((key) => key in right && compareValues(left[key], right[key]));
  }
  return false;
}

function fieldChangeList(before, after) {
  const changes = [];
  const properties = [
    'kind',
    'role',
    'group_id',
    'label',
    'name',
    'description',
    'visible',
    'enabled',
    'required',
    'readonly',
    'latest_state',
    'reachable',
  ];
  for (const property of properties) {
    const left = property === 'latest_state' ? before.latest_state : before[property];
    const right = property === 'latest_state' ? after.latest_state : after[property];
    if (!compareValues(left, right)) changes.push({ property, from: clone(left), to: clone(right) });
  }
  return changes;
}

function diffFieldSnapshots(beforeFields, afterFields, fromObservationId, toObservationId, blockersAdded = [], blockersRemoved = []) {
  const before = new Map(beforeFields.map((field) => [field.field_id, field]));
  const after = new Map(afterFields.map((field) => [field.field_id, field]));
  const added = [];
  const removed = [];
  const changed = [];
  for (const field of afterFields) {
    const prior = before.get(field.field_id);
    if (!prior || !prior.present_in_latest_observation) {
      added.push({ field_id: field.field_id, ref: field.latest_ref, kind: field.kind });
    } else {
      const changes = fieldChangeList(prior, field);
      if (changes.length > 0) changed.push({ field_id: field.field_id, changes });
    }
  }
  for (const field of beforeFields) {
    if (field.present_in_latest_observation && !after.get(field.field_id)?.present_in_latest_observation) {
      removed.push({ field_id: field.field_id, ref: field.latest_ref, kind: field.kind });
    }
  }
  return {
    schema: DIFF_SCHEMA,
    from_observation_id: fromObservationId,
    to_observation_id: toObservationId,
    added,
    removed,
    changed,
    blockers_added: blockersAdded,
    blockers_removed: blockersRemoved,
  };
}

function observationControlMap(observation) {
  return new Map(observation.controls.map((control) => [control.stable_id, control]));
}

export function diffObservations(previousObservation, nextObservation) {
  if (previousObservation !== null) validateObservation(previousObservation);
  validateObservation(nextObservation);
  if (previousObservation && nextObservation.previous_observation_id !== previousObservation.observation_id) {
    throw new Phase1StaleReferenceError('observation chain does not match');
  }
  const previous = previousObservation ? observationControlMap(previousObservation) : new Map();
  const next = observationControlMap(nextObservation);
  const added = [];
  const removed = [];
  const changed = [];
  for (const control of nextObservation.controls) {
    const before = previous.get(control.stable_id);
    if (!before) {
      added.push({ field_id: control.stable_id, ref: control.ref, kind: control.kind });
      continue;
    }
    const beforeSnapshot = {
      ...before,
      latest_state: controlSnapshot(before),
      reachable: isReachableControl(before),
    };
    const afterSnapshot = {
      ...control,
      latest_state: controlSnapshot(control),
      reachable: isReachableControl(control),
    };
    const changes = fieldChangeList(beforeSnapshot, afterSnapshot);
    if (changes.length > 0 || before.ref !== control.ref) {
      if (before.ref !== control.ref) changes.unshift({ property: 'ref', from: before.ref, to: control.ref });
      changed.push({ field_id: control.stable_id, changes });
    }
  }
  if (previousObservation) {
    for (const control of previousObservation.controls) {
      if (!next.has(control.stable_id)) removed.push({ field_id: control.stable_id, ref: control.ref, kind: control.kind });
    }
  }
  const beforeBlockers = new Set(previousObservation?.blockers.map(normalizeBlocker) ?? []);
  const afterBlockers = new Set(nextObservation.blockers.map(normalizeBlocker));
  const blockersAdded = [...afterBlockers].filter((code) => !beforeBlockers.has(code)).sort();
  const blockersRemoved = [...beforeBlockers].filter((code) => !afterBlockers.has(code)).sort();
  return immutable({
    schema: DIFF_SCHEMA,
    from_observation_id: previousObservation?.observation_id ?? null,
    to_observation_id: nextObservation.observation_id,
    added,
    removed,
    changed,
    blockers_added: blockersAdded,
    blockers_removed: blockersRemoved,
  });
}

function emptyLedger() {
  return {
    schema: LEDGER_SCHEMA,
    latest_observation_id: null,
    observation_ids: [],
    fields: [],
    diffs: [],
    action_attempts: [],
    submit_action_count: 0,
    unknown_candidates: [],
    active_blockers: [],
    current_candidate_refs: [],
  };
}

export function createLedger(initialObservation = null) {
  const ledger = emptyLedger();
  if (initialObservation === null) return immutable(ledger);
  return mergeObservation(ledger, initialObservation);
}

function validateDigest(value, path, nullable = true) {
  if (value === null && nullable) return;
  if (typeof value !== 'string' || !DIGEST.test(value)) fail(path, 'expected lowercase SHA-256 digest');
}
function validateInferenceEvidenceDigests(value, path) {
  if (value === null) return;
  assertRecord(value, path);
  assertExactKeys(value, INFERENCE_EVIDENCE_KEYS, path);
  assertRequiredKeys(value, [...INFERENCE_EVIDENCE_KEYS], path);
  validateDigest(value.resume_sha256, `${path}.resume_sha256`, false);
  validateDigest(value.job_description_sha256, `${path}.job_description_sha256`, false);
}

function validateState(state, path) {
  assertRecord(state, path);
  assertExactKeys(state, STATE_KEYS, path);
  assertRequiredKeys(state, [...STATE_KEYS], path);
  assertBoolean(state.value_present, `${path}.value_present`);
  assertString(state.value_kind, `${path}.value_kind`, { identifier: true });
  assertBoolean(state.checked, `${path}.checked`, true);
  validateSelected(state.selected, `${path}.selected`);
  assertArray(state.option_states, `${path}.option_states`);
  state.option_states.forEach((option, index) => {
    const itemPath = `${path}.option_states[${index}]`;
    assertRecord(option, itemPath);
    assertExactKeys(option, OPTION_STATE_KEYS, itemPath);
    assertRequiredKeys(option, [...OPTION_STATE_KEYS], itemPath);
    assertString(option.label, `${itemPath}.label`, { nullable: true });
    assertBoolean(option.selected, `${itemPath}.selected`);
    assertBoolean(option.disabled, `${itemPath}.disabled`);
  });
  assertRecord(state.validity, `${path}.validity`);
  assertExactKeys(state.validity, VALIDITY_KEYS, `${path}.validity`);
  assertRequiredKeys(state.validity, [...VALIDITY_KEYS], `${path}.validity`);
  assertBoolean(state.validity.valid, `${path}.validity.valid`);
  assertBoolean(state.validity.aria_invalid, `${path}.validity.aria_invalid`, true);
  assertBoolean(state.validity.has_message, `${path}.validity.has_message`);
  assertRecord(state.file, `${path}.file`);
  assertExactKeys(state.file, FILE_STATE_KEYS, `${path}.file`);
  assertRequiredKeys(state.file, [...FILE_STATE_KEYS], `${path}.file`);
  if (state.file.accept !== null && typeof state.file.accept !== 'string' &&
      !(Array.isArray(state.file.accept) && state.file.accept.every((item) => typeof item === 'string'))) {
    fail(`${path}.file.accept`, 'invalid file accept state');
  }
  assertInteger(state.file.count, `${path}.file.count`);
  assertBoolean(state.file.present, `${path}.file.present`);
}

function validateField(field, path) {
  assertRecord(field, path);
  assertExactKeys(field, FIELD_KEYS, path);
  assertRequiredKeys(field, [...FIELD_KEYS], path);
  assertString(field.field_id, `${path}.field_id`, { identifier: true });
  assertString(field.kind, `${path}.kind`, { identifier: true });
  for (const key of ['role', 'group_id', 'label', 'name', 'description']) assertString(field[key], `${path}.${key}`, { nullable: true });
  assertString(field.latest_ref, `${path}.latest_ref`, { identifier: true });
  assertString(field.latest_observation_id, `${path}.latest_observation_id`, { identifier: true });
  assertArray(field.ref_history, `${path}.ref_history`);
  field.ref_history.forEach((item, index) => {
    const itemPath = `${path}.ref_history[${index}]`;
    assertRecord(item, itemPath);
    assertExactKeys(item, REF_KEYS, itemPath);
    assertRequiredKeys(item, [...REF_KEYS], itemPath);
    assertString(item.observation_id, `${itemPath}.observation_id`, { identifier: true });
    assertString(item.ref, `${itemPath}.ref`, { identifier: true });
  });
  for (const key of [
    'present_in_latest_observation',
    'reachable',
    'visible',
    'enabled',
    'required',
    'readonly',
    'optional',
    'sensitive',
    'final',
    'retained',
    'valid',
  ]) assertBoolean(field[key], `${path}.${key}`);
  validateState(field.latest_state, `${path}.latest_state`);
  if (!new Set(['unresolved', 'answered', 'blank']).has(field.answer_state)) fail(`${path}.answer_state`, 'invalid answer state');
  if (field.answer_state === 'unresolved' &&
      (field.answer_source !== null ||
       field.value_digest !== null ||
       field.semantic_choice !== null ||
       field.inference_rationale_digest !== null ||
       field.inference_evidence_digests !== null)) {
    fail(`${path}.answer_state`, 'unresolved field cannot carry an answer');
  }
  if (field.answer_state === 'answered' &&
      (field.answer_source === null || field.value_digest === null)) {
    fail(`${path}.answer_state`, 'answered field requires source and value digest');
  }
  if (field.answer_state === 'blank' &&
      (field.answer_source === null || field.semantic_choice === null)) {
    fail(`${path}.answer_state`, 'blank field requires source and deliberate semantic choice');
  }
  if (field.answer_source !== null) {
    assertString(field.answer_source, `${path}.answer_source`, { identifier: true });
    if (!ANSWER_SOURCE_SET.has(field.answer_source)) fail(`${path}.answer_source`, 'invalid answer source');
  }
  validateDigest(field.value_digest, `${path}.value_digest`);
  if (field.semantic_choice !== null) {
    assertString(field.semantic_choice, `${path}.semantic_choice`, { identifier: true });
    if (!SEMANTIC_CHOICES.has(field.semantic_choice)) fail(`${path}.semantic_choice`, 'invalid semantic choice');
  }
  validateDigest(field.inference_rationale_digest, `${path}.inference_rationale_digest`);
  validateInferenceEvidenceDigests(field.inference_evidence_digests, `${path}.inference_evidence_digests`);
  if (field.answer_source === 'agent_inference') {
    if (field.inference_rationale_digest === null || field.inference_evidence_digests === null) {
      fail(`${path}.inference_rationale_digest`, 'agent_inference requires inference metadata');
    }
  } else if (field.inference_rationale_digest !== null || field.inference_evidence_digests !== null) {
    fail(`${path}.inference_rationale_digest`, 'inference metadata is restricted to agent_inference');
  }
  assertArray(field.retry_notes, `${path}.retry_notes`);
  field.retry_notes.forEach((note, index) => assertString(note, `${path}.retry_notes[${index}]`, { identifier: true }));
  assertString(field.revealed_observation_id, `${path}.revealed_observation_id`, { identifier: true, nullable: true });
  assertString(field.last_revealed_observation_id, `${path}.last_revealed_observation_id`, { identifier: true, nullable: true });
}

function validateDiff(diff, path) {
  assertRecord(diff, path);
  assertExactKeys(diff, DIFF_KEYS, path);
  assertRequiredKeys(diff, [...DIFF_KEYS], path);
  if (diff.schema !== DIFF_SCHEMA) fail(`${path}.schema`, 'unexpected diff schema');
  assertString(diff.from_observation_id, `${path}.from_observation_id`, { identifier: true, nullable: true });
  assertString(diff.to_observation_id, `${path}.to_observation_id`, { identifier: true });
  for (const key of ['added', 'removed']) {
    assertArray(diff[key], `${path}.${key}`);
    diff[key].forEach((item, index) => {
      const itemPath = `${path}.${key}[${index}]`;
      assertRecord(item, itemPath);
      assertExactKeys(item, DIFF_ITEM_KEYS, itemPath);
      assertRequiredKeys(item, [...DIFF_ITEM_KEYS], itemPath);
      assertString(item.field_id, `${itemPath}.field_id`, { identifier: true });
      assertString(item.ref, `${itemPath}.ref`, { identifier: true });
      assertString(item.kind, `${itemPath}.kind`, { identifier: true });
    });
  }
  assertArray(diff.changed, `${path}.changed`);
  diff.changed.forEach((item, index) => {
    const itemPath = `${path}.changed[${index}]`;
    assertRecord(item, itemPath);
    assertExactKeys(item, DIFF_CHANGE_KEYS, itemPath);
    assertRequiredKeys(item, [...DIFF_CHANGE_KEYS], itemPath);
    assertString(item.field_id, `${itemPath}.field_id`, { identifier: true });
    assertArray(item.changes, `${itemPath}.changes`);
    item.changes.forEach((change, changeIndex) => {
      const changePath = `${itemPath}.changes[${changeIndex}]`;
      assertRecord(change, changePath);
      assertExactKeys(change, CHANGE_KEYS, changePath);
      assertRequiredKeys(change, [...CHANGE_KEYS], changePath);
      assertString(change.property, `${changePath}.property`, { identifier: true });
    });
  });
  for (const key of ['blockers_added', 'blockers_removed']) {
    assertArray(diff[key], `${path}.${key}`);
    diff[key].forEach((code, index) => assertString(code, `${path}.${key}[${index}]`, { identifier: true }));
  }
}

function validateAction(action, path) {
  assertRecord(action, path);
  assertExactKeys(action, ACTION_KEYS, path);
  assertRequiredKeys(action, [...ACTION_KEYS], path);
  assertString(action.action_id, `${path}.action_id`, { identifier: true });
  assertString(action.action, `${path}.action`, { identifier: true });
  if (!ACTIONS.has(action.action)) fail(`${path}.action`, 'invalid action');
  assertString(action.field_id, `${path}.field_id`, { identifier: true, nullable: true });
  assertString(action.observation_id, `${path}.observation_id`, { identifier: true });
  assertString(action.ref, `${path}.ref`, { identifier: true, nullable: true });
  assertString(action.outcome, `${path}.outcome`, { identifier: true });
  if (!ACTION_OUTCOMES.has(action.outcome)) fail(`${path}.outcome`, 'invalid action outcome');
  if (action.action === 'final_submit'
      && action.outcome !== 'attempted'
      && !FINAL_SUBMIT_TERMINAL_OUTCOMES.has(action.outcome)) {
    fail(`${path}.outcome`, 'final_submit must be attempted or terminal');
  }
  if (action.retry_of !== null) assertInteger(action.retry_of, `${path}.retry_of`, { minimum: 0 });
  assertString(action.error_code, `${path}.error_code`, { identifier: true, nullable: true });
  assertBoolean(action.stale_ref, `${path}.stale_ref`);
  validateActionBindingShape(action, path);
}
function validateActionBindingShape(action, path) {
  if (isFieldMutationAction(action.action)) {
    if (action.field_id === null || action.ref === null) {
      fail(path, 'field mutation requires field_id and ref');
    }
    return;
  }
  if (action.action === NON_FINAL_NAVIGATION_ACTION) {
    if (action.field_id !== null) fail(`${path}.field_id`, 'navigation cannot target a field');
    if (action.ref === null) fail(`${path}.ref`, 'non-final navigation requires a candidate ref');
    return;
  }
  if (isTargetlessAction(action.action) && (action.field_id !== null || action.ref !== null)) {
    fail(path, 'action does not accept a field binding');
  }
}
function validateActiveBlockers(value, path) {
  assertArray(value, path);
  const seen = new Set();
  for (let index = 0; index < value.length; index += 1) {
    const itemPath = `${path}[${index}]`;
    assertString(value[index], itemPath, { identifier: true });
    if (seen.has(value[index])) fail(itemPath, 'duplicate blocker code');
    if (index > 0 && value[index - 1] >= value[index]) fail(itemPath, 'blocker codes must be sorted');
    seen.add(value[index]);
  }
}

function validateCurrentCandidateRefs(value, path, latestObservationId) {
  assertArray(value, path);
  const refs = new Set();
  const identities = new Set();
  for (let index = 0; index < value.length; index += 1) {
    const item = value[index];
    const itemPath = `${path}[${index}]`;
    assertRecord(item, itemPath);
    assertExactKeys(item, CANDIDATE_REF_KEYS, itemPath);
    assertRequiredKeys(item, [...CANDIDATE_REF_KEYS], itemPath);
    assertString(item.stable_id, `${itemPath}.stable_id`, { identifier: true });
    assertString(item.ref, `${itemPath}.ref`, { identifier: true });
    assertString(item.observation_id, `${itemPath}.observation_id`, { identifier: true });
    assertString(item.class, `${itemPath}.class`, { identifier: true });
    if (!isCandidateRefClass(item.class)) fail(`${itemPath}.class`, 'invalid candidate ref class');
    if (latestObservationId === null || item.observation_id !== latestObservationId) {
      fail(`${itemPath}.observation_id`, 'candidate ref is not current');
    }
    if (refs.has(item.ref)) fail(`${itemPath}.ref`, 'duplicate candidate ref');
    if (identities.has(item.stable_id)) fail(`${itemPath}.stable_id`, 'duplicate candidate identity');
    if (index > 0) {
      const previous = value[index - 1];
      const previousKey = `${previous.ref}\u0000${previous.stable_id}\u0000${previous.class}`;
      const currentKey = `${item.ref}\u0000${item.stable_id}\u0000${item.class}`;
      if (previousKey >= currentKey) fail(itemPath, 'candidate refs must be sorted');
    }
    refs.add(item.ref);
    identities.add(item.stable_id);
  }
}


function validateLedgerShape(ledger) {
  assertRecord(ledger, 'ledger');
  assertExactKeys(ledger, LEDGER_KEYS, 'ledger');
  assertRequiredKeys(ledger, [...LEDGER_KEYS], 'ledger');
  if (ledger.schema !== LEDGER_SCHEMA) fail('ledger.schema', 'unexpected schema');
  assertString(ledger.latest_observation_id, 'ledger.latest_observation_id', { identifier: true, nullable: true });
  assertArray(ledger.observation_ids, 'ledger.observation_ids');
  const observations = new Set();
  ledger.observation_ids.forEach((id, index) => {
    assertString(id, `ledger.observation_ids[${index}]`, { identifier: true });
    if (observations.has(id)) fail(`ledger.observation_ids[${index}]`, 'duplicate observation id');
    observations.add(id);
  });
  if ((ledger.latest_observation_id === null) !== (ledger.observation_ids.length === 0)) {
    fail('ledger.latest_observation_id', 'latest observation does not match observation history');
  }
  if (ledger.latest_observation_id !== null && ledger.observation_ids.at(-1) !== ledger.latest_observation_id) {
    fail('ledger.observation_ids', 'latest observation must be last');
  }
  validateActiveBlockers(ledger.active_blockers, 'ledger.active_blockers');
  validateCurrentCandidateRefs(
    ledger.current_candidate_refs,
    'ledger.current_candidate_refs',
    ledger.latest_observation_id,
  );
  assertArray(ledger.fields, 'ledger.fields');
  const fields = new Set();
  ledger.fields.forEach((field, index) => {
    validateField(field, `ledger.fields[${index}]`);
    if (fields.has(field.field_id)) fail(`ledger.fields[${index}].field_id`, 'duplicate field id');
    fields.add(field.field_id);
  });
  assertArray(ledger.diffs, 'ledger.diffs');
  ledger.diffs.forEach((diff, index) => validateDiff(diff, `ledger.diffs[${index}]`));
  assertArray(ledger.action_attempts, 'ledger.action_attempts');
  const countedSubmits = ledger.action_attempts.filter(
    (action) => action.action === 'final_submit',
  ).length;
  if (countedSubmits !== ledger.submit_action_count) {
    fail('ledger.submit_action_count', 'does not match final action evidence');
  }
  const actionIds = new Set();
  ledger.action_attempts.forEach((action, index) => {
    validateAction(action, `ledger.action_attempts[${index}]`);
    if (actionIds.has(action.action_id)) fail(`ledger.action_attempts[${index}].action_id`, 'duplicate action id');
    actionIds.add(action.action_id);
  });
  assertInteger(ledger.submit_action_count, 'ledger.submit_action_count');
  assertArray(ledger.unknown_candidates, 'ledger.unknown_candidates');
  ledger.unknown_candidates.forEach((item, index) => {
    const path = `ledger.unknown_candidates[${index}]`;
    assertRecord(item, path);
    assertExactKeys(item, UNKNOWN_KEYS, path);
    assertRequiredKeys(item, [...UNKNOWN_KEYS], path);
    assertString(item.stable_id, `${path}.stable_id`, { identifier: true });
    assertString(item.ref, `${path}.ref`, { identifier: true });
    assertString(item.observation_id, `${path}.observation_id`, { identifier: true });
    assertString(item.reason, `${path}.reason`, { nullable: true });
  });
}

export function validateLedger(ledger) {
  validateLedgerShape(ledger);
  return true;
}

function normalizeFieldBeforeDiff(field) {
  return field;
}

export function mergeObservation(ledger, observation) {
  validateLedgerShape(ledger);
  validateObservation(observation);
  if (ledger.latest_observation_id === null) {
    if (observation.previous_observation_id !== null) {
      throw new Phase1StaleReferenceError('first observation must not point to a prior observation');
    }
  } else if (observation.previous_observation_id !== ledger.latest_observation_id) {
    throw new Phase1StaleReferenceError('observation is not the next observation in this ledger');
  }
  if (ledger.observation_ids.includes(observation.observation_id)) {
    throw new Phase1StaleReferenceError('observation was already merged');
  }
  const priorFields = ledger.fields;
  const priorById = new Map(priorFields.map((field) => [field.field_id, field]));
  const currentById = new Map(observation.controls.map((control) => [control.stable_id, control]));
  const nextFields = priorFields.map(markAbsent);
  const byId = new Map(nextFields.map((field) => [field.field_id, field]));
  for (const control of observation.controls) {
    if (control.candidate.class !== 'field') continue;
    const previous = priorById.get(control.stable_id) ?? null;
    const field = fieldFromControl(control, observation.observation_id, previous);
    byId.set(control.stable_id, field);
  }
  const orderedFields = [];
  const seen = new Set();
  for (const field of priorFields) {
    const next = byId.get(field.field_id);
    if (next) {
      orderedFields.push(next);
      seen.add(field.field_id);
    }
  }
  for (const control of observation.controls) {
    if (control.candidate.class !== 'field' || seen.has(control.stable_id)) continue;
    orderedFields.push(byId.get(control.stable_id));
    seen.add(control.stable_id);
  }
  const previousUnknown = ledger.unknown_candidates.filter((item) => item.observation_id !== observation.observation_id);
  const unknownCandidates = [
    ...previousUnknown,
    ...observation.controls
      .filter((control) => control.candidate.class === 'unknown' && control.visible && control.enabled)
      .map((control) => ({
        stable_id: control.stable_id,
        ref: control.ref,
        observation_id: observation.observation_id,
        reason: control.candidate.reason ?? null,
      })),
  ];
  const priorBlockers = ledger.active_blockers;
  const nextBlockers = [...new Set(observation.blockers.map(normalizeBlocker))].sort();
  const blockersAdded = nextBlockers.filter((code) => !priorBlockers.includes(code));
  const blockersRemoved = priorBlockers.filter((code) => !nextBlockers.includes(code));
  const currentCandidateRefs = currentCandidateRefsFor(observation);
  const diff = diffFieldSnapshots(
    priorFields.map(normalizeFieldBeforeDiff),
    orderedFields,
    ledger.latest_observation_id,
    observation.observation_id,
    [...new Set(blockersAdded)].sort(),
    [...new Set(blockersRemoved)].sort(),
  );
  const nextLedger = {
    ...clone(ledger),
    latest_observation_id: observation.observation_id,
    observation_ids: [...ledger.observation_ids, observation.observation_id],
    fields: orderedFields,
    diffs: [...ledger.diffs, diff],
    unknown_candidates: unknownCandidates,
    active_blockers: nextBlockers,
    current_candidate_refs: currentCandidateRefs,
  };
  return immutable(nextLedger);
}

function normalizeResolution(resolution) {
  assertRecord(resolution, 'resolution');
  const allowed = new Set([
    'field_id',
    'observation_id',
    'ref',
    'source',
    'value_digest',
    'semantic_choice',
    'inference_rationale_digest',
    'inference_evidence_digests',
    'sensitive',
  ]);
  assertExactKeys(resolution, allowed, 'resolution');
  assertRequiredKeys(resolution, ['field_id', 'observation_id', 'ref', 'source'], 'resolution');
  assertString(resolution.field_id, 'resolution.field_id', { identifier: true });
  assertString(resolution.observation_id, 'resolution.observation_id', { identifier: true });
  assertString(resolution.ref, 'resolution.ref', { identifier: true });
  assertString(resolution.source, 'resolution.source', { identifier: true });
  if (!ANSWER_SOURCE_SET.has(resolution.source)) fail('resolution.source', 'invalid answer source');
  const digest = resolution.value_digest ?? null;
  validateDigest(digest, 'resolution.value_digest');
  const semanticChoice = resolution.semantic_choice ?? null;
  if (semanticChoice !== null) {
    assertString(semanticChoice, 'resolution.semantic_choice', { identifier: true });
    if (!SEMANTIC_CHOICES.has(semanticChoice)) fail('resolution.semantic_choice', 'invalid semantic choice');
  }
  if (digest === null && semanticChoice === null) {
    fail('resolution', 'a value digest or deliberate semantic choice is required');
  }
  const rationaleDigest = resolution.inference_rationale_digest ?? null;
  validateDigest(rationaleDigest, 'resolution.inference_rationale_digest');
  const evidenceDigests = resolution.inference_evidence_digests ?? null;
  validateInferenceEvidenceDigests(evidenceDigests, 'resolution.inference_evidence_digests');
  if (resolution.source === 'agent_inference') {
    if (rationaleDigest === null || evidenceDigests === null) {
      fail('resolution.inference_rationale_digest', 'agent_inference requires inference metadata');
    }
  } else if (rationaleDigest !== null || evidenceDigests !== null) {
    fail('resolution.inference_rationale_digest', 'inference metadata is restricted to agent_inference');
  }
  if (resolution.sensitive !== undefined) assertBoolean(resolution.sensitive, 'resolution.sensitive');
  return {
    ...resolution,
    value_digest: digest,
    semantic_choice: semanticChoice,
    inference_rationale_digest: rationaleDigest,
    inference_evidence_digests: evidenceDigests === null ? null : clone(evidenceDigests),
  };
}

export function recordResolution(ledger, resolution) {
  validateLedgerShape(ledger);
  const normalized = normalizeResolution(resolution);
  const index = ledger.fields.findIndex((field) => field.field_id === normalized.field_id);
  if (index < 0) fail('resolution.field_id', 'unknown field identity', 'UNKNOWN_FIELD');
  const field = ledger.fields[index];
  if (ledger.latest_observation_id !== normalized.observation_id ||
      field.latest_observation_id !== normalized.observation_id ||
      field.latest_ref !== normalized.ref ||
      !field.present_in_latest_observation ||
      !field.reachable ||
      field.final) {
    throw new Phase1StaleReferenceError('resolution is not bound to the latest reachable control');
  }
  if (normalized.source === 'agent_inference' &&
      (normalized.sensitive === true || field.sensitive)) {
    fail('resolution.source', 'agent_inference is prohibited for sensitive fields');
  }
  const fields = ledger.fields.map((item, itemIndex) => {
    if (itemIndex !== index) return item;
    return {
      ...item,
      answer_state: normalized.value_digest === null ? 'blank' : 'answered',
      answer_source: normalized.source,
      value_digest: normalized.value_digest,
      semantic_choice: normalized.semantic_choice,
      inference_rationale_digest: normalized.inference_rationale_digest,
      inference_evidence_digests: normalized.inference_evidence_digests,
      sensitive: normalized.sensitive ?? item.sensitive,
      retained: false,
      valid: false,
    };
  });
  return immutable({ ...clone(ledger), fields });
}

export function markFieldSensitive(ledger, fieldId) {
  validateLedgerShape(ledger);
  assertString(fieldId, 'field_id', { identifier: true });
  const index = ledger.fields.findIndex((field) => field.field_id === fieldId);
  if (index < 0) fail('field_id', 'unknown field identity', 'UNKNOWN_FIELD');
  const field = ledger.fields[index];
  const derived = field.answer_source === 'resume' || field.answer_source === 'agent_inference';
  if (field.sensitive && !derived) return ledger;
  const fields = ledger.fields.map((item, itemIndex) => {
    if (itemIndex !== index) return item;
    return {
      ...item,
      sensitive: true,
      answer_state: derived ? 'unresolved' : item.answer_state,
      answer_source: derived ? null : item.answer_source,
      value_digest: derived ? null : item.value_digest,
      semantic_choice: derived ? null : item.semantic_choice,
      inference_rationale_digest: derived ? null : item.inference_rationale_digest,
      inference_evidence_digests: derived ? null : item.inference_evidence_digests,
      retained: derived ? false : item.retained,
      valid: derived ? false : item.valid,
    };
  });
  return immutable({ ...ledger, fields });
}

function normalizeActionAttempt(attempt, sequence) {
  assertRecord(attempt, 'action');
  assertExactKeys(attempt, new Set([
    'action_id',
    'action',
    'field_id',
    'observation_id',
    'ref',
    'outcome',
    'retry_of',
    'error_code',
  ]), 'action');
  assertRequiredKeys(attempt, ['action', 'observation_id'], 'action');
  const actionId = attempt.action_id ?? `action-${sequence}`;
  assertString(actionId, 'action.action_id', { identifier: true });
  assertString(attempt.action, 'action.action', { identifier: true });
  if (!ACTIONS.has(attempt.action)) fail('action.action', 'invalid action');
  const fieldId = attempt.field_id ?? null;
  assertString(fieldId, 'action.field_id', { identifier: true, nullable: true });
  assertString(attempt.observation_id, 'action.observation_id', { identifier: true });
  const ref = attempt.ref ?? null;
  assertString(ref, 'action.ref', { identifier: true, nullable: true });
  const outcome = attempt.outcome ?? 'attempted';
  assertString(outcome, 'action.outcome', { identifier: true });
  if (!ACTION_OUTCOMES.has(outcome)) fail('action.outcome', 'invalid action outcome');
  if (attempt.action === 'final_submit' && outcome !== 'attempted') {
    fail('action.outcome', 'final_submit must begin as attempted and resolve through resolveFinalSubmitAttempt');
  }
  const retryOf = attempt.retry_of ?? null;
  if (retryOf !== null) assertInteger(retryOf, 'action.retry_of', { minimum: 0 });
  const errorCode = attempt.error_code ?? null;
  assertString(errorCode, 'action.error_code', { identifier: true, nullable: true });
  const normalized = {
    action_id: actionId,
    action: attempt.action,
    field_id: fieldId,
    observation_id: attempt.observation_id,
    ref,
    outcome,
    retry_of: retryOf,
    error_code: errorCode,
  };
  validateActionBindingShape(normalized, 'action');
  return normalized;
}

function validateActionBinding(ledger, normalized) {
  let boundField = null;
  if (isFieldMutationAction(normalized.action)) {
    boundField = ledger.fields.find((item) => item.field_id === normalized.field_id) ?? null;
    if (boundField === null) fail('action.field_id', 'unknown field identity', 'UNKNOWN_FIELD');
    if (!boundField.present_in_latest_observation || !boundField.reachable ||
        boundField.latest_observation_id !== ledger.latest_observation_id ||
        boundField.latest_ref !== normalized.ref ||
        !boundField.ref_history.some((item) =>
          item.observation_id === normalized.observation_id && item.ref === normalized.ref)) {
      throw new Phase1StaleReferenceError('action is not bound to the latest reachable field control');
    }
  } else if (normalized.action === NON_FINAL_NAVIGATION_ACTION) {
    const candidate = ledger.current_candidate_refs.find((item) =>
      item.observation_id === normalized.observation_id && item.ref === normalized.ref);
    if (!candidate) throw new Phase1StaleReferenceError('navigation candidate ref is not current');
    if (candidate.class !== NON_FINAL_NAVIGATION_ACTION) {
      throw new Phase1StaleReferenceError('navigation target is not a non-final candidate');
    }
  }
  return boundField;
}

function appendNormalizedActionAttempt(ledger, normalized, { allowConsumed = false } = {}) {
  if (ledger.action_attempts.some((item) => item.action_id === normalized.action_id)) {
    fail('action.action_id', 'duplicate action identity');
  }
  if (normalized.action === 'final_submit' && ledger.action_attempts.some(
    (item) => item.action === 'final_submit' && item.outcome === 'attempted',
  )) {
    fail('action.action', 'a final-submit attempt is already pending', 'PENDING_FINAL_SUBMIT');
  }

  if (normalized.observation_id !== ledger.latest_observation_id) {
    throw new Phase1StaleReferenceError('action observation is not current');
  }
  if (!allowConsumed && isFieldMutationAction(normalized.action) && requiresReobservation(ledger)) {
    throw new Phase1StaleReferenceError('latest observation was consumed by a field mutation; reobserve first');
  }

  validateActionBinding(ledger, normalized);

  const action = { ...normalized, stale_ref: false };
  const shouldInvalidate = isFieldMutationAction(normalized.action);
  const fields = shouldInvalidate
    ? ledger.fields.map((item) => {
      if (item.field_id !== normalized.field_id) return item;
      const note = normalized.error_code ?? null;
      return {
        ...item,
        retained: false,
        valid: false,
        retry_notes: note === null || item.retry_notes.includes(note)
          ? [...item.retry_notes]
          : [...item.retry_notes, note],
      };
    })
    : ledger.fields;
  const submit = normalized.action === 'final_submit';
  return immutable({
    ...clone(ledger),
    fields,
    action_attempts: [...ledger.action_attempts, action],
    submit_action_count: ledger.submit_action_count + (submit ? 1 : 0),
  });
}

export function recordActionAttempt(ledger, attempt) {
  validateLedgerShape(ledger);
  const normalized = normalizeActionAttempt(attempt, ledger.action_attempts.length + 1);
  return appendNormalizedActionAttempt(ledger, normalized);
}

const BATCH_SUCCESS_OUTCOMES = new Set(['succeeded']);
const BATCH_TERMINAL_STOP_OUTCOMES = new Set(['attempted', 'failed', 'retry', 'blocked', 'stale']);

function validateBatchAttempt(ledger, normalized, index, finalIndex, fieldIds, refs, actionIds) {
  const path = `attempts[${index}]`;
  if (actionIds.has(normalized.action_id)) {
    fail(`${path}.action_id`, 'duplicate action identity');
  }
  actionIds.add(normalized.action_id);

  if (normalized.observation_id !== ledger.latest_observation_id) {
    throw new Phase1StaleReferenceError(`${path}.observation_id is not current`);
  }
  if (normalized.action !== 'fill') {
    fail(`${path}.action`, 'batch accepts only routine fill actions');
  }

  const succeeded = BATCH_SUCCESS_OUTCOMES.has(normalized.outcome);
  const terminalStop = BATCH_TERMINAL_STOP_OUTCOMES.has(normalized.outcome);
  if (!succeeded && !(terminalStop && index === finalIndex)) {
    fail(`${path}.outcome`, 'batch actions must succeed before an optional terminal non-success');
  }
  if (succeeded && normalized.error_code !== null) {
    fail(`${path}.outcome`, 'successful batch fills cannot carry error semantics');
  }
  if (terminalStop && normalized.outcome !== 'attempted' && normalized.error_code === null) {
    fail(`${path}.error_code`, 'terminal failed/retry/blocked/stale batch fill requires error_code');
  }

  const field = validateActionBinding(ledger, normalized);
  if (field === null || field.final) {
    fail(`${path}.field_id`, 'batch fill must target a current non-final field');
  }
  if (fieldIds.has(normalized.field_id)) {
    fail(`${path}.field_id`, 'batch fields must be distinct');
  }
  if (refs.has(normalized.ref)) {
    fail(`${path}.ref`, 'batch refs must be distinct');
  }
  fieldIds.add(normalized.field_id);
  refs.add(normalized.ref);
}

export function recordActionBatch(ledger, attempts) {
  validateLedgerShape(ledger);
  assertArray(attempts, 'attempts');
  if (attempts.length < 2 || attempts.length > 3) {
    fail('attempts', 'batch must contain 2-3 actions');
  }
  if (requiresReobservation(ledger)) {
    throw new Phase1StaleReferenceError('latest observation was consumed by a field mutation; reobserve first');
  }

  const normalizedAttempts = attempts.map((attempt, index) =>
    normalizeActionAttempt(attempt, ledger.action_attempts.length + index + 1));
  const fieldIds = new Set();
  const refs = new Set();
  const actionIds = new Set(ledger.action_attempts.map((action) => action.action_id));
  const finalIndex = normalizedAttempts.length - 1;
  normalizedAttempts.forEach((normalized, index) => {
    validateBatchAttempt(ledger, normalized, index, finalIndex, fieldIds, refs, actionIds);
  });

  let nextLedger = ledger;
  for (const normalized of normalizedAttempts) {
    nextLedger = appendNormalizedActionAttempt(nextLedger, normalized, { allowConsumed: true });
  }
  return nextLedger;
}
function normalizeFinalSubmitResolution(resolution) {
  assertRecord(resolution, 'resolution');
  assertExactKeys(resolution, new Set(['action_id', 'outcome', 'error_code']), 'resolution');
  assertRequiredKeys(resolution, ['action_id', 'outcome'], 'resolution');
  assertString(resolution.action_id, 'resolution.action_id', { identifier: true });
  assertString(resolution.outcome, 'resolution.outcome', { identifier: true });
  if (!FINAL_SUBMIT_TERMINAL_OUTCOMES.has(resolution.outcome)) {
    fail('resolution.outcome', 'final-submit resolution must be terminal');
  }
  const errorCode = resolution.error_code ?? null;
  assertString(errorCode, 'resolution.error_code', { identifier: true, nullable: true });
  return { action_id: resolution.action_id, outcome: resolution.outcome, error_code: errorCode };
}

export function resolveFinalSubmitAttempt(ledger, resolution) {
  validateLedgerShape(ledger);
  const normalized = normalizeFinalSubmitResolution(resolution);
  const matching = ledger.action_attempts.filter((action) => action.action_id === normalized.action_id);
  if (matching.length !== 1) {
    fail('resolution.action_id', 'unknown final-submit attempt', 'UNKNOWN_ACTION');
  }
  const [attempt] = matching;
  if (attempt.action !== 'final_submit') {
    fail('resolution.action_id', 'action is not a final-submit attempt', 'UNKNOWN_ACTION');
  }
  if (attempt.outcome !== 'attempted') {
    fail('resolution.action_id', 'final-submit attempt is already resolved (duplicate result)', 'ALREADY_RESOLVED');
  }
  const action_attempts = ledger.action_attempts.map((action) => action.action_id === normalized.action_id
    ? { ...action, outcome: normalized.outcome, error_code: normalized.error_code }
    : action);
  return immutable({ ...clone(ledger), action_attempts });
}


function canonicalValue(value) {
  validateValue(value, 'value');
  return JSON.stringify(value);
}

export function digestPrivateValue(value) {
  return createHash('sha256').update(canonicalValue(value), 'utf8').digest('hex');
}

export function digestObservedValue(control, value) {
  const normalized = control?.role === 'radio' && typeof value === 'string'
    ? value.trim().toLowerCase()
    : value;
  return digestPrivateValue(normalized);
}

const RETENTION_PROOF_KEYS = new Set(['value_digest', 'action_id', 'file_name']);

function normalizeProofs(proofs) {
  if (proofs === undefined || proofs === null) return new Map();
  assertRecord(proofs, 'retention_proofs');
  const map = new Map();
  for (const [fieldId, proof] of Object.entries(proofs)) {
    assertString(fieldId, 'retention_proofs field id', { identifier: true });
    assertRecord(proof, `retention_proofs.${fieldId}`);
    assertExactKeys(proof, RETENTION_PROOF_KEYS, `retention_proofs.${fieldId}`);
    assertRequiredKeys(proof, [...RETENTION_PROOF_KEYS], `retention_proofs.${fieldId}`);
    validateDigest(proof.value_digest, `retention_proofs.${fieldId}.value_digest`, false);
    assertString(proof.action_id, `retention_proofs.${fieldId}.action_id`, { identifier: true });
    assertBasename(proof.file_name, `retention_proofs.${fieldId}.file_name`);
    map.set(fieldId, {
      value_digest: proof.value_digest,
      action_id: proof.action_id,
      file_name: proof.file_name,
    });
  }
  return map;
}

function isFileControl(control) {
  return control.type === 'file';
}

function normalizedChoiceText(value) {
  return String(value ?? '')
    .toLowerCase()
    .replace(/[_-]+/g, ' ')
    .replace(/\s+/g, ' ')
    .trim();
}

function optionSupportsSemanticChoice(option, choice) {
  if (!option.selected) return false;
  const text = normalizedChoiceText(`${option.value ?? ''} ${option.label ?? ''}`);
  const expected = normalizedChoiceText(choice);
  if (text === expected || text.includes(expected)) return true;
  const aliases = {
    not_applicable: ['not applicable', 'n/a', 'not relevant'],
    prefer_not_to_answer: ['prefer not to answer', 'prefer not', 'decline'],
    decline: ['decline', 'prefer not to answer'],
    none: ['none', 'no selection'],
    blank: ['blank', 'no answer'],
  };
  return aliases[choice]?.some((alias) => text.includes(alias)) ?? false;
}

function blankStateIsSupported(field, control) {
  if (!['memory', 'profile', 'agent_inference', 'user'].includes(field.answer_source)) return false;
  if (!SEMANTIC_CHOICES.has(field.semantic_choice)) return false;
  const controlKinds = [control.kind, control.type, control.role]
    .filter((value) => typeof value === 'string')
    .map(normalizedChoiceText)
    .join(' ');
  const uncheckedChoice = /checkbox|radio|switch/.test(controlKinds) &&
    control.checked === false && control.value_present === false;
  if (uncheckedChoice) return true;
  const emptyOptional = field.semantic_choice === 'blank' &&
    control.required === false &&
    control.value_present === false &&
    (!control.file || control.file.count === 0);
  if (emptyOptional) return true;
  return control.options.some((option) => optionSupportsSemanticChoice(option, field.semantic_choice));
}



function uploadActionProvesCurrentField(ledger, field, proof) {
  const action = ledger.action_attempts.find((item) => item.action_id === proof.action_id);
  if (!action || action.action !== 'upload' || action.outcome !== 'succeeded' ||
      action.stale_ref || action.field_id !== field.field_id || action.ref === null) {
    return false;
  }
  return field.ref_history.some((item) =>
    item.observation_id === action.observation_id && item.ref === action.ref);
}

function retentionResultFor(field, control, proof, ledger, pendingMutation) {
  const errors = [];
  if (field.latest_ref !== control.ref) errors.push('stale-reference');
  const valid = control.validity.valid && !control.validity.aria_invalid;
  if (!valid) errors.push('validation-error');
  let retained = false;

  if (pendingMutation) {
    errors.push('pending-mutation');
  } else if (field.answer_state === 'blank') {
    retained = blankStateIsSupported(field, control);
    if (!retained) errors.push('blank-not-deliberate');
  } else if (field.answer_state === 'answered') {
    if (isFileControl(control)) {
      if (proof === undefined) {
        errors.push('proof-required');
      } else if (!control.file || control.file.count <= 0 ||
                 !control.file.names.includes(proof.file_name) ||
                 proof.value_digest !== field.value_digest ||
                 !uploadActionProvesCurrentField(ledger, field, proof)) {
        errors.push('invalid-proof');
      } else {
        retained = true;
      }
    } else if (proof !== undefined) {
      errors.push('proof-not-allowed');
    } else {
      retained = control.value_present && digestObservedValue(control, control.value) === field.value_digest;
    }
    if (!retained && !errors.includes('proof-required') && !errors.includes('invalid-proof') &&
        !errors.includes('proof-not-allowed')) {
      errors.push('value-not-retained');
    }
  }
  return { retained, valid: pendingMutation ? false : valid, errors };
}

function hasPendingMutation(ledger, field, observationId) {
  return ledger.action_attempts.some((action) =>
    action.field_id === field.field_id &&
    action.observation_id === observationId &&
    isFieldMutationAction(action.action) &&
    !action.stale_ref);
}

export function requiresReobservation(ledger) {
  validateLedgerShape(ledger);
  return observationHasFieldMutation(ledger);
}

export function verifyRetention(ledger, observation, retentionProofs = undefined) {
  validateLedgerShape(ledger);
  validateObservation(observation);
  const proofs = normalizeProofs(retentionProofs);
  const previousObservationId = ledger.observation_ids.length > 1
    ? ledger.observation_ids.at(-2)
    : null;
  if (ledger.latest_observation_id !== observation.observation_id ||
      observation.previous_observation_id !== previousObservationId) {
    return immutable({
      ledger: clone(ledger),
      ok: false,
      retry_required: true,
      errors: [{ code: 'STALE_OBSERVATION', message: 'retention observation is not the latest chained observation' }],
      retry_notes: ['stale-observation'],
    });
  }

  const controls = observationControlMap(observation);
  const errors = [];
  const retryNotes = [];
  for (const [fieldId, proof] of proofs) {
    const field = ledger.fields.find((item) => item.field_id === fieldId);
    const control = controls.get(fieldId);
    if (!field || !control || field.answer_state !== 'answered' || !isFileControl(control)) {
      errors.push({
        code: 'INVALID_PROOF',
        field_id: fieldId,
        message: 'retention proofs are restricted to answered current file controls',
      });
      retryNotes.push(`${fieldId}:invalid-proof`);
    }
  }

  const fields = ledger.fields.map((field) => {
    if (hasPendingMutation(ledger, field, observation.observation_id)) {
      errors.push({
        code: 'MUTATION_PENDING',
        field_id: field.field_id,
        message: 'pending-mutation',
      });
      retryNotes.push(`${field.field_id}:pending-mutation`);
      return {
        ...field,
        retained: false,
        valid: false,
        retry_notes: field.retry_notes.includes('pending-mutation')
          ? [...field.retry_notes]
          : [...field.retry_notes, 'pending-mutation'],
      };
    }
    if (field.answer_state === 'unresolved' || !field.reachable || !field.present_in_latest_observation) {
      if (isLedgerFileField(field) && field.answer_state === 'answered' &&
          !field.present_in_latest_observation) {
        errors.push({
          code: 'VALUE_NOT_RETAINED',
          field_id: field.field_id,
          message: 'answered file control is no longer present',
        });
        retryNotes.push(`${field.field_id}:value-not-retained`);
        return { ...field, retained: false, valid: false };
      }
      return field;
    }
    const control = controls.get(field.field_id);
    if (!control || control.ref !== field.latest_ref) {
      const issue = { code: 'STALE_REFERENCE', field_id: field.field_id, message: 'field ref is not current' };
      errors.push(issue);
      retryNotes.push(`${field.field_id}:stale-reference`);
      return {
        ...field,
        retained: false,
        valid: false,
        retry_notes: field.retry_notes.includes('stale-reference')
          ? [...field.retry_notes]
          : [...field.retry_notes, 'stale-reference'],
      };
    }
    const result = retentionResultFor(
      field,
      control,
      proofs.get(field.field_id),
      ledger,
      hasPendingMutation(ledger, field, observation.observation_id),
    );
    for (const code of result.errors) {
      const mappedCode = code === 'stale-reference'
        ? 'STALE_REFERENCE'
        : code === 'validation-error'
          ? 'INVALID_FIELD'
          : code === 'pending-mutation'
            ? 'MUTATION_PENDING'
            : code === 'proof-required' || code === 'invalid-proof' || code === 'proof-not-allowed'
              ? 'INVALID_PROOF'
              : 'VALUE_NOT_RETAINED';
      errors.push({
        code: mappedCode,
        field_id: field.field_id,
        message: code,
      });
      retryNotes.push(`${field.field_id}:${code}`);
    }
    const notes = result.errors.filter((code) => !field.retry_notes.includes(code));
    return {
      ...field,
      retained: result.retained,
      valid: result.valid,
      retry_notes: notes.length > 0 ? [...field.retry_notes, ...notes] : [...field.retry_notes],
    };
  });
  const nextLedger = immutable({ ...clone(ledger), fields });
  return immutable({
    ledger: nextLedger,
    ok: errors.length === 0,
    retry_required: errors.length > 0,
    errors,
    retry_notes: retryNotes,
  });
}

export function isReachableFieldControl(control) {
  validateControl(control, 'control');
  return isReachableControl(control);
}

export function isHoneypotControl(control) {
  validateControl(control, 'control');
  return isHoneypot(control);
}

export function semanticChoiceIsDeliberate(value) {
  return typeof value === 'string' && SEMANTIC_CHOICES.has(value);
}

export function answerSourceIsAllowed(value) {
  return typeof value === 'string' && ANSWER_SOURCE_SET.has(value);
}
