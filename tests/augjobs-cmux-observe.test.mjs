import assert from 'node:assert/strict';
import { execFile, execFileSync } from 'node:child_process';
import crypto from 'node:crypto';
import fs from 'node:fs';
import fsp from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import test from 'node:test';

import {
  CmuxObserverCliError,
  observeCmuxSurface,
  parseCliArgs,
  runCmuxObserverCli,
  secureWriteObservationJson,
  validateCmuxPath,
  validateSurfaceRef,
} from '../src/phase1/cmux-observer-cli.mjs';

function createDummyObservation({ observationId = 'obs-001', previousObservationId = null } = {}) {
  const frames = [
    {
      id: 'frame-0',
      parent_id: null,
      url: 'https://boards.greenhouse.io/test/jobs/12345',
      origin: 'https://boards.greenhouse.io',
      accessible: true,
    },
  ];
  const controls = [
    {
      ref: `${observationId}:control-0`,
      stable_id: 'first_name',
      group_id: null,
      kind: 'input',
      type: 'text',
      tag: 'input',
      role: 'textbox',
      label: 'First Name',
      name: 'first_name',
      description: null,
      value: 'Jane',
      value_present: true,
      checked: null,
      selected: null,
      options: [],
      required: true,
      disabled: false,
      readonly: false,
      visible: true,
      enabled: true,
      frame_id: 'frame-0',
      locator: { strategy: 'id', value: 'first_name', role: 'textbox', name: 'first_name' },
      candidate: { class: 'field', reason: 'visible user-facing field control' },
      validity: { valid: true, aria_invalid: false, message: null },
      file: null,
    },
  ];
  const blockers = [];
  const title = 'Test Job Application';
  const url = 'https://boards.greenhouse.io/test/jobs/12345';
  const snapshot_sha256 = crypto
    .createHash('sha256')
    .update(JSON.stringify({ frames, controls, blockers, title, url }))
    .digest('hex');

  return {
    schema: 'phase1-observation-v1',
    observation_id: observationId,
    previous_observation_id: previousObservationId,
    observed_at: new Date().toISOString(),
    url,
    title,
    snapshot_sha256,
    frames,
    controls,
    blockers,
  };
}

async function withTempDirectory(fn) {
  const dir = await fsp.mkdtemp(path.join(os.tmpdir(), 'cmux-obs-test-'));
  const privateDir = path.join(dir, 'private');
  await fsp.mkdir(privateDir, { mode: 0o700 });
  try {
    return await fn({ root: dir, privateDir });
  } finally {
    await fsp.rm(dir, { recursive: true, force: true });
  }
}

async function createFakeCmuxScript(dir, handlerSource) {
  const scriptPath = path.join(dir, 'fake-cmux.mjs');
  const logPath = path.join(dir, 'fake-cmux-argv.json');
  const code = `#!/usr/bin/env node
import fsp from 'node:fs/promises';
const logPath = ${JSON.stringify(logPath)};
await fsp.writeFile(logPath, JSON.stringify(process.argv));
${handlerSource}
`;
  await fsp.writeFile(scriptPath, code, { mode: 0o755 });
  return { scriptPath, logPath };
}

test('parseCliArgs: validates strict flags and returns normalized options', () => {
  const valid = parseCliArgs([
    'node',
    'bin/augjobs-cmux-observe',
    '--cmux',
    '/usr/local/bin/cmux',
    '--surface',
    'surface-1234',
    '--initial',
    '--output',
    'private/obs.json',
  ]);
  assert.equal(valid.cmuxPath, '/usr/local/bin/cmux');
  assert.equal(valid.surface, 'surface-1234');
  assert.equal(valid.previousObservationId, null);
  assert.equal(valid.outputPath, 'private/obs.json');

  const chained = parseCliArgs([
    'node',
    'bin/augjobs-cmux-observe',
    '--cmux',
    '/usr/local/bin/cmux',
    '--surface',
    'surface-1234',
    '--previous-observation-id',
    'obs-001',
    '--output',
    'private/obs.json',
  ]);
  assert.equal(chained.previousObservationId, 'obs-001');

  assert.throws(
    () => parseCliArgs(['node', 'bin/augjobs-cmux-observe', '--unknown']),
    (err) => err instanceof CmuxObserverCliError && err.code === 'E_CLI_UNKNOWN_FLAG',
  );

  assert.throws(
    () => parseCliArgs(['node', 'bin/augjobs-cmux-observe', '--cmux', '/path']),
    (err) => err instanceof CmuxObserverCliError && err.code === 'E_CLI_MISSING_ARG',
  );
});

test('validateSurfaceRef: rejects non-browser or invalid format surface refs', () => {
  assert.equal(validateSurfaceRef('surface-uuid-1234'), 'surface-uuid-1234');
  assert.equal(validateSurfaceRef('12345678-1234-4234-8234-123456789abc'), '12345678-1234-4234-8234-123456789abc');

  assert.throws(
    () => validateSurfaceRef(''),
    (err) => err instanceof CmuxObserverCliError && err.code === 'E_INVALID_SURFACE_REF',
  );
  assert.throws(
    () => validateSurfaceRef('   '),
    (err) => err instanceof CmuxObserverCliError && err.code === 'E_INVALID_SURFACE_REF',
  );
  assert.throws(
    () => validateSurfaceRef('surface with spaces'),
    (err) => err instanceof CmuxObserverCliError && err.code === 'E_INVALID_SURFACE_REF',
  );
  assert.throws(
    () => validateSurfaceRef('surface<script>'),
    (err) => err instanceof CmuxObserverCliError && err.code === 'E_INVALID_SURFACE_REF',
  );
});

test('observeCmuxSurface: passes exact argv array, sets global, evaluates observer source without form mutations', async () => {
  await withTempDirectory(async ({ root, privateDir }) => {
    const dummyObs = createDummyObservation({ observationId: 'obs-001', previousObservationId: null });
    const { scriptPath, logPath } = await createFakeCmuxScript(
      root,
      `console.log(JSON.stringify(${JSON.stringify(dummyObs)}));`,
    );

    const outputPath = path.join(privateDir, 'output.json');
    const result = await observeCmuxSurface({
      cmuxPath: scriptPath,
      surface: 'surface-test-123',
      previousObservationId: null,
      outputPath,
    });

    assert.equal(result.status, 'ok');
    assert.equal(result.observation_id, 'obs-001');
    assert.equal(result.previous_observation_id, null);
    assert.equal(result.control_count, 1);
    assert.equal(result.field_count, 1);
    assert.equal(result.blocker_count, 0);

    const rawLog = await fsp.readFile(logPath, 'utf8');
    const argv = JSON.parse(rawLog);
    assert.equal(argv[2], 'browser');
    assert.equal(argv[3], '--surface');
    assert.equal(argv[4], 'surface-test-123');
    assert.equal(argv[5], 'eval');
    assert.equal(argv[6], '--script');

    const evalScript = argv[7];
    assert.ok(evalScript.includes('globalThis.__omp_phase1_previous_observation_id_v1 = null;'));
    const observerSource = await fsp.readFile(
      new URL('../src/phase1/observer.js', import.meta.url),
      'utf8',
    );
    assert.equal(
      evalScript,
      `globalThis.__omp_phase1_previous_observation_id_v1 = null;\n${observerSource}`,
    );
  });
});

test('observeCmuxSurface: enforces observation chain requirement', async () => {
  await withTempDirectory(async ({ root, privateDir }) => {
    const mismatchedObs = createDummyObservation({ observationId: 'obs-002', previousObservationId: 'obs-wrong' });
    const { scriptPath } = await createFakeCmuxScript(
      root,
      `console.log(JSON.stringify(${JSON.stringify(mismatchedObs)}));`,
    );

    const outputPath = path.join(privateDir, 'mismatch-output.json');
    await assert.rejects(
      () => observeCmuxSurface({
        cmuxPath: scriptPath,
        surface: 'surface-test-123',
        previousObservationId: null,
        outputPath,
      }),
      (err) => err instanceof CmuxObserverCliError && err.code === 'E_OBSERVATION_CHAIN_MISMATCH',
    );

    const chainedObs = createDummyObservation({ observationId: 'obs-003', previousObservationId: 'obs-002' });
    const { scriptPath: scriptPath2 } = await createFakeCmuxScript(
      root,
      `console.log(JSON.stringify(${JSON.stringify(chainedObs)}));`,
    );

    const outputPath2 = path.join(privateDir, 'chained-output.json');
    const okResult = await observeCmuxSurface({
      cmuxPath: scriptPath2,
      surface: 'surface-test-123',
      previousObservationId: 'obs-002',
      outputPath: outputPath2,
    });
    assert.equal(okResult.observation_id, 'obs-003');
    assert.equal(okResult.previous_observation_id, 'obs-002');
  });
});

test('secureWriteObservationJson: enforces 0600 mode, owner-private directory, and atomic no-overwrite', async () => {
  await withTempDirectory(async ({ root, privateDir }) => {
    const dummyObs = createDummyObservation({ observationId: 'obs-sec-1' });
    const targetFile = path.join(privateDir, 'obs-sec.json');

    await secureWriteObservationJson(targetFile, dummyObs);
    const stat = await fsp.stat(targetFile);
    assert.equal(stat.mode & 0o777, 0o600);

    await assert.rejects(
      () => secureWriteObservationJson(targetFile, dummyObs),
      (err) => err instanceof CmuxObserverCliError && err.code === 'E_OUTPUT_EXISTS',
    );

    const unsafeDir = path.join(root, 'unsafe-dir');
    await fsp.mkdir(unsafeDir, { mode: 0o755 });
    const unsafeTarget = path.join(unsafeDir, 'obs.json');
    await assert.rejects(
      () => secureWriteObservationJson(unsafeTarget, dummyObs),
      (err) => err instanceof CmuxObserverCliError && err.code === 'E_UNSAFE_OUTPUT_PATH',
    );
  });
});

test('observeCmuxSurface: handles malformed results and execution failures', async () => {
  await withTempDirectory(async ({ root, privateDir }) => {
    const { scriptPath: nonJsonScript } = await createFakeCmuxScript(root, 'console.log("NOT_JSON");');
    await assert.rejects(
      () => observeCmuxSurface({
        cmuxPath: nonJsonScript,
        surface: 'surface-123',
        previousObservationId: null,
        outputPath: path.join(privateDir, 'err1.json'),
      }),
      (err) => err instanceof CmuxObserverCliError && err.code === 'E_MALFORMED_OBSERVATION_JSON',
    );

    const { scriptPath: invalidObsScript } = await createFakeCmuxScript(
      root,
      'console.log(JSON.stringify({ schema: "phase1-observation-v1", observation_id: "x" }));',
    );
    await assert.rejects(
      () => observeCmuxSurface({
        cmuxPath: invalidObsScript,
        surface: 'surface-123',
        previousObservationId: null,
        outputPath: path.join(privateDir, 'err2.json'),
      }),
      (err) => err instanceof CmuxObserverCliError && err.code === 'E_INVALID_OBSERVATION',
    );

    const { scriptPath: exitErrorScript } = await createFakeCmuxScript(root, 'process.exit(1);');
    await assert.rejects(
      () => observeCmuxSurface({
        cmuxPath: exitErrorScript,
        surface: 'surface-123',
        previousObservationId: null,
        outputPath: path.join(privateDir, 'err3.json'),
      }),
      (err) => err instanceof CmuxObserverCliError && err.code === 'E_CMUX_EXEC_FAILED',
    );
  });
});

test('runCmuxObserverCli: returns narrow stdout metadata JSON only', async () => {
  await withTempDirectory(async ({ root, privateDir }) => {
    const dummyObs = createDummyObservation({ observationId: 'obs-narrow-1', previousObservationId: null });
    const { scriptPath } = await createFakeCmuxScript(
      root,
      `console.log(JSON.stringify(${JSON.stringify(dummyObs)}));`,
    );

    const outputPath = path.join(privateDir, 'narrow.json');
    const argv = [
      'node',
      'bin/augjobs-cmux-observe',
      '--cmux',
      scriptPath,
      '--surface',
      'surface-narrow-1',
      '--initial',
      '--output',
      outputPath,
    ];

    const metadata = await runCmuxObserverCli(argv);
    assert.deepEqual(Object.keys(metadata).sort(), [
      'blocker_count',
      'control_count',
      'field_count',
      'observation_id',
      'previous_observation_id',
      'status',
    ]);
    assert.equal(metadata.status, 'ok');
    assert.equal(metadata.observation_id, 'obs-narrow-1');
    assert.equal(metadata.previous_observation_id, null);

    assert.equal(metadata.url, undefined);
    assert.equal(metadata.title, undefined);
    assert.equal(metadata.controls, undefined);
    assert.equal(metadata.frames, undefined);
  });
});
