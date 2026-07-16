import httpx
import json

import pytest
from pathlib import Path

import jobs_assistant.cli as cli_mod
from jobs_assistant.backlog import upsert_job
from jobs_assistant.contracts import JobInput
from jobs_assistant.db import connect, init_db

from jobs_assistant.cli import job_scrape_main, main
from jobs_assistant.theirstack import TheirStackClient


def test_application_preferences_cli_edits_atomically_and_redacts_values(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.chdir(tmp_path)
    path = tmp_path / "preferences.json"
    assert main(["application-preferences", "init", str(path)]) == 0
    assert path.stat().st_mode & 0o777 == 0o600
    assert main([
        "application-preferences", "set-mapping", str(path),
        "--ats", "lever", "--kind", "email", "--name", "email",
        "--value", "ada@example.test",
    ]) == 0
    assert path.stat().st_mode & 0o777 == 0o600
    assert main(["application-preferences", "show", str(path)]) == 0
    output = capsys.readouterr().out
    assert "ada@example.test" not in output
    assert "value_hash" in output and "value_length" in output


def test_application_preferences_cli_removes_review_order_atomically(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.chdir(tmp_path)
    path = tmp_path / "preferences.json"
    assert main(["application-preferences", "init", str(path)]) == 0
    assert main([
        "application-preferences", "set-mapping", str(path),
        "--ats", "lever", "--kind", "email", "--name", "email",
        "--value", "ada@example.test",
    ]) == 0
    assert main([
        "application-preferences", "set-review-order", str(path),
        "--ats", "lever", "--kind", "email", "--name", "email",
    ]) == 0
    assert main([
        "application-preferences", "remove-review-order", str(path),
        "--ats", "lever", "--kind", "email", "--name", "email",
    ]) == 0
    assert path.stat().st_mode & 0o777 == 0o600
    assert main(["application-preferences", "show", str(path)]) == 0
    output = capsys.readouterr().out
    shown = json.loads(output.strip().splitlines()[-1])
    assert shown["review_order"] == []
    assert "ada@example.test" not in output
    assert main([
        "application-preferences", "remove-review-order", str(path),
        "--ats", "lever", "--kind", "email", "--name", "email",
    ]) == 1

def test_application_preferences_cli_rejects_symlink_and_conflicting_entries(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    outside = tmp_path / "outside.json"
    outside.write_text("{}")
    link = tmp_path / "link.json"
    link.symlink_to(outside)
    assert main(["application-preferences", "init", str(link)]) == 1
    path = tmp_path / "preferences.json"
    assert main(["application-preferences", "init", str(path)]) == 0
    args = ["application-preferences", "set-opt-out", str(path), "--ats", "lever", "--kind", "email", "--name", "email"]
    assert main(args) == 0
    assert main([
        "application-preferences", "set-mapping", str(path),
        "--ats", "lever", "--kind", "email", "--name", "email", "--value", "ada@example.test",
    ]) == 1


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


def _seed_cli_backlog(db: Path) -> None:
    connection = connect(db)
    init_db(connection)
    connection.executemany(
        """
        INSERT INTO jobs (
            source, source_job_id, canonical_url, title, company, location,
            remote, posted_at, discovered_at, description, raw_json,
            first_seen_at, last_seen_at, status
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                "feed",
                "queued-late",
                "https://jobs.example.test/late",
                "Late",
                "Acme",
                "Toronto",
                1,
                "2026-03-01",
                "2026-01-03",
                "description",
                '{"secret":"hidden"}',
                "2026-01-03",
                "2026-01-03",
                "queued",
            ),
            (
                "feed",
                "queued-early",
                "https://jobs.example.test/early",
                "Early",
                "Acme",
                "Montreal",
                0,
                "2026-02-01",
                "2026-01-02",
                "description",
                '{"secret":"hidden"}',
                "2026-01-02",
                "2026-01-02",
                "queued",
            ),
            (
                "feed",
                "queued-no-date",
                "https://jobs.example.test/no-date",
                "No date",
                "Acme",
                None,
                None,
                None,
                "2026-01-01",
                "description",
                '{"secret":"hidden"}',
                "2026-01-01",
                "2026-01-01",
                "queued",
            ),
            (
                "feed",
                "in-progress",
                "https://jobs.example.test/in-progress",
                "In progress",
                "Acme",
                "Remote",
                1,
                "2026-04-01",
                "2026-01-04",
                "description",
                '{"secret":"hidden"}',
                "2026-01-04",
                "2026-01-04",
                "in_progress",
            ),
            (
                "feed",
                "archived",
                "https://jobs.example.test/archived",
                "Archived",
                "Acme",
                "Remote",
                0,
                "2026-05-01",
                "2026-01-05",
                "description",
                '{"secret":"hidden"}',
                "2026-01-05",
                "2026-01-05",
                "archived",
            ),
        ],
    )
    connection.commit()
    connection.close()


def test_cli_backlog_list_empty_db(tmp_path: Path, capsys) -> None:
    db = tmp_path / "jobs.sqlite3"
    assert main(["--db", str(db), "init-db"]) == 0
    capsys.readouterr()

    assert main(["--db", str(db), "backlog-list"]) == 0
    assert json.loads(capsys.readouterr().out) == {
        "jobs": [],
        "limit": 25,
        "pending": 0,
        "status": "queued",
        "total": 0,
    }

def test_cli_backlog_list_missing_db_fails_without_creating_file(tmp_path: Path, capsys) -> None:
    db = tmp_path / "missing.sqlite3"

    assert not db.exists()
    assert main(["--db", str(db), "backlog-list"]) == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == '{"error": {"code": "database_error", "message": "database operation failed"}}\n'
    assert not db.exists()


def test_cli_backlog_list_filters_orders_and_limits_without_raw_json(tmp_path: Path, capsys) -> None:
    db = tmp_path / "jobs.sqlite3"
    _seed_cli_backlog(db)

    assert main(["--db", str(db), "backlog-list", "--status", "queued", "--limit", "2"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "queued"
    assert payload["limit"] == 2
    assert payload["total"] == 5
    assert payload["pending"] == 3
    assert [job["source_job_id"] for job in payload["jobs"]] == ["queued-late", "queued-early"]
    assert set(payload["jobs"][0]) == {
        "id",
        "source",
        "source_job_id",
        "canonical_url",
        "title",
        "company",
        "location",
        "remote",
        "posted_at",
        "discovered_at",
        "status",
    }
    assert "hidden" not in json.dumps(payload)

    assert main(["backlog-list", "--db", str(db), "--status", "archived"]) == 0
    archived = json.loads(capsys.readouterr().out)
    assert [job["source_job_id"] for job in archived["jobs"]] == ["archived"]
    assert archived["jobs"][0]["status"] == "archived"
    assert archived["pending"] == 3


@pytest.mark.parametrize(
    "option",
    [
        ["--limit", "0"],
        ["--limit", "101"],
        ["--status", "unsupported"],
    ],
)
def test_cli_backlog_list_rejects_invalid_arguments_before_db_work(tmp_path: Path, monkeypatch, option) -> None:
    def fail_connect(*args, **kwargs):
        pytest.fail("database opened before backlog-list argument validation")

    monkeypatch.setattr(cli_mod, "connect", fail_connect)
    with pytest.raises(SystemExit) as exc:
        main(["--db", str(tmp_path / "jobs.sqlite3"), "backlog-list", *option])
    assert exc.value.code == 2


def test_cli_backlog_list_does_not_mutate_database(tmp_path: Path, capsys) -> None:
    db = tmp_path / "jobs.sqlite3"
    _seed_cli_backlog(db)
    before = db.read_bytes()

    assert main(["--db", str(db), "backlog-list", "--status", "in_progress"]) == 0
    capsys.readouterr()
    assert db.read_bytes() == before
    connection = connect(db)
    try:
        assert connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 5
        assert connection.execute("SELECT COUNT(*) FROM jobs WHERE status = 'queued'").fetchone()[0] == 3
    finally:
        connection.close()


def test_cli_init_db_rejects_group_readable_parent_without_leaking_details(tmp_path: Path, capsys):
    parent = tmp_path / "shared"
    parent.mkdir()
    parent.chmod(0o755)
    db = parent / "jobs.sqlite3"

    assert main(["--db", str(db), "init-db"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == '{"error": {"code": "database_privacy_error", "message": "database privacy validation failed"}}\n'
    assert str(db) not in captured.err
    assert "Traceback" not in captured.err
    assert "0755" not in captured.err


def test_cli_init_db_rejects_group_readable_database_without_leaking_details(tmp_path: Path, capsys):
    db = tmp_path / "jobs.sqlite3"
    db.touch()
    db.chmod(0o644)

    assert main(["--db", str(db), "init-db"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == '{"error": {"code": "database_privacy_error", "message": "database privacy validation failed"}}\n'
    assert str(db) not in captured.err
    assert "Traceback" not in captured.err
    assert "0644" not in captured.err

def _corrupt_database(path: Path) -> None:
    path.write_bytes(b"not a sqlite database")
    path.chmod(0o600)


def test_cli_corrupt_db_init_maps_generic_database_error(tmp_path: Path, capsys) -> None:
    db = tmp_path / "corrupt.sqlite3"
    _corrupt_database(db)

    assert main(["--db", str(db), "init-db"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == '{"error": {"code": "database_error", "message": "database operation failed"}}\n'
    assert str(db) not in captured.err
    assert "Traceback" not in captured.err


def test_cli_corrupt_db_import_feed_maps_generic_database_error(tmp_path: Path, capsys) -> None:
    db = tmp_path / "corrupt.sqlite3"
    fixture = tmp_path / "jobs.json"
    _corrupt_database(db)
    fixture.write_text('{"jobs":[]}')

    assert main(["--db", str(db), "import-feed", "--json-file", str(fixture)]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == '{"error": {"code": "database_error", "message": "database operation failed"}}\n'
    assert str(db) not in captured.err
    assert "Traceback" not in captured.err


def test_job_scrape_corrupt_db_maps_generic_database_error(tmp_path: Path, capsys) -> None:
    db = tmp_path / "corrupt.sqlite3"
    _corrupt_database(db)

    assert job_scrape_main(["--db", str(db), "--paid-fetch"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == '{"error": {"code": "database_error", "message": "database operation failed"}}\n'
    assert str(db) not in captured.err
    assert "Traceback" not in captured.err

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


@pytest.mark.parametrize(
    "command",
    [
        ["theirstack-preview"],
        ["theirstack-sync", "--paid-fetch"],
    ],
)
def test_cli_theirstack_commands_without_api_key_are_sanitized(
    command, tmp_path: Path, capsys, monkeypatch
):
    monkeypatch.delenv("THEIRSTACK_API_KEY", raising=False)
    db = tmp_path / "jobs.sqlite3"

    assert main(["--db", str(db), *command]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert json.loads(captured.err) == {
        "error": {
            "code": "invalid_input",
            "message": "autofill input was rejected",
        }
    }

def test_cli_accepts_non_coop_profile_choice_without_network(tmp_path: Path, capsys, monkeypatch):
    db = tmp_path / "jobs.sqlite3"
    monkeypatch.setenv("THEIRSTACK_API_KEY", "test-key")
    fake = FakeTheirStackClient({"total_results": 7})
    monkeypatch.setattr(cli_mod, "_theirstack_client", lambda *, paid_fetch: fake)

    assert main(["--db", str(db), "theirstack-preview", "--source-profile", "new_grad_non_coop_cs"]) == 0
    out = capsys.readouterr().out.strip()
    assert '"profile": "new_grad_non_coop_cs"' in out
    assert '"credit_safe": true' in out
    assert fake.payloads and fake.payloads[0]["limit"] == 1


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


@pytest.mark.parametrize("failure", ["timeout", 429, 500])
def test_cli_paid_sync_transport_failure_is_single_attempt_and_redacted(
    failure, tmp_path: Path, capsys, monkeypatch
):
    db = tmp_path / "jobs.sqlite3"
    requests = []

    def handler(request):
        requests.append(request)
        if failure == "timeout":
            raise httpx.ReadTimeout("private transport detail", request=request)
        return httpx.Response(
            failure,
            json={"detail": "private response detail"},
            request=request,
        )

    http_client = httpx.Client(transport=httpx.MockTransport(handler))

    def factory(*, paid_fetch):
        assert paid_fetch is True
        return TheirStackClient("test-key", enable_paid_fetch=True, client=http_client)

    monkeypatch.setattr(cli_mod, "_theirstack_client", factory)
    try:
        assert main(
            [
                "--db",
                str(db),
                "theirstack-sync",
                "--paid-fetch",
                "--source-profile",
                "new_grad_cs",
            ]
        ) == 1
    finally:
        http_client.close()

    captured = capsys.readouterr()
    assert captured.out == ""
    assert json.loads(captured.err) == {
        "error": {
            "code": "theirstack_error",
            "message": "TheirStack paid sync failed; no jobs were written and the request was not replayed automatically",
        }
    }
    assert "private" not in captured.err

    from jobs_assistant.db import connect

    conn = connect(str(db))
    assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0
    run = conn.execute("SELECT success, checkpoint, jobs_inserted, error FROM sync_runs").fetchone()
    assert run["success"] == 0
    assert run["checkpoint"] is None
    assert run["jobs_inserted"] == 0
    assert run["error"] == "theirstack request failed"
    assert len(requests) == 1



def test_cli_pinned_sync_public_output_hides_checkpoint_namespace(tmp_path: Path, capsys, monkeypatch):
    db = tmp_path / "jobs.sqlite3"
    monkeypatch.setenv("THEIRSTACK_API_KEY", "test-key")
    fake = FakeTheirStackClient(
        {
            "data": [
                {
                    "id": "gh-1",
                    "title": "Engineer",
                    "company_name": "Acme",
                    "url": "https://boards.greenhouse.io/acme/jobs/123",
                }
            ]
        }
    )
    monkeypatch.setattr(cli_mod, "_theirstack_client", lambda *, paid_fetch: fake)

    assert main(
        [
            "--db",
            str(db),
            "theirstack-sync",
            "--paid-fetch",
            "--source-profile",
            "new_grad_cs",
            "--ats",
            "greenhouse",
        ]
    ) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["source_profile"] == "new_grad_cs"
    assert "::ats::" not in json.dumps(output)
    assert "checkpoint_profile" not in output

def test_cli_pinned_sync_refetches_window_without_checkpoint_and_dedupes(tmp_path: Path, capsys, monkeypatch):
    """Pinned syncs repeat the same paid window instead of advancing a checkpoint."""
    db = tmp_path / "jobs.sqlite3"
    monkeypatch.setenv("THEIRSTACK_API_KEY", "test-key")
    fake = FakeTheirStackClient(
        {
            "data": [
                {
                    "id": "gh-1",
                    "title": "Engineer",
                    "company_name": "Acme",
                    "url": "https://boards.greenhouse.io/acme/jobs/123",
                }
            ]
        }
    )
    monkeypatch.setattr(cli_mod, "_theirstack_client", lambda *, paid_fetch: fake)

    args = [
        "--db",
        str(db),
        "theirstack-sync",
        "--paid-fetch",
        "--source-profile",
        "new_grad_cs",
        "--ats",
        "greenhouse",
        "--limit",
        "2",
    ]
    assert main(args) == 0
    first = json.loads(capsys.readouterr().out)
    assert main(args) == 0
    second = json.loads(capsys.readouterr().out)
    assert first["checkpoint_advanced"] is False
    assert second["checkpoint_advanced"] is False

    assert len(fake.payloads) == 2
    assert fake.payloads[0] == fake.payloads[1]
    assert all("discovered_at_gte" not in payload for payload in fake.payloads)
    assert all(payload["limit"] == 2 and payload["page"] == 0 for payload in fake.payloads)
    assert first["inserted"] == 1
    assert second["inserted"] == 0
    assert second["updated"] == 1

    from jobs_assistant.db import connect

    conn = connect(str(db))
    rows = conn.execute(
        "SELECT profile, success, checkpoint FROM sync_runs ORDER BY id"
    ).fetchall()
    assert [row["profile"] for row in rows] == ["new_grad_cs::ats::greenhouse"] * 2
    assert [row["success"] for row in rows] == [1, 1]
    assert [row["checkpoint"] for row in rows] == [None, None]
    assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 1


def test_cli_auto_sync_keeps_incremental_checkpoint(tmp_path: Path, capsys, monkeypatch):
    """Auto mode continues to pass and advance its legacy checkpoint."""
    db = tmp_path / "jobs.sqlite3"
    monkeypatch.setenv("THEIRSTACK_API_KEY", "test-key")
    fake = FakeTheirStackClient(
        {
            "data": [
                {
                    "id": "legacy-1",
                    "title": "Engineer",
                    "company_name": "Acme",
                    "url": "https://arbitrary.example/apply",
                }
            ]
        }
    )
    monkeypatch.setattr(cli_mod, "_theirstack_client", lambda *, paid_fetch: fake)

    args = [
        "--db",
        str(db),
        "theirstack-sync",
        "--paid-fetch",
        "--source-profile",
        "new_grad_cs",
        "--limit",
        "2",
    ]
    assert main(args) == 0
    capsys.readouterr()
    assert main(args) == 0
    capsys.readouterr()

    assert len(fake.payloads) == 2
    assert "discovered_at_gte" not in fake.payloads[0]
    assert fake.payloads[1]["discovered_at_gte"]
    assert fake.payloads[0]["limit"] == fake.payloads[1]["limit"] == 2

    from jobs_assistant.db import connect

    conn = connect(str(db))
    rows = conn.execute(
        "SELECT profile, success, checkpoint FROM sync_runs ORDER BY id"
    ).fetchall()
    assert [row["profile"] for row in rows] == ["new_grad_cs", "new_grad_cs"]
    assert [row["success"] for row in rows] == [1, 1]
    assert all(row["checkpoint"] for row in rows)

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


def test_cli_autofill_help_documents_durable_no_submit_workflow(capsys):
    """Autofill exposes the guarded workflow and supported route selector only."""
    with pytest.raises(SystemExit) as exc:
        main(["autofill", "--help"])

    assert exc.value.code == 0
    out = capsys.readouterr().out.lower()
    assert "--resume-file" in out
    assert "--artifact-root" in out
    assert "--application-profile-json" in out
    assert "--ats {auto,greenhouse,lever}" in out
    assert "--headed" in out
    assert "no-final-submit" in out
    assert "hold-open" not in out
    assert "autofill-review" not in out


def test_cli_autofill_defaults_are_stable():
    args = cli_mod.build_parser().parse_args(["autofill"])
    assert args.resume_file == "resume/Main_Resume.pdf"
    assert args.artifact_root == "data/application-runs"
    assert args.ats == "auto"
    assert args.limit == 1



class _FakeReviewRoot:
    def __init__(self):
        self.opened_refs: list[tuple[str, int]] = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def open_artifact_ref(self, artifact_ref, *, run_id):
        self.opened_refs.append((artifact_ref, run_id))
        return self


class _FakeReviewConnection:
    def __init__(self, artifact_ref: str | None = "run-3"):
        self.artifact_ref = artifact_ref

    def execute(self, sql, params):
        assert sql == "SELECT artifact_dir FROM application_runs WHERE id=?"
        assert len(params) == 1
        return self

    def fetchone(self):
        if self.artifact_ref is None:
            return None
        return {"artifact_dir": self.artifact_ref}
def _review_row(*, run_id: int = 3, status: str = "failed", reason_code: str = "browser_error") -> dict[str, object]:
    return {
        "run_id": run_id,
        "job_id": 7,
        "status": status,
        "reason_code": reason_code,
        "title": "Software Engineer",
        "company": "Acme",
        "artifact_ref": f"run-{run_id}",
        "finished_at": "2026-07-10T00:00:00Z",
        "outcome": None,
        "window_state": "closed",
    }

def test_cli_review_list_uses_public_api_and_exact_schema(tmp_path: Path, capsys, monkeypatch):
    root = _FakeReviewRoot()
    monkeypatch.setattr(cli_mod.ArtifactRoot, "open", lambda *args, **kwargs: root)
    monkeypatch.setattr(cli_mod, "connect", lambda path: object())
    monkeypatch.setattr(cli_mod, "initialize_database", lambda connection, migration_artifact_root: None)
    monkeypatch.setattr(cli_mod, "list_application_reviews", lambda connection, *, limit, artifact_root: [_review_row()])

    assert main([
        "--db", str(tmp_path / "db.sqlite3"),
        "autofill-review", "--artifact-root", str(tmp_path / "artifacts"), "list", "--limit", "1",
    ]) == 0
    assert json.loads(capsys.readouterr().out) == {"runs": [_review_row()]}


def test_cli_review_complete_projects_public_result(tmp_path: Path, capsys, monkeypatch):
    root = _FakeReviewRoot()
    monkeypatch.setattr(cli_mod.ArtifactRoot, "open", lambda *args, **kwargs: root)
    monkeypatch.setattr(cli_mod, "connect", lambda path: _FakeReviewConnection())
    monkeypatch.setattr(cli_mod, "initialize_database", lambda connection, migration_artifact_root: None)
    monkeypatch.setattr(
        cli_mod,
        "complete_review",
        lambda connection, **kwargs: {
            "run_id": 3,
            "job_id": 7,
            "status": "failed",
            "reason_code": "browser_error",
            "outcome": "skipped",
            "job_status": "archived",
            "window_state": "closed",
        },
    )

    assert main([
        "--db", str(tmp_path / "db.sqlite3"),
        "autofill-review", "complete", "--run-id", "3", "--outcome", "skipped",
    ]) == 0
    assert json.loads(capsys.readouterr().out) == {
        "run_id": 3,
        "job_id": 7,
        "status": "failed",
        "reason_code": "browser_error",
        "outcome": "skipped",
        "job_status": "archived",
        "artifact_ref": "run-3",
        "window_state": "closed",
    }


def test_cli_review_annotation_is_persisted_before_cas(tmp_path: Path, capsys, monkeypatch):
    root = _FakeReviewRoot()
    annotation = tmp_path / "annotation.txt"
    annotation.write_text("human review note", encoding="utf-8")
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(cli_mod.ArtifactRoot, "open", lambda *args, **kwargs: root)
    monkeypatch.setattr(cli_mod, "connect", lambda path: _FakeReviewConnection())
    monkeypatch.setattr(cli_mod, "initialize_database", lambda connection, migration_artifact_root: None)
    monkeypatch.setattr(cli_mod, "list_application_reviews", lambda **kwargs: pytest.fail("list API must not be used for annotation"))
    monkeypatch.setattr(
        cli_mod,
        "persist_review_annotation",
        lambda run, path: calls.append(("annotation", str(path))) or {"artifact_ref": "run-3/annotations/x.txt", "sha256": "a" * 64},
    )
    monkeypatch.setattr(
        cli_mod,
        "complete_review",
        lambda connection, **kwargs: {
            "run_id": 3,
            "job_id": 7,
            "status": "failed",
            "reason_code": "browser_error",
            "outcome": "skipped",
            "job_status": "archived",
            "window_state": "closed",
        },
    )

    assert main([
        "--db", str(tmp_path / "db.sqlite3"),
        "autofill-review", "complete", "--run-id", "3", "--outcome", "skipped",
        "--annotation-file", str(annotation),
    ]) == 0
    assert calls == [("annotation", str(annotation))]
    assert json.loads(capsys.readouterr().out)["artifact_ref"] == "run-3"




def test_cli_review_annotation_uses_requested_older_run_ref(tmp_path: Path, capsys, monkeypatch):
    """Annotation lookup addresses run 101 directly instead of a recent-list window."""
    root = _FakeReviewRoot()
    connection = _FakeReviewConnection("legacy-run-101")
    annotation = tmp_path / "annotation.txt"
    annotation.write_text("older run note", encoding="utf-8")
    monkeypatch.setattr(cli_mod.ArtifactRoot, "open", lambda *args, **kwargs: root)
    monkeypatch.setattr(cli_mod, "connect", lambda path: connection)
    monkeypatch.setattr(cli_mod, "initialize_database", lambda connection, migration_artifact_root: None)
    monkeypatch.setattr(cli_mod, "list_application_reviews", lambda **kwargs: pytest.fail("list API must not be used"))
    monkeypatch.setattr(
        cli_mod,
        "persist_review_annotation",
        lambda run, path: {"artifact_ref": "legacy-run-101/annotations/x.txt", "sha256": "a" * 64},
    )
    monkeypatch.setattr(
        cli_mod,
        "complete_review",
        lambda connection, **kwargs: {
            "run_id": 101,
            "job_id": 7,
            "status": "failed",
            "reason_code": "browser_error",
            "outcome": "skipped",
            "job_status": "archived",
            "window_state": "closed",
        },
    )

    assert main([
        "--db", str(tmp_path / "db.sqlite3"),
        "autofill-review", "complete", "--run-id", "101", "--outcome", "skipped",
        "--annotation-file", str(annotation),
    ]) == 0
    assert root.opened_refs == [("legacy-run-101", 101)]
    assert json.loads(capsys.readouterr().out)["artifact_ref"] == "legacy-run-101"
def test_cli_review_retry_projects_queued_result(tmp_path: Path, capsys, monkeypatch):
    root = _FakeReviewRoot()
    monkeypatch.setattr(cli_mod.ArtifactRoot, "open", lambda *args, **kwargs: root)
    monkeypatch.setattr(cli_mod, "connect", lambda path: _FakeReviewConnection())
    monkeypatch.setattr(cli_mod, "initialize_database", lambda connection, migration_artifact_root: None)
    monkeypatch.setattr(
        cli_mod,
        "retry_review",
        lambda connection, **kwargs: {
            "run_id": 3,
            "job_id": 7,
            "status": "failed",
            "reason_code": "abandoned_running_attempt",
            "outcome": "retry",
            "job_status": "queued",
            "window_state": "closed",
        },
    )
    assert main([
        "--db", str(tmp_path / "db.sqlite3"),
        "autofill-review", "retry", "--run-id", "3",
    ]) == 0
    assert json.loads(capsys.readouterr().out) == {
        "run_id": 3,
        "job_id": 7,
        "status": "failed",
        "reason_code": "abandoned_running_attempt",
        "outcome": "retry",
        "job_status": "queued",
        "artifact_ref": "run-3",
        "window_state": "closed",
    }

@pytest.mark.parametrize("limit", ["0", "101"])
def test_cli_review_list_range_rejected_before_dependencies(monkeypatch, limit):
    monkeypatch.setattr(cli_mod.ArtifactRoot, "open", lambda *args, **kwargs: pytest.fail("artifact opened"))
    with pytest.raises(SystemExit) as exc:
        main(["autofill-review", "list", "--limit", limit])
    assert exc.value.code == 2


@pytest.mark.parametrize("review_command", ["complete", "retry"])
@pytest.mark.parametrize("run_id", ["0", "-1"])
def test_cli_review_run_id_rejected_before_dependencies(monkeypatch, review_command, run_id):
    monkeypatch.setattr(cli_mod.ArtifactRoot, "open", lambda *args, **kwargs: pytest.fail("artifact opened"))
    with pytest.raises(SystemExit) as exc:
        argv = ["autofill-review", review_command, "--run-id", run_id]
        if review_command == "complete":
            argv.extend(["--outcome", "skipped"])
        main(argv)
    assert exc.value.code == 2




@pytest.mark.parametrize("detail", ["run review CAS failed", "run retry CAS failed"])
def test_cli_review_cas_failures_are_fixed_state_conflicts(detail):
    assert cli_mod._review_failure_code(RuntimeError(detail)) == "state_conflict"
@pytest.mark.parametrize("limit", ["0", "11"])
def test_cli_autofill_range_rejected_before_dependencies(monkeypatch, limit):
    calls: list[str] = []
    monkeypatch.setattr(cli_mod.ArtifactRoot, "open", lambda *args, **kwargs: calls.append("artifact") or pytest.fail("artifact opened"))
    monkeypatch.setattr(cli_mod.PuppeteerSession, "preflight", lambda **kwargs: calls.append("preflight"))
    monkeypatch.setattr(cli_mod, "connect", lambda *args, **kwargs: calls.append("db"))

    with pytest.raises(SystemExit) as exc:
        main(["autofill", "--limit", limit])

    assert exc.value.code == 2
    assert calls == []


def test_cli_autofill_malformed_profile_is_preclaim_error(tmp_path: Path, capsys, monkeypatch):
    resume_file = tmp_path / "resume.txt"
    resume_file.write_text("Alex Example\nalex@example.test", encoding="utf-8")
    profile_file = tmp_path / "profile.json"
    profile_file.write_text("{malformed", encoding="utf-8")
    monkeypatch.setattr(cli_mod, "connect", lambda *args, **kwargs: pytest.fail("database opened"))

    assert main([
        "--db", str(tmp_path / "db.sqlite3"),
        "autofill",
        "--resume-file", str(resume_file),
        "--profile-json", str(profile_file),
        "--artifact-root", str(tmp_path / "artifacts"),
    ]) == 1
    assert json.loads(capsys.readouterr().err) == {
        "error": {"code": "invalid_input", "message": "autofill input was rejected"}
    }


@pytest.mark.parametrize("ats", ["bogus", "greenhouse.io", ""])
def test_cli_autofill_ats_is_exact_and_validated_before_dependencies(ats, monkeypatch):
    monkeypatch.setattr(cli_mod, "connect", lambda *args, **kwargs: pytest.fail("database opened"))
    with pytest.raises(SystemExit) as exc:
        main(["autofill", "--ats", ats])
    assert exc.value.code == 2


@pytest.mark.parametrize("profile_flag", ["--profile-json", "--application-profile-json"])
def test_cli_autofill_passes_durable_workflow_kwargs_without_browser(tmp_path: Path, capsys, monkeypatch, profile_flag: str):
    """Autofill cleanly calls the durable workflow without launching Puppeteer."""
    db = tmp_path / "jobs.sqlite3"
    resume_file = tmp_path / "resume.txt"
    profile_json = tmp_path / "profile.json"
    artifact_root = tmp_path / "artifacts"
    resume_file.write_text("fixture resume", encoding="utf-8")
    profile_json.write_text('{"name": "Explicit Profile"}')
    calls: list[dict[str, object]] = []

    async def fake_run_application_workflow(conn, **kwargs):
        calls.append(kwargs)
        return [{
            "job_id": 7,
            "run_id": 3,
            "status": "failed",
            "reason_code": "browser_error",
            "ats": "greenhouse",
            "artifact_ref": "run-3",
            "window_state": "closed",
        }]

    monkeypatch.setattr(cli_mod, "run_application_workflow", fake_run_application_workflow)

    assert main([
        "--db", str(db),
        "autofill",
        "--limit", "2",
        "--resume-file", str(resume_file),
        profile_flag, str(profile_json),
        "--artifact-root", str(artifact_root),
        "--ats", "greenhouse",
        "--headed",
    ]) == 0

    assert calls == [{
        "limit": 2,
        "resume_file": str(resume_file),
        "application_profile_json": str(profile_json),
        "application_profile_preset": None,
        "application_profile_dir": None,
        "application_preferences": None,
        "ats": "greenhouse",
        "applicant_description_file": None,
        "artifact_root": str(artifact_root),
        "headed": True,
    }]
    assert json.loads(capsys.readouterr().out) == {
        "results": [{
            "job_id": 7,
            "run_id": 3,
            "status": "failed",
            "reason_code": "browser_error",
            "ats": "greenhouse",
            "artifact_ref": "run-3",
            "window_state": "closed",
        }],
    }


def test_cli_autofill_runtime_failure_is_redacted(tmp_path: Path, capsys, monkeypatch):
    secret = str(tmp_path / "private-secret-resume.pdf")

    def fail_preflight(**kwargs):
        raise RuntimeError(f"node stack trace at {secret}")

    monkeypatch.setattr(cli_mod.PuppeteerSession, "preflight", fail_preflight)
    assert main(["--db", str(tmp_path / "db.sqlite3"), "autofill", "--artifact-root", str(tmp_path / "artifacts")]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert json.loads(captured.err) == {
        "error": {"code": "browser_preflight_error", "message": "browser preflight failed"}
    }
    assert secret not in captured.err


def test_cli_autofill_malformed_result_fails_closed(tmp_path: Path, capsys, monkeypatch):
    secret = "do-not-publish"

    async def fake_run_application_workflow(conn, **kwargs):
        return [{
            "job_id": 7,
            "run_id": 3,
            "status": "manual",
            "reason_code": "browser_error",
            "ats": "greenhouse",
            "artifact_ref": "run-3",
            "window_state": "closed",
            "secret": secret,
        }]

    monkeypatch.setattr(cli_mod, "run_application_workflow", fake_run_application_workflow)
    assert main(["--db", str(tmp_path / "db.sqlite3"), "autofill", "--artifact-root", str(tmp_path / "artifacts")]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert json.loads(captured.err) == {
        "error": {"code": "invalid_result", "message": "autofill returned an invalid result"}
    }
    assert secret not in captured.out
    assert secret not in captured.err



def test_cli_review_transition_unhashable_job_status_fails_closed():
    raw = {
        "run_id": 3,
        "job_id": 7,
        "status": "failed",
        "reason_code": "browser_error",
        "outcome": "skipped",
        "job_status": {"archived": True},
        "window_state": "closed",
    }
    with pytest.raises(cli_mod._CliFailure) as exc:
        cli_mod._sanitize_review_transition(raw, "run-3", outcome="skipped")
    assert exc.value.code == "invalid_result"


def test_cli_theirstack_preview_pinned_labels_count_unfiltered_and_does_not_persist(tmp_path: Path, capsys, monkeypatch):
    db = tmp_path / "jobs.sqlite3"
    monkeypatch.setenv("THEIRSTACK_API_KEY", "test-key")
    fake = FakeTheirStackClient({"total_results": 9, "data": [{"id": "blurred", "url": "https://evil.example/apply"}]})
    monkeypatch.setattr(cli_mod, "_theirstack_client", lambda *, paid_fetch: fake)

    assert main(["--db", str(db), "theirstack-preview", "--ats", "greenhouse"]) == 0
    assert "url_domain_or" not in fake.payloads[0]
    output = json.loads(capsys.readouterr().out)
    assert output["total_results"] == 9
    assert output["total_results_unfiltered"] == 9
    assert output["ats_filter"] == "greenhouse"
    assert output["ats_filter_applied"] is False
    assert "no application URLs" in output["ats_filter_reason"]
    from jobs_assistant.db import connect
    conn = connect(str(db))
    assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0


def test_cli_theirstack_pinned_sync_reports_filter_counts_and_forwards_ats(tmp_path: Path, capsys, monkeypatch):
    db = tmp_path / "jobs.sqlite3"
    monkeypatch.setenv("THEIRSTACK_API_KEY", "test-key")
    fake = FakeTheirStackClient(
        {
            "data": [
                {"id": "gh", "title": "Engineer", "company_name": "Acme", "url": "https://boards.greenhouse.io/acme/jobs/123"},
                {"id": "bad", "title": "Engineer", "company_name": "Evil", "url": "https://evil.example/apply"},
            ]
        }
    )
    monkeypatch.setattr(cli_mod, "_theirstack_client", lambda *, paid_fetch: fake)

    assert main(["--db", str(db), "theirstack-sync", "--paid-fetch", "--ats", "greenhouse"]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["ats_filter"] == "greenhouse"
    assert output["ats_filter_applied"] is True
    assert output["fetched"] == 2
    assert output["ats_eligible"] == 1
    assert output["ats_rejected"] == 1
    assert output["inserted"] == 1
    assert output["seen"] == 1
    from jobs_assistant.db import connect
    conn = connect(str(db))
    assert fake.payloads[0]["url_domain_or"] == ["greenhouse.io", "grnh.se"]
    assert [row["source_job_id"] for row in conn.execute("SELECT source_job_id FROM jobs")] == ["gh"]


def test_job_scrape_forwards_pinned_ats_filter(tmp_path: Path, capsys, monkeypatch):
    db = tmp_path / "jobs.sqlite3"
    monkeypatch.setenv("THEIRSTACK_API_KEY", "test-key")
    fake = FakeTheirStackClient(
        {
            "data": [
                {"id": "lev", "title": "Engineer", "company_name": "Acme", "url": "https://jobs.lever.co/acme/123e4567-e89b-12d3-a456-426614174000"},
                {"id": "bad", "title": "Engineer", "company_name": "Evil", "url": "https://example.com/apply"},
            ]
        }
    )
    monkeypatch.setattr(cli_mod, "_theirstack_client", lambda *, paid_fetch: fake)

    assert job_scrape_main(["--db", str(db), "--paid-fetch", "--ats", "lever", "--count", "2"]) == 0
    assert fake.payloads[0]["url_domain_or"] == ["lever.co"]
    output = json.loads(capsys.readouterr().out)
    assert output["ats_filter"] == "lever"
    assert output["fetched"] == 2
    assert output["ats_eligible"] == 1
    assert output["ats_rejected"] == 1


def _add_cli_url_less_queued(db: Path) -> int:
    connection = connect(db)
    try:
        result = upsert_job(
            connection,
            JobInput(source="feed", source_job_id="queued-url-less", url=None, title="No URL", company="Acme"),
        )
        return result.job_id
    finally:
        connection.close()


def test_cli_backlog_archive_is_explicit_atomic_and_updates_read_only_list(tmp_path: Path, capsys) -> None:
    db = tmp_path / "jobs.sqlite3"
    _seed_cli_backlog(db)
    url_less_id = _add_cli_url_less_queued(db)

    assert main(["--db", str(db), "backlog-archive", "1", str(url_less_id), "--confirm"]) == 0
    assert json.loads(capsys.readouterr().out) == {"archived": [1, url_less_id], "count": 2}

    connection = connect(db)
    try:
        assert connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 6
        assert connection.execute("SELECT status FROM jobs WHERE id=1").fetchone()[0] == "archived"
        assert connection.execute("SELECT status FROM jobs WHERE id=?", (url_less_id,)).fetchone()[0] == "archived"
    finally:
        connection.close()

    assert main(["--db", str(db), "backlog-list", "--status", "queued"]) == 0
    listed = json.loads(capsys.readouterr().out)
    assert listed["pending"] == 2
    assert [job["id"] for job in listed["jobs"]] == [2, 3]


def test_cli_backlog_archive_rolls_back_on_in_progress_or_missing_row(tmp_path: Path, capsys) -> None:
    db = tmp_path / "jobs.sqlite3"
    _seed_cli_backlog(db)

    assert main(["--db", str(db), "backlog-archive", "1", "4", "--confirm"]) == 1
    assert capsys.readouterr().err == '{"error": {"code": "backlog_archive_conflict", "message": "backlog archive state conflict"}}\n'
    connection = connect(db)
    try:
        assert connection.execute("SELECT status FROM jobs WHERE id=1").fetchone()[0] == "queued"
    finally:
        connection.close()

    assert main(["--db", str(db), "backlog-archive", "1", "999", "--confirm"]) == 1
    assert capsys.readouterr().err == '{"error": {"code": "backlog_archive_conflict", "message": "backlog archive state conflict"}}\n'
    connection = connect(db)
    try:
        assert connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 5
        assert connection.execute("SELECT status FROM jobs WHERE id=1").fetchone()[0] == "queued"
    finally:
        connection.close()

@pytest.mark.parametrize(
    "args",
    [
        ["1"],
        ["--confirm"],
        ["1", "1", "--confirm"],
        ["0", "--confirm"],
        [*(str(value) for value in range(1, 102)), "--confirm"],
    ],
)
def test_cli_backlog_archive_rejects_invalid_ids_before_opening_db(tmp_path: Path, monkeypatch, capsys, args) -> None:
    def fail_connect(*_args, **_kwargs):
        pytest.fail("database opened before backlog-archive argument validation")

    monkeypatch.setattr(cli_mod, "connect", fail_connect)
    assert main(["--db", str(tmp_path / "jobs.sqlite3"), "backlog-archive", *args]) == 1
    assert capsys.readouterr().out == ""