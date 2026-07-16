from __future__ import annotations

import errno
import hashlib
import json
import math
import os
import re
import stat
from dataclasses import dataclass, field as dataclass_field
import unicodedata
from datetime import date
from decimal import Decimal, InvalidOperation
from io import BytesIO
from pathlib import Path
from typing import Any, Iterator, Mapping, Protocol
from urllib.parse import urlsplit

from .contracts import (
    ApplicationContext,
    AutofillPlan,
    FieldAnswer,
    JsonValue,
    ObservedField,
    PageObservation,
    PublicReasonCode,
    freeze_json,
    thaw_json,
)
from .safety import (
    DescriptorSafety,
    GreenhouseRouteDecision,
    SUPPORTED_ATS_POLICIES,
    classify_descriptors,
    classify_greenhouse_form_action,
    classify_greenhouse_request,
    classify_greenhouse_url,
    is_greenhouse_interactive_origin,
)

SUPPORTED_ATS = tuple(SUPPORTED_ATS_POLICIES)
SUPPORTED_ATS_IDENTIFIERS = SUPPORTED_ATS
SUPPORTED_FIELD_ANSWER_ATS = frozenset((*SUPPORTED_ATS, "*"))

try:
    from .browser_adapter import validate_lever_url
except ImportError:  # pragma: no cover - only protects import-cycle tooling
    validate_lever_url = None  # type: ignore[assignment]

MAX_RESUME_BYTES = 10 * 1024 * 1024
MAX_RESUME_TEXT_CHARS = 100_000
MAX_PDF_PAGES = 100
MAX_PROFILE_BYTES = 256 * 1024
MAX_PROFILE_DEPTH = 8
MAX_PROFILE_NODES = 2_000
MAX_PROFILE_STRING_CHARS = 20_000
# Keep profile answer cardinality bounded independently of the generic JSON
# node cap.  This is deliberately the same order of magnitude as the other
# profile collection caps and preserves the original safety boundary.
MAX_PROFILE_ANSWERS = 500
MAX_DESCRIPTION_CHARS = 12_000
MAX_SINGLE_LINE_CHARS = 2_048
MAX_TEXTAREA_CHARS = 12_000
MAX_EMAIL_CHARS = 320
MAX_TEL_CHARS = 32
MAX_URL_CHARS = 2_048
__all__ = (
    "ApplicationContext",
    "ApplicationProfile",
    "ATSClassification",
    "AutofillPlan",
    "ConfiguredFieldAnswer",
    "FieldAnswer",
    "GreenhouseAdapter",
    "LeverAdapter",
    "GreenhouseRouteDecision",
    "LoadedApplicationProfile",
    "MAX_PROFILE_ANSWERS",
    "ResumeContext",
    "ResumeFacts",
    "SUPPORTED_ATS",
    "SUPPORTED_ATS_IDENTIFIERS",
    "SUPPORTED_FIELD_ANSWER_ATS",
    "classify_greenhouse_form_action",
    "classify_greenhouse_request",
    "classify_greenhouse_url",
    "extract_resume_facts",
    "is_greenhouse_interactive_origin",
    "load_application_profile",
    "load_application_profile_snapshot",
    "load_applicant_description",
    "load_resume_context",
    "merge_plans",
    "validate_answer_value",
)



@dataclass(frozen=True, init=False)
class ResumeFacts:
    """Facts extracted from the retained resume snapshot.

    ``candidates`` retains every unique candidate, while ``facts`` contains only
    keys with exactly one candidate.  An ambiguous key is intentionally absent
    from ``facts`` so callers cannot accidentally choose one occurrence.
    """

    facts: Mapping[str, JsonValue]
    candidates: Mapping[str, tuple[str, ...]]
    ambiguous: tuple[str, ...]

    def __init__(
        self,
        facts: Mapping[str, Any] | None = None,
        candidates: Mapping[str, tuple[str, ...]] | None = None,
        ambiguous: tuple[str, ...] = (),
    ) -> None:
        object.__setattr__(self, "facts", freeze_json(dict(facts or {})))
        object.__setattr__(self, "candidates", freeze_json(dict(candidates or {})))
        object.__setattr__(self, "ambiguous", tuple(ambiguous))


@dataclass(frozen=True)
class ResumeContext:
    basename: str
    media_type: str
    text: str
    sha256: str
    _fd: int
    path: Path | None = None
    facts: ResumeFacts = dataclass_field(default_factory=ResumeFacts)

    def fileno(self) -> int:
        return self._fd

    def close(self) -> None:
        fd = self._fd
        if fd >= 0:
            object.__setattr__(self, "_fd", -1)
            os.close(fd)

    def __enter__(self) -> ResumeContext:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


@dataclass(frozen=True)
class ConfiguredFieldAnswer:
    ats: str
    name: str | None
    label: str | None
    kind: str
    value: str | bool


@dataclass(frozen=True, init=False)
class ApplicationProfile:
    facts: Mapping[str, JsonValue]
    description: str
    field_answers: tuple[ConfiguredFieldAnswer, ...]

    def __init__(
        self,
        facts: Mapping[str, Any] | None = None,
        description: str = "",
        field_answers: tuple[ConfiguredFieldAnswer, ...] = (),
    ) -> None:
        object.__setattr__(self, "facts", freeze_json(dict(facts or {})))
        object.__setattr__(self, "description", description)
        object.__setattr__(self, "field_answers", tuple(field_answers))


@dataclass(frozen=True)
class LoadedApplicationProfile:
    """A validated profile plus provenance for the exact bytes parsed."""

    profile: ApplicationProfile
    source_kind: str
    source_sha256: str | None

    def __post_init__(self) -> None:
        if self.source_kind not in {"default", "explicit_json"}:
            raise ValueError("unsupported application profile source kind")
        if self.source_kind == "default":
            if self.source_sha256 is not None:
                raise ValueError("default application profile cannot have a source hash")
        elif (
            type(self.source_sha256) is not str
            or re.fullmatch(r"[0-9a-f]{64}", self.source_sha256) is None
        ):
            raise ValueError("explicit application profile source hash is invalid")


@dataclass(frozen=True)
class ATSClassification:
    name: str | None
    confidence: float
    reason: str




class ATSAdapter(Protocol):
    name: str

    def matches(self, url: str, html: str) -> bool: ...

    def deterministic_answers(
        self,
        observation: PageObservation,
        context: ApplicationContext,
        *,
        profile: ApplicationProfile | None = None,
        resume_context: ResumeContext | None = None,
        resume_facts: ResumeFacts | None = None,
    ) -> tuple[FieldAnswer, ...]: ...


def _open_regular(path: Path, *, max_bytes: int, description: str) -> tuple[int, os.stat_result]:
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        if exc.errno in {errno.ELOOP, errno.EMLINK}:
            raise ValueError(f"{description} must be a regular file, not a directory or symlink") from exc
        raise
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise ValueError(f"{description} must be a regular file, not a directory or symlink")
        if st.st_uid != os.geteuid():
            raise ValueError(f"{description} must be owned by the effective user")
        if st.st_size > max_bytes:
            raise ValueError(f"{description} exceeds its size cap")
        return fd, st
    except Exception:
        os.close(fd)
        raise


def _read_fd_snapshot(fd: int, size: int) -> bytes:
    """Read the fstat snapshot and probe EOF to catch replacement/growth races."""

    os.lseek(fd, 0, os.SEEK_SET)
    chunks: list[bytes] = []
    remaining = size
    while remaining:
        chunk = os.read(fd, min(remaining, 1024 * 1024))
        if not chunk:
            raise ValueError("file changed while being read")
        chunks.append(chunk)
        remaining -= len(chunk)
    probe = os.read(fd, 1)
    if probe:
        raise ValueError("file changed while being read")
    os.lseek(fd, 0, os.SEEK_SET)
    return b"".join(chunks)


def load_resume_context(path: str | Path) -> ResumeContext:
    resume_path = Path(path)
    fd, st = _open_regular(resume_path, max_bytes=MAX_RESUME_BYTES, description="resume")
    try:
        data = _read_fd_snapshot(fd, st.st_size)
        media_type = _resume_media_type(resume_path, data)
        text = _extract_resume_text(media_type, data)
        if len(text) > MAX_RESUME_TEXT_CHARS:
            raise ValueError("resume text must be <=100,000 characters")
        return ResumeContext(
            basename=resume_path.name,
            media_type=media_type,
            text=text,
            sha256=hashlib.sha256(data).hexdigest(),
            _fd=fd,
            path=resume_path,
            facts=extract_resume_facts(text),
        )
    except Exception:
        os.close(fd)
        raise


def _resume_media_type(path: Path, data: bytes) -> str:
    suffix = path.suffix.lower()
    if suffix == ".pdf" and data.startswith(b"%PDF"):
        return "application/pdf"
    if suffix == ".txt":
        return "text/plain"
    if suffix == ".md":
        return "text/markdown"
    raise ValueError("resume must be an explicit PDF, TXT, or MD file")


def _extract_resume_text(media_type: str, data: bytes) -> str:
    if media_type == "application/pdf":
        try:
            from pypdf import PdfReader

            reader = PdfReader(BytesIO(data))
            if len(reader.pages) > MAX_PDF_PAGES:
                raise ValueError("resume PDF must be <=100 pages")
            return "\n".join(page.extract_text() or "" for page in reader.pages)
        except ValueError:
            raise
        except Exception as exc:
            raise ValueError("resume PDF could not be decoded") from exc
    return data.decode("utf-8")


def _candidate_map(text: str) -> dict[str, list[str]]:
    candidates: dict[str, list[str]] = {}

    def add(key: str, value: str) -> None:
        cleaned = value.strip().strip(".,;:()[]{}<>")
        if not cleaned:
            return
        normalized = _normalize_fact(key, cleaned)
        values = candidates.setdefault(key, [])
        if not any(_normalize_fact(key, existing) == normalized for existing in values):
            values.append(cleaned)

    for match in re.finditer(r"(?i)(?<![\w.+-])[A-Z0-9._%+\-]+@[A-Z0-9.\-]+\.[A-Z]{2,}(?![\w.-])", text):
        add("email", match.group(0))

    for match in re.finditer(r"(?<!\w)(?:\+?\d[\d().\- ]{7,}\d)(?!\w)", text):
        phone = " ".join(match.group(0).split())
        if len(re.sub(r"\D", "", phone)) >= 8:
            add("phone", phone)

    for match in re.finditer(r"(?i)(?:https?://|www\.)[^\s<>\"']+", text):
        value = match.group(0).rstrip(".,;:)]}")
        lowered = value.lower()
        if "linkedin.com/" in lowered:
            add("linkedin", value)
        else:
            add("website", value)

    # Names are accepted only from the compact contact header.  Treating every
    # prose line as a name would create false facts and hide ambiguity.
    header_lines = [line.strip(" -*#\t") for line in text.splitlines()[:8] if line.strip()]
    name_re = re.compile(r"^[A-Za-z][A-Za-z'’-]{1,30}(?:\s+[A-Za-z][A-Za-z'’-]{1,30}){1,3}$")
    for line in header_lines:
        if "@" in line or "://" in line or any(char.isdigit() for char in line):
            continue
        if name_re.fullmatch(line):
            add("full_name", line)
    return candidates


def _normalize_fact(key: str, value: str) -> str:
    normalized = " ".join(value.strip().split()).casefold()
    if key == "email":
        return normalized
    if key == "phone":
        return re.sub(r"\D", "", normalized)
    if key in {"linkedin", "website"}:
        return normalized.rstrip("/")
    if key in {"full_name", "first_name", "last_name"}:
        return normalized
    return normalized


def extract_resume_facts(text: str) -> ResumeFacts:
    candidates = _candidate_map(text)
    normalized_candidates: dict[str, tuple[str, ...]] = {
        key: tuple(values) for key, values in candidates.items() if values
    }
    facts: dict[str, str] = {}
    ambiguous: list[str] = []
    for key, values in normalized_candidates.items():
        if len(values) == 1:
            facts[key] = values[0]
            if key == "full_name":
                words = values[0].split()
                if len(words) >= 2:
                    facts["first_name"] = words[0]
                    facts["last_name"] = words[-1]
        else:
            ambiguous.append(key)
    return ResumeFacts(facts=facts, candidates=normalized_candidates, ambiguous=tuple(sorted(set(ambiguous))))


def parse_application_profile(payload: Mapping[str, Any]) -> ApplicationProfile:
    """Validate an already-decoded applicant profile using the canonical rules."""

    if not isinstance(payload, Mapping):
        raise ValueError("profile JSON must contain an object")
    profile_payload = dict(payload)
    _validate_json_caps(profile_payload)
    raw_answers = profile_payload.pop("field_answers", ())
    description = profile_payload.pop("resume_summary", "")
    if description is None:
        description = ""
    if not isinstance(description, str):
        raise ValueError("resume_summary must be a string")
    if len(description) > MAX_DESCRIPTION_CHARS:
        raise ValueError("resume_summary must be <=12,000 characters")
    field_answers = _parse_configured_answers(raw_answers)
    return ApplicationProfile(
        facts=profile_payload,
        description=description,
        field_answers=field_answers,
    )


def load_application_profile_snapshot(profile_json: str | Path | None) -> LoadedApplicationProfile:
    """Load and validate one profile from a single byte snapshot."""

    if profile_json is None:
        return LoadedApplicationProfile(ApplicationProfile(), "default", None)
    path = Path(profile_json)
    fd, st = _open_regular(path, max_bytes=MAX_PROFILE_BYTES, description="profile JSON")
    try:
        raw = _read_fd_snapshot(fd, st.st_size)
    finally:
        os.close(fd)
    try:
        payload = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_object_pairs,
            parse_constant=_reject_nonfinite_json,
        )
    except RecursionError as exc:
        raise ValueError("profile JSON exceeds recursion limits") from exc
    profile = parse_application_profile(payload)
    return LoadedApplicationProfile(profile, "explicit_json", hashlib.sha256(raw).hexdigest())


def load_application_profile(profile_json: str | Path | None) -> ApplicationProfile:
    """Compatibility loader returning only the validated profile value."""

    return load_application_profile_snapshot(profile_json).profile
def load_applicant_description(path: str | Path | None, profile: ApplicationProfile | None = None) -> str:
    if path is None:
        return profile.description if profile is not None else ""
    description_path = Path(path)
    fd, st = _open_regular(description_path, max_bytes=MAX_DESCRIPTION_CHARS * 4, description="applicant description")
    try:
        data = _read_fd_snapshot(fd, st.st_size)
    finally:
        os.close(fd)
    text = data.decode("utf-8")
    if len(text) > MAX_DESCRIPTION_CHARS:
        raise ValueError("applicant description must be <=12,000 characters")
    return text


def _reject_duplicate_object_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_nonfinite_json(value: str) -> None:
    raise ValueError(f"non-finite JSON number: {value}")


def _validate_json_caps(value: Any) -> None:
    nodes = 0

    def walk(item: Any, depth: int) -> None:
        nonlocal nodes
        nodes += 1
        if nodes > MAX_PROFILE_NODES:
            raise ValueError("profile JSON exceeds node cap")
        if depth > MAX_PROFILE_DEPTH:
            raise ValueError("profile JSON exceeds depth cap")
        if isinstance(item, str):
            if len(item) > MAX_PROFILE_STRING_CHARS:
                raise ValueError("profile JSON string exceeds size cap")
        elif isinstance(item, float):
            if not math.isfinite(item):
                raise ValueError("profile JSON contains non-finite number")
        elif isinstance(item, dict):
            for key, child in item.items():
                if not isinstance(key, str):
                    raise ValueError("profile JSON object keys must be strings")
                if len(key) > MAX_PROFILE_STRING_CHARS:
                    raise ValueError("profile JSON object key exceeds string size cap")
                walk(child, depth + 1)
        elif isinstance(item, list):
            for child in item:
                walk(child, depth + 1)
        elif item is None or isinstance(item, bool | int):
            return
        else:
            raise ValueError(f"profile JSON contains unsupported value: {type(item).__name__}")

    walk(value, 0)


def _parse_configured_answers(raw_answers: Any) -> tuple[ConfiguredFieldAnswer, ...]:
    if raw_answers in (None, ()):
        return ()
    if not isinstance(raw_answers, list):
        raise ValueError("field_answers must be a list")
    if len(raw_answers) > MAX_PROFILE_ANSWERS:
        raise ValueError("field_answers exceeds cap")
    answers: list[ConfiguredFieldAnswer] = []
    seen: set[tuple[str, str | None, str | None, str]] = set()
    allowed_kinds = {"text", "email", "tel", "url", "number", "date", "textarea", "select", "checkbox", "radio"}
    for raw in raw_answers:
        if not isinstance(raw, dict):
            raise ValueError("field_answers entries must be objects")
        ats = raw.get("ats")
        kind = raw.get("kind")
        value = raw.get("value")
        name = raw.get("name")
        label = raw.get("label")
        if ats not in SUPPORTED_FIELD_ANSWER_ATS:
            raise ValueError("field_answers ats must be greenhouse, lever, or *")
        if kind not in allowed_kinds:
            raise ValueError("field_answers kind is unsupported; file is forbidden")
        if name is None and label is None:
            raise ValueError("field_answers requires name or label")
        if name is not None and not isinstance(name, str):
            raise ValueError("field_answers name must be a string")
        if label is not None and not isinstance(label, str):
            raise ValueError("field_answers label must be a string")
        if kind in {"checkbox", "radio"}:
            if not isinstance(value, bool):
                raise ValueError("checkbox/radio field_answers require boolean values")
        elif not isinstance(value, str):
            raise ValueError("field_answers require string values")
        key = (str(ats), _norm_match(name), _norm_match(label), str(kind))
        if key in seen:
            raise ValueError("duplicate field_answers match tuple")
        seen.add(key)
        answers.append(ConfiguredFieldAnswer(ats=str(ats), name=name, label=label, kind=str(kind), value=value))
    return tuple(answers)


def greenhouse_value_for_field(field: ObservedField, profile: Mapping[str, Any] | ApplicationProfile) -> str | None:
    facts = profile.facts if isinstance(profile, ApplicationProfile) else profile
    canonical = canonical_greenhouse_fact(field)
    if canonical is None:
        return None
    for key in _FACT_ALIASES.get(canonical, (canonical,)):
        value = facts.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _field_group_key(field: ObservedField) -> tuple[str, str]:
    kind = _field_kind(field)
    if kind == "radio":
        return ("radio", field.group_id or field.name or field.field_key)
    if kind == "checkbox" and field.group_id:
        return ("checkbox", field.group_id)
    return (kind, field.target_id)


def unresolved_required_fields(observation: PageObservation, answers: tuple[FieldAnswer, ...]) -> tuple[str, ...]:
    answered = {answer.target_id: answer.value for answer in answers}
    unresolved: list[str] = []
    handled_radio: set[tuple[str, str]] = set()
    for field in observation.fields:
        if (
            not field.required
            or not field.visible
            or not field.enabled
            or _field_is_manual(field)
        ):
            continue
        kind = _field_kind(field)
        if field.readonly and kind != "radio":
            if field.value not in (None, "", False) and field.valid:
                continue
            unresolved.append(field.target_id)
            continue
        if kind == "radio":
            group = _field_group_key(field)
            if group in handled_radio:
                continue
            handled_radio.add(group)
            members = [
                item
                for item in observation.fields
                if _field_group_key(item) == group
                and item.visible
                and item.enabled
            ]
            checked = [
                item.target_id
                for item in members
                if item.value is True
                or answered.get(item.target_id) is True
            ]
            if len(checked) == 1:
                continue
            required_member = next((item for item in members if item.required), field)
            unresolved.append(required_member.target_id)
            continue
        if field.value not in (None, "", False) and field.valid:
            continue
        if kind == "checkbox":
            if field.value is True and field.valid:
                continue
        elif field.target_id in answered:
            candidate = answered[field.target_id]
            if candidate is not None and candidate is not False and candidate != "":
                continue
        unresolved.append(field.target_id)
    return tuple(unresolved)


class GreenhouseAdapter:
    name = "greenhouse"

    def matches(self, url: str, html: str) -> bool:
        del html
        return classify_greenhouse_url(url).allowed

    def deterministic_answers(
        self,
        observation: PageObservation,
        context: ApplicationContext,
        *,
        profile: ApplicationProfile | None = None,
        resume_context: ResumeContext | None = None,
        resume_facts: ResumeFacts | None = None,
    ) -> tuple[FieldAnswer, ...]:
        active_profile = profile if profile is not None else ApplicationProfile(facts=context.profile_facts)
        active_resume_facts = (
            resume_facts
            if resume_facts is not None
            else (resume_context.facts if resume_context is not None else ResumeFacts())
        )
        configured, configured_conflict = _configured_resolution_for_observation(
            observation,
            active_profile.field_answers,
            ats_name=self.name,
        )
        preserved_radio_groups = {
            _field_group_key(field)
            for field in observation.fields
            if _field_kind(field) == "radio" and field.value is True
        }
        preserved_checked_targets = {
            field.target_id
            for field in observation.fields
            if _field_kind(field) == "checkbox" and field.value is True
        }
        answers: list[FieldAnswer] = []
        for field in observation.fields:
            if (
                (_field_kind(field) == "radio" and _field_group_key(field) in preserved_radio_groups)
                or field.target_id in preserved_checked_targets
            ):
                continue
            if not field.visible or not field.enabled or field.readonly or _field_is_manual(field):
                continue
            target_id = field.target_id
            if _is_resume_field(field):
                if (
                    context.resume_available
                    and resume_context is not None
                    and field_accepts_resume(field, resume_context)
                ):
                    # The file path is owned by the workflow/input staging
                    # layer; never expose it through a FieldAnswer.
                    answers.append(FieldAnswer(target_id, "", 1.0, "configured resume upload", "configured"))
                continue
            if target_id in configured_conflict:
                continue
            selected = configured.get(target_id)
            if selected is not None:
                if validate_answer_value(field, selected.value, kind=selected.kind):
                    answers.append(FieldAnswer(target_id, selected.value, 1.0, "configured field answer", "configured"))
                continue
            canonical, descriptor_conflict = _canonical_field_identity(field)
            if descriptor_conflict or canonical is None:
                continue
            value, source_reason = _agreed_fact(canonical, active_profile.facts, active_resume_facts)
            if value is not None and validate_answer_value(field, value, kind=_field_kind(field)):
                answers.append(FieldAnswer(target_id, value, 1.0, source_reason, "profile"))
        return tuple(answers)
class LeverAdapter(GreenhouseAdapter):
    """Lever adapter sharing canonical field identity and deterministic safety."""

    name = "lever"

    def matches(self, url: str, html: str) -> bool:
        del html
        return bool(validate_lever_url is not None and _lever_url_allowed(url))


def _lever_url_allowed(url: str) -> bool:
    try:
        validate_lever_url(url)  # type: ignore[misc]
    except Exception:
        return False
    return True


def _fact_values(source: Mapping[str, Any], canonical: str) -> tuple[str, ...]:
    values: list[str] = []
    for key in _FACT_ALIASES.get(canonical, (canonical,)):
        value = source.get(key)
        if isinstance(value, str) and value.strip():
            cleaned = value.strip()
            if not any(_normalize_fact(canonical, existing) == _normalize_fact(canonical, cleaned) for existing in values):
                values.append(cleaned)
    return tuple(values)


def _agreed_fact(canonical: str, profile_facts: Mapping[str, Any], resume_facts: ResumeFacts) -> tuple[str | None, str]:
    profile_values = _fact_values(profile_facts, canonical)
    if len(profile_values) > 1:
        return None, "profile fact conflict"

    aliases = _FACT_ALIASES.get(canonical, (canonical,))
    if any(alias in resume_facts.ambiguous for alias in aliases):
        return None, "ambiguous resume fact"
    resume_values = _fact_values(resume_facts.facts, canonical)
    if len(resume_values) > 1:
        return None, "ambiguous resume fact"

    profile_value = profile_values[0] if profile_values else None
    resume_value = resume_values[0] if resume_values else None
    if resume_value is None:
        return profile_value, "profile field"
    if profile_value is not None and _normalize_fact(canonical, profile_value) != _normalize_fact(canonical, resume_value):
        return None, "profile/resume fact conflict"
    if profile_value is not None:
        return profile_value, "profile/resume fact"
    return resume_value, "resume fact"


def canonical_greenhouse_fact(field: ObservedField) -> str | None:
    canonical, _conflict = _canonical_field_identity(field)
    return canonical


def _canonical_field_identity(field: ObservedField) -> tuple[str | None, bool]:
    candidates: list[str] = []
    unknown_nonopaque = False
    url_autocomplete = False
    for kind, raw in _descriptor_items(field):
        if not raw:
            continue
        if kind == "autocomplete" and _norm_autocomplete(raw) == "url":
            url_autocomplete = True
            continue
        canonical = _canonical_descriptor(kind, raw)
        if canonical:
            candidates.append(canonical)
        elif not _descriptor_is_opaque(kind, raw):
            unknown_nonopaque = True
    unique = tuple(dict.fromkeys(candidates))
    if len(unique) > 1:
        return None, True
    if url_autocomplete and unique and unique[0] not in {"linkedin", "website"}:
        return None, True
    if unique and unknown_nonopaque:
        return None, True
    return (unique[0] if unique else None), False


def _descriptor_items(field: ObservedField) -> Iterator[tuple[str, str | None]]:
    yield "name", field.name
    yield "label", field.label
    autocomplete = getattr(field, "autocomplete", None)
    if isinstance(autocomplete, str) and autocomplete:
        yield "autocomplete", autocomplete
    for descriptor in field.safety_descriptors:
        if isinstance(descriptor, str) and descriptor.startswith("autocomplete="):
            yield "autocomplete", descriptor.split("=", 1)[1]


_NAME_ALIASES = {
    "first": "first_name",
    "first name": "first_name",
    "first_name": "first_name",
    "given name": "first_name",
    "given_name": "first_name",
    "job_application[first_name]": "first_name",
    "last": "last_name",
    "last name": "last_name",
    "last_name": "last_name",
    "family name": "last_name",
    "family_name": "last_name",
    "surname": "last_name",
    "job_application[last_name]": "last_name",
    "full name": "full_name",
    "full_name": "full_name",
    "name": "full_name",
    "job_application[name]": "full_name",
    "email": "email",
    "email address": "email",
    "email_address": "email",
    "job_application[email]": "email",
    "phone": "phone",
    "phone number": "phone",
    "phone_number": "phone",
    "tel": "phone",
    "mobile phone": "phone",
    "job_application[phone]": "phone",
    "linkedin": "linkedin",
    "linkedin url": "linkedin",
    "linkedin_url": "linkedin",
    "job_application[linkedin]": "linkedin",
    "website": "website",
    "portfolio": "website",
    "personal site": "website",
    "personal_site": "website",
    "job_application[website]": "website",
    "address": "address",
    "street": "address",
    "street address": "address",
    "address 1": "address",
    "address1": "address",
    "address line 1": "address",
    "job_application[address]": "address",
    "city": "city",
    "town": "city",
    "job_application[city]": "city",
    "state": "state",
    "province": "state",
    "state province": "state",
    "state/province": "state",
    "region": "state",
    "job_application[state]": "state",
    "job_application[state_province]": "state",
    "postal": "postal_code",
    "postal code": "postal_code",
    "postcode": "postal_code",
    "zip": "postal_code",
    "zip code": "postal_code",
    "job_application[postal_code]": "postal_code",
    "job_application[zip]": "postal_code",
    "country": "country",
    "country/region": "country",
    "country or region": "country",
    "job_application[country]": "country",
    "job_application[country_code]": "country",
    "address 2": "address_line_2",
    "address2": "address_line_2",
    "address line 2": "address_line_2",
    "apartment": "address_line_2",
    "unit": "address_line_2",
    "suite": "address_line_2",
    "job_application[address_line_2]": "address_line_2",
}
_LABEL_ALIASES = {
    "first": "first_name",
    "first name": "first_name",
    "given name": "first_name",
    "last": "last_name",
    "last name": "last_name",
    "family name": "last_name",
    "surname": "last_name",
    "full name": "full_name",
    "name": "full_name",
    "email": "email",
    "email address": "email",
    "phone": "phone",
    "phone number": "phone",
    "mobile phone": "phone",
    "linkedin": "linkedin",
    "linkedin url": "linkedin",
    "linkedin profile": "linkedin",
    "website": "website",
    "portfolio": "website",
    "personal site": "website",
    "website / portfolio": "website",
    "resume": "resume",
    "resume/cv": "resume",
    "cv": "resume",
    "address": "address",
    "street": "address",
    "street address": "address",
    "address 1": "address",
    "address line 1": "address",
    "city": "city",
    "town": "city",
    "state": "state",
    "province": "state",
    "state/province": "state",
    "region": "state",
    "postal": "postal_code",
    "postal code": "postal_code",
    "postcode": "postal_code",
    "zip": "postal_code",
    "zip code": "postal_code",
    "country": "country",
    "country/region": "country",
    "country or region": "country",
    "address 2": "address_line_2",
    "address line 2": "address_line_2",
    "apartment": "address_line_2",
    "unit": "address_line_2",
    "suite": "address_line_2",
}
_AUTOCOMPLETE_ALIASES = {
    "given-name": "first_name",
    "family-name": "last_name",
    "name": "full_name",
    "email": "email",
    "tel": "phone",
    "tel-national": "phone",
    "street-address": "address",
    "address-line1": "address",
    "address-line2": "address_line_2",
    "address-level2": "city",
    "address-level1": "state",
    "postal-code": "postal_code",
    "country-name": "country",
    "country": "country",
}
_FACT_ALIASES = {
    "first_name": ("first_name", "given_name", "first"),
    "last_name": ("last_name", "family_name", "surname", "last"),
    "full_name": ("full_name", "name"),
    "email": ("email", "email_address"),
    "phone": ("phone", "tel", "phone_number"),
    "linkedin": ("linkedin", "linkedin_url"),
    "website": ("website", "portfolio", "personal_site"),
    "resume": ("resume",),
    "address": ("address", "street_address", "address_line_1", "street"),
    "address_line_2": ("address_line_2", "address2", "apartment", "unit", "suite"),
    "city": ("city", "town"),
    "state": ("state", "state_province", "province", "region"),
    "postal_code": ("postal_code", "postal", "postcode", "zip", "zip_code"),
    "country": ("country", "country_code", "country_name"),
}


def _canonical_descriptor(kind: str, raw: str) -> str | None:
    if kind == "name":
        return _NAME_ALIASES.get(_norm_name(raw))
    if kind == "label":
        return _LABEL_ALIASES.get(_norm_label(raw))
    if kind == "autocomplete":
        normalized = _norm_autocomplete(raw)
        return _AUTOCOMPLETE_ALIASES.get(normalized)
    return None


def _descriptor_is_opaque(kind: str, raw: str) -> bool:
    normalized = _norm_label(raw)
    if normalized == "":
        return True
    if kind == "name":
        # Greenhouse uses generated names for custom answers. Preserve the
        # normalized generic forms while also recognizing bracketed names.
        value = _norm_name(raw)
        bracketed = raw.strip().lower()
        return bool(
            re.fullmatch(r"(?:question|field|input|custom field)[ _-][0-9]+", value)
            or re.fullmatch(
                r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
                value,
            )
            or re.fullmatch(
                r"job_application\[answers_attributes\]\[[0-9]+\]\[(?:text_value|answer_value|boolean_value)\]",
                bracketed,
            )
        )
    if kind == "label":
        return bool(re.fullmatch(r"(?:question|field|input)(?: [0-9]+)?", normalized))
    if kind == "autocomplete":
        return _norm_autocomplete(raw) in {"", "on", "off"}
    return False


def _norm_name(value: str) -> str:
    return " ".join(value.strip().lower().replace("_", " ").split()) if "[" not in value else " ".join(value.strip().lower().split())


def _norm_label(value: str) -> str:
    return " ".join(value.strip().lower().split())


def _norm_match(value: str | None) -> str | None:
    return None if value is None else _norm_label(value)


def _norm_autocomplete(value: str) -> str:
    tokens = [token for token in value.strip().lower().split() if token not in {"section-*", "home", "work", "mobile", "shipping", "billing"}]
    return tokens[-1] if tokens else ""


_CONFLICT = object()


def _configured_answer_for_field(
    field: ObservedField,
    answers: tuple[ConfiguredFieldAnswer, ...],
    *,
    ats_name: str = "greenhouse",
) -> ConfiguredFieldAnswer | object | None:
    matches: list[ConfiguredFieldAnswer] = []
    for answer in answers:
        if _configured_answer_matches(field, answer):
            matches.append(answer)
            continue
        if answer.name is not None and answer.label is not None:
            name_match = _norm_match(answer.name) == _norm_match(field.name)
            label_match = _norm_match(answer.label) == _norm_match(field.label)
            if name_match != label_match:
                return _CONFLICT
    if not matches:
        return None
    specific = [answer for answer in matches if answer.ats == ats_name]
    selected = specific or [answer for answer in matches if answer.ats == "*"]
    if not selected:
        return None
    first = selected[0]
    for answer in selected[1:]:
        if answer.kind != first.kind or answer.value != first.value:
            return _CONFLICT
    return first


def _configured_answer_matches(field: ObservedField, answer: ConfiguredFieldAnswer) -> bool:
    name = _norm_match(field.name)
    label = _norm_match(field.label)
    if answer.name is not None and _norm_match(answer.name) != name:
        return False
    if answer.label is not None and _norm_match(answer.label) != label:
        return False
    return True


def _configured_resolution_for_observation(
    observation: PageObservation,
    answers: tuple[ConfiguredFieldAnswer, ...],
    *,
    ats_name: str = "greenhouse",
) -> tuple[dict[str, ConfiguredFieldAnswer], set[str]]:
    """Resolve exact configured entries once across one observation."""
    matches_by_target: dict[str, list[ConfiguredFieldAnswer]] = {}
    conflicts: set[str] = set()

    for answer in answers:
        name_matches = (
            [field for field in observation.fields if _norm_match(field.name) == _norm_match(answer.name)]
            if answer.name is not None else list(observation.fields)
        )
        label_matches = (
            [field for field in observation.fields if _norm_match(field.label) == _norm_match(answer.label)]
            if answer.label is not None else list(observation.fields)
        )
        matched = [field for field in name_matches if field in label_matches]
        if answer.name is not None and answer.label is not None and not matched:
            conflicts.update(field.target_id for field in (*name_matches, *label_matches))
            continue
        if len(matched) > 1:
            conflicts.update(field.target_id for field in matched)
        for field in matched:
            matches_by_target.setdefault(field.target_id, []).append(answer)

    resolved: dict[str, ConfiguredFieldAnswer] = {}
    for target_id, matches in matches_by_target.items():
        specific = [answer for answer in matches if answer.ats == ats_name]
        selected = specific or [answer for answer in matches if answer.ats == "*"]
        if not selected:
            continue
        first = selected[0]
        if any(answer.kind != first.kind or answer.value != first.value for answer in selected[1:]):
            conflicts.add(target_id)
            continue
        if target_id not in conflicts:
            resolved[target_id] = first
    return resolved, conflicts


def validate_answer_value(field: ObservedField, value: str | bool, *, kind: str | None = None) -> bool:
    """Purely validate a proposed value against the observed field contract."""

    field_kind = _field_kind(field)
    answer_kind = (kind or field_kind).lower()
    if answer_kind in {"checkbox", "radio"}:
        return isinstance(value, bool) and field_kind == answer_kind
    if not isinstance(value, str) or _contains_forbidden_controls(value):
        return False
    if field_kind in {"checkbox", "radio", "file"}:
        return False
    if field.required and value == "":
        return False
    if answer_kind != field_kind and not ({answer_kind, field_kind} <= {"text", "textarea"}):
        return False

    is_textarea = field_kind == "textarea"
    if not is_textarea and any(char in value for char in "\r\n\t"):
        return False
    portable_cap = MAX_TEXTAREA_CHARS if is_textarea else MAX_SINGLE_LINE_CHARS
    if len(value) > portable_cap:
        return False
    if field.min_length is not None and len(value) < field.min_length:
        return False
    if field.max_length is not None and len(value) > min(field.max_length, portable_cap):
        return False

    if answer_kind == "email":
        if len(value) > MAX_EMAIL_CHARS or value.count("@") != 1 or not value.isascii():
            return False
        local, domain = value.rsplit("@", 1)
        local_atom = r"[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+"
        domain_labels = domain.split(".")
        if (
            not local
            or len(local) > 64
            or re.fullmatch(fr"{local_atom}(?:\.{local_atom})*", local) is None
            or not domain
            or len(domain) > 253
            or len(domain_labels) < 2
            or any(
                re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", label) is None
                for label in domain_labels
            )
            or re.fullmatch(r"[A-Za-z](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", domain_labels[-1]) is None
        ):
            return False
        return _pattern_matches(field, value)

    if answer_kind == "tel":
        if len(value) > MAX_TEL_CHARS or re.fullmatch(r"[0-9+().\- ]+", value) is None:
            return False
        digits = sum(char.isdigit() for char in value)
        return 7 <= digits <= 15 and _pattern_matches(field, value)

    if answer_kind == "url":
        if len(value) > MAX_URL_CHARS or any(char.isspace() for char in value):
            return False
        try:
            parts = urlsplit(value)
            hostname = parts.hostname
            _ = parts.port
        except ValueError:
            return False
        if not (
            parts.scheme == "https"
            and bool(parts.netloc)
            and bool(hostname)
            and not parts.username
            and not parts.password
        ):
            return False
        return _pattern_matches(field, value)

    if answer_kind == "date":
        if re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value) is None:
            return False
        try:
            parsed = date.fromisoformat(value)
        except ValueError:
            return False
        if field.min_value:
            try:
                if parsed < date.fromisoformat(field.min_value):
                    return False
            except ValueError:
                return False
        if field.max_value:
            try:
                if parsed > date.fromisoformat(field.max_value):
                    return False
            except ValueError:
                return False
        if field.step not in (None, "", "any"):
            if re.fullmatch(r"[0-9]+", str(field.step)) is None or int(str(field.step)) <= 0:
                return False
            base = date.fromisoformat(field.min_value) if field.min_value else date(1970, 1, 1)
            if (parsed - base).days % int(str(field.step)) != 0:
                return False
        return _pattern_matches(field, value)


    if answer_kind == "number" and not _number_value_is_valid(field, value):
        return False

    if answer_kind == "select":
        options = tuple(option for option in field.options if option.enabled)
        if not options or value not in {option.value for option in options}:
            return False

    return _pattern_matches(field, value)


def _pattern_matches(field: ObservedField, value: str) -> bool:
    if not field.pattern:
        return True
    try:
        return re.fullmatch(field.pattern, value) is not None
    except re.error:
        return False
    return True


def _contains_forbidden_controls(value: str) -> bool:
    return any(
        (
            char not in {"\n", "\t"}
            and unicodedata.category(char) in {"Cc", "Cf"}
        )
        for char in value
    )


def _number_value_is_valid(field: ObservedField, value: str) -> bool:
    if re.fullmatch(r"-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?", value) is None:
        return False
    try:
        numeric = Decimal(value)
    except InvalidOperation:
        return False
    if not numeric.is_finite():
        return False
    try:
        minimum = Decimal(str(field.min_value)) if field.min_value not in (None, "") else None
        maximum = Decimal(str(field.max_value)) if field.max_value not in (None, "") else None
        if minimum is not None and not minimum.is_finite():
            return False
        if maximum is not None and not maximum.is_finite():
            return False
        if minimum is not None and numeric < minimum:
            return False
        if maximum is not None and numeric > maximum:
            return False
        if field.step not in (None, "", "any"):
            step = Decimal(str(field.step))
            if not step.is_finite() or step <= 0:
                return False
            base = minimum if minimum is not None else Decimal("0")
            if (numeric - base) % step != 0:
                return False
    except (InvalidOperation, ValueError, ZeroDivisionError):
        return False
    return True


def field_accepts_resume(field: ObservedField, context: ResumeContext, accept: tuple[str, ...] | None = None) -> bool:
    if _field_kind(field) != "file":
        return False
    accept_values = tuple(accept if accept is not None else field.accept)
    tokens = [
        token.strip().lower()
        for raw in accept_values
        for token in raw.split(",")
        if token.strip()
    ]
    # A genuinely empty accept attribute is unrestricted. Blank entries mixed
    # with real constraints are not an unrestricted wildcard.
    if not tokens:
        return True
    suffix = Path(context.basename).suffix.lower()
    media = context.media_type.lower()
    for token in tokens:
        if token.startswith(".") and token == suffix:
            return True
        if token.endswith("/*") and media.startswith(token[:-1]):
            return True
        if "/" in token and token == media:
            return True
    return False


def _is_resume_field(field: ObservedField) -> bool:
    return _field_kind(field) == "file" and _norm_label(field.label) in {"resume", "resume/cv", "cv"}


def _field_kind(field: ObservedField) -> str:
    return field.kind.lower() or "text"


def _field_is_manual(field: ObservedField) -> bool:
    options = tuple((option.value, option.label) for option in field.options)
    descriptors = tuple(field.safety_descriptors)
    return classify_descriptors(descriptors, field_kind=field.kind, options=options) is DescriptorSafety.SENSITIVE



ADAPTERS: tuple[ATSAdapter, ...] = (GreenhouseAdapter(), LeverAdapter())


def select_adapter(name: str, *, url: str = "", html: str = "") -> ATSAdapter | None:
    if name != "auto":
        for adapter in ADAPTERS:
            if adapter.name == name:
                return adapter
        raise ValueError(f"unsupported ATS: {name}")
    for adapter in ADAPTERS:
        if adapter.matches(url, html):
            return adapter
    return None


KNOWN_ATS_MARKERS = ("workday", "myworkdayjobs.com", "lever.co", "ashbyhq.com", "smartrecruiters", "greenhouse", "grnh.se")


def classify_application_site(*, adapter: ATSAdapter | None, url: str, html: str) -> str:
    if adapter is not None:
        return adapter.name
    host = (urlsplit(url).hostname or "").lower()
    haystack = f"{url} {host} {html}".lower()
    if any(marker in haystack for marker in KNOWN_ATS_MARKERS):
        return "unknown_ats"
    return "in_house"


def classify_ats(name: str = "auto", *, url: str = "", html: str = "") -> ATSClassification:
    adapter = select_adapter(name, url=url, html=html)
    if adapter is None:
        site_classification = classify_application_site(adapter=None, url=url, html=html)
        return ATSClassification(site_classification if site_classification != "in_house" else None, 0.0, "no supported ATS matched")
    confidence = 1.0 if name != "auto" else 0.95
    return ATSClassification(adapter.name, confidence, "explicit ATS selection" if name != "auto" else "matched supported ATS adapter")


def merge_plans(
    adapter_answers: tuple[FieldAnswer, ...],
    llm_plan: AutofillPlan,
    observation: PageObservation | None = None,
) -> AutofillPlan:
    by_target_id = {answer.target_id: answer for answer in adapter_answers}
    for answer in llm_plan.answers:
        by_target_id.setdefault(answer.target_id, answer)
    private_raw = dict(thaw_json(llm_plan.private_raw))
    private_raw["deterministic_answer_count"] = len(adapter_answers)
    if private_raw.get("blocking_sensitive_fields"):
        return AutofillPlan(
            answers=(),
            resume_upload_target_id=None,
            safe_click_target_id=None,
            status=llm_plan.status,
            reason_code=llm_plan.reason_code,
            skipped_target_ids=llm_plan.skipped_target_ids,
            private_raw=private_raw,
        )
    answers = tuple(by_target_id.values())
    unresolved = unresolved_required_fields(observation, answers) if observation is not None else ()
    status = "ready" if answers else llm_plan.status
    reason_code = llm_plan.reason_code
    if unresolved:
        private_raw["unresolved_required_target_ids"] = list(unresolved)
        status = "manual"
        reason_code = PublicReasonCode.required_safe_fields_unresolved
    return AutofillPlan(
        answers=answers,
        resume_upload_target_id=llm_plan.resume_upload_target_id,
        safe_click_target_id=llm_plan.safe_click_target_id,
        status=status,
        reason_code=reason_code,
        skipped_target_ids=llm_plan.skipped_target_ids,
        private_raw=private_raw,
    )
