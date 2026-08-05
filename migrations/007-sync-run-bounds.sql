-- Migration 007: persist paid pagination bounds for deterministic crash recovery.
-- Only migrate through migrateIngestionDatabase() or initializeIngestionDatabase(), which
-- verifies the exact v6 identity and schema (and rejects active nonterminal v6 runs)
-- before applying this transactionally.
ALTER TABLE sync_runs ADD COLUMN page_limit INTEGER CHECK (
  page_limit IS NULL OR (typeof(page_limit) = 'integer' AND page_limit BETWEEN 1 AND 100)
);
ALTER TABLE sync_runs ADD COLUMN max_pages INTEGER CHECK (
  max_pages IS NULL OR (typeof(max_pages) = 'integer' AND max_pages BETWEEN 1 AND 1000)
);
ALTER TABLE sync_runs ADD COLUMN max_items INTEGER CHECK (
  max_items IS NULL OR (typeof(max_items) = 'integer' AND max_items BETWEEN 1 AND 1000000)
);
