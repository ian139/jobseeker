import assert from 'node:assert/strict';
import { DatabaseSync } from 'node:sqlite';
import { promises as fsp } from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import test from 'node:test';
import {
  assertIngestionSchema,
  migrateIngestionDatabase,
} from '../src/ingestion/database.mjs';

const NOW = '2026-08-04T00:00:00.000Z';

async function fixture({ malformedRaw = false } = {}) {
  const root = await fsp.mkdtemp(path.join(os.tmpdir(), 'ingestion-migration-'));
  const databasePath = path.join(root, 'jobs.sqlite');
  const payloadRoot = path.join(root, 'payloads');
  const db = new DatabaseSync(databasePath);
  db.exec(`
    CREATE TABLE jobs (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      source TEXT NOT NULL,
      source_job_id TEXT,
      canonical_url TEXT,
      title TEXT NOT NULL,
      company TEXT NOT NULL,
      location TEXT,
      remote INTEGER,
      posted_at TEXT,
      discovered_at TEXT NOT NULL,
      description TEXT,
      status TEXT NOT NULL DEFAULT 'queued',
      raw_json TEXT NOT NULL DEFAULT '{}',
      first_seen_at TEXT NOT NULL,
      last_seen_at TEXT NOT NULL,
      CHECK (source_job_id IS NOT NULL OR canonical_url IS NOT NULL),
      UNIQUE(source, source_job_id),
      UNIQUE(canonical_url)
    );
    CREATE TABLE sync_runs (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      source TEXT NOT NULL,
      profile TEXT,
      mode TEXT NOT NULL,
      started_at TEXT NOT NULL,
      finished_at TEXT,
      checkpoint TEXT,
      success INTEGER NOT NULL DEFAULT 0,
      jobs_seen INTEGER NOT NULL DEFAULT 0,
      jobs_returned INTEGER NOT NULL DEFAULT 0,
      jobs_inserted INTEGER NOT NULL DEFAULT 0,
      jobs_updated INTEGER NOT NULL DEFAULT 0,
      error TEXT
    );
    CREATE TABLE application_jobs (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      source_table TEXT NOT NULL,
      source_db TEXT NOT NULL,
      source_rowid INTEGER NOT NULL,
      source_job_id TEXT NOT NULL,
      application_url TEXT NOT NULL,
      eligibility_tier TEXT NOT NULL,
      verification_reason TEXT,
      source_posted_at TEXT,
      source_last_seen_at TEXT,
      status TEXT NOT NULL DEFAULT 'queued',
      status_reason TEXT,
      claimed_at TEXT,
      completed_at TEXT,
      UNIQUE(source_table, source_db, source_rowid),
      UNIQUE(application_url)
    );
    CREATE TABLE application_runs (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      job_id INTEGER NOT NULL,
      status TEXT NOT NULL,
      reason_code TEXT NOT NULL,
      started_at TEXT NOT NULL,
      finished_at TEXT,
      final_url TEXT,
      actions_json TEXT NOT NULL DEFAULT '[]',
      evidence_path TEXT NOT NULL,
      submit_action_count INTEGER,
      active INTEGER NOT NULL DEFAULT 0,
      owner_id TEXT,
      browser_session_id TEXT,
      claimed_at TEXT,
      lease_expires_at TEXT,
      last_progress_at TEXT,
      workspace_path TEXT,
      resume_artifact_path TEXT,
      resume_artifact_sha256 TEXT,
      answer_memory_path TEXT,
      blocker_alias TEXT
    );
  `);
  const raw = malformedRaw ? '{malformed' : JSON.stringify({ employment_types: ['full_time'], closed_at: null });
  db.prepare(`INSERT INTO jobs (
    id, source, source_job_id, canonical_url, title, company, location, remote,
    posted_at, discovered_at, description, status, raw_json, first_seen_at, last_seen_at
  ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`)
    .run(7, 'fixture', 'job-7', 'https://example.test/jobs/7', 'Role', 'Company', 'Remote', 1,
      '2026-08-01', '2026-08-02T00:00:00.000Z', 'description', 'queued', raw,
      '2026-08-02T00:00:00.000Z', '2026-08-02T00:00:00.000Z');
  db.prepare(`INSERT INTO sync_runs (
    id, source, profile, mode, started_at, finished_at, checkpoint, success,
    jobs_seen, jobs_returned, jobs_inserted, jobs_updated, error
  ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`)
    .run(3, 'fixture', 'profile', 'paid_fetch', '2026-08-02T00:00:00.000Z',
      '2026-08-02T00:01:00.000Z', 'checkpoint-value', 1, 1, 1, 1, 0, null);
  db.prepare(`INSERT INTO application_jobs (
    id, source_table, source_db, source_rowid, source_job_id, application_url,
    eligibility_tier, verification_reason, status
  ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)`)
    .run(9, 'jobs', databasePath, 7, 'job-7', 'https://example.test/jobs/7', 'active_verified', 'fixture', 'queued');
  db.close();
  return { root, databasePath, payloadRoot };
}

test('legacy migration preserves IDs bindings counts and private payload identity', async (t) => {
  const value = await fixture();
  t.after(() => fsp.rm(value.root, { recursive: true, force: true }));
  const first = await migrateIngestionDatabase(value.databasePath, { payloadRoot: value.payloadRoot, now: NOW });
  assert.equal(first.idempotent, false);
  assertIngestionSchema(value.databasePath);
  const db = new DatabaseSync(value.databasePath);
  assert.deepEqual(db.prepare('SELECT id, source_job_id FROM jobs').all().map((row) => ({ id: row.id, source_job_id: row.source_job_id })), [{ id: 7, source_job_id: 'job-7' }]);
  assert.deepEqual(db.prepare('SELECT id, source_rowid, dedupe_group_id FROM application_jobs').all().map((row) => ({ id: row.id, source_rowid: row.source_rowid, dedupe_group_id: row.dedupe_group_id })), [{ id: 9, source_rowid: 7, dedupe_group_id: null }]);
  assert.deepEqual(db.prepare('SELECT source, profile, checkpoint, last_sync_run_id FROM source_checkpoints').all(), [{ source: 'fixture', profile: 'profile', checkpoint: 'checkpoint-value', last_sync_run_id: 3 }]);
  assert.deepEqual(db.prepare('SELECT jobs_seen, jobs_inserted, jobs_updated, error FROM sync_runs').all(), [{ jobs_seen: 1, jobs_inserted: 1, jobs_updated: 0, error: null }]);
  const payloadPath = db.prepare('SELECT raw_payload_path, raw_payload_sha256 FROM jobs').get();
  db.close();
  const stat = await fsp.lstat(payloadPath.raw_payload_path);
  assert(stat.isFile());
  assert.equal(stat.mode & 0o777, 0o600);
  assert.equal((await fsp.readFile(payloadPath.raw_payload_path)).toString('utf8').length > 0, true);
  assert.match(payloadPath.raw_payload_sha256, /^[0-9a-f]{64}$/);
  const second = await migrateIngestionDatabase(value.databasePath, { payloadRoot: value.payloadRoot, now: NOW });
  assert.equal(second.idempotent, true);
});

test('malformed legacy payload rolls back database and staged files', async (t) => {
  const value = await fixture({ malformedRaw: true });
  t.after(() => fsp.rm(value.root, { recursive: true, force: true }));
  await assert.rejects(migrateIngestionDatabase(value.databasePath, { payloadRoot: value.payloadRoot, now: NOW }));
  const db = new DatabaseSync(value.databasePath);
  assert.deepEqual(db.prepare("SELECT name FROM pragma_table_info('jobs') WHERE name = 'raw_json'").all(), [{ name: 'raw_json' }]);
  assert.equal(db.prepare("SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'source_observations'").get(), undefined);
  db.close();
  const payloadEntries = await fsp.readdir(value.payloadRoot);
  assert.deepEqual(payloadEntries, []);
});

test('wrong schema refuses without replacing unrelated database tables', async (t) => {
  const root = await fsp.mkdtemp(path.join(os.tmpdir(), 'ingestion-schema-'));
  t.after(() => fsp.rm(root, { recursive: true, force: true }));
  const databasePath = path.join(root, 'wrong.sqlite');
  const payloadRoot = path.join(root, 'payloads');
  const db = new DatabaseSync(databasePath);
  db.exec('CREATE TABLE jobs (id INTEGER PRIMARY KEY); CREATE TABLE sentinel (value TEXT); INSERT INTO sentinel VALUES (\'kept\');');
  db.close();
  await assert.rejects(migrateIngestionDatabase(databasePath, { payloadRoot, now: NOW }));
  const check = new DatabaseSync(databasePath);
  assert.deepEqual(check.prepare('SELECT value FROM sentinel').all(), [{ value: 'kept' }]);
  check.close();
});
