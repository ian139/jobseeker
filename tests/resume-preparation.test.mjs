import assert from 'node:assert/strict';
import crypto from 'node:crypto';
import { DatabaseSync } from 'node:sqlite';
import { promises as fsp, realpathSync } from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import test from 'node:test';

import { initializeIngestionDatabase } from '../src/ingestion/database.mjs';
import { recoverPrepareOrClaimBacklogRun } from '../src/phase1/application-orchestrator.mjs';
import {
  prepareNextQueuedResume,
  ResumePreparationError,
} from '../src/phase1/resume-preparation.mjs';

const NOW = '2026-08-04T10:00:00.000Z';
const DESCRIPTION = 'Build reliable job automation with deterministic evidence.';

function sha256(value) {
  return crypto.createHash('sha256').update(value).digest('hex');
}

async function privateFile(filePath, contents) {
  await fsp.writeFile(filePath, contents, { mode: 0o600 });
  await fsp.chmod(filePath, 0o600);
}

function fakeGeneratorSource({ tamperDescription = false } = {}) {
  return `#!/usr/bin/env python3
import hashlib, json, os, pathlib, sys

def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")

def digest(value):
    return hashlib.sha256(value).hexdigest()

args = sys.argv[1:]
root = pathlib.Path(args[args.index("--output-root") + 1])
job = json.loads(sys.stdin.buffer.read().decode("utf-8"))
job_payload = {key: job[key] for key in ("id", "title", "company", "description", "location", "posted_at")}
directory = root / ("job-" + str(job["id"])) / "artifact-fixture"
directory.mkdir(parents=True, exist_ok=True, mode=0o700)
os.chmod(directory.parent, 0o700)
os.chmod(directory, 0o700)
files = {
    "resume.tex": b"fixture tex",
    "resume.pdf": b"%PDF-1.7\\nfixture one-page pdf\\n",
    "optimization.json": b"{}",
    "job_description.txt": (job["description"] + ${JSON.stringify(tamperDescription ? ' + " tampered"' : '')}).encode("utf-8"),
}
artifacts = {}
for name, data in files.items():
    target = directory / name
    target.write_bytes(data)
    os.chmod(target, 0o600)
    artifacts[name] = {"bytes": len(data), "sha256": digest(data)}
manifest = {
    "schema_version": 1,
    "generator_schema_version": "fixture-generator-v1",
    "algorithm_sha256": "c" * 64,
    "fingerprint": "d" * 64,
    "job_id": job["id"],
    "inputs": {
        "job_sha256": digest(canonical(job_payload)),
        "profile_sha256": "e" * 64,
        "template_sha256": "f" * 64,
        "skill_sha256": "a" * 64,
        "compiler_identity": "fixture-compiler",
    },
    "artifacts": artifacts,
    "manifest_sha256": "",
}
manifest["manifest_sha256"] = digest(canonical({key: value for key, value in manifest.items() if key != "manifest_sha256"}))
manifest_path = directory / "manifest.json"
manifest_path.write_bytes(canonical(manifest))
os.chmod(manifest_path, 0o600)
result = {
    "schema": "generated-resume-v1",
    "job_id": job["id"],
    "artifact_ref": "d" * 64,
    "tex_path": str(directory / "resume.tex"),
    "pdf_path": str(directory / "resume.pdf"),
    "report_path": str(directory / "optimization.json"),
    "manifest_path": str(manifest_path),
    "pages": 1,
    "field": "software",
    "graduation_date": "2020",
    "matched_keywords": [],
}
sys.stdout.buffer.write(canonical(result))
`;
}

async function fixture(options = {}) {
  const root = await fsp.mkdtemp(path.join(os.tmpdir(), 'resume-preparation-test-'));
  await fsp.chmod(root, 0o700);
  const database = path.join(root, 'jobs.sqlite');
  const outputRoot = path.join(root, 'artifacts');
  const generator = path.join(root, 'fake-generator');
  const applicantProfile = path.join(root, 'applicant-profile.json');
  const sourceResume = path.join(root, 'source.pdf');
  const answerMemory = path.join(root, 'answer-memory.jsonl');
  const workspaceRoot = path.join(root, 'workspaces');
  const profile = path.join(root, 'profile.json');
  const template = path.join(root, 'resume.tex');
  const skill = path.join(root, 'SKILL.md');
  await fsp.mkdir(outputRoot, { mode: 0o700 });
  await privateFile(generator, fakeGeneratorSource(options));
  await fsp.chmod(generator, 0o700);
  await privateFile(profile, '{}');
  await privateFile(template, 'fixture');
  await privateFile(skill, 'fixture');
  initializeIngestionDatabase(database, { now: NOW });
  const db = new DatabaseSync(database);
  await privateFile(applicantProfile, JSON.stringify({ schema: 'phase1-profile-v1' }));
  await privateFile(sourceResume, '%PDF-1.7\nfixture source resume\n');
  await privateFile(answerMemory, '');
  const descriptionSha256 = sha256(Buffer.from(DESCRIPTION));
  db.prepare('INSERT INTO dedupe_groups (id,identity_kind,identity_key,review_required,created_at,updated_at) VALUES (1,?,?,?,?,?)')
    .run('application_url', 'https://job-boards.greenhouse.io/example/jobs/1', 0, NOW, NOW);
  db.prepare(`INSERT INTO jobs (
    id,source,source_job_id,canonical_listing_url,canonical_application_url,ats_kind,ats_identifier,
    title,company,location,workplace_type,employment_types_json,description,description_sha256,
    source_posted_at,source_updated_at,discovered_at,first_seen_at,last_seen_at,availability_state,
    freshness_state,eligibility_state,eligibility_reason_codes_json,priority,dedupe_group_id,
    raw_payload_path,raw_payload_sha256
  ) VALUES (1,'fixture','job-1','https://job-boards.greenhouse.io/example/jobs/1','https://job-boards.greenhouse.io/example/jobs/1','greenhouse',NULL,
    'Engineer','Example','Remote','remote','[]',?,?,?,NULL,?,?,?,'open','current','eligible','[]',100,1,?,?)`)
    .run(DESCRIPTION, descriptionSha256, NOW, NOW, NOW, NOW, path.join(root, 'raw.json'), 'b'.repeat(64));
  db.prepare(`INSERT INTO application_jobs (
    id,source_table,source_db,source_rowid,source_job_id,application_url,eligibility_tier,
    verification_reason,source_posted_at,source_last_seen_at,status,dedupe_group_id
  ) VALUES (1,'jobs',?,1,'job-1','https://job-boards.greenhouse.io/example/jobs/1','active_verified','fixture',?,?,'queued',1)`)
    .run(database, NOW, NOW);
  db.prepare(`UPDATE application_jobs SET
    platform = 'greenhouse',
    application_host = 'job-boards.greenhouse.io',
    job_title = 'Engineer',
    job_company = 'Example',
    job_location = 'Remote',
    job_description = ?,
    job_description_sha256 = ?`).run(DESCRIPTION, descriptionSha256);
  db.close();
  return {
    root,
    database,
    options: {
      pythonExecutable: generator,
      resumeProfilePath: profile,
      resumeTemplatePath: template,
      resumeSkillPath: skill,
      resumeOutputRoot: realpathSync(outputRoot),
      now: NOW,
      timeoutMs: 10_000,
    },
    applicationOptions: {
      ownerId: 'fixture-owner',
      browserSessionId: 'fixture-browser',
      now: NOW,
      leaseSeconds: 300,
      maxActiveJobs: 1,
      workspaceRoot,
      applicantProfilePath: applicantProfile,
      sourceResumePath: sourceResume,
      answerMemoryPath: answerMemory,
    },
  };
}

test('preparation verifies and atomically binds one immutable resume artifact', async (t) => {
  const value = await fixture();
  t.after(() => fsp.rm(value.root, { recursive: true, force: true }));

  const first = await prepareNextQueuedResume(value.database, value.options);
  const second = await prepareNextQueuedResume(value.database, value.options);

  assert.equal(first.applicationJobId, 1);
  assert.equal(first.pages, 1);
  assert.equal(first.reused, false);
  assert.equal(second.resumeArtifactId, first.resumeArtifactId);
  assert.equal(second.reused, true);
  assert.equal(Object.isFrozen(first), true);
  const db = new DatabaseSync(value.database);
  assert.deepEqual({ ...db.prepare('SELECT resume_preparation_state,current_resume_artifact_id FROM application_jobs WHERE id=1').get() }, {
    resume_preparation_state: 'ready',
    current_resume_artifact_id: first.resumeArtifactId,
  });
  assert.equal(db.prepare('SELECT count(*) AS count FROM resume_artifacts').get().count, 1);
  assert.equal(db.prepare('PRAGMA foreign_key_check').all().length, 0);
  db.close();
});

test('description mismatch fails closed and leaves the queued job retryable', async (t) => {
  const value = await fixture({ tamperDescription: true });
  t.after(() => fsp.rm(value.root, { recursive: true, force: true }));

  await assert.rejects(
    prepareNextQueuedResume(value.database, value.options),
    (error) => error instanceof ResumePreparationError && error.code === 'resume_description_binding_mismatch',
  );
  const db = new DatabaseSync(value.database);
  assert.deepEqual({ ...db.prepare('SELECT status,resume_preparation_state,resume_preparation_reason,current_resume_artifact_id FROM application_jobs WHERE id=1').get() }, {
    status: 'queued',
    resume_preparation_state: 'failed',
    resume_preparation_reason: 'resume_description_binding_mismatch',
    current_resume_artifact_id: null,
  });
  assert.equal(db.prepare('SELECT count(*) AS count FROM application_runs').get().count, 0);
  db.close();
});

test('orchestrator targets one queued application job without changing queue order', async (t) => {
  const value = await fixture();
  t.after(() => fsp.rm(value.root, { recursive: true, force: true }));
  const description = 'Build a second deterministic application workflow.';
  const db = new DatabaseSync(value.database);
  const description2Sha256 = sha256(Buffer.from(description));
  db.prepare('INSERT INTO dedupe_groups (id,identity_kind,identity_key,review_required,created_at,updated_at) VALUES (2,?,?,?,?,?)')
    .run('application_url', 'https://job-boards.greenhouse.io/example/jobs/2', 0, NOW, NOW);
  db.prepare(`INSERT INTO jobs (
    id,source,source_job_id,canonical_listing_url,canonical_application_url,ats_kind,ats_identifier,
    title,company,location,workplace_type,employment_types_json,description,description_sha256,
    source_posted_at,source_updated_at,discovered_at,first_seen_at,last_seen_at,availability_state,
    freshness_state,eligibility_state,eligibility_reason_codes_json,priority,dedupe_group_id,
    raw_payload_path,raw_payload_sha256
  ) VALUES (2,'fixture','job-2','https://job-boards.greenhouse.io/example/jobs/2','https://job-boards.greenhouse.io/example/jobs/2','greenhouse',NULL,
    'Second Engineer','Example','Remote','remote','[]',?,?,?,NULL,?,?,?,'open','current','eligible','[]',100,2,?,?)`)
    .run(description, description2Sha256, NOW, NOW, NOW, NOW, path.join(value.root, 'raw-2.json'), 'c'.repeat(64));
  db.prepare(`INSERT INTO application_jobs (
    id,source_table,source_db,source_rowid,source_job_id,application_url,eligibility_tier,
    verification_reason,source_posted_at,source_last_seen_at,status,dedupe_group_id
  ) VALUES (2,'jobs',?,2,'job-2','https://job-boards.greenhouse.io/example/jobs/2','active_verified','fixture',?,?,'queued',2)`)
    .run(value.database, NOW, NOW);
  db.prepare(`UPDATE application_jobs SET
    platform = 'greenhouse',
    application_host = 'job-boards.greenhouse.io',
    job_title = 'Second Engineer',
    job_company = 'Example',
    job_location = 'Remote',
    job_description = ?,
    job_description_sha256 = ?
    WHERE id = 2`).run(description, description2Sha256);
  db.close();

  const result = await recoverPrepareOrClaimBacklogRun(value.database, {
    ...value.applicationOptions,
    applicationJobId: 2,
    resumePreparation: value.options,
  });

  assert.equal(result.kind, 'claimed');
  assert.equal(result.run.jobId, 2);
  const persisted = new DatabaseSync(value.database);
  assert.deepEqual({ ...persisted.prepare('SELECT status,resume_preparation_state FROM application_jobs WHERE id=1').get() }, {
    status: 'queued',
    resume_preparation_state: 'pending',
  });
  assert.deepEqual({ ...persisted.prepare('SELECT status,resume_preparation_state FROM application_jobs WHERE id=2').get() }, {
    status: 'claimed',
    resume_preparation_state: 'ready',
  });
  persisted.close();
});
test('orchestrator claims only the exact persisted resume binding', async (t) => {
  const value = await fixture();
  t.after(() => fsp.rm(value.root, { recursive: true, force: true }));

  const result = await recoverPrepareOrClaimBacklogRun(value.database, {
    ...value.applicationOptions,
    resumePreparation: value.options,
  });

  assert.equal(result.kind, 'claimed');
  assert.equal(result.run.jobId, result.preparation.applicationJobId);
  assert.equal(result.run.resumeArtifactId, result.preparation.resumeArtifactId);
  assert.equal(result.run.resumeArtifactPath, result.preparation.pdfPath);
  assert.equal(result.run.resumeArtifactSha256, result.preparation.pdfSha256);
});


