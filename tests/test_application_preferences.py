from __future__ import annotations

import hashlib
import json
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from jobs_assistant.application_preferences import (
    APPLICATION_PREFERENCES_SCHEMA_VERSION,
    ApplicationPreferences,
    ObservedFieldDescriptor,
    PreferenceMapping,
    PreferenceOptOut,
    PreferenceValidationError,
    PreferenceMatcher,
    apply_preferences,
    load_application_preferences,
    mapping_answer,
    order_actions,
)
from jobs_assistant.contracts import FieldAnswer, ObservedField


def _field(
    target_id: str,
    *,
    kind: str = "text",
    name: str | None = None,
    label: str = "",
    required: bool = False,
    descriptors: tuple[str, ...] = (),
    value: str | bool | None = None,
) -> ObservedField:
    return ObservedField(
        target_id=target_id,
        field_key=target_id,
        frame_id="frame-0",
        frame_url="https://boards.greenhouse.io/acme/jobs/1",
        form_action_url=None,
        kind=kind,
        name=name,
        label=label,
        group_id=None,
        option_value=None,
        safety_descriptors=descriptors,
        selector=f"#{target_id}",
        required=required,
        visible=True,
        enabled=True,
        readonly=False,
        value=value,
        will_validate=True,
        valid=True,
        validity_flags=(),
        file_count=0,
        file_basenames=(),
        accept=(),
        min_length=None,
        max_length=None,
        pattern=None,
        min_value=None,
        max_value=None,
        step=None,
        options=(),
    )


def _document(**overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "schema_version": APPLICATION_PREFERENCES_SCHEMA_VERSION,
        "mappings": [
            {"ats": "*", "name": "email", "kind": "email", "value": "ada@example.test"},
        ],
        "opt_outs": [
            {"ats": "*", "label": "Cover Letter", "kind": "textarea"},
        ],
        "review_order": [
            {"ats": "*", "name": "email", "kind": "email"},
        ],
    }
    value.update(overrides)
    return value


def _write_preferences(path: Path, document: dict[str, object]) -> None:
    path.write_text(json.dumps(document), encoding="utf-8")


def test_loader_returns_versioned_immutable_document_and_exact_matchers(tmp_path: Path) -> None:
    path = tmp_path / "preferences.json"
    _write_preferences(path, _document())
    loaded = load_application_preferences(path, cwd=tmp_path)
    assert isinstance(loaded, ApplicationPreferences)
    assert loaded.schema_version == 1
    assert loaded.mappings[0].value == "ada@example.test"
    assert loaded.review_order[0].matcher_id == loaded.mappings[0].matcher_id
    assert loaded.source_sha256 == hashlib.sha256(path.read_bytes()).hexdigest()
    with pytest.raises(FrozenInstanceError):
        loaded.schema_version = 2  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        loaded.mappings[0].value = "other@example.test"  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        loaded.source_sha256 = "0" * 64  # type: ignore[misc]

    assert load_application_preferences(None, cwd=tmp_path).source_sha256 is None
    with pytest.raises(PreferenceValidationError, match="source_sha256"):
        ApplicationPreferences(1, (), (), (), "not-a-sha256")


def test_loader_rejects_unknown_keys_version_duplicates_malformed_and_caps(tmp_path: Path) -> None:
    path = tmp_path / "preferences.json"
    for document in (
        {**_document(), "extra": True},
        {**_document(), "schema_version": 2},
        {
            **_document(),
            "mappings": [
                {"ats": "*", "name": "email", "kind": "email", "value": "a@example.test"},
                {"ats": "*", "name": "email", "kind": "email", "value": "b@example.test"},
            ],
        },
    ):
        _write_preferences(path, document)
        with pytest.raises(PreferenceValidationError):
            load_application_preferences(path, cwd=tmp_path)
    path.write_text('{"schema_version":1,"mappings":[],"opt_outs":[],"review_order":[],}', encoding="utf-8")
    with pytest.raises(PreferenceValidationError, match="malformed"):
        load_application_preferences(path, cwd=tmp_path)
    path.write_text(json.dumps(_document())[:-1] + (" " * (256 * 1024)), encoding="utf-8")
    with pytest.raises(PreferenceValidationError, match="size cap"):
        load_application_preferences(path, cwd=tmp_path)


def test_loader_rejects_traversal_symlink_nonregular_and_deep_json(tmp_path: Path) -> None:
    path = tmp_path / "preferences.json"
    _write_preferences(path, _document())
    with pytest.raises(PreferenceValidationError, match="traversal"):
        load_application_preferences("../preferences.json", cwd=tmp_path)
    outside = tmp_path.parent / "outside-preferences.json"
    _write_preferences(outside, _document())
    link = tmp_path / "link.json"
    link.symlink_to(outside)
    with pytest.raises(PreferenceValidationError, match="symlink"):
        load_application_preferences(link, cwd=tmp_path)
    with pytest.raises(PreferenceValidationError, match="regular"):
        load_application_preferences(tmp_path, cwd=tmp_path)
    deep: object = []
    for _ in range(10):
        deep = [deep]
    _write_preferences(path, {**_document(), "mappings": deep})
    with pytest.raises(PreferenceValidationError, match="depth|malformed"):
        load_application_preferences(path, cwd=tmp_path)


@pytest.mark.parametrize(
    "mapping",
    [
        {"ats": "*", "name": "resume", "kind": "file", "value": "x"},
        {"ats": "*", "name": "password", "kind": "password", "value": "x"},
        {"ats": "*", "name": "ssn", "kind": "text", "value": "123"},
        {"ats": "*", "kind": "text", "value": "broadcast"},
        {"ats": "*", "name": "field_12", "kind": "text", "value": "opaque"},
    ],
)
def test_sensitive_blocked_and_wildcard_only_mappings_reject(tmp_path: Path, mapping: dict[str, object]) -> None:
    path = tmp_path / "preferences.json"
    _write_preferences(path, _document(mappings=[mapping]))
    with pytest.raises(PreferenceValidationError):
        load_application_preferences(path, cwd=tmp_path)


def test_mapping_answer_uses_complete_value_validator_and_never_bypasses_safety() -> None:
    safe = ApplicationPreferences(
        1,
        (PreferenceMapping("greenhouse", "email", None, "email", "not-an-email"),),
        (),
        (),
    )
    assert mapping_answer(safe, _field("email", kind="email", name="email", required=True)) is None
    sensitive = _field("ssn", name="ssn", descriptors=("social security number",))
    with pytest.raises(PreferenceValidationError):
        mapping_answer(
            ApplicationPreferences(1, (PreferenceMapping("*", "ssn", None, "text", "123"),), (), ()),
            sensitive,
        )


def test_optouts_report_manual_or_skipped_and_never_authorize() -> None:
    preferences = ApplicationPreferences(
        1,
        (PreferenceMapping("*", "email", None, "email", "ada@example.test"),),
        (PreferenceOptOut("*", "nickname", None, "text"), PreferenceOptOut("*", "phone", None, "tel")),
        (),
    )
    result = apply_preferences(
        preferences,
        (
            _field("email", kind="email", name="email", required=True),
            _field("nickname", kind="text", name="nickname", required=True),
            _field("phone", kind="tel", name="phone"),
        ),
    )
    assert [answer.target_id for answer in result.selected_answers] == ["email"]
    assert result.manual_target_ids == ("nickname",)
    assert result.skipped_target_ids == ("phone",)
    assert result.ordered_target_ids == ("email",)

def test_lever_mapping_uses_shared_supported_ats() -> None:
    preferences = ApplicationPreferences(
        1,
        (PreferenceMapping("lever", "email", None, "email", "ada@example.test"),),
        (),
        (),
    )
    field = _field("email", kind="email", name="email", required=True)
    answer = mapping_answer(preferences, field, ats="lever")
    assert answer is not None
    assert answer.value == "ada@example.test"

def test_preferences_apply_descriptors_and_mappings_with_ats() -> None:
    preferences = ApplicationPreferences(
        1,
        (PreferenceMapping("lever", "email", None, "email", "ada@example.test"),),
        (),
        (PreferenceMatcher("lever", "email", None, "email"),),
    )
    descriptor = ObservedFieldDescriptor("email", "lever", "email", "", "email", True)
    mapping_field = {
        "target_id": "email",
        "ats": "lever",
        "name": "email",
        "kind": "email",
        "required": True,
    }
    descriptor_result = apply_preferences(preferences, (descriptor,), ats="lever")
    mapping_result = apply_preferences(preferences, (mapping_field,), ats="lever")
    assert descriptor_result.selected_answers[0].value == "ada@example.test"
    assert mapping_result.selected_answers[0].target_id == "email"
    assert order_actions(preferences, (mapping_field,), ats="lever") == ("email",)

def test_lever_review_matcher_uses_actual_ats_and_wildcard_remains() -> None:
    preferences = ApplicationPreferences(
        1,
        (),
        (),
        (
            PreferenceMatcher("lever", "email", None, "email"),
            PreferenceMatcher("*", "first_name", None, "text"),
        ),
    )
    fields = (
        _field("name", name="first_name"),
        _field("email", kind="email", name="email"),
    )
    answers = tuple(FieldAnswer(item.target_id, "value", 1.0, "existing", "configured") for item in fields)
    assert order_actions(preferences, answers, descriptors=fields, ats="lever") == ("email", "name")
    assert order_actions(preferences, answers, descriptors=fields, ats="greenhouse") == ("name", "email")


def test_apply_and_order_are_stable_and_only_reorder_existing_actions() -> None:
    email = PreferenceMatcher("*", "email", None, "email")
    name = PreferenceMatcher("*", "first_name", None, "text")
    preferences = ApplicationPreferences(
        1,
        (),
        (),
        (email, name),
    )
    fields = (
        _field("name", name="first_name"),
        _field("other", name="other"),
        _field("email", kind="email", name="email"),
    )
    answers = (
        FieldAnswer("name", "Ada", 1.0, "existing", "configured"),
        FieldAnswer("other", "x", 1.0, "existing", "configured"),
        FieldAnswer("email", "ada@example.test", 1.0, "existing", "configured"),
    )
    ordered = order_actions(preferences, answers, descriptors=fields)
    assert ordered == ("email", "name", "other")
    assert {answer.target_id: answer.value for answer in answers} == {
        "name": "Ada",
        "other": "x",
        "email": "ada@example.test",
    }
    assert len(ordered) == len(answers)
    applied = apply_preferences(preferences, fields, answers)
    assert applied.ordered_target_ids == ("email", "name", "other")
    assert {answer.target_id: answer.value for answer in applied.selected_answers} == {
        "name": "Ada",
        "other": "x",
        "email": "ada@example.test",
    }

def test_sensitive_opaque_final_actions_reject() -> None:
    preferences = ApplicationPreferences(1, (), (), ())
    for kind in ("file", "password", "hidden", "button", "final", "opaque"):
        with pytest.raises(PreferenceValidationError):
            order_actions(preferences, ({"target_id": kind, "kind": kind},))
    with pytest.raises(PreferenceValidationError):
        apply_preferences(preferences, (ObservedFieldDescriptor("x", "*", "x", "", "password", False),))
