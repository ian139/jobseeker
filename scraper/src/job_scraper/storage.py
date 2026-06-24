from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, Sequence

LOGGER = logging.getLogger(__name__)

UpsertStatus = Literal["inserted", "updated", "skipped"]


@dataclass(frozen=True)
class UpsertResult:
    status: UpsertStatus
    discovered_at: str | None = None


ApplicationStatus = Literal["queued", "tailored", "applied", "interviewing", "offer", "rejected", "withdrawn"]
APPLICATION_STATUSES: tuple[str, ...] = (
    "queued",
    "tailored",
    "applied",
    "interviewing",
    "offer",
    "rejected",
    "withdrawn",
)

ApplicationAttemptStatus = Literal["prepared", "submitted", "blocked", "failed"]
APPLICATION_ATTEMPT_STATUSES: tuple[str, ...] = ("prepared", "submitted", "blocked", "failed")


@dataclass(frozen=True)
class JobRecord:
    theirstack_id: str
    title: str | None
    company: str | None
    company_domain: str | None
    country_code: str | None
    remote: int | None
    date_posted: str | None
    discovered_at: str | None
    url: str | None
    source_url: str | None
    final_url: str | None
    raw: dict[str, Any]


@dataclass(frozen=True)
class ApplicationRecord:
    id: int
    theirstack_id: str
    status: str
    notes: str
    contact_name: str | None
    contact_email: str | None
    applied_at: str | None
    follow_up_at: str | None
    resume_path: str | None
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class ApplicationAttemptRecord:
    id: int
    application_id: int
    theirstack_id: str
    target_url: str
    status: str
    submitted: int
    message: str
    fields_filled_json: str
    resume_uploaded: int
    created_at: str

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
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS applications (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    theirstack_id TEXT NOT NULL UNIQUE,
                    status TEXT NOT NULL DEFAULT 'queued',
                    notes TEXT NOT NULL DEFAULT '',
                    contact_name TEXT,
                    contact_email TEXT,
                    applied_at TEXT,
                    follow_up_at TEXT,
                    resume_path TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(theirstack_id) REFERENCES jobs(theirstack_id)
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS application_attempts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    application_id INTEGER NOT NULL,
                    theirstack_id TEXT NOT NULL,
                    target_url TEXT NOT NULL,
                    status TEXT NOT NULL,
                    submitted INTEGER NOT NULL DEFAULT 0,
                    message TEXT NOT NULL DEFAULT '',
                    fields_filled_json TEXT NOT NULL DEFAULT '[]',
                    resume_uploaded INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(application_id) REFERENCES applications(id),
                    FOREIGN KEY(theirstack_id) REFERENCES jobs(theirstack_id)
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS resume_versions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    application_id INTEGER NOT NULL,
                    resume_markdown TEXT NOT NULL,
                    selected_bullets_json TEXT NOT NULL,
                    keywords_json TEXT NOT NULL,
                    llm_used INTEGER NOT NULL DEFAULT 0,
                    model TEXT,
                    output_path TEXT,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(application_id) REFERENCES applications(id)
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

    def list_jobs(self, *, limit: int = 100) -> list[JobRecord]:
        self.initialize()
        if limit < 1:
            raise ValueError("Job list limit must be at least 1")

        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT theirstack_id, title, company, company_domain, country_code, remote,
                       date_posted, discovered_at, url, source_url, final_url, raw_json
                FROM jobs
                ORDER BY COALESCE(discovered_at, date_posted, final_url, url, theirstack_id) DESC,
                         theirstack_id ASC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [_job_record_from_row(row) for row in rows]

    def get_job(self, theirstack_id: str) -> JobRecord | None:
        self.initialize()
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT theirstack_id, title, company, company_domain, country_code, remote,
                       date_posted, discovered_at, url, source_url, final_url, raw_json
                FROM jobs
                WHERE theirstack_id = ?
                """,
                (theirstack_id,),
            ).fetchone()
        if row is None:
            return None
        return _job_record_from_row(row)

    def ensure_application(self, theirstack_id: str, *, notes: str = "") -> ApplicationRecord:
        if self.get_job(theirstack_id) is None:
            raise ValueError(f"Unknown job id: {theirstack_id}")

        self.initialize()
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO applications (theirstack_id, status, notes, created_at, updated_at)
                VALUES (?, 'queued', ?, ?, ?)
                ON CONFLICT(theirstack_id) DO NOTHING
                """,
                (theirstack_id, notes, now, now),
            )
            row = connection.execute(
                """
                SELECT id, theirstack_id, status, notes, contact_name, contact_email,
                       applied_at, follow_up_at, resume_path, created_at, updated_at
                FROM applications
                WHERE theirstack_id = ?
                """,
                (theirstack_id,),
            ).fetchone()
        if row is None:
            raise ValueError(f"Unknown job id: {theirstack_id}")
        return _application_record_from_row(row)

    def update_application(
        self,
        theirstack_id: str,
        *,
        status: ApplicationStatus | None = None,
        notes: str | None = None,
        contact_name: str | None = None,
        contact_email: str | None = None,
        applied_at: str | None = None,
        follow_up_at: str | None = None,
        resume_path: str | None = None,
    ) -> ApplicationRecord:
        if status is not None and status not in APPLICATION_STATUSES:
            raise ValueError(f"Invalid application status: {status}")

        self.ensure_application(theirstack_id)
        fields: dict[str, Any] = {
            "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        for name, value in (
            ("status", status),
            ("notes", notes),
            ("contact_name", contact_name),
            ("contact_email", contact_email),
            ("applied_at", applied_at),
            ("follow_up_at", follow_up_at),
            ("resume_path", resume_path),
        ):
            if value is not None:
                fields[name] = value

        assignments = ", ".join(f"{name} = :{name}" for name in fields)
        fields["theirstack_id"] = theirstack_id
        with self._connect() as connection:
            connection.execute(f"UPDATE applications SET {assignments} WHERE theirstack_id = :theirstack_id", fields)
            row = connection.execute(
                """
                SELECT id, theirstack_id, status, notes, contact_name, contact_email,
                       applied_at, follow_up_at, resume_path, created_at, updated_at
                FROM applications
                WHERE theirstack_id = ?
                """,
                (theirstack_id,),
            ).fetchone()
        if row is None:
            raise ValueError(f"Unknown job id: {theirstack_id}")
        return _application_record_from_row(row)

    def list_applications(self, *, status: ApplicationStatus | None = None) -> list[ApplicationRecord]:
        if status is not None and status not in APPLICATION_STATUSES:
            raise ValueError(f"Invalid application status: {status}")

        self.initialize()
        query = """
            SELECT id, theirstack_id, status, notes, contact_name, contact_email,
                   applied_at, follow_up_at, resume_path, created_at, updated_at
            FROM applications
        """
        params: tuple[str, ...] = ()
        if status is not None:
            query += " WHERE status = ?"
            params = (status,)
        query += " ORDER BY updated_at DESC, id DESC"
        with self._connect() as connection:
            rows = connection.execute(query, params).fetchall()
        return [_application_record_from_row(row) for row in rows]

    def record_application_attempt(
        self,
        theirstack_id: str,
        *,
        target_url: str,
        status: ApplicationAttemptStatus,
        submitted: bool,
        message: str = "",
        fields_filled: Sequence[str] = (),
        resume_uploaded: bool = False,
    ) -> ApplicationAttemptRecord:
        if status not in APPLICATION_ATTEMPT_STATUSES:
            raise ValueError(f"Invalid application attempt status: {status}")

        application = self.ensure_application(theirstack_id)
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        fields_filled_json = json.dumps(list(fields_filled), separators=(",", ":"))
        with self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO application_attempts (
                    application_id,
                    theirstack_id,
                    target_url,
                    status,
                    submitted,
                    message,
                    fields_filled_json,
                    resume_uploaded,
                    created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    application.id,
                    theirstack_id,
                    target_url,
                    status,
                    int(submitted),
                    message,
                    fields_filled_json,
                    int(resume_uploaded),
                    now,
                ),
            )
            row = connection.execute(
                """
                SELECT id, application_id, theirstack_id, target_url, status, submitted,
                       message, fields_filled_json, resume_uploaded, created_at
                FROM application_attempts
                WHERE id = ?
                """,
                (int(cursor.lastrowid),),
            ).fetchone()
        if row is None:
            raise ValueError(f"Application attempt insert failed for job id: {theirstack_id}")
        return _application_attempt_record_from_row(row)

    def list_application_attempts(self, theirstack_id: str | None = None) -> list[ApplicationAttemptRecord]:
        self.initialize()
        query = """
            SELECT id, application_id, theirstack_id, target_url, status, submitted,
                   message, fields_filled_json, resume_uploaded, created_at
            FROM application_attempts
        """
        params: tuple[str, ...] = ()
        if theirstack_id is not None:
            query += " WHERE theirstack_id = ?"
            params = (theirstack_id,)
        query += " ORDER BY created_at DESC, id DESC"
        with self._connect() as connection:
            rows = connection.execute(query, params).fetchall()
        return [_application_attempt_record_from_row(row) for row in rows]

    def save_resume_version(
        self,
        *,
        application_id: int,
        resume_markdown: str,
        selected_bullets_json: str,
        keywords_json: str,
        llm_used: bool,
        model: str | None,
        output_path: str | None,
    ) -> int:
        self.initialize()
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO resume_versions (
                    application_id, resume_markdown, selected_bullets_json, keywords_json,
                    llm_used, model, output_path, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    application_id,
                    resume_markdown,
                    selected_bullets_json,
                    keywords_json,
                    1 if llm_used else 0,
                    model,
                    output_path,
                    now,
                ),
            )
        return int(cursor.lastrowid)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path)
        connection.execute("PRAGMA foreign_keys = ON")
        connection.row_factory = sqlite3.Row
        return connection


def _job_record_from_row(row: sqlite3.Row) -> JobRecord:
    try:
        raw = json.loads(str(row["raw_json"]))
    except json.JSONDecodeError as exc:
        raise ValueError(f"Stored job raw_json is invalid for {row['theirstack_id']}") from exc
    if not isinstance(raw, dict):
        raise ValueError(f"Stored job raw_json is invalid for {row['theirstack_id']}")
    return JobRecord(
        theirstack_id=str(row["theirstack_id"]),
        title=_string_or_none(row["title"]),
        company=_string_or_none(row["company"]),
        company_domain=_string_or_none(row["company_domain"]),
        country_code=_string_or_none(row["country_code"]),
        remote=int(row["remote"]) if row["remote"] is not None else None,
        date_posted=_string_or_none(row["date_posted"]),
        discovered_at=_string_or_none(row["discovered_at"]),
        url=_string_or_none(row["url"]),
        source_url=_string_or_none(row["source_url"]),
        final_url=_string_or_none(row["final_url"]),
        raw=raw,
    )


def _application_record_from_row(row: sqlite3.Row) -> ApplicationRecord:
    return ApplicationRecord(
        id=int(row["id"]),
        theirstack_id=str(row["theirstack_id"]),
        status=str(row["status"]),
        notes=str(row["notes"]),
        contact_name=_string_or_none(row["contact_name"]),
        contact_email=_string_or_none(row["contact_email"]),
        applied_at=_string_or_none(row["applied_at"]),
        follow_up_at=_string_or_none(row["follow_up_at"]),
        resume_path=_string_or_none(row["resume_path"]),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
    )


def _application_attempt_record_from_row(row: sqlite3.Row) -> ApplicationAttemptRecord:
    return ApplicationAttemptRecord(
        id=int(row["id"]),
        application_id=int(row["application_id"]),
        theirstack_id=str(row["theirstack_id"]),
        target_url=str(row["target_url"]),
        status=str(row["status"]),
        submitted=int(row["submitted"]),
        message=str(row["message"]),
        fields_filled_json=str(row["fields_filled_json"]),
        resume_uploaded=int(row["resume_uploaded"]),
        created_at=str(row["created_at"]),
    )


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
