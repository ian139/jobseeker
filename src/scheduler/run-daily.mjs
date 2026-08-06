import {
  constants as fsConstants,
  existsSync,
  readFileSync,
} from 'node:fs';
import {
  mkdir,
  open,
  rename,
  rm,
  rmdir,
} from 'node:fs/promises';
import { randomUUID } from 'node:crypto';
import { dirname, resolve } from 'node:path';
import { openIngestionDatabase } from '../ingestion/database.mjs';
import { createAdapter as registryCreateAdapter } from '../ingestion/source-registry.mjs';
import { syncSource as defaultSyncSource } from '../ingestion/sync.mjs';

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
 * Checks whether a succeeded sync_run exists for the source and profile on the specified date string.
 */
export function hasSucceededRunToday({ database, source, profile, dateStr, timeZone = 'America/New_York' }) {
  try {
    const rows = database.prepare(
      "SELECT started_at FROM sync_runs WHERE source = ? AND profile = ? AND state = 'succeeded'"
    ).all(source, profile);

    for (const row of rows) {
      if (!row.started_at) continue;
      const rowDateStr = formatFormattedDate(new Date(row.started_at), timeZone);
      if (rowDateStr === dateStr) {
        return true;
      }
    }
  } catch (err) {
    if (err?.code === 'ERR_SQLITE_ERROR' || err?.message?.includes('no such table')) {
      return false;
    }
    throw err;
  }
  return false;
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
 * Loads configuration object from options, file path, or fallback example config.
 */
export function loadConfig(options = {}) {
  if (options.config) return options.config;

  const candidatePaths = [
    options.configPath,
    process.env.SCHEDULER_CONFIG_PATH,
    resolve(process.cwd(), 'private/scheduler-config.json'),
    resolve(process.cwd(), 'src/scheduler/config.example.json'),
  ].filter(Boolean);

  for (const path of candidatePaths) {
    if (existsSync(path)) {
      try {
        const raw = readFileSync(path, 'utf8');
        return JSON.parse(raw);
      } catch (err) {
        throw new Error(`Failed to parse scheduler config at ${path}: ${err.message}`);
      }
    }
  }

  throw new Error('No scheduler configuration file found.');
}

/**
 * Runs the daily source ingestion scheduler.
 */
export async function runDailyScheduler(options = {}) {
  loadEnv(options.envPath);

  const config = loadConfig(options);
  const timeZone = options.timeZone ?? config.timezone ?? 'America/New_York';
  const databasePath = options.databasePath ?? config.databasePath ?? process.env.DATABASE_PATH ?? 'data/RealJobs.sqlite';
  const privateRoot = options.privateRoot ?? config.privateRoot ?? process.env.PRIVATE_ROOT ?? 'private/artifacts';
  const flagPath = options.flagPath ?? config.flagPath ?? resolve(process.cwd(), 'scheduler/wake-omp.flag');
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

  const sources = options.sources ?? config.sources ?? [];
  const results = [];

  await mkdir(privateRoot, { recursive: true, mode: 0o700 });

  try {
    for (const sourceConfig of sources) {
      const source = sourceConfig.source;
      const profile = sourceConfig.profile ?? 'default';
      const mode = sourceConfig.mode ?? 'paid';
      const sourceOpts = sourceConfig.options ?? {};

      const alreadySynced = hasSucceededRunToday({
        database: db,
        source,
        profile,
        dateStr: todayDateStr,
        timeZone,
      });
      if (alreadySynced) {
        console.log(`[scheduler] Source ${source}:${profile} already succeeded for date ${todayDateStr} (${timeZone}). Skipping.`);
        results.push({
          source,
          profile,
          status: 'skipped',
          reason: 'already_succeeded_today',
          date: todayDateStr,
        });
        continue;
      }

      console.log(`[scheduler] Synchronizing source ${source}:${profile} for date ${todayDateStr}...`);
      const adapter = await createAdapterFn({
        source,
        profile,
        config: sourceOpts,
        env: process.env,
        now,
      });

      const defaultBounds = {
        limit: 25,
        maxPages: 100,
        windowEnd: now,
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
        ...defaultBounds,
        ...sourceOpts,
        bounds: {
          limit: sourceOpts.limit ?? 25,
          maxPages: sourceOpts.maxPages ?? 100,
          windowEnd: sourceOpts.windowEnd ?? now,
          ...(sourceOpts.bounds ?? {}),
        },
      });
      if (syncResult.state !== 'succeeded') {
        throw new Error(`Sync for source ${source}:${profile} failed with state '${syncResult.state}' (reason: ${syncResult.reasonCode ?? 'unknown'})`);
      }

      results.push({
        source,
        profile,
        status: 'succeeded',
        syncRunId: syncResult.syncRunId,
        date: todayDateStr,
      });
    }

    const flagFile = await wakeOmpSession({
      reason: 'daily_scheduler_complete',
      flagPath,
    });

    return {
      date: todayDateStr,
      timeZone,
      results,
      flagPath: flagFile,
    };
  } finally {
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
