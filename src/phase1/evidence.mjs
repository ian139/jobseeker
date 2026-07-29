import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import crypto from 'node:crypto';

export const EVIDENCE_SCHEMA_VERSION = 'phase1-evidence-v2';
export const DEFAULT_MAX_CANONICAL_JSON_BYTES = 1024 * 1024;
export const DEFAULT_MAX_INPUT_BYTES = 256 * 1024 * 1024;

const JOURNAL_NAME = 'action-journal.jsonl';
const COMPLETION_NAME = 'completion.json';
const RUN_NAME = 'run.json';
const PRIVATE_DIRECTORY_MODE = 0o700;
const PRIVATE_FILE_MODE = 0o600;
const JOURNAL_LOCK_NAME = '.action-journal.lock';
const HEX_SHA256 = /^[0-9a-f]{64}$/;
const ARTIFACT_NAME = /^[A-Za-z0-9][A-Za-z0-9._-]*$/;
const SEQUENCED_NAME = /^([a-z][a-z0-9-]*)-(\d+)\.json$/;
const ACTION_PREFIX = 'action';
const IDENTITY_PREFIXES = new Set(['input', 'screenshot', 'upload']);
const RECORD_PREFIXES = new Set([
  'observation',
  'diff',
  'action',
  'retry',
  'ledger',
  'audit',
  'input',
  'screenshot',
  'upload',
]);
const FINAL_AUDIT_SCHEMA_VERSION = 'phase1-audit-v2';
const FINAL_AUDIT_KEYS = new Set([
  'schema',
  'observation_id',
  'passed',
  'complete',
  'blockers',
  'stale_target_ids',
  'unresolved_field_ids',
  'invalid_field_ids',
  'unretained_field_ids',
  'revealed_field_ids',
  'final_candidate_target_ids',
  'final_review_boundary',
  'submit_action_count',
  'field_count',
  'target_count',
  'final',
]);
const FINAL_AUDIT_ARRAY_KEYS = new Set([
  'stale_target_ids',
  'unresolved_field_ids',
  'invalid_field_ids',
  'unretained_field_ids',
  'revealed_field_ids',
]);
const MAX_FINAL_AUDIT_REFS = 256;
const MAX_FINAL_AUDIT_REF_BYTES = 4096;
const MAX_FINAL_AUDIT_OBSERVATION_ID_BYTES = 180;
const MAX_FINAL_AUDIT_FIELD_COUNT = 1_000_000;
const COMPLETION_KEYS = new Set([
  'schema_version',
  'finalized_at',
  'final_audit',
  'screenshot',
  'upload',
  'action_journal',
  'submit_action_count',
]);
const COMPLETION_AUDIT_KEYS = new Set(['artifact', 'sha256']);
const COMPLETION_IDENTITY_KEYS = new Set(['artifact', 'path', 'size', 'sha256']);
const COMPLETION_JOURNAL_KEYS = new Set(['artifact', 'entries', 'sha256']);
const TERMINAL_SUBMIT_OUTCOMES = new Set(['succeeded', 'failed', 'blocked']);
const MAX_ACTION_ID_BYTES = 180;
const AUDIT_ARTIFACT_NAME = /^audit-\d+\.json$/;

const FLAGS = fs.constants;
const O_NOFOLLOW = FLAGS.O_NOFOLLOW ?? 0;
const O_CLOEXEC = FLAGS.O_CLOEXEC ?? 0;
const O_DIRECTORY = FLAGS.O_DIRECTORY ?? 0;
const OPEN_ROOT_FLAGS = FLAGS.O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC;
const OPEN_READ_FLAGS = FLAGS.O_RDONLY | O_NOFOLLOW | O_CLOEXEC;
const OPEN_WRITE_FLAGS = FLAGS.O_WRONLY | FLAGS.O_CREAT | FLAGS.O_EXCL | O_NOFOLLOW | O_CLOEXEC;
const OPEN_APPEND_FLAGS = FLAGS.O_WRONLY | FLAGS.O_CREAT | FLAGS.O_APPEND | O_NOFOLLOW | O_CLOEXEC;

const ERROR_MESSAGES = Object.freeze({
  CLOSED: 'evidence store is closed',
  ALREADY_FINALIZED: 'evidence store is already finalized',
  INVALID_ROOT: 'evidence root is invalid',
  ROOT_NOT_PRIVATE: 'evidence root is not private',
  ROOT_CHANGED: 'evidence root changed',
  UNSAFE_PATH: 'evidence path is unsafe',
  ARTIFACT_RESERVED: 'completion.json is reserved for finalize',
  ARTIFACT_EXISTS: 'evidence artifact already exists',
  ARTIFACT_MISSING: 'evidence artifact is missing',
  ARTIFACT_CORRUPT: 'evidence artifact is corrupt',
  ARTIFACT_WRITE_FAILED: 'evidence artifact could not be published',
  PAYLOAD_INVALID: 'evidence payload is invalid',
  PAYLOAD_TOO_LARGE: 'evidence payload is too large',
  JOURNAL_CORRUPT: 'evidence action journal is corrupt',
  JOURNAL_BUSY: 'evidence action journal is busy',
  INPUT_INVALID: 'evidence input file is invalid',
  INPUT_MISSING: 'evidence input file is missing',
  INPUT_UNREADABLE: 'evidence input file is unreadable',
  SUBMISSION_EVIDENCE: 'submission evidence is invalid or incomplete',
  SUBMIT_COUNT_REQUIRED: 'explicit submit action count is required',
  FINAL_AUDIT_REQUIRED: 'a complete final audit is required',
  IDENTITY_REQUIRED: 'screenshot and upload identities are required',
  IDENTITY_INVALID: 'file identity is invalid',
  FINALIZATION_FAILED: 'evidence completion could not be published',
});

export class EvidenceStoreError extends Error {
  constructor(code) {
    const message = ERROR_MESSAGES[code] ?? 'evidence operation failed';
    super(message);
    this.name = 'EvidenceStoreError';
    this.code = code;
  }
}

function fail(code) {
  throw new EvidenceStoreError(code);
}

function isPlainObject(value) {
  if (value === null || typeof value !== 'object' || Array.isArray(value)) return false;
  const prototype = Object.getPrototypeOf(value);
  return prototype === Object.prototype || prototype === null;
}

function normalizeJsonValue(value, seen, depth, maxDepth) {
  if (depth > maxDepth) fail('PAYLOAD_INVALID');
  if (value === null) return null;
  switch (typeof value) {
    case 'string':
      return value;
    case 'boolean':
      return value;
    case 'number':
      if (!Number.isFinite(value)) fail('PAYLOAD_INVALID');
      return Object.is(value, -0) ? 0 : value;
    case 'object': {
      if (seen.has(value)) fail('PAYLOAD_INVALID');
      seen.add(value);
      let normalized;
      if (Array.isArray(value)) {
        normalized = value.map((item) => normalizeJsonValue(item, seen, depth + 1, maxDepth));
      } else {
        const prototype = Object.getPrototypeOf(value);
        if (prototype !== Object.prototype && prototype !== null) fail('PAYLOAD_INVALID');
        normalized = {};
        for (const key of Object.keys(value).sort()) {
          normalized[key] = normalizeJsonValue(value[key], seen, depth + 1, maxDepth);
        }
      }
      seen.delete(value);
      return normalized;
    }
    default:
      fail('PAYLOAD_INVALID');
  }
}

/** Return compact, recursively key-sorted JSON suitable for private artifacts. */
export function canonicalJson(value, options = {}) {
  const maxBytes = options.maxBytes ?? DEFAULT_MAX_CANONICAL_JSON_BYTES;
  const maxDepth = options.maxDepth ?? 64;
  if (!Number.isSafeInteger(maxBytes) || maxBytes < 1) fail('PAYLOAD_INVALID');
  if (!Number.isSafeInteger(maxDepth) || maxDepth < 1) fail('PAYLOAD_INVALID');
  let normalized;
  try {
    normalized = normalizeJsonValue(value, new Set(), 0, maxDepth);
  } catch (error) {
    if (error instanceof EvidenceStoreError) throw error;
    fail('PAYLOAD_INVALID');
  }
  let text;
  try {
    text = JSON.stringify(normalized);
  } catch {
    fail('PAYLOAD_INVALID');
  }
  if (typeof text !== 'string') fail('PAYLOAD_INVALID');
  if (Buffer.byteLength(text, 'utf8') > maxBytes) fail('PAYLOAD_TOO_LARGE');
  return text;
}

export function canonicalJsonBytes(value, options = {}) {
  return Buffer.from(canonicalJson(value, options), 'utf8');
}

function canonicalParse(bytes, maxBytes, journal = false) {
  if (!Buffer.isBuffer(bytes) || bytes.length > maxBytes) fail(journal ? 'JOURNAL_CORRUPT' : 'ARTIFACT_CORRUPT');
  let value;
  try {
    value = JSON.parse(bytes.toString('utf8'));
  } catch {
    fail(journal ? 'JOURNAL_CORRUPT' : 'ARTIFACT_CORRUPT');
  }
  let canonical;
  try {
    canonical = canonicalJson(value, { maxBytes });
  } catch {
    fail(journal ? 'JOURNAL_CORRUPT' : 'ARTIFACT_CORRUPT');
  }
  if (canonical !== bytes.toString('utf8')) fail(journal ? 'JOURNAL_CORRUPT' : 'ARTIFACT_CORRUPT');
  return value;
}

function toAbsolutePath(value, invalidCode = 'INPUT_INVALID') {
  let raw;
  try {
    if (typeof value === 'string') raw = value;
    else if (value instanceof URL) raw = fileURLToPath(value);
    else fail(invalidCode);
  } catch {
    fail(invalidCode);
  }
  if (!raw || raw.includes('\0')) fail(invalidCode);
  return path.resolve(raw);
}

function normalizeRootPath(value) {
  let raw;
  try {
    if (typeof value === 'string') raw = value;
    else if (value instanceof URL) raw = fileURLToPath(value);
    else fail('INVALID_ROOT');
  } catch {
    fail('INVALID_ROOT');
  }
  if (!raw || raw.includes('\0')) fail('INVALID_ROOT');
  const parsed = path.parse(raw);
  if (raw.split(path.sep).includes('..')) fail('UNSAFE_PATH');
  const resolved = path.resolve(raw);
  if (resolved === parsed.root || resolved === path.parse(resolved).root) fail('INVALID_ROOT');
  return resolved;
}

function safeName(name) {
  if (typeof name !== 'string' || !name || name.length > 180 || name === '.' || name === '..') {
    fail('UNSAFE_PATH');
  }
  if (name.includes('\0') || name.includes('/') || name.includes('\\') || !ARTIFACT_NAME.test(name)) {
    fail('UNSAFE_PATH');
  }
  return name;
}

function modeBits(stat) {
  return stat.mode & 0o777;
}

function isCurrentOwner(stat) {
  const getuid = process.getuid;
  return typeof getuid !== 'function' || stat.uid === getuid();
}

function sameIdentity(left, right) {
  return left.dev === right.dev && left.ino === right.ino;
}

function validateIdentity(value) {
  if (!isPlainObject(value)) fail('IDENTITY_INVALID');
  const keys = Object.keys(value);
  if (keys.some((key) => !['path', 'size', 'sha256', 'kind'].includes(key))
    || !keys.includes('path')
    || !keys.includes('size')
    || !keys.includes('sha256')
    || keys.length > 4) {
    fail('IDENTITY_INVALID');
  }
  if (typeof value.path !== 'string' || !value.path || value.path.includes('\0')) fail('IDENTITY_INVALID');
  if (!Number.isSafeInteger(value.size) || value.size < 0) fail('IDENTITY_INVALID');
  if (typeof value.sha256 !== 'string' || !HEX_SHA256.test(value.sha256)) fail('IDENTITY_INVALID');
  if (value.kind !== undefined && typeof value.kind !== 'string') fail('IDENTITY_INVALID');
  return { path: value.path, size: value.size, sha256: value.sha256, ...(value.kind === undefined ? {} : { kind: value.kind }) };
}

function timestampsStable(left, right) {
  const timestampFields = ['mtimeNs', 'ctimeNs', 'birthtimeNs', 'mtimeMs', 'ctimeMs', 'birthtimeMs'];
  let compared = false;
  for (const field of timestampFields) {
    if (field in left && field in right && left[field] !== undefined && right[field] !== undefined) {
      compared = true;
      if (left[field] !== right[field]) return false;
    }
  }
  if (compared) return true;
  for (const field of ['mtime', 'ctime', 'birthtime']) {
    if (left[field] instanceof Date && right[field] instanceof Date) {
      compared = true;
      if (left[field].getTime() !== right[field].getTime()) return false;
    }
  }
  return compared;
}

function sameIdentityFields(expected, actual) {
  return expected.path === actual.path && expected.size === actual.size && expected.sha256 === actual.sha256;
}

function readExternalIdentity(filePath, maxInputBytes) {
  const absolute = toAbsolutePath(filePath);
  const maxBytes = BigInt(maxInputBytes);
  let before;
  try {
    before = fs.lstatSync(absolute, { bigint: true });
  } catch {
    fail('INPUT_MISSING');
  }
  if (!before.isFile() || before.isSymbolicLink() || before.size > maxBytes) fail('INPUT_INVALID');
  let fd;
  try {
    fd = fs.openSync(absolute, OPEN_READ_FLAGS);
  } catch {
    fail('INPUT_UNREADABLE');
  }
  try {
    const opened = fs.fstatSync(fd, { bigint: true });
    if (!opened.isFile() || !sameIdentity(before, opened)) fail('INPUT_INVALID');
    if (opened.size > maxBytes || opened.size > BigInt(Number.MAX_SAFE_INTEGER)) fail('INPUT_INVALID');
    const size = Number(opened.size);
    const hash = crypto.createHash('sha256');
    const buffer = Buffer.allocUnsafe(64 * 1024);
    let remaining = size;
    while (remaining > 0) {
      const wanted = Math.min(buffer.length, remaining);
      const count = fs.readSync(fd, buffer, 0, wanted, null);
      if (!count) fail('INPUT_UNREADABLE');
      hash.update(buffer.subarray(0, count));
      remaining -= count;
    }
    const after = fs.fstatSync(fd, { bigint: true });
    if (!sameIdentity(opened, after) || opened.size !== after.size || !timestampsStable(opened, after)) {
      fail('INPUT_INVALID');
    }
    return { path: absolute, size, sha256: hash.digest('hex') };
  } catch (error) {
    if (error instanceof EvidenceStoreError) throw error;
    fail('INPUT_UNREADABLE');
  } finally {
    try {
      fs.closeSync(fd);
    } catch {
      // The original error is more useful and still contains no caller value.
    }
  }
}
function validateFinalAudit(value) {
  if (!isPlainObject(value)) fail('FINAL_AUDIT_REQUIRED');
  const keys = Object.keys(value);
  const required = [
    'schema',
    'observation_id',
    'passed',
    'complete',
    'blockers',
    'stale_target_ids',
    'unresolved_field_ids',
    'invalid_field_ids',
    'unretained_field_ids',
    'revealed_field_ids',
    'final_candidate_target_ids',
    'final_review_boundary',
    'submit_action_count',
    'final',
  ];
  if (keys.some((key) => !FINAL_AUDIT_KEYS.has(key))
    || required.some((key) => !Object.hasOwn(value, key))
    || (!Object.hasOwn(value, 'field_count') && !Object.hasOwn(value, 'target_count'))) {
    fail('FINAL_AUDIT_REQUIRED');
  }
  if (value.schema !== FINAL_AUDIT_SCHEMA_VERSION) fail('FINAL_AUDIT_REQUIRED');
  if (typeof value.observation_id !== 'string'
    || !value.observation_id
    || Buffer.byteLength(value.observation_id, 'utf8') > MAX_FINAL_AUDIT_OBSERVATION_ID_BYTES) {
    fail('FINAL_AUDIT_REQUIRED');
  }
  if (value.passed !== true || value.complete !== true || value.final !== true) fail('FINAL_AUDIT_REQUIRED');
  if (!Number.isSafeInteger(value.submit_action_count) || value.submit_action_count < 0) fail('FINAL_AUDIT_REQUIRED');
  for (const countKey of ['field_count', 'target_count']) {
    if (Object.hasOwn(value, countKey)
      && (!Number.isSafeInteger(value[countKey]) || value[countKey] < 0 || value[countKey] > MAX_FINAL_AUDIT_FIELD_COUNT)) {
      fail('FINAL_AUDIT_REQUIRED');
    }
  }
  if (Object.hasOwn(value, 'field_count') && Object.hasOwn(value, 'target_count')
    && value.field_count !== value.target_count) {
    fail('FINAL_AUDIT_REQUIRED');
  }
  if (!Array.isArray(value.blockers) || value.blockers.length !== 0) fail('FINAL_AUDIT_REQUIRED');
  for (const key of FINAL_AUDIT_ARRAY_KEYS) {
    if (!Array.isArray(value[key]) || value[key].length !== 0) fail('FINAL_AUDIT_REQUIRED');
    const seen = new Set();
    for (const item of value[key]) {
      if (typeof item !== 'string'
        || item.length === 0
        || Buffer.byteLength(item, 'utf8') > MAX_FINAL_AUDIT_REF_BYTES
        || seen.has(item)) fail('FINAL_AUDIT_REQUIRED');
      seen.add(item);
    }
  }
  if (!Array.isArray(value.final_candidate_target_ids)
    || value.final_candidate_target_ids.length > MAX_FINAL_AUDIT_REFS) {
    fail('FINAL_AUDIT_REQUIRED');
  }
  const candidates = new Set();
  for (const targetId of value.final_candidate_target_ids) {
    if (typeof targetId !== 'string'
      || !targetId
      || Buffer.byteLength(targetId, 'utf8') > MAX_FINAL_AUDIT_REF_BYTES
      || candidates.has(targetId)) fail('FINAL_AUDIT_REQUIRED');
    candidates.add(targetId);
  }
  if (typeof value.final_review_boundary !== 'boolean') fail('FINAL_AUDIT_REQUIRED');
  if (value.final_candidate_target_ids.length === 0 && value.final_review_boundary !== true) {
    fail('FINAL_AUDIT_REQUIRED');
  }
  return value;
}
function requireExactKeys(value, allowed, code) {
  if (!isPlainObject(value)) fail(code);
  const keys = Object.keys(value);
  if (keys.length !== allowed.size || keys.some((key) => !allowed.has(key))) fail(code);
}

function deepFreeze(value) {
  if (value !== null && typeof value === 'object' && !Object.isFrozen(value)) {
    for (const child of Object.values(value)) deepFreeze(child);
    Object.freeze(value);
  }
  return value;
}

function validActionId(value) {
  return typeof value === 'string'
    && value.length > 0
    && !value.includes('\0')
    && Buffer.byteLength(value, 'utf8') <= MAX_ACTION_ID_BYTES;
}

function validateFinalSubmitJournal(journal) {
  if (!Array.isArray(journal)) fail('SUBMISSION_EVIDENCE');
  const begins = [];
  const beginsById = new Map();
  const results = new Map();
  const actionIds = new Set();
  for (const entry of journal) {
    if (!isPlainObject(entry)) fail('SUBMISSION_EVIDENCE');
    if (Object.prototype.hasOwnProperty.call(entry, 'action_id')) {
      if (!validActionId(entry.action_id) || actionIds.has(entry.action_id)) fail('SUBMISSION_EVIDENCE');
      actionIds.add(entry.action_id);
    }
    if (entry.action === 'final_submit') {
      if (entry.outcome !== 'attempted'
        || !validActionId(entry.action_id)
        || typeof entry.target_id !== 'string'
        || !validActionId(entry.target_id)
        || beginsById.has(entry.action_id)) {
        fail('SUBMISSION_EVIDENCE');
      }
      beginsById.set(entry.action_id, entry);
      begins.push(entry);
      continue;
    }
    if (entry.action === 'final_submit_result') {
      if (!validActionId(entry.attempt_id)
        || !TERMINAL_SUBMIT_OUTCOMES.has(entry.outcome)
        || results.has(entry.attempt_id)) {
        fail('SUBMISSION_EVIDENCE');
      }
      if (Object.prototype.hasOwnProperty.call(entry, 'error_code')
        && entry.error_code !== null
        && (typeof entry.error_code !== 'string' || !entry.error_code || Buffer.byteLength(entry.error_code, 'utf8') > 180)) {
        fail('SUBMISSION_EVIDENCE');
      }
      results.set(entry.attempt_id, entry);
    } else if (entry.action === 'submit') {
      fail('SUBMISSION_EVIDENCE');
    }
  }

  const actionSummary = [];
  let succeeded = 0;
  for (const begin of begins) {
    const result = results.get(begin.action_id);
    if (result === undefined || result.sequence <= begin.sequence) fail('SUBMISSION_EVIDENCE');
    if (result.outcome === 'succeeded') succeeded += 1;
    actionSummary.push({ action: 'final_submit', outcome: result.outcome });
  }
  if (results.size !== begins.length || succeeded !== 1) fail('SUBMISSION_EVIDENCE');
  for (const attemptId of results.keys()) {
    if (!beginsById.has(attemptId)) fail('SUBMISSION_EVIDENCE');
  }
  return { begins, actionSummary };
}

function validateCompletionHash(value, keys, expectedArtifact = null) {
  requireExactKeys(value, keys, 'ARTIFACT_CORRUPT');
  if (typeof value.artifact !== 'string'
    || value.artifact.length === 0
    || value.artifact.includes('\0')
    || (expectedArtifact !== null && value.artifact !== expectedArtifact)
    || !HEX_SHA256.test(value.sha256)) {
    fail('ARTIFACT_CORRUPT');
  }
  try {
    safeName(value.artifact);
  } catch {
    fail('ARTIFACT_CORRUPT');
  }
  return value;
}

function validateCompletionReportShape(report) {
  requireExactKeys(report, COMPLETION_KEYS, 'ARTIFACT_CORRUPT');
  if (report.schema_version !== EVIDENCE_SCHEMA_VERSION
    || typeof report.finalized_at !== 'string'
    || !/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$/.test(report.finalized_at)
    || !Number.isFinite(Date.parse(report.finalized_at))
    || !Number.isSafeInteger(report.submit_action_count)
    || report.submit_action_count < 0) {
    fail('ARTIFACT_CORRUPT');
  }
  validateCompletionHash(report.final_audit, COMPLETION_AUDIT_KEYS);
  if (!AUDIT_ARTIFACT_NAME.test(report.final_audit.artifact)) fail('ARTIFACT_CORRUPT');
  validateCompletionHash(report.action_journal, COMPLETION_JOURNAL_KEYS, JOURNAL_NAME);
  if (!Number.isSafeInteger(report.action_journal.entries) || report.action_journal.entries < 1) {
    fail('ARTIFACT_CORRUPT');
  }
  for (const [key, prefix] of [['screenshot', 'screenshot'], ['upload', 'upload']]) {
    const reference = report[key];
    requireExactKeys(reference, COMPLETION_IDENTITY_KEYS, 'ARTIFACT_CORRUPT');
    if (typeof reference.artifact !== 'string'
      || !new RegExp(`^${prefix}-\\d+\\.json$`).test(reference.artifact)
      || typeof reference.path !== 'string'
      || !path.isAbsolute(reference.path)
      || reference.path.includes('\0')
      || !Number.isSafeInteger(reference.size)
      || reference.size < 0
      || !HEX_SHA256.test(reference.sha256)) {
      fail('ARTIFACT_CORRUPT');
    }
    try {
      safeName(reference.artifact);
    } catch {
      fail('ARTIFACT_CORRUPT');
    }
    if (path.resolve(reference.path) !== reference.path) fail('ARTIFACT_CORRUPT');
  }
  return report;
}

const RUN_METADATA_SCHEMA_VERSION = 'phase1-run-evidence-v2';
const RUN_LOOP_CONTRACTS = new Set([
  'safe-batch-observe-act-reobserve',
  'one-field-observe-act-reobserve',
]);
const RUN_METADATA_KEYS = new Set([
  'schema',
  'application_url',
  'run_contract_sha256',
  'resume_upload_path',
  'resume_upload_sha256',
  'browser_mode',
  'perception_driver',
  'action_driver',
  'model_provider',
  'submit_policy',
  'loop_contract',
  'started_at',
]);

function normalizeRunMetadata(metadata) {
  if (!isPlainObject(metadata)) fail('PAYLOAD_INVALID');
  const keys = Object.keys(metadata);
  if (keys.length !== RUN_METADATA_KEYS.size || keys.some((key) => !RUN_METADATA_KEYS.has(key))) {
    fail('PAYLOAD_INVALID');
  }
  if (metadata.schema !== RUN_METADATA_SCHEMA_VERSION
    || metadata.browser_mode !== 'headed'
    || metadata.perception_driver !== 'image_agent_v1'
    || metadata.action_driver !== 'omp_computer'
    || !['codex', 'gemini'].includes(metadata.model_provider)
    || metadata.submit_policy !== 'omp_agent'
    || !RUN_LOOP_CONTRACTS.has(metadata.loop_contract)) {
    fail('PAYLOAD_INVALID');
  }
  if (typeof metadata.application_url !== 'string' || !/^https?:\/\//.test(metadata.application_url)) fail('PAYLOAD_INVALID');
  let applicationUrl;
  try {
    applicationUrl = new URL(metadata.application_url);
  } catch {
    fail('PAYLOAD_INVALID');
  }
  if (applicationUrl.protocol !== 'http:' && applicationUrl.protocol !== 'https:') fail('PAYLOAD_INVALID');
  if (!HEX_SHA256.test(metadata.run_contract_sha256) || !HEX_SHA256.test(metadata.resume_upload_sha256)) {
    fail('PAYLOAD_INVALID');
  }
  if (typeof metadata.resume_upload_path !== 'string' || !path.isAbsolute(metadata.resume_upload_path)) fail('PAYLOAD_INVALID');
  const resumePath = toAbsolutePath(metadata.resume_upload_path, 'PAYLOAD_INVALID');
  if (resumePath !== metadata.resume_upload_path) fail('PAYLOAD_INVALID');
  if (typeof metadata.started_at !== 'string'
    || !/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{3})?Z$/.test(metadata.started_at)
    || !Number.isFinite(Date.parse(metadata.started_at))) fail('PAYLOAD_INVALID');
  return { ...metadata, resume_upload_path: resumePath };
}

export function sha256File(filePath, options = {}) {
  const maxInputBytes = options.maxInputBytes ?? DEFAULT_MAX_INPUT_BYTES;
  return readExternalIdentity(filePath, maxInputBytes);
}

export const hashRegularFile = sha256File;
export const hashFile = sha256File;

function fsyncDirectory(fd) {
  try {
    fs.fsyncSync(fd);
  } catch (error) {
    // Directory fsync is unavailable on some non-POSIX filesystems. File
    // contents are always fsynced; POSIX publication failures are surfaced.
    if (error?.code !== 'EINVAL' && error?.code !== 'ENOTSUP' && error?.code !== 'EBADF') throw error;
  }
}

export class EvidenceStore {
  constructor(root, options = {}) {
    this._rootPath = normalizeRootPath(root);
    this._maxJsonBytes = options.maxJsonBytes ?? DEFAULT_MAX_CANONICAL_JSON_BYTES;
    this._maxInputBytes = options.maxInputBytes ?? DEFAULT_MAX_INPUT_BYTES;
    if (!Number.isSafeInteger(this._maxJsonBytes) || this._maxJsonBytes < 1) fail('PAYLOAD_INVALID');
    if (!Number.isSafeInteger(this._maxInputBytes) || this._maxInputBytes < 1) fail('PAYLOAD_INVALID');
    this._rootFd = null;
    this._closed = false;
    this._openRoot();
  }

  _openRoot() {
    let stat;
    try {
      stat = fs.lstatSync(this._rootPath);
    } catch (error) {
      if (error?.code !== 'ENOENT') fail('INVALID_ROOT');
      try {
        fs.mkdirSync(this._rootPath, { mode: PRIVATE_DIRECTORY_MODE, recursive: true });
      } catch {
        fail('INVALID_ROOT');
      }
      try {
        stat = fs.lstatSync(this._rootPath);
      } catch {
        fail('INVALID_ROOT');
      }
    }
    if (stat.isSymbolicLink() || !stat.isDirectory()) fail('INVALID_ROOT');
    try {
      fs.chmodSync(this._rootPath, PRIVATE_DIRECTORY_MODE);
      this._rootFd = fs.openSync(this._rootPath, OPEN_ROOT_FLAGS);
      const opened = fs.fstatSync(this._rootFd);
      if (!opened.isDirectory() || !isCurrentOwner(opened) || modeBits(opened) & 0o077) fail('ROOT_NOT_PRIVATE');
      if (!sameIdentity(stat, opened)) fail('ROOT_CHANGED');
      fs.fchmodSync(this._rootFd, PRIVATE_DIRECTORY_MODE);
      fsyncDirectory(this._rootFd);
    } catch (error) {
      if (this._rootFd !== null) {
        try {
          fs.closeSync(this._rootFd);
        } catch {
          // Keep the stable domain error below.
        }
        this._rootFd = null;
      }
      if (error instanceof EvidenceStoreError) throw error;
      fail('INVALID_ROOT');
    }
  }

  get root() {
    return this._rootPath;
  }

  get rootPath() {
    return this._rootPath;
  }

  get journalPath() {
    return JOURNAL_NAME;
  }

  get finalized() {
    if (!this._exists(COMPLETION_NAME)) return false;
    try {
      this._readValidatedCompletion();
      return true;
    } catch {
      return false;
    }
  }

  _ensureOpen() {
    if (this._closed || this._rootFd === null) fail('CLOSED');
    return this._rootFd;
  }

  _assertRoot() {
    const fd = this._ensureOpen();
    let pathStat;
    let fdStat;
    try {
      pathStat = fs.lstatSync(this._rootPath);
      fdStat = fs.fstatSync(fd);
    } catch {
      fail('ROOT_CHANGED');
    }
    if (pathStat.isSymbolicLink() || !pathStat.isDirectory() || !fdStat.isDirectory() || !sameIdentity(pathStat, fdStat)) fail('ROOT_CHANGED');
    if (!isCurrentOwner(fdStat) || modeBits(fdStat) & 0o077) fail('ROOT_NOT_PRIVATE');
    return fd;
  }

  _descriptorRootPath() {
    const fd = this._assertRoot();
    if (process.platform === 'linux') return `/proc/self/fd/${fd}`;
    // Node does not expose openat(2), and Darwin's /dev/fd directory handles
    // are not traversable. Every pathname open is therefore followed by an
    // identity check before any caller bytes are written.
    return this._rootPath;
  }

  _descriptorPath(name) {
    if (name !== JOURNAL_LOCK_NAME) safeName(name);
    return path.join(this._descriptorRootPath(), name);
  }

  _ensureMutable() {
    this._assertRoot();
    if (this._exists(COMPLETION_NAME)) fail('ALREADY_FINALIZED');
  }

  _exists(name) {
    safeName(name);
    try {
      const stat = fs.lstatSync(this._descriptorPath(name));
      return stat !== undefined;
    } catch (error) {
      if (error?.code === 'ENOENT') return false;
      throw error;
    }
  }

  _safeArtifactPath(name) {
    return this._descriptorPath(name);
  }

  _rawArtifact(name, options = {}) {
    const maxBytes = options.maxBytes ?? this._maxJsonBytes;
    const journal = options.journal === true;
    const absolute = this._safeArtifactPath(name);
    let pre;
    try {
      pre = fs.lstatSync(absolute);
    } catch (error) {
      if (error?.code === 'ENOENT') fail('ARTIFACT_MISSING');
      fail(journal ? 'JOURNAL_CORRUPT' : 'ARTIFACT_CORRUPT');
    }
    if (pre.isSymbolicLink() || !pre.isFile() || !isCurrentOwner(pre) || modeBits(pre) & 0o077) {
      fail(journal ? 'JOURNAL_CORRUPT' : 'ARTIFACT_CORRUPT');
    }
    if (pre.size > maxBytes) fail(journal ? 'JOURNAL_CORRUPT' : 'ARTIFACT_CORRUPT');
    let fd;
    try {
      fd = fs.openSync(absolute, OPEN_READ_FLAGS);
    } catch {
      fail(journal ? 'JOURNAL_CORRUPT' : 'ARTIFACT_CORRUPT');
    }
    try {
      const opened = fs.fstatSync(fd);
      if (!opened.isFile()
        || !isCurrentOwner(opened)
        || !sameIdentity(pre, opened)
        || opened.size > maxBytes) {
        fail(journal ? 'JOURNAL_CORRUPT' : 'ARTIFACT_CORRUPT');
      }
      const bytes = Buffer.alloc(opened.size);
      let offset = 0;
      while (offset < bytes.length) {
        const count = fs.readSync(fd, bytes, offset, bytes.length - offset, null);
        if (!count) fail(journal ? 'JOURNAL_CORRUPT' : 'ARTIFACT_CORRUPT');
        offset += count;
      }
      const after = fs.fstatSync(fd);
      if (!sameIdentity(opened, after) || opened.size !== after.size || !isCurrentOwner(after)) {
        fail(journal ? 'JOURNAL_CORRUPT' : 'ARTIFACT_CORRUPT');
      }
      return bytes;
    } catch (error) {
      if (error instanceof EvidenceStoreError) throw error;
      fail(journal ? 'JOURNAL_CORRUPT' : 'ARTIFACT_CORRUPT');
    } finally {
      try {
        fs.closeSync(fd);
      } catch {
        // Keep the stable domain error above.
      }
    }
  }

  _publicationRef(name, bytes) {
    const absolute = path.join(this._rootPath, name);
    return {
      name,
      path: name,
      relativePath: name,
      artifactPath: name,
      absolutePath: absolute,
      bytes: bytes.length,
      size: bytes.length,
      sha256: crypto.createHash('sha256').update(bytes).digest('hex'),
    };
  }

  _atomicPublish(name, bytes, options = {}) {
    safeName(name);
    if (name === COMPLETION_NAME && options.allowCompletion !== true) fail('ARTIFACT_RESERVED');
    if (!Buffer.isBuffer(bytes) || bytes.length > this._maxJsonBytes) fail('PAYLOAD_TOO_LARGE');
    const rootFd = this._assertRoot();
    const temporary = `tmp-${name}-${crypto.randomUUID().replaceAll('-', '')}.tmp`;
    safeName(temporary);
    const temporaryPath = this._descriptorPath(temporary);
    let fd = null;
    let published = false;
    try {
      fd = fs.openSync(temporaryPath, OPEN_WRITE_FLAGS, PRIVATE_FILE_MODE);
      this._assertRoot();
      fs.fchmodSync(fd, PRIVATE_FILE_MODE);
      let offset = 0;
      while (offset < bytes.length) {
        const count = fs.writeSync(fd, bytes, offset, bytes.length - offset, null);
        if (!count) fail('ARTIFACT_WRITE_FAILED');
        offset += count;
      }
      fs.fsyncSync(fd);
      fs.closeSync(fd);
      fd = null;
      this._assertRoot();
      const targetPath = this._descriptorPath(name);
      try {
        fs.linkSync(temporaryPath, targetPath);
      } catch (error) {
        if (error?.code === 'EEXIST') fail('ARTIFACT_EXISTS');
        fail('ARTIFACT_WRITE_FAILED');
      }
      published = true;
      try {
        fs.unlinkSync(temporaryPath);
      } catch {
        // The target is already published; leave the hardlink cleanup to the
        // next maintenance pass rather than attempting a destructive rename.
      }
      fsyncDirectory(rootFd);
      this._assertRoot();
      return this._publicationRef(name, bytes);
    } catch (error) {
      if (error instanceof EvidenceStoreError) throw error;
      fail('ARTIFACT_WRITE_FAILED');
    } finally {
      if (fd !== null) {
        try {
          fs.closeSync(fd);
        } catch {
          // no caller value is exposed
        }
      }
      if (!published) {
        try {
          fs.unlinkSync(temporaryPath);
        } catch {
          // no caller value is exposed
        }
      }
    }
  }
  _nextIndex(prefix) {
    if (!RECORD_PREFIXES.has(prefix)) fail('PAYLOAD_INVALID');
    this._assertRoot();
    let names;
    try {
      names = fs.readdirSync(this._descriptorRootPath());
    } catch {
      fail('ARTIFACT_CORRUPT');
    }
    let max = 0;
    const pattern = new RegExp(`^${prefix}-(\\d+)\\.json$`);
    for (const name of names) {
      const match = pattern.exec(name);
      if (match) {
        const number = Number(match[1]);
        if (Number.isSafeInteger(number) && number > max) max = number;
      }
    }
    return max + 1;
  }
  _writeJson(name, value, options = {}) {
    const bytes = canonicalJsonBytes(value, { maxBytes: this._maxJsonBytes });
    return this._atomicPublish(name, bytes, options);
  }

  _writeUnique(prefix, value) {
    const bytes = canonicalJsonBytes(value, { maxBytes: this._maxJsonBytes });
    let index = this._nextIndex(prefix);
    for (let attempts = 0; attempts < 100000; attempts += 1) {
      const name = `${prefix}-${String(index).padStart(6, '0')}.json`;
      try {
        return this._atomicPublish(name, bytes);
      } catch (error) {
        if (!(error instanceof EvidenceStoreError) || error.code !== 'ARTIFACT_EXISTS') throw error;
        index += 1;
      }
    }
    fail('ARTIFACT_WRITE_FAILED');
  }

  writeJsonArtifact(name, value) {
    this._ensureMutable();
    return this._writeJson(safeName(name), value);
  }

  writeArtifact(name, value) {
    return this.writeJsonArtifact(name, value);
  }

  readArtifact(name) {
    const safe = safeName(name);
    if (safe === COMPLETION_NAME) return this._readValidatedCompletion().report;
    const bytes = this._rawArtifact(safe);
    return canonicalParse(bytes, this._maxJsonBytes, false);
  }

  readJson(name) {
    return this.readArtifact(name);
  }

  getArtifact(name) {
    return this.readArtifact(name);
  }

  listArtifacts() {
    this._assertRoot();
    let names;
    try {
      names = fs.readdirSync(this._descriptorRootPath());
    } catch {
      fail('ARTIFACT_CORRUPT');
    }
    return names.filter((name) => name.endsWith('.json') || name.endsWith('.jsonl')).sort();
  }

  _record(prefix, value) {
    this._ensureMutable();
    if (!isPlainObject(value)) fail('PAYLOAD_INVALID');
    return this._writeUnique(prefix, value);
  }

  recordRunMetadata(metadata) {
    this._ensureMutable();
    const normalized = normalizeRunMetadata(metadata);
    const verified = readExternalIdentity(normalized.resume_upload_path, this._maxInputBytes);
    if (verified.sha256 !== normalized.resume_upload_sha256) fail('IDENTITY_INVALID');
    return this._writeJson(RUN_NAME, normalized);
  }

  recordRun(metadata) {
    return this.recordRunMetadata(metadata);
  }

  recordObservation(observation) {
    return this._record('observation', observation);
  }

  recordDiff(diff) {
    return this._record('diff', diff);
  }

  recordRetry(retry) {
    return this._record('retry', retry);
  }

  recordLedger(ledger) {
    return this._record('ledger', ledger);
  }

  recordAudit(audit, options = {}) {
    this._ensureMutable();
    if (!isPlainObject(audit)) fail('PAYLOAD_INVALID');
    const final = options?.final === true || options?.isFinal === true;
    const candidate = final && audit.final !== true ? { ...audit, final: true } : audit;
    if (final) validateFinalAudit(candidate);
    return this._writeUnique('audit', candidate);
  }

  recordFinalAudit(audit) {
    return this.recordAudit(audit, { final: true });
  }

  _acquireJournalLock() {
    this._assertRoot();
    const lockPath = this._descriptorPath(JOURNAL_LOCK_NAME);
    const waitBuffer = new Int32Array(new SharedArrayBuffer(4));
    for (let attempt = 0; attempt < 200; attempt += 1) {
      let fd = null;
      try {
        fd = fs.openSync(lockPath, OPEN_WRITE_FLAGS, PRIVATE_FILE_MODE);
        this._assertRoot();
        fs.fchmodSync(fd, PRIVATE_FILE_MODE);
        return { fd, lockPath };
      } catch (error) {
        if (fd !== null) {
          try { fs.closeSync(fd); } catch {}
          try { fs.unlinkSync(lockPath); } catch {}
        }
        if (error instanceof EvidenceStoreError) throw error;
        if (error?.code !== 'EEXIST') fail('JOURNAL_BUSY');
        Atomics.wait(waitBuffer, 0, 0, 2);
      }
    }
    fail('JOURNAL_BUSY');
  }

  _releaseJournalLock(lock) {
    try {
      fs.closeSync(lock.fd);
    } catch {
      // Preserve the operation's primary result.
    }
    try {
      fs.unlinkSync(lock.lockPath);
      fsyncDirectory(this._assertRoot());
    } catch {
      // A stale lock is safer than an unlocked append; next append reports busy.
    }
  }

  _readJournal() {
    if (!this._exists(JOURNAL_NAME)) return [];
    const bytes = this._rawArtifact(JOURNAL_NAME, { maxBytes: this._maxJsonBytes * 16, journal: true });
    if (bytes.length === 0) return [];
    const text = bytes.toString('utf8');
    if (!text.endsWith('\n')) fail('JOURNAL_CORRUPT');
    const lines = text.slice(0, -1).split('\n');
    const entries = [];
    for (const line of lines) {
      if (!line || Buffer.byteLength(line, 'utf8') > this._maxJsonBytes) fail('JOURNAL_CORRUPT');
      const value = canonicalParse(Buffer.from(line, 'utf8'), this._maxJsonBytes, true);
      if (!isPlainObject(value) || !Number.isSafeInteger(value.sequence) || value.sequence !== entries.length + 1) fail('JOURNAL_CORRUPT');
      entries.push(value);
    }
    return entries;
  }

  _appendJournalEntry(action) {
    if (!isPlainObject(action)) fail('PAYLOAD_INVALID');
    if (Object.prototype.hasOwnProperty.call(action, 'sequence')) fail('PAYLOAD_INVALID');
    const normalized = normalizeJsonValue(action, new Set(), 0, 64);
    const lock = this._acquireJournalLock();
    let fd = null;
    try {
      const entries = this._readJournal();
      const entry = { sequence: entries.length + 1, ...normalized };
      const line = Buffer.from(`${canonicalJson(entry, { maxBytes: this._maxJsonBytes })}\n`, 'utf8');
      const journalPath = this._descriptorPath(JOURNAL_NAME);
      fd = fs.openSync(journalPath, OPEN_APPEND_FLAGS, PRIVATE_FILE_MODE);
      this._assertRoot();
      const stat = fs.fstatSync(fd);
      if (!stat.isFile() || modeBits(stat) & 0o077) fail('JOURNAL_CORRUPT');
      let offset = 0;
      while (offset < line.length) {
        const count = fs.writeSync(fd, line, offset, line.length - offset, null);
        if (!count) fail('JOURNAL_CORRUPT');
        offset += count;
      }
      fs.fsyncSync(fd);
      fs.closeSync(fd);
      fd = null;
      fsyncDirectory(this._assertRoot());
      return { entry, bytes: line };
    } catch (error) {
      if (error instanceof EvidenceStoreError) throw error;
      fail('JOURNAL_CORRUPT');
    } finally {
      if (fd !== null) {
        try {
          fs.closeSync(fd);
        } catch {
          // stable domain error already selected
        }
      }
      this._releaseJournalLock(lock);
    }
  }

  appendAction(action) {
    this._ensureMutable();
    const result = this._appendJournalEntry(action);
    return {
      ...this._publicationRef(JOURNAL_NAME, this._rawArtifact(JOURNAL_NAME, { maxBytes: this._maxJsonBytes * 16, journal: true })),
      sequence: result.entry.sequence,
      entry: result.entry,
    };
  }

  recordAction(action) {
    this._ensureMutable();
    if (!isPlainObject(action)) fail('PAYLOAD_INVALID');
    const artifact = this._writeUnique(ACTION_PREFIX, action);
    const journal = this._appendJournalEntry(action);
    return {
      ...artifact,
      sequence: journal.entry.sequence,
      journalPath: JOURNAL_NAME,
      journalSequence: journal.entry.sequence,
    };
  }

  readActionJournal() {
    return this._readJournal();
  }

  recordFileIdentity(filePath, kind = 'input') {
    this._ensureMutable();
    let sourcePath = filePath;
    let identityKind = kind;
    if (isPlainObject(filePath)) {
      sourcePath = filePath.path;
      identityKind = filePath.kind ?? kind;
    } else if (typeof filePath === 'string' && IDENTITY_PREFIXES.has(filePath) && typeof kind !== 'undefined' && !IDENTITY_PREFIXES.has(kind)) {
      identityKind = filePath;
      sourcePath = kind;
    }
    if (typeof identityKind !== 'string' || !IDENTITY_PREFIXES.has(identityKind)) fail('IDENTITY_INVALID');
    const identity = readExternalIdentity(sourcePath, this._maxInputBytes);
    const artifact = this._writeUnique(identityKind, identity);
    return {
      ...artifact,
      path: identity.path,
      sourcePath: identity.path,
      size: identity.size,
      sha256: identity.sha256,
      artifactSha256: artifact.sha256,
      identitySha256: identity.sha256,
      kind: identityKind,
    };
  }

  recordInput(filePath) {
    return this.recordFileIdentity(filePath, 'input');
  }

  recordInputFile(filePath) {
    return this.recordInput(filePath);
  }

  recordScreenshot(filePath) {
    return this.recordFileIdentity(filePath, 'screenshot');
  }

  recordUpload(filePath) {
    return this.recordFileIdentity(filePath, 'upload');
  }

  hashFile(filePath) {
    return readExternalIdentity(filePath, this._maxInputBytes);
  }

  sha256File(filePath) {
    return this.hashFile(filePath);
  }

  _latestArtifact(prefix) {
    const pattern = new RegExp(`^${prefix}-(\\d+)\\.json$`);
    const names = this.listArtifacts()
      .map((name) => ({ name, match: pattern.exec(name) }))
      .filter((item) => item.match)
      .sort((left, right) => Number(right.match[1]) - Number(left.match[1]));
    return names[0]?.name;
  }

  _artifactRecord(name) {
    const bytes = this._rawArtifact(name);
    return { value: canonicalParse(bytes, this._maxJsonBytes, false), ref: this._publicationRef(name, bytes) };
  }

  _identityFromArtifact(prefix, reference) {
    safeName(reference);
    if (!new RegExp(`^${prefix}-\\d+\\.json$`).test(reference)) fail('IDENTITY_INVALID');
    const record = this._artifactRecord(reference);
    const identity = validateIdentity(record.value);
    if (identity.kind !== undefined && identity.kind !== prefix) fail('IDENTITY_INVALID');
    const fresh = readExternalIdentity(identity.path, this._maxInputBytes);
    if (!sameIdentityFields(identity, fresh)) fail('IDENTITY_INVALID');
    return { identity: fresh, ref: record.ref };
  }

  _resolveIdentity(prefix, supplied) {
    if (supplied === undefined || supplied === null) {
      const latest = this._latestArtifact(prefix);
      if (!latest) fail('IDENTITY_REQUIRED');
      return this._identityFromArtifact(prefix, latest);
    }
    if (typeof supplied === 'string') {
      let artifactName = null;
      try {
        safeName(supplied);
        artifactName = supplied;
      } catch {
        artifactName = null;
      }
      if (artifactName !== null && this._exists(artifactName)) {
        if (!new RegExp(`^${prefix}-\\d+\\.json$`).test(artifactName)) fail('IDENTITY_INVALID');
        return this._identityFromArtifact(prefix, artifactName);
      }
      if (artifactName !== null && SEQUENCED_NAME.test(artifactName)) fail('IDENTITY_INVALID');
      const identity = readExternalIdentity(supplied, this._maxInputBytes);
      const ref = this._writeUnique(prefix, identity);
      return { identity, ref };
    }
    if (!isPlainObject(supplied)) fail('IDENTITY_INVALID');
    if (supplied.kind !== undefined && supplied.kind !== prefix) fail('IDENTITY_INVALID');
    const artifactName = supplied.artifactPath ?? supplied.relativePath ?? supplied.artifact ?? supplied.name;
    if (artifactName !== undefined) {
      if (typeof artifactName !== 'string') fail('IDENTITY_INVALID');
      try {
        safeName(artifactName);
      } catch {
        fail('IDENTITY_INVALID');
      }
      if (!new RegExp(`^${prefix}-\\d+\\.json$`).test(artifactName)) fail('IDENTITY_INVALID');
      if (!this._exists(artifactName)) fail('IDENTITY_INVALID');
      const resolved = this._identityFromArtifact(prefix, artifactName);
      if (Object.prototype.hasOwnProperty.call(supplied, 'path')
        || Object.prototype.hasOwnProperty.call(supplied, 'size')
        || Object.prototype.hasOwnProperty.call(supplied, 'sha256')) {
        const candidate = validateIdentity({
          path: supplied.path,
          size: supplied.size,
          sha256: supplied.sha256,
          ...(supplied.kind === undefined ? {} : { kind: supplied.kind }),
        });
        const candidatePath = toAbsolutePath(candidate.path, 'IDENTITY_INVALID');
        if (candidatePath !== resolved.identity.path
          || candidate.size !== resolved.identity.size
          || candidate.sha256 !== resolved.identity.sha256) fail('IDENTITY_INVALID');
      }
      return resolved;
    }
    const candidate = validateIdentity(supplied);
    const candidatePath = toAbsolutePath(candidate.path, 'IDENTITY_INVALID');
    const fresh = readExternalIdentity(candidatePath, this._maxInputBytes);
    if (candidate.size !== fresh.size || candidate.sha256 !== fresh.sha256) fail('IDENTITY_INVALID');
    const ref = this._writeUnique(prefix, fresh);
    return { identity: fresh, ref };
  }

  _resolveAudit(supplied) {
    if (supplied === undefined || supplied === null) {
      const latest = this._latestArtifact('audit');
      if (!latest) fail('FINAL_AUDIT_REQUIRED');
      const record = this._artifactRecord(latest);
      return { audit: record.value, ref: record.ref };
    }
    if (typeof supplied === 'string') {
      const name = safeName(supplied);
      if (!/^audit-\d+\.json$/.test(name)) fail('FINAL_AUDIT_REQUIRED');
      const record = this._artifactRecord(name);
      return { audit: record.value, ref: record.ref };
    }
    if (!isPlainObject(supplied)) fail('FINAL_AUDIT_REQUIRED');
    const artifactName = supplied.artifactPath ?? supplied.relativePath ?? supplied.artifact ?? supplied.name;
    if (artifactName !== undefined) {
      if (typeof artifactName !== 'string' || !/^audit-\d+\.json$/.test(artifactName)) fail('FINAL_AUDIT_REQUIRED');
      if (!this._exists(artifactName)) fail('FINAL_AUDIT_REQUIRED');
      const record = this._artifactRecord(artifactName);
      return { audit: record.value, ref: record.ref };
    }
    validateFinalAudit(supplied);
    const ref = this.recordAudit(supplied, { final: true });
    return { audit: this.readArtifact(ref.name), ref };
  }

  _verifyFinalAudit(audit) {
    validateFinalAudit(audit);
  }

  _verifiedResumeIdentity() {
    if (!this._exists(RUN_NAME)) fail('IDENTITY_REQUIRED');
    const metadata = normalizeRunMetadata(this._artifactRecord(RUN_NAME).value);
    const configured = readExternalIdentity(metadata.resume_upload_path, this._maxInputBytes);
    if (configured.sha256 !== metadata.resume_upload_sha256) fail('IDENTITY_INVALID');
    return configured;
  }

  _verifyResumeBinding(upload) {
    const configured = this._verifiedResumeIdentity();
    if (!sameIdentityFields(configured, upload.identity)) fail('IDENTITY_INVALID');
  }

  _validateAllJsonArtifacts() {
    for (const name of this.listArtifacts()) {
      if (name === JOURNAL_NAME || name === COMPLETION_NAME) continue;
      if (name.endsWith('.json')) this.readArtifact(name);
    }
    this._readJournal();
  }
  _validateCompletionIdentity(prefix, reference) {
    const record = this._artifactRecord(reference.artifact);
    const identity = validateIdentity(record.value);
    if (identity.kind !== undefined && identity.kind !== prefix) fail('IDENTITY_INVALID');
    const fresh = readExternalIdentity(identity.path, this._maxInputBytes);
    if (!sameIdentityFields(identity, fresh)
      || fresh.path !== reference.path
      || fresh.size !== reference.size
      || fresh.sha256 !== reference.sha256) {
      fail('IDENTITY_INVALID');
    }
    return { identity: fresh, ref: record.ref };
  }

  _readValidatedCompletion() {
    const bytes = this._rawArtifact(COMPLETION_NAME);
    const report = validateCompletionReportShape(
      canonicalParse(bytes, this._maxJsonBytes, false),
    );
    this._validateAllJsonArtifacts();

    const auditRecord = this._artifactRecord(report.final_audit.artifact);
    if (auditRecord.ref.sha256 !== report.final_audit.sha256) fail('ARTIFACT_CORRUPT');
    validateFinalAudit(auditRecord.value);

    this._validateCompletionIdentity('screenshot', report.screenshot);
    const upload = this._validateCompletionIdentity('upload', report.upload);
    const configuredUpload = this._verifiedResumeIdentity();
    if (!sameIdentityFields(configuredUpload, upload.identity)) fail('IDENTITY_INVALID');

    const journalBytes = this._rawArtifact(JOURNAL_NAME, {
      maxBytes: this._maxJsonBytes * 16,
      journal: true,
    });
    const journalDigest = crypto.createHash('sha256').update(journalBytes).digest('hex');
    if (journalDigest !== report.action_journal.sha256) fail('ARTIFACT_CORRUPT');
    const journal = this._readJournal();
    if (journal.length !== report.action_journal.entries) fail('ARTIFACT_CORRUPT');
    const submit = validateFinalSubmitJournal(journal);
    if (submit.begins.length !== report.submit_action_count) fail('SUBMISSION_EVIDENCE');
    const runMetadata = normalizeRunMetadata(this._artifactRecord(RUN_NAME).value);

    return deepFreeze({
      report,
      runMetadata,
      applicationUrl: runMetadata.application_url,
      submitActionCount: report.submit_action_count,
      actionSummary: submit.actionSummary,
    });
  }


  _ensureEmptyJournal() {
    if (this._exists(JOURNAL_NAME)) return;
    this._atomicPublish(JOURNAL_NAME, Buffer.alloc(0));
  }

  _artifactDigest(name, maxBytes = this._maxJsonBytes * 16) {
    const bytes = this._rawArtifact(name, { maxBytes, journal: name === JOURNAL_NAME });
    return { path: name, relativePath: name, artifactPath: name, bytes: bytes.length, size: bytes.length, sha256: crypto.createHash('sha256').update(bytes).digest('hex') };
  }

  finalize(options = {}) {
    this._ensureMutable();
    if (!isPlainObject(options)) fail('FINALIZATION_FAILED');
    const hasSubmitCount = Object.prototype.hasOwnProperty.call(options, 'submitActionCount') || Object.prototype.hasOwnProperty.call(options, 'submit_action_count');
    if (!hasSubmitCount) fail('SUBMIT_COUNT_REQUIRED');
    const submitActionCount = options.submitActionCount ?? options.submit_action_count;
    if (!Number.isSafeInteger(submitActionCount) || submitActionCount < 0) fail('SUBMISSION_EVIDENCE');

    const journal = this._readJournal();
    const submit = validateFinalSubmitJournal(journal);
    if (submit.begins.length !== submitActionCount) fail('SUBMISSION_EVIDENCE');

    const audit = this._resolveAudit(options.audit ?? options.finalAudit ?? options.final_audit);
    this._verifyFinalAudit(audit.audit);
    const screenshot = this._resolveIdentity('screenshot', options.screenshotPath ?? options.screenshot ?? options.screenshotIdentity ?? options.screenshot_identity);
    const upload = this._resolveIdentity('upload', options.uploadPath ?? options.upload ?? options.uploadIdentity ?? options.upload_identity);
    this._verifyResumeBinding(upload);
    this._validateAllJsonArtifacts();
    this._ensureEmptyJournal();
    const journalMeta = this._artifactDigest(JOURNAL_NAME);
    const report = {
      schema_version: EVIDENCE_SCHEMA_VERSION,
      finalized_at: new Date().toISOString(),
      final_audit: { artifact: audit.ref.name, sha256: audit.ref.sha256 },
      screenshot: { artifact: screenshot.ref.name, path: screenshot.identity.path, size: screenshot.identity.size, sha256: screenshot.identity.sha256 },
      upload: { artifact: upload.ref.name, path: upload.identity.path, size: upload.identity.size, sha256: upload.identity.sha256 },
      action_journal: { artifact: JOURNAL_NAME, entries: journal.length, sha256: journalMeta.sha256 },
      submit_action_count: submitActionCount,
    };
    const reportRef = this._writeJson(COMPLETION_NAME, report, { allowCompletion: true });
    return { ...reportRef, report, path: COMPLETION_NAME, relativePath: COMPLETION_NAME, artifactPath: COMPLETION_NAME };
  }

  finalizeCompletion(options = {}) {
    return this.finalize(options);
  }

  complete(options = {}) {
    return this.finalize(options);
  }

  readCompletionReport() {
    return this.readArtifact(COMPLETION_NAME);
  }

  completionReport() {
    return this.readCompletionReport();
  }

  close() {
    if (this._closed) return;
    this._closed = true;
    if (this._rootFd !== null) {
      try {
        fs.closeSync(this._rootFd);
      } catch {
        // close is idempotent and does not expose filesystem values
      }
      this._rootFd = null;
    }
  }

  [Symbol.dispose]() {
    this.close();
  }
}

function existingEvidenceRoot(value) {
  const rootPath = normalizeRootPath(value);
  let stat;
  try {
    stat = fs.lstatSync(rootPath);
  } catch (error) {
    if (error?.code === 'ENOENT') fail('ARTIFACT_MISSING');
    fail('INVALID_ROOT');
  }
  if (stat.isSymbolicLink() || !stat.isDirectory()) fail('INVALID_ROOT');
  if (!isCurrentOwner(stat) || modeBits(stat) & 0o077) fail('ROOT_NOT_PRIVATE');
  return rootPath;
}

export function validateCompletionEvidence(evidenceDir) {
  const rootPath = existingEvidenceRoot(evidenceDir);
  const store = new EvidenceStore(rootPath);
  try {
    return store._readValidatedCompletion();
  } finally {
    store.close();
  }
}

/**
 * Async integration surface used by the Phase 1 coordinator. The underlying
 * store is synchronous so each mutation is serialized and publication is
 * complete when its Promise resolves.
 */
export async function createEvidenceStore(root, runMetadata, options = {}) {
  const store = new EvidenceStore(root, options);
  try {
    if (runMetadata !== undefined && runMetadata !== null) store.recordRunMetadata(runMetadata);
    return {
      root: store.root,
      store,
      recordObservation: async (value) => store.recordObservation(value),
      recordRunMetadata: async (value) => store.recordRunMetadata(value),
      recordRun: async (value) => store.recordRun(value),
      recordDiff: async (value) => store.recordDiff(value),
      recordAction: async (value) => store.recordAction(value),
      recordRetry: async (value) => store.recordRetry(value),
      recordLedger: async (value) => store.recordLedger(value),
      recordAudit: async (value, optionsForAudit) => store.recordAudit(value, optionsForAudit),
      recordFileIdentity: async (...args) => store.recordFileIdentity(...args),
      hashFile: async (value) => store.hashFile(value),
      sha256File: async (value) => store.sha256File(value),
      recordScreenshot: async (value) => store.recordScreenshot(value),
      recordUpload: async (value) => store.recordUpload(value),
      appendAction: async (value) => store.appendAction(value),
      finalize: async (value) => store.finalize(value),
      readArtifact: async (value) => store.readArtifact(value),
      readActionJournal: async () => store.readActionJournal(),
      readCompletionReport: async () => store.readCompletionReport(),
      close: async () => store.close(),
    };
  } catch (error) {
    store.close();
    throw error;
  }
}

export default EvidenceStore;
