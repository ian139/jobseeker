import test from 'node:test';
import assert from 'node:assert/strict';
import { execSync } from 'node:child_process';
import path from 'node:path';

test('bin/augjobs-list executes cleanly against backlog database', () => {
  const scriptPath = path.resolve('bin/augjobs-list');
  const dbPath = path.resolve('data/RealGreenhouse.sqlite');
  const output = execSync(`node ${scriptPath} ${dbPath}`, { encoding: 'utf8' });
  assert.ok(output.includes('All Backlog Applications'));
  assert.ok(output.includes('SpaceX'));
});
