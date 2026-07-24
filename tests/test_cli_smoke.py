import asyncio
import io
import os
import signal
import time
import httpx
import secrets
import shutil
import sqlite3
import json
import sys
from typing import Any

import pytest
from pathlib import Path

import jobs_assistant.cli as cli_mod
from jobs_assistant.artifacts import ArtifactRoot
from jobs_assistant.backlog import upsert_job
from jobs_assistant.contracts import JobInput
from jobs_assistant.db import connect, init_db, initialize_database

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


def test_cli_backlog_list_offset_pages_are_disjoint_and_keep_full_counts(tmp_path: Path, capsys) -> None:
    db = tmp_path / "jobs.sqlite3"
    _seed_cli_backlog(db)

    pages = []
    for offset in (0, 1, 2):
        assert main(
            [
                "--db",
                str(db),
                "backlog-list",
                "--status",
                "queued",
                "--limit",
                "1",
                "--offset",
                str(offset),
            ]
        ) == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["offset"] == offset
        assert payload["limit"] == 1
        assert payload["total"] == 5
        assert payload["pending"] == 3
        pages.append(payload["jobs"])

    page_ids = [{job["id"] for job in page} for page in pages]
    assert page_ids == [{1}, {2}, {3}]
    assert not (page_ids[0] & page_ids[1] or page_ids[0] & page_ids[2] or page_ids[1] & page_ids[2])

def test_cli_backlog_list_source_filters_exactly_and_scopes_counts(tmp_path: Path, capsys) -> None:
    db = tmp_path / "jobs.sqlite3"
    _seed_cli_backlog(db)
    connection = connect(db)
    try:
        connection.execute(
            "UPDATE jobs SET source = ? WHERE source_job_id IN (?, ?)",
            ("other-feed", "queued-early", "archived"),
        )
        connection.commit()
    finally:
        connection.close()
    before = db.read_bytes()

    assert main(["--db", str(db), "backlog-list", "--source", "feed", "--status", "queued"]) == 0
    feed = json.loads(capsys.readouterr().out)
    assert feed["total"] == 3
    assert feed["pending"] == 2
    assert [job["source_job_id"] for job in feed["jobs"]] == ["queued-late", "queued-no-date"]
    assert {job["source"] for job in feed["jobs"]} == {"feed"}
    assert db.read_bytes() == before

    assert main(["--db", str(db), "backlog-list", "--source", "other-feed", "--status", "archived"]) == 0
    other = json.loads(capsys.readouterr().out)
    assert other["total"] == 2
    assert other["pending"] == 1
    assert [job["source_job_id"] for job in other["jobs"]] == ["archived"]
    assert db.read_bytes() == before

    injection = "feed' OR 1=1 --"
    assert main(["--db", str(db), "backlog-list", "--source", injection, "--offset", "0"]) == 0
    literal = json.loads(capsys.readouterr().out)
    assert literal["jobs"] == []
    assert literal["total"] == 0
    assert literal["pending"] == 0
    assert db.read_bytes() == before


def test_cli_backlog_list_source_missing_db_fails_without_creating_file(tmp_path: Path, capsys) -> None:
    db = tmp_path / "missing.sqlite3"

    assert main(["--db", str(db), "backlog-list", "--source", "feed", "--offset", "1"]) == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == '{"error": {"code": "database_error", "message": "database operation failed"}}\n'
    assert not db.exists()


@pytest.mark.parametrize("source", ["", "   ", "x" * 129])
def test_cli_backlog_list_rejects_invalid_source_before_db_work(tmp_path: Path, monkeypatch, source: str) -> None:
    def fail_connect(*args, **kwargs):
        pytest.fail("database opened before backlog-list source validation")

    monkeypatch.setattr(cli_mod, "connect_read_only", fail_connect)
    with pytest.raises(SystemExit) as exc:
        main(["--db", str(tmp_path / "jobs.sqlite3"), "backlog-list", "--source", source])
    assert exc.value.code == 2


@pytest.mark.parametrize(
    "option",
    [
        ["--limit", "0"],
        ["--limit", "101"],
        ["--offset", "-1"],
        ["--offset", str(cli_mod.MAX_BACKLOG_OFFSET + 1)],
        ["--status", "unsupported"],
    ],
)
def test_cli_backlog_list_rejects_invalid_arguments_before_db_work(tmp_path: Path, monkeypatch, option) -> None:
    def fail_connect(*args, **kwargs):
        pytest.fail("database opened before backlog-list argument validation")

    monkeypatch.setattr(cli_mod, "connect_read_only", fail_connect)
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



def test_cli_backlog_show_returns_public_fields_description_and_no_raw_json(tmp_path: Path, capsys) -> None:
    db = tmp_path / "jobs.sqlite3"
    _seed_cli_backlog(db)
    before = db.read_bytes()

    assert main(["--db", str(db), "backlog-show", "1"]) == 0
    output = capsys.readouterr().out
    payload = json.loads(output)

    assert set(payload) == {*cli_mod._BACKLOG_PUBLIC_FIELDS, "description"}
    assert payload["id"] == 1
    assert payload["source_job_id"] == "queued-late"
    assert payload["description"] == "description"
    assert "raw_json" not in payload
    assert "hidden" not in output
    assert db.read_bytes() == before


@pytest.mark.parametrize(("job_id", "status"), [(1, "queued"), (4, "in_progress"), (5, "archived")])
def test_cli_backlog_show_supports_all_backlog_statuses(
    tmp_path: Path, capsys, job_id: int, status: str
) -> None:
    db = tmp_path / "jobs.sqlite3"
    _seed_cli_backlog(db)

    assert main(["backlog-show", "--db", str(db), str(job_id)]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == status
    assert payload["id"] == job_id


def test_cli_backlog_show_truncates_plain_text_and_preserves_null_description(tmp_path: Path, capsys) -> None:
    db = tmp_path / "jobs.sqlite3"
    _seed_cli_backlog(db)
    connection = connect(db)
    try:
        connection.execute(
            "UPDATE jobs SET description = ? WHERE id = 1",
            (
                "<p>Start</p><script>secret-from-script</script><p>"
                + ("x" * (cli_mod.MAX_BACKLOG_DESCRIPTION_CHARS + 100))
                + "</p>",
            ),
        )
        connection.execute("UPDATE jobs SET description = NULL WHERE id = 5")
        connection.commit()
    finally:
        connection.close()
    before = db.read_bytes()

    assert main(["--db", str(db), "backlog-show", "1"]) == 0
    shown = json.loads(capsys.readouterr().out)
    assert shown["description"].startswith("Start\n")
    assert len(shown["description"]) <= cli_mod.MAX_BACKLOG_DESCRIPTION_CHARS
    assert "<p>" not in shown["description"]
    assert "secret-from-script" not in shown["description"]
    assert db.read_bytes() == before

    assert main(["--db", str(db), "backlog-show", "5"]) == 0
    assert json.loads(capsys.readouterr().out)["description"] is None
    assert db.read_bytes() == before


def test_cli_backlog_show_unknown_id_uses_database_error_without_mutation(tmp_path: Path, capsys) -> None:
    db = tmp_path / "jobs.sqlite3"
    _seed_cli_backlog(db)
    before = db.read_bytes()

    assert main(["--db", str(db), "backlog-show", "9999"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == '{"error": {"code": "database_error", "message": "database operation failed"}}\n'
    assert db.read_bytes() == before


def test_cli_backlog_show_missing_db_uses_database_error_without_creating_file(tmp_path: Path, capsys) -> None:
    db = tmp_path / "missing.sqlite3"

    assert main(["backlog-show", "--db", str(db), "1"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == '{"error": {"code": "database_error", "message": "database operation failed"}}\n'
    assert not db.exists()


@pytest.mark.parametrize("job_id", ["0", "-1", "not-an-id"])
def test_cli_backlog_show_rejects_invalid_id_before_db_work(tmp_path: Path, monkeypatch, job_id: str) -> None:
    def fail_connect(*args, **kwargs):
        pytest.fail("database opened before backlog-show ID validation")

    monkeypatch.setattr(cli_mod, "connect_read_only", fail_connect)
    with pytest.raises(SystemExit) as exc:
        main(["--db", str(tmp_path / "jobs.sqlite3"), "backlog-show", job_id])
    assert exc.value.code == 2

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

def test_cli_import_feed_json_fixture_defaults_to_job_source(tmp_path: Path, capsys):
    db = tmp_path / "jobs.sqlite3"
    fixture = tmp_path / "jobs.json"
    fixture.write_text('{"jobs":[{"id":"1","title":"Software Engineer","company":"Acme","apply_url":"https://jobs.example.com/1"}]}')
    assert main(["--db", str(db), "import-feed", "--json-file", str(fixture)]) == 0
    assert capsys.readouterr().out.strip() == '{"inserted": 1, "seen": 1, "updated": 0}'
    connection = connect(db)
    try:
        assert connection.execute("SELECT source FROM jobs").fetchone()["source"] == "job_source"
    finally:
        connection.close()


@pytest.mark.parametrize(
    "payload",
    [
        [{"id": "list-1", "title": "List Engineer", "company": "Acme", "apply_url": "https://jobs.example.com/list-1"}],
        {"data": [{"id": "data-1", "title": "Data Engineer", "company": "Acme", "apply_url": "https://jobs.example.com/data-1"}]},
    ],
)
def test_cli_import_feed_accepts_list_and_data_envelopes(tmp_path: Path, capsys, payload):
    db = tmp_path / "jobs.sqlite3"
    fixture = tmp_path / "jobs.json"
    fixture.write_text(json.dumps(payload), encoding="utf-8")

    assert main(["--db", str(db), "import-feed", "--json-file", str(fixture)]) == 0
    assert json.loads(capsys.readouterr().out) == {"inserted": 1, "seen": 1, "updated": 0}


@pytest.mark.parametrize(
    "payload",
    [
        None,
        17,
        "private raw payload",
        {},
        {"results": []},
        {"jobs": {}},
        {"data": None},
        {"jobs": [{"id": "valid"}, "private malformed record"]},
        {"data": [{"id": "valid"}, None]},
        {"jobs": [{"id": "valid"}], "data": [None]},
    ],
)
def test_cli_import_feed_rejects_malformed_payload_without_writes(tmp_path: Path, capsys, payload):
    db = tmp_path / "jobs.sqlite3"
    fixture = tmp_path / "private-feed.json"
    fixture.write_text(json.dumps(payload), encoding="utf-8")

    assert main(["--db", str(db), "import-feed", "--json-file", str(fixture)]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == '{"error": {"code": "invalid_input", "message": "autofill input was rejected"}}\n'
    assert str(fixture) not in captured.err
    assert "private" not in captured.err
    assert not db.exists()


def test_cli_import_feed_keeps_distinct_sources_for_backlog_filters(tmp_path: Path, capsys, monkeypatch):
    db = tmp_path / "jobs.sqlite3"
    fixture = tmp_path / "jobs.json"
    fixture.write_text(
        '{"jobs":[{"id":"json-1","title":"JSON Engineer","company":"Acme",'
        '"apply_url":"https://jobs.example.com/json-1"}]}'
    )

    def fake_fetch_source_jobs(base_url: str, api_key: str | None = None):
        assert base_url == "https://feed.example.test"
        return [
            {
                "id": "feed-1",
                "title": "Feed Engineer",
                "company": "Acme",
                "apply_url": "https://jobs.example.com/feed-1",
            }
        ]

    monkeypatch.setattr(cli_mod, "fetch_source_jobs", fake_fetch_source_jobs)
    assert main(
        [
            "--db",
            str(db),
            "import-feed",
            "--json-file",
            str(fixture),
            "--source",
            "json-fixture",
        ]
    ) == 0
    assert json.loads(capsys.readouterr().out) == {"inserted": 1, "seen": 1, "updated": 0}
    assert main(
        [
            "--db",
            str(db),
            "import-feed",
            "--base-url",
            "https://feed.example.test",
            "--source",
            "http-feed' OR 1=1 --",
        ]
    ) == 0
    assert json.loads(capsys.readouterr().out) == {"inserted": 1, "seen": 1, "updated": 0}

    assert main(["--db", str(db), "backlog-list", "--source", "json-fixture"]) == 0
    json_backlog = json.loads(capsys.readouterr().out)
    assert json_backlog["total"] == 1
    assert json_backlog["pending"] == 1
    assert [job["source"] for job in json_backlog["jobs"]] == ["json-fixture"]

    assert main(["--db", str(db), "backlog-list", "--source", "http-feed' OR 1=1 --"]) == 0
    feed_backlog = json.loads(capsys.readouterr().out)
    assert feed_backlog["total"] == 1
    assert feed_backlog["pending"] == 1
    assert [job["source"] for job in feed_backlog["jobs"]] == ["http-feed' OR 1=1 --"]


@pytest.mark.parametrize("source", ["", "   ", "x" * 129])
def test_cli_import_feed_rejects_invalid_source_before_db_work(tmp_path: Path, monkeypatch, source: str):
    def fail_connect(*args, **kwargs):
        pytest.fail("database opened before import-feed source validation")

    monkeypatch.setattr(cli_mod, "connect", fail_connect)
    with pytest.raises(SystemExit) as exc:
        main(
            [
                "--db",
                str(tmp_path / "jobs.sqlite3"),
                "import-feed",
                "--source",
                source,
            ]
        )
    assert exc.value.code == 2


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


def test_cli_import_feed_records_file_sync_audit_without_changing_stdout(tmp_path: Path, capsys):
    db = tmp_path / "jobs.sqlite3"
    fixture = tmp_path / "jobs.json"
    fixture.write_text(
        '{"jobs":[{"id":"file-1","title":"Software Engineer","company":"Acme",'
        '"apply_url":"https://jobs.example.com/file-1"}]}',
        encoding="utf-8",
    )

    assert main(["--db", str(db), "import-feed", "--json-file", str(fixture), "--source", "file-feed"]) == 0
    assert capsys.readouterr().out.strip() == '{"inserted": 1, "seen": 1, "updated": 0}'

    connection = connect(db)
    try:
        run = connection.execute(
            "SELECT source, mode, jobs_seen, jobs_returned, jobs_inserted, jobs_updated, finished_at, success, error "
            "FROM sync_runs"
        ).fetchone()
        assert run["source"] == "file-feed"
        assert run["mode"] == "json_file"
        assert (run["jobs_seen"], run["jobs_returned"], run["jobs_inserted"], run["jobs_updated"]) == (1, 1, 1, 0)
        assert run["finished_at"]
        assert run["success"] == 1
        assert run["error"] is None
    finally:
        connection.close()


def test_cli_import_feed_records_http_sync_audit(tmp_path: Path, capsys, monkeypatch):
    db = tmp_path / "jobs.sqlite3"

    def fake_fetch_source_jobs(base_url: str, api_key: str | None = None):
        assert base_url == "https://feed.example.test"
        assert api_key == "private-token"
        return [{"id": "http-1", "title": "Backend Engineer", "company": "Acme", "apply_url": "https://jobs.example.com/http-1"}]

    monkeypatch.setenv("JOB_SOURCE_API_KEY", "private-token")
    monkeypatch.setattr(cli_mod, "fetch_source_jobs", fake_fetch_source_jobs)

    assert main(["--db", str(db), "import-feed", "--base-url", "https://feed.example.test", "--source", "http-feed"]) == 0
    assert capsys.readouterr().out.strip() == '{"inserted": 1, "seen": 1, "updated": 0}'

    connection = connect(db)
    try:
        run = connection.execute(
            "SELECT source, mode, jobs_seen, jobs_returned, jobs_inserted, jobs_updated, finished_at, success, error "
            "FROM sync_runs"
        ).fetchone()
        assert run["source"] == "http-feed"
        assert run["mode"] == "http"
        assert (run["jobs_seen"], run["jobs_returned"], run["jobs_inserted"], run["jobs_updated"]) == (1, 1, 1, 0)
        assert run["finished_at"]
        assert run["success"] == 1
        assert run["error"] is None
        assert "private-token" not in json.dumps(dict(run))
    finally:
        connection.close()


def test_cli_import_feed_http_failure_audits_redacted_error_without_jobs(tmp_path: Path, capsys, monkeypatch):
    db = tmp_path / "jobs.sqlite3"

    def fail_fetch_source_jobs(base_url: str, api_key: str | None = None):
        request = httpx.Request("GET", f"{base_url}/v1/jobs?token={api_key}")
        raise httpx.ReadTimeout("private transport detail", request=request)

    monkeypatch.setenv("JOB_SOURCE_API_KEY", "private-token")
    monkeypatch.setattr(cli_mod, "fetch_source_jobs", fail_fetch_source_jobs)

    assert main(["--db", str(db), "import-feed", "--base-url", "https://feed.example.test", "--source", "http-feed"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert json.loads(captured.err) == {
        "error": {"code": "invalid_input", "message": "autofill input was rejected"}
    }
    assert "private-token" not in captured.err
    assert "feed.example.test" not in captured.err

    connection = connect(db)
    try:
        assert connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0
        run = connection.execute(
            "SELECT source, mode, jobs_seen, jobs_returned, jobs_inserted, jobs_updated, finished_at, success, error "
            "FROM sync_runs"
        ).fetchone()
        assert run["source"] == "http-feed"
        assert run["mode"] == "http"
        assert (run["jobs_seen"], run["jobs_returned"], run["jobs_inserted"], run["jobs_updated"]) == (0, 0, 0, 0)
        assert run["finished_at"]
        assert run["success"] == 0
        assert run["error"] == "source request failed"
    finally:
        connection.close()


def test_cli_import_feed_failure_rolls_back_jobs_but_audits_attempt(tmp_path: Path, capsys):
    db = tmp_path / "jobs.sqlite3"
    fixture = tmp_path / "jobs.json"
    fixture.write_text(
        '{"jobs":[{"id":"valid","title":"Valid Engineer","company":"Acme",'
        '"apply_url":"https://jobs.example.com/valid"},{"title":"Missing URL","company":"Acme"}]}',
        encoding="utf-8",
    )

    assert main(["--db", str(db), "import-feed", "--json-file", str(fixture), "--source", "file-feed"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert json.loads(captured.err) == {
        "error": {"code": "invalid_input", "message": "autofill input was rejected"}
    }

    connection = connect(db)
    try:
        assert connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0
        run = connection.execute(
            "SELECT source, mode, jobs_seen, jobs_returned, jobs_inserted, jobs_updated, finished_at, success, error "
            "FROM sync_runs"
        ).fetchone()
        assert run["source"] == "file-feed"
        assert run["mode"] == "json_file"
        assert (run["jobs_seen"], run["jobs_returned"], run["jobs_inserted"], run["jobs_updated"]) == (2, 2, 0, 0)
        assert run["finished_at"]
        assert run["success"] == 0
        assert run["error"] == "source payload rejected"
    finally:
        connection.close()

def test_cli_import_feed_success_audit_failure_rolls_back_jobs_and_audits_failure(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    db = tmp_path / "jobs.sqlite3"
    fixture = tmp_path / "jobs.json"
    fixture.write_text(
        '{"jobs":[{"id":"audit-fail","title":"Audit Engineer","company":"Acme",'
        '"apply_url":"https://jobs.example.com/audit-fail"}]}',
        encoding="utf-8",
    )
    real_update_sync_run = cli_mod.update_sync_run
    calls: list[bool] = []

    def fail_success_audit(connection, run_id: int, **kwargs):
        calls.append(bool(kwargs["success"]))
        if kwargs["success"]:
            raise sqlite3.DatabaseError("injected terminal success audit failure")
        return real_update_sync_run(connection, run_id, **kwargs)

    monkeypatch.setattr(cli_mod, "update_sync_run", fail_success_audit)

    assert main(["--db", str(db), "import-feed", "--json-file", str(fixture), "--source", "file-feed"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert json.loads(captured.err) == {
        "error": {"code": "database_error", "message": "database operation failed"}
    }
    assert calls == [True, False]

    connection = connect(db)
    try:
        assert connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0
        run = connection.execute(
            "SELECT jobs_seen, jobs_returned, jobs_inserted, jobs_updated, finished_at, success, error "
            "FROM sync_runs"
        ).fetchone()
        assert (run["jobs_seen"], run["jobs_returned"], run["jobs_inserted"], run["jobs_updated"]) == (1, 1, 0, 0)
        assert run["finished_at"]
        assert run["success"] == 0
        assert run["error"] == "database operation failed"
    finally:
        connection.close()


def test_cli_import_feed_double_audit_failure_preserves_original_error_mapping(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    db = tmp_path / "jobs.sqlite3"
    fixture = tmp_path / "jobs.json"
    fixture.write_text(
        '{"jobs":[{"id":"double-audit-fail","title":"Database Engineer","company":"Acme",'
        '"apply_url":"https://jobs.example.com/double-audit-fail"}]}',
        encoding="utf-8",
    )
    calls: list[bool] = []

    def fail_both_audits(connection, run_id: int, **kwargs):
        calls.append(bool(kwargs["success"]))
        if kwargs["success"]:
            raise sqlite3.DatabaseError("injected terminal success audit failure")
        raise RuntimeError("injected failure audit failure")

    monkeypatch.setattr(cli_mod, "update_sync_run", fail_both_audits)

    assert main(["--db", str(db), "import-feed", "--json-file", str(fixture), "--source", "file-feed"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert json.loads(captured.err) == {
        "error": {"code": "database_error", "message": "database operation failed"}
    }
    assert calls == [True, False]

    connection = connect(db)
    try:
        assert connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0
        run = connection.execute(
            "SELECT jobs_seen, jobs_returned, jobs_inserted, jobs_updated, finished_at, success, error "
            "FROM sync_runs"
        ).fetchone()
        assert (run["jobs_seen"], run["jobs_returned"], run["jobs_inserted"], run["jobs_updated"]) == (0, 0, 0, 0)
        assert run["finished_at"] is None
        assert run["success"] == 0
        assert run["error"] is None
    finally:
        connection.close()




def test_cli_import_feed_rollback_failure_skips_failure_audit(tmp_path: Path, capsys, monkeypatch) -> None:
    db = tmp_path / "jobs.sqlite3"
    fixture = tmp_path / "jobs.json"
    fixture.write_text(
        '{"jobs":[{"id":"rollback-fail","title":"Rollback Engineer","company":"Acme",'
        '"apply_url":"https://jobs.example.com/rollback-fail"}]}',
        encoding="utf-8",
    )

    class RollbackFailureConnection:
        def __init__(self, inner):
            self.inner = inner
            self.rollback_calls = 0

        def rollback(self):
            self.rollback_calls += 1
            raise sqlite3.DatabaseError("injected rollback failure")

        def close(self):
            self.inner.close()

        def __getattr__(self, name):
            return getattr(self.inner, name)

    real_connect = cli_mod.connect
    connections: list[RollbackFailureConnection] = []

    def fail_connect(path):
        connection = RollbackFailureConnection(real_connect(path))
        connections.append(connection)
        return connection

    monkeypatch.setattr(cli_mod, "connect", fail_connect)
    real_update_sync_run = cli_mod.update_sync_run
    calls: list[bool] = []

    def fail_success_audit(connection, run_id: int, **kwargs):
        calls.append(bool(kwargs["success"]))
        if kwargs["success"]:
            raise sqlite3.DatabaseError("injected terminal success audit failure")
        return real_update_sync_run(connection, run_id, **kwargs)

    monkeypatch.setattr(cli_mod, "update_sync_run", fail_success_audit)

    assert main(["--db", str(db), "import-feed", "--json-file", str(fixture), "--source", "file-feed"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert json.loads(captured.err) == {
        "error": {"code": "database_error", "message": "database operation failed"}
    }
    assert calls == [True]
    assert connections[0].rollback_calls == 1

    connection = connect(db)
    try:
        assert connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0
        run = connection.execute(
            "SELECT jobs_seen, jobs_returned, jobs_inserted, jobs_updated, finished_at, success, error "
            "FROM sync_runs"
        ).fetchone()
        assert (run["jobs_seen"], run["jobs_returned"], run["jobs_inserted"], run["jobs_updated"]) == (0, 0, 0, 0)
        assert run["finished_at"] is None
        assert run["success"] == 0
        assert run["error"] is None
    finally:
        connection.close()


def test_cli_import_feed_stdout_failure_preserves_success_audit_and_jobs(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    """A stdout failure after a committed success audit must not rewrite it or mutate jobs."""
    db = tmp_path / "jobs.sqlite3"
    fixture = tmp_path / "jobs.json"
    fixture.write_text(
        '{"jobs":[{"id":"stdout-fail","title":"Stdout Engineer","company":"Acme",'
        '"apply_url":"https://jobs.example.com/stdout-fail"}]}',
        encoding="utf-8",
    )

    class FailingStdout:
        def write(self, s: str) -> int:
            raise OSError(28, "stdout failure")

        def flush(self) -> None:
            pass

    monkeypatch.setattr(sys, "stdout", FailingStdout())

    with pytest.raises(OSError, match="stdout failure"):
        main(["--db", str(db), "import-feed", "--json-file", str(fixture), "--source", "file-feed"])

    connection = connect(db)
    try:
        assert connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 1
        run = connection.execute(
            "SELECT source, mode, jobs_seen, jobs_returned, jobs_inserted, jobs_updated, "
            "finished_at, success, error FROM sync_runs"
        ).fetchone()
        assert run["source"] == "file-feed"
        assert run["mode"] == "json_file"
        assert (
            run["jobs_seen"],
            run["jobs_returned"],
            run["jobs_inserted"],
            run["jobs_updated"],
        ) == (1, 1, 1, 0)
        assert run["finished_at"]
        assert run["success"] == 1
        assert run["error"] is None
    finally:
        connection.close()


class FakeTheirStackClient:
    """Fake TheirStackClient that returns a canned response without HTTP."""

    def __init__(self, response: dict):
        self._response = response
        self.payloads: list[dict] = []

    def search_jobs(self, payload: dict) -> dict:
        self.payloads.append(payload)
        return self._response


def test_cli_import_feed_dry_run_missing_db_remains_absent(tmp_path: Path, capsys) -> None:
    fixture = tmp_path / "jobs.json"
    fixture.write_text(
        '{"jobs":[{"id":"dry-1","title":"Dry Engineer","company":"Acme",'
        '"apply_url":"https://jobs.example.com/dry-1","description":"secret"}]}',
        encoding="utf-8",
    )
    db = tmp_path / "missing.sqlite3"

    assert main(["--db", str(db), "import-feed", "--json-file", str(fixture), "--dry-run"]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["seen"] == 1
    assert output["would_insert"] == 1
    assert output["would_update"] == 0
    assert output["preview_truncated"] is False
    assert len(output["preview"]) == 1
    preview = output["preview"][0]
    assert set(preview) == {
        "source_job_id",
        "canonical_url",
        "title",
        "company",
        "location",
        "remote",
        "posted_at",
    }
    assert preview["source_job_id"] == "dry-1"
    assert preview["title"] == "Dry Engineer"
    assert "description" not in output
    assert "raw" not in output
    assert "source" not in preview
    assert not db.exists()


def test_cli_import_feed_dry_run_existing_db_unchanged_and_counts_updates(
    tmp_path: Path, capsys
) -> None:
    db = tmp_path / "jobs.sqlite3"
    fixture = tmp_path / "jobs.json"
    fixture.write_text(
        '{"jobs":[{"id":"dry-1","title":"Dry Engineer","company":"Acme",'
        '"apply_url":"https://jobs.example.com/dry-1"}]}',
        encoding="utf-8",
    )

    assert main(["--db", str(db), "import-feed", "--json-file", str(fixture), "--source", "dry-src"]) == 0
    capsys.readouterr()
    before = db.read_bytes()

    connection = connect(db)
    try:
        sync_before = connection.execute("SELECT COUNT(*) FROM sync_runs").fetchone()[0]
        assert connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 1
    finally:
        connection.close()

    assert (
        main(
            [
                "--db",
                str(db),
                "import-feed",
                "--json-file",
                str(fixture),
                "--source",
                "dry-src",
                "--dry-run",
            ]
        )
        == 0
    )
    output = json.loads(capsys.readouterr().out)
    assert output["seen"] == 1
    assert output["would_insert"] == 0
    assert output["would_update"] == 1
    assert output["preview_truncated"] is False
    assert len(output["preview"]) == 1
    assert db.read_bytes() == before

    connection = connect(db)
    try:
        assert connection.execute("SELECT COUNT(*) FROM sync_runs").fetchone()[0] == sync_before
        assert connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 1
    finally:
        connection.close()


def test_cli_import_feed_dry_run_counts_duplicate_records(tmp_path: Path, capsys) -> None:
    fixture = tmp_path / "jobs.json"
    fixture.write_text(
        json.dumps(
            {
                "jobs": [
                    {"id": "dup-1", "title": "First", "company": "Acme", "apply_url": "https://jobs.example.com/dup-1"},
                    {"id": "dup-1", "title": "Second", "company": "Acme", "apply_url": "https://jobs.example.com/dup-1"},
                ]
            }
        ),
        encoding="utf-8",
    )
    db = tmp_path / "missing.sqlite3"

    assert main(["--db", str(db), "import-feed", "--json-file", str(fixture), "--dry-run"]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["seen"] == 2
    assert output["would_insert"] == 1
    assert output["would_update"] == 1
    assert output["preview_truncated"] is False
    assert not db.exists()


def test_cli_import_feed_dry_run_preview_truncates_at_101(tmp_path: Path, capsys) -> None:
    jobs = [
        {
            "id": str(i),
            "title": f"Job {i}",
            "company": "Acme",
            "apply_url": f"https://jobs.example.com/{i}",
        }
        for i in range(101)
    ]
    fixture = tmp_path / "jobs.json"
    fixture.write_text(json.dumps({"jobs": jobs}), encoding="utf-8")
    db = tmp_path / "missing.sqlite3"

    assert main(["--db", str(db), "import-feed", "--json-file", str(fixture), "--dry-run"]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["seen"] == 101
    assert output["would_insert"] == 101
    assert output["would_update"] == 0
    assert len(output["preview"]) == 100
    assert output["preview_truncated"] is True
    assert not db.exists()


@pytest.mark.parametrize(
    "payload",
    [
        None,
        17,
        "private raw payload",
        {},
        {"results": []},
        {"jobs": {}},
        {"data": None},
        {"jobs": [{"id": "valid"}, "private malformed record"]},
    ],
)
def test_cli_import_feed_dry_run_rejects_malformed_payload_without_db(
    tmp_path: Path, capsys, payload
) -> None:
    fixture = tmp_path / "jobs.json"
    fixture.write_text(json.dumps(payload), encoding="utf-8")
    db = tmp_path / "missing.sqlite3"

    assert main(["--db", str(db), "import-feed", "--json-file", str(fixture), "--dry-run"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert json.loads(captured.err) == {
        "error": {"code": "invalid_input", "message": "autofill input was rejected"}
    }
    assert not db.exists()


def test_cli_import_feed_dry_run_http_fetches_without_persisting(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    db = tmp_path / "missing.sqlite3"
    calls: list[tuple[str, str | None]] = []

    def fake_fetch_source_jobs(base_url: str, api_key: str | None = None):
        calls.append((base_url, api_key))
        return [
            {
                "id": "http-dry-1",
                "title": "HTTP Dry Engineer",
                "company": "Acme",
                "apply_url": "https://jobs.example.com/http-dry-1",
            }
        ]

    monkeypatch.setattr(cli_mod, "fetch_source_jobs", fake_fetch_source_jobs)

    assert (
        main(
            [
                "--db",
                str(db),
                "import-feed",
                "--base-url",
                "https://feed.example.test",
                "--source",
                "http-dry",
                "--dry-run",
            ]
        )
        == 0
    )
    output = json.loads(capsys.readouterr().out)
    assert output["seen"] == 1
    assert output["would_insert"] == 1
    assert output["would_update"] == 0
    assert calls[0][0] == "https://feed.example.test"
    assert not db.exists()


def test_cli_import_feed_dry_run_http_failure_is_redacted_without_db(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    db = tmp_path / "missing.sqlite3"

    def fail_fetch_source_jobs(base_url: str, api_key: str | None = None):
        request = httpx.Request("GET", f"{base_url}/v1/jobs")
        raise httpx.ConnectError("private transport detail", request=request)

    monkeypatch.setattr(cli_mod, "fetch_source_jobs", fail_fetch_source_jobs)

    assert (
        main(
            [
                "--db",
                str(db),
                "import-feed",
                "--base-url",
                "https://feed.example.test",
                "--source",
                "http-dry",
                "--dry-run",
            ]
        )
        == 1
    )
    captured = capsys.readouterr()
    assert captured.out == ""
    assert json.loads(captured.err) == {
        "error": {"code": "invalid_input", "message": "autofill input was rejected"}
    }
    assert "private" not in captured.err
    assert not db.exists()


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


def _make_review_show_run(
    tmp_path: Path,
    *,
    ats: str = "greenhouse",
    status: str = "review_ready",
    reason_code: str = "draft_ready",
    stage: str = "finished",
    outcome: str | None = None,
    reviewed_at: str | None = None,
    blocker_codes: list[str] | None = None,
    browser_failure: dict[str, Any] | None = None,
    final_submit_calls: int = 0,
    plan_status: str | None = None,
    plan_reason_code: str | None = None,
) -> tuple[Path, Path, int]:
    db_path = tmp_path / "jobs.sqlite3"
    root_path = tmp_path / "artifacts"
    conn = connect(db_path)
    root = ArtifactRoot.open(root_path, cwd=tmp_path)
    initialize_database(conn, migration_artifact_root=root)
    root.close()

    conn.execute(
        """
        INSERT INTO jobs (
            source, source_job_id, canonical_url, title, company, discovered_at,
            raw_json, first_seen_at, last_seen_at, status
        ) VALUES (
            'fixture', ?, ?, 'Engineer', 'Acme', '2026-07-10T00:00:00+00:00',
            '{}', '2026-07-10T00:00:00+00:00', '2026-07-10T00:00:00+00:00', 'in_progress'
        )
        """,
        (f"job-{ats}-{secrets.token_hex(4)}", f"https://{ats}.example.test/jobs/{secrets.token_hex(4)}"),
    )
    conn.commit()
    job_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.execute(
        """
        INSERT INTO application_runs (
            job_id, apply_url, status, owner, started_at, finished_at,
            reason_code, artifact_dir, outcome, reviewed_at
        ) VALUES (?, ?, ?, 'owner', '2026-07-10T00:00:00Z',
                  '2026-07-10T00:01:00Z', ?, 'run-1', ?, ?)
        """,
        (job_id, f"https://{ats}.example.test/jobs/1", status, reason_code, outcome, reviewed_at),
    )
    conn.commit()
    run_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

    root = ArtifactRoot.open(root_path, cwd=tmp_path)
    run = root.create_run_dir(run_id)
    observation = {
        "observation_id": "obs1",
        "url_host": f"{ats}.example.test",
        "field_count": 5,
        "button_count": 1,
        "required_count": 2,
        "final_marker_count": 1,
        "error_count": 0,
        "blocker_codes": list(blocker_codes or []),
    }
    plan = {
        "status": plan_status if plan_status is not None else ("manual" if blocker_codes else "ready"),
        "reason_code": plan_reason_code if plan_reason_code is not None else reason_code,
        "answer_count": 3 if not blocker_codes else 0,
        "skipped_target_count": 0,
        "resume_upload": False,
        "safe_click": False,
    }
    actions = {
        "mutation_count": 0,
        "actions": [],
        "final_submit_calls": final_submit_calls,
    }
    obs_result = run.write_json("observation.json", observation)
    plan_result = run.write_json("plan.json", plan)
    actions_result = run.write_json("actions.json", actions)
    artifacts_descriptor: dict[str, Any] = {
        "observation": {"path": "observation.json", "sha256": obs_result.sha256, "iteration": 1, "stage": stage},
        "plan": {"path": "plan.json", "sha256": plan_result.sha256, "iteration": 1, "stage": stage},
        "actions": {"path": "actions.json", "sha256": actions_result.sha256, "iteration": 1, "stage": stage},
    }
    if browser_failure is not None:
        failure_result = run.write_json("browser_failure.json", browser_failure)
        artifacts_descriptor["browser_failure"] = {
            "path": "browser_failure.json",
            "sha256": failure_result.sha256,
            "iteration": 1,
            "stage": "failed",
        }
    manifest = {
        "run_id": run_id,
        "job_id": job_id,
        "ats_policy": ats,
        "no_final_submit": True,
        "stage": stage,
        "latest_iteration": 1,
        "latest_stage": stage,
        "latest": {"iteration": 1, "stage": stage},
        "commit_token_sha256": None,
        "artifacts": artifacts_descriptor,
    }
    run.write_json("run.json", manifest)
    run.close()
    conn.execute("UPDATE application_runs SET artifact_dir=? WHERE id=?", (f"run-{run_id}", run_id))
    conn.commit()
    conn.close()
    root.close()
    return db_path, root_path, run_id


def test_cli_review_show_greenhouse_success(tmp_path: Path, capsys) -> None:
    db, root, run_id = _make_review_show_run(tmp_path, ats="greenhouse")
    assert main(["--db", str(db), "autofill-review", "--artifact-root", str(root), "show", str(run_id)]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["run_id"] == run_id
    assert payload["ats"] == "greenhouse"
    assert payload["status"] == "review_ready"
    assert payload["reason_code"] == "draft_ready"
    assert payload["artifact_ref"] == f"run-{run_id}"
    assert payload["window_state"] == "none"
    assert payload["evidence"] == {
        "ats": "greenhouse",
        "stage": "finished",
        "latest": {"iteration": 1, "stage": "finished"},
        "no_final_submit": True,
    }
    assert payload["observation"] == {
        "field_count": 5,
        "button_count": 1,
        "required_count": 2,
        "final_marker_count": 1,
        "error_count": 0,
        "blocker_codes": [],
    }
    assert payload["plan"] == {
        "status": "ready",
        "reason_code": "draft_ready",
        "answer_count": 3,
        "skipped_target_count": 0,
        "resume_upload": False,
        "safe_click": False,
    }
    assert payload["actions"] == {
        "mutation_count": 0,
        "action_count": 0,
        "final_submit_calls": 0,
    }
    assert payload["browser_failure"] is None


def test_cli_review_show_lever_success(tmp_path: Path, capsys) -> None:
    db, root, run_id = _make_review_show_run(tmp_path, ats="lever")
    assert main(["--db", str(db), "autofill-review", "--artifact-root", str(root), "show", str(run_id)]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ats"] == "lever"
    assert payload["evidence"]["ats"] == "lever"
    assert payload["plan"]["status"] == "ready"


def test_cli_review_show_blocker_run(tmp_path: Path, capsys) -> None:
    db, root, run_id = _make_review_show_run(
        tmp_path,
        ats="greenhouse",
        status="blocked",
        reason_code="captcha",
        stage="finished",
        blocker_codes=["captcha"],
        plan_status="manual",
        plan_reason_code="captcha",
    )
    assert main(["--db", str(db), "autofill-review", "--artifact-root", str(root), "show", str(run_id)]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "blocked"
    assert payload["reason_code"] == "captcha"
    assert payload["observation"]["blocker_codes"] == ["captcha"]
    assert payload["plan"] == {
        "status": "manual",
        "reason_code": "captcha",
        "answer_count": 0,
        "skipped_target_count": 0,
        "resume_upload": False,
        "safe_click": False,
    }


def test_cli_review_show_browser_failure_run(tmp_path: Path, capsys) -> None:
    failure = {
        "version": 1,
        "stage": "observation",
        "operation": "observe",
        "code": "browser_command_failed",
        "iteration": 1,
        "ats_policy": "greenhouse",
        "no_final_submit": True,
        "protocol": "length-prefixed-json-v1",
    }
    db, root, run_id = _make_review_show_run(
        tmp_path,
        ats="greenhouse",
        status="failed",
        reason_code="browser_error",
        stage="failed",
        browser_failure=failure,
        plan_status="manual",
        plan_reason_code="browser_error",
    )
    assert main(["--db", str(db), "autofill-review", "--artifact-root", str(root), "show", str(run_id)]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "failed"
    assert payload["reason_code"] == "browser_error"
    assert payload["browser_failure"] == {
        "stage": "observation",
        "operation": "observe",
        "code": "browser_command_failed",
        "iteration": 1,
        "ats": "greenhouse",
        "no_final_submit": True,
    }


def test_cli_review_show_reviewed_and_unreviewed_rows(tmp_path: Path, capsys) -> None:
    db_reviewed, root_reviewed, run_id_reviewed = _make_review_show_run(
        tmp_path,
        ats="greenhouse",
        status="review_ready",
        reason_code="draft_ready",
        stage="finished",
        outcome="submitted",
        reviewed_at="2026-07-10T00:02:00Z",
    )
    db_unreviewed, root_unreviewed, run_id_unreviewed = _make_review_show_run(
        tmp_path,
        ats="greenhouse",
        status="review_ready",
        reason_code="draft_ready",
        stage="finished",
    )
    assert main(["--db", str(db_reviewed), "autofill-review", "--artifact-root", str(root_reviewed), "show", str(run_id_reviewed)]) == 0
    reviewed = json.loads(capsys.readouterr().out)
    assert reviewed["outcome"] == "submitted"
    assert reviewed["reviewed_at"] == "2026-07-10T00:02:00Z"
    assert main(["--db", str(db_unreviewed), "autofill-review", "--artifact-root", str(root_unreviewed), "show", str(run_id_unreviewed)]) == 0
    unreviewed = json.loads(capsys.readouterr().out)
    assert unreviewed["outcome"] is None
    assert unreviewed["reviewed_at"] is None


@pytest.mark.parametrize("run_id", ["0", "-1", "abc"])
def test_cli_review_show_rejects_invalid_run_id_before_opening_dependencies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, run_id: str
) -> None:
    def fail_open(*args: object, **kwargs: object) -> object:
        pytest.fail("artifact root opened before run_id validation")

    monkeypatch.setattr(cli_mod.ArtifactRoot, "open_existing", fail_open)
    monkeypatch.setattr(cli_mod.ArtifactRoot, "open", fail_open)
    monkeypatch.setattr(cli_mod, "connect_read_only", lambda *args, **kwargs: pytest.fail("db opened"))
    monkeypatch.setattr(cli_mod, "connect", lambda *args, **kwargs: pytest.fail("db opened"))
    with pytest.raises(SystemExit) as exc:
        main([
            "--db", str(tmp_path / "jobs.sqlite3"),
            "--artifact-root", str(tmp_path / "artifacts"),
            "autofill-review", "show", run_id,
        ])
    assert exc.value.code == 2


def test_cli_review_show_nonexistent_run_returns_fixed_error_and_does_not_mutate_db(
    tmp_path: Path, capsys
) -> None:
    db, root, _ = _make_review_show_run(tmp_path)
    before = db.read_bytes()
    assert main(["--db", str(db), "autofill-review", "--artifact-root", str(root), "show", "999"]) == 1
    err = json.loads(capsys.readouterr().err)
    assert err == {"error": {"code": "run_not_found", "message": "review run was not found"}}
    assert db.read_bytes() == before


def test_cli_review_show_missing_db_returns_database_error_without_creating_file(
    tmp_path: Path, capsys
) -> None:
    db = tmp_path / "missing.sqlite3"
    root = tmp_path / "artifacts"
    root.mkdir()
    assert not db.exists()
    assert main(["--db", str(db), "autofill-review", "--artifact-root", str(root), "show", "1"]) == 1
    err = json.loads(capsys.readouterr().err)
    assert err["error"]["code"] == "database_error"
    assert not db.exists()


def test_cli_review_show_missing_root_returns_artifact_error_without_creating_root(
    tmp_path: Path, capsys
) -> None:
    db, root, _ = _make_review_show_run(tmp_path)
    missing_root = tmp_path / "missing-artifacts"
    assert not missing_root.exists()
    assert main(["--db", str(db), "autofill-review", "--artifact-root", str(missing_root), "show", "1"]) == 1
    err = json.loads(capsys.readouterr().err)
    assert err["error"]["code"] == "artifact_root_error"
    assert not missing_root.exists()


def test_cli_review_show_db_artifact_mismatch_returns_fixed_error(
    tmp_path: Path, capsys
) -> None:
    db, root, run_id = _make_review_show_run(tmp_path)
    conn = connect(db)
    try:
        conn.execute("UPDATE application_runs SET artifact_dir='run-2' WHERE id=?", (run_id,))
        conn.commit()
    finally:
        conn.close()
    before = db.read_bytes()
    assert main(["--db", str(db), "autofill-review", "--artifact-root", str(root), "show", str(run_id)]) == 1
    err = json.loads(capsys.readouterr().err)
    assert err["error"]["code"] == "manifest_error"
    assert db.read_bytes() == before


def test_cli_review_show_symlink_run_dir_is_rejected_as_artifact_error(
    tmp_path: Path, capsys
) -> None:
    db, root, run_id = _make_review_show_run(tmp_path)
    run_dir = root / f"run-{run_id}"
    outside = tmp_path / "outside-run"
    outside.mkdir()
    shutil.rmtree(run_dir)
    run_dir.symlink_to(outside, target_is_directory=True)
    before = db.read_bytes()
    assert main(["--db", str(db), "autofill-review", "--artifact-root", str(root), "show", str(run_id)]) == 1
    err = json.loads(capsys.readouterr().err)
    assert err["error"]["code"] == "artifact_root_error"
    assert db.read_bytes() == before


def test_cli_review_show_malformed_run_json_returns_manifest_error(
    tmp_path: Path, capsys
) -> None:
    db, root, run_id = _make_review_show_run(tmp_path)
    run_dir = root / f"run-{run_id}"
    (run_dir / "run.json").write_text("{not json", encoding="utf-8")
    before = db.read_bytes()
    assert main(["--db", str(db), "autofill-review", "--artifact-root", str(root), "show", str(run_id)]) == 1
    err = json.loads(capsys.readouterr().err)
    assert err["error"]["code"] == "manifest_error"
    assert db.read_bytes() == before


def test_cli_review_show_oversized_run_json_returns_manifest_error(
    tmp_path: Path, capsys
) -> None:
    db, root, run_id = _make_review_show_run(tmp_path)
    run_dir = root / f"run-{run_id}"
    (run_dir / "run.json").write_text('"' + "x" * (131072 + 1) + '"', encoding="utf-8")
    before = db.read_bytes()
    assert main(["--db", str(db), "autofill-review", "--artifact-root", str(root), "show", str(run_id)]) == 1
    err = json.loads(capsys.readouterr().err)
    assert err["error"]["code"] == "manifest_error"
    assert db.read_bytes() == before


def test_cli_review_show_hash_changed_observation_returns_manifest_error(
    tmp_path: Path, capsys
) -> None:
    db, root, run_id = _make_review_show_run(tmp_path)
    run_dir = root / f"run-{run_id}"
    obs_path = run_dir / "observation.json"
    before = db.read_bytes()
    before_obs = obs_path.read_bytes()
    obs_path.write_text(obs_path.read_text(encoding="utf-8").replace("5", "6"), encoding="utf-8")
    assert obs_path.read_bytes() != before_obs
    assert main(["--db", str(db), "autofill-review", "--artifact-root", str(root), "show", str(run_id)]) == 1
    err = json.loads(capsys.readouterr().err)
    assert err["error"]["code"] == "manifest_error"
    assert db.read_bytes() == before


def test_cli_review_show_final_submit_calls_nonzero_returns_manifest_error(
    tmp_path: Path, capsys
) -> None:
    db, root, run_id = _make_review_show_run(tmp_path, final_submit_calls=1)
    before = db.read_bytes()
    assert main(["--db", str(db), "autofill-review", "--artifact-root", str(root), "show", str(run_id)]) == 1
    err = json.loads(capsys.readouterr().err)
    assert err["error"]["code"] == "manifest_error"
    assert db.read_bytes() == before


def test_cli_review_show_uses_read_only_db_and_no_migration_browser_or_claim(
    tmp_path: Path, capsys, monkeypatch: pytest.MonkeyPatch
) -> None:
    db, root, run_id = _make_review_show_run(tmp_path)
    monkeypatch.setattr(cli_mod, "connect", lambda *args, **kwargs: pytest.fail("writable connect called"))
    monkeypatch.setattr(
        cli_mod, "initialize_database", lambda *args, **kwargs: pytest.fail("initialize_database called")
    )
    monkeypatch.setattr(
        cli_mod.PuppeteerSession, "preflight", lambda **kwargs: pytest.fail("preflight called")
    )
    assert main(["--db", str(db), "autofill-review", "--artifact-root", str(root), "show", str(run_id)]) == 0
    assert json.loads(capsys.readouterr().out)["run_id"] == run_id


def test_application_rpc_parser_and_help_do_not_start_runtime(capsys, monkeypatch) -> None:
    monkeypatch.setattr(cli_mod.ArtifactRoot, "open", lambda *args, **kwargs: pytest.fail("artifact opened"))
    monkeypatch.setattr(cli_mod, "ApplicationRpcCoordinator", lambda *args, **kwargs: pytest.fail("coordinator started"))
    with pytest.raises(SystemExit) as exc:
        main(["application-rpc", "--help"])
    assert exc.value.code == 0
    output = capsys.readouterr().out
    assert "--resume-file" in output
    assert "--application-profile-preset" in output
    assert "--application-preferences" in output
    assert "--headed" not in output
    args = cli_mod.build_parser().parse_args(["application-rpc"])
    assert args.db == str(cli_mod.DEFAULT_DB)
    assert args.artifact_root == str(cli_mod.DEFAULT_ARTIFACT_ROOT)
    assert args.resume_file == str(cli_mod.DEFAULT_RESUME_FILE)
    assert args.ats == "auto"


def test_application_rpc_runtime_config_uses_pinned_path_and_auth_allowlist(tmp_path: Path, monkeypatch) -> None:
    executable = tmp_path / "omp"
    executable.write_text("#!/bin/sh\nexit 0\n")
    executable.chmod(0o755)
    monkeypatch.setenv("JOBS_ASSISTANT_OMP_EXECUTABLE", str(executable))
    monkeypatch.setenv("OMP_AUTH_BROKER_URL", "https://auth.example.test")
    monkeypatch.setenv("OMP_AUTH_BROKER_TOKEN", "")
    monkeypatch.setenv("OPENAI_API_KEY", "api-key-sentinel")
    monkeypatch.setenv("HTTP_PROXY", "ambient-proxy-sentinel")
    monkeypatch.setenv("PATH", "ambient-path-sentinel")
    args = cli_mod.build_parser().parse_args(
        ["application-rpc", "--omp-runtime-root", str(tmp_path / "omp-root")]
    )
    config = cli_mod._application_rpc_omp_launch_config(args)
    pinned_bin = Path(cli_mod.__file__).resolve().parents[2] / "node_modules" / ".bin"
    assert config.executable == executable
    assert str(pinned_bin) in config.trusted_path
    assert "/opt/homebrew/bin" not in config.trusted_path
    assert dict(config.auth_env) == {
        "OMP_AUTH_BROKER_URL": "https://auth.example.test",
        "OPENAI_API_KEY": "api-key-sentinel",
    }
    assert dict(config.proxy_env) == {}
    assert "ambient-" not in repr(config)
    assert all("ambient-" not in path for path in config.trusted_path)


def test_application_rpc_persists_coordinator_and_closes_on_eof(monkeypatch, capsys) -> None:
    class FakeConfig:
        def __init__(self, callback):
            self.event_callback = callback

    class FakeCoordinator:
        instances: list["FakeCoordinator"] = []

        def __init__(self, config):
            self.config = config
            self.lines: list[str] = []
            self.closed = False
            self.__class__.instances.append(self)

        async def handle(self, line: str):
            self.lines.append(line)
            if len(self.lines) == 1:
                await self.config.event_callback(
                    {
                        "run_id": 1,
                        "sequence": 1,
                        "request_id": "00000000-0000-0000-0000-000000000001",
                        "action_sequence": 0,
                        "timestamp": "2024-01-01T00:00:00Z",
                        "event_type": "started",
                        "summary_code": "ok",
                        "observation_sha256": None,
                    }
                )
            return cli_mod.build_rejected_application_response({}, error="invalid_request")

        async def close(self):
            self.closed = True

    class FakeRoot:
        def close(self):
            return None

    monkeypatch.setattr(
        cli_mod,
        "_application_rpc_service_config",
        lambda args, *, event_callback: FakeConfig(event_callback),
    )
    monkeypatch.setattr(cli_mod, "resolve_application_rpc_identity", lambda config: {})
    monkeypatch.setattr(cli_mod.ArtifactRoot, "open", lambda *args, **kwargs: FakeRoot())
    monkeypatch.setattr(cli_mod, "ApplicationRpcCoordinator", FakeCoordinator)
    monkeypatch.setattr(
        sys,
        "stdin",
        io.StringIO('{"secret":"stdout-sentinel"}\n{"request":"second"}\n'),
    )
    monkeypatch.setattr(sys, "stdout", io.StringIO())
    assert main(["application-rpc"]) == 0
    output = sys.stdout.getvalue()
    lines = [json.loads(line) for line in output.splitlines()]
    assert len(FakeCoordinator.instances) == 1
    assert FakeCoordinator.instances[0].lines == ['{"secret":"stdout-sentinel"}', '{"request":"second"}']
    assert FakeCoordinator.instances[0].closed is True
    assert lines[0]["event_type"] == "started"
    assert all(item["error"]["code"] == "invalid_request" for item in lines[1:])
    assert "stdout-sentinel" not in output
    assert "second" not in output
    assert capsys.readouterr().out == ""


def test_application_rpc_malformed_and_oversized_requests_are_public_safe() -> None:
    class FakeCoordinator:
        def __init__(self):
            self.calls = 0

        async def handle(self, line: str):
            self.calls += 1
            return cli_mod.build_rejected_application_response(line, error="invalid_request")

    async def run(value: str) -> tuple[FakeCoordinator, str]:
        coordinator = FakeCoordinator()
        output = io.StringIO()
        await cli_mod._application_rpc_loop(
            coordinator,
            input_stream=io.StringIO(value),
            output_stream=output,
        )
        return coordinator, output.getvalue()

    malformed, malformed_output = asyncio.run(run('{"secret":"malformed-sentinel"\n'))
    oversized, oversized_output = asyncio.run(
        run("x" * (cli_mod.MAX_APPLICATION_JSON_BYTES + 1) + "\n")
    )
    malformed_response = json.loads(malformed_output)
    oversized_response = json.loads(oversized_output)
    assert malformed.calls == 1
    assert malformed_response["error"]["code"] == "invalid_request"
    assert oversized.calls == 0
    assert oversized_response["error"]["code"] == "invalid_request"
    assert "sentinel" not in malformed_output
    assert "sentinel" not in oversized_output


def test_application_rpc_durability_failure_closes_transport_without_response() -> None:
    class FakeCoordinator:
        async def handle(self, _line: str):
            raise cli_mod.ApplicationRpcDurabilityError("not persisted")

    output = io.StringIO()
    with pytest.raises(cli_mod.ApplicationRpcDurabilityError):
        asyncio.run(
            cli_mod._application_rpc_loop(
                FakeCoordinator(),
                input_stream=io.StringIO('{"request":"reserved"}\n'),
                output_stream=output,
            )
        )
    assert output.getvalue() == ""


def test_application_rpc_oversized_line_discards_tail_before_next_request() -> None:
    class FakeCoordinator:
        def __init__(self):
            self.calls: list[str] = []

        async def handle(self, line: str):
            self.calls.append(line)
            return cli_mod.build_rejected_application_response(line, error="invalid_request")

    async def run() -> tuple[FakeCoordinator, str]:
        coordinator = FakeCoordinator()
        output = io.StringIO()
        value = (
            b"x" * (cli_mod.MAX_APPLICATION_JSON_BYTES + 2)
            + b'{"request":"tail"}\n'
            + b'{"request":"real"}\n'
        )
        await cli_mod._application_rpc_loop(
            coordinator,
            input_stream=io.BytesIO(value),
            output_stream=output,
        )
        return coordinator, output.getvalue()

    coordinator, output = asyncio.run(run())
    assert coordinator.calls == ['{"request":"real"}']
    assert len(output.splitlines()) == 2


def test_application_rpc_sigterm_cancels_blocked_fd_and_closes_once(monkeypatch) -> None:
    read_fd, write_fd = os.pipe()
    input_stream = io.TextIOWrapper(os.fdopen(read_fd, "rb"))
    output_stream = io.StringIO()

    class FakeRoot:
        def close(self) -> None:
            return None

    monkeypatch.setattr(sys, "stdin", input_stream)
    monkeypatch.setattr(sys, "stdout", output_stream)
    monkeypatch.setattr(
        cli_mod,
        "_application_rpc_service_config",
        lambda args, *, event_callback: object(),
    )
    monkeypatch.setattr(cli_mod, "resolve_application_rpc_identity", lambda config: {})
    monkeypatch.setattr(cli_mod.ArtifactRoot, "open", lambda *args, **kwargs: FakeRoot())
    close_calls: list[int] = []

    class FakeCoordinator:
        def __init__(self, config):
            self.config = config

        async def close(self) -> None:
            close_calls.append(1)

    removed_signals: list[signal.Signals] = []

    monkeypatch.setattr(cli_mod, "ApplicationRpcCoordinator", FakeCoordinator)
    args = cli_mod.build_parser().parse_args(["application-rpc"])

    async def run() -> None:
        ready = asyncio.Event()
        real_install = cli_mod._application_rpc_install_signal_handlers
        real_remove = cli_mod._application_rpc_remove_signal_handlers

        def remove(loop, installed):
            removed_signals.extend(installed)
            return real_remove(loop, installed)

        monkeypatch.setattr(cli_mod, "_application_rpc_remove_signal_handlers", remove)

        def install(loop, shutdown_event):
            installed = real_install(loop, shutdown_event)
            ready.set()
            return installed

        monkeypatch.setattr(cli_mod, "_application_rpc_install_signal_handlers", install)
        task = asyncio.create_task(cli_mod._application_rpc_async(args))
        await asyncio.wait_for(ready.wait(), timeout=1)
        os.kill(os.getpid(), signal.SIGTERM)
        await asyncio.wait_for(task, timeout=1)

    started = time.monotonic()
    try:
        asyncio.run(run())
    finally:
        input_stream.close()
        os.close(write_fd)
    assert time.monotonic() - started < 1
    assert close_calls == [1]
    assert tuple(removed_signals) == (signal.SIGTERM, signal.SIGHUP, signal.SIGINT)


def test_application_rpc_fd_reader_preserves_lines_and_eof() -> None:
    read_fd, write_fd = os.pipe()
    source = os.fdopen(read_fd, "rb")
    os.write(write_fd, b'{"one":1}\n{"two":2}\n')
    os.close(write_fd)

    async def run() -> tuple[object, object, object]:
        return (
            await cli_mod._application_rpc_read_line(source),
            await cli_mod._application_rpc_read_line(source),
            await cli_mod._application_rpc_read_line(source),
        )

    try:
        first, second, eof = asyncio.run(run())
    finally:
        source.close()
    assert first == b'{"one":1}\n'
    assert second == b'{"two":2}\n'
    assert eof == b""

@pytest.mark.parametrize("option", ["--application-preferences", "--applicant-description-file"])
def test_application_rpc_rejects_invalid_preclaim_inputs_before_claim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys, option: str
) -> None:
    resume_file = Path(cli_mod.__file__).resolve().parents[2] / "resume" / "Main_Resume.pdf"
    invalid = tmp_path / ("preferences.json" if option == "--application-preferences" else "description.txt")
    if option == "--application-preferences":
        invalid.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(
        cli_mod,
        "ApplicationRpcCoordinator",
        lambda *_args, **_kwargs: pytest.fail("RPC coordinator started before preclaim validation"),
    )
    monkeypatch.setattr(
        cli_mod,
        "connect",
        lambda *_args, **_kwargs: pytest.fail("database opened before preclaim validation"),
    )
    assert main(
        [
            "--db",
            str(tmp_path / "jobs.sqlite3"),
            "application-rpc",
            "--resume-file",
            str(resume_file),
            option,
            str(invalid),
        ]
    ) == 1
    assert json.loads(capsys.readouterr().err)["error"]["code"] == "invalid_input"
    assert not (tmp_path / "jobs.sqlite3").exists()
