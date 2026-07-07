from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal


@dataclass(frozen=True)
class JobInput:
    source: str
    source_job_id: str | None
    url: str | None
    title: str
    company: str
    location: str | None = None
    remote: bool | None = None
    posted_at: str | None = None
    description: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SourceJob:
    external_id: str | None
    title: str
    company: str | None
    listing_url: str | None
    apply_url: str | None
    date_posted: str | None
    raw: dict[str, Any]


@dataclass(frozen=True)
class StoredJobInfo:
    status: Literal["inserted", "updated", "skipped"]
    discovered_at: str | None


@dataclass(frozen=True)
class SyncRunInfo:
    id: int | None = None
    success: bool = False
    jobs_returned: int = 0
    jobs_inserted: int = 0
    jobs_updated: int = 0
    error: str | None = None


@dataclass(frozen=True)
class CreditEstimate:
    dry_run_credits: int = 0
    paid_mode_max_credits: int = 0
