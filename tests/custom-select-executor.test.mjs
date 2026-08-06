import assert from 'node:assert/strict';
import { runInNewContext } from 'node:vm';
import test from 'node:test';

import {
  CustomSelectExecutorError,
  executeCustomSelectOption,
  resolveCustomSelectOption,
} from '../src/phase1/custom-select.mjs';

function option(key, text, value, disabled = false) {
  return { key, text, value, disabled };
}

function target(optionText, optionValue) {
  return { optionText, optionValue };
}

function errorCode(callback) {
  try {
    callback();
  } catch (error) {
    assert.ok(error instanceof CustomSelectExecutorError);
    return error.code;
  }
  return null;
}

test('normalizes text and gives exact text priority over exact value', () => {
  const result = resolveCustomSelectOption([
    option('text', '  Data   Science ', 'other-value'),
    option('value', 'Other label', ' DS-42 '),
  ], target('data science', 'ds-42'));

  assert.deepEqual(result, {
    key: 'text',
    text: '  Data   Science ',
    value: 'other-value',
    strategy: 'exact_text',
  });
  assert.equal(Object.isFrozen(result), true);
});

test('falls back to normalized option value after text misses', () => {
  const result = resolveCustomSelectOption([
    option('value', 'Other label', ' DS-42 '),
  ], target('unrelated label', 'ds-42'));

  assert.equal(result.key, 'value');
  assert.equal(result.strategy, 'exact_value');
});

test('accepts options without exposed values when text identifies them', () => {
  const result = resolveCustomSelectOption([
    option('text-only', 'Remote', ''),
  ], target('remote', 'provider-value'));

  assert.equal(result.key, 'text-only');
  assert.equal(result.strategy, 'exact_text');
});

test('selects one stable word-boundary substring and avoids inside-word matches', () => {
  const result = resolveCustomSelectOption([
    option('engineer', 'Senior Software Engineer', 'eng-1'),
    option('engineering', 'Engineering', 'eng-2'),
  ], target('software engineer', 'unrelated'));
  assert.equal(result.key, 'engineer');
  assert.equal(result.strategy, 'substring');

  const reverse = resolveCustomSelectOption([
    option('reverse', 'Software Engineer', ''),
  ], target('Senior Software Engineer', 'unrelated'));
  assert.equal(reverse.key, 'reverse');
  assert.equal(reverse.strategy, 'substring');

  assert.equal(
    errorCode(() => resolveCustomSelectOption(
      [option('usa', 'USA', 'usa')],
      target('US', 'unrelated'),
    )),
    'E_CUSTOM_SELECT_OPTION_NOT_FOUND',
  );
});

test('preserves symbols that distinguish technical option labels', () => {
  for (const [requested, visible] of [
    ['C++', 'C'],
    ['C#', 'C'],
    ['A+', 'A'],
  ]) {
    assert.equal(
      errorCode(() => resolveCustomSelectOption(
        [option('different', visible, 'different')],
        target(requested, 'requested'),
      )),
      'E_CUSTOM_SELECT_OPTION_NOT_FOUND',
    );
  }

  const result = resolveCustomSelectOption([
    option('cpp', 'C++', ''),
    option('c', 'C', ''),
  ], target('c++', 'requested'));
  assert.equal(result.key, 'cpp');
  assert.equal(result.strategy, 'exact_text');
});

test('rejects ambiguous, disabled, and missing matches without exposing option data', () => {
  const ambiguous = (() => {
    try {
      resolveCustomSelectOption([
        option('one', 'Platform Engineer', 'one'),
        option('two', 'Platform Engineer', 'two'),
      ], target('platform engineer', 'missing'));
      return null;
    } catch (error) {
      return error;
    }
  })();
  assert.equal(ambiguous.code, 'E_CUSTOM_SELECT_OPTION_AMBIGUOUS');
  assert.equal(ambiguous.details.strategy, 'exact_text');
  assert.doesNotMatch(ambiguous.message, /Platform|one|two/u);

  assert.equal(
    errorCode(() => resolveCustomSelectOption(
      [option('disabled', 'Remote', 'remote', true)],
      target('remote', 'remote'),
    )),
    'E_CUSTOM_SELECT_OPTION_DISABLED',
  );
  assert.equal(
    errorCode(() => resolveCustomSelectOption(
      [option('other', 'On-site', 'onsite')],
      target('Remote', 'remote'),
    )),
    'E_CUSTOM_SELECT_OPTION_NOT_FOUND',
  );
});

test('uses already-open options directly without preparing', async () => {
  let reads = 0;
  let opens = 0;
  let prepares = 0;
  const clicked = [];
  const result = await executeCustomSelectOption({
    openMenu: () => {
      opens += 1;
    },
    readOptions: () => {
      reads += 1;
      return [option('open', 'Remote', 'remote')];
    },
    prepareOptions: () => {
      prepares += 1;
    },
    clickOption: (candidate) => {
      clicked.push(candidate);
    },
    target: target('remote', 'remote'),
    timeoutMs: 100,
    now: () => 0,
    sleep: async () => {},
  });

  assert.equal(opens, 1);
  assert.equal(reads, 1);
  assert.equal(prepares, 0);
  assert.deepEqual(clicked, [result]);
  assert.equal(Object.isFrozen(result), true);
  assert.equal(Object.isFrozen(clicked[0]), true);
});

test('allows one already-open match attempt with a zero timeout', async () => {
  let clicks = 0;
  const result = await executeCustomSelectOption({
    openMenu: () => {},
    readOptions: () => [option('open', 'Remote', '')],
    prepareOptions: () => {
      throw new Error('prepare must not run');
    },
    clickOption: () => {
      clicks += 1;
    },
    target: target('remote', 'provider-value'),
    timeoutMs: 0,
    now: () => 0,
  });

  assert.equal(result.key, 'open');
  assert.equal(clicks, 1);
});

test('accepts plain executor records from a separate VM realm', async () => {
  let clicks = 0;
  const input = runInNewContext(`({
    openMenu,
    readOptions: () => [{ key: 'remote', text: 'Remote', value: '', disabled: false }],
    prepareOptions,
    clickOption,
    target: { optionText: 'remote', optionValue: 'provider-value' },
    timeoutMs: 100,
    now
  })`, {
    clickOption: () => {
      clicks += 1;
    },
    now: () => 0,
    openMenu: () => {},
    prepareOptions: () => {},
  });

  const result = await executeCustomSelectOption(input);
  assert.equal(result.key, 'remote');
  assert.equal(clicks, 1);
});

test('prepares at most once before polling for options', async () => {
  let reads = 0;
  let prepares = 0;
  let elapsed = 0;
  const events = [];
  const clicked = [];
  const result = await executeCustomSelectOption({
    openMenu: () => {
      events.push('open');
    },
    readOptions: () => {
      reads += 1;
      events.push(`read${reads}`);
      return prepares === 0 ? [] : [option('prepared', 'Remote', 'remote')];
    },
    prepareOptions: () => {
      prepares += 1;
      events.push('prepare');
    },
    clickOption: (candidate) => {
      clicked.push(candidate);
      events.push('click');
    },
    target: target('remote', 'remote'),
    timeoutMs: 100,
    pollIntervalMs: 10,
    now: () => elapsed,
    sleep: (milliseconds) => {
      elapsed += milliseconds;
    },
  });

  assert.equal(prepares, 1);
  assert.ok(reads > 1);
  assert.deepEqual(clicked, [result]);
  assert.equal(events[0], 'open');
  assert.ok(events.indexOf('prepare') > events.indexOf('read1'));
  assert.equal(events.at(-1), 'click');
});

test('bounds every awaited browser callback by the executor deadline', async () => {
  const never = () => new Promise(() => {});
  const cases = [
    {
      overrides: { openMenu: never },
      code: 'E_CUSTOM_SELECT_CALLBACK',
      kind: 'openMenu',
    },
    {
      overrides: { readOptions: never },
      code: 'E_CUSTOM_SELECT_OPTION_TIMEOUT',
      kind: null,
    },
    {
      overrides: {
        readOptions: () => [],
        prepareOptions: never,
      },
      code: 'E_CUSTOM_SELECT_CALLBACK',
      kind: 'prepareOptions',
    },
    {
      overrides: {
        readOptions: () => [option('match', 'Remote', 'remote')],
        clickOption: never,
      },
      code: 'E_CUSTOM_SELECT_CALLBACK',
      kind: 'clickOption',
    },
    {
      overrides: {
        readOptions: () => [],
        sleep: never,
      },
      code: 'E_CUSTOM_SELECT_OPTION_TIMEOUT',
      kind: null,
    },
  ];

  for (const { overrides, code, kind } of cases) {
    await assert.rejects(
      executeCustomSelectOption({
        openMenu: () => {},
        readOptions: () => [],
        prepareOptions: () => {},
        clickOption: () => {},
        target: target('Remote', 'remote'),
        timeoutMs: 10,
        pollIntervalMs: 1,
        ...overrides,
      }),
      (error) => {
        assert.equal(error.code, code);
        assert.equal(error.details.kind ?? null, kind);
        return true;
      },
    );
  }
});

test('never clicks ambiguous or disabled state', async () => {
  for (const options of [
    [option('one', 'Remote', 'one'), option('two', 'Remote', 'two')],
    [option('disabled', 'Remote', 'remote', true)],
  ]) {
    let clicks = 0;
    await assert.rejects(
      executeCustomSelectOption({
        openMenu: () => {},
        readOptions: () => options,
        prepareOptions: () => {
          throw new Error('prepare must not run');
        },
        clickOption: () => {
          clicks += 1;
        },
        target: target('remote', 'remote'),
        timeoutMs: 100,
        now: () => 0,
        sleep: async () => {},
      }),
      (error) => error.code === (
        options[0].disabled
          ? 'E_CUSTOM_SELECT_OPTION_DISABLED'
          : 'E_CUSTOM_SELECT_OPTION_AMBIGUOUS'
      ),
    );
    assert.equal(clicks, 0);
  }
});

test('times out deterministically without wall-clock delay or clicking missing options', async () => {
  let elapsed = 0;
  let reads = 0;
  let prepares = 0;
  let clicks = 0;
  await assert.rejects(
    executeCustomSelectOption({
      openMenu: () => {},
      readOptions: () => {
        reads += 1;
        return [];
      },
      prepareOptions: () => {
        prepares += 1;
      },
      clickOption: () => {
        clicks += 1;
      },
      target: target('Remote', 'remote'),
      timeoutMs: 25,
      pollIntervalMs: 10,
      now: () => elapsed,
      sleep: (milliseconds) => {
        elapsed += milliseconds;
      },
    }),
    (error) => {
      assert.equal(error.code, 'E_CUSTOM_SELECT_OPTION_TIMEOUT');
      assert.equal(error.details.prepared, true);
      assert.ok(error.details.attempts > 0);
      return true;
    },
  );
  assert.equal(prepares, 1);
  assert.equal(clicks, 0);
  assert.ok(reads <= 5);
  assert.equal(elapsed, 25);
});

test('sanitizes callback errors without exposing callback-controlled messages', async () => {
  const secret = 'owner-private-secret';
  const callbackError = () => {
    throw new CustomSelectExecutorError(secret);
  };
  const cases = [
    {
      openMenu: callbackError,
      readOptions: () => [],
      prepareOptions: () => {},
      clickOption: () => {},
    },
    {
      readOptions: callbackError,
      clickOption: () => {},
    },
    {
      readOptions: () => [],
      prepareOptions: callbackError,
      clickOption: () => {},
    },
    {
      readOptions: () => [option('match', 'Remote', 'remote')],
      clickOption: callbackError,
    },
    {
      readOptions: () => [],
      clickOption: () => {},
      sleep: callbackError,
    },
  ];

  for (const callbacks of cases) {
    await assert.rejects(
      executeCustomSelectOption({
        openMenu: () => {},
        prepareOptions: () => {},
        ...callbacks,
        target: target('Remote', 'remote'),
        timeoutMs: 100,
        now: () => 0,
      }),
      (error) => {
        assert.equal(error.code, 'E_CUSTOM_SELECT_CALLBACK');
        assert.doesNotMatch(error.message, new RegExp(secret, 'u'));
        return true;
      },
    );
  }

  const sanitized = new CustomSelectExecutorError(secret);
  assert.equal(sanitized.code, 'E_CUSTOM_SELECT_INVALID_INPUT');
  assert.doesNotMatch(sanitized.message, new RegExp(secret, 'u'));
});
