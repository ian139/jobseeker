-- Migration 005: unified owner-private ingestion schema.
-- The runtime migration runner stages and validates legacy payloads before applying
-- this cutover. This SQL is also suitable for fresh disposable initialization.
PRAGMA foreign_keys = OFF;
BEGIN IMMEDIATE;

CREATE TABLE IF NOT EXISTS dedupe_groups (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  identity_kind TEXT NOT NULL CHECK (identity_kind IN ('ats','application_url','review_fingerprint')),
  identity_key TEXT NOT NULL CHECK (length(identity_key) BETWEEN 1 AND 1024),
  review_required INTEGER NOT NULL CHECK (review_required IN (0,1)),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(identity_kind, identity_key)
);

CREATE TABLE IF NOT EXISTS sync_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  source TEXT NOT NULL,
  profile TEXT NOT NULL,
  mode TEXT NOT NULL CHECK (mode = 'paid'),
  state TEXT NOT NULL CHECK (state IN ('fetching','ready_to_commit','succeeded','failed','paid_ambiguous')),
  started_at TEXT NOT NULL,
  finished_at TEXT,
  window_end_at TEXT NOT NULL,
  checkpoint_before TEXT,
  checkpoint_after TEXT,
  artifact_dir TEXT NOT NULL,
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
  pending_request_sha256 TEXT,
  pending_started_at TEXT,
  next_page INTEGER NOT NULL DEFAULT 0 CHECK (next_page >= 0),
  expected_total_results INTEGER CHECK (expected_total_results IS NULL OR expected_total_results >= 0),
  failure_class TEXT CHECK (failure_class IS NULL OR failure_class IN ('retryable','terminal','paid_ambiguous','authentication','account_health')),
  reason_code TEXT,
  result_sha256 TEXT,
  CHECK ((pending_page IS NULL AND pending_request_sha256 IS NULL AND pending_started_at IS NULL) OR (pending_page IS NOT NULL AND pending_request_sha256 IS NOT NULL AND pending_started_at IS NOT NULL)),
  CHECK (state IN ('fetching','ready_to_commit') OR finished_at IS NOT NULL),
  CHECK (state <> 'paid_ambiguous' OR failure_class = 'paid_ambiguous'),
  CHECK (state <> 'failed' OR (failure_class IS NOT NULL AND reason_code IS NOT NULL)),
  CHECK (state <> 'succeeded' OR result_sha256 IS NOT NULL)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_sync_runs_nonterminal ON sync_runs(source, profile) WHERE state IN ('fetching','ready_to_commit');

CREATE TABLE IF NOT EXISTS jobs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  source TEXT NOT NULL,
  source_job_id TEXT,
  canonical_listing_url TEXT,
  canonical_application_url TEXT,
  ats_kind TEXT NOT NULL CHECK (ats_kind IN ('greenhouse','ashby','lever','workday','linkedin','custom','unknown')),
  ats_identifier TEXT,
  title TEXT NOT NULL,
  company TEXT NOT NULL,
  location TEXT,
  workplace_type TEXT NOT NULL CHECK (workplace_type IN ('remote','hybrid','onsite','unknown')),
  employment_types_json TEXT NOT NULL CHECK (json_valid(employment_types_json) AND json_type(employment_types_json) = 'array'),
  description TEXT NOT NULL,
  description_sha256 TEXT NOT NULL,
  source_posted_at TEXT,
  source_updated_at TEXT,
  discovered_at TEXT NOT NULL,
  first_seen_at TEXT NOT NULL,
  last_seen_at TEXT NOT NULL,
  availability_state TEXT NOT NULL CHECK (availability_state IN ('open','closed','unknown')),
  freshness_state TEXT NOT NULL CHECK (freshness_state IN ('current','stale','unverified')),
  eligibility_state TEXT NOT NULL CHECK (eligibility_state IN ('eligible','ineligible','review')),
  eligibility_reason_codes_json TEXT NOT NULL CHECK (json_valid(eligibility_reason_codes_json) AND json_type(eligibility_reason_codes_json) = 'array'),
  priority INTEGER NOT NULL CHECK (priority BETWEEN 0 AND 1000),
  dedupe_group_id INTEGER REFERENCES dedupe_groups(id) ON DELETE RESTRICT,
  raw_payload_path TEXT NOT NULL,
  raw_payload_sha256 TEXT NOT NULL,
  CHECK (source_job_id IS NOT NULL OR canonical_application_url IS NOT NULL),
  UNIQUE(source, source_job_id)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_jobs_source_job_id ON jobs(source, source_job_id) WHERE source_job_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS idx_jobs_source_application_fallback ON jobs(source, canonical_application_url) WHERE source_job_id IS NULL AND canonical_application_url IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_jobs_dedupe_group ON jobs(dedupe_group_id);
CREATE INDEX IF NOT EXISTS idx_jobs_eligibility_priority ON jobs(eligibility_state, priority, last_seen_at);

CREATE TABLE IF NOT EXISTS source_checkpoints (
  source TEXT NOT NULL,
  profile TEXT NOT NULL,
  checkpoint TEXT,
  last_sync_run_id INTEGER REFERENCES sync_runs(id) ON DELETE RESTRICT,
  updated_at TEXT NOT NULL,
  revision INTEGER NOT NULL DEFAULT 0 CHECK (revision >= 0),
  PRIMARY KEY (source, profile)
);

CREATE TABLE IF NOT EXISTS source_observations (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  sync_run_id INTEGER REFERENCES sync_runs(id) ON DELETE RESTRICT,
  job_id INTEGER NOT NULL REFERENCES jobs(id) ON DELETE RESTRICT,
  source TEXT NOT NULL,
  source_job_id TEXT,
  observed_at TEXT NOT NULL,
  raw_payload_path TEXT NOT NULL,
  raw_payload_sha256 TEXT NOT NULL,
  normalized_job_sha256 TEXT NOT NULL,
  UNIQUE(sync_run_id, job_id, raw_payload_sha256)
);

CREATE TABLE IF NOT EXISTS source_sync_pages (
  sync_run_id INTEGER NOT NULL REFERENCES sync_runs(id) ON DELETE RESTRICT,
  page_number INTEGER NOT NULL CHECK (page_number >= 0),
  request_sha256 TEXT NOT NULL,
  response_path TEXT NOT NULL,
  response_sha256 TEXT NOT NULL,
  received_at TEXT NOT NULL,
  item_count INTEGER NOT NULL CHECK (item_count >= 0),
  total_results INTEGER,
  estimated_credits REAL NOT NULL CHECK (estimated_credits >= 0),
  PRIMARY KEY (sync_run_id, page_number)
);

CREATE TABLE IF NOT EXISTS schema_migrations (
  version INTEGER PRIMARY KEY,
  name TEXT NOT NULL,
  sha256 TEXT NOT NULL,
  applied_at TEXT NOT NULL
);

CREATE TRIGGER IF NOT EXISTS source_observations_immutable_update BEFORE UPDATE ON source_observations BEGIN SELECT RAISE(ABORT, 'immutable source observation'); END;
CREATE TRIGGER IF NOT EXISTS source_observations_immutable_delete BEFORE DELETE ON source_observations BEGIN SELECT RAISE(ABORT, 'immutable source observation'); END;
CREATE TRIGGER IF NOT EXISTS source_sync_pages_immutable_update BEFORE UPDATE ON source_sync_pages BEGIN SELECT RAISE(ABORT, 'immutable source sync page'); END;
CREATE TRIGGER IF NOT EXISTS source_sync_pages_immutable_delete BEFORE DELETE ON source_sync_pages BEGIN SELECT RAISE(ABORT, 'immutable source sync page'); END;

-- Preserve the Phase 1 application lifecycle while adding one queue-group link.
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
INSERT INTO application_jobs_new (id,source_table,source_db,source_rowid,source_job_id,application_url,eligibility_tier,verification_reason,source_posted_at,source_last_seen_at,status,status_reason,claimed_at,completed_at)
SELECT id,source_table,source_db,source_rowid,source_job_id,application_url,eligibility_tier,verification_reason,source_posted_at,source_last_seen_at,status,status_reason,claimed_at,completed_at
FROM application_jobs;
DROP TABLE application_jobs;
ALTER TABLE application_jobs_new RENAME TO application_jobs;
CREATE INDEX IF NOT EXISTS idx_application_jobs_status_id ON application_jobs(status,id);
CREATE INDEX IF NOT EXISTS idx_application_jobs_dedupe_group ON application_jobs(dedupe_group_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_application_jobs_dedupe_group_unique ON application_jobs(dedupe_group_id) WHERE dedupe_group_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS schema_migrations (
  version INTEGER PRIMARY KEY,
  name TEXT NOT NULL,
  sha256 TEXT NOT NULL,
  applied_at TEXT NOT NULL
);

COMMIT;
PRAGMA foreign_keys = ON;
