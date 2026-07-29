import crypto from 'node:crypto';
import { validateObservation } from './visual-observation.mjs';

export const COMPUTER_USE_ADAPTER_SCHEMA = 'phase1-computer-use-adapter-v1';
export const VISUAL_OBSERVATION_SCHEMA = 'phase1-visual-observation-v1';
export const MODEL_PROVIDERS = Object.freeze(['codex', 'gemini']);
export const DEFAULT_MODEL_PROVIDER = 'codex';
export const NORMALIZED_ACTIONS = Object.freeze(['click', 'type_text', 'press_key', 'scroll', 'upload_file']);

const ACTION_SET = new Set(NORMALIZED_ACTIONS);
const PROVIDER_SET = new Set(MODEL_PROVIDERS);
const SHA256_HEX = /^[0-9a-f]{64}$/u;
const IDENTIFIER_MAX = 512;
const STRING_MAX = 64 * 1024;
const PATH_MAX = 16 * 1024;
const ARRAY_MAX = 1024;
const OBJECT_KEYS_MAX = 256;
const DEPTH_MAX = 16;
const BINARY_MAX = 8 * 1024 * 1024;
const VIEWPORT_MAX = 100_000;
const SCROLL_MAX = 1_000_000;
const MODEL_MAX = 512;

const VIEW_KEYS = new Set(['surfaceId', 'screenshotPath', 'screenshotSha256', 'viewport', 'url', 'title']);
const VIEW_OPTION_KEYS = new Set(['surfaceId']);
const OBSERVE_OPTION_KEYS = new Set(['surfaceId', 'previousObservationId']);
const ACTION_KEYS = new Set(['action', 'surfaceId', 'observationId', 'targetId', 'text', 'key', 'deltaX', 'deltaY', 'filePath']);
const OUTCOME_KEYS = new Set(['actionId', 'status', 'ok', 'acted', 'errorCode', 'surfaceId', 'observationId', 'screenshotSha256', 'visuallyConfirmed']);
const SENSITIVE_OUTCOME_KEYS = new Set(['text', 'filePath', 'value', 'answer']);

export class ComputerUseAdapterError extends Error {
  constructor(code, message = code) {
    super(message);
    this.name = 'ComputerUseAdapterError';
    this.code = code;
  }
}

export class ComputerTransportError extends ComputerUseAdapterError {
  constructor(code = 'TRANSPORT_FAILURE', message = code) {
    super(message);
    this.name = 'ComputerTransportError';
    this.code = code;
  }
}

export class ComputerIdentityError extends ComputerUseAdapterError {
  constructor(code, message = code) {
    super(message);
    this.name = 'ComputerIdentityError';
    this.code = code;
  }
}

function isRecord(value) {
  if (value === null || typeof value !== 'object' || Array.isArray(value)) return false;
  const prototype = Object.getPrototypeOf(value);
  return prototype === Object.prototype || prototype === null;
}

function fail(code, message = code) {
  throw new ComputerUseAdapterError(code, message);
}

function assertRecord(value, code = 'INVALID_ARGUMENT') {
  if (!isRecord(value)) fail(code);
  return value;
}

function assertNoUnknownKeys(value, allowed, code = 'INVALID_ARGUMENT') {
  for (const key of Object.keys(value)) if (!allowed.has(key)) fail(code, `${code}:${key}`);
}

function assertSafeString(value, code = 'INVALID_STRING', { identifier = false, nullable = false, empty = false, max = STRING_MAX } = {}) {
  if (nullable && value === null) return null;
  if (typeof value !== 'string' || (!empty && value.length === 0) || value.length > max || /\p{Cc}/u.test(value)) fail(code);
  if (identifier && value.trim() !== value) fail(code);
  return value;
}

function assertInteger(value, code = 'INVALID_INTEGER', { min = 0, max = Number.MAX_SAFE_INTEGER } = {}) {
  if (!Number.isSafeInteger(value) || value < min || value > max) fail(code);
  return value;
}

function assertFiniteNumber(value, code = 'INVALID_NUMBER') {
  if (typeof value !== 'number' || !Number.isFinite(value)) fail(code);
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

export function cloneTransportValue(value, path = 'value', depth = 0, seen = new WeakSet()) {
  if (value === null || typeof value === 'boolean') return value;
  if (typeof value === 'number') {
    if (!Number.isFinite(value)) fail('UNSAFE_TRANSPORT_VALUE', path);
    return value;
  }
  if (typeof value === 'string') return assertSafeString(value, 'UNSAFE_TRANSPORT_VALUE', { empty: true });
  if (value === undefined) return undefined;
  if (typeof value === 'bigint' || typeof value === 'function' || typeof value === 'symbol') fail('UNSAFE_TRANSPORT_VALUE', path);
  const binary = cloneBinary(value);
  if (binary !== null) {
    if (binary.byteLength > BINARY_MAX) fail('UNSAFE_TRANSPORT_VALUE', path);
    return binary;
  }
  if (depth > DEPTH_MAX || (Array.isArray(value) && value.length > ARRAY_MAX)) fail('UNSAFE_TRANSPORT_VALUE', path);
  if (!Array.isArray(value) && !isRecord(value)) fail('UNSAFE_TRANSPORT_VALUE', path);
  if (seen.has(value)) fail('UNSAFE_TRANSPORT_VALUE', path);
  seen.add(value);
  let result;
  if (Array.isArray(value)) {
    result = value.map((item, index) => cloneTransportValue(item, `${path}[${index}]`, depth + 1, seen));
  } else {
    const keys = Object.keys(value);
    if (keys.length > OBJECT_KEYS_MAX) fail('UNSAFE_TRANSPORT_VALUE', path);
    result = {};
    for (const key of keys) {
      if (!key || key === '__proto__' || key === 'constructor' || key === 'prototype') fail('UNSAFE_TRANSPORT_VALUE', `${path}.${key}`);
      const descriptor = Object.getOwnPropertyDescriptor(value, key);
      if (!descriptor || !('value' in descriptor)) fail('UNSAFE_TRANSPORT_VALUE', `${path}.${key}`);
      const child = cloneTransportValue(descriptor.value, `${path}.${key}`, depth + 1, seen);
      if (child !== undefined) result[key] = child;
    }
  }
  seen.delete(value);
  return result;
}

function freeze(value, seen = new WeakSet()) {
  if (value === null || typeof value !== 'object' || value instanceof Uint8Array || value instanceof ArrayBuffer) return value;
  if (seen.has(value)) return value;
  seen.add(value);
  for (const child of Object.values(value)) freeze(child, seen);
  return Object.freeze(value);
}

function immutable(value) {
  return freeze(cloneTransportValue(value));
}

function identity(value, code = 'INVALID_ID') {
  return assertSafeString(value, code, { identifier: true, max: IDENTIFIER_MAX });
}

function provider(value, code = 'INVALID_PROVIDER') {
  if (typeof value !== 'string' || !PROVIDER_SET.has(value)) fail(code);
  return value;
}

function model(value, code = 'INVALID_MODEL') {
  return assertSafeString(value, code, { max: MODEL_MAX });
}

function hash(value, code = 'INVALID_SCREENSHOT_HASH') {
  if (typeof value !== 'string' || !SHA256_HEX.test(value)) fail(code);
  return value;
}

function validateViewport(value, code = 'INVALID_VIEWPORT') {
  assertRecord(value, code);
  assertNoUnknownKeys(value, new Set(['width', 'height']), code);
  if (Object.keys(value).length !== 2) fail(code);
  assertInteger(value.width, code, { min: 1, max: VIEWPORT_MAX });
  assertInteger(value.height, code, { min: 1, max: VIEWPORT_MAX });
  return value;
}

function normalizeView(value) {
  const view = assertRecord(value, 'INVALID_VIEW');
  assertNoUnknownKeys(view, VIEW_KEYS, 'INVALID_VIEW');
  if (Object.keys(view).length !== VIEW_KEYS.size) fail('INVALID_VIEW');
  return immutable({
    surfaceId: identity(view.surfaceId, 'INVALID_VIEW'),
    screenshotPath: assertSafeString(view.screenshotPath, 'INVALID_VIEW', { max: PATH_MAX }),
    screenshotSha256: hash(view.screenshotSha256),
    viewport: { ...validateViewport(view.viewport) },
    url: assertSafeString(view.url, 'INVALID_VIEW', { max: STRING_MAX }),
    title: assertSafeString(view.title, 'INVALID_VIEW', { empty: true, max: STRING_MAX }),
  });
}

function normalizeOptions(value, allowed, code) {
  if (value === undefined) return {};
  const options = assertRecord(value, code);
  assertNoUnknownKeys(options, allowed, code);
  const result = {};
  if (options.surfaceId !== undefined) result.surfaceId = identity(options.surfaceId, code);
  if (options.previousObservationId !== undefined) {
    result.previousObservationId = options.previousObservationId === null ? null : identity(options.previousObservationId, code);
  }
  return immutable(result);
}



export function validateVisualObservation(input) {
  try {
    validateObservation(input);
  } catch {
    fail('INVALID_OBSERVATION');
  }
  return immutable(input);
}

function normalizeAction(input) {
  const action = assertRecord(input, 'INVALID_ACTION');
  assertNoUnknownKeys(action, ACTION_KEYS, 'INVALID_ACTION');
  if (!ACTION_SET.has(action.action)) fail('INVALID_ACTION');
  const normalized = {
    action: action.action,
    surfaceId: identity(action.surfaceId, 'INVALID_ACTION'),
    observationId: identity(action.observationId, 'INVALID_ACTION'),
  };
  if (action.targetId !== undefined) normalized.targetId = identity(action.targetId, 'INVALID_ACTION');
  if (action.action === 'click' && normalized.targetId === undefined) fail('INVALID_ACTION');
  if (action.action === 'type_text') normalized.text = assertSafeString(action.text, 'INVALID_ACTION', { max: STRING_MAX });
  else if (action.text !== undefined) fail('INVALID_ACTION');
  if (action.action === 'press_key') normalized.key = assertSafeString(action.key, 'INVALID_ACTION', { max: 256 });
  else if (action.key !== undefined) fail('INVALID_ACTION');
  if (action.action === 'scroll') {
    normalized.deltaX = assertInteger(action.deltaX, 'INVALID_ACTION', { min: -SCROLL_MAX, max: SCROLL_MAX });
    normalized.deltaY = assertInteger(action.deltaY, 'INVALID_ACTION', { min: -SCROLL_MAX, max: SCROLL_MAX });
  } else if (action.deltaX !== undefined || action.deltaY !== undefined) fail('INVALID_ACTION');
  if (action.action === 'upload_file') normalized.filePath = assertSafeString(action.filePath, 'INVALID_ACTION', { max: PATH_MAX });
  else if (action.filePath !== undefined) fail('INVALID_ACTION');
  return immutable(normalized);
}

export const normalizeComputerAction = normalizeAction;

function normalizeOutcome(value) {
  const result = assertRecord(value, 'INVALID_OUTCOME');
  const output = {};
  for (const key of Object.keys(result)) {
    if (SENSITIVE_OUTCOME_KEYS.has(key) || !OUTCOME_KEYS.has(key)) continue;
    const item = result[key];
    if (key === 'actionId' || key === 'surfaceId' || key === 'observationId') output[key] = identity(item, 'INVALID_OUTCOME');
    else if (key === 'screenshotSha256') output[key] = hash(item, 'INVALID_OUTCOME');
    else if (key === 'status') output[key] = assertSafeString(item, 'INVALID_OUTCOME', { identifier: true, max: IDENTIFIER_MAX });
    else if (key === 'errorCode') output[key] = item === null
      ? null
      : assertSafeString(item, 'INVALID_OUTCOME', { identifier: true, max: IDENTIFIER_MAX });
    else if (typeof item === 'boolean') output[key] = item;
    else fail('INVALID_OUTCOME');
  }
  return immutable(output);
}

function transportMethod(transport, method) {
  if (typeof transport[method] !== 'function') throw new ComputerTransportError('TRANSPORT_METHOD_UNAVAILABLE');
  return transport[method].bind(transport);
}

export class ComputerUseAdapter {
  #transport;
  #provider;
  #model;
  #surfaceId = null;
  #screenshotSha256 = null;
  #view = null;
  #observationId = null;
  #observationScreenshotSha256 = null;
  #tail = Promise.resolve();

  constructor(transport, options = {}) {
    if (!isRecord(transport)) throw new ComputerTransportError('TRANSPORT_REQUIRED');
    const normalized = assertRecord(options, 'INVALID_OPTIONS');
    assertNoUnknownKeys(normalized, new Set(['provider', 'model']), 'INVALID_OPTIONS');
    this.#provider = normalized.provider === undefined ? DEFAULT_MODEL_PROVIDER : provider(normalized.provider, 'INVALID_PROVIDER');
    this.#model = normalized.model === undefined ? this.#provider : model(normalized.model, 'INVALID_MODEL');
    for (const method of ['captureView', 'analyzeView', 'performAction']) transportMethod(transport, method);
    this.#transport = transport;
  }

  get provider() { return this.#provider; }
  get model() { return this.#model; }
  get surfaceId() { return this.#surfaceId; }
  get observationId() { return this.#observationId; }
  get screenshotSha256() { return this.#screenshotSha256; }
  get identity() {
    return Object.freeze({ surfaceId: this.#surfaceId, observationId: this.#observationId, screenshotSha256: this.#screenshotSha256 });
  }

  #enqueue(operation) {
    const next = this.#tail.then(operation, operation);
    this.#tail = next.catch(() => undefined);
    return next;
  }

  async #call(method, payload) {
    let raw;
    try {
      raw = await transportMethod(this.#transport, method)(immutable(payload));
    } catch (error) {
      if (error instanceof ComputerUseAdapterError) throw error;
      throw new ComputerTransportError('TRANSPORT_FAILURE');
    }
    if (raw === undefined) throw new ComputerTransportError('EMPTY_TRANSPORT_RESULT');
    try { return immutable(raw); } catch (_) { throw new ComputerTransportError('UNSAFE_TRANSPORT_RESULT'); }
  }

  #assertSurface(surfaceId) {
    if (this.#surfaceId === null || surfaceId !== this.#surfaceId) throw new ComputerIdentityError('SURFACE_ID_MISMATCH');
  }

  #assertObservation(observationId) {
    if (this.#observationId === null || observationId !== this.#observationId) throw new ComputerIdentityError('OBSERVATION_ID_MISMATCH');
    if (this.#observationScreenshotSha256 !== this.#screenshotSha256) throw new ComputerIdentityError('SCREENSHOT_ID_MISMATCH');
  }

  #assertViewCurrent(view) {
    if (this.#view === null || view.surfaceId !== this.#surfaceId || view.screenshotSha256 !== this.#screenshotSha256 ||
        view.url !== this.#view.url || view.title !== this.#view.title || view.viewport.width !== this.#view.viewport.width ||
        view.viewport.height !== this.#view.viewport.height) throw new ComputerIdentityError('VIEW_IDENTITY_MISMATCH');
  }

  async #captureView(options) {
    const normalizedOptions = normalizeOptions(options, VIEW_OPTION_KEYS, 'INVALID_CAPTURE_OPTIONS');
    if (normalizedOptions.surfaceId !== undefined && this.#surfaceId !== null && normalizedOptions.surfaceId !== this.#surfaceId) throw new ComputerIdentityError('SURFACE_ID_MISMATCH');
    const view = normalizeView(await this.#call('captureView', normalizedOptions));
    if (normalizedOptions.surfaceId !== undefined && view.surfaceId !== normalizedOptions.surfaceId) throw new ComputerIdentityError('SURFACE_ID_MISMATCH');
    this.#surfaceId = view.surfaceId;
    this.#screenshotSha256 = view.screenshotSha256;
    this.#view = view;
    return view;
  }

  async #analyzeView(view, options) {
    const normalizedView = normalizeView(view);
    this.#assertViewCurrent(normalizedView);
    const normalizedOptions = normalizeOptions(options, OBSERVE_OPTION_KEYS, 'INVALID_ANALYZE_OPTIONS');
    const previousObservationId = normalizedOptions.previousObservationId === undefined ? this.#observationId : normalizedOptions.previousObservationId;
    if (previousObservationId !== null && this.#observationId !== previousObservationId) throw new ComputerIdentityError('OBSERVATION_ID_MISMATCH');
    const observation = validateVisualObservation(await this.#call('analyzeView', {
      provider: this.#provider,
      model: this.#model,
      view: normalizedView,
      previousObservationId: previousObservationId ?? null,
    }));
    if (observation.surface.surface_id !== normalizedView.surfaceId || observation.surface.screenshot_sha256 !== normalizedView.screenshotSha256 ||
        observation.surface.url !== normalizedView.url || observation.surface.title !== normalizedView.title ||
        observation.surface.viewport.width !== normalizedView.viewport.width || observation.surface.viewport.height !== normalizedView.viewport.height) {
      throw new ComputerIdentityError('SCREENSHOT_ID_MISMATCH');
    }
    if (observation.agent.provider !== this.#provider || observation.agent.model !== this.#model) throw new ComputerIdentityError('AGENT_IDENTITY_MISMATCH');
    if (observation.previous_observation_id !== (previousObservationId ?? null)) throw new ComputerIdentityError('OBSERVATION_CHAIN_MISMATCH');
    this.#observationId = observation.observation_id;
    this.#observationScreenshotSha256 = observation.surface.screenshot_sha256;
    return observation;
  }

  captureView(options = {}) { return this.#enqueue(() => this.#captureView(options)); }
  analyzeView(view, options = {}) { return this.#enqueue(() => this.#analyzeView(view, options)); }

  observe(options = {}) {
    return this.#enqueue(async () => {
      const normalized = normalizeOptions(options, OBSERVE_OPTION_KEYS, 'INVALID_OBSERVE_OPTIONS');
      const previousObservationId = normalized.previousObservationId === undefined ? this.#observationId : normalized.previousObservationId;
      const view = await this.#captureView(
        normalized.surfaceId === undefined ? {} : { surfaceId: normalized.surfaceId },
      );
      return this.#analyzeView(view, { previousObservationId });
    });
  }

  performAction(input) {
    return this.#enqueue(async () => {
      const action = normalizeAction(input);
      this.#assertSurface(action.surfaceId);
      this.#assertObservation(action.observationId);
      const outcome = normalizeOutcome(await this.#call('performAction', action));
      if (outcome.surfaceId !== undefined && outcome.surfaceId !== action.surfaceId) throw new ComputerIdentityError('SURFACE_ID_MISMATCH');
      if (outcome.observationId !== undefined && outcome.observationId !== action.observationId) throw new ComputerIdentityError('OBSERVATION_ID_MISMATCH');
      if (outcome.screenshotSha256 !== undefined && outcome.screenshotSha256 !== this.#screenshotSha256) throw new ComputerIdentityError('SCREENSHOT_ID_MISMATCH');
      return outcome;
    });
  }
}

export function createComputerUseAdapter(transport, options = {}) {
  return new ComputerUseAdapter(transport, options);
}

export function sha256Bytes(value) {
  const bytes = value instanceof Uint8Array ? value : (value instanceof ArrayBuffer ? new Uint8Array(value) : null);
  if (bytes === null || bytes.byteLength > BINARY_MAX) fail('INVALID_IMAGE');
  return crypto.createHash('sha256').update(bytes).digest('hex');
}
