-- Migration 001: Add needs_user status to application_jobs and application_runs
-- Allows runs to pause for missing truthful applicant facts without terminal-blocking the job.
-- Applied 2026-07-26.

PRAGMA foreign_keys = OFF;
BEGIN IMMEDIATE;

-- application_jobs: add needs_user to status CHECK constraint
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
  status TEXT NOT NULL DEFAULT 'queued' CHECK (status IN ('queued','claimed','ready_for_human','blocked','closed','skipped','failed','needs_user')),
  status_reason TEXT,
  claimed_at TEXT,
  completed_at TEXT,
  UNIQUE(source_table, source_db, source_rowid),
  UNIQUE(application_url)
);
INSERT INTO application_jobs_new SELECT * FROM application_jobs;
DROP TABLE application_jobs;
ALTER TABLE application_jobs_new RENAME TO application_jobs;
CREATE INDEX idx_application_jobs_status_id ON application_jobs(status, id);

-- application_runs: add needs_user to status CHECK constraint
CREATE TABLE application_runs_new (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  job_id INTEGER NOT NULL REFERENCES application_jobs(id) ON DELETE RESTRICT,
  status TEXT NOT NULL CHECK (status IN ('preparing','ready_for_human','blocked','closed','failed','needs_user')),
  reason_code TEXT NOT NULL,
  started_at TEXT NOT NULL,
  finished_at TEXT,
  final_url TEXT,
  actions_json TEXT NOT NULL DEFAULT '[]',
  evidence_path TEXT NOT NULL,
  submit_action_count INTEGER NOT NULL DEFAULT 0 CHECK (submit_action_count = 0)
);
INSERT INTO application_runs_new SELECT * FROM application_runs;
DROP TABLE application_runs;
ALTER TABLE application_runs_new RENAME TO application_runs;
CREATE INDEX idx_application_runs_job_id ON application_runs(job_id);
CREATE INDEX idx_application_runs_status ON application_runs(status);

COMMIT;
PRAGMA foreign_keys = ON;
