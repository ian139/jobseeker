-- Migration 003: Preserve unknown submit counts without estimates
-- Canonical completed runs require a known positive count; noncanonical runs may
-- retain NULL until the exact browser-action journal can be reconstructed.

PRAGMA foreign_keys = OFF;
BEGIN IMMEDIATE;

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
  submit_action_count INTEGER CHECK (submit_action_count IS NULL OR submit_action_count >= 0),
  CHECK (status <> 'completed' OR (submit_action_count IS NOT NULL AND submit_action_count >= 1))
);

INSERT INTO application_runs_new (
  id, job_id, status, reason_code, started_at, finished_at, final_url,
  actions_json, evidence_path, submit_action_count
)
SELECT
  id, job_id, status, reason_code, started_at, finished_at, final_url,
  actions_json, evidence_path, submit_action_count
FROM application_runs;

DROP TABLE application_runs;
ALTER TABLE application_runs_new RENAME TO application_runs;
CREATE INDEX idx_application_runs_job_id ON application_runs(job_id);
CREATE INDEX idx_application_runs_status ON application_runs(status);

COMMIT;
PRAGMA foreign_keys = ON;
