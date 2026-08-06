import {
  ACTION_RESULT_SCHEMA,
  validateBrowserActionPlan,
  validateBrowserActionResult,
} from './action-plan.mjs';
import {
  CustomSelectExecutorError,
  executeCustomSelectOption,
} from './custom-select.mjs';
import { validateObservation } from './ledger.mjs';

/**
 * Reusable OMP-browser execution layer between a canonical browser action
 * plan and the injected OMP browser transport.
 *
 * The transport is validated with exact keys before any browser work:
 *   required: fill, click, press, select, uploadFile, readOptions,
 *             clickOption, observe
 *   optional: now, sleep
 *
 * `executeOmpBrowserActionPlan(plan, transport, options?)`:
 *   - structurally validates the plan with `validateBrowserActionPlan`;
 *   - executes each planned action once through its exact planned helper,
 *     stopping a batch at the first non-success (no replay at this layer);
 *   - bounds every injected direct helper and the observe callback with a
 *     stable sanitized timeout (`options.callbackTimeoutMs`, default 10s) so
 *     a never-settling transport cannot hang the call;
 *   - executes custom selects only through `executeCustomSelectOption` with
 *     the planned click + targeted ArrowDown on the open step, the query
 *     step's own selector and value, the currently visible option records,
 *     and the exact verified option click, bounded by the query step's
 *     exact-option-visible wait timeout;
 *   - obtains exactly one post-action observation chained to
 *     `plan.observation_id` after the attempted work;
 *   - validates the constructed receipt with `validateBrowserActionResult`
 *     before returning and maps any failure to the sanitized
 *     `E_OMP_BROWSER_RESULT_INVALID` code;
 *   - returns a recursively frozen `{ result, postObservation }` in the exact
 *     ACTION_RESULT_SCHEMA shape.
 *
 * No persistence happens here; the application CLI remains the
 * process/evidence boundary. Values never appear in errors or outcomes.
 */

export const OMP_BROWSER_EXECUTOR_ERROR_CODES = Object.freeze([
  'E_OMP_BROWSER_TRANSPORT_INVALID',
  'E_OMP_BROWSER_PLAN_INVALID',
  'E_OMP_BROWSER_DRIVER_MISMATCH',
  'E_OMP_BROWSER_HELPER_FAILED',
  'E_OMP_BROWSER_OBSERVE_FAILED',
  'E_OMP_BROWSER_RESULT_INVALID',
]);

const ERROR_CODE_SET = new Set(OMP_BROWSER_EXECUTOR_ERROR_CODES);
const REQUIRED_TRANSPORT_KEYS = Object.freeze([
  'fill',
  'click',
  'press',
  'select',
  'uploadFile',
  'readOptions',
  'clickOption',
  'observe',
]);
const OPTIONAL_TRANSPORT_KEYS = Object.freeze(['now', 'sleep']);
const ALLOWED_TRANSPORT_KEYS = new Set([
  ...REQUIRED_TRANSPORT_KEYS,
  ...OPTIONAL_TRANSPORT_KEYS,
]);
const OUTCOME_DRIVER = 'omp_browser';
const ARROW_DOWN = 'ArrowDown';
const CALLBACK_TIMEOUT_MS = 10_000;
const CALLBACK_TIMEOUT = Symbol('omp-browser-callback-timeout');

export class OmpBrowserExecutorError extends Error {
  constructor(code) {
    const normalized = typeof code === 'string' && ERROR_CODE_SET.has(code)
      ? code
      : 'E_OMP_BROWSER_RESULT_INVALID';
    super(normalized);
    this.name = 'OmpBrowserExecutorError';
    this.code = normalized;
  }
}

function fail(code) {
  throw new OmpBrowserExecutorError(code);
}

function isObject(value) {
  if (value === null || typeof value !== 'object' || Array.isArray(value)) return false;
  const prototype = Object.getPrototypeOf(value);
  return prototype === Object.prototype || prototype === null;
}

function freezeDeep(value, seen = new Set()) {
  if (value === null || typeof value !== 'object' || seen.has(value)) return value;
  seen.add(value);
  for (const child of Object.values(value)) freezeDeep(child, seen);
  return Object.freeze(value);
}

function clone(value) {
  try {
    return structuredClone(value);
  } catch (_) {
    fail('E_OMP_BROWSER_RESULT_INVALID');
  }
}

function immutable(value) {
  return freezeDeep(clone(value));
}

function validateTransport(transport) {
  if (!isObject(transport)) fail('E_OMP_BROWSER_TRANSPORT_INVALID');
  for (const key of Object.keys(transport)) {
    if (!ALLOWED_TRANSPORT_KEYS.has(key)) fail('E_OMP_BROWSER_TRANSPORT_INVALID');
  }
  for (const key of REQUIRED_TRANSPORT_KEYS) {
    if (typeof transport[key] !== 'function') fail('E_OMP_BROWSER_TRANSPORT_INVALID');
  }
  for (const key of OPTIONAL_TRANSPORT_KEYS) {
    if (transport[key] !== undefined && typeof transport[key] !== 'function') {
      fail('E_OMP_BROWSER_TRANSPORT_INVALID');
    }
  }
}

/** Exact planned selected option text carried by successful select outcomes. */
function plannedOptionText(action) {
  const optionStep = action.steps.find((step) => step.option_text !== null);
  return optionStep === undefined ? null : optionStep.option_text;
}

function succeededOutcome(action) {
  const outcome = {
    action_id: action.action_id,
    outcome: 'succeeded',
    error_code: null,
    driver: OUTCOME_DRIVER,
    selected_option_text: null,
  };
  if (action.semantic_action === 'select_option') {
    const optionText = plannedOptionText(action);
    if (optionText === null) fail('E_OMP_BROWSER_RESULT_INVALID');
    outcome.selected_option_text = optionText;
  }
  return outcome;
}

function failedOutcome(action, errorCode) {
  return {
    action_id: action.action_id,
    outcome: 'failed',
    error_code: errorCode,
    driver: OUTCOME_DRIVER,
    selected_option_text: null,
  };
}

/** Race an injected callback against a real timer; never leaks the raw result. */
async function raceCallback(callback, timeoutMs) {
  let timer = null;
  try {
    return await Promise.race([
      Promise.resolve().then(() => callback()),
      new Promise((_, reject) => {
        timer = setTimeout(() => reject(CALLBACK_TIMEOUT), Math.max(0, timeoutMs));
      }),
    ]);
  } finally {
    clearTimeout(timer);
  }
}

/**
 * Bounded invocation of a direct transport helper; failures and timeouts
 * sanitize to the same stable code without leaking values.
 */
async function invokeHelper(transport, method, args, timeoutMs = CALLBACK_TIMEOUT_MS) {
  try {
    await raceCallback(() => transport[method](...args), timeoutMs);
  } catch (_) {
    fail('E_OMP_BROWSER_HELPER_FAILED');
  }
}

function isCustomSelectAction(action) {
  const steps = action.steps;
  return action.semantic_action === 'select_option'
    && steps.length === 3
    && steps[0].helper === 'click'
    && steps[1].helper === 'fill'
    && steps[2].helper === 'click_exact_option';
}

async function attemptCustomSelect(action, transport) {
  const [open, query, option] = action.steps;
  const timeoutMs = query.wait_after.timeoutMs;
  const target = {
    optionText: option.option_text,
    optionValue: option.option_value,
  };
  try {
    await executeCustomSelectOption({
      openMenu: async () => {
        await invokeHelper(transport, 'click', [open.selector], timeoutMs);
        await invokeHelper(transport, 'press', [open.selector, ARROW_DOWN], timeoutMs);
      },
      readOptions: () => transport.readOptions(),
      prepareOptions: async () => {
        await invokeHelper(transport, 'fill', [query.selector, query.value], timeoutMs);
      },
      clickOption: async (candidate) => {
        await invokeHelper(transport, 'clickOption', [candidate], timeoutMs);
      },
      target,
      timeoutMs,
      now: transport.now,
      sleep: transport.sleep,
    });
  } catch (error) {
    return failedOutcome(
      action,
      error instanceof CustomSelectExecutorError
        ? error.code
        : 'E_OMP_BROWSER_HELPER_FAILED',
    );
  }
  return succeededOutcome(action);
}

async function attemptSingleStep(action, transport, timeoutMs) {
  const step = action.steps[0];
  const selector = step.selector;
  let operation;
  if (action.semantic_action === 'fill_text' || action.semantic_action === 'clear') {
    operation = () => invokeHelper(transport, 'fill', [selector, step.value], timeoutMs);
  } else if (action.semantic_action === 'select_option') {
    operation = () => invokeHelper(transport, 'select', [selector, step.option_value], timeoutMs);
  } else if (action.semantic_action === 'toggle') {
    operation = () => invokeHelper(transport, 'click', [selector], timeoutMs);
  } else if (action.semantic_action === 'upload_file') {
    operation = () => invokeHelper(transport, 'uploadFile', [selector, step.file_path], timeoutMs);
  } else {
    fail('E_OMP_BROWSER_PLAN_INVALID');
  }
  try {
    await operation();
  } catch (_) {
    return failedOutcome(action, 'E_OMP_BROWSER_HELPER_FAILED');
  }
  return succeededOutcome(action);
}

/** Exactly one post-action observation chained to the plan observation. */
async function observePostAction(plan, transport, timeoutMs) {
  let postObservation;
  try {
    const raw = await raceCallback(() => transport.observe(), timeoutMs);
    validateObservation(raw);
    postObservation = immutable(raw);
  } catch (_) {
    fail('E_OMP_BROWSER_OBSERVE_FAILED');
  }
  if (postObservation.previous_observation_id !== plan.observation_id
      || postObservation.observation_id === plan.observation_id) {
    fail('E_OMP_BROWSER_OBSERVE_FAILED');
  }
  return postObservation;
}

/** Treat an upload helper error as recoverable when fresh exact DOM state proves commitment. */
function reconcileCommittedUpload(action, outcome, postObservation) {
  if (action.semantic_action !== 'upload_file' || outcome.outcome === 'succeeded') return outcome;
  const controls = postObservation.controls.filter((control) => control.stable_id === action.field_id);
  if (controls.length !== 1) return outcome;
  const file = controls[0].file;
  if (file === null || file.count !== 1 || !Array.isArray(file.names)
      || file.names.length !== 1 || file.names[0] !== action.retention.file_name) {
    return outcome;
  }
  return succeededOutcome(action);
}

function normalizeOptions(options) {
  if (!isObject(options)) return { callbackTimeoutMs: CALLBACK_TIMEOUT_MS };
  const callbackTimeoutMs = Number.isSafeInteger(options.callbackTimeoutMs)
    && options.callbackTimeoutMs >= 0
    ? options.callbackTimeoutMs
    : CALLBACK_TIMEOUT_MS;
  return { callbackTimeoutMs };
}

/**
 * Execute a validated single-action or conservative fill-batch plan through
 * the injected OMP browser transport.
 *
 * @param {object} plan canonical browser action plan (v2)
 * @param {object} transport exact-callback OMP browser transport
 * @param {object} [options] executor options
 * @param {number} [options.callbackTimeoutMs] bound in milliseconds applied to
 *   every direct helper and the observe callback (default 10_000)
 * @returns {Promise<{result: object, postObservation: object}>} frozen result
 *   and chained post-action observation in the ACTION_RESULT_SCHEMA shape
 */
export async function executeOmpBrowserActionPlan(plan, transport, options = {}) {
  validateTransport(transport);
  const { callbackTimeoutMs } = normalizeOptions(options);
  let normalizedPlan;
  try {
    normalizedPlan = validateBrowserActionPlan(plan);
  } catch (_) {
    fail('E_OMP_BROWSER_PLAN_INVALID');
  }
  if (normalizedPlan.driver !== 'omp_browser') fail('E_OMP_BROWSER_DRIVER_MISMATCH');

  const outcomes = [];
  for (const action of normalizedPlan.actions) {
    const outcome = isCustomSelectAction(action)
      ? await attemptCustomSelect(action, transport)
      : await attemptSingleStep(action, transport, callbackTimeoutMs);
    outcomes.push(outcome);
    if (outcome.outcome !== 'succeeded') break;
  }

  const postObservation = await observePostAction(normalizedPlan, transport, callbackTimeoutMs);
  const reconciledOutcomes = outcomes.map((outcome, index) => (
    reconcileCommittedUpload(normalizedPlan.actions[index], outcome, postObservation)
  ));
  const result = freezeDeep({
    schema: ACTION_RESULT_SCHEMA,
    plan_id: normalizedPlan.plan_id,
    post_observation_id: postObservation.observation_id,
    outcomes: reconciledOutcomes,
  });
  const receipt = freezeDeep({ result, postObservation });
  try {
    validateBrowserActionResult(result, normalizedPlan, postObservation);
  } catch (_) {
    fail('E_OMP_BROWSER_RESULT_INVALID');
  }
  return receipt;
}
