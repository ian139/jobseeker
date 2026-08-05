import { createHash } from 'node:crypto';
import {
  canonicalizeJobUrl,
  classifyAts,
  classifyEligibility,
  deriveDedupeIdentity,
  sha256Canonical,
  sha256Text,
  validateNormalizedJob,
} from './contracts.mjs';

export const GREENHOUSE_SOURCE = 'greenhouse';
export const GREENHOUSE_DEFAULT_BASE_URL = 'https://boards-api.greenhouse.io/v1/boards';
export const GREENHOUSE_DEFAULT_TIMEOUT_MS = 30_000;
export const GREENHOUSE_DEFAULT_MAX_RESPONSE_BYTES = 10 * 1024 * 1024;

export const GREENHOUSE_ERROR_CODES = Object.freeze({
  board_not_found: 'BOARD_NOT_FOUND',
  network_error: 'NETWORK_ERROR',
  invalid_json: 'INVALID_JSON',
  http_error: 'HTTP_ERROR',
  invalid_profile: 'INVALID_PROFILE',
});

export class GreenhouseIngestionError extends Error {
  constructor(code, message, options = {}) {
    super(message || code);
    this.name = 'GreenhouseIngestionError';
    this.code = code;
    if (options.status !== undefined) {
      this.status = options.status;
    }
    if (options.cause !== undefined) {
      this.cause = options.cause;
    }
  }
}

export function normalizeCheckpoint(value) {
  if (!value) {
    return new Date(0).toISOString();
  }
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return new Date(0).toISOString();
  }
  return date.toISOString();
}

function extractBoardToken(profile, optionsToken) {
  const token = profile || optionsToken;
  if (!token || typeof token !== 'string' || !token.trim()) {
    throw new GreenhouseIngestionError(
      GREENHOUSE_ERROR_CODES.invalid_profile,
      'A valid boardToken or profile is required for Greenhouse ingestion.',
    );
  }
  return token.trim();
}

function inferWorkplaceType(location, title) {
  const text = `${location ?? ''} ${title ?? ''}`.toLowerCase();
  if (text.includes('remote')) return 'remote';
  if (text.includes('hybrid')) return 'hybrid';
  if (text.includes('onsite') || text.includes('on-site')) return 'onsite';
  return 'unknown';
}

function extractLocationName(location) {
  if (!location) return null;
  if (typeof location === 'string') return location.trim() || null;
  if (typeof location === 'object' && typeof location.name === 'string') {
    return location.name.trim() || null;
  }
  return null;
}

export function createGreenhouseAdapter(options = {}) {
  const fetchImpl = options.fetch ?? globalThis.fetch;
  const nowImpl = options.now ?? (() => new Date());
  const baseUrl = (options.baseUrl ?? GREENHOUSE_DEFAULT_BASE_URL).replace(/\/+$/u, '');
  const timeoutMs = options.timeoutMs ?? GREENHOUSE_DEFAULT_TIMEOUT_MS;
  const maxResponseBytes = options.maxResponseBytes ?? GREENHOUSE_DEFAULT_MAX_RESPONSE_BYTES;
  const defaultBoardToken = options.boardToken ?? options.boardName ?? null;
  let snapshotCache = null;

  function buildRequest({ profile, page = 0, limit = 100, checkpoint, windowEnd } = {}) {
    const boardToken = extractBoardToken(profile, defaultBoardToken);
    const normalizedCheckpoint = normalizeCheckpoint(checkpoint);
    const normalizedWindowEnd = windowEnd ? normalizeCheckpoint(windowEnd) : null;
    const url = `${baseUrl}/${encodeURIComponent(boardToken)}/jobs?content=true`;
    const reqPayload = {
      url,
      method: 'GET',
      page,
      limit,
      checkpoint: normalizedCheckpoint,
      windowEnd: normalizedWindowEnd,
      body: {},
    };
    const requestSha256 = sha256Canonical(reqPayload);

    return Object.freeze({
      ...reqPayload,
      boardToken,
      requestSha256,
    });
  }

  async function fetchPage({ request } = {}) {
    if (!request || !request.url) {
      throw new GreenhouseIngestionError(
        GREENHOUSE_ERROR_CODES.invalid_profile,
        'Invalid request object provided to fetchPage.',
      );
    }

    const boardToken = request.boardToken || extractBoardToken(null, defaultBoardToken);
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
              method: request.method || 'GET',
              headers: request.headers || {},
              signal: controller.signal,
            });
          } catch (error) {
            throw new GreenhouseIngestionError(
              GREENHOUSE_ERROR_CODES.network_error,
              `Network error fetching Greenhouse index: ${error.message}`,
              { cause: error },
            );
          } finally {
            clearTimeout(timer);
          }

          if (response.status === 404) {
            throw new GreenhouseIngestionError(
              GREENHOUSE_ERROR_CODES.board_not_found,
              `Greenhouse board not found: ${boardToken}`,
              { status: 404 },
            );
          }
          if (!response.ok) {
            throw new GreenhouseIngestionError(
              GREENHOUSE_ERROR_CODES.http_error,
              `HTTP error fetching Greenhouse index: ${response.status}`,
              { status: response.status },
            );
          }

          let indexData;
          try {
            if (typeof response.text === 'function') {
              const text = await response.text();
              if (Buffer.byteLength(text, 'utf8') > maxResponseBytes) {
                throw new Error('Response byte limit exceeded');
              }
              indexData = JSON.parse(text);
            } else if (typeof response.json === 'function') {
              indexData = await response.json();
            } else {
              throw new Error('Response has no JSON body reader');
            }
          } catch (error) {
            throw new GreenhouseIngestionError(
              GREENHOUSE_ERROR_CODES.invalid_json,
              `Invalid JSON from Greenhouse index: ${error.message}`,
              { cause: error },
            );
          }

          const rawJobs = Array.isArray(indexData)
            ? indexData
            : (Array.isArray(indexData?.jobs) ? indexData.jobs : []);
          const checkpointTime = Date.parse(request.checkpoint);
          const windowEndTime = request.windowEnd ? Date.parse(request.windowEnd) : null;
          const items = rawJobs
            .map((job) => ({ ...job, boardToken }))
            .filter((job) => {
              const jobTime = Date.parse(job.updated_at || job.first_published || '');
              if (!Number.isFinite(jobTime)) return true;
              if (jobTime < checkpointTime) return false;
              return windowEndTime === null || jobTime <= windowEndTime;
            });

          return Object.freeze({
            items: Object.freeze(items),
            receivedAt: new Date(nowImpl()).toISOString(),
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
    if (!raw || typeof raw !== 'object') {
      throw new GreenhouseIngestionError(
        GREENHOUSE_ERROR_CODES.invalid_json,
        'Invalid raw job object for normalizeJob',
      );
    }

    const sourceJobId = String(raw.id);
    const listingUrl = raw.absolute_url || raw.url || null;
    const canonicalListingUrl = listingUrl ? canonicalizeJobUrl(listingUrl) : null;
    const canonicalApplicationUrl = canonicalListingUrl;

    const classifiedAts = canonicalApplicationUrl ? classifyAts(canonicalApplicationUrl) : { kind: 'unknown', identifier: null };
    const atsKind = classifiedAts.kind === 'unknown' ? 'greenhouse' : classifiedAts.kind;
    const atsIdentifier = classifiedAts.identifier ?? sourceJobId;

    const title = raw.title || 'Untitled';
    const company = raw.company_name || raw.company || raw.boardToken || 'Greenhouse';
    const location = extractLocationName(raw.location);
    const workplaceType = inferWorkplaceType(location, title);

    const description = typeof raw.content === 'string' ? raw.content : (typeof raw.description === 'string' ? raw.description : '');
    const descriptionSha256 = sha256Text(description);

    const postedTs = raw.first_published || raw.updated_at;
    const updatedTs = raw.updated_at || raw.first_published;
    const sourcePostedAt = postedTs ? new Date(postedTs).toISOString() : observedAt;
    const sourceUpdatedAt = updatedTs ? new Date(updatedTs).toISOString() : observedAt;

    const baseJob = {
      schema: 'normalized-job-v1',
      source: GREENHOUSE_SOURCE,
      sourceJobId,
      canonicalListingUrl,
      canonicalApplicationUrl,
      atsKind,
      atsIdentifier,
      title,
      company,
      location,
      workplaceType,
      employmentTypes: [],
      description,
      descriptionSha256,
      sourcePostedAt,
      sourceUpdatedAt,
      discoveredAt: observedAt,
      availabilityState: 'open',
      freshnessState: 'current',
      rawPayloadPath,
      rawPayloadSha256,
    };

    const eligibility = classifyEligibility(baseJob);
    const jobWithEligibility = {
      ...baseJob,
      eligibilityState: eligibility.eligibilityState,
      eligibilityReasonCodes: eligibility.eligibilityReasonCodes,
      priority: eligibility.priority,
    };

    const identity = deriveDedupeIdentity(jobWithEligibility);
    const finalJob = {
      ...jobWithEligibility,
      dedupeIdentityKind: identity.kind,
      dedupeIdentityKey: identity.key,
      dedupeReviewRequired: identity.reviewRequired,
    };

    return validateNormalizedJob(finalJob);
  }

  return Object.freeze({
    source: GREENHOUSE_SOURCE,
    requiresCreditReconciliation: false,
    buildRequest,
    fetchPage,
    normalizeJob,
    normalizeCheckpoint,
  });
}
