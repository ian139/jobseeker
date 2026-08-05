import { createHash } from 'node:crypto';

import {
  canonicalJson,
  canonicalizeJobUrl,
  classifyAts,
  classifyEligibility,
  deriveDedupeIdentity,
  sha256Canonical,
  validateNormalizedJob,
} from './contracts.mjs';

export const THEIRSTACK_SOURCE = 'theirstack';
export const THEIRSTACK_SEARCH_PATH = '/v1/jobs/search';
export const THEIRSTACK_DEFAULT_BASE_URL = 'https://api.theirstack.com';
export const THEIRSTACK_DEFAULT_POSTED_MAX_AGE_DAYS = 7;
export const THEIRSTACK_DEFAULT_PAGE_LIMIT = 25;
export const THEIRSTACK_DEFAULT_TIMEOUT_MS = 10_000;
export const THEIRSTACK_DEFAULT_PREVIEW_RETRIES = 2;
export const THEIRSTACK_DEFAULT_MAX_RESPONSE_BYTES = 64 * 1024 * 1024;

export const THEIRSTACK_ERROR_CODES = Object.freeze({
  authentication: 'authentication',
  terminal_validation: 'terminal_validation',
  account_health: 'account_health',
  retryable_preview: 'retryable_preview',
  paid_ambiguous: 'paid_ambiguous',
  paid_authorization: 'paid_authorization',
});

export class TheirStackError extends Error {
  constructor(code, message = code) {
    super(message);
    this.name = 'TheirStackError';
    this.code = code;
  }
}

export const THEIRSTACK_PROFILE_NAMES = Object.freeze([
  'new_grad_cs',
  'new_grad_non_coop_cs',
  'fall_coop_swe_data',
  'default',
]);

const BAD_TITLE_MATCHES = Object.freeze([
  'senior',
  'sr.',
  'staff',
  'principal',
  'manager',
  'director',
  'lead',
  'architect',
  'recruiter',
  'sales',
  'account executive',
]);

const BAD_DESCRIPTION_PATTERNS = Object.freeze([
  '(?i)\\b(5|6|7|8|9|10)\\+?\\s+years?\\b',
  '(?i)\\bactive\\s+security\\s+clearance\\b',
  '(?i)\\bcommission[- ]only\\b',
]);

const CS_ROLE_TITLES = Object.freeze([
  'software engineer intern',
  'software developer intern',
  'backend engineer intern',
  'frontend engineer intern',
  'full stack engineer intern',
  'data scientist intern',
  'data engineer intern',
  'devops engineer intern',
  'site reliability engineer intern',
  'platform engineer intern',
  'machine learning engineer intern',
  'ai engineer intern',
  'software engineer',
  'software developer',
  'backend engineer',
  'frontend engineer',
  'full stack engineer',
  'data scientist',
  'data engineer',
  'devops engineer',
  'site reliability engineer',
  'platform engineer',
  'machine learning engineer',
  'ai engineer',
  'new grad software engineer',
  'new grad data scientist',
  'new grad data engineer',
  'entry level software engineer',
  'junior software engineer',
  'co-op software engineer',
  'co-op software developer',
  'co-op data scientist',
  'co-op data engineer',
]);

const EARLY_CAREER_PATTERNS = Object.freeze([
  '(?i)\\bco-op\\b',
  '(?i)\\bnew grad(uate)?s?\\b',
  '(?i)\\buniversity grad(uate)?s?\\b',
  '(?i)\\bearly career\\b',
  '(?i)\\bentry[- ]level\\b',
  '(?i)\\bintern(ship)?\\b',
  '(?i)\\bgraduate program\\b',
]);

const COOP_ROLE_TITLES = Object.freeze([
  'co-op software engineer',
  'co-op software developer',
  'co-op data scientist',
  'co-op data engineer',
]);

const COOP_DESCRIPTION_PATTERNS = Object.freeze(['(?i)\\bco-op\\b']);
const NON_COOP_ROLE_TITLES = Object.freeze(CS_ROLE_TITLES.filter((title) => !title.includes('co-op')));
const NON_COOP_EARLY_CAREER_PATTERNS = Object.freeze(
  EARLY_CAREER_PATTERNS.filter((pattern) => !pattern.includes('co-op')),
);

const ORDER_BY = Object.freeze([
  Object.freeze({ field: 'date_posted', desc: true }),
  Object.freeze({ field: 'discovered_at', desc: true }),
]);

const QUERY_FILTER_KEYS = Object.freeze([
  'company_domain_or',
  'company_linkedin_url_or',
  'company_name_or',
  'company_name_case_insensitive_or',
  'job_title_or',
  'job_title_pattern_or',
  'url_domain_or',
]);
const QUERY_FILTER_KEY_SET = new Set(QUERY_FILTER_KEYS);
const PREVIEW_INCOMPATIBLE_FILTERS = new Set([
  'company_domain_or',
  'company_linkedin_url_or',
  'company_name_or',
  'company_name_case_insensitive_or',
]);
const TOP_LEVEL_RESPONSE_KEYS = new Set(['data', 'metadata']);
const METADATA_KEYS = new Set([
  'total_results',
  'total_companies',
  'truncated_results',
  'truncated_companies',
  'page',
  'limit',
]);
const COMPANY_KEYS = new Set([
  'name',
  'id',
  'domain',
  'country',
  'country_code',
  'linkedin_url',
  'url',
  'is_recruiting_agency',
]);
const LOCATION_OBJECT_KEYS = new Set([
  'id',
  'name',
  'display_name',
  'type',
  'country_code',
  'admin1_name',
  'admin1_code',
  'admin2_name',
  'admin2_code',
  'continent',
  'latitude',
  'longitude',
  'city',
  'state',
  'state_code',
  'postal_code',
  'address',
  'feature_code',
]);
const STRING_ITEM_FIELDS = new Set([
  'company',
  'company_domain',
  'country',
  'country_code',
  'date_posted',
  'date_reposted',
  'description',
  'discovered_at',
  'final_url',
  'job_title',
  'location',
  'long_location',
  'short_location',
  'postal_code',
  'source_url',
  'state_code',
  'url',
  'closed_at',
]);
const BOOLEAN_ITEM_FIELDS = new Set(['easy_apply', 'hybrid', 'remote', 'reposted']);
const STRING_ARRAY_ITEM_FIELDS = new Set([
  'cities',
  'continents',
  'countries',
  'country_codes',
  'employment_statuses',
  'technology_slugs',
  'keyword_slugs',
]);
const NUMBER_ITEM_FIELDS = new Set(['latitude', 'longitude']);

function isRecord(value) {
  return value !== null && typeof value === 'object' && !Array.isArray(value);
}

function deepFreeze(value, seen = new WeakSet()) {
  if (value === null || typeof value !== 'object' || seen.has(value)) return value;
  seen.add(value);
  for (const child of Object.values(value)) deepFreeze(child, seen);
  return Object.freeze(value);
}

function immutable(value) {
  return deepFreeze(structuredClone(value));
}

function terminal() {
  throw new TheirStackError(THEIRSTACK_ERROR_CODES.terminal_validation);
}

function assertRecord(value) {
  if (!isRecord(value)) terminal();
  return value;
}

function assertNonEmptyString(value) {
  if (typeof value !== 'string' || value.length === 0 || /[\u0000-\u001f\u007f]/u.test(value)) terminal();
  return value;
}

function assertIsoTimestamp(value) {
  assertNonEmptyString(value);
  if (!Number.isFinite(Date.parse(value))) terminal();
  return value;
}

function assertPage(value) {
  if (typeof value !== 'number' || !Number.isInteger(value) || value < 0) terminal();
  return value;
}

function assertLimit(value) {
  if (typeof value !== 'number' || !Number.isInteger(value) || value < 1 || value > 100) terminal();
  return value;
}

function assertBoolean(value) {
  if (typeof value !== 'boolean') terminal();
  return value;
}

function assertNullableString(value) {
  if (value !== null) assertNonEmptyString(value);
  return value;
}

function assertResponseStatus(response) {
  if (!isRecord(response)) terminal();
  if (response.status === undefined) return 200;
  if (typeof response.status !== 'number' || !Number.isInteger(response.status) || response.status < 100 || response.status > 599) {
    terminal();
  }
  return response.status;
}

async function boundedResponseJson(response, maxBytes, mode) {
  const code = mode === 'paid' ? THEIRSTACK_ERROR_CODES.paid_ambiguous : THEIRSTACK_ERROR_CODES.terminal_validation;
  const contentLength = response?.headers?.get?.('content-length') ?? response?.headers?.['content-length'];
  if (typeof contentLength === 'string' && /^\d+$/u.test(contentLength) && Number(contentLength) > maxBytes) {
    throw new TheirStackError(code);
  }
  const body = response?.body;
  if (body !== null && body !== undefined && typeof body[Symbol.asyncIterator] === 'function') {
    const chunks = [];
    let total = 0;
    for await (const chunk of body) {
      if (!(chunk instanceof Uint8Array)) throw new TheirStackError(code);
      total += chunk.byteLength;
      if (total > maxBytes) throw new TheirStackError(code);
      chunks.push(chunk);
    }
    return JSON.parse(Buffer.concat(chunks, total).toString('utf8'));
  }
  if (typeof response?.json !== 'function') throw new TheirStackError(code);
  return response.json();
}

function sleep(milliseconds) {
  if (milliseconds <= 0) return Promise.resolve();
  return new Promise((resolve) => setTimeout(resolve, milliseconds));
}

function retryDelay(attempt, response, configured) {
  const retryAfter = response?.headers?.get?.('retry-after') ?? response?.headers?.['retry-after'];
  if (typeof retryAfter === 'string') {
    if (/^\d+$/u.test(retryAfter)) return Math.min(Number(retryAfter) * 1_000, 60_000);
    const retryAt = Date.parse(retryAfter);
    if (Number.isFinite(retryAt)) return Math.min(Math.max(retryAt - Date.now(), 0), 60_000);
  }
  if (configured > 0) return Math.min(configured * 2 ** attempt, 2_000);
  return 0;
}

function validateProfile(profile) {
  if (typeof profile !== 'string' || !THEIRSTACK_PROFILE_NAMES.includes(profile)) terminal();
  return profile;
}

function validateMode(mode) {
  if (mode !== 'preview' && mode !== 'paid') terminal();
  return mode;
}

function validateWindowEnd(windowEnd) {
  return assertIsoTimestamp(windowEnd);
}

function profileFilters(profile) {
  const result = {};
  if (profile === 'new_grad_cs') {
    result.job_title_or = [...CS_ROLE_TITLES];
    result.job_description_pattern_or = [...EARLY_CAREER_PATTERNS];
  } else if (profile === 'new_grad_non_coop_cs') {
    result.job_title_or = [...NON_COOP_ROLE_TITLES];
    result.job_description_pattern_or = [...NON_COOP_EARLY_CAREER_PATTERNS];
    result.job_description_pattern_not = [
      ...BAD_DESCRIPTION_PATTERNS,
      ...COOP_DESCRIPTION_PATTERNS,
    ];
  } else if (profile === 'fall_coop_swe_data') {
    result.job_title_or = [...COOP_ROLE_TITLES];
    result.job_description_pattern_or = [...COOP_DESCRIPTION_PATTERNS];
  }
  return result;
}

function validateQueryFilters(value) {
  if (value === undefined || value === null) return {};
  assertRecord(value);
  const filters = {};
  for (const key of Object.keys(value)) {
    if (!QUERY_FILTER_KEY_SET.has(key)) terminal();
    const entries = value[key];
    if (!Array.isArray(entries)) terminal();
    const copy = entries.map((entry) => assertNonEmptyString(entry));
    const sorted = [...copy].sort();
    if (copy.some((entry, index) => entry !== sorted[index]) || new Set(copy).size !== copy.length) terminal();
    filters[key] = copy;
  }
  return filters;
}

function mergeQueryFilters(body, queryFilters, mode) {
  const filters = validateQueryFilters(queryFilters);
  if (mode === 'preview') {
    for (const key of PREVIEW_INCOMPATIBLE_FILTERS) {
      if (filters[key]?.length > 0) terminal();
    }
  }
  for (const [key, entries] of Object.entries(filters)) {
    if (key === 'job_title_or' && Array.isArray(body.job_title_or)) {
      body[key] = [...new Set([...body[key], ...entries])];
    } else {
      body[key] = [...entries];
    }
  }
  return body;
}

function buildBody({
  profile,
  mode,
  page,
  limit,
  checkpoint,
  windowEnd,
  includeTotals,
  postedAtMaxAgeDays,
  queryFilters,
}) {
  const body = {
    blur_company_data: mode === 'preview',
    include_total_results: mode === 'preview' ? true : includeTotals,
    limit: mode === 'preview' ? 1 : limit,
    page,
    posted_at_max_age_days: postedAtMaxAgeDays,
    discovered_at_lte: windowEnd,
    order_by: ORDER_BY.map((sort) => ({ ...sort })),
    is_closed: false,
    company_type: 'direct_employer',
    job_country_code_or: ['US'],
    job_title_not: [...BAD_TITLE_MATCHES],
    job_description_pattern_not: [...BAD_DESCRIPTION_PATTERNS],
    ...profileFilters(profile),
  };
  if (checkpoint !== null) body.discovered_at_gte = checkpoint;
  return mergeQueryFilters(body, queryFilters, mode);
}

function validateRequest(request, mode, baseUrl) {
  assertRecord(request);
  if (new Set(Object.keys(request)).size !== 5 || !Object.keys(request).every((key) => ['url', 'body', 'page', 'limit', 'requestSha256'].includes(key))) terminal();
  if (request.url !== `${baseUrl}${THEIRSTACK_SEARCH_PATH}`) terminal();
  assertRecord(request.body);
  assertPage(request.page);
  assertLimit(request.limit);
  if (request.body.page !== request.page || request.body.limit !== request.limit) terminal();
  if (typeof request.requestSha256 !== 'string' || !/^[0-9a-f]{64}$/u.test(request.requestSha256)) terminal();
  if (sha256Canonical(request.body) !== request.requestSha256) terminal();
  if (mode === 'preview') {
    if (request.page !== 0 || request.limit !== 1 || request.body.blur_company_data !== true || request.body.include_total_results !== true) terminal();
  } else if (request.body.blur_company_data !== false) {
    terminal();
  }
  return request;
}

function validateCompanyObject(companyObject) {
  assertRecord(companyObject);
  for (const key of Object.keys(companyObject)) if (!COMPANY_KEYS.has(key)) continue;
  if (typeof companyObject.name !== 'string' || companyObject.name.length === 0) terminal();
}

function validateLocationObject(location) {
  assertRecord(location);
  for (const [key, value] of Object.entries(location)) {
    if (!LOCATION_OBJECT_KEYS.has(key)) continue;
    if (['id'].includes(key) && value !== undefined && (typeof value !== 'number' || !Number.isInteger(value))) terminal();
    if (['latitude', 'longitude'].includes(key) && value !== undefined && (typeof value !== 'number' || !Number.isFinite(value))) terminal();
    if (['address', 'postal_code'].includes(key)) {
      if (value !== null && value !== undefined && typeof value !== 'string') terminal();
    } else if (value !== undefined && value !== null && typeof value !== 'string') {
      if (!['id', 'latitude', 'longitude'].includes(key)) terminal();
    }
  }
}

function validateJobItem(item) {
  assertRecord(item);
  if (typeof item.id !== 'number' || !Number.isSafeInteger(item.id) || item.id < 0) terminal();
  assertNonEmptyString(item.job_title);
  validateCompanyObject(item.company_object);
  const urls = [item.url, item.source_url, item.final_url];
  if (!urls.some((value) => typeof value === 'string' && value.length > 0)) terminal();
  assertNonEmptyString(item.date_posted);
  assertIsoTimestamp(item.date_posted);
  assertNonEmptyString(item.discovered_at);
  assertIsoTimestamp(item.discovered_at);

  for (const field of STRING_ITEM_FIELDS) {
    if (!(field in item)) continue;
    const value = item[field];
    if (value === null || value === '') continue;
    if (typeof value !== 'string') terminal();
    if (['date_posted', 'date_reposted', 'discovered_at', 'closed_at'].includes(field)) assertIsoTimestamp(value);
  }
  for (const field of BOOLEAN_ITEM_FIELDS) {
    if (!(field in item)) continue;
    if (item[field] !== null && typeof item[field] !== 'boolean') terminal();
  }
  for (const field of STRING_ARRAY_ITEM_FIELDS) {
    if (!(field in item)) continue;
    const value = item[field];
    if (value === null) continue;
    if (!Array.isArray(value) || value.some((entry) => typeof entry !== 'string')) terminal();
  }
  for (const field of NUMBER_ITEM_FIELDS) {
    if (!(field in item)) continue;
    const value = item[field];
    if (value !== null && (typeof value !== 'number' || !Number.isFinite(value))) terminal();
  }
  if ('locations' in item) {
    if (item.locations === null) return item;
    if (!Array.isArray(item.locations)) terminal();
    for (const location of item.locations) validateLocationObject(location);
  }
  return item;
}

function validateResponse(payload, request, mode) {
  assertRecord(payload);
  const keys = Object.keys(payload);
  if (keys.length !== TOP_LEVEL_RESPONSE_KEYS.size || keys.some((key) => !TOP_LEVEL_RESPONSE_KEYS.has(key))) terminal();
  if (!Array.isArray(payload.data)) terminal();
  assertRecord(payload.metadata);
  for (const key of Object.keys(payload.metadata)) if (!METADATA_KEYS.has(key)) terminal();

  const metadata = payload.metadata;
  for (const key of ['total_companies', 'truncated_results', 'truncated_companies']) {
    if (!(key in metadata)) continue;
    if (metadata[key] !== null && (typeof metadata[key] !== 'number' || !Number.isSafeInteger(metadata[key]) || metadata[key] < 0)) terminal();
  }
  if ((metadata.truncated_results ?? 0) > 0) throw new TheirStackError(THEIRSTACK_ERROR_CODES.account_health);
  let totalResults = null;
  if ('total_results' in metadata) {
    if (metadata.total_results !== null && (typeof metadata.total_results !== 'number' || !Number.isSafeInteger(metadata.total_results) || metadata.total_results < 0)) terminal();
    totalResults = metadata.total_results;
  }
  if ('page' in metadata) {
    if (typeof metadata.page !== 'number' || !Number.isSafeInteger(metadata.page) || metadata.page !== request.page) terminal();
  }
  if ('limit' in metadata) {
    if (typeof metadata.limit !== 'number' || !Number.isSafeInteger(metadata.limit) || metadata.limit !== request.limit) terminal();
  }
  if (payload.data.length > request.limit) terminal();
  for (const item of payload.data) mode === 'preview' ? assertRecord(item) : validateJobItem(item);
  if (totalResults !== null && request.page * request.limit + payload.data.length > totalResults) terminal();
  return { data: payload.data, totalResults };
}

function classifyHttpStatus(status, mode, attempt, maxRetries) {
  if (status === 401 || status === 403) {
    throw new TheirStackError(THEIRSTACK_ERROR_CODES.authentication);
  }
  if (status === 402 || status === 409 || status === 423 || status === 451) {
    throw new TheirStackError(THEIRSTACK_ERROR_CODES.account_health);
  }
  const retryable = status === 429 || (status >= 500 && status <= 599);
  if (retryable) {
    if (mode === 'preview') {
      if (attempt < maxRetries) return true;
      throw new TheirStackError(THEIRSTACK_ERROR_CODES.retryable_preview);
    }
    throw new TheirStackError(THEIRSTACK_ERROR_CODES.paid_ambiguous);
  }
  if (status < 200 || status >= 300) {
    throw new TheirStackError(THEIRSTACK_ERROR_CODES.terminal_validation);
  }
}

function dayWindow(value) {
  const date = new Date(value);
  const start = new Date(Date.UTC(date.getUTCFullYear(), date.getUTCMonth(), date.getUTCDate(), 0, 0, 0));
  const end = new Date(Date.UTC(date.getUTCFullYear(), date.getUTCMonth(), date.getUTCDate(), 23, 59, 59, 999));
  return { start: start.toISOString(), end: end.toISOString() };
}

function canonicalTimestamp(value, nullable = false) {
  if (value === null && nullable) return null;
  assertIsoTimestamp(value);
  return new Date(value).toISOString();
}

function validateObservedAt(value) {
  return canonicalTimestamp(value);
}

function sha256Text(value) {
  return createHash('sha256').update(value, 'utf8').digest('hex');
}

function locationValue(raw) {
  for (const key of ['long_location', 'location', 'short_location']) {
    if (typeof raw[key] === 'string' && raw[key].length > 0) return raw[key];
  }
  if (Array.isArray(raw.locations) && raw.locations.length > 0) {
    const first = raw.locations[0];
    if (typeof first.display_name === 'string' && first.display_name.length > 0) return first.display_name;
    if (typeof first.name === 'string' && first.name.length > 0) return first.name;
  }
  for (const key of ['city', 'state_code', 'country']) {
    if (typeof raw[key] === 'string' && raw[key].length > 0) return raw[key];
  }
  return null;
}

function workplaceValue(raw) {
  if (raw.hybrid === true) return 'hybrid';
  if (raw.remote === true) return 'remote';
  if (raw.hybrid === false && raw.remote === false) return 'onsite';
  if (raw.remote === false && raw.hybrid === undefined) return 'onsite';
  return 'unknown';
}

function freshnessValue(raw, observedAt) {
  const sourceTimestamp = raw.date_reposted ?? raw.discovered_at ?? raw.date_posted;
  if (typeof sourceTimestamp !== 'string') return 'unverified';
  const sourceMillis = Date.parse(sourceTimestamp);
  const observedMillis = Date.parse(observedAt);
  if (!Number.isFinite(sourceMillis) || !Number.isFinite(observedMillis) || sourceMillis > observedMillis) return 'unverified';
  return observedMillis - sourceMillis <= 30 * 24 * 60 * 60 * 1_000 ? 'current' : 'stale';
}

function classifyAtsValue({ canonicalApplicationUrl }) {
  const result = classifyAts(canonicalApplicationUrl);
  if (!isRecord(result)) terminal();
  const { kind, identifier } = result;
  if (typeof kind !== 'string' || (identifier !== null && typeof identifier !== 'string')) terminal();
  return { kind, identifier };
}

function classifyIdentityValue(job) {
  const result = deriveDedupeIdentity(job);
  if (!isRecord(result)) terminal();
  const { kind, key, reviewRequired } = result;
  if (typeof kind !== 'string' || typeof key !== 'string' || typeof reviewRequired !== 'boolean') terminal();
  return { kind, key, reviewRequired };
}

function classifyEligibilityValue(job) {
  const result = classifyEligibility(job);
  if (!isRecord(result)) terminal();
  const { eligibilityState, eligibilityReasonCodes, priority } = result;
  if (
    typeof eligibilityState !== 'string'
    || !Array.isArray(eligibilityReasonCodes)
    || eligibilityReasonCodes.some((code) => typeof code !== 'string')
    || typeof priority !== 'number'
    || !Number.isInteger(priority)
  ) {
    terminal();
  }
  return { state: eligibilityState, reasonCodes: [...eligibilityReasonCodes], priority };
}

function firstNonEmptyString(...values) {
  return values.find((value) => typeof value === 'string' && value.length > 0) ?? null;
}


function normalizeOfficialJob(raw, context) {
  assertRecord(raw);
  validateJobItem(raw);
  assertRecord(context);
  const observedAt = validateObservedAt(context.observedAt);
  const rawPayloadPath = assertNonEmptyString(context.rawPayloadPath);
  const rawPayloadSha256 = assertNonEmptyString(context.rawPayloadSha256);
  if (!/^[0-9a-f]{64}$/u.test(rawPayloadSha256)) terminal();

  const sourceJobId = String(raw.id);
  const canonicalListingUrl = canonicalizeJobUrl(firstNonEmptyString(raw.url, raw.source_url, raw.final_url));
  const canonicalApplicationUrl = canonicalizeJobUrl(firstNonEmptyString(raw.final_url, raw.url, raw.source_url));
  const ats = classifyAtsValue({ canonicalListingUrl, canonicalApplicationUrl });
  const description = typeof raw.description === 'string' ? raw.description : '';
  const sourcePostedAt = canonicalTimestamp(raw.date_posted);
  const repostedAt = firstNonEmptyString(raw.date_reposted);
  const sourceUpdatedAt = canonicalTimestamp(repostedAt ?? raw.discovered_at ?? raw.date_posted);
  const discoveredAt = canonicalTimestamp(raw.discovered_at);
  const availabilityState = raw.closed_at === undefined ? 'unknown' : firstNonEmptyString(raw.closed_at) === null ? 'open' : 'closed';
  const freshnessState = freshnessValue(
    {
      ...raw,
      date_posted: sourcePostedAt,
      date_reposted: repostedAt === null ? null : canonicalTimestamp(repostedAt),
      discovered_at: canonicalTimestamp(raw.discovered_at),
    },
    observedAt,
  );

  const baseJob = {
    schema: 'normalized-job-v1',
    source: THEIRSTACK_SOURCE,
    sourceJobId,
    canonicalListingUrl,
    canonicalApplicationUrl,
    atsKind: ats.kind,
    atsIdentifier: ats.identifier,
    title: raw.job_title,
    company: raw.company_object.name,
    location: locationValue(raw),
    workplaceType: workplaceValue(raw),
    employmentTypes: raw.employment_statuses ? [...new Set(raw.employment_statuses)].sort() : [],
    description,
    descriptionSha256: sha256Text(description),
    sourcePostedAt,
    sourceUpdatedAt,
    discoveredAt,
    availabilityState,
    freshnessState,
    rawPayloadPath,
    rawPayloadSha256,
  };

  const eligibility = classifyEligibilityValue(baseJob);
  const withEligibility = {
    ...baseJob,
    eligibilityState: eligibility.state,
    eligibilityReasonCodes: eligibility.reasonCodes,
    priority: eligibility.priority,
  };
  const identity = classifyIdentityValue(withEligibility);
  const normalized = {
    ...withEligibility,
    dedupeIdentityKind: identity.kind,
    dedupeIdentityKey: identity.key,
    dedupeReviewRequired: identity.reviewRequired,
  };
  return validateNormalizedJob(normalized);
}
const CONSTRUCTION_OPTION_KEYS = new Set([
  'apiKey',
  'fetch',
  'baseUrl',
  'timeoutMs',
  'maxPreviewRetries',
  'retryDelayMs',
  'maxResponseBytes',
  'postedAtMaxAgeDays',
  'queryFilters',
  'windowEnd',
  'now',
  'creditNow',
  'paidAuthorization',
]);

export function createTheirStackAdapter(options = {}) {
  assertRecord(options);
  for (const key of Object.keys(options)) if (!CONSTRUCTION_OPTION_KEYS.has(key)) terminal();
  const apiKey = options.apiKey;
  if (apiKey !== undefined && (typeof apiKey !== 'string' || apiKey.length === 0 || /[\u0000-\u001f\u007f]/u.test(apiKey))) terminal();
  const fetchImpl = options.fetch ?? globalThis.fetch;
  if (typeof fetchImpl !== 'function') terminal();
  const baseUrl = options.baseUrl ?? THEIRSTACK_DEFAULT_BASE_URL;
  if (typeof baseUrl !== 'string' || baseUrl.length === 0 || /[\u0000-\u001f\u007f]/u.test(baseUrl)) terminal();
  const normalizedBaseUrl = baseUrl.replace(/\/+$/u, '');
  const timeoutMs = options.timeoutMs ?? THEIRSTACK_DEFAULT_TIMEOUT_MS;
  if (typeof timeoutMs !== 'number' || !Number.isFinite(timeoutMs) || timeoutMs <= 0 || timeoutMs > 120_000) terminal();
  const maxPreviewRetries = options.maxPreviewRetries ?? THEIRSTACK_DEFAULT_PREVIEW_RETRIES;
  if (typeof maxPreviewRetries !== 'number' || !Number.isInteger(maxPreviewRetries) || maxPreviewRetries < 0 || maxPreviewRetries > 5) terminal();
  const retryDelayMs = options.retryDelayMs ?? 0;
  if (typeof retryDelayMs !== 'number' || !Number.isFinite(retryDelayMs) || retryDelayMs < 0 || retryDelayMs > 2_000) terminal();
  const maxResponseBytes = options.maxResponseBytes ?? THEIRSTACK_DEFAULT_MAX_RESPONSE_BYTES;
  if (typeof maxResponseBytes !== 'number' || !Number.isSafeInteger(maxResponseBytes) || maxResponseBytes < 1 || maxResponseBytes > 128 * 1024 * 1024) terminal();
  const postedAtMaxAgeDays = options.postedAtMaxAgeDays ?? THEIRSTACK_DEFAULT_POSTED_MAX_AGE_DAYS;
  if (typeof postedAtMaxAgeDays !== 'number' || !Number.isInteger(postedAtMaxAgeDays) || postedAtMaxAgeDays < 0 || postedAtMaxAgeDays > 30) terminal();
  const configuredQueryFilters = validateQueryFilters(options.queryFilters);
  const paidAuthorization = options.paidAuthorization;
  if (paidAuthorization !== undefined && paidAuthorization !== true && paidAuthorization !== false) terminal();
  const now = options.now ?? (() => new Date());
  const responseNow = options.responseNow ?? (() => new Date());
  const creditNow = options.creditNow ?? (() => new Date());
  if (typeof now !== 'function' || typeof responseNow !== 'function' || typeof creditNow !== 'function') terminal();
  const clockValue = (clock) => {
    const value = clock();
    if (value instanceof Date) {
      if (!Number.isFinite(value.getTime())) terminal();
      return value.toISOString();
    }
    return canonicalTimestamp(value);
  };
  const clockNow = () => clockValue(now);
  const responseClockNow = () => clockValue(responseNow);
  const creditClockNow = () => clockValue(creditNow);
  async function fetchJson(url, init, mode) {
    for (let attempt = 0; ; attempt += 1) {
      const controller = new AbortController();
      const timeout = setTimeout(() => controller.abort(), timeoutMs);
      let response = null;
      let payload;
      let shouldRetry = false;
      let transientFailure = false;
      let failure = null;
      try {
        response = await fetchImpl(url, { ...init, signal: controller.signal });
        shouldRetry = classifyHttpStatus(assertResponseStatus(response), mode, attempt, maxPreviewRetries) === true;
        if (!shouldRetry) {
          try {
            payload = await boundedResponseJson(response, maxResponseBytes, mode);
          } catch (error) {
            if (error instanceof TheirStackError) {
              failure = error;
            } else if (mode === 'paid') {
              failure = new TheirStackError(THEIRSTACK_ERROR_CODES.paid_ambiguous);
            } else if (error instanceof SyntaxError) {
              failure = new TheirStackError(THEIRSTACK_ERROR_CODES.terminal_validation);
            } else {
              transientFailure = true;
            }
          }
        }
      } catch (error) {
        if (error instanceof TheirStackError) failure = error;
        else transientFailure = true;
      } finally {
        clearTimeout(timeout);
      }
      if (shouldRetry) {
        await sleep(retryDelay(attempt, response, retryDelayMs));
        continue;
      }
      if (transientFailure) {
        if (mode === 'preview' && attempt < maxPreviewRetries) {
          await sleep(retryDelay(attempt, response, retryDelayMs));
          continue;
        }
        throw new TheirStackError(mode === 'preview' ? THEIRSTACK_ERROR_CODES.retryable_preview : THEIRSTACK_ERROR_CODES.paid_ambiguous);
      }
      if (failure !== null) throw failure;
      return payload;
    }
  }

  async function readCreditUsage(period = undefined) {
    if (typeof apiKey !== 'string' || apiKey.length === 0) {
      throw new TheirStackError(THEIRSTACK_ERROR_CODES.authentication);
    }
    const observedAt = creditClockNow();
    let periodStart;
    let periodEnd;
    if (period === undefined) {
      ({ start: periodStart, end: periodEnd } = dayWindow(observedAt));
    } else {
      assertRecord(period);
      const keys = Object.keys(period);
      if (keys.length !== 2 || !keys.includes('periodStart') || !keys.includes('periodEnd')) terminal();
      periodStart = canonicalTimestamp(period.periodStart);
      periodEnd = canonicalTimestamp(period.periodEnd);
      const expected = dayWindow(periodStart);
      if (periodStart !== expected.start || periodEnd !== expected.end) terminal();
    }
    const startMs = Date.parse(periodStart);
    const endMs = Date.parse(periodEnd);
    const observedMs = Date.parse(observedAt);
    if (!Number.isFinite(startMs) || !Number.isFinite(endMs) || !Number.isFinite(observedMs)
      || startMs > endMs || observedMs < startMs) {
      throw new TheirStackError(THEIRSTACK_ERROR_CODES.terminal_validation);
    }
    const params = new URLSearchParams({
      start_datetime: periodStart,
      end_datetime: periodEnd,
      timezone: 'UTC',
    });
    const url = `${normalizedBaseUrl}/v0/teams/credits_consumption?${params.toString()}`;
    const headers = { accept: 'application/json', authorization: `Bearer ${apiKey}` };
    const payload = await fetchJson(url, { method: 'GET', headers }, 'preview');
    if (!Array.isArray(payload)) {
      throw new TheirStackError(THEIRSTACK_ERROR_CODES.terminal_validation);
    }
    let consumedCredits = 0;
    const seenPeriods = new Set();
    for (const entry of payload) {
      if (!isRecord(entry)) {
        throw new TheirStackError(THEIRSTACK_ERROR_CODES.terminal_validation);
      }
      const entryPeriodStart = canonicalTimestamp(entry.period_start);
      if (entryPeriodStart < periodStart || entryPeriodStart > periodEnd
        || entryPeriodStart > observedAt || seenPeriods.has(entryPeriodStart)) {
        throw new TheirStackError(THEIRSTACK_ERROR_CODES.terminal_validation);
      }
      seenPeriods.add(entryPeriodStart);
      const consumed = entry.api_credits_consumed;
      if (typeof consumed !== 'number' || !Number.isSafeInteger(consumed) || consumed < 0) {
        throw new TheirStackError(THEIRSTACK_ERROR_CODES.terminal_validation);
      }
      consumedCredits += consumed;
      if (!Number.isSafeInteger(consumedCredits)) {
        throw new TheirStackError(THEIRSTACK_ERROR_CODES.terminal_validation);
      }
    }
    return deepFreeze({ observedAt, periodStart, periodEnd, consumedCredits });
  }
  const configuredWindowEnd = options.windowEnd ?? clockNow();
  validateWindowEnd(configuredWindowEnd);

  const totalsByScope = new Map();
  function buildRequest({
    profile = 'default',
    mode = 'paid',
    page = 0,
    limit = THEIRSTACK_DEFAULT_PAGE_LIMIT,
    checkpoint = null,
    windowEnd = configuredWindowEnd,
    includeTotals = true,
  } = {}) {
    validateProfile(profile);
    validateMode(mode);
    assertPage(page);
    if (mode === 'preview') {
      if (page !== 0) terminal();
    } else {
      assertLimit(limit);
    }
    if (mode === 'preview') limit = 1;
    else assertLimit(limit);
    if (typeof includeTotals !== 'boolean') terminal();
    if (checkpoint !== null) {
      assertIsoTimestamp(checkpoint);
      if (Date.parse(checkpoint) > Date.parse(windowEnd)) terminal();
    }
    validateWindowEnd(windowEnd);
    const body = buildBody({
      profile,
      mode,
      page,
      limit,
      checkpoint,
      windowEnd,
      includeTotals: page === 0 ? includeTotals : false,
      postedAtMaxAgeDays,
      queryFilters: configuredQueryFilters,
    });
    const request = {
      url: `${normalizedBaseUrl}${THEIRSTACK_SEARCH_PATH}`,
      body,
      page,
      limit,
      requestSha256: sha256Canonical(body),
    };
    return immutable(request);
  }

  async function fetchPage({ request, mode } = {}) {
    validateMode(mode);
    validateRequest(request, mode, normalizedBaseUrl);
    if (mode === 'paid') {
      if (paidAuthorization !== true) {
        throw new TheirStackError(THEIRSTACK_ERROR_CODES.paid_authorization);
      }
      if (typeof apiKey !== 'string' || apiKey.length === 0) {
        throw new TheirStackError(THEIRSTACK_ERROR_CODES.authentication);
      }
    } else if (typeof apiKey !== 'string' || apiKey.length === 0) {
      throw new TheirStackError(THEIRSTACK_ERROR_CODES.authentication);
    }

    const headers = {
      accept: 'application/json',
      authorization: `Bearer ${apiKey}`,
      'content-type': 'application/json',
    };
    const payload = await fetchJson(request.url, {
      method: 'POST',
      headers,
      body: JSON.stringify(request.body),
    }, mode);
    let validated;
    try {
      validated = validateResponse(payload, request, mode);
    } catch (error) {
      if (mode === 'paid' && error instanceof TheirStackError && error.code === THEIRSTACK_ERROR_CODES.terminal_validation) {
        throw new TheirStackError(THEIRSTACK_ERROR_CODES.paid_ambiguous);
      }
      if (!(error instanceof TheirStackError)) {
        throw new TheirStackError(mode === 'paid' ? THEIRSTACK_ERROR_CODES.paid_ambiguous : THEIRSTACK_ERROR_CODES.terminal_validation);
      }
      throw error;
    }
    const scopeBody = { ...request.body };
    delete scopeBody.page;
    delete scopeBody.include_total_results;
    const scope = canonicalJson(scopeBody);
    if (validated.totalResults !== null) {
      const previous = totalsByScope.get(scope);
      if (previous !== undefined && previous !== validated.totalResults) terminal();
      totalsByScope.set(scope, validated.totalResults);
    }
    const result = {
      requestSha256: request.requestSha256,
      items: validated.data.map((item) => immutable(item)),
      totalResults: validated.totalResults,
      receivedAt: responseClockNow(),
      estimatedCredits: mode === 'preview' ? (validated.totalResults ?? 0) : validated.data.length,
      reportedCredits: mode === 'preview' ? 0 : null,
    };
    return deepFreeze(result);
  }

  const adapter = {
    source: THEIRSTACK_SOURCE,
    requiresCreditReconciliation: true,
    buildRequest,
    fetchPage,
    readCreditUsage,
    normalizeJob: normalizeOfficialJob,
    normalizeCheckpoint(value) {
      return canonicalTimestamp(value);
    },
  };
  return deepFreeze(adapter);
}
