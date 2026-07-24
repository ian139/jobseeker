from __future__ import annotations

from dataclasses import FrozenInstanceError, fields, replace
from importlib import resources
from types import MappingProxyType

import pytest

from jobs_assistant.contracts import (
    ApplicationClaim,
    ApplicationContext,
    AutofillPlan,
    FieldAnswer,
    ObservedBlocker,
    ObservedButton,
    ObservedField,
    ObservedOption,
    ObservedValidationError,
    PageObservation,
    PublicReasonCode,
    freeze_json,
    thaw_json,
)
from jobs_assistant.safety import (
    DescriptorLimitError,
    DescriptorSafety,
    SafetyPolicy,
    ats_route_parity_vectors,
    classify_ats_form_action,
    classify_ats_request,
    classify_ats_route_vector,
    classify_ats_url,
    classify_descriptors,
    classify_greenhouse_form_action,
    classify_greenhouse_request,
    classify_greenhouse_route_vector,
    classify_greenhouse_url,
    greenhouse_route_parity_vectors,
    is_greenhouse_interactive_origin,
    load_greenhouse_route_policy,
    load_safety_policy,
    normalize_descriptor,
)


def test_json_freeze_thaw_recurses_and_is_immutable() -> None:
    frozen = freeze_json({"a": [1, {"b": True}], "c": None})

    assert isinstance(frozen, MappingProxyType)
    assert isinstance(frozen["a"], tuple)
    assert isinstance(frozen["a"][1], MappingProxyType)
    assert thaw_json(frozen) == {"a": [1, {"b": True}], "c": None}

    with pytest.raises(TypeError):
        frozen["x"] = 1  # type: ignore[index]


def test_freeze_json_rejects_non_json_values() -> None:
    with pytest.raises(TypeError):
        freeze_json({"bad": object()})

    with pytest.raises(TypeError):
        freeze_json({"bad": {1, 2}})


def test_application_claim_deep_freezes_complete_row_snapshot() -> None:
    raw = {"id": 7, "raw": {"score": 1}, "tags": ["python"]}
    claim = ApplicationClaim(run_id=11, job=raw)
    raw["raw"]["score"] = 99  # type: ignore[index]
    raw["tags"].append("changed")  # type: ignore[union-attr]

    assert claim.job["raw"]["score"] == 1  # type: ignore[index]
    assert claim.job["tags"] == ("python",)
    with pytest.raises(TypeError):
        claim.job["id"] = 8  # type: ignore[index]


def test_public_reason_code_exact_members_round_trip() -> None:
    expected = (
        "draft_ready",
        "required_safe_fields_unresolved",
        "required_sensitive_fields_manual",
        "no_deterministic_next_step",
        "profile_field_conflict",
        "field_identity_collision",
        "preexisting_value_conflict",
        "field_value_not_retained",
        "page_validation_error",
        "page_not_stable",
        "missing_llm_api_key",
        "invalid_llm_response",
        "llm_request_failed",
        "inference_context_too_large",
        "inference_privacy_violation",
        "unsupported_ats",
        "ats_mismatch",
        "invalid_application_url",
        "unsafe_navigation_target",
        "unsafe_network_attempt",
        "observation_too_large",
        "captcha",
        "authentication_required",
        "assessment_required",
        "unsupported_frame",
        "safe_click_no_progress",
        "iteration_limit",
        "artifact_error",
        "browser_error",
        "database_error",
        "handoff_failed",
        "abandoned_running_attempt",
        "legacy_run",
    )

    assert tuple(code.value for code in PublicReasonCode) == expected
    assert tuple(PublicReasonCode(value).value for value in expected) == expected


def test_observation_dataclass_field_shapes_and_booleans() -> None:
    assert tuple(field.name for field in fields(ObservedOption)) == ("value", "label", "enabled")
    assert tuple(field.name for field in fields(ObservedField)) == (
        "target_id",
        "field_key",
        "frame_id",
        "frame_url",
        "form_action_url",
        "kind",
        "name",
        "label",
        "group_id",
        "option_value",
        "safety_descriptors",
        "selector",
        "required",
        "visible",
        "enabled",
        "readonly",
        "value",
        "will_validate",
        "valid",
        "validity_flags",
        "file_count",
        "file_basenames",
        "accept",
        "min_length",
        "max_length",
        "pattern",
        "min_value",
        "max_value",
        "step",
        "options",
        "multiple",
    )
    assert tuple(field.name for field in fields(ObservedButton)) == (
        "target_id",
        "frame_id",
        "frame_url",
        "click_key",
        "element_id",
        "element_kind",
        "text",
        "selector",
        "button_type",
        "name",
        "value",
        "target",
        "download",
        "effective_action_url",
        "effective_method",
        "href_url",
        "href_attribute",
        "visible",
        "enabled",
        "safety_descriptors",
    )
    assert tuple(field.name for field in fields(ObservedBlocker)) == ("code", "frame_id", "text")
    assert tuple(field.name for field in fields(ObservedValidationError)) == ("target_id", "text")
    assert tuple(field.name for field in fields(PageObservation)) == (
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

    option = ObservedOption(value="yes", label="Yes", enabled=False)
    field = ObservedField(
        target_id="obs-1:frame-0:field-1",
        field_key="key",
        frame_id="frame-0",
        frame_url="https://boards.greenhouse.io/acme/jobs/1",
        form_action_url=None,
        kind="checkbox",
        name="agree",
        label="Agree",
        group_id=None,
        option_value=None,
        safety_descriptors=("Agree",),
        selector="#agree",
        required=True,
        visible=True,
        enabled=False,
        readonly=False,
        value=False,
        will_validate=True,
        valid=False,
        validity_flags=("valueMissing",),
        file_count=0,
        file_basenames=(),
        accept=(),
        min_length=None,
        max_length=None,
        pattern=None,
        min_value=None,
        max_value=None,
        step=None,
        options=(option,),
    )

    assert field.enabled is False
    assert field.value is False
    with pytest.raises(FrozenInstanceError):
        field.enabled = True  # type: ignore[misc]


def test_plan_contract_preserves_bool_answers_and_freezes_context() -> None:
    answer = FieldAnswer("field-1", False, 1.0, "configured", "configured")
    plan = AutofillPlan(
        answers=(answer,),
        resume_upload_target_id=None,
        safe_click_target_id=None,
        status="manual",
        reason_code=PublicReasonCode.required_safe_fields_unresolved,
        skipped_target_ids=("field-2",),
        private_raw={"source": ["model"]},
    )
    context = ApplicationContext(profile_facts={"email": "person@example.test"}, resume_available=True)

    assert plan.answers[0].value is False
    assert thaw_json(plan.private_raw) == {"source": ["model"]}
    assert context.profile_facts["email"] == "person@example.test"
    with pytest.raises(TypeError):
        context.profile_facts["email"] = "changed@example.test"  # type: ignore[index]


def test_safety_policy_loads_from_package_resources() -> None:
    policy = load_safety_policy()
    assert isinstance(policy, SafetyPolicy)
    assert policy.version == "2026-07-10.wave-b"
    assert policy.sensitive_field_kinds == ("password",)

    resource = resources.files("jobs_assistant").joinpath("safety_policy.json")
    assert resource.is_file()
    assert "socialsecuritynumber" in resource.read_text(encoding="utf-8")


def test_greenhouse_route_graph_is_versioned_and_vectors_are_deterministic() -> None:
    policy = load_safety_policy()
    graph = load_greenhouse_route_policy(policy)
    assert graph["version"] == "2026-07-10.greenhouse-routes.v1"
    assert tuple(graph["interactive_frame_origins"]) == (
        "https://boards.greenhouse.io",
        "https://job-boards.greenhouse.io",
    )
    assert is_greenhouse_interactive_origin("https://boards.greenhouse.io", policy)
    assert is_greenhouse_interactive_origin("https://job-boards.greenhouse.io/", policy)
    assert not is_greenhouse_interactive_origin("https://boards.greenhouse.io.evil.example", policy)
    assert not is_greenhouse_interactive_origin("https://boards.greenhouse.io/embed/job_app", policy)
    assert tuple(graph["approved_static_get_head"]["methods"]) == ("GET", "HEAD")
    assert graph["final_like_match"] == "ascii_word_boundary"
    assert greenhouse_route_parity_vectors(policy) == policy.route_parity_vectors
    for vector in policy.route_parity_vectors:
        decision = classify_greenhouse_route_vector(vector, policy=policy)
        expected = str(vector["expected"])
        if expected == "human:field_ownership":
            assert decision.human_only is True
            assert decision.allowed is False
            assert decision.field_ownership is True
        else:
            expected_allowed, expected_value = expected.split(":", 1)
            assert decision.allowed is (expected_allowed == "allow"), vector["name"]
            if decision.allowed:
                assert decision.route_class == expected_value, vector["name"]
            else:
                assert decision.reason == expected_value, vector["name"]


def test_greenhouse_classifiers_use_policy_routes_not_hardcoded_defaults() -> None:
    policy = load_safety_policy()
    graph = dict(policy.greenhouse_route_policy)
    initial = dict(graph["automated_initial_get"])
    hosted = dict(initial["routes"][0])
    hosted["path_template"] = "/{board}/preview/{job}"
    initial["routes"] = (hosted,)
    graph["automated_initial_get"] = initial
    forms = dict(graph["human_only_form_actions"])
    form_route = dict(forms["routes"][0])
    form_route["path_template"] = "/{board}/preview/{job}"
    forms["routes"] = (form_route,)
    graph["human_only_form_actions"] = forms
    static = dict(graph["approved_static_get_head"])
    static["path_prefixes"] = {"boards.greenhouse.io": ("/cdn/",)}
    graph["approved_static_get_head"] = static

    modified = replace(policy, greenhouse_route_policy=graph)
    assert classify_greenhouse_request(
        "https://boards.greenhouse.io/assets/application.js",
        request_class="static",
        resource_type="script",
        policy=modified,
    ).reason == "unsupported_static_path"
    assert classify_greenhouse_request(
        "https://boards.greenhouse.io/cdn/application.js",
        request_class="static",
        resource_type="script",
        policy=modified,
    ).allowed is True

    assert classify_greenhouse_url("https://boards.greenhouse.io/example/jobs/123", policy=modified).allowed is False
    custom_initial = classify_greenhouse_url("https://boards.greenhouse.io/example/preview/123", policy=modified)
    assert custom_initial.allowed is True
    assert custom_initial.route_class == "hosted"
    custom_form = classify_greenhouse_form_action(
        "https://boards.greenhouse.io/example/preview/123",
        method="POST",
        policy=modified,
    )
    assert custom_form.allowed is False
    assert custom_form.field_ownership is True


@pytest.mark.parametrize(
    ("url", "route_class"),
    [
        ("https://boards.greenhouse.io/example/jobs/123", "hosted"),
        ("https://job-boards.greenhouse.io/example/jobs/123?gh_src=fixture", "hosted"),
        ("https://boards.greenhouse.io/embed/job_app?for=example&token=123", "embed"),
        ("https://grnh.se/example", "shortlink"),
    ],
)
def test_greenhouse_initial_route_classes_are_exact(url: str, route_class: str) -> None:
    decision = classify_greenhouse_url(url)
    assert decision.allowed is True
    assert decision.automation is True
    assert decision.route_class == route_class


@pytest.mark.parametrize(
    ("url", "reason"),
    [
        ("https://custom.example/jobs/123", "unsupported_host"),
        ("https://127.0.0.1/example/jobs/123", "private_host"),
        ("https://user:password@boards.greenhouse.io/example/jobs/123", "userinfo_rejected"),
        ("https://boards.greenhouse.io/example/jobs/123?gh_src=a&gh_src=b", "duplicate_query"),
        ("https://boards.greenhouse.io/example/jobs/123?next=confirm", "final_like_route"),
        ("https://boards.greenhouse.io/example/jobs/123?custom=fixture", "unknown_query"),
    ],
)
def test_greenhouse_initial_route_rejections_fail_closed(url: str, reason: str) -> None:
    decision = classify_greenhouse_url(url)
    assert decision.allowed is False
    assert decision.reason == reason


def test_greenhouse_static_method_type_and_path_caps_are_independent() -> None:
    url = "https://boards.greenhouse.io/assets/application.js"
    assert classify_greenhouse_request(url, request_class="static", resource_type="script").allowed is True
    assert classify_greenhouse_request(url, method="HEAD", request_class="static", resource_type="script").allowed is True
    cdn_url = "https://job-boards.cdn.greenhouse.io/assets/application-f00ba4.js"
    assert classify_greenhouse_request(cdn_url, request_class="static", resource_type="script").allowed is True
    assert (
        classify_greenhouse_request(
            "https://job-boards.cdn.greenhouse.io/tracker.js",
            request_class="static",
            resource_type="script",
        ).reason
        == "unsupported_static_path"
    )
    assert (
        classify_greenhouse_request(
            "https://grnh.se/assets/application.js",
            request_class="static",
            resource_type="script",
        ).reason
        == "unsupported_static_host"
    )
    assert classify_greenhouse_request(url, method="POST", request_class="static", resource_type="script").reason == "method_mismatch"
    assert classify_greenhouse_request(url, request_class="static", resource_type="document").reason == "unsupported_resource_type"
    long_url = "https://boards.greenhouse.io/" + ("x" * 2048)
    assert classify_greenhouse_request(long_url, request_class="static", resource_type="script").reason == "path_cap"


def test_greenhouse_form_ownership_never_becomes_automation_and_confirmation_needs_permit() -> None:
    page_url = "https://job-boards.greenhouse.io/example/jobs/123"
    form = classify_greenhouse_form_action(page_url, method="POST")
    assert form.allowed is False
    assert form.human_only is True
    assert form.field_ownership is True
    assert form.permit_required is True

    form_request = classify_greenhouse_request(
        page_url,
        method="POST",
        request_class="form",
        page_url=page_url,
        human_permit=True,
    )
    assert form_request.allowed is False
    assert form_request.automation is False
    assert form_request.field_ownership is True
    assert form_request.reason == "human_only_form_action"

    confirmation_url = page_url + "/confirmation"
    no_confirmation_permit = classify_greenhouse_request(
        confirmation_url,
        request_class="post_human_confirmation",
        page_url=page_url,
        human_permit=False,
    )
    assert no_confirmation_permit.reason == "human_permit_required"
    confirmed = classify_greenhouse_request(
        confirmation_url,
        request_class="post_human_confirmation",
        page_url=page_url,
        human_permit=True,
    )
    assert confirmed.allowed is True
    assert confirmed.same_board_job is True
    assert confirmed.permit_required is True
    wrong_job = classify_greenhouse_request(
        page_url.replace("/123", "/124") + "/confirmation",
        request_class="post_human_confirmation",
        page_url=page_url,
        human_permit=True,
    )
    assert wrong_job.reason == "same_board_job_required"
    assert classify_greenhouse_request(
        confirmation_url,
        method="POST",
        request_class="post_human_confirmation",
        page_url=page_url,
        human_permit=True,
    ).reason == "method_mismatch"


@pytest.mark.parametrize(
    ("raw", "normalized"),
    [
        ("dateOfBirth", "date of birth"),
        ("salaryExpectation", "salary expectation"),
        ("EEO-1 VeteranStatus", "eeo 1 veteran status"),
        ("job_application[phone]", "job application phone"),
        ("Question_1234", "question 1234"),
    ],
)
def test_ascii_camel_token_and_compact_normalization(raw: str, normalized: str) -> None:
    assert normalize_descriptor(raw) == normalized


@pytest.mark.parametrize(
    "descriptors",
    [
        ("SSN",),
        ("socialSecurityNumber",),
        ("DateOfBirth",),
        ("veteranStatus",),
        ("salaryExpectation",),
        ("Are you legally authorized to work?",),
        ("Race", "White"),
    ],
)
def test_sensitive_terms_and_compact_aliases_are_manual(descriptors: tuple[str, ...]) -> None:
    decision = classify_descriptors(descriptors)
    assert decision == DescriptorSafety.SENSITIVE

@pytest.mark.parametrize(
    "descriptor",
    [
        "Are you a U.S. citizen?",
        "usCitizen",
        "citizenStatus",
        "uscitizen",
        "Citizenship Status*",
        "citizenshipStatus",
        "citizenshipstatus",
        "Nationality",
        "nationalityStatus",
        "nationalitystatus",
        "Immigration Status",
        "immigrationStatus",
        "immigrationstatus",
    ],
)
def test_citizenship_nationality_immigration_descriptors_are_manual(descriptor: str) -> None:
    assert classify_descriptors((descriptor,)) == DescriptorSafety.SENSITIVE


@pytest.mark.parametrize(
    ("unsafe_class", "descriptors"),
    [
        ("payment", ("Payment method",)),
        ("financial", ("Financial account",)),
        ("credit-card", ("creditCardNumber",)),
        ("bank", ("bankAccount",)),
        ("password", ("account password",)),
        ("login", ("login email",)),
        ("sign-in", ("signInRequired",)),
        ("authentication", ("authentication credential",)),
        ("credential", ("credential secret",)),
        ("signature", ("electronic signature",)),
        ("consent", ("consent to terms",)),
        ("identity", ("identity document",)),
        ("government identifiers", ("governmentId",)),
        ("eeo", ("EEO voluntary self-identification",)),
        ("demographic", ("demographic survey",)),
        ("protected", ("protectedClass",)),
        ("legal eligibility", ("legalEligibility",)),
        ("compensation", ("compensation expectation",)),
        ("salary", ("salaryExpectation",)),
        ("availability", ("availability to interview",)),
        ("start-date", ("startDate",)),
        ("scheduling", ("scheduling preferences",)),
        ("background", ("background check",)),
        ("drug", ("drugTest",)),
        ("disability", ("disability status",)),
        ("veteran", ("protectedVeteranStatus",)),
        ("race", ("race",)),
        ("gender", ("gender identity",)),
        ("religion", ("religion",)),
        ("marital", ("maritalStatus",)),
        ("ssn", ("SSN",)),
        ("dob", ("DOB",)),
    ],
)
def test_every_unsafe_policy_class_is_manual(unsafe_class: str, descriptors: tuple[str, ...]) -> None:
    assert unsafe_class
    assert classify_descriptors(descriptors) == DescriptorSafety.SENSITIVE


@pytest.mark.parametrize(
    "descriptors",
    [
        ("creditCardNumber",),
        ("bankAccountNumber",),
        ("governmentIdentifier",),
        ("protectedVeteranStatus",),
        ("legalEligibility",),
        ("startDate",),
        ("drugTest",),
        ("maritalStatus",),
        ("taxId",),
        ("passportNumber",),
    ],
)
def test_compact_and_camel_unsafe_aliases_are_manual(descriptors: tuple[str, ...]) -> None:
    assert classify_descriptors(descriptors) == DescriptorSafety.SENSITIVE


@pytest.mark.parametrize(
    "options",
    [
        (("hispanic_or_latino", "Hispanic or Latino"),),
        (("decline", "I do not wish to answer"),),
        (("protected_veteran", "Protected Veteran"),),
        (("female", "Female"), ("male", "Male")),
    ],
)
def test_option_only_eeo_and_demographic_values_are_manual(options: tuple[tuple[str, str], ...]) -> None:
    assert classify_descriptors(("Custom question",), options=options) == DescriptorSafety.SENSITIVE


@pytest.mark.parametrize(
    ("descriptors", "options"),
    [
        (("Custom question",), ()),
        (("Portfolio URL",), ()),
        (("Referral source",), (("1", "LinkedIn"),)),
        (("racecar project",), ()),
        (("banking experience",), ()),
        (("startups",), ()),
        (("internationality",), ()),
    ],
)
def test_benign_near_boundaries_remain_safe(
    descriptors: tuple[str, ...],
    options: tuple[tuple[str, str], ...],
) -> None:
    assert classify_descriptors(descriptors, options=options) == DescriptorSafety.SAFE


def test_password_field_kind_fails_closed_even_with_safe_descriptors() -> None:
    assert classify_descriptors(("Account",), field_kind="password") == DescriptorSafety.SENSITIVE


def test_non_ascii_descriptor_fails_closed() -> None:
    assert classify_descriptors(("naïve field",)) == DescriptorSafety.SENSITIVE
    assert classify_descriptors(("salary\u202e",)) == DescriptorSafety.SENSITIVE
    assert classify_descriptors(("safe",), options=(("1", "расе"),)) == DescriptorSafety.SENSITIVE
    assert classify_descriptors(("Account",), field_kind="passwоrd") == DescriptorSafety.SENSITIVE


def test_descriptor_and_option_caps_fail_without_truncation() -> None:
    policy = load_safety_policy()

    with pytest.raises(DescriptorLimitError):
        classify_descriptors(("x",) * (policy.max_descriptors + 1), policy=policy)
    with pytest.raises(DescriptorLimitError):
        classify_descriptors(("x" * (policy.max_descriptor_bytes + 1),), policy=policy)
    with pytest.raises(DescriptorLimitError):
        classify_descriptors(("x" * 1024,) * 9, policy=policy)
    with pytest.raises(DescriptorLimitError):
        classify_descriptors(("safe",), options=(("1", "one"),) * (policy.max_options + 1), policy=policy)
    with pytest.raises(DescriptorLimitError):
        classify_descriptors(("safe",), options=(("x", "y" * (policy.max_option_bytes + 1)),), policy=policy)


def test_descriptor_and_option_caps_accept_exact_boundaries() -> None:
    policy = load_safety_policy()

    assert classify_descriptors(("x",) * policy.max_descriptors, policy=policy) == DescriptorSafety.SAFE
    assert classify_descriptors(("x" * policy.max_descriptor_bytes,), policy=policy) == DescriptorSafety.SAFE
    assert classify_descriptors(("x" * policy.max_descriptor_bytes,) * 4, policy=policy) == DescriptorSafety.SAFE
    assert (
        classify_descriptors(
            ("safe",),
            options=(("1", "one"),) * policy.max_options,
            policy=policy,
        )
        == DescriptorSafety.SAFE
    )
    assert (
        classify_descriptors(
            ("safe",),
            options=(("x" * 1024, "y" * 1024),) * 32,
            policy=policy,
        )
        == DescriptorSafety.SAFE
    )


def test_numeric_option_values_are_neutral_only_with_safe_labels() -> None:
    assert classify_descriptors(("Referral source",), options=(("1", "LinkedIn"),)) == DescriptorSafety.SAFE
    assert classify_descriptors(("Referral source",), options=(("1", "Veteran Status"),)) == DescriptorSafety.SENSITIVE
    assert classify_descriptors(("Referral source",), options=(("1", ""),)) == DescriptorSafety.SENSITIVE

def test_lever_route_graph_accepts_eu_and_requires_canonical_uuid_identity() -> None:
    policy = load_safety_policy()
    graph = policy.lever_route_policy
    assert tuple(graph["interactive_frame_origins"]) == (
        "https://jobs.lever.co",
        "https://jobs.eu.lever.co",
    )
    vectors = ats_route_parity_vectors("lever", policy)
    assert vectors == tuple(graph["parity_vectors"])
    for vector in vectors:
        decision = classify_ats_route_vector(vector, ats_policy="lever", policy=policy)
        expected_allowed, expected_value = str(vector["expected"]).split(":", 1)
        if expected_value == "field_ownership":
            assert decision.field_ownership is (expected_allowed == "human")
        else:
            assert decision.allowed is (expected_allowed == "allow"), vector["name"]
            if decision.allowed:
                assert decision.route_class == expected_value, vector["name"]
            else:
                assert decision.reason == expected_value, vector["name"]

    uuid_url = "https://jobs.eu.lever.co/acme/123e4567-e89b-12d3-a456-426614174000"
    assert classify_ats_url(uuid_url, ats_policy="lever", policy=policy).allowed
    assert not classify_ats_url("https://jobs.eu.lever.co/acme/job-123", ats_policy="lever", policy=policy).allowed
    for suffix in ("?", "#"):
        assert not classify_ats_url(uuid_url + suffix, ats_policy="lever", policy=policy).allowed
    assert not classify_ats_form_action(
        uuid_url + "/apply",
        ats_policy="lever",
        page_url="https://jobs.lever.co/acme/123e4567-e89b-12d3-a456-426614174000/apply",
        policy=policy,
    ).field_ownership
    cross_host = classify_ats_request(
        "https://jobs.eu.lever.co/acme/123e4567-e89b-12d3-a456-426614174000/confirmation",
        ats_policy="lever",
        request_class="post_human_confirmation",
        page_url="https://jobs.lever.co/acme/123e4567-e89b-12d3-a456-426614174000/apply",
        human_permit=True,
        policy=policy,
    )
    assert cross_host.reason == "same_company_job_required"
@pytest.mark.parametrize(
    "descriptor",
    [
        "OTP",
        "mfaRequired",
        "twoFactorAuthentication",
        "multi-factor authentication",
        "oneTimePassword",
        "passcode",
        "verificationCode",
        "emailVerification",
        "accountCreation",
        "accountVerification",
        "verifyYourAccount",
    ],
)
def test_task10_authentication_descriptors_are_manual(descriptor: str) -> None:
    assert classify_descriptors((descriptor,)) == DescriptorSafety.SENSITIVE


@pytest.mark.parametrize(
    "descriptor",
    [
        "codingAssessment",
        "codingChallenge",
        "personalityAssessment",
        "aptitudeAssessment",
        "cognitiveAssessment",
        "behavioralAssessment",
        "workStyleAssessment",
        "personality assessments",
        "work-style assessments",
    ],
)
def test_task10_assessment_descriptors_are_manual(descriptor: str) -> None:
    assert classify_descriptors((descriptor,)) == DescriptorSafety.SENSITIVE


@pytest.mark.parametrize(
    "descriptor",
    [
        "relocation",
        "willingToRelocate",
        "relocationAssistance",
    ],
)
def test_task10_relocation_descriptors_are_manual(descriptor: str) -> None:
    assert classify_descriptors((descriptor,)) == DescriptorSafety.SENSITIVE


@pytest.mark.parametrize(
    "descriptor",
    [
        "code",
        "code sample",
        "test",
        "test environment",
        "challenge",
        "challenge accepted",
        "verify",
        "verify input",
        "account",
        "account balance",
        "verification status",
        "personality type",
        "aptitude score",
        "cognitive science",
        "behavioral interview",
        "work style preferences",
        "willing to travel",
    ],
)
def test_task10_bare_security_and_assessment_words_remain_safe(descriptor: str) -> None:
    assert classify_descriptors((descriptor,)) == DescriptorSafety.SAFE
