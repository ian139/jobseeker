import {
  canonicalizeJobUrl,
  classifyAts,
  classifyEligibility,
  deriveDedupeIdentity,
  sha256Canonical,
  sha256Text,
  validateNormalizedJob,
} from './contracts.mjs';

export const COMPANY_SITE_SOURCE = 'company-site';
export const COMPANY_SITE_DEFAULT_TIMEOUT_MS = 30_000;
export const COMPANY_SITE_DEFAULT_MAX_RESPONSE_BYTES = 32 * 1024 * 1024;
export const COMPANY_SITE_ERROR_CODES = Object.freeze({
  invalid_config: 'invalid_config',
  network_error: 'network_error',
  invalid_html: 'invalid_html',
  board_not_found: 'board_not_found',
  invalid_checkpoint: 'invalid_checkpoint',
});

export class CompanySiteIngestionError extends Error {
  constructor(code, message, options = {}) {
    super(message, options);
    this.name = 'CompanySiteIngestionError';
    this.code = code;
  }
}

export function normalizeCheckpoint(value) {
  if (value === null || value === undefined || value === '') {
    return new Date(0).toISOString();
  }
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    throw new CompanySiteIngestionError(
      COMPANY_SITE_ERROR_CODES.invalid_checkpoint,
      `Invalid checkpoint timestamp: ${value}`
    );
  }
  return date.toISOString();
}

function parseEmploymentTypes(raw) {
  const input = Array.isArray(raw) ? raw : (raw ? [raw] : []);
  const normalized = input.map((v) => {
    const s = String(v).toLowerCase().replace(/[^a-z0-9_-]/gu, '');
    if (s.includes('full')) return 'full-time';
    if (s.includes('part')) return 'part-time';
    if (s.includes('intern')) return 'internship';
    if (s.includes('contract') || s.includes('temp')) return 'contract';
    return s;
  }).filter((v) => /^[a-z][a-z0-9_-]{0,63}$/u.test(v));

  return [...new Set(normalized)].sort();
}

function inferWorkplace(raw) {
  const locType = String(raw.jobLocationType ?? raw.workplaceType ?? '').toLowerCase();
  if (locType === 'telecommute' || locType === 'remote') return 'remote';
  if (locType === 'hybrid') return 'hybrid';
  if (locType === 'onsite' || locType === 'in_office') return 'onsite';

  const loc = String(raw.location ?? raw.jobLocation ?? '').toLowerCase();
  const desc = String(raw.description ?? '').toLowerCase();
  if (loc.includes('remote') || desc.includes('100% remote') || desc.includes('fully remote')) return 'remote';
  if (loc.includes('hybrid') || desc.includes('hybrid work')) return 'hybrid';
  if (loc) return 'onsite';
  return 'unknown';
}

function extractLocation(jobLocation) {
  if (!jobLocation) return null;
  if (typeof jobLocation === 'string') return jobLocation.trim() || null;
  if (Array.isArray(jobLocation)) {
    const locs = jobLocation.map(extractLocation).filter(Boolean);
    return locs.length > 0 ? locs.join(', ') : null;
  }
  if (typeof jobLocation === 'object') {
    const addr = jobLocation.address;
    if (typeof addr === 'string') return addr.trim() || null;
    if (typeof addr === 'object' && addr !== null) {
      const parts = [addr.addressLocality, addr.addressRegion, addr.addressCountry].filter(Boolean);
      if (parts.length > 0) return parts.join(', ');
    }
    if (jobLocation.name && typeof jobLocation.name === 'string') return jobLocation.name.trim();
  }
  return null;
}

function parseJsonLdPostings(html) {
  const postings = [];
  const regex = /<script\s+[^>]*type=["']application\/ld\+json["'][^>]*>([\s\S]*?)<\/script>/giu;
  let match;
  while ((match = regex.exec(html)) !== null) {
    try {
      const json = JSON.parse(match[1]);
      const items = Array.isArray(json) ? json : (json['@graph'] ?? [json]);
      for (const item of items) {
        if (!item) continue;
        const type = item['@type'];
        const isJobPosting = type === 'JobPosting' || (Array.isArray(type) && type.includes('JobPosting'));
        if (isJobPosting) {
          postings.push(item);
        }
      }
    } catch {
      // ignore JSON parse errors in malformed script tags
    }
  }
  return postings;
}

function parseHtmlFallback(html, companyUrl) {
  const items = [];
  const linkRegex = /<a\s+[^>]*href=["']([^"']+)["'][^>]*>(.*?)<\/a>/giu;
  let match;
  const seenUrls = new Set();
  while ((match = linkRegex.exec(html)) !== null) {
    const href = match[1];
    const text = match[2].replace(/<[^>]+>/gu, '').trim();
    if (!href || !text || text.length < 3) continue;

    const lowerHref = href.toLowerCase();
    const isJobLink = lowerHref.includes('/job') || lowerHref.includes('/career') || lowerHref.includes('/position') || lowerHref.includes('jobid=') || lowerHref.includes('gh_jid=');
    if (isJobLink) {
      let absoluteUrl;
      try {
        absoluteUrl = new URL(href, companyUrl).href;
      } catch {
        continue;
      }
      if (seenUrls.has(absoluteUrl)) continue;
      seenUrls.add(absoluteUrl);

      items.push({
        title: text,
        url: absoluteUrl,
        description: text,
        _fallback: true,
      });
    }
  }
  return items;
}

export function createCompanySiteAdapter(options = {}) {
  const fetchImpl = options.fetch ?? globalThis.fetch;
  if (typeof fetchImpl !== 'function') {
    throw new CompanySiteIngestionError(
      COMPANY_SITE_ERROR_CODES.invalid_config,
      'options.fetch must be a function'
    );
  }

  const companies = options.companies;
  if (!Array.isArray(companies) || companies.length === 0) {
    throw new CompanySiteIngestionError(
      COMPANY_SITE_ERROR_CODES.invalid_config,
      'options.companies must be a non-empty array'
    );
  }

  const activeCompanies = companies.filter((c) => c && c.enabled !== false).map((c) => Object.freeze({ ...c }));
  if (activeCompanies.length === 0) {
    throw new CompanySiteIngestionError(
      COMPANY_SITE_ERROR_CODES.invalid_config,
      'options.companies must contain at least one enabled company'
    );
  }

  const timeoutMs = options.timeoutMs > 0 ? options.timeoutMs : COMPANY_SITE_DEFAULT_TIMEOUT_MS;
  const maxResponseBytes = options.maxResponseBytes > 0 ? options.maxResponseBytes : COMPANY_SITE_DEFAULT_MAX_RESPONSE_BYTES;
  let snapshotCache = null;
  const getNow = () => (typeof options.now === 'function' ? options.now() : new Date().toISOString());

  function buildRequest({ page = 0, limit = 100, checkpoint = null, windowEnd = null } = {}) {
    const cp = checkpoint === null ? null : normalizeCheckpoint(checkpoint);
    const end = windowEnd === null ? null : normalizeCheckpoint(windowEnd);
    if (cp && end && cp > end) {
      throw new CompanySiteIngestionError(
        COMPANY_SITE_ERROR_CODES.invalid_config,
        'checkpoint must not be later than windowEnd'
      );
    }

    const first = activeCompanies[0];
    const url = first.careerUrl || `https://${first.hostname}`;
    const body = {
      companies: activeCompanies.map((company) => company.id ?? company.hostname),
    };
    const payload = {
      url,
      page,
      limit,
      checkpoint: cp,
      windowEnd: end,
      method: 'GET',
      body,
    };
    return Object.freeze({
      ...payload,
      headers: Object.freeze({ accept: 'text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8' }),
      companies: Object.freeze(activeCompanies.map((company) => Object.freeze({ ...company }))),
      requestSha256: sha256Canonical({ source: COMPANY_SITE_SOURCE, ...payload }),
    });
  }

  async function fetchPage({ request, mode } = {}) {
    if (!request || !Array.isArray(request.companies)) {
      throw new CompanySiteIngestionError(
        COMPANY_SITE_ERROR_CODES.invalid_config,
        'Invalid request object passed to fetchPage'
      );
    }

    const cacheKey = sha256Canonical({
      companies: request.companies,
      checkpoint: request.checkpoint,
      windowEnd: request.windowEnd,
    });
    if (!snapshotCache || snapshotCache.key !== cacheKey) {
      snapshotCache = {
        key: cacheKey,
        promise: (async () => {
          const combined = [];
          for (const company of request.companies) {
            const source = (company.source || 'custom').toLowerCase();
            if (source === 'greenhouse' || source === 'ashby') {
              try {
                const module = source === 'greenhouse'
                  ? await import('./greenhouse.mjs')
                  : await import('./ashby.mjs');
                const board = company.boardToken || company.boardName || company.id;
                const delegate = source === 'greenhouse'
                  ? module.createGreenhouseAdapter({
                    boardToken: board,
                    fetch: fetchImpl,
                    timeoutMs,
                    maxResponseBytes,
                    now: getNow,
                  })
                  : module.createAshbyAdapter({
                    boardName: board,
                    fetch: fetchImpl,
                    timeoutMs,
                    maxResponseBytes,
                    now: getNow,
                  });
                const items = [];
                let totalResults = null;
                for (let page = 0; page < 1_000; page += 1) {
                  const subRequest = delegate.buildRequest({
                    page,
                    limit: request.limit,
                    checkpoint: request.checkpoint,
                    windowEnd: request.windowEnd,
                  });
                  const subPage = await delegate.fetchPage({ request: subRequest, mode });
                  if (totalResults === null) totalResults = subPage.totalResults;
                  if (subPage.totalResults !== totalResults) {
                    throw new Error('Board total changed while reading its snapshot');
                  }
                  items.push(...subPage.items);
                  if (items.length >= totalResults) break;
                  if (subPage.items.length === 0 || page === 999) {
                    throw new Error('Board pagination did not reach its reported total');
                  }
                }
                combined.push(...items.map((item) => Object.freeze({
                  ...item,
                  _company: company,
                  _atsSource: source,
                })));
                continue;
              } catch (error) {
                if (error.code === 'BOARD_NOT_FOUND') {
                  throw new CompanySiteIngestionError(COMPANY_SITE_ERROR_CODES.board_not_found, error.message);
                }
                if (error instanceof CompanySiteIngestionError) throw error;
                throw new CompanySiteIngestionError(
                  COMPANY_SITE_ERROR_CODES.network_error,
                  `${source} delegation failed: ${error.message}`
                );
              }
            }

            const targetUrl = company.careerUrl || `https://${company.hostname}`;
            const controller = new AbortController();
            const timer = setTimeout(() => controller.abort(), timeoutMs);
            let response;
            try {
              response = await fetchImpl(targetUrl, {
                method: 'GET',
                headers: request.headers,
                signal: controller.signal,
              });
            } catch (error) {
              throw new CompanySiteIngestionError(
                COMPANY_SITE_ERROR_CODES.network_error,
                `Network fetch failed for company site ${targetUrl}: ${error.message}`
              );
            } finally {
              clearTimeout(timer);
            }
            if (response.status === 404) {
              throw new CompanySiteIngestionError(
                COMPANY_SITE_ERROR_CODES.board_not_found,
                `Career page not found (404): ${targetUrl}`
              );
            }
            if (!response.ok) {
              throw new CompanySiteIngestionError(
                COMPANY_SITE_ERROR_CODES.network_error,
                `HTTP ${response.status} fetching career page: ${targetUrl}`
              );
            }

            let html;
            try {
              if (typeof response.arrayBuffer === 'function') {
                const bytes = await response.arrayBuffer();
                if (bytes.byteLength > maxResponseBytes) {
                  throw new CompanySiteIngestionError(
                    COMPANY_SITE_ERROR_CODES.network_error,
                    'Response body exceeds maxResponseBytes'
                  );
                }
                html = new TextDecoder().decode(bytes);
              } else {
                html = await response.text();
                if (Buffer.byteLength(html) > maxResponseBytes) {
                  throw new CompanySiteIngestionError(
                    COMPANY_SITE_ERROR_CODES.network_error,
                    'Response body exceeds maxResponseBytes'
                  );
                }
              }
            } catch (error) {
              if (error instanceof CompanySiteIngestionError) throw error;
              throw new CompanySiteIngestionError(
                COMPANY_SITE_ERROR_CODES.invalid_html,
                `Failed to read HTML response: ${error.message}`
              );
            }
            if (typeof html !== 'string' || html.trim() === '') {
              throw new CompanySiteIngestionError(
                COMPANY_SITE_ERROR_CODES.invalid_html,
                `Empty HTML response from ${targetUrl}`
              );
            }

            let postings = parseJsonLdPostings(html);
            if (postings.length === 0) postings = parseHtmlFallback(html, targetUrl);
            const checkpointTime = request.checkpoint ? Date.parse(request.checkpoint) : null;
            const windowEndTime = request.windowEnd ? Date.parse(request.windowEnd) : null;
            combined.push(...postings
              .filter((item) => {
                const published = Date.parse(item.dateModified || item.datePosted || '');
                if (!Number.isFinite(published)) return true;
                if (checkpointTime !== null && published < checkpointTime) return false;
                return windowEndTime === null || published <= windowEndTime;
              })
              .map((item) => Object.freeze({
                ...item,
                _company: company,
                _sourceUrl: targetUrl,
              })));
          }

          const unique = new Map();
          for (const item of combined) {
            const company = item._company?.id ?? item._company?.hostname ?? '';
            const identity = item.id ?? item.jobId ?? item.identifier?.value
              ?? item.applyUrl ?? item.jobUrl ?? item.url ?? `${company}:${item.title ?? item.name ?? ''}`;
            const key = `${item._atsSource ?? item._company?.source ?? 'custom'}:${identity}`;
            if (!unique.has(key)) unique.set(key, item);
          }
          return Object.freeze({
            items: Object.freeze([...unique.values()]),
            receivedAt: getNow(),
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
      throw new TypeError('raw item must be an object');
    }

    const companyMeta = raw._company ?? {};
    const companyName = String(
      raw.hiringOrganization?.name ||
      raw.companyName ||
      raw.company ||
      companyMeta.id ||
      companyMeta.hostname ||
      'Unknown Company'
    ).trim();

    const title = String(raw.title || raw.name || raw.jobTitle || 'Untitled Position').trim();
    const rawListingUrl = raw.url || raw.absolute_url || raw.jobUrl || raw.hostedUrl || raw.applyUrl || companyMeta.careerUrl || (companyMeta.hostname ? `https://${companyMeta.hostname}` : null);
    const rawAppUrl = raw.applyUrl || raw.url || raw.absolute_url || rawListingUrl;

    const canonicalListingUrl = rawListingUrl ? canonicalizeJobUrl(rawListingUrl) : null;
    const canonicalApplicationUrl = rawAppUrl ? canonicalizeJobUrl(rawAppUrl) : null;

    const ats = classifyAts(canonicalApplicationUrl);
    let atsKind = ats.kind;
    if (atsKind === 'unknown') {
      if (raw._atsSource) atsKind = raw._atsSource;
      else if (companyMeta.source && companyMeta.source !== 'custom') atsKind = companyMeta.source;
      else atsKind = 'custom';
    }

    const sourceJobId = raw.id ? String(raw.id) : (raw.identifier?.value ? String(raw.identifier.value) : null);
    const atsIdentifier = ats.identifier ?? (sourceJobId ? `${companyMeta.id || companyName}:${sourceJobId}` : null);

    const locationName = extractLocation(raw.jobLocation || raw.location);
    const workplace = inferWorkplace(raw);
    const employment = parseEmploymentTypes(raw.employmentType || raw.employmentTypes);

    const rawDescription = String(raw.descriptionHtml || raw.description || raw.content || raw.summary || '').trim();

    const postedAtDate = raw.datePosted || raw.first_published || raw.publishedAt || raw.postedAt || raw.createdAt;
    const sourcePostedAt = postedAtDate ? new Date(postedAtDate).toISOString() : null;

    const updatedAtDate = raw.dateModified || raw.updated_at || raw.updatedAt;
    const sourceUpdatedAt = updatedAtDate ? new Date(updatedAtDate).toISOString() : null;

    const nowIso = getNow();
    const discoveredAt = observedAt ? new Date(observedAt).toISOString() : nowIso;

    const base = {
      schema: 'normalized-job-v1',
      source: COMPANY_SITE_SOURCE,
      sourceJobId,
      canonicalListingUrl,
      canonicalApplicationUrl,
      atsKind,
      atsIdentifier,
      title,
      company: companyName,
      location: locationName,
      workplaceType: workplace,
      employmentTypes: employment,
      description: rawDescription,
      descriptionSha256: sha256Text(rawDescription),
      sourcePostedAt,
      sourceUpdatedAt,
      discoveredAt,
      availabilityState: 'open',
      freshnessState: 'current',
      rawPayloadPath: (() => {
        const path = rawPayloadPath || `raw/company-site/${companyMeta.id || 'default'}.json`;
        return path.startsWith('/') ? path : `/${path}`;
      })(),
      rawPayloadSha256: rawPayloadSha256 || sha256Text(JSON.stringify(raw)),
    };

    const eligibility = classifyEligibility(base);
    const withEligibility = { ...base, ...eligibility };
    const identity = deriveDedupeIdentity(withEligibility);

    return validateNormalizedJob({
      ...withEligibility,
      dedupeIdentityKind: identity.kind,
      dedupeIdentityKey: identity.key,
      dedupeReviewRequired: identity.reviewRequired,
    });
  }

  return Object.freeze({
    source: COMPANY_SITE_SOURCE,
    requiresCreditReconciliation: false,
    buildRequest,
    fetchPage,
    normalizeJob,
    normalizeCheckpoint,
  });
}

export default createCompanySiteAdapter;
