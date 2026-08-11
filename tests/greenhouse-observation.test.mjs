import assert from 'node:assert/strict';
import crypto from 'node:crypto';
import test from 'node:test';

import { canonicalJson } from '../src/phase1/evidence.mjs';
import {
  createLedger,
  digestObservedValue,
  mergeObservation,
  recordActionAttempt,
  recordResolution,
} from '../src/phase1/ledger.mjs';
import { normalizeGreenhouseObservation } from '../src/phase1/greenhouse-observation.mjs';

const GREENHOUSE_URL = 'https://job-boards.greenhouse.io/acme/jobs/12345';
const LEGACY_GREENHOUSE_URL = 'https://boards.greenhouse.io/acme/jobs/12345';

function snapshotSha256(frames, controls, blockers, title, url) {
  return crypto.createHash('sha256')
    .update(canonicalJson({ frames, controls, blockers, title, url }), 'utf8')
    .digest('hex');
}

function greenhouseObservation(sequence, controls, options = {}) {
  const url = options.url ?? GREENHOUSE_URL;
  const id = `obs-${sequence}`;
  return {
    schema: 'phase1-observation-v1',
    observation_id: id,
    previous_observation_id: sequence === 1 ? null : `obs-${sequence - 1}`,
    observed_at: `2026-08-05T08:00:${String(sequence).padStart(2, '0')}.000Z`,
    url,
    title: options.title ?? 'Greenhouse application',
    snapshot_sha256: 'a'.repeat(64),
    frames: [{
      id: 'frame-main',
      parent_id: null,
      url,
      origin: 'https://job-boards.greenhouse.io',
      accessible: true,
    }],
    controls,
    blockers: options.blockers ?? [],
  };
}

function comboboxControl(fieldId, options = {}) {
  return {
    ref: `control-${fieldId}`,
    stable_id: fieldId,
    group_id: null,
    kind: options.kind ?? 'input',
    tag: options.tag ?? 'input',
    type: options.type ?? 'text',
    role: options.role ?? 'combobox',
    label: options.label ?? fieldId,
    name: options.name ?? fieldId,
    description: options.description ?? null,
    locator: { strategy: 'id', value: fieldId, role: 'combobox', name: fieldId },
    frame_id: 'frame-main',
    visible: true,
    enabled: true,
    required: true,
    readonly: false,
    disabled: false,
    value: options.value ?? null,
    value_present: options.valuePresent ?? false,
    checked: null,
    selected: options.selected ?? null,
    options: options.options ?? [],
    validity: options.validity ?? { valid: true, aria_invalid: null, message: null },
    file: null,
    candidate: { class: 'field', reason: 'synthetic custom combobox' },
  };
}
function textInputControl(fieldId, options = {}) {
  return {
    ref: `control-${fieldId}`,
    stable_id: fieldId,
    group_id: null,
    kind: 'input',
    tag: 'input',
    type: 'text',
    role: 'textbox',
    label: options.label ?? fieldId,
    name: options.name ?? fieldId,
    description: options.description ?? null,
    locator: { strategy: 'id', value: fieldId, role: 'textbox', name: fieldId },
    frame_id: 'frame-main',
    visible: true,
    enabled: true,
    required: true,
    readonly: false,
    disabled: false,
    value: options.value ?? null,
    value_present: options.valuePresent ?? false,
    checked: null,
    selected: null,
    options: [],
    validity: options.validity ?? { valid: true, aria_invalid: null, message: null },
    file: null,
    candidate: { class: 'field', reason: 'synthetic text input' },
  };
}

function committedCombobox(fieldId, value = 'Engineering', options = {}) {
  return comboboxControl(fieldId, {
    value,
    valuePresent: true,
    selected: [value],
    validity: { valid: false, aria_invalid: true, message: 'This field is required.' },
    ...options,
  });
}

const STALE_REQUIRED = { valid: false, aria_invalid: true, message: 'This field is required.' };

function answeredLedger(fieldId, value, observation, options = {}) {
  const control = observation.controls.find((item) => item.stable_id === fieldId);
  assert.ok(control, 'control must exist for the answered field');
  let ledger = createLedger(observation);
  ledger = recordResolution(ledger, {
    field_id: fieldId,
    observation_id: observation.observation_id,
    ref: control.ref,
    source: 'profile_verified',
    value_digest: digestObservedValue(control, value),
  });
  if (options.withSelect !== false) {
    ledger = recordActionAttempt(ledger, {
      action: options.action ?? 'select',
      field_id: fieldId,
      observation_id: observation.observation_id,
      ref: control.ref,
      outcome: options.outcome ?? 'succeeded',
      retry_of: null,
      error_code: options.outcome === 'succeeded' ? null : 'synthetic_failure',
    });
  }
  return ledger;
}

function assertDeepFrozen(value, seen = new Set()) {
  if (value === null || typeof value !== 'object' || seen.has(value)) return;
  seen.add(value);
  assert.equal(Object.isFrozen(value), true);
  for (const item of Object.values(value)) assertDeepFrozen(item, seen);
}

test('normalizes a proven stale required error on a committed Greenhouse combobox', () => {
  const control = committedCombobox('department');
  const observation = greenhouseObservation(1, [control]);
  const ledger = answeredLedger('department', 'Engineering', observation);
  const inputFrames = structuredClone(observation.frames);
  const inputControls = structuredClone(observation.controls);
  const inputBlockers = structuredClone(observation.blockers);

  const normalized = normalizeGreenhouseObservation(observation, ledger);

  assert.notEqual(normalized, observation);
  assert.equal(normalized.controls[0].validity.valid, true);
  assert.equal(normalized.controls[0].validity.aria_invalid, false);
  assert.equal(normalized.controls[0].validity.message, null);
  assert.deepEqual(
    normalized.snapshot_sha256,
    snapshotSha256(
      observation.frames,
      normalized.controls,
      observation.blockers,
      observation.title,
      observation.url,
    ),
  );
  assert.notEqual(normalized.snapshot_sha256, observation.snapshot_sha256);
  assert.equal(normalized.observation_id, observation.observation_id);
  assert.equal(normalized.previous_observation_id, observation.previous_observation_id);
  assert.equal(normalized.observed_at, observation.observed_at);
  assert.equal(normalized.url, observation.url);
  assert.equal(normalized.title, observation.title);
  assertDeepFrozen(normalized);

  assert.deepEqual(observation.frames, inputFrames, 'frames must not be mutated');
  assert.deepEqual(observation.controls, inputControls, 'controls must not be mutated');
  assert.deepEqual(observation.blockers, inputBlockers, 'blockers must not be mutated');
  assert.equal(observation.controls[0].validity.valid, false, 'input validity must not be mutated');
  assert.equal(Object.isFrozen(observation), false);
});

test('accepts the legacy boards.greenhouse.io host and recomputes the snapshot hash', () => {
  const observation = greenhouseObservation(1, [committedCombobox('department')], {
    url: LEGACY_GREENHOUSE_URL,
  });
  const ledger = answeredLedger('department', 'Engineering', observation);
  const normalized = normalizeGreenhouseObservation(observation, ledger);
  assert.equal(normalized.controls[0].validity.valid, true);
  assert.deepEqual(
    normalized.snapshot_sha256,
    snapshotSha256(
      observation.frames,
      normalized.controls,
      observation.blockers,
      observation.title,
      observation.url,
    ),
  );
});

test('normalizes through a single committed selected option when the menu is open', () => {
  const control = comboboxControl('department', {
    value: 'Engineering',
    valuePresent: true,
    options: [
      { value: 'eng', label: 'Engineering', disabled: false, selected: true },
      { value: 'fin', label: 'Finance', disabled: false, selected: false },
    ],
    validity: STALE_REQUIRED,
  });
  const observation = greenhouseObservation(1, [control]);
  const ledger = answeredLedger('department', 'Engineering', observation);
  const normalized = normalizeGreenhouseObservation(observation, ledger);
  assert.equal(normalized.controls[0].validity.valid, true);
  assert.equal(normalized.controls[0].validity.message, null);
});

test('normalizes when the successful select proof comes from the supplied attempts', () => {
  const control = committedCombobox('department');
  const observation = greenhouseObservation(1, [control]);
  const ledger = answeredLedger('department', 'Engineering', observation, { withSelect: false });
  const attempts = [{
    action_id: 'action-1',
    action: 'select',
    field_id: 'department',
    observation_id: observation.observation_id,
    ref: control.ref,
    outcome: 'succeeded',
    retry_of: null,
    error_code: null,
  }];
  const normalized = normalizeGreenhouseObservation(observation, ledger, attempts);
  assert.equal(normalized.controls[0].validity.valid, true);
});

test('is idempotent and never mutates its inputs', () => {
  const control = committedCombobox('department');
  const observation = greenhouseObservation(1, [control]);
  const ledger = answeredLedger('department', 'Engineering', observation);
  const first = normalizeGreenhouseObservation(observation, ledger);
  const second = normalizeGreenhouseObservation(first, ledger);
  assert.deepEqual(second, first);
  assert.equal(second.controls[0].validity.valid, true);
  assert.equal(first.controls[0].validity.valid, true);
});

test('leaves non-Greenhouse hosts untouched', () => {
  for (const url of [
    'https://example.invalid/jobs/12345',
    'https://greenhouse.com/jobs/12345',
    'https://job-boards.greenhouse.io.evil.example/jobs/12345',
    'not-a-url',
  ]) {
    const observation = greenhouseObservation(1, [committedCombobox('department')], { url });
    const ledger = answeredLedger('department', 'Engineering', observation);
    const normalized = normalizeGreenhouseObservation(observation, ledger);
    assert.deepEqual(normalized, observation, `must not normalize ${url}`);
    assert.equal(normalized.controls[0].validity.valid, false);
  }
});

test('leaves native select comboboxes untouched', () => {
  const control = comboboxControl('department', {
    kind: 'select',
    tag: 'select',
    type: 'select',
    value: 'eng',
    valuePresent: true,
    selected: ['eng'],
    options: [
      { value: 'eng', label: 'Engineering', disabled: false, selected: true },
      { value: 'fin', label: 'Finance', disabled: false, selected: false },
    ],
    validity: STALE_REQUIRED,
  });
  const observation = greenhouseObservation(1, [control]);
  const ledger = answeredLedger('department', 'eng', observation);
  const normalized = normalizeGreenhouseObservation(observation, ledger);
  assert.deepEqual(normalized, observation);
  assert.equal(normalized.controls[0].validity.valid, false);
});

test('leaves non-input combobox elements untouched', () => {
  const control = comboboxControl('department', {
    kind: 'aria',
    tag: 'div',
    type: null,
    value: 'Engineering',
    valuePresent: true,
    selected: ['Engineering'],
    validity: STALE_REQUIRED,
  });
  const observation = greenhouseObservation(1, [control]);
  const ledger = answeredLedger('department', 'Engineering', observation);
  const normalized = normalizeGreenhouseObservation(observation, ledger);
  assert.deepEqual(normalized, observation);
  assert.equal(normalized.controls[0].validity.valid, false);
});

test('leaves typed queries with no committed selection untouched', () => {
  const control = comboboxControl('department', {
    value: 'Engi',
    valuePresent: true,
    selected: [],
    options: [
      { value: 'eng', label: 'Engineering', disabled: false, selected: false },
    ],
    validity: STALE_REQUIRED,
  });
  const observation = greenhouseObservation(1, [control]);
  const ledger = answeredLedger('department', 'Engineering', observation);
  const normalized = normalizeGreenhouseObservation(observation, ledger);
  assert.deepEqual(normalized, observation);
  assert.equal(normalized.controls[0].validity.valid, false);
});

test('leaves multi-committed selections untouched', () => {
  const control = comboboxControl('department', {
    value: 'Engineering',
    valuePresent: true,
    selected: ['Engineering', 'Finance'],
    validity: STALE_REQUIRED,
  });
  const observation = greenhouseObservation(1, [control]);
  const ledger = answeredLedger('department', 'Engineering', observation);
  const normalized = normalizeGreenhouseObservation(observation, ledger);
  assert.deepEqual(normalized, observation);
  assert.equal(normalized.controls[0].validity.valid, false);
});

test('leaves committed selections whose digest does not match the field untouched', () => {
  const control = committedCombobox('department', 'Finance');
  const observation = greenhouseObservation(1, [control]);
  const ledger = answeredLedger('department', 'Engineering', observation);
  const normalized = normalizeGreenhouseObservation(observation, ledger);
  assert.deepEqual(normalized, observation);
  assert.equal(normalized.controls[0].validity.valid, false);
});

test('leaves controls without a present value untouched', () => {
  const control = comboboxControl('department', {
    value: '',
    valuePresent: false,
    selected: [],
    validity: STALE_REQUIRED,
  });
  const observation = greenhouseObservation(1, [control]);
  const ledger = answeredLedger('department', 'Engineering', observation);
  const normalized = normalizeGreenhouseObservation(observation, ledger);
  assert.deepEqual(normalized, observation);
  assert.equal(normalized.controls[0].validity.valid, false);
});

test('normalizes both spellings of the stale required message', () => {
  for (const message of ['This field is required.', 'This field is required']) {
    const control = committedCombobox('department', 'Engineering', {
      validity: { valid: false, aria_invalid: true, message },
    });
    const observation = greenhouseObservation(1, [control]);
    const ledger = answeredLedger('department', 'Engineering', observation);
    const normalized = normalizeGreenhouseObservation(observation, ledger);
    assert.equal(normalized.controls[0].validity.valid, true);
    assert.equal(normalized.controls[0].validity.message, null);
  }
});

test('leaves genuine custom errors and other validity states untouched', () => {
  const otherMessages = [
    'This field is required. Please choose a valid option.',
    'Please select an option.',
    'The selected option is no longer available.',
    'Please enter a value.',
  ];
  const validityStates = [
    { valid: true, aria_invalid: null, message: null },
    { valid: true, aria_invalid: false, message: 'This field is required.' },
    { valid: false, aria_invalid: null, message: 'This field is required.' },
    { valid: false, aria_invalid: false, message: 'This field is required.' },
  ];
  for (const validity of [...otherMessages.map((message) => ({ valid: false, aria_invalid: true, message })), ...validityStates]) {
    const control = committedCombobox('department', 'Engineering', { validity });
    const observation = greenhouseObservation(1, [control]);
    const ledger = answeredLedger('department', 'Engineering', observation);
    const normalized = normalizeGreenhouseObservation(observation, ledger);
    assert.deepEqual(normalized, observation, `must not normalize ${JSON.stringify(validity)}`);
    assert.deepEqual(normalized.controls[0].validity, validity);
  }
});

test('leaves unresolved and blank fields untouched', () => {
  const control = committedCombobox('department');
  const observation = greenhouseObservation(1, [control]);

  const unresolved = createLedger(observation);
  let normalized = normalizeGreenhouseObservation(observation, unresolved);
  assert.deepEqual(normalized, observation);
  assert.equal(normalized.controls[0].validity.valid, false);
  assert.equal(
    unresolved.fields.find((field) => field.field_id === 'department').answer_state,
    'unresolved',
  );

  const blank = recordResolution(createLedger(observation), {
    field_id: 'department',
    observation_id: observation.observation_id,
    ref: control.ref,
    source: 'profile_verified',
    semantic_choice: 'not_applicable',
  });
  normalized = normalizeGreenhouseObservation(observation, blank);
  assert.deepEqual(normalized, observation);
  assert.equal(normalized.controls[0].validity.valid, false);
  assert.equal(
    blank.fields.find((field) => field.field_id === 'department').answer_state,
    'blank',
  );
});

test('leaves fields without a successful select action untouched', () => {
  const control = committedCombobox('department');
  const observation = greenhouseObservation(1, [control]);
  let ledger = createLedger(observation);
  ledger = recordResolution(ledger, {
    field_id: 'department',
    observation_id: observation.observation_id,
    ref: control.ref,
    source: 'profile_verified',
    value_digest: digestObservedValue(control, 'Engineering'),
  });
  ledger = recordActionAttempt(ledger, {
    action: 'fill',
    field_id: 'department',
    observation_id: observation.observation_id,
    ref: control.ref,
    outcome: 'succeeded',
    error_code: null,
  });
  const normalized = normalizeGreenhouseObservation(observation, ledger);
  assert.deepEqual(normalized, observation);
  assert.equal(normalized.controls[0].validity.valid, false);
});

test('leaves failed and other-field select actions untouched', () => {
  const control = committedCombobox('department');
  const other = committedCombobox('location', 'Remote', {
    validity: { valid: true, aria_invalid: null, message: null },
  });
  const observation = greenhouseObservation(1, [control, other]);
  let ledger = createLedger(observation);
  for (const [fieldId, value, target] of [
    ['department', 'Engineering', control],
    ['location', 'Remote', other],
  ]) {
    ledger = recordResolution(ledger, {
      field_id: fieldId,
      observation_id: observation.observation_id,
      ref: target.ref,
      source: 'profile_verified',
      value_digest: digestObservedValue(target, value),
    });
  }

  let normalized = normalizeGreenhouseObservation(observation, ledger);
  assert.deepEqual(normalized, observation, 'successful select for another field must not normalize');

  const failedLedger = recordActionAttempt(ledger, {
    action: 'select',
    field_id: 'department',
    observation_id: observation.observation_id,
    ref: control.ref,
    outcome: 'failed',
    error_code: 'synthetic_failure',
  });
  normalized = normalizeGreenhouseObservation(observation, failedLedger);
  assert.deepEqual(normalized, observation);
  assert.equal(normalized.controls[0].validity.valid, false);
});

test('leaves observations with blockers untouched', () => {
  const control = committedCombobox('department');
  const observation = greenhouseObservation(1, [control], {
    blockers: [{ code: 'captcha', label: null, frame_id: null, visible: true }],
  });
  const ledger = answeredLedger('department', 'Engineering', observation);
  const normalized = normalizeGreenhouseObservation(observation, ledger);
  assert.deepEqual(normalized, observation);
  assert.equal(normalized.controls[0].validity.valid, false);
  assert.equal(normalized.blockers.length, 1);
});

test('leaves phone-country fields untouched', () => {
  const control = committedCombobox('phone_country', 'United States', {
    label: 'Phone country',
    description: 'Country for the phone number',
  });
  const observation = greenhouseObservation(1, [control]);
  const ledger = answeredLedger('phone_country', 'United States', observation);
  const normalized = normalizeGreenhouseObservation(observation, ledger);
  assert.deepEqual(normalized, observation);
  assert.equal(normalized.controls[0].validity.valid, false);
});

test('normalizes only the matching control and preserves every other control', () => {
  const department = committedCombobox('department');
  const location = comboboxControl('location', {
    value: 'Remote',
    valuePresent: true,
    selected: ['Remote'],
    validity: STALE_REQUIRED,
  });
  const first = greenhouseObservation(1, [department, location]);
  let ledger = createLedger(first);
  ledger = recordResolution(ledger, {
    field_id: 'department',
    observation_id: first.observation_id,
    ref: department.ref,
    source: 'profile_verified',
    value_digest: digestObservedValue(department, 'Engineering'),
  });
  ledger = recordActionAttempt(ledger, {
    action: 'select',
    field_id: 'department',
    observation_id: first.observation_id,
    ref: department.ref,
    outcome: 'succeeded',
    error_code: null,
  });

  const second = greenhouseObservation(2, [department, location]);
  ledger = mergeObservation(ledger, second);
  ledger = recordResolution(ledger, {
    field_id: 'location',
    observation_id: second.observation_id,
    ref: location.ref,
    source: 'profile_verified',
    value_digest: digestObservedValue(location, 'Remote'),
  });

  const normalized = normalizeGreenhouseObservation(second, ledger);
  assert.equal(normalized.controls[0].validity.valid, true);
  assert.deepEqual(normalized.controls[1].validity, STALE_REQUIRED);
  assert.equal(normalized.controls[1].value, 'Remote');
});

test('returns a frozen clone even when nothing is normalized', () => {
  const control = committedCombobox('department');
  const observation = greenhouseObservation(1, [control]);
  const ledger = createLedger(observation);
  const normalized = normalizeGreenhouseObservation(observation, ledger);
  assert.notEqual(normalized, observation);
  assertDeepFrozen(normalized);
  assert.deepEqual(normalized, observation);
});

test('requires a valid observation and ledger and rejects invalid inputs', () => {
  const control = committedCombobox('department');
  const observation = greenhouseObservation(1, [control]);
  const ledger = answeredLedger('department', 'Engineering', observation);
  const broken = { ...observation, snapshot_sha256: 'not-a-digest' };
  assert.throws(() => normalizeGreenhouseObservation(broken, ledger));
  const brokenLedger = { ...ledger, fields: 'not-an-array' };
  assert.throws(() => normalizeGreenhouseObservation(observation, brokenLedger));
});
test('normalizes a label-derived stale required message on a committed Greenhouse combobox', () => {
  const value = 'University of Massachusetts - Amherst';
  const control = committedCombobox('school', value, {
    label: 'School*',
    description: 'School is required.',
    validity: { valid: false, aria_invalid: true, message: 'School is required.' },
  });
  const observation = greenhouseObservation(1, [control]);
  const ledger = answeredLedger('school', value, observation);
  const normalized = normalizeGreenhouseObservation(observation, ledger);
  assert.equal(normalized.controls[0].validity.valid, true);
  assert.equal(normalized.controls[0].validity.aria_invalid, false);
  assert.equal(normalized.controls[0].validity.message, null);
});

test('normalizes a description prompt stale validity message on a committed Greenhouse combobox', () => {
  const value = '+1';
  const control = committedCombobox('country', value, {
    label: 'Country*',
    description: 'Select a country',
    validity: { valid: false, aria_invalid: true, message: 'Select a country' },
    options: [],
  });
  const observation = greenhouseObservation(1, [control]);
  const ledger = answeredLedger('country', value, observation);
  const normalized = normalizeGreenhouseObservation(observation, ledger);
  assert.equal(normalized.controls[0].validity.valid, true);
  assert.equal(normalized.controls[0].validity.aria_invalid, false);
  assert.equal(normalized.controls[0].validity.message, null);
});
test('normalizes a label-derived stale required message on a filled Greenhouse text input', () => {
  const value = 'ianrapko@gmail.com';
  const control = textInputControl('email', {
    label: 'Email',
    value,
    valuePresent: true,
    validity: { valid: false, aria_invalid: true, message: 'Email is required.' },
  });
  const observation = greenhouseObservation(1, [control]);
  const ledger = answeredLedger('email', value, observation, { withSelect: false });
  const normalized = normalizeGreenhouseObservation(observation, ledger);
  assert.equal(normalized.controls[0].validity.valid, true);
  assert.equal(normalized.controls[0].validity.aria_invalid, false);
  assert.equal(normalized.controls[0].validity.message, null);
});

test('leaves a label-derived required message untouched when no value is committed', () => {
  const control = comboboxControl('school', {
    label: 'School*',
    description: 'School is required.',
    validity: { valid: false, aria_invalid: true, message: 'School is required.' },
    value: '',
    valuePresent: false,
    selected: [],
    options: [],
  });
  const observation = greenhouseObservation(1, [control]);
  const ledger = answeredLedger('school', 'University of Massachusetts - Amherst', observation);
  const normalized = normalizeGreenhouseObservation(observation, ledger);
  assert.deepEqual(normalized, observation);
  assert.equal(normalized.controls[0].validity.valid, false);
});
