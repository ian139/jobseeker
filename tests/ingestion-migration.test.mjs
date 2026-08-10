import assert from 'node:assert/strict';
import { DatabaseSync } from 'node:sqlite';
import { promises as fsp } from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import test from 'node:test';
import {
  assertIngestionSchema,
  initializeIngestionDatabase,
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
      '2026-08-02T00:01:00.000Z', '2026-08-02T00:00:00.000Z', 1, 1, 1, 1, 0, null);
  db.prepare(`INSERT INTO application_jobs (
    id, source_table, source_db, source_rowid, source_job_id, application_url,
    eligibility_tier, verification_reason, status
  ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)`)
    .run(9, 'jobs', databasePath, 7, 'job-7', 'https://example.test/jobs/7', 'active_verified', 'fixture', 'queued');
  db.close();
  await fsp.chmod(databasePath, 0o600);
  return { root, databasePath, payloadRoot };
}

const V5_SHA256 = '783c09fa4c083f731a90c27f0b61c3242ef0b0224daf656d9281bbf8db35d2c3';
const V6_SHA256 = 'a0cf117e7b155ac7dd842a77250261141b41490c69c502f8e1d72da2de47997e';
const V7_SHA256 = '7b33c5aac22a2efbbfaa5c579182780defa22fa446a0ab7086d07282dfdaf0c4';
const V8_SHA256 = 'b95be0f59a4cd9cda6cb21500e831227b5f413df6b93d5daed9ea6d36d641cf2';

async function v5Fixture() {
  const value = await fsp.mkdtemp(path.join(os.tmpdir(), 'ingestion-v5-'));
  const databasePath = path.join(value, 'jobs.sqlite');
  const payloadRoot = path.join(value, 'payloads');
  const artifact = await fsp.readFile(new URL('../migrations/005-unified-ingestion.sql', import.meta.url), 'utf8');
  const db = new DatabaseSync(databasePath);
  db.exec(artifact);
  db.prepare('INSERT INTO schema_migrations (version,name,sha256,applied_at) VALUES (5,?,?,?)').run('005-unified-ingestion', V5_SHA256, NOW);
  db.close();
  await fsp.chmod(databasePath, 0o600);
  return { root: value, databasePath, payloadRoot };
}

async function v6Fixture() {
  const value = await v5Fixture();
  const artifact = await fsp.readFile(new URL('../migrations/006-source-credit-audit.sql', import.meta.url), 'utf8');
  const db = new DatabaseSync(value.databasePath);
  db.exec(artifact);
  db.prepare('INSERT INTO schema_migrations (version,name,sha256,applied_at) VALUES (6,?,?,?)').run('006-source-credit-audit', V6_SHA256, NOW);
  db.close();
  return value;
}
async function v8Fixture() {
  const value = await v6Fixture();
  const syncBounds = await fsp.readFile(new URL('../migrations/007-sync-run-bounds.sql', import.meta.url), 'utf8');
  const resumeArtifacts = await fsp.readFile(new URL('../migrations/008-resume-artifacts.sql', import.meta.url), 'utf8');
  const db = new DatabaseSync(value.databasePath);
  db.exec(syncBounds);
  db.prepare('INSERT INTO schema_migrations (version,name,sha256,applied_at) VALUES (7,?,?,?)').run('007-sync-run-bounds', V7_SHA256, NOW);
  db.exec(resumeArtifacts);
  db.prepare('INSERT INTO schema_migrations (version,name,sha256,applied_at) VALUES (8,?,?,?)').run('008-resume-artifacts', V8_SHA256, NOW);
  db.close();
  return value;
}

async function initializedFixture() {
  const root = await fsp.mkdtemp(path.join(os.tmpdir(), 'ingestion-v6-'));
  const databasePath = path.join(root, 'jobs.sqlite');
  initializeIngestionDatabase(databasePath, { now: NOW });
  return { root, databasePath };
}

function insertSyncRun(db, id) {
  db.prepare('INSERT INTO sync_runs (id,source,profile,mode,state,started_at,window_end_at,artifact_dir,page_limit,max_pages,max_items) VALUES (?,?,?,?,?,?,?,?,?,?,?)')
    .run(id, `fixture-${id}`, 'profile', 'paid', 'fetching', NOW, NOW, `/private/fixture/sync-${id}`, 25, 100, null);
}

test('legacy migration preserves IDs bindings counts and private payload identity', async (t) => {
  const value = await fixture();
  t.after(() => fsp.rm(value.root, { recursive: true, force: true }));
  const first = await migrateIngestionDatabase(value.databasePath, { payloadRoot: value.payloadRoot, now: NOW });
  assert.equal(first.idempotent, false);
  assertIngestionSchema(value.databasePath);
  const db = new DatabaseSync(value.databasePath);
  assert.deepEqual(db.prepare('SELECT id, source_job_id FROM jobs').all().map((row) => ({ id: row.id, source_job_id: row.source_job_id })), [{ id: 7, source_job_id: 'job-7' }]);
  assert.deepEqual(db.prepare('SELECT id, source_db, source_rowid, dedupe_group_id FROM application_jobs').all().map((row) => ({ ...row })), [{ id: 9, source_db: 'ingestion', source_rowid: 7, dedupe_group_id: 1 }]);
  assert.deepEqual(db.prepare('SELECT source, profile, checkpoint, last_sync_run_id FROM source_checkpoints').all().map((row) => ({ ...row })), [{ source: 'fixture', profile: 'profile', checkpoint: '2026-08-02T00:00:00.000Z', last_sync_run_id: 3 }]);
  assert.deepEqual(db.prepare('SELECT jobs_seen, jobs_inserted, jobs_updated, reason_code FROM sync_runs').all().map((row) => ({ ...row })), [{ jobs_seen: 1, jobs_inserted: 1, jobs_updated: 0, reason_code: null }]);
  assert.equal(db.prepare('SELECT sync_run_id FROM source_observations').get().sync_run_id, null);
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

test('legacy failed runs cannot advance checkpoints or receive invented observation provenance', async (t) => {
  const value = await fixture();
  t.after(() => fsp.rm(value.root, { recursive: true, force: true }));
  const legacy = new DatabaseSync(value.databasePath);
  legacy.prepare(`INSERT INTO sync_runs (
    id, source, profile, mode, started_at, finished_at, checkpoint, success,
    jobs_seen, jobs_returned, jobs_inserted, jobs_updated, error
  ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`).run(
    4,
    'fixture',
    'profile',
    'paid_fetch',
    '2026-08-03T00:00:00.000Z',
    '2026-08-03T00:01:00.000Z',
    '2026-08-03T00:00:00.000Z',
    0,
    0,
    0,
    0,
    0,
    'failed',
  );
  legacy.close();
  await migrateIngestionDatabase(value.databasePath, { payloadRoot: value.payloadRoot, now: NOW });
  const db = new DatabaseSync(value.databasePath);
  assert.deepEqual(db.prepare('SELECT checkpoint,last_sync_run_id FROM source_checkpoints').all().map((row) => ({ ...row })), [{
    checkpoint: '2026-08-02T00:00:00.000Z',
    last_sync_run_id: 3,
  }]);
  assert.equal(db.prepare('SELECT sync_run_id FROM source_observations').get().sync_run_id, null);
  assert.deepEqual(db.prepare('SELECT id,state FROM sync_runs ORDER BY id').all().map((row) => ({ ...row })), [
    { id: 3, state: 'succeeded' },
    { id: 4, state: 'failed' },
  ]);
  db.close();
});

test('legacy same-group queues keep the terminal outcome and suppress duplicate queued rows', async (t) => {
  const value = await fixture();
  t.after(() => fsp.rm(value.root, { recursive: true, force: true }));
  const legacy = new DatabaseSync(value.databasePath);
  legacy.prepare('UPDATE jobs SET canonical_url=? WHERE id=7').run('https://boards.greenhouse.io/acme/jobs/first?gh_jid=123');
  legacy.prepare('UPDATE application_jobs SET application_url=? WHERE id=9').run('https://boards.greenhouse.io/acme/jobs/first?gh_jid=123');
  legacy.prepare(`INSERT INTO jobs (
    id,source,source_job_id,canonical_url,title,company,location,remote,posted_at,
    discovered_at,description,status,raw_json,first_seen_at,last_seen_at
  ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)`).run(
    8,
    'fixture',
    'job-8',
    'https://boards.greenhouse.io/acme/jobs/second?gh_jid=123',
    'Role',
    'Company',
    'Remote',
    1,
    '2026-08-01',
    '2026-08-02T00:00:00.000Z',
    'description',
    'queued',
    JSON.stringify({ employment_types: ['full_time'], closed_at: null }),
    '2026-08-02T00:00:00.000Z',
    '2026-08-02T00:00:00.000Z',
  );
  legacy.prepare(`INSERT INTO application_jobs (
    id,source_table,source_db,source_rowid,source_job_id,application_url,
    eligibility_tier,verification_reason,status,completed_at
  ) VALUES (?,?,?,?,?,?,?,?,?,?)`).run(
    10,
    'jobs',
    value.databasePath,
    8,
    'job-8',
    'https://boards.greenhouse.io/acme/jobs/second?gh_jid=123',
    'active_verified',
    'fixture',
    'completed',
    NOW,
  );
  legacy.close();
  await migrateIngestionDatabase(value.databasePath, { payloadRoot: value.payloadRoot, now: NOW });
  const db = new DatabaseSync(value.databasePath);
  assert.equal(db.prepare('SELECT count(*) AS n FROM jobs').get().n, 2);
  assert.equal(db.prepare('SELECT count(*) AS n FROM dedupe_groups').get().n, 1);
  assert.deepEqual(db.prepare('SELECT id,status,status_reason,dedupe_group_id FROM application_jobs ORDER BY id').all().map((row) => ({ ...row })), [
    { id: 9, status: 'skipped', status_reason: 'deduplicated', dedupe_group_id: null },
    { id: 10, status: 'completed', status_reason: null, dedupe_group_id: 1 },
  ]);
  db.close();
});

test('malformed legacy payload rolls back database and staged files', async (t) => {
  const value = await fixture({ malformedRaw: true });
  t.after(() => fsp.rm(value.root, { recursive: true, force: true }));
  await assert.rejects(migrateIngestionDatabase(value.databasePath, { payloadRoot: value.payloadRoot, now: NOW }));
  const db = new DatabaseSync(value.databasePath);
  assert.deepEqual(db.prepare("SELECT name FROM pragma_table_info('jobs') WHERE name = 'raw_json'").all().map((row) => ({ ...row })), [{ name: 'raw_json' }]);
  assert.equal(db.prepare("SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'source_observations'").get(), undefined);
  db.close();
  const payloadEntries = await fsp.readdir(value.payloadRoot);
  assert.deepEqual(payloadEntries, []);
});

test('legacy extra objects roll back the migration before payload cleanup can orphan jobs', async (t) => {
  const value = await fixture();
  t.after(() => fsp.rm(value.root, { recursive: true, force: true }));
  const altered = new DatabaseSync(value.databasePath);
  altered.exec("CREATE TABLE sentinel (value TEXT NOT NULL); INSERT INTO sentinel VALUES ('kept');");
  altered.close();
  await assert.rejects(
    migrateIngestionDatabase(value.databasePath, { payloadRoot: value.payloadRoot, now: NOW }),
    /E_SCHEMA_OBJECTS/u,
  );
  const db = new DatabaseSync(value.databasePath);
  assert.deepEqual(db.prepare("SELECT name FROM pragma_table_info('jobs') WHERE name = 'raw_json'").all().map((row) => ({ ...row })), [{ name: 'raw_json' }]);
  assert.deepEqual(db.prepare('SELECT value FROM sentinel').all().map((row) => ({ ...row })), [{ value: 'kept' }]);
  db.close();
  assert.equal((await fsp.readdir(value.payloadRoot, { recursive: true, withFileTypes: true })).some((entry) => entry.isFile()), false);
});

test('wrong schema refuses without replacing unrelated database tables', async (t) => {
  const root = await fsp.mkdtemp(path.join(os.tmpdir(), 'ingestion-schema-'));
  t.after(() => fsp.rm(root, { recursive: true, force: true }));
  const databasePath = path.join(root, 'wrong.sqlite');
  const payloadRoot = path.join(root, 'payloads');
  const db = new DatabaseSync(databasePath);
  db.exec('CREATE TABLE jobs (id INTEGER PRIMARY KEY); CREATE TABLE sentinel (value TEXT); INSERT INTO sentinel VALUES (\'kept\');');
  db.close();
  await fsp.chmod(databasePath, 0o600);
  await assert.rejects(migrateIngestionDatabase(databasePath, { payloadRoot, now: NOW }));
  const check = new DatabaseSync(databasePath);
  assert.deepEqual(check.prepare('SELECT value FROM sentinel').all().map((row) => ({ ...row })), [{ value: 'kept' }]);
  check.close();
});

test('initialization rejects extra objects without committing the canonical schema', async (t) => {
  const root = await fsp.mkdtemp(path.join(os.tmpdir(), 'ingestion-initialize-'));
  t.after(() => fsp.rm(root, { recursive: true, force: true }));
  const databasePath = path.join(root, 'jobs.sqlite');
  const db = new DatabaseSync(databasePath);
  db.exec("CREATE TABLE sentinel (value TEXT NOT NULL); INSERT INTO sentinel VALUES ('kept');");
  db.close();
  await fsp.chmod(databasePath, 0o600);
  assert.throws(() => initializeIngestionDatabase(databasePath, { now: NOW }), /E_SCHEMA_OBJECTS/u);
  const check = new DatabaseSync(databasePath);
  assert.deepEqual(check.prepare("SELECT type,name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name").all().map((row) => ({ ...row })), [{
    type: 'table',
    name: 'sentinel',
  }]);
  assert.deepEqual(check.prepare('SELECT value FROM sentinel').all().map((row) => ({ ...row })), [{ value: 'kept' }]);
  check.close();
});

test('schema attestation rejects altered canonical objects', async (t) => {
  const root = await fsp.mkdtemp(path.join(os.tmpdir(), 'ingestion-attestation-'));
  t.after(() => fsp.rm(root, { recursive: true, force: true }));
  const databasePath = path.join(root, 'jobs.sqlite');
  initializeIngestionDatabase(databasePath, { now: NOW });
  const db = new DatabaseSync(databasePath);
  db.exec('DROP TRIGGER source_observations_immutable_update');
  db.close();
  assert.throws(() => assertIngestionSchema(databasePath), /E_SCHEMA_OBJECTS/);
});

test('fresh initialization records only the v9 migration identity', async (t) => {
  const value = await initializedFixture();
  t.after(() => fsp.rm(value.root, { recursive: true, force: true }));
  const db = new DatabaseSync(value.databasePath);
  assert.deepEqual(db.prepare('SELECT version, name FROM schema_migrations ORDER BY version').all().map((row) => ({ ...row })), [
    { version: 9, name: '009-platform-application-bindings' },
  ]);
  db.close();
});

test('v5 upgrade adds credit audits and recovery bounds without rebuilding canonical data', async (t) => {
  const value = await v5Fixture();
  t.after(() => fsp.rm(value.root, { recursive: true, force: true }));
  const result = await migrateIngestionDatabase(value.databasePath, { payloadRoot: value.payloadRoot, now: NOW });
  assert.equal(result.idempotent, false);
  assert.equal(result.upgradedFrom, 5);
  assertIngestionSchema(value.databasePath);
  const db = new DatabaseSync(value.databasePath);
  assert.deepEqual(db.prepare('SELECT version, name, sha256 FROM schema_migrations ORDER BY version').all().map((row) => ({ ...row })), [
    { version: 5, name: '005-unified-ingestion', sha256: V5_SHA256 },
    { version: 6, name: '006-source-credit-audit', sha256: V6_SHA256 },
    { version: 7, name: '007-sync-run-bounds', sha256: db.prepare('SELECT sha256 FROM schema_migrations WHERE version = 7').get().sha256 },
    { version: 8, name: '008-resume-artifacts', sha256: db.prepare('SELECT sha256 FROM schema_migrations WHERE version = 8').get().sha256 },
    { version: 9, name: '009-platform-application-bindings', sha256: db.prepare('SELECT sha256 FROM schema_migrations WHERE version = 9').get().sha256 },
  ]);
  assert.notEqual(db.prepare("SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'source_credit_audits'").get(), undefined);
  assert.deepEqual(db.prepare("SELECT name FROM sqlite_master WHERE type = 'trigger' AND name LIKE 'source_credit_audits_%' ORDER BY name").all().map((row) => row.name), [
    'source_credit_audits_guarded_update',
    'source_credit_audits_immutable_delete',
  ]);
  assert.deepEqual(db.prepare("SELECT name FROM pragma_table_info('sync_runs') WHERE name IN ('page_limit','max_pages','max_items') ORDER BY cid").all().map((row) => row.name), ['page_limit', 'max_pages', 'max_items']);
  db.close();
});

test('v5 upgrade rejects tampered schema and leaves migration evidence untouched', async (t) => {
  const value = await v5Fixture();
  t.after(() => fsp.rm(value.root, { recursive: true, force: true }));
  const tampered = new DatabaseSync(value.databasePath);
  tampered.exec('ALTER TABLE jobs RENAME COLUMN company TO company_tampered');
  tampered.close();
  await assert.rejects(migrateIngestionDatabase(value.databasePath, { payloadRoot: value.payloadRoot, now: NOW }), /E_SCHEMA_/u);
  const db = new DatabaseSync(value.databasePath);

  assert.deepEqual(db.prepare('SELECT version, name, sha256 FROM schema_migrations ORDER BY version').all().map((row) => ({ ...row })), [
    { version: 5, name: '005-unified-ingestion', sha256: V5_SHA256 },
  ]);
  assert.equal(db.prepare("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'source_credit_audits'").get(), undefined);
  db.close();
});
test('v6 upgrade rejects active nonterminal sync runs whose bounds cannot be recovered', async (t) => {
  const value = await v6Fixture();
  t.after(() => fsp.rm(value.root, { recursive: true, force: true }));
  const db = new DatabaseSync(value.databasePath);
  db.prepare(`INSERT INTO sync_runs (source,profile,mode,state,started_at,window_end_at,artifact_dir) VALUES (?,?,?,?,?,?,?)`).run(
    'fixture', 'profile', 'paid', 'fetching', NOW, NOW, '/private/fixture/active',
  );
  db.close();
  await assert.rejects(
    async () => initializeIngestionDatabase(value.databasePath, { now: NOW }),
    /E_SCHEMA_ACTIVE_RUN/u,
  );
});

test('v5 upgrade and repeated initialization are idempotent', async (t) => {
  const value = await v5Fixture();
  t.after(() => fsp.rm(value.root, { recursive: true, force: true }));
  const first = await migrateIngestionDatabase(value.databasePath, { payloadRoot: value.payloadRoot, now: NOW });
  assert.equal(first.idempotent, false);
  const second = initializeIngestionDatabase(value.databasePath, { now: NOW });
  assert.equal(second.idempotent, true);
  const third = await migrateIngestionDatabase(value.databasePath, { payloadRoot: value.payloadRoot, now: NOW });
  assert.equal(third.idempotent, true);
  const db = new DatabaseSync(value.databasePath);
  assert.deepEqual(db.prepare('SELECT version FROM schema_migrations ORDER BY version').all().map((row) => row.version), [5, 6, 7, 8, 9]);
  assert.equal(db.prepare('SELECT count(*) AS count FROM source_credit_audits').get().count, 0);
  db.close();
});

test('v6 upgrade preserves history and adds deterministic recovery bounds', async (t) => {
  const value = await v6Fixture();
  t.after(() => fsp.rm(value.root, { recursive: true, force: true }));
  const result = initializeIngestionDatabase(value.databasePath, { now: NOW });
  assert.equal(result.idempotent, false);
  assert.equal(result.upgradedFrom, 6);
  assertIngestionSchema(value.databasePath);
  const db = new DatabaseSync(value.databasePath);
  assert.deepEqual(db.prepare('SELECT version, name FROM schema_migrations ORDER BY version').all().map((row) => ({ ...row })), [
    { version: 5, name: '005-unified-ingestion' },
    { version: 6, name: '006-source-credit-audit' },
    { version: 7, name: '007-sync-run-bounds' },
    { version: 8, name: '008-resume-artifacts' },
    { version: 9, name: '009-platform-application-bindings' },
  ]);
  db.close();
});
test('v8 upgrade quarantines unresolved application rows for deterministic re-ingestion', async (t) => {
  const value = await v8Fixture();
  t.after(() => fsp.rm(value.root, { recursive: true, force: true }));
  const db = new DatabaseSync(value.databasePath);
  const insert = db.prepare(`INSERT INTO application_jobs (
    source_table,source_db,source_rowid,source_job_id,application_url,
    eligibility_tier,status
  ) VALUES (?,?,?,?,?,?,?)`);
  insert.run(
    'ingestion', 'ingestion', 42, 'legacy-42a', 'https://job-boards.greenhouse.io/example/jobs/42a',
    'active_verified', 'queued',
  );
  insert.run(
    'archive', 'ingestion', 42, 'legacy-42b', 'https://job-boards.greenhouse.io/example/jobs/42b',
    'active_verified', 'queued',
  );
  db.close();
  const result = initializeIngestionDatabase(value.databasePath, { now: NOW });
  assert.equal(result.upgradedFrom, 8);
  assertIngestionSchema(value.databasePath);
  const upgraded = new DatabaseSync(value.databasePath);
  assert.deepEqual(
    upgraded.prepare('SELECT source_table,status,status_reason,platform,application_host FROM application_jobs ORDER BY id').all().map((row) => ({ ...row })),
    ['ingestion', 'archive'].map((source_table) => ({
      source_table,
      status: 'skipped',
      status_reason: 'platform_reingest_required',
      platform: null,
      application_host: null,
    })),
  );
  upgraded.close();
});

test('v8 upgrade refuses an active application run without mutating migration evidence', async (t) => {
  const value = await v8Fixture();
  t.after(() => fsp.rm(value.root, { recursive: true, force: true }));
  const db = new DatabaseSync(value.databasePath);
  db.exec('PRAGMA foreign_keys = OFF;');
  const job = db.prepare(`INSERT INTO application_jobs (
    source_table,source_db,source_rowid,source_job_id,application_url,
    eligibility_tier,status
  ) VALUES (?,?,?,?,?,?,?) RETURNING id`).get(
    'jobs', 'ingestion', 43, 'legacy-43', 'https://job-boards.greenhouse.io/example/jobs/43',
    'active_verified', 'claimed',
  );
  const digest = 'a'.repeat(64);
  const artifact = db.prepare(`INSERT INTO resume_artifacts (
    application_job_id,normalized_job_id,job_description_sha256,
    generator_fingerprint_sha256,generator_schema_version,manifest_path,
    manifest_sha256,pdf_path,pdf_sha256,job_description_path,pages,created_at
  ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?) RETURNING id`).get(
    job.id, 1, digest, digest, 'fixture-v1', '/private/manifest.json',
    digest, '/private/resume.pdf', digest, '/private/job.txt', 1, NOW,
  );
  db.prepare(`INSERT INTO application_runs (
    job_id,status,reason_code,started_at,evidence_path,active,owner_id,
    browser_session_id,claimed_at,lease_expires_at,last_progress_at,
    workspace_path,resume_artifact_path,resume_artifact_sha256,
    answer_memory_path,resume_artifact_id
  ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)`).run(
    job.id, 'applying', 'claimed', NOW, '/private/evidence', 1, 'owner',
    'browser', NOW, NOW, NOW, '/private/workspace', '/private/resume.pdf',
    digest, '/private/memory.json', artifact.id,
  );
  db.close();
  assert.throws(() => initializeIngestionDatabase(value.databasePath, { now: NOW }), /E_SCHEMA_ACTIVE_RUN/u);
  const unchanged = new DatabaseSync(value.databasePath);
  assert.equal(unchanged.prepare('SELECT max(version) AS version FROM schema_migrations').get().version, 8);
  assert.equal(unchanged.prepare('SELECT active FROM application_runs').get().active, 1);
  unchanged.close();
});

test('source credit audit enforces canonical states and one-way baseline-preserving transitions', async (t) => {
  const value = await initializedFixture();
  t.after(() => fsp.rm(value.root, { recursive: true, force: true }));
  const db = new DatabaseSync(value.databasePath);
  insertSyncRun(db, 1);
  db.prepare(`INSERT INTO source_credit_audits (
    sync_run_id, source, period_start, period_end, observed_before_at,
    credits_before, observed_after_at, credits_after, reported_credits, state, reason_code
  ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`).run(
    1, 'fixture-1', NOW, NOW, NOW, 5, null, null, null, 'pending', null,
  );
  assert.throws(() => db.prepare('UPDATE source_credit_audits SET reported_credits = 1 WHERE sync_run_id = 1').run(), /invalid source credit audit/u);
  db.prepare(`UPDATE source_credit_audits
    SET observed_after_at = ?, credits_after = ?, reported_credits = ?, state = 'reconciled'
    WHERE sync_run_id = ?`).run('2026-08-04T00:00:01.000Z', 8, 3, 1);
  assert.deepEqual(db.prepare('SELECT state, credits_before, credits_after, reported_credits FROM source_credit_audits').all().map((row) => ({ ...row })), [
    { state: 'reconciled', credits_before: 5, credits_after: 8, reported_credits: 3 },
  ]);
  assert.throws(() => db.prepare('UPDATE source_credit_audits SET credits_before = 6 WHERE sync_run_id = 1').run(), /invalid source credit audit/u);
  assert.throws(() => db.prepare("UPDATE source_credit_audits SET state = 'unavailable', observed_after_at = NULL, credits_after = NULL, reported_credits = NULL, reason_code = 'late' WHERE sync_run_id = 1").run(), /invalid source credit audit/u);
  assert.throws(() => db.prepare('DELETE FROM source_credit_audits WHERE sync_run_id = 1').run(), /immutable source credit audit/u);
  insertSyncRun(db, 2);
  assert.throws(() => db.prepare(`INSERT INTO source_credit_audits (
    sync_run_id, source, period_start, period_end, observed_before_at,
    credits_before, observed_after_at, credits_after, reported_credits, state, reason_code
  ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`).run(
    2, 'fixture-2', NOW, NOW, NOW, 5, null, null, 1, 'pending', null,
  ), /constraint failed/u);
  db.prepare(`INSERT INTO source_credit_audits (
    sync_run_id, source, period_start, period_end, observed_before_at,
    credits_before, observed_after_at, credits_after, reported_credits, state, reason_code
  ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`).run(
    2, 'fixture-2', NOW, NOW, NOW, 5, null, null, null, 'pending', null,
  );
  db.prepare(`UPDATE source_credit_audits
    SET state = 'unavailable', reason_code = 'usage_unavailable'
    WHERE sync_run_id = 2`).run();
  assert.equal(db.prepare('SELECT state, reason_code FROM source_credit_audits WHERE sync_run_id = 2').get().state, 'unavailable');
  db.close();
});

test('source credit audit triggers are part of runtime schema attestation', async (t) => {
  const value = await initializedFixture();
  t.after(() => fsp.rm(value.root, { recursive: true, force: true }));
  const db = new DatabaseSync(value.databasePath);
  db.exec('DROP TRIGGER source_credit_audits_guarded_update');
  db.close();
  assert.throws(() => assertIngestionSchema(value.databasePath), /E_SCHEMA_OBJECTS/u);
});
