import { performance } from 'node:perf_hooks';

const DEFAULT_TIMEOUT_MS = 5_000;
const DEFAULT_POLL_INTERVAL_MS = 50;
const MAX_OPEN_SETTLE_MS = 5_000;
const MAX_TIMEOUT_MS = 120_000;
const MAX_POLL_INTERVAL_MS = 120_000;
const MAX_POLL_ATTEMPTS = 1_024;
const MAX_OPTIONS = 256;
const MAX_TEXT_LENGTH = 512;
const MAX_KEY_LENGTH = 512;
const CONTROL_CHARACTERS = /[\u0000-\u001f\u007f-\u009f]/u;

const MATCH_STRATEGIES = new Set(['exact_text', 'exact_value', 'substring']);
const ERROR_CODES = new Set([
  'E_CUSTOM_SELECT_INVALID_INPUT',
  'E_CUSTOM_SELECT_CALLBACK',
  'E_CUSTOM_SELECT_OPTION_AMBIGUOUS',
  'E_CUSTOM_SELECT_OPTION_DISABLED',
  'E_CUSTOM_SELECT_OPTION_NOT_FOUND',
  'E_CUSTOM_SELECT_OPTION_TIMEOUT',
]);
const DIAGNOSTIC_KINDS = new Set([
  'input',
  'options',
  'target',
  'config',
  'openMenu',
  'readOptions',
  'prepareOptions',
  'clickOption',
  'now',
  'sleep',
  'timeout',
]);
const EXECUTOR_KEYS = new Set([
  'openMenu',
  'readOptions',
  'prepareOptions',
  'clickOption',
  'target',
  'timeoutMs',
  'pollIntervalMs',
  'now',
  'sleep',
]);
const CONFIG_KEYS = new Set(['maxOptions', 'maxTextLength', 'maxKeyLength']);

function isRecord(value) {
  if (value === null || typeof value !== 'object' || Array.isArray(value)) return false;
  const prototype = Object.getPrototypeOf(value);
  return prototype === null || Object.getPrototypeOf(prototype) === null;
}

function freezeDeep(value) {
  if (value === null || typeof value !== 'object' || Object.isFrozen(value)) return value;
  for (const child of Object.values(value)) freezeDeep(child);
  return Object.freeze(value);
}

function diagnosticDetails(input = {}) {
  if (!isRecord(input)) return Object.freeze({});
  const output = {};
  if (typeof input.kind === 'string' && DIAGNOSTIC_KINDS.has(input.kind)) output.kind = input.kind;
  if (typeof input.strategy === 'string' && MATCH_STRATEGIES.has(input.strategy)) {
    output.strategy = input.strategy;
  }
  if (Number.isSafeInteger(input.matchCount) && input.matchCount >= 0) {
    output.matchCount = Math.min(input.matchCount, MAX_OPTIONS);
  }
  if (Number.isSafeInteger(input.attempts) && input.attempts >= 0) {
    output.attempts = Math.min(input.attempts, MAX_POLL_ATTEMPTS);
  }
  if (typeof input.prepared === 'boolean') output.prepared = input.prepared;
  return freezeDeep(output);
}

export class CustomSelectExecutorError extends Error {
  constructor(code, details = {}) {
    const normalizedCode = typeof code === 'string' && ERROR_CODES.has(code)
      ? code
      : 'E_CUSTOM_SELECT_INVALID_INPUT';
    super(normalizedCode);
    this.name = 'CustomSelectExecutorError';
    this.code = normalizedCode;
    this.details = diagnosticDetails(details);
    Object.freeze(this);
  }
}

function fail(code, details = {}) {
  throw new CustomSelectExecutorError(code, details);
}

function invalid(kind = 'input') {
  fail('E_CUSTOM_SELECT_INVALID_INPUT', { kind });
}

function callbackFailure(kind) {
  fail('E_CUSTOM_SELECT_CALLBACK', { kind });
}

function assertExactKeys(value, keys, kind) {
  if (!isRecord(value)) invalid(kind);
  const allowed = new Set(keys);
  for (const key of Reflect.ownKeys(value)) {
    if (typeof key !== 'string' || !allowed.has(key)) invalid(kind);
  }
}

function boundedString(value, kind, maxLength, { allowEmpty = false } = {}) {
  if (typeof value !== 'string' || value.length > maxLength || CONTROL_CHARACTERS.test(value)) {
    invalid(kind);
  }
  if (!allowEmpty && value.trim().length === 0) invalid(kind);
  return value;
}

function boundedInteger(value, kind, minimum, maximum) {
  if (!Number.isSafeInteger(value) || value < minimum || value > maximum) invalid(kind);
  return value;
}

function normalizeMatchText(value) {
  return value
    .normalize('NFKD')
    .replace(/\p{M}/gu, '')
    .toLowerCase()
    .replace(/[^\p{L}\p{N}+#.&/]+/gu, ' ')
    .trim()
    .replace(/\s+/gu, ' ');
}

function words(value) {
  return value.length === 0 ? [] : value.split(' ');
}

function hasWordBoundarySubstring(candidate, query) {
  const candidateWords = words(candidate);
  const queryWords = words(query);
  if (queryWords.length === 0 || queryWords.length > candidateWords.length) return false;
  const lastStart = candidateWords.length - queryWords.length;
  for (let start = 0; start <= lastStart; start += 1) {
    let matches = true;
    for (let offset = 0; offset < queryWords.length; offset += 1) {
      if (candidateWords[start + offset] !== queryWords[offset]) {
        matches = false;
        break;
      }
    }
    if (matches) return true;
  }
  return false;
}

function normalizeConfig(config) {
  if (config === undefined) {
    return {
      maxOptions: MAX_OPTIONS,
      maxTextLength: MAX_TEXT_LENGTH,
      maxKeyLength: MAX_KEY_LENGTH,
    };
  }
  assertExactKeys(config, CONFIG_KEYS, 'config');
  const maxOptions = config.maxOptions === undefined
    ? MAX_OPTIONS
    : boundedInteger(config.maxOptions, 'config', 1, MAX_OPTIONS);
  const maxTextLength = config.maxTextLength === undefined
    ? MAX_TEXT_LENGTH
    : boundedInteger(config.maxTextLength, 'config', 1, MAX_TEXT_LENGTH);
  const maxKeyLength = config.maxKeyLength === undefined
    ? MAX_KEY_LENGTH
    : boundedInteger(config.maxKeyLength, 'config', 1, MAX_KEY_LENGTH);
  return { maxOptions, maxTextLength, maxKeyLength };
}

function normalizeTarget(target, config) {
  assertExactKeys(target, ['optionText', 'optionValue'], 'target');
  if (!Object.hasOwn(target, 'optionText') || !Object.hasOwn(target, 'optionValue')) invalid('target');
  const optionText = boundedString(target.optionText, 'target', config.maxTextLength);
  const optionValue = boundedString(target.optionValue, 'target', config.maxTextLength);
  const normalizedText = normalizeMatchText(optionText);
  const normalizedValue = normalizeMatchText(optionValue);
  if (normalizedText.length === 0 || normalizedValue.length === 0) invalid('target');
  return { normalizedText, normalizedValue };
}

function normalizeOption(option, index, config) {
  if (!isRecord(option)) invalid('options');
  if (!Object.hasOwn(option, 'key')
    || !Object.hasOwn(option, 'text')
    || !Object.hasOwn(option, 'value')
    || !Object.hasOwn(option, 'disabled')) {
    invalid('options');
  }
  const key = boundedString(option.key, 'options', config.maxKeyLength);
  const text = boundedString(option.text, 'options', config.maxTextLength);
  const value = boundedString(option.value, 'options', config.maxTextLength, { allowEmpty: true });
  if (typeof option.disabled !== 'boolean') invalid('options');
  return {
    index,
    key,
    text,
    value,
    disabled: option.disabled,
    normalizedText: normalizeMatchText(text),
    normalizedValue: normalizeMatchText(value),
  };
}

function cloneResult(option, strategy) {
  return freezeDeep({
    key: option.key,
    text: option.text,
    value: option.value,
    strategy,
  });
}

function resolveTier(matches, strategy) {
  if (matches.length === 0) return null;
  if (matches.length > 1) {
    fail('E_CUSTOM_SELECT_OPTION_AMBIGUOUS', {
      strategy,
      matchCount: matches.length,
    });
  }
  const [match] = matches;
  if (match.disabled) {
    fail('E_CUSTOM_SELECT_OPTION_DISABLED', {
      strategy,
      matchCount: 1,
    });
  }
  return cloneResult(match, strategy);
}

function resolveNormalizedOptions(options, target, config) {
  if (!Array.isArray(options) || options.length > config.maxOptions) invalid('options');
  const normalizedOptions = options.map((option, index) => normalizeOption(option, index, config));
  const normalizedTarget = normalizeTarget(target, config);

  const exactText = normalizedOptions.filter(
    (option) => option.normalizedText === normalizedTarget.normalizedText,
  );
  const exactTextResult = resolveTier(exactText, 'exact_text');
  if (exactTextResult !== null) return exactTextResult;

  const exactValue = normalizedOptions.filter(
    (option) => option.normalizedValue === normalizedTarget.normalizedValue,
  );
  const exactValueResult = resolveTier(exactValue, 'exact_value');
  if (exactValueResult !== null) return exactValueResult;

  const substring = normalizedOptions.filter((option) => (
    hasWordBoundarySubstring(option.normalizedText, normalizedTarget.normalizedText)
      || hasWordBoundarySubstring(normalizedTarget.normalizedText, option.normalizedText)
  ));
  const substringResult = resolveTier(substring, 'substring');
  if (substringResult !== null) return substringResult;

  fail('E_CUSTOM_SELECT_OPTION_NOT_FOUND', {
    strategy: 'substring',
    matchCount: 0,
  });
}

export function resolveCustomSelectOption(options, target, config = undefined) {
  const normalizedConfig = normalizeConfig(config);
  return resolveNormalizedOptions(options, target, normalizedConfig);
}

function normalizeExecutorInput(input) {
  assertExactKeys(input, EXECUTOR_KEYS, 'input');
  if (!Object.hasOwn(input, 'openMenu') || typeof input.openMenu !== 'function') invalid('input');
  if (!Object.hasOwn(input, 'readOptions') || typeof input.readOptions !== 'function') invalid('input');
  if (!Object.hasOwn(input, 'prepareOptions') || typeof input.prepareOptions !== 'function') invalid('input');
  if (!Object.hasOwn(input, 'clickOption') || typeof input.clickOption !== 'function') invalid('input');
  if (!Object.hasOwn(input, 'target')) invalid('input');
  if (input.now !== undefined && typeof input.now !== 'function') invalid('input');
  if (input.sleep !== undefined && typeof input.sleep !== 'function') invalid('input');

  const timeoutMs = input.timeoutMs === undefined
    ? DEFAULT_TIMEOUT_MS
    : boundedInteger(input.timeoutMs, 'input', 0, MAX_TIMEOUT_MS);
  const pollIntervalMs = input.pollIntervalMs === undefined
    ? DEFAULT_POLL_INTERVAL_MS
    : boundedInteger(input.pollIntervalMs, 'input', 0, MAX_POLL_INTERVAL_MS);
  return {
    openMenu: input.openMenu,
    readOptions: input.readOptions,
    prepareOptions: input.prepareOptions,
    clickOption: input.clickOption,
    target: input.target,
    timeoutMs,
    pollIntervalMs,
    now: input.now ?? (() => performance.now()),
    sleep: input.sleep ?? ((milliseconds) => new Promise((resolve) => {
      setTimeout(resolve, milliseconds);
    })),
  };
}


function maxAttempts(timeoutMs, pollIntervalMs) {
  if (timeoutMs === 0) return 1;
  if (pollIntervalMs === 0) return MAX_POLL_ATTEMPTS;
  return Math.min(
    MAX_POLL_ATTEMPTS,
    Math.max(1, Math.ceil(timeoutMs / pollIntervalMs) + 2),
  );
}

function makeClock(now) {
  let previous = null;
  return () => {
    let value;
    try {
      value = now();
    } catch (_) {
      callbackFailure('now');
    }
    if (typeof value !== 'number' || !Number.isFinite(value)) callbackFailure('now');
    if (previous !== null && value < previous) callbackFailure('now');
    previous = value;
    return value;
  };
}

const CALLBACK_TIMEOUT = Symbol('custom-select-callback-timeout');

async function invokeCallback(callback, args, kind, timeoutMs, attempts, prepared) {
  let timer = null;
  try {
    return await Promise.race([
      Promise.resolve().then(() => callback(...args)),
      new Promise((_, reject) => {
        timer = setTimeout(() => reject(CALLBACK_TIMEOUT), Math.max(0, timeoutMs));
      }),
    ]);
  } catch (error) {
    if (error === CALLBACK_TIMEOUT) {
      if (kind === 'readOptions' || kind === 'sleep') timeoutError(attempts, prepared);
      callbackFailure(kind);
    }
    callbackFailure(kind);
  } finally {
    clearTimeout(timer);
  }
}

async function openMenu(openMenuCallback, timeoutMs) {
  await invokeCallback(openMenuCallback, [], 'openMenu', timeoutMs, 0, false);
}

async function readOptions(readOptionsCallback, timeoutMs, attempts, prepared) {
  const options = await invokeCallback(
    readOptionsCallback,
    [],
    'readOptions',
    timeoutMs,
    attempts,
    prepared,
  );
  if (!Array.isArray(options)) invalid('options');
  return options;
}

async function prepareOptions(prepareOptionsCallback, timeoutMs, attempts) {
  await invokeCallback(
    prepareOptionsCallback,
    [],
    'prepareOptions',
    timeoutMs,
    attempts,
    true,
  );
}

async function clickOption(clickOptionCallback, candidate, timeoutMs, attempts, prepared) {
  await invokeCallback(
    clickOptionCallback,
    [candidate],
    'clickOption',
    timeoutMs,
    attempts,
    prepared,
  );
}

async function sleep(sleepCallback, milliseconds, timeoutMs, attempts, prepared) {
  await invokeCallback(
    sleepCallback,
    [milliseconds],
    'sleep',
    timeoutMs,
    attempts,
    prepared,
  );
}
function timeoutError(attempts, prepared, matchCount = 0) {
  fail('E_CUSTOM_SELECT_OPTION_TIMEOUT', { attempts, prepared, matchCount });
}

export async function executeCustomSelectOption(input = {}) {
  const normalized = normalizeExecutorInput(input);
  const config = normalizeConfig(undefined);
  const clock = makeClock(normalized.now);
  const start = clock();
  const deadline = start + normalized.timeoutMs;
  if (!Number.isFinite(deadline)) callbackFailure('now');

  await openMenu(normalized.openMenu, deadline - clock());

  const attemptLimit = maxAttempts(normalized.timeoutMs, normalized.pollIntervalMs);
  let attempts = 0;
  let prepared = false;
  const openSettleLimitMs = Math.min(
    MAX_OPEN_SETTLE_MS,
    Math.floor(normalized.timeoutMs / 3),
  );
  let openWaitedMs = 0;
  let maxOptionCount = 0;

  while (true) {
    if (attempts > 0 && clock() >= deadline) {
      timeoutError(attempts, prepared, maxOptionCount);
    }
    if (attempts >= attemptLimit) timeoutError(attempts, prepared, maxOptionCount);

    attempts += 1;
    const options = await readOptions(
      normalized.readOptions,
      deadline - clock(),
      attempts,
      prepared,
    );
    maxOptionCount = Math.max(maxOptionCount, options.length);
    let candidate = null;
    try {
      candidate = resolveNormalizedOptions(options, normalized.target, config);
    } catch (error) {
      if (!(error instanceof CustomSelectExecutorError)
        || error.code !== 'E_CUSTOM_SELECT_OPTION_NOT_FOUND') {
        throw error;
      }
    }

    const afterRead = clock();
    if (candidate !== null) {
      if (afterRead > deadline) timeoutError(attempts, prepared, maxOptionCount);
      await clickOption(
        normalized.clickOption,
        candidate,
        deadline - clock(),
        attempts,
        prepared,
      );
      return cloneResult(candidate, candidate.strategy);
    }

    if (afterRead >= deadline || attempts >= attemptLimit) {
      timeoutError(attempts, prepared, maxOptionCount);
    }

    if (!prepared && options.length === 0 && openWaitedMs < openSettleLimitMs) {
      const remaining = deadline - afterRead;
      const settleRemaining = openSettleLimitMs - openWaitedMs;
      const waitMs = Math.min(
        Math.max(1, normalized.pollIntervalMs),
        remaining,
        settleRemaining,
      );
      await sleep(
        normalized.sleep,
        waitMs,
        remaining,
        attempts,
        prepared,
      );
      openWaitedMs += waitMs;
      continue;
    }

    if (!prepared) {
      prepared = true;
      await prepareOptions(
        normalized.prepareOptions,
        deadline - clock(),
        attempts,
      );
      continue;
    }

    const remaining = deadline - afterRead;
    if (remaining <= 0) timeoutError(attempts, prepared, maxOptionCount);
    await sleep(
      normalized.sleep,
      Math.min(normalized.pollIntervalMs, remaining),
      remaining,
      attempts,
      prepared,
    );
  }
}
