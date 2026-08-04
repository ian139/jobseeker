import { createHash } from 'node:crypto';
import { chmod, lstat, mkdir, open, readFile, rename } from 'node:fs/promises';
import { dirname, join, relative, resolve, sep } from 'node:path';
import { DatabaseSync } from 'node:sqlite';
import {
  canonicalJson,
  sha256Canonical,
  validateNormalizedJob,
  validateSourceSyncResult,
} from './contracts.mjs';

const RESULT_SCHEMA = 'source-sync-result-v1';
const RECEIPT_SCHEMA = 'source-sync-page-receipt-v1';
const SHA256 = /^[0-9a-f]{64}$/;
const ISO = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{3})?Z$/;
const TERMINAL = new Set(['succeeded', 'failed', 'paid_ambiguous']);
const DIR_MODE = 0o700;
const FILE_MODE = 0o600;

export class SyncFailure extends Error {
  constructor(reasonCode, failureClass = 'validation') {
    super(reasonCode);
    this.name = 'SyncFailure';
    this.reasonCode = reasonCode;
    this.failureClass = failureClass;
  }
}
const FAILURE_CLASSES = new Set(['retryable', 'terminal', 'paid_ambiguous', 'authentication', 'account_health']);

export class CrashInjection extends Error {
  constructor(point) {
    super(point);
    this.name = 'CrashInjection';
    this.point = point;
  }
}

function fail(reasonCode, failureClass = 'validation') {
  throw new SyncFailure(reasonCode, failureClass);
}
function resultFailureClass(value, state) {
  if (state === 'paid_ambiguous') return 'paid_ambiguous';
  if (FAILURE_CLASSES.has(value)) return value;
  if (value === 'authentication') return 'authentication';
  if (value === 'account_health') return 'account_health';
  return state === 'failed' ? 'terminal' : null;
}

function object(value) {
  return value !== null && typeof value === 'object' && !Array.isArray(value);
}

function string(value, code) {
  if (typeof value !== 'string' || value.length === 0) fail(code);
  return value;
}

function time(value, code) {
  if (typeof value !== 'string' || !ISO.test(value) || !Number.isFinite(Date.parse(value))) fail(code);
  return new Date(Date.parse(value)).toISOString();
}

function digest(value, code) {
  if (typeof value !== 'string' || !SHA256.test(value)) fail(code);
  return value;
}

function integer(value, code, min = 0, max = Number.MAX_SAFE_INTEGER) {
  if (!Number.isSafeInteger(value) || value < min || value > max) fail(code);
  return value;
}

function q(value) {
  if (typeof value !== 'string' || !/^[A-Za-z_][A-Za-z0-9_]*$/.test(value)) throw new TypeError('unsafe sqlite identifier');
  return `"${value}"`;
}

function databaseLike(value) {
  return value !== null && typeof value === 'object' && typeof value.prepare === 'function' && typeof value.exec === 'function';
}

function openDatabase(options) {
  const supplied = options.database ?? options.db;
  if (databaseLike(supplied)) return { database: supplied, owned: false };
  const path = options.databasePath ?? options.dbPath ?? (typeof supplied === 'string' ? supplied : null);
  string(path, 'database_required');
  try {
    return { database: new DatabaseSync(path), owned: true };
  } catch {
    fail('database_open_failed', 'database');
  }
}

function run(database, sql, values = []) {
  try {
    return database.prepare(sql).run(...values);
  } catch {
    fail('database_write_failed', 'database');
  }
}

function one(database, sql, values = []) {
  try {
    return database.prepare(sql).get(...values) ?? null;
  } catch {
    fail('database_read_failed', 'database');
  }
}

function many(database, sql, values = []) {
  try {
    return database.prepare(sql).all(...values);
  } catch {
    fail('database_read_failed', 'database');
  }
}

function sql(database, statement) {
  try {
    database.exec(statement);
  } catch {
    fail('database_transaction_failed', 'database');
  }
}

function tx(database, callback) {
  sql(database, 'BEGIN IMMEDIATE');
  try {
    const result = callback();
    sql(database, 'COMMIT');
    return result;
  } catch (error) {
    try {
      database.exec('ROLLBACK');
    } catch {
      // Keep the first stable failure.
    }
    throw error;
  }
}

function columns(database, table) {
  try {
    return many(database, `PRAGMA table_info(${q(table)})`).map((row) => ({
      name: String(row.name),
      type: String(row.type ?? '').toUpperCase(),
      notnull: Number(row.notnull) === 1,
      pk: Number(row.pk) > 0,
      defaultValue: row.dflt_value,
    }));
  } catch {
    fail('database_schema_required', 'database');
  }
}

function hasTable(database, table) {
  return one(database, "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", [table]) !== null;
}

function assertMigrated(database) {
  const required = ['jobs', 'source_observations', 'dedupe_groups', 'source_checkpoints', 'sync_runs', 'source_sync_pages', 'schema_migrations'];
  for (const table of required) if (!hasTable(database, table) || columns(database, table).length === 0) fail('database_schema_required', 'database');
  const migrations = many(database, 'SELECT * FROM schema_migrations');
  if (!migrations.some((row) => Object.values(row).some((value) => String(value) === '005' || String(value) === '5' || String(value).startsWith('005-')))) fail('database_schema_required', 'database');
  const expected = {
    jobs: ['id', 'source', 'source_job_id', 'canonical_application_url', 'dedupe_group_id', 'raw_payload_path', 'raw_payload_sha256'],
    source_observations: ['id', 'sync_run_id', 'job_id', 'source', 'observed_at', 'raw_payload_path', 'raw_payload_sha256', 'normalized_job_sha256'],
    dedupe_groups: ['id', 'identity_kind', 'identity_key', 'review_required'],
    source_checkpoints: ['source', 'profile', 'checkpoint', 'last_sync_run_id', 'revision'],
    sync_runs: ['id', 'source', 'profile', 'mode', 'state', 'pending_page', 'pending_request_sha256', 'pending_started_at', 'next_page'],
    source_sync_pages: ['sync_run_id', 'page_number', 'request_sha256', 'response_path', 'response_sha256', 'item_count', 'total_results'],
  };
  for (const [table, names] of Object.entries(expected)) {
    const actual = new Set(columns(database, table).map((column) => column.name));
    if (names.some((name) => !actual.has(name))) fail('database_schema_required', 'database');
  }
}

function context(options, paid) {
  const adapter = options.adapter;
  if (!adapter || typeof adapter.buildRequest !== 'function' || typeof adapter.fetchPage !== 'function' || typeof adapter.normalizeJob !== 'function') fail('adapter_required');
  const source = options.source ?? adapter.source;
  if (typeof source !== 'string' || !/^[a-z0-9][a-z0-9_-]{0,63}$/.test(source) || adapter.source !== source) fail('source_required');
  const profile = options.profile;
  if (typeof profile !== 'string' || profile.length === 0 || profile.length > 128) fail('profile_required');
  const now = time(options.now, 'invalid_now');
  if (paid && options.paidAuthorization !== true) fail('paid_authorization_required', 'authorization');
  return { adapter, source, profile, now };
}

function bounds(options) {
  const input = object(options.bounds) ? options.bounds : {};
  const limit = options.limit ?? options.pageSize ?? input.limit;
  const maxPages = options.maxPages ?? input.maxPages;
  const maxItems = options.maxItems ?? input.maxItems ?? null;
  const windowEnd = options.windowEnd ?? input.windowEnd;
  const checkpoint = options.checkpoint ?? input.checkpoint ?? null;
  integer(limit, 'invalid_limit', 1, 100);
  integer(maxPages, 'invalid_max_pages', 1, 1000);
  if (maxItems !== null) integer(maxItems, 'invalid_max_items', 1, 1_000_000);
  const end = time(windowEnd, 'invalid_window_end');
  const start = checkpoint === null ? null : time(checkpoint, 'invalid_checkpoint');
  if (start !== null && start > end) fail('invalid_time_bounds');
  return { limit, maxPages, maxItems, windowEnd: end, checkpoint: start };
}

function check(validator, value, code) {
  try {
    const checked = validator(value);
    if (checked === false) fail(code);
    return checked && typeof checked === 'object' ? checked : value;
  } catch (error) {
    if (error instanceof SyncFailure) throw error;
    fail(code);
  }
}

function canonicalDigest(value) {
  try {
    return sha256Canonical(value);
  } catch {
    return createHash('sha256').update(canonicalJson(value), 'utf8').digest('hex');
  }
}

function privateRoot(value) {
  string(value, 'private_root_required');
  return resolve(value);
}

async function ensurePrivateRoot(value) {
  const root = privateRoot(value);
  let info;
  try {
    info = await lstat(root);
  } catch {
    fail('private_root_required', 'filesystem');
  }
  if (!info.isDirectory() || (info.mode & 0o077) !== 0) fail('private_root_not_private', 'filesystem');
  return root;
}

async function privateDir(path) {
  try {
    await mkdir(path, { recursive: true, mode: DIR_MODE });
    const info = await lstat(path);
    if (!info.isDirectory() || (info.mode & 0o077) !== 0) fail('private_directory_not_private', 'filesystem');
    if ((info.mode & 0o777) !== DIR_MODE) await chmod(path, DIR_MODE);
  } catch (error) {
    if (error instanceof SyncFailure) throw error;
    fail('private_directory_failed', 'filesystem');
  }
}

async function privateFile(path, text) {
  await privateDir(dirname(path));
  const temporary = `${path}.tmp-${process.pid}-${Math.random().toString(16).slice(2)}`;
  let handle;
  try {
    handle = await open(temporary, 'wx', FILE_MODE);
    await handle.writeFile(text, 'utf8');
    await handle.sync();
    await handle.close();
    await rename(temporary, path);
    await chmod(path, FILE_MODE);
  } catch {
    try {
      await handle?.close();
    } catch {
      // Best effort close.
    }
    try {
      const { unlink } = await import('node:fs/promises');
      await unlink(temporary);
    } catch {
      // Best effort cleanup.
    }
    fail('private_artifact_write_failed', 'filesystem');
  }
}

async function readPrivate(path) {
  let info;
  try {
    info = await lstat(path);
  } catch {
    fail('private_artifact_missing', 'filesystem');
  }
  if (!info.isFile() || (info.mode & 0o077) !== 0) fail('private_artifact_not_private', 'filesystem');
  try {
    return await readFile(path, 'utf8');
  } catch {
    fail('private_artifact_read_failed', 'filesystem');
  }
}

function rel(root, path) {
  const value = relative(root, path);
  if (!value || value.startsWith('..') || value.startsWith(sep) || value.includes(`..${sep}`)) fail('unsafe_artifact_path', 'filesystem');
  return value.split(sep).join('/');
}

function abs(root, value) {
  if (typeof value !== 'string' || value.length === 0 || value.startsWith('/') || value.includes('\\')) fail('unsafe_artifact_path', 'filesystem');
  const path = resolve(root, value);
  if (!path.startsWith(`${root}${sep}`)) fail('unsafe_artifact_path', 'filesystem');
  return path;
}

function requestCheck(value, page, limit) {
  if (!object(value) || typeof value.url !== 'string' || !object(value.body)) fail('invalid_request');
  integer(value.page, 'invalid_request');
  integer(value.limit, 'invalid_request', 1, 100);
  digest(value.requestSha256, 'invalid_request');
  if (value.page !== page || value.limit !== limit) fail('request_mismatch');
  return value;
}

function responseCheck(value, request, now, expectedTotal) {
  if (!object(value) || !Array.isArray(value.items)) fail('invalid_response');
  digest(value.requestSha256, 'invalid_response');
  if (value.requestSha256 !== request.requestSha256) fail('request_mismatch');
  if (value.items.length > request.limit || value.items.some((item) => !object(item))) fail('invalid_response');
  const total = value.totalResults === undefined ? expectedTotal : value.totalResults;
  if (total !== null) integer(total, 'invalid_response');
  if (expectedTotal !== null && total !== expectedTotal) fail('totals_changed');
  if (total !== null && value.items.length > total) fail('total_bounds_exceeded');
  const receivedAt = time(value.receivedAt, 'invalid_response');
  if (receivedAt > now) fail('response_time_out_of_bounds');
  const estimatedCredits = value.estimatedCredits ?? 0;
  if (typeof estimatedCredits !== 'number' || !Number.isFinite(estimatedCredits) || estimatedCredits < 0) fail('invalid_response');
  return { items: value.items, totalResults: total, receivedAt, estimatedCredits };
}

async function buildRequest(ctx, b, page, mode) {
  try {
    const request = await ctx.adapter.buildRequest({ profile: ctx.profile, mode, page, limit: b.limit, checkpoint: b.checkpoint, windowEnd: b.windowEnd, includeTotals: page === 0 });
    return requestCheck(request, page, b.limit);
  } catch (error) {
    if (error instanceof SyncFailure) throw error;
    fail('request_build_failed');
  }
}

async function fetchPage(ctx, request, mode) {
  try {
    return await ctx.adapter.fetchPage({ request, mode });
  } catch {
    fail('paid_request_ambiguous', 'paid_ambiguous');
  }
}

function idOf(result) {
  const value = result?.lastInsertRowid;
  const number = typeof value === 'bigint' ? Number(value) : Number(value);
  if (!Number.isSafeInteger(number)) fail('database_write_failed', 'database');
  return number;
}

function runRow(database, id) {
  return one(database, 'SELECT * FROM sync_runs WHERE id=?', [id]);
}

function pages(database, id) {
  return many(database, 'SELECT * FROM source_sync_pages WHERE sync_run_id=? ORDER BY page_number', [id]);
}

function checkpointRow(database, source, profile) {
  return one(database, 'SELECT * FROM source_checkpoints WHERE source=? AND profile=?', [source, profile]);
}

function maxTime(a, b) {
  if (!a) return b;
  if (!b) return a;
  return a >= b ? a : b;
}

function createRun(database, ctx, b, root) {
  const result = run(database, `INSERT INTO sync_runs (
    source,profile,mode,state,started_at,finished_at,window_end_at,checkpoint_before,
    checkpoint_after,artifact_dir,request_count,pages_fetched,jobs_seen,jobs_inserted,
    jobs_updated,jobs_unchanged,dedupe_groups_touched,queue_rows_inserted,estimated_credits,
    reported_credits,pending_page,pending_request_sha256,pending_started_at,next_page,
    expected_total_results,failure_class,reason_code,result_sha256
  ) VALUES (${new Array(28).fill('?').join(',')})`, [
    ctx.source, ctx.profile, 'paid', 'fetching', ctx.now, null, b.windowEnd, b.checkpoint,
    null, `source-sync/${ctx.source}`, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0,
    null, null, null, 0, null, null, null, null,
  ]);
  const id = idOf(result);
  const row = runRow(database, id);
  if (!row) fail('database_write_failed', 'database');
  const rootDirectory = join(root, 'source-sync', ctx.source, String(row.id));
  updateRun(database, id, { artifact_dir: `source-sync/${ctx.source}/${row.id}` });
  return { ...row, artifact_dir: `source-sync/${ctx.source}/${row.id}`, rootDirectory };
}

function updateRun(database, id, values) {
  const keys = Object.keys(values);
  if (keys.length === 0) return;
  run(database, `UPDATE sync_runs SET ${keys.map((key) => `${q(key)}=?`).join(',')} WHERE id=?`, [...keys.map((key) => values[key]), id]);
}

function resultFrom(row, ctx, overrides = {}) {
  const state = overrides.state ?? row?.state;
  const publicState = state === 'ready_to_commit' || state === 'fetching' ? 'failed' : state;
  return check(validateSourceSyncResult, {
    schema: RESULT_SCHEMA,
    syncRunId: overrides.syncRunId ?? row?.id ?? null,
    source: overrides.source ?? row?.source ?? ctx.source,
    profile: overrides.profile ?? row?.profile ?? ctx.profile,
    mode: overrides.mode ?? row?.mode ?? 'paid',
    state: publicState,
    startedAt: overrides.startedAt ?? row?.started_at ?? ctx.now,
    finishedAt: overrides.finishedAt ?? row?.finished_at ?? null,
    checkpointBefore: overrides.checkpointBefore ?? row?.checkpoint_before ?? null,
    checkpointAfter: overrides.checkpointAfter ?? row?.checkpoint_after ?? null,
    pagesFetched: overrides.pagesFetched ?? Number(row?.pages_fetched ?? 0),
    requestCount: overrides.requestCount ?? Number(row?.request_count ?? 0),
    jobsSeen: overrides.jobsSeen ?? Number(row?.jobs_seen ?? 0),
    jobsInserted: overrides.jobsInserted ?? Number(row?.jobs_inserted ?? 0),
    jobsUpdated: overrides.jobsUpdated ?? Number(row?.jobs_updated ?? 0),
    jobsUnchanged: overrides.jobsUnchanged ?? Number(row?.jobs_unchanged ?? 0),
    dedupeGroupsTouched: overrides.dedupeGroupsTouched ?? Number(row?.dedupe_groups_touched ?? 0),
    queueRowsInserted: overrides.queueRowsInserted ?? Number(row?.queue_rows_inserted ?? 0),
    estimatedCredits: overrides.estimatedCredits ?? Number(row?.estimated_credits ?? 0),
    reportedCredits: overrides.reportedCredits ?? Number(row?.reported_credits ?? 0),
    failureClass: overrides.failureClass !== undefined
      ? resultFailureClass(overrides.failureClass, publicState)
      : resultFailureClass(row?.failure_class ?? null, publicState),
    reasonCode: overrides.reasonCode ?? row?.reason_code ?? null,
  }, 'invalid_result');
}

async function publishPage(root, source, id, page, request, response) {
  const pageRoot = join(root, 'source-sync', source, String(id), 'pages', String(page));
  const jobsRoot = join(pageRoot, 'jobs');
  await privateDir(jobsRoot);
  const items = [];
  for (let index = 0; index < response.items.length; index += 1) {
    const text = canonicalJson(response.items[index]);
    const payloadSha = createHash('sha256').update(text, 'utf8').digest('hex');
    const path = join(jobsRoot, `${String(index).padStart(6, '0')}-${payloadSha}.json`);
    await privateFile(path, text);
    items.push({ index, rawPayloadPath: rel(root, path), rawPayloadSha256: payloadSha });
  }
  const receipt = {
    schema: RECEIPT_SCHEMA,
    source,
    syncRunId: id,
    pageNumber: page,
    requestSha256: request.requestSha256,
    itemCount: response.items.length,
    totalResults: response.totalResults,
    receivedAt: response.receivedAt,
    estimatedCredits: response.estimatedCredits,
    items,
  };
  const text = canonicalJson(receipt);
  const responseSha = createHash('sha256').update(text, 'utf8').digest('hex');
  const responsePath = join(pageRoot, 'response.json');
  await privateFile(responsePath, text);
  return { responsePath: rel(root, responsePath), responseSha, receipt, items };
}

async function loadPage(root, page) {
  digest(page.response_sha256, 'private_artifact_digest_mismatch');
  const text = await readPrivate(abs(root, page.response_path));
  if (createHash('sha256').update(text, 'utf8').digest('hex') !== page.response_sha256) fail('private_artifact_digest_mismatch', 'filesystem');
  let receipt;
  try {
    receipt = JSON.parse(text);
  } catch {
    fail('private_artifact_invalid', 'filesystem');
  }
  if (canonicalJson(receipt) !== text) fail('private_artifact_noncanonical', 'filesystem');
  if (!object(receipt) || receipt.schema !== RECEIPT_SCHEMA || !Array.isArray(receipt.items)) fail('private_artifact_invalid', 'filesystem');
  if (receipt.requestSha256 !== page.request_sha256 || receipt.itemCount !== page.item_count || receipt.items.length !== page.item_count || receipt.totalResults !== page.total_results || receipt.receivedAt !== page.received_at || receipt.estimatedCredits !== page.estimated_credits) fail('private_artifact_metadata_mismatch', 'filesystem');
  const indexes = new Set();
  for (const entry of receipt.items) {
    if (!object(entry) || !Number.isSafeInteger(entry.index) || indexes.has(entry.index)) fail('private_artifact_invalid', 'filesystem');
    indexes.add(entry.index);
    digest(entry.rawPayloadSha256, 'private_artifact_digest_mismatch');
    const rawText = await readPrivate(abs(root, entry.rawPayloadPath));
    if (createHash('sha256').update(rawText, 'utf8').digest('hex') !== entry.rawPayloadSha256) fail('private_artifact_digest_mismatch', 'filesystem');
    let raw;
    try {
      raw = JSON.parse(rawText);
    } catch {
      fail('private_artifact_invalid', 'filesystem');
    }
    if (canonicalJson(raw) !== rawText) fail('private_artifact_noncanonical', 'filesystem');
    if (!object(raw)) fail('private_artifact_invalid', 'filesystem');
    items.push({ index: entry.index, raw, rawPayloadPath: entry.rawPayloadPath, rawPayloadSha256: entry.rawPayloadSha256 });
  }
  items.sort((a, b) => a.index - b.index);
  return { receipt, items };
}

async function normalizePages(root, database, run, ctx, b) {
  const result = [];
  const sourceIds = new Set();
  const applicationUrls = new Set();
  for (const page of pages(database, run.id)) {
    for (const item of (await loadPage(root, page)).items) {
      let normalized;
      try {
        normalized = await ctx.adapter.normalizeJob(item.raw, { observedAt: ctx.now, rawPayloadPath: item.rawPayloadPath, rawPayloadSha256: item.rawPayloadSha256 });
      } catch {
        fail('normalization_failed');
      }
      normalized = check(validateNormalizedJob, normalized, 'normalization_failed');
      if (normalized.source !== ctx.source || normalized.rawPayloadPath !== item.rawPayloadPath || normalized.rawPayloadSha256 !== item.rawPayloadSha256) fail('normalization_artifact_mismatch');
      if (normalized.discoveredAt > b.windowEnd || normalized.discoveredAt > ctx.now || (b.checkpoint && normalized.discoveredAt < b.checkpoint)) fail('discovered_time_out_of_bounds');
      if (normalized.sourceJobId !== null) {
        const id = String(normalized.sourceJobId);
        if (sourceIds.has(id)) fail('duplicate_job_identity');
        sourceIds.add(id);
      }
      if (normalized.canonicalApplicationUrl !== null) {
        if (applicationUrls.has(normalized.canonicalApplicationUrl)) fail('duplicate_job_identity');
        applicationUrls.add(normalized.canonicalApplicationUrl);
      }
      result.push(normalized);
    }
  }
  if (b.maxItems !== null && result.length > b.maxItems) fail('item_bounds_exceeded');
  return result;
}

function dedupeGroup(database, job, now) {
  const existing = one(database, 'SELECT * FROM dedupe_groups WHERE identity_kind=? AND identity_key=?', [job.dedupeIdentityKind, job.dedupeIdentityKey]);
  if (existing) {
    run(database, 'UPDATE dedupe_groups SET review_required=?,updated_at=? WHERE id=?', [job.dedupeReviewRequired ? 1 : 0, now, existing.id]);
    return existing.id;
  }
  return idOf(run(database, 'INSERT INTO dedupe_groups (identity_kind,identity_key,review_required,created_at,updated_at) VALUES (?,?,?,?,?)', [job.dedupeIdentityKind, job.dedupeIdentityKey, job.dedupeReviewRequired ? 1 : 0, now, now]));
}

function existingJob(database, job) {
  const source = job.sourceJobId === null ? null : one(database, 'SELECT * FROM jobs WHERE source=? AND source_job_id=?', [job.source, job.sourceJobId]);
  const url = job.canonicalApplicationUrl === null ? null : one(database, 'SELECT * FROM jobs WHERE source=? AND canonical_application_url=?', [job.source, job.canonicalApplicationUrl]);
  if (source && url && source.id !== url.id) fail('identity_conflict');
  return source ?? url;
}

function jobParams(job, groupId, now, existing) {
  return [
    job.source,
    job.sourceJobId,
    job.canonicalListingUrl,
    job.canonicalApplicationUrl,
    job.atsKind,
    job.atsIdentifier,
    job.title,
    job.company,
    job.location,
    job.workplaceType,
    canonicalJson(job.employmentTypes),
    job.description,
    job.descriptionSha256,
    job.sourcePostedAt,
    job.sourceUpdatedAt,
    job.discoveredAt,
    existing?.first_seen_at ?? job.discoveredAt,
    job.discoveredAt,
    job.availabilityState,
    job.freshnessState,
    job.eligibilityState,
    canonicalJson(job.eligibilityReasonCodes),
    job.priority,
    groupId,
    job.rawPayloadPath,
    job.rawPayloadSha256,
    now,
  ];
}

function upsertJob(database, job, groupId, now) {
  const existing = existingJob(database, job);
  const params = jobParams(job, groupId, now, existing);
  if (existing) {
    run(database, `UPDATE jobs SET source=?,source_job_id=?,canonical_listing_url=?,canonical_application_url=?,ats_kind=?,ats_identifier=?,title=?,company=?,location=?,workplace_type=?,employment_types_json=?,description=?,description_sha256=?,source_posted_at=?,source_updated_at=?,discovered_at=?,first_seen_at=?,last_seen_at=?,availability_state=?,freshness_state=?,eligibility_state=?,eligibility_reason_codes_json=?,priority=?,dedupe_group_id=?,raw_payload_path=?,raw_payload_sha256=? WHERE id=?`, [...params.slice(0, 26), existing.id]);
    return { id: existing.id, inserted: false, unchanged: existing.raw_payload_sha256 === job.rawPayloadSha256 && existing.discovered_at === job.discoveredAt };
  }
  const result = run(database, `INSERT INTO jobs (source,source_job_id,canonical_listing_url,canonical_application_url,ats_kind,ats_identifier,title,company,location,workplace_type,employment_types_json,description,description_sha256,source_posted_at,source_updated_at,discovered_at,first_seen_at,last_seen_at,availability_state,freshness_state,eligibility_state,eligibility_reason_codes_json,priority,dedupe_group_id,raw_payload_path,raw_payload_sha256) VALUES (${new Array(26).fill('?').join(',')})`, params.slice(0, 26));
  return { id: idOf(result), inserted: true, unchanged: false };
}
function appRows(database, jobId, job = null) {
  const names = appColumns(database);
  if (!names) return [];
  const rows = [];
  if (names.has('source_table') && names.has('source_db') && names.has('source_rowid')) {
    rows.push(...many(database, "SELECT * FROM application_jobs WHERE source_table='jobs' AND source_db='ingestion' AND source_rowid=?", [jobId]));
  } else if (names.has('job_id')) {
    rows.push(...many(database, 'SELECT * FROM application_jobs WHERE job_id=?', [jobId]));
  }
  if (job?.canonicalApplicationUrl && names.has('application_url')) {
    for (const row of many(database, 'SELECT * FROM application_jobs WHERE application_url=?', [job.canonicalApplicationUrl])) {
      if (!rows.some((current) => current.id === row.id)) rows.push(row);
    }
  }
  return rows;
}

function queuedGroup(database, groupId) {
  const names = appColumns(database);
  if (!names?.has('status') || !names.has('dedupe_group_id')) return false;
  return one(database, "SELECT 1 FROM application_jobs WHERE status='queued' AND dedupe_group_id=?", [groupId]) !== null;
}

function addQueue(database, job, jobId, groupId, now) {
  const names = appColumns(database);
  if (!names?.has('status')) return false;
  const tier = job.eligibilityState === 'eligible'
    ? 'active_verified'
    : job.freshnessState === 'stale'
      ? 'unverified_stale'
      : 'backfill_only';
  const candidate = {
    status: 'queued',
    source_rowid: jobId,
    source_job_id: job.sourceJobId ?? job.canonicalApplicationUrl,
    source_table: 'jobs',
    source_db: 'ingestion',
    application_url: job.canonicalApplicationUrl,
    eligibility_tier: tier,
    verification_reason: 'source_sync',
    source_posted_at: job.sourcePostedAt,
    source_last_seen_at: job.discoveredAt,
    created_at: now,
    updated_at: now,
  };
  const namesToUse = [];
  const values = [];
  for (const [name, value] of Object.entries(candidate)) if (names.has(name)) {
    namesToUse.push(name);
    values.push(value);
  }
  for (const column of columns(database, 'application_jobs')) {
    if (column.pk || namesToUse.includes(column.name) || !column.notnull || column.defaultValue !== null) continue;
    namesToUse.push(column.name);
    values.push(column.name === 'reason_code' ? 'source_sync' : column.type.includes('INT') ? 0 : column.name.includes('json') ? '[]' : '');
  }
  run(database, `INSERT INTO application_jobs (${namesToUse.map(q).join(',')}) VALUES (${namesToUse.map(() => '?').join(',')})`, values);
  return true;
}

function promote(database, job, jobId, groupId, now, groups) {
  if (job.eligibilityState !== 'eligible') return false;
  const current = appRows(database, jobId, job);
  if (current.length > 0) {
    if (current.some((row) => row.status === 'queued')) groups.add(groupId);
    return false;
  }
  if (groups.has(groupId) || queuedGroup(database, groupId)) return false;
  const added = addQueue(database, job, jobId, groupId, now);
  if (added) groups.add(groupId);
  return added;
}

function observe(database, runIdValue, jobId, job, now) {
  run(database, 'INSERT INTO source_observations (sync_run_id,job_id,source,source_job_id,observed_at,raw_payload_path,raw_payload_sha256,normalized_job_sha256) VALUES (?,?,?,?,?,?,?,?)', [runIdValue, jobId, job.source, job.sourceJobId, now, job.rawPayloadPath, job.rawPayloadSha256, canonicalDigest(job)]);
}

function checkpoint(database, ctx, runIdValue, after, now) {
  const existing = checkpointRow(database, ctx.source, ctx.profile);
  const durable = maxTime(existing?.checkpoint, after);
  if (existing) run(database, 'UPDATE source_checkpoints SET checkpoint=?,last_sync_run_id=?,updated_at=?,revision=revision+1 WHERE source=? AND profile=?', [durable, runIdValue, now, ctx.source, ctx.profile]);
  else run(database, 'INSERT INTO source_checkpoints (source,profile,checkpoint,last_sync_run_id,updated_at,revision) VALUES (?,?,?,?,?,?)', [ctx.source, ctx.profile, durable, runIdValue, now, 1]);
  return durable;
}

function pageAggregate(rows) {
  let jobs = 0;
  let credits = 0;
  let total = null;
  for (const row of rows) {
    jobs += Number(row.item_count) || 0;
    credits += Number(row.estimated_credits) || 0;
    if (row.total_results !== null) {
      if (total !== null && total !== Number(row.total_results)) fail('totals_changed');
      total = Number(row.total_results);
    }
  }
  return { jobs, credits, total };
}

async function commit(database, root, run, ctx, b) {
  const jobs = await normalizePages(root, database, run, ctx, b);
  const pageRows = pages(database, run.id);
  const aggregate = pageAggregate(pageRows);
  let after = checkpointRow(database, ctx.source, ctx.profile)?.checkpoint ?? b.checkpoint;
  for (const job of jobs) after = maxTime(after, job.discoveredAt);
  let inserted = 0;
  let updated = 0;
  let unchanged = 0;
  let groupsTouched = 0;
  let queueInserted = 0;
  tx(database, () => {
    const queuedGroups = new Set();
    const touchedGroups = new Set();
    for (const job of jobs) {
      const groupId = dedupeGroup(database, job, ctx.now);
      touchedGroups.add(groupId);
      const row = upsertJob(database, job, groupId, ctx.now);
      if (row.inserted) inserted += 1;
      else if (row.unchanged) unchanged += 1;
      else updated += 1;
      observe(database, run.id, row.id, job, ctx.now);
      if (promote(database, job, row.id, groupId, ctx.now, queuedGroups)) queueInserted += 1;
    }
    groupsTouched = touchedGroups.size;
    after = checkpoint(database, ctx, run.id, after, ctx.now);
    const result = check(validateSourceSyncResult, {
      schema: RESULT_SCHEMA,
      syncRunId: run.id,
      source: ctx.source,
      profile: ctx.profile,
      mode: 'paid',
      state: 'succeeded',
      startedAt: run.started_at,
      finishedAt: ctx.now,
      checkpointBefore: run.checkpoint_before,
      checkpointAfter: after,
      pagesFetched: pageRows.length,
      requestCount: run.request_count,
      jobsSeen: jobs.length,
      jobsInserted: inserted,
      jobsUpdated: updated,
      jobsUnchanged: unchanged,
      dedupeGroupsTouched: groupsTouched,
      queueRowsInserted: queueInserted,
      estimatedCredits: aggregate.credits,
      reportedCredits: aggregate.credits,
      failureClass: null,
      reasonCode: null,
    }, 'invalid_result');
    updateRun(database, run.id, {
      state: 'succeeded',
      finished_at: ctx.now,
      checkpoint_after: after,
      pages_fetched: pageRows.length,
      jobs_seen: jobs.length,
      jobs_inserted: inserted,
      jobs_updated: updated,
      jobs_unchanged: unchanged,
      dedupe_groups_touched: groupsTouched,
      queue_rows_inserted: queueInserted,
      estimated_credits: aggregate.credits,
      reported_credits: aggregate.credits,
      pending_page: null,
      pending_request_sha256: null,
      pending_started_at: null,
      failure_class: null,
      reason_code: null,
      result_sha256: canonicalDigest(result),
    });
  });
  return resultFrom(runRow(database, run.id), ctx, { state: 'succeeded', checkpointAfter: after });
}

function failRun(database, run, ctx, failureClass, reasonCode, state = 'failed') {
  const storedClass = resultFailureClass(failureClass, state === 'paid_ambiguous' ? 'paid_ambiguous' : 'failed');
  tx(database, () => updateRun(database, run.id, {
    state,
    finished_at: ctx.now,
    failure_class: storedClass,
    reason_code: reasonCode,
    ...(state === 'failed'
      ? { pending_page: null, pending_request_sha256: null, pending_started_at: null }
      : {}),
  }));
  return resultFrom(runRow(database, run.id), ctx, { state, failureClass: storedClass, reasonCode });
}

function hook(options, point, value) {
  const callback = options.hooks?.[point] ?? options.onState;
  if (typeof callback !== 'function') return;
  const returned = callback.length >= 2 ? callback(point, value) : callback({ point, ...value });
  if (returned === false) throw new CrashInjection(point);
}

async function continueRun(database, root, initialRun, ctx, b, options) {
  let run = initialRun;
  let pageRows = pages(database, run.id);
  const first = pageAggregate(pageRows);
  let fetched = first.jobs;
  let expectedTotal = first.total;
  let next = Number(run.next_page) || pageRows.length;
  if (run.pending_page !== null) return failRun(database, run, ctx, 'paid_ambiguous', 'paid_request_ambiguous', 'paid_ambiguous');
  for (;;) {
    if (next >= b.maxPages) return failRun(database, run, ctx, 'bounds', 'page_bounds_exceeded');
    if (pageRows.some((page) => Number(page.page_number) === next)) {
      next += 1;
      continue;
    }
    let request;
    try {
      request = await buildRequest(ctx, b, next, 'paid');
    } catch (error) {
      if (error instanceof SyncFailure) return failRun(database, run, ctx, error.failureClass, error.reasonCode);
      throw error;
    }
    tx(database, () => updateRun(database, run.id, {
      state: 'fetching',
      pending_page: next,
      pending_request_sha256: request.requestSha256,
      pending_started_at: ctx.now,
      request_count: Number(run.request_count) + 1,
      next_page: next,
    }));
    hook(options, 'afterPending', { runId: run.id, page: next, request });
    let raw;
    try {
      raw = await fetchPage(ctx, request, 'paid');
    } catch (error) {
      if (error instanceof SyncFailure) return failRun(database, run, ctx, error.failureClass, error.reasonCode, 'paid_ambiguous');
      throw error;
    }
    let response;
    try {
      response = responseCheck(raw, request, ctx.now, expectedTotal);
    } catch (error) {
      if (error instanceof SyncFailure) return failRun(database, run, ctx, error.failureClass, error.reasonCode);
      throw error;
    }
    if (expectedTotal === null) expectedTotal = response.totalResults;
    if (b.maxItems !== null && fetched + response.items.length > b.maxItems) return failRun(database, run, ctx, 'bounds', 'item_bounds_exceeded');
    let artifact;
    try {
      artifact = await publishPage(root, ctx.source, run.id, next, request, response);
    } catch (error) {
      if (error instanceof SyncFailure) return failRun(database, run, ctx, error.failureClass, error.reasonCode, 'paid_ambiguous');
      throw error;
    }
    hook(options, 'afterReceipt', { runId: run.id, page: next, request, artifact });
    tx(database, () => {
      run(database, 'INSERT INTO source_sync_pages (sync_run_id,page_number,request_sha256,response_path,response_sha256,received_at,item_count,total_results,estimated_credits) VALUES (?,?,?,?,?,?,?,?,?)', [run.id, next, request.requestSha256, artifact.responsePath, artifact.responseSha, response.receivedAt, response.items.length, response.totalResults, response.estimatedCredits]);
      updateRun(database, run.id, {
        pending_page: null,
        pending_request_sha256: null,
        pending_started_at: null,
        next_page: next + 1,
        pages_fetched: Number(run.pages_fetched) + 1,
        jobs_seen: Number(run.jobs_seen) + response.items.length,
        estimated_credits: Number(run.estimated_credits) + response.estimatedCredits,
        reported_credits: Number(run.reported_credits) + response.estimatedCredits,
        expected_total_results: expectedTotal,
      });
    });
    hook(options, 'afterReceiptCommit', { runId: run.id, page: next, request, artifact });
    pageRows = pages(database, run.id);
    fetched += response.items.length;
    if (response.items.length === 0 || (expectedTotal !== null && fetched >= expectedTotal) || response.items.length < b.limit) break;
    next += 1;
    run = runRow(database, run.id);
  }
  tx(database, () => updateRun(database, run.id, { state: 'ready_to_commit', pending_page: null, pending_request_sha256: null, pending_started_at: null }));
  hook(options, 'beforeCommit', { runId: run.id, pagesFetched: pageRows.length });
  try {
    return await commit(database, root, runRow(database, run.id), ctx, b);
  } catch (error) {
    if (error instanceof CrashInjection) throw error;
    if (error instanceof SyncFailure) return resultFrom(runRow(database, run.id), ctx, { state: 'failed', failureClass: error.failureClass, reasonCode: error.reasonCode });
    return resultFrom(runRow(database, run.id), ctx, { state: 'failed', failureClass: 'database', reasonCode: 'commit_failed' });
  }
}

export async function previewSource(options = {}) {
  const ctx = context(options, false);
  const b = bounds(options);
  const request = await buildRequest(ctx, b, 0, 'preview');
  let raw;
  try {
    raw = await ctx.adapter.fetchPage({ request, mode: 'preview' });
  } catch {
    return check(validateSourceSyncResult, {
      schema: RESULT_SCHEMA,
      syncRunId: null,
      source: ctx.source,
      profile: ctx.profile,
      mode: 'preview',
      state: 'failed',
      startedAt: ctx.now,
      finishedAt: ctx.now,
      checkpointBefore: b.checkpoint,
      checkpointAfter: b.checkpoint,
      pagesFetched: 0,
      requestCount: 1,
      jobsSeen: 0,
      jobsInserted: 0,
      jobsUpdated: 0,
      jobsUnchanged: 0,
      dedupeGroupsTouched: 0,
      queueRowsInserted: 0,
      estimatedCredits: 0,
      reportedCredits: 0,
      failureClass: 'retryable',
      reasonCode: 'preview_failed',
    }, 'invalid_result');
  }
  let response;
  try {
    response = responseCheck(raw, request, ctx.now, null);
  } catch (error) {
    const reasonCode = error instanceof SyncFailure ? error.reasonCode : 'invalid_response';
    return check(validateSourceSyncResult, {
      schema: RESULT_SCHEMA,
      syncRunId: null,
      source: ctx.source,
      profile: ctx.profile,
      mode: 'preview',
      state: 'failed',
      startedAt: ctx.now,
      finishedAt: ctx.now,
      checkpointBefore: b.checkpoint,
      checkpointAfter: b.checkpoint,
      pagesFetched: 0,
      requestCount: 1,
      jobsSeen: 0,
      jobsInserted: 0,
      jobsUpdated: 0,
      jobsUnchanged: 0,
      dedupeGroupsTouched: 0,
      queueRowsInserted: 0,
      estimatedCredits: 0,
      reportedCredits: 0,
      failureClass: 'terminal',
      reasonCode,
    }, 'invalid_result');
  }
  return check(validateSourceSyncResult, {
    schema: RESULT_SCHEMA,
    syncRunId: null,
    source: ctx.source,
    profile: ctx.profile,
    mode: 'preview',
    state: 'previewed',
    startedAt: ctx.now,
    finishedAt: ctx.now,
    checkpointBefore: b.checkpoint,
    checkpointAfter: b.checkpoint,
    pagesFetched: 1,
    requestCount: 1,
    jobsSeen: response.items.length,
    jobsInserted: 0,
    jobsUpdated: 0,
    jobsUnchanged: 0,
    dedupeGroupsTouched: 0,
    queueRowsInserted: 0,
    estimatedCredits: response.estimatedCredits,
    reportedCredits: response.estimatedCredits,
    failureClass: null,
    reasonCode: null,
  }, 'invalid_result');
}

export async function syncSource(options = {}) {
  const ctx = context(options, true);
  const b = bounds(options);
  const root = await ensurePrivateRoot(options.privateRoot ?? options.artifactRoot);
  const opened = openDatabase(options);
  try {
    assertMigrated(opened.database);
    const durableRaw = checkpointRow(opened.database, ctx.source, ctx.profile)?.checkpoint ?? b.checkpoint;
    const durable = durableRaw === null ? null : time(String(durableRaw), 'invalid_checkpoint');
    if (options.checkpoint !== undefined && options.checkpoint !== null && durable !== time(String(options.checkpoint), 'invalid_checkpoint')) fail('checkpoint_mismatch');
    const effective = { ...b, checkpoint: durable };
    const run = tx(opened.database, () => createRun(opened.database, ctx, effective, root));
    return await continueRun(opened.database, root, run, ctx, effective, options);
  } finally {
    if (opened.owned) {
      try {
        opened.database.close();
      } catch {
        // Best effort close.
      }
    }
  }
}

function selectRun(database, options, ctx) {
  if (options.runId !== undefined && options.runId !== null) {
    const selected = runRow(database, options.runId);
    if (!selected) fail('run_not_found');
    return selected;
  }
  const selected = one(database, 'SELECT * FROM sync_runs WHERE source=? AND profile=? ORDER BY id DESC LIMIT 1', [ctx.source, ctx.profile]);
  if (!selected) fail('run_not_found');
  return selected;
}

export async function recoverSourceSync(options = {}) {
  const ctx = context(options, true);
  const supplied = bounds(options);
  const root = await ensurePrivateRoot(options.privateRoot ?? options.artifactRoot);
  const opened = openDatabase(options);
  try {
    assertMigrated(opened.database);
    const selected = selectRun(opened.database, options, ctx);
    if (selected.source !== ctx.source || selected.profile !== ctx.profile) fail('run_context_mismatch');
    if (TERMINAL.has(selected.state)) return resultFrom(selected, ctx);
    const storedCheckpoint = selected.checkpoint_before ?? supplied.checkpoint;
    const storedWindowEnd = selected.window_end_at ?? supplied.windowEnd;
    const effective = {
      ...supplied,
      checkpoint: storedCheckpoint === null ? null : time(String(storedCheckpoint), 'invalid_checkpoint'),
      windowEnd: time(String(storedWindowEnd), 'invalid_window_end'),
    };
    if (selected.pending_page !== null) return failRun(opened.database, selected, ctx, 'paid_ambiguous', 'paid_request_ambiguous', 'paid_ambiguous');
    if (selected.state === 'ready_to_commit') {
      try {
        return await commit(opened.database, root, selected, ctx, effective);
      } catch (error) {
        if (error instanceof CrashInjection) throw error;
        if (error instanceof SyncFailure) return resultFrom(runRow(opened.database, selected.id), ctx, { state: 'failed', failureClass: error.failureClass, reasonCode: error.reasonCode });
        return resultFrom(runRow(opened.database, selected.id), ctx, { state: 'failed', failureClass: 'database', reasonCode: 'commit_failed' });
      }
    }
    return await continueRun(opened.database, root, selected, ctx, effective, options);
  } finally {
    if (opened.owned) {
      try {
        opened.database.close();
      } catch {
        // Best effort close.
      }
    }
  }
}
