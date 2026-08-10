import {
  constants as fsConstants,
  existsSync,
  readFileSync,
} from 'node:fs';
import {
  lstat,
  mkdir,
  open,
  rename,
  rm,
  rmdir,
} from 'node:fs/promises';
import { randomUUID } from 'node:crypto';
import { dirname, join, resolve } from 'node:path';
import { openIngestionDatabase } from '../ingestion/database.mjs';
import { createAdapter as registryCreateAdapter } from '../ingestion/source-registry.mjs';
import { syncSource as defaultSyncSource } from '../ingestion/sync.mjs';

export const MAX_SCHEDULER_CONFIG_BYTES = 1024 * 1024;
export const DEFAULT_MINIMUM_INTERVAL_HOURS = 4;
const LOCK_FILE_NAME = 'scheduler.lock';
const NOFOLLOW = fsConstants.O_NOFOLLOW ?? 0;

const SINGLE_SOURCE_CONFIG_KEYS = new Set([
  'source',
  'profile',
  'postedAtMaxAgeDays',
  'queryFilters',
  'pageSize',
  'maxPages',
  'maxItems',
  'databasePath',
  'privateRoot',
  'timeoutMs',
  'maxPreviewRetries',
  'retryDelayMs',
  'minimumIntervalHours',
]);

const MULTI_SOURCE_CONFIG_KEYS = new Set([
  'timezone',
  'databasePath',
  'privateRoot',
  'flagPath',
  'minimumIntervalHours',
  'sources',
]);

/**
 * Loads key-value environment variables from a file (e.g. private/.env) if it exists.
 * Does not overwrite existing environment variables.
 */
export function loadEnv(envPath) {
  const targetPath = envPath ?? resolve(process.cwd(), 'private/.env');
  if (!existsSync(targetPath)) return;

  try {
    const content = readFileSync(targetPath, 'utf8');
    for (const line of content.split('\n')) {
      const trimmed = line.trim();
      if (!trimmed || trimmed.startsWith('#')) continue;
      const eqIdx = trimmed.indexOf('=');
      if (eqIdx === -1) continue;
      const key = trimmed.slice(0, eqIdx).trim();
      let value = trimmed.slice(eqIdx + 1).trim();
      if (
        (value.startsWith('"') && value.endsWith('"')) ||
        (value.startsWith("'") && value.endsWith("'"))
      ) {
        value = value.slice(1, -1);
      }
      if (key && process.env[key] === undefined) {
        process.env[key] = value;
      }
    }
  } catch {
    // Optional env file read failure ignored
  }
}

/**
 * Formats a Date object or ISO string into a YYYY-MM-DD date string in the target timeZone.
 */
export function formatFormattedDate(date = new Date(), timeZone = 'America/New_York') {
  const d = typeof date === 'string' || typeof date === 'number' ? new Date(date) : date;
  const formatter = new Intl.DateTimeFormat('en-CA', {
    timeZone,
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
  });
  return formatter.format(d);
}

/**
 * Returns the timestamp of the most recent succeeded sync run for the source and
 * profile, or null when none exists. Uses finished_at when available and falls
 * back to started_at.
 */
export function lastSucceededRunAt({ database, source, profile }) {
  try {
    const row = database.prepare(
      "SELECT started_at, finished_at FROM sync_runs WHERE source = ? AND profile = ? AND state = 'succeeded' ORDER BY id DESC LIMIT 1"
    ).get(source, profile);
    if (!row) return null;
    return row.finished_at ?? row.started_at;
  } catch (err) {
    if (err?.code === 'ERR_SQLITE_ERROR' || err?.message?.includes('no such table')) {
      return null;
    }
    throw err;
  }
}

/**
 * Returns the most recent sync run row identity for the source and profile,
 * or null when no run exists yet.
 */
function latestRun({ database, source, profile }) {
  try {
    return database.prepare(
      'SELECT id, state, reason_code FROM sync_runs WHERE source = ? AND profile = ? ORDER BY id DESC LIMIT 1'
    ).get(source, profile) ?? null;
  } catch (err) {
    if (err?.code === 'ERR_SQLITE_ERROR' || err?.message?.includes('no such table')) {
      return null;
    }
    throw err;
  }
}

/**
 * Returns true when the last succeeded run finished within minimumIntervalHours
 * of now, fencing repeated runs within the configured interval.
 */
export function withinMinimumInterval({ lastSucceededAt, now, minimumIntervalHours }) {
  const lastMs = Date.parse(lastSucceededAt);
  const nowMs = Date.parse(now);
  if (!Number.isFinite(lastMs) || !Number.isFinite(nowMs)) return false;
  return nowMs - lastMs < minimumIntervalHours * 60 * 60 * 1000;
}

function configError(code, detail = '') {
  const error = new Error(detail ? `${code}: ${detail}` : code);
  error.code = code;
  return error;
}

export const MAX_WAKE_PAYLOAD_BYTES = 64 * 1024;

const WAKE_REASON_PATTERN = /^[a-z][a-z0-9_]{0,63}$/;
const IDLE_WAKE = Object.freeze({ kind: 'idle' });

export class OmpWakeInvalidError extends Error {
  constructor() {
    super('E_OMP_WAKE_INVALID');
    this.name = 'OmpWakeInvalidError';
    this.code = 'E_OMP_WAKE_INVALID';
  }
}

export class OmpWakeAbortedError extends Error {
  constructor() {
    super('E_OMP_WAKE_ABORTED');
    this.name = 'OmpWakeAbortedError';
    this.code = 'E_OMP_WAKE_ABORTED';
  }
}

function invalidWake() {
  throw new OmpWakeInvalidError();
}

function normalizeWakePayload(value) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) invalidWake();
  const keys = Object.keys(value);
  if (keys.length !== 2 || !keys.includes('timestamp') || !keys.includes('reason')) invalidWake();
  if (typeof value.timestamp !== 'string'
    || Number.isNaN(Date.parse(value.timestamp))
    || new Date(value.timestamp).toISOString() !== value.timestamp) invalidWake();
  if (typeof value.reason !== 'string' || !WAKE_REASON_PATTERN.test(value.reason)) invalidWake();
  return Object.freeze({
    kind: 'wake',
    timestamp: value.timestamp,
    reason: value.reason,
  });
}

function boundedInteger(value, fallback, minimum, maximum) {
  const selected = value ?? fallback;
  if (!Number.isSafeInteger(selected) || selected < minimum || selected > maximum) invalidWake();
  return selected;
}

function throwIfAborted(signal) {
  if (signal?.aborted) throw new OmpWakeAbortedError();
}

async function syncDirectory(path) {
  const handle = await open(path, fsConstants.O_RDONLY);
  try {
    await handle.sync();
  } finally {
    await handle.close();
  }
}

async function readBoundedWake(handle) {
  const buffer = Buffer.allocUnsafe(MAX_WAKE_PAYLOAD_BYTES + 1);
  let total = 0;
  while (total < buffer.length) {
    const { bytesRead } = await handle.read(
      buffer,
      total,
      buffer.length - total,
      total,
    );
    if (bytesRead === 0) break;
    total += bytesRead;
  }
  if (total === 0 || total > MAX_WAKE_PAYLOAD_BYTES) invalidWake();
  return buffer.subarray(0, total).toString('utf8');
}

/**
 * Atomically publishes a timestamped advisory signal for the persistent OMP session.
 */
export async function wakeOmpSession({ reason = 'daily_scheduler_complete', flagPath } = {}) {
  if (typeof reason !== 'string' || !WAKE_REASON_PATTERN.test(reason)) invalidWake();
  const targetPath = flagPath ?? resolve(process.cwd(), 'scheduler/wake-omp.flag');
  const dir = dirname(targetPath);
  await mkdir(dir, { recursive: true, mode: 0o700 });
  const tempPath = `${targetPath}.tmp-${process.pid}-${randomUUID()}`;
  let handle;
  try {
    handle = await open(
      tempPath,
      fsConstants.O_CREAT | fsConstants.O_EXCL | fsConstants.O_WRONLY | fsConstants.O_NOFOLLOW,
      0o600,
    );
    await handle.writeFile(`${JSON.stringify({
      timestamp: new Date().toISOString(),
      reason,
    })}\n`, 'utf8');
    await handle.sync();
    await handle.close();
    handle = undefined;
    await rename(tempPath, targetPath);
    await syncDirectory(dir);
  } catch (error) {
    if (handle !== undefined) await handle.close().catch(() => {});
    await rm(tempPath, { force: true }).catch(() => {});
    throw error;
  }
  console.log(`[scheduler] OMP session wake signal written to ${targetPath}`);
  return targetPath;
}

/**
 * Atomically claims and consumes at most one advisory OMP wake signal.
 */
export async function consumeOmpWake({ flagPath } = {}) {
  const targetPath = flagPath ?? resolve(process.cwd(), 'scheduler/wake-omp.flag');
  const claimPath = `${targetPath}.claim-${process.pid}-${randomUUID()}`;
  try {
    await rename(targetPath, claimPath);
  } catch (error) {
    if (error?.code === 'ENOENT') return IDLE_WAKE;
    throw error;
  }

  let handle;
  let result;
  let failure;
  try {
    handle = await open(
      claimPath,
      fsConstants.O_RDONLY | fsConstants.O_NOFOLLOW | fsConstants.O_NONBLOCK,
    );
    const metadata = await handle.stat();
    if (!metadata.isFile() || metadata.size === 0 || metadata.size > MAX_WAKE_PAYLOAD_BYTES) invalidWake();
    const content = await readBoundedWake(handle);
    let payload;
    try {
      payload = JSON.parse(content);
    } catch {
      invalidWake();
    }
    result = normalizeWakePayload(payload);
  } catch (error) {
    failure = error?.code === 'ELOOP' ? new OmpWakeInvalidError() : error;
  } finally {
    if (handle !== undefined) await handle.close().catch(() => {});
    try {
      await rm(claimPath, { force: true });
    } catch (cleanupError) {
      if (cleanupError?.code === 'ERR_FS_EISDIR' || cleanupError?.code === 'EISDIR') {
        try {
          await rmdir(claimPath);
        } catch (directoryError) {
          if (failure === undefined) failure = directoryError;
        }
      } else if (failure === undefined) {
        failure = cleanupError;
      }
    }
  }
  if (failure !== undefined) throw failure;
  return result;
}

function sleepWithSignal(milliseconds, signal) {
  return new Promise((resolveSleep, rejectSleep) => {
    throwIfAborted(signal);
    const timer = setTimeout(() => {
      signal?.removeEventListener('abort', onAbort);
      resolveSleep();
    }, milliseconds);
    function onAbort() {
      clearTimeout(timer);
      rejectSleep(new OmpWakeAbortedError());
    }
    signal?.addEventListener('abort', onAbort, { once: true });
  });
}

/**
 * Polls for one advisory wake signal and returns idle after a bounded timeout.
 */
export async function waitForOmpWake({
  flagPath,
  pollIntervalMs = 100,
  timeoutMs = 5_000,
  signal,
} = {}) {
  const interval = boundedInteger(pollIntervalMs, 100, 1, 60_000);
  const timeout = boundedInteger(timeoutMs, 5_000, 0, 86_400_000);
  throwIfAborted(signal);
  const deadline = performance.now() + timeout;
  while (true) {
    const result = await consumeOmpWake({ flagPath });
    if (result.kind === 'wake') return result;
    const remaining = deadline - performance.now();
    if (remaining <= 0) return IDLE_WAKE;
    await sleepWithSignal(Math.min(interval, Math.ceil(remaining)), signal);
  }
}

/**
 * Securely reads a scheduler config file. The parent directory must be owned by
 * the current user and not group/world writable; the file itself must not be a
 * symlink, must be owned by the current user, must be owner-only, and must not
 * exceed MAX_SCHEDULER_CONFIG_BYTES.
 */
export async function readSchedulerConfigFile(configPath) {
  if (typeof configPath !== 'string' || configPath.length === 0) {
    throw configError('E_SCHEDULER_CONFIG', 'config path required');
  }
  const absolute = resolve(configPath);
  const parent = await lstat(dirname(absolute)).catch(() => {
    throw configError('E_SCHEDULER_CONFIG', 'config unreadable');
  });
  if (
    !parent.isDirectory()
    || (parent.mode & 0o022) !== 0
    || (typeof process.getuid === 'function' && parent.uid !== process.getuid())
  ) {
    throw configError('E_SCHEDULER_CONFIG', 'config parent not private');
  }

  let handle;
  try {
    handle = await open(absolute, fsConstants.O_RDONLY | NOFOLLOW);
    const info = await handle.stat();
    const linked = await lstat(absolute);
    if (
      !info.isFile()
      || !linked.isFile()
      || info.dev !== linked.dev
      || info.ino !== linked.ino
      || info.size > MAX_SCHEDULER_CONFIG_BYTES
      || (info.mode & 0o077) !== 0
      || (typeof process.getuid === 'function' && info.uid !== process.getuid())
    ) {
      throw configError('E_SCHEDULER_CONFIG', 'config not private');
    }
    const text = await handle.readFile('utf8');
    return JSON.parse(text);
  } catch (error) {
    if (error instanceof SyntaxError) throw configError('E_SCHEDULER_CONFIG', 'config invalid JSON');
    if (error?.code === 'E_SCHEDULER_CONFIG') throw error;
    throw configError('E_SCHEDULER_CONFIG', 'config unreadable');
  } finally {
    await handle?.close();
  }
}

function isObject(value) {
  return value !== null && typeof value === 'object' && !Array.isArray(value);
}

function unknownKeys(value, allowed) {
  return Object.keys(value).filter((key) => !allowed.has(key));
}

/**
 * Normalizes either the multi-source scheduler shape ({sources: [...]}) or the
 * single-source private shape ({source, profile, ...}) into an internal list of
 * source entries. Single-source options map to one paid source without exposing
 * their values outside the returned sources list.
 */
export function normalizeSchedulerConfig(config) {
  if (!isObject(config)) throw configError('E_SCHEDULER_CONFIG', 'config must be an object');

  if (Array.isArray(config.sources)) {
    const unknown = unknownKeys(config, MULTI_SOURCE_CONFIG_KEYS);
    if (unknown.length > 0) {
      throw configError('E_SCHEDULER_CONFIG', `unknown config keys: ${unknown.join(', ')}`);
    }
    const sources = config.sources.map((entry) => {
      if (!isObject(entry)) throw configError('E_SCHEDULER_CONFIG', 'source entry must be an object');
      if (typeof entry.source !== 'string' || entry.source.length === 0) {
        throw configError('E_SCHEDULER_CONFIG', 'source entry requires a source');
      }
      return {
        source: entry.source,
        profile: entry.profile ?? 'default',
        mode: entry.mode ?? 'paid',
        options: isObject(entry.options) ? entry.options : {},
      };
    });
    return { kind: 'multi', sources, config };
  }

  if (typeof config.source === 'string' && config.source.length > 0) {
    const unknown = unknownKeys(config, SINGLE_SOURCE_CONFIG_KEYS);
    if (unknown.length > 0) {
      throw configError('E_SCHEDULER_CONFIG', `unknown config keys: ${unknown.join(', ')}`);
    }
    const options = {};
    for (const key of ['postedAtMaxAgeDays', 'queryFilters', 'pageSize', 'maxPages', 'maxItems', 'timeoutMs', 'maxPreviewRetries', 'retryDelayMs']) {
      if (config[key] !== undefined) options[key] = config[key];
    }
    return {
      kind: 'single',
      sources: [{
        source: config.source,
        profile: config.profile ?? 'default',
        mode: 'paid',
        options,
      }],
      config,
    };
  }

  throw configError('E_SCHEDULER_CONFIG', 'config must define sources[] or a single source');
}

/**
 * Loads configuration from an in-memory object or from the first existing
 * secure config file candidate (configPath, SCHEDULER_CONFIG_PATH, or
 * private/scheduler-config.json).
 */
export async function loadConfig(options = {}) {
  if (options.config) return options.config;

  const candidatePaths = [
    options.configPath,
    process.env.SCHEDULER_CONFIG_PATH,
    resolve(process.cwd(), 'private/scheduler-config.json'),
  ].filter(Boolean);

  for (const path of candidatePaths) {
    if (existsSync(path)) {
      return await readSchedulerConfigFile(path);
    }
  }

  throw configError('E_SCHEDULER_CONFIG', 'no scheduler configuration file found');
}

function boundedInterval(value) {
  if (!Number.isSafeInteger(value) || value < 1 || value > 24 * 365) {
    throw configError('E_SCHEDULER_CONFIG', 'minimumIntervalHours must be a positive integer');
  }
  return value;
}

/**
 * Acquires the scheduler cycle lock inside privateRoot with O_EXCL semantics.
 * Fails closed when another cycle holds the lock.
 */
async function acquireCycleLock(privateRoot) {
  const lockPath = join(privateRoot, LOCK_FILE_NAME);
  const token = randomUUID();
  let handle;
  try {
    handle = await open(
      lockPath,
      fsConstants.O_CREAT | fsConstants.O_EXCL | fsConstants.O_WRONLY | NOFOLLOW,
      0o600,
    );
    await handle.writeFile(`${JSON.stringify({
      pid: process.pid,
      token,
      acquiredAt: new Date().toISOString(),
    })}\n`, 'utf8');
    await handle.sync();
    await handle.close();
    handle = undefined;
    return { lockPath, token };
  } catch (error) {
    if (handle !== undefined) {
      await handle.close().catch(() => {});
      await rm(lockPath, { force: true }).catch(() => {});
    }
    if (error?.code === 'EEXIST' || error?.code === 'ELOOP') {
      throw configError('E_SCHEDULER_LOCKED', 'another scheduler cycle is already running');
    }
    throw error;
  }
}

/**
 * Releases the cycle lock only when it still belongs to this cycle, so a stale
 * or replaced lock is never removed by a different owner.
 */
async function releaseCycleLock(lock) {
  if (!lock) return;
  try {
    const content = readFileSync(lock.lockPath, 'utf8');
    const parsed = JSON.parse(content);
    if (parsed.token !== lock.token) return;
  } catch {
    return;
  }
  await rm(lock.lockPath, { force: true });
}

async function requirePrivateRoot(privateRoot) {
  const status = await lstat(privateRoot).catch(() => {
    throw configError('E_SCHEDULER_PRIVATE_ROOT', 'private root unreadable');
  });
  if (!status.isDirectory()
    || status.isSymbolicLink()
    || (status.mode & 0o077) !== 0
    || (typeof process.getuid === 'function' && status.uid !== process.getuid())) {
    throw configError('E_SCHEDULER_PRIVATE_ROOT', 'private root must be owner-only');
  }
}

/**
 * Runs the scheduled source ingestion cycle: acquires the cycle lock, fences
 * sources by minimum interval and paid ambiguity, syncs due sources, and
 * publishes an OMP wake only when new queue rows were inserted.
 */
export async function runDailyScheduler(options = {}) {
  loadEnv(options.envPath);

  const config = await loadConfig(options);
  const normalized = normalizeSchedulerConfig(config);

  const timeZone = options.timeZone ?? config.timezone ?? 'America/New_York';
  const databasePath = options.databasePath ?? config.databasePath ?? process.env.DATABASE_PATH ?? 'data/RealJobs.sqlite';
  const privateRoot = options.privateRoot ?? config.privateRoot ?? process.env.PRIVATE_ROOT ?? 'private/artifacts';
  const flagPath = options.flagPath ?? config.flagPath ?? join(privateRoot, 'wake-omp.flag');
  const minimumIntervalHours = boundedInterval(
    options.minimumIntervalHours ?? config.minimumIntervalHours ?? DEFAULT_MINIMUM_INTERVAL_HOURS,
  );
  const now = options.now ? new Date(options.now).toISOString() : new Date().toISOString();
  const todayDateStr = formatFormattedDate(now, timeZone);

  const createAdapterFn = options.createAdapter ?? registryCreateAdapter;
  const syncSourceFn = options.syncSource ?? defaultSyncSource;

  let db = options.database;
  let ownedDb = false;

  if (!db) {
    db = openIngestionDatabase(databasePath);
    ownedDb = true;
  }

  const results = [];
  let totalQueueRowsInserted = 0;
  let flagFile = null;
  let lock = null;

  try {
    await mkdir(privateRoot, { recursive: true, mode: 0o700 });
    await requirePrivateRoot(privateRoot);
    lock = await acquireCycleLock(privateRoot);

    for (const sourceConfig of normalized.sources) {
      const source = sourceConfig.source;
      const profile = sourceConfig.profile;
      const mode = sourceConfig.mode;
      const sourceOpts = sourceConfig.options ?? {};

      const lastSucceededAt = lastSucceededRunAt({ database: db, source, profile });
      if (lastSucceededAt !== null && withinMinimumInterval({ lastSucceededAt, now, minimumIntervalHours })) {
        console.log(`[scheduler] Source ${source}:${profile} succeeded within ${minimumIntervalHours}h interval. Skipping.`);
        results.push({
          source,
          profile,
          status: 'skipped',
          reason: 'within_minimum_interval',
          date: todayDateStr,
          lastSucceededAt,
        });
        continue;
      }

      const lastRun = latestRun({ database: db, source, profile });
      if (lastRun !== null && lastRun.state === 'paid_ambiguous') {
        console.log(`[scheduler] Source ${source}:${profile} last run is paid_ambiguous (${lastRun.reason_code ?? 'unknown'}). Not replaying.`);
        results.push({
          source,
          profile,
          status: 'paid_ambiguous',
          syncRunId: lastRun.id,
          reasonCode: lastRun.reason_code ?? 'paid_request_ambiguous',
          date: todayDateStr,
        });
        continue;
      }

      console.log(`[scheduler] Synchronizing source ${source}:${profile}...`);
      const adapter = await createAdapterFn({
        source,
        profile,
        config: sourceOpts,
        env: process.env,
        now,
      });

      const boundsObject = {
        limit: sourceOpts.pageSize ?? sourceOpts.limit ?? 25,
        maxPages: sourceOpts.maxPages ?? 100,
        maxItems: sourceOpts.maxItems ?? null,
        windowEnd: sourceOpts.windowEnd ?? now,
        ...(sourceOpts.bounds ?? {}),
      };

      const syncResult = await syncSourceFn({
        adapter,
        source,
        profile,
        mode,
        database: db,
        databasePath,
        privateRoot,
        paidAuthorization: true,
        now,
        ...sourceOpts,
        limit: boundsObject.limit,
        maxPages: boundsObject.maxPages,
        maxItems: boundsObject.maxItems,
        windowEnd: boundsObject.windowEnd,
        bounds: boundsObject,
      });

      if (syncResult.state === 'paid_ambiguous') {
        console.log(`[scheduler] Source ${source}:${profile} finished paid_ambiguous (${syncResult.reasonCode ?? 'unknown'}). Not retried.`);
        results.push({
          source,
          profile,
          status: 'paid_ambiguous',
          syncRunId: syncResult.syncRunId ?? null,
          reasonCode: syncResult.reasonCode ?? 'paid_request_ambiguous',
          date: todayDateStr,
        });
        continue;
      }
      if (syncResult.state !== 'succeeded') {
        throw new Error(`Sync for source ${source}:${profile} failed with state '${syncResult.state}' (reason: ${syncResult.reasonCode ?? 'unknown'})`);
      }

      totalQueueRowsInserted += Number(syncResult.queueRowsInserted ?? 0);
      results.push({
        source,
        profile,
        status: 'succeeded',
        syncRunId: syncResult.syncRunId,
        date: todayDateStr,
      });
    }

    if (totalQueueRowsInserted > 0) {
      flagFile = await wakeOmpSession({
        reason: 'daily_scheduler_complete',
        flagPath,
      });
    }

    return {
      date: todayDateStr,
      timeZone,
      results,
      flagPath: flagFile,
    };
  } finally {
    await releaseCycleLock(lock);
    if (ownedDb && db) {
      try {
        db.close();
      } catch {
        // Best effort close
      }
    }
  }
}

// CLI entry point
if (process.argv[1] && resolve(process.argv[1]) === resolve(import.meta.filename ?? '')) {
  runDailyScheduler()
    .then((summary) => {
      console.log('[scheduler] Daily run complete:', JSON.stringify(summary, null, 2));
      process.exit(0);
    })
    .catch((err) => {
      console.error('[scheduler] Daily run failed:', err);
      process.exit(1);
    });
}
