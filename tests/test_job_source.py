from jobs_assistant.db import connect, init_db
from jobs_assistant.job_source import import_source_jobs, normalize_source_job, source_job_to_input


def test_source_job_normalization_stays_at_ingestion_boundary():
    source = normalize_source_job({"id": 7, "title": "Dev", "company": {"name": "Acme"}, "apply_url": "https://a.test/apply"})
    job = source_job_to_input(source)
    assert job.source == "job_source"
    assert job.source_job_id == "7"
    assert job.company == "Acme"
    assert job.url == "https://a.test/apply"


def test_import_source_jobs_dedupes_by_external_id():
    conn = connect(":memory:")
    init_db(conn)
    first = {"id": "1", "title": "Dev", "company": "Acme", "apply_url": "https://a.test/one"}
    second = {"id": "1", "title": "Dev II", "company": "Acme", "apply_url": "https://a.test/two"}
    assert import_source_jobs(conn, [first]) == (1, 1, 0)
    assert import_source_jobs(conn, [second]) == (1, 0, 1)
    assert conn.execute("SELECT title FROM jobs").fetchone()["title"] == "Dev II"
