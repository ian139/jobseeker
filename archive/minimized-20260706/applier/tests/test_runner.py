from pathlib import Path

from jobs_assistant.backlog import upsert_job
from jobs_assistant.contracts import JobInput, RunStatus
from jobs_assistant.db import connect, init_db
from jobs_assistant.runner import run_static_dry_run


def test_static_runner_records_pages_actions_and_stops_at_submit(tmp_path: Path):
    conn = connect(":memory:")
    init_db(conn)
    resume = tmp_path / "resume.pdf"
    resume.write_text("pdf")
    job = upsert_job(conn, JobInput(source="feed", source_job_id="1", url="https://a.test", title="Dev", company="A"))
    first = '<label for="name">Full name</label><input id="name" required><button type="button">Continue</button>'
    final = '<button type="submit">Submit application</button>'
    run_id, status, actions = run_static_dry_run(conn, job_id=job.job_id, html_pages=[first, final], start_url="https://a.test", facts={"full_name": "Ian"}, resume_path=resume)
    assert status == RunStatus.DRY_RUN_READY
    assert [(a.action, a.success) for a in actions] == [("fill", True), ("click", True)]
    assert conn.execute("SELECT COUNT(*) FROM application_pages WHERE run_id=?", (run_id,)).fetchone()[0] == 2
    run = conn.execute("SELECT status FROM application_runs WHERE id=?", (run_id,)).fetchone()
    assert run["status"] == "dry_run_ready"


def test_static_runner_persists_needs_review_for_unknown_required(tmp_path: Path):
    conn = connect(":memory:")
    init_db(conn)
    resume = tmp_path / "resume.pdf"
    resume.write_text("pdf")
    job = upsert_job(conn, JobInput(source="feed", source_job_id="1", url="https://a.test", title="Dev", company="A"))
    html = '<label for="portfolio">Portfolio</label><input id="portfolio" required>'
    _, status, _ = run_static_dry_run(conn, job_id=job.job_id, html_pages=[html], start_url="https://a.test", facts={}, resume_path=resume)
    assert status == RunStatus.NEEDS_REVIEW
