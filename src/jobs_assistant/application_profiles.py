"""Secure, versioned application-profile preset loading.

Presets are deliberately separate from TheirStack source profiles.  A preset is
an explicitly named, local JSON document containing the payload accepted by
:func:`jobs_assistant.ats.load_application_profile` (without its filesystem
loader).  This module only validates and returns typed values; it does not
persist or publish profile data.
"""
from __future__ import annotations

import errno
import hashlib
import json
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from .ats import (
    MAX_PROFILE_BYTES,
    MAX_PROFILE_DEPTH,
    MAX_PROFILE_NODES,
    MAX_PROFILE_STRING_CHARS,
    ApplicationProfile,
    _read_fd_snapshot,
    _reject_duplicate_object_pairs,
    _reject_nonfinite_json,
    _validate_json_caps,
    parse_application_profile,
)

APPLICATION_PROFILE_SCHEMA_VERSION = 1
MAX_APPLICATION_PROFILE_PRESET_BYTES = MAX_PROFILE_BYTES
MAX_APPLICATION_PROFILE_PRESET_DEPTH = MAX_PROFILE_DEPTH
MAX_APPLICATION_PROFILE_PRESET_NODES = MAX_PROFILE_NODES
MAX_APPLICATION_PROFILE_PRESET_STRING_CHARS = MAX_PROFILE_STRING_CHARS
MAX_APPLICATION_PROFILE_PRESET_NAME_CHARS = 64
_PRESET_FILENAME_SUFFIX = ".json"
_PRESET_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")

__all__ = (
    "APPLICATION_PROFILE_SCHEMA_VERSION",
    "MAX_APPLICATION_PROFILE_PRESET_BYTES",
    "MAX_APPLICATION_PROFILE_PRESET_DEPTH",
    "MAX_APPLICATION_PROFILE_PRESET_NODES",
    "MAX_APPLICATION_PROFILE_PRESET_STRING_CHARS",
    "ApplicationProfilePreset",
    "ApplicationProfilePresetRegistry",
    "load_application_profile_preset",
    "validate_application_profile_preset_name",
)


@dataclass(frozen=True)
class ApplicationProfilePreset:
    """An explicitly named, validated application-profile preset."""

    name: str
    schema_version: int
    profile: ApplicationProfile
    source_sha256: str

    def __post_init__(self) -> None:
        if type(self.source_sha256) is not str or re.fullmatch(r"[0-9a-f]{64}", self.source_sha256) is None:
            raise ValueError("application profile preset source_sha256 is invalid")

def validate_application_profile_preset_name(name: str) -> str:
    """Validate the filename-safe preset identifier and return it unchanged."""

    if type(name) is not str:
        raise ValueError("application profile preset name must be a string")
    if not name or len(name) > MAX_APPLICATION_PROFILE_PRESET_NAME_CHARS:
        raise ValueError("application profile preset name is invalid")
    # Keep the name a single portable filename component.  In particular, do
    # not normalize separators: accepting them would make the requested name
    # and the path opened below disagree on platforms.
    if "\\" in name or "/" in name or "\x00" in name or name in {".", ".."}:
        raise ValueError("application profile preset name is invalid")
    if _PRESET_NAME_RE.fullmatch(name) is None:
        raise ValueError("application profile preset name is invalid")
    return name


def _secure_directory(directory: str | os.PathLike[str], *, cwd: str | os.PathLike[str]) -> Path:
    """Resolve and validate a configured preset directory without symlinks."""

    raw = Path(directory)
    if any(part == ".." for part in raw.parts):
        raise ValueError("application profile preset directory may not escape its base")
    try:
        cwd_path = Path(cwd).resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ValueError("application profile preset cwd is unavailable") from exc
    try:
        cwd_stat = cwd_path.stat()
    except OSError as exc:
        raise ValueError("application profile preset cwd is unavailable") from exc
    if not stat.S_ISDIR(cwd_stat.st_mode):
        raise ValueError("application profile preset cwd must be a directory")

    candidate = raw if raw.is_absolute() else cwd_path / raw
    # lstat every component, rather than calling resolve(), so a configured
    # symlink cannot silently redirect preset loading outside the configured
    # directory.  Existing ancestors such as /tmp may be root-owned; only the
    # configured final directory must belong to this process's effective user.
    parts = candidate.parts
    if not parts:
        raise ValueError("application profile preset directory is invalid")
    current = Path(parts[0])
    for part in parts[1:]:
        current /= part
        try:
            component = os.lstat(current)
        except FileNotFoundError:
            raise
        except OSError as exc:
            raise ValueError("application profile preset directory is unavailable") from exc
        if stat.S_ISLNK(component.st_mode):
            raise ValueError("application profile preset directory may not contain symlinks")
    try:
        final = os.lstat(candidate)
    except FileNotFoundError:
        raise
    if not stat.S_ISDIR(final.st_mode):
        raise ValueError("application profile preset directory must be a directory")
    if final.st_uid != os.geteuid():
        raise ValueError("application profile preset directory must be owned by the effective user")
    if candidate == Path(candidate.anchor):
        raise ValueError("application profile preset directory may not be filesystem root")
    return candidate

_OPEN_DIRECTORY_FLAGS = os.O_RDONLY
for _flag_name in ("O_DIRECTORY", "O_NOFOLLOW", "O_CLOEXEC"):
    _OPEN_DIRECTORY_FLAGS |= getattr(os, _flag_name, 0)
_OPEN_PRESET_FLAGS = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)


def _directory_components(directory: Path) -> tuple[str, ...]:
    return tuple(part for part in directory.parts if part not in (directory.anchor, ""))


def _open_configured_directory(directory: Path) -> int:
    """Open the configured directory by descriptor, never by a re-resolved path."""

    components = _directory_components(directory)
    if not components:
        raise ValueError("application profile preset directory may not be filesystem root")

    # Capture the expected identity of every component before opening.  Each
    # descriptor is then checked against this snapshot, so an ancestor swap
    # cannot redirect traversal into a different directory tree.
    expected: list[os.stat_result] = []
    current = Path(directory.anchor or "/")
    for component in components:
        current /= component
        try:
            expected.append(os.lstat(current))
        except OSError as exc:
            raise ValueError("application profile preset directory is unavailable") from exc

    try:
        fd = os.open(directory.anchor or "/", _OPEN_DIRECTORY_FLAGS)
    except OSError as exc:
        raise ValueError("application profile preset directory is unavailable") from exc
    try:
        for index, component in enumerate(components):
            try:
                child_fd = os.open(component, _OPEN_DIRECTORY_FLAGS, dir_fd=fd)
            except OSError as exc:
                raise ValueError("application profile preset directory is unavailable") from exc
            try:
                child_stat = os.fstat(child_fd)
                expected_stat = expected[index]
                if (
                    not stat.S_ISDIR(child_stat.st_mode)
                    or stat.S_ISLNK(expected_stat.st_mode)
                    or child_stat.st_dev != expected_stat.st_dev
                    or child_stat.st_ino != expected_stat.st_ino
                ):
                    raise ValueError("application profile preset directory changed while opening")
                if index == len(components) - 1 and child_stat.st_uid != os.geteuid():
                    raise ValueError("application profile preset directory must be owned by the effective user")
            except Exception:
                os.close(child_fd)
                raise
            os.close(fd)
            fd = child_fd
        return fd
    except Exception:
        os.close(fd)
        raise


def _open_preset_file(directory_fd: int, name: str) -> tuple[int, os.stat_result]:
    try:
        fd = os.open(f"{name}{_PRESET_FILENAME_SUFFIX}", _OPEN_PRESET_FLAGS, dir_fd=directory_fd)
    except OSError as exc:
        if exc.errno in {errno.ELOOP, errno.EMLINK}:
            raise ValueError("application profile preset JSON must not be a symlink") from exc
        raise
    try:
        metadata = os.fstat(fd)
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError("application profile preset JSON must be a regular file")
        if metadata.st_uid != os.geteuid():
            raise ValueError("application profile preset JSON must be owned by the effective user")
        if stat.S_IMODE(metadata.st_mode) & (stat.S_IWGRP | stat.S_IWOTH):
            raise ValueError("application profile preset JSON must not be group/world writable")
        if metadata.st_size > MAX_APPLICATION_PROFILE_PRESET_BYTES:
            raise ValueError("application profile preset JSON exceeds its size cap")
        return fd, metadata
    except Exception:
        os.close(fd)
        raise



def _profile_from_payload(payload: Mapping[str, Any]) -> ApplicationProfile:
    """Build ``ApplicationProfile`` through the canonical profile parser."""

    return parse_application_profile(payload)


def _decode_preset(raw: bytes, *, requested_name: str) -> ApplicationProfilePreset:
    source_sha256 = hashlib.sha256(raw).hexdigest()
    try:
        payload = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_object_pairs,
            parse_constant=_reject_nonfinite_json,
        )
    except UnicodeDecodeError as exc:
        raise ValueError("application profile preset JSON must be UTF-8") from exc
    except RecursionError as exc:
        raise ValueError("application profile preset JSON exceeds recursion limits") from exc
    if not isinstance(payload, dict):
        raise ValueError("application profile preset JSON must contain an object")
    _validate_json_caps(payload)

    expected_keys = {"schema_version", "name", "profile"}
    actual_keys = set(payload)
    if actual_keys != expected_keys:
        unknown = sorted(actual_keys - expected_keys)
        missing = sorted(expected_keys - actual_keys)
        detail = []
        if unknown:
            detail.append("unknown key(s): " + ", ".join(unknown))
        if missing:
            detail.append("missing key(s): " + ", ".join(missing))
        raise ValueError("application profile preset document has " + "; ".join(detail))

    schema_version = payload["schema_version"]
    if type(schema_version) is not int or schema_version != APPLICATION_PROFILE_SCHEMA_VERSION:
        raise ValueError("unknown application profile preset schema_version")
    document_name = validate_application_profile_preset_name(payload["name"])
    if document_name != requested_name:
        raise ValueError("application profile preset name does not match filename")
    profile_payload = payload["profile"]
    if not isinstance(profile_payload, dict):
        raise ValueError("application profile preset profile must be an object")
    profile = _profile_from_payload(profile_payload)
    return ApplicationProfilePreset(
        name=document_name,
        schema_version=APPLICATION_PROFILE_SCHEMA_VERSION,
        profile=profile,
        source_sha256=source_sha256,
    )


@dataclass(frozen=True)
class ApplicationProfilePresetRegistry:
    """A securely configured directory of explicitly named profile presets."""

    directory: Path

    @classmethod
    def open(
        cls,
        directory: str | os.PathLike[str],
        *,
        cwd: str | os.PathLike[str] | None = None,
    ) -> "ApplicationProfilePresetRegistry":
        base = Path.cwd() if cwd is None else cwd
        return cls(_secure_directory(directory, cwd=base))

    def load(self, name: str) -> ApplicationProfilePreset:
        requested_name = validate_application_profile_preset_name(name)
        directory_fd = _open_configured_directory(self.directory)
        try:
            fd, metadata = _open_preset_file(directory_fd, requested_name)
        finally:
            os.close(directory_fd)
        try:
            raw = _read_fd_snapshot(fd, metadata.st_size)
        finally:
            os.close(fd)
        return _decode_preset(raw, requested_name=requested_name)

    # ``resolve`` is a boring alias useful to callers that treat the registry
    # as a name resolver; it performs exactly the same validated load.
    def resolve(self, name: str) -> ApplicationProfilePreset:
        return self.load(name)


def load_application_profile_preset(
    directory: str | os.PathLike[str],
    name: str,
    *,
    cwd: str | os.PathLike[str] | None = None,
) -> ApplicationProfilePreset:
    """Load one validated ``<name>.json`` preset from ``directory``."""

    return ApplicationProfilePresetRegistry.open(directory, cwd=cwd).load(name)
