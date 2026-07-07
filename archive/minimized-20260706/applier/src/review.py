from __future__ import annotations

import sqlite3


def sample_failures(conn: sqlite3.Connection, *, limit: int = 20) -> list[dict[str, object]]:
    rows = conn.execute(
        """
        SELECT status, reason, COUNT(*) AS count, MIN(id) AS sample_run_id
        FROM application_runs
        WHERE status IN ('needs_review', 'blocked', 'failed')
        GROUP BY status, reason
        ORDER BY count DESC, sample_run_id ASC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    return [dict(row) for row in rows]
