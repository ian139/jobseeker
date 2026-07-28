import assert from 'node:assert/strict';
import crypto from 'node:crypto';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import test from 'node:test';

import {
  acceptObservation,
  beginFinalSubmit,
  completeFinalSubmit,
  finalizeRun,
  prepareSubmission,
  recordAction,
  recordActionBatch,
  resolveField,
  startRun,
  verifyRetention,
} from '../src/phase1/session.mjs';
import { digestPrivateValue } from '../src/phase1/ledger.mjs';

function privateFile(root, name, contents) {
  const filePath = path.join(root, name);
  fs.writeFileSync(filePath, contents, { mode: 0o600 });
  fs.chmodSync(filePath, 0o600);
  return filePath;
}

function fieldControl(fieldId, value = null, valuePresent = false) {
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
  };
}

function finalControl() {
  return {
    ref: 'control-final',
    stable_id: 'final',
    group_id: null,
    kind: 'button',
    tag: 'button',
    type: 'submit',
    role: 'button',
    label: 'Submit Application',
    name: null,
    description: null,
    locator: null,
    frame_id: 'frame-main',
    visible: true,
    enabled: true,
    required: false,
    readonly: false,
    disabled: false,
    value: null,
    value_present: false,
    checked: null,
    selected: null,
    options: [],
    validity: { valid: true, aria_invalid: null, message: null },
    file: null,
    candidate: { class: 'final_candidate', reason: 'final submission control' },
  };
}

function observation(applicationUrl, sequence, fields) {
  const id = `obs-${sequence}`;
  return {
    schema: 'phase1-observation-v1',
    observation_id: id,
    previous_observation_id: sequence === 1 ? null : `obs-${sequence - 1}`,
    observed_at: `2026-07-25T08:00:${String(sequence).padStart(2, '0')}.000Z`,
    url: applicationUrl,
    title: 'Synthetic application',
    snapshot_sha256: 'a'.repeat(64),
    frames: [{
      id: 'frame-main',
      parent_id: null,
      url: applicationUrl,
      origin: 'https://example.invalid',
      accessible: true,
    }],
    controls: [...fields, finalControl()],
    blockers: [],
  };
}

function createHarness(t, { answers = {}, profile = null, sourceResume = null } = {}) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'phase1-session-test-'));
  fs.chmodSync(root, 0o700);
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));

  const profileInput = profile ?? {
    schema: 'phase1-profile-v1',
    answers,
  };
  const jobDescription = 'Synthetic job description.';
  const jobPath = privateFile(root, 'job.txt', jobDescription);
  const jobSha256 = crypto.createHash('sha256').update(jobDescription).digest('hex');
  const profilePath = privateFile(root, 'profile.json', JSON.stringify(profileInput));
  const uploadPath = privateFile(root, 'resume.pdf', '%PDF-1.7\nsynthetic upload');
  const screenshotPath = privateFile(root, 'final.png', 'synthetic screenshot');
  const memoryDirectory = path.join(root, 'memory');
  fs.mkdirSync(memoryDirectory, { mode: 0o700 });
  fs.chmodSync(memoryDirectory, 0o700);
  const memoryPath = path.join(memoryDirectory, 'answers.jsonl');
  const sourceResumePath = sourceResume === null
    ? null
    : privateFile(root, 'source-resume.txt', sourceResume);
  const sourceSha256 = sourceResume === null
    ? null
    : crypto.createHash('sha256').update(sourceResume).digest('hex');

  function runFor(name) {
    const run = {
      schema: 'phase1-run-v1',
      application_url: `https://example.invalid/jobs/${name}`,
      job_description_path: jobPath,
      applicant_profile_path: profilePath,
      resume_upload_path: uploadPath,
      answer_memory_path: memoryPath,
      run_artifact_dir: path.join(root, `${name}-artifacts`),
      browser_mode: 'headed',
      observer: 'playwright_dom_v1',
      action_driver: 'omp_browser',
      submit_policy: 'omp_agent',
    };
    if (sourceResumePath !== null) run.source_resume_path = sourceResumePath;
    const runPath = privateFile(root, `${name}-run.json`, JSON.stringify(run));
    return { run, runPath };
  }

  return {
    memoryPath,
    runFor,
    screenshotPath,
    sourceResumePath,
    sourceResume,
    jobDescription,
    sourceSha256,
    jobSha256,
  };
}

function makeAgentInference({ sourceSha256, jobSha256, answers }) {
  const wrapped = {};
  for (const [alias, { value, rationale }] of Object.entries(answers)) {
    wrapped[alias] = {
      value,
      rationale,
      evidence: {
        resume_sha256: sourceSha256,
        job_description_sha256: jobSha256,
      },
    };
  }
  return {
    source_resume_sha256: sourceSha256,
    job_description_sha256: jobSha256,
    answers: wrapped,
  };
}

async function finalizeRetained(session, screenshotPath) {
  const retention = await verifyRetention(session);
  assert.equal(retention.ok, true);
  const prepared = await prepareSubmission(session, { finalRef: 'control-final' });
  assert.equal(prepared.authorized, true);
  const begun = await beginFinalSubmit(session);
  assert.equal(begun.ref, prepared.authorizedFinalRef);
  await completeFinalSubmit(session, {
    attemptId: begun.attemptId,
    outcome: 'succeeded',
  });
  const final = await finalizeRun(session, { screenshotPath });
  assert.equal(final.finalized, true);
  assert.equal(final.submitActionCount, 1);
  assert.equal(session.finalized, true);
}

test('coordinator uses the sensitive structured country resolution contract', async (t) => {
  const harness = createHarness(t, {
    profile: {
      schema: 'phase1-profile-v1',
      address: { country: 'Structured Country' },
      answers: { 'profile.address.country': 'Conflicting Alias Country' },
    },
  });
  const { run, runPath } = harness.runFor('structured-country');
  const session = await startRun(runPath, { startedAt: '2026-07-25T08:00:00.000Z' });

  await acceptObservation(session, observation(run.application_url, 1, [fieldControl('country')]));
  const resolution = await resolveField(session, {
    field_id: 'country',
    alias: 'profile.address.country',
    sensitive: true,
  });
  assert.equal(resolution.answer.source, 'profile');
  assert.equal(resolution.answer.value, 'Structured Country');
  assert.equal(
    resolution.ledger.fields.find((field) => field.field_id === 'country').sensitive,
    true,
  );
  await recordAction(session, { action: 'fill', field_id: 'country', outcome: 'succeeded' });
  await acceptObservation(
    session,
    observation(run.application_url, 2, [
      fieldControl('country', 'Structured Country', true),
    ]),
  );
  await finalizeRetained(session, harness.screenshotPath);
});

test('sensitive country excludes both resume and agent inference', async (t) => {
  const sourceResume = 'Synthetic country source resume.';
  const harness = createHarness(t, { sourceResume });
  const { run, runPath } = harness.runFor('derived-country');
  const session = await startRun(runPath, {
    startedAt: '2026-07-25T08:00:00.000Z',
    resume: {
      source_sha256: harness.sourceSha256,
      answers: { 'profile.address.country': 'Resume Country' },
    },
    agentInference: makeAgentInference({
      sourceSha256: harness.sourceSha256,
      jobSha256: harness.jobSha256,
      answers: {
        'profile.address.country': {
          value: 'Inferred Country',
          rationale: 'Inferred from resume and job description.',
        },
      },
    }),
  });

  await acceptObservation(session, observation(run.application_url, 1, [fieldControl('country')]));
  const missing = await resolveField(session, {
    field_id: 'country',
    alias: 'profile.address.country',
    sensitive: true,
  });
  assert.equal(missing.missing, true);
  const unresolved = missing.ledger.fields.find((field) => field.field_id === 'country');
  assert.equal(unresolved.sensitive, true);
  assert.equal(unresolved.answer_state, 'unresolved');
  assert.equal(unresolved.answer_source, null);
  assert.equal(unresolved.value_digest, null);
  assert.equal(unresolved.inference_rationale_digest, null);
  assert.equal(unresolved.inference_evidence_digests, null);

  const explicit = await resolveField(session, {
    field_id: 'country',
    alias: 'profile.address.country',
    sensitive: true,
    user: { answers: { 'profile.address.country': 'Explicit Country' } },
  });
  assert.equal(explicit.answer.source, 'user');
  await recordAction(session, { action: 'fill', field_id: 'country', outcome: 'succeeded' });
  await acceptObservation(
    session,
    observation(run.application_url, 2, [
      fieldControl('country', 'Explicit Country', true),
    ]),
  );
  await finalizeRetained(session, harness.screenshotPath);
});

test('agent inference rejects per-answer evidence mismatched to envelope hashes', async (t) => {
  const sourceResume = 'Synthetic source resume.';
  const harness = createHarness(t, { sourceResume });
  const { runPath } = harness.runFor('mismatched-evidence');
  const lastChar = harness.jobSha256.at(-1);
  const tamperedJobSha256 = harness.jobSha256.slice(0, -1) + (lastChar === '0' ? '1' : '0');
  const agentInference = {
    source_resume_sha256: harness.sourceSha256,
    job_description_sha256: harness.jobSha256,
    answers: {
      field: {
        value: 'Derived answer',
        rationale: 'Derived from resume and job description.',
        evidence: {
          resume_sha256: harness.sourceSha256,
          job_description_sha256: tamperedJobSha256,
        },
      },
    },
  };

  await assert.rejects(
    startRun(runPath, {
      startedAt: '2026-07-25T08:00:00.000Z',
      agentInference,
    }),
    /evidence must match the configured inputs/,
  );
});

test('free-form state wording remains eligible for agent inference', async (t) => {
  const sourceResume = 'Synthetic source resume.';
  const harness = createHarness(t, { sourceResume });
  const { run, runPath } = harness.runFor('state-why');
  const session = await startRun(runPath, {
    startedAt: '2026-07-25T08:00:00.000Z',
    agentInference: makeAgentInference({
      sourceSha256: harness.sourceSha256,
      jobSha256: harness.jobSha256,
      answers: {
        motivation: {
          value: 'The role aligns with supported engineering experience.',
          rationale: 'Resume experience is relevant to the job requirements.',
        },
      },
    }),
  });
  await acceptObservation(session, observation(run.application_url, 1, [{
    ...fieldControl('motivation'),
    label: 'State why you are interested in this role',
  }]));

  const resolved = await resolveField(session, {
    field_id: 'motivation',
    alias: 'motivation',
  });
  assert.equal(resolved.answer.source, 'agent_inference');
  assert.equal(resolved.ledger.fields[0].sensitive, false);
});

test('coordinator enforces observe-act-reobserve and final submission authorization gate', async (t) => {
  const harness = createHarness(t, { answers: { field: 'Profile answer' } });
  const { run, runPath } = harness.runFor('lifecycle');
  const session = await startRun(runPath, { startedAt: '2026-07-25T08:00:00.000Z' });

  await acceptObservation(session, observation(run.application_url, 1, [fieldControl('field')]));
  const resolved = await resolveField(session, { field_id: 'field', alias: 'field' });
  assert.equal(resolved.answer.source, 'profile');
  await assert.rejects(
    recordAction(session, { action: 'final_submit', outcome: 'attempted' }),
    /must use beginFinalSubmit/,
  );

  await recordAction(session, {
    action: 'fill',
    field_id: 'field',
    outcome: 'failed',
    error_code: 'synthetic-failure',
  });
  await assert.rejects(
    recordAction(session, { action: 'fill', field_id: 'field', outcome: 'succeeded' }),
    /consumed|fresh observation/,
  );
  await assert.rejects(
    resolveField(session, { field_id: 'field', alias: 'field' }),
    /consumed|fresh observation/,
  );
  const pending = await verifyRetention(session);
  assert.equal(pending.ok, false);
  assert.ok(pending.errors.some((error) => error.code === 'MUTATION_PENDING'));

  await acceptObservation(session, observation(run.application_url, 2, [fieldControl('field')]));
  await resolveField(session, { field_id: 'field', alias: 'field' });
  await recordAction(session, { action: 'fill', field_id: 'field', outcome: 'succeeded' });
  await acceptObservation(
    session,
    observation(run.application_url, 3, [fieldControl('field', 'Profile answer', true)]),
  );
  await finalizeRetained(session, harness.screenshotPath);
});

test('recordActionBatch publishes routine fills atomically and requires re-observation', async (t) => {
  const harness = createHarness(t);
  const { run, runPath } = harness.runFor('batch-actions');
  const session = await startRun(runPath, { startedAt: '2026-07-25T08:00:00.000Z' });
  await acceptObservation(session, observation(run.application_url, 1, [
    fieldControl('alpha'),
    fieldControl('beta'),
  ]));
  const actionArtifacts = () => fs.readdirSync(run.run_artifact_dir)
    .filter((name) => /^action-\d+\.json$/.test(name));

  const before = session.ledger;
  await assert.rejects(
    recordActionBatch(session, [
      { action: 'fill', field_id: 'alpha', outcome: 'succeeded' },
      { action: 'select', field_id: 'beta', outcome: 'succeeded' },
    ]),
    /only routine fill/i,
  );
  assert.strictEqual(session.ledger, before);
  assert.equal(actionArtifacts().length, 0);

  const successful = await recordActionBatch(session, [
    { action: 'fill', field_id: 'alpha', outcome: 'succeeded' },
    { action: 'fill', field_id: 'beta', outcome: 'succeeded' },
  ]);
  assert.deepEqual(successful.actions.map((action) => action.action_id), ['action-1', 'action-2']);
  assert.equal(successful.actionRefs.length, 2);
  assert.equal(actionArtifacts().length, 2);
  await assert.rejects(
    recordActionBatch(session, [
      { action: 'fill', field_id: 'alpha', outcome: 'succeeded' },
      { action: 'fill', field_id: 'beta', outcome: 'succeeded' },
    ]),
    /consumed|reobserve|fresh observation/i,
  );

  await acceptObservation(session, observation(run.application_url, 2, [
    fieldControl('alpha'),
    fieldControl('beta'),
  ]));
  const failed = await recordActionBatch(session, [
    { action: 'fill', field_id: 'alpha', outcome: 'succeeded' },
    { action: 'fill', field_id: 'beta', outcome: 'failed', error_code: 'synthetic-failure' },
  ]);
  assert.deepEqual(failed.actions.map((action) => action.outcome), ['succeeded', 'failed']);
  assert.equal(failed.retryRefs[0], null);
  assert.ok(failed.retryRefs[1]);
  assert.equal(actionArtifacts().length, 4);
  await assert.rejects(
    recordActionBatch(session, [
      { action: 'fill', field_id: 'alpha', outcome: 'succeeded' },
      { action: 'fill', field_id: 'beta', outcome: 'succeeded' },
    ]),
    /consumed|reobserve|fresh observation/i,
  );
  await acceptObservation(session, observation(run.application_url, 3, [
    fieldControl('alpha'),
    fieldControl('beta'),
  ]));
});


test('prepareSubmission authorizes final_submit', async (t) => {
  const harness = createHarness(t, { answers: { field: 'Profile answer' } });
  const { run, runPath } = harness.runFor('final-submit');
  const session = await startRun(runPath, { startedAt: '2026-07-25T08:00:00.000Z' });

  await acceptObservation(session, observation(run.application_url, 1, [fieldControl('field')]));
  const resolved = await resolveField(session, { field_id: 'field', alias: 'field' });
  assert.equal(resolved.answer.source, 'profile');
  await recordAction(session, { action: 'fill', field_id: 'field', outcome: 'succeeded' });
  await acceptObservation(
    session,
    observation(run.application_url, 2, [fieldControl('field', 'Profile answer', true)]),
  );
  const retention = await verifyRetention(session);
  assert.equal(retention.ok, true);

  const prepared = await prepareSubmission(session, { finalRef: 'control-final' });
  assert.equal(prepared.authorized, true);
  const rejectedPreparation = await prepareSubmission(session, { finalRef: 'control-not-final' });
  assert.equal(rejectedPreparation.authorized, false);
  await assert.rejects(
    recordAction(session, {
      action: 'final_submit',
      outcome: 'succeeded',
      ref: prepared.authorizedFinalRef,
    }),
    /must use beginFinalSubmit/,
  );
  const reprepared = await prepareSubmission(session, { finalRef: 'control-final' });
  assert.equal(reprepared.authorized, true);

  const begun = await beginFinalSubmit(session);
  assert.equal(begun.action.action, 'final_submit');
  assert.equal(begun.ledger.submit_action_count, 1);
  await completeFinalSubmit(session, {
    attemptId: begun.attemptId,
    outcome: 'succeeded',
  });

  const final = await finalizeRun(session, { screenshotPath: harness.screenshotPath });
  assert.equal(final.finalized, true);
  assert.equal(final.submitActionCount, 1);
  assert.equal(session.finalized, true);
});

test('submission authorization is observation-bound and failed submits require fresh reauthorization', async (t) => {
  const harness = createHarness(t, { answers: { field: 'Profile answer' } });
  const { run, runPath } = harness.runFor('final-submit-retry');
  const session = await startRun(runPath, { startedAt: '2026-07-25T08:00:00.000Z' });

  await acceptObservation(session, observation(run.application_url, 1, [fieldControl('field')]));
  await resolveField(session, { field_id: 'field', alias: 'field' });
  await recordAction(session, { action: 'fill', field_id: 'field', outcome: 'succeeded' });
  await acceptObservation(
    session,
    observation(run.application_url, 2, [fieldControl('field', 'Profile answer', true)]),
  );
  const firstPreparation = await prepareSubmission(session, { finalRef: 'control-final' });
  assert.equal(firstPreparation.authorized, true);

  await acceptObservation(
    session,
    observation(run.application_url, 3, [fieldControl('field', 'Profile answer', true)]),
  );
  await assert.rejects(
    recordAction(session, {
      action: 'final_submit',
      outcome: 'succeeded',
      ref: 'control-final',
    }),
    /must use beginFinalSubmit/,
  );

  const retryPreparation = await prepareSubmission(session, { finalRef: 'control-final' });
  assert.equal(retryPreparation.authorized, true);
  const failedBegin = await beginFinalSubmit(session);
  await assert.rejects(
    finalizeRun(session, { screenshotPath: harness.screenshotPath }),
    /unresolved/,
  );
  await assert.rejects(
    prepareSubmission(session, { finalRef: 'control-final' }),
    /pending/,
  );
  const failedComplete = await completeFinalSubmit(session, {
    attemptId: failedBegin.attemptId,
    outcome: 'failed',
    errorCode: 'synthetic-submit-failure',
  });
  assert.equal(
    failedComplete.ledger.action_attempts.at(-1).outcome,
    'failed',
  );
  await assert.rejects(
    completeFinalSubmit(session, {
      attemptId: failedBegin.attemptId,
      outcome: 'failed',
    }),
    /already resolved|duplicate/,
  );
  await assert.rejects(
    completeFinalSubmit(session, {
      attemptId: 'unknown-attempt',
      outcome: 'failed',
    }),
    /unknown/,
  );
  await assert.rejects(
    prepareSubmission(session, { finalRef: 'control-final' }),
    /fresh observation/,
  );

  await acceptObservation(
    session,
    observation(run.application_url, 4, [fieldControl('field', 'Profile answer', true)]),
  );
  const finalPreparation = await prepareSubmission(session, { finalRef: 'control-final' });
  assert.equal(finalPreparation.authorized, true);
  const finalBegin = await beginFinalSubmit(session);
  await completeFinalSubmit(session, {
    attemptId: finalBegin.attemptId,
    outcome: 'succeeded',
  });
  const finalized = await finalizeRun(session, { screenshotPath: harness.screenshotPath });
  assert.equal(finalized.finalized, true);
  assert.equal(finalized.submitActionCount, 2);
});

test('sensitive reclassification invalidates agent inference before user resolution', async (t) => {
  const sourceResume = 'Synthetic source resume.';
  const harness = createHarness(t, { sourceResume });
  const { run, runPath } = harness.runFor('sensitive');
  const rationaleText = 'Derived from resume and job description.';
  const session = await startRun(runPath, {
    startedAt: '2026-07-25T08:00:00.000Z',
    agentInference: makeAgentInference({
      sourceSha256: harness.sourceSha256,
      jobSha256: harness.jobSha256,
      answers: {
        field: { value: 'Derived answer', rationale: rationaleText },
      },
    }),
  });

  await acceptObservation(session, observation(run.application_url, 1, [fieldControl('field')]));
  const derived = await resolveField(session, { field_id: 'field', alias: 'field' });
  assert.equal(derived.answer.source, 'agent_inference');
  assert.equal(
    derived.answer.inference_rationale_digest,
    crypto.createHash('sha256').update(rationaleText, 'utf8').digest('hex'),
  );
  assert.deepEqual(derived.answer.inference_evidence_digests, {
    resume_sha256: harness.sourceSha256,
    job_description_sha256: harness.jobSha256,
  });
  const resolvedField = derived.ledger.fields.find((field) => field.field_id === 'field');
  assert.equal(resolvedField.answer_source, 'agent_inference');
  assert.equal(resolvedField.value_digest, digestPrivateValue('Derived answer'));
  assert.equal(resolvedField.inference_rationale_digest, derived.answer.inference_rationale_digest);
  assert.deepEqual(resolvedField.inference_evidence_digests, derived.answer.inference_evidence_digests);
  assert.equal(Object.hasOwn(resolvedField, 'rationale'), false);

  await recordAction(session, { action: 'fill', field_id: 'field', outcome: 'succeeded' });
  await acceptObservation(
    session,
    observation(run.application_url, 2, [fieldControl('field', 'Derived answer', true)]),
  );
  assert.equal((await verifyRetention(session)).ok, true);

  const missing = await resolveField(session, {
    field_id: 'field',
    alias: 'field',
    sensitive: true,
  });
  const invalidated = missing.ledger.fields.find((field) => field.field_id === 'field');
  assert.equal(missing.missing, true);
  assert.equal(invalidated.sensitive, true);
  assert.equal(invalidated.answer_state, 'unresolved');
  assert.equal(invalidated.answer_source, null);
  assert.equal(invalidated.value_digest, null);
  assert.equal(invalidated.inference_rationale_digest, null);
  assert.equal(invalidated.inference_evidence_digests, null);

  const explicit = await resolveField(session, {
    field_id: 'field',
    alias: 'field',
    sensitive: true,
    user: { answers: { field: 'Explicit user answer' } },
  });
  assert.equal(explicit.answer.source, 'user');
  await recordAction(session, { action: 'fill', field_id: 'field', outcome: 'succeeded' });
  await acceptObservation(
    session,
    observation(run.application_url, 3, [fieldControl('field', 'Explicit user answer', true)]),
  );
  await finalizeRetained(session, harness.screenshotPath);
});

test('agent inference is excluded for sensitive categories but resolves non-sensitive role summary', async (t) => {
  const sourceResume = 'Synthetic source resume.';
  const harness = createHarness(t, { sourceResume });
  const { run, runPath } = harness.runFor('sensitive-categories');
  const session = await startRun(runPath, {
    startedAt: '2026-07-25T08:00:00.000Z',
    agentInference: makeAgentInference({
      sourceSha256: harness.sourceSha256,
      jobSha256: harness.jobSha256,
      answers: {
        first_name: { value: 'Alex', rationale: 'Name appears in resume.' },
        work_authorization: { value: 'Authorized', rationale: 'Resume states authorization.' },
        gender: { value: 'Non-binary', rationale: 'Inferred from pronouns.' },
        identity: { value: 'Applicant identity', rationale: 'Resume states identity.' },
        authorization: { value: 'Authorized', rationale: 'Resume states authorization.' },
        protected_class: { value: 'Protected class', rationale: 'Inferred demographic status.' },
        salary_expectation: { value: '100000', rationale: 'Inferred from resume.' },
        start_date: { value: '2024-01-01', rationale: 'Inferred from availability.' },
        credentials: { value: 'CPA', rationale: 'Inferred from certifications.' },
        role_summary: { value: 'Engineering lead', rationale: 'Inferred from experience.' },
        optional_reason: { value: null, rationale: 'The role requirements make this optional question not applicable.' },
      },
    }),
  });

  const controls = [
    fieldControl('first_name'),
    fieldControl('work_authorization'),
    fieldControl('gender'),
    fieldControl('identity'),
    fieldControl('authorization'),
    fieldControl('protected_class'),
    fieldControl('salary_expectation'),
    fieldControl('start_date'),
    fieldControl('credentials'),
    fieldControl('role_summary'),
    {
      ...fieldControl('optional_reason'),
      kind: 'select',
      tag: 'select',
      type: 'select',
      role: 'combobox',
      required: false,
      value: 'not_applicable',
      value_present: true,
      selected: ['not_applicable'],
      options: [{
        value: 'not_applicable',
        label: 'Not applicable',
        disabled: false,
        selected: true,
      }],
    },
  ];
  await acceptObservation(session, observation(run.application_url, 1, controls));

  const sensitiveAliases = [
    'first_name',
    'work_authorization',
    'gender',
    'identity',
    'authorization',
    'protected_class',
    'salary_expectation',
    'start_date',
    'credentials',
  ];
  for (const alias of sensitiveAliases) {
    const missing = await resolveField(session, {
      field_id: alias,
      alias,
    });
    assert.equal(missing.missing, true);
    const field = missing.ledger.fields.find((f) => f.field_id === alias);
    assert.equal(field.sensitive, true);
    assert.equal(field.answer_state, 'unresolved');
    assert.equal(field.answer_source, null);
    assert.equal(field.value_digest, null);
    assert.equal(field.inference_rationale_digest, null);
    assert.equal(field.inference_evidence_digests, null);
  }

  const resolved = await resolveField(session, {
    field_id: 'role_summary',
    alias: 'role_summary',
  });
  assert.equal(resolved.answer.source, 'agent_inference');
  assert.equal(resolved.answer.value, 'Engineering lead');
  assert.equal(
    resolved.answer.inference_rationale_digest,
    crypto.createHash('sha256').update('Inferred from experience.', 'utf8').digest('hex'),
  );
  assert.deepEqual(resolved.answer.inference_evidence_digests, {
    resume_sha256: harness.sourceSha256,
    job_description_sha256: harness.jobSha256,
  });
  const roleField = resolved.ledger.fields.find((f) => f.field_id === 'role_summary');
  assert.equal(roleField.answer_source, 'agent_inference');
  assert.equal(roleField.value_digest, digestPrivateValue('Engineering lead'));
  assert.equal(roleField.inference_rationale_digest, resolved.answer.inference_rationale_digest);
  assert.deepEqual(roleField.inference_evidence_digests, resolved.answer.inference_evidence_digests);
  assert.equal(Object.hasOwn(roleField, 'rationale'), false);
  const inferredBlank = await resolveField(session, {
    field_id: 'optional_reason',
    alias: 'optional_reason',
    deliberate_blank: true,
    semantic_choice: 'not_applicable',
  });
  assert.equal(inferredBlank.answer.source, 'agent_inference');
  const optionalField = inferredBlank.ledger.fields.find((f) => f.field_id === 'optional_reason');
  assert.equal(optionalField.answer_state, 'blank');
  assert.equal(optionalField.answer_source, 'agent_inference');
  assert.equal(optionalField.semantic_choice, 'not_applicable');
  assert.match(optionalField.inference_rationale_digest, /^[a-f0-9]{64}$/);

  await recordAction(session, { action: 'fill', field_id: 'role_summary', outcome: 'succeeded' });
  const retainedControls = controls.map((control) =>
    control.stable_id === 'role_summary'
      ? fieldControl('role_summary', 'Engineering lead', true)
      : control,
  );
  await acceptObservation(session, observation(run.application_url, 2, retainedControls));
  const retention = await verifyRetention(session);
  assert.equal(retention.ok, true, JSON.stringify(retention.errors));
  const retainedRole = retention.ledger.fields.find((f) => f.field_id === 'role_summary');
  assert.equal(retainedRole.answer_state, 'answered');
  assert.equal(retainedRole.answer_source, 'agent_inference');
  assert.equal(retainedRole.retained, true);
  for (const alias of sensitiveAliases) {
    const f = retention.ledger.fields.find((field) => field.field_id === alias);
    assert.equal(f.sensitive, true);
    assert.equal(f.answer_state, 'unresolved');
    assert.equal(f.answer_source, null);
    assert.equal(f.value_digest, null);
    assert.equal(f.inference_rationale_digest, null);
    assert.equal(f.inference_evidence_digests, null);
  }
});

test('approved user answers persist for same-run and fresh-run private reuse', async (t) => {
  const harness = createHarness(t);
  const firstRun = harness.runFor('remember-first');
  const first = await startRun(firstRun.runPath, { startedAt: '2026-07-25T08:00:00.000Z' });
  await acceptObservation(
    first,
    observation(firstRun.run.application_url, 1, [fieldControl('field')]),
  );

  await assert.rejects(
    resolveField(first, {
      field_id: 'field',
      alias: 'remembered',
      user: { answers: { remembered: 'Approved synthetic answer' } },
      remember: true,
      approved_at: '2026-02-30T00:00:00.000Z',
    }),
    /exact ISO date string/,
  );
  const approved = await resolveField(first, {
    field_id: 'field',
    alias: 'remembered',
    user: { answers: { remembered: 'Approved synthetic answer' } },
    remember: true,
    approved_at: '2026-07-25T08:00:01.000Z',
  });
  assert.equal(approved.answer.source, 'user');
  assert.equal(first.memory.length, 1);
  await recordAction(first, { action: 'fill', field_id: 'field', outcome: 'succeeded' });
  await acceptObservation(
    first,
    observation(firstRun.run.application_url, 2, [
      fieldControl('field', 'Approved synthetic answer', true),
    ]),
  );
  assert.equal((await verifyRetention(first)).ok, true);

  const sameRun = await resolveField(first, { field_id: 'field', alias: 'remembered' });
  assert.equal(sameRun.answer.source, 'memory');
  await recordAction(first, { action: 'fill', field_id: 'field', outcome: 'succeeded' });
  await acceptObservation(
    first,
    observation(firstRun.run.application_url, 3, [
      fieldControl('field', 'Approved synthetic answer', true),
    ]),
  );
  await finalizeRetained(first, harness.screenshotPath);

  const freshRun = harness.runFor('remember-fresh');
  const fresh = await startRun(freshRun.runPath, { startedAt: '2026-07-25T08:01:00.000Z' });
  await acceptObservation(
    fresh,
    observation(freshRun.run.application_url, 1, [fieldControl('field')]),
  );
  const reused = await resolveField(fresh, {
    field_id: 'field',
    alias: 'remembered',
    sensitive: true,
  });
  assert.equal(reused.answer.source, 'memory');
  assert.equal(
    reused.ledger.fields.find((field) => field.field_id === 'field').sensitive,
    true,
  );
  await recordAction(fresh, { action: 'fill', field_id: 'field', outcome: 'succeeded' });
  await acceptObservation(
    fresh,
    observation(freshRun.run.application_url, 2, [
      fieldControl('field', 'Approved synthetic answer', true),
    ]),
  );
  await finalizeRetained(fresh, harness.screenshotPath);
});
