import { spawnSync } from 'node:child_process';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

import { evaluate as evaluateLedgerRetention } from './custom-select-cases/ledger-retention.mjs';
import { evaluate as evaluateObserverStable } from './custom-select-cases/observer-stable.mjs';
import { evaluate as evaluatePlanResult } from './custom-select-cases/plan-result.mjs';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const NARROW_TESTS = Object.freeze([
  'tests/action-plan.test.mjs',
  'tests/custom-select-executor.test.mjs',
  'tests/application-cli.test.mjs',
  'tests/ledger-audit.test.mjs',
  'tests/observer-regressions.test.mjs',
  'tests/observer.test.mjs',
  'tests/session.test.mjs',
]);

function validateCaseResult(result) {
  if (result === null || typeof result !== 'object' || Array.isArray(result)) {
    throw new TypeError('benchmark case must return an object');
  }
  const keys = Object.keys(result).sort();
  if (JSON.stringify(keys) !== JSON.stringify(['checks', 'diagnostics', 'failures', 'name'])) {
    throw new TypeError('benchmark case returned unexpected keys');
  }
  if (typeof result.name !== 'string' || result.name.length === 0) {
    throw new TypeError('benchmark case name must be nonempty');
  }
  if (!Number.isSafeInteger(result.checks) || result.checks < 1) {
    throw new TypeError('benchmark case checks must be positive');
  }
  if (!Number.isSafeInteger(result.failures) || result.failures < 0 || result.failures > result.checks) {
    throw new TypeError('benchmark case failures are invalid');
  }
  if (!Array.isArray(result.diagnostics)
      || result.diagnostics.length !== result.failures
      || result.diagnostics.some((value) => typeof value !== 'string' || !/^[A-Z][A-Z0-9_]{0,127}$/u.test(value))) {
    throw new TypeError('benchmark case diagnostics are invalid');
  }
  return result;
}

function runNodeTests(files) {
  const child = spawnSync(process.execPath, ['--test', ...files], {
    cwd: ROOT,
    encoding: 'utf8',
    env: {
      ...process.env,
      LANG: 'C',
      LC_ALL: 'C',
      TZ: 'UTC',
    },
    maxBuffer: 16 * 1024 * 1024,
  });
  if (child.error) throw child.error;
  if (child.signal !== null) throw new Error(`node test runner terminated by ${child.signal}`);
  const output = `${child.stdout ?? ''}\n${child.stderr ?? ''}`;
  const testsMatch = /^ℹ tests (\d+)$/mu.exec(output) ?? /^# tests (\d+)$/mu.exec(output);
  const failuresMatch = /^ℹ fail (\d+)$/mu.exec(output) ?? /^# fail (\d+)$/mu.exec(output);
  if (testsMatch === null || failuresMatch === null) {
    throw new Error('node test runner did not emit a parseable summary');
  }
  return Object.freeze({
    checks: Number.parseInt(testsMatch[1], 10),
    failures: Number.parseInt(failuresMatch[1], 10),
  });
}

const caseResults = [
  evaluatePlanResult(),
  evaluateObserverStable(),
  evaluateLedgerRetention(),
].map(validateCaseResult);
const regression = runNodeTests(['tests/autoresearch-custom-select.test.mjs']);
const narrow = runNodeTests(NARROW_TESTS);
const caseChecks = caseResults.reduce((total, result) => total + result.checks, 0);
const caseFailures = caseResults.reduce((total, result) => total + result.failures, 0);
const totalChecks = caseChecks + regression.checks + narrow.checks;
const totalFailures = caseFailures + regression.failures + narrow.failures;

console.log(`METRIC custom_select_failures=${totalFailures}`);
console.log(`METRIC custom_select_checks=${totalChecks}`);
console.log(`METRIC behavioral_case_failures=${caseFailures}`);
console.log(`METRIC regression_test_failures=${regression.failures}`);
console.log(`METRIC narrow_suite_failures=${narrow.failures}`);
