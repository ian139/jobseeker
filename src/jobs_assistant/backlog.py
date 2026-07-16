from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Iterable

from .contracts import JobInput
from .db import canonicalize_url, encode_json, find_existing_job, utc_now


@dataclass(frozen=True)
class UpsertResult:
    job_id: int
    inserted: bool
    updated: bool


def _upsert_job(conn: sqlite3.Connection, job: JobInput) -> UpsertResult:
    canonical = canonicalize_url(job.url)
    if not job.source_job_id and not canonical:
        raise ValueError("job needs source_job_id or url")
    existing = find_existing_job(conn, job.source_job_id, canonical, job.source)
    existing_id = int(existing["id"]) if existing is not None else None
    now = utc_now()
    raw_json = encode_json(job.raw)
    remote = None if job.remote is None else int(job.remote)
    if existing_id is None:
        cur = conn.execute(
            """
            INSERT INTO jobs (source, source_job_id, canonical_url, title, company, location, remote, posted_at,
                              discovered_at, description, raw_json, first_seen_at, last_seen_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (job.source, job.source_job_id, canonical, job.title, job.company, job.location, remote, job.posted_at, now, job.description, raw_json, now, now),
        )
        return UpsertResult(int(cur.lastrowid), True, False)
    conn.execute(
        """
        UPDATE jobs
        SET source_job_id = COALESCE(?, source_job_id), canonical_url = COALESCE(?, canonical_url),
            title = ?, company = ?, location = ?, remote = ?, posted_at = ?, description = ?, raw_json = ?, last_seen_at = ?
        WHERE id = ?
        """,
        (job.source_job_id, canonical, job.title, job.company, job.location, remote, job.posted_at, job.description, raw_json, now, existing_id),
    )
    return UpsertResult(existing_id, False, True)


def upsert_job(conn: sqlite3.Connection, job: JobInput) -> UpsertResult:
    result = _upsert_job(conn, job)
    conn.commit()
    return result


def upsert_jobs(conn: sqlite3.Connection, jobs: Iterable[JobInput]) -> tuple[int, int]:
    inserted = updated = 0
    owns_transaction = not conn.in_transaction
    savepoint = "_jobs_assistant_upsert_jobs"
    if owns_transaction:
        conn.execute("BEGIN")
    else:
        conn.execute(f"SAVEPOINT {savepoint}")
    try:
        for job in jobs:
            result = _upsert_job(conn, job)
            inserted += int(result.inserted)
            updated += int(result.updated)
        if owns_transaction:
            conn.commit()
        else:
            conn.execute(f"RELEASE SAVEPOINT {savepoint}")
    except BaseException:
        if owns_transaction:
            conn.rollback()
        else:
            conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
            conn.execute(f"RELEASE SAVEPOINT {savepoint}")
        raise
    return inserted, updated


def next_queued_jobs(conn: sqlite3.Connection, *, limit: int = 10) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            """
            SELECT * FROM jobs
            WHERE status = 'queued' AND canonical_url IS NOT NULL
            ORDER BY posted_at DESC NULLS LAST, first_seen_at ASC, id ASC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    )


def job_application_url(row: sqlite3.Row | dict[str, object]) -> str | None:
    value = row["canonical_url"] if isinstance(row, sqlite3.Row) else row.get("canonical_url")
    return str(value) if value else None


def count_backlog(conn: sqlite3.Connection) -> dict[str, int]:
    counts = conn.execute(
        "SELECT COUNT(*) AS total, SUM(status = 'queued') AS pending FROM jobs"
    ).fetchone()
    return {"total": int(counts[0]), "pending": int(counts[1] or 0)}
