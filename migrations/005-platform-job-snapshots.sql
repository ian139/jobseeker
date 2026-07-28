-- Migration 005: Greenhouse/Ashby-only normalized job snapshots
-- Historical rows remain intact. Unsupported queued rows are quarantined as
-- skipped; runtime classification remains the exact claim-boundary authority.

PRAGMA foreign_keys = ON;
BEGIN IMMEDIATE;

ALTER TABLE application_jobs
  ADD COLUMN platform TEXT
  CHECK (platform IS NULL OR platform IN ('greenhouse', 'ashby'));
ALTER TABLE application_jobs ADD COLUMN job_title TEXT;
ALTER TABLE application_jobs ADD COLUMN job_company TEXT;
ALTER TABLE application_jobs ADD COLUMN job_location TEXT;
ALTER TABLE application_jobs ADD COLUMN job_description TEXT;
ALTER TABLE application_jobs
  ADD COLUMN job_description_sha256 TEXT
  CHECK (
    job_description_sha256 IS NULL
    OR (
      length(job_description_sha256) = 64
      AND job_description_sha256 = lower(job_description_sha256)
      AND job_description_sha256 NOT GLOB '*[^0-9a-f]*'
    )
  );

UPDATE application_jobs
SET platform = CASE
  WHEN lower(application_url) GLOB 'https://job-boards.greenhouse.io/*/jobs/[1-9][0-9]*'
    OR lower(application_url) GLOB 'https://boards.greenhouse.io/*/jobs/[1-9][0-9]*'
    THEN 'greenhouse'
  WHEN lower(application_url) GLOB 'https://jobs.ashbyhq.com/*/????????-????-????-????-????????????*'
    THEN 'ashby'
  ELSE NULL
END
WHERE platform IS NULL;

UPDATE application_jobs
SET status = 'skipped',
    status_reason = 'unsupported_platform',
    claimed_at = NULL,
    completed_at = NULL
WHERE status = 'queued'
  AND platform IS NULL;

CREATE INDEX idx_application_jobs_platform_status
  ON application_jobs(platform, status, id);

COMMIT;
