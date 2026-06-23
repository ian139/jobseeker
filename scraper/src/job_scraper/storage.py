from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

LOGGER = logging.getLogger(__name__)

UpsertStatus = Literal["inserted", "updated", "skipped"]


@dataclass(frozen=True)
class UpsertResult:
    status: UpsertStatus
    discovered_at: str | None = None


class JobStorage:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path

    def initialize(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS jobs (
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
                    raw_json TEXT NOT NULL,
                    first_seen_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS sync_state (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
                """
            )

    def get_state(self, key: str) -> str | None:
        self.initialize()
        with self._connect() as connection:
            row = connection.execute("SELECT value FROM sync_state WHERE key = ?", (key,)).fetchone()
        return str(row["value"]) if row else None

    def set_state(self, key: str, value: str) -> None:
        self.initialize()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO sync_state (key, value)
                VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (key, value),
            )

    def record_run(self, checkpoint_after: str | None) -> None:
        self.initialize()
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO sync_state (key, value)
                VALUES ('last_run_at', ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (now,),
            )
            if checkpoint_after is not None:
                connection.execute(
                    """
                    INSERT INTO sync_state (key, value)
                    VALUES ('last_successful_discovered_at', ?)
                    ON CONFLICT(key) DO UPDATE SET value = excluded.value
                    """,
                    (checkpoint_after,),
                )

    def upsert_job(self, job: dict[str, Any]) -> UpsertResult:
        self.initialize()
        theirstack_id = job.get("id")
        if theirstack_id in (None, ""):
            LOGGER.warning(
                "Skipping TheirStack job without id: title=%r company=%r",
                _get_field(job, "job_title", "title"),
                _company_name(job),
            )
            return UpsertResult(status="skipped")

        theirstack_id_text = str(theirstack_id)
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        raw_json = json.dumps(job, separators=(",", ":"), sort_keys=True)
        row = _normalized_row(job, theirstack_id_text, raw_json, now)

        with self._connect() as connection:
            existing = connection.execute(
                "SELECT theirstack_id FROM jobs WHERE theirstack_id = ?", (theirstack_id_text,)
            ).fetchone()
            connection.execute(
                """
                INSERT INTO jobs (
                    theirstack_id, title, company, company_domain, country_code, remote,
                    date_posted, discovered_at, url, source_url, final_url,
                    min_annual_salary_usd, max_annual_salary_usd, raw_json,
                    first_seen_at, last_seen_at
                )
                VALUES (
                    :theirstack_id, :title, :company, :company_domain, :country_code, :remote,
                    :date_posted, :discovered_at, :url, :source_url, :final_url,
                    :min_annual_salary_usd, :max_annual_salary_usd, :raw_json,
                    :first_seen_at, :last_seen_at
                )
                ON CONFLICT(theirstack_id) DO UPDATE SET
                    title = excluded.title,
                    company = excluded.company,
                    company_domain = excluded.company_domain,
                    country_code = excluded.country_code,
                    remote = excluded.remote,
                    date_posted = excluded.date_posted,
                    discovered_at = excluded.discovered_at,
                    url = excluded.url,
                    source_url = excluded.source_url,
                    final_url = excluded.final_url,
                    min_annual_salary_usd = excluded.min_annual_salary_usd,
                    max_annual_salary_usd = excluded.max_annual_salary_usd,
                    raw_json = excluded.raw_json,
                    last_seen_at = excluded.last_seen_at
                """,
                row,
            )
        status: UpsertStatus = "updated" if existing else "inserted"
        return UpsertResult(status=status, discovered_at=_string_or_none(job.get("discovered_at")))

    def count_jobs(self) -> int:
        self.initialize()
        with self._connect() as connection:
            row = connection.execute("SELECT COUNT(*) AS count FROM jobs").fetchone()
        return int(row["count"])

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path)
        connection.row_factory = sqlite3.Row
        return connection


def _normalized_row(job: dict[str, Any], theirstack_id: str, raw_json: str, now: str) -> dict[str, Any]:
    return {
        "theirstack_id": theirstack_id,
        "title": _string_or_none(_get_field(job, "job_title", "title")),
        "company": _company_name(job),
        "company_domain": _string_or_none(_get_field(job, "company_domain", "domain")),
        "country_code": _string_or_none(_get_field(job, "job_country_code", "country_code")),
        "remote": _remote_value(job.get("remote")),
        "date_posted": _string_or_none(_get_field(job, "date_posted", "posted_at")),
        "discovered_at": _string_or_none(job.get("discovered_at")),
        "url": _string_or_none(job.get("url")),
        "source_url": _string_or_none(job.get("source_url")),
        "final_url": _string_or_none(job.get("final_url")),
        "min_annual_salary_usd": _number_or_none(job.get("min_annual_salary_usd")),
        "max_annual_salary_usd": _number_or_none(job.get("max_annual_salary_usd")),
        "raw_json": raw_json,
        "first_seen_at": now,
        "last_seen_at": now,
    }


def _get_field(job: dict[str, Any], *names: str) -> Any:
    for name in names:
        value = job.get(name)
        if value is not None:
            return value
    return None


def _company_name(job: dict[str, Any]) -> str | None:
    company = job.get("company")
    if isinstance(company, dict):
        return _string_or_none(company.get("name"))
    if company is not None:
        return _string_or_none(company)
    return _string_or_none(job.get("company_name"))


def _string_or_none(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)


def _number_or_none(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _remote_value(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"true", "yes", "remote"}:
            return 1
        if lowered in {"false", "no", "onsite", "on-site", "hybrid"}:
            return 0
    return None
