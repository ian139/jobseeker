import { spawn } from 'node:child_process';
import { promises as fsp } from 'node:fs';
import os from 'node:os';
import path from 'node:path';

import {
  claimNextQueuedJob,
  claimSpecificQueuedJob,
} from '../src/phase1/backlog-runner.mjs';

const NOW = '2026-07-28T00:00:00.000Z';
const GREENHOUSE_URL = 'https://job-boards.greenhouse.io/northstar/jobs/1234567';
const ASHBY_URL = 'https://jobs.ashbyhq.com/orbit/11111111-1111-4111-8111-111111111111';
const EMPLOYER_HOST = 'jobs.northstar.example';
const EMPLOYER_URL = `https://${EMPLOYER_HOST}/careers/backend-engineer/apply`;
const RESUME_UPLOAD_PATH = '/private/generated/resume.pdf';

const URL_CASES = Object.freeze([
  [GREENHOUSE_URL, 'greenhouse'],
  ['https://boards.greenhouse.io/northstar/jobs/1234567?gh_src=fixture', 'greenhouse'],
  [ASHBY_URL, 'ashby'],
  [`${ASHBY_URL}?utm_source=fixture`, 'ashby'],
  ['https://jobs.lever.co/northstar/backend-engineer', null],
  ['https://northstar.wd1.myworkdayjobs.com/jobs/backend-engineer', null],
  ['https://example.test/careers/backend-engineer', null],
  ['https://greenhouse.io/northstar/jobs/1234567', null],
  ['https://job-boards.greenhouse.io/northstar', null],
  ['https://jobs.ashbyhq.com/orbit', null],
  ['https://jobs.ashbyhq.com.evil.test/orbit/11111111-1111-4111-8111-111111111111', null],
  ['https://user@jobs.ashbyhq.com/orbit/11111111-1111-4111-8111-111111111111', null],
  ['not a URL', null],
]);

const PLATFORM_CASES = Object.freeze([
  {
    id: 1,
    platform: 'greenhouse',
    applicationUrl: GREENHOUSE_URL,
    expected: {
      schema: 'platform-job-snapshot-v1',
      platform: 'greenhouse',
      applicationUrl: GREENHOUSE_URL,
      externalJobId: '1234567',
      title: 'Backend Platform Engineer',
      company: 'Northstar Robotics',
      location: 'Remote, United States',
      descriptionTerms: ['Python', 'SQL', 'reliable data services'],
      expectedResumeTerms: ['Python', 'SQL'],
    },
    payload: {
      id: 1234567,
      title: 'Backend Platform Engineer',
      company_name: 'Northstar Robotics',
      location: { name: 'Remote, United States' },
      absolute_url: GREENHOUSE_URL,
      content: '<p>Build reliable data services for robotics teams.</p><h2>Requirements</h2><ul><li>Python</li><li>SQL</li></ul><script>ignore this script</script>',
    },
    observation: {
      observationId: 'greenhouse-observation-1',
      controls: [
        control('gh-first', 'first_name', 'First Name', 'input', 'text', 'gh-ref-first'),
        control('gh-last', 'last_name', 'Last Name', 'input', 'text', 'gh-ref-last'),
        control('gh-email', 'email', 'Email', 'input', 'email', 'gh-ref-email'),
        control('gh-phone', 'phone', 'Phone', 'input', 'tel', 'gh-ref-phone'),
        control('gh-resume', 'resume', 'Resume/CV', 'input', 'file', 'gh-ref-resume'),
        control('gh-country', 'question_100', 'Country', 'select', null, 'gh-ref-country', {
          options: [
            { label: 'United States', value: 'US' },
            { label: 'Canada', value: 'CA' },
          ],
        }),
        control('gh-auth', 'question_101', 'Are you authorized to work in the United States?', 'input', 'radio', 'gh-ref-auth', {
          options: [
            { label: 'Yes', value: 'yes' },
            { label: 'No', value: 'no' },
          ],
        }),
        control('gh-response', 'question_102', 'Why are you interested in this role?', 'textarea', null, 'gh-ref-response'),
        control('gh-submit', null, 'Submit Application', 'button', 'submit', 'gh-ref-submit', {
          candidateClass: 'final_candidate',
        }),
      ],
    },
    answers: {
      'gh-first': answer('profile', 'Ada'),
      'gh-last': answer('profile', 'Example'),
      'gh-email': answer('profile', 'ada@example.test'),
      'gh-phone': answer('profile', '+1-555-0100'),
      'gh-resume': answer('profile', RESUME_UPLOAD_PATH),
      'gh-country': answer('profile', 'United States'),
      'gh-auth': answer('memory', 'Yes'),
    },
    expectedActions: [
      expectedAction('gh-first', 'fill_text', 'greenhouse_native_input', 'Ada', 'profile', 'gh-ref-first'),
      expectedAction('gh-last', 'fill_text', 'greenhouse_native_input', 'Example', 'profile', 'gh-ref-last'),
      expectedAction('gh-email', 'fill_text', 'greenhouse_native_input', 'ada@example.test', 'profile', 'gh-ref-email'),
      expectedAction('gh-phone', 'fill_text', 'greenhouse_native_input', '+1-555-0100', 'profile', 'gh-ref-phone'),
      expectedAction('gh-resume', 'upload_file', 'greenhouse_file_input', RESUME_UPLOAD_PATH, 'profile', 'gh-ref-resume'),
      expectedAction('gh-country', 'select_option', 'greenhouse_native_select', 'US', 'profile', 'gh-ref-country'),
      expectedAction('gh-auth', 'toggle', 'greenhouse_native_radio', 'yes', 'memory', 'gh-ref-auth'),
    ],
    inferenceFieldId: 'gh-response',
    finalRef: 'gh-ref-submit',
  },
  {
    id: 2,
    platform: 'ashby',
    applicationUrl: ASHBY_URL,
    expected: {
      schema: 'platform-job-snapshot-v1',
      platform: 'ashby',
      applicationUrl: ASHBY_URL,
      externalJobId: '11111111-1111-4111-8111-111111111111',
      title: 'Frontend Product Engineer',
      company: 'Orbit Software',
      location: 'Remote, United States',
      descriptionTerms: ['React', 'TypeScript', 'accessible product interfaces'],
      expectedResumeTerms: ['React', 'TypeScript'],
    },
    payload: {
      jobPosting: {
        id: '11111111-1111-4111-8111-111111111111',
        title: 'Frontend Product Engineer',
        organizationName: 'Orbit Software',
        locationName: 'Remote, United States',
        applicationUrl: ASHBY_URL,
        descriptionHtml: '<div><p>Build accessible product interfaces.</p><p>Requirements:</p><ul><li>React</li><li>TypeScript</li></ul><script>ignore this script</script></div>',
      },
    },
    observation: {
      observationId: 'ashby-observation-1',
      controls: [
        control('ashby-name', '_systemfield_name', 'Name', 'input', 'text', 'ashby-ref-name'),
        control('ashby-email', '_systemfield_email', 'Email', 'input', 'email', 'ashby-ref-email'),
        control('ashby-phone', '_systemfield_phone', 'Phone', 'input', 'tel', 'ashby-ref-phone'),
        control('ashby-linkedin', '_systemfield_linkedin', 'LinkedIn URL', 'input', 'url', 'ashby-ref-linkedin'),
        control('ashby-resume', '_systemfield_resume', 'Resume', 'input', 'file', 'ashby-ref-resume'),
        control('ashby-location', 'locationPreference', 'Preferred work location', 'aria', null, 'ashby-ref-location', {
          role: 'combobox',
          options: [
            { label: 'Remote', value: 'Remote' },
            { label: 'New York', value: 'New York' },
          ],
        }),
        control('ashby-auth', 'workAuthorization', 'Are you authorized to work in the United States?', 'aria', null, 'ashby-ref-auth', {
          role: 'radiogroup',
          options: [
            { label: 'Yes', value: 'yes' },
            { label: 'No', value: 'no' },
          ],
        }),
        control('ashby-response', 'interest', 'What excites you about Orbit?', 'textarea', null, 'ashby-ref-response'),
        control('ashby-submit', null, 'Submit Application', 'button', 'submit', 'ashby-ref-submit', {
          candidateClass: 'final_candidate',
        }),
      ],
    },
    answers: {
      'ashby-name': answer('profile', 'Ada Example'),
      'ashby-email': answer('profile', 'ada@example.test'),
      'ashby-phone': answer('profile', '+1-555-0100'),
      'ashby-linkedin': answer('profile', 'https://linkedin.example.test/ada'),
      'ashby-resume': answer('profile', RESUME_UPLOAD_PATH),
      'ashby-location': answer('memory', 'Remote'),
      'ashby-auth': answer('memory', 'Yes'),
    },
    expectedActions: [
      expectedAction('ashby-name', 'fill_text', 'ashby_native_input', 'Ada Example', 'profile', 'ashby-ref-name'),
      expectedAction('ashby-email', 'fill_text', 'ashby_native_input', 'ada@example.test', 'profile', 'ashby-ref-email'),
      expectedAction('ashby-phone', 'fill_text', 'ashby_native_input', '+1-555-0100', 'profile', 'ashby-ref-phone'),
      expectedAction('ashby-linkedin', 'fill_text', 'ashby_native_input', 'https://linkedin.example.test/ada', 'profile', 'ashby-ref-linkedin'),
      expectedAction('ashby-resume', 'upload_file', 'ashby_file_input', RESUME_UPLOAD_PATH, 'profile', 'ashby-ref-resume'),
      expectedAction('ashby-location', 'select_option', 'ashby_combobox_exact_option', 'Remote', 'memory', 'ashby-ref-location'),
      expectedAction('ashby-auth', 'toggle', 'ashby_yes_no', 'yes', 'memory', 'ashby-ref-auth'),
    ],
    inferenceFieldId: 'ashby-response',
    finalRef: 'ashby-ref-submit',
  },
]);

function control(fieldId, name, label, kind, type, controlReference, extra = {}) {
  return Object.freeze({
    fieldId,
    name,
    label,
    kind,
    type,
    required: true,
    controlReference,
    candidateClass: null,
    options: [],
    role: null,
    ...extra,
  });
}

function answer(source, value) {
  return Object.freeze({ source, value });
}

function expectedAction(fieldId, operation, mechanic, value, source, controlReference) {
  return Object.freeze({ fieldId, operation, mechanic, value, source, controlReference });
}

let pipelineGaps = 0;
let contractChecks = 0;
let adapterErrors = 0;
const failedChecks = [];

function check(name, condition) {
  contractChecks += 1;
  if (!condition) {
    pipelineGaps += 1;
    failedChecks.push(name);
  }
}

function isRecord(value) {
  return value !== null && typeof value === 'object' && !Array.isArray(value);
}

async function loadPlatformModule() {
  try {
    return await import('../src/phase1/platforms.mjs');
  } catch {
    adapterErrors += 1;
    return {};
  }
}

async function invoke(module, name, value) {
  if (typeof module[name] !== 'function') return undefined;
  try {
    return await module[name](value);
  } catch {
    adapterErrors += 1;
    return undefined;
  }
}

function sql(value) {
  if (value === null || value === undefined) return 'NULL';
  if (typeof value === 'number') return String(value);
  return `'${String(value).replaceAll("'", "''")}'`;
}

async function sqlite(database, statement) {
  const stdout = await new Promise((resolve, reject) => {
    const child = spawn('sqlite3', ['-bail', '-json', database], {
      stdio: ['pipe', 'pipe', 'pipe'],
    });
    let output = '';
    let errors = '';
    child.stdout.setEncoding('utf8');
    child.stderr.setEncoding('utf8');
    child.stdout.on('data', (chunk) => { output += chunk; });
    child.stderr.on('data', (chunk) => { errors += chunk; });
    child.once('error', reject);
    child.once('close', (code) => {
      if (code === 0) resolve(output);
      else reject(new Error(errors || `sqlite3 exited ${code}`));
    });
    child.stdin.end(statement);
  });
  const text = stdout.trim();
  return text === '' ? [] : JSON.parse(text);
}

async function privateFile(filePath, contents) {
  await fsp.writeFile(filePath, contents, { mode: 0o600 });
  await fsp.chmod(filePath, 0o600);
}

async function createBacklogFixture(rows) {
  const root = await fsp.mkdtemp(path.join(os.tmpdir(), 'platform-backlog-benchmark-'));
  await fsp.chmod(root, 0o700);
  const database = path.join(root, 'jobs.sqlite');
  const jobDescriptionPath = path.join(root, 'job-description.txt');
  const applicantProfilePath = path.join(root, 'applicant-profile.json');
  const sourceResumePath = path.join(root, 'source-resume.pdf');
  const resumeUploadPath = path.join(root, 'resume-upload.pdf');
  const answerMemoryPath = path.join(root, 'answer-memory.jsonl');
  await privateFile(jobDescriptionPath, 'Synthetic deterministic benchmark job description');
  await privateFile(applicantProfilePath, JSON.stringify({ schema: 'phase1-profile-v1' }));
  await privateFile(sourceResumePath, '%PDF-1.7\nsynthetic source resume\n');
  await privateFile(resumeUploadPath, '%PDF-1.7\nsynthetic generated resume\n');
  await privateFile(answerMemoryPath, '');
  await sqlite(database, `
PRAGMA foreign_keys = ON;
CREATE TABLE application_jobs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  source_table TEXT NOT NULL CHECK (source_table IN ('legacy_jobs','assistant_jobs')),
  source_db TEXT NOT NULL,
  source_rowid INTEGER NOT NULL,
  source_job_id TEXT NOT NULL,
  application_url TEXT NOT NULL,
  platform TEXT,
  application_host TEXT,
  job_title TEXT,
  job_company TEXT,
  job_location TEXT,
  job_description TEXT,
  job_description_sha256 TEXT,
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
CREATE TABLE application_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  job_id INTEGER NOT NULL REFERENCES application_jobs(id) ON DELETE RESTRICT,
  status TEXT NOT NULL CHECK (status IN ('preparing','applying','completed','blocked','closed','failed','needs_user','skipped')),
  reason_code TEXT NOT NULL,
  started_at TEXT NOT NULL,
  finished_at TEXT,
  final_url TEXT,
  actions_json TEXT NOT NULL DEFAULT '[]',
  evidence_path TEXT NOT NULL,
  submit_action_count INTEGER CHECK (submit_action_count IS NULL OR submit_action_count >= 0),
  active INTEGER NOT NULL DEFAULT 0 CHECK (active IN (0, 1)),
  owner_id TEXT,
  browser_session_id TEXT,
  claimed_at TEXT,
  lease_expires_at TEXT,
  last_progress_at TEXT,
  workspace_path TEXT,
  resume_artifact_path TEXT,
  resume_artifact_sha256 TEXT,
  answer_memory_path TEXT,
  blocker_alias TEXT,
  CHECK (active = 0 OR (status IN ('applying','needs_user') AND owner_id IS NOT NULL AND browser_session_id IS NOT NULL AND claimed_at IS NOT NULL AND lease_expires_at IS NOT NULL AND last_progress_at IS NOT NULL AND workspace_path IS NOT NULL AND evidence_path IS NOT NULL AND resume_artifact_path IS NOT NULL AND resume_artifact_sha256 IS NOT NULL AND answer_memory_path IS NOT NULL AND (status <> 'needs_user' OR blocker_alias IS NOT NULL))),
  CHECK (status <> 'completed' OR (submit_action_count IS NOT NULL AND submit_action_count >= 1))
);
CREATE INDEX idx_application_jobs_status_id ON application_jobs(status, id);
CREATE INDEX idx_application_runs_job_id ON application_runs(job_id);
CREATE INDEX idx_application_runs_status ON application_runs(status);
CREATE INDEX idx_application_runs_active_status ON application_runs(active, status);
CREATE UNIQUE INDEX idx_application_runs_active_job ON application_runs(job_id) WHERE active = 1;
CREATE UNIQUE INDEX idx_application_runs_active_owner ON application_runs(owner_id) WHERE active = 1;
CREATE UNIQUE INDEX idx_application_runs_active_global ON application_runs((1)) WHERE active = 1;
${rows.map((row) => {
    const platform = row.applicationUrl === GREENHOUSE_URL
      ? 'greenhouse'
      : row.applicationUrl === ASHBY_URL
        ? 'ashby'
        : null;
    const applicationHost = platform === 'greenhouse'
      ? 'job-boards.greenhouse.io'
      : platform === 'ashby'
        ? 'jobs.ashbyhq.com'
        : null;
    return `INSERT INTO application_jobs (
  id, source_table, source_db, source_rowid, source_job_id, application_url,
  platform, application_host, job_title, job_company, job_location,
  job_description, job_description_sha256,
  eligibility_tier, verification_reason, source_posted_at, source_last_seen_at,
  status, status_reason, claimed_at, completed_at
) VALUES (
  ${sql(row.id)}, 'legacy_jobs', 'synthetic.sqlite', ${sql(row.id)}, ${sql(`synthetic:${row.id}`)},
  ${sql(row.applicationUrl)}, ${sql(platform)}, ${sql(applicationHost)},
  ${sql(platform === null ? null : 'Synthetic Engineer')},
  ${sql(platform === null ? null : 'Synthetic Company')},
  ${sql(platform === null ? null : 'Remote')},
  ${sql(platform === null ? null : 'Synthetic deterministic benchmark job description')},
  ${sql(platform === null ? null : '4cf61c223e60c13c3797d6394853dcae45070fd2ed63d6f3ef39f623e3f7d0ae')},
  ${sql(row.tier ?? 'active_verified')}, 'synthetic_fixture',
  '2026-07-01T00:00:00.000Z', ${sql(row.lastSeen ?? '2026-07-27T00:00:00.000Z')},
  'queued', NULL, NULL, NULL
);`;
  }).join('\n')}
`);
  return {
    root,
    database,
    options: {
      ownerId: 'benchmark-owner',
      browserSessionId: 'benchmark-browser',
      now: NOW,
      leaseSeconds: 60,
      maxActiveJobs: 1,
      workspaceRoot: path.join(root, 'private'),
      jobDescriptionPath,
      applicantProfilePath,
      sourceResumePath,
      resumeUploadPath,
      answerMemoryPath,
    },
  };
}

async function backlogScenario(rows, operation = 'next', jobId = null) {
  const fixture = await createBacklogFixture(rows);
  try {
    const claimed = operation === 'specific'
      ? await claimSpecificQueuedJob(fixture.database, jobId, fixture.options)
      : await claimNextQueuedJob(fixture.database, fixture.options);
    const jobs = await sqlite(fixture.database, 'SELECT id, status FROM application_jobs ORDER BY id;');
    const runs = await sqlite(fixture.database, 'SELECT job_id, active FROM application_runs ORDER BY id;');
    return { claimed, jobs, runs };
  } finally {
    await fsp.rm(fixture.root, { recursive: true, force: true });
  }
}

async function assessBacklogFiltering() {
  const mixed = await backlogScenario([
    { id: 1, applicationUrl: 'https://jobs.lever.co/example/role', lastSeen: '2026-07-28T00:00:00.000Z' },
    { id: 2, applicationUrl: GREENHOUSE_URL, lastSeen: '2026-07-26T00:00:00.000Z' },
    { id: 3, applicationUrl: ASHBY_URL, lastSeen: '2026-07-27T00:00:00.000Z' },
  ]);
  check('backlog mixed claim chooses newest supported job', mixed.claimed?.jobId === 3);
  check('backlog unsupported job remains queued', mixed.jobs.find((row) => row.id === 1)?.status === 'queued');
  check('backlog creates run only for supported job', mixed.runs.length === 1 && mixed.runs[0].job_id === 3);

  const unsupported = await backlogScenario([
    { id: 10, applicationUrl: 'https://example.test/jobs/10' },
    { id: 11, applicationUrl: 'https://northstar.wd1.myworkdayjobs.com/jobs/11' },
  ]);
  check('all-unsupported backlog stays idle', unsupported.claimed === null);
  check('all-unsupported backlog creates no run', unsupported.runs.length === 0);
  check('all-unsupported rows stay queued', unsupported.jobs.every((row) => row.status === 'queued'));

  const targeted = await backlogScenario([
    { id: 20, applicationUrl: 'https://jobs.lever.co/example/targeted' },
    { id: 21, applicationUrl: GREENHOUSE_URL },
  ], 'specific', 20);
  check('targeted unsupported claim is rejected', targeted.claimed === null);
  check('targeted unsupported claim creates no run', targeted.runs.length === 0);
  check('targeted unsupported row stays queued', targeted.jobs.find((row) => row.id === 20)?.status === 'queued');
}

function normalizedResumeJob(value, fallback) {
  if (!isRecord(value)) return fallback;
  for (const key of ['title', 'company', 'location', 'description']) {
    if (typeof value[key] !== 'string' || value[key].trim() === '') return fallback;
  }
  return {
    title: value.title,
    company: value.company,
    location: value.location,
    description: value.description,
    expectedResumeTerms: fallback.expectedResumeTerms,
  };
}

async function runResumeWorkload(jobs) {
  const stdout = await new Promise((resolve, reject) => {
    const child = spawn('uv', [
      'run', '--offline', '--frozen', 'python', 'benchmarks/resume-generation-benchmark.py',
    ], {
      cwd: path.resolve('.'),
      env: {
        ...process.env,
        LANG: 'C',
        LC_ALL: 'C',
        TZ: 'UTC',
        PYTHONHASHSEED: '0',
      },
      stdio: ['pipe', 'pipe', 'pipe'],
    });
    let output = '';
    let errors = '';
    child.stdout.setEncoding('utf8');
    child.stderr.setEncoding('utf8');
    child.stdout.on('data', (chunk) => { output += chunk; });
    child.stderr.on('data', (chunk) => { errors += chunk; });
    child.once('error', reject);
    child.once('close', (code) => {
      if (code === 0) resolve(output);
      else reject(new Error(errors || `resume workload exited ${code}`));
    });
    child.stdin.end(JSON.stringify({ jobs }));
  });
  return JSON.parse(stdout.trim());
}

async function main() {
  const platformModule = await loadPlatformModule();
  check('classifyApplicationUrl export', typeof platformModule.classifyApplicationUrl === 'function');
  check('filterSupportedJobs export', typeof platformModule.filterSupportedJobs === 'function');
  check('extractPlatformJobSnapshot export', typeof platformModule.extractPlatformJobSnapshot === 'function');
  check('planPlatformApplication export', typeof platformModule.planPlatformApplication === 'function');

  for (const [url, expected] of URL_CASES) {
    const actual = await invoke(platformModule, 'classifyApplicationUrl', url);
    check(`URL classification: ${url}`, actual === expected);
  }

  const filtered = await invoke(platformModule, 'filterSupportedJobs', URL_CASES.map(([applicationUrl], index) => ({
    id: index + 1,
    applicationUrl,
  })));
  const expectedFilteredIds = URL_CASES
    .map(([, platform], index) => (platform === null ? null : index + 1))
    .filter((id) => id !== null);
  check('supported ingestion filter returns an array', Array.isArray(filtered));
  check('supported ingestion filter preserves only eligible rows', Array.isArray(filtered)
    && JSON.stringify(filtered.map((row) => row.id)) === JSON.stringify(expectedFilteredIds));
  check('supported ingestion filter marks exact platform', Array.isArray(filtered)
    && filtered.every((row) => row.platform === URL_CASES[row.id - 1][1]));

  const extractedJobs = [];
  let inferenceResponses = 0;
  let deterministicActions = 0;
  for (const fixture of PLATFORM_CASES) {
    const input = { applicationUrl: fixture.applicationUrl, payload: fixture.payload };
    const first = await invoke(platformModule, 'extractPlatformJobSnapshot', input);
    const second = await invoke(platformModule, 'extractPlatformJobSnapshot', input);
    extractedJobs.push(normalizedResumeJob(first, {
      title: fixture.expected.title,
      company: fixture.expected.company,
      location: fixture.expected.location,
      description: fixture.expected.descriptionTerms.join(' '),
      expectedResumeTerms: fixture.expected.expectedResumeTerms,
    }));

    check(`${fixture.platform} snapshot object`, isRecord(first));
    check(`${fixture.platform} snapshot frozen`, isRecord(first) && Object.isFrozen(first));
    for (const key of ['schema', 'platform', 'applicationUrl', 'externalJobId', 'title', 'company', 'location']) {
      check(`${fixture.platform} snapshot ${key}`, first?.[key] === fixture.expected[key]);
    }
    check(`${fixture.platform} description normalized`, typeof first?.description === 'string'
      && !first.description.includes('<')
      && !first.description.includes('ignore this script'));
    for (const term of fixture.expected.descriptionTerms) {
      check(`${fixture.platform} description includes ${term}`, first?.description?.includes(term) === true);
    }
    check(`${fixture.platform} extraction deterministic`, JSON.stringify(first) === JSON.stringify(second));

    const planInput = {
      platform: fixture.platform,
      observation: fixture.observation,
      answers: fixture.answers,
      resumeUploadPath: RESUME_UPLOAD_PATH,
    };
    const plan = await invoke(platformModule, 'planPlatformApplication', planInput);
    const repeatedPlan = await invoke(platformModule, 'planPlatformApplication', planInput);
    check(`${fixture.platform} plan object`, isRecord(plan));
    check(`${fixture.platform} plan schema`, plan?.schema === 'deterministic-platform-plan-v1');
    check(`${fixture.platform} plan platform`, plan?.platform === fixture.platform);
    check(`${fixture.platform} dedicated adapter`, plan?.adapter === `${fixture.platform}_v1`);
    check(`${fixture.platform} plan observation`, plan?.observationId === fixture.observation.observationId);
    check(`${fixture.platform} plan frozen`, isRecord(plan) && Object.isFrozen(plan));
    check(`${fixture.platform} plan deterministic`, JSON.stringify(plan) === JSON.stringify(repeatedPlan));
    check(`${fixture.platform} action count`, plan?.actions?.length === fixture.expectedActions.length);
    for (const expectedActionValue of fixture.expectedActions) {
      const action = plan?.actions?.find((item) => item.fieldId === expectedActionValue.fieldId);
      for (const key of ['operation', 'mechanic', 'value', 'source', 'controlReference']) {
        check(`${fixture.platform} ${expectedActionValue.fieldId} ${key}`, action?.[key] === expectedActionValue[key]);
      }
    }
    const unresolved = Array.isArray(plan?.unresolved) ? plan.unresolved : [];
    inferenceResponses += unresolved.filter((item) => item.reason === 'inference_required').length;
    deterministicActions += Array.isArray(plan?.actions) ? plan.actions.length : 0;
    check(`${fixture.platform} only response content needs inference`, unresolved.length === 1
      && unresolved[0]?.fieldId === fixture.inferenceFieldId
      && unresolved[0]?.reason === 'inference_required');
    check(`${fixture.platform} final action stays audit-gated`, plan?.actions?.every(
      (item) => item.fieldId !== fixture.observation.controls.at(-1).fieldId,
    ) === true);
    check(`${fixture.platform} final candidate returned`, plan?.finalCandidateRef === fixture.finalRef);
  }

  const employerOptions = { verifiedEmployerHost: EMPLOYER_HOST };
  check(
    'employer-hosted URL requires and accepts exact host authority',
    platformModule.classifyApplicationUrl(EMPLOYER_URL, employerOptions) === 'employer_hosted'
      && platformModule.classifyApplicationUrl(EMPLOYER_URL) === null,
  );
  const employerSnapshotInput = {
    applicationUrl: EMPLOYER_URL,
    verifiedEmployerHost: EMPLOYER_HOST,
    payload: {
      url: EMPLOYER_URL,
      job_title: 'Employer Platform Engineer',
      company: 'Northstar Robotics',
      location: 'Remote',
      description: '<p>Build deterministic employer systems.</p>',
    },
  };
  const employerSnapshot = await invoke(
    platformModule,
    'extractPlatformJobSnapshot',
    employerSnapshotInput,
  );
  check('employer-hosted snapshot binds exact host', employerSnapshot?.platform === 'employer_hosted'
    && employerSnapshot?.applicationHost === EMPLOYER_HOST);
  const employerPlan = await invoke(platformModule, 'planPlatformApplication', {
    platform: 'employer_hosted',
    applicationUrl: EMPLOYER_URL,
    applicationHost: EMPLOYER_HOST,
    observation: {
      observationId: 'employer-observation-1',
      url: `${EMPLOYER_URL}?step=2`,
      controls: [
        control('employer-name', 'name', 'Name', 'input', 'text', 'employer-ref-name'),
        control('employer-widget', 'widget', 'Widget', 'widget', null, 'employer-ref-widget', {
          tag: 'div',
          role: null,
        }),
      ],
    },
    answers: {
      'employer-name': answer('profile', 'Ada Example'),
      'employer-widget': answer('memory', 'Exact value'),
    },
    resumeUploadPath: RESUME_UPLOAD_PATH,
  });
  check('employer-hosted planner emits deterministic mechanic', employerPlan?.actions?.[0]?.mechanic
    === 'employer_hosted_native_input');
  check('employer-hosted planner leaves unknown widget unresolved', employerPlan?.unresolved?.[0]?.reason
    === 'unsupported_widget');
  deterministicActions += employerPlan?.actions?.length ?? 0;

  await assessBacklogFiltering();

  const resume = await runResumeWorkload(extractedJobs);
  check('two platform resumes generated', resume.jobs_generated === 2);
  check('identical inputs reuse deterministic bundles', resume.cache_hits === 2);
  check('both five-file bundles validate', resume.valid_bundles === 2);
  check('both resumes match platform job terms', resume.keyword_matches === 2);
  check('compiler runs once per distinct job', resume.compile_count === 2);
  check('platform jobs produce distinct artifacts', resume.distinct_artifacts === 2);
  check('both generated resumes are one page', resume.one_page_resumes === 2);

  if (failedChecks.length > 0) {
    process.stderr.write(`Benchmark gaps (${failedChecks.length}): ${failedChecks.join(' | ')}\n`);
  }
  console.log(`METRIC pipeline_gaps=${pipelineGaps}`);
  console.log(`METRIC contract_checks=${contractChecks}`);
  console.log(`METRIC adapter_errors=${adapterErrors}`);
  console.log('METRIC supported_platforms=3');
  console.log(`METRIC deterministic_actions=${deterministicActions}`);
  console.log(`METRIC inference_response_calls=${inferenceResponses}`);
  console.log(`METRIC generated_resumes=${resume.jobs_generated}`);
  console.log(`METRIC validated_resume_bundles=${resume.valid_bundles}`);
}

await main();
