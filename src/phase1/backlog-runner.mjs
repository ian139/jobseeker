import { spawn } from 'node:child_process';
import { constants as fsConstants, promises as fsp } from 'node:fs';
import path from 'node:path';

import {
  ANSWER_SCHEMA,
  approvalContextSha256,
  loadAnswerMemory,
  loadRunContractSnapshot,
  loadRunInputs,
  readRegularFile,
  validateRunContractLocal,
} from './contract.mjs';
import { validateCompletionEvidence } from './evidence.mjs';

const SQLITE_BINARY = 'sqlite3';
const SQLITE_MAX_BUFFER = 4 * 1024 * 1024;
const PRIVATE_DIRECTORY_MODE = 0o700;
const PRIVATE_FILE_MODE = 0o600;
const NOFOLLOW = fsConstants.O_NOFOLLOW ?? 0;
const WRITE_PRIVATE = fsConstants.O_WRONLY
  | fsConstants.O_CREAT
  | fsConstants.O_EXCL
  | NOFOLLOW;
const DEFAULT_LEASE_SECONDS = 300;
const SHA256_HEX = /^[0-9a-f]{64}$/u;

export const QUEUE_PRIORITY = Object.freeze([
  'active_verified',
  'unverified_stale',
  'backfill_only',
]);

export const TERMINAL_OUTCOMES = Object.freeze([
  'completed',
  'blocked',
  'closed',
  'failed',
  'skipped',
]);

const ACTIVE_STATUSES = new Set(['applying', 'needs_user']);
const RUN_STATUSES = new Set([
  'preparing',
  'applying',
  'completed',
  'blocked',
  'closed',
  'failed',
  'needs_user',
  'skipped',
]);
const QUEUE_PRIORITY_SQL = `CASE eligibility_tier
  WHEN 'active_verified' THEN 0
  WHEN 'unverified_stale' THEN 1
  WHEN 'backfill_only' THEN 2
  ELSE 3
END`;
const PREFLIGHT_KEYS = Object.freeze([
  'workspaceRoot',
  'jobDescriptionPath',
  'applicantProfilePath',
  'sourceResumePath',
  'resumeUploadPath',
  'answerMemoryPath',
]);
const PREFLIGHT_KEY_SET = new Set(PREFLIGHT_KEYS);
const CLAIM_KEY_SET = new Set([
  ...PREFLIGHT_KEYS,
  'resumeArtifactPath',
  'resumeArtifactSha256',
  'ownerId',
  'browserSessionId',
  'now',
  'leaseSeconds',
  'maxActiveJobs',
]);
const WORKSPACE_KEY_SET = new Set([
  ...PREFLIGHT_KEYS,
  'resumeArtifactPath',
  'resumeArtifactSha256',
  'startedAt',
]);

const SAFE_ACTIONS = new Set([
  'observe',
  'click',
  'clear',
  'fill',
  'select',
  'check',
  'uncheck',
  'upload',
  'scroll',
  'wait',
  'review',
  'validate',
  'retry',
  'conditional_recheck',
  'non_final_navigation',
  'final_submit',
]);

const SAFE_ACTION_OUTCOMES = new Set([
  'attempted',
  'succeeded',
  'failed',
  'blocked',
  'skipped',
]);

const SAFE_ACTION_SOURCES = new Set([
  'memory',
  'profile',
  'resume',
  'agent_inference',
  'user',
]);

const RUN_RESULT_KEYS = new Set([
  'run_id',
  'job_id',
  'owner_id',
  'browser_session_id',
  'status',
  'active',
  'reason_code',
  'started_at',
  'finished_at',
  'final_url',
  'actions_json',
  'submit_action_count',
  'claimed_at',
  'lease_expires_at',
  'last_progress_at',
  'workspace_path',
  'evidence_path',
  'resume_artifact_path',
  'answer_memory_path',
  'blocker_alias',
  'resume_artifact_sha256',
  'resume_artifact_id',
  'source_table',
  'source_db',
  'source_rowid',
  'source_job_id',
  'application_url',
  'eligibility_tier',
  'verification_reason',
  'source_posted_at',
  'source_last_seen_at',
  'job_status',
  'job_claimed_at',
]);

const RUN_SELECT_COLUMNS = `
  r.id AS run_id,
  r.job_id AS job_id,
  r.owner_id AS owner_id,
  r.browser_session_id AS browser_session_id,
  r.status AS status,
  r.active AS active,
  r.reason_code AS reason_code,
  r.started_at AS started_at,
  r.finished_at AS finished_at,
  r.final_url AS final_url,
  r.actions_json AS actions_json,
  r.submit_action_count AS submit_action_count,
  r.claimed_at AS claimed_at,
  r.lease_expires_at AS lease_expires_at,
  r.last_progress_at AS last_progress_at,
  r.workspace_path AS workspace_path,
  r.answer_memory_path AS answer_memory_path,
  r.blocker_alias AS blocker_alias,
  r.evidence_path AS evidence_path,
  r.resume_artifact_path AS resume_artifact_path,
  r.resume_artifact_sha256 AS resume_artifact_sha256,
  r.resume_artifact_id AS resume_artifact_id,
  j.source_table AS source_table,
  j.source_db AS source_db,
  j.source_rowid AS source_rowid,
  j.source_job_id AS source_job_id,
  j.application_url AS application_url,
  j.eligibility_tier AS eligibility_tier,
  j.verification_reason AS verification_reason,
  j.source_posted_at AS source_posted_at,
  j.source_last_seen_at AS source_last_seen_at,
  j.status AS job_status,
  j.claimed_at AS job_claimed_at
`;

export class BacklogRunnerError extends Error {
  constructor(code) {
    super(code);
    this.name = 'BacklogRunnerError';
    this.code = code;
  }
}

function fail(code) {
  throw new BacklogRunnerError(code);
}

function assertPlainRecord(value, code) {
  if (value === null || typeof value !== 'object' || Array.isArray(value)) fail(code);
  const prototype = Object.getPrototypeOf(value);
  if (prototype !== Object.prototype && prototype !== null) fail(code);
}

function assertExactKeys(value, allowed, code) {
  assertPlainRecord(value, code);
  for (const key of Object.keys(value)) {
    if (!allowed.has(key)) fail(`${code}_UNKNOWN_KEY`);
  }
}

function requireString(value, code, { max = 4096 } = {}) {
  if (typeof value !== 'string' || value.length === 0 || value.length > max) fail(code);
  if (/[\u0000-\u001f\u007f]/u.test(value)) fail(code);
  return value;
}

function requirePath(value, code) {
  return path.resolve(requireString(value, code, { max: 16 * 1024 }));
}

function requireStoredPath(value, code) {
  const resolved = requirePath(value, code);
  if (resolved !== value) fail(code);
  return resolved;
}

function requirePositiveInteger(value, code) {
  if (!Number.isSafeInteger(value) || value <= 0) fail(code);
  return value;
}

function normalizeTimestamp(value, code, fallback = new Date()) {
  const candidate = value === undefined
    ? (fallback instanceof Date ? fallback.toISOString() : fallback)
    : value;
  if (typeof candidate !== 'string' || !Number.isFinite(Date.parse(candidate))) fail(code);
  return new Date(candidate).toISOString();
}

function normalizeLeaseSeconds(value) {
  const seconds = value === undefined ? DEFAULT_LEASE_SECONDS : value;
  if (!Number.isSafeInteger(seconds) || seconds < 1 || seconds > 86400) fail('E_LEASE_SECONDS');
  return seconds;
}

function leaseExpiry(now, leaseSeconds) {
  const millis = Date.parse(now) + (leaseSeconds * 1000);
  if (!Number.isFinite(millis) || millis > 8640000000000000) fail('E_LEASE_SECONDS');
  return new Date(millis).toISOString();
}

function sqlLiteral(value) {
  if (value === null || value === undefined) return 'NULL';
  if (typeof value === 'number') {
    if (!Number.isSafeInteger(value)) fail('E_SQL_VALUE');
    return String(value);
  }
  if (typeof value !== 'string') fail('E_SQL_VALUE');
  return `'${value.replaceAll("'", "''")}'`;
}

function databasePath(value) {
  return requirePath(value, 'E_DATABASE_PATH');
}

async function runSqlite(database, sql) {
  return new Promise((resolve, reject) => {
    const child = spawn(
      SQLITE_BINARY,
      ['-bail', '-json', '-cmd', '.timeout 5000', database],
      { stdio: ['pipe', 'pipe', 'pipe'], windowsHide: true },
    );
    let stdout = '';
    let outputBytes = 0;
    let settled = false;
    const timer = setTimeout(() => {
      if (settled) return;
      settled = true;
      child.kill('SIGKILL');
      reject(new Error('sqlite timeout'));
    }, 15_000);
    child.stdout.setEncoding('utf8');
    child.stdout.on('data', (chunk) => {
      outputBytes += Buffer.byteLength(chunk, 'utf8');
      if (outputBytes > SQLITE_MAX_BUFFER) {
        if (!settled) {
          settled = true;
          clearTimeout(timer);
          child.kill('SIGKILL');
          reject(new Error('sqlite output limit'));
        }
        return;
      }
      stdout += chunk;
    });
    child.stderr.resume();
    child.once('error', (error) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      reject(error);
    });
    child.once('close', (code) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      if (code !== 0) reject(new Error('sqlite failed'));
      else resolve(stdout.trim());
    });
    child.stdin.end(sql);
  }).catch(() => {
    fail('E_SQLITE_COMMAND');
  });
}

function parseRows(stdout, code) {
  if (stdout === '') return [];
  let parsed;
  try {
    parsed = JSON.parse(stdout);
  } catch {
    fail(code);
  }
  if (!Array.isArray(parsed)) fail(code);
  return parsed;
}

function normalizePreflightConfig(config) {
  assertExactKeys(config, PREFLIGHT_KEY_SET, 'E_PREFLIGHT');
  const workspaceRoot = requirePath(config.workspaceRoot, 'E_WORKSPACE_ROOT');
  const jobDescriptionPath = requirePath(config.jobDescriptionPath, 'E_JOB_DESCRIPTION_PATH');
  const resumeUploadPath = requirePath(config.resumeUploadPath, 'E_RESUME_UPLOAD_PATH');
  const answerMemoryPath = requirePath(config.answerMemoryPath, 'E_ANSWER_MEMORY_PATH');
  const applicantProfilePath = config.applicantProfilePath === undefined
    ? undefined
    : requirePath(config.applicantProfilePath, 'E_APPLICANT_PROFILE_PATH');
  const sourceResumePath = config.sourceResumePath === undefined
    ? undefined
    : requirePath(config.sourceResumePath, 'E_SOURCE_RESUME_PATH');
  if (applicantProfilePath === undefined && sourceResumePath === undefined) {
    fail('E_RUN_EVIDENCE_REQUIRED');
  }
  return {
    workspaceRoot,
    jobDescriptionPath,
    applicantProfilePath,
    sourceResumePath,
    resumeUploadPath,
    answerMemoryPath,
  };
}

function preflightContract(config) {
  return {
    schema: 'phase1-run-v1',
    application_url: 'https://phase1-preflight.invalid/',
    job_description_path: config.jobDescriptionPath,
    ...(config.applicantProfilePath === undefined
      ? {}
      : { applicant_profile_path: config.applicantProfilePath }),
    ...(config.sourceResumePath === undefined ? {} : { source_resume_path: config.sourceResumePath }),
    resume_upload_path: config.resumeUploadPath,
    answer_memory_path: config.answerMemoryPath,
    run_artifact_dir: config.workspaceRoot,
    browser_mode: 'headed',
    observer: 'playwright_dom_v1',
    action_driver: 'omp_browser',
    submit_policy: 'omp_agent',
  };
}

function preflightError(error) {
  if (error instanceof BacklogRunnerError) throw error;
  if (typeof error?.code === 'string' && error.code.length > 0) fail(error.code);
  fail('E_PREFLIGHT');
}

/** Validate all static run inputs before a claim transaction can start. */
export async function preflightBacklogRun(config) {
  let normalized;
  try {
    normalized = normalizePreflightConfig(config ?? {});
    await ensurePrivateRoot(normalized.workspaceRoot);
    const contract = preflightContract(normalized);
    await validateRunContractLocal(contract);
    const inputs = await loadRunInputs(contract);
    return Object.freeze({
      ...normalized,
      resumeArtifactPath: inputs.resumeIdentity.path,
      resumeArtifactSha256: inputs.resumeIdentity.sha256,
    });
  } catch (error) {
    preflightError(error);
  }
}

function pathConfigFrom(value) {
  const config = {};
  for (const key of PREFLIGHT_KEYS) {
    if (Object.hasOwn(value, key)) config[key] = value[key];
  }
  return config;
}

function normalizeOwner(value, code) {
  return requireString(value, code, { max: 256 });
}
function normalizeClaimOptions(options) {
  assertExactKeys(options, CLAIM_KEY_SET, 'E_CLAIM_OPTIONS');
  const ownerId = normalizeOwner(options.ownerId, 'E_OWNER_ID');
  const browserSessionId = normalizeOwner(options.browserSessionId, 'E_BROWSER_SESSION_ID');
  const now = normalizeTimestamp(options.now, 'E_CLAIM_TIMESTAMP');
  const leaseSeconds = normalizeLeaseSeconds(options.leaseSeconds);
  const maxActiveJobs = options.maxActiveJobs === undefined ? 1 : options.maxActiveJobs;
  if (maxActiveJobs !== 1) fail('E_MAX_ACTIVE_JOBS');
  const suppliedResumePath = options.resumeArtifactPath === undefined
    ? undefined
    : requirePath(options.resumeArtifactPath, 'E_RESUME_ARTIFACT_PATH');
  const suppliedResumeHash = options.resumeArtifactSha256;
  if (suppliedResumeHash !== undefined
    && (typeof suppliedResumeHash !== 'string' || !SHA256_HEX.test(suppliedResumeHash))) {
    fail('E_RESUME_ARTIFACT_HASH');
  }
  return {
    ownerId,
    browserSessionId,
    now,
    leaseSeconds,
    maxActiveJobs,
    suppliedResumePath,
    suppliedResumeHash,
    preflightConfig: pathConfigFrom(options),
  };
}

function normalizeRunResultRow(row) {
  assertPlainRecord(row, 'E_RUN_RESULT');
  const keys = Object.keys(row);
  if (keys.length !== RUN_RESULT_KEYS.size || keys.some((key) => !RUN_RESULT_KEYS.has(key))) {
    fail('E_RUN_RESULT');
  }
  const runId = requirePositiveInteger(row.run_id, 'E_RUN_RESULT');
  const jobId = requirePositiveInteger(row.job_id, 'E_RUN_RESULT');
  if (!RUN_STATUSES.has(row.status) || (row.active !== 0 && row.active !== 1)) fail('E_RUN_RESULT');
  if (row.active === 1 && !ACTIVE_STATUSES.has(row.status)) fail('E_RUN_RESULT');
  const reasonCode = normalizeReasonCode(row.reason_code);
  const startedAt = normalizeTimestamp(row.started_at, 'E_RUN_RESULT');
  const claimedAt = row.claimed_at === null ? null : normalizeTimestamp(row.claimed_at, 'E_RUN_RESULT');
  const leaseExpiresAt = row.lease_expires_at === null
    ? null
    : normalizeTimestamp(row.lease_expires_at, 'E_RUN_RESULT');
  const lastProgressAt = row.last_progress_at === null
    ? null
    : normalizeTimestamp(row.last_progress_at, 'E_RUN_RESULT');
  const finishedAt = row.finished_at === null ? null : normalizeTimestamp(row.finished_at, 'E_RUN_RESULT');
  const ownerId = row.owner_id === null ? null : normalizeOwner(row.owner_id, 'E_RUN_RESULT');
  const browserSessionId = row.browser_session_id === null
    ? null
    : normalizeOwner(row.browser_session_id, 'E_RUN_RESULT');
  const workspacePath = row.workspace_path === null ? null : requireStoredPath(row.workspace_path, 'E_RUN_RESULT');
  const evidencePath = requireStoredPath(row.evidence_path, 'E_RUN_RESULT');
  const resumeArtifactPath = row.resume_artifact_path === null
    ? null
    : requireStoredPath(row.resume_artifact_path, 'E_RUN_RESULT');
  const answerMemoryPath = row.answer_memory_path === null
    ? null
    : requireStoredPath(row.answer_memory_path, 'E_RUN_RESULT');
  if (row.blocker_alias !== null) requireString(row.blocker_alias, 'E_RUN_RESULT', { max: 256 });
  if (row.resume_artifact_sha256 !== null
    && (typeof row.resume_artifact_sha256 !== 'string' || !SHA256_HEX.test(row.resume_artifact_sha256))) {
    fail('E_RUN_RESULT');
  }
  const resumeArtifactId = row.resume_artifact_id === null
    ? null
    : requirePositiveInteger(row.resume_artifact_id, 'E_RUN_RESULT');
  if (row.active === 1
    && (ownerId === null
      || browserSessionId === null
      || claimedAt === null
      || leaseExpiresAt === null
      || lastProgressAt === null
      || workspacePath === null
      || resumeArtifactPath === null
      || row.resume_artifact_sha256 === null
      || resumeArtifactId === null
      || answerMemoryPath === null
      || (row.status === 'needs_user' && row.blocker_alias === null))) {
    fail('E_RUN_RESULT');
  }
  if (row.active === 1 && row.status === 'applying' && row.blocker_alias !== null) fail('E_RUN_RESULT');
  if (typeof row.application_url !== 'string' || row.application_url.length === 0) fail('E_RUN_RESULT');
  if (row.source_table !== null && typeof row.source_table !== 'string') fail('E_RUN_RESULT');
  if (row.source_db !== null && typeof row.source_db !== 'string') fail('E_RUN_RESULT');
  if (row.source_job_id !== null && typeof row.source_job_id !== 'string') fail('E_RUN_RESULT');
  if (row.eligibility_tier !== null
    && (typeof row.eligibility_tier !== 'string' || !QUEUE_PRIORITY.includes(row.eligibility_tier))) {
    fail('E_RUN_RESULT');
  }
  if (row.job_status !== null && typeof row.job_status !== 'string') fail('E_RUN_RESULT');
  let parsedActions;
  try {
    parsedActions = JSON.parse(row.actions_json);
  } catch {
    fail('E_RUN_RESULT');
  }
  const actions = sanitizeActionSummary(parsedActions)
    .map((item) => Object.freeze({ ...item }));
  const submitActionCount = row.submit_action_count;
  if (submitActionCount !== null
    && (!Number.isSafeInteger(submitActionCount) || submitActionCount < 0)) {
    fail('E_RUN_RESULT');
  }
  const result = {
    runId,
    jobId,
    ownerId,
    browserSessionId,
    status: row.status,
    active: row.active === 1,
    reasonCode,
    startedAt,
    finishedAt,
    finalUrl: sanitizeFinalUrl(row.final_url),
    actions: Object.freeze(actions),
    submitActionCount,
    claimedAt,
    leaseExpiresAt,
    lastProgressAt,
    workspacePath,
    evidencePath,
    resumeArtifactPath,
    resumeArtifactSha256: row.resume_artifact_sha256,
    resumeArtifactId,
    answerMemoryPath,
    blockerAlias: row.blocker_alias,
    sourceTable: row.source_table,
    sourceDb: row.source_db,
    sourceRowid: row.source_rowid,
    sourceJobId: row.source_job_id,
    applicationUrl: row.application_url,
    eligibilityTier: row.eligibility_tier,
    verificationReason: row.verification_reason,
    sourcePostedAt: row.source_posted_at,
    sourceLastSeenAt: row.source_last_seen_at,
    jobStatus: row.job_status,
    jobClaimedAt: row.job_claimed_at,
  };
  return Object.freeze(result);
}

function normalizeReasonCode(value, fallback = undefined) {
  const candidate = value === undefined ? fallback : value;
  const reasonCode = requireString(candidate, 'E_REASON_CODE', { max: 128 });
  if (!/^[a-z][a-z0-9_:-]*$/u.test(reasonCode)) fail('E_REASON_CODE');
  return reasonCode;
}
function normalizeQuestionAlias(value) {
  const alias = requireString(value, 'E_QUESTION_ALIAS', { max: 256 });
  if (alias.trim().length === 0) fail('E_QUESTION_ALIAS');
  return alias;
}

function normalizeRunRows(rows, emptyCode = 'E_RUN_RESULT') {
  if (rows.length === 0) return null;
  if (rows.length !== 1) fail(emptyCode);
  return normalizeRunResultRow(rows[0]);
}

function claimSql(preflight, ownerId, browserSessionId, now, expiresAt) {
  const rootPrefix = `${preflight.workspaceRoot}/job-`;
  const priority = QUEUE_PRIORITY_SQL.replaceAll('eligibility_tier', 'j.eligibility_tier');
  return `
PRAGMA foreign_keys = ON;
BEGIN IMMEDIATE;
CREATE TEMP TABLE _backlog_claim_result (run_id INTEGER PRIMARY KEY);
CREATE TEMP TABLE _backlog_new_claim_result (run_id INTEGER PRIMARY KEY);
UPDATE application_runs
SET browser_session_id = ${sqlLiteral(browserSessionId)},
    lease_expires_at = ${sqlLiteral(expiresAt)},
    last_progress_at = ${sqlLiteral(now)}
WHERE active = 1
  AND owner_id = ${sqlLiteral(ownerId)}
  AND browser_session_id = ${sqlLiteral(browserSessionId)};
INSERT INTO _backlog_claim_result (run_id)
SELECT id
FROM application_runs
WHERE active = 1
  AND owner_id = ${sqlLiteral(ownerId)}
  AND browser_session_id = ${sqlLiteral(browserSessionId)};
INSERT INTO application_runs (
  job_id,
  status,
  reason_code,
  started_at,
  finished_at,
  final_url,
  actions_json,
  evidence_path,
  submit_action_count,
  active,
  owner_id,
  browser_session_id,
  claimed_at,
  lease_expires_at,
  last_progress_at,
  workspace_path,
  resume_artifact_path,
  resume_artifact_sha256,
  resume_artifact_id,
  answer_memory_path,
  blocker_alias
)
SELECT
  j.id,
  'applying',
  'claimed_by_backlog_runner',
  ${sqlLiteral(now)},
  NULL,
  NULL,
  '[]',
  ${sqlLiteral(rootPrefix)} || CAST(j.id AS TEXT) || '/evidence',
  NULL,
  1,
  ${sqlLiteral(ownerId)},
  ${sqlLiteral(browserSessionId)},
  ${sqlLiteral(now)},
  ${sqlLiteral(expiresAt)},
  ${sqlLiteral(now)},
  ${sqlLiteral(rootPrefix)} || CAST(j.id AS TEXT),
  ${sqlLiteral(preflight.resumeArtifactPath)},
  ${sqlLiteral(preflight.resumeArtifactSha256)},
  j.current_resume_artifact_id,
  ${sqlLiteral(preflight.answerMemoryPath)},
  NULL
FROM application_jobs AS j
WHERE j.status = 'queued'
  AND NOT EXISTS (SELECT 1 FROM _backlog_claim_result)
  AND NOT EXISTS (SELECT 1 FROM application_runs WHERE active = 1)
  AND EXISTS (
    SELECT 1
    FROM resume_artifacts AS a
    WHERE a.id = j.current_resume_artifact_id
      AND a.application_job_id = j.id
      AND a.pdf_path = ${sqlLiteral(preflight.resumeArtifactPath)}
      AND a.pdf_sha256 = ${sqlLiteral(preflight.resumeArtifactSha256)}
      AND a.job_description_path = ${sqlLiteral(preflight.jobDescriptionPath)}
      AND a.pages = 1
  )
ORDER BY ${priority},
         j.source_last_seen_at IS NULL ASC,
         j.source_last_seen_at DESC,
         j.id ASC
LIMIT 1;
INSERT INTO _backlog_new_claim_result (run_id)
SELECT last_insert_rowid()
WHERE changes() = 1;
UPDATE application_runs
SET workspace_path = ${sqlLiteral(rootPrefix)} || CAST(job_id AS TEXT)
      || '/run-' || CAST(id AS TEXT),
    evidence_path = ${sqlLiteral(rootPrefix)} || CAST(job_id AS TEXT)
      || '/run-' || CAST(id AS TEXT) || '/evidence'
WHERE id IN (SELECT run_id FROM _backlog_new_claim_result);
INSERT INTO _backlog_claim_result (run_id)
SELECT r.id
FROM application_runs AS r
WHERE r.active = 1
  AND r.owner_id = ${sqlLiteral(ownerId)}
  AND NOT EXISTS (SELECT 1 FROM _backlog_claim_result);
UPDATE application_jobs
SET status = 'claimed',
    status_reason = 'claimed_by_backlog_runner',
    claimed_at = ${sqlLiteral(now)},
    completed_at = NULL
WHERE id IN (
  SELECT r.job_id
  FROM application_runs AS r
  JOIN _backlog_claim_result AS c ON c.run_id = r.id
  WHERE r.status = 'applying'
)
  AND status = 'queued';
SELECT ${RUN_SELECT_COLUMNS}
FROM application_runs AS r
JOIN application_jobs AS j ON j.id = r.job_id
JOIN _backlog_claim_result AS c ON c.run_id = r.id;
COMMIT;
`;
}

/** Claim the next queued job and insert its applying run in one transaction. */
export async function claimNextQueuedJob(database, options = {}) {
  const normalized = normalizeClaimOptions(options);
  const preflight = await preflightBacklogRun(normalized.preflightConfig);
  if (normalized.suppliedResumePath !== undefined
    && normalized.suppliedResumePath !== preflight.resumeArtifactPath) {
    fail('E_RESUME_BINDING');
  }
  if (normalized.suppliedResumeHash !== undefined
    && normalized.suppliedResumeHash !== preflight.resumeArtifactSha256) {
    fail('E_RESUME_BINDING');
  }
  const db = databasePath(database);
  const expiresAt = leaseExpiry(normalized.now, normalized.leaseSeconds);
  const rows = parseRows(
    await runSqlite(
      db,
      claimSql(preflight, normalized.ownerId, normalized.browserSessionId, normalized.now, expiresAt),
    ),
    'E_CLAIM_RESULT',
  );
  return normalizeRunRows(rows, 'E_CLAIM_RESULT');
}


function recoverSql(ownerId, browserSessionId, now, expiresAt) {
  return `
PRAGMA foreign_keys = ON;
BEGIN IMMEDIATE;
CREATE TEMP TABLE _backlog_recover_result (run_id INTEGER PRIMARY KEY);
UPDATE application_runs
SET owner_id = ${sqlLiteral(ownerId)},
    browser_session_id = ${sqlLiteral(browserSessionId)},
    lease_expires_at = ${sqlLiteral(expiresAt)},
    last_progress_at = ${sqlLiteral(now)},
    reason_code = CASE
      WHEN owner_id = ${sqlLiteral(ownerId)}
        AND browser_session_id = ${sqlLiteral(browserSessionId)}
        THEN reason_code
      ELSE 'lease_recovered'
    END
WHERE active = 1
  AND (
    (
      owner_id = ${sqlLiteral(ownerId)}
      AND browser_session_id = ${sqlLiteral(browserSessionId)}
    )
    OR lease_expires_at <= ${sqlLiteral(now)}
  );
INSERT INTO _backlog_recover_result (run_id)
SELECT id
FROM application_runs
WHERE active = 1
  AND owner_id = ${sqlLiteral(ownerId)}
  AND last_progress_at = ${sqlLiteral(now)}
  AND lease_expires_at = ${sqlLiteral(expiresAt)};
SELECT ${RUN_SELECT_COLUMNS}
FROM application_runs AS r
JOIN application_jobs AS j ON j.id = r.job_id
JOIN _backlog_recover_result AS c ON c.run_id = r.id;
COMMIT;
`;
}

/** Refresh an owned run or take exactly one expired active lease; never inserts. */
export async function recoverActiveRun(database, options = {}) {
  const allowed = new Set(['ownerId', 'browserSessionId', 'now', 'leaseSeconds']);
  assertExactKeys(options, allowed, 'E_RECOVER_OPTIONS');
  const ownerId = normalizeOwner(options.ownerId, 'E_OWNER_ID');
  const browserSessionId = normalizeOwner(options.browserSessionId, 'E_BROWSER_SESSION_ID');
  const now = normalizeTimestamp(options.now, 'E_RECOVER_TIMESTAMP');
  const leaseSeconds = normalizeLeaseSeconds(options.leaseSeconds);
  const expiresAt = leaseExpiry(now, leaseSeconds);
  const rows = parseRows(
    await runSqlite(databasePath(database), recoverSql(ownerId, browserSessionId, now, expiresAt)),
    'E_RECOVER_RESULT',
  );
  return normalizeRunRows(rows, 'E_RECOVER_RESULT');
}

/** Recover the active run before validating preflight inputs or attempting a claim. */
export async function recoverOrClaimBacklogRun(database, options = {}) {
  assertPlainRecord(options, 'E_STARTUP_OPTIONS');
  assertExactKeys(options, CLAIM_KEY_SET, 'E_STARTUP_OPTIONS');
  const recovered = await recoverActiveRun(database, {
    ownerId: options.ownerId,
    browserSessionId: options.browserSessionId,
    now: options.now,
    leaseSeconds: options.leaseSeconds,
  });
  if (recovered !== null) return Object.freeze({ kind: 'recovered', run: recovered });

  const claimed = await claimNextQueuedJob(database, options);
  if (claimed === null) return Object.freeze({ kind: 'idle', run: null });
  return Object.freeze({ kind: 'claimed', run: claimed });
}

function heartbeatSql(runId, ownerId, browserSessionId, now, expiresAt, reason) {
  const reasonSet = reason === undefined ? '' : `,
    reason_code = ${sqlLiteral(reason)}`;
  return `
PRAGMA foreign_keys = ON;
BEGIN IMMEDIATE;
CREATE TEMP TABLE _backlog_heartbeat_result (run_id INTEGER PRIMARY KEY);
UPDATE application_runs
SET lease_expires_at = ${sqlLiteral(expiresAt)},
    last_progress_at = ${sqlLiteral(now)}${reasonSet}
WHERE id = ${sqlLiteral(runId)}
  AND active = 1
  AND owner_id = ${sqlLiteral(ownerId)}
  AND browser_session_id = ${sqlLiteral(browserSessionId)};
INSERT INTO _backlog_heartbeat_result (run_id)
SELECT id
FROM application_runs
WHERE id = ${sqlLiteral(runId)}
  AND active = 1
  AND owner_id = ${sqlLiteral(ownerId)}
  AND browser_session_id = ${sqlLiteral(browserSessionId)}
  AND last_progress_at = ${sqlLiteral(now)}
  AND lease_expires_at = ${sqlLiteral(expiresAt)};
SELECT ${RUN_SELECT_COLUMNS}
FROM application_runs AS r
JOIN application_jobs AS j ON j.id = r.job_id
JOIN _backlog_heartbeat_result AS h ON h.run_id = r.id;
COMMIT;
`;
}

/** Extend an owned active lease without changing its run identity. */
export async function heartbeatActiveRun(database, runId, options = {}) {
  const allowed = new Set(['ownerId', 'browserSessionId', 'now', 'leaseSeconds', 'reason']);
  assertExactKeys(options, allowed, 'E_HEARTBEAT_OPTIONS');
  const id = requirePositiveInteger(runId, 'E_RUN_ID');
  const ownerId = normalizeOwner(options.ownerId, 'E_OWNER_ID');
  const browserSessionId = normalizeOwner(options.browserSessionId, 'E_BROWSER_SESSION_ID');
  const now = normalizeTimestamp(options.now, 'E_HEARTBEAT_TIMESTAMP');
  const leaseSeconds = normalizeLeaseSeconds(options.leaseSeconds);
  const reason = options.reason === undefined ? undefined : normalizeReasonCode(options.reason);
  const expiresAt = leaseExpiry(now, leaseSeconds);
  const rows = parseRows(
    await runSqlite(
      databasePath(database),
      heartbeatSql(id, ownerId, browserSessionId, now, expiresAt, reason),
    ),
    'E_HEARTBEAT_RESULT',
  );
  const result = normalizeRunRows(rows, 'E_HEARTBEAT_RESULT');
  if (result === null) fail('E_RUN_NOT_ACTIVE');
  return result;
}

function pauseSql(runId, ownerId, browserSessionId, now, reason, questionAlias) {
  return `
PRAGMA foreign_keys = ON;
BEGIN IMMEDIATE;
CREATE TEMP TABLE _backlog_pause_result (run_id INTEGER PRIMARY KEY);
UPDATE application_runs
SET status = 'needs_user',
    reason_code = ${sqlLiteral(reason)},
    blocker_alias = ${sqlLiteral(questionAlias)},
    last_progress_at = ${sqlLiteral(now)}
WHERE id = ${sqlLiteral(runId)}
  AND active = 1
  AND owner_id = ${sqlLiteral(ownerId)}
  AND browser_session_id = ${sqlLiteral(browserSessionId)}
  AND status IN ('applying', 'needs_user');
INSERT INTO _backlog_pause_result (run_id)
SELECT id
FROM application_runs
WHERE id = ${sqlLiteral(runId)}
  AND active = 1
  AND owner_id = ${sqlLiteral(ownerId)}
  AND browser_session_id = ${sqlLiteral(browserSessionId)}
  AND status = 'needs_user'
  AND blocker_alias = ${sqlLiteral(questionAlias)}
  AND last_progress_at = ${sqlLiteral(now)};
UPDATE application_jobs
SET status = 'needs_user',
    status_reason = ${sqlLiteral(reason)},
    completed_at = NULL
WHERE id = (
  SELECT r.job_id
  FROM application_runs AS r
  JOIN _backlog_pause_result AS p ON p.run_id = r.id
);
SELECT ${RUN_SELECT_COLUMNS}
FROM application_runs AS r
JOIN application_jobs AS j ON j.id = r.job_id
JOIN _backlog_pause_result AS p ON p.run_id = r.id;
COMMIT;
`;
}

/** Pause an applying run in place for user input; the active row is retained. */
export async function pauseRunForUser(database, runId, options = {}) {
  const allowed = new Set(['ownerId', 'browserSessionId', 'now', 'reason', 'questionAlias']);
  assertExactKeys(options, allowed, 'E_PAUSE_OPTIONS');
  const id = requirePositiveInteger(runId, 'E_RUN_ID');
  const ownerId = normalizeOwner(options.ownerId, 'E_OWNER_ID');
  const browserSessionId = normalizeOwner(options.browserSessionId, 'E_BROWSER_SESSION_ID');
  const now = normalizeTimestamp(options.now, 'E_PAUSE_TIMESTAMP');
  const reason = normalizeReasonCode(options.reason, 'needs_user');
  const questionAlias = normalizeQuestionAlias(options.questionAlias);
  const rows = parseRows(
    await runSqlite(
      databasePath(database),
      pauseSql(id, ownerId, browserSessionId, now, reason, questionAlias),
    ),
    'E_PAUSE_RESULT',
  );
  const result = normalizeRunRows(rows, 'E_PAUSE_RESULT');
  if (result === null) fail('E_RUN_NOT_ACTIVE');
  return result;
}

function resumeSql(runId, ownerId, browserSessionId, now, expiresAt, reason) {
  return `
PRAGMA foreign_keys = ON;
BEGIN IMMEDIATE;
CREATE TEMP TABLE _backlog_resume_result (run_id INTEGER PRIMARY KEY);
UPDATE application_runs
SET status = 'applying',
    browser_session_id = ${sqlLiteral(browserSessionId)},
    reason_code = ${sqlLiteral(reason)},
    blocker_alias = NULL,
    lease_expires_at = ${sqlLiteral(expiresAt)},
    last_progress_at = ${sqlLiteral(now)}
WHERE id = ${sqlLiteral(runId)}
  AND active = 1
  AND owner_id = ${sqlLiteral(ownerId)}
  AND browser_session_id = ${sqlLiteral(browserSessionId)}
  AND status = 'needs_user';
INSERT INTO _backlog_resume_result (run_id)
SELECT id
FROM application_runs
WHERE id = ${sqlLiteral(runId)}
  AND active = 1
  AND owner_id = ${sqlLiteral(ownerId)}
  AND browser_session_id = ${sqlLiteral(browserSessionId)}
  AND status = 'applying'
  AND blocker_alias IS NULL
  AND last_progress_at = ${sqlLiteral(now)}
  AND lease_expires_at = ${sqlLiteral(expiresAt)};
UPDATE application_jobs
SET status = 'claimed',
    status_reason = ${sqlLiteral(reason)},
    completed_at = NULL
WHERE id = (
  SELECT r.job_id
  FROM application_runs AS r
  JOIN _backlog_resume_result AS p ON p.run_id = r.id
);
SELECT ${RUN_SELECT_COLUMNS}
FROM application_runs AS r
JOIN application_jobs AS j ON j.id = r.job_id
JOIN _backlog_resume_result AS p ON p.run_id = r.id;
COMMIT;
`;
}

const NON_ANSWER_RESUMABLE_REASONS = new Set([
  'third_party_access_control_required',
  'third_party_authentication_required',
]);

async function verifyPersistedQuestionAnswer(
  database,
  runId,
  ownerId,
  browserSessionId,
  accessControlResolved,
) {
  const row = await readRunRow(database, runId);
  if (row.active !== 1
    || row.status !== 'needs_user'
    || row.owner_id !== ownerId
    || row.browser_session_id !== browserSessionId
    || typeof row.blocker_alias !== 'string'
    || row.blocker_alias.length === 0
    || typeof row.workspace_path !== 'string'
    || typeof row.answer_memory_path !== 'string') {
    fail('E_RUN_NOT_NEEDS_USER');
  }
  if (accessControlResolved) {
    if (!NON_ANSWER_RESUMABLE_REASONS.has(row.reason_code)) {
      fail('E_ACCESS_CONTROL_RESOLUTION');
    }
    return;
  }
  const workspacePath = requireStoredPath(row.workspace_path, 'E_WORKSPACE_PATH');
  const memoryPath = requireStoredPath(row.answer_memory_path, 'E_ANSWER_MEMORY');
  let contractSnapshot;
  try {
    contractSnapshot = await loadRunContractSnapshot(
      path.join(workspacePath, 'contract.json'),
      { local: false },
    );
  } catch (error) {
    if (typeof error?.code === 'string') fail(error.code);
    fail('E_RUN_CONTRACT');
  }
  if (contractSnapshot.run.application_url !== row.application_url
    || contractSnapshot.run.answer_memory_path !== memoryPath
    || contractSnapshot.run.run_artifact_dir !== path.join(workspacePath, 'evidence')) {
    fail('E_RUN_CONTRACT');
  }
  let records;
  try {
    records = await loadAnswerMemory(memoryPath);
  } catch (error) {
    if (typeof error?.code === 'string') fail(error.code);
    fail('E_ANSWER_MEMORY');
  }
  let hasAnswer = false;
  for (const record of records) {
    if (record.schema !== ANSWER_SCHEMA
      || record.alias !== row.blocker_alias
      || record.approval_context?.alias !== row.blocker_alias
      || record.approval_context?.run_contract_sha256 !== contractSnapshot.identity.sha256) {
      continue;
    }
    let digest;
    try {
      digest = approvalContextSha256(record.approval_context);
    } catch {
      fail('E_ANSWER_CONTEXT');
    }
    if (record.approval_context_sha256 === digest) {
      hasAnswer = true;
      break;
    }
  }
  if (!hasAnswer) fail('E_ANSWER_REQUIRED');
}

/** Resume a paused run in place; never creates a second run row. */
export async function resumeNeedsUserRun(database, runId, options = {}) {
  const allowed = new Set([
    'ownerId',
    'browserSessionId',
    'now',
    'leaseSeconds',
    'reason',
    'accessControlResolved',
  ]);
  assertExactKeys(options, allowed, 'E_RESUME_OPTIONS');
  const id = requirePositiveInteger(runId, 'E_RUN_ID');
  const ownerId = normalizeOwner(options.ownerId, 'E_OWNER_ID');
  const browserSessionId = normalizeOwner(options.browserSessionId, 'E_BROWSER_SESSION_ID');
  const now = normalizeTimestamp(options.now, 'E_RESUME_TIMESTAMP');
  const leaseSeconds = normalizeLeaseSeconds(options.leaseSeconds);
  const reason = normalizeReasonCode(options.reason, 'resumed_by_user');
  const accessControlResolved = options.accessControlResolved ?? false;
  if (typeof accessControlResolved !== 'boolean') fail('E_ACCESS_CONTROL_RESOLUTION');
  const expiresAt = leaseExpiry(now, leaseSeconds);
  const db = databasePath(database);
  await verifyPersistedQuestionAnswer(
    db,
    id,
    ownerId,
    browserSessionId,
    accessControlResolved,
  );
  const rows = parseRows(
    await runSqlite(
      db,
      resumeSql(id, ownerId, browserSessionId, now, expiresAt, reason),
    ),
    'E_RESUME_RESULT',
  );
  const result = normalizeRunRows(rows, 'E_RESUME_RESULT');
  if (result === null) fail('E_RUN_NOT_NEEDS_USER');
  return result;
}
/** Skip an active needs_user run without fabricating a user answer. */
export async function skipNeedsUserRun(database, runId, options = {}) {
  const allowed = new Set(['ownerId', 'browserSessionId', 'now', 'reason']);
  assertExactKeys(options, allowed, 'E_SKIP_OPTIONS');
  const id = requirePositiveInteger(runId, 'E_RUN_ID');
  const ownerId = normalizeOwner(options.ownerId, 'E_OWNER_ID');
  const browserSessionId = normalizeOwner(options.browserSessionId, 'E_BROWSER_SESSION_ID');
  const now = normalizeTimestamp(options.now, 'E_SKIP_TIMESTAMP');
  const reason = normalizeReasonCode(options.reason, 'captcha_unsolved');
  const db = databasePath(database);
  const rows = parseRows(await runSqlite(db, `
PRAGMA foreign_keys = ON;
BEGIN IMMEDIATE;
CREATE TEMP TABLE _backlog_skip_result (run_id INTEGER PRIMARY KEY);
UPDATE application_runs
SET status = 'skipped',
    active = 0,
    reason_code = ${sqlLiteral(reason)},
    finished_at = ${sqlLiteral(now)},
    blocker_alias = NULL,
    lease_expires_at = NULL,
    last_progress_at = ${sqlLiteral(now)}
WHERE id = ${sqlLiteral(id)}
  AND owner_id = ${sqlLiteral(ownerId)}
  AND browser_session_id = ${sqlLiteral(browserSessionId)}
  AND active = 1
  AND status = 'needs_user'
  AND EXISTS (
    SELECT 1
    FROM application_jobs
    WHERE application_jobs.id = application_runs.job_id
      AND application_jobs.status = 'needs_user'
  );
INSERT INTO _backlog_skip_result (run_id)
SELECT id FROM application_runs
WHERE id = ${sqlLiteral(id)} AND changes() = 1;
UPDATE application_jobs
SET status = 'skipped',
    status_reason = ${sqlLiteral(reason)},
    completed_at = ${sqlLiteral(now)}
WHERE id = (
  SELECT job_id FROM application_runs AS r
  JOIN _backlog_skip_result AS s ON s.run_id = r.id
)
  AND status = 'needs_user';
SELECT ${RUN_SELECT_COLUMNS}
FROM application_runs AS r
JOIN application_jobs AS j ON j.id = r.job_id
JOIN _backlog_skip_result AS s ON s.run_id = r.id;
COMMIT;
`), 'E_SKIP_RESULT');
  const result = normalizeRunRows(rows, 'E_SKIP_RESULT');
  if (result === null) fail('E_RUN_NOT_NEEDS_USER');
  return result;
}
async function ensurePrivateRoot(root) {
  let status;
  try {
    await fsp.mkdir(root, { mode: PRIVATE_DIRECTORY_MODE, recursive: true });
    status = await fsp.lstat(root);
  } catch {
    fail('E_WORKSPACE_ROOT');
  }
  const uid = typeof process.geteuid === 'function'
    ? process.geteuid()
    : (typeof process.getuid === 'function' ? process.getuid() : undefined);
  if (!status.isDirectory()
    || status.isSymbolicLink()
    || (status.mode & 0o777) !== PRIVATE_DIRECTORY_MODE
    || (uid !== undefined && status.uid !== uid)) {
    fail('E_WORKSPACE_ROOT');
  }
}
async function requireExistingPrivateDirectory(directory, code) {
  let status;
  try {
    status = await fsp.lstat(directory);
  } catch {
    fail(code);
  }
  const uid = typeof process.geteuid === 'function'
    ? process.geteuid()
    : (typeof process.getuid === 'function' ? process.getuid() : undefined);
  if (!status.isDirectory()
    || status.isSymbolicLink()
    || (status.mode & 0o777) !== PRIVATE_DIRECTORY_MODE
    || (uid !== undefined && status.uid !== uid)) {
    fail(code);
  }
}

async function createPrivateDirectory(directory) {
  let status;
  try {
    status = await fsp.lstat(directory);
  } catch (error) {
    if (error?.code !== 'ENOENT') fail('E_WORKSPACE_DIRECTORY');
  }
  if (status !== undefined) {
    await requireExistingPrivateDirectory(directory, 'E_WORKSPACE_DIRECTORY');
    return;
  }
  try {
    await fsp.mkdir(directory, { mode: PRIVATE_DIRECTORY_MODE });
    await fsp.chmod(directory, PRIVATE_DIRECTORY_MODE);
  } catch (error) {
    if (error?.code !== 'EEXIST') fail('E_WORKSPACE_DIRECTORY');
    await requireExistingPrivateDirectory(directory, 'E_WORKSPACE_DIRECTORY');
    return;
  }
  await requireExistingPrivateDirectory(directory, 'E_WORKSPACE_DIRECTORY');
  await syncDirectory(path.dirname(directory), 'E_WORKSPACE_DIRECTORY');
}

async function syncDirectory(directory, code) {
  let handle;
  try {
    handle = await fsp.open(
      directory,
      fsConstants.O_RDONLY | (fsConstants.O_DIRECTORY ?? 0) | NOFOLLOW,
    );
    await handle.sync();
  } catch {
    fail(code);
  } finally {
    if (handle) await handle.close();
  }
}

async function writePrivateFile(filePath, contents) {
  const maxBytes = Math.max(1, Buffer.byteLength(contents, 'utf8'));
  let existing;
  try {
    existing = await readRegularFile(filePath, { maxBytes, ownerOnly: true });
  } catch (error) {
    if (error?.code !== 'E_PATH_MISSING') fail('E_WORKSPACE_FILE');
  }
  if (existing !== undefined) {
    if (existing !== contents) fail('E_WORKSPACE_FILE');
    return;
  }
  let handle;
  try {
    handle = await fsp.open(filePath, WRITE_PRIVATE, PRIVATE_FILE_MODE);
    await handle.writeFile(contents, 'utf8');
    await handle.sync();
  } catch (error) {
    if (error?.code === 'EEXIST') {
      let raced;
      try {
        raced = await readRegularFile(filePath, { maxBytes, ownerOnly: true });
      } catch {
        fail('E_WORKSPACE_FILE');
      }
      if (raced !== contents) fail('E_WORKSPACE_FILE');
      return;
    }
    fail('E_WORKSPACE_FILE');
  } finally {
    if (handle) await handle.close();
  }
  try {
    await fsp.chmod(filePath, PRIVATE_FILE_MODE);
  } catch {
    fail('E_WORKSPACE_FILE');
  }
  let verified;
  try {
    verified = await readRegularFile(filePath, { maxBytes, ownerOnly: true });
  } catch {
    fail('E_WORKSPACE_FILE');
  }
  if (verified !== contents) fail('E_WORKSPACE_FILE');
  await syncDirectory(path.dirname(filePath), 'E_WORKSPACE_FILE');
}

function normalizeWorkspaceOptions(options) {
  assertExactKeys(options, WORKSPACE_KEY_SET, 'E_WORKSPACE_OPTIONS');
  const pathConfig = normalizePreflightConfig(pathConfigFrom(options));
  const startedAt = normalizeTimestamp(options.startedAt, 'E_WORKSPACE_TIMESTAMP');
  const suppliedResumePath = options.resumeArtifactPath === undefined
    ? undefined
    : requirePath(options.resumeArtifactPath, 'E_RESUME_ARTIFACT_PATH');
  const suppliedResumeHash = options.resumeArtifactSha256 === undefined
    ? undefined
    : options.resumeArtifactSha256;
  if (suppliedResumeHash !== undefined
    && (typeof suppliedResumeHash !== 'string' || !SHA256_HEX.test(suppliedResumeHash))) {
    fail('E_RESUME_ARTIFACT_HASH');
  }
  return { ...pathConfig, startedAt, suppliedResumePath, suppliedResumeHash };
}

function normalizeWorkspaceRun(run) {
  assertPlainRecord(run, 'E_RUN');
  const required = new Set([
    'runId',
    'jobId',
    'applicationUrl',
    'workspacePath',
    'evidencePath',
    'resumeArtifactPath',
    'resumeArtifactSha256',
  ]);
  for (const key of required) {
    if (!Object.hasOwn(run, key)) fail('E_RUN');
  }
  const runId = requirePositiveInteger(run.runId, 'E_RUN_ID');
  const jobId = requirePositiveInteger(run.jobId, 'E_JOB_ID');
  const applicationUrl = requireString(run.applicationUrl, 'E_APPLICATION_URL', { max: 8192 });
  const workspacePath = requireStoredPath(run.workspacePath, 'E_WORKSPACE_PATH');
  const evidencePath = requireStoredPath(run.evidencePath, 'E_EVIDENCE_PATH');
  const resumeArtifactPath = requireStoredPath(run.resumeArtifactPath, 'E_RESUME_ARTIFACT_PATH');
  if (typeof run.resumeArtifactSha256 !== 'string' || !SHA256_HEX.test(run.resumeArtifactSha256)) {
    fail('E_RESUME_ARTIFACT_HASH');
  }
  return {
    runId,
    jobId,
    applicationUrl,
    workspacePath,
    evidencePath,
    resumeArtifactPath,
    resumeArtifactSha256: run.resumeArtifactSha256,
    claimedAt: run.claimedAt === undefined || run.claimedAt === null
      ? undefined
      : normalizeTimestamp(run.claimedAt, 'E_RUN_TIMESTAMP'),
    eligibilityTier: run.eligibilityTier ?? null,
    sourceTable: run.sourceTable ?? null,
    sourceDb: run.sourceDb ?? null,
    sourceRowid: run.sourceRowid ?? null,
    sourceJobId: run.sourceJobId ?? null,
    sourcePostedAt: run.sourcePostedAt ?? null,
    sourceLastSeenAt: run.sourceLastSeenAt ?? null,
  };
}

function canonicalJson(value) {
  return `${JSON.stringify(value)}\n`;
}

/** Create workspace/contract.json and workspace/evidence without copying inputs. */
export async function createJobWorkspace(run, options) {
  const normalizedRun = normalizeWorkspaceRun(run);
  const normalized = normalizeWorkspaceOptions(options ?? {});
  let preflight;
  try {
    preflight = await preflightBacklogRun({
      workspaceRoot: normalized.workspaceRoot,
      jobDescriptionPath: normalized.jobDescriptionPath,
      ...(normalized.applicantProfilePath === undefined
        ? {}
        : { applicantProfilePath: normalized.applicantProfilePath }),
      ...(normalized.sourceResumePath === undefined
        ? {}
        : { sourceResumePath: normalized.sourceResumePath }),
      resumeUploadPath: normalized.resumeUploadPath,
      answerMemoryPath: normalized.answerMemoryPath,
    });
  } catch (error) {
    if (error instanceof BacklogRunnerError) throw error;
    preflightError(error);
  }
  if (normalized.suppliedResumePath !== undefined
    && normalized.suppliedResumePath !== preflight.resumeArtifactPath) {
    fail('E_RESUME_BINDING');
  }
  if (normalized.suppliedResumeHash !== undefined
    && normalized.suppliedResumeHash !== preflight.resumeArtifactSha256) {
    fail('E_RESUME_BINDING');
  }
  if (normalizedRun.resumeArtifactPath !== preflight.resumeArtifactPath
    || normalizedRun.resumeArtifactSha256 !== preflight.resumeArtifactSha256) {
    fail('E_RESUME_BINDING');
  }
  const jobWorkspaceRoot = path.join(normalized.workspaceRoot, `job-${normalizedRun.jobId}`);
  const expectedWorkspacePath = path.join(jobWorkspaceRoot, `run-${normalizedRun.runId}`);
  const expectedEvidencePath = path.join(expectedWorkspacePath, 'evidence');
  if (normalizedRun.workspacePath !== expectedWorkspacePath) fail('E_WORKSPACE_BINDING');
  if (normalizedRun.evidencePath !== expectedEvidencePath) fail('E_EVIDENCE_BINDING');
  await ensurePrivateRoot(normalized.workspaceRoot);
  await createPrivateDirectory(jobWorkspaceRoot);
  await createPrivateDirectory(expectedWorkspacePath);
  await createPrivateDirectory(expectedEvidencePath);

  const runContract = {
    schema: 'phase1-run-v1',
    application_url: normalizedRun.applicationUrl,
    job_description_path: preflight.jobDescriptionPath,
    ...(preflight.applicantProfilePath === undefined
      ? {}
      : { applicant_profile_path: preflight.applicantProfilePath }),
    ...(preflight.sourceResumePath === undefined
      ? {}
      : { source_resume_path: preflight.sourceResumePath }),
    resume_upload_path: preflight.resumeUploadPath,
    answer_memory_path: preflight.answerMemoryPath,
    run_artifact_dir: expectedEvidencePath,
    browser_mode: 'headed',
    observer: 'playwright_dom_v1',
    action_driver: 'omp_browser',
    submit_policy: 'omp_agent',
  };
  const jobSnapshot = {
    id: normalizedRun.jobId,
    application_url: normalizedRun.applicationUrl,
    eligibility_tier: normalizedRun.eligibilityTier,
    source_table: normalizedRun.sourceTable,
    source_db: normalizedRun.sourceDb,
    source_rowid: normalizedRun.sourceRowid,
    source_job_id: normalizedRun.sourceJobId,
    source_posted_at: normalizedRun.sourcePostedAt,
    source_last_seen_at: normalizedRun.sourceLastSeenAt,
    claimed_at: normalizedRun.claimedAt ?? normalized.startedAt,
  };
  const contractPath = path.join(expectedWorkspacePath, 'contract.json');
  const jobSnapshotPath = path.join(expectedWorkspacePath, 'job.json');
  await writePrivateFile(contractPath, canonicalJson(runContract));
  await writePrivateFile(jobSnapshotPath, canonicalJson(jobSnapshot));
  return Object.freeze({
    runId: normalizedRun.runId,
    jobId: normalizedRun.jobId,
    workspacePath: expectedWorkspacePath,
    evidencePath: expectedEvidencePath,
    contractPath,
    jobSnapshotPath,
    startedAt: normalized.startedAt,
    resumeArtifactPath: preflight.resumeArtifactPath,
    resumeArtifactSha256: preflight.resumeArtifactSha256,
    contract: Object.freeze(structuredClone(runContract)),
  });
}

function sanitizeFinalUrl(value) {
  if (value === undefined || value === null) return null;
  const input = requireString(value, 'E_FINAL_URL', { max: 8192 });
  let parsed;
  try {
    parsed = new URL(input);
  } catch {
    fail('E_FINAL_URL');
  }
  if (parsed.protocol !== 'http:' && parsed.protocol !== 'https:') fail('E_FINAL_URL');
  if (parsed.username !== '' || parsed.password !== '') fail('E_FINAL_URL');
  return `${parsed.origin}${parsed.pathname || '/'}`;
}

function sanitizeActionItem(item, index) {
  if (typeof item === 'string') {
    if (!SAFE_ACTIONS.has(item)) fail(`E_ACTION_SUMMARY_${index}`);
    return { action: item };
  }
  assertPlainRecord(item, `E_ACTION_SUMMARY_${index}`);
  if (typeof item.action !== 'string' || !SAFE_ACTIONS.has(item.action)) {
    fail(`E_ACTION_SUMMARY_${index}`);
  }
  const summary = { action: item.action };
  if (item.outcome !== undefined) {
    if (typeof item.outcome !== 'string' || !SAFE_ACTION_OUTCOMES.has(item.outcome)) {
      fail(`E_ACTION_SUMMARY_${index}`);
    }
    summary.outcome = item.outcome;
  }
  if (item.source !== undefined) {
    if (typeof item.source !== 'string' || !SAFE_ACTION_SOURCES.has(item.source)) {
      fail(`E_ACTION_SUMMARY_${index}`);
    }
    summary.source = item.source;
  }
  return summary;
}

export function sanitizeActionSummary(value = []) {
  if (!Array.isArray(value) || value.length > 256) fail('E_ACTION_SUMMARY');
  return value.map((item, index) => sanitizeActionItem(item, index));
}

async function verifyCanonicalBinding(row) {
  if (row.workspace_path === null || row.resume_artifact_path === null || row.resume_artifact_sha256 === null) {
    fail('E_CANONICAL_EVIDENCE');
  }
  const workspacePath = requireStoredPath(row.workspace_path, 'E_CANONICAL_EVIDENCE');
  const evidencePath = requireStoredPath(row.evidence_path, 'E_CANONICAL_EVIDENCE');
  const expectedEvidencePath = path.join(workspacePath, 'evidence');
  if (evidencePath !== expectedEvidencePath) fail('E_CANONICAL_EVIDENCE');
  try {
    await requireExistingPrivateDirectory(workspacePath, 'E_CANONICAL_EVIDENCE');
    await requireExistingPrivateDirectory(evidencePath, 'E_CANONICAL_EVIDENCE');
    const snapshot = await loadRunContractSnapshot(path.join(workspacePath, 'contract.json'), { local: false });
    const contract = snapshot.run;
    if (contract.application_url !== row.application_url
      || contract.run_artifact_dir !== evidencePath
      || contract.resume_upload_path !== row.resume_artifact_path) {
      fail('E_CANONICAL_EVIDENCE');
    }
    const completion = validateCompletionEvidence(evidencePath);
    const metadata = completion.runMetadata;
    if (!metadata
      || metadata.run_contract_sha256 !== snapshot.identity.sha256
      || metadata.application_url !== row.application_url
      || metadata.resume_upload_path !== row.resume_artifact_path
      || metadata.resume_upload_sha256 !== row.resume_artifact_sha256
      || completion.applicationUrl !== row.application_url
      || completion.report.upload.path !== row.resume_artifact_path
      || completion.report.upload.sha256 !== row.resume_artifact_sha256) {
      fail('E_CANONICAL_EVIDENCE');
    }
    return completion;
  } catch (error) {
    if (error instanceof BacklogRunnerError) throw error;
    fail('E_CANONICAL_EVIDENCE');
  }
}

async function normalizeTerminalOutcome(outcome, row) {
  const allowed = new Set([
    'runId',
    'ownerId',
    'browserSessionId',
    'jobId',
    'status',
    'reasonCode',
    'finishedAt',
    'finalUrl',
    'evidencePath',
    'actionSummary',
    'actions',
    'submitActionCount',
  ]);
  assertExactKeys(outcome, allowed, 'E_OUTCOME');
  const runId = requirePositiveInteger(outcome.runId, 'E_RUN_ID');
  const ownerId = normalizeOwner(outcome.ownerId, 'E_OWNER_ID');
  const browserSessionId = normalizeOwner(outcome.browserSessionId, 'E_BROWSER_SESSION_ID');
  const jobId = requirePositiveInteger(outcome.jobId, 'E_JOB_ID');
  if (runId !== row.run_id) fail('E_RUN_ID');
  if (jobId !== row.job_id) fail('E_RUN_JOB_MISMATCH');
  if (ownerId !== row.owner_id || browserSessionId !== row.browser_session_id) {
    fail('E_RUN_NOT_OWNER');
  }
  if (row.active !== 1 || row.status !== 'applying') fail('E_RUN_NOT_ACTIVE');
  if (!TERMINAL_OUTCOMES.includes(outcome.status)) fail('E_OUTCOME_STATUS');
  const reasonCode = normalizeReasonCode(outcome.reasonCode);
  const finishedAt = normalizeTimestamp(outcome.finishedAt, 'E_FINISHED_AT');
  if (Date.parse(finishedAt) < Date.parse(row.started_at)) fail('E_OUTCOME_TIME');
  const evidencePath = requireStoredPath(outcome.evidencePath, 'E_EVIDENCE_PATH');
  if (evidencePath !== row.evidence_path) fail('E_EVIDENCE_BINDING');
  if (row.workspace_path === null || path.join(row.workspace_path, 'evidence') !== row.evidence_path) {
    fail('E_EVIDENCE_BINDING');
  }
  let actionSummary;
  let submitActionCount;
  if (outcome.status === 'completed') {
    if (outcome.actionSummary !== undefined
      || outcome.actions !== undefined
      || outcome.submitActionCount !== undefined) {
      fail('E_COMPLETION_METADATA');
    }
    const completion = await verifyCanonicalBinding(row);
    actionSummary = sanitizeActionSummary(completion.actionSummary);
    submitActionCount = completion.submitActionCount;
  } else {
    if (outcome.actionSummary !== undefined && outcome.actions !== undefined) {
      fail('E_ACTION_SUMMARY_DUPLICATE');
    }
    actionSummary = sanitizeActionSummary(outcome.actionSummary ?? outcome.actions ?? []);
    const finalSubmits = actionSummary.filter((item) => item.action === 'final_submit');
    const successfulFinalSubmits = finalSubmits.filter((item) => item.outcome === 'succeeded');
    const hasSubmitActionCount = Object.hasOwn(outcome, 'submitActionCount');
    submitActionCount = hasSubmitActionCount ? outcome.submitActionCount : null;
    if (submitActionCount !== null
      && (!Number.isSafeInteger(submitActionCount) || submitActionCount < 0)) {
      fail('E_SUBMIT_ACTION_COUNT');
    }
    if ((submitActionCount !== null && finalSubmits.length !== submitActionCount)
      || successfulFinalSubmits.length > 1) {
      fail('E_SUBMIT_ACTION_COUNT');
    }
  }
  return {
    runId,
    ownerId,
    browserSessionId,
    jobId,
    status: outcome.status,
    reasonCode,
    finishedAt,
    finalUrl: sanitizeFinalUrl(outcome.finalUrl),
    evidencePath,
    actionSummary,
    submitActionCount,
  };
}

async function readRunRow(database, runId) {
  const rows = parseRows(await runSqlite(database, `
SELECT ${RUN_SELECT_COLUMNS}
FROM application_runs AS r
JOIN application_jobs AS j ON j.id = r.job_id
WHERE r.id = ${sqlLiteral(runId)};
`), 'E_OUTCOME_RESULT');
  if (rows.length === 0) fail('E_RUN_NOT_FOUND');
  if (rows.length !== 1) fail('E_OUTCOME_RESULT');
  return rows[0];
}

/** Persist a terminal outcome by updating the claimed active run row. */
export async function persistTerminalOutcome(database, outcome) {
  assertPlainRecord(outcome, 'E_OUTCOME');
  const db = databasePath(database);
  const runId = requirePositiveInteger(outcome.runId, 'E_RUN_ID');
  const rawRow = await readRunRow(db, runId);
  const value = await normalizeTerminalOutcome(outcome, rawRow);
  const actionsJson = JSON.stringify(value.actionSummary);
  const sql = `
PRAGMA foreign_keys = ON;
BEGIN IMMEDIATE;
CREATE TEMP TABLE _backlog_terminal_result (run_id INTEGER PRIMARY KEY);
UPDATE application_runs
SET status = ${sqlLiteral(value.status)},
    active = 0,
    reason_code = ${sqlLiteral(value.reasonCode)},
    finished_at = ${sqlLiteral(value.finishedAt)},
    final_url = ${sqlLiteral(value.finalUrl)},
    actions_json = ${sqlLiteral(actionsJson)},
    evidence_path = ${sqlLiteral(value.evidencePath)},
    submit_action_count = ${sqlLiteral(value.submitActionCount)},
    lease_expires_at = NULL,
    last_progress_at = ${sqlLiteral(value.finishedAt)}
WHERE id = ${sqlLiteral(value.runId)}
  AND job_id = ${sqlLiteral(value.jobId)}
  AND owner_id = ${sqlLiteral(value.ownerId)}
  AND browser_session_id = ${sqlLiteral(value.browserSessionId)}
  AND active = 1
  AND status = 'applying'
  AND evidence_path = ${sqlLiteral(value.evidencePath)}
  AND EXISTS (
    SELECT 1
    FROM application_jobs
    WHERE application_jobs.id = application_runs.job_id
      AND application_jobs.status = 'claimed'
  );
INSERT INTO _backlog_terminal_result (run_id)
SELECT id
FROM application_runs
WHERE id = ${sqlLiteral(value.runId)}
  AND changes() = 1;
UPDATE application_jobs
SET status = ${sqlLiteral(value.status)},
    status_reason = ${sqlLiteral(value.reasonCode)},
    completed_at = ${sqlLiteral(value.finishedAt)}
WHERE id = ${sqlLiteral(value.jobId)}
  AND status = 'claimed'
  AND EXISTS (SELECT 1 FROM _backlog_terminal_result);
SELECT ${RUN_SELECT_COLUMNS}
FROM application_runs AS r
JOIN application_jobs AS j ON j.id = r.job_id
JOIN _backlog_terminal_result AS t ON t.run_id = r.id;
COMMIT;
`;
  const rows = parseRows(await runSqlite(db, sql), 'E_OUTCOME_RESULT');
  const result = normalizeRunRows(rows, 'E_OUTCOME_RESULT');
  if (result === null) fail('E_RUN_NOT_ACTIVE');
  return result;
}

/** Requeue a diagnosed failed or blocked job while preserving immutable run history. */
export async function requeueTerminalJob(database, jobId, options = {}) {
  const allowed = new Set(['reason']);
  assertExactKeys(options, allowed, 'E_REQUEUE_OPTIONS');
  const id = requirePositiveInteger(jobId, 'E_JOB_ID');
  const reason = normalizeReasonCode(options.reason, 'diagnosed_terminal_retry');
  const db = databasePath(database);
  const rows = parseRows(await runSqlite(db, `
PRAGMA foreign_keys = ON;
BEGIN IMMEDIATE;
CREATE TEMP TABLE _backlog_requeue_result (job_id INTEGER PRIMARY KEY);
INSERT INTO _backlog_requeue_result (job_id)
SELECT j.id
FROM application_jobs AS j
WHERE j.id = ${sqlLiteral(id)}
  AND j.status IN ('failed', 'blocked')
  AND NOT EXISTS (
    SELECT 1
    FROM application_runs AS active_run
    WHERE active_run.job_id = j.id
      AND active_run.active = 1
  )
  AND EXISTS (
    SELECT 1
    FROM application_runs AS terminal_run
    WHERE terminal_run.job_id = j.id
      AND terminal_run.active = 0
      AND terminal_run.status = j.status
  );
UPDATE application_jobs
SET status = 'queued',
    status_reason = ${sqlLiteral(reason)},
    claimed_at = NULL,
    completed_at = NULL
WHERE id IN (SELECT job_id FROM _backlog_requeue_result);
SELECT
  j.id AS job_id,
  j.status,
  j.status_reason AS reason_code
FROM application_jobs AS j
JOIN _backlog_requeue_result AS r ON r.job_id = j.id;
COMMIT;
`), 'E_REQUEUE_RESULT');
  if (rows.length !== 1) fail('E_JOB_NOT_RETRYABLE');
  return Object.freeze({
    jobId: requirePositiveInteger(rows[0].job_id, 'E_REQUEUE_RESULT'),
    status: rows[0].status,
    reasonCode: rows[0].reason_code,
  });
}
