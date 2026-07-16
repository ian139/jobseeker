from jobs_assistant.db import connect, init_db
from jobs_assistant.job_source import import_source_jobs, normalize_source_job, source_job_to_input


def test_source_job_normalization_stays_at_ingestion_boundary():
    source = normalize_source_job({"id": 7, "title": "Dev", "company": {"name": "Acme"}, "apply_url": "https://a.test/apply"})
    job = source_job_to_input(source)
    assert job.source == "job_source"
    assert job.source_job_id == "7"
    assert job.company == "Acme"
    assert job.url == "https://a.test/apply"



def test_source_job_normalization_extracts_metadata_aliases():
    source = normalize_source_job(
        {
            "id": "metadata",
            "title": "Dev",
            "company": "Acme",
            "apply_url": "https://a.test/apply",
            "job_location": " Toronto ",
            "remote": True,
            "details": {"content": " Build platform services. "},
        }
    )

    assert source.location == "Toronto"
    assert source.remote is True
    assert source.description == "Build platform services."
    job = source_job_to_input(source)
    assert job.location == "Toronto"
    assert job.remote is True
    assert job.description == "Build platform services."


def test_import_source_jobs_persists_and_updates_metadata():
    conn = connect(":memory:")
    init_db(conn)
    first = {
        "id": "metadata",
        "title": "Dev",
        "company": "Acme",
        "apply_url": "https://a.test/one",
        "location": "Toronto",
        "remote": True,
        "description": "First description",
    }
    second = {
        "id": "metadata",
        "title": "Dev II",
        "company": "Acme",
        "apply_url": "https://a.test/two",
        "location": "Montreal",
        "remote": False,
        "description_html": "<p>Updated description</p>",
    }

    assert import_source_jobs(conn, [first]) == (1, 1, 0)
    row = conn.execute("SELECT location, remote, description FROM jobs").fetchone()
    assert (row["location"], row["remote"], row["description"]) == ("Toronto", 1, "First description")

    assert import_source_jobs(conn, [second]) == (1, 0, 1)
    row = conn.execute("SELECT location, remote, description FROM jobs").fetchone()
    assert (row["location"], row["remote"], row["description"]) == ("Montreal", 0, "<p>Updated description</p>")


def test_source_job_normalization_does_not_coerce_malformed_metadata():
    source = normalize_source_job(
        {
            "id": "malformed",
            "title": "Dev",
            "company": "Acme",
            "apply_url": "https://a.test/apply",
            "location": {"city": "Toronto"},
            "remote": "false",
        }
    )

    assert source.location is None
    assert source.remote is None
    job = source_job_to_input(source)
    assert job.location is None
    assert job.remote is None

def test_import_source_jobs_dedupes_by_external_id():
    conn = connect(":memory:")
    init_db(conn)
    first = {"id": "1", "title": "Dev", "company": "Acme", "apply_url": "https://a.test/one"}
    second = {"id": "1", "title": "Dev II", "company": "Acme", "apply_url": "https://a.test/two"}
    assert import_source_jobs(conn, [first]) == (1, 1, 0)
    assert import_source_jobs(conn, [second]) == (1, 0, 1)
    assert conn.execute("SELECT title FROM jobs").fetchone()["title"] == "Dev II"
