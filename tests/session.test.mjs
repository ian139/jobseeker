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
  getPendingActionPlans,
  recordAction,
  recordActionBatch,
  recordActionPlan,
  recordPlannedActionResult,
  resolveCanonicalUpload,
  resolveField,
  startRun,
  verifyRetention,
} from '../src/phase1/session.mjs';
import { approvalContextSha256 } from '../src/phase1/contract.mjs';
import { digestPrivateValue } from '../src/phase1/ledger.mjs';
import {
  ACTION_RESULT_SCHEMA,
  createBrowserActionPlan,
} from '../src/phase1/action-plan.mjs';
import EvidenceStore from '../src/phase1/evidence.mjs';

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
function uploadControl(fieldId) {
  return {
    ...fieldControl(fieldId),
    kind: 'file',
    type: 'file',
    role: 'input',
    file: { accept: '.pdf', count: 0, names: [] },
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
  await assert.rejects(
    finalizeRun(session, { screenshotPath, finalUrl: screenshotPath }),
    /fresh post-submit observation|absolute http/,
  );
  const post = structuredClone(session.observation);
  post.observation_id = `${post.observation_id}-post-submit`;
  post.previous_observation_id = session.observation.observation_id;
  post.observed_at = '2026-07-25T08:01:00.000Z';
  await acceptObservation(session, post);
  await assert.rejects(
    finalizeRun(session, { screenshotPath, finalUrl: 'https://example.invalid/wrong' }),
    /match the post-submit observation URL/,
  );
  const final = await finalizeRun(session, { screenshotPath, finalUrl: post.url });
  assert.equal(final.finalized, true);
  assert.equal(final.submitActionCount, 1);
  assert.equal(session.finalized, true);
}
function plannedFillDecision(current, fieldId, value) {
  return {
    observationId: current.observation_id,
    fieldId,
    controlReference: `control-${fieldId}`,
    fieldPolicy: 'qualification',
    proposedAnswer: value,
    answerSource: 'profile',
    evidenceReferences: [`profile:${fieldId}`],
    inferenceRationaleDigest: null,
    inferenceEvidenceDigests: null,
    proposedAction: 'fill_text',
    expectedRetainedState: value,
    modelTier: 'standard',
    confidence: 1,
    reasonCode: 'profile_exact',
    reobservationRequired: true,
    automaticSubmissionEligible: false,
  };
}

function createFillPlan(session, fields, retryOf = undefined) {
  const decisions = fields.map(({ fieldId, value }) =>
    plannedFillDecision(session.observation, fieldId, value));
  const answerAliases = Object.fromEntries(fields.map(({ fieldId, value }) => [
    fieldId,
    { alias: fieldId, value },
  ]));
  return createBrowserActionPlan({
    observation: session.observation,
    ledger: session.ledger,
    decisions,
    answerAliases,
    optionMatches: {},
    driver: 'omp_browser',
    retryOf,
    createdAt: '2026-07-25T08:00:10.000Z',
    ats: 'greenhouse',
  });
}

test('planned receipts recover without browser replay and support retained retries across restarts', async (t) => {
  const harness = createHarness(t, { answers: { first: 'Ada', second: 'Lovelace' } });
  const { run, runPath } = harness.runFor('planned-recovery');
  const firstSession = await startRun(runPath, { startedAt: '2026-07-25T08:00:00.000Z' });
  await acceptObservation(firstSession, observation(run.application_url, 1, [
    fieldControl('first'),
    fieldControl('second'),
  ]));
  await resolveField(firstSession, { field_id: 'first', alias: 'first' });
  await resolveField(firstSession, { field_id: 'second', alias: 'second' });

  const batch = createFillPlan(firstSession, [
    { fieldId: 'first', value: 'Ada' },
    { fieldId: 'second', value: 'Lovelace' },
  ]);
  await recordActionPlan(firstSession, batch);
  await assert.rejects(
    acceptObservation(firstSession, observation(run.application_url, 2, [
      fieldControl('first', 'Ada', true),
      fieldControl('second'),
    ])),
    /pending/,
  );

  const partialObservation = observation(run.application_url, 2, [
    fieldControl('first', 'Ada', true),
    fieldControl('second'),
  ]);
  const partialResult = {
    schema: ACTION_RESULT_SCHEMA,
    plan_id: batch.plan_id,
    post_observation_id: partialObservation.observation_id,
    outcomes: [
      {
        action_id: batch.actions[0].action_id,
        outcome: 'succeeded',
        error_code: null,
        driver: 'omp_browser',
        selected_option_text: null,
      },
      {
        action_id: batch.actions[1].action_id,
        outcome: 'failed',
        error_code: 'synthetic_validation',
        driver: 'omp_browser',
        selected_option_text: null,
      },
    ],
  };

  const crashStore = new EvidenceStore(run.run_artifact_dir);
  crashStore.recordActionResult(batch, partialResult, partialObservation);
  for (const [index, outcome] of partialResult.outcomes.entries()) {
    const planned = batch.actions[index];
    crashStore.recordAction({
      action_id: planned.action_id,
      action: 'fill',
      field_id: planned.field_id,
      observation_id: batch.observation_id,
      ref: planned.control_reference,
      outcome: outcome.outcome,
      retry_of: planned.retry_of,
      error_code: outcome.error_code,
      stale_ref: false,
    });
  }
  crashStore.close();

  const recovered = await startRun(runPath, { resumeExisting: true });
  assert.deepEqual(await getPendingActionPlans(recovered), []);
  assert.equal(recovered.ledger.action_attempts.length, 2);
  assert.equal(
    recovered.ledger.fields.find((field) => field.field_id === 'first').retained,
    true,
  );
  const retryField = recovered.ledger.fields.find((field) => field.field_id === 'second');
  assert.equal(retryField.retained, false);
  assert.equal(retryField.retry_notes.length > 0, true);

  const retryPlan = createFillPlan(
    recovered,
    [{ fieldId: 'second', value: 'Lovelace' }],
    { second: 1 },
  );
  await recordActionPlan(recovered, retryPlan);
  const retainedObservation = observation(run.application_url, 3, [
    fieldControl('first', 'Ada', true),
    fieldControl('second', 'Lovelace', true),
  ]);
  const retryResult = {
    schema: ACTION_RESULT_SCHEMA,
    plan_id: retryPlan.plan_id,
    post_observation_id: retainedObservation.observation_id,
    outcomes: [{
      action_id: retryPlan.actions[0].action_id,
      outcome: 'succeeded',
      error_code: null,
      driver: 'omp_browser',
      selected_option_text: null,
    }],
  };
  const completed = await recordPlannedActionResult(
    recovered,
    retryPlan,
    retryResult,
    retainedObservation,
  );
  assert.equal(completed.retention.ok, true, JSON.stringify(completed.retention.errors));

  const restarted = await startRun(runPath, { resumeExisting: true });
  assert.deepEqual(await getPendingActionPlans(restarted), []);
  assert.equal(restarted.ledger.action_attempts.length, 3);
  assert.equal(
    restarted.ledger.fields.every((field) => field.final || field.retained),
    true,
  );
});
test('planned action retention is scoped away from unrelated aggregate failures', async (t) => {
  const harness = createHarness(t, { answers: { first: 'Ada', second: 'Lovelace' } });
  const { run, runPath } = harness.runFor('action-scoped-retention');
  const session = await startRun(runPath, { startedAt: '2026-07-25T08:00:00.000Z' });
  await acceptObservation(session, observation(run.application_url, 1, [
    fieldControl('first'),
    fieldControl('second'),
  ]));
  await resolveField(session, { field_id: 'first', alias: 'first' });
  await resolveField(session, { field_id: 'second', alias: 'second' });
  const plan = createFillPlan(session, [{ fieldId: 'first', value: 'Ada' }]);
  await recordActionPlan(session, plan);
  const postObservation = observation(run.application_url, 2, [
    fieldControl('first', 'Ada', true),
    fieldControl('second', 'Wrong', true),
  ]);
  const completed = await recordPlannedActionResult(session, plan, {
    schema: ACTION_RESULT_SCHEMA,
    plan_id: plan.plan_id,
    post_observation_id: postObservation.observation_id,
    outcomes: [{
      action_id: plan.actions[0].action_id,
      outcome: 'succeeded',
      error_code: null,
      driver: 'omp_browser',
      selected_option_text: null,
    }],
  }, postObservation);
  assert.equal(completed.actionRetention.ok, true);
  assert.equal(completed.actionRetention.retryRequired, false);
  assert.deepEqual(completed.actionRetention.fieldIds, ['first']);
  assert.equal(completed.retention.ok, false);
  assert.equal(Object.isFrozen(completed.actionRetention), true);
  const prepared = await prepareSubmission(session, { finalRef: 'control-final' });
  assert.equal(prepared.authorized, false);
});

test('failed planned action outcomes cannot report action retention success', async (t) => {
  const harness = createHarness(t, { answers: { first: 'Ada' } });
  const { run, runPath } = harness.runFor('action-failed-retention');
  const session = await startRun(runPath, { startedAt: '2026-07-25T08:00:00.000Z' });
  await acceptObservation(session, observation(run.application_url, 1, [fieldControl('first')]));
  await resolveField(session, { field_id: 'first', alias: 'first' });
  const plan = createFillPlan(session, [{ fieldId: 'first', value: 'Ada' }]);
  await recordActionPlan(session, plan);
  const postObservation = observation(run.application_url, 2, [fieldControl('first')]);
  const completed = await recordPlannedActionResult(session, plan, {
    schema: ACTION_RESULT_SCHEMA,
    plan_id: plan.plan_id,
    post_observation_id: postObservation.observation_id,
    outcomes: [{
      action_id: plan.actions[0].action_id,
      outcome: 'failed',
      error_code: 'synthetic_failure',
      driver: 'omp_browser',
      selected_option_text: null,
    }],
  }, postObservation);
  assert.equal(completed.actionRetention.ok, false);
  assert.equal(completed.actionRetention.retryRequired, true);
  assert.equal(completed.actionRetention.errors.some((error) => error.code === 'ACTION_OUTCOME_FAILED'), true);
});

test('canonical upload resolution binds the configured resume identity to a persisted plan', async (t) => {
  const harness = createHarness(t);
  const { run, runPath } = harness.runFor('canonical-upload');
  const session = await startRun(runPath, { startedAt: '2026-07-25T08:00:00.000Z' });
  await acceptObservation(
    session,
    observation(run.application_url, 1, [uploadControl('resume')]),
  );
  const resolved = await resolveCanonicalUpload(session, {
    field_id: 'resume',
    alias: 'canonical_resume_upload',
  });
  assert.equal(resolved.answer.source, 'resume');
  assert.equal(resolved.actionValue, session.runMetadata.resume_upload_path);
  assert.equal(
    resolved.resolution.value_digest,
    digestPrivateValue(session.runMetadata.resume_upload_path),
  );

  const decision = {
    ...plannedFillDecision(session.observation, 'resume', resolved.actionValue),
    answerSource: 'resume',
    evidenceReferences: [`resume-upload:${session.runMetadata.resume_upload_sha256}`],
    proposedAction: 'upload_file',
  };
  const plan = createBrowserActionPlan({
    observation: session.observation,
    ledger: session.ledger,
    decisions: [decision],
    answerAliases: {},
    optionMatches: {},
    resumeUpload: {
      path: session.runMetadata.resume_upload_path,
      sha256: session.runMetadata.resume_upload_sha256,
    },
    driver: 'omp_browser',
    createdAt: '2026-07-25T08:00:10.000Z',
    ats: 'greenhouse',
  });
  const persisted = await recordActionPlan(session, plan);
  assert.equal(persisted.plan.actions[0].steps[0].helper, 'uploadFile');
  assert.equal(
    persisted.plan.actions[0].retention.artifact_sha256,
    session.runMetadata.resume_upload_sha256,
  );
});

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

test('numeric profile answers retain canonical salary and GPA DOM values', async (t) => {
  const harness = createHarness(t, {
    profile: {
      schema: 'phase1-profile-v1',
      compensation: { currency: 'USD', target: 120000, period: 'year' },
      education: [{
        institution: 'Example University',
        level: 'college',
        gpa: 3.8,
        current: true,
      }],
    },
  });
  const { run, runPath } = harness.runFor('numeric-profile-values');
  const session = await startRun(runPath, { startedAt: '2026-07-25T08:00:00.000Z' });
  const controls = (salary = null, salaryPresent = false, gpa = null, gpaPresent = false) => [
    {
      ...fieldControl('salary', salary, salaryPresent),
      label: 'What salary do you require?',
    },
    {
      ...fieldControl('gpa', gpa, gpaPresent),
      label: 'College GPA',
    },
  ];

  await acceptObservation(session, observation(run.application_url, 1, controls()));
  const salary = await resolveField(session, {
    field_id: 'salary',
    alias: 'What salary do you require?',
    sensitive: true,
  });
  assert.equal(salary.answer.source, 'profile');
  assert.equal(salary.answer.value, 120000);
  await recordAction(session, { action: 'fill', field_id: 'salary', outcome: 'succeeded' });
  await acceptObservation(
    session,
    observation(run.application_url, 2, controls('$120,000', true)),
  );
  let retention = await verifyRetention(session);
  assert.equal(retention.ledger.fields.find((field) => field.field_id === 'salary').retained, true);

  const gpa = await resolveField(session, {
    field_id: 'gpa',
    alias: 'College GPA',
    sensitive: true,
  });
  assert.equal(gpa.answer.source, 'profile');
  assert.equal(gpa.answer.value, 3.8);
  await recordAction(session, { action: 'fill', field_id: 'gpa', outcome: 'succeeded' });
  await acceptObservation(
    session,
    observation(run.application_url, 3, controls('$120,000', true, '3.8', true)),
  );
  retention = await verifyRetention(session);
  assert.equal(retention.ok, true);
  assert.equal(retention.ledger.fields.find((field) => field.field_id === 'gpa').retained, true);
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
  const post = structuredClone(session.observation);
  post.observation_id = `${post.observation_id}-post-submit`;
  post.previous_observation_id = session.observation.observation_id;
  post.observed_at = '2026-07-25T08:01:00.000Z';
  await acceptObservation(session, post);

  const final = await finalizeRun(session, { screenshotPath: harness.screenshotPath, finalUrl: post.url });
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
    finalizeRun(session, { screenshotPath: harness.screenshotPath, finalUrl: run.application_url }),
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
  const post = structuredClone(session.observation);
  post.observation_id = `${post.observation_id}-post-submit`;
  post.previous_observation_id = session.observation.observation_id;
  post.observed_at = '2026-07-25T08:01:00.000Z';
  await acceptObservation(session, post);
  const finalized = await finalizeRun(session, { screenshotPath: harness.screenshotPath, finalUrl: post.url });
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
        optional_reason: { value: 'Not applicable', rationale: 'The role requirements make this optional question not applicable.' },
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
  assert.equal(inferredBlank.actionValue, null);
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
  const remembered = first.memory[0];
  assert.equal(remembered.schema, 'phase1-answer-v2');
  assert.equal(Object.isFrozen(remembered), true);
  const approval_context = {
    run_contract_sha256: first.runMetadata.run_contract_sha256,
    observation_id: 'obs-1',
    field_id: 'field',
    alias: 'remembered',
  };
  assert.deepEqual(remembered.approval_context, approval_context);
  assert.equal(
    remembered.approval_context_sha256,
    approvalContextSha256(approval_context),
  );
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

const GREENHOUSE_URL = 'https://job-boards.greenhouse.io/acme/jobs/12345';
const STALE_REQUIRED = { valid: false, aria_invalid: true, message: 'This field is required.' };

function comboboxControl(fieldId, options = {}) {
  const value = options.value ?? null;
  return {
    ...fieldControl(fieldId, value, options.valuePresent ?? false),
    kind: options.kind ?? 'input',
    tag: options.tag ?? 'input',
    type: 'text',
    role: 'combobox',
    label: options.label ?? fieldId,
    locator: { strategy: 'id', value: fieldId, role: 'combobox', name: fieldId },
    selected: options.selected ?? null,
    options: options.options ?? [],
    validity: options.validity ?? { valid: true, aria_invalid: null, message: null },
    candidate: { class: 'field', reason: 'synthetic custom combobox' },
  };
}

function plannedSelectDecision(current, fieldId, value) {
  return {
    observationId: current.observation_id,
    fieldId,
    controlReference: `control-${fieldId}`,
    fieldPolicy: 'qualification',
    proposedAnswer: value,
    answerSource: 'profile',
    evidenceReferences: [`profile:${fieldId}`],
    inferenceRationaleDigest: null,
    inferenceEvidenceDigests: null,
    proposedAction: 'select_option',
    expectedRetainedState: value,
    modelTier: 'standard',
    confidence: 1,
    reasonCode: 'profile_exact',
    reobservationRequired: true,
    automaticSubmissionEligible: false,
  };
}

function createSelectPlan(session, fieldId, value) {
  return createBrowserActionPlan({
    observation: session.observation,
    ledger: session.ledger,
    decisions: [plannedSelectDecision(session.observation, fieldId, value)],
    answerAliases: { [fieldId]: { alias: fieldId, value } },
    optionMatches: { [fieldId]: { option_text: value, option_value: value } },
    driver: 'omp_browser',
    createdAt: '2026-08-05T08:00:10.000Z',
    ats: 'greenhouse',
  });
}

function committedCombobox(fieldId, options = {}) {
  return comboboxControl(fieldId, {
    value: options.value ?? 'Engineering',
    valuePresent: true,
    selected: [options.value ?? 'Engineering'],
    validity: STALE_REQUIRED,
    ...options,
  });
}

test('planned Greenhouse select normalizes, persists, and retains a stale required post observation', async (t) => {
  const harness = createHarness(t, { answers: { department: 'Engineering' } });
  const { run, runPath } = harness.runFor('greenhouse-select-normalize');
  const session = await startRun(runPath, { startedAt: '2026-08-05T08:00:00.000Z' });
  await acceptObservation(session, observation(GREENHOUSE_URL, 1, [comboboxControl('department')]));
  await resolveField(session, { field_id: 'department', alias: 'department' });
  const plan = createSelectPlan(session, 'department', 'Engineering');
  await recordActionPlan(session, plan);

  const postObservation = observation(GREENHOUSE_URL, 2, [
    committedCombobox('department'),
  ]);
  const completed = await recordPlannedActionResult(session, plan, {
    schema: ACTION_RESULT_SCHEMA,
    plan_id: plan.plan_id,
    post_observation_id: postObservation.observation_id,
    outcomes: [{
      action_id: plan.actions[0].action_id,
      outcome: 'succeeded',
      error_code: null,
      driver: 'omp_browser',
      selected_option_text: 'Engineering',
    }],
  }, postObservation);

  assert.equal(completed.retention.ok, true, JSON.stringify(completed.retention.errors));
  assert.equal(completed.actionRetention.ok, true);
  assert.equal(completed.actionRetention.retryRequired, false);

  const acceptedControl = session.observation.controls.find((control) => control.stable_id === 'department');
  assert.deepEqual(acceptedControl.validity, { valid: true, aria_invalid: false, message: null });
  assert.equal(session.observation.observation_id, postObservation.observation_id);
  assert.equal(session.observation.previous_observation_id, 'obs-1');
  assert.notEqual(session.observation.snapshot_sha256, postObservation.snapshot_sha256);
  assert.equal(
    completed.accepted.observation.controls.find((control) => control.stable_id === 'department').validity.valid,
    true,
  );

  const field = session.ledger.fields.find((item) => item.field_id === 'department');
  assert.equal(field.answer_state, 'answered');
  assert.equal(field.retained, true);
  assert.equal(field.valid, true);

  const store = new EvidenceStore(run.run_artifact_dir);
  try {
    const receipts = store.listActionResults();
    assert.equal(receipts.length, 1);
    const receipt = store.readActionResult(receipts[0]);
    assert.equal(receipt.post_observation.observation_id, postObservation.observation_id);
    assert.deepEqual(
      receipt.post_observation.controls.find((control) => control.stable_id === 'department').validity,
      { valid: true, aria_invalid: false, message: null },
    );
    assert.equal(
      receipt.post_observation.snapshot_sha256,
      session.observation.snapshot_sha256,
    );
  } finally {
    store.close();
  }
});

test('carries normalization forward to later direct observations from ledger select proof', async (t) => {
  const harness = createHarness(t, { answers: { department: 'Engineering' } });
  const { runPath } = harness.runFor('greenhouse-select-carry-forward');
  const session = await startRun(runPath, { startedAt: '2026-08-05T08:00:00.000Z' });
  await acceptObservation(session, observation(GREENHOUSE_URL, 1, [comboboxControl('department')]));
  await resolveField(session, { field_id: 'department', alias: 'department' });
  const plan = createSelectPlan(session, 'department', 'Engineering');
  await recordActionPlan(session, plan);
  const completed = await recordPlannedActionResult(session, plan, {
    schema: ACTION_RESULT_SCHEMA,
    plan_id: plan.plan_id,
    post_observation_id: 'obs-2',
    outcomes: [{
      action_id: plan.actions[0].action_id,
      outcome: 'succeeded',
      error_code: null,
      driver: 'omp_browser',
      selected_option_text: 'Engineering',
    }],
  }, observation(GREENHOUSE_URL, 2, [committedCombobox('department')]));
  assert.equal(completed.retention.ok, true);

  const carried = observation(GREENHOUSE_URL, 3, [
    comboboxControl('department', {
      value: 'Engineering',
      valuePresent: true,
      selected: ['Engineering'],
      validity: STALE_REQUIRED,
    }),
  ]);
  await acceptObservation(session, carried);
  assert.equal(session.observation.observation_id, 'obs-3');
  assert.deepEqual(
    session.observation.controls.find((control) => control.stable_id === 'department').validity,
    { valid: true, aria_invalid: false, message: null },
  );
  const retention = await verifyRetention(session);
  assert.equal(retention.ok, true, JSON.stringify(retention.errors));
});

test('historical normalized Greenhouse receipts recover unchanged', async (t) => {
  const harness = createHarness(t, { answers: { department: 'Engineering' } });
  const { runPath } = harness.runFor('greenhouse-select-recovery');
  const session = await startRun(runPath, { startedAt: '2026-08-05T08:00:00.000Z' });
  await acceptObservation(session, observation(GREENHOUSE_URL, 1, [comboboxControl('department')]));
  await resolveField(session, { field_id: 'department', alias: 'department' });
  const plan = createSelectPlan(session, 'department', 'Engineering');
  await recordActionPlan(session, plan);
  const completed = await recordPlannedActionResult(session, plan, {
    schema: ACTION_RESULT_SCHEMA,
    plan_id: plan.plan_id,
    post_observation_id: 'obs-2',
    outcomes: [{
      action_id: plan.actions[0].action_id,
      outcome: 'succeeded',
      error_code: null,
      driver: 'omp_browser',
      selected_option_text: 'Engineering',
    }],
  }, observation(GREENHOUSE_URL, 2, [committedCombobox('department')]));
  assert.equal(completed.retention.ok, true);

  const restarted = await startRun(runPath, { resumeExisting: true });
  assert.deepEqual(await getPendingActionPlans(restarted), []);
  assert.equal(restarted.observation.observation_id, 'obs-2');
  assert.deepEqual(
    restarted.observation.controls.find((control) => control.stable_id === 'department').validity,
    { valid: true, aria_invalid: false, message: null },
  );
  assert.equal(
    restarted.ledger.fields.find((field) => field.field_id === 'department').retained,
    true,
  );
  assert.equal(restarted.ledger.action_attempts.length, 1);
});

test('genuine custom select errors stay fail-closed and are never normalized', async (t) => {
  const harness = createHarness(t, { answers: { department: 'Engineering' } });
  const { runPath } = harness.runFor('greenhouse-select-genuine-error');
  const session = await startRun(runPath, { startedAt: '2026-08-05T08:00:00.000Z' });
  await acceptObservation(session, observation(GREENHOUSE_URL, 1, [comboboxControl('department')]));
  await resolveField(session, { field_id: 'department', alias: 'department' });
  const plan = createSelectPlan(session, 'department', 'Engineering');
  await recordActionPlan(session, plan);

  const postObservation = observation(GREENHOUSE_URL, 2, [
    comboboxControl('department', {
      value: 'Engineering',
      valuePresent: true,
      selected: ['Engineering'],
      validity: { valid: false, aria_invalid: true, message: 'Please select a valid option.' },
    }),
  ]);
  const completed = await recordPlannedActionResult(session, plan, {
    schema: ACTION_RESULT_SCHEMA,
    plan_id: plan.plan_id,
    post_observation_id: postObservation.observation_id,
    outcomes: [{
      action_id: plan.actions[0].action_id,
      outcome: 'succeeded',
      error_code: null,
      driver: 'omp_browser',
      selected_option_text: 'Engineering',
    }],
  }, postObservation);

  assert.equal(completed.retention.ok, false);
  assert.equal(completed.actionRetention.ok, false);
  assert.equal(completed.actionRetention.retryRequired, true);
  assert.equal(
    session.observation.controls.find((control) => control.stable_id === 'department').validity.valid,
    false,
  );
  assert.equal(
    session.ledger.fields.find((field) => field.field_id === 'department').valid,
    false,
  );
});

test('stale required errors outside Greenhouse hosts stay fail-closed', async (t) => {
  const harness = createHarness(t, { answers: { department: 'Engineering' } });
  const { run, runPath } = harness.runFor('non-greenhouse-select-stale');
  const session = await startRun(runPath, { startedAt: '2026-08-05T08:00:00.000Z' });
  await acceptObservation(session, observation(run.application_url, 1, [comboboxControl('department')]));
  await resolveField(session, { field_id: 'department', alias: 'department' });
  const plan = createSelectPlan(session, 'department', 'Engineering');
  await recordActionPlan(session, plan);

  const postObservation = observation(run.application_url, 2, [committedCombobox('department')]);
  const completed = await recordPlannedActionResult(session, plan, {
    schema: ACTION_RESULT_SCHEMA,
    plan_id: plan.plan_id,
    post_observation_id: postObservation.observation_id,
    outcomes: [{
      action_id: plan.actions[0].action_id,
      outcome: 'succeeded',
      error_code: null,
      driver: 'omp_browser',
      selected_option_text: 'Engineering',
    }],
  }, postObservation);

  assert.equal(completed.retention.ok, false);
  assert.equal(completed.actionRetention.ok, false);
  assert.equal(completed.actionRetention.retryRequired, true);
  assert.equal(
    session.observation.controls.find((control) => control.stable_id === 'department').validity.valid,
    false,
  );
});
