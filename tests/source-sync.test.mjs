import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { mkdtemp, mkdir, readFile, readdir, rm, stat, symlink, unlink } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import test from 'node:test';
import { initializeIngestionDatabase, openIngestionDatabase } from '../src/ingestion/database.mjs';
import { classifyAts, classifyEligibility, deriveDedupeIdentity, sha256Canonical, validateSourceSyncResult } from '../src/ingestion/contracts.mjs';
import { CrashInjection, previewSource, recoverSourceSync, syncSource } from '../src/ingestion/sync.mjs';

const NOW = '2026-08-03T12:00:00.000Z';
const WINDOW_END = '2026-08-03T23:59:59.000Z';

function textSha(value) {
  return createHash('sha256').update(value, 'utf8').digest('hex');
}

function normalized(raw, artifact) {
  const description = raw.description ?? `Description for ${raw.id}`;
  const canonicalApplicationUrl = raw.url ?? null;
  const ats = classifyAts(canonicalApplicationUrl);
  const base = {
    schema: 'normalized-job-v1',
    source: 'fake',
    sourceJobId: raw.id ?? null,
    canonicalListingUrl: `https://boards.greenhouse.io/acme/jobs/${raw.id}`,
    canonicalApplicationUrl,
    atsKind: ats.kind,
    atsIdentifier: ats.identifier,
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
    availabilityState: raw.availabilityState ?? 'open',
    freshnessState: raw.freshnessState ?? 'current',
    rawPayloadPath: artifact.rawPayloadPath,
    rawPayloadSha256: artifact.rawPayloadSha256,
  };
  const eligibility = classifyEligibility(base);
  const withEligibility = { ...base, ...eligibility };
  const identity = deriveDedupeIdentity(withEligibility);
  return {
    ...withEligibility,
    dedupeIdentityKind: identity.kind,
    dedupeIdentityKey: identity.key,
    dedupeReviewRequired: identity.reviewRequired,
  };
}

function makeAdapter(pagesByNumber, {
  malformedPage = null,
  calls = [],
  creditSnapshots = null,
  creditCalls = [],
} = {}) {
  const snapshots = creditSnapshots === null ? null : [...creditSnapshots];
  const adapter = {
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
      calls.push({ page: request.page, limit: request.limit, mode });
      const response = pagesByNumber[request.page] ?? { items: [], totalResults: 0 };
      if (malformedPage === request.page) {
        return { ...response, requestSha256: '0'.repeat(64), receivedAt: NOW, estimatedCredits: 1 };
      }
      return {
        requestSha256: request.requestSha256,
        items: response.items,
        totalResults: response.totalResults,
        receivedAt: response.receivedAt ?? NOW,
        estimatedCredits: response.estimatedCredits ?? 1,
      };
    },
    normalizeJob(raw, artifact) {
      return normalized(raw, artifact);
    },
  };
  if (snapshots !== null) {
    adapter.requiresCreditReconciliation = true;
    adapter.readCreditUsage = async (period) => {
      creditCalls.push(period ?? null);
      const snapshot = snapshots.shift();
      if (snapshot instanceof Error) throw snapshot;
      return snapshot;
    };
  }
  return adapter;
}

function creditSnapshot(consumedCredits, observedAt) {
  return {
    observedAt,
    periodStart: '2026-08-03T00:00:00.000Z',
    periodEnd: '2026-08-03T23:59:59.999Z',
    consumedCredits,
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
      limit: 2,
      maxPages: 5,
    });
    validateSourceSyncResult(result);
    assert.equal(result.state, 'previewed');
    assert.equal(result.syncRunId, null);
    assert.deepEqual(calls, [{ page: 0, limit: 1, mode: 'preview' }]);
    assert.equal((await readdir(root)).length, 0);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test('source and profile identities are rejected before paid side effects', async () => {
  const value = await fixture();
  const calls = [];
  try {
    const invalidSource = makeAdapter({}, { calls });
    invalidSource.source = '1invalid';
    await assert.rejects(
      syncSource(options(value, invalidSource)),
      (error) => error.reasonCode === 'source_required',
    );
    await assert.rejects(
      syncSource(options(value, makeAdapter({}, { calls }), { profile: 'invalid profile' })),
      (error) => error.reasonCode === 'profile_required',
    );
    assert.equal(calls.length, 0);
    assert.equal(rows(value, 'SELECT count(*) AS n FROM sync_runs')[0].n, 0);
  } finally {
    await closeFixture(value);
  }
});

test('opaque checkpoints are rejected before any paid request or database mutation', async () => {
  const value = await fixture();
  const calls = [];
  try {
    const direct = makeAdapter({}, { calls });
    await assert.rejects(
      syncSource(options(value, direct, { checkpoint: 'opaque-cursor' })),
      (error) => error.reasonCode === 'invalid_checkpoint',
    );
    const normalized = makeAdapter({}, { calls });
    normalized.normalizeCheckpoint = () => 'opaque-cursor';
    await assert.rejects(
      syncSource(options(value, normalized, { checkpoint: NOW })),
      (error) => error.reasonCode === 'invalid_checkpoint',
    );
    assert.equal(calls.length, 0);
    assert.equal(rows(value, 'SELECT count(*) AS n FROM sync_runs')[0].n, 0);
  } finally {
    await closeFixture(value);
  }
});

test('paid sync ingests every listing and promotes at most one queue row per dedupe group', async () => {
  const value = await fixture();
  const adapter = makeAdapter({
    0: {
      items: [
        { id: 'a', url: 'https://boards.greenhouse.io/acme/jobs/shared' },
        { id: 'b', url: 'https://boards.greenhouse.io/acme/jobs/shared' },
      ],
      totalResults: 3,
    },
    1: { items: [{ id: 'c', url: 'https://boards.greenhouse.io/acme/jobs/third' }], totalResults: 3 },
  });
  try {
    const result = await syncSource(options(value, adapter));
    validateSourceSyncResult(result);
    assert.equal(result.state, 'succeeded', JSON.stringify(result));
    assert.equal(result.jobsSeen, 3);
    assert.equal(rows(value, 'SELECT count(*) AS n FROM jobs')[0].n, 3);
    assert.equal(rows(value, "SELECT count(*) AS n FROM application_jobs WHERE status='queued'")[0].n, 2);
    assert.equal(rows(value, 'SELECT count(*) AS n FROM dedupe_groups')[0].n, 2);
    assert.equal(rows(value, 'SELECT count(*) AS n FROM source_observations')[0].n, 3);
  } finally {
    await closeFixture(value);
  }
});

test('completed runs retain one queue row for distinct URLs in a shared dedupe group', async () => {
  const value = await fixture();
  const first = makeAdapter({
    0: {
      items: [{ id: 'first', url: 'https://boards.greenhouse.io/acme/jobs/first-path?for=acme&gh_jid=123' }],
      totalResults: 1,
    },
  });
  const second = makeAdapter({
    0: {
      items: [{ id: 'second', url: 'https://boards.greenhouse.io/acme/jobs/second-path?for=acme&gh_jid=123' }],
      totalResults: 1,
    },
  });
  try {
    assert.equal((await syncSource(options(value, first, { limit: 1, maxPages: 1 }))).state, 'succeeded');
    assert.equal((await syncSource(options(value, second, { limit: 1, maxPages: 1 }))).state, 'succeeded');
    assert.equal(rows(value, 'SELECT count(*) AS n FROM jobs')[0].n, 2);
    const queued = rows(value, "SELECT application_url,dedupe_group_id FROM application_jobs WHERE status='queued'");
    assert.equal(queued.length, 1);
    assert.notEqual(queued[0].dedupe_group_id, null);
  } finally {
    await closeFixture(value);
  }
});

test('a terminal application suppresses a later same-group queue candidate', async () => {
  const value = await fixture();
  const first = makeAdapter({
    0: {
      items: [{ id: 'terminal-first', url: 'https://boards.greenhouse.io/acme/jobs/first?for=acme&gh_jid=987' }],
      totalResults: 1,
    },
  });
  const second = makeAdapter({
    0: {
      items: [{ id: 'terminal-second', url: 'https://boards.greenhouse.io/acme/jobs/second?for=acme&gh_jid=987' }],
      totalResults: 1,
    },
  });
  try {
    assert.equal((await syncSource(options(value, first, { limit: 1, maxPages: 1 }))).state, 'succeeded');
    const database = openIngestionDatabase(value.databasePath);
    try {
      database.prepare("UPDATE application_jobs SET status='completed',completed_at=? WHERE status='queued'").run(NOW);
    } finally {
      database.close();
    }
    const result = await syncSource(options(value, second, { limit: 1, maxPages: 1 }));
    assert.equal(result.state, 'succeeded', JSON.stringify(result));
    assert.equal(result.queueRowsInserted, 0);
    assert.equal(rows(value, 'SELECT count(*) AS n FROM jobs')[0].n, 2);
    assert.deepEqual(rows(value, 'SELECT status,count(*) AS n FROM application_jobs GROUP BY status').map((row) => ({ ...row })), [{
      status: 'completed',
      n: 1,
    }]);
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
    assert.deepEqual(rows(value, 'SELECT source_job_id,canonical_application_url,title FROM jobs').map((row) => ({ ...row })), [{ source_job_id: 'same', canonical_application_url: 'https://boards.greenhouse.io/acme/jobs/new', title: 'Updated role' }]);
    assert.deepEqual(rows(value, "SELECT application_url,dedupe_group_id FROM application_jobs WHERE status='queued'").map((row) => ({ ...row })), [{ application_url: 'https://boards.greenhouse.io/acme/jobs/new', dedupe_group_id: 2 }]);
  } finally {
    await closeFixture(value);
  }
});

test('an eligibility downgrade revokes only a still-queued application', async () => {
  const value = await fixture();
  const open = { id: 'downgrade', url: 'https://boards.greenhouse.io/acme/jobs/downgrade' };
  try {
    const initial = await syncSource(options(value, makeAdapter({
      0: { items: [open], totalResults: 1 },
    }), { limit: 1, maxPages: 1 }));
    assert.equal(initial.state, 'succeeded');
    assert.equal(rows(value, 'SELECT status FROM application_jobs')[0].status, 'queued');

    const downgraded = await syncSource(options(value, makeAdapter({
      0: { items: [{ ...open, availabilityState: 'closed' }], totalResults: 1 },
    }), { limit: 1, maxPages: 1 }));
    assert.equal(downgraded.state, 'succeeded');
    assert.deepEqual(rows(value, 'SELECT status,status_reason,dedupe_group_id FROM application_jobs').map((row) => ({ ...row })), [{
      status: 'closed',
      status_reason: 'source_closed',
      dedupe_group_id: 1,
    }]);
    assert.equal(rows(value, 'SELECT eligibility_state FROM jobs')[0].eligibility_state, 'ineligible');
  } finally {
    await closeFixture(value);
  }
});

test('malformed paid response is ambiguous and leaves jobs and checkpoint untouched', async () => {
  const value = await fixture();
  const adapter = makeAdapter({
    0: { items: [{ id: 'valid', url: 'https://boards.greenhouse.io/acme/jobs/valid' }], totalResults: 2 },
    1: { items: [{ id: 'later', url: 'https://boards.greenhouse.io/acme/jobs/later' }], totalResults: 2 },
  }, { malformedPage: 1 });
  try {
    const result = await syncSource(options(value, adapter, { limit: 1, maxPages: 2 }));
    assert.equal(result.state, 'paid_ambiguous');
    assert.equal(rows(value, 'SELECT count(*) AS n FROM jobs')[0].n, 0);
    assert.equal(rows(value, 'SELECT count(*) AS n FROM source_checkpoints')[0].n, 0);
  } finally {
    await closeFixture(value);
  }
});

test('normalized discovery timestamps must remain inside the requested window', async () => {
  for (const { checkpoint, discoveredAt } of [
    { checkpoint: null, discoveredAt: '2026-08-04T00:00:00.000Z' },
    { checkpoint: '2026-08-03T12:00:00.000Z', discoveredAt: '2026-08-03T11:59:59.999Z' },
  ]) {
    const value = await fixture();
    const adapter = makeAdapter({
      0: { items: [{ id: discoveredAt, url: `https://boards.greenhouse.io/acme/jobs/${encodeURIComponent(discoveredAt)}`, discoveredAt }], totalResults: 1 },
    });
    try {
      const result = await syncSource(options(value, adapter, { checkpoint, limit: 1, maxPages: 1 }));
      assert.equal(result.state, 'failed');
      assert.equal(result.reasonCode, 'normalization_time_bounds');
      assert.equal(rows(value, 'SELECT count(*) AS n FROM jobs')[0].n, 0);
      assert.equal(rows(value, 'SELECT count(*) AS n FROM source_checkpoints')[0].n, 0);
    } finally {
      await closeFixture(value);
    }
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

test('accounted paid syncs are serialized across profiles', async () => {
  const value = await fixture();
  const first = makeAdapter({}, {
    creditSnapshots: [creditSnapshot(10, '2026-08-03T11:59:00.000Z')],
  });
  let runId;
  try {
    await assert.rejects(
      () => syncSource(options(value, first, {
        profile: 'first',
        hooks: { afterPending({ runId: value }) { runId = value; return false; } },
      })),
      CrashInjection,
    );
    const second = makeAdapter({}, {
      creditSnapshots: [creditSnapshot(10, '2026-08-03T11:59:00.000Z')],
    });
    await assert.rejects(
      () => syncSource(options(value, second, { profile: 'second' })),
      (error) => error?.reasonCode === 'source_sync_active',
    );
    assert.equal(rows(value, "SELECT count(*) AS n FROM sync_runs WHERE state='fetching'")[0].n, 1);
    assert.equal(runId, 1);
  } finally {
    await closeFixture(value);
  }
});

test('a crash after private receipt publication never replays the paid page', async () => {
  const value = await fixture();
  const calls = [];
  const adapter = makeAdapter({
    0: { items: [{ id: 'receipt-crash', url: 'https://boards.greenhouse.io/acme/jobs/receipt-crash' }], totalResults: 1 },
  }, { calls });
  let runId;
  try {
    await assert.rejects(
      () => syncSource(options(value, adapter, {
        hooks: { afterReceipt({ runId: value }) { runId = value; return false; } },
      })),
      CrashInjection,
    );
    assert.equal(calls.length, 1);
    assert.equal(rows(value, 'SELECT count(*) AS n FROM source_sync_pages')[0].n, 0);
    assert.equal(rows(value, 'SELECT pending_page FROM sync_runs')[0].pending_page, 0);
    const result = await recoverSourceSync(options(value, makeAdapter({}, { calls }), { runId }));
    assert.equal(result.state, 'paid_ambiguous');
    assert.equal(calls.length, 1);
    assert.equal(rows(value, 'SELECT count(*) AS n FROM jobs')[0].n, 0);
  } finally {
    await closeFixture(value);
  }
});

test('a crash after receipt commit resumes without fetching an already complete page', async () => {
  const value = await fixture();
  const calls = [];
  const adapter = makeAdapter({
    0: { items: [{ id: 'receipt-commit-crash', url: 'https://boards.greenhouse.io/acme/jobs/receipt-commit-crash' }], totalResults: 1 },
  }, { calls });
  let runId;
  try {
    await assert.rejects(
      () => syncSource(options(value, adapter, {
        hooks: { afterReceiptCommit({ runId: value }) { runId = value; return false; } },
      })),
      CrashInjection,
    );
    assert.equal(calls.length, 1);
    assert.equal(rows(value, 'SELECT count(*) AS n FROM source_sync_pages')[0].n, 1);
    assert.equal(rows(value, 'SELECT pending_page FROM sync_runs')[0].pending_page, null);
    const result = await recoverSourceSync(options(value, makeAdapter({}, { calls }), { runId }));
    assert.equal(result.state, 'succeeded', JSON.stringify(result));
    assert.equal(calls.length, 1);
    assert.equal(rows(value, 'SELECT count(*) AS n FROM jobs')[0].n, 1);
  } finally {
    await closeFixture(value);
  }
});

test('recovery reuses durable paid paging bounds when current configuration changes', async () => {
  const value = await fixture();
  const calls = [];
  let runId;
  const initial = makeAdapter({
    0: {
      items: [
        { id: 'bounds-a', url: 'https://boards.greenhouse.io/acme/jobs/bounds-a' },
        { id: 'bounds-b', url: 'https://boards.greenhouse.io/acme/jobs/bounds-b' },
      ],
      totalResults: 4,
    },
  }, { calls });
  try {
    await assert.rejects(
      () => syncSource(options(value, initial, {
        limit: 2,
        maxPages: 2,
        maxItems: 4,
        hooks: { afterReceiptCommit({ runId: value }) { runId = value; return false; } },
      })),
      CrashInjection,
    );
    assert.deepEqual(rows(value, 'SELECT page_limit,max_pages,max_items FROM sync_runs').map((row) => ({ ...row })), [{
      page_limit: 2,
      max_pages: 2,
      max_items: 4,
    }]);
    const recovery = makeAdapter({
      1: {
        items: [
          { id: 'bounds-c', url: 'https://boards.greenhouse.io/acme/jobs/bounds-c' },
          { id: 'bounds-d', url: 'https://boards.greenhouse.io/acme/jobs/bounds-d' },
        ],
      },
    }, { calls });
    const result = await recoverSourceSync(options(value, recovery, {
      runId,
      limit: 1,
      maxPages: 1,
      maxItems: 1,
    }));
    assert.equal(result.state, 'succeeded', JSON.stringify(result));
    assert.deepEqual(calls, [
      { page: 0, limit: 2, mode: 'paid' },
      { page: 1, limit: 2, mode: 'paid' },
    ]);
    assert.equal(rows(value, 'SELECT count(*) AS n FROM jobs')[0].n, 4);
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

test('recovery never follows a substituted private artifact symlink', async () => {
  const value = await fixture();
  const calls = [];
  const adapter = makeAdapter({
    0: { items: [{ id: 'symlink', url: 'https://boards.greenhouse.io/acme/jobs/symlink' }], totalResults: 1 },
  }, { calls });
  let runId;
  try {
    await assert.rejects(
      () => syncSource(options(value, adapter, {
        hooks: { beforeCommit({ runId: value }) { runId = value; return false; } },
      })),
      CrashInjection,
    );
    const page = rows(value, 'SELECT response_path FROM source_sync_pages')[0];
    const responsePath = join(value.privateRoot, page.response_path);
    const receipt = JSON.parse(await readFile(responsePath, 'utf8'));
    const rawPath = join(value.privateRoot, receipt.items[0].rawPayloadPath);
    await unlink(responsePath);
    await symlink(rawPath, responsePath);
    const result = await recoverSourceSync(options(value, makeAdapter({}, { calls }), { runId }));
    assert.equal(result.state, 'failed');
    assert.equal(result.reasonCode, 'private_artifact_missing');
    assert.equal(calls.length, 1);
    assert.equal(rows(value, 'SELECT count(*) AS n FROM jobs')[0].n, 0);
  } finally {
    await closeFixture(value);
  }
});

test('a failed accounting baseline creates neither a sync run nor an unaudited row', async () => {
  const value = await fixture();
  const calls = [];
  const adapter = makeAdapter({}, {
    calls,
    creditSnapshots: [Object.assign(new Error('authentication failed'), { code: 'authentication' })],
  });
  try {
    const result = await syncSource(options(value, adapter));
    assert.equal(result.state, 'failed');
    assert.equal(result.syncRunId, null);
    assert.equal(result.failureClass, 'authentication');
    assert.equal(result.reasonCode, 'provider_authentication');
    assert.equal(calls.length, 0);
    assert.equal(rows(value, 'SELECT count(*) AS n FROM sync_runs')[0].n, 0);
    assert.equal(rows(value, 'SELECT count(*) AS n FROM source_credit_audits')[0].n, 0);
  } finally {
    await closeFixture(value);
  }
});

test('paid accounting reconciles provider usage into one durable audit', async () => {
  const value = await fixture();
  const creditCalls = [];
  const adapter = makeAdapter({
    0: { items: [{ id: 'accounted', url: 'https://boards.greenhouse.io/acme/jobs/accounted' }], totalResults: 1 },
  }, {
    creditCalls,
    creditSnapshots: [
      creditSnapshot(10, '2026-08-03T11:59:00.000Z'),
      creditSnapshot(13, '2026-08-03T12:01:00.000Z'),
    ],
  });
  try {
    const result = await syncSource(options(value, adapter, { limit: 1, maxPages: 1 }));
    assert.equal(result.state, 'succeeded', JSON.stringify(result));
    assert.equal(result.estimatedCredits, 1);
    assert.equal(result.reportedCredits, 3);
    assert.equal(creditCalls.length, 2);
    assert.deepEqual(rows(value, 'SELECT state,credits_before,credits_after,reported_credits FROM source_credit_audits').map((row) => ({ ...row })), [{
      state: 'reconciled',
      credits_before: 10,
      credits_after: 13,
      reported_credits: 3,
    }]);
    assert.deepEqual(rows(value, 'SELECT state,reported_credits FROM sync_runs').map((row) => ({ ...row })), [{
      state: 'succeeded',
      reported_credits: 3,
    }]);
  } finally {
    await closeFixture(value);
  }
});

test('terminal paid failures reconcile the accounting audit before settlement', async () => {
  const value = await fixture();
  const adapter = makeAdapter({
    0: { items: [{ id: 'bounded', url: 'https://boards.greenhouse.io/acme/jobs/bounded' }], totalResults: 3 },
  }, {
    creditSnapshots: [
      creditSnapshot(10, '2026-08-03T11:59:00.000Z'),
      creditSnapshot(12, '2026-08-03T12:01:00.000Z'),
    ],
  });
  try {
    const result = await syncSource(options(value, adapter, { limit: 1, maxPages: 1 }));
    assert.equal(result.state, 'failed');
    assert.equal(result.reasonCode, 'page_bounds_exceeded');
    assert.equal(result.reportedCredits, 2);
    assert.deepEqual(rows(value, 'SELECT state,reported_credits FROM source_credit_audits').map((row) => ({ ...row })), [{
      state: 'reconciled',
      reported_credits: 2,
    }]);
    assert.equal(rows(value, 'SELECT count(*) AS n FROM jobs')[0].n, 0);
  } finally {
    await closeFixture(value);
  }
});

test('terminal paid failures become ambiguous when provider usage cannot reconcile', async () => {
  const value = await fixture();
  const adapter = makeAdapter({
    0: { items: [{ id: 'bounded', url: 'https://boards.greenhouse.io/acme/jobs/bounded' }], totalResults: 3 },
  }, {

    creditSnapshots: [
      creditSnapshot(10, '2026-08-03T11:59:00.000Z'),
      Object.assign(new Error('provider unavailable'), { code: 'retryable_preview' }),
    ],
  });
  try {
    const result = await syncSource(options(value, adapter, { limit: 1, maxPages: 1 }));
    assert.equal(result.state, 'paid_ambiguous');
    assert.equal(result.reasonCode, 'credit_reconciliation_unavailable');
    assert.deepEqual(rows(value, 'SELECT state,reported_credits FROM source_credit_audits').map((row) => ({ ...row })), [{
      state: 'unavailable',
      reported_credits: null,
    }]);
    assert.equal(rows(value, 'SELECT count(*) AS n FROM jobs')[0].n, 0);
  } finally {
    await closeFixture(value);
  }
});
test('a paid run spanning UTC accounting periods is ambiguous and cannot commit jobs', async () => {
  const value = await fixture();
  const adapter = makeAdapter({
    0: {
      items: [{ id: 'cross-period', url: 'https://boards.greenhouse.io/acme/jobs/cross-period' }],
      totalResults: 1,
      receivedAt: '2026-08-04T00:01:00.000Z',
    },
  }, {
    creditSnapshots: [
      creditSnapshot(30, '2026-08-03T23:59:00.000Z'),
    ],
  });
  try {
    const result = await syncSource(options(value, adapter));
    assert.equal(result.state, 'paid_ambiguous');
    assert.equal(rows(value, 'SELECT count(*) AS n FROM jobs')[0].n, 0);
    assert.deepEqual(rows(value, 'SELECT state,reason_code FROM source_credit_audits').map((row) => ({ ...row })), [{
      state: 'unavailable',
      reason_code: 'credit_period_spanned',
    }]);
  } finally {
    await closeFixture(value);
  }
});

test('unavailable or negative paid reconciliation is ambiguous and cannot commit jobs', async () => {
  for (const after of [
    Object.assign(new Error('provider unavailable'), { code: 'retryable_preview' }),
    creditSnapshot(9, '2026-08-03T12:01:00.000Z'),
    creditSnapshot(12, '2026-08-03T11:58:00.000Z'),
  ]) {
    const value = await fixture();
    const adapter = makeAdapter({
      0: { items: [{ id: 'unreconciled', url: 'https://boards.greenhouse.io/acme/jobs/unreconciled' }], totalResults: 1 },
    }, {
      creditSnapshots: [
        creditSnapshot(10, '2026-08-03T11:59:00.000Z'),
        after,
      ],
    });
    try {
      const result = await syncSource(options(value, adapter, { limit: 1, maxPages: 1 }));
      assert.equal(result.state, 'paid_ambiguous');
      assert.match(result.reasonCode, /^credit_(?:reconciliation_unavailable|usage_regressed|observation_regressed)$/u);
      assert.equal(rows(value, 'SELECT count(*) AS n FROM jobs')[0].n, 0);
      assert.equal(rows(value, 'SELECT count(*) AS n FROM source_checkpoints')[0].n, 0);
      assert.deepEqual(rows(value, 'SELECT state,reported_credits FROM source_credit_audits').map((row) => ({ ...row })), [{
        state: 'unavailable',
        reported_credits: null,
      }]);
    } finally {
      await closeFixture(value);
    }
  }
});

test('ambiguous paid responses retain reconciled provider credits without committing jobs', async () => {
  const value = await fixture();
  const adapter = makeAdapter({
    0: { items: [{ id: 'ambiguous', url: 'https://boards.greenhouse.io/acme/jobs/ambiguous' }], totalResults: 1 },
  }, {
    malformedPage: 0,
    creditSnapshots: [
      creditSnapshot(30, '2026-08-03T11:59:00.000Z'),
      creditSnapshot(32, '2026-08-03T12:01:00.000Z'),
    ],
  });
  try {
    const result = await syncSource(options(value, adapter, { limit: 1, maxPages: 1 }));
    assert.equal(result.state, 'paid_ambiguous');
    assert.equal(result.reportedCredits, 2);
    assert.equal(rows(value, 'SELECT count(*) AS n FROM jobs')[0].n, 0);
    assert.deepEqual(rows(value, 'SELECT state,reported_credits FROM source_credit_audits').map((row) => ({ ...row })), [{
      state: 'reconciled',
      reported_credits: 2,
    }]);
  } finally {
    await closeFixture(value);
  }
});

test('ready-to-commit recovery reconciles the existing audit without replaying a paid page', async () => {
  const value = await fixture();
  const calls = [];
  const beforeCalls = [];
  const afterCalls = [];
  let runId;
  const initial = makeAdapter({
    0: { items: [{ id: 'recover-accounting', url: 'https://boards.greenhouse.io/acme/jobs/recover-accounting' }], totalResults: 1 },
  }, {
    calls,
    creditCalls: beforeCalls,
    creditSnapshots: [creditSnapshot(20, '2026-08-03T11:59:00.000Z')],
  });
  try {
    await assert.rejects(
      () => syncSource(options(value, initial, {
        limit: 1,
        maxPages: 1,
        hooks: { beforeCommit({ runId: value }) { runId = value; return false; } },
      })),
      CrashInjection,
    );
    assert.equal(calls.length, 1);
    assert.equal(beforeCalls.length, 1);
    assert.equal(rows(value, 'SELECT state FROM source_credit_audits')[0].state, 'pending');

    const recovery = makeAdapter({}, {
      calls,
      creditCalls: afterCalls,
      creditSnapshots: [creditSnapshot(24, '2026-08-04T00:01:00.000Z')],
    });
    const result = await recoverSourceSync(options(value, recovery, { runId }));
    assert.equal(result.state, 'succeeded', JSON.stringify(result));
    assert.equal(result.reportedCredits, 4);
    assert.equal(calls.length, 1);
    assert.equal(afterCalls.length, 1);
    assert.deepEqual(afterCalls[0], {
      periodStart: '2026-08-03T00:00:00.000Z',
      periodEnd: '2026-08-03T23:59:59.999Z',
    });
    assert.equal(rows(value, 'SELECT count(*) AS n FROM jobs')[0].n, 1);
    assert.deepEqual(rows(value, 'SELECT state,reported_credits FROM source_credit_audits').map((row) => ({ ...row })), [{
      state: 'reconciled',
      reported_credits: 4,
    }]);
  } finally {
    await closeFixture(value);
  }
});

test('maxItems constrains the paid provider request body before fetching', async () => {
  const value = await fixture();
  const calls = [];
  const adapter = makeAdapter({
    0: { items: [{ id: 'bounded', url: 'https://boards.greenhouse.io/acme/jobs/bounded' }], totalResults: 1 },
  }, { calls });
  try {
    const result = await syncSource(options(value, adapter, { limit: 2, maxItems: 1 }));
    assert.equal(result.state, 'succeeded', JSON.stringify(result));
    assert.equal(calls.length, 1);
    assert.equal(calls[0].limit, 1);
    assert.equal(rows(value, 'SELECT count(*) AS n FROM jobs')[0].n, 1);
  } finally {
    await closeFixture(value);
  }
});

test('a non-divisible maxItems bound never changes paid page size or replays results', async () => {
  const value = await fixture();
  const calls = [];
  const adapter = makeAdapter({
    0: {
      items: [
        { id: 'fixed-a', url: 'https://boards.greenhouse.io/acme/jobs/fixed-a' },
        { id: 'fixed-b', url: 'https://boards.greenhouse.io/acme/jobs/fixed-b' },
        { id: 'fixed-c', url: 'https://boards.greenhouse.io/acme/jobs/fixed-c' },
      ],
      totalResults: 5,
    },
  }, { calls });
  try {
    const result = await syncSource(options(value, adapter, { limit: 3, maxItems: 5 }));
    assert.equal(result.state, 'failed');
    assert.equal(result.reasonCode, 'item_bounds_exceeded');
    assert.deepEqual(calls, [{ page: 0, limit: 3, mode: 'paid' }]);
    assert.equal(rows(value, 'SELECT count(*) AS n FROM jobs')[0].n, 0);
  } finally {
    await closeFixture(value);
  }
});

test('exact full pages without totals continue until an empty page proves completion', async () => {
  const value = await fixture();
  const calls = [];
  const adapter = makeAdapter({
    0: { items: [{ id: 'a', url: 'https://boards.greenhouse.io/acme/jobs/a' }, { id: 'b', url: 'https://boards.greenhouse.io/acme/jobs/b' }] },
    1: { items: [{ id: 'c', url: 'https://boards.greenhouse.io/acme/jobs/c' }, { id: 'd', url: 'https://boards.greenhouse.io/acme/jobs/d' }] },
    2: { items: [] },
  }, { calls });
  try {
    const result = await syncSource(options(value, adapter, { maxPages: 3 }));
    assert.equal(result.state, 'succeeded', JSON.stringify(result));
    assert.equal(result.jobsSeen, 4);
    assert.deepEqual(calls.map(({ page }) => page), [0, 1, 2]);
    assert.equal(rows(value, 'SELECT count(*) AS n FROM jobs')[0].n, 4);
  } finally {
    await closeFixture(value);
  }
});

test('later pages may omit totals after the first page establishes them', async () => {
  const value = await fixture();
  const calls = [];
  const adapter = makeAdapter({
    0: {
      items: [
        { id: 'a', url: 'https://boards.greenhouse.io/acme/jobs/a' },
        { id: 'b', url: 'https://boards.greenhouse.io/acme/jobs/b' },
      ],
      totalResults: 3,
    },
    1: { items: [{ id: 'c', url: 'https://boards.greenhouse.io/acme/jobs/c' }], totalResults: null },
  }, { calls });
  try {
    const result = await syncSource(options(value, adapter));
    assert.equal(result.state, 'succeeded', JSON.stringify(result));
    assert.equal(result.jobsSeen, 3);
    assert.deepEqual(calls.map(({ page }) => page), [0, 1]);
    assert.equal(rows(value, 'SELECT count(*) AS n FROM jobs')[0].n, 3);
  } finally {
    await closeFixture(value);
  }
});

test('repeated full pages stop before another paid request and persist a terminal failure', async () => {
  const value = await fixture();
  const calls = [];
  const repeated = [
    { id: 'a', url: 'https://boards.greenhouse.io/acme/jobs/a' },
    { id: 'b', url: 'https://boards.greenhouse.io/acme/jobs/b' },
  ];
  const adapter = makeAdapter({
    0: { items: repeated },
    1: { items: repeated },
  }, { calls });
  try {
    const result = await syncSource(options(value, adapter));
    assert.equal(result.state, 'failed');
    assert.equal(result.reasonCode, 'pagination_repeated_page');
    assert.deepEqual(calls.map(({ page }) => page), [0, 1]);
    assert.deepEqual(rows(value, 'SELECT state,reason_code FROM sync_runs').map((row) => ({ ...row })), [{ state: 'failed', reason_code: 'pagination_repeated_page' }]);
    assert.equal(rows(value, 'SELECT count(*) AS n FROM jobs')[0].n, 0);
  } finally {
    await closeFixture(value);
  }
});

test('partial final pages must reconcile with the provider total', async () => {
  const value = await fixture();
  const adapter = makeAdapter({
    0: { items: [{ id: 'a', url: 'https://boards.greenhouse.io/acme/jobs/a' }], totalResults: 3 },
  });
  try {
    const result = await syncSource(options(value, adapter));
    assert.equal(result.state, 'failed');
    assert.equal(result.reasonCode, 'pagination_total_mismatch');
    assert.equal(rows(value, 'SELECT state FROM sync_runs')[0].state, 'failed');
    assert.equal(rows(value, 'SELECT count(*) AS n FROM jobs')[0].n, 0);
  } finally {
    await closeFixture(value);
  }
});
