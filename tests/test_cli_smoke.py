from pathlib import Path

from jobs_assistant.cli import main
from jobs_assistant.backlog import upsert_job
from jobs_assistant.contracts import JobInput
from jobs_assistant.db import connect, init_db


def test_cli_help_smoke(capsys):
    assert main([]) == 0
    assert "jobs-assistant" in capsys.readouterr().out


def test_cli_init_db(tmp_path: Path):
    db = tmp_path / "jobs.sqlite3"
    assert main(["--db", str(db), "init-db"]) == 0
    assert db.exists()


def test_cli_dry_run_static(tmp_path: Path, capsys):
    db = tmp_path / "jobs.sqlite3"
    conn = connect(db)
    init_db(conn)
    job = upsert_job(conn, JobInput(source="feed", source_job_id="1", url="https://a.test", title="Dev", company="A"))
    html = tmp_path / "final.html"
    html.write_text('<button type="submit">Submit application</button>')
    resume = tmp_path / "resume.pdf"
    resume.write_text("pdf")
    assert main(["--db", str(db), "dry-run-static", "--job-id", str(job.job_id), "--html", str(html), "--resume", str(resume)]) == 0
    assert '"status": "dry_run_ready"' in capsys.readouterr().out


def test_cli_live_smoke_reports_status(capsys, tmp_path: Path):
    db = tmp_path / "jobs.sqlite3"
    assert main(["--db", str(db), "live-smoke"]) in {0, 1}
    assert '"status":' in capsys.readouterr().out
    assert not db.exists()
