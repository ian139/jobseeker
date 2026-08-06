-- Migration 006: Add the employer-hosted platform enum value only.
-- Host binding is added and legacy employer rows are quarantined by migration 007.

PRAGMA foreign_keys = OFF;
BEGIN IMMEDIATE;

CREATE TEMP TABLE _migration_006_active_guard (
  active_count INTEGER NOT NULL CHECK (active_count = 0)
);
INSERT INTO _migration_006_active_guard (active_count)
SELECT count(*) FROM application_runs WHERE active = 1;
DROP TABLE _migration_006_active_guard;

CREATE TABLE application_jobs_new (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  source_table TEXT NOT NULL CHECK (source_table IN ('jobs','legacy_jobs','assistant_jobs')),
  source_db TEXT NOT NULL,
  source_rowid INTEGER NOT NULL,
  source_job_id TEXT NOT NULL,
  application_url TEXT NOT NULL,
  eligibility_tier TEXT NOT NULL CHECK (
    eligibility_tier IN ('active_verified','backfill_only','unverified_stale')
  ),
  verification_reason TEXT,
  source_posted_at TEXT,
  source_last_seen_at TEXT,
  status TEXT NOT NULL DEFAULT 'queued' CHECK (
    status IN ('queued','claimed','completed','blocked','closed','skipped','failed','needs_user')
  ),
  status_reason TEXT,
  claimed_at TEXT,
  completed_at TEXT,
  platform TEXT CHECK (platform IS NULL OR platform IN ('greenhouse','ashby','employer_hosted')),
  job_title TEXT,
  job_company TEXT,
  job_location TEXT,
  job_description TEXT,
  job_description_sha256 TEXT CHECK (
    job_description_sha256 IS NULL
    OR (
      length(job_description_sha256) = 64
      AND job_description_sha256 = lower(job_description_sha256)
      AND job_description_sha256 NOT GLOB '*[^0-9a-f]*'
    )
  ),
  CHECK (
    (
      platform IS NULL
      AND job_title IS NULL
      AND job_company IS NULL
      AND job_location IS NULL
      AND job_description IS NULL
      AND job_description_sha256 IS NULL
    )
    OR (
      platform IS NOT NULL
      AND length(job_title) > 0
      AND length(job_company) > 0
      AND length(job_location) > 0
      AND length(job_description) > 0
      AND job_description_sha256 IS NOT NULL
    )
  ),
  UNIQUE(source_table, source_db, source_rowid),
  UNIQUE(application_url)
);

INSERT INTO application_jobs_new (
  id,
  source_table,
  source_db,
  source_rowid,
  source_job_id,
  application_url,
  eligibility_tier,
  verification_reason,
  source_posted_at,
  source_last_seen_at,
  status,
  status_reason,
  claimed_at,
  completed_at,
  platform,
  job_title,
  job_company,
  job_location,
  job_description,
  job_description_sha256
)
SELECT
  id,
  source_table,
  source_db,
  source_rowid,
  source_job_id,
  application_url,
  eligibility_tier,
  verification_reason,
  source_posted_at,
  source_last_seen_at,
  status,
  status_reason,
  claimed_at,
  completed_at,
  platform,
  job_title,
  job_company,
  job_location,
  job_description,
  job_description_sha256
FROM application_jobs;

DROP TABLE application_jobs;
ALTER TABLE application_jobs_new RENAME TO application_jobs;

CREATE INDEX idx_application_jobs_status_id
  ON application_jobs(status, id);
CREATE INDEX idx_application_jobs_platform_status
  ON application_jobs(platform, status, id);

COMMIT;
PRAGMA foreign_keys = ON;
