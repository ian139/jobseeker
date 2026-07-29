import assert from 'node:assert/strict';
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
  resolveField,
  startRun,
  verifyRetention,
} from '../src/phase1/session.mjs';

const SCREENSHOT_SHA256 = 'a'.repeat(64);

function privateFile(root, name, contents) {
  const filePath = path.join(root, name);
  fs.writeFileSync(filePath, contents, { mode: 0o600 });
  fs.chmodSync(filePath, 0o600);
  return filePath;
}

function target(targetId, overrides = {}) {
  return {
    target_id: targetId,
    field_id: targetId,
    group_id: null,
    kind: 'text',
    label: `Question ${targetId}`,
    description: null,
    bounds: { x: 20, y: 20, width: 300, height: 40 },
    visible: true,
    enabled: true,
    required: true,
    readonly: false,
    value_state: 'blank',
    checked: null,
    selected: null,
    options: [],
    validation: { valid: true, message_present: false },
    file: null,
    candidate: { class: 'field', reason: 'visible application field' },
    confidence: 0.99,
    ...overrides,
  };
}

function finalTarget() {
  return target('target-submit', {
    field_id: null,
    kind: 'button',
    label: 'Submit application',
    required: false,
    candidate: { class: 'final_candidate', reason: 'current final action' },
  });
}

function observation(id, targets, previous = null, title = 'Application') {
  return {
    schema: 'phase1-visual-observation-v1',
    observation_id: id,
    previous_observation_id: previous,
    observed_at: `2026-07-28T00:00:0${id.at(-1) ?? '0'}.000Z`,
    surface: {
      surface_id: 'surface-current-browser',
      url: 'https://example.invalid/apply',
      title,
      screenshot_sha256: SCREENSHOT_SHA256,
      viewport: { width: 1280, height: 720 },
    },
    agent: { provider: 'codex', model: 'codex' },
    targets,
    blockers: [],
  };
}

function harness(t) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'visual-session-'));
  fs.chmodSync(root, 0o700);
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const jobPath = privateFile(root, 'job.txt', 'Synthetic role');
  const resumePath = privateFile(root, 'resume.pdf', '%PDF-1.7\nsynthetic resume');
  const screenshotPath = privateFile(root, 'submitted.png', 'synthetic screenshot');
  const memoryRoot = path.join(root, 'memory');
  fs.mkdirSync(memoryRoot, { mode: 0o700 });
  const evidenceRoot = path.join(root, 'evidence');
  const runPath = path.join(root, 'run.json');
  const run = {
    schema: 'phase1-run-v2',
    application_url: 'https://example.invalid/apply',
    job_description_path: jobPath,
    source_resume_path: resumePath,
    resume_upload_path: resumePath,
    answer_memory_path: path.join(memoryRoot, 'answers.jsonl'),
    run_artifact_dir: evidenceRoot,
    browser_mode: 'headed',
    perception_driver: 'image_agent_v1',
    action_driver: 'omp_computer',
    model_provider: 'codex',
    submit_policy: 'omp_agent',
  };
  privateFile(root, 'run.json', JSON.stringify(run));
  return { runPath, screenshotPath };
}

test('visual session enforces image-bound action, retention, audit, and final submission', async (t) => {
  const fixture = harness(t);
  const session = await startRun(fixture.runPath, { startedAt: '2026-07-28T00:00:00.000Z' });
  const first = observation('observation-1', [target('full-name'), finalTarget()]);
  await acceptObservation(session, first);

  const resolution = await resolveField(session, {
    field_id: 'full-name',
    target_id: 'full-name',
    alias: 'Full name',
    user: { 'Full name': 'Private Applicant' },
  });
  assert.equal(resolution.answer.source, 'user');
  assert.equal(JSON.stringify(resolution.ledger).includes('Private Applicant'), false);
  assert.equal(resolution.ledger.targets[0].answer_state, 'answered');

  const action = await recordAction(session, {
    action: 'type_text',
    field_id: 'full-name',
    target_id: 'full-name',
    outcome: 'succeeded',
  });
  assert.equal(action.ledger.targets[0].answer_state, 'answered');
  await assert.rejects(
    recordAction(session, { action: 'type_text', field_id: 'full-name', target_id: 'full-name', outcome: 'succeeded' }),
    /observe again|fresh observation|consumed/i,
  );

  const second = observation('observation-2', [
    target('full-name', { value_state: 'present' }),
    finalTarget(),
  ], 'observation-1');
  await acceptObservation(session, second);
  const retained = await verifyRetention(session, {
    'full-name': { action_id: action.action.action_id, visually_confirmed: true },
  });
  assert.equal(retained.ok, true);
  assert.equal(retained.ledger.targets[0].answer_state, 'answered');

  const prepared = await prepareSubmission(session, { finalTargetId: 'target-submit' });
  assert.equal(prepared.authorized, true);
  const begun = await beginFinalSubmit(session);
  assert.equal(begun.finalTargetId, 'target-submit');
  await assert.rejects(
    completeFinalSubmit(session, { attemptId: begun.attemptId, outcome: 'succeeded' }),
    /fresh visual observation/i,
  );

  const postSubmit = observation('observation-3', [], 'observation-2', 'Application received');
  await acceptObservation(session, postSubmit);
  await completeFinalSubmit(session, { attemptId: begun.attemptId, outcome: 'succeeded' });
  const finalized = await finalizeRun(session, { screenshotPath: fixture.screenshotPath });
  assert.equal(finalized.finalized, true);
  assert.equal(finalized.submitActionCount, 1);
});
