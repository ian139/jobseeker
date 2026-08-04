import { fileURLToPath } from 'node:url';
import { lstat, readFile } from 'node:fs/promises';
import { resolve } from 'node:path';
import { createTheirStackAdapter } from './theirstack.mjs';
import { previewSource, recoverSourceSync, syncSource, SyncFailure } from './sync.mjs';
import { validateSourceSyncResult } from './contracts.mjs';

const RESULT_SCHEMA = 'source-sync-result-v1';
const ALLOWED_CONFIG = new Set([
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
  'baseUrl',
  'timeoutMs',
  'maxPreviewRetries',
  'retryDelayMs',
]);

function object(value) {
  return value !== null && typeof value === 'object' && !Array.isArray(value);
}

function cliFailure(code, mode = 'preview', source = 'theirstack', profile = 'default') {
  const result = {
    schema: RESULT_SCHEMA,
    syncRunId: null,
    source,
    profile,
    mode,
    state: 'failed',
    startedAt: '1970-01-01T00:00:00.000Z',
    finishedAt: '1970-01-01T00:00:00.000Z',
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
    reasonCode: code,
  };
  try {
    const checked = validateSourceSyncResult(result);
    return checked && typeof checked === 'object' ? checked : result;
  } catch {
    return result;
  }
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
  let info;
  try {
    info = await lstat(absolute);
  } catch {
    throw new Error('config_unreadable');
  }
  if (!info.isFile() || (info.mode & 0o077) !== 0 || (typeof process.getuid === 'function' && info.uid !== process.getuid())) throw new Error('config_not_private');
  let parsed;
  try {
    parsed = JSON.parse(await readFile(absolute, 'utf8'));
  } catch {
    throw new Error('config_invalid');
  }
  if (!object(parsed)) throw new Error('config_invalid');
  for (const key of Object.keys(parsed)) if (!ALLOWED_CONFIG.has(key)) throw new Error('config_unknown_key');
  return parsed;
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
  const profile = values.profile;
  if (typeof profile !== 'string' || profile.length === 0) throw new Error('profile_required');
  const now = values.now;
  if (typeof now !== 'string' || now.length === 0) throw new Error('now_required');
  const windowEnd = values.windowEnd;
  if (typeof windowEnd !== 'string' || windowEnd.length === 0) throw new Error('window_end_required');
  const pageSize = integer(values.pageSize, 'page_size');
  const maxPages = integer(values.maxPages, 'max_pages');
  if (!pageSize || pageSize < 1 || pageSize > 100) throw new Error('page_size_required');
  if (!maxPages || maxPages < 1 || maxPages > 1000) throw new Error('max_pages_required');
  if (command !== 'preview') {
    if (typeof values.databasePath !== 'string' || values.databasePath.length === 0) throw new Error('database_required');
    if (typeof values.privateRoot !== 'string' || values.privateRoot.length === 0) throw new Error('private_root_required');
  }
  return { profile, now, windowEnd, pageSize, maxPages };
}

export async function runCli(argv = process.argv.slice(2), env = process.env) {
  let mode = 'preview';
  let profile = 'default';
  try {
    const parsed = parseArgs(argv);
    mode = parsed.command;
    const config = await readConfig(parsed.config ?? parsed.configPath);
    const values = mergedArgs(parsed, config);
    profile = values.profile ?? profile;
    const required = requiredOptions(values, mode);
    const apiKey = env.THEIRSTACK_API_KEY;
    const adapter = createTheirStackAdapter({
      apiKey,
      profile: required.profile,
      baseUrl: values.baseUrl,
      timeoutMs: scalar(values.timeoutMs, 'timeout_ms', Number),
      maxPreviewRetries: integer(values.maxPreviewRetries, 'max_preview_retries'),
      retryDelayMs: scalar(values.retryDelayMs, 'retry_delay_ms', Number),
      postedAtMaxAgeDays: integer(values.postedAtMaxAgeDays, 'posted_at_max_age_days'),
      queryFilters: values.queryFilters,
      windowEnd: required.windowEnd,
      now: required.now,
      paidAuthorization: mode !== 'preview',
    });
    const options = {
      adapter,
      source: adapter.source,
      profile: required.profile,
      now: required.now,
      windowEnd: required.windowEnd,
      limit: required.pageSize,
      maxPages: required.maxPages,
      maxItems: integer(values.maxItems, 'max_items'),
      checkpoint: values.checkpoint,
      databasePath: values.databasePath,
      privateRoot: values.privateRoot,
      runId: integer(values.runId, 'run_id'),
      paidAuthorization: mode !== 'preview',
    };
    const result = mode === 'preview'
      ? await previewSource(options)
      : mode === 'sync'
        ? await syncSource(options)
        : await recoverSourceSync(options);
    const checked = validateSourceSyncResult(result);
    process.stdout.write(`${JSON.stringify(checked && typeof checked === 'object' ? checked : result)}\n`);
    return checked && typeof checked === 'object' ? checked : result;
  } catch (error) {
    const code = error instanceof SyncFailure ? error.reasonCode : (error instanceof Error ? error.message : 'cli_failed');
    const result = cliFailure(/^[a-z0-9_:-]{1,128}$/.test(code) ? code : 'cli_failed', mode, 'theirstack', profile);
    process.stdout.write(`${JSON.stringify(result)}\n`);
    return result;
  }
}

const invoked = process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url);
if (invoked) await runCli();

export { parseArgs, readConfig };
