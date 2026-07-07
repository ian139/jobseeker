import json

import pytest

from pathlib import Path

import jobs_assistant.cli as cli_mod

from jobs_assistant.cli import job_scrape_main, main


def test_cli_help_smoke(capsys):
    assert main([]) == 0
    out = capsys.readouterr().out
    assert "jobs-assistant" in out
    assert "import-feed" in out
    assert "dry-run-static" not in out


def test_job_scrape_help_smoke(capsys):
    with pytest.raises(SystemExit) as exc:
        job_scrape_main(["--help"])

    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "job-scrape" in out
    assert "--source-profile" in out
    assert "--profile" in out
    assert "--paid-fetch" in out


def test_cli_init_db(tmp_path: Path):
    db = tmp_path / "jobs.sqlite3"
    assert main(["--db", str(db), "init-db"]) == 0
    assert db.exists()


def test_cli_import_feed_json_fixture(tmp_path: Path, capsys):
    db = tmp_path / "jobs.sqlite3"
    fixture = tmp_path / "jobs.json"
    fixture.write_text('{"jobs":[{"id":"1","title":"Software Engineer","company":"Acme","apply_url":"https://jobs.example.com/1"}]}')
    assert main(["--db", str(db), "import-feed", "--json-file", str(fixture)]) == 0
    assert capsys.readouterr().out.strip() == '{"inserted": 1, "seen": 1, "updated": 0}'


def test_cli_import_feed_uses_env_base_url(tmp_path: Path, capsys, monkeypatch):
    db = tmp_path / "jobs.sqlite3"
    calls: list[tuple[str, str | None]] = []

    def fake_fetch_source_jobs(base_url: str, api_key: str | None = None):
        calls.append((base_url, api_key))
        return [{"id": "env-1", "title": "Backend Engineer", "company": "Acme", "apply_url": "https://jobs.example.com/env-1"}]

    monkeypatch.setenv("JOB_SOURCE_BASE_URL", "https://feed.example.test")
    monkeypatch.setenv("JOB_SOURCE_API_KEY", "secret")
    monkeypatch.setattr(cli_mod, "fetch_source_jobs", fake_fetch_source_jobs)

    assert main(["--db", str(db), "import-feed"]) == 0
    assert calls == [("https://feed.example.test", "secret")]
    assert capsys.readouterr().out.strip() == '{"inserted": 1, "seen": 1, "updated": 0}'


class FakeTheirStackClient:
    """Fake TheirStackClient that returns a canned response without HTTP."""

    def __init__(self, response: dict):
        self._response = response
        self.payloads: list[dict] = []

    def search_jobs(self, payload: dict) -> dict:
        self.payloads.append(payload)
        return self._response


def test_cli_theirstack_preview_prints_total_results(tmp_path: Path, capsys, monkeypatch):
    """theirstack-preview prints profile, total_results, and credit_safe flag."""
    db = tmp_path / "jobs.sqlite3"
    monkeypatch.setenv("THEIRSTACK_API_KEY", "test-key")
    fake = FakeTheirStackClient({"total_results": 42})
    monkeypatch.setattr(cli_mod, "_theirstack_client", lambda *, paid_fetch: fake)

    assert main(["--db", str(db), "theirstack-preview", "--source-profile", "new_grad_cs"]) == 0
    out = capsys.readouterr().out.strip()
    assert '"total_results": 42' in out
    assert '"profile": "new_grad_cs"' in out
    assert '"credit_safe": true' in out


def test_cli_theirstack_sync_persists_jobs_and_sync_run(tmp_path: Path, capsys, monkeypatch):
    """theirstack-sync --paid-fetch persists jobs and records a sync_run."""
    db = tmp_path / "jobs.sqlite3"
    monkeypatch.setenv("THEIRSTACK_API_KEY", "test-key")
    fake = FakeTheirStackClient({"data": [
        {"id": "s1", "title": "SWE Intern", "company_name": "Acme", "url": "https://a.test/s1"},
        {"id": "s2", "title": "Data Intern", "company_name": "Beta", "url": "https://b.test/s2"},
    ]})
    monkeypatch.setattr(cli_mod, "_theirstack_client", lambda *, paid_fetch: fake)

    assert main(["--db", str(db), "theirstack-sync", "--paid-fetch", "--source-profile", "fall_coop_swe_data"]) == 0
    out = capsys.readouterr().out.strip()
    assert '"inserted": 2' in out
    assert '"seen": 2' in out

    # Verify jobs table
    from jobs_assistant.db import connect
    conn = connect(str(db))
    row = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()
    assert row[0] == 2
    # Verify sync_runs table
    run_row = conn.execute("SELECT source, profile, mode, success, jobs_inserted FROM sync_runs").fetchone()
    assert run_row["source"] == "theirstack"
    assert run_row["profile"] == "fall_coop_swe_data"
    assert run_row["mode"] == "paid_fetch"
    assert run_row["success"] == 1
    assert run_row["jobs_inserted"] == 2


def test_job_scrape_persists_source_profile_and_mode(tmp_path: Path, capsys, monkeypatch):
    db = tmp_path / "jobs.sqlite3"
    monkeypatch.setenv("THEIRSTACK_API_KEY", "test-key")
    fake = FakeTheirStackClient({"data": [
        {"id": "js1", "title": "SWE Intern", "company_name": "Acme", "url": "https://a.test/js1"},
    ]})
    monkeypatch.setattr(cli_mod, "_theirstack_client", lambda *, paid_fetch: fake)

    assert job_scrape_main(["--db", str(db), "--paid-fetch", "--profile", "new_grad_cs", "--count", "1"]) == 0

    assert json.loads(capsys.readouterr().out) == {"count": 1, "source_profile": "new_grad_cs", "inserted": 1, "seen": 1, "updated": 0}
    from jobs_assistant.db import connect
    conn = connect(str(db))
    run_row = conn.execute("SELECT source, profile, mode, success, jobs_inserted FROM sync_runs").fetchone()
    assert run_row["source"] == "theirstack"
    assert run_row["profile"] == "new_grad_cs"
    assert run_row["mode"] == "job_scrape"
    assert run_row["success"] == 1
    assert run_row["jobs_inserted"] == 1


def test_cli_theirstack_sync_refuses_without_paid_flag(tmp_path: Path, capsys, monkeypatch):
    """theirstack-sync without --paid-fetch or env exits non-zero."""
    db = tmp_path / "jobs.sqlite3"
    monkeypatch.setenv("THEIRSTACK_API_KEY", "test-key")
    monkeypatch.delenv("THEIRSTACK_ENABLE_PAID_FETCH", raising=False)

    with pytest.raises(SystemExit) as exc:
        main(["--db", str(db), "theirstack-sync", "--profile", "new_grad_cs"])
    assert exc.value.code != 0


def test_job_scrape_refuses_without_paid_fetch(tmp_path: Path, monkeypatch):
    db = tmp_path / "jobs.sqlite3"
    monkeypatch.setenv("THEIRSTACK_API_KEY", "test-key")
    monkeypatch.delenv("THEIRSTACK_ENABLE_PAID_FETCH", raising=False)

    with pytest.raises(SystemExit) as exc:
        job_scrape_main(["--db", str(db), "--source-profile", "new_grad_cs"])

    assert exc.value.code != 0


def test_cli_autofill_help_documents_no_final_submit_and_supported_flags(capsys):
    """autofill help exposes guarded Greenhouse workflow flags and stop-before-submit safety."""
    with pytest.raises(SystemExit) as exc:
        main(["autofill", "--help"])

    assert exc.value.code == 0
    out = capsys.readouterr().out.lower()
    assert "--ats" in out
    assert "auto" in out
    assert "greenhouse" in out
    assert "--profile-json" in out
    assert "--application-profile-json" in out
    assert "--artifact-dir" in out
    assert "--headed" in out
    assert "no-final-submit" in out


@pytest.mark.parametrize(
    ("ats", "profile_flag"),
    [
        ("auto", "--profile-json"),
        ("greenhouse", "--application-profile-json"),
    ],
)
def test_cli_autofill_passes_guarded_workflow_kwargs_without_browser(tmp_path: Path, capsys, monkeypatch, ats: str, profile_flag: str):
    """autofill parses workflow flags and calls the runner with those kwargs without launching Puppeteer."""
    db = tmp_path / "jobs.sqlite3"
    resume_dir = tmp_path / "resume"
    profile_json = tmp_path / "profile.json"
    artifact_dir = tmp_path / "artifacts"
    resume_dir.mkdir()
    profile_json.write_text('{"name": "Explicit Profile"}')
    calls: list[dict[str, object]] = []

    async def fake_run_browser_autofill(conn, **kwargs):
        calls.append(kwargs)
        return [{"status": "manual", "reason": "fake guarded run"}]

    monkeypatch.setattr(cli_mod, "run_browser_autofill", fake_run_browser_autofill)

    assert main([
        "--db", str(db),
        "autofill",
        "--limit", "2",
        "--resume-dir", str(resume_dir),
        profile_flag, str(profile_json),
        "--artifact-dir", str(artifact_dir),
        "--ats", ats,
        "--headed",
    ]) == 0

    assert calls == [{
        "limit": 2,
        "resume_dir": str(resume_dir),
        "application_profile_json": str(profile_json),
        "artifact_dir": str(artifact_dir),
        "ats": ats,
        "headed": True,
    }]
    assert json.loads(capsys.readouterr().out) == {
        "results": [{"status": "manual", "reason": "fake guarded run"}],
    }
