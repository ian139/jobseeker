import assert from 'node:assert/strict';
import test from 'node:test';

import {
  ANSWER_SOURCES,
  createLedger,
  digestPrivateValue,
  markFieldSensitive,
  mergeObservation,
  recordActionAttempt,
  recordActionBatch,
  recordResolution,
  validateLedger,
  validateObservation,
  verifyRetention,
} from '../src/phase1/ledger.mjs';
import { auditCompletion } from '../src/phase1/audit.mjs';

const SNAPSHOT = 'a'.repeat(64);
const UPLOAD_DIGEST = 'b'.repeat(64);

function frame() {
  return {
    id: 'frame-main',
    parent_id: null,
    url: 'https://example.test/app',
    origin: 'https://example.test',
    accessible: true,
  };
}

function control(stableId, overrides = {}) {
  return {
    ref: `ref-${stableId}`,
    stable_id: stableId,
    group_id: null,
    kind: 'text',
    tag: 'input',
    type: 'text',
    role: 'textbox',
    label: `Question ${stableId}`,
    name: stableId,
    description: null,
    locator: null,
    frame_id: 'frame-main',
    visible: true,
    enabled: true,
    required: true,
    readonly: false,
    disabled: false,
    value: `value-${stableId}`,
    value_present: true,
    checked: null,
    selected: null,
    options: [],
    validity: { valid: true, aria_invalid: null, message: null },
    file: null,
    candidate: { class: 'field', reason: 'visible user-facing field control' },
    ...overrides,
  };
}

function finalCandidate() {
  return control('final-review', {
    ref: 'ref-final-review',
    stable_id: 'final-review',
    kind: 'button',
    tag: 'button',
    type: 'button',
    role: 'button',
    label: 'Review application',
    name: null,
    required: false,
    value: null,
    value_present: false,
    candidate: { class: 'final_candidate', reason: 'ready for OMP submission' },
  });
}

function nonFinalCandidate(id = 'next-page', ref = `ref-${id}`) {
  return control(id, {
    ref,
    stable_id: id,
    kind: 'button',
    tag: 'button',
    type: 'button',
    role: 'button',
    label: 'Continue',
    name: null,
    required: false,
    value: null,
    value_present: false,
    candidate: { class: 'non_final_navigation', reason: 'continue to the next page' },
  });
}

function blocker(code) {
  return {
    code,
    label: code,
    frame_id: 'frame-main',
    visible: true,
  };
}

function observation(id, controls, previous = null, blockers = []) {
  return {
    schema: 'phase1-observation-v1',
    observation_id: id,
    previous_observation_id: previous,
    observed_at: '2026-07-24T00:00:00.000Z',
    url: 'https://example.test/app',
    title: 'Synthetic application',
    snapshot_sha256: SNAPSHOT,
    frames: [frame()],
    controls,
    blockers,
  };
}

function fieldById(ledger, fieldId) {
  const field = ledger.fields.find((item) => item.field_id === fieldId);
  assert.ok(field, `missing field ${fieldId}`);
  return field;
}

function answer(ledger, fieldId, observationId, ref, value, extra = {}) {
  return recordResolution(ledger, {
    field_id: fieldId,
    observation_id: observationId,
    ref,
    source: 'user',
    value_digest: digestPrivateValue(value),
    ...extra,
  });
}

function fileControl(id = 'resume-file', overrides = {}) {
  return control(id, {
    ref: `ref-${id}`,
    stable_id: id,
    kind: 'input',
    tag: 'input',
    type: 'file',
    role: 'button',
    value: null,
    value_present: true,
    file: {
      accept: ['application/pdf'],
      count: 1,
      names: ['resume.pdf'],
      committed_method: 'native_file_list',
    },
    ...overrides,
  });
}

test('carries the active blocker set across persistent observations', () => {
  const first = observation('obs-1', [control('alpha'), finalCandidate()], null, [
    blocker('captcha'),
    blocker('access_control'),
  ]);
  let ledger = createLedger(first);
  assert.deepEqual(ledger.active_blockers, ['access_control', 'captcha']);
  assert.deepEqual(ledger.diffs[0].blockers_added, ['access_control', 'captcha']);
  assert.deepEqual(ledger.diffs[0].blockers_removed, []);

  const second = observation('obs-2', [control('alpha'), finalCandidate()], 'obs-1', [blocker('captcha')]);
  ledger = mergeObservation(ledger, second);
  assert.deepEqual(ledger.active_blockers, ['captcha']);
  assert.deepEqual(ledger.diffs[1].blockers_added, []);
  assert.deepEqual(ledger.diffs[1].blockers_removed, ['access_control']);

  const third = observation('obs-3', [control('alpha'), finalCandidate()], 'obs-2', [
    blocker('access_control'),
    blocker('captcha'),
  ]);
  ledger = mergeObservation(ledger, third);
  assert.deepEqual(ledger.active_blockers, ['access_control', 'captcha']);
  assert.deepEqual(ledger.diffs[2].blockers_added, ['access_control']);
  assert.deepEqual(ledger.diffs[2].blockers_removed, []);
  validateLedger(ledger);
});

test('persists candidate classes and rejects targetless, stale, and mismatched actions', () => {
  const first = observation('obs-1', [
    control('alpha'),
    nonFinalCandidate(),
    control('disabled-page', {
      ref: 'ref-disabled-page',
      stable_id: 'disabled-page',
      kind: 'button',
      tag: 'button',
      type: 'button',
      role: 'button',
      label: 'Disabled continue',
      name: null,
      required: false,
      value: null,
      value_present: false,
      disabled: true,
      candidate: { class: 'non_final_navigation', reason: 'disabled continuation' },
    }),
    finalCandidate(),
  ]);
  let ledger = createLedger(first);
  assert.deepEqual(ledger.current_candidate_refs.map((item) => item.class), [
    'final_candidate',
    'non_final_navigation',
  ]);
  assert.equal(
    ledger.current_candidate_refs.some((item) => item.stable_id === 'disabled-page'),
    false,
  );

  assert.throws(() => recordActionAttempt(ledger, {
    action: 'fill',
    observation_id: 'obs-1',
  }), /field_id and ref/i);
  assert.throws(() => recordActionAttempt(ledger, {
    action: 'fill',
    field_id: 'alpha',
    observation_id: 'obs-1',
  }), /field_id and ref/i);
  assert.throws(() => recordActionAttempt(ledger, {
    action: 'fill',
    field_id: 'alpha',
    ref: 'ref-wrong',
    observation_id: 'obs-1',
  }), /current|bound|stale/i);
  assert.throws(() => recordActionAttempt(ledger, {
    action: 'non_final_navigation',
    observation_id: 'obs-1',
  }), /candidate ref|ref/i);
  assert.throws(() => recordActionAttempt(ledger, {
    action: 'non_final_navigation',
    ref: 'ref-final-review',
    observation_id: 'obs-1',
  }), /non-final|candidate/i);
  assert.throws(() => recordActionAttempt(ledger, {
    action: 'non_final_navigation',
    field_id: 'alpha',
    ref: 'ref-next-page',
    observation_id: 'obs-1',
  }), /navigation|field/i);

  ledger = recordActionAttempt(ledger, {
    action: 'fill',
    field_id: 'alpha',
    ref: 'ref-alpha',
    observation_id: 'obs-1',
    outcome: 'attempted',
  });
  ledger = recordActionAttempt(ledger, {
    action: 'non_final_navigation',
    ref: 'ref-next-page',
    observation_id: 'obs-1',
    outcome: 'succeeded',
  });
  ledger = recordActionAttempt(ledger, {
    action: 'scroll',
    observation_id: 'obs-1',
    outcome: 'succeeded',
  });
  assert.equal(ledger.action_attempts.at(-1).stale_ref, false);

  const second = observation('obs-2', [
    control('alpha', { ref: 'ref-alpha-new' }),
    nonFinalCandidate('next-page', 'ref-next-page-new'),
    finalCandidate(),
  ], 'obs-1');
  ledger = mergeObservation(ledger, second);
  assert.throws(() => recordActionAttempt(ledger, {
    action: 'fill',
    field_id: 'alpha',
    ref: 'ref-alpha',
    observation_id: 'obs-1',
  }), /current|bound|stale/i);
  assert.throws(() => recordActionAttempt(ledger, {
    action: 'non_final_navigation',
    ref: 'ref-next-page',
    observation_id: 'obs-2',
  }), /current|candidate|stale/i);
  validateLedger(ledger);
});

test('records clear as a canonical field mutation', () => {
  const first = observation('obs-1', [control('alpha', { value: 'answer' }), finalCandidate()]);
  const ledger = recordActionAttempt(createLedger(first), {
    action: 'clear',
    field_id: 'alpha',
    ref: 'ref-alpha',
    observation_id: 'obs-1',
    outcome: 'succeeded',
  });
  assert.equal(ledger.action_attempts.at(-1).action, 'clear');
  assert.equal(fieldById(ledger, 'alpha').retained, false);
  assert.equal(fieldById(ledger, 'alpha').valid, false);
});
test('fill batches preserve succeeded retry ancestry and terminal stale outcomes', () => {
  const current = observation('obs-1', [
    control('alpha'),
    control('beta'),
    finalCandidate(),
  ]);
  const ledger = recordActionBatch(createLedger(current), [
    {
      action_id: 'retry-success',
      action: 'fill',
      field_id: 'alpha',
      ref: 'ref-alpha',
      observation_id: 'obs-1',
      outcome: 'succeeded',
      retry_of: 0,
      error_code: null,
    },
    {
      action_id: 'stale-stop',
      action: 'fill',
      field_id: 'beta',
      ref: 'ref-beta',
      observation_id: 'obs-1',
      outcome: 'stale',
      retry_of: null,
      error_code: 'stale_reference',
    },
  ]);
  assert.equal(ledger.action_attempts[0].retry_of, 0);
  assert.equal(ledger.action_attempts[1].outcome, 'stale');
});

test('invalidates mutation retention until a newer chained observation verifies it', () => {
  const first = observation('obs-1', [control('alpha', { value: 'answer' }), finalCandidate()]);
  let ledger = answer(createLedger(first), 'alpha', 'obs-1', 'ref-alpha', 'answer');
  let result = verifyRetention(ledger, first);
  assert.equal(result.ok, true);
  ledger = result.ledger;
  assert.equal(fieldById(ledger, 'alpha').retained, true);

  ledger = recordActionAttempt(ledger, {
    action: 'type',
    field_id: 'alpha',
    ref: 'ref-alpha',
    observation_id: 'obs-1',
    outcome: 'attempted',
  });
  assert.equal(fieldById(ledger, 'alpha').retained, false);
  assert.equal(fieldById(ledger, 'alpha').valid, false);
  result = verifyRetention(ledger, first);
  assert.equal(result.ok, false);
  assert.ok(result.errors.some((item) => item.code === 'MUTATION_PENDING'));

  const second = observation('obs-2', [control('alpha', { value: 'answer' }), finalCandidate()], 'obs-1');
  ledger = mergeObservation(ledger, second);
  result = verifyRetention(ledger, second);
  assert.equal(result.ok, true);
  assert.equal(fieldById(result.ledger, 'alpha').retained, true);
  assert.equal(fieldById(result.ledger, 'alpha').valid, true);
});

test('requires typed upload proof and rejects injection, ordinary overrides, and disappearance', () => {
  const first = observation('obs-1', [fileControl(), control('ordinary'), finalCandidate()]);
  let ledger = createLedger(first);
  const fileDigest = digestPrivateValue('resolved-resume');
  ledger = recordActionAttempt(ledger, {
    action: 'upload',
    field_id: 'resume-file',
    ref: 'ref-resume-file',
    observation_id: 'obs-1',
    outcome: 'succeeded',
    action_id: 'upload-1',
    source_sha256: UPLOAD_DIGEST,
  });
  ledger = answer(ledger, 'resume-file', 'obs-1', 'ref-resume-file', 'resolved-resume', { source: 'resume' });
  ledger = answer(ledger, 'ordinary', 'obs-1', 'ref-ordinary', 'value-ordinary');

  const second = observation('obs-2', [fileControl(), control('ordinary'), finalCandidate()], 'obs-1');
  ledger = mergeObservation(ledger, second);
  let result = verifyRetention(ledger, second);
  assert.equal(result.ok, false);
  assert.ok(result.errors.some((item) => item.code === 'INVALID_PROOF'));

  const validProof = {
    field_id: 'resume-file',
    value_digest: fileDigest,
    action_id: 'upload-1',
    file_name: 'resume.pdf',
    source_sha256: UPLOAD_DIGEST,
    observation_id: 'obs-2',
    container_identity: 'resume-file',
    committed_method: 'native_file_list',
  };
  result = verifyRetention(ledger, second, { 'resume-file': validProof });
  assert.equal(result.ok, true);
  ledger = result.ledger;
  assert.equal(fieldById(ledger, 'resume-file').retained, true);

  const unrelated = observation('obs-3', [
    fileControl(),
    control('ordinary', {
      ref: 'ref-ordinary-updated',
      value: 'unrelated-update',
      value_present: true,
    }),
    finalCandidate(),
  ], 'obs-2');
  const unrelatedLedger = mergeObservation(ledger, unrelated);
  const replayed = verifyRetention(unrelatedLedger, unrelated, { 'resume-file': validProof });
  assert.equal(replayed.ok, false);
  assert.ok(replayed.errors.some((item) => item.field_id === 'resume-file' && item.code === 'INVALID_PROOF'));
  assert.equal(fieldById(replayed.ledger, 'resume-file').retained, false);
  const refreshed = verifyRetention(unrelatedLedger, unrelated, {
    'resume-file': { ...validProof, observation_id: 'obs-3' },
  });
  assert.equal(refreshed.ok, false);
  assert.ok(refreshed.errors.some((item) => item.field_id === 'ordinary' && item.code === 'VALUE_NOT_RETAINED'));
  assert.equal(fieldById(refreshed.ledger, 'resume-file').retained, true);

  assert.throws(() => verifyRetention(ledger, second, {
    'resume-file': { value_digest: fileDigest },
  }), /missing key|unknown key/i);
  result = verifyRetention(ledger, second, {
    'resume-file': { ...validProof, value_digest: digestPrivateValue('forged') },
  });
  assert.equal(result.ok, false);
  assert.ok(result.errors.some((item) => item.code === 'INVALID_PROOF'));
  result = verifyRetention(ledger, second, {
    'resume-file': { ...validProof, action_id: 'ordinary-action' },
  });
  assert.equal(result.ok, false);
  result = verifyRetention(ledger, second, {
    'resume-file': { ...validProof, source_sha256: SNAPSHOT },
  });
  assert.equal(result.ok, false);
  assert.ok(result.errors.some((item) => item.code === 'INVALID_PROOF'));
  result = verifyRetention(ledger, second, {
    'resume-file': { ...validProof, file_name: 'other.pdf' },
  });
  assert.equal(result.ok, false);
  result = verifyRetention(ledger, second, {
    'resume-file': { ...validProof, committed_method: 'rendered_container' },
  });
  assert.equal(result.ok, false);
  assert.ok(result.errors.some((item) => item.code === 'INVALID_PROOF'));

  result = verifyRetention(ledger, second, {
    ordinary: { ...validProof, field_id: 'ordinary', container_identity: 'ordinary' },
  });
  assert.equal(result.ok, false);
  assert.ok(result.errors.some((item) => item.field_id === 'ordinary' && item.code === 'INVALID_PROOF'));

  const disappeared = observation('obs-3', [fileControl('resume-file', {
    value_present: false,
    file: { accept: ['application/pdf'], count: 0, names: [] },
  }), control('ordinary'), finalCandidate()], 'obs-2');
  ledger = mergeObservation(ledger, disappeared);
  assert.equal(fieldById(ledger, 'resume-file').retained, false);
  result = verifyRetention(ledger, disappeared, { 'resume-file': validProof });
  assert.equal(result.ok, false);
  assert.equal(fieldById(result.ledger, 'resume-file').retained, false);

  const removed = observation('obs-4', [control('ordinary'), finalCandidate()], 'obs-3');
  ledger = mergeObservation(result.ledger, removed);
  assert.equal(fieldById(ledger, 'resume-file').retained, false);
  validateLedger(ledger);
});

test('requires UI-backed deliberate blanks and excludes job wording evidence', () => {
  assert.deepEqual(ANSWER_SOURCES, [
    'memory',
    'profile_verified',
    'profile_user_attested',
    'resume',
    'agent_inference',
    'user',
  ]);
  const text = control('optional-text', {
    required: false,
    value: null,
    value_present: false,
  });
  const unchecked = control('optional-check', {
    kind: 'checkbox',
    tag: 'input',
    type: 'checkbox',
    role: 'checkbox',
    checked: false,
    value: false,
    value_present: false,
  });
  const explicit = control('optional-select', {
    kind: 'select',
    tag: 'select',
    type: 'select',
    role: 'combobox',
    required: false,
    value: 'na',
    value_present: true,
    selected: ['na'],
    options: [{ value: 'na', label: 'Not applicable', disabled: false, selected: true }],
  });
  const first = observation('obs-1', [text, unchecked, explicit, finalCandidate()]);
  let ledger = createLedger(first);
  assert.throws(() => recordResolution(ledger, {
    field_id: 'optional-text',
    observation_id: 'obs-1',
    ref: 'ref-optional-text',
    source: 'job_wording',
    value_digest: null,
    semantic_choice: 'not_applicable',
  }), /invalid answer source/i);
  ledger = recordResolution(ledger, {
    field_id: 'optional-text',
    observation_id: 'obs-1',
    ref: 'ref-optional-text',
    source: 'user',
    value_digest: null,
    semantic_choice: 'not_applicable',
  });
  ledger = recordResolution(ledger, {
    field_id: 'optional-check',
    observation_id: 'obs-1',
    ref: 'ref-optional-check',
    source: 'user',
    value_digest: null,
    semantic_choice: 'none',
  });
  ledger = recordResolution(ledger, {
    field_id: 'optional-select',
    observation_id: 'obs-1',
    ref: 'ref-optional-select',
    source: 'profile_verified',
    value_digest: null,
    semantic_choice: 'not_applicable',
  });
  let result = verifyRetention(ledger, first);
  assert.equal(result.ok, false);
  assert.ok(result.errors.some((item) => item.field_id === 'optional-text'));
  assert.equal(fieldById(result.ledger, 'optional-check').retained, true);
  assert.equal(fieldById(result.ledger, 'optional-select').retained, true);

  const unsupportedSource = observation('obs-2', [unchecked, finalCandidate()], 'obs-1');
  let next = mergeObservation(result.ledger, unsupportedSource);
  next = recordResolution(next, {
    field_id: 'optional-check',
    observation_id: 'obs-2',
    ref: 'ref-optional-check',
    source: 'resume',
    value_digest: null,
    semantic_choice: 'none',
  });
  result = verifyRetention(next, unsupportedSource);
  assert.equal(result.ok, false);
  assert.ok(result.errors.some((item) => item.field_id === 'optional-check'));
  validateObservation(first);
});

test('requires valid agent inference metadata and clears it on sensitive reclassification', () => {
  const metadata = {
    inference_rationale_digest: 'b'.repeat(64),
    inference_evidence_digests: {
      resume_sha256: 'c'.repeat(64),
      job_description_sha256: 'd'.repeat(64),
    },
  };
  const current = observation('obs-inference', [
    control('inferred'),
    control('sensitive-via-resolution'),
    control('already-sensitive'),
    finalCandidate(),
  ]);
  let ledger = createLedger(current);
  const resolution = (fieldId, extra = {}) => ({
    field_id: fieldId,
    observation_id: 'obs-inference',
    ref: `ref-${fieldId}`,
    source: 'agent_inference',
    value_digest: digestPrivateValue(`inferred-${fieldId}`),
    ...metadata,
    ...extra,
  });

  const withoutMetadata = resolution('inferred');
  delete withoutMetadata.inference_rationale_digest;
  delete withoutMetadata.inference_evidence_digests;
  assert.throws(
    () => recordResolution(ledger, withoutMetadata),
    /agent_inference requires inference metadata/i,
  );

  for (const malformed of [
    { ...metadata, inference_rationale_digest: 'not-a-digest' },
    {
      ...metadata,
      inference_evidence_digests: {
        ...metadata.inference_evidence_digests,
        resume_sha256: 'not-a-digest',
      },
    },
    {
      ...metadata,
      inference_evidence_digests: {
        ...metadata.inference_evidence_digests,
        job_description_sha256: 'not-a-digest',
      },
    },
  ]) {
    assert.throws(
      () => recordResolution(ledger, resolution('inferred', malformed)),
      /digest/i,
    );
  }

  assert.throws(
    () => recordResolution(ledger, resolution('inferred', { source: 'profile_verified' })),
    /inference metadata is restricted to agent_inference/i,
  );

  ledger = recordResolution(ledger, resolution('inferred'));
  const inferred = fieldById(ledger, 'inferred');
  assert.equal(inferred.answer_source, 'agent_inference');
  assert.equal(inferred.inference_rationale_digest, metadata.inference_rationale_digest);
  assert.deepEqual(inferred.inference_evidence_digests, metadata.inference_evidence_digests);

  assert.throws(
    () => recordResolution(
      ledger,
      resolution('sensitive-via-resolution', { sensitive: true }),
    ),
    /agent_inference is prohibited for sensitive fields/i,
  );
  ledger = markFieldSensitive(ledger, 'already-sensitive');
  assert.throws(
    () => recordResolution(ledger, resolution('already-sensitive')),
    /agent_inference is prohibited for sensitive fields/i,
  );

  ledger = markFieldSensitive(ledger, 'inferred');
  const reclassified = fieldById(ledger, 'inferred');
  assert.equal(reclassified.sensitive, true);
  assert.equal(reclassified.answer_state, 'unresolved');
  assert.equal(reclassified.answer_source, null);
  assert.equal(reclassified.value_digest, null);
  assert.equal(reclassified.inference_rationale_digest, null);
  assert.equal(reclassified.inference_evidence_digests, null);
});

test('requires observer-normalized checkbox validity and rejects custom errors', () => {
  const checked = control('source-a', {
    group_id: 'source-group',
    kind: 'checkbox',
    tag: 'input',
    type: 'checkbox',
    role: 'checkbox',
    name: 'source[]',
    checked: true,
    value: true,
    value_present: true,
    validity: { valid: true, aria_invalid: false, message: null },
  });
  const normalizedUnchecked = control('source-b', {
    group_id: 'source-group',
    kind: 'checkbox',
    tag: 'input',
    type: 'checkbox',
    role: 'checkbox',
    name: 'source[]',
    checked: false,
    value: false,
    value_present: false,
    validity: { valid: true, aria_invalid: false, message: null },
  });
  const resolveGroup = (current) => {
    let ledger = createLedger(current);
    ledger = recordResolution(ledger, {
      field_id: 'source-a',
      observation_id: current.observation_id,
      ref: current.controls[0].ref,
      source: 'profile_verified',
      value_digest: digestPrivateValue(true),
    });
    return recordResolution(ledger, {
      field_id: 'source-b',
      observation_id: current.observation_id,
      ref: current.controls[1].ref,
      source: 'profile_verified',
      value_digest: null,
      semantic_choice: 'none',
    });
  };

  const normalized = observation('obs-1', [checked, normalizedUnchecked, finalCandidate()]);
  const retained = verifyRetention(resolveGroup(normalized), normalized);
  assert.equal(retained.ok, true);
  assert.equal(auditCompletion(retained.ledger, normalized).passed, true);

  const customError = observation('obs-2', [
    { ...checked, ref: 'ref-source-a-custom' },
    {
      ...normalizedUnchecked,
      ref: 'ref-source-b-custom',
      validity: {
        valid: false,
        aria_invalid: false,
        message: 'Select at least two checkboxes',
      },
    },
    finalCandidate(),
  ]);
  const rejected = verifyRetention(resolveGroup(customError), customError);
  assert.equal(rejected.ok, false);
  assert.ok(rejected.errors.some((item) =>
    item.code === 'INVALID_FIELD' && item.field_id === 'source-b'));
  assert.equal(auditCompletion(rejected.ledger, customError).passed, false);
});
