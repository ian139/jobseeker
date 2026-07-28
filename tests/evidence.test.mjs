import assert from 'node:assert/strict';
import crypto from 'node:crypto';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import test from 'node:test';

import {
  EvidenceStore,
  EvidenceStoreError,
  canonicalJson,
  createEvidenceStore,
  sha256File,
  validateCompletionEvidence,
} from '../src/phase1/evidence.mjs';

function temporaryDirectory() {
  return fs.mkdtempSync(path.join(os.tmpdir(), 'phase1-evidence-'));
}

function withStore(callback) {
  const parent = temporaryDirectory();
  const root = path.join(parent, 'run');
  const store = new EvidenceStore(root);
  try {
    return callback({ parent, root, store });
  } finally {
    store.close();
    fs.rmSync(parent, { recursive: true, force: true });
  }
}

function expectCode(callback, code) {
  assert.throws(callback, (error) => error instanceof EvidenceStoreError && error.code === code);
}

function runMetadata(resumePath, resumeBytes) {
  return {
    schema: 'phase1-run-evidence-v1',
    application_url: 'https://example.invalid/app',
    run_contract_sha256: '0'.repeat(64),
    resume_upload_path: path.resolve(resumePath),
    resume_upload_sha256: crypto.createHash('sha256').update(resumeBytes).digest('hex'),
    browser_mode: 'headed',
    observer: 'playwright_dom_v1',
    action_driver: 'omp_browser',
    submit_policy: 'omp_agent',
    loop_contract: 'safe-batch-observe-act-reobserve',
    started_at: '2026-01-01T00:00:00Z',
  };
}

function finalAudit(observationId = 'obs-1', overrides = {}) {
  return {
    schema: 'phase1-audit-v1',
    observation_id: observationId,
    passed: true,
    complete: true,
    blockers: [],
    stale_refs: [],
    unresolved_field_ids: [],
    invalid_field_ids: [],
    unretained_field_ids: [],
    revealed_field_ids: [],
    final_candidate_refs: [],
    final_review_boundary: true,
    submit_action_count: 0,
    field_count: 0,
    final: true,
    ...overrides,
  };
}
function recordFinalSubmit(store, actionId = 'attempt-1', outcome = 'succeeded') {
  store.recordAction({
    action: 'final_submit',
    action_id: actionId,
    outcome: 'attempted',
    observation_id: 'obs-1',
    ref: 'final-ref',
  });
  store.recordAction({
    action: 'final_submit_result',
    attempt_id: actionId,
    outcome,
    error_code: null,
  });
}
function publishValidCompletion(parent, store) {
  const screenshotPath = path.join(parent, 'screenshot.png');
  const uploadPath = path.join(parent, 'resume.pdf');
  fs.writeFileSync(screenshotPath, 'screen', { mode: 0o600 });
  fs.writeFileSync(uploadPath, 'upload', { mode: 0o600 });
  store.recordRunMetadata(runMetadata(uploadPath, Buffer.from('upload')));
  recordFinalSubmit(store);
  const auditRef = store.recordFinalAudit(finalAudit());
  const completion = store.finalize({
    audit: auditRef,
    screenshotPath,
    uploadPath,
    submitActionCount: 1,
  });
  return { completion, screenshotPath, uploadPath, auditRef };
}

test('creates and verifies an owner-private root and artifacts', () => withStore(({ root, store }) => {
  assert.equal(fs.statSync(root).mode & 0o777, 0o700);
  const ref = store.recordObservation({ z: 1, a: { y: true, x: 'safe' } });
  assert.equal(ref.path, 'observation-000001.json');
  assert.equal(fs.statSync(path.join(root, ref.path)).mode & 0o777, 0o600);
  assert.equal(fs.readFileSync(path.join(root, ref.path), 'utf8'), '{"a":{"x":"safe","y":true},"z":1}');
  assert.deepEqual(store.readArtifact(ref.path), { a: { x: 'safe', y: true }, z: 1 });
}));

test('rejects symlink and non-directory roots', () => {
  const parent = temporaryDirectory();
  try {
    const target = path.join(parent, 'target');
    fs.mkdirSync(target, { mode: 0o700 });
    const link = path.join(parent, 'link');
    fs.symlinkSync(target, link, 'dir');
    expectCode(() => new EvidenceStore(link), 'INVALID_ROOT');
    const file = path.join(parent, 'file');
    fs.writeFileSync(file, 'x', { mode: 0o600 });
    expectCode(() => new EvidenceStore(file), 'INVALID_ROOT');
  } finally {
    fs.rmSync(parent, { recursive: true, force: true });
  }
});

test('keeps artifact paths confined and refuses overwrite', () => withStore(({ root, store }) => {
  const ref = store.recordDiff({ changed: true });
  expectCode(() => store.writeJsonArtifact('../outside.json', {}), 'UNSAFE_PATH');
  expectCode(() => store.writeJsonArtifact(path.join('nested', 'x.json'), {}), 'UNSAFE_PATH');
  expectCode(() => store.writeJsonArtifact(ref.path, { changed: false }), 'ARTIFACT_EXISTS');
  assert.equal(fs.existsSync(path.join(path.dirname(root), 'outside.json')), false);
  expectCode(() => store.writeJsonArtifact('completion.json', { receipt: true }), 'ARTIFACT_RESERVED');
  expectCode(() => store.writeArtifact('completion.json', { receipt: true }), 'ARTIFACT_RESERVED');
}));

test('hashes only regular files and persists path, size, and digest', () => withStore(({ parent, store }) => {
  const input = path.join(parent, 'input.bin');
  const content = Buffer.from('bounded test input');
  fs.writeFileSync(input, content, { mode: 0o600 });
  const expected = crypto.createHash('sha256').update(content).digest('hex');
  const direct = sha256File(input);
  assert.deepEqual(direct, { path: path.resolve(input), size: content.length, sha256: expected });
  const ref = store.recordFileIdentity(input, 'input');
  assert.equal(ref.sha256, expected);
  assert.equal(ref.size, content.length);
  assert.equal(ref.path, path.resolve(input));
  assert.deepEqual(store.readArtifact(ref.artifactPath), { path: path.resolve(input), size: content.length, sha256: expected });
  const link = path.join(parent, 'input-link');
  fs.symlinkSync(input, link);
  expectCode(() => store.recordFileIdentity(link, 'input'), 'INPUT_INVALID');
}));

test('canonical JSON is compact, sorted, and bounded', () => {
  assert.equal(canonicalJson({ b: 2, a: [true, { d: 4, c: 3 }] }), '{"a":[true,{"c":3,"d":4}],"b":2}');
  expectCode(() => canonicalJson({ value: 'x'.repeat(200) }, { maxBytes: 32 }), 'PAYLOAD_TOO_LARGE');
});

test('appends an ordered private action journal', () => withStore(({ root, store }) => {
  store.appendAction({ kind: 'fill', ref: 'field-1' });
  store.appendAction({ kind: 'click', ref: 'continue-1' });
  const entries = store.readActionJournal();
  assert.deepEqual(entries.map((entry) => entry.sequence), [1, 2]);
  assert.deepEqual(entries.map((entry) => entry.kind), ['fill', 'click']);
  assert.equal(fs.statSync(path.join(root, 'action-journal.jsonl')).mode & 0o777, 0o600);
  assert.match(fs.readFileSync(path.join(root, 'action-journal.jsonl'), 'utf8'), /^\{"kind":"fill","ref":"field-1","sequence":1\}\n/);
}));

test('recordAction publishes both action artifact and journal entry', () => withStore(({ store }) => {
  const result = store.recordAction({ type: 'fill', ref: 'field-1' });
  assert.equal(result.path, 'action-000001.json');
  assert.equal(result.sequence, 1);
  assert.deepEqual(store.readActionJournal()[0], { ref: 'field-1', sequence: 1, type: 'fill' });
}));

test('detects corrupt and missing artifacts instead of treating them as evidence', () => withStore(({ root, store }) => {
  const ref = store.recordLedger({ complete: true });
  fs.writeFileSync(path.join(root, ref.path), '{corrupt', { mode: 0o600 });
  expectCode(() => store.readArtifact(ref.path), 'ARTIFACT_CORRUPT');
  fs.unlinkSync(path.join(root, ref.path));
  expectCode(() => store.readArtifact(ref.path), 'ARTIFACT_MISSING');
}));

test('finalizes with complete audit, screenshot identity, upload identity, and non-negative submit count', () => withStore(({ parent, root, store }) => {
  const screenshotPath = path.join(parent, 'screenshot.png');
  const uploadPath = path.join(parent, 'resume.pdf');
  fs.writeFileSync(screenshotPath, Buffer.from('screen'), { mode: 0o600 });
  fs.writeFileSync(uploadPath, Buffer.from('upload'), { mode: 0o600 });
  store.recordRunMetadata(runMetadata(uploadPath, Buffer.from('upload')));
  recordFinalSubmit(store);
  store.recordObservation({ observation_id: 'obs-1', controls: [], blockers: [] });
  const audit = finalAudit();
  const auditRef = store.recordFinalAudit(audit);
  const completion = store.finalize({
    audit: auditRef,
    screenshotPath,
    uploadPath,
    submitActionCount: 1,
  });
  assert.equal(completion.report.submit_action_count, 1);
  assert.equal(completion.report.screenshot.size, 6);
  assert.equal(completion.report.upload.size, 6);
  assert.deepEqual(store.readCompletionReport(), completion.report);
  const validated = validateCompletionEvidence(root);
  assert.deepEqual(validated.actionSummary, [{ action: 'final_submit', outcome: 'succeeded' }]);
  assert.equal(validated.submitActionCount, 1);
  assert.equal(Object.isFrozen(validated), true);
  assert.equal(Object.isFrozen(validated.report), true);
  assert.equal(Object.isFrozen(validated.actionSummary[0]), true);
  assert.equal(fs.statSync(path.join(root, 'completion.json')).mode & 0o777, 0o600);
}));

test('requires explicit submit count and complete audit', () => withStore(({ parent, store }) => {
  const screenshotPath = path.join(parent, 'screenshot.png');
  const uploadPath = path.join(parent, 'resume.pdf');
  fs.writeFileSync(screenshotPath, 'screen', { mode: 0o600 });
  fs.writeFileSync(uploadPath, 'upload', { mode: 0o600 });
  recordFinalSubmit(store);
  expectCode(() => store.finalize({ audit: { complete: true }, screenshotPath, uploadPath }), 'SUBMIT_COUNT_REQUIRED');
  expectCode(() => store.finalize({ audit: { complete: false }, screenshotPath, uploadPath, submitActionCount: 1 }), 'FINAL_AUDIT_REQUIRED');
}));

test('requires non-negative submit action count', () => withStore(({ parent, store }) => {
  const screenshotPath = path.join(parent, 'screenshot.png');
  const uploadPath = path.join(parent, 'resume.pdf');
  fs.writeFileSync(screenshotPath, 'screen', { mode: 0o600 });
  fs.writeFileSync(uploadPath, 'upload', { mode: 0o600 });
  expectCode(() => store.finalize({ audit: finalAudit(), screenshotPath, uploadPath, submitActionCount: -1 }), 'SUBMISSION_EVIDENCE');
}));

test('requires the exact final audit payload and artifact shape', () => withStore(({ parent, root, store }) => {
  const screenshotPath = path.join(parent, 'screenshot.png');
  const uploadPath = path.join(parent, 'resume.pdf');
  fs.writeFileSync(screenshotPath, 'screen', { mode: 0o600 });
  fs.writeFileSync(uploadPath, 'upload', { mode: 0o600 });
  store.recordRunMetadata(runMetadata(uploadPath, Buffer.from('upload')));
  recordFinalSubmit(store);
  const audit = finalAudit();
  expectCode(() => store.recordFinalAudit({ ...audit, unexpected: true }), 'FINAL_AUDIT_REQUIRED');
  expectCode(() => store.finalize({
    audit: { ...audit, final: false },
    screenshotPath,
    uploadPath,
    submitActionCount: 1,
  }), 'FINAL_AUDIT_REQUIRED');
  const auditRef = store.recordFinalAudit(audit);
  fs.writeFileSync(path.join(root, auditRef.path), canonicalJson({ ...audit, unexpected: true }), { mode: 0o600 });
  expectCode(() => store.finalize({
    audit: auditRef,
    screenshotPath,
    uploadPath,
    submitActionCount: 1,
  }), 'FINAL_AUDIT_REQUIRED');
}));

test('reopens supplied identities and binds artifact refs to their kind', () => withStore(({ parent, store }) => {
  const screenshotPath = path.join(parent, 'screenshot.png');
  const uploadPath = path.join(parent, 'resume.pdf');
  fs.writeFileSync(screenshotPath, 'screen', { mode: 0o600 });
  fs.writeFileSync(uploadPath, 'upload', { mode: 0o600 });
  store.recordRunMetadata(runMetadata(uploadPath, Buffer.from('upload')));
  recordFinalSubmit(store);
  const auditRef = store.recordFinalAudit(finalAudit());
  const screenshotRef = store.recordScreenshot(screenshotPath);
  const uploadRef = store.recordUpload(uploadPath);
  fs.writeFileSync(screenshotPath, 'change', { mode: 0o600 });
  expectCode(() => store.finalize({
    audit: auditRef,
    screenshot: screenshotRef,
    upload: uploadRef,
    submitActionCount: 1,
  }), 'IDENTITY_INVALID');
  fs.writeFileSync(screenshotPath, 'screen', { mode: 0o600 });
  expectCode(() => store.finalize({
    audit: auditRef,
    screenshot: screenshotRef,
    upload: screenshotRef,
    submitActionCount: 1,
  }), 'IDENTITY_INVALID');
  const link = path.join(parent, 'upload-link');
  fs.symlinkSync(uploadPath, link);
  const digest = crypto.createHash('sha256').update('upload').digest('hex');
  expectCode(() => store.finalize({
    audit: auditRef,
    screenshot: screenshotPath,
    upload: { path: link, size: 6, sha256: digest },
    submitActionCount: 1,
  }), 'INPUT_INVALID');
}));

test('propagates submit context through nested targets without matching job text', () => withStore(({ parent, store }) => {
  const screenshotPath = path.join(parent, 'screenshot.png');
  const uploadPath = path.join(parent, 'resume.pdf');
  fs.writeFileSync(screenshotPath, 'screen', { mode: 0o600 });
  fs.writeFileSync(uploadPath, 'upload', { mode: 0o600 });
  store.recordRunMetadata(runMetadata(uploadPath, Buffer.from('upload')));
  store.recordAction({ type: 'click', target: { role: 'button', name: 'Submit application' } });
  recordFinalSubmit(store);
  const completion = store.finalize({
    audit: finalAudit(),
    screenshotPath,
    uploadPath,
    submitActionCount: 1,
  });
  assert.equal(completion.report.submit_action_count, 1);
  assert.equal(completion.report.action_journal.entries, 3);
}));

test('allows unrelated job text while finalizing a valid run', () => withStore(({ parent, store }) => {
  const screenshotPath = path.join(parent, 'screenshot.png');
  const uploadPath = path.join(parent, 'resume.pdf');
  fs.writeFileSync(screenshotPath, 'screen', { mode: 0o600 });
  fs.writeFileSync(uploadPath, 'upload', { mode: 0o600 });
  store.recordRunMetadata(runMetadata(uploadPath, Buffer.from('upload')));
  store.recordAction({ type: 'fill', target: { role: 'textbox', name: 'Job title' }, value: 'Submit application' });
  recordFinalSubmit(store);
  const completion = store.finalize({
    audit: finalAudit(),
    screenshotPath,
    uploadPath,
    submitActionCount: 1,
  });
  assert.equal(completion.report.submit_action_count, 1);
  assert.equal(completion.report.action_journal.entries, 3);
}));

test('rejects malformed and nonregular manual completion receipts', () => withStore(({ root, store }) => {
  fs.writeFileSync(path.join(root, 'completion.json'), canonicalJson({ receipt: true }), { mode: 0o600 });
  assert.equal(store.finalized, false);
  expectCode(() => store.readCompletionReport(), 'ARTIFACT_CORRUPT');
  fs.unlinkSync(path.join(root, 'completion.json'));
  const target = path.join(root, 'manual.json');
  fs.writeFileSync(target, canonicalJson({ receipt: true }), { mode: 0o600 });
  fs.symlinkSync(target, path.join(root, 'completion.json'));
  assert.equal(store.finalized, false);
  expectCode(() => validateCompletionEvidence(root), 'ARTIFACT_CORRUPT');
}));

test('rejects unresolved final submit attempts before publication', () => withStore(({ parent, store }) => {
  const screenshotPath = path.join(parent, 'screenshot.png');
  const uploadPath = path.join(parent, 'resume.pdf');
  fs.writeFileSync(screenshotPath, 'screen', { mode: 0o600 });
  fs.writeFileSync(uploadPath, 'upload', { mode: 0o600 });
  store.recordAction({
    action: 'final_submit',
    action_id: 'unresolved-attempt',
    outcome: 'attempted',
  });
  expectCode(() => store.finalize({
    audit: finalAudit(),
    screenshotPath,
    uploadPath,
    submitActionCount: 1,
  }), 'SUBMISSION_EVIDENCE');
}));

test('rejects mutated canonical references and external identities', () => withStore(({ parent, root, store }) => {
  const { completion, auditRef } = publishValidCompletion(parent, store);
  const mutatedAudit = { ...finalAudit(), field_count: 1 };
  fs.writeFileSync(path.join(root, auditRef.path), canonicalJson(mutatedAudit), { mode: 0o600 });
  expectCode(() => validateCompletionEvidence(root), 'ARTIFACT_CORRUPT');
  fs.writeFileSync(path.join(root, 'completion.json'), canonicalJson({
    ...completion.report,
    final_audit: { ...completion.report.final_audit, sha256: '0'.repeat(64) },
  }), { mode: 0o600 });
  expectCode(() => store.readCompletionReport(), 'ARTIFACT_CORRUPT');
}));

test('rejects external identity mutation on canonical readback', () => withStore(({ parent, root, store }) => {
  const { screenshotPath } = publishValidCompletion(parent, store);
  fs.writeFileSync(screenshotPath, 'changed', { mode: 0o600 });
  expectCode(() => validateCompletionEvidence(root), 'IDENTITY_INVALID');
  assert.equal(store.finalized, false);
}));

test('rejects uploads that do not match the configured resume identity', () => withStore(({ parent, store }) => {
  const screenshotPath = path.join(parent, 'screenshot.png');
  const uploadPath = path.join(parent, 'resume.pdf');
  const otherPath = path.join(parent, 'other.pdf');
  fs.writeFileSync(screenshotPath, 'screen', { mode: 0o600 });
  fs.writeFileSync(uploadPath, 'upload', { mode: 0o600 });
  fs.writeFileSync(otherPath, 'upload', { mode: 0o600 });
  store.recordRunMetadata(runMetadata(uploadPath, Buffer.from('upload')));
  recordFinalSubmit(store);
  expectCode(() => store.finalize({
    audit: finalAudit(),
    screenshotPath,
    uploadPath: otherPath,
    submitActionCount: 1,
  }), 'IDENTITY_INVALID');
}));

test('fails closed when the pinned root is replaced during publication', () => {
  const parent = temporaryDirectory();
  const root = path.join(parent, 'run');
  const store = new EvidenceStore(root);
  const moved = path.join(parent, 'run-old');
  const originalFsync = fs.fsyncSync;
  let replaced = false;
  fs.fsyncSync = (fd) => {
    originalFsync(fd);
    if (!replaced) {
      replaced = true;
      fs.renameSync(root, moved);
      fs.mkdirSync(root, { mode: 0o700 });
    }
  };
  try {
    expectCode(() => store.recordObservation({ observation_id: 'obs-1' }), 'ROOT_CHANGED');
  } finally {
    fs.fsyncSync = originalFsync;
    store.close();
    fs.rmSync(parent, { recursive: true, force: true });
  }
});

test('accepts immutable evidence from the legacy one-field loop contract', async () => {
  const parent = temporaryDirectory();
  try {
    const resumePath = path.join(parent, 'resume.pdf');
    const resumeBytes = Buffer.from('upload');
    fs.writeFileSync(resumePath, resumeBytes, { mode: 0o600 });
    const metadata = {
      ...runMetadata(resumePath, resumeBytes),
      loop_contract: 'one-field-observe-act-reobserve',
    };
    const evidence = await createEvidenceStore(path.join(parent, 'legacy-run'), metadata);
    assert.equal(evidence.root, path.join(parent, 'legacy-run'));
    await evidence.close();
  } finally {
    fs.rmSync(parent, { recursive: true, force: true });
  }
});

test('async factory records run metadata and returns the narrow coordinator surface', async () => {
  const parent = temporaryDirectory();
  try {
    const resumePath = path.join(parent, 'resume.pdf');
    const resumeBytes = Buffer.from('upload');
    fs.writeFileSync(resumePath, resumeBytes, { mode: 0o600 });
    const evidence = await createEvidenceStore(path.join(parent, 'run'), runMetadata(resumePath, resumeBytes));
    const ref = await evidence.recordObservation({ observation_id: 'obs-1' });
    assert.equal(ref.path, 'observation-000001.json');
    assert.equal(evidence.root, path.join(parent, 'run'));
    await evidence.close();
  } finally {
    fs.rmSync(parent, { recursive: true, force: true });
  }
});
