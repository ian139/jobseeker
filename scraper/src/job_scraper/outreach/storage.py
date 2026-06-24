from __future__ import annotations

import csv
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal, cast

from job_scraper.outreach.models import (
    ContactMarkStatus,
    OutreachAction,
    OutreachConfig,
    OutreachContact,
    OutreachImportSummary,
    OutreachQueueSummary,
    OutreachStepKind,
)
from job_scraper.outreach.templates import render_message
from job_scraper.outreach.urls import normalize_linkedin_profile_url

CONTACT_STATUSES = {
    "new",
    "queued",
    "connection_requested",
    "connected",
    "replied",
    "skipped",
    "do_not_contact",
}
ACTION_STATUSES = {"pending", "sent", "skipped", "replied", "blocked"}
QUEUEABLE_CONTACT_STATUSES = {"new", "queued", "connection_requested", "connected"}
CLOSING_CONTACT_ACTION_STATUS = {
    "replied": "skipped",
    "skipped": "skipped",
    "do_not_contact": "blocked",
}
TERMINAL_CONTACT_STATUSES = {"replied", "skipped", "do_not_contact"}


class OutreachStorage:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path

    def initialize(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS outreach_contacts (
                  linkedin_profile_url TEXT PRIMARY KEY,
                  full_name TEXT NOT NULL,
                  company TEXT,
                  role_title TEXT,
                  company_domain TEXT,
                  job_id TEXT,
                  job_title TEXT,
                  source TEXT NOT NULL,
                  status TEXT NOT NULL,
                  notes TEXT,
                  created_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL,
                  last_action_at TEXT
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS outreach_actions (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  linkedin_profile_url TEXT NOT NULL,
                  step_index INTEGER NOT NULL,
                  kind TEXT NOT NULL,
                  delay_days INTEGER NOT NULL,
                  message TEXT NOT NULL,
                  due_at TEXT NOT NULL,
                  status TEXT NOT NULL,
                  sent_at TEXT,
                  created_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL,
                  FOREIGN KEY(linkedin_profile_url) REFERENCES outreach_contacts(linkedin_profile_url),
                  UNIQUE(linkedin_profile_url, step_index)
                )
                """
            )

    def upsert_contact(self, contact: OutreachContact) -> Literal["inserted", "updated"]:
        self.initialize()
        normalized_url = normalize_linkedin_profile_url(contact.linkedin_profile_url)
        if normalized_url is None:
            raise ValueError(f"Invalid LinkedIn profile URL: {contact.linkedin_profile_url}")
        full_name = _clean_required(contact.full_name)
        if full_name is None:
            raise ValueError("full_name must not be blank")

        now = _now_iso()
        incoming = {
            "linkedin_profile_url": normalized_url,
            "full_name": full_name,
            "company": _clean_optional(contact.company),
            "role_title": _clean_optional(contact.role_title),
            "company_domain": _clean_optional(contact.company_domain),
            "job_id": _clean_optional(contact.job_id),
            "job_title": _clean_optional(contact.job_title),
            "source": _clean_optional(contact.source) or "manual-csv",
            "notes": _clean_optional(contact.notes),
        }

        with self._connect() as connection:
            existing = connection.execute(
                "SELECT * FROM outreach_contacts WHERE linkedin_profile_url = ?", (normalized_url,)
            ).fetchone()
            if existing is None:
                connection.execute(
                    """
                    INSERT INTO outreach_contacts (
                        linkedin_profile_url, full_name, company, role_title, company_domain,
                        job_id, job_title, source, status, notes, created_at, updated_at, last_action_at
                    )
                    VALUES (
                        :linkedin_profile_url, :full_name, :company, :role_title, :company_domain,
                        :job_id, :job_title, :source, 'new', :notes, :created_at, :updated_at, NULL
                    )
                    """,
                    incoming | {"created_at": now, "updated_at": now},
                )
                return "inserted"

            merged = {
                key: _incoming_or_existing(incoming[key], existing[key])
                for key in (
                    "full_name",
                    "company",
                    "role_title",
                    "company_domain",
                    "job_id",
                    "job_title",
                    "source",
                    "notes",
                )
            }
            connection.execute(
                """
                UPDATE outreach_contacts
                SET full_name = :full_name,
                    company = :company,
                    role_title = :role_title,
                    company_domain = :company_domain,
                    job_id = :job_id,
                    job_title = :job_title,
                    source = :source,
                    notes = :notes,
                    updated_at = :updated_at
                WHERE linkedin_profile_url = :linkedin_profile_url
                """,
                merged | {"linkedin_profile_url": normalized_url, "updated_at": now},
            )
        return "updated"

    def import_contacts_csv(self, path: Path) -> OutreachImportSummary:
        self.initialize()
        inserted = 0
        updated = 0
        skipped = 0
        with path.open("r", encoding="utf-8", newline="") as handle:
            reader = csv.DictReader(handle)
            for row in reader:
                normalized_url = normalize_linkedin_profile_url(row.get("linkedin_profile_url", ""))
                full_name = _clean_required(row.get("full_name"))
                if normalized_url is None or full_name is None:
                    skipped += 1
                    continue

                company = _clean_optional(row.get("company"))
                company_domain = _clean_optional(row.get("company_domain"))
                job_id = _clean_optional(row.get("job_id"))
                job_title = _clean_optional(row.get("job_title"))
                if job_id is not None:
                    job = self._job_for_id(job_id)
                    if job is not None:
                        company = company or _clean_optional(job["company"])
                        company_domain = company_domain or _clean_optional(job["company_domain"])
                        job_title = job_title or _clean_optional(job["title"])

                result = self.upsert_contact(
                    OutreachContact(
                        linkedin_profile_url=normalized_url,
                        full_name=full_name,
                        company=company,
                        role_title=_clean_optional(row.get("role_title")),
                        company_domain=company_domain,
                        job_id=job_id,
                        job_title=job_title,
                        notes=_clean_optional(row.get("notes")),
                    )
                )
                if result == "inserted":
                    inserted += 1
                else:
                    updated += 1
        return OutreachImportSummary(inserted=inserted, updated=updated, skipped=skipped)

    def queue_sequence(self, config: OutreachConfig, now: datetime | None = None) -> OutreachQueueSummary:
        self.initialize()
        queued_at = _iso(now)
        created = 0
        existing_count = 0
        skipped = 0
        contacts_considered = 0

        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM outreach_contacts ORDER BY created_at ASC, linkedin_profile_url ASC").fetchall()
            for row in rows:
                if row["status"] not in QUEUEABLE_CONTACT_STATUSES:
                    skipped += 1
                    continue
                contacts_considered += 1
                contact = _contact_from_row(row)
                cumulative_delay = 0
                for step_index, step in enumerate(config.sequence):
                    cumulative_delay += step.delay_days
                    action_exists = connection.execute(
                        """
                        SELECT id FROM outreach_actions
                        WHERE linkedin_profile_url = ? AND step_index = ?
                        """,
                        (contact.linkedin_profile_url, step_index),
                    ).fetchone()
                    if action_exists is not None:
                        existing_count += 1
                        continue

                    message = render_message(step.message, contact)
                    due_at = _iso(_dt(queued_at) + timedelta(days=cumulative_delay))
                    connection.execute(
                        """
                        INSERT INTO outreach_actions (
                            linkedin_profile_url, step_index, kind, delay_days, message,
                            due_at, status, sent_at, created_at, updated_at
                        )
                        VALUES (?, ?, ?, ?, ?, ?, 'pending', NULL, ?, ?)
                        """,
                        (
                            contact.linkedin_profile_url,
                            step_index,
                            step.kind,
                            step.delay_days,
                            message,
                            due_at,
                            queued_at,
                            queued_at,
                        ),
                    )
                    created += 1
                if row["status"] == "new":
                    connection.execute(
                        """
                        UPDATE outreach_contacts
                        SET status = 'queued', updated_at = ?
                        WHERE linkedin_profile_url = ?
                        """,
                        (queued_at, contact.linkedin_profile_url),
                    )
        return OutreachQueueSummary(
            contacts_considered=contacts_considered,
            actions_created=created,
            actions_existing=existing_count,
            skipped=skipped,
        )

    def due_actions(self, limit: int, now: datetime | None = None) -> list[OutreachAction]:
        self.initialize()
        due_at = _iso(now)
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT actions.id,
                       actions.linkedin_profile_url,
                       contacts.full_name,
                       actions.kind,
                       actions.message,
                       actions.due_at,
                       actions.status,
                       actions.step_index
                FROM outreach_actions AS actions
                JOIN outreach_contacts AS contacts
                  ON contacts.linkedin_profile_url = actions.linkedin_profile_url
                WHERE actions.status = 'pending'
                  AND actions.due_at <= ?
                  AND (
                    (actions.kind = 'connect' AND contacts.status IN ('new', 'queued'))
                    OR
                    (actions.kind = 'message'
                     AND contacts.status = 'connected'
                     AND NOT EXISTS (
                       SELECT 1
                       FROM outreach_actions AS previous
                       WHERE previous.linkedin_profile_url = actions.linkedin_profile_url
                         AND previous.step_index < actions.step_index
                         AND previous.status != 'sent'
                     ))
                  )
                ORDER BY actions.due_at ASC, actions.id ASC
                LIMIT ?
                """,
                (due_at, limit),
            ).fetchall()
        return [_action_from_row(row) for row in rows]

    def mark_action(
        self,
        action_id: int,
        status: Literal["sent", "skipped", "replied", "blocked"],
        now: datetime | None = None,
    ) -> OutreachAction:
        if status not in {"sent", "skipped", "replied", "blocked"}:
            raise ValueError(f"Unknown outreach action status: {status}")
        self.initialize()
        marked_at = _iso(now)
        with self._connect() as connection:
            action = connection.execute("SELECT * FROM outreach_actions WHERE id = ?", (action_id,)).fetchone()
            if action is None:
                raise ValueError(f"Unknown outreach action ID: {action_id}")

            contact = connection.execute(
                "SELECT status FROM outreach_contacts WHERE linkedin_profile_url = ?",
                (action["linkedin_profile_url"],),
            ).fetchone()
            if contact is None:
                raise ValueError(f"Unknown outreach contact: {action['linkedin_profile_url']}")
            if status == "sent":
                if action["status"] != "pending":
                    raise ValueError(
                        f"Cannot mark outreach action {action_id} sent because it is already {action['status']}"
                    )
                if contact["status"] in TERMINAL_CONTACT_STATUSES:
                    raise ValueError(
                        f"Cannot mark outreach action {action_id} sent because contact is {contact['status']}"
                    )

            sent_at = marked_at if status == "sent" else action["sent_at"]
            connection.execute(
                """
                UPDATE outreach_actions
                SET status = ?, sent_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (status, sent_at, marked_at, action_id),
            )
            if status == "sent":
                contact_status = (
                    "connection_requested"
                    if action["kind"] == "connect" and contact["status"] in {"new", "queued"}
                    else None
                )
                if contact_status is None:
                    connection.execute(
                        """
                        UPDATE outreach_contacts
                        SET last_action_at = ?, updated_at = ?
                        WHERE linkedin_profile_url = ?
                        """,
                        (marked_at, marked_at, action["linkedin_profile_url"]),
                    )
                else:
                    connection.execute(
                        """
                        UPDATE outreach_contacts
                        SET status = ?, last_action_at = ?, updated_at = ?
                        WHERE linkedin_profile_url = ?
                        """,
                        (contact_status, marked_at, marked_at, action["linkedin_profile_url"]),
                    )
                self._reschedule_next_pending(connection, str(action["linkedin_profile_url"]), _dt(marked_at))
            elif status == "replied":
                self._close_contact(connection, str(action["linkedin_profile_url"]), "replied", "skipped", marked_at)
            elif status == "skipped":
                self._close_contact(connection, str(action["linkedin_profile_url"]), "skipped", "skipped", marked_at)
            elif status == "blocked":
                self._close_contact(connection, str(action["linkedin_profile_url"]), "do_not_contact", "blocked", marked_at)

            updated = self._action_for_id(connection, action_id)
            if updated is None:
                raise ValueError(f"Unknown outreach action ID: {action_id}")
            return updated

    def mark_contact(
        self,
        linkedin_profile_url: str,
        status: ContactMarkStatus,
        now: datetime | None = None,
    ) -> None:
        if status not in {"connected", "replied", "skipped", "do_not_contact"}:
            raise ValueError(f"Unknown outreach contact status: {status}")
        self.initialize()
        normalized_url = normalize_linkedin_profile_url(linkedin_profile_url)
        if normalized_url is None:
            raise ValueError(f"Invalid LinkedIn profile URL: {linkedin_profile_url}")
        marked_at = _iso(now)
        with self._connect() as connection:
            existing = connection.execute(
                "SELECT linkedin_profile_url FROM outreach_contacts WHERE linkedin_profile_url = ?", (normalized_url,)
            ).fetchone()
            if existing is None:
                raise ValueError(f"Unknown outreach contact: {normalized_url}")
            connection.execute(
                """
                UPDATE outreach_contacts
                SET status = ?, updated_at = ?
                WHERE linkedin_profile_url = ?
                """,
                (status, marked_at, normalized_url),
            )
            if status == "connected":
                connection.execute(
                    """
                    UPDATE outreach_actions
                    SET status = 'sent', sent_at = COALESCE(sent_at, ?), updated_at = ?
                    WHERE linkedin_profile_url = ?
                      AND kind = 'connect'
                      AND status = 'pending'
                    """,
                    (marked_at, marked_at, normalized_url),
                )
                self._reschedule_next_pending(connection, normalized_url, _dt(marked_at))
            closing_status = CLOSING_CONTACT_ACTION_STATUS.get(status)
            if closing_status is not None:
                connection.execute(
                    """
                    UPDATE outreach_actions
                    SET status = ?, updated_at = ?
                    WHERE linkedin_profile_url = ? AND status = 'pending'
                    """,
                    (closing_status, marked_at, normalized_url),
                )

    def _job_for_id(self, job_id: str) -> sqlite3.Row | None:
        with self._connect() as connection:
            table = connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'jobs'"
            ).fetchone()
            if table is None:
                return None
            return connection.execute(
                "SELECT company, company_domain, title FROM jobs WHERE theirstack_id = ?", (job_id,)
            ).fetchone()

    def _reschedule_next_pending(self, connection: sqlite3.Connection, linkedin_profile_url: str, sent_at: datetime) -> None:
        next_action = connection.execute(
            """
            SELECT id, delay_days
            FROM outreach_actions
            WHERE linkedin_profile_url = ? AND status = 'pending'
            ORDER BY step_index ASC, id ASC
            LIMIT 1
            """,
            (linkedin_profile_url,),
        ).fetchone()
        if next_action is None:
            return
        due_at = _iso(sent_at + timedelta(days=int(next_action["delay_days"])))
        connection.execute(
            "UPDATE outreach_actions SET due_at = ?, updated_at = ? WHERE id = ?",
            (due_at, _iso(sent_at), next_action["id"]),
        )

    def _close_contact(
        self,
        connection: sqlite3.Connection,
        linkedin_profile_url: str,
        contact_status: str,
        remaining_action_status: str,
        marked_at: str,
    ) -> None:
        connection.execute(
            """
            UPDATE outreach_contacts
            SET status = ?, updated_at = ?
            WHERE linkedin_profile_url = ?
            """,
            (contact_status, marked_at, linkedin_profile_url),
        )
        connection.execute(
            """
            UPDATE outreach_actions
            SET status = ?, updated_at = ?
            WHERE linkedin_profile_url = ? AND status = 'pending'
            """,
            (remaining_action_status, marked_at, linkedin_profile_url),
        )

    def _action_for_id(self, connection: sqlite3.Connection, action_id: int) -> OutreachAction | None:
        row = connection.execute(
            """
            SELECT actions.id,
                   actions.linkedin_profile_url,
                   contacts.full_name,
                   actions.kind,
                   actions.message,
                   actions.due_at,
                   actions.status,
                   actions.step_index
            FROM outreach_actions AS actions
            JOIN outreach_contacts AS contacts
              ON contacts.linkedin_profile_url = actions.linkedin_profile_url
            WHERE actions.id = ?
            """,
            (action_id,),
        ).fetchone()
        return _action_from_row(row) if row is not None else None

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path)
        connection.row_factory = sqlite3.Row
        return connection


def _action_from_row(row: sqlite3.Row) -> OutreachAction:
    return OutreachAction(
        id=int(row["id"]),
        linkedin_profile_url=str(row["linkedin_profile_url"]),
        full_name=str(row["full_name"]),
        kind=cast(OutreachStepKind, str(row["kind"])),
        message=str(row["message"]),
        due_at=str(row["due_at"]),
        status=str(row["status"]),
        step_index=int(row["step_index"]),
    )


def _contact_from_row(row: sqlite3.Row) -> OutreachContact:
    return OutreachContact(
        linkedin_profile_url=str(row["linkedin_profile_url"]),
        full_name=str(row["full_name"]),
        company=_clean_optional(row["company"]),
        role_title=_clean_optional(row["role_title"]),
        company_domain=_clean_optional(row["company_domain"]),
        job_id=_clean_optional(row["job_id"]),
        job_title=_clean_optional(row["job_title"]),
        source=str(row["source"]),
        notes=_clean_optional(row["notes"]),
    )


def _clean_optional(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _clean_required(value: Any) -> str | None:
    return _clean_optional(value)


def _incoming_or_existing(incoming: Any, existing: Any) -> str | None:
    clean_incoming = _clean_optional(incoming)
    if clean_incoming is not None:
        return clean_incoming
    return _clean_optional(existing)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _iso(value: datetime | None) -> str:
    if value is None:
        return _now_iso()
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat(timespec="seconds")


def _dt(value: str) -> datetime:
    return datetime.fromisoformat(value)
