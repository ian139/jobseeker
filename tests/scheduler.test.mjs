import assert from 'node:assert/strict';
import { existsSync, readFileSync, statSync } from 'node:fs';
import { chmod, mkdir, mkdtemp, rm, symlink, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import test from 'node:test';
import { createGreenhouseAdapter } from '../src/ingestion/greenhouse.mjs';
import { initializeIngestionDatabase, openIngestionDatabase } from '../src/ingestion/database.mjs';
import {
  consumeOmpWake,
  formatFormattedDate,
  lastSucceededRunAt,
  loadConfig,
  loadEnv,
  MAX_SCHEDULER_CONFIG_BYTES,
  MAX_WAKE_PAYLOAD_BYTES,
  normalizeSchedulerConfig,
  OmpWakeAbortedError,
  OmpWakeInvalidError,
  readSchedulerConfigFile,
  runDailyScheduler,
  waitForOmpWake,
  wakeOmpSession,
  withinMinimumInterval,
} from '../src/scheduler/run-daily.mjs';

function createMockAdapter() {
  const mockFetch = async () =>
    new Response(
      JSON.stringify({
        jobs: [
          {
            id: 12345,
            title: 'Senior Software Engineer',
            updated_at: '2026-08-01T00:00:00Z',
            absolute_url: 'https://boards.greenhouse.io/stripe/jobs/12345',
            location: { name: 'San Francisco, CA' },
            content: 'Job description content here.',
          },
        ],
      }),
      {
        status: 200,
        headers: { 'content-type': 'application/json' },
      }
    );

  return createGreenhouseAdapter({
    boardToken: 'stripe',
    fetch: mockFetch,
  });
}

function insertPaidAmbiguousRun(db, { source, profile, now }) {
  const values = [
    source, profile, 'paid', 'paid_ambiguous', now, now, now, null,
    null, `source-sync/${source}`, 1, 0, 0, 0, 0, 0, 0, 0, 0, null,
    null, null, null, 0, null, 'paid_ambiguous', 'paid_request_ambiguous', null, 25, 16, 400,
  ];
  const result = db.prepare(`INSERT INTO sync_runs (
    source,profile,mode,state,started_at,finished_at,window_end_at,checkpoint_before,
    checkpoint_after,artifact_dir,request_count,pages_fetched,jobs_seen,jobs_inserted,
    jobs_updated,jobs_unchanged,dedupe_groups_touched,queue_rows_inserted,estimated_credits,
    reported_credits,pending_page,pending_request_sha256,pending_started_at,next_page,
    expected_total_results,failure_class,reason_code,result_sha256,page_limit,max_pages,max_items
  ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)`, values).run(...values);
  return Number(result.lastInsertRowid);
}

test('scheduler: formatFormattedDate formats date in target timeZone', () => {
  const dateStr = formatFormattedDate('2026-08-04T14:00:00.000Z', 'America/New_York');
  assert.equal(dateStr, '2026-08-04');
});

test('scheduler: wakeOmpSession writes timestamped sentinel file', async () => {
  const tmpDir = await mkdtemp(join(tmpdir(), 'scheduler-test-wake-'));
  const flagPath = join(tmpDir, 'wake-omp.flag');

  try {
    const writtenPath = await wakeOmpSession({ reason: 'unit_test_complete', flagPath });
    assert.equal(writtenPath, flagPath);
    assert.equal(existsSync(flagPath), true);

    const content = JSON.parse(readFileSync(flagPath, 'utf8'));
    assert.equal(content.reason, 'unit_test_complete');
    assert.ok(typeof content.timestamp === 'string');
  } finally {
    await rm(tmpDir, { recursive: true, force: true });
  }
});

test('scheduler: wake signals are owner-only and consumed once', async () => {
  const tmpDir = await mkdtemp(join(tmpdir(), 'scheduler-test-consume-'));
  const flagPath = join(tmpDir, 'wake-omp.flag');
  try {
    await wakeOmpSession({ reason: 'task_started', flagPath });
    assert.equal(statSync(flagPath).mode & 0o777, 0o600);
    const wake = await consumeOmpWake({ flagPath });
    assert.deepEqual(wake, {
      kind: 'wake',
      timestamp: wake.timestamp,
      reason: 'task_started',
    });
    assert.equal(Object.isFrozen(wake), true);
    assert.equal(existsSync(flagPath), false);
    assert.deepEqual(await consumeOmpWake({ flagPath }), { kind: 'idle' });
  } finally {
    await rm(tmpDir, { recursive: true, force: true });
  }
});

test('scheduler: publication after an atomic claim leaves the next wake intact', async () => {
  const tmpDir = await mkdtemp(join(tmpdir(), 'scheduler-test-race-'));
  const flagPath = join(tmpDir, 'wake-omp.flag');
  try {
    await wakeOmpSession({ reason: 'first_wake', flagPath });
    const consuming = consumeOmpWake({ flagPath });
    while (existsSync(flagPath)) {
      await new Promise((resolveWait) => setTimeout(resolveWait, 0));
    }
    await wakeOmpSession({ reason: 'second_wake', flagPath });
    assert.equal((await consuming).reason, 'first_wake');
    assert.equal((await consumeOmpWake({ flagPath })).reason, 'second_wake');
    assert.deepEqual(await consumeOmpWake({ flagPath }), { kind: 'idle' });
  } finally {
    await rm(tmpDir, { recursive: true, force: true });
  }
});

test('scheduler: invalid wake payloads fail closed after removing only the claim', async () => {
  const tmpDir = await mkdtemp(join(tmpdir(), 'scheduler-test-invalid-'));
  const flagPath = join(tmpDir, 'wake-omp.flag');
  const outsidePath = join(tmpDir, 'outside.json');
  const invalidPayloads = [
    '{',
    JSON.stringify({ timestamp: new Date().toISOString(), reason: 'test', unknown: true }),
    JSON.stringify({ timestamp: 'yesterday', reason: 'test' }),
    JSON.stringify({ timestamp: new Date().toISOString(), reason: 'not valid' }),
    JSON.stringify({
      timestamp: new Date().toISOString(),
      reason: 'x'.repeat(MAX_WAKE_PAYLOAD_BYTES),
    }),
  ];
  try {
    await assert.rejects(
      wakeOmpSession({ reason: ['task_started'], flagPath }),
      (error) => error instanceof OmpWakeInvalidError && error.code === 'E_OMP_WAKE_INVALID',
    );
    assert.equal(existsSync(flagPath), false);
    for (const payload of invalidPayloads) {
      await writeFile(flagPath, payload, { mode: 0o600 });
      await assert.rejects(
        consumeOmpWake({ flagPath }),
        (error) => error instanceof OmpWakeInvalidError && error.code === 'E_OMP_WAKE_INVALID',
      );
      assert.equal(existsSync(flagPath), false);
    }
    await writeFile(outsidePath, '{"private":"untouched"}', { mode: 0o600 });
    await symlink(outsidePath, flagPath);
    await assert.rejects(
      consumeOmpWake({ flagPath }),
      (error) => error instanceof OmpWakeInvalidError && error.code === 'E_OMP_WAKE_INVALID',
    );
    assert.equal(readFileSync(outsidePath, 'utf8'), '{"private":"untouched"}');
    await mkdir(flagPath);
    await assert.rejects(
      consumeOmpWake({ flagPath }),
      (error) => error instanceof OmpWakeInvalidError && error.code === 'E_OMP_WAKE_INVALID',
    );
    assert.equal(existsSync(flagPath), false);
  } finally {
    await rm(tmpDir, { recursive: true, force: true });
  }
});

test('scheduler: waitForOmpWake returns bounded wake, timeout, and abort results', async () => {
  const tmpDir = await mkdtemp(join(tmpdir(), 'scheduler-test-wait-'));
  const flagPath = join(tmpDir, 'wake-omp.flag');
  try {
    const delayedWake = waitForOmpWake({ flagPath, pollIntervalMs: 5, timeoutMs: 500 });
    setTimeout(() => {
      void wakeOmpSession({ reason: 'delayed_wake', flagPath });
    }, 20);
    assert.equal((await delayedWake).reason, 'delayed_wake');
    assert.deepEqual(
      await waitForOmpWake({ flagPath, pollIntervalMs: 5, timeoutMs: 10 }),
      { kind: 'idle' },
    );

    const controller = new AbortController();
    const waiting = waitForOmpWake({
      flagPath,
      pollIntervalMs: 100,
      timeoutMs: 1_000,
      signal: controller.signal,
    });
    controller.abort();
    await assert.rejects(
      waiting,
      (error) => error instanceof OmpWakeAbortedError && error.code === 'E_OMP_WAKE_ABORTED',
    );
  } finally {
    await rm(tmpDir, { recursive: true, force: true });
  }
});

test('scheduler: loadEnv populates process.env from file without overwriting', async () => {
  const tmpDir = await mkdtemp(join(tmpdir(), 'scheduler-test-env-'));
  const envPath = join(tmpDir, '.env');
  await writeFile(envPath, 'TEST_SCHEDULER_KEY=secret_value_123\n# Comment line\n');

  try {
    delete process.env.TEST_SCHEDULER_KEY;
    loadEnv(envPath);
    assert.equal(process.env.TEST_SCHEDULER_KEY, 'secret_value_123');
  } finally {
    delete process.env.TEST_SCHEDULER_KEY;
    await rm(tmpDir, { recursive: true, force: true });
  }
});

test('scheduler: secure config file loading rejects unsafe paths and accepts a private file', async () => {
  const tmpDir = await mkdtemp(join(tmpdir(), 'scheduler-test-config-'));
  const goodConfigPath = join(tmpDir, 'config-good.json');
  const goodConfig = {
    minimumIntervalHours: 4,
    sources: [{ source: 'greenhouse', profile: 'default', mode: 'free' }],
  };

  try {
    // Owner-only file in an owner-only directory loads.
    await writeFile(goodConfigPath, JSON.stringify(goodConfig), { mode: 0o600 });
    assert.deepEqual(await readSchedulerConfigFile(goodConfigPath), goodConfig);
    assert.deepEqual(await loadConfig({ configPath: goodConfigPath }), goodConfig);

    // Group/world-readable file is rejected.
    await chmod(goodConfigPath, 0o644);
    await assert.rejects(
      readSchedulerConfigFile(goodConfigPath),
      (error) => error?.code === 'E_SCHEDULER_CONFIG' && error.message.includes('config not private'),
    );
    await chmod(goodConfigPath, 0o600);

    // A symlinked config file is rejected.
    const targetPath = join(tmpDir, 'config-target.json');
    const linkPath = join(tmpDir, 'config-link.json');
    await writeFile(targetPath, JSON.stringify(goodConfig), { mode: 0o600 });
    await symlink(targetPath, linkPath);
    await assert.rejects(
      readSchedulerConfigFile(linkPath),
      (error) => error?.code === 'E_SCHEDULER_CONFIG',
    );

    // A group/world-writable parent directory is rejected.
    const looseDir = join(tmpDir, 'loose');
    await mkdir(looseDir);
    await chmod(looseDir, 0o777);
    const looseConfig = join(looseDir, 'config.json');
    await writeFile(looseConfig, JSON.stringify(goodConfig), { mode: 0o600 });
    await assert.rejects(
      readSchedulerConfigFile(looseConfig),
      (error) => error?.code === 'E_SCHEDULER_CONFIG' && error.message.includes('config parent not private'),
    );

    // An oversized config file is rejected.
    const bigConfig = join(tmpDir, 'config-big.json');
    await writeFile(bigConfig, 'x'.repeat(MAX_SCHEDULER_CONFIG_BYTES + 1), { mode: 0o600 });
    await assert.rejects(
      readSchedulerConfigFile(bigConfig),
      (error) => error?.code === 'E_SCHEDULER_CONFIG' && error.message.includes('config not private'),
    );

    // Invalid JSON is rejected.
    const badJson = join(tmpDir, 'config-bad.json');
    await writeFile(badJson, '{ not json', { mode: 0o600 });
    await assert.rejects(
      readSchedulerConfigFile(badJson),
      (error) => error?.code === 'E_SCHEDULER_CONFIG' && error.message.includes('config invalid JSON'),
    );
  } finally {
    await rm(tmpDir, { recursive: true, force: true });
  }
});

test('scheduler: loadConfig honors SCHEDULER_CONFIG_PATH with secure loading', async () => {
  const tmpDir = await mkdtemp(join(tmpdir(), 'scheduler-test-envconfig-'));
  const configPath = join(tmpDir, 'auggood-ingestion.json');
  const config = { source: 'theirstack', profile: 'new_grad_cs', databasePath: join(tmpDir, 'db.sqlite'), privateRoot: join(tmpDir, 'private') };
  try {
    await writeFile(configPath, JSON.stringify(config), { mode: 0o600 });
    const previous = process.env.SCHEDULER_CONFIG_PATH;
    process.env.SCHEDULER_CONFIG_PATH = configPath;
    try {
      assert.deepEqual(await loadConfig({}), config);
    } finally {
      if (previous === undefined) delete process.env.SCHEDULER_CONFIG_PATH;
      else process.env.SCHEDULER_CONFIG_PATH = previous;
    }
  } finally {
    await rm(tmpDir, { recursive: true, force: true });
  }
});

test('scheduler: config shape normalization accepts multi-source and single-source shapes', () => {
  const multi = normalizeSchedulerConfig({
    timezone: 'America/New_York',
    sources: [
      { source: 'greenhouse', profile: 'default', mode: 'free' },
      { source: 'theirstack', mode: 'paid', options: { postedAtMaxAgeDays: 7 } },
    ],
  });
  assert.equal(multi.kind, 'multi');
  assert.equal(multi.sources.length, 2);
  assert.equal(multi.sources[1].profile, 'default');
  assert.deepEqual(multi.sources[1].options, { postedAtMaxAgeDays: 7 });

  const single = normalizeSchedulerConfig({
    source: 'theirstack',
    profile: 'new_grad_cs',
    postedAtMaxAgeDays: 7,
    queryFilters: { url_domain_or: ['ashbyhq.com', 'greenhouse.io'] },
    pageSize: 25,
    maxPages: 16,
    maxItems: 400,
    timeoutMs: 60000,
    maxPreviewRetries: 2,
    retryDelayMs: 1000,
  });
  assert.equal(single.kind, 'single');
  assert.equal(single.sources.length, 1);
  assert.equal(single.sources[0].source, 'theirstack');
  assert.equal(single.sources[0].profile, 'new_grad_cs');
  assert.equal(single.sources[0].mode, 'paid');
  assert.deepEqual(single.sources[0].options, {
    postedAtMaxAgeDays: 7,
    queryFilters: { url_domain_or: ['ashbyhq.com', 'greenhouse.io'] },
    pageSize: 25,
    maxPages: 16,
    maxItems: 400,
    timeoutMs: 60000,
    maxPreviewRetries: 2,
    retryDelayMs: 1000,
  });

  assert.throws(
    () => normalizeSchedulerConfig({ unknownKey: true, sources: [] }),
    (error) => error?.code === 'E_SCHEDULER_CONFIG' && error.message.includes('unknown config keys'),
  );
  assert.throws(
    () => normalizeSchedulerConfig({ timezone: 'America/New_York' }),
    (error) => error?.code === 'E_SCHEDULER_CONFIG',
  );
});

test('scheduler: single-source config maps to one paid source without exposing values', async () => {
  const tmpDir = await mkdtemp(join(tmpdir(), 'scheduler-test-single-'));
  const dbPath = join(tmpDir, 'test.sqlite');
  const privateRoot = join(tmpDir, 'private');
  const flagPath = join(tmpDir, 'wake-omp.flag');

  let capturedSyncOptions = null;
  let capturedAdapterConfig = null;
  const mockSync = async (options) => {
    capturedSyncOptions = options;
    return { state: 'succeeded', syncRunId: 77, queueRowsInserted: 2 };
  };
  const mockCreateAdapter = async (options) => {
    capturedAdapterConfig = options.config;
    return { source: 'theirstack', profile: 'new_grad_cs' };
  };

  const singleSourceConfig = {
    source: 'theirstack',
    profile: 'new_grad_cs',
    postedAtMaxAgeDays: 7,
    queryFilters: { url_domain_or: ['ashbyhq.com', 'greenhouse.io'] },
    pageSize: 25,
    maxPages: 16,
    maxItems: 400,
    databasePath: dbPath,
    privateRoot,
    timeoutMs: 60000,
    maxPreviewRetries: 2,
    retryDelayMs: 1000,
  };

  try {
    initializeIngestionDatabase(dbPath);
    const result = await runDailyScheduler({
      config: singleSourceConfig,
      databasePath: dbPath,
      privateRoot,
      flagPath,
      now: '2026-08-04T10:00:00.000Z',
      createAdapter: mockCreateAdapter,
      syncSource: mockSync,
    });

    assert.equal(result.results.length, 1);
    assert.equal(result.results[0].source, 'theirstack');
    assert.equal(result.results[0].profile, 'new_grad_cs');
    assert.equal(result.results[0].status, 'succeeded');
    assert.equal(result.results[0].syncRunId, 77);
    assert.equal(result.flagPath, flagPath);

    assert.equal(capturedSyncOptions.source, 'theirstack');
    assert.equal(capturedSyncOptions.profile, 'new_grad_cs');
    assert.equal(capturedSyncOptions.mode, 'paid');
    assert.equal(capturedSyncOptions.paidAuthorization, true);
    // Bounds map from the single-source shape.
    assert.equal(capturedSyncOptions.limit, 25);
    assert.equal(capturedSyncOptions.maxPages, 16);
    assert.equal(capturedSyncOptions.maxItems, 400);
    assert.equal(capturedSyncOptions.timeoutMs, 60000);
    assert.equal(capturedSyncOptions.maxPreviewRetries, 2);
    assert.equal(capturedSyncOptions.retryDelayMs, 1000);
    assert.deepEqual(capturedSyncOptions.bounds, {
      limit: 25,
      maxPages: 16,
      maxItems: 400,
      windowEnd: '2026-08-04T10:00:00.000Z',
    });

    // Adapter-relevant options reach the adapter factory.
    assert.deepEqual(capturedAdapterConfig.queryFilters, { url_domain_or: ['ashbyhq.com', 'greenhouse.io'] });
    assert.equal(capturedAdapterConfig.postedAtMaxAgeDays, 7);
    assert.equal(capturedAdapterConfig.timeoutMs, 60000);
    assert.equal(capturedAdapterConfig.maxPreviewRetries, 2);
    assert.equal(capturedAdapterConfig.retryDelayMs, 1000);

    // Private values are never exposed in the scheduler summary.
    const serialized = JSON.stringify(result);
    assert.equal(serialized.includes('ashbyhq.com'), false);
    assert.equal(serialized.includes('url_domain_or'), false);
    assert.equal(serialized.includes('new_grad_cs'), true);
  } finally {
    await rm(tmpDir, { recursive: true, force: true });
  }
});

test('scheduler: minimum interval fences repeated runs and is configurable', async () => {
  const tmpDir = await mkdtemp(join(tmpdir(), 'scheduler-test-interval-'));
  const dbPath = join(tmpDir, 'test.sqlite');
  const privateRoot = join(tmpDir, 'private');
  const flagPath = join(tmpDir, 'wake-omp.flag');

  const mockAdapter = createMockAdapter();
  const mockCreateAdapter = async () => mockAdapter;

  const testConfig = {
    timezone: 'America/New_York',
    databasePath: dbPath,
    privateRoot,
    flagPath,
    minimumIntervalHours: 4,
    sources: [
      {
        source: 'greenhouse',
        profile: 'default',
        mode: 'free',
      },
    ],
  };

  try {
    initializeIngestionDatabase(dbPath);

    // 1. First invocation succeeds and publishes a wake only if rows were queued.
    const firstRun = await runDailyScheduler({
      config: testConfig,
      databasePath: dbPath,
      privateRoot,
      flagPath,
      now: '2026-08-04T10:00:00.000Z',
      createAdapter: mockCreateAdapter,
    });
    assert.equal(firstRun.date, '2026-08-04');
    assert.equal(firstRun.results.length, 1);
    assert.equal(firstRun.results[0].status, 'succeeded');

    const db1 = openIngestionDatabase(dbPath);
    let firstQueueRows;
    try {
      const runs = db1.prepare("SELECT * FROM sync_runs WHERE source='greenhouse' AND state='succeeded'").all();
      assert.equal(runs.length, 1);
      firstQueueRows = Number(runs[0].queue_rows_inserted);
      assert.ok(lastSucceededRunAt({ database: db1, source: 'greenhouse', profile: 'default' }));
    } finally {
      db1.close();
    }

    // 2. Second invocation within the 4h interval is skipped and rewrites no wake.
    await rm(flagPath, { force: true });
    const secondRun = await runDailyScheduler({
      config: testConfig,
      databasePath: dbPath,
      privateRoot,
      flagPath,
      now: '2026-08-04T12:00:00.000Z',
      createAdapter: mockCreateAdapter,
    });
    assert.equal(secondRun.results.length, 1);
    assert.equal(secondRun.results[0].status, 'skipped');
    assert.equal(secondRun.results[0].reason, 'within_minimum_interval');
    assert.equal(secondRun.flagPath, null);
    assert.equal(existsSync(flagPath), false);

    const db2 = openIngestionDatabase(dbPath);
    try {
      const runs = db2.prepare("SELECT * FROM sync_runs WHERE source='greenhouse' AND state='succeeded'").all();
      assert.equal(runs.length, 1);
    } finally {
      db2.close();
    }

    // 3. Third invocation beyond the interval executes again.
    const thirdRun = await runDailyScheduler({
      config: testConfig,
      databasePath: dbPath,
      privateRoot,
      flagPath,
      now: '2026-08-04T15:00:00.000Z',
      createAdapter: mockCreateAdapter,
    });
    assert.equal(thirdRun.results.length, 1);
    assert.equal(thirdRun.results[0].status, 'succeeded');

    const db3 = openIngestionDatabase(dbPath);
    try {
      const runs = db3.prepare("SELECT * FROM sync_runs WHERE source='greenhouse' AND state='succeeded'").all();
      assert.equal(runs.length, 2);
    } finally {
      db3.close();
    }

    // The pure fence is configurable and never skips without a prior success.
    assert.equal(
      withinMinimumInterval({ lastSucceededAt: '2026-08-04T08:00:00.000Z', now: '2026-08-04T10:00:00.000Z', minimumIntervalHours: 4 }),
      true,
    );
    assert.equal(
      withinMinimumInterval({ lastSucceededAt: '2026-08-04T08:00:00.000Z', now: '2026-08-04T10:00:00.000Z', minimumIntervalHours: 1 }),
      false,
    );
    assert.equal(
      withinMinimumInterval({ lastSucceededAt: null, now: '2026-08-04T10:00:00.000Z', minimumIntervalHours: 4 }),
      false,
    );

    // A run that inserted rows must have published a wake; a re-sync of the same
    // dedupe group inserts no queue rows and therefore must not wake.
    assert.equal(firstQueueRows > 0, true);
    assert.equal(firstRun.flagPath, flagPath);
    assert.equal(thirdRun.flagPath, null);
    assert.equal(existsSync(flagPath), false);
  } finally {
    await rm(tmpDir, { recursive: true, force: true });
  }
});

test('scheduler: cycle lock fails closed on overlap', async () => {
  const tmpDir = await mkdtemp(join(tmpdir(), 'scheduler-test-lock-'));
  const dbPath = join(tmpDir, 'test.sqlite');
  const privateRoot = join(tmpDir, 'private');
  const flagPath = join(tmpDir, 'wake-omp.flag');
  const lockPath = join(privateRoot, 'scheduler.lock');

  try {
    initializeIngestionDatabase(dbPath);
    await mkdir(privateRoot, { recursive: true, mode: 0o700 });
    await writeFile(lockPath, '{"pid":99999,"token":"stale","acquiredAt":"2026-08-04T00:00:00.000Z"}', { mode: 0o600 });

    await assert.rejects(
      runDailyScheduler({
        config: {
          timezone: 'America/New_York',
          databasePath: dbPath,
          privateRoot,
          flagPath,
          sources: [{ source: 'greenhouse', profile: 'default', mode: 'free' }],
        },
        databasePath: dbPath,
        privateRoot,
        flagPath,
        now: '2026-08-04T10:00:00.000Z',
        createAdapter: mockAdapterFactoryForLock(),
      }),
      (error) => error?.code === 'E_SCHEDULER_LOCKED',
    );
    // The pre-existing lock is not removed by the failed cycle.
    assert.equal(existsSync(lockPath), true);
  } finally {
    await rm(tmpDir, { recursive: true, force: true });
  }
});

function mockAdapterFactoryForLock() {
  return async () => createMockAdapter();
}

test('scheduler: cycle lock is always released after success and failure', async () => {
  const tmpDir = await mkdtemp(join(tmpdir(), 'scheduler-test-lockrelease-'));
  const dbPath = join(tmpDir, 'test.sqlite');
  const privateRoot = join(tmpDir, 'private');
  const flagPath = join(tmpDir, 'wake-omp.flag');
  const lockPath = join(privateRoot, 'scheduler.lock');

  const baseOptions = {
    databasePath: dbPath,
    privateRoot,
    flagPath,
    now: '2026-08-04T10:00:00.000Z',
  };
  const config = {
    timezone: 'America/New_York',
    databasePath: dbPath,
    privateRoot,
    flagPath,
    sources: [{ source: 'greenhouse', profile: 'default', mode: 'free' }],
  };

  try {
    initializeIngestionDatabase(dbPath);

    // Success path: lock released.
    await runDailyScheduler({ ...baseOptions, config, createAdapter: async () => createMockAdapter() });
    assert.equal(existsSync(lockPath), false);

    // Failure path: sync throws, lock still released.
    await assert.rejects(
      runDailyScheduler({
        ...baseOptions,
        config,
        now: '2026-08-04T20:00:00.000Z',
        createAdapter: async () => createMockAdapter(),
        syncSource: async () => { throw new Error('boom'); },
      }),
      /boom/u,
    );
    assert.equal(existsSync(lockPath), false);
  } finally {
    await rm(tmpDir, { recursive: true, force: true });
  }
});

test('scheduler: paid ambiguous sync result is recorded without wake or replay', async () => {
  const tmpDir = await mkdtemp(join(tmpdir(), 'scheduler-test-ambiguous-'));
  const dbPath = join(tmpDir, 'test.sqlite');
  const privateRoot = join(tmpDir, 'private');
  const flagPath = join(tmpDir, 'wake-omp.flag');

  try {
    initializeIngestionDatabase(dbPath);

    const syncCalls = [];
    const mockSync = async () => {
      syncCalls.push('called');
      return { state: 'paid_ambiguous', syncRunId: 5, reasonCode: 'paid_request_ambiguous', queueRowsInserted: 0 };
    };

    const config = {
      source: 'theirstack',
      profile: 'new_grad_cs',
      pageSize: 25,
      maxPages: 16,
      databasePath: dbPath,
      privateRoot,
    };

    const result = await runDailyScheduler({
      config,
      databasePath: dbPath,
      privateRoot,
      flagPath,
      now: '2026-08-04T10:00:00.000Z',
      createAdapter: async () => ({ source: 'theirstack', profile: 'new_grad_cs' }),
      syncSource: mockSync,
    });

    assert.equal(syncCalls.length, 1);
    assert.equal(result.results.length, 1);
    assert.equal(result.results[0].status, 'paid_ambiguous');
    assert.equal(result.results[0].syncRunId, 5);
    assert.equal(result.results[0].reasonCode, 'paid_request_ambiguous');
    assert.equal(result.flagPath, null);
    assert.equal(existsSync(flagPath), false);
  } finally {
    await rm(tmpDir, { recursive: true, force: true });
  }
});

test('scheduler: a paid ambiguous last run is never replayed', async () => {
  const tmpDir = await mkdtemp(join(tmpdir(), 'scheduler-test-noreplay-'));
  const dbPath = join(tmpDir, 'test.sqlite');
  const privateRoot = join(tmpDir, 'private');
  const flagPath = join(tmpDir, 'wake-omp.flag');

  try {
    initializeIngestionDatabase(dbPath);
    const db = openIngestionDatabase(dbPath);
    let runId;
    try {
      runId = insertPaidAmbiguousRun(db, {
        source: 'theirstack',
        profile: 'new_grad_cs',
        now: '2026-08-03T09:00:00.000Z',
      });
    } finally {
      db.close();
    }

    let syncCalls = 0;
    const config = {
      source: 'theirstack',
      profile: 'new_grad_cs',
      pageSize: 25,
      maxPages: 16,
      databasePath: dbPath,
      privateRoot,
    };

    const result = await runDailyScheduler({
      config,
      databasePath: dbPath,
      privateRoot,
      flagPath,
      now: '2026-08-04T10:00:00.000Z',
      createAdapter: async () => { throw new Error('adapter must not be created'); },
      syncSource: async () => { syncCalls += 1; throw new Error('sync must not replay'); },
    });

    assert.equal(syncCalls, 0);
    assert.equal(result.results.length, 1);
    assert.equal(result.results[0].status, 'paid_ambiguous');
    assert.equal(result.results[0].syncRunId, runId);
    assert.equal(result.results[0].reasonCode, 'paid_request_ambiguous');
    assert.equal(result.flagPath, null);
    assert.equal(existsSync(flagPath), false);

    const db2 = openIngestionDatabase(dbPath);
    try {
      const runs = db2.prepare('SELECT * FROM sync_runs WHERE source=? AND profile=?').all('theirstack', 'new_grad_cs');
      assert.equal(runs.length, 1);
      assert.equal(runs[0].state, 'paid_ambiguous');
    } finally {
      db2.close();
    }
  } finally {
    await rm(tmpDir, { recursive: true, force: true });
  }
});

test('scheduler: wake is published only when total queue rows are inserted', async () => {
  const tmpDir = await mkdtemp(join(tmpdir(), 'scheduler-test-wakeonly-'));
  const dbPath = join(tmpDir, 'test.sqlite');
  const privateRoot = join(tmpDir, 'private');
  const flagPath = join(tmpDir, 'wake-omp.flag');

  try {
    initializeIngestionDatabase(dbPath);

    const config = {
      timezone: 'America/New_York',
      databasePath: dbPath,
      privateRoot,
      flagPath,
      sources: [{ source: 'greenhouse', profile: 'default', mode: 'free' }],
    };

    // No queue rows inserted -> no wake signal.
    const dryRun = await runDailyScheduler({
      config,
      databasePath: dbPath,
      privateRoot,
      flagPath,
      now: '2026-08-04T10:00:00.000Z',
      createAdapter: async () => createMockAdapter(),
      syncSource: async () => ({ state: 'succeeded', syncRunId: 11, queueRowsInserted: 0 }),
    });
    assert.equal(dryRun.flagPath, null);
    assert.equal(existsSync(flagPath), false);

    // Queue rows inserted -> wake signal published.
    const insertRun = await runDailyScheduler({
      config,
      databasePath: dbPath,
      privateRoot,
      flagPath,
      now: '2026-08-04T11:00:00.000Z',
      createAdapter: async () => createMockAdapter(),
      syncSource: async () => ({ state: 'succeeded', syncRunId: 12, queueRowsInserted: 3 }),
    });
    assert.equal(insertRun.flagPath, flagPath);
    assert.equal(existsSync(flagPath), true);
    const wake = await consumeOmpWake({ flagPath });
    assert.equal(wake.reason, 'daily_scheduler_complete');
  } finally {
    await rm(tmpDir, { recursive: true, force: true });
  }
});

test('scheduler: launchd plist invariants', () => {
  const plistPath = new URL('../src/scheduler/launchd/com.ian.jobs.auggood-ingestion.plist', import.meta.url);
  const xml = readFileSync(plistPath, 'utf8');

  // Well-formed tag balance for the XML subset used.
  for (const tag of ['plist', 'dict', 'array', 'string', 'integer', 'key']) {
    const openCount = (xml.match(new RegExp(`<${tag}(?:\\s[^>]*)?>`, 'g')) ?? []).length;
    const closeCount = (xml.match(new RegExp(`</${tag}>`, 'g')) ?? []).length;
    assert.equal(openCount, closeCount, `${tag} tags must balance`);
  }

  const simpleValues = {};
  const simpleRe = /<key>([^<]+)<\/key>\s*<(string|integer|true|false)(?:\s[^>]*)?(?:\/>|>(.*?)<\/\2>)/gs;
  let match;
  while ((match = simpleRe.exec(xml)) !== null) {
    const [, key, tag, text] = match;
    if (tag === 'string') simpleValues[key] = text;
    else if (tag === 'integer') simpleValues[key] = Number(text);
    else simpleValues[key] = tag === 'true';
  }

  assert.equal(simpleValues.Label, 'com.ian.jobs.auggood-ingestion');
  assert.equal(simpleValues.WorkingDirectory, '/Users/ian/Projects/jobs-new');
  assert.equal(simpleValues.StartInterval, 14400);
  assert.equal(simpleValues.RunAtLoad, false);
  assert.equal(simpleValues.KeepAlive, false);
  assert.equal(simpleValues.ProcessType, 'Background');
  assert.ok(simpleValues.StandardOutPath.startsWith('/Users/ian/Projects/jobs-new/private/'));
  assert.ok(simpleValues.StandardErrorPath.startsWith('/Users/ian/Projects/jobs-new/private/'));
  assert.notEqual(simpleValues.StandardOutPath, simpleValues.StandardErrorPath);

  const argsBlock = xml.match(/<key>ProgramArguments<\/key>\s*<array>([\s\S]*?)<\/array>/u);
  assert.ok(argsBlock, 'ProgramArguments array must exist');
  const args = [...argsBlock[1].matchAll(/<string>(.*?)<\/string>/gu)].map((item) => item[1]);
  assert.deepEqual(args, ['/opt/homebrew/bin/node', '/Users/ian/Projects/jobs-new/src/scheduler/run-daily.mjs']);

  const envBlock = xml.match(/<key>EnvironmentVariables<\/key>\s*<dict>([\s\S]*?)<\/dict>/u);
  assert.ok(envBlock, 'EnvironmentVariables dict must exist');
  const env = {};
  for (const item of envBlock[1].matchAll(/<key>([^<]+)<\/key>\s*<string>(.*?)<\/string>/gu)) {
    env[item[1]] = item[2];
  }
  assert.deepEqual(env, { SCHEDULER_CONFIG_PATH: 'private/auggood-ingestion.json' });

  // No systemd keys or references.
  assert.equal(xml.includes('systemd'), false);
  for (const key of ['OnCalendar', 'ExecStart', 'EnvironmentFile', 'Unit', 'Service', 'Timer', 'WantedBy']) {
    assert.equal(xml.includes(`<key>${key}</key>`), false, `plist must not contain systemd key ${key}`);
  }
});
