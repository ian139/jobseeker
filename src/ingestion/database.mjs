import crypto from 'node:crypto';
import * as fs from 'node:fs';
import { constants as fsConstants, promises as fsp } from 'node:fs';
import path from 'node:path';
import { DatabaseSync } from 'node:sqlite';
import {
  ATS_KINDS,
  AVAILABILITY_STATES,
  DEDUPE_IDENTITY_KINDS,
  ELIGIBILITY_STATES,
  FRESHNESS_STATES,
  IngestionValidationError,
  WORKPLACE_TYPES,
  canonicalizeJobUrl,
  canonicalJson,
  classifyAts,
  classifyEligibility,
  deriveDedupeIdentity,
  sha256Canonical,
  sha256Text,
  validateNormalizedJob,
} from './contracts.mjs';
export const INGESTION_MIGRATION_VERSION = 5;
export const INGESTION_MIGRATION_NAME = '005-unified-ingestion';

const SHA256_RE = /^[0-9a-f]{64}$/u;
const ISO_RE = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$/u;
const SOURCE_RE = /^[a-z][a-z0-9_-]{0,63}$/u;
const PROFILE_RE = /^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$/u;
const SAFE_REASON_RE = /^[a-z][a-z0-9_]{0,63}$/u;
const PRIVATE_ROOT_MODE = 0o700;
const PRIVATE_FILE_MODE = 0o600;
const NOFOLLOW = fsConstants.O_NOFOLLOW ?? 0;
const READ_ONLY = fsConstants.O_RDONLY;
const WRITE_ONLY = fsConstants.O_WRONLY;
const CREATE = fsConstants.O_CREAT;
const EXCLUSIVE = fsConstants.O_EXCL;
const TRUNCATE = fsConstants.O_TRUNC;
const MAX_COUNT = 2 ** 31 - 1;

const JOB_COLUMNS = Object.freeze([
  'id',
  'source',
  'source_job_id',
  'canonical_listing_url',
  'canonical_application_url',
  'ats_kind',
  'ats_identifier',
  'title',
  'company',
  'location',
  'workplace_type',
  'employment_types_json',
  'description',
  'description_sha256',
  'source_posted_at',
  'source_updated_at',
  'discovered_at',
  'first_seen_at',
  'last_seen_at',
  'availability_state',
  'freshness_state',
  'eligibility_state',
  'eligibility_reason_codes_json',
  'priority',
  'dedupe_group_id',
  'raw_payload_path',
  'raw_payload_sha256',
]);

const DEDUPE_COLUMNS = Object.freeze(['id', 'identity_kind', 'identity_key', 'review_required', 'created_at', 'updated_at']);
const OBSERVATION_COLUMNS = Object.freeze(['id', 'sync_run_id', 'job_id', 'source', 'source_job_id', 'observed_at', 'raw_payload_path', 'raw_payload_sha256', 'normalized_job_sha256']);
const CHECKPOINT_COLUMNS = Object.freeze(['source', 'profile', 'checkpoint', 'last_sync_run_id', 'updated_at', 'revision']);
const SYNC_RUN_COLUMNS = Object.freeze([
  'id',
  'source',
  'profile',
  'mode',
  'state',
  'started_at',
  'finished_at',
  'window_end_at',
  'checkpoint_before',
  'checkpoint_after',
  'artifact_dir',
  'request_count',
  'pages_fetched',
  'jobs_seen',
  'jobs_inserted',
  'jobs_updated',
  'jobs_unchanged',
  'dedupe_groups_touched',
  'queue_rows_inserted',
  'estimated_credits',
  'reported_credits',
  'pending_page',
  'pending_request_sha256',
  'pending_started_at',
  'next_page',
  'expected_total_results',
  'failure_class',
  'reason_code',
  'result_sha256',
]);
const PAGE_COLUMNS = Object.freeze(['sync_run_id', 'page_number', 'request_sha256', 'response_path', 'response_sha256', 'received_at', 'item_count', 'total_results', 'estimated_credits']);
const MIGRATION_COLUMNS = Object.freeze(['version', 'name', 'sha256', 'applied_at']);

export const FINAL_SCHEMA_SQL = `
PRAGMA foreign_keys = OFF;

CREATE TABLE IF NOT EXISTS dedupe_groups (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  identity_kind TEXT NOT NULL CHECK (identity_kind IN ('ats', 'application_url', 'review_fingerprint')),
  identity_key TEXT NOT NULL CHECK (length(identity_key) BETWEEN 1 AND 1024),
  review_required INTEGER NOT NULL CHECK (review_required IN (0, 1)),
  created_at TEXT NOT NULL CHECK (created_at GLOB '????-??-??T??:??:??.???Z'),
  updated_at TEXT NOT NULL CHECK (updated_at GLOB '????-??-??T??:??:??.???Z'),
  UNIQUE(identity_kind, identity_key)
);

CREATE TABLE IF NOT EXISTS sync_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  source TEXT NOT NULL CHECK (source GLOB '[a-z]*' AND length(source) BETWEEN 1 AND 64),
  profile TEXT NOT NULL CHECK (length(profile) BETWEEN 1 AND 128),
  mode TEXT NOT NULL CHECK (mode = 'paid'),
  state TEXT NOT NULL CHECK (state IN ('fetching', 'ready_to_commit', 'succeeded', 'failed', 'paid_ambiguous')),
  started_at TEXT NOT NULL CHECK (started_at GLOB '????-??-??T??:??:??.???Z'),
  finished_at TEXT CHECK (finished_at IS NULL OR finished_at GLOB '????-??-??T??:??:??.???Z'),
  window_end_at TEXT NOT NULL CHECK (window_end_at GLOB '????-??-??T??:??:??.???Z'),
  checkpoint_before TEXT,
  checkpoint_after TEXT,
  artifact_dir TEXT NOT NULL CHECK (length(artifact_dir) BETWEEN 1 AND 4096),
  request_count INTEGER NOT NULL DEFAULT 0 CHECK (request_count >= 0),
  pages_fetched INTEGER NOT NULL DEFAULT 0 CHECK (pages_fetched >= 0),
  jobs_seen INTEGER NOT NULL DEFAULT 0 CHECK (jobs_seen >= 0),
  jobs_inserted INTEGER NOT NULL DEFAULT 0 CHECK (jobs_inserted >= 0),
  jobs_updated INTEGER NOT NULL DEFAULT 0 CHECK (jobs_updated >= 0),
  jobs_unchanged INTEGER NOT NULL DEFAULT 0 CHECK (jobs_unchanged >= 0),
  dedupe_groups_touched INTEGER NOT NULL DEFAULT 0 CHECK (dedupe_groups_touched >= 0),
  queue_rows_inserted INTEGER NOT NULL DEFAULT 0 CHECK (queue_rows_inserted >= 0),
  estimated_credits REAL NOT NULL DEFAULT 0 CHECK (estimated_credits >= 0),
  reported_credits REAL CHECK (reported_credits IS NULL OR reported_credits >= 0),
  pending_page INTEGER CHECK (pending_page IS NULL OR pending_page >= 0),
  pending_request_sha256 TEXT CHECK (pending_request_sha256 IS NULL OR pending_request_sha256 GLOB '[0-9a-f]*' AND length(pending_request_sha256) = 64),
  pending_started_at TEXT CHECK (pending_started_at IS NULL OR pending_started_at GLOB '????-??-??T??:??:??.???Z'),
  next_page INTEGER NOT NULL DEFAULT 0 CHECK (next_page >= 0),
  expected_total_results INTEGER CHECK (expected_total_results IS NULL OR expected_total_results >= 0),
  failure_class TEXT CHECK (failure_class IS NULL OR failure_class IN ('retryable', 'terminal', 'paid_ambiguous', 'authentication', 'account_health')),
  reason_code TEXT CHECK (reason_code IS NULL OR (length(reason_code) BETWEEN 1 AND 256 AND reason_code GLOB '[a-z]*')),
  result_sha256 TEXT CHECK (result_sha256 IS NULL OR result_sha256 GLOB '[0-9a-f]*' AND length(result_sha256) = 64),
  CHECK ((pending_page IS NULL AND pending_request_sha256 IS NULL AND pending_started_at IS NULL) OR (pending_page IS NOT NULL AND pending_request_sha256 IS NOT NULL AND pending_started_at IS NOT NULL)),
  CHECK (state IN ('fetching', 'ready_to_commit') OR finished_at IS NOT NULL),
  CHECK (state NOT IN ('succeeded', 'paid_ambiguous') OR failure_class IS NULL),
  CHECK (state <> 'paid_ambiguous' OR failure_class = 'paid_ambiguous'),
  CHECK (state <> 'failed' OR (failure_class IS NOT NULL AND reason_code IS NOT NULL)),
  CHECK (state <> 'succeeded' OR result_sha256 IS NOT NULL)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_sync_runs_nonterminal ON sync_runs(source, profile) WHERE state IN ('fetching', 'ready_to_commit');

CREATE TABLE IF NOT EXISTS jobs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  source TEXT NOT NULL CHECK (source GLOB '[a-z]*' AND length(source) BETWEEN 1 AND 64),
  source_job_id TEXT,
  canonical_listing_url TEXT,
  canonical_application_url TEXT,
  ats_kind TEXT NOT NULL CHECK (ats_kind IN ('greenhouse', 'ashby', 'lever', 'workday', 'linkedin', 'custom', 'unknown')),
  ats_identifier TEXT,
  title TEXT NOT NULL CHECK (length(title) BETWEEN 1 AND 1024),
  company TEXT NOT NULL CHECK (length(company) BETWEEN 1 AND 1024),
  location TEXT,
  workplace_type TEXT NOT NULL CHECK (workplace_type IN ('remote', 'hybrid', 'onsite', 'unknown')),
  employment_types_json TEXT NOT NULL CHECK (json_valid(employment_types_json) AND json_type(employment_types_json) = 'array'),
  description TEXT NOT NULL,
  description_sha256 TEXT NOT NULL CHECK (description_sha256 GLOB '[0-9a-f]*' AND length(description_sha256) = 64),
  source_posted_at TEXT CHECK (source_posted_at IS NULL OR source_posted_at GLOB '????-??-??T??:??:??.???Z'),
  source_updated_at TEXT CHECK (source_updated_at IS NULL OR source_updated_at GLOB '????-??-??T??:??:??.???Z'),
  discovered_at TEXT NOT NULL CHECK (discovered_at GLOB '????-??-??T??:??:??.???Z'),
  first_seen_at TEXT NOT NULL CHECK (first_seen_at GLOB '????-??-??T??:??:??.???Z'),
  last_seen_at TEXT NOT NULL CHECK (last_seen_at GLOB '????-??-??T??:??:??.???Z'),
  availability_state TEXT NOT NULL CHECK (availability_state IN ('open', 'closed', 'unknown')),
  freshness_state TEXT NOT NULL CHECK (freshness_state IN ('current', 'stale', 'unverified')),
  eligibility_state TEXT NOT NULL CHECK (eligibility_state IN ('eligible', 'ineligible', 'review')),
  eligibility_reason_codes_json TEXT NOT NULL CHECK (json_valid(eligibility_reason_codes_json) AND json_type(eligibility_reason_codes_json) = 'array'),
  priority INTEGER NOT NULL CHECK (priority BETWEEN 0 AND 1000),
  dedupe_group_id INTEGER REFERENCES dedupe_groups(id) ON DELETE RESTRICT,
  raw_payload_path TEXT NOT NULL CHECK (length(raw_payload_path) BETWEEN 1 AND 4096),
  raw_payload_sha256 TEXT NOT NULL CHECK (raw_payload_sha256 GLOB '[0-9a-f]*' AND length(raw_payload_sha256) = 64),
  CHECK (source_job_id IS NOT NULL OR canonical_application_url IS NOT NULL),
  UNIQUE (source, source_job_id)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_jobs_source_job_id ON jobs(source, source_job_id) WHERE source_job_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS idx_jobs_source_application_fallback ON jobs(source, canonical_application_url) WHERE source_job_id IS NULL AND canonical_application_url IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_jobs_dedupe_group ON jobs(dedupe_group_id);
CREATE INDEX IF NOT EXISTS idx_jobs_eligibility_priority ON jobs(eligibility_state, priority, last_seen_at);

CREATE TABLE IF NOT EXISTS source_checkpoints (
  source TEXT NOT NULL CHECK (source GLOB '[a-z]*' AND length(source) BETWEEN 1 AND 64),
  profile TEXT NOT NULL CHECK (length(profile) BETWEEN 1 AND 128),
  checkpoint TEXT,
  last_sync_run_id INTEGER REFERENCES sync_runs(id) ON DELETE RESTRICT,
  updated_at TEXT NOT NULL CHECK (updated_at GLOB '????-??-??T??:??:??.???Z'),
  revision INTEGER NOT NULL DEFAULT 0 CHECK (revision >= 0),
  PRIMARY KEY (source, profile)
);

CREATE TABLE IF NOT EXISTS source_observations (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  sync_run_id INTEGER REFERENCES sync_runs(id) ON DELETE RESTRICT,
  job_id INTEGER NOT NULL REFERENCES jobs(id) ON DELETE RESTRICT,
  source TEXT NOT NULL CHECK (source GLOB '[a-z]*' AND length(source) BETWEEN 1 AND 64),
  source_job_id TEXT,
  observed_at TEXT NOT NULL CHECK (observed_at GLOB '????-??-??T??:??:??.???Z'),
  raw_payload_path TEXT NOT NULL CHECK (length(raw_payload_path) BETWEEN 1 AND 4096),
  raw_payload_sha256 TEXT NOT NULL CHECK (raw_payload_sha256 GLOB '[0-9a-f]*' AND length(raw_payload_sha256) = 64),
  normalized_job_sha256 TEXT NOT NULL CHECK (normalized_job_sha256 GLOB '[0-9a-f]*' AND length(normalized_job_sha256) = 64),
  UNIQUE(sync_run_id, job_id, raw_payload_sha256)
);

CREATE TABLE IF NOT EXISTS source_sync_pages (
  sync_run_id INTEGER NOT NULL REFERENCES sync_runs(id) ON DELETE RESTRICT,
  page_number INTEGER NOT NULL CHECK (page_number >= 0),
  request_sha256 TEXT NOT NULL CHECK (request_sha256 GLOB '[0-9a-f]*' AND length(request_sha256) = 64),
  response_path TEXT NOT NULL CHECK (length(response_path) BETWEEN 1 AND 4096),
  response_sha256 TEXT NOT NULL CHECK (response_sha256 GLOB '[0-9a-f]*' AND length(response_sha256) = 64),
  received_at TEXT NOT NULL CHECK (received_at GLOB '????-??-??T??:??:??.???Z'),
  item_count INTEGER NOT NULL CHECK (item_count >= 0),
  total_results INTEGER CHECK (total_results IS NULL OR total_results >= 0),
  estimated_credits REAL NOT NULL CHECK (estimated_credits >= 0),
  PRIMARY KEY (sync_run_id, page_number)
);
CREATE TABLE IF NOT EXISTS application_jobs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  source_table TEXT NOT NULL,
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
  dedupe_group_id INTEGER REFERENCES dedupe_groups(id) ON DELETE RESTRICT,
  UNIQUE(source_table, source_db, source_rowid),
  UNIQUE(application_url)
);

CREATE TABLE IF NOT EXISTS application_runs (
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
  active INTEGER NOT NULL DEFAULT 0 CHECK (active IN (0,1)),
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

CREATE TABLE IF NOT EXISTS source_checkpoints (
  source TEXT NOT NULL CHECK (source GLOB '[a-z]*' AND length(source) BETWEEN 1 AND 64),
  profile TEXT NOT NULL CHECK (length(profile) BETWEEN 1 AND 128),
  checkpoint TEXT,
  last_sync_run_id INTEGER REFERENCES sync_runs(id) ON DELETE RESTRICT,
  updated_at TEXT NOT NULL CHECK (updated_at GLOB '????-??-??T??:??:??.???Z'),
  revision INTEGER NOT NULL DEFAULT 0 CHECK (revision >= 0),
  PRIMARY KEY (source, profile)
);
CREATE TABLE IF NOT EXISTS schema_migrations (
  version INTEGER PRIMARY KEY,
  name TEXT NOT NULL CHECK (length(name) BETWEEN 1 AND 256),
  sha256 TEXT NOT NULL CHECK (sha256 GLOB '[0-9a-f]*' AND length(sha256) = 64),
  applied_at TEXT NOT NULL CHECK (applied_at GLOB '????-??-??T??:??:??.???Z')
);

CREATE TRIGGER IF NOT EXISTS source_observations_immutable_update BEFORE UPDATE ON source_observations BEGIN SELECT RAISE(ABORT, 'immutable source observation'); END;
CREATE TRIGGER IF NOT EXISTS source_observations_immutable_delete BEFORE DELETE ON source_observations BEGIN SELECT RAISE(ABORT, 'immutable source observation'); END;
CREATE TRIGGER IF NOT EXISTS source_sync_pages_immutable_update BEFORE UPDATE ON source_sync_pages BEGIN SELECT RAISE(ABORT, 'immutable source sync page'); END;
CREATE TRIGGER IF NOT EXISTS source_sync_pages_immutable_delete BEFORE DELETE ON source_sync_pages BEGIN SELECT RAISE(ABORT, 'immutable source sync page'); END;

PRAGMA foreign_keys = ON;
`;
const FINAL_APPLICATION_INDEX_SQL = `
CREATE INDEX IF NOT EXISTS idx_application_jobs_status_id ON application_jobs(status, id);
CREATE INDEX IF NOT EXISTS idx_application_jobs_dedupe_group ON application_jobs(dedupe_group_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_application_jobs_dedupe_group_unique ON application_jobs(dedupe_group_id) WHERE dedupe_group_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_application_runs_job_id ON application_runs(job_id);
CREATE INDEX IF NOT EXISTS idx_application_runs_status ON application_runs(status);
CREATE INDEX IF NOT EXISTS idx_application_runs_active_status ON application_runs(active, status);
CREATE UNIQUE INDEX IF NOT EXISTS idx_application_runs_active_job ON application_runs(job_id) WHERE active = 1;
CREATE UNIQUE INDEX IF NOT EXISTS idx_application_runs_active_owner ON application_runs(owner_id) WHERE active = 1;
CREATE UNIQUE INDEX IF NOT EXISTS idx_application_runs_active_global ON application_runs((1)) WHERE active = 1;
`;

const LEGACY_JOB_COLUMNS = Object.freeze(['id', 'source', 'source_job_id', 'canonical_url', 'title', 'company', 'location', 'remote', 'posted_at', 'discovered_at', 'description', 'status', 'raw_json', 'first_seen_at', 'last_seen_at']);
const LEGACY_SYNC_COLUMNS = Object.freeze(['id', 'source', 'profile', 'mode', 'started_at', 'finished_at', 'checkpoint', 'success', 'jobs_seen', 'jobs_returned', 'jobs_inserted', 'jobs_updated', 'error']);
const APPLICATION_JOB_COLUMNS = Object.freeze(['id', 'source_table', 'source_db', 'source_rowid', 'source_job_id', 'application_url', 'eligibility_tier', 'verification_reason', 'source_posted_at', 'source_last_seen_at', 'status', 'status_reason', 'claimed_at', 'completed_at']);
const APPLICATION_JOB_COLUMNS_WITH_DEDUPE = Object.freeze([...APPLICATION_JOB_COLUMNS, 'dedupe_group_id']);
const APPLICATION_RUN_COLUMNS = Object.freeze(['id', 'job_id', 'status', 'reason_code', 'started_at', 'finished_at', 'final_url', 'actions_json', 'evidence_path', 'submit_action_count', 'active', 'owner_id', 'browser_session_id', 'claimed_at', 'lease_expires_at', 'last_progress_at', 'workspace_path', 'resume_artifact_path', 'resume_artifact_sha256', 'answer_memory_path', 'blocker_alias']);

function nowIso(value = new Date()) {
  if (value instanceof Date) {
    if (!Number.isFinite(value.getTime())) throw new Error('invalid now');
    return value.toISOString();
  }
  if (typeof value !== 'string' || !ISO_RE.test(value) || new Date(value).toISOString() !== value) throw new Error('invalid now');
  return value;
}

function digestBytes(value) {
  return crypto.createHash('sha256').update(value).digest('hex');
}

function tableColumns(db, table) {
  return db.prepare(`PRAGMA table_info(${quoteIdentifier(table)})`).all().map((row) => row.name);
}

function quoteIdentifier(value) {
  if (!/^[A-Za-z_][A-Za-z0-9_]*$/u.test(value)) throw new Error('unsafe SQL identifier');
  return `"${value}"`;
}

function tableExists(db, table) {
  return db.prepare("SELECT 1 AS present FROM sqlite_master WHERE type = 'table' AND name = ?").get(table) !== undefined;
}

function triggerExists(db, trigger) {
  return db.prepare("SELECT 1 AS present FROM sqlite_master WHERE type = 'trigger' AND name = ?").get(trigger) !== undefined;
}

function assertColumns(db, table, expected, { allowExtra = false } = {}) {
  if (!tableExists(db, table)) throw new Error(`E_SCHEMA_MISSING_TABLE:${table}`);
  const actual = tableColumns(db, table);
  if ((!allowExtra && actual.length !== expected.length) || expected.some((name) => !actual.includes(name))) throw new Error(`E_SCHEMA_COLUMNS:${table}`);
}

function migrationIdentity(db) {
  return db.prepare('SELECT version, name, sha256, applied_at FROM schema_migrations WHERE version = ?').get(INGESTION_MIGRATION_VERSION);
}

function migrationDigest() {
  return digestBytes(FINAL_SCHEMA_SQL);
}

export function assertIngestionSchema(database) {
  const owned = typeof database === 'string' ? new DatabaseSync(database) : null;
  const db = owned ?? database;
  try {
    for (const [table, columns] of [['jobs', JOB_COLUMNS], ['dedupe_groups', DEDUPE_COLUMNS], ['source_observations', OBSERVATION_COLUMNS], ['source_checkpoints', CHECKPOINT_COLUMNS], ['sync_runs', SYNC_RUN_COLUMNS], ['source_sync_pages', PAGE_COLUMNS], ['schema_migrations', MIGRATION_COLUMNS]]) assertColumns(db, table, columns);
    assertColumns(db, 'application_jobs', APPLICATION_JOB_COLUMNS_WITH_DEDUPE);
    assertColumns(db, 'application_runs', APPLICATION_RUN_COLUMNS);
    const forbidden = db.prepare("SELECT name FROM pragma_table_info('jobs') WHERE name IN ('raw_json', 'status')").all();
    if (forbidden.length > 0) throw new Error('E_SCHEMA_OBSOLETE_COLUMN:jobs');
    for (const trigger of ['source_observations_immutable_update', 'source_observations_immutable_delete', 'source_sync_pages_immutable_update', 'source_sync_pages_immutable_delete']) if (!triggerExists(db, trigger)) throw new Error(`E_SCHEMA_TRIGGER:${trigger}`);
    return true;
  } finally {
    if (owned) owned.close();
  }
}

function assertLegacySchema(db) {
  assertColumns(db, 'jobs', LEGACY_JOB_COLUMNS);
  assertColumns(db, 'sync_runs', LEGACY_SYNC_COLUMNS);
  assertColumns(db, 'application_jobs', APPLICATION_JOB_COLUMNS, { allowExtra: false });
  assertColumns(db, 'application_runs', APPLICATION_RUN_COLUMNS, { allowExtra: false });
  if (tableExists(db, 'schema_migrations')) {
    const rows = db.prepare('SELECT version, name, sha256 FROM schema_migrations').all();
    if (rows.some((row) => Number(row.version) >= INGESTION_MIGRATION_VERSION)) throw new Error('E_SCHEMA_MIGRATION_IDENTITY');
  }
}

function assertCanonicalTimestamp(value, location) {
  if (typeof value !== 'string' || !ISO_RE.test(value) || new Date(value).toISOString() !== value) throw new Error(`E_LEGACY_TIMESTAMP:${location}`);
  return value;
}

function normalizeLegacyTimestamp(value, location, { nullable = true } = {}) {
  if (value === null || value === undefined || value === '') return nullable ? null : (() => { throw new Error(`E_LEGACY_TIMESTAMP:${location}`); })();
  const parsed = Date.parse(String(value));
  if (!Number.isFinite(parsed)) throw new Error(`E_LEGACY_TIMESTAMP:${location}`);
  return new Date(parsed).toISOString();
}

function safeSource(value) {
  if (typeof value !== 'string' || !SOURCE_RE.test(value)) throw new Error('E_LEGACY_SOURCE');
  return value;
}

function safeProfile(value) {
  if (typeof value !== 'string' || !PROFILE_RE.test(value)) throw new Error('E_LEGACY_PROFILE');
  return value;
}

function legacyRawField(raw, ...names) {
  for (const name of names) {
    const value = raw?.[name];
    if (value !== undefined && value !== null && value !== '') return value;
  }
  return null;
}

function normalizeEmploymentTypes(raw) {
  const value = legacyRawField(raw, 'employment_types', 'employmentStatuses', 'employment_statuses', 'employment_type', 'job_type');
  const values = Array.isArray(value) ? value : value === null ? [] : [value];
  return [...new Set(values.filter((entry) => typeof entry === 'string').map((entry) => entry.normalize('NFKC').trim().toLowerCase().replace(/\s+/gu, '_')).filter((entry) => /^[a-z][a-z0-9_-]{0,63}$/u.test(entry)))].sort();
}

function normalizeWorkplaceType(row, raw) {
  const explicit = legacyRawField(raw, 'workplace_type', 'workplaceType', 'remote_type');
  const candidate = typeof explicit === 'string' ? explicit.toLowerCase() : '';
  if (candidate.includes('hybrid')) return 'hybrid';
  if (candidate.includes('remote')) return 'remote';
  if (candidate.includes('on_site') || candidate.includes('onsite') || candidate.includes('on-site')) return 'onsite';
  if (row.remote === 1 || row.remote === true) return 'remote';
  if (row.remote === 0 || row.remote === false) return 'onsite';
  return 'unknown';
}

function normalizeLegacyJob(row, rawPayloadPath, rawPayloadSha256, observedAt, now) {
  if (!row || typeof row !== 'object') throw new Error('E_LEGACY_JOB');
  const source = safeSource(row.source);
  const sourceJobId = row.source_job_id === null || row.source_job_id === undefined ? null : String(row.source_job_id);
  const raw = rawPayloadValue(row.raw_json);
  const listingCandidate = row.canonical_url;
  const applicationCandidate = legacyRawField(raw, 'application_url', 'apply_url', 'final_url', 'url') ?? listingCandidate;
  const canonicalListingUrl = listingCandidate === null || listingCandidate === undefined || listingCandidate === '' ? null : canonicalizeJobUrl(String(listingCandidate));
  const canonicalApplicationUrl = applicationCandidate === null || applicationCandidate === undefined || applicationCandidate === '' ? null : canonicalizeJobUrl(String(applicationCandidate));
  const ats = classifyAts(canonicalApplicationUrl);
  const title = normalizedLegacyText(row.title, 'title', 1024);
  const company = normalizedLegacyText(row.company, 'company', 1024);
  const location = row.location === null || row.location === undefined || row.location === '' ? null : normalizedLegacyText(row.location, 'location', 2048);
  const descriptionValue = row.description === null || row.description === undefined ? '' : String(row.description).replaceAll('\r\n', '\n').replaceAll('\r', '\n');
  if (descriptionValue.length > 1_000_000 || descriptionValue.includes('\u0000')) throw new Error('E_LEGACY_DESCRIPTION');
  const discoveredAt = normalizeLegacyTimestamp(row.discovered_at, 'discovered_at', { nullable: false });
  const firstSeenAt = normalizeLegacyTimestamp(row.first_seen_at, 'first_seen_at', { nullable: false });
  const lastSeenAt = normalizeLegacyTimestamp(row.last_seen_at, 'last_seen_at', { nullable: false });
  const sourcePostedAt = normalizeOptionalLegacyDate(row.posted_at, 'posted_at');
  const sourceUpdatedAt = normalizeOptionalLegacyDate(legacyRawField(raw, 'date_updated', 'date_reposted', 'updated_at') ?? row.last_seen_at, 'source_updated_at');
  const availabilityState = row.status === 'archived' || legacyRawField(raw, 'closed_at', 'close_date') ? 'closed' : 'open';
  const ageDays = Math.max(0, (Date.parse(now) - Date.parse(lastSeenAt)) / 86_400_000);
  const freshnessState = ageDays > 30 ? 'stale' : 'current';
  const base = {
    schema: 'normalized-job-v1',
    source,
    sourceJobId,
    canonicalListingUrl,
    canonicalApplicationUrl,
    atsKind: ats.kind,
    atsIdentifier: ats.identifier,
    title,
    company,
    location,
    workplaceType: normalizeWorkplaceType(row, raw),
    employmentTypes: normalizeEmploymentTypes(raw),
    description: descriptionValue,
    descriptionSha256: sha256Text(descriptionValue),
    sourcePostedAt,
    sourceUpdatedAt,
    discoveredAt,
    availabilityState,
    freshnessState,
    eligibilityState: 'review',
    eligibilityReasonCodes: [],
    priority: 0,
    dedupeIdentityKind: 'application_url',
    dedupeIdentityKey: canonicalApplicationUrl ?? `${source}:${sourceJobId ?? row.id}`,
    dedupeReviewRequired: false,
    rawPayloadPath,
    rawPayloadSha256,
  };
  const identity = deriveDedupeIdentity(base);
  const eligibility = classifyEligibility(base);
  const normalized = {
    ...base,
    eligibilityState: eligibility.eligibilityState,
    eligibilityReasonCodes: [...eligibility.eligibilityReasonCodes],
    priority: eligibility.priority,
    dedupeIdentityKind: identity.kind,
    dedupeIdentityKey: identity.key,
    dedupeReviewRequired: identity.reviewRequired,
  };
  return validateNormalizedJob(normalized);
}

function normalizedLegacyText(value, location, max) {
  if (typeof value !== 'string') throw new Error(`E_LEGACY_TEXT:${location}`);
  const normalized = value.replaceAll('\r\n', '\n').replaceAll('\r', '\n').trim();
  if (!normalized || normalized.length > max || normalized.includes('\u0000')) throw new Error(`E_LEGACY_TEXT:${location}`);
  return normalized;
}

function normalizeOptionalLegacyDate(value, location) {
  if (value === null || value === undefined || value === '') return null;
  return normalizeLegacyTimestamp(value, location);
}

function rawPayloadValue(value) {
  if (typeof value !== 'string' || value.length > 4 * 1024 * 1024 || value.includes('\u0000')) throw new Error('E_LEGACY_RAW_JSON');
  let parsed;
  try {
    parsed = JSON.parse(value);
  } catch {
    throw new Error('E_LEGACY_RAW_JSON');
  }
  if (parsed === null || typeof parsed !== 'object' || Array.isArray(parsed)) throw new Error('E_LEGACY_RAW_JSON');
  return parsed;
}

function verifyPrivateRoot(root) {
  if (typeof root !== 'string' || !path.isAbsolute(root) || root.includes('\u0000') || root.includes('\\')) throw new Error('E_PAYLOAD_ROOT');
}

async function ensurePrivateDirectory(root) {
  verifyPrivateRoot(root);
  await fsp.mkdir(root, { recursive: true, mode: PRIVATE_ROOT_MODE });
  await fsp.chmod(root, PRIVATE_ROOT_MODE);
  const stat = await fsp.lstat(root);
  if (!stat.isDirectory() || (stat.mode & 0o777) !== PRIVATE_ROOT_MODE || (typeof process.getuid === 'function' && stat.uid !== process.getuid())) throw new Error('E_PAYLOAD_ROOT_PRIVATE');
}

async function writePrivatePayload(root, source, digest, rawText) {
  const sourceDir = path.join(root, source);
  await fsp.mkdir(sourceDir, { recursive: true, mode: PRIVATE_ROOT_MODE });
  await fsp.chmod(sourceDir, PRIVATE_ROOT_MODE);
  const directoryStat = await fsp.lstat(sourceDir);
  if (!directoryStat.isDirectory() || (directoryStat.mode & 0o777) !== PRIVATE_ROOT_MODE) throw new Error('E_PAYLOAD_DIRECTORY');
  const destination = path.join(sourceDir, `${digest}.json`);
  let created = false;
  try {
    const handle = await fsp.open(destination, WRITE_ONLY | CREATE | EXCLUSIVE | NOFOLLOW, PRIVATE_FILE_MODE);
    try {
      await handle.writeFile(rawText, 'utf8');
      await handle.sync();
    } finally {
      await handle.close();
    }
    created = true;
  } catch (error) {
    if (error?.code !== 'EEXIST') throw error;
  }
  const stat = await fsp.lstat(destination);
  if (!stat.isFile() || (stat.mode & 0o777) !== PRIVATE_FILE_MODE || (typeof process.getuid === 'function' && stat.uid !== process.getuid())) throw new Error('E_PAYLOAD_FILE_PRIVATE');
  const existing = await fsp.readFile(destination);
  if (digestBytes(existing) !== digest || existing.toString('utf8') !== rawText) throw new Error('E_PAYLOAD_DIGEST');
  return { path: destination, created };
}

function removeCreatedPayloads(paths) {
  return Promise.all(paths.map(async (value) => {
    try {
      await fsp.unlink(value);
    } catch (error) {
      if (error?.code !== 'ENOENT') throw error;
    }
  }));
}

function rawTextForRow(row) {
  if (typeof row.raw_json !== 'string') throw new Error('E_LEGACY_RAW_JSON');
  rawPayloadValue(row.raw_json);
  return row.raw_json;
}

function tableNames(db) {
  return db.prepare("SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name").all().map((row) => row.name);
}

function applicationJobTableSql() {
  return `
CREATE TABLE application_jobs_new (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  source_table TEXT NOT NULL,
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
  dedupe_group_id INTEGER REFERENCES dedupe_groups(id) ON DELETE RESTRICT,
  UNIQUE(source_table, source_db, source_rowid),
  UNIQUE(application_url)
);
`;
}

function copyApplicationJobs(db, dedupeByJobId) {
  const rows = db.prepare('SELECT * FROM application_jobs ORDER BY id').all();
  const insert = db.prepare(`INSERT INTO application_jobs_new (id,source_table,source_db,source_rowid,source_job_id,application_url,eligibility_tier,verification_reason,source_posted_at,source_last_seen_at,status,status_reason,claimed_at,completed_at,dedupe_group_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)`);
  const seenGroups = new Set();
  for (const row of rows) {
    const groupId = dedupeByJobId.get(Number(row.source_rowid)) ?? null;
    let assigned = groupId;
    if (assigned !== null && seenGroups.has(assigned)) assigned = null;
    if (assigned !== null) seenGroups.add(assigned);
    insert.run(row.id, row.source_table, row.source_db, row.source_rowid, row.source_job_id, row.application_url, row.eligibility_tier, row.verification_reason, row.source_posted_at, row.source_last_seen_at, row.status, row.status_reason, row.claimed_at, row.completed_at, assigned);
  }
}

function createFinalApplicationJobs(db) {
  db.exec(applicationJobTableSql());
  db.exec('DROP TABLE application_jobs; ALTER TABLE application_jobs_new RENAME TO application_jobs;');
  db.exec('CREATE INDEX idx_application_jobs_status_id ON application_jobs(status, id); CREATE INDEX idx_application_jobs_dedupe_group ON application_jobs(dedupe_group_id); CREATE UNIQUE INDEX idx_application_jobs_dedupe_group_unique ON application_jobs(dedupe_group_id) WHERE dedupe_group_id IS NOT NULL;');
}

function createLegacySyncRows(db, rows, payloadRoot, now) {
  const insert = db.prepare(`INSERT INTO sync_runs (id,source,profile,mode,state,started_at,finished_at,window_end_at,checkpoint_before,checkpoint_after,artifact_dir,request_count,pages_fetched,jobs_seen,jobs_inserted,jobs_updated,jobs_unchanged,dedupe_groups_touched,queue_rows_inserted,estimated_credits,reported_credits,pending_page,pending_request_sha256,pending_started_at,next_page,expected_total_results,failure_class,reason_code,result_sha256) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)`);
  const syncMap = new Map();
  for (const row of rows) {
    const source = safeSource(row.source);
    const profile = row.profile === null || row.profile === undefined || row.profile === '' ? 'legacy' : safeProfile(String(row.profile));
    const startedAt = normalizeLegacyTimestamp(row.started_at, 'sync.started_at', { nullable: false });
    const finishedAt = normalizeOptionalLegacyDate(row.finished_at, 'sync.finished_at') ?? now;
    const checkpoint = row.checkpoint === null || row.checkpoint === undefined || row.checkpoint === '' ? null : String(row.checkpoint);
    const failed = Number(row.success) !== 1;
    const artifactDir = path.join(payloadRoot, source, `sync-${row.id}`);
    const reasonCode = failed ? 'legacy_sync_failed' : null;
    const failureClass = failed ? 'terminal' : null;
    const jobsSeen = nonnegativeCount(row.jobs_seen, 'sync.jobs_seen');
    const jobsReturned = nonnegativeCount(row.jobs_returned, 'sync.jobs_returned');
    const jobsInserted = nonnegativeCount(row.jobs_inserted, 'sync.jobs_inserted');
    const jobsUpdated = nonnegativeCount(row.jobs_updated, 'sync.jobs_updated');
    const resultSha256 = failed ? null : digestBytes(JSON.stringify({ source, profile, checkpoint, jobsSeen, jobsReturned, jobsInserted, jobsUpdated }));
    insert.run(row.id, source, profile, 'paid', failed ? 'failed' : 'succeeded', startedAt, finishedAt, finishedAt, null, checkpoint, artifactDir, jobsReturned > 0 ? 1 : 0, jobsReturned > 0 ? 1 : 0, jobsSeen, jobsInserted, jobsUpdated, Math.max(0, jobsSeen - jobsInserted - jobsUpdated), 0, 0, 0, null, null, null, null, 0, null, failureClass, reasonCode, resultSha256);
    syncMap.set(source, row.id);
  }
  return syncMap;
}
function createLegacyCheckpoints(db, rows, now) {
  const latest = new Map();
  for (const row of rows) {
    const source = safeSource(row.source);
    const profile = row.profile === null || row.profile === undefined || row.profile === '' ? 'legacy' : safeProfile(String(row.profile));
    const checkpoint = row.checkpoint === null || row.checkpoint === undefined || row.checkpoint === '' ? null : String(row.checkpoint);
    const finishedAt = normalizeOptionalLegacyDate(row.finished_at, 'sync.finished_at') ?? now;
    const existing = latest.get(`${source}\u0000${profile}`);
    if (!existing || Number(row.id) > existing.id) latest.set(`${source}\u0000${profile}`, { id: Number(row.id), source, profile, checkpoint, finishedAt });
  }
  const insert = db.prepare('INSERT INTO source_checkpoints (source,profile,checkpoint,last_sync_run_id,updated_at,revision) VALUES (?,?,?,?,?,?)');
  for (const row of latest.values()) insert.run(row.source, row.profile, row.checkpoint, row.id, row.finishedAt, 1);
}

function nonnegativeCount(value, location) {
  const number = Number(value ?? 0);
  if (!Number.isInteger(number) || number < 0 || number > MAX_COUNT) throw new Error(`E_LEGACY_COUNT:${location}`);
  return number;
}

function migrateRows(db, jobs, syncMap, normalizedRows, now) {
  const insertGroup = db.prepare('INSERT INTO dedupe_groups (identity_kind,identity_key,review_required,created_at,updated_at) VALUES (?,?,?,?,?) ON CONFLICT(identity_kind,identity_key) DO UPDATE SET updated_at=excluded.updated_at RETURNING id');
  const insertJob = db.prepare(`INSERT INTO jobs (id,source,source_job_id,canonical_listing_url,canonical_application_url,ats_kind,ats_identifier,title,company,location,workplace_type,employment_types_json,description,description_sha256,source_posted_at,source_updated_at,discovered_at,first_seen_at,last_seen_at,availability_state,freshness_state,eligibility_state,eligibility_reason_codes_json,priority,dedupe_group_id,raw_payload_path,raw_payload_sha256) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)`);
  const insertObservation = db.prepare(`INSERT INTO source_observations (sync_run_id,job_id,source,source_job_id,observed_at,raw_payload_path,raw_payload_sha256,normalized_job_sha256) VALUES (?,?,?,?,?,?,?,?)`);
  const dedupeByJobId = new Map();
  const groupByIdentity = new Map();
  for (const item of normalizedRows) {
    const { row, normalized } = item;
    let groupId = null;
    if (normalized.dedupeIdentityKind !== 'source') {
      const key = `${normalized.dedupeIdentityKind}\u0000${normalized.dedupeIdentityKey}`;
      groupId = groupByIdentity.get(key);
      if (groupId === undefined) {
        const result = insertGroup.get(normalized.dedupeIdentityKind, normalized.dedupeIdentityKey, normalized.dedupeReviewRequired ? 1 : 0, now, now);
        groupId = Number(result.id);
        groupByIdentity.set(key, groupId);
      }
      dedupeByJobId.set(Number(row.id), groupId);
    }
    insertJob.run(row.id, normalized.source, normalized.sourceJobId, normalized.canonicalListingUrl, normalized.canonicalApplicationUrl, normalized.atsKind, normalized.atsIdentifier, normalized.title, normalized.company, normalized.location, normalized.workplaceType, JSON.stringify(normalized.employmentTypes), normalized.description, normalized.descriptionSha256, normalized.sourcePostedAt, normalized.sourceUpdatedAt, normalized.discoveredAt, normalizeLegacyTimestamp(row.first_seen_at, 'first_seen_at', { nullable: false }), normalizeLegacyTimestamp(row.last_seen_at, 'last_seen_at', { nullable: false }), normalized.availabilityState, normalized.freshnessState, normalized.eligibilityState, JSON.stringify(normalized.eligibilityReasonCodes), normalized.priority, groupId, normalized.rawPayloadPath, normalized.rawPayloadSha256);
    const syncRunId = syncMap.get(normalized.source) ?? null;
    insertObservation.run(syncRunId, row.id, normalized.source, normalized.sourceJobId, normalizeLegacyTimestamp(row.last_seen_at, 'last_seen_at', { nullable: false }), normalized.rawPayloadPath, normalized.rawPayloadSha256, sha256Canonical(normalized));
  }
  return dedupeByJobId;
}

export function openIngestionDatabase(databasePath) {
  if (typeof databasePath !== 'string' || databasePath.length === 0 || databasePath.includes('\u0000')) throw new Error('E_DATABASE_PATH');
  const db = new DatabaseSync(databasePath);
  db.exec('PRAGMA foreign_keys = ON; PRAGMA busy_timeout = 5000;');
  return db;
}

export function initializeIngestionDatabase(databasePath, { now = new Date() } = {}) {
  const appliedAt = nowIso(now);
  const db = openIngestionDatabase(databasePath);
  try {
    db.exec('BEGIN IMMEDIATE;');
    db.exec(FINAL_SCHEMA_SQL);
    db.exec(FINAL_APPLICATION_INDEX_SQL);
    const identity = migrationIdentity(db);
    const digest = migrationDigest();
    if (identity && (identity.name !== INGESTION_MIGRATION_NAME || identity.sha256 !== digest)) throw new Error('E_SCHEMA_MIGRATION_IDENTITY');
    if (!identity) db.prepare('INSERT INTO schema_migrations (version,name,sha256,applied_at) VALUES (?,?,?,?)').run(INGESTION_MIGRATION_VERSION, INGESTION_MIGRATION_NAME, digest, appliedAt);
    db.exec('COMMIT;');
    assertIngestionSchema(db);
    return Object.freeze({ databasePath, version: INGESTION_MIGRATION_VERSION, idempotent: Boolean(identity) });
  } catch (error) {
    try { db.exec('ROLLBACK;'); } catch {}
    throw error;
  } finally {
    db.close();
  }
}

export async function migrateIngestionDatabase(databasePath, { payloadRoot, now = new Date() } = {}) {
  if (typeof payloadRoot !== 'string') throw new Error('E_PAYLOAD_ROOT');
  const appliedAt = nowIso(now);
  const root = path.resolve(payloadRoot);
  await ensurePrivateDirectory(root);
  const db = openIngestionDatabase(databasePath);
  const createdPayloads = [];
  try {
    if (tableExists(db, 'schema_migrations')) {
      const identity = migrationIdentity(db);
      if (identity) {
        const digest = migrationDigest();
        if (identity.name !== INGESTION_MIGRATION_NAME || identity.sha256 !== digest) throw new Error('E_SCHEMA_MIGRATION_IDENTITY');
        assertIngestionSchema(db);
        return Object.freeze({ databasePath, version: INGESTION_MIGRATION_VERSION, idempotent: true });
      }
    }
    assertLegacySchema(db);
    const legacyJobs = db.prepare('SELECT * FROM jobs ORDER BY id').all();
    const legacySyncRuns = db.prepare('SELECT * FROM sync_runs ORDER BY id').all();
    const staged = [];
    for (const row of legacyJobs) {
      const rawText = rawTextForRow(row);
      const digest = digestBytes(rawText);
      const payload = await writePrivatePayload(root, safeSource(row.source), digest, rawText);
      if (payload.created) createdPayloads.push(payload.path);
      const normalized = normalizeLegacyJob(row, payload.path, digest, row.last_seen_at, appliedAt);
      staged.push({ row, normalized });
    }
    db.exec('PRAGMA foreign_keys = OFF; BEGIN IMMEDIATE;');
    db.exec('ALTER TABLE jobs RENAME TO legacy_jobs; ALTER TABLE sync_runs RENAME TO legacy_sync_runs;');
    db.exec(FINAL_SCHEMA_SQL);
    const syncMap = createLegacySyncRows(db, legacySyncRuns, root, appliedAt);
    createLegacyCheckpoints(db, legacySyncRuns, appliedAt);
    const dedupeByJobId = migrateRows(db, legacyJobs, syncMap, staged, appliedAt);
    createFinalApplicationJobs(db);
    // Preserve one-to-one application source_rowid bindings while linking queue rows to groups.
    const updateApplication = db.prepare('UPDATE application_jobs SET dedupe_group_id = ? WHERE source_rowid = ? AND source_table IN (\'jobs\', \'legacy_jobs\')');
    for (const [jobId, groupId] of dedupeByJobId) updateApplication.run(groupId, jobId);
    db.prepare('INSERT INTO schema_migrations (version,name,sha256,applied_at) VALUES (?,?,?,?)').run(INGESTION_MIGRATION_VERSION, INGESTION_MIGRATION_NAME, migrationDigest(), appliedAt);
    db.exec('DROP TABLE legacy_jobs; DROP TABLE legacy_sync_runs; COMMIT; PRAGMA foreign_keys = ON;');
    assertIngestionSchema(db);
    return Object.freeze({ databasePath, version: INGESTION_MIGRATION_VERSION, idempotent: false, jobsMigrated: staged.length, payloadRoot: root });
  } catch (error) {
    try { db.exec('ROLLBACK; PRAGMA foreign_keys = ON;'); } catch {}
    if (createdPayloads.length > 0) {
      try { await removeCreatedPayloads(createdPayloads); } catch {}
    }
    throw error;
  } finally {
    db.close();
  }
}

export const FINAL_TABLE_COLUMNS = Object.freeze({
  jobs: JOB_COLUMNS,
  dedupe_groups: DEDUPE_COLUMNS,
  source_observations: OBSERVATION_COLUMNS,
  source_checkpoints: CHECKPOINT_COLUMNS,
  sync_runs: SYNC_RUN_COLUMNS,
  source_sync_pages: PAGE_COLUMNS,
  schema_migrations: MIGRATION_COLUMNS,
  application_jobs: APPLICATION_JOB_COLUMNS_WITH_DEDUPE,
  application_runs: APPLICATION_RUN_COLUMNS,
});

export default Object.freeze({
  FINAL_SCHEMA_SQL,
  FINAL_TABLE_COLUMNS,
  INGESTION_MIGRATION_VERSION,
  INGESTION_MIGRATION_NAME,
  openIngestionDatabase,
  initializeIngestionDatabase,
  migrateIngestionDatabase,
  assertIngestionSchema,
});
