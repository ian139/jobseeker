import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { spawn } from 'node:child_process';
import { promises as fsp } from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import test from 'node:test';

import { claimSpecificQueuedJob } from '../src/phase1/backlog-runner.mjs';
import { canonicalJson, createAnswerRecord } from '../src/phase1/contract.mjs';
import {
  generateBoundResume,
  prepareOrRecoverSupportedRun,
} from '../src/phase1/preparation.mjs';
import {
  ingestSupportedJobs,
  listBoundQueuedJobs,
  loadBoundJob,
  quarantineUnsupportedQueuedJobs,
} from '../src/phase1/job-source.mjs';

const SQLITE = 'sqlite3';
const PROJECT_ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const FIXED_NOW = '2026-07-28T00:00:00.000Z';
const GREENHOUSE_URL = 'https://job-boards.greenhouse.io/synthetic-co/jobs/101';
const ASHBY_EXTERNAL_ID = '123e4567-e89b-12d3-a456-426614174000';
const ASHBY_URL = `https://jobs.ashbyhq.com/synthetic-org/${ASHBY_EXTERNAL_ID}`;
const GREENHOUSE_DESCRIPTION = 'Build Python API services for synthetic applicants. Use SQL and FastAPI. Spring internship role.';
const ASHBY_DESCRIPTION = 'Design React dashboards for synthetic applicants. Partner with TypeScript and accessibility.';

function sql(value) {
  if (value === null || value === undefined) return 'NULL';
  if (typeof value === 'number') {
    assert.ok(Number.isSafeInteger(value));
    return String(value);
  }
  return `'${String(value).replaceAll("'", "''")}'`;
}

function sha256(value) {
  return createHash('sha256').update(value, 'utf8').digest('hex');
}

function sha256Bytes(value) {
  return createHash('sha256').update(value).digest('hex');
}

async function sqlite(database, statement) {
  const stdout = await new Promise((resolve, reject) => {
    const child = spawn(SQLITE, ['-bail', '-json', database], {
      stdio: ['pipe', 'pipe', 'pipe'],
      windowsHide: true,
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

async function applyHistoricalSchema(database) {
  await sqlite(database, `
PRAGMA foreign_keys = ON;
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
  status TEXT NOT NULL DEFAULT 'queued' CHECK (status IN ('queued','claimed','ready_for_human','blocked','closed','skipped','failed')),
  status_reason TEXT,
  claimed_at TEXT,
  completed_at TEXT,
  UNIQUE(source_table, source_db, source_rowid),
  UNIQUE(application_url)
);
CREATE TABLE application_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  job_id INTEGER NOT NULL REFERENCES application_jobs(id) ON DELETE RESTRICT,
  status TEXT NOT NULL CHECK (status IN ('preparing','ready_for_human','blocked','closed','failed')),
  reason_code TEXT NOT NULL,
  started_at TEXT NOT NULL,
  finished_at TEXT,
  final_url TEXT,
  actions_json TEXT NOT NULL DEFAULT '[]',
  evidence_path TEXT NOT NULL,
  submit_action_count INTEGER NOT NULL DEFAULT 0 CHECK (submit_action_count = 0)
);
INSERT INTO application_jobs (
  id, source_table, source_db, source_rowid, source_job_id, application_url,
  eligibility_tier, verification_reason, source_posted_at, source_last_seen_at,
  status, status_reason, claimed_at, completed_at
) VALUES
  (1, 'legacy_jobs', 'historical.sqlite', 1, 'historical:closed',
   'https://unsupported.example/terminal/closed', 'active_verified', 'historical terminal',
   '2026-07-20T00:00:00.000Z', '2026-07-20T00:05:00.000Z', 'closed', 'posting_closed',
   '2026-07-20T00:00:00.000Z', '2026-07-20T00:10:00.000Z'),
  (2, 'assistant_jobs', 'historical.sqlite', 2, 'historical:failed',
   'https://unsupported.example/terminal/failed', 'backfill_only', 'historical failure',
   '2026-07-20T00:00:00.000Z', '2026-07-20T00:06:00.000Z', 'failed', 'source_failure',
   NULL, NULL);
`);

  for (const prefix of ['001-', '002-', '003-', '004-', '005-', '006-', '007-']) {
    const names = await fsp.readdir(path.join(PROJECT_ROOT, 'migrations'));
    const matches = names.filter((name) => name.startsWith(prefix) && name.endsWith('.sql'));
    assert.equal(matches.length, 1, `expected one migration for ${prefix}`);
    await sqlite(database, await fsp.readFile(path.join(PROJECT_ROOT, 'migrations', matches[0]), 'utf8'));
  }
}

async function privateDirectory(directory) {
  await fsp.mkdir(directory, { recursive: true, mode: 0o700 });
  await fsp.chmod(directory, 0o700);
}

async function privateFile(filePath, contents, mode = 0o600) {
  await fsp.writeFile(filePath, contents, { mode });
  await fsp.chmod(filePath, mode);
}

function dates(start, end, display) {
  return { start, end, display };
}

function profilePayload() {
  const sourceId = 'synthetic-profile-source';
  const source = {
    id: sourceId,
    type: 'fixture',
    location: 'fixture://synthetic-profile',
    sha256: 'a'.repeat(64),
    retrieved_at: '2026-01-01T00:00:00Z',
    notes: 'deterministic synthetic evidence',
  };
  const bullet = {
    id: 'experience-bullet',
    text: 'Built Python API services backed by SQL',
    keywords: ['Python', 'API', 'SQL'],
    sources: [sourceId],
  };
  return {
    schema_version: 1,
    skills: {
      Languages: [
        { name: 'Python', keywords: ['Python'], sources: [sourceId] },
      ],
      Frameworks: [
        { name: 'FastAPI', keywords: ['FastAPI', 'API'], sources: [sourceId] },
      ],
    },
    experience: [{
      id: 'experience-main',
      title: 'Software Engineer',
      organization: 'Synthetic Systems',
      location: 'Remote',
      dates: dates('2025-01', 'Present', 'Jan 2025 - Present'),
      bullets: [bullet],
      keywords: ['Python', 'API'],
      sources: [sourceId],
    }],
    leadership: [],
    education: [{
      id: 'education-main',
      institution: 'Synthetic University',
      location: 'Synthetic, NY',
      degree: 'B.S. Computer Science',
      dates: dates('2022-09', '2026-12', 'Sep 2022 - Dec 2026'),
      graduation: {
        default: 'December 2026',
        rules: [{
          id: 'spring_coop',
          value: 'May 2027',
          all_keyword_groups: [['spring'], ['co-op', 'coop', 'internship']],
          sources: [sourceId],
        }],
      },
      keywords: ['Computer Science'],
      sources: [sourceId],
    }],
    projects: [],
    others: {
      contact: {
        full_name: 'Synthetic Applicant',
        phone: '+1-555-0101',
        email: 'applicant@example.test',
      },
      links: {
        linkedin: 'https://linkedin.example.test/synthetic-applicant',
        github: 'https://github.example.test/synthetic-applicant',
        website: 'https://applicant.example.test',
      },
      sources: [source],
      public_repositories: [],
      open_questions: [],
    },
  };
}

function greenhouseEnvelope({ sourceRowid = 101, sourceJobId = 'greenhouse:101', applicationUrl }) {
  return {
    sourceTable: 'jobs',
    sourceDb: 'synthetic-source.sqlite',
    sourceRowid,
    sourceJobId,
    applicationUrl,
    eligibilityTier: 'active_verified',
    verificationReason: 'synthetic platform fixture',
    sourcePostedAt: '2026-07-27T00:00:00Z',
    sourceLastSeenAt: '2026-07-28T00:05:00Z',
    payload: {
      url: `${GREENHOUSE_URL}?gh_src=source`,
      job_title: 'Backend Engineer',
      company: 'Synthetic Greenhouse',
      location: 'Remote',
      description: '<p>Build Python API services for synthetic applicants.</p><ul><li>Use SQL and FastAPI.</li></ul><p>Spring internship role.</p><script>ignore this unsafe text</script>',
    },
  };
}

function ashbyEnvelope() {
  return {
    sourceTable: 'assistant_jobs',
    sourceDb: 'synthetic-source.sqlite',
    sourceRowid: 102,
    sourceJobId: `ashby:${ASHBY_EXTERNAL_ID}`,
    applicationUrl: `${ASHBY_URL}?source=tracking`,
    eligibilityTier: 'unverified_stale',
    verificationReason: 'synthetic platform fixture',
    sourcePostedAt: '2026-07-26T00:00:00Z',
    sourceLastSeenAt: '2026-07-28T00:10:00Z',
    payload: {
      jobPosting: {
        id: ASHBY_EXTERNAL_ID,
        applicationUrl: ASHBY_URL,
        title: 'Frontend Engineer',
        organizationName: 'Synthetic Ashby',
        locationName: 'Remote',
        descriptionHtml: '<p>Design React dashboards for synthetic applicants.</p><p>Partner with TypeScript and accessibility.</p>',
      },
    },
  };
}

function expectedBoundJob({ id, platform, applicationUrl, title, company, description, tier, postedAt, lastSeenAt }) {
  return {
    id,
    platform,
    applicationHost: new URL(applicationUrl).hostname,
    applicationUrl,
    title,
    company,
    location: 'Remote',
    description,
    descriptionSha256: sha256(description),
    sourcePostedAt: postedAt,
    sourceLastSeenAt: lastSeenAt,
    eligibilityTier: tier,
  };
}

async function readCounter(counterPath) {
  return Number.parseInt(await fsp.readFile(counterPath, 'utf8'), 10);
}

test('prepares and recovers an owner-private canonical platform job run', async (t) => {
  const root = await fsp.realpath(await fsp.mkdtemp(path.join(os.tmpdir(), 'job-source-preparation-')));
  await fsp.chmod(root, 0o700);
  const previousCounter = process.env.BENCHMARK_COMPILER_COUNTER;
  t.after(async () => {
    if (previousCounter === undefined) delete process.env.BENCHMARK_COMPILER_COUNTER;
    else process.env.BENCHMARK_COMPILER_COUNTER = previousCounter;
    await fsp.rm(root, { recursive: true, force: true });
  });

  const database = path.join(root, 'jobs.sqlite');
  await applyHistoricalSchema(database);
  await fsp.chmod(database, 0o600);
  assert.equal((await fsp.stat(database)).mode & 0o777, 0o600);
  const terminalBefore = await sqlite(database, `
SELECT id, status, status_reason, claimed_at, completed_at
FROM application_jobs WHERE id IN (1, 2) ORDER BY id;
`);
  assert.deepEqual(terminalBefore, [
    {
      id: 1,
      status: 'closed',
      status_reason: 'posting_closed',
      claimed_at: '2026-07-20T00:00:00.000Z',
      completed_at: '2026-07-20T00:10:00.000Z',
    },
    {
      id: 2,
      status: 'failed',
      status_reason: 'source_failure',
      claimed_at: null,
      completed_at: null,
    },
  ]);

  const greenhouseTracking = greenhouseEnvelope({
    applicationUrl: `${GREENHOUSE_URL}?utm_source=tracking`,
  });
  const greenhouseSameIdentity = greenhouseEnvelope({
    applicationUrl: `${GREENHOUSE_URL}?utm_campaign=duplicate`,
  });
  const greenhouseSameUrl = greenhouseEnvelope({
    sourceRowid: 103,
    sourceJobId: 'greenhouse:103',
    applicationUrl: `${GREENHOUSE_URL}?utm_medium=duplicate`,
  });
  const ashby = ashbyEnvelope();
  const unsupported = {
    sourceTable: 'legacy_jobs',
    sourceDb: 'synthetic-source.sqlite',
    sourceRowid: 104,
    sourceJobId: 'unsupported:104',
    applicationUrl: 'https://unsupported.example/source/104',
    eligibilityTier: 'backfill_only',
    verificationReason: 'unsupported source fixture',
    sourcePostedAt: '2026-07-25T00:00:00Z',
    sourceLastSeenAt: '2026-07-25T00:05:00Z',
    payload: {},
  };

  const ingested = await ingestSupportedJobs(database, [
    greenhouseTracking,
    greenhouseSameIdentity,
    greenhouseSameUrl,
    ashby,
    unsupported,
  ]);
  assert.deepEqual(ingested, { count: 2, ids: [3, 4] });
  assert.ok(Object.isFrozen(ingested));
  assert.ok(Object.isFrozen(ingested.ids));

  const queuedRows = await sqlite(database, `
SELECT id, platform, application_url, job_title, job_company, job_location,
       job_description, job_description_sha256, status
FROM application_jobs WHERE status = 'queued' ORDER BY id;
`);
  assert.deepEqual(queuedRows, [
    {
      id: 3,
      platform: 'greenhouse',
      application_url: GREENHOUSE_URL,
      job_title: 'Backend Engineer',
      job_company: 'Synthetic Greenhouse',
      job_location: 'Remote',
      job_description: GREENHOUSE_DESCRIPTION,
      job_description_sha256: sha256(GREENHOUSE_DESCRIPTION),
      status: 'queued',
    },
    {
      id: 4,
      platform: 'ashby',
      application_url: ASHBY_URL,
      job_title: 'Frontend Engineer',
      job_company: 'Synthetic Ashby',
      job_location: 'Remote',
      job_description: ASHBY_DESCRIPTION,
      job_description_sha256: sha256(ASHBY_DESCRIPTION),
      status: 'queued',
    },
  ]);

  const greenhouse = expectedBoundJob({
    id: 3,
    platform: 'greenhouse',
    applicationUrl: GREENHOUSE_URL,
    title: 'Backend Engineer',
    company: 'Synthetic Greenhouse',
    description: GREENHOUSE_DESCRIPTION,
    tier: 'active_verified',
    postedAt: '2026-07-27T00:00:00.000Z',
    lastSeenAt: '2026-07-28T00:05:00.000Z',
  });
  const ashbyBound = expectedBoundJob({
    id: 4,
    platform: 'ashby',
    applicationUrl: ASHBY_URL,
    title: 'Frontend Engineer',
    company: 'Synthetic Ashby',
    description: ASHBY_DESCRIPTION,
    tier: 'unverified_stale',
    postedAt: '2026-07-26T00:00:00.000Z',
    lastSeenAt: '2026-07-28T00:10:00.000Z',
  });
  assert.deepEqual(await loadBoundJob(database, 3), greenhouse);
  assert.deepEqual(await loadBoundJob(database, 4), ashbyBound);
  assert.ok(Object.isFrozen(await loadBoundJob(database, 3)));

  const listed = await listBoundQueuedJobs(database);
  assert.deepEqual(listed, {
    count: 2,
    jobs: [
      {
        id: greenhouse.id,
        platform: greenhouse.platform,
        applicationHost: greenhouse.applicationHost,
        applicationHost: greenhouse.applicationHost,
        applicationUrl: greenhouse.applicationUrl,
        title: greenhouse.title,
        company: greenhouse.company,
        location: greenhouse.location,
        descriptionSha256: greenhouse.descriptionSha256,
        sourcePostedAt: greenhouse.sourcePostedAt,
        sourceLastSeenAt: greenhouse.sourceLastSeenAt,
        eligibilityTier: greenhouse.eligibilityTier,
      },
      {
        id: ashbyBound.id,
        platform: ashbyBound.platform,
        applicationHost: ashbyBound.applicationHost,
        applicationUrl: ashbyBound.applicationUrl,
        applicationHost: ashbyBound.applicationHost,
        title: ashbyBound.title,
        company: ashbyBound.company,
        location: ashbyBound.location,
        descriptionSha256: ashbyBound.descriptionSha256,
        sourcePostedAt: ashbyBound.sourcePostedAt,
        sourceLastSeenAt: ashbyBound.sourceLastSeenAt,
        eligibilityTier: ashbyBound.eligibilityTier,
      },
    ],
  });
  assert.ok(Object.isFrozen(listed));
  assert.ok(Object.isFrozen(listed.jobs));
  assert.ok(listed.jobs.every((job) => Object.isFrozen(job)));

  const workspaceRoot = path.join(root, 'workspace');
  const resumeOutputRoot = path.join(root, 'resume-output');
  await privateDirectory(workspaceRoot);
  await privateDirectory(resumeOutputRoot);
  const resumeProfilePath = path.join(root, 'synthetic-profile.json');
  const resumeTemplatePath = path.join(root, 'synthetic-template.tex');
  const resumeSkillPath = path.join(root, 'synthetic-skill.md');
  const sourceResumePath = path.join(root, 'synthetic-source-resume.pdf');
  const answerMemoryPath = path.join(root, 'synthetic-answer-memory.jsonl');
  const alternateAnswerMemoryPath = path.join(root, 'alternate-answer-memory.jsonl');
  const seedDescriptionPath = path.join(root, 'claim-description.txt');
  const seedResumePath = path.join(root, 'claim-resume.pdf');
  const compilerPath = path.join(root, 'pdflatex');
  const counterPath = path.join(root, 'compiler-count.txt');
  await privateFile(resumeProfilePath, JSON.stringify(profilePayload()));
  await privateFile(
    resumeTemplatePath,
    '\\documentclass{article}\n\\begin{document}\n%%RESUME_HEADER%%\n%%RESUME_SECTIONS%%\n\\end{document}\n',
  );
  await privateFile(
    resumeSkillPath,
    '# Resume Generation Skill\n\nVersion: 1\n\n## Source-of-truth policy\n\nUse only source-backed claims.\n\n## Output invariants\n\nGenerate exactly one page.\n',
  );
  await privateFile(sourceResumePath, '%PDF-1.7\nsynthetic source resume\n');
  await privateFile(seedDescriptionPath, 'synthetic preflight description');
  await privateFile(seedResumePath, '%PDF-1.7\nsynthetic preflight resume\n');
  await privateFile(counterPath, '0');
  const answer = createAnswerRecord('synthetic-answer', 'Synthetic answer', FIXED_NOW);
  await privateFile(answerMemoryPath, `${canonicalJson(answer)}\n`);
  await privateFile(alternateAnswerMemoryPath, `${canonicalJson(answer)}\n`);
  await fsp.copyFile(path.join(PROJECT_ROOT, 'benchmarks', 'fake-latex-compiler.py'), compilerPath);
  await fsp.chmod(compilerPath, 0o700);
  process.env.BENCHMARK_COMPILER_COUNTER = counterPath;

  await sqlite(database, `
INSERT INTO application_jobs (
  source_table, source_db, source_rowid, source_job_id, application_url,
  eligibility_tier, verification_reason, source_posted_at, source_last_seen_at,
  status, status_reason, claimed_at, completed_at
) VALUES (
  'legacy_jobs', 'manual.sqlite', 500, 'manual:unsupported',
  'https://unsupported.example/manual/500', 'active_verified', 'manual unsupported fixture',
  '2026-07-28T00:00:00.000Z', '2026-07-28T00:11:00.000Z', 'queued', NULL, NULL, NULL
);
`);
  const [manualRow] = await sqlite(database, `
SELECT id, application_url FROM application_jobs WHERE source_db = 'manual.sqlite';
`);
  assert.ok(manualRow?.id > 0);

  const unsupportedClaim = await claimSpecificQueuedJob(database, manualRow.id, {
    workspaceRoot,
    jobDescriptionPath: seedDescriptionPath,
    sourceResumePath,
    resumeUploadPath: seedResumePath,
    answerMemoryPath,
    ownerId: 'synthetic-owner',
    browserSessionId: 'synthetic-browser',
    now: FIXED_NOW,
    leaseSeconds: 120,
    maxActiveJobs: 1,
  });
  assert.equal(unsupportedClaim, null);
  const timestampMismatchClaim = await claimSpecificQueuedJob(database, greenhouse.id, {
    workspaceRoot,
    jobDescriptionPath: seedDescriptionPath,
    sourceResumePath,
    resumeUploadPath: seedResumePath,
    answerMemoryPath,
    ownerId: 'synthetic-owner',
    browserSessionId: 'synthetic-browser',
    now: FIXED_NOW,
    leaseSeconds: 120,
    maxActiveJobs: 1,
    expectedJobBinding: {
      platform: greenhouse.platform,
      applicationHost: greenhouse.applicationHost,
      applicationUrl: greenhouse.applicationUrl,
      title: greenhouse.title,
      company: greenhouse.company,
      location: greenhouse.location,
      description: greenhouse.description,
      descriptionSha256: greenhouse.descriptionSha256,
      sourcePostedAt: '2026-07-26T00:00:00.000Z',
    },
  });
  assert.equal(timestampMismatchClaim, null);

  assert.deepEqual(await sqlite(database, `
SELECT status, status_reason FROM application_jobs WHERE id = ${sql(manualRow.id)};
`), [{ status: 'queued', status_reason: null }]);

  const quarantined = await quarantineUnsupportedQueuedJobs(database);
  assert.deepEqual(quarantined, { count: 1, ids: [manualRow.id] });
  assert.ok(Object.isFrozen(quarantined));
  assert.deepEqual(await sqlite(database, `
SELECT id, status, status_reason FROM application_jobs WHERE id = ${sql(manualRow.id)};
`), [{ id: manualRow.id, status: 'skipped', status_reason: 'unsupported_platform' }]);
  assert.deepEqual(await sqlite(database, `
SELECT id, status, status_reason, claimed_at, completed_at
FROM application_jobs WHERE id IN (1, 2) ORDER BY id;
`), terminalBefore);

  const preparationOptions = {
    ownerId: 'synthetic-owner',
    browserSessionId: 'synthetic-browser',
    now: FIXED_NOW,
    leaseSeconds: 120,
    maxActiveJobs: 1,
    workspaceRoot,
    sourceResumePath,
    answerMemoryPath,
    resumeProfilePath,
    resumeTemplatePath,
    resumeSkillPath,
    resumeOutputRoot,
    compiler: compilerPath,
  };
  const prepared = await prepareOrRecoverSupportedRun(database, preparationOptions);
  assert.equal(prepared.kind, 'claimed');
  assert.deepEqual(prepared.job, greenhouse);
  assert.equal(prepared.run.jobId, greenhouse.id);
  assert.equal(prepared.run.applicationUrl, greenhouse.applicationUrl);
  assert.equal(prepared.run.status, 'applying');
  assert.equal(prepared.run.active, true);
  assert.equal(prepared.run.workspacePath, path.join(workspaceRoot, `job-${greenhouse.id}`));
  assert.equal(prepared.workspace.workspacePath, prepared.run.workspacePath);
  assert.equal(prepared.workspace.jobId, greenhouse.id);
  assert.equal(prepared.resume.pages, 1);
  assert.equal(prepared.resume.reused, false);
  assert.equal(prepared.resume.pdfPath, prepared.run.resumeArtifactPath);
  assert.equal(prepared.resume.pdfSha256, prepared.run.resumeArtifactSha256);
  assert.equal(prepared.resume.pdfSha256, prepared.workspace.resumeArtifactSha256);
  assert.equal(prepared.resume.pdfSha256, sha256Bytes(await fsp.readFile(prepared.resume.pdfPath)));
  assert.equal(prepared.workspace.contract.application_url, greenhouse.applicationUrl);
  assert.equal(prepared.workspace.contract.resume_upload_path, prepared.resume.pdfPath);
  assert.equal(prepared.workspace.contract.run_artifact_dir, prepared.workspace.evidencePath);
  assert.equal(prepared.workspace.contract.job_description_path, path.join(
    workspaceRoot,
    '.phase1-preparation',
    `owner-${sha256('synthetic-owner')}`,
    `job-${greenhouse.id}-${greenhouse.descriptionSha256}`,
    'job-description.txt',
  ));
  assert.equal(await fsp.readFile(prepared.workspace.contract.job_description_path, 'utf8'), GREENHOUSE_DESCRIPTION);
  assert.deepEqual(JSON.parse(await fsp.readFile(prepared.workspace.contractPath, 'utf8')), prepared.workspace.contract);
  assert.deepEqual(JSON.parse(await fsp.readFile(prepared.workspace.jobSnapshotPath, 'utf8')), {
    id: greenhouse.id,
    application_url: greenhouse.applicationUrl,
    platform: greenhouse.platform,
    application_host: greenhouse.applicationHost,
    eligibility_tier: greenhouse.eligibilityTier,
    source_table: 'jobs',
    source_db: 'synthetic-source.sqlite',
    source_rowid: 101,
    source_job_id: 'greenhouse:101',
    source_posted_at: greenhouse.sourcePostedAt,
    source_last_seen_at: greenhouse.sourceLastSeenAt,
    claimed_at: FIXED_NOW,
  });

  const artifactDirectory = path.dirname(prepared.resume.pdfPath);
  assert.match(prepared.resume.artifactRef, new RegExp(`^job-${greenhouse.id}/[0-9a-f]{16}$`));
  assert.equal(path.relative(resumeOutputRoot, artifactDirectory), prepared.resume.artifactRef);
  assert.deepEqual((await fsp.readdir(artifactDirectory)).sort(), [
    'job_description.txt',
    'manifest.json',
    'optimization.json',
    'resume.pdf',
    'resume.tex',
  ]);
  for (const name of await fsp.readdir(artifactDirectory)) {
    const info = await fsp.stat(path.join(artifactDirectory, name));
    assert.equal(info.mode & 0o777, 0o600);
  }
  assert.ok((await fsp.readFile(prepared.resume.pdfPath)).subarray(0, 5).equals(Buffer.from('%PDF-')));
  assert.equal(await fsp.readFile(path.join(artifactDirectory, 'job_description.txt'), 'utf8'), GREENHOUSE_DESCRIPTION);
  const manifest = JSON.parse(await fsp.readFile(path.join(artifactDirectory, 'manifest.json'), 'utf8'));
  assert.equal(manifest.job_id, greenhouse.id);
  assert.equal(manifest.artifacts['resume.pdf'].sha256, prepared.resume.pdfSha256);
  assert.equal(manifest.artifacts['resume.pdf'].bytes, (await fsp.stat(prepared.resume.pdfPath)).size);
  assert.ok(Object.isFrozen(prepared.resume));
  assert.ok(Object.isFrozen(prepared.workspace));

  const compileCountAfterFirst = await readCounter(counterPath);
  assert.ok(compileCountAfterFirst >= 1);

  const ashbyResume = await generateBoundResume(ashbyBound, {
    ownerId: 'synthetic-owner',
    workspaceRoot,
    resumeProfilePath,
    resumeTemplatePath,
    resumeSkillPath,
    resumeOutputRoot,
    compiler: compilerPath,
  });
  assert.equal(ashbyResume.pages, 1);
  assert.notEqual(ashbyResume.pdfSha256, prepared.resume.pdfSha256);
  assert.equal(ashbyResume.reused, false);
  assert.notEqual(ashbyResume.pdfPath, prepared.resume.pdfPath);
  assert.notEqual(ashbyResume.artifactRef, prepared.resume.artifactRef);
  assert.deepEqual((await fsp.readdir(path.dirname(ashbyResume.pdfPath))).sort(), [
    'job_description.txt',
    'manifest.json',
    'optimization.json',
    'resume.pdf',
    'resume.tex',
  ]);
  assert.equal(
    await fsp.readFile(path.join(path.dirname(ashbyResume.pdfPath), 'job_description.txt'), 'utf8'),
    ASHBY_DESCRIPTION,
  );
  const refreshedDescription = `${GREENHOUSE_DESCRIPTION} Refreshed source snapshot.`;
  const refreshedJob = {
    ...greenhouse,
    description: refreshedDescription,
    descriptionSha256: sha256(refreshedDescription),
  };
  const refreshedResume = await generateBoundResume(refreshedJob, {
    ownerId: 'synthetic-owner',
    workspaceRoot,
    resumeProfilePath,
    resumeTemplatePath,
    resumeSkillPath,
    resumeOutputRoot,
    compiler: compilerPath,
  });
  assert.notEqual(refreshedResume.pdfPath, prepared.resume.pdfPath);
  assert.equal(
    await fsp.readFile(path.join(
      workspaceRoot,
      '.phase1-preparation',
      `owner-${sha256('synthetic-owner')}`,
      `job-${greenhouse.id}-${refreshedJob.descriptionSha256}`,
      'job-description.txt',
    ), 'utf8'),
    refreshedDescription,
  );


  await fsp.rm(prepared.workspace.workspacePath, { recursive: true, force: true });
  await assert.rejects(fsp.lstat(prepared.workspace.contractPath), { code: 'ENOENT' });
  await assert.rejects(
    prepareOrRecoverSupportedRun(database, {
      ...preparationOptions,
      answerMemoryPath: alternateAnswerMemoryPath,
    }),
    (error) => error.code === 'E_RECOVERY_BINDING',
  );

  const compileCountBeforeRecovery = await readCounter(counterPath);
  const recovered = await prepareOrRecoverSupportedRun(database, preparationOptions);
  assert.equal(recovered.kind, 'recovered');
  assert.equal(recovered.job.id, greenhouse.id);
  assert.equal(recovered.run.runId, prepared.run.runId);
  assert.equal(recovered.run.jobId, prepared.run.jobId);
  assert.equal(recovered.run.applicationUrl, prepared.run.applicationUrl);
  assert.equal(recovered.run.workspacePath, prepared.run.workspacePath);
  assert.equal(recovered.workspace.workspacePath, prepared.workspace.workspacePath);
  assert.equal(recovered.workspace.contractPath, prepared.workspace.contractPath);
  assert.equal(recovered.workspace.jobSnapshotPath, prepared.workspace.jobSnapshotPath);
  assert.equal(recovered.workspace.resumeArtifactSha256, prepared.resume.pdfSha256);
  assert.deepEqual(
    JSON.parse(await fsp.readFile(recovered.workspace.contractPath, 'utf8')),
    recovered.workspace.contract,
  );
  assert.equal(recovered.run.resumeArtifactPath, prepared.run.resumeArtifactPath);
  assert.equal(recovered.run.resumeArtifactSha256, prepared.run.resumeArtifactSha256);
  assert.equal(recovered.resume.pdfPath, prepared.resume.pdfPath);
  assert.equal(recovered.resume.pdfSha256, prepared.resume.pdfSha256);
  assert.equal(recovered.resume.reused, true);
  assert.equal(await readCounter(counterPath), compileCountBeforeRecovery);
  assert.deepEqual(await sqlite(database, `
SELECT id, job_id, status, active, resume_artifact_path, resume_artifact_sha256
FROM application_runs ORDER BY id;
`), [{
    id: prepared.run.runId,
    job_id: greenhouse.id,
    status: 'applying',
    active: 1,
    resume_artifact_path: prepared.resume.pdfPath,
    resume_artifact_sha256: prepared.resume.pdfSha256,
  }]);
  assert.deepEqual(await sqlite(database, `
SELECT id, status, status_reason FROM application_jobs ORDER BY id;
`), [
    { id: 1, status: 'closed', status_reason: 'posting_closed' },
    { id: 2, status: 'failed', status_reason: 'source_failure' },
    { id: 3, status: 'claimed', status_reason: 'claimed_by_backlog_runner' },
    { id: 4, status: 'queued', status_reason: null },
    { id: manualRow.id, status: 'skipped', status_reason: 'unsupported_platform' },
  ]);
});
