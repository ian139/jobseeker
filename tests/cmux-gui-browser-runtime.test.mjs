import assert from 'node:assert/strict';
import test from 'node:test';
import os from 'node:os';
import path from 'node:path';

import {
  CMUX_GUI_BROWSER_PROFILE_MODE,
  CMUX_GUI_BROWSER_RUNTIME_SCHEMA,
  CmuxGuiBrowserRuntimeError,
  createCmuxGuiBrowserBinding,
  createCmuxGuiBrowserTransport,
  validateCmuxGuiBrowserBinding,
  validateCmuxGuiBrowserResult,
} from '../src/phase1/cmux-gui-browser-runtime.mjs';
import {
  BrowserAdapterError,
  BrowserIdentityError,
  BrowserTransportError,
} from '../src/phase1/browser-adapter.mjs';
import {
  OMP_BROWSER_ADAPTER_SCHEMA,
  OmpBrowserAdapter,
  createOmpBrowserAdapter,
} from '../src/phase1/omp-browser-adapter.mjs';

const VALID_WINDOW_ID = '11111111-1111-4111-8111-111111111111';
const VALID_WORKSPACE_ID = '22222222-2222-4222-8222-222222222222';
const VALID_SURFACE_ID = '33333333-3333-4333-8333-333333333333';
const VALID_SOCKET_PATH = path.join(os.homedir(), '.local', 'state', 'cmux', 'cmux-501.sock');

function validBindingInput(overrides = {}) {
  return {
    windowId: VALID_WINDOW_ID,
    workspaceId: VALID_WORKSPACE_ID,
    surfaceId: VALID_SURFACE_ID,
    socketPath: VALID_SOCKET_PATH,
    profileMode: 'persistent',
    ...overrides,
  };
}

test('binding validation accepts valid input and returns frozen clone', () => {
  const input = validBindingInput();
  const binding = createCmuxGuiBrowserBinding(input);
  assert.equal(binding.windowId, VALID_WINDOW_ID);
  assert.equal(binding.workspaceId, VALID_WORKSPACE_ID);
  assert.equal(binding.surfaceId, VALID_SURFACE_ID);
  assert.equal(binding.socketPath, VALID_SOCKET_PATH);
  assert.equal(binding.profileMode, 'persistent');
  assert.ok(Object.isFrozen(binding));

  // Default profileMode
  const inputWithoutMode = {
    windowId: VALID_WINDOW_ID,
    workspaceId: VALID_WORKSPACE_ID,
    surfaceId: VALID_SURFACE_ID,
    socketPath: VALID_SOCKET_PATH,
  };
  const bindingDefault = validateCmuxGuiBrowserBinding(inputWithoutMode);
  assert.equal(bindingDefault.profileMode, CMUX_GUI_BROWSER_PROFILE_MODE);
});

test('binding validation rejects invalid binding shapes and parameters', () => {
  // Non-record
  assert.throws(
    () => validateCmuxGuiBrowserBinding(null),
    (err) => err instanceof CmuxGuiBrowserRuntimeError && err.message === 'INVALID_BINDING'
  );

  // Unknown key
  assert.throws(
    () => validateCmuxGuiBrowserBinding(validBindingInput({ cdpUrl: 'http://127.0.0.1:9222' })),
    (err) => err instanceof CmuxGuiBrowserRuntimeError && err.message === 'INVALID_BINDING'
  );

  // Invalid windowId
  assert.throws(
    () => validateCmuxGuiBrowserBinding(validBindingInput({ windowId: 'not-a-uuid' })),
    (err) => err instanceof CmuxGuiBrowserRuntimeError && err.message === 'INVALID_WINDOW_ID'
  );

  // Missing workspaceId
  const missingWorkspace = validBindingInput();
  delete missingWorkspace.workspaceId;
  assert.throws(
    () => validateCmuxGuiBrowserBinding(missingWorkspace),
    (err) => err instanceof CmuxGuiBrowserRuntimeError && err.message === 'INVALID_WORKSPACE_ID'
  );

  // Invalid surfaceId
  assert.throws(
    () => validateCmuxGuiBrowserBinding(validBindingInput({ surfaceId: '12345' })),
    (err) => err instanceof CmuxGuiBrowserRuntimeError && err.message === 'INVALID_SURFACE_ID'
  );

  // Invalid socketPath (relative)
  assert.throws(
    () => validateCmuxGuiBrowserBinding(validBindingInput({ socketPath: 'tmp/cmux.sock' })),
    (err) => err instanceof CmuxGuiBrowserRuntimeError && err.message === 'INVALID_SOCKET_PATH'
  );

  // Invalid socketPath (root)
  assert.throws(
    () => validateCmuxGuiBrowserBinding(validBindingInput({ socketPath: '/' })),
    (err) => err instanceof CmuxGuiBrowserRuntimeError && err.message === 'INVALID_SOCKET_PATH'
  );

  // Socket paths are restricted to the current owner's CMUX state directory.
  assert.throws(
    () => validateCmuxGuiBrowserBinding(validBindingInput({ socketPath: '/tmp/cmux-501.sock' })),
    (err) => err instanceof CmuxGuiBrowserRuntimeError && err.message === 'INVALID_SOCKET_PATH'
  );

  // Ephemeral profile mode forbidden
  assert.throws(
    () => validateCmuxGuiBrowserBinding(validBindingInput({ profileMode: 'ephemeral' })),
    (err) => err instanceof CmuxGuiBrowserRuntimeError && err.message === 'EPHEMERAL_PROFILE_FORBIDDEN'
  );

  // Unknown profile mode
  assert.throws(
    () => validateCmuxGuiBrowserBinding(validBindingInput({ profileMode: 'temporary' })),
    (err) => err instanceof CmuxGuiBrowserRuntimeError && err.message === 'INVALID_BINDING'
  );
});

test('request and result identity fencing injects identity into transport calls', async () => {
  const binding = createCmuxGuiBrowserBinding(validBindingInput());
  let capturedRequest = null;
  const mockTransport = {
    async request(method, req) {
      capturedRequest = req;
      return { status: 'ok', windowId: VALID_WINDOW_ID, workspaceId: VALID_WORKSPACE_ID, surfaceId: VALID_SURFACE_ID };
    },
  };

  const runtimeTransport = createCmuxGuiBrowserTransport(mockTransport, binding);
  const res = await runtimeTransport.request('observePage', { observationId: 'obs-1' });

  assert.equal(capturedRequest.windowId, VALID_WINDOW_ID);
  assert.equal(capturedRequest.workspaceId, VALID_WORKSPACE_ID);
  assert.equal(capturedRequest.surfaceId, VALID_SURFACE_ID);
  assert.equal(capturedRequest.observationId, 'obs-1');
  assert.equal(res.status, 'ok');
});

test('recursive identity mismatch detection rejects mismatched IDs in results', async () => {
  const binding = createCmuxGuiBrowserBinding(validBindingInput());

  // Valid recursive payload
  const validPayload = {
    frames: [
      { id: 'f1', windowId: VALID_WINDOW_ID, workspaceId: VALID_WORKSPACE_ID, surfaceId: VALID_SURFACE_ID },
    ],
  };
  const verified = validateCmuxGuiBrowserResult(validPayload, binding);
  assert.equal(verified.frames[0].id, 'f1');

  // Window identity mismatch inside array
  const mismatchedWindowPayload = {
    frames: [
      { id: 'f1', windowId: '99999999-9999-4999-8999-999999999999' },
    ],
  };
  assert.throws(
    () => validateCmuxGuiBrowserResult(mismatchedWindowPayload, binding),
    (err) => err instanceof CmuxGuiBrowserRuntimeError && err.message === 'WINDOW_IDENTITY_MISMATCH'
  );

  // Workspace identity mismatch
  const mismatchedWorkspacePayload = {
    nested: { workspaceId: '99999999-9999-4999-8999-999999999999' },
  };
  assert.throws(
    () => validateCmuxGuiBrowserResult(mismatchedWorkspacePayload, binding),
    (err) => err instanceof CmuxGuiBrowserRuntimeError && err.message === 'WORKSPACE_IDENTITY_MISMATCH'
  );

  // Surface identity mismatch
  const mismatchedSurfacePayload = {
    surfaceId: '99999999-9999-4999-8999-999999999999',
  };
  assert.throws(
    () => validateCmuxGuiBrowserResult(mismatchedSurfacePayload, binding),
    (err) => err instanceof CmuxGuiBrowserRuntimeError && err.message === 'SURFACE_IDENTITY_MISMATCH'
  );
});

test('OmpBrowserAdapter observation fencing and identity exposure', async () => {
  const binding = validBindingInput();
  let callCount = 0;
  const makeObservation = (id, previous = null) => ({
    schema: 'phase1-observation-v1',
    observation_id: id,
    previous_observation_id: previous,
    observed_at: '2026-08-09T00:00:00.000Z',
    url: 'https://example.com',
    title: 'Test Page',
    snapshot_sha256: 'a'.repeat(64),
    frames: [{ id: 'f1', parent_id: null, url: 'https://example.com', origin: 'https://example.com', accessible: true }],
    controls: [],
    blockers: [],
  });

  const mockTransport = {
    async observePage(req) {
      callCount++;
      if (callCount === 1) return { observation: makeObservation('obs-100', null), workspaceId: VALID_WORKSPACE_ID };
      return { observation: makeObservation('obs-101', 'obs-100'), workspaceId: VALID_WORKSPACE_ID };
    },
  };

  const adapter = createOmpBrowserAdapter(mockTransport, binding);
  assert.equal(adapter.windowId, VALID_WINDOW_ID);
  assert.equal(adapter.workspaceId, VALID_WORKSPACE_ID);
  assert.equal(adapter.surfaceId, VALID_SURFACE_ID);
  assert.equal(adapter.socketPath, VALID_SOCKET_PATH);
  assert.equal(adapter.profileMode, 'persistent');
  assert.deepEqual(adapter.identity, {
    windowId: VALID_WINDOW_ID,
    workspaceId: VALID_WORKSPACE_ID,
    surfaceId: VALID_SURFACE_ID,
  });

  const obs1 = await adapter.observePage();
  assert.equal(obs1.observation_id, 'obs-100');
  assert.equal(adapter.observationId, 'obs-100');

  const obs2 = await adapter.observePage({ observationId: 'obs-100' });
  assert.equal(obs2.observation_id, 'obs-101');
  assert.equal(adapter.observationId, 'obs-101');

  // Observation fencing mismatch
  await assert.rejects(
    () => adapter.observePage({ observationId: 'obs-wrong' }),
    (err) => err instanceof BrowserAdapterError && err.message === 'OBSERVATION_ID_MISMATCH'
  );
});

test('closeTarget target-only close semantics', async () => {
  const binding = validBindingInput();
  let closeCalled = false;
  let closeRequestPayload = null;

  const mockTransport = {
    async closeTarget(req) {
      closeCalled = true;
      closeRequestPayload = req;
      return { closed: true, surfaceId: VALID_SURFACE_ID };
    },
  };

  const adapter = createOmpBrowserAdapter(mockTransport, binding);
  const result = await adapter.closeTarget({ reason: 'job_completed' });

  assert.ok(closeCalled);
  assert.equal(closeRequestPayload.windowId, VALID_WINDOW_ID);
  assert.equal(closeRequestPayload.workspaceId, VALID_WORKSPACE_ID);
  assert.equal(closeRequestPayload.surfaceId, VALID_SURFACE_ID);
  assert.equal(closeRequestPayload.reason, 'job_completed');
  assert.equal(result.closed, true);

  // Missing closeTarget handler
  const noCloseTransport = {
    async observePage(req) {
      return { observation_id: 'obs-1', previous_observation_id: null, controls: [] };
    },
  };
  const adapterNoClose = createOmpBrowserAdapter(noCloseTransport, binding);
  await assert.rejects(
    () => adapterNoClose.closeTarget(),
    (err) => err instanceof CmuxGuiBrowserRuntimeError && err.message === 'TARGET_CLOSE_UNAVAILABLE'
  );
});
