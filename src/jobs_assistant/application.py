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
from urllib.parse import parse_qsl, urlsplit

import httpx

from .artifacts import ArtifactRoot, ArtifactRun
from .ats import (
    ApplicationProfile,
    ATSAdapter,
    ResumeContext,
    SUPPORTED_ATS,
    _canonical_field_identity as _ats_canonical_field_identity,
    _canonicalize_select_value,
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
    normalize_field_descriptor,
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
    ApplicationClaim,
    ApplicationContext,
    AutofillPlan,
    FieldAnswer,
    FieldValue,
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
    _supervise_partial_handoff_processes,
)
from .application_rpc_contracts import (
    BrowserToolProposal,
    validate_public_result,
)
from .safety import DescriptorSafety, classify_descriptors, is_ats_interactive_origin

BLOCKED_STATUS = "blocked"
MANUAL_STATUS = "manual"
COMPLETED_STATUS = "review_ready"
FAILED_STATUS = "failed"
DEFAULT_LLM_MODEL = "deepseek-v4-flash"
DEFAULT_LLM_THINK = "low"
LLM_RESPONSE_CONTRACT = (
    "You are a constrained job-application draft resolver. Return exactly one JSON object and no markdown or commentary. "
    "The object must have exactly the keys answers and safe_click_target_id. answers must be an array; every item must "
    "have exactly target_id, value, confidence, and reason. target_id must exactly match one listed field. value must be "
    "a string for text-like fields, a boolean for checkbox/radio fields, and a string or array of unique strings for "
    "select fields according to multiple; every select string must exactly match an enabled option value. confidence "
    "must be a number from 0.7 through 1.0 and reason must be a string no longer than 2000 characters. Use only explicit "
    "evidence in the provided job and context. Omit ambiguous, unsupported, unproven, sensitive, legal, protected-class, "
    "financial, authentication, CAPTCHA, or assessment answers; never invent. safe_click_target_id must be null unless "
    "exactly one listed button clearly advances the current application without submitting or authenticating, otherwise "
    "it must exactly match that listed button target_id. Do not output any extra keys."
)
MAX_AUTOFILL_ITERATIONS = 100
MAX_LLM_BYTES = 1024 * 1024
MAX_REQUEST_BYTES = 512 * 1024
MAX_SCREENSHOTS_PER_RUN = 10
MAX_SCREENSHOT_BYTES = 20 * 1024 * 1024
MAX_SCREENSHOT_TOTAL_BYTES = 50 * 1024 * 1024
SCREENSHOT_SLOTS = frozenset({"initial", "after-reveal", "blocker", "final"})
GREENHOUSE_ITERATION_PATH = (
    "claim_job", "observe", "resolve", "execute_one_safe_action", "persist_evidence", "commit_review_handoff"
)

# The RPC candidate_profile_id for the built-in empty profile is this
# deterministic canonical content hash.  Explicit JSON and preset profiles
# use their retained source-byte SHA-256 instead.
DEFAULT_APPLICATION_PROFILE_SHA256 = hashlib.sha256(
    json.dumps(
        {"description": "", "facts": {}, "field_answers": []},
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
).hexdigest()

_CONTROL_PROGRESS_EVENTS = frozenset({
    "page_observed",
    "action_allowed",
    "action_rejected",
    "screenshot_captured",
    "manual_intervention_required",
    "review_ready",
    "browser_handed_off",
    "run_failed",
})
_CONTROL_PROGRESS_CODES = frozenset({
    "started",
    "observed",
    "allowed",
    "rejected",
    "uploaded",
    "validation_error",
    "manual_required",
    "review_ready",
    "handed_off",
    "cancelled",
    "failed",
    "captured",
    *(code.value for code in PublicReasonCode),
})
_PUBLIC_ID_HMAC_KEY = secrets.token_bytes(32)


class ApplicationWorkflowControl:
    """Minimal duck-typed protocol for external RPC-driven application control.

    Implementations may suspend ``propose_action`` and ``authorize_handoff``.
    The workflow deliberately awaits those calls without closing the browser
    session or finalizing the running application row.
    """

    async def on_claimed(
        self, run_id: int, job_id: int, ats_policy: str, application_url: str
    ) -> None:
        ...

    async def cancellation_requested(self, run_id: int) -> bool:
        ...

    async def record_progress(
        self,
        run_id: int,
        event_type: str,
        summary_code: str,
        action_sequence: int,
        observation_sha256: str | None = None,
        request_id: str | None = None,
    ) -> None:
        """Persist one event from ``_CONTROL_PROGRESS_EVENTS``."""
        ...

    async def propose_action(
        self,
        run_id: int,
        iteration: int,
        observation_sha256: str,
        public_observation: dict[str, Any],
        inference_request: dict[str, Any] | None,
        deterministic_plan: dict[str, Any],
    ) -> BrowserToolProposal | None:
        ...

    async def authorize_handoff(
        self,
        run_id: int,
        iteration: int,
        observation_sha256: str,
        public_observation: dict[str, Any],
    ) -> BrowserToolProposal | None:
        ...

    async def before_action_dispatch(
        self, proposal: BrowserToolProposal, action_sequence: int
    ) -> bool:
        """Atomically enforce deadline/cancellation and mark dispatch."""
        ...

    async def proposal_finished(
        self,
        proposal: BrowserToolProposal,
        action_sequence: int,
        ok: bool,
        state: str,
        result: Mapping[str, Any] | None = None,
        error_code: str | None = None,
        application_finalization: Mapping[str, Any] | None = None,
    ) -> bool:
        """Resolve one child proposal exactly once; return app-row ownership."""
        ...

    async def finalize_failure(
        self,
        run_id: int,
        *,
        status: str,
        reason_code: str,
        observation_summary: Mapping[str, Any],
        plan_summary: Mapping[str, Any],
        artifact_dir: str | None,
        pending_proposal: BrowserToolProposal | None = None,
        action_sequence: int = 0,
        error_code: str | None = None,
        observation_sha256: str | None = None,
        manifest_sha256: str | None = None,
    ) -> bool:
        """Atomically finalize the application and RPC failure state."""
        return False

    async def prepare_handoff_finalization(
        self,
        proposal: BrowserToolProposal,
        *,
        action_sequence: int,
        intent: Mapping[str, Any],
    ) -> bool:
        return False

    async def reconcile_postcommit_handoff_failure(
        self,
        run_id: int,
        *,
        session_id: str | None,
        artifact_root: ArtifactRoot,
    ) -> bool:
        return False

    def mark_handoff_committed(self) -> None:
        return None


def _public_id_digest(value: str, *, prefix: str, generation: str | None = None) -> str:
    """Return a process-private, observation-bound opaque public identifier."""
    payload = (
        f"omp-public-id-v2\0{prefix}\0{str(generation or '')}\0{str(value or '')}"
    ).encode("utf-8")
    digest = hmac.new(_PUBLIC_ID_HMAC_KEY, payload, hashlib.sha256).hexdigest()[:32]
    return f"{prefix}-{digest}"


def _public_element_id(
    value: str,
    *,
    prefix: str = "el",
    generation: str | None = None,
) -> str:
    """Return an opaque, generation-bound public element identifier."""
    return _public_id_digest(value, prefix=prefix, generation=generation)


def _opaque_option_id(
    observation_generation: str,
    target_id: str,
    index: int,
) -> str:
    return _public_id_digest(
        f"{target_id}\0{index}",
        prefix="opt",
        generation=observation_generation,
    )


def _public_frame_id(frame_id: str, *, generation: str | None = None) -> str:
    return _public_id_digest(frame_id, prefix="frame", generation=generation)


def _observation_generation(
    observation: PageObservation,
    observation_sha256: str | None = None,
) -> str:
    """Bind public IDs to this immutable in-process observation instance."""
    snapshot_sha256 = str(
        observation_sha256 or _observation_snapshot_sha256(observation)
    )
    return f"{id(observation):x}\0{observation.observation_id}\0{snapshot_sha256}"


def _public_label(text: Any, protected: tuple[str, ...]) -> str:
    redacted = _redact_text(str(text or ""), protected)
    return redacted[:2000]


def _public_page_type(observation: PageObservation) -> str:
    blocker_codes = {str(item.code) for item in observation.blockers}
    if "captcha" in blocker_codes:
        return "captcha"
    if "authentication_required" in blocker_codes:
        return "authentication"
    if "assessment_required" in blocker_codes:
        return "assessment"
    markers = {str(item).casefold() for item in observation.site_markers}
    if "confirmation" in markers or "submitted" in markers:
        return "confirmation"
    if observation.fields or observation.buttons:
        return "application"
    return "unknown"


def _public_control_action_type(button: ObservedButton, *, final: bool) -> str:
    if final:
        return "final_submit"
    if button.effective_action_url or button.href_url:
        return "navigation"
    if str(button.button_type).casefold() in {"button", "submit"}:
        return "continue"
    return "unknown"


def _public_safety_class(field: ObservedField, *, ats_policy: str) -> str:
    if _field_is_sensitive(field):
        return "sensitive"
    if not field.visible or not field.enabled or field.readonly:
        return "manual"
    if _field_is_llm_eligible(field):
        return "safe"
    return "ambiguous"


def _build_public_observation(
    observation: PageObservation,
    *,
    claimed_url: str,
    ats_policy: str = "greenhouse",
    observation_sha256: str | None = None,
    observation_sequence: int = 1,
    observed_at: str | None = None,
    protected_values: tuple[str, ...] = (),
) -> dict[str, Any]:
    """Project private browser state through the public RPC observation schema."""
    if type(claimed_url) is not str or not claimed_url:
        raise ValueError("claimed_url is required")
    if observation_sha256 is None:
        observation_sha256 = _observation_snapshot_sha256(observation)
    if observed_at is None:
        observed_at = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    generation = _observation_generation(observation, observation_sha256)
    protected = _expanded_protected_values(protected_values)
    fields: list[dict[str, Any]] = []
    for field in observation.fields:
        field_id = _public_element_id(field.target_id, generation=generation)
        frame_id = _public_frame_id(field.frame_id, generation=generation)
        options = [
            {
                "id": _opaque_option_id(generation, field.target_id, index),
                "label": _public_label(option.label, protected),
                "enabled": bool(option.enabled),
            }
            for index, option in enumerate(field.options)
        ]
        accept = [
            _public_label(item, protected)[:256]
            for item in field.accept
            if str(item)
        ]
        fields.append({
            "element_id": field_id,
            "frame_id": frame_id,
            "label": _public_label(field.label or field.name or "", protected),
            "kind": field.kind,
            "required": bool(field.required),
            "disabled": not bool(field.enabled),
            "readonly": bool(field.readonly),
            "has_value": _field_has_existing_value(field),
            "multiple": bool(field.multiple),
            "options": options,
            "accept": accept,
            "safety_class": _public_safety_class(field, ats_policy=ats_policy),
        })
    final_ids = frozenset(observation.final_submit_target_ids)
    controls: list[dict[str, Any]] = []
    for button in observation.buttons:
        final = button.target_id in final_ids
        controls.append({
            "element_id": _public_element_id(button.target_id, generation=generation),
            "frame_id": _public_frame_id(button.frame_id, generation=generation),
            "label": _public_label(button.text, protected),
            "kind": button.element_kind,
            "action_type": _public_control_action_type(button, final=final),
            "enabled": bool(button.visible and button.enabled),
            "terminal": final,
        })
    frame_source = (
        observation.fields[0].frame_id
        if observation.fields
        else observation.buttons[0].frame_id
        if observation.buttons
        else "main"
    )
    public = {
        "observation_sha256": observation_sha256,
        "observation_sequence": int(observation_sequence),
        "observed_at": observed_at,
        "url": claimed_url,
        "ats": ats_policy,
        "page_type": _public_page_type(observation),
        "frame_id": _public_frame_id(frame_source, generation=generation),
        "fields": fields,
        "controls": controls,
        "validation_errors": [
            {
                "element_id": (
                    _public_element_id(error.target_id, generation=generation)
                    if error.target_id is not None
                    else None
                ),
                "code": "page_validation_error",
            }
            for error in observation.errors
        ],
        "progress": {"step_index": None, "step_count": None},
        "blocker_codes": [str(item.code) for item in observation.blockers],
    }
    # Fail closed if any projection drifts from the host contract.
    return thaw_json(validate_public_result(public, operation="browser.observe"))


def _resolve_public_target(
    observation: PageObservation,
    public_id: str,
    *,
    observation_sha256: str | None = None,
    buttons: bool = False,
) -> str | None:
    generation = _observation_generation(observation, observation_sha256)
    items = observation.buttons if buttons else observation.fields
    matches = [
        item.target_id
        for item in items
        if _public_element_id(item.target_id, generation=generation) == public_id
    ]
    return matches[0] if len(matches) == 1 else None


def _validate_control_proposal(
    proposal: BrowserToolProposal,
    observation: PageObservation,
    observation_sha256: str,
    deterministic_plan: AutofillPlan,
    *,
    ats_policy: str = "greenhouse",
) -> tuple[dict[str, Any] | None, str | None]:
    """Validate one host proposal and return a private browser action."""
    if not isinstance(proposal, BrowserToolProposal):
        return None, "invalid_proposal"
    if proposal.tool_name != proposal.request.operation:
        return None, "invalid_proposal"
    payload = thaw_json(proposal.request.payload)
    operation = proposal.request.operation
    prop_sha = payload.get("observation_sha256")
    if not isinstance(prop_sha, str) or not hmac.compare_digest(prop_sha, observation_sha256):
        return None, "stale_observation_hash"

    element_id = payload.get("element_id")
    if operation == "browser.upload_configured_resume":
        if not isinstance(element_id, str):
            return None, "missing_element_id"
        target_id = _resolve_public_target(
            observation,
            element_id,
            observation_sha256=observation_sha256,
        )
        if target_id is None:
            return None, "target_not_observed"
        if deterministic_plan.resume_upload_target_id != target_id:
            return None, "resume_target_mismatch"
        field = next((item for item in observation.fields if item.target_id == target_id), None)
        if field is None:
            return None, "target_not_observed"
        if str(field.kind).lower() != "file":
            return None, "not_a_file_field"
        if not field.visible or not field.enabled or field.readonly:
            return None, "ineligible_field"
        if field.file_count != 0:
            return None, "file_already_uploaded"
        if _field_is_sensitive(field):
            return None, "sensitive_field"
        return {
            "target_id": target_id,
            "action": "upload",
            "kind": "file",
            "source": "configured",
        }, None

    if operation == "browser.activate_safe_control":
        if not isinstance(element_id, str):
            return None, "missing_element_id"
        target_id = _resolve_public_target(
            observation,
            element_id,
            observation_sha256=observation_sha256,
            buttons=True,
        )
        if target_id is None:
            return None, "target_not_observed"
        button = next((item for item in observation.buttons if item.target_id == target_id), None)
        if button is None:
            return None, "target_not_observed"
        if target_id in frozenset(observation.final_submit_target_ids):
            return None, "final_submit_control"
        if not _safe_click_is_eligible(
            button,
            observation.final_submit_target_ids,
            ats_policy=ats_policy,
            page_url=observation.url,
        ):
            return None, "ineligible_control"
        return {
            "target_id": target_id,
            "action": "click",
            "kind": "button",
            "source": "inference",
        }, None

    if operation not in {"browser.fill_field", "browser.select_option", "browser.set_checkbox"}:
        return None, "unsupported_operation"
    if not isinstance(element_id, str):
        return None, "missing_element_id"
    target_id = _resolve_public_target(
        observation,
        element_id,
        observation_sha256=observation_sha256,
    )
    if target_id is None:
        return None, "target_not_observed"
    field = next((item for item in observation.fields if item.target_id == target_id), None)
    if field is None:
        return None, "target_not_observed"
    value = payload.get("value")
    confidence = payload.get("confidence")
    inference_reason = payload.get("reason")
    if value is not None or confidence is not None or inference_reason is not None:
        return None, "unsupported_model_value"
    det_answer = next(
        (answer for answer in deterministic_plan.answers if answer.target_id == target_id),
        None,
    )
    if det_answer is None:
        return None, "no_deterministic_answer"
    actual_value = det_answer.value
    source = det_answer.source
    if _field_has_existing_value(field):
        return None, "field_already_filled"
    if _field_is_sensitive(field):
        return None, "sensitive_field"
    if not field.visible or not field.enabled or field.readonly:
        return None, "ineligible_field"
    if str(field.kind).lower() == "file":
        return None, "ineligible_field"
    if target_id in frozenset(deterministic_plan.skipped_target_ids):
        return None, "tombstoned_target"
    expected_action = _action_for(field)
    expected_operation = {
        "fill": "browser.fill_field",
        "select": "browser.select_option",
        "check": "browser.set_checkbox",
    }[expected_action]
    if operation != expected_operation:
        return None, "action_kind_mismatch"
    return {
        "target_id": target_id,
        "action": expected_action,
        "kind": field.kind,
        "source": source,
        "value": actual_value,
        "confidence": None,
        "reason": None,
    }, None


def _deterministic_plan_summary(
    plan: AutofillPlan,
    *,
    observation: PageObservation,
    observation_sha256: str,
) -> dict[str, Any]:
    """Expose only observation-scoped availability metadata."""
    generation = _observation_generation(observation, observation_sha256)

    def public_id(target_id: str) -> str:
        return _public_element_id(target_id, generation=generation)

    return {
        "answers": [{"target_id": public_id(answer.target_id)} for answer in plan.answers],
        "resume_upload_target_id": (
            public_id(plan.resume_upload_target_id)
            if plan.resume_upload_target_id is not None
            else None
        ),
        "status": plan.status,
        "reason_code": plan.reason_code.value,
        "skipped_target_ids": [public_id(target_id) for target_id in plan.skipped_target_ids],
    }
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
        if not secret:
            continue
        value = re.sub(re.escape(secret), "[REDACTED]", value, flags=re.I)
        normalized = _normal(secret)
        tokens = normalized.split()
        if len(tokens) > 1:
            separator_pattern = r"[\W_]+".join(re.escape(token) for token in tokens)
            value = re.sub(separator_pattern, "[REDACTED]", value, flags=re.I)
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
    plain = {item for item in (raw, _normal(raw), _compact(raw)) if item}
    variants: set[str] = set()
    for item in plain:
        encoded = item.encode()
        standard = base64.b64encode(encoded).decode()
        urlsafe = base64.urlsafe_b64encode(encoded).decode()
        variants.update({
            item,
            hashlib.sha256(encoded).hexdigest(),
            standard,
            standard.rstrip("="),
            urlsafe,
            urlsafe.rstrip("="),
        })
    return {item for item in variants if item}


def _expanded_protected_values(values: Any) -> tuple[str, ...]:
    variants: set[str] = set()
    for value in values:
        variants.update(_protected_variants(value))
    return tuple(sorted(variants, key=lambda item: (-len(item), item)))


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

def _flatten_prompt_private_values(
    value: Any,
    *,
    depth: int = 0,
) -> tuple[str, ...]:
    if depth > 8:
        return ()
    if isinstance(value, str):
        return (value,)
    if type(value) in {int, float}:
        return (str(value),)
    if isinstance(value, Mapping):
        result: list[str] = []
        for item in value.values():
            result.extend(
                _flatten_prompt_private_values(item, depth=depth + 1)
            )
        return tuple(result)
    if isinstance(value, (tuple, list)):
        result = []
        for item in value:
            result.extend(
                _flatten_prompt_private_values(item, depth=depth + 1)
            )
        return tuple(result)
    return ()


def _configured_answer_values(
    profile: ApplicationProfile,
    *,
    preferences: ApplicationPreferences | None = None,
    deterministic: AutofillPlan | None = None,
) -> tuple[str, ...]:
    values: list[FieldValue] = [
        answer.value
        for answer in profile.field_answers
    ]
    if preferences is not None:
        values.extend(mapping.value for mapping in preferences.mappings)
    if deterministic is not None:
        values.extend(answer.value for answer in deterministic.answers)
    return _flatten_prompt_private_values(tuple(values))


def validate_inference_privacy(plan: AutofillPlan, *, protected_values: tuple[str, ...], source_text: str = "") -> bool:
    protected: set[str] = set()
    for value in protected_values:
        protected.update(_protected_variants(value))
    source_tokens = _normal(source_text).split()
    copied_spans = {
        tuple(source_tokens[index:index + 12])
        for index in range(max(0, len(source_tokens) - 11))
    }
    normalized_protected = {
        _normal(item)
        for item in protected_values
        if isinstance(item, str) and item.strip()
    }
    for answer in plan.answers:
        if answer.source != "inference":
            continue
        if isinstance(answer.value, str):
            values = (answer.value,)
        elif type(answer.value) is tuple:
            if any(type(item) is not str for item in answer.value):
                return False
            values = answer.value
        else:
            continue
        for value in values:
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
        if not all(key not in raw or type(raw[key]) is bool for key in ("required", "visible", "enabled", "readonly", "will_validate", "valid", "multiple")):
            return False
        multiple = raw.get("multiple", False)
        if type(multiple) is not bool:
            return False
        kind = raw["kind"]
        if kind != kind.lower() or kind != kind.strip():
            return False
        if kind not in {
            "text", "email", "tel", "url", "number", "date", "textarea", "select",
            "checkbox", "radio", "file", "password", "search", "color", "range",
            "month", "week", "time", "datetime-local",
        }:
            return False
        if kind != "select" and multiple:
            return False
        options = raw.get("options")
        if kind == "select" and (
            "options" not in raw
            or type(options) is not list
            or any(
                not isinstance(item, Mapping)
                or type(item.get("value")) is not str
                or type(item.get("label")) is not str
                or type(item.get("enabled")) is not bool
                for item in options
            )
        ):
            return False
        if kind == "select":
            validity_flags = raw.get("validity_flags", ())
            options_ambiguous = (
                raw.get("valid") is False
                and type(validity_flags) is list
                and "options_ambiguous" in validity_flags
            )
            invalid_selection = (
                raw.get("valid") is False
                and type(validity_flags) is list
                and ("invalid_selected_option" in validity_flags or "options_ambiguous" in validity_flags)
            )
            option_values = [item["value"] for item in options]
            if len(option_values) != len(set(option_values)) and not options_ambiguous:
                return False
            enabled_values = {item["value"] for item in options if item["enabled"]}
            if "value" not in raw:
                return False
            value = raw["value"]
            if multiple:
                if type(value) is not list or any(type(item) is not str for item in value):
                    return False
                if not invalid_selection:
                    if len(value) != len(set(value)) or any(item not in enabled_values for item in value):
                        return False
                    observed_order = [
                        item["value"] for item in options
                        if item["enabled"] and item["value"] in enabled_values and item["value"] in value
                    ]
                    if value != observed_order:
                        return False
            elif type(value) is not str or (value and value not in enabled_values and not invalid_selection):
                return False
        elif "value" in raw and raw["value"] is not None and type(raw["value"]) not in (str, bool):
            return False
        if "file_count" in raw and (type(raw["file_count"]) is not int or raw["file_count"] < 0):
            return False
        if any(
            key in raw and raw[key] is not None
            and (type(raw[key]) is not int or raw[key] < 0)
            for key in ("min_length", "max_length")
        ):
            return False
        if not _strings_or_none(raw, ("pattern", "min_value", "max_value", "step")):
            return False
        if not _string_lists(raw, ("safety_descriptors", "validity_flags", "file_basenames", "accept")):
            return False
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
        return ObservedOption(raw["value"], raw["label"], raw["enabled"])


    fields: list[ObservedField] = []
    for raw in payload["fields"]:
        raw_value = raw.get("value")
        value: str | bool | tuple[str, ...] | None = raw_value
        if isinstance(raw_value, list):
            value = tuple(raw_value)
        fields.append(ObservedField(
            target_id=str(raw.get("target_id", "")), field_key=str(raw.get("field_key", "")),
            frame_id=str(raw.get("frame_id", "")), frame_url=str(raw.get("frame_url", "")),
            form_action_url=raw.get("form_action_url"), kind=str(raw.get("kind", "text")),
            name=raw.get("name"), label=str(raw.get("label", "")), group_id=raw.get("group_id"),
            option_value=raw.get("option_value"), safety_descriptors=tuple(str(x) for x in raw.get("safety_descriptors", ())),
            selector=str(raw.get("selector", "")), required=bool(raw.get("required", False)), visible=bool(raw.get("visible", False)),
            enabled=bool(raw.get("enabled", False)), readonly=bool(raw.get("readonly", False)), value=value,
            will_validate=bool(raw.get("will_validate", False)), valid=bool(raw.get("valid", True)),
            validity_flags=tuple(str(x) for x in raw.get("validity_flags", ())), file_count=int(raw.get("file_count", 0) or 0),
            file_basenames=tuple(str(x) for x in raw.get("file_basenames", ())), accept=tuple(str(x) for x in raw.get("accept", ())),
            min_length=raw.get("min_length"), max_length=raw.get("max_length"), pattern=raw.get("pattern"),
            min_value=raw.get("min_value"), max_value=raw.get("max_value"), step=raw.get("step"),
            options=tuple(option(x) for x in raw.get("options", ())),
            multiple=raw.get("multiple", False),
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
def _observation_snapshot(observation: PageObservation) -> dict[str, Any]:
    """Serialize the exact private observation that authorized one action."""
    return {
        "observation_id": observation.observation_id,
        "url": observation.url,
        "title": observation.title,
        "site_markers": list(observation.site_markers),
        "fields": [
            {
                "target_id": field.target_id,
                "field_key": field.field_key,
                "frame_id": field.frame_id,
                "frame_url": field.frame_url,
                "form_action_url": field.form_action_url,
                "kind": field.kind,
                "name": field.name,
                "label": field.label,
                "group_id": field.group_id,
                "option_value": field.option_value,
                "safety_descriptors": list(field.safety_descriptors),
                "selector": field.selector,
                "required": field.required,
                "visible": field.visible,
                "enabled": field.enabled,
                "readonly": field.readonly,
                "value": field.value,
                "multiple": field.multiple,
                "will_validate": field.will_validate,
                "valid": field.valid,
                "validity_flags": list(field.validity_flags),
                "file_count": field.file_count,
                "file_basenames": list(field.file_basenames),
                "accept": list(field.accept),
                "min_length": field.min_length,
                "max_length": field.max_length,
                "pattern": field.pattern,
                "min_value": field.min_value,
                "max_value": field.max_value,
                "step": field.step,
                "options": [
                    {"value": option.value, "label": option.label, "enabled": option.enabled}
                    for option in field.options
                ],
            }
            for field in observation.fields
        ],
        "buttons": [
            {
                "target_id": button.target_id,
                "frame_id": button.frame_id,
                "frame_url": button.frame_url,
                "click_key": button.click_key,
                "element_id": button.element_id,
                "element_kind": button.element_kind,
                "text": button.text,
                "selector": button.selector,
                "button_type": button.button_type,
                "name": button.name,
                "value": button.value,
                "target": button.target,
                "download": button.download,
                "effective_action_url": button.effective_action_url,
                "effective_method": button.effective_method,
                "href_url": button.href_url,
                "href_attribute": button.href_attribute,
                "visible": button.visible,
                "enabled": button.enabled,
                "safety_descriptors": list(button.safety_descriptors),
            }
            for button in observation.buttons
        ],
        "final_submit_target_ids": list(observation.final_submit_target_ids),
        "errors": [
            {"target_id": error.target_id, "text": error.text}
            for error in observation.errors
        ],
        "blockers": [
            {"code": blocker.code, "frame_id": blocker.frame_id, "text": blocker.text}
            for blocker in observation.blockers
        ],
    }

def _observation_snapshot_sha256(observation: PageObservation) -> str:
    """Return the canonical digest of a private observation snapshot."""
    encoded = json.dumps(
        _observation_snapshot(observation),
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()




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
            field.multiple,
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
        len(final_target_ids),
        tuple(button.target_id in final_target_ids for button in observation.buttons),
        tuple(error.text for error in observation.errors),
        tuple((blocker.code, blocker.text) for blocker in observation.blockers),
    )


def _observation_page_scope_signature(observation: PageObservation) -> tuple[Any, ...]:
    """Identify page-scoped controls while ignoring values changed by filling."""
    final_target_ids = frozenset(observation.final_submit_target_ids)
    return (
        observation.url,
        observation.title,
        observation.site_markers,
        tuple(
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
                field.selector,
                field.required,
                field.visible,
                field.enabled,
                field.readonly,
                field.multiple,
                field.will_validate,
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
        ),
        tuple(
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
        ),
    )




def _plan_summary(plan: AutofillPlan) -> dict[str, Any]:
    return {
        "status": plan.status, "reason_code": _enum_reason(plan.reason_code).value,
        "answer_count": len(plan.answers), "skipped_target_count": len(plan.skipped_target_ids),
        "resume_upload": bool(plan.resume_upload_target_id), "safe_click": bool(plan.safe_click_target_id),
    }
def _answer_payload(field: ObservedField, protected: tuple[str, ...] = ()) -> dict[str, Any]:
    def redact(value: Any) -> Any:
        return _redact_text(str(value), protected) if value is not None else value
    return {
        "target_id": field.target_id,
        "kind": field.kind,
        "label": redact(_target_label(field)),
        "descriptors": [redact(item) for item in field.safety_descriptors],
        "multiple": field.multiple,
        "options": [
            {"value": redact(option.value), "label": redact(option.label), "enabled": option.enabled}
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
    page_url: str | None = None,
) -> dict[str, Any] | None:
    if not _safe_click_is_eligible(
        button,
        final_submit_target_ids,
        ats_policy=ats_policy,
        page_url=page_url,
    ):
        return None
    def redact(value: Any) -> Any:
        return _redact_text(str(value), protected) if value is not None else value
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
            page_url=observation.url,
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
    configured_values: tuple[FieldValue, ...] = (),
    protected_values: tuple[str, ...] = (),
    ats_policy: str = "greenhouse",
) -> dict[str, Any]:
    protected = _expanded_protected_values(
        _flatten_prompt_private_values(profile_facts)
        + _flatten_prompt_private_values(configured_values)
        + _flatten_prompt_private_values(protected_values)
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
        if _field_is_llm_eligible(field) and field.value in (None, "", False, ())
    ]
    buttons = _eligible_inference_buttons(
        observation,
        protected=tuple(protected),
        ats_policy=ats_policy,
    )
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

def _build_control_inference_request(
    observation: PageObservation,
    *,
    observation_sha256: str,
    job: Mapping[str, Any],
    applicant_description: str = "",
    profile_facts: Mapping[str, Any],
    resume_facts: Mapping[str, Any],
    resume_basename: str,
    deterministic: AutofillPlan,
    ats_policy: str,
    configured_values: tuple[FieldValue, ...] = (),
    protected_values: tuple[str, ...] = (),
) -> dict[str, Any]:
    """Build only the bounded, untrusted context allowed across the control edge."""
    protected = _expanded_protected_values(
        _flatten_prompt_private_values(profile_facts)
        + _flatten_prompt_private_values(resume_facts)
        + (str(resume_basename),)
        + _flatten_prompt_private_values(configured_values)
        + _flatten_prompt_private_values(protected_values)
        + _flatten_prompt_private_values(
            tuple(answer.value for answer in deterministic.answers)
        )
    )

    def bounded(value: Any) -> str:
        return _redact_text(str(value or ""), tuple(protected))[:12000]

    def fact_categories(value: Mapping[str, Any]) -> list[str]:
        keys: list[str] = []

        def collect(item: Any, *, depth: int = 0) -> None:
            if depth > 8 or not isinstance(item, Mapping):
                return
            for key, child in item.items():
                keys.append(_normal(str(key)))
                collect(child, depth=depth + 1)

        collect(value)
        markers = {
            "contact": ("contact", "email", "phone"),
            "education": ("education", "school", "degree", "university"),
            "experience": ("employment", "employer", "experience", "role", "work"),
            "links": ("linkedin", "portfolio", "website"),
            "location": ("address", "city", "country", "location", "state"),
            "skills": ("framework", "language", "skill", "technology"),
        }
        return [
            category
            for category, terms in markers.items()
            if any(any(term in key.split() for term in terms) for key in keys)
        ]

    def applicant_capabilities(value: str) -> list[str]:
        normalized = f" {_normal(value)} "
        markers = {
            "ai_ml": (" artificial intelligence ", " machine learning "),
            "backend": (" api ", " backend ", " service "),
            "data": (" analytics ", " data engineering ", " database "),
            "distributed_systems": (" distributed system ", " distributed systems "),
            "frontend": (" frontend ", " user interface ", " web application "),
            "infrastructure": (" cloud ", " devops ", " infrastructure ", " kubernetes ", " platform "),
            "leadership": (" leadership ", " management ", " mentor "),
            "mobile": (" android ", " ios ", " mobile "),
            "security": (" cybersecurity ", " security "),
        }
        return [
            category
            for category, terms in markers.items()
            if any(term in normalized for term in terms)
        ]

    allowed_job = {
        key: bounded(job.get(key))
        for key in ("title", "company", "location", "description")
    }
    deterministic_target_ids = {
        answer.target_id
        for answer in deterministic.answers
        if answer.target_id not in frozenset(deterministic.skipped_target_ids)
    }
    generation = _observation_generation(observation, observation_sha256)
    available_targets = [
        {
            "target_id": _public_element_id(field.target_id, generation=generation),
            "kind": _public_element_id(field.kind, prefix="kind", generation=generation),
        }
        for field in observation.fields
        if field.target_id in deterministic_target_ids
    ]
    available_targets.extend(
        {
            "target_id": _public_element_id(button.target_id, generation=generation),
            "kind": _public_element_id(button.element_kind, prefix="kind", generation=generation),
        }
        for button in observation.buttons
        if _safe_click_is_eligible(
            button,
            observation.final_submit_target_ids,
            ats_policy=ats_policy,
            page_url=observation.url,
        )
    )
    request = {
        "job": allowed_job,
        "available_targets": available_targets,
        "context": {
            "applicant_capabilities": applicant_capabilities(applicant_description),
            "profile_categories": fact_categories(profile_facts),
            "resume_categories": fact_categories(resume_facts),
            "deterministic_answer_count": len(deterministic.answers),
        },
    }
    if len(json.dumps(request, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) > MAX_REQUEST_BYTES:
        raise ValueError("inference_context_too_large")
    return request

def _validate_llm_answer(field: ObservedField, item: Mapping[str, Any]) -> bool:
    value = item.get("value")
    if field.kind in {"checkbox", "radio"}:
        if type(value) is not bool:
            return False
    elif field.kind == "select":
        if field.multiple:
            if isinstance(value, list):
                value = tuple(value)
            elif not isinstance(value, (tuple, str)) or isinstance(value, bool):
                return False
        elif not isinstance(value, str) or isinstance(value, bool):
            return False
    elif type(value) is not str:
        return False
    return validate_answer_value(field, value, kind=field.kind)


def _field_blocks_page_validation(field: ObservedField) -> bool:
    """Return whether an observed invalid control must stop the workflow."""
    if field.valid is not False or not field.visible or not field.enabled or field.readonly:
        return False
    return not (
        field.required
        and field.value in (None, "", False, ())
        and field.validity_flags == ("valueMissing",)
    )


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
    return field.value is not None and field.value != "" and field.value is not False and field.value != ()


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
        answer_value: str | bool | tuple[str, ...]
        if field.kind == "select":
            canonical_value = _canonicalize_select_value(field, item["value"])
            if canonical_value is None:
                return AutofillPlan(status="manual", reason_code=PublicReasonCode.invalid_llm_response)
            answer_value = canonical_value
        elif field.kind in {"checkbox", "radio"}:
            answer_value = bool(item["value"])
        else:
            answer_value = str(item["value"])
        answers.append(FieldAnswer(target_id, answer_value, float(confidence), item["reason"], "inference"))
    click = payload.get("safe_click_target_id")
    if click is not None:
        eligible = {
            button.target_id
            for button in observation.buttons
            if _safe_click_is_eligible(
                button,
                observation.final_submit_target_ids,
                ats_policy=ats_policy,
                page_url=observation.url,
            )
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

def _same_origin(left: str, right: str) -> bool:
    try:
        left_parts = urlsplit(left)
        right_parts = urlsplit(right)
        left_port = left_parts.port
        right_port = right_parts.port
    except (TypeError, ValueError):
        return False
    if left_parts.scheme != "https" or right_parts.scheme != "https":
        return False
    if not left_parts.hostname or not right_parts.hostname:
        return False
    return (
        left_parts.hostname.lower().rstrip(".") == right_parts.hostname.lower().rstrip(".")
        and (left_port or 443) == (right_port or 443)
        and left_parts.username is None
        and left_parts.password is None
        and right_parts.username is None
        and right_parts.password is None
    )




def _navigation_candidate_url(button: ObservedButton) -> str | None:
    """Return only an observed anchor href eligible for guarded GET navigation."""
    if str(button.element_kind).lower() != "a" or not isinstance(button.href_url, str) or not button.href_url:
        return None
    # Form/action metadata is deliberately excluded: this branch never submits
    # a form, including a form whose method happens to be GET.
    if button.effective_action_url or button.effective_method:
        return None
    return button.href_url


def _navigation_continuation_permitted(
    button: ObservedButton,
    final_submit_target_ids: tuple[str, ...],
    *,
    ats_policy: str,
    page_url: str | None,
    approved_route_identity: tuple[Any, ...] | None = None,
) -> bool:
    """Permit one observed, same-job, non-final anchor GET continuation."""
    candidate = _navigation_candidate_url(button)
    if (
        candidate is None
        or button.frame_id != "frame-0"
        or button.target_id in final_submit_target_ids
        or not isinstance(button.click_key, str)
        or not button.click_key
        or not _frame_origin_allowed(button.frame_url, ats_policy)
        or not button.visible
        or not button.enabled
        or _field_is_sensitive_button(button)
        or button.target not in (None, "")
        or button.download
        or page_url is None
        or not _same_origin(button.frame_url, page_url)
        or not _same_origin(candidate, page_url)
    ):
        return False
    current_route = _application_route_identity(page_url, ats_policy)
    candidate_route = _application_route_identity(candidate, ats_policy)
    expected_route = approved_route_identity or current_route
    return _continuation_route_is_approved(expected_route, candidate_route)


def _native_progress_button_is_allowed(button: ObservedButton) -> bool:
    """Allow only explicit non-final application-progress button semantics."""
    raw = " ".join(
        item
        for item in (
            button.text,
            button.value,
            button.name,
            *button.safety_descriptors,
        )
        if isinstance(item, str) and item.strip()
    )
    normalized = " ".join(re.sub(r"[^a-z0-9]+", " ", raw.lower()).split())
    if (
        not normalized
        or re.search(r"\b(?:continue|next|proceed|save and continue)\s+(?:with|via|using)\b", normalized)
        or re.search(
            r"\b(?:apply|alert|another|quick|mygreenhouse|sso|oauth|account|login|log in|sign in|auth|authenticate|authentication|submit|confirm|finish|send|final|finalize)\b",
            normalized,
        )
    ):
        return False
    return bool(
        re.match(r"^(?:continue|next|proceed)(?:\b|$)", normalized)
        or re.match(r"^save and continue(?:\b|$)", normalized)
    )


def _safe_click_is_eligible(
    button: ObservedButton,
    final_submit_target_ids: tuple[str, ...] = (),
    *,
    ats_policy: str = "greenhouse",
    page_url: str | None = None,
) -> bool:
    button_type = str(button.button_type).lower()
    element_kind = button.element_kind.lower()
    is_native_offline = (
        (element_kind == "button" and button_type in {"button", "submit"})
        or (element_kind == "input" and button_type == "button")
    )
    common = bool(
        button.target_id not in final_submit_target_ids
        and isinstance(button.click_key, str)
        and bool(button.click_key)
        and _frame_origin_allowed(button.frame_url, ats_policy)
        and button.visible
        and button.enabled
        and not _field_is_sensitive_button(button)
        and button.target in (None, "")
        and not button.download
    )
    if not common:
        return False
    if _navigation_continuation_permitted(
        button,
        final_submit_target_ids,
        ats_policy=ats_policy,
        page_url=page_url,
    ):
        return True
    is_native_without_navigation = (
        is_native_offline
        and not button.effective_action_url
        and not button.effective_method
        and not button.href_url
        and not button.href_attribute
        and (page_url is None or _same_origin(button.frame_url, page_url))
        and _native_progress_button_is_allowed(button)
    )
    if not is_native_without_navigation:
        return False
    if button_type == "button":
        return True
    # A submit-typed continuation is only safe when it cannot submit a form:
    # the runner proves this by requiring a native button with no form action.
    return _same_origin(button.frame_url, page_url)


def _continuation_permitted(
    button: ObservedButton,
    final_submit_target_ids: tuple[str, ...],
    *,
    ats_policy: str,
    page_url: str | None,
    approved_route_identity: tuple[Any, ...] | None = None,
) -> bool:
    """Return the explicit permit for a native submit or anchor GET continuation."""
    return (
        (
            str(button.button_type).lower() == "submit"
            and _safe_click_is_eligible(
                button,
                final_submit_target_ids,
                ats_policy=ats_policy,
                page_url=page_url,
            )
        )
        or _navigation_continuation_permitted(
            button,
            final_submit_target_ids,
            ats_policy=ats_policy,
            page_url=page_url,
            approved_route_identity=approved_route_identity,
        )
    )




def _application_route_identity(url: str, ats_policy: str) -> tuple[Any, ...] | None:
    """Project an approved ATS URL to the identity used by continuation gates."""
    try:
        route = validate_ats_url(url, ats_policy)
    except (BrowserAdapterError, TypeError, ValueError):
        return None
    if ats_policy == "lever":
        parts = route.path.strip("/").split("/")
        if len(parts) < 2:
            return None
        return ("lever_job", route.host, parts[0], parts[1])
    if route.mode == "greenhouse_embed":
        parsed = urlsplit(route.url)
        try:
            query = tuple(sorted(parse_qsl(parsed.query, keep_blank_values=True, strict_parsing=True)))
        except ValueError:
            return None
        return (route.mode, route.host, route.path, query)
    return (route.mode, route.host, route.path)


def _continuation_route_is_approved(
    expected: tuple[Any, ...] | None,
    candidate: tuple[Any, ...] | None,
) -> bool:
    if expected is None or candidate is None:
        return False
    if expected == candidate:
        return True
    # A grnh.se short route has no job identity until its approved hosted
    # redirect; retain the runner's one-time route establishment behavior.
    return expected[0] == "greenhouse_short" and candidate[0] == "greenhouse_job"


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
    preferences: ApplicationPreferences | None = None,
    deterministic: AutofillPlan | None = None,
    protected_values: tuple[str, ...] = (),
    api_key: str | None = None,
    base_url: str | None = None,
    model: str | None = None,
    mutated: bool = False,
    ats_policy: str = "greenhouse",
) -> AutofillPlan:
    if mutated:
        return AutofillPlan(status="manual", reason_code=PublicReasonCode.no_deterministic_next_step)
    profile_facts: Mapping[str, Any]
    configured_value_items: list[FieldValue] = []
    if isinstance(profile_context, ApplicationProfile):
        profile_facts = thaw_json(profile_context.facts)
        configured_value_items.extend(
            answer.value for answer in profile_context.field_answers
        )
    else:
        profile_facts = profile_context or {}
    if preferences is not None:
        configured_value_items.extend(mapping.value for mapping in preferences.mappings)
    if deterministic is not None:
        configured_value_items.extend(answer.value for answer in deterministic.answers)
    configured_values = tuple(configured_value_items)
    text = resume_context.text if isinstance(resume_context, ResumeContext) else str(resume_context or "")
    try:
        request = build_inference_request(
            observation,
            job=job,
            resume_text=text,
            profile_facts=profile_facts,
            configured_values=configured_values,
            protected_values=protected_values,
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
    user_content = json.dumps(request, ensure_ascii=False, separators=(",", ":"))
    body = {
        "model": model or os.environ.get("OLLAMA_CLOUD_MODEL") or os.environ.get("DEEPSEEK_MODEL") or DEFAULT_LLM_MODEL,
        "messages": [
            {"role": "system", "content": LLM_RESPONSE_CONTRACT},
            {"role": "user", "content": user_content},
        ],
        "think": os.environ.get("OLLAMA_CLOUD_THINK") or os.environ.get("OLLAMA_CLOUD_REASONING") or DEFAULT_LLM_THINK,
        "stream": True,
    }
    if len(json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) > MAX_REQUEST_BYTES:
        return AutofillPlan(status="manual", reason_code=PublicReasonCode.inference_context_too_large)
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
        protected_sources = (
            _flatten_prompt_private_values(profile_facts)
            + _flatten_prompt_private_values(configured_values)
            + _flatten_prompt_private_values(protected_values)
        )
        if not validate_inference_privacy(plan, protected_values=protected_sources, source_text=text):
            return AutofillPlan(status="manual", reason_code=PublicReasonCode.inference_privacy_violation)
        return plan
    except httpx.HTTPError:
        return AutofillPlan(status="manual", reason_code=PublicReasonCode.llm_request_failed)
    except (ValueError, TypeError, KeyError):
        return AutofillPlan(status="manual", reason_code=PublicReasonCode.invalid_llm_response)


def unresolved_required_fields(observation: PageObservation, answers: tuple[FieldAnswer, ...]) -> tuple[str, ...]:
    """Evaluate required controls using the adapter's group semantics."""
    unresolved = _ats_unresolved_required_fields(observation, answers)
    by_id = _unique_observed_fields(observation)
    if by_id is None:
        return unresolved
    return tuple(
        target_id
        for target_id in unresolved
        if not _field_existing_value_resolved(by_id[target_id])
    )

def _configured_and_profile_plan(
    observation: PageObservation,
    *,
    adapter: ATSAdapter,
    context: ApplicationContext,
    profile: ApplicationProfile,
    resume: ResumeContext,
    preferences: ApplicationPreferences | None = None,
) -> AutofillPlan:
    if any(_field_blocks_page_validation(field) for field in observation.fields):
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
            preference_fields: list[ObservedField] = []
            for field in observation.fields:
                if (
                    not field.visible
                    or not field.enabled
                    or field.readonly
                    or (
                        field.valid is False
                        and not (
                            field.required
                            and field.value in (None, "", False, ())
                            and field.validity_flags == ("valueMissing",)
                        )
                    )
                    or str(field.kind).lower() == "file"
                    or "field_identity_collision" in field.validity_flags
                    or _field_is_sensitive(field)
                ):
                    continue
                try:
                    normalize_field_descriptor(field, ats=adapter.name)
                except PreferenceValidationError:
                    continue
                preference_fields.append(field)
            pref_result = apply_preferences(preferences, tuple(preference_fields), deterministic, ats=adapter.name)
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
            if not _field_existing_value_resolved(field) and field_accepts_resume(field, resume):
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


def _same_value(field: ObservedField, value: str | bool | tuple[str, ...]) -> bool:
    if type(value) is tuple or type(field.value) is tuple:
        return type(value) is tuple and type(field.value) is tuple and value == field.value
    if type(value) is bool or type(field.value) is bool:
        return type(value) is bool and type(field.value) is bool and value == field.value
    return _normal(str(field.value or "")) == _normal(str(value or ""))


def _retained_value_equal(field: ObservedField, expected: str | bool | tuple[str, ...]) -> bool:
    if type(expected) is tuple or type(field.value) is tuple:
        return type(expected) is tuple and type(field.value) is tuple and expected == field.value
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
                    "value_length": len(answer.value) if isinstance(answer.value, (str, tuple)) else None,
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
        if (
            _safe_click_is_eligible(
                button,
                observation.final_submit_target_ids,
                ats_policy=ats_policy,
                page_url=observation.url,
            )
            if button is not None
            else False
        ):
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


def _screenshot_payload(run: ArtifactRun, payload: Any) -> dict[str, Any]:
    """Validate one browser screenshot response and its private file."""
    if not isinstance(payload, Mapping):
        raise BrowserAdapterError("protocol_invalid_response")
    required = (
        "path",
        "reference",
        "bytes",
        "sha256",
        "full_page",
        "truncated",
        "pixel_width",
        "pixel_height",
    )
    if any(key not in payload for key in required):
        raise BrowserAdapterError("protocol_invalid_response")
    path = payload.get("path")
    if (
        not isinstance(path, str)
        or not path
        or len(path) > 256
        or Path(path).name != path
        or path in {".", ".."}
        or "\\" in path
        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,255}\.png", path)
    ):
        raise BrowserAdapterError("artifact_error")
    reference = payload.get("reference")
    digest = payload.get("sha256")
    size = payload.get("bytes")
    if (
        not isinstance(reference, str)
        or not isinstance(digest, str)
        or re.fullmatch(r"[0-9a-f]{64}", digest) is None
        or reference != f"screenshot:{digest}"
        or type(size) is not int
        or size < 0
        or size > MAX_SCREENSHOT_BYTES
    ):
        raise BrowserAdapterError("protocol_invalid_response")
    full_page = payload.get("full_page")
    truncated = payload.get("truncated")
    pixel_width = payload.get("pixel_width")
    pixel_height = payload.get("pixel_height")
    if (
        type(full_page) is not bool
        or type(truncated) is not bool
        or type(pixel_width) is not int
        or type(pixel_height) is not int
        or pixel_width < 0
        or pixel_height < 0
    ):
        raise BrowserAdapterError("protocol_invalid_response")
    deduplicated = payload.get("deduplicated", False)
    if type(deduplicated) is not bool:
        raise BrowserAdapterError("protocol_invalid_response")
    relative_path = f"screenshots/{path}"
    try:
        persisted = run.read_bytes(
            relative_path,
            max_bytes=MAX_SCREENSHOT_BYTES,
            expected_sha256=digest,
        )
    except Exception:
        raise BrowserAdapterError("artifact_error") from None
    if len(persisted) != size:
        raise BrowserAdapterError("artifact_error")
    return {
        "path": relative_path,
        "reference": reference,
        "bytes": size,
        "sha256": digest,
        "full_page": full_page,
        "truncated": truncated,
        "pixel_width": pixel_width,
        "pixel_height": pixel_height,
        "deduplicated": deduplicated,
    }


def _screenshot_index_state(payload: Mapping[str, Any]) -> tuple[dict[str, tuple[str, int]], int]:
    raw = payload.get("screenshots", {})
    if not isinstance(raw, dict):
        raise BrowserAdapterError("artifact_error")
    distinct: dict[str, tuple[str, int]] = {}
    for slot, item in raw.items():
        if slot not in SCREENSHOT_SLOTS or not isinstance(item, Mapping):
            raise BrowserAdapterError("artifact_error")
        path = item.get("path")
        digest = item.get("sha256")
        size = item.get("bytes")
        reference = item.get("reference")
        if (
            not isinstance(path, str)
            or not path.startswith("screenshots/")
            or path.count("/") != 1
            or not re.fullmatch(r"screenshots/[A-Za-z0-9][A-Za-z0-9._-]{0,255}\.png", path)
            or not isinstance(digest, str)
            or re.fullmatch(r"[0-9a-f]{64}", digest) is None
            or reference != f"screenshot:{digest}"
            or type(size) is not int
            or size < 0
            or size > MAX_SCREENSHOT_BYTES
        ):
            raise BrowserAdapterError("artifact_error")
        prior = distinct.get(digest)
        if prior is not None and prior != (path, size):
            raise BrowserAdapterError("artifact_error")
        distinct[digest] = (path, size)
    return distinct, sum(size for _, size in distinct.values())


def _manifest_set_screenshot(
    run: ArtifactRun,
    payload: dict[str, Any],
    slot: str,
    screenshot: Mapping[str, Any],
    *,
    iteration: int,
    stage: str,
) -> None:
    if slot not in SCREENSHOT_SLOTS:
        raise BrowserAdapterError("artifact_error")
    distinct, total_bytes = _screenshot_index_state(payload)
    digest = screenshot["sha256"]
    path = screenshot["path"]
    size = screenshot["bytes"]
    prior = distinct.get(digest)
    if prior is not None and prior != (path, size):
        raise BrowserAdapterError("artifact_error")
    raw_screenshots = payload.get("screenshots", {})
    if not isinstance(raw_screenshots, dict):
        raise BrowserAdapterError("artifact_error")
    existing_slot = raw_screenshots.get(slot)
    if existing_slot is not None and (
        not isinstance(existing_slot, Mapping)
        or existing_slot.get("sha256") != digest
    ):
        raise BrowserAdapterError("artifact_error")
    if prior is None and (
        len(distinct) >= MAX_SCREENSHOTS_PER_RUN
        or total_bytes + size > MAX_SCREENSHOT_TOTAL_BYTES
    ):
        raise BrowserAdapterError("artifact_error")
    indexed = dict(screenshot)
    indexed["iteration"] = iteration
    indexed["stage"] = stage
    screenshots = dict(raw_screenshots)
    screenshots[slot] = indexed
    raw_artifacts = payload.get("artifacts", {})
    if not isinstance(raw_artifacts, dict):
        raise BrowserAdapterError("artifact_error")
    artifacts = dict(raw_artifacts)
    artifacts[f"screenshot_{slot.replace('-', '_')}"] = {
        "path": path,
        "reference": screenshot["reference"],
        "sha256": digest,
        "bytes": size,
        "iteration": iteration,
        "stage": stage,
    }
    updated = dict(payload)
    updated["screenshots"] = screenshots
    updated["artifacts"] = artifacts
    _write_run_manifest(run, updated)
    payload.clear()
    payload.update(updated)




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
def _action_evidence_payload(
    *,
    iteration: int,
    observation_id: str,
    observation_artifact: str,
    observation_sha256: str,
    ats_policy: str,
    planned: list[dict[str, Any]],
    rejected: list[dict[str, Any]],
    continuation_permit: bool | None = None,
) -> dict[str, Any]:
    payload = {
        "version": 2,
        "iteration": iteration,
        "observation_id": observation_id,
        "observation_artifact": observation_artifact,
        "observation_sha256": observation_sha256,
        "ats_policy": ats_policy,
        "no_final_submit": True,
        "planned": planned,
        "rejected": rejected,
    }
    if continuation_permit is not None:
        payload["continuation_permit"] = continuation_permit
    return payload




def _manifest_token_hash(path: Path) -> str | None:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    value = raw.get("commit_token_sha256") if isinstance(raw, Mapping) else None
    return value if isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) else None

def _manifest_commit_evidence(
    path: Path,
    *,
    token_hash: str,
    run_id: int,
    job_id: int,
    session_id: str,
    owner_identity: Mapping[str, Any] | None,
    browser_identity: Mapping[str, Any] | None,
) -> bool:
    """Recognize detached ownership only when identities bind exactly."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return False
    if not isinstance(payload, Mapping):
        return False
    if (
        payload.get("state") not in {"open_guarded", "closed", "failed"}
        or payload.get("detached") is not True
        or payload.get("commit_token_sha256") != token_hash
        or payload.get("run_id") != run_id
        or payload.get("job_id") != job_id
        or payload.get("session_id") != session_id
        or not isinstance(owner_identity, Mapping)
        or not isinstance(browser_identity, Mapping)
        or payload.get("owner_identity") != dict(owner_identity)
        or payload.get("browser_identity") != dict(browser_identity)
    ):
        return False
    return True


_HANDOFF_FINALIZATION_ARTIFACT_KEYS = frozenset(
    {
        "automated_submission",
        "child_request_id",
        "commit_token_sha256",
        "job_id",
        "observation_sha256",
        "operation",
        "parent_request_id",
        "reason_code",
        "run_id",
        "session_id",
        "status",
        "unresolved_required_count",
        "version",
    }
)


def _handoff_finalization_payload(
    *,
    run_id: int,
    job_id: int,
    session_id: str,
    proposal: BrowserToolProposal,
    status: str,
    reason_code: str,
    observation_sha256: str,
    unresolved_required_count: int,
    commit_token_sha256: str,
) -> dict[str, Any]:
    payload = {
        "version": 1,
        "run_id": run_id,
        "job_id": job_id,
        "session_id": session_id,
        "child_request_id": proposal.request.request_id,
        "parent_request_id": proposal.parent_request_id,
        "operation": proposal.request.operation,
        "status": status,
        "reason_code": reason_code,
        "observation_sha256": observation_sha256,
        "unresolved_required_count": unresolved_required_count,
        "automated_submission": False,
        "commit_token_sha256": commit_token_sha256,
    }
    if (
        set(payload) != _HANDOFF_FINALIZATION_ARTIFACT_KEYS
        or type(run_id) is not int
        or run_id <= 0
        or type(job_id) is not int
        or job_id <= 0
        or type(session_id) is not str
        or not session_id
        or type(payload["child_request_id"]) is not str
        or type(payload["parent_request_id"]) is not str
        or re.fullmatch(r"[0-9a-f-]{36}", payload["child_request_id"]) is None
        or re.fullmatch(r"[0-9a-f-]{36}", payload["parent_request_id"]) is None
        or proposal.request.operation != "browser.prepare_human_handoff"
        or type(status) is not str
        or status not in {"review_ready", "manual", "blocked"}
        or type(reason_code) is not str
        or _status_for_reason(reason_code) != status
        or type(observation_sha256) is not str
        or re.fullmatch(r"[0-9a-f]{64}", observation_sha256) is None
        or type(commit_token_sha256) is not str
        or re.fullmatch(r"[0-9a-f]{64}", commit_token_sha256) is None
        or type(unresolved_required_count) is not int
        or unresolved_required_count < 0
    ):
        raise RuntimeError("handoff_finalization_schema")
    return payload


def _validate_handoff_finalization_payload(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, Mapping) or set(payload) != _HANDOFF_FINALIZATION_ARTIFACT_KEYS:
        raise RuntimeError("handoff_finalization_schema")
    if (
        type(payload.get("version")) is not int
        or payload["version"] != 1
        or type(payload.get("run_id")) is not int
        or payload["run_id"] <= 0
        or type(payload.get("job_id")) is not int
        or payload["job_id"] <= 0
        or type(payload.get("session_id")) is not str
        or not payload["session_id"]
        or type(payload.get("child_request_id")) is not str
        or re.fullmatch(r"[0-9a-f-]{36}", payload["child_request_id"]) is None
        or type(payload.get("parent_request_id")) is not str
        or re.fullmatch(r"[0-9a-f-]{36}", payload["parent_request_id"]) is None
        or payload.get("operation") != "browser.prepare_human_handoff"
        or type(payload.get("status")) is not str
        or payload["status"] not in {"review_ready", "manual", "blocked"}
        or type(payload.get("reason_code")) is not str
        or _status_for_reason(payload["reason_code"]) != payload["status"]
        or type(payload.get("observation_sha256")) is not str
        or re.fullmatch(r"[0-9a-f]{64}", payload["observation_sha256"]) is None
        or type(payload.get("commit_token_sha256")) is not str
        or re.fullmatch(r"[0-9a-f]{64}", payload["commit_token_sha256"]) is None
        or type(payload.get("unresolved_required_count")) is not int
        or payload["unresolved_required_count"] < 0
        or payload.get("automated_submission") is not False
    ):
        raise RuntimeError("handoff_finalization_schema")
    return dict(payload)


def _validate_indexed_manifest_artifacts(run: ArtifactRun, manifest: Mapping[str, Any]) -> None:
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, Mapping):
        raise RuntimeError("manifest_error")
    screenshot_total = 0
    screenshot_paths: set[str] = set()
    for descriptor in artifacts.values():
        if not isinstance(descriptor, Mapping):
            raise RuntimeError("manifest_error")
        path = descriptor.get("path")
        digest = descriptor.get("sha256")
        if (
            type(path) is not str
            or not path
            or type(digest) is not str
            or re.fullmatch(r"[0-9a-f]{64}", digest) is None
        ):
            raise RuntimeError("manifest_error")
        if path.startswith("screenshots/"):
            max_bytes = MAX_SCREENSHOT_BYTES
        elif path.startswith("input/"):
            max_bytes = 10 * 1024 * 1024
        else:
            max_bytes = 8 * 1024 * 1024
        data = run.read_bytes(path, max_bytes=max_bytes, expected_sha256=digest)
        if path.startswith("screenshots/") and path not in screenshot_paths:
            screenshot_paths.add(path)
            screenshot_total += len(data)
    if len(screenshot_paths) > MAX_SCREENSHOTS_PER_RUN or screenshot_total > MAX_SCREENSHOT_TOTAL_BYTES:
        raise RuntimeError("manifest_error")


def _write_handoff_finalization_artifact(
    run: ArtifactRun,
    manifest_payload: dict[str, Any],
    payload: Mapping[str, Any],
    *,
    iteration: int,
) -> tuple[Any, Any]:
    validated = _validate_handoff_finalization_payload(payload)
    artifact = _write_json_verified(run, "handoff_finalization.json", validated)
    _manifest_set_artifact(
        manifest_payload,
        "handoff_finalization",
        artifact,
        "handoff_finalization.json",
        iteration=iteration,
        stage="prepared",
    )
    manifest_result = _write_run_manifest(run, manifest_payload)
    _validate_indexed_manifest_artifacts(run, manifest_payload)
    return artifact, manifest_result
@dataclass(frozen=True)
class _BrowserCallOutcome:
    """Preserve a browser call outcome when the waiting task is cancelled."""

    cancelled: bool
    value: Any = None
    error: BaseException | None = None


def _unwrap_browser_call_outcome(value: Any) -> Any:
    if not isinstance(value, _BrowserCallOutcome):
        return value
    if value.error is not None:
        raise value.error
    return value.value


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
    *,
    capture_cancellation: bool = False,
) -> Any:
    try:
        result = await _await_browser_call(
            call,
            capture_cancellation=capture_cancellation,
        )
        if isinstance(result, _BrowserCallOutcome) and result.error is not None:
            error = result.error
            if isinstance(error, _BrowserFailure):
                raise error
            if isinstance(error, BrowserAdapterError):
                raise _BrowserFailure(
                    stage,
                    operation,
                    normalize_browser_error_code(str(error)),
                    iteration,
                ) from None
            raise _BrowserFailure(
                stage,
                operation,
                "browser_command_failed",
                iteration,
            ) from None
        return _unwrap_browser_call_outcome(result)
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


async def _run_browser_call(call: Any) -> Any:
    result = await asyncio.to_thread(call)
    return await _maybe(result)


async def _drain_browser_call(task: asyncio.Task[Any]) -> None:
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            continue
        except BaseException:
            return


async def _await_browser_call(
    call: Any,
    *,
    capture_cancellation: bool = False,
) -> Any:
    task = asyncio.create_task(_run_browser_call(call), name="application-browser-call")
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        await _drain_browser_call(task)
        # A cancellation raised by the browser coroutine itself is an
        # operation outcome, not cancellation of the workflow waiter.
        if task.cancelled() or not capture_cancellation:
            raise
        try:
            value = task.result()
        except BaseException as error:
            return _BrowserCallOutcome(cancelled=True, error=error)
        return _BrowserCallOutcome(cancelled=True, value=value)
    except BaseException:
        await _drain_browser_call(task)
        raise
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
    await _await_browser_call(session.close)


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

def _supervise_postcommit_handoff_failure(session: Any) -> str:
    """Probe and, when partial, reap the exact bound handoff identities."""
    identities = {
        "owner": getattr(session, "_handoff_owner_identity", None)
        or getattr(session, "owner_identity", None),
        "browser": getattr(session, "_handoff_browser_identity", None)
        or getattr(session, "browser_identity", None),
    }
    try:
        mode = _supervise_partial_handoff_processes(identities)
    except Exception:
        return "unknown"
    return mode if mode in {"healthy", "partial", "absent"} else "unknown"



def _control_progress_code(event_type: str, summary_code: Any) -> str:
    if isinstance(summary_code, PublicReasonCode):
        candidate = summary_code.value
    else:
        candidate = str(summary_code or "")
    if candidate in _CONTROL_PROGRESS_CODES:
        return candidate
    return {
        "page_observed": "observed",
        "action_allowed": "allowed",
        "action_rejected": "rejected",
        "screenshot_captured": "captured",
        "manual_intervention_required": "manual_required",
        "review_ready": "review_ready",
        "browser_handed_off": "handed_off",
        "run_failed": "failed",
    }.get(event_type, "failed")


async def _record_control_progress(
    control: ApplicationWorkflowControl,
    run_id: int,
    event_type: str,
    summary_code: Any,
    action_sequence: int,
    *,
    observation_sha256: str | None = None,
    request_id: str | None = None,
) -> None:
    if event_type not in _CONTROL_PROGRESS_EVENTS:
        raise RuntimeError("invalid_progress_event")
    await _maybe(control.record_progress(
        run_id,
        event_type,
        _control_progress_code(event_type, summary_code),
        action_sequence,
        observation_sha256=observation_sha256,
        request_id=request_id,
    ))


def _validate_control_result(
    proposal: BrowserToolProposal,
    result: Mapping[str, Any],
    *,
    state: str,
) -> dict[str, Any]:
    try:
        validated = validate_public_result(
            result,
            operation=proposal.request.operation,
        )
    except Exception:
        raise RuntimeError("invalid_control_result") from None
    if not isinstance(validated, Mapping):
        raise RuntimeError("invalid_control_result")
    return thaw_json(validated)


async def _finish_control_proposal(
    control: ApplicationWorkflowControl,
    proposal: BrowserToolProposal,
    action_sequence: int,
    *,
    ok: bool,
    state: str,
    result: Mapping[str, Any] | None = None,
    error_code: str | None = None,
    application_finalization: Mapping[str, Any] | None = None,
) -> bool:
    validated_result = (
        _validate_control_result(proposal, result, state=state)
        if result is not None
        else None
    )
    callback = control.proposal_finished
    kwargs: dict[str, Any] = {
        "proposal": proposal,
        "action_sequence": action_sequence,
        "ok": bool(ok),
        "state": state,
        "result": validated_result,
        "error_code": error_code,
    }
    if application_finalization is not None:
        kwargs["application_finalization"] = application_finalization
    try:
        parameters = inspect.signature(callback).parameters
    except (TypeError, ValueError):
        parameters = {}
    if parameters and not any(
        item.kind is inspect.Parameter.VAR_KEYWORD for item in parameters.values()
    ):
        kwargs = {
            key: value
            for key, value in kwargs.items()
            if key in parameters
        }
    value = await _maybe(callback(**kwargs))
    return value is True


async def _finalize_control_failure(
    control: ApplicationWorkflowControl,
    run_id: int,
    *,
    status: str,
    reason_code: str,
    observation_summary: Mapping[str, Any],
    plan_summary: Mapping[str, Any],
    artifact_dir: str | None,
    pending_proposal: BrowserToolProposal | None = None,
    action_sequence: int = 0,
    error_code: str | None = None,
    observation_sha256: str | None = None,
    manifest_sha256: str | None = None,
) -> bool:
    callback = getattr(control, "finalize_failure", None)
    if not callable(callback):
        return False
    kwargs: dict[str, Any] = {
        "run_id": run_id,
        "status": status,
        "reason_code": reason_code,
        "observation_summary": observation_summary,
        "plan_summary": plan_summary,
        "artifact_dir": artifact_dir,
        "pending_proposal": pending_proposal,
        "action_sequence": action_sequence,
        "error_code": error_code,
        "observation_sha256": observation_sha256,
        "manifest_sha256": manifest_sha256,
    }
    try:
        parameters = inspect.signature(callback).parameters
    except (TypeError, ValueError):
        parameters = {}
    if parameters and not any(
        item.kind is inspect.Parameter.VAR_KEYWORD for item in parameters.values()
    ):
        kwargs = {
            key: value
            for key, value in kwargs.items()
            if key in parameters
        }
    value = await _maybe(callback(**kwargs))
    return value is True


def _control_error_code(reason: str | None) -> str:
    if reason in {"stale_observation_hash", "stale_option"}:
        return "stale_observation"
    if reason in {"abandoned_running_attempt", "cancelled"}:
        return "cancelled"
    if reason in {"manual_intervention_required", "no_deterministic_next_step"}:
        return "manual_intervention_required"
    if reason == "deadline_exceeded":
        return "deadline_exceeded"
    return "action_rejected"


def _validate_expected_content_hash(value: str | None, *, error_code: str) -> str | None:
    if value is None:
        return None
    if type(value) is not str or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise ValueError(error_code)
    return value

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
    claim_provider: Any = None,
    control: ApplicationWorkflowControl | None = None,
    expected_resume_sha256: str | None = None,
    expected_profile_sha256: str | None = None,
    application_preferences_snapshot: ApplicationPreferences | None = None,
    applicant_description_snapshot: str | None = None,
) -> list[dict[str, Any]]:
    if type(limit) is not int or not 1 <= limit <= 10:
        raise ValueError("limit must be between 1 and 10")
    if application_profile_json is not None and application_profile_preset is not None:
        raise ValueError("application profile JSON and preset are mutually exclusive")
    if application_profile_dir is not None and application_profile_preset is None:
        raise ValueError("application profile directory requires a preset")
    if ats not in {"auto", *SUPPORTED_ATS}:
        raise ValueError("unsupported_ats")
    expected_resume_sha256 = _validate_expected_content_hash(
        expected_resume_sha256,
        error_code="configured_resume_changed",
    )
    expected_profile_sha256 = _validate_expected_content_hash(
        expected_profile_sha256,
        error_code="candidate_profile_changed",
    )
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
    configured_profile_sha256 = (
        profile_provenance["content_sha256"]
        if profile_provenance is not None
        else (
            profile_json_provenance["sha256"]
            if profile_json_provenance is not None
            else DEFAULT_APPLICATION_PROFILE_SHA256
        )
    )
    if application_preferences_snapshot is None:
        preferences = load_application_preferences(
            application_preferences,
            cwd=Path.cwd(),
        )
    else:
        preferences = application_preferences_snapshot
    preferences_provenance = (
        {"sha256": preferences.source_sha256}
        if preferences.source_sha256 is not None
        else None
    )
    applicant_description = (
        applicant_description_snapshot
        if applicant_description_snapshot is not None
        else load_applicant_description(applicant_description_file, profile)
    )
    results: list[dict[str, Any]] = []
    owner = f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"
    with load_resume_context(resume_file) as resume, ArtifactRoot.open(artifact_root, cwd=Path.cwd()) as artifacts:
        if (
            expected_resume_sha256 is not None
            and not hmac.compare_digest(resume.sha256, expected_resume_sha256)
        ):
            raise ValueError("configured_resume_changed")
        if (
            expected_profile_sha256 is not None
            and not hmac.compare_digest(configured_profile_sha256, expected_profile_sha256)
        ):
            raise ValueError("candidate_profile_changed")
        _claim_fn = claim_provider if claim_provider is not None else claim_next_application_job
        for claim_index in range(limit):
            # A coordinator-supplied claim is already frozen and owned.  It is
            # consumed exactly once; never re-query or reclaim it.
            if claim_provider is not None and claim_index:
                break
            claim = _claim_fn(connection, owner=owner)
            if claim is None:
                break
            if not isinstance(claim, ApplicationClaim):
                raise TypeError("claim_provider must return ApplicationClaim")
            run_id = claim.run_id
            job = thaw_json(claim.job)
            run: ArtifactRun | None = None
            session: PuppeteerSession | Any | None = None
            committed = False
            window_state = "closed"
            session_reconciled = False
            application_finished = False
            post_commit_guard = False
            handoff_intent_bound = False
            artifact_ref: str | None = None
            reason = PublicReasonCode.browser_error
            status = "failed"
            run_dir_path: Path | None = None
            post_commit_reconciled = False
            manifest_payload: dict[str, Any] | None = None
            session_id: str | None = None
            latest_iteration = 0
            adapter_name = "greenhouse"
            pending_proposal: BrowserToolProposal | None = None
            pending_action_sequence = 0
            pending_action: dict[str, Any] | None = None
            mutation_count = 0
            executed_actions: list[dict[str, Any]] = []
            observation_result: Any | None = None
            plan_result: Any | None = None
            actions_result: Any | None = None
            filled_state_result: Any | None = None
            run_protected_values: list[str] = []

            def remember_protected_values(value: Any) -> None:
                for item in _flatten_prompt_private_values(value):
                    if item and item not in run_protected_values:
                        run_protected_values.append(item)
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
                application_route_identity = _application_route_identity(url, adapter.name)
                if application_route_identity is None:
                    raise RuntimeError("invalid_application_url")
                if control is not None:
                    await _maybe(control.on_claimed(
                        run_id=run_id,
                        job_id=int(job.get("id", 0)),
                        ats_policy=adapter_name,
                        application_url=url,
                    ))
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
                    "screenshots": {},
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
                startup_owner_identity: dict[str, Any] | None = None
                startup_browser_identity: dict[str, Any] | None = None

                def _identity_arg(args: tuple[Any, ...]) -> dict[str, Any] | None:
                    return next((item for item in reversed(args) if isinstance(item, Mapping)), None)

                def capture_owner_identity(*args: Any) -> bool:
                    nonlocal startup_owner_identity
                    identity = _identity_arg(args)
                    if identity is not None:
                        startup_owner_identity = dict(identity)
                    return True

                def capture_browser_identity(*args: Any) -> bool:
                    nonlocal startup_browser_identity
                    identity = _identity_arg(args)
                    if identity is not None:
                        startup_browser_identity = dict(identity)
                    return True

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
                    "screenshot_root": run_dir_path / "screenshots",
                    "session_manifest": session_manifest, "staged_input": resume.basename,
                    "staged_sha256": resume.sha256, "staged_media_type": resume.media_type,
                    "session_id": session_id, "run_id": run_id, "job_id": int(job.get("id", 0)),
                    "ats_policy": adapter.name,
                }
                parameters = inspect.signature(PuppeteerSession.start).parameters
                accepts_kwargs = any(item.kind is inspect.Parameter.VAR_KEYWORD for item in parameters.values())
                if "ats_policy" not in parameters and not accepts_kwargs:
                    start_kwargs.pop("ats_policy", None)
                if "screenshot_root" not in parameters and not accepts_kwargs:
                    start_kwargs.pop("screenshot_root", None)
                if "on_owner_identity" in parameters or accepts_kwargs:
                    start_kwargs["on_owner_identity"] = capture_owner_identity
                if "on_browser_identity" in parameters or accepts_kwargs:
                    start_kwargs["on_browser_identity"] = capture_browser_identity
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
                if control is not None and await _maybe(control.cancellation_requested(run_id)):
                    raise RuntimeError("abandoned_running_attempt")
                startup_result = await _invoke_browser(
                    "start",
                    "startup",
                    0,
                    lambda: PuppeteerSession.start(**start_kwargs),
                    capture_cancellation=True,
                )
                if isinstance(startup_result, _BrowserCallOutcome):
                    startup_session = startup_result.value
                    if startup_session is not None:
                        session = startup_session
                        try:
                            await _invoke_browser(
                                "close",
                                "cleanup",
                                0,
                                lambda: startup_session.close(),
                                capture_cancellation=True,
                            )
                        except BaseException:
                            pass
                    if startup_result.error is not None:
                        raise startup_result.error
                    raise asyncio.CancelledError
                session = startup_result
                if not owner_registered:
                    on_owner_identity(startup_owner_identity or getattr(session, "owner_identity", None))
                if not browser_registered and getattr(session, "browser_pid", None):
                    on_browser_identity(startup_browser_identity or getattr(session, "browser_identity", None))
                if not owner_registered or (getattr(session, "browser_pid", None) and not browser_registered):
                    raise RuntimeError("database_error")
                setattr(
                    session,
                    "_handoff_owner_identity",
                    dict(startup_owner_identity or getattr(session, "owner_identity", {})),
                )
                setattr(
                    session,
                    "_handoff_browser_identity",
                    dict(startup_browser_identity or getattr(session, "browser_identity", {})),
                )
                if control is not None and await _maybe(control.cancellation_requested(run_id)):
                    raise RuntimeError("abandoned_running_attempt")
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
                continuation_route_identity: tuple[Any, ...] | None = None
                attempted_click_signature: tuple[Any, ...] | None = None
                page_scope_signature: tuple[Any, ...] | None = None
                executed_actions: list[dict[str, Any]] = []
                final_plan: AutofillPlan | None = None
                control_action_sequence = 0
                def persist_iteration_action_evidence(
                    iteration: int,
                    observation_id: str,
                    observation_result: Any,
                    planned: list[dict[str, Any]],
                    rejected: list[dict[str, Any]],
                    continuation_permit: bool | None = None,
                ) -> Any:
                    if run is None or manifest_payload is None:
                        raise RuntimeError("manifest_error")
                    relative_path = f"iterations/{iteration:04d}/action_evidence.json"
                    observation_path = f"iterations/{iteration:04d}/observation.json"
                    evidence = _action_evidence_payload(
                        iteration=iteration,
                        observation_id=observation_id,
                        observation_artifact=observation_path,
                        observation_sha256=observation_result.sha256,
                        ats_policy=adapter.name,
                        planned=planned,
                        rejected=rejected,
                        continuation_permit=continuation_permit,
                    )
                    result = _write_json_verified(run, relative_path, evidence)
                    _manifest_set_iteration(
                        manifest_payload,
                        iteration,
                        stage="action_planned",
                        artifacts={
                            "observation": _manifest_artifact(
                                observation_result,
                                observation_path,
                                iteration=iteration,
                                stage="action_planned",
                            ),
                            "action_evidence": _manifest_artifact(
                                result,
                                relative_path,
                                iteration=iteration,
                                stage="action_planned",
                            ),
                        },
                    )
                    _write_run_manifest(run, manifest_payload)
                    return result
                def persist_action_result(
                    iteration: int,
                    observation: PageObservation,
                    observation_result: Any,
                    observation_path: str,
                    iteration_action_evidence: Any,
                    action_plan: AutofillPlan,
                    action: Mapping[str, Any],
                    *,
                    succeeded: bool,
                    cancelled: bool,
                    error_code: str | None = None,
                    continuation: bool | None = None,
                ) -> dict[str, Any]:
                    nonlocal mutation_count
                    if run is None or manifest_payload is None:
                        raise RuntimeError("manifest_error")
                    is_mutation = action["action"] != "click"
                    if succeeded and is_mutation:
                        mutation_count += 1
                    action_result: dict[str, Any] = {
                        "outcome": "allowed" if succeeded else "manual",
                        "reason_code": None if succeeded else (error_code or "browser_error"),
                        "observation_sha256": observation_result.sha256,
                        "changed": bool(succeeded),
                    }
                    executed_action: dict[str, Any] = {
                        "target_id": action.get("target_id"),
                        "action": action["action"],
                        "generation": observation.observation_id,
                        "executed": bool(succeeded),
                        "result": action_result,
                        "cancelled": bool(cancelled),
                    }
                    if continuation is not None:
                        executed_action["continuation"] = bool(continuation)
                    executed_actions.append(executed_action)
                    action_path = f"iterations/{iteration:04d}/action.json"
                    result_path = f"iterations/{iteration:04d}/result.json"
                    plan_path = f"iterations/{iteration:04d}/plan.json"
                    checkpoint_path = f"iterations/{iteration:04d}/checkpoint.json"
                    iteration_action = _write_json_verified(run, action_path, executed_action)
                    iteration_result = _write_json_verified(run, result_path, action_result)
                    iteration_plan = _write_json_verified(
                        run,
                        plan_path,
                        _plan_summary(action_plan),
                    )
                    iteration_checkpoint = _write_json_verified(
                        run,
                        checkpoint_path,
                        {
                            "mutation": is_mutation,
                            "observation_id": observation.observation_id,
                            "action": action["action"],
                            "result": action_result,
                            "filled_state": {"mutation_count": mutation_count},
                        },
                    )
                    _manifest_set_iteration(
                        manifest_payload,
                        iteration,
                        stage="action_applied",
                        artifacts={
                            "action_evidence": _manifest_artifact(
                                iteration_action_evidence,
                                f"iterations/{iteration:04d}/action_evidence.json",
                                iteration=iteration,
                                stage="action_applied",
                            ),
                            "action": _manifest_artifact(
                                iteration_action,
                                action_path,
                                iteration=iteration,
                                stage="action_applied",
                            ),
                            "result": _manifest_artifact(
                                iteration_result,
                                result_path,
                                iteration=iteration,
                                stage="action_applied",
                            ),
                            "observation": _manifest_artifact(
                                observation_result,
                                observation_path,
                                iteration=iteration,
                                stage="action_applied",
                            ),
                            "plan": _manifest_artifact(
                                iteration_plan,
                                plan_path,
                                iteration=iteration,
                                stage="action_applied",
                            ),
                            "checkpoint": _manifest_artifact(
                                iteration_checkpoint,
                                checkpoint_path,
                                iteration=iteration,
                                stage="action_applied",
                            ),
                        },
                    )
                    _write_run_manifest(run, manifest_payload)
                    return action_result

                async def cancellation_after_action() -> bool:
                    if control is None:
                        return False
                    task = asyncio.create_task(
                        _maybe(control.cancellation_requested(run_id)),
                        name="application-cancellation-check",
                    )
                    try:
                        return bool(await asyncio.shield(task))
                    except BaseException:
                        await _drain_browser_call(task)
                        return True

                def mark_action_cancelled(
                    iteration: int,
                    observation: PageObservation,
                    action: Mapping[str, Any],
                    action_result: Mapping[str, Any],
                ) -> None:
                    if run is None or manifest_payload is None or not executed_actions:
                        raise RuntimeError("manifest_error")
                    executed_actions[-1]["cancelled"] = True
                    action_path = f"iterations/{iteration:04d}/action.json"
                    checkpoint_path = f"iterations/{iteration:04d}/checkpoint.json"
                    action_artifact = _write_json_verified(
                        run,
                        action_path,
                        executed_actions[-1],
                    )
                    checkpoint_artifact = _write_json_verified(
                        run,
                        checkpoint_path,
                        {
                            "mutation": action["action"] != "click",
                            "observation_id": observation.observation_id,
                            "action": action["action"],
                            "result": dict(action_result),
                            "filled_state": {"mutation_count": mutation_count},
                            "cancelled": True,
                        },
                    )
                    iteration_entry = manifest_payload.get("iterations", {}).get(str(iteration))
                    if not isinstance(iteration_entry, Mapping):
                        raise RuntimeError("manifest_error")
                    artifacts = iteration_entry.get("artifacts")
                    if not isinstance(artifacts, Mapping):
                        raise RuntimeError("manifest_error")
                    updated_artifacts = dict(artifacts)
                    stage = str(iteration_entry.get("stage", "action_applied"))
                    updated_artifacts["action"] = _manifest_artifact(
                        action_artifact,
                        action_path,
                        iteration=iteration,
                        stage=stage,
                    )
                    updated_artifacts["checkpoint"] = _manifest_artifact(
                        checkpoint_artifact,
                        checkpoint_path,
                        iteration=iteration,
                        stage=stage,
                    )
                    _manifest_set_iteration(
                        manifest_payload,
                        iteration,
                        stage=stage,
                        artifacts=updated_artifacts,
                    )
                    _write_run_manifest(run, manifest_payload)
                async def capture_screenshot(slot: str, stage: str, iteration: int) -> dict[str, Any]:
                    if run is None or manifest_payload is None:
                        raise RuntimeError("manifest_error")
                    try:
                        response = await _invoke_browser(
                            "screenshot",
                            stage,
                            iteration,
                            lambda: session.screenshot(slot, full_page=False),
                        )
                    except _BrowserFailure:
                        raise
                    try:
                        screenshot = _screenshot_payload(run, response)
                        _manifest_set_screenshot(
                            run,
                            manifest_payload,
                            slot,
                            screenshot,
                            iteration=iteration,
                            stage=stage,
                        )
                        return screenshot
                    except BrowserAdapterError as exc:
                        raise _BrowserFailure(
                            stage,
                            "screenshot",
                            normalize_browser_error_code(str(exc)),
                            iteration,
                        ) from None
                    except Exception:
                        raise _BrowserFailure(
                            stage,
                            "screenshot",
                            "artifact_error",
                            iteration,
                        ) from None
                if control is not None and await _maybe(control.cancellation_requested(run_id)):
                    raise RuntimeError("abandoned_running_attempt")

                await capture_screenshot("initial", "observation", 1)


                for iteration in range(1, MAX_AUTOFILL_ITERATIONS + 1):
                    latest_iteration = iteration
                    if control is not None and pending_proposal is None:
                        if await _maybe(control.cancellation_requested(run_id)):
                            raise RuntimeError("abandoned_running_attempt")
                    payload = await _invoke_browser(
                        "observe",
                        "observation",
                        iteration,
                        lambda: session.observe(),
                    )
                    observation = _observation_from_browser_payload(payload, iteration=iteration)
                    if control is not None:
                        if pending_proposal is not None and pending_action is not None:
                            fresh_observation_path = f"iterations/{iteration:04d}/observation.json"
                            fresh_observation_result = _write_json_verified(
                                run,
                                fresh_observation_path,
                                _observation_snapshot(observation),
                            )
                            fresh_observation_sha = _observation_snapshot_sha256(observation)
                            if not hmac.compare_digest(
                                fresh_observation_result.sha256,
                                fresh_observation_sha,
                            ):
                                raise RuntimeError("artifact_hash_mismatch")
                            if manifest_payload is None:
                                raise RuntimeError("manifest_error")
                            _manifest_set_iteration(
                                manifest_payload,
                                iteration,
                                stage="action_applied",
                                artifacts={
                                    "observation": _manifest_artifact(
                                        fresh_observation_result,
                                        fresh_observation_path,
                                        iteration=iteration,
                                        stage="action_applied",
                                    ),
                                },
                            )
                            _write_run_manifest(run, manifest_payload)
                            pending_changed = False
                            pending_reason = PublicReasonCode.safe_click_no_progress
                            if pending_action["action"] == "click":
                                pending_changed = (
                                    _observation_semantic_signature(observation)
                                    != pending_action["prior_signature"]
                                )
                            elif pending_action["action"] == "upload":
                                field = next(
                                    (
                                        item
                                        for item in observation.fields
                                        if item.target_id == pending_action["target_id"]
                                    ),
                                    None,
                                )
                                pending_changed = bool(
                                    field is not None
                                    and _field_existing_value_resolved(field)
                                    and resume.basename in field.file_basenames
                                )
                                pending_reason = PublicReasonCode.field_value_not_retained
                            else:
                                field = next(
                                    (
                                        item
                                        for item in observation.fields
                                        if item.target_id == pending_action["target_id"]
                                    ),
                                    None,
                                )
                                pending_changed = bool(
                                    field is not None
                                    and _retained_value_equal(
                                        field,
                                        pending_action["expected"],
                                    )
                                )
                                pending_reason = PublicReasonCode.field_value_not_retained
                            pending_result = {
                                "outcome": "allowed" if pending_changed else "manual",
                                "reason_code": None if pending_changed else pending_reason.value,
                                "observation_sha256": fresh_observation_sha,
                                "changed": pending_changed,
                            }
                            await _finish_control_proposal(
                                control,
                                pending_proposal,
                                pending_action_sequence,
                                ok=pending_changed,
                                state="running" if pending_changed else MANUAL_STATUS,
                                result=pending_result,
                                error_code=(
                                    None
                                    if pending_changed
                                    else _control_error_code(pending_reason.value)
                                ),
                            )
                            pending_proposal = None
                            pending_action = None
                            if not pending_changed:
                                final_plan = AutofillPlan(
                                    status=MANUAL_STATUS,
                                    reason_code=pending_reason,
                                )
                                reason = pending_reason
                                break
                            if await _maybe(control.cancellation_requested(run_id)):
                                raise RuntimeError("abandoned_running_attempt")

                        final_observation = observation
                        if observation.blockers:
                            reason = _enum_reason(observation.blockers[0].code)
                            final_plan = AutofillPlan(status=MANUAL_STATUS, reason_code=reason)
                            await capture_screenshot("blocker", "blocker", iteration)
                            await _record_control_progress(
                                control,
                                run_id,
                                "manual_intervention_required",
                                reason,
                                control_action_sequence,
                                observation_sha256=_observation_snapshot_sha256(observation),
                            )
                            break
                        if observation.errors:
                            reason = PublicReasonCode.page_validation_error
                            final_plan = AutofillPlan(status=MANUAL_STATUS, reason_code=reason)
                            break
                        sensitive_control = any(
                            _field_is_sensitive(field)
                            and field.visible
                            and field.enabled
                            and not field.readonly
                            for field in observation.fields
                        ) or any(
                            _field_is_sensitive_button(button)
                            and button.visible
                            and button.enabled
                            for button in observation.buttons
                        )
                        if sensitive_control:
                            reason = PublicReasonCode.required_sensitive_fields_manual
                            final_plan = AutofillPlan(status=MANUAL_STATUS, reason_code=reason)
                            await capture_screenshot("blocker", "blocker", iteration)
                            await _record_control_progress(
                                control,
                                run_id,
                                "manual_intervention_required",
                                reason,
                                control_action_sequence,
                                observation_sha256=_observation_snapshot_sha256(observation),
                            )
                            break
                        deterministic = _configured_and_profile_plan(
                            observation,
                            adapter=adapter,
                            context=context,
                            profile=profile,
                            resume=resume,
                            preferences=preferences,
                        )
                        obs_sha256 = _observation_snapshot_sha256(observation)
                        protected_values = (
                            _flatten_prompt_private_values(profile.facts)
                            + _flatten_prompt_private_values(resume.facts.facts)
                            + (resume.basename,)
                            + _flatten_prompt_private_values(
                                tuple(answer.value for answer in deterministic.answers)
                            )
                            + _configured_answer_values(
                                profile,
                                preferences=preferences,
                                deterministic=deterministic,
                            )
                            + tuple(run_protected_values)
                        )
                        observation_path = f"iterations/{iteration:04d}/observation.json"
                        iteration_observation = _write_json_verified(
                            run,
                            observation_path,
                            _observation_snapshot(observation),
                        )
                        if not hmac.compare_digest(iteration_observation.sha256, obs_sha256):
                            raise RuntimeError("artifact_hash_mismatch")
                        public_observation = _build_public_observation(
                            observation,
                            claimed_url=url,
                            ats_policy=adapter.name,
                            observation_sha256=obs_sha256,
                            observation_sequence=iteration,
                            protected_values=tuple(protected_values),
                        )
                        inference_request = None
                        if any(
                            _field_is_llm_eligible(field) and not _field_has_existing_value(field)
                            for field in observation.fields
                        ) or any(
                            _safe_click_is_eligible(
                                button,
                                observation.final_submit_target_ids,
                                ats_policy=adapter.name,
                                page_url=observation.url,
                            )
                            for button in observation.buttons
                        ):
                            try:
                                inference_request = _build_control_inference_request(
                                    observation,
                                    job=job,
                                    observation_sha256=obs_sha256,
                                    applicant_description=applicant_description,
                                    profile_facts=profile.facts,
                                    resume_facts=resume.facts.facts,
                                    resume_basename=resume.basename,
                                    deterministic=deterministic,
                                    ats_policy=adapter.name,
                                    configured_values=tuple(
                                        answer.value for answer in profile.field_answers
                                    ) + tuple(
                                        mapping.value for mapping in preferences.mappings
                                    ),
                                    protected_values=tuple(run_protected_values),
                                )
                            except ValueError:
                                inference_request = None
                        deterministic_summary = _deterministic_plan_summary(
                            deterministic,
                            observation=observation,
                            observation_sha256=obs_sha256,
                        )
                        await _record_control_progress(
                            control,
                            run_id,
                            "page_observed",
                            "observed",
                            control_action_sequence,
                            observation_sha256=obs_sha256,
                        )
                        proposal = await _maybe(control.propose_action(
                            run_id,
                            iteration,
                            obs_sha256,
                            public_observation,
                            inference_request,
                            deterministic_summary,
                        ))
                        if proposal is None:
                            final_plan = AutofillPlan(
                                status=MANUAL_STATUS,
                                reason_code=PublicReasonCode.no_deterministic_next_step,
                                skipped_target_ids=deterministic.skipped_target_ids,
                            )
                            reason = PublicReasonCode.no_deterministic_next_step
                            await _record_control_progress(
                                control,
                                run_id,
                                "manual_intervention_required",
                                reason,
                                control_action_sequence,
                                observation_sha256=obs_sha256,
                            )
                            break
                        control_action_sequence += 1
                        pending_proposal = proposal
                        pending_action_sequence = control_action_sequence
                        pending_action = None
                        request_id = (
                            proposal.request.request_id
                            if isinstance(proposal, BrowserToolProposal)
                            else None
                        )
                        proposal_payload = (
                            thaw_json(proposal.request.payload)
                            if isinstance(proposal, BrowserToolProposal)
                            else {}
                        )
                        proposal_operation = (
                            proposal.request.operation
                            if isinstance(proposal, BrowserToolProposal)
                            else ""
                        )
                        if proposal_operation in {"browser.fill_field", "browser.select_option"}:
                            remember_protected_values(proposal_payload.get("value"))
                        if not isinstance(proposal, BrowserToolProposal):
                            rejection_reason = "invalid_proposal"
                            persist_iteration_action_evidence(
                                iteration,
                                observation.observation_id,
                                iteration_observation,
                                [],
                                [{"action": proposal_operation, "reason": rejection_reason}],
                            )
                            await _record_control_progress(
                                control,
                                run_id,
                                "action_rejected",
                                "rejected",
                                control_action_sequence,
                                observation_sha256=obs_sha256,
                                request_id=request_id,
                            )
                            pending_proposal = None
                            final_plan = AutofillPlan(
                                status=MANUAL_STATUS,
                                reason_code=PublicReasonCode.no_deterministic_next_step,
                            )
                            reason = final_plan.reason_code
                            break
                        if proposal_operation == "browser.capture_screenshot":
                            if not hmac.compare_digest(
                                proposal_payload.get("observation_sha256", ""),
                                obs_sha256,
                            ):
                                persist_iteration_action_evidence(
                                    iteration,
                                    observation.observation_id,
                                    iteration_observation,
                                    [],
                                    [{"action": proposal_operation, "reason": "stale_observation_hash"}],
                                )
                                await _record_control_progress(
                                    control,
                                    run_id,
                                    "action_rejected",
                                    "rejected",
                                    control_action_sequence,
                                    observation_sha256=obs_sha256,
                                    request_id=request_id,
                                )
                                await _finish_control_proposal(
                                    control,
                                    proposal,
                                    control_action_sequence,
                                    ok=False,
                                    state=MANUAL_STATUS,
                                    error_code="stale_observation",
                                )
                                pending_proposal = None
                                final_plan = AutofillPlan(
                                    status=MANUAL_STATUS,
                                    reason_code=PublicReasonCode.no_deterministic_next_step,
                                )
                                reason = final_plan.reason_code
                                break
                            screenshot_action = {
                                "action": "screenshot",
                                "target_id": None,
                                "source": "control",
                            }
                            iteration_action_evidence = persist_iteration_action_evidence(
                                iteration,
                                observation.observation_id,
                                iteration_observation,
                                [screenshot_action],
                                [],
                            )
                            await _record_control_progress(
                                control,
                                run_id,
                                "action_allowed",
                                "allowed",
                                control_action_sequence,
                                observation_sha256=obs_sha256,
                                request_id=request_id,
                            )
                            dispatch_allowed = await _maybe(
                                control.before_action_dispatch(
                                    proposal,
                                    control_action_sequence,
                                )
                            )
                            if not dispatch_allowed:
                                await _record_control_progress(
                                    control,
                                    run_id,
                                    "action_rejected",
                                    "rejected",
                                    control_action_sequence,
                                    observation_sha256=obs_sha256,
                                    request_id=request_id,
                                )
                                await _finish_control_proposal(
                                    control,
                                    proposal,
                                    control_action_sequence,
                                    ok=False,
                                    state=MANUAL_STATUS,
                                    error_code="cancelled",
                                )
                                pending_proposal = None
                                final_plan = AutofillPlan(
                                    status=MANUAL_STATUS,
                                    reason_code=PublicReasonCode.abandoned_running_attempt,
                                )
                                reason = final_plan.reason_code
                                break
                            screenshot = await capture_screenshot(
                                "after-reveal",
                                "screenshot",
                                iteration,
                            )
                            await _record_control_progress(
                                control,
                                run_id,
                                "screenshot_captured",
                                "captured",
                                control_action_sequence,
                                observation_sha256=obs_sha256,
                                request_id=request_id,
                            )
                            await _finish_control_proposal(
                                control,
                                proposal,
                                control_action_sequence,
                                ok=True,
                                state="running",
                                result={
                                    "evidence_sha256": screenshot["sha256"],
                                    "observation_sha256": obs_sha256,
                                },
                            )
                            pending_proposal = None
                            final_plan = deterministic
                            reason = (
                                deterministic.reason_code
                                if deterministic.reason_code != PublicReasonCode.no_deterministic_next_step
                                else PublicReasonCode.no_deterministic_next_step
                            )
                            break
                        action_dict, rejection_reason = _validate_control_proposal(
                            proposal,
                            observation,
                            obs_sha256,
                            deterministic,
                            ats_policy=adapter.name,
                        )
                        if action_dict is None:
                            persist_iteration_action_evidence(
                                iteration,
                                observation.observation_id,
                                iteration_observation,
                                [],
                                [{
                                    "target_id": proposal_payload.get("element_id"),
                                    "action": proposal_operation,
                                    "reason": rejection_reason,
                                }],
                            )
                            await _record_control_progress(
                                control,
                                run_id,
                                "action_rejected",
                                "rejected",
                                control_action_sequence,
                                observation_sha256=obs_sha256,
                                request_id=request_id,
                            )
                            await _finish_control_proposal(
                                control,
                                proposal,
                                control_action_sequence,
                                ok=False,
                                state=MANUAL_STATUS,
                                error_code=_control_error_code(rejection_reason),
                            )
                            pending_proposal = None
                            final_plan = AutofillPlan(
                                status=MANUAL_STATUS,
                                reason_code=(
                                    PublicReasonCode.inference_privacy_violation
                                    if rejection_reason == "inference_privacy_violation"
                                    else PublicReasonCode.no_deterministic_next_step
                                ),
                            )
                            reason = final_plan.reason_code
                            break
                        if action_dict["action"] in {"fill", "select"}:
                            remember_protected_values(action_dict.get("value"))
                        action_plan = deterministic
                        if action_dict["action"] == "click":
                            action_plan = AutofillPlan(
                                safe_click_target_id=action_dict["target_id"],
                                status="ready",
                                reason_code=PublicReasonCode.draft_ready,
                            )
                        iteration_action_evidence = persist_iteration_action_evidence(
                            iteration,
                            observation.observation_id,
                            iteration_observation,
                            [action_dict],
                            [],
                        )
                        await _record_control_progress(
                            control,
                            run_id,
                            "action_allowed",
                            "allowed",
                            control_action_sequence,
                            observation_sha256=obs_sha256,
                            request_id=request_id,
                        )
                        dispatch_allowed = await _maybe(
                            control.before_action_dispatch(
                                proposal,
                                control_action_sequence,
                            )
                        )
                        if not dispatch_allowed:
                            await _record_control_progress(
                                control,
                                run_id,
                                "action_rejected",
                                "rejected",
                                control_action_sequence,
                                observation_sha256=obs_sha256,
                                request_id=request_id,
                            )
                            await _finish_control_proposal(
                                control,
                                proposal,
                                control_action_sequence,
                                ok=False,
                                state=MANUAL_STATUS,
                                error_code=(
                                    "cancelled"
                                    if await _maybe(control.cancellation_requested(run_id))
                                    else "action_rejected"
                                ),
                            )
                            pending_proposal = None
                            final_plan = AutofillPlan(
                                status=MANUAL_STATUS,
                                reason_code=(
                                    PublicReasonCode.abandoned_running_attempt
                                    if await _maybe(control.cancellation_requested(run_id))
                                    else PublicReasonCode.no_deterministic_next_step
                                ),
                            )
                            reason = final_plan.reason_code
                            break
                        if action_dict["action"] == "click":
                            browser_outcome = await _invoke_browser(
                                "click_offline",
                                "mutation",
                                iteration,
                                lambda: session.click_offline(
                                    action_dict["target_id"],
                                    continuation=False,
                                ),
                                capture_cancellation=True,
                            )
                        elif action_dict["action"] == "upload":
                            browser_outcome = await _invoke_browser(
                                "upload",
                                "mutation",
                                iteration,
                                lambda: session.upload(action_dict["target_id"]),
                                capture_cancellation=True,
                            )
                        elif action_dict["action"] == "select":
                            browser_outcome = await _invoke_browser(
                                "select",
                                "mutation",
                                iteration,
                                lambda: session.select(
                                    action_dict["target_id"],
                                    action_dict["value"],
                                ),
                                capture_cancellation=True,
                            )
                        elif action_dict["action"] == "check":
                            browser_outcome = await _invoke_browser(
                                "check",
                                "mutation",
                                iteration,
                                lambda: session.check(
                                    action_dict["target_id"],
                                    bool(action_dict["value"]),
                                ),
                                capture_cancellation=True,
                            )
                        else:
                            browser_outcome = await _invoke_browser(
                                "fill",
                                "mutation",
                                iteration,
                                lambda: session.fill(
                                    action_dict["target_id"],
                                    str(action_dict["value"]),
                                ),
                                capture_cancellation=True,
                            )
                        browser_interrupted = (
                            isinstance(browser_outcome, _BrowserCallOutcome)
                            and browser_outcome.cancelled
                        )
                        try:
                            _unwrap_browser_call_outcome(browser_outcome)
                        except _BrowserFailure as browser_error:
                            action_result = persist_action_result(
                                iteration,
                                observation,
                                iteration_observation,
                                observation_path,
                                iteration_action_evidence,
                                action_plan,
                                action_dict,
                                succeeded=False,
                                cancelled=browser_interrupted,
                                error_code=browser_error.code,
                            )
                            if browser_interrupted or await cancellation_after_action():
                                mark_action_cancelled(
                                    iteration,
                                    observation,
                                    action_dict,
                                    action_result,
                                )
                                await _finish_control_proposal(
                                    control,
                                    proposal,
                                    control_action_sequence,
                                    ok=False,
                                    state=FAILED_STATUS,
                                    error_code=browser_error.code,
                                )
                                pending_proposal = None
                                final_plan = AutofillPlan(
                                    status=FAILED_STATUS,
                                    reason_code=PublicReasonCode.abandoned_running_attempt,
                                )
                                reason = final_plan.reason_code
                                break
                            raise
                        action_result = persist_action_result(
                            iteration,
                            observation,
                            iteration_observation,
                            observation_path,
                            iteration_action_evidence,
                            action_plan,
                            action_dict,
                            succeeded=True,
                            cancelled=browser_interrupted,
                        )
                        cancelled_after_action = browser_interrupted or await cancellation_after_action()
                        if cancelled_after_action:
                            mark_action_cancelled(
                                iteration,
                                observation,
                                action_dict,
                                action_result,
                            )
                            await _finish_control_proposal(
                                control,
                                proposal,
                                control_action_sequence,
                                ok=True,
                                state=FAILED_STATUS,
                                result=action_result,
                            )
                            pending_proposal = None
                            pending_action = None
                            final_plan = AutofillPlan(
                                status=FAILED_STATUS,
                                reason_code=PublicReasonCode.abandoned_running_attempt,
                            )
                            reason = final_plan.reason_code
                            break
                        pending_action = {
                            "action": action_dict["action"],
                            "target_id": action_dict["target_id"],
                            "expected": action_dict.get("value"),
                            "prior_signature": _observation_semantic_signature(observation),
                        }
                        final_plan = action_plan
                        continue
                    final_observation = observation
                    cached_click_disappeared_on_scope_change = False
                    current_page_scope_signature = _observation_page_scope_signature(observation)
                    if (
                        page_scope_signature is not None
                        and current_page_scope_signature != page_scope_signature
                    ):
                        cached_llm = None
                        cached_inference_target_ids.clear()
                        cached_inference_button_keys.clear()
                        cached_inference.clear()
                        cached_click_disappeared_on_scope_change = (
                            cached_click_key is not None
                            and not any(
                                button.click_key == cached_click_key
                                and _safe_click_is_eligible(
                                    button,
                                    observation.final_submit_target_ids,
                                    ats_policy=adapter.name,
                                    page_url=observation.url,
                                )
                                for button in observation.buttons
                            )
                        )
                        cached_click_key = None
                    page_scope_signature = current_page_scope_signature
                    if continuation_route_identity is not None:
                        expected_route_identity = continuation_route_identity
                        observed_route_identity = _application_route_identity(observation.url, adapter.name)
                        if not _continuation_route_is_approved(expected_route_identity, observed_route_identity):
                            raise _BrowserFailure(
                                "observation",
                                "route",
                                "unsafe_navigation_target",
                                iteration,
                            )
                        if expected_route_identity[0] == "greenhouse_short":
                            continuation_route_identity = observed_route_identity
                    if attempted_mutation is not None:
                        attempted_key, attempted_kind, expected = attempted_mutation
                        retained_field = next((item for item in observation.fields if item.field_key == attempted_key and item.kind == attempted_kind), None)
                        retained = bool(retained_field is not None and (
                            (attempted_kind == "file" and _field_existing_value_resolved(retained_field) and resume.basename in retained_field.file_basenames)
                            or (attempted_kind != "file" and retained_field.value is not None and _retained_value_equal(retained_field, expected))
                        ))
                        attempted_mutation = None
                        if not retained:
                            final_plan = AutofillPlan(status="manual", reason_code=PublicReasonCode.field_value_not_retained)
                            reason = PublicReasonCode.field_value_not_retained
                            break
                    if any(_field_blocks_page_validation(field) for field in observation.fields):
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
                                page_url=observation.url,
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
                        await capture_screenshot("blocker", "blocker", iteration)
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
                        and field.value in (None, "", False, ())
                        and (field.field_key, field.kind) not in cached_inference_target_ids
                    )
                    eligible_inference_buttons = tuple(
                        button
                        for button in observation.buttons
                        if _safe_click_is_eligible(
                            button,
                            observation.final_submit_target_ids,
                            ats_policy=adapter.name,
                            page_url=observation.url,
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
                            preferences=preferences,
                            deterministic=deterministic,
                            protected_values=tuple(run_protected_values),
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
                                    page_url=observation.url,
                                )
                                else None
                            )
                    llm_reason = (
                        cached_llm.reason_code
                        if cached_llm is not None
                        else PublicReasonCode.no_deterministic_next_step
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
                                page_url=observation.url,
                            )
                        ),
                        None,
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
                    protected_sources = (
                        _flatten_prompt_private_values(profile.facts)
                        + _flatten_prompt_private_values(resume.facts.facts)
                        + (resume.basename,)
                        + _flatten_prompt_private_values(
                            tuple(answer.value for answer in deterministic.answers)
                        )
                        + _configured_answer_values(
                            profile,
                            preferences=preferences,
                            deterministic=deterministic,
                        )
                        + tuple(run_protected_values)
                    )
                    if not validate_inference_privacy(plan, protected_values=protected_sources, source_text=resume.text):
                        plan = AutofillPlan(status="manual", reason_code=PublicReasonCode.inference_privacy_violation)
                    else:
                        remember_protected_values(
                            tuple(answer.value for answer in llm.answers)
                        )
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
                    optional_inference_reasons = {
                        PublicReasonCode.no_deterministic_next_step,
                        PublicReasonCode.missing_llm_api_key,
                        PublicReasonCode.invalid_llm_response,
                        PublicReasonCode.llm_request_failed,
                        PublicReasonCode.inference_context_too_large,
                    }
                    conflict = any(item.get("reason") == "preexisting_value_conflict" for item in rejected)
                    if conflict:
                        plan = AutofillPlan(status="manual", reason_code=PublicReasonCode.preexisting_value_conflict, skipped_target_ids=plan.skipped_target_ids)
                        planned = []
                    if (
                        cached_click_disappeared_on_scope_change
                        and not planned
                        and plan.reason_code
                        in optional_inference_reasons | {PublicReasonCode.draft_ready}
                    ):
                        plan = AutofillPlan(
                            status="manual",
                            reason_code=PublicReasonCode.safe_click_no_progress,
                            skipped_target_ids=plan.skipped_target_ids,
                        )
                    continuation_permit: bool | None = None
                    click_button_for_action: ObservedButton | None = None
                    if planned and planned[0].get("action") == "click":
                        click_button_for_action = next(
                            (
                                item
                                for item in observation.buttons
                                if item.target_id == planned[0]["target_id"]
                            ),
                            None,
                        )
                        continuation_permit = (
                            _continuation_permitted(
                                click_button_for_action,
                                observation.final_submit_target_ids,
                                ats_policy=adapter.name,
                                page_url=observation.url,
                                approved_route_identity=application_route_identity,
                            )
                            if click_button_for_action is not None
                            else False
                        )
                    observation_path = f"iterations/{iteration:04d}/observation.json"
                    iteration_observation = _write_json_verified(
                        run,
                        observation_path,
                        _observation_snapshot(observation),
                    )
                    if not hmac.compare_digest(
                        iteration_observation.sha256,
                        _observation_snapshot_sha256(observation),
                    ):
                        raise RuntimeError("artifact_hash_mismatch")
                    iteration_action_evidence = persist_iteration_action_evidence(
                        iteration,
                        observation.observation_id,
                        iteration_observation,
                        planned,
                        rejected,
                        continuation_permit=continuation_permit,
                    )
                    if planned:
                        action = planned[0]
                        if action["action"] == "click":
                            cached_click_key = None
                            attempted_click_signature = current_signature
                            click_button = click_button_for_action
                            if click_button is None or not _safe_click_is_eligible(
                                click_button,
                                observation.final_submit_target_ids,
                                ats_policy=adapter.name,
                                page_url=observation.url,
                            ):
                                raise RuntimeError("safe_click_no_progress")
                            click_continuation = bool(continuation_permit)
                            if click_continuation:
                                observed_route_identity = _application_route_identity(observation.url, adapter.name)
                                navigation_candidate = _navigation_candidate_url(click_button)
                                if navigation_candidate is not None:
                                    if mutation_count != 0:
                                        raise _BrowserFailure(
                                            "mutation",
                                            "route",
                                            "unsafe_navigation_target",
                                            iteration,
                                        )
                                    candidate_route_identity = _application_route_identity(navigation_candidate, adapter.name)
                                    if (
                                        not _continuation_route_is_approved(application_route_identity, candidate_route_identity)
                                        or not _navigation_continuation_permitted(
                                            click_button,
                                            observation.final_submit_target_ids,
                                            ats_policy=adapter.name,
                                            page_url=observation.url,
                                            approved_route_identity=application_route_identity,
                                        )
                                    ):
                                        raise _BrowserFailure(
                                            "mutation",
                                            "route",
                                            "unsafe_navigation_target",
                                            iteration,
                                        )
                                    continuation_route_identity = candidate_route_identity
                                else:
                                    if not _continuation_route_is_approved(application_route_identity, observed_route_identity):
                                        raise _BrowserFailure(
                                            "mutation",
                                            "route",
                                            "unsafe_navigation_target",
                                            iteration,
                                        )
                                    continuation_route_identity = observed_route_identity
                            await _invoke_browser(
                                "click_offline",
                                "mutation",
                                iteration,
                                lambda: session.click_offline(
                                    action["target_id"],
                                    continuation=click_continuation,
                                ),
                            )
                            cached_llm = None
                            cached_inference_target_ids.clear()
                            cached_inference_button_keys.clear()
                            cached_inference.clear()
                            cached_click_key = None
                        else:
                            field = next(item for item in observation.fields if item.target_id == action["target_id"])
                            expected_value: FieldValue | None = resume.basename if action["action"] == "upload" else next(item.value for item in plan.answers if item.target_id == field.target_id)
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
                                    lambda: session.select(field.target_id, answer.value),
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
                        if action["action"] == "click":
                            executed_action["continuation"] = bool(continuation_permit)
                        executed_actions.append(executed_action)
                        iteration_action = _write_json_verified(
                            run,
                            f"iterations/{iteration:04d}/action.json",
                            executed_action,
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
                                "action_evidence": _manifest_artifact(iteration_action_evidence, f"iterations/{iteration:04d}/action_evidence.json", iteration=iteration, stage=iteration_stage),
                                "action": _manifest_artifact(iteration_action, f"iterations/{iteration:04d}/action.json", iteration=iteration, stage=iteration_stage),
                                "observation": _manifest_artifact(iteration_observation, observation_path, iteration=iteration, stage=iteration_stage),
                                "plan": _manifest_artifact(iteration_plan, f"iterations/{iteration:04d}/plan.json", iteration=iteration, stage=iteration_stage),
                                "checkpoint": _manifest_artifact(iteration_checkpoint, f"iterations/{iteration:04d}/checkpoint.json", iteration=iteration, stage=iteration_stage),
                            },
                        )
                        _write_run_manifest(run, manifest_payload)
                        final_plan = plan
                        continue
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
                        if continuation_route_identity is not None:
                            expected_route_identity = continuation_route_identity
                            stable_route_identity = _application_route_identity(stable_observation.url, adapter.name)
                            if not _continuation_route_is_approved(expected_route_identity, stable_route_identity):
                                raise _BrowserFailure(
                                    "observation",
                                    "route",
                                    "unsafe_navigation_target",
                                    iteration,
                                )
                            if expected_route_identity[0] == "greenhouse_short":
                                continuation_route_identity = stable_route_identity
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
                can_handoff = (
                    headed
                    and status in {"review_ready", "manual", "blocked"}
                    and reason not in {
                        PublicReasonCode.unsupported_ats,
                        PublicReasonCode.ats_mismatch,
                        PublicReasonCode.invalid_application_url,
                        PublicReasonCode.unsafe_network_attempt,
                    }
                    and not (
                        control is not None
                        and reason
                        in {
                            PublicReasonCode.no_deterministic_next_step,
                            PublicReasonCode.abandoned_running_attempt,
                        }
                    )
                )
                authorized_semantic_signature = _observation_semantic_signature(final_observation)
                authorized_blockers = tuple(
                    (blocker.code, blocker.frame_id, blocker.text)
                    for blocker in final_observation.blockers
                )
                authorized_final_target_ids = frozenset(final_observation.final_submit_target_ids)
                if can_handoff and control is not None:
                    if await _maybe(control.cancellation_requested(run_id)):
                        raise RuntimeError("abandoned_running_attempt")
                    obs_sha256 = _observation_snapshot_sha256(final_observation)
                    handoff_public = _build_public_observation(
                        final_observation,
                        claimed_url=url,
                        ats_policy=adapter_name,
                        observation_sha256=obs_sha256,
                        observation_sequence=final_iteration,
                        protected_values=tuple(
                            _flatten_prompt_private_values(profile.facts)
                            + _flatten_prompt_private_values(resume.facts.facts)
                            + (resume.basename,)
                            + _flatten_prompt_private_values(
                                tuple(answer.value for answer in final_plan.answers)
                            )
                            + _configured_answer_values(
                                profile,
                                preferences=preferences,
                                deterministic=final_plan,
                            )
                            + tuple(run_protected_values)
                        ),
                    )
                    handoff_proposal = await _maybe(control.authorize_handoff(
                        run_id,
                        final_iteration,
                        obs_sha256,
                        handoff_public,
                    ))
                    if handoff_proposal is None:
                        can_handoff = False
                        if reason == PublicReasonCode.draft_ready:
                            status = MANUAL_STATUS
                            reason = PublicReasonCode.no_deterministic_next_step
                    elif (
                        not isinstance(handoff_proposal, BrowserToolProposal)
                        or handoff_proposal.request.operation != "browser.prepare_human_handoff"
                        or not hmac.compare_digest(
                            thaw_json(handoff_proposal.request.payload).get(
                                "observation_sha256",
                                "",
                            ),
                            obs_sha256,
                        )
                    ):
                        if isinstance(handoff_proposal, BrowserToolProposal):
                            pending_proposal = handoff_proposal
                            pending_action_sequence = control_action_sequence + 1
                            control_action_sequence = pending_action_sequence
                            await _record_control_progress(
                                control,
                                run_id,
                                "action_rejected",
                                "rejected",
                                pending_action_sequence,
                                observation_sha256=obs_sha256,
                                request_id=handoff_proposal.request.request_id,
                            )
                            await _finish_control_proposal(
                                control,
                                handoff_proposal,
                                pending_action_sequence,
                                ok=False,
                                state=MANUAL_STATUS,
                                error_code="stale_observation",
                            )
                            pending_proposal = None
                        can_handoff = False
                        if reason == PublicReasonCode.draft_ready:
                            status = MANUAL_STATUS
                            reason = PublicReasonCode.no_deterministic_next_step
                    else:
                        pending_proposal = handoff_proposal
                        pending_action_sequence = control_action_sequence + 1
                        control_action_sequence = pending_action_sequence
                await capture_screenshot(
                    "final",
                    "handoff" if can_handoff else "final",
                    final_iteration,
                )
                if control is not None:
                    await _record_control_progress(
                        control,
                        run_id,
                        "screenshot_captured",
                        "captured",
                        final_iteration,
                        observation_sha256=_observation_snapshot_sha256(final_observation),
                    )
                if can_handoff and control is not None and pending_proposal is not None:
                    handoff_evidence = _write_json_verified(
                        run,
                        "handoff_proposal.json",
                        {
                            "operation": pending_proposal.request.operation,
                            "host_call_id": pending_proposal.host_call_id,
                            "tool_call_id": pending_proposal.tool_call_id,
                            "child_request_id": pending_proposal.request.request_id,
                            "observation_sha256": _observation_snapshot_sha256(final_observation),
                        },
                    )
                    _manifest_set_artifact(
                        manifest_payload,
                        "handoff_proposal",
                        handoff_evidence,
                        "handoff_proposal.json",
                        iteration=final_iteration,
                        stage="handoff",
                    )
                    _write_run_manifest(run, manifest_payload)
                    await _record_control_progress(
                        control,
                        run_id,
                        "action_allowed",
                        "allowed",
                        pending_action_sequence,
                        observation_sha256=_observation_snapshot_sha256(final_observation),
                        request_id=pending_proposal.request.request_id,
                    )
                    dispatch_allowed = await _maybe(
                        control.before_action_dispatch(
                            pending_proposal,
                            pending_action_sequence,
                        )
                    )
                    if not dispatch_allowed:
                        await _record_control_progress(
                            control,
                            run_id,
                            "action_rejected",
                            "rejected",
                            pending_action_sequence,
                            observation_sha256=_observation_snapshot_sha256(final_observation),
                            request_id=pending_proposal.request.request_id,
                        )
                        await _finish_control_proposal(
                            control,
                            pending_proposal,
                            pending_action_sequence,
                            ok=False,
                            state=MANUAL_STATUS,
                            error_code=(
                                "cancelled"
                                if await _maybe(control.cancellation_requested(run_id))
                                else "action_rejected"
                            ),
                        )
                        pending_proposal = None
                        can_handoff = False
                        if reason == PublicReasonCode.draft_ready:
                            status = MANUAL_STATUS
                            reason = PublicReasonCode.no_deterministic_next_step
                if can_handoff and control is not None and pending_proposal is not None:
                    if await _maybe(control.cancellation_requested(run_id)):
                        await _record_control_progress(
                            control,
                            run_id,
                            "action_rejected",
                            "rejected",
                            pending_action_sequence,
                            observation_sha256=_observation_snapshot_sha256(final_observation),
                            request_id=pending_proposal.request.request_id,
                        )
                        await _finish_control_proposal(
                            control,
                            pending_proposal,
                            pending_action_sequence,
                            ok=False,
                            state=FAILED_STATUS,
                            error_code="cancelled",
                        )
                        pending_proposal = None
                        can_handoff = False
                        final_plan = AutofillPlan(
                            status=FAILED_STATUS,
                            reason_code=PublicReasonCode.abandoned_running_attempt,
                        )
                        status = FAILED_STATUS
                        reason = PublicReasonCode.abandoned_running_attempt
                    else:
                        settled_payload = await _invoke_browser(
                            "observe",
                            "handoff",
                            final_iteration,
                            lambda: session.observe(),
                        )
                        settled_observation = _observation_from_browser_payload(
                            settled_payload,
                            iteration=final_iteration,
                        )
                        settled_blockers = tuple(
                            (blocker.code, blocker.frame_id, blocker.text)
                            for blocker in settled_observation.blockers
                        )
                        settled_final_target_ids = frozenset(
                            settled_observation.final_submit_target_ids
                        )
                        if (
                            _observation_semantic_signature(settled_observation)
                            != authorized_semantic_signature
                            or settled_blockers != authorized_blockers
                            or settled_final_target_ids != authorized_final_target_ids
                        ):
                            await _record_control_progress(
                                control,
                                run_id,
                                "action_rejected",
                                "rejected",
                                pending_action_sequence,
                                observation_sha256=_observation_snapshot_sha256(final_observation),
                                request_id=pending_proposal.request.request_id,
                            )
                            await _finish_control_proposal(
                                control,
                                pending_proposal,
                                pending_action_sequence,
                                ok=False,
                                state=MANUAL_STATUS,
                                error_code="stale_observation",
                            )
                            pending_proposal = None
                            can_handoff = False
                            final_plan = AutofillPlan(
                                status=MANUAL_STATUS,
                                reason_code=PublicReasonCode.page_not_stable,
                            )
                            status = MANUAL_STATUS
                            reason = PublicReasonCode.page_not_stable
                        elif await _maybe(control.cancellation_requested(run_id)):
                            await _record_control_progress(
                                control,
                                run_id,
                                "action_rejected",
                                "rejected",
                                pending_action_sequence,
                                observation_sha256=_observation_snapshot_sha256(final_observation),
                                request_id=pending_proposal.request.request_id,
                            )
                            await _finish_control_proposal(
                                control,
                                pending_proposal,
                                pending_action_sequence,
                                ok=False,
                                state=FAILED_STATUS,
                                error_code="cancelled",
                            )
                            pending_proposal = None
                            can_handoff = False
                            final_plan = AutofillPlan(
                                status=FAILED_STATUS,
                                reason_code=PublicReasonCode.abandoned_running_attempt,
                            )
                            status = FAILED_STATUS
                            reason = PublicReasonCode.abandoned_running_attempt
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
                    unresolved_count = len(
                        unresolved_required_fields(
                            final_observation,
                            final_plan.answers,
                        )
                    )
                    observation_sha256 = _observation_snapshot_sha256(final_observation)
                    application_finalization = {
                        "artifact_dir": artifact_ref,
                        "observation_summary": _observation_summary(final_observation),
                        "plan_summary": _plan_summary(final_plan),
                        "reason_code": reason.value,
                        "status": status,
                    }
                    proposal_result = {
                        "outcome": "committed",
                        "reason_code": reason.value,
                        "observation_sha256": observation_sha256,
                        "unresolved_required_count": unresolved_count,
                        "automated_submission": False,
                    }
                    finalization_result = None
                    manifest_result = _write_run_manifest(run, manifest_payload)
                    if control is not None and pending_proposal is not None:
                        finalization_artifact = _handoff_finalization_payload(
                            run_id=run_id,
                            job_id=int(job.get("id", 0)),
                            session_id=session_id,
                            proposal=pending_proposal,
                            status=status,
                            reason_code=reason.value,
                            observation_sha256=observation_sha256,
                            unresolved_required_count=unresolved_count,
                            commit_token_sha256=token_hash,
                        )
                        finalization_result, manifest_result = _write_handoff_finalization_artifact(
                            run,
                            manifest_payload,
                            finalization_artifact,
                            iteration=final_iteration,
                        )
                        intent = {
                            "application_finalization": application_finalization,
                            "proposal_result": proposal_result,
                            "artifact_manifest_sha256": manifest_result.sha256,
                            "artifact_sha256": finalization_result.sha256,
                            "child_request_id": pending_proposal.request.request_id,
                            "commit_token_sha256": token_hash,
                            "job_id": int(job.get("id", 0)),
                            "observation_sha256": observation_sha256,
                            "parent_request_id": pending_proposal.parent_request_id,
                            "session_id": session_id,
                        }
                        if (
                            pending_proposal.request.deadline_unix_ms
                            <= int(datetime.now(timezone.utc).timestamp() * 1000)
                        ):
                            raise RuntimeError("deadline_exceeded")
                        prepare_intent = getattr(control, "prepare_handoff_finalization", None)
                        if callable(prepare_intent):
                            handoff_intent_bound = (
                                await _maybe(
                                    prepare_intent(
                                        pending_proposal,
                                        action_sequence=pending_action_sequence,
                                        intent=intent,
                                    )
                                )
                            ) is True
                        if (
                            getattr(control, "requires_handoff_intent", False)
                            and not handoff_intent_bound
                        ):
                            raise RuntimeError("handoff_intent_unbound")
                    if control is not None and await _maybe(control.cancellation_requested(run_id)):
                        raise RuntimeError("abandoned_running_attempt")
                    if (
                        control is not None
                        and pending_proposal is not None
                        and pending_proposal.request.deadline_unix_ms
                        <= int(datetime.now(timezone.utc).timestamp() * 1000)
                    ):
                        raise RuntimeError("deadline_exceeded")
                    commit_error: BaseException | None = None
                    try:
                        await _invoke_browser(
                            "commit_handoff",
                            "handoff",
                            final_iteration,
                            lambda: session.commit_handoff(token),
                        )
                    except BaseException as exc:
                        commit_error = exc
                    if not hmac.compare_digest(_manifest_token_hash(session_manifest) or "", token_hash):
                        if commit_error is not None:
                            raise commit_error
                        raise RuntimeError("handoff_failed")
                    post_commit_guard = True
                    committed = True
                    window_state = "open_guarded"
                    if control is not None:
                        mark_handoff_committed = getattr(control, "mark_handoff_committed", None)
                        if callable(mark_handoff_committed):
                            await _maybe(mark_handoff_committed())
                    if not register_application_session(connection, run_id=run_id, session_id=session_id, session_state="open"):
                        raise RuntimeError("database_error")
                    status = "review_ready" if reason == PublicReasonCode.draft_ready else status
                    consumed_application = False
                    if control is not None and pending_proposal is not None:
                        consumed_application = await _finish_control_proposal(
                            control,
                            pending_proposal,
                            pending_action_sequence,
                            ok=True,
                            state=status,
                            result=proposal_result,
                            application_finalization=application_finalization,
                        )
                        pending_proposal = None
                    if not consumed_application:
                        finish_application_run(
                            connection,
                            run_id=run_id,
                            status=status,
                            reason_code=reason.value,
                            observation_summary=_observation_summary(final_observation),
                            plan_summary=_plan_summary(final_plan),
                            artifact_dir=artifact_ref,
                        )
                    application_finished = True
                    try:
                        release_result = await _await_browser_call(session.release_handoff)
                        if not isinstance(release_result, Mapping) or release_result.get("released") is not True:
                            raise RuntimeError("handoff_release_unconfirmed")
                        window_state = "open"
                    except Exception:
                        supervision = _supervise_postcommit_handoff_failure(session)
                        committed = supervision == "healthy"
                        # A detached adapter has already closed its transport;
                        # calling close() cannot supervise the owner/browser
                        # groups and must never be the cleanup mechanism here.
                        if supervision != "healthy" and getattr(session, "_detached", None) is not True:
                            try:
                                await _close_session(session)
                            except Exception:
                                pass
                        reconcile_postcommit = getattr(
                            control,
                            "reconcile_postcommit_handoff_failure",
                            None,
                        ) if control is not None else None
                        if not callable(reconcile_postcommit):
                            session_reconciled = True
                            window_state = (
                                "open"
                                if supervision == "healthy"
                                else ("closed" if supervision in {"partial", "absent"} else "unknown")
                            )
                        else:
                            try:
                                reconciled = await _maybe(
                                    reconcile_postcommit(
                                        run_id,
                                        session_id=session_id,
                                        artifact_root=artifacts,
                                    )
                                )
                            except Exception as reconcile_error:
                                raise RuntimeError(
                                    "postcommit_reconciliation_failed"
                                ) from reconcile_error
                            # RpcApplicationControl returns False for a healthy
                            # both-live handoff: durable reconciliation correctly
                            # leaves that ownership intact.  A partial/absent
                            # handoff must have been reaped before this call and
                            # therefore returns True after its durable downgrade.
                            reconciled_state = (
                                (
                                    reconciled.get("state")
                                    or reconciled.get("browser_state")
                                )
                                if isinstance(reconciled, Mapping)
                                else (
                                    reconciled
                                    if isinstance(reconciled, str)
                                    else None
                                )
                            )
                            healthy_result = (
                                supervision == "healthy"
                                and (
                                    reconciled is False
                                    or reconciled_state in {"healthy", "open", "open_guarded"}
                                )
                            )
                            cleaned_result = (
                                supervision in {"partial", "absent"}
                                and (
                                    reconciled is True
                                    or reconciled_state in {"closed", "failed", "manual", "terminal"}
                                )
                            )
                            if healthy_result:
                                post_commit_reconciled = True
                                window_state = "open"
                            elif cleaned_result:
                                post_commit_reconciled = True
                                status = MANUAL_STATUS
                                reason = PublicReasonCode.page_not_stable
                                session_reconciled = True
                                window_state = "closed"
                            else:
                                raise RuntimeError("postcommit_reconciliation_failed")
                else:
                    manifest_payload["stage"] = "finished"
                    manifest_payload["commit_token_sha256"] = None
                    _manifest_latest(manifest_payload, iteration=final_iteration, stage="finished")
                    _manifest_set_artifact(manifest_payload, "observation", observation_result, "observation.json", iteration=final_iteration, stage="finished")
                    _manifest_set_artifact(manifest_payload, "plan", plan_result, "plan.json", iteration=final_iteration, stage="finished")
                    _manifest_set_artifact(manifest_payload, "actions", actions_result, "actions.json", iteration=final_iteration, stage="finished")
                    _manifest_set_artifact(manifest_payload, "filled_state", filled_state_result, "filled_state.json", iteration=final_iteration, stage="finished")
                    _write_run_manifest(run, manifest_payload)
                    finish_application_run(connection, run_id=run_id, status=status, reason_code=reason.value, observation_summary=_observation_summary(final_observation), plan_summary=_plan_summary(final_plan), artifact_dir=artifact_ref)
                results.append({"job_id": int(job.get("id", 0)), "run_id": run_id, "status": status, "reason_code": reason.value, "ats": adapter_name, "artifact_ref": artifact_ref, "window_state": window_state})
            except Exception as exc:
                if post_commit_guard:
                    if not post_commit_reconciled:
                        reconcile_postcommit = getattr(
                            control,
                            "reconcile_postcommit_handoff_failure",
                            None,
                        ) if control is not None else None
                        if not callable(reconcile_postcommit):
                            if application_finished:
                                results.append({
                                    "job_id": int(job.get("id", 0)),
                                    "run_id": run_id,
                                    "status": status,
                                    "reason_code": reason.value,
                                    "ats": adapter_name,
                                    "artifact_ref": artifact_ref,
                                    "window_state": window_state,
                                })
                                continue
                            raise RuntimeError("database_error") from None
                        try:
                            reconciled = await _maybe(
                                reconcile_postcommit(
                                    run_id,
                                    session_id=session_id,
                                    artifact_root=artifacts,
                                )
                            )
                        except Exception as reconcile_error:
                            raise RuntimeError(
                                "postcommit_reconciliation_failed"
                            ) from reconcile_error
                        if reconciled is not True:
                            raise RuntimeError(
                                "postcommit_reconciliation_failed"
                            ) from exc
                        post_commit_reconciled = True
                        status = MANUAL_STATUS
                        reason = PublicReasonCode.page_not_stable
                    if application_finished:
                        results.append({
                            "job_id": int(job.get("id", 0)),
                            "run_id": run_id,
                            "status": status,
                            "reason_code": reason.value,
                            "ats": adapter_name,
                            "artifact_ref": artifact_ref,
                            "window_state": window_state,
                        })
                        continue
                    raise RuntimeError("database_error") from None
                browser_failure = exc if isinstance(exc, _BrowserFailure) else None
                if browser_failure is not None:
                    failure_code = browser_failure.code
                    continuation_safety_codes = (
                        PublicReasonCode.unsafe_navigation_target.value,
                        PublicReasonCode.unsafe_network_attempt.value,
                    )
                    reason = (
                        PublicReasonCode(failure_code)
                        if failure_code in continuation_safety_codes
                        else PublicReasonCode.browser_error
                    )
                    status = _status_for_reason(reason)
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
                                "screenshots": {},
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
                    if session is not None and not committed and not post_commit_guard and not session_reconciled:
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
                        and not post_commit_guard
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
