from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Iterable

from .contracts import JobInput
from .db import canonicalize_url, encode_json, utc_now


@dataclass(frozen=True)
class UpsertResult:
    job_id: int
    inserted: bool
    updated: bool


def _existing_job_id(conn: sqlite3.Connection, job: JobInput, canonical_url: str | None) -> int | None:
    if job.source_job_id:
        row = conn.execute("SELECT id FROM jobs WHERE source = ? AND source_job_id = ?", (job.source, job.source_job_id)).fetchone()
        if row:
            return int(row["id"])
    if canonical_url:
        row = conn.execute("SELECT id FROM jobs WHERE canonical_url = ?", (canonical_url,)).fetchone()
        if row:
            return int(row["id"])
    return None


def upsert_job(conn: sqlite3.Connection, job: JobInput) -> UpsertResult:
    canonical = canonicalize_url(job.url)
    if not job.source_job_id and not canonical:
        raise ValueError("job needs source_job_id or url")
    existing_id = _existing_job_id(conn, job, canonical)
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
        conn.commit()
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
    conn.commit()
    return UpsertResult(existing_id, False, True)


def upsert_jobs(conn: sqlite3.Connection, jobs: Iterable[JobInput]) -> tuple[int, int]:
    inserted = updated = 0
    for job in jobs:
        result = upsert_job(conn, job)
        inserted += int(result.inserted)
        updated += int(result.updated)
    return inserted, updated


def next_queued_jobs(conn: sqlite3.Connection, *, limit: int = 10) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            """
            SELECT * FROM jobs
            WHERE status = 'queued' AND canonical_url IS NOT NULL
              AND NOT EXISTS (
                SELECT 1 FROM application_runs
                WHERE application_runs.job_id = jobs.id
                  AND application_runs.status IN ('dry_run_ready', 'needs_review', 'blocked')
              )
            ORDER BY posted_at DESC NULLS LAST, first_seen_at ASC, id ASC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    )


def next_backlog_jobs(conn: sqlite3.Connection, *, limit: int = 10) -> list[sqlite3.Row]:
    return next_queued_jobs(conn, limit=limit)


def job_application_url(row: sqlite3.Row | dict[str, object]) -> str | None:
    value = row["canonical_url"] if isinstance(row, sqlite3.Row) else row.get("canonical_url")
    return str(value) if value else None


def count_backlog(conn: sqlite3.Connection) -> dict[str, int]:
    total = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
    pending = conn.execute(
        """
        SELECT COUNT(*) FROM jobs
        WHERE status = 'queued'
          AND NOT EXISTS (
            SELECT 1 FROM application_runs
            WHERE application_runs.job_id = jobs.id
              AND application_runs.status IN ('dry_run_ready', 'needs_review', 'blocked')
          )
        """
    ).fetchone()[0]
    return {"total": int(total), "pending": int(pending)}
