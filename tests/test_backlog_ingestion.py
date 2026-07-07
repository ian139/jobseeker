from jobs_assistant.backlog import canonicalize_url, next_queued_jobs, upsert_job
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
