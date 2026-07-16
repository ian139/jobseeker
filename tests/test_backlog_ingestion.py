import sqlite3

import pytest

from jobs_assistant.backlog import (
    MAX_ARCHIVE_JOB_IDS,
    BacklogArchiveConflictError,
    BacklogArchiveError,
    archive_queued_jobs,
    canonicalize_url,
    count_backlog,
    next_queued_jobs,
    upsert_job,
    upsert_jobs,
)
from jobs_assistant.contracts import JobInput
from jobs_assistant.db import connect, init_db


def memory_db():
    conn = connect(":memory:")
    init_db(conn)
    return conn


def test_canonicalize_url_removes_tracking_and_fragment():
    assert canonicalize_url("HTTPS://Example.com/apply/?utm_source=x&job=1#frag") == "https://example.com/apply?job=1"


def test_upsert_dedupes_by_source_job_id():
    conn = memory_db()
    first = upsert_job(conn, JobInput(source="theirstack", source_job_id="j1", url="https://a.test/apply", title="Dev", company="A"))
    second = upsert_job(conn, JobInput(source="theirstack", source_job_id="j1", url="https://a.test/apply?utm_source=x", title="Dev II", company="A"))
    row = conn.execute("SELECT title, COUNT(*) OVER () AS total FROM jobs").fetchone()
    assert first.inserted is True
    assert second.updated is True
    assert second.job_id == first.job_id
    assert row["title"] == "Dev II"
    assert row["total"] == 1


def test_upsert_dedupes_by_canonical_url_without_source_id():
    conn = memory_db()
    first = upsert_job(conn, JobInput(source="feed", source_job_id=None, url="https://a.test/jobs/1/", title="Dev", company="A"))
    second = upsert_job(conn, JobInput(source="feed", source_job_id=None, url="https://a.test/jobs/1?gclid=x", title="Dev", company="A"))
    assert first.job_id == second.job_id
    assert conn.execute("SELECT COUNT(*) AS c FROM jobs").fetchone()["c"] == 1


def test_next_queued_jobs_returns_queued_jobs_without_applier_state():
    conn = memory_db()
    upsert_job(conn, JobInput(source="feed", source_job_id="1", url="https://a.test/apply", title="Dev", company="A", posted_at="2026-01-01"))
    assert [row["source_job_id"] for row in next_queued_jobs(conn, limit=1)] == ["1"]


def test_count_backlog_supports_raw_sqlite_connections():
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE jobs (status TEXT)")
    conn.executemany("INSERT INTO jobs(status) VALUES (?)", [("queued",), ("done",)])
    assert count_backlog(conn) == {"total": 2, "pending": 1}


def test_upsert_jobs_rolls_back_when_a_later_job_is_invalid():
    conn = memory_db()

    jobs = iter(
        [
            JobInput(source="feed", source_job_id="first", url="https://a.test/first", title="First", company="A"),
            JobInput(source="feed", source_job_id=None, url=None, title="Invalid", company="A"),
        ]
    )
    with pytest.raises(ValueError, match="source_job_id or url"):
        upsert_jobs(conn, jobs)

    assert count_backlog(conn) == {"total": 0, "pending": 0}
    assert conn.in_transaction is False


def test_upsert_jobs_reports_inserted_and_updated_counts():
    conn = memory_db()
    upsert_job(conn, JobInput(source="feed", source_job_id="existing", url="https://a.test/existing", title="Old", company="A"))

    assert upsert_jobs(
        conn,
        [
            JobInput(source="feed", source_job_id="existing", url="https://a.test/existing", title="New", company="A"),
            JobInput(source="feed", source_job_id="new", url="https://a.test/new", title="New", company="B"),
        ],
    ) == (1, 1)
    rows = conn.execute("SELECT source_job_id, title FROM jobs ORDER BY source_job_id").fetchall()
    assert [(row["source_job_id"], row["title"]) for row in rows] == [("existing", "New"), ("new", "New")]


def test_upsert_jobs_rollback_is_bounded_by_caller_transaction():
    conn = memory_db()
    conn.execute("CREATE TABLE caller_state (value TEXT NOT NULL)")
    conn.commit()
    conn.execute("BEGIN")
    conn.execute("INSERT INTO caller_state(value) VALUES ('keep')")

    with pytest.raises(ValueError, match="source_job_id or url"):
        upsert_jobs(
            conn,
            [
                JobInput(source="feed", source_job_id="first", url="https://a.test/first", title="First", company="A"),
                JobInput(source="feed", source_job_id=None, url=None, title="Invalid", company="A"),
            ],
        )

    assert conn.in_transaction is True
    assert conn.execute("SELECT value FROM caller_state").fetchone()["value"] == "keep"
    assert count_backlog(conn) == {"total": 0, "pending": 0}
    conn.commit()


def test_upsert_jobs_success_leaves_caller_transaction_open():
    conn = memory_db()
    conn.execute("SAVEPOINT caller_scope")

    assert upsert_jobs(
        conn,
        [JobInput(source="feed", source_job_id="first", url="https://a.test/first", title="First", company="A")],
    ) == (1, 0)
    assert conn.in_transaction is True

    conn.execute("ROLLBACK TO SAVEPOINT caller_scope")
    conn.execute("RELEASE SAVEPOINT caller_scope")
    assert count_backlog(conn) == {"total": 0, "pending": 0}


def test_archive_queued_jobs_archives_url_less_rows_without_deleting_them():
    conn = memory_db()
    queued = upsert_job(conn, JobInput(source="feed", source_job_id="url-less", url=None, title="No URL", company="A"))

    assert archive_queued_jobs(conn, [queued.job_id]) == (queued.job_id,)
    row = conn.execute("SELECT status, canonical_url FROM jobs WHERE id=?", (queued.job_id,)).fetchone()
    assert row["status"] == "archived"
    assert row["canonical_url"] is None
    assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 1


def test_archive_queued_jobs_rolls_back_when_a_later_requested_row_conflicts():
    conn = memory_db()
    first = upsert_job(conn, JobInput(source="feed", source_job_id="first", url=None, title="First", company="A"))
    second = upsert_job(conn, JobInput(source="feed", source_job_id="second", url=None, title="Second", company="A"))
    conn.execute("UPDATE jobs SET status='in_progress' WHERE id=?", (second.job_id,))
    conn.commit()

    with pytest.raises(BacklogArchiveConflictError, match="not queued"):
        archive_queued_jobs(conn, [first.job_id, second.job_id])

    statuses = conn.execute("SELECT id, status FROM jobs ORDER BY id").fetchall()
    assert [(row["id"], row["status"]) for row in statuses] == [
        (first.job_id, "queued"),
        (second.job_id, "in_progress"),
    ]
    assert conn.in_transaction is False


@pytest.mark.parametrize(
    ("job_ids", "message"),
    [
        ([], "must not be empty"),
        ([0], "positive integers"),
        ([1, 1], "must be unique"),
        (list(range(1, MAX_ARCHIVE_JOB_IDS + 2)), "at most"),
    ],
)
def test_archive_queued_jobs_rejects_invalid_id_lists_before_mutation(job_ids, message):
    conn = memory_db()

    with pytest.raises(BacklogArchiveError, match=message):
        archive_queued_jobs(conn, job_ids)

    assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0
    assert conn.in_transaction is False
