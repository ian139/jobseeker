import assert from 'node:assert/strict';
import test from 'node:test';

import { createAdapter } from '../src/ingestion/source-registry.mjs';
import { validateSourceSyncResult } from '../src/ingestion/contracts.mjs';

const now = '2026-08-04T00:00:00.000Z';

function response(body, status = 200) {
  const text = JSON.stringify(body);
  return {
    ok: status >= 200 && status < 300,
    status,
    headers: new Headers({ 'content-type': 'application/json', 'content-length': String(text.length) }),
    async text() { return text; },
    async json() { return body; },
  };
}

test('registry dynamically creates TheirStack adapter and produces schema-valid result', async () => {
  const fetch = async () => response({ data: [], metadata: { total_results: 0 } });
  const adapter = await createAdapter({
    profile: 'default',
    env: { THEIRSTACK_API_KEY: 'test-key' },
    config: { fetch, postedAtMaxAgeDays: 7, timeoutMs: 1000 },
    now: () => now,
  });
  assert.equal(adapter.source, 'theirstack');
  assert.equal(Object.isFrozen(adapter), true);
  const request = adapter.buildRequest({ profile: 'default', mode: 'preview', page: 0, limit: 1, windowEnd: now });
  const page = await adapter.fetchPage({ request, mode: 'preview' });
  assert.deepEqual(page.items, []);
  const result = validateSourceSyncResult({
    schema: 'source-sync-result-v1', syncRunId: null, source: adapter.source, profile: 'default', mode: 'preview', state: 'previewed',
    startedAt: now, finishedAt: now, checkpointBefore: null, checkpointAfter: now,
    pagesFetched: 1, requestCount: 1, jobsSeen: 0, jobsInserted: 0, jobsUpdated: 0, jobsUnchanged: 0,
    dedupeGroupsTouched: 0, queueRowsInserted: 0, estimatedCredits: page.estimatedCredits, reportedCredits: page.reportedCredits,
    failureClass: null, reasonCode: null,
  });
  assert.equal(result.schema, 'source-sync-result-v1');
});

test('registry resolves public source factories and builds board options', async () => {
  const greenhouse = await createAdapter({ source: 'greenhouse', profile: 'env-board', env: { GREENHOUSE_BOARD_TOKEN: 'env-board' }, now: () => now });
  assert.equal(greenhouse.source, 'greenhouse');
  const greenhouseRequest = greenhouse.buildRequest({ profile: 'env-board', mode: 'preview', page: 0, limit: 1 });
  assert.match(greenhouseRequest.url, /env-board/u);

  const ashby = await createAdapter({ source: 'ashby', profile: 'env-board', env: { ASHBY_BOARD_NAME: 'env-board' }, now: () => now });
  assert.equal(ashby.source, 'ashby');
  const ashbyRequest = ashby.buildRequest({ profile: 'env-board', mode: 'preview', page: 0, limit: 1 });
  assert.match(ashbyRequest.url, /env-board/u);
});

test('registry rejects unknown sources and invalid profile names', async () => {
  await assert.rejects(() => createAdapter({ source: 'missing', profile: 'default' }), /source_unsupported/u);
  await assert.rejects(() => createAdapter({ source: 'theirstack', profile: 'bad profile' }), /profile_invalid/u);
});
