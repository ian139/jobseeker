from __future__ import annotations

import sqlite3
from pathlib import Path

from job_scraper.job_parser import parse_theirstack_job
from job_scraper.storage import JobStorage


def test_initialize_migrates_legacy_jobs_table_for_parsed_digest_columns(tmp_path: Path) -> None:
    db_path = tmp_path / "jobs.sqlite3"
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            CREATE TABLE jobs (
                theirstack_id TEXT PRIMARY KEY,
                title TEXT,
                company TEXT,
                company_domain TEXT,
                country_code TEXT,
                remote INTEGER,
                date_posted TEXT,
                discovered_at TEXT,
                url TEXT,
                source_url TEXT,
                final_url TEXT,
                min_annual_salary_usd REAL,
                max_annual_salary_usd REAL,
                job_description TEXT,
                job_seniority TEXT,
                employment_statuses_json TEXT NOT NULL DEFAULT '[]',
                skills_json TEXT NOT NULL DEFAULT '[]',
                responsibilities_json TEXT NOT NULL DEFAULT '[]',
                requirements_json TEXT NOT NULL DEFAULT '[]',
                benefits_json TEXT NOT NULL DEFAULT '[]',
                raw_json TEXT NOT NULL,
                first_seen_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL
            )
            """
        )

    storage = JobStorage(db_path)
    storage.initialize()
    raw = {
        "id": "legacy-intern-1",
        "job_title": "Data Engineering Intern",
        "company_name": "Acme",
        "company_domain": "acme.example",
        "job_country_code": "US",
        "remote": "remote",
        "date_posted": "2026-06-23",
        "discovered_at": "2026-06-23T12:00:00+00:00",
        "url": "https://www.linkedin.com/jobs/view/123",
        "source_url": "https://www.linkedin.com/jobs/view/123",
        "final_url": "https://acme.example/jobs/123",
        "skills": ["Python"],
    }
    parsed = parse_theirstack_job(raw)
    assert parsed is not None

    storage.upsert_job(parsed)
    job = storage.get_job("legacy-intern-1")

    assert job is not None
    assert job.role_kind == "internship"
    assert job.digest["workplace"] == "Remote"
    assert job.skills == ("Python",)
