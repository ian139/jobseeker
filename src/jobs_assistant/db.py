from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .contracts import ActionAttempt, RunStatus, StoredJobInfo


SCHEMA_SQL = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,
    source_job_id TEXT,
    canonical_url TEXT,
    title TEXT NOT NULL,
    company TEXT NOT NULL,
    location TEXT,
    remote INTEGER,
    posted_at TEXT,
    discovered_at TEXT NOT NULL,
    description TEXT,
    status TEXT NOT NULL DEFAULT 'queued' CHECK (status IN ('queued', 'in_progress', 'archived')),
    raw_json TEXT NOT NULL DEFAULT '{}',
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    CHECK (source_job_id IS NOT NULL OR canonical_url IS NOT NULL),
    UNIQUE(source, source_job_id),
    UNIQUE(canonical_url)
);

CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
CREATE INDEX IF NOT EXISTS idx_jobs_discovered_at ON jobs(discovered_at);
CREATE INDEX IF NOT EXISTS idx_jobs_posted_at ON jobs(posted_at);

CREATE TABLE IF NOT EXISTS sync_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,
    mode TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    success INTEGER NOT NULL DEFAULT 0,
    jobs_seen INTEGER NOT NULL DEFAULT 0,
    jobs_returned INTEGER NOT NULL DEFAULT 0,
    jobs_inserted INTEGER NOT NULL DEFAULT 0,
    jobs_updated INTEGER NOT NULL DEFAULT 0,
    error TEXT
);

CREATE TABLE IF NOT EXISTS application_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    status TEXT NOT NULL CHECK (status IN ('dry_run_ready','needs_review','blocked','failed')),
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
"""


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def encode_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def decode_json(value: str | None, default: Any) -> Any:
    if not value:
        return default
    return json.loads(value)


def canonicalize_url(value: str | None) -> str | None:
    if not value or not value.strip():
        return None
    parsed = urlsplit(value.strip())
    if not parsed.netloc:
        return value.strip().rstrip("/") or None
    query = [
        (key, val)
        for key, val in parse_qsl(parsed.query, keep_blank_values=True)
        if not key.lower().startswith("utm_") and key.lower() not in {"gclid", "fbclid", "msclkid", "gh_src"}
    ]
    path = parsed.path.rstrip("/") or "/"
    return urlunsplit((parsed.scheme.lower() or "https", parsed.netloc.lower(), path, urlencode(query, doseq=True), ""))


def connect(db_path: str | Path) -> sqlite3.Connection:
    if str(db_path) != ":memory:":
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(str(db_path))
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    return connection


def initialize_database(connection: sqlite3.Connection) -> None:
    connection.executescript(SCHEMA_SQL)
    connection.commit()


def init_db(connection: sqlite3.Connection) -> None:
    initialize_database(connection)


def _first_string(raw: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = raw.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if value is not None and not isinstance(value, (dict, list)):
            return str(value)
    return None


def _company_value(raw: dict[str, Any]) -> str:
    company = raw.get("company")
    if isinstance(company, dict):
        name = company.get("name")
        if name:
            return str(name)
    if isinstance(company, str) and company.strip():
        return company.strip()
    return _first_string(raw, "company_name") or "Unknown company"


def find_existing_job(connection: sqlite3.Connection, source_job_id: str | None, canonical_url: str | None, source: str) -> sqlite3.Row | None:
    if source_job_id:
        row = connection.execute("SELECT * FROM jobs WHERE source = ? AND source_job_id = ?", (source, source_job_id)).fetchone()
        if row is not None:
            return row
    if canonical_url:
        return connection.execute("SELECT * FROM jobs WHERE canonical_url = ?", (canonical_url,)).fetchone()
    return None


def upsert_raw_job(connection: sqlite3.Connection, raw: dict[str, Any], *, source: str = "theirstack") -> StoredJobInfo:
    source_job_id = _first_string(raw, "source_job_id", "id", "job_id", "theirstack_job_id", "external_id")
    url = _first_string(raw, "apply_url", "url", "job_url", "listing_url", "canonical_url")
    canonical_url = canonicalize_url(url)
    if not source_job_id and not canonical_url:
        raise ValueError("job needs source_job_id or url")
    title = _first_string(raw, "title", "job_title", "normalized_title") or "Untitled role"
    company = _company_value(raw)
    location = _first_string(raw, "location", "job_location", "city", "country_code")
    remote = raw.get("remote")
    remote_int = int(remote) if isinstance(remote, bool) else None
    posted_at = _first_string(raw, "posted_at", "date_posted")
    discovered_at = _first_string(raw, "discovered_at") or utc_now()
    description = _first_string(raw, "description", "job_description", "description_text")
    existing = find_existing_job(connection, source_job_id, canonical_url, source)
    now = utc_now()
    if existing is not None:
        connection.execute(
            """
            UPDATE jobs
            SET source_job_id = COALESCE(?, source_job_id), canonical_url = COALESCE(?, canonical_url),
                title = ?, company = ?, location = ?, remote = ?, posted_at = ?, description = ?,
                raw_json = ?, last_seen_at = ?
            WHERE id = ?
            """,
            (source_job_id, canonical_url, title, company, location, remote_int, posted_at, description, encode_json(raw), now, existing["id"]),
        )
        connection.commit()
        return StoredJobInfo("updated", str(existing["discovered_at"]))
    connection.execute(
        """
        INSERT INTO jobs (source, source_job_id, canonical_url, title, company, location, remote, posted_at,
                          discovered_at, description, raw_json, first_seen_at, last_seen_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (source, source_job_id, canonical_url, title, company, location, remote_int, posted_at, discovered_at, description, encode_json(raw), now, now),
    )
    connection.commit()
    return StoredJobInfo("inserted", discovered_at)


def record_sync_run(connection: sqlite3.Connection, source: str, mode: str, *, started_at: str | None = None, profile: str | None = None) -> int:
    actual_source = profile or source
    cur = connection.execute("INSERT INTO sync_runs (source, mode, started_at) VALUES (?, ?, ?)", (actual_source, mode, started_at or utc_now()))
    connection.commit()
    return int(cur.lastrowid)


def update_sync_run(connection: sqlite3.Connection, run_id: int, **kwargs: Any) -> None:
    allowed = {"finished_at", "success", "jobs_seen", "jobs_returned", "jobs_inserted", "jobs_updated", "error"}
    fields = {key: value for key, value in kwargs.items() if key in allowed}
    if not fields:
        return
    values = list(fields.values()) + [run_id]
    connection.execute(f"UPDATE sync_runs SET {', '.join(f'{key}=?' for key in fields)} WHERE id=?", values)
    connection.commit()


def latest_sync_checkpoint(connection: sqlite3.Connection) -> str | None:
    row = connection.execute("SELECT MAX(started_at) AS checkpoint FROM sync_runs WHERE success = 1").fetchone()
    return str(row["checkpoint"]) if row and row["checkpoint"] else None


def start_application_run(connection: sqlite3.Connection, job_id: int, *, reason: str = "running") -> int:
    cur = connection.execute(
        "INSERT INTO application_runs (job_id, status, reason, started_at) VALUES (?, ?, ?, ?)",
        (job_id, RunStatus.FAILED.value, reason, utc_now()),
    )
    connection.commit()
    return int(cur.lastrowid)


def record_application_page(connection: sqlite3.Connection, run_id: int, page_index: int, *, url: str, snapshot_json: str, resolver_json: str | None) -> None:
    connection.execute(
        "INSERT INTO application_pages (run_id, page_index, url, snapshot_json, resolver_json, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (run_id, page_index, url, snapshot_json, resolver_json, utc_now()),
    )
    connection.commit()


def finish_application_run(
    connection: sqlite3.Connection,
    run_id: int,
    *,
    status: RunStatus,
    reason: str,
    final_url: str | None,
    actions: list[ActionAttempt] | None = None,
) -> None:
    action_payload = [action.__dict__ for action in actions or []]
    connection.execute(
        "UPDATE application_runs SET status = ?, reason = ?, finished_at = ?, final_url = ?, actions_json = ? WHERE id = ?",
        (status.value, reason, utc_now(), final_url, encode_json(action_payload), run_id),
    )
    connection.commit()
