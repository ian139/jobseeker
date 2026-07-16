import sqlite3

import pytest

from jobs_assistant.backlog import (
    BACKLOG_PUBLIC_FIELDS,
    MAX_BACKLOG_LIMIT,
    MAX_BACKLOG_OFFSET,
    MAX_BACKLOG_SOURCE_CHARS,
    MAX_ARCHIVE_JOB_IDS,
    BacklogArchiveConflictError,
    BacklogArchiveError,
    archive_queued_jobs,
    canonicalize_url,
    count_backlog,
    list_backlog_jobs,
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


def _seed_backlog_query_rows(conn):
    rows = (
        ("feed", "queued-new", "2026-03-01", "2026-01-03", "queued"),
        ("feed", "queued-tie-first", "2026-02-01", "2026-01-02", "queued"),
        ("feed", "queued-tie-last", "2026-02-01", "2026-01-04", "queued"),
        ("feed", "queued-null", None, "2026-01-05", "queued"),
        ("feed", "in-progress", "2026-04-01", "2026-01-06", "in_progress"),
        ("feed", "archived", "2026-05-01", "2026-01-07", "archived"),
        ("other-feed", "other-queued", "2026-06-01", "2026-01-08", "queued"),
    )
    job_ids = {}
    for source, source_job_id, posted_at, first_seen_at, status in rows:
        result = upsert_job(
            conn,
            JobInput(
                source=source,
                source_job_id=source_job_id,
                url=f"https://a.test/{source_job_id}",
                title=source_job_id,
                company="A",
                posted_at=posted_at,
            ),
        )
        job_ids[source_job_id] = result.job_id
        conn.execute(
            "UPDATE jobs SET status=?, first_seen_at=?, last_seen_at=? WHERE id=?",
            (status, first_seen_at, first_seen_at, result.job_id),
        )
    conn.commit()
    return job_ids


def test_list_backlog_jobs_filters_all_statuses_and_orders_stably():
    conn = memory_db()
    _seed_backlog_query_rows(conn)

    for status, expected in (
        ("queued", ["queued-new", "queued-tie-first", "queued-tie-last", "queued-null"]),
        ("in_progress", ["in-progress"]),
        ("archived", ["archived"]),
    ):
        rows, counts = list_backlog_jobs(conn, status=status, source="feed", limit=10, offset=0)
        assert [row["source_job_id"] for row in rows] == expected
        assert counts == {"total": 6, "pending": 4}
        assert [*rows[0]] == list(BACKLOG_PUBLIC_FIELDS)

    page, counts = list_backlog_jobs(conn, status="queued", source="feed", limit=2, offset=1)
    assert [row["source_job_id"] for row in page] == ["queued-tie-first", "queued-tie-last"]
    assert counts == {"total": 6, "pending": 4}

    literal, literal_counts = list_backlog_jobs(
        conn,
        status="queued",
        source="feed' OR 1=1 --",
        limit=10,
        offset=0,
    )
    assert literal == []
    assert literal_counts == {"total": 0, "pending": 0}


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"status": "done"}, "status"),
        ({"status": None}, "status"),
        ({"source": 1}, "source"),
        ({"source": ""}, "source"),
        ({"source": "   "}, "source"),
        ({"source": "x" * (MAX_BACKLOG_SOURCE_CHARS + 1)}, "source"),
        ({"limit": 0}, "limit"),
        ({"limit": MAX_BACKLOG_LIMIT + 1}, "limit"),
        ({"limit": True}, "limit"),
        ({"offset": -1}, "offset"),
        ({"offset": MAX_BACKLOG_OFFSET + 1}, "offset"),
        ({"offset": True}, "offset"),
    ],
)
def test_list_backlog_jobs_rejects_invalid_parameters_before_query(kwargs, message):
    conn = sqlite3.connect(":memory:")
    with pytest.raises(ValueError, match=message):
        list_backlog_jobs(conn, **kwargs)


def test_list_backlog_jobs_is_read_only_and_counts_full_unfiltered_backlog():
    conn = memory_db()
    _seed_backlog_query_rows(conn)
    before = conn.execute("SELECT source_job_id, status FROM jobs ORDER BY id").fetchall()
    conn.execute("BEGIN")
    conn.execute("UPDATE jobs SET title='caller pending' WHERE source_job_id='queued-new'")
    changes_after_caller_write = conn.total_changes

    rows, counts = list_backlog_jobs(conn, status="queued", limit=1, offset=0)

    assert [row["source_job_id"] for row in rows] == ["other-queued"]
    assert counts == {"total": 7, "pending": 5}
    assert conn.in_transaction is True
    assert conn.total_changes == changes_after_caller_write
    conn.rollback()
    assert conn.execute("SELECT title FROM jobs WHERE source_job_id='queued-new'").fetchone()["title"] == "queued-new"
    assert conn.execute("SELECT source_job_id, status FROM jobs ORDER BY id").fetchall() == before

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
