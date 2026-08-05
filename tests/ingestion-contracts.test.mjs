import assert from 'node:assert/strict';
import crypto from 'node:crypto';
import test from 'node:test';
import {
  NORMALIZED_JOB_SCHEMA,
  SOURCE_SYNC_RESULT_SCHEMA,
  canonicalJson,
  canonicalizeJobUrl,
  classifyAts,
  classifyEligibility,
  deriveDedupeIdentity,
  validateNormalizedJob,
  validateSourceSyncResult,
} from '../src/ingestion/contracts.mjs';

const DIGEST = 'a'.repeat(64);
const DESCRIPTION_DIGEST = crypto.createHash('sha256').update('description', 'utf8').digest('hex');

function normalized(overrides = {}) {
  return {
    schema: NORMALIZED_JOB_SCHEMA,
    source: 'fixture',
    sourceJobId: 'job-1',
    canonicalListingUrl: 'https://example.test/jobs/job-1',
    canonicalApplicationUrl: 'https://boards.greenhouse.io/acme/jobs/1',
    atsKind: 'greenhouse',
    atsIdentifier: 'acme:1',
    title: 'Role',
    company: 'Company',
    location: 'Remote',
    workplaceType: 'remote',
    employmentTypes: ['full_time'],
    description: 'description',
    descriptionSha256: DESCRIPTION_DIGEST,
    sourcePostedAt: '2026-08-01T00:00:00.000Z',
    sourceUpdatedAt: '2026-08-02T00:00:00.000Z',
    discoveredAt: '2026-08-02T00:00:00.000Z',
    availabilityState: 'open',
    freshnessState: 'current',
    eligibilityState: 'eligible',
    eligibilityReasonCodes: [],
    priority: 835,
    dedupeIdentityKind: 'ats',
    dedupeIdentityKey: 'greenhouse:acme:1',
    dedupeReviewRequired: false,
    rawPayloadPath: '/private/payload/fixture/a.json',
    rawPayloadSha256: DIGEST,
    ...overrides,
  };
}

test('canonical URL normalization removes credentials and tracking state', () => {
  assert.equal(
    canonicalizeJobUrl('HTTPS://Example.test:443/jobs/1/?utm_source=mail&b=2&a=1#fragment'),
    'https://example.test/jobs/1?a=1&b=2',
  );
  assert.throws(() => canonicalizeJobUrl('ftp://example.test/jobs/1'), /E_URL_SCHEME/);
  assert.throws(() => canonicalizeJobUrl('https://user:pass@example.test/jobs/1'), /E_URL_CREDENTIALS/);
});

test('ATS matching is boundary-aware', () => {
  assert.deepEqual(classifyAts('https://boards.greenhouse.io/acme/jobs/1'), { kind: 'greenhouse', identifier: 'acme:1' });
  assert.deepEqual(classifyAts('https://jobs.ashbyhq.com/acme/role-123'), { kind: 'ashby', identifier: 'acme:role-123' });
  assert.deepEqual(classifyAts('https://jobs.lever.co/acme/role-456'), { kind: 'lever', identifier: 'acme:role-456' });
  assert.deepEqual(classifyAts('https://boards.greenhouse.io.evil.test/acme/jobs/1'), { kind: 'unknown', identifier: null });
  assert.deepEqual(classifyAts(null), { kind: 'unknown', identifier: null });
});

test('normalized validation clones and freezes exact records', () => {
  const input = normalized();
  const output = validateNormalizedJob(input);
  assert.notEqual(output, input);
  assert(Object.isFrozen(output));
  assert(Object.isFrozen(output.employmentTypes));
  assert.throws(() => validateNormalizedJob({ ...input, extra: true }), /E_SCHEMA_UNKNOWN_KEY/);
  assert.throws(() => validateNormalizedJob({ ...input, eligibilityReasonCodes: ['stale', 'stale'] }), /E_SCHEMA_ARRAY/);
});

test('identity precedence and deterministic eligibility are explicit', () => {
  assert.deepEqual(deriveDedupeIdentity(normalized()), { kind: 'ats', key: 'greenhouse:acme:1', reviewRequired: false });
  assert.deepEqual(deriveDedupeIdentity(normalized({ source: 'other', sourceJobId: 'different' })), { kind: 'ats', key: 'greenhouse:acme:1', reviewRequired: false });
  const fallbackIdentity = deriveDedupeIdentity(normalized({ sourceJobId: null, atsKind: 'unknown', atsIdentifier: null, canonicalApplicationUrl: null }));
  assert.equal(fallbackIdentity.kind, 'review_fingerprint');
  assert.match(fallbackIdentity.key, /^[0-9a-f]{64}$/);
  assert.equal(fallbackIdentity.reviewRequired, true);
  assert.deepEqual(classifyEligibility({ ...normalized(), availabilityState: 'closed' }), {
    eligibilityState: 'ineligible',
    eligibilityReasonCodes: ['closed'],
    priority: 0,
  });
  assert.deepEqual(classifyEligibility({ ...normalized(), atsKind: 'unknown', freshnessState: 'unverified', description: '' }), {
    eligibilityState: 'review',
    eligibilityReasonCodes: ['missing_description', 'unknown_ats', 'unverified_freshness'],
    priority: 260,
  });
});

test('source sync result validation rejects raw or unknown fields', () => {
  const result = {
    schema: SOURCE_SYNC_RESULT_SCHEMA,
    syncRunId: null,
    source: 'fixture',
    profile: 'profile',
    mode: 'preview',
    state: 'previewed',
    startedAt: '2026-08-02T00:00:00.000Z',
    finishedAt: '2026-08-02T00:00:01.000Z',
    checkpointBefore: null,
    checkpointAfter: null,
    pagesFetched: 1,
    requestCount: 1,
    jobsSeen: 1,
    jobsInserted: 0,
    jobsUpdated: 0,
    jobsUnchanged: 1,
    dedupeGroupsTouched: 0,
    queueRowsInserted: 0,
    estimatedCredits: 0,
    reportedCredits: null,
    failureClass: null,
    reasonCode: null,
  };
  const output = validateSourceSyncResult(result);
  assert(Object.isFrozen(output));
  assert.throws(() => validateSourceSyncResult({ ...result, raw: {} }), /E_SCHEMA_UNKNOWN_KEY/);
  assert.throws(() => validateSourceSyncResult({ ...result, syncRunId: 0 }), /E_SCHEMA_INTEGER/);
  assert.equal(canonicalJson({ b: 2, a: 1 }), '{"a":1,"b":2}');
  const previewFailure = validateSourceSyncResult({
    ...result,
    state: 'failed',
    pagesFetched: 0,
    jobsSeen: 0,
    jobsUnchanged: 0,
    failureClass: 'terminal',
    reasonCode: 'preview_failed',
  });
  assert.equal(previewFailure.syncRunId, null);
  assert.throws(() => validateSourceSyncResult({
    ...result,
    mode: 'paid',
    state: 'succeeded',
  }), /E_PAID_RUN_ID/);
  assert.throws(() => validateSourceSyncResult({
    ...result,
    state: 'failed',
    failureClass: null,
    reasonCode: null,
  }), /E_SYNC_FAILURE_DETAIL/);
});
