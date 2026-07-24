"""Strict, deterministic resume generation for queued jobs.

This module owns structured-profile validation, evidence-grounded optimization,
LaTeX rendering, bounded compilation, one-page fitting, and private artifact
publication.  It deliberately does not access the database or submit jobs.
"""

from __future__ import annotations

import errno
import hashlib
import io
import json
import math
import os
import re
import selectors
import signal
import stat
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from urllib.parse import urlsplit
from typing import Any, Mapping, Sequence

__all__ = [
    "GeneratedResume",
    "ResumeJob",
    "generate_resume",
    "load_resume_profile",
    "optimize_resume",
]

# These limits apply before parsing and before subprocess invocation.  They are
# intentionally conservative because profile and job data are user-controlled.
_GENERATOR_SCHEMA_VERSION = "resume-generator-v4"
_PROFILE_SCHEMA_VERSION = 1
_MAX_PROFILE_BYTES = 256 * 1024
_MAX_TEMPLATE_BYTES = 512 * 1024
_MAX_SKILL_BYTES = 128 * 1024
_MAX_DESCRIPTION_CHARS = 12_000
_MAX_RESUME_TEX_BYTES = 512 * 1024
_MAX_RESUME_PDF_BYTES = 8 * 1024 * 1024
_MAX_OPTIMIZATION_REPORT_BYTES = 512 * 1024
_MAX_JOB_DESCRIPTION_BYTES = _MAX_DESCRIPTION_CHARS * 4
_MAX_MANIFEST_BYTES = 256 * 1024
_MAX_STAGE_BYTES = 16 * 1024 * 1024
_MAX_PROFILE_DEPTH = 8
_MAX_PROFILE_NODES = 4_000
_MAX_PROFILE_STRING_CHARS = 20_000
_MAX_JOB_TITLE_CHARS = 512
_MAX_JOB_COMPANY_CHARS = 512
_MAX_JOB_LOCATION_CHARS = 512
_MAX_JOB_POSTED_AT_CHARS = 128
_MAX_COMPILER_OUTPUT_BYTES = 32 * 1024
_COMPILE_TIMEOUT_SECONDS = 60
_TRIM_TIMEOUT_SECONDS = 60
_MAX_PROJECTS = 3
_MAX_PROJECT_BULLETS = 4
_MAX_LEADERSHIP = 2
_MAX_SKILLS = 20
_MIN_SKILLS_LINE_CHARS = 60
_MAX_EXPANSION_ATTEMPTS = 64

_ALGORITHM_DESCRIPTOR = (
    "title-first-requirements-field-selection-v3|source-backed-claims|graduation-render-v2|"
    "experience-first-fill-v2|tectonic-pdflatex-bounded-argv-v1|"
    "private-five-artifact-cache-v3|strict-profile-v1-header-metadata-v4|"
    "authoritative-resume-skill-v2"
)
_ALGORITHM_SHA256 = hashlib.sha256(_ALGORITHM_DESCRIPTOR.encode("ascii")).hexdigest()

_LATEX_ESCAPE_TABLE = str.maketrans(
    {
        "\\": r"\textbackslash{}",
        "&": r"\&",
        "%": r"\%",
        "$": r"\$",
        "#": r"\#",
        "_": r"\_",
        "{": r"\{",
        "}": r"\}",
        "~": r"\textasciitilde{}",
        "^": r"\textasciicircum{}",
    }
)


def _latex_escape(text: str) -> str:
    """Escape ordinary LaTeX text; never pass profile text as commands."""
    return text.translate(_LATEX_ESCAPE_TABLE)


def _latex_escape_url(url: str) -> str:
    """Escape a URL for a hyperref argument without introducing commands."""
    return url.translate(_LATEX_ESCAPE_TABLE)


def _nonempty_string(value: Any, label: str) -> str:
    if type(value) is not str or not value.strip():
        raise ValueError(f"{label} must be a non-empty string")
    if "\x00" in value:
        raise ValueError(f"{label} contains NUL")
    return value


def _string(value: Any, label: str, *, nonempty: bool = False) -> str:
    if type(value) is not str:
        raise ValueError(f"{label} must be a string")
    if nonempty and not value.strip():
        raise ValueError(f"{label} must be a non-empty string")
    if "\x00" in value:
        raise ValueError(f"{label} contains NUL")
    return value


def _safe_url(value: Any, label: str, *, nonempty: bool = False) -> str:
    """Accept only printable HTTP(S) links suitable for a hyperref target."""
    value = _string(value, label, nonempty=nonempty)
    if not value:
        return value
    if any(character.isspace() or ord(character) < 0x20 or ord(character) == 0x7F for character in value):
        raise ValueError(f"{label} must not contain whitespace or control characters")
    if "\\" in value:
        raise ValueError(f"{label} must not contain a backslash")
    try:
        parsed = urlsplit(value)
    except ValueError as exc:
        raise ValueError(f"{label} is not a valid URL: {exc}") from exc
    if parsed.scheme.casefold() not in {"http", "https"} or not parsed.netloc or parsed.hostname is None:
        raise ValueError(f"{label} must be an absolute HTTP(S) URL")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError(f"{label} must not contain URL credentials")
    return value


def _sha256_string(value: Any, label: str) -> str:
    value = _nonempty_string(value, label)
    if not re.fullmatch(r"[0-9a-fA-F]{64}", value):
        raise ValueError(f"{label} must be a 64-character SHA-256 hex digest")
    return value

def _email_address(value: Any, label: str) -> str:
    value = _nonempty_string(value, label)
    if any(character.isspace() or ord(character) < 0x20 or ord(character) == 0x7F for character in value):
        raise ValueError(f"{label} must not contain whitespace or control characters")
    if "\\" in value or value.count("@") != 1:
        raise ValueError(f"{label} must be a valid email address")
    local, domain = value.rsplit("@", 1)
    if not local or not domain or domain.startswith(".") or domain.endswith(".") or ".." in domain:
        raise ValueError(f"{label} must be a valid email address")
    return value


def _list(value: Any, label: str) -> list[Any]:
    if type(value) is not list:
        raise ValueError(f"{label} must be a list")
    return value


def _dict(value: Any, label: str) -> dict[str, Any]:
    if type(value) is not dict:
        raise ValueError(f"{label} must be an object")
    return value


@dataclass(frozen=True)
class ResumeJob:
    """A single immutable backlog row selected for generation."""

    id: int
    title: str
    company: str
    description: str
    location: str | None = None
    posted_at: str | None = None

    def __post_init__(self) -> None:
        if type(self.id) is not int or self.id <= 0:
            raise ValueError("ResumeJob.id must be a positive integer")
        for name, value, limit in (
            ("title", self.title, _MAX_JOB_TITLE_CHARS),
            ("company", self.company, _MAX_JOB_COMPANY_CHARS),
            ("description", self.description, _MAX_DESCRIPTION_CHARS),
        ):
            if type(value) is not str or not value.strip():
                raise ValueError(f"ResumeJob.{name} must be a non-empty string")
            if len(value) > limit:
                raise ValueError(f"ResumeJob.{name} exceeds {limit} characters")
            if "\x00" in value:
                raise ValueError(f"ResumeJob.{name} contains NUL")
        for name, value, limit in (
            ("location", self.location, _MAX_JOB_LOCATION_CHARS),
            ("posted_at", self.posted_at, _MAX_JOB_POSTED_AT_CHARS),
        ):
            if value is not None:
                if type(value) is not str:
                    raise ValueError(f"ResumeJob.{name} must be a string or None")
                if len(value) > limit or "\x00" in value:
                    raise ValueError(f"ResumeJob.{name} exceeds its safety bound")


@dataclass(frozen=True)
class GeneratedResume:
    """Paths and deterministic optimization facts for a generated resume."""

    job_id: int
    artifact_ref: str
    tex_path: Path
    pdf_path: Path
    report_path: Path
    pages: int
    field: str
    graduation_date: str
    matched_keywords: tuple[str, ...]


@dataclass(frozen=True)
class _DateRange:
    start: str
    end: str
    display: str


@dataclass(frozen=True)
class _Bullet:
    id: str
    text: str
    keywords: tuple[str, ...]
    sources: tuple[str, ...]


@dataclass(frozen=True)
class _ExperienceEntry:
    id: str
    title: str
    organization: str
    location: str
    dates: _DateRange
    bullets: tuple[_Bullet, ...]
    keywords: tuple[str, ...]
    sources: tuple[str, ...]


@dataclass(frozen=True)
class _LeadershipEntry:
    id: str
    title: str
    organization: str
    location: str
    dates: _DateRange
    bullets: tuple[_Bullet, ...]
    keywords: tuple[str, ...]
    sources: tuple[str, ...]


@dataclass(frozen=True)
class _GraduationRule:
    id: str
    value: str
    all_keyword_groups: tuple[tuple[str, ...], ...]
    sources: tuple[str, ...]


@dataclass(frozen=True)
class _Graduation:
    default: str
    rules: tuple[_GraduationRule, ...]


@dataclass(frozen=True)
class _EducationEntry:
    id: str
    institution: str
    location: str
    degree: str
    dates: _DateRange
    graduation: _Graduation
    keywords: tuple[str, ...]
    sources: tuple[str, ...]


@dataclass(frozen=True)
class _SkillEntry:
    name: str
    keywords: tuple[str, ...]
    sources: tuple[str, ...]

@dataclass(frozen=True)
class _ProjectEntry:
    id: str
    name: str
    link: str
    dates: _DateRange
    technologies: tuple[str, ...]
    bullets: tuple[_Bullet, ...]
    keywords: tuple[str, ...]
    sources: tuple[str, ...]
    enabled: bool

@dataclass(frozen=True)
class _Contact:
    full_name: str
    phone: str
    email: str


@dataclass(frozen=True)
class _Links:
    linkedin: str
    github: str
    website: str

@dataclass(frozen=True)
class _Source:
    id: str
    type: str
    location: str
    sha256: str
    retrieved_at: str
    notes: str


@dataclass(frozen=True)
class _PublicRepo:
    name: str
    url: str
    description: str
    primary_language: str
    fork: bool
    created_at: str
    updated_at: str
    sources: tuple[str, ...]
    resume_eligible: bool


@dataclass(frozen=True)
class _OpenQuestion:
    id: str
    question: str
    reason: str
    status: str


@dataclass(frozen=True)
class _Others:
    contact: _Contact
    links: _Links
    sources: tuple[_Source, ...]
    public_repositories: tuple[_PublicRepo, ...]
    open_questions: tuple[_OpenQuestion, ...]


@dataclass(frozen=True)
class ResumeProfile:
    """Fully parsed immutable structured profile v1."""

    schema_version: int
    skills: Mapping[str, tuple[_SkillEntry, ...]]
    experience: tuple[_ExperienceEntry, ...]
    leadership: tuple[_LeadershipEntry, ...]
    education: tuple[_EducationEntry, ...]
    projects: tuple[_ProjectEntry, ...]
    others: _Others


@dataclass(frozen=True)
class _Selection:
    experience: tuple[tuple[str, tuple[str, ...]], ...]
    leadership: tuple[tuple[str, tuple[str, ...]], ...]
    projects: tuple[tuple[str, tuple[str, ...]], ...]
    primary_experience: str | None


@dataclass(frozen=True)
class ResumePlan:
    """Deterministic render plan (kept private by the public API boundary)."""

    field: str
    graduation_date: str
    header_text: str
    sections: tuple[tuple[str, str], ...]
    matched_keywords: tuple[str, ...]
    unsupported_keywords: tuple[str, ...]
    compressed_skills: tuple[str, ...]
    selection: _Selection | None = None
    evidence_inventory: tuple[str, ...] = ()
    requirement_terms: tuple[str, ...] = ()
    job_terms: tuple[str, ...] = ()
    coverage_ratio: float = 0.0
    graduation_rule: str | None = None
    selected_claims: tuple[tuple[str, tuple[str, ...]], ...] = ()

# ---------------------------------------------------------------------------
# Bounded, no-symlink input snapshots
# ---------------------------------------------------------------------------


def _open_regular(path: Path, max_bytes: int, description: str) -> tuple[int, os.stat_result]:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(os.fspath(path), flags)
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise ValueError(f"{description} must not be a symlink") from exc
        if exc.errno == errno.ENOENT:
            raise FileNotFoundError(f"{description} not found: {path}") from exc
        raise ValueError(f"cannot open {description}: {exc}") from exc
    try:
        initial = os.fstat(fd)
        if not stat.S_ISREG(initial.st_mode):
            raise ValueError(f"{description} must be a regular file")
        if initial.st_size < 0 or initial.st_size > max_bytes:
            raise ValueError(f"{description} exceeds {max_bytes} bytes")
        return fd, initial
    except Exception:
        os.close(fd)
        raise


def _read_fd_snapshot(fd: int, initial: os.stat_result, description: str) -> bytes:
    remaining = initial.st_size
    chunks: list[bytes] = []
    while remaining:
        try:
            chunk = os.read(fd, min(64 * 1024, remaining))
        except InterruptedError:
            continue
        if not chunk:
            raise ValueError(f"{description} changed during read")
        if len(chunk) > remaining:
            raise ValueError(f"{description} changed during read")
        chunks.append(chunk)
        remaining -= len(chunk)
    try:
        final = os.fstat(fd)
    except OSError as exc:
        raise ValueError(f"cannot stat {description} after read: {exc}") from exc
    if (
        final.st_dev,
        final.st_ino,
        final.st_size,
        final.st_mtime_ns,
        final.st_ctime_ns,
    ) != (
        initial.st_dev,
        initial.st_ino,
        initial.st_size,
        initial.st_mtime_ns,
        initial.st_ctime_ns,
    ):
        raise ValueError(f"{description} changed during read")
    return b"".join(chunks)


def _snapshot_regular(path: Path, max_bytes: int, description: str) -> bytes:
    fd, initial = _open_regular(path, max_bytes, description)
    try:
        return _read_fd_snapshot(fd, initial, description)
    finally:
        os.close(fd)

def _validate_skill_bytes(skill_bytes: bytes, skill_path: Path) -> str:
    try:
        text = skill_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"resume skill is not valid UTF-8: {exc}") from exc
    required = (
        "# Resume Generation Skill",
        "## Source-of-truth policy",
        "## Output invariants",
    )
    if any(marker not in text for marker in required) or re.search(r"(?m)^Version: [12]$", text) is None:
        raise ValueError(f"resume skill is malformed: {skill_path}")
    return _sha256_hex(skill_bytes)


def _check_no_duplicate_keys(raw: str) -> None:
    duplicates: list[str] = []

    def hook(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        seen: set[str] = set()
        for key, _value in pairs:
            if key in seen:
                duplicates.append(key)
            seen.add(key)
        return dict(pairs)

    try:
        json.loads(raw, object_pairs_hook=hook)
    except json.JSONDecodeError:
        return
    if duplicates:
        names = ", ".join(sorted(set(duplicates)))
        raise ValueError(f"duplicate key(s) in profile: {names}")


def _validate_json_caps(value: Any) -> None:
    nodes = 0

    def walk(item: Any, depth: int) -> None:
        nonlocal nodes
        nodes += 1
        if nodes > _MAX_PROFILE_NODES:
            raise ValueError("profile JSON exceeds node cap")
        if depth > _MAX_PROFILE_DEPTH:
            raise ValueError("profile JSON exceeds depth cap")
        if type(item) is str:
            if len(item) > _MAX_PROFILE_STRING_CHARS:
                raise ValueError("profile JSON string exceeds size cap")
        elif type(item) is float:
            if not math.isfinite(item):
                raise ValueError("profile JSON contains non-finite number")
        elif type(item) is dict:
            for key, child in item.items():
                if type(key) is not str:
                    raise ValueError("profile JSON object keys must be strings")
                if len(key) > _MAX_PROFILE_STRING_CHARS:
                    raise ValueError("profile JSON object key exceeds string size cap")
                walk(child, depth + 1)
        elif type(item) is list:
            for child in item:
                walk(child, depth + 1)
        elif item is None or type(item) in (bool, int):
            return
        else:
            raise ValueError(f"profile JSON contains unsupported value: {type(item).__name__}")

    walk(value, 0)


def _check_exact_keys(obj: dict[str, Any], expected: frozenset[str], label: str) -> None:
    actual = set(obj)
    if actual == expected:
        return
    unknown = sorted(actual - expected)
    missing = sorted(expected - actual)
    details: list[str] = []
    if unknown:
        details.append(f"unknown key(s) in {label}: {', '.join(unknown)}")
    if missing:
        details.append(f"missing key(s) in {label}: {', '.join(missing)}")
    raise ValueError("; ".join(details))


def _parse_string_list(value: Any, label: str, *, nonempty: bool = False) -> tuple[str, ...]:
    values = _list(value, label)
    parsed: list[str] = []
    for i, item in enumerate(values):
        parsed.append(_nonempty_string(item, f"{label}[{i}]") if nonempty else _string(item, f"{label}[{i}]"))
    if nonempty and not parsed:
        raise ValueError(f"{label} must not be empty")
    return tuple(parsed)


def _parse_date_range(obj: Any, label: str) -> _DateRange:
    obj = _dict(obj, label)
    _check_exact_keys(obj, frozenset({"start", "end", "display"}), label)
    return _DateRange(
        start=_nonempty_string(obj["start"], f"{label}.start"),
        end=_nonempty_string(obj["end"], f"{label}.end"),
        display=_nonempty_string(obj["display"], f"{label}.display"),
    )


def _parse_bullet(obj: Any, label: str) -> _Bullet:
    obj = _dict(obj, label)
    _check_exact_keys(obj, frozenset({"id", "text", "keywords", "sources"}), label)
    return _Bullet(
        id=_nonempty_string(obj["id"], f"{label}.id"),
        text=_nonempty_string(obj["text"], f"{label}.text"),
        keywords=_parse_string_list(obj["keywords"], f"{label}.keywords", nonempty=True),
        sources=_parse_string_list(obj["sources"], f"{label}.sources", nonempty=True),
    )


def _parse_experience_entry(obj: Any, label: str) -> _ExperienceEntry:
    obj = _dict(obj, label)
    expected = frozenset({"id", "title", "organization", "location", "dates", "bullets", "keywords", "sources"})
    _check_exact_keys(obj, expected, label)
    bullets = tuple(_parse_bullet(v, f"{label}.bullets[{i}]") for i, v in enumerate(_list(obj["bullets"], f"{label}.bullets")))
    return _ExperienceEntry(
        id=_nonempty_string(obj["id"], f"{label}.id"),
        title=_nonempty_string(obj["title"], f"{label}.title"),
        organization=_nonempty_string(obj["organization"], f"{label}.organization"),
        location=_nonempty_string(obj["location"], f"{label}.location"),
        dates=_parse_date_range(obj["dates"], f"{label}.dates"),
        bullets=bullets,
        keywords=_parse_string_list(obj["keywords"], f"{label}.keywords", nonempty=True),
        sources=_parse_string_list(obj["sources"], f"{label}.sources", nonempty=True),
    )


def _parse_leadership_entry(obj: Any, label: str) -> _LeadershipEntry:
    obj = _dict(obj, label)
    expected = frozenset({"id", "title", "organization", "location", "dates", "bullets", "keywords", "sources"})
    _check_exact_keys(obj, expected, label)
    bullets = tuple(_parse_bullet(v, f"{label}.bullets[{i}]") for i, v in enumerate(_list(obj["bullets"], f"{label}.bullets")))
    return _LeadershipEntry(
        id=_nonempty_string(obj["id"], f"{label}.id"),
        title=_nonempty_string(obj["title"], f"{label}.title"),
        organization=_nonempty_string(obj["organization"], f"{label}.organization"),
        location=_nonempty_string(obj["location"], f"{label}.location"),
        dates=_parse_date_range(obj["dates"], f"{label}.dates"),
        bullets=bullets,
        keywords=_parse_string_list(obj["keywords"], f"{label}.keywords", nonempty=True),
        sources=_parse_string_list(obj["sources"], f"{label}.sources", nonempty=True),
    )


def _parse_graduation(obj: Any, label: str) -> _Graduation:
    obj = _dict(obj, label)
    _check_exact_keys(obj, frozenset({"default", "rules"}), label)
    rules: list[_GraduationRule] = []
    for i, value in enumerate(_list(obj["rules"], f"{label}.rules")):
        rule_label = f"{label}.rules[{i}]"
        rule = _dict(value, rule_label)
        _check_exact_keys(rule, frozenset({"id", "value", "all_keyword_groups", "sources"}), rule_label)
        groups: list[tuple[str, ...]] = []
        for j, group in enumerate(_list(rule["all_keyword_groups"], f"{rule_label}.all_keyword_groups")):
            groups.append(_parse_string_list(group, f"{rule_label}.all_keyword_groups[{j}]", nonempty=True))
        if not groups:
            raise ValueError(f"{rule_label}.all_keyword_groups must not be empty")
        rules.append(
            _GraduationRule(
                id=_nonempty_string(rule["id"], f"{rule_label}.id"),
                value=_nonempty_string(rule["value"], f"{rule_label}.value"),
                all_keyword_groups=tuple(groups),
                sources=_parse_string_list(rule["sources"], f"{rule_label}.sources", nonempty=True),
            )
        )
    return _Graduation(
        default=_nonempty_string(obj["default"], f"{label}.default"),
        rules=tuple(rules),
    )


def _parse_education_entry(obj: Any, label: str) -> _EducationEntry:
    obj = _dict(obj, label)
    expected = frozenset({"id", "institution", "location", "degree", "dates", "graduation", "keywords", "sources"})
    _check_exact_keys(obj, expected, label)
    return _EducationEntry(
        id=_nonempty_string(obj["id"], f"{label}.id"),
        institution=_nonempty_string(obj["institution"], f"{label}.institution"),
        location=_nonempty_string(obj["location"], f"{label}.location"),
        degree=_nonempty_string(obj["degree"], f"{label}.degree"),
        dates=_parse_date_range(obj["dates"], f"{label}.dates"),
        graduation=_parse_graduation(obj["graduation"], f"{label}.graduation"),
        keywords=_parse_string_list(obj["keywords"], f"{label}.keywords", nonempty=True),
        sources=_parse_string_list(obj["sources"], f"{label}.sources", nonempty=True),
    )


def _parse_project_entry(obj: Any, label: str) -> _ProjectEntry:
    obj = _dict(obj, label)
    expected = frozenset({"id", "name", "link", "dates", "technologies", "bullets", "keywords", "sources", "enabled"})
    _check_exact_keys(obj, expected, label)
    if type(obj["enabled"]) is not bool:
        raise ValueError(f"{label}.enabled must be a boolean")
    return _ProjectEntry(
        id=_nonempty_string(obj["id"], f"{label}.id"),
        name=_nonempty_string(obj["name"], f"{label}.name"),
        link=_safe_url(obj["link"], f"{label}.link"),
        dates=_parse_date_range(obj["dates"], f"{label}.dates"),
        technologies=_parse_string_list(obj["technologies"], f"{label}.technologies", nonempty=True),
        bullets=tuple(_parse_bullet(v, f"{label}.bullets[{i}]") for i, v in enumerate(_list(obj["bullets"], f"{label}.bullets"))),
        keywords=_parse_string_list(obj["keywords"], f"{label}.keywords", nonempty=True),
        sources=_parse_string_list(obj["sources"], f"{label}.sources", nonempty=True),
        enabled=obj["enabled"],
    )


def _parse_skill_entry(obj: Any, label: str) -> _SkillEntry:
    obj = _dict(obj, label)
    _check_exact_keys(obj, frozenset({"name", "keywords", "sources"}), label)
    return _SkillEntry(
        name=_nonempty_string(obj["name"], f"{label}.name"),
        keywords=_parse_string_list(obj["keywords"], f"{label}.keywords", nonempty=True),
        sources=_parse_string_list(obj["sources"], f"{label}.sources", nonempty=True),
    )


def _parse_contact(obj: Any, label: str) -> _Contact:
    obj = _dict(obj, label)
    _check_exact_keys(obj, frozenset({"full_name", "phone", "email"}), label)
    return _Contact(
        full_name=_nonempty_string(obj["full_name"], f"{label}.full_name"),
        phone=_nonempty_string(obj["phone"], f"{label}.phone"),
        email=_email_address(obj["email"], f"{label}.email"),
    )


def _parse_links(obj: Any, label: str) -> _Links:
    obj = _dict(obj, label)
    _check_exact_keys(obj, frozenset({"linkedin", "github", "website"}), label)
    return _Links(
        linkedin=_safe_url(obj["linkedin"], f"{label}.linkedin"),
        github=_safe_url(obj["github"], f"{label}.github"),
        website=_safe_url(obj["website"], f"{label}.website"),
    )


def _parse_source(obj: Any, label: str) -> _Source:
    obj = _dict(obj, label)
    expected = frozenset({"id", "type", "location", "sha256", "retrieved_at", "notes"})
    _check_exact_keys(obj, expected, label)
    return _Source(
        id=_nonempty_string(obj["id"], f"{label}.id"),
        type=_nonempty_string(obj["type"], f"{label}.type"),
        location=_nonempty_string(obj["location"], f"{label}.location"),
        sha256=_sha256_string(obj["sha256"], f"{label}.sha256"),
        retrieved_at=_nonempty_string(obj["retrieved_at"], f"{label}.retrieved_at"),
        notes=_nonempty_string(obj["notes"], f"{label}.notes"),
    )


def _parse_public_repo(obj: Any, label: str) -> _PublicRepo:
    obj = _dict(obj, label)
    expected = frozenset({"name", "url", "description", "primary_language", "fork", "created_at", "updated_at", "sources", "resume_eligible"})
    _check_exact_keys(obj, expected, label)
    if type(obj["fork"]) is not bool:
        raise ValueError(f"{label}.fork must be a boolean")
    if type(obj["resume_eligible"]) is not bool:
        raise ValueError(f"{label}.resume_eligible must be a boolean")
    return _PublicRepo(
        name=_nonempty_string(obj["name"], f"{label}.name"),
        url=_safe_url(obj["url"], f"{label}.url", nonempty=True),
        description=_string(obj["description"], f"{label}.description"),
        primary_language=_string(obj["primary_language"], f"{label}.primary_language"),
        fork=obj["fork"],
        created_at=_string(obj["created_at"], f"{label}.created_at"),
        updated_at=_string(obj["updated_at"], f"{label}.updated_at"),
        sources=_parse_string_list(obj["sources"], f"{label}.sources", nonempty=True),
        resume_eligible=obj["resume_eligible"],
    )


def _parse_open_question(obj: Any, label: str) -> _OpenQuestion:
    obj = _dict(obj, label)
    _check_exact_keys(obj, frozenset({"id", "question", "reason", "status"}), label)
    return _OpenQuestion(
        id=_nonempty_string(obj["id"], f"{label}.id"),
        question=_nonempty_string(obj["question"], f"{label}.question"),
        reason=_nonempty_string(obj["reason"], f"{label}.reason"),
        status=_nonempty_string(obj["status"], f"{label}.status"),
    )


def _parse_others(obj: Any) -> _Others:
    obj = _dict(obj, "others")
    expected = frozenset({"contact", "links", "sources", "public_repositories", "open_questions"})
    _check_exact_keys(obj, expected, "others")
    sources = tuple(_parse_source(v, f"others.sources[{i}]") for i, v in enumerate(_list(obj["sources"], "others.sources")))
    return _Others(
        contact=_parse_contact(obj["contact"], "others.contact"),
        links=_parse_links(obj["links"], "others.links"),
        sources=sources,
        public_repositories=tuple(
            _parse_public_repo(v, f"others.public_repositories[{i}]")
            for i, v in enumerate(_list(obj["public_repositories"], "others.public_repositories"))
        ),
        open_questions=tuple(
            _parse_open_question(v, f"others.open_questions[{i}]")
            for i, v in enumerate(_list(obj["open_questions"], "others.open_questions"))
        ),
    )



def _validate_profile_references(profile: ResumeProfile) -> None:
    if not profile.education:
        raise ValueError("education must contain at least one entry")
    expected_groups = (("spring",), ("co-op", "coop", "internship"))
    graduation_rules: list[tuple[str, str, tuple[tuple[str, ...], ...], tuple[str, ...]]] = []
    for entry_index, entry in enumerate(profile.education):
        if entry.graduation.default != "December 2026":
            raise ValueError(
                f"education[{entry_index}].graduation.default must be exactly 'December 2026'"
            )
        for rule_index, rule in enumerate(entry.graduation.rules):
            graduation_rules.append(
                (rule.id, rule.value, rule.all_keyword_groups, rule.sources)
            )
    if len(graduation_rules) != 1:
        raise ValueError("profile must contain exactly one graduation rule")
    rule_id, rule_value, rule_groups, _rule_sources = graduation_rules[0]
    if (
        rule_id != "spring_coop"
        or rule_value != "May 2027"
        or rule_groups != expected_groups
    ):
        raise ValueError("graduation rule must be the exact spring_coop policy")

    source_ids = {source.id for source in profile.others.sources}
    if len(source_ids) != len(profile.others.sources):
        raise ValueError("source IDs must be unique")
    id_locations: dict[str, str] = {}

    def add_id(identifier: str, location: str) -> None:
        if identifier in id_locations:
            raise ValueError(f"duplicate stable id {identifier!r} at {location}; first at {id_locations[identifier]}")
        id_locations[identifier] = location

    def refs(values: tuple[str, ...], location: str) -> None:
        if not values:
            raise ValueError(f"{location} must cite at least one source")
        for source_id in values:
            if source_id not in source_ids:
                raise ValueError(f"{location} references missing source {source_id!r}")

    for category, entries in profile.skills.items():
        if type(category) is not str or not category.strip():
            raise ValueError("skill category names must be non-empty strings")
        for i, entry in enumerate(entries):
            add_id(f"skill:{category}:{entry.name}", f"skills.{category}[{i}].name")
            refs(entry.sources, f"skills.{category}[{i}].sources")
    for section_name, entries in (
        ("experience", profile.experience),
        ("leadership", profile.leadership),
        ("education", profile.education),
        ("projects", profile.projects),
    ):
        for i, entry in enumerate(entries):
            add_id(entry.id, f"{section_name}[{i}].id")
            refs(entry.sources, f"{section_name}[{i}].sources")
            if isinstance(entry, _EducationEntry):
                for rule_index, rule in enumerate(entry.graduation.rules):
                    add_id(rule.id, f"{section_name}[{i}].graduation.rules[{rule_index}].id")
                    refs(rule.sources, f"{section_name}[{i}].graduation.rules[{rule_index}].sources")
            else:
                for bullet_index, bullet in enumerate(entry.bullets):
                    add_id(bullet.id, f"{section_name}[{i}].bullets[{bullet_index}].id")
                    refs(bullet.sources, f"{section_name}[{i}].bullets[{bullet_index}].sources")
    for i, source in enumerate(profile.others.sources):
        add_id(source.id, f"others.sources[{i}].id")
    for i, repo in enumerate(profile.others.public_repositories):
        refs(repo.sources, f"others.public_repositories[{i}].sources")
    for i, question in enumerate(profile.others.open_questions):
        add_id(question.id, f"others.open_questions[{i}].id")
    repo_names: set[str] = set()
    repo_urls: set[str] = set()
    for repo in profile.others.public_repositories:
        name_key = repo.name.casefold()
        url_key = repo.url.casefold()
        if name_key in repo_names or url_key in repo_urls:
            raise ValueError("public repository names and URLs must be unique")
        repo_names.add(name_key)
        repo_urls.add(url_key)


def _parse_profile_bytes(raw: bytes, path: Path) -> ResumeProfile:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"profile JSON is not valid UTF-8: {exc}") from exc
    _check_no_duplicate_keys(text)
    try:
        payload = json.loads(text, parse_constant=lambda value: (_ for _ in ()).throw(ValueError(f"invalid JSON constant {value}")))
    except (json.JSONDecodeError, ValueError) as exc:
        raise ValueError(f"profile JSON is invalid: {exc}") from exc
    if type(payload) is not dict:
        raise ValueError("profile JSON must contain an object")
    _validate_json_caps(payload)
    _check_exact_keys(
        payload,
        frozenset({"schema_version", "skills", "experience", "leadership", "education", "projects", "others"}),
        "profile root",
    )
    if type(payload["schema_version"]) is not int or payload["schema_version"] != _PROFILE_SCHEMA_VERSION:
        raise ValueError(f"unsupported schema_version: {payload['schema_version']!r}")
    skills_obj = _dict(payload["skills"], "skills")
    skills: dict[str, tuple[_SkillEntry, ...]] = {}
    category_keys: set[str] = set()
    for category, values in skills_obj.items():
        if type(category) is not str or not category.strip():
            raise ValueError("skill category names must be non-empty strings")
        category_key = category.casefold()
        if category_key in category_keys:
            raise ValueError(f"duplicate skill category: {category}")
        category_keys.add(category_key)
        skills[category] = tuple(_parse_skill_entry(v, f"skills.{category}[{i}]") for i, v in enumerate(_list(values, f"skills.{category}")))
    skills = {category: skills[category] for category in sorted(skills, key=lambda value: (value.casefold(), value))}
    experience = tuple(_parse_experience_entry(v, f"experience[{i}]") for i, v in enumerate(_list(payload["experience"], "experience")))
    leadership = tuple(_parse_leadership_entry(v, f"leadership[{i}]") for i, v in enumerate(_list(payload["leadership"], "leadership")))
    education = tuple(_parse_education_entry(v, f"education[{i}]") for i, v in enumerate(_list(payload["education"], "education")))
    projects = tuple(_parse_project_entry(v, f"projects[{i}]") for i, v in enumerate(_list(payload["projects"], "projects")))
    others = _parse_others(payload["others"])
    profile = ResumeProfile(
        schema_version=payload["schema_version"],
        skills=MappingProxyType(skills),
        experience=experience,
        leadership=leadership,
        education=education,
        projects=projects,
        others=others,
    )
    _validate_profile_references(profile)
    return profile


def load_resume_profile(path: str | Path) -> ResumeProfile:
    """Snapshot and strictly parse a profile without following symlinks."""
    path = Path(path)
    return _parse_profile_bytes(_snapshot_regular(path, _MAX_PROFILE_BYTES, "profile JSON"), path)


# ---------------------------------------------------------------------------
# Deterministic evidence matching and optimization
# ---------------------------------------------------------------------------

_FIELD_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = tuple(
    (re.compile(pattern, re.IGNORECASE), field)
    for pattern, field in (
        (r"\b(?:machine\s*learning|deep\s*learning|neural\s*net|nlp|natural\s*language|transformer|llm|large\s*language|gpt|bert)\b", "Machine Learning / AI"),
        (r"\b(?:data\s*science|data\s*engineer|data\s*analyst|big\s*data|etl|data\s*pipeline|spark|hadoop)\b", "Data Engineering"),
        (r"\b(?:front\s*end|frontend|react|angular|vue|ui\s*engineer|ux)\b", "Frontend Engineering"),
        (r"\b(?:back\s*end|backend|api|microservice|serverless|rest|graphql)\b", "Backend Engineering"),
        (r"\b(?:full\s*stack|fullstack)\b", "Full-Stack Engineering"),
        (r"\b(?:devops|sre|site\s*reliability|infrastructure|platform\s*engineer|kubernetes|docker|terraform|ci/cd|ci\s*cd)\b", "Platform / DevOps"),
        (r"\b(?:security|infosec|penetration|cryptograph|appsec)\b", "Security Engineering"),
        (r"\b(?:mobile|ios|android|swift|kotlin|react\s*native|flutter)\b", "Mobile Engineering"),
        (r"\b(?:embedded|firmware|iot|rtos)\b", "Embedded Systems"),
        (r"\b(?:cloud|aws|azure|gcp)\b", "Cloud Engineering"),
        (r"\b(?:blockchain|web3|defi|solidity|smart\s*contract)\b", "Blockchain / Web3"),
    )
)

_TECH_TERM_RE = re.compile(
    r"(?:Python|Java(?:Script)?|TypeScript|C\+\+|C#|Go|Rust|Ruby|PHP|Swift|Kotlin|Scala|SQL|NoSQL|PostgreSQL|MySQL|MongoDB|Redis|DynamoDB|Cassandra|React|Angular|Vue|Node\.js|Django|Flask|FastAPI|Spring|Rails|REST|GraphQL|gRPC|WebSocket|Docker|Kubernetes|AWS|Azure|GCP|Terraform|Ansible|CI/CD|Jenkins|Git|Linux|Unix|Bash|PyTorch|TensorFlow|JAX|scikit-learn|pandas|NumPy|Spark|Hadoop|Kafka|RabbitMQ|Agile|Scrum|Kanban|JIRA|API|SDK|CLI|HTML|CSS|Sass|Less|Machine\s*Learning|Deep\s*Learning|NLP|Computer\s*Vision|LLM|RAG|Embeddings|Vector|Microservices|Serverless|CI|CD|DevOps|SRE|OAuth|JWT|SAML|TCP/IP|HTTP|DNS|iOS|Android|React\s*Native|Flutter|Solidity|Ethereum|Web3|\.NET|Razor|Blazor|Prometheus|Grafana|ELK|HIPAA|SOC2|SOC\s*2|GDPR|Firecrawl|Qdrant|vLLM|Hugging\s*Face|Mapbox|OSMnx)",
    re.IGNORECASE,
)
_REQUIREMENT_PATTERN = re.compile(
    r"(?:required|requirements?|must\s+have|must\s+be|qualification|qualifications|preferred|you\s+will|you'll|we\s+are\s+looking|we're\s+looking|experience\s+(?:with|in|using|building|developing|working)|proficien(?:t|cy)|familiar(?:ity)?|knowledge\s+of|background\s+in|expertise\s+in|skilled\s+in|strong\s+(?:understanding|knowledge|background|experience))",
    re.IGNORECASE,
)
_REQUIREMENT_TOKEN_RE = re.compile(r"(?:\.[A-Za-z][A-Za-z0-9]*|[A-Za-z][A-Za-z0-9+#./-]{2,})")
_STOP_TERMS = {
    "and", "the", "with", "for", "from", "this", "that", "have", "has", "are", "will", "your", "our", "you", "we", "they", "their", "into", "using", "use", "work", "working", "years", "year", "team", "teams", "strong", "required", "requirements", "preferred", "experience", "knowledge", "ability", "skills", "skill", "familiar", "including", "looking", "develop", "developing", "build", "building", "role", "position", "candidate", "about", "through", "plus", "like", "such", "other", "must", "want", "need", "job", "company", "engineer", "engineering", "developer", "software", "intern", "internship", "co-op", "coop",
}


def _infer_field(title: str, description: str) -> str:
    for context in (title, description):
        for pattern, field in _FIELD_PATTERNS:
            if pattern.search(context):
                return field
    lowered = title.casefold()
    if "data" in lowered:
        return "Data Engineering"
    if "product" in lowered or "manager" in lowered:
        return "Product Management"
    if "engineer" in lowered or "developer" in lowered or "software" in lowered:
        return "Software Engineering"
    return "Software Engineering"


def _extract_keywords(text: str) -> tuple[str, ...]:
    seen: dict[str, str] = {}
    for match in _TECH_TERM_RE.finditer(text):
        value = re.sub(r"\s+", " ", match.group(0)).strip()
        key = value.casefold()
        seen.setdefault(key, value)
    return tuple(seen[key] for key in sorted(seen))


def _contains_term(text: str, term: str) -> bool:
    term = re.sub(r"\s+", " ", term.strip())
    if not term:
        return False
    try:
        pattern = re.compile(r"(?<![\w])" + re.escape(term) + r"(?![\w])", re.IGNORECASE)
    except re.error:
        return False
    return pattern.search(text) is not None


def _requirement_terms(title: str, description: str) -> tuple[str, ...]:
    terms: dict[str, str] = {}
    for value in _extract_keywords(title):
        terms.setdefault(value.casefold(), value)
    # Every title term is relevant context, including a non-technology product
    # name, but generic role words are deliberately excluded.
    for match in _REQUIREMENT_TOKEN_RE.finditer(title):
        value = match.group(0).strip(".,;:()[]{}")
        if value.casefold() not in _STOP_TERMS and len(value) >= 3:
            terms.setdefault(value.casefold(), value)
    lines = description.splitlines()
    in_requirements = False
    for line in lines:
        stripped = line.strip()
        marker = bool(_REQUIREMENT_PATTERN.search(stripped)) or bool(re.match(r"^(?:[-*•]\s*)?(?:requirements?|qualifications?)\s*:", stripped, re.IGNORECASE))
        if marker:
            in_requirements = True
        elif in_requirements and not stripped:
            in_requirements = False
        if not marker and not in_requirements:
            # Known technology terms anywhere in a description remain useful;
            # arbitrary terms are only considered from requirement-bearing text.
            for value in _extract_keywords(stripped):
                terms.setdefault(value.casefold(), value)
            continue
        for match in _REQUIREMENT_TOKEN_RE.finditer(stripped):
            value = match.group(0).strip(".,;:()[]{}")
            key = value.casefold()
            if len(value) >= 3 and key not in _STOP_TERMS:
                terms.setdefault(key, value)
    return tuple(terms[key] for key in sorted(terms))


def _job_term_weights(title: str, description: str, field: str) -> dict[str, float]:
    weights: dict[str, float] = {}
    def add(value: str, weight: float) -> None:
        key = value.casefold()
        if key and key not in _STOP_TERMS:
            weights[key] = max(weight, weights.get(key, 0.0))
    for value in _extract_keywords(title):
        add(value, 3.0)
    for match in _REQUIREMENT_TOKEN_RE.finditer(title):
        value = match.group(0).strip(".,;:()[]{}")
        if len(value) >= 3:
            add(value, 2.5)
    for value in _extract_keywords(description):
        add(value, 2.0 if _REQUIREMENT_PATTERN.search(description) else 1.0)
    for value in _requirement_terms(title, description):
        add(value, max(2.0, weights.get(value.casefold(), 0.0)))
    # Field participates in scoring but is not reported as a missing job term.
    for value in _REQUIREMENT_TOKEN_RE.findall(field):
        add(value, 0.5)
    return weights


def _score_text_against_terms(text: str, terms: Mapping[str, float]) -> float:
    return sum(weight for term, weight in terms.items() if _contains_term(text, term))


def _resolve_graduation_with_rule(
    education: tuple[_EducationEntry, ...], title: str, description: str
) -> tuple[str, str | None]:
    if not education:
        raise ValueError("education must contain at least one entry")
    context = f"{title} {description}"
    for entry in education:
        for rule in entry.graduation.rules:
            if all(any(_contains_term(context, term) for term in group) for group in rule.all_keyword_groups):
                return rule.value, rule.id
    return education[0].graduation.default, None


def _date_key(value: str) -> tuple[int, int, str]:
    months = {name.casefold(): index for index, name in enumerate(("January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"), 1)}
    match = re.search(r"\b(" + "|".join(months) + r")\s+(\d{4})\b", value, re.IGNORECASE)
    if match:
        return int(match.group(2)), months[match.group(1).casefold()], value
    year = re.search(r"\b(19|20)\d{2}\b", value)
    return (int(year.group(0)) if year else 0, 0, value)


def _profile_evidence(profile: ResumeProfile) -> tuple[str, ...]:
    values: list[str] = []
    for category, entries in profile.skills.items():
        values.append(category)
        for entry in entries:
            values.extend((entry.name, *entry.keywords))
    for entries in (profile.experience, profile.leadership):
        for entry in entries:
            values.extend((entry.title, entry.organization, entry.location, entry.dates.display, *entry.keywords))
            for bullet in entry.bullets:
                values.extend((bullet.text, *bullet.keywords))
    for entry in profile.education:
        values.extend((entry.institution, entry.location, entry.degree, entry.dates.display, entry.graduation.default, *entry.keywords))
        for rule in entry.graduation.rules:
            values.extend((rule.value, *[term for group in rule.all_keyword_groups for term in group]))
    for entry in profile.projects:
        values.extend((entry.name, entry.dates.display, *entry.technologies, *entry.keywords))
        for bullet in entry.bullets:
            values.extend((bullet.text, *bullet.keywords))
    # Keep the inventory deduplicated and deterministic for reports/fingerprints.
    return tuple(sorted({value for value in values if value}, key=lambda value: (value.casefold(), value)))


def _entry_text(entry: _ExperienceEntry | _LeadershipEntry | _ProjectEntry) -> str:
    if isinstance(entry, _ProjectEntry):
        values = [entry.name, entry.dates.display, *entry.technologies, *entry.keywords]
    else:
        values = [entry.title, entry.organization, entry.location, entry.dates.display, *entry.keywords]
    for bullet in entry.bullets:
        values.extend((bullet.text, *bullet.keywords))
    return " ".join(values)


def _bullet_text(bullet: _Bullet) -> str:
    return " ".join((bullet.text, *bullet.keywords))


def _select_skills(profile: ResumeProfile, terms: Mapping[str, float]) -> tuple[str, ...]:
    scored: list[tuple[float, str, int, int]] = []
    ordered_categories = sorted(profile.skills.items(), key=lambda item: (item[0].casefold(), item[0]))
    for category_index, (category, entries) in enumerate(ordered_categories):
        for entry_index, entry in enumerate(entries):
            score = _score_text_against_terms(" ".join((category, entry.name, *entry.keywords)), terms)
            if score > 0:
                scored.append((score, entry.name, category_index, entry_index))
    scored.sort(key=lambda item: (-item[0], item[1].casefold(), item[2], item[3]))
    seen: set[str] = set()
    result: list[str] = []
    for _score, name, _category_index, _entry_index in scored:
        key = name.casefold()
        if key not in seen:
            seen.add(key)
            result.append(name)
        if len(result) >= _MAX_SKILLS:
            break
    return tuple(result)


def _header(profile: ResumeProfile) -> str:
    contact = profile.others.contact
    links = profile.others.links
    lines = [
        r"\begin{center}",
        r"\textbf{\Large " + _latex_escape(contact.full_name) + r"} \\ \vspace{2pt}",
        _latex_escape(contact.phone) + r" $|$ \href{" + _latex_escape_url("mailto:" + contact.email) + "}{" + _latex_escape(contact.email) + "}",
    ]
    for label, url in (("linkedin", links.linkedin), ("github", links.github), ("website", links.website)):
        if url:
            lines.append(r"$|$ \href{" + _latex_escape_url(url) + "}{" + _latex_escape(label) + "}")
    lines.append(r"\end{center}")
    return "\n".join(lines)


def _subheading(first: str, second: str, third: str, fourth: str) -> str:
    return r"\resumeSubheading{" + first + "}{" + second + "}{" + third + "}{" + fourth + "}"


def _education_date_display(
    entry: _EducationEntry,
    graduation_date: str,
) -> str:
    if (
        graduation_date == entry.graduation.default
        or all(rule.value != graduation_date for rule in entry.graduation.rules)
    ):
        return entry.dates.display
    for separator in (" - ", " – ", " — "):
        if separator in entry.dates.display:
            start, _end = entry.dates.display.rsplit(separator, 1)
            return f"{start}{separator}{graduation_date}"
    return graduation_date


def _render_selection(
    profile: ResumeProfile,
    selection: _Selection,
    graduation_date: str,
    compressed_skills: tuple[str, ...],
) -> tuple[tuple[str, str], ...]:
    by_exp = {entry.id: entry for entry in profile.experience}
    by_lead = {entry.id: entry for entry in profile.leadership}
    by_project = {entry.id: entry for entry in profile.projects}
    sections: list[tuple[str, str]] = []
    if profile.education:
        lines: list[str] = []
        for entry in profile.education:
            date_display = _education_date_display(entry, graduation_date)
            lines.append(
                _subheading(
                    _latex_escape(entry.institution),
                    _latex_escape(entry.location),
                    _latex_escape(entry.degree),
                    _latex_escape(date_display),
                )
            )
        sections.append(("Education", "\n".join(lines)))
    exp_lines: list[str] = []
    for entry_id, bullet_ids in selection.experience:
        entry = by_exp[entry_id]
        exp_lines.append(_subheading(_latex_escape(entry.title), _latex_escape(entry.dates.display), _latex_escape(entry.organization), _latex_escape(entry.location)))
        bullets = {bullet.id: bullet for bullet in entry.bullets}
        chosen = [bullets[bullet_id] for bullet_id in bullet_ids if bullet_id in bullets]
        if chosen:
            exp_lines.append(r"\resumeItemListStart")
            exp_lines.extend(r"\resumeItem{" + _latex_escape(bullet.text) + "}" for bullet in chosen)
            exp_lines.append(r"\resumeItemListEnd")
    if exp_lines:
        sections.append(("Experience", "\n".join(exp_lines)))
    lead_lines: list[str] = []
    for entry_id, bullet_ids in selection.leadership:
        entry = by_lead[entry_id]
        lead_lines.append(_subheading(_latex_escape(entry.title), _latex_escape(entry.dates.display), _latex_escape(entry.organization), _latex_escape(entry.location)))
        bullets = {bullet.id: bullet for bullet in entry.bullets}
        chosen = [bullets[bullet_id] for bullet_id in bullet_ids if bullet_id in bullets]
        if chosen:
            lead_lines.append(r"\resumeItemListStart")
            lead_lines.extend(r"\resumeItem{" + _latex_escape(bullet.text) + "}" for bullet in chosen)
            lead_lines.append(r"\resumeItemListEnd")
    if lead_lines:
        sections.append(("Leadership", "\n".join(lead_lines)))
    project_lines: list[str] = []
    for entry_id, bullet_ids in selection.projects:
        entry = by_project[entry_id]
        name = _latex_escape(entry.name)
        if entry.link:
            name = r"\href{" + _latex_escape_url(entry.link) + "}{" + name + "}"
        project_lines.append(r"\resumeProjectHeading{\textbf{" + name + r"} $|$ \textit{" + _latex_escape(", ".join(entry.technologies)) + "}}{" + _latex_escape(entry.dates.display) + "}")
        bullets = {bullet.id: bullet for bullet in entry.bullets}
        chosen = [bullets[bullet_id] for bullet_id in bullet_ids if bullet_id in bullets]
        if chosen:
            project_lines.append(r"\resumeItemListStart")
            project_lines.extend(r"\resumeItem{" + _latex_escape(bullet.text) + "}" for bullet in chosen)
            project_lines.append(r"\resumeItemListEnd")
    if project_lines:
        sections.append(("Projects", "\n".join(project_lines)))
    if compressed_skills:
        sections.append(("Technical Skills", r"\resumeItem{\textbf{Skills:} " + _latex_escape(", ".join(compressed_skills)) + "}"))
    return tuple(sections)


def _selected_claims(
    profile: ResumeProfile,
    selection: _Selection,
    compressed_skills: tuple[str, ...],
    graduation_rule: str | None,
) -> tuple[tuple[str, tuple[str, ...]], ...]:
    claims: list[tuple[str, tuple[str, ...]]] = []

    def add(identifier: str, sources: tuple[str, ...]) -> None:
        claims.append((identifier, sources))

    for entry in profile.education:
        add(entry.id, entry.sources)
    if graduation_rule is not None:
        for entry in profile.education:
            for rule in entry.graduation.rules:
                if rule.id == graduation_rule:
                    add(rule.id, rule.sources)
                    break
    by_exp = {entry.id: entry for entry in profile.experience}
    for entry_id, bullet_ids in selection.experience:
        entry = by_exp[entry_id]
        add(entry.id, entry.sources)
        bullets = {bullet.id: bullet for bullet in entry.bullets}
        for bullet_id in bullet_ids:
            bullet = bullets.get(bullet_id)
            if bullet is not None:
                add(bullet.id, bullet.sources)
    by_lead = {entry.id: entry for entry in profile.leadership}
    for entry_id, bullet_ids in selection.leadership:
        entry = by_lead[entry_id]
        add(entry.id, entry.sources)
        bullets = {bullet.id: bullet for bullet in entry.bullets}
        for bullet_id in bullet_ids:
            bullet = bullets.get(bullet_id)
            if bullet is not None:
                add(bullet.id, bullet.sources)
    by_project = {entry.id: entry for entry in profile.projects}
    for entry_id, bullet_ids in selection.projects:
        entry = by_project[entry_id]
        add(entry.id, entry.sources)
        bullets = {bullet.id: bullet for bullet in entry.bullets}
        for bullet_id in bullet_ids:
            bullet = bullets.get(bullet_id)
            if bullet is not None:
                add(bullet.id, bullet.sources)
    wanted_skills = {name.casefold() for name in compressed_skills}
    for category, entries in sorted(profile.skills.items(), key=lambda item: (item[0].casefold(), item[0])):
        for entry in entries:
            if entry.name.casefold() in wanted_skills:
                add(f"skill:{category}:{entry.name}", entry.sources)
    return tuple(claims)


def optimize_resume(profile: ResumeProfile, job: ResumeJob) -> ResumePlan:
    """Select only source-backed profile claims for ``job`` deterministically."""
    if not isinstance(profile, ResumeProfile):
        raise TypeError("profile must be a ResumeProfile")
    if not isinstance(job, ResumeJob):
        raise TypeError("job must be a ResumeJob")
    field = _infer_field(job.title, job.description)
    graduation_date, graduation_rule = _resolve_graduation_with_rule(profile.education, job.title, job.description)
    terms = _job_term_weights(job.title, job.description, field)
    keyword_terms = _requirement_terms(job.title, job.description)
    extracted_job_terms: dict[str, str] = {}
    for value in (*_extract_keywords(job.title), *_extract_keywords(job.description), *keyword_terms):
        extracted_job_terms.setdefault(value.casefold(), value)
    job_terms = tuple(extracted_job_terms[key] for key in sorted(extracted_job_terms))
    evidence_inventory = _profile_evidence(profile)
    matched = tuple(term for term in keyword_terms if any(_contains_term(value, term) for value in evidence_inventory))
    unsupported = tuple(term for term in keyword_terms if term not in matched)
    matched_sorted = tuple(sorted(matched, key=lambda value: (value.casefold(), value)))
    unsupported_sorted = tuple(sorted(unsupported, key=lambda value: (value.casefold(), value)))
    coverage_ratio = 1.0 if not keyword_terms else len(matched) / len(keyword_terms)

    exp_scores: dict[str, float] = {}
    exp_bullet_scores: dict[tuple[str, str], float] = {}
    for entry in profile.experience:
        entry_score = _score_text_against_terms(_entry_text(entry), terms)
        exp_scores[entry.id] = entry_score
        for bullet in entry.bullets:
            exp_bullet_scores[(entry.id, bullet.id)] = _score_text_against_terms(_bullet_text(bullet), terms) + entry_score * 0.25
    # Experience is source order only after an explicit date ordering.  Bullets
    # are relevance ordered, never rewritten or synthesized.
    exp_order = sorted(range(len(profile.experience)), key=lambda i: (-_date_key(profile.experience[i].dates.start)[0], -_date_key(profile.experience[i].dates.start)[1], i))
    exp_selection: list[tuple[str, tuple[str, ...]]] = []
    for index in exp_order:
        entry = profile.experience[index]
        bullet_ids = tuple(
            bullet.id
            for bullet in sorted(entry.bullets, key=lambda b: (-exp_bullet_scores[(entry.id, b.id)], entry.bullets.index(b)))
        )
        exp_selection.append((entry.id, bullet_ids))
    primary = None
    if exp_selection:
        primary = max((entry_id for entry_id, _bullet_ids in exp_selection), key=lambda entry_id: (exp_scores[entry_id], -next(i for i, pair in enumerate(exp_selection) if pair[0] == entry_id)))

    lead_scores: dict[str, float] = {}
    lead_bullet_scores: dict[tuple[str, str], float] = {}
    for entry in profile.leadership:
        score = _score_text_against_terms(_entry_text(entry), terms)
        lead_scores[entry.id] = score
        for bullet in entry.bullets:
            lead_bullet_scores[(entry.id, bullet.id)] = _score_text_against_terms(_bullet_text(bullet), terms) + score * 0.25
    lead_candidates = [entry for entry in profile.leadership if lead_scores[entry.id] > 0]
    lead_candidates.sort(key=lambda entry: (-lead_scores[entry.id], -_date_key(entry.dates.start)[0], -_date_key(entry.dates.start)[1], entry.id))
    lead_selection = tuple(
        (
            entry.id,
            tuple(b.id for b in sorted(entry.bullets, key=lambda b: (-lead_bullet_scores[(entry.id, b.id)], entry.bullets.index(b)))),
        )
        for entry in lead_candidates[:_MAX_LEADERSHIP]
    )

    project_scores: dict[str, float] = {}
    project_bullet_scores: dict[tuple[str, str], float] = {}
    for entry in profile.projects:
        score = _score_text_against_terms(_entry_text(entry), terms)
        project_scores[entry.id] = score
        for bullet in entry.bullets:
            project_bullet_scores[(entry.id, bullet.id)] = _score_text_against_terms(_bullet_text(bullet), terms) + score * 0.25
    project_candidates = [entry for entry in profile.projects if entry.enabled and project_scores[entry.id] > 0]
    project_candidates.sort(key=lambda entry: (-project_scores[entry.id], -_date_key(entry.dates.start)[0], -_date_key(entry.dates.start)[1], entry.id))
    project_selection = tuple(
        (
            entry.id,
            tuple(
                b.id
                for b in sorted(entry.bullets, key=lambda b: (-project_bullet_scores[(entry.id, b.id)], entry.bullets.index(b)))[:_MAX_PROJECT_BULLETS]
            ),
        )
        for entry in project_candidates[:_MAX_PROJECTS]
    )
    selection = _Selection(tuple(exp_selection), lead_selection, project_selection, primary)
    compressed_skills = _select_skills(profile, terms)
    sections = _render_selection(profile, selection, graduation_date, compressed_skills)
    return ResumePlan(
        field=field,
        graduation_date=graduation_date,
        header_text=_header(profile),
        sections=sections,
        matched_keywords=matched_sorted,
        unsupported_keywords=unsupported_sorted,
        compressed_skills=compressed_skills,
        selection=selection,
        evidence_inventory=evidence_inventory,
        requirement_terms=tuple(keyword_terms),
        job_terms=job_terms,
        coverage_ratio=coverage_ratio,
        graduation_rule=graduation_rule,
        selected_claims=_selected_claims(profile, selection, compressed_skills, graduation_rule),
    )


def _render_section(name: str, body: str) -> str:
    return r"\section{" + _latex_escape(name) + "}\n" + r"\resumeSubHeadingListStart" + "\n" + body + "\n" + r"\resumeSubHeadingListEnd" + "\n"


def _render_resume(plan: ResumePlan, template_text: str) -> str:
    if template_text.count("%%RESUME_HEADER%%") != 1 or template_text.count("%%RESUME_SECTIONS%%") != 1:
        raise ValueError("template must contain exactly one RESUME_HEADER and RESUME_SECTIONS marker")
    begin = template_text.find(r"\begin{document}")
    end = template_text.find(r"\end{document}")
    if begin < 0 or end < 0 or begin >= end:
        raise ValueError("template document boundaries are invalid")
    header_marker = template_text.find("%%RESUME_HEADER%%")
    section_marker = template_text.find("%%RESUME_SECTIONS%%")
    if not begin < header_marker < section_marker < end:
        raise ValueError("template markers must be ordered inside the document")
    sections = "\n".join(_render_section(name, body) for name, body in plan.sections)
    return template_text.replace("%%RESUME_HEADER%%", plan.header_text).replace("%%RESUME_SECTIONS%%", sections)


# ---------------------------------------------------------------------------
# Fingerprints, compiler invocation, PDF inspection, and trimming
# ---------------------------------------------------------------------------


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()

def _canonical_json(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")

def _job_payload(job: ResumeJob) -> dict[str, Any]:
    return {
        "id": job.id,
        "title": job.title,
        "company": job.company,
        "description": job.description,
        "location": job.location,
        "posted_at": job.posted_at,
    }
def _input_digests(
    job: ResumeJob,
    profile_bytes: bytes,
    template_bytes: bytes,
    skill_bytes: bytes,
    compiler_identity: str = "auto",
) -> dict[str, str]:
    job_digest = _sha256_hex(_canonical_json(_job_payload(job)))
    return {
        "job_sha256": job_digest,
        "profile_sha256": _sha256_hex(profile_bytes),
        "template_sha256": _sha256_hex(template_bytes),
        "skill_sha256": _sha256_hex(skill_bytes),
        "compiler_identity": compiler_identity,
    }


def _fingerprint_inputs(
    job: ResumeJob,
    profile_bytes: bytes,
    template_bytes: bytes,
    skill_bytes: bytes,
    compiler_identity: str = "auto",
) -> str:
    digests = _input_digests(job, profile_bytes, template_bytes, skill_bytes, compiler_identity)
    payload = {
        "algorithm_sha256": _ALGORITHM_SHA256,
        "generator_schema_version": _GENERATOR_SCHEMA_VERSION,
        "compiler_identity": compiler_identity,
        "profile_sha256": digests["profile_sha256"],
        "template_sha256": digests["template_sha256"],
        "skill_sha256": digests["skill_sha256"],
        "job_sha256": digests["job_sha256"],
        "job": _job_payload(job),
    }
    return _sha256_hex(_canonical_json(payload))


def _compiler_argv(value: str | Sequence[str]) -> list[str]:
    if isinstance(value, str):
        argv = [value]
    else:
        argv = list(value)
    if len(argv) != 1 or type(argv[0]) is not str or not argv[0] or "\x00" in argv[0]:
        raise ValueError("compiler override must be exactly one executable path")
    executable = Path(argv[0]).name
    if executable not in {"tectonic", "pdflatex"}:
        raise ValueError("compiler executable basename must be tectonic or pdflatex")
    if any(char in argv[0] for char in ";|&<>\n\r") or "$(" in argv[0]:
        raise ValueError("unsafe shell syntax in compiler path")
    return argv


@dataclass(frozen=True)
class _ProcessResult:
    returncode: int
    stdout_tail: str
    stderr_tail: str
    reason: str | None = None


class _TailBuffer:
    __slots__ = ("limit", "total", "data")

    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.total = 0
        self.data = bytearray()

    def append(self, chunk: bytes) -> None:
        self.total += len(chunk)
        self.data.extend(chunk)
        if len(self.data) > self.limit:
            del self.data[: len(self.data) - self.limit]

    def text(self) -> str:
        return bytes(self.data).decode("utf-8", errors="replace")


def _stage_bytes(path: Path, limit: int) -> int:
    total = 0

    def walk(directory: Path) -> None:
        nonlocal total
        try:
            entries = list(os.scandir(directory))
        except OSError as exc:
            raise RuntimeError(f"cannot inspect compiler stage: {exc}") from exc
        for entry in entries:
            try:
                info = entry.stat(follow_symlinks=False)
            except OSError as exc:
                raise RuntimeError(f"cannot inspect compiler stage: {exc}") from exc
            if stat.S_ISREG(info.st_mode):
                total += info.st_size
                if total > limit:
                    return
            elif stat.S_ISDIR(info.st_mode):
                walk(Path(entry.path))
                if total > limit:
                    return

    walk(path)
    return total


def _kill_process_group(process: subprocess.Popen[bytes]) -> None:
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    except OSError:
        try:
            process.kill()
        except OSError:
            pass
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        try:
            process.kill()
        except OSError:
            pass
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass


def _run_bounded_process(
    cmd: Sequence[str],
    *,
    cwd: Path | None,
    timeout: int,
    stage: Path | None = None,
) -> _ProcessResult:
    try:
        process = subprocess.Popen(
            list(cmd),
            cwd=os.fspath(cwd) if cwd is not None else None,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
            start_new_session=True,
        )
    except OSError as exc:
        raise RuntimeError(f"cannot execute LaTeX compiler: {exc}") from exc
    assert process.stdout is not None
    assert process.stderr is not None
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ, "stdout")
    selector.register(process.stderr, selectors.EVENT_READ, "stderr")
    stdout_tail = _TailBuffer(_MAX_COMPILER_OUTPUT_BYTES)
    stderr_tail = _TailBuffer(_MAX_COMPILER_OUTPUT_BYTES)
    deadline = time.monotonic() + max(0, timeout)
    reason: str | None = None
    try:
        while selector.get_map():
            if stage is not None and _stage_bytes(stage, _MAX_STAGE_BYTES) > _MAX_STAGE_BYTES:
                reason = "stage byte cap exceeded"
                _kill_process_group(process)
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0 and process.poll() is None:
                reason = "timeout"
                _kill_process_group(process)
                break
            events = selector.select(max(0.01, min(0.1, remaining)))
            if not events:
                if process.poll() is not None and time.monotonic() >= deadline:
                    reason = "process streams did not close"
                    _kill_process_group(process)
                    break
                continue
            for key, _ in events:
                stream = key.fileobj
                file_descriptor = stream if isinstance(stream, int) else stream.fileno()
                try:
                    chunk = os.read(file_descriptor, 64 * 1024)
                except (OSError, ValueError):
                    chunk = b""
                if not chunk:
                    try:
                        selector.unregister(stream)
                    except Exception:
                        pass
                    continue
                tail = stdout_tail if key.data == "stdout" else stderr_tail
                tail.append(chunk)
                if tail.total > _MAX_COMPILER_OUTPUT_BYTES:
                    reason = f"{key.data} byte cap exceeded"
                    _kill_process_group(process)
                    break
            if reason is not None:
                break
        if reason is None:
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                reason = "process did not exit"
                _kill_process_group(process)
    finally:
        try:
            selector.close()
        finally:
            process.stdout.close()
            process.stderr.close()
    return _ProcessResult(
        process.returncode if process.returncode is not None else -signal.SIGKILL,
        stdout_tail.text(),
        stderr_tail.text(),
        reason,
    )


def _find_compiler() -> str:
    """Return tectonic only when its bounded version command succeeds."""
    for candidate in ("tectonic", "pdflatex"):
        try:
            result = _run_bounded_process([candidate, "--version"], cwd=None, timeout=10)
        except RuntimeError:
            continue
        if result.reason is None and result.returncode == 0:
            return candidate
    raise RuntimeError("no successful LaTeX compiler found (tried tectonic, pdflatex)")


def _resolve_compiler_path(value: str | Sequence[str]) -> str:
    executable = _compiler_argv(value)[0]
    if not os.path.dirname(executable):
        located = shutil.which(executable)
        if located is None:
            raise RuntimeError(f"compiler executable not found: {executable}")
        executable = located
    return os.path.abspath(executable)

def _compiler_identity(value: str | Sequence[str]) -> str:
    argv = _compiler_argv(value)
    executable = _resolve_compiler_path(argv)
    try:
        info = os.stat(executable)
    except OSError as exc:
        raise RuntimeError(f"cannot stat compiler executable: {exc}") from exc
    if not stat.S_ISREG(info.st_mode):
        raise RuntimeError("compiler executable must be a regular file")
    resolved = os.path.realpath(executable)
    try:
        resolved_info = os.stat(resolved)
    except OSError as exc:
        raise RuntimeError(f"cannot stat resolved compiler executable: {exc}") from exc
    if not stat.S_ISREG(resolved_info.st_mode):
        raise RuntimeError("resolved compiler executable must be a regular file")
    with tempfile.TemporaryDirectory(prefix="resume-compiler-version-") as version_dir:
        result = _run_bounded_process(
            [executable, "--version"],
            cwd=Path(version_dir),
            timeout=10,
        )
    if result.reason is not None or result.returncode != 0:
        raise RuntimeError("compiler version probe failed")
    version_bytes = (result.stdout_tail + "\n" + result.stderr_tail).encode("utf-8")
    stat_payload = {
        "dev": resolved_info.st_dev,
        "ino": resolved_info.st_ino,
        "mode": stat.S_IMODE(resolved_info.st_mode),
        "size": resolved_info.st_size,
        "mtime_ns": resolved_info.st_mtime_ns,
        "ctime_ns": resolved_info.st_ctime_ns,
    }
    return _canonical_json(
        {
            "kind": Path(argv[0]).name,
            "path": resolved,
            "version_sha256": _sha256_hex(version_bytes),
            "stat_sha256": _sha256_hex(_canonical_json(stat_payload)),
        }
    ).decode("utf-8")


def _compile_latex(tex_path: Path, output_dir: Path, compiler: str | Sequence[str], timeout: int) -> None:
    argv = _compiler_argv(compiler)
    argv[0] = _resolve_compiler_path(argv)
    executable = Path(argv[0]).name
    if executable == "tectonic":
        cmd = [*argv, "--untrusted", "--outdir", str(output_dir), str(tex_path)]
    else:
        cmd = [
            *argv,
            "-interaction=nonstopmode",
            "-halt-on-error",
            "-no-shell-escape",
            "-output-directory",
            str(output_dir),
            str(tex_path),
        ]
    result = _run_bounded_process(cmd, cwd=output_dir, timeout=timeout, stage=output_dir)
    details = result.stderr_tail.strip() or result.stdout_tail.strip()
    if result.reason == "timeout":
        raise RuntimeError(f"LaTeX compilation timed out after {timeout}s")
    if result.reason is not None:
        raise RuntimeError(f"LaTeX compilation rejected: {result.reason}")
    if result.returncode != 0:
        raise RuntimeError(f"LaTeX compilation failed (exit {result.returncode}): {details}")


def _inspect_pdf_bytes(pdf_bytes: bytes) -> tuple[int, str]:
    if len(pdf_bytes) > _MAX_RESUME_PDF_BYTES:
        raise RuntimeError(f"generated PDF exceeds {_MAX_RESUME_PDF_BYTES} bytes")
    try:
        from pypdf import PdfReader
    except ImportError:
        raise RuntimeError("pypdf is required for PDF validation") from None
    try:
        reader = PdfReader(io.BytesIO(pdf_bytes))
        pages = len(reader.pages)
        text_parts: list[str] = []
        for page in reader.pages:
            extracted = page.extract_text()
            if extracted:
                text_parts.append(extracted)
    except Exception as exc:
        raise RuntimeError(f"cannot read generated PDF: {exc}") from exc
    return pages, "\n".join(text_parts)


def _validate_pdf_bytes(pdf_bytes: bytes) -> int:
    pages, text = _inspect_pdf_bytes(pdf_bytes)
    if pages != 1:
        raise RuntimeError(f"generated PDF has {pages} page(s); exactly 1 required")
    if not text.strip():
        raise RuntimeError("generated PDF has no extractable text")
    return pages


def _replace_plan_selection(plan: ResumePlan, profile: ResumeProfile, selection: _Selection) -> ResumePlan:
    sections = _render_selection(profile, selection, plan.graduation_date, plan.compressed_skills)
    return ResumePlan(
        field=plan.field,
        graduation_date=plan.graduation_date,
        header_text=plan.header_text,
        sections=sections,
        matched_keywords=plan.matched_keywords,
        unsupported_keywords=plan.unsupported_keywords,
        compressed_skills=plan.compressed_skills,
        selection=selection,
        evidence_inventory=plan.evidence_inventory,
        requirement_terms=plan.requirement_terms,
        job_terms=plan.job_terms,
        coverage_ratio=plan.coverage_ratio,
        graduation_rule=plan.graduation_rule,
        selected_claims=_selected_claims(profile, selection, plan.compressed_skills, plan.graduation_rule),
    )


def _replace_plan_skills(
    plan: ResumePlan,
    profile: ResumeProfile,
    compressed_skills: tuple[str, ...],
) -> ResumePlan:
    if plan.selection is None:
        raise RuntimeError("cannot replace skills without selection metadata")
    sections = _render_selection(profile, plan.selection, plan.graduation_date, compressed_skills)
    return ResumePlan(
        field=plan.field,
        graduation_date=plan.graduation_date,
        header_text=plan.header_text,
        sections=sections,
        matched_keywords=plan.matched_keywords,
        unsupported_keywords=plan.unsupported_keywords,
        compressed_skills=compressed_skills,
        selection=plan.selection,
        evidence_inventory=plan.evidence_inventory,
        requirement_terms=plan.requirement_terms,
        job_terms=plan.job_terms,
        coverage_ratio=plan.coverage_ratio,
        graduation_rule=plan.graduation_rule,
        selected_claims=_selected_claims(
            profile,
            plan.selection,
            compressed_skills,
            plan.graduation_rule,
        ),
    )


def _remove_lowest_project_content(
    selection: _Selection,
    profile: ResumeProfile,
    terms: Mapping[str, float],
) -> _Selection | None:
    if not selection.projects:
        return None
    project_pairs = list(selection.projects)
    by_project = {entry.id: entry for entry in profile.projects}
    extra_bullets: list[tuple[float, int, int, str, str]] = []
    for entry_index, (entry_id, bullet_ids) in enumerate(project_pairs):
        if len(bullet_ids) <= 1:
            continue
        entry = by_project[entry_id]
        entry_score = _score_text_against_terms(_entry_text(entry), terms)
        bullets = {bullet.id: bullet for bullet in entry.bullets}
        for bullet_index, bullet_id in enumerate(bullet_ids[1:], start=1):
            score = _score_text_against_terms(_bullet_text(bullets[bullet_id]), terms)
            extra_bullets.append(
                (score + entry_score * 0.25, entry_index, bullet_index, entry_id, bullet_id)
            )
    if extra_bullets:
        _score, entry_index, bullet_index, entry_id, _bullet_id = min(
            extra_bullets,
            key=lambda item: (item[0], -item[1], -item[2], item[3], item[4]),
        )
        bullet_ids = list(project_pairs[entry_index][1])
        bullet_ids.pop(bullet_index)
        project_pairs[entry_index] = (entry_id, tuple(bullet_ids))
    else:
        projects = [
            (
                _score_text_against_terms(_entry_text(by_project[entry_id]), terms),
                entry_index,
                entry_id,
            )
            for entry_index, (entry_id, _bullet_ids) in enumerate(project_pairs)
        ]
        _score, entry_index, _entry_id = min(
            projects,
            key=lambda item: (item[0], -item[1], item[2]),
        )
        project_pairs.pop(entry_index)
    return _Selection(
        selection.experience,
        selection.leadership,
        tuple(project_pairs),
        selection.primary_experience,
    )


def _remove_leadership_content(selection: _Selection) -> _Selection | None:
    if not selection.leadership:
        return None
    return _Selection(selection.experience, selection.leadership[:-1], selection.projects, selection.primary_experience)


def _remove_extra_experience_bullet(
    selection: _Selection,
    profile: ResumeProfile,
    terms: Mapping[str, float],
) -> _Selection | None:
    pairs = list(selection.experience)
    by_experience = {entry.id: entry for entry in profile.experience}
    candidates: list[tuple[float, int, int, str, str]] = []
    for entry_index, (entry_id, bullet_ids) in enumerate(pairs):
        if entry_id == selection.primary_experience or len(bullet_ids) <= 1:
            continue
        entry = by_experience[entry_id]
        entry_score = _score_text_against_terms(_entry_text(entry), terms)
        bullets = {bullet.id: bullet for bullet in entry.bullets}
        for bullet_index, bullet_id in enumerate(bullet_ids[1:], start=1):
            score = _score_text_against_terms(_bullet_text(bullets[bullet_id]), terms)
            candidates.append(
                (score + entry_score * 0.25, entry_index, bullet_index, entry_id, bullet_id)
            )
    if not candidates:
        return None
    _score, entry_index, bullet_index, entry_id, _bullet_id = min(
        candidates,
        key=lambda item: (item[0], -item[1], -item[2], item[3], item[4]),
    )
    bullet_ids = list(pairs[entry_index][1])
    bullet_ids.pop(bullet_index)
    pairs[entry_index] = (entry_id, tuple(bullet_ids))
    return _Selection(
        tuple(pairs),
        selection.leadership,
        selection.projects,
        selection.primary_experience,
    )


def _remove_optional_experience(
    selection: _Selection,
    profile: ResumeProfile,
    terms: Mapping[str, float],
) -> _Selection | None:
    if not selection.experience:
        return None
    by_exp = {entry.id: entry for entry in profile.experience}
    candidates: list[tuple[float, int, str]] = []
    for index, (entry_id, _bullets) in enumerate(selection.experience):
        if entry_id != selection.primary_experience:
            candidates.append((_score_text_against_terms(_entry_text(by_exp[entry_id]), terms), index, entry_id))
    if not candidates:
        return None
    _score, index, _entry_id = min(candidates, key=lambda item: (item[0], -item[1], item[2]))
    pairs = list(selection.experience)
    pairs.pop(index)
    return _Selection(tuple(pairs), selection.leadership, selection.projects, selection.primary_experience)


def _write_private(path: Path, data: bytes, *, max_bytes: int | None = None, label: str = "artifact") -> None:
    if max_bytes is not None and len(data) > max_bytes:
        raise RuntimeError(f"{label} exceeds {max_bytes} bytes")
    if path.exists() or path.is_symlink():
        if path.is_symlink():
            raise RuntimeError(f"refusing symlink artifact path: {path}")
        if not path.is_file():
            raise RuntimeError(f"artifact path is not a regular file: {path}")
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(os.fspath(path), flags, 0o600)
    except OSError as exc:
        raise RuntimeError(f"cannot create artifact {path}: {exc}") from exc
    try:
        view = memoryview(data)
        offset = 0
        while offset < len(view):
            offset += os.write(fd, view[offset:])
        os.fchmod(fd, 0o600)
    finally:
        os.close(fd)


def _ensure_private_dir(path: Path) -> None:
    path = Path(os.path.abspath(os.fspath(path)))
    parts = path.parts
    if not parts:
        raise RuntimeError(f"cannot create output directory {path}")
    current = Path(parts[0])
    for index, component in enumerate(parts[1:], start=1):
        current /= component
        try:
            info = os.lstat(current)
        except FileNotFoundError:
            parent = current.parent
            current = parent
            for remaining in parts[index:]:
                current /= remaining
                try:
                    os.mkdir(current, 0o700)
                except FileExistsError:
                    raced = os.lstat(current)
                    if stat.S_ISLNK(raced.st_mode) or not stat.S_ISDIR(raced.st_mode):
                        raise RuntimeError(f"unsafe output directory: {current}")
                else:
                    continue
                if stat.S_ISLNK(raced.st_mode) or not stat.S_ISDIR(raced.st_mode):
                    raise RuntimeError(f"unsafe output directory: {current}")
            break
        if stat.S_ISLNK(info.st_mode):
            raise RuntimeError(f"refusing symlink directory: {current}")
        if not stat.S_ISDIR(info.st_mode):
            raise RuntimeError(f"output path is not a directory: {current}")
        if index == len(parts) - 1:
            if info.st_mode & 0o077:
                raise RuntimeError(f"existing output directory is not owner-private: {current}")
            owner = getattr(info, "st_uid", os.geteuid())
            if owner != os.geteuid():
                raise RuntimeError(f"existing output directory is not owned by the current user: {current}")

def _directory_identity(path: Path) -> tuple[int, int]:
    flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    try:
        fd = os.open(os.fspath(path), flags)
    except OSError as exc:
        raise RuntimeError(f"cannot open directory {path}: {exc}") from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISDIR(info.st_mode):
            raise RuntimeError(f"directory path is not a directory: {path}")
        return info.st_dev, info.st_ino
    finally:
        os.close(fd)


def _cleanup_stage_tree(path: Path, identity: tuple[int, int]) -> None:
    try:
        if _directory_identity(path) != identity:
            return
    except RuntimeError:
        return
    try:
        children = list(path.iterdir())
    except OSError:
        return
    for child in children:
        try:
            info = os.lstat(child)
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or stat.S_ISREG(info.st_mode):
            try:
                os.unlink(child)
            except OSError:
                pass
        elif stat.S_ISDIR(info.st_mode):
            try:
                _cleanup_stage_tree(child, (info.st_dev, info.st_ino))
                os.rmdir(child)
            except OSError:
                pass
    try:
        os.rmdir(path)
    except OSError:
        pass


def _require_regular_child(path: Path, *, label: str) -> None:
    try:
        info = os.lstat(path)
    except FileNotFoundError as exc:
        raise RuntimeError(f"missing {label}: {path.name}") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise RuntimeError(f"{label} must be a regular non-symlink file: {path.name}")
    if info.st_mode & 0o077:
        raise RuntimeError(f"{label} is not private: {path.name}")

def _read_regular_child(
    path: Path, *, label: str, max_bytes: int, private: bool = True
) -> bytes:
    if private:
        _require_regular_child(path, label=label)
    else:
        try:
            info = os.lstat(path)
        except FileNotFoundError as exc:
            raise RuntimeError(f"missing {label}: {path.name}") from exc
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise RuntimeError(f"{label} must be a regular non-symlink file: {path.name}")
    return _snapshot_regular(path, max_bytes, label)


def _ordered_bullet_ids(entry: Any, terms: Mapping[str, float]) -> tuple[str, ...]:
    return tuple(
        bullet.id
        for bullet in sorted(
            entry.bullets,
            key=lambda bullet: (
                -_score_text_against_terms(_bullet_text(bullet), terms),
                entry.bullets.index(bullet),
            ),
        )
    )


def _expansion_candidates(
    plan: ResumePlan,
    profile: ResumeProfile,
    job: ResumeJob,
) -> tuple[ResumePlan, ...]:
    if plan.selection is None:
        raise RuntimeError("cannot expand a plan without selection metadata")
    selection = plan.selection
    terms = _job_term_weights(job.title, job.description, _infer_field(job.title, job.description))
    candidates: list[ResumePlan] = []

    selected_skills = {name.casefold() for name in plan.compressed_skills}
    skill_rows = [
        (
            _score_text_against_terms(" ".join((category, entry.name, *entry.keywords)), terms),
            entry.name,
            category,
            index,
        )
        for category, entries in sorted(profile.skills.items(), key=lambda item: (item[0].casefold(), item[0]))
        for index, entry in enumerate(entries)
    ]
    skill_rows.sort(key=lambda item: (-item[0], item[1].casefold(), item[2].casefold(), item[3]))
    seen_skills = set(selected_skills)
    for _score, name, _category, _index in skill_rows:
        if name.casefold() in seen_skills or len(plan.compressed_skills) + len(candidates) >= _MAX_SKILLS:
            continue
        seen_skills.add(name.casefold())
        candidates.append(_replace_plan_skills(plan, profile, (*plan.compressed_skills, name)))

    selected_exp = {entry_id: bullet_ids for entry_id, bullet_ids in selection.experience}
    for entry in sorted(
        profile.experience,
        key=lambda value: (-_score_text_against_terms(_entry_text(value), terms), value.id),
    ):
        ordered = _ordered_bullet_ids(entry, terms)
        chosen = selected_exp.get(entry.id)
        if chosen is None:
            experience = (*selection.experience, (entry.id, ordered[:1]))
            candidates.append(
                _replace_plan_selection(
                    plan,
                    profile,
                    _Selection(experience, selection.leadership, selection.projects, selection.primary_experience),
                )
            )
            continue
        for bullet_id in ordered:
            if bullet_id in chosen:
                continue
            experience = list(selection.experience)
            index = next(i for i, pair in enumerate(experience) if pair[0] == entry.id)
            experience[index] = (entry.id, (*chosen, bullet_id))
            candidates.append(
                _replace_plan_selection(
                    plan,
                    profile,
                    _Selection(tuple(experience), selection.leadership, selection.projects, selection.primary_experience),
                )
            )

    selected_leadership = {entry_id for entry_id, _bullet_ids in selection.leadership}
    for entry in sorted(
        profile.leadership,
        key=lambda value: (-_score_text_against_terms(_entry_text(value), terms), value.id),
    ):
        if entry.id in selected_leadership or len(selection.leadership) >= _MAX_LEADERSHIP:
            continue
        leadership = (*selection.leadership, (entry.id, _ordered_bullet_ids(entry, terms)[:1]))
        candidates.append(
            _replace_plan_selection(
                plan,
                profile,
                _Selection(selection.experience, leadership, selection.projects, selection.primary_experience),
            )
        )

    selected_projects = {entry_id: bullet_ids for entry_id, bullet_ids in selection.projects}
    for entry in sorted(
        (value for value in profile.projects if value.enabled),
        key=lambda value: (-_score_text_against_terms(_entry_text(value), terms), value.id),
    ):
        ordered = _ordered_bullet_ids(entry, terms)
        chosen = selected_projects.get(entry.id)
        if chosen is None:
            if len(selection.projects) >= _MAX_PROJECTS:
                continue
            projects = (*selection.projects, (entry.id, ordered[:1]))
        else:
            if len(chosen) >= _MAX_PROJECT_BULLETS:
                continue
            extra = next((bullet_id for bullet_id in ordered if bullet_id not in chosen), None)
            if extra is None:
                continue
            projects_list = list(selection.projects)
            index = next(i for i, pair in enumerate(projects_list) if pair[0] == entry.id)
            projects_list[index] = (entry.id, (*chosen, extra))
            projects = tuple(projects_list)
        candidates.append(
            _replace_plan_selection(
                plan,
                profile,
                _Selection(selection.experience, selection.leadership, projects, selection.primary_experience),
            )
        )
    return tuple(candidates)


def _expand_plan(
    plan: ResumePlan,
    profile: ResumeProfile,
    job: ResumeJob,
    template_text: str,
    output_dir: Path,
    compiler: str | Sequence[str],
    initial_measure: tuple[int, str] | None = None,
) -> ResumePlan:
    """Add supported content while retaining the fullest one-page candidate."""
    current = plan
    if initial_measure is None:
        pages, _text = _compile_plan(current, job, template_text, output_dir, compiler)
    else:
        pages, _text = initial_measure
    if pages != 1:
        raise RuntimeError(f"cannot expand resume with {pages} page(s)")
    attempted: set[tuple[str, tuple[str, ...]]] = set()
    expansion_attempts = 0
    stage_matches_current = True

    def action_key(candidate: ResumePlan) -> tuple[str, tuple[str, ...]]:
        if candidate.selection == current.selection:
            added_skills = tuple(
                name
                for name in candidate.compressed_skills
                if name.casefold() not in {value.casefold() for value in current.compressed_skills}
            )
            return ("skill", tuple(value.casefold() for value in added_skills))
        current_claims = {identifier for identifier, _sources in current.selected_claims}
        candidate_claims = {identifier for identifier, _sources in candidate.selected_claims}
        return ("claim", tuple(sorted(candidate_claims - current_claims)))

    def run_phase(skills_only: bool) -> bool:
        nonlocal current, stage_matches_current, expansion_attempts
        while True:
            if skills_only and len(", ".join(current.compressed_skills)) >= _MIN_SKILLS_LINE_CHARS:
                return True
            found_candidate = False
            for candidate in _expansion_candidates(current, profile, job):
                if skills_only:
                    eligible = (
                        candidate.selection == current.selection
                        and candidate.compressed_skills != current.compressed_skills
                    )
                else:
                    eligible = candidate.compressed_skills == current.compressed_skills
                if not eligible:
                    continue
                key = action_key(candidate)
                if expansion_attempts >= _MAX_EXPANSION_ATTEMPTS:
                    return False
                if key in attempted:
                    continue
                expansion_attempts += 1
                attempted.add(key)
                found_candidate = True
                pages, _text = _compile_plan(candidate, job, template_text, output_dir, compiler)
                stage_matches_current = False
                if pages < 1:
                    raise RuntimeError(f"generated resume has {pages} page(s)")
                if pages == 1:
                    current = candidate
                    stage_matches_current = True
                break
            if not found_candidate:
                return False

    run_phase(True)
    run_phase(False)
    if not stage_matches_current:
        _compile_plan(current, job, template_text, output_dir, compiler)
    return current


def _compile_plan(
    plan: ResumePlan,
    job: ResumeJob,
    template_text: str,
    output_dir: Path,
    compiler: str | Sequence[str],
) -> tuple[int, str]:
    """Render and compile one plan, returning page count and extracted text."""
    tex_path = output_dir / "resume.tex"
    pdf_path = output_dir / "resume.pdf"
    expected_tex = _render_resume(plan, template_text).encode("utf-8")
    _write_private(
        tex_path,
        expected_tex,
        max_bytes=_MAX_RESUME_TEX_BYTES,
        label="resume.tex",
    )
    _write_private(
        output_dir / "job_description.txt",
        job.description.encode("utf-8"),
        max_bytes=_MAX_JOB_DESCRIPTION_BYTES,
        label="job_description.txt",
    )
    try:
        pdf_info = os.lstat(pdf_path)
    except FileNotFoundError:
        pass
    else:
        if stat.S_ISLNK(pdf_info.st_mode) or not stat.S_ISREG(pdf_info.st_mode):
            raise RuntimeError("resume.pdf path is unsafe before compilation")
        os.unlink(pdf_path)
    _compile_latex(tex_path, output_dir, compiler, _TRIM_TIMEOUT_SECONDS)
    actual_tex = _read_regular_child(
        tex_path,
        label="generated resume.tex",
        max_bytes=_MAX_RESUME_TEX_BYTES,
        private=False,
    )
    if actual_tex != expected_tex:
        raise RuntimeError("compiler modified rendered resume.tex")
    actual_job = _read_regular_child(
        output_dir / "job_description.txt",
        label="generated job description",
        max_bytes=_MAX_JOB_DESCRIPTION_BYTES,
        private=False,
    )
    if actual_job != job.description.encode("utf-8"):
        raise RuntimeError("compiler modified job_description.txt")
    pdf_bytes = _read_regular_child(
        pdf_path,
        label="generated PDF",
        max_bytes=_MAX_RESUME_PDF_BYTES,
        private=False,
    )
    return _inspect_pdf_bytes(pdf_bytes)

def _trim_plan(
    plan: ResumePlan,
    profile: ResumeProfile,
    job: ResumeJob,
    template_text: str,
    output_dir: Path,
    compiler: str | Sequence[str],
) -> tuple[ResumePlan, tuple[int, str]]:
    """Compile/measure and trim in the contract's strict priority order."""
    if plan.selection is None:
        raise RuntimeError("cannot trim a plan without selection metadata")
    current = plan
    trim_terms = _job_term_weights(job.title, job.description, _infer_field(job.title, job.description))
    for _attempt in range(256):
        pages, text = _compile_plan(current, job, template_text, output_dir, compiler)
        if pages == 1:
            return current, (pages, text)
        if pages < 1:
            raise RuntimeError(f"generated resume has {pages} page(s)")
        selection = current.selection
        if selection is None:
            raise RuntimeError("cannot trim a plan without selection metadata")
        next_selection = _remove_lowest_project_content(selection, profile, trim_terms)
        if next_selection is None:
            next_selection = _remove_leadership_content(selection)
        if next_selection is None:
            next_selection = _remove_extra_experience_bullet(selection, profile, trim_terms)
        if next_selection is None:
            next_selection = _remove_optional_experience(selection, profile, trim_terms)
        if next_selection is None:
            raise RuntimeError(f"unable to fit resume to one page after trimming ({pages} pages)")
        current = _replace_plan_selection(current, profile, next_selection)
    raise RuntimeError("unable to fit resume to one page within trim bound")


# ---------------------------------------------------------------------------
# Strict cache validation and atomic artifact publication
# ---------------------------------------------------------------------------

_EXPECTED_ARTIFACTS = ("resume.tex", "resume.pdf", "optimization.json", "job_description.txt", "manifest.json")
_ARTIFACT_BYTE_CAPS = {
    "resume.tex": _MAX_RESUME_TEX_BYTES,
    "resume.pdf": _MAX_RESUME_PDF_BYTES,
    "optimization.json": _MAX_OPTIMIZATION_REPORT_BYTES,
    "job_description.txt": _MAX_JOB_DESCRIPTION_BYTES,
    "manifest.json": _MAX_MANIFEST_BYTES,
}


def _manifest_body(manifest: Mapping[str, Any]) -> dict[str, Any]:
    body = json.loads(json.dumps(manifest))
    body.pop("manifest_sha256", None)
    return body

def _manifest_digest(manifest: Mapping[str, Any]) -> str:
    return _sha256_hex(_canonical_json(_manifest_body(manifest)))


def _build_report(
    plan: ResumePlan, job: ResumeJob, fingerprint: str, skill_sha256: str
) -> dict[str, Any]:
    return {
        "schema_version": _PROFILE_SCHEMA_VERSION,
        "generator_schema_version": _GENERATOR_SCHEMA_VERSION,
        "algorithm_sha256": _ALGORITHM_SHA256,
        "fingerprint": fingerprint,
        "skill_sha256": skill_sha256,
        "job_id": job.id,
        "field": plan.field,
        "graduation_date": plan.graduation_date,
        "graduation_rule": plan.graduation_rule,
        "job_terms": list(plan.job_terms),
        "requirement_terms": list(plan.requirement_terms),
        "matched_keywords": list(plan.matched_keywords),
        "unsupported_keywords": list(plan.unsupported_keywords),
        "coverage_ratio": plan.coverage_ratio,
        "compressed_skills": list(plan.compressed_skills),
        "sections": [name for name, _body in plan.sections],
        "selected_claims": [
            {"id": identifier, "sources": list(sources)} for identifier, sources in plan.selected_claims
        ],
        "evidence_inventory": list(plan.evidence_inventory),
    }


def _validate_manifest_and_cache(
    artifact_dir: Path,
    job: ResumeJob,
    fingerprint: str,
    digests: Mapping[str, str],
    output_root: Path,
) -> GeneratedResume:
    try:
        info = os.lstat(artifact_dir)
    except FileNotFoundError:
        raise FileNotFoundError from None
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise RuntimeError(f"unsafe existing artifact directory: {artifact_dir}")
    if info.st_mode & 0o077:
        raise RuntimeError("existing artifact directory is not private")
    try:
        children = {child.name for child in artifact_dir.iterdir()}
    except OSError as exc:
        raise RuntimeError(f"cannot inspect existing artifact directory: {exc}") from exc
    if children != set(_EXPECTED_ARTIFACTS):
        raise RuntimeError("existing artifact directory contains unexpected or missing artifacts")
    paths = {name: artifact_dir / name for name in _EXPECTED_ARTIFACTS}
    for path in paths.values():
        _require_regular_child(path, label="cached artifact")

    cached: dict[str, bytes] = {}
    for name in _EXPECTED_ARTIFACTS:
        cached[name] = _read_regular_child(
            paths[name],
            label=f"cached {name}",
            max_bytes=_ARTIFACT_BYTE_CAPS[name],
        )
    manifest_bytes = cached["manifest.json"]
    try:
        manifest_text = manifest_bytes.decode("utf-8")
        _check_no_duplicate_keys(manifest_text)
        manifest = json.loads(manifest_text)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise RuntimeError(f"cached manifest is invalid: {exc}") from exc
    if type(manifest) is not dict:
        raise RuntimeError("cached manifest must be an object")
    expected_keys = frozenset(
        {
            "schema_version",
            "generator_schema_version",
            "algorithm_sha256",
            "fingerprint",
            "job_id",
            "inputs",
            "artifacts",
            "manifest_sha256",
        }
    )
    try:
        _check_exact_keys(manifest, expected_keys, "manifest")
    except ValueError as exc:
        raise RuntimeError(str(exc)) from exc
    if (
        manifest["schema_version"] != _PROFILE_SCHEMA_VERSION
        or manifest["generator_schema_version"] != _GENERATOR_SCHEMA_VERSION
        or manifest["algorithm_sha256"] != _ALGORITHM_SHA256
    ):
        raise RuntimeError("cached manifest schema or algorithm mismatch")
    if type(manifest["fingerprint"]) is not str or manifest["fingerprint"] != fingerprint:
        raise RuntimeError("cached manifest fingerprint mismatch")
    if type(manifest["job_id"]) is not int or manifest["job_id"] != job.id:
        raise RuntimeError("cached manifest job ID mismatch")
    inputs = manifest["inputs"]
    if type(inputs) is not dict or set(inputs) != set(digests):
        raise RuntimeError("cached manifest input digest schema mismatch")
    if any(type(inputs[key]) is not str or inputs[key] != digests[key] for key in digests):
        raise RuntimeError("cached manifest input digest mismatch")
    if type(manifest["manifest_sha256"]) is not str or not re.fullmatch(
        r"[0-9a-f]{64}", manifest["manifest_sha256"]
    ):
        raise RuntimeError("cached manifest self digest is invalid")
    if _manifest_digest(manifest) != manifest["manifest_sha256"]:
        raise RuntimeError("cached manifest self digest mismatch")

    artifacts = manifest["artifacts"]
    expected_artifacts = {name for name in _EXPECTED_ARTIFACTS if name != "manifest.json"}
    if type(artifacts) is not dict or set(artifacts) != expected_artifacts:
        raise RuntimeError("cached artifact hash schema mismatch")
    for name in expected_artifacts:
        value = artifacts[name]
        cap = _ARTIFACT_BYTE_CAPS[name]
        if (
            type(value) is not dict
            or set(value) != {"bytes", "sha256"}
            or type(value["bytes"]) is not int
            or value["bytes"] < 0
            or value["bytes"] > cap
            or type(value["sha256"]) is not str
            or not re.fullmatch(r"[0-9a-f]{64}", value["sha256"])
        ):
            raise RuntimeError(f"cached artifact hash entry is invalid: {name}")
        actual = cached[name]
        if len(actual) != value["bytes"] or _sha256_hex(actual) != value["sha256"]:
            raise RuntimeError(f"cached artifact digest mismatch: {name}")

    report_bytes = cached["optimization.json"]
    try:
        report_text = report_bytes.decode("utf-8")
        _check_no_duplicate_keys(report_text)
        report = json.loads(report_text)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise RuntimeError(f"cached report is invalid: {exc}") from exc
    expected_report_keys = frozenset(
        {
            "schema_version",
            "generator_schema_version",
            "algorithm_sha256",
            "fingerprint",
            "skill_sha256",
            "job_id",
            "field",
            "graduation_date",
            "graduation_rule",
            "job_terms",
            "requirement_terms",
            "matched_keywords",
            "unsupported_keywords",
            "coverage_ratio",
            "compressed_skills",
            "sections",
            "selected_claims",
            "evidence_inventory",
        }
    )
    if type(report) is not dict:
        raise RuntimeError("cached report must be an object")
    try:
        _check_exact_keys(report, expected_report_keys, "optimization report")
    except ValueError as exc:
        raise RuntimeError(str(exc)) from exc
    if (
        report["schema_version"] != _PROFILE_SCHEMA_VERSION
        or report["generator_schema_version"] != _GENERATOR_SCHEMA_VERSION
        or report["algorithm_sha256"] != _ALGORITHM_SHA256
        or report["fingerprint"] != fingerprint
        or report["skill_sha256"] != digests["skill_sha256"]
        or report["job_id"] != job.id
    ):
        raise RuntimeError("cached report identity mismatch")
    for key in ("field", "graduation_date"):
        if type(report[key]) is not str or not report[key].strip():
            raise RuntimeError(f"cached report {key} is invalid")
    if report["graduation_date"] not in {"December 2026", "May 2027"}:
        raise RuntimeError("cached report graduation_date is invalid")
    if report["graduation_rule"] is not None and (
        type(report["graduation_rule"]) is not str
        or report["graduation_rule"] != "spring_coop"
    ):
        raise RuntimeError("cached report graduation_rule is invalid")
    for key in (
        "job_terms",
        "requirement_terms",
        "matched_keywords",
        "unsupported_keywords",
        "compressed_skills",
        "sections",
        "evidence_inventory",
    ):
        if type(report[key]) is not list or any(type(value) is not str for value in report[key]):
            raise RuntimeError(f"cached report {key} is invalid")
    if (
        type(report["coverage_ratio"]) not in (int, float)
        or isinstance(report["coverage_ratio"], bool)
        or not 0 <= report["coverage_ratio"] <= 1
    ):
        raise RuntimeError("cached report coverage_ratio is invalid")
    selected_claims = report["selected_claims"]
    if type(selected_claims) is not list:
        raise RuntimeError("cached report selected_claims is invalid")
    claim_ids: set[str] = set()
    for claim in selected_claims:
        if (
            type(claim) is not dict
            or set(claim) != {"id", "sources"}
            or type(claim["id"]) is not str
            or not claim["id"].strip()
            or claim["id"] in claim_ids
            or type(claim["sources"]) is not list
            or not claim["sources"]
            or any(type(source_id) is not str or not source_id.strip() for source_id in claim["sources"])
        ):
            raise RuntimeError("cached report selected_claims is invalid")
        claim_ids.add(claim["id"])
    if report["graduation_rule"] is not None and "spring_coop" not in claim_ids:
        raise RuntimeError("cached report is missing active graduation claim")
    if cached["job_description.txt"] != job.description.encode("utf-8"):
        raise RuntimeError("cached job description mismatch")
    pages = _validate_pdf_bytes(cached["resume.pdf"])
    return GeneratedResume(
        job_id=job.id,
        artifact_ref=os.fspath(artifact_dir.relative_to(output_root)),
        tex_path=paths["resume.tex"],
        pdf_path=paths["resume.pdf"],
        report_path=paths["optimization.json"],
        pages=pages,
        field=report["field"],
        graduation_date=report["graduation_date"],
        matched_keywords=tuple(report["matched_keywords"]),
    )

def _prune_stage(stage: Path) -> None:
    if _stage_bytes(stage, _MAX_STAGE_BYTES) > _MAX_STAGE_BYTES:
        raise RuntimeError("compiler stage exceeds byte cap")
    keep = {"resume.tex", "resume.pdf", "job_description.txt"}
    for child in stage.iterdir():
        try:
            info = os.lstat(child)
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or stat.S_ISDIR(info.st_mode):
            raise RuntimeError(f"compiler created unsafe artifact: {child.name}")
        if not stat.S_ISREG(info.st_mode):
            raise RuntimeError(f"compiler created unsafe artifact: {child.name}")
        if child.name in keep:
            cap = _ARTIFACT_BYTE_CAPS[child.name]
            if info.st_size > cap:
                raise RuntimeError(f"generated {child.name} exceeds {cap} bytes")
            os.chmod(child, 0o600)
            continue
        if info.st_size > _MAX_STAGE_BYTES:
            raise RuntimeError(f"compiler-created artifact exceeds {_MAX_STAGE_BYTES} bytes")
        os.unlink(child)


def generate_resume(
    job: ResumeJob,
    *,
    profile_path: str | Path,
    template_path: str | Path,
    output_root: str | Path,
    compiler: str | None = None,
    skill_path: str | Path | None = None,
) -> GeneratedResume:
    """Generate/reuse exactly five private, integrity-checked artifacts."""
    if not isinstance(job, ResumeJob):
        raise TypeError("job must be a ResumeJob")
    profile_path = Path(profile_path)
    template_path = Path(template_path)
    skill_path = Path(skill_path) if skill_path is not None else template_path.with_name("SKILL.md")
    output_root = Path(os.path.abspath(os.fspath(output_root)))
    profile_bytes = _snapshot_regular(profile_path, _MAX_PROFILE_BYTES, "profile JSON")
    template_bytes = _snapshot_regular(template_path, _MAX_TEMPLATE_BYTES, "template")
    skill_bytes = _snapshot_regular(skill_path, _MAX_SKILL_BYTES, "resume skill")
    skill_sha256 = _validate_skill_bytes(skill_bytes, skill_path)
    try:
        template_text = template_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"template is not valid UTF-8: {exc}") from exc
    if template_text.count("%%RESUME_HEADER%%") != 1 or template_text.count("%%RESUME_SECTIONS%%") != 1:
        raise ValueError("template must contain exactly one RESUME_HEADER and RESUME_SECTIONS marker")
    profile = _parse_profile_bytes(profile_bytes, profile_path)
    requested_compiler = compiler if compiler is not None else _find_compiler()
    resolved_compiler = _resolve_compiler_path(requested_compiler)
    compiler_identity = _compiler_identity(resolved_compiler)
    fingerprint = _fingerprint_inputs(
        job, profile_bytes, template_bytes, skill_bytes, compiler_identity
    )
    digests = _input_digests(job, profile_bytes, template_bytes, skill_bytes, compiler_identity)

    _ensure_private_dir(output_root)
    job_dir = output_root / f"job-{job.id}"
    _ensure_private_dir(job_dir)
    job_identity = _directory_identity(job_dir)
    artifact_dir = job_dir / fingerprint[:16]
    try:
        existing = os.lstat(artifact_dir)
    except FileNotFoundError:
        existing = None
    if existing is not None:
        return _validate_manifest_and_cache(
            artifact_dir,
            job,
            fingerprint,
            digests,
            output_root,
        )

    stage = Path(tempfile.mkdtemp(prefix=".resume-stage-", dir=os.fspath(job_dir)))
    os.chmod(stage, 0o700)
    stage_identity = _directory_identity(stage)
    try:
        if _directory_identity(job_dir) != job_identity:
            raise RuntimeError("job directory changed during stage creation")
        plan = optimize_resume(profile, job)
        tex_path = stage / "resume.tex"
        pdf_path = stage / "resume.pdf"
        expected_job = job.description.encode("utf-8")
        pages, text = _compile_plan(plan, job, template_text, stage, resolved_compiler)
        if pages < 1:
            raise RuntimeError(f"generated resume has {pages} page(s)")
        if pages != 1:
            plan, measure = _trim_plan(plan, profile, job, template_text, stage, resolved_compiler)
        else:
            measure = (pages, text)
        plan = _expand_plan(
            plan,
            profile,
            job,
            template_text,
            stage,
            resolved_compiler,
            initial_measure=measure,
        )
        expected_tex = _render_resume(plan, template_text).encode("utf-8")
        actual_tex = _read_regular_child(
            tex_path,
            label="final resume.tex",
            max_bytes=_MAX_RESUME_TEX_BYTES,
            private=False,
        )
        if actual_tex != expected_tex:
            raise RuntimeError("compiler modified rendered resume.tex")
        actual_job = _read_regular_child(
            stage / "job_description.txt",
            label="final job description",
            max_bytes=_MAX_JOB_DESCRIPTION_BYTES,
            private=False,
        )
        if actual_job != expected_job:
            raise RuntimeError("compiler modified job_description.txt")
        pdf_bytes = _read_regular_child(
            pdf_path,
            label="final generated PDF",
            max_bytes=_MAX_RESUME_PDF_BYTES,
            private=False,
        )
        pages = _validate_pdf_bytes(pdf_bytes)

        _prune_stage(stage)
        report_bytes = _canonical_json(_build_report(plan, job, fingerprint, skill_sha256))
        _write_private(
            stage / "optimization.json",
            report_bytes,
            max_bytes=_MAX_OPTIMIZATION_REPORT_BYTES,
            label="optimization.json",
        )

        artifacts: dict[str, dict[str, Any]] = {}
        for name in ("resume.tex", "resume.pdf", "optimization.json", "job_description.txt"):
            path = stage / name
            _require_regular_child(path, label="generated artifact")
            data = (
                pdf_bytes
                if name == "resume.pdf"
                else _read_regular_child(
                    path,
                    label=f"generated {name}",
                    max_bytes=_ARTIFACT_BYTE_CAPS[name],
                )
            )
            if len(data) > _ARTIFACT_BYTE_CAPS[name]:
                raise RuntimeError(f"generated {name} exceeds {_ARTIFACT_BYTE_CAPS[name]} bytes")
            artifacts[name] = {"bytes": len(data), "sha256": _sha256_hex(data)}
        manifest: dict[str, Any] = {
            "schema_version": _PROFILE_SCHEMA_VERSION,
            "generator_schema_version": _GENERATOR_SCHEMA_VERSION,
            "algorithm_sha256": _ALGORITHM_SHA256,
            "fingerprint": fingerprint,
            "job_id": job.id,
            "inputs": dict(digests),
            "artifacts": artifacts,
            "manifest_sha256": "",
        }
        manifest["manifest_sha256"] = _manifest_digest(manifest)
        manifest_bytes = _canonical_json(manifest)
        _write_private(
            stage / "manifest.json",
            manifest_bytes,
            max_bytes=_MAX_MANIFEST_BYTES,
            label="manifest.json",
        )
        if set(child.name for child in stage.iterdir()) != set(_EXPECTED_ARTIFACTS):
            raise RuntimeError("staging directory contains unexpected artifacts")
        if _stage_bytes(stage, _MAX_STAGE_BYTES) > _MAX_STAGE_BYTES:
            raise RuntimeError("compiler stage exceeds byte cap")
        if _directory_identity(job_dir) != job_identity:
            raise RuntimeError("job directory changed before publication")
        if _directory_identity(stage) != stage_identity:
            raise RuntimeError("staging directory changed before publication")
        try:
            os.lstat(artifact_dir)
        except FileNotFoundError:
            pass
        else:
            raise RuntimeError("artifact appeared concurrently; refusing overwrite")
        try:
            os.replace(stage, artifact_dir)
        except FileExistsError as exc:
            raise RuntimeError("artifact appeared concurrently; refusing overwrite") from exc
        stage = None  # type: ignore[assignment]
        return GeneratedResume(
            job_id=job.id,
            artifact_ref=os.fspath(artifact_dir.relative_to(output_root)),
            tex_path=artifact_dir / "resume.tex",
            pdf_path=artifact_dir / "resume.pdf",
            report_path=artifact_dir / "optimization.json",
            pages=pages,
            field=plan.field,
            graduation_date=plan.graduation_date,
            matched_keywords=plan.matched_keywords,
        )
    finally:
        if stage is not None:
            _cleanup_stage_tree(stage, stage_identity)
