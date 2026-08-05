import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import test from 'node:test';

import {
  createTheirStackAdapter,
  TheirStackError,
} from '../src/ingestion/theirstack.mjs';

const WINDOW_END = '2026-08-03T23:59:59.000Z';
const OBSERVED_AT = '2026-08-03T12:00:00.000Z';
const CREDIT_PERIOD = '2026-08-03T00:00:00Z';
const RAW_PATH = '/tmp/jobs-new/theirstack/abc.json';
const RAW_SHA = 'a'.repeat(64);

function item(id = 1, overrides = {}) {
  return {
    id,
    job_title: 'Software Engineer',
    company_object: { name: 'Acme' },
    url: `https://jobs.example.test/listing/${id}`,
    source_url: `https://source.example.test/job/${id}`,
    final_url: `https://boards.greenhouse.io/acme/jobs/${id}`,
    date_posted: '2026-08-02',
    date_reposted: null,
    discovered_at: '2026-08-03T10:00:00Z',
    description: 'Build reliable software.',
    closed_at: null,
    remote: true,
    hybrid: false,
    employment_statuses: ['full_time'],
    location: 'New York, NY',
    country: 'United States',
    country_code: 'US',
    country_codes: ['US'],
    locations: [],
    ...overrides,
  };
}

function response(payload, status = 200, headers = {}) {
  return {
    status,
    headers: { get: (name) => headers[String(name).toLowerCase()] ?? null },
    async json() {
      return payload;
    },
  };
}

function adapterWith(sequence, options = {}) {
  const calls = [];
  const responses = [...sequence];
  const adapter = createTheirStackAdapter({
    apiKey: 'test-key',
    fetch: async (url, init) => {
      calls.push({ url, init });
      const next = responses.shift();
      if (next instanceof Error) throw next;
      return next;
    },
    windowEnd: WINDOW_END,
    ...options,
  });
  return { adapter, calls };
}

test('profiles preserve current documented search semantics and immutable requests', () => {
  const { adapter } = adapterWith([]);
  const request = adapter.buildRequest({ profile: 'new_grad_cs', mode: 'paid', page: 0, limit: 25, includeTotals: true });
  assert.equal(request.body.posted_at_max_age_days, 7);
  assert.deepEqual(request.body.job_country_code_or, ['US']);
  assert.equal(request.body.company_type, 'direct_employer');
  assert.equal(request.body.is_closed, false);
  assert.deepEqual(request.body.order_by, [
    { field: 'date_posted', desc: true },
    { field: 'discovered_at', desc: true },
  ]);
  assert.ok(Object.isFrozen(request));
  assert.ok(Object.isFrozen(request.body));
  assert.throws(() => request.body.job_title_or.push('tamper'), TypeError);
  const rebuilt = adapter.buildRequest({ profile: 'new_grad_cs', mode: 'paid', page: 0, limit: 25, includeTotals: true });
  assert.equal(rebuilt.body.job_title_or.length, request.body.job_title_or.length);
  assert.equal('company_name_or' in rebuilt.body, false);
  assert.equal('company_domain_or' in rebuilt.body, false);
  assert.equal(adapter.normalizeCheckpoint('2026-08-03T10:00:00+00:00'), '2026-08-03T10:00:00.000Z');
});

test('construction query filters are allowlisted, canonical, and preview-safe', () => {
  const { adapter } = adapterWith([], {
    postedAtMaxAgeDays: 0,
    queryFilters: {
      job_title_or: ['Data scientist'],
      job_title_pattern_or: ['(?i)engineer'],
      url_domain_or: ['jobs.example.test'],
    },
  });
  const request = adapter.buildRequest({ mode: 'paid' });
  assert.equal(request.body.posted_at_max_age_days, 0);
  assert.deepEqual(request.body.job_title_or, ['Data scientist']);
  assert.deepEqual(request.body.job_title_pattern_or, ['(?i)engineer']);
  assert.deepEqual(request.body.url_domain_or, ['jobs.example.test']);
  assert.throws(() => createTheirStackAdapter({ queryFilters: { unsafe: ['x'] } }), TheirStackError);
  assert.throws(() => createTheirStackAdapter({ queryFilters: { job_title_or: ['z', 'a'] } }), TheirStackError);
  const { adapter: previewAdapter } = adapterWith([], { queryFilters: { company_name_or: ['Acme'] } });
  assert.throws(() => previewAdapter.buildRequest({ mode: 'preview' }), TheirStackError);
});

test('preview is exactly blur plus total plus limit one and costs zero', async () => {
  const { adapter, calls } = adapterWith([response({ data: [item()], metadata: { total_results: 1 } })]);
  const request = adapter.buildRequest({ profile: 'default', mode: 'preview', page: 0, limit: 99, includeTotals: false });
  assert.equal(request.limit, 1);
  assert.equal(request.body.blur_company_data, true);
  assert.equal(request.body.include_total_results, true);
  const result = await adapter.fetchPage({ request, mode: 'preview' });
  assert.equal(result.estimatedCredits, 1);
  assert.equal(result.reportedCredits, 0);
  assert.equal(result.totalResults, 1);
  assert.equal(calls.length, 1);
  assert.equal(calls[0].init.method, 'POST');
  assert.equal(calls[0].init.headers.authorization, 'Bearer test-key');
  assert.deepEqual(JSON.parse(calls[0].init.body), request.body);
});

test('preview accepts structurally bounded blurred items without treating them as paid jobs', async () => {
  const blurred = { id: 1, company_object: null, has_blurred_data: true };
  const { adapter } = adapterWith([response({ data: [blurred], metadata: { total_results: 1 } })]);
  const request = adapter.buildRequest({ mode: 'preview' });
  const result = await adapter.fetchPage({ request, mode: 'preview' });
  assert.equal(result.items.length, 1);

  const { adapter: paidAdapter } = adapterWith(
    [response({ data: [blurred], metadata: { total_results: 1 } })],
    { paidAuthorization: true },
  );
  const paidRequest = paidAdapter.buildRequest({ mode: 'paid', page: 0, limit: 1 });
  await assert.rejects(
    paidAdapter.fetchPage({ request: paidRequest, mode: 'paid' }),
    (error) => error.code === 'paid_ambiguous',
  );
});

test('paid fetch requires explicit authorization and never replays an ambiguous response', async () => {
  const { adapter, calls } = adapterWith([response({ error: { title: 'private' } }, 500)]);
  const request = adapter.buildRequest({ mode: 'paid', page: 0, limit: 1 });
  await assert.rejects(
    adapter.fetchPage({ request, mode: 'paid' }),
    (error) => error.code === 'paid_authorization',
  );
  assert.equal(calls.length, 0);
  const { adapter: authorizedAdapter, calls: authorizedCalls } = adapterWith(
    [response({ error: { title: 'private' } }, 500)],
    { paidAuthorization: true },
  );
  const authorizedRequest = authorizedAdapter.buildRequest({ mode: 'paid', page: 0, limit: 1 });
  await assert.rejects(
    authorizedAdapter.fetchPage({ request: authorizedRequest, mode: 'paid' }),
    (error) => error.code === 'paid_ambiguous' && !error.message.includes('private'),
  );
  assert.equal(authorizedCalls.length, 1);
});

test('preview retries bounded 429 and server failures only', async () => {
  const { adapter, calls } = adapterWith([
    response({ error: { description: 'private' } }, 429),
    response({ error: { description: 'private' } }, 503),
    response({ data: [item()], metadata: { total_results: 1 } }),
  ]);
  const request = adapter.buildRequest({ mode: 'preview' });
  const result = await adapter.fetchPage({ request, mode: 'preview' });
  assert.equal(result.items.length, 1);
  assert.equal(calls.length, 3);
});

test('preview retries honor Retry-After before the configured fallback', { timeout: 500 }, async () => {
  const { adapter, calls } = adapterWith([
    response({ error: { description: 'private' } }, 429, { 'retry-after': '0' }),
    response({ data: [item()], metadata: { total_results: 1 } }),
  ], { retryDelayMs: 2_000 });
  const request = adapter.buildRequest({ mode: 'preview' });
  const result = await adapter.fetchPage({ request, mode: 'preview' });
  assert.equal(result.items.length, 1);
  assert.equal(calls.length, 2);
});

test('authentication, account health, terminal, and paid ambiguity errors expose stable codes only', async () => {
  for (const [status, code] of [[401, 'authentication'], [402, 'account_health'], [422, 'terminal_validation']]) {
    const { adapter } = adapterWith([response({ secret: 'do-not-leak' }, status)], { paidAuthorization: true });
    const request = adapter.buildRequest({ mode: 'paid', page: 0, limit: 1 });
    await assert.rejects(
      adapter.fetchPage({ request, mode: 'paid' }),
      (error) => error.code === code && !error.message.includes('secret'),
    );
  }
  const { adapter } = adapterWith([new Error('private timeout detail')], { paidAuthorization: true });
  const request = adapter.buildRequest({ mode: 'paid', page: 0, limit: 1 });
  await assert.rejects(
    adapter.fetchPage({ request, mode: 'paid' }),
    (error) => error.code === 'paid_ambiguous' && !error.message.includes('private'),
  );
});

test('credit-truncated success responses fail as account health errors', async () => {
  const { adapter } = adapterWith([
    response({ data: [item()], metadata: { total_results: 3, truncated_results: 2 } }),
  ], { paidAuthorization: true });
  const request = adapter.buildRequest({ mode: 'paid', page: 0, limit: 3 });
  await assert.rejects(
    adapter.fetchPage({ request, mode: 'paid' }),
    (error) => error.code === 'account_health',
  );
});

test('later pages disable totals, retain checkpoint, and classify malformed paid responses as ambiguous', async () => {
  const { adapter } = adapterWith([
    response({ data: [item(1)], metadata: { total_results: 2, page: 0 } }),
    response({ data: [item(2)], metadata: { total_results: 2, page: 1 } }),
  ], { paidAuthorization: true });
  const first = adapter.buildRequest({ mode: 'paid', page: 0, limit: 1, checkpoint: '2026-08-02T00:00:00Z', windowEnd: WINDOW_END, includeTotals: true });
  const second = adapter.buildRequest({ mode: 'paid', page: 1, limit: 1, checkpoint: '2026-08-02T00:00:00Z', windowEnd: WINDOW_END, includeTotals: true });
  assert.equal(first.body.discovered_at_gte, '2026-08-02T00:00:00Z');
  assert.equal(first.body.discovered_at_lte, WINDOW_END);
  assert.equal(first.body.include_total_results, true);
  assert.equal(second.body.include_total_results, false);
  await adapter.fetchPage({ request: first, mode: 'paid' });
  await adapter.fetchPage({ request: second, mode: 'paid' });
  for (const badPayload of [
    { jobs: [], metadata: {} },
    { data: [{}], metadata: {} },
    { data: [item()], metadata: { total_results: 1.5 } },
    { data: [item()], metadata: { page: 4 } },
  ]) {
    const { adapter: badAdapter } = adapterWith([response(badPayload)], { paidAuthorization: true });
    const badRequest = badAdapter.buildRequest({ mode: 'paid', page: 0, limit: 1 });
    await assert.rejects(
      badAdapter.fetchPage({ request: badRequest, mode: 'paid' }),
      (error) => error.code === 'paid_ambiguous',
    );
  }
});

test('all listings are returned without one-company post-filtering', async () => {
  const { adapter } = adapterWith([
    response({ data: [item(1), item(2, { company_object: { name: 'Acme' } })], metadata: { total_results: 2 } }),
  ], { paidAuthorization: true });
  const request = adapter.buildRequest({ mode: 'paid', page: 0, limit: 2 });
  const result = await adapter.fetchPage({ request, mode: 'paid' });
  assert.deepEqual(result.items.map(({ id }) => id), [1, 2]);
});

test('normalization uses only official fields, URL precedence, and raw digest metadata', () => {
  const { adapter } = adapterWith([]);
  const raw = item(42, {
    company: 'Deprecated Wrong Name',
    company_object: { name: 'Official Name' },
    url: 'https://jobs.example.test/listing/42?utm_source=x',
    source_url: 'https://source.example.test/job/42',
    final_url: 'https://boards.greenhouse.io/acme/jobs/42?utm_campaign=x',
    date_reposted: '2026-08-03',
    discovered_at: '2026-08-03T10:00:00Z',
    description: 'Official description',
    remote: false,
    hybrid: true,
    employment_statuses: ['full_time', 'contract'],
    locations: [{ display_name: 'New York, NY, US' }],
  });
  const normalized = adapter.normalizeJob(raw, {
    observedAt: OBSERVED_AT,
    rawPayloadPath: RAW_PATH,
    rawPayloadSha256: RAW_SHA,
  });
  assert.equal(normalized.source, 'theirstack');
  assert.equal(normalized.sourceJobId, '42');
  assert.equal(normalized.company, 'Official Name');
  assert.equal(normalized.title, 'Software Engineer');
  assert.equal(normalized.canonicalListingUrl, 'https://jobs.example.test/listing/42');
  assert.equal(normalized.canonicalApplicationUrl, 'https://boards.greenhouse.io/acme/jobs/42');
  assert.equal(normalized.workplaceType, 'hybrid');
  assert.deepEqual(normalized.employmentTypes, ['contract', 'full_time']);
  assert.equal(normalized.descriptionSha256, createHash('sha256').update('Official description').digest('hex'));
  assert.equal(normalized.discoveredAt, '2026-08-03T10:00:00.000Z');
  assert.equal(normalized.rawPayloadPath, RAW_PATH);
  assert.equal(normalized.rawPayloadSha256, RAW_SHA);
  assert.ok(Object.isFrozen(normalized));
});

test('future provider timestamps are unverified rather than fresh', () => {
  const { adapter } = adapterWith([]);
  const normalized = adapter.normalizeJob(item(43, {
    date_reposted: '2026-08-05',
  }), {
    observedAt: OBSERVED_AT,
    rawPayloadPath: RAW_PATH,
    rawPayloadSha256: RAW_SHA,
  });
  assert.equal(normalized.freshnessState, 'unverified');
  assert.equal(normalized.eligibilityState, 'review');
  assert.ok(normalized.eligibilityReasonCodes.includes('unverified_freshness'));
});

test('provider raw items are never logged while returned as frozen process data', async () => {
  const raw = item(7, { token: 'raw-secret', nested: { note: 'private' } });
  const { adapter } = adapterWith([response({ data: [raw], metadata: { total_results: 1 } })]);
  const request = adapter.buildRequest({ mode: 'preview' });
  const logs = [];
  const original = console.log;
  console.log = (...args) => logs.push(args);
  try {
    const result = await adapter.fetchPage({ request, mode: 'preview' });
    assert.equal(result.items[0].token, 'raw-secret');
    assert.ok(Object.isFrozen(result.items[0]));
    assert.ok(Object.isFrozen(result.items[0].nested));
  } finally {
    console.log = original;
  }
  assert.deepEqual(logs, []);
});

test('credit reconciliation capability reads and freezes current-day usage', async () => {
  const { adapter, calls } = adapterWith([response([
    { api_credits_consumed: 5, period_start: CREDIT_PERIOD },
  ])], { now: () => OBSERVED_AT, creditNow: () => '2026-08-03T12:05:00.000Z' });
  assert.equal(adapter.requiresCreditReconciliation, true);
  assert.equal(typeof adapter.readCreditUsage, 'function');
  const usage = await adapter.readCreditUsage();
  assert.deepEqual(usage, {
    observedAt: '2026-08-03T12:05:00.000Z',
    periodStart: '2026-08-03T00:00:00.000Z',
    periodEnd: '2026-08-03T23:59:59.999Z',
    consumedCredits: 5,
  });
  assert.ok(Object.isFrozen(usage));
  assert.equal(calls.length, 1);
  assert.equal(calls[0].init.method, 'GET');
  assert.equal(calls[0].init.headers.authorization, 'Bearer test-key');
  const requestUrl = new URL(calls[0].url);
  assert.equal(requestUrl.pathname, '/v0/teams/credits_consumption');
  assert.equal(requestUrl.searchParams.get('start_datetime'), '2026-08-03T00:00:00.000Z');
  assert.equal(requestUrl.searchParams.get('end_datetime'), '2026-08-03T23:59:59.999Z');
  assert.equal(requestUrl.searchParams.get('timezone'), 'UTC');
  assert.equal('creditUsage' in adapter, false);
});

test('recovery can reconcile the persisted UTC period after that day ends', async () => {
  const { adapter, calls } = adapterWith([response([
    { api_credits_consumed: 8, period_start: CREDIT_PERIOD },
  ])], { creditNow: () => '2026-08-04T00:05:00.000Z' });
  const usage = await adapter.readCreditUsage({
    periodStart: '2026-08-03T00:00:00.000Z',
    periodEnd: '2026-08-03T23:59:59.999Z',
  });
  assert.deepEqual(usage, {
    observedAt: '2026-08-04T00:05:00.000Z',
    periodStart: '2026-08-03T00:00:00.000Z',
    periodEnd: '2026-08-03T23:59:59.999Z',
    consumedCredits: 8,
  });
  const requestUrl = new URL(calls[0].url);
  assert.equal(requestUrl.searchParams.get('start_datetime'), '2026-08-03T00:00:00.000Z');
  assert.equal(requestUrl.searchParams.get('end_datetime'), '2026-08-03T23:59:59.999Z');
});

test('credit reconciliation rejects malformed rows and invalid timestamps', async () => {
  for (const payload of [
    {},
    [null],
    [{ api_credits_consumed: 1 }],
    [{ api_credits_consumed: -1, period_start: CREDIT_PERIOD }],
    [{ api_credits_consumed: 1.5, period_start: CREDIT_PERIOD }],
    [{ api_credits_consumed: '1', period_start: CREDIT_PERIOD }],
    [{ api_credits_consumed: 1, period_start: 'not-a-date' }],
    [{ api_credits_consumed: 1, period_start: '2026-08-02T23:59:59Z' }],
    [{ api_credits_consumed: 1, period_start: '2026-08-03T13:00:00Z' }],
    [{ api_credits_consumed: 1, period_start: CREDIT_PERIOD }, { api_credits_consumed: 1, period_start: CREDIT_PERIOD }],
    [{ api_credits_consumed: Number.MAX_SAFE_INTEGER, period_start: CREDIT_PERIOD }, { api_credits_consumed: 1, period_start: '2026-08-03T01:00:00Z' }],
  ]) {
    const { adapter } = adapterWith([response(payload)], { now: () => OBSERVED_AT });
    await assert.rejects(
      adapter.readCreditUsage(),
      (error) => error instanceof TheirStackError && error.code === 'terminal_validation',
    );
  }
});

test('credit reconciliation classifies auth and bounded transient failures', async () => {
  const auth = adapterWith([response({}, 401)], { now: () => OBSERVED_AT }).adapter;
  await assert.rejects(auth.readCreditUsage(), (error) => error.code === 'authentication');

  const { adapter, calls } = adapterWith([
    response({}, 429),
    response([{ api_credits_consumed: 4, period_start: CREDIT_PERIOD }]),
  ], { maxPreviewRetries: 1, now: () => OBSERVED_AT, creditNow: () => OBSERVED_AT });
  const usage = await adapter.readCreditUsage();
  assert.equal(usage.consumedCredits, 4);
  assert.equal(calls.length, 2);

  const failed = adapterWith([response({}, 500)], { maxPreviewRetries: 0, now: () => OBSERVED_AT }).adapter;
  await assert.rejects(failed.readCreditUsage(), (error) => error.code === 'retryable_preview');
});

test('response body timeouts cover preview, paid, and accounting JSON reads', async () => {
  function stalledFetch(calls) {
    return async (url, init) => {
      calls.push({ url, init });
      return {
        status: 200,
        headers: { get() { return null; } },
        json() {
          return new Promise((resolve, reject) => {
            init.signal.addEventListener('abort', () => reject(new Error('body aborted')), { once: true });
          });
        },
      };
    };
  }

  const previewCalls = [];
  const preview = createTheirStackAdapter({
    apiKey: 'test-key',
    fetch: stalledFetch(previewCalls),
    timeoutMs: 5,
    maxPreviewRetries: 1,
    retryDelayMs: 0,
    now: () => OBSERVED_AT,
    windowEnd: WINDOW_END,
  });
  await assert.rejects(
    preview.fetchPage({ request: preview.buildRequest({ mode: 'preview' }), mode: 'preview' }),
    (error) => error.code === 'retryable_preview',
  );
  assert.equal(previewCalls.length, 2);

  const paidCalls = [];
  const paid = createTheirStackAdapter({
    apiKey: 'test-key',
    timeoutMs: 5,
    maxPreviewRetries: 0,
    paidAuthorization: true,
    fetch: stalledFetch(paidCalls),
    now: () => OBSERVED_AT,
    windowEnd: WINDOW_END,
  });
  await assert.rejects(
    paid.fetchPage({ request: paid.buildRequest({ mode: 'paid', limit: 1 }), mode: 'paid' }),
    (error) => error.code === 'paid_ambiguous',
  );
  assert.equal(paidCalls.length, 1);

  const accountingCalls = [];
  const accounting = createTheirStackAdapter({
    apiKey: 'test-key',
    timeoutMs: 5,
    maxPreviewRetries: 0,
    now: () => OBSERVED_AT,
    windowEnd: WINDOW_END,
    fetch: stalledFetch(accountingCalls),
  });
  await assert.rejects(accounting.readCreditUsage(), (error) => error.code === 'retryable_preview');
  assert.equal(accountingCalls.length, 1);
});

test('malformed JSON is terminal for preview and ambiguous after a paid request', async () => {
  const invalid = {
    status: 200,
    headers: { get() { return null; } },
    async json() {
      throw new SyntaxError('invalid JSON');
    },
  };
  const preview = adapterWith([invalid]).adapter;
  await assert.rejects(
    preview.fetchPage({ request: preview.buildRequest({ mode: 'preview' }), mode: 'preview' }),
    (error) => error.code === 'terminal_validation',
  );
  const paid = adapterWith([invalid], { paidAuthorization: true }).adapter;
  await assert.rejects(
    paid.fetchPage({ request: paid.buildRequest({ mode: 'paid', limit: 1 }), mode: 'paid' }),
    (error) => error.code === 'paid_ambiguous',
  );
});

test('streamed response bodies are byte-bounded before JSON parsing', async () => {
  const streamed = () => ({
    status: 200,
    headers: { get() { return null; } },
    body: {
      async *[Symbol.asyncIterator]() {
        yield new TextEncoder().encode('{"private":"oversized"}');
      },
    },
  });
  const preview = adapterWith([streamed()], { maxResponseBytes: 4, maxPreviewRetries: 0 }).adapter;
  await assert.rejects(
    preview.fetchPage({ request: preview.buildRequest({ mode: 'preview' }), mode: 'preview' }),
    (error) => error.code === 'terminal_validation',
  );
  const paid = adapterWith([streamed()], { maxResponseBytes: 4, paidAuthorization: true }).adapter;
  await assert.rejects(
    paid.fetchPage({ request: paid.buildRequest({ mode: 'paid', limit: 1 }), mode: 'paid' }),
    (error) => error.code === 'paid_ambiguous',
  );
});
