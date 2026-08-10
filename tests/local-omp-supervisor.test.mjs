import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { readFileSync } from 'node:fs';
import { chmod, mkdir, mkdtemp, rm, symlink, writeFile } from 'node:fs/promises';
import { homedir, tmpdir } from 'node:os';
import { dirname, join, resolve } from 'node:path';
import test from 'node:test';
import { fileURLToPath } from 'node:url';
import {
  buildOmpInvocation,
  buildWorkerPrompt,
  CMUX_TOP_ARGS,
  CMUX_TREE_ARGS,
  checkWorkerState,
  countProfileSessions,
  createWorkerWorkspace,
  decideWorkerAction,
  DEFAULT_CONFIG_FILENAME,
  DEFAULT_CONFIG_PATH,
  findOmpProcessesInWorkspace,
  findWorkerWorkspaces,
  forwardSigterm,
  loadWorkerConfig,
  LocalOmpSupervisorError,
  parseTopFlat,
  parseTreeJson,
  restartOmpInWorkspace,
  runSupervisor,
  shellQuote,
} from '../src/scheduler/local-omp-supervisor.mjs';

const REPO_ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const BROWSER_SURFACE_ID = '33333333-3333-4333-8333-333333333333';

function failCode(promise, code) {
  return promise.then(
    () => assert.fail(`expected ${code}`),
    (error) => {
      assert.ok(error instanceof LocalOmpSupervisorError, `expected LocalOmpSupervisorError, got ${error}`);
      assert.equal(error.code, code);
      return error;
    },
  );
}

async function makeEnv(overrides = {}) {
  const root = await mkdtemp(join(tmpdir(), 'omp-supervisor-'));
  const repo = join(root, 'repo');
  await mkdir(repo);
  const cmuxPath = join(root, 'cmux');
  const ompPath = join(root, 'omp');
  await writeFile(cmuxPath, '#!/bin/sh\nexit 0\n');
  await writeFile(ompPath, '#!/bin/sh\nexit 0\n');
  await chmod(cmuxPath, 0o700);
  await chmod(ompPath, 0o700);
  const databasePath = join(root, 'AugGood.sqlite');
  await writeFile(databasePath, '');
  const sessionDir = join(root, 'sessions');
  const config = {
    repoPath: repo,
    cmuxPath,
    ompPath,
    workspaceTitle: 'AugGood Worker',
    ompProfile: 'auggood-worker',
    ownerId: 'ian-omp-backlog',
    browserSessionId: 'auggood-canonical-loop',
    browserSurfaceId: BROWSER_SURFACE_ID,
    databasePath,
    sessionDir,
    ...overrides,
  };
  return { root, repo, cmuxPath, ompPath, databasePath, sessionDir, config };
}

async function writeConfig(env, overrides = {}) {
  const configPath = join(env.root, 'auggood-worker.json');
  await writeFile(configPath, JSON.stringify({ ...env.config, ...overrides }, null, 2));
  await chmod(configPath, 0o600);
  return configPath;
}

function wsObj(id, title, surfaces = [{ id: `S-${id}`, type: 'terminal' }]) {
  return {
    id,
    index: 0,
    title,
    pinned: false,
    selected: false,
    panes: [{ id: `P-${id}`, index: 0, surface_count: surfaces.length, surfaces }],
  };
}

function treeText(...workspaces) {
  return JSON.stringify({
    active: { workspace_id: workspaces[0]?.id ?? null },
    caller: {},
    windows: [{ id: 'WIN1', index: 0, visible: true, workspace_count: workspaces.length, workspaces }],
  });
}

function topLine(kind, ref, parentRef, title) {
  return `1.0\t1024\t1\t${kind}\t${ref}\t${parentRef}\t${title}`;
}

function topText(lines) {
  return `${lines.join('\n')}\n`;
}

function healthyFakeExec(calls, ompPid = '4242', workspaceId = 'WS1', surfaceId = 'S-WS1') {
  const tree = treeText(wsObj(workspaceId, 'AugGood Worker', [{ id: surfaceId, type: 'terminal' }]));
  const top = topText([
    topLine('total', 'total', '', ''),
    topLine('window', 'WIN1', '', 'total'),
    topLine('workspace', workspaceId, 'WIN1', 'AugGood Worker'),
    topLine('pane', `P-${workspaceId}`, workspaceId, ''),
    topLine('surface', surfaceId, `P-${workspaceId}`, 'worker'),
    topLine('process', ompPid, surfaceId, 'omp'),
  ]);
  return async (file, args) => {
    calls.push(args);
    if (args[0] === 'tree') return { stdout: tree };
    if (args[0] === 'top') return { stdout: top };
    return { stdout: '' };
  };
}

function pollUntil(fn, timeoutMs = 5_000) {
  const started = Date.now();
  return new Promise((resolvePromise, reject) => {
    const tick = () => {
      if (fn()) return resolvePromise();
      if (Date.now() - started > timeoutMs) return reject(new Error('pollUntil timed out'));
      setTimeout(tick, 5);
    };
    tick();
  });
}

// ---------------------------------------------------------------------------
// Secure config
// ---------------------------------------------------------------------------

test('supervisor: default config path lives under private/ in the repo', () => {
  assert.equal(DEFAULT_CONFIG_FILENAME, 'auggood-worker.json');
  assert.ok(DEFAULT_CONFIG_PATH.endsWith(join('private', 'auggood-worker.json')));
});

test('supervisor: valid owner-only config loads with all required fields', async () => {
  const env = await makeEnv();
  try {
    const configPath = await writeConfig(env);
    const config = await loadWorkerConfig({ configPath });
    assert.equal(config.repoPath, env.repo);
    assert.equal(config.cmuxPath, env.cmuxPath);
    assert.equal(config.ompPath, env.ompPath);
    assert.equal(config.databasePath, env.databasePath);
    assert.equal(config.sessionDir, env.sessionDir);
    assert.equal(config.workspaceTitle, 'AugGood Worker');
    assert.equal(config.ompProfile, 'auggood-worker');
    assert.equal(config.ownerId, 'ian-omp-backlog');
    assert.equal(config.browserSessionId, 'auggood-canonical-loop');
    assert.equal(config.browserSurfaceId, BROWSER_SURFACE_ID);
    assert.equal(config.resumeSessionPath, undefined);
    assert.ok(Object.isFrozen(config));
  } finally {
    await rm(env.root, { recursive: true, force: true });
  }
});

test('supervisor: missing config fails closed', async () => {
  const env = await makeEnv();
  try {
    await failCode(loadWorkerConfig({ configPath: join(env.root, 'nope.json') }), 'E_CONFIG_UNREADABLE');
  } finally {
    await rm(env.root, { recursive: true, force: true });
  }
});

test('supervisor: group/world-readable config is rejected as insecure', async () => {
  const env = await makeEnv();
  try {
    const configPath = await writeConfig(env);
    await chmod(configPath, 0o644);
    await failCode(loadWorkerConfig({ configPath }), 'E_CONFIG_INSECURE_MODE');
  } finally {
    await rm(env.root, { recursive: true, force: true });
  }
});

test('supervisor: symlinked config is rejected', async () => {
  const env = await makeEnv();
  try {
    const target = await writeConfig(env);
    const link = join(env.root, 'worker-link.json');
    await symlink(target, link);
    await failCode(loadWorkerConfig({ configPath: link }), 'E_CONFIG_UNREADABLE');
  } finally {
    await rm(env.root, { recursive: true, force: true });
  }
});

test('supervisor: malformed JSON config fails closed', async () => {
  const env = await makeEnv();
  try {
    const configPath = join(env.root, 'bad.json');
    await writeFile(configPath, '{not json');
    await chmod(configPath, 0o600);
    await failCode(loadWorkerConfig({ configPath }), 'E_CONFIG_PARSE');
  } finally {
    await rm(env.root, { recursive: true, force: true });
  }
});

test('supervisor: relative paths and missing paths are rejected', async () => {
  const env = await makeEnv();
  try {
    const relative = await writeConfig(env, { repoPath: 'relative/repo' });
    await failCode(loadWorkerConfig({ configPath: relative }), 'E_CONFIG_PATH_NOT_ABSOLUTE');

    const missing = await writeConfig(env, { repoPath: join(env.root, 'does-not-exist') });
    await failCode(loadWorkerConfig({ configPath: missing }), 'E_CONFIG_PATH_MISSING');

    const notExec = await writeConfig(env, { ompPath: env.databasePath });
    await failCode(loadWorkerConfig({ configPath: notExec }), 'E_CONFIG_INVALID');
  } finally {
    await rm(env.root, { recursive: true, force: true });
  }
});

test('supervisor: unknown keys, bad profile charset, and bad titles are rejected', async () => {
  const env = await makeEnv();
  try {
    const unknown = await writeConfig(env, { surprise: true });
    await failCode(loadWorkerConfig({ configPath: unknown }), 'E_CONFIG_INVALID');

    const badProfile = await writeConfig(env, { ompProfile: 'aug good' });
    await failCode(loadWorkerConfig({ configPath: badProfile }), 'E_CONFIG_INVALID');

    const paddedTitle = await writeConfig(env, { workspaceTitle: '  spaced  ' });
    await failCode(loadWorkerConfig({ configPath: paddedTitle }), 'E_CONFIG_INVALID');
  } finally {
    await rm(env.root, { recursive: true, force: true });
  }
});

test('supervisor: optional resume session path must be exact, absolute, and existing', async () => {
  const env = await makeEnv();
  try {
    const resumePath = join(env.root, 'resume-session');
    await mkdir(resumePath);

    const valid = await writeConfig(env, { resumeSessionPath: resumePath });
    const config = await loadWorkerConfig({ configPath: valid });
    assert.equal(config.resumeSessionPath, resumePath);

    const relative = await writeConfig(env, { resumeSessionPath: 'relative/session' });
    await failCode(loadWorkerConfig({ configPath: relative }), 'E_CONFIG_PATH_NOT_ABSOLUTE');

    const missing = await writeConfig(env, { resumeSessionPath: join(env.root, 'absent-session') });
    await failCode(loadWorkerConfig({ configPath: missing }), 'E_CONFIG_PATH_MISSING');
  } finally {
    await rm(env.root, { recursive: true, force: true });
  }
});

// ---------------------------------------------------------------------------
// Shell-safe command construction
// ---------------------------------------------------------------------------

test('supervisor: shellQuote escapes quotes, dollars, backticks, and newlines', () => {
  assert.equal(shellQuote("a'b"), `'a'\\''b'`);
  assert.equal(shellQuote('$(rm -rf /)'), `'$(rm -rf /)'`);
  assert.equal(shellQuote('`id`'), "'`id`'");
  assert.equal(shellQuote('line1\nline2'), "'line1\nline2'");
  assert.equal(shellQuote(''), "''");
});

test('supervisor: invocation command round-trips through a real shell as one argv', async () => {
  const weird = `wrk'dir$(echo pwned)\`id\` "quoted"`;
  const env = await makeEnv({ workspaceTitle: 'AugGood Worker' });
  const argvFile = join(env.root, 'argv.txt');
  try {
    const repo = join(env.root, weird);
    await mkdir(repo);
    const ompPath = join(repo, "om'p");
    await writeFile(ompPath, '#!/bin/sh\n{ printf "%s\\n" "$0"; printf "%s\\n" "$@"; } > "$OMP_ARGV_FILE"\n');
    await chmod(ompPath, 0o700);
    const cmuxPath = join(repo, 'cmux');
    await writeFile(cmuxPath, '#!/bin/sh\nexit 0\n');
    await chmod(cmuxPath, 0o700);
    const databasePath = join(repo, 'AugGood.sqlite');
    await writeFile(databasePath, '');
    const sessionDir = join(repo, 'sessions');
    const config = {
      ...env.config,
      repoPath: repo,
      ompPath,
      cmuxPath,
      databasePath,
      sessionDir,
      ownerId: "ian'$(echo oops)",
      browserSessionId: 'auggood-canonical-loop',
    };
    const invocation = buildOmpInvocation({ config, sessionCount: 0 });
    execFileSync('/bin/sh', ['-c', invocation.command], {
      env: { ...process.env, OMP_ARGV_FILE: argvFile, PATH: '/usr/bin:/bin' },
    });
    const actual = readFileSync(argvFile, 'utf8').trimEnd().split('\n');
    assert.deepEqual(actual, invocation.args);
  } finally {
    await rm(env.root, { recursive: true, force: true });
  }
});

// ---------------------------------------------------------------------------
// Exact resume / isolated continue rules
// ---------------------------------------------------------------------------

test('supervisor: base invocation flags are exact and the prompt is the final message', async () => {
  const env = await makeEnv();
  try {
    const { args, command } = buildOmpInvocation({ config: env.config, sessionCount: 1 });
    assert.equal(args[0], env.config.ompPath);
    assert.deepEqual(args.slice(1, 9), [
      '--profile',
      'auggood-worker',
      '--cwd',
      env.repo,
      '--auto-approve',
      '--approval-mode',
      'yolo',
      '--session-dir',
    ]);
    assert.equal(args[9], env.sessionDir);
    assert.equal(args[args.length - 1], buildWorkerPrompt(env.config));
    assert.equal(command, args.map(shellQuote).join(' '));
    assert.ok(command.startsWith(`'${env.config.ompPath}'`));
  } finally {
    await rm(env.root, { recursive: true, force: true });
  }
});

test('supervisor: --resume wins when an exact resume session path is configured', async () => {
  const env = await makeEnv();
  try {
    const resumePath = join(env.root, 'resume-session');
    await mkdir(resumePath);
    for (const sessionCount of [0, 1, 2, 5]) {
      const { args } = buildOmpInvocation({ config: { ...env.config, resumeSessionPath: resumePath }, sessionCount });
      assert.ok(args.includes('--resume'), `sessionCount ${sessionCount}`);
      assert.equal(args[args.indexOf('--resume') + 1], resumePath);
      assert.ok(!args.includes('--continue'));
    }
  } finally {
    await rm(env.root, { recursive: true, force: true });
  }
});

test('supervisor: session continuation is deterministic and ambiguous state fails closed', async () => {
  const env = await makeEnv();
  try {
    const none = buildOmpInvocation({ config: env.config, sessionCount: 0 });
    assert.ok(!none.args.includes('--continue'));
    assert.ok(!none.args.includes('--resume'));

    const one = buildOmpInvocation({ config: env.config, sessionCount: 1 });
    assert.ok(one.args.includes('--continue'));

    for (const sessionCount of [2, 9]) {
      assert.throws(
        () => buildOmpInvocation({ config: env.config, sessionCount }),
        (error) => error instanceof LocalOmpSupervisorError
          && error.code === 'E_SESSION_AMBIGUOUS',
      );
    }
  } finally {
    await rm(env.root, { recursive: true, force: true });
  }
});

test('supervisor: prompt requires the full canonical loop and exact ids', async () => {
  const env = await makeEnv();
  try {
    const prompt = buildWorkerPrompt(env.config);
    assert.ok(prompt.includes(env.config.ownerId));
    assert.ok(prompt.includes(env.config.browserSessionId));
    assert.ok(prompt.includes(env.config.browserSurfaceId));
    assert.ok(prompt.includes(env.config.databasePath));
    for (const token of [
      'recoverPrepareOrClaimBacklogRun',
      'heartbeatActiveRun',
      'CMUX browser surface',
      'prepareSubmission',
      'persistTerminalOutcome',
      'waitForOmpWake',
    ]) {
      assert.ok(prompt.includes(token), `prompt missing ${token}`);
    }
  } finally {
    await rm(env.root, { recursive: true, force: true });
  }
});

test('supervisor: countProfileSessions counts only session dirs in the right layout', async () => {
  const env = await makeEnv();
  try {
    assert.equal(await countProfileSessions(env.sessionDir), 0);

    await mkdir(join(env.sessionDir, 'storage', 'session'), { recursive: true });
    assert.equal(await countProfileSessions(env.sessionDir), 0);

    await mkdir(join(env.sessionDir, 'storage', 'session', '0a1b2c3d4e5f60718293a4b5c6d7e8f901234567'));
    await mkdir(join(env.sessionDir, 'storage', 'session', 'ses_4b2dd571dffePPbu2RPcWnQ51e'));
    await mkdir(join(env.sessionDir, 'storage', 'session', 'global'));
    assert.equal(await countProfileSessions(env.sessionDir), 2);

    await mkdir(join(env.sessionDir, 'message'));
    await mkdir(join(env.sessionDir, 'not-a-session'));
    assert.equal(await countProfileSessions(env.sessionDir), 2);
  } finally {
    await rm(env.root, { recursive: true, force: true });
  }
});

// ---------------------------------------------------------------------------
// Structured CMUX parsing
// ---------------------------------------------------------------------------

test('supervisor: parseTreeJson extracts workspaces, surfaces, and terminal surface', () => {
  const text = treeText(
    wsObj('WS-A', 'AugGood Worker', [
      { id: 'S-T1', type: 'terminal' },
      { id: 'S-B1', type: 'browser' },
    ]),
    wsObj('WS-B', 'Research', [{ id: 'S-T2', type: 'terminal' }]),
  );
  const { workspaces } = parseTreeJson(text);
  assert.equal(workspaces.length, 2);
  assert.deepEqual(workspaces[0], {
    id: 'WS-A',
    title: 'AugGood Worker',
    paneIds: ['P-WS-A'],
    surfaceIds: ['S-T1', 'S-B1'],
    terminalSurfaceId: 'S-T1',
  });
  assert.equal(workspaces[1].title, 'Research');
  assert.equal(workspaces[1].terminalSurfaceId, 'S-T2');
});

test('supervisor: malformed tree output fails closed', () => {
  assert.throws(() => parseTreeJson('{not json'), (error) => error.code === 'E_MALFORMED_TREE');
  assert.throws(() => parseTreeJson('{"active":{}}'), (error) => error.code === 'E_MALFORMED_TREE');
  assert.throws(
    () => parseTreeJson(treeText({ title: 'no id' })),
    (error) => error.code === 'E_MALFORMED_TREE',
  );
});

test('supervisor: parseTopFlat parses structured TSV rows and fails closed on malformed rows', () => {
  const { rows } = parseTopFlat(
    topText([
      topLine('total', 'total', '', ''),
      topLine('process', '4242', 'S-WS1', 'omp'),
      topLine('process', '51794', 'WIN1', 'cmux'),
    ]),
  );
  assert.equal(rows.length, 3);
  assert.equal(rows[1].kind, 'process');
  assert.equal(rows[1].ref, '4242');
  assert.equal(rows[1].parentRef, 'S-WS1');
  assert.equal(rows[1].title, 'omp');

  assert.equal(parseTopFlat('').rows.length, 0);
  assert.throws(() => parseTopFlat('only\tthree\tfields\n'), (error) => error.code === 'E_MALFORMED_TOP_ROW');
});

// ---------------------------------------------------------------------------
// Duplicate / zero / one worker states and the one-OMP-process invariant
// ---------------------------------------------------------------------------

test('supervisor: findWorkerWorkspaces matches exact titles', () => {
  const { workspaces } = parseTreeJson(treeText(wsObj('A', 'AugGood Worker'), wsObj('B', 'Other')));
  assert.equal(findWorkerWorkspaces(workspaces, 'AugGood Worker').length, 1);
  assert.equal(findWorkerWorkspaces(workspaces, 'Other').length, 1);
  assert.equal(findWorkerWorkspaces(workspaces, 'Missing').length, 0);
  const dup = parseTreeJson(treeText(wsObj('A', 'AugGood Worker'), wsObj('B', 'AugGood Worker')));
  assert.equal(findWorkerWorkspaces(dup.workspaces, 'AugGood Worker').length, 2);
});

test('supervisor: OMP processes are scoped to the worker workspace, including pid chains', () => {
  const rows = parseTopFlat(
    topText([
      topLine('process', '100', 'S-WS1', 'omp'),
      topLine('process', '101', '100', 'omp'), // nested omp child
      topLine('process', '200', 'S-OTHER', 'omp'), // other workspace
      topLine('process', '300', 'S-WS1', 'zsh'),
      topLine('process', '99999', '77777', 'omp'), // parent pid missing from snapshot
    ]),
  ).rows;
  const containerIds = ['WS1', 'P-WS1', 'S-WS1'];
  const found = findOmpProcessesInWorkspace({ rows, containerIds, processName: 'omp' });
  assert.deepEqual(
    found.map((row) => row.ref),
    ['100', '101'],
  );
  assert.equal(
    findOmpProcessesInWorkspace({ rows, containerIds, processName: 'zsh' }).length,
    1,
  );
});

test('supervisor: decideWorkerAction covers create, healthy, wait, restart, and fail-closed', () => {
  const oneWorkspace = [{ id: 'WS1', title: 'AugGood Worker' }];
  const oneOmp = [{ ref: '4242' }];

  assert.deepEqual(
    decideWorkerAction({ state: { workerWorkspaces: [], ompProcesses: [] } }),
    { action: 'create', zeroStreak: 0 },
  );
  assert.deepEqual(
    decideWorkerAction({ state: { workerWorkspaces: oneWorkspace, ompProcesses: oneOmp } }),
    { action: 'healthy', zeroStreak: 0 },
  );
  assert.deepEqual(
    decideWorkerAction({
      state: { workerWorkspaces: oneWorkspace, ompProcesses: [] },
      createdRecently: true,
      zeroStreak: 7,
    }),
    { action: 'wait', zeroStreak: 0 },
  );
  assert.deepEqual(
    decideWorkerAction({
      state: { workerWorkspaces: oneWorkspace, ompProcesses: [] },
      createdRecently: false,
      zeroStreak: 0,
      restartAfterZeroChecks: 3,
    }),
    { action: 'wait', zeroStreak: 1 },
  );
  assert.deepEqual(
    decideWorkerAction({
      state: { workerWorkspaces: oneWorkspace, ompProcesses: [] },
      createdRecently: false,
      zeroStreak: 2,
      restartAfterZeroChecks: 3,
    }),
    { action: 'restart', zeroStreak: 0 },
  );

  assert.throws(
    () => decideWorkerAction({ state: { workerWorkspaces: [oneWorkspace[0], oneWorkspace[0]], ompProcesses: [] } }),
    (error) => error.code === 'E_WORKSPACE_DUPLICATE',
  );
  assert.throws(
    () => decideWorkerAction({ state: { workerWorkspaces: oneWorkspace, ompProcesses: [oneOmp[0], oneOmp[0]] } }),
    (error) => error.code === 'E_OMP_PROCESS_DUPLICATE',
  );
});

test('supervisor: checkWorkerState uses structured queries and scopes top to the workspace', async () => {
  const env = await makeEnv();
  try {
    const configPath = await writeConfig(env);
    const config = await loadWorkerConfig({ configPath });
    const calls = [];
    const fake = healthyFakeExec(calls);
    const state = await checkWorkerState(config, { execFile: fake });
    assert.deepEqual(calls, [CMUX_TREE_ARGS, CMUX_TOP_ARGS]);
    assert.equal(state.workerWorkspaces.length, 1);
    assert.equal(state.workerWorkspaces[0].id, 'WS1');
    assert.equal(state.ompProcesses.length, 1);
    assert.equal(state.ompProcesses[0].ref, '4242');
    assert.equal(state.queriedTop, true);
  } finally {
    await rm(env.root, { recursive: true, force: true });
  }
});

test('supervisor: checkWorkerState skips top and fails closed on duplicate workspaces', async () => {
  const env = await makeEnv();
  try {
    const configPath = await writeConfig(env);
    const config = await loadWorkerConfig({ configPath });

    const noWorkspace = [];
    const callsA = [];
    const fakeA = async (file, args) => {
      callsA.push(args);
      if (args[0] === 'tree') return { stdout: treeText() };
      return { stdout: '' };
    };
    const empty = await checkWorkerState(config, { execFile: fakeA });
    assert.deepEqual(callsA, [CMUX_TREE_ARGS]);
    assert.equal(empty.workerWorkspaces.length, 0);
    assert.equal(empty.queriedTop, false);

    const dupCalls = [];
    const fakeB = async (file, args) => {
      dupCalls.push(args);
      if (args[0] === 'tree') return { stdout: treeText(wsObj('A', 'AugGood Worker'), wsObj('B', 'AugGood Worker')) };
      return { stdout: '' };
    };
    const error = await failCode(checkWorkerState(config, { execFile: fakeB }), 'E_WORKSPACE_DUPLICATE');
    assert.ok(error.message.includes('2 workspaces'));
    assert.deepEqual(dupCalls, [CMUX_TREE_ARGS]);
  } finally {
    await rm(env.root, { recursive: true, force: true });
  }
});

test('supervisor: createWorkerWorkspace and restartOmpInWorkspace use safe cmux actions', async () => {
  const env = await makeEnv();
  try {
    const calls = [];
    const fake = async (file, args) => {
      calls.push(args);
      return { stdout: '' };
    };
    await createWorkerWorkspace(env.config, { command: "om'p --profile aug", execFile: fake });
    assert.deepEqual(calls, [['new-workspace', '--name', 'AugGood Worker', '--cwd', env.repo, '--command', "om'p --profile aug"]]);

    calls.length = 0;
    await restartOmpInWorkspace(env.config, {
      workspaceId: 'WS1',
      surfaceId: 'S-T1',
      command: "om'p --continue",
      execFile: fake,
    });
    assert.deepEqual(calls, [['send', '--workspace', 'WS1', '--surface', 'S-T1', "om'p --continue\n"]]);
  } finally {
    await rm(env.root, { recursive: true, force: true });
  }
});

// ---------------------------------------------------------------------------
// No application lifecycle imports
// ---------------------------------------------------------------------------

test('supervisor: module imports only Node builtins, never application lifecycle code', () => {
  const source = readFileSync(join(REPO_ROOT, 'src', 'scheduler', 'local-omp-supervisor.mjs'), 'utf8');
  const importLines = source.split('\n').filter((line) => /^\s*import\s/.test(line));
  assert.ok(importLines.length >= 1, 'expected import statements');
  for (const line of importLines) {
    assert.match(line, /^import .* from 'node:/, `non-builtin import: ${line.trim()}`);
  }
  for (const forbidden of ['phase1', 'backlog-runner', 'preparation.mjs', 'application-orchestrator', 'run-daily', 'session.mjs']) {
    assert.ok(!source.includes(forbidden), `module must not reference ${forbidden}`);
  }
});

// ---------------------------------------------------------------------------
// Graceful abort and SIGTERM forwarding
// ---------------------------------------------------------------------------

test('supervisor: forwardSigterm forwards to exactly the known worker pids', () => {
  const killed = [];
  const kill = (pid, sig) => killed.push([pid, sig]);
  assert.deepEqual(forwardSigterm(kill, ['4242']), ['4242']);
  assert.deepEqual(killed, [[4242, 'SIGTERM']]);

  killed.length = 0;
  assert.deepEqual(forwardSigterm(kill, []), []);
  assert.deepEqual(killed, []);

  const throwing = () => {
    throw new Error('ESRCH');
  };
  assert.deepEqual(forwardSigterm(throwing, ['4242']), []);
});

test('supervisor: runSupervisor rechecks boundedly and stops on maxChecks', async () => {
  const env = await makeEnv();
  try {
    const configPath = await writeConfig(env);
    const calls = [];
    const result = await runSupervisor({
      configPath,
      execFile: healthyFakeExec(calls),
      checkIntervalMs: 1,
      maxChecks: 2,
      cmuxTimeoutMs: 2_000,
    });
    assert.equal(result.kind, 'max-checks');
    assert.equal(result.checks, 2);
    assert.equal(calls.length, 4);
  } finally {
    await rm(env.root, { recursive: true, force: true });
  }
});

test('supervisor: abort during sleep exits gracefully without further cmux calls', async () => {
  const env = await makeEnv();
  try {
    const configPath = await writeConfig(env);
    const controller = new AbortController();
    const calls = [];
    const resultPromise = runSupervisor({
      configPath,
      execFile: healthyFakeExec(calls),
      checkIntervalMs: 60_000,
      maxChecks: 100,
      signal: controller.signal,
      cmuxTimeoutMs: 2_000,
    });
    await pollUntil(() => calls.length >= 2);
    controller.abort();
    const result = await Promise.race([
      resultPromise,
      new Promise((_, reject) => setTimeout(() => reject(new Error('runSupervisor did not terminate')), 5_000)),
    ]);
    assert.equal(result.kind, 'terminated');
    assert.equal(calls.length, 2);
  } finally {
    await rm(env.root, { recursive: true, force: true });
  }
});

test('supervisor: terminate signal forwards to the OMP worker and exits gracefully', async () => {
  const env = await makeEnv();
  const signalName = 'omp-supervisor-test-terminate';
  try {
    const configPath = await writeConfig(env);
    const calls = [];
    const killed = [];
    const kill = (pid, sig) => killed.push([pid, sig]);
    const resultPromise = runSupervisor({
      configPath,
      execFile: healthyFakeExec(calls),
      checkIntervalMs: 60_000,
      maxChecks: 100,
      kill,
      terminateSignal: signalName,
      cmuxTimeoutMs: 2_000,
    });
    try {
      await pollUntil(() => process.listenerCount(signalName) > 0);
      await pollUntil(() => calls.length >= 2);
      process.emit(signalName);
      const result = await Promise.race([
        resultPromise,
        new Promise((_, reject) => setTimeout(() => reject(new Error('runSupervisor did not terminate on signal')), 5_000)),
      ]);
      assert.equal(result.kind, 'terminated');
      assert.deepEqual(killed, [[4242, 'SIGTERM']]);
    } finally {
      assert.equal(process.listenerCount(signalName), 0);
    }
  } finally {
    await rm(env.root, { recursive: true, force: true });
  }
});

test('supervisor: runSupervisor creates exactly one workspace when none exists', async () => {
  const env = await makeEnv();
  try {
    const configPath = await writeConfig(env);
    const controller = new AbortController();
    const calls = [];
    const fake = async (file, args) => {
      calls.push(args);
      if (args[0] === 'tree') return { stdout: treeText() };
      return { stdout: '' };
    };
    const config = await loadWorkerConfig({ configPath });
    const expected = buildOmpInvocation({ config, sessionCount: 0 }).command;
    const resultPromise = runSupervisor({
      configPath,
      execFile: fake,
      checkIntervalMs: 60_000,
      maxChecks: 100,
      signal: controller.signal,
      cmuxTimeoutMs: 2_000,
    });
    await pollUntil(() => calls.length >= 2);
    controller.abort();
    const result = await Promise.race([
      resultPromise,
      new Promise((_, reject) => setTimeout(() => reject(new Error('runSupervisor did not terminate')), 5_000)),
    ]);
    assert.equal(result.kind, 'terminated');
    assert.deepEqual(calls[0], CMUX_TREE_ARGS);
    assert.deepEqual(calls[1], ['new-workspace', '--name', 'AugGood Worker', '--cwd', env.repo, '--command', expected]);
    assert.equal(calls.length, 2);
  } finally {
    await rm(env.root, { recursive: true, force: true });
  }
});

// ---------------------------------------------------------------------------
// Exact launchd plist invariants
// ---------------------------------------------------------------------------

test('launchd plist: exact invariants and inert until installed', (t) => {
  if (process.platform !== 'darwin') {
    t.skip('plutil requires macOS');
    return;
  }
  const plistPath = join(REPO_ROOT, 'src', 'scheduler', 'launchd', 'com.ian.jobs.auggood-worker.plist');
  const source = readFileSync(plistPath, 'utf8');
  assert.match(source, /^\s*<\?xml version="1\.0"/);
  const json = execFileSync('/usr/bin/plutil', ['-convert', 'json', '-o', '-', plistPath], { encoding: 'utf8' });
  const plist = JSON.parse(json);

  assert.equal(plist.Label, 'com.ian.jobs.auggood-worker');
  assert.deepEqual(plist.ProgramArguments, ['/opt/homebrew/bin/node', join(plist.WorkingDirectory, 'src', 'scheduler', 'local-omp-supervisor.mjs')]);
  assert.ok(resolve(plist.WorkingDirectory).startsWith('/'), 'WorkingDirectory must be absolute');
  assert.equal(plist.LimitLoadToSessionType, 'Aqua');
  assert.equal(plist.RunAtLoad, true);
  assert.deepEqual(plist.KeepAlive, { SuccessfulExit: false });
  assert.ok(Number.isInteger(plist.ThrottleInterval) && plist.ThrottleInterval >= 1);
  for (const key of ['StandardOutPath', 'StandardErrorPath']) {
    assert.ok(resolve(plist[key]).startsWith('/'), `${key} must be absolute`);
    assert.ok(plist[key].startsWith(join(plist.WorkingDirectory, 'private')), `${key} must live under private/`);
  }
  assert.notEqual(plist.StandardOutPath, plist.StandardErrorPath);

  const installed = join(homedir(), 'Library', 'LaunchAgents', 'com.ian.jobs.auggood-worker.plist');
  assert.ok(!readFileSyncSafe(installed), 'plist must be inert: not installed in ~/Library/LaunchAgents');
});

function readFileSyncSafe(path) {
  try {
    return readFileSync(path);
  } catch {
    return null;
  }
}
