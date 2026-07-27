-- Migration 002: Enable audited OMP submission lifecycle
-- Requeues legacy ready_for_human jobs because they were prepared but not submitted.

PRAGMA foreign_keys = OFF;
BEGIN IMMEDIATE;

CREATE TABLE application_jobs_new (
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
  status TEXT NOT NULL DEFAULT 'queued' CHECK (status IN ('queued','claimed','completed','blocked','closed','skipped','failed','needs_user')),
  status_reason TEXT,
  claimed_at TEXT,
  completed_at TEXT,
  UNIQUE(source_table, source_db, source_rowid),
  UNIQUE(application_url)
);

INSERT INTO application_jobs_new (
  id, source_table, source_db, source_rowid, source_job_id, application_url,
  eligibility_tier, verification_reason, source_posted_at, source_last_seen_at,
  status, status_reason, claimed_at, completed_at
)
SELECT
  id, source_table, source_db, source_rowid, source_job_id, application_url,
  eligibility_tier, verification_reason, source_posted_at, source_last_seen_at,
  CASE status WHEN 'ready_for_human' THEN 'queued' ELSE status END,
  CASE status WHEN 'ready_for_human' THEN NULL ELSE status_reason END,
  CASE status WHEN 'ready_for_human' THEN NULL ELSE claimed_at END,
  CASE status WHEN 'ready_for_human' THEN NULL ELSE completed_at END
FROM application_jobs;

DROP TABLE application_jobs;
ALTER TABLE application_jobs_new RENAME TO application_jobs;

CREATE TABLE application_runs_new (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  job_id INTEGER NOT NULL REFERENCES application_jobs(id) ON DELETE RESTRICT,
  status TEXT NOT NULL CHECK (status IN ('preparing','completed','blocked','closed','failed','needs_user')),
  reason_code TEXT NOT NULL,
  started_at TEXT NOT NULL,
  finished_at TEXT,
  final_url TEXT,
  actions_json TEXT NOT NULL DEFAULT '[]',
  evidence_path TEXT NOT NULL,
  submit_action_count INTEGER NOT NULL DEFAULT 0 CHECK (submit_action_count >= 0)
);

INSERT INTO application_runs_new (
  id, job_id, status, reason_code, started_at, finished_at, final_url,
  actions_json, evidence_path, submit_action_count
)
SELECT
  id, job_id,
  CASE status WHEN 'ready_for_human' THEN 'failed' ELSE status END,
  CASE status WHEN 'ready_for_human' THEN 'legacy_prepared_not_submitted' ELSE reason_code END,
  started_at, finished_at, final_url, actions_json, evidence_path, submit_action_count
FROM application_runs;

DROP TABLE application_runs;
ALTER TABLE application_runs_new RENAME TO application_runs;
CREATE INDEX idx_application_jobs_status_id ON application_jobs(status, id);
CREATE INDEX idx_application_runs_job_id ON application_runs(job_id);
CREATE INDEX idx_application_runs_status ON application_runs(status);

COMMIT;
PRAGMA foreign_keys = ON;
