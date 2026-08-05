const CATEGORIES = new Set(['schools', 'degrees', 'disciplines']);
const FETCH_KEYS = new Set([
  'boardToken',
  'category',
  'queryText',
  'fetchImpl',
  'maxPages',
  'maxItems',
  'maxProbes',
  'timeoutMs',
  'signal',
]);
const RESOLVE_KEYS = new Set([
  'boardToken',
  'category',
  'value',
  'fetchImpl',
  'maxPages',
  'maxItems',
  'maxProbes',
  'timeoutMs',
  'signal',
]);

const MAX_RESPONSE_BYTES = 1024 * 1024;
const MAX_BOARD_TOKEN_LENGTH = 256;
const MAX_QUERY_LENGTH = 4096;
const MAX_PAGES = 1000;
const MAX_ITEMS = 100_000;
const MAX_PROBES = 100;
const MAX_TIMEOUT_MS = 120_000;
const DEFAULT_MAX_PAGES = 100;
const DEFAULT_MAX_ITEMS = 10_000;
const DEFAULT_MAX_PROBES = 5;
const DEFAULT_TIMEOUT_MS = 10_000;
const CONTROL_CHARACTERS = /[\u0000-\u001f\u007f-\u009f]/u;
const TIMEOUT_ABORT = Symbol('greenhouse-timeout-abort');
const CALLER_ABORT = Symbol('greenhouse-caller-abort');

export class GreenhouseCatalogError extends Error {
  constructor(code, message = code, options = {}) {
    super(message);
    this.name = 'GreenhouseCatalogError';
    this.code = code;
    if (options.cause !== undefined) this.cause = options.cause;
    if (options.status !== undefined) this.status = options.status;
  }
}

function fail(code, message = code, options = {}) {
  throw new GreenhouseCatalogError(code, message, options);
}

function isRecord(value) {
  return value !== null && typeof value === 'object' && !Array.isArray(value);
}

function hasExactKeys(value, keys) {
  if (!isRecord(value)) return false;
  const ownKeys = Reflect.ownKeys(value);
  return ownKeys.length === keys.length
    && ownKeys.every((key) => typeof key === 'string' && keys.includes(key))
    && keys.every((key) => Object.hasOwn(value, key));
}

function freezeDeep(value) {
  if (value === null || typeof value !== 'object' || Object.isFrozen(value)) return value;
  for (const child of Object.values(value)) freezeDeep(child);
  return Object.freeze(value);
}

function validateInputObject(input, allowedKeys) {
  if (!isRecord(input)) fail('E_GREENHOUSE_ARGUMENT', 'options must be an object');
  const prototype = Object.getPrototypeOf(input);
  if (prototype !== Object.prototype && prototype !== null) {
    fail('E_GREENHOUSE_ARGUMENT', 'options must be a plain object');
  }
  for (const key of Reflect.ownKeys(input)) {
    if (typeof key !== 'string' || !allowedKeys.has(key)) {
      fail('E_GREENHOUSE_ARGUMENT', 'unknown option');
    }
  }
}

function readOption(input, key, defaultValue) {
  return input[key] === undefined ? defaultValue : input[key];
}

function validateString(value, { name, maxLength, nonEmpty = true, trimRequired = false } = {}) {
  if (typeof value !== 'string') fail('E_GREENHOUSE_ARGUMENT', `${name} must be a string`);
  if (value.length > maxLength) fail('E_GREENHOUSE_ARGUMENT', `${name} is too long`);
  if (nonEmpty && value.trim().length === 0) {
    fail('E_GREENHOUSE_ARGUMENT', `${name} must not be empty`);
  }
  if (trimRequired && value.trim() !== value) {
    fail('E_GREENHOUSE_ARGUMENT', `${name} must not have surrounding whitespace`);
  }
  if (CONTROL_CHARACTERS.test(value)) {
    fail('E_GREENHOUSE_ARGUMENT', `${name} contains control characters`);
  }
  try {
    encodeURIComponent(value);
  } catch (error) {
    fail('E_GREENHOUSE_ARGUMENT', `${name} is not valid text`, { cause: error });
  }
  return value;
}

function isAbortSignal(value) {
  return typeof AbortSignal !== 'undefined' && value instanceof AbortSignal;
}

function validateBoundedNumber(value, { name, max, integer = true }) {
  if (integer ? !Number.isSafeInteger(value) : !Number.isFinite(value)) {
    fail('E_GREENHOUSE_ARGUMENT', `${name} must be a bounded number`);
  }
  if (value < 1 || value > max) {
    fail('E_GREENHOUSE_ARGUMENT', `${name} is outside its allowed range`);
  }
  return value;
}

function validateOptions(input, allowedKeys, textKey) {
  validateInputObject(input, allowedKeys);
  if (!Object.hasOwn(input, 'boardToken') || !Object.hasOwn(input, 'category') || !Object.hasOwn(input, textKey)) {
    fail('E_GREENHOUSE_ARGUMENT', 'required option missing');
  }

  const boardToken = validateString(input.boardToken, {
    name: 'boardToken',
    maxLength: MAX_BOARD_TOKEN_LENGTH,
    trimRequired: true,
  });
  if (typeof input.category !== 'string' || !CATEGORIES.has(input.category)) {
    fail('E_GREENHOUSE_ARGUMENT', 'invalid category');
  }
  const text = validateString(input[textKey], {
    name: textKey,
    maxLength: MAX_QUERY_LENGTH,
  });
  const fetchImpl = readOption(input, 'fetchImpl', globalThis.fetch);
  const maxPages = validateBoundedNumber(readOption(input, 'maxPages', DEFAULT_MAX_PAGES), {
    name: 'maxPages',
    max: MAX_PAGES,
  });
  const maxItems = validateBoundedNumber(readOption(input, 'maxItems', DEFAULT_MAX_ITEMS), {
    name: 'maxItems',
    max: MAX_ITEMS,
  });
  const maxProbes = validateBoundedNumber(readOption(input, 'maxProbes', DEFAULT_MAX_PROBES), {
    name: 'maxProbes',
    max: MAX_PROBES,
  });
  const timeoutMs = validateBoundedNumber(readOption(input, 'timeoutMs', DEFAULT_TIMEOUT_MS), {
    name: 'timeoutMs',
    max: MAX_TIMEOUT_MS,
    integer: false,
  });
  const signal = readOption(input, 'signal', null);

  if (typeof fetchImpl !== 'function') fail('E_GREENHOUSE_ARGUMENT', 'fetchImpl must be a function');
  if (signal !== null && !isAbortSignal(signal)) {
    fail('E_GREENHOUSE_ARGUMENT', 'signal must be an AbortSignal');
  }

  return {
    boardToken,
    category: input.category,
    text,
    fetchImpl,
    maxPages,
    maxItems,
    maxProbes,
    timeoutMs,
    signal,
  };
}

export function normalizeGreenhouseCatalogText(value) {
  if (typeof value !== 'string') fail('E_GREENHOUSE_ARGUMENT', 'catalog text must be a string');
  return value.trim().replace(/\s+/gu, ' ').toLowerCase();
}
function catalogMatchKey(value) {
  return normalizeGreenhouseCatalogText(value)
    .normalize('NFKD')
    .replace(/\p{M}/gu, '')
    .replace(/\([^)]*\)/gu, ' ')
    .replace(/&/gu, ' and ')
    .replace(/[^a-z0-9]+/gu, ' ')
    .trim()
    .replace(/\s+/gu, ' ');
}


function cloneNormalizedItem(item) {
  return freezeDeep({ value: item.value, label: item.label });
}

function isNormalizedItem(item) {
  return hasExactKeys(item, ['value', 'label'])
    && typeof item.value === 'string'
    && item.value.length > 0
    && typeof item.label === 'string'
    && item.label.trim().length > 0;
}

export function findExactGreenhouseEducationOption(items, value) {
  if (!Array.isArray(items) || typeof value !== 'string') {
    fail('E_GREENHOUSE_ARGUMENT', 'items and value are required');
  }
  for (const item of items) {
    if (!isNormalizedItem(item)) fail('E_GREENHOUSE_ARGUMENT', 'invalid normalized catalog item');
  }
  const target = catalogMatchKey(value);
  const matches = items.filter((item) => catalogMatchKey(item.label) === target);
  if (matches.length > 1) fail('E_GREENHOUSE_AMBIGUOUS', 'multiple exact catalog options matched');
  return matches.length === 0 ? null : cloneNormalizedItem(matches[0]);
}
function catalogKeyWords(value) {
  return new Set(catalogMatchKey(value).split(' '));
}

function degreeLevelKey(value) {
  const key = catalogMatchKey(value);
  if (/\b(phd|doctorate|doctoral)\b/.test(key)) return 'doctorate';
  if (/\b(master|mba|m\.s\.|m\.a\.)\b/.test(key)) return 'master';
  if (/\b(bachelor|bs|ba|b\.s\.|b\.a\.)\b/.test(key)) return 'bachelor';
  if (/\b(high school|ged)\b/.test(key)) return 'high school';
  return null;
}

function findDegreeLevelOption(items, value) {
  const level = degreeLevelKey(value);
  if (level === null) return null;
  const matches = items.filter((item) => {
    const itemKey = catalogMatchKey(item.label);
    if (itemKey.startsWith(level)) return true;
    const valueWords = catalogKeyWords(value);
    const itemWords = catalogKeyWords(item.label);
    return [...itemWords].some((word) => valueWords.has(word)) && itemKey.includes(level);
  });
  if (matches.length === 0) return null;
  matches.sort((a, b) => a.label.length - b.label.length);
  return cloneNormalizedItem(matches[0]);
}

export function findGreenhouseEducationOption(items, value, category) {
  const exact = findExactGreenhouseEducationOption(items, value);
  if (exact !== null) return exact;
  if (category === 'degrees') return findDegreeLevelOption(items, value);
  return null;
}

function buildProbeQueries(value, maxProbes) {
  const words = value.trim().replace(/\s+/gu, ' ').split(' ');
  const probes = [];
  for (let wordCount = words.length; wordCount > 0 && probes.length < maxProbes; wordCount -= 1) {
    probes.push(words.slice(0, wordCount).join(' '));
  }
  return probes;
}
function buildEducationProbes(value, maxProbes, category) {
  const probes = buildProbeQueries(value, maxProbes);
  if (category !== 'degrees') return probes;
  const level = degreeLevelKey(value);
  if (level === null) return probes;
  const levelQuery = level === 'high school' ? 'High School' : `${level.charAt(0).toUpperCase() + level.slice(1)}`;
  const existingIndex = probes.findIndex((probe) => catalogMatchKey(probe) === catalogMatchKey(levelQuery));
  if (existingIndex >= 0) {
    probes.splice(existingIndex, 1);
  }
  probes.unshift(levelQuery);
  return probes.slice(0, maxProbes);
}

function makeCatalogUrl(boardToken, category, queryText, page) {
  try {
    return `https://boards.greenhouse.io/v1/boards/${encodeURIComponent(boardToken)}/education/${category}?term=${encodeURIComponent(queryText)}&page=${page}`;
  } catch (error) {
    fail('E_GREENHOUSE_ARGUMENT', 'catalog URL could not be encoded', { cause: error });
  }
}

function getHeader(headers, name) {
  if (!headers) return null;
  if (typeof headers.get === 'function') return headers.get(name);
  for (const key of Object.keys(headers)) {
    if (key.toLowerCase() === name.toLowerCase()) return headers[key];
  }
  return null;
}

function successfulResponse(response) {
  return isRecord(response)
    && response.ok === true
    && Number.isInteger(response.status)
    && response.status >= 200
    && response.status < 300;
}

async function readResponseBody(response, abortPromise) {
  if (response.body && typeof response.body.getReader === 'function') {
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let bytesRead = 0;
    let body = '';
    try {
      while (true) {
        const { done, value } = await Promise.race([reader.read(), abortPromise]);
        if (done) break;
        if (!value) continue;
        bytesRead += value.byteLength;
        if (bytesRead > MAX_RESPONSE_BYTES) {
          await reader.cancel().catch(() => {});
          fail('E_GREENHOUSE_BOUND', 'response byte bound exceeded');
        }
        body += decoder.decode(value, { stream: true });
      }
      return body + decoder.decode();
    } catch (error) {
      await reader.cancel().catch(() => {});
      throw error;
    } finally {
      if (typeof reader.releaseLock === 'function') reader.releaseLock();
    }
  }
  if (typeof response.text !== 'function') return undefined;
  const body = await Promise.race([
    Promise.resolve().then(() => response.text()),
    abortPromise,
  ]);
  if (typeof body === 'string' && new TextEncoder().encode(body).byteLength > MAX_RESPONSE_BYTES) {
    fail('E_GREENHOUSE_BOUND', 'response byte bound exceeded');
  }
  return body;
}

async function requestCatalogPage({ fetchImpl, url, timeoutMs, signal }) {
  const controller = new AbortController();
  let rejectAbort;
  let timedOut = false;
  let callerAborted = false;
  const abortPromise = new Promise((_, reject) => {
    rejectAbort = reject;
  });
  const onCallerAbort = () => {
    if (callerAborted || timedOut) return;
    callerAborted = true;
    controller.abort();
    rejectAbort(CALLER_ABORT);
  };
  const timer = setTimeout(() => {
    if (callerAborted) return;
    timedOut = true;
    controller.abort();
    rejectAbort(TIMEOUT_ABORT);
  }, timeoutMs);

  if (signal !== null) {
    if (signal.aborted) onCallerAbort();
    else signal.addEventListener('abort', onCallerAbort, { once: true });
  }

  try {
    const response = await Promise.race([
      Promise.resolve().then(() => fetchImpl(url, { method: 'GET', signal: controller.signal })),
      abortPromise,
    ]);
    if (!successfulResponse(response)) {
      fail('E_GREENHOUSE_HTTP', `HTTP ${response?.status ?? 'unknown'}`, {
        status: response?.status,
      });
    }
    const contentType = getHeader(response.headers, 'content-type');
    if (!contentType || !contentType.toLowerCase().includes('application/json')) {
      fail('E_GREENHOUSE_HTTP', 'response content-type is not JSON');
    }
    const contentLength = getHeader(response.headers, 'content-length');
    if (contentLength !== null && contentLength !== undefined) {
      const length = Number(contentLength);
      if (Number.isFinite(length) && length > MAX_RESPONSE_BYTES) {
        fail('E_GREENHOUSE_BOUND', 'declared Content-Length exceeds byte bound');
      }
    }
    return await readResponseBody(response, abortPromise);
  } catch (error) {
    if (error === TIMEOUT_ABORT || error === CALLER_ABORT || timedOut || callerAborted || error?.name === 'AbortError') {
      fail('E_GREENHOUSE_TIMEOUT', 'Greenhouse catalog request timed out or was aborted', { cause: error });
    }
    if (error instanceof GreenhouseCatalogError) throw error;
    fail('E_GREENHOUSE_HTTP', error?.message || 'Greenhouse catalog request failed', { cause: error });
  } finally {
    clearTimeout(timer);
    if (signal !== null) signal.removeEventListener('abort', onCallerAbort);
  }
}

function parseCatalogPage(body) {
  if (typeof body !== 'string') fail('E_GREENHOUSE_RESPONSE', 'response body must be text');
  let payload;
  try {
    if (new TextEncoder().encode(body).byteLength > MAX_RESPONSE_BYTES) {
      fail('E_GREENHOUSE_BOUND', 'response byte bound exceeded');
    }
    payload = JSON.parse(body);
  } catch (error) {
    if (error instanceof GreenhouseCatalogError) throw error;
    fail('E_GREENHOUSE_RESPONSE', 'response was not valid JSON', { cause: error });
  }

  if (!hasExactKeys(payload, ['items', 'meta']) || !Array.isArray(payload.items)
    || !hasExactKeys(payload.meta, ['total_count', 'per_page'])) {
    fail('E_GREENHOUSE_RESPONSE', 'response shape is invalid');
  }
  const { total_count: totalCount, per_page: perPage } = payload.meta;
  if (!Number.isSafeInteger(totalCount) || totalCount < 0 || !Number.isSafeInteger(perPage) || perPage < 1) {
    fail('E_GREENHOUSE_RESPONSE', 'response pagination metadata is invalid');
  }
  if (perPage > MAX_ITEMS) fail('E_GREENHOUSE_BOUND', 'provider page size exceeds bound');
  if (payload.items.length > perPage) fail('E_GREENHOUSE_RESPONSE', 'response page contains too many items');

  const items = payload.items.map((rawItem) => {
    if (!hasExactKeys(rawItem, ['id', 'text'])
      || typeof rawItem.id !== 'number'
      || !Number.isSafeInteger(rawItem.id)
      || rawItem.id <= 0
      || typeof rawItem.text !== 'string'
      || rawItem.text.trim().length === 0
      || rawItem.text.trim() !== rawItem.text
      || CONTROL_CHARACTERS.test(rawItem.text)
      || rawItem.text.length > MAX_QUERY_LENGTH) {
      fail('E_GREENHOUSE_RESPONSE', 'response item shape is invalid');
    }
    return freezeDeep({ value: String(rawItem.id), label: rawItem.text });
  });

  return { totalCount, perPage, items };
}


export async function fetchGreenhouseEducationCatalog(input = {}) {
  const {
    boardToken,
    category,
    text: queryText,
    fetchImpl,
    maxPages,
    maxItems,
    timeoutMs,
    signal,
  } = validateOptions(input, FETCH_KEYS, 'queryText');

  const items = [];
  const seenItemIds = new Set();
  const seenPageIdentities = new Set();
  let reportedTotal = null;
  let reportedPerPage = null;
  let itemsSeen = 0;
  let pagesFetched = 0;
  let complete = false;

  for (let page = 1; page <= maxPages; page += 1) {
    const url = makeCatalogUrl(boardToken, category, queryText, page);
    const body = await requestCatalogPage({ fetchImpl, url, timeoutMs, signal });
    const parsed = parseCatalogPage(body);

    if (reportedTotal === null) {
      reportedTotal = parsed.totalCount;
      reportedPerPage = parsed.perPage;
      if (reportedTotal > maxItems) {
        fail('E_GREENHOUSE_BOUND', 'provider total exceeds item bound');
      }
    } else {
      if (parsed.totalCount !== reportedTotal || parsed.perPage !== reportedPerPage) {
        fail('E_GREENHOUSE_TOTAL_MISMATCH', 'provider pagination metadata changed');
      }
    }

    const identity = JSON.stringify(parsed.items.map((item) => item.value));
    if (seenPageIdentities.has(identity)) {
      fail('E_GREENHOUSE_REPEATED_PAGE', 'provider repeated an ordered page');
    }
    seenPageIdentities.add(identity);
    for (const item of parsed.items) {
      if (seenItemIds.has(item.value)) {
        fail('E_GREENHOUSE_REPEATED_PAGE', 'provider returned overlapping or duplicate item IDs');
      }
      seenItemIds.add(item.value);
    }

    itemsSeen += parsed.items.length;
    if (itemsSeen > maxItems) fail('E_GREENHOUSE_BOUND', 'item bound exceeded');
    if (itemsSeen > reportedTotal) {
      fail('E_GREENHOUSE_TOTAL_MISMATCH', 'provider returned more items than reported');
    }
    items.push(...parsed.items);
    pagesFetched += 1;

    if (itemsSeen === reportedTotal) {
      complete = true;
      break;
    }
    if (parsed.items.length === 0 || parsed.items.length < parsed.perPage) {
      fail('E_GREENHOUSE_TOTAL_MISMATCH', 'provider stopped before reported total');
    }
  }

  if (!complete) fail('E_GREENHOUSE_BOUND', 'page bound exceeded before catalog completed');
  return freezeDeep({
    category,
    query_text: queryText,
    items,
    pages_fetched: pagesFetched,
    items_seen: itemsSeen,
    reported_total: reportedTotal,
  });
}

export async function resolveGreenhouseEducationOption(input = {}) {
  const {
    boardToken,
    category,
    text: value,
    fetchImpl,
    maxPages,
    maxItems,
    maxProbes,
    timeoutMs,
    signal,
  } = validateOptions(input, RESOLVE_KEYS, 'value');
  const probes = buildEducationProbes(value, maxProbes, category);
  let pagesFetched = 0;
  let itemsSeen = 0;
  let probesUsed = 0;

  for (const queryText of probes) {
    probesUsed += 1;
    const catalog = await fetchGreenhouseEducationCatalog({
      boardToken,
      category,
      queryText,
      fetchImpl,
      maxPages,
      maxItems,
      timeoutMs,
      signal,
    });
    pagesFetched += catalog.pages_fetched;
    itemsSeen += catalog.items_seen;
    const match = findGreenhouseEducationOption(catalog.items, value, category);
    if (match !== null) {
      return freezeDeep({
        category,
        query_text: queryText,
        option_text: match.label,
        option_value: match.value,
        pages_fetched: pagesFetched,
        items_seen: itemsSeen,
        probes: probesUsed,
      });
    }
  }

  fail('E_GREENHOUSE_NOT_FOUND', 'no exact catalog option matched');
}
