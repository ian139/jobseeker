-- Historical v5 schema reference: declarative DDL only.
-- Only migrateIngestionDatabase() performs secure legacy migration and records migration identity.
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

CREATE INDEX IF NOT EXISTS idx_application_jobs_status_id ON application_jobs(status, id);
CREATE INDEX IF NOT EXISTS idx_application_jobs_dedupe_group ON application_jobs(dedupe_group_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_application_jobs_dedupe_group_unique ON application_jobs(dedupe_group_id) WHERE dedupe_group_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_application_runs_job_id ON application_runs(job_id);
CREATE INDEX IF NOT EXISTS idx_application_runs_status ON application_runs(status);
CREATE INDEX IF NOT EXISTS idx_application_runs_active_status ON application_runs(active, status);
CREATE UNIQUE INDEX IF NOT EXISTS idx_application_runs_active_job ON application_runs(job_id) WHERE active = 1;
CREATE UNIQUE INDEX IF NOT EXISTS idx_application_runs_active_owner ON application_runs(owner_id) WHERE active = 1;
CREATE UNIQUE INDEX IF NOT EXISTS idx_application_runs_active_global ON application_runs((1)) WHERE active = 1;
