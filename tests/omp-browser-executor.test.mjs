import assert from 'node:assert/strict';
import test from 'node:test';

import {
  ACTION_RESULT_SCHEMA,
  createBrowserActionPlan,
  validateBrowserActionPlan,
} from '../src/phase1/action-plan.mjs';
import {
  OMP_BROWSER_EXECUTOR_ERROR_CODES,
  OmpBrowserExecutorError,
  executeOmpBrowserActionPlan,
} from '../src/phase1/browser-plan-executor.mjs';
import {
  createLedger,
  digestObservedValue,
  recordResolution,
} from '../src/phase1/ledger.mjs';

const DIGEST = 'a'.repeat(64);
const FILE_DIGEST = 'b'.repeat(64);
const SECRET = 'owner-private-secret';

function frame() {
  return {
    id: 'frame-main',
    parent_id: null,
    url: 'https://example.test/app',
    origin: 'https://example.test',
    accessible: true,
  };
}

function control(fieldId, overrides = {}) {
  return {
    ref: `ref-${fieldId}`,
    stable_id: fieldId,
    group_id: null,
    kind: 'text',
    tag: 'input',
    type: 'text',
    role: 'textbox',
    label: `Question ${fieldId}`,
    name: fieldId,
    description: null,
    locator: { strategy: 'id', value: fieldId, role: null, name: null },
    frame_id: 'frame-main',
    visible: true,
    enabled: true,
    required: true,
    readonly: false,
    disabled: false,
    value: `current-${fieldId}`,
    value_present: true,
    checked: null,
    selected: null,
    options: [],
    validity: { valid: true, aria_invalid: null, message: null },
    file: null,
    candidate: { class: 'field', reason: 'visible user-facing field control' },
    ...overrides,
  };
}

function observation(id, controls, previous = null) {
  return {
    schema: 'phase1-observation-v1',
    observation_id: id,
    previous_observation_id: previous,
    observed_at: `2026-08-04T00:00:${id === 'obs-1' ? '01' : '02'}.000Z`,
    url: 'https://example.test/app',
    title: 'Synthetic application',
    snapshot_sha256: DIGEST,
    frames: [frame()],
    controls,
    blockers: [],
  };
}

function postObservation(previous = 'obs-1', controls = []) {
  return observation('obs-2', controls, previous);
}

/** Post-observation with the native select committed to the planned option. */
function committedNativePost() {
  return postObservation('obs-1', [control('country', {
    kind: 'select',
    tag: 'select',
    type: 'select',
    role: 'combobox',
    value: 'us',
    options: [
      { value: 'us', label: 'United States', disabled: false, selected: true },
      { value: 'ca', label: 'Canada', disabled: false, selected: false },
    ],
    selected: ['us'],
  })]);
}

/** Post-observation with the custom select committed to the planned option. */
function committedCustomPost() {
  return postObservation('obs-1', [control('role', {
    kind: 'input',
    type: 'text',
    role: 'combobox',
    value: 'eng',
    value_present: true,
    options: [{ value: 'eng', label: 'Engineering', disabled: false, selected: true }],
    selected: ['eng'],
  })]);
}

/** Post-observation with the upload committed to the planned file. */
function committedUploadPost() {
  return postObservation('obs-1', [control('resume', {
    kind: 'file',
    type: 'file',
    role: 'input',
    value: null,
    value_present: false,
    file: { accept: null, count: 1, names: ['resume.pdf'] },
  })]);
}

function decision(fieldId, action, proposedAnswer, overrides = {}) {
  return {
    observationId: 'obs-1',
    fieldId,
    controlReference: `ref-${fieldId}`,
    fieldPolicy: 'qualification',
    proposedAnswer,
    answerSource: 'user',
    evidenceReferences: [],
    inferenceRationaleDigest: null,
    inferenceEvidenceDigests: null,
    proposedAction: action,
    expectedRetainedState: proposedAnswer,
    modelTier: 'standard',
    confidence: 0.9,
    reasonCode: 'test_answer',
    reobservationRequired: true,
    automaticSubmissionEligible: false,
    ...overrides,
  };
}

function resolvedLedger(current, resolutions) {
  let ledger = createLedger(current);
  for (const resolution of resolutions) {
    const control = current.controls.find((item) => item.stable_id === resolution.field_id);
    ledger = recordResolution(ledger, {
      field_id: resolution.field_id,
      observation_id: current.observation_id,
      ref: control.ref,
      source: 'user',
      value_digest: resolution.semantic_choice ? null : digestObservedValue(control, resolution.value),
      ...(resolution.semantic_choice ? { semantic_choice: resolution.semantic_choice } : {}),
    });
  }
  return ledger;
}

function fillPlan(value = 'Ada Lovelace') {
  const current = observation('obs-1', [control('name')]);
  const ledger = resolvedLedger(current, [{ field_id: 'name', value }]);
  return createBrowserActionPlan({
    observation: current,
    ledger,
    decisions: [decision('name', 'fill_text', value)],
    answerAliases: { name: { alias: 'full_name', value } },
    optionMatches: {},
    driver: 'omp_browser',
  });
}

function clearPlan() {
  const current = observation('obs-1', [control('optional', {
    required: false,
    value: '',
    value_present: false,
  })]);
  const ledger = resolvedLedger(current, [{ field_id: 'optional', value: null, semantic_choice: 'blank' }]);
  return createBrowserActionPlan({
    observation: current,
    ledger,
    decisions: [decision('optional', 'clear', null)],
    answerAliases: { optional: { alias: 'blank', value: null } },
    optionMatches: {},
    driver: 'omp_browser',
  });
}

function nativeSelectPlan() {
  const current = observation('obs-1', [control('country', {
    kind: 'select',
    tag: 'select',
    type: 'select',
    role: 'combobox',
    value: 'us',
    options: [
      { value: 'us', label: 'United States', disabled: false, selected: true },
      { value: 'ca', label: 'Canada', disabled: false, selected: false },
    ],
  })]);
  const ledger = resolvedLedger(current, [{ field_id: 'country', value: 'us' }]);
  return createBrowserActionPlan({
    observation: current,
    ledger,
    decisions: [decision('country', 'select_option', 'us')],
    answerAliases: { country: { alias: 'country', value: 'us' } },
    optionMatches: { country: { option_text: 'United States', option_value: 'us' } },
    driver: 'omp_browser',
  });
}

function customSelectPlan() {
  const current = observation('obs-1', [control('role', {
    kind: 'input',
    type: 'text',
    role: 'combobox',
    value: null,
    value_present: false,
    options: [{ value: 'eng', label: 'Engineering', disabled: false, selected: false }],
  })]);
  const ledger = resolvedLedger(current, [{ field_id: 'role', value: 'eng' }]);
  return createBrowserActionPlan({
    observation: current,
    ledger,
    decisions: [decision('role', 'select_option', 'eng')],
    answerAliases: { role: { alias: 'role', value: 'eng' } },
    optionMatches: { role: { option_text: 'Engineering', option_value: 'eng' } },
    driver: 'omp_browser',
  });
}

function togglePlan() {
  const current = observation('obs-1', [control('consent', {
    kind: 'checkbox',
    type: 'checkbox',
    role: 'checkbox',
    value: null,
    value_present: false,
    checked: true,
  })]);
  const ledger = resolvedLedger(current, [{ field_id: 'consent', value: true }]);
  return createBrowserActionPlan({
    observation: current,
    ledger,
    decisions: [decision('consent', 'toggle', true)],
    answerAliases: { consent: { alias: 'consent', value: true } },
    optionMatches: {},
    driver: 'omp_browser',
  });
}

function uploadPlan() {
  const current = observation('obs-1', [control('resume', {
    kind: 'file',
    type: 'file',
    role: 'input',
    value: null,
    value_present: false,
    file: { accept: null, count: 0, names: [] },
  })]);
  const ledger = resolvedLedger(current, [{ field_id: 'resume', value: '/tmp/resume.pdf' }]);
  return createBrowserActionPlan({
    observation: current,
    ledger,
    decisions: [decision('resume', 'upload_file', '/tmp/resume.pdf')],
    answerAliases: {},
    optionMatches: {},
    resumeUpload: { path: '/tmp/resume.pdf', sha256: FILE_DIGEST },
    driver: 'omp_browser',
  });
}

function batchPlan(count) {
  const fields = ['first', 'second', 'third'].slice(0, count);
  const current = observation('obs-1', fields.map((fieldId) => control(fieldId)));
  const ledger = resolvedLedger(
    current,
    fields.map((fieldId, index) => ({ field_id: fieldId, value: `value-${index}` })),
  );
  return createBrowserActionPlan({
    observation: current,
    ledger,
    decisions: fields.map((fieldId, index) => decision(fieldId, 'fill_text', `value-${index}`)),
    answerAliases: Object.fromEntries(
      fields.map((fieldId, index) => [fieldId, { alias: fieldId, value: `value-${index}` }]),
    ),
    optionMatches: {},
    driver: 'omp_browser',
  });
}

function baseTransport(overrides = {}, post = postObservation()) {
  return {
    fill: async () => {},
    click: async () => {},
    press: async () => {},
    select: async () => {},
    uploadFile: async () => {},
    readOptions: async () => [],
    clickOption: async () => {},
    observe: async () => post,
    ...overrides,
  };
}

test('executes a single fill action and returns a chained frozen result', async () => {
  const plan = fillPlan();
  const calls = [];
  const transport = baseTransport({
    fill: async (selector, value) => {
      calls.push(['fill', selector, value]);
    },
    observe: async () => {
      calls.push(['observe']);
      return postObservation();
    },
  });

  const executed = await executeOmpBrowserActionPlan(plan, transport);
  const { result, postObservation: post } = executed;

  assert.deepEqual(calls, [['fill', '[id="name"]', 'Ada Lovelace'], ['observe']]);
  assert.equal(result.schema, ACTION_RESULT_SCHEMA);
  assert.equal(result.plan_id, plan.plan_id);
  assert.equal(result.post_observation_id, post.observation_id);
  assert.deepEqual(result.outcomes, [{
    action_id: plan.actions[0].action_id,
    outcome: 'succeeded',
    error_code: null,
    driver: 'omp_browser',
    selected_option_text: null,
  }]);
  assert.equal(post.observation_id, 'obs-2');
  assert.equal(post.previous_observation_id, plan.observation_id);
  assert.equal(Object.isFrozen(executed), true);
  assert.equal(Object.isFrozen(result), true);
  assert.equal(Object.isFrozen(result.outcomes), true);
  assert.equal(Object.isFrozen(result.outcomes[0]), true);
  assert.equal(Object.isFrozen(post), true);
  assert.equal(Object.isFrozen(post.controls), true);
});

test('clear fills the exact planned empty value', async () => {
  const calls = [];
  const transport = baseTransport({
    fill: async (selector, value) => {
      calls.push([selector, value]);
    },
  });
  const { result } = await executeOmpBrowserActionPlan(clearPlan(), transport);
  assert.deepEqual(calls, [['[id="optional"]', '']]);
  assert.equal(result.outcomes[0].outcome, 'succeeded');
  assert.equal(result.outcomes[0].selected_option_text, null);
});

test('native select uses the planned option value and reports the planned label', async () => {
  const calls = [];
  const transport = baseTransport({
    select: async (selector, optionValue) => {
      calls.push([selector, optionValue]);
    },
  }, committedNativePost());
  const { result } = await executeOmpBrowserActionPlan(nativeSelectPlan(), transport);
  assert.deepEqual(calls, [['[id="country"]', 'us']]);
  assert.equal(result.outcomes[0].outcome, 'succeeded');
  assert.equal(result.outcomes[0].selected_option_text, 'United States');
});

test('successful select without committed post-observation state is rejected', async () => {
  const plan = nativeSelectPlan();
  const cases = [
    {
      name: 'missing control binding',
      post: postObservation(),
    },
    {
      name: 'no committed option',
      post: postObservation('obs-1', [control('country', {
        kind: 'select',
        tag: 'select',
        type: 'select',
        role: 'combobox',
        value: 'us',
        options: [
          { value: 'us', label: 'United States', disabled: false, selected: false },
          { value: 'ca', label: 'Canada', disabled: false, selected: false },
        ],
        selected: [],
      })]),
    },
    {
      name: 'different option committed',
      post: postObservation('obs-1', [control('country', {
        kind: 'select',
        tag: 'select',
        type: 'select',
        role: 'combobox',
        value: 'ca',
        options: [
          { value: 'us', label: 'United States', disabled: false, selected: false },
          { value: 'ca', label: 'Canada', disabled: false, selected: true },
        ],
        selected: ['ca'],
      })]),
    },
  ];
  for (const entry of cases) {
    const calls = [];
    const transport = baseTransport({
      select: async (selector, optionValue) => {
        calls.push([selector, optionValue]);
      },
      observe: async () => entry.post,
    });
    await assert.rejects(
      executeOmpBrowserActionPlan(plan, transport),
      (error) => {
        assert.ok(error instanceof OmpBrowserExecutorError);
        assert.equal(error.code, 'E_OMP_BROWSER_RESULT_INVALID');
        assert.ok(!String(error.message).includes(SECRET));
        return true;
      },
    );
    assert.deepEqual(calls, [['[id="country"]', 'us']]);
  }
});

test('custom select already-open path clicks the exact option without a query', async () => {
  const calls = { click: [], press: [], fill: [], readOptions: 0, clickOption: [] };
  const transport = baseTransport({
    click: async (selector) => {
      calls.click.push(selector);
    },
    press: async (selector, key) => {
      calls.press.push([selector, key]);
    },
    fill: async () => {
      calls.fill.push('unexpected');
    },
    readOptions: async () => {
      calls.readOptions += 1;
      return [{ key: 'eng', text: 'Engineering', value: 'eng', disabled: false }];
    },
    clickOption: async (candidate) => {
      calls.clickOption.push(candidate);
    },
    now: () => 0,
    sleep: async () => {},
  }, committedCustomPost());

  const { result } = await executeOmpBrowserActionPlan(customSelectPlan(), transport);
  assert.deepEqual(calls.click, ['[id="role"]']);
  assert.deepEqual(calls.press, [['[id="role"]', 'ArrowDown']]);
  assert.deepEqual(calls.fill, []);
  assert.equal(calls.readOptions, 1);
  assert.equal(calls.clickOption.length, 1);
  assert.deepEqual(calls.clickOption[0], {
    key: 'eng',
    text: 'Engineering',
    value: 'eng',
    strategy: 'exact_text',
  });
  assert.equal(Object.isFrozen(calls.clickOption[0]), true);
  assert.equal(result.outcomes[0].outcome, 'succeeded');
  assert.equal(result.outcomes[0].selected_option_text, 'Engineering');
});

test('custom select one-query path fills the planned query exactly once', async () => {
  let fillCount = 0;
  let readCount = 0;
  const clicked = [];
  const transport = baseTransport({
    readOptions: async () => {
      readCount += 1;
      return fillCount === 0
        ? []
        : [{ key: 'eng', text: 'Engineering', value: 'eng', disabled: false }];
    },
    fill: async (selector, value) => {
      fillCount += 1;
      assert.equal(selector, '[id="role"]');
      assert.equal(value, 'Engineering');
    },
    clickOption: async (candidate) => {
      clicked.push(candidate);
    },
    now: () => 0,
    sleep: async () => {},
  }, committedCustomPost());

  const { result } = await executeOmpBrowserActionPlan(customSelectPlan(), transport);
  assert.equal(fillCount, 1);
  assert.ok(readCount > 1);
  assert.equal(clicked.length, 1);
  assert.equal(result.outcomes[0].outcome, 'succeeded');
  assert.equal(result.outcomes[0].selected_option_text, 'Engineering');
});

test('custom select fills the query step selector when it differs from the open step', async () => {
  const plan = structuredClone(customSelectPlan());
  plan.actions[0].steps[1].selector = '[id="role-search"]';
  validateBrowserActionPlan(plan);

  const clickCalls = [];
  const pressCalls = [];
  const fillCalls = [];
  let fillCount = 0;
  const transport = baseTransport({
    click: async (selector) => {
      clickCalls.push(selector);
    },
    press: async (selector, key) => {
      pressCalls.push([selector, key]);
    },
    readOptions: async () => (fillCount === 0
      ? []
      : [{ key: 'eng', text: 'Engineering', value: 'eng', disabled: false }]),
    fill: async (selector, value) => {
      fillCount += 1;
      fillCalls.push([selector, value]);
    },
    now: () => 0,
    sleep: async () => {},
  }, committedCustomPost());

  const { result } = await executeOmpBrowserActionPlan(plan, transport);
  assert.deepEqual(clickCalls, ['[id="role"]']);
  assert.deepEqual(pressCalls, [['[id="role"]', 'ArrowDown']]);
  assert.deepEqual(fillCalls, [['[id="role-search"]', 'Engineering']]);
  assert.equal(fillCount, 1);
  assert.equal(result.outcomes[0].outcome, 'succeeded');
  assert.equal(result.outcomes[0].selected_option_text, 'Engineering');
});

test('custom select discovery uses the exact-option-visible timeout, not the commit timeout', async () => {
  const plan = structuredClone(customSelectPlan());
  plan.actions[0].steps[1].wait_after.timeoutMs = 40;
  plan.actions[0].steps[2].wait_after.timeoutMs = 15_000;
  validateBrowserActionPlan(plan);

  let reads = 0;
  const transport = baseTransport({
    readOptions: async () => {
      reads += 1;
      return [];
    },
    now: () => 0,
    sleep: async () => {},
  });

  const { result } = await executeOmpBrowserActionPlan(plan, transport);
  assert.equal(result.outcomes[0].outcome, 'failed');
  assert.equal(result.outcomes[0].error_code, 'E_CUSTOM_SELECT_OPTION_TIMEOUT');
  assert.equal(reads, 3);
});

test('fill batch stops at the first failed action and re-observes exactly once', async () => {
  const plan = batchPlan(3);
  const calls = [];
  const transport = baseTransport({
    fill: async (selector, value) => {
      calls.push(['fill', selector, value]);
      if (value === 'value-1') throw new Error(SECRET);
    },
    observe: async () => {
      calls.push(['observe']);
      return postObservation();
    },
  });

  const { result } = await executeOmpBrowserActionPlan(plan, transport);
  assert.deepEqual(calls, [
    ['fill', '[id="first"]', 'value-0'],
    ['fill', '[id="second"]', 'value-1'],
    ['observe'],
  ]);
  assert.equal(result.outcomes.length, 2);
  assert.equal(result.outcomes[0].outcome, 'succeeded');
  assert.deepEqual(result.outcomes[1], {
    action_id: plan.actions[1].action_id,
    outcome: 'failed',
    error_code: 'E_OMP_BROWSER_HELPER_FAILED',
    driver: 'omp_browser',
    selected_option_text: null,
  });
});

test('helper failures sanitize to a stable code without leaking values', async () => {
  const syncTransport = baseTransport({
    fill: () => {
      throw new Error(`boom ${SECRET}`);
    },
  });
  const sync = await executeOmpBrowserActionPlan(fillPlan(), syncTransport);
  assert.equal(sync.result.outcomes[0].outcome, 'failed');
  assert.equal(sync.result.outcomes[0].error_code, 'E_OMP_BROWSER_HELPER_FAILED');
  assert.ok(!JSON.stringify(sync.result).includes(SECRET));

  const asyncTransport = baseTransport({
    select: async () => {
      throw new Error(`async ${SECRET}`);
    },
  });
  const asyncResult = await executeOmpBrowserActionPlan(nativeSelectPlan(), asyncTransport);
  assert.equal(asyncResult.result.outcomes[0].outcome, 'failed');
  assert.equal(asyncResult.result.outcomes[0].error_code, 'E_OMP_BROWSER_HELPER_FAILED');
  assert.ok(!JSON.stringify(asyncResult.result).includes(SECRET));
});

test('never-settling direct helpers time out to the sanitized helper code', async () => {
  const calls = [];
  const transport = baseTransport({
    fill: () => new Promise(() => {}),
    observe: async () => {
      calls.push('observe');
      return postObservation();
    },
  });
  const started = Date.now();
  const { result } = await executeOmpBrowserActionPlan(
    fillPlan(),
    transport,
    { callbackTimeoutMs: 25 },
  );
  const elapsed = Date.now() - started;
  assert.ok(elapsed < 1000, `expected a bounded timeout, took ${elapsed}ms`);
  assert.equal(result.outcomes[0].outcome, 'failed');
  assert.equal(result.outcomes[0].error_code, 'E_OMP_BROWSER_HELPER_FAILED');
  assert.ok(!JSON.stringify(result).includes(SECRET));
  assert.deepEqual(calls, ['observe']);

  const customPlan = structuredClone(customSelectPlan());
  customPlan.actions[0].steps[1].wait_after.timeoutMs = 40;
  validateBrowserActionPlan(customPlan);
  const customTransport = baseTransport({
    click: () => new Promise(() => {}),
    now: () => 0,
    sleep: async () => {},
  });
  const customStarted = Date.now();
  const customResult = await executeOmpBrowserActionPlan(customPlan, customTransport);
  const customElapsed = Date.now() - customStarted;
  assert.ok(customElapsed < 1000, `expected a bounded timeout, took ${customElapsed}ms`);
  assert.equal(customResult.result.outcomes[0].outcome, 'failed');
  assert.equal(customResult.result.outcomes[0].error_code, 'E_CUSTOM_SELECT_CALLBACK');
});

test('never-settling observe times out to the sanitized observe code', async () => {
  const transport = baseTransport({
    observe: () => new Promise(() => {}),
  });
  const started = Date.now();
  await assert.rejects(
    executeOmpBrowserActionPlan(fillPlan(), transport, { callbackTimeoutMs: 25 }),
    (error) => {
      assert.ok(error instanceof OmpBrowserExecutorError);
      assert.equal(error.code, 'E_OMP_BROWSER_OBSERVE_FAILED');
      assert.ok(!String(error.message).includes(SECRET));
      return true;
    },
  );
  const elapsed = Date.now() - started;
  assert.ok(elapsed < 1000, `expected a bounded timeout, took ${elapsed}ms`);
});

test('custom select executor codes are preserved unchanged', async () => {
  const plan = customSelectPlan();
  const cases = [
    {
      name: 'ambiguous',
      readOptions: async () => [
        { key: 'one', text: 'Engineering', value: 'one', disabled: false },
        { key: 'two', text: 'Engineering', value: 'two', disabled: false },
      ],
      code: 'E_CUSTOM_SELECT_OPTION_AMBIGUOUS',
    },
    {
      name: 'disabled',
      readOptions: async () => [{ key: 'eng', text: 'Engineering', value: 'eng', disabled: true }],
      code: 'E_CUSTOM_SELECT_OPTION_DISABLED',
    },
    {
      name: 'callback failure',
      readOptions: async () => {
        throw new Error(SECRET);
      },
      code: 'E_CUSTOM_SELECT_CALLBACK',
    },
  ];
  for (const entry of cases) {
    const transport = baseTransport({
      readOptions: entry.readOptions,
      now: () => 0,
      sleep: async () => {},
    });
    const { result } = await executeOmpBrowserActionPlan(plan, transport);
    assert.equal(result.outcomes[0].outcome, 'failed');
    assert.equal(result.outcomes[0].error_code, entry.code);
    assert.ok(!JSON.stringify(result).includes(SECRET));
  }
});

test('post-action observation must chain to the plan observation or the call fails', async () => {
  const plan = fillPlan();
  const cases = [
    {
      name: 'unchained previous observation',
      observe: async () => observation('obs-2', [], 'obs-other'),
    },
    {
      name: 'same observation id',
      observe: async () => observation('obs-1', [], 'obs-1'),
    },
    {
      name: 'invalid observation payload',
      observe: async () => ({ schema: 'not-an-observation' }),
    },
    {
      name: 'observe callback rejects',
      observe: async () => {
        throw new Error(SECRET);
      },
    },
  ];
  for (const entry of cases) {
    await assert.rejects(
      executeOmpBrowserActionPlan(plan, baseTransport({ observe: entry.observe })),
      (error) => {
        assert.ok(error instanceof OmpBrowserExecutorError);
        assert.equal(error.code, 'E_OMP_BROWSER_OBSERVE_FAILED');
        assert.ok(!String(error.message).includes(SECRET));
        return true;
      },
    );
  }
});

test('transport requires exactly the documented callbacks', async () => {
  const plan = fillPlan();
  const valid = baseTransport();
  const invalidTransports = [
    { ...valid, extra: async () => {} },
    (() => {
      const missing = { ...valid };
      delete missing.observe;
      return missing;
    })(),
    { ...valid, fill: 'not-a-function' },
    { ...valid, now: 42 },
    null,
  ];
  for (const transport of invalidTransports) {
    await assert.rejects(
      executeOmpBrowserActionPlan(plan, transport),
      (error) => error instanceof OmpBrowserExecutorError
        && error.code === 'E_OMP_BROWSER_TRANSPORT_INVALID',
    );
  }

  const { result } = await executeOmpBrowserActionPlan(plan, {
    ...valid,
    now: () => 0,
    sleep: async () => {},
  });
  assert.equal(result.outcomes[0].outcome, 'succeeded');
});

test('rejects structurally invalid plans and foreign drivers before any browser work', async () => {
  const touched = [];
  const transport = baseTransport({
    fill: async () => {
      touched.push('fill');
    },
    observe: async () => {
      touched.push('observe');
      return postObservation();
    },
  });

  await assert.rejects(
    executeOmpBrowserActionPlan({ schema: 'phase1-browser-action-plan-v2', actions: [] }, transport),
    (error) => error instanceof OmpBrowserExecutorError
      && error.code === 'E_OMP_BROWSER_PLAN_INVALID',
  );
  assert.deepEqual(touched, []);

  const current = observation('obs-1', [control('name')]);
  const ledger = resolvedLedger(current, [{ field_id: 'name', value: 'Ada' }]);
  const foreign = createBrowserActionPlan({
    observation: current,
    ledger,
    decisions: [decision('name', 'fill_text', 'Ada')],
    answerAliases: { name: { alias: 'full_name', value: 'Ada' } },
    optionMatches: {},
    driver: 'playwright_cli',
  });
  await assert.rejects(
    executeOmpBrowserActionPlan(foreign, transport),
    (error) => error instanceof OmpBrowserExecutorError
      && error.code === 'E_OMP_BROWSER_DRIVER_MISMATCH',
  );
  assert.deepEqual(touched, []);
});

test('never replays or re-attempts actions at this layer', async () => {
  const calls = [];
  const transport = baseTransport({
    fill: async (_selector, value) => {
      calls.push(`fill:${value}`);
      if (value === 'value-1') throw new Error('stop');
    },
    observe: async () => {
      calls.push('observe');
      return postObservation();
    },
  });
  const { result } = await executeOmpBrowserActionPlan(batchPlan(3), transport);
  assert.deepEqual(calls, ['fill:value-0', 'fill:value-1', 'observe']);
  assert.equal(result.outcomes.length, 2);

  let opens = 0;
  const customTransport = baseTransport({
    click: async () => {
      opens += 1;
    },
    readOptions: async () => [
      { key: 'a', text: 'Engineering', value: 'a', disabled: false },
      { key: 'b', text: 'Engineering', value: 'b', disabled: false },
    ],
    now: () => 0,
    sleep: async () => {},
  });
  await executeOmpBrowserActionPlan(customSelectPlan(), customTransport);
  assert.equal(opens, 1);
});

test('toggle clicks the planned control', async () => {
  const calls = [];
  const transport = baseTransport({
    click: async (selector) => {
      calls.push(selector);
    },
  });
  const { result } = await executeOmpBrowserActionPlan(togglePlan(), transport);
  assert.deepEqual(calls, ['[id="consent"]']);
  assert.equal(result.outcomes[0].outcome, 'succeeded');
  assert.equal(result.outcomes[0].selected_option_text, null);
});

test('upload uses the planned file path', async () => {
  const calls = [];
  const transport = baseTransport({
    uploadFile: async (selector, filePath) => {
      calls.push([selector, filePath]);
    },
  }, committedUploadPost());
  const { result } = await executeOmpBrowserActionPlan(uploadPlan(), transport);
  assert.deepEqual(calls, [['[id="resume"]', '/tmp/resume.pdf']]);
  assert.equal(result.outcomes[0].outcome, 'succeeded');
  assert.equal(result.outcomes[0].selected_option_text, null);
});

test('exposes the stable executor error code vocabulary', () => {
  assert.deepEqual(OMP_BROWSER_EXECUTOR_ERROR_CODES, [
    'E_OMP_BROWSER_TRANSPORT_INVALID',
    'E_OMP_BROWSER_PLAN_INVALID',
    'E_OMP_BROWSER_DRIVER_MISMATCH',
    'E_OMP_BROWSER_HELPER_FAILED',
    'E_OMP_BROWSER_OBSERVE_FAILED',
    'E_OMP_BROWSER_RESULT_INVALID',
  ]);
  const error = new OmpBrowserExecutorError('E_OMP_BROWSER_OBSERVE_FAILED');
  assert.equal(error.code, 'E_OMP_BROWSER_OBSERVE_FAILED');
  assert.equal(error.name, 'OmpBrowserExecutorError');
  assert.equal(new OmpBrowserExecutorError(SECRET).code, 'E_OMP_BROWSER_RESULT_INVALID');
});
