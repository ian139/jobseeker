-- Migration 004: Durable single-run lifecycle
-- Historical rows remain historical (active = 0); ownership and leases are
-- populated only by the durable backlog-runner lifecycle.

PRAGMA foreign_keys = OFF;
BEGIN IMMEDIATE;

CREATE TABLE application_runs_new (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  job_id INTEGER NOT NULL REFERENCES application_jobs(id) ON DELETE RESTRICT,
  status TEXT NOT NULL CHECK (status IN (
    'preparing',
    'applying',
    'completed',
    'blocked',
    'closed',
    'failed',
    'needs_user',
    'skipped'
  )),
  reason_code TEXT NOT NULL,
  started_at TEXT NOT NULL,
  finished_at TEXT,
  final_url TEXT,
  actions_json TEXT NOT NULL DEFAULT '[]',
  evidence_path TEXT NOT NULL,
  submit_action_count INTEGER CHECK (submit_action_count IS NULL OR submit_action_count >= 0),
  active INTEGER NOT NULL DEFAULT 0 CHECK (active IN (0, 1)),
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
  CHECK (
    active = 0
    OR (
      status IN ('applying', 'needs_user')
      AND owner_id IS NOT NULL
      AND browser_session_id IS NOT NULL
      AND claimed_at IS NOT NULL
      AND lease_expires_at IS NOT NULL
      AND last_progress_at IS NOT NULL
      AND workspace_path IS NOT NULL
      AND evidence_path IS NOT NULL
      AND resume_artifact_path IS NOT NULL
      AND resume_artifact_sha256 IS NOT NULL
      AND answer_memory_path IS NOT NULL
      AND (status <> 'needs_user' OR blocker_alias IS NOT NULL)
    )
  ),
  CHECK (status <> 'completed' OR (submit_action_count IS NOT NULL AND submit_action_count >= 1))
);

INSERT INTO application_runs_new (
  id,
  job_id,
  status,
  reason_code,
  started_at,
  finished_at,
  final_url,
  actions_json,
  evidence_path,
  submit_action_count,
  active,
  owner_id,
  browser_session_id,
  claimed_at,
  lease_expires_at,
  last_progress_at,
  workspace_path,
  resume_artifact_path,
  resume_artifact_sha256,
  answer_memory_path,
  blocker_alias
)
SELECT
  id,
  job_id,
  status,
  reason_code,
  started_at,
  finished_at,
  final_url,
  actions_json,
  evidence_path,
  submit_action_count,
  0,
  NULL,
  NULL,
  NULL,
  NULL,
  NULL,
  NULL,
  NULL,
  NULL,
  NULL,
  NULL
FROM application_runs;

DROP TABLE application_runs;
ALTER TABLE application_runs_new RENAME TO application_runs;

CREATE INDEX idx_application_runs_job_id ON application_runs(job_id);
CREATE INDEX idx_application_runs_status ON application_runs(status);
CREATE INDEX idx_application_runs_active_status ON application_runs(active, status);
CREATE UNIQUE INDEX idx_application_runs_active_job
  ON application_runs(job_id)
  WHERE active = 1;
CREATE UNIQUE INDEX idx_application_runs_active_owner
  ON application_runs(owner_id)
  WHERE active = 1;
CREATE UNIQUE INDEX idx_application_runs_active_global
  ON application_runs((1))
  WHERE active = 1;

COMMIT;
PRAGMA foreign_keys = ON;
