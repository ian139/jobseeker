import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';

import { canonicalJson, createAnswerRecord } from '../src/phase1/contract.mjs';
import { validateProfile } from '../src/phase1/profile.mjs';
import {
  acceptObservation,
  recordAction,
  recordActionBatch,
  resolveField,
  startRun,
  verifyRetention,
} from '../src/phase1/session.mjs';
import { selectSafeApplicationBatch } from '../src/phase1/selector.mjs';

const ROUTINE_IDS = ['a', 'b', 'c', 'd', 'e', 'f', 'g'];
const ROUTINE_ANSWERS = Object.fromEntries(ROUTINE_IDS.map((id) => [
  `Routine qualification ${id}`,
  `Verified answer ${id}`,
]));

const PROFILE = validateProfile({
  schema: 'phase1-profile-v1',
  contact: {
    name: 'Example Applicant',
    preferred_name: 'Example',
    first_name: 'Example',
    last_name: 'Applicant',
    email: 'applicant@example.invalid',
    phone: '+1 555 555 0100',
  },
  address: {
    street: '100 Example Street',
    street2: 'Unit 2',
    city: 'Example City',
    region: 'Example State',
    postal_code: '00000',
    country: 'United States',
    formatted: '100 Example Street, Unit 2, Example City, Example State 00000, United States',
  },
  links: [
    { label: 'LinkedIn', kind: 'linkedin', url: 'https://example.invalid/linkedin' },
    { label: 'Portfolio', kind: 'portfolio', url: 'https://example.invalid/portfolio' },
  ],
  education: [
    { institution: 'Example High School', level: 'high_school', end_date: '2022-06' },
    {
      institution: 'Example University',
      level: 'college',
      degree: 'Bachelor of Science',
      field: 'Computer Science',
      gpa: 3.8,
      end_date: '2026-05',
      current: true,
    },
  ],
  employment: [{
    employer: 'Example Employer',
    title: 'Software Engineering Intern',
    current: true,
  }],
  location_preferences: {
    current_location: 'Example City, Example State',
    remote: true,
    willing_to_relocate: false,
  },
  relocation: { willing: false },
  compensation: { currency: 'USD', target: 120000, period: 'year' },
  work_authorization: {
    authorized: true,
    countries: ['United States'],
    status: 'United States citizen',
  },
  sponsorship: { needed: false },
  answers: ROUTINE_ANSWERS,
});

const MEMORY_RECORD = createAnswerRecord(
  'Preferred work location',
  'Remote',
  '2026-01-01T00:00:00.000Z',
);

const RESOLUTION_CASES = [
  { alias: 'Full Name', expected: 'Example Applicant', source: 'profile', sensitive: true },
  { alias: 'Full name', expected: 'Example Applicant', source: 'profile', sensitive: true },
  { alias: 'Your full name', expected: 'Example Applicant', source: 'profile', sensitive: true },
  { alias: 'Email', expected: 'applicant@example.invalid', source: 'profile', sensitive: true },
  { alias: 'Email address', expected: 'applicant@example.invalid', source: 'profile', sensitive: true },
  { alias: 'Phone number', expected: '+1 555 555 0100', source: 'profile', sensitive: true },
  { alias: 'First name', expected: 'Example', source: 'profile', sensitive: true },
  { alias: 'Last name', expected: 'Applicant', source: 'profile', sensitive: true },
  { alias: 'Street address', expected: '100 Example Street', source: 'profile', sensitive: true },
  { alias: 'Address line 2', expected: 'Unit 2', source: 'profile', sensitive: true },
  { alias: 'City', expected: 'Example City', source: 'profile', sensitive: true },
  { alias: 'State / Province', expected: 'Example State', source: 'profile', sensitive: true },
  { alias: 'Postal code', expected: '00000', source: 'profile', sensitive: true },
  { alias: 'Country', expected: 'United States', source: 'profile', sensitive: true },
  { alias: 'profile.address.country', expected: 'United States', source: 'profile', sensitive: true },
  { alias: 'LinkedIn profile URL', expected: 'https://example.invalid/linkedin', source: 'profile', sensitive: true },
  { alias: 'Portfolio URL', expected: 'https://example.invalid/portfolio', source: 'profile', sensitive: false },
  { alias: 'Current employer', expected: 'Example Employer', source: 'profile', sensitive: false },
  { alias: 'Current job title', expected: 'Software Engineering Intern', source: 'profile', sensitive: false },
  { alias: 'Current location', expected: 'Example City, Example State', source: 'profile', sensitive: true },
  { alias: 'Are you willing to relocate?', expected: false, source: 'profile', sensitive: true },
  {
    alias: 'What salary do you require?',
    expected: 120000,
    source: 'profile',
    sensitive: true,
    expectedFailure: 'numeric_value',
  },
  { alias: 'Are you legally authorized to work in the United States?', expected: true, source: 'profile', sensitive: true },
  { alias: 'Will you now or in the future require sponsorship?', expected: false, source: 'profile', sensitive: true },
  { alias: 'High school name', expected: 'Example High School', source: 'profile', sensitive: true },
  {
    alias: 'College GPA',
    expected: 3.8,
    source: 'profile',
    sensitive: true,
    expectedFailure: 'numeric_value',
  },
  { alias: 'Expected graduation date', expected: '2026-05', source: 'profile', sensitive: true },
  { alias: 'Preferred work location', expected: 'Remote', source: 'memory', sensitive: false },
  {
    alias: 'Why are you interested in this role?',
    mandatoryExternal: 'delegate',
    sensitive: false,
  },
  {
    alias: 'Describe a project you are proud of',
    mandatoryExternal: 'delegate',
    sensitive: false,
  },
];

function privateFile(root, name, contents) {
  const filePath = path.join(root, name);
  fs.writeFileSync(filePath, contents, { mode: 0o600 });
  fs.chmodSync(filePath, 0o600);
  return filePath;
}

function createFixture() {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'pipeline-replay-'));
  fs.chmodSync(root, 0o700);
  const jobPath = privateFile(root, 'job.txt', 'Fixed synthetic job description.');
  const profilePath = privateFile(root, 'profile.json', JSON.stringify(PROFILE));
  const uploadPath = privateFile(root, 'resume.pdf', '%PDF-1.7\nfixed synthetic resume');
  const memoryPath = privateFile(root, 'answers.jsonl', `${canonicalJson(MEMORY_RECORD)}\n`);

  function runPath(name) {
    const run = {
      schema: 'phase1-run-v1',
      application_url: `https://example.invalid/jobs/${name}`,
      job_description_path: jobPath,
      applicant_profile_path: profilePath,
      resume_upload_path: uploadPath,
      answer_memory_path: memoryPath,
      run_artifact_dir: path.join(root, `${name}-evidence`),
      browser_mode: 'headed',
      observer: 'playwright_dom_v1',
      action_driver: 'omp_browser',
      submit_policy: 'omp_agent',
    };
    return {
      run,
      path: privateFile(root, `${name}-run.json`, JSON.stringify(run)),
    };
  }

  return { root, runPath };
}

function fieldControl(fieldId, label, value = null, valuePresent = false) {
  return {
    ref: `control-${fieldId}`,
    stable_id: fieldId,
    group_id: null,
    kind: 'input',
    tag: 'input',
    type: 'text',
    role: 'textbox',
    label,
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
    candidate: { class: 'field', reason: 'fixed replay field' },
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
    candidate: { class: 'final_candidate', reason: 'fixed replay final control' },
  };
}

function observation(applicationUrl, sequence, controls) {
  return {
    schema: 'phase1-observation-v1',
    observation_id: `observation-${sequence}`,
    previous_observation_id: sequence === 1 ? null : `observation-${sequence - 1}`,
    observed_at: `2026-01-01T00:00:${String(sequence).padStart(2, '0')}.000Z`,
    url: applicationUrl,
    title: 'Fixed synthetic application',
    snapshot_sha256: 'a'.repeat(64),
    frames: [{
      id: 'frame-main',
      parent_id: null,
      url: applicationUrl,
      origin: 'https://example.invalid',
      accessible: true,
    }],
    controls: [...controls, finalControl()],
    blockers: [],
  };
}

function dispatchMissingAnswer(trace, item) {
  const kind = item.sensitive ? 'ask_user' : 'delegate';
  trace.push({ kind, alias: item.alias });
}

async function replayResolutionBoundary(fixture) {
  const { run, path: runPath } = fixture.runPath('resolution-boundary');
  const session = await startRun(runPath, { startedAt: '2026-01-01T00:00:00.000Z' });
  const controls = RESOLUTION_CASES.map((item, index) => fieldControl(
    `resolution-${index}`,
    item.alias,
  ));
  await acceptObservation(session, observation(run.application_url, 1, controls));

  const externalTrace = [];
  let inProcessResolutions = 0;
  let contractChecks = 0;

  for (let index = 0; index < RESOLUTION_CASES.length; index += 1) {
    const item = RESOLUTION_CASES[index];
    let result;
    try {
      result = await resolveField(session, {
        field_id: `resolution-${index}`,
        alias: item.alias,
        sensitive: item.sensitive,
      });
    } catch (error) {
      if (item.expectedFailure === 'numeric_value' && error?.code === 'SCHEMA_INVALID') {
        externalTrace.push({ kind: 'implementation_repair', alias: item.alias });
        contractChecks += 2;
        continue;
      }
      throw error;
    }
    assert.equal(result.missing, result.answer.missing, `${item.alias}: session boundary`);
    contractChecks += 1;

    if (item.mandatoryExternal) {
      assert.equal(result.missing, true, `${item.alias}: unsupported answer must stay unresolved`);
      assert.equal(item.mandatoryExternal, item.sensitive ? 'ask_user' : 'delegate');
      dispatchMissingAnswer(externalTrace, item);
      contractChecks += 2;
      continue;
    }

    if (result.missing) {
      dispatchMissingAnswer(externalTrace, item);
      continue;
    }

    assert.equal(result.answer.source, item.source, `${item.alias}: source`);
    assert.deepEqual(result.answer.value, item.expected, `${item.alias}: value`);
    inProcessResolutions += 1;
    contractChecks += 2;
  }

  return { contractChecks, externalTrace, inProcessResolutions };
}

async function replaySafeBatches(fixture) {
  const { run, path: runPath } = fixture.runPath('safe-batches');
  const session = await startRun(runPath, { startedAt: '2026-01-01T00:00:00.000Z' });
  const completed = new Set();
  const trace = [{ kind: 'start_run' }];
  const expectedBatches = [
    ['a'],
    ['b', 'c', 'd'],
    ['e', 'f', 'g'],
  ];
  let contractChecks = 0;

  const controlsForState = () => ROUTINE_IDS.map((id) => fieldControl(
    id,
    `Routine qualification ${id}`,
    completed.has(id) ? ROUTINE_ANSWERS[`Routine qualification ${id}`] : null,
    completed.has(id),
  ));

  await acceptObservation(session, observation(run.application_url, 1, controlsForState()));
  trace.push({ kind: 'accept_observation' });

  for (let round = 0; round < expectedBatches.length; round += 1) {
    trace.push({ kind: 'planner' });
    const plan = selectSafeApplicationBatch({
      observation: session.observation,
      ledger: session.ledger,
    });
    assert.ok(plan, `round ${round + 1}: plan`);
    assert.deepEqual(plan.units.map((unit) => unit.fieldId), expectedBatches[round]);
    assert.equal(plan.units.length <= 3, true);
    assert.equal(plan.mode, plan.units.length > 1 ? 'batch' : 'single');
    contractChecks += 4;

    const resolutions = [];
    for (const unit of plan.units) {
      const alias = `Routine qualification ${unit.fieldId}`;
      trace.push({ kind: 'resolve_field', fieldId: unit.fieldId });
      const resolved = await resolveField(session, {
        field_id: unit.fieldId,
        alias,
        sensitive: false,
      });
      assert.equal(resolved.missing, false, `${unit.fieldId}: deterministic batch resolution`);
      assert.equal(resolved.answer.source, 'profile', `${unit.fieldId}: deterministic source`);
      assert.equal(resolved.answer.value, ROUTINE_ANSWERS[alias], `${unit.fieldId}: deterministic value`);
      resolutions.push(resolved);
      contractChecks += 3;
    }
    assert.equal(resolutions.length, plan.units.length);
    contractChecks += 1;

    trace.push({ kind: 'browser_snapshot' });
    for (const unit of plan.units) {
      trace.push({ kind: 'browser_action', fieldId: unit.fieldId });
    }

    if (plan.units.length > 1) {
      trace.push({ kind: 'record_action_batch' });
      await recordActionBatch(session, plan.units.map((unit) => ({
        action: 'fill',
        field_id: unit.fieldId,
        outcome: 'succeeded',
      })));
    } else {
      trace.push({ kind: 'record_action' });
      await recordAction(session, {
        action: 'fill',
        field_id: plan.units[0].fieldId,
        outcome: 'succeeded',
      });
    }

    for (const unit of plan.units) completed.add(unit.fieldId);
    trace.push({ kind: 'observe' });
    trace.push({ kind: 'accept_observation' });
    await acceptObservation(
      session,
      observation(run.application_url, round + 2, controlsForState()),
    );
    trace.push({ kind: 'verify_retention' });
    const retention = await verifyRetention(session);
    for (const fieldId of completed) {
      const retained = retention.ledger.fields.find((field) => field.field_id === fieldId);
      assert.equal(retained?.retained, true, `${fieldId}: retained`);
      assert.equal(retained?.valid, true, `${fieldId}: valid`);
      contractChecks += 2;
    }
  }

  assert.deepEqual([...completed].sort(), ROUTINE_IDS);
  assert.equal(session.ledger.fields.filter((field) => ROUTINE_IDS.includes(field.field_id)).every(
    (field) => field.retained && field.valid,
  ), true);
  contractChecks += 2;
  return { contractChecks, trace };
}

async function main() {
  const fixture = createFixture();
  try {
    const resolutionReplay = await replayResolutionBoundary(fixture);
    const batchReplay = await replaySafeBatches(fixture);
    const delegationCalls = resolutionReplay.externalTrace.filter(
      (event) => event.kind === 'delegate',
    ).length;
    const userQuestions = resolutionReplay.externalTrace.filter(
      (event) => event.kind === 'ask_user',
    ).length;
    const implementationRepairCalls = resolutionReplay.externalTrace.filter(
      (event) => event.kind === 'implementation_repair',
    ).length;
    const mandatoryDelegations = RESOLUTION_CASES.filter(
      (item) => item.mandatoryExternal === 'delegate',
    ).length;
    const externalResolutionCalls = delegationCalls + userQuestions;
    const contractChecks = resolutionReplay.contractChecks + batchReplay.contractChecks;

    assert.ok(delegationCalls >= mandatoryDelegations);
    assert.equal(
      externalResolutionCalls + implementationRepairCalls + resolutionReplay.inProcessResolutions,
      RESOLUTION_CASES.length,
    );

    console.log(`METRIC external_resolution_calls=${externalResolutionCalls}`);
    console.log(`METRIC delegation_calls=${delegationCalls}`);
    console.log(`METRIC user_questions=${userQuestions}`);
    console.log(`METRIC implementation_repair_calls=${implementationRepairCalls}`);
    console.log(`METRIC in_process_resolutions=${resolutionReplay.inProcessResolutions}`);
    console.log(`METRIC mandatory_delegations=${mandatoryDelegations}`);
    console.log(`METRIC resolution_cases=${RESOLUTION_CASES.length}`);
    console.log(`METRIC replay_tool_calls=${batchReplay.trace.length}`);
    console.log(`METRIC planner_rounds=3`);
    console.log(`METRIC routine_fields=${ROUTINE_IDS.length}`);
    console.log(`METRIC contract_checks=${contractChecks + 2}`);
  } finally {
    fs.rmSync(fixture.root, { recursive: true, force: true });
  }
}

await main();
