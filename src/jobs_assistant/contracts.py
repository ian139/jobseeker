from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType
from typing import Any, Literal, Mapping, TypeAlias

ATSFilter: TypeAlias = Literal["auto", "greenhouse", "lever"]
FieldValue: TypeAlias = str | bool | tuple[str, ...]



JsonScalar: TypeAlias = str | int | float | bool | None
JsonValue: TypeAlias = JsonScalar | tuple["JsonValue", ...] | Mapping[str, "JsonValue"]


def freeze_json(value: Any) -> JsonValue:
    if value is None or isinstance(value, str | int | float | bool):
        return value
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): freeze_json(item) for key, item in value.items()})
    if isinstance(value, tuple | list):
        return tuple(freeze_json(item) for item in value)
    raise TypeError(f"value is not JSON-compatible: {type(value).__name__}")


def thaw_json(value: JsonValue) -> Any:
    if isinstance(value, Mapping):
        return {key: thaw_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [thaw_json(item) for item in value]
    return value


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
    location: str | None = None
    remote: bool | None = None
    description: str | None = None


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


@dataclass(frozen=True, init=False)
class ApplicationClaim:
    run_id: int
    job: Mapping[str, JsonValue]

    def __init__(self, run_id: int, job: Mapping[str, Any]) -> None:
        object.__setattr__(self, "run_id", run_id)
        object.__setattr__(self, "job", freeze_json(dict(job)))


@dataclass(frozen=True)
class ObservedOption:
    value: str
    label: str
    enabled: bool


@dataclass(frozen=True)
class ObservedField:
    target_id: str
    field_key: str
    frame_id: str
    frame_url: str
    form_action_url: str | None
    kind: str
    name: str | None
    label: str
    group_id: str | None
    option_value: str | None
    safety_descriptors: tuple[str, ...]
    selector: str
    required: bool
    visible: bool
    enabled: bool
    readonly: bool
    value: FieldValue | None
    will_validate: bool
    valid: bool
    validity_flags: tuple[str, ...]
    file_count: int
    file_basenames: tuple[str, ...]
    accept: tuple[str, ...]
    min_length: int | None
    max_length: int | None
    pattern: str | None
    min_value: str | None
    max_value: str | None
    step: str | None
    options: tuple[ObservedOption, ...]
    multiple: bool = False


@dataclass(frozen=True)
class ObservedButton:
    target_id: str
    frame_id: str
    frame_url: str
    click_key: str | None
    element_id: str | None
    element_kind: str
    text: str
    selector: str
    button_type: str
    name: str | None
    value: str | None
    target: str | None
    download: bool
    effective_action_url: str | None
    effective_method: str | None
    href_url: str | None
    href_attribute: str | None
    visible: bool
    enabled: bool
    safety_descriptors: tuple[str, ...]


@dataclass(frozen=True)
class ObservedBlocker:
    code: Literal["captcha", "authentication_required", "assessment_required", "unsupported_frame"]
    frame_id: str
    text: str


@dataclass(frozen=True)
class ObservedValidationError:
    target_id: str | None
    text: str


@dataclass(frozen=True)
class PageObservation:
    observation_id: str
    url: str
    title: str
    site_markers: tuple[str, ...]
    fields: tuple[ObservedField, ...]
    buttons: tuple[ObservedButton, ...]
    final_submit_target_ids: tuple[str, ...]
    errors: tuple[ObservedValidationError, ...]
    blockers: tuple[ObservedBlocker, ...]


class PublicReasonCode(str, Enum):
    draft_ready = "draft_ready"
    required_safe_fields_unresolved = "required_safe_fields_unresolved"
    required_sensitive_fields_manual = "required_sensitive_fields_manual"
    no_deterministic_next_step = "no_deterministic_next_step"
    profile_field_conflict = "profile_field_conflict"
    field_identity_collision = "field_identity_collision"
    preexisting_value_conflict = "preexisting_value_conflict"
    field_value_not_retained = "field_value_not_retained"
    page_validation_error = "page_validation_error"
    page_not_stable = "page_not_stable"
    missing_llm_api_key = "missing_llm_api_key"
    invalid_llm_response = "invalid_llm_response"
    llm_request_failed = "llm_request_failed"
    inference_context_too_large = "inference_context_too_large"
    inference_privacy_violation = "inference_privacy_violation"
    unsupported_ats = "unsupported_ats"
    ats_mismatch = "ats_mismatch"
    invalid_application_url = "invalid_application_url"
    unsafe_navigation_target = "unsafe_navigation_target"
    unsafe_network_attempt = "unsafe_network_attempt"
    observation_too_large = "observation_too_large"
    captcha = "captcha"
    authentication_required = "authentication_required"
    assessment_required = "assessment_required"
    unsupported_frame = "unsupported_frame"
    safe_click_no_progress = "safe_click_no_progress"
    iteration_limit = "iteration_limit"
    artifact_error = "artifact_error"
    browser_error = "browser_error"
    database_error = "database_error"
    handoff_failed = "handoff_failed"
    abandoned_running_attempt = "abandoned_running_attempt"
    legacy_run = "legacy_run"


@dataclass(frozen=True)
class FieldAnswer:
    target_id: str
    value: FieldValue
    confidence: float
    reason: str
    source: Literal["configured", "profile", "inference"]


@dataclass(frozen=True, init=False)
class AutofillPlan:
    answers: tuple[FieldAnswer, ...]
    resume_upload_target_id: str | None
    safe_click_target_id: str | None
    status: str
    reason_code: PublicReasonCode
    skipped_target_ids: tuple[str, ...]
    private_raw: Mapping[str, JsonValue]

    def __init__(
        self,
        answers: tuple[FieldAnswer, ...] = (),
        resume_upload_target_id: str | None = None,
        safe_click_target_id: str | None = None,
        status: str = "manual",
        reason_code: PublicReasonCode = PublicReasonCode.no_deterministic_next_step,
        skipped_target_ids: tuple[str, ...] = (),
        private_raw: Mapping[str, Any] | None = None,
    ) -> None:
        object.__setattr__(self, "answers", answers)
        object.__setattr__(self, "resume_upload_target_id", resume_upload_target_id)
        object.__setattr__(self, "safe_click_target_id", safe_click_target_id)
        object.__setattr__(self, "status", status)
        object.__setattr__(self, "reason_code", reason_code)
        object.__setattr__(self, "skipped_target_ids", skipped_target_ids)
        object.__setattr__(self, "private_raw", freeze_json(dict(private_raw or {})))


@dataclass(frozen=True, init=False)
class ApplicationContext:
    profile_facts: Mapping[str, JsonValue]
    resume_available: bool

    def __init__(self, profile_facts: Mapping[str, Any] | None = None, resume_available: bool = False) -> None:
        object.__setattr__(self, "profile_facts", freeze_json(dict(profile_facts or {})))
        object.__setattr__(self, "resume_available", resume_available)
