from __future__ import annotations

import sqlite3
from typing import Any


def next_backlog_jobs(connection: sqlite3.Connection, *, limit: int = 10) -> list[sqlite3.Row]:
    return list(
        connection.execute(
            """
            SELECT jobs.*
            FROM jobs
            WHERE NOT EXISTS (
                SELECT 1
                FROM application_runs
                WHERE application_runs.job_id = jobs.id
                  AND application_runs.status IN ('dry_run_ready', 'needs_review', 'blocked')
            )
            ORDER BY jobs.discovered_at DESC, jobs.id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    )


def job_application_url(row: sqlite3.Row | dict[str, Any]) -> str | None:
    raw_value = row["canonical_url"] if isinstance(row, sqlite3.Row) else row.get("canonical_url")
    return str(raw_value) if raw_value else None
