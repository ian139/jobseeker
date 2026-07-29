import assert from 'node:assert/strict';
import test from 'node:test';

import {
  ComputerIdentityError,
  createComputerUseAdapter,
} from '../src/phase1/computer-use-adapter.mjs';

const HASH_A = 'a'.repeat(64);
const HASH_B = 'b'.repeat(64);

function view(hash) {
  return {
    surfaceId: 'surface-1',
    screenshotPath: `/tmp/${hash}.png`,
    screenshotSha256: hash,
    viewport: { width: 800, height: 600 },
    url: 'https://example.invalid/apply',
    title: 'Application',
  };
}

function observation(hash, previousObservationId = null, observationId = 'observation-1') {
  return {
    schema: 'phase1-visual-observation-v1',
    observation_id: observationId,
    previous_observation_id: previousObservationId,
    observed_at: '2026-01-01T00:00:00.000Z',
    surface: {
      surface_id: 'surface-1',
      url: 'https://example.invalid/apply',
      title: 'Application',
      screenshot_sha256: hash,
      viewport: { width: 800, height: 600 },
    },
    agent: { provider: 'gemini', model: 'vision-test' },
    targets: [{
      target_id: 'target-1',
      field_id: 'field-1',
      group_id: 'group-1',
      kind: 'text',
      label: 'Name',
      description: 'Applicant name',
      bounds: { x: 10, y: 10, width: 300, height: 40 },
      visible: true,
      enabled: true,
      required: true,
      readonly: false,
      value_state: 'blank',
      checked: null,
      selected: null,
      options: [],
      validation: { valid: null, message_present: false },
      file: null,
      candidate: { class: 'field', reason: null },
      confidence: 1,
    }],
    blockers: [],
  };
}

test('captures, analyzes, binds identities, and normalizes computer actions', async () => {
  const calls = [];
  let currentView = view(HASH_A);
  const transport = {
    captureView: async (request) => {
      calls.push(['captureView', request]);
      return currentView;
    },
    analyzeView: async (request) => {
      calls.push(['analyzeView', request]);
      return observation(request.view.screenshotSha256, request.previousObservationId, 'observation-1');
    },
    performAction: async (request) => {
      calls.push(['performAction', request]);
      return { ok: true, text: request.text, filePath: request.filePath };
    },
  };
  const adapter = createComputerUseAdapter(transport, { provider: 'gemini', model: 'vision-test' });
  const result = await adapter.observe();
  assert.equal(result.observation_id, 'observation-1');
  assert.equal(calls[1][1].provider, 'gemini');
  assert.deepEqual(await adapter.performAction({
    action: 'type_text',
    surfaceId: 'surface-1',
    observationId: 'observation-1',
    targetId: 'target-1',
    text: 'private applicant text',
  }), { ok: true });
  assert.equal(calls[2][1].action, 'type_text');
  assert.equal(calls[2][1].targetId, 'target-1');
  await assert.rejects(() => adapter.performAction({
    action: 'navigate',
    surfaceId: 'surface-1',
    observationId: 'observation-1',
  }), /INVALID_ACTION/);

  currentView = view(HASH_B);
  await adapter.captureView({ surfaceId: 'surface-1' });
  await assert.rejects(() => adapter.performAction({
    action: 'click',
    surfaceId: 'surface-1',
    observationId: 'observation-1',
    targetId: 'target-1',
  }), (error) => error instanceof ComputerIdentityError && error.code === 'SCREENSHOT_ID_MISMATCH');
});

test('accepts only Codex or Gemini agents', () => {
  assert.throws(() => createComputerUseAdapter({}), /TRANSPORT_METHOD_UNAVAILABLE|TRANSPORT_REQUIRED/);
  assert.throws(() => createComputerUseAdapter({ captureView() {}, analyzeView() {}, performAction() {} }, { provider: 'other' }), /INVALID_PROVIDER/);
});
