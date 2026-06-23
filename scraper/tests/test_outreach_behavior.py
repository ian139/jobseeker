from __future__ import annotations

import csv
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from job_scraper.cli import main
from job_scraper.outreach import OutreachConfig, OutreachContact, OutreachLimits, OutreachStep
from job_scraper.outreach_storage import OutreachStorage
from job_scraper.storage import JobStorage


NOW = datetime(2026, 6, 23, 12, 0, 0, tzinfo=timezone.utc)


def test_import_contacts_normalizes_urls_enriches_from_jobs_and_skips_bad_rows(tmp_path: Path) -> None:
    db_path = tmp_path / "jobs.sqlite3"
    JobStorage(db_path).upsert_job(_job("job-1"))
    csv_path = tmp_path / "contacts.csv"
    _write_contacts_csv(
        csv_path,
        [
            {
                "linkedin_profile_url": "linkedin.com/in/Ada-Lovelace/?trk=public_profile",
                "full_name": "Ada Lovelace",
                "company": "",
                "role_title": "Engineering Lead",
                "company_domain": "",
                "job_id": "job-1",
                "job_title": "",
                "notes": "priority",
            },
            {
                "linkedin_profile_url": "https://www.linkedin.com/company/acme",
                "full_name": "Bad Url",
                "company": "Acme",
                "role_title": "",
                "company_domain": "",
                "job_id": "",
                "job_title": "",
                "notes": "",
            },
            {
                "linkedin_profile_url": "linkedin.com/in/missing-name",
                "full_name": " ",
                "company": "Acme",
                "role_title": "",
                "company_domain": "",
                "job_id": "",
                "job_title": "",
                "notes": "",
            },
        ],
    )

    summary = OutreachStorage(db_path).import_contacts_csv(csv_path)

    assert summary.inserted == 1
    assert summary.updated == 0
    assert summary.skipped == 2
    row = _contact_row(db_path, "https://www.linkedin.com/in/ada-lovelace")
    assert row["full_name"] == "Ada Lovelace"
    assert row["company"] == "Acme"
    assert row["company_domain"] == "acme.example"
    assert row["job_title"] == "Fall Software Co-op"


def test_queue_sequence_returns_connect_before_message_until_connected(tmp_path: Path) -> None:
    db_path = tmp_path / "jobs.sqlite3"
    storage = OutreachStorage(db_path)
    storage.upsert_contact(
        OutreachContact(
            linkedin_profile_url="www.linkedin.com/in/Ada-Lovelace/",
            full_name="Ada Lovelace",
            company="Acme",
            job_title="Fall Software Co-op",
        )
    )
    config = _two_step_config()

    summary = storage.queue_sequence(config, now=NOW)

    assert summary.contacts_considered == 1
    assert summary.actions_created == 2
    assert summary.actions_existing == 0
    due = storage.due_actions(limit=10, now=NOW)
    assert [action.kind for action in due] == ["connect"]
    storage.mark_action(due[0].id, "sent", now=NOW)
    assert storage.due_actions(limit=10, now=NOW) == []

    storage.mark_contact("linkedin.com/in/ada-lovelace", "connected", now=NOW)

    due_after_connected = storage.due_actions(limit=10, now=NOW)
    assert [action.kind for action in due_after_connected] == ["message"]
    assert due_after_connected[0].message == "Thanks Ada from Acme about Fall Software Co-op"


def test_mark_replied_or_blocked_stops_remaining_actions(tmp_path: Path) -> None:
    db_path = tmp_path / "jobs.sqlite3"
    storage = OutreachStorage(db_path)
    storage.upsert_contact(OutreachContact("linkedin.com/in/replied-person", "Replied Person"))
    storage.upsert_contact(OutreachContact("linkedin.com/in/blocked-person", "Blocked Person"))
    storage.queue_sequence(_two_step_config(), now=NOW)
    replied_first_action = _action_id(db_path, "https://www.linkedin.com/in/replied-person", 0)
    blocked_first_action = _action_id(db_path, "https://www.linkedin.com/in/blocked-person", 0)

    storage.mark_action(replied_first_action, "replied", now=NOW)
    storage.mark_action(blocked_first_action, "blocked", now=NOW)

    assert _contact_row(db_path, "https://www.linkedin.com/in/replied-person")["status"] == "replied"
    assert _action_status(db_path, "https://www.linkedin.com/in/replied-person", 0) == "replied"
    assert _action_status(db_path, "https://www.linkedin.com/in/replied-person", 1) == "skipped"
    assert _contact_row(db_path, "https://www.linkedin.com/in/blocked-person")["status"] == "do_not_contact"
    assert _action_status(db_path, "https://www.linkedin.com/in/blocked-person", 0) == "blocked"
    assert _action_status(db_path, "https://www.linkedin.com/in/blocked-person", 1) == "blocked"


def test_cli_outreach_next_prints_tsv_and_open_uses_first_due_url(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    db_path = tmp_path / "jobs.sqlite3"
    monkeypatch.setenv("JOB_SCRAPER_DB_PATH", str(db_path))
    storage = OutreachStorage(db_path)
    storage.upsert_contact(OutreachContact("linkedin.com/in/ada-lovelace", "Ada Lovelace", company="Acme"))
    storage.upsert_contact(OutreachContact("linkedin.com/in/grace-hopper", "Grace Hopper", company="Navy"))
    storage.queue_sequence(_two_step_config(), now=NOW)
    opened: list[str] = []
    monkeypatch.setattr("job_scraper.cli.webbrowser.open", lambda url: opened.append(url))

    exit_code = main(["outreach", "next", "--limit", "5", "--open"])

    assert exit_code == 0
    output = capsys.readouterr().out.strip().splitlines()
    assert len(output) == 2
    first_fields = output[0].split("\t")
    assert first_fields[1] == "connect"
    assert first_fields[3] == "https://www.linkedin.com/in/ada-lovelace"
    assert first_fields[4] == "Connect Ada at Acme"
    assert opened == ["https://www.linkedin.com/in/ada-lovelace"]


def _two_step_config() -> OutreachConfig:
    return OutreachConfig(
        sequence=[
            OutreachStep(kind="connect", delay_days=0, message="Connect {first_name} at {company}"),
            OutreachStep(kind="message", delay_days=0, message="Thanks {first_name} from {company} about {job_title}"),
        ],
        limits=OutreachLimits(next_limit=10),
    )


def _write_contacts_csv(path: Path, rows: list[dict[str, str]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "linkedin_profile_url",
                "full_name",
                "company",
                "role_title",
                "company_domain",
                "job_id",
                "job_title",
                "notes",
            ],
        )
        writer.writeheader()
        writer.writerows(rows)


def _contact_row(db_path: Path, linkedin_profile_url: str) -> sqlite3.Row:
    with _connection(db_path) as connection:
        row = connection.execute(
            "SELECT * FROM outreach_contacts WHERE linkedin_profile_url = ?", (linkedin_profile_url,)
        ).fetchone()
    assert row is not None
    return row


def _action_id(db_path: Path, linkedin_profile_url: str, step_index: int) -> int:
    with _connection(db_path) as connection:
        row = connection.execute(
            """
            SELECT id FROM outreach_actions
            WHERE linkedin_profile_url = ? AND step_index = ?
            """,
            (linkedin_profile_url, step_index),
        ).fetchone()
    assert row is not None
    return int(row["id"])


def _action_status(db_path: Path, linkedin_profile_url: str, step_index: int) -> str:
    with _connection(db_path) as connection:
        row = connection.execute(
            """
            SELECT status FROM outreach_actions
            WHERE linkedin_profile_url = ? AND step_index = ?
            """,
            (linkedin_profile_url, step_index),
        ).fetchone()
    assert row is not None
    return str(row["status"])


def _connection(db_path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    return connection


def _job(job_id: str) -> dict[str, object]:
    return {
        "id": job_id,
        "job_title": "Fall Software Co-op",
        "company_name": "Acme",
        "company_domain": "acme.example",
        "job_country_code": "US",
        "remote": False,
        "date_posted": "2026-06-23",
        "discovered_at": "2026-06-23T12:00:00+00:00",
        "url": "https://www.linkedin.com/jobs/view/123",
        "source_url": "https://www.linkedin.com/jobs/view/123",
        "final_url": "https://acme.example/jobs/123",
    }
