import { execFile as execFileCallback } from 'node:child_process';
import { constants as fsConstants } from 'node:fs';
import { lstat, open, readFile, readdir, stat, mkdir } from 'node:fs/promises';
import { basename, dirname, isAbsolute, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { promisify } from 'node:util';

const execFileAsync = promisify(execFileCallback);

/**
 * A deterministic environment supervisor for exactly one visible CMUX-hosted
 * OMP worker. It owns no application state: it only guarantees one CMUX
 * workspace with one OMP process running the worker invocation, and it never
 * imports or calls application lifecycle APIs (recovery, claim, heartbeat,
 * persistence). OMP itself follows skills/application-prep/SKILL.md.
 *
 * The supervisor is one-shot and owner-private: launchd invokes it
 * periodically (RunAtLoad + KeepAlive on failure), it loads an owner-only
 * config under private/, queries CMUX through execFile with structured
 * `tree --all --json` and `top --all --processes --flat` queries, fails
 * closed on duplicate matching workspaces or more than one OMP process, and
 * creates exactly one worker workspace when none exists.
 */

export class LocalOmpSupervisorError extends Error {
  constructor(code, message, options) {
    super(message, options);
    this.name = 'LocalOmpSupervisorError';
    this.code = code;
  }
}

export const DEFAULT_CONFIG_FILENAME = 'auggood-worker.json';

const REPO_ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..', '..');

export const DEFAULT_CONFIG_PATH = join(REPO_ROOT, 'private', DEFAULT_CONFIG_FILENAME);

export const CMUX_TREE_ARGS = Object.freeze(['tree', '--all', '--json', '--id-format', 'uuids']);

export const CMUX_TOP_ARGS = Object.freeze(['top', '--all', '--processes', '--flat', '--id-format', 'uuids']);

export const DEFAULT_CHECK_INTERVAL_MS = 15_000;

export const DEFAULT_SPAWN_GRACE_MS = 60_000;

export const DEFAULT_RESTART_AFTER_ZERO_CHECKS = 3;

export const CMUX_EXEC_TIMEOUT_MS = 30_000;
export const MAX_CONFIG_BYTES = 1024 * 1024;
const NOFOLLOW = fsConstants.O_NOFOLLOW ?? 0;
const UUID_PATTERN = /^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$/u;

const CONFIG_KEYS = new Set([
  'repoPath',
  'cmuxPath',
  'ompPath',
  'workspaceTitle',
  'ompProfile',
  'ownerId',
  'browserSessionId',
  'browserSurfaceId',
  'databasePath',
  'sessionDir',
  'resumeSessionPath',
]);

const PROFILE_PATTERN = /^[A-Za-z0-9._-]+$/;

const CONTROL_CHARS = /[\u0000-\u001f\u007f]/;

const SESSION_ID_PATTERN = /^(ses_[A-Za-z0-9]+|[0-9a-f]{40})$/;

function invalid(message) {
  throw new LocalOmpSupervisorError('E_CONFIG_INVALID', message);
}

function fail(kind, message) {
  throw new LocalOmpSupervisorError(kind, message);
}

function requireAbsoluteString(value, field) {
  if (typeof value !== 'string' || value.trim() === '') {
    invalid(`${field} must be a non-empty string`);
  }
  if (!isAbsolute(value)) {
    throw new LocalOmpSupervisorError('E_CONFIG_PATH_NOT_ABSOLUTE', `${field} must be an absolute path: ${value}`);
  }
  return value;
}

async function requireExisting(value, field, kind) {
  const path = requireAbsoluteString(value, field);
  let info;
  try {
    info = await stat(path);
  } catch {
    throw new LocalOmpSupervisorError('E_CONFIG_PATH_MISSING', `${field} does not exist: ${path}`);
  }
  if (kind === 'dir' && !info.isDirectory()) {
    invalid(`${field} is not a directory: ${path}`);
  }
  if (kind === 'file' && !info.isFile()) {
    invalid(`${field} is not a regular file: ${path}`);
  }
  if (kind === 'exec' && (info.mode & 0o111) === 0) {
    invalid(`${field} is not executable: ${path}`);
  }
  return path;
}

function requireNonEmptyString(value, field, { allowControl = true } = {}) {
  if (typeof value !== 'string' || value.length === 0) {
    invalid(`${field} must be a non-empty string`);
  }
  if (value.length > 200) {
    invalid(`${field} must be at most 200 characters`);
  }
  if (!allowControl && CONTROL_CHARS.test(value)) {
    invalid(`${field} contains control characters`);
  }
  return value;
}

/**
 * Loads and validates the owner-only worker config. The config must be a
 * regular file readable only by the owner (no group/other bits) and every
 * path must be absolute; existence is required for repo, cmux, omp, and
 * database paths, and for the optional exact resume session path.
 */
export async function loadWorkerConfig(options = {}) {
  const configPath = options.configPath ?? DEFAULT_CONFIG_PATH;
  const resolved = resolve(configPath);
  const parent = await lstat(dirname(resolved)).catch(() => {
    throw new LocalOmpSupervisorError('E_CONFIG_UNREADABLE', 'config parent is unreadable');
  });
  if (!parent.isDirectory()
    || parent.isSymbolicLink()
    || (parent.mode & 0o022) !== 0
    || (typeof process.getuid === 'function' && parent.uid !== process.getuid())) {
    throw new LocalOmpSupervisorError('E_CONFIG_INSECURE_MODE', 'config parent must be owner-controlled');
  }

  let handle;
  let raw;
  try {
    handle = await open(resolved, fsConstants.O_RDONLY | NOFOLLOW);
    const [info, linked] = await Promise.all([handle.stat(), lstat(resolved)]);
    if (!info.isFile()
      || !linked.isFile()
      || info.dev !== linked.dev
      || info.ino !== linked.ino
      || info.size > MAX_CONFIG_BYTES
      || (info.mode & 0o077) !== 0
      || (typeof process.getuid === 'function' && info.uid !== process.getuid())) {
      throw new LocalOmpSupervisorError('E_CONFIG_INSECURE_MODE', 'config must be an owner-only regular file');
    }
    raw = await handle.readFile('utf8');
  } catch (error) {
    if (error instanceof LocalOmpSupervisorError) throw error;
    throw new LocalOmpSupervisorError('E_CONFIG_UNREADABLE', 'config is unreadable');
  } finally {
    await handle?.close();
  }
  let parsed;
  try {
    parsed = JSON.parse(raw);
  } catch {
    throw new LocalOmpSupervisorError('E_CONFIG_PARSE', 'config is not valid JSON');
  }
  if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
    invalid('config must be a JSON object');
  }
  for (const key of Object.keys(parsed)) {
    if (!CONFIG_KEYS.has(key)) invalid(`unknown config key: ${key}`);
  }

  const repoPath = await requireExisting(parsed.repoPath, 'repoPath', 'dir');
  const cmuxPath = await requireExisting(parsed.cmuxPath, 'cmuxPath', 'exec');
  const ompPath = await requireExisting(parsed.ompPath, 'ompPath', 'exec');
  const databasePath = await requireExisting(parsed.databasePath, 'databasePath', 'file');
  const sessionDir = requireAbsoluteString(parsed.sessionDir, 'sessionDir');

  const workspaceTitle = requireNonEmptyString(parsed.workspaceTitle, 'workspaceTitle', { allowControl: false });
  if (workspaceTitle !== workspaceTitle.trim()) invalid('workspaceTitle must not have leading or trailing whitespace');
  const ompProfile = requireNonEmptyString(parsed.ompProfile, 'ompProfile', { allowControl: false });
  if (!PROFILE_PATTERN.test(ompProfile)) invalid(`ompProfile must match ${PROFILE_PATTERN}`);
  const ownerId = requireNonEmptyString(parsed.ownerId, 'ownerId', { allowControl: false });
  const browserSessionId = requireNonEmptyString(parsed.browserSessionId, 'browserSessionId', { allowControl: false });
  const browserSurfaceId = requireNonEmptyString(
    parsed.browserSurfaceId,
    'browserSurfaceId',
    { allowControl: false },
  );
  if (!UUID_PATTERN.test(browserSurfaceId)) invalid('browserSurfaceId must be a UUID');

  let resumeSessionPath;
  if (parsed.resumeSessionPath !== undefined) {
    resumeSessionPath = await requireExisting(parsed.resumeSessionPath, 'resumeSessionPath', 'path');
  }

  return Object.freeze({
    configPath: resolved,
    repoPath,
    cmuxPath,
    ompPath,
    databasePath,
    sessionDir,
    workspaceTitle,
    ompProfile,
    ownerId,
    browserSessionId,
    browserSurfaceId,
    resumeSessionPath,
  });
}

/**
 * POSIX single-quote shell escaping: the value is embedded literally inside
 * single quotes, so the produced text is one shell word regardless of quotes,
 * spaces, dollar signs, backticks, or newlines.
 */
export function shellQuote(value) {
  return `'${String(value).replaceAll("'", "'\\''")}'`;
}

/**
 * The concise canonical worker prompt. It requires the recovery-first loop,
 * lease heartbeat, the local CMUX browser surface, the audited submission
 * path, canonical terminal persistence, and idle waitForOmpWake, and it
 * carries the exact owner and browser-session ids.
 */
export function buildWorkerPrompt(config) {
  return [
    `Persistent AugGood worker for owner ${config.ownerId} and browser session ${config.browserSessionId}.`,
    'On every startup call recoverPrepareOrClaimBacklogRun with database ' + config.databasePath +
      ' and exactly these ownerId and browserSessionId values, following skills/application-prep/SKILL.md and AGENTS.md.',
    'Recover the sole active run first; otherwise prepare and atomically claim one queued job.',
    'While a run is active, call heartbeatActiveRun on an interval strictly shorter than half the lease duration.',
    `Use only CMUX browser surface ${config.browserSurfaceId} as the headed browser. Before every startup, resolve it with cmux identify --surface and reject any identity mismatch; never close the browser surface or workspace.`,
    'Submit only through the audited path: prepareSubmission, beginFinalSubmit, OMP click of the exact authorized final ref, completeFinalSubmit.',
    'Persist exactly one canonical terminal outcome through persistTerminalOutcome, then immediately rerun startup.',
    'When startup returns kind idle, call waitForOmpWake with a bounded timeout and rerun startup whenever a wake arrives or the idle timeout elapses.',
  ].join(' ');
}

/**
 * Builds the OMP worker invocation.
 * `--resume <path>` is used when configured. Otherwise exactly one existing
 * profile session is continued, zero starts a new session, and multiple
 * sessions fail closed as ambiguous. The prompt is the final positional message.
 */
export function buildOmpInvocation({ config, sessionCount }) {
  const args = [
    config.ompPath,
    '--profile',
    config.ompProfile,
    '--cwd',
    config.repoPath,
    '--auto-approve',
    '--approval-mode',
    'yolo',
    '--session-dir',
    config.sessionDir,
  ];
  if (config.resumeSessionPath) {
    args.push('--resume', config.resumeSessionPath);
  } else if (sessionCount === 1) {
    args.push('--continue');
  } else if (sessionCount > 1) {
    fail('E_SESSION_AMBIGUOUS', `found ${sessionCount} sessions; configure resumeSessionPath`);
  }
  args.push(buildWorkerPrompt(config));
  return {
    args,
    command: args.map(shellQuote).join(' '),
  };
}

/**
 * Counts existing OMP sessions for the isolated profile. Sessions live under
 * `<sessionDir>/storage/session/` (opencode-compatible layout); when that is
 * absent, `<sessionDir>` itself is inspected. Only session-looking
 * directories (`ses_*` or 40-hex ids) count; the `global` storage entry is
 * excluded.
 */
export async function countProfileSessions(sessionDir) {
  for (const candidate of [join(sessionDir, 'storage', 'session'), sessionDir]) {
    let entries;
    try {
      entries = await readdir(candidate, { withFileTypes: true });
    } catch {
      continue;
    }
    return entries.filter((entry) => entry.isDirectory() && SESSION_ID_PATTERN.test(entry.name)).length;
  }
  return 0;
}

/** Parses structured `cmux tree --all --json --id-format uuids` output. */
export function parseTreeJson(text) {
  let parsed;
  try {
    parsed = JSON.parse(text);
  } catch (error) {
    throw new LocalOmpSupervisorError('E_MALFORMED_TREE', `tree --json output is not valid JSON: ${String(error?.message ?? error)}`);
  }
  if (!parsed || typeof parsed !== 'object' || !Array.isArray(parsed.windows)) {
    throw new LocalOmpSupervisorError('E_MALFORMED_TREE', 'tree --json output has no windows array');
  }
  const workspaces = [];
  for (const win of parsed.windows) {
    if (!win || typeof win !== 'object' || !Array.isArray(win.workspaces)) {
      throw new LocalOmpSupervisorError('E_MALFORMED_TREE', 'tree window entry is missing the workspaces array');
    }
    for (const ws of win.workspaces) {
      if (!ws || typeof ws !== 'object') {
        throw new LocalOmpSupervisorError('E_MALFORMED_TREE', 'tree workspace entry is not an object');
      }
      const id = ws.id ?? ws.ref;
      const title = ws.title;
      if (typeof id !== 'string' || id.length === 0) {
        throw new LocalOmpSupervisorError('E_MALFORMED_TREE', 'tree workspace entry is missing an id');
      }
      if (typeof title !== 'string') {
        throw new LocalOmpSupervisorError('E_MALFORMED_TREE', `tree workspace ${id} is missing a title`);
      }
      const surfaceIds = [];
      const paneIds = [];
      let terminalSurfaceId = null;
      for (const pane of Array.isArray(ws.panes) ? ws.panes : []) {
        const paneId = pane?.id ?? pane?.ref;
        if (typeof paneId === 'string' && paneId.length > 0) paneIds.push(paneId);
        for (const surface of Array.isArray(pane?.surfaces) ? pane.surfaces : []) {
          const surfaceId = surface?.id ?? surface?.ref;
          if (typeof surfaceId !== 'string' || surfaceId.length === 0) {
            throw new LocalOmpSupervisorError('E_MALFORMED_TREE', `tree surface entry is missing an id in workspace ${id}`);
          }
          surfaceIds.push(surfaceId);
          if (terminalSurfaceId === null && surface?.type === 'terminal') terminalSurfaceId = surfaceId;
        }
      }
      workspaces.push({ id, title, paneIds, surfaceIds, terminalSurfaceId });
    }
  }
  return { workspaces };
}

/**
 * Parses structured `cmux top --all --processes --flat --id-format uuids`
 * output. Rows are TSV: cpu_percent, memory_bytes, process_count, kind, ref,
 * parent_ref, title.
 */
export function parseTopFlat(text) {
  const rows = [];
  const lines = String(text).split('\n');
  for (let i = 0; i < lines.length; i += 1) {
    const line = lines[i];
    if (line.trim() === '') continue;
    const fields = line.split('\t');
    if (fields.length < 6) {
      throw new LocalOmpSupervisorError('E_MALFORMED_TOP_ROW', `top --flat row ${i + 1} has ${fields.length} columns`);
    }
    rows.push({
      cpu: fields[0],
      memoryBytes: fields[1],
      count: fields[2],
      kind: fields[3],
      ref: fields[4],
      parentRef: fields[5],
      title: fields.slice(6).join('\t'),
    });
  }
  return { rows };
}

/** Exact-title workspace matches; the caller fails closed on more than one. */
export function findWorkerWorkspaces(workspaces, title) {
  return workspaces.filter((workspace) => workspace.title === title);
}

/**
 * OMP processes belonging to one workspace: process rows whose parent chain
 * (surface/container id, or transitively a parent pid row) reaches any id in
 * `containerIds`. Only the configured process name counts.
 */
export function findOmpProcessesInWorkspace({ rows, containerIds, processName }) {
  const containers = new Set(containerIds);
  const processRows = rows.filter((row) => row.kind === 'process');
  const byPid = new Map();
  for (const row of processRows) byPid.set(row.ref, row);
  const membership = new Map();
  const belongs = (row) => {
    if (membership.has(row.ref)) return membership.get(row.ref);
    membership.set(row.ref, false); // break pid cycles
    let result = containers.has(row.parentRef);
    if (!result) {
      const parent = byPid.get(row.parentRef);
      if (parent) result = belongs(parent);
    }
    membership.set(row.ref, result);
    return result;
  };
  return processRows.filter((row) => belongs(row) && row.title === processName);
}

/**
 * Deterministic per-check decision. Fails closed on duplicate matching
 * workspaces or more than one OMP process; otherwise returns the action and
 * the next zero-streak: 'create' (no workspace), 'healthy' (one OMP
 * process), 'wait' (transient zero, grace window, or streak still building),
 * or 'restart' (workspace present but OMP has been absent long enough).
 */
export function decideWorkerAction({ state, createdRecently = false, zeroStreak = 0, restartAfterZeroChecks = DEFAULT_RESTART_AFTER_ZERO_CHECKS }) {
  const threshold = restartAfterZeroChecks >= 1 ? restartAfterZeroChecks : DEFAULT_RESTART_AFTER_ZERO_CHECKS;
  const workspaceCount = state.workerWorkspaces.length;
  if (workspaceCount > 1) {
    throw new LocalOmpSupervisorError('E_WORKSPACE_DUPLICATE', `found ${workspaceCount} workspaces titled '${state.workerWorkspaces[0]?.title ?? '?'}'; expected exactly one`);
  }
  if (workspaceCount === 0) {
    return { action: 'create', zeroStreak: 0 };
  }
  const ompCount = state.ompProcesses.length;
  if (ompCount > 1) {
    throw new LocalOmpSupervisorError('E_OMP_PROCESS_DUPLICATE', `found ${ompCount} OMP processes in the worker workspace; expected at most one`);
  }
  if (ompCount === 1) {
    return { action: 'healthy', zeroStreak: 0 };
  }
  if (createdRecently) {
    return { action: 'wait', zeroStreak: 0 };
  }
  if (zeroStreak + 1 < threshold) {
    return { action: 'wait', zeroStreak: zeroStreak + 1 };
  }
  return { action: 'restart', zeroStreak: 0 };
}

function defaultExecFile(file, args, options) {
  return execFileAsync(file, args, options);
}

async function runCmux(cmuxPath, args, { execFile = defaultExecFile, signal, timeoutMs = CMUX_EXEC_TIMEOUT_MS }) {
  try {
    const { stdout } = await execFile(cmuxPath, args, {
      encoding: 'utf8',
      maxBuffer: 16 * 1024 * 1024,
      timeout: timeoutMs,
      signal,
    });
    return stdout;
  } catch (error) {
    if (signal?.aborted) {
      throw new LocalOmpSupervisorError('E_ABORTED', `cmux ${args[0]} aborted`);
    }
    throw new LocalOmpSupervisorError('E_CMUX_FAILED', `cmux ${args[0]} failed: ${String(error?.message ?? error)}`);
  }
}

/**
 * Queries CMUX state for the worker workspace: structured tree, then top
 * scoped to the matching workspace. The top query is skipped when no
 * workspace exists (nothing to scope against).
 */
export async function checkWorkerState(config, options = {}) {
  const { execFile = defaultExecFile, signal, timeoutMs = CMUX_EXEC_TIMEOUT_MS } = options;
  const treeText = await runCmux(config.cmuxPath, CMUX_TREE_ARGS, { execFile, signal, timeoutMs });
  const tree = parseTreeJson(treeText);
  const matches = findWorkerWorkspaces(tree.workspaces, config.workspaceTitle);
  if (matches.length > 1) {
    throw new LocalOmpSupervisorError('E_WORKSPACE_DUPLICATE', `found ${matches.length} workspaces titled '${config.workspaceTitle}'; expected exactly one`);
  }
  if (matches.length === 0) {
    return { workerWorkspaces: [], ompProcesses: [], queriedTop: false };
  }
  const topText = await runCmux(config.cmuxPath, CMUX_TOP_ARGS, { execFile, signal, timeoutMs });
  const top = parseTopFlat(topText);
  const workspace = matches[0];
  const containerIds = [workspace.id, ...workspace.paneIds, ...workspace.surfaceIds];
  const ompProcesses = findOmpProcessesInWorkspace({
    rows: top.rows,
    containerIds,
    processName: basename(config.ompPath),
  });
  return { workerWorkspaces: matches, ompProcesses, queriedTop: true };
}

/** Creates exactly one worker workspace; the invocation is the shell-quoted OMP command. */
export async function createWorkerWorkspace(config, options = {}) {
  const { command, execFile = defaultExecFile, signal, timeoutMs = CMUX_EXEC_TIMEOUT_MS } = options;
  const args = ['new-workspace', '--name', config.workspaceTitle, '--cwd', config.repoPath, '--command', command];
  await runCmux(config.cmuxPath, args, { execFile, signal, timeoutMs });
}

/** Re-invokes the worker command in the existing workspace's terminal surface. */
export async function restartOmpInWorkspace(config, options = {}) {
  const { workspaceId, surfaceId, command, execFile = defaultExecFile, signal, timeoutMs = CMUX_EXEC_TIMEOUT_MS } = options;
  const args = ['send', '--workspace', workspaceId, '--surface', surfaceId, `${command}\n`];
  await runCmux(config.cmuxPath, args, { execFile, signal, timeoutMs });
}

/** Forwards SIGTERM to the known OMP worker pids (best effort), then returns them. */
export function forwardSigterm(kill, pids) {
  const forwarded = [];
  for (const pid of pids) {
    try {
      kill(Number(pid), 'SIGTERM');
      forwarded.push(String(pid));
    } catch {
      // Process already gone; forwarding is best effort.
    }
  }
  return forwarded;
}

function sleepAbortable(ms, signal) {
  return new Promise((resolvePromise) => {
    if (signal.aborted) {
      resolvePromise();
      return;
    }
    const timer = setTimeout(() => {
      signal.removeEventListener('abort', onAbort);
      resolvePromise();
    }, ms);
    const onAbort = () => {
      clearTimeout(timer);
      resolvePromise();
    };
    signal.addEventListener('abort', onAbort, { once: true });
  });
}

/**
 * Runs the supervision loop. On SIGTERM (launchd stop), the SIGTERM is
 * forwarded to the known OMP worker pid and the loop exits gracefully with
 * `{ kind: 'terminated' }`; it never closes the browser or the workspace.
 */
export async function runSupervisor(options = {}) {
  const config = await loadWorkerConfig({ configPath: options.configPath });
  const controller = new AbortController();
  const externalSignal = options.signal ?? null;
  const signal = externalSignal ?? controller.signal;
  const kill = options.kill ?? ((pid, sig) => process.kill(pid, sig));
  const log = options.log ?? (() => {});
  const execFile = options.execFile ?? defaultExecFile;
  const checkIntervalMs = options.checkIntervalMs ?? DEFAULT_CHECK_INTERVAL_MS;
  const spawnGraceMs = options.spawnGraceMs ?? DEFAULT_SPAWN_GRACE_MS;
  const restartAfterZeroChecks = options.restartAfterZeroChecks ?? DEFAULT_RESTART_AFTER_ZERO_CHECKS;
  const maxChecks = options.maxChecks ?? Number.POSITIVE_INFINITY;
  const timeoutMs = options.cmuxTimeoutMs ?? CMUX_EXEC_TIMEOUT_MS;

  const terminateSignal = options.terminateSignal ?? 'SIGTERM';
  let lastOmpPids = [];
  const onSigterm = () => {
    forwardSigterm(kill, lastOmpPids);
    controller.abort();
  };
  if (!externalSignal) {
    process.on(terminateSignal, onSigterm);
  }
  try {
    await mkdir(config.sessionDir, { recursive: true, mode: 0o700 });
    let zeroStreak = 0;
    let createdAt = 0;
    let checks = 0;
    while (true) {
      if (signal.aborted) return { kind: 'terminated' };
      if (checks >= maxChecks) return { kind: 'max-checks', checks };
      checks += 1;
      try {
        const state = await checkWorkerState(config, { execFile, signal, timeoutMs });
        lastOmpPids = state.ompProcesses.map((processRow) => processRow.ref);
        const decision = decideWorkerAction({
          state,
          createdRecently: createdAt !== 0 && Date.now() - createdAt < spawnGraceMs,
          zeroStreak,
          restartAfterZeroChecks,
        });
        zeroStreak = decision.zeroStreak;
        if (decision.action === 'create') {
          const sessionCount = await countProfileSessions(config.sessionDir);
          const invocation = buildOmpInvocation({ config, sessionCount });
          await createWorkerWorkspace(config, { command: invocation.command, execFile, signal, timeoutMs });
          createdAt = Date.now();
          log(`worker workspace '${config.workspaceTitle}' created with ${sessionCount} existing session(s)`);
        } else if (decision.action === 'restart') {
          const sessionCount = await countProfileSessions(config.sessionDir);
          const invocation = buildOmpInvocation({ config, sessionCount });
          const workspace = state.workerWorkspaces[0];
          if (!workspace.terminalSurfaceId) {
            throw new LocalOmpSupervisorError('E_NO_TERMINAL_SURFACE', `worker workspace '${config.workspaceTitle}' has no terminal surface to restart OMP in`);
          }
          await restartOmpInWorkspace(config, {
            workspaceId: workspace.id,
            surfaceId: workspace.terminalSurfaceId,
            command: invocation.command,
            execFile,
            signal,
            timeoutMs,
          });
          log(`worker OMP restarted in workspace '${config.workspaceTitle}' with ${sessionCount} existing session(s)`);
        }
      } catch (error) {
        if (signal.aborted) return { kind: 'terminated' };
        throw error;
      }
      await sleepAbortable(checkIntervalMs, signal);
    }
  } finally {
    if (!externalSignal) {
      process.off(terminateSignal, onSigterm);
    }
  }
}

// CLI entry point: launchd invokes this file directly. SIGTERM exits 0
// (graceful); any other exit is a failure so launchd restarts throttled.
if (process.argv[1] && resolve(process.argv[1]) === resolve(import.meta.filename ?? '')) {
  runSupervisor()
    .then((result) => {
      process.exitCode = result.kind === 'terminated' ? 0 : 1;
    })
    .catch((error) => {
      const code = error instanceof LocalOmpSupervisorError ? error.code : 'E_UNEXPECTED';
      console.error(`local-omp-supervisor: ${code}: ${error?.message ?? error}`);
      process.exitCode = 1;
    });
}
