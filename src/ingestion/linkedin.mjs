import {
  canonicalizeJobUrl,
  classifyAts,
  classifyEligibility,
  deriveDedupeIdentity,
  sha256Canonical,
  sha256Text,
  validateNormalizedJob,
} from './contracts.mjs';

export const LINKEDIN_SOURCE = 'linkedin';
export const LINKEDIN_DEFAULT_BASE_URL = 'https://www.linkedin.com';
export const LINKEDIN_DEFAULT_TIMEOUT_MS = 30_000;
export const LINKEDIN_DEFAULT_MAX_RESPONSE_BYTES = 10 * 1024 * 1024;

export const LINKEDIN_ERROR_CODES = Object.freeze({
  account_health: 'account_health',
  invalid_request: 'invalid_request',
  fetch_failed: 'fetch_failed',
  invalid_html: 'invalid_html',
});

export class LinkedInIngestionError extends Error {
  constructor(message, code = 'account_health', options = {}) {
    super(message, options);
    this.name = 'LinkedInIngestionError';
    this.code = code;
    this.reason = options.reason ?? message;
  }
}

export function normalizeCheckpoint(value) {
  if (!value) {
    return new Date(0).toISOString();
  }
  const date = value instanceof Date ? value : new Date(value);
  if (!Number.isFinite(date.getTime())) {
    return new Date(0).toISOString();
  }
  return date.toISOString();
}

function inferWorkplaceType(location, title, description) {
  const combined = `${location ?? ''} ${title ?? ''} ${description ?? ''}`.toLowerCase();
  if (combined.includes('remote')) return 'remote';
  if (combined.includes('hybrid')) return 'hybrid';
  if (
    combined.includes('on-site') ||
    combined.includes('onsite') ||
    combined.includes('in-office') ||
    combined.includes('in office')
  ) {
    return 'onsite';
  }
  if (location && location.trim()) return 'onsite';
  return 'unknown';
}

function inferEmploymentTypes(title, description) {
  const combined = `${title ?? ''} ${description ?? ''}`.toLowerCase();
  const types = [];
  if (combined.includes('full-time') || combined.includes('full time')) types.push('full-time');
  if (combined.includes('part-time') || combined.includes('part time')) types.push('part-time');
  if (combined.includes('contract') || combined.includes('contractor')) types.push('contract');
  if (combined.includes('intern') || combined.includes('internship')) types.push('internship');
  return types.sort();
}

function cleanLinkedInUrl(rawUrl, baseUrl = LINKEDIN_DEFAULT_BASE_URL) {
  if (!rawUrl) return null;
  let fullUrl = rawUrl.trim();
  if (fullUrl.startsWith('/')) {
    fullUrl = `${baseUrl}${fullUrl}`;
  }
  try {
    const urlObj = new URL(fullUrl);
    // Strip common LinkedIn tracking parameters
    const trackingParams = ['refId', 'trackingId', 'position', 'pageNum', 'trk', 'currentJobId'];
    for (const param of trackingParams) {
      urlObj.searchParams.delete(param);
    }
    return canonicalizeJobUrl(urlObj.toString());
  } catch {
    return canonicalizeJobUrl(fullUrl);
  }
}

function extractJobIdFromUrl(urlStr) {
  if (!urlStr) return null;
  const match = /\/jobs\/view\/(?:[^\/]+-)?(\d+)/i.exec(urlStr) || /currentJobId=(\d+)/i.exec(urlStr);
  return match ? match[1] : null;
}

function detectAccountHealthStop(status, html) {
  if (status === 401) {
    return 'HTTP 401 Unauthorized: Session expired or unauthenticated';
  }
  if (status === 403) {
    return 'HTTP 403 Forbidden: Access restricted';
  }
  if (status === 429) {
    return 'HTTP 429 Rate Limited: Too many requests';
  }
  if (status < 200 || status >= 300) {
    return `HTTP ${status} Response`;
  }

  if (typeof html !== 'string') return null;
  const lowerHtml = html.toLowerCase();

  // Login / Auth wall detection
  if (
    lowerHtml.includes('id="username"') ||
    lowerHtml.includes('name="session_key"') ||
    lowerHtml.includes('action="/uas/login-submit"') ||
    lowerHtml.includes('class="login-form"') ||
    lowerHtml.includes('/uas/login') ||
    lowerHtml.includes('/authwall') ||
    lowerHtml.includes('sign in to linkedin') ||
    lowerHtml.includes('join linkedin') ||
    lowerHtml.includes('input name="session_key"') ||
    lowerHtml.includes('sign-in-form')
  ) {
    return 'Authentication required / Login wall detected';
  }

  // CAPTCHA / Bot challenge detection
  if (
    lowerHtml.includes('g-recaptcha') ||
    lowerHtml.includes('cf-turnstile') ||
    lowerHtml.includes('px-captcha') ||
    lowerHtml.includes('captcha-internal') ||
    lowerHtml.includes('security verification') ||
    lowerHtml.includes('verify you are human') ||
    lowerHtml.includes('please solve this puzzle') ||
    lowerHtml.includes('bot detection')
  ) {
    return 'CAPTCHA / Bot challenge detected';
  }

  // Identity / Security Checkpoint
  if (
    lowerHtml.includes('/checkpoint/challenge/') ||
    lowerHtml.includes('verify your identity') ||
    lowerHtml.includes('enter code sent') ||
    lowerHtml.includes('confirm phone number') ||
    lowerHtml.includes('email verification') ||
    lowerHtml.includes('security checkpoint')
  ) {
    return 'Identity / Security verification checkpoint required';
  }

  // Unusual activity warning
  if (
    lowerHtml.includes('unusual activity') ||
    lowerHtml.includes('automated behavior') ||
    lowerHtml.includes('suspicious activity') ||
    lowerHtml.includes('unusual traffic')
  ) {
    return 'Unusual activity warning detected';
  }

  // Restricted / Suspended / Limited account
  if (
    lowerHtml.includes('account has been restricted') ||
    lowerHtml.includes('account suspended') ||
    lowerHtml.includes('temporarily restricted') ||
    lowerHtml.includes('account limited') ||
    lowerHtml.includes('restricted account')
  ) {
    return 'Account restricted or suspended';
  }

  // Robots / Policy / Consent wall
  if (
    lowerHtml.includes('cookie preferences') ||
    lowerHtml.includes('/legal/cookie-policy') ||
    lowerHtml.includes('access denied') ||
    lowerHtml.includes('please agree to terms')
  ) {
    return 'Robots / Policy / Cookie consent wall detected';
  }

  // Unexpected DOM / Auth State check:
  // If page does NOT contain any job listing markers AND contains generic auth/error structural hints
  const hasJobMarkers =
    lowerHtml.includes('jobs-search-results') ||
    lowerHtml.includes('job-card-container') ||
    lowerHtml.includes('job-card-list') ||
    lowerHtml.includes('/jobs/view/') ||
    lowerHtml.includes('data-job-id') ||
    lowerHtml.includes('jobs-search__results-list');

  const hasUnexpectedState =
    lowerHtml.includes('something went wrong') ||
    lowerHtml.includes('page not found') ||
    lowerHtml.includes('error-container') ||
    !hasJobMarkers;

  if (!hasJobMarkers && hasUnexpectedState) {
    return 'Unexpected DOM or auth state detected';
  }

  return null;
}

function parseLinkedInHtml(html, baseUrl = LINKEDIN_DEFAULT_BASE_URL) {
  const items = [];
  let totalResults = null;

  // Try extracting total results count
  const totalMatch =
    /(\d[\d,]*)\s+(?:results|jobs)/i.exec(html) ||
    /data-total-results="(\d+)"/i.exec(html) ||
    /class="[^"]*results-context-header__job-count[^"]*">?\s*([\d,]+)/i.exec(html);
  if (totalMatch) {
    totalResults = parseInt(totalMatch[1].replace(/,/g, ''), 10);
  }

  // Parse job cards / items from HTML
  // Match card blocks or individual job links
  const cardRegex = /<li[^>]*class="[^"]*(?:job-card-container|jobs-search-results__list-item|result-card|job-search-card)[^"]*"[\s\S]*?<\/li>/gi;
  let matches = [...html.matchAll(cardRegex)];

  // Fallback: search for direct anchor tags pointing to /jobs/view/
  if (matches.length === 0) {
    const linkRegex = /<a[^>]+href="([^"]*\/jobs\/view\/[^"]*)"[^>]*>([\s\S]*?)<\/a>/gi;
    const linkMatches = [...html.matchAll(linkRegex)];
    for (const match of linkMatches) {
      const linkHref = match[1];
      const linkText = match[2].replace(/<[^>]+>/g, '').trim();
      const canonicalListingUrl = cleanLinkedInUrl(linkHref, baseUrl);
      if (canonicalListingUrl) {
        const jobId = extractJobIdFromUrl(canonicalListingUrl);
        items.push({
          id: jobId,
          jobId,
          title: linkText || 'Untitled Position',
          company: 'Unknown Company',
          location: null,
          listingUrl: linkHref,
          canonicalListingUrl,
          applicationUrl: linkHref,
          canonicalApplicationUrl: canonicalListingUrl,
          postedDateText: null,
          descriptionSnippet: '',
        });
      }
    }
  } else {
    for (const match of matches) {
      const block = match[0];
      const hrefMatch = /href="([^"]*\/jobs\/view\/[^"]*)"/i.exec(block) || /data-job-id="(\d+)"/i.exec(block);
      let rawHref = hrefMatch ? (hrefMatch[1].startsWith('data-job-id') ? `/jobs/view/${hrefMatch[1].split('"')[1]}/` : hrefMatch[1]) : null;
      
      if (!rawHref) {
        const idAttrMatch = /data-entity-urn="urn:li:jobPosting:(\d+)"/i.exec(block) || /data-job-id="(\d+)"/i.exec(block);
        if (idAttrMatch) {
          rawHref = `/jobs/view/${idAttrMatch[1]}/`;
        }
      }

      if (!rawHref) continue;

      const canonicalListingUrl = cleanLinkedInUrl(rawHref, baseUrl);
      const jobId = extractJobIdFromUrl(canonicalListingUrl) || extractJobIdFromUrl(rawHref);

      // Title extraction
      const titleMatch =
        /class="[^"]*(?:job-card-list__title|base-search-card__title|job-card-container__link)[^"]*"[^>]*>([\s\S]*?)<\/[a-z0-9]+>/i.exec(block) ||
        /<a[^>]*aria-label="([^"]*)"[^>]*>/i.exec(block);
      const title = titleMatch ? titleMatch[1].replace(/<[^>]+>/g, '').trim() : 'Untitled Position';

      // Company extraction
      const companyMatch =
        /class="[^"]*(?:job-card-container__primary-description|job-card-container__company-name|base-search-card__subtitle)[^"]*"[^>]*>([\s\S]*?)<\/[a-z0-9]+>/i.exec(block) ||
        /<h4[^>]*class="[^"]*base-search-card__subtitle[^"]*"[^>]*>([\s\S]*?)<\/h4>/i.exec(block);
      const company = companyMatch ? companyMatch[1].replace(/<[^>]+>/g, '').trim() : 'Unknown Company';

      // Location extraction
      const locationMatch =
        /class="[^"]*(?:job-card-container__metadata-item|job-search-card__location)[^"]*"[^>]*>([\s\S]*?)<\/[a-z0-9]+>/i.exec(block);
      const location = locationMatch ? locationMatch[1].replace(/<[^>]+>/g, '').trim() : null;

      // Posted Date extraction
      const timeMatch =
        /<time[^>]*datetime="([^"]*)"[^>]*>([\s\S]*?)<\/time>/i.exec(block) ||
        /class="[^"]*(?:job-search-card__listdate|job-card-container__listed-time)[^"]*"[^>]*>([\s\S]*?)<\/[a-z0-9]+>/i.exec(block);
      const postedDateText = timeMatch ? (timeMatch[1] || timeMatch[2]).replace(/<[^>]+>/g, '').trim() : null;

      // Description snippet extraction
      const snippetMatch =
        /class="[^"]*(?:job-card-list__snippet|base-search-card__snippet)[^"]*"[^>]*>([\s\S]*?)<\/[a-z0-9]+>/i.exec(block);
      const descriptionSnippet = snippetMatch ? snippetMatch[1].replace(/<[^>]+>/g, '').trim().slice(0, 1000) : '';

      const applyMatch = /<a[^>]+href="([^"]+)"[^>]*>([\s\S]*?apply[\s\S]*?)<\/a>/i.exec(block);
      const rawApplicationUrl = applyMatch ? applyMatch[1] : rawHref;
      const canonicalApplicationUrl = cleanLinkedInUrl(rawApplicationUrl, baseUrl);
      items.push({
        id: jobId,
        jobId,
        title: title || 'Untitled Position',
        company: company || 'Unknown Company',
        location: location || null,
        listingUrl: rawHref,
        canonicalListingUrl,
        applicationUrl: rawApplicationUrl,
        canonicalApplicationUrl,
        postedDateText,
        descriptionSnippet,
      });
    }
  }

  // Deduplicate by canonical listing URL
  const uniqueItems = [];
  const seenUrls = new Set();
  for (const item of items) {
    if (item.canonicalListingUrl && !seenUrls.has(item.canonicalListingUrl)) {
      seenUrls.add(item.canonicalListingUrl);
      uniqueItems.push(item);
    }
  }

  if (totalResults === null) {
    totalResults = uniqueItems.length;
  }

  return { items: uniqueItems, totalResults };
}

export function createLinkedInAdapter(options = {}) {
  const fetchImpl = options.browserFetch ?? null;
  if (fetchImpl !== null && typeof fetchImpl !== 'function') {
    throw new TypeError('options.browserFetch must be a function');
  }

  const baseUrl = (options.baseUrl ?? LINKEDIN_DEFAULT_BASE_URL).replace(/\/+$/u, '');
  const timeoutMs = options.timeoutMs > 0 ? options.timeoutMs : LINKEDIN_DEFAULT_TIMEOUT_MS;
  const maxResponseBytes = options.maxResponseBytes > 0 ? options.maxResponseBytes : LINKEDIN_DEFAULT_MAX_RESPONSE_BYTES;
  const clock = typeof options.now === 'function' ? options.now : () => new Date();

  function nowIso() {
    const d = clock();
    const date = d instanceof Date ? d : new Date(d);
    return date.toISOString();
  }

  function resolveSearchUrl(profile) {
    let urlStr = options.searchUrl ?? null;
    let queryStr = options.savedSearchQuery ?? null;

    if (!urlStr && !queryStr && profile) {
      if (typeof profile === 'string') {
        if (profile.startsWith('http://') || profile.startsWith('https://')) {
          urlStr = profile;
        } else {
          queryStr = profile;
        }
      } else if (typeof profile === 'object' && profile !== null) {
        urlStr = profile.searchUrl ?? null;
        queryStr = profile.savedSearchQuery ?? null;
      }
    }

    if (urlStr) return urlStr;
    if (queryStr) {
      return `${baseUrl}/jobs/search/?keywords=${encodeURIComponent(queryStr)}`;
    }
    throw new LinkedInIngestionError(
      'LinkedIn adapter requires searchUrl or savedSearchQuery',
      LINKEDIN_ERROR_CODES.invalid_request
    );
  }

  function buildRequest({ profile = 'default', page = 0, limit = 25, checkpoint = null, windowEnd = null } = {}) {
    const rawSearchUrl = resolveSearchUrl(profile);
    const cp = checkpoint === null ? null : normalizeCheckpoint(checkpoint);
    const end = windowEnd === null ? null : normalizeCheckpoint(windowEnd);

    if (cp && end && cp > end) {
      throw new LinkedInIngestionError('checkpoint must not be later than windowEnd', LINKEDIN_ERROR_CODES.invalid_request);
    }

    const urlObj = new URL(rawSearchUrl.startsWith('/') ? `${baseUrl}${rawSearchUrl}` : rawSearchUrl);
    const offset = Math.max(0, page) * Math.max(1, limit);
    urlObj.searchParams.set('start', String(offset));
    const paginatedUrl = urlObj.toString();
    const body = {};
    const payload = {
      url: paginatedUrl,
      page,
      limit,
      checkpoint: cp,
      windowEnd: end,
      method: 'GET',
      body,
    };

    return Object.freeze({
      ...payload,
      headers: Object.freeze({
        accept: 'text/html,application/xhtml+xml',
      }),
      requestSha256: sha256Canonical({ source: LINKEDIN_SOURCE, ...payload }),
    });
  }

  async function fetchPage({ request } = {}) {
    if (!request || typeof request.url !== 'string') {
      throw new LinkedInIngestionError('Invalid request object', LINKEDIN_ERROR_CODES.invalid_request);
    }
    if (typeof fetchImpl !== 'function') {
      throw new LinkedInIngestionError(
        'LinkedIn fetch requires an authenticated visible-browser bridge',
        LINKEDIN_ERROR_CODES.invalid_request
      );
    }

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
      throw new LinkedInIngestionError(
        `Network request failed: ${error.message}`,
        LINKEDIN_ERROR_CODES.fetch_failed
      );
    } finally {
      clearTimeout(timer);
    }

    let text;
    try {
      if (typeof response.arrayBuffer === 'function') {
        const bytes = await response.arrayBuffer();
        if (bytes.byteLength > maxResponseBytes) {
          throw new LinkedInIngestionError('Response exceeds maxResponseBytes', LINKEDIN_ERROR_CODES.fetch_failed);
        }
        text = new TextDecoder().decode(bytes);
      } else {
        text = await response.text();
        if (Buffer.byteLength(text) > maxResponseBytes) {
          throw new LinkedInIngestionError('Response exceeds maxResponseBytes', LINKEDIN_ERROR_CODES.fetch_failed);
        }
      }
    } catch (error) {
      if (error instanceof LinkedInIngestionError) throw error;
      throw new LinkedInIngestionError(
        `Failed to read response body: ${error.message}`,
        LINKEDIN_ERROR_CODES.fetch_failed
      );
    }

    // Check account health stop conditions
    const stopReason = detectAccountHealthStop(response.status, text);
    if (stopReason) {
      throw new LinkedInIngestionError(stopReason, LINKEDIN_ERROR_CODES.account_health, {
        reason: stopReason,
      });
    }

    const { items, totalResults } = parseLinkedInHtml(text, baseUrl);

    return Object.freeze({
      requestSha256: request.requestSha256,
      items: Object.freeze(items.map(Object.freeze)),
      totalResults,
      receivedAt: nowIso(),
      estimatedCredits: 0,
      reportedCredits: null,
    });
  }

  function normalizeJob(raw, { observedAt, rawPayloadPath, rawPayloadSha256 } = {}) {
    const obsAt = observedAt ? normalizeCheckpoint(observedAt) : nowIso();
    const jobId = raw.jobId ?? raw.id ?? null;
    const listingUrl = raw.listingUrl ?? raw.canonicalListingUrl ?? null;
    const applicationUrl = raw.applicationUrl ?? raw.canonicalApplicationUrl ?? listingUrl;
    const canonicalListingUrl = cleanLinkedInUrl(listingUrl, baseUrl);
    const canonicalApplicationUrl = cleanLinkedInUrl(applicationUrl, baseUrl);
    const classifiedAts = canonicalApplicationUrl ? classifyAts(canonicalApplicationUrl) : { kind: 'unknown', identifier: null };
    const title = String(raw.title ?? 'Untitled Position').trim();
    const company = String(raw.company ?? 'Unknown Company').trim();
    const location = raw.location ? String(raw.location).trim() : null;
    const description = String(raw.descriptionSnippet ?? raw.description ?? '').trim().slice(0, 1000);
    let sourcePostedAt = null;
    if (raw.postedDateText) {
      const parsed = new Date(raw.postedDateText);
      if (Number.isFinite(parsed.getTime())) sourcePostedAt = parsed.toISOString();
    }
    const base = {
      schema: 'normalized-job-v1',
      source: LINKEDIN_SOURCE,
      sourceJobId: jobId === null ? null : String(jobId),
      canonicalListingUrl,
      canonicalApplicationUrl,
      atsKind: classifiedAts.kind === 'unknown' ? 'linkedin' : classifiedAts.kind,
      atsIdentifier: classifiedAts.identifier ?? (jobId === null ? null : `linkedin:${jobId}`),
      title,
      company,
      location,
      workplaceType: inferWorkplaceType(location, title, description),
      employmentTypes: inferEmploymentTypes(title, description),
      description,
      descriptionSha256: sha256Text(description),
      sourcePostedAt,
      sourceUpdatedAt: null,
      discoveredAt: obsAt,
      availabilityState: 'open',
      freshnessState: 'current',
      rawPayloadPath: rawPayloadPath ?? '/linkedin/raw.json',
      rawPayloadSha256: rawPayloadSha256 ?? sha256Text(JSON.stringify(raw)),
    };
    const eligibility = classifyEligibility(base);
    const jobWithEligibility = { ...base, ...eligibility };
    const identity = deriveDedupeIdentity(jobWithEligibility);
    return validateNormalizedJob({
      ...jobWithEligibility,
      dedupeIdentityKind: identity.kind,
      dedupeIdentityKey: identity.key,
      dedupeReviewRequired: identity.reviewRequired,
    });
  }

  return Object.freeze({
    source: LINKEDIN_SOURCE,
    requiresCreditReconciliation: false,
    buildRequest,
    fetchPage,
    normalizeJob,
    normalizeCheckpoint,
  });
}

export default createLinkedInAdapter;
