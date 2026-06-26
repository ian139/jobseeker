CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    theirstack_job_id TEXT UNIQUE,
    canonical_url TEXT UNIQUE,
    title TEXT,
    company_name TEXT,
    location TEXT,
    country_code TEXT,
    remote INTEGER,
    posted_at TEXT,
    discovered_at TEXT,
    raw_json TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    CHECK (theirstack_job_id IS NOT NULL OR canonical_url IS NOT NULL)
);

CREATE INDEX IF NOT EXISTS idx_jobs_discovered_at ON jobs(discovered_at);
CREATE INDEX IF NOT EXISTS idx_jobs_posted_at ON jobs(posted_at);

CREATE TABLE IF NOT EXISTS sync_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    profile TEXT NOT NULL,
    mode TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    success INTEGER NOT NULL DEFAULT 0,
    jobs_returned INTEGER NOT NULL DEFAULT 0,
    jobs_inserted INTEGER NOT NULL DEFAULT 0,
    jobs_updated INTEGER NOT NULL DEFAULT 0,
    checkpoint_discovered_at TEXT,
    error TEXT
);

CREATE TABLE IF NOT EXISTS application_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    status TEXT NOT NULL CHECK (status IN ('dry_run_ready', 'needs_review', 'blocked', 'failed')),
    reason TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    final_url TEXT,
    actions_json TEXT NOT NULL DEFAULT '[]'
);

CREATE INDEX IF NOT EXISTS idx_application_runs_job_id ON application_runs(job_id);
CREATE INDEX IF NOT EXISTS idx_application_runs_status ON application_runs(status);

CREATE TABLE IF NOT EXISTS application_pages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL REFERENCES application_runs(id) ON DELETE CASCADE,
    page_index INTEGER NOT NULL,
    url TEXT NOT NULL,
    snapshot_json TEXT NOT NULL,
    resolver_json TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(run_id, page_index)
);

CREATE INDEX IF NOT EXISTS idx_application_pages_run_id ON application_pages(run_id);
