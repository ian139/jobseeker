import assert from 'node:assert/strict';
import { existsSync, readFileSync } from 'node:fs';
import { mkdtemp, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import test from 'node:test';
import { createGreenhouseAdapter } from '../src/ingestion/greenhouse.mjs';
import { initializeIngestionDatabase, openIngestionDatabase } from '../src/ingestion/database.mjs';
import {
  formatFormattedDate,
  hasSucceededRunToday,
  loadEnv,
  runDailyScheduler,
  wakeOmpSession,
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

test('scheduler: date idempotency skips runs on same date and executes on new date', async () => {
  const tmpDir = await mkdtemp(join(tmpdir(), 'scheduler-test-db-'));
  const dbPath = join(tmpDir, 'test.sqlite');
  const privateRoot = join(tmpDir, 'private_artifacts');
  const flagPath = join(tmpDir, 'wake-omp.flag');

  const mockAdapter = createMockAdapter({ source: 'greenhouse', profile: 'default' });

  const mockCreateAdapter = async () => mockAdapter;

  const testConfig = {
    timezone: 'America/New_York',
    databasePath: dbPath,
    privateRoot,
    flagPath,
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
    // 1. First invocation on 2026-08-04
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
    assert.equal(existsSync(flagPath), true);

    // Verify DB recorded exactly 1 succeeded run
    const db1 = openIngestionDatabase(dbPath);
    try {
      const runs = db1.prepare("SELECT * FROM sync_runs WHERE source='greenhouse' AND state='succeeded'").all();
      assert.equal(runs.length, 1);
      assert.ok(hasSucceededRunToday({ database: db1, source: 'greenhouse', profile: 'default', dateStr: '2026-08-04' }));
    } finally {
      db1.close();
    }

    // 2. Second invocation on same date (2026-08-04 15:00 UTC)
    // Remove flag file to verify wake sentinel gets rewritten even when source is skipped
    await rm(flagPath, { force: true });

    const secondRun = await runDailyScheduler({
      config: testConfig,
      databasePath: dbPath,
      privateRoot,
      flagPath,
      now: '2026-08-04T15:00:00.000Z',
      createAdapter: mockCreateAdapter,
    });

    assert.equal(secondRun.date, '2026-08-04');
    assert.equal(secondRun.results.length, 1);
    assert.equal(secondRun.results[0].status, 'skipped');
    assert.equal(secondRun.results[0].reason, 'already_succeeded_today');
    assert.equal(existsSync(flagPath), true); // Wake sentinel re-written

    // Verify DB STILL has only 1 succeeded run
    const db2 = openIngestionDatabase(dbPath);
    try {
      const runs = db2.prepare("SELECT * FROM sync_runs WHERE source='greenhouse' AND state='succeeded'").all();
      assert.equal(runs.length, 1);
    } finally {
      db2.close();
    }

    // 3. Third invocation on next date (2026-08-05 10:00 UTC)
    const thirdRun = await runDailyScheduler({
      config: testConfig,
      databasePath: dbPath,
      privateRoot,
      flagPath,
      now: '2026-08-05T10:00:00.000Z',
      createAdapter: mockCreateAdapter,
    });

    assert.equal(thirdRun.date, '2026-08-05');
    assert.equal(thirdRun.results.length, 1);
    assert.equal(thirdRun.results[0].status, 'succeeded');

    // Verify DB now has 2 succeeded runs
    const db3 = openIngestionDatabase(dbPath);
    try {
      const runs = db3.prepare("SELECT * FROM sync_runs WHERE source='greenhouse' AND state='succeeded'").all();
      assert.equal(runs.length, 2);
    } finally {
      db3.close();
    }
  } finally {
    await rm(tmpDir, { recursive: true, force: true });
  }
});
