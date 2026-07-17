from __future__ import annotations

import inspect
import sys
import os
import hashlib
from importlib import import_module
from pathlib import Path
from types import MappingProxyType

import pytest

import jobs_assistant.ats as ats
from jobs_assistant.application import _configured_and_profile_plan
from jobs_assistant.ats import (
    ApplicationProfile,
    GreenhouseAdapter,
    LeverAdapter,
    ResumeContext,
    ResumeFacts,
    canonical_greenhouse_fact,
    classify_greenhouse_form_action,
    classify_greenhouse_request,
    classify_greenhouse_url,
    extract_resume_facts,
    field_accepts_resume,
    load_application_profile,
    load_application_profile_snapshot,
    load_applicant_description,
    load_resume_context,
    merge_plans,
    parse_application_profile,
    unresolved_required_fields,
    validate_answer_value,
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


def _field(
    *,
    target_id: str = "field-1",
    kind: str = "text",
    name: str | None = None,
    label: str = "",
    group_id: str | None = None,
    required: bool = False,
    readonly: bool = False,
    safety_descriptors: tuple[str, ...] = (),
    accept: tuple[str, ...] = (),
    options: tuple[ObservedOption, ...] = (),
    min_length: int | None = None,
    max_length: int | None = None,
    pattern: str | None = None,
    min_value: str | None = None,
    max_value: str | None = None,
    step: str | None = None,
    value: str | bool | tuple[str, ...] | None = None,
    multiple: bool = False,
) -> ObservedField:
    return ObservedField(
        target_id=target_id,
        field_key=target_id,
        frame_id="frame-0",
        frame_url="https://boards.greenhouse.io/acme/jobs/123",
        form_action_url=None,
        kind=kind,
        name=name,
        label=label,
        group_id=group_id,
        option_value=None,
        safety_descriptors=safety_descriptors,
        selector=f"#{target_id}",
        required=required,
        visible=True,
        enabled=True,
        readonly=readonly,
        value=value,
        will_validate=True,
        valid=True,
        validity_flags=(),
        file_count=0,
        file_basenames=(),
        accept=accept,
        min_length=min_length,
        max_length=max_length,
        pattern=pattern,
        min_value=min_value,
        max_value=max_value,
        step=step,
        options=options,
        multiple=multiple,
    )


def _observation(*fields: ObservedField) -> PageObservation:
    return PageObservation(
        observation_id="obs-1",
        url="https://boards.greenhouse.io/acme/jobs/123",
        title="Apply",
        site_markers=("greenhouse",),
        fields=fields,
        buttons=(),
        final_submit_target_ids=(),
        errors=(),
        blockers=(),
    )


def _valid_empty_pdf() -> bytes:
    body = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    objects = (
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [] /Count 0 >>",
    )
    for number, obj in enumerate(objects, 1):
        offsets.append(len(body))
        body.extend(f"{number} 0 obj\n".encode("ascii"))
        body.extend(obj)
        body.extend(b"\nendobj\n")
    xref_offset = len(body)
    body.extend(b"xref\n0 3\n0000000000 65535 f \n")
    for offset in offsets[1:]:
        body.extend(f"{offset:010d} 00000 n \n".encode("ascii"))
    body.extend(
        b"trailer\n<< /Size 3 /Root 1 0 R >>\n"
        + f"startxref\n{xref_offset}\n".encode("ascii")
        + b"%%EOF\n"
    )
    return bytes(body)


def _context(profile: ApplicationProfile | None = None, *, resume: bool = False) -> ApplicationContext:
    return ApplicationContext(profile_facts=(profile.facts if profile else {}), resume_available=resume)


def test_ats_uses_shared_contracts_and_has_no_legacy_surface() -> None:
    contracts = import_module("jobs_assistant.contracts")
    assert ats.ApplicationContext is contracts.ApplicationContext
    assert "application" not in ats.__dict__.get("__all__", ())
    assert not hasattr(ats, "find_resume_file")
    assert not hasattr(ats, "load_resume_metadata")
    assert "resume_dir" not in inspect.signature(load_application_profile).parameters
    assert "resume_dir" not in inspect.signature(load_resume_context).parameters
    assert "selector" not in inspect.signature(ats.merge_plans).parameters
    assert inspect.signature(FieldAnswer).parameters.keys() == {"target_id", "value", "confidence", "reason", "source"}

def test_lever_adapter_is_registered_and_explicit_route_is_fail_closed() -> None:
    valid = "https://jobs.lever.co/acme/123e4567-e89b-12d3-a456-426614174000"
    adapter = LeverAdapter()
    assert any(isinstance(item, LeverAdapter) for item in ats.ADAPTERS)
    assert adapter.matches(valid, "")
    assert not adapter.matches("https://boards.greenhouse.io/acme/jobs/123", "")
    assert ats.select_adapter("auto", url=valid).name == "lever"  # type: ignore[union-attr]
    with pytest.raises(ValueError):
        ats.select_adapter("unknown")


def test_resume_context_retains_fd_snapshot_and_facts_and_closes(tmp_path: Path) -> None:
    resume = tmp_path / "Main_Resume.txt"
    resume.write_text("Ada Lovelace\nada@example.test\nhttps://www.linkedin.com/in/ada\n")

    context = load_resume_context(resume)
    assert isinstance(context, ResumeContext)
    assert context.basename == "Main_Resume.txt"
    assert context.media_type == "text/plain"
    assert context.facts.facts["email"] == "ada@example.test"
    assert context.facts.facts["full_name"] == "Ada Lovelace"
    fd = context.fileno()

    replacement = tmp_path / "replacement.txt"
    replacement.write_text("Grace Hopper\ngrace@example.test\n")
    os.replace(replacement, resume)
    dup_fd = os.dup(fd)
    try:
        os.lseek(dup_fd, 0, os.SEEK_SET)
        assert os.read(dup_fd, 256).startswith(b"Ada Lovelace")
    finally:
        os.close(dup_fd)
    context.close()
    with pytest.raises(OSError):
        os.fstat(fd)
    context.close()


def test_resume_rejects_symlink_directory_oversize_and_unknown_suffix(tmp_path: Path) -> None:
    target = tmp_path / "real.txt"
    target.write_text("safe")
    link = tmp_path / "Main_Resume.txt"
    link.symlink_to(target)
    with pytest.raises(ValueError, match="symlink|regular"):
        load_resume_context(link)
    with pytest.raises(ValueError, match="regular"):
        load_resume_context(tmp_path)

    too_large = tmp_path / "large.txt"
    too_large.write_bytes(b"a" * (10 * 1024 * 1024 + 1))
    with pytest.raises(ValueError, match="cap|10 MiB"):
        load_resume_context(too_large)

    unknown = tmp_path / "resume.rtf"
    unknown.write_text("safe")
    with pytest.raises(ValueError, match="explicit"):
        load_resume_context(unknown)


def test_resume_facts_require_unique_candidates_and_preserve_ambiguity() -> None:
    unique = extract_resume_facts("Ada Lovelace\nada@example.test\n+1 (555) 010-0000\n")
    assert isinstance(unique, ResumeFacts)
    assert unique.facts["email"] == "ada@example.test"
    assert unique.facts["first_name"] == "Ada"

    ambiguous = extract_resume_facts("Ada Lovelace\nGrace Hopper\nada@example.test\ngrace@example.test\n")
    assert "full_name" in ambiguous.ambiguous
    assert "email" in ambiguous.ambiguous
    assert "full_name" not in ambiguous.facts
    assert "email" not in ambiguous.facts


def test_resume_pdf_and_text_caps_and_accept_matching(tmp_path: Path) -> None:
    resume = tmp_path / "Main_Resume.pdf"
    resume.write_bytes(_valid_empty_pdf())
    context = load_resume_context(resume)
    try:
        file_field = _field(target_id="resume", kind="file", label="Resume", accept=(".pdf",))
        assert field_accepts_resume(file_field, context)
        assert field_accepts_resume(file_field, context, accept=("application/*",))
        assert not field_accepts_resume(file_field, context, accept=(".docx",))
        assert not field_accepts_resume(_field(kind="text"), context)
    finally:
        context.close()

    huge_text = tmp_path / "Main_Resume.md"
    huge_text.write_text("x" * (ats.MAX_RESUME_TEXT_CHARS + 1))
    with pytest.raises(ValueError, match="100,000"):
        load_resume_context(huge_text)

def test_resume_pdf_page_and_extracted_text_caps_close_failure_fds(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    class FakePage:
        def __init__(self, text: str = "") -> None:
            self.text = text

        def extract_text(self) -> str:
            return self.text

    close_calls: list[int] = []
    real_close = ats.os.close

    def tracking_close(fd: int) -> None:
        close_calls.append(fd)
        real_close(fd)

    monkeypatch.setattr(ats.os, "close", tracking_close)
    pdf = tmp_path / "Main_Resume.pdf"
    pdf.write_bytes(b"%PDF-1.4\n")

    pages = [FakePage()] * (ats.MAX_PDF_PAGES + 1)

    class TooManyPagesReader:
        def __init__(self, _stream: object) -> None:
            self.pages = pages

    monkeypatch.setitem(sys.modules, "pypdf", type("FakePdf", (), {"PdfReader": TooManyPagesReader}))
    with pytest.raises(ValueError, match="100 pages"):
        load_resume_context(pdf)

    class TooMuchTextReader:
        def __init__(self, _stream: object) -> None:
            self.pages = [FakePage("x" * (ats.MAX_RESUME_TEXT_CHARS + 1))]

    monkeypatch.setitem(sys.modules, "pypdf", type("FakePdf", (), {"PdfReader": TooMuchTextReader}))
    with pytest.raises(ValueError, match="100,000"):
        load_resume_context(pdf)
    assert len(close_calls) == 2


def test_profile_load_is_bounded_frozen_and_exactly_typed(tmp_path: Path) -> None:
    profile_path = tmp_path / "profile.json"
    profile_path.write_text(
        '{"first_name":"Ada","nested":{"skills":["python"]},'
        '"resume_summary":"Analytical engine work.",'
        '"field_answers":[{"ats":"*","name":"job_application[email]",'
        '"kind":"email","value":"ada@example.test"}]}'
    )
    profile = load_application_profile(profile_path)
    assert isinstance(profile.facts, MappingProxyType)
    assert isinstance(profile.facts["nested"], MappingProxyType)
    assert profile.field_answers[0].value == "ada@example.test"
    with pytest.raises(TypeError):
        profile.facts["first_name"] = "Grace"  # type: ignore[index]

    duplicate = tmp_path / "duplicate.json"
    duplicate.write_text('{"first_name":"Ada","first_name":"Grace"}')
    with pytest.raises(ValueError, match="duplicate"):
        load_application_profile(duplicate)
    nonfinite = tmp_path / "nonfinite.json"
    nonfinite.write_text('{"score": NaN}')
    with pytest.raises(ValueError, match="non-finite"):
        load_application_profile(nonfinite)


def test_profile_snapshot_provenance_uses_parsed_bytes_after_replacement(tmp_path: Path) -> None:
    profile_path = tmp_path / "profile.json"
    original = b'{"first_name":"Ada"}'
    replacement = b'{"first_name":"Replacement"}'
    profile_path.write_bytes(original)
    loaded = load_application_profile_snapshot(profile_path)
    profile_path.write_bytes(replacement)
    assert loaded.profile.facts["first_name"] == "Ada"
    assert loaded.source_kind == "explicit_json"
    assert loaded.source_sha256 == hashlib.sha256(original).hexdigest()
    assert loaded.source_sha256 != hashlib.sha256(replacement).hexdigest()


def test_profile_symlink_replacement_and_depth_caps(tmp_path: Path) -> None:
    real = tmp_path / "real.json"
    real.write_text('{"email":"ada@example.test"}')
    link = tmp_path / "profile.json"
    link.symlink_to(real)
    with pytest.raises(ValueError, match="symlink|regular"):
        load_application_profile(link)

    nested = "1"
    for _ in range(ats.MAX_PROFILE_DEPTH + 1):
        nested = '{"x":' + nested + '}'
    deep = tmp_path / "deep.json"
    deep.write_text(nested)
    with pytest.raises(ValueError, match="depth"):
        load_application_profile(deep)
    too_big = tmp_path / "too-big.json"
    too_big.write_bytes(b"x" * (ats.MAX_PROFILE_BYTES + 1))
    with pytest.raises(ValueError, match="cap"):
        load_application_profile(too_big)
    long_string = tmp_path / "long-string.json"
    long_string.write_text('{"x":"' + ("a" * (ats.MAX_PROFILE_STRING_CHARS + 1)) + '"}')
    with pytest.raises(ValueError, match="string"):
        load_application_profile(long_string)


def test_description_loader_uses_regular_file_and_character_cap(tmp_path: Path) -> None:
    description = tmp_path / "description.txt"
    description.write_text("A concise applicant summary.")
    assert load_applicant_description(description) == "A concise applicant summary."
    assert load_applicant_description(None, ApplicationProfile(description="from profile")) == "from profile"

    link = tmp_path / "description-link.txt"
    link.symlink_to(description)
    with pytest.raises(ValueError, match="symlink|regular"):
        load_applicant_description(link)
    huge = tmp_path / "huge-description.txt"
    huge.write_text("x" * (ats.MAX_DESCRIPTION_CHARS + 1))
    with pytest.raises(ValueError, match="12,000"):
        load_applicant_description(huge)


def test_configured_specific_answer_wins_wildcard_and_boolean_types() -> None:
    profile = ApplicationProfile(
        facts={"email": "profile@example.test"},
        field_answers=(
            ats.ConfiguredFieldAnswer("*", "job_application[email]", None, "email", "wild@example.test"),
            ats.ConfiguredFieldAnswer("greenhouse", "job_application[email]", None, "email", "specific@example.test"),
            ats.ConfiguredFieldAnswer("greenhouse", None, "Subscribe", "checkbox", True),
        ),
    )
    observation = _observation(
        _field(target_id="email", kind="email", name="job_application[email]", label="Email"),
        _field(target_id="subscribe", kind="checkbox", name="subscribe", label="Subscribe"),
    )
    answers = GreenhouseAdapter().deterministic_answers(observation, _context(profile), profile=profile)
    assert answers == (
        FieldAnswer("email", "specific@example.test", 1.0, "configured field answer", "configured"),
        FieldAnswer("subscribe", True, 1.0, "configured field answer", "configured"),
    )


def test_configured_conflicts_and_invalid_values_are_manual() -> None:
    profile = ApplicationProfile(
        field_answers=(
            ats.ConfiguredFieldAnswer("greenhouse", None, "Email", "email", "not-an-email"),
        )
    )
    observation = _observation(_field(target_id="email", kind="email", label="Email", required=True))
    assert GreenhouseAdapter().deterministic_answers(observation, _context(profile), profile=profile) == ()


def test_descriptor_aliases_consensus_and_opaque_boundaries() -> None:
    profile = ApplicationProfile(facts={"first_name": "Ada", "last_name": "Lovelace", "email": "ada@example.test"})
    adapter = GreenhouseAdapter()
    observation = _observation(
        _field(target_id="first", name="job_application[first_name]", label="Given Name"),
        _field(target_id="last", name="last_name", label="Family Name"),
        _field(target_id="email", name="email", label="Email", safety_descriptors=("autocomplete=email",)),
        _field(target_id="opaque", name="question_1234", label="Question 1234"),
    )
    answers = adapter.deterministic_answers(observation, _context(profile), profile=profile)
    assert {answer.target_id for answer in answers} == {"first", "last", "email"}
    assert canonical_greenhouse_fact(observation.fields[3]) is None

    conflict = _observation(_field(target_id="bad", name="job_application[first_name]", label="Last Name"))
    assert canonical_greenhouse_fact(conflict.fields[0]) is None
    assert adapter.deterministic_answers(conflict, _context(profile), profile=profile) == ()




@pytest.mark.parametrize("adapter_type", (GreenhouseAdapter, LeverAdapter))
def test_location_aliases_fill_common_required_ats_fields(adapter_type) -> None:
    profile = ApplicationProfile(
        facts={
            "street_address": "123 Main Street",
            "address_line_2": "Apt 4",
            "city": "New York",
            "state_province": "NY",
            "zip": "10001",
            "country_code": "US",
        }
    )
    fields = (
        _field(
            target_id="address",
            name="job_application[address]",
            label="Address",
            safety_descriptors=("autocomplete=street-address",),
        ),
        _field(
            target_id="address-2",
            name="address_line_2",
            label="Address Line 2",
            safety_descriptors=("autocomplete=address-line2",),
        ),
        _field(target_id="city", name="city", label="City"),
        _field(target_id="state", name="state_province", label="State/Province"),
        _field(target_id="postal", name="zip", label="Postal Code"),
        _field(
            target_id="country",
            name="country",
            label="Country",
            safety_descriptors=("autocomplete=country",),
        ),
    )
    answers = adapter_type().deterministic_answers(
        _observation(*fields),
        _context(profile),
        profile=profile,
    )
    assert {answer.target_id: answer.value for answer in answers} == {
        "address": "123 Main Street",
        "address-2": "Apt 4",
        "city": "New York",
        "state": "NY",
        "postal": "10001",
        "country": "US",
    }


def test_location_aliases_preserve_conflict_ambiguity_and_validation_gates() -> None:
    profile = ApplicationProfile(
        facts={
            "address": "123 Main Street",
            "street_address": "456 Other Street",
            "postal_code": "100001",
        }
    )
    observation = _observation(
        _field(target_id="address", name="address", label="Address"),
        _field(target_id="postal", name="postal_code", label="Postal Code", max_length=5),
        _field(target_id="city", name="city", label="City"),
    )
    answers = GreenhouseAdapter().deterministic_answers(
        observation,
        _context(profile),
        profile=profile,
        resume_facts=ResumeFacts(
            facts={"city": "Paris"},
            candidates={"city": ("Paris", "London")},
            ambiguous=("city",),
        ),
    )
    assert answers == ()


def test_location_answer_resolves_required_field_before_manual_fallback() -> None:
    profile = ApplicationProfile(facts={"city": "New York"})
    field = _field(target_id="city", name="city", label="City", required=True)
    resume = ResumeContext("resume.txt", "text/plain", "", "0" * 64, -1, facts=ResumeFacts())
    plan = _configured_and_profile_plan(
        _observation(field),
        adapter=LeverAdapter(),
        context=_context(profile),
        profile=profile,
        resume=resume,
    )
    assert plan.status == "ready"
    assert plan.answers == (FieldAnswer("city", "New York", 1.0, "profile field", "profile"),)
    assert unresolved_required_fields(_observation(field), plan.answers) == ()


def test_safe_noncanonical_zero_candidate_remains_inference_eligible() -> None:
    field = _field(target_id="question", name="favorite_color", label="Favorite color", required=True)
    observation = _observation(field)
    llm_answer = FieldAnswer("question", "blue", 0.9, "model answer", "inference")
    plan = AutofillPlan(
        answers=(llm_answer,),
        status="ready",
        reason_code=PublicReasonCode.draft_ready,
        private_raw={},
    )
    merged = merge_plans((), plan, observation)
    assert canonical_greenhouse_fact(field) is None
    assert merged.answers == (llm_answer,)
    assert merged.status == "ready"


def test_profile_resume_agreement_and_ambiguity_gate_deterministic_facts(tmp_path: Path) -> None:
    resume_path = tmp_path / "Main_Resume.txt"
    resume_path.write_text("Ada Lovelace\nada@example.test\n")
    resume = load_resume_context(resume_path)
    try:
        profile = ApplicationProfile(facts={"first_name": "Ada", "email": "ada@example.test"})
        observation = _observation(
            _field(target_id="first", name="first_name", label="First Name"),
            _field(target_id="email", name="email", label="Email", kind="email"),
        )
        answers = GreenhouseAdapter().deterministic_answers(observation, _context(profile, resume=True), profile=profile, resume_context=resume)
        assert {answer.target_id for answer in answers} == {"first", "email"}

        disagreement = ApplicationProfile(facts={"first_name": "Grace", "email": "ada@example.test"})
        disagreement_answers = GreenhouseAdapter().deterministic_answers(observation, _context(disagreement, resume=True), profile=disagreement, resume_context=resume)
        assert {answer.target_id for answer in disagreement_answers} == {"email"}

        ambiguous = ResumeFacts(candidates={"email": ("ada@example.test", "grace@example.test")}, ambiguous=("email",))
        ambiguous_answers = GreenhouseAdapter().deterministic_answers(observation, _context(profile, resume=True), profile=profile, resume_facts=ambiguous)
        assert {answer.target_id for answer in ambiguous_answers} == {"first"}
    finally:
        resume.close()


def test_validate_answer_value_boundaries_is_pure() -> None:
    email = _field(target_id="email", kind="email", label="Email", required=True)
    assert validate_answer_value(email, "ada@example.test", kind="email")
    assert not validate_answer_value(email, "not-email", kind="email")
    assert not validate_answer_value(email, "", kind="email")

    number = _field(kind="number", min_value="1", max_value="5", step="2")
    assert validate_answer_value(number, "3")
    assert not validate_answer_value(number, "4")
    assert not validate_answer_value(number, "6")

    select = _field(kind="select", options=(ObservedOption("yes", "Yes", True), ObservedOption("no", "No", False)))
    assert validate_answer_value(select, "yes")
    assert not validate_answer_value(select, "no")
    patterned = _field(pattern=r"[A-Z]{2}", min_length=2, max_length=2)
    assert validate_answer_value(patterned, "AB")
    assert not validate_answer_value(patterned, "abc")
    assert not validate_answer_value(patterned, "A\u202eB")


def test_subtype_validators_apply_observed_patterns() -> None:
    assert not validate_answer_value(_field(kind="email", pattern=r".+@example\.test"), "ada@other.test")
    assert not validate_answer_value(_field(kind="tel", pattern=r"\+1.*"), "202-555-0100")
    assert not validate_answer_value(_field(kind="url", pattern=r"https://allowed\.test/.*"), "https://other.test/x")
    assert not validate_answer_value(_field(kind="date", pattern=r"2025-.*"), "2024-01-01")


def test_route_policy_classes_are_structural_and_nonfinal() -> None:
    hosted = classify_greenhouse_url("https://boards.greenhouse.io/acme/jobs/123?gh_src=abc")
    assert hosted.allowed and hosted.route_class == "hosted" and hosted.automation
    embed = classify_greenhouse_url("https://boards.greenhouse.io/embed/job_app?for=acme&token=123")
    assert embed.allowed and embed.route_class == "embed" and embed.automation
    short = classify_greenhouse_url("https://grnh.se/acme")
    assert short.allowed and short.route_class == "shortlink" and short.automation
    static = classify_greenhouse_request(
        "https://boards.greenhouse.io/assets/application.js",
        request_class="static",
        resource_type="script",
    )
    assert static.allowed and static.route_class == "static" and static.automation

    form = classify_greenhouse_form_action(
        "https://boards.greenhouse.io/acme/jobs/123",
        page_url="https://boards.greenhouse.io/acme/jobs/123",
    )
    assert not form.allowed
    assert form.human_only and form.field_ownership and form.permit_required
    assert not form.automation

    rejected = (
        "https://boards.greenhouse.io/acme/jobs/123?x=1",
        "https://boards.greenhouse.io/acme/jobs/123/submit",
        "https://boards.greenhouse.io/acme/jobs/123#token",
        "http://boards.greenhouse.io/acme/jobs/123",
        "https://lever.co/acme/123",
        "https://127.0.0.1/acme/jobs/123",
    )
    for url in rejected:
        assert not classify_greenhouse_url(url).allowed

def test_adapter_matching_requires_allowed_shared_route() -> None:
    adapter = GreenhouseAdapter()
    assert adapter.matches("https://boards.greenhouse.io/acme/jobs/123", "<html></html>")
    assert not adapter.matches("https://boards.greenhouse.io/acme/jobs/123/submit", 'data-source="greenhouse"')
    assert not adapter.matches("https://evil.example/apply", 'data-source="greenhouse"')


def test_generated_greenhouse_names_defer_to_canonical_labels() -> None:
    for generated_name in (
        "question_1234",
        "field-1234",
        "job_application[answers_attributes][0][text_value]",
    ):
        field = _field(name=generated_name, label="First Name")
        assert canonical_greenhouse_fact(field) == "first_name"

    assert canonical_greenhouse_fact(_field(name="question_123", label="First Name")) == "first_name"


def test_profile_json_key_strings_obey_string_cap(tmp_path: Path) -> None:
    profile_path = tmp_path / "long-key.json"
    profile_path.write_text(
        '{"' + ("k" * (ats.MAX_PROFILE_STRING_CHARS + 1)) + '":"value"}',
    )
    with pytest.raises(ValueError, match="key|string"):
        load_application_profile(profile_path)


def test_answer_validation_rejects_malformed_urls_impossible_dates_and_empty_selects() -> None:
    url_field = _field(kind="url")
    assert not validate_answer_value(url_field, "https://[")
    assert not validate_answer_value(url_field, "https://example.test:not-a-port")

    date_field = _field(kind="date")
    assert validate_answer_value(date_field, "2024-02-29")
    assert not validate_answer_value(date_field, "2023-02-29")
    assert not validate_answer_value(date_field, "2024-04-31")

    select_field = _field(
        kind="select",
        options=(ObservedOption("yes", "Yes", False),),
    )
    assert not validate_answer_value(select_field, "yes")
    assert not validate_answer_value(_field(kind="select"), "yes")


def test_mixed_blank_accept_tokens_do_not_make_incompatible_resume_unrestricted(tmp_path: Path) -> None:
    resume_path = tmp_path / "Main_Resume.pdf"
    resume_path.write_bytes(_valid_empty_pdf())
    context = load_resume_context(resume_path)
    try:
        file_field = _field(kind="file", label="Resume")
        assert not field_accepts_resume(file_field, context, accept=("", ".docx"))
        assert field_accepts_resume(file_field, context, accept=("", ".pdf"))
        assert field_accepts_resume(file_field, context, accept=("", ""))
    finally:
        context.close()


def test_explicit_empty_resume_facts_retain_caller_precedence(tmp_path: Path) -> None:
    resume_path = tmp_path / "Main_Resume.txt"
    class FalseyResumeFacts(ResumeFacts):
        def __bool__(self) -> bool:
            return False

    resume_path.write_text("Ada Lovelace\n")
    resume = load_resume_context(resume_path)
    try:
        observation = _observation(_field(name="first_name", label="First Name"))
        assert GreenhouseAdapter().deterministic_answers(
            observation,
            _context(resume=True),
            resume_context=resume,
            resume_facts=FalseyResumeFacts(),
        ) == ()
    finally:
        resume.close()


def test_configured_entries_conflict_across_observation_targets() -> None:
    adapter = GreenhouseAdapter()
    profile = ApplicationProfile(
        facts={"email": "profile@example.test"},
        field_answers=(
            ats.ConfiguredFieldAnswer("greenhouse", None, "Email", "email", "one@example.test"),
            ats.ConfiguredFieldAnswer("greenhouse", None, "Email", "email", "two@example.test"),
        ),
    )
    observation = _observation(
        _field(target_id="email-a", kind="email", label="Email"),
        _field(target_id="email-b", kind="email", label="Email"),
    )
    assert adapter.deterministic_answers(observation, _context(profile), profile=profile) == ()

    same_target = _observation(_field(target_id="email", kind="email", label="Email"))
    assert adapter.deterministic_answers(same_target, _context(profile), profile=profile) == ()


def test_configured_name_label_disagreement_tombstones_both_partial_targets() -> None:
    profile = ApplicationProfile(
        field_answers=(
            ats.ConfiguredFieldAnswer("greenhouse", "email-a", "Email B", "email", "a@example.test"),
        )
    )
    observation = _observation(
        _field(target_id="a", kind="email", name="email-a", label="Email A"),
        _field(target_id="b", kind="email", name="email-b", label="Email B"),
    )
    assert GreenhouseAdapter().deterministic_answers(observation, _context(profile), profile=profile) == ()


def test_profile_alias_consensus_conflict_is_manual() -> None:
    profile = ApplicationProfile(
        facts={"email": "one@example.test", "email_address": "two@example.test"}
    )
    observation = _observation(_field(kind="email", name="email", label="Email"))
    assert GreenhouseAdapter().deterministic_answers(observation, _context(profile), profile=profile) == ()


def test_resume_requires_exact_label_and_emits_no_path(tmp_path: Path) -> None:
    resume_path = tmp_path / "Main_Resume.pdf"
    resume_path.write_bytes(_valid_empty_pdf())
    resume = load_resume_context(resume_path)
    try:
        adapter = GreenhouseAdapter()
        exact = _observation(_field(target_id="resume", kind="file", label="Resume"))
        answers = adapter.deterministic_answers(exact, _context(resume=True), resume_context=resume)
        assert answers == (FieldAnswer("resume", "", 1.0, "configured resume upload", "configured"),)
        assert adapter.deterministic_answers(
            _observation(_field(target_id="resume", kind="file", label="Upload your resume")),
            _context(resume=True),
            resume_context=resume,
        ) == ()
        assert canonical_greenhouse_fact(_field(kind="file", name="resume", label="")) is None
        assert adapter.deterministic_answers(exact, _context(resume=False), resume_context=resume) == ()
    finally:
        resume.close()


def test_corrupt_pdf_is_rejected(tmp_path: Path) -> None:
    corrupt = tmp_path / "corrupt.pdf"
    corrupt.write_bytes(b"%PDF-1.4\nnot a PDF")
    with pytest.raises(ValueError, match="decoded|PDF"):
        load_resume_context(corrupt)


def test_portable_answer_constraints_cover_text_contacts_and_numbers() -> None:
    text = _field(kind="text")
    assert validate_answer_value(text, "a" * ats.MAX_SINGLE_LINE_CHARS)
    assert not validate_answer_value(text, "a" * (ats.MAX_SINGLE_LINE_CHARS + 1))
    assert not validate_answer_value(text, "line\nbreak")

    textarea = _field(kind="textarea")
    assert validate_answer_value(textarea, "line\nbreak")
    assert not validate_answer_value(textarea, "a" * (ats.MAX_TEXTAREA_CHARS + 1))
    assert not validate_answer_value(text, "line\nbreak", kind="textarea")

    tel = _field(kind="tel")
    assert validate_answer_value(tel, "+1 (555) 010-0000")
    assert not validate_answer_value(tel, "123-45")
    assert not validate_answer_value(tel, "1-800-FLOWERS")
    assert not validate_answer_value(tel, "1" * 16)

    email = _field(kind="email")
    assert validate_answer_value(email, "ada@example.test")
    assert not validate_answer_value(email, "a..b@example.test")
    assert not validate_answer_value(email, "a" * 65 + "@example.test")
    assert not validate_answer_value(email, "a@" + ("b" * 310) + ".test")

    url = _field(kind="url")
    assert validate_answer_value(url, "https://example.test/profile")
    assert not validate_answer_value(url, "http://example.test/profile")
    assert not validate_answer_value(url, "https://user:secret@example.test")
    assert not validate_answer_value(url, "https://example.test/" + ("a" * ats.MAX_URL_CHARS))

    number = _field(kind="number", min_value="1", max_value="5", step="0.5")
    assert validate_answer_value(number, "2.5")
    assert not validate_answer_value(number, "2.25")
    assert not validate_answer_value(number, "1e2")
    assert not validate_answer_value(number, "01")


def test_group_required_satisfaction_preserves_checked_radio_and_checkbox() -> None:
    checked_radio = _field(
        target_id="yes",
        kind="radio",
        group_id="consent",
        required=True,
        value=True,
    )
    unchecked_radio = _field(
        target_id="no",
        kind="radio",
        group_id="consent",
        required=True,
        value=False,
    )
    observation = _observation(checked_radio, unchecked_radio)
    assert unresolved_required_fields(observation, ()) == ()
    assert GreenhouseAdapter().deterministic_answers(
        observation,
        _context(ApplicationProfile(facts={"first_name": "Ada"})),
        profile=ApplicationProfile(facts={"first_name": "Ada"}),
    ) == ()

    unchecked = _observation(
        _field(target_id="yes", kind="radio", group_id="consent", required=True, value=False),
        _field(target_id="no", kind="radio", group_id="consent", required=True, value=False),
    )
    assert unresolved_required_fields(unchecked, ()) == ("yes",)
    assert unresolved_required_fields(
        unchecked,
        (FieldAnswer("no", True, 1.0, "configured", "configured"),),
    ) == ()

    required_checkbox = _observation(
        _field(target_id="terms", kind="checkbox", required=True, value=True)
    )
    assert unresolved_required_fields(required_checkbox, ()) == ()

def test_required_readonly_empty_field_remains_unresolved() -> None:
    empty = _observation(
        _field(target_id="readonly_required", required=True, readonly=True, value="")
    )
    assert unresolved_required_fields(empty, ()) == ("readonly_required",)

    populated = _observation(
        _field(target_id="readonly_required", required=True, readonly=True, value="Ada")
    )
    assert unresolved_required_fields(populated, ()) == ()




def test_radio_group_with_multiple_checked_options_is_unresolved() -> None:
    multiple = _observation(
        _field(target_id="yes", kind="radio", group_id="consent", required=True, value=True),
        _field(target_id="no", kind="radio", group_id="consent", required=True, value=True),
    )
    assert unresolved_required_fields(multiple, ()) == ("yes",)

def test_autocomplete_url_is_compatible_but_never_selects_a_fact() -> None:
    linkedin = _field(name="linkedin", safety_descriptors=("autocomplete=url",))
    assert canonical_greenhouse_fact(linkedin) == "linkedin"
    email = _field(name="email", safety_descriptors=("autocomplete=url",))
    assert canonical_greenhouse_fact(email) is None
    opaque = _field(name="question_1234", safety_descriptors=("autocomplete=url",))
    assert canonical_greenhouse_fact(opaque) is None


def test_profile_configured_select_list_freezes_to_tuple() -> None:
    profile = ApplicationProfile(
        field_answers=(
            ats.ConfiguredFieldAnswer("greenhouse", None, "Skills", "select", ["python", "go"]),
        ),
    )
    assert profile.field_answers[0].value == ("python", "go")


def test_profile_configured_multi_select_order_and_canonicalization() -> None:
    profile = ApplicationProfile(
        field_answers=(
            ats.ConfiguredFieldAnswer("greenhouse", None, "Skills", "select", ("go", "python")),
        ),
    )
    field = _field(
        target_id="skills",
        kind="select",
        label="Skills",
        multiple=True,
        options=(
            ObservedOption("python", "Python", True),
            ObservedOption("go", "Go", True),
            ObservedOption("rust", "Rust", True),
        ),
    )
    observation = _observation(field)
    answers = GreenhouseAdapter().deterministic_answers(observation, _context(profile), profile=profile)
    assert answers == (FieldAnswer("skills", ("python", "go"), 1.0, "configured field answer", "configured"),)


def test_profile_configured_multi_select_rejects_duplicate_requested() -> None:
    profile = ApplicationProfile(
        field_answers=(
            ats.ConfiguredFieldAnswer("greenhouse", None, "Skills", "select", ("python", "python")),
        ),
    )
    field = _field(
        target_id="skills",
        kind="select",
        label="Skills",
        multiple=True,
        options=(ObservedOption("python", "Python", True),),
    )
    assert GreenhouseAdapter().deterministic_answers(_observation(field), _context(profile), profile=profile) == ()


def test_profile_configured_multi_select_rejects_disabled_options() -> None:
    profile = ApplicationProfile(
        field_answers=(
            ats.ConfiguredFieldAnswer("greenhouse", None, "Skills", "select", ("python", "rust")),
        ),
    )
    field = _field(
        target_id="skills",
        kind="select",
        label="Skills",
        multiple=True,
        options=(
            ObservedOption("python", "Python", True),
            ObservedOption("rust", "Rust", False),
        ),
    )
    assert GreenhouseAdapter().deterministic_answers(_observation(field), _context(profile), profile=profile) == ()


def test_profile_configured_multi_select_rejects_required_empty() -> None:
    profile = ApplicationProfile(
        field_answers=(ats.ConfiguredFieldAnswer("greenhouse", None, "Skills", "select", ()),)
    )
    field = _field(
        target_id="skills",
        kind="select",
        label="Skills",
        required=True,
        multiple=True,
        options=(ObservedOption("python", "Python", True),),
    )
    assert GreenhouseAdapter().deterministic_answers(_observation(field), _context(profile), profile=profile) == ()


def test_profile_configured_multi_select_rejects_malformed_list() -> None:
    with pytest.raises(ValueError, match="list values must be strings"):
        parse_application_profile(
            {"field_answers": [{"ats": "greenhouse", "label": "Skills", "kind": "select", "value": ["python", 1]}]}
        )


def test_validate_answer_value_rejects_duplicate_observed_option_values() -> None:
    field = _field(
        target_id="skills",
        kind="select",
        label="Skills",
        multiple=True,
        options=(
            ObservedOption("python", "Python", True),
            ObservedOption("python", "Python duplicate", True),
        ),
    )
    assert not validate_answer_value(field, ("python",))


def test_validate_answer_value_rejects_forbidden_controls_in_multi_select() -> None:
    field = _field(
        target_id="skills",
        kind="select",
        label="Skills",
        multiple=True,
        options=(ObservedOption("python\x00", "Python", True), ObservedOption("go", "Go", True)),
    )
    assert not validate_answer_value(field, ("python\x00",))
    assert not validate_answer_value(field, ("go", "python\x00"))


def test_validate_answer_value_multi_select_uses_observed_dom_order() -> None:
    field = _field(
        target_id="skills",
        kind="select",
        label="Skills",
        multiple=True,
        options=(
            ObservedOption("python", "Python", True),
            ObservedOption("go", "Go", True),
            ObservedOption("rust", "Rust", True),
        ),
    )
    assert validate_answer_value(field, ("rust", "python"))
    answer = next(
        answer
        for answer in GreenhouseAdapter().deterministic_answers(
            _observation(field),
            _context(
                ApplicationProfile(
                    field_answers=(ats.ConfiguredFieldAnswer("greenhouse", None, "Skills", "select", ("rust", "python")),)
                )
            ),
            profile=ApplicationProfile(
                field_answers=(ats.ConfiguredFieldAnswer("greenhouse", None, "Skills", "select", ("rust", "python")),)
            ),
        )
    )
    assert answer.value == ("python", "rust")
