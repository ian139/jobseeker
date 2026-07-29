const VISUAL_OBSERVATION_SCHEMA = 'phase1-visual-observation-v1';
const FIELD_POLICIES = Object.freeze(['subjective', 'qualification', 'legal', 'demographic', 'identity', 'hard_fact']);
const ANSWER_SOURCES_BY_POLICY = Object.freeze({
  subjective: Object.freeze(['exact_memory', 'profile_evidence', 'resume_evidence', 'supported_inference', 'best_effort_inference']),
  qualification: Object.freeze(['exact_memory', 'profile_evidence', 'resume_evidence', 'supported_inference', 'best_effort_inference']),
  legal: Object.freeze(['exact_memory', 'configured_default', 'configured_decline', 'require_user']),
  demographic: Object.freeze(['exact_memory', 'configured_default', 'configured_decline', 'require_user']),
  identity: Object.freeze(['exact_memory', 'configured_default', 'configured_decline', 'require_user']),
  hard_fact: Object.freeze(['exact_memory', 'configured_default', 'configured_decline', 'require_user']),
});
const COMPUTER_ACTIONS = Object.freeze(['click', 'type_text', 'press_key', 'scroll', 'upload_file']);
const VISUAL_KINDS = Object.freeze(['text', 'textarea', 'email', 'phone', 'number', 'date', 'radio_group', 'checkbox', 'checkbox_group', 'native_select', 'custom_select', 'combobox', 'autocomplete', 'file_upload', 'button', 'link', 'dialog', 'unknown']);
const SUBJECTIVE_POLICIES = new Set(['subjective', 'qualification']);
const REJECTED_OUTCOMES = new Set(['failed', 'blocked', 'retry', 'rejected', 'invalid', 'error', 'failure']);
const PENDING_OUTCOMES = new Set(['attempted', 'succeeded']);
const ORDINARY_TEXT_KINDS = new Set(['text', 'textarea', 'email', 'phone', 'number', 'date']);
const PAGE_CLASSES = new Set(['non_final_navigation', 'final_candidate']);
const CANDIDATE_CLASSES = new Set(['field', 'non_final_navigation', 'final_candidate', 'unknown']);
const SHA256 = /^[a-f0-9]{64}$/u;
const EMPTY = Object.freeze([]);
const DEFAULT_BATCH_SIZE = 3;

export { FIELD_POLICIES, ANSWER_SOURCES_BY_POLICY, COMPUTER_ACTIONS, VISUAL_KINDS };

export class PlannerError extends TypeError {
  constructor(message, code = 'E_PLANNER_INPUT') {
    super(`${code}: ${message}`);
    this.name = 'PlannerError';
    this.code = code;
  }
}

function isRecord(value) {
  return value !== null && typeof value === 'object' && !Array.isArray(value);
}

function own(value, key) {
  return isRecord(value) && Object.prototype.hasOwnProperty.call(value, key);
}

function get(value, key, fallback = undefined) {
  return own(value, key) && value[key] !== undefined ? value[key] : fallback;
}

function text(value) {
  if (value === null || value === undefined) return '';
  if (typeof value === 'string' || typeof value === 'number' || typeof value === 'boolean') return String(value).toLowerCase();
  return '';
}

function clone(value) {
  if (Array.isArray(value)) return value.map(clone);
  if (isRecord(value)) return Object.fromEntries(Object.entries(value).map(([key, item]) => [key, clone(item)]));
  return value;
}

function freeze(value) {
  if (Array.isArray(value) || isRecord(value)) {
    for (const child of Object.values(value)) freeze(child);
    Object.freeze(value);
  }
  return value;
}

function immutable(value) {
  return freeze(clone(value));
}

function requireRecord(value, location) {
  if (!isRecord(value)) throw new PlannerError(`${location} must be an object`);
  return value;
}

function requireString(value, location) {
  if (typeof value !== 'string' || value.length === 0 || value.length > 512) throw new PlannerError(`${location} must be a non-empty string`);
  return value;
}

function targetId(target, location = 'target') {
  return requireString(get(target, 'target_id'), `${location}.target_id`);
}

function canonicalPolicy(value) {
  if (typeof value !== 'string') return null;
  const normalized = value.trim().toLowerCase().replace(/[-\s]+/gu, '_');
  if (FIELD_POLICIES.includes(normalized)) return normalized;
  if (normalized === 'hardfact' || normalized === 'hard_factual' || normalized === 'factual') return 'hard_fact';
  return null;
}

function mapValue(container, key) {
  if (container instanceof Map) return container.get(key);
  if (isRecord(container)) return container[key];
  if (Array.isArray(container)) {
    const match = container.find((item) => isRecord(item) && (item.field_id === key || item.target_id === key || item.alias === key));
    if (match === undefined) return undefined;
    return own(match, 'value') ? match.value : match;
  }
  return undefined;
}

function configuredFor(options, target, names) {
  const fieldId = get(target, 'field_id');
  const keys = [fieldId, targetId(target)].filter((item) => typeof item === 'string' && item.length > 0);
  for (const name of names) {
    if (own(target, name)) return { found: true, value: target[name] };
    const container = get(options, name);
    for (const key of keys) {
      const value = mapValue(container, key);
      if (value !== undefined) return { found: true, value };
    }
  }
  return { found: false, value: null };
}

function targetText(target, field) {
  return [get(target, 'label'), get(target, 'description'), get(target, 'kind'), get(field, 'label'), get(field, 'description')].map(text).filter(Boolean).join(' ');
}

export function classifyFieldPolicy(target, options = {}, field = null) {
  requireRecord(target, 'target');
  if (!isRecord(options)) throw new PlannerError('options must be an object');
  const fieldId = get(target, 'field_id');
  const configured = get(options, 'field_policies');
  const configuredValue = mapValue(configured, fieldId) ?? mapValue(configured, targetId(target));
  const explicit = canonicalPolicy((isRecord(configuredValue) ? get(configuredValue, 'policy') : configuredValue) ?? get(target, 'field_policy') ?? get(target, 'policy') ?? get(field, 'field_policy') ?? get(field, 'policy'));
  if (explicit !== null) return explicit;
  const value = targetText(target, field);
  if (/\b(?:race|ethnic(?:ity)?|gender|sex|sexual orientation|pronoun|disability|disabled|veteran|demographic|diversity|lgbtq)\b/u.test(value)) return 'demographic';
  if (/(?:work authorization|authorized to work|right to work|sponsor(?:ship)?|visa|citizen(?:ship)?|immigration|criminal|conviction|felon(?:y)?|background check|security clearance|export control|legally eligible)/u.test(value)) return 'legal';
  if (/\b(?:first|last|full|preferred)?\s*(?:name|email|e-mail|phone|telephone|mobile|address|street|city|state|province|zip|postal|country|location|linkedin|github|portfolio|website|contact)\b/u.test(value)) return 'identity';
  if (/(?:date of birth|birth date|graduat|gpa|grade point|degree|school|university|college|certif|license|clearance|credential|start date|end date|availability date|relocat|salary|compensation|pay rate|hourly rate)/u.test(value)) return 'hard_fact';
  if (/(?:why|motiv(?:at|ation)|interest|passion|hobby|obsession|favorite|tell us|describe|essay|proud|achievement|challenge|strength|weakness|excite|culture|ideal|goal|anything else|additional information|cover letter)/u.test(value)) return 'subjective';
  if (/(?:experience|skill|proficien|familiar|technical|program(?:ming)?|language|framework|tool|stack|database|cloud|project|qualification|years?\s+(?:of\s+)?experience|availability|work history)/u.test(value)) return 'qualification';
  return 'hard_fact';
}
function normalizeObservation(observation) {
  requireRecord(observation, 'observation');
  if (get(observation, 'schema') !== VISUAL_OBSERVATION_SCHEMA) throw new PlannerError('observation has an unsupported schema');
  const observationId = requireString(get(observation, 'observation_id'), 'observation.observation_id');
  const surface = requireRecord(get(observation, 'surface'), 'observation.surface');
  const screenshot = requireString(get(surface, 'screenshot_sha256'), 'observation.surface.screenshot_sha256');
  if (!SHA256.test(screenshot)) throw new PlannerError('observation surface screenshot identity is invalid');
  const viewport = requireRecord(get(surface, 'viewport'), 'observation.surface.viewport');
  if (!Number.isInteger(get(viewport, 'width')) || get(viewport, 'width') < 1 || !Number.isInteger(get(viewport, 'height')) || get(viewport, 'height') < 1) {
    throw new PlannerError('observation surface viewport is invalid');
  }
  const agent = requireRecord(get(observation, 'agent'), 'observation.agent');
  if (!['codex', 'gemini'].includes(get(agent, 'provider'))) throw new PlannerError('observation agent provider is invalid');
  const targets = get(observation, 'targets');
  if (!Array.isArray(targets)) throw new PlannerError('observation.targets must be an array');
  const seen = new Set();
  for (let index = 0; index < targets.length; index += 1) validateTarget(targets[index], index, viewport);
  for (const target of targets) {
    const id = targetId(target);
    if (seen.has(id)) throw new PlannerError(`duplicate target ${id}`);
    seen.add(id);
  }
  const blockers = get(observation, 'blockers', EMPTY);
  if (!Array.isArray(blockers)) throw new PlannerError('observation.blockers must be an array');
  return { observationId, screenshot, targets, blockers, agent, surface };
}

function validateTarget(target, index, viewport) {
  requireRecord(target, `observation.targets[${index}]`);
  targetId(target, `observation.targets[${index}]`);
  const kind = get(target, 'kind');
  if (typeof kind !== 'string' || !VISUAL_KINDS.includes(kind)) throw new PlannerError(`observation.targets[${index}].kind is invalid`);
  const candidate = requireRecord(get(target, 'candidate'), `observation.targets[${index}].candidate`);
  if (!CANDIDATE_CLASSES.has(get(candidate, 'class'))) throw new PlannerError(`observation.targets[${index}].candidate.class is invalid`);
  const bounds = requireRecord(get(target, 'bounds'), `observation.targets[${index}].bounds`);
  for (const key of ['x', 'y', 'width', 'height']) {
    if (!Number.isInteger(get(bounds, key))) throw new PlannerError(`observation.targets[${index}].bounds.${key} is invalid`);
  }
  if (get(bounds, 'width') < 1 || get(bounds, 'height') < 1 || get(bounds, 'x') < 0 || get(bounds, 'y') < 0 ||
      get(bounds, 'x') + get(bounds, 'width') > get(viewport, 'width') || get(bounds, 'y') + get(bounds, 'height') > get(viewport, 'height')) {
    throw new PlannerError(`observation.targets[${index}].bounds is outside the viewport`);
  }
  if (typeof get(target, 'visible') !== 'boolean' || typeof get(target, 'enabled') !== 'boolean') {
    throw new PlannerError(`observation.targets[${index}] visibility is invalid`);
  }
}

function normalizeInput(first, second, third) {
  let observation = first;
  let ledger = second;
  let options = third;
  if (isRecord(first) && own(first, 'observation')) {
    observation = first.observation;
    ledger = first.ledger ?? {};
    options = { ...first };
    delete options.observation;
    delete options.ledger;
    if (isRecord(third)) options = { ...options, ...third };
  } else if (isRecord(second) && own(second, 'ledger')) {
    ledger = second.ledger;
    options = { ...second };
    delete options.ledger;
    if (isRecord(third)) options = { ...options, ...third };
  }
  if (options === undefined || options === null) options = {};
  if (!isRecord(options)) throw new PlannerError('options must be an object');
  return { observation: normalizeObservation(observation), ledger: ledger ?? {}, options };
}
function normalizeLedger(ledger, observationId) {
  requireRecord(ledger, 'ledger');
  const latest = get(ledger, 'latest_observation_id');
  if (latest !== undefined && latest !== observationId) throw new PlannerError('ledger does not describe the current observation', 'E_PLANNER_STALE');
  const candidates = get(ledger, 'current_candidate_targets');
  if (candidates !== undefined && !Array.isArray(candidates)) throw new PlannerError('ledger.current_candidate_targets must be an array');
  if (Array.isArray(candidates)) {
    for (const candidate of candidates) {
      requireRecord(candidate, 'ledger.current_candidate_targets entry');
      if (get(candidate, 'observation_id') !== observationId || typeof get(candidate, 'target_id') !== 'string' || get(candidate, 'target_id').length === 0) {
        throw new PlannerError('ledger current candidate is stale', 'E_PLANNER_STALE');
      }
    }
  }
  const records = get(ledger, 'targets', EMPTY);
  if (!Array.isArray(records)) throw new PlannerError('ledger.targets must be an array');
  const byField = new Map();
  const byTarget = new Map();
  for (const record of records) {
    requireRecord(record, 'ledger target entry');
    const id = get(record, 'target_id');
    if (id !== undefined) {
      requireString(id, 'ledger target target_id');
      byTarget.set(id, record);
    }
    const fieldId = get(record, 'field_id');
    if (fieldId !== undefined) {
      requireString(fieldId, 'ledger target field_id');
      byField.set(fieldId, record);
    }
    const recordObservation = get(record, 'latest_observation_id');
    if (recordObservation !== undefined && recordObservation !== observationId) throw new PlannerError('ledger target is stale', 'E_PLANNER_STALE');
  }
  return { byField, byTarget, candidates };
}

function fieldRecord(target, ledgerInfo) {
  return ledgerInfo.byTarget.get(get(target, 'target_id')) ?? ledgerInfo.byField.get(get(target, 'field_id')) ?? null;
}

function candidateClass(target) {
  return get(get(target, 'candidate'), 'class', 'unknown');
}

function visibleAndEnabled(target, field) {
  if (get(target, 'visible') === false || get(target, 'enabled') === false) return false;
  if (get(target, 'readonly') === true || get(field, 'readonly') === true) return false;
  return true;
}

function fileTarget(target) {
  return get(target, 'kind') === 'file_upload' || isRecord(get(target, 'file'));
}

function valuePresent(target, field) {
  const state = get(target, 'value_state') ?? get(field, 'value_state');
  if (state === 'present' || state === 'selected') return true;
  if (get(target, 'checked') === true || (get(target, 'selected') !== null && get(target, 'selected') !== undefined)) return true;
  const file = get(target, 'file');
  return isRecord(file) && get(file, 'present') === true;
}

function targetValidity(target, field) {
  const validation = get(target, 'validation') ?? get(field, 'validation');
  if (!isRecord(validation)) return null;
  return get(validation, 'valid');
}

function isRejected(target, field, attempts) {
  if (targetValidity(target, field) === false || get(field, 'valid') === false) return true;
  if (get(field, 'rejected') === true || get(field, 'invalid') === true) return true;
  return valuePresent(target, field) && attempts.some((attempt) => REJECTED_OUTCOMES.has(text(get(attempt, 'outcome'))));
}

function attemptsFor(ledger, target, observationId) {
  const attempts = get(ledger, 'action_attempts', EMPTY);
  if (!Array.isArray(attempts)) throw new PlannerError('ledger.action_attempts must be an array');
  return attempts.filter((attempt) => isRecord(attempt) && get(attempt, 'observation_id') === observationId &&
    get(attempt, 'target_id') === get(target, 'target_id') && get(attempt, 'stale_target') !== true);
}

function hasPendingAttempt(attempts) {
  return attempts.some((attempt) => PENDING_OUTCOMES.has(text(get(attempt, 'outcome'))));
}

function isRetained(target, field) {
  if (get(field, 'retained') === true && get(field, 'valid') !== false) return true;
  return valuePresent(target, field) && targetValidity(target, field) === true;
}

function currentCandidateAllowed(target, ledgerInfo) {
  if (!Array.isArray(ledgerInfo.candidates) || ledgerInfo.candidates.length === 0) return true;
  return ledgerInfo.candidates.some((entry) => get(entry, 'target_id') === get(target, 'target_id'));
}

function optionsForTarget(target) {
  const options = get(target, 'options');
  if (!Array.isArray(options)) return [];
  const result = [];
  const seen = new Set();
  for (const option of options) {
    if (isRecord(option) && (get(option, 'enabled') === false || get(option, 'disabled') === true)) continue;
    const value = isRecord(option) ? get(option, 'value') ?? get(option, 'label') : option;
    if (value === null || value === undefined) continue;
    const normalized = String(value);
    if (seen.has(normalized)) continue;
    seen.add(normalized);
    result.push(normalized);
  }
  return result;
}

function actionForTarget(target) {
  if (fileTarget(target)) return ['upload_file'];
  if (ORDINARY_TEXT_KINDS.has(get(target, 'kind'))) return ['type_text'];
  return ['click'];
}
function dependencyMarked(target, field) {
  return get(target, 'dependency_marked') === true || get(field, 'dependency_marked') === true ||
    get(target, 'dependent') === true || get(field, 'dependent') === true;
}

function retryMarked(target, field, attempts) {
  return attempts.some((attempt) => get(attempt, 'retry_required') === true) ||
    get(target, 'retry_required') === true || get(field, 'retry_required') === true;
}

function newlyRevealed(target, field, ledger, observationId) {
  if (get(target, 'revealed_observation_id') === observationId || get(field, 'revealed_observation_id') === observationId) return true;
  if (get(field, 'last_revealed_observation_id') === observationId) return true;
  const diffs = get(ledger, 'diffs');
  if (!Array.isArray(diffs)) return false;
  const latest = diffs.at(-1);
  if (!isRecord(latest) || get(latest, 'to_observation_id') !== observationId) return false;
  return Array.isArray(get(latest, 'added_targets')) && get(latest, 'added_targets').some((item) => get(item, 'target_id') === get(target, 'target_id'));
}

function answerAvailability(target, options) {
  const exact = configuredFor(options, target, ['exact_answer', 'exact_memory', 'memory_answer']);
  const fallback = configuredFor(options, target, ['configured_fallback', 'configured_default', 'default_answer']);
  return { exact, fallback };
}

function inferable(target, field, policy, options) {
  if (typeof get(target, 'inferable') === 'boolean') return get(target, 'inferable');
  if (typeof get(field, 'inferable') === 'boolean') return get(field, 'inferable');
  const configured = get(options, 'inferable_fields');
  const value = mapValue(configured, get(target, 'field_id'));
  if (typeof value === 'boolean') return value;
  return SUBJECTIVE_POLICIES.has(policy);
}

function requiredModelTier(target, field, options) {
  const value = get(target, 'required_model_tier') ?? get(field, 'required_model_tier') ?? get(options, 'required_model_tier');
  if (value === undefined) return 'cheap';
  if (!['cheap', 'standard', 'strong', 'highest'].includes(value)) throw new PlannerError('required_model_tier is invalid');
  return value;
}

function fieldUnit(info, options) {
  const policy = classifyFieldPolicy(info.target, options, info.field);
  const availability = answerAvailability(info.target, options);
  const configuredFallback = availability.fallback.found ? availability.fallback.value : null;
  const allowedAnswerSources = Array.isArray(get(info.field, 'allowed_answer_sources'))
    ? [...get(info.field, 'allowed_answer_sources')]
    : [...ANSWER_SOURCES_BY_POLICY[policy]];
  return immutable({
    observationId: info.observationId,
    targetId: get(info.target, 'target_id'),
    fieldId: get(info.target, 'field_id') ?? null,
    fieldPolicy: policy,
    allowedAnswerSources,
    allowedActions: actionForTarget(info.target),
    allowedOptions: optionsForTarget(info.target),
    configuredFallback,
    requiredModelTier: requiredModelTier(info.target, info.field, options),
    escalationPermitted: Boolean(get(info.field, 'escalation_permitted') ?? get(info.target, 'escalation_permitted') ?? SUBJECTIVE_POLICIES.has(policy)),
    reobservationRequired: true,
  });
}

function pageUnit(info, options) {
  return immutable({
    observationId: info.observationId,
    targetId: get(info.target, 'target_id'),
    fieldId: null,
    fieldPolicy: null,
    allowedAnswerSources: [],
    allowedActions: ['click'],
    allowedOptions: [],
    configuredFallback: null,
    requiredModelTier: get(options, 'required_model_tier', 'cheap'),
    escalationPermitted: false,
    reobservationRequired: true,
  });
}

function compareInfo(left, right) {
  if (left.priority !== right.priority) return left.priority - right.priority;
  if (left.priority >= 4) {
    const rank = (info) => info.exact ? 0 : info.fallback ? 1 : info.inferable ? 2 : 3;
    if (rank(left) !== rank(right)) return rank(left) - rank(right);
  }
  const leftField = String(get(left.target, 'field_id') ?? '');
  const rightField = String(get(right.target, 'field_id') ?? '');
  if (leftField !== rightField) return leftField < rightField ? -1 : 1;
  const leftTarget = String(get(left.target, 'target_id'));
  const rightTarget = String(get(right.target, 'target_id'));
  if (leftTarget === rightTarget) return left.index - right.index;
  return leftTarget < rightTarget ? -1 : 1;
}

function targetInfos(observation, ledger, ledgerInfo, options) {
  const infos = [];
  observation.targets.forEach((target, index) => {
    const klass = candidateClass(target);
    if (PAGE_CLASSES.has(klass) || !currentCandidateAllowed(target, ledgerInfo)) return;
    const field = fieldRecord(target, ledgerInfo) ?? target;
    const fieldId = get(target, 'field_id') ?? get(field, 'field_id');
    if (typeof fieldId !== 'string' || fieldId.length === 0) return;
    if (!visibleAndEnabled(target, field)) return;
    const attempts = attemptsFor(ledger, target, observation.observationId);
    const rejected = isRejected(target, field, attempts);
    const retained = isRetained(target, field);
    if (retained && !rejected) return;
    if (!rejected && hasPendingAttempt(attempts)) return;
    const state = get(target, 'value_state', 'unknown');
    const unresolved = !retained && (state === 'blank' || state === 'unknown' || !valuePresent(target, field));
    const required = get(target, 'required') === true || get(field, 'required') === true;
    const newly = newlyRevealed(target, field, ledger, observation.observationId);
    const upload = fileTarget(target);
    const availability = answerAvailability(target, options);
    const policy = classifyFieldPolicy(target, options, field);
    const info = {
      observationId: observation.observationId,
      target,
      field,
      index,
      rejected,
      retained,
      unresolved,
      required,
      newly,
      upload,
      exact: availability.exact.found,
      fallback: availability.fallback.found,
      inferable: inferable(target, field, policy, options),
      attempts,
      policy,
    };
    if (rejected) info.priority = 0;
    else if (required && unresolved) {
      if (newly) info.priority = 2;
      else if (upload) info.priority = 3;
      else info.priority = 1;
    } else if (!required && unresolved) {
      if (info.exact || info.fallback) info.priority = 4;
      else if (info.inferable || upload) info.priority = 5;
      else info.priority = 6;
    } else info.priority = null;
    if (info.priority !== null) infos.push(info);
  });
  infos.sort(compareInfo);
  return infos;
}
function safeBatchInfo(info) {
  if (!info || info.rejected || info.upload || !info.unresolved || info.newly) return false;
  if (retryMarked(info.target, info.field, info.attempts) || dependencyMarked(info.target, info.field)) return false;
  if (candidateClass(info.target) !== 'field') return false;
  if (!ORDINARY_TEXT_KINDS.has(get(info.target, 'kind'))) return false;
  if (get(info.target, 'readonly') === true || get(info.field, 'readonly') === true) return false;
  if (get(info.target, 'sensitive') === true || get(info.field, 'sensitive') === true) return false;
  return info.policy !== 'legal' && info.policy !== 'demographic' && info.policy !== 'identity';
}

function batchLimit(options) {
  const value = get(options, 'max_batch_size', DEFAULT_BATCH_SIZE);
  if (!Number.isInteger(value) || value < 1 || value > 3) throw new PlannerError('max_batch_size must be an integer from 1 through 3');
  return value;
}

function pageCandidates(observation, ledgerInfo) {
  return observation.targets
    .filter((target) => PAGE_CLASSES.has(candidateClass(target)) && visibleAndEnabled(target, {}) && currentCandidateAllowed(target, ledgerInfo))
    .sort((left, right) => {
      const leftId = get(left, 'target_id');
      const rightId = get(right, 'target_id');
      return leftId < rightId ? -1 : leftId > rightId ? 1 : 0;
    });
}

function auditFor(options, observation) {
  const audit = get(options, 'visual_audit') ?? get(options, 'visualAudit') ?? get(options, 'current_visual_audit');
  if (!isRecord(audit)) return null;
  if (get(audit, 'observation_id') !== observation.observationId || get(audit, 'screenshot_sha256') !== observation.screenshot) return null;
  const ids = get(audit, 'final_candidate_target_ids');
  if (!Array.isArray(ids)) return null;
  return ids;
}

function planFromNormalized(observation, ledger, ledgerInfo, options) {
  const infos = targetInfos(observation, ledger, ledgerInfo, options);
  if (infos.length > 0) return fieldUnit(infos[0], options);

  const pages = pageCandidates(observation, ledgerInfo);
  const continuation = pages.find((target) => candidateClass(target) === 'non_final_navigation');
  if (continuation) return pageUnit({ observationId: observation.observationId, target: continuation }, options);

  if (get(options, 'submission_ready') === true || get(options, 'submissionReady') === true) {
    const finalIds = auditFor(options, observation);
    if (!finalIds || finalIds.length === 0) return null;
    const final = pages.find((target) => candidateClass(target) === 'final_candidate' && finalIds.includes(get(target, 'target_id')));
    if (!final) throw new PlannerError('submission-ready state has no current audited final target', 'E_PLANNER_STALE');
    return pageUnit({ observationId: observation.observationId, target: final }, options);
  }
  return null;
}

function planInternal(first, second, third) {
  const { observation, ledger, options } = normalizeInput(first, second, third);
  const ledgerInfo = normalizeLedger(ledger, observation.observationId);
  return planFromNormalized(observation, ledger, ledgerInfo, options);
}

export function planVisualApplicationWork(first, second, third) {
  return planInternal(first, second, third);
}

export function planSafeVisualBatch(first, second, third) {
  const { observation, ledger, options } = normalizeInput(first, second, third);
  const ledgerInfo = normalizeLedger(ledger, observation.observationId);
  const infos = targetInfos(observation, ledger, ledgerInfo, options);
  const limit = batchLimit(options);
  if (infos.length > 0) {
    const units = [];
    for (const info of infos) {
      if (!safeBatchInfo(info)) {
        if (units.length === 0) return immutable({ mode: 'single', observationId: observation.observationId, units: [fieldUnit(info, options)] });
        break;
      }
      units.push(fieldUnit(info, options));
      if (units.length >= limit) break;
    }
    return immutable({ mode: units.length > 1 ? 'batch' : 'single', observationId: observation.observationId, units });
  }
  const next = planFromNormalized(observation, ledger, ledgerInfo, options);
  if (next === null) return null;
  return immutable({ mode: 'single', observationId: observation.observationId, units: [next] });
}

export default Object.freeze({
  classifyFieldPolicy,
  planVisualApplicationWork,
  planSafeVisualBatch,
  PlannerError,
  FIELD_POLICIES,
  ANSWER_SOURCES_BY_POLICY,
  COMPUTER_ACTIONS,
  VISUAL_KINDS,
});
