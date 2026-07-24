from __future__ import annotations

import json
import math
from uuid import UUID, uuid5
import pytest

from jobs_assistant.application_rpc_contracts import (
    APPLICATION_OPERATIONS,
    BROWSER_HOST_TOOL_DEFINITIONS,
    MAX_APPLICATION_JSON_BYTES,
    MAX_APPLICATION_JSON_DEPTH,
    ApplicationRpcError,
    BrowserToolProposal,
    build_rejected_application_response,
    build_application_response,
    build_host_tool_result,
    parse_rejected_application_response,
    parse_application_request,
    parse_application_response,
    parse_host_tool_call,
    semantic_request_sha256,
)

NOW = 1_700_000_000_000
REQ_ID = "123e4567-e89b-12d3-a456-426614174000"
JOB_URL = "https://boards.greenhouse.io/acme/jobs/123"
BROWSER_OPERATIONS = {
    "browser.observe",
    "browser.fill_field",
    "browser.select_option",
    "browser.set_checkbox",
    "browser.upload_configured_resume",
    "browser.activate_safe_control",
    "browser.capture_screenshot",
    "browser.prepare_human_handoff",
}


def start_request(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "goal": "prepare_application_draft",
        "job_url": JOB_URL,
        "candidate_profile_id": "candidate-main",
        "configured_resume_id": "resume-main",
        "headed": True,
    }
    payload.update(overrides.pop("payload", {}))  # type: ignore[arg-type]
    request: dict[str, object] = {
        "protocol_version": 1,
        "request_id": REQ_ID,
        "operation": "run.start",
        "deadline_unix_ms": NOW + 30_000,
        "run_id": None,
        "payload": payload,
    }
    request.update(overrides)
    return request


def application_request(operation: str, **overrides: object) -> dict[str, object]:
    request = start_request(operation=operation, run_id=42, payload={})
    request["payload"] = operation_payload(operation, **overrides)
    return request


def operation_payload(operation: str, **overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {}
    if operation in {"browser.fill_field", "browser.select_option", "browser.set_checkbox"}:
        payload.update(
            {
                "observation_sha256": "a" * 64,
                "element_id": "field-1",
                "value": "safe-value" if operation != "browser.set_checkbox" else True,
                "confidence": 0.9,
                "reason": "explicit configured answer",
            }
        )
    elif operation in {"browser.upload_configured_resume", "browser.activate_safe_control"}:
        payload.update({"observation_sha256": "a" * 64, "element_id": "control-1"})
    elif operation in {"browser.capture_screenshot", "browser.prepare_human_handoff"}:
        payload.update({"observation_sha256": "a" * 64})
    payload.update(overrides)
    return payload


def host_context(**overrides: object) -> dict[str, object]:
    context: dict[str, object] = {
        "protocol_version": 1,
        "run_id": 42,
        "request_id": REQ_ID,
        "deadline_unix_ms": NOW + 30_000,
    }
    context.update(overrides)
    return context


def host_call(operation: str, **argument_overrides: object) -> dict[str, object]:
    return {
        "type": "host_tool_call",
        "id": "host-1",
        "toolCallId": "toolu-1",
        "toolName": operation,
        "arguments": operation_payload(operation, **argument_overrides),
    }


def lifecycle_result(state: str = "starting") -> dict[str, object]:
    reason_codes = {
        "starting": None,
        "running": None,
        "manual": "required_sensitive_fields_manual",
        "blocked": "captcha",
        "review_ready": "draft_ready",
        "failed": "browser_error",
    }
    coordinator_states = {
        "starting": "starting",
        "running": "executing",
        "manual": "awaiting_resume",
        "blocked": "terminal",
        "review_ready": "terminal",
        "failed": "terminal",
    }
    browser_states = {
        "starting": "starting",
        "running": "owned",
        "manual": "idle",
        "blocked": "failed",
        "review_ready": "handed_off",
        "failed": "failed",
    }
    ready = state == "review_ready"
    return {
        "ats": "greenhouse",
        "job_url": JOB_URL,
        "reason_code": reason_codes[state],
        "current_step": "contact_info",
        "coordinator_state": coordinator_states[state],
        "browser_state": browser_states[state],
        "last_observation_sha256": "a" * 64,
        "artifact_manifest_sha256": None,
        "human_review_ready": ready,
        "handoff_committed": ready,
        "automated_submission": False,
    }


def mutation_result(*, outcome: str = "allowed", reason_code: str | None = None) -> dict[str, object]:
    return {
        "outcome": outcome,
        "reason_code": reason_code,
        "observation_sha256": "a" * 64,
        "changed": outcome == "allowed",
    }


def observation_result() -> dict[str, object]:
    return {
        "observation_sha256": "b" * 64,
        "observation_sequence": 3,
        "observed_at": "2026-01-01T00:00:00Z",
        "url": JOB_URL,
        "ats": "greenhouse",
        "page_type": "application",
        "frame_id": "frame-1",
        "fields": [
            {
                "element_id": "field-1",
                "frame_id": "frame-1",
                "label": "Full name",
                "kind": "text",
                "required": True,
                "disabled": False,
                "readonly": False,
                "has_value": False,
                "multiple": False,
                "options": [],
                "accept": [],
                "safety_class": "safe",
            }
        ],
        "controls": [
            {
                "element_id": "continue-1",
                "frame_id": "frame-1",
                "label": "Continue",
                "kind": "button",
                "action_type": "continue",
                "enabled": True,
                "terminal": False,
            }
        ],
        "validation_errors": [{"element_id": None, "code": "page_validation_error"}],
        "progress": {"step_index": 0, "step_count": 2},
        "blocker_codes": [],
    }


def test_operation_allowlist_and_strict_browser_tool_definitions() -> None:
    assert set(APPLICATION_OPERATIONS) == {
        "run.start",
        "run.status",
        "run.resume",
        "run.cancel",
        *BROWSER_OPERATIONS,
    }
    assert {tool["name"] for tool in BROWSER_HOST_TOOL_DEFINITIONS} == BROWSER_OPERATIONS
    assert not any(tool["name"].startswith("run.") for tool in BROWSER_HOST_TOOL_DEFINITIONS)
    for tool in BROWSER_HOST_TOOL_DEFINITIONS:
        assert set(tool) == {"name", "label", "description", "parameters"}
        schema = tool["parameters"]
        assert schema["additionalProperties"] is False
        assert not ({"protocol_version", "run_id", "request_id", "deadline_unix_ms"} & set(schema["properties"]))


@pytest.mark.parametrize("operation", sorted(APPLICATION_OPERATIONS))
def test_each_operation_has_exact_application_schema(operation: str) -> None:
    if operation == "run.start":
        request = start_request()
    else:
        request = application_request(operation)
    parsed = parse_application_request(request, now_unix_ms=NOW)
    assert parsed.operation == operation


@pytest.mark.parametrize("operation", sorted(BROWSER_OPERATIONS))
def test_each_browser_operation_parses_exact_native_host_arguments(operation: str) -> None:
    parsed = parse_host_tool_call(host_call(operation), host_context(), now_unix_ms=NOW)
    assert isinstance(parsed, BrowserToolProposal)
    assert parsed.request.operation == operation
    assert parsed.parent_request_id == REQ_ID
    assert parsed.request.request_id == str(uuid5(UUID(REQ_ID), "host-1\0toolu-1\0" + operation))
    assert set(parsed.arguments) == set(operation_payload(operation))


@pytest.mark.parametrize(
    "extra_key",
    ("path", "basename", "filename", "file", "resume_file"),
)
def test_configured_resume_upload_rejects_model_selected_file_metadata(extra_key: str) -> None:
    parsed = parse_host_tool_call(
        host_call("browser.upload_configured_resume"),
        host_context(),
        now_unix_ms=NOW,
    )
    assert set(parsed.request.payload) == {"observation_sha256", "element_id"}
    with pytest.raises(ApplicationRpcError):
        parse_host_tool_call(
            host_call(
                "browser.upload_configured_resume",
                **{extra_key: "/tmp/alternate.pdf"},
            ),
            host_context(),
            now_unix_ms=NOW,
        )




def test_application_envelope_and_native_frame_are_exact_and_metadata_is_host_bound() -> None:
    parsed = parse_application_request(start_request(), now_unix_ms=NOW)
    assert set(parsed.to_mapping()) == {
        "protocol_version",
        "request_id",
        "operation",
        "deadline_unix_ms",
        "run_id",
        "payload",
    }
    with pytest.raises(ApplicationRpcError):
        parse_host_tool_call(host_call("browser.observe"), now_unix_ms=NOW)
    with pytest.raises(ApplicationRpcError):
        parse_host_tool_call(
            {**host_call("browser.observe"), "arguments": {"protocol_version": 1}},
            host_context(),
            now_unix_ms=NOW,
        )
    with pytest.raises(ApplicationRpcError):
        parse_host_tool_call({**host_call("browser.observe"), "extra": 1}, host_context(), now_unix_ms=NOW)


def test_unknown_fields_and_unsafe_operations_rejected() -> None:
    with pytest.raises(ApplicationRpcError):
        parse_application_request({**start_request(), "extra": 1}, now_unix_ms=NOW)
    payload = dict(start_request()["payload"])
    payload["path"] = "/tmp/resume.pdf"
    with pytest.raises(ApplicationRpcError):
        parse_application_request(start_request(payload=payload), now_unix_ms=NOW)
    for unsafe in (
        "browser.click",
        "browser.type",
        "browser.navigate",
        "browser.evaluate",
        "browser.submit",
        "browser.upload",
        "run.handoff",
        "run.submit",
        "submit_application",
    ):
        with pytest.raises(ApplicationRpcError):
            parse_host_tool_call(host_call(unsafe), host_context(), now_unix_ms=NOW)
    with pytest.raises(ApplicationRpcError):
        parse_application_request(start_request(payload={"headed": False}), now_unix_ms=NOW)


@pytest.mark.parametrize("bad_id", [REQ_ID.upper(), "123e4567-e89b-12d3-a456-42661417400", "not-a-uuid"])
def test_request_id_must_be_canonical_lowercase_uuid(bad_id: str) -> None:
    with pytest.raises(ApplicationRpcError):
        parse_application_request(start_request(request_id=bad_id), now_unix_ms=NOW)


@pytest.mark.parametrize("deadline", [NOW - 1, NOW + 300_001, True, 1.0])
def test_deadline_is_injected_now_bounded_and_strict_integer(deadline: object) -> None:
    with pytest.raises(ApplicationRpcError):
        parse_application_request(start_request(deadline_unix_ms=deadline), now_unix_ms=NOW)
    assert parse_application_request(start_request(deadline_unix_ms=NOW), now_unix_ms=NOW).deadline_unix_ms == NOW
    assert parse_application_request(start_request(deadline_unix_ms=NOW + 300_000), now_unix_ms=NOW).deadline_unix_ms == NOW + 300_000


def test_host_context_deadline_is_required_and_validated_before_arguments() -> None:
    with pytest.raises(ApplicationRpcError):
        parse_host_tool_call(host_call("browser.observe"), {"protocol_version": 1, "run_id": 42, "request_id": REQ_ID, "deadline_unix_ms": NOW - 1}, now_unix_ms=NOW)
    with pytest.raises(ApplicationRpcError):
        parse_host_tool_call(host_call("browser.observe"), {"protocol_version": 1, "run_id": True, "request_id": REQ_ID, "deadline_unix_ms": NOW}, now_unix_ms=NOW)
    parsed = parse_host_tool_call(host_call("browser.observe"), protocol_version=1, run_id=42, request_id=REQ_ID, deadline_unix_ms=NOW, now_unix_ms=NOW)
    assert parsed.request.deadline_unix_ms == NOW


def test_run_id_is_null_only_for_start_and_positive_for_other_operations() -> None:
    with pytest.raises(ApplicationRpcError):
        parse_application_request(start_request(run_id=0), now_unix_ms=NOW)
    with pytest.raises(ApplicationRpcError):
        parse_application_request(application_request("run.status", **{"run_id": None}), now_unix_ms=NOW)
    with pytest.raises(ApplicationRpcError):
        parse_application_request(application_request("run.status", **{"run_id": True}), now_unix_ms=NOW)
    assert parse_application_request(application_request("run.status"), now_unix_ms=NOW).run_id == 42


def test_profile_and_resume_ids_are_safe_opaque_slugs_not_paths() -> None:
    for key in ("candidate_profile_id", "configured_resume_id"):
        for value in ("/tmp/x", "../x", "..", "resume\\x", "file:///x", ""):
            with pytest.raises(ApplicationRpcError):
                parse_application_request(start_request(payload={key: value}), now_unix_ms=NOW)
    assert parse_application_request(start_request(), now_unix_ms=NOW).payload["configured_resume_id"] == "resume-main"


def test_action_value_types_and_deterministic_null_triplet() -> None:
    for operation in ("browser.fill_field", "browser.select_option", "browser.set_checkbox"):
        base = host_call(operation)
        args = dict(base["arguments"])
        args.update({"value": None, "confidence": None, "reason": None})
        assert parse_host_tool_call({**base, "arguments": args}, host_context(), now_unix_ms=NOW).request.payload["value"] is None
        for key, value in (("confidence", math.nan), ("confidence", True), ("reason", 0), ("value", object())):
            bad_args = dict(base["arguments"])
            bad_args[key] = value
            with pytest.raises(ApplicationRpcError):
                parse_host_tool_call({**base, "arguments": bad_args}, host_context(), now_unix_ms=NOW)
    with pytest.raises(ApplicationRpcError):
        parse_host_tool_call(host_call("browser.fill_field", value="answer", confidence=None, reason="why"), host_context(), now_unix_ms=NOW)
    with pytest.raises(ApplicationRpcError):
        parse_host_tool_call(host_call("browser.fill_field", value="answer", confidence=0.69, reason="why"), host_context(), now_unix_ms=NOW)
    with pytest.raises(ApplicationRpcError):
        parse_host_tool_call(host_call("browser.fill_field", value="answer", confidence=0.9, reason=None), host_context(), now_unix_ms=NOW)


def test_select_accepts_tuple_or_list_of_strings_but_checkbox_is_strict_bool() -> None:
    for value in (("A", "B"), ["A", "B"], "A"):
        parsed = parse_host_tool_call(host_call("browser.select_option", value=value), host_context(), now_unix_ms=NOW)
        assert parsed.request.payload["value"] in ("A", ("A", "B"))
    for value in (1, 0, "true", [], [1], ["A", 1]):
        with pytest.raises(ApplicationRpcError):
            parse_host_tool_call(host_call("browser.set_checkbox", value=value), host_context(), now_unix_ms=NOW)
    for value in (True, False):
        assert parse_host_tool_call(host_call("browser.set_checkbox", value=value), host_context(), now_unix_ms=NOW).request.payload["value"] is value


def test_semantic_hash_changes_only_when_non_deadline_intent_changes() -> None:
    first = parse_application_request(start_request(deadline_unix_ms=NOW + 1), now_unix_ms=NOW)
    second = parse_application_request(start_request(deadline_unix_ms=NOW + 300_000), now_unix_ms=NOW)
    assert semantic_request_sha256(first) == semantic_request_sha256(second) == first.semantic_sha256
    changed = parse_application_request(start_request(payload={"candidate_profile_id": "candidate-other"}), now_unix_ms=NOW)
    assert semantic_request_sha256(first) != semantic_request_sha256(changed)


def test_immutable_request_and_proposal() -> None:
    raw = start_request()
    parsed = parse_application_request(raw, now_unix_ms=NOW)
    raw["payload"]["headed"] = False  # type: ignore[index]
    assert parsed.payload["headed"] is True
    with pytest.raises(TypeError):
        parsed.payload["headed"] = False  # type: ignore[index]
    with pytest.raises((AttributeError, TypeError)):
        parsed.request_id = "x"  # type: ignore[misc]
    proposal = parse_host_tool_call(host_call("browser.observe"), host_context(), now_unix_ms=NOW)
    with pytest.raises((AttributeError, TypeError)):
        proposal.tool_name = "browser.submit"  # type: ignore[misc]


def test_operation_specific_public_projections_and_persisted_replay() -> None:
    observe_request = parse_application_request(application_request("browser.observe"), now_unix_ms=NOW)
    observe_response = build_application_response(
        observe_request,
        ok=True,
        state="running",
        action_sequence=0,
        event_sequence=1,
        result=observation_result(),
    )
    replayed = parse_application_response(observe_response, request=observe_request)
    assert replayed["result"]["fields"][0]["has_value"] is False  # type: ignore[index]
    for bad in (
        {**observation_result(), "unknown": True},
        {**observation_result(), "fields": [{**observation_result()["fields"][0], "value": "secret"}]},  # type: ignore[index]
        {**observation_result(), "fields": [{**observation_result()["fields"][0], "name": "email"}]},  # type: ignore[index]
    ):
        with pytest.raises(ApplicationRpcError):
            build_application_response(
                observe_request,
                ok=True,
                state="running",
                action_sequence=0,
                event_sequence=1,
                result=bad,
            )
    action_request = parse_application_request(application_request("browser.fill_field"), now_unix_ms=NOW)
    with pytest.raises(ApplicationRpcError):
        build_application_response(
            action_request,
            ok=True,
            state="running",
            action_sequence=1,
            event_sequence=2,
            result=mutation_result(outcome="rejected"),
        )
    rejected = build_application_response(
        action_request,
        ok=True,
        state="manual",
        action_sequence=1,
        event_sequence=2,
        result=mutation_result(outcome="rejected", reason_code="page_validation_error"),
    )
    assert rejected["result"]["reason_code"] == "page_validation_error"  # type: ignore[index]


def test_public_observation_rejects_duplicate_field_and_control_element_ids() -> None:
    request = parse_application_request(
        application_request("browser.observe"),
        now_unix_ms=NOW,
    )
    result = observation_result()
    result["controls"][0]["element_id"] = result["fields"][0]["element_id"]  # type: ignore[index]
    with pytest.raises(ApplicationRpcError):
        build_application_response(
            request,
            ok=True,
            state="running",
            action_sequence=0,
            event_sequence=1,
            result=result,
        )


def test_public_observation_rejects_duplicate_option_ids() -> None:
    request = parse_application_request(
        application_request("browser.observe"),
        now_unix_ms=NOW,
    )
    result = observation_result()
    result["fields"][0]["kind"] = "select"  # type: ignore[index]
    result["fields"][0]["options"] = [  # type: ignore[index]
        {"id": "option-1", "label": "One", "enabled": True},
        {"id": "option-1", "label": "Also one", "enabled": True},
    ]
    with pytest.raises(ApplicationRpcError):
        build_application_response(
            request,
            ok=True,
            state="running",
            action_sequence=0,
            event_sequence=1,
            result=result,
        )




def test_response_envelope_states_and_fixed_public_errors() -> None:
    request = parse_application_request(start_request(), now_unix_ms=NOW)
    for state in ("starting", "running", "manual", "blocked", "review_ready", "failed"):
        response = build_application_response(
            request,
            ok=True,
            state=state,
            action_sequence=0,
            event_sequence=1,
            result=lifecycle_result(state),
            run_id=42,
        )
        assert set(response) == {"protocol_version", "request_id", "operation", "ok", "run_id", "state", "action_sequence", "event_sequence", "result", "error"}
        assert response["state"] == state and response["error"] is None
    with pytest.raises(ApplicationRpcError):
        build_application_response(request, ok=True, state="done", action_sequence=0, event_sequence=0)
    failed = build_application_response(request, ok=False, state="failed", action_sequence=0, event_sequence=1, error="invalid_request")
    assert failed["error"] == {"code": "invalid_request", "message": "Request rejected"}
    with pytest.raises(ApplicationRpcError):
        build_application_response(request, ok=False, state="failed", action_sequence=0, event_sequence=0, error="nope")


def test_response_privacy_rejects_nested_forbidden_keys_paths_oversize_and_depth() -> None:
    request = parse_application_request(start_request(), now_unix_ms=NOW)
    for result in (
        {"nested": {"cookie": "secret"}},
        {"nested": {"resume_path": "x"}},
        {"nested": {"CDP": "ws://secret"}},
        {"nested": {"where": "/tmp/private"}},
        {"nested": {"where": "../private"}},
        {"nested": {"where": "C:\\private\\resume.pdf"}},
        {"nested": {"where": "\\\\server\\share"}},
    ):
        with pytest.raises(ApplicationRpcError):
            build_application_response(request, ok=True, state="running", action_sequence=0, event_sequence=0, result=result)
    with pytest.raises(ApplicationRpcError):
        build_application_response(request, ok=True, state="running", action_sequence=0, event_sequence=0, result={"x": "a" * (MAX_APPLICATION_JSON_BYTES + 1)})
    too_deep: object = "x"
    for _ in range(MAX_APPLICATION_JSON_DEPTH + 1):
        too_deep = {"x": too_deep}
    with pytest.raises(ApplicationRpcError):
        build_application_response(request, ok=True, state="running", action_sequence=0, event_sequence=0, result=too_deep)


def test_response_never_echoes_submitted_fill_value_and_host_result_is_native_shape() -> None:
    proposal = parse_host_tool_call(host_call("browser.fill_field", value="SECRET_SENTINEL"), host_context(), now_unix_ms=NOW)
    with pytest.raises(ApplicationRpcError):
        build_application_response(proposal.request, ok=True, state="running", action_sequence=1, event_sequence=2, result={"echo": "SECRET_SENTINEL"})
    safe = build_application_response(proposal.request, ok=True, state="running", action_sequence=1, event_sequence=2, result=mutation_result())
    native = build_host_tool_result(proposal, safe)
    assert set(native) == {"type", "id", "result"}
    assert native["type"] == "host_tool_result" and native["id"] == "host-1"
    text = native["result"]["content"][0]["text"]
    assert json.loads(text)["result"] == mutation_result()
    assert "SECRET_SENTINEL" not in json.dumps(native)


def test_native_host_call_requires_exact_frame_and_metadata_context() -> None:
    call = host_call("browser.observe")
    for key in ("type", "id", "toolCallId", "toolName", "arguments"):
        bad = dict(call)
        bad.pop(key)
        with pytest.raises(ApplicationRpcError):
            parse_host_tool_call(bad, host_context(), now_unix_ms=NOW)
    with pytest.raises(ApplicationRpcError):
        parse_host_tool_call({**call, "type": "host_tool_result"}, host_context(), now_unix_ms=NOW)


def test_browser_proposal_requires_typed_operation_bound_request() -> None:
    proposal = parse_host_tool_call(
        host_call("browser.observe"),
        host_context(),
        now_unix_ms=NOW,
    )
    mismatched_mapping = proposal.request.to_mapping()
    mismatched_mapping["operation"] = "browser.fill_field"
    mismatched_mapping["payload"] = operation_payload("browser.fill_field")
    mismatched = parse_application_request(mismatched_mapping, now_unix_ms=NOW)
    with pytest.raises(ApplicationRpcError):
        BrowserToolProposal(
            proposal.host_call_id,
            proposal.tool_call_id,
            proposal.tool_name,
            mismatched,
            proposal.parent_request_id,
        )
    with pytest.raises(ApplicationRpcError):
        BrowserToolProposal(
            proposal.host_call_id,
            proposal.tool_call_id,
            proposal.tool_name,
            object(),  # type: ignore[arg-type]
            proposal.parent_request_id,
        )


def test_json_limits_and_malformed_json_values_rejected() -> None:
    with pytest.raises(ApplicationRpcError):
        parse_application_request("{not-json", now_unix_ms=NOW)
    with pytest.raises(ApplicationRpcError):
        parse_application_request(start_request(payload={"headed": object()}), now_unix_ms=NOW)
    with pytest.raises(ApplicationRpcError):
        parse_application_request(start_request(payload={"job_url": "https://x/" + "a" * 2_100}), now_unix_ms=NOW)
    with pytest.raises(ApplicationRpcError):
        parse_application_request(start_request(payload={"candidate_profile_id": "x" * 129}), now_unix_ms=NOW)


def test_job_url_must_be_exact_canonical_ats_route() -> None:
    for url in (
        JOB_URL + "?gh_src=tracking",
        JOB_URL + "?tracking=1",
        "https://BOARDS.GREENHOUSE.IO/acme/jobs/123",
        "https://jobs.lever.co/acme/123e4567-e89b-12d3-a456-426614174000?x=1",
        "https://jobs.lever.co/acme/123e4567-e89b-12d3-a456-426614174000#fragment",
    ):
        with pytest.raises(ApplicationRpcError):
            parse_application_request(start_request(payload={"job_url": url}), now_unix_ms=NOW)


def test_response_request_binding_start_run_and_host_error_consistency() -> None:
    start = parse_application_request(start_request(), now_unix_ms=NOW)
    with pytest.raises(ApplicationRpcError):
        build_application_response(
            start,
            ok=True,
            state="running",
            action_sequence=0,
            event_sequence=0,
            result=lifecycle_result("running"),
        )
    good = build_application_response(
        start,
        ok=True,
        state="running",
        action_sequence=0,
        event_sequence=0,
        result=lifecycle_result("running"),
        run_id=42,
    )
    mismatched = dict(good)
    mismatched["request_id"] = "12345678-1234-4234-8234-123456789012"
    with pytest.raises(ApplicationRpcError):
        parse_application_response(mismatched, request=start)

    proposal = parse_host_tool_call(host_call("browser.observe"), host_context(), now_unix_ms=NOW)
    observe = build_application_response(
        proposal.request,
        ok=True,
        state="running",
        action_sequence=0,
        event_sequence=1,
        result=observation_result(),
    )
    with pytest.raises(ApplicationRpcError):
        build_host_tool_result(proposal, observe, is_error=True)


def test_manual_handoff_is_distinct_from_draft_ready_and_requires_state_binding() -> None:
    request = parse_application_request(application_request("run.status"), now_unix_ms=NOW)
    manual = lifecycle_result("manual")
    manual["browser_state"] = "handed_off"
    manual["handoff_committed"] = True
    response = build_application_response(
        request,
        ok=True,
        state="manual",
        action_sequence=0,
        event_sequence=1,
        result=manual,
    )
    assert response["state"] == "manual"
    assert response["result"]["human_review_ready"] is False  # type: ignore[index]
    assert response["result"]["handoff_committed"] is True  # type: ignore[index]

    inconsistent = lifecycle_result("running")
    inconsistent["browser_state"] = "handed_off"
    with pytest.raises(ApplicationRpcError):
        build_application_response(
            request,
            ok=True,
            state="running",
            action_sequence=0,
            event_sequence=1,
            result=inconsistent,
        )
    invalid_manual = lifecycle_result("manual")
    invalid_manual["human_review_ready"] = True
    with pytest.raises(ApplicationRpcError):
        build_application_response(
            request,
            ok=True,
            state="manual",
            action_sequence=0,
            event_sequence=1,
            result=invalid_manual,
        )
    invalid_failed = lifecycle_result("failed")
    invalid_failed["human_review_ready"] = True
    with pytest.raises(ApplicationRpcError):
        build_application_response(
            request,
            ok=True,
            state="failed",
            action_sequence=0,
            event_sequence=1,
            result=invalid_failed,
        )
    missing_failed_reason = lifecycle_result("failed")
    missing_failed_reason["reason_code"] = None
    with pytest.raises(ApplicationRpcError):
        build_application_response(
            request,
            ok=True,
            state="failed",
            action_sequence=0,
            event_sequence=1,
            result=missing_failed_reason,
        )


def test_rejected_response_builder_round_trips_without_echoing_bad_identity() -> None:
    rejected = build_rejected_application_response(
        {
            "protocol_version": 999,
            "request_id": "not-a-uuid",
            "operation": "browser.submit",
            "run_id": "../secret",
            "payload": {"token": "do-not-echo"},
        },
        error="unsupported_operation",
    )
    assert rejected["request_id"] is None
    assert rejected["operation"] is None
    assert rejected["run_id"] is None
    parsed = parse_rejected_application_response(rejected)
    assert parsed["ok"] is False
    assert parsed["error"] == {
        "code": "unsupported_operation",
        "message": "Operation is not supported",
    }
    assert "do-not-echo" not in str(parsed)
    assert parse_application_response(rejected)["ok"] is False
