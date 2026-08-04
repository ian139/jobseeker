import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { mkdtemp, mkdir, readFile, readdir, rm, stat } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import test from 'node:test';
import { initializeIngestionDatabase, openIngestionDatabase } from '../src/ingestion/database.mjs';
import { sha256Canonical, validateSourceSyncResult } from '../src/ingestion/contracts.mjs';
import { CrashInjection, previewSource, recoverSourceSync, syncSource } from '../src/ingestion/sync.mjs';

const NOW = '2026-08-03T12:00:00.000Z';
const WINDOW_END = '2026-08-03T23:59:59.000Z';

function textSha(value) {
  return createHash('sha256').update(value, 'utf8').digest('hex');
}

function normalized(raw, artifact) {
  const description = raw.description ?? `Description for ${raw.id}`;
  return {
    schema: 'normalized-job-v1',
    source: 'fake',
    sourceJobId: raw.id ?? null,
    canonicalListingUrl: `https://boards.greenhouse.io/acme/jobs/${raw.id}`,
    canonicalApplicationUrl: raw.url ?? null,
    atsKind: 'greenhouse',
    atsIdentifier: 'acme',
    title: raw.title ?? `Role ${raw.id}`,
    company: raw.company ?? 'Acme',
    location: raw.location ?? 'Remote',
    workplaceType: 'remote',
    employmentTypes: ['full_time'],
    description,
    descriptionSha256: textSha(description),
    sourcePostedAt: raw.postedAt ?? '2026-08-03T10:00:00.000Z',
    sourceUpdatedAt: raw.updatedAt ?? '2026-08-03T10:00:00.000Z',
    discoveredAt: raw.discoveredAt ?? '2026-08-03T11:00:00.000Z',
    availabilityState: 'open',
    freshnessState: 'current',
    eligibilityState: 'eligible',
    eligibilityReasonCodes: [],
    priority: 0,
    dedupeIdentityKind: 'application_url',
    dedupeIdentityKey: raw.group ?? raw.url ?? `job:${raw.id}`,
    dedupeReviewRequired: false,
    rawPayloadPath: artifact.rawPayloadPath,
    rawPayloadSha256: artifact.rawPayloadSha256,
  };
}

function makeAdapter(pagesByNumber, { malformedPage = null, calls = [] } = {}) {
  return {
    source: 'fake',
    buildRequest({ page, limit }) {
      const body = { page, limit };
      return Object.freeze({
        url: 'https://fake.invalid/search',
        body,
        page,
        limit,
        requestSha256: sha256Canonical({ url: 'https://fake.invalid/search', body, page, limit }),
      });
    },
    async fetchPage({ request, mode }) {
      calls.push({ page: request.page, mode });
      const response = pagesByNumber[request.page] ?? { items: [], totalResults: 0 };
      if (malformedPage === request.page) {
        return { ...response, requestSha256: '0'.repeat(64), receivedAt: NOW, estimatedCredits: 1 };
      }
      return {
        requestSha256: request.requestSha256,
        items: response.items,
        totalResults: response.totalResults,
        receivedAt: NOW,
        estimatedCredits: response.estimatedCredits ?? 1,
      };
    },
    normalizeJob(raw, artifact) {
      return normalized(raw, artifact);
    },
  };
}

async function fixture() {
  const root = await mkdtemp(join(tmpdir(), 'source-sync-'));
  const privateRoot = join(root, 'private');
  await mkdir(privateRoot, { mode: 0o700 });
  const databasePath = join(root, 'jobs.sqlite');
  initializeIngestionDatabase(databasePath, { now: NOW });
  return { root, privateRoot, databasePath };
}

async function closeFixture(value) {
  await rm(value.root, { recursive: true, force: true });
}

function options(value, adapter, extra = {}) {
  return {
    databasePath: value.databasePath,
    privateRoot: value.privateRoot,
    adapter,
    profile: 'default',
    now: NOW,
    windowEnd: WINDOW_END,
    checkpoint: null,
    limit: 2,
    maxPages: 5,
    maxItems: 20,
    paidAuthorization: true,
    ...extra,
  };
}

function rows(value, query) {
  const database = openIngestionDatabase(value.databasePath);
  try {
    return database.prepare(query).all();
  } finally {
    database.close();
  }
}

test('preview is bounded, schema-valid, and does not open or mutate SQLite/artifacts', async () => {
  const root = await mkdtemp(join(tmpdir(), 'source-preview-'));
  const calls = [];
  const adapter = makeAdapter({ 0: { items: [{ id: 'p1', url: 'https://boards.greenhouse.io/acme/jobs/p1' }], totalResults: 1 } }, { calls });
  try {
    const result = await previewSource({
      adapter,
      profile: 'default',
      now: NOW,
      windowEnd: WINDOW_END,
      checkpoint: null,
      limit: 1,
      maxPages: 1,
    });
    validateSourceSyncResult(result);
    assert.equal(result.state, 'previewed');
    assert.equal(result.syncRunId, null);
    assert.deepEqual(calls, [{ page: 0, mode: 'preview' }]);
    assert.equal((await readdir(root)).length, 0);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test('paid sync ingests every listing and promotes at most one queue row per dedupe group', async () => {
  const value = await fixture();
  const adapter = makeAdapter({
    0: {
      items: [
        { id: 'a', url: 'https://boards.greenhouse.io/acme/jobs/a', group: 'same-company-role-group' },
        { id: 'b', url: 'https://boards.greenhouse.io/acme/jobs/b', group: 'same-company-role-group' },
      ],
      totalResults: 3,
    },
    1: { items: [{ id: 'c', url: 'https://boards.greenhouse.io/acme/jobs/c', group: 'third-group' }], totalResults: 3 },
  });
  try {
    const result = await syncSource(options(value, adapter));
    validateSourceSyncResult(result);
    assert.equal(result.state, 'succeeded');
    assert.equal(result.jobsSeen, 3);
    assert.equal(rows(value, 'SELECT count(*) AS n FROM jobs')[0].n, 3);
    assert.equal(rows(value, "SELECT count(*) AS n FROM application_jobs WHERE status='queued'")[0].n, 2);
    assert.equal(rows(value, 'SELECT count(*) AS n FROM dedupe_groups')[0].n, 2);
    assert.equal(rows(value, 'SELECT count(*) AS n FROM source_observations')[0].n, 3);
  } finally {
    await closeFixture(value);
  }
});

test('source ID and application URL are update identities', async () => {
  const value = await fixture();
  const first = makeAdapter({ 0: { items: [{ id: 'same', url: 'https://boards.greenhouse.io/acme/jobs/old' }], totalResults: 1 } });
  try {
    const initial = await syncSource(options(value, first, { limit: 1, maxPages: 1 }));
    assert.equal(initial.state, 'succeeded');
    const second = makeAdapter({ 0: { items: [{ id: 'same', url: 'https://boards.greenhouse.io/acme/jobs/new', title: 'Updated role' }], totalResults: 1 } });
    const updated = await syncSource(options(value, second, { limit: 1, maxPages: 1 }));
    assert.equal(updated.state, 'succeeded');
    assert.equal(rows(value, 'SELECT count(*) AS n FROM jobs')[0].n, 1);
    assert.deepEqual(rows(value, 'SELECT source_job_id,canonical_application_url,title FROM jobs'), [{ source_job_id: 'same', canonical_application_url: 'https://boards.greenhouse.io/acme/jobs/new', title: 'Updated role' }]);
  } finally {
    await closeFixture(value);
  }
});

test('malformed later page leaves jobs and checkpoint untouched', async () => {
  const value = await fixture();
  const adapter = makeAdapter({
    0: { items: [{ id: 'valid', url: 'https://boards.greenhouse.io/acme/jobs/valid' }], totalResults: 2 },
    1: { items: [{ id: 'later', url: 'https://boards.greenhouse.io/acme/jobs/later' }], totalResults: 2 },
  }, { malformedPage: 1 });
  try {
    const result = await syncSource(options(value, adapter, { limit: 1, maxPages: 2 }));
    assert.equal(result.state, 'failed');
    assert.equal(rows(value, 'SELECT count(*) AS n FROM jobs')[0].n, 0);
    assert.equal(rows(value, 'SELECT count(*) AS n FROM source_checkpoints')[0].n, 0);
  } finally {
    await closeFixture(value);
  }
});

test('pending paid request recovers as paid_ambiguous without replay', async () => {
  const value = await fixture();
  const calls = [];
  const adapter = makeAdapter({ 0: { items: [{ id: 'x', url: 'https://boards.greenhouse.io/acme/jobs/x' }], totalResults: 1 } }, { calls });
  let runId;
  try {
    await assert.rejects(() => syncSource(options(value, adapter, { hooks: { afterPending({ runId: value }) { runId = value; return false; } } })) , CrashInjection);
    const recoveryAdapter = makeAdapter({ 0: { items: [{ id: 'x', url: 'https://boards.greenhouse.io/acme/jobs/x' }], totalResults: 1 } }, { calls });
    const result = await recoverSourceSync(options(value, recoveryAdapter, { runId }));
    assert.equal(result.state, 'paid_ambiguous');
    assert.equal(calls.length, 0);
    assert.equal(rows(value, 'SELECT state FROM sync_runs')[0].state, 'paid_ambiguous');
  } finally {
    await closeFixture(value);
  }
});

test('receipt-complete pages are private and ready-to-commit recovery does not fetch', async () => {
  const value = await fixture();
  const calls = [];
  const adapter = makeAdapter({ 0: { items: [{ id: 'r', url: 'https://boards.greenhouse.io/acme/jobs/r' }], totalResults: 1 } }, { calls });
  let runId;
  try {
    await assert.rejects(() => syncSource(options(value, adapter, { hooks: { beforeCommit({ runId: value }) { runId = value; return false; } } })) , CrashInjection);
    assert.equal(calls.length, 1);
    const pagesRows = rows(value, 'SELECT response_path,response_sha256 FROM source_sync_pages');
    assert.equal(pagesRows.length, 1);
    const responsePath = join(value.privateRoot, pagesRows[0].response_path);
    const info = await stat(responsePath);
    assert.equal(info.mode & 0o777, 0o600);
    const receipt = JSON.parse(await readFile(responsePath, 'utf8'));
    const rawPath = join(value.privateRoot, receipt.items[0].rawPayloadPath);
    const rawInfo = await stat(rawPath);
    assert.equal(rawInfo.mode & 0o777, 0o600);
    assert.equal(textSha(await readFile(rawPath, 'utf8')), receipt.items[0].rawPayloadSha256);
    const result = await recoverSourceSync(options(value, makeAdapter({}, { calls }), { runId }));
    assert.equal(result.state, 'succeeded');
    assert.equal(calls.length, 1);
    assert.equal(rows(value, 'SELECT count(*) AS n FROM jobs')[0].n, 1);
    assert.match(pagesRows[0].response_sha256, /^[0-9a-f]{64}$/);
    assert.equal((await readFile(responsePath, 'utf8')).includes('description'), false);
  } finally {
    await closeFixture(value);
  }
});
