from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any, Literal

FieldKind = Literal["text", "textarea", "select", "radio", "checkbox", "typeahead", "file", "unknown"]


class StepStatus(StrEnum):
    CONTINUE = "continue"
    DRY_RUN_READY = "dry_run_ready"
    NEEDS_REVIEW = "needs_review"
    BLOCKED = "blocked"
    FAILED = "failed"

@dataclass(frozen=True)
class ObservedField:
    id: str
    kind: FieldKind
    label: str
    required: bool = False
    options: tuple[str, ...] = ()
    value: str | bool | None = None


@dataclass(frozen=True)
class ObservedButton:
    id: str
    text: str
    type: str | None = None
    disabled: bool = False
    final_submit_candidate: bool = False


@dataclass(frozen=True)
class PageSnapshot:
    url: str
    fields: tuple[ObservedField, ...] = ()
    buttons: tuple[ObservedButton, ...] = ()
    errors: tuple[str, ...] = ()
    blockers: tuple[str, ...] = ()


@dataclass(frozen=True)
class ResolvedAnswer:
    field_id: str
    value: str | bool | list[str]


@dataclass(frozen=True)
class ResolverOutput:
    answers: tuple[ResolvedAnswer, ...] = ()
    next_button_id: str | None = None
    submit_button_id: str | None = None
    needs_review: tuple[str, ...] = ()


@dataclass(frozen=True)
class ExecutorAction:
    kind: Literal["fill", "select", "check", "upload", "click"]
    target_id: str
    value: str | bool | list[str] | None = None


@dataclass(frozen=True)
class RunDecision:
    status: StepStatus
    reason: str
    actions: tuple[ExecutorAction, ...] = field(default_factory=tuple)


def to_jsonable(value: Any) -> Any:
    if hasattr(value, "__dataclass_fields__"):
        return asdict(value)
    if isinstance(value, tuple):
        return [to_jsonable(item) for item in value]
    if isinstance(value, list):
        return [to_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {key: to_jsonable(item) for key, item in value.items()}
    if isinstance(value, StrEnum):
        return str(value)
    return value
