import assert from 'node:assert/strict';
import crypto from 'node:crypto';
import { spawn } from 'node:child_process';
import { promises as fsp } from 'node:fs';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import test from 'node:test';
import {
  appendAnswerRecord,
  approvalContextSha256,
  createAnswerRecord,
  loadRunContractSnapshot,
} from '../src/phase1/contract.mjs';

import { EvidenceStore } from '../src/phase1/evidence.mjs';
import {
  claimNextQueuedJob,
  createJobWorkspace,
  heartbeatActiveRun,
  pauseRunForUser,
  persistTerminalOutcome,
  requeueTerminalJob,
  preflightBacklogRun,
  recoverActiveRun,
  recoverOrClaimBacklogRun,
  resumeNeedsUserRun,
  skipNeedsUserRun,
} from '../src/phase1/backlog-runner.mjs';

const SQLITE = 'sqlite3';
const NOW = '2026-07-26T00:00:00.000Z';

function sql(value) {
  if (value === null || value === undefined) return 'NULL';
  if (typeof value === 'number') return String(value);
  return `'${String(value).replaceAll("'", "''")}'`;
}

async function sqlite(database, statement) {
  const stdout = await new Promise((resolve, reject) => {
    const child = spawn(SQLITE, ['-bail', '-json', database], {
      stdio: ['pipe', 'pipe', 'pipe'],
    });
    let output = '';
    let errorOutput = '';
    child.stdout.setEncoding('utf8');
    child.stderr.setEncoding('utf8');
    child.stdout.on('data', (chunk) => { output += chunk; });
    child.stderr.on('data', (chunk) => { errorOutput += chunk; });
    child.once('error', reject);
    child.once('close', (code) => {
      if (code === 0) resolve(output);
      else reject(new Error(errorOutput || `sqlite exited ${code}`));
    });
    child.stdin.end(statement);
  });
  const text = stdout.trim();
  return text === '' ? [] : JSON.parse(text);
}

async function migration004() {
  const names = await fsp.readdir('migrations');
  const name = names.find((entry) => /^004-.*\.sql$/u.test(entry));
  assert.ok(name, 'migration 004 must be present');
  return fsp.readFile(path.join('migrations', name), 'utf8');
}
async function migration008() {
  return fsp.readFile('migrations/008-resume-artifacts.sql', 'utf8');
}

async function createPost003Database(database, rows) {
  await sqlite(database, `
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS jobs (id INTEGER PRIMARY KEY);
CREATE TABLE application_jobs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  source_table TEXT NOT NULL CHECK (source_table IN ('legacy_jobs','assistant_jobs')),
  source_db TEXT NOT NULL,
  source_rowid INTEGER NOT NULL,
  source_job_id TEXT NOT NULL,
  application_url TEXT NOT NULL,
  eligibility_tier TEXT NOT NULL CHECK (eligibility_tier IN ('active_verified','backfill_only','unverified_stale')),
  verification_reason TEXT,
  source_posted_at TEXT,
  source_last_seen_at TEXT,
  status TEXT NOT NULL DEFAULT 'queued' CHECK (status IN ('queued','claimed','completed','blocked','closed','skipped','failed','needs_user')),
  status_reason TEXT,
  claimed_at TEXT,
  completed_at TEXT,
  UNIQUE(source_table, source_db, source_rowid),
  UNIQUE(application_url)
);
CREATE INDEX idx_application_jobs_status_id ON application_jobs(status, id);
CREATE TABLE application_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  job_id INTEGER NOT NULL REFERENCES application_jobs(id) ON DELETE RESTRICT,
  status TEXT NOT NULL CHECK (status IN ('preparing','completed','blocked','closed','failed','needs_user')),
  reason_code TEXT NOT NULL,
  started_at TEXT NOT NULL,
  finished_at TEXT,
  final_url TEXT,
  actions_json TEXT NOT NULL DEFAULT '[]',
  evidence_path TEXT NOT NULL,
  submit_action_count INTEGER CHECK (submit_action_count IS NULL OR submit_action_count >= 0),
  CHECK (status <> 'completed' OR (submit_action_count IS NOT NULL AND submit_action_count >= 1))
);
CREATE INDEX idx_application_runs_job_id ON application_runs(job_id);
CREATE INDEX idx_application_runs_status ON application_runs(status);
${rows.map((row) => `INSERT INTO application_jobs (
  id, source_table, source_db, source_rowid, source_job_id, application_url,
  eligibility_tier, verification_reason, source_posted_at, source_last_seen_at,
  status, status_reason, claimed_at, completed_at
) VALUES (
  ${sql(row.id)}, ${sql(row.sourceTable ?? 'legacy_jobs')}, ${sql(row.sourceDb ?? 'fixture.sqlite')},
  ${sql(row.sourceRowid ?? row.id)}, ${sql(row.sourceJobId ?? `fixture:${row.id}`)},
  ${sql(row.applicationUrl ?? `https://job-boards.greenhouse.io/fixture/jobs/${row.id}`)},
  ${sql(row.tier ?? 'active_verified')}, ${sql(row.verificationReason ?? 'fixture_reason')},
  ${sql(row.postedAt ?? '2026-06-01T00:00:00.000Z')},
  ${sql(row.lastSeen ?? '2026-07-01T00:00:00.000Z')},
  ${sql(row.status ?? 'queued')}, ${sql(row.statusReason)}, ${sql(row.claimedAt)}, ${sql(row.completedAt)}
);`).join('\n')}
`);
  await sqlite(database, `
${rows.map((row) => `INSERT INTO jobs (id) VALUES (${sql(row.id)});`).join('\n')}
`);
  await sqlite(database, await migration004());
  await sqlite(database, await migration008());
  await sqlite(database, `
ALTER TABLE application_jobs ADD COLUMN platform TEXT;
ALTER TABLE application_jobs ADD COLUMN application_host TEXT;
ALTER TABLE application_jobs ADD COLUMN job_title TEXT;
ALTER TABLE application_jobs ADD COLUMN job_company TEXT;
ALTER TABLE application_jobs ADD COLUMN job_location TEXT;
ALTER TABLE application_jobs ADD COLUMN job_description TEXT;
ALTER TABLE application_jobs ADD COLUMN job_description_sha256 TEXT;
UPDATE application_jobs
SET platform = 'greenhouse',
    application_host = 'job-boards.greenhouse.io',
    job_title = 'Fixture Engineer',
    job_company = 'Fixture Company',
    job_location = 'Remote',
    job_description = 'Fixture job description.',
    job_description_sha256 = '${sha256Bytes('Fixture job description.')}';
`);
}

function profileValue() {
  return JSON.stringify({ schema: 'phase1-profile-v1' });
}

async function privateInput(filePath, contents) {
  await fsp.writeFile(filePath, contents, { mode: 0o600 });
  await fsp.chmod(filePath, 0o600);
}

async function fixture(rows) {
  const root = await fsp.mkdtemp(path.join(os.tmpdir(), 'phase3-backlog-runner-'));
  const database = path.join(root, 'jobs.sqlite');
  const privateRoot = path.join(root, 'private');
  const jobDescriptionPath = path.join(root, 'job-description.txt');
  const applicantProfilePath = path.join(root, 'applicant-profile.json');
  const sourceResumePath = path.join(root, 'source-resume.pdf');
  const resumeUploadPath = path.join(root, 'resume-upload.pdf');
  const answerMemoryPath = path.join(root, 'answer-memory.jsonl');
  await privateInput(jobDescriptionPath, 'fixture job description; no applicant values');
  await privateInput(applicantProfilePath, profileValue());
  await privateInput(sourceResumePath, '%PDF-1.7\nfixture source resume\n');
  await privateInput(resumeUploadPath, '%PDF-1.7\nfixture selected resume\n');
  await privateInput(answerMemoryPath, '');
  await createPost003Database(database, rows);
  const descriptionSha256 = sha256Bytes(Buffer.from('fixture job description; no applicant values'));
  const resumeSha256 = sha256Bytes(Buffer.from('%PDF-1.7\nfixture selected resume\n'));
  await sqlite(database, `
${rows.map((row) => `INSERT INTO resume_artifacts (
  application_job_id, normalized_job_id, job_description_sha256,
  generator_fingerprint_sha256, generator_schema_version, manifest_path,
  manifest_sha256, pdf_path, pdf_sha256, job_description_path, pages, created_at
) VALUES (
  ${sql(row.id)}, ${sql(row.id)}, ${sql(descriptionSha256)}, ${sql('a'.repeat(64))},
  'fixture-generator-v1', ${sql(path.join(root, `manifest-${row.id}.json`))},
  ${sql('b'.repeat(64))}, ${sql(resumeUploadPath)}, ${sql(resumeSha256)},
  ${sql(jobDescriptionPath)}, 1, ${sql(NOW)}
);
UPDATE application_jobs
SET current_resume_artifact_id = last_insert_rowid(),
    resume_preparation_state = 'ready',
    resume_preparation_attempted_at = ${sql(NOW)},
    resume_prepared_at = ${sql(NOW)}
WHERE id = ${sql(row.id)};`).join('\n')}
`);
  return {
    root,
    database,
    preflight: {
      workspaceRoot: privateRoot,
      jobDescriptionPath,
      applicantProfilePath,
      sourceResumePath,
      resumeUploadPath,
      answerMemoryPath,
    },
  };
}

async function removeFixture(value) {
  await fsp.rm(value.root, { recursive: true, force: true });
}

function claimOptions(value, overrides = {}) {
  return {
    ownerId: 'owner-a',
    browserSessionId: 'browser-a',
    now: NOW,
    leaseSeconds: 60,
    maxActiveJobs: 1,
    ...value.preflight,
    ...overrides,
  };
}

async function readRows(database, query) {
  return sqlite(database, `${query}\n`);
}

function sha256Bytes(value) {
  return crypto.createHash('sha256').update(value).digest('hex');
}

async function sha256File(filePath) {
  return sha256Bytes(await fsp.readFile(filePath));
}

async function publishCanonicalCompletion({
  root,
  evidencePath,
  applicationUrl,
  contractPath,
  contractSha256,
  resumeUploadPath,
  finalUrl,
}) {
  const screenshotPath = path.join(root, `screenshot-${path.basename(evidencePath)}.png`);
  const uploadBytes = await fsp.readFile(resumeUploadPath);
  fs.writeFileSync(screenshotPath, 'fixture screenshot', { mode: 0o600 });
  await fsp.chmod(screenshotPath, 0o600);
  const store = new EvidenceStore(evidencePath);
  try {
    store.recordRunMetadata({
      schema: 'phase1-run-evidence-v1',
      application_url: applicationUrl,
      run_contract_sha256: contractSha256 ?? await sha256File(contractPath),
      resume_upload_path: path.resolve(resumeUploadPath),
      resume_upload_sha256: sha256Bytes(uploadBytes),
      browser_mode: 'headed',
      observer: 'playwright_dom_v1',
      action_driver: 'omp_browser',
      submit_policy: 'omp_agent',
      loop_contract: 'safe-batch-observe-act-reobserve',
      started_at: NOW,
    });
    store.recordAction({
      action: 'final_submit',
      action_id: 'attempt-1',
      outcome: 'attempted',
      observation_id: 'observation-1',
      ref: 'final-ref',
    });
    store.recordAction({
      action: 'final_submit_result',
      attempt_id: 'attempt-1',
      outcome: 'succeeded',
      error_code: null,
    });
    const audit = store.recordFinalAudit({
      schema: 'phase1-audit-v1',
      observation_id: 'observation-1',
      passed: true,
      complete: true,
      blockers: [],
      stale_refs: [],
      unresolved_field_ids: [],
      invalid_field_ids: [],
      unretained_field_ids: [],
      revealed_field_ids: [],
      final_candidate_refs: ['final-ref'],
      final_review_boundary: true,
      submit_action_count: 1,
      field_count: 0,
      final: true,
    });
    store.finalize({
      audit,
      screenshotPath,
      uploadPath: resumeUploadPath,
      finalUrl: finalUrl ?? applicationUrl,
      submitActionCount: 1,
    });
  } finally {
    store.close();
  }
}

async function createWorkspace(value, run) {
  return createJobWorkspace(run, {
    ...value.preflight,
    startedAt: run.claimedAt ?? NOW,
  });
}

function assertRunIdentity(run, expected) {
  assert.equal(run.runId, expected.runId);
  assert.equal(run.jobId, expected.jobId);
  assert.equal(run.ownerId, expected.ownerId);
  assert.equal(run.browserSessionId, expected.browserSessionId);
}

test('preflight accepts profile-only and source-only complete inputs', async () => {
  const value = await fixture([{ id: 1 }]);
  try {
    const { sourceResumePath, ...profileOnly } = value.preflight;
    const profileResult = await preflightBacklogRun(profileOnly);
    assert.equal(profileResult.workspaceRoot, path.resolve(value.preflight.workspaceRoot));
    assert.equal(profileResult.resumeArtifactPath, path.resolve(value.preflight.resumeUploadPath));
    assert.match(profileResult.resumeArtifactSha256, /^[0-9a-f]{64}$/u);

    const { applicantProfilePath, ...sourceOnly } = value.preflight;
    const sourceResult = await preflightBacklogRun(sourceOnly);
    assert.equal(sourceResult.workspaceRoot, path.resolve(value.preflight.workspaceRoot));
    assert.equal(sourceResult.resumeArtifactPath, path.resolve(value.preflight.resumeUploadPath));
    assert.match(sourceResult.resumeArtifactSha256, /^[0-9a-f]{64}$/u);
  } finally {
    await removeFixture(value);
  }
});

test('invalid preflight is rejected before claim and cannot mutate the queue', async () => {
  const value = await fixture([{ id: 2 }]);
  try {
    const invalid = {
      ...claimOptions(value),
      jobDescriptionPath: path.join(value.root, 'missing-description.txt'),
    };
    await assert.rejects(() => claimNextQueuedJob(value.database, invalid));
    assert.deepEqual(
      await readRows(value.database, 'SELECT id, status FROM application_jobs WHERE id = 2'),
      [{ id: 2, status: 'queued' }],
    );
    assert.deepEqual(await readRows(value.database, 'SELECT count(*) AS count FROM application_runs'), [{ count: 0 }]);

    const malformedProfile = path.join(value.root, 'malformed-profile.json');
    await privateInput(malformedProfile, '{not-json');
    await assert.rejects(() => preflightBacklogRun({
      ...value.preflight,
      applicantProfilePath: malformedProfile,
      sourceResumePath: undefined,
    }));

    await assert.rejects(() => claimNextQueuedJob(value.database, claimOptions(value, {
      leaseSeconds: 86401,
    })));
    assert.deepEqual(await readRows(value.database, 'SELECT id, status FROM application_jobs WHERE id = 2'), [
      { id: 2, status: 'queued' },
    ]);
    assert.deepEqual(await readRows(value.database, 'SELECT count(*) AS count FROM application_runs'), [{ count: 0 }]);
  } finally {
    await removeFixture(value);
  }
});

test('startup recovery takes precedence over invalid claim preflight', async () => {
  const value = await fixture([{ id: 3 }]);
  try {
    const claimed = await claimNextQueuedJob(value.database, claimOptions(value, {
      ownerId: 'owner-a',
      browserSessionId: 'browser-a',
      leaseSeconds: 10,
    }));
    const result = await recoverOrClaimBacklogRun(value.database, claimOptions(value, {
      ownerId: 'owner-b',
      browserSessionId: 'browser-b',
      now: '2026-07-26T00:01:00.000Z',
      leaseSeconds: 60,
      jobDescriptionPath: path.join(value.root, 'missing-description.txt'),
    }));

    assert.equal(result.kind, 'recovered');
    assert.equal(result.run.runId, claimed.runId);
    assert.equal(result.run.jobId, claimed.jobId);
    assert.equal(result.run.ownerId, 'owner-b');
    assert.equal(result.run.browserSessionId, 'browser-b');
    assert.deepEqual(await readRows(value.database, 'SELECT count(*) AS count FROM application_runs'), [{ count: 1 }]);
  } finally {
    await removeFixture(value);
  }
});

test('startup rejects unknown options before recovering an active run', async () => {
  const value = await fixture([{ id: 6 }]);
  try {
    const claimed = await claimNextQueuedJob(value.database, claimOptions(value, {
      ownerId: 'owner-a',
      browserSessionId: 'browser-a',
    }));
    await assert.rejects(
      () => recoverOrClaimBacklogRun(value.database, {
        ...claimOptions(value, {
          ownerId: 'owner-b',
          browserSessionId: 'browser-b',
          now: '2026-07-26T00:01:00.000Z',
        }),
        unexpected: true,
      }),
      (error) => error?.code === 'E_STARTUP_OPTIONS_UNKNOWN_KEY',
    );

    assert.deepEqual(await readRows(value.database, 'SELECT id, owner_id, browser_session_id FROM application_runs'), [
      {
        id: claimed.runId,
        owner_id: 'owner-a',
        browser_session_id: 'browser-a',
      },
    ]);
  } finally {
    await removeFixture(value);
  }
});

test('startup claims through the existing atomic path when recovery is idle', async () => {
  const value = await fixture([{ id: 4 }]);
  try {
    const result = await recoverOrClaimBacklogRun(value.database, claimOptions(value));

    assert.equal(result.kind, 'claimed');
    assert.equal(result.run.jobId, 4);
    assert.equal(result.run.status, 'applying');
    assert.deepEqual(await readRows(value.database, 'SELECT count(*) AS count FROM application_runs WHERE active = 1'), [
      { count: 1 },
    ]);
  } finally {
    await removeFixture(value);
  }
});

test('startup returns idle when recovery and claim find no work', async () => {
  const value = await fixture([]);
  try {
    const result = await recoverOrClaimBacklogRun(value.database, claimOptions(value));

    assert.deepEqual(result, { kind: 'idle', run: null });
    assert.deepEqual(await readRows(value.database, 'SELECT count(*) AS count FROM application_runs'), [{ count: 0 }]);
  } finally {
    await removeFixture(value);
  }
});

test('invalid startup preflight fails before mutation when recovery is unavailable', async () => {
  const value = await fixture([{ id: 5 }]);
  try {
    await assert.rejects(() => recoverOrClaimBacklogRun(value.database, claimOptions(value, {
      jobDescriptionPath: path.join(value.root, 'missing-description.txt'),
    })));

    assert.deepEqual(await readRows(value.database, 'SELECT id, status FROM application_jobs WHERE id = 5'), [
      { id: 5, status: 'queued' },
    ]);
    assert.deepEqual(await readRows(value.database, 'SELECT count(*) AS count FROM application_runs'), [{ count: 0 }]);
  } finally {
    await removeFixture(value);
  }
});

test('claims jobs in deterministic order only after terminally releasing each active run', async () => {
  const value = await fixture([
    { id: 1, tier: 'active_verified', lastSeen: '2026-07-01T00:00:00.000Z' },
    { id: 2, tier: 'active_verified', lastSeen: '2026-07-03T00:00:00.000Z' },
    { id: 3, tier: 'unverified_stale', lastSeen: '2026-07-04T00:00:00.000Z' },
    { id: 4, tier: 'backfill_only', lastSeen: '2026-07-09T00:00:00.000Z' },
    { id: 5, tier: 'unverified_stale', lastSeen: '2026-07-04T00:00:00.000Z' },
  ]);
  try {
    const claimed = [];
    for (let index = 0; index < 5; index += 1) {
      const run = await claimNextQueuedJob(value.database, claimOptions(value, {
        now: `2026-07-26T00:00:0${index}.000Z`,
      }));
      assert.ok(run);
      claimed.push(run.jobId);
      const workspace = await createWorkspace(value, run);
      await persistTerminalOutcome(value.database, {
        runId: run.runId,
        ownerId: run.ownerId,
        browserSessionId: run.browserSessionId,
        jobId: run.jobId,
        status: 'closed',
        reasonCode: 'fixture_closed',
        finishedAt: `2026-07-26T00:00:1${index}.000Z`,
        finalUrl: `https://job-boards.greenhouse.io/fixture/jobs/${run.jobId}`,
        evidencePath: workspace.evidencePath,
        actionSummary: [{ action: 'review', outcome: 'succeeded' }],
        submitActionCount: 0,
      });
    }
    assert.deepEqual(claimed, [2, 1, 3, 5, 4]);
    assert.equal(await claimNextQueuedJob(value.database, claimOptions(value)), null);
    assert.deepEqual(
      await readRows(value.database, 'SELECT status, active FROM application_runs ORDER BY id'),
      [1, 2, 3, 4, 5].map(() => ({ status: 'closed', active: 0 })),
    );
  } finally {
    await removeFixture(value);
  }
});

test('concurrent claims enforce one global active job', async () => {
  const value = await fixture([{ id: 10 }, { id: 11 }]);
  try {
    const [first, second] = await Promise.all([
      claimNextQueuedJob(value.database, claimOptions(value, {
        ownerId: 'owner-a', browserSessionId: 'browser-a', now: '2026-07-26T01:00:00.000Z',
      })),
      claimNextQueuedJob(value.database, claimOptions(value, {
        ownerId: 'owner-b', browserSessionId: 'browser-b', now: '2026-07-26T01:00:00.000Z',
      })),
    ]);
    assert.equal([first, second].filter(Boolean).length, 1);
    assert.deepEqual(await readRows(value.database, 'SELECT count(*) AS count FROM application_runs WHERE active = 1'), [{ count: 1 }]);
    assert.deepEqual(await readRows(value.database, 'SELECT count(*) AS count FROM application_jobs WHERE status = \'queued\''), [{ count: 1 }]);
  } finally {
    await removeFixture(value);
  }
});

test('minimumJobId filters new claims without bypassing active recovery', async () => {
  const recoveryFixture = await fixture([{ id: 1 }, { id: 2 }]);
  try {
    const active = await claimNextQueuedJob(recoveryFixture.database, claimOptions(recoveryFixture));
    const recovered = await recoverOrClaimBacklogRun(
      recoveryFixture.database,
      claimOptions(recoveryFixture, { minimumJobId: 2 }),
    );
    assert.equal(active.jobId, 1);
    assert.equal(recovered.kind, 'recovered');
    assert.equal(recovered.run.jobId, 1);
  } finally {
    await removeFixture(recoveryFixture);
  }

  const claimFixture = await fixture([{ id: 1 }, { id: 2 }]);
  try {
    const claimed = await claimNextQueuedJob(
      claimFixture.database,
      claimOptions(claimFixture, { minimumJobId: 2 }),
    );
    assert.equal(claimed.jobId, 2);
    assert.deepEqual(await readRows(
      claimFixture.database,
      'SELECT id, status FROM application_jobs ORDER BY id',
    ), [
      { id: 1, status: 'queued' },
      { id: 2, status: 'claimed' },
    ]);
  } finally {
    await removeFixture(claimFixture);
  }
});

test('claim transaction rollback leaves the job and active-run table unchanged', async () => {
  const value = await fixture([{ id: 12 }]);
  try {
    await sqlite(value.database, `
CREATE TRIGGER fixture_abort_claim
BEFORE INSERT ON application_runs
WHEN NEW.owner_id = 'rollback-owner'
BEGIN
  SELECT RAISE(ABORT, 'fixture claim failure');
END;
`);
    await assert.rejects(() => claimNextQueuedJob(value.database, claimOptions(value, {
      ownerId: 'rollback-owner', browserSessionId: 'rollback-browser',
    })));
    assert.deepEqual(await readRows(value.database, 'SELECT id, status, claimed_at FROM application_jobs WHERE id = 12'), [
      { id: 12, status: 'queued', claimed_at: null },
    ]);
    assert.deepEqual(await readRows(value.database, 'SELECT count(*) AS count FROM application_runs'), [{ count: 0 }]);
  } finally {
    await removeFixture(value);
  }
});

test('same-owner repeated claim refreshes and preserves the active run identity', async () => {
  const value = await fixture([{ id: 20 }, { id: 21 }]);
  try {
    const first = await claimNextQueuedJob(value.database, claimOptions(value, {
      now: '2026-07-26T02:00:00.000Z', leaseSeconds: 60,
    }));
    const repeated = await claimNextQueuedJob(value.database, claimOptions(value, {
      now: '2026-07-26T02:00:30.000Z', leaseSeconds: 120,
    }));
    assertRunIdentity(repeated, first);
    assert.equal(repeated.status, 'applying');
    assert.notEqual(repeated.leaseExpiresAt, first.leaseExpiresAt);
    assert.equal((await readRows(value.database, 'SELECT count(*) AS count FROM application_runs'))[0].count, 1);
    assert.deepEqual(await readRows(value.database, 'SELECT id, status FROM application_jobs ORDER BY id'), [
      { id: 20, status: 'claimed' },
      { id: 21, status: 'queued' },
    ]);
  } finally {
    await removeFixture(value);
  }
});

test('live leases cannot be stolen by another owner', async () => {
  const value = await fixture([{ id: 30 }, { id: 31 }]);
  try {
    const first = await claimNextQueuedJob(value.database, claimOptions(value, {
      ownerId: 'owner-a', browserSessionId: 'browser-a', leaseSeconds: 120,
    }));
    const recovered = await recoverActiveRun(value.database, {
      ownerId: 'owner-b', browserSessionId: 'browser-b', now: '2026-07-26T00:01:00.000Z', leaseSeconds: 120,
    });
    assert.equal(recovered, null);
    const active = await readRows(value.database, 'SELECT owner_id, browser_session_id, status, active FROM application_runs');
    assert.deepEqual(active, [{ owner_id: 'owner-a', browser_session_id: 'browser-a', status: 'applying', active: 1 }]);
    assertRunIdentity(first, { ...first, ownerId: 'owner-a', browserSessionId: 'browser-a' });
  } finally {
    await removeFixture(value);
  }
});

test('concurrent stale recovery transfers one active row exactly once', async () => {
  const value = await fixture([{ id: 40 }]);
  try {
    const first = await claimNextQueuedJob(value.database, claimOptions(value, {
      ownerId: 'owner-a', browserSessionId: 'browser-a', leaseSeconds: 10,
    }));
    const [left, right] = await Promise.all([
      recoverActiveRun(value.database, {
        ownerId: 'owner-b', browserSessionId: 'browser-b', now: '2026-07-26T00:01:00.000Z', leaseSeconds: 60,
      }),
      recoverActiveRun(value.database, {
        ownerId: 'owner-c', browserSessionId: 'browser-c', now: '2026-07-26T00:01:00.000Z', leaseSeconds: 60,
      }),
    ]);
    const recovered = [left, right].filter(Boolean);
    assert.equal(recovered.length, 1);
    assert.equal(recovered[0].runId, first.runId);
    assert.ok(['owner-b', 'owner-c'].includes(recovered[0].ownerId));
    assert.deepEqual(await readRows(value.database, 'SELECT count(*) AS count FROM application_runs WHERE active = 1'), [{ count: 1 }]);
    assert.deepEqual(await readRows(value.database, 'SELECT count(*) AS count FROM application_runs'), [{ count: 1 }]);
  } finally {
    await removeFixture(value);
  }
});

test('stale-session mutations are fenced after recovery rotates ownership', async () => {
  const value = await fixture([{ id: 45 }]);
  try {
    const claimed = await claimNextQueuedJob(value.database, claimOptions(value, {
      ownerId: 'owner-a',
      browserSessionId: 'browser-a',
      leaseSeconds: 10,
    }));
    const recovered = await recoverActiveRun(value.database, {
      ownerId: 'owner-b',
      browserSessionId: 'browser-b',
      now: '2026-07-26T00:01:00.000Z',
      leaseSeconds: 60,
    });
    assertRunIdentity(recovered, { ...claimed, ownerId: 'owner-b', browserSessionId: 'browser-b' });
    const before = await readRows(value.database, 'SELECT status, active, owner_id, browser_session_id, blocker_alias FROM application_runs');

    await assert.rejects(() => heartbeatActiveRun(value.database, claimed.runId, {
      ownerId: 'owner-a',
      browserSessionId: 'browser-a',
      now: '2026-07-26T00:01:10.000Z',
      leaseSeconds: 60,
    }));
    await assert.rejects(() => pauseRunForUser(value.database, claimed.runId, {
      ownerId: 'owner-a',
      browserSessionId: 'browser-a',
      now: '2026-07-26T00:01:10.000Z',
      reason: 'stale_pause',
      questionAlias: 'work_authorization',
    }));
    await assert.rejects(() => persistTerminalOutcome(value.database, {
      runId: claimed.runId,
      ownerId: 'owner-a',
      browserSessionId: 'browser-a',
      jobId: claimed.jobId,
      status: 'closed',
      reasonCode: 'stale_terminal',
      finishedAt: '2026-07-26T00:01:10.000Z',
      finalUrl: 'https://job-boards.greenhouse.io/fixture/jobs/45',
      evidencePath: recovered.evidencePath,
      submitActionCount: 0,
    }));
    assert.deepEqual(
      before,
      await readRows(value.database, 'SELECT status, active, owner_id, browser_session_id, blocker_alias FROM application_runs'),
    );

    const paused = await pauseRunForUser(value.database, recovered.runId, {
      ownerId: 'owner-b',
      browserSessionId: 'browser-b',
      now: '2026-07-26T00:01:20.000Z',
      reason: 'needs_user_input',
      questionAlias: 'work_authorization',
    });
    assert.equal(paused.status, 'needs_user');
    await assert.rejects(() => resumeNeedsUserRun(value.database, recovered.runId, {
      ownerId: 'owner-a',
      browserSessionId: 'browser-a',
      now: '2026-07-26T00:01:30.000Z',
      leaseSeconds: 60,
      reason: 'stale_resume',
    }));
    assert.deepEqual(await readRows(value.database, 'SELECT status, active, owner_id, browser_session_id, blocker_alias FROM application_runs'), [
      { status: 'needs_user', active: 1, owner_id: 'owner-b', browser_session_id: 'browser-b', blocker_alias: 'work_authorization' },
    ]);
    assert.notDeepEqual(before, await readRows(value.database, 'SELECT status, active, owner_id, browser_session_id, blocker_alias FROM application_runs'));
  } finally {
    await removeFixture(value);
  }
});

test('restart recovery preserves run identity for the same owner and session', async () => {
  const value = await fixture([{ id: 50 }]);
  try {
    const claimed = await claimNextQueuedJob(value.database, claimOptions(value, {
      ownerId: 'restart-owner', browserSessionId: 'restart-browser', leaseSeconds: 120,
    }));
    const recovered = await recoverActiveRun(value.database, {
      ownerId: 'restart-owner', browserSessionId: 'restart-browser', now: '2026-07-26T00:00:30.000Z', leaseSeconds: 120,
    });
    assertRunIdentity(recovered, claimed);
    assert.equal(recovered.workspacePath, claimed.workspacePath);
    assert.equal(recovered.evidencePath, claimed.evidencePath);
    assert.equal(recovered.resumeArtifactPath, claimed.resumeArtifactPath);
    assert.equal(recovered.resumeArtifactSha256, claimed.resumeArtifactSha256);
    assert.equal((await readRows(value.database, 'SELECT count(*) AS count FROM application_runs'))[0].count, 1);
  } finally {
    await removeFixture(value);
  }
});

test('heartbeat keeps operational failures active and records a reason', async () => {
  const value = await fixture([{ id: 60 }]);
  try {
    const claimed = await claimNextQueuedJob(value.database, claimOptions(value));
    const heartbeat = await heartbeatActiveRun(value.database, claimed.runId, {
      ownerId: claimed.ownerId,
      browserSessionId: claimed.browserSessionId,
      now: '2026-07-26T00:00:20.000Z',
      leaseSeconds: 90,
      reason: 'browser_transport_failure',
    });
    assertRunIdentity(heartbeat, claimed);
    assert.equal(heartbeat.status, 'applying');
    assert.equal(heartbeat.reasonCode, 'browser_transport_failure');
    assert.equal(heartbeat.active, true);
    assert.deepEqual(await readRows(value.database, 'SELECT status, active, reason_code FROM application_runs'), [
      { status: 'applying', active: 1, reason_code: 'browser_transport_failure' },
    ]);
  } finally {
    await removeFixture(value);
  }
});

test('needs_user pauses the same active row and blocks every new claim', async () => {
  const value = await fixture([{ id: 70 }, { id: 71 }]);
  try {
    const claimed = await claimNextQueuedJob(value.database, claimOptions(value));
    const paused = await pauseRunForUser(value.database, claimed.runId, {
      ownerId: claimed.ownerId,
      browserSessionId: claimed.browserSessionId,
      now: '2026-07-26T00:00:10.000Z',
      reason: 'missing_required_fact',
      questionAlias: 'work_authorization',
    });
    assertRunIdentity(paused, claimed);
    assert.equal(paused.status, 'needs_user');
    assert.ok(Date.parse(paused.leaseExpiresAt) > Date.parse('2026-07-26T00:00:10.000Z'));
    const notRecovered = await recoverActiveRun(value.database, {
      ownerId: 'owner-b',
      browserSessionId: 'browser-b',
      now: '2026-07-26T00:00:20.000Z',
      leaseSeconds: 60,
    });
    assert.equal(notRecovered, null);
    const blockedClaim = await claimNextQueuedJob(value.database, claimOptions(value, {
      ownerId: 'owner-b', browserSessionId: 'browser-b', now: '2026-07-26T00:00:20.000Z',
    }));
    assert.equal(blockedClaim, null);
    assert.deepEqual(await readRows(value.database, 'SELECT id, status FROM application_jobs ORDER BY id'), [
      { id: 70, status: 'needs_user' },
      { id: 71, status: 'queued' },
    ]);
    assert.deepEqual(await readRows(value.database, 'SELECT status, active, reason_code, blocker_alias, lease_expires_at FROM application_runs'), [
      {
        status: 'needs_user',
        active: 1,
        reason_code: 'missing_required_fact',
        blocker_alias: 'work_authorization',
        lease_expires_at: paused.leaseExpiresAt,
      },
    ]);
  } finally {
    await removeFixture(value);
  }
});

test('resumeNeedsUserRun resumes the same row only after the persisted blocker answer exists', async () => {
  const value = await fixture([{ id: 80 }]);
  try {
    const claimed = await claimNextQueuedJob(value.database, claimOptions(value));
    const workspace = await createWorkspace(value, claimed);
    const contractSnapshot = await loadRunContractSnapshot(workspace.contractPath, { local: false });
    const blockerAlias = 'work_authorization';
    const approval_context = {
      run_contract_sha256: contractSnapshot.identity.sha256,
      observation_id: 'fixture-observation',
      field_id: 'fixture-field',
      alias: blockerAlias,
    };
    await pauseRunForUser(value.database, claimed.runId, {
      ownerId: claimed.ownerId,
      browserSessionId: claimed.browserSessionId,
      now: '2026-07-26T00:00:10.000Z',
      reason: 'needs_user_input',
      questionAlias: blockerAlias,
    });
    const resumeOptions = {
      ownerId: claimed.ownerId,
      browserSessionId: claimed.browserSessionId,
      now: '2026-07-26T00:00:20.000Z',
      leaseSeconds: 120,
      reason: 'user_provided_fact',
    };
    await assert.rejects(() => resumeNeedsUserRun(value.database, claimed.runId, resumeOptions));
    await assert.rejects(() => resumeNeedsUserRun(value.database, claimed.runId, {
      ...resumeOptions,
      accessControlResolved: true,
    }), { code: 'E_ACCESS_CONTROL_RESOLUTION' });
    assert.deepEqual(await readRows(value.database, 'SELECT status, active, blocker_alias FROM application_runs'), [
      { status: 'needs_user', active: 1, blocker_alias: blockerAlias },
    ]);

    await appendAnswerRecord(
      value.preflight.answerMemoryPath,
      createAnswerRecord({
        alias: blockerAlias,
        value: 'fixture-authorized',
        approved_at: '2026-07-26T00:00:15.000Z',
        approval_context,
        approval_context_sha256: approvalContextSha256(approval_context),
      }),
    );
    const resumed = await resumeNeedsUserRun(value.database, claimed.runId, resumeOptions);
    assertRunIdentity(resumed, claimed);
    assert.equal(resumed.status, 'applying');
    assert.equal((await readRows(value.database, 'SELECT count(*) AS count FROM application_runs'))[0].count, 1);
    assert.deepEqual(await readRows(value.database, 'SELECT status, claimed_at, completed_at FROM application_jobs WHERE id = 80'), [
      { status: 'claimed', claimed_at: '2026-07-26T00:00:00.000Z', completed_at: null },
    ]);
    assert.deepEqual(await readRows(value.database, `SELECT blocker_alias FROM application_runs WHERE id = ${claimed.runId}`), [
      { blocker_alias: null },
    ]);
  } finally {
    await removeFixture(value);
  }
});

test('resumeNeedsUserRun resumes an observed restored access-control surface without an answer record', async () => {
  const value = await fixture([{ id: 81 }]);
  try {
    const claimed = await claimNextQueuedJob(value.database, claimOptions(value));
    await createWorkspace(value, claimed);
    await pauseRunForUser(value.database, claimed.runId, {
      ownerId: claimed.ownerId,
      browserSessionId: claimed.browserSessionId,
      now: '2026-07-26T00:00:10.000Z',
      reason: 'third_party_access_control_required',
      questionAlias: 'linkedin_jobs_access_control',
    });

    const resumed = await resumeNeedsUserRun(value.database, claimed.runId, {
      ownerId: claimed.ownerId,
      browserSessionId: claimed.browserSessionId,
      now: '2026-07-26T00:00:20.000Z',
      leaseSeconds: 120,
      reason: 'third_party_access_restored',
      accessControlResolved: true,
    });
    assertRunIdentity(resumed, claimed);
    assert.equal(resumed.status, 'applying');
    assert.equal((await readRows(value.database, 'SELECT count(*) AS count FROM application_runs'))[0].count, 1);
    assert.deepEqual(await readRows(value.database, `SELECT reason_code, blocker_alias FROM application_runs WHERE id = ${claimed.runId}`), [
      { reason_code: 'third_party_access_restored', blocker_alias: null },
    ]);
  } finally {
    await removeFixture(value);
  }
});

test('skipNeedsUserRun atomically releases a needs_user CAPTCHA run', async () => {
  const value = await fixture([{ id: 81 }]);
  try {
    const claimed = await claimNextQueuedJob(value.database, claimOptions(value));
    await createWorkspace(value, claimed);
    await pauseRunForUser(value.database, claimed.runId, {
      ownerId: claimed.ownerId,
      browserSessionId: claimed.browserSessionId,
      now: '2026-07-26T00:00:10.000Z',
      reason: 'captcha_required',
      questionAlias: 'layup_captcha',
    });
    const skipped = await skipNeedsUserRun(value.database, claimed.runId, {
      ownerId: claimed.ownerId,
      browserSessionId: claimed.browserSessionId,
      now: '2026-07-26T00:00:20.000Z',
      reason: 'captcha_unsolved',
    });
    assert.equal(skipped.status, 'skipped');
    assert.equal(skipped.active, false);
    assert.equal(skipped.leaseExpiresAt, null);
    assert.deepEqual(await readRows(value.database, `
      SELECT r.status, r.active, r.blocker_alias, r.lease_expires_at, j.status AS job_status
      FROM application_runs AS r JOIN application_jobs AS j ON j.id = r.job_id
    `), [{
      status: 'skipped',
      active: 0,
      blocker_alias: null,
      lease_expires_at: null,
      job_status: 'skipped',
    }]);
    await assert.rejects(() => skipNeedsUserRun(value.database, claimed.runId, {
      ownerId: 'wrong-owner',
      browserSessionId: claimed.browserSessionId,
      now: '2026-07-26T00:00:30.000Z',
      reason: 'captcha_unsolved',
    }));
  } finally {
    await removeFixture(value);
  }
});

test('workspace keeps contract and evidence private and distinct', async () => {
  const value = await fixture([{ id: 90 }]);
  try {
    const run = await claimNextQueuedJob(value.database, claimOptions(value));
    const workspace = await createWorkspace(value, run);
    assert.equal(workspace.contractPath, path.join(run.workspacePath, 'contract.json'));
    assert.equal(workspace.evidencePath, path.join(run.workspacePath, 'evidence'));
    assert.notEqual(workspace.contractPath, workspace.evidencePath);
    assert.equal(workspace.contractPath, path.join(workspace.workspacePath, 'contract.json'));
    assert.equal(workspace.evidencePath, path.join(workspace.workspacePath, 'evidence'));
    assert.equal((await fsp.stat(workspace.workspacePath)).mode & 0o777, 0o700);
    assert.equal((await fsp.stat(workspace.contractPath)).mode & 0o777, 0o600);
    assert.equal((await fsp.stat(workspace.evidencePath)).mode & 0o777, 0o700);
    const contract = JSON.parse(await fsp.readFile(workspace.contractPath, 'utf8'));
    assert.equal(contract.run_artifact_dir, workspace.evidencePath);
    assert.equal(contract.application_url, 'https://job-boards.greenhouse.io/fixture/jobs/90');
    assert.equal(Object.hasOwn(contract, 'description'), false);
    assert.equal(Object.hasOwn(contract, 'applicant_name'), false);
    assert.equal((await fsp.readFile(workspace.contractPath, 'utf8')).includes('fixture job description'), false);
    assert.equal((await fsp.readFile(workspace.contractPath, 'utf8')).includes('phase1-profile-v1'), false);
    const snapshot = JSON.parse(await fsp.readFile(workspace.jobSnapshotPath, 'utf8'));
    assert.equal(snapshot.job_title, run.jobTitle);
    assert.equal(snapshot.job_company, run.jobCompany);
    assert.equal(snapshot.job_location, run.jobLocation);
    assert.equal(snapshot.job_description, run.jobDescription);
    assert.equal(snapshot.job_description_sha256, run.jobDescriptionSha256);
  } finally {
    await removeFixture(value);
  }
});

test('workspace creation recovers partial crashes idempotently without overwriting artifacts', async () => {
  const value = await fixture([{ id: 91 }]);
  try {
    const run = await claimNextQueuedJob(value.database, claimOptions(value));
    const initial = await createWorkspace(value, run);
    const expectedContract = await fsp.readFile(initial.contractPath);
    const expectedSnapshot = await fsp.readFile(initial.jobSnapshotPath);

    await fsp.rm(initial.jobSnapshotPath);
    const recovered = await createWorkspace(value, run);
    assert.deepEqual(await fsp.readFile(recovered.contractPath), expectedContract);
    assert.deepEqual(await fsp.readFile(recovered.jobSnapshotPath), expectedSnapshot);
    const completeContract = await fsp.readFile(recovered.contractPath);
    const completeSnapshot = await fsp.readFile(recovered.jobSnapshotPath);

    await createWorkspace(value, run);
    assert.deepEqual(await fsp.readFile(recovered.contractPath), completeContract);
    assert.deepEqual(await fsp.readFile(recovered.jobSnapshotPath), completeSnapshot);

    const mismatch = JSON.stringify({
      ...JSON.parse(expectedContract.toString('utf8')),
      application_url: 'https://job-boards.greenhouse.io/fixture/jobs/mismatch',
    }) + '\n';
    await fsp.writeFile(recovered.contractPath, mismatch, { mode: 0o600 });
    await fsp.chmod(recovered.contractPath, 0o600);
    await assert.rejects(() => createWorkspace(value, run));
    assert.equal(await fsp.readFile(recovered.contractPath, 'utf8'), mismatch);

    await fsp.writeFile(recovered.contractPath, expectedContract, { mode: 0o600 });
    await fsp.chmod(recovered.contractPath, 0o600);
    await fsp.rm(recovered.jobSnapshotPath);
    const outsideArtifact = path.join(value.root, 'outside-job.json');
    await privateInput(outsideArtifact, 'outside artifact');
    await fsp.symlink(outsideArtifact, recovered.jobSnapshotPath);
    await assert.rejects(() => createWorkspace(value, run));
    assert.equal((await fsp.lstat(recovered.jobSnapshotPath)).isSymbolicLink(), true);
    assert.equal(await fsp.readFile(outsideArtifact, 'utf8'), 'outside artifact');
  } finally {
    await removeFixture(value);
  }
});

test('terminal persistence updates the active row without creating a duplicate', async () => {
  const value = await fixture([{ id: 100 }]);
  try {
    const run = await claimNextQueuedJob(value.database, claimOptions(value));
    const workspace = await createWorkspace(value, run);
    const outcome = await persistTerminalOutcome(value.database, {
      runId: run.runId,
      ownerId: run.ownerId,
      browserSessionId: run.browserSessionId,
      jobId: run.jobId,
      status: 'closed',
      reasonCode: 'posting_closed',
      finishedAt: '2026-07-26T00:01:00.000Z',
      finalUrl: 'https://job-boards.greenhouse.io/fixture/jobs/100',
      evidencePath: workspace.evidencePath,
      actionSummary: [{ action: 'review', outcome: 'succeeded' }],
      submitActionCount: 0,
    });
    assert.equal(outcome.runId, run.runId);
    assert.equal(outcome.status, 'closed');
    assert.deepEqual(await readRows(value.database, 'SELECT id, active, status FROM application_runs'), [
      { id: run.runId, active: 0, status: 'closed' },
    ]);
    assert.deepEqual(await readRows(value.database, 'SELECT id, status FROM application_jobs WHERE id = 100'), [
      { id: 100, status: 'closed' },
    ]);
  } finally {
    await removeFixture(value);
  }
});

test('diagnosed failed job requeues without rewriting terminal run history', async () => {
  const value = await fixture([{ id: 101 }]);
  try {
    const first = await claimNextQueuedJob(value.database, claimOptions(value));
    const workspace = await createWorkspace(value, first);
    await persistTerminalOutcome(value.database, {
      runId: first.runId,
      ownerId: first.ownerId,
      browserSessionId: first.browserSessionId,
      jobId: first.jobId,
      status: 'failed',
      reasonCode: 'browser_input_unavailable',
      finishedAt: '2026-07-26T00:01:00.000Z',
      finalUrl: 'https://example.test/jobs/101',
      evidencePath: workspace.evidencePath,
      actionSummary: [{ action: 'fill', outcome: 'failed' }],
    });

    const requeued = await requeueTerminalJob(value.database, first.jobId, {
      reason: 'browser_input_restored',
    });
    assert.deepEqual(requeued, {
      jobId: first.jobId,
      status: 'queued',
      reasonCode: 'browser_input_restored',
    });

    const second = await claimNextQueuedJob(value.database, claimOptions(value, {
      now: '2026-07-26T00:02:00.000Z',
    }));
    assert.equal(second.jobId, first.jobId);
    assert.notEqual(second.runId, first.runId);
    assert.deepEqual(await readRows(
      value.database,
      'SELECT id, status, active FROM application_runs ORDER BY id',
    ), [
      { id: first.runId, status: 'failed', active: 0 },
      { id: second.runId, status: 'applying', active: 1 },
    ]);
  } finally {
    await removeFixture(value);
  }
});

test('canonical completion is evidence-derived and binds URL, evidence directory, and resume identity', async (t) => {
  await t.test('valid evidence completes the exact application URL', async () => {
    const value = await fixture([{ id: 110 }]);
    try {
      const run = await claimNextQueuedJob(value.database, claimOptions(value));
      const workspace = await createWorkspace(value, run);
      await publishCanonicalCompletion({
        root: value.root,
        evidencePath: workspace.evidencePath,
        applicationUrl: 'https://job-boards.greenhouse.io/fixture/jobs/110',
        contractPath: workspace.contractPath,
        resumeUploadPath: value.preflight.resumeUploadPath,
      });
      const outcome = await persistTerminalOutcome(value.database, {
        runId: run.runId,
        ownerId: run.ownerId,
        browserSessionId: run.browserSessionId,
        jobId: run.jobId,
        status: 'completed',
        reasonCode: 'omp_submission_succeeded',
        finishedAt: '2026-07-26T00:01:00.000Z',
        finalUrl: 'https://job-boards.greenhouse.io/fixture/jobs/110',
        evidencePath: workspace.evidencePath,
      });
      assert.equal(outcome.status, 'completed');
      assert.equal(outcome.submitActionCount, 1);
      assert.deepEqual(outcome.actions, [{ action: 'final_submit', outcome: 'succeeded' }]);
      assert.equal(outcome.resumeArtifactPath, path.resolve(value.preflight.resumeUploadPath));
      assert.match(outcome.resumeArtifactSha256, /^[0-9a-f]{64}$/u);
      assert.deepEqual(await readRows(value.database, 'SELECT id, status, active, submit_action_count FROM application_runs'), [
        { id: run.runId, status: 'completed', active: 0, submit_action_count: 1 },
      ]);
    } finally {
      await removeFixture(value);
    }
  });

  await t.test('post-submit confirmation redirect is accepted when evidence binds the claimed application', async () => {
    const value = await fixture([{ id: 111 }]);
    try {
      const run = await claimNextQueuedJob(value.database, claimOptions(value));
      const workspace = await createWorkspace(value, run);
      await publishCanonicalCompletion({
        root: value.root,
        evidencePath: workspace.evidencePath,
        applicationUrl: 'https://job-boards.greenhouse.io/fixture/jobs/111',
        contractPath: workspace.contractPath,
        resumeUploadPath: value.preflight.resumeUploadPath,
        finalUrl: 'https://example.test/jobs/111/confirmation',
      });
      const outcome = await persistTerminalOutcome(value.database, {
        runId: run.runId,
        ownerId: run.ownerId,
        browserSessionId: run.browserSessionId,
        jobId: run.jobId,
        status: 'completed',
        reasonCode: 'omp_submission_succeeded',
        finishedAt: '2026-07-26T00:01:00.000Z',
        finalUrl: 'https://job-boards.greenhouse.io/fixture/jobs/111/confirmation',
        evidencePath: workspace.evidencePath,
      });
      assert.equal(outcome.status, 'completed');
      assert.equal(outcome.finalUrl, 'https://job-boards.greenhouse.io/fixture/jobs/111/confirmation');
      assert.deepEqual(await readRows(value.database, 'SELECT status, active FROM application_runs'), [
        { status: 'completed', active: 0 },
      ]);
    } finally {
      await removeFixture(value);
    }
  });

  await t.test('evidence from a different application is rejected', async () => {
    const value = await fixture([{ id: 114 }]);
    try {
      const run = await claimNextQueuedJob(value.database, claimOptions(value));
      const workspace = await createWorkspace(value, run);
      await publishCanonicalCompletion({
        root: value.root,
        evidencePath: workspace.evidencePath,
        applicationUrl: 'https://job-boards.greenhouse.io/fixture/jobs/different-application',
        contractPath: workspace.contractPath,
        resumeUploadPath: value.preflight.resumeUploadPath,
        finalUrl: 'https://example.test/jobs/114/confirmation',
      });
      await assert.rejects(() => persistTerminalOutcome(value.database, {
        runId: run.runId,
        ownerId: run.ownerId,
        browserSessionId: run.browserSessionId,
        jobId: run.jobId,
        status: 'completed',
        reasonCode: 'omp_submission_succeeded',
        finishedAt: '2026-07-26T00:01:00.000Z',
        finalUrl: 'https://job-boards.greenhouse.io/fixture/jobs/114/confirmation',
        evidencePath: workspace.evidencePath,
      }));
      assert.deepEqual(await readRows(value.database, 'SELECT status, active FROM application_runs'), [
        { status: 'applying', active: 1 },
      ]);
    } finally {
      await removeFixture(value);
    }
  });

  await t.test('evidence with a stale contract digest is rejected', async () => {
    const value = await fixture([{ id: 115 }]);
    try {
      const run = await claimNextQueuedJob(value.database, claimOptions(value));
      const workspace = await createWorkspace(value, run);
      await publishCanonicalCompletion({
        root: value.root,
        evidencePath: workspace.evidencePath,
        applicationUrl: 'https://job-boards.greenhouse.io/fixture/jobs/115',
        contractPath: workspace.contractPath,
        contractSha256: 'f'.repeat(64),
        resumeUploadPath: value.preflight.resumeUploadPath,
      });
      await assert.rejects(() => persistTerminalOutcome(value.database, {
        runId: run.runId,
        ownerId: run.ownerId,
        browserSessionId: run.browserSessionId,
        jobId: run.jobId,
        status: 'completed',
        reasonCode: 'omp_submission_succeeded',
        finishedAt: '2026-07-26T00:01:00.000Z',
        finalUrl: 'https://job-boards.greenhouse.io/fixture/jobs/115',
        evidencePath: workspace.evidencePath,
      }));
      assert.deepEqual(await readRows(value.database, 'SELECT status, active FROM application_runs'), [
        { status: 'applying', active: 1 },
      ]);
    } finally {
      await removeFixture(value);
    }
  });

  await t.test('different evidence directory is rejected', async () => {
    const value = await fixture([{ id: 112 }]);
    try {
      const run = await claimNextQueuedJob(value.database, claimOptions(value));
      const workspace = await createWorkspace(value, run);
      const wrongEvidencePath = path.join(value.root, 'wrong-evidence');
      await fsp.mkdir(wrongEvidencePath, { mode: 0o700 });
      await assert.rejects(() => persistTerminalOutcome(value.database, {
        runId: run.runId,
        ownerId: run.ownerId,
        browserSessionId: run.browserSessionId,
        jobId: run.jobId,
        status: 'completed',
        reasonCode: 'omp_submission_succeeded',
        finishedAt: '2026-07-26T00:01:00.000Z',
        finalUrl: 'https://job-boards.greenhouse.io/fixture/jobs/112',
        evidencePath: wrongEvidencePath,
      }));
      assert.deepEqual(await readRows(value.database, 'SELECT status, active FROM application_runs'), [
        { status: 'applying', active: 1 },
      ]);
      assert.equal(workspace.evidencePath, run.evidencePath);
    } finally {
      await removeFixture(value);
    }
  });

  await t.test('resume artifact identity in evidence must match the claimed selection', async () => {
    const value = await fixture([{ id: 113 }]);
    try {
      const run = await claimNextQueuedJob(value.database, claimOptions(value));
      const workspace = await createWorkspace(value, run);
      const wrongResumePath = path.join(value.root, 'wrong-resume.pdf');
      await privateInput(wrongResumePath, '%PDF-1.7\nwrong selected resume\n');
      await publishCanonicalCompletion({
        root: value.root,
        evidencePath: workspace.evidencePath,
        applicationUrl: 'https://job-boards.greenhouse.io/fixture/jobs/113',
        contractPath: workspace.contractPath,
        resumeUploadPath: wrongResumePath,
      });
      await assert.rejects(() => persistTerminalOutcome(value.database, {
        runId: run.runId,
        ownerId: run.ownerId,
        jobId: run.jobId,
        browserSessionId: run.browserSessionId,
        status: 'completed',
        reasonCode: 'omp_submission_succeeded',
        finishedAt: '2026-07-26T00:01:00.000Z',
        finalUrl: 'https://job-boards.greenhouse.io/fixture/jobs/113',
        evidencePath: workspace.evidencePath,
      }));
      assert.deepEqual(await readRows(value.database, 'SELECT status, active FROM application_runs'), [
        { status: 'applying', active: 1 },
      ]);
    } finally {
      await removeFixture(value);
    }
  });
});
