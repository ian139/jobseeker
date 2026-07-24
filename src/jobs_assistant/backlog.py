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

MAX_ARCHIVE_JOB_IDS = 100

BACKLOG_STATUSES = frozenset({"queued", "in_progress", "archived"})
MAX_BACKLOG_SOURCE_CHARS = 128
MAX_BACKLOG_LIMIT = 100
MAX_BACKLOG_OFFSET = 100_000
BACKLOG_PUBLIC_FIELDS = (
    "id",
    "source",
    "source_job_id",
    "canonical_url",
    "title",
    "company",
    "location",
    "remote",
    "posted_at",
    "discovered_at",
    "status",
)


class BacklogArchiveError(ValueError):
    """A queued-backlog archive request was rejected."""


class BacklogArchiveConflictError(BacklogArchiveError):
    """A requested backlog row was missing or not queued."""


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
        job_id = cur.lastrowid
        if job_id is None:
            raise RuntimeError("job id unavailable")
        return UpsertResult(job_id, True, False)
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


def _validate_backlog_list_params(
    *,
    status: object,
    source: object,
    limit: object,
    offset: object,
) -> None:
    if type(status) is not str or status not in BACKLOG_STATUSES:
        raise ValueError("backlog status must be one of archived, in_progress, queued")
    if source is not None and (
        type(source) is not str
        or not source.strip()
        or len(source) > MAX_BACKLOG_SOURCE_CHARS
    ):
        raise ValueError(
            f"backlog source must be a non-empty string of at most {MAX_BACKLOG_SOURCE_CHARS} characters"
        )
    if type(limit) is not int or not 1 <= limit <= MAX_BACKLOG_LIMIT:
        raise ValueError(f"backlog limit must be between 1 and {MAX_BACKLOG_LIMIT}")
    if type(offset) is not int or not 0 <= offset <= MAX_BACKLOG_OFFSET:
        raise ValueError(f"backlog offset must be between 0 and {MAX_BACKLOG_OFFSET}")


def list_backlog_jobs(
    conn: sqlite3.Connection,
    *,
    status: str = "queued",
    source: str | None = None,
    limit: int = 25,
    offset: int = 0,
) -> tuple[list[dict[str, object]], dict[str, int]]:
    """Read a deterministic, filtered page of public backlog fields.

    The returned counts cover every status in the optional source scope, while
    ``pending`` always counts queued rows.  This keeps listing metadata stable
    when the requested page status changes and performs no database mutation.
    """
    _validate_backlog_list_params(status=status, source=source, limit=limit, offset=offset)
    where_sql = "status = ?"
    query_params: list[object] = [status]
    if source is not None:
        where_sql += " AND source = ?"
        query_params.append(source)

    count_sql = "SELECT COUNT(*) AS total, SUM(status = 'queued') AS pending FROM jobs"
    if source is None:
        counts = count_backlog(conn)
    else:
        count_row = conn.execute(f"{count_sql} WHERE source = ?", (source,)).fetchone()
        counts = {"total": int(count_row[0]), "pending": int(count_row[1] or 0)}

    rows = conn.execute(
        f"""
        SELECT {", ".join(BACKLOG_PUBLIC_FIELDS)}
        FROM jobs
        WHERE {where_sql}
        ORDER BY posted_at DESC NULLS LAST, first_seen_at ASC, id ASC
        LIMIT ? OFFSET ?
        """,
        (*query_params, limit, offset),
    ).fetchall()
    return (
        [
            {field: row[index] for index, field in enumerate(BACKLOG_PUBLIC_FIELDS)}
            for row in rows
        ],
        counts,
    )




def count_backlog(conn: sqlite3.Connection) -> dict[str, int]:
    counts = conn.execute(
        "SELECT COUNT(*) AS total, SUM(status = 'queued') AS pending FROM jobs"
    ).fetchone()
    return {"total": int(counts[0]), "pending": int(counts[1] or 0)}


def _validated_archive_ids(job_ids: Iterable[int]) -> tuple[int, ...]:
    if isinstance(job_ids, (str, bytes)):
        raise BacklogArchiveError("job IDs must be positive integers")
    try:
        values = tuple(job_ids)
    except TypeError as exc:
        raise BacklogArchiveError("job IDs must be positive integers") from exc
    if not values:
        raise BacklogArchiveError("job IDs must not be empty")
    if len(values) > MAX_ARCHIVE_JOB_IDS:
        raise BacklogArchiveError(f"job IDs must contain at most {MAX_ARCHIVE_JOB_IDS} entries")
    if any(type(value) is not int or value <= 0 for value in values):
        raise BacklogArchiveError("job IDs must be positive integers")
    if len(set(values)) != len(values):
        raise BacklogArchiveError("job IDs must be unique")
    return tuple(sorted(values))


def archive_queued_jobs(conn: sqlite3.Connection, job_ids: Iterable[int]) -> tuple[int, ...]:
    """Atomically archive exactly the requested queued jobs.

    Every requested row must exist and still be queued.  The compare-and-set
    update is scoped to ``status = 'queued'`` and is rolled back in full when
    any requested row is missing or has another status.
    """
    ids = _validated_archive_ids(job_ids)
    placeholders = ",".join("?" for _ in ids)
    owns_transaction = not conn.in_transaction
    savepoint = "_jobs_assistant_archive_queued"
    if owns_transaction:
        conn.execute("BEGIN IMMEDIATE")
    else:
        conn.execute(f"SAVEPOINT {savepoint}")
    try:
        rows = conn.execute(
            f"SELECT id, status FROM jobs WHERE id IN ({placeholders})",
            ids,
        ).fetchall()
        if len(rows) != len(ids):
            raise BacklogArchiveConflictError("requested job ID was not found")
        if any(row[1] != "queued" for row in rows):
            raise BacklogArchiveConflictError("requested job is not queued")
        changed = conn.execute(
            f"UPDATE jobs SET status = 'archived' WHERE status = 'queued' AND id IN ({placeholders})",
            ids,
        ).rowcount
        if changed != len(ids):
            raise BacklogArchiveConflictError("queued job state changed")
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
    return ids
