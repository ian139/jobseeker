from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .contracts import StoredJobInfo


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
    profile TEXT,
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
    cur = connection.execute("INSERT INTO sync_runs (source, profile, mode, started_at) VALUES (?, ?, ?, ?)", (source, profile, mode, started_at or utc_now()))
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


def latest_sync_checkpoint(connection: sqlite3.Connection, *, source: str | None = None, profile: str | None = None) -> str | None:
    clauses = ["success = 1"]
    values: list[Any] = []
    if source is not None:
        clauses.append("source = ?")
        values.append(source)
    if profile is not None:
        clauses.append("profile = ?")
        values.append(profile)
    row = connection.execute(f"SELECT MAX(started_at) AS checkpoint FROM sync_runs WHERE {' AND '.join(clauses)}", values).fetchone()
    return str(row["checkpoint"]) if row and row["checkpoint"] else None
