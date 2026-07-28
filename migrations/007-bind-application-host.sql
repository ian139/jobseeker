-- Migration 007: Bind every supported application to its verified host.
-- Greenhouse/Ashby hosts are derived from their canonical application URLs.
-- Employer-hosted rows require a fresh verified host before they can be queued.

PRAGMA foreign_keys = OFF;
BEGIN IMMEDIATE;

CREATE TEMP TABLE _migration_007_active_guard (
  active_count INTEGER NOT NULL CHECK (active_count = 0)
);
INSERT INTO _migration_007_active_guard (active_count)
SELECT count(*) FROM application_runs WHERE active = 1;
DROP TABLE _migration_007_active_guard;

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
  application_host TEXT CHECK (
    application_host IS NULL
    OR (
      length(application_host) BETWEEN 1 AND 253
      AND application_host = lower(application_host)
      AND application_host NOT GLOB '*[^a-z0-9.-]*'
      AND application_host NOT GLOB '-*'
      AND application_host NOT GLOB '*-'
      AND application_host NOT GLOB '.*'
      AND application_host NOT GLOB '*.'
    )
  ),
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
      AND application_host IS NULL
      AND job_title IS NULL
      AND job_company IS NULL
      AND job_location IS NULL
      AND job_description IS NULL
      AND job_description_sha256 IS NULL
    )
    OR (
      platform IN ('greenhouse','ashby')
      AND application_host IS NOT NULL
      AND length(job_title) > 0
      AND length(job_company) > 0
      AND length(job_location) > 0
      AND length(job_description) > 0
      AND job_description_sha256 IS NOT NULL
    )
    OR (
      platform = 'employer_hosted'
      AND (application_host IS NOT NULL OR status IN ('completed','closed','failed','skipped'))
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
  application_host,
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
  CASE
    WHEN platform = 'employer_hosted'
      AND status NOT IN ('completed','closed','failed','skipped')
      THEN 'skipped'
    ELSE status
  END,
  CASE
    WHEN platform = 'employer_hosted'
      AND status NOT IN ('completed','closed','failed','skipped')
      THEN 'employer_host_reingest_required'
    ELSE status_reason
  END,
  CASE
    WHEN platform = 'employer_hosted'
      AND status NOT IN ('completed','closed','failed','skipped')
      THEN NULL
    ELSE claimed_at
  END,
  CASE
    WHEN platform = 'employer_hosted'
      AND status NOT IN ('completed','closed','failed','skipped')
      THEN NULL
    ELSE completed_at
  END,
  platform,
  CASE
    WHEN platform = 'greenhouse' AND application_url LIKE 'https://job-boards.greenhouse.io/%'
      THEN 'job-boards.greenhouse.io'
    WHEN platform = 'greenhouse' AND application_url LIKE 'https://boards.greenhouse.io/%'
      THEN 'boards.greenhouse.io'
    WHEN platform = 'greenhouse' AND application_url LIKE 'https://job-boards.eu.greenhouse.io/%'
      THEN 'job-boards.eu.greenhouse.io'
    WHEN platform = 'greenhouse' AND application_url LIKE 'https://boards.eu.greenhouse.io/%'
      THEN 'boards.eu.greenhouse.io'
    WHEN platform = 'ashby' AND application_url LIKE 'https://jobs.ashbyhq.com/%'
      THEN 'jobs.ashbyhq.com'
    ELSE NULL
  END,
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
CREATE INDEX idx_application_jobs_application_host
  ON application_jobs(application_host, status, id);

COMMIT;
PRAGMA foreign_keys = ON;
