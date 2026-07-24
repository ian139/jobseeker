"""Pure contracts for the guarded application coordinator and OMP host tools.

This module deliberately does not own a transport.  ``omp --mode rpc`` owns the
JSONL protocol and invokes the host tools described by
``BROWSER_HOST_TOOL_DEFINITIONS``.  The application envelope is the small,
pure request identity used by the local coordinator behind those tools.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import time

from dataclasses import dataclass
from datetime import datetime, timezone
from types import MappingProxyType
from typing import Mapping, Sequence
from uuid import UUID, uuid5

from .contracts import JsonValue, PublicReasonCode, freeze_json, thaw_json

APPLICATION_RPC_PROTOCOL_VERSION = 1
MAX_APPLICATION_JSON_BYTES = 512 * 1024
MAX_APPLICATION_JSON_DEPTH = 12
MAX_APPLICATION_STRING_CHARS = 12_000
MAX_APPLICATION_URL_CHARS = 2_000
MAX_APPLICATION_REASON_CHARS = 2_000
MAX_APPLICATION_ID_CHARS = 128
MAX_APPLICATION_ARRAY_ITEMS = 256
MAX_APPLICATION_OBJECT_ITEMS = 256
MAX_APPLICATION_DEADLINE_WINDOW_MS = 300_000
MAX_APPLICATION_RUN_ID = 2**63 - 1

APPLICATION_OPERATIONS = (
    "run.start",
    "run.status",
    "run.resume",
    "run.cancel",
    "browser.observe",
    "browser.fill_field",
    "browser.select_option",
    "browser.set_checkbox",
    "browser.upload_configured_resume",
    "browser.activate_safe_control",
    "browser.capture_screenshot",
    "browser.prepare_human_handoff",
)
LIFECYCLE_OPERATIONS = (
    "run.start",
    "run.status",
    "run.resume",
    "run.cancel",
)
BROWSER_OPERATIONS = (
    "browser.observe",
    "browser.fill_field",
    "browser.select_option",
    "browser.set_checkbox",
    "browser.upload_configured_resume",
    "browser.activate_safe_control",
    "browser.capture_screenshot",
    "browser.prepare_human_handoff",
)
_APPLICATION_OPERATION_SET = frozenset(APPLICATION_OPERATIONS)
_LIFECYCLE_OPERATION_SET = frozenset(LIFECYCLE_OPERATIONS)
_BROWSER_OPERATION_SET = frozenset(BROWSER_OPERATIONS)

RUN_STATES = (
    "starting",
    "running",
    "manual",
    "blocked",
    "review_ready",
    "failed",
)
_RUN_STATE_SET = frozenset(RUN_STATES)

PUBLIC_ERROR_MESSAGES = MappingProxyType(
    {
        "invalid_request": "Request rejected",
        "unsupported_operation": "Operation is not supported",
        "protocol_mismatch": "Protocol version is not supported",
        "deadline_exceeded": "Request deadline exceeded",
        "request_conflict": "Request identifier conflicts with prior intent",
        "request_incomplete": "Prior request outcome is incomplete",
        "run_not_found": "Application run was not found",
        "run_not_owned": "Application run is not owned by this coordinator",
        "run_not_active": "Application run is not active",
        "stale_observation": "Observation is stale",
        "action_rejected": "Action was rejected by safety policy",
        "manual_intervention_required": "Human intervention is required",
        "cancelled": "Application run was cancelled",
        "workflow_failed": "Application workflow failed",
        "unavailable": "Application coordinator is unavailable",
        "internal_error": "Internal application error",
    }
)
PUBLIC_ERROR_CODES = frozenset(PUBLIC_ERROR_MESSAGES)
PUBLIC_REASON_CODES = frozenset(code.value for code in PublicReasonCode)
_ATS_NAMES = frozenset({"greenhouse", "lever"})
_LIFECYCLE_COORDINATOR_STATES = frozenset(
    {"starting", "prompting", "awaiting_resume", "executing", "cancelling", "terminal"}
)
_BROWSER_STATES = frozenset({"not_started", "starting", "owned", "idle", "handed_off", "closed", "failed"})
_PAGE_TYPES = frozenset({"application", "confirmation", "authentication", "captcha", "assessment", "unknown"})
_ACTION_OUTCOMES = frozenset({"allowed", "rejected", "manual"})
_ACTION_TYPES = frozenset({"continue", "navigation", "final_submit", "authentication", "external", "unknown"})
_SAFETY_CLASSES = frozenset({"safe", "manual", "sensitive", "ambiguous"})
_BLOCKER_CODES = frozenset(
    {
        "captcha",
        "authentication_required",
        "assessment_required",
        "unsupported_frame",
        "page_validation_error",
        "observation_too_large",
    }
)
_PUBLIC_TEXT_REDACTIONS = re.compile(
    r"(?:\b(?:password|passcode|secret|api[_ -]?key|cookie|token|authorization)\s*[:=]\s*\S+"
    r"|\bbearer\s+[A-Za-z0-9._~+/=-]{8,})",
    re.IGNORECASE,
)
_EMAIL_RE = re.compile(r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b")
_RFC3339_UTC_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z$"
)

_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)
_SLUG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.~:-]{0,127}$")
_TRANSPORT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")

_METADATA_KEYS = ("protocol_version", "run_id", "request_id", "deadline_unix_ms")
_ENVELOPE_KEYS = (
    "protocol_version",
    "request_id",
    "operation",
    "deadline_unix_ms",
    "run_id",
    "payload",
)
_RESPONSE_KEYS = (
    "protocol_version",
    "request_id",
    "operation",
    "ok",
    "run_id",
    "state",
    "action_sequence",
    "event_sequence",
    "result",
    "error",
)
_NATIVE_HOST_CALL_KEYS = ("type", "id", "toolCallId", "toolName", "arguments")


class ApplicationRpcError(ValueError):
    """A deterministic, public-safe validation error."""

    def __init__(self, code: str = "invalid_request") -> None:
        if code not in PUBLIC_ERROR_CODES:
            code = "invalid_request"
        self.code = code
        super().__init__(PUBLIC_ERROR_MESSAGES[code])


def _reject(code: str = "invalid_request") -> None:
    raise ApplicationRpcError(code)


def _decode_json_object(value: object) -> Mapping[str, object]:
    if isinstance(value, (str, bytes, bytearray)):
        if isinstance(value, str):
            raw_bytes = value.encode("utf-8")
        else:
            raw_bytes = bytes(value)
        if len(raw_bytes) > MAX_APPLICATION_JSON_BYTES:
            _reject()
        try:
            decoded = json.loads(raw_bytes)
        except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError):
            _reject()
        value = decoded
    if not isinstance(value, Mapping):
        _reject()
    if any(type(key) is not str for key in value):
        _reject()
    _validate_json_value(value)
    _canonical_json(value)
    return value


def _validate_json_value(value: object, *, depth: int = 0) -> None:
    if depth > MAX_APPLICATION_JSON_DEPTH:
        _reject()
    if value is None or type(value) is bool:
        return
    if type(value) is int:
        return
    if type(value) is float:
        if not math.isfinite(value):
            _reject()
        return
    if type(value) is str:
        if len(value) > MAX_APPLICATION_STRING_CHARS:
            _reject()
        if any(ord(char) < 0x20 and char not in "\t\n\r" for char in value):
            _reject()
        return
    if isinstance(value, Mapping):
        if len(value) > MAX_APPLICATION_OBJECT_ITEMS:
            _reject()
        for key, item in value.items():
            if type(key) is not str:
                _reject()
            _validate_json_value(item, depth=depth + 1)
        return
    if isinstance(value, (list, tuple)):
        if len(value) > MAX_APPLICATION_ARRAY_ITEMS:
            _reject()
        for item in value:
            _validate_json_value(item, depth=depth + 1)
        return
    _reject()


def _jsonable(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_jsonable(item) for item in value]
    if isinstance(value, list):
        return [_jsonable(item) for item in value]
    return value


def _canonical_json(value: object) -> str:
    _validate_json_value(value)
    try:
        encoded = json.dumps(
            _jsonable(value),
            ensure_ascii=True,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError, OverflowError):
        _reject()
    if len(encoded.encode("utf-8")) > MAX_APPLICATION_JSON_BYTES:
        _reject()
    return encoded


def _expect_exact_keys(value: Mapping[str, object], expected: Sequence[str]) -> None:
    if set(value) != set(expected):
        _reject()


def _expect_string(value: object, *, maximum: int = MAX_APPLICATION_STRING_CHARS, allow_empty: bool = False) -> str:
    if type(value) is not str or len(value) > maximum or (not allow_empty and not value):
        _reject()
    if any(ord(char) < 0x20 and char not in "\t\n\r" for char in value):
        _reject()
    return value


def _expect_safe_slug(value: object, *, allow_empty: bool = False) -> str:
    value = _expect_string(value, maximum=MAX_APPLICATION_ID_CHARS, allow_empty=allow_empty)
    if not allow_empty and not _SLUG_RE.fullmatch(value):
        _reject()
    if allow_empty and value and not _SLUG_RE.fullmatch(value):
        _reject()
    if ".." in value or "/" in value or "\\" in value or value.startswith("~"):
        _reject()
    return value


def _expect_transport_id(value: object) -> str:
    value = _expect_string(value, maximum=MAX_APPLICATION_ID_CHARS)
    if not _TRANSPORT_ID_RE.fullmatch(value) or ".." in value:
        _reject()
    return value


def _expect_uuid(value: object) -> str:
    value = _expect_string(value, maximum=36)
    if _UUID_RE.fullmatch(value) is None:
        _reject()
    try:
        if str(UUID(value)) != value:
            _reject()
    except (ValueError, AttributeError):
        _reject()
    return value


def _expect_hash(value: object) -> str:
    value = _expect_string(value, maximum=64)
    if _HASH_RE.fullmatch(value) is None:
        _reject()
    return value


def _expect_positive_run_id(value: object) -> int:
    if type(value) is not int or value <= 0 or value > MAX_APPLICATION_RUN_ID:
        _reject()
    return value


def _expect_deadline(value: object, *, now_unix_ms: int) -> int:
    if type(now_unix_ms) is not int or now_unix_ms < 0:
        _reject()
    if type(value) is not int or value <= 0 or value < now_unix_ms or value > now_unix_ms + MAX_APPLICATION_DEADLINE_WINDOW_MS:
        _reject("deadline_exceeded" if type(value) is int and value < now_unix_ms else "invalid_request")
    return value

def _expect_job_url(value: object) -> str:
    return _expect_direct_ats_url(value)


def _expect_reason(value: object) -> str:
    return _expect_string(value, maximum=MAX_APPLICATION_REASON_CHARS)


def _parse_action_triplet(payload: Mapping[str, object], operation: str) -> dict[str, object]:
    if operation == "browser.fill_field":
        value = payload["value"]
        if value is not None and type(value) is not str:
            _reject()
    elif operation == "browser.select_option":
        value = payload["value"]
        if value is not None and type(value) not in (str, list, tuple):
            _reject()
        if isinstance(value, (list, tuple)):
            if len(value) > MAX_APPLICATION_ARRAY_ITEMS or any(type(item) is not str for item in value):
                _reject()
            if any(len(item) > MAX_APPLICATION_STRING_CHARS for item in value):
                _reject()
            value = tuple(value)
    else:
        value = payload["value"]
        if value is not None and type(value) is not bool:
            _reject()

    confidence = payload["confidence"]
    reason = payload["reason"]
    if value is None and confidence is None and reason is None:
        return {"value": None, "confidence": None, "reason": None}
    if value is None or confidence is None or reason is None:
        _reject()
    confidence = _expect_confidence(confidence)
    reason = _expect_reason(reason)
    return {"value": value, "confidence": confidence, "reason": reason}


def _parse_payload(operation: str, payload: object) -> dict[str, object]:
    if not isinstance(payload, Mapping) or any(type(key) is not str for key in payload):
        _reject()
    _validate_json_value(payload)
    if operation == "run.start":
        expected = ("goal", "job_url", "candidate_profile_id", "configured_resume_id", "headed")
        _expect_exact_keys(payload, expected)
        if payload["goal"] != "prepare_application_draft":
            _reject()
        job_url = _expect_job_url(payload["job_url"])
        profile_id = _expect_safe_slug(payload["candidate_profile_id"])
        resume_id = _expect_safe_slug(payload["configured_resume_id"])
        if payload["headed"] is not True:
            _reject()
        return {
            "goal": "prepare_application_draft",
            "job_url": job_url,
            "candidate_profile_id": profile_id,
            "configured_resume_id": resume_id,
            "headed": payload["headed"],
        }
    if operation in {"run.status", "run.resume", "run.cancel", "browser.observe"}:
        if len(payload) != 0:
            _reject()
        return {}
    if operation in {"browser.fill_field", "browser.select_option", "browser.set_checkbox"}:
        expected = ("observation_sha256", "element_id", "value", "confidence", "reason")
        _expect_exact_keys(payload, expected)
        output = {
            "observation_sha256": _expect_hash(payload["observation_sha256"]),
            "element_id": _expect_safe_slug(payload["element_id"]),
        }
        output.update(_parse_action_triplet(payload, operation))
        return output
    if operation in {"browser.upload_configured_resume", "browser.activate_safe_control"}:
        expected = ("observation_sha256", "element_id")
        _expect_exact_keys(payload, expected)
        return {
            "observation_sha256": _expect_hash(payload["observation_sha256"]),
            "element_id": _expect_safe_slug(payload["element_id"]),
        }
    if operation in {"browser.capture_screenshot", "browser.prepare_human_handoff"}:
        expected = ("observation_sha256",)
        _expect_exact_keys(payload, expected)
        return {"observation_sha256": _expect_hash(payload["observation_sha256"])}
    _reject("unsupported_operation")


@dataclass(frozen=True, slots=True)
class ApplicationRpcRequest:
    protocol_version: int
    request_id: str
    operation: str
    deadline_unix_ms: int
    run_id: int | None
    payload: Mapping[str, JsonValue]

    def __post_init__(self) -> None:
        if type(self.protocol_version) is not int or self.protocol_version != APPLICATION_RPC_PROTOCOL_VERSION:
            _reject("protocol_mismatch")
        _expect_uuid(self.request_id)
        if type(self.operation) is not str or self.operation not in _APPLICATION_OPERATION_SET:
            _reject("unsupported_operation")
        if type(self.deadline_unix_ms) is not int or self.deadline_unix_ms <= 0:
            _reject()
        if self.operation == "run.start":
            if self.run_id is not None:
                _reject()
        else:
            _expect_positive_run_id(self.run_id)
        parsed_payload = _parse_payload(self.operation, self.payload)
        object.__setattr__(self, "payload", freeze_json(parsed_payload))

    @property
    def semantic_sha256(self) -> str:
        return semantic_request_sha256(self)

    @property
    def semantic_hash(self) -> str:
        return self.semantic_sha256

    def to_mapping(self) -> dict[str, object]:
        return {
            "protocol_version": self.protocol_version,
            "request_id": self.request_id,
            "operation": self.operation,
            "deadline_unix_ms": self.deadline_unix_ms,
            "run_id": self.run_id,
            "payload": thaw_json(self.payload),
        }

    as_dict = to_mapping

    def __hash__(self) -> int:
        return hash(self.semantic_sha256)
@dataclass(frozen=True, slots=True)
class HostToolContext:
    protocol_version: int
    run_id: int
    request_id: str
    deadline_unix_ms: int

    def __post_init__(self) -> None:
        if type(self.protocol_version) is not int or self.protocol_version != APPLICATION_RPC_PROTOCOL_VERSION:
            _reject("protocol_mismatch")
        _expect_positive_run_id(self.run_id)
        _expect_uuid(self.request_id)
        if type(self.deadline_unix_ms) is not int or self.deadline_unix_ms <= 0:
            _reject()

    def to_mapping(self) -> dict[str, object]:
        return {
            "protocol_version": self.protocol_version,
            "run_id": self.run_id,
            "request_id": self.request_id,
            "deadline_unix_ms": self.deadline_unix_ms,
        }




@dataclass(frozen=True, slots=True)
class BrowserToolProposal:
    host_call_id: str
    tool_call_id: str
    tool_name: str
    request: ApplicationRpcRequest
    parent_request_id: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "host_call_id", _expect_transport_id(self.host_call_id))
        object.__setattr__(self, "tool_call_id", _expect_transport_id(self.tool_call_id))
        if type(self.tool_name) is not str or self.tool_name not in _BROWSER_OPERATION_SET:
            _reject("unsupported_operation")
        if not isinstance(self.request, ApplicationRpcRequest) or self.request.operation != self.tool_name:
            _reject()
        object.__setattr__(self, "parent_request_id", _expect_uuid(self.parent_request_id))
        if str(uuid5(UUID(self.parent_request_id), f"{self.host_call_id}\0{self.tool_call_id}\0{self.tool_name}")) != self.request.request_id:
            _reject()

    @property
    def id(self) -> str:
        return self.host_call_id

    @property
    def host_id(self) -> str:
        return self.host_call_id

    @property
    def toolCallId(self) -> str:
        return self.tool_call_id

    @property
    def toolName(self) -> str:
        return self.tool_name

    @property
    def arguments(self) -> Mapping[str, object]:
        return thaw_json(self.request.payload)

    @property
    def semantic_sha256(self) -> str:
        return self.request.semantic_sha256

    @property
    def semantic_hash(self) -> str:
        return self.semantic_sha256



def parse_application_request(value: object, *, now_unix_ms: int | None = None) -> ApplicationRpcRequest:
    raw = _decode_json_object(value)
    _expect_exact_keys(raw, _ENVELOPE_KEYS)
    if now_unix_ms is None:
        now_unix_ms = int(time.time() * 1000)
    protocol_version = raw["protocol_version"]
    if type(protocol_version) is not int or protocol_version != APPLICATION_RPC_PROTOCOL_VERSION:
        _reject("protocol_mismatch")
    request_id = _expect_uuid(raw["request_id"])
    operation = raw["operation"]
    if type(operation) is not str or operation not in _APPLICATION_OPERATION_SET:
        _reject("unsupported_operation")
    deadline = _expect_deadline(raw["deadline_unix_ms"], now_unix_ms=now_unix_ms)
    run_id = raw["run_id"]
    if operation == "run.start":
        if run_id is not None:
            _reject()
    else:
        run_id = _expect_positive_run_id(run_id)
    payload = _parse_payload(operation, raw["payload"])
    # The complete parsed object is bounded separately from each value cap.
    _canonical_json(
        {
            "protocol_version": protocol_version,
            "request_id": request_id,
            "operation": operation,
            "deadline_unix_ms": deadline,
            "run_id": run_id,
            "payload": payload,
        }
    )
    return ApplicationRpcRequest(protocol_version, request_id, operation, deadline, run_id, payload)


def semantic_request_sha256(request: ApplicationRpcRequest | Mapping[str, object]) -> str:
    """Hash request intent while excluding only deadline liveness metadata.

    A retried request may receive a fresh deadline without becoming a new
    intent. Protocol version, request identity, operation, run identity, and
    every payload value remain part of the hash.
    """
    if not isinstance(request, ApplicationRpcRequest):
        raw = dict(request)
        _expect_exact_keys(raw, _ENVELOPE_KEYS)
        canonical_request = {
            "protocol_version": raw["protocol_version"],
            "request_id": raw["request_id"],
            "operation": raw["operation"],
            "run_id": raw["run_id"],
            "payload": raw["payload"],
        }
    else:
        canonical_request = {
            "protocol_version": request.protocol_version,
            "request_id": request.request_id,
            "operation": request.operation,
            "run_id": request.run_id,
            "payload": request.payload,
        }
    encoded = _canonical_json(canonical_request).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()

def _metadata_schema(*, start: bool) -> dict[str, object]:
    return {
        "protocol_version": {"type": "integer", "const": APPLICATION_RPC_PROTOCOL_VERSION},
        "run_id": {"type": "null" if start else "integer", **({} if start else {"minimum": 1})},
        "request_id": {"type": "string", "pattern": _UUID_RE.pattern, "maxLength": 36},
        "deadline_unix_ms": {"type": "integer", "minimum": 1},
    }


def _action_value_schema(operation: str) -> dict[str, object]:
    if operation == "browser.fill_field":
        return {"type": ["string", "null"], "maxLength": MAX_APPLICATION_STRING_CHARS}
    if operation == "browser.select_option":
        return {
            "oneOf": [
                {"type": "string", "maxLength": MAX_APPLICATION_STRING_CHARS},
                {
                    "type": "array",
                    "items": {"type": "string", "maxLength": MAX_APPLICATION_STRING_CHARS},
                    "maxItems": MAX_APPLICATION_ARRAY_ITEMS,
                },
                {"type": "null"},
            ]
        }
    return {"type": ["boolean", "null"]}


def _tool_parameters_schema(operation: str, *, include_metadata: bool = True) -> dict[str, object]:
    start = operation == "run.start"
    properties = _metadata_schema(start=start) if include_metadata else {}
    required = list(_METADATA_KEYS) if include_metadata else []
    if start:
        properties.update(
            {
                "goal": {"const": "prepare_application_draft", "type": "string"},
                "job_url": {"type": "string", "maxLength": MAX_APPLICATION_URL_CHARS, "format": "uri"},
                "candidate_profile_id": {"type": "string", "pattern": _SLUG_RE.pattern, "maxLength": MAX_APPLICATION_ID_CHARS},
                "configured_resume_id": {"type": "string", "pattern": _SLUG_RE.pattern, "maxLength": MAX_APPLICATION_ID_CHARS},
                "headed": {"const": True, "type": "boolean"},
            }
        )
        required.extend(("goal", "job_url", "candidate_profile_id", "configured_resume_id", "headed"))
    elif operation in {"browser.fill_field", "browser.select_option", "browser.set_checkbox"}:
        properties.update(
            {
                "observation_sha256": {"type": "string", "pattern": _HASH_RE.pattern, "maxLength": 64},
                "element_id": {"type": "string", "pattern": _SLUG_RE.pattern, "maxLength": MAX_APPLICATION_ID_CHARS},
                "value": _action_value_schema(operation),
                "confidence": {"type": ["number", "null"], "minimum": 0.7, "maximum": 1.0},
                "reason": {"type": ["string", "null"], "maxLength": MAX_APPLICATION_REASON_CHARS},
            }
        )
        required.extend(("observation_sha256", "element_id", "value", "confidence", "reason"))
    elif operation in {"browser.upload_configured_resume", "browser.activate_safe_control"}:
        properties.update(
            {
                "observation_sha256": {"type": "string", "pattern": _HASH_RE.pattern, "maxLength": 64},
                "element_id": {"type": "string", "pattern": _SLUG_RE.pattern, "maxLength": MAX_APPLICATION_ID_CHARS},
            }
        )
        required.extend(("observation_sha256", "element_id"))
    elif operation in {"browser.capture_screenshot", "browser.prepare_human_handoff"}:
        properties["observation_sha256"] = {"type": "string", "pattern": _HASH_RE.pattern, "maxLength": 64}
        required.append("observation_sha256")
    elif operation not in {"run.status", "run.resume", "run.cancel", "browser.observe"}:
        _reject("unsupported_operation")
    schema: dict[str, object] = {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }
    if operation in {"browser.fill_field", "browser.select_option", "browser.set_checkbox"}:
        deterministic = {
            "type": "object",
            "properties": {
                "value": {"type": "null"},
                "confidence": {"type": "null"},
                "reason": {"type": "null"},
            },
            "required": ["value", "confidence", "reason"],
        }
        inferred = {
            "type": "object",
            "properties": {
                "value": {"not": {"type": "null"}},
                "confidence": {"type": "number", "minimum": 0.7, "maximum": 1.0},
                "reason": {"type": "string", "minLength": 1, "maxLength": MAX_APPLICATION_REASON_CHARS},
            },
            "required": ["value", "confidence", "reason"],
        }
        schema["oneOf"] = [deterministic, inferred]
    return schema


def _tool_label(operation: str) -> str:
    return operation.replace(".", " ").replace("_", " ").title()


def _build_tool_definitions(operations: Sequence[str]) -> tuple[dict[str, object], ...]:
    return tuple(
        {
            "name": operation,
            "label": _tool_label(operation),
            "description": "Safe application coordinator control; never submits an application.",
            "parameters": _tool_parameters_schema(operation, include_metadata=False),
        }
        for operation in operations
    )


BROWSER_HOST_TOOL_DEFINITIONS = _build_tool_definitions(BROWSER_OPERATIONS)


def build_set_host_tools_command(*, request_id: str | None = None) -> dict[str, object]:
    command: dict[str, object] = {"type": "set_host_tools", "tools": _jsonable(BROWSER_HOST_TOOL_DEFINITIONS)}
    if request_id is not None:
        command["id"] = _expect_transport_id(request_id)
    return command


def _parse_host_context(
    context: Mapping[str, object] | HostToolContext | None,
    *,
    protocol_version: object | None,
    run_id: object | None,
    request_id: object | None,
    deadline_unix_ms: object | None,
    now_unix_ms: int | None,
) -> dict[str, object]:
    if isinstance(context, HostToolContext):
        context = context.to_mapping()
    supplied_kwargs = (protocol_version, run_id, request_id, deadline_unix_ms)
    if context is not None and any(item is not None for item in supplied_kwargs):
        _reject()
    if context is None:
        if any(item is None for item in supplied_kwargs):
            _reject()
        context = {
            "protocol_version": protocol_version,
            "run_id": run_id,
            "request_id": request_id,
            "deadline_unix_ms": deadline_unix_ms,
        }
    if not isinstance(context, Mapping) or any(type(key) is not str for key in context):
        _reject()
    _expect_exact_keys(context, _METADATA_KEYS)
    protocol = context["protocol_version"]
    if type(protocol) is not int or protocol != APPLICATION_RPC_PROTOCOL_VERSION:
        _reject("protocol_mismatch")
    context_request_id = _expect_uuid(context["request_id"])
    context_run_id = _expect_positive_run_id(context["run_id"])
    if now_unix_ms is None:
        now_unix_ms = int(time.time() * 1000)
    context_deadline = _expect_deadline(context["deadline_unix_ms"], now_unix_ms=now_unix_ms)
    return {
        "protocol_version": protocol,
        "run_id": context_run_id,
        "request_id": context_request_id,
        "deadline_unix_ms": context_deadline,
    }


def parse_host_tool_call(
    value: object,
    context: Mapping[str, object] | HostToolContext | None = None,
    *,
    now_unix_ms: int | None = None,
    host_context: Mapping[str, object] | HostToolContext | None = None,
    protocol_version: object | None = None,
    run_id: object | None = None,
    request_id: object | None = None,
    deadline_unix_ms: object | None = None,
) -> BrowserToolProposal:
    if context is None:
        context = host_context
    elif host_context is not None:
        _reject()
    raw = _decode_json_object(value)
    _expect_exact_keys(raw, _NATIVE_HOST_CALL_KEYS)
    if raw["type"] != "host_tool_call":
        _reject()
    host_call_id = _expect_transport_id(raw["id"])
    tool_call_id = _expect_transport_id(raw["toolCallId"])
    tool_name = raw["toolName"]
    if type(tool_name) is not str or tool_name not in _BROWSER_OPERATION_SET:
        _reject("unsupported_operation")
    bound = _parse_host_context(
        context,
        protocol_version=protocol_version,
        run_id=run_id,
        request_id=request_id,
        deadline_unix_ms=deadline_unix_ms,
        now_unix_ms=now_unix_ms,
    )
    arguments = raw["arguments"]
    if not isinstance(arguments, Mapping) or any(type(key) is not str for key in arguments):
        _reject()
    payload_keys = _parse_payload_keys(tool_name)
    if set(arguments) != set(payload_keys):
        _reject()
    payload = {key: arguments[key] for key in payload_keys}
    child_request_id = str(
        uuid5(
            UUID(bound["request_id"]),
            f"{host_call_id}\0{tool_call_id}\0{tool_name}",
        )
    )
    envelope = {
        "protocol_version": bound["protocol_version"],
        "request_id": child_request_id,
        "operation": tool_name,
        "deadline_unix_ms": bound["deadline_unix_ms"],
        "run_id": bound["run_id"],
        "payload": payload,
    }
    request = parse_application_request(envelope, now_unix_ms=now_unix_ms)
    return BrowserToolProposal(host_call_id, tool_call_id, tool_name, request, bound["request_id"])


def _parse_payload_keys(operation: str) -> tuple[str, ...]:
    if operation == "run.start":
        return ("goal", "job_url", "candidate_profile_id", "configured_resume_id", "headed")
    if operation in {"run.status", "run.resume", "run.cancel", "browser.observe"}:
        return ()
    if operation in {"browser.fill_field", "browser.select_option", "browser.set_checkbox"}:
        return ("observation_sha256", "element_id", "value", "confidence", "reason")
    if operation in {"browser.upload_configured_resume", "browser.activate_safe_control"}:
        return ("observation_sha256", "element_id")
    if operation in {"browser.capture_screenshot", "browser.prepare_human_handoff"}:
        return ("observation_sha256",)
    _reject("unsupported_operation")


def _is_forbidden_path_string(value: str) -> bool:
    if value.startswith(("/", "\\", "~/", "~\\")):
        return True
    if re.match(r"^[A-Za-z]:[\\/]", value) is not None:
        return True
    if "\\\\" in value:
        return True
    normalized = value.replace("\\", "/")
    if normalized == ".." or normalized.startswith("../") or "/../" in normalized or normalized.endswith("/.."):
        return True
    if normalized.startswith("file:"):
        return True
    return False


def _contains_submitted_action_value(value: object, submitted: tuple[object, ...]) -> bool:
    if isinstance(value, Mapping):
        return any(_contains_submitted_action_value(item, submitted) for item in value.values())
    if isinstance(value, (list, tuple)):
        return any(_contains_submitted_action_value(item, submitted) for item in value)
    # Strings are the dangerous echo channel; matching boolean values would
    # make ordinary ``ok``/status projections impossible.
    return type(value) is str and value in submitted


def _expect_public_text(value: object, *, allow_empty: bool = True) -> str:
    text = _expect_string(value, maximum=MAX_APPLICATION_REASON_CHARS, allow_empty=allow_empty)
    if any(ord(char) < 0x20 for char in text):
        _reject()
    if _EMAIL_RE.search(text) or _PUBLIC_TEXT_REDACTIONS.search(text) or _is_forbidden_path_string(text):
        _reject()
    return text


def _expect_public_code(value: object, allowed: frozenset[str]) -> str:
    if type(value) is not str or value not in allowed:
        _reject()
    return value


def _expect_confidence(value: object) -> float | int | None:
    if value is None:
        return None
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0.7 or value > 1.0:
        _reject()
    return value


def _expect_reason_code(value: object, *, nullable: bool = True) -> str | None:
    if value is None and nullable:
        return None
    if type(value) is not str or value not in PUBLIC_REASON_CODES:
        _reject()
    return value


def _expect_hash_or_none(value: object) -> str | None:
    if value is None:
        return None
    return _expect_hash(value)


def _expect_nonnegative(value: object) -> int:
    if type(value) is not int or value < 0 or value > MAX_APPLICATION_RUN_ID:
        _reject()
    return value


def _expect_rfc3339_utc(value: object) -> str:
    text = _expect_string(value, maximum=64)
    if _RFC3339_UTC_RE.fullmatch(text) is None:
        _reject()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        _reject()
    if parsed.tzinfo != timezone.utc:
        _reject()
    return text


def _expect_direct_ats_url(value: object, ats: str | None = None) -> str:
    text = _expect_string(value, maximum=MAX_APPLICATION_URL_CHARS)
    if ats is not None and ats not in _ATS_NAMES:
        _reject()
    try:
        from .browser_adapter import BrowserAdapterError, validate_ats_url
        policies = (ats,) if ats is not None else tuple(_ATS_NAMES)
        for policy in policies:
            try:
                route = validate_ats_url(text, policy)
                if route.url == text:
                    return text
            except (BrowserAdapterError, ValueError, UnicodeError):
                continue
    except (ImportError, TypeError, ValueError):
        _reject()
    _reject()

def _expect_list(value: object) -> list[object] | tuple[object, ...]:
    if not isinstance(value, (list, tuple)) or len(value) > MAX_APPLICATION_ARRAY_ITEMS:
        _reject()
    return value


def _validate_lifecycle_result(
    result: object,
    *,
    envelope_state: str | None = None,
) -> dict[str, object]:
    if not isinstance(result, Mapping):
        _reject()
    expected = (
        "ats",
        "job_url",
        "reason_code",
        "current_step",
        "coordinator_state",
        "browser_state",
        "last_observation_sha256",
        "artifact_manifest_sha256",
        "human_review_ready",
        "handoff_committed",
        "automated_submission",
    )
    _expect_exact_keys(result, expected)
    ats = result["ats"]
    if ats is not None:
        ats = _expect_public_code(ats, _ATS_NAMES)
    job_url = _expect_direct_ats_url(result["job_url"], ats)
    reason_code = _expect_reason_code(result["reason_code"])
    current_step = result["current_step"]
    if current_step is not None:
        current_step = _expect_safe_slug(current_step)
    coordinator_state = _expect_public_code(result["coordinator_state"], _LIFECYCLE_COORDINATOR_STATES)
    browser_state = _expect_public_code(result["browser_state"], _BROWSER_STATES)
    last_observation = _expect_hash_or_none(result["last_observation_sha256"])
    artifact_manifest = _expect_hash_or_none(result["artifact_manifest_sha256"])
    human_review_ready = result["human_review_ready"]
    handoff_committed = result["handoff_committed"]
    if type(human_review_ready) is not bool or type(handoff_committed) is not bool:
        _reject()
    if result["automated_submission"] is not False:
        _reject()
    if reason_code == "draft_ready" and not human_review_ready:
        _reject()
    if human_review_ready and reason_code != "draft_ready":
        _reject()
    if browser_state == "handed_off" and not handoff_committed:
        _reject()
    if handoff_committed and browser_state not in {"handed_off", "closed", "failed"}:
        _reject()
    if coordinator_state == "terminal" and reason_code is None:
        _reject()
    if browser_state == "failed" and reason_code is None:
        _reject()
    if envelope_state == "review_ready":
        if reason_code != "draft_ready" or not human_review_ready or not handoff_committed or browser_state != "handed_off":
            _reject()
    elif envelope_state in {"manual", "blocked"}:
        if reason_code is None or human_review_ready:
            _reject()
    elif envelope_state in {"starting", "running"}:
        if reason_code is not None or human_review_ready or handoff_committed:
            _reject()
    elif envelope_state == "failed":
        if reason_code is None or human_review_ready:
            _reject()
    return {
        "ats": ats,
        "job_url": job_url,
        "reason_code": reason_code,
        "current_step": current_step,
        "coordinator_state": coordinator_state,
        "browser_state": browser_state,
        "last_observation_sha256": last_observation,
        "artifact_manifest_sha256": artifact_manifest,
        "human_review_ready": human_review_ready,
        "handoff_committed": handoff_committed,
        "automated_submission": False,
    }


def _validate_mutation_result(result: object) -> dict[str, object]:
    if not isinstance(result, Mapping):
        _reject()
    expected = ("outcome", "reason_code", "observation_sha256", "changed")
    _expect_exact_keys(result, expected)
    outcome = _expect_public_code(result["outcome"], _ACTION_OUTCOMES)
    reason_code = _expect_reason_code(result["reason_code"])
    if outcome == "allowed" and reason_code is not None:
        _reject()
    if outcome in {"rejected", "manual"} and reason_code is None:
        _reject()
    observation_sha256 = _expect_hash(result["observation_sha256"])
    if type(result["changed"]) is not bool:
        _reject()
    return {
        "outcome": outcome,
        "reason_code": reason_code,
        "observation_sha256": observation_sha256,
        "changed": result["changed"],
    }


def _validate_screenshot_result(result: object) -> dict[str, object]:
    if not isinstance(result, Mapping):
        _reject()
    _expect_exact_keys(result, ("evidence_sha256", "observation_sha256"))
    return {
        "evidence_sha256": _expect_hash(result["evidence_sha256"]),
        "observation_sha256": _expect_hash(result["observation_sha256"]),
    }


def _validate_handoff_result(result: object, *, envelope_state: str | None = None) -> dict[str, object]:
    if not isinstance(result, Mapping):
        _reject()
    expected = ("outcome", "reason_code", "observation_sha256", "unresolved_required_count", "automated_submission")
    _expect_exact_keys(result, expected)
    if result["outcome"] != "committed":
        _reject()
    reason_code = _expect_reason_code(result["reason_code"], nullable=False)
    if reason_code == "draft_ready":
        if envelope_state is not None and envelope_state != "review_ready":
            _reject()
    elif envelope_state is not None and envelope_state not in {"manual", "blocked"}:
        _reject()
    if result["observation_sha256"] is None:
        _reject()
    observation_sha256 = _expect_hash(result["observation_sha256"])
    unresolved = _expect_nonnegative(result["unresolved_required_count"])
    if reason_code == "draft_ready" and unresolved != 0:
        _reject()
    if result["automated_submission"] is not False:
        _reject()
    return {
        "outcome": "committed",
        "reason_code": reason_code,
        "observation_sha256": observation_sha256,
        "unresolved_required_count": unresolved,
        "automated_submission": False,
    }


def _validate_observation_option(value: object) -> dict[str, object]:
    if not isinstance(value, Mapping):
        _reject()
    _expect_exact_keys(value, ("id", "label", "enabled"))
    return {
        "id": _expect_safe_slug(value["id"]),
        "label": _expect_public_text(value["label"]),
        "enabled": _expect_bool(value["enabled"]),
    }


def _expect_bool(value: object) -> bool:
    if type(value) is not bool:
        _reject()
    return value


def _validate_observation_field(value: object) -> dict[str, object]:
    if not isinstance(value, Mapping):
        _reject()
    expected = (
        "element_id",
        "frame_id",
        "label",
        "kind",
        "required",
        "disabled",
        "readonly",
        "has_value",
        "multiple",
        "options",
        "accept",
        "safety_class",
    )
    _expect_exact_keys(value, expected)
    options = [_validate_observation_option(item) for item in _expect_list(value["options"])]
    accept = [_expect_public_text(item, allow_empty=False) for item in _expect_list(value["accept"])]
    if len({str(item["id"]) for item in options}) != len(options):
        _reject()
    return {
        "element_id": _expect_safe_slug(value["element_id"]),
        "frame_id": _expect_safe_slug(value["frame_id"]),
        "label": _expect_public_text(value["label"]),
        "kind": _expect_safe_slug(value["kind"]),
        "required": _expect_bool(value["required"]),
        "disabled": _expect_bool(value["disabled"]),
        "readonly": _expect_bool(value["readonly"]),
        "has_value": _expect_bool(value["has_value"]),
        "multiple": _expect_bool(value["multiple"]),
        "options": options,
        "accept": accept,
        "safety_class": _expect_public_code(value["safety_class"], _SAFETY_CLASSES),
    }


def _validate_observation_control(value: object) -> dict[str, object]:
    if not isinstance(value, Mapping):
        _reject()
    expected = ("element_id", "frame_id", "label", "kind", "action_type", "enabled", "terminal")
    _expect_exact_keys(value, expected)
    return {
        "element_id": _expect_safe_slug(value["element_id"]),
        "frame_id": _expect_safe_slug(value["frame_id"]),
        "label": _expect_public_text(value["label"]),
        "kind": _expect_safe_slug(value["kind"]),
        "action_type": _expect_public_code(value["action_type"], _ACTION_TYPES),
        "enabled": _expect_bool(value["enabled"]),
        "terminal": _expect_bool(value["terminal"]),
    }


def _validate_observation_error(value: object) -> dict[str, object]:
    if not isinstance(value, Mapping):
        _reject()
    _expect_exact_keys(value, ("element_id", "code"))
    element_id = value["element_id"]
    if element_id is not None:
        element_id = _expect_safe_slug(element_id)
    return {"element_id": element_id, "code": _expect_safe_slug(value["code"])}


def _validate_progress(value: object) -> dict[str, object]:
    if not isinstance(value, Mapping):
        _reject()
    _expect_exact_keys(value, ("step_index", "step_count"))
    index = value["step_index"]
    count = value["step_count"]
    if index is not None:
        index = _expect_nonnegative(index)
    if count is not None:
        count = _expect_positive_run_id(count)
    if index is not None and count is not None and index >= count:
        _reject()
    return {"step_index": index, "step_count": count}


def _validate_observation_result(result: object) -> dict[str, object]:
    if not isinstance(result, Mapping):
        _reject()
    expected = (
        "observation_sha256",
        "observation_sequence",
        "observed_at",
        "url",
        "ats",
        "page_type",
        "frame_id",
        "fields",
        "controls",
        "validation_errors",
        "progress",
        "blocker_codes",
    )
    _expect_exact_keys(result, expected)
    ats = _expect_public_code(result["ats"], _ATS_NAMES)
    url = _expect_direct_ats_url(result["url"], ats)
    fields = [_validate_observation_field(item) for item in _expect_list(result["fields"])]
    controls = [_validate_observation_control(item) for item in _expect_list(result["controls"])]
    errors = [_validate_observation_error(item) for item in _expect_list(result["validation_errors"])]
    element_ids = [
        *(str(item["element_id"]) for item in fields),
        *(str(item["element_id"]) for item in controls),
    ]
    if len(set(element_ids)) != len(element_ids):
        _reject()
    blockers = [_expect_public_code(item, _BLOCKER_CODES) for item in _expect_list(result["blocker_codes"])]
    return {
        "observation_sha256": _expect_hash(result["observation_sha256"]),
        "observation_sequence": _expect_nonnegative(result["observation_sequence"]),
        "observed_at": _expect_rfc3339_utc(result["observed_at"]),
        "url": url,
        "ats": ats,
        "page_type": _expect_public_code(result["page_type"], _PAGE_TYPES),
        "frame_id": _expect_safe_slug(result["frame_id"]),
        "fields": fields,
        "controls": controls,
        "validation_errors": errors,
        "progress": _validate_progress(result["progress"]),
        "blocker_codes": blockers,
    }


def _validate_public_projection(
    operation: str,
    result: object,
    *,
    envelope_state: str | None = None,
) -> dict[str, object]:
    if operation in LIFECYCLE_OPERATIONS:
        return _validate_lifecycle_result(result, envelope_state=envelope_state)
    if operation in {
        "browser.fill_field",
        "browser.select_option",
        "browser.set_checkbox",
        "browser.upload_configured_resume",
        "browser.activate_safe_control",
    }:
        return _validate_mutation_result(result)
    if operation == "browser.capture_screenshot":
        return _validate_screenshot_result(result)
    if operation == "browser.prepare_human_handoff":
        return _validate_handoff_result(result, envelope_state=envelope_state)
    if operation == "browser.observe":
        return _validate_observation_result(result)
    _reject("unsupported_operation")
def validate_public_result(
    result: object,
    *,
    request: ApplicationRpcRequest | None = None,
    operation: str | None = None,
    envelope_state: str | None = None,
) -> JsonValue:
    if request is not None:
        if operation is not None and operation != request.operation:
            _reject()
        operation = request.operation
    if operation is None or operation not in _APPLICATION_OPERATION_SET:
        _reject("unsupported_operation")
    safe_result = _validate_public_projection(operation, result, envelope_state=envelope_state)
    _validate_json_value(safe_result)
    submitted: tuple[object, ...] = ()
    if request is not None:
        payload = thaw_json(request.payload)
        value = payload.get("value") if isinstance(payload, dict) else None
        if type(value) is str:
            submitted = (value,)
        elif isinstance(value, (list, tuple)):
            submitted = tuple(item for item in value if type(item) is str)
    if submitted and _contains_submitted_action_value(safe_result, submitted):
        _reject()
    frozen = freeze_json(safe_result)
    encoded = _canonical_json(frozen)
    if len(encoded.encode("utf-8")) > MAX_APPLICATION_JSON_BYTES:
        _reject()
    return frozen


def _coerce_request(request: ApplicationRpcRequest | Mapping[str, object]) -> ApplicationRpcRequest:
    if isinstance(request, ApplicationRpcRequest):
        return request
    return parse_application_request(request)


def _error_object(error: object) -> dict[str, str]:
    if isinstance(error, Mapping):
        if set(error) == {"code"}:
            code = error["code"]
        elif set(error) == {"code", "message"}:
            code = error["code"]
            if type(code) is not str or code not in PUBLIC_ERROR_CODES or error["message"] != PUBLIC_ERROR_MESSAGES[code]:
                _reject()
        else:
            _reject()
    else:
        code = error
    if type(code) is not str or code not in PUBLIC_ERROR_CODES:
        _reject()
    return {"code": code, "message": PUBLIC_ERROR_MESSAGES[code]}


def _best_effort_object(value: object) -> Mapping[str, object]:
    if isinstance(value, Mapping):
        return value
    if isinstance(value, (str, bytes, bytearray)):
        try:
            decoded = json.loads(value)
        except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError):
            return {}
        return decoded if isinstance(decoded, Mapping) else {}
    return {}


def _safe_uuid(value: object) -> str | None:
    if type(value) is not str or _UUID_RE.fullmatch(value) is None:
        return None
    try:
        return value if str(UUID(value)) == value else None
    except (ValueError, AttributeError):
        return None


def _safe_transport_id(value: object) -> str | None:
    if type(value) is not str or _TRANSPORT_ID_RE.fullmatch(value) is None or ".." in value:
        return None
    return value


def _safe_positive_run_id(value: object) -> int | None:
    if type(value) is not int or value <= 0 or value > MAX_APPLICATION_RUN_ID:
        return None
    return value


def _safe_operation(value: object) -> str | None:
    return value if type(value) is str and value in _APPLICATION_OPERATION_SET else None


def build_rejected_application_response(
    value: object,
    *,
    error: object = "invalid_request",
) -> dict[str, object]:
    raw = _best_effort_object(value)
    error_code = error if type(error) is str and error in PUBLIC_ERROR_CODES else "invalid_request"
    response = {
        "protocol_version": APPLICATION_RPC_PROTOCOL_VERSION,
        "request_id": _safe_uuid(raw.get("request_id")),
        "operation": _safe_operation(raw.get("operation")),
        "ok": False,
        "run_id": _safe_positive_run_id(raw.get("run_id")),
        "state": "failed",
        "action_sequence": 0,
        "event_sequence": 0,
        "result": None,
        "error": _error_object(error_code),
    }
    if len(_canonical_json(response).encode("utf-8")) > MAX_APPLICATION_JSON_BYTES:
        _reject()
    return response


def build_rejected_host_tool_result(
    value: object,
    *,
    error: object = "invalid_request",
) -> dict[str, object]:
    raw = _best_effort_object(value)
    host_id = _safe_transport_id(raw.get("id")) or "rejected"
    response = build_rejected_application_response({}, error=error)
    text = _canonical_json(response)
    return {
        "type": "host_tool_result",
        "id": host_id,
        "result": {"content": [{"type": "text", "text": text}]},
        "isError": True,
    }


def build_application_response(
    request: ApplicationRpcRequest | Mapping[str, object],
    *,
    ok: bool,
    state: str,
    action_sequence: int,
    event_sequence: int,
    result: object = None,
    error: object = None,
    run_id: int | None = None,
) -> dict[str, object]:
    request = _coerce_request(request)
    if type(ok) is not bool or type(state) is not str or state not in _RUN_STATE_SET:
        _reject()
        action_sequence = _expect_nonnegative(action_sequence)
        event_sequence = _expect_nonnegative(event_sequence)
    if run_id is None:
        run_id = request.run_id
    elif type(run_id) is not int or run_id <= 0 or run_id > MAX_APPLICATION_RUN_ID:
        _reject()
    if request.operation != "run.start" and run_id != request.run_id:
        _reject()
    if ok:
        if error is not None:
            _reject()
        safe_result = validate_public_result(result, request=request, envelope_state=state)
        safe_error = None
    else:
        if result is not None:
            _reject()
        safe_result = None
        safe_error = _error_object(error)
    envelope: dict[str, object] = {
        "protocol_version": APPLICATION_RPC_PROTOCOL_VERSION,
        "request_id": request.request_id,
        "operation": request.operation,
        "ok": ok,
        "run_id": run_id,
        "state": state,
        "action_sequence": action_sequence,
        "event_sequence": event_sequence,
        "result": thaw_json(safe_result) if safe_result is not None else None,
        "error": safe_error,
    }
    _validate_response_envelope(envelope, request=request)
    if len(_canonical_json(envelope).encode("utf-8")) > MAX_APPLICATION_JSON_BYTES:
        _reject()
    return envelope


def _validate_response_envelope(value: object, *, request: ApplicationRpcRequest | None = None) -> None:
    if not isinstance(value, Mapping):
        _reject()
    _validate_json_value(value)
    _expect_exact_keys(value, _RESPONSE_KEYS)
    if value["protocol_version"] != APPLICATION_RPC_PROTOCOL_VERSION or type(value["protocol_version"]) is not int:
        _reject("protocol_mismatch")
    _expect_uuid(value["request_id"])
    if type(value["operation"]) is not str or value["operation"] not in _APPLICATION_OPERATION_SET:
        _reject("unsupported_operation")
    is_start = value["operation"] == "run.start"
    if is_start:
        if value["run_id"] is not None:
            _expect_positive_run_id(value["run_id"])
        elif value["ok"]:
            _reject()
    else:
        _expect_positive_run_id(value["run_id"])
    if request is not None:
        if value["request_id"] != request.request_id or value["operation"] != request.operation:
            _reject()
        if not is_start and value["run_id"] != request.run_id:
            _reject()
    if type(value["ok"]) is not bool:
        _reject()
    if type(value["state"]) is not str or value["state"] not in _RUN_STATE_SET:
        _reject()
        _expect_nonnegative(value["action_sequence"])
        _expect_nonnegative(value["event_sequence"])
    if value["ok"]:
        if value["error"] is not None:
            _reject()
    else:
        if value["result"] is not None:
            _reject()
        _error_object(value["error"])
    if value["ok"]:
        if value["result"] is None:
            _reject()
        validate_public_result(
            value["result"],
            request=request,
            operation=value["operation"],
            envelope_state=value["state"],
        )
def parse_rejected_application_response(value: object) -> Mapping[str, JsonValue]:
    raw = _decode_json_object(value)
    _expect_exact_keys(raw, _RESPONSE_KEYS)
    if raw["protocol_version"] != APPLICATION_RPC_PROTOCOL_VERSION or type(raw["protocol_version"]) is not int:
        _reject("protocol_mismatch")
    request_id = raw["request_id"]
    if request_id is not None:
        _expect_uuid(request_id)
    operation = raw["operation"]
    if operation is not None:
        if type(operation) is not str or operation not in _APPLICATION_OPERATION_SET:
            _reject("unsupported_operation")
    if raw["ok"] is not False:
        _reject()
    if raw["run_id"] is not None:
        _expect_positive_run_id(raw["run_id"])
    if raw["state"] != "failed":
        _reject()
    if raw["action_sequence"] != 0 or raw["event_sequence"] != 0:
        _reject()
    if raw["result"] is not None:
        _reject()
    error = _error_object(raw["error"])
    frozen = freeze_json(
        {
            "protocol_version": APPLICATION_RPC_PROTOCOL_VERSION,
            "request_id": request_id,
            "operation": operation,
            "ok": False,
            "run_id": raw["run_id"],
            "state": "failed",
            "action_sequence": 0,
            "event_sequence": 0,
            "result": None,
            "error": error,
        }
    )
    _canonical_json(frozen)
    return frozen  # type: ignore[return-value]


def parse_application_response(
    value: object,
    *,
    request: ApplicationRpcRequest | None = None,
) -> Mapping[str, JsonValue]:
    raw = _decode_json_object(value)
    if request is None and (raw.get("request_id") is None or raw.get("operation") is None):
        return parse_rejected_application_response(raw)
    _validate_response_envelope(raw, request=request)
    if request is not None:
        if raw["request_id"] != request.request_id or raw["operation"] != request.operation:
            _reject()
        raw_run_id = raw["run_id"]
        if request.operation != "run.start" and raw_run_id != request.run_id:
            _reject()
        if request.operation == "run.start" and raw_run_id is not None:
            _expect_positive_run_id(raw_run_id)
    frozen = freeze_json(dict(raw))
    _canonical_json(frozen)
    return frozen  # type: ignore[return-value]


def build_host_tool_result(
    proposal: BrowserToolProposal,
    response: Mapping[str, object],
    *,
    is_error: bool | None = None,
) -> dict[str, object]:
    if not isinstance(proposal, BrowserToolProposal):
        _reject()
    _validate_response_envelope(response, request=proposal.request)
    expected_is_error = not bool(response["ok"])
    if is_error is not None and type(is_error) is not bool:
        _reject()
    if is_error is not None and is_error != expected_is_error:
        _reject()
    is_error = expected_is_error
    text = _canonical_json(response)
    output: dict[str, object] = {
        "type": "host_tool_result",
        "id": proposal.host_call_id,
        "result": {"content": [{"type": "text", "text": text}]},
    }
    if is_error:
        output["isError"] = True
    _validate_json_value(output)
    return output


__all__ = [
    "APPLICATION_RPC_PROTOCOL_VERSION",
    "APPLICATION_OPERATIONS",
    "BROWSER_HOST_TOOL_DEFINITIONS",
    "LIFECYCLE_OPERATIONS",
    "BROWSER_OPERATIONS",
    "ApplicationRpcError",
    "ApplicationRpcRequest",
    "HostToolContext",
    "BrowserToolProposal",
    "parse_application_request",
    "parse_host_tool_call",
    "parse_rejected_application_response",
    "parse_application_response",
    "semantic_request_sha256",
    "build_application_response",
    "build_host_tool_result",
    "build_rejected_application_response",
    "build_rejected_host_tool_result",
    "build_set_host_tools_command",
    "PUBLIC_REASON_CODES",
    "PUBLIC_ERROR_CODES",
    "validate_public_result",
    "PUBLIC_ERROR_MESSAGES",
    "RUN_STATES",
    "MAX_APPLICATION_JSON_BYTES",
    "MAX_APPLICATION_JSON_DEPTH",
]
