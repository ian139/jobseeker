from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Literal


class RunStatus(StrEnum):
    DRY_RUN_READY = "dry_run_ready"
    NEEDS_REVIEW = "needs_review"
    BLOCKED = "blocked"
    FAILED = "failed"


class StepStatus(StrEnum):
    CONTINUE = "continue"
    DRY_RUN_READY = "dry_run_ready"
    NEEDS_REVIEW = "needs_review"
    BLOCKED = "blocked"
    FAILED = "failed"

    def terminal(self) -> RunStatus | None:
        if self == StepStatus.CONTINUE:
            return None
        return RunStatus(self.value)


class FieldKind(StrEnum):
    TEXT = "text"
    TEXTAREA = "textarea"
    SELECT = "select"
    CHECKBOX = "checkbox"
    RADIO = "radio"
    FILE = "file"
    UNKNOWN = "unknown"


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


@dataclass(frozen=True)
class FieldSnapshot:
    id: str
    kind: FieldKind
    label: str
    required: bool
    options: tuple[str, ...] = ()
    value: str | bool | None = None
    disabled: bool = False
    visible: bool = True
    frame: str = "main"
    selector: str = ""


@dataclass(frozen=True)
class ButtonSnapshot:
    id: str
    text: str
    type: str
    disabled: bool
    final_submit_candidate: bool
    visible: bool = True
    frame: str = "main"
    selector: str = ""


@dataclass(frozen=True)
class PageSnapshot:
    url: str
    title: str = ""
    fields: tuple[FieldSnapshot, ...] = ()
    buttons: tuple[ButtonSnapshot, ...] = ()
    errors: tuple[str, ...] = ()
    blockers: tuple[str, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Answer:
    field_id: str
    value: str | bool


@dataclass(frozen=True)
class ResolverDecision:
    status: StepStatus
    answers: tuple[Answer, ...] = ()
    next_button: str | None = None
    submit_button: str | None = None
    review_reasons: tuple[str, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ExecutorAction:
    kind: Literal["fill", "select", "check", "upload", "click"]
    target_id: str
    value: str | bool | list[str] | None = None


@dataclass(frozen=True)
class RunDecision:
    status: StepStatus
    reason: str
    actions: tuple[ExecutorAction, ...] = ()


@dataclass(frozen=True)
class ActionAttempt:
    action: Literal["fill", "select", "check", "upload", "click"]
    target_id: str
    value: str | bool | list[str] | None
    success: bool
    message: str = ""
