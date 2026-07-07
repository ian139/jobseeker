from pathlib import Path

import jobs_assistant.cli as cli_mod

from jobs_assistant.cli import main


def test_cli_help_smoke(capsys):
    assert main([]) == 0
    out = capsys.readouterr().out
    assert "jobs-assistant" in out
    assert "import-feed" in out
    assert "dry-run-static" not in out


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
