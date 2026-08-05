import { spawn } from 'node:child_process';
import crypto from 'node:crypto';
import fs from 'node:fs';
import path from 'node:path';

import { openIngestionDatabase } from '../ingestion/database.mjs';

const SHA256 = /^[0-9a-f]{64}$/u;
const REASON = /^[a-z][a-z0-9_]{0,63}$/u;
const OPTION_KEYS = new Set([
  'pythonExecutable',
  'resumeProfilePath',
  'resumeTemplatePath',
  'resumeSkillPath',
  'resumeOutputRoot',
  'resumeCompiler',
  'now',
  'timeoutMs',
]);
const REQUIRED_OPTION_KEYS = Object.freeze([
  'pythonExecutable',
  'resumeProfilePath',
  'resumeTemplatePath',
  'resumeSkillPath',
  'resumeOutputRoot',
]);
const GENERATED_KEYS = new Set([
  'schema', 'job_id', 'artifact_ref', 'tex_path', 'pdf_path', 'report_path',
  'manifest_path', 'pages', 'field', 'graduation_date', 'matched_keywords',
]);
const MANIFEST_KEYS = new Set([
  'schema_version', 'generator_schema_version', 'algorithm_sha256', 'fingerprint',
  'job_id', 'inputs', 'artifacts', 'manifest_sha256',
]);
const MAX_PROCESS_OUTPUT = 1024 * 1024;
const MAX_MANIFEST_BYTES = 256 * 1024;
const DEFAULT_TIMEOUT_MS = 5 * 60 * 1000;

export class ResumePreparationError extends Error {
  constructor(code, options = {}) {
    super(code, options);
    this.name = 'ResumePreparationError';
    this.code = code;
  }
}

function fail(code, cause) {
  throw new ResumePreparationError(code, cause === undefined ? {} : { cause });
}

function exactKeys(value, keys, code) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) fail(code);
  const actual = Object.keys(value);
  if (actual.length !== keys.size || actual.some((key) => !keys.has(key))) fail(code);
  return value;
}

function canonicalize(value) {
  if (Array.isArray(value)) return `[${value.map(canonicalize).join(',')}]`;
  if (value && typeof value === 'object') {
    return `{${Object.keys(value).sort().map((key) => `${JSON.stringify(key)}:${canonicalize(value[key])}`).join(',')}}`;
  }
  return JSON.stringify(value);
}

function digest(value) {
  return crypto.createHash('sha256').update(value).digest('hex');
}

function normalizeNow(value) {
  const date = value === undefined ? new Date() : new Date(value);
  if (!Number.isFinite(date.getTime())) fail('invalid_preparation_time');
  return date.toISOString();
}

function absolutePath(value, code) {
  if (typeof value !== 'string' || !path.isAbsolute(value) || value.includes('\0')) fail(code);
  return path.resolve(value);
}
function canonicalOutputRoot(value) {
  try {
    return fs.realpathSync(absolutePath(value, 'invalid_resume_output_root'));
  } catch (error) {
    if (error instanceof ResumePreparationError) throw error;
    fail('invalid_resume_output_root', error);
  }
}

function normalizeOptions(options) {
  if (!options || typeof options !== 'object' || Array.isArray(options)
    || Object.keys(options).some((key) => !OPTION_KEYS.has(key))
    || REQUIRED_OPTION_KEYS.some((key) => !Object.hasOwn(options, key))) {
    fail('invalid_preparation_options');
  }
  const normalized = {
    pythonExecutable: absolutePath(options.pythonExecutable, 'invalid_python_executable'),
    resumeProfilePath: absolutePath(options.resumeProfilePath, 'invalid_resume_profile_path'),
    resumeTemplatePath: absolutePath(options.resumeTemplatePath, 'invalid_resume_template_path'),
    resumeSkillPath: absolutePath(options.resumeSkillPath, 'invalid_resume_skill_path'),
    resumeOutputRoot: canonicalOutputRoot(options.resumeOutputRoot),
    resumeCompiler: options.resumeCompiler === undefined
      ? undefined
      : absolutePath(options.resumeCompiler, 'invalid_resume_compiler'),
    now: normalizeNow(options.now),
    timeoutMs: options.timeoutMs === undefined ? DEFAULT_TIMEOUT_MS : options.timeoutMs,
  };
  if (!Number.isInteger(normalized.timeoutMs) || normalized.timeoutMs < 1000 || normalized.timeoutMs > 30 * 60 * 1000) {
    fail('invalid_preparation_timeout');
  }
  const executable = fs.statSync(normalized.pythonExecutable, { throwIfNoEntry: false });
  if (!executable?.isFile() || (executable.mode & 0o111) === 0) {
    fail('invalid_python_executable');
  }
  const outputRoot = fs.lstatSync(normalized.resumeOutputRoot, { throwIfNoEntry: false });
  if (!outputRoot?.isDirectory() || outputRoot.isSymbolicLink() || (outputRoot.mode & 0o777) !== 0o700
    || (typeof process.getuid === 'function' && outputRoot.uid !== process.getuid())) {
    fail('invalid_resume_output_root');
  }
  return normalized;
}

function queueOrderSql(alias = 'aj') {
  return `CASE ${alias}.eligibility_tier WHEN 'active_verified' THEN 0 WHEN 'unverified_stale' THEN 1 WHEN 'backfill_only' THEN 2 ELSE 3 END,
    ${alias}.source_last_seen_at IS NULL ASC, ${alias}.source_last_seen_at DESC, ${alias}.id ASC`;
}

function readCandidate(db, applicationJobId = null) {
  const target = applicationJobId === null
    ? `SELECT * FROM application_jobs AS aj WHERE aj.status = 'queued' ORDER BY ${queueOrderSql('aj')} LIMIT 1`
    : 'SELECT * FROM application_jobs AS aj WHERE aj.status = \'queued\' AND aj.id = ?';
  const sql = `WITH target AS (${target})
    SELECT target.id AS application_job_id, target.dedupe_group_id, j.id AS normalized_job_id,
      j.title, j.company, j.description, j.description_sha256, j.location, j.source_posted_at
    FROM target
    LEFT JOIN jobs AS j ON j.dedupe_group_id = target.dedupe_group_id
    ORDER BY CASE j.availability_state WHEN 'open' THEN 0 ELSE 1 END,
      CASE j.freshness_state WHEN 'current' THEN 0 WHEN 'unverified' THEN 1 ELSE 2 END,
      COALESCE(j.source_updated_at, j.source_posted_at, j.discovered_at) DESC,
      j.id DESC
    LIMIT 1`;
  return applicationJobId === null ? db.prepare(sql).get() : db.prepare(sql).get(applicationJobId);
}

function ensureCandidate(candidate) {
  if (!candidate) return null;
  if (!Number.isInteger(candidate.normalized_job_id)) fail('job_description_missing');
  if (typeof candidate.description !== 'string' || candidate.description.length === 0) fail('job_description_missing');
  if (typeof candidate.description_sha256 !== 'string' || !SHA256.test(candidate.description_sha256)) fail('job_description_invalid');
  if (digest(Buffer.from(candidate.description, 'utf8')) !== candidate.description_sha256) fail('job_description_mismatch');
  return candidate;
}

function runGenerator(candidate, options) {
  const args = [
    '-m', 'resume_generation.command',
    '--profile', options.resumeProfilePath,
    '--template', options.resumeTemplatePath,
    '--skill', options.resumeSkillPath,
    '--output-root', options.resumeOutputRoot,
  ];
  if (options.resumeCompiler !== undefined) args.push('--compiler', options.resumeCompiler);
  const job = {
    schema: 'resume-job-v1',
    id: candidate.application_job_id,
    title: candidate.title,
    company: candidate.company,
    description: candidate.description,
    location: candidate.location,
    posted_at: candidate.source_posted_at,
  };
  const env = { ...process.env, RESUME_ADVISORY_ENABLED: '' };
  delete env.OLLAMA_CLOUD_API_KEY;
  delete env.OLLAMA_API_KEY;
  return new Promise((resolve, reject) => {
    const child = spawn(options.pythonExecutable, args, {
      cwd: path.resolve('.'),
      env,
      detached: process.platform !== 'win32',
      stdio: ['pipe', 'pipe', 'pipe'],
    });
    const stdout = [];
    const stderr = [];
    let bytes = 0;
    let settled = false;
    const terminate = () => {
      if (child.pid === undefined) return;
      try {
        if (process.platform === 'win32') child.kill('SIGKILL');
        else process.kill(-child.pid, 'SIGKILL');
      } catch {}
    };
    const timer = setTimeout(() => {
      if (settled) return;
      settled = true;
      terminate();
      reject(new ResumePreparationError('resume_generation_timeout'));
    }, options.timeoutMs);
    const collect = (chunks) => (chunk) => {
      bytes += chunk.length;
      if (bytes > MAX_PROCESS_OUTPUT) {
        if (!settled) {
          settled = true;
          clearTimeout(timer);
          terminate();
          reject(new ResumePreparationError('resume_generation_output_limit'));
        }
        return;
      }
      chunks.push(chunk);
    };
    child.stdout.on('data', collect(stdout));
    child.stderr.on('data', collect(stderr));
    child.once('error', (error) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      reject(new ResumePreparationError('resume_generation_spawn_failed', { cause: error }));
    });
    child.once('close', (code) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      if (code !== 0) {
        reject(new ResumePreparationError('resume_generation_failed'));
        return;
      }
      try {
        resolve(JSON.parse(Buffer.concat(stdout).toString('utf8')));
      } catch (error) {
        reject(new ResumePreparationError('resume_generation_invalid_output', { cause: error }));
      }
    });
    child.stdin.end(Buffer.from(canonicalize(job), 'utf8'));
  });
}

function requirePrivateFile(filePath, outputRoot, maxBytes, code) {
  const resolved = absolutePath(filePath, code);
  const relative = path.relative(outputRoot, resolved);
  if (!relative || relative.startsWith('..') || path.isAbsolute(relative)) fail(code);
  let current = resolved;
  while (true) {
    const info = fs.lstatSync(current, { throwIfNoEntry: false });
    if (!info || info.isSymbolicLink() || (typeof process.getuid === 'function' && info.uid !== process.getuid())) fail(code);
    if (current === resolved) {
      if (!info.isFile() || (info.mode & 0o777) !== 0o600 || info.size > maxBytes) fail(code);
    } else if (!info.isDirectory() || (info.mode & 0o777) !== 0o700) {
      fail(code);
    }
    if (current === outputRoot) break;
    const parent = path.dirname(current);
    if (parent === current || !parent.startsWith(`${outputRoot}${path.sep}`) && parent !== outputRoot) fail(code);
    current = parent;
  }
  return { path: resolved, bytes: fs.readFileSync(resolved) };
}

function validateGenerated(generated, candidate, options) {
  exactKeys(generated, GENERATED_KEYS, 'resume_generation_invalid_output');
  if (generated.schema !== 'generated-resume-v1' || generated.job_id !== candidate.application_job_id || generated.pages !== 1) {
    fail('resume_generation_invalid_output');
  }
  const manifestFile = requirePrivateFile(generated.manifest_path, options.resumeOutputRoot, MAX_MANIFEST_BYTES, 'resume_manifest_invalid');
  let manifest;
  try {
    manifest = JSON.parse(manifestFile.bytes.toString('utf8'));
  } catch (error) {
    fail('resume_manifest_invalid', error);
  }
  exactKeys(manifest, MANIFEST_KEYS, 'resume_manifest_invalid');
  if (Buffer.from(canonicalize(manifest), 'utf8').compare(manifestFile.bytes) !== 0) fail('resume_manifest_noncanonical');
  if ((manifest.schema_version !== 1 && manifest.schema_version !== 2)
    || manifest.job_id !== candidate.application_job_id
    || typeof manifest.generator_schema_version !== 'string'
    || !SHA256.test(manifest.algorithm_sha256)
    || !SHA256.test(manifest.fingerprint)
    || !SHA256.test(manifest.manifest_sha256)) fail('resume_manifest_invalid');
  const body = { ...manifest };
  delete body.manifest_sha256;
  if (digest(Buffer.from(canonicalize(body), 'utf8')) !== manifest.manifest_sha256) fail('resume_manifest_self_digest_mismatch');
  const inputKeys = new Set(['job_sha256', 'profile_sha256', 'template_sha256', 'skill_sha256', 'compiler_identity']);
  exactKeys(manifest.inputs, inputKeys, 'resume_manifest_invalid');
  for (const key of ['job_sha256', 'profile_sha256', 'template_sha256', 'skill_sha256']) {
    if (typeof manifest.inputs[key] !== 'string' || !SHA256.test(manifest.inputs[key])) fail('resume_manifest_invalid');
  }
  if (typeof manifest.inputs.compiler_identity !== 'string' || manifest.inputs.compiler_identity.length === 0
    || manifest.inputs.compiler_identity.length > 4096) fail('resume_manifest_invalid');
  const jobPayload = {
    id: candidate.application_job_id,
    title: candidate.title,
    company: candidate.company,
    description: candidate.description,
    location: candidate.location,
    posted_at: candidate.source_posted_at,
  };
  if (manifest.inputs.job_sha256 !== digest(Buffer.from(canonicalize(jobPayload), 'utf8'))) fail('resume_manifest_job_mismatch');
  const artifactKeys = new Set(['resume.tex', 'resume.pdf', 'optimization.json', 'job_description.txt']);
  exactKeys(manifest.artifacts, artifactKeys, 'resume_manifest_invalid');
  const pdfEntry = manifest.artifacts['resume.pdf'];
  const descriptionEntry = manifest.artifacts['job_description.txt'];
  for (const entry of Object.values(manifest.artifacts)) {
    exactKeys(entry, new Set(['bytes', 'sha256']), 'resume_manifest_invalid');
    if (!Number.isInteger(entry.bytes) || entry.bytes < 0 || !SHA256.test(entry.sha256)) fail('resume_manifest_invalid');
  }
  if (path.basename(generated.manifest_path) !== 'manifest.json' || path.basename(generated.pdf_path) !== 'resume.pdf') {
    fail('resume_artifact_path_mismatch');
  }
  const pdf = requirePrivateFile(generated.pdf_path, options.resumeOutputRoot, 16 * 1024 * 1024, 'resume_pdf_invalid');
  const description = requirePrivateFile(path.join(path.dirname(manifestFile.path), 'job_description.txt'), options.resumeOutputRoot, 64 * 1024, 'resume_description_artifact_invalid');
  if (pdf.bytes.length !== pdfEntry.bytes || digest(pdf.bytes) !== pdfEntry.sha256) fail('resume_pdf_digest_mismatch');
  if (description.bytes.length !== descriptionEntry.bytes || digest(description.bytes) !== descriptionEntry.sha256) fail('resume_description_digest_mismatch');
  if (descriptionEntry.sha256 !== candidate.description_sha256
    || !description.bytes.equals(Buffer.from(candidate.description, 'utf8'))) fail('resume_description_binding_mismatch');
  if (path.dirname(pdf.path) !== path.dirname(manifestFile.path)) fail('resume_artifact_directory_mismatch');
  return Object.freeze({
    manifestPath: manifestFile.path,
    manifestSha256: digest(manifestFile.bytes),
    pdfPath: pdf.path,
    pdfSha256: pdfEntry.sha256,
    jobDescriptionPath: description.path,
    jobDescriptionSha256: descriptionEntry.sha256,
    generatorFingerprintSha256: manifest.fingerprint,
    generatorSchemaVersion: manifest.generator_schema_version,
    pages: 1,
  });
}

function markFailure(database, applicationJobId, reason, now) {
  if (!Number.isInteger(applicationJobId) || !REASON.test(reason)) return;
  const db = openIngestionDatabase(database);
  try {
    db.prepare(`UPDATE application_jobs
      SET current_resume_artifact_id = NULL, resume_preparation_state = 'failed',
        resume_preparation_reason = ?, resume_preparation_attempted_at = ?, resume_prepared_at = NULL
      WHERE id = ? AND status = 'queued'`).run(reason, now, applicationJobId);
  } finally {
    db.close();
  }
}

function persistArtifact(database, candidate, artifact, now) {
  const db = openIngestionDatabase(database);
  try {
    db.exec('BEGIN IMMEDIATE');
    const current = ensureCandidate(readCandidate(db, candidate.application_job_id));
    if (!current || current.normalized_job_id !== candidate.normalized_job_id
      || current.description_sha256 !== candidate.description_sha256) fail('resume_preparation_stale');
    const inserted = db.prepare(`INSERT OR IGNORE INTO resume_artifacts (
      application_job_id, normalized_job_id, job_description_sha256,
      generator_fingerprint_sha256, generator_schema_version, manifest_path,
      manifest_sha256, pdf_path, pdf_sha256, job_description_path, pages, created_at
    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)`).run(
      candidate.application_job_id,
      candidate.normalized_job_id,
      artifact.jobDescriptionSha256,
      artifact.generatorFingerprintSha256,
      artifact.generatorSchemaVersion,
      artifact.manifestPath,
      artifact.manifestSha256,
      artifact.pdfPath,
      artifact.pdfSha256,
      artifact.jobDescriptionPath,
      artifact.pages,
      now,
    );
    const row = db.prepare(`SELECT * FROM resume_artifacts
      WHERE application_job_id = ? AND job_description_sha256 = ? AND generator_fingerprint_sha256 = ?`).get(
      candidate.application_job_id,
      artifact.jobDescriptionSha256,
      artifact.generatorFingerprintSha256,
    );
    if (!row || row.normalized_job_id !== candidate.normalized_job_id
      || row.manifest_path !== artifact.manifestPath || row.manifest_sha256 !== artifact.manifestSha256
      || row.pdf_path !== artifact.pdfPath || row.pdf_sha256 !== artifact.pdfSha256
      || row.job_description_path !== artifact.jobDescriptionPath || row.pages !== 1) fail('resume_artifact_binding_mismatch');
    const update = db.prepare(`UPDATE application_jobs
      SET current_resume_artifact_id = ?, resume_preparation_state = 'ready',
        resume_preparation_reason = NULL, resume_preparation_attempted_at = ?, resume_prepared_at = ?
      WHERE id = ? AND status = 'queued'`).run(row.id, now, now, candidate.application_job_id);
    if (update.changes !== 1) fail('resume_preparation_stale');
    db.exec('COMMIT');
    return { row, reused: inserted.changes === 0 };
  } catch (error) {
    try { db.exec('ROLLBACK'); } catch {}
    throw error;
  } finally {
    db.close();
  }
}

function deepFreeze(value, seen = new Set()) {
  if (!value || typeof value !== 'object' || seen.has(value)) return value;
  seen.add(value);
  for (const child of Object.values(value)) deepFreeze(child, seen);
  return Object.freeze(value);
}

export async function prepareNextQueuedResume(database, options = {}) {
  const normalized = normalizeOptions(options);
  let candidate;
  const db = openIngestionDatabase(database);
  try {
    candidate = readCandidate(db);
  } finally {
    db.close();
  }
  if (!candidate) return null;
  try {
    ensureCandidate(candidate);
    const generated = await runGenerator(candidate, normalized);
    const artifact = validateGenerated(generated, candidate, normalized);
    const persisted = persistArtifact(database, candidate, artifact, normalized.now);
    return deepFreeze({
      applicationJobId: candidate.application_job_id,
      normalizedJobId: candidate.normalized_job_id,
      resumeArtifactId: persisted.row.id,
      jobDescriptionPath: artifact.jobDescriptionPath,
      jobDescriptionSha256: artifact.jobDescriptionSha256,
      manifestPath: artifact.manifestPath,
      manifestSha256: artifact.manifestSha256,
      pdfPath: artifact.pdfPath,
      pdfSha256: artifact.pdfSha256,
      generatorFingerprintSha256: artifact.generatorFingerprintSha256,
      generatorSchemaVersion: artifact.generatorSchemaVersion,
      pages: 1,
      reused: persisted.reused,
    });
  } catch (error) {
    const reason = error instanceof ResumePreparationError && REASON.test(error.code)
      ? error.code
      : 'resume_preparation_failed';
    markFailure(database, candidate.application_job_id, reason, normalized.now);
    if (error instanceof ResumePreparationError) throw error;
    throw new ResumePreparationError(reason, { cause: error });
  }
}
