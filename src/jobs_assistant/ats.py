from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlsplit

from .application import AutofillPlan, FieldAnswer, ObservedField, PageObservation, _field_is_sensitive


@dataclass(frozen=True)
class ApplicationContext:
    resume_text: str
    resume_file: Path | None
    application_profile: dict[str, Any]


@dataclass(frozen=True)
class ATSClassification:
    name: str | None
    confidence: float
    reason: str


class ATSAdapter(Protocol):
    name: str

    def matches(self, url: str, html: str) -> bool: ...

    def deterministic_answers(self, observation: PageObservation, context: ApplicationContext) -> tuple[FieldAnswer, ...]: ...


def find_resume_file(resume_dir: str | Path) -> Path | None:
    root = Path(resume_dir)
    if not root.exists():
        return None
    for path in root.iterdir():
        if path.is_file() and path.name.lower().startswith("main_resume.") and path.suffix.lower() in {".pdf", ".doc", ".docx"}:
            return path
    return None


def load_application_profile(profile_json: str | Path | None, resume_dir: str | Path) -> dict[str, Any]:
    import json

    candidates: list[Path] = []
    if profile_json:
        candidates.append(Path(profile_json))
    candidates.append(Path(resume_dir) / "profile.json")
    for path in candidates:
        if path.exists() and path.is_file():
            value = json.loads(path.read_text())
            if not isinstance(value, dict):
                raise ValueError(f"profile JSON must contain an object: {path}")
            return value
    return {}


def greenhouse_value_for_field(field: ObservedField, profile: dict[str, Any]) -> str | None:
    key = " ".join(part for part in [field.name, field.label] if part).lower().replace("-", "_")
    if "first" in key and "name" in key:
        return _profile_string(profile, "first_name")
    if "last" in key and "name" in key:
        return _profile_string(profile, "last_name")
    if "full" in key and "name" in key:
        return _profile_string(profile, "full_name")
    if "email" in key:
        return _profile_string(profile, "email")
    if "phone" in key or "tel" in key:
        return _profile_string(profile, "phone")
    if "linkedin" in key:
        return _profile_string(profile, "linkedin")
    if "website" in key or "portfolio" in key or "personal_site" in key:
        return _profile_string(profile, "website", "portfolio", "personal_site")
    return None


def unresolved_required_fields(observation: PageObservation, answers: tuple[FieldAnswer, ...]) -> tuple[str, ...]:
    answered_selectors = {answer.selector for answer in answers if answer.value}
    return tuple(
        field.label
        for field in observation.fields
        if field.required and field.selector not in answered_selectors and not _field_is_sensitive(field)
    )


def _profile_string(profile: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = profile.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


class GreenhouseAdapter:
    name = "greenhouse"

    def matches(self, url: str, html: str) -> bool:
        host = urlsplit(url).netloc.lower()
        lowered = html.lower()
        return "greenhouse.io" in host or "greenhouse" in host or "grnh.se" in host or "greenhouse" in lowered or "data-source=\"greenhouse\"" in lowered

    def deterministic_answers(self, observation: PageObservation, context: ApplicationContext) -> tuple[FieldAnswer, ...]:
        answers: list[FieldAnswer] = []
        for field in observation.fields:
            if _field_is_sensitive(field):
                continue
            if field.kind == "file" and _is_resume_field(field) and context.resume_file is not None:
                answers.append(FieldAnswer(field.selector, str(context.resume_file), 1.0, "configured resume upload"))
                continue
            value = greenhouse_value_for_field(field, context.application_profile)
            if value:
                answers.append(FieldAnswer(field.selector, value, 1.0, "profile field"))
        return tuple(answers)


def _is_resume_field(field: ObservedField) -> bool:
    key = " ".join(part for part in [field.name, field.label] if part).lower()
    return "resume" in key or "cv" in key


ADAPTERS: tuple[ATSAdapter, ...] = (GreenhouseAdapter(),)


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
    host = urlsplit(url).netloc.lower()
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


def merge_plans(adapter_answers: tuple[FieldAnswer, ...], llm_plan: AutofillPlan, observation: PageObservation | None = None) -> AutofillPlan:
    by_selector = {answer.selector: answer for answer in adapter_answers}
    for answer in llm_plan.answers:
        by_selector.setdefault(answer.selector, answer)
    if llm_plan.raw.get("blocking_sensitive_fields"):
        raw = dict(llm_plan.raw)
        raw["deterministic_answer_count"] = len(adapter_answers)
        return AutofillPlan(
            answers=(),
            safe_button_selector=None,
            status=llm_plan.status,
            reason=llm_plan.reason,
            skipped_fields=llm_plan.skipped_fields,
            raw=raw,
        )
    unresolved = unresolved_required_fields(observation, tuple(by_selector.values())) if observation is not None else ()
    status = "ready" if by_selector else llm_plan.status
    reason = llm_plan.reason if llm_plan.status != "ready" and not by_selector else "adapter/LLM answers ready"
    raw = dict(llm_plan.raw)
    raw["deterministic_answer_count"] = len(adapter_answers)
    if unresolved:
        raw["unresolved_required_fields"] = list(unresolved)
        status = "manual"
        reason = "required safe fields unresolved"
    return AutofillPlan(
        answers=tuple(by_selector.values()),
        safe_button_selector=llm_plan.safe_button_selector,
        status=status,
        reason=reason,
        skipped_fields=llm_plan.skipped_fields,
        raw=raw,
    )
