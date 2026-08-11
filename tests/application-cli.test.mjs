import assert from 'node:assert/strict';
import fs from 'node:fs';
import crypto from 'node:crypto';
import os from 'node:os';
import path from 'node:path';
import test from 'node:test';

import {
  ApplicationCliError,
  MAX_CLI_JSON_BYTES,
  parseCliArgs,
  runCli,
  secureReadJson,
  secureWriteJson,
  validateBrowserActionRequest,
} from '../src/phase1/application-cli.mjs';
import { ACTION_RESULT_SCHEMA } from '../src/phase1/action-plan.mjs';
import { resolveField, startRun } from '../src/phase1/session.mjs';


function privateFile(root, name, contents) {
  const filePath = path.join(root, name);
  fs.writeFileSync(filePath, contents, { mode: 0o600 });
  fs.chmodSync(filePath, 0o600);
  return filePath;
}

function fieldControl(fieldId, value = null, valuePresent = false, overrides = {}) {
  return {
    ref: `control-${fieldId}`,
    stable_id: fieldId,
    group_id: null,
    kind: 'input',
    tag: 'input',
    type: 'text',
    role: 'textbox',
    label: fieldId,
    name: fieldId,
    description: null,
    locator: { strategy: 'id', value: fieldId, role: 'textbox', name: fieldId },
    frame_id: 'frame-main',
    visible: true,
    enabled: true,
    required: true,
    readonly: false,
    disabled: false,
    value,
    value_present: valuePresent,
    checked: null,
    selected: null,
    options: [],
    validity: { valid: true, aria_invalid: null, message: null },
    file: null,
    candidate: { class: 'field', reason: 'visible user-facing field control' },
    ...overrides,
  };
}

function finalControl() {
  return {
    ...fieldControl('final'),
    ref: 'control-final',
    kind: 'button',
    tag: 'button',
    type: 'submit',
    role: 'button',
    label: 'Submit Application',
    name: null,
    locator: null,
    required: false,
    candidate: { class: 'final_candidate', reason: 'final submission control' },
  };
}

function observation(applicationUrl, sequence, controls, url = applicationUrl) {
  return {
    schema: 'phase1-observation-v1',
    observation_id: `obs-${sequence}`,
    previous_observation_id: sequence === 1 ? null : `obs-${sequence - 1}`,
    observed_at: `2026-07-25T08:00:${String(sequence).padStart(2, '0')}.000Z`,
    url,
    title: 'Synthetic application',
    snapshot_sha256: 'a'.repeat(64),
    frames: [{
      id: 'frame-main',
      parent_id: null,
      url,
      origin: 'https://example.invalid',
      accessible: true,
    }],
    controls,
    blockers: [],
  };
}

function createHarness(t, answers = {}) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'phase1-cli-test-'));
  fs.chmodSync(root, 0o700);
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const jobPath = privateFile(root, 'job.txt', 'Synthetic job description.');
  const profilePath = privateFile(root, 'profile.json', JSON.stringify({
    schema: 'phase1-profile-v2',
    verified_facts: {
      answers,
    },
  }));
  const uploadPath = privateFile(root, 'resume.pdf', '%PDF-1.7\nsynthetic');
  const memoryDir = path.join(root, 'memory');
  fs.mkdirSync(memoryDir, { mode: 0o700 });
  const memoryPath = path.join(memoryDir, 'answers.jsonl');
  const run = {
    schema: 'phase1-run-v1',
    application_url: 'https://example.invalid/jobs/cli',
    job_description_path: jobPath,
    applicant_profile_path: profilePath,
    source_resume_path: uploadPath,
    resume_upload_path: uploadPath,
    answer_memory_path: memoryPath,
    run_artifact_dir: path.join(root, 'artifacts'),
    browser_mode: 'headed',
    observer: 'playwright_dom_v1',
    action_driver: 'omp_browser',
    submit_policy: 'omp_agent',
  };
  const runPath = privateFile(root, 'run.json', JSON.stringify(run));
  return { root, run, runPath, uploadPath };
}

function decision(observationId, fieldId, proposedAnswer, proposedAction = 'fill_text') {
  return {
    observationId,
    fieldId,
    controlReference: `control-${fieldId}`,
    fieldPolicy: 'qualification',
    proposedAnswer,
    answerSource: 'profile_verified',
    evidenceReferences: [`profile_verified:${fieldId}`],
    inferenceRationaleDigest: null,
    inferenceEvidenceDigests: null,
    proposedAction,
    expectedRetainedState: proposedAnswer,
    modelTier: 'standard',
    confidence: 1,
    reasonCode: 'profile_exact',
    reobservationRequired: true,
    automaticSubmissionEligible: false,
  };
}

function answerResolution(fieldId, alias = fieldId) {
  return {
    kind: 'answer',
    field_id: fieldId,
    alias,
    sensitive: null,
    remember: false,
    approved_at: null,
    deliberate_blank: false,
    semantic_choice: null,
    formatted_value: null,
    user: null,
  };
}

function request(item, overrides = {}) {
  return {
    schema: 'phase1-browser-action-request-v1',
    driver: 'omp_browser',
    screenshot_sha256: null,
    retry_of: null,
    created_at: '2026-07-25T08:00:10.000Z',
    ats: 'greenhouse',
    resume: null,
    agent_inference: null,
    greenhouse_board_token: null,
    items: [item],
    ...overrides,
  };
}

async function writePrivateJson(root, name, value) {
  const filePath = path.join(root, name);
  await secureWriteJson(filePath, value);
  return filePath;
}

test('strict CLI parser rejects malformed, duplicate, and incomplete flags', () => {
  assert.deepEqual(
    parseCliArgs(['accept-observation', '--run', 'run.json', '--observation', 'obs.json']),
    {
      command: 'accept-observation',
      args: { run: 'run.json', observation: 'obs.json' },
    },
  );
  assert.deepEqual(
    parseCliArgs(['resolve-blank', '--run', 'run.json', '--request', 'request.json']),
    {
      command: 'resolve-blank',
      args: { run: 'run.json', request: 'request.json' },
    },
  );
  for (const argv of [
    [],
    ['unknown'],
    ['accept-observation', '--run', 'run.json'],
    ['accept-observation', '--run', 'a', '--run', 'b', '--observation', 'obs'],
    ['accept-observation', 'run', 'a', '--observation', 'obs'],
    ['accept-observation', '--run', '--observation', 'obs'],
  ]) {
    assert.throws(
      () => parseCliArgs(argv),
      (error) => error instanceof ApplicationCliError,
    );
  }
});

test('resolve-blank records an observed optional blank without a browser action', async (t) => {
  const { root, run, runPath } = createHarness(t);
  await runCli([
    'accept-observation',
    '--run', runPath,
    '--observation', await writePrivateJson(root, 'blank-observation.json', observation(run.application_url, 1, [
      fieldControl('optional2', null, false, { required: false }),
      fieldControl('optional', null, false, { required: false }),
      finalControl(),
    ])),
  ]);
  const resumeSha256 = crypto.createHash('sha256')
    .update(fs.readFileSync(run.resume_upload_path))
    .digest('hex');
  const jobDescriptionSha256 = crypto.createHash('sha256')
    .update(fs.readFileSync(run.job_description_path))
    .digest('hex');
  const rationale = 'Optional identity field is deliberately left blank.';
  const rationaleDigest = crypto.createHash('sha256').update(rationale).digest('hex');
  const blankDecision = {
    ...decision('obs-1', 'optional', null, 'clear'),
    fieldPolicy: 'identity',
    answerSource: 'agent_inference',
    evidenceReferences: [`resume:${resumeSha256}`, `job_description:${jobDescriptionSha256}`],
    inferenceRationaleDigest: rationaleDigest,
    inferenceEvidenceDigests: {
      resumeSha256,
      jobDescriptionSha256,
    },
    expectedRetainedState: null,
    reasonCode: 'agent_inferred_optional_blank',
  };
  const blankResolution = {
    ...answerResolution('optional'),
    alias: 'Preferred Name',
    sensitive: true,
    deliberate_blank: true,
    semantic_choice: 'blank',
  };
  const secondBlankDecision = {
    ...blankDecision,
    fieldId: 'optional2',
    controlReference: 'control-optional2',
    fieldPolicy: 'qualification',
  };
  const secondBlankResolution = {
    ...blankResolution,
    field_id: 'optional2',
    alias: 'Cover Letter (Optional)',
    sensitive: false,
  };
  const requestPath = await writePrivateJson(root, 'blank-request.json', request({
    decision: blankDecision,
    resolution: blankResolution,
    option: null,
  }, {
    items: [
      {
        decision: blankDecision,
        resolution: blankResolution,
        option: null,
      },
      {
        decision: secondBlankDecision,
        resolution: secondBlankResolution,
        option: null,
      },
    ],
    resume: {
      source_sha256: resumeSha256,
      answers: {},
    },
    agent_inference: {
      source_resume_sha256: resumeSha256,
      job_description_sha256: jobDescriptionSha256,
      answers: {
        'Preferred Name': {
          value: null,
          rationale,
          evidence: {
            resume_sha256: resumeSha256,
            job_description_sha256: jobDescriptionSha256,
          },
        },
        'Cover Letter (Optional)': {
          value: null,
          rationale,
          evidence: {
            resume_sha256: resumeSha256,
            job_description_sha256: jobDescriptionSha256,
          },
        },
      },
    },
  }));

  const result = await runCli(['resolve-blank', '--run', runPath, '--request', requestPath]);
  assert.deepEqual(result, {
    status: 'ok',
    command: 'resolve-blank',
    resolved_count: 2,
    retention_ok: true,
    retry_required: false,
  });
  const prepared = await runCli([
    'prepare-submit', '--run', runPath, '--final-ref', 'control-final',
  ]);
  assert.equal(prepared.authorized, true);
});

test('secure CLI JSON I/O rejects unsafe inputs and never overwrites output', async (t) => {
  const { root } = createHarness(t);
  const output = path.join(root, 'output.json');
  await secureWriteJson(output, { private: true });
  assert.equal(fs.statSync(output).mode & 0o777, 0o600);
  assert.deepEqual(await secureReadJson(output), { private: true });
  await assert.rejects(secureWriteJson(output, { private: false }), /E_CLI_OUTPUT_EXISTS/);

  const unsafe = privateFile(root, 'unsafe.json', '{}');
  fs.chmodSync(unsafe, 0o644);
  await assert.rejects(secureReadJson(unsafe), (error) => error.code === 'E_PATH_PERMISSIONS');
  fs.chmodSync(unsafe, 0o600);
  const link = path.join(root, 'link.json');
  fs.symlinkSync(unsafe, link);
  await assert.rejects(secureReadJson(link), (error) => error.code === 'E_PATH_SYMLINK');
  const oversized = privateFile(root, 'oversized.json', ' '.repeat(MAX_CLI_JSON_BYTES + 1));
  await assert.rejects(secureReadJson(oversized), (error) => error.code === 'E_JSON_OVERSIZE');
});

test('request validation enforces exact resolution and option contracts', () => {
  const valid = request({
    resolution: answerResolution('name'),
    decision: decision('obs-1', 'name', 'Ada'),
    option: null,
  });
  assert.deepEqual(validateBrowserActionRequest(valid), valid);
  assert.throws(
    () => validateBrowserActionRequest({ ...valid, unknown: true }),
    /invalid keys/,
  );
  assert.throws(
    () => validateBrowserActionRequest({
      ...valid,
      items: [{ ...valid.items[0], option: {
        kind: 'observed_exact',
        option_text: 'Ada',
        option_value: 'ada',
      } }],
    }),
    /option/,
  );
  assert.throws(
    () => validateBrowserActionRequest({
      ...valid,
      items: [
        valid.items[0],
        {
          resolution: answerResolution('country'),
          decision: decision('obs-1', 'country', 'us', 'select_option'),
          option: { kind: 'observed_exact', option_text: 'United States', option_value: 'us' },
        },
      ],
    }),
    /items/,
  );
});

test('CLI persists plan, receipt, authorization, submit, and final URL across process-style resumes', async (t) => {
  const { root, run, runPath } = createHarness(t, { name: 'Ada Lovelace' });
  const firstObservation = observation(run.application_url, 1, [
    fieldControl('name'),
    finalControl(),
  ]);
  const firstPath = await writePrivateJson(root, 'observation-1-input.json', firstObservation);
  const accepted = await runCli([
    'accept-observation', '--run', runPath, '--observation', firstPath,
  ], { now: '2026-07-25T08:00:00.000Z' });
  assert.deepEqual(accepted, {
    status: 'ok',
    command: 'accept-observation',
    observation_id: 'obs-1',
    field_count: 1,
  });

  const requestPath = await writePrivateJson(root, 'request.json', request({
    resolution: answerResolution('name'),
    decision: decision('obs-1', 'name', 'Ada Lovelace'),
    option: null,
  }));
  const planPath = path.join(root, 'plan.json');
  const planned = await runCli([
    'plan', '--run', runPath, '--request', requestPath, '--output', planPath,
  ]);
  assert.equal(planned.action_count, 1);
  const plan = await secureReadJson(planPath);
  assert.equal(plan.actions[0].steps[0].helper, 'fill');
  assert.equal(plan.actions[0].steps[0].value, 'Ada Lovelace');

  const pendingPath = path.join(root, 'pending.json');
  const pending = await runCli([
    'pending-plan', '--run', runPath, '--output', pendingPath,
  ]);
  assert.equal(pending.count, 1);
  assert.deepEqual((await secureReadJson(pendingPath))[0], plan);

  const secondObservation = observation(run.application_url, 2, [
    fieldControl('name', 'Ada Lovelace', true),
    finalControl(),
  ]);
  const resultPath = await writePrivateJson(root, 'result.json', {
    schema: ACTION_RESULT_SCHEMA,
    plan_id: plan.plan_id,
    post_observation_id: 'obs-2',
    outcomes: [{
      action_id: plan.actions[0].action_id,
      outcome: 'succeeded',
      error_code: null,
      driver: 'omp_browser',
      selected_option_text: null,
    }],
  });
  const secondPath = await writePrivateJson(root, 'observation-2-input.json', secondObservation);
  const completed = await runCli([
    'complete-action',
    '--run', runPath,
    '--plan', planPath,
    '--result', resultPath,
    '--observation', secondPath,
  ]);
  assert.equal(completed.retention_ok, true);
  assert.equal(completed.status, 'ok');
  assert.equal(completed.aggregate_retention_ok, true);
  assert.equal(completed.retry_required, false);

  const prepared = await runCli([
    'prepare-submit', '--run', runPath, '--final-ref', 'control-final',
  ]);
  assert.equal(prepared.authorized, true);
  const beginPath = path.join(root, 'begin.json');
  const begun = await runCli([
    'begin-submit', '--run', runPath, '--output', beginPath,
  ]);
  const begin = await secureReadJson(beginPath);
  assert.equal(begin.attempt_id, begun.attempt_id);
  assert.equal(begin.ref, 'control-final');

  const submitted = await runCli([
    'complete-submit',
    '--run', runPath,
    '--attempt-id', begun.attempt_id,
    '--outcome', 'succeeded',
  ]);
  assert.equal(submitted.outcome, 'succeeded');

  const finalUrl = 'https://example.invalid/confirmation';
  const postObservation = observation(run.application_url, 3, [], finalUrl);
  const postPath = await writePrivateJson(root, 'observation-3-input.json', postObservation);
  await runCli([
    'accept-observation', '--run', runPath, '--observation', postPath,
  ]);
  const screenshotPath = privateFile(root, 'final.png', 'private screenshot');
  const finalized = await runCli([
    'finalize',
    '--run', runPath,
    '--screenshot', screenshotPath,
    '--final-url', finalUrl,
  ]);
  assert.equal(finalized.finalized, true);
  const completion = JSON.parse(fs.readFileSync(path.join(run.run_artifact_dir, 'completion.json'), 'utf8'));
  assert.equal(completion.final_url, finalUrl);
  assert.equal(completion.submit_action_count, 1);
});
test('complete-action scopes retention while prepare-submit keeps aggregate gate', async (t) => {
  const { root, run, runPath } = createHarness(t, { name: 'Ada', other: 'Expected' });
  const firstObservation = observation(run.application_url, 1, [
    fieldControl('name'),
    fieldControl('other'),
    finalControl(),
  ]);
  const firstPath = await writePrivateJson(root, 'scope-observation-1.json', firstObservation);
  await runCli(['accept-observation', '--run', runPath, '--observation', firstPath]);
  const resumed = await startRun(runPath, { resumeExisting: true });
  await resolveField(resumed, { field_id: 'other', alias: 'other' });
  const requestPath = await writePrivateJson(root, 'scope-request.json', request({
    resolution: answerResolution('name'),
    decision: decision('obs-1', 'name', 'Ada'),
    option: null,
  }));
  const planPath = path.join(root, 'scope-plan.json');
  await runCli(['plan', '--run', runPath, '--request', requestPath, '--output', planPath]);
  const plan = await secureReadJson(planPath);
  const resultPath = await writePrivateJson(root, 'scope-result.json', {
    schema: ACTION_RESULT_SCHEMA,
    plan_id: plan.plan_id,
    post_observation_id: 'obs-2',
    outcomes: [{
      action_id: plan.actions[0].action_id,
      outcome: 'succeeded',
      error_code: null,
      driver: 'omp_browser',
      selected_option_text: null,
    }],
  });
  const postPath = await writePrivateJson(root, 'scope-observation-2.json', observation(run.application_url, 2, [
    fieldControl('name', 'Ada', true),
    fieldControl('other', 'Wrong', true),
    finalControl(),
  ]));
  const completed = await runCli([
    'complete-action',
    '--run', runPath,
    '--plan', planPath,
    '--result', resultPath,
    '--observation', postPath,
  ]);
  assert.equal(completed.status, 'ok');
  assert.equal(completed.retention_ok, true);
  assert.equal(completed.aggregate_retention_ok, false);
  assert.equal(completed.retry_required, false);
  const prepared = await runCli(['prepare-submit', '--run', runPath, '--final-ref', 'control-final']);
  assert.equal(prepared.status, 'blocked');
  assert.equal(prepared.authorized, false);
});

test('complete-action reports failed attempted outcomes as blocked', async (t) => {
  const { root, run, runPath } = createHarness(t, { name: 'Ada' });
  const firstPath = await writePrivateJson(root, 'failed-observation-1.json', observation(
    run.application_url,
    1,
    [fieldControl('name'), finalControl()],
  ));
  await runCli(['accept-observation', '--run', runPath, '--observation', firstPath]);
  const requestPath = await writePrivateJson(root, 'failed-request.json', request({
    resolution: answerResolution('name'),
    decision: decision('obs-1', 'name', 'Ada'),
    option: null,
  }));
  const planPath = path.join(root, 'failed-plan.json');
  await runCli(['plan', '--run', runPath, '--request', requestPath, '--output', planPath]);
  const plan = await secureReadJson(planPath);
  const resultPath = await writePrivateJson(root, 'failed-result.json', {
    schema: ACTION_RESULT_SCHEMA,
    plan_id: plan.plan_id,
    post_observation_id: 'obs-2',
    outcomes: [{
      action_id: plan.actions[0].action_id,
      outcome: 'failed',
      error_code: 'synthetic_failure',
      driver: 'omp_browser',
      selected_option_text: null,
    }],
  });
  const postPath = await writePrivateJson(root, 'failed-observation-2.json', observation(
    run.application_url,
    2,
    [fieldControl('name'), finalControl()],
  ));
  const completed = await runCli([
    'complete-action',
    '--run', runPath,
    '--plan', planPath,
    '--result', resultPath,
    '--observation', postPath,
  ]);
  assert.equal(completed.status, 'blocked');
  assert.equal(completed.retention_ok, false);
  assert.equal(completed.retry_required, true);
});


test('CLI resolves Greenhouse education options through the bounded catalog', async (t) => {
  const school = 'Massachusetts Institute of Technology';
  const { root, run, runPath } = createHarness(t, { school });
  const select = fieldControl('school', null, false, {
    kind: 'input',
    tag: 'input',
    type: 'text',
    role: 'combobox',
    options: [],
  });
  const observationPath = await writePrivateJson(
    root,
    'catalog-observation.json',
    observation(run.application_url, 1, [select, finalControl()]),
  );
  await runCli(['accept-observation', '--run', runPath, '--observation', observationPath]);
  const requestPath = await writePrivateJson(root, 'catalog-request.json', request({
    resolution: { ...answerResolution('school'), formatted_value: school },
    decision: decision('obs-1', 'school', school, 'select_option'),
    option: { kind: 'greenhouse_education', category: 'schools' },
  }, { greenhouse_board_token: 'acme' }));
  const outputPath = path.join(root, 'catalog-plan.json');
  const fetchImpl = async () => ({
    ok: true,
    status: 200,
    headers: { get: (name) => name.toLowerCase() === 'content-type' ? 'application/json' : null },
    text: async () => JSON.stringify({
      items: [{ id: 7, text: school }],
      meta: { total_count: 1, per_page: 1 },
    }),
  });
  await runCli([
    'plan', '--run', runPath, '--request', requestPath, '--output', outputPath,
  ], { fetchImpl });
  const plan = await secureReadJson(outputPath);
  assert.equal(plan.actions[0].steps.length, 3);
  assert.equal(plan.actions[0].steps[0].helper, 'click');
  assert.equal(plan.actions[0].steps[1].helper, 'fill');
  assert.equal(plan.actions[0].steps[1].value, school);
  assert.equal(plan.actions[0].steps[1].wait_after.state, 'exact_option_visible');
  assert.equal(plan.actions[0].steps[2].option_text, school);
  assert.equal(plan.actions[0].steps[2].option_value, school);
  assert.equal(plan.actions[0].steps[2].helper, 'click_exact_option');
  assert.equal(plan.actions[0].steps[2].wait_after.state, 'custom_select_committed_menu_closed');
  assert.equal(plan.actions[0].steps[2].reobserve_after.action, 'reobserve');
});
