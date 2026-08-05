import assert from 'node:assert/strict';
import { execFile } from 'node:child_process';
import { mkdtemp, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { promisify } from 'node:util';
import test from 'node:test';

import { validateSourceSyncResult } from '../src/ingestion/contracts.mjs';

const execFileAsync = promisify(execFile);

test('CLI failures emit one schema-valid result and exit nonzero', async () => {
  let failure;
  try {
    await execFileAsync(process.execPath, ['src/ingestion/cli.mjs', 'preview', '--config', '/definitely/missing/config.json'], {
      cwd: process.cwd(),
      env: { PATH: process.env.PATH },
      encoding: 'utf8',
    });
  } catch (error) {
    failure = error;
  }
  assert.ok(failure);
  assert.equal(failure.code, 1);
  assert.equal(failure.stderr, '');
  const lines = failure.stdout.trim().split('\n');
  assert.equal(lines.length, 1);
  const result = JSON.parse(lines[0]);
  validateSourceSyncResult(result);
  assert.equal(result.mode, 'preview');
  assert.equal(result.state, 'failed');
  assert.equal(result.failureClass, 'terminal');
  assert.equal(result.reasonCode, 'config_unreadable');

  let paidFailure;
  try {
    await execFileAsync(process.execPath, ['src/ingestion/cli.mjs', 'sync', '--config', '/definitely/missing/config.json', '--paid'], {
      cwd: process.cwd(),
      env: { PATH: process.env.PATH },
      encoding: 'utf8',
    });
  } catch (error) {
    paidFailure = error;
  }
  assert.equal(paidFailure.code, 1);
  const paidResult = JSON.parse(paidFailure.stdout);
  validateSourceSyncResult(paidResult);
  assert.equal(paidResult.mode, 'paid');
  assert.equal(paidResult.state, 'failed');
});


test('CLI sanitizes invalid config identity fields in its single failure result', async () => {
  const root = await mkdtemp(join(tmpdir(), 'ingestion-cli-'));
  const configPath = join(root, 'config.json');
  await writeFile(configPath, JSON.stringify({ profile: {}, pageSize: 1, maxPages: 1 }), { mode: 0o600 });
  try {
    let failure;
    try {
      await execFileAsync(process.execPath, ['src/ingestion/cli.mjs', 'preview', '--config', configPath], {
        cwd: process.cwd(),
        env: { PATH: process.env.PATH },
        encoding: 'utf8',
      });
    } catch (error) {
      failure = error;
    }
    assert.ok(failure);
    assert.equal(failure.code, 1);
    assert.equal(failure.stderr, '');
    const lines = failure.stdout.trim().split('\n');
    assert.equal(lines.length, 1);
    const result = JSON.parse(lines[0]);
    validateSourceSyncResult(result);
    assert.equal(result.profile, 'default');
    assert.equal(result.reasonCode, 'profile_required');
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test('CLI never echoes an unsupported private profile identifier', async () => {
  const root = await mkdtemp(join(tmpdir(), 'ingestion-cli-profile-'));
  const configPath = join(root, 'config.json');
  await writeFile(configPath, JSON.stringify({
    profile: 'candidate_private_identifier',
    pageSize: 1,
    maxPages: 1,
  }), { mode: 0o600 });
  try {
    let failure;
    try {
      await execFileAsync(process.execPath, ['src/ingestion/cli.mjs', 'preview', '--config', configPath], {
        cwd: process.cwd(),
        env: { PATH: process.env.PATH },
        encoding: 'utf8',
      });
    } catch (error) {
      failure = error;
    }
    assert.ok(failure);
    assert.equal(failure.code, 1);
    assert.equal(failure.stderr, '');
    assert.equal(failure.stdout.includes('candidate_private_identifier'), false);
    const result = JSON.parse(failure.stdout);
    validateSourceSyncResult(result);
    assert.equal(result.profile, 'default');
    assert.equal(result.reasonCode, 'profile_unsupported');
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});