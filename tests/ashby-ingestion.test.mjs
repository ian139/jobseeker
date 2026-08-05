import test from 'node:test';
import assert from 'node:assert/strict';
import { createAshbyAdapter, AshbyBoardNotFoundError, AshbyMalformedJsonError } from '../src/ingestion/ashby.mjs';

const postings = [
  { id: 'job-1', title: 'Software Engineer Intern', companyName: 'Acme', jobUrl: 'https://jobs.ashbyhq.com/acme/job-1', applyUrl: 'https://jobs.ashbyhq.com/acme/job-1', publishedAt: '2026-08-03T00:00:00Z', updatedAt: '2026-08-03T01:00:00Z', locationName: 'Remote', isRemote: true, descriptionPlain: 'Build software.' },
  { id: 'job-0', title: 'Old Role', companyName: 'Acme', jobUrl: 'https://jobs.ashbyhq.com/acme/job-0', publishedAt: '2026-07-01T00:00:00Z', descriptionPlain: 'Old.' },
];
function response(body, status = 200) { const text = JSON.stringify(body); return { ok: status >= 200 && status < 300, status, statusText: '', arrayBuffer: async () => new TextEncoder().encode(text).buffer }; }

test('buildRequest creates page-zero public board request', () => {
  const adapter = createAshbyAdapter({ boardName: 'acme' });
  const request = adapter.buildRequest({ profile: 'ignored', page: 0, checkpoint: '2026-08-01T00:00:00Z' });
  assert.equal(request.url, 'https://api.ashbyhq.com/posting-api/job-board/acme');
  assert.equal(request.page, 0);
  assert.equal(request.checkpoint, '2026-08-01T00:00:00.000Z');
  assert.match(request.requestSha256, /^[0-9a-f]{64}$/);
});

test('fetchPage parses postings and applies checkpoint', async () => {
  const adapter = createAshbyAdapter({ boardName: 'acme', fetch: async () => response({ jobs: postings }), now: () => '2026-08-04T00:00:00Z' });
  const request = adapter.buildRequest({ checkpoint: '2026-08-01T00:00:00Z' });
  const page = await adapter.fetchPage({ request, mode: 'preview' });
  assert.equal(page.items.length, 1);
  assert.equal(page.items[0].id, 'job-1');
  assert.equal(page.totalResults, 1);
});

test('fetchPage slices a cached complete board snapshot into bounded pages', async () => {
  let fetchCount = 0;
  const adapter = createAshbyAdapter({
    boardName: 'acme',
    fetch: async () => {
      fetchCount += 1;
      return response({ jobs: [
        postings[0],
        { ...postings[0], id: 'job-2' },
        { ...postings[0], id: 'job-3' },
      ] });
    },
    now: () => '2026-08-04T00:00:00Z',
  });
  const first = await adapter.fetchPage({ request: adapter.buildRequest({ page: 0, limit: 2 }) });
  const second = await adapter.fetchPage({ request: adapter.buildRequest({ page: 1, limit: 2 }) });
  assert.deepEqual(first.items.map((item) => item.id), ['job-1', 'job-2']);
  assert.deepEqual(second.items.map((item) => item.id), ['job-3']);
  assert.equal(first.totalResults, 3);
  assert.equal(second.totalResults, 3);
  assert.equal(fetchCount, 1);
});

test('normalizeJob maps dates, canonical URLs, ATS, and identity', () => {
  const adapter = createAshbyAdapter({ boardName: 'acme' });
  const job = adapter.normalizeJob(postings[0], { observedAt: '2026-08-04T00:00:00Z', rawPayloadPath: '/tmp/raw.json', rawPayloadSha256: 'a'.repeat(64) });
  assert.equal(job.schema, 'normalized-job-v1');
  assert.equal(job.source, 'ashby');
  assert.equal(job.sourcePostedAt, '2026-08-03T00:00:00.000Z');
  assert.equal(job.sourceUpdatedAt, '2026-08-03T01:00:00.000Z');
  assert.equal(job.atsKind, 'ashby');
  assert.equal(job.dedupeIdentityKind, 'ats');
  assert.equal(job.eligibilityState, 'eligible');
});

test('stable errors are raised for board-not-found and malformed JSON', async () => {
  const missing = createAshbyAdapter({ boardName: 'missing', fetch: async () => response({}, 404) });
  await assert.rejects(() => missing.fetchPage({ request: missing.buildRequest(), mode: 'preview' }), (error) => error instanceof AshbyBoardNotFoundError && error.code === 'BOARD_NOT_FOUND');
  const malformed = createAshbyAdapter({ boardName: 'bad', fetch: async () => ({ ok: true, status: 200, arrayBuffer: async () => new TextEncoder().encode('{').buffer }) });
  await assert.rejects(() => malformed.fetchPage({ request: malformed.buildRequest(), mode: 'preview' }), (error) => error instanceof AshbyMalformedJsonError && error.code === 'MALFORMED_JSON');
});

test('normalizeCheckpoint returns canonical ISO timestamp', () => {
  const adapter = createAshbyAdapter({ boardName: 'acme' });
  assert.equal(adapter.normalizeCheckpoint('2026-08-04T01:02:03Z'), '2026-08-04T01:02:03.000Z');
});
