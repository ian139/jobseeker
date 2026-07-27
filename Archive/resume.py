"""Backlog-to-resume workflow: models, claims, scoring, validation, and generation."""

from __future__ import annotations

import json
import re
import hashlib
import unicodedata
from dataclasses import dataclass, field as dataclass_field
from enum import Enum
from pathlib import Path
from typing import Any, Mapping, Sequence

from .ats import ApplicationProfile, LoadedApplicationProfile, ResumeContext
MAX_RESUME_JSON_BYTES = 100 * 1024  # 100 KB
MAX_TEXT_CHARS = 50_000
MAX_LIST_ITEMS = 256


class ResumeReasonCode(str, Enum):
    PROMPT_INJECTION_DETECTED = "prompt_injection_detected"
    UNSUPPORTED_CLAIM = "unsupported_claim"
    INVALID_PROVENANCE_HASH = "invalid_provenance_hash"
    MALFORMED_JSON = "malformed_json"
    OVERSIZED_JSON = "oversized_json"
    EXTRANEOUS_KEYS = "extraneous_keys"
    SENSITIVE_INFERENCE_REJECTED = "sensitive_inference_rejected"
    MISSING_CITATION = "missing_citation"
    ALTERED_FACT = "altered_fact"
    PRIVACY_VIOLATION = "privacy_violation"
    UNSUPPORTED_REJECTED = "unsupported_rejected"


class ResumeValidationError(ValueError):
    """Raised when resume validation fails with a fixed reason code."""

    def __init__(self, code: ResumeReasonCode | str, message: str | None = None) -> None:
        if isinstance(code, ResumeReasonCode):
            self.code = code
        elif isinstance(code, str):
            val = code.lower().strip()
            member = next((m for m in ResumeReasonCode if m.value == val or m.name.lower() == val), None)
            self.code = member if member is not None else code
        else:
            self.code = code
        self.message = message or str(self.code)
        super().__init__(f"[{self.code}] {self.message}")


@dataclass(frozen=True)
class GeneratedResumeArtifact:
    resume_id: str
    job_id: int
    job_snapshot_sha256: str
    profile_sha256: str
    source_resume_sha256: str
    generation_config_sha256: str
    content_sha256: str
    pdf_sha256: str
    private_pdf_path: Path
    created_at: str


@dataclass(frozen=True)
class JobResumeSnapshot:
    job_id: int
    canonical_application_url: str
    title: str
    company: str
    description: str
    source_identifier: str
    job_snapshot_sha256: str
    location: str | None = None
    requirements: tuple[str, ...] = ()

    @property
    def snapshot_sha256(self) -> str:
        return self.job_snapshot_sha256

    @property
    def description_text(self) -> str:
        return self.description


@dataclass(frozen=True)
class CandidateClaim:
    claim_id: str
    category: str
    text: str
    source: str
    source_sha256: str
    sensitive: bool = False
    metadata: dict[str, Any] = dataclass_field(default_factory=dict)

    @property
    def organization(self) -> str | None:
        return self.metadata.get("organization") or self.metadata.get("org")

    @property
    def role(self) -> str | None:
        return self.metadata.get("role")

    @property
    def dates(self) -> str | None:
        return self.metadata.get("dates")

    @property
    def claim_sha256(self) -> str:
        payload = {
            "category": self.category,
            "metadata": self.metadata,
            "sensitive": self.sensitive,
            "source": self.source,
            "source_sha256": self.source_sha256,
            "text": self.text,
        }
        return compute_sha256(canonical_json(payload))


@dataclass(frozen=True)
class ResumeScoreData:
    matched_requirements: tuple[str, ...]
    unsupported_requirements: tuple[str, ...]
    selected_claims: tuple[str, ...]
    omitted_claims: tuple[str, ...]
    matched_req_to_claim_ids: dict[str, tuple[str, ...]] = dataclass_field(default_factory=dict)
    missing_fact_questions: tuple[str, ...] = ()
    validation_decisions: tuple[str, ...] = ()


def normalize_text(text: str) -> str:
    """Normalize whitespace and Unicode characters deterministically."""
    if not isinstance(text, str):
        return ""
    normalized = unicodedata.normalize("NFKC", text)
    lines = [re.sub(r"[ \t\r\f\v]+", " ", line).strip() for line in normalized.splitlines()]
    non_empty = [line for line in lines if line]
    return "\n".join(non_empty)


def canonical_json(obj: Any) -> str:
    """Produce deterministic JSON representation of any object."""
    def _freeze(val: Any) -> Any:
        if isinstance(val, (dict, Mapping)):
            return {str(k): _freeze(v) for k, v in sorted(val.items())}
        if isinstance(val, (list, tuple)):
            return [_freeze(x) for x in val]
        if isinstance(val, Enum):
            return val.value
        if isinstance(val, Path):
            return str(val)
        return val

    return json.dumps(_freeze(obj), sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def compute_sha256(data: str | bytes) -> str:
    """Compute SHA-256 hex digest of string or bytes."""
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def build_job_resume_snapshot(
    job_id: int | str,
    title: str,
    company: str,
    description: str | None = None,
    description_text: str | None = None,
    canonical_application_url: str = "",
    location: str | None = None,
    source_identifier: str = "",
    requirements: Sequence[str] | None = None,
) -> JobResumeSnapshot:
    """Construct a frozen job resume snapshot with deterministic hash."""
    if isinstance(job_id, str):
        digits = re.sub(r"\D", "", job_id)
        int_job_id = int(digits) if digits else 0
    else:
        int_job_id = int(job_id)

    raw_desc = description if description is not None else (description_text if description_text is not None else "")
    norm_desc = normalize_text(raw_desc)
    if not norm_desc or not norm_desc.strip():
        raise ValueError("Job description cannot be blank or unusable")

    norm_title = normalize_text(title)
    norm_company = normalize_text(company)
    norm_loc = normalize_text(location) if location else None
    norm_url = canonical_application_url.strip() if canonical_application_url else ""
    norm_src_id = source_identifier.strip() if source_identifier else ""

    if requirements is None:
        extracted: list[str] = []
        for line in norm_desc.split("\n"):
            line_str = line.strip("•-* ").strip()
            if line_str and len(line_str) > 3:
                extracted.append(line_str)
        req_tuple = tuple(extracted)
    else:
        req_tuple = tuple(normalize_text(r) for r in requirements if normalize_text(r))

    payload = {
        "canonical_application_url": norm_url,
        "company": norm_company,
        "description": norm_desc,
        "job_id": int_job_id,
        "location": norm_loc,
        "requirements": list(req_tuple),
        "source_identifier": norm_src_id,
        "title": norm_title,
    }
    job_snapshot_sha256 = compute_sha256(canonical_json(payload))

    return JobResumeSnapshot(
        job_id=int_job_id,
        canonical_application_url=norm_url,
        title=norm_title,
        company=norm_company,
        location=norm_loc,
        description=norm_desc,
        source_identifier=norm_src_id,
        job_snapshot_sha256=job_snapshot_sha256,
        requirements=req_tuple,
    )


_PROTECTED_CLASS_PATTERNS = (
    r"\b(?:race|ethnicity|religion|political\s+party|sexual\s+orientation|marital\s+status|ssn|social\s+security)\b",
)

_PRIVACY_PATTERNS = (
    r"(?:/Users/|/home/|[A-Za-z]:\\)",
    r"file://",
    r"\b(?:127\.0\.0\.1|localhost|10\.\d{1,3}\.\d{1,3}\.\d{1,3}|192\.168\.\d{1,3}\.\d{1,3})\b",
    r"(?:password|api_key|bearer|secret)\s*[:=]\s*\S+",
    r"(?<![\w.+-])[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}(?![\w.-])",
)

_CONTACT_FIELD_NAMES = frozenset(
    {
        "address",
        "address_line_1",
        "address_line_2",
        "city",
        "email",
        "email_address",
        "first_name",
        "full_name",
        "last_name",
        "linkedin",
        "linkedin_url",
        "location",
        "mobile",
        "mobile_phone",
        "name",
        "phone",
        "phone_number",
        "postal_code",
        "portfolio",
        "personal_site",
        "state",
        "street_address",
        "tel",
        "telephone",
        "website",
        "zip",
    }
)

_INJECTION_PATTERNS = (
    r"ignore\s+(?:all\s+)?previous\s+instructions",
    r"system\s+prompt",
    r"you\s+are\s+an\s+ai",
    r"override\s+(?:safety|instructions)",
    r"format\s+as\s+json",
    r"\[system\]",
    r"drop\s+table",
    r"<script",
)


def extract_candidate_claims(
    profile: LoadedApplicationProfile | ApplicationProfile | None = None,
    resume_context: ResumeContext | None = None,
) -> tuple[CandidateClaim, ...]:
    """Extract canonical claims from LoadedApplicationProfile/ApplicationProfile and ResumeContext."""
    claims: list[CandidateClaim] = []

    profile_obj: ApplicationProfile | None = None
    profile_sha256 = compute_sha256("")
    if isinstance(profile, LoadedApplicationProfile):
        profile_obj = profile.profile
        if profile.source_sha256:
            profile_sha256 = profile.source_sha256
    elif isinstance(profile, ApplicationProfile):
        profile_obj = profile

    if profile_obj is not None:
        facts = getattr(profile_obj, "facts", {}) or {}
        if profile_sha256 == compute_sha256(""):
            profile_sha256 = compute_sha256(canonical_json(facts))

        description = getattr(profile_obj, "description", "") or ""
        if isinstance(description, str) and description.strip():
            _add_claim(claims, category="summary", text=normalize_text(description), source="profile", source_sha256=profile_sha256)

        if isinstance(facts, Mapping):
            skills = facts.get("skills") or facts.get("top_skills") or []
            if isinstance(skills, (list, tuple)):
                for s in skills:
                    if isinstance(s, str) and s.strip():
                        _add_claim(claims, category="skill", text=normalize_text(s), source="profile", source_sha256=profile_sha256)

            work_history = facts.get("work_history") or facts.get("experience") or []
            if isinstance(work_history, (list, tuple)):
                for idx, item in enumerate(work_history):
                    if isinstance(item, Mapping):
                        org = normalize_text(str(item.get("company") or item.get("organization") or item.get("employer") or "")) or None
                        role = normalize_text(str(item.get("title") or item.get("role") or item.get("position") or "")) or None
                        dates = normalize_text(str(item.get("dates") or item.get("duration") or item.get("period") or "")) or None
                        entry_id = str(item.get("source_entry_id") or f"exp-{idx + 1}")
                        bullets = item.get("highlights") or item.get("bullets") or item.get("description") or []
                        if isinstance(bullets, str):
                            bullets = [bullets]
                        if isinstance(bullets, (list, tuple)):
                            for b in bullets:
                                if isinstance(b, str) and b.strip():
                                    _add_claim(
                                        claims,
                                        category="experience",
                                        text=normalize_text(b),
                                        source="profile",
                                        source_sha256=profile_sha256,
                                        metadata={"organization": org, "role": role, "dates": dates, "source_entry_id": entry_id},
                                    )

            education = facts.get("education") or []
            if isinstance(education, (list, tuple)):
                for item in education:
                    if isinstance(item, Mapping):
                        inst = normalize_text(str(item.get("institution") or item.get("school") or item.get("university") or "")) or None
                        degree = normalize_text(str(item.get("degree") or item.get("major") or item.get("field") or "")) or None
                        dates = normalize_text(str(item.get("dates") or item.get("year") or "")) or None

                        education_descriptions: list[str] = []
                        for description_field in ("description", "highlights"):
                            raw_descriptions = item.get(description_field)
                            if isinstance(raw_descriptions, str):
                                description_items: Sequence[Any] = (raw_descriptions,)
                            elif isinstance(raw_descriptions, (list, tuple)):
                                description_items = raw_descriptions
                            else:
                                description_items = ()
                            for description_item in description_items:
                                if isinstance(description_item, str):
                                    normalized_description = normalize_text(description_item)
                                    if normalized_description:
                                        education_descriptions.append(normalized_description)

                        metadata = {"institution": inst, "degree": degree, "dates": dates}
                        if education_descriptions:
                            for description in education_descriptions:
                                _add_claim(
                                    claims,
                                    category="education",
                                    text=description,
                                    source="profile",
                                    source_sha256=profile_sha256,
                                    metadata=metadata,
                                )
                        else:
                            text_parts = [p for p in [degree, inst] if p]
                            combined_text = " - ".join(text_parts) if text_parts else (inst or degree or "Degree")
                            _add_claim(
                                claims,
                                category="education",
                                text=normalize_text(combined_text),
                                source="profile",
                                source_sha256=profile_sha256,
                                metadata=metadata,
                            )

            for k, v in facts.items():
                if k not in {"skills", "top_skills", "work_history", "experience", "education"}:
                    if isinstance(v, str) and v.strip():
                        key_norm = re.sub(r"[^a-z0-9]+", "_", str(k).casefold()).strip("_")
                        key_parts = set(key_norm.split("_"))
                        contact_like = (
                            key_norm in _CONTACT_FIELD_NAMES
                            or key_norm.startswith("contact_")
                            or bool(key_parts & {"email", "phone", "mobile", "address", "linkedin", "website", "portfolio", "telephone", "tel"})
                            or key_norm.endswith("_name")
                        )
                        category = "contact" if contact_like else "general"
                        _add_claim(
                            claims,
                            category=category,
                            text=normalize_text(f"{k}: {v}"),
                            source="profile",
                            source_sha256=profile_sha256,
                        )

    source_resume_sha256 = compute_sha256("")
    if resume_context is not None:
        source_resume_sha256 = getattr(resume_context, "sha256", compute_sha256(""))
        resume_text = getattr(resume_context, "text", "") or ""
        if isinstance(resume_text, str) and resume_text.strip():
            norm_resume = normalize_text(resume_text)
            for line in norm_resume.split("\n"):
                line_clean = line.strip("•-* ").strip()
                if line_clean and len(line_clean) > 2:
                    _add_claim(claims, category="general", text=line_clean, source="source_resume", source_sha256=source_resume_sha256)

    seen_ids: set[str] = set()
    unique_claims: list[CandidateClaim] = []
    for c in claims:
        if c.claim_id not in seen_ids:
            seen_ids.add(c.claim_id)
            unique_claims.append(c)

    return tuple(unique_claims)


def _add_claim(
    claims: list[CandidateClaim],
    category: str,
    text: str,
    source: str,
    source_sha256: str,
    metadata: dict[str, Any] | None = None,
) -> None:
    norm_text = normalize_text(text)
    if not norm_text:
        return
    meta = dict(metadata or {})

    sensitive = category == "contact"
    # Metadata is part of the grounded claim; inspect it too so contact data
    # cannot become a non-sensitive container claim.
    sensitive_source = canonical_json(meta)
    for pat in _PROTECTED_CLASS_PATTERNS + _PRIVACY_PATTERNS:
        if re.search(pat, f"{norm_text}\n{sensitive_source}", re.IGNORECASE):
            sensitive = True
            break

    payload = {
        "category": category,
        "metadata": meta,
        "sensitive": sensitive,
        "source": source,
        "source_sha256": source_sha256,
        "text": norm_text,
    }
    claim_sha256 = compute_sha256(canonical_json(payload))
    # Include the category and full content digest: IDs are deterministic,
    # collision-resistant, and change when provenance or sensitivity changes.
    claim_id = f"{category}-{claim_sha256}"
    claims.append(
        CandidateClaim(
            claim_id=claim_id,
            category=category,
            text=norm_text,
            source=source,
            source_sha256=source_sha256,
            sensitive=sensitive,
            metadata=meta,
        )
    )


def score_resume_against_job(
    job_snapshot: JobResumeSnapshot,
    claims: Sequence[CandidateClaim],
    selected_claim_ids: Sequence[str] | None = None,
) -> ResumeScoreData:
    """Compute deterministic requirement matches and rendered/omitted claims."""
    matched_reqs: list[str] = []
    unsupported_reqs: list[str] = []
    matched_map: dict[str, list[str]] = {}

    non_sensitive_claims = [claim for claim in claims if not claim.sensitive]
    claim_by_id = {claim.claim_id: claim for claim in non_sensitive_claims}

    def tokens(value: str) -> set[str]:
        return {
            token
            for token in re.findall(r"\w+", normalize_text(value).casefold())
            if len(token) >= 2
        }

    for req in job_snapshot.requirements:
        if any(re.search(pattern, req, re.IGNORECASE) for pattern in _INJECTION_PATTERNS):
            continue
        req_tokens = tokens(req)
        matching_cids = [
            claim.claim_id
            for claim in non_sensitive_claims
            if req_tokens & tokens(claim.text)
        ]
        if matching_cids:
            matched_reqs.append(req)
            matched_map[req] = matching_cids
        else:
            unsupported_reqs.append(req)

    if selected_claim_ids is None:
        selected_sequence = [claim.claim_id for claim in non_sensitive_claims]
    else:
        selected_sequence = []
        seen: set[str] = set()
        for claim_id in selected_claim_ids:
            if claim_id in claim_by_id and claim_id not in seen:
                selected_sequence.append(claim_id)
                seen.add(claim_id)

    selected_set = set(selected_sequence)
    omitted_sequence = [
        claim.claim_id
        for claim in non_sensitive_claims
        if claim.claim_id not in selected_set
    ]
    questions = tuple(f"Do you have experience with '{req}'?" for req in sorted(unsupported_reqs))
    decisions = (f"Matched {len(matched_reqs)} requirements.", f"Omitted {len(omitted_sequence)} claims.")

    return ResumeScoreData(
        matched_requirements=tuple(sorted(matched_reqs)),
        unsupported_requirements=tuple(sorted(unsupported_reqs)),
        selected_claims=tuple(selected_sequence),
        omitted_claims=tuple(omitted_sequence),
        matched_req_to_claim_ids={key: tuple(value) for key, value in sorted(matched_map.items())},
        missing_fact_questions=questions,
        validation_decisions=decisions,
    )


REQUIRED_TOP_KEYS = {
    "schema_version",
    "job_snapshot_sha256",
    "profile_sha256",
    "source_resume_sha256",
    "headline",
    "summary",
    "experience",
    "skills",
    "education",
    "omitted_claim_ids",
    "missing_fact_questions",
    "generation_notes",
}
def _require_text(value: Any, label: str) -> str:
    if type(value) is not str:
        raise ResumeValidationError(ResumeReasonCode.MALFORMED_JSON, f"{label} must be a string")
    if len(value) > MAX_TEXT_CHARS:
        raise ResumeValidationError(ResumeReasonCode.OVERSIZED_JSON, f"{label} exceeds {MAX_TEXT_CHARS} characters")
    return value


def _require_list(value: Any, label: str) -> list[Any]:
    if type(value) is not list:
        raise ResumeValidationError(ResumeReasonCode.MALFORMED_JSON, f"{label} must be a list")
    if len(value) > MAX_LIST_ITEMS:
        raise ResumeValidationError(ResumeReasonCode.OVERSIZED_JSON, f"{label} exceeds {MAX_LIST_ITEMS} items")
    return value



def validate_tailored_resume_json(
    raw_or_dict: str | bytes | dict[str, Any],
    job_snapshot: JobResumeSnapshot,
    claims: Sequence[CandidateClaim],
    expected_profile_sha256: str,
    expected_source_resume_sha256: str,
) -> dict[str, Any]:
    """Strictly validate resume JSON against size, structure, provenance, safety, and claim citations."""
    if isinstance(raw_or_dict, (str, bytes)):
        raw_bytes = raw_or_dict.encode("utf-8") if isinstance(raw_or_dict, str) else raw_or_dict
        if len(raw_bytes) > MAX_RESUME_JSON_BYTES:
            raise ResumeValidationError(ResumeReasonCode.OVERSIZED_JSON, f"JSON size {len(raw_bytes)} exceeds limit {MAX_RESUME_JSON_BYTES}")
        try:
            data = json.loads(raw_bytes)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ResumeValidationError(ResumeReasonCode.MALFORMED_JSON, "Invalid JSON encoding") from exc
    elif isinstance(raw_or_dict, dict):
        try:
            raw_bytes = canonical_json(raw_or_dict).encode("utf-8")
        except (TypeError, ValueError, OverflowError) as exc:
            raise ResumeValidationError(ResumeReasonCode.MALFORMED_JSON, "Resume payload is not JSON serializable") from exc
        if len(raw_bytes) > MAX_RESUME_JSON_BYTES:
            raise ResumeValidationError(ResumeReasonCode.OVERSIZED_JSON, f"JSON size {len(raw_bytes)} exceeds limit {MAX_RESUME_JSON_BYTES}")
        data = raw_or_dict
    else:
        raise ResumeValidationError(ResumeReasonCode.MALFORMED_JSON, "Resume payload must be a dict or JSON string")

    if not isinstance(data, dict):
        raise ResumeValidationError(ResumeReasonCode.MALFORMED_JSON, "Root resume payload must be a JSON object")

    # Check top-level keys exact match
    data_keys = set(data.keys())
    extraneous = data_keys - REQUIRED_TOP_KEYS
    if extraneous:
        raise ResumeValidationError(ResumeReasonCode.EXTRANEOUS_KEYS, f"Extraneous top-level keys: {sorted(extraneous)}")
    missing = REQUIRED_TOP_KEYS - data_keys
    if missing:
        raise ResumeValidationError(ResumeReasonCode.MALFORMED_JSON, f"Missing required top-level keys: {sorted(missing)}")

    # Provenance hash check
    for field_name, expected in (
        ("job_snapshot_sha256", job_snapshot.job_snapshot_sha256),
        ("profile_sha256", expected_profile_sha256),
        ("source_resume_sha256", expected_source_resume_sha256),
    ):
        actual = _require_text(data.get(field_name), field_name)
        if actual != expected:
            raise ResumeValidationError(ResumeReasonCode.INVALID_PROVENANCE_HASH, f"Stale {field_name}")

    schema_version = data.get("schema_version")
    if type(schema_version) is not int or schema_version != 1:
        raise ResumeValidationError(ResumeReasonCode.MALFORMED_JSON, "schema_version must be integer 1")

    # Validate headline and summary schema
    headline = data.get("headline")
    if not isinstance(headline, dict) or set(headline.keys()) != {"text", "claim_ids"}:
        raise ResumeValidationError(ResumeReasonCode.EXTRANEOUS_KEYS if isinstance(headline, dict) else ResumeReasonCode.MALFORMED_JSON, "headline must have exact keys text, claim_ids")
    _require_text(headline.get("text"), "headline.text")
    _require_list(headline.get("claim_ids"), "headline.claim_ids")

    summary = data.get("summary")
    if not isinstance(summary, dict) or set(summary.keys()) != {"text", "claim_ids"}:
        raise ResumeValidationError(ResumeReasonCode.EXTRANEOUS_KEYS if isinstance(summary, dict) else ResumeReasonCode.MALFORMED_JSON, "summary must have exact keys text, claim_ids")
    _require_text(summary.get("text"), "summary.text")
    _require_list(summary.get("claim_ids"), "summary.claim_ids")

    # Validate experience
    exp_list = _require_list(data.get("experience"), "experience")
    for exp in exp_list:
        if not isinstance(exp, dict) or set(exp.keys()) != {"source_entry_id", "organization", "role", "dates", "bullets"}:
            raise ResumeValidationError(ResumeReasonCode.EXTRANEOUS_KEYS if isinstance(exp, dict) else ResumeReasonCode.MALFORMED_JSON, "experience item keys mismatch")
        for field_name in ("source_entry_id", "organization", "role", "dates"):
            field_value = _require_text(exp.get(field_name), f"experience.{field_name}")
            if not field_value.strip():
                raise ResumeValidationError(ResumeReasonCode.MALFORMED_JSON, f"experience.{field_name} must be non-empty")
        bullets = _require_list(exp.get("bullets"), "experience.bullets")
        if not bullets:
            raise ResumeValidationError(ResumeReasonCode.MISSING_CITATION, "experience container requires cited bullets")
        for b in bullets:
            if not isinstance(b, dict) or set(b.keys()) != {"text", "claim_ids"}:
                raise ResumeValidationError(ResumeReasonCode.EXTRANEOUS_KEYS if isinstance(b, dict) else ResumeReasonCode.MALFORMED_JSON, "experience bullet keys mismatch")
            bullet_text = _require_text(b.get("text"), "experience bullet.text")
            bullet_cids = _require_list(b.get("claim_ids"), "experience bullet.claim_ids")
            if not bullet_text.strip() or not bullet_cids:
                raise ResumeValidationError(ResumeReasonCode.MISSING_CITATION, "experience bullets must be non-empty and cited")

    # Validate skills
    skills_list = _require_list(data.get("skills"), "skills")
    for s in skills_list:
        if not isinstance(s, dict) or set(s.keys()) != {"name", "claim_ids"}:
            raise ResumeValidationError(ResumeReasonCode.EXTRANEOUS_KEYS if isinstance(s, dict) else ResumeReasonCode.MALFORMED_JSON, "skill item keys mismatch")
        skill_name = _require_text(s.get("name"), "skill.name")
        skill_cids = _require_list(s.get("claim_ids"), "skill.claim_ids")
        if not skill_name.strip() or not skill_cids:
            raise ResumeValidationError(ResumeReasonCode.MISSING_CITATION, "skills must be non-empty and cited")

    # Validate education
    edu_list = _require_list(data.get("education"), "education")
    for edu in edu_list:
        if not isinstance(edu, dict) or set(edu.keys()) != {"institution", "degree", "dates", "bullets"}:
            raise ResumeValidationError(ResumeReasonCode.EXTRANEOUS_KEYS if isinstance(edu, dict) else ResumeReasonCode.MALFORMED_JSON, "education item keys mismatch")
        for field_name in ("institution", "degree", "dates"):
            field_value = _require_text(edu.get(field_name), f"education.{field_name}")
            if not field_value.strip():
                raise ResumeValidationError(ResumeReasonCode.MALFORMED_JSON, f"education.{field_name} must be non-empty")
        bullets = _require_list(edu.get("bullets"), "education.bullets")
        if not bullets:
            raise ResumeValidationError(ResumeReasonCode.MISSING_CITATION, "education container requires cited bullets")
        for b in bullets:
            if not isinstance(b, dict) or set(b.keys()) != {"text", "claim_ids"}:
                raise ResumeValidationError(ResumeReasonCode.EXTRANEOUS_KEYS if isinstance(b, dict) else ResumeReasonCode.MALFORMED_JSON, "education bullet keys mismatch")
            bullet_text = _require_text(b.get("text"), "education bullet.text")
            bullet_cids = _require_list(b.get("claim_ids"), "education bullet.claim_ids")
            if not bullet_text.strip() or not bullet_cids:
                raise ResumeValidationError(ResumeReasonCode.MISSING_CITATION, "education bullets must be non-empty and cited")

    omitted_claim_ids = _require_list(data.get("omitted_claim_ids"), "omitted_claim_ids")
    missing_fact_questions = _require_list(data.get("missing_fact_questions"), "missing_fact_questions")
    generation_notes = _require_list(data.get("generation_notes"), "generation_notes")
    for index, cid in enumerate(omitted_claim_ids):
        _require_text(cid, f"omitted_claim_ids[{index}]")
    for index, question in enumerate(missing_fact_questions):
        _require_text(question, f"missing_fact_questions[{index}]")
    for index, note in enumerate(generation_notes):
        _require_text(note, f"generation_notes[{index}]")

    # Safety inspection ONLY on generated resume content fields
    output_parts: list[str] = [
        str(headline.get("text", "")),
        str(summary.get("text", "")),
    ]
    for s in skills_list:
        output_parts.append(str(s.get("name", "")))
    for exp in exp_list:
        output_parts.extend([str(exp.get("organization", "")), str(exp.get("role", "")), str(exp.get("dates", ""))])
        for b in exp.get("bullets", []):
            output_parts.append(str(b.get("text", "")))
    for edu in edu_list:
        output_parts.extend([str(edu.get("institution", "")), str(edu.get("degree", "")), str(edu.get("dates", ""))])
        for b in edu.get("bullets", []):
            output_parts.append(str(b.get("text", "")))

    output_content_str = "\n".join(output_parts)

    for pat in _INJECTION_PATTERNS:
        if re.search(pat, output_content_str, re.IGNORECASE):
            raise ResumeValidationError(ResumeReasonCode.PROMPT_INJECTION_DETECTED, f"Prompt injection copied into output: {pat}")

    for pat in _PROTECTED_CLASS_PATTERNS:
        if re.search(pat, output_content_str, re.IGNORECASE):
            raise ResumeValidationError(ResumeReasonCode.SENSITIVE_INFERENCE_REJECTED, f"Sensitive protected class pattern detected in output: {pat}")

    for pat in _PRIVACY_PATTERNS:
        if re.search(pat, output_content_str, re.IGNORECASE):
            raise ResumeValidationError(ResumeReasonCode.PRIVACY_VIOLATION, f"Privacy violation pattern detected in output: {pat}")

    # Citations & Claims Map
    claims_map = {c.claim_id: c for c in claims}
    for index, claim_id in enumerate(omitted_claim_ids):
        claim = claims_map.get(claim_id)
        if claim is None:
            raise ResumeValidationError(ResumeReasonCode.UNSUPPORTED_CLAIM, f"omitted_claim_ids[{index}] cites unknown claim '{claim_id}'")
        if claim.sensitive:
            raise ResumeValidationError(ResumeReasonCode.SENSITIVE_INFERENCE_REJECTED, f"omitted_claim_ids[{index}] cites sensitive claim '{claim_id}'")

    # Verify citations before applying strict text grounding.
    # Headline and summary are copied claim text, never paraphrased prose.
    _verify_leaf_citations(headline, "Headline", claims_map)
    headline_claims = _claims_for_citations(headline["claim_ids"], "Headline", claims_map)
    _verify_exact_text_grounding(headline["text"], headline_claims, "Headline")
    _verify_leaf_citations(summary, "Summary", claims_map)
    summary_claims = _claims_for_citations(summary["claim_ids"], "Summary", claims_map)
    _verify_exact_text_grounding(summary["text"], summary_claims, "Summary")
    for s in skills_list:
        cids = s["claim_ids"]
        cited_claims = _claims_for_citations(cids, f"Skill '{s['name']}'", claims_map)
        cited_text = " ".join(c.text for c in cited_claims)
        skill_words = set(re.findall(r"\w+", s["name"].lower()))
        cited_words = set(re.findall(r"\w+", cited_text.lower()))
        if not skill_words.issubset(cited_words):
            raise ResumeValidationError(ResumeReasonCode.UNSUPPORTED_CLAIM, f"Skill '{s['name']}' contains unsupported words not in cited claim")

    for exp in exp_list:
        _verify_experience_container(exp, claims_map)

    for edu in edu_list:
        _verify_education_container(edu, claims_map)

    return data
def _claims_for_citations(
    claim_ids: list[Any],
    label: str,
    claims_map: dict[str, CandidateClaim],
) -> list[CandidateClaim]:
    if type(claim_ids) is not list:
        raise ResumeValidationError(ResumeReasonCode.MALFORMED_JSON, f"{label} claim_ids must be a list")
    cited_claims: list[CandidateClaim] = []
    for cid in claim_ids:
        if type(cid) is not str:
            raise ResumeValidationError(ResumeReasonCode.MALFORMED_JSON, f"{label} claim IDs must be strings")
        claim = claims_map.get(cid)
        if claim is None:
            raise ResumeValidationError(ResumeReasonCode.UNSUPPORTED_CLAIM, f"{label} cites unknown claim '{cid}'")
        if claim.sensitive:
            raise ResumeValidationError(ResumeReasonCode.SENSITIVE_INFERENCE_REJECTED, f"{label} cites sensitive claim '{cid}'")
        cited_claims.append(claim)
    return cited_claims

def _verify_leaf_citations(item: dict[str, Any], label: str, claims_map: dict[str, CandidateClaim]) -> None:
    text = item.get("text")
    cids = item.get("claim_ids")
    if type(text) is not str:
        raise ResumeValidationError(ResumeReasonCode.MALFORMED_JSON, f"{label} text must be a string")
    if type(cids) is not list:
        raise ResumeValidationError(ResumeReasonCode.MALFORMED_JSON, f"{label} claim_ids must be a list")
    if text.strip() and not cids:
        raise ResumeValidationError(ResumeReasonCode.MISSING_CITATION, f"{label} is missing claim citations")
    if not text.strip() and cids:
        raise ResumeValidationError(ResumeReasonCode.ALTERED_FACT, f"{label} has citations but no grounded text")
    _claims_for_citations(cids, label, claims_map)


def _verify_exact_text_grounding(
    text: str,
    cited_claims: list[CandidateClaim],
    label: str,
) -> None:
    if not text.strip():
        return
    normalized = normalize_text(text)
    if not any(normalized == claim.text for claim in cited_claims):
        raise ResumeValidationError(
            ResumeReasonCode.ALTERED_FACT,
            f"{label} must exactly match cited claim text",
        )


def _normalized_fact(value: Any) -> str:
    return normalize_text(value).casefold() if isinstance(value, str) else ""


def _require_container_fact(
    value: Any,
    supported_values: set[str],
    label: str,
) -> None:
    normalized = _normalized_fact(value)
    if not normalized or normalized not in supported_values:
        raise ResumeValidationError(ResumeReasonCode.ALTERED_FACT, f"{label} is not supported by cited claims")


def _verify_experience_container(
    exp: dict[str, Any],
    claims_map: dict[str, CandidateClaim],
) -> None:
    bullets = exp["bullets"]
    cited_claims: list[CandidateClaim] = []
    for bullet in bullets:
        bullet_claims = _claims_for_citations(
            bullet["claim_ids"],
            f"Bullet '{bullet['text']}'",
            claims_map,
        )
        cited_claims.extend(bullet_claims)
        _verify_text_grounding(bullet["text"], bullet_claims)
    if not cited_claims:
        raise ResumeValidationError(ResumeReasonCode.MISSING_CITATION, "experience container requires cited bullets")

    source_ids = {
        _normalized_fact(claim.metadata.get("source_entry_id"))
        for claim in cited_claims
        if _normalized_fact(claim.metadata.get("source_entry_id"))
    }
    if not source_ids or _normalized_fact(exp["source_entry_id"]) not in source_ids:
        raise ResumeValidationError(ResumeReasonCode.ALTERED_FACT, "experience source_entry_id is not supported by cited claims")

    organizations = {
        _normalized_fact(claim.organization)
        for claim in cited_claims
        if _normalized_fact(claim.organization)
    }
    roles = {
        _normalized_fact(claim.role)
        for claim in cited_claims
        if _normalized_fact(claim.role)
    }
    dates = {
        _normalized_fact(claim.dates)
        for claim in cited_claims
        if _normalized_fact(claim.dates)
    }
    _require_container_fact(exp["organization"], organizations, "experience organization")
    _require_container_fact(exp["role"], roles, "experience role")
    _require_container_fact(exp["dates"], dates, "experience dates")


def _verify_education_container(
    edu: dict[str, Any],
    claims_map: dict[str, CandidateClaim],
) -> None:
    bullets = edu["bullets"]
    cited_claims: list[CandidateClaim] = []
    for bullet in bullets:
        bullet_claims = _claims_for_citations(
            bullet["claim_ids"],
            f"Education bullet '{bullet['text']}'",
            claims_map,
        )
        cited_claims.extend(bullet_claims)
        _verify_text_grounding(bullet["text"], bullet_claims)
    if not cited_claims:
        raise ResumeValidationError(ResumeReasonCode.MISSING_CITATION, "education container requires cited bullets")

    institutions = {
        _normalized_fact(claim.metadata.get("institution") or claim.organization)
        for claim in cited_claims
        if _normalized_fact(claim.metadata.get("institution") or claim.organization)
    }
    degrees = {
        _normalized_fact(claim.metadata.get("degree") or claim.role)
        for claim in cited_claims
        if _normalized_fact(claim.metadata.get("degree") or claim.role)
    }
    dates = {
        _normalized_fact(claim.dates)
        for claim in cited_claims
        if _normalized_fact(claim.dates)
    }
    _require_container_fact(edu["institution"], institutions, "education institution")
    _require_container_fact(edu["degree"], degrees, "education degree")
    _require_container_fact(edu["dates"], dates, "education dates")

def _verify_text_grounding(text: str, cited_claims: list[CandidateClaim]) -> None:
    if not text or not cited_claims:
        return
    cited_combined = " ".join(c.text for c in cited_claims)

    # Check metrics/numbers
    text_nums = set(re.findall(r"\b\d+(?:\.\d+)?%?\b", text))
    if text_nums:
        cited_nums = set(re.findall(r"\b\d+(?:\.\d+)?%?\b", cited_combined))
        unsupported_nums = text_nums - cited_nums
        if unsupported_nums:
            raise ResumeValidationError(ResumeReasonCode.ALTERED_FACT, f"Metric(s) {unsupported_nums} in '{text}' not in cited claims")

    # Check unsupported words
    text_words = set(w.lower() for w in re.findall(r"\b[A-Za-z]{3,}\b", text))
    stop_words = {"the", "and", "for", "with", "that", "this", "from", "was", "were", "been", "have", "has", "had", "will", "would", "could", "should"}
    text_meaningful = text_words - stop_words
    cited_words = set(w.lower() for w in re.findall(r"\b[A-Za-z]{3,}\b", cited_combined))
    unsupported_words = text_meaningful - cited_words
    if unsupported_words:
        raise ResumeValidationError(ResumeReasonCode.ALTERED_FACT, f"Word(s) {unsupported_words} in '{text}' not supported by cited claim text")


def _rank_claims_for_job(
    claims: Sequence[CandidateClaim],
    job_snapshot: JobResumeSnapshot,
) -> list[CandidateClaim]:
    requirement_tokens: set[str] = set()
    for requirement in job_snapshot.requirements:
        if any(re.search(pattern, requirement, re.IGNORECASE) for pattern in _INJECTION_PATTERNS):
            continue
        requirement_tokens.update(
            token
            for token in re.findall(r"\w+", normalize_text(requirement).casefold())
            if len(token) >= 2
        )

    def rank_key(claim: CandidateClaim) -> tuple[int, str]:
        metadata_text = " ".join(
            str(value)
            for value in claim.metadata.values()
            if isinstance(value, str)
        )
        claim_tokens = set(
            re.findall(r"\w+", f"{claim.text} {metadata_text}".casefold())
        )
        overlap = len(requirement_tokens & claim_tokens)
        return (-overlap, claim.claim_id)

    return sorted(claims, key=rank_key)


def generate_grounded_tailored_resume(
    job_snapshot: JobResumeSnapshot,
    profile: LoadedApplicationProfile | ApplicationProfile | None,
    resume_context: ResumeContext | None,
) -> tuple[dict[str, Any], ResumeScoreData]:
    """Deterministically generate a grounded tailored resume dictionary and scoring data."""
    claims = extract_candidate_claims(profile, resume_context)
    non_sensitive_claims = [c for c in claims if not c.sensitive]
    ranked_claims = _rank_claims_for_job(non_sensitive_claims, job_snapshot)
    selected_claims = ranked_claims[:MAX_LIST_ITEMS]

    profile_obj: ApplicationProfile | None
    profile_source_sha256: str | None
    if isinstance(profile, LoadedApplicationProfile):
        profile_obj = profile.profile
        profile_source_sha256 = profile.source_sha256
    elif isinstance(profile, ApplicationProfile):
        profile_obj = profile
        profile_source_sha256 = None
    else:
        profile_obj = None
        profile_source_sha256 = None

    if profile_source_sha256:
        expected_profile_sha256 = profile_source_sha256
    elif profile_obj is not None and profile_obj.facts:
        expected_profile_sha256 = compute_sha256(canonical_json(profile_obj.facts))
    else:
        expected_profile_sha256 = compute_sha256("")

    if resume_context is not None and getattr(resume_context, "sha256", None):
        expected_source_resume_sha256 = getattr(resume_context, "sha256")
    elif resume_context is not None and getattr(resume_context, "text", None):
        expected_source_resume_sha256 = compute_sha256(getattr(resume_context, "text"))
    else:
        expected_source_resume_sha256 = compute_sha256("")

    # Select sections from exact non-sensitive claims
    skills_entries: list[dict[str, Any]] = []
    exp_map: dict[str, dict[str, Any]] = {}
    edu_map: dict[str, dict[str, Any]] = {}
    headline_claim: CandidateClaim | None = None
    summary_claim: CandidateClaim | None = None

    selected_cids: set[str] = set()

    for c in selected_claims:
        if c.category == "headline" and headline_claim is None:
            headline_claim = c
        elif c.category == "summary" and summary_claim is None:
            summary_claim = c
        elif c.category == "skill":
            skills_entries.append({"name": c.text, "claim_ids": [c.claim_id]})
            selected_cids.add(c.claim_id)
        elif c.category == "experience":
            entry_id = c.metadata.get("source_entry_id")
            org = c.organization
            role = c.role
            dates = c.dates
            if (
                not isinstance(entry_id, str)
                or not entry_id.strip()
                or not isinstance(org, str)
                or not org.strip()
                or not isinstance(role, str)
                or not role.strip()
                or not isinstance(dates, str)
                or not dates.strip()
            ):
                continue
            if entry_id not in exp_map:
                exp_map[entry_id] = {
                    "source_entry_id": entry_id,
                    "organization": org,
                    "role": role,
                    "dates": dates,
                    "bullets": [],
                }
            exp_map[entry_id]["bullets"].append({"text": c.text, "claim_ids": [c.claim_id]})
            selected_cids.add(c.claim_id)
        elif c.category == "education":
            inst = c.metadata.get("institution") or c.organization
            deg = c.metadata.get("degree") or c.role
            dt = c.dates
            if not all(isinstance(value, str) and value.strip() for value in (inst, deg, dt)):
                continue
            edu_key = f"{inst}\x00{deg}\x00{dt}"
            if edu_key not in edu_map:
                edu_map[edu_key] = {
                    "institution": inst,
                    "degree": deg,
                    "dates": dt,
                    "bullets": [],
                }
            edu_map[edu_key]["bullets"].append({"text": c.text, "claim_ids": [c.claim_id]})
            selected_cids.add(c.claim_id)
    # Pick headline/summary if not set by category
    if headline_claim is None:
        for c in selected_claims:
            if c.category in {"summary", "experience", "general", "education", "skill"}:
                headline_claim = c
                break

    if summary_claim is None:
        for c in selected_claims:
            if c != headline_claim and c.category in {"experience", "general", "education", "skill"}:
                summary_claim = c
                break
    if summary_claim is None and selected_claims:
        summary_claim = headline_claim


    headline_dict = {"text": headline_claim.text if headline_claim else "", "claim_ids": [headline_claim.claim_id] if headline_claim else []}
    summary_dict = {"text": summary_claim.text if summary_claim else "", "claim_ids": [summary_claim.claim_id] if summary_claim else []}

    if headline_claim:
        selected_cids.add(headline_claim.claim_id)
    if summary_claim:
        selected_cids.add(summary_claim.claim_id)

    all_claim_ids = set(c.claim_id for c in non_sensitive_claims)
    omitted_cids = sorted(all_claim_ids - selected_cids)

    selected_ordered_cids = [
        claim.claim_id
        for claim in selected_claims
        if claim.claim_id in selected_cids
    ]
    score_data = score_resume_against_job(
        job_snapshot=job_snapshot,
        claims=claims,
        selected_claim_ids=selected_ordered_cids,
    )

    gen_notes = ["Generated resume selecting exact candidate claim text only."]
    questions = list(score_data.missing_fact_questions)

    resume_data = {
        "schema_version": 1,
        "job_snapshot_sha256": job_snapshot.job_snapshot_sha256,
        "profile_sha256": expected_profile_sha256,
        "source_resume_sha256": expected_source_resume_sha256,
        "headline": headline_dict,
        "summary": summary_dict,
        "experience": list(exp_map.values()),
        "skills": skills_entries,
        "education": list(edu_map.values()),
        "omitted_claim_ids": omitted_cids,
        "missing_fact_questions": questions,
        "generation_notes": gen_notes,
    }

    validated_data = validate_tailored_resume_json(
        raw_or_dict=resume_data,
        job_snapshot=job_snapshot,
        claims=claims,
        expected_profile_sha256=expected_profile_sha256,
        expected_source_resume_sha256=expected_source_resume_sha256,
    )

    return validated_data, score_data


def build_generated_resume_artifact(
    resume_id: str,
    job_snapshot: JobResumeSnapshot,
    profile_sha256: str,
    source_resume_sha256: str,
    generation_config: dict[str, Any],
    resume_content_json: str | dict[str, Any],
    pdf_bytes: bytes | None,
    private_pdf_path: str | Path,
    created_at: str | None = None,
) -> GeneratedResumeArtifact:
    """Build a frozen GeneratedResumeArtifact."""
    import datetime

    gen_config_sha256 = compute_sha256(canonical_json(generation_config))
    content_str = canonical_json(resume_content_json) if isinstance(resume_content_json, dict) else resume_content_json
    content_sha256 = compute_sha256(content_str)
    pdf_sha256 = compute_sha256(pdf_bytes) if pdf_bytes is not None else compute_sha256(b"")

    timestamp = created_at or datetime.datetime.now(datetime.timezone.utc).isoformat()

    return GeneratedResumeArtifact(
        resume_id=str(resume_id),
        job_id=int(job_snapshot.job_id),
        job_snapshot_sha256=job_snapshot.job_snapshot_sha256,
        profile_sha256=profile_sha256,
        source_resume_sha256=source_resume_sha256,
        generation_config_sha256=gen_config_sha256,
        content_sha256=content_sha256,
        pdf_sha256=pdf_sha256,
        private_pdf_path=Path(private_pdf_path),
        created_at=timestamp,
    )
