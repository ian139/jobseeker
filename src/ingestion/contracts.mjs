import crypto from 'node:crypto';
import path from 'node:path';

export const NORMALIZED_JOB_SCHEMA = 'normalized-job-v1';
export const SOURCE_SYNC_RESULT_SCHEMA = 'source-sync-result-v1';

export const ATS_KINDS = Object.freeze(['greenhouse', 'ashby', 'lever', 'workday', 'linkedin', 'custom', 'unknown']);
export const WORKPLACE_TYPES = Object.freeze(['remote', 'hybrid', 'onsite', 'unknown']);
export const AVAILABILITY_STATES = Object.freeze(['open', 'closed', 'unknown']);
export const FRESHNESS_STATES = Object.freeze(['current', 'stale', 'unverified']);
export const ELIGIBILITY_STATES = Object.freeze(['eligible', 'ineligible', 'review']);
export const DEDUPE_IDENTITY_KINDS = Object.freeze(['source', 'ats', 'application_url', 'review_fingerprint']);
export const SYNC_MODES = Object.freeze(['preview', 'paid']);
export const SYNC_STATES = Object.freeze(['previewed', 'succeeded', 'failed', 'paid_ambiguous']);
export const FAILURE_CLASSES = Object.freeze(['retryable', 'terminal', 'paid_ambiguous', 'authentication', 'account_health']);

const NORMALIZED_KEYS = Object.freeze([
  'schema',
  'source',
  'sourceJobId',
  'canonicalListingUrl',
  'canonicalApplicationUrl',
  'atsKind',
  'atsIdentifier',
  'title',
  'company',
  'location',
  'workplaceType',
  'employmentTypes',
  'description',
  'descriptionSha256',
  'sourcePostedAt',
  'sourceUpdatedAt',
  'discoveredAt',
  'availabilityState',
  'freshnessState',
  'eligibilityState',
  'eligibilityReasonCodes',
  'priority',
  'dedupeIdentityKind',
  'dedupeIdentityKey',
  'dedupeReviewRequired',
  'rawPayloadPath',
  'rawPayloadSha256',
]);

const SOURCE_RESULT_KEYS = Object.freeze([
  'schema',
  'syncRunId',
  'source',
  'profile',
  'mode',
  'state',
  'startedAt',
  'finishedAt',
  'checkpointBefore',
  'checkpointAfter',
  'pagesFetched',
  'requestCount',
  'jobsSeen',
  'jobsInserted',
  'jobsUpdated',
  'jobsUnchanged',
  'dedupeGroupsTouched',
  'queueRowsInserted',
  'estimatedCredits',
  'reportedCredits',
  'failureClass',
  'reasonCode',
]);

const NORMALIZED_KEY_SET = new Set(NORMALIZED_KEYS);
const SOURCE_RESULT_KEY_SET = new Set(SOURCE_RESULT_KEYS);
const ATS_SET = new Set(ATS_KINDS);
const WORKPLACE_SET = new Set(WORKPLACE_TYPES);
const AVAILABILITY_SET = new Set(AVAILABILITY_STATES);
const FRESHNESS_SET = new Set(FRESHNESS_STATES);
const ELIGIBILITY_SET = new Set(ELIGIBILITY_STATES);
const DEDUPE_KIND_SET = new Set(DEDUPE_IDENTITY_KINDS);
const SYNC_MODE_SET = new Set(SYNC_MODES);
const SYNC_STATE_SET = new Set(SYNC_STATES);
const FAILURE_CLASS_SET = new Set(FAILURE_CLASSES);
const SHA256_RE = /^[0-9a-f]{64}$/u;
const ISO_RE = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$/u;
const SOURCE_RE = /^[a-z][a-z0-9_-]{0,63}$/u;
const PROFILE_RE = /^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$/u;
const REASON_RE = /^[a-z][a-z0-9_]{0,63}$/u;
const EMPLOYMENT_RE = /^[a-z][a-z0-9_-]{0,63}$/u;
const SAFE_COMPONENT_RE = /^[^/\\\u0000]+$/u;
const TRACKING_PARAM_RE = /^(?:utm_[a-z0-9_]+|fbclid|gclid|dclid|msclkid|mc_cid|mc_eid|_hsenc|_hsmi|hsa_[a-z0-9_]+|vero_id|wickedid|yclid|gbraid|wbraid)$/iu;
const MAX_PRIORITY = 1000;
const MAX_COUNT = 2 ** 31 - 1;

export class IngestionValidationError extends Error {
  constructor(code, location = '') {
    const suffix = location ? `:${location}` : '';
    super(`${code}${suffix}`);
    this.name = 'IngestionValidationError';
    this.code = code;
    this.location = location;
  }
}


function fail(code, location = '') {
  throw new IngestionValidationError(code, location);
}

function isPlainObject(value) {
  if (value === null || typeof value !== 'object' || Array.isArray(value)) return false;
  const prototype = Object.getPrototypeOf(value);
  return prototype === Object.prototype || prototype === null;
}

function requireObject(value, location) {
  if (!isPlainObject(value)) fail('E_SCHEMA_OBJECT', location);
  return value;
}

function rejectUnknownKeys(value, allowed, location) {
  const ownKeys = Reflect.ownKeys(value);
  if (ownKeys.length !== allowed.size || ownKeys.some((key) => typeof key !== 'string' || !allowed.has(key))) {
    const unknown = ownKeys.find((key) => typeof key !== 'string' || !allowed.has(key));
    fail('E_SCHEMA_UNKNOWN_KEY', `${location}.${String(unknown ?? 'unknown')}`);
  }
  for (const key of allowed) {
    const descriptor = Object.getOwnPropertyDescriptor(value, key);
    if (!descriptor || descriptor.enumerable !== true || !Object.hasOwn(descriptor, 'value')) {
      fail('E_SCHEMA_PROPERTY', `${location}.${key}`);
    }
  }
}

function requireString(value, location, { max = 8192, nonEmpty = true, pattern = null } = {}) {
  if (typeof value !== 'string' || value.includes('\u0000') || value.length > max || (nonEmpty && value.length === 0)) {
    fail('E_SCHEMA_STRING', location);
  }
  if (pattern && !pattern.test(value)) fail('E_SCHEMA_STRING', location);
  return value;
}

function requireNullableString(value, location, options = {}) {
  if (value === null) return null;
  return requireString(value, location, options);
}

function requireEnum(value, values, location) {
  if (typeof value !== 'string' || !values.has(value)) fail('E_SCHEMA_ENUM', location);
  return value;
}

function requireInteger(value, location, { min = 0, max = MAX_COUNT } = {}) {
  if (!Number.isSafeInteger(value) || value < min || value > max) fail('E_SCHEMA_INTEGER', location);
  return value;
}

function requireBoolean(value, location) {
  if (typeof value !== 'boolean') fail('E_SCHEMA_BOOLEAN', location);
  return value;
}

function requireTimestamp(value, location, { nullable = false } = {}) {
  if (nullable && value === null) return null;
  requireString(value, location, { max: 24, pattern: ISO_RE });
  const parsed = Date.parse(value);
  if (!Number.isFinite(parsed) || new Date(parsed).toISOString() !== value) fail('E_TIMESTAMP', location);
  return value;
}

function requireDigest(value, location, { nullable = false } = {}) {
  if (nullable && value === null) return null;
  requireString(value, location, { max: 64, pattern: SHA256_RE });
  return value;
}

function requireArray(value, location, { item, unique = false, sorted = false, max = 128 } = {}) {
  if (!Array.isArray(value) || value.length > max) fail('E_SCHEMA_ARRAY', location);
  const result = [];
  for (let index = 0; index < value.length; index += 1) result.push(item(value[index], `${location}[${index}]`));
  if (unique && new Set(result).size !== result.length) fail('E_SCHEMA_ARRAY', location);
  if (sorted && result.some((entry, index) => index > 0 && result[index - 1] >= entry)) fail('E_SCHEMA_ARRAY', location);
  return result;
}

function clone(value) {
  let copied;
  try {
    copied = structuredClone(value);
  } catch {
    fail('E_SCHEMA_CLONE');
  }
  return deepFreeze(copied);
}

function deepFreeze(value, seen = new Set()) {
  if (value === null || typeof value !== 'object' || seen.has(value)) return value;
  seen.add(value);
  for (const nested of Object.values(value)) deepFreeze(nested, seen);
  return Object.freeze(value);
}

function cloneAndFreeze(value) {
  // Validate before cloning so accessors, prototypes, and cycles are not silently accepted.
  return clone(value);
}

function normalizeTimestamp(value, location = 'timestamp') {
  if (value instanceof Date && Number.isFinite(value.getTime())) return value.toISOString();
  if (typeof value !== 'string' || value.length === 0 || value.includes('\u0000')) fail('E_TIMESTAMP', location);
  const parsed = Date.parse(value);
  if (!Number.isFinite(parsed)) fail('E_TIMESTAMP', location);
  return new Date(parsed).toISOString();
}

function normalizeOptionalTimestamp(value, location) {
  if (value === null || value === undefined || value === '') return null;
  return normalizeTimestamp(value, location);
}

function normalizeText(value, location, { max = 8192, nullable = false } = {}) {
  if (value === null || value === undefined) {
    if (nullable) return null;
    fail('E_SCHEMA_STRING', location);
  }
  if (typeof value !== 'string' || value.includes('\u0000') || value.length > max) fail('E_SCHEMA_STRING', location);
  const normalized = value.replaceAll('\r\n', '\n').replaceAll('\r', '\n').trim();
  if (!normalized && !nullable) fail('E_SCHEMA_STRING', location);
  return normalized;
}

function validateSafePayloadPath(value, location) {
  requireString(value, location, { max: 4096 });
  if (!path.isAbsolute(value) || value.includes('\\') || value.endsWith('/') || path.normalize(value) !== value) fail('E_UNSAFE_PATH', location);
  const parts = value.split('/').slice(1);
  if (parts.some((part) => part === '' || part === '.' || part === '..' || !SAFE_COMPONENT_RE.test(part))) fail('E_UNSAFE_PATH', location);
  return value;
}

function validateNormalizedShape(input, location = 'job') {
  const value = requireObject(input, location);
  rejectUnknownKeys(value, NORMALIZED_KEY_SET, location);
  const required = NORMALIZED_KEYS;
  for (const key of required) if (!Object.hasOwn(value, key)) fail('E_SCHEMA_REQUIRED', `${location}.${key}`);
  if (value.schema !== NORMALIZED_JOB_SCHEMA) fail('E_SCHEMA_VERSION', `${location}.schema`);
  requireString(value.source, `${location}.source`, { max: 64, pattern: SOURCE_RE });
  requireNullableString(value.sourceJobId, `${location}.sourceJobId`, { max: 512 });
  for (const key of ['canonicalListingUrl', 'canonicalApplicationUrl']) {
    const url = value[key];
    if (url !== null) {
      requireString(url, `${location}.${key}`, { max: 4096 });
      if (canonicalizeJobUrl(url) !== url) fail('E_NONCANONICAL_URL', `${location}.${key}`);
    }
  }
  requireEnum(value.atsKind, ATS_SET, `${location}.atsKind`);
  requireNullableString(value.atsIdentifier, `${location}.atsIdentifier`, { max: 512 });
  requireString(value.title, `${location}.title`, { max: 1024 });
  requireString(value.company, `${location}.company`, { max: 1024 });
  requireNullableString(value.location, `${location}.location`, { max: 2048 });
  requireEnum(value.workplaceType, WORKPLACE_SET, `${location}.workplaceType`);
  requireArray(value.employmentTypes, `${location}.employmentTypes`, {
    item: (entry, itemLocation) => requireString(entry, itemLocation, { max: 64, pattern: EMPLOYMENT_RE }),
    unique: true,
    sorted: true,
    max: 32,
  });
  requireString(value.description, `${location}.description`, { max: 1_000_000, nonEmpty: false });
  requireDigest(value.descriptionSha256, `${location}.descriptionSha256`);
  if (sha256Text(value.description) !== value.descriptionSha256) fail('E_DESCRIPTION_DIGEST', `${location}.descriptionSha256`);
  requireTimestamp(value.sourcePostedAt, `${location}.sourcePostedAt`, { nullable: true });
  requireTimestamp(value.sourceUpdatedAt, `${location}.sourceUpdatedAt`, { nullable: true });
  requireTimestamp(value.discoveredAt, `${location}.discoveredAt`);
  requireEnum(value.availabilityState, AVAILABILITY_SET, `${location}.availabilityState`);
  requireEnum(value.freshnessState, FRESHNESS_SET, `${location}.freshnessState`);
  requireEnum(value.eligibilityState, ELIGIBILITY_SET, `${location}.eligibilityState`);
  requireArray(value.eligibilityReasonCodes, `${location}.eligibilityReasonCodes`, {
    item: (entry, itemLocation) => requireString(entry, itemLocation, { max: 64, pattern: REASON_RE }),
    unique: true,
    sorted: true,
    max: 32,
  });
  requireInteger(value.priority, `${location}.priority`, { min: 0, max: MAX_PRIORITY });
  requireEnum(value.dedupeIdentityKind, DEDUPE_KIND_SET, `${location}.dedupeIdentityKind`);
  requireString(value.dedupeIdentityKey, `${location}.dedupeIdentityKey`, { max: 1024 });
  requireBoolean(value.dedupeReviewRequired, `${location}.dedupeReviewRequired`);
  validateSafePayloadPath(value.rawPayloadPath, `${location}.rawPayloadPath`);
  requireDigest(value.rawPayloadSha256, `${location}.rawPayloadSha256`);
  return value;
}

export function validateNormalizedJob(input) {
  const value = validateNormalizedShape(input);
  return cloneAndFreeze(value);
}

function validateSourceResultShape(input, location = 'result') {
  const value = requireObject(input, location);
  rejectUnknownKeys(value, SOURCE_RESULT_KEY_SET, location);
  for (const key of SOURCE_RESULT_KEYS) if (!Object.hasOwn(value, key)) fail('E_SCHEMA_REQUIRED', `${location}.${key}`);
  if (value.schema !== SOURCE_SYNC_RESULT_SCHEMA) fail('E_SCHEMA_VERSION', `${location}.schema`);
  if (value.syncRunId !== null) requireString(value.syncRunId, `${location}.syncRunId`, { max: 128 });
  requireString(value.source, `${location}.source`, { max: 64, pattern: SOURCE_RE });
  requireString(value.profile, `${location}.profile`, { max: 128, pattern: PROFILE_RE });
  requireEnum(value.mode, SYNC_MODE_SET, `${location}.mode`);
  requireEnum(value.state, SYNC_STATE_SET, `${location}.state`);
  requireTimestamp(value.startedAt, `${location}.startedAt`);
  requireTimestamp(value.finishedAt, `${location}.finishedAt`, { nullable: true });
  requireNullableString(value.checkpointBefore, `${location}.checkpointBefore`, { max: 512 });
  requireNullableString(value.checkpointAfter, `${location}.checkpointAfter`, { max: 512 });
  if (value.mode === 'preview' && value.syncRunId !== null) fail('E_PREVIEW_RUN_ID', `${location}.syncRunId`);
  if (value.mode === 'paid' && value.syncRunId === null) fail('E_PAID_RUN_ID', `${location}.syncRunId`);
  if (value.state === 'previewed' && value.mode !== 'preview') fail('E_SYNC_STATE', `${location}.state`);
  if (value.state !== 'previewed' && value.mode !== 'paid') fail('E_SYNC_STATE', `${location}.state`);
  if (value.state === 'succeeded' && value.finishedAt === null) fail('E_SYNC_FINISHED', `${location}.finishedAt`);
  for (const key of ['pagesFetched', 'requestCount', 'jobsSeen', 'jobsInserted', 'jobsUpdated', 'jobsUnchanged', 'dedupeGroupsTouched', 'queueRowsInserted']) {
    requireInteger(value[key], `${location}.${key}`);
  }
  if (value.estimatedCredits !== null) {
    if (typeof value.estimatedCredits !== 'number' || !Number.isFinite(value.estimatedCredits) || value.estimatedCredits < 0 || value.estimatedCredits > MAX_COUNT) fail('E_CREDITS', `${location}.estimatedCredits`);
  }
  if (value.reportedCredits !== null) {
    if (typeof value.reportedCredits !== 'number' || !Number.isFinite(value.reportedCredits) || value.reportedCredits < 0 || value.reportedCredits > MAX_COUNT) fail('E_CREDITS', `${location}.reportedCredits`);
  }
  if (value.failureClass !== null) requireEnum(value.failureClass, FAILURE_CLASS_SET, `${location}.failureClass`);
  requireNullableString(value.reasonCode, `${location}.reasonCode`, { max: 256, pattern: REASON_RE });
  if (value.state === 'failed' || value.state === 'paid_ambiguous') {
    if (value.failureClass === null || value.reasonCode === null) fail('E_SYNC_FAILURE_DETAIL', location);
  } else if (value.failureClass !== null || value.reasonCode !== null) {
    fail('E_SYNC_FAILURE_DETAIL', location);
  }
  return value;
}

export function validateSourceSyncResult(input) {
  return cloneAndFreeze(validateSourceResultShape(input));
}

function canonicalUrlInput(value) {
  requireString(value, 'url', { max: 8192 });
  if (value.trim() !== value || /[\u0000-\u0020\u007f]/u.test(value)) fail('E_URL', 'url');
  let parsed;
  try {
    parsed = new URL(value);
  } catch {
    fail('E_URL', 'url');
  }
  if (parsed.protocol !== 'http:' && parsed.protocol !== 'https:') fail('E_URL_SCHEME', 'url');
  if (parsed.username || parsed.password) fail('E_URL_CREDENTIALS', 'url');
  if (!parsed.hostname || parsed.hostname.includes('..')) fail('E_URL_HOST', 'url');
  return parsed;
}

export function canonicalizeJobUrl(value) {
  const parsed = canonicalUrlInput(value);
  parsed.protocol = parsed.protocol.toLowerCase();
  parsed.hostname = parsed.hostname.toLowerCase();
  if ((parsed.protocol === 'http:' && parsed.port === '80') || (parsed.protocol === 'https:' && parsed.port === '443')) parsed.port = '';
  parsed.hash = '';
  const pathname = parsed.pathname || '/';
  parsed.pathname = pathname === '/' ? '/' : pathname.replace(/\/+$/u, '') || '/';
  const retained = [];
  let index = 0;
  for (const [key, paramValue] of parsed.searchParams.entries()) {
    if (!TRACKING_PARAM_RE.test(key)) retained.push([key, paramValue, index]);
    index += 1;
  }
  retained.sort((left, right) => (left[0] === right[0] ? 0 : left[0] < right[0] ? -1 : 1) || (left[1] === right[1] ? 0 : left[1] < right[1] ? -1 : 1) || left[2] - right[2]);
  parsed.search = '';
  for (const [key, paramValue] of retained) parsed.searchParams.append(key, paramValue);
  return parsed.toString();
}

function hostMatches(hostname, exact, suffix) {
  return hostname === exact || (suffix && hostname.endsWith(`.${suffix}`));
}

function atsFromUrl(value) {
  if (typeof value !== 'string' || value.length === 0) return { kind: 'unknown', identifier: null };
  let parsed;
  try {
    parsed = new URL(value);
  } catch {
    return { kind: 'unknown', identifier: null };
  }
  const host = parsed.hostname.toLowerCase();
  const parts = parsed.pathname.split('/').filter(Boolean);
  if (hostMatches(host, 'boards.greenhouse.io', 'greenhouse.io')) return { kind: 'greenhouse', identifier: parts[0] ?? null };
  if (hostMatches(host, 'jobs.ashbyhq.com', 'ashbyhq.com')) return { kind: 'ashby', identifier: parts[0] ?? null };
  if (hostMatches(host, 'jobs.lever.co', 'lever.co') || hostMatches(host, 'jobs.eu.lever.co', null)) return { kind: 'lever', identifier: parts[0] ?? null };
  if (host === 'myworkdayjobs.com' || host.endsWith('.myworkdayjobs.com') || host === 'workdayjobs.com' || host.endsWith('.workdayjobs.com')) return { kind: 'workday', identifier: parts[0] ?? host };
  if (host === 'linkedin.com' || host.endsWith('.linkedin.com')) {
    const jobPart = parts.find((part) => /^\d{5,}$/u.test(part)) ?? parts.at(-1) ?? null;
    return { kind: 'linkedin', identifier: jobPart };
  }
  return { kind: 'unknown', identifier: null };
}

export function classifyAts(canonicalApplicationUrl) {
  if (canonicalApplicationUrl === null) return Object.freeze({ kind: 'unknown', identifier: null });
  requireString(canonicalApplicationUrl, 'canonicalApplicationUrl', { max: 4096 });
  if (canonicalizeJobUrl(canonicalApplicationUrl) !== canonicalApplicationUrl) fail('E_NONCANONICAL_URL', 'canonicalApplicationUrl');
  return Object.freeze(atsFromUrl(canonicalApplicationUrl));
}

function normalizeIdentityText(value) {
  return String(value ?? '').normalize('NFKC').trim().replace(/\s+/gu, ' ').toLowerCase();
}

export function deriveDedupeIdentity(job) {
  const value = requireObject(job, 'job');
  const source = typeof value.source === 'string' ? value.source.trim().toLowerCase() : '';
  const sourceJobId = value.sourceJobId;
  let kind;
  let key;
  let reviewRequired = false;
  if (source && typeof sourceJobId === 'string' && sourceJobId.length > 0) {
    kind = 'source';
    key = `${source}:${sourceJobId}`;
  } else if (ATS_SET.has(value.atsKind) && value.atsKind !== 'unknown' && value.atsKind !== 'custom' && typeof value.atsIdentifier === 'string' && value.atsIdentifier.length > 0) {
    kind = 'ats';
    key = `${value.atsKind}:${value.atsIdentifier}`;
  } else if (typeof value.canonicalApplicationUrl === 'string' && value.canonicalApplicationUrl.length > 0) {
    kind = 'application_url';
    key = value.canonicalApplicationUrl;
  } else {
    kind = 'review_fingerprint';
    const fingerprint = {
      company: normalizeIdentityText(value.company),
      title: normalizeIdentityText(value.title),
      location: normalizeIdentityText(value.location),
    };
    key = sha256Canonical(fingerprint);
    reviewRequired = true;
  }
  return Object.freeze({ kind, key, reviewRequired });
}

function priorityFor(state, value, reasonCodes) {
  let score = state === 'eligible' ? 800 : state === 'review' ? 400 : 0;
  if (state !== 'ineligible') {
    if (value.sourceUpdatedAt || value.sourcePostedAt) score += 25;
    if (value.discoveredAt) score += 10;
    if (value.freshnessState === 'stale') score -= 100;
    if (value.freshnessState === 'unverified') score -= 50;
    if (reasonCodes.includes('missing_description')) score -= 75;
    if (reasonCodes.includes('unknown_ats') || reasonCodes.includes('custom_ats')) score -= 50;
  }
  return Math.max(0, Math.min(MAX_PRIORITY, Math.trunc(score)));
}

export function classifyEligibility(input) {
  const value = requireObject(input, 'job');
  const reasons = new Set();
  if (value.availabilityState === 'closed') reasons.add('closed');
  if (value.availabilityState === 'unknown' || value.availabilityState === undefined) reasons.add('unknown_availability');
  if (value.canonicalApplicationUrl === null || value.canonicalApplicationUrl === undefined || value.canonicalApplicationUrl === '') reasons.add('missing_application_url');
  if (value.freshnessState === 'stale') reasons.add('stale');
  if (value.freshnessState === 'unverified' || value.freshnessState === undefined) reasons.add('unverified_freshness');
  if (typeof value.description !== 'string' || value.description.trim() === '') reasons.add('missing_description');
  if (value.atsKind === 'unknown' || value.atsKind === undefined) reasons.add('unknown_ats');
  if (value.atsKind === 'custom') reasons.add('custom_ats');
  if (value.atsKind !== undefined && value.atsKind !== null && !ATS_SET.has(value.atsKind)) reasons.add('unsupported_ats');
  const eligibilityReasonCodes = [...reasons].sort();
  let eligibilityState = 'eligible';
  if (reasons.has('closed') || reasons.has('missing_application_url')) eligibilityState = 'ineligible';
  else if (eligibilityReasonCodes.length > 0) eligibilityState = 'review';
  const priority = priorityFor(eligibilityState, value, eligibilityReasonCodes);
  return Object.freeze({
    eligibilityState,
    eligibilityReasonCodes: Object.freeze(eligibilityReasonCodes),
    priority,
  });
}

function assertJsonValue(value, location = '$', seen = new Set()) {
  if (value === null || typeof value === 'string' || typeof value === 'boolean') {
    if (typeof value === 'string' && (value.includes('\u0000') || value.length > 1_000_000)) fail('E_JSON_VALUE', location);
    return;
  }
  if (typeof value === 'number') {
    if (!Number.isFinite(value)) fail('E_JSON_VALUE', location);
    return;
  }
  if (typeof value !== 'object' || seen.has(value)) fail('E_JSON_VALUE', location);
  seen.add(value);
  if (Array.isArray(value)) {
    if (value.length > 4096) fail('E_JSON_VALUE', location);
    for (let index = 0; index < value.length; index += 1) assertJsonValue(value[index], `${location}[${index}]`, seen);
  } else {
    if (!isPlainObject(value) || Reflect.ownKeys(value).some((key) => typeof key !== 'string')) fail('E_JSON_VALUE', location);
    for (const key of Object.keys(value)) {
      if (key.includes('\u0000')) fail('E_JSON_VALUE', `${location}.${key}`);
      assertJsonValue(value[key], `${location}.${key}`, seen);
    }
  }
  seen.delete(value);
}

function canonicalValue(value) {
  if (Array.isArray(value)) return value.map(canonicalValue);
  if (isPlainObject(value)) return Object.fromEntries(Object.keys(value).sort().map((key) => [key, canonicalValue(value[key])]));
  return value;
}

export function canonicalJson(value) {
  assertJsonValue(value);
  const encoded = JSON.stringify(canonicalValue(value));
  if (typeof encoded !== 'string') fail('E_JSON_VALUE');
  return encoded;
}

export function sha256Canonical(value) {
  return crypto.createHash('sha256').update(canonicalJson(value), 'utf8').digest('hex');
}
export function sha256Text(value) {
  requireString(value, 'text', { max: 1_000_000, nonEmpty: false });
  return crypto.createHash('sha256').update(value, 'utf8').digest('hex');
}

export const NORMALIZED_JOB_KEYS = NORMALIZED_KEYS;
export const SOURCE_SYNC_RESULT_KEYS = SOURCE_RESULT_KEYS;

export default Object.freeze({
  NORMALIZED_JOB_SCHEMA,
  SOURCE_SYNC_RESULT_SCHEMA,
  validateNormalizedJob,
  validateSourceSyncResult,
  canonicalizeJobUrl,
  classifyAts,
  deriveDedupeIdentity,
  classifyEligibility,
  canonicalJson,
  sha256Canonical,
  IngestionValidationError,
});
