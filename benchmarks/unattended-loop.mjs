import { spawnSync } from 'node:child_process';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const MATRIX_FILES = Object.freeze([
  'tests/ingestion-migration.test.mjs',
  'tests/job-source-preparation.test.mjs',
  'tests/backlog-runner.test.mjs',
  'tests/platforms.test.mjs',
  'tests/session.test.mjs',
  'tests/application-cli.test.mjs',
  'tests/action-plan.test.mjs',
  'tests/custom-select-executor.test.mjs',
  'tests/omp-browser-transport.test.mjs',
  'tests/omp-browser-executor.test.mjs',
  'tests/observer.test.mjs',
  'tests/ledger-audit.test.mjs',
  'tests/ledger-regressions.test.mjs',
  'tests/observer-audit-regressions.test.mjs',
  'tests/evidence.test.mjs',
  'tests/recovery.test.mjs',
]);

const child = spawnSync(process.execPath, ['--test', '--test-concurrency=1', ...MATRIX_FILES], {
  cwd: ROOT,
  encoding: 'utf8',
  env: { ...process.env, LANG: 'C', LC_ALL: 'C', TZ: 'UTC' },
  maxBuffer: 48 * 1024 * 1024,
});
if (child.error) throw child.error;
if (child.signal !== null) throw new Error(`node test runner terminated by ${child.signal}`);

const output = `${child.stdout ?? ''}\n${child.stderr ?? ''}`;
function summary(name) {
  const match = new RegExp(`^(?:ℹ|#) ${name} (\\d+)$`, 'mu').exec(output);
  if (match === null) throw new Error(`node test runner omitted ${name} summary`);
  return Number.parseInt(match[1], 10);
}

if (child.status !== 0) {
  let detail = `status ${child.status}`;
  try {
    const checks = summary('tests');
    const failures = summary('fail');
    const passed = summary('pass');
    detail += ` (checks=${checks}, passed=${passed}, failures=${failures})`;
  } catch {
    // Summary omitted or unparseable
  }
  throw new Error(`node test runner failed with exit ${detail}`);
}

const checks = summary('tests');
const failures = summary('fail');
const passed = summary('pass');
if (checks !== passed + failures + summary('cancelled') + summary('skipped') + summary('todo')) {
  throw new Error('node test runner emitted an inconsistent summary');
}

if (failures > 0) {
  throw new Error(`node test runner reported ${failures} failure(s) (checks=${checks}, passed=${passed})`);
}

console.log(`METRIC unattended_loop_failures=${failures}`);
console.log(`METRIC unattended_loop_passed=${passed}`);
console.log(`METRIC unattended_loop_checks=${checks}`);
