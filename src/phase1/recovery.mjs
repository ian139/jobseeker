const MAX_FAILURE_TEXT = 512;
const MAX_IDENTIFIER_LENGTH = 512;
const MAX_RETRY_HISTORY = 100;

function deepFreeze(value, seen = new Set()) {
  if (value === null || typeof value !== 'object' || seen.has(value)) return value;
  seen.add(value);
  for (const child of Object.values(value)) deepFreeze(child, seen);
  return Object.freeze(value);
}

function immutable(value) {
  if (value === undefined) return undefined;
  return deepFreeze(structuredClone(value));
}

function isRecord(value) {
  return value !== null && typeof value === 'object' && !Array.isArray(value);
}

function enumList(values, aliases = {}) {
  const result = [...values];
  for (const [name, value] of Object.entries(aliases)) {
    Object.defineProperty(result, name, {
      configurable: false,
      enumerable: false,
      value,
      writable: false,
    });
  }
  return Object.freeze(result);
}

function normalizedText(value, fallback = '') {
  if (typeof value !== 'string') return fallback;
  return value.trim().slice(0, MAX_FAILURE_TEXT);
}

function normalizedToken(value, fallback = '') {
  const text = normalizedText(value, fallback);
  return text
    .toLowerCase()
    .replace(/([a-z0-9])([A-Z])/gu, '$1_$2')
    .replace(/[^a-z0-9]+/gu, '_')
    .replace(/^_+|_+$/gu, '');
}

function normalizedIdentifier(value, fallback = null) {
  if (value === null || value === undefined) return fallback;
  const text = normalizedText(String(value), '');
  if (!text || text.length > MAX_IDENTIFIER_LENGTH) return fallback;
  return text;
}

function firstDefined(...values) {
  return values.find((value) => value !== undefined && value !== null);
}

export const RECOVERY_SCHEMA = 'phase1-recovery-v1';
export const FAILURE_CLASSIFICATION_SCHEMA = 'phase1-failure-classification-v1';
export const RETRY_BUDGET_SCHEMA = 'phase1-retry-budget-v1';
export const SUBMISSION_RECONCILIATION_SCHEMA = 'phase1-submission-reconciliation-v1';

export const FAILURE_CLASSES = enumList([
  'validation',
  'stale_observation',
  'navigation',
  'action',
  'observation',
  'transient',
  'submission_uncertain',
  'user_required',
  'access_control',
  'captcha',
  'unknown',
], {
  VALIDATION: 'validation',
  VALIDATION_FAILURE: 'validation',
  STALE_OBSERVATION: 'stale_observation',
  STALE_REFERENCE: 'stale_observation',
  STALE_REF: 'stale_observation',
  NAVIGATION: 'navigation',
  NAVIGATION_FAILURE: 'navigation',
  ACTION: 'action',
  ACTION_FAILURE: 'action',
  OBSERVATION: 'observation',
  OBSERVATION_FAILURE: 'observation',
  TRANSIENT: 'transient',
  TRANSIENT_FAILURE: 'transient',
  SUBMISSION_UNCERTAIN: 'submission_uncertain',
  UNCERTAIN_SUBMISSION: 'submission_uncertain',
  USER_REQUIRED: 'user_required',
  USER: 'user_required',
  ACCESS_CONTROL: 'access_control',
  ACCESS: 'access_control',
  UNKNOWN: 'unknown',
});

export const TERMINAL_FAILURE_CLASSES = enumList([
  'user_required',
  'access_control',
], {
  USER_REQUIRED: 'user_required',
  USER: 'user_required',
  ACCESS_CONTROL: 'access_control',
  ACCESS: 'access_control',
});

export const RECOVERY_STEPS = enumList([
  'reobserve',
  'retry_changed_strategy',
  'alternate_action',
  'reconcile_submission',
  'escalate_user',
  'escalate_access_control',
  'stop',
], {
  REOBSERVE: 'reobserve',
  NEW_OBSERVATION: 'reobserve',
  RETRY: 'retry_changed_strategy',
  RETRY_CHANGED_STRATEGY: 'retry_changed_strategy',
  ALTERNATE_ACTION: 'alternate_action',
  RECONCILE_SUBMISSION: 'reconcile_submission',
  ESCALATE_USER: 'escalate_user',
  USER_ESCALATION: 'escalate_user',
  ESCALATE_ACCESS_CONTROL: 'escalate_access_control',
  ACCESS_CONTROL_ESCALATION: 'escalate_access_control',
  STOP: 'stop',
});

export const ESCALATION_ORDER = Object.freeze([
  'reobserve',
  'retry_changed_strategy',
  'alternate_action',
  'reconcile_submission',
  'escalate_user',
  'escalate_access_control',
]);

export const RETRYABLE_FAILURE_CLASSES = enumList([
  'validation',
  'stale_observation',
  'navigation',
  'action',
  'observation',
  'transient',
  'unknown',
  'captcha',
]);

export const DEFAULT_RETRY_BUDGET = immutable({
  schema: RETRY_BUDGET_SCHEMA,
  maxRetries: 3,
  maxAttempts: 4,
});

const TERMINAL_CLASS_SET = new Set(TERMINAL_FAILURE_CLASSES);
const FAILURE_CLASS_SET = new Set(FAILURE_CLASSES);
const RETRYABLE_CLASS_SET = new Set(RETRYABLE_FAILURE_CLASSES);
const FINAL_SUBMIT_ACTIONS = new Set(['final_submit', 'submit', 'final-submit', 'final submit']);
const SUCCESS_OUTCOMES = new Set(['success', 'succeeded', 'submitted', 'complete', 'completed', 'confirmed']);
const FAILED_OUTCOMES = new Set(['failed', 'failure', 'rejected', 'declined', 'not_submitted', 'not-submitted']);
const UNCERTAIN_OUTCOMES = new Set([
  'uncertain',
  'unknown',
  'pending',
  'timeout',
  'timed_out',
  'timed-out',
  'disconnected',
  'connection_lost',
  'browser_closed',
  'transport_error',
]);
const BLOCKED_OUTCOMES = new Set(['blocked', 'access_control', 'user_required', 'needs_user']);

const CLASS_ALIASES = new Map([
  ['validation', 'validation'],
  ['validation_error', 'validation'],
  ['validation_failure', 'validation'],
  ['invalid', 'validation'],
  ['invalid_field', 'validation'],
  ['required_field', 'validation'],
  ['stale', 'stale_observation'],
  ['stale_ref', 'stale_observation'],
  ['stale_reference', 'stale_observation'],
  ['stale_observation', 'stale_observation'],
  ['observation_stale', 'stale_observation'],
  ['navigation', 'navigation'],
  ['navigation_error', 'navigation'],
  ['navigation_failure', 'navigation'],
  ['redirect', 'navigation'],
  ['route_mismatch', 'navigation'],
  ['posting_unavailable', 'navigation'],
  ['not_found', 'navigation'],
  ['action', 'action'],
  ['action_error', 'action'],
  ['action_failure', 'action'],
  ['control_error', 'action'],
  ['observation', 'observation'],
  ['observation_error', 'observation'],
  ['observation_failure', 'observation'],
  ['dom_error', 'observation'],
  ['snapshot_error', 'observation'],
  ['transient', 'transient'],
  ['transient_error', 'transient'],
  ['transient_failure', 'transient'],
  ['network', 'transient'],
  ['network_error', 'transient'],
  ['timeout', 'transient'],
  ['rate_limit', 'transient'],
  ['uncertain', 'submission_uncertain'],
  ['uncertain_submit', 'submission_uncertain'],
  ['uncertain_submission', 'submission_uncertain'],
  ['submission_uncertain', 'submission_uncertain'],
  ['submit_uncertain', 'submission_uncertain'],
  ['submission_timeout', 'submission_uncertain'],
  ['user', 'user_required'],
  ['user_required', 'user_required'],
  ['needs_user', 'user_required'],
  ['user_blocked', 'user_required'],
  ['manual_intervention', 'user_required'],
  ['missing_answer', 'user_required'],
  ['access', 'access_control'],
  ['access_control', 'access_control'],
  ['access_denied', 'access_control'],
  ['authentication', 'access_control'],
  ['authentication_required', 'access_control'],
  ['authorization', 'access_control'],
  ['captcha', 'captcha'],
  ['forbidden', 'access_control'],
  ['permission_denied', 'access_control'],
  ['unknown', 'unknown'],
]);

const CLASS_FAMILY_TOKENS = Object.freeze({
  validation: ['valid', 'invalid', 'required', 'constraint', 'field'],
  stale_observation: ['stale', 'reference', 'ref', 'observation'],
  navigation: ['navigation', 'redirect', 'route', 'url', 'posting', 'not_found', '404'],
  action: ['action', 'click', 'fill', 'select', 'upload', 'control'],
  observation: ['observation', 'snapshot', 'dom', 'frame'],
  transient: ['transient', 'network', 'timeout', 'disconnect', 'rate_limit', 'temporary'],
});

export function canonicalClass(value) {
  const token = normalizedToken(value);
  if (!token) return null;
  if (CLASS_ALIASES.has(token)) return CLASS_ALIASES.get(token);
  if (FAILURE_CLASS_SET.has(token)) return token;
  return null;
}

function hasToken(text, tokens) {
  return tokens.some((token) => text.includes(token));
}

function inputRecord(input) {
  if (input instanceof Error) {
    return { error: input, code: input.code, message: input.message, name: input.name };
  }
  if (typeof input === 'string') return { code: input, message: input };
  if (!isRecord(input)) return {};
  if (isRecord(input.failure) && !Object.hasOwn(input, 'failureClass') && !Object.hasOwn(input, 'class')) {
    return { ...input.failure, ...input };
  }
  return input;
}

function explicitFailureClass(input) {
  const candidates = [
    input.failureClass,
    input.failure_class,
    input.class,
    input.kind,
    input.category,
    input.type,
  ];
  for (const candidate of candidates) {
    const value = canonicalClass(candidate);
    if (value !== null) return value;
  }
  return null;
}

function inferredFailureClass(input, code, message) {
  const submitAttempt = input.finalSubmit === true
    || input.final_submit === true
    || FINAL_SUBMIT_ACTIONS.has(normalizedToken(input.action));
  const combined = `${code} ${message} ${normalizedToken(input.name)} ${normalizedToken(input.errorName)}`;
  const directCode = canonicalClass(code);
  const captchaSignal = input.captcha === true
    || input.captchaRequired === true
    || input.captcha_required === true
    || hasToken(combined, ['captcha', 'recaptcha', 'hcaptcha', 'turnstile']);
  if (captchaSignal) return 'captcha';
  if (submitAttempt && (directCode === 'submission_uncertain'
    || directCode === 'transient'
    || hasToken(combined, ['uncertain', 'timeout', 'timed_out', 'disconnected', 'connection_lost'])
    || input.outcome === undefined || input.outcome === null)) {
    return 'submission_uncertain';
  }
  if (directCode !== null) return directCode;
  if (input.requiresAccessControl === true || input.accessControl === true || input.access_control === true) {
    return 'access_control';
  }
  if (input.requiresUser === true || input.userRequired === true || input.user_required === true) {
    return 'user_required';
  }
  if (input.status === 401 || input.status === 403 || input.httpStatus === 401 || input.httpStatus === 403) {
    return 'access_control';
  }
  if (hasToken(combined, ['access_control', 'authentication', 'auth_required', 'forbidden', 'permission_denied', 'access_denied'])) {
    return 'access_control';
  }
  if (hasToken(combined, ['needs_user', 'user_required', 'manual_intervention', 'missing_answer', 'ask_user'])) {
    return 'user_required';
  }
  for (const [failureClass, tokens] of Object.entries(CLASS_FAMILY_TOKENS)) {
    if (hasToken(combined, tokens)) return failureClass;
  }
  if (input.outcome !== undefined && UNCERTAIN_OUTCOMES.has(normalizedToken(input.outcome))) {
    return submitAttempt ? 'submission_uncertain' : 'transient';
  }
  return 'unknown';
}

export class RecoveryError extends Error {
  constructor(code, message = code, details = {}) {
    if (isRecord(message)) {
      details = message;
      message = code;
    }
    super(String(message));
    this.name = 'RecoveryError';
    this.code = String(code);
    this.details = immutable(isRecord(details) ? details : { value: details });
  }
}

function recoveryFail(code, message, details = {}) {
  throw new RecoveryError(code, message, details);
}

export function isTerminalFailure(value) {
  return TERMINAL_CLASS_SET.has(canonicalClass(value) ?? normalizedToken(value));
}

export function isRecoverableFailure(value) {
  return !isTerminalFailure(value);
}

export function classifyFailure(input = {}) {
  const value = inputRecord(input);
  const code = normalizedText(firstDefined(value.code, value.errorCode, value.error_code, value.reasonCode, value.reason_code), 'unknown');
  const message = normalizedText(firstDefined(value.message, value.error?.message, value.errorMessage, value.error_message), '');
  const direct = explicitFailureClass(value);
  const failureClass = direct ?? inferredFailureClass(value, normalizedToken(code), normalizedToken(message));
  const terminal = TERMINAL_CLASS_SET.has(failureClass);
  const uncertain = failureClass === 'submission_uncertain';
  const requiresUser = failureClass === 'user_required';
  const requiresAccessControl = failureClass === 'access_control';
  const recoverable = !terminal;
  const retryable = RETRYABLE_CLASS_SET.has(failureClass);
  const submitAttempt = value.finalSubmit === true
    || value.final_submit === true
    || FINAL_SUBMIT_ACTIONS.has(normalizedToken(value.action));
  const disposition = terminal
    ? 'escalate'
    : uncertain
      ? 'reconcile'
      : 'recover';
  const escalation = requiresUser ? 'user' : requiresAccessControl ? 'access_control' : null;
  return immutable({
    schema: FAILURE_CLASSIFICATION_SCHEMA,
    failureClass,
    failure_class: failureClass,
    class: failureClass,
    code,
    message,
    terminal,
    recoverable,
    retryable,
    uncertain,
    submissionUncertain: uncertain,
    requiresUser,
    requiresAccessControl,
    genericUserBlocker: false,
    disposition,
    escalation,
    submitAttempt,
    terminalClass: terminal ? failureClass : null,
    original: direct,
  });
}

function retryInputRecord(input) {
  if (!isRecord(input)) return {};
  return input.retry && isRecord(input.retry) ? input.retry : input;
}

function budgetStateAndCandidate(first, second) {
  if (second !== undefined) return { state: first ?? {}, candidate: second };
  if (!isRecord(first)) return { state: {}, candidate: first };
  if (isRecord(first.retry) && (first.budget || first.retryBudget || first.retryState)) {
    return {
      state: first.budget ?? first.retryBudget ?? first.retryState ?? {},
      candidate: first.retry,
    };
  }
  const budgetKeys = [
    'maxRetries', 'max_retries', 'maxAttempts', 'max_attempts', 'retries', 'retryHistory',
    'retry_history', 'attempts', 'retryCount', 'retry_count', 'remaining', 'exhausted',
  ];
  const hasBudgetShape = budgetKeys.some((key) => Object.hasOwn(first, key));
  if (!hasBudgetShape) {
    return Object.keys(first).length === 0
      ? { state: first, candidate: undefined }
      : { state: {}, candidate: first };
  }
  const hasCanonicalHistory = Array.isArray(first.retries)
    || Array.isArray(first.retryHistory)
    || Array.isArray(first.retry_history)
    || Object.hasOwn(first, 'retryCount');
  if (!hasCanonicalHistory && (isRecord(first.nextRetry) || isRecord(first.retryRecord))) {
    return { state: first, candidate: first.nextRetry ?? first.retryRecord };
  }
  const retryFields = [
    'failure', 'failureClass', 'failure_class', 'class', 'kind', 'category', 'type',
    'strategy', 'approach', 'method', 'plan', 'action', 'actionType', 'action_type',
    'observationId', 'observation_id', 'newObservationId', 'new_observation_id',
    'attemptId', 'attempt_id', 'outcome', 'result', 'code', 'errorCode', 'error_code',
  ];
  if (retryFields.some((key) => Object.hasOwn(first, key))) {
    const state = { ...first };
    for (const key of retryFields) delete state[key];
    return { state, candidate: first };
  }
  return { state: first, candidate: undefined };
}

function numeric(value, fallback, minimum = 0, maximum = MAX_RETRY_HISTORY) {
  if (value === undefined || value === null) return fallback;
  if (!Number.isSafeInteger(value) || value < minimum || value > maximum) return null;
  return value;
}

function historyFromState(state) {
  const retries = Array.isArray(state.retries)
    ? state.retries
    : Array.isArray(state.retryHistory)
      ? state.retryHistory
      : Array.isArray(state.retry_history)
        ? state.retry_history
        : Array.isArray(state.attempts)
          ? state.attempts
          : [];
  const countValue = firstDefined(state.retryCount, state.retry_count, state.attemptCount, state.attempt_count);
  const numericAttempts = !Array.isArray(state.attempts)
    ? numeric(state.attempts, null)
    : null;
  const count = retries.length > 0
    ? retries.length
    : countValue !== undefined && countValue !== null
      ? countValue
      : numericAttempts !== null
        ? numericAttempts
        : 0;
  return { retries: [...retries], count, attemptsArray: Array.isArray(state.attempts) };
}

function maxRetriesFromState(state) {
  const explicitRetries = firstDefined(state.maxRetries, state.max_retries);
  if (explicitRetries !== undefined) {
    const maxRetries = numeric(explicitRetries, null);
    if (maxRetries === null) recoveryFail('INVALID_RETRY_BUDGET', 'maxRetries must be a non-negative safe integer');
    return maxRetries;
  }
  const explicitAttempts = firstDefined(state.maxAttempts, state.max_attempts);
  if (explicitAttempts !== undefined) {
    const maxAttempts = numeric(explicitAttempts, null, 1);
    if (maxAttempts === null) recoveryFail('INVALID_RETRY_BUDGET', 'maxAttempts must be a safe integer of at least 1');
    return maxAttempts - 1;
  }
  return DEFAULT_RETRY_BUDGET.maxRetries;
}

function normalizeRetryEntry(input, index = 0) {
  const value = retryInputRecord(input);
  if (!isRecord(value)) recoveryFail('INVALID_RETRY', 'retry record must be an object', { index });
  const strategy = normalizedIdentifier(firstDefined(value.strategy, value.approach, value.method, value.plan), null);
  const action = normalizedIdentifier(firstDefined(value.action, value.actionType, value.action_type), null);
  const observationId = normalizedIdentifier(firstDefined(value.observationId, value.observation_id, value.newObservationId, value.new_observation_id), null);
  const attemptId = normalizedIdentifier(firstDefined(value.attemptId, value.attempt_id), null);
  const failure = classifyFailure(value.failure ?? value);
  if (strategy === null && action === null && observationId === null) {
    recoveryFail('RETRY_CONTEXT_REQUIRED', 'a retry requires a changed strategy or a new observation');
  }
  return {
    retryIndex: Number.isSafeInteger(value.retryIndex) ? value.retryIndex : index,
    failureClass: failure.failureClass,
    strategy: strategy ?? action,
    action,
    observationId,
    attemptId,
    code: failure.code,
    outcome: normalizedIdentifier(firstDefined(value.outcome, value.result), null),
  };
}

function previousRetryState(state, history, count) {
  const last = history.at(-1);
  if (last !== undefined) return normalizeRetryEntry(last, Math.max(0, count - 1));
  const strategy = normalizedIdentifier(firstDefined(state.lastStrategy, state.last_strategy, state.strategy), null);
  const observationId = normalizedIdentifier(firstDefined(
    state.lastObservationId,
    state.last_observation_id,
    state.observationId,
    state.observation_id,
  ), null);
  if (strategy === null && observationId === null && count === 0) return null;
  return {
    retryIndex: Math.max(0, count - 1),
    strategy,
    observationId,
    action: normalizedIdentifier(firstDefined(state.lastAction, state.last_action, state.action), null),
    failureClass: canonicalClass(firstDefined(state.lastFailureClass, state.last_failure_class, state.failureClass)) ?? 'unknown',
    attemptId: null,
    code: normalizedText(firstDefined(state.lastCode, state.last_code), 'unknown'),
    outcome: null,
  };
}

function outputBudget(state, history, count, maxRetries, extras = {}) {
  const attemptsArray = Array.isArray(state.attempts);
  const result = {
    ...state,
    schema: RETRY_BUDGET_SCHEMA,
    maxRetries,
    maxAttempts: maxRetries + 1,
    retries: history,
    retryHistory: history,
    attempts: attemptsArray ? history : count,
    retryCount: count,
    remaining: Math.max(0, maxRetries - count),
    exhausted: count >= maxRetries,
    canRetry: count < maxRetries,
    valid: true,
    ...extras,
  };
  return immutable(result);
}

function retryProgress(previous, current) {
  if (previous === null) {
    return {
      strategyChanged: current.strategy !== null,
      observationChanged: current.observationId !== null,
    };
  }
  return {
    strategyChanged: current.strategy !== null
      && (previous.strategy === null || current.strategy !== previous.strategy),
    observationChanged: current.observationId !== null
      && (previous.observationId === null || current.observationId !== previous.observationId),
  };
}

function validateHistory(state, history, count) {
  if (history.length > MAX_RETRY_HISTORY) recoveryFail('INVALID_RETRY_BUDGET', 'retry history exceeds the bounded limit');
  if (history.length !== 0 && count !== history.length) {
    recoveryFail('INVALID_RETRY_BUDGET', 'retry count does not match retry history', {
      count,
      historyLength: history.length,
    });
  }
  let previous = null;
  for (let index = 0; index < history.length; index += 1) {
    const current = normalizeRetryEntry(history[index], index);
    if (previous !== null) {
      const progress = retryProgress(previous, current);
      if (!progress.strategyChanged && !progress.observationChanged) {
        recoveryFail('RETRY_NO_PROGRESS', 'each retry must change strategy or use a new observation', { index });
      }
    }
    previous = current;
  }
}

export function validateRetryBudget(input = {}, candidate = undefined) {
  const split = budgetStateAndCandidate(input, candidate);
  const state = isRecord(split.state) ? split.state : {};
  const maxRetries = maxRetriesFromState(state);
  const historyState = historyFromState(state);
  const history = historyState.retries.map((entry, index) => normalizeRetryEntry(entry, index));
  const count = history.length > 0 ? history.length : historyState.count;
  if (count === null || count < 0 || count > MAX_RETRY_HISTORY) {
    recoveryFail('INVALID_RETRY_BUDGET', 'retry count must be a bounded non-negative integer');
  }
  validateHistory(state, history, count);
  if (split.candidate !== undefined) {
    const candidateEntry = normalizeRetryEntry(split.candidate, count);
    const previous = previousRetryState(state, history, count);
    const progress = retryProgress(previous, candidateEntry);
    if (previous !== null && !progress.strategyChanged && !progress.observationChanged) {
      recoveryFail('RETRY_NO_PROGRESS', 'each retry must change strategy or use a new observation');
    }
    if (count >= maxRetries) recoveryFail('RETRY_BUDGET_EXHAUSTED', 'retry budget is exhausted', { maxRetries, count });
    history.push(candidateEntry);
    return outputBudget(state, history, count + 1, maxRetries, {
      retry: candidateEntry,
      lastRetry: candidateEntry,
      strategyChanged: progress.strategyChanged,
      observationChanged: progress.observationChanged,
    });
  }
  return outputBudget(state, history, count, maxRetries);
}

function hasUncertainSubmission(state) {
  if (!isRecord(state)) return false;
  const direct = normalizedToken(firstDefined(state.status, state.outcome, state.submissionStatus, state.submission_status));
  if (UNCERTAIN_OUTCOMES.has(direct) || direct === 'uncertain' || direct === 'pending') return true;
  const attempts = Array.isArray(state.attempts)
    ? state.attempts
    : Array.isArray(state.submissionAttempts)
      ? state.submissionAttempts
      : Array.isArray(state.submission_attempts)
        ? state.submission_attempts
        : [];
  return attempts.some((attempt) => UNCERTAIN_OUTCOMES.has(normalizedToken(attempt?.outcome ?? attempt?.status)));
}

export function recordRetry(first = {}, second = undefined) {
  const split = budgetStateAndCandidate(first, second);
  let state = isRecord(split.state) ? split.state : {};
  let candidate = split.candidate;
  if (candidate === undefined && isRecord(first) && !Object.keys(state).length) candidate = first;
  if (!isRecord(candidate)) recoveryFail('RETRY_CONTEXT_REQUIRED', 'retry details are required');
  if (hasUncertainSubmission(state) && FINAL_SUBMIT_ACTIONS.has(normalizedToken(candidate.action))) {
    recoveryFail('DUPLICATE_FINAL_SUBMIT', 'final submission is forbidden while the previous attempt is uncertain');
  }
  const maxRetries = maxRetriesFromState(state);
  const historyState = historyFromState(state);
  const history = historyState.retries.map((entry, index) => normalizeRetryEntry(entry, index));
  const count = history.length > 0 ? history.length : historyState.count;
  validateHistory(state, history, count);
  if (count >= maxRetries) recoveryFail('RETRY_BUDGET_EXHAUSTED', 'retry budget is exhausted', { maxRetries, count });
  const entry = normalizeRetryEntry(candidate, count);
  const previous = previousRetryState(state, history, count);
  const progress = retryProgress(previous, entry);
  const strategyChanged = progress.strategyChanged;
  const observationChanged = progress.observationChanged;
  if (previous !== null && !strategyChanged && !observationChanged) {
    recoveryFail('RETRY_NO_PROGRESS', 'each retry must change strategy or use a new observation');
  }
  history.push(entry);
  return outputBudget(state, history, count + 1, maxRetries, {
    retry: entry,
    retryRecord: entry,
    lastRetry: entry,
    strategyChanged,
    observationChanged,
    sameRun: true,
    sameWorkspace: true,
  });
}

function planInput(failure, context) {
  if (context !== undefined) return { failure, context: isRecord(context) ? context : {} };
  if (isRecord(failure) && isRecord(failure.failure)) {
    const { failure: nested, ...rest } = failure;
    return { failure: nested, context: rest };
  }
  return { failure, context: {} };
}

function recoveryCandidates(context) {
  const source = firstDefined(context.alternateActions, context.alternate_actions, context.availableActions, context.available_actions, context.actions);
  if (!Array.isArray(source)) return [];
  const current = normalizedToken(firstDefined(context.currentAction, context.current_action, context.action));
  const result = [];
  for (const item of source) {
    const value = typeof item === 'string' ? { action: item } : isRecord(item) ? structuredClone(item) : null;
    if (value === null) continue;
    const action = normalizedIdentifier(firstDefined(value.action, value.type, value.name), null);
    if (action === null || FINAL_SUBMIT_ACTIONS.has(normalizedToken(action))) continue;
    if (current && normalizedToken(action) === current) continue;
    result.push({ ...value, action, candidateIndex: result.length });
  }
  return result;
}

function defaultStrategy(failureClass) {
  if (failureClass === 'validation') return 'validation_recheck';
  if (failureClass === 'stale_observation' || failureClass === 'observation') return 'fresh_observation';
  if (failureClass === 'navigation') return 'route_recheck';
  if (failureClass === 'action') return 'alternate_control';
  if (failureClass === 'transient') return 'transient_retry';
  if (failureClass === 'captcha') return 'captcha_resolution';
  return 'bounded_recovery';
}

function planForClass(failureClass) {
  if (failureClass === 'submission_uncertain') {
    return ['reconcile_submission', 'reobserve', 'stop'];
  }
  if (failureClass === 'user_required') return ['escalate_user'];
  if (failureClass === 'access_control') return ['escalate_access_control'];
  if (failureClass === 'stale_observation' || failureClass === 'observation') {
    return ['reobserve', 'retry_changed_strategy', 'alternate_action'];
  }
  if (failureClass === 'validation' || failureClass === 'action') {
    return ['reobserve', 'alternate_action', 'retry_changed_strategy'];
  }
  return ['reobserve', 'retry_changed_strategy', 'alternate_action'];
}

function preservationFor(context) {
  const run = isRecord(context.run) ? context.run : {};
  const workspace = isRecord(context.workspace) ? context.workspace : {};
  return {
    sameRun: true,
    sameWorkspace: true,
    preserveRun: true,
    preserveWorkspace: true,
    runId: firstDefined(context.runId, context.run_id, run.runId, run.run_id, null),
    workspacePath: firstDefined(
      context.workspacePath,
      context.workspace_path,
      workspace.path,
      workspace.workspacePath,
      workspace.workspace_path,
      null,
    ),
  };
}

export function planRecovery(failure = {}, context = undefined) {
  const args = planInput(failure, context);
  const failureResult = classifyFailure(args.failure);
  const options = args.context;
  let budget;
  const budgetInput = firstDefined(options.retryBudget, options.retry_budget, options.retryState, options.retry_state);
  try {
    budget = validateRetryBudget(budgetInput ?? {});
  } catch (error) {
    if (error instanceof RecoveryError) throw error;
    throw new RecoveryError('INVALID_RETRY_BUDGET', 'retry budget is invalid', { cause: error.message });
  }
  const retries = budget.retryCount;
  const candidates = recoveryCandidates(options);
  const preservation = preservationFor(options);
  let steps = planForClass(failureResult.failureClass);
  let step = steps[0];
  let retryAllowed = failureResult.retryable && !budget.exhausted;
  let alternateAction = null;
  if (failureResult.terminal) {
    retryAllowed = false;
  } else if (failureResult.uncertain) {
    retryAllowed = false;
  } else if (budget.exhausted) {
    steps = ['stop'];
    step = 'stop';
  } else if (retries > 0 && candidates.length > 0) {
    alternateAction = candidates[0];
    step = 'alternate_action';
    steps = ['alternate_action', 'reobserve', 'retry_changed_strategy'];
  } else if (step === 'alternate_action' && candidates.length === 0) {
    step = 'reobserve';
  }
  if (step === 'alternate_action' && alternateAction === null && candidates.length > 0) alternateAction = candidates[0];
  const strategy = failureResult.uncertain
    ? 'submission_reconciliation'
    : normalizedIdentifier(firstDefined(options.strategy, options.currentStrategy, options.current_strategy), null)
      ?? defaultStrategy(failureResult.failureClass);
  const proposedAction = failureResult.terminal
    ? { type: 'escalate', target: failureResult.escalation }
    : failureResult.uncertain
      ? { type: 'reconcile_submission', action: 'reobserve' }
      : step === 'alternate_action' && alternateAction !== null
        ? alternateAction
        : { type: step, strategy };
  const finalSubmitForbidden = failureResult.uncertain || failureResult.terminal || step === 'stop';
  const plan = {
    schema: RECOVERY_SCHEMA,
    failure: failureResult,
    failureClass: failureResult.failureClass,
    step,
    nextStep: step,
    steps,
    escalationOrder: ESCALATION_ORDER,
    strategy,
    alternateActions: candidates,
    alternateAction,
    proposedAction,
    retryAllowed,
    canRetry: retryAllowed,
    retryCount: retries,
    retryBudget: budget,
    budgetExhausted: budget.exhausted,
    changedStrategyRequired: !failureResult.terminal && !failureResult.uncertain,
    newObservationRequired: !failureResult.terminal,
    finalSubmitForbidden,
    duplicateFinalSubmitForbidden: finalSubmitForbidden,
    forbiddenActions: finalSubmitForbidden ? ['final_submit', 'submit'] : [],
    terminal: failureResult.terminal,
    recoverable: failureResult.recoverable,
    requiresUser: failureResult.requiresUser,
    requiresAccessControl: failureResult.requiresAccessControl,
    genericUserBlocker: false,
    preservation,
    sameRun: true,
    sameWorkspace: true,
    preserveRun: true,
    preserveWorkspace: true,
    runId: preservation.runId,
    workspacePath: preservation.workspacePath,
  };
  return immutable(plan);
}

function outcomeToken(value) {
  return normalizedToken(value);
}

function observationSubmissionSignal(observation) {
  if (!isRecord(observation)) {
    return { success: false, failed: false, uncertain: false, captcha: false, observationId: null };
  }
  const observationId = normalizedIdentifier(firstDefined(observation.observationId, observation.observation_id, observation.id), null);
  const explicitStatus = outcomeToken(firstDefined(
    observation.submissionStatus,
    observation.submission_status,
    observation.status,
    observation.outcome,
    observation.result,
  ));
  const signalText = normalizedText(firstDefined(
    observation.title,
    observation.url,
    observation.finalUrl,
    observation.final_url,
    observation.text,
    observation.message,
  ), '').toLowerCase();
  if (explicitStatus.includes('captcha')
    || observation.captcha === true
    || observation.captchaRequired === true
    || observation.captcha_required === true
    || /(?:captcha|recaptcha|hcaptcha|turnstile)/u.test(signalText)) {
    return { success: false, failed: true, uncertain: false, captcha: true, observationId };
  }
  if (SUCCESS_OUTCOMES.has(explicitStatus)
    || observation.submitted === true
    || observation.confirmation === true
    || observation.confirmed === true
    || observation.success === true) {
    return { success: true, failed: false, uncertain: false, captcha: false, observationId };
  }
  if (BLOCKED_OUTCOMES.has(explicitStatus)
    || observation.accessControl === true
    || observation.access_control === true
    || observation.userRequired === true
    || observation.user_required === true) {
    return { success: false, failed: true, uncertain: false, captcha: false, blocked: true, observationId };
  }
  if (FAILED_OUTCOMES.has(explicitStatus)
    || observation.submitted === false
    || observation.failed === true
    || observation.error === true) {
    return { success: false, failed: true, uncertain: false, captcha: false, observationId };
  }
  if (/(?:application|submission).{0,32}(?:success|submitted|received|thank\s*you)/u.test(signalText)
    || /thank\s*you.{0,32}(?:application|applying)/u.test(signalText)) {
    return { success: true, failed: false, uncertain: false, captcha: false, observationId };
  }
  return { success: false, failed: false, uncertain: true, captcha: false, observationId };
}

function submissionAttempts(input) {
  if (!isRecord(input)) return [];
  const source = firstDefined(input.attempts, input.submissionAttempts, input.submission_attempts, input.history, input.submissionHistory);
  return Array.isArray(source) ? source.map((entry) => (isRecord(entry) ? structuredClone(entry) : { outcome: entry })) : [];
}

function previousSubmission(input) {
  const nested = firstDefined(input.previous, input.prior, input.reconciliation, input.submissionState, input.submission_state);
  if (isRecord(nested)) return nested;
  const prior = firstDefined(input.priorState, input.prior_state, input.previousState, input.previous_state);
  if (isRecord(prior)) return prior;
  return {};
}

function normalizedAttemptId(input, previous) {
  return normalizedIdentifier(firstDefined(
    input.attemptId,
    input.attempt_id,
    input.actionId,
    input.action_id,
    previous.attemptId,
    previous.attempt_id,
  ), null);
}

export function reconcileSubmission(input = {}, options = undefined) {
  const value = isRecord(input) ? input : { outcome: input };
  const extra = isRecord(options) ? options : {};
  const previous = previousSubmission(value);
  const attempts = submissionAttempts(previous);
  const previousStatus = outcomeToken(firstDefined(previous.status, previous.outcome, previous.submissionStatus, previous.submission_status));
  const priorUncertain = hasUncertainSubmission(previous)
    || attempts.some((entry) => UNCERTAIN_OUTCOMES.has(outcomeToken(firstDefined(entry.outcome, entry.status))));
  const action = normalizedToken(firstDefined(value.action, extra.action));
  const attemptId = normalizedAttemptId(value, previous);
  const previousAttemptId = normalizedIdentifier(firstDefined(previous.attemptId, previous.attempt_id, attempts.at(-1)?.attemptId, attempts.at(-1)?.attempt_id), null);
  const isNewFinalSubmit = FINAL_SUBMIT_ACTIONS.has(action)
    && (attemptId === null || previousAttemptId === null || attemptId !== previousAttemptId || priorUncertain);
  const rawOutcome = firstDefined(value.outcome, value.result, value.submissionOutcome, value.submission_outcome, value.status);
  const rawToken = outcomeToken(rawOutcome);
  const observation = firstDefined(value.observation, value.postSubmitObservation, value.post_submit_observation, extra.observation);
  const signal = observationSubmissionSignal(observation);
  if (isNewFinalSubmit && (priorUncertain || previousStatus === 'succeeded' || previousStatus === 'completed')) {
    recoveryFail('DUPLICATE_FINAL_SUBMIT', 'a final submit cannot be issued until the previous submission is reconciled', {
      attemptId,
      previousAttemptId,
      previousStatus: previousStatus || null,
    });
  }
  let status;
  let failureClass = null;
  if (signal.captcha || rawToken.includes('captcha')) {
    status = 'failed';
    failureClass = 'captcha';
  } else if (signal.success || SUCCESS_OUTCOMES.has(rawToken)) {
    status = 'succeeded';
  } else if (signal.blocked || BLOCKED_OUTCOMES.has(rawToken)) {
    status = 'blocked';
    failureClass = rawToken === 'user_required' || rawToken === 'needs_user' ? 'user_required' : 'access_control';
  } else if (signal.failed || FAILED_OUTCOMES.has(rawToken)) {
    status = 'failed';
    failureClass = classifyFailure(value).failureClass;
    if (failureClass === 'unknown' || failureClass === 'submission_uncertain') failureClass = 'action';
  } else if (rawToken === 'attempted' || rawToken === 'started' || rawToken === 'in_progress') {
    status = 'pending';
  } else {
    status = 'uncertain';
    failureClass = 'submission_uncertain';
  }
  if (status === 'uncertain' && previousStatus === 'succeeded') status = 'succeeded';
  if (status === 'uncertain' && previousStatus === 'failed' && !priorUncertain && rawOutcome === undefined && observation === undefined) {
    status = 'failed';
    failureClass = 'action';
  }
  const observationId = signal.observationId
    ?? normalizedIdentifier(firstDefined(value.observationId, value.observation_id), null);
  const freshObservation = observationId !== null && observationId !== previous.observationId && observationId !== previous.observation_id;
  const unresolved = status === 'uncertain' || status === 'pending';
  const terminal = status === 'blocked' || status === 'succeeded';
  const finalSubmitForbidden = unresolved || terminal;
  const canSubmit = status === 'failed' && freshObservation;
  const attempt = {
    attemptId,
    outcome: status,
    status,
    observationId,
    reconciled: !unresolved,
  };
  const nextAttempts = attempts.filter((entry) => {
    const entryId = normalizedIdentifier(firstDefined(entry.attemptId, entry.attempt_id), null);
    return !(attemptId !== null && entryId === attemptId);
  });
  nextAttempts.push(attempt);
  const preservation = preservationFor({ ...previous, ...value, ...extra });
  return immutable({
    schema: SUBMISSION_RECONCILIATION_SCHEMA,
    attemptId,
    previousAttemptId,
    attempts: nextAttempts,
    status,
    outcome: status,
    result: status,
    failureClass,
    reconciled: !unresolved,
    submissionResolved: !unresolved,
    success: status === 'succeeded',
    failed: status === 'failed',
    uncertain: status === 'uncertain',
    pending: status === 'pending',
    blocked: status === 'blocked',
    terminal,
    retryAllowed: canSubmit,
    canRetry: canSubmit,
    finalSubmitAllowed: canSubmit,
    allowFinalSubmit: canSubmit,
    canSubmit,
    requiresReconciliation: unresolved,
    requiresFreshObservation: unresolved || status === 'failed',
    freshObservation,
    finalSubmitForbidden,
    duplicateFinalSubmitForbidden: finalSubmitForbidden,
    forbiddenActions: finalSubmitForbidden ? ['final_submit', 'submit'] : [],
    nextStep: status === 'succeeded'
      ? 'stop'
      : status === 'blocked'
        ? failureClass === 'user_required' ? 'escalate_user' : 'escalate_access_control'
        : 'reobserve',
    preservation,
    sameRun: true,
    sameWorkspace: true,
    preserveRun: true,
    preserveWorkspace: true,
    runId: preservation.runId,
    workspacePath: preservation.workspacePath,
  });
}

export function canIssueFinalSubmit(reconciliation = {}) {
  if (!isRecord(reconciliation)) return false;
  return reconciliation.finalSubmitAllowed === true
    && reconciliation.duplicateFinalSubmitForbidden !== true
    && reconciliation.finalSubmitForbidden !== true;
}

export function recoveryPlanIsImmutable(plan) {
  return plan !== null && typeof plan === 'object' && Object.isFrozen(plan);
}
