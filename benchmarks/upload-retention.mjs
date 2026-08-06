import { spawnSync } from 'node:child_process';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const MATRIX_FILES = Object.freeze([
  'tests/omp-browser-transport.test.mjs',
  'tests/omp-browser-executor.test.mjs',
  'tests/action-plan.test.mjs',
  'tests/observer.test.mjs',
  'tests/ledger-audit.test.mjs',
  'tests/ledger-regressions.test.mjs',
  'tests/observer-audit-regressions.test.mjs',
  'tests/session.test.mjs',
]);

const child = spawnSync(process.execPath, ['--test', ...MATRIX_FILES], {
  cwd: ROOT,
  encoding: 'utf8',
  env: { ...process.env, LANG: 'C', LC_ALL: 'C', TZ: 'UTC' },
  maxBuffer: 32 * 1024 * 1024,
});
if (child.error) throw child.error;
if (child.signal !== null) throw new Error(`node test runner terminated by ${child.signal}`);

const output = `${child.stdout ?? ''}\n${child.stderr ?? ''}`;
function summary(name) {
  const match = new RegExp(`^(?:ℹ|#) ${name} (\\d+)$`, 'mu').exec(output);
  if (match === null) throw new Error(`node test runner omitted ${name} summary`);
  return Number.parseInt(match[1], 10);
}

const checks = summary('tests');
const failures = summary('fail');
const passed = summary('pass');
if (checks !== passed + failures + summary('cancelled') + summary('skipped') + summary('todo')) {
  throw new Error('node test runner emitted an inconsistent summary');
}

console.log(`METRIC upload_matrix_failures=${failures}`);
console.log(`METRIC upload_matrix_passed=${passed}`);
console.log(`METRIC upload_matrix_checks=${checks}`);
