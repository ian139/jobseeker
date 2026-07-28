import {
  BrowserAdapterError,
  cloneTransportValue,
  normalizeAction,
} from './browser-adapter.mjs';

export const ATS_ADAPTER_SCHEMA = 'phase1-ats-adapter-v1';
export const CONTROL_DISPATCH_KINDS = Object.freeze(['known', 'reusable', 'generic']);
export const NORMALIZED_CONTROL_TYPES = Object.freeze([
  'text',
  'textarea',
  'email',
  'phone',
  'number',
  'date',
  'radio_group',
  'checkbox',
  'checkbox_group',
  'native_select',
  'custom_select',
  'combobox',
  'autocomplete',
  'file_upload',
  'button',
  'link',
  'dialog',
  'iframe_control',
  'unknown_control',
]);
export const ATS_RESULT_KINDS = Object.freeze(['validation', 'navigation', 'submission']);

const CONTROL_KIND_SET = new Set(CONTROL_DISPATCH_KINDS);
const CONTROL_TYPE_SET = new Set(NORMALIZED_CONTROL_TYPES);
const RESULT_KIND_SET = new Set(ATS_RESULT_KINDS);
const CONTROL_KEYS = new Set([
  'schema',
  'control',
  'controlReference',
  'fieldId',
  'dispatchKind',
  'controlKind',
  'controlType',
  'observationId',
  'frameId',
  'ref',
  'stableId',
  'stable_id',
  'field_id',
  'observation_id',
  'frame_id',
  'kind',
  'tag',
  'type',
  'role',
  'label',
  'name',
  'description',
  'locator',
  'group_id',
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
const RESULT_KEYS = new Set([
  'kind',
  'type',
  'resultKind',
  'result',
  'status',
  'reasonCode',
  'errorCode',
  'message',
  'observationId',
  'observation_id',
  'workspaceId',
  'workspace_id',
  'valid',
  'errors',
  'url',
  'title',
  'state',
  'changed',
  'submitted',
  'accepted',
  'confirmation',
  'retained',
]);
const ERROR_KEYS = new Set(['code', 'reasonCode', 'fieldId', 'controlReference', 'message']);
const MAX_ID = 512;
const MAX_TEXT = 16 * 1024;
const MAX_ERRORS = 256;

export class ATSAdapterError extends Error {
  constructor(code, message = code) {
    super(message);
    this.name = 'ATSAdapterError';
    this.code = code;
  }
}

function isRecord(value) {
  return value !== null && typeof value === 'object' && !Array.isArray(value);
}

function fail(code) {
  throw new ATSAdapterError(code, code);
}

function assertRecord(value, code = 'INVALID_ARGUMENT') {
  if (!isRecord(value)) fail(code);
  return value;
}

function assertString(value, code, { nullable = false, identifier = false } = {}) {
  if (nullable && value === null) return value;
  if (typeof value !== 'string' || value.length === 0 || value.length > (identifier ? MAX_ID : MAX_TEXT)) fail(code);
  if (/[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f]/u.test(value)) fail(code);
  if (identifier && value.trim() !== value) fail(code);
  return value;
}

function assertBoolean(value, code, nullable = false) {
  if (nullable && value === null) return value;
  if (typeof value !== 'boolean') fail(code);
  return value;
}

function assertNoUnknownKeys(value, allowed, code) {
  for (const key of Object.keys(value)) if (!allowed.has(key)) fail(code);
}

function immutable(value) {
  const copied = cloneTransportValue(value);
  return freeze(copied);
}

function freeze(value, seen = new WeakSet()) {
  if (value === null || typeof value !== 'object' || value instanceof Uint8Array || value instanceof ArrayBuffer) return value;
  if (seen.has(value)) return value;
  seen.add(value);
  for (const child of Object.values(value)) freeze(child, seen);
  return Object.freeze(value);
}

function valueAt(value, ...keys) {
  for (const key of keys) if (value[key] !== undefined) return value[key];
  return undefined;
}

function classifyKind(value) {
  const candidate = valueAt(value, 'dispatchKind', 'controlKind');
  if (candidate !== undefined) {
    if (typeof candidate !== 'string' || !CONTROL_KIND_SET.has(candidate)) fail('INVALID_CONTROL_KIND');
    return candidate;
  }
  if (typeof value.kind === 'string' && CONTROL_KIND_SET.has(value.kind)) return value.kind;
  return 'generic';
}

function controlReference(value) {
  const reference = valueAt(value, 'controlReference', 'ref', 'stableId', 'stable_id');
  return assertString(reference, 'INVALID_CONTROL_REFERENCE', { identifier: true });
}

function controlType(value) {
  const candidate = valueAt(value, 'controlType');
  if (candidate !== undefined) {
    if (typeof candidate !== 'string' || !CONTROL_TYPE_SET.has(candidate)) fail('INVALID_CONTROL_TYPE');
    return candidate;
  }
  const type = valueAt(value, 'type');
  if (typeof type === 'string') {
    const normalized = type.toLowerCase();
    if (CONTROL_TYPE_SET.has(normalized)) return normalized;
  }
  return 'unknown_control';
}

function normalizedRawControl(value) {
  const raw = isRecord(value.control) ? value.control : value;
  const copied = {};
  for (const key of Object.keys(raw)) {
    if (!CONTROL_KEYS.has(key) || key === 'control' || key === 'schema') continue;
    copied[key] = raw[key];
  }
  return immutable(copied);
}

/**
 * Normalize an observer control without deriving a selector from page text.
 * Classification is explicit (`dispatchKind`) and otherwise falls back to
 * the deliberately boring generic handler.
 */
export function normalizeATSControl(input) {
  const value = assertRecord(input, 'INVALID_CONTROL');
  assertNoUnknownKeys(value, CONTROL_KEYS, 'INVALID_CONTROL');
  const raw = isRecord(value.control) ? value.control : value;
  assertNoUnknownKeys(raw, CONTROL_KEYS, 'INVALID_CONTROL');
  const merged = value.control ? { ...raw, ...value } : value;
  const reference = controlReference(merged);
  const dispatchKind = classifyKind(merged);
  const normalized = {
    schema: `${ATS_ADAPTER_SCHEMA}-control`,
    controlReference: reference,
    fieldId: valueAt(merged, 'fieldId', 'field_id') ?? null,
    dispatchKind,
    controlType: controlType(merged),
    observationId: valueAt(merged, 'observationId', 'observation_id') ?? null,
    frameId: valueAt(merged, 'frameId', 'frame_id') ?? null,
    control: normalizedRawControl(value),
  };
  if (normalized.fieldId !== null) assertString(normalized.fieldId, 'INVALID_CONTROL', { identifier: true });
  if (normalized.observationId !== null) assertString(normalized.observationId, 'INVALID_CONTROL', { identifier: true });
  if (normalized.frameId !== null) assertString(normalized.frameId, 'INVALID_CONTROL', { identifier: true });
  return immutable(normalized);
}

function normalizedError(value) {
  assertRecord(value, 'INVALID_RESULT');
  assertNoUnknownKeys(value, ERROR_KEYS, 'INVALID_RESULT');
  const result = {};
  if (value.code !== undefined) result.code = assertString(value.code, 'INVALID_RESULT', { identifier: true });
  if (value.reasonCode !== undefined) result.reasonCode = assertString(value.reasonCode, 'INVALID_RESULT', { identifier: true });
  if (value.fieldId !== undefined) result.fieldId = assertString(value.fieldId, 'INVALID_RESULT', { identifier: true });
  if (value.controlReference !== undefined) result.controlReference = assertString(value.controlReference, 'INVALID_RESULT', { identifier: true });
  if (value.message !== undefined) result.message = assertString(value.message, 'INVALID_RESULT');
  if (Object.keys(result).length === 0) fail('INVALID_RESULT');
  return result;
}

function resultKind(value, expectedKind = null) {
  const candidate = valueAt(value, 'resultKind', 'kind', 'type');
  if (candidate === undefined) {
    if (expectedKind === null) fail('RESULT_KIND_REQUIRED');
    return expectedKind;
  }
  if (typeof candidate !== 'string' || !RESULT_KIND_SET.has(candidate)) fail('INVALID_RESULT_KIND');
  if (expectedKind !== null && candidate !== expectedKind) fail('RESULT_KIND_MISMATCH');
  return candidate;
}

/** Normalize only bounded, non-page-content result data. */
export function normalizeATSResult(input, expectedKind = null) {
  const value = assertRecord(input, 'INVALID_RESULT');
  const wrapped = isRecord(value.result);
  const nested = wrapped ? value.result : value;
  assertNoUnknownKeys(value, RESULT_KEYS, 'INVALID_RESULT');
  assertNoUnknownKeys(nested, RESULT_KEYS, 'INVALID_RESULT');
  const outerKind = wrapped && valueAt(value, 'resultKind', 'kind', 'type') !== undefined
    ? resultKind(value, null)
    : null;
  const nestedKind = valueAt(nested, 'resultKind', 'kind', 'type') !== undefined
    ? resultKind(nested, outerKind ?? expectedKind)
    : outerKind ?? expectedKind;
  if (nestedKind === null) fail('RESULT_KIND_REQUIRED');
  if (expectedKind !== null && nestedKind !== expectedKind) fail('RESULT_KIND_MISMATCH');
  const kind = nestedKind;
  const result = { schema: `${ATS_ADAPTER_SCHEMA}-result`, kind };
  if (nested.status !== undefined) result.status = assertString(nested.status, 'INVALID_RESULT', { identifier: true });
  if (nested.reasonCode !== undefined) result.reasonCode = assertString(nested.reasonCode, 'INVALID_RESULT', { identifier: true });
  if (nested.errorCode !== undefined) result.errorCode = assertString(nested.errorCode, 'INVALID_RESULT', { identifier: true });
  if (nested.message !== undefined) result.message = assertString(nested.message, 'INVALID_RESULT');
  const observationId = valueAt(nested, 'observationId', 'observation_id');
  if (observationId !== undefined) result.observationId = assertString(observationId, 'INVALID_RESULT', { identifier: true });
  const workspaceId = valueAt(nested, 'workspaceId', 'workspace_id');
  if (workspaceId !== undefined) result.workspaceId = assertString(workspaceId, 'INVALID_RESULT', { identifier: true });
  if (nested.valid !== undefined) result.valid = assertBoolean(nested.valid, 'INVALID_RESULT', true);
  if (nested.errors !== undefined) {
    if (!Array.isArray(nested.errors) || nested.errors.length > MAX_ERRORS) fail('INVALID_RESULT');
    result.errors = nested.errors.map(normalizedError);
  }
  if (nested.url !== undefined) result.url = assertString(nested.url, 'INVALID_RESULT');
  if (nested.title !== undefined) result.title = assertString(nested.title, 'INVALID_RESULT');
  if (nested.state !== undefined) result.state = assertString(nested.state, 'INVALID_RESULT', { identifier: true });
  if (nested.changed !== undefined) result.changed = assertBoolean(nested.changed, 'INVALID_RESULT', true);
  if (nested.submitted !== undefined) result.submitted = assertBoolean(nested.submitted, 'INVALID_RESULT');
  if (nested.accepted !== undefined) result.accepted = assertBoolean(nested.accepted, 'INVALID_RESULT', true);
  if (nested.confirmation !== undefined) result.confirmation = assertString(nested.confirmation, 'INVALID_RESULT');
  if (nested.retained !== undefined) result.retained = assertBoolean(nested.retained, 'INVALID_RESULT', true);
  return immutable(result);
}

function ensureAdapter(value) {
  if (value === null || typeof value !== 'object' ||
      typeof value.resolveControl !== 'function' || typeof value.performAction !== 'function') {
    fail('BROWSER_ADAPTER_REQUIRED');
  }
  return value;
}

function optionsForConstructor(value) {
  if (value === undefined) return {};
  const options = assertRecord(value, 'INVALID_OPTIONS');
  assertNoUnknownKeys(options, new Set(['browserAdapter', 'controlHandlers', 'resultHandlers']), 'INVALID_OPTIONS');
  for (const key of ['controlHandlers', 'resultHandlers']) {
    if (options[key] === undefined) continue;
    assertRecord(options[key], 'INVALID_OPTIONS');
    for (const handler of Object.values(options[key])) if (typeof handler !== 'function') fail('INVALID_OPTIONS');
  }
  return options;
}

export class ATSAdapter {
  constructor(browserAdapterOrOptions, maybeOptions = {}) {
    let browserAdapter = browserAdapterOrOptions;
    let options = maybeOptions;
    if (isRecord(browserAdapterOrOptions) &&
        Object.prototype.hasOwnProperty.call(browserAdapterOrOptions, 'browserAdapter')) {
      browserAdapter = browserAdapterOrOptions.browserAdapter;
      options = browserAdapterOrOptions;
    }
    this.#browserAdapter = ensureAdapter(browserAdapter);
    const normalizedOptions = optionsForConstructor(options);
    this.#controlHandlers = new Map(Object.entries(normalizedOptions.controlHandlers ?? {}));
    this.#resultHandlers = new Map(Object.entries(normalizedOptions.resultHandlers ?? {}));
  }

  #browserAdapter;
  #controlHandlers;
  #resultHandlers;

  get browserAdapter() {
    return this.#browserAdapter;
  }

  registerControlHandler(kind, handler) {
    if (typeof kind !== 'string' || !CONTROL_KIND_SET.has(kind) || typeof handler !== 'function') fail('INVALID_HANDLER');
    this.#controlHandlers.set(kind, handler);
    return this;
  }

  registerResultHandler(kind, handler) {
    if (typeof kind !== 'string' || !RESULT_KIND_SET.has(kind) || typeof handler !== 'function') fail('INVALID_HANDLER');
    this.#resultHandlers.set(kind, handler);
    return this;
  }

  async dispatchControl(input, context = {}) {
    const control = normalizeATSControl(input);
    const handler = this.#controlHandlers.get(control.dispatchKind) ??
      (async (normalized, details) => {
        const request = {
          control: normalized.control,
          controlReference: normalized.controlReference,
          ...details,
        };
        if (normalized.observationId !== null) request.observationId = normalized.observationId;
        return this.#browserAdapter.resolveControl(request);
      });
    let output;
    try {
      output = await handler(control, immutable(assertRecord(context, 'INVALID_CONTEXT')));
    } catch (error) {
      if (error instanceof ATSAdapterError || error instanceof BrowserAdapterError) throw error;
      throw new ATSAdapterError('CONTROL_DISPATCH_FAILED');
    }
    if (output === undefined) fail('EMPTY_DISPATCH_RESULT');
    return immutable(output);
  }

  async resolveControl(input, context = {}) {
    return this.dispatchControl(input, context);
  }

  async performControlAction(controlInput, actionInput, context = {}) {
    const control = normalizeATSControl(controlInput);
    const actionValue = assertRecord(actionInput, 'INVALID_ACTION');
    const action = normalizeAction(actionValue.controlReference === undefined
      ? { ...actionValue, controlReference: control.controlReference }
      : actionValue);
    if (action.controlReference !== control.controlReference) {
      fail('CONTROL_REFERENCE_MISMATCH');
    }
    if (control.observationId !== null && action.observationId !== undefined && action.observationId !== control.observationId) {
      fail('OBSERVATION_ID_MISMATCH');
    }
    const details = immutable(assertRecord(context, 'INVALID_CONTEXT'));
    try {
      const output = await this.#browserAdapter.performAction(
        action,
        {
          ...details,
          observationId: action.observationId ?? control.observationId ?? undefined,
          controlReference: control.controlReference,
        },
      );
      if (output === undefined) fail('EMPTY_ACTION_RESULT');
      return immutable(output);
    } catch (error) {
      if (error instanceof ATSAdapterError || error instanceof BrowserAdapterError) throw error;
      throw new ATSAdapterError('ACTION_DISPATCH_FAILED');
    }
  }

  async dispatchResult(input, expectedKind = null, context = {}) {
    const normalized = normalizeATSResult(input, expectedKind);
    const handler = this.#resultHandlers.get(normalized.kind);
    if (!handler) return normalized;
    try {
      const output = await handler(normalized, immutable(assertRecord(context, 'INVALID_CONTEXT')));
      if (output === undefined) fail('EMPTY_RESULT_HANDLER');
      return immutable(output);
    } catch (error) {
      if (error instanceof ATSAdapterError || error instanceof BrowserAdapterError) throw error;
      throw new ATSAdapterError('RESULT_DISPATCH_FAILED');
    }
  }

  async dispatchValidationResult(input, context = {}) {
    return this.dispatchResult(input, 'validation', context);
  }

  async dispatchNavigationResult(input, context = {}) {
    return this.dispatchResult(input, 'navigation', context);
  }

  async dispatchSubmissionResult(input, context = {}) {
    return this.dispatchResult(input, 'submission', context);
  }

  async handleValidation(input, context = {}) {
    return this.dispatchValidationResult(input, context);
  }

  async handleNavigation(input, context = {}) {
    return this.dispatchNavigationResult(input, context);
  }

  async handleSubmission(input, context = {}) {
    return this.dispatchSubmissionResult(input, context);
  }
}

export const AtsAdapter = ATSAdapter;

export function createATSAdapter(browserAdapterOrOptions, options = {}) {
  return new ATSAdapter(browserAdapterOrOptions, options);
}

export const createAtsAdapter = createATSAdapter;
