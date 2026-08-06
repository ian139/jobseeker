import { performance } from 'node:perf_hooks';

/**
 * Reusable OMP-browser transport between canonical browser action plans and
 * the OMP browser tab surface. This factory is the only normal executor
 * transport: it returns the exact transport shape accepted by
 * `executeOmpBrowserActionPlan`:
 *   required: fill, click, press, select, uploadFile, readOptions,
 *             clickOption, observe
 *   optional: now, sleep
 *
 * `createOmpBrowserTransport(tab, options)`:
 *   - validates the tab capabilities (`fill`, `click`, `press`, `select`,
 *     `uploadFile`, `evaluate`) and the options exactly (`observe` required;
 *     `now` and `sleep` injectable for deterministic tests);
 *   - wraps every tab helper so failures sanitize to stable codes and raw
 *     field values never escape into errors or outcomes;
 *   - ordinary text/clear `fill` performs the exact `tab.fill`, detects
 *     custom `role=combobox` targets with a read-only evaluate and skips the
 *     browser-key commit there; otherwise it presses targeted End and Space
 *     and presses Backspace only when the Space sample proves the value
 *     appended, then requires the final DOM value to equal the planned exact
 *     value;
 *   - `uploadFile` performs the exact `tab.uploadFile`, derives the planned
 *     basename locally, and polls only read-only native `files` and rendered
 *     upload filename state through a bounded settle, requiring the planned
 *     basename to remain stably visible; transient, missing, and ambiguous
 *     selections fail closed;
 *   - `readOptions` emits bounded frozen key/text/value/disabled records
 *     preserving the disabled flag; `clickOption` re-reads the visible
 *     options and clicks the exact unique non-disabled candidate;
 *   - returns a frozen transport whose own keys are exactly the executor's
 *     allowed transport keys.
 *
 * The returned `now` and `sleep` are the injected ones (or monotonic
 * defaults) and drive both the upload settle loop and the custom-select
 * executor's clock.
 */

export const OMP_BROWSER_TRANSPORT_KEYS = Object.freeze([
  'fill',
  'click',
  'press',
  'select',
  'uploadFile',
  'readOptions',
  'clickOption',
  'observe',
  'now',
  'sleep',
]);

export const OMP_BROWSER_TRANSPORT_ERROR_CODES = Object.freeze([
  'E_OMP_BROWSER_TRANSPORT_INVALID',
  'E_OMP_BROWSER_TAB_FAILED',
  'E_OMP_BROWSER_FILL_UNVERIFIABLE',
  'E_OMP_BROWSER_FILL_MISMATCH',
  'E_OMP_BROWSER_UPLOAD_INVALID_PATH',
  'E_OMP_BROWSER_UPLOAD_MISSING',
  'E_OMP_BROWSER_UPLOAD_TRANSIENT',
  'E_OMP_BROWSER_UPLOAD_AMBIGUOUS',
  'E_OMP_BROWSER_OPTION_NOT_FOUND',
  'E_OMP_BROWSER_OPTION_AMBIGUOUS',
]);

const ERROR_CODE_SET = new Set(OMP_BROWSER_TRANSPORT_ERROR_CODES);
const REQUIRED_TAB_KEYS = Object.freeze([
  'fill',
  'click',
  'press',
  'select',
  'uploadFile',
  'evaluate',
]);
const OPTIONS_KEYS = new Set(['observe', 'now', 'sleep']);
const PRESS_KEYS = new Set(['ArrowDown', 'End', 'Space', 'Backspace']);
const COMBINED_ROLE = 'combobox';
const MAX_OPTIONS = 256;
const MAX_STRING_LENGTH = 512;
const MAX_ARGUMENT_LENGTH = 16 * 1024;
const FILL_SETTLE_MS = 50;
const UPLOAD_SETTLE_MS = 1_000;
const UPLOAD_POLL_INTERVAL_MS = 50;
const MAX_UPLOAD_ATTEMPTS = 1_024;
const BASENAME = /^(?!\.{1,2}$)(?!\s)[^/\\\u0000-\u001f\u007f]{1,255}$/u;

export class OmpBrowserTransportError extends Error {
  constructor(code) {
    const normalized = typeof code === 'string' && ERROR_CODE_SET.has(code)
      ? code
      : 'E_OMP_BROWSER_TRANSPORT_INVALID';
    super(normalized);
    this.name = 'OmpBrowserTransportError';
    this.code = normalized;
  }
}

function fail(code) {
  throw new OmpBrowserTransportError(code);
}

function isRecord(value) {
  if (value === null || typeof value !== 'object' || Array.isArray(value)) return false;
  const prototype = Object.getPrototypeOf(value);
  return prototype === Object.prototype || prototype === null;
}

/** Sanitized invocation of a tab helper; never leaks the raw rejection. */
async function invokeTab(tab, method, args) {
  try {
    return await tab[method](...args);
  } catch (_) {
    fail('E_OMP_BROWSER_TAB_FAILED');
  }
}

/** Sanitized clock reads; injected test clocks may be fallible. */
function clockNow(now) {
  try {
    return now();
  } catch (_) {
    fail('E_OMP_BROWSER_TAB_FAILED');
  }
}

/** Sanitized sleeps; injected test sleeps may be fallible. */
async function sleepFor(sleep, milliseconds) {
  try {
    await sleep(milliseconds);
  } catch (_) {
    fail('E_OMP_BROWSER_TAB_FAILED');
  }
}

/** Read-only role/value state of the fill target (browser context). */
function readFillState(selector) {
  const el = document.querySelector(selector);
  if (el === null) return { found: false, role: null, value: null };
  const role = typeof el.getAttribute === 'function' ? el.getAttribute('role') : null;
  const value = typeof el.value === 'string' ? el.value : (el.textContent ?? '');
  return { found: true, role, value };
}

/** Read-only native files and rendered upload filename state (browser context). */
function readFileState(selector) {
  const el = document.querySelector(selector);
  if (el !== null) {
    const files = el.files;
    const length = files === null || files === undefined ? 0 : files.length;
    const names = [];
    for (let index = 0; index < length; index += 1) {
      const name = files[index] && files[index].name;
      if (typeof name === 'string') names.push(name);
    }
    const rendered = typeof el.value === 'string' ? el.value : '';
    if (names.length > 0) {
      return { found: true, count: names.length, names, rendered };
    }
  }
  let container = el && typeof el.closest === 'function'
    ? el.closest('.file-upload, [role="group"]')
    : null;
  if (container === null && typeof selector === 'string' && selector.startsWith('#')) {
    const label = document.querySelector(`label[for="${selector.slice(1)}"]`);
    container = label && typeof label.closest === 'function'
      ? label.closest('.file-upload, [role="group"]')
      : null;
  }
  if (container !== null) {
    const filenameEl = container.querySelector('.file-upload__filename, [aria-label="Remove file"], [title="Delete file"]');
    if (filenameEl !== null) {
      const rawText = (filenameEl.textContent ?? '').trim();
      const basename = rawText.split(/[\\/]/).pop();
      if (basename.length > 0) {
        return { found: true, count: 1, names: [basename], rendered: basename };
      }
    }
  }
  return el === null
    ? { found: false, count: 0, names: [], rendered: null }
    : { found: true, count: 0, names: [], rendered: typeof el.value === 'string' ? el.value : '' };
}

/** Read-only visible option records with internal selectors (browser context). */
function readOptionRecords(maxOptions, maxStringLength) {
  const visible = (el) => {
    const style = window.getComputedStyle(el);
    const rect = el.getBoundingClientRect();
    return style.display !== 'none'
      && style.visibility !== 'hidden'
      && rect.width > 0
      && rect.height > 0;
  };
  const uniquePath = (el) => {
    if (typeof el.id === 'string' && el.id.length > 0) return `#${CSS.escape(el.id)}`;
    const parts = [];
    let node = el;
    while (node !== null && node.nodeType === 1) {
      const tag = node.tagName.toLowerCase();
      const parent = node.parentElement;
      let part = tag;
      if (parent !== null) {
        const siblings = Array.from(parent.children)
          .filter((child) => child.tagName === node.tagName);
        if (siblings.length > 1) part += `:nth-of-type(${siblings.indexOf(node) + 1})`;
      }
      parts.unshift(part);
      node = parent;
    }
    return parts.join(' > ');
  };
  const options = Array.from(document.querySelectorAll('[role="option"]')).filter(visible);
  const records = [];
  for (let index = 0; index < options.length && records.length < maxOptions; index += 1) {
    const el = options[index];
    const dataValue = el.getAttribute('data-value');
    const hasValue = dataValue !== null && dataValue.length > 0;
    const text = (el.textContent ?? '').trim();
    const rawValue = hasValue ? dataValue : text;
    records.push({
      key: (hasValue ? dataValue : String(index)).slice(0, maxStringLength),
      text: text.slice(0, maxStringLength),
      value: rawValue.slice(0, maxStringLength),
      disabled: el.getAttribute('aria-disabled') === 'true' || el.hasAttribute('disabled'),
      selector: uniquePath(el),
    });
  }
  return records;
}

function validateTab(tab) {
  if (tab === null || (typeof tab !== 'object' && typeof tab !== 'function')) {
    fail('E_OMP_BROWSER_TRANSPORT_INVALID');
  }
  for (const key of REQUIRED_TAB_KEYS) {
    if (typeof tab[key] !== 'function') fail('E_OMP_BROWSER_TRANSPORT_INVALID');
  }
}

function normalizeOptions(options) {
  if (options === undefined) options = {};
  if (!isRecord(options)) fail('E_OMP_BROWSER_TRANSPORT_INVALID');
  for (const key of Object.keys(options)) {
    if (!OPTIONS_KEYS.has(key)) fail('E_OMP_BROWSER_TRANSPORT_INVALID');
  }
  if (typeof options.observe !== 'function') fail('E_OMP_BROWSER_TRANSPORT_INVALID');
  const now = options.now ?? (() => performance.now());
  const sleep = options.sleep ?? ((milliseconds) => new Promise((resolve) => {
    setTimeout(resolve, milliseconds);
  }));
  if (typeof now !== 'function' || typeof sleep !== 'function') {
    fail('E_OMP_BROWSER_TRANSPORT_INVALID');
  }
  return { observe: options.observe, now, sleep };
}

function boundedSelector(selector) {
  if (typeof selector !== 'string'
      || selector.length === 0
      || selector.length > MAX_ARGUMENT_LENGTH) {
    fail('E_OMP_BROWSER_TRANSPORT_INVALID');
  }
  return selector;
}

function boundedString(value, { allowEmpty = false } = {}) {
  if (typeof value !== 'string'
      || (!allowEmpty && value.length === 0)
      || value.length > MAX_ARGUMENT_LENGTH) {
    fail('E_OMP_BROWSER_TRANSPORT_INVALID');
  }
  return value;
}

function deriveBasename(filePath) {
  const name = filePath.split(/[\\/]/u).at(-1);
  if (typeof name !== 'string' || !BASENAME.test(name)) {
    fail('E_OMP_BROWSER_UPLOAD_INVALID_PATH');
  }
  return name;
}

function renderedBasename(rendered) {
  return rendered.split(/[\\/]/u).at(-1);
}

function optionRecord(record) {
  return Object.freeze({
    key: String(record.key),
    text: String(record.text),
    value: String(record.value),
    disabled: record.disabled === true,
  });
}

/**
 * Create the OMP-browser transport for `executeOmpBrowserActionPlan`.
 *
 * @param {object} tab OMP browser tab with fill/click/press/select/uploadFile
 *   and evaluate helpers
 * @param {object} options transport options
 * @param {Function} options.observe observation producer (required)
 * @param {Function} [options.now] monotonic clock for deterministic tests
 * @param {Function} [options.sleep] sleep for deterministic tests
 * @returns {object} frozen transport with exactly the executor's allowed keys
 */
export function createOmpBrowserTransport(tab, options = {}) {
  validateTab(tab);
  const { observe: observeAction, now, sleep } = normalizeOptions(options);

  async function fill(selector, value) {
    boundedSelector(selector);
    boundedString(value, { allowEmpty: true });
    await invokeTab(tab, 'fill', [selector, value]);
    const state = await invokeTab(tab, 'evaluate', [readFillState, selector]);
    if (state.found === false) fail('E_OMP_BROWSER_FILL_UNVERIFIABLE');
    if (state.role === COMBINED_ROLE) return;
    await invokeTab(tab, 'press', [selector, 'End']);
    await invokeTab(tab, 'press', [selector, 'Space']);
    const sampled = await invokeTab(tab, 'evaluate', [readFillState, selector]);
    if (sampled.found === false) fail('E_OMP_BROWSER_FILL_UNVERIFIABLE');
    if (sampled.value === value + ' ') {
      await invokeTab(tab, 'press', [selector, 'Backspace']);
    }
    const finalState = await invokeTab(tab, 'evaluate', [readFillState, selector]);
    if (finalState.found === false || finalState.value !== value) {
      fail('E_OMP_BROWSER_FILL_MISMATCH');
    }
    await sleepFor(sleep, FILL_SETTLE_MS);
    const settledState = await invokeTab(tab, 'evaluate', [readFillState, selector]);
    if (settledState.found === false || settledState.value !== value) {
      fail('E_OMP_BROWSER_FILL_MISMATCH');
    }
  }

  async function click(selector) {
    boundedSelector(selector);
    await invokeTab(tab, 'click', [selector]);
  }

  async function press(selector, key) {
    boundedSelector(selector);
    if (typeof key !== 'string' || !PRESS_KEYS.has(key)) {
      fail('E_OMP_BROWSER_TRANSPORT_INVALID');
    }
    await invokeTab(tab, 'press', [selector, key]);
  }

  async function select(selector, optionValue) {
    boundedSelector(selector);
    boundedString(optionValue);
    await invokeTab(tab, 'select', [selector, optionValue]);
  }

  async function uploadFile(selector, filePath) {
    boundedSelector(selector);
    boundedString(filePath);
    const basename = deriveBasename(filePath);
    await invokeTab(tab, 'uploadFile', [selector, filePath]);
    const start = clockNow(now);
    let attempts = 0;
    let everMatched = false;
    let matched = false;
    while (true) {
      attempts += 1;
      if (attempts > MAX_UPLOAD_ATTEMPTS) fail('E_OMP_BROWSER_UPLOAD_MISSING');
      const state = await invokeTab(tab, 'evaluate', [readFileState, selector]);
      if (state.found === true && state.count > 1) fail('E_OMP_BROWSER_UPLOAD_AMBIGUOUS');
      matched = state.found === true
        && state.count === 1
        && state.names[0] === basename
        && (state.rendered.length === 0 || renderedBasename(state.rendered) === basename);
      if (matched) everMatched = true;
      if (everMatched && !matched) fail('E_OMP_BROWSER_UPLOAD_TRANSIENT');
      const elapsed = clockNow(now) - start;
      if (elapsed >= UPLOAD_SETTLE_MS) break;
      const waitMs = Math.min(UPLOAD_POLL_INTERVAL_MS, UPLOAD_SETTLE_MS - elapsed);
      await sleepFor(sleep, Math.max(0, waitMs));
    }
    if (!matched) fail('E_OMP_BROWSER_UPLOAD_MISSING');
  }

  async function readOptions() {
    const records = await invokeTab(
      tab,
      'evaluate',
      [readOptionRecords, MAX_OPTIONS, MAX_STRING_LENGTH],
    );
    if (!Array.isArray(records)) fail('E_OMP_BROWSER_TRANSPORT_INVALID');
    return records.map(optionRecord);
  }

  async function clickOption(candidate) {
    if (!isRecord(candidate)
        || typeof candidate.text !== 'string'
        || typeof candidate.value !== 'string') {
      fail('E_OMP_BROWSER_TRANSPORT_INVALID');
    }
    const records = await invokeTab(
      tab,
      'evaluate',
      [readOptionRecords, MAX_OPTIONS, MAX_STRING_LENGTH],
    );
    const matches = records.filter((record) => (
      record.disabled === false
      && record.text === candidate.text
      && record.value === candidate.value
    ));
    if (matches.length === 0) fail('E_OMP_BROWSER_OPTION_NOT_FOUND');
    if (matches.length > 1) fail('E_OMP_BROWSER_OPTION_AMBIGUOUS');
    await invokeTab(tab, 'click', [matches[0].selector]);
  }

  async function observe() {
    try {
      return await observeAction();
    } catch (_) {
      fail('E_OMP_BROWSER_TAB_FAILED');
    }
  }

  return Object.freeze({
    fill,
    click,
    press,
    select,
    uploadFile,
    readOptions,
    clickOption,
    observe,
    now,
    sleep,
  });
}
