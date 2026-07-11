#!/usr/bin/env node
'use strict';
process.umask(0o077);

const crypto = require('node:crypto');
const dns = require('node:dns');
const fs = require('node:fs');
const http = require('node:http');
const net = require('node:net');
const os = require('node:os');
const path = require('node:path');
const { URL } = require('node:url');

const MAX_IN_FRAME = 256 * 1024;
const MAX_OUT_FRAME = 2 * 1024 * 1024;
const MAX_OBSERVATION_BYTES = 1_900_000;
const STATIC_TYPES = new Set(['stylesheet', 'script', 'image', 'font', 'media']);
const POLICY_PATH = path.join(__dirname, 'safety_policy.json');
const POLICY_HASH = crypto.createHash('sha256').update(fs.readFileSync(POLICY_PATH)).digest('hex');
const SAFETY_POLICY = JSON.parse(fs.readFileSync(POLICY_PATH, 'utf8'));
const ROUTE_POLICIES = Object.freeze({
  greenhouse: SAFETY_POLICY.greenhouse_route_policy,
  lever: SAFETY_POLICY.lever_route_policy,
});
const EXPECTED_ROUTE_VERSIONS = Object.freeze({
  greenhouse: '2026-07-10.greenhouse-routes.v1',
  lever: '2026-07-10.lever-routes.v1',
});
let selectedPolicyName = 'greenhouse';
let ROUTES = ROUTE_POLICIES.greenhouse || {};
let FINAL_LIKE_TOKENS = new Set(ROUTES.final_like_tokens || []);
let ALLOWED_ATS_HOSTS = new Set((ROUTES.public_host_constraints?.allowed_hosts || []).map(value => String(value).toLowerCase()));
const LEVER_HOSTS = new Set(['jobs.lever.co', 'jobs.eu.lever.co']);
let STATIC_HOSTS = new Set((ROUTES.approved_static_get_head?.hosts || []).map(value => String(value).toLowerCase()));
let STATIC_PREFIXES = ROUTES.approved_static_get_head?.path_prefixes || {};
let MAX_REDIRECTS = ROUTES.redirect_caps?.max_redirects || 10;
let MAX_STATIC_REQUESTS = ROUTES.redirect_caps?.max_static_requests || 200;
let MAX_STATIC_BYTES = ROUTES.redirect_caps?.max_static_bytes || 20 * 1024 * 1024;
let MAX_PATH_BYTES = ROUTES.approved_static_get_head?.path_caps?.max_path_bytes || 2048;
let MAX_QUERY_BYTES = ROUTES.approved_static_get_head?.path_caps?.max_query_bytes || 2048;
const SAFE_RUNNER_ERROR_CODES = new Set([
  'unsupported_ats',
  'invalid_ats_policy',
  'invalid_application_url',
  'final_like_route',
  'unsafe_navigation_target',
  'unsafe_network_attempt',
  'redirect_limit_exceeded',
  'invalid_path_encoding',
  'unsafe_path',
  'resolver_address_count',
  'resolver_address_rejected',
  'resolver_hostname_required',
  'local_transport_not_numeric',
  'proxy_authorization_revoked',
  'response_body_too_large',
  'puppeteer_version_mismatch',
  'chromium_executable_missing',
  'browser_process_missing',
  'browser_disconnected',
  'ats_policy_mismatch',
  'page_not_stable',
  'query_selector_unavailable',
  'observation_too_large',
  'artifact_error',
  'startup_identity_required',
  'startup_identity_mismatch',
  'browser_handshake_failed',
  'browser_identity_mismatch',
  'process_identity_mismatch',
  'headless_handoff_forbidden',
  'handoff_not_eligible',
  'handoff_state_conflict',
  'stale_generation',
  'generation_already_consumed',
  'field_identity_collision',
  'sensitive_field',
  'target_not_actionable',
  'invalid_field_value',
  'invalid_boolean_value',
  'invalid_select_value',
  'upload_path_forbidden',
  'upload_accept_mismatch',
  'staged_input_mismatch',
  'staged_input_changed',
  'field_value_not_retained',
  'final_or_anchor_not_automated',
  'button_not_hit_tested',
  'prototype_poisoned',
  'artifact_budget',
  'file_budget',
  'input_frame_too_large',
  'invalid_frame_prefix',
  'invalid_json_frame',
  'invalid_command_frame',
  'browser_launch_timeout',
  'browser_launch_failed',
  'navigation_timeout',
  'navigation_dns_failed',
  'navigation_connection_failed',
  'navigation_tls_failed',
  'observation_timeout',
  'browser_command_failed',
]);
function selectRoutePolicy(name) {
  if (typeof name !== 'string' || !Object.hasOwn(ROUTE_POLICIES, name)) throw new Error('unsupported_ats');
  const graph = ROUTE_POLICIES[name];
  if (!graph || graph.version !== EXPECTED_ROUTE_VERSIONS[name]
      || !Array.isArray(graph.automated_initial_get?.routes)
      || !Array.isArray(graph.approved_static_get_head?.hosts)
      || !graph.public_host_constraints?.allowed_hosts) throw new Error('invalid_ats_policy');
  selectedPolicyName = name;
  ROUTES = graph;
  FINAL_LIKE_TOKENS = new Set(graph.final_like_tokens || []);
  ALLOWED_ATS_HOSTS = new Set((graph.public_host_constraints.allowed_hosts || []).map(value => String(value).toLowerCase()));
  STATIC_HOSTS = new Set((graph.approved_static_get_head.hosts || []).map(value => String(value).toLowerCase()));
  STATIC_PREFIXES = graph.approved_static_get_head.path_prefixes || {};
  MAX_REDIRECTS = graph.redirect_caps?.max_redirects || 10;
  MAX_STATIC_REQUESTS = graph.redirect_caps?.max_static_requests || 200;
  MAX_STATIC_BYTES = graph.redirect_caps?.max_static_bytes || 20 * 1024 * 1024;
  MAX_PATH_BYTES = graph.approved_static_get_head.path_caps?.max_path_bytes || 2048;
  MAX_QUERY_BYTES = graph.approved_static_get_head.path_caps?.max_query_bytes || 2048;
}
const SAFETY_TERMS = new Set((SAFETY_POLICY.terms || []).map(value => String(value).toLowerCase()));
const SAFETY_COMPACT = new Set((SAFETY_POLICY.compact_aliases || []).map(value => String(value).toLowerCase().replace(/[^a-z0-9]/g, '')));

const PACKAGE_SEARCH_PATHS = [
  __dirname,
  ...(process.env.JOBS_ASSISTANT_PUPPETEER_ROOT ? [process.env.JOBS_ASSISTANT_PUPPETEER_ROOT] : []),
];
let puppeteer;
try {
  const packageEntry = require.resolve('puppeteer', { paths: PACKAGE_SEARCH_PATHS });
  puppeteer = require(packageEntry);
} catch (error) {
  // This path intentionally does not include a path, endpoint, or environment
  // detail in the protocol error.
  void writeFrame({ ok: false, error: safeErrorCode(error, 'preflight') });
  process.exit(process.argv.includes('--preflight') ? 1 : 0);
}

let browser = null;
let page = null;
let proxy = null;
const proxySockets = new Set();
const proxyTunnelExpiries = new Map();
function trackProxyTunnel(socket, expiresAt) {
  proxySockets.add(socket);
  if (expiresAt) {
    proxyTunnelExpiries.set(socket, expiresAt);
    const delay = Math.max(1, expiresAt - Date.now());
    setTimeout(() => {
      if (Date.now() >= expiresAt) {
        try { socket.destroy(); } catch {}
        proxyTunnelExpiries.delete(socket);
      }
    }, delay).unref?.();
  }
  socket.once('close', () => {
    proxySockets.delete(socket);
    proxyTunnelExpiries.delete(socket);
  });
}
let proxyFrozen = false;
let userDataDir = null;
let ownerRoot = null;
let commandQueue = Promise.resolve();
let inputBuffer = Buffer.alloc(0);
let observationGeneration = 0;
let currentGeneration = null;
const MAX_SCREENSHOTS_PER_RUN = 10;
const MAX_SCREENSHOT_BYTES = 20 * 1024 * 1024;
const MAX_SCREENSHOT_TOTAL_BYTES = 50 * 1024 * 1024;
const screenshotRecords = new Map();
let screenshotTotalBytes = 0;
let generationConsumed = true;
let generationBlocked = false;
let generationBlocker = null;
let handleCache = new Map();
let firstApplicantMutation = false;
let terminalReason = null;
let logicalInitialUrl = null;
let documentRouteIdentity = null;
let documentRedirectCount = 0;
let internalTransportUrl = null;
let staticRequests = 0;
let staticBytes = 0;
let reviewState = 'closed';
let reviewToken = null;
// A permit is retained until both the request interception and the strict
// proxy validate the same navigation.  Ancillary fetches never consume it.
let reviewPermit = null;
let reviewLedger = new Map();
let reviewEpoch = null;
const proxyPermitUrls = new Map();
let pendingNetwork = 0;
let lastNetworkActivity = Date.now();
let initialQuietReady = false;
let launchHeadless = true;
let cleanupStarted = false;
let cleanupPromise = null;
let heartbeatTimer = null;
let startupIdentity = null;
let budgetTimer = null;
let browserExit = null;
let browserIdentity = null;
let closeRequested = false;
let detachedOwner = false;
let detachedCloseRequested = false;
let cleanupTrigger = null;

const networkCounters = {
  allowed: 0,
  denied: 0,
  dnsLookups: 0,
  attackerDnsLookups: 0,
  attackerHttpRequests: 0,
  finalLikeDenied: 0,
  proxyRequests: 0,
  upstreamConnectAttempts: 0,
  upstreamHttpAttempts: 0,
  redirectsDenied: 0,
  responseBytesRejected: 0,
};

async function writeFrame(payload) {
  const body = Buffer.from(JSON.stringify(payload), 'utf8');
  if (body.length > MAX_OUT_FRAME) throw new Error('output_frame_too_large');
  const prefix = Buffer.from(`${body.length}\n`, 'ascii');
  if (!process.stdout.write(prefix)) await onceDrain();
  if (!process.stdout.write(body)) await onceDrain();
}
function onceDrain() {
  return new Promise(resolve => process.stdout.once('drain', resolve));
}
async function send(data) { await writeFrame({ ok: true, data }); }
function classifyNativeBrowserError(error, action) {
  const code = typeof error?.code === 'string' ? error.code : null;
  const message = typeof error?.message === 'string' ? error.message : '';
  if (code && SAFE_RUNNER_ERROR_CODES.has(code)) return code;
  if (message && SAFE_RUNNER_ERROR_CODES.has(message)) return message;
  if (error?.name === 'TimeoutError') {
    if (action === 'launch') return 'browser_launch_timeout';
    if (action === 'goto') return 'navigation_timeout';
    if (action === 'observe') return 'observation_timeout';
  }
  if (action === 'goto') {
    if (message.includes('ERR_NAME_NOT_RESOLVED') || message.includes('ENOTFOUND')) return 'navigation_dns_failed';
    if (message.includes('ERR_CONNECTION_') || message.includes('ECONNREFUSED') || message.includes('ECONNRESET')) return 'navigation_connection_failed';
    if (message.includes('ERR_CERT_') || message.includes('CERT_')) return 'navigation_tls_failed';
  }
  if (action === 'launch') return 'browser_launch_failed';
  return 'browser_command_failed';
}
function safeErrorCode(error, action) {
  const classified = classifyNativeBrowserError(error, action);
  return SAFE_RUNNER_ERROR_CODES.has(classified) ? classified : 'browser_command_failed';
}
async function fail(error, action = 'protocol') {
  try { await writeFrame({ ok: false, error: safeErrorCode(error, action) }); } catch {}
}
function hash(value) { return crypto.createHash('sha256').update(String(value)).digest('hex'); }
function safeUrl(value) { try { return new URL(String(value)); } catch { return null; } }
function isLocalHost(host) { return host === '127.0.0.1' || host === 'localhost' || host === '[::1]' || host === '::1'; }
function isFinalLike(value) {
  let text = String(value || '');
  for (let i = 0; i < 3; i += 1) {
    try { const decoded = decodeURIComponent(text); if (decoded === text) break; text = decoded; } catch { break; }
  }
  const words = new Set((text.toLowerCase().match(/[a-z0-9]+/g) || []));
  for (const token of FINAL_LIKE_TOKENS) if (words.has(String(token).toLowerCase())) return true;
  return false;
}
function asciiSlug(value) { return /^[A-Za-z0-9_-]+$/.test(value) && value.length > 0; }
function canonicalUuid(value) {
  if (typeof value !== 'string' || !/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/.test(value)) return false;
  return value === value.toLowerCase();
}
function positiveJobId(value) {
  try { return /^[0-9]+$/.test(value) && BigInt(value) > 0n && BigInt(value) <= 9007199254740991n; } catch { return false; }
}
function validateInitialUrl(value) {
  const parsed = safeUrl(value);
  if (!parsed || parsed.protocol !== 'https:' || parsed.username || parsed.password || parsed.hash || (parsed.port && parsed.port !== '443')) throw new Error('invalid_application_url');
  const host = parsed.hostname.toLowerCase().replace(/\.$/, '');
  const params = [...parsed.searchParams.keys()];
  if (new Set(params).size !== params.length) throw new Error('invalid_application_url');
  const canonical = () => {
    const out = new URL(parsed.href);
    out.hostname = host;
    out.searchParams.delete('gh_src');
    out.hash = '';
    return out.href;
  };
  if (selectedPolicyName === 'lever') {
    if (!ALLOWED_ATS_HOSTS.has(host) || parsed.search || /[%\\?]/.test(String(value))) throw new Error('invalid_application_url');
    const parts = parsed.pathname.split('/');
    if (parts.length !== 3 && parts.length !== 4) throw new Error('invalid_application_url');
    if (parts.length === 4 && parts[3] !== 'apply') throw new Error('invalid_application_url');
    if (!asciiSlug(parts[1]) || !canonicalUuid(parts[2]) || isFinalLike(parsed.pathname)) throw new Error('invalid_application_url');
    return { mode: parts.length === 4 ? 'lever_apply' : 'lever_job', host, path: parsed.pathname, url: `https://${host}${parsed.pathname}` };
  }
  if (host === 'grnh.se') {
    if (parsed.search || parsed.pathname.split('/').filter(Boolean).length !== 1 || !asciiSlug(parsed.pathname.slice(1)) || isFinalLike(parsed.pathname)) throw new Error('invalid_application_url');
    return { mode: 'greenhouse_short', host, path: parsed.pathname, url: canonical() };
  }
  if (!ALLOWED_ATS_HOSTS.has(host)) throw new Error('unsupported_ats');
  for (const key of params) if (!['gh_src', 'for', 'token'].includes(key)) throw new Error('invalid_application_url');
  if (isFinalLike(parsed.pathname) || [...parsed.searchParams.values()].some(isFinalLike)) throw new Error('final_like_route');
  const parts = parsed.pathname.split('/');
  if (parts.length === 4 && parts[0] === '' && parts[2] === 'jobs' && asciiSlug(parts[1]) && positiveJobId(parts[3])) {
    if (params.some(key => key !== 'gh_src')) throw new Error('invalid_application_url');
    return { mode: 'greenhouse_job', host, path: parsed.pathname, url: canonical() };
  }
  if (host === 'boards.greenhouse.io' && parsed.pathname === '/embed/job_app') {
    const slug = parsed.searchParams.get('for') || '';
    const token = parsed.searchParams.get('token') || '';
    if (!asciiSlug(slug) || !positiveJobId(token)) throw new Error('invalid_application_url');
    return { mode: 'greenhouse_embed', host, path: parsed.pathname, url: canonical() };
  }
  throw new Error('invalid_application_url');
}
function documentRouteKey(route) {
  const parsed = safeUrl(route.url);
  if (!parsed) throw new Error('invalid_application_url');
  if (route.mode === 'greenhouse_short') return { mode: route.mode, slug: parsed.pathname.slice(1) };
  if (route.mode === 'lever_job' || route.mode === 'lever_apply') {
    const parts = parsed.pathname.split('/');
    return { mode: 'lever_job', host: parsed.hostname.toLowerCase(), account: parts[1], job: parts[2] };
  }
  if (route.mode === 'greenhouse_job') {
    const parts = parsed.pathname.split('/');
    return { mode: route.mode, host: parsed.hostname.toLowerCase(), board: parts[1], job: parts[3] };
  }
  return {
    mode: route.mode,
    host: parsed.hostname.toLowerCase(),
    board: parsed.searchParams.get('for') || '',
    token: parsed.searchParams.get('token') || '',
  };
}
function sameDocumentRoute(left, right) {
  if (!left || !right || left.mode !== right.mode) return false;
  if (left.mode === 'greenhouse_job') return left.host === right.host && left.board === right.board && left.job === right.job;
  if (left.mode === 'lever_job') return left.host === right.host && left.account === right.account && left.job === right.job;
  if (left.mode === 'greenhouse_embed') return left.host === right.host && left.board === right.board && left.token === right.token;
  return left.mode === 'greenhouse_short' && left.slug === right.slug;
}
function validateDocumentNavigation(value, { redirectCount = 0 } = {}) {
  if (redirectCount > MAX_REDIRECTS) throw new Error('redirect_limit_exceeded');
  const route = validateInitialUrl(value);
  const candidate = documentRouteKey(route);
  documentRedirectCount = Math.max(documentRedirectCount, redirectCount);
  if (!documentRouteIdentity) {
    documentRouteIdentity = candidate;
    return route;
  }
  if (sameDocumentRoute(documentRouteIdentity, candidate)) return route;
  // A short URL has no board/job identity until its first hosted redirect.
  if (documentRouteIdentity.mode === 'greenhouse_short' && route.mode === 'greenhouse_job' && redirectCount > 0) {
    documentRouteIdentity = candidate;
    return route;
  }
  throw new Error('unsafe_navigation_target');
}
function canonicalStaticPath(value) {
  let text = String(value || '');
  let settled = false;
  for (let index = 0; index < 3; index += 1) {
    if (/%(?![0-9A-Fa-f]{2})/.test(text)) throw new Error('invalid_path_encoding');
    let decoded;
    try { decoded = decodeURIComponent(text); } catch { throw new Error('invalid_path_encoding'); }
    if (decoded === text) { settled = true; break; }
    text = decoded;
  }
  if (!settled && /%[0-9A-Fa-f]{2}/.test(text)) throw new Error('invalid_path_encoding');
  if (text.includes('\\') || text.split('/').some(segment => segment === '.' || segment === '..')) {
    throw new Error('unsafe_path');
  }
  return text;
}

function rawPathFromUrl(value) {
  const text = String(value || '');
  const scheme = text.indexOf('://');
  const authorityEnd = scheme >= 0 ? text.indexOf('/', scheme + 3) : -1;
  if (authorityEnd < 0) return '/';
  const query = text.indexOf('?', authorityEnd);
  const fragment = text.indexOf('#', authorityEnd);
  const end = [query, fragment].filter(index => index >= 0).sort((left, right) => left - right)[0];
  return text.slice(authorityEnd, end === undefined ? text.length : end);
}

function validateRequestUrl(value, { initial = false, local = false, staticRequest = false } = {}) {
  const parsed = safeUrl(value);
  if (!parsed || parsed.username || parsed.password || parsed.hash) return false;
  if (local) return (parsed.protocol === 'http:' || parsed.protocol === 'https:') && isLocalHost(parsed.hostname);
  if (parsed.protocol !== 'https:' || (parsed.port && parsed.port !== '443')) return false;
  const host = parsed.hostname.toLowerCase();
  if (!ALLOWED_ATS_HOSTS.has(host) && host !== 'grnh.se' && !STATIC_HOSTS.has(host)) return false;
  let path = parsed.pathname;
  if (staticRequest) {
    try { path = canonicalStaticPath(rawPathFromUrl(value)); } catch { return false; }
  }
  if (Buffer.byteLength(path) > MAX_PATH_BYTES || Buffer.byteLength(parsed.search) > MAX_QUERY_BYTES) return false;
  if (isFinalLike(path) || [...parsed.searchParams.values()].some(isFinalLike)) return false;
  if (initial) { try { validateInitialUrl(parsed.href); return true; } catch { return false; } }
  if (staticRequest) {
    if (!STATIC_HOSTS.has(host)) return false;
    const prefixes = STATIC_PREFIXES[host] || [];
    return prefixes.some(prefix => path.startsWith(prefix));
  }
  return ALLOWED_ATS_HOSTS.has(host);
}
function validateOtherRequest(value) {
  if (validateRequestUrl(value, { staticRequest: true })) return true;
  const route = routeIdentityForUrl(value);
  return Boolean(route && documentRouteIdentity && sameDocumentRoute(route, documentRouteIdentity));
}
function routeIdentityForUrl(value) {
  try { return documentRouteKey(validateInitialUrl(value)); } catch { return null; }
}
function sameBoardJobUrl(value, identity, { confirmation = false } = {}) {
  const parsed = safeUrl(value);
  if (!parsed || !identity || parsed.username || parsed.password || parsed.hash) return false;
  if ((isFinalLike(parsed.pathname) && !(confirmation && /\/confirmation$/.test(parsed.pathname)))
      || [...parsed.searchParams.values()].some(isFinalLike)) return false;
  if (identity.mode === 'lever_job') {
    if (!LEVER_HOSTS.has(parsed.hostname.toLowerCase()) || parsed.search || /[%\\?]/.test(String(value))) return false;
    const parts = parsed.pathname.split('/');
    const expected = 4;
    if (parts.length !== expected || parts[0] !== '' || parts[1] !== identity.account || parts[2] !== identity.job
        || parsed.hostname.toLowerCase() !== identity.host || !canonicalUuid(parts[2])) return false;
    if (confirmation ? parts[3] !== 'confirmation' : parts[3] !== 'apply') return false;
    return true;
  }
  if (!ALLOWED_ATS_HOSTS.has(parsed.hostname.toLowerCase())) return false;
  if (identity.mode === 'greenhouse_job') {
    const parts = parsed.pathname.split('/');
    const expected = confirmation ? 5 : 4;
    if (parts.length !== expected || parts[0] !== '' || parts[2] !== 'jobs' || parts[1] !== identity.board || parts[3] !== identity.job
        || parsed.hostname.toLowerCase() !== identity.host) return false;
    if (confirmation && parts[4] !== 'confirmation') return false;
    return [...parsed.searchParams.keys()].every(key => key === 'gh_src');
  }
  if (identity.mode === 'greenhouse_embed') {
    if (parsed.hostname !== 'boards.greenhouse.io' || parsed.pathname !== '/embed/job_app') return false;
    return parsed.searchParams.get('for') === identity.board && parsed.searchParams.get('token') === identity.token
      && [...parsed.searchParams.keys()].every(key => ['for', 'token', 'gh_src'].includes(key));
  }
  return false;
}
function validateFormAction(value, method, frameUrl) {
  if (!value) return true;
  const parsed = safeUrl(value);
  if (!parsed || !['GET', 'POST'].includes(String(method || 'GET').toUpperCase())) return false;
  const identity = documentRouteIdentity || routeIdentityForUrl(frameUrl);
  return sameBoardJobUrl(value, identity) || sameBoardJobUrl(value, identity, { confirmation: true });
}
function proxyPermitKey(url, method) { return `${String(method || 'GET').toUpperCase()} ${String(url)}`; }
function authorizeProxyRequest(url, method, { ttl = 1000 } = {}) {
  const key = proxyPermitKey(url, method);
  proxyPermitUrls.set(key, Date.now() + ttl);
  return key;
}
function consumeProxyAuthorization(url, method) {
  const key = proxyPermitKey(url, method);
  const expires = proxyPermitUrls.get(key);
  if (!expires || expires < Date.now()) { proxyPermitUrls.delete(key); return false; }
  proxyPermitUrls.delete(key);
  return true;
}

function chromiumExecutable() {
  const configured = process.env.JOBS_ASSISTANT_CHROMIUM_EXECUTABLE;
  return path.resolve(String(configured || puppeteer.executablePath()));
}

async function preflight() {
  const packageJson = require.resolve('puppeteer/package.json', { paths: PACKAGE_SEARCH_PATHS });
  const packageVersion = JSON.parse(fs.readFileSync(packageJson, 'utf8')).version;
  const version = packageVersion;
  if (version !== '24.43.1') throw new Error('puppeteer_version_mismatch');
  const executablePath = chromiumExecutable();
  if (!path.isAbsolute(executablePath) || !fs.existsSync(executablePath) || !fs.statSync(executablePath).isFile()) throw new Error('chromium_executable_missing');
  return { node: process.version, puppeteer: version, executablePathBasename: path.basename(executablePath), executablePathAbsolute: executablePath, policy_version: SAFETY_POLICY.version || null, policy_sha256: POLICY_HASH };
}

function confinedPath(root, candidate) {
  if (!root || !candidate || path.basename(candidate) !== candidate) throw new Error('confined_path_required');
  const rootReal = fs.realpathSync(root);
  const candidateAbs = path.join(rootReal, candidate);
  const candidateStat = fs.lstatSync(candidateAbs);
  if (candidateStat.isSymbolicLink() || !candidateStat.isFile()) throw new Error('confined_path');
  return candidateAbs;
}

function acceptsStagedInput(tokens, filename, mediaType) {
  if (!Array.isArray(tokens) || tokens.length === 0) return true;
  const lowerName = filename.toLowerCase();
  return tokens.some(raw => {
    const token = String(raw).trim().toLowerCase();
    if (token === '*/*' || token === mediaType) return true;
    if (token.endsWith('/*') && mediaType.startsWith(token.slice(0, -1))) return true;
    return token.startsWith('.') && lowerName.endsWith(token);
  });
}

function ipv4ToBigInt(value) {
  if (typeof value !== 'string' || net.isIP(value) !== 4) return null;
  const parts = value.split('.');
  if (parts.length !== 4 || parts.some(part => !/^(?:0|[1-9]\d*)$/.test(part) || Number(part) > 255)) return null;
  return parts.reduce((result, part) => (result << 8n) | BigInt(Number(part)), 0n);
}

function ipv6ToBigInt(value) {
  if (typeof value !== 'string') return null;
  let text = value.toLowerCase();
  if (text.startsWith('[') && text.endsWith(']')) text = text.slice(1, -1);
  if (text.includes('%') || net.isIP(text) !== 6) return null;
  const pieces = text.split('::');
  if (pieces.length > 2) return null;
  const left = pieces[0] ? pieces[0].split(':') : [];
  const right = pieces.length === 2 && pieces[1] ? pieces[1].split(':') : [];
  const groups = [...left, ...right];
  const dotted = groups.findIndex(part => part.includes('.'));
  if (dotted >= 0) {
    if (dotted !== groups.length - 1) return null;
    const ipv4 = ipv4ToBigInt(groups[dotted]);
    if (ipv4 === null) return null;
    groups.splice(dotted, 1, Number((ipv4 >> 16n) & 0xffffn).toString(16), Number(ipv4 & 0xffffn).toString(16));
  }
  if (groups.some(part => !/^[0-9a-f]{1,4}$/.test(part))) return null;
  const missing = 8 - groups.length;
  if (missing < 0 || (pieces.length === 1 && missing !== 0)) return null;
  const expanded = [...groups.slice(0, left.length), ...Array(missing).fill('0'), ...groups.slice(left.length)];
  return expanded.reduce((result, part) => (result << 16n) | BigInt(parseInt(part, 16)), 0n);
}

function cidr4(value, networkValue, prefix) {
  const address = ipv4ToBigInt(value);
  if (address === null) return false;
  const network = BigInt(networkValue);
  const mask = ((1n << BigInt(prefix)) - 1n) << BigInt(32 - prefix);
  return (address & mask) === (network & mask);
}

function cidr6(value, networkText, prefix) {
  const address = ipv6ToBigInt(value);
  const network = ipv6ToBigInt(networkText);
  if (address === null || network === null) return false;
  const mask = ((1n << BigInt(prefix)) - 1n) << BigInt(128 - prefix);
  return (address & mask) === (network & mask);
}

// This is deliberately stricter than "not private".  Only an IANA global
// unicast address may become a dial target; all special-use allocations fail
// closed even when a platform library calls them globally routable.
function isGlobalUnicastAddress(value) {
  if (typeof value !== 'string' || net.isIP(value) === 0) return false;
  if (net.isIP(value) === 4) {
    const special = [
      [0x00000000, 8], [0x0a000000, 8], [0x64400000, 10],
      [0x7f000000, 8], [0xa9fe0000, 16], [0xac100000, 12],
    ];
    if (special.some(([network, prefix]) => cidr4(value, network, prefix))) return false;
    return ![
      [0xc0000000, 24], [0xc0000200, 24], [0xc01fc400, 24],
      [0xc034c100, 24], [0xc0586300, 24], [0xc6120000, 15],
      [0xc6336400, 24], [0xcb007100, 24], [0xc0a80000, 16],
      [0xc0af3000, 24],
    ].some(([network, prefix]) => cidr4(value, network, prefix))
      && !cidr4(value, 0xe0000000, 3);
  }
  // IPv4-mapped IPv6 has an IPv4 escape hatch and is never a valid IPv6
  // transport address (including ::ffff:127.0.0.1).
  if (cidr6(value, '::ffff:0:0', 96)) return false;
  if (!cidr6(value, '2000::', 3)) return false;
  return ![
    ['::', 128], ['::1', 128], ['100::', 64], ['2001::', 23],
    ['2001:db8::', 32], ['3fff::', 20], ['2002::', 16],
    ['fc00::', 7], ['fe80::', 10], ['ff00::', 8],
  ].some(([network, prefix]) => cidr6(value, network, prefix));
}

function classifyResolverResult(addresses) {
  if (!Array.isArray(addresses) || addresses.length === 0) throw new Error('resolver_address_count');
  const validated = addresses.map(result => {
    if (!result || typeof result !== 'object' || Array.isArray(result) || typeof result.address !== 'string' || typeof result.family !== 'number' || !Number.isInteger(result.family)) {
      throw new Error('resolver_address_rejected');
    }
    const address = result.address;
    const family = result.family;
    const parsedFamily = net.isIP(address);
    if ((parsedFamily !== 4 && parsedFamily !== 6) || (family !== 4 && family !== 6)
      || family !== parsedFamily || !isGlobalUnicastAddress(address)) throw new Error('resolver_address_rejected');
    return { address, family };
  });
  return validated[0];
}

async function resolvePinnedAddress(hostname) {
  const host = String(hostname || '').toLowerCase().replace(/\.$/, '');
  if (!host || net.isIP(host)) throw new Error('resolver_hostname_required');
  networkCounters.dnsLookups += 1;
  const delayMs = Number(process.env.JOBS_ASSISTANT_TEST_RESOLVER_DELAY_MS || 0);
  if (Number.isFinite(delayMs) && delayMs > 0) await new Promise(resolve => setTimeout(resolve, Math.min(delayMs, 10_000)));
  let addresses;
  const injected = process.env.JOBS_ASSISTANT_TEST_RESOLVER_JSON;
  if (injected) {
    let mapping;
    try { mapping = JSON.parse(injected); } catch { throw new Error('resolver_injection_invalid'); }
    if (!mapping || !Object.prototype.hasOwnProperty.call(mapping, host)) throw new Error('resolver_injection_missing');
    addresses = mapping[host];
  } else {
    addresses = await dns.promises.lookup(host, { all: true, verbatim: true });
  }
  return classifyResolverResult(addresses);
}

function currentConnectAuthorization(authority, host, localTunnel, localParsed, parsed) {
  const normalizedAuthority = String(authority || '').toLowerCase();
  const logicalParsed = logicalInitialUrl ? safeUrl(logicalInitialUrl) : null;
  const logicalAuthority = logicalParsed ? `${logicalParsed.hostname}:443` : '';
  const permitUrl = reviewPermit && reviewPermit.expires > Date.now() ? safeUrl(reviewPermit.url) : null;
  const permitAuthority = permitUrl ? `${permitUrl.hostname}:${permitUrl.port || 443}` : null;
  const epoch = reviewEpoch && reviewEpoch.expires > Date.now() && sameDocumentRoute(reviewEpoch.routeIdentity, documentRouteIdentity)
    ? reviewEpoch : null;
  const epochExpiresAt = epoch ? epoch.expires : null;
  const reviewExpiry = permitAuthority && normalizedAuthority === permitAuthority.toLowerCase()
    ? reviewPermit.expires : epochExpiresAt;
  const initialAllowed = !proxyFrozen && !firstApplicantMutation && !terminalReason && (
    localTunnel
    || normalizedAuthority === logicalAuthority.toLowerCase()
    || STATIC_HOSTS.has(host)
  );
  const reviewAllowed = reviewState === 'open_guarded' && !terminalReason && (
    (permitAuthority && normalizedAuthority === permitAuthority.toLowerCase())
    || (epoch && STATIC_HOSTS.has(host))
  );
  const shapeAllowed = Boolean(parsed && parsed.pathname === '/' && !parsed.search && !parsed.hash && !parsed.username
    && !parsed.password && (!parsed.port || parsed.port === '443')
    && (localTunnel || STATIC_HOSTS.has(host) || ALLOWED_ATS_HOSTS.has(host)));
  return {
    allowed: shapeAllowed && (initialAllowed || reviewAllowed),
    initialAllowed,
    reviewAllowed,
    reviewExpiry,
  };
}

function absoluteAuthority(parsed) {
  return parsed.hostname.includes(':') ? `[${parsed.hostname}]:${parsed.port || 443}` : `${parsed.hostname}:${parsed.port || 443}`;
}

async function startProxy() {
  proxy = http.createServer((req, res) => {
    networkCounters.proxyRequests += 1;
    const parsed = safeUrl(req.url);
    const local = parsed && isLocalHost(parsed.hostname);
    const method = String(req.method || 'GET').toUpperCase();
    const authorized = parsed && consumeProxyAuthorization(parsed.href, method);
    const preMutationAllowed = Boolean(parsed && (
      local
        ? internalTransportUrl && validateRequestUrl(parsed.href, { local: true })
        : validateRequestUrl(parsed.href, { initial: true }) || validateRequestUrl(parsed.href, { staticRequest: true })
    ));
    const permitAllowed = Boolean(authorized && reviewState === 'open_guarded' && !terminalReason);
    if ((!permitAllowed && (proxyFrozen || firstApplicantMutation || terminalReason))
        || (!permitAllowed && !preMutationAllowed)
        || (!permitAllowed && method !== 'GET' && method !== 'HEAD')) {
      if (firstApplicantMutation && !terminalReason && !permitAllowed) terminalReason = 'unsafe_network_attempt';
      res.writeHead(403); res.end(); return;
    }
    if (!permitAllowed && method !== 'GET' && method !== 'HEAD') { res.writeHead(403); res.end(); return; }
    const localBase = internalTransportUrl ? safeUrl(internalTransportUrl) : null;
    const transport = local && localBase ? localBase : parsed;
    const finish = async () => {
      try {
        let target;
        if (local && localBase) {
          const family = net.isIP(localBase.hostname);
          if (!family) throw new Error('local_transport_not_numeric');
          target = { address: localBase.hostname, family };
        } else {
          target = await resolvePinnedAddress(parsed.hostname);
        }
        const requestAuthority = absoluteAuthority(parsed);
        const refreshedAuthorization = local && localBase
          ? { allowed: Boolean(internalTransportUrl && !proxyFrozen && !firstApplicantMutation && !terminalReason), reviewAllowed: false, reviewExpiry: null }
          : currentConnectAuthorization(requestAuthority, parsed.hostname.toLowerCase(), false, null, parsed);
        const routeIdentity = !local ? routeIdentityForUrl(parsed.href) : null;
        const routeUnchanged = local
          ? true
          : (routeIdentity && documentRouteIdentity
            ? sameDocumentRoute(routeIdentity, documentRouteIdentity)
            : STATIC_HOSTS.has(parsed.hostname.toLowerCase()));
        const permitUnexpired = !authorized || (
          reviewState === 'open_guarded'
          && refreshedAuthorization.reviewAllowed
          && refreshedAuthorization.reviewExpiry
          && refreshedAuthorization.reviewExpiry > Date.now()
        );
        if (req.destroyed || res.destroyed || !refreshedAuthorization.allowed || !routeUnchanged || !permitUnexpired) {
          if (firstApplicantMutation && !terminalReason) terminalReason = 'unsafe_network_attempt';
          throw new Error('proxy_authorization_revoked');
        }
        const tls = transport.protocol === 'https:';
        const transportProtocol = tls ? require('node:https') : http;
        const chunks = [];
        let requestBytes = 0;
        req.on('data', chunk => {
          requestBytes += chunk.length;
          if (requestBytes > 256 * 1024) req.destroy(new Error('request_body_too_large'));
          else chunks.push(chunk);
        });
        await new Promise(resolve => req.once('end', resolve));
        const latestAuthorization = local && localBase
          ? { allowed: Boolean(internalTransportUrl && !proxyFrozen && !firstApplicantMutation && !terminalReason), reviewAllowed: false, reviewExpiry: null }
          : currentConnectAuthorization(requestAuthority, parsed.hostname.toLowerCase(), false, null, parsed);
        const latestRoute = !local ? routeIdentityForUrl(parsed.href) : null;
        const latestRouteUnchanged = local
          ? true
          : (latestRoute && documentRouteIdentity
            ? sameDocumentRoute(latestRoute, documentRouteIdentity)
            : STATIC_HOSTS.has(parsed.hostname.toLowerCase()));
        const latestPermit = !authorized || (
          reviewState === 'open_guarded'
          && latestAuthorization.reviewAllowed
          && latestAuthorization.reviewExpiry
          && latestAuthorization.reviewExpiry > Date.now()
        );
        if (req.destroyed || res.destroyed || !latestAuthorization.allowed || !latestRouteUnchanged || !latestPermit) {
          if (firstApplicantMutation && !terminalReason) terminalReason = 'unsafe_network_attempt';
          throw new Error('proxy_authorization_revoked');
        }
        const options = {
          host: target.address,
          family: target.family,
          port: transport.port || (tls ? 443 : 80),
          path: local && localBase ? `${localBase.pathname}${localBase.search}` : `${transport.pathname}${transport.search}`,
          method,
          headers: { accept: req.headers.accept || '*', host: transport.hostname },
        };
        if (tls) { options.servername = transport.hostname; options.rejectUnauthorized = true; }
        networkCounters.upstreamHttpAttempts += 1;
        const request = transportProtocol.request(options, upstream => {
          const headers = { ...upstream.headers };
          delete headers['content-length']; delete headers['transfer-encoding']; delete headers.connection;
          res.writeHead(upstream.statusCode || 502, headers);
          let transferred = 0;
          upstream.on('data', chunk => {
            transferred += chunk.length;
            if (transferred > MAX_STATIC_BYTES || staticBytes + chunk.length > MAX_STATIC_BYTES) {
              networkCounters.responseBytesRejected += 1;
              if (!terminalReason) terminalReason = 'observation_too_large';
              upstream.destroy(); res.destroy(); return;
            }
            staticBytes += chunk.length;
            res.write(chunk);
          });
          upstream.on('end', () => { if (!res.destroyed) res.end(); });
          upstream.on('error', () => { if (!res.destroyed) res.destroy(); });
        });
        request.on('error', () => { if (!res.headersSent) res.writeHead(502); if (!res.destroyed) res.end(); });
        for (const chunk of chunks) request.write(chunk);
        request.end();
      } catch {
        if (!res.headersSent) res.writeHead(403);
        if (!res.destroyed) res.end();
      }
    };
    void finish();
  });
  proxy.on('connect', (req, client, head) => {
    const authority = String(req.url || '');
    const parsed = safeUrl(`https://${authority}`);
    const host = parsed?.hostname?.toLowerCase().replace(/^\[|\]$/g, '') || '';
    if (/attacker|exfil/i.test(host)) { networkCounters.attackerDnsLookups += 1; client.destroy(); return; }
    const localParsed = internalTransportUrl ? safeUrl(internalTransportUrl) : null;
    const logicalParsed = logicalInitialUrl ? safeUrl(logicalInitialUrl) : null;
    const localAuthority = localParsed ? `${localParsed.hostname}:${localParsed.port || (localParsed.protocol === 'https:' ? 443 : 80)}` : '';
    const logicalAuthority = logicalParsed ? `${logicalParsed.hostname}:443` : '';
    const normalizedAuthority = authority.toLowerCase();
    const localTunnel = Boolean(localParsed && localParsed.protocol === 'https:'
      && (normalizedAuthority === localAuthority.toLowerCase() || normalizedAuthority === logicalAuthority.toLowerCase()));
    const initialAuthorization = currentConnectAuthorization(authority, host, localTunnel, localParsed, parsed);
    if (client.destroyed || !initialAuthorization.allowed) {
      if (firstApplicantMutation && !terminalReason && !initialAuthorization.reviewAllowed) terminalReason = 'unsafe_network_attempt';
      client.destroy(); return;
    }
    const destinationPort = localTunnel ? Number(localParsed.port || 443) : 443;
    const connect = target => {
      const authorization = currentConnectAuthorization(authority, host, localTunnel, localParsed, parsed);
      if (client.destroyed || !authorization.allowed
          || (authorization.reviewAllowed && (!authorization.reviewExpiry || authorization.reviewExpiry <= Date.now()))) {
        client.destroy(); return;
      }
      networkCounters.upstreamConnectAttempts += 1;
      const socket = net.connect({ port: destinationPort, host: target.address, family: target.family }, () => {
        const connectedAuthorization = currentConnectAuthorization(authority, host, localTunnel, localParsed, parsed);
        if (client.destroyed || !connectedAuthorization.allowed
            || (connectedAuthorization.reviewAllowed
              && (!connectedAuthorization.reviewExpiry || connectedAuthorization.reviewExpiry <= Date.now()))) {
          socket.destroy(); client.destroy(); return;
        }
        client.write('HTTP/1.1 200 Connection Established\r\n\r\n');
        let transferred = head.length;
        const relay = (source, destination) => source.on('data', chunk => {
          const relayAuthorization = currentConnectAuthorization(authority, host, localTunnel, localParsed, parsed);
          if (relayAuthorization.reviewAllowed
              && (!relayAuthorization.reviewExpiry || relayAuthorization.reviewExpiry <= Date.now())) {
            source.destroy(); destination.destroy(); return;
          }
          transferred += chunk.length;
          if (transferred > MAX_STATIC_BYTES || staticBytes + chunk.length > MAX_STATIC_BYTES) {
            networkCounters.responseBytesRejected += 1;
            if (!terminalReason) terminalReason = 'observation_too_large';
            source.destroy(); destination.destroy(); return;
          }
          staticBytes += chunk.length; destination.write(chunk);
        });
        if (head.length) socket.write(head);
        relay(socket, client); relay(client, socket);
      });
      trackProxyTunnel(socket, authorization.reviewExpiry);
      trackProxyTunnel(client, authorization.reviewExpiry);
      socket.on('error', () => client.destroy());
    };
    void (async () => {
      try {
        let target;
        if (localTunnel) {
          const family = net.isIP(localParsed.hostname);
          if (!family) throw new Error('local_transport_not_numeric');
          target = { address: localParsed.hostname, family };
        } else target = await resolvePinnedAddress(host);
        // DNS is asynchronous.  Every state and capability check must be
        // repeated after it settles; the pre-await decision is not a permit.
        if (client.destroyed) { client.destroy(); return; }
        const authorization = currentConnectAuthorization(authority, host, localTunnel, localParsed, parsed);
        if (!authorization.allowed
            || (authorization.reviewAllowed && (!authorization.reviewExpiry || authorization.reviewExpiry <= Date.now()))) {
          client.destroy(); return;
        }
        connect(target);
      } catch { client.destroy(); }
    })();
  });
  await new Promise((resolve, reject) => { proxy.once('error', reject); proxy.listen(0, '127.0.0.1', resolve); });
  return proxy.address().port;
}
function measureTree(root, { allowSymlinks = false } = {}) {
  if (!root) return { bytes: 0, files: 0 };
  const start = path.resolve(root);
  const stack = [start];
  let bytes = 0; let files = 0;
  while (stack.length) {
    const current = stack.pop();
    let stat;
    try { stat = fs.lstatSync(current); } catch { throw new Error('artifact_error'); }
    if (stat.isSymbolicLink()) {
      if (!allowSymlinks) throw new Error('artifact_error');
      let linkText;
      try { linkText = fs.readlinkSync(current); } catch { throw new Error('artifact_error'); }
      files += 1; bytes += Buffer.byteLength(linkText);
      if (files > 6000 || bytes > 192 * 1024 * 1024) return { bytes, files };
      continue;
    }
    if (stat.isDirectory()) {
      let names;
      try { names = fs.readdirSync(current); } catch { throw new Error('artifact_error'); }
      for (const name of names) stack.push(path.join(current, name));
    } else if (stat.isFile()) {
      files += 1; bytes += stat.size;
      if (files > 6000 || bytes > 192 * 1024 * 1024) return { bytes, files };
    }
  }
  return { bytes, files };
}
function enforceBudgets() {
  const run = measureTree(process.cwd());
  const profile = measureTree(ownerRoot, { allowSymlinks: true });
  const inputRoot = process.env.JOBS_ASSISTANT_INPUT_ROOT;
  const input = inputRoot ? measureTree(inputRoot) : { bytes: 0, files: 0 };
  if (input.bytes > 10 * 1024 * 1024 || input.files > 1
      || run.bytes > 50 * 1024 * 1024 || run.files > 500
      || profile.bytes > 128 * 1024 * 1024 || profile.files > 5000
      || run.bytes + profile.bytes > 192 * 1024 * 1024 || run.files + profile.files > 6000) {
    throw new Error('artifact_error');
  }
}

function requestTerminalCleanup(trigger, exitCode, reason = null) {
  detachedCloseRequested = true;
  cleanupTrigger = cleanupTrigger || trigger;
  if (reason && !terminalReason) terminalReason = reason;
  void close(trigger).then(() => process.exit(exitCode)).catch(() => process.exit(exitCode));
}
function installBudgetWatcher() {
  clearInterval(budgetTimer);
  budgetTimer = setInterval(() => {
    try { enforceBudgets(); }
    catch (error) {
      if (!terminalReason) terminalReason = error?.message || 'artifact_error';
      clearInterval(budgetTimer); budgetTimer = null;
      requestTerminalCleanup('budget_exceeded', 1);
    }
  }, 1000);
  budgetTimer.unref?.();
}
async function launch(command = {}) {
  if (browser) return;
  launchHeadless = command.headless !== false;
  ownerRoot = fs.mkdtempSync(path.join(os.tmpdir(), 'jobs-assistant-owner-'));
  userDataDir = path.join(ownerRoot, 'browser-profile');
  fs.mkdirSync(userDataDir, { mode: 0o700 });
  const proxyPort = await startProxy();
  const executablePath = chromiumExecutable();
  if (!fs.existsSync(executablePath)) throw new Error('chromium_executable_missing');
  const launchOptions = {
    executablePath,
    headless: launchHeadless,
    pipe: true,
    dumpio: false,
    userDataDir,
    handleSIGINT: false,
    handleSIGTERM: false,
    handleSIGHUP: false,
    args: [
      `--proxy-server=http://127.0.0.1:${proxyPort}`,
      '--proxy-bypass-list=<-loopback>',
      '--disable-background-networking', '--disable-background-timer-throttling', '--disable-breakpad',
      '--disable-client-side-phishing-detection', '--disable-default-apps', '--disable-dev-shm-usage',
      '--disable-quic', '--disable-async-dns', '--dns-prefetch-disable',
      '--force-webrtc-ip-handling-policy=disable_non_proxied_udp',
      '--disable-webrtc', '--disable-features=NetworkServiceInProcess,InterestFeedContentSuggestions,PreloadMediaEngagementData,SpeculationRulesPrefetchProxy,WebRtcAllowAllInterfaces,WebRtcHideLocalIpsWithMdns,PrefetchPrivacyChanges,AsyncDns,DnsOverHttps,UseDnsHttpsSvcbAlpn',
      '--disable-hang-monitor', '--disable-popup-blocking', '--disable-prompt-on-repost', '--disable-sync',
      '--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE localhost, EXCLUDE 127.0.0.1, EXCLUDE ::1',
      '--metrics-recording-only', '--no-first-run', '--password-store=basic', '--use-mock-keychain',
    ],
  };
  if (!launchHeadless) launchOptions.args.push('--window-position=100,100', '--window-size=1100,900');
  if (process.env.JOBS_ASSISTANT_CONTAINER_NO_SANDBOX === '1') launchOptions.args.push('--no-sandbox', '--disable-setuid-sandbox');
  try {
    browser = await puppeteer.launch(launchOptions);
  } catch (error) {
    const classified = classifyNativeBrowserError(error, 'launch');
    const normalized = new Error(classified);
    normalized.code = classified;
    throw normalized;
  }
  const browserProcess = browser?.process?.() || null;
  browserProcess?.once('exit', (code, signal) => {
    browserExit = { code: Number.isInteger(code) ? code : null, signal: signal || null };
    if (detachedOwner && !closeRequested) {
      try { manifestWrite('open_guarded', { detached: true, browser_exit: browserExit }); } catch {}
    }
  });
  if (!browserProcess?.pid) throw new Error('browser_process_missing');
  const pages = await browser.pages();
  page = pages[0] || await browser.newPage();
  if (!launchHeadless) await page.bringToFront();
  browser.on('targetcreated', target => {
    if (target.type() !== 'page') {
      requestTerminalCleanup('unexpected_target', 1, 'unexpected_target');
      return;
    }
    target.page().then(child => {
      if (!child || child === page) return;
      child.close().catch(() => {});
    }).catch(() => {});
  });
  browser.on('disconnected', () => {
    if (!closeRequested) requestTerminalCleanup('browser_disconnected', 1, 'browser_error');
  });
  page.once('close', () => {
    if (!closeRequested) requestTerminalCleanup('page_close', 0);
  });
  page.on('framenavigated', frame => {
    if (currentGeneration && reviewState !== 'open_guarded') invalidateGeneration('navigation_invalidated');
  });
  await installRequestGuards(page);
  installBudgetWatcher();
}

async function installRequestGuards(targetPage) {
  function fetchInternalDocument(logicalUrl, method = 'GET', body = null) {
    return new Promise((resolve, reject) => {
      const base = safeUrl(internalTransportUrl);
      if (!base || !net.isIP(base.hostname)) return reject(new Error('unsafe_navigation_target'));
      const transport = base.protocol === 'https:' ? require('node:https') : http;
      const options = {
        host: base.hostname, family: net.isIP(base.hostname),
        port: base.port || (base.protocol === 'https:' ? 443 : 80),
        path: `${base.pathname}${base.search}`, method,
      };
      if (typeof logicalUrl === 'string') {
        options.headers = {
          'x-jobs-assistant-logical-url': Buffer.from(logicalUrl, 'utf8').toString('base64url'),
        };
      }
      if (base.protocol === 'https:') { options.servername = base.hostname; options.rejectUnauthorized = true; }
      let settled = false; const chunks = []; let size = 0;
      const failResponse = error => { if (settled) return; settled = true; networkCounters.responseBytesRejected += 1; reject(error); };
      const request = transport.request(options, response => {
        const declared = Number(response.headers['content-length'] || 0);
        if (Number.isFinite(declared) && declared > MAX_STATIC_BYTES) { response.destroy(); failResponse(new Error('response_body_too_large')); return; }
        response.on('data', chunk => {
          size += chunk.length;
          if (size > MAX_STATIC_BYTES) { response.destroy(); failResponse(new Error('response_body_too_large')); return; }
          chunks.push(chunk);
        });
        response.on('error', failResponse);
        response.on('end', () => { if (!settled) { settled = true; resolve({ status: response.statusCode || 502, headers: response.headers, body: Buffer.concat(chunks) }); } });
      });
      request.on('error', failResponse);
      if (body) request.write(body);
      request.end();
    });
  }
  await targetPage.setRequestInterception(true);
  const trackedRequests = new WeakSet();
  const completedRequests = new WeakSet();
  const markStart = request => {
    if (trackedRequests.has(request)) return;
    trackedRequests.add(request);
    pendingNetwork += 1;
    lastNetworkActivity = Date.now();
    request.once?.('error', () => {});
  };
  const markComplete = request => {
    if (!trackedRequests.has(request) || completedRequests.has(request)) return;
    completedRequests.add(request);
    pendingNetwork = Math.max(0, pendingNetwork - 1);
    lastNetworkActivity = Date.now();
  };
  targetPage.on('request', async request => {
    markStart(request);
    const url = request.url(); const parsed = safeUrl(url);
    const host = parsed ? parsed.hostname.toLowerCase() : '';
    const local = isLocalHost(host); const method = String(request.method() || 'GET').toUpperCase();
    const attacker = /attacker|exfil/i.test(url); const finalLike = isFinalLike(url);
    if (finalLike) networkCounters.finalLikeDenied += 1;
    const type = request.resourceType();
    const navigation = type === 'document' && request.isNavigationRequest();
    if (navigation && reviewState !== 'open_guarded' && currentGeneration) invalidateGeneration('navigation_invalidated');
    const redirectChain = typeof request.redirectChain === 'function' ? request.redirectChain() : [];
    const redirectExceeded = redirectChain.length > MAX_REDIRECTS;
    if (redirectExceeded) networkCounters.redirectsDenied += 1;
    let documentAllowed = true; let documentError = null; let reviewAllowed = false;
    if (navigation && reviewState !== 'open_guarded') {
      try { validateDocumentNavigation(url, { redirectCount: redirectChain.length }); }
      catch (error) {
        documentAllowed = false;
        documentError = error?.message || 'unsafe_navigation_target';
        if (!terminalReason) terminalReason = documentError;
      }
    }
    if (reviewState === 'open_guarded' && !documentError) {
      reviewAllowed = await consumeRealmReviewPermit(request).catch(() => false);
      if (!reviewAllowed && reviewEpoch && reviewEpoch.expires > Date.now()) {
        if (navigation) {
          reviewAllowed = method === 'GET' && redirectChain.length > 0 && !redirectExceeded
            && sameBoardJobUrl(url, reviewEpoch.routeIdentity, { confirmation: /\/confirmation(?:[/?]|$)/.test(parsed?.pathname || '') });
          if (reviewAllowed) reviewEpoch.redirects = Math.max(reviewEpoch.redirects, redirectChain.length);
        } else {
          reviewAllowed = STATIC_TYPES.has(type) && !redirectExceeded
            && validateRequestUrl(url, { staticRequest: true }) && reviewEpoch.staticRequests < MAX_STATIC_REQUESTS;
        }
      }
    }
    if (navigation && reviewAllowed && currentGeneration) invalidateGeneration('navigation_invalidated');
    const initialLocal = Boolean(local && internalTransportUrl && validateRequestUrl(url, { local: true }));
    const productionAllowed = type === 'document'
      ? documentAllowed && validateRequestUrl(url, { initial: true })
      : STATIC_TYPES.has(type)
        ? !redirectExceeded && validateRequestUrl(url, { staticRequest: true })
        : type === 'other'
          ? !redirectExceeded && validateOtherRequest(url)
          : false;
    const transportAllowed = initialLocal && type !== 'other';
    let allowed = !attacker && !finalLike && !terminalReason && !documentError
      && (transportAllowed || productionAllowed)
      && (type === 'document' || STATIC_TYPES.has(type) || type === 'other');
    if (firstApplicantMutation) allowed = false;
    if (reviewState === 'open_guarded' && reviewAllowed) allowed = true;
    if (!allowed) {
      networkCounters.denied += 1;
      if (firstApplicantMutation && !terminalReason) {
        terminalReason = 'unsafe_network_attempt';
        request.abort('blockedbyclient').catch(() => {});
        setTimeout(() => { if (!cleanupStarted) void close(); }, 1000).unref?.();
      } else request.abort('blockedbyclient').catch(() => {});
      markComplete(request);
      return;
    }
    if (type !== 'document') {
      staticRequests += 1;
      if (staticRequests > MAX_STATIC_REQUESTS) {
        request.abort('blockedbyclient').catch(() => {});
        markComplete(request);
        return;
      }
      if (reviewEpoch) reviewEpoch.staticRequests = staticRequests;
    }
    if (reviewState === 'open_guarded' && reviewAllowed && parsed) authorizeProxyRequest(url, method);
    // The local fixture is a capability substitution, but it still passed all
    // the same strict state and route gates above.
    if (internalTransportUrl && safeUrl(internalTransportUrl)?.protocol === 'http:' && (initialLocal || productionAllowed)
        && method === 'GET' && (!firstApplicantMutation || reviewAllowed)) {
      try {
        const internal = await fetchInternalDocument(url);
        staticBytes += internal.body.length;
        if (staticBytes > MAX_STATIC_BYTES) throw new Error('response_body_too_large');
        const headers = { ...internal.headers };
        delete headers['content-encoding']; delete headers.connection; delete headers['content-length']; delete headers['transfer-encoding'];
        await request.respond({ status: internal.status, headers, body: internal.body });
        networkCounters.allowed += 1;
      } catch (error) {
        networkCounters.denied += 1;
        if (error?.message === 'response_body_too_large' && !terminalReason) terminalReason = 'observation_too_large';
        await request.abort('failed').catch(() => {});
      }
      markComplete(request);
      return;
    }
    networkCounters.allowed += 1;
    request.continue().catch(() => {});
  });
  targetPage.on('requestfinished', request => markComplete(request));
  targetPage.on('requestfailed', request => markComplete(request));
  targetPage.on('dialog', dialog => dialog.dismiss().catch(() => {}));
  targetPage.on('popup', popup => popup.close().catch(() => {}));
}
async function waitForInitialQuiet(timeoutMs = 10000) {
  if (!page) throw new Error('Puppeteer page is not initialized');
  const realm = page.mainFrame().isolatedRealm();
  await realm.evaluate(() => {
    const key = '__JOBS_ASSISTANT_QUIET_STATE__';
    if (!globalThis[key]) {
      const state = { lastMutation: Date.now() };
      const observer = new MutationObserver(() => { state.lastMutation = Date.now(); });
      observer.observe(document, { subtree: true, childList: true, attributes: true, characterData: true });
      globalThis[key] = { state, observer };
    }
  });
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    const mutationAt = await realm.evaluate(() => globalThis.__JOBS_ASSISTANT_QUIET_STATE__?.state?.lastMutation || Date.now()).catch(() => Date.now());
    if (pendingNetwork === 0 && Date.now() - Math.max(lastNetworkActivity, mutationAt) >= 500) return;
    await new Promise(resolve => setTimeout(resolve, 50));
  }
  throw new Error('page_not_stable');
}
function observationSemanticKey(value) {
  const copy = JSON.parse(JSON.stringify(value));
  delete copy.observation_id; delete copy.counters; delete copy.terminal_reason;
  for (const field of copy.fields || []) { delete field.target_id; delete field.value; }
  for (const button of copy.buttons || []) delete button.target_id;
  copy.final_submit_target_ids = (copy.final_submit_target_ids || []).map(() => 'final');
  return JSON.stringify(copy);
}

async function goto(command) {
  assertPage();
  if (command.ats_policy !== undefined && command.ats_policy !== selectedPolicyName) throw new Error('ats_policy_mismatch');
  const route = validateInitialUrl(command.url);
  logicalInitialUrl = route.url;
  documentRouteIdentity = documentRouteKey(route);
  documentRedirectCount = 0;
  const candidate = command.internal_url;
  if (candidate !== undefined) {
    const expectedToken = process.env.JOBS_ASSISTANT_INTERNAL_TRANSPORT_TOKEN;
    if (!expectedToken || command.internal_token !== expectedToken || !candidate || !validateRequestUrl(candidate, { local: true })) throw new Error('unsafe_navigation_target');
    internalTransportUrl = candidate;
  } else if (internalTransportUrl) {
    throw new Error('unsafe_navigation_target');
  }
  let response;
  try {
    response = await page.goto(logicalInitialUrl, { waitUntil: 'domcontentloaded', timeout: command.timeoutMs || 10000 });
  } catch (error) {
    if (terminalReason) throw new Error(terminalReason);
    const classified = classifyNativeBrowserError(error, 'goto');
    const normalized = new Error(classified);
    normalized.code = classified;
    throw normalized;
  }
  await waitForInitialQuiet(command.quietTimeoutMs || 10000);
  const currentUrl = page.url();
  const firstObservation = await observe();
  await new Promise(resolve => setTimeout(resolve, 250));
  const secondObservation = await observe();
  if (observationSemanticKey(firstObservation) !== observationSemanticKey(secondObservation)) throw new Error('page_not_stable');
  freezeProxy();
  initialQuietReady = true;
  return { url: currentUrl, title: await page.title(), status: response ? response.status() : null, mode: route.mode };
}
function normalizeDescriptor(value) {
  return String(value ?? '')
    .replace(/([a-z])([A-Z])/g, '$1 $2')
    .replace(/([A-Za-z])([0-9])/g, '$1 $2')
    .replace(/([0-9])([A-Za-z])/g, '$1 $2')
    .replace(/[^A-Za-z0-9]+/g, ' ')
    .trim().toLowerCase().replace(/\s+/g, ' ');
}
function descriptorSafe(value) {
  const text = String(value ?? '');
  for (const char of text) {
    const code = char.codePointAt(0);
    if (!((code === 9 || code === 10 || code === 13) || (code >= 32 && code <= 126))) return false;
  }
  const normalized = normalizeDescriptor(text);
  if (!normalized) return true;
  const tokens = normalized.split(' ');
  const compact = tokens.join('');
  const phraseMatch = phrase => {
    const wanted = normalizeDescriptor(phrase).split(' ').filter(Boolean);
    for (let index = 0; index + wanted.length <= tokens.length; index += 1) {
      if (wanted.every((part, offset) => tokens[index + offset] === part)) return true;
    }
    return false;
  };
  return ![...SAFETY_COMPACT].some(alias => compact.includes(alias)) && ![...SAFETY_TERMS].some(phraseMatch);
}
function descriptorClassification(info) {
  const descriptors = Array.isArray(info?.descriptors) ? info.descriptors : [];
  const options = Array.isArray(info?.options) ? info.options : [];
  let aggregate = 0;
  if (descriptors.length > Number(SAFETY_POLICY.caps?.max_descriptors || 32)) return { overflow: true, sensitive: true };
  for (const value of descriptors) {
    const bytes = Buffer.byteLength(String(value ?? ''), 'utf8');
    if (bytes > Number(SAFETY_POLICY.caps?.max_descriptor_bytes || 2048)) return { overflow: true, sensitive: true };
    aggregate += bytes;
  }
  if (aggregate > Number(SAFETY_POLICY.caps?.max_descriptor_aggregate_bytes || 8192)) return { overflow: true, sensitive: true };
  if (options.length > Number(SAFETY_POLICY.caps?.max_options || 200)) return { overflow: true, sensitive: true };
  let optionBytes = 0;
  let sensitive = false;
  for (const option of options) {
    const value = String(option?.value ?? '');
    const label = String(option?.label ?? '');
    const valueBytes = Buffer.byteLength(value, 'utf8'); const labelBytes = Buffer.byteLength(label, 'utf8');
    if (valueBytes > Number(SAFETY_POLICY.caps?.max_option_bytes || 2048)
        || labelBytes > Number(SAFETY_POLICY.caps?.max_option_bytes || 2048)) return { overflow: true, sensitive: true };
    optionBytes += valueBytes + labelBytes;
    if (!label.trim() || !descriptorSafe(value) || !descriptorSafe(label)
        || !descriptorSafe(value) || !descriptorSafe(label)) sensitive = true;
  }
  if (optionBytes > Number(SAFETY_POLICY.caps?.max_option_aggregate_bytes || 65536)) return { overflow: true, sensitive: true };
  if (descriptors.some(value => !descriptorSafe(value))) sensitive = true;
  return { overflow: false, sensitive };
}
function identityKey(frameUrl, info) {
  const origin = safeUrl(frameUrl)?.origin || frameUrl;
  const form = info.formActionUrl ? (() => { const u = safeUrl(info.formActionUrl); return u ? `${u.origin}${u.pathname}` : ''; })() : '';
  return hash([origin, form, info.kind, info.name || '', info.label || '', info.groupId || '', (info.options || []).map(option => `${option.value}:${option.label}`).join('|')].join('\u001f')).slice(0, 24);
}
function reviewClickKey(frameUrl, info) {
  const origin = safeUrl(frameUrl)?.origin || frameUrl;
  const identity = info.id || info.name || '';
  const text = String(info.text || '').replace(/\s+/g, ' ').trim();
  return hash([origin, info.isAnchor ? 'a' : 'button', identity, text].join('\u001f')).slice(0, 24);
}
async function consumeRealmReviewPermit(request) {
  if (!page || request.frame() !== page.mainFrame() || !request.isNavigationRequest()) return false;
  let permit = null;
  try { permit = await page.mainFrame().isolatedRealm().evaluate(() => globalThis.__JOBS_ASSISTANT_REVIEW_PERMIT__ || null); }
  catch { return false; }
  if (!permit || permit.trusted !== true || permit.detail === 0 || permit.expires < Date.now()) return false;
  const key = hash([String(permit.origin || ''), String(permit.kind || ''), String(permit.identity || ''), String(permit.text || '').replace(/\s+/g, ' ').trim()].join('\u001f')).slice(0, 24);
  const ledger = reviewLedger.get(key);
  if (!ledger || ledger.frameId !== 'frame-0' || ledger.href !== (permit.href || null)
      || ledger.action !== (permit.action || null) || ledger.method !== String(permit.method || 'GET').toUpperCase()) return false;
  const requestMethod = String(request.method() || 'GET').toUpperCase();
  const expected = ledger.action || ledger.href;
  const expectedUrl = safeUrl(expected); const requestUrl = safeUrl(request.url());
  if (requestMethod !== ledger.method || !expectedUrl || !requestUrl
      || requestUrl.href !== expectedUrl.href
      || isFinalLike(requestUrl.pathname) || [...requestUrl.searchParams.values()].some(isFinalLike)
      || !validateFormAction(expected, requestMethod, page.url())) return false;
  // The permit is a one-use capability for this exact document URL, including
  // query and method. Proxy authorization below is separately consumed once.
  reviewPermit = { url: requestUrl.href, method: requestMethod, expires: Date.now() + 1000, proxyConsumed: false };
  reviewEpoch = { routeIdentity: documentRouteIdentity, expires: Date.now() + 10000, redirects: 0, staticRequests: 0 };
  authorizeProxyRequest(request.url(), requestMethod);
  try { await page.mainFrame().isolatedRealm().evaluate(() => { globalThis.__JOBS_ASSISTANT_REVIEW_PERMIT__ = null; }); } catch {}
  return true;
}

async function safeElementInfo(handle) {
  return handle.evaluate((el, finalTokens) => {
    const tag = String(el.localName || '').toLowerCase();
    const input = tag === 'input'; const textarea = tag === 'textarea'; const select = tag === 'select';
    const button = tag === 'button'; const anchor = tag === 'a';
    const rawType = input ? String(el.getAttribute('type') || 'text').toLowerCase() : (textarea ? 'textarea' : select ? 'select' : tag);
    const type = button ? String(el.getAttribute('type') || 'submit').toLowerCase() : rawType;
    const form = el.form || null;
    const own = ['type', 'name', 'id', 'autocomplete', 'placeholder', 'title', 'aria-label'].map(name => el.getAttribute(name)).filter(Boolean);
    const labels = [];
    if (el.labels) for (const item of el.labels) labels.push(item.textContent || '');
    const described = el.getAttribute('aria-describedby');
    if (described) for (const id of described.split(/\s+/)) { const item = el.ownerDocument.getElementById(id); if (item) labels.push(item.textContent || ''); }
    const parentLabel = el.closest('label,fieldset'); if (parentLabel) labels.push(parentLabel.textContent || '');
    // Field values are applicant data, never descriptor material. Button values
    // are control metadata needed for final-submit classification.
    const scalarValue = input && ['checkbox', 'radio'].includes(type) ? Boolean(el.checked)
      : input && type === 'file' ? null : String(el.value ?? '');
    const buttonValue = (button || input) && ['submit', 'button', 'reset', 'image'].includes(type)
      ? String(el.value ?? '') : '';
    const text = (button || anchor) ? String(el.innerText || el.textContent || '').trim() : '';
    const descriptors = [...own, ...labels, ...(button || anchor ? [text, buttonValue] : [])].map(value => String(value).trim()).filter(Boolean);
    const rect = typeof el.getBoundingClientRect === 'function' ? el.getBoundingClientRect() : { width: 0, height: 0 };
    const style = typeof getComputedStyle === 'function' ? getComputedStyle(el) : null;
    const valueDescriptor = input ? Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value') : textarea ? Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value') : null;
    const checkedDescriptor = input ? Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'checked') : null;
    const options = select ? Array.from(el.options || []).map(option => ({ value: String(option.value || ''), label: String(option.label || option.text || ''), enabled: !option.disabled })) : [];
    const label = String(el.getAttribute('aria-label') || labels.join(' ').replace(/\s+/g, ' ').trim() || el.getAttribute('placeholder') || el.getAttribute('name') || el.id || '').slice(0, 2048);
    const name = el.getAttribute('name') || el.id || null;
    const action = form ? (el.hasAttribute('formaction') ? el.formAction : form.action) : null;
    const method = form ? String(el.hasAttribute('formmethod') ? el.formMethod : form.method || 'get').toLowerCase() : null;
    const formIndex = form ? Array.from(el.ownerDocument.forms || []).indexOf(form) : -1;
    const formIdentity = form ? [form.id || '', form.getAttribute('name') || '', action || '', method || '', formIndex].join('\u001f') : null;
    const formRegistry = globalThis.__JOBS_ASSISTANT_FORM_REGISTRY__ || (globalThis.__JOBS_ASSISTANT_FORM_REGISTRY__ = { tokens: new WeakMap(), next: 1 });
    const formToken = form ? (() => {
      let token = formRegistry.tokens.get(form);
      if (!token) { token = `form-${formRegistry.next++}`; formRegistry.tokens.set(form, token); }
      return token;
    })() : null;
    const groupId = input && ['checkbox', 'radio'].includes(type) && name ? `${form ? (form.action || '') : ''}\u001f${name}` : null;
    const flat = [...descriptors, name, action, el.getAttribute('href'), text, buttonValue].filter(Boolean).join(' ');
    const finalLike = finalTokens.some(token => new Set((flat.toLowerCase().match(/[a-z0-9]+/g) || [])).has(String(token).toLowerCase()));
    return {
      id: el.id || null,
      tag,
      isField: (input || textarea || select) && !['hidden', 'submit', 'button', 'reset', 'image'].includes(type),
      isButton: button || anchor || el.getAttribute('role') === 'button' || (input && ['submit', 'button', 'reset', 'image'].includes(type)),
      isNativeOfflineButton: button && type === 'button',
      isAnchor: anchor, isFile: input && type === 'file', isCustomElement: tag.includes('-'),
      prototypePoisoned: (input || textarea) && (!valueDescriptor || typeof valueDescriptor.set !== 'function') || (input && ['checkbox', 'radio'].includes(type) && (!checkedDescriptor || typeof checkedDescriptor.set !== 'function')),
      kind: textarea ? 'textarea' : select ? 'select' : type, name, label, groupId, formIdentity, formToken, optionValue: input && ['checkbox', 'radio'].includes(type) ? String(el.value || '') : null, descriptors, selector: el.id ? `#${CSS.escape(el.id)}` : name ? `${tag}[name="${String(name).replaceAll('"', '\\"')}"]` : tag,
      required: Boolean(el.required || el.getAttribute('aria-required') === 'true'), visible: Boolean(rect.width && rect.height && (!style || (style.visibility !== 'hidden' && style.display !== 'none'))), enabled: !el.disabled, readonly: Boolean(el.readOnly),
      value: scalarValue, buttonValue, willValidate: Boolean(el.willValidate), valid: Boolean(el.validity ? el.validity.valid : true),
      validityFlags: el.validity ? ['valueMissing', 'typeMismatch', 'patternMismatch', 'tooLong', 'tooShort', 'rangeUnderflow', 'rangeOverflow', 'stepMismatch', 'badInput', 'customError'].filter(flag => Boolean(el.validity[flag])) : [],
      fileCount: input && type === 'file' && el.files ? el.files.length : 0, fileBasenames: input && type === 'file' && el.files ? Array.from(el.files).map(file => file.name) : [], accept: input && type === 'file' ? String(el.accept || '').split(',').map(value => value.trim()).filter(Boolean) : [],
      minLength: Number.isInteger(el.minLength) && el.minLength >= 0 ? el.minLength : null, maxLength: Number.isInteger(el.maxLength) && el.maxLength >= 0 ? el.maxLength : null, pattern: el.pattern || null, minValue: el.min || null, maxValue: el.max || null, step: el.step || null, options,
      formActionUrl: action, effectiveMethod: method, text: text.slice(0, 2048), buttonType: input ? type : (button ? type : (anchor ? 'anchor' : null)), target: el.getAttribute('target'), download: Boolean(el.getAttribute('download')), hrefUrl: anchor ? el.href : null, hrefAttribute: anchor ? el.getAttribute('href') : null, finalLike,
    };
  }, [...FINAL_LIKE_TOKENS]);
}

function freezeProxy() {
  proxyFrozen = true;
  for (const socket of proxySockets) socket.destroy();
  proxySockets.clear();
  proxyTunnelExpiries.clear();
}
async function testProxySetup(command) {
  if (process.env.JOBS_ASSISTANT_TEST_PROXY !== '1' || proxy) throw new Error('test_proxy_unavailable');
  const route = validateInitialUrl(command.logical_url);
  logicalInitialUrl = route.url;
  documentRouteIdentity = documentRouteKey(route);
  internalTransportUrl = null;
  proxyFrozen = false;
  firstApplicantMutation = false;
  terminalReason = null;
  reviewState = 'closed';
  reviewPermit = null;
  reviewEpoch = null;
  proxyPermitUrls.clear();
  const proxyPort = await startProxy();
  return { proxy_port: proxyPort };
}

function testProxyFreeze(command = {}) {
  if (process.env.JOBS_ASSISTANT_TEST_PROXY !== '1' || !proxy) throw new Error('test_proxy_unavailable');
  if (command.mutate !== false) beginMutation();
  else freezeProxy();
  if (typeof command.terminal_reason === 'string' && command.terminal_reason) terminalReason = command.terminal_reason;
  return { ...networkCounters, terminal_reason: terminalReason };
}


function invalidateGeneration(reason = 'stale_generation') {
  generationConsumed = true;
  generationBlocked = true;
  generationBlocker = reason;
  currentGeneration = null;
  const stale = handleCache;
  handleCache = new Map();
  reviewLedger = new Map();
  for (const entry of stale.values()) void entry.handle.dispose().catch(() => {});
}

function toObservedField(targetId, frameId, frameUrl, info, key, sensitive) {
  return { target_id: targetId, field_key: key, frame_id: frameId, frame_url: frameUrl, form_action_url: info.formActionUrl, kind: info.kind, name: info.name, label: info.label, group_id: info.groupId || null, option_value: info.optionValue || null, safety_descriptors: info.descriptors, selector: info.selector, required: info.required, visible: info.visible, enabled: info.enabled, readonly: info.readonly, value: info.value, will_validate: info.willValidate, valid: info.valid, validity_flags: [...info.validityFlags, ...(sensitive ? ['sensitive_field'] : [])], file_count: info.fileCount, file_basenames: info.fileBasenames, accept: info.accept, min_length: info.minLength, max_length: info.maxLength, pattern: info.pattern, min_value: info.minValue, max_value: info.maxValue, step: info.step, options: info.options };
}
function toObservedButton(targetId, frameId, frameUrl, info, clickKey, sensitive) {
  return { target_id: targetId, frame_id: frameId, frame_url: frameUrl, click_key: info.finalLike || sensitive || info.collision ? null : clickKey, collision_count: Number(info.collisionCount || 0), element_id: info.id || null, element_kind: info.isAnchor ? 'a' : 'button', text: info.text, selector: info.selector, button_type: info.buttonType, name: info.name, value: info.buttonValue || null, target: info.target, download: info.download, effective_action_url: info.formActionUrl, effective_method: info.effectiveMethod, href_url: info.hrefUrl, href_attribute: info.hrefAttribute, visible: info.visible, enabled: info.enabled, safety_descriptors: info.descriptors };
}
function assertPage() { if (!page) throw new Error('Puppeteer page is not initialized'); }
async function trustedFrame(frame) {
  let current = frame;
  while (current) {
    const value = current.url();
    const url = safeUrl(value);
    if (!url || url.protocol === 'about:') return false;
    if (isLocalHost(url.hostname)) {
      const transport = safeUrl(internalTransportUrl || '');
      if (!transport || url.origin !== transport.origin || url.pathname !== transport.pathname) return false;
    } else {
      if (url.protocol !== 'https:' || !ALLOWED_ATS_HOSTS.has(url.hostname.toLowerCase())) return false;
      const routeIdentity = routeIdentityForUrl(url.href);
      if (!routeIdentity || !documentRouteIdentity || !sameDocumentRoute(routeIdentity, documentRouteIdentity)) return false;
    }
    current = current.parentFrame();
  }
  return true;
}
function frameAncestry(frame) {
  const chain = [];
  let current = frame;
  while (current) {
    chain.push({ frame: current, url: current.url() });
    current = current.parentFrame();
  }
  return chain;
}
async function scanVisibleBlockers(frame) {
  const mainBody = await frame.$('body');
  if (!mainBody) return { blockers: [], errors: [] };
  const body = await frame.isolatedRealm().adoptHandle(mainBody);
  await mainBody.dispose().catch(() => {});
  try {
    return await body.evaluate(root => {
      const document = root.ownerDocument;
      const blockers = [];
      const errors = [];
      const seen = new Set();
      const visible = element => {
        if (!element || typeof element.getBoundingClientRect !== 'function') return false;
        const rect = element.getBoundingClientRect();
        const style = typeof getComputedStyle === 'function' ? getComputedStyle(element) : null;
        return Boolean(rect.width && rect.height && (!style || (style.display !== 'none' && style.visibility !== 'hidden')));
      };
      const add = (code, text) => {
        const value = String(text || '').replace(/\s+/g, ' ').trim().slice(0, 2048);
        const key = `${code}\u001f${value}`;
        if (!seen.has(key)) { seen.add(key); blockers.push({ code, text: value || code }); }
      };
      const classify = text => {
        const value = String(text || '').toLowerCase();
        if (/\b(?:captcha|recaptcha|hcaptcha)\b|i['’]?m\s+not\s+a\s+robot/.test(value)) add('captcha', text);
        else if (/\b(?:authentication|required\s+to\s+log\s+in|sign\s+in|log\s+in|session\s+expired)\b/.test(value)) add('authentication_required', text);
        else if (/\b(?:assessment|coding\s+challenge|skills?\s+test|test\s+invitation)\b/.test(value)) add('assessment_required', text);
      };
      classify(String(root.innerText || '').slice(0, 8192));
      const errorNodes = document.querySelectorAll('[role="alert"], [aria-live="assertive"], [aria-invalid="true"], .error, .errors, .field-error, .validation-error, [data-error]');
      for (const element of errorNodes) {
        if (!visible(element)) continue;
        const text = String(element.innerText || element.textContent || '').replace(/\s+/g, ' ').trim();
        if (text || element.getAttribute('aria-invalid') === 'true') {
          const value = text || 'visible invalid field';
          errors.push({ text: value.slice(0, 2048) });
          add('page_validation_error', value);
        }
      }
      return { blockers, errors };
    });
  } finally {
    await body.dispose().catch(() => {});
  }
}
async function observe() {
  assertPage();
  for (const entry of handleCache.values()) await entry.handle.dispose().catch(() => {});
  handleCache = new Map();
  reviewLedger = new Map();
  observationGeneration += 1; currentGeneration = `obs-${observationGeneration}`; generationConsumed = false;
  generationBlocked = false;
  generationBlocker = null;
  const currentUrl = page.url();
  validateDocumentNavigation(currentUrl, { redirectCount: documentRedirectCount });
  const observation = { observation_id: currentGeneration, url: currentUrl, title: await page.title(), site_markers: [], fields: [], buttons: [], final_submit_target_ids: [], errors: [], blockers: [], counters: { ...networkCounters }, terminal_reason: terminalReason };
  const seen = new Map(); let frameIndex = 0;
  for (const frame of page.frames()) {
    const frameId = `frame-${frameIndex++}`; const trusted = await trustedFrame(frame);
    const ancestry = frameAncestry(frame);
    if (!trusted) { observation.blockers.push({ code: 'unsupported_frame', frame_id: frameId, text: 'frame origin is not adapter-approved' }); continue; }
    try {
      const signals = await scanVisibleBlockers(frame);
      for (const blocker of signals.blockers || []) observation.blockers.push({ ...blocker, frame_id: frameId });
      for (const error of signals.errors || []) observation.errors.push({ target_id: null, text: error.text });
    } catch {
      observation.blockers.push({ code: 'unsupported_frame', frame_id: frameId, text: 'isolated blocker scan unavailable' });
      continue;
    }
    let elements;
    try {
      const realm = frame.isolatedRealm();
      const collection = await realm.evaluateHandle(() => {
        const query = Object.getOwnPropertyDescriptor(Document.prototype, 'querySelectorAll')?.value;
        if (typeof query !== 'function') throw new Error('query_selector_unavailable');
        return Array.from(query.call(document, 'input, textarea, select, button, a, [role="button"]'));
      });
      const properties = await collection.getProperties();
      elements = [];
      for (const [key, property] of properties) {
        if (!/^[0-9]+$/.test(key)) { await property.dispose().catch(() => {}); continue; }
        const element = property.asElement?.();
        if (element) elements.push(element);
        else await property.dispose().catch(() => {});
      }
      await collection.dispose().catch(() => {});
    } catch {
      observation.blockers.push({ code: 'unsupported_frame', frame_id: frameId, text: 'isolated realm unavailable' });
      continue;
    }
    let fieldIndex = 0; let buttonIndex = 0;
    for (const handle of elements) {
      const info = await safeElementInfo(handle).catch(() => null);
      if (!info) { await handle.dispose().catch(() => {}); continue; }
      const isCandidate = info.isField || info.isButton;
      if (!isCandidate) { await handle.dispose().catch(() => {}); continue; }
      const key = identityKey(frame.url(), info);
      const clickKey = info.isButton ? reviewClickKey(frame.url(), info) : null;
      const identity = info.isField ? `field:${key}` : `button:${clickKey}`;
      seen.set(identity, (seen.get(identity) || 0) + 1);
      const classification = descriptorClassification(info);
      if (classification.overflow) {
        observation.blockers.push({ code: 'observation_too_large', frame_id: frameId, text: 'descriptor or option cap exceeded' });
        await handle.dispose().catch(() => {});
        continue;
      }
      const sensitive = classification.sensitive;
      info.sensitive = sensitive;
      if (info.isField) {
        if (observation.fields.length >= 500) { observation.blockers.push({ code: 'observation_too_large', frame_id: frameId, text: 'field cap exceeded' }); await handle.dispose().catch(() => {}); continue; }
        const targetId = `${currentGeneration}:${frameId}:field-${fieldIndex++}`;
        info.fieldKey = key; info.sensitive = sensitive; info.identity = identity;
        info.collisionCount = seen.get(identity) || 0;
        handleCache.set(targetId, { handle, info, frame, frameUrl: frame.url(), frameChain: ancestry, generation: currentGeneration, kind: 'field' });
        const fieldRecord = toObservedField(targetId, frameId, frame.url(), info, key, sensitive);
        fieldRecord.collision_count = info.collisionCount;
        observation.fields.push(fieldRecord);
      } else {
        if (buttonIndex >= 200) { observation.blockers.push({ code: 'observation_too_large', frame_id: frameId, text: 'button cap exceeded' }); await handle.dispose().catch(() => {}); continue; }
        const targetId = `${currentGeneration}:${frameId}:button-${buttonIndex++}`;
        info.clickKey = clickKey; info.identity = identity; info.collisionCount = seen.get(identity) || 0;
        handleCache.set(targetId, { handle, info, frame, frameUrl: frame.url(), frameChain: ancestry, generation: currentGeneration, kind: 'button' });
        if (info.finalLike) observation.final_submit_target_ids.push(targetId);
      }
    }
  }
  for (const [targetId, entry] of handleCache) {
    const count = seen.get(entry.info.identity) || 0;
    entry.info.collisionCount = count;
    entry.info.collision = count > 1;
    if (entry.kind === 'field') {
      const field = observation.fields.find(item => item.target_id === targetId);
      if (field) {
        field.collision_count = count;
        if (count > 1 && !field.validity_flags.includes('field_identity_collision')) field.validity_flags.push('field_identity_collision');
      }
    } else {
      const button = toObservedButton(targetId, targetId.split(':')[1] || '', entry.frameUrl, entry.info, entry.info.clickKey, Boolean(entry.info.sensitive));
      button.collision_count = count;
      observation.buttons.push(button);
      if (count === 1 && entry.info.clickKey && (entry.info.isAnchor || entry.info.tag === 'button')
          && validateFormAction(entry.info.formActionUrl || entry.info.hrefUrl, String(entry.info.effectiveMethod || 'get').toUpperCase(), entry.frameUrl)) {
        reviewLedger.set(entry.info.clickKey, {
          frameId: targetId.split(':')[1],
          href: entry.info.hrefUrl,
          action: entry.info.formActionUrl || entry.info.hrefUrl,
          method: String(entry.info.effectiveMethod || 'get').toUpperCase(),
          routeIdentity: documentRouteIdentity,
        });
      }
    }
  }
  const hardCodes = new Set(['unsupported_frame', 'captcha', 'authentication_required', 'assessment_required', 'page_validation_error', 'observation_too_large']);
  const hard = observation.blockers.find(blocker => hardCodes.has(blocker.code));
  if (hard) {
    generationBlocked = true;
    generationBlocker = hard.code;
    for (const entry of handleCache.values()) await entry.handle.dispose().catch(() => {});
    handleCache = new Map();
  }
  const encoded = Buffer.byteLength(JSON.stringify(observation));
  if (encoded > MAX_OBSERVATION_BYTES) {
    generationBlocked = true;
    generationBlocker = 'observation_too_large';
    for (const entry of handleCache.values()) await entry.handle.dispose().catch(() => {});
    handleCache = new Map();
    throw new Error('observation_too_large');
  }
  return observation;
}

async function consumeTarget(command) {
  if (generationBlocked) throw new Error(generationBlocker || 'observation_blocked');
  const targetId = String(command.target_id || ''); const entry = handleCache.get(targetId);
  if (!entry || entry.generation !== currentGeneration) throw new Error('stale_generation');
  if (generationConsumed) throw new Error('generation_already_consumed');
  if (terminalReason) throw new Error(terminalReason);
  if (entry.info.collision) throw new Error('field_identity_collision');
  generationConsumed = true;
  const selected = entry.handle;
  const all = [...handleCache.values()]; handleCache = new Map();
  for (const item of all) if (item.handle !== selected) await item.handle.dispose().catch(() => {});
  let documentCurrent = true;
  try { validateDocumentNavigation(page.url(), { redirectCount: documentRedirectCount }); } catch { documentCurrent = false; }
  const currentChain = entry.frame ? frameAncestry(entry.frame) : [];
  const observedChain = Array.isArray(entry.frameChain) ? entry.frameChain : [];
  const ancestryCurrent = currentChain.length === observedChain.length
    && currentChain.every((item, index) => item.frame === observedChain[index].frame && item.url === observedChain[index].url);
  const frameCurrent = Boolean(entry.frame && page.frames().includes(entry.frame));
  const frameTrusted = frameCurrent && ancestryCurrent && documentCurrent && await trustedFrame(entry.frame).catch(() => false);
  const live = frameTrusted ? await safeElementInfo(selected).catch(() => null) : null;
  const liveClassification = live ? descriptorClassification(live) : { overflow: false, sensitive: false };
  const isButton = entry.kind === 'button';
  const identityMatches = isButton
    ? reviewClickKey(entry.frame.url(), live || {}) === entry.info.clickKey
    : identityKey(entry.frame.url(), live || {}) === entry.info.fieldKey;
  const forbiddenButton = isButton && (live?.finalLike || live?.isAnchor || entry.info.finalLike || entry.info.isAnchor);
  if (!frameTrusted || !live || live.kind !== entry.info.kind
      || !identityMatches
      || live.formIdentity !== entry.info.formIdentity
      || live.formToken !== entry.info.formToken
      || !validateFormAction(live.formActionUrl || live.hrefUrl, live.effectiveMethod, entry.frame.url())
      || forbiddenButton) {
    await selected.dispose().catch(() => {});
    throw new Error(forbiddenButton ? 'final_or_anchor_not_automated' : 'stale_generation');
  }
  if (liveClassification.overflow) { await selected.dispose().catch(() => {}); throw new Error('observation_too_large'); }
  if (!live.visible || !live.enabled || live.readonly || live.isCustomElement || live.prototypePoisoned || liveClassification.sensitive) {
    await selected.dispose().catch(() => {});
    throw new Error(liveClassification.sensitive ? 'sensitive_field' : 'target_not_actionable');
  }
  return { selected, live };
}
function hasForbiddenScalarCharacters(value, { multiline = false } = {}) {
  for (const char of value) {
    const code = char.codePointAt(0);
    if (code === 0x202a || code === 0x202b || code === 0x202c || code === 0x202d || code === 0x202e || code === 0x2066 || code === 0x2067 || code === 0x2069) return true;
    if (code < 0x20 && (multiline ? ![9, 10, 13].includes(code) : true)) return true;
    if (!multiline && [9, 10, 13].includes(code)) return true;
  }
  return false;
}
function canonicalDecimal(value) { return /^(?:0|[+-]?(?:[1-9]\d*)(?:\.\d+)?|[+-]?0\.\d+)$/.test(value) && Number.isFinite(Number(value)); }
function validateCandidateValue(value, live) {
  if (typeof value !== 'string') return false;
  const multiline = live.kind === 'textarea';
  if (hasForbiddenScalarCharacters(value, { multiline })) return false;
  const limit = multiline ? 20000 : 2048;
  if (value.length > Math.min(limit, live.maxLength ?? limit) || (!multiline && value.length < (live.minLength || 0))) return false;
  if (live.pattern) {
    try { if (!(new RegExp(`^(?:${live.pattern})$`)).test(value)) return false; } catch { return false; }
  }
  if (live.kind === 'email' && (value.length > 320 || !/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(value))) return false;
  if (live.kind === 'tel' && (value.length > 64 || (value.match(/\d/g) || []).length < 7 || (value.match(/\d/g) || []).length > 15)) return false;
  if (live.kind === 'url') {
    if (value.length > 2048) return false;
    try { const parsed = new URL(value); if (parsed.protocol !== 'https:' || parsed.username || parsed.password) return false; } catch { return false; }
  }
  if (live.kind === 'date' && !/^\d{4}-\d{2}-\d{2}$/.test(value)) return false;
  if (live.kind === 'number') {
    if (!canonicalDecimal(value)) return false;
    const numeric = Number(value);
    if (live.minValue !== null && live.minValue !== '' && numeric < Number(live.minValue)) return false;
    if (live.maxValue !== null && live.maxValue !== '' && numeric > Number(live.maxValue)) return false;
    if (live.step && live.step !== 'any') {
      const step = Number(live.step); const base = live.minValue ? Number(live.minValue) : 0;
      if (!Number.isFinite(step) || step <= 0 || Math.abs((numeric - base) / step - Math.round((numeric - base) / step)) > 1e-9) return false;
    }
  }
  return true;
}
async function nativeCandidateValid(handle, value) {
  return handle.evaluate((el, next) => {
    const clone = el.cloneNode(false);
    const proto = clone.localName === 'textarea' ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
    const setter = Object.getOwnPropertyDescriptor(proto, 'value')?.set;
    if (!setter) return false;
    setter.call(clone, next);
    return !clone.willValidate || Boolean(clone.validity?.valid);
  }, value).catch(() => false);
}
function beginMutation() {
  if (!firstApplicantMutation) {
    firstApplicantMutation = true;
    freezeProxy();
  }
}
function stageImmutableUpload(root, candidate, expectedHash) {
  const uploadPath = confinedPath(root, candidate);
  const flags = fs.constants.O_RDONLY | (fs.constants.O_NOFOLLOW || 0);
  let fd = null; let temp = null; let cleanupDir = null;
  try {
    fd = fs.openSync(uploadPath, flags);
    const before = fs.fstatSync(fd);
    if (!before.isFile() || before.size > 10 * 1024 * 1024) throw new Error('file_budget');
    const bytes = fs.readFileSync(fd);
    const digest = crypto.createHash('sha256').update(bytes).digest('hex');
    if (!/^[0-9a-f]{64}$/.test(expectedHash || '') || digest !== expectedHash) throw new Error('staged_input_mismatch');
    const after = fs.fstatSync(fd); const pathStat = fs.lstatSync(uploadPath);
    if (pathStat.isSymbolicLink() || !pathStat.isFile() || before.dev !== after.dev || before.ino !== after.ino
        || before.size !== after.size || pathStat.dev !== before.dev || pathStat.ino !== before.ino) throw new Error('staged_input_changed');
    const tempRoot = ownerRoot || fs.realpathSync(root);
    fs.mkdirSync(tempRoot, { recursive: true, mode: 0o700 });
    cleanupDir = path.join(tempRoot, `.upload-${process.pid}-${crypto.randomBytes(8).toString('hex')}`);
    fs.mkdirSync(cleanupDir, { mode: 0o700 });
    temp = path.join(cleanupDir, candidate);
    const out = fs.openSync(temp, 'wx', 0o600);
    try { fs.writeFileSync(out, bytes); fs.fsyncSync(out); } finally { fs.closeSync(out); }
    const staged = fs.lstatSync(temp);
    if (!staged.isFile() || staged.size !== bytes.length) throw new Error('staged_input_changed');
    return { path: temp, cleanupDir, basename: candidate, bytes: bytes.length };
  } catch (error) {
    if (temp) { try { fs.unlinkSync(temp); } catch {} }
    if (cleanupDir) { try { fs.rmdirSync(cleanupDir); } catch {} }
    throw error;
  } finally {
    if (fd !== null) fs.closeSync(fd);
  }
}
async function action(command) {
  const { selected, live } = await consumeTarget(command);
  try {
    if (live.isButton) return await buttonAction(selected, live, command);
    if (command.action === 'fill') {
      if (live.isFile || live.kind === 'select' || !validateCandidateValue(command.value, live)
          || !(await nativeCandidateValid(selected, command.value))) throw new Error('invalid_field_value');
      beginMutation();
      await selected.evaluate((el, next) => {
        const proto = el.localName === 'textarea' ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
        const setter = Object.getOwnPropertyDescriptor(proto, 'value')?.set;
        if (!setter) throw new Error('prototype_poisoned');
        setter.call(el, next);
        el.dispatchEvent(new Event('input', { bubbles: true })); el.dispatchEvent(new Event('change', { bubbles: true }));
      }, command.value);
    } else if (command.action === 'check') {
      if (!['checkbox', 'radio'].includes(live.kind) || typeof command.value !== 'boolean') throw new Error('invalid_boolean_value');
      beginMutation();
      await selected.evaluate((el, value) => {
        const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'checked')?.set;
        if (!setter) throw new Error('prototype_poisoned');
        setter.call(el, value);
        el.dispatchEvent(new Event('input', { bubbles: true })); el.dispatchEvent(new Event('change', { bubbles: true }));
      }, command.value);
    } else if (command.action === 'select') {
      if (live.kind !== 'select' || typeof command.value !== 'string' || !live.options.some(option => option.enabled && option.value === command.value)) throw new Error('invalid_select_value');
      beginMutation();
      await selected.evaluate((el, value) => {
        const setter = Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, 'value')?.set;
        if (!setter) throw new Error('prototype_poisoned');
        setter.call(el, value);
        el.dispatchEvent(new Event('input', { bubbles: true })); el.dispatchEvent(new Event('change', { bubbles: true }));
      }, command.value);
    } else if (command.action === 'upload') {
      if (!live.isFile || Object.hasOwn(command, 'path')) throw new Error('upload_path_forbidden');
      const root = process.env.JOBS_ASSISTANT_INPUT_ROOT;
      const candidate = process.env.JOBS_ASSISTANT_STAGED_INPUT_NAME;
      const mediaType = process.env.JOBS_ASSISTANT_STAGED_INPUT_MEDIA_TYPE;
      if (!acceptsStagedInput(live.accept, candidate, mediaType || '')) throw new Error('upload_accept_mismatch');
      beginMutation();
      const staged = stageImmutableUpload(root, candidate, process.env.JOBS_ASSISTANT_STAGED_INPUT_SHA256);
      try {
        await selected.uploadFile(staged.path);
      } catch (error) {
        try { fs.unlinkSync(staged.path); } catch {}
        try { fs.rmdirSync(staged.cleanupDir); } catch {}
        throw error;
      }
    } else throw new Error(`unknown_target_action:${command.action}`);
    await settle();
    const after = await safeElementInfo(selected).catch(() => null);
    const retained = command.action === 'fill' ? after?.value === command.value
      : command.action === 'check' ? after?.value === command.value
        : command.action === 'select' ? after?.value === command.value
          : command.action === 'upload' ? after?.fileCount === 1 && after?.fileBasenames?.length === 1 && after.fileBasenames[0] === process.env.JOBS_ASSISTANT_STAGED_INPUT_NAME : true;
    if (!retained) throw new Error('field_value_not_retained');
    return { retained: true, counters: { ...networkCounters } };
  } finally { await selected.dispose().catch(() => {}); }
}
async function buttonAction(handle, info, command) {
  if (!command.offline || info.isAnchor || !info.isNativeOfflineButton || info.finalLike || info.buttonType !== 'button' || !info.visible || !info.enabled) throw new Error('final_or_anchor_not_automated');
  const hit = await handle.evaluate(el => {
    const rect = el.getBoundingClientRect();
    const style = getComputedStyle(el);
    if (!rect.width || !rect.height || style.display === 'none' || style.visibility === 'hidden' || style.pointerEvents === 'none') return false;
    const getter = Object.getOwnPropertyDescriptor(Document.prototype, 'elementFromPoint')?.value;
    if (typeof getter !== 'function') return false;
    const top = getter.call(document, rect.left + rect.width / 2, rect.top + rect.height / 2);
    return top === el || Boolean(top && el.contains(top));
  }).catch(() => false);
  if (!hit) throw new Error('button_not_hit_tested');
  beginMutation();
  await handle.evaluate(el => {
    const click = Object.getOwnPropertyDescriptor(HTMLElement.prototype, 'click')?.value;
    if (typeof click !== 'function') throw new Error('prototype_poisoned');
    click.call(el);
  });
  await settle();
  return { clicked: true, counters: { ...networkCounters } };
}
async function screenshot(command) {
  assertPage();
  const allowedSlots = new Set(['initial', 'after-reveal', 'blocker', 'final']);
  const slot = String(command.slot || '');
  if (!allowedSlots.has(slot)) throw new Error('screenshot_slot_forbidden');
  const root = process.env.JOBS_ASSISTANT_SCREENSHOT_ROOT ? path.resolve(process.env.JOBS_ASSISTANT_SCREENSHOT_ROOT) : (ownerRoot || fs.mkdtempSync(path.join(os.tmpdir(), 'jobs-assistant-shot-')));
  fs.mkdirSync(root, { recursive: true, mode: 0o700 });
  const dimensions = await page.mainFrame().isolatedRealm().evaluate(() => ({
    width: Math.max(document.documentElement?.scrollWidth || 0, document.body?.scrollWidth || 0, innerWidth),
    height: Math.max(document.documentElement?.scrollHeight || 0, document.body?.scrollHeight || 0, innerHeight),
    viewportWidth: innerWidth, viewportHeight: innerHeight,
  })).catch(() => ({ width: 0, height: 0, viewportWidth: 0, viewportHeight: 0 }));
  const requestedFullPage = Boolean(command.fullPage) && !firstApplicantMutation;
  let fullPage = requestedFullPage && dimensions.width * dimensions.height <= 40_000_000;
  let truncated = requestedFullPage && !fullPage;
  const tempName = `${slot}-${process.pid}-${Date.now()}.png`;
  const tmp = path.join(root, `${tempName}.tmp`);
  const capture = async () => {
    await page.screenshot({ path: tmp, fullPage });
    let bytes = fs.readFileSync(tmp);
    if (bytes.length > MAX_SCREENSHOT_BYTES) {
      fs.rmSync(tmp, { force: true });
      if (fullPage) {
        fullPage = false; truncated = true;
        await page.screenshot({ path: tmp, fullPage: false });
        bytes = fs.readFileSync(tmp);
      }
    }
    if (bytes.length > MAX_SCREENSHOT_BYTES) throw new Error('artifact_budget');
    return bytes;
  };
  let bytes;
  try { bytes = await capture(); } catch (error) { fs.rmSync(tmp, { force: true }); throw error; }
  const size = bytes.length;
  const digest = crypto.createHash('sha256').update(bytes).digest('hex');
  const existing = screenshotRecords.get(digest);
  if (existing) {
    fs.rmSync(tmp, { force: true });
    return { ...existing, deduplicated: true };
  }
  if (screenshotRecords.size >= MAX_SCREENSHOTS_PER_RUN || screenshotTotalBytes + size > MAX_SCREENSHOT_TOTAL_BYTES) {
    fs.rmSync(tmp, { force: true });
    throw new Error('artifact_budget');
  }
  const out = path.join(root, `${slot}-${digest.slice(0, 16)}.png`);
  const fd = fs.openSync(tmp, 'r'); try { fs.fsyncSync(fd); } finally { fs.closeSync(fd); }
  fs.renameSync(tmp, out);
  const metadata = {
    path: path.basename(out), reference: `screenshot:${digest}`, bytes: size, sha256: digest,
    full_page: fullPage, truncated,
    pixel_width: fullPage ? dimensions.width : dimensions.viewportWidth,
    pixel_height: fullPage ? dimensions.height : dimensions.viewportHeight,
  };
  screenshotRecords.set(digest, metadata);
  screenshotTotalBytes += size;
  return metadata;
}
async function webRtcStatus() {
  assertPage();
  const available = await page.mainFrame().isolatedRealm().evaluate(() => typeof globalThis.RTCPeerConnection === 'function');
  return { available: Boolean(available), policy: 'disable_non_proxied_udp' };
}
function processIdentity(value, { expectedPid = null, allowNull = false } = {}) {
  if (allowNull && value === null) return null;
  if (!value || typeof value !== 'object' || Array.isArray(value) || Object.keys(value).sort().join(',') !== 'birth,pgid,pid') throw new Error('invalid_process_identity');
  if (!Number.isSafeInteger(value.pid) || value.pid <= 0 || !Number.isSafeInteger(value.pgid) || value.pgid <= 0 || value.pgid !== value.pid) throw new Error('invalid_process_identity');
  if (expectedPid !== null && value.pid !== expectedPid) throw new Error('process_identity_mismatch');
  if (typeof value.birth !== 'string' || !value.birth || value.birth.length > 256 || /[^\x00-\x7f]/.test(value.birth)) throw new Error('invalid_process_identity');
  return { pid: value.pid, pgid: value.pgid, birth: value.birth };
}
function validateStartupIdentity(value) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw new Error('startup_identity_required');
  const keys = Object.keys(value).sort().join(',');
  if (keys !== 'ats_policy,browser_identity,job_id,owner_identity,run_id,session_id,version') throw new Error('startup_identity_mismatch');
  if (value.version !== 1 || !Object.hasOwn(ROUTE_POLICIES, value.ats_policy)
      || !Number.isSafeInteger(value.run_id) || value.run_id <= 0 || !Number.isSafeInteger(value.job_id) || value.job_id <= 0
      || typeof value.session_id !== 'string' || !value.session_id || value.session_id.length > 256 || /[^\x00-\x7f]/.test(value.session_id)) throw new Error('startup_identity_mismatch');
  selectRoutePolicy(value.ats_policy);
  const owner = processIdentity(value.owner_identity, { expectedPid: process.pid });
  const browser = processIdentity(value.browser_identity, { allowNull: true });
  if (browser !== null) throw new Error('startup_identity_mismatch');
  return { version: 1, ats_policy: value.ats_policy, run_id: value.run_id, job_id: value.job_id, session_id: value.session_id, owner_identity: owner, browser_identity: browser };
}
async function startupIdentityCommand(command) {
  if (startupIdentity) {
    if (command.handshake !== process.env.JOBS_ASSISTANT_HANDSHAKE || JSON.stringify(validateStartupIdentity(command.identity)) !== JSON.stringify(startupIdentity)) throw new Error('startup_identity_mismatch');
    return { hello: true, protocol: 'length-prefixed-json-v1', identity: startupIdentity };
  }
  if (command.handshake !== process.env.JOBS_ASSISTANT_HANDSHAKE) throw new Error('browser_handshake_failed');
  startupIdentity = validateStartupIdentity(command.identity);
  browserIdentity = startupIdentity.browser_identity;
  if (process.env.JOBS_ASSISTANT_SESSION_MANIFEST) manifestWrite('starting');
  return { hello: true, protocol: 'length-prefixed-json-v1', identity: startupIdentity };
}
async function registerBrowserIdentity(command) {
  if (!startupIdentity) throw new Error('startup_identity_required');
  const observed = processIdentity(command.identity);
  const expectedPid = browser?.process?.()?.pid || null;
  if (!expectedPid || observed.pid !== expectedPid) throw new Error('browser_identity_mismatch');
  if (browserIdentity && JSON.stringify(browserIdentity) !== JSON.stringify(observed)) throw new Error('browser_identity_mismatch');
  browserIdentity = observed;
  startupIdentity = { ...startupIdentity, browser_identity: browserIdentity };
  manifestWrite('starting');
  return { browser_identity: browserIdentity, identity: startupIdentity };
}
function manifestWrite(state, extra = {}) {
  const manifest = process.env.JOBS_ASSISTANT_SESSION_MANIFEST;
  if (!manifest) return;
  if (!startupIdentity) throw new Error('startup_identity_required');
  const dir = path.dirname(manifest);
  fs.mkdirSync(dir, { recursive: true, mode: 0o700 });
  const temp = `${manifest}.${process.pid}.${crypto.randomBytes(8).toString('hex')}.tmp`;
  const data = {
    version: startupIdentity.version,
    ats_policy: startupIdentity.ats_policy,
    run_id: startupIdentity.run_id,
    job_id: startupIdentity.job_id,
    session_id: startupIdentity.session_id,
    owner_identity: startupIdentity.owner_identity,
    browser_identity: browserIdentity,
    owner_pid: startupIdentity.owner_identity.pid,
    owner_pgid: startupIdentity.owner_identity.pgid,
    owner_birth: startupIdentity.owner_identity.birth,
    browser_pid: browserIdentity ? browserIdentity.pid : null,
    browser_pgid: browserIdentity ? browserIdentity.pgid : null,
    browser_birth: browserIdentity ? browserIdentity.birth : null,
    commit_token_sha256: reviewToken ? hash(reviewToken) : null,
    state,
    profile_path: userDataDir,
    spawn_attempted: true,
    heartbeat: new Date().toISOString(),
    ...Object.fromEntries(Object.entries(extra).filter(([key]) => !['version', 'ats_policy', 'run_id', 'job_id', 'session_id', 'owner_identity', 'browser_identity', 'owner_pid', 'owner_pgid', 'owner_birth', 'browser_pid', 'browser_pgid', 'browser_birth', 'commit_token', 'commit_token_sha256', 'state', 'spawn_attempted'].includes(key))),
  };
  let fd = null;
  let dirFd = null;
  let renamed = false;
  try {
    fd = fs.openSync(temp, 'wx', 0o600);
    fs.writeFileSync(fd, JSON.stringify(data));
    fs.fchmodSync(fd, 0o600);
    fs.fsyncSync(fd);
    fs.closeSync(fd);
    fd = null;
    fs.renameSync(temp, manifest);
    renamed = true;
    dirFd = fs.openSync(dir, 'r');
    fs.fsyncSync(dirFd);
  } finally {
    if (fd !== null) fs.closeSync(fd);
    if (dirFd !== null) fs.closeSync(dirFd);
    if (!renamed) {
      try { fs.unlinkSync(temp); } catch (error) { if (error?.code !== 'ENOENT') throw error; }
    }
  }
}
async function prepareHandoff(command) {
  if (launchHeadless) { await close(); throw new Error('headless_handoff_forbidden'); }
  if (!startupIdentity || !browserIdentity) throw new Error('startup_identity_required');
  if (!browser || (firstApplicantMutation && terminalReason)) throw new Error('handoff_not_eligible');
  if (command.run_id !== startupIdentity.run_id || command.job_id !== startupIdentity.job_id || command.session_id !== startupIdentity.session_id) throw new Error('startup_identity_mismatch');
  reviewState = 'prepared'; manifestWrite('prepared'); return { state: reviewState, identity: startupIdentity };
}
async function commitHandoff(command) {
  if (!startupIdentity || !browserIdentity) throw new Error('startup_identity_required');
  if (reviewState === 'open_guarded' && reviewToken === command.commit_token) return { state: reviewState, idempotent: true, identity: startupIdentity };
  if (reviewState !== 'prepared' || typeof command.commit_token !== 'string' || command.commit_token.length < 16) throw new Error('handoff_state_conflict');
  reviewToken = command.commit_token;
  await installReviewGesture();
  reviewState = 'open_guarded';
  manifestWrite('open_guarded');
  heartbeatTimer = setInterval(() => { if (reviewState === 'open_guarded') { try { manifestWrite('open_guarded', { detached: detachedOwner }); } catch { /* owner will observe failure */ } } }, 5000);
  return { state: reviewState, identity: startupIdentity };
}
async function installReviewGesture() {
  if (!page || page.isClosed()) return;
  const realm = page.mainFrame().isolatedRealm();
  await realm.evaluate(() => {
    document.addEventListener('click', event => {
      if (!event.isTrusted || event.detail === 0) return;
      const target = event.target;
      if (!(target instanceof Element)) return;
      const anchor = target.closest('a');
      const button = target.closest('button, input[type="submit"], input[type="button"], input[type="reset"]');
      if (!anchor && !button) return;
      const element = anchor || button;
      const form = button?.form || null;
      const href = anchor?.href || null;
      const action = button ? (button.formAction || form?.action || null) : href;
      const method = button ? String(button.formMethod || form?.method || 'get').toUpperCase() : 'GET';
      const ownText = String(element.innerText || element.textContent || '').replace(/\s+/g, ' ').trim();
      const identity = element.id || element.getAttribute('name') || '';
      if (anchor && (element.target && element.target !== '_self' || element.hasAttribute('download'))) return;
      globalThis.__JOBS_ASSISTANT_REVIEW_PERMIT__ = {
        href, action, method, origin: location.origin, kind: anchor ? 'a' : 'button',
        identity, text: ownText, detail: event.detail, trusted: true, expires: Date.now() + 1000,
      };
    }, true);
  });
}
function groupPresent(identity) {
  if (!identity || !Number.isSafeInteger(identity.pgid) || identity.pgid <= 0) return true;
  try { process.kill(-identity.pgid, 0); return true; } catch (error) { return error?.code === 'EPERM'; }
}
async function waitGroupAbsent(identity, timeoutMs = 5000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (!groupPresent(identity)) return true;
    await new Promise(resolve => setTimeout(resolve, 50));
  }
  return !groupPresent(identity);
}
function removeStagedInputSafely(root, candidate) {
  if (!root || !candidate || path.basename(candidate) !== candidate) return false;
  let rootFd = null; let inputFd = null;
  try {
    const rootPathStat = fs.lstatSync(root);
    if (!rootPathStat.isDirectory() || rootPathStat.isSymbolicLink()) return false;
    const rootReal = fs.realpathSync(root);
    const rootRealStat = fs.lstatSync(rootReal);
    if (rootRealStat.dev !== rootPathStat.dev || rootRealStat.ino !== rootPathStat.ino) return false;
    rootFd = fs.openSync(rootReal, fs.constants.O_RDONLY | (fs.constants.O_DIRECTORY || 0) | (fs.constants.O_NOFOLLOW || 0));
    const rootOpenStat = fs.fstatSync(rootFd);
    if (rootOpenStat.dev !== rootRealStat.dev || rootOpenStat.ino !== rootRealStat.ino) return false;
    const stagedPath = path.join(rootReal, candidate);
    const before = fs.lstatSync(stagedPath);
    if (!before.isFile() || before.isSymbolicLink()) return false;
    inputFd = fs.openSync(stagedPath, fs.constants.O_RDONLY | (fs.constants.O_NOFOLLOW || 0));
    const bytes = fs.readFileSync(inputFd);
    const expected = process.env.JOBS_ASSISTANT_STAGED_INPUT_SHA256 || '';
    if (!/^[0-9a-f]{64}$/.test(expected) || hash(bytes) !== expected) return false;
    const opened = fs.fstatSync(inputFd);
    const currentRoot = fs.fstatSync(rootFd);
    const currentRootPath = fs.lstatSync(rootReal);
    const currentPath = fs.lstatSync(stagedPath);
    if (currentRoot.dev !== rootOpenStat.dev || currentRoot.ino !== rootOpenStat.ino
        || currentRootPath.dev !== rootOpenStat.dev || currentRootPath.ino !== rootOpenStat.ino
        || currentPath.dev !== before.dev || currentPath.ino !== before.ino
        || opened.dev !== before.dev || opened.ino !== before.ino || opened.size !== before.size) return false;
    fs.unlinkSync(stagedPath);
    return !fs.existsSync(stagedPath);
  } catch { return false; }
  finally {
    if (inputFd !== null) fs.closeSync(inputFd);
    if (rootFd !== null) fs.closeSync(rootFd);
  }
}
async function releaseHandoff() {
  if (reviewState !== 'open_guarded') throw new Error('handoff_state_conflict');
  // The owner is now independent of its command transport.  Stop consuming
  // the parent's pipe before it can reach EOF; an EOF/SIGTERM from a
  // short-lived helper must not be interpreted as a browser close.
  detachedOwner = true;
  process.stdout.on('error', () => {});
  process.stderr.on('error', () => {});
  process.stdin.removeAllListeners('data');
  process.stdin.removeAllListeners('end');
  heartbeatTimer?.ref?.();
  manifestWrite('open_guarded', { detached: true });
  return { state: reviewState, released: true };
}
async function close(trigger = 'unknown') {
  if (reviewState === 'open_guarded' && !detachedCloseRequested) return;
  if (cleanupPromise) return cleanupPromise;
  cleanupTrigger = cleanupTrigger || trigger;
  cleanupStarted = true;
  clearInterval(budgetTimer); budgetTimer = null;
  cleanupPromise = (async () => {
    closeRequested = true;
    clearInterval(heartbeatTimer); heartbeatTimer = null;
    reviewPermit = null; reviewLedger = new Map(); reviewEpoch = null; proxyPermitUrls.clear();
    const browserProcess = browser?.process?.() || null;
    freezeProxy();
    if (browser) await browser.close().catch(() => {});
    browser = null; page = null;
    const browserGone = !browserIdentity || await waitGroupAbsent(browserIdentity, 5000);
    if (browserProcess?.pid && !browserGone) {
      terminalReason = terminalReason || 'browser_error';
    }
    if (proxy) await new Promise(resolve => proxy.close(() => resolve())).catch(() => {});
    proxy = null;
    for (const socket of proxySockets) socket.destroy();
    proxySockets.clear();
    let stagedRemoved = true;
    const inputRoot = process.env.JOBS_ASSISTANT_INPUT_ROOT;
    const candidate = process.env.JOBS_ASSISTANT_STAGED_INPUT_NAME;
    if (browserGone && inputRoot && candidate) {
      stagedRemoved = removeStagedInputSafely(inputRoot, candidate);
    } else if (inputRoot || candidate) {
      stagedRemoved = false;
    }
    const cleanupProof = browserGone && stagedRemoved;
    if (cleanupProof) {
      if (userDataDir) fs.rmSync(userDataDir, { recursive: true, force: true });
      if (ownerRoot) fs.rmSync(ownerRoot, { recursive: true, force: true });
      reviewState = 'closed';
      manifestWrite('closed', { cleanup: true, staged_input_removed: stagedRemoved, terminal_reason: terminalReason, detached: detachedOwner, cleanup_trigger: cleanupTrigger, browser_exit: browserExit });
    } else {
      reviewState = 'failed';
      manifestWrite('failed', { cleanup: false, browser_group_absent: browserGone, staged_input_removed: stagedRemoved, terminal_reason: terminalReason, detached: detachedOwner, cleanup_trigger: cleanupTrigger, browser_exit: browserExit });
    }
    userDataDir = null; ownerRoot = null;
  })();
  return cleanupPromise;
}
async function settle() {
  if (!page || page.isClosed()) throw new Error('page_not_stable');
  const realm = page.mainFrame().isolatedRealm();
  try {
    await realm.evaluate(() => new Promise(resolve => {
      const raf = globalThis.requestAnimationFrame;
      if (typeof raf !== 'function') return resolve();
      raf(() => raf(() => resolve()));
    }));
  } catch { throw new Error('page_not_stable'); }
  const deadline = Date.now() + 2000;
  while (Date.now() < deadline) {
    let mutationAt;
    try {
      mutationAt = await realm.evaluate(() => globalThis.__JOBS_ASSISTANT_QUIET_STATE__?.state?.lastMutation ?? Date.now());
    } catch { throw new Error('page_not_stable'); }
    if (pendingNetwork === 0 && Date.now() - mutationAt >= 250) return;
    await new Promise(resolve => setTimeout(resolve, 25));
  }
  throw new Error('page_not_stable');
}
async function handle(command) {
  switch (command.action) {
    case 'preflight': return preflight();
    case 'startup_identity': return startupIdentityCommand(command);
    case 'register_browser_identity': return registerBrowserIdentity(command);
    case 'resolvePinnedAddress': return resolvePinnedAddress(command.hostname);
    case 'launch': await launch(command); return { owner_pid: process.pid, browser_pid: browser?.process()?.pid || null, pipe: true };
    case 'goto': return goto(command);
    case 'observe': return observe();
    case 'fill': case 'select': case 'check': case 'upload': case 'click': return action(command.action === 'click' ? { ...command, action: 'click' } : command);
    case 'screenshot': return screenshot(command);
    case 'webrtcStatus': return webRtcStatus();
    case 'prepare_handoff': return prepareHandoff(command);
    case 'commit_handoff': return commitHandoff(command);
    case 'release_handoff': return releaseHandoff();
    case 'classifyResolverResult': return classifyResolverResult(command.addresses);
    case 'networkCounters': return { ...networkCounters, terminal_reason: terminalReason, review_state: reviewState };
    case 'test_proxy_setup': return testProxySetup(command);
    case 'test_proxy_freeze': return testProxyFreeze(command);
    case 'close': await close(); return {};
    default: throw new Error(`unknown_action:${String(command.action || '')}`);
  }
}
function parseFrames() {
  while (inputBuffer.length) {
    const newline = inputBuffer.indexOf(0x0a);
    if (newline < 0) { if (inputBuffer.length > MAX_IN_FRAME) throw new Error('input_frame_too_large'); return; }
    const prefix = inputBuffer.slice(0, newline).toString('ascii');
    if (!/^\d+$/.test(prefix)) throw new Error('invalid_frame_prefix');
    const length = Number(prefix); if (!Number.isSafeInteger(length) || length > MAX_IN_FRAME) throw new Error('input_frame_too_large');
    if (inputBuffer.length < newline + 1 + length) { if (inputBuffer.length > newline + 1 + MAX_IN_FRAME) throw new Error('input_frame_too_large'); return; }
    const body = inputBuffer.slice(newline + 1, newline + 1 + length); inputBuffer = inputBuffer.slice(newline + 1 + length);
    let command; try { command = JSON.parse(body.toString('utf8')); } catch { throw new Error('invalid_json_frame'); }
    if (!command || typeof command !== 'object' || Array.isArray(command)) throw new Error('invalid_command_frame');
    enqueue(command);
  }
}
function enqueue(command) {
  commandQueue = commandQueue.then(async () => { try { const data = await handle(command); await send(data); if (command.action === 'close') process.exit(0); } catch (error) { await fail(error, command.action); } });
}

async function runSmoke() {
  const smokeToken = crypto.randomBytes(16).toString('hex');
  process.env.JOBS_ASSISTANT_INTERNAL_TRANSPORT_TOKEN = smokeToken;
  let attackerHttpRequests = 0;
  const server = http.createServer((req, res) => { if (/attacker|exfil/i.test(req.url || '') || isFinalLike(req.url || '')) { attackerHttpRequests += 1; res.statusCode = 500; res.end('blocked'); return; } res.setHeader('content-type', 'text/html'); res.end('<!doctype html><form><label>First Name <input name="first_name" required></label><button type="submit">Submit Application</button><button type="button" id="offline">Continue</button><script>window.addEventListener("input",()=>{fetch("/exfil-attacker").catch(()=>{});const image=new Image();image.src="http://attacker.invalid/leak"});</script></form>'); });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  try { await preflight(); await launch({ headless: true }); internalTransportUrl = `http://127.0.0.1:${server.address().port}/fixture`; await goto({ url: 'https://boards.greenhouse.io/fixture/jobs/123', internal_url: internalTransportUrl, internal_token: smokeToken }); const observed = await observe(); const field = observed.fields.find(item => item.name === 'first_name'); if (!field) throw new Error('smoke_field_missing'); if (!observed.final_submit_target_ids.length) throw new Error('smoke_final_marker_missing'); await action({ action: 'fill', target_id: field.target_id, value: 'Ada' }); await settle(); const counters = { ...networkCounters, attackerHttpRequests }; if (counters.attackerDnsLookups !== 0 || counters.attackerHttpRequests !== 0) throw new Error('smoke_attacker_counter_nonzero'); await close(); await send({ smoke: true, counters, cleanup: !userDataDir }); } finally { server.close(); await close(); }
}

async function runErrorCodeSelfTest() {
  const cases = [
    [{ name: 'TimeoutError', message: 'launch timeout' }, 'launch', 'browser_launch_timeout'],
    [{ name: 'Error', message: 'native launch failure' }, 'launch', 'browser_launch_failed'],
    [{ name: 'TimeoutError', message: 'navigation timeout' }, 'goto', 'navigation_timeout'],
    [{ name: 'Error', message: 'net::ERR_NAME_NOT_RESOLVED' }, 'goto', 'navigation_dns_failed'],
    [{ name: 'Error', message: 'net::ERR_CONNECTION_REFUSED' }, 'goto', 'navigation_connection_failed'],
    [{ name: 'Error', message: 'net::ERR_CERT_AUTHORITY_INVALID' }, 'goto', 'navigation_tls_failed'],
    [{ name: 'Error', message: 'native navigation failure' }, 'goto', 'browser_command_failed'],
    [{ name: 'TimeoutError', message: 'observation timeout' }, 'observe', 'observation_timeout'],
    [{ code: 'navigation_timeout', message: 'ignored native detail' }, 'goto', 'navigation_timeout'],
    [{ message: 'unknown_snake_case native detail' }, 'observe', 'browser_command_failed'],
  ];
  for (const [error, action, expected] of cases) {
    if (classifyNativeBrowserError(error, action) !== expected || safeErrorCode(error, action) !== expected) {
      throw new Error('error_code_self_test_failed');
    }
  }
  return { passed: cases.length };
}

function runRequestGuardSelfTest() {
  selectRoutePolicy('greenhouse');
  documentRouteIdentity = { mode: 'greenhouse_job', host: 'boards.greenhouse.io', board: 'acme', job: '123' };
  const vectors = [
    [validateOtherRequest('https://boards.greenhouse.io/acme/jobs/123'), true],
    [validateOtherRequest('https://job-boards.cdn.greenhouse.io/assets/application.js'), true],
    [validateOtherRequest('https://boards.greenhouse.io/unknown/path'), false],
    [validateOtherRequest('https://attacker.invalid/collect'), false],
  ];
  selectRoutePolicy('lever');
  documentRouteIdentity = { mode: 'lever_job', host: 'jobs.lever.co', account: 'example', job: '123e4567-e89b-12d3-a456-426614174000' };
  if (!sameBoardJobUrl(
    'https://jobs.lever.co/example/123e4567-e89b-12d3-a456-426614174000/apply',
    documentRouteIdentity,
  ) || sameBoardJobUrl(
    'https://boards.greenhouse.io/example/jobs/123',
    documentRouteIdentity,
  )) throw new Error('request_guard_self_test_failed');
  documentRouteIdentity = null;
  if (vectors.some(([actual, expected]) => actual !== expected)) throw new Error('request_guard_self_test_failed');
  return { passed: vectors.length + 2 };
}
if (process.argv.includes('--error-code-self-test')) runErrorCodeSelfTest().then(send).then(() => process.exit(0)).catch(async error => { await fail(error, 'protocol'); process.exit(1); });
else if (process.argv.includes('--request-guard-self-test')) Promise.resolve(runRequestGuardSelfTest()).then(send).then(() => process.exit(0)).catch(async error => { await fail(error, 'protocol'); process.exit(1); });
else if (process.argv.includes('--preflight')) preflight().then(send).then(() => process.exit(0)).catch(async error => { await fail(error, 'preflight'); process.exit(1); });
else if (process.argv.includes('--smoke')) runSmoke().then(() => process.exit(0)).catch(async error => { await fail(error, 'smoke'); process.exit(1); });
else {
  void writeFrame({ ok: true, data: { ready: true, protocol: 'length-prefixed-json-v1', owner_pid: process.pid, identity: process.env.JOBS_ASSISTANT_HANDSHAKE || null } });
  process.stdin.on('data', chunk => { try { inputBuffer = Buffer.concat([inputBuffer, chunk]); if (inputBuffer.length > MAX_IN_FRAME + 32) throw new Error('input_frame_too_large'); parseFrames(); } catch (error) { void fail(error, 'protocol').finally(() => process.exit(1)); } });
  process.stdin.on('end', () => {
    commandQueue.then(async () => {
      if (reviewState === 'open_guarded') {
        manifestWrite('open_guarded', { detached: true });
        return;
      }
      await close();
      process.exit(0);
    });
  });
}
function handleOwnerSignal() {
  if (reviewState === 'open_guarded') return;
  void close().finally(() => process.exit(0));
}
for (const signal of ['SIGTERM', 'SIGHUP', 'SIGINT', 'SIGQUIT']) process.on(signal, handleOwnerSignal);
