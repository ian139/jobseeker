import { validateObservation } from './ledger.mjs';

export const BROWSER_ADAPTER_SCHEMA = 'phase1-browser-adapter-v1';

export const NORMALIZED_ACTIONS = Object.freeze([
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

const ACTION_SET = new Set(NORMALIZED_ACTIONS);
const ACTION_KEYS = new Set([
  'action',
  'observationId',
  'controlReference',
  'fieldId',
  'value',
  'optionValue',
  'checked',
  'filePath',
  'url',
  'state',
  'timeoutMs',
]);
const CONTROL_ACTIONS = new Set([
  'fill_text',
  'clear',
  'select_option',
  'toggle',
  'upload_file',
  'click',
]);
const IDENTIFIER_MAX = 512;
const STRING_MAX = 16 * 1024;
const ARRAY_MAX = 1024;
const OBJECT_KEYS_MAX = 256;
const DEPTH_MAX = 16;
const TIMEOUT_MAX_MS = 10 * 60 * 1000;
const FRAME_KEYS = new Set(['id', 'parent_id', 'url', 'origin', 'accessible']);
const CONTROL_REFERENCE_KEYS = ['controlReference', 'fieldId'];

export class BrowserAdapterError extends Error {
  constructor(code, message = code) {
    super(message);
    this.name = 'BrowserAdapterError';
    this.code = code;
  }
}

export class BrowserTransportError extends BrowserAdapterError {
  constructor(code = 'TRANSPORT_FAILURE') {
    super(code, code);
    this.name = 'BrowserTransportError';
  }
}

export class BrowserIdentityError extends BrowserAdapterError {
  constructor(code, message = code) {
    super(code, message);
    this.name = 'BrowserIdentityError';
  }
}

function isRecord(value) {
  return value !== null && typeof value === 'object' && !Array.isArray(value);
}

function fail(code, message = code) {
  throw new BrowserAdapterError(code, message);
}

function assertRecord(value, code = 'INVALID_ARGUMENT') {
  if (!isRecord(value)) fail(code);
  return value;
}

function assertSafeString(value, code = 'INVALID_STRING', { identifier = false, nullable = false } = {}) {
  if (nullable && value === null) return value;
  if (typeof value !== 'string' || value.length === 0 || value.length > (identifier ? IDENTIFIER_MAX : STRING_MAX)) {
    fail(code);
  }
  if (/[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f]/u.test(value)) fail(code);
  if (identifier && value.trim() !== value) fail(code);
  return value;
}

function assertTimeout(value, code = 'INVALID_TIMEOUT') {
  if (!Number.isSafeInteger(value) || value < 0 || value > TIMEOUT_MAX_MS) fail(code);
  return value;
}

function cloneBinary(value) {
  if (value instanceof Uint8Array) return new Uint8Array(value);
  if (value instanceof ArrayBuffer) return value.slice(0);
  if (typeof ArrayBuffer !== 'undefined' && ArrayBuffer.isView(value)) {
    return new Uint8Array(value.buffer.slice(value.byteOffset, value.byteOffset + value.byteLength));
  }
  return null;
}

/**
 * Copy data crossing the browser boundary without retaining page-owned objects.
 * Functions, symbols, cyclic values, accessors, and prototype-bearing objects are
 * deliberately rejected. Strings and collections are bounded before the copy.
 */
export function cloneTransportValue(value, path = 'value', depth = 0, seen = new WeakSet()) {
  if (value === null || typeof value === 'boolean' || typeof value === 'number') {
    if (typeof value === 'number' && !Number.isFinite(value)) fail('UNSAFE_TRANSPORT_VALUE');
    return value;
  }
  if (typeof value === 'string') {
    assertSafeString(value, 'UNSAFE_TRANSPORT_VALUE');
    return value;
  }
  if (value === undefined) return undefined;
  if (typeof value === 'bigint' || typeof value === 'function' || typeof value === 'symbol') {
    fail('UNSAFE_TRANSPORT_VALUE');
  }
  const binary = cloneBinary(value);
  if (binary !== null) return binary;
  if (depth > DEPTH_MAX || (Array.isArray(value) && value.length > ARRAY_MAX)) {
    fail('UNSAFE_TRANSPORT_VALUE');
  }
  if (!Array.isArray(value) && !isRecord(value)) fail('UNSAFE_TRANSPORT_VALUE');
  if (seen.has(value)) fail('UNSAFE_TRANSPORT_VALUE');
  seen.add(value);
  let result;
  if (Array.isArray(value)) {
    result = value.map((item, index) => cloneTransportValue(item, `${path}[${index}]`, depth + 1, seen));
  } else {
    const keys = Object.keys(value);
    if (keys.length > OBJECT_KEYS_MAX) fail('UNSAFE_TRANSPORT_VALUE');
    result = {};
    for (const key of keys) {
      if (!key || key === '__proto__' || key === 'constructor' || key === 'prototype') {
        fail('UNSAFE_TRANSPORT_VALUE');
      }
      const descriptor = Object.getOwnPropertyDescriptor(value, key);
      if (!descriptor || !('value' in descriptor)) fail('UNSAFE_TRANSPORT_VALUE');
      const child = cloneTransportValue(descriptor.value, `${path}.${key}`, depth + 1, seen);
      if (child !== undefined) result[key] = child;
    }
  }
  seen.delete(value);
  return result;
}

function freezeTransportValue(value, seen = new WeakSet()) {
  if (value === null || typeof value !== 'object' || value instanceof Uint8Array || value instanceof ArrayBuffer) {
    return value;
  }
  if (seen.has(value)) return value;
  seen.add(value);
  for (const item of Object.values(value)) freezeTransportValue(item, seen);
  return Object.freeze(value);
}

function immutableTransportValue(value) {
  return freezeTransportValue(cloneTransportValue(value));
}

function assertNoUnknownKeys(value, allowed, code = 'INVALID_ARGUMENT') {
  for (const key of Object.keys(value)) {
    if (!allowed.has(key)) fail(code);
  }
}

function normalizeIdentity(value, code = 'INVALID_WORKSPACE_ID') {
  return assertSafeString(value, code, { identifier: true });
}

function identityFrom(value, key, alternateKey = null) {
  if (!isRecord(value)) return undefined;
  const direct = value[key];
  if (direct !== undefined) return direct;
  if (alternateKey !== null && value[alternateKey] !== undefined) return value[alternateKey];
  return undefined;
}

function nestedResult(value) {
  if (!isRecord(value)) return null;
  if (isRecord(value.observation)) return value.observation;
  if (isRecord(value.page)) return value.page;
  return null;
}

function observationFrom(value) {
  if (isRecord(value)) {
    const candidate = nestedResult(value);
    if (candidate !== null && candidate.schema === 'phase1-observation-v1') return candidate;
    if (value.schema === 'phase1-observation-v1') return value;
  }
  return null;
}

function assertObservation(value) {
  const observation = observationFrom(value);
  if (observation === null) fail('INVALID_OBSERVATION');
  try {
    validateObservation(observation);
  } catch (_) {
    fail('INVALID_OBSERVATION');
  }
  return observation;
}

function validateFrame(frame) {
  assertRecord(frame, 'INVALID_FRAMES');
  assertNoUnknownKeys(frame, FRAME_KEYS, 'INVALID_FRAMES');
  if (Object.keys(frame).length !== FRAME_KEYS.size) fail('INVALID_FRAMES');
  normalizeIdentity(frame.id, 'INVALID_FRAMES');
  if (frame.parent_id !== null) normalizeIdentity(frame.parent_id, 'INVALID_FRAMES');
  if (typeof frame.url !== 'string' || frame.url.length > STRING_MAX) fail('INVALID_FRAMES');
  if (typeof frame.origin !== 'string' || frame.origin.length > STRING_MAX) fail('INVALID_FRAMES');
  if (typeof frame.accessible !== 'boolean') fail('INVALID_FRAMES');
}

function framesFrom(value) {
  if (Array.isArray(value)) return value;
  if (isRecord(value) && Array.isArray(value.frames)) return value.frames;
  fail('INVALID_FRAMES');
}

function ensureIdentityMatch(expected, actual, code) {
  if (expected !== null && expected !== undefined && actual !== undefined && actual !== expected) {
    throw new BrowserIdentityError(code);
  }
  if (expected !== null && expected !== undefined && actual === null) {
    throw new BrowserIdentityError(code);
  }
}

function resultWorkspaceId(value) {
  if (!isRecord(value)) return undefined;
  const direct = identityFrom(value, 'workspaceId', 'workspace_id');
  if (direct !== undefined) return direct;
  if (isRecord(value.workspace)) return identityFrom(value.workspace, 'id', 'workspace_id');
  if (isRecord(value.result)) return resultWorkspaceId(value.result);
  return undefined;
}

function resultObservationId(value) {
  if (!isRecord(value)) return undefined;
  const direct = identityFrom(value, 'observationId', 'observation_id');
  if (direct !== undefined) return direct;
  const observation = observationFrom(value);
  if (observation !== null) return observation.observation_id;
  if (isRecord(value.result)) return resultObservationId(value.result);
  return undefined;
}

function expectedObservation(options, current) {
  if (!isRecord(options)) return current;
  if (options.expectedObservationId !== undefined) return options.expectedObservationId;
  if (options.observationId !== undefined) return options.observationId;
  return current;
}

function normalizeOptions(value, code = 'INVALID_ARGUMENT') {
  if (value === undefined) return {};
  assertRecord(value, code);
  return cloneTransportValue(value);
}

function actionValueAllowed(value) {
  return value === null || typeof value === 'string' || typeof value === 'boolean' ||
    (Array.isArray(value) && value.every((item) => typeof item === 'string'));
}

function requireControlReference(action) {
  if (!CONTROL_ACTIONS.has(action.action)) return;
  if (typeof action.controlReference !== 'string' || action.controlReference.length === 0) {
    fail('INVALID_ACTION');
  }
}

/** Validate and copy the action vocabulary shared by ATS adapters. */
export function normalizeAction(input) {
  assertRecord(input, 'INVALID_ACTION');
  assertNoUnknownKeys(input, ACTION_KEYS, 'INVALID_ACTION');
  if (typeof input.action !== 'string' || !ACTION_SET.has(input.action)) fail('INVALID_ACTION');
  const action = { action: input.action };
  if (input.observationId !== undefined) action.observationId = normalizeIdentity(input.observationId, 'INVALID_ACTION');
  if (input.controlReference !== undefined) action.controlReference = normalizeIdentity(input.controlReference, 'INVALID_ACTION');
  if (input.fieldId !== undefined) action.fieldId = normalizeIdentity(input.fieldId, 'INVALID_ACTION');
  if (input.value !== undefined) {
    if (!actionValueAllowed(input.value)) fail('INVALID_ACTION');
    action.value = cloneTransportValue(input.value);
  }
  if (input.optionValue !== undefined) action.optionValue = assertSafeString(input.optionValue, 'INVALID_ACTION');
  if (input.checked !== undefined && typeof input.checked !== 'boolean') fail('INVALID_ACTION');
  if (input.checked !== undefined) action.checked = input.checked;
  if (input.filePath !== undefined) action.filePath = assertSafeString(input.filePath, 'INVALID_ACTION');
  if (input.url !== undefined) action.url = assertSafeString(input.url, 'INVALID_ACTION');
  if (input.state !== undefined) action.state = assertSafeString(input.state, 'INVALID_ACTION');
  if (input.timeoutMs !== undefined) action.timeoutMs = assertTimeout(input.timeoutMs, 'INVALID_ACTION');
  requireControlReference(action);
  if (action.action === 'fill_text' && typeof action.value !== 'string') fail('INVALID_ACTION');
  if (action.action === 'select_option' && typeof action.optionValue !== 'string' && typeof action.value !== 'string') {
    fail('INVALID_ACTION');
  }
  if (action.action === 'toggle' && action.checked === undefined && typeof action.value !== 'boolean') fail('INVALID_ACTION');
  if (action.action === 'upload_file' && typeof action.filePath !== 'string') fail('INVALID_ACTION');
  if (action.action === 'navigate' && typeof action.url !== 'string') fail('INVALID_ACTION');
  if (action.action === 'wait' && action.state === undefined && action.timeoutMs === undefined) fail('INVALID_ACTION');
  return immutableTransportValue(action);
}

function transportMethod(transport, method) {
  if (typeof transport[method] === 'function') return transport[method].bind(transport);
  if (typeof transport.request === 'function') return (request) => transport.request(method, request);
  if (typeof transport.invoke === 'function') return (request) => transport.invoke(method, request);
  throw new BrowserTransportError('TRANSPORT_METHOD_UNAVAILABLE');
}

function operationPayload(value) {
  if (value === undefined) return {};
  if (isRecord(value)) return cloneTransportValue(value);
  return { value: cloneTransportValue(value) };
}

export class BrowserAdapter {
  constructor(transport, options = {}) {
    if (transport === null || typeof transport !== 'object') throw new BrowserTransportError('TRANSPORT_REQUIRED');
    const normalizedOptions = normalizeOptions(options, 'INVALID_OPTIONS');
    assertNoUnknownKeys(normalizedOptions, new Set(['workspaceId', 'observationId']), 'INVALID_OPTIONS');
    this.#transport = transport;
    this.#workspaceId = normalizedOptions.workspaceId === undefined
      ? null
      : normalizeIdentity(normalizedOptions.workspaceId);
    this.#observationId = normalizedOptions.observationId === undefined
      ? null
      : normalizeIdentity(normalizedOptions.observationId);
    this.#tail = Promise.resolve();
  }

  #transport;
  #workspaceId;
  #observationId;
  #tail;

  get workspaceId() {
    return this.#workspaceId;
  }

  get observationId() {
    return this.#observationId;
  }

  get identity() {
    return Object.freeze({ workspaceId: this.#workspaceId, observationId: this.#observationId });
  }

  #enqueue(operation) {
    const next = this.#tail.then(operation, operation);
    this.#tail = next.catch(() => undefined);
    return next;
  }

  #request(method, payload, expectedObservationId = this.#observationId, checkResultObservation = true) {
    const request = operationPayload(payload);
    if (this.#workspaceId !== null) request.workspaceId = this.#workspaceId;
    if (expectedObservationId !== null && expectedObservationId !== undefined) request.observationId = expectedObservationId;
    const call = transportMethod(this.#transport, method);
    return Promise.resolve().then(() => call(immutableTransportValue(request))).then((raw) => {
      if (raw === undefined) throw new BrowserTransportError('EMPTY_TRANSPORT_RESULT');
      let sanitized;
      try {
        sanitized = immutableTransportValue(raw);
      } catch (_) {
        throw new BrowserTransportError('UNSAFE_TRANSPORT_RESULT');
      }
      const workspaceId = resultWorkspaceId(sanitized);
      if (workspaceId !== undefined) {
        normalizeIdentity(workspaceId, 'WORKSPACE_ID_MISMATCH');
        ensureIdentityMatch(this.#workspaceId, workspaceId, 'WORKSPACE_ID_MISMATCH');
        if (this.#workspaceId === null) this.#workspaceId = workspaceId;
      }
      const observationId = resultObservationId(sanitized);
      if (observationId !== undefined) {
        normalizeIdentity(observationId, 'OBSERVATION_ID_MISMATCH');
        if (checkResultObservation) {
          ensureIdentityMatch(expectedObservationId, observationId, 'OBSERVATION_ID_MISMATCH');
        }
      }
      return sanitized;
    }).catch((error) => {
      if (error instanceof BrowserAdapterError || error instanceof BrowserTransportError) throw error;
      throw new BrowserTransportError('TRANSPORT_FAILURE');
    });
  }

  #checkExpectedObservation(options) {
    const expected = expectedObservation(options, this.#observationId);
    if (expected !== null && expected !== undefined) normalizeIdentity(expected, 'OBSERVATION_ID_MISMATCH');
    ensureIdentityMatch(this.#observationId, expected, 'OBSERVATION_ID_MISMATCH');
    return expected;
  }

  async observePage(options = {}) {
    return this.#enqueue(async () => {
      const normalized = normalizeOptions(options);
      const expected = this.#checkExpectedObservation(normalized);
      const response = await this.#request('observePage', normalized, expected, false);
      const observation = assertObservation(response);
      if (expected !== null && expected !== undefined && observation.previous_observation_id !== expected) {
        throw new BrowserIdentityError('OBSERVATION_CHAIN_MISMATCH');
      }
      if (expected === null && observation.previous_observation_id !== null) {
        throw new BrowserIdentityError('OBSERVATION_CHAIN_MISMATCH');
      }
      this.#observationId = observation.observation_id;
      return observation;
    });
  }

  async listFrames(options = {}) {
    return this.#enqueue(async () => {
      const normalized = normalizeOptions(options);
      const expected = this.#checkExpectedObservation(normalized);
      const response = await this.#request('listFrames', normalized, expected);
      const frames = framesFrom(response);
      const seen = new Set();
      const copied = frames.map((frame) => {
        const value = immutableTransportValue(frame);
        validateFrame(value);
        if (seen.has(value.id)) fail('INVALID_FRAMES');
        seen.add(value.id);
        return value;
      });
      return Object.freeze(copied);
    });
  }

  async resolveControl(controlOrOptions, maybeOptions = {}) {
    return this.#enqueue(async () => {
      let payload;
      if (isRecord(controlOrOptions) && Object.prototype.hasOwnProperty.call(controlOrOptions, 'control')) {
        payload = { ...controlOrOptions, ...normalizeOptions(maybeOptions) };
      } else {
        payload = { control: controlOrOptions, ...normalizeOptions(maybeOptions) };
      }
      const expected = this.#checkExpectedObservation(payload);
      if (payload.control === undefined && payload.controlReference === undefined && payload.fieldId === undefined) {
        fail('INVALID_CONTROL');
      }
      return await this.#request('resolveControl', payload, expected);
    });
  }

  async performAction(input, maybeOptions = {}) {
    return this.#enqueue(async () => {
      const options = normalizeOptions(maybeOptions);
      const actionInput = isRecord(input) && Object.prototype.hasOwnProperty.call(input, 'action')
        ? input
        : (isRecord(options.action) ? options.action : input);
      const action = normalizeAction(actionInput);
      const expected = action.observationId ?? this.#checkExpectedObservation(options);
      if (expected !== null && expected !== undefined) ensureIdentityMatch(this.#observationId, expected, 'OBSERVATION_ID_MISMATCH');
      const payload = { ...options, action };
      delete payload.action;
      payload.action = action;
      return await this.#request('performAction', payload, expected);
    });
  }

  async uploadFile(input, filePathOrOptions = undefined, maybeOptions = {}) {
    return this.#enqueue(async () => {
      let payload;
      if (isRecord(input)) {
        payload = { ...input, ...normalizeOptions(filePathOrOptions) };
      } else if (typeof input === 'string' && typeof filePathOrOptions === 'string') {
        payload = { controlReference: input, filePath: filePathOrOptions, ...normalizeOptions(maybeOptions) };
      } else if (typeof input === 'string') {
        payload = { filePath: input, ...normalizeOptions(filePathOrOptions) };
      } else {
        fail('INVALID_UPLOAD');
      }
      if (typeof payload.filePath !== 'string') fail('INVALID_UPLOAD');
      assertSafeString(payload.filePath, 'INVALID_UPLOAD');
      if (payload.controlReference !== undefined) normalizeIdentity(payload.controlReference, 'INVALID_UPLOAD');
      const expected = this.#checkExpectedObservation(payload);
      return await this.#request('uploadFile', payload, expected);
    });
  }

  async captureScreenshot(options = {}) {
    return this.#enqueue(async () => {
      const normalized = normalizeOptions(options);
      const expected = this.#checkExpectedObservation(normalized);
      return await this.#request('captureScreenshot', normalized, expected);
    });
  }

  async waitForPageState(stateOrOptions, maybeOptions = {}) {
    return this.#enqueue(async () => {
      let payload;
      if (isRecord(stateOrOptions)) {
        payload = { ...stateOrOptions, ...normalizeOptions(maybeOptions) };
      } else {
        payload = { state: stateOrOptions, ...normalizeOptions(maybeOptions) };
      }
      if (payload.state !== undefined) assertSafeString(payload.state, 'INVALID_PAGE_STATE');
      if (payload.timeoutMs !== undefined) assertTimeout(payload.timeoutMs, 'INVALID_PAGE_STATE');
      const expected = this.#checkExpectedObservation(payload);
      return await this.#request('waitForPageState', payload, expected);
    });
  }

  async reattachWorkspace(input = {}) {
    return this.#enqueue(async () => {
      const payload = normalizeOptions(input, 'INVALID_REATTACH');
      if (payload.workspaceId === undefined && this.#workspaceId === null) fail('WORKSPACE_ID_REQUIRED');
      const requested = payload.workspaceId ?? this.#workspaceId;
      normalizeIdentity(requested, 'INVALID_REATTACH');
      ensureIdentityMatch(this.#workspaceId, requested, 'WORKSPACE_ID_MISMATCH');
      payload.workspaceId = requested;
      const response = await this.#request('reattachWorkspace', payload, this.#observationId);
      const workspaceId = resultWorkspaceId(response);
      if (workspaceId === undefined) throw new BrowserIdentityError('WORKSPACE_ID_MISSING');
      normalizeIdentity(workspaceId, 'WORKSPACE_ID_MISMATCH');
      if (workspaceId !== requested) throw new BrowserIdentityError('WORKSPACE_ID_MISMATCH');
      const observationId = resultObservationId(response);
      if (observationId !== undefined) {
        normalizeIdentity(observationId, 'OBSERVATION_ID_MISMATCH');
        ensureIdentityMatch(this.#observationId, observationId, 'OBSERVATION_ID_MISMATCH');
        if (this.#observationId === null) this.#observationId = observationId;
      }
      this.#workspaceId = requested;
      return response;
    });
  }
}

export function createBrowserAdapter(transport, options = {}) {
  return new BrowserAdapter(transport, options);
}
