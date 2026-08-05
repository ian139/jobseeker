-- Migration 008: bind verified job-specific resume artifacts before claim.
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
CREATE TRIGGER resume_artifacts_immutable_update
BEFORE UPDATE ON resume_artifacts
BEGIN
  SELECT RAISE(ABORT, 'immutable resume artifact');
END;
CREATE TRIGGER resume_artifacts_immutable_delete
BEFORE DELETE ON resume_artifacts
BEGIN
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
CREATE TRIGGER application_runs_resume_binding_insert
BEFORE INSERT ON application_runs
WHEN NEW.active = 1
BEGIN
  SELECT CASE WHEN NEW.resume_artifact_id IS NULL OR NOT EXISTS (
    SELECT 1 FROM resume_artifacts AS a
    WHERE a.id = NEW.resume_artifact_id
      AND a.application_job_id = NEW.job_id
      AND a.pdf_path = NEW.resume_artifact_path
      AND a.pdf_sha256 = NEW.resume_artifact_sha256
  ) THEN RAISE(ABORT, 'invalid active resume binding') END;
END;
CREATE TRIGGER application_runs_resume_binding_update
BEFORE UPDATE OF active, resume_artifact_id, resume_artifact_path, resume_artifact_sha256, job_id ON application_runs
WHEN NEW.active = 1
BEGIN
  SELECT CASE WHEN NEW.resume_artifact_id IS NULL OR NOT EXISTS (
    SELECT 1 FROM resume_artifacts AS a
    WHERE a.id = NEW.resume_artifact_id
      AND a.application_job_id = NEW.job_id
      AND a.pdf_path = NEW.resume_artifact_path
      AND a.pdf_sha256 = NEW.resume_artifact_sha256
  ) THEN RAISE(ABORT, 'invalid active resume binding') END;
END;
