import { createHash } from 'node:crypto';
import { constants as fsConstants } from 'node:fs';
import { chmod, lstat, mkdir, open, unlink } from 'node:fs/promises';
import { dirname, join, relative, resolve, sep } from 'node:path';
import { assertIngestionSchema, openIngestionDatabase } from './database.mjs';
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
const NOFOLLOW = fsConstants.O_NOFOLLOW ?? 0;
const MAX_PRIVATE_ARTIFACT_BYTES = 64 * 1024 * 1024;
const CREDIT_SNAPSHOT_KEYS = Object.freeze(['observedAt', 'periodStart', 'periodEnd', 'consumedCredits']);

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
    return { database: openIngestionDatabase(path), owned: true };
  } catch {
    fail('database_open_failed', 'database');
  }
}

function execute(database, sql, values = []) {
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
  try {
    assertIngestionSchema(database);
  } catch {
    fail('database_schema_required', 'database');
  }
}

function appColumns(database) {
  if (!hasTable(database, 'application_jobs')) return null;
  return new Set(columns(database, 'application_jobs').map((column) => column.name));
}

function context(options, paid) {
  const adapter = options.adapter;
  if (!adapter || typeof adapter.buildRequest !== 'function' || typeof adapter.fetchPage !== 'function' || typeof adapter.normalizeJob !== 'function') fail('adapter_required');
  const requiresCreditReconciliation = adapter.requiresCreditReconciliation ?? false;
  if (typeof requiresCreditReconciliation !== 'boolean') fail('adapter_accounting_contract');
  if (requiresCreditReconciliation && typeof adapter.readCreditUsage !== 'function') fail('adapter_accounting_contract');
  const source = options.source ?? adapter.source;
  if (typeof source !== 'string' || !/^[a-z][a-z0-9_-]{0,63}$/u.test(source) || adapter.source !== source) fail('source_required');
  const profile = options.profile;
  if (typeof profile !== 'string' || !/^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$/u.test(profile)) fail('profile_required');
  const now = time(options.now, 'invalid_now');
  if (paid && options.paidAuthorization !== true) fail('paid_authorization_required', 'authorization');
  return { adapter, source, profile, now, requiresCreditReconciliation };
}

function checkpointValue(adapter, value) {
  if (value === null || value === undefined) return null;
  let normalized = value;
  if (typeof adapter.normalizeCheckpoint === 'function') {
    try {
      normalized = adapter.normalizeCheckpoint(value);
    } catch {
      fail('invalid_checkpoint');
    }
  }
  return time(normalized, 'invalid_checkpoint');
}

function bounds(options, adapter) {
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
  const start = checkpointValue(adapter, checkpoint);
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
  if (!info.isDirectory() || (info.mode & 0o077) !== 0 || (typeof process.getuid === 'function' && info.uid !== process.getuid())) fail('private_root_not_private', 'filesystem');
  return root;
}

async function privateDir(path) {
  try {
    await mkdir(path, { recursive: true, mode: DIR_MODE });
    const info = await lstat(path);
    if (!info.isDirectory() || (info.mode & 0o077) !== 0 || (typeof process.getuid === 'function' && info.uid !== process.getuid())) fail('private_directory_not_private', 'filesystem');
    if ((info.mode & 0o777) !== DIR_MODE) await chmod(path, DIR_MODE);
  } catch (error) {
    if (error instanceof SyncFailure) throw error;
    fail('private_directory_failed', 'filesystem');
  }
}

async function privateFile(path, text) {
  await privateDir(dirname(path));
  let handle;
  let created = false;
  try {
    handle = await open(path, 'wx', FILE_MODE);
    created = true;
    await handle.writeFile(text, 'utf8');
    await handle.sync();
    await handle.chmod(FILE_MODE);
    await handle.close();
    handle = null;
  } catch {
    try {
      await handle?.close();
    } catch {
      // Best effort close.
    }
    if (created) {
      try {
        await unlink(path);
      } catch {
        // Best effort cleanup.
      }
    }
    fail('private_artifact_write_failed', 'filesystem');
  }
}

async function readPrivate(path) {
  let handle;
  try {
    handle = await open(path, fsConstants.O_RDONLY | NOFOLLOW);
  } catch {
    fail('private_artifact_missing', 'filesystem');
  }
  try {
    const info = await handle.stat();
    const linked = await lstat(path);
    if (
      !info.isFile()
      || !linked.isFile()
      || info.dev !== linked.dev
      || info.ino !== linked.ino
      || info.size > MAX_PRIVATE_ARTIFACT_BYTES
      || (info.mode & 0o077) !== 0
      || (typeof process.getuid === 'function' && info.uid !== process.getuid())
    ) fail('private_artifact_not_private', 'filesystem');
    const chunks = [];
    let total = 0;
    for await (const chunk of handle.createReadStream({ autoClose: false })) {
      total += chunk.byteLength;
      if (total > MAX_PRIVATE_ARTIFACT_BYTES) fail('private_artifact_not_private', 'filesystem');
      chunks.push(chunk);
    }
    return Buffer.concat(chunks, total).toString('utf8');
  } catch (error) {
    if (error instanceof SyncFailure) throw error;
    fail('private_artifact_read_failed', 'filesystem');
  } finally {
    try {
      await handle?.close();
    } catch {
      // Best effort close.
    }
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

function responseCheck(value, request, _now, expectedTotal) {
  if (!object(value) || !Array.isArray(value.items)) fail('invalid_response');
  digest(value.requestSha256, 'invalid_response');
  if (value.requestSha256 !== request.requestSha256) fail('request_mismatch');
  if (value.items.length > request.limit || value.items.some((item) => !object(item))) fail('invalid_response');
  const total = value.totalResults === undefined || value.totalResults === null ? expectedTotal : value.totalResults;
  if (total !== null) integer(total, 'invalid_response');
  if (expectedTotal !== null && total !== expectedTotal) fail('totals_changed');
  if (total !== null && value.items.length > total) fail('total_bounds_exceeded');
  const receivedAt = time(value.receivedAt, 'invalid_response');
  if (receivedAt > new Date().toISOString()) fail('response_time_out_of_bounds');
  const estimatedCredits = value.estimatedCredits ?? 0;
  const reportedCredits = value.reportedCredits ?? null;
  if (typeof estimatedCredits !== 'number' || !Number.isFinite(estimatedCredits) || estimatedCredits < 0) fail('invalid_response');
  if (reportedCredits !== null && (typeof reportedCredits !== 'number' || !Number.isFinite(reportedCredits) || reportedCredits < 0)) fail('invalid_response');
  return { items: value.items, totalResults: total, receivedAt, estimatedCredits, reportedCredits };
}

function paidRequestLimit(b, fetched) {
  if (b.maxItems === null) return b.limit;
  const remaining = b.maxItems - fetched;
  return remaining < b.limit ? 0 : b.limit;
}

async function buildRequest(ctx, b, page, mode, limit = b.limit) {
  try {
    const request = await ctx.adapter.buildRequest({ profile: ctx.profile, mode, page, limit, checkpoint: b.checkpoint, windowEnd: b.windowEnd, includeTotals: page === 0 });
    return requestCheck(request, page, limit);
  } catch (error) {
    if (error instanceof SyncFailure) throw error;
    fail('request_build_failed');
  }
}

function providerFailure(error, mode) {
  if (error instanceof SyncFailure) return error;
  const code = typeof error?.code === 'string' ? error.code : null;
  if (code === 'authentication') return new SyncFailure('provider_authentication', 'authentication');
  if (code === 'account_health') return new SyncFailure('provider_account_health', 'account_health');
  if (code === 'terminal_validation') return new SyncFailure('provider_terminal_validation', 'terminal');
  if (code === 'paid_authorization') return new SyncFailure('paid_authorization_required', 'terminal');
  if (code === 'retryable_preview') return new SyncFailure('preview_retry_exhausted', 'retryable');
  if (code === 'paid_ambiguous' || mode === 'paid') return new SyncFailure('paid_request_ambiguous', 'paid_ambiguous');
  return new SyncFailure('preview_failed', 'retryable');
}

async function fetchPage(ctx, request, mode) {
  try {
    return await ctx.adapter.fetchPage({ request, mode });
  } catch (error) {
    throw providerFailure(error, mode);
  }
}
function validateCreditSnapshot(value, { allowAfterPeriod = false } = {}) {
  if (!object(value)) fail('credit_usage_invalid');
  const keys = Object.keys(value);
  if (keys.length !== CREDIT_SNAPSHOT_KEYS.length || CREDIT_SNAPSHOT_KEYS.some((key) => !Object.hasOwn(value, key))) fail('credit_usage_invalid');
  const observedAt = time(value.observedAt, 'credit_usage_invalid');
  const periodStart = time(value.periodStart, 'credit_usage_invalid');
  const periodEnd = time(value.periodEnd, 'credit_usage_invalid');
  if (periodStart > periodEnd || periodStart > observedAt || (!allowAfterPeriod && observedAt > periodEnd)) fail('credit_usage_invalid');
  const consumedCredits = integer(value.consumedCredits, 'credit_usage_invalid');
  return { observedAt, periodStart, periodEnd, consumedCredits };
}

async function readCreditSnapshot(ctx, mode, period = null) {
  try {
    const request = period === null ? undefined : {
      periodStart: period.periodStart,
      periodEnd: period.periodEnd,
    };
    return validateCreditSnapshot(await ctx.adapter.readCreditUsage(request), {
      allowAfterPeriod: period !== null,
    });
  } catch (error) {
    if (error instanceof SyncFailure) throw error;
    throw providerFailure(error, mode);
  }
}

function creditAuditRow(database, runId) {
  return one(database, 'SELECT * FROM source_credit_audits WHERE sync_run_id=?', [runId]);
}

async function startCreditAudit(database, run, ctx) {
  if (!ctx.requiresCreditReconciliation) return null;
  const existing = creditAuditRow(database, run.id);
  if (existing === null || existing.source !== ctx.source) fail('credit_audit_missing', 'paid_ambiguous');
  return existing;
}

function makeCreditAuditUnavailable(database, runId, reasonCode) {
  tx(database, () => execute(database, `UPDATE source_credit_audits
    SET state='unavailable',reason_code=?
    WHERE sync_run_id=? AND state='pending'`, [reasonCode, runId]));
}

async function reconcileCreditAudit(database, run, ctx) {
  if (!ctx.requiresCreditReconciliation) return undefined;
  const audit = creditAuditRow(database, run.id);
  if (audit === null || audit.source !== ctx.source) fail('credit_audit_missing', 'paid_ambiguous');
  if (audit.state === 'reconciled') return integer(Number(audit.reported_credits), 'credit_audit_invalid');
  if (audit.state !== 'pending') fail('credit_reconciliation_unavailable', 'paid_ambiguous');
  const outsidePeriod = one(database, `SELECT 1 FROM source_sync_pages
    WHERE sync_run_id=? AND (received_at < ? OR received_at > ?) LIMIT 1`, [
    run.id,
    audit.period_start,
    audit.period_end,
  ]);
  if (outsidePeriod !== null) {
    makeCreditAuditUnavailable(database, run.id, 'credit_period_spanned');
    fail('credit_period_spanned', 'paid_ambiguous');
  }
  let snapshot;
  try {
    snapshot = await readCreditSnapshot(ctx, 'paid', {
      periodStart: audit.period_start,
      periodEnd: audit.period_end,
    });
  } catch (error) {
    makeCreditAuditUnavailable(database, run.id, 'credit_usage_unavailable');
    if (error instanceof SyncFailure && error.failureClass === 'database') throw error;
    fail('credit_reconciliation_unavailable', 'paid_ambiguous');
  }
  if (snapshot.periodStart !== audit.period_start || snapshot.periodEnd !== audit.period_end) {
    makeCreditAuditUnavailable(database, run.id, 'credit_period_changed');
    fail('credit_period_changed', 'paid_ambiguous');
  }
  if (snapshot.observedAt < audit.observed_before_at) {
    makeCreditAuditUnavailable(database, run.id, 'credit_observation_regressed');
    fail('credit_observation_regressed', 'paid_ambiguous');
  }
  const before = integer(Number(audit.credits_before), 'credit_audit_invalid');
  if (snapshot.consumedCredits < before) {
    makeCreditAuditUnavailable(database, run.id, 'credit_usage_regressed');
    fail('credit_usage_regressed', 'paid_ambiguous');
  }
  const reportedCredits = snapshot.consumedCredits - before;
  tx(database, () => execute(database, `UPDATE source_credit_audits
    SET observed_after_at=?,credits_after=?,reported_credits=?,state='reconciled',reason_code=NULL
    WHERE sync_run_id=? AND state='pending'`, [
    snapshot.observedAt,
    snapshot.consumedCredits,
    reportedCredits,
    run.id,
  ]));
  return reportedCredits;
}

async function settleCreditAuditAfterAmbiguity(database, run, ctx) {
  if (!ctx.requiresCreditReconciliation) return;
  try {
    const reportedCredits = await reconcileCreditAudit(database, run, ctx);
    updateRun(database, run.id, { reported_credits: reportedCredits });
  } catch (error) {
    if (!(error instanceof SyncFailure) || error.failureClass !== 'paid_ambiguous') throw error;
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

function createRun(database, ctx, b, root, creditBaseline = null) {
  if (ctx.requiresCreditReconciliation) {
    const active = one(database, "SELECT 1 FROM sync_runs WHERE source=? AND state IN ('fetching','ready_to_commit') LIMIT 1", [ctx.source]);
    if (active !== null) fail('source_sync_active', 'retryable');
    if (creditBaseline === null) fail('credit_audit_missing', 'database');
  }
  const result = execute(database, `INSERT INTO sync_runs (
    source,profile,mode,state,started_at,finished_at,window_end_at,checkpoint_before,
    checkpoint_after,artifact_dir,request_count,pages_fetched,jobs_seen,jobs_inserted,
    jobs_updated,jobs_unchanged,dedupe_groups_touched,queue_rows_inserted,estimated_credits,
    reported_credits,pending_page,pending_request_sha256,pending_started_at,next_page,
    expected_total_results,failure_class,reason_code,result_sha256,page_limit,max_pages,max_items
  ) VALUES (${new Array(31).fill('?').join(',')})`, [
    ctx.source, ctx.profile, 'paid', 'fetching', ctx.now, null, b.windowEnd, b.checkpoint,
    null, `source-sync/${ctx.source}`, 0, 0, 0, 0, 0, 0, 0, 0, 0, null,
    null, null, null, 0, null, null, null, null, b.limit, b.maxPages, b.maxItems,
  ]);
  const id = idOf(result);
  const row = runRow(database, id);
  if (!row) fail('database_write_failed', 'database');
  const rootDirectory = join(root, 'source-sync', ctx.source, String(row.id));
  updateRun(database, id, { artifact_dir: `source-sync/${ctx.source}/${row.id}` });
  if (creditBaseline !== null) {
    execute(database, `INSERT INTO source_credit_audits (
      sync_run_id,source,period_start,period_end,observed_before_at,credits_before,
      observed_after_at,credits_after,reported_credits,state,reason_code
    ) VALUES (?,?,?,?,?,?,NULL,NULL,NULL,'pending',NULL)`, [
      id,
      ctx.source,
      creditBaseline.periodStart,
      creditBaseline.periodEnd,
      creditBaseline.observedAt,
      creditBaseline.consumedCredits,
    ]);
  }
  return { ...row, artifact_dir: `source-sync/${ctx.source}/${row.id}`, rootDirectory };
}

function updateRun(database, id, values) {
  const keys = Object.keys(values);
  if (keys.length === 0) return;
  execute(database, `UPDATE sync_runs SET ${keys.map((key) => `${q(key)}=?`).join(',')} WHERE id=?`, [...keys.map((key) => values[key]), id]);
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
    reportedCredits: overrides.reportedCredits !== undefined
      ? overrides.reportedCredits
      : row?.reported_credits === null || row?.reported_credits === undefined ? null : Number(row.reported_credits),
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
    reportedCredits: response.reportedCredits,
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
  if (receipt.requestSha256 !== page.request_sha256 || receipt.itemCount !== page.item_count || receipt.items.length !== page.item_count || receipt.totalResults !== page.total_results || receipt.receivedAt !== page.received_at || receipt.estimatedCredits !== page.estimated_credits || receipt.reportedCredits !== page.reported_credits) fail('private_artifact_metadata_mismatch', 'filesystem');
  const indexes = new Set();
  const items = [];
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
  const fallbackApplicationUrls = new Set();
  for (const page of pages(database, run.id)) {
    for (const item of (await loadPage(root, page)).items) {
      const rawPayloadPath = abs(root, item.rawPayloadPath);
      let normalized;
      try {
        normalized = await ctx.adapter.normalizeJob(item.raw, { observedAt: ctx.now, rawPayloadPath, rawPayloadSha256: item.rawPayloadSha256 });
      } catch {
        fail('normalization_failed');
      }
      normalized = check(validateNormalizedJob, normalized, 'normalization_failed');
      if (normalized.source !== ctx.source || normalized.rawPayloadPath !== rawPayloadPath || normalized.rawPayloadSha256 !== item.rawPayloadSha256) fail('normalization_artifact_mismatch');
      if ((b.checkpoint !== null && normalized.discoveredAt < b.checkpoint) || normalized.discoveredAt > b.windowEnd) {
        fail('normalization_time_bounds');
      }
      if (normalized.sourceJobId !== null) {
        const id = String(normalized.sourceJobId);
        if (sourceIds.has(id)) fail('duplicate_job_identity');
        sourceIds.add(id);
      }
      if (normalized.sourceJobId === null && normalized.canonicalApplicationUrl !== null) {
        if (fallbackApplicationUrls.has(normalized.canonicalApplicationUrl)) fail('duplicate_job_identity');
        fallbackApplicationUrls.add(normalized.canonicalApplicationUrl);
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
    execute(database, 'UPDATE dedupe_groups SET review_required=?,updated_at=? WHERE id=?', [job.dedupeReviewRequired ? 1 : 0, now, existing.id]);
    return existing.id;
  }
  return idOf(execute(database, 'INSERT INTO dedupe_groups (identity_kind,identity_key,review_required,created_at,updated_at) VALUES (?,?,?,?,?)', [job.dedupeIdentityKind, job.dedupeIdentityKey, job.dedupeReviewRequired ? 1 : 0, now, now]));
}

function existingJob(database, job) {
  if (job.sourceJobId !== null) {
    return one(database, 'SELECT * FROM jobs WHERE source=? AND source_job_id=?', [job.source, job.sourceJobId]);
  }
  if (job.canonicalApplicationUrl === null) return null;
  return one(database, 'SELECT * FROM jobs WHERE source=? AND source_job_id IS NULL AND canonical_application_url=?', [job.source, job.canonicalApplicationUrl]);
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
  const storedJob = existing && job.sourceJobId === null && existing.source_job_id !== null
    ? { ...job, sourceJobId: existing.source_job_id }
    : job;
  const params = jobParams(storedJob, groupId, now, existing);
  if (existing) {
    execute(database, `UPDATE jobs SET source=?,source_job_id=?,canonical_listing_url=?,canonical_application_url=?,ats_kind=?,ats_identifier=?,title=?,company=?,location=?,workplace_type=?,employment_types_json=?,description=?,description_sha256=?,source_posted_at=?,source_updated_at=?,discovered_at=?,first_seen_at=?,last_seen_at=?,availability_state=?,freshness_state=?,eligibility_state=?,eligibility_reason_codes_json=?,priority=?,dedupe_group_id=?,raw_payload_path=?,raw_payload_sha256=? WHERE id=?`, [...params.slice(0, 26), existing.id]);
    return { id: existing.id, inserted: false, unchanged: existing.raw_payload_sha256 === job.rawPayloadSha256 && existing.discovered_at === job.discoveredAt };
  }
  const result = execute(database, `INSERT INTO jobs (source,source_job_id,canonical_listing_url,canonical_application_url,ats_kind,ats_identifier,title,company,location,workplace_type,employment_types_json,description,description_sha256,source_posted_at,source_updated_at,discovered_at,first_seen_at,last_seen_at,availability_state,freshness_state,eligibility_state,eligibility_reason_codes_json,priority,dedupe_group_id,raw_payload_path,raw_payload_sha256) VALUES (${new Array(26).fill('?').join(',')})`, params.slice(0, 26));
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

function boundGroup(database, groupId) {
  const names = appColumns(database);
  if (!names?.has('dedupe_group_id')) return false;
  return one(database, 'SELECT 1 FROM application_jobs WHERE dedupe_group_id=?', [groupId]) !== null;
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
    dedupe_group_id: groupId,
    source_rowid: jobId,
    source_job_id: job.sourceJobId ?? job.canonicalApplicationUrl,
    source_table: 'jobs',
    source_db: 'ingestion',
    application_url: job.canonicalApplicationUrl,
    eligibility_tier: tier,
    verification_reason: 'source_sync',
    source_posted_at: job.sourcePostedAt,
    source_last_seen_at: now,
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
  execute(database, `INSERT INTO application_jobs (${namesToUse.map(q).join(',')}) VALUES (${namesToUse.map(() => '?').join(',')})`, values);
  return true;
}

function refreshQueuedApplication(database, rows, job, jobId, groupId, now) {
  const byUrl = rows.find((row) => row.application_url === job.canonicalApplicationUrl) ?? null;
  const byGroup = one(database, 'SELECT * FROM application_jobs WHERE dedupe_group_id=?', [groupId]);
  const keeper = byGroup ?? byUrl ?? rows.find((row) => row.status === 'queued') ?? null;
  for (const row of rows) {
    if (row.status === 'queued' && keeper !== null && row.id !== keeper.id) {
      execute(database, "UPDATE application_jobs SET status='skipped',status_reason='deduplicated',dedupe_group_id=NULL WHERE id=? AND status='queued'", [row.id]);
    }
  }
  if (keeper === null || keeper.status !== 'queued' || !rows.some((row) => row.id === keeper.id)) return false;
  execute(database, `UPDATE application_jobs SET
    source_rowid=?,source_job_id=?,application_url=?,eligibility_tier='active_verified',
    verification_reason='source_sync',source_posted_at=?,source_last_seen_at=?,dedupe_group_id=?
    WHERE id=? AND status='queued'`, [
    jobId,
    job.sourceJobId ?? job.canonicalApplicationUrl,
    job.canonicalApplicationUrl,
    job.sourcePostedAt,
    now,
    groupId,
    keeper.id,
  ]);
  return true;
}

function revokeQueuedApplication(database, job, jobId, groupId, now) {
  const current = appRows(database, jobId, job);
  const status = job.availabilityState === 'closed' ? 'closed' : 'skipped';
  const reason = job.availabilityState === 'closed' ? 'source_closed' : 'source_ineligible';
  const tier = job.freshnessState === 'stale' ? 'unverified_stale' : 'backfill_only';
  for (const row of current) {
    if (row.status !== 'queued') continue;
    execute(database, `UPDATE application_jobs SET
      status=?,status_reason=?,source_rowid=?,source_job_id=?,application_url=?,
      eligibility_tier=?,verification_reason='source_sync',source_posted_at=?,
      source_last_seen_at=?,dedupe_group_id=?
      WHERE id=? AND status='queued'`, [
      status,
      reason,
      jobId,
      job.sourceJobId ?? job.canonicalApplicationUrl,
      job.canonicalApplicationUrl,
      tier,
      job.sourcePostedAt,
      now,
      groupId,
      row.id,
    ]);
  }
}

function promote(database, job, jobId, groupId, now, groups) {
  if (job.eligibilityState !== 'eligible') {
    revokeQueuedApplication(database, job, jobId, groupId, now);
    return false;
  }
  const current = appRows(database, jobId, job);
  if (current.length > 0) {
    if (refreshQueuedApplication(database, current, job, jobId, groupId, now)) groups.add(groupId);
    return false;
  }
  if (groups.has(groupId) || boundGroup(database, groupId)) return false;
  const added = addQueue(database, job, jobId, groupId, now);
  if (added) groups.add(groupId);
  return added;
}

function observe(database, runIdValue, jobId, job, now) {
  execute(database, 'INSERT INTO source_observations (sync_run_id,job_id,source,source_job_id,observed_at,raw_payload_path,raw_payload_sha256,normalized_job_sha256) VALUES (?,?,?,?,?,?,?,?)', [runIdValue, jobId, job.source, job.sourceJobId, now, job.rawPayloadPath, job.rawPayloadSha256, canonicalDigest(job)]);
}

function checkpoint(database, ctx, runIdValue, after, now) {
  const existing = checkpointRow(database, ctx.source, ctx.profile);
  const durable = maxTime(
    checkpointValue(ctx.adapter, existing?.checkpoint),
    checkpointValue(ctx.adapter, after),
  );
  if (existing) execute(database, 'UPDATE source_checkpoints SET checkpoint=?,last_sync_run_id=?,updated_at=?,revision=revision+1 WHERE source=? AND profile=?', [durable, runIdValue, now, ctx.source, ctx.profile]);
  else execute(database, 'INSERT INTO source_checkpoints (source,profile,checkpoint,last_sync_run_id,updated_at,revision) VALUES (?,?,?,?,?,?)', [ctx.source, ctx.profile, durable, runIdValue, now, 1]);
  return durable;
}

function pageAggregate(rows) {
  let jobs = 0;
  let estimatedCredits = 0;
  let reportedCredits = 0;
  let reportedComplete = true;
  let total = null;
  for (const row of rows) {
    jobs += Number(row.item_count) || 0;
    estimatedCredits += Number(row.estimated_credits) || 0;
    if (row.reported_credits === null) reportedComplete = false;
    else reportedCredits += Number(row.reported_credits) || 0;
    if (row.total_results !== null) {
      if (total !== null && total !== Number(row.total_results)) fail('totals_changed');
      total = Number(row.total_results);
    }
  }
  return { jobs, estimatedCredits, reportedCredits: reportedComplete ? reportedCredits : null, total };
}
function pageContentDigest(items) {
  return canonicalDigest(items.map((item) => item.rawPayloadSha256));
}

async function commit(database, root, run, ctx, b) {
  const jobs = await normalizePages(root, database, run, ctx, b);
  const pageRows = pages(database, run.id);
  const aggregate = pageAggregate(pageRows);
  const reportedCredits = ctx.requiresCreditReconciliation
    ? await reconcileCreditAudit(database, run, ctx)
    : aggregate.reportedCredits;
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
      estimatedCredits: aggregate.estimatedCredits,
      reportedCredits,
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
      estimated_credits: aggregate.estimatedCredits,
      reported_credits: reportedCredits,
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

async function settleCreditAuditOnFailure(database, run, ctx) {
  if (!ctx.requiresCreditReconciliation) return null;
  const audit = creditAuditRow(database, run.id);
  const current = runRow(database, run.id);
  const hadPaidRequest = Number(current.request_count) > 0;
  if (audit === null) return null;
  if (audit.state === 'reconciled') return 'reconciled';
  if (audit.state === 'unavailable') return hadPaidRequest ? 'unavailable' : null;
  if (hadPaidRequest) {
    try {
      const reportedCredits = await reconcileCreditAudit(database, current, ctx);
      updateRun(database, run.id, { reported_credits: reportedCredits });
      return 'reconciled';
    } catch (error) {
      if (error instanceof SyncFailure && error.failureClass === 'database') throw error;
    }
  }
  if (creditAuditRow(database, run.id)?.state === 'pending') {
    makeCreditAuditUnavailable(database, run.id, 'run_failed_unreconciled');
  }
  return hadPaidRequest ? 'unavailable' : null;
}

async function failRun(database, run, ctx, failureClass, reasonCode, state = 'failed') {
  const creditState = await settleCreditAuditOnFailure(database, run, ctx);
  if (state !== 'paid_ambiguous' && creditState === 'unavailable') {
    state = 'paid_ambiguous';
    failureClass = 'paid_ambiguous';
    reasonCode = 'credit_reconciliation_unavailable';
  }
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
  try {
    await startCreditAudit(database, run, ctx);
  } catch (error) {
    if (error instanceof SyncFailure) {
      const state = error.failureClass === 'paid_ambiguous' ? 'paid_ambiguous' : 'failed';
      return failRun(database, run, ctx, error.failureClass, error.reasonCode, state);
    }
    throw error;
  }
  let pageRows = pages(database, run.id);
  const first = pageAggregate(pageRows);
  let fetched = first.jobs;
  let expectedTotal = first.total;
  let reportedCredits = first.reportedCredits;
  let next = Number(run.next_page) || pageRows.length;
  const seenPageContent = new Set();
  try {
    for (const page of pageRows) seenPageContent.add(pageContentDigest((await loadPage(root, page)).items));
  } catch (error) {
    if (error instanceof SyncFailure) return failRun(database, run, ctx, error.failureClass, error.reasonCode);
    throw error;
  }
  if (run.pending_page !== null) {
    await settleCreditAuditAfterAmbiguity(database, run, ctx);
    return failRun(database, run, ctx, 'paid_ambiguous', 'paid_request_ambiguous', 'paid_ambiguous');
  }
  if (b.maxItems !== null && fetched > b.maxItems) return failRun(database, run, ctx, 'terminal', 'item_bounds_exceeded');
  let completeFromReceipts = false;
  const lastReceipt = pageRows.at(-1) ?? null;
  if (lastReceipt !== null) {
    const lastItemCount = Number(lastReceipt.item_count);
    const lastRequestLimit = paidRequestLimit(b, fetched - lastItemCount);
    if (lastRequestLimit === 0) return failRun(database, run, ctx, 'terminal', 'item_bounds_exceeded');
    if (expectedTotal !== null) {
      if (fetched > expectedTotal) return failRun(database, run, ctx, 'terminal', 'pagination_total_mismatch');
      if (fetched === expectedTotal) completeFromReceipts = true;
      else if (lastItemCount < lastRequestLimit) return failRun(database, run, ctx, 'terminal', 'pagination_total_mismatch');
    } else if (lastItemCount < lastRequestLimit) {
      completeFromReceipts = true;
    }
  }
  while (!completeFromReceipts) {
    if (next >= b.maxPages) return failRun(database, run, ctx, 'terminal', 'page_bounds_exceeded');
    if (pageRows.some((page) => Number(page.page_number) === next)) {
      next += 1;
      continue;
    }
    const limit = paidRequestLimit(b, fetched);
    if (limit === 0) return failRun(database, run, ctx, 'terminal', 'item_bounds_exceeded');
    let request;
    try {
      request = await buildRequest(ctx, b, next, 'paid', limit);
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
      if (error instanceof SyncFailure) {
        if (error.failureClass === 'paid_ambiguous') await settleCreditAuditAfterAmbiguity(database, run, ctx);
        const state = error.failureClass === 'paid_ambiguous' ? 'paid_ambiguous' : 'failed';
        return failRun(database, run, ctx, error.failureClass, error.reasonCode, state);
      }
      throw error;
    }
    let response;
    try {
      response = responseCheck(raw, request, ctx.now, expectedTotal);
    } catch (error) {
      if (error instanceof SyncFailure) {
        await settleCreditAuditAfterAmbiguity(database, run, ctx);
        return failRun(database, run, ctx, 'paid_ambiguous', 'paid_response_invalid', 'paid_ambiguous');
      }
      throw error;
    }
    if (expectedTotal === null) expectedTotal = response.totalResults;
    const exceedsItemBound = b.maxItems !== null && fetched + response.items.length > b.maxItems;
    let artifact;
    try {
      artifact = await publishPage(root, ctx.source, run.id, next, request, response);
    } catch (error) {
      if (error instanceof SyncFailure) {
        await settleCreditAuditAfterAmbiguity(database, run, ctx);
        return failRun(database, run, ctx, error.failureClass, error.reasonCode, 'paid_ambiguous');
      }
      throw error;
    }
    hook(options, 'afterReceipt', { runId: run.id, page: next, request, artifact });
    reportedCredits = reportedCredits === null || response.reportedCredits === null
      ? null
      : reportedCredits + response.reportedCredits;
    tx(database, () => {
      execute(database, 'INSERT INTO source_sync_pages (sync_run_id,page_number,request_sha256,response_path,response_sha256,received_at,item_count,total_results,estimated_credits,reported_credits) VALUES (?,?,?,?,?,?,?,?,?,?)', [run.id, next, request.requestSha256, artifact.responsePath, artifact.responseSha, response.receivedAt, response.items.length, response.totalResults, response.estimatedCredits, response.reportedCredits]);
      updateRun(database, run.id, {
        pending_page: null,
        pending_request_sha256: null,
        pending_started_at: null,
        next_page: next + 1,
        pages_fetched: Number(run.pages_fetched) + 1,
        jobs_seen: Number(run.jobs_seen) + response.items.length,
        estimated_credits: Number(run.estimated_credits) + response.estimatedCredits,
        reported_credits: reportedCredits,
        expected_total_results: expectedTotal,
      });
    });
    hook(options, 'afterReceiptCommit', { runId: run.id, page: next, request, artifact });
    const contentDigest = pageContentDigest(artifact.items);
    if (response.items.length > 0 && seenPageContent.has(contentDigest)) return failRun(database, run, ctx, 'terminal', 'pagination_repeated_page');
    seenPageContent.add(contentDigest);
    if (exceedsItemBound) return failRun(database, run, ctx, 'terminal', 'item_bounds_exceeded');
    pageRows = pages(database, run.id);
    fetched += response.items.length;
    if (expectedTotal !== null && fetched > expectedTotal) return failRun(database, run, ctx, 'terminal', 'pagination_total_mismatch');
    if (response.items.length < request.limit && expectedTotal !== null && fetched < expectedTotal) return failRun(database, run, ctx, 'terminal', 'pagination_total_mismatch');
    if (response.items.length === 0 || (expectedTotal !== null && fetched === expectedTotal) || response.items.length < request.limit) break;
    next += 1;
    run = runRow(database, run.id);
  }
  tx(database, () => updateRun(database, run.id, { state: 'ready_to_commit', pending_page: null, pending_request_sha256: null, pending_started_at: null }));
  hook(options, 'beforeCommit', { runId: run.id, pagesFetched: pageRows.length });
  try {
    return await commit(database, root, runRow(database, run.id), ctx, b);
  } catch (error) {
    if (error instanceof CrashInjection) throw error;
    if (error instanceof SyncFailure) {
      const state = error.failureClass === 'paid_ambiguous' ? 'paid_ambiguous' : 'failed';
      return failRun(database, runRow(database, run.id), ctx, error.failureClass, error.reasonCode, state);
    }
    throw error;
  }
}

export async function previewSource(options = {}) {
  const ctx = context(options, false);
  const b = bounds(options, ctx.adapter);
  const request = await buildRequest(ctx, { ...b, limit: 1, maxPages: 1 }, 0, 'preview');
  let raw;
  let previewFailure = null;
  try {
    raw = await ctx.adapter.fetchPage({ request, mode: 'preview' });
  } catch (error) {
    previewFailure = providerFailure(error, 'preview');
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
      failureClass: resultFailureClass(previewFailure.failureClass, 'failed'),
      reasonCode: previewFailure.reasonCode,
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
    reportedCredits: response.reportedCredits,
    failureClass: null,
    reasonCode: null,
  }, 'invalid_result');
}

export async function syncSource(options = {}) {
  const ctx = context(options, true);
  const b = bounds(options, ctx.adapter);
  const root = await ensurePrivateRoot(options.privateRoot ?? options.artifactRoot);
  const opened = openDatabase(options);
  try {
    assertMigrated(opened.database);
    const durableRaw = checkpointRow(opened.database, ctx.source, ctx.profile)?.checkpoint ?? b.checkpoint;
    const durable = checkpointValue(ctx.adapter, durableRaw);
    if (options.checkpoint !== undefined && options.checkpoint !== null && durable !== checkpointValue(ctx.adapter, options.checkpoint)) fail('checkpoint_mismatch');
    const effective = {
      ...b,
      limit: b.maxItems === null ? b.limit : Math.min(b.limit, b.maxItems),
      checkpoint: durable,
    };
    let run;
    if (ctx.requiresCreditReconciliation) {
      opened.database.exec('BEGIN IMMEDIATE;');
      try {
        const active = one(opened.database, "SELECT 1 FROM sync_runs WHERE source=? AND state IN ('fetching','ready_to_commit') LIMIT 1", [ctx.source]);
        if (active !== null) fail('source_sync_active', 'retryable');
        let creditBaseline;
        try {
          creditBaseline = await readCreditSnapshot(ctx, 'preview');
        } catch (error) {
          opened.database.exec('ROLLBACK;');
          if (error instanceof SyncFailure) {
            const failureClass = resultFailureClass(error.failureClass, 'failed');
            return resultFrom(null, ctx, {
              mode: 'paid',
              state: 'failed',
              finishedAt: ctx.now,
              failureClass,
              reasonCode: error.reasonCode,
            });
          }
          throw error;
        }
        run = createRun(opened.database, ctx, effective, root, creditBaseline);
        opened.database.exec('COMMIT;');
      } catch (error) {
        try { opened.database.exec('ROLLBACK;'); } catch {}
        throw error;
      }
    } else {
      run = tx(opened.database, () => createRun(opened.database, ctx, effective, root));
    }
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
  const supplied = bounds(options, ctx.adapter);
  const root = await ensurePrivateRoot(options.privateRoot ?? options.artifactRoot);
  const opened = openDatabase(options);
  try {
    assertMigrated(opened.database);
    const selected = selectRun(opened.database, options, ctx);
    if (selected.source !== ctx.source || selected.profile !== ctx.profile) fail('run_context_mismatch');
    if (TERMINAL.has(selected.state)) return resultFrom(selected, ctx);
    const storedCheckpoint = selected.checkpoint_before ?? supplied.checkpoint;
    const storedWindowEnd = selected.window_end_at ?? supplied.windowEnd;
    if (selected.page_limit === null || selected.max_pages === null) fail('run_bounds_missing', 'database');
    const effective = {
      ...supplied,
      limit: integer(Number(selected.page_limit), 'stored_page_limit_invalid', 1, 100),
      maxPages: integer(Number(selected.max_pages), 'stored_max_pages_invalid', 1, 1000),
      maxItems: selected.max_items === null
        ? null
        : integer(Number(selected.max_items), 'stored_max_items_invalid', 1, 1_000_000),
      checkpoint: checkpointValue(ctx.adapter, storedCheckpoint),
      windowEnd: time(String(storedWindowEnd), 'invalid_window_end'),
    };
    if (selected.pending_page !== null) {
      await settleCreditAuditAfterAmbiguity(opened.database, selected, ctx);
      return await failRun(opened.database, selected, ctx, 'paid_ambiguous', 'paid_request_ambiguous', 'paid_ambiguous');
    }
    if (selected.state === 'ready_to_commit') {
      try {
        return await commit(opened.database, root, selected, ctx, effective);
      } catch (error) {
        if (error instanceof CrashInjection) throw error;
        if (error instanceof SyncFailure) {
          const state = error.failureClass === 'paid_ambiguous' ? 'paid_ambiguous' : 'failed';
          return await failRun(opened.database, runRow(opened.database, selected.id), ctx, error.failureClass, error.reasonCode, state);
        }
        throw error;
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
