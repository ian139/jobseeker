import assert from 'node:assert/strict';
import test from 'node:test';
import {
  createGreenhouseAdapter,
  GreenhouseIngestionError,
  normalizeCheckpoint,
} from '../src/ingestion/greenhouse.mjs';
import { validateNormalizedJob } from '../src/ingestion/contracts.mjs';

const observedAt = '2026-08-04T12:00:00.000Z';
const rawSha = 'a'.repeat(64);

function response(payload, status = 200) {
  return {
    status,
    ok: status >= 200 && status < 300,
    async json() { return payload; },
  };
}

function job(id, updated_at, overrides = {}) {
  return {
    id,
    title: `Software Engineer ${id}`,
    absolute_url: `https://boards.greenhouse.io/acme/jobs/${id}`,
    location: { name: 'Remote' },
    updated_at,
    first_published: updated_at,
    content: `<p>Build software ${id}</p>`,
    ...overrides,
  };
}

test('buildRequest creates page-zero board URL and stable hash', () => {
  const adapter = createGreenhouseAdapter({ boardToken: 'acme' });
  const request = adapter.buildRequest({ profile: 'acme', mode: 'preview', page: 0, limit: 100 });
  assert.equal(request.url, 'https://boards-api.greenhouse.io/v1/boards/acme/jobs?content=true');
  assert.equal(request.page, 0);
  assert.equal(request.limit, 100);
  assert.match(request.requestSha256, /^[0-9a-f]{64}$/);
});

test('fetchPage returns normalized raw items and total count', async () => {
  const adapter = createGreenhouseAdapter({
    boardToken: 'acme',
    now: () => observedAt,
    fetch: async () => response({ jobs: [job(1, '2026-08-04T10:00:00Z'), job(2, '2026-08-03T10:00:00Z')] }),
  });
  const request = adapter.buildRequest({ profile: 'acme', mode: 'preview', page: 0 });
  const page = await adapter.fetchPage({ request, mode: 'preview' });
  assert.equal(page.items.length, 2);
  assert.equal(page.totalResults, 2);
  const normalized = adapter.normalizeJob(page.items[0], {
    observedAt,
    rawPayloadPath: '/tmp/greenhouse/job.json',
    rawPayloadSha256: rawSha,
  });
  assert.equal(normalized.schema, 'normalized-job-v1');
  assert.equal(normalized.source, 'greenhouse');
  assert.doesNotThrow(() => validateNormalizedJob(normalized));
});

test('fetchPage slices a cached complete board snapshot into bounded pages', async () => {
  let fetchCount = 0;
  const adapter = createGreenhouseAdapter({
    boardToken: 'acme',
    now: () => observedAt,
    fetch: async () => {
      fetchCount += 1;
      return response({ jobs: [job(1, observedAt), job(2, observedAt), job(3, observedAt)] });
    },
  });
  const first = await adapter.fetchPage({
    request: adapter.buildRequest({ profile: 'acme', page: 0, limit: 2 }),
  });
  const second = await adapter.fetchPage({
    request: adapter.buildRequest({ profile: 'acme', page: 1, limit: 2 }),
  });
  assert.deepEqual(first.items.map((item) => item.id), [1, 2]);
  assert.deepEqual(second.items.map((item) => item.id), [3]);
  assert.equal(first.totalResults, 3);
  assert.equal(second.totalResults, 3);
  assert.equal(fetchCount, 1);
});

test('checkpoint filters jobs older than checkpoint', async () => {
  const adapter = createGreenhouseAdapter({
    boardToken: 'acme',
    fetch: async () => response({ jobs: [job(1, '2026-08-04T10:00:00Z'), job(2, '2026-08-01T10:00:00Z')] }),
  });
  const request = adapter.buildRequest({ profile: 'acme', checkpoint: '2026-08-03T00:00:00Z' });
  const page = await adapter.fetchPage({ request });
  assert.deepEqual(page.items.map((item) => item.id), [1]);
});

test('normalizeCheckpoint canonicalizes and defaults invalid values', () => {
  assert.equal(normalizeCheckpoint('2026-08-03T00:00:00Z'), '2026-08-03T00:00:00.000Z');
  assert.equal(normalizeCheckpoint('not-a-date'), '1970-01-01T00:00:00.000Z');
});

test('board-not-found has stable error code', async () => {
  const adapter = createGreenhouseAdapter({ boardToken: 'missing', fetch: async () => response({}, 404) });
  const request = adapter.buildRequest({ profile: 'missing' });
  await assert.rejects(() => adapter.fetchPage({ request }), (error) => {
    assert.ok(error instanceof GreenhouseIngestionError);
    assert.equal(error.code, 'BOARD_NOT_FOUND');
    return true;
  });
});
