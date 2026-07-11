from __future__ import annotations

import json
from pathlib import Path
from dataclasses import replace
import pytest

import jobs_assistant.application as app
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
    )


def observation(*fields, final=()):
    return PageObservation("obs-1", "https://boards.greenhouse.io/fixture/jobs/123", "Apply", (), tuple(fields), (), tuple(final), (), ())


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

def test_readiness_signature_rejects_validity_safety_and_enabled_changes() -> None:
    baseline = observation(field(valid=True, enabled=True, descriptors=("safe",)))
    disabled = observation(field(valid=True, enabled=False, descriptors=("safe",)))
    unsafe = observation(field(valid=False, enabled=True, descriptors=("social security number",)))
    assert _observation_semantic_signature(baseline) != _observation_semantic_signature(disabled)
    assert _observation_semantic_signature(baseline) != _observation_semantic_signature(unsafe)
