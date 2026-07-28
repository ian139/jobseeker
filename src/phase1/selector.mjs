const FIELD_POLICY_VALUES = Object.freeze([
  'subjective',
  'qualification',
  'legal',
  'demographic',
  'identity',
  'hard_fact',
]);

const SUBJECTIVE_ANSWER_SOURCES = Object.freeze([
  'exact_memory',
  'profile_evidence',
  'resume_evidence',
  'supported_inference',
  'best_effort_inference',
]);
const PROTECTED_ANSWER_SOURCES = Object.freeze([
  'exact_memory',
  'configured_default',
  'configured_decline',
  'require_user',
]);
const RESUME_ANSWER_SOURCES = Object.freeze(['resume_evidence']);
const EMPTY_ARRAY = Object.freeze([]);
const NORMALIZED_ACTIONS = Object.freeze([
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

const POLICY_SET = new Set(FIELD_POLICY_VALUES);
const SUBJECTIVE_POLICY_SET = new Set(['subjective', 'qualification']);
const REJECTED_OUTCOMES = new Set([
  'failed',
  'blocked',
  'retry',
  'rejected',
  'invalid',
]);
const PENDING_OR_SUCCESSFUL_OUTCOMES = new Set(['attempted', 'succeeded']);
const FIELD_CANDIDATE = 'field';
const CONTINUATION_CANDIDATE = 'non_final_navigation';
const FINAL_CANDIDATE = 'final_candidate';
const UNSET = Symbol('unset');
const SAFE_BATCH_DEFAULT_SIZE = 3;
const ORDINARY_TEXT_TYPES = new Set([
  'text',
  'email',
  'tel',
  'url',
  'search',
  'number',
  'date',
  'datetime-local',
  'month',
  'week',
  'time',
]);
const DEPENDENCY_KEYS = Object.freeze([
  'dependency',
  'dependencies',
  'dependencyMarked',
  'dependency_marked',
  'dependent',
  'dependsOn',
  'depends_on',
  'revealedBy',
  'revealed_by',
  'conditionalOn',
  'conditional_on',
  'requiresField',
  'requires_field',
  'blockedBy',
  'blocked_by',
]);
const RETRY_KEYS = Object.freeze([
  'retry',
  'retryRequired',
  'retry_required',
  'needsRetry',
  'needs_retry',
  'requiresRetry',
  'requires_retry',
]);
const CUSTOM_WIDGET_KEYS = Object.freeze([
  'custom',
  'customWidget',
  'custom_widget',
  'isCustom',
  'is_custom',
  'widget',
  'widgetType',
  'widget_type',
]);
const MODEL_TIERS = new Set(['cheap', 'standard', 'strong', 'highest']);

const KEY_ALIASES = Object.freeze({
  observationId: ['observationId', 'observation_id'],
  previousObservationId: ['previousObservationId', 'previous_observation_id'],
  stableId: ['stableId', 'stable_id', 'fieldId', 'field_id'],
  fieldId: ['fieldId', 'field_id', 'stableId', 'stable_id'],
  ref: ['controlReference', 'control_reference', 'ref', 'reference'],
  latestRef: ['latestRef', 'latest_ref'],
  latestObservationId: ['latestObservationId', 'latest_observation_id'],
  candidateClass: ['candidateClass', 'candidate_class'],
  answerState: ['answerState', 'answer_state'],
  answerSource: ['answerSource', 'answer_source'],
  latestState: ['latestState', 'latest_state'],
  retained: ['retained'],
  valid: ['valid'],
  required: ['required'],
  visible: ['visible'],
  enabled: ['enabled'],
  disabled: ['disabled'],
  fieldPolicy: ['fieldPolicy', 'field_policy', 'policy', 'policyClass', 'policy_class'],
});

export const FIELD_POLICIES = FIELD_POLICY_VALUES;
export const SELECTOR_ACTIONS = NORMALIZED_ACTIONS;
export const ANSWER_SOURCES_BY_POLICY = Object.freeze({
  subjective: SUBJECTIVE_ANSWER_SOURCES,
  qualification: SUBJECTIVE_ANSWER_SOURCES,
  legal: PROTECTED_ANSWER_SOURCES,
  demographic: PROTECTED_ANSWER_SOURCES,
  identity: PROTECTED_ANSWER_SOURCES,
  hard_fact: PROTECTED_ANSWER_SOURCES,
});

export class SelectorError extends TypeError {
  constructor(message, code = 'E_SELECTOR_INPUT') {
    super(`${code}: ${message}`);
    this.name = 'SelectorError';
    this.code = code;
  }
}

function isRecord(value) {
  return value !== null && typeof value === 'object' && !Array.isArray(value);
}

function own(value, key) {
  return isRecord(value) && Object.prototype.hasOwnProperty.call(value, key);
}

function firstDefined(value, keys) {
  if (!isRecord(value)) return undefined;
  for (const key of keys) {
    if (own(value, key) && value[key] !== undefined) return value[key];
  }
  return undefined;
}

function firstPresent(value, keys) {
  if (!isRecord(value)) return UNSET;
  for (const key of keys) {
    if (own(value, key)) return value[key];
  }
  return UNSET;
}

function aliasValue(value, alias) {
  return firstDefined(value, KEY_ALIASES[alias] ?? [alias]);
}

function clone(value) {
  if (Array.isArray(value)) return value.map(clone);
  if (isRecord(value)) {
    const copy = {};
    for (const [key, item] of Object.entries(value)) copy[key] = clone(item);
    return copy;
  }
  return value;
}

function freeze(value) {
  if (Array.isArray(value) || isRecord(value)) {
    for (const item of Object.values(value)) freeze(item);
    Object.freeze(value);
  }
  return value;
}

function immutable(value) {
  return freeze(clone(value));
}

function nonEmptyString(value) {
  return typeof value === 'string' && value.length > 0 ? value : null;
}

function normalizedText(value) {
  if (value === null || value === undefined) return '';
  if (typeof value === 'string') return value.toLowerCase();
  if (typeof value === 'number' || typeof value === 'boolean') return String(value).toLowerCase();
  return '';
}

function canonicalPolicy(value) {
  if (typeof value !== 'string') return null;
  const normalized = value.trim().toLowerCase().replace(/[-\s]+/g, '_');
  if (POLICY_SET.has(normalized)) return normalized;
  if (normalized === 'hardfact' || normalized === 'hard_factual' || normalized === 'factual') return 'hard_fact';
  return null;
}

function modelTier(value) {
  const normalized = normalizedText(value).replace(/[-\s]+/g, '_');
  if (normalized === 'highest_inference' || normalized === 'highest_tier') return 'highest';
  if (MODEL_TIERS.has(normalized)) return normalized;
  return 'cheap';
}

function configForField(container, field) {
  if (container === undefined || container === null) return undefined;
  const fieldId = aliasValue(field, 'fieldId');
  const label = firstDefined(field, ['label', 'question', 'text', 'description']);
  const name = firstDefined(field, ['name', 'key']);
  const keys = [fieldId, label, name].filter((key) => typeof key === 'string' && key.length > 0);

  if (container instanceof Map) {
    for (const key of keys) {
      if (container.has(key)) return container.get(key);
    }
    return undefined;
  }
  if (Array.isArray(container)) {
    for (const item of container) {
      if (!isRecord(item)) continue;
      const itemKey = firstDefined(item, [
        'fieldId',
        'field_id',
        'stableId',
        'stable_id',
        'alias',
        'label',
        'name',
        'question',
      ]);
      if (keys.some((key) => key === itemKey)) return item;
    }
    return undefined;
  }
  if (!isRecord(container)) return undefined;
  for (const key of keys) {
    if (own(container, key)) return container[key];
  }
  return undefined;
}

function unwrapConfiguredValue(value) {
  if (isRecord(value) && own(value, 'value')) return { found: true, value: value.value };
  return { found: true, value };
}

function lookupRecordValue(container, field) {
  const direct = configForField(container, field);
  if (direct !== undefined) return unwrapConfiguredValue(direct);

  if (!Array.isArray(container)) return { found: false, value: null };
  const fieldId = aliasValue(field, 'fieldId');
  const label = firstDefined(field, ['label', 'question', 'text', 'description']);
  const name = firstDefined(field, ['name', 'key']);
  const aliases = [fieldId, label, name].filter((item) => typeof item === 'string' && item.length > 0);
  for (const item of container) {
    if (!isRecord(item)) continue;
    const itemAlias = firstDefined(item, [
      'alias',
      'fieldId',
      'field_id',
      'stableId',
      'stable_id',
      'label',
      'name',
      'question',
    ]);
    if (aliases.includes(itemAlias)) {
      const value = firstPresent(item, ['value', 'answer', 'default', 'configuredDefault', 'configured_default']);
      return value === UNSET ? { found: false, value: null } : unwrapConfiguredValue(value);
    }
  }
  return { found: false, value: null };
}

function fieldPolicyConfig(field, options) {
  const configured = configForField(
    firstDefined(options, ['fieldPolicies', 'field_policies']),
    field,
  );
  if (typeof configured === 'string') return { policy: canonicalPolicy(configured) };
  return isRecord(configured) ? configured : {};
}

function textForField(field) {
  const values = [
    'label',
    'name',
    'description',
    'question',
    'text',
    'ariaLabel',
    'aria_label',
    'kind',
    'type',
    'role',
  ];
  return values.map((key) => normalizedText(field[key])).filter(Boolean).join(' ');
}

export function classifyFieldPolicy(field, options = {}) {
  if (!isRecord(field)) throw new SelectorError('field must be an object');
  if (options !== undefined && options !== null && !isRecord(options)) {
    throw new SelectorError('field policy options must be an object');
  }

  const configured = fieldPolicyConfig(field, options);
  const configuredPolicy = canonicalPolicy(
    firstDefined(configured, KEY_ALIASES.fieldPolicy) ??
    firstDefined(options, ['fieldPolicy', 'field_policy', 'policy', 'policyClass', 'policy_class']),
  );
  if (configuredPolicy !== null) return configuredPolicy;

  const explicit = canonicalPolicy(firstDefined(field, KEY_ALIASES.fieldPolicy));
  if (explicit !== null) return explicit;

  const text = textForField(field);
  const sensitive = field.sensitive === true || field.isSensitive === true || field.is_sensitive === true;
  const hardFact = field.hardFact === true || field.hard_fact === true || field.factual === true;

  if (/\b(?:race|ethnic(?:ity)?|gender|sex|sexual orientation|pronoun|disability|disabled|veteran|demographic|diversity|lgbtq)\b/.test(text)) {
    return 'demographic';
  }
  if (/(?:work authorization|authorized to work|right to work|sponsor(?:ship)?|visa|citizen(?:ship)?|immigration|criminal|conviction|felon(?:y)?|background check|security clearance|export control|legally eligible)/.test(text)) {
    return 'legal';
  }
  if (/\b(?:first|last|full|preferred)?\s*(?:name|email|e-mail|phone|telephone|mobile|address|street|city|state|province|zip|postal|country|location|residence|linkedin|github|portfolio|website|contact)\b/.test(text)) {
    return 'identity';
  }
  if (/(?:date of birth|birth date|graduat|gpa|grade point|degree|school|university|college|certif|license|clearance|credential|start date|end date|availability date|relocat|salary|compensation|pay rate|hourly rate)/.test(text)) {
    return 'hard_fact';
  }
  if (/(?:why|motiv(?:at|ation)|interest|passion|hobby|obsession|favorite|tell us|describe|essay|proud|achievement|challenge|strength|weakness|excite|culture|ideal|goal|anything else|additional information|cover letter)/.test(text)) {
    return 'subjective';
  }
  if (/(?:experience|skill|proficien|familiar|technical|program(?:ming)?|language|framework|tool|stack|database|cloud|project|qualification|years?\s+(?:of\s+)?experience|availability|work history)/.test(text)) {
    return 'qualification';
  }
  if (hardFact || sensitive) return 'hard_fact';
  return 'hard_fact';
}

function isFileControl(control) {
  const file = firstDefined(control, ['file']);
  const kind = normalizedText(firstDefined(control, ['kind', 'controlType', 'control_type']));
  const type = normalizedText(firstDefined(control, ['type', 'inputType', 'input_type']));
  const role = normalizedText(firstDefined(control, ['role']));
  return (isRecord(file) || file !== undefined && file !== null) ||
    kind.includes('file') || type === 'file' || role.includes('file');
}

function filePresent(control, field) {
  const file = firstDefined(control, ['file']);
  if (isRecord(file)) {
    const count = firstDefined(file, ['count']);
    if (typeof count === 'number') return count > 0;
    if (file.present === true || file.present === 'true') return true;
    if (Array.isArray(file.names)) return file.names.length > 0;
  }
  const state = firstDefined(field, KEY_ALIASES.latestState);
  if (isRecord(state) && isRecord(state.file)) {
    if (state.file.present === true || (typeof state.file.count === 'number' && state.file.count > 0)) return true;
  }
  return false;
}

function valuePresent(control, field) {
  const state = firstDefined(field, KEY_ALIASES.latestState);
  if (firstDefined(field, ['value_present', 'valuePresent']) === true) return true;
  if (isRecord(state) && state.value_present === true) return true;
  if (firstDefined(control, ['value_present', 'valuePresent']) === true) return true;
  if (firstDefined(control, ['checked']) === true) return true;
  const selected = firstDefined(control, ['selected']);
  if (Array.isArray(selected) && selected.length > 0) return true;
  if (selected !== null && selected !== undefined && selected !== false && !Array.isArray(selected)) return true;
  if (filePresent(control, field)) return true;
  return false;
}

function validityFor(control, field) {
  const fieldState = firstDefined(field, KEY_ALIASES.latestState);
  const validity = firstDefined(field, ['validity']) ??
    (isRecord(fieldState) ? fieldState.validity : undefined) ??
    firstDefined(control, ['validity']);
  return isRecord(validity) ? validity : {};
}

function hasInvalidValidity(control, field) {
  const validity = validityFor(control, field);
  return validity.valid === false || validity.aria_invalid === true || validity.ariaInvalid === true;
}

function hasExplicitBoolean(value, keys, expected) {
  const candidate = firstDefined(value, keys);
  return typeof candidate === 'boolean' && candidate === expected;
}

function answerStateFor(control, field) {
  const state = aliasValue(field, 'answerState');
  if (typeof state === 'string') return state.toLowerCase();
  if (field && (field.value_digest !== undefined || field.valueDigest !== undefined)) return 'answered';
  if (valuePresent(control, field)) return 'answered';
  return 'unresolved';
}

function hasCompletedValue(control, field, answerState) {
  return answerState === 'answered' || answerState === 'blank' ||
    field.retained === true ||
    field.value_digest !== undefined && field.value_digest !== null ||
    field.valueDigest !== undefined && field.valueDigest !== null ||
    valuePresent(control, field);
}

function actionAttemptsFor(ledger, fieldId, observationId, ref) {
  const attempts = Array.isArray(ledger?.action_attempts)
    ? ledger.action_attempts
    : Array.isArray(ledger?.actionAttempts) ? ledger.actionAttempts : [];
  return attempts.filter((attempt) => {
    if (!isRecord(attempt)) return false;
    const attemptFieldId = aliasValue(attempt, 'fieldId');
    const attemptObservationId = aliasValue(attempt, 'observationId');
    const attemptRef = aliasValue(attempt, 'ref');
    if (attemptFieldId !== fieldId || attemptObservationId !== observationId || attemptRef !== ref) return false;
    return attempt.stale_ref !== true && attempt.staleRef !== true;
  });
}

function isRejectedField(control, field, attempts, answerState) {
  const explicit = firstDefined(field, [
    'invalid',
    'rejected',
    'validationError',
    'validation_error',
    'rejectedAnswer',
    'rejected_answer',
  ]);
  if (explicit === true || hasInvalidValidity(control, field)) return true;
  const completed = hasCompletedValue(control, field, answerState);
  if (field.valid === false && completed && answerState !== 'unresolved') return true;
  if (completed && attempts.some((attempt) => REJECTED_OUTCOMES.has(normalizedText(attempt.outcome)))) return true;
  return false;
}

function hasPendingOrSuccessfulAction(attempts) {
  return attempts.some((attempt) => PENDING_OR_SUCCESSFUL_OUTCOMES.has(normalizedText(attempt.outcome)));
}

function isReachable(control, field) {
  const visible = aliasValue(control, 'visible');
  const enabled = aliasValue(control, 'enabled');
  const disabled = aliasValue(control, 'disabled');
  if (visible === false || field.present_in_latest_observation === false || field.presentInLatestObservation === false) return false;
  if (enabled === false || disabled === true) return false;
  if (field.reachable === false || field.reachable === undefined && control.reachable === false) return false;
  return true;
}

function currentObservationId(observation) {
  return aliasValue(observation, 'observationId');
}

function ensureRecord(value, location) {
  if (!isRecord(value)) throw new SelectorError(`${location} must be an object`);
  return value;
}

function ensureCurrentObservation(observation) {
  ensureRecord(observation, 'observation');
  const id = currentObservationId(observation);
  if (typeof id !== 'string' || id.length === 0) {
    throw new SelectorError('observation must have observation_id', 'E_SELECTOR_INPUT');
  }
  const controls = firstDefined(observation, ['controls']);
  if (!Array.isArray(controls)) throw new SelectorError('observation.controls must be an array');
  return { id, controls };
}

function ledgerFields(ledger) {
  const fields = firstDefined(ledger, ['fields']);
  if (fields === undefined) return [];
  if (!Array.isArray(fields)) throw new SelectorError('ledger.fields must be an array');
  return fields;
}

function fieldMap(fields) {
  const result = new Map();
  for (const field of fields) {
    if (!isRecord(field)) throw new SelectorError('ledger.fields entries must be objects');
    const id = aliasValue(field, 'fieldId');
    if (typeof id !== 'string' || id.length === 0) throw new SelectorError('ledger field must have field_id');
    if (result.has(id)) throw new SelectorError(`duplicate ledger field ${id}`);
    result.set(id, field);
  }
  return result;
}

function ensureLedgerFreshness(ledger, observationId) {
  if (!isRecord(ledger)) return;
  const latest = firstDefined(ledger, ['latest_observation_id', 'latestObservationId']);
  if (latest !== undefined && latest !== null && latest !== observationId) {
    throw new SelectorError('ledger does not describe the current observation', 'E_SELECTOR_STALE');
  }
  const candidateRefs = firstDefined(ledger, ['current_candidate_refs', 'currentCandidateRefs']);
  if (candidateRefs === undefined) return;
  if (!Array.isArray(candidateRefs)) throw new SelectorError('ledger.current_candidate_refs must be an array');
  for (const candidate of candidateRefs) {
    if (!isRecord(candidate)) throw new SelectorError('current candidate refs must be objects');
    const id = aliasValue(candidate, 'observationId');
    const ref = aliasValue(candidate, 'ref');
    if (id !== observationId || typeof ref !== 'string' || ref.length === 0) {
      throw new SelectorError('current candidate ref is stale', 'E_SELECTOR_STALE');
    }
  }
}

function ensureFieldFresh(field, observationId, ref) {
  if (!field) return;
  const fieldObservationId = aliasValue(field, 'latestObservationId');
  const fieldRef = aliasValue(field, 'latestRef');
  if (fieldObservationId !== undefined && fieldObservationId !== null && fieldObservationId !== observationId) {
    throw new SelectorError('field observation reference is stale', 'E_SELECTOR_STALE');
  }
  if (fieldRef !== undefined && fieldRef !== null && fieldRef !== ref) {
    throw new SelectorError('field control reference is stale', 'E_SELECTOR_STALE');
  }
}

function normalizeInput(first, second, third) {
  let observation = first;
  let ledger = second;
  let options = third;
  if (isRecord(first) && (
    own(first, 'observation') || own(first, 'currentObservation') || own(first, 'current_observation') ||
    own(first, 'pageObservation') || own(first, 'page_observation')
  )) {
    observation = first.observation ?? first.currentObservation ?? first.current_observation ??
      first.pageObservation ?? first.page_observation;
    ledger = first.ledger ?? first.applicationLedger ?? first.application_ledger;
    options = {
      ...first,
      ...(isRecord(third) ? third : {}),
    };
    delete options.observation;
    delete options.currentObservation;
    delete options.current_observation;
    delete options.pageObservation;
    delete options.page_observation;
    delete options.ledger;
    delete options.applicationLedger;
    delete options.application_ledger;
  } else if (isRecord(second) && own(second, 'ledger')) {
    ledger = second.ledger;
    options = { ...second, ...(isRecord(third) ? third : {}) };
    delete options.ledger;
  }
  if (options === undefined || options === null) options = {};
  if (!isRecord(options)) throw new SelectorError('selector options must be an object');
  return { observation, ledger: ledger ?? {}, options };
}

function isNewlyRevealed(field, ledger, observationId) {
  const last = firstDefined(field, ['last_revealed_observation_id', 'lastRevealedObservationId']);
  const revealed = firstDefined(field, ['revealed_observation_id', 'revealedObservationId']);
  if (last === observationId) return true;
  if (revealed === observationId) {
    const ids = firstDefined(ledger, ['observation_ids', 'observationIds']);
    return !Array.isArray(ids) || ids.length > 1;
  }
  const diffs = firstDefined(ledger, ['diffs']);
  if (Array.isArray(diffs)) {
    const latest = diffs.at(-1);
    if (isRecord(latest) && firstDefined(latest, ['to_observation_id', 'toObservationId']) === observationId) {
      const added = firstDefined(latest, ['added']);
      return Array.isArray(added) && added.some((item) => aliasValue(item, 'fieldId') === aliasValue(field, 'fieldId'));
    }
  }
  return false;
}

function optionsForControl(control) {
  const options = firstDefined(control, ['options', 'allowedOptions']);
  if (!Array.isArray(options)) return [];
  const result = [];
  const seen = new Set();
  for (const option of options) {
    if (isRecord(option) && (option.disabled === true || option.isDisabled === true)) continue;
    const value = isRecord(option)
      ? firstDefined(option, ['label', 'value', 'text'])
      : option;
    if (value === null || value === undefined) continue;
    const stringValue = String(value);
    if (seen.has(stringValue)) continue;
    seen.add(stringValue);
    result.push(stringValue);
  }
  return result;
}

function actionForControl(control) {
  if (isFileControl(control)) return ['upload_file'];
  const kind = normalizedText(firstDefined(control, ['kind', 'controlType', 'control_type']));
  const type = normalizedText(firstDefined(control, ['type', 'inputType', 'input_type']));
  const role = normalizedText(firstDefined(control, ['role']));
  const tag = normalizedText(firstDefined(control, ['tag']));
  if (type === 'checkbox' || role === 'checkbox' || kind.includes('checkbox')) return ['toggle'];
  if (type === 'radio' || role === 'radio' || kind.includes('radio')) return ['select_option'];
  if (kind.includes('select') || kind.includes('combobox') || kind.includes('autocomplete') ||
      role === 'combobox' || role === 'listbox' || tag === 'select') {
    return ['select_option'];
  }
  if (kind.includes('dialog') || role === 'dialog') return ['open_dialog'];
  return ['fill_text'];
}

function candidateClass(control) {
  const candidate = firstDefined(control, ['candidate']);
  const fromCandidate = isRecord(candidate) ? firstDefined(candidate, ['class', 'candidateClass', 'candidate_class']) : undefined;
  return fromCandidate ?? firstDefined(control, ['candidateClass', 'candidate_class', 'class']) ?? null;
}

function controlLabel(control) {
  const candidate = firstDefined(control, ['candidate']);
  const reason = isRecord(candidate) ? firstDefined(candidate, ['reason']) : undefined;
  return [
    firstDefined(control, ['label', 'name', 'description', 'text', 'title']),
    reason,
  ].map(normalizedText).filter(Boolean).join(' ');
}

function pageActionKind(control) {
  const klass = candidateClass(control);
  if (klass === FINAL_CANDIDATE || control.final === true || control.isFinal === true) return 'final';
  if (klass === CONTINUATION_CANDIDATE) return 'continue';
  const text = controlLabel(control);
  const controlKind = normalizedText(firstDefined(control, ['kind', 'type', 'role', 'tag']));
  if (!/(?:button|link|submit|navigation|review|continue|next|save|proceed|finish|apply)/.test(`${controlKind} ${text}`)) return null;
  if (/(?:submit|finish|complete application|send application|finalize)/.test(text)) return 'final';
  if (/(?:continue|next|review|save|proceed|application)/.test(text)) return 'continue';
  return null;
}

function pageCandidates(controls, observationId, ledger) {
  const refs = firstDefined(ledger, ['current_candidate_refs', 'currentCandidateRefs']);
  const allowed = Array.isArray(refs) && refs.length > 0
    ? new Set(refs.filter((item) => isRecord(item) && aliasValue(item, 'observationId') === observationId)
      .map((item) => aliasValue(item, 'ref')))
    : null;
  return controls
    .filter((control) => isRecord(control) && isReachable(control, {}))
    .map((control, index) => ({ control, index, kind: pageActionKind(control) }))
    .filter((item) => item.kind !== null && (allowed === null || allowed.has(aliasValue(item.control, 'ref'))))
    .sort((left, right) => {
      const leftRef = String(aliasValue(left.control, 'ref') ?? '');
      const rightRef = String(aliasValue(right.control, 'ref') ?? '');
      if (leftRef < rightRef) return -1;
      if (leftRef > rightRef) return 1;
      const leftId = String(aliasValue(left.control, 'stableId') ?? '');
      const rightId = String(aliasValue(right.control, 'stableId') ?? '');
      if (leftId < rightId) return -1;
      if (leftId > rightId) return 1;
      return left.index - right.index;
    });
}

function lookupConfiguredValue(field, options, directKeys, containers) {
  for (const key of directKeys) {
    const direct = firstPresent(field, [key]);
    if (direct !== UNSET) return unwrapConfiguredValue(direct);
  }
  for (const container of containers) {
    const found = lookupRecordValue(container, field);
    if (found.found) return found;
  }
  return { found: false, value: null };
}

function answerAvailability(field, options) {
  const answerMemory = firstDefined(options, ['answerMemory', 'answer_memory', 'memory']);
  const defaults = firstDefined(options, ['configuredDefaults', 'configured_defaults', 'defaults']);
  const exact = lookupConfiguredValue(
    field,
    options,
    ['exactAnswer', 'exact_answer', 'exactMemory', 'exact_memory', 'memoryAnswer', 'memory_answer'],
    [answerMemory],
  );
  const fallback = lookupConfiguredValue(
    field,
    options,
    ['configuredFallback', 'configured_fallback', 'configuredDefault', 'configured_default', 'defaultAnswer', 'default_answer'],
    [defaults],
  );
  return { exact, fallback };
}

function inferableField(field, policy, config) {
  const explicit = firstDefined(field, ['inferable', 'canInfer', 'can_infer']);
  if (typeof explicit === 'boolean') return explicit;
  const configured = firstDefined(config, ['inferable', 'canInfer', 'can_infer']);
  if (typeof configured === 'boolean') return configured;
  return SUBJECTIVE_POLICY_SET.has(policy);
}

function policyMetadata(field, options, control = null) {
  const policyField = isRecord(control) ? { ...control, ...field } : field;
  const config = fieldPolicyConfig(policyField, options);
  const policy = classifyFieldPolicy(policyField, options);
  const sources = isFileControl(policyField) ? RESUME_ANSWER_SOURCES :
    (Array.isArray(config.allowedAnswerSources) ? config.allowedAnswerSources : ANSWER_SOURCES_BY_POLICY[policy]);
  return {
    config,
    policy,
    sources: [...sources],
    inferable: inferableField(policyField, policy, config),
    requiredModelTier: modelTier(firstDefined(config, ['requiredModelTier', 'required_model_tier']) ??
      firstDefined(policyField, ['requiredModelTier', 'required_model_tier'])),
    escalationPermitted: firstDefined(config, ['escalationPermitted', 'escalation_permitted']) ??
      firstDefined(policyField, ['escalationPermitted', 'escalation_permitted']) ?? SUBJECTIVE_POLICY_SET.has(policy),
  };
}

function outputForField(info, options) {
  const metadata = policyMetadata(info.field, options, info.control);
  const configured = info.availability.fallback.found ? info.availability.fallback.value : null;
  const sources = isFileControl(info.control) ? [...RESUME_ANSWER_SOURCES] : metadata.sources;
  return immutable({
    observationId: info.observationId,
    fieldId: info.fieldId,
    controlReference: info.controlReference,
    fieldPolicy: metadata.policy,
    allowedAnswerSources: sources,
    allowedActions: actionForControl(info.control),
    allowedOptions: optionsForControl(info.control),
    configuredFallback: configured,
    requiredModelTier: metadata.requiredModelTier,
    escalationPermitted: Boolean(metadata.escalationPermitted),
    reobservationRequired: true,
  });
}

function outputForPage(info, kind, options) {
  return immutable({
    observationId: info.observationId,
    fieldId: null,
    controlReference: info.controlReference,
    fieldPolicy: null,
    allowedAnswerSources: [],
    allowedActions: kind === 'final' ? ['click'] : ['navigate'],
    allowedOptions: [],
    configuredFallback: null,
    requiredModelTier: modelTier(firstDefined(options, ['requiredModelTier', 'required_model_tier'])),
    escalationPermitted: false,
    reobservationRequired: true,
  });
}

function compareInfo(left, right) {
  if (left.priority === right.priority && left.priority >= 4) {
    const leftAnswerRank = left.exact ? 0 : left.fallback ? 1 : left.inferable ? 2 : 3;
    const rightAnswerRank = right.exact ? 0 : right.fallback ? 1 : right.inferable ? 2 : 3;
    if (leftAnswerRank !== rightAnswerRank) return leftAnswerRank - rightAnswerRank;
  }
  const leftId = String(left.fieldId ?? '');
  const rightId = String(right.fieldId ?? '');
  if (leftId < rightId) return -1;
  if (leftId > rightId) return 1;
  const leftRef = String(left.controlReference ?? '');
  const rightRef = String(right.controlReference ?? '');
  if (leftRef < rightRef) return -1;
  if (leftRef > rightRef) return 1;
  return left.index - right.index;
}

function selectFieldInfos(observation, ledger, options, observationId, controls, byId) {
  const infos = [];
  for (let index = 0; index < controls.length; index += 1) {
    const control = controls[index];
    if (!isRecord(control)) throw new SelectorError(`observation.controls[${index}] must be an object`);
    const klass = candidateClass(control);
    if (klass !== null && klass !== FIELD_CANDIDATE) continue;
    if (klass === null && pageActionKind(control) !== null) continue;
    const fieldId = aliasValue(control, 'stableId');
    const controlReference = aliasValue(control, 'ref');
    if (typeof fieldId !== 'string' || fieldId.length === 0 || typeof controlReference !== 'string' || controlReference.length === 0) {
      if (klass === FIELD_CANDIDATE || klass === null) {
        throw new SelectorError(`field control at index ${index} requires stable_id and ref`);
      }
      continue;
    }
    const field = byId.get(fieldId) ?? control;
    ensureFieldFresh(field === control ? null : field, observationId, controlReference);
    if (!isReachable(control, field)) continue;
    const answerState = answerStateFor(control, field);
    const attempts = actionAttemptsFor(ledger, fieldId, observationId, controlReference);
    const rejected = isRejectedField(control, field, attempts, answerState);
    const retainedValid = field.retained === true && !rejected && field.valid !== false && !hasInvalidValidity(control, field);
    if (retainedValid) continue;
    if (!rejected && hasPendingOrSuccessfulAction(attempts)) continue;
    const required = aliasValue(field, 'required') ?? aliasValue(control, 'required') === true;
    const unresolved = !retainedValid && (
      answerState === 'unresolved' ||
      field.retained !== true ||
      field.valid === false ||
      hasInvalidValidity(control, field)
    );
    const metadata = policyMetadata(field, options, control);
    const availability = answerAvailability(field, options);
    const info = {
      observationId,
      fieldId,
      controlReference,
      field,
      control,
      index,
      required: Boolean(required),
      unresolved: Boolean(unresolved),
      rejected,
      newlyRevealed: isNewlyRevealed(field, ledger, observationId),
      upload: isFileControl(control),
      exact: availability.exact.found,
      fallback: availability.fallback.found,
      inferable: metadata.inferable,
      availability,
    };
    if (info.rejected) {
      info.priority = 0;
    } else if (info.required && info.unresolved) {
      if (info.upload) info.priority = 3;
      else if (info.newlyRevealed) info.priority = 2;
      else info.priority = 1;
    } else if (!info.required && info.unresolved) {
      if (info.exact || info.fallback) info.priority = 4;
      else if (info.inferable || info.upload) info.priority = 5;
      else info.priority = 6;
    } else {
      info.priority = null;
    }
    if (info.priority !== null) infos.push(info);
  }
  infos.sort((left, right) => left.priority - right.priority || compareInfo(left, right));
  return infos;
}
function hasMeaningfulMarker(value, keys) {
  if (!isRecord(value)) return false;
  for (const key of keys) {
    if (!own(value, key)) continue;
    const marker = value[key];
    if (marker === true) return true;
    if (typeof marker === 'string' && marker.trim().length > 0) return true;
    if (Array.isArray(marker) && marker.length > 0) return true;
    if (isRecord(marker) && Object.keys(marker).length > 0) return true;
  }
  return false;
}

function dependencyMarked(control, field) {
  return hasMeaningfulMarker(control, DEPENDENCY_KEYS) ||
    hasMeaningfulMarker(field, DEPENDENCY_KEYS);
}

function retryMarked(info, ledger) {
  const attempts = actionAttemptsFor(ledger, info.fieldId, info.observationId, info.controlReference);
  if (attempts.some((attempt) => {
    const outcome = normalizedText(attempt.outcome);
    return REJECTED_OUTCOMES.has(outcome) || outcome === 'error' || outcome === 'failure' ||
      hasMeaningfulMarker(attempt, RETRY_KEYS);
  })) return true;
  return hasMeaningfulMarker(info.control, RETRY_KEYS) ||
    hasMeaningfulMarker(info.field, RETRY_KEYS) ||
    hasMeaningfulMarker(info.field, ['retry_notes']);
}

function newlyRevealedMarked(info, observationId) {
  if (info.newlyRevealed) return true;
  const revealedObservationId = firstDefined(info.field, ['revealed_observation_id', 'revealedObservationId']);
  if (revealedObservationId === observationId) return true;
  return hasMeaningfulMarker(info.control, ['newlyRevealed', 'newly_revealed', 'revealed']) ||
    hasMeaningfulMarker(info.field, ['newlyRevealed', 'newly_revealed', 'revealed']);
}

function ordinaryTextControl(control) {
  if (!isRecord(control) || isFileControl(control)) return false;
  if (actionForControl(control).join(',') !== 'fill_text') return false;
  if (firstDefined(control, ['native']) === false ||
      hasMeaningfulMarker(control, CUSTOM_WIDGET_KEYS)) return false;
  if (optionsForControl(control).length > 0) return false;
  if (firstDefined(control, ['checked']) !== undefined && firstDefined(control, ['checked']) !== null) return false;
  if (firstDefined(control, ['selected']) !== undefined && firstDefined(control, ['selected']) !== null) return false;

  const kind = normalizedText(firstDefined(control, ['kind', 'controlType', 'control_type']));
  const tag = normalizedText(firstDefined(control, ['tag']));
  const type = normalizedText(firstDefined(control, ['type', 'inputType', 'input_type']));
  const role = normalizedText(firstDefined(control, ['role']));
  if (role !== '' && role !== 'textbox') return false;
  if (tag === 'textarea') return kind === '' || kind === 'textarea';
  if (tag === 'input') return (kind === '' || kind === 'input') &&
    (type === '' || ORDINARY_TEXT_TYPES.has(type));
  return false;
}

function safeBatchInfo(info, ledger, observationId, options) {
  if (!info || info.rejected || info.upload || !info.unresolved) return false;
  if (retryMarked(info, ledger) || newlyRevealedMarked(info, observationId)) return false;
  if (dependencyMarked(info.control, info.field)) return false;
  if (candidateClass(info.control) === CONTINUATION_CANDIDATE ||
      candidateClass(info.control) === FINAL_CANDIDATE ||
      pageActionKind(info.control) !== null) return false;
  if (!ordinaryTextControl(info.control)) return false;
  if (firstDefined(info.control, ['readonly', 'readOnly']) === true ||
      firstDefined(info.field, ['readonly', 'readOnly']) === true) return false;
  if (firstDefined(info.control, ['sensitive', 'isSensitive', 'is_sensitive']) === true ||
      firstDefined(info.field, ['sensitive', 'isSensitive', 'is_sensitive']) === true) return false;

  const metadata = policyMetadata(info.field, options, info.control);
  if (metadata.policy === 'legal' || metadata.policy === 'demographic') return false;
  return true;
}

function safeBatchLimit(options) {
  const configured = firstDefined(options, ['maxBatchSize', 'max_batch_size']);
  if (configured === undefined) return SAFE_BATCH_DEFAULT_SIZE;
  if (!Number.isInteger(configured) || configured < 1 || configured > 3) {
    throw new SelectorError('maxBatchSize must be an integer from 1 through 3');
  }
  return configured;
}
export function selectSafeApplicationBatch(first, second, third) {
  const { observation, ledger, options } = normalizeInput(first, second, third);
  const { id: observationId, controls } = ensureCurrentObservation(observation);
  ensureRecord(ledger, 'ledger');
  ensureLedgerFreshness(ledger, observationId);
  const fields = ledgerFields(ledger);
  const byId = fieldMap(fields);
  const infos = selectFieldInfos(observation, ledger, options, observationId, controls, byId);
  const limit = safeBatchLimit(options);

  if (infos.length > 0) {
    const units = [];
    for (const info of infos) {
      if (!safeBatchInfo(info, ledger, observationId, options)) {
        if (units.length === 0) {
          return immutable({
            mode: 'single',
            observationId,
            units: [outputForField(info, options)],
          });
        }
        break;
      }
      units.push(outputForField(info, options));
      if (units.length >= limit) break;
    }
    return immutable({
      mode: units.length > 1 ? 'batch' : 'single',
      observationId,
      units,
    });
  }

  const next = selectNextApplicationWork(observation, ledger, options);
  if (next === null) return null;
  return immutable({
    mode: 'single',
    observationId,
    units: [next],
  });
}




export function selectNextApplicationWork(first, second, third) {
  const { observation, ledger, options } = normalizeInput(first, second, third);
  const { id: observationId, controls } = ensureCurrentObservation(observation);
  ensureRecord(ledger, 'ledger');
  ensureLedgerFreshness(ledger, observationId);
  const fields = ledgerFields(ledger);
  const byId = fieldMap(fields);
  const infos = selectFieldInfos(observation, ledger, options, observationId, controls, byId);
  if (infos.length > 0) return outputForField(infos[0], options);

  const submissionReady = firstDefined(options, ['submissionReady', 'submission_ready']) === true;
  const pages = pageCandidates(controls, observationId, ledger);
  const continuation = pages.find((item) => item.kind === 'continue');
  for (const control of controls) {
    if (!isRecord(control) || !isReachable(control, {})) continue;
    if (pageActionKind(control) !== null) {
      const pageRef = aliasValue(control, 'ref');
      if (typeof pageRef !== 'string' || pageRef.length === 0) {
        throw new SelectorError('page action requires a current control reference', 'E_SELECTOR_INPUT');
      }
    }
  }
  if (continuation) {
    return outputForPage({
      observationId,
      controlReference: aliasValue(continuation.control, 'ref') ?? null,
    }, 'continue', options);
  }
  if (submissionReady) {
    const final = pages.find((item) => item.kind === 'final');
    if (!final) {
      throw new SelectorError('submission-ready state has no current final candidate', 'E_SELECTOR_STALE');
    }
    return outputForPage({
      observationId,
      controlReference: aliasValue(final.control, 'ref') ?? null,
    }, 'final', options);
  }
  return null;
}

export default Object.freeze({
  classifyFieldPolicy,
  selectSafeApplicationBatch,
  selectNextApplicationWork,
  SelectorError,
  FIELD_POLICIES,
  SELECTOR_ACTIONS,
});
