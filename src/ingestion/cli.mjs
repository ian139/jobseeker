import { fileURLToPath } from 'node:url';
import { constants as fsConstants } from 'node:fs';
import { lstat, open } from 'node:fs/promises';
import { dirname, resolve } from 'node:path';
import { createAdapter } from './source-registry.mjs';
import { previewSource, recoverSourceSync, syncSource, SyncFailure } from './sync.mjs';
import { validateSourceSyncResult } from './contracts.mjs';

const RESULT_SCHEMA = 'source-sync-result-v1';
const ALLOWED_CONFIG = new Set([
  'source',
  'profile',
  'postedAtMaxAgeDays',
  'queryFilters',
  'pageSize',
  'maxPages',
  'maxItems',
  'windowEnd',
  'checkpoint',
  'now',
  'databasePath',
  'privateRoot',
  'runId',
  'timeoutMs',
  'maxPreviewRetries',
  'retryDelayMs',
  'companies',
  'searchUrl',
  'savedSearchQuery',
]);
const ALLOWED_FLAGS = new Set([
  'config',
  'source',
  'profile',
  'private-root',
  'database-path',
  'db',
  'page-size',
  'max-pages',
  'max-items',
  'window-end',
  'checkpoint',
  'now',
  'run-id',
  'timeout-ms',
  'max-preview-retries',
  'retry-delay-ms',
  'posted-at-max-age-days',
]);
const MAX_CONFIG_BYTES = 1024 * 1024;
const NOFOLLOW = fsConstants.O_NOFOLLOW ?? 0;

function object(value) {
  return value !== null && typeof value === 'object' && !Array.isArray(value);
}

function cliFailure(code, mode = 'preview', source = 'theirstack', profile = 'default') {
  const timestamp = new Date().toISOString();
  const safeSource = typeof source === 'string' && /^[a-z][a-z0-9_-]{0,63}$/u.test(source) ? source : 'theirstack';
  const safeProfile = typeof profile === 'string' && /^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$/u.test(profile) ? profile : 'default';
  const safeCode = typeof code === 'string' && /^[a-z][a-z0-9_]{0,63}$/u.test(code) ? code : 'cli_failed';
  return validateSourceSyncResult({
    schema: RESULT_SCHEMA,
    syncRunId: null,
    source: safeSource,
    profile: safeProfile,
    mode: mode === 'paid' ? 'paid' : 'preview',
    state: 'failed',
    startedAt: timestamp,
    finishedAt: timestamp,
    checkpointBefore: null,
    checkpointAfter: null,
    pagesFetched: 0,
    requestCount: 0,
    jobsSeen: 0,
    jobsInserted: 0,
    jobsUpdated: 0,
    jobsUnchanged: 0,
    dedupeGroupsTouched: 0,
    queueRowsInserted: 0,
    estimatedCredits: 0,
    reportedCredits: 0,
    failureClass: 'terminal',
    reasonCode: safeCode,
  });
}

function parseArgs(argv) {
  if (argv.length === 0 || !['preview', 'sync', 'recover'].includes(argv[0])) throw new Error('command_required');
  const command = argv[0];
  const values = { command };
  for (let index = 1; index < argv.length; index += 1) {
    const token = argv[index];
    if (token === '--paid') {
      if (command === 'preview') throw new Error('paid_not_allowed');
      values.paid = true;
      continue;
    }
    if (!token.startsWith('--') || token === '--') throw new Error('unknown_argument');
    const equals = token.indexOf('=');
    const name = equals === -1 ? token.slice(2) : token.slice(2, equals);
    if (!name || name === 'api-key' || name === 'apiKey') throw new Error('unsupported_credential_flag');
    if (!ALLOWED_FLAGS.has(name)) throw new Error('unknown_argument');
    let value;
    if (equals !== -1) value = token.slice(equals + 1);
    else {
      if (index + 1 >= argv.length || argv[index + 1].startsWith('--')) throw new Error('flag_value_required');
      value = argv[++index];
    }
    if (name === 'paid') throw new Error('paid_must_be_explicit');
    values[name] = value;
  }
  if (command !== 'preview' && values.paid !== true) throw new Error('paid_confirmation_required');
  return values;
}

async function readConfig(path) {
  if (typeof path !== 'string' || path.length === 0) throw new Error('config_required');
  const absolute = resolve(path);
  const parent = await lstat(dirname(absolute)).catch(() => { throw new Error('config_unreadable'); });
  if (!parent.isDirectory() || (parent.mode & 0o022) !== 0 || (typeof process.getuid === 'function' && parent.uid !== process.getuid())) throw new Error('config_parent_unsafe');
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
      || info.size > MAX_CONFIG_BYTES
      || (info.mode & 0o077) !== 0
      || (typeof process.getuid === 'function' && info.uid !== process.getuid())
    ) throw new Error('config_not_private');
    const parsed = JSON.parse(await handle.readFile('utf8'));
    if (!object(parsed)) throw new Error('config_invalid');
    for (const key of Object.keys(parsed)) if (!ALLOWED_CONFIG.has(key)) throw new Error('config_unknown_key');
    return parsed;
  } catch (error) {
    if (error instanceof SyntaxError) throw new Error('config_invalid');
    if (error instanceof Error && error.message.startsWith('config_')) throw error;
    throw new Error('config_unreadable');
  } finally {
    await handle?.close();
  }
}

function scalar(value, name, parse = (input) => input) {
  if (value === undefined || value === null) return undefined;
  try {
    return parse(value);
  } catch {
    throw new Error(`invalid_${name}`);
  }
}

function integer(value, name) {
  const parsed = scalar(value, name, (input) => {
    if (!/^[0-9]+$/.test(String(input))) throw new Error('not_integer');
    return Number(input);
  });
  if (parsed !== undefined && (!Number.isSafeInteger(parsed) || parsed < 0)) throw new Error(`invalid_${name}`);
  return parsed;
}

function mergedArgs(args, config) {
  const result = { ...config };
  const aliases = {
    config: 'configPath',
    'private-root': 'privateRoot',
    'database-path': 'databasePath',
    db: 'databasePath',
    'page-size': 'pageSize',
    'max-pages': 'maxPages',
    'max-items': 'maxItems',
    'window-end': 'windowEnd',
    'posted-at-max-age-days': 'postedAtMaxAgeDays',
    'run-id': 'runId',
    'timeout-ms': 'timeoutMs',
    'max-preview-retries': 'maxPreviewRetries',
    'retry-delay-ms': 'retryDelayMs',
  };
  for (const [key, value] of Object.entries(args)) {
    if (key === 'command' || key === 'paid' || key === 'configPath') continue;
    const target = aliases[key] ?? key;
    if (Object.hasOwn(config, target) && String(config[target]) !== String(value)) throw new Error('config_flag_conflict');
    result[target] = value;
  }
  return result;
}

function requiredOptions(values, command) {
  const source = values.source ?? 'theirstack';
  const profile = values.profile ?? 'default';
  if (typeof source !== 'string' || source.length === 0) throw new Error('source_required');
  if (typeof profile !== 'string' || profile.length === 0) throw new Error('profile_required');
  const now = values.now ?? new Date().toISOString();
  if (typeof now !== 'string' || now.length === 0) throw new Error('now_required');
  const windowEnd = values.windowEnd ?? now;
  if (typeof windowEnd !== 'string' || windowEnd.length === 0) throw new Error('window_end_required');
  const pageSize = integer(values.pageSize, 'page_size');
  const maxPages = integer(values.maxPages, 'max_pages');
  if (!pageSize || pageSize < 1 || pageSize > 100) throw new Error('page_size_required');
  if (!maxPages || maxPages < 1 || maxPages > 1000) throw new Error('max_pages_required');
  if (command !== 'preview') {
    if (typeof values.databasePath !== 'string' || values.databasePath.length === 0) throw new Error('database_required');
    if (typeof values.privateRoot !== 'string' || values.privateRoot.length === 0) throw new Error('private_root_required');
  }
  return { source, profile, now, windowEnd, pageSize, maxPages };
}

function adapterConfig(values, command, required) {
  return {
    ...values,
    timeoutMs: scalar(values.timeoutMs, 'timeout_ms', Number),
    maxPreviewRetries: integer(values.maxPreviewRetries, 'max_preview_retries'),
    retryDelayMs: scalar(values.retryDelayMs, 'retry_delay_ms', Number),
    postedAtMaxAgeDays: integer(values.postedAtMaxAgeDays, 'posted_at_max_age_days'),
    windowEnd: required.windowEnd,
    paidAuthorization: command !== 'preview',
    now: () => required.now,
  };
}

export async function runCli(argv = process.argv.slice(2), env = process.env) {
  let command = 'preview';
  let mode = 'preview';
  let source = 'theirstack';
  let profile = 'default';
  try {
    const parsed = parseArgs(argv);
    command = parsed.command;
    mode = command === 'preview' ? 'preview' : 'paid';
    const config = await readConfig(parsed.config ?? parsed.configPath);
    const values = mergedArgs(parsed, config);
    const required = requiredOptions(values, command);
    source = required.source;
    const adapter = await createAdapter({
      source,
      profile: required.profile,
      config: adapterConfig(values, command, required),
      env,
      now: () => required.now,
    });
    profile = required.profile;
    const options = {
      adapter,
      source: adapter.source,
      profile,
      now: required.now,
      windowEnd: required.windowEnd,
      limit: required.pageSize,
      maxPages: required.maxPages,
      maxItems: integer(values.maxItems, 'max_items'),
      checkpoint: values.checkpoint,
      databasePath: values.databasePath,
      privateRoot: values.privateRoot,
      runId: integer(values.runId, 'run_id'),
      paidAuthorization: command !== 'preview',
    };
    const result = command === 'preview'
      ? await previewSource(options)
      : command === 'sync'
        ? await syncSource(options)
        : await recoverSourceSync(options);
    const checked = validateSourceSyncResult(result);
    process.stdout.write(`${JSON.stringify(checked && typeof checked === 'object' ? checked : result)}\n`);
    return checked && typeof checked === 'object' ? checked : result;
  } catch (error) {
    const code = error instanceof SyncFailure ? error.reasonCode : (error instanceof Error ? error.message : 'cli_failed');
    const result = cliFailure(/^[a-z][a-z0-9_]{0,63}$/.test(code) ? code : 'cli_failed', mode, source, profile);
    process.stdout.write(`${JSON.stringify(result)}\n`);
    return result;
  }
}

const invoked = process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url);
if (invoked) {
  const result = await runCli();
  if (result.state === 'failed' || result.state === 'paid_ambiguous') process.exitCode = 1;
}
export { parseArgs, readConfig };
