from __future__ import annotations

import hashlib
import json
import stat
from pathlib import Path
from types import MappingProxyType

import pytest


import jobs_assistant.application_profiles as profiles
import jobs_assistant.ats as ats
from jobs_assistant.ats import ApplicationProfile, ConfiguredFieldAnswer
from jobs_assistant.application_profiles import (
    APPLICATION_PROFILE_SCHEMA_VERSION,
    MAX_APPLICATION_PROFILE_PRESET_BYTES,
    MAX_APPLICATION_PROFILE_PRESET_DEPTH,
    MAX_APPLICATION_PROFILE_PRESET_NODES,
    MAX_APPLICATION_PROFILE_PRESET_STRING_CHARS,
    ApplicationProfilePreset,
    ApplicationProfilePresetRegistry,
    load_application_profile_preset,
)


def _document(name: str = "default", profile: dict[str, object] | None = None) -> dict[str, object]:
    return {
        "schema_version": APPLICATION_PROFILE_SCHEMA_VERSION,
        "name": name,
        "profile": profile if profile is not None else {"first_name": "Ada", "resume_summary": "Engine work"},
    }


def _write(
    directory: Path,
    name: str = "default",
    document: dict[str, object] | None = None,
    profile: dict[str, object] | None = None,
) -> Path:
    path = directory / f"{name}.json"
    if document is None:
        document = _document(name, profile)
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def test_valid_preset_is_frozen_and_contains_existing_application_profile(tmp_path: Path) -> None:
    directory = tmp_path / "presets"
    directory.mkdir()
    _write(
        directory,
        profile={
            "first_name": "Ada",
            "resume_summary": "Engine work",
            "field_answers": [
                {"ats": "greenhouse", "name": "years", "kind": "text", "value": "10"},
            ],
        },
    )

    preset = load_application_profile_preset(directory, "default", cwd=tmp_path)
    source_sha256 = hashlib.sha256((directory / "default.json").read_bytes()).hexdigest()

    assert isinstance(preset, ApplicationProfilePreset)
    assert preset.source_sha256 == source_sha256
    assert preset == ApplicationProfilePreset(
        name="default",
        schema_version=APPLICATION_PROFILE_SCHEMA_VERSION,
        profile=ApplicationProfile(
            facts={"first_name": "Ada"},
            description="Engine work",
            field_answers=(ConfiguredFieldAnswer("greenhouse", "years", None, "text", "10"),),
        ),
        source_sha256=source_sha256,
    )
    assert isinstance(preset.profile.facts, MappingProxyType)
    with pytest.raises(TypeError):
        preset.profile.facts["first_name"] = "Grace"  # type: ignore[index]

    with pytest.raises(ValueError, match="source_sha256"):
        ApplicationProfilePreset(
            name="default",
            schema_version=APPLICATION_PROFILE_SCHEMA_VERSION,
            profile=ApplicationProfile(),
            source_sha256="not-a-sha256",
        )


def test_registry_open_resolves_only_configured_directory_and_rejects_traversal(tmp_path: Path) -> None:
    directory = tmp_path / "presets"
    directory.mkdir()
    _write(directory)
    registry = ApplicationProfilePresetRegistry.open("presets", cwd=tmp_path)
    assert registry.load("default").name == "default"
    for name in ("", ".", "..", "../default", "nested/default", r"nested\\default", "bad name", "bad.json"):
        with pytest.raises(ValueError, match="name"):
            registry.load(name)


def test_unknown_or_missing_version_and_unknown_document_key_are_rejected(tmp_path: Path) -> None:
    directory = tmp_path / "presets"
    directory.mkdir()
    unknown = _document()
    unknown["schema_version"] = 2
    _write(directory, document=unknown)
    with pytest.raises(ValueError, match="schema_version"):
        load_application_profile_preset(directory, "default", cwd=tmp_path)

    missing = _document()
    del missing["schema_version"]
    _write(directory, document=missing)
    with pytest.raises(ValueError, match="missing"):
        load_application_profile_preset(directory, "default", cwd=tmp_path)

    extra = _document()
    extra["source_profile"] = "new_grad_cs"
    _write(directory, document=extra)
    with pytest.raises(ValueError, match="unknown"):
        load_application_profile_preset(directory, "default", cwd=tmp_path)


def test_duplicate_and_nonfinite_json_are_rejected(tmp_path: Path) -> None:
    directory = tmp_path / "presets"
    directory.mkdir()
    duplicate = directory / "default.json"
    duplicate.write_text(
        '{"schema_version":1,"name":"default","name":"other","profile":{}}',
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="duplicate"):
        load_application_profile_preset(directory, "default", cwd=tmp_path)

    duplicate.write_text('{"schema_version":1,"name":"default","profile":{"score":NaN}}', encoding="utf-8")
    with pytest.raises(ValueError, match="non-finite"):
        load_application_profile_preset(directory, "default", cwd=tmp_path)


def test_oversize_depth_node_and_string_caps_are_rejected(tmp_path: Path) -> None:
    directory = tmp_path / "presets"
    directory.mkdir()

    too_big = directory / "default.json"
    too_big.write_bytes(b"x" * (MAX_APPLICATION_PROFILE_PRESET_BYTES + 1))
    with pytest.raises(ValueError, match="cap"):
        load_application_profile_preset(directory, "default", cwd=tmp_path)

    nested: object = "x"
    for _ in range(MAX_APPLICATION_PROFILE_PRESET_DEPTH + 1):
        nested = {"x": nested}
    _write(directory, document=_document(profile={"nested": nested}))
    with pytest.raises(ValueError, match="depth"):
        load_application_profile_preset(directory, "default", cwd=tmp_path)

    _write(directory, document=_document(profile={"many": [0] * (MAX_APPLICATION_PROFILE_PRESET_NODES + 1)}))
    with pytest.raises(ValueError, match="node"):
        load_application_profile_preset(directory, "default", cwd=tmp_path)

    _write(directory, document=_document(profile={"long": "x" * (MAX_APPLICATION_PROFILE_PRESET_STRING_CHARS + 1)}))
    with pytest.raises(ValueError, match="string"):
        load_application_profile_preset(directory, "default", cwd=tmp_path)


def test_symlink_nonregular_and_filename_name_mismatch_are_rejected(tmp_path: Path) -> None:
    directory = tmp_path / "presets"
    directory.mkdir()
    outside = tmp_path / "outside.json"
    _write(tmp_path, name="outside")
    link = directory / "default.json"
    link.symlink_to(outside)
    with pytest.raises(ValueError, match="regular|symlink"):
        load_application_profile_preset(directory, "default", cwd=tmp_path)

    link.unlink()
    directory_path = directory / "default.json"
    directory_path.mkdir()
    with pytest.raises(ValueError, match="regular|directory"):
        load_application_profile_preset(directory, "default", cwd=tmp_path)

    directory_path.rmdir()
    _write(directory, document=_document(name="other"))
    with pytest.raises(ValueError, match="match|name"):
        load_application_profile_preset(directory, "default", cwd=tmp_path)

    symlinked_directory = tmp_path / "preset-link"
    symlinked_directory.symlink_to(directory, target_is_directory=True)
    with pytest.raises(ValueError, match="symlink"):
        ApplicationProfilePresetRegistry.open(symlinked_directory, cwd=tmp_path)


def test_ancestor_swap_during_dirfd_traversal_never_loads_outside_target(monkeypatch, tmp_path: Path) -> None:
    anchor = tmp_path / "anchor"
    directory = anchor / "presets"
    outside = tmp_path / "outside"
    directory.mkdir(parents=True)
    outside.mkdir()
    _write(directory, profile={"first_name": "Inside"})
    _write(outside, profile={"first_name": "Outside"})
    registry = ApplicationProfilePresetRegistry.open(directory, cwd=tmp_path)
    anchor_backup = tmp_path / "anchor-real"
    swapped = False
    real_open = profiles.os.open

    def race_open(path, flags, mode=0o777, *, dir_fd=None):
        nonlocal swapped
        if path == "anchor" and dir_fd is not None and not swapped:
            anchor.rename(anchor_backup)
            anchor.symlink_to(outside, target_is_directory=True)
            swapped = True
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(profiles.os, "open", race_open)
    try:
        with pytest.raises(ValueError, match="changed|unavailable|symlink"):
            registry.load("default")
    finally:
        if anchor.is_symlink():
            anchor.unlink()
        if anchor_backup.exists():
            anchor_backup.rename(anchor)


def test_final_swap_during_openat_never_loads_outside_target(monkeypatch, tmp_path: Path) -> None:
    directory = tmp_path / "presets"
    outside = tmp_path / "outside.json"
    directory.mkdir()
    _write(directory, profile={"first_name": "Inside"})
    _write(tmp_path, name="outside", profile={"first_name": "Outside"})
    registry = ApplicationProfilePresetRegistry.open(directory, cwd=tmp_path)
    preset = directory / "default.json"
    preset_backup = directory / "default-real.json"
    swapped = False
    real_open = profiles.os.open

    def race_open(path, flags, mode=0o777, *, dir_fd=None):
        nonlocal swapped
        if path == "default.json" and dir_fd is not None and not swapped:
            preset.rename(preset_backup)
            preset.symlink_to(outside)
            swapped = True
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(profiles.os, "open", race_open)
    try:
        with pytest.raises(ValueError, match="symlink"):
            registry.load("default")
    finally:
        if preset.is_symlink():
            preset.unlink()
        if preset_backup.exists():
            preset_backup.rename(preset)

def test_source_profile_names_and_types_are_not_accepted_implicitly(tmp_path: Path) -> None:
    directory = tmp_path / "presets"
    directory.mkdir()
    with pytest.raises(FileNotFoundError):
        load_application_profile_preset(directory, "new_grad_cs", cwd=tmp_path)

    _write(directory, document=_document(profile="new_grad_cs"))  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="object"):
        load_application_profile_preset(directory, "default", cwd=tmp_path)
