import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { promises as fsp } from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import test from 'node:test';

const SQLITE = 'sqlite3';

async function sqlite(database, statement) {
  const stdout = await new Promise((resolve, reject) => {
    const child = spawn(SQLITE, ['-bail', '-json', database], {
      stdio: ['pipe', 'pipe', 'pipe'],
    });
    let output = '';
    let errorOutput = '';
    child.stdout.setEncoding('utf8');
    child.stderr.setEncoding('utf8');
    child.stdout.on('data', (chunk) => { output += chunk; });
    child.stderr.on('data', (chunk) => { errorOutput += chunk; });
    child.once('error', reject);
    child.once('close', (code) => {
      if (code === 0) resolve(output);
      else reject(new Error(errorOutput || `sqlite exited ${code}`));
    });
    child.stdin.end(statement);
  });
  const text = stdout.trim();
  return text === '' ? [] : JSON.parse(text);
}

async function migration(namePrefix) {
  const names = await fsp.readdir('migrations');
  const name = names.find((entry) => entry.startsWith(namePrefix) && entry.endsWith('.sql'));
  assert.ok(name, `${namePrefix} migration must be present`);
  return fsp.readFile(path.join('migrations', name), 'utf8');
}

function sql(value) {
  if (value === null || value === undefined) return 'NULL';
  if (typeof value === 'number') return String(value);
  return `'${String(value).replaceAll("'", "''")}'`;
}

async function createPreMigrationDatabase(database, rows = []) {
  await sqlite(database, `
PRAGMA foreign_keys = ON;
CREATE TABLE application_jobs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  source_table TEXT NOT NULL CHECK (source_table IN ('legacy_jobs','assistant_jobs')),
  source_db TEXT NOT NULL,
  source_rowid INTEGER NOT NULL,
  source_job_id TEXT NOT NULL,
  application_url TEXT NOT NULL,
  eligibility_tier TEXT NOT NULL CHECK (eligibility_tier IN ('active_verified','backfill_only','unverified_stale')),
  verification_reason TEXT,
  source_posted_at TEXT,
  source_last_seen_at TEXT,
  status TEXT NOT NULL DEFAULT 'queued' CHECK (status IN ('queued','claimed','ready_for_human','blocked','closed','skipped','failed')),
  status_reason TEXT,
  claimed_at TEXT,
  completed_at TEXT,
  UNIQUE(source_table, source_db, source_rowid),
  UNIQUE(application_url)
);
CREATE TABLE application_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  job_id INTEGER NOT NULL REFERENCES application_jobs(id) ON DELETE RESTRICT,
  status TEXT NOT NULL CHECK (status IN ('preparing','ready_for_human','blocked','closed','failed')),
  reason_code TEXT NOT NULL,
  started_at TEXT NOT NULL,
  finished_at TEXT,
  final_url TEXT,
  actions_json TEXT NOT NULL DEFAULT '[]',
  evidence_path TEXT NOT NULL,
  submit_action_count INTEGER NOT NULL DEFAULT 0 CHECK (submit_action_count = 0)
);
${rows.map((row) => `INSERT INTO application_jobs (
  id, source_table, source_db, source_rowid, source_job_id, application_url,
  eligibility_tier, verification_reason, source_posted_at, source_last_seen_at,
  status, status_reason, claimed_at, completed_at
) VALUES (
  ${sql(row.id)}, ${sql(row.sourceTable ?? 'legacy_jobs')}, ${sql(row.sourceDb ?? 'fixture.sqlite')},
  ${sql(row.sourceRowid ?? row.id)}, ${sql(row.sourceJobId ?? `fixture:${row.id}`)},
  ${sql(row.applicationUrl ?? `https://example.test/jobs/${row.id}`)}, ${sql(row.tier ?? 'active_verified')},
  ${sql(row.verificationReason ?? 'fixture_reason')}, ${sql(row.postedAt ?? '2026-07-25T00:00:00.000Z')},
  ${sql(row.lastSeen ?? '2026-07-25T00:05:00.000Z')}, ${sql(row.status ?? 'queued')},
  ${sql(row.statusReason)}, ${sql(row.claimedAt)}, ${sql(row.completedAt)}
);`).join('\n')}
`);
}

async function applyHistory(database) {
  await sqlite(database, await migration('001-'));
  await sqlite(database, await migration('002-'));
  await sqlite(database, await migration('003-'));
  await sqlite(database, await migration('004-'));
}

async function createPost004Database(rows) {
  const root = await fsp.mkdtemp(path.join(os.tmpdir(), 'phase3-migration-'));
  const database = path.join(root, 'jobs.sqlite');
  await createPreMigrationDatabase(database, rows);
  await applyHistory(database);
  return { root, database };
}

function activeRunInsert({ id, jobId, ownerId, browserSessionId }) {
  return `INSERT INTO application_runs (
  id, job_id, status, reason_code, started_at, finished_at, final_url, actions_json,
  evidence_path, submit_action_count, active, owner_id, browser_session_id,
  claimed_at, lease_expires_at, last_progress_at, workspace_path,
  answer_memory_path, resume_artifact_path, resume_artifact_sha256, blocker_alias
) VALUES (
  ${sql(id)}, ${sql(jobId)}, 'applying', 'claimed_by_backlog_runner',
  '2026-07-26T00:00:00.000Z', NULL, NULL, '[]', '/private/run-${jobId}/evidence', NULL,
  1, ${sql(ownerId)}, ${sql(browserSessionId)}, '2026-07-26T00:00:00.000Z',
  '2026-07-26T00:01:00.000Z', '2026-07-26T00:00:00.000Z', '/private/run-${jobId}',
  '/private/answer-memory.jsonl', '/private/resume.pdf', '${'a'.repeat(64)}', NULL
);`;
}

test('migration history preserves terminal rows and TENEX-like NULL submit incidents', async (t) => {
  const root = await fsp.mkdtemp(path.join(os.tmpdir(), 'phase3-migration-history-'));
  t.after(() => fsp.rm(root, { recursive: true, force: true }));
  const database = path.join(root, 'jobs.sqlite');

  await createPreMigrationDatabase(database, [
    {
      id: 1,
      status: 'ready_for_human',
      statusReason: 'prepared',
      sourceDb: '/private/TENEX.sqlite',
      sourceJobId: 'TENEX:1',
      applicationUrl: 'https://example.test/tenex/1',
      claimedAt: '2026-07-25T00:00:00.000Z',
      completedAt: '2026-07-25T00:10:00.000Z',
    },
    {
      id: 2,
      status: 'closed',
      statusReason: 'posting_closed',
      sourceDb: '/private/TENEX.sqlite',
      sourceJobId: 'TENEX:2',
      applicationUrl: 'https://example.test/tenex/2',
      claimedAt: '2026-07-25T00:00:00.000Z',
      completedAt: '2026-07-25T00:10:00.000Z',
    },
    {
      id: 3,
      status: 'failed',
      statusReason: 'legacy_failure',
      sourceDb: '/private/TENEX.sqlite',
      sourceJobId: 'TENEX:3',
      applicationUrl: 'https://example.test/tenex/3',
    },
  ]);
  await sqlite(database, `
INSERT INTO application_runs VALUES
  (10, 1, 'ready_for_human', 'prepared', '2026-07-25T00:00:00.000Z',
   '2026-07-25T00:10:00.000Z', NULL, '[]', '/private/run-1', 0),
  (11, 2, 'closed', 'posting_closed', '2026-07-25T00:00:00.000Z',
   '2026-07-25T00:10:00.000Z', NULL, '[]', '/private/run-2', 0);
`);
  await sqlite(database, await migration('001-'));
  await sqlite(database, await migration('002-'));
  await sqlite(database, await migration('003-'));
  await sqlite(database, `
INSERT INTO application_runs (
  id, job_id, status, reason_code, started_at, finished_at, final_url,
  actions_json, evidence_path, submit_action_count
) VALUES (
  12, 3, 'failed', 'noncanonical_submission_receipt',
  '2026-07-25T00:00:00.000Z', '2026-07-25T00:10:00.000Z', NULL,
  '[]', '/private/TENEX/run-3/evidence', NULL
);
`);
  await sqlite(database, await migration('004-'));

  assert.deepEqual(await sqlite(database, `
SELECT id, status, status_reason, claimed_at, completed_at
FROM application_jobs ORDER BY id;
`), [
    { id: 1, status: 'queued', status_reason: null, claimed_at: null, completed_at: null },
    {
      id: 2,
      status: 'closed',
      status_reason: 'posting_closed',
      claimed_at: '2026-07-25T00:00:00.000Z',
      completed_at: '2026-07-25T00:10:00.000Z',
    },
    {
      id: 3,
      status: 'failed',
      status_reason: 'legacy_failure',
      claimed_at: null,
      completed_at: null,
    },
  ]);
  assert.deepEqual(await sqlite(database, `
SELECT id, job_id, status, reason_code, evidence_path, submit_action_count,
       active, owner_id, browser_session_id, workspace_path,
       answer_memory_path, resume_artifact_path, resume_artifact_sha256, blocker_alias
FROM application_runs ORDER BY id;
`), [
    {
      id: 10,
      job_id: 1,
      status: 'failed',
      reason_code: 'legacy_prepared_not_submitted',
      evidence_path: '/private/run-1',
      submit_action_count: 0,
      active: 0,
      owner_id: null,
      browser_session_id: null,
      workspace_path: null,
      answer_memory_path: null,
      resume_artifact_path: null,
      resume_artifact_sha256: null,
      blocker_alias: null,
    },
    {
      id: 11,
      job_id: 2,
      status: 'closed',
      reason_code: 'posting_closed',
      evidence_path: '/private/run-2',
      submit_action_count: 0,
      active: 0,
      owner_id: null,
      browser_session_id: null,
      workspace_path: null,
      answer_memory_path: null,
      resume_artifact_path: null,
      resume_artifact_sha256: null,
      blocker_alias: null,
    },
    {
      id: 12,
      job_id: 3,
      status: 'failed',
      reason_code: 'noncanonical_submission_receipt',
      evidence_path: '/private/TENEX/run-3/evidence',
      submit_action_count: null,
      active: 0,
      owner_id: null,
      browser_session_id: null,
      workspace_path: null,
      answer_memory_path: null,
      resume_artifact_path: null,
      resume_artifact_sha256: null,
      blocker_alias: null,
    },
  ]);
  await sqlite(database, await migration('005-'));
  assert.deepEqual(await sqlite(database, `
SELECT id, status, status_reason, claimed_at, completed_at
FROM application_jobs ORDER BY id;
`), [
    {
      id: 1,
      status: 'skipped',
      status_reason: 'platform_reingest_required',
      claimed_at: null,
      completed_at: null,
    },
    {
      id: 2,
      status: 'closed',
      status_reason: 'posting_closed',
      claimed_at: '2026-07-25T00:00:00.000Z',
      completed_at: '2026-07-25T00:10:00.000Z',
    },
    {
      id: 3,
      status: 'failed',
      status_reason: 'legacy_failure',
      claimed_at: null,
      completed_at: null,
    },
  ]);
  assert.deepEqual(await sqlite(database, `
SELECT id, job_id, status FROM application_runs ORDER BY id;
`), [
    { id: 10, job_id: 1, status: 'failed' },
    { id: 11, job_id: 2, status: 'closed' },
    { id: 12, job_id: 3, status: 'failed' },
  ]);
  assert.deepEqual(await sqlite(database, 'PRAGMA foreign_key_check;'), []);
  await sqlite(database, `
INSERT INTO application_jobs (
  source_table, source_db, source_rowid, source_job_id, application_url,
  eligibility_tier, source_last_seen_at, status, platform, job_title,
  job_company, job_location, job_description, job_description_sha256
) VALUES (
  'jobs', '/private/source.sqlite', 100, 'jobs:100',
  'https://job-boards.greenhouse.io/example/jobs/100', 'active_verified',
  '2026-07-28T00:00:00.000Z', 'queued', 'greenhouse', 'Engineer',
  'Example', 'Remote', 'Build systems.', '${'a'.repeat(64)}'
);
`);
  assert.deepEqual(await sqlite(database, `
SELECT source_table, platform, status FROM application_jobs WHERE source_job_id = 'jobs:100';
`), [{ source_table: 'jobs', platform: 'greenhouse', status: 'queued' }]);
});

test('migration 004 creates active uniqueness for the same job and globally across different jobs', async (t) => {
  await t.test('same job cannot have two active rows', async () => {
    const value = await createPost004Database([{ id: 1 }, { id: 2 }]);
    t.after(() => fsp.rm(value.root, { recursive: true, force: true }));
    await sqlite(value.database, activeRunInsert({ id: 1, jobId: 1, ownerId: 'owner-a', browserSessionId: 'browser-a' }));
    await assert.rejects(
      sqlite(value.database, activeRunInsert({ id: 2, jobId: 1, ownerId: 'owner-b', browserSessionId: 'browser-b' })),
      /UNIQUE constraint failed|constraint failed/u,
    );
    assert.deepEqual(await sqlite(value.database, 'SELECT count(*) AS count FROM application_runs WHERE active = 1;'), [{ count: 1 }]);
  });

  await t.test('different jobs and owners cannot have two active rows under global max one', async () => {
    const value = await createPost004Database([{ id: 10 }, { id: 11 }]);
    t.after(() => fsp.rm(value.root, { recursive: true, force: true }));
    await sqlite(value.database, activeRunInsert({ id: 1, jobId: 10, ownerId: 'owner-a', browserSessionId: 'browser-a' }));
    await assert.rejects(
      sqlite(value.database, activeRunInsert({ id: 2, jobId: 11, ownerId: 'owner-b', browserSessionId: 'browser-b' })),
      /UNIQUE constraint failed|constraint failed/u,
    );
    assert.deepEqual(await sqlite(value.database, 'SELECT job_id, owner_id FROM application_runs WHERE active = 1;'), [
      { job_id: 10, owner_id: 'owner-a' },
    ]);
  });
});

test('migration 005 refuses to rebuild while a durable run is active', async (t) => {
  const value = await createPost004Database([{ id: 1 }]);
  t.after(() => fsp.rm(value.root, { recursive: true, force: true }));
  await sqlite(value.database, activeRunInsert({
    id: 1,
    jobId: 1,
    ownerId: 'owner-active',
    browserSessionId: 'browser-active',
  }));

  await assert.rejects(
    sqlite(value.database, await migration('005-')),
    /CHECK constraint failed|constraint failed/u,
  );
  assert.deepEqual(
    await sqlite(value.database, 'SELECT id, active, status FROM application_runs;'),
    [{ id: 1, active: 1, status: 'applying' }],
  );
  assert.deepEqual(
    await sqlite(value.database, 'SELECT id, status FROM application_jobs;'),
    [{ id: 1, status: 'queued' }],
  );
});
