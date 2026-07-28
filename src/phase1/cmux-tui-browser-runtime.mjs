import path from 'node:path';

import {
  BrowserAdapterError,
  BrowserTransportError,
  cloneTransportValue,
} from './browser-adapter.mjs';

export const CMUX_TUI_BROWSER_RUNTIME_SCHEMA = 'phase1-cmux-tui-browser-runtime-v1';
export const CMUX_TUI_BROWSER_PROFILE_MODE = 'persistent';

const BINDING_KEYS = new Set([
  'muxSessionId',
  'targetId',
  'cdpUrl',
  'profileMode',
  'userDataDir',
]);
const IDENTIFIER_MAX = 512;
const TEXT_MAX = 16 * 1024;
const USER_DATA_DIR_MAX = 4096;
const RUNTIME_IDENTITY_KEYS = Object.freeze([
  ['muxSessionId', 'RUNTIME_IDENTITY_MISMATCH'],
  ['targetId', 'TARGET_IDENTITY_MISMATCH'],
]);

export class CmuxTuiBrowserRuntimeError extends BrowserAdapterError {
  constructor(code, message = code) {
    super(code, message);
    this.name = 'CmuxTuiBrowserRuntimeError';
  }
}

function fail(code) {
  throw new CmuxTuiBrowserRuntimeError(code);
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

function assertIdentifier(value, code) {
  return assertSafeText(value, code, IDENTIFIER_MAX);
}

function isLoopbackHostname(hostname) {
  const host = hostname.toLowerCase();
  if (host === 'localhost' || host === '::1') return true;
  const parts = host.split('.');
  if (parts.length !== 4 || parts[0] !== '127') return false;
  return parts.slice(1).every((part) => /^(?:0|[1-9]\d{0,2})$/u.test(part) && Number(part) <= 255);
}

function validateCdpUrl(value) {
  const source = assertSafeText(value, 'INVALID_CDP_URL', TEXT_MAX);
  let parsed;
  try {
    parsed = new URL(source);
  } catch (_) {
    fail('INVALID_CDP_URL');
  }
  if (parsed.protocol !== 'http:' && parsed.protocol !== 'ws:') fail('INVALID_CDP_URL');
  if (parsed.username !== '' || parsed.password !== '' || parsed.hash !== '') fail('INVALID_CDP_URL');
  const hostname = parsed.hostname.startsWith('[') && parsed.hostname.endsWith(']')
    ? parsed.hostname.slice(1, -1)
    : parsed.hostname;
  if (!isLoopbackHostname(hostname)) fail('INVALID_CDP_URL');
  return source;
}

function normalizedPosixPath(value) {
  return path.posix.normalize(value);
}

function isWithin(parent, candidate) {
  return candidate === parent || candidate.startsWith(`${parent}/`);
}

function isOsDefaultChromeProfile(value) {
  const candidate = normalizedPosixPath(value);
  const home = typeof process.env.HOME === 'string' && process.env.HOME.length > 0
    ? normalizedPosixPath(process.env.HOME)
    : null;
  if (home === null) return false;
  const defaults = [
    path.posix.join(home, 'Library/Application Support/Google/Chrome'),
    path.posix.join(home, 'Library/Application Support/Chromium'),
    path.posix.join(home, 'Library/Application Support/Microsoft Edge'),
  ];
  return defaults.some((defaultPath) => isWithin(defaultPath, candidate));
}

function validateUserDataDir(value) {
  const source = assertSafeText(value, 'INVALID_USER_DATA_DIR', USER_DATA_DIR_MAX);
  if (!path.posix.isAbsolute(source)) fail('INVALID_USER_DATA_DIR');
  const normalized = normalizedPosixPath(source);
  if (normalized !== source || normalized === '/') fail('INVALID_USER_DATA_DIR');
  if (isOsDefaultChromeProfile(normalized)) fail('INVALID_USER_DATA_DIR');
  return source;
}

function validateBindingInput(value) {
  const input = assertPlainRecord(value);
  for (const key of Object.keys(input)) {
    if (!BINDING_KEYS.has(key)) fail('INVALID_BINDING');
  }
  if (!Object.prototype.hasOwnProperty.call(input, 'muxSessionId')) fail('INVALID_MUX_SESSION_ID');
  if (!Object.prototype.hasOwnProperty.call(input, 'targetId')) fail('INVALID_TARGET_ID');
  if (!Object.prototype.hasOwnProperty.call(input, 'cdpUrl')) fail('INVALID_CDP_URL');

  const muxSessionId = assertIdentifier(input.muxSessionId, 'INVALID_MUX_SESSION_ID');
  const targetId = assertIdentifier(input.targetId, 'INVALID_TARGET_ID');
  const cdpUrl = validateCdpUrl(input.cdpUrl);
  const profileMode = input.profileMode === undefined
    ? CMUX_TUI_BROWSER_PROFILE_MODE
    : input.profileMode;
  if (profileMode === 'ephemeral') fail('EPHEMERAL_PROFILE_FORBIDDEN');
  if (profileMode !== CMUX_TUI_BROWSER_PROFILE_MODE) fail('INVALID_BINDING');

  const binding = {
    muxSessionId,
    targetId,
    cdpUrl,
    profileMode,
  };
  if (input.userDataDir !== undefined) binding.userDataDir = validateUserDataDir(input.userDataDir);
  return Object.freeze(binding);
}

export function validateCmuxTuiBrowserBinding(value) {
  return validateBindingInput(value);
}

export function createCmuxTuiBrowserBinding(value) {
  return validateBindingInput(value);
}

function withRuntimeIdentity(payload, binding) {
  const base = payload === undefined
    ? {}
    : (isRecord(payload) ? cloneTransportValue(payload) : { value: cloneTransportValue(payload) });
  base.muxSessionId = binding.muxSessionId;
  base.targetId = binding.targetId;
  return Object.freeze(base);
}

function identityMismatch(key, expected, actual) {
  if (typeof actual !== 'string' || actual !== expected) {
    const code = key === 'muxSessionId' ? 'RUNTIME_IDENTITY_MISMATCH' : 'TARGET_IDENTITY_MISMATCH';
    throw new CmuxTuiBrowserRuntimeError(code);
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
        throw new CmuxTuiBrowserRuntimeError('TARGET_CLOSE_UNAVAILABLE');
      }
      const request = withRuntimeIdentity(payload, binding);
      return Promise.resolve()
        .then(() => invokeTransport(transport, 'closeTarget', request))
        .then((result) => validateRuntimeResult(result, binding));
    },
  };
  return Object.freeze(wrapped);
}

export function createCmuxTuiBrowserTransport(transport, binding) {
  const normalizedBinding = validateCmuxTuiBrowserBinding(binding);
  if (transport === null || typeof transport !== 'object') {
    throw new BrowserTransportError('TRANSPORT_REQUIRED');
  }
  return createRuntimeTransport(transport, normalizedBinding);
}

export function validateCmuxTuiBrowserResult(value, binding) {
  const normalizedBinding = validateCmuxTuiBrowserBinding(binding);
  return validateRuntimeResult(value, normalizedBinding);
}
