from jobs_assistant.contracts import ActionAttempt, RunStatus
from jobs_assistant.db import connect, finish_application_run, init_db, record_application_page, start_application_run
from jobs_assistant.backlog import upsert_job
from jobs_assistant.contracts import JobInput


def test_application_run_and_page_persistence():
    conn = connect(":memory:")
    init_db(conn)
    job = upsert_job(conn, JobInput(source="feed", source_job_id="1", url="https://a.test", title="Dev", company="A"))
    run_id = start_application_run(conn, job.job_id)
    record_application_page(conn, run_id, 0, url="https://a.test", snapshot_json="{}", resolver_json="{}")
    finish_application_run(conn, run_id, status=RunStatus.NEEDS_REVIEW, reason="unknown", final_url="https://a.test", actions=[ActionAttempt("fill", "name", "Ian", True)])
    run = conn.execute("SELECT status, reason, actions_json FROM application_runs WHERE id=?", (run_id,)).fetchone()
    page = conn.execute("SELECT url FROM application_pages WHERE run_id=?", (run_id,)).fetchone()
    assert run["status"] == "needs_review"
    assert "name" in run["actions_json"]
    assert page["url"] == "https://a.test"
