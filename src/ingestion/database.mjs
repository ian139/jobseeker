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
export const INGESTION_MIGRATION_VERSION = 8;
export const INGESTION_MIGRATION_NAME = '008-resume-artifacts';

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
const HISTORICAL_SYNC_RUN_COLUMNS = Object.freeze([
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
const SYNC_RUN_COLUMNS = Object.freeze([
  ...HISTORICAL_SYNC_RUN_COLUMNS,
  'page_limit',
  'max_pages',
  'max_items',
]);
const PAGE_COLUMNS = Object.freeze(['sync_run_id', 'page_number', 'request_sha256', 'response_path', 'response_sha256', 'received_at', 'item_count', 'total_results', 'estimated_credits', 'reported_credits']);
const CREDIT_AUDIT_COLUMNS = Object.freeze(['sync_run_id', 'source', 'period_start', 'period_end', 'observed_before_at', 'credits_before', 'observed_after_at', 'credits_after', 'reported_credits', 'state', 'reason_code']);
const MIGRATION_COLUMNS = Object.freeze(['version', 'name', 'sha256', 'applied_at']);

const HISTORICAL_SCHEMA_V5_SQL = `
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
  source TEXT NOT NULL CHECK (substr(source, 1, 1) GLOB '[a-z]' AND source NOT GLOB '*[^a-z0-9_-]*' AND length(source) BETWEEN 1 AND 64),
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
  pending_request_sha256 TEXT CHECK (pending_request_sha256 IS NULL OR (pending_request_sha256 NOT GLOB '*[^0-9a-f]*' AND length(pending_request_sha256) = 64)),
  pending_started_at TEXT CHECK (pending_started_at IS NULL OR pending_started_at GLOB '????-??-??T??:??:??.???Z'),
  next_page INTEGER NOT NULL DEFAULT 0 CHECK (next_page >= 0),
  expected_total_results INTEGER CHECK (expected_total_results IS NULL OR expected_total_results >= 0),
  failure_class TEXT CHECK (failure_class IS NULL OR failure_class IN ('retryable', 'terminal', 'paid_ambiguous', 'authentication', 'account_health')),
  reason_code TEXT CHECK (reason_code IS NULL OR (length(reason_code) BETWEEN 1 AND 256 AND substr(reason_code, 1, 1) GLOB '[a-z]' AND reason_code NOT GLOB '*[^a-z0-9_]*')),
  result_sha256 TEXT CHECK (result_sha256 IS NULL OR (result_sha256 NOT GLOB '*[^0-9a-f]*' AND length(result_sha256) = 64)),
  CHECK ((pending_page IS NULL AND pending_request_sha256 IS NULL AND pending_started_at IS NULL) OR (pending_page IS NOT NULL AND pending_request_sha256 IS NOT NULL AND pending_started_at IS NOT NULL)),
  CHECK (state IN ('fetching', 'ready_to_commit') OR finished_at IS NOT NULL),
  CHECK (state <> 'succeeded' OR failure_class IS NULL),
  CHECK (state <> 'paid_ambiguous' OR failure_class = 'paid_ambiguous'),
  CHECK (state <> 'failed' OR (failure_class IS NOT NULL AND reason_code IS NOT NULL)),
  CHECK (state <> 'succeeded' OR result_sha256 IS NOT NULL)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_sync_runs_nonterminal ON sync_runs(source, profile) WHERE state IN ('fetching', 'ready_to_commit');

CREATE TABLE IF NOT EXISTS jobs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  source TEXT NOT NULL CHECK (substr(source, 1, 1) GLOB '[a-z]' AND source NOT GLOB '*[^a-z0-9_-]*' AND length(source) BETWEEN 1 AND 64),
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
  description_sha256 TEXT NOT NULL CHECK (description_sha256 NOT GLOB '*[^0-9a-f]*' AND length(description_sha256) = 64),
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
  dedupe_group_id INTEGER NOT NULL REFERENCES dedupe_groups(id) ON DELETE RESTRICT,
  raw_payload_path TEXT NOT NULL CHECK (length(raw_payload_path) BETWEEN 1 AND 4096),
  raw_payload_sha256 TEXT NOT NULL CHECK (raw_payload_sha256 NOT GLOB '*[^0-9a-f]*' AND length(raw_payload_sha256) = 64),
  CHECK (source_job_id IS NOT NULL OR canonical_application_url IS NOT NULL),
  UNIQUE (source, source_job_id)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_jobs_source_job_id ON jobs(source, source_job_id) WHERE source_job_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS idx_jobs_source_application_fallback ON jobs(source, canonical_application_url) WHERE source_job_id IS NULL AND canonical_application_url IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_jobs_dedupe_group ON jobs(dedupe_group_id);
CREATE INDEX IF NOT EXISTS idx_jobs_eligibility_priority ON jobs(eligibility_state, priority, last_seen_at);

CREATE TABLE IF NOT EXISTS source_checkpoints (
  source TEXT NOT NULL CHECK (substr(source, 1, 1) GLOB '[a-z]' AND source NOT GLOB '*[^a-z0-9_-]*' AND length(source) BETWEEN 1 AND 64),
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
  source TEXT NOT NULL CHECK (substr(source, 1, 1) GLOB '[a-z]' AND source NOT GLOB '*[^a-z0-9_-]*' AND length(source) BETWEEN 1 AND 64),
  source_job_id TEXT,
  observed_at TEXT NOT NULL CHECK (observed_at GLOB '????-??-??T??:??:??.???Z'),
  raw_payload_path TEXT NOT NULL CHECK (length(raw_payload_path) BETWEEN 1 AND 4096),
  raw_payload_sha256 TEXT NOT NULL CHECK (raw_payload_sha256 NOT GLOB '*[^0-9a-f]*' AND length(raw_payload_sha256) = 64),
  normalized_job_sha256 TEXT NOT NULL CHECK (normalized_job_sha256 NOT GLOB '*[^0-9a-f]*' AND length(normalized_job_sha256) = 64),
  UNIQUE(sync_run_id, job_id, raw_payload_sha256)
);

CREATE TABLE IF NOT EXISTS source_sync_pages (
  sync_run_id INTEGER NOT NULL REFERENCES sync_runs(id) ON DELETE RESTRICT,
  page_number INTEGER NOT NULL CHECK (page_number >= 0),
  request_sha256 TEXT NOT NULL CHECK (request_sha256 NOT GLOB '*[^0-9a-f]*' AND length(request_sha256) = 64),
  response_path TEXT NOT NULL CHECK (length(response_path) BETWEEN 1 AND 4096),
  response_sha256 TEXT NOT NULL CHECK (response_sha256 NOT GLOB '*[^0-9a-f]*' AND length(response_sha256) = 64),
  received_at TEXT NOT NULL CHECK (received_at GLOB '????-??-??T??:??:??.???Z'),
  item_count INTEGER NOT NULL CHECK (item_count >= 0),
  total_results INTEGER CHECK (total_results IS NULL OR total_results >= 0),
  estimated_credits REAL NOT NULL CHECK (estimated_credits >= 0),
  reported_credits REAL CHECK (reported_credits IS NULL OR reported_credits >= 0),
  PRIMARY KEY (sync_run_id, page_number)
);
${applicationJobTableSql('application_jobs', { ifNotExists: true })}

${applicationRunTableSql('application_runs', { ifNotExists: true })}

CREATE TABLE IF NOT EXISTS schema_migrations (
  version INTEGER PRIMARY KEY,
  name TEXT NOT NULL CHECK (length(name) BETWEEN 1 AND 256),
  sha256 TEXT NOT NULL CHECK (sha256 NOT GLOB '*[^0-9a-f]*' AND length(sha256) = 64),
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
const SOURCE_CREDIT_AUDIT_SQL = `
CREATE TABLE IF NOT EXISTS source_credit_audits (
  sync_run_id INTEGER PRIMARY KEY REFERENCES sync_runs(id) ON DELETE RESTRICT,
  source TEXT NOT NULL CHECK (
    substr(source, 1, 1) GLOB '[a-z]'
    AND source NOT GLOB '*[^a-z0-9_-]*'
    AND length(source) BETWEEN 1 AND 64
  ),
  period_start TEXT NOT NULL CHECK (
    period_start GLOB '????-??-??T??:??:??.???Z'
    AND strftime('%Y-%m-%dT%H:%M:%fZ', period_start) = period_start
  ),
  period_end TEXT NOT NULL CHECK (
    period_end GLOB '????-??-??T??:??:??.???Z'
    AND strftime('%Y-%m-%dT%H:%M:%fZ', period_end) = period_end
  ),
  observed_before_at TEXT NOT NULL CHECK (
    observed_before_at GLOB '????-??-??T??:??:??.???Z'
    AND strftime('%Y-%m-%dT%H:%M:%fZ', observed_before_at) = observed_before_at
  ),
  credits_before INTEGER NOT NULL CHECK (
    typeof(credits_before) = 'integer'
    AND credits_before BETWEEN 0 AND 9007199254740991
  ),
  observed_after_at TEXT CHECK (
    observed_after_at IS NULL
    OR (
      observed_after_at GLOB '????-??-??T??:??:??.???Z'
      AND strftime('%Y-%m-%dT%H:%M:%fZ', observed_after_at) = observed_after_at
    )
  ),
  credits_after INTEGER CHECK (
    credits_after IS NULL
    OR (
      typeof(credits_after) = 'integer'
      AND credits_after BETWEEN 0 AND 9007199254740991
    )
  ),
  reported_credits INTEGER CHECK (
    reported_credits IS NULL
    OR (
      typeof(reported_credits) = 'integer'
      AND reported_credits BETWEEN 0 AND 9007199254740991
    )
  ),
  state TEXT NOT NULL CHECK (state IN ('pending', 'reconciled', 'unavailable')),
  reason_code TEXT CHECK (
    reason_code IS NULL
    OR (
      length(reason_code) BETWEEN 1 AND 64
      AND substr(reason_code, 1, 1) GLOB '[a-z]'
      AND reason_code NOT GLOB '*[^a-z0-9_]*'
    )
  ),
  CHECK (
    (state = 'pending' AND observed_after_at IS NULL AND credits_after IS NULL AND reported_credits IS NULL AND reason_code IS NULL)
    OR (state = 'reconciled' AND observed_after_at IS NOT NULL AND credits_after IS NOT NULL AND reported_credits = credits_after - credits_before AND reason_code IS NULL)
    OR (state = 'unavailable' AND observed_after_at IS NULL AND credits_after IS NULL AND reported_credits IS NULL AND reason_code IS NOT NULL)
  )
);
CREATE TRIGGER IF NOT EXISTS source_credit_audits_immutable_delete
BEFORE DELETE ON source_credit_audits
BEGIN
  SELECT RAISE(ABORT, 'immutable source credit audit');
END;
CREATE TRIGGER IF NOT EXISTS source_credit_audits_guarded_update
BEFORE UPDATE ON source_credit_audits
BEGIN
  SELECT CASE
    WHEN OLD.state <> 'pending'
      OR NEW.state NOT IN ('reconciled', 'unavailable')
      OR NEW.sync_run_id IS NOT OLD.sync_run_id
      OR NEW.source IS NOT OLD.source
      OR NEW.period_start IS NOT OLD.period_start
      OR NEW.period_end IS NOT OLD.period_end
      OR NEW.observed_before_at IS NOT OLD.observed_before_at
      OR NEW.credits_before IS NOT OLD.credits_before
    THEN RAISE(ABORT, 'invalid source credit audit transition')
  END;
END;
`;
const SYNC_RUN_BOUNDS_SQL = `
ALTER TABLE sync_runs ADD COLUMN page_limit INTEGER CHECK (
  page_limit IS NULL OR (typeof(page_limit) = 'integer' AND page_limit BETWEEN 1 AND 100)
);
ALTER TABLE sync_runs ADD COLUMN max_pages INTEGER CHECK (
  max_pages IS NULL OR (typeof(max_pages) = 'integer' AND max_pages BETWEEN 1 AND 1000)
);
ALTER TABLE sync_runs ADD COLUMN max_items INTEGER CHECK (
  max_items IS NULL OR (typeof(max_items) = 'integer' AND max_items BETWEEN 1 AND 1000000)
);
`;
const HISTORICAL_SCHEMA_V7_SQL = `${HISTORICAL_SCHEMA_V5_SQL}\n${SOURCE_CREDIT_AUDIT_SQL}\n${SYNC_RUN_BOUNDS_SQL}`;
const RESUME_ARTIFACTS_SQL = `
CREATE TABLE resume_artifacts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  application_job_id INTEGER NOT NULL REFERENCES application_jobs(id) ON DELETE RESTRICT,
  normalized_job_id INTEGER NOT NULL REFERENCES jobs(id) ON DELETE RESTRICT,
  job_description_sha256 TEXT NOT NULL CHECK (length(job_description_sha256) = 64 AND job_description_sha256 NOT GLOB '*[^0-9a-f]*'),
  generator_fingerprint_sha256 TEXT NOT NULL CHECK (length(generator_fingerprint_sha256) = 64 AND generator_fingerprint_sha256 NOT GLOB '*[^0-9a-f]*'),
  generator_schema_version TEXT NOT NULL CHECK (length(generator_schema_version) BETWEEN 1 AND 128),
  manifest_path TEXT NOT NULL CHECK (length(manifest_path) BETWEEN 1 AND 16384),
  manifest_sha256 TEXT NOT NULL CHECK (length(manifest_sha256) = 64 AND manifest_sha256 NOT GLOB '*[^0-9a-f]*'),
  pdf_path TEXT NOT NULL CHECK (length(pdf_path) BETWEEN 1 AND 16384),
  pdf_sha256 TEXT NOT NULL CHECK (length(pdf_sha256) = 64 AND pdf_sha256 NOT GLOB '*[^0-9a-f]*'),
  job_description_path TEXT NOT NULL CHECK (length(job_description_path) BETWEEN 1 AND 16384),
  pages INTEGER NOT NULL CHECK (pages = 1),
  created_at TEXT NOT NULL CHECK (created_at GLOB '????-??-??T??:??:??.???Z'),
  UNIQUE(application_job_id, job_description_sha256, generator_fingerprint_sha256)
);
CREATE INDEX idx_resume_artifacts_application_job ON resume_artifacts(application_job_id, id);
ALTER TABLE application_jobs ADD COLUMN current_resume_artifact_id INTEGER REFERENCES resume_artifacts(id) ON DELETE RESTRICT;
ALTER TABLE application_jobs ADD COLUMN resume_preparation_reason TEXT;
ALTER TABLE application_jobs ADD COLUMN resume_preparation_attempted_at TEXT CHECK (resume_preparation_attempted_at IS NULL OR resume_preparation_attempted_at GLOB '????-??-??T??:??:??.???Z');
ALTER TABLE application_jobs ADD COLUMN resume_prepared_at TEXT CHECK (resume_prepared_at IS NULL OR resume_prepared_at GLOB '????-??-??T??:??:??.???Z');
ALTER TABLE application_jobs ADD COLUMN resume_preparation_state TEXT NOT NULL DEFAULT 'pending' CHECK (
  (resume_preparation_state = 'pending' AND current_resume_artifact_id IS NULL AND resume_preparation_reason IS NULL AND resume_prepared_at IS NULL)
  OR (resume_preparation_state = 'ready' AND current_resume_artifact_id IS NOT NULL AND resume_preparation_reason IS NULL AND resume_prepared_at IS NOT NULL)
  OR (resume_preparation_state = 'failed' AND current_resume_artifact_id IS NULL AND resume_preparation_reason IS NOT NULL AND length(resume_preparation_reason) BETWEEN 1 AND 64 AND resume_preparation_reason NOT GLOB '*[^a-z0-9_]*' AND resume_prepared_at IS NULL)
);
ALTER TABLE application_runs ADD COLUMN resume_artifact_id INTEGER REFERENCES resume_artifacts(id) ON DELETE RESTRICT;
CREATE TRIGGER resume_artifacts_immutable_update BEFORE UPDATE ON resume_artifacts BEGIN
  SELECT RAISE(ABORT, 'immutable resume artifact');
END;
CREATE TRIGGER resume_artifacts_immutable_delete BEFORE DELETE ON resume_artifacts BEGIN
  SELECT RAISE(ABORT, 'immutable resume artifact');
END;
CREATE TRIGGER application_jobs_resume_binding_update
BEFORE UPDATE OF current_resume_artifact_id, resume_preparation_state ON application_jobs
WHEN NEW.resume_preparation_state = 'ready'
BEGIN
  SELECT CASE WHEN NOT EXISTS (
    SELECT 1 FROM resume_artifacts AS a
    WHERE a.id = NEW.current_resume_artifact_id AND a.application_job_id = NEW.id
  ) THEN RAISE(ABORT, 'invalid prepared resume binding') END;
END;
CREATE TRIGGER application_runs_resume_binding_insert BEFORE INSERT ON application_runs WHEN NEW.active = 1 BEGIN
  SELECT CASE WHEN NEW.resume_artifact_id IS NULL OR NOT EXISTS (
    SELECT 1 FROM resume_artifacts AS a
    WHERE a.id = NEW.resume_artifact_id AND a.application_job_id = NEW.job_id
      AND a.pdf_path = NEW.resume_artifact_path AND a.pdf_sha256 = NEW.resume_artifact_sha256
  ) THEN RAISE(ABORT, 'invalid active resume binding') END;
END;
CREATE TRIGGER application_runs_resume_binding_update
BEFORE UPDATE OF active, resume_artifact_id, resume_artifact_path, resume_artifact_sha256, job_id ON application_runs
WHEN NEW.active = 1
BEGIN
  SELECT CASE WHEN NEW.resume_artifact_id IS NULL OR NOT EXISTS (
    SELECT 1 FROM resume_artifacts AS a
    WHERE a.id = NEW.resume_artifact_id AND a.application_job_id = NEW.job_id
      AND a.pdf_path = NEW.resume_artifact_path AND a.pdf_sha256 = NEW.resume_artifact_sha256
  ) THEN RAISE(ABORT, 'invalid active resume binding') END;
END;
`;
export const FINAL_SCHEMA_SQL = `${HISTORICAL_SCHEMA_V7_SQL}\n${RESUME_ARTIFACTS_SQL}`;
const HISTORICAL_MIGRATION_005 = Object.freeze({
  version: 5,
  name: '005-unified-ingestion',
  sha256: '783c09fa4c083f731a90c27f0b61c3242ef0b0224daf656d9281bbf8db35d2c3',
});
const HISTORICAL_MIGRATION_006 = Object.freeze({
  version: 6,
  name: '006-source-credit-audit',
  sha256: 'a0cf117e7b155ac7dd842a77250261141b41490c69c502f8e1d72da2de47997e',
});
const HISTORICAL_MIGRATION_007 = Object.freeze({
  version: 7,
  name: '007-sync-run-bounds',
  sha256: '7b33c5aac22a2efbbfaa5c579182780defa22fa446a0ab7086d07282dfdaf0c4',
});

const LEGACY_JOB_COLUMNS = Object.freeze(['id', 'source', 'source_job_id', 'canonical_url', 'title', 'company', 'location', 'remote', 'posted_at', 'discovered_at', 'description', 'status', 'raw_json', 'first_seen_at', 'last_seen_at']);
const LEGACY_SYNC_COLUMNS = Object.freeze(['id', 'source', 'profile', 'mode', 'started_at', 'finished_at', 'checkpoint', 'success', 'jobs_seen', 'jobs_returned', 'jobs_inserted', 'jobs_updated', 'error']);
const APPLICATION_JOB_COLUMNS = Object.freeze(['id', 'source_table', 'source_db', 'source_rowid', 'source_job_id', 'application_url', 'eligibility_tier', 'verification_reason', 'source_posted_at', 'source_last_seen_at', 'status', 'status_reason', 'claimed_at', 'completed_at']);
const APPLICATION_JOB_COLUMNS_WITH_DEDUPE = Object.freeze([...APPLICATION_JOB_COLUMNS, 'dedupe_group_id']);
const APPLICATION_JOB_COLUMNS_WITH_RESUME = Object.freeze([...APPLICATION_JOB_COLUMNS_WITH_DEDUPE, 'current_resume_artifact_id', 'resume_preparation_reason', 'resume_preparation_attempted_at', 'resume_prepared_at', 'resume_preparation_state']);
const APPLICATION_RUN_COLUMNS = Object.freeze(['id', 'job_id', 'status', 'reason_code', 'started_at', 'finished_at', 'final_url', 'actions_json', 'evidence_path', 'submit_action_count', 'active', 'owner_id', 'browser_session_id', 'claimed_at', 'lease_expires_at', 'last_progress_at', 'workspace_path', 'resume_artifact_path', 'resume_artifact_sha256', 'answer_memory_path', 'blocker_alias']);
const APPLICATION_RUN_COLUMNS_WITH_RESUME = Object.freeze([...APPLICATION_RUN_COLUMNS, 'resume_artifact_id']);
const RESUME_ARTIFACT_COLUMNS = Object.freeze(['id', 'application_job_id', 'normalized_job_id', 'job_description_sha256', 'generator_fingerprint_sha256', 'generator_schema_version', 'manifest_path', 'manifest_sha256', 'pdf_path', 'pdf_sha256', 'job_description_path', 'pages', 'created_at']);

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
  const actual = tableColumns(db, table);
  if (
    (!allowExtra && actual.length !== expected.length)
    || expected.some((name, index) => allowExtra ? !actual.includes(name) : actual[index] !== name)
  ) throw new Error(`E_SCHEMA_COLUMNS:${table}`);
}

function migrationVersion(db) {
  return db.prepare('SELECT MAX(version) AS version FROM schema_migrations').get().version;
}

function migrationIdentity(db) {
  return db.prepare('SELECT version, name, sha256, applied_at FROM schema_migrations WHERE version = ?').get(INGESTION_MIGRATION_VERSION);
}

function migrationRow(db, version) {
  return db.prepare('SELECT version, name, sha256, applied_at FROM schema_migrations WHERE version = ?').get(version);
}

function migrationDigest() {
  return digestBytes(`${FINAL_SCHEMA_SQL}\n${FINAL_APPLICATION_INDEX_SQL}`);
}

function assertMigrationIdentity(row, expected) {
  if (
    !row
    || Number(row.version) !== expected.version
    || row.name !== expected.name
    || row.sha256 !== expected.sha256
  ) throw new Error('E_SCHEMA_MIGRATION_IDENTITY');
}

function assertHistoricalMigration(row) {
  assertMigrationIdentity(row, HISTORICAL_MIGRATION_005);
}

function assertMigrationHistory(db) {
  const historicalV5 = migrationRow(db, HISTORICAL_MIGRATION_005.version);
  if (historicalV5 !== undefined) assertMigrationIdentity(historicalV5, HISTORICAL_MIGRATION_005);
  const historicalV6 = migrationRow(db, HISTORICAL_MIGRATION_006.version);
  if (historicalV6 !== undefined) assertMigrationIdentity(historicalV6, HISTORICAL_MIGRATION_006);
  const historicalV7 = migrationRow(db, HISTORICAL_MIGRATION_007.version);
  if (historicalV7 !== undefined) assertMigrationIdentity(historicalV7, HISTORICAL_MIGRATION_007);
  if (db.prepare('SELECT 1 FROM schema_migrations WHERE version > ? LIMIT 1').get(INGESTION_MIGRATION_VERSION) !== undefined) throw new Error('E_SCHEMA_MIGRATION_FUTURE');
}

function normalizedSchemaSql(sql) {
  return String(sql).replace(/["`]/gu, '').replace(/\s+/gu, '').replace(/;$/u, '');
}

function schemaSignature(db) {
  return JSON.stringify(
    db.prepare("SELECT type,name,tbl_name,sql FROM sqlite_master WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%' ORDER BY type,name").all().map((row) => ({
      type: row.type,
      name: row.name,
      table: row.tbl_name,
      sql: normalizedSchemaSql(row.sql),
    })),
  );
}

let expectedSchemaSignature = null;

function finalSchemaSignature() {
  if (expectedSchemaSignature !== null) return expectedSchemaSignature;
  const expected = new DatabaseSync(':memory:');
  try {
    expected.exec(FINAL_SCHEMA_SQL);
    expected.exec(FINAL_APPLICATION_INDEX_SQL);
    expectedSchemaSignature = schemaSignature(expected);
    return expectedSchemaSignature;
  } finally {
    expected.close();
  }
}
let expectedV5SchemaSignature = null;

function historicalV5SchemaSignature() {
  if (expectedV5SchemaSignature !== null) return expectedV5SchemaSignature;
  const expected = new DatabaseSync(':memory:');
  try {
    expected.exec(HISTORICAL_SCHEMA_V5_SQL);
    expected.exec(FINAL_APPLICATION_INDEX_SQL);
    expectedV5SchemaSignature = schemaSignature(expected);
    return expectedV5SchemaSignature;
  } finally {
    expected.close();
  }
}
let expectedV6SchemaSignature = null;

function historicalV6SchemaSignature() {
  if (expectedV6SchemaSignature !== null) return expectedV6SchemaSignature;
  const expected = new DatabaseSync(':memory:');
  try {
    expected.exec(HISTORICAL_SCHEMA_V5_SQL);
    expected.exec(SOURCE_CREDIT_AUDIT_SQL);
    expected.exec(FINAL_APPLICATION_INDEX_SQL);
    expectedV6SchemaSignature = schemaSignature(expected);
    return expectedV6SchemaSignature;
  } finally {
    expected.close();
  }
}
let expectedV7SchemaSignature = null;

function historicalV7SchemaSignature() {
  if (expectedV7SchemaSignature !== null) return expectedV7SchemaSignature;
  const expected = new DatabaseSync(':memory:');
  try {
    expected.exec(HISTORICAL_SCHEMA_V7_SQL);
    expected.exec(FINAL_APPLICATION_INDEX_SQL);
    expectedV7SchemaSignature = schemaSignature(expected);
    return expectedV7SchemaSignature;
  } finally {
    expected.close();
  }
}


export function assertIngestionSchema(database) {
  const owned = typeof database === 'string' ? openIngestionDatabase(database) : null;
  const db = owned ?? database;
  try {
    for (const [table, columns] of [
      ['jobs', JOB_COLUMNS],
      ['dedupe_groups', DEDUPE_COLUMNS],
      ['source_observations', OBSERVATION_COLUMNS],
      ['source_checkpoints', CHECKPOINT_COLUMNS],
      ['sync_runs', SYNC_RUN_COLUMNS],
      ['source_sync_pages', PAGE_COLUMNS],
      ['source_credit_audits', CREDIT_AUDIT_COLUMNS],
      ['schema_migrations', MIGRATION_COLUMNS],
    ]) assertColumns(db, table, columns);
    assertColumns(db, 'application_jobs', APPLICATION_JOB_COLUMNS_WITH_RESUME);
    assertColumns(db, 'application_runs', APPLICATION_RUN_COLUMNS_WITH_RESUME);
    assertColumns(db, 'resume_artifacts', RESUME_ARTIFACT_COLUMNS);
    const forbidden = db.prepare("SELECT name FROM pragma_table_info('jobs') WHERE name IN ('raw_json', 'status')").all();
    if (forbidden.length > 0) throw new Error('E_SCHEMA_OBSOLETE_COLUMN:jobs');
    if (schemaSignature(db) !== finalSchemaSignature()) throw new Error('E_SCHEMA_OBJECTS');
    assertMigrationHistory(db);
    const identity = migrationIdentity(db);
    if (!identity || identity.name !== INGESTION_MIGRATION_NAME || identity.sha256 !== migrationDigest()) throw new Error('E_SCHEMA_MIGRATION_IDENTITY');
    if (db.prepare('SELECT 1 FROM schema_migrations WHERE version > ? LIMIT 1').get(INGESTION_MIGRATION_VERSION) !== undefined) throw new Error('E_SCHEMA_MIGRATION_FUTURE');
    if (db.prepare('PRAGMA foreign_key_check').get() !== undefined) throw new Error('E_SCHEMA_FOREIGN_KEY');
    return true;
  } finally {
    if (owned) owned.close();
  }
}

function assertCanonicalV5Schema(db) {
  assertHistoricalMigration(migrationRow(db, HISTORICAL_MIGRATION_005.version));
  for (const [table, columns] of [
    ['jobs', JOB_COLUMNS],
    ['dedupe_groups', DEDUPE_COLUMNS],
    ['source_observations', OBSERVATION_COLUMNS],
    ['source_checkpoints', CHECKPOINT_COLUMNS],
    ['sync_runs', HISTORICAL_SYNC_RUN_COLUMNS],
    ['source_sync_pages', PAGE_COLUMNS],
    ['schema_migrations', MIGRATION_COLUMNS],
  ]) assertColumns(db, table, columns);
  assertColumns(db, 'application_jobs', APPLICATION_JOB_COLUMNS_WITH_DEDUPE);
  assertColumns(db, 'application_runs', APPLICATION_RUN_COLUMNS);
  const forbidden = db.prepare("SELECT name FROM pragma_table_info('jobs') WHERE name IN ('raw_json', 'status')").all();
  if (forbidden.length > 0) throw new Error('E_SCHEMA_OBSOLETE_COLUMN:jobs');
  if (schemaSignature(db) !== historicalV5SchemaSignature()) throw new Error('E_SCHEMA_OBJECTS');
  if (db.prepare('SELECT 1 FROM schema_migrations WHERE version > ? LIMIT 1').get(HISTORICAL_MIGRATION_005.version) !== undefined) throw new Error('E_SCHEMA_MIGRATION_FUTURE');
  if (db.prepare('PRAGMA foreign_key_check').get() !== undefined) throw new Error('E_SCHEMA_FOREIGN_KEY');
}

function assertCanonicalV6Schema(db) {
  assertMigrationIdentity(migrationRow(db, HISTORICAL_MIGRATION_005.version), HISTORICAL_MIGRATION_005);
  assertMigrationIdentity(migrationRow(db, HISTORICAL_MIGRATION_006.version), HISTORICAL_MIGRATION_006);
  for (const [table, columns] of [
    ['jobs', JOB_COLUMNS],
    ['dedupe_groups', DEDUPE_COLUMNS],
    ['source_observations', OBSERVATION_COLUMNS],
    ['source_checkpoints', CHECKPOINT_COLUMNS],
    ['sync_runs', HISTORICAL_SYNC_RUN_COLUMNS],
    ['source_sync_pages', PAGE_COLUMNS],
    ['source_credit_audits', CREDIT_AUDIT_COLUMNS],
    ['schema_migrations', MIGRATION_COLUMNS],
  ]) assertColumns(db, table, columns);
  assertColumns(db, 'application_jobs', APPLICATION_JOB_COLUMNS_WITH_DEDUPE);
  assertColumns(db, 'application_runs', APPLICATION_RUN_COLUMNS);
  if (schemaSignature(db) !== historicalV6SchemaSignature()) throw new Error('E_SCHEMA_OBJECTS');
  if (db.prepare('SELECT 1 FROM schema_migrations WHERE version > ? LIMIT 1').get(HISTORICAL_MIGRATION_006.version) !== undefined) throw new Error('E_SCHEMA_MIGRATION_FUTURE');
  if (db.prepare('PRAGMA foreign_key_check').get() !== undefined) throw new Error('E_SCHEMA_FOREIGN_KEY');
}

function assertCanonicalV7Schema(db) {
  assertMigrationIdentity(migrationRow(db, HISTORICAL_MIGRATION_005.version), HISTORICAL_MIGRATION_005);
  assertMigrationIdentity(migrationRow(db, HISTORICAL_MIGRATION_006.version), HISTORICAL_MIGRATION_006);
  assertMigrationIdentity(migrationRow(db, HISTORICAL_MIGRATION_007.version), HISTORICAL_MIGRATION_007);
  for (const [table, columns] of [
    ['jobs', JOB_COLUMNS],
    ['dedupe_groups', DEDUPE_COLUMNS],
    ['source_observations', OBSERVATION_COLUMNS],
    ['source_checkpoints', CHECKPOINT_COLUMNS],
    ['sync_runs', SYNC_RUN_COLUMNS],
    ['source_sync_pages', PAGE_COLUMNS],
    ['source_credit_audits', CREDIT_AUDIT_COLUMNS],
    ['schema_migrations', MIGRATION_COLUMNS],
  ]) assertColumns(db, table, columns);
  assertColumns(db, 'application_jobs', APPLICATION_JOB_COLUMNS_WITH_DEDUPE);
  assertColumns(db, 'application_runs', APPLICATION_RUN_COLUMNS);
  if (schemaSignature(db) !== historicalV7SchemaSignature()) throw new Error('E_SCHEMA_OBJECTS');
  if (db.prepare('SELECT 1 FROM schema_migrations WHERE version > ? LIMIT 1').get(HISTORICAL_MIGRATION_007.version) !== undefined) throw new Error('E_SCHEMA_MIGRATION_FUTURE');
  if (db.prepare('PRAGMA foreign_key_check').get() !== undefined) throw new Error('E_SCHEMA_FOREIGN_KEY');
}

function upgradeV7InTransaction(db, appliedAt) {
  assertCanonicalV7Schema(db);
  if (db.prepare('SELECT id FROM application_runs WHERE active = 1 LIMIT 1').get() !== undefined) {
    throw new Error('E_SCHEMA_ACTIVE_APPLICATION_RUN');
  }
  db.exec(RESUME_ARTIFACTS_SQL);
  db.prepare('INSERT INTO schema_migrations (version,name,sha256,applied_at) VALUES (?,?,?,?)').run(
    INGESTION_MIGRATION_VERSION,
    INGESTION_MIGRATION_NAME,
    migrationDigest(),
    appliedAt,
  );
}

function upgradeV6InTransaction(db, appliedAt) {
  assertCanonicalV6Schema(db);
  const active = db.prepare("SELECT id FROM sync_runs WHERE state IN ('fetching','ready_to_commit') LIMIT 1").get();
  if (active !== undefined) throw new Error('E_SCHEMA_ACTIVE_RUN');
  db.exec(SYNC_RUN_BOUNDS_SQL);
  db.prepare("UPDATE sync_runs SET page_limit = COALESCE(page_limit,25),max_pages = COALESCE(max_pages,100),max_items = NULL WHERE state IN ('failed','succeeded','paid_ambiguous')");
  db.prepare('INSERT INTO schema_migrations (version,name,sha256,applied_at) VALUES (?,?,?,?)').run(
    HISTORICAL_MIGRATION_007.version,
    HISTORICAL_MIGRATION_007.name,
    HISTORICAL_MIGRATION_007.sha256,
    appliedAt,
  );
}

function upgradeV5InTransaction(db, appliedAt) {
  assertCanonicalV5Schema(db);
  db.exec(SOURCE_CREDIT_AUDIT_SQL);
  db.prepare('INSERT INTO schema_migrations (version,name,sha256,applied_at) VALUES (?,?,?,?)').run(
    HISTORICAL_MIGRATION_006.version,
    HISTORICAL_MIGRATION_006.name,
    HISTORICAL_MIGRATION_006.sha256,
    appliedAt,
  );
  upgradeV6InTransaction(db, appliedAt);
}

function assertLegacySchema(db) {
  assertColumns(db, 'jobs', LEGACY_JOB_COLUMNS);
  assertColumns(db, 'sync_runs', LEGACY_SYNC_COLUMNS);
  assertColumns(db, 'application_jobs', APPLICATION_JOB_COLUMNS, { allowExtra: false });
  assertColumns(db, 'application_runs', APPLICATION_RUN_COLUMNS, { allowExtra: false });
  if (tableExists(db, 'schema_migrations')) {
    const rows = db.prepare('SELECT version, name, sha256 FROM schema_migrations').all();
    const historical = rows.find((row) => Number(row.version) === HISTORICAL_MIGRATION_005.version);
    if (historical !== undefined) assertHistoricalMigration(historical);
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

function normalizeLegacyCheckpoint(value, source) {
  if (value === null || value === undefined || value === '') return null;
  const checkpoint = String(value);
  if (checkpoint.length > 512 || /[\u0000-\u001f\u007f]/u.test(checkpoint)) throw new Error('E_LEGACY_CHECKPOINT');
  return source === 'theirstack'
    ? normalizeLegacyTimestamp(checkpoint, 'sync.checkpoint', { nullable: false })
    : checkpoint;
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
  const existing = await checkExistingPayload(destination, digest, rawText);
  if (existing !== null) return { path: destination, created: false };
  const tempPath = path.join(sourceDir, `.tmp-${crypto.randomUUID()}`);
  let tempCreated = false;
  try {
    const handle = await fsp.open(tempPath, WRITE_ONLY | CREATE | EXCLUSIVE | NOFOLLOW, PRIVATE_FILE_MODE);
    try {
      await handle.writeFile(rawText, 'utf8');
      await handle.sync();
    } finally {
      await handle.close();
    }
    tempCreated = true;
    const tempBytes = await fsp.readFile(tempPath);
    if (digestBytes(tempBytes) !== digest || tempBytes.toString('utf8') !== rawText) throw new Error('E_PAYLOAD_DIGEST');
    try {
      await fsp.link(tempPath, destination);
    } catch (error) {
      if (error?.code !== 'EEXIST') throw error;
      const retry = await checkExistingPayload(destination, digest, rawText);
      if (retry === null) throw new Error('E_PAYLOAD_DIGEST');
    }
  } finally {
    if (tempCreated) {
      try { await fsp.unlink(tempPath); } catch {}
    }
  }
  return { path: destination, created: true };
}

async function checkExistingPayload(destination, digest, rawText) {
  try {
    const stat = await fsp.lstat(destination);
    if (!stat.isFile()) return null;
    if ((stat.mode & 0o777) !== PRIVATE_FILE_MODE || (typeof process.getuid === 'function' && stat.uid !== process.getuid())) throw new Error('E_PAYLOAD_FILE_PRIVATE');
    const existing = await fsp.readFile(destination);
    if (digestBytes(existing) !== digest || existing.toString('utf8') !== rawText) throw new Error('E_PAYLOAD_DIGEST');
    return true;
  } catch (error) {
    if (error?.code === 'ENOENT') return null;
    throw error;
  }
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

function applicationJobTableSql(table = 'application_jobs_new', { ifNotExists = false } = {}) {
  return `
CREATE TABLE ${ifNotExists ? 'IF NOT EXISTS ' : ''}${quoteIdentifier(table)} (
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

function applicationRunTableSql(table = 'application_runs_new', { ifNotExists = false } = {}) {
  return `
CREATE TABLE ${ifNotExists ? 'IF NOT EXISTS ' : ''}${quoteIdentifier(table)} (
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
`;
}

function applicationGroupRank(row) {
  if (row.status === 'completed' || row.status === 'closed' || row.status === 'skipped') return 0;
  if (row.status === 'claimed' || row.status === 'needs_user' || row.status === 'blocked') return 1;
  if (row.status === 'queued') return 2;
  return 3;
}

function copyApplicationJobs(db, dedupeByJobId, databasePath) {
  const rows = db.prepare('SELECT * FROM application_jobs ORDER BY id').all();
  const insert = db.prepare(`INSERT INTO application_jobs_new (id,source_table,source_db,source_rowid,source_job_id,application_url,eligibility_tier,verification_reason,source_posted_at,source_last_seen_at,status,status_reason,claimed_at,completed_at,dedupe_group_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)`);
  const groupByRow = new Map();
  const keeperByGroup = new Map();
  for (const row of rows) {
    const localLegacy = row.source_table === 'jobs' && row.source_db === databasePath;
    const sourceDb = localLegacy ? 'ingestion' : row.source_db;
    const groupId = localLegacy ? dedupeByJobId.get(Number(row.source_rowid)) ?? null : null;
    groupByRow.set(row.id, groupId);
    if (groupId === null) continue;
    const keeper = keeperByGroup.get(groupId);
    if (keeper === undefined || applicationGroupRank(row) < applicationGroupRank(keeper)) keeperByGroup.set(groupId, row);
  }
  for (const row of rows) {
    const groupId = groupByRow.get(row.id);
    const keeper = groupId === null ? null : keeperByGroup.get(groupId);
    const assigned = keeper?.id === row.id ? groupId : null;
    const duplicateQueued = groupId !== null && assigned === null && row.status === 'queued';
    const localLegacy = row.source_table === 'jobs' && row.source_db === databasePath;
    const sourceDb = localLegacy ? 'ingestion' : row.source_db;
    insert.run(
      row.id,
      row.source_table,
      sourceDb,
      row.source_rowid,
      row.source_job_id,
      row.application_url,
      row.eligibility_tier,
      row.verification_reason,
      row.source_posted_at,
      row.source_last_seen_at,
      duplicateQueued ? 'skipped' : row.status,
      duplicateQueued ? 'deduplicated' : row.status_reason,
      row.claimed_at,
      row.completed_at,
      assigned,
    );
  }
}

function createFinalApplicationJobs(db, dedupeByJobId, databasePath) {
  db.exec(applicationJobTableSql());
  copyApplicationJobs(db, dedupeByJobId, databasePath);
  db.exec('DROP TABLE application_jobs; ALTER TABLE application_jobs_new RENAME TO application_jobs;');
}

function createFinalApplicationRuns(db) {
  if (db.prepare('SELECT 1 FROM application_runs WHERE active <> 0 LIMIT 1').get() !== undefined) throw new Error('E_ACTIVE_APPLICATION_RUN');
  db.exec(applicationRunTableSql());
  const columns = APPLICATION_RUN_COLUMNS.map(quoteIdentifier).join(',');
  db.exec(`INSERT INTO application_runs_new (${columns}) SELECT ${columns} FROM application_runs ORDER BY id`);
  db.exec('DROP TABLE application_runs; ALTER TABLE application_runs_new RENAME TO application_runs;');
}

function createLegacySyncRows(db, rows, payloadRoot, now) {
  const insert = db.prepare(`INSERT INTO sync_runs (id,source,profile,mode,state,started_at,finished_at,window_end_at,checkpoint_before,checkpoint_after,artifact_dir,request_count,pages_fetched,jobs_seen,jobs_inserted,jobs_updated,jobs_unchanged,dedupe_groups_touched,queue_rows_inserted,estimated_credits,reported_credits,pending_page,pending_request_sha256,pending_started_at,next_page,expected_total_results,failure_class,reason_code,result_sha256,page_limit,max_pages,max_items) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)`);
  for (const row of rows) {
    const source = safeSource(row.source);
    const profile = row.profile === null || row.profile === undefined || row.profile === '' ? 'legacy' : safeProfile(String(row.profile));
    const startedAt = normalizeLegacyTimestamp(row.started_at, 'sync.started_at', { nullable: false });
    const finishedAt = normalizeOptionalLegacyDate(row.finished_at, 'sync.finished_at') ?? now;
    const checkpoint = normalizeLegacyCheckpoint(row.checkpoint, source);
    const failed = Number(row.success) !== 1;
    const artifactDir = path.join(payloadRoot, source, `sync-${row.id}`);
    const reasonCode = failed ? 'legacy_sync_failed' : null;
    const failureClass = failed ? 'terminal' : null;
    const jobsSeen = nonnegativeCount(row.jobs_seen, 'sync.jobs_seen');
    const jobsReturned = nonnegativeCount(row.jobs_returned, 'sync.jobs_returned');
    const jobsInserted = nonnegativeCount(row.jobs_inserted, 'sync.jobs_inserted');
    const jobsUpdated = nonnegativeCount(row.jobs_updated, 'sync.jobs_updated');
    const resultSha256 = failed ? null : digestBytes(JSON.stringify({ source, profile, checkpoint, jobsSeen, jobsReturned, jobsInserted, jobsUpdated }));
    insert.run(row.id, source, profile, 'paid', failed ? 'failed' : 'succeeded', startedAt, finishedAt, finishedAt, null, checkpoint, artifactDir, jobsReturned > 0 ? 1 : 0, jobsReturned > 0 ? 1 : 0, jobsSeen, jobsInserted, jobsUpdated, Math.max(0, jobsSeen - jobsInserted - jobsUpdated), 0, 0, 0, null, null, null, null, 0, null, failureClass, reasonCode, resultSha256, 25, 100, null);
  }
}
function createLegacyCheckpoints(db, rows, now) {
  const latest = new Map();
  for (const row of rows) {
    if (Number(row.success) !== 1) continue;
    const source = safeSource(row.source);
    const profile = row.profile === null || row.profile === undefined || row.profile === '' ? 'legacy' : safeProfile(String(row.profile));
    const checkpoint = normalizeLegacyCheckpoint(row.checkpoint, source);
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

function migrateRows(db, jobs, normalizedRows, now) {
  const insertGroup = db.prepare('INSERT INTO dedupe_groups (identity_kind,identity_key,review_required,created_at,updated_at) VALUES (?,?,?,?,?) ON CONFLICT(identity_kind,identity_key) DO UPDATE SET updated_at=excluded.updated_at RETURNING id');
  const insertJob = db.prepare(`INSERT INTO jobs (id,source,source_job_id,canonical_listing_url,canonical_application_url,ats_kind,ats_identifier,title,company,location,workplace_type,employment_types_json,description,description_sha256,source_posted_at,source_updated_at,discovered_at,first_seen_at,last_seen_at,availability_state,freshness_state,eligibility_state,eligibility_reason_codes_json,priority,dedupe_group_id,raw_payload_path,raw_payload_sha256) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)`);
  const insertObservation = db.prepare(`INSERT INTO source_observations (sync_run_id,job_id,source,source_job_id,observed_at,raw_payload_path,raw_payload_sha256,normalized_job_sha256) VALUES (?,?,?,?,?,?,?,?)`);
  const dedupeByJobId = new Map();
  const groupByIdentity = new Map();
  for (const item of normalizedRows) {
    const { row, normalized } = item;
    const key = `${normalized.dedupeIdentityKind}\u0000${normalized.dedupeIdentityKey}`;
    let groupId = groupByIdentity.get(key);
    if (groupId === undefined) {
      const result = insertGroup.get(normalized.dedupeIdentityKind, normalized.dedupeIdentityKey, normalized.dedupeReviewRequired ? 1 : 0, now, now);
      groupId = Number(result.id);
      groupByIdentity.set(key, groupId);
    }
    dedupeByJobId.set(Number(row.id), groupId);
    insertJob.run(row.id, normalized.source, normalized.sourceJobId, normalized.canonicalListingUrl, normalized.canonicalApplicationUrl, normalized.atsKind, normalized.atsIdentifier, normalized.title, normalized.company, normalized.location, normalized.workplaceType, JSON.stringify(normalized.employmentTypes), normalized.description, normalized.descriptionSha256, normalized.sourcePostedAt, normalized.sourceUpdatedAt, normalized.discoveredAt, normalizeLegacyTimestamp(row.first_seen_at, 'first_seen_at', { nullable: false }), normalizeLegacyTimestamp(row.last_seen_at, 'last_seen_at', { nullable: false }), normalized.availabilityState, normalized.freshnessState, normalized.eligibilityState, JSON.stringify(normalized.eligibilityReasonCodes), normalized.priority, groupId, normalized.rawPayloadPath, normalized.rawPayloadSha256);
    insertObservation.run(null, row.id, normalized.source, normalized.sourceJobId, normalizeLegacyTimestamp(row.last_seen_at, 'last_seen_at', { nullable: false }), normalized.rawPayloadPath, normalized.rawPayloadSha256, sha256Canonical(normalized));
  }
  return dedupeByJobId;
}

export function openIngestionDatabase(databasePath) {
  if (typeof databasePath !== 'string' || databasePath.length === 0 || databasePath.includes('\u0000')) throw new Error('E_DATABASE_PATH');
  const resolved = path.resolve(databasePath);
  const parent = fs.lstatSync(path.dirname(resolved));
  if (!parent.isDirectory() || (parent.mode & 0o022) !== 0 || (typeof process.getuid === 'function' && parent.uid !== process.getuid())) throw new Error('E_DATABASE_PARENT');
  let before = null;
  try {
    before = fs.lstatSync(resolved);
    if (!before.isFile() || (before.mode & 0o077) !== 0 || (typeof process.getuid === 'function' && before.uid !== process.getuid())) throw new Error('E_DATABASE_PRIVATE');
  } catch (error) {
    if (error?.code !== 'ENOENT') throw error;
  }
  const db = new DatabaseSync(resolved);
  if (before === null) fs.chmodSync(resolved, PRIVATE_FILE_MODE);
  const after = fs.lstatSync(resolved);
  if (!after.isFile() || (after.mode & 0o777) !== PRIVATE_FILE_MODE || (typeof process.getuid === 'function' && after.uid !== process.getuid()) || (before !== null && (before.dev !== after.dev || before.ino !== after.ino))) {
    db.close();
    throw new Error('E_DATABASE_PRIVATE');
  }
  db.exec('PRAGMA foreign_keys = ON; PRAGMA busy_timeout = 5000;');
  return db;
}

export function initializeIngestionDatabase(databasePath, { now = new Date() } = {}) {
  const appliedAt = nowIso(now);
  const db = openIngestionDatabase(databasePath);
  try {
    db.exec('BEGIN IMMEDIATE;');
    const hasMigrationTable = tableExists(db, 'schema_migrations');
    const identityBefore = hasMigrationTable ? migrationIdentity(db) : undefined;
    const historicalV7 = hasMigrationTable ? migrationRow(db, HISTORICAL_MIGRATION_007.version) : undefined;
    const historicalV6 = hasMigrationTable ? migrationRow(db, HISTORICAL_MIGRATION_006.version) : undefined;
    const historicalV5 = hasMigrationTable ? migrationRow(db, HISTORICAL_MIGRATION_005.version) : undefined;
    if (identityBefore !== undefined) {
      if (identityBefore.name !== INGESTION_MIGRATION_NAME || identityBefore.sha256 !== migrationDigest()) throw new Error('E_SCHEMA_MIGRATION_IDENTITY');
      assertIngestionSchema(db);
      db.exec('COMMIT; PRAGMA foreign_keys = ON;');
      return Object.freeze({ databasePath, version: INGESTION_MIGRATION_VERSION, idempotent: true });
    }
    if (historicalV7 !== undefined) {
      upgradeV7InTransaction(db, appliedAt);
      assertIngestionSchema(db);
      db.exec('COMMIT; PRAGMA foreign_keys = ON;');
      return Object.freeze({ databasePath, version: INGESTION_MIGRATION_VERSION, idempotent: false, upgradedFrom: HISTORICAL_MIGRATION_007.version });
    }
    if (historicalV6 !== undefined) {
      upgradeV6InTransaction(db, appliedAt);
      upgradeV7InTransaction(db, appliedAt);
      assertIngestionSchema(db);
      db.exec('COMMIT; PRAGMA foreign_keys = ON;');
      return Object.freeze({ databasePath, version: INGESTION_MIGRATION_VERSION, idempotent: false, upgradedFrom: HISTORICAL_MIGRATION_006.version });
    }
    if (historicalV5 !== undefined) {
      upgradeV5InTransaction(db, appliedAt);
      upgradeV7InTransaction(db, appliedAt);
      assertIngestionSchema(db);
      db.exec('COMMIT; PRAGMA foreign_keys = ON;');
      return Object.freeze({ databasePath, version: INGESTION_MIGRATION_VERSION, idempotent: false, upgradedFrom: HISTORICAL_MIGRATION_005.version });
    }
    db.exec(FINAL_SCHEMA_SQL);
    db.exec(FINAL_APPLICATION_INDEX_SQL);
    const digest = migrationDigest();
    db.prepare('INSERT INTO schema_migrations (version,name,sha256,applied_at) VALUES (?,?,?,?)').run(INGESTION_MIGRATION_VERSION, INGESTION_MIGRATION_NAME, digest, appliedAt);
    assertIngestionSchema(db);
    db.exec('COMMIT; PRAGMA foreign_keys = ON;');
    return Object.freeze({ databasePath, version: INGESTION_MIGRATION_VERSION, idempotent: false });
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
  let committed = false;
  try {
    if (tableExists(db, 'schema_migrations')) {
      const identity = migrationIdentity(db);
      const historicalV7 = migrationRow(db, HISTORICAL_MIGRATION_007.version);
      const historicalV6 = migrationRow(db, HISTORICAL_MIGRATION_006.version);
      const historicalV5 = migrationRow(db, HISTORICAL_MIGRATION_005.version);
      if (identity) {
        const digest = migrationDigest();
        if (identity.name !== INGESTION_MIGRATION_NAME || identity.sha256 !== digest) throw new Error('E_SCHEMA_MIGRATION_IDENTITY');
        assertIngestionSchema(db);
        return Object.freeze({ databasePath, version: INGESTION_MIGRATION_VERSION, idempotent: true });
      }
      if (historicalV7 !== undefined || historicalV6 !== undefined || historicalV5 !== undefined) {
        db.exec('BEGIN IMMEDIATE;');
        let upgradedFrom;
        if (historicalV7 !== undefined) {
          upgradedFrom = HISTORICAL_MIGRATION_007.version;
        } else if (historicalV6 !== undefined) {
          upgradedFrom = HISTORICAL_MIGRATION_006.version;
          upgradeV6InTransaction(db, appliedAt);
        } else {
          upgradedFrom = HISTORICAL_MIGRATION_005.version;
          upgradeV5InTransaction(db, appliedAt);
        }
        upgradeV7InTransaction(db, appliedAt);
        assertIngestionSchema(db);
        db.exec('COMMIT;');
        committed = true;
        db.exec('PRAGMA foreign_keys = ON;');
        return Object.freeze({
          databasePath,
          version: INGESTION_MIGRATION_VERSION,
          idempotent: false,
          upgradedFrom,
        });
      }
    }
    db.exec('PRAGMA foreign_keys = OFF; BEGIN IMMEDIATE;');
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
    db.exec('ALTER TABLE jobs RENAME TO legacy_jobs; ALTER TABLE sync_runs RENAME TO legacy_sync_runs;');
    db.exec(HISTORICAL_SCHEMA_V7_SQL);
    createLegacySyncRows(db, legacySyncRuns, root, appliedAt);
    createLegacyCheckpoints(db, legacySyncRuns, appliedAt);
    const dedupeByJobId = migrateRows(db, legacyJobs, staged, appliedAt);
    createFinalApplicationJobs(db, dedupeByJobId, databasePath);
    createFinalApplicationRuns(db);
    db.exec(RESUME_ARTIFACTS_SQL);
    db.exec(FINAL_APPLICATION_INDEX_SQL);
    db.prepare('INSERT INTO schema_migrations (version,name,sha256,applied_at) VALUES (?,?,?,?)').run(INGESTION_MIGRATION_VERSION, INGESTION_MIGRATION_NAME, migrationDigest(), appliedAt);
    db.exec('DROP TABLE legacy_jobs; DROP TABLE legacy_sync_runs;');
    assertIngestionSchema(db);
    db.exec('COMMIT;');
    committed = true;
    db.exec('PRAGMA foreign_keys = ON;');
    return Object.freeze({ databasePath, version: INGESTION_MIGRATION_VERSION, idempotent: false, jobsMigrated: staged.length, payloadRoot: root });
  } catch (error) {
    try { db.exec('ROLLBACK; PRAGMA foreign_keys = ON;'); } catch {}
    if (!committed && createdPayloads.length > 0) {
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
  source_credit_audits: CREDIT_AUDIT_COLUMNS,
  schema_migrations: MIGRATION_COLUMNS,
  application_jobs: APPLICATION_JOB_COLUMNS_WITH_DEDUPE,
  application_runs: APPLICATION_RUN_COLUMNS,
});
export { migrationVersion };
export default Object.freeze({
  migrationVersion,
  initializeIngestionDatabase,
  migrateIngestionDatabase,
  assertIngestionSchema,
});
