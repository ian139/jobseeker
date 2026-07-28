import { createHash } from 'node:crypto';
import { spawn } from 'node:child_process';
import { constants as fsConstants, promises as fsp } from 'node:fs';
import { fileURLToPath } from 'node:url';
import path from 'node:path';

import {
  claimSpecificQueuedJob,
  createJobWorkspace,
  recoverActiveRun,
} from './backlog-runner.mjs';
import { listBoundQueuedJobs, loadBoundJob } from './job-source.mjs';

const PRIVATE_DIRECTORY_MODE = 0o700;
const PRIVATE_FILE_MODE = 0o600;
const NOFOLLOW = fsConstants.O_NOFOLLOW ?? 0;
const READ_ONLY = fsConstants.O_RDONLY | NOFOLLOW;
const WRITE_PRIVATE = fsConstants.O_WRONLY
  | fsConstants.O_CREAT
  | fsConstants.O_EXCL
  | NOFOLLOW;
const SHA256_HEX = /^[0-9a-f]{64}$/u;
const PLATFORM_SET = new Set(['greenhouse', 'ashby']);
const ELIGIBILITY_SET = new Set(['active_verified', 'unverified_stale', 'backfill_only']);
const PREPARATION_DIRECTORY = '.phase1-preparation';
const MAX_DESCRIPTION_CHARS = 12_000;
const MAX_DESCRIPTION_BYTES = MAX_DESCRIPTION_CHARS * 4;
const MAX_PROFILE_BYTES = 256 * 1024;
const MAX_TEMPLATE_BYTES = 512 * 1024;
const MAX_SKILL_BYTES = 128 * 1024;
const MAX_ANSWER_MEMORY_BYTES = 8 * 1024 * 1024;
const MAX_SOURCE_RESUME_BYTES = 16 * 1024 * 1024;
const MAX_CHILD_STDOUT_BYTES = 256 * 1024;
const MAX_CHILD_STDERR_BYTES = 64 * 1024;
const MAX_GENERATOR_SECONDS = 180;
const MAX_RESUME_TEX_BYTES = 512 * 1024;
const MAX_RESUME_PDF_BYTES = 8 * 1024 * 1024;
const MAX_REPORT_BYTES = 512 * 1024;
const MAX_MANIFEST_BYTES = 256 * 1024;
const MAX_MATCHED_KEYWORDS = 256;
const GENERATOR_SCHEMA_VERSION = 'resume-generator-v4';
const PROJECT_ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..', '..');

const GENERATOR_KEYS = new Set([
  'ownerId',
  'workspaceRoot',
  'resumeProfilePath',
  'resumeTemplatePath',
  'resumeSkillPath',
  'resumeOutputRoot',
  'compiler',
]);

const PREPARATION_KEYS = new Set([
  'ownerId',
  'browserSessionId',
  'now',
  'leaseSeconds',
  'maxActiveJobs',
  'workspaceRoot',
  'applicantProfilePath',
  'sourceResumePath',
  'answerMemoryPath',
  'resumeProfilePath',
  'resumeTemplatePath',
  'resumeSkillPath',
  'resumeOutputRoot',
  'compiler',
]);

const GENERATOR_OUTPUT_KEYS = new Set([
  'schema',
  'job_id',
  'artifact_ref',
  'tex_path',
  'pdf_path',
  'report_path',
  'manifest_path',
  'pages',
  'field',
  'graduation_date',
  'matched_keywords',
]);

const MANIFEST_KEYS = new Set([
  'schema_version',
  'generator_schema_version',
  'algorithm_sha256',
  'fingerprint',
  'job_id',
  'inputs',
  'artifacts',
  'manifest_sha256',
]);

const MANIFEST_INPUT_KEYS = new Set([
  'job_sha256',
  'profile_sha256',
  'template_sha256',
  'skill_sha256',
  'compiler_identity',
]);

const MANIFEST_ARTIFACT_KEYS = new Set([
  'resume.tex',
  'resume.pdf',
  'optimization.json',
  'job_description.txt',
]);

export class PreparationError extends Error {
  constructor(code = 'E_PREPARATION') {
    super(code);
    this.name = 'PreparationError';
    this.code = code;
  }
}

function fail(code) {
  throw new PreparationError(code);
}

function isPlainRecord(value) {
  if (value === null || typeof value !== 'object' || Array.isArray(value)) return false;
  const prototype = Object.getPrototypeOf(value);
  return prototype === Object.prototype || prototype === null;
}

function assertPlainRecord(value, code) {
  if (!isPlainRecord(value)) fail(code);
}

function assertExactKeys(value, allowed, code) {
  assertPlainRecord(value, code);
  for (const key of Object.keys(value)) {
    if (!allowed.has(key)) fail(`${code}_UNKNOWN_KEY`);
  }
}

function requireString(value, code, max = 4096) {
  if (typeof value !== 'string' || value.length === 0 || value.length > max) fail(code);
  if (/[\u0000-\u001f\u007f]/u.test(value)) fail(code);
  return value;
}

function requireNonblankString(value, code, max = 4096) {
  const result = requireString(value, code, max);
  if (result.trim().length === 0) fail(code);
  return result;
}

function requireNullableString(value, code, max = 4096) {
  if (value === null) return null;
  return requireString(value, code, max);
}

function requirePositiveInteger(value, code) {
  if (!Number.isSafeInteger(value) || value <= 0) fail(code);
  return value;
}

function requireAbsolutePath(value, code) {
  const result = requireString(value, code, 16 * 1024);
  if (!path.isAbsolute(result) || path.resolve(result) !== result) fail(code);
  return result;
}

function requireSha256(value, code) {
  if (typeof value !== 'string' || !SHA256_HEX.test(value)) fail(code);
  return value;
}

function uid() {
  if (typeof process.geteuid === 'function') return process.geteuid();
  if (typeof process.getuid === 'function') return process.getuid();
  return undefined;
}

function modeBits(mode) {
  return mode & 0o777;
}

function sameIdentity(first, second) {
  return first.dev === second.dev && first.ino === second.ino;
}

function pathParts(target) {
  const parsed = path.parse(target);
  const relative = target.slice(parsed.root.length);
  return {
    root: parsed.root,
    parts: relative.split(path.sep).filter((part) => part.length > 0),
  };
}

async function assertExistingComponentsAreSafe(target, code) {
  const { root, parts } = pathParts(target);
  let current = root;
  for (const part of parts) {
    current = path.join(current, part);
    let info;
    try {
      info = await fsp.lstat(current);
    } catch (error) {
      if (error?.code === 'ENOENT') return;
      fail(code);
    }
    if (info.isSymbolicLink()) fail(code);
  }
}

async function verifyPrivateDirectory(directory, code) {
  let info;
  try {
    info = await fsp.lstat(directory);
  } catch {
    fail(code);
  }
  const owner = uid();
  if (!info.isDirectory()
    || info.isSymbolicLink()
    || modeBits(info.mode) !== PRIVATE_DIRECTORY_MODE
    || (owner !== undefined && info.uid !== owner)) {
    fail(code);
  }
}

async function ensurePrivateDirectory(directory, code = 'E_PRIVATE_DIRECTORY') {
  await assertExistingComponentsAreSafe(directory, code);
  let existed = false;
  try {
    const info = await fsp.lstat(directory);
    existed = true;
    if (info.isSymbolicLink()) fail(code);
  } catch (error) {
    if (error instanceof PreparationError) throw error;
    if (error?.code !== 'ENOENT') fail(code);
  }
  try {
    await fsp.mkdir(directory, { recursive: true, mode: PRIVATE_DIRECTORY_MODE });
  } catch {
    fail(code);
  }
  await assertExistingComponentsAreSafe(directory, code);
  if (!existed) {
    try {
      await fsp.chmod(directory, PRIVATE_DIRECTORY_MODE);
    } catch {
      fail(code);
    }
  }
  await verifyPrivateDirectory(directory, code);
}

async function readPrivateFile(filePath, maxBytes, code) {
  let handle;
  let initial;
  try {
    handle = await fsp.open(filePath, READ_ONLY);
    initial = await handle.stat();
    const owner = uid();
    if (!initial.isFile()
      || initial.isSymbolicLink()
      || modeBits(initial.mode) !== PRIVATE_FILE_MODE
      || (owner !== undefined && initial.uid !== owner)
      || initial.size > maxBytes) {
      fail(code);
    }
    const bytes = await handle.readFile();
    if (bytes.length > maxBytes) fail(code);
    const final = await handle.stat();
    if (!sameIdentity(initial, final) || final.size !== bytes.length) fail(code);
    return bytes;
  } catch (error) {
    if (error instanceof PreparationError) throw error;
    fail(code);
  } finally {
    if (handle) await handle.close().catch(() => {});
  }
}

async function writePrivateFileIdempotent(filePath, bytes, maxBytes, code) {
  if (!Buffer.isBuffer(bytes) || bytes.length > maxBytes) fail(code);
  let existing;
  try {
    const info = await fsp.lstat(filePath);
    if (info.isSymbolicLink() || !info.isFile()) fail(code);
    existing = await readPrivateFile(filePath, maxBytes, code);
  } catch (error) {
    if (error?.code !== 'ENOENT') {
      if (error instanceof PreparationError) throw error;
      fail(code);
    }
  }
  if (existing !== undefined) {
    if (!existing.equals(bytes)) fail(code);
    return;
  }

  let handle;
  try {
    handle = await fsp.open(filePath, WRITE_PRIVATE, PRIVATE_FILE_MODE);
    await handle.writeFile(bytes);
    await handle.sync();
    await handle.chmod(PRIVATE_FILE_MODE);
  } catch (error) {
    if (error?.code === 'EEXIST') {
      const raced = await readPrivateFile(filePath, maxBytes, code);
      if (!raced.equals(bytes)) fail(code);
      return;
    }
    fail(code);
  } finally {
    if (handle) await handle.close().catch(() => {});
  }
  const verified = await readPrivateFile(filePath, maxBytes, code);
  if (!verified.equals(bytes)) fail(code);
}

function sha256Bytes(bytes) {
  return createHash('sha256').update(bytes).digest('hex');
}

function canonicalJson(value) {
  if (Array.isArray(value)) return `[${value.map((item) => canonicalJson(item)).join(',')}]`;
  if (isPlainRecord(value)) {
    return `{${Object.keys(value).sort().map((key) => (
      `${JSON.stringify(key)}:${canonicalJson(value[key])}`
    )).join(',')}}`;
  }
  const result = JSON.stringify(value);
  if (result === undefined) fail('E_CANONICAL_JSON');
  return result;
}

function canonicalJsonBytes(value, newline = false) {
  const text = `${canonicalJson(value)}${newline ? '\n' : ''}`;
  return Buffer.from(text, 'utf8');
}

function parseCanonicalJson(bytes, code, newline = false) {
  let text;
  try {
    text = Buffer.from(bytes).toString('utf8');
    if (!Buffer.from(text, 'utf8').equals(bytes)) fail(code);
  } catch {
    fail(code);
  }
  let value;
  try {
    value = JSON.parse(text);
  } catch {
    fail(code);
  }
  try {
    if (!canonicalJsonBytes(value, newline).equals(bytes)) fail(code);
  } catch (error) {
    if (error instanceof PreparationError) throw error;
    fail(code);
  }
  return value;
}

function assertKeys(value, allowed, code) {
  assertPlainRecord(value, code);
  const keys = Object.keys(value);
  if (keys.length !== allowed.size || keys.some((key) => !allowed.has(key))) fail(code);
}

function normalizeBoundJob(value) {
  assertKeys(value, new Set([
    'id',
    'platform',
    'applicationUrl',
    'title',
    'company',
    'location',
    'description',
    'descriptionSha256',
    'sourcePostedAt',
    'sourceLastSeenAt',
    'eligibilityTier',
  ]), 'E_JOB_BINDING');
  const id = requirePositiveInteger(value.id, 'E_JOB_BINDING');
  const platform = requireString(value.platform, 'E_JOB_BINDING', 32);
  if (!PLATFORM_SET.has(platform)) fail('E_JOB_BINDING');
  const applicationUrl = requireNonblankString(value.applicationUrl, 'E_JOB_BINDING', 16 * 1024);
  let parsedUrl;
  try {
    parsedUrl = new URL(applicationUrl);
  } catch {
    fail('E_JOB_BINDING');
  }
  if ((parsedUrl.protocol !== 'https:' && parsedUrl.protocol !== 'http:')
    || parsedUrl.username !== ''
    || parsedUrl.password !== '') fail('E_JOB_BINDING');
  const title = requireNonblankString(value.title, 'E_JOB_BINDING', 512);
  const company = requireNonblankString(value.company, 'E_JOB_BINDING', 512);
  const location = requireNullableString(value.location, 'E_JOB_BINDING', 512);
  const description = requireNonblankString(value.description, 'E_JOB_BINDING', MAX_DESCRIPTION_CHARS);
  const descriptionBytes = Buffer.from(description, 'utf8');
  if (descriptionBytes.length > MAX_DESCRIPTION_BYTES) fail('E_JOB_BINDING');
  const descriptionSha256 = requireSha256(value.descriptionSha256, 'E_JOB_BINDING');
  if (sha256Bytes(descriptionBytes) !== descriptionSha256) fail('E_JOB_BINDING');
  const sourcePostedAt = requireNullableString(value.sourcePostedAt, 'E_JOB_BINDING', 128);
  const sourceLastSeenAt = requireNullableString(value.sourceLastSeenAt, 'E_JOB_BINDING', 128);
  const eligibilityTier = requireString(value.eligibilityTier, 'E_JOB_BINDING', 32);
  if (!ELIGIBILITY_SET.has(eligibilityTier)) fail('E_JOB_BINDING');
  return Object.freeze({
    id,
    platform,
    applicationUrl,
    title,
    company,
    location,
    description,
    descriptionSha256,
    sourcePostedAt,
    sourceLastSeenAt,
    eligibilityTier,
  });
}

function normalizeBoundSummary(value) {
  assertKeys(value, new Set([
    'id',
    'platform',
    'applicationUrl',
    'title',
    'company',
    'location',
    'descriptionSha256',
    'sourcePostedAt',
    'sourceLastSeenAt',
    'eligibilityTier',
  ]), 'E_JOB_SUMMARY');
  const result = {
    id: requirePositiveInteger(value.id, 'E_JOB_SUMMARY'),
    platform: requireString(value.platform, 'E_JOB_SUMMARY', 32),
    applicationUrl: requireNonblankString(value.applicationUrl, 'E_JOB_SUMMARY', 16 * 1024),
    title: requireNonblankString(value.title, 'E_JOB_SUMMARY', 512),
    company: requireNonblankString(value.company, 'E_JOB_SUMMARY', 512),
    location: requireNullableString(value.location, 'E_JOB_SUMMARY', 512),
    descriptionSha256: requireSha256(value.descriptionSha256, 'E_JOB_SUMMARY'),
    sourcePostedAt: requireNullableString(value.sourcePostedAt, 'E_JOB_SUMMARY', 128),
    sourceLastSeenAt: requireNullableString(value.sourceLastSeenAt, 'E_JOB_SUMMARY', 128),
    eligibilityTier: requireString(value.eligibilityTier, 'E_JOB_SUMMARY', 32),
  };
  if (!PLATFORM_SET.has(result.platform) || !ELIGIBILITY_SET.has(result.eligibilityTier)) {
    fail('E_JOB_SUMMARY');
  }
  return Object.freeze(result);
}

function sameBoundIdentity(summary, job) {
  return summary.id === job.id
    && summary.platform === job.platform
    && summary.applicationUrl === job.applicationUrl
    && summary.title === job.title
    && summary.company === job.company
    && summary.location === job.location
    && summary.descriptionSha256 === job.descriptionSha256
    && summary.sourcePostedAt === job.sourcePostedAt
    && summary.sourceLastSeenAt === job.sourceLastSeenAt
    && summary.eligibilityTier === job.eligibilityTier;
}

function sameBoundJob(first, second) {
  return sameBoundIdentity(first, second)
    && first.description === second.description;
}

function normalizeGenerationOptions(options) {
  assertExactKeys(options, GENERATOR_KEYS, 'E_GENERATOR_OPTIONS');
  const ownerId = requireNonblankString(options.ownerId, 'E_OWNER_ID', 256);
  const workspaceRoot = requireAbsolutePath(options.workspaceRoot, 'E_WORKSPACE_ROOT');
  const resumeProfilePath = requireAbsolutePath(options.resumeProfilePath, 'E_RESUME_PROFILE_PATH');
  const resumeTemplatePath = requireAbsolutePath(options.resumeTemplatePath, 'E_RESUME_TEMPLATE_PATH');
  const resumeSkillPath = requireAbsolutePath(options.resumeSkillPath, 'E_RESUME_SKILL_PATH');
  const resumeOutputRoot = requireAbsolutePath(options.resumeOutputRoot, 'E_RESUME_OUTPUT_ROOT');
  const compiler = options.compiler === undefined
    ? undefined
    : requireNonblankString(options.compiler, 'E_COMPILER', 16 * 1024);
  if (compiler !== undefined && /\s/u.test(compiler)) fail('E_COMPILER');
  return Object.freeze({
    ownerId,
    workspaceRoot,
    resumeProfilePath,
    resumeTemplatePath,
    resumeSkillPath,
    resumeOutputRoot,
    compiler,
  });
}
function generationOptionValues(options) {
  return {
    ownerId: options.ownerId,
    workspaceRoot: options.workspaceRoot,
    resumeProfilePath: options.resumeProfilePath,
    resumeTemplatePath: options.resumeTemplatePath,
    resumeSkillPath: options.resumeSkillPath,
    resumeOutputRoot: options.resumeOutputRoot,
    ...(options.compiler === undefined ? {} : { compiler: options.compiler }),
  };
}


function normalizeCoordinatorShape(options) {
  assertExactKeys(options, PREPARATION_KEYS, 'E_PREPARATION_OPTIONS');
  return options;
}

async function validateRegularInput(filePath, maxBytes, code) {
  await readPrivateFile(filePath, maxBytes, code);
  return filePath;
}

async function validateGenerationInputs(options) {
  await ensurePrivateDirectory(options.workspaceRoot, 'E_WORKSPACE_ROOT');
  await ensurePrivateDirectory(options.resumeOutputRoot, 'E_RESUME_OUTPUT_ROOT');
  await validateRegularInput(options.resumeProfilePath, MAX_PROFILE_BYTES, 'E_RESUME_PROFILE_PATH');
  await validateRegularInput(options.resumeTemplatePath, MAX_TEMPLATE_BYTES, 'E_RESUME_TEMPLATE_PATH');
  await validateRegularInput(options.resumeSkillPath, MAX_SKILL_BYTES, 'E_RESUME_SKILL_PATH');
}

function optionalAbsolutePath(value, code) {
  return value === undefined ? undefined : requireAbsolutePath(value, code);
}

async function normalizeWorkspaceInputs(options) {
  normalizeCoordinatorShape(options);
  const ownerId = requireNonblankString(options.ownerId, 'E_OWNER_ID', 256);
  const workspaceRoot = requireAbsolutePath(options.workspaceRoot, 'E_WORKSPACE_ROOT');
  const applicantProfilePath = optionalAbsolutePath(options.applicantProfilePath, 'E_APPLICANT_PROFILE_PATH');
  const sourceResumePath = optionalAbsolutePath(options.sourceResumePath, 'E_SOURCE_RESUME_PATH');
  if (applicantProfilePath === undefined && sourceResumePath === undefined) fail('E_RUN_EVIDENCE_REQUIRED');
  const answerMemoryPath = requireAbsolutePath(options.answerMemoryPath, 'E_ANSWER_MEMORY_PATH');
  const browserSessionId = requireNonblankString(options.browserSessionId, 'E_BROWSER_SESSION_ID', 256);
  const maxActiveJobs = options.maxActiveJobs === undefined ? 1 : options.maxActiveJobs;
  if (maxActiveJobs !== 1) fail('E_MAX_ACTIVE_JOBS');
  await ensurePrivateDirectory(workspaceRoot, 'E_WORKSPACE_ROOT');
  if (applicantProfilePath !== undefined) {
    await validateRegularInput(applicantProfilePath, MAX_PROFILE_BYTES, 'E_APPLICANT_PROFILE_PATH');
  }
  if (sourceResumePath !== undefined) {
    await validateRegularInput(sourceResumePath, MAX_SOURCE_RESUME_BYTES, 'E_SOURCE_RESUME_PATH');
  }
  await validateRegularInput(answerMemoryPath, MAX_ANSWER_MEMORY_BYTES, 'E_ANSWER_MEMORY_PATH');
  return Object.freeze({
    ownerId,
    workspaceRoot,
    browserSessionId,
    applicantProfilePath,
    sourceResumePath,
    answerMemoryPath,
    now: options.now,
    leaseSeconds: options.leaseSeconds,
    maxActiveJobs,
  });
}

async function normalizeCoordinatorOptions(options) {
  const workspace = await normalizeWorkspaceInputs(options);
  const generation = normalizeGenerationOptions(generationOptionValues(options));
  await validateGenerationInputs(generation);
  return Object.freeze({
    ...generation,
    ...workspace,
  });
}

function preparationPaths(options, job) {
  const ownerHash = sha256Bytes(Buffer.from(options.ownerId, 'utf8'));
  const ownerDirectory = path.join(options.workspaceRoot, PREPARATION_DIRECTORY, `owner-${ownerHash}`);
  const jobDirectory = path.join(ownerDirectory, `job-${job.id}-${job.descriptionSha256}`);
  return {
    ownerDirectory,
    jobDirectory,
    descriptionPath: path.join(jobDirectory, 'job-description.txt'),
  };
}

async function stageDescription(job, options) {
  const paths = preparationPaths(options, job);
  await ensurePrivateDirectory(path.join(options.workspaceRoot, PREPARATION_DIRECTORY), 'E_PREPARATION_DIRECTORY');
  await ensurePrivateDirectory(paths.ownerDirectory, 'E_PREPARATION_DIRECTORY');
  await ensurePrivateDirectory(paths.jobDirectory, 'E_PREPARATION_DIRECTORY');
  const bytes = Buffer.from(job.description, 'utf8');
  if (sha256Bytes(bytes) !== job.descriptionSha256) fail('E_JOB_BINDING');
  await writePrivateFileIdempotent(
    paths.descriptionPath,
    bytes,
    MAX_DESCRIPTION_BYTES,
    'E_PREPARATION_DESCRIPTION',
  );
  const verified = await readPrivateFile(paths.descriptionPath, MAX_DESCRIPTION_BYTES, 'E_PREPARATION_DESCRIPTION');
  if (sha256Bytes(verified) !== job.descriptionSha256 || !verified.equals(bytes)) {
    fail('E_PREPARATION_DESCRIPTION');
  }
  return Object.freeze({ path: paths.descriptionPath, sha256: job.descriptionSha256 });
}

function resumeJobInput(job) {
  return {
    schema: 'resume-job-v1',
    id: job.id,
    title: job.title,
    company: job.company,
    description: job.description,
    location: job.location,
    posted_at: job.sourcePostedAt,
  };
}

function resumeJobDigest(job) {
  return sha256Bytes(canonicalJsonBytes({
    id: job.id,
    title: job.title,
    company: job.company,
    description: job.description,
    location: job.location,
    posted_at: job.sourcePostedAt,
  }));
}

function generatorEnvironment() {
  const environment = { ...process.env };
  delete environment.OLLAMA_CLOUD_API_KEY;
  delete environment.OLLAMA_API_KEY;
  delete environment.RESUME_ADVISORY_ENABLED;
  environment.RESUME_ADVISORY_ENABLED = '0';
  return environment;
}

function runGenerator(input, options) {
  const args = [
    'run',
    '--offline',
    '--frozen',
    'python',
    '-m',
    'resume_generation.command',
    '--profile',
    options.resumeProfilePath,
    '--template',
    options.resumeTemplatePath,
    '--skill',
    options.resumeSkillPath,
    '--output-root',
    options.resumeOutputRoot,
  ];
  if (options.compiler !== undefined) args.push('--compiler', options.compiler);
  const inputBytes = canonicalJsonBytes(input);
  if (inputBytes.length > 64 * 1024) fail('E_GENERATOR_INPUT');
  return new Promise((resolve, reject) => {
    let child;
    try {
      child = spawn('uv', args, {
        cwd: PROJECT_ROOT,
        env: generatorEnvironment(),
        stdio: ['pipe', 'pipe', 'pipe'],
        windowsHide: true,
      });
    } catch {
      reject(new PreparationError('E_GENERATOR_SPAWN'));
      return;
    }
    const stdout = [];
    let stdoutBytes = 0;
    let stderrBytes = 0;
    let settled = false;
    let timedOut = false;
    const timer = setTimeout(() => {
      if (settled) return;
      timedOut = true;
      child.kill('SIGKILL');
    }, MAX_GENERATOR_SECONDS * 1000);
    const rejectOnce = (error) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      reject(error);
    };
    child.stdout.on('data', (chunk) => {
      const bytes = Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk);
      stdoutBytes += bytes.length;
      if (stdoutBytes > MAX_CHILD_STDOUT_BYTES) {
        child.kill('SIGKILL');
        rejectOnce(new PreparationError('E_GENERATOR_OUTPUT_LIMIT'));
        return;
      }
      stdout.push(bytes);
    });
    child.stderr.on('data', (chunk) => {
      const bytes = Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk);
      stderrBytes += bytes.length;
      if (stderrBytes > MAX_CHILD_STDERR_BYTES) {
        child.kill('SIGKILL');
        rejectOnce(new PreparationError('E_GENERATOR_OUTPUT_LIMIT'));
      }
    });
    child.once('error', () => rejectOnce(new PreparationError('E_GENERATOR_SPAWN')));
    child.once('close', (code) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      if (timedOut) {
        reject(new PreparationError('E_GENERATOR_TIMEOUT'));
        return;
      }
      if (code !== 0) {
        reject(new PreparationError('E_GENERATOR_FAILED'));
        return;
      }
      resolve(Buffer.concat(stdout));
    });
    child.stdin.on('error', () => {});
    child.stdin.end(inputBytes);
  });
}

function normalizeGeneratorOutput(bytes, job, options) {
  const output = parseCanonicalJson(bytes, 'E_GENERATOR_OUTPUT', true);
  assertKeys(output, GENERATOR_OUTPUT_KEYS, 'E_GENERATOR_OUTPUT');
  if (output.schema !== 'generated-resume-v1') fail('E_GENERATOR_OUTPUT');
  if (output.job_id !== job.id) fail('E_GENERATOR_BINDING');
  if (typeof output.artifact_ref !== 'string' || output.artifact_ref.length === 0 || output.artifact_ref.length > 1024) {
    fail('E_GENERATOR_OUTPUT');
  }
  if (output.artifact_ref.startsWith('/') || output.artifact_ref.includes('\\')) fail('E_GENERATOR_PATH');
  if (!Number.isSafeInteger(output.pages) || output.pages !== 1) fail('E_GENERATOR_PAGES');
  const field = requireNonblankString(output.field, 'E_GENERATOR_OUTPUT', 256);
  const graduationDate = requireNonblankString(output.graduation_date, 'E_GENERATOR_OUTPUT', 128);
  if (!Array.isArray(output.matched_keywords)
    || output.matched_keywords.length > MAX_MATCHED_KEYWORDS
    || output.matched_keywords.some((value) => typeof value !== 'string' || value.length > 256)) {
    fail('E_GENERATOR_OUTPUT');
  }
  const paths = {};
  for (const [key, expectedName] of [
    ['tex_path', 'resume.tex'],
    ['pdf_path', 'resume.pdf'],
    ['report_path', 'optimization.json'],
    ['manifest_path', 'manifest.json'],
  ]) {
    const value = requireAbsolutePath(output[key], 'E_GENERATOR_PATH');
    if (path.basename(value) !== expectedName) fail('E_GENERATOR_PATH');
    if (!pathWithin(options.resumeOutputRoot, value)) fail('E_GENERATOR_PATH');
    paths[key] = value;
  }
  const artifactDirectory = path.dirname(paths.pdf_path);
  const expectedArtifactDirectory = path.resolve(options.resumeOutputRoot, output.artifact_ref);
  if (artifactDirectory !== expectedArtifactDirectory
    || path.relative(options.resumeOutputRoot, artifactDirectory) !== output.artifact_ref
    || path.dirname(artifactDirectory) !== path.join(options.resumeOutputRoot, `job-${job.id}`)
    || !/^[0-9a-f]{16}$/u.test(path.basename(artifactDirectory))) {
    fail('E_GENERATOR_PATH');
  }
  for (const value of Object.values(paths)) {
    if (path.dirname(value) !== artifactDirectory) fail('E_GENERATOR_PATH');
  }
  return Object.freeze({
    artifactDirectory,
    artifactRef: output.artifact_ref,
    texPath: paths.tex_path,
    pdfPath: paths.pdf_path,
    reportPath: paths.report_path,
    manifestPath: paths.manifest_path,
    pages: output.pages,
    field,
    graduationDate,
    matchedKeywords: Object.freeze([...output.matched_keywords]),
  });
}

function pathWithin(root, candidate) {
  return candidate === root || candidate.startsWith(`${root}${path.sep}`);
}

async function existingArtifactNames(outputRoot, jobId) {
  const jobDirectory = path.join(outputRoot, `job-${jobId}`);
  let info;
  try {
    info = await fsp.lstat(jobDirectory);
  } catch (error) {
    if (error?.code === 'ENOENT') return new Set();
    fail('E_GENERATOR_CACHE');
  }
  if (info.isSymbolicLink() || !info.isDirectory()) fail('E_GENERATOR_CACHE');
  const owner = uid();
  if (modeBits(info.mode) !== PRIVATE_DIRECTORY_MODE
    || (owner !== undefined && info.uid !== owner)) fail('E_GENERATOR_CACHE');
  let children;
  try {
    children = await fsp.readdir(jobDirectory, { withFileTypes: true });
  } catch {
    fail('E_GENERATOR_CACHE');
  }
  const names = new Set();
  for (const child of children) {
    if (child.isSymbolicLink() || !child.isDirectory() || !/^[0-9a-f]{16}$/u.test(child.name)) {
      fail('E_GENERATOR_CACHE');
    }
    await verifyPrivateDirectory(path.join(jobDirectory, child.name), 'E_GENERATOR_CACHE');
    names.add(child.name);
  }
  return names;
}

function normalizeManifest(manifest, job, artifactDirectory) {
  assertKeys(manifest, MANIFEST_KEYS, 'E_GENERATOR_MANIFEST');
  if (!Number.isSafeInteger(manifest.schema_version) || ![1, 2].includes(manifest.schema_version)
    || manifest.generator_schema_version !== GENERATOR_SCHEMA_VERSION
    || typeof manifest.algorithm_sha256 !== 'string'
    || !SHA256_HEX.test(manifest.algorithm_sha256)
    || typeof manifest.fingerprint !== 'string'
    || !/^[0-9a-f]{64}$/u.test(manifest.fingerprint)
    || !manifest.fingerprint.startsWith(path.basename(artifactDirectory))
    || manifest.job_id !== job.id) {
    fail('E_GENERATOR_MANIFEST');
  }
  assertKeys(manifest.inputs, MANIFEST_INPUT_KEYS, 'E_GENERATOR_MANIFEST');
  if (manifest.inputs.job_sha256 !== resumeJobDigest(job)
    || !SHA256_HEX.test(manifest.inputs.profile_sha256)
    || !SHA256_HEX.test(manifest.inputs.template_sha256)
    || !SHA256_HEX.test(manifest.inputs.skill_sha256)
    || typeof manifest.inputs.compiler_identity !== 'string'
    || manifest.inputs.compiler_identity.length === 0
    || manifest.inputs.compiler_identity.length > 16 * 1024) {
    fail('E_GENERATOR_MANIFEST');
  }
  assertKeys(manifest.artifacts, MANIFEST_ARTIFACT_KEYS, 'E_GENERATOR_MANIFEST');
  if (typeof manifest.manifest_sha256 !== 'string' || !SHA256_HEX.test(manifest.manifest_sha256)) {
    fail('E_GENERATOR_MANIFEST');
  }
  const manifestBody = { ...manifest };
  delete manifestBody.manifest_sha256;
  if (sha256Bytes(canonicalJsonBytes(manifestBody)) !== manifest.manifest_sha256) {
    fail('E_GENERATOR_MANIFEST');
  }
  return manifest;
}

function validateManifestArtifactEntry(entry, bytes, maxBytes) {
  assertKeys(entry, new Set(['bytes', 'sha256']), 'E_GENERATOR_MANIFEST');
  if (!Number.isSafeInteger(entry.bytes) || entry.bytes < 0 || entry.bytes > maxBytes
    || !SHA256_HEX.test(entry.sha256)
    || entry.bytes !== bytes.length
    || entry.sha256 !== sha256Bytes(bytes)) {
    fail('E_GENERATOR_MANIFEST');
  }
}

async function validateGeneratedArtifacts(generated, job, inputDigests) {
  await assertExistingComponentsAreSafe(generated.artifactDirectory, 'E_GENERATOR_PATH');
  const files = {
    'resume.tex': await readPrivateFile(generated.texPath, MAX_RESUME_TEX_BYTES, 'E_GENERATOR_ARTIFACT'),
    'resume.pdf': await readPrivateFile(generated.pdfPath, MAX_RESUME_PDF_BYTES, 'E_GENERATOR_ARTIFACT'),
    'optimization.json': await readPrivateFile(generated.reportPath, MAX_REPORT_BYTES, 'E_GENERATOR_ARTIFACT'),
    'job_description.txt': await readPrivateFile(
      path.join(generated.artifactDirectory, 'job_description.txt'),
      MAX_DESCRIPTION_BYTES,
      'E_GENERATOR_ARTIFACT',
    ),
    manifest: await readPrivateFile(generated.manifestPath, MAX_MANIFEST_BYTES, 'E_GENERATOR_MANIFEST'),
  };
  if (!files['resume.pdf'].subarray(0, 5).equals(Buffer.from('%PDF-'))
    || !files['job_description.txt'].equals(Buffer.from(job.description, 'utf8'))) {
    fail('E_GENERATOR_ARTIFACT');
  }
  const manifest = normalizeManifest(
    parseCanonicalJson(files.manifest, 'E_GENERATOR_MANIFEST', false),
    job,
    generated.artifactDirectory,
  );
  if (manifest.inputs.profile_sha256 !== inputDigests.profile_sha256
    || manifest.inputs.template_sha256 !== inputDigests.template_sha256
    || manifest.inputs.skill_sha256 !== inputDigests.skill_sha256) {
    fail('E_GENERATOR_MANIFEST');
  }
  validateManifestArtifactEntry(manifest.artifacts['resume.tex'], files['resume.tex'], MAX_RESUME_TEX_BYTES);
  validateManifestArtifactEntry(manifest.artifacts['resume.pdf'], files['resume.pdf'], MAX_RESUME_PDF_BYTES);
  validateManifestArtifactEntry(manifest.artifacts['optimization.json'], files['optimization.json'], MAX_REPORT_BYTES);
  validateManifestArtifactEntry(manifest.artifacts['job_description.txt'], files['job_description.txt'], MAX_DESCRIPTION_BYTES);
  return sha256Bytes(files['resume.pdf']);
}

function resumeIdentity(generated, pdfSha256, reused) {
  return Object.freeze({
    pdfPath: generated.pdfPath,
    pdfSha256,
    manifestPath: generated.manifestPath,
    artifactRef: generated.artifactRef,
    pages: generated.pages,
    reused: reused === true,
  });
}

function emptyResult() {
  return Object.freeze({ kind: 'idle', job: null, resume: null, run: null, workspace: null });
}

async function boundJobOrNull(database, id) {
  let result;
  try {
    result = await loadBoundJob(database, id);
  } catch {
    fail('E_JOB_SOURCE');
  }
  if (result === null) return null;
  try {
    return normalizeBoundJob(result);
  } catch (error) {
    if (error instanceof PreparationError) throw error;
    fail('E_JOB_BINDING');
  }
}

async function listCandidates(database) {
  let result;
  try {
    result = await listBoundQueuedJobs(database);
  } catch {
    fail('E_JOB_SOURCE');
  }
  assertKeys(result, new Set(['count', 'jobs']), 'E_JOB_SUMMARY');
  if (!Number.isSafeInteger(result.count) || result.count < 0 || !Array.isArray(result.jobs)
    || result.count !== result.jobs.length) {
    fail('E_JOB_SUMMARY');
  }
  const ids = new Set();
  const jobs = result.jobs.map((item) => normalizeBoundSummary(item));
  for (const item of jobs) {
    if (ids.has(item.id)) fail('E_JOB_SUMMARY');
    ids.add(item.id);
  }
  return Object.freeze(jobs);
}

function recoverResume(run) {
  return Object.freeze({
    pdfPath: run.resumeArtifactPath,
    pdfSha256: run.resumeArtifactSha256,
    manifestPath: null,
    artifactRef: null,
    pages: null,
    reused: true,
  });
}

function runnerOptions(databaseOptions, staged, resume, job) {
  return {
    workspaceRoot: databaseOptions.workspaceRoot,
    jobDescriptionPath: staged.path,
    ...(databaseOptions.applicantProfilePath === undefined
      ? {}
      : { applicantProfilePath: databaseOptions.applicantProfilePath }),
    ...(databaseOptions.sourceResumePath === undefined
      ? {}
      : { sourceResumePath: databaseOptions.sourceResumePath }),
    resumeUploadPath: resume.pdfPath,
    answerMemoryPath: databaseOptions.answerMemoryPath,
    resumeArtifactPath: resume.pdfPath,
    resumeArtifactSha256: resume.pdfSha256,
    expectedJobBinding: {
      platform: job.platform,
      applicationUrl: job.applicationUrl,
      title: job.title,
      company: job.company,
      location: job.location,
      description: job.description,
      descriptionSha256: job.descriptionSha256,
      sourcePostedAt: job.sourcePostedAt,
    },
    ownerId: databaseOptions.ownerId,
    browserSessionId: databaseOptions.browserSessionId,
    now: databaseOptions.now,
    leaseSeconds: databaseOptions.leaseSeconds,
    maxActiveJobs: databaseOptions.maxActiveJobs,
  };
}

function workspaceOptions(databaseOptions, staged, resume, run) {
  return {
    workspaceRoot: databaseOptions.workspaceRoot,
    jobDescriptionPath: staged.path,
    ...(databaseOptions.applicantProfilePath === undefined
      ? {}
      : { applicantProfilePath: databaseOptions.applicantProfilePath }),
    ...(databaseOptions.sourceResumePath === undefined
      ? {}
      : { sourceResumePath: databaseOptions.sourceResumePath }),
    resumeUploadPath: resume.pdfPath,
    answerMemoryPath: databaseOptions.answerMemoryPath,
    resumeArtifactPath: resume.pdfPath,
    resumeArtifactSha256: resume.pdfSha256,
    startedAt: run.startedAt,
  };
}

async function generateBoundResumeInternal(job, options) {
  const normalizedJob = normalizeBoundJob(job);
  const generation = normalizeGenerationOptions(options);
  await validateGenerationInputs(generation);
  const profileBytes = await readPrivateFile(generation.resumeProfilePath, MAX_PROFILE_BYTES, 'E_RESUME_PROFILE_PATH');
  const templateBytes = await readPrivateFile(generation.resumeTemplatePath, MAX_TEMPLATE_BYTES, 'E_RESUME_TEMPLATE_PATH');
  const skillBytes = await readPrivateFile(generation.resumeSkillPath, MAX_SKILL_BYTES, 'E_RESUME_SKILL_PATH');
  const staged = await stageDescription(normalizedJob, generation);
  const before = await existingArtifactNames(generation.resumeOutputRoot, normalizedJob.id);
  let output;
  try {
    output = await runGenerator(resumeJobInput(normalizedJob), generation);
  } catch (error) {
    if (error instanceof PreparationError) throw error;
    fail('E_GENERATOR_FAILED');
  }
  const generated = normalizeGeneratorOutput(output, normalizedJob, generation);
  const inputDigests = {
    profile_sha256: sha256Bytes(profileBytes),
    template_sha256: sha256Bytes(templateBytes),
    skill_sha256: sha256Bytes(skillBytes),
  };
  const pdfSha256 = await validateGeneratedArtifacts(generated, normalizedJob, inputDigests);
  const reused = before.has(path.basename(generated.artifactDirectory));
  return Object.freeze({
    ...resumeIdentity(generated, pdfSha256, reused),
    _stagedDescriptionPath: staged.path,
    _job: normalizedJob,
  });
}

export async function generateBoundResume(job, options = {}) {
  try {
    const generated = await generateBoundResumeInternal(job, options);
    return Object.freeze({
      pdfPath: generated.pdfPath,
      pdfSha256: generated.pdfSha256,
      manifestPath: generated.manifestPath,
      artifactRef: generated.artifactRef,
      pages: generated.pages,
      reused: generated.reused,
    });
  } catch (error) {
    if (error instanceof PreparationError) throw error;
    fail('E_GENERATOR_FAILED');
  }
}

export async function prepareOrRecoverSupportedRun(database, options = {}) {
  try {
    normalizeCoordinatorShape(options);
    let recovered;
    try {
      recovered = await recoverActiveRun(database, {
        ownerId: options.ownerId,
        browserSessionId: options.browserSessionId,
        now: options.now,
        leaseSeconds: options.leaseSeconds,
      });
    } catch {
      fail('E_RECOVERY');
    }
    if (recovered !== null) {
      const job = await boundJobOrNull(database, recovered.jobId);
      if (job === null) fail('E_RECOVERY_BINDING');
      const normalized = await normalizeWorkspaceInputs(options);
      if (normalized.answerMemoryPath !== recovered.answerMemoryPath) fail('E_RECOVERY_BINDING');
      const persisted = Object.freeze({
        ...normalized,
        answerMemoryPath: recovered.answerMemoryPath,
      });
      const staged = await stageDescription(job, persisted);
      const resume = recoverResume(recovered);
      let workspace;
      try {
        workspace = await createJobWorkspace(
          recovered,
          workspaceOptions(persisted, staged, resume, recovered),
        );
      } catch {
        fail('E_WORKSPACE');
      }
      return Object.freeze({
        kind: 'recovered',
        job,
        resume,
        run: recovered,
        workspace,
      });
    }

    const normalized = await normalizeCoordinatorOptions(options);
    const summaries = await listCandidates(database);
    if (summaries.length === 0) return emptyResult();

    const summary = summaries[0];
    const job = await boundJobOrNull(database, summary.id);
    if (job === null) return emptyResult();
    if (!sameBoundIdentity(summary, job)) fail('E_JOB_BINDING');

    const generated = await generateBoundResumeInternal(job, generationOptionValues(normalized));
    const staged = Object.freeze({
      path: generated._stagedDescriptionPath,
      sha256: job.descriptionSha256,
    });
    const current = await boundJobOrNull(database, job.id);
    if (current === null) return emptyResult();
    if (!sameBoundJob(job, current)) fail('E_JOB_BINDING');

    let run;
    try {
      run = await claimSpecificQueuedJob(
        database,
        job.id,
        runnerOptions(normalized, staged, generated, job),
      );
    } catch (error) {
      if (error instanceof PreparationError) throw error;
      fail('E_CLAIM');
    }
    if (run === null) return emptyResult();
    if (run.jobId !== job.id
      || run.applicationUrl !== job.applicationUrl
      || run.resumeArtifactPath !== generated.pdfPath
      || run.resumeArtifactSha256 !== generated.pdfSha256) {
      fail('E_CLAIM_BINDING');
    }
    let workspace;
    try {
      workspace = await createJobWorkspace(
        run,
        workspaceOptions(normalized, staged, generated, run),
      );
    } catch {
      fail('E_WORKSPACE');
    }
    return Object.freeze({
      kind: 'claimed',
      job,
      resume: Object.freeze({
        pdfPath: generated.pdfPath,
        pdfSha256: generated.pdfSha256,
        manifestPath: generated.manifestPath,
        artifactRef: generated.artifactRef,
        pages: generated.pages,
        reused: generated.reused,
      }),
      run,
      workspace,
    });
  } catch (error) {
    if (error instanceof PreparationError) throw error;
    fail('E_PREPARATION');
  }
}
