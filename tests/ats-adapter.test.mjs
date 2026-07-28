import assert from 'node:assert/strict';
import test from 'node:test';

import {
  createATSAdapter,
  normalizeATSControl,
  normalizeATSResult,
} from '../src/phase1/ats-adapter.mjs';
import { createBrowserAdapter } from '../src/phase1/browser-adapter.mjs';


test('normalizes wrapped ATS results using the outer result kind', () => {
  assert.deepEqual(
    normalizeATSResult({ kind: 'validation', result: { valid: true } }),
    {
      schema: 'phase1-ats-adapter-v1-result',
      kind: 'validation',
      valid: true,
    },
  );
  assert.throws(
    () => normalizeATSResult({ kind: 'navigation', result: { kind: 'validation', valid: true } }),
    (error) => error.code === 'RESULT_KIND_MISMATCH',
  );
});

test('dispatches normalized controls and actions through the browser boundary', async () => {
  const calls = [];
  const transport = {
    resolveControl: async (request) => {
      calls.push(['resolveControl', request]);
      return { resolved: true };
    },
    performAction: async (request) => {
      calls.push(['performAction', request]);
      return { acted: true };
    },
  };
  const browserAdapter = createBrowserAdapter(transport);
  const ats = createATSAdapter(browserAdapter);
  const control = normalizeATSControl({
    ref: 'control-1',
    stable_id: 'field-1',
    type: 'email',
  });
  assert.equal(control.controlType, 'email');
  assert.deepEqual(await ats.resolveControl(control), { resolved: true });
  assert.deepEqual(
    await ats.performControlAction(
      control,
      { action: 'fill_text', value: 'example' },
      { controlReference: 'other-control', observationId: 'other-observation' },
    ),
    { acted: true },
  );
  assert.deepEqual(calls.map(([name]) => name), ['resolveControl', 'performAction']);
  assert.equal(calls.at(-1)[1].controlReference, 'control-1');
  await assert.rejects(
    () => ats.performControlAction(
      control,
      { action: 'fill_text', value: 'example', controlReference: 'other-control' },
    ),
    (error) => error.code === 'CONTROL_REFERENCE_MISMATCH',
  );
});
