from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

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
