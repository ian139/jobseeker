import path from 'node:path';
import os from 'node:os';

import {
  BrowserAdapterError,
  BrowserTransportError,
  cloneTransportValue,
} from './browser-adapter.mjs';

export const CMUX_GUI_BROWSER_RUNTIME_SCHEMA = 'phase1-cmux-gui-browser-runtime-v1';
export const CMUX_GUI_BROWSER_PROFILE_MODE = 'persistent';

const BINDING_KEYS = new Set([
  'windowId',
  'workspaceId',
  'surfaceId',
  'socketPath',
  'profileMode',
]);

const RUNTIME_IDENTITY_KEYS = Object.freeze([
  ['windowId', 'WINDOW_IDENTITY_MISMATCH'],
  ['workspaceId', 'WORKSPACE_IDENTITY_MISMATCH'],
  ['surfaceId', 'SURFACE_IDENTITY_MISMATCH'],
]);

const UUID_REGEX = /^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$/;

export class CmuxGuiBrowserRuntimeError extends BrowserAdapterError {
  constructor(code, message = code) {
    super(code, message);
    this.name = 'CmuxGuiBrowserRuntimeError';
  }
}

function fail(code) {
  throw new CmuxGuiBrowserRuntimeError(code);
}

function isRecord(value) {
  return value !== null && typeof value === 'object' && !Array.isArray(value);
}

function assertPlainRecord(value, code = 'INVALID_BINDING') {
  if (!isRecord(value)) fail(code);
  const prototype = Object.getPrototypeOf(value);
  if (prototype !== Object.prototype && prototype !== null) fail(code);
  for (const key of Reflect.ownKeys(value)) {
    if (typeof key !== 'string') fail(code);
    const descriptor = Object.getOwnPropertyDescriptor(value, key);
    if (!descriptor || !('value' in descriptor)) fail(code);
  }
  return value;
}

function assertSafeText(value, code, maximum) {
  if (typeof value !== 'string' || value.length === 0 || value.length > maximum) fail(code);
  if (value.trim() !== value || /[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f]/u.test(value)) fail(code);
  return value;
}

function validateUuid(value, code) {
  if (typeof value !== 'string' || value.length === 0 || value.length > 512) fail(code);
  if (value.trim() !== value || !UUID_REGEX.test(value)) fail(code);
  return value;
}

function validateSocketPath(value) {
  const source = assertSafeText(value, 'INVALID_SOCKET_PATH', 4096);
  if (!path.posix.isAbsolute(source)) fail('INVALID_SOCKET_PATH');
  const normalized = path.posix.normalize(source);
  const ownerSocketDirectory = path.posix.join(os.homedir(), '.local', 'state', 'cmux');
  const relative = path.posix.relative(ownerSocketDirectory, normalized);
  if (normalized !== source
    || relative.length === 0
    || relative.startsWith('../')
    || relative.includes('/')
    || !/^cmux-[1-9][0-9]*\.sock$/u.test(relative)) {
    fail('INVALID_SOCKET_PATH');
  }
  return source;
}

function validateBindingInput(value) {
  const input = assertPlainRecord(value, 'INVALID_BINDING');
  for (const key of Object.keys(input)) {
    if (!BINDING_KEYS.has(key)) fail('INVALID_BINDING');
  }

  if (!Object.prototype.hasOwnProperty.call(input, 'windowId')) fail('INVALID_WINDOW_ID');
  if (!Object.prototype.hasOwnProperty.call(input, 'workspaceId')) fail('INVALID_WORKSPACE_ID');
  if (!Object.prototype.hasOwnProperty.call(input, 'surfaceId')) fail('INVALID_SURFACE_ID');
  if (!Object.prototype.hasOwnProperty.call(input, 'socketPath')) fail('INVALID_SOCKET_PATH');

  const windowId = validateUuid(input.windowId, 'INVALID_WINDOW_ID');
  const workspaceId = validateUuid(input.workspaceId, 'INVALID_WORKSPACE_ID');
  const surfaceId = validateUuid(input.surfaceId, 'INVALID_SURFACE_ID');
  const socketPath = validateSocketPath(input.socketPath);

  const profileMode = input.profileMode === undefined
    ? CMUX_GUI_BROWSER_PROFILE_MODE
    : input.profileMode;

  if (profileMode === 'ephemeral') fail('EPHEMERAL_PROFILE_FORBIDDEN');
  if (profileMode !== CMUX_GUI_BROWSER_PROFILE_MODE) fail('INVALID_BINDING');

  const binding = {
    windowId,
    workspaceId,
    surfaceId,
    socketPath,
    profileMode,
  };

  return Object.freeze(binding);
}

export function validateCmuxGuiBrowserBinding(value) {
  return validateBindingInput(value);
}

export function createCmuxGuiBrowserBinding(value) {
  return validateBindingInput(value);
}

function withRuntimeIdentity(payload, binding) {
  const base = payload === undefined
    ? {}
    : (isRecord(payload) ? cloneTransportValue(payload) : { value: cloneTransportValue(payload) });
  base.windowId = binding.windowId;
  base.workspaceId = binding.workspaceId;
  base.surfaceId = binding.surfaceId;
  return Object.freeze(base);
}

function identityMismatch(key, expected, actual) {
  if (typeof actual !== 'string' || actual !== expected) {
    const entry = RUNTIME_IDENTITY_KEYS.find(([k]) => k === key);
    const code = entry ? entry[1] : 'INVALID_BINDING';
    throw new CmuxGuiBrowserRuntimeError(code);
  }
}

function validateResultIdentity(value, binding, seen = new WeakSet()) {
  if (value === null || typeof value !== 'object') return;
  if (value instanceof Uint8Array || value instanceof ArrayBuffer) return;
  if (seen.has(value)) return;
  seen.add(value);
  if (Array.isArray(value)) {
    for (const item of value) validateResultIdentity(item, binding, seen);
    return;
  }
  for (const [key] of RUNTIME_IDENTITY_KEYS) {
    if (Object.prototype.hasOwnProperty.call(value, key)) identityMismatch(key, binding[key], value[key]);
  }
  for (const child of Object.values(value)) validateResultIdentity(child, binding, seen);
}

function sanitizeResult(value) {
  try {
    return cloneTransportValue(value);
  } catch (_) {
    throw new BrowserTransportError('UNSAFE_TRANSPORT_RESULT');
  }
}

function validateRuntimeResult(value, binding) {
  if (value === undefined) return value;
  const sanitized = sanitizeResult(value);
  validateResultIdentity(sanitized, binding);
  return sanitized;
}

function invokeTransport(transport, method, request) {
  if (typeof transport[method] === 'function') return transport[method].call(transport, request);
  if (typeof transport.request === 'function') return transport.request.call(transport, method, request);
  if (typeof transport.invoke === 'function') return transport.invoke.call(transport, method, request);
  throw new BrowserTransportError('TRANSPORT_METHOD_UNAVAILABLE');
}

function hasTransportRoute(transport, method) {
  return typeof transport[method] === 'function' ||
    typeof transport.request === 'function' ||
    typeof transport.invoke === 'function';
}

function createRuntimeTransport(transport, binding) {
  const wrapped = {
    request(method, payload) {
      const request = withRuntimeIdentity(payload, binding);
      return Promise.resolve()
        .then(() => invokeTransport(transport, method, request))
        .then((result) => validateRuntimeResult(result, binding));
    },
    closeTarget(payload = {}) {
      if (!hasTransportRoute(transport, 'closeTarget')) {
        throw new CmuxGuiBrowserRuntimeError('TARGET_CLOSE_UNAVAILABLE');
      }
      const request = withRuntimeIdentity(payload, binding);
      return Promise.resolve()
        .then(() => invokeTransport(transport, 'closeTarget', request))
        .then((result) => validateRuntimeResult(result, binding));
    },
  };
  return Object.freeze(wrapped);
}

export function createCmuxGuiBrowserTransport(transport, binding) {
  const normalizedBinding = validateCmuxGuiBrowserBinding(binding);
  if (transport === null || typeof transport !== 'object') {
    throw new BrowserTransportError('TRANSPORT_REQUIRED');
  }
  return createRuntimeTransport(transport, normalizedBinding);
}

export function validateCmuxGuiBrowserResult(value, binding) {
  const normalizedBinding = validateCmuxGuiBrowserBinding(binding);
  return validateRuntimeResult(value, normalizedBinding);
}
