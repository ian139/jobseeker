import { createHash } from 'node:crypto';
import { spawn } from 'node:child_process';

import {
  canonicalizeApplicationUrl,
  classifyApplicationUrl,
  extractPlatformJobSnapshot,
} from './platforms.mjs';

const SQLITE_BINARY = 'sqlite3';
const SQLITE_MAX_BUFFER = 4 * 1024 * 1024;
const SQLITE_MAX_INPUT = 32 * 1024 * 1024;
const SQLITE_TIMEOUT_MS = 15_000;
const MAX_SOURCE_ROWS = 2048;
const MAX_QUEUED_ROWS = 100_000;
const BOUND_BATCH_SIZE = 16;
const URL_BATCH_SIZE = 256;
const QUARANTINE_MAX_ROWS = 10_000;
const URL_MAX = 8192;
const SOURCE_DB_MAX = 4096;
const SOURCE_JOB_ID_MAX = 512;
const SOURCE_TEXT_MAX = 16 * 1024;
const SOURCE_REASON_MAX = 16 * 1024;
const METADATA_TEXT_MAX = 512;
const SHA256_HEX = /^[0-9a-f]{64}$/u;
const SUPPORTED_PLATFORMS = new Set(['greenhouse', 'ashby']);
const SOURCE_TABLES = new Set(['jobs', 'legacy_jobs', 'assistant_jobs']);
const ELIGIBILITY_TIERS = new Set(['active_verified', 'backfill_only', 'unverified_stale']);
const SOURCE_KEYS = new Set([
  'sourceTable',
  'sourceDb',
  'sourceRowid',
  'sourceJobId',
  'applicationUrl',
  'eligibilityTier',
  'verificationReason',
  'sourcePostedAt',
  'sourceLastSeenAt',
  'payload',
]);
const REQUIRED_SOURCE_KEYS = Object.freeze([
  'sourceTable',
  'sourceDb',
  'sourceRowid',
  'sourceJobId',
  'applicationUrl',
  'eligibilityTier',
  'payload',
]);
const CORE_COLUMNS = Object.freeze([
  'id',
  'source_table',
  'source_db',
  'source_rowid',
  'source_job_id',
  'application_url',
  'eligibility_tier',
  'verification_reason',
  'source_posted_at',
  'source_last_seen_at',
  'status',
  'status_reason',
  'claimed_at',
  'completed_at',
]);
const BINDING_COLUMNS = Object.freeze([
  'platform',
  'job_title',
  'job_company',
  'job_location',
  'job_description',
  'job_description_sha256',
]);
const INGEST_COLUMNS = Object.freeze([
  'source_table',
  'source_db',
  'source_rowid',
  'source_job_id',
  'application_url',
  'eligibility_tier',
  'verification_reason',
  'source_posted_at',
  'source_last_seen_at',
  'platform',
  'job_title',
  'job_company',
  'job_location',
  'job_description',
  'job_description_sha256',
]);

export class JobSourceError extends TypeError {
  constructor(code, message = code) {
    super(message);
    this.name = 'JobSourceError';
    this.code = code;
  }
}

function fail(code) {
  throw new JobSourceError(code);
}

function isRecord(value) {
  if (value === null || typeof value !== 'object' || Array.isArray(value)) return false;
  const prototype = Object.getPrototypeOf(value);
  return prototype === Object.prototype || prototype === null;
}

function assertRecord(value, code) {
  if (!isRecord(value)) fail(code);
  return value;
}

function assertDataDescriptor(value, key, code) {
  const descriptor = Object.getOwnPropertyDescriptor(value, key);
  if (descriptor && !Object.hasOwn(descriptor, 'value')) fail(code);
  return descriptor;
}

function ownValue(value, key, code) {
  const descriptor = assertDataDescriptor(value, key, code);
  return descriptor ? descriptor.value : undefined;
}

function hasOwn(value, key) {
  return Object.prototype.hasOwnProperty.call(value, key);
}

function assertExactKeys(value, allowed, code) {
  if (Reflect.ownKeys(value).some((key) => typeof key === 'symbol')) fail(code);
  for (const key of Object.keys(value)) {
    if (!allowed.has(key)) fail(code);
    assertDataDescriptor(value, key, code);
  }
}

function requireOwn(value, key, code) {
  if (!hasOwn(value, key)) fail(code);
  return ownValue(value, key, code);
}

function safeString(value, code, max, { trim = false, allowEmpty = false } = {}) {
  if (typeof value !== 'string' || value.length > max || (!allowEmpty && value.length === 0)) fail(code);
  if (/[\u0000-\u001f\u007f]/u.test(value)) fail(code);
  if (trim && value.trim() !== value) fail(code);
  return value;
}

function normalizedText(value, code, max = METADATA_TEXT_MAX) {
  const text = safeString(value, code, max, { trim: true });
  if (text.replace(/\s+/gu, ' ').trim() !== text) fail(code);
  return text;
}

function nullableText(value, code, max) {
  if (value === undefined || value === null) return null;
  return safeString(value, code, max, { trim: true });
}

function normalizeTimestamp(value, code) {
  if (value === undefined || value === null) return null;
  const text = safeString(value, code, 128, { trim: true });
  const millis = Date.parse(text);
  if (!Number.isFinite(millis)) fail(code);
  return new Date(millis).toISOString();
}

function requirePositiveInteger(value, code) {
  if (!Number.isSafeInteger(value) || value < 1) fail(code);
  return value;
}

function sha256(value) {
  return createHash('sha256').update(value, 'utf8').digest('hex');
}

function deepFreeze(value, seen = new Set()) {
  if (value === null || typeof value !== 'object' || seen.has(value)) return value;
  seen.add(value);
  for (const child of Object.values(value)) deepFreeze(child, seen);
  return Object.freeze(value);
}

function immutable(value) {
  return deepFreeze(value);
}

function normalizeSourceJobInternal(input) {
  const code = 'INVALID_SOURCE_JOB';
  const envelope = assertRecord(input, code);
  assertExactKeys(envelope, SOURCE_KEYS, code);
  for (const key of REQUIRED_SOURCE_KEYS) requireOwn(envelope, key, code);

  const rawApplicationUrl = ownValue(envelope, 'applicationUrl', code);
  safeString(rawApplicationUrl, code, URL_MAX, { trim: true });
  const platform = classifyApplicationUrl(rawApplicationUrl);
  if (platform === null) return null;

  const sourceTable = ownValue(envelope, 'sourceTable', code);
  if (typeof sourceTable !== 'string' || !SOURCE_TABLES.has(sourceTable)) fail(code);
  const sourceDb = safeString(ownValue(envelope, 'sourceDb', code), code, SOURCE_DB_MAX, { trim: true });
  const sourceRowid = requirePositiveInteger(ownValue(envelope, 'sourceRowid', code), code);
  const sourceJobId = safeString(
    ownValue(envelope, 'sourceJobId', code),
    code,
    SOURCE_JOB_ID_MAX,
    { trim: true },
  );
  const eligibilityTier = ownValue(envelope, 'eligibilityTier', code);
  if (typeof eligibilityTier !== 'string' || !ELIGIBILITY_TIERS.has(eligibilityTier)) fail(code);
  const verificationReason = nullableText(
    ownValue(envelope, 'verificationReason', code),
    code,
    SOURCE_REASON_MAX,
  );
  const sourcePostedAt = normalizeTimestamp(
    ownValue(envelope, 'sourcePostedAt', code),
    code,
  );
  const sourceLastSeenAt = normalizeTimestamp(
    ownValue(envelope, 'sourceLastSeenAt', code),
    code,
  );

  let applicationUrl;
  try {
    applicationUrl = canonicalizeApplicationUrl(rawApplicationUrl);
  } catch {
    fail(code);
  }
  if (typeof applicationUrl !== 'string' || classifyApplicationUrl(applicationUrl) !== platform) fail(code);

  const payload = ownValue(envelope, 'payload', code);
  let snapshot;
  try {
    snapshot = extractPlatformJobSnapshot({ applicationUrl, payload });
  } catch {
    fail('INVALID_SOURCE_JOB_PAYLOAD');
  }
  if (!isRecord(snapshot)
      || snapshot.platform !== platform
      || snapshot.applicationUrl !== applicationUrl) {
    fail('INVALID_SOURCE_JOB_PAYLOAD');
  }

  const title = normalizedText(snapshot.title, 'INVALID_SOURCE_JOB_PAYLOAD');
  const company = normalizedText(snapshot.company, 'INVALID_SOURCE_JOB_PAYLOAD');
  const location = normalizedText(snapshot.location, 'INVALID_SOURCE_JOB_PAYLOAD');
  const description = safeString(snapshot.description, 'INVALID_SOURCE_JOB_PAYLOAD', SOURCE_TEXT_MAX, {
    trim: true,
  });
  if (description.replace(/\s+/gu, ' ').trim() !== description) fail('INVALID_SOURCE_JOB_PAYLOAD');
  const descriptionSha256 = sha256(description);

  return {
    sourceTable,
    sourceDb,
    sourceRowid,
    sourceJobId,
    applicationUrl,
    eligibilityTier,
    verificationReason,
    sourcePostedAt,
    sourceLastSeenAt,
    platform,
    jobTitle: title,
    jobCompany: company,
    jobLocation: location,
    jobDescription: description,
    jobDescriptionSha256: descriptionSha256,
  };
}

export function normalizeSourceJob(input) {
  try {
    const normalized = normalizeSourceJobInternal(input);
    return normalized === null ? null : immutable(normalized);
  } catch (error) {
    if (error instanceof JobSourceError) throw error;
    fail('INVALID_SOURCE_JOB');
  }
}

function sourceIdentityKey(job) {
  return `${job.sourceTable.length}:${job.sourceTable}|${job.sourceDb.length}:${job.sourceDb}|${job.sourceRowid}`;
}

function dedupeNormalizedJobs(jobs) {
  const byIdentity = new Set();
  const byUrl = new Set();
  const deduped = [];
  for (const job of jobs) {
    const identity = sourceIdentityKey(job);
    if (byIdentity.has(identity) || byUrl.has(job.applicationUrl)) continue;
    byIdentity.add(identity);
    byUrl.add(job.applicationUrl);
    deduped.push(job);
  }
  return deduped;
}

function sqlLiteral(value) {
  if (value === null || value === undefined) return 'NULL';
  if (typeof value === 'number') {
    if (!Number.isSafeInteger(value)) fail('SQL_VALUE_INVALID');
    return String(value);
  }
  if (typeof value !== 'string') fail('SQL_VALUE_INVALID');
  return `'${value.replaceAll("'", "''")}'`;
}
function databasePath(value) {
  if (typeof value !== 'string'
      || value.length === 0
      || value.length > 4096
      || value.startsWith('-')) fail('INVALID_DATABASE');
  if (/[\u0000-\u001f\u007f]/u.test(value)) fail('INVALID_DATABASE');
  return value;
}

async function runSqlite(database, sql) {
  if (Buffer.byteLength(sql, 'utf8') > SQLITE_MAX_INPUT) fail('SQLITE_INPUT_LIMIT');
  return new Promise((resolve, reject) => {
    const child = spawn(
      SQLITE_BINARY,
      ['-batch', '-bail', '-json', '-cmd', '.timeout 5000', database],
      { stdio: ['pipe', 'pipe', 'pipe'], windowsHide: true },
    );
    let stdout = '';
    let outputBytes = 0;
    let settled = false;
    const timer = setTimeout(() => {
      if (settled) return;
      settled = true;
      child.kill('SIGKILL');
      reject(new JobSourceError('SQLITE_TIMEOUT'));
    }, SQLITE_TIMEOUT_MS);
    child.stdout.setEncoding('utf8');
    child.stdout.on('data', (chunk) => {
      if (settled) return;
      outputBytes += Buffer.byteLength(chunk, 'utf8');
      if (outputBytes > SQLITE_MAX_BUFFER) {
        settled = true;
        clearTimeout(timer);
        child.kill('SIGKILL');
        reject(new JobSourceError('SQLITE_OUTPUT_LIMIT'));
        return;
      }
      stdout += chunk;
    });
    child.stderr.resume();
    child.once('error', () => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      reject(new JobSourceError('SQLITE_COMMAND'));
    });
    child.once('close', (exitCode) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      if (exitCode !== 0) reject(new JobSourceError('SQLITE_COMMAND'));
      else resolve(stdout.trim());
    });
    child.stdin.on('error', () => {});
    child.stdin.end(sql);
  });
}

function parseRows(stdout, code = 'SQLITE_RESULT') {
  if (stdout === '') return [];
  let parsed;
  try {
    parsed = JSON.parse(stdout);
  } catch {
    fail(code);
  }
  if (!Array.isArray(parsed)) fail(code);
  return parsed;
}

async function ensureSchema(database, columns) {
  const rows = parseRows(await runSqlite(database, 'PRAGMA table_info(application_jobs);'), 'SCHEMA_INVALID');
  const available = new Set();
  for (const row of rows) {
    if (!isRecord(row) || typeof row.name !== 'string') fail('SCHEMA_INVALID');
    available.add(row.name);
  }
  for (const column of columns) {
    if (!available.has(column)) fail('SCHEMA_INVALID');
  }
}

function ingestSql(jobs) {
  const statements = [
    'PRAGMA foreign_keys = ON;',
    'BEGIN IMMEDIATE;',
    'CREATE TEMP TABLE _job_source_result (seq INTEGER PRIMARY KEY, id INTEGER NOT NULL, action TEXT NOT NULL);',
  ];
  for (let index = 0; index < jobs.length; index += 1) {
    const job = jobs[index];
    const seq = index + 1;
    const identity = `
      SELECT id FROM application_jobs
      WHERE source_table = ${sqlLiteral(job.sourceTable)}
        AND source_db = ${sqlLiteral(job.sourceDb)}
        AND source_rowid = ${sqlLiteral(job.sourceRowid)}
      ORDER BY id ASC LIMIT 1`;
    const byUrl = `
      SELECT id FROM application_jobs
      WHERE application_url = ${sqlLiteral(job.applicationUrl)}
      ORDER BY id ASC LIMIT 1`;
    const target = `COALESCE((${identity}), (${byUrl}))`;
    const values = [
      job.sourceTable,
      job.sourceDb,
      job.sourceRowid,
      job.sourceJobId,
      job.applicationUrl,
      job.eligibilityTier,
      job.verificationReason,
      job.sourcePostedAt,
      job.sourceLastSeenAt,
      job.platform,
      job.jobTitle,
      job.jobCompany,
      job.jobLocation,
      job.jobDescription,
      job.jobDescriptionSha256,
    ];
    const columns = INGEST_COLUMNS.join(', ');
    const literals = values.map(sqlLiteral).join(', ');
    statements.push(`
      UPDATE application_jobs AS target
      SET source_table = ${sqlLiteral(job.sourceTable)},
          source_db = ${sqlLiteral(job.sourceDb)},
          source_rowid = ${sqlLiteral(job.sourceRowid)},
          source_job_id = ${sqlLiteral(job.sourceJobId)},
          application_url = ${sqlLiteral(job.applicationUrl)},
          eligibility_tier = ${sqlLiteral(job.eligibilityTier)},
          verification_reason = ${sqlLiteral(job.verificationReason)},
          source_posted_at = ${sqlLiteral(job.sourcePostedAt)},
          source_last_seen_at = ${sqlLiteral(job.sourceLastSeenAt)},
          platform = ${sqlLiteral(job.platform)},
          job_title = ${sqlLiteral(job.jobTitle)},
          job_company = ${sqlLiteral(job.jobCompany)},
          job_location = ${sqlLiteral(job.jobLocation)},
          job_description = ${sqlLiteral(job.jobDescription)},
          job_description_sha256 = ${sqlLiteral(job.jobDescriptionSha256)},
          status = CASE
            WHEN target.status = 'skipped'
              AND target.status_reason IN ('platform_reingest_required', 'unsupported_platform')
              THEN 'queued'
            ELSE target.status
          END,
          status_reason = CASE
            WHEN target.status = 'skipped'
              AND target.status_reason IN ('platform_reingest_required', 'unsupported_platform')
              THEN NULL
            ELSE target.status_reason
          END,
          claimed_at = CASE
            WHEN target.status = 'skipped'
              AND target.status_reason IN ('platform_reingest_required', 'unsupported_platform')
              THEN NULL
            ELSE target.claimed_at
          END,
          completed_at = CASE
            WHEN target.status = 'skipped'
              AND target.status_reason IN ('platform_reingest_required', 'unsupported_platform')
              THEN NULL
            ELSE target.completed_at
          END
      WHERE target.id = ${target}
        AND (
          target.status = 'queued'
          OR (
            target.status = 'skipped'
            AND target.status_reason IN ('platform_reingest_required', 'unsupported_platform')
          )
        )
        AND (
          target.application_url = ${sqlLiteral(job.applicationUrl)}
          OR NOT EXISTS (
            SELECT 1 FROM application_jobs AS conflict
            WHERE conflict.application_url = ${sqlLiteral(job.applicationUrl)}
              AND conflict.id <> target.id
          )
        );
      INSERT INTO _job_source_result (seq, id, action)
      SELECT ${seq}, target.id, 'updated'
      FROM application_jobs AS target
      WHERE target.id = ${target}
        AND changes() = 1;
      INSERT INTO application_jobs (${columns}, status, status_reason, claimed_at, completed_at)
      SELECT ${literals}, 'queued', NULL, NULL, NULL
      WHERE NOT EXISTS (
        SELECT 1 FROM application_jobs
        WHERE (
          source_table = ${sqlLiteral(job.sourceTable)}
          AND source_db = ${sqlLiteral(job.sourceDb)}
          AND source_rowid = ${sqlLiteral(job.sourceRowid)}
        )
        OR application_url = ${sqlLiteral(job.applicationUrl)}
      );
      INSERT INTO _job_source_result (seq, id, action)
      SELECT ${seq}, last_insert_rowid(), 'inserted'
      WHERE changes() = 1;
      INSERT INTO _job_source_result (seq, id, action)
      SELECT ${seq}, target.id, 'unchanged'
      FROM application_jobs AS target
      WHERE target.id = ${target}
        AND NOT EXISTS (SELECT 1 FROM _job_source_result WHERE _job_source_result.seq = ${seq});
    `);
  }
  statements.push('SELECT seq, id, action FROM _job_source_result ORDER BY seq ASC;', 'COMMIT;');
  return statements.join('\n');
}

function normalizeResultRows(rows, expectedCount) {
  const bySeq = new Map();
  for (const row of rows) {
    if (!isRecord(row)
        || !Number.isSafeInteger(row.seq)
        || !Number.isSafeInteger(row.id)
        || row.seq < 1
        || row.seq > expectedCount
        || row.id < 1
        || typeof row.action !== 'string'
        || !['inserted', 'updated', 'unchanged'].includes(row.action)
        || bySeq.has(row.seq)) {
      fail('SQLITE_RESULT');
    }
    bySeq.set(row.seq, row.id);
  }
  if (bySeq.size !== expectedCount) fail('SQLITE_RESULT');
  return Array.from({ length: expectedCount }, (_, index) => bySeq.get(index + 1));
}

export async function ingestSupportedJobs(database, rows) {
  const db = databasePath(database);
  if (!Array.isArray(rows) || rows.length > MAX_SOURCE_ROWS) fail('INVALID_SOURCE_ROWS');
  const normalized = [];
  for (const row of rows) {
    const job = normalizeSourceJob(row);
    if (job !== null) normalized.push(job);
  }
  const deduped = dedupeNormalizedJobs(normalized);
  if (deduped.length === 0) return immutable({ count: 0, ids: [] });
  const sql = ingestSql(deduped);
  if (Buffer.byteLength(sql, 'utf8') > SQLITE_MAX_INPUT) fail('SQLITE_INPUT_LIMIT');
  await ensureSchema(db, [...CORE_COLUMNS, ...BINDING_COLUMNS]);
  const resultRows = parseRows(await runSqlite(db, sql), 'SQLITE_RESULT');
  const ids = normalizeResultRows(resultRows, deduped.length);
  return immutable({ count: ids.length, ids });
}

function validateStoredSnapshot(row) {
  if (!isRecord(row)) fail('BOUND_JOB_INVALID');
  if (!Number.isSafeInteger(row.id) || row.id < 1) fail('BOUND_JOB_INVALID');
  if (typeof row.platform !== 'string' || !SUPPORTED_PLATFORMS.has(row.platform)) fail('BOUND_JOB_INVALID');
  if (typeof row.application_url !== 'string') fail('BOUND_JOB_INVALID');
  let canonicalUrl;
  try {
    canonicalUrl = canonicalizeApplicationUrl(row.application_url);
  } catch {
    fail('BOUND_JOB_INVALID');
  }
  if (canonicalUrl !== row.application_url || classifyApplicationUrl(row.application_url) !== row.platform) {
    fail('BOUND_JOB_INVALID');
  }
  const title = normalizedText(row.job_title, 'BOUND_JOB_INVALID');
  const company = normalizedText(row.job_company, 'BOUND_JOB_INVALID');
  const location = normalizedText(row.job_location, 'BOUND_JOB_INVALID');
  const description = safeString(row.job_description, 'BOUND_JOB_INVALID', SOURCE_TEXT_MAX, { trim: true });
  if (description.replace(/\s+/gu, ' ').trim() !== description) fail('BOUND_JOB_INVALID');
  if (typeof row.job_description_sha256 !== 'string'
      || !SHA256_HEX.test(row.job_description_sha256)
      || sha256(description) !== row.job_description_sha256) {
    fail('BOUND_JOB_INVALID');
  }
  if (typeof row.eligibility_tier !== 'string' || !ELIGIBILITY_TIERS.has(row.eligibility_tier)) {
    fail('BOUND_JOB_INVALID');
  }
  const sourcePostedAt = normalizeTimestamp(row.source_posted_at, 'BOUND_JOB_INVALID');
  const sourceLastSeenAt = normalizeTimestamp(row.source_last_seen_at, 'BOUND_JOB_INVALID');
  if (row.source_posted_at !== null && row.source_posted_at !== undefined
      && row.source_posted_at !== sourcePostedAt) {
    fail('BOUND_JOB_INVALID');
  }
  if (row.source_last_seen_at !== null && row.source_last_seen_at !== undefined
      && row.source_last_seen_at !== sourceLastSeenAt) {
    fail('BOUND_JOB_INVALID');
  }
  return {
    id: row.id,
    platform: row.platform,
    applicationUrl: row.application_url,
    title,
    company,
    location,
    description,
    descriptionSha256: row.job_description_sha256,
    sourcePostedAt,
    sourceLastSeenAt,
    eligibilityTier: row.eligibility_tier,
  };
}

export async function loadBoundJob(database, jobId) {
  const db = databasePath(database);
  const id = requirePositiveInteger(jobId, 'INVALID_JOB_ID');
  await ensureSchema(db, [...CORE_COLUMNS, ...BINDING_COLUMNS]);
  const rows = parseRows(
    await runSqlite(
      db,
      `SELECT id, platform, application_url, job_title, job_company, job_location,
              job_description, job_description_sha256, source_posted_at, source_last_seen_at,
              eligibility_tier
       FROM application_jobs
       WHERE id = ${sqlLiteral(id)};`,
    ),
    'SQLITE_RESULT',
  );
  if (rows.length === 0) return null;
  if (rows.length !== 1) fail('SQLITE_RESULT');
  return immutable(validateStoredSnapshot(rows[0]));
}

function queuedSelectSql(lastId) {
  return `
    SELECT id, platform, application_url, job_title, job_company, job_location,
           job_description, job_description_sha256, source_posted_at, source_last_seen_at,
           eligibility_tier
    FROM application_jobs
    WHERE status = 'queued' AND id > ${sqlLiteral(lastId)}
    ORDER BY id ASC
    LIMIT ${BOUND_BATCH_SIZE};`;
}

async function readQueuedBoundRows(database) {
  const all = [];
  let lastId = 0;
  while (true) {
    const rows = parseRows(await runSqlite(database, queuedSelectSql(lastId)), 'SQLITE_RESULT');
    if (rows.length === 0) break;
    if (rows.length > BOUND_BATCH_SIZE) fail('SQLITE_RESULT');
    let nextId = lastId;
    for (const row of rows) {
      if (!isRecord(row) || !Number.isSafeInteger(row.id) || row.id <= nextId) fail('SQLITE_RESULT');
      nextId = row.id;
      all.push(row);
    }
    if (all.length > MAX_QUEUED_ROWS) fail('SQLITE_OUTPUT_LIMIT');
    lastId = nextId;
    if (rows.length < BOUND_BATCH_SIZE) break;
  }
  return all;
}

function listOrder(a, b) {
  const tierRank = {
    active_verified: 0,
    unverified_stale: 1,
    backfill_only: 2,
  };
  const rankDifference = tierRank[a.eligibilityTier] - tierRank[b.eligibilityTier];
  if (rankDifference !== 0) return rankDifference;
  const aLast = a.sourceLastSeenAt;
  const bLast = b.sourceLastSeenAt;
  if (aLast === null && bLast !== null) return 1;
  if (aLast !== null && bLast === null) return -1;
  if (aLast !== null && bLast !== null) {
    const dateDifference = Date.parse(bLast) - Date.parse(aLast);
    if (dateDifference !== 0) return dateDifference;
  }
  return a.id - b.id;
}

export async function listBoundQueuedJobs(database) {
  const db = databasePath(database);
  await ensureSchema(db, [...CORE_COLUMNS, ...BINDING_COLUMNS]);
  const rows = await readQueuedBoundRows(db);
  const jobs = [];
  for (const row of rows) {
    try {
      const snapshot = validateStoredSnapshot(row);
      const { description, ...summary } = snapshot;
      jobs.push(summary);
    } catch {
      // Unbound, unsupported, or tampered queued rows are not claim candidates.
    }
  }
  jobs.sort(listOrder);
  return immutable({ count: jobs.length, jobs });
}

async function readQueuedUrls(database) {
  const rows = [];
  let lastId = 0;
  while (true) {
    const batch = parseRows(
      await runSqlite(
        database,
        `SELECT id, application_url
         FROM application_jobs
         WHERE status = 'queued' AND id > ${sqlLiteral(lastId)}
         ORDER BY id ASC
         LIMIT ${URL_BATCH_SIZE};`,
      ),
      'SQLITE_RESULT',
    );
    if (batch.length === 0) break;
    if (batch.length > URL_BATCH_SIZE) fail('SQLITE_RESULT');
    let nextId = lastId;
    for (const row of batch) {
      if (!isRecord(row) || !Number.isSafeInteger(row.id) || row.id <= nextId) fail('SQLITE_RESULT');
      nextId = row.id;
      rows.push({ id: row.id, applicationUrl: row.application_url });
    }
    if (rows.length > MAX_QUEUED_ROWS) fail('SQLITE_OUTPUT_LIMIT');
    lastId = nextId;
    if (batch.length < URL_BATCH_SIZE) break;
  }
  return rows;
}

function quarantineSql(rows) {
  const statements = [
    'PRAGMA foreign_keys = ON;',
    'BEGIN IMMEDIATE;',
    'CREATE TEMP TABLE _job_source_quarantine (id INTEGER PRIMARY KEY, application_url TEXT);',
  ];
  for (const row of rows) {
    statements.push(
      `INSERT INTO _job_source_quarantine (id, application_url) VALUES (${sqlLiteral(row.id)}, ${sqlLiteral(row.applicationUrl)});`,
    );
  }
  statements.push(`
    UPDATE application_jobs
    SET status = 'skipped', status_reason = 'unsupported_platform'
    WHERE status = 'queued'
      AND EXISTS (
        SELECT 1
        FROM _job_source_quarantine AS candidate
        WHERE candidate.id = application_jobs.id
          AND (
            candidate.application_url = application_jobs.application_url
            OR (candidate.application_url IS NULL AND application_jobs.application_url IS NULL)
          )
      );
    SELECT candidate.id
    FROM _job_source_quarantine AS candidate
    JOIN application_jobs AS job ON job.id = candidate.id
    WHERE job.status = 'skipped'
      AND job.status_reason = 'unsupported_platform'
      AND (
        candidate.application_url = job.application_url
        OR (candidate.application_url IS NULL AND job.application_url IS NULL)
      )
    ORDER BY candidate.id ASC;
    COMMIT;
  `);
  return statements.join('\n');
}

export async function quarantineUnsupportedQueuedJobs(database) {
  const db = databasePath(database);
  await ensureSchema(db, ['id', 'application_url', 'status', 'status_reason']);
  const queued = await readQueuedUrls(db);
  const unsupported = [];
  for (const row of queued) {
    let platform = null;
    try {
      platform = classifyApplicationUrl(row.applicationUrl);
    } catch {
      platform = null;
    }
    if (platform === null) unsupported.push(row);
  }
  if (unsupported.length === 0) return immutable({ count: 0, ids: [] });
  if (unsupported.length > QUARANTINE_MAX_ROWS) fail('SQLITE_INPUT_LIMIT');
  const ids = parseRows(await runSqlite(db, quarantineSql(unsupported)), 'SQLITE_RESULT')
    .map((row) => {
      if (!isRecord(row) || !Number.isSafeInteger(row.id) || row.id < 1) fail('SQLITE_RESULT');
      return row.id;
    });
  const uniqueIds = [...new Set(ids)].sort((a, b) => a - b);
  return immutable({ count: uniqueIds.length, ids: uniqueIds });
}
