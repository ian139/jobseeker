import assert from 'node:assert/strict';
import fsp from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import test from 'node:test';

import {
  CmuxPlanExecutorError,
  executeCmuxPlan,
  loadPlanSecure,
  parseCliArgs,
  runCmuxPlanExecutor,
  validateCmuxPath,
  validatePlanForCmux,
  validateSurfaceRef,
} from '../src/phase1/cmux-plan-executor.mjs';

function createValidPlan({
  planId = 'plan-001',
  actions = null,
  mode = null,
} = {}) {
  const defaultActions = [
    {
      action_id: 'act-001',
      field_id: 'first_name',
      stable_id: 'first_name',
      control_reference: 'obs-001:c-1',
      answer_alias: 'first_name',
      semantic_action: 'fill_text',
      retry_of: null,
      decision: {
        observationId: 'obs-001',
        fieldId: 'first_name',
        controlReference: 'obs-001:c-1',
        fieldPolicy: 'identity',
        proposedAnswer: 'Jane',
        answerSource: 'profile',
        evidenceReferences: ['profile:first_name'],
        inferenceRationaleDigest: null,
        inferenceEvidenceDigests: null,
        proposedAction: 'fill_text',
        expectedRetainedState: { value: 'Jane' },
        modelTier: 'standard',
        confidence: 1.0,
        reasonCode: 'direct_fill',
        reobservationRequired: true,
        automaticSubmissionEligible: false,
      },
      steps: [
        {
          sequence: 1,
          helper: 'fill',
          selector: 'input#first_name',
          value: 'SECRET_PRIVATE_VALUE_JANE',
          option_value: null,
          file_path: null,
          option_text: null,
          exact: null,
          normalized_action: {
            action: 'fill_text',
            observationId: 'obs-001',
            fieldId: 'first_name',
            controlReference: 'obs-001:c-1',
            value: 'SECRET_PRIVATE_VALUE_JANE',
          },
          wait_after: null,
          reobserve_after: null,
        },
      ],
      retention: {
        kind: 'exact_value',
        expected_value_digest: 'a'.repeat(64),
        option_text_digest: null,
        file_name: null,
        artifact_sha256: null,
      },
    },
  ];

  const resolvedActions = actions ?? defaultActions;
  const resolvedMode = mode ?? (resolvedActions.length === 1 ? 'single_action' : 'fill_batch');

  return {
    schema: 'phase1-browser-action-plan-v2',
    plan_id: planId,
    created_at: '2026-08-09T00:00:00.000Z',
    ats: 'greenhouse',
    observation_id: 'obs-001',
    driver: 'omp_browser',
    screenshot_sha256: null,
    mode: resolvedMode,
    actions: resolvedActions,
    fallback_order: ['omp_browser', 'playwright_cli', 'computer'],
    reobserve_after: true,
  };
}

test('parseCliArgs parses valid args and throws on missing or invalid args', () => {
  const parsed = parseCliArgs([
    'node',
    'bin/augjobs-cmux-execute',
    '--cmux',
    '/usr/local/bin/cmux',
    '--surface',
    'surface-1',
    '--plan',
    '/tmp/plan.json',
  ]);
  assert.deepEqual(parsed, {
    cmuxPath: '/usr/local/bin/cmux',
    surface: 'surface-1',
    planPath: '/tmp/plan.json',
  });

  assert.throws(
    () => parseCliArgs(['node', 'bin/augjobs-cmux-execute', '--cmux', '/usr/local/bin/cmux']),
    (err) => err instanceof CmuxPlanExecutorError && err.code === 'E_CLI_MISSING_ARG',
  );

  assert.throws(
    () => parseCliArgs(['node', 'bin/augjobs-cmux-execute', '--cmux', '/usr/local/bin/cmux', '--surface', 's1', '--plan', 'p1', '--invalid-flag']),
    (err) => err instanceof CmuxPlanExecutorError && err.code === 'E_CLI_INVALID_ARGS',
  );
});

test('validateSurfaceRef validates surface ref format', () => {
  assert.equal(validateSurfaceRef('surface_1.2'), 'surface_1.2');
  assert.throws(
    () => validateSurfaceRef(''),
    (err) => err instanceof CmuxPlanExecutorError && err.code === 'E_INVALID_SURFACE_REF',
  );
  assert.throws(
    () => validateSurfaceRef('invalid surface space'),
    (err) => err instanceof CmuxPlanExecutorError && err.code === 'E_INVALID_SURFACE_REF',
  );
});

test('validateCmuxPath rejects non-absolute or non-executable files', async () => {
  await assert.rejects(
    async () => validateCmuxPath('relative/cmux'),
    (err) => err instanceof CmuxPlanExecutorError && err.code === 'E_INVALID_CMUX_PATH',
  );
  await assert.rejects(
    async () => validateCmuxPath('/nonexistent/path/to/cmux'),
    (err) => err instanceof CmuxPlanExecutorError && err.code === 'E_INVALID_CMUX_PATH',
  );
});

test('executeCmuxPlan passes exact argv and no shell to fake executable for fill, click, select', async () => {
  const calls = [];
  const fakeExecFile = async (executable, args, options) => {
    calls.push({ executable, args, options });
    return { stdout: '', stderr: '' };
  };

  const fillPlan = createValidPlan({
    planId: 'plan-fill',
    actions: [
      {
        action_id: 'act-fill',
        field_id: 'first_name',
        stable_id: 'first_name',
        control_reference: 'obs-001:c-1',
        answer_alias: 'first_name',
        semantic_action: 'fill_text',
        retry_of: null,
        decision: {
          observationId: 'obs-001',
          fieldId: 'first_name',
          controlReference: 'obs-001:c-1',
          fieldPolicy: 'identity',
          proposedAnswer: 'Jane',
          answerSource: 'profile',
          evidenceReferences: ['profile:first_name'],
          inferenceRationaleDigest: null,
          inferenceEvidenceDigests: null,
          proposedAction: 'fill_text',
          expectedRetainedState: { value: 'Jane' },
          modelTier: 'standard',
          confidence: 1.0,
          reasonCode: 'direct_fill',
          reobservationRequired: true,
          automaticSubmissionEligible: false,
        },
        steps: [
          {
            sequence: 1,
            helper: 'fill',
            selector: 'input#first_name',
            value: 'SECRET_VALUE_JANE',
            option_value: null,
            file_path: null,
            option_text: null,
            exact: null,
            normalized_action: {
              action: 'fill_text',
              observationId: 'obs-001',
              fieldId: 'first_name',
              controlReference: 'obs-001:c-1',
              value: 'SECRET_VALUE_JANE',
            },
            wait_after: null,
            reobserve_after: null,
          },
        ],
        retention: {
          kind: 'exact_value',
          expected_value_digest: 'a'.repeat(64),
          option_text_digest: null,
          file_name: null,
          artifact_sha256: null,
        },
      },
    ],
  });

  const clickPlan = createValidPlan({
    planId: 'plan-click',
    actions: [
      {
        action_id: 'act-click',
        field_id: 'agree_terms',
        stable_id: 'agree_terms',
        control_reference: 'obs-001:c-2',
        answer_alias: 'agree_terms',
        semantic_action: 'toggle',
        retry_of: null,
        decision: {
          observationId: 'obs-001',
          fieldId: 'agree_terms',
          controlReference: 'obs-001:c-2',
          fieldPolicy: 'legal',
          proposedAnswer: true,
          answerSource: 'user',
          evidenceReferences: [],
          inferenceRationaleDigest: null,
          inferenceEvidenceDigests: null,
          proposedAction: 'toggle',
          expectedRetainedState: { checked: true },
          modelTier: 'standard',
          confidence: 1.0,
          reasonCode: 'direct_toggle',
          reobservationRequired: true,
          automaticSubmissionEligible: false,
        },
        steps: [
          {
            sequence: 1,
            helper: 'click',
            selector: 'input#agree_terms',
            value: null,
            option_value: null,
            file_path: null,
            option_text: null,
            exact: null,
            normalized_action: {
              action: 'toggle',
              observationId: 'obs-001',
              fieldId: 'agree_terms',
              controlReference: 'obs-001:c-2',
              checked: true,
            },
            wait_after: null,
            reobserve_after: null,
          },
        ],
        retention: {
          kind: 'exact_value',
          expected_value_digest: 'b'.repeat(64),
          option_text_digest: null,
          file_name: null,
          artifact_sha256: null,
        },
      },
    ],
  });

  const selectPlan = createValidPlan({
    planId: 'plan-select',
    actions: [
      {
        action_id: 'act-select',
        field_id: 'country',
        stable_id: 'country',
        control_reference: 'obs-001:c-3',
        answer_alias: 'country',
        semantic_action: 'select_option',
        retry_of: null,
        decision: {
          observationId: 'obs-001',
          fieldId: 'country',
          controlReference: 'obs-001:c-3',
          fieldPolicy: 'identity',
          proposedAnswer: 'United States',
          answerSource: 'profile',
          evidenceReferences: ['profile:country'],
          inferenceRationaleDigest: null,
          inferenceEvidenceDigests: null,
          proposedAction: 'select_option',
          expectedRetainedState: { value: 'US' },
          modelTier: 'standard',
          confidence: 1.0,
          reasonCode: 'direct_select',
          reobservationRequired: true,
          automaticSubmissionEligible: false,
        },
        steps: [
          {
            sequence: 1,
            helper: 'select',
            selector: 'select#country',
            value: null,
            option_value: 'US',
            file_path: null,
            option_text: 'United States',
            exact: null,
            normalized_action: {
              action: 'select_option',
              observationId: 'obs-001',
              fieldId: 'country',
              controlReference: 'obs-001:c-3',
              optionValue: 'US',
            },
            wait_after: null,
            reobserve_after: null,
          },
        ],
        retention: {
          kind: 'normalized_option',
          expected_value_digest: 'c'.repeat(64),
          option_text_digest: 'd'.repeat(64),
          file_name: null,
          artifact_sha256: null,
        },
      },
    ],
  });

  const fakeExecutablePath = path.resolve(process.execPath);

  const resFill = await executeCmuxPlan({
    cmuxPath: fakeExecutablePath,
    surface: 'main-surface',
    plan: fillPlan,
    execFileFn: fakeExecFile,
  });
  const resClick = await executeCmuxPlan({
    cmuxPath: fakeExecutablePath,
    surface: 'main-surface',
    plan: clickPlan,
    execFileFn: fakeExecFile,
  });
  const resSelect = await executeCmuxPlan({
    cmuxPath: fakeExecutablePath,
    surface: 'main-surface',
    plan: selectPlan,
    execFileFn: fakeExecFile,
  });

  assert.equal(resFill.status, 'ok');
  assert.equal(resClick.status, 'ok');
  assert.equal(resSelect.status, 'ok');

  assert.equal(calls.length, 3);

  assert.equal(calls[0].executable, fakeExecutablePath);
  assert.deepEqual(calls[0].args, [
    'browser',
    '--surface',
    'main-surface',
    'fill',
    '--selector',
    'input#first_name',
    '--text',
    'SECRET_VALUE_JANE',
  ]);
  assert.equal(calls[0].options.shell, undefined);

  assert.equal(calls[1].executable, fakeExecutablePath);
  assert.deepEqual(calls[1].args, [
    'browser',
    '--surface',
    'main-surface',
    'click',
    '--selector',
    'input#agree_terms',
  ]);
  assert.equal(calls[1].options.shell, undefined);

  assert.equal(calls[2].executable, fakeExecutablePath);
  assert.deepEqual(calls[2].args, [
    'browser',
    '--surface',
    'main-surface',
    'select',
    '--selector',
    'select#country',
    '--value',
    'US',
  ]);
  assert.equal(calls[2].options.shell, undefined);
});

test('executor stdout / result metadata contains no private values or selectors', async () => {
  const secretValue = 'SUPER_SECRET_SSN_12345';
  const secretSelector = 'input[data-private-id="ssn_field"]';

  const plan = createValidPlan({
    actions: [
      {
        action_id: 'act-secret',
        field_id: 'ssn',
        stable_id: 'ssn',
        control_reference: 'obs-001:c-secret',
        answer_alias: 'ssn',
        semantic_action: 'fill_text',
        retry_of: null,
        decision: {
          observationId: 'obs-001',
          fieldId: 'ssn',
          controlReference: 'obs-001:c-secret',
          fieldPolicy: 'identity',
          proposedAnswer: secretValue,
          answerSource: 'profile',
          evidenceReferences: ['profile:ssn'],
          inferenceRationaleDigest: null,
          inferenceEvidenceDigests: null,
          proposedAction: 'fill_text',
          expectedRetainedState: { value: secretValue },
          modelTier: 'standard',
          confidence: 1.0,
          reasonCode: 'direct_fill',
          reobservationRequired: true,
          automaticSubmissionEligible: false,
        },
        steps: [
          {
            sequence: 1,
            helper: 'fill',
            selector: secretSelector,
            value: secretValue,
            option_value: null,
            file_path: null,
            option_text: null,
            exact: null,
            normalized_action: {
              action: 'fill_text',
              observationId: 'obs-001',
              fieldId: 'ssn',
              controlReference: 'obs-001:c-secret',
              value: secretValue,
            },
            wait_after: null,
            reobserve_after: null,
          },
        ],
        retention: {
          kind: 'exact_value',
          expected_value_digest: 'd'.repeat(64),
          option_text_digest: null,
          file_name: null,
          artifact_sha256: null,
        },
      },
    ],
  });

  const fakeExecutablePath = path.resolve(process.execPath);
  const fakeExecFile = async () => ({ stdout: '', stderr: '' });

  const result = await executeCmuxPlan({
    cmuxPath: fakeExecutablePath,
    surface: 'main-surface',
    plan,
    execFileFn: fakeExecFile,
  });

  const jsonString = JSON.stringify(result);
  assert.equal(jsonString.includes(secretValue), false);
  assert.equal(jsonString.includes(secretSelector), false);
  assert.deepEqual(Object.keys(result), [
    'status',
    'plan_id',
    'completed_action_count',
    'completed_step_count',
    'failed_action_id',
    'error_code',
  ]);
});

test('executor stops on first failure and records exact completion state', async () => {
  const calls = [];
  const fakeExecFile = async (executable, args) => {
    calls.push({ executable, args });
    if (calls.length === 2) {
      const err = new Error('CMUX execution process exited with code 1');
      err.code = 1;
      throw err;
    }
    return { stdout: '', stderr: '' };
  };

  const plan = createValidPlan({
    actions: [
      {
        action_id: 'act-1',
        field_id: 'first_name',
        stable_id: 'first_name',
        control_reference: 'obs-001:c-1',
        answer_alias: 'first_name',
        semantic_action: 'fill_text',
        retry_of: null,
        decision: {
          observationId: 'obs-001',
          fieldId: 'first_name',
          controlReference: 'obs-001:c-1',
          fieldPolicy: 'identity',
          proposedAnswer: 'Jane',
          answerSource: 'profile',
          evidenceReferences: ['profile:first_name'],
          inferenceRationaleDigest: null,
          inferenceEvidenceDigests: null,
          proposedAction: 'fill_text',
          expectedRetainedState: { value: 'Jane' },
          modelTier: 'standard',
          confidence: 1.0,
          reasonCode: 'direct_fill',
          reobservationRequired: true,
          automaticSubmissionEligible: false,
        },
        steps: [
          {
            sequence: 1,
            helper: 'fill',
            selector: 'input#first_name',
            value: 'Jane',
            option_value: null,
            file_path: null,
            option_text: null,
            exact: null,
            normalized_action: {
              action: 'fill_text',
              observationId: 'obs-001',
              fieldId: 'first_name',
              controlReference: 'obs-001:c-1',
              value: 'Jane',
            },
            wait_after: null,
            reobserve_after: null,
          },
        ],
        retention: {
          kind: 'exact_value',
          expected_value_digest: 'e'.repeat(64),
          option_text_digest: null,
          file_name: null,
          artifact_sha256: null,
        },
      },
      {
        action_id: 'act-2',
        field_id: 'last_name',
        stable_id: 'last_name',
        control_reference: 'obs-001:c-2',
        answer_alias: 'last_name',
        semantic_action: 'fill_text',
        retry_of: null,
        decision: {
          observationId: 'obs-001',
          fieldId: 'last_name',
          controlReference: 'obs-001:c-2',
          fieldPolicy: 'identity',
          proposedAnswer: 'Doe',
          answerSource: 'profile',
          evidenceReferences: ['profile:last_name'],
          inferenceRationaleDigest: null,
          inferenceEvidenceDigests: null,
          proposedAction: 'fill_text',
          expectedRetainedState: { value: 'Doe' },
          modelTier: 'standard',
          confidence: 1.0,
          reasonCode: 'direct_fill',
          reobservationRequired: true,
          automaticSubmissionEligible: false,
        },
        steps: [
          {
            sequence: 1,
            helper: 'fill',
            selector: 'input#last_name',
            value: 'Doe',
            option_value: null,
            file_path: null,
            option_text: null,
            exact: null,
            normalized_action: {
              action: 'fill_text',
              observationId: 'obs-001',
              fieldId: 'last_name',
              controlReference: 'obs-001:c-2',
              value: 'Doe',
            },
            wait_after: null,
            reobserve_after: null,
          },
        ],
        retention: {
          kind: 'exact_value',
          expected_value_digest: 'f'.repeat(64),
          option_text_digest: null,
          file_name: null,
          artifact_sha256: null,
        },
      },
      {
        action_id: 'act-3',
        field_id: 'email',
        stable_id: 'email',
        control_reference: 'obs-001:c-3',
        answer_alias: 'email',
        semantic_action: 'fill_text',
        retry_of: null,
        decision: {
          observationId: 'obs-001',
          fieldId: 'email',
          controlReference: 'obs-001:c-3',
          fieldPolicy: 'identity',
          proposedAnswer: 'jane@example.com',
          answerSource: 'profile',
          evidenceReferences: ['profile:email'],
          inferenceRationaleDigest: null,
          inferenceEvidenceDigests: null,
          proposedAction: 'fill_text',
          expectedRetainedState: { value: 'jane@example.com' },
          modelTier: 'standard',
          confidence: 1.0,
          reasonCode: 'direct_fill',
          reobservationRequired: true,
          automaticSubmissionEligible: false,
        },
        steps: [
          {
            sequence: 1,
            helper: 'fill',
            selector: 'input#email',
            value: 'jane@example.com',
            option_value: null,
            file_path: null,
            option_text: null,
            exact: null,
            normalized_action: {
              action: 'fill_text',
              observationId: 'obs-001',
              fieldId: 'email',
              controlReference: 'obs-001:c-3',
              value: 'jane@example.com',
            },
            wait_after: null,
            reobserve_after: null,
          },
        ],
        retention: {
          kind: 'exact_value',
          expected_value_digest: '1'.repeat(64),
          option_text_digest: null,
          file_name: null,
          artifact_sha256: null,
        },
      },
    ],
  });

  const fakeExecutablePath = path.resolve(process.execPath);

  const result = await executeCmuxPlan({
    cmuxPath: fakeExecutablePath,
    surface: 'main-surface',
    plan,
    execFileFn: fakeExecFile,
  });

  assert.equal(result.status, 'failed');
  assert.equal(result.plan_id, 'plan-001');
  assert.equal(result.completed_action_count, 1);
  assert.equal(result.completed_step_count, 1);
  assert.equal(result.failed_action_id, 'act-2');
  assert.equal(result.error_code, 'E_CMUX_EXEC_FAILED');

  assert.equal(calls.length, 2);
});

test('unsupported upload/final submission paths rejected before execution', async () => {
  let callCount = 0;
  const fakeExecFile = async () => {
    callCount += 1;
    return { stdout: '', stderr: '' };
  };

  const uploadPlan = {
    schema: 'phase1-browser-action-plan-v2',
    plan_id: 'plan-upload',
    created_at: '2026-08-09T00:00:00.000Z',
    ats: 'greenhouse',
    observation_id: 'obs-001',
    driver: 'omp_browser',
    screenshot_sha256: null,
    mode: 'single_action',
    actions: [
      {
        action_id: 'act-upload',
        field_id: 'resume',
        stable_id: 'resume',
        control_reference: 'obs-001:c-resume',
        answer_alias: null,
        semantic_action: 'upload_file',
        retry_of: null,
        decision: {
          observationId: 'obs-001',
          fieldId: 'resume',
          controlReference: 'obs-001:c-resume',
          fieldPolicy: 'qualification',
          proposedAnswer: '/tmp/resume.pdf',
          answerSource: 'resume',
          evidenceReferences: ['resume:file'],
          inferenceRationaleDigest: null,
          inferenceEvidenceDigests: null,
          proposedAction: 'upload_file',
          expectedRetainedState: { file: { present: true, accept: ['.pdf'] } },
          modelTier: 'standard',
          confidence: 1.0,
          reasonCode: 'direct_upload',
          reobservationRequired: true,
          automaticSubmissionEligible: false,
        },
        steps: [
          {
            sequence: 1,
            helper: 'uploadFile',
            selector: 'input#resume',
            value: null,
            option_value: null,
            file_path: '/tmp/resume.pdf',
            option_text: null,
            exact: null,
            normalized_action: {
              action: 'upload_file',
              observationId: 'obs-001',
              fieldId: 'resume',
              controlReference: 'obs-001:c-resume',
              filePath: '/tmp/resume.pdf',
            },
            wait_after: null,
            reobserve_after: null,
          },
        ],
        retention: {
          kind: 'upload_file',
          expected_value_digest: '2'.repeat(64),
          option_text_digest: null,
          file_name: 'resume.pdf',
          artifact_sha256: '3'.repeat(64),
        },
      },
    ],
    fallback_order: ['omp_browser', 'playwright_cli', 'computer'],
    reobserve_after: true,
  };

  const fakeExecutablePath = path.resolve(process.execPath);

  await assert.rejects(
    async () => executeCmuxPlan({
      cmuxPath: fakeExecutablePath,
      surface: 'main-surface',
      plan: uploadPlan,
      execFileFn: fakeExecFile,
    }),
    (err) => err instanceof CmuxPlanExecutorError && err.code === 'E_UNSUPPORTED_ACTION',
  );

  assert.equal(callCount, 0);
});

test('loadPlanSecure requires owner-only (mode 0o600) plan file permissions', async () => {
  const tmpDir = await fsp.mkdtemp(path.join(os.tmpdir(), 'cmux-test-'));
  const planPath = path.join(tmpDir, 'plan-public.json');

  try {
    const plan = createValidPlan();
    await fsp.writeFile(planPath, JSON.stringify(plan), { mode: 0o644 });

    await assert.rejects(
      async () => loadPlanSecure(planPath),
      (err) => err instanceof CmuxPlanExecutorError && err.code === 'E_UNSAFE_PLAN_PATH',
    );
  } finally {
    await fsp.rm(tmpDir, { recursive: true, force: true });
  }
});
