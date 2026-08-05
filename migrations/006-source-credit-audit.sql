-- Migration 006: durable per-sync-run source credit reconciliation audit.
-- The secure operational entrypoint is migrateIngestionDatabase() in
-- src/ingestion/database.mjs, which verifies the exact v5 identity and schema,
-- applies this one-way change transactionally, and records migration identity.
-- Keep this file limited to the incremental table and trigger definitions;
-- do not use it to bypass the JS runner's private database checks.
CREATE TABLE IF NOT EXISTS source_credit_audits (
  sync_run_id INTEGER PRIMARY KEY REFERENCES sync_runs(id) ON DELETE RESTRICT,
  source TEXT NOT NULL CHECK (
    substr(source, 1, 1) GLOB '[a-z]'
    AND source NOT GLOB '*[^a-z0-9_-]*'
    AND length(source) BETWEEN 1 AND 64
  ),
  period_start TEXT NOT NULL CHECK (
    period_start GLOB '????-??-??T??:??:??.???Z'
    AND strftime('%Y-%m-%dT%H:%M:%fZ', period_start) = period_start
  ),
  period_end TEXT NOT NULL CHECK (
    period_end GLOB '????-??-??T??:??:??.???Z'
    AND strftime('%Y-%m-%dT%H:%M:%fZ', period_end) = period_end
  ),
  observed_before_at TEXT NOT NULL CHECK (
    observed_before_at GLOB '????-??-??T??:??:??.???Z'
    AND strftime('%Y-%m-%dT%H:%M:%fZ', observed_before_at) = observed_before_at
  ),
  credits_before INTEGER NOT NULL CHECK (
    typeof(credits_before) = 'integer'
    AND credits_before BETWEEN 0 AND 9007199254740991
  ),
  observed_after_at TEXT CHECK (
    observed_after_at IS NULL
    OR (
      observed_after_at GLOB '????-??-??T??:??:??.???Z'
      AND strftime('%Y-%m-%dT%H:%M:%fZ', observed_after_at) = observed_after_at
    )
  ),
  credits_after INTEGER CHECK (
    credits_after IS NULL
    OR (
      typeof(credits_after) = 'integer'
      AND credits_after BETWEEN 0 AND 9007199254740991
    )
  ),
  reported_credits INTEGER CHECK (
    reported_credits IS NULL
    OR (
      typeof(reported_credits) = 'integer'
      AND reported_credits BETWEEN 0 AND 9007199254740991
    )
  ),
  state TEXT NOT NULL CHECK (state IN ('pending', 'reconciled', 'unavailable')),
  reason_code TEXT CHECK (
    reason_code IS NULL
    OR (
      length(reason_code) BETWEEN 1 AND 64
      AND substr(reason_code, 1, 1) GLOB '[a-z]'
      AND reason_code NOT GLOB '*[^a-z0-9_]*'
    )
  ),
  CHECK (
    (state = 'pending' AND observed_after_at IS NULL AND credits_after IS NULL AND reported_credits IS NULL AND reason_code IS NULL)
    OR (state = 'reconciled' AND observed_after_at IS NOT NULL AND credits_after IS NOT NULL AND reported_credits = credits_after - credits_before AND reason_code IS NULL)
    OR (state = 'unavailable' AND observed_after_at IS NULL AND credits_after IS NULL AND reported_credits IS NULL AND reason_code IS NOT NULL)
  )
);
CREATE TRIGGER IF NOT EXISTS source_credit_audits_immutable_delete
BEFORE DELETE ON source_credit_audits
BEGIN
  SELECT RAISE(ABORT, 'immutable source credit audit');
END;
CREATE TRIGGER IF NOT EXISTS source_credit_audits_guarded_update
BEFORE UPDATE ON source_credit_audits
BEGIN
  SELECT CASE
    WHEN OLD.state <> 'pending'
      OR NEW.state NOT IN ('reconciled', 'unavailable')
      OR NEW.sync_run_id IS NOT OLD.sync_run_id
      OR NEW.source IS NOT OLD.source
      OR NEW.period_start IS NOT OLD.period_start
      OR NEW.period_end IS NOT OLD.period_end
      OR NEW.observed_before_at IS NOT OLD.observed_before_at
      OR NEW.credits_before IS NOT OLD.credits_before
    THEN RAISE(ABORT, 'invalid source credit audit transition')
  END;
END;
