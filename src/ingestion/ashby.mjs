import {
  canonicalizeJobUrl, classifyAts, classifyEligibility, deriveDedupeIdentity,
  sha256Canonical, sha256Text, validateNormalizedJob,
} from './contracts.mjs';

export const ASHBY_SOURCE = 'ashby';
export const ASHBY_DEFAULT_BASE_URL = 'https://api.ashbyhq.com';
export const ASHBY_DEFAULT_TIMEOUT_MS = 10_000;
export const ASHBY_DEFAULT_MAX_RESPONSE_BYTES = 32 * 1024 * 1024;
export const ASHBY_ERROR_CODES = Object.freeze({ board_not_found: 'BOARD_NOT_FOUND', network_error: 'NETWORK_ERROR', malformed_json: 'MALFORMED_JSON' });

export class AshbyError extends Error { constructor(code, message = code) { super(message); this.name = 'AshbyError'; this.code = code; } }
export class AshbyBoardNotFoundError extends AshbyError { constructor(message = 'Board not found') { super(ASHBY_ERROR_CODES.board_not_found, message); this.name = 'AshbyBoardNotFoundError'; } }
export class AshbyNetworkError extends AshbyError { constructor(message = 'Network error') { super(ASHBY_ERROR_CODES.network_error, message); this.name = 'AshbyNetworkError'; } }
export class AshbyMalformedJsonError extends AshbyError { constructor(message = 'Malformed JSON response') { super(ASHBY_ERROR_CODES.malformed_json, message); this.name = 'AshbyMalformedJsonError'; } }

function timestamp(value) {
  if (value === null || value === undefined || value === '') return null;
  const date = value instanceof Date ? value : new Date(value);
  if (!Number.isFinite(date.getTime())) throw new TypeError(`Invalid timestamp: ${value}`);
  return date.toISOString();
}
function employmentTypes(raw) {
  const values = Array.isArray(raw.employmentTypes) ? raw.employmentTypes : (raw.employmentType ? [raw.employmentType] : []);
  return [...new Set(values.filter((v) => typeof v === 'string').map((v) => {
    const s = v.toLowerCase().replace(/[^a-z0-9_-]/gu, '');
    if (s.includes('full')) return 'full-time'; if (s.includes('part')) return 'part-time';
    if (s.includes('intern')) return 'internship'; if (s.includes('contract')) return 'contract'; return s;
  }).filter((v) => /^[a-z][a-z0-9_-]{0,63}$/u.test(v)))].sort();
}
function workplace(raw) {
  if (raw.isRemote === true || String(raw.workplaceType ?? '').toLowerCase() === 'remote') return 'remote';
  const value = String(raw.workplaceType ?? '').toLowerCase();
  if (value === 'hybrid') return 'hybrid'; if (value === 'onsite' || value === 'in_office') return 'onsite';
  const loc = String(raw.locationName ?? raw.location ?? '').toLowerCase();
  if (loc.includes('remote')) return 'remote'; if (loc.includes('hybrid')) return 'hybrid'; if (loc) return 'onsite'; return 'unknown';
}
function location(raw) {
  const value = raw.locationName ?? raw.location;
  return typeof value === 'string' && value.trim() ? value.trim() : null;
}

export function createAshbyAdapter(options = {}) {
  const fetchImpl = options.fetch ?? globalThis.fetch;
  if (typeof fetchImpl !== 'function') throw new TypeError('options.fetch must be a function');
  const boardName = typeof options.boardName === 'string' && options.boardName.trim() ? options.boardName.trim() : null;
  const baseUrl = (options.baseUrl ?? ASHBY_DEFAULT_BASE_URL).replace(/\/+$/u, '');
  const timeoutMs = options.timeoutMs > 0 ? options.timeoutMs : ASHBY_DEFAULT_TIMEOUT_MS;
  const maxResponseBytes = options.maxResponseBytes > 0 ? options.maxResponseBytes : ASHBY_DEFAULT_MAX_RESPONSE_BYTES;
  const now = () => timestamp(typeof options.now === 'function' ? options.now() : new Date());
  let snapshotCache = null;
  const resolveBoard = (profile) => boardName ?? (typeof profile === 'string' && profile.trim() ? profile.trim() : null) ?? (() => { throw new Error('Ashby boardName is required'); })();

  function buildRequest({ profile = 'default', page = 0, limit = 100, checkpoint = null, windowEnd = null } = {}) {
    const board = resolveBoard(profile);
    const cp = checkpoint === null ? null : normalizeCheckpoint(checkpoint);
    const end = windowEnd === null ? null : normalizeCheckpoint(windowEnd);
    if (cp && end && cp > end) throw new Error('checkpoint must not be later than windowEnd');
    const url = `${baseUrl}/posting-api/job-board/${encodeURIComponent(board)}`;
    const payload = {
      url,
      page,
      limit,
      checkpoint: cp,
      windowEnd: end,
      method: 'GET',
      body: {},
    };
    return Object.freeze({
      ...payload,
      headers: Object.freeze({ accept: 'application/json' }),
      requestSha256: sha256Canonical({ source: ASHBY_SOURCE, boardName: board, ...payload }),
    });
  }

  async function fetchPage({ request } = {}) {
    if (!request || typeof request.url !== 'string') throw new Error('Invalid request object');
    const cacheKey = sha256Canonical({
      url: request.url,
      checkpoint: request.checkpoint,
      windowEnd: request.windowEnd,
    });

    if (!snapshotCache || snapshotCache.key !== cacheKey) {
      snapshotCache = {
        key: cacheKey,
        promise: (async () => {
          const controller = new AbortController();
          const timer = setTimeout(() => controller.abort(), timeoutMs);
          let response;
          try {
            response = await fetchImpl(request.url, {
              method: request.method ?? 'GET',
              headers: request.headers,
              signal: controller.signal,
            });
          } catch (error) {
            throw new AshbyNetworkError(`Network request failed: ${error.message}`);
          } finally {
            clearTimeout(timer);
          }
          if (response.status === 404) {
            throw new AshbyBoardNotFoundError(`Ashby job board not found (404): ${request.url}`);
          }
          if (!response.ok) {
            throw new AshbyNetworkError(`HTTP ${response.status} ${response.statusText ?? ''}`.trim());
          }

          let text;
          try {
            if (typeof response.arrayBuffer === 'function') {
              const bytes = await response.arrayBuffer();
              if (bytes.byteLength > maxResponseBytes) {
                throw new AshbyNetworkError('Response exceeds maxResponseBytes');
              }
              text = new TextDecoder().decode(bytes);
            } else {
              text = await response.text();
              if (Buffer.byteLength(text) > maxResponseBytes) {
                throw new AshbyNetworkError('Response exceeds maxResponseBytes');
              }
            }
          } catch (error) {
            if (error instanceof AshbyError) throw error;
            throw new AshbyNetworkError(`Failed to read response body: ${error.message}`);
          }

          let payload;
          try {
            payload = JSON.parse(text);
          } catch (error) {
            throw new AshbyMalformedJsonError(`Invalid JSON response: ${error.message}`);
          }
          const raw = Array.isArray(payload) ? payload : payload && (payload.jobs ?? payload.postings);
          if (!Array.isArray(raw)) {
            throw new AshbyMalformedJsonError('Expected jobs or postings array in JSON response');
          }
          const cp = request.checkpoint ? Date.parse(request.checkpoint) : null;
          const end = request.windowEnd ? Date.parse(request.windowEnd) : null;
          const items = raw
            .filter((item) => {
              const posted = Date.parse(item?.publishedAt ?? item?.postedAt ?? item?.createdAt ?? '');
              if (cp !== null && (!Number.isFinite(posted) || posted < cp)) return false;
              if (end !== null && Number.isFinite(posted) && posted > end) return false;
              return true;
            })
            .map((item) => Object.freeze({ ...item }));
          return Object.freeze({
            items: Object.freeze(items),
            receivedAt: now(),
          });
        })(),
      };
    }

    const snapshot = await snapshotCache.promise;
    const start = request.page * request.limit;
    return Object.freeze({
      requestSha256: request.requestSha256,
      items: Object.freeze(snapshot.items.slice(start, start + request.limit)),
      totalResults: snapshot.items.length,
      receivedAt: snapshot.receivedAt,
      estimatedCredits: 0,
      reportedCredits: null,
    });
  }

  function normalizeJob(raw, { observedAt, rawPayloadPath, rawPayloadSha256 } = {}) {
    const id = raw.id ?? raw.jobId ?? null; const listing = raw.jobUrl ?? raw.jobBoardUrl ?? raw.hostedUrl ?? raw.applyUrl ?? null; const application = raw.applyUrl ?? listing;
    const canonicalListingUrl = listing ? canonicalizeJobUrl(listing) : null; const canonicalApplicationUrl = application ? canonicalizeJobUrl(application) : null; const ats = classifyAts(canonicalApplicationUrl);
    const base = { schema: 'normalized-job-v1', source: ASHBY_SOURCE, sourceJobId: id === null ? null : String(id), canonicalListingUrl, canonicalApplicationUrl, atsKind: ats.kind === 'unknown' ? 'ashby' : ats.kind, atsIdentifier: ats.identifier ?? (id ? `${boardName ?? 'unknown'}:${id}` : null), title: String(raw.title ?? raw.jobTitle ?? raw.name ?? 'Untitled Position').trim(), company: String(raw.companyName ?? raw.company ?? options.companyName ?? boardName ?? 'Unknown Company').trim(), location: location(raw), workplaceType: workplace(raw), employmentTypes: employmentTypes(raw), description: String(raw.descriptionHtml ?? raw.descriptionPlain ?? raw.description ?? raw.summary ?? '').trim(), descriptionSha256: sha256Text(String(raw.descriptionHtml ?? raw.descriptionPlain ?? raw.description ?? raw.summary ?? '').trim()), sourcePostedAt: timestamp(raw.publishedAt ?? raw.postedAt ?? raw.createdAt), sourceUpdatedAt: timestamp(raw.updatedAt), discoveredAt: timestamp(observedAt ?? now()), availabilityState: 'open', freshnessState: 'current', rawPayloadPath: rawPayloadPath ?? '/tmp/ashby-raw-payload.json', rawPayloadSha256: rawPayloadSha256 ?? sha256Text('{}') };
    const eligibility = classifyEligibility(base); const withEligibility = { ...base, ...eligibility }; const identity = deriveDedupeIdentity(withEligibility);
    return validateNormalizedJob({ ...withEligibility, dedupeIdentityKind: identity.kind, dedupeIdentityKey: identity.key, dedupeReviewRequired: identity.reviewRequired });
  }
  function normalizeCheckpoint(value) { const normalized = timestamp(value); if (!normalized) throw new TypeError('Checkpoint is required'); return normalized; }
  return Object.freeze({ source: ASHBY_SOURCE, requiresCreditReconciliation: false, buildRequest, fetchPage, normalizeJob, normalizeCheckpoint });
}
export default createAshbyAdapter;
