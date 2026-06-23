from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from string import Formatter
from typing import Literal
from urllib.parse import urlparse

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator


OutreachStepKind = Literal["connect", "message"]
ActionStatus = Literal["sent", "skipped", "replied", "blocked"]
ContactMarkStatus = Literal["connected", "replied", "skipped", "do_not_contact"]


class OutreachStep(BaseModel):
    kind: OutreachStepKind
    delay_days: int = 0
    message: str

    @field_validator("delay_days")
    @classmethod
    def delay_days_must_not_be_negative(cls, value: int) -> int:
        if value < 0:
            raise ValueError("delay_days must not be negative")
        return value


class OutreachLimits(BaseModel):
    next_limit: int = 10

    @field_validator("next_limit")
    @classmethod
    def next_limit_must_be_positive(cls, value: int) -> int:
        if value < 1:
            raise ValueError("next_limit must be at least 1")
        return value


class OutreachConfig(BaseModel):
    sequence: list[OutreachStep]
    limits: OutreachLimits = Field(default_factory=OutreachLimits)

    @model_validator(mode="after")
    def sequence_must_not_be_empty(self) -> "OutreachConfig":
        if not self.sequence:
            raise ValueError("sequence must not be empty")
        return self


@dataclass(frozen=True)
class OutreachContact:
    linkedin_profile_url: str
    full_name: str
    company: str | None = None
    role_title: str | None = None
    company_domain: str | None = None
    job_id: str | None = None
    job_title: str | None = None
    source: str = "manual-csv"
    notes: str | None = None


@dataclass(frozen=True)
class OutreachAction:
    id: int
    linkedin_profile_url: str
    full_name: str
    kind: OutreachStepKind
    message: str
    due_at: str
    status: str
    step_index: int


@dataclass(frozen=True)
class OutreachImportSummary:
    inserted: int
    updated: int
    skipped: int


@dataclass(frozen=True)
class OutreachQueueSummary:
    contacts_considered: int
    actions_created: int
    actions_existing: int
    skipped: int


def load_outreach_config(path: Path) -> OutreachConfig:
    with path.open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}
    return OutreachConfig.model_validate(data)


def normalize_linkedin_profile_url(value: str) -> str | None:
    candidate = value.strip()
    if not candidate:
        return None
    if not candidate.startswith(("http://", "https://")):
        candidate = f"https://{candidate}"

    parsed = urlparse(candidate)
    host = (parsed.hostname or "").lower()
    if host not in {"linkedin.com", "www.linkedin.com"}:
        return None

    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) < 2 or parts[0].lower() != "in":
        return None

    slug = parts[1].strip().lower()
    if not slug:
        return None
    return f"https://www.linkedin.com/in/{slug}"


def render_message(template: str, contact: OutreachContact) -> str:
    values = _template_values(contact)
    try:
        fields = [field_name for _, field_name, _, _ in Formatter().parse(template) if field_name is not None]
    except ValueError as exc:
        raise ValueError(str(exc)) from exc

    for field_name in fields:
        if not field_name or field_name not in values:
            raise ValueError(f"Unknown outreach template placeholder: {field_name}")
    try:
        return template.format(**values)
    except (KeyError, IndexError, AttributeError, ValueError) as exc:
        raise ValueError(str(exc)) from exc


def _template_values(contact: OutreachContact) -> dict[str, str]:
    return {
        "first_name": _first_name(contact.full_name),
        "full_name": contact.full_name or "",
        "company": contact.company or "",
        "role_title": contact.role_title or "",
        "company_domain": contact.company_domain or "",
        "job_id": contact.job_id or "",
        "job_title": contact.job_title or "",
        "linkedin_profile_url": contact.linkedin_profile_url or "",
        "notes": contact.notes or "",
    }


def _first_name(full_name: str) -> str:
    stripped = full_name.strip()
    if not stripped:
        return ""
    return stripped.split(maxsplit=1)[0]
