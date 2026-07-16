from __future__ import annotations

from dataclasses import dataclass, replace
import asyncio
import hmac
import base64
import hashlib
import inspect
import json
import os
import re
import stat
import secrets
import socket
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlsplit

import httpx

from .artifacts import ArtifactRoot, ArtifactRun
from .ats import (
    ApplicationProfile,
    ATSAdapter,
    ResumeContext,
    SUPPORTED_ATS,
    _canonical_field_identity as _ats_canonical_field_identity,
    _configured_answer_for_field as _ats_configured_answer_for_field,
    field_accepts_resume,
    is_greenhouse_interactive_origin,
    load_application_profile_snapshot,
    load_applicant_description,
    load_resume_context,
    select_adapter,
    unresolved_required_fields as _ats_unresolved_required_fields,
    validate_answer_value,
)
from .application_preferences import (
    ApplicationPreferences,
    PreferenceValidationError,
    apply_preferences,
    load_application_preferences,
    order_actions,
)
from .application_profiles import load_application_profile_preset
from .browser_adapter import (
    BrowserAdapterError,
    PuppeteerSession,
    normalize_browser_error_code,
    validate_ats_url,
)
from .contracts import (
    ApplicationContext,
    AutofillPlan,
    FieldAnswer,
    JsonValue,
    ObservedBlocker,
    ObservedButton,
    ObservedField,
    ObservedOption,
    ObservedValidationError,
    PageObservation,
    PublicReasonCode,
    thaw_json,
)
from .db import (
    claim_next_application_job,
    finish_application_run,
    mark_application_spawn_attempted,
    register_application_artifact,
    register_application_browser_process,
    register_application_owner_process,
    register_application_session,
    reconcile_open_session_failure,
)
from .safety import DescriptorSafety, classify_descriptors, is_ats_interactive_origin

BLOCKED_STATUS = "blocked"
MANUAL_STATUS = "manual"
COMPLETED_STATUS = "review_ready"
FAILED_STATUS = "failed"
DEFAULT_LLM_MODEL = "deepseek-v4-flash"
DEFAULT_LLM_THINK = "low"
MAX_AUTOFILL_ITERATIONS = 100
MAX_LLM_BYTES = 1024 * 1024
MAX_REQUEST_BYTES = 512 * 1024
GREENHOUSE_ITERATION_PATH = (
    "claim_job", "observe", "resolve", "execute_one_safe_action", "persist_evidence", "commit_review_handoff"
)


def _enum_reason(value: str | PublicReasonCode) -> PublicReasonCode:
    try:
        return value if isinstance(value, PublicReasonCode) else PublicReasonCode(value)
    except (TypeError, ValueError):
        return PublicReasonCode.no_deterministic_next_step


def _status_for_reason(reason: str | PublicReasonCode) -> str:
    code = _enum_reason(reason).value
    if code in {"draft_ready"}:
        return "review_ready"
    if code in {"artifact_error", "browser_error", "database_error", "handoff_failed", "abandoned_running_attempt"}:
        return "failed"
    if code in {
        "unsupported_ats", "ats_mismatch", "invalid_application_url", "unsafe_navigation_target",
        "unsafe_network_attempt", "observation_too_large", "captcha", "authentication_required",
        "assessment_required", "unsupported_frame",
    }:
        return "blocked"
    return "manual"


def _field_options(field: ObservedField) -> tuple[tuple[str, str], ...]:
    return tuple((option.value, option.label) for option in field.options)


def _field_is_sensitive(field: ObservedField) -> bool:
    try:
        return classify_descriptors(tuple(field.safety_descriptors), field_kind=field.kind, options=_field_options(field)) is DescriptorSafety.SENSITIVE
    except Exception:
        return True


def _target_label(field: ObservedField) -> str:
    return field.label or field.name or field.target_id


def _redact_text(
    text: str,
    protected: tuple[str, ...] = (),
    *,
    strip_header: bool = False,
) -> str:
    value = str(text or "")
    patterns = [
        r"[A-Z0-9._%+\-]+@[A-Z0-9.\-]+\.[A-Z]{2,}",
        r"\+?\d[\d ().\-]{7,}\d",
        r"https?://[^\s<>]+",
    ]
    for pattern in patterns:
        value = re.sub(pattern, "[REDACTED]", value, flags=re.I)
    for secret in protected:
        if secret:
            value = re.sub(re.escape(secret), "[REDACTED]", value, flags=re.I)
    if strip_header:
        lines = [line.strip() for line in value.splitlines() if line.strip()]
        if lines:
            value = "\n".join(["[REDACTED_HEADER]", *lines[1:]])
    return value


def _normal(text: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9]+", " ", str(text).lower()).split())


def _compact(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", str(text).lower())


def _protected_variants(value: Any) -> set[str]:
    if not isinstance(value, str) or not value.strip():
        return set()
    raw = value.strip()
    values = {raw, _normal(raw), _compact(raw)}
    digest = hashlib.sha256(raw.encode()).hexdigest()
    values.add(digest)
    values.add(base64.b64encode(raw.encode()).decode())
    return {item for item in values if item}


def _flatten_strings(value: Any, *, depth: int = 0) -> tuple[str, ...]:
    if depth > 8:
        return ()
    if isinstance(value, str):
        return (value,)
    if isinstance(value, Mapping):
        result: list[str] = []
        for item in value.values():
            result.extend(_flatten_strings(item, depth=depth + 1))
        return tuple(result)
    if isinstance(value, (tuple, list)):
        result: list[str] = []
        for item in value:
            result.extend(_flatten_strings(item, depth=depth + 1))
        return tuple(result)
    return ()


def validate_inference_privacy(plan: AutofillPlan, *, protected_values: tuple[str, ...], source_text: str = "") -> bool:
    protected: set[str] = set()
    for value in protected_values:
        protected.update(_protected_variants(value))
    source_tokens = _normal(source_text).split()
    copied_spans = {
        tuple(source_tokens[index:index + 12])
        for index in range(max(0, len(source_tokens) - 11))
    }
    for answer in plan.answers:
        if answer.source != "inference" or not isinstance(answer.value, str):
            continue
        value = answer.value
        variants = {
            _normal(value),
            _compact(value),
            value,
            hashlib.sha256(value.encode()).hexdigest(),
            base64.b64encode(value.encode()).decode(),
        }
        for candidate in variants:
            if candidate and candidate in protected:
                return False
        normalized = _normal(value)
        normalized_protected = {
            _normal(item)
            for item in protected_values
            if isinstance(item, str) and item.strip()
        }
        if normalized and normalized in normalized_protected:
            return False
        tokens = normalized.split()
        if len(tokens) >= 12 and copied_spans:
            if any(tuple(tokens[index:index + 12]) in copied_spans for index in range(len(tokens) - 11)):
                return False
    return True


def _observation_from_payload(payload: Mapping[str, Any]) -> PageObservation:
    required = (
        "observation_id",
        "url",
        "title",
        "site_markers",
        "fields",
        "buttons",
        "final_submit_target_ids",
        "errors",
        "blockers",
    )
    if not isinstance(payload, Mapping) or any(key not in payload for key in required):
        raise BrowserAdapterError("protocol_invalid_response")
    if any(type(payload[key]) is not str for key in ("observation_id", "url", "title")):
        raise BrowserAdapterError("protocol_invalid_response")
    if (
        type(payload["site_markers"]) is not list
        or any(type(item) is not str for item in payload["site_markers"])
        or type(payload["final_submit_target_ids"]) is not list
        or any(type(item) is not str for item in payload["final_submit_target_ids"])
    ):
        raise BrowserAdapterError("protocol_invalid_response")
    collection_keys = ("fields", "buttons", "errors", "blockers")
    if any(
        type(payload[key]) is not list
        or any(not isinstance(item, Mapping) for item in payload[key])
        for key in collection_keys
    ):
        raise BrowserAdapterError("protocol_invalid_response")
    def _strings_or_none(raw: Mapping[str, Any], keys: tuple[str, ...]) -> bool:
        return all(key not in raw or raw[key] is None or type(raw[key]) is str for key in keys)

    def _string_lists(raw: Mapping[str, Any], keys: tuple[str, ...]) -> bool:
        return all(
            key not in raw
            or (type(raw[key]) is list and all(type(item) is str for item in raw[key]))
            for key in keys
        )

    def _valid_field(raw: Mapping[str, Any]) -> bool:
        if any(type(raw.get(key)) is not str for key in ("target_id", "field_key", "kind")):
            return False
        if not _strings_or_none(
            raw,
            ("frame_id", "frame_url", "form_action_url", "name", "label", "group_id", "option_value", "selector"),
        ):
            return False
        if not all(key not in raw or type(raw[key]) is bool for key in ("required", "visible", "enabled", "readonly", "will_validate", "valid")):
            return False
        value = raw.get("value")
        if "value" in raw and value is not None and type(value) not in (str, bool):
            return False
        if "file_count" in raw and (type(raw["file_count"]) is not int or raw["file_count"] < 0):
            return False
        if not _string_lists(raw, ("safety_descriptors", "validity_flags", "file_basenames", "accept")):
            return False
        options = raw.get("options")
        if "options" in raw and (
            type(options) is not list
            or any(
                not isinstance(item, Mapping)
                or type(item.get("value")) is not str
                or type(item.get("label")) is not str
                or type(item.get("enabled")) is not bool
                for item in options
            )
        ):
            return False
        return True

    def _valid_button(raw: Mapping[str, Any]) -> bool:
        if any(type(raw.get(key)) is not str for key in ("target_id", "frame_id", "frame_url", "element_kind", "button_type")):
            return False
        if not _strings_or_none(
            raw,
            (
                "click_key",
                "element_id",
                "text",
                "selector",
                "name",
                "value",
                "target",
                "effective_action_url",
                "effective_method",
                "href_url",
                "href_attribute",
            ),
        ):
            return False
        return all(key not in raw or type(raw[key]) is bool for key in ("download", "visible", "enabled"))

    def _valid_blocker(raw: Mapping[str, Any]) -> bool:
        return all(type(raw.get(key)) is str for key in ("code", "frame_id", "text"))

    def _valid_error(raw: Mapping[str, Any]) -> bool:
        return (raw.get("target_id") is None or type(raw.get("target_id")) is str) and type(raw.get("text")) is str

    if (
        any(not _valid_field(raw) for raw in payload["fields"])
        or any(not _valid_button(raw) for raw in payload["buttons"])
        or any(not _valid_blocker(raw) for raw in payload["blockers"])
        or any(not _valid_error(raw) for raw in payload["errors"])
    ):
        raise BrowserAdapterError("protocol_invalid_response")


    def option(raw: Mapping[str, Any]) -> ObservedOption:
        return ObservedOption(str(raw.get("value", "")), str(raw.get("label", "")), bool(raw.get("enabled", True)))

    fields: list[ObservedField] = []
    for raw in payload["fields"]:
        fields.append(ObservedField(
            target_id=str(raw.get("target_id", "")), field_key=str(raw.get("field_key", "")),
            frame_id=str(raw.get("frame_id", "")), frame_url=str(raw.get("frame_url", "")),
            form_action_url=raw.get("form_action_url"), kind=str(raw.get("kind", "text")),
            name=raw.get("name"), label=str(raw.get("label", "")), group_id=raw.get("group_id"),
            option_value=raw.get("option_value"), safety_descriptors=tuple(str(x) for x in raw.get("safety_descriptors", ())),
            selector=str(raw.get("selector", "")), required=bool(raw.get("required", False)), visible=bool(raw.get("visible", False)),
            enabled=bool(raw.get("enabled", False)), readonly=bool(raw.get("readonly", False)), value=raw.get("value"),
            will_validate=bool(raw.get("will_validate", False)), valid=bool(raw.get("valid", True)),
            validity_flags=tuple(str(x) for x in raw.get("validity_flags", ())), file_count=int(raw.get("file_count", 0) or 0),
            file_basenames=tuple(str(x) for x in raw.get("file_basenames", ())), accept=tuple(str(x) for x in raw.get("accept", ())),
            min_length=raw.get("min_length"), max_length=raw.get("max_length"), pattern=raw.get("pattern"),
            min_value=raw.get("min_value"), max_value=raw.get("max_value"), step=raw.get("step"),
            options=tuple(option(x) for x in raw.get("options", ()) if isinstance(x, Mapping)),
        ))
    buttons: list[ObservedButton] = []
    for raw in payload["buttons"]:
        buttons.append(ObservedButton(
            target_id=str(raw.get("target_id", "")), frame_id=str(raw.get("frame_id", "")), frame_url=str(raw.get("frame_url", "")),
            click_key=raw.get("click_key"), element_id=raw.get("element_id"), element_kind=str(raw.get("element_kind", "button")),
            text=str(raw.get("text", "")), selector=str(raw.get("selector", "")), button_type=str(raw.get("button_type", "button")),
            name=raw.get("name"), value=raw.get("value"), target=raw.get("target"), download=bool(raw.get("download", False)),
            effective_action_url=raw.get("effective_action_url"), effective_method=raw.get("effective_method"), href_url=raw.get("href_url"),
            href_attribute=raw.get("href_attribute"), visible=bool(raw.get("visible", False)), enabled=bool(raw.get("enabled", False)),
            safety_descriptors=tuple(str(x) for x in raw.get("safety_descriptors", ())),
        ))
    blockers = tuple(ObservedBlocker(str(x.get("code")), str(x.get("frame_id", "")), str(x.get("text", ""))) for x in payload["blockers"])
    errors = tuple(ObservedValidationError(x.get("target_id"), str(x.get("text", ""))) for x in payload["errors"])
    return PageObservation(
        observation_id=payload["observation_id"], url=payload["url"], title=payload["title"],
        site_markers=tuple(payload["site_markers"]), fields=tuple(fields), buttons=tuple(buttons),
        final_submit_target_ids=tuple(payload["final_submit_target_ids"]), errors=errors, blockers=blockers,
    )


def _observation_summary(observation: PageObservation) -> dict[str, Any]:
    return {
        "observation_id": observation.observation_id, "url_host": (urlsplit(observation.url).hostname or "").lower(),
        "field_count": len(observation.fields), "button_count": len(observation.buttons),
        "required_count": sum(1 for field in observation.fields if field.required), "final_marker_count": len(observation.final_submit_target_ids),
        "error_count": len(observation.errors), "blocker_codes": [blocker.code for blocker in observation.blockers],
    }

def _observation_semantic_signature(observation: PageObservation) -> tuple[Any, ...]:
    """Compare all planning-relevant state without generation IDs/selectors."""
    fields = tuple(
        (
            field.field_key,
            field.frame_id,
            field.frame_url,
            field.form_action_url,
            field.kind,
            field.name,
            field.label,
            field.group_id,
            field.option_value,
            field.safety_descriptors,
            field.required,
            field.visible,
            field.enabled,
            field.readonly,
            field.value,
            field.will_validate,
            field.valid,
            field.validity_flags,
            field.file_count,
            field.file_basenames,
            field.accept,
            field.min_length,
            field.max_length,
            field.pattern,
            field.min_value,
            field.max_value,
            field.step,
            tuple((option.value, option.label, option.enabled) for option in field.options),
        )
        for field in observation.fields
    )
    final_target_ids = frozenset(observation.final_submit_target_ids)
    buttons = tuple(
        (
            button.frame_id,
            button.frame_url,
            button.click_key,
            button.element_id,
            button.element_kind,
            button.text,
            button.button_type,
            button.name,
            button.value,
            button.target,
            button.download,
            button.effective_action_url,
            button.effective_method,
            button.href_url,
            button.href_attribute,
            button.visible,
            button.enabled,
            button.safety_descriptors,
            button.target_id in final_target_ids,
        )
        for button in observation.buttons
    )
    return (
        observation.url,
        observation.title,
        observation.site_markers,
        fields,
        buttons,
        len(observation.final_submit_target_ids),
        tuple(button.target_id in final_target_ids for button in observation.buttons),
        tuple(error.text for error in observation.errors),
        tuple((blocker.code, blocker.text) for blocker in observation.blockers),
    )


def _plan_summary(plan: AutofillPlan) -> dict[str, Any]:
    return {
        "status": plan.status, "reason_code": _enum_reason(plan.reason_code).value,
        "answer_count": len(plan.answers), "skipped_target_count": len(plan.skipped_target_ids),
        "resume_upload": bool(plan.resume_upload_target_id), "safe_click": bool(plan.safe_click_target_id),
    }
def _answer_payload(field: ObservedField, protected: tuple[str, ...] = ()) -> dict[str, Any]:
    redact = lambda value: _redact_text(str(value), protected) if value is not None else value
    return {
        "target_id": field.target_id,
        "kind": field.kind,
        "label": redact(_target_label(field)),
        "descriptors": [redact(item) for item in field.safety_descriptors],
        "options": [
            {"value": redact(option.value), "label": redact(option.label)}
            for option in field.options
        ],
        "required": field.required,
        "constraints": {
            "min_length": field.min_length,
            "max_length": field.max_length,
            "pattern": redact(field.pattern),
            "min": redact(field.min_value),
            "max": redact(field.max_value),
            "step": redact(field.step),
        },
    }
def _button_payload(
    button: ObservedButton,
    final_submit_target_ids: tuple[str, ...],
    protected: tuple[str, ...],
    *,
    ats_policy: str,
) -> dict[str, Any] | None:
    if not _safe_click_is_eligible(button, final_submit_target_ids, ats_policy=ats_policy):
        return None
    redact = lambda value: _redact_text(str(value), protected) if value is not None else value
    return {
        "target_id": button.target_id,
        "text": redact(button.text),
        "descriptors": [redact(item) for item in button.safety_descriptors],
    }


def _eligible_inference_buttons(
    observation: PageObservation,
    *,
    protected: tuple[str, ...] = (),
    ats_policy: str = "greenhouse",
) -> list[dict[str, Any]]:
    return [
        payload
        for button in observation.buttons
        if (payload := _button_payload(
            button,
            observation.final_submit_target_ids,
            protected,
            ats_policy=ats_policy,
        )) is not None
    ]




def build_inference_request(
    observation: PageObservation,
    *,
    job: Mapping[str, Any],
    resume_text: str,
    profile_facts: Mapping[str, Any],
    job_description: str | None = None,
    applicant_description: str = "",
    configured_values: tuple[str, ...] = (),
    ats_policy: str = "greenhouse",
) -> dict[str, Any]:
    protected = _flatten_strings(profile_facts) + tuple(
        value for value in configured_values if isinstance(value, str)
    )
    resolved_job_description = str(job_description or str(job.get("description") or ""))
    resolved_applicant_description = str(applicant_description or "")
    if any(
        len(value.encode("utf-8")) >= MAX_REQUEST_BYTES
        for value in (str(resume_text or ""), resolved_job_description, resolved_applicant_description)
    ):
        raise ValueError("inference_context_too_large")
    fields = [
        field
        for field in observation.fields
        if _field_is_llm_eligible(field) and field.value in (None, "", False)
    ]
    buttons = _eligible_inference_buttons(observation, protected=tuple(protected), ats_policy=ats_policy)
    allowed_job = {
        key: _redact_text(str(job.get(key) or ""), protected)
        for key in ("title", "company", "location")
        if job.get(key)
    }
    allowed_job["description"] = _redact_text(
        resolved_job_description,
        protected,
    )
    context = _redact_text(resume_text, protected, strip_header=True)
    request = {
        "job": allowed_job,
        "context": {
            "resume": context,
            "description": _redact_text(resolved_applicant_description, protected),
        },
        "fields": [_answer_payload(field, tuple(protected)) for field in fields],
        "buttons": buttons,
        "answers": [],
        "safe_click_target_id": None,
    }
    encoded = json.dumps(request, ensure_ascii=False, separators=(",", ":")).encode()
    if len(encoded) > MAX_REQUEST_BYTES:
        raise ValueError("inference_context_too_large")
    return request

def _validate_llm_answer(field: ObservedField, item: Mapping[str, Any]) -> bool:
    value = item.get("value")
    if type(value) is not str and field.kind not in {"checkbox", "radio"}:
        return False
    if field.kind in {"checkbox", "radio"} and type(value) is not bool:
        return False
    return validate_answer_value(field, value, kind=field.kind)


def _field_is_llm_eligible(field: ObservedField) -> bool:
    """Return whether this live field may receive an inferred answer.

    Eligibility is intentionally derived only from the current observation.  A
    stale/invisible/disabled/read-only/sensitive target must never be admitted
    by either the parser or the action-evidence gate.
    """

    if (
        not field.visible
        or not field.enabled
        or field.readonly
        or str(field.kind).lower() == "file"
        or _field_is_sensitive(field)
        or "field_identity_collision" in field.validity_flags
        or (_field_has_existing_value(field) and not field.valid)
    ):
        return False
    canonical, identity_conflict = _ats_canonical_field_identity(field)
    return canonical is None and not identity_conflict


def _field_has_existing_value(field: ObservedField) -> bool:
    return field.value is not None and field.value != "" and field.value is not False


def _field_existing_value_resolved(field: ObservedField) -> bool:
    if str(field.kind).lower() == "file":
        return field.file_count == 1 and bool(field.valid)
    return _field_has_existing_value(field) and bool(field.valid)


def _unique_observed_fields(observation: PageObservation) -> dict[str, ObservedField] | None:
    fields = {field.target_id: field for field in observation.fields if field.target_id}
    if len(fields) != len(observation.fields):
        return None
    return fields


def _configured_tombstones(
    observation: PageObservation,
    *,
    profile: ApplicationProfile,
    resume: ResumeContext,
    deterministic: tuple[FieldAnswer, ...],
    ats_name: str = "greenhouse",
) -> set[str]:
    """Identify targets where deterministic resolution deliberately failed.

    GreenhouseAdapter returns only successful answers.  A missing answer is not
    enough to authorize inference because it can also represent a configured
    conflict, invalid configured value, or canonical identity collision.
    """

    answered = {answer.target_id for answer in deterministic}
    tombstones: set[str] = set()
    for field in observation.fields:
        target_id = field.target_id
        if not target_id or target_id in answered:
            continue
        canonical, identity_conflict = _ats_canonical_field_identity(field)
        if "field_identity_collision" in field.validity_flags or canonical is not None or identity_conflict:
            tombstones.add(target_id)
            continue
        if not _field_is_llm_eligible(field):
            continue
        if _field_has_existing_value(field) and not field.valid:
            tombstones.add(target_id)
        configured = _ats_configured_answer_for_field(field, profile.field_answers, ats_name=ats_name)
        # A deterministic match that did not produce an answer is a
        # configured/manual tombstone (conflict or invalid value).  Never let
        # inference reinterpret it.
        if configured is not None:
            tombstones.add(target_id)
    return tombstones


def _merge_blocked_target_ids(
    observation: PageObservation,
    deterministic: AutofillPlan,
    *,
    profile: ApplicationProfile | None = None,
    resume: ResumeContext | None = None,
    ats_name: str = "greenhouse",
) -> set[str]:
    blocked = set(deterministic.skipped_target_ids)
    if profile is not None and resume is not None:
        blocked.update(
            _configured_tombstones(
                observation,
                profile=profile,
                resume=resume,
                deterministic=deterministic.answers,
                ats_name=ats_name,
            )
        )
    # A manual deterministic reason without target metadata is fail-closed:
    # do not let an LLM plan mutate any target from that observation.
    if deterministic.reason_code in {
        PublicReasonCode.profile_field_conflict,
        PublicReasonCode.field_identity_collision,
        PublicReasonCode.preexisting_value_conflict,
        PublicReasonCode.page_validation_error,
        PublicReasonCode.field_value_not_retained,
    }:
        blocked.update(field.target_id for field in observation.fields)
    return blocked

def parse_llm_plan(payload: Any, observation: PageObservation, *, ats_policy: str = "greenhouse") -> AutofillPlan:
    if (
        not isinstance(payload, Mapping)
        or set(payload) != {"answers", "safe_click_target_id"}
        or not isinstance(payload.get("answers"), list)
    ):
        return AutofillPlan(status="manual", reason_code=PublicReasonCode.invalid_llm_response)
    by_id = _unique_observed_fields(observation)
    if by_id is None:
        return AutofillPlan(status="manual", reason_code=PublicReasonCode.invalid_llm_response)
    answers: list[FieldAnswer] = []
    seen: set[str] = set()
    for item in payload["answers"]:
        if not isinstance(item, Mapping) or set(item) != {"target_id", "value", "confidence", "reason"}:
            return AutofillPlan(status="manual", reason_code=PublicReasonCode.invalid_llm_response)
        target_id = item.get("target_id")
        confidence = item.get("confidence")
        if (
            not isinstance(target_id, str)
            or not target_id
            or target_id in seen
            or target_id not in by_id
            or type(confidence) not in {int, float}
            or not 0.7 <= float(confidence) <= 1.0
        ):
            return AutofillPlan(status="manual", reason_code=PublicReasonCode.invalid_llm_response)
        field = by_id[target_id]
        if not _field_is_llm_eligible(field) or not _validate_llm_answer(field, item):
            return AutofillPlan(status="manual", reason_code=PublicReasonCode.invalid_llm_response)
        if not isinstance(item.get("reason"), str) or len(item["reason"]) > 2000:
            return AutofillPlan(status="manual", reason_code=PublicReasonCode.invalid_llm_response)
        seen.add(target_id)
        answers.append(FieldAnswer(target_id, item["value"], float(confidence), item["reason"], "inference"))
    click = payload.get("safe_click_target_id")
    if click is not None:
        eligible = {
            button.target_id
            for button in observation.buttons
            if _safe_click_is_eligible(button, observation.final_submit_target_ids, ats_policy=ats_policy)
        }
        if not isinstance(click, str) or click not in eligible:
            return AutofillPlan(status="manual", reason_code=PublicReasonCode.invalid_llm_response)
    return AutofillPlan(
        answers=tuple(answers),
        safe_click_target_id=click,
        status="ready" if answers or click else "manual",
        reason_code=PublicReasonCode.draft_ready if answers or click else PublicReasonCode.no_deterministic_next_step,
    )


def _frame_origin_allowed(frame_url: str, ats_policy: str = "greenhouse") -> bool:
    if ats_policy not in ("greenhouse", "lever"):
        return False
    try:
        parts = urlsplit(frame_url)
    except ValueError:
        return False
    if parts.scheme != "https" or not parts.hostname:
        return False
    try:
        origin = f"https://{parts.hostname}" + (f":{parts.port}" if parts.port else "")
    except ValueError:
        return False
    if ats_policy == "greenhouse":
        return is_greenhouse_interactive_origin(origin)
    if ats_policy == "lever":
        return is_ats_interactive_origin(origin, ats_policy="lever")
    return False


def _safe_click_is_eligible(
    button: ObservedButton,
    final_submit_target_ids: tuple[str, ...] = (),
    *,
    ats_policy: str = "greenhouse",
) -> bool:
    return bool(
        button.target_id not in final_submit_target_ids
        and button.element_kind.lower() == "button"
        and button.button_type.lower() == "button"
        and isinstance(button.click_key, str)
        and bool(button.click_key)
        and _frame_origin_allowed(button.frame_url, ats_policy)
        and button.visible
        and button.enabled
        and not _field_is_sensitive_button(button)
        and button.target in (None, "")
        and not button.download
        and not button.effective_action_url
        and not button.effective_method
        and not button.href_url
        and not button.href_attribute
    )


def _field_is_sensitive_button(button: ObservedButton) -> bool:
    try:
        return classify_descriptors(tuple(button.safety_descriptors)) is DescriptorSafety.SENSITIVE
    except Exception:
        return True


def _extract_llm_content(data: Mapping[str, Any]) -> Any:
    if "answers" in data:
        return data
    message = data.get("message")
    if isinstance(message, Mapping):
        content = message.get("content")
    else:
        choices = data.get("choices")
        content = choices[0].get("message", {}).get("content") if isinstance(choices, list) and choices and isinstance(choices[0], Mapping) else None
    if not isinstance(content, str):
        raise ValueError("invalid_llm_response")
    try:
        parsed = json.loads(content)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("invalid_llm_response") from exc
    return parsed


def _response_content_type(response: Any) -> str:
    headers = getattr(response, "headers", None)
    if not isinstance(headers, Mapping):
        raise ValueError("invalid_llm_response")
    raw = headers.get("content-type") or headers.get("Content-Type")
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("invalid_llm_response")
    return raw.split(";", 1)[0].strip().lower()


def _decode_llm_response(raw_bytes: bytes, content_type: str, *, allow_ndjson: bool) -> Mapping[str, Any]:
    if len(raw_bytes) > MAX_LLM_BYTES:
        raise ValueError("invalid_llm_response")
    if content_type in {"application/x-ndjson", "application/ndjson"}:
        if not allow_ndjson:
            raise ValueError("invalid_llm_response")
        try:
            records = [json.loads(line) for line in raw_bytes.splitlines() if line.strip()]
        except (UnicodeDecodeError, ValueError, json.JSONDecodeError) as exc:
            raise ValueError("invalid_llm_response") from exc
        if not records or not all(isinstance(record, Mapping) for record in records):
            raise ValueError("invalid_llm_response")
        content_parts: list[str] = []
        terminal = False
        for record in records:
            if terminal:
                raise ValueError("invalid_llm_response")
            message = record.get("message")
            if isinstance(message, Mapping) and isinstance(message.get("content"), str):
                content_parts.append(message["content"])
            if record.get("done") is True:
                terminal = True
        if not terminal:
            raise ValueError("invalid_llm_response")
        return {"message": {"content": "".join(content_parts)}}
    if content_type != "application/json":
        raise ValueError("invalid_llm_response")
    try:
        data = json.loads(raw_bytes.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("invalid_llm_response") from exc
    if not isinstance(data, Mapping):
        raise ValueError("invalid_llm_response")
    return data


def _client_json(
    client: Any,
    endpoint: str,
    *,
    headers: Mapping[str, str],
    body: Mapping[str, Any],
    allow_ndjson: bool = False,
) -> Mapping[str, Any]:
    try:
        response = client.stream("POST", endpoint, headers=headers, json=body)
    except AttributeError:
        response = client.post(endpoint, headers=headers, json=body, timeout=60)
        if hasattr(response, "raise_for_status"):
            response.raise_for_status()
        content_type = _response_content_type(response)
        raw = getattr(response, "content", None)
        if raw is None:
            raw = getattr(response, "text", "")
        if isinstance(raw, str):
            raw = raw.encode("utf-8")
        if not isinstance(raw, bytes):
            raise ValueError("invalid_llm_response")
        return _decode_llm_response(raw, content_type, allow_ndjson=allow_ndjson)
    with response as stream:
        if hasattr(stream, "raise_for_status"):
            stream.raise_for_status()
        content_type = _response_content_type(stream)
        chunks: list[bytes] = []
        total = 0
        for chunk in stream.iter_bytes():
            if not isinstance(chunk, bytes):
                raise ValueError("invalid_llm_response")
            total += len(chunk)
            if total > MAX_LLM_BYTES:
                raise ValueError("invalid_llm_response")
            chunks.append(chunk)
        return _decode_llm_response(
            b"".join(chunks),
            content_type,
            allow_ndjson=allow_ndjson,
        )




class _PinnedAddressTransport(httpx.BaseTransport):
    """Connect to one validated address while retaining HTTPS hostname validation."""

    def __init__(self, address: str, hostname: str, port: int) -> None:
        self._address = address
        self._hostname = hostname
        self._port = port
        self._transport = httpx.HTTPTransport(retries=0, verify=True)

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        request.extensions = dict(request.extensions)
        request.extensions["sni_hostname"] = self._hostname
        request.url = request.url.copy_with(host=self._address)
        host_header = self._hostname if self._port == 443 else f"{self._hostname}:{self._port}"
        request.headers["host"] = host_header
        return self._transport.handle_request(request)

    def close(self) -> None:
        self._transport.close()

def resolve_with_llm(
    observation: PageObservation,
    *,
    job: Mapping[str, Any],
    resume_context: str | ResumeContext,
    job_description: str | None = None,
    applicant_description: str = "",
    resume_metadata: Mapping[str, Any] | None = None,
    profile_context: Mapping[str, Any] | ApplicationProfile | None = None,
    api_key: str | None = None,
    base_url: str | None = None,
    model: str | None = None,
    mutated: bool = False,
    ats_policy: str = "greenhouse",
) -> AutofillPlan:
    if mutated:
        return AutofillPlan(status="manual", reason_code=PublicReasonCode.no_deterministic_next_step)
    profile_facts: Mapping[str, Any]
    configured_values: tuple[str, ...] = ()
    if isinstance(profile_context, ApplicationProfile):
        profile_facts = thaw_json(profile_context.facts)
        configured_values = tuple(
            answer.value
            for answer in profile_context.field_answers
            if isinstance(answer.value, str)
        )
    else:
        profile_facts = profile_context or {}
    text = resume_context.text if isinstance(resume_context, ResumeContext) else str(resume_context or "")
    try:
        request = build_inference_request(
            observation,
            job=job,
            resume_text=text,
            profile_facts=profile_facts,
            configured_values=configured_values,
            job_description=job_description,
            applicant_description=applicant_description,
            ats_policy=ats_policy,
        )
    except ValueError as exc:
        reason = str(exc)
        code = PublicReasonCode.inference_context_too_large if "too_large" in reason else PublicReasonCode.invalid_llm_response
        return AutofillPlan(status="manual", reason_code=code)
    fields = request["fields"]
    if not fields and not request["buttons"]:
        return AutofillPlan(status="manual", reason_code=PublicReasonCode.no_deterministic_next_step)
    token = api_key or os.environ.get("OLLAMA_CLOUD_API_KEY") or os.environ.get("OLLAMA_API_KEY")
    if not token:
        return AutofillPlan(status="manual", reason_code=PublicReasonCode.missing_llm_api_key)
    body = {
        "model": model or os.environ.get("OLLAMA_CLOUD_MODEL") or os.environ.get("DEEPSEEK_MODEL") or DEFAULT_LLM_MODEL,
        "messages": [{"role": "user", "content": json.dumps(request, ensure_ascii=False)}],
        "think": os.environ.get("OLLAMA_CLOUD_THINK") or os.environ.get("OLLAMA_CLOUD_REASONING") or DEFAULT_LLM_THINK,
        "stream": True,
    }
    endpoint = (base_url or os.environ.get("OLLAMA_CLOUD_BASE_URL") or "https://ollama.com").rstrip("/") + "/api/chat"
    parsed = urlsplit(endpoint)
    if parsed.scheme != "https" or parsed.username or parsed.password or not parsed.hostname:
        return AutofillPlan(status="manual", reason_code=PublicReasonCode.llm_request_failed)
    try:
        addresses = socket.getaddrinfo(parsed.hostname, parsed.port or 443, type=socket.SOCK_STREAM)
        import ipaddress
        validated_addresses = []
        for item in addresses:
            try:
                address = ipaddress.ip_address(item[4][0])
            except (IndexError, ValueError):
                continue
            if not (
                address.is_private
                or address.is_loopback
                or address.is_link_local
                or address.is_reserved
                or address.is_unspecified
            ):
                validated_addresses.append(str(address))
        if not validated_addresses:
            return AutofillPlan(status="manual", reason_code=PublicReasonCode.llm_request_failed)
        pinned_address = validated_addresses[0]
    except OSError:
        return AutofillPlan(status="manual", reason_code=PublicReasonCode.llm_request_failed)
    transport = _PinnedAddressTransport(
        pinned_address,
        parsed.hostname,
        parsed.port or 443,
    )
    try:
        with httpx.Client(
            transport=transport,
            trust_env=False,
            follow_redirects=False,
            timeout=60.0,
        ) as client:
            raw = _client_json(
                client,
                endpoint,
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                body=body,
                allow_ndjson=True,
            )
        plan = parse_llm_plan(_extract_llm_content(raw), observation, ats_policy=ats_policy)
        protected_sources = _flatten_strings(profile_facts) + configured_values
        if not validate_inference_privacy(plan, protected_values=protected_sources, source_text=text):
            return AutofillPlan(status="manual", reason_code=PublicReasonCode.inference_privacy_violation)
        return plan
    except httpx.HTTPError:
        return AutofillPlan(status="manual", reason_code=PublicReasonCode.llm_request_failed)
    except (ValueError, TypeError, KeyError):
        return AutofillPlan(status="manual", reason_code=PublicReasonCode.invalid_llm_response)


def unresolved_required_fields(observation: PageObservation, answers: tuple[FieldAnswer, ...]) -> tuple[str, ...]:
    """Evaluate required controls using the adapter's group semantics."""
    return _ats_unresolved_required_fields(observation, answers)


def _configured_and_profile_plan(
    observation: PageObservation,
    *,
    adapter: ATSAdapter,
    context: ApplicationContext,
    profile: ApplicationProfile,
    resume: ResumeContext,
    preferences: ApplicationPreferences | None = None,
) -> AutofillPlan:
    if any(field.valid is False for field in observation.fields):
        return AutofillPlan(status="manual", reason_code=PublicReasonCode.page_validation_error)
    deterministic = adapter.deterministic_answers(
        observation,
        context,
        profile=profile,
        resume_context=resume,
        resume_facts=resume.facts,
    )
    preference_optouts: set[str] = set()
    if preferences is not None:
        try:
            pref_result = apply_preferences(preferences, observation.fields, deterministic, ats=adapter.name)
        except PreferenceValidationError:
            return AutofillPlan(status="manual", reason_code=PublicReasonCode.no_deterministic_next_step)
        deterministic = pref_result.selected_answers
        preference_optouts.update(item.target_id for item in pref_result.opted_out)
    answers: list[FieldAnswer] = []
    upload_target: str | None = None
    for answer in deterministic:
        field = next((item for item in observation.fields if item.target_id == answer.target_id), None)
        if field is None:
            continue
        if str(field.kind).lower() == "file":
            if field_accepts_resume(field, resume):
                upload_target = field.target_id
            continue
        if _field_is_sensitive(field):
            continue
        answers.append(answer)
    tombstones = _configured_tombstones(
        observation,
        profile=profile,
        resume=resume,
        deterministic=deterministic,
        ats_name=adapter.name,
    )
    tombstones.update(preference_optouts)
    missing = unresolved_required_fields(observation, tuple(answers))
    sensitive_required = tuple(
        field.target_id
        for field in observation.fields
        if field.required and field.visible and field.enabled and not field.readonly and _field_is_sensitive(field)
    )
    reason = (
        PublicReasonCode.required_sensitive_fields_manual
        if sensitive_required
        else (PublicReasonCode.required_safe_fields_unresolved if missing else PublicReasonCode.no_deterministic_next_step)
    )
    status = "manual" if missing or sensitive_required else ("ready" if answers or upload_target or observation.final_submit_target_ids else "manual")
    skipped = set(tombstones)
    skipped.update(field.target_id for field in observation.fields if _field_is_sensitive(field))
    return AutofillPlan(
        answers=tuple(answers),
        resume_upload_target_id=upload_target,
        status=status,
        reason_code=reason,
        skipped_target_ids=tuple(sorted(skipped)),
    )


def _same_value(field: ObservedField, value: str | bool) -> bool:
    if type(value) is bool or type(field.value) is bool:
        return type(value) is bool and type(field.value) is bool and value == field.value
    return _normal(str(field.value or "")) == _normal(str(value or ""))


def _retained_value_equal(field: ObservedField, expected: str | bool) -> bool:
    if type(expected) is bool or type(field.value) is bool:
        return type(expected) is bool and type(field.value) is bool and expected == field.value
    return type(field.value) is str and type(expected) is str and field.value == expected


def _action_for(field: ObservedField) -> str:
    return "check" if field.kind in {"checkbox", "radio"} else ("select" if field.kind == "select" else "fill")


def plan_action_evidence(
    observation: PageObservation,
    plan: AutofillPlan,
    *,
    ats_policy: str = "greenhouse",
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    planned: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    inference_rejected: set[str] = set()
    by_id = _unique_observed_fields(observation)
    if by_id is None:
        return [], [{"target_id": None, "action": "field", "reason": "field_identity_collision"}]
    answer_ids = [answer.target_id for answer in plan.answers]
    duplicate_ids = {target_id for target_id in answer_ids if answer_ids.count(target_id) > 1}
    if duplicate_ids:
        return [], [
            {"target_id": target_id, "action": "field", "reason": "duplicate_target"}
            for target_id in sorted(duplicate_ids)
        ]

    def reject(answer: FieldAnswer, action: str, reason: str) -> None:
        rejected.append({"target_id": answer.target_id, "action": action, "reason": reason})
        if answer.source == "inference":
            inference_rejected.add(answer.target_id)

    for answer in plan.answers:
        field = by_id.get(answer.target_id)
        if field is None:
            reject(answer, "field", "target_not_observed")
        elif answer.target_id in plan.skipped_target_ids:
            reject(answer, _action_for(field), "tombstoned_target")
        elif "field_identity_collision" in field.validity_flags:
            reject(answer, _action_for(field), "tombstoned_target")
        elif answer.source == "inference" and (
            _ats_canonical_field_identity(field)[0] is not None
            or _ats_canonical_field_identity(field)[1]
        ):
            reject(answer, _action_for(field), "tombstoned_target")
        elif _field_is_sensitive(field):
            reject(answer, _action_for(field), "sensitive_field")
        elif str(field.kind).lower() == "file" or not field.visible or not field.enabled or field.readonly:
            reject(answer, _action_for(field), "ineligible_field")
        elif _field_has_existing_value(field) and not field.valid:
            reject(answer, _action_for(field), "invalid_existing_value")
        elif _field_has_existing_value(field) and not _same_value(field, answer.value):
            reject(answer, _action_for(field), "preexisting_value_conflict")
        elif not _field_has_existing_value(field) and not validate_answer_value(field, answer.value, kind=field.kind):
            reject(answer, _action_for(field), "invalid_value")
        elif not _field_has_existing_value(field):
            planned.append(
                {
                    "target_id": answer.target_id,
                    "action": _action_for(field),
                    "kind": field.kind,
                    "source": answer.source,
                    "value_length": len(answer.value) if isinstance(answer.value, str) else None,
                }
            )
    if plan.resume_upload_target_id:
        field = by_id.get(plan.resume_upload_target_id)
        if (
            field is not None
            and str(field.kind).lower() == "file"
            and field.visible
            and field.enabled
            and not field.readonly
            and field.file_count == 0
            and not _field_is_sensitive(field)
        ):
            planned.append({"target_id": field.target_id, "action": "upload", "kind": "file", "source": "configured"})
        elif field is None:
            rejected.append({"target_id": plan.resume_upload_target_id, "action": "upload", "reason": "target_not_observed"})
        else:
            rejected.append({"target_id": field.target_id, "action": "upload", "reason": "ineligible_field"})
    if not planned and plan.safe_click_target_id:
        button = next((item for item in observation.buttons if item.target_id == plan.safe_click_target_id), None)
        if _safe_click_is_eligible(button, observation.final_submit_target_ids, ats_policy=ats_policy) if button is not None else False:
            planned.append({"target_id": button.target_id, "action": "click", "kind": "button", "source": "inference"})
        else:
            rejected.append({"target_id": plan.safe_click_target_id, "action": "click", "reason": "safe_click_no_progress"})
    if inference_rejected:
        planned = [item for item in planned if item.get("source") != "inference"]
    field_order = {field.target_id: index for index, field in enumerate(observation.fields)}
    planned.sort(key=lambda item: field_order.get(item["target_id"], len(field_order)))
    return planned, rejected


def _jsonable_job(job: Mapping[str, Any]) -> dict[str, Any]:
    # Complete claim snapshots are private artifacts only; never put this in DB/public output.
    return {str(key): thaw_json(value) if not isinstance(value, (str, int, float, bool, type(None))) else value for key, value in job.items()}


def _private_run_path(root: ArtifactRoot, run: ArtifactRun) -> Path:
    root_path = getattr(root, "_path", None)
    if root_path is None:
        return Path(tempfile.mkdtemp(prefix="jobs-assistant-run-"))
    return Path(root_path) / run.public_ref


class AnnotationError(ValueError):
    """Fixed, value-free review annotation boundary error."""

    code = "annotation_error"


class AnnotationUnavailable(AnnotationError):
    code = "annotation_unavailable"


def persist_review_annotation(
    run: ArtifactRun,
    annotation_path: str | Path,
) -> dict[str, str]:
    """Persist one bounded annotation and index it in the existing run manifest."""
    if not isinstance(run, ArtifactRun):
        raise AnnotationUnavailable("annotation_unavailable")
    try:
        manifest = run.read_json("run.json", max_bytes=1024 * 1024)
    except Exception:
        raise AnnotationUnavailable("annotation_unavailable") from None
    if not isinstance(manifest, dict) or type(manifest.get("run_id")) is not int:
        raise AnnotationUnavailable("annotation_unavailable")
    raw_annotations = manifest.get("annotations", [])
    if not isinstance(raw_annotations, list):
        raise AnnotationUnavailable("annotation_unavailable")
    annotations: list[dict[str, Any]] = []
    total_chars = 0
    for item in raw_annotations:
        if (
            not isinstance(item, dict)
            or not isinstance(item.get("artifact_ref"), str)
            or not isinstance(item.get("sha256"), str)
            or not re.fullmatch(r"[0-9a-f]{64}", item["sha256"])
            or type(item.get("chars")) is not int
            or item["chars"] < 0
        ):
            raise AnnotationUnavailable("annotation_unavailable")
        annotations.append(dict(item))
        total_chars += item["chars"]

    path = Path(annotation_path)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(path, flags)
    except (OSError, TypeError):
        raise AnnotationError("annotation_error") from None
    try:
        metadata = os.fstat(fd)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or metadata.st_size > 48_001
        ):
            raise AnnotationError("annotation_error")
        payload = bytearray()
        while len(payload) <= 48_000:
            chunk = os.read(fd, min(64 * 1024, 48_001 - len(payload)))
            if not chunk:
                break
            payload.extend(chunk)
        if len(payload) > 48_000:
            raise AnnotationError("annotation_error")
    except AnnotationError:
        raise
    except OSError:
        raise AnnotationError("annotation_error") from None
    finally:
        os.close(fd)
    try:
        text = bytes(payload).decode("utf-8")
    except UnicodeDecodeError:
        raise AnnotationError("annotation_error") from None
    if len(text) > 12_000:
        raise AnnotationError("annotation_error")
    digest = hashlib.sha256(payload).hexdigest()
    prefix = f"{run.public_ref}/"
    for item in annotations:
        if hmac.compare_digest(item["sha256"], digest):
            artifact_ref = item["artifact_ref"]
            if not artifact_ref.startswith(prefix):
                raise AnnotationUnavailable("annotation_unavailable")
            try:
                persisted = run.read_bytes(
                    artifact_ref[len(prefix):],
                    max_bytes=48_000,
                    expected_sha256=digest,
                )
            except Exception:
                raise AnnotationUnavailable("annotation_unavailable") from None
            try:
                persisted_text = persisted.decode("utf-8")
            except UnicodeDecodeError:
                raise AnnotationUnavailable("annotation_unavailable") from None
            if len(persisted_text) != item["chars"]:
                raise AnnotationUnavailable("annotation_unavailable")
            return {"artifact_ref": artifact_ref, "sha256": digest}
    if len(annotations) >= 10 or total_chars + len(text) > 120_000:
        raise AnnotationError("annotation_error")

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    try:
        result = _verified_artifact_write(run, run.write_bytes(f"annotations/{stamp}-{digest}.txt", bytes(payload)), f"annotations/{stamp}-{digest}.txt")
        annotations.append(
            {
                "artifact_ref": result.relative_path,
                "sha256": digest,
                "chars": len(text),
            }
        )
        manifest["annotations"] = annotations
        _verified_artifact_write(run, run.replace_json("run.json", manifest), "run.json")
    except Exception:
        raise AnnotationError("annotation_error") from None
    return {"artifact_ref": result.relative_path, "sha256": digest}


def _verified_artifact_write(run: ArtifactRun, result: Any, relative_path: str) -> Any:
    """Verify an artifact immediately after its atomic publication."""
    if not hasattr(result, "sha256") or not isinstance(result.sha256, str):
        raise RuntimeError("artifact_hash_mismatch")
    expected_path = f"{run.public_ref}/{relative_path}"
    if getattr(result, "relative_path", None) != expected_path:
        raise RuntimeError("artifact_path_mismatch")
    try:
        run.read_bytes(
            relative_path,
            max_bytes=max(int(getattr(result, "bytes_written", 0)), 1),
            expected_sha256=result.sha256,
        )
    except Exception as exc:
        raise RuntimeError("artifact_hash_mismatch") from exc
    return result


def _write_json_verified(run: ArtifactRun, relative_path: str, value: Any) -> Any:
    return _verified_artifact_write(run, run.write_json(relative_path, value), relative_path)


def _manifest_artifact(result: Any, relative_path: str, *, iteration: int, stage: str) -> dict[str, Any]:
    return {
        "path": relative_path,
        "sha256": result.sha256,
        "iteration": iteration,
        "stage": stage,
    }


def _write_run_manifest(run: ArtifactRun, payload: Mapping[str, Any]) -> Any:
    """Atomically publish and hash-verify the private run manifest."""
    manifest = dict(payload)
    try:
        run.read_bytes("run.json", max_bytes=1024 * 1024)
    except Exception:
        writer = run.write_json
    else:
        writer = run.replace_json
    result = writer("run.json", manifest)
    return _verified_artifact_write(run, result, "run.json")


def _manifest_latest(payload: dict[str, Any], *, iteration: int, stage: str) -> None:
    payload["latest_iteration"] = iteration
    payload["latest_stage"] = stage
    payload["latest"] = {"iteration": iteration, "stage": stage}


def _manifest_set_artifact(
    payload: dict[str, Any],
    key: str,
    result: Any,
    relative_path: str,
    *,
    iteration: int,
    stage: str,
) -> None:
    artifacts = payload.setdefault("artifacts", {})
    if not isinstance(artifacts, dict):
        raise RuntimeError("manifest_error")
    artifacts[key] = _manifest_artifact(result, relative_path, iteration=iteration, stage=stage)




def _manifest_set_iteration(
    payload: dict[str, Any],
    iteration: int,
    *,
    stage: str,
    artifacts: Mapping[str, Any],
) -> None:
    iterations = payload.setdefault("iterations", {})
    if not isinstance(iterations, dict):
        raise RuntimeError("manifest_error")
    iterations[str(iteration)] = {"stage": stage, "artifacts": dict(artifacts)}
    _manifest_latest(payload, iteration=iteration, stage=stage)




def _manifest_token_hash(path: Path) -> str | None:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    value = raw.get("commit_token_sha256") if isinstance(raw, Mapping) else None
    return value if isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) else None
@dataclass(frozen=True)
class _BrowserFailure(Exception):
    stage: str
    operation: str
    code: str
    iteration: int

    def __post_init__(self) -> None:
        Exception.__init__(self, self.code)


def _browser_failure_payload(
    failure: _BrowserFailure,
    *,
    ats_policy: str,
) -> dict[str, JsonValue]:
    return {
        "version": 1,
        "stage": failure.stage,
        "operation": failure.operation,
        "code": failure.code,
        "iteration": failure.iteration,
        "ats_policy": ats_policy,
        "no_final_submit": True,
        "protocol": "length-prefixed-json-v1",
    }


async def _invoke_browser(
    operation: str,
    stage: str,
    iteration: int,
    call: Any,
) -> Any:
    try:
        return await _maybe(call())
    except _BrowserFailure:
        raise
    except BrowserAdapterError as exc:
        raise _BrowserFailure(
            stage,
            operation,
            normalize_browser_error_code(str(exc)),
            iteration,
        ) from None
    except Exception:
        raise _BrowserFailure(
            stage,
            operation,
            "browser_command_failed",
            iteration,
        ) from None
def _observation_from_browser_payload(payload: Any, *, iteration: int) -> PageObservation:
    try:
        return _observation_from_payload(payload)
    except BrowserAdapterError as exc:
        raise _BrowserFailure(
            "observation",
            "observe",
            normalize_browser_error_code(str(exc)),
            iteration,
        ) from None



async def _maybe(value: Any) -> Any:
    return await value if inspect.isawaitable(value) else value


async def _close_session(session: Any) -> None:
    if session is None:
        return
    result = session.close()
    await _maybe(result)


def _session_process_closed(session: Any) -> bool | None:
    """Return process proof when available; otherwise report unknown."""
    process = getattr(session, "process", None)
    poll = getattr(process, "poll", None)
    if not callable(poll):
        return None
    try:
        return poll() is not None
    except Exception:
        return None


async def run_application_workflow(
    connection: Any,
    *,
    limit: int = 1,
    resume_file: str | Path = "resume/Main_Resume.pdf",
    application_profile_json: str | Path | None = None,
    application_profile_preset: str | None = None,
    application_profile_dir: str | Path | None = None,
    application_preferences: str | Path | None = None,
    applicant_description_file: str | Path | None = None,
    artifact_root: str | Path = "data/application-runs",
    headed: bool = False,
    ats: str = "auto",
) -> list[dict[str, Any]]:
    if type(limit) is not int or not 1 <= limit <= 10:
        raise ValueError("limit must be between 1 and 10")
    if application_profile_json is not None and application_profile_preset is not None:
        raise ValueError("application profile JSON and preset are mutually exclusive")
    if application_profile_dir is not None and application_profile_preset is None:
        raise ValueError("application profile directory requires a preset")
    if ats not in {"auto", *SUPPORTED_ATS}:
        raise ValueError("unsupported_ats")
    profile_provenance = None
    profile_json_provenance = None
    if application_profile_preset is not None:
        if application_profile_dir is None:
            raise ValueError("application profile preset directory is required")
        preset = load_application_profile_preset(application_profile_dir, application_profile_preset, cwd=Path.cwd())
        profile = preset.profile
        profile_provenance = {
            "source_kind": "preset",
            "name": preset.name,
            "version": preset.schema_version,
            "content_sha256": preset.source_sha256,
        }
    else:
        loaded_profile = load_application_profile_snapshot(application_profile_json)
        profile = loaded_profile.profile
        if loaded_profile.source_sha256 is not None:
            profile_json_provenance = {
                "source_kind": loaded_profile.source_kind,
                "sha256": loaded_profile.source_sha256,
            }
    preferences = load_application_preferences(application_preferences, cwd=Path.cwd())
    preferences_provenance = (
        {"sha256": preferences.source_sha256}
        if preferences.source_sha256 is not None
        else None
    )
    applicant_description = load_applicant_description(applicant_description_file, profile)
    results: list[dict[str, Any]] = []
    owner = f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"
    with load_resume_context(resume_file) as resume, ArtifactRoot.open(artifact_root, cwd=Path.cwd()) as artifacts:
        for _ in range(limit):
            claim = claim_next_application_job(connection, owner=owner)
            if claim is None:
                break
            run_id = claim.run_id
            job = thaw_json(claim.job)
            run: ArtifactRun | None = None
            session: PuppeteerSession | Any | None = None
            committed = False
            window_state = "closed"
            session_reconciled = False
            run_json_written = False
            artifact_ref: str | None = None
            reason = PublicReasonCode.browser_error
            status = "failed"
            adapter_name = "greenhouse"
            run_dir_path: Path | None = None
            failed_current = False
            manifest_payload: dict[str, Any] | None = None
            session_id: str | None = None
            latest_iteration = 0
            try:
                run = artifacts.create_run_dir(run_id)
                artifact_ref = run.public_ref
                run_dir_path = _private_run_path(artifacts, run)
                if not register_application_artifact(connection, run_id=run_id, artifact_dir=artifact_ref):
                    raise RuntimeError("artifact_error")
                url = str(job.get("canonical_url") or job.get("apply_url") or "")
                adapter = select_adapter(ats, url=url)
                if adapter is None:
                    raise RuntimeError("unsupported_ats")
                adapter_name = adapter.name
                if ats != "auto" and not adapter.matches(url, ""):
                    raise RuntimeError("ats_mismatch")
                try:
                    validate_ats_url(url, adapter.name)
                except BrowserAdapterError as exc:
                    raise RuntimeError(str(exc)) from exc
                claim_result = _write_json_verified(
                    run,
                    "claim.json",
                    {"run_id": run_id, "job_id": int(job.get("id", 0)), "job": _jsonable_job(job)},
                )
                input_result = _verified_artifact_write(
                    run,
                    run.copy_from_fd(f"input/{resume.basename}", resume.fileno(), expected_sha256=resume.sha256),
                    f"input/{resume.basename}",
                )
                job_description_result = None
                raw_job_description = job.get("description")
                if isinstance(raw_job_description, str) and raw_job_description:
                    job_description_result = _verified_artifact_write(
                        run,
                        run.write_bytes("job_description.txt", raw_job_description.encode("utf-8")),
                        "job_description.txt",
                    )
                manifest_payload = {
                    "version": 2,
                    "run_id": run_id,
                    "job_id": int(job.get("id", 0)),
                    "ats_policy": adapter.name,
                    "no_final_submit": True,
                    "stage": "claimed",
                    "commit_token_sha256": None,
                    "artifacts": {},
                    "inputs": {
                        "application_profile_preset": profile_provenance,
                        "application_profile_json": profile_json_provenance,
                        "application_preferences": preferences_provenance,
                    },
                    "iterations": {},
                }
                _manifest_latest(manifest_payload, iteration=0, stage="claimed")
                _manifest_set_artifact(manifest_payload, "claim", claim_result, "claim.json", iteration=0, stage="claimed")
                _manifest_set_artifact(manifest_payload, "input", input_result, f"input/{resume.basename}", iteration=0, stage="claimed")
                if job_description_result is not None:
                    _manifest_set_artifact(
                        manifest_payload,
                        "job_description",
                        job_description_result,
                        "job_description.txt",
                        iteration=0,
                        stage="claimed",
                    )
                _write_run_manifest(run, manifest_payload)
                run_json_written = True
                input_dir = run_dir_path / "input"
                session_manifest = run_dir_path / "review_session.json"
                session_id = uuid.uuid4().hex
                _write_json_verified(run, "review_session.json", {
                    "version": 1,
                    "run_id": run_id,
                    "job_id": int(job.get("id", 0)),
                    "session_id": session_id,
                    "state": "starting",
                    "spawn_attempted": False,
                    "owner_identity": None,
                    "browser_identity": None,
                    "process": {},
                })
                if not register_application_session(connection, run_id=run_id, session_id=session_id, session_state="starting"):
                    raise RuntimeError("database_error")
                owner_registered = False
                browser_registered = False
                def _identity_arg(args: tuple[Any, ...]) -> dict[str, Any] | None:
                    return next((item for item in reversed(args) if isinstance(item, Mapping)), None)
                def on_owner_identity(*args: Any) -> None:
                    nonlocal owner_registered
                    identity = _identity_arg(args)
                    pid = int(identity.get("pid")) if identity and identity.get("pid") is not None else int(getattr(session, "owner_pid", 0))
                    owner_registered = register_application_owner_process(connection, run_id=run_id, owner_pid=pid, process_identity=identity, artifact_root=artifacts)
                    if not owner_registered:
                        raise RuntimeError("database_error")
                def on_browser_identity(*args: Any) -> None:
                    nonlocal browser_registered
                    identity = _identity_arg(args)
                    pid = int(identity.get("pid")) if identity and identity.get("pid") is not None else int(getattr(session, "browser_pid", 0))
                    browser_registered = register_application_browser_process(connection, run_id=run_id, browser_pid=pid, process_identity=identity, artifact_root=artifacts)
                    if not browser_registered:
                        raise RuntimeError("database_error")
                start_kwargs: dict[str, Any] = {
                    "headless": not headed, "run_cwd": run_dir_path, "input_root": input_dir,
                    "session_manifest": session_manifest, "staged_input": resume.basename,
                    "staged_sha256": resume.sha256, "staged_media_type": resume.media_type,
                    "session_id": session_id, "run_id": run_id, "job_id": int(job.get("id", 0)),
                    "ats_policy": adapter.name,
                }
                parameters = inspect.signature(PuppeteerSession.start).parameters
                accepts_kwargs = any(item.kind is inspect.Parameter.VAR_KEYWORD for item in parameters.values())
                if "ats_policy" not in parameters and not accepts_kwargs:
                    start_kwargs.pop("ats_policy", None)
                if "on_owner_identity" in parameters or accepts_kwargs:
                    start_kwargs["on_owner_identity"] = on_owner_identity
                if "on_browser_identity" in parameters or accepts_kwargs:
                    start_kwargs["on_browser_identity"] = on_browser_identity
                try:
                    spawn_allowed = mark_application_spawn_attempted(
                        connection,
                        run_id=run_id,
                        session_id=session_id,
                    )
                except AttributeError:
                    # Test doubles without a DB connection do not expose CAS.
                    spawn_allowed = True
                if not spawn_allowed:
                    raise RuntimeError("database_error")
                _verified_artifact_write(run, run.replace_json("review_session.json", {
                    "version": 1,
                    "run_id": run_id,
                    "job_id": int(job.get("id", 0)),
                    "session_id": session_id,
                    "state": "starting",
                    "spawn_attempted": True,
                    "owner_identity": None,
                    "browser_identity": None,
                    "process": {},
                }), "review_session.json")
                session = await _invoke_browser(
                    "start",
                    "startup",
                    0,
                    lambda: PuppeteerSession.start(**start_kwargs),
                )
                if not owner_registered:
                    on_owner_identity(getattr(session, "owner_identity", None))
                if not browser_registered and getattr(session, "browser_pid", None):
                    on_browser_identity(getattr(session, "browser_identity", None))
                if not owner_registered or (getattr(session, "browser_pid", None) and not browser_registered):
                    raise RuntimeError("database_error")
                await _invoke_browser(
                    "goto",
                    "navigation",
                    0,
                    lambda: session.goto(url, ats_policy=adapter.name),
                )
                context = ApplicationContext(profile_facts=profile.facts, resume_available=True)
                mutation_count = 0
                cached_llm: AutofillPlan | None = None
                cached_inference_target_ids: set[tuple[str, str]] = set()
                cached_inference_button_keys: set[str] = set()
                final_observation: PageObservation | None = None
                cached_inference: dict[tuple[str, str], FieldAnswer] = {}
                cached_click_key: str | None = None
                attempted_mutation: tuple[str, str, str | bool] | None = None
                attempted_click_signature: tuple[Any, ...] | None = None
                executed_actions: list[dict[str, Any]] = []
                final_plan: AutofillPlan | None = None
                for iteration in range(1, MAX_AUTOFILL_ITERATIONS + 1):
                    latest_iteration = iteration
                    payload = await _invoke_browser(
                        "observe",
                        "observation",
                        iteration,
                        lambda: session.observe(),
                    )
                    observation = _observation_from_browser_payload(payload, iteration=iteration)
                    final_observation = observation
                    if attempted_mutation is not None:
                        attempted_key, attempted_kind, expected = attempted_mutation
                        retained_field = next((item for item in observation.fields if item.field_key == attempted_key and item.kind == attempted_kind), None)
                        retained = bool(retained_field is not None and (
                            (attempted_kind == "file" and retained_field.file_count == 1 and resume.basename in retained_field.file_basenames)
                            or (attempted_kind != "file" and retained_field.value is not None and _retained_value_equal(retained_field, expected))
                        ))
                        attempted_mutation = None
                        if not retained:
                            final_plan = AutofillPlan(status="manual", reason_code=PublicReasonCode.field_value_not_retained)
                            reason = PublicReasonCode.field_value_not_retained
                            break
                    if any(field.valid is False for field in observation.fields):
                        final_plan = AutofillPlan(status="manual", reason_code=PublicReasonCode.page_validation_error)
                        reason = PublicReasonCode.page_validation_error
                        break
                    final_target_ids = frozenset(observation.final_submit_target_ids)
                    current_signature = (
                        observation.url,
                        tuple((field.field_key, field.kind, field.required, field.valid, field.file_count, field.value) for field in observation.fields),
                        tuple(
                            (
                                button.click_key,
                                button.text,
                                button.element_kind,
                                button.button_type,
                                button.effective_action_url,
                                button.href_url,
                                button.target_id in final_target_ids,
                            )
                            for button in observation.buttons
                        ),
                        len(final_target_ids),
                    )
                    if attempted_click_signature is not None:
                        prior_signature = attempted_click_signature
                        attempted_click_signature = None
                        if current_signature == prior_signature:
                            final_plan = AutofillPlan(status="manual", reason_code=PublicReasonCode.safe_click_no_progress)
                            reason = PublicReasonCode.safe_click_no_progress
                            break
                    if cached_click_key is not None:
                        rebound_available = any(
                            button.click_key == cached_click_key
                            and _safe_click_is_eligible(
                                button,
                                observation.final_submit_target_ids,
                                ats_policy=adapter.name,
                            )
                            for button in observation.buttons
                        )
                        if not rebound_available:
                            final_plan = AutofillPlan(status="manual", reason_code=PublicReasonCode.safe_click_no_progress)
                            reason = PublicReasonCode.safe_click_no_progress
                            break
                    if observation.blockers:
                        reason = _enum_reason(observation.blockers[0].code)
                        final_plan = AutofillPlan(status="manual", reason_code=reason)
                        break
                    if observation.errors:
                        reason = PublicReasonCode.page_validation_error
                        final_plan = AutofillPlan(status="manual", reason_code=reason)
                        break
                    deterministic = _configured_and_profile_plan(
                        observation,
                        adapter=adapter,
                        context=context,
                        profile=profile,
                        resume=resume,
                        preferences=preferences,
                    )
                    current_by_key = {
                        (field.field_key, field.kind): field
                        for field in observation.fields
                    }
                    for key, answer in tuple(cached_inference.items()):
                        field = current_by_key.get(key)
                        if (
                            field is None
                            or not _field_is_llm_eligible(field)
                            or _field_has_existing_value(field)
                        ):
                            cached_inference.pop(key, None)
                    new_fields = tuple(
                        field
                        for field in observation.fields
                        if _field_is_llm_eligible(field)
                        and field.value in (None, "", False)
                        and (field.field_key, field.kind) not in cached_inference_target_ids
                    )
                    eligible_inference_buttons = tuple(
                        button
                        for button in observation.buttons
                        if _safe_click_is_eligible(
                            button,
                            observation.final_submit_target_ids,
                            ats_policy=adapter.name,
                        )
                    )
                    new_inference_buttons = tuple(
                        button
                        for button in eligible_inference_buttons
                        if button.click_key not in cached_inference_button_keys
                    )
                    if new_fields or new_inference_buttons:
                        cached_inference_target_ids.update(
                            (field.field_key, field.kind)
                            for field in new_fields
                        )
                        cached_inference_button_keys.update(
                            button.click_key
                            for button in eligible_inference_buttons
                            if button.click_key is not None
                        )
                        cached_llm = resolve_with_llm(
                            replace(observation, fields=new_fields, buttons=new_inference_buttons),
                            job=job,
                            resume_context=resume,
                            job_description=job.get("description"),
                            applicant_description=applicant_description,
                            profile_context=profile,
                            mutated=False,
                            ats_policy=adapter.name,
                        )
                        for answer in cached_llm.answers:
                            source_field = next(
                                (
                                    item
                                    for item in new_fields
                                    if item.target_id == answer.target_id
                                ),
                                None,
                            )
                            if source_field is not None:
                                cached_inference[(source_field.field_key, source_field.kind)] = answer
                        if cached_llm.safe_click_target_id:
                            source_button = next(
                                (
                                    button
                                    for button in observation.buttons
                                    if button.target_id == cached_llm.safe_click_target_id
                                ),
                                None,
                            )
                            cached_click_key = (
                                source_button.click_key
                                if source_button is not None
                                and _safe_click_is_eligible(
                                    source_button,
                                    observation.final_submit_target_ids,
                                    ats_policy=adapter.name,
                                )
                                else None
                            )
                    rebound = tuple(
                        FieldAnswer(
                            field.target_id,
                            answer.value,
                            answer.confidence,
                            answer.reason,
                            answer.source,
                        )
                        for key, answer in cached_inference.items()
                        for field in (current_by_key.get(key),)
                        if field is not None
                    )
                    rebound_click = next(
                        (
                            button.target_id
                            for button in observation.buttons
                            if cached_click_key
                            and button.click_key == cached_click_key
                            and _safe_click_is_eligible(
                                button,
                                observation.final_submit_target_ids,
                                ats_policy=adapter.name,
                            )
                        ),
                        None,
                    )
                    llm_reason = (
                        cached_llm.reason_code
                        if cached_llm is not None
                        else PublicReasonCode.no_deterministic_next_step
                    )
                    llm = AutofillPlan(
                        answers=rebound,
                        safe_click_target_id=rebound_click,
                        status="ready" if rebound or rebound_click else "manual",
                        reason_code=(
                            PublicReasonCode.draft_ready
                            if rebound or rebound_click
                            else llm_reason
                        ),
                    )
                    blocked_targets = _merge_blocked_target_ids(
                        observation,
                        deterministic,
                        profile=profile,
                        resume=resume,
                        ats_name=adapter.name,
                    )
                    answers = {answer.target_id: answer for answer in deterministic.answers}
                    for answer in llm.answers:
                        if answer.target_id not in blocked_targets:
                            answers.setdefault(answer.target_id, answer)
                    plan = AutofillPlan(
                        answers=tuple(answers.values()),
                        resume_upload_target_id=deterministic.resume_upload_target_id,
                        safe_click_target_id=llm.safe_click_target_id,
                        status=deterministic.status if deterministic.status != "manual" else llm.status,
                        reason_code=deterministic.reason_code if deterministic.reason_code != PublicReasonCode.no_deterministic_next_step else llm.reason_code,
                        skipped_target_ids=tuple(sorted(blocked_targets)),
                    )
                    protected_sources = _flatten_strings(profile.facts) + _flatten_strings(resume.facts.facts) + tuple(answer.value for answer in profile.field_answers if isinstance(answer.value, str))
                    if not validate_inference_privacy(plan, protected_values=protected_sources, source_text=resume.text):
                        plan = AutofillPlan(status="manual", reason_code=PublicReasonCode.inference_privacy_violation)
                    planned, rejected = plan_action_evidence(observation, plan, ats_policy=adapter.name)
                    if planned and preferences.review_order:
                        ordered_ids = order_actions(
                            preferences,
                            [item["target_id"] for item in planned],
                            descriptors=observation.fields,
                            ats=adapter.name,
                        )
                        rank = {target_id: index for index, target_id in enumerate(ordered_ids)}
                        planned.sort(key=lambda item: rank.get(item["target_id"], len(rank)))
                    conflict = any(item.get("reason") == "preexisting_value_conflict" for item in rejected)
                    if conflict:
                        plan = AutofillPlan(status="manual", reason_code=PublicReasonCode.preexisting_value_conflict, skipped_target_ids=plan.skipped_target_ids)
                        planned = []
                    if planned:
                        action = planned[0]
                        if action["action"] == "click":
                            cached_click_key = None
                            attempted_click_signature = current_signature
                            await _invoke_browser(
                                "click_offline",
                                "mutation",
                                iteration,
                                lambda: session.click_offline(action["target_id"]),
                            )
                        else:
                            field = next(item for item in observation.fields if item.target_id == action["target_id"])
                            expected_value: str | bool = resume.basename if action["action"] == "upload" else next(item.value for item in plan.answers if item.target_id == field.target_id)
                            cached_inference.pop((field.field_key, field.kind), None)
                            attempted_mutation = (field.field_key, field.kind, expected_value)
                            if action["action"] == "upload":
                                await _invoke_browser(
                                    "upload",
                                    "mutation",
                                    iteration,
                                    lambda: session.upload(field.target_id),
                                )
                            elif action["action"] == "select":
                                answer = next(item for item in plan.answers if item.target_id == field.target_id)
                                await _invoke_browser(
                                    "select",
                                    "mutation",
                                    iteration,
                                    lambda: session.select(field.target_id, str(answer.value)),
                                )
                            elif action["action"] == "check":
                                answer = next(item for item in plan.answers if item.target_id == field.target_id)
                                await _invoke_browser(
                                    "check",
                                    "mutation",
                                    iteration,
                                    lambda: session.check(field.target_id, bool(answer.value)),
                                )
                            else:
                                answer = next(item for item in plan.answers if item.target_id == field.target_id)
                                await _invoke_browser(
                                    "fill",
                                    "mutation",
                                    iteration,
                                    lambda: session.fill(field.target_id, str(answer.value)),
                                )
                        if action["action"] != "click":
                            mutation_count += 1
                        executed_action = {
                            "target_id": action["target_id"],
                            "action": action["action"],
                            "generation": observation.observation_id,
                            "executed": True,
                        }
                        executed_actions.append(executed_action)
                        iteration_action = _write_json_verified(
                            run,
                            f"iterations/{iteration:04d}/action.json",
                            executed_action,
                        )
                        iteration_observation = _write_json_verified(
                            run,
                            f"iterations/{iteration:04d}/observation.json",
                            _observation_summary(observation),
                        )
                        iteration_plan = _write_json_verified(
                            run,
                            f"iterations/{iteration:04d}/plan.json",
                            _plan_summary(plan),
                        )
                        iteration_checkpoint = _write_json_verified(
                            run,
                            f"iterations/{iteration:04d}/checkpoint.json",
                            {"mutation": action["action"] != "click", "observation_id": observation.observation_id},
                        )
                        if manifest_payload is None:
                            raise RuntimeError("manifest_error")
                        iteration_stage = "action_applied"
                        _manifest_set_iteration(
                            manifest_payload,
                            iteration,
                            stage=iteration_stage,
                            artifacts={
                                "action": _manifest_artifact(iteration_action, f"iterations/{iteration:04d}/action.json", iteration=iteration, stage=iteration_stage),
                                "observation": _manifest_artifact(iteration_observation, f"iterations/{iteration:04d}/observation.json", iteration=iteration, stage=iteration_stage),
                                "plan": _manifest_artifact(iteration_plan, f"iterations/{iteration:04d}/plan.json", iteration=iteration, stage=iteration_stage),
                                "checkpoint": _manifest_artifact(iteration_checkpoint, f"iterations/{iteration:04d}/checkpoint.json", iteration=iteration, stage=iteration_stage),
                            },
                        )
                        final_plan = plan
                        continue
                    optional_inference_reasons = {
                        PublicReasonCode.no_deterministic_next_step,
                        PublicReasonCode.missing_llm_api_key,
                        PublicReasonCode.invalid_llm_response,
                        PublicReasonCode.llm_request_failed,
                        PublicReasonCode.inference_context_too_large,
                    }
                    ready_candidate = (
                        plan.reason_code == PublicReasonCode.draft_ready
                        or (
                            not unresolved_required_fields(observation, plan.answers)
                            and bool(observation.final_submit_target_ids)
                            and plan.reason_code in optional_inference_reasons
                        )
                    )
                    if ready_candidate:
                        await asyncio.sleep(0.25)
                        stable_payload = await _invoke_browser(
                            "observe",
                            "observation",
                            iteration,
                            lambda: session.observe(),
                        )
                        stable_observation = _observation_from_browser_payload(stable_payload, iteration=iteration)
                        if _observation_semantic_signature(observation) != _observation_semantic_signature(stable_observation):
                            final_observation = stable_observation
                            final_plan = AutofillPlan(
                                status="manual",
                                reason_code=PublicReasonCode.page_not_stable,
                                skipped_target_ids=plan.skipped_target_ids,
                            )
                            reason = PublicReasonCode.page_not_stable
                            break
                        final_observation = stable_observation
                        final_plan = AutofillPlan(
                            answers=plan.answers,
                            resume_upload_target_id=plan.resume_upload_target_id,
                            status="ready",
                            reason_code=PublicReasonCode.draft_ready,
                            skipped_target_ids=plan.skipped_target_ids,
                        )
                        reason = PublicReasonCode.draft_ready
                    else:
                        final_plan = plan
                        reason = plan.reason_code
                    break
                else:
                    final_plan = AutofillPlan(status="manual", reason_code=PublicReasonCode.iteration_limit)
                    reason = PublicReasonCode.iteration_limit
                if final_observation is None or final_plan is None:
                    raise RuntimeError("browser_error")
                observation_result = _write_json_verified(run, "observation.json", _observation_summary(final_observation))
                plan_result = _write_json_verified(run, "plan.json", _plan_summary(final_plan))
                actions_result = _write_json_verified(
                    run,
                    "actions.json",
                    {
                        "mutation_count": mutation_count,
                        "actions": executed_actions,
                        "final_submit_calls": 0,
                    }
                )
                filled_state_result = _write_json_verified(run, "filled_state.json", {"mutation_count": mutation_count})
                if manifest_payload is None:
                    raise RuntimeError("manifest_error")
                final_iteration = latest_iteration
                reason = _enum_reason(final_plan.reason_code)
                status = _status_for_reason(reason)
                can_handoff = headed and status in {"review_ready", "manual", "blocked"} and reason not in {PublicReasonCode.unsupported_ats, PublicReasonCode.ats_mismatch, PublicReasonCode.invalid_application_url, PublicReasonCode.unsafe_network_attempt}
                if can_handoff:
                    await _invoke_browser(
                        "prepare_handoff",
                        "handoff",
                        final_iteration,
                        lambda: session.prepare_handoff(run_id=run_id, job_id=int(job.get("id", 0))),
                    )
                    if not register_application_session(connection, run_id=run_id, session_id=session_id, session_state="prepared"):
                        raise RuntimeError("database_error")
                    token = secrets.token_urlsafe(32)
                    token_hash = hashlib.sha256(token.encode()).hexdigest()
                    manifest_payload["stage"] = "prepared"
                    manifest_payload["commit_token_sha256"] = token_hash
                    _manifest_latest(manifest_payload, iteration=final_iteration, stage="prepared")
                    _manifest_set_artifact(manifest_payload, "observation", observation_result, "observation.json", iteration=final_iteration, stage="prepared")
                    _manifest_set_artifact(manifest_payload, "plan", plan_result, "plan.json", iteration=final_iteration, stage="prepared")
                    _manifest_set_artifact(manifest_payload, "actions", actions_result, "actions.json", iteration=final_iteration, stage="prepared")
                    _manifest_set_artifact(manifest_payload, "filled_state", filled_state_result, "filled_state.json", iteration=final_iteration, stage="prepared")
                    _write_run_manifest(run, manifest_payload)
                    run_json_written = True
                    await _invoke_browser(
                        "commit_handoff",
                        "handoff",
                        final_iteration,
                        lambda: session.commit_handoff(token),
                    )
                    if not hmac.compare_digest(_manifest_token_hash(session_manifest) or "", token_hash):
                        raise RuntimeError("handoff_failed")
                    if not register_application_session(connection, run_id=run_id, session_id=session_id, session_state="open"):
                        raise RuntimeError("database_error")
                    status = "review_ready" if reason == PublicReasonCode.draft_ready else status
                    finish_application_run(connection, run_id=run_id, status=status, reason_code=reason.value, observation_summary=_observation_summary(final_observation), plan_summary=_plan_summary(final_plan), artifact_dir=artifact_ref)
                    try:
                        release_result = await _maybe(session.release_handoff())
                        if not isinstance(release_result, Mapping) or release_result.get("released") is not True:
                            raise RuntimeError("handoff_release_unconfirmed")
                        committed = True
                        window_state = "open"
                    except Exception:
                        # A failed release is reconciled through the bounded close
                        # path; never report an open window without release proof.
                        committed = False
                        try:
                            await _close_session(session)
                        except Exception:
                            pass
                        session_reconciled = True
                        window_state = "closed" if _session_process_closed(session) is True else "unknown"
                else:
                    manifest_payload["stage"] = "finished"
                    manifest_payload["commit_token_sha256"] = None
                    _manifest_latest(manifest_payload, iteration=final_iteration, stage="finished")
                    _manifest_set_artifact(manifest_payload, "observation", observation_result, "observation.json", iteration=final_iteration, stage="finished")
                    _manifest_set_artifact(manifest_payload, "plan", plan_result, "plan.json", iteration=final_iteration, stage="finished")
                    _manifest_set_artifact(manifest_payload, "actions", actions_result, "actions.json", iteration=final_iteration, stage="finished")
                    _manifest_set_artifact(manifest_payload, "filled_state", filled_state_result, "filled_state.json", iteration=final_iteration, stage="finished")
                    _write_run_manifest(run, manifest_payload)
                    run_json_written = True
                    finish_application_run(connection, run_id=run_id, status=status, reason_code=reason.value, observation_summary=_observation_summary(final_observation), plan_summary=_plan_summary(final_plan), artifact_dir=artifact_ref)
                results.append({"job_id": int(job.get("id", 0)), "run_id": run_id, "status": status, "reason_code": reason.value, "ats": adapter_name, "artifact_ref": artifact_ref, "window_state": window_state})
            except Exception as exc:
                failed_current = True
                browser_failure = exc if isinstance(exc, _BrowserFailure) else None
                if browser_failure is not None:
                    reason = PublicReasonCode.browser_error
                    status = "failed"
                    failure_payload = _browser_failure_payload(
                        browser_failure,
                        ats_policy=adapter_name,
                    )
                else:
                    error_code = str(exc) if str(exc) in {code.value for code in PublicReasonCode} else "browser_error"
                    reason = PublicReasonCode(error_code)
                    status = _status_for_reason(reason)
                    failure_payload = None
                if run is not None:
                    try:
                        if manifest_payload is None:
                            manifest_payload = {
                                "version": 2,
                                "run_id": run_id,
                                "job_id": int(job.get("id", 0)),
                                "ats_policy": adapter_name,
                                "no_final_submit": True,
                                "artifacts": {},
                                "iterations": {},
                            }
                        manifest_payload["stage"] = "failed"
                        manifest_payload["commit_token_sha256"] = None
                        _manifest_latest(manifest_payload, iteration=latest_iteration, stage="failed")
                        manifest_payload["reason_code"] = reason.value
                        if failure_payload is not None:
                            try:
                                failure_result = _write_json_verified(
                                    run,
                                    "browser_failure.json",
                                    failure_payload,
                                )
                                _manifest_set_artifact(
                                    manifest_payload,
                                    "browser_failure",
                                    failure_result,
                                    "browser_failure.json",
                                    iteration=browser_failure.iteration,
                                    stage="failed",
                                )
                            except Exception:
                                pass
                        _write_run_manifest(run, manifest_payload)
                        run_json_written = True
                    except Exception:
                        pass
                summary: dict[str, Any] = {"error_code": reason.value}
                if failure_payload is not None:
                    summary["browser_failure"] = failure_payload
                try:
                    finish_application_run(
                        connection,
                        run_id=run_id,
                        status=status,
                        reason_code=reason.value,
                        observation_summary=summary,
                        plan_summary={},
                        artifact_dir=artifact_ref,
                    )
                except Exception as exc:
                    # Never report a terminal result when durable finalization
                    # failed; the claim may still be running in the database.
                    raise RuntimeError("database_error") from None
                results.append({
                    "job_id": int(job.get("id", 0)),
                    "run_id": run_id,
                    "status": status,
                    "reason_code": reason.value,
                    "ats": adapter_name,
                    "artifact_ref": artifact_ref,
                    "window_state": "closed",
                })
            finally:
                try:
                    cleanup_failure: _BrowserFailure | None = None
                    if session is not None and not committed and not session_reconciled:
                        try:
                            await _close_session(session)
                        except BrowserAdapterError as exc:
                            cleanup_failure = _BrowserFailure(
                                "cleanup",
                                "close",
                                normalize_browser_error_code(str(exc)),
                                latest_iteration,
                            )
                        except Exception:
                            cleanup_failure = _BrowserFailure(
                                "cleanup",
                                "close",
                                "browser_command_failed",
                                latest_iteration,
                            )
                        if cleanup_failure is not None:
                            if run is not None and manifest_payload is not None:
                                cleanup_payload = _browser_failure_payload(
                                    cleanup_failure,
                                    ats_policy=adapter_name,
                                )
                                try:
                                    cleanup_result = _write_json_verified(
                                        run,
                                        "browser_cleanup_failure.json",
                                        cleanup_payload,
                                    )
                                    _manifest_set_artifact(
                                        manifest_payload,
                                        "browser_cleanup_failure",
                                        cleanup_result,
                                        "browser_cleanup_failure.json",
                                        iteration=latest_iteration,
                                        stage="failed",
                                    )
                                    manifest_payload["stage"] = "failed"
                                    manifest_payload["reason_code"] = reason.value
                                    _manifest_latest(
                                        manifest_payload,
                                        iteration=latest_iteration,
                                        stage="failed",
                                    )
                                    _write_run_manifest(run, manifest_payload)
                                except Exception:
                                    pass
                                if results and results[-1].get("run_id") == run_id:
                                    results[-1]["window_state"] = "unknown"
                    if (
                        not committed
                        and not session_reconciled
                        and reason is PublicReasonCode.browser_error
                        and session_id is not None
                    ):
                        try:
                            reconcile_open_session_failure(
                                connection,
                                run_id=run_id,
                                session_id=session_id,
                                reason_code="browser_error",
                                artifact_root=artifacts,
                            )
                        except Exception:
                            pass
                finally:
                    if run is not None:
                        run.close()
    return results


# Compatibility name is deliberately absent; callers must migrate to durable workflow.
