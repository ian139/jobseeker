from __future__ import annotations

import json
from pathlib import Path
from dataclasses import replace
import pytest

import jobs_assistant.application as app
from jobs_assistant.application_preferences import PreferenceOptOut
from jobs_assistant.application import (
    _configured_and_profile_plan,
    _merge_blocked_target_ids,
    _observation_semantic_signature,
    build_inference_request,
    parse_llm_plan,
    plan_action_evidence,
    resolve_with_llm,
    unresolved_required_fields,
    validate_inference_privacy,
)
from jobs_assistant.ats import (
    ApplicationProfile,
    ConfiguredFieldAnswer,
    GreenhouseAdapter,
    ResumeContext,
    ResumeFacts,
)
from jobs_assistant.contracts import (
    ApplicationContext,
    AutofillPlan,
    FieldAnswer,
    ObservedButton,
    ObservedField,
    ObservedOption,
    PageObservation,
    PublicReasonCode,
)


def field(
    target_id: str = "obs-1:frame-0:field-0",
    *,
    kind: str = "text",
    name: str | None = "question_1234",
    label: str = "",
    value=None,
    required: bool = False,
    visible: bool = True,
    enabled: bool = True,
    readonly: bool = False,
    valid: bool = True,
    validity_flags=(),
    descriptors=(),
    multiple: bool = False,
):
    return ObservedField(
        target_id,
        f"key-{target_id}",
        "frame-0",
        "https://boards.greenhouse.io/fixture/jobs/123",
        None,
        kind,
        name,
        label,
        None,
        None,
        tuple(descriptors),
        "#ignored",
        required,
        visible,
        enabled,
        readonly,
        value,
        True,
        valid,
        tuple(validity_flags),
        0,
        (),
        (),
        None,
        None,
        None,
        None,
        None,
        None,
        (),
        multiple,
    )


def button(
    target_id: str = "button-continue",
    *,
    frame_url: str = "https://boards.greenhouse.io/fixture/jobs/123",
    text: str = "Continue",
    click_key: str = "click-continue",
    visible: bool = True,
    enabled: bool = True,
    descriptors: tuple[str, ...] = (),
) -> ObservedButton:
    return ObservedButton(
        target_id=target_id,
        frame_id="frame-0",
        frame_url=frame_url,
        click_key=click_key,
        element_id=None,
        element_kind="button",
        text=text,
        selector=f"#{target_id}",
        button_type="button",
        name=None,
        value=None,
        target=None,
        download=False,
        effective_action_url=None,
        effective_method=None,
        href_url=None,
        href_attribute=None,
        visible=visible,
        enabled=enabled,
        safety_descriptors=descriptors,
    )


def observation(*fields, final=(), buttons=(), url="https://boards.greenhouse.io/fixture/jobs/123"):
    return PageObservation("obs-1", url, "Apply", (), tuple(fields), tuple(buttons), tuple(final), (), ())


def input_button(
    target_id: str = "input-button-continue",
    *,
    frame_url: str = "https://boards.greenhouse.io/fixture/jobs/123",
    text: str = "Continue",
    value: str | None = None,
    click_key: str = "click-continue",
    button_type: str = "button",
    visible: bool = True,
    enabled: bool = True,
    effective_action_url: str | None = None,
    effective_method: str | None = None,
    descriptors: tuple[str, ...] = (),
) -> ObservedButton:
    return ObservedButton(
        target_id=target_id,
        frame_id="frame-0",
        frame_url=frame_url,
        click_key=click_key,
        element_id=None,
        element_kind="input",
        text=text,
        selector=f"#{target_id}",
        button_type=button_type,
        name=None,
        value=value if value is not None else text,
        target=None,
        download=False,
        effective_action_url=effective_action_url,
        effective_method=effective_method,
        href_url=None,
        href_attribute=None,
        visible=visible,
        enabled=enabled,
        safety_descriptors=descriptors,
    )




def test_exact_llm_schema_and_kind_validation():
    item = field()
    plan = parse_llm_plan({"answers": [{"target_id": item.target_id, "value": "Ada", "confidence": 0.9, "reason": "profile"}], "safe_click_target_id": None}, observation(item))
    assert plan.answers[0].value == "Ada"
    assert plan.answers[0].source == "inference"
    assert parse_llm_plan({"answers": [], "safe_click_target_id": None, "extra": 1}, observation(item)).reason_code is PublicReasonCode.invalid_llm_response


def test_sensitive_and_invalid_inference_are_rejected_before_action():
    item = field(descriptors=("social security number",))
    plan = parse_llm_plan({"answers": [{"target_id": item.target_id, "value": "123", "confidence": 0.99, "reason": "x"}], "safe_click_target_id": None}, observation(item))
    assert plan.answers == ()
    assert plan.reason_code is PublicReasonCode.invalid_llm_response


def test_current_value_is_preserved_and_different_value_is_manual():
    item = field(value="Ada")
    plan = parse_llm_plan({"answers": [{"target_id": item.target_id, "value": "Grace", "confidence": 0.9, "reason": "x"}], "safe_click_target_id": None}, observation(item))
    planned, rejected = plan_action_evidence(observation(item), plan)
    assert planned == []
    assert rejected[0]["reason"] == "preexisting_value_conflict"

def test_inference_request_projects_only_policy_safe_buttons_and_redacts_text() -> None:
    safe = button(
        frame_url="https://jobs.eu.lever.co/acme/123e4567-e89b-12d3-a456-426614174000/apply",
        text="Continue safely",
    )
    final = button(target_id="final", click_key="click-final")
    hidden = button(target_id="hidden", click_key="click-hidden", visible=False)
    sensitive = button(
        target_id="sensitive",
        click_key="click-sensitive",
        descriptors=("start date",),
    )
    wrong_origin = button(
        target_id="wrong-origin",
        click_key="click-wrong",
        frame_url="https://api.lever.co/acme/123",
    )
    request = build_inference_request(
        observation(
            buttons=(safe, final, hidden, sensitive, wrong_origin),
            final=("final",),
            url=safe.frame_url,
        ),
        job={"title": "Engineer"},
        resume_text="resume",
        profile_facts={"private": "safely"},
        ats_policy="lever",
    )
    assert [item["target_id"] for item in request["buttons"]] == ["button-continue"]
    assert request["buttons"][0]["text"] == "Continue [REDACTED]"
    assert "safely" not in json.dumps(request)


def test_parse_and_action_evidence_require_selected_ats_policy_for_safe_click() -> None:
    safe = button(
        frame_url="https://jobs.eu.lever.co/acme/123e4567-e89b-12d3-a456-426614174000/apply",
    )
    observed = observation(buttons=(safe,), url=safe.frame_url)
    payload = {"answers": [], "safe_click_target_id": safe.target_id}
    assert parse_llm_plan(payload, observed).reason_code is PublicReasonCode.invalid_llm_response
    plan = parse_llm_plan(payload, observed, ats_policy="lever")
    assert plan.reason_code is PublicReasonCode.draft_ready
    assert plan.safe_click_target_id == safe.target_id
    assert plan_action_evidence(observed, plan)[0] == []
    planned, rejected = plan_action_evidence(observed, plan, ats_policy="lever")
    assert planned == [{"target_id": safe.target_id, "action": "click", "kind": "button", "source": "inference"}]
    assert rejected == []


def test_button_only_lever_inference_is_resolved(monkeypatch) -> None:
    safe = button(
        frame_url="https://jobs.lever.co/acme/123e4567-e89b-12d3-a456-426614174000/apply",
    )
    observed = observation(buttons=(safe,), url=safe.frame_url)
    calls = []
    monkeypatch.setattr(app.socket, "getaddrinfo", lambda *args, **kwargs: [(2, 1, 6, "", ("8.8.8.8", 443))])

    class Client:
        def __init__(self, **kwargs):
            pass
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass

    monkeypatch.setattr(app.httpx, "Client", Client)
    def client_json(*args, **kwargs):
        calls.append(kwargs["body"] if isinstance(kwargs["body"], dict) else None)
        return {"message": {"role": "assistant", "content": json.dumps({"answers": [], "safe_click_target_id": safe.target_id})}, "done": True}

    monkeypatch.setattr(app, "_client_json", client_json)
    result = resolve_with_llm(
        observed,
        job={"title": "Engineer"},
        resume_context="safe",
        profile_context={},
        api_key="token",
        base_url="https://ollama.example.test",
        ats_policy="lever",
    )
    assert result.reason_code is PublicReasonCode.draft_ready
    assert result.safe_click_target_id == safe.target_id
    assert len(calls) == 1
    body = calls[0]
    assert [message["role"] for message in body["messages"]] == ["system", "user"]
    assert "safe_click_target_id" in body["messages"][0]["content"]
    assert "Engineer" not in body["messages"][0]["content"]
    assert "format" not in body
    assert len(json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) <= app.MAX_REQUEST_BYTES
    projected = json.loads(body["messages"][1]["content"])
    assert projected["job"]["title"] == "Engineer"
    assert projected["fields"] == []
    assert projected["buttons"][0]["target_id"] == safe.target_id


def test_resolver_rejects_body_that_only_exceeds_cap_after_contract(monkeypatch) -> None:
    item = field()
    request = build_inference_request(
        observation(item),
        job={"title": "Engineer"},
        resume_text="safe",
        profile_facts={},
    )
    request_bytes = len(json.dumps(request, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    monkeypatch.setattr(app, "MAX_REQUEST_BYTES", request_bytes + 1)
    monkeypatch.setattr(app.socket, "getaddrinfo", lambda *args, **kwargs: pytest.fail("network must not run"))

    result = resolve_with_llm(
        observation(item),
        job={"title": "Engineer"},
        resume_context="safe",
        api_key="token",
        base_url="https://ollama.example.test",
    )

    assert result.reason_code is PublicReasonCode.inference_context_too_large


def test_native_click_requires_explicit_application_progress_semantics() -> None:
    allowed = (
        button(target_id="continue", click_key="click-continue", text="Continue"),
        button(target_id="next", click_key="click-next", text="Next step"),
        button(target_id="proceed", click_key="click-proceed", text="Proceed"),
        button(target_id="save", click_key="click-save", text="Save and continue"),
    )
    denied = tuple(
        button(target_id=f"denied-{index}", click_key=f"click-denied-{index}", text=text)
        for index, text in enumerate(
            (
                "Apply",
                "Create alert",
                "Quick Apply with MyGreenhouse",
                "Add another",
                "Continue with MyGreenhouse",
                "Sign in to continue",
                "Continue with Google",
                "Continue with LinkedIn",
                "Continue with email",
                "Continue with ExampleID",
                "Continue via Google",
                "Continue using ExampleID",
                "Next with Google",
                "Submit application",
            )
        )
    )
    denied += (
        replace(
            button(target_id="denied-mixed-oauth", click_key="click-denied-mixed-oauth", text="Next"),
            safety_descriptors=("Next", "Continue with Google"),
        ),
        replace(
            button(target_id="denied-mixed-via", click_key="click-denied-mixed-via", text="Next"),
            safety_descriptors=("Next", "Continue via ExampleID"),
        ),
    )
    observed = observation(buttons=(*allowed, *denied))

    eligible = [
        item.target_id
        for item in observed.buttons
        if app._safe_click_is_eligible(
            item,
            ats_policy="greenhouse",
            page_url=observed.url,
        )
    ]
    assert eligible == [item.target_id for item in allowed]
    request = build_inference_request(
        observed,
        job={"title": "Engineer"},
        resume_text="resume",
        profile_facts={},
    )
    assert [item["target_id"] for item in request["buttons"]] == eligible


def test_input_type_button_eligibility_and_descriptor_rejection() -> None:
    safe = input_button(target_id="input-safe")
    submit_input = input_button(target_id="input-submit", button_type="submit", text="Continue")
    final_like = input_button(target_id="input-final", descriptors=("submit",))
    hidden = input_button(target_id="input-hidden", visible=False)
    wrong_origin = input_button(target_id="input-origin", frame_url="https://evil.example/jobs/123")
    with_form_meta = input_button(target_id="input-meta", effective_action_url="https://boards.greenhouse.io/fixture/jobs/123", effective_method="post")
    final = button(target_id="final", click_key="click-final")
    observed = observation(
        buttons=(safe, submit_input, final_like, hidden, wrong_origin, with_form_meta, final),
        final=("final", "input-final"),
        url="https://boards.greenhouse.io/fixture/jobs/123",
    )
    eligible_ids = [
        b.target_id
        for b in observed.buttons
        if app._safe_click_is_eligible(
            b,
            observed.final_submit_target_ids,
            ats_policy="greenhouse",
            page_url=observed.url,
        )
    ]
    assert eligible_ids == [safe.target_id]

    request = build_inference_request(
        observed,
        job={"title": "Engineer"},
        resume_text="resume",
        profile_facts={},
        ats_policy="greenhouse",
    )
    assert [item["target_id"] for item in request["buttons"]] == [safe.target_id]
def test_same_job_anchor_get_is_a_guarded_continuation_only() -> None:
    url = "https://boards.greenhouse.io/fixture/jobs/123"
    anchor = replace(
        button(target_id="apply", text="Apply"),
        element_kind="a",
        button_type="anchor",
        href_url=url,
        href_attribute="/fixture/jobs/123",
    )
    identity = app._application_route_identity(url, "greenhouse")
    assert app._navigation_continuation_permitted(
        anchor,
        (),
        ats_policy="greenhouse",
        page_url=url,
        approved_route_identity=identity,
    )
    assert app._safe_click_is_eligible(anchor, ats_policy="greenhouse", page_url=url)
    child_frame = replace(anchor, frame_id="frame-1")
    assert not app._navigation_continuation_permitted(
        child_frame,
        (),
        ats_policy="greenhouse",
        page_url=url,
        approved_route_identity=identity,
    )
    assert not app._safe_click_is_eligible(child_frame, ats_policy="greenhouse", page_url=url)
    cross_job = replace(anchor, href_url="https://boards.greenhouse.io/other/jobs/999")
    assert not app._navigation_continuation_permitted(
        cross_job,
        (),
        ats_policy="greenhouse",
        page_url=url,
        approved_route_identity=identity,
    )


def test_input_type_button_value_does_not_pollute_field_descriptors() -> None:
    text_field = field(name="first_name", label="First Name", value="Ada")
    input_btn = input_button(value="Next")
    observed = observation(text_field, buttons=(input_btn,))
    request = build_inference_request(
        observed,
        job={"title": "Engineer"},
        resume_text="resume",
        profile_facts={"first_name": "Ada"},
        ats_policy="greenhouse",
    )
    assert [item["target_id"] for item in request["buttons"]] == [input_btn.target_id]
    assert "Next" not in json.dumps(request["fields"])
    assert "Ada" not in json.dumps(request["fields"])


def test_inference_request_is_target_scoped_and_redacted():
    item = field()
    request = build_inference_request(observation(item), job={"title": "Engineer", "company": "Acme", "email": "secret@example.test"}, resume_text="Ada Lovelace\nAda ada@example.test\nEngineer", profile_facts={"email": "ada@example.test"}, job_description="Work", applicant_description="Applicant")
    encoded = json.dumps(request)
    assert item.target_id in encoded
    assert "ada@example.test" not in encoded
    assert "secret@example.test" not in encoded
    assert "value" not in request["fields"][0]

def test_inference_request_keeps_listing_and_applicant_descriptions_separate():
    item = field()
    request = build_inference_request(
        observation(item),
        job={"title": "Engineer", "description": "ROW_DESCRIPTION"},
        resume_text="resume",
        profile_facts={},
        job_description="JOB_DESCRIPTION",
        applicant_description="APPLICANT_DESCRIPTION",
    )
    assert request["job"]["description"] == "JOB_DESCRIPTION"
    assert request["context"]["description"] == "APPLICANT_DESCRIPTION"


def test_inference_request_description_fallbacks_are_independent():
    item = field()
    from_row = build_inference_request(
        observation(item),
        job={"description": "ROW_DESCRIPTION"},
        resume_text="resume",
        profile_facts={},
        job_description=None,
    )
    assert from_row["job"]["description"] == "ROW_DESCRIPTION"
    assert from_row["context"]["description"] == ""

    from_explicit = build_inference_request(
        observation(item),
        job={},
        resume_text="resume",
        profile_facts={},
        job_description="JOB_DESCRIPTION",
    )
    assert from_explicit["job"]["description"] == "JOB_DESCRIPTION"
    assert from_explicit["context"]["description"] == ""


def test_inference_request_redacts_and_caps_each_description():
    item = field()
    request = build_inference_request(
        observation(item),
        job={"description": "listing ada@example.test"},
        resume_text="resume",
        profile_facts={"email": "ada@example.test"},
        job_description="listing ada@example.test",
        applicant_description="applicant ada@example.test",
    )
    assert request["job"]["description"] == "listing [REDACTED]"
    assert request["context"]["description"] == "applicant [REDACTED]"

    with pytest.raises(ValueError, match="inference_context_too_large"):
        build_inference_request(
            observation(item),
            job={},
            resume_text="resume",
            profile_facts={},
            job_description="j" * app.MAX_REQUEST_BYTES,
        )
    with pytest.raises(ValueError, match="inference_context_too_large"):
        build_inference_request(
            observation(item),
            job={},
            resume_text="resume",
            profile_facts={},
            applicant_description="a" * app.MAX_REQUEST_BYTES,
        )


def test_inference_projection_redacts_nested_values_from_field_metadata() -> None:
    item = replace(
        field(name="secret-token", label="Secret token"),
        safety_descriptors=("secret-token descriptor",),
        options=(ObservedOption("secret-token", "secret label", True),),
        pattern="secret-token",
    )
    request = build_inference_request(
        observation(item),
        job={"title": "Engineer"},
        resume_text="safe",
        profile_facts={"nested": {"token": "secret-token"}},
    )
    encoded = json.dumps(request)
    assert "secret-token" not in encoded
    assert request["fields"][0]["target_id"] == item.target_id


def test_inference_privacy_rejects_one_character_exact_sensitive_values_only() -> None:
    item = field()
    exact = parse_llm_plan(
        {"answers": [{"target_id": item.target_id, "value": "x", "confidence": 0.9, "reason": "x"}], "safe_click_target_id": None},
        observation(item),
    )
    assert not validate_inference_privacy(exact, protected_values=("x",))
    longer = parse_llm_plan(
        {"answers": [{"target_id": item.target_id, "value": "xx", "confidence": 0.9, "reason": "x"}], "safe_click_target_id": None},
        observation(item),
    )
    assert validate_inference_privacy(longer, protected_values=("x",))

def test_inference_uses_one_prevalidated_address_with_hostname_sni(monkeypatch) -> None:
    item = field()
    observed = {}
    monkeypatch.setattr(
        app.socket,
        "getaddrinfo",
        lambda *args, **kwargs: [(2, 1, 6, "", ("8.8.8.8", 443))],
    )

    class Client:
        def __init__(self, **kwargs):
            observed["transport"] = kwargs["transport"]
        def __enter__(self):
            return self
        def __exit__(self, *args):
            observed["closed"] = True

    monkeypatch.setattr(app.httpx, "Client", Client)
    monkeypatch.setattr(app, "_client_json", lambda *args, **kwargs: {"answers": [], "safe_click_target_id": None})
    result = resolve_with_llm(
        observation(item),
        job={"title": "Engineer"},
        resume_context="safe",
        profile_context={},
        api_key="token",
        base_url="https://ollama.example.test",
    )
    assert result.reason_code is PublicReasonCode.no_deterministic_next_step
    assert observed["transport"]._address == "8.8.8.8"
    assert observed["transport"]._hostname == "ollama.example.test"


def test_missing_key_is_manual_without_http_call(monkeypatch):
    monkeypatch.delenv("OLLAMA_API_KEY", raising=False)
    monkeypatch.delenv("OLLAMA_CLOUD_API_KEY", raising=False)
    result = resolve_with_llm(observation(field()), job={"title": "Engineer"}, resume_context="safe context", api_key=None)
    assert result.reason_code is PublicReasonCode.missing_llm_api_key


def test_privacy_rejects_copying_protected_identifier_and_long_resume_span():
    plan = parse_llm_plan({"answers": [{"target_id": field().target_id, "value": "ada@example.test", "confidence": 0.9, "reason": "x"}], "safe_click_target_id": None}, observation(field()))
    assert validate_inference_privacy(plan, protected_values=("ada@example.test",), source_text="") is False
    long = "one two three four five six seven eight nine ten eleven twelve"
    copied = parse_llm_plan({"answers": [{"target_id": field().target_id, "value": long, "confidence": 0.9, "reason": "x"}], "safe_click_target_id": None}, observation(field()))
    assert validate_inference_privacy(copied, protected_values=(), source_text=long) is False


def test_inference_privacy_checks_each_multi_select_item_independently() -> None:
    target_id = field().target_id
    protected_item = AutofillPlan(
        answers=(FieldAnswer(target_id, ("safe", "ada@example.test"), 0.9, "x", "inference"),),
        status="ready",
        reason_code=PublicReasonCode.draft_ready,
    )
    assert not validate_inference_privacy(protected_item, protected_values=("ada@example.test",))

    copied_value = "one two three four five six seven eight nine ten eleven twelve"
    copied_item = AutofillPlan(
        answers=(FieldAnswer(target_id, ("safe", copied_value), 0.9, "x", "inference"),),
        status="ready",
        reason_code=PublicReasonCode.draft_ready,
    )
    assert not validate_inference_privacy(copied_item, protected_values=(), source_text=copied_value)

def test_resolve_privacy_flattens_configured_multi_values(monkeypatch) -> None:
    target = field(name="nickname", label="Nickname")
    profile = ApplicationProfile(field_answers=(
        ConfiguredFieldAnswer("greenhouse", "skills", None, "select", ("python", "go")),
    ))
    monkeypatch.setattr(app.socket, "getaddrinfo", lambda *args, **kwargs: [(2, 1, 6, "", ("8.8.8.8", 443))])

    class Client:
        def __init__(self, **kwargs):
            pass
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass

    monkeypatch.setattr(app.httpx, "Client", Client)
    monkeypatch.setattr(
        app,
        "_client_json",
        lambda *args, **kwargs: {
            "answers": [{
                "target_id": target.target_id,
                "value": "go",
                "confidence": 0.9,
                "reason": "copied",
            }],
            "safe_click_target_id": None,
        },
    )
    result = resolve_with_llm(
        observation(target),
        job={"title": "Engineer"},
        resume_context="safe",
        profile_context=profile,
        api_key="token",
        base_url="https://ollama.example.test",
    )
    assert result.reason_code is PublicReasonCode.inference_privacy_violation


def test_llm_parser_rejects_non_current_or_sensitive_fields():
    payload_for = lambda item: {
        "answers": [{"target_id": item.target_id, "value": "Ada", "confidence": 0.9, "reason": "x"}],
        "safe_click_target_id": None,
    }
    for kwargs in (
        {"visible": False},
        {"enabled": False},
        {"readonly": True},
        {"descriptors": ("social security number",)},
    ):
        item = field(**kwargs)
        plan = parse_llm_plan(payload_for(item), observation(item))
        assert plan.answers == ()
        assert plan.reason_code is PublicReasonCode.invalid_llm_response

    canonical = field(name="first_name", label="First Name")
    canonical_plan = parse_llm_plan(payload_for(canonical), observation(canonical))
    assert canonical_plan.reason_code is PublicReasonCode.invalid_llm_response
    collision = field(validity_flags=("field_identity_collision",))
    collision_plan = parse_llm_plan(payload_for(collision), observation(collision))
    assert collision_plan.reason_code is PublicReasonCode.invalid_llm_response


def test_llm_parser_rejects_unknown_duplicate_and_invalid_targets():
    item = field()
    unknown = parse_llm_plan(
        {"answers": [{"target_id": "obs-unknown", "value": "Ada", "confidence": 0.9, "reason": "x"}], "safe_click_target_id": None},
        observation(item),
    )
    assert unknown.reason_code is PublicReasonCode.invalid_llm_response
    duplicate_item = {"target_id": item.target_id, "value": "Ada", "confidence": 0.9, "reason": "x"}
    duplicate = parse_llm_plan(
        {"answers": [duplicate_item, dict(duplicate_item)], "safe_click_target_id": None},
        observation(item),
    )
    assert duplicate.reason_code is PublicReasonCode.invalid_llm_response
    email = field(kind="email", name="question_1234", label="")
    invalid = parse_llm_plan(
        {"answers": [{"target_id": email.target_id, "value": "not-an-email", "confidence": 0.9, "reason": "x"}], "safe_click_target_id": None},
        observation(email),
    )
    assert invalid.reason_code is PublicReasonCode.invalid_llm_response


def test_action_evidence_rejects_ineligible_inference_without_planning():
    item = field(visible=False)
    plan = AutofillPlan(
        answers=(FieldAnswer(item.target_id, "Ada", 0.9, "x", "inference"),),
    )
    planned, rejected = plan_action_evidence(observation(item), plan)
    assert planned == []
    assert rejected[0]["reason"] == "ineligible_field"


def test_action_evidence_rejects_canonical_and_collision_tombstones():
    for item in (
        field(name="first_name", label="First Name"),
        field(validity_flags=("field_identity_collision",)),
    ):
        plan = AutofillPlan(
            answers=(FieldAnswer(item.target_id, "Ada", 0.9, "x", "inference"),),
        )
        planned, rejected = plan_action_evidence(observation(item), plan)
        assert planned == []
        assert rejected[0]["reason"] == "tombstoned_target"


def test_prefilled_valid_required_field_is_resolved():
    item = field(value="Ada", required=True)
    assert unresolved_required_fields(observation(item), ()) == ()


def test_configured_conflict_tombstone_blocks_llm_merge():
    item = field(required=True, name="question_1234", label="")
    profile = ApplicationProfile(
        field_answers=(
            ConfiguredFieldAnswer("greenhouse", "question_1234", None, "text", "Ada"),
            ConfiguredFieldAnswer("greenhouse", "question_1234", None, "text", "Grace"),
        )
    )
    resume = ResumeContext("resume.txt", "text/plain", "", "0" * 64, -1, facts=ResumeFacts())
    deterministic = _configured_and_profile_plan(
        observation(item),
        adapter=GreenhouseAdapter(),
        context=ApplicationContext(),
        profile=profile,
        resume=resume,
    )
    blocked = _merge_blocked_target_ids(observation(item), deterministic, profile=profile, resume=resume)
    assert item.target_id in blocked
    llm = parse_llm_plan(
        {"answers": [{"target_id": item.target_id, "value": "Ada", "confidence": 0.9, "reason": "x"}], "safe_click_target_id": None},
        observation(item),
    )
    merged = tuple(answer for answer in llm.answers if answer.target_id not in blocked)
    assert merged == ()


def test_hidden_sensitive_required_field_does_not_force_manual_reason():
    item = field(required=True, visible=False, descriptors=("social security number",))
    resume = ResumeContext("resume.txt", "text/plain", "", "0" * 64, -1, facts=ResumeFacts())
    plan = _configured_and_profile_plan(
        observation(item),
        adapter=GreenhouseAdapter(),
        context=ApplicationContext(),
        profile=ApplicationProfile(),
        resume=resume,
    )
    assert plan.reason_code is not PublicReasonCode.required_sensitive_fields_manual


@pytest.mark.parametrize(
    ("kwargs", "blocks_page_validation"),
    (
        ({"valid": False, "visible": False, "validity_flags": ("field_identity_collision",)}, False),
        ({"valid": False, "enabled": False, "validity_flags": ("customError",)}, False),
        ({"valid": False, "readonly": True, "validity_flags": ("customError",)}, False),
        ({"valid": False, "validity_flags": ("valueMissing",)}, True),
        ({"valid": False, "required": True, "value": None, "validity_flags": ("valueMissing",)}, False),
    ),
)
def test_configured_plan_uses_live_field_validation_activity(
    kwargs: dict[str, object],
    blocks_page_validation: bool,
) -> None:
    item = field(**kwargs)
    resume = ResumeContext("resume.txt", "text/plain", "", "0" * 64, -1, facts=ResumeFacts())

    assert app._field_blocks_page_validation(item) is blocks_page_validation
    plan = _configured_and_profile_plan(
        observation(item),
        adapter=GreenhouseAdapter(),
        context=ApplicationContext(),
        profile=ApplicationProfile(),
        resume=resume,
    )
    assert (plan.reason_code is PublicReasonCode.page_validation_error) is blocks_page_validation
    if not item.visible or not item.enabled or item.readonly:
        assert not app._field_is_llm_eligible(item)

def test_optional_value_missing_is_not_exempt_from_page_validation() -> None:
    item = field(valid=False, validity_flags=("valueMissing",), value=None, required=False)
    resume = ResumeContext("resume.txt", "text/plain", "", "0" * 64, -1, facts=ResumeFacts())
    plan = _configured_and_profile_plan(
        observation(item),
        adapter=GreenhouseAdapter(),
        context=ApplicationContext(),
        profile=ApplicationProfile(),
        resume=resume,
    )
    assert plan.reason_code is PublicReasonCode.page_validation_error


def test_identity_collision_flag_is_a_manual_tombstone():
    item = field(
        required=True,
        name="question_1234",
        label="",
        validity_flags=("field_identity_collision",),
    )
    resume = ResumeContext("resume.txt", "text/plain", "", "0" * 64, -1, facts=ResumeFacts())
    deterministic = _configured_and_profile_plan(
        observation(item),
        adapter=GreenhouseAdapter(),
        context=ApplicationContext(),
        profile=ApplicationProfile(),
        resume=resume,
    )
    blocked = _merge_blocked_target_ids(observation(item), deterministic, profile=ApplicationProfile(), resume=resume)
    assert item.target_id in blocked


def test_action_evidence_allows_deterministic_canonical_answer():
    item = field(name="first_name", label="First Name")
    plan = AutofillPlan(
        answers=(FieldAnswer(item.target_id, "Ada", 1.0, "profile", "profile"),),
    )
    planned, rejected = plan_action_evidence(observation(item), plan)
    assert planned and planned[0]["target_id"] == item.target_id
    assert rejected == []

def test_observation_snapshot_digest_includes_generation_and_selector_fields() -> None:
    first = observation(field(target_id="generation-a"))
    changed_generation = observation(field(target_id="generation-b"))
    changed_selector = observation(replace(first.fields[0], selector="#generation-b"))

    snapshot = app._observation_snapshot(first)
    assert snapshot["observation_id"] == first.observation_id
    assert snapshot["fields"][0]["target_id"] == "generation-a"
    assert snapshot["fields"][0]["selector"] == "#ignored"
    assert app._observation_snapshot_sha256(first) != app._observation_snapshot_sha256(changed_generation)
    assert app._observation_snapshot_sha256(first) != app._observation_snapshot_sha256(changed_selector)

def test_readiness_signature_rejects_validity_safety_and_enabled_changes() -> None:
    baseline = observation(field(valid=True, enabled=True, descriptors=("safe",)))
    disabled = observation(field(valid=True, enabled=False, descriptors=("safe",)))
    unsafe = observation(field(valid=False, enabled=True, descriptors=("social security number",)))
    assert _observation_semantic_signature(baseline) != _observation_semantic_signature(disabled)
    assert _observation_semantic_signature(baseline) != _observation_semantic_signature(unsafe)
    first = replace(
        baseline,
        buttons=(button("safe-a"), button("final-a", text="Submit", click_key="click-final")),
        final_submit_target_ids=("final-a",),
    )
    renamed = replace(
        baseline,
        buttons=(button("safe-b"), button("final-b", text="Submit", click_key="click-final")),
        final_submit_target_ids=("final-b",),
    )
    assert _observation_semantic_signature(first) == _observation_semantic_signature(renamed)
    swapped = replace(renamed, final_submit_target_ids=("safe-b",))
    assert _observation_semantic_signature(first) != _observation_semantic_signature(swapped)


def test_observation_payload_rejects_multi_select_value_without_multiple() -> None:
    payload = {
        "observation_id": "obs-1",
        "url": "https://boards.greenhouse.io/fixture/jobs/123",
        "title": "Apply",
        "site_markers": [],
        "fields": [{
            "target_id": "skills",
            "field_key": "skills",
            "kind": "select",
            "label": "Skills",
            "value": ["python", "go"],
            "options": [
                {"value": "python", "label": "Python", "enabled": True},
                {"value": "go", "label": "Go", "enabled": True},
            ],
        }],
        "buttons": [],
        "final_submit_target_ids": [],
        "errors": [],
        "blockers": [],
    }
    with pytest.raises(Exception):
        app._observation_from_payload(payload)


def test_observation_payload_accepts_multi_select_list_and_rejects_scalar() -> None:
    single = {
        "observation_id": "obs-1",
        "url": "https://boards.greenhouse.io/fixture/jobs/123",
        "title": "Apply",
        "site_markers": [],
        "fields": [{
            "target_id": "skills",
            "field_key": "skills",
            "kind": "select",
            "label": "Skills",
            "multiple": True,
            "value": "python",
            "options": [{"value": "python", "label": "Python", "enabled": True}],
        }],
        "buttons": [],
        "final_submit_target_ids": [],
        "errors": [],
        "blockers": [],
    }
    with pytest.raises(Exception):
        app._observation_from_payload(single)

    multi = {
        **single,
        "fields": [{
            "target_id": "skills",
            "field_key": "skills",
            "kind": "select",
            "label": "Skills",
            "multiple": True,
            "value": ["python", "go"],
            "options": [
                {"value": "python", "label": "Python", "enabled": True},
                {"value": "go", "label": "Go", "enabled": True},
            ],
        }],
    }
    parsed = app._observation_from_payload(multi)
    assert parsed.fields[0].value == ("python", "go")
    assert parsed.fields[0].multiple is True


def test_observation_payload_rejects_non_select_list_value() -> None:
    payload = {
        "observation_id": "obs-1",
        "url": "https://boards.greenhouse.io/fixture/jobs/123",
        "title": "Apply",
        "site_markers": [],
        "fields": [{
            "target_id": "name",
            "field_key": "name",
            "kind": "text",
            "label": "Name",
            "value": ["Ada"],
        }],
        "buttons": [],
        "final_submit_target_ids": [],
        "errors": [],
        "blockers": [],
    }
    with pytest.raises(Exception):
        app._observation_from_payload(payload)


def test_observation_snapshot_persists_multiple_and_tuple_value_as_array() -> None:
    item = field(kind="select", target_id="skills", label="Skills", multiple=True, value=("go", "python"))
    item = replace(item, options=(ObservedOption("python", "Python", True), ObservedOption("go", "Go", True)))
    obs = observation(item)
    snapshot = app._observation_snapshot(obs)
    assert snapshot["fields"][0]["multiple"] is True
    assert snapshot["fields"][0]["value"] == ("go", "python")
    encoded = json.dumps(snapshot, sort_keys=True)
    assert "\"value\": [\"go\", \"python\"]" in encoded


def test_llm_parser_accepts_multi_select_list_and_rejects_mismatches() -> None:
    item = field(kind="select", target_id="skills", label="Skills", multiple=True, value=None)
    item = replace(item, options=(ObservedOption("python", "Python", True), ObservedOption("go", "Go", True)))
    obs = observation(item)
    plan = parse_llm_plan(
        {"answers": [{"target_id": item.target_id, "value": ["go", "python"], "confidence": 0.9, "reason": "x"}], "safe_click_target_id": None},
        obs,
    )
    assert plan.status == "ready"
    assert plan.answers[0].value == ("python", "go")

    scalar = parse_llm_plan(
        {"answers": [{"target_id": item.target_id, "value": "python", "confidence": 0.9, "reason": "x"}], "safe_click_target_id": None},
        obs,
    )
    assert scalar.status == "manual"

    single = replace(item, multiple=False)
    list_for_single = parse_llm_plan(
        {"answers": [{"target_id": single.target_id, "value": ["python"], "confidence": 0.9, "reason": "x"}], "safe_click_target_id": None},
        observation(single),
    )
    assert list_for_single.status == "manual"


def test_llm_multi_select_rejects_non_string_items_before_normalization() -> None:
    item = field(kind="select", target_id="numeric-option", label="Skills", multiple=True, value=None)
    item = replace(item, options=(ObservedOption("1", "One", True),))
    plan = parse_llm_plan(
        {
            "answers": [{"target_id": item.target_id, "value": [1], "confidence": 0.9, "reason": "x"}],
            "safe_click_target_id": None,
        },
        observation(item),
    )
    assert plan.status == "manual"


def test_observation_payload_enforces_select_mode_metadata() -> None:
    base = {
        "observation_id": "obs-1",
        "url": "https://boards.greenhouse.io/fixture/jobs/123",
        "title": "Apply",
        "site_markers": [],
        "buttons": [],
        "final_submit_target_ids": [],
        "errors": [],
        "blockers": [],
    }
    scalar_select = {
        **base,
        "fields": [{
            "target_id": "skill",
            "field_key": "skill",
            "kind": "select",
            "value": "python",
            "options": [{"value": "python", "label": "Python", "enabled": True}],
        }],
    }
    assert app._observation_from_payload(scalar_select).fields[0].multiple is False
    for field_payload in (
        {
            "target_id": "skill",
            "field_key": "skill",
            "kind": "select",
            "multiple": False,

            "options": [{"value": "python", "label": "Python", "enabled": True}],
        },
        {
            "target_id": "skill",
            "field_key": "skill",
            "kind": "select",
            "multiple": True,
            "value": None,
            "options": [{"value": "python", "label": "Python", "enabled": True}],
        },
        {
            "target_id": "name",
            "field_key": "name",
            "kind": "text",
            "multiple": True,
            "value": "Ada",
        },
        {
            "target_id": "skill",
            "field_key": "skill",
            "kind": "select",
            "multiple": 1,
            "value": [],
        },
    ):
        with pytest.raises(Exception):
            app._observation_from_payload({**base, "fields": [field_payload]})
    empty_multi = {
        **base,
        "fields": [{
            "target_id": "skill",
            "field_key": "skill",
            "kind": "select",
            "multiple": True,
            "value": [],
            "options": [{"value": "python", "label": "Python", "enabled": True}],
        }],
    }
    assert app._observation_from_payload(empty_multi).fields[0].value == ()

@pytest.mark.parametrize(
    "field_payload",
    (
        {
            "target_id": "skills", "field_key": "skills", "kind": "SeLeCt",
            "multiple": True, "value": [], "options": [],
        },
        {
            "target_id": "skills", "field_key": "skills", "kind": "select",
            "multiple": True, "value": ["a"],
            "options": [
                {"value": "a", "label": "A", "enabled": True},
                {"value": "a", "label": "A2", "enabled": True},
            ],
        },
        {
            "target_id": "skills", "field_key": "skills", "kind": "select",
            "multiple": True, "value": ["z"],
            "options": [{"value": "a", "label": "A", "enabled": True}],
        },
        {
            "target_id": "skills", "field_key": "skills", "kind": "select",
            "multiple": True, "value": ["b"],
            "options": [
                {"value": "a", "label": "A", "enabled": True},
                {"value": "b", "label": "B", "enabled": False},
            ],
        },
        {
            "target_id": "skills", "field_key": "skills", "kind": "select",
            "multiple": True, "value": ["a", "a"],
            "options": [{"value": "a", "label": "A", "enabled": True}],
        },
        {
            "target_id": "skills", "field_key": "skills", "kind": "select",
            "multiple": True, "value": ["b", "a"],
            "options": [
                {"value": "a", "label": "A", "enabled": True},
                {"value": "b", "label": "B", "enabled": True},
            ],
        },
        {
            "target_id": "skill", "field_key": "skill", "kind": "select",
            "value": "b",
            "options": [
                {"value": "a", "label": "A", "enabled": True},
                {"value": "b", "label": "B", "enabled": False},
            ],
        },
    ),
)
def test_observation_payload_rejects_noncanonical_or_invalid_select_state(field_payload) -> None:
    payload = {
        "observation_id": "obs-1",
        "url": "https://boards.greenhouse.io/fixture/jobs/123",
        "title": "Apply",
        "site_markers": [],
        "fields": [field_payload],
        "buttons": [],
        "final_submit_target_ids": [],
        "errors": [],
        "blockers": [],
    }
    with pytest.raises(app.BrowserAdapterError, match="protocol_invalid_response"):
        app._observation_from_payload(payload)

def test_observation_payload_preserves_invalid_select_evidence_with_flags() -> None:
    base = {
        "observation_id": "obs-1",
        "url": "https://boards.greenhouse.io/fixture/jobs/123",
        "title": "Apply",
        "site_markers": [],
        "buttons": [],
        "final_submit_target_ids": [],
        "errors": [],
        "blockers": [],
    }
    ambiguous = {
        **base,
        "fields": [{
            "target_id": "skills",
            "field_key": "skills",
            "kind": "select",
            "multiple": True,
            "valid": False,
            "validity_flags": ["options_ambiguous"],
            "value": ["a", "a"],
            "options": [
                {"value": "a", "label": "A", "enabled": True},
                {"value": "a", "label": "A2", "enabled": True},
            ],
        }],
    }
    parsed = app._observation_from_payload(ambiguous).fields[0]
    assert parsed.valid is False
    assert parsed.value == ("a", "a")

    invalid_selection = {
        **base,
        "fields": [{
            "target_id": "skills",
            "field_key": "skills",
            "kind": "select",
            "multiple": True,
            "valid": False,
            "validity_flags": ["invalid_selected_option"],
            "value": ["unknown"],
            "options": [{"value": "a", "label": "A", "enabled": True}],
        }],
    }
    parsed = app._observation_from_payload(invalid_selection).fields[0]
    assert parsed.validity_flags == ("invalid_selected_option",)


def test_action_evidence_rejects_noncanonical_multi_select_and_preserves_existing() -> None:
    item = field(kind="select", target_id="skills", label="Skills", multiple=True, value=None)
    item = replace(item, options=(ObservedOption("python", "Python", True), ObservedOption("go", "Go", True)))
    plan = AutofillPlan(
        answers=(FieldAnswer(item.target_id, ("rust",), 1.0, "x", "configured"),),
        status="ready",
        reason_code=PublicReasonCode.draft_ready,
    )
    planned, rejected = plan_action_evidence(observation(item), plan)
    assert rejected and rejected[0]["reason"] == "invalid_value"

    existing = replace(item, value=("python", "go"))
    same_plan = AutofillPlan(
        answers=(FieldAnswer(existing.target_id, ("python", "go"), 1.0, "x", "configured"),),
        status="ready",
        reason_code=PublicReasonCode.draft_ready,
    )
    planned, rejected = plan_action_evidence(observation(existing), same_plan)
    assert not planned and not rejected


def test_inference_request_advertises_multiple_for_multi_select() -> None:
    item = field(kind="select", target_id="skills", label="Skills", multiple=True, value=None)
    item = replace(item, options=(ObservedOption("python", "Python", True), ObservedOption("go", "Go", True)))
    request = build_inference_request(
        observation(item),
        job={"title": "x"},
        resume_text="",
        profile_facts={},
    )
    field_payload = request["fields"][0]
    assert field_payload["multiple"] is True
    assert "enabled" in field_payload["options"][0]
    assert request["answers"] == []

def test_empty_preferences_preserve_profile_answers_around_filtered_controls() -> None:
    fields = (
        field(target_id="first", name="first_name", label="First Name", required=True),
        field(target_id="last", name="last_name", label="Last Name", required=True),
        field(target_id="email", kind="email", name="email", label="Email", required=True),
        field(target_id="phone", kind="tel", name="phone", label="Phone", required=True),
        field(
            target_id="hidden-collision",
            name="question_1234",
            label="Name",
            required=True,
            visible=False,
            valid=False,
            validity_flags=("field_identity_collision",),
        ),
        field(target_id="opaque", name="question_5678", label="Question 5678"),
        field(target_id="sensitive", name="ssn", label="Social Security Number"),
        field(target_id="unsupported", kind="password", name="password", label="Password"),
        field(target_id="resume", kind="file", name="resume", label="Resume"),
    )
    observed = observation(*fields)
    profile = ApplicationProfile(
        facts={
            "first_name": "Ada",
            "last_name": "Lovelace",
            "email": "ada@example.test",
            "phone": "+1 555 0100",
        }
    )
    resume = ResumeContext("resume.txt", "text/plain", "", "0" * 64, -1, facts=ResumeFacts())
    adapter = GreenhouseAdapter()
    context = ApplicationContext()

    without_preferences = _configured_and_profile_plan(
        observed,
        adapter=adapter,
        context=context,
        profile=profile,
        resume=resume,
    )
    with_empty_preferences = _configured_and_profile_plan(
        observed,
        adapter=adapter,
        context=context,
        profile=profile,
        resume=resume,
        preferences=app.ApplicationPreferences(1, (), (), ()),
    )

    expected = {
        "first": "Ada",
        "last": "Lovelace",
        "email": "ada@example.test",
        "phone": "+1 555 0100",
    }
    assert {answer.target_id: answer.value for answer in without_preferences.answers} == expected
    assert with_empty_preferences.answers == without_preferences.answers
    assert with_empty_preferences.status == without_preferences.status == "ready"

def test_required_empty_value_missing_optout_remains_fail_closed() -> None:
    item = field(
        target_id="required-first",
        name="first_name",
        label="First Name",
        required=True,
        value=None,
        valid=False,
        validity_flags=("valueMissing",),
    )
    profile = ApplicationProfile(facts={"first_name": "Ada"})
    resume = ResumeContext("resume.txt", "text/plain", "", "0" * 64, -1, facts=ResumeFacts())
    preferences = app.ApplicationPreferences(
        1,
        (),
        (PreferenceOptOut("greenhouse", "first_name", None, "text"),),
        (),
    )

    plan = _configured_and_profile_plan(
        observation(item),
        adapter=GreenhouseAdapter(),
        context=ApplicationContext(),
        profile=profile,
        resume=resume,
        preferences=preferences,
    )

    assert plan.answers == ()
    assert plan.status == "manual"
    assert plan.reason_code is PublicReasonCode.required_safe_fields_unresolved
    assert item.target_id in plan.skipped_target_ids