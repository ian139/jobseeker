from jobs_assistant.review import sample_failures
from jobs_assistant.db import connect, init_db
from jobs_assistant.backlog import upsert_job
from jobs_assistant.contracts import JobInput


def test_sample_failures_groups_terminal_problem_runs():
    conn = connect(":memory:")
    init_db(conn)
    job = upsert_job(conn, JobInput(source="feed", source_job_id="1", url="https://a.test", title="Dev", company="A"))
    conn.executemany("INSERT INTO application_runs (job_id, status, reason, started_at) VALUES (?, ?, ?, 'now')", [
        (job.job_id, "needs_review", "unknown_required:name"),
        (job.job_id, "needs_review", "unknown_required:name"),
        (job.job_id, "blocked", "blocker:captcha"),
    ])
    rows = sample_failures(conn)
    assert rows[0]["status"] == "needs_review"
    assert rows[0]["count"] == 2
