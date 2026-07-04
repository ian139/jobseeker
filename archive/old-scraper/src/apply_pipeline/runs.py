from __future__ import annotations

import json
import sqlite3
from typing import Any

from .contracts import PageSnapshot, ResolverOutput, StepStatus, to_jsonable

TERMINAL_STATUSES = {
    StepStatus.DRY_RUN_READY.value,
    StepStatus.NEEDS_REVIEW.value,
    StepStatus.BLOCKED.value,
    StepStatus.FAILED.value,
}


def start_application_run(connection: sqlite3.Connection, *, job_id: int, started_at: str) -> int:
    row = connection.execute("SELECT id FROM jobs WHERE id = ?", (job_id,)).fetchone()
    if row is None:
        raise ValueError(f"Unknown job_id: {job_id}")
    cursor = connection.execute(
        """
        INSERT INTO application_runs (job_id, status, reason, started_at, actions_json)
        VALUES (?, 'failed', 'run started', ?, '[]')
        """,
        (job_id, started_at),
    )
    return int(cursor.lastrowid)


def finish_application_run(
    connection: sqlite3.Connection,
    *,
    run_id: int,
    status: StepStatus | str,
    reason: str,
    finished_at: str,
    final_url: str | None = None,
    actions: list[dict[str, Any]] | tuple[Any, ...] = (),
) -> None:
    status_value = status.value if isinstance(status, StepStatus) else status
    if status_value not in TERMINAL_STATUSES:
        raise ValueError(f"Invalid terminal application status: {status_value}")
    payload = json.dumps(to_jsonable(list(actions)), sort_keys=True)
    cursor = connection.execute(
        """
        UPDATE application_runs
        SET status = ?, reason = ?, finished_at = ?, final_url = ?, actions_json = ?
        WHERE id = ?
        """,
        (status_value, reason, finished_at, final_url, payload, run_id),
    )
    if cursor.rowcount != 1:
        raise ValueError(f"Unknown application run_id: {run_id}")


def record_application_page(
    connection: sqlite3.Connection,
    *,
    run_id: int,
    page_index: int,
    url: str,
    snapshot: PageSnapshot,
    created_at: str,
    resolver_output: ResolverOutput | None = None,
) -> int:
    if page_index < 0:
        raise ValueError("page_index must be non-negative")
    snapshot_json = json.dumps(to_jsonable(snapshot), sort_keys=True)
    resolver_json = json.dumps(to_jsonable(resolver_output), sort_keys=True) if resolver_output is not None else None
    cursor = connection.execute(
        """
        INSERT INTO application_pages (run_id, page_index, url, snapshot_json, resolver_json, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (run_id, page_index, url, snapshot_json, resolver_json, created_at),
    )
    return int(cursor.lastrowid)
