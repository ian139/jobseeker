from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Protocol, Sequence

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

_TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9+.#-]{1,}")
_KEEP_SHORT = {"c++", "c#", "go", "js", "ai", "ml"}
_NORMALIZED_FIELDS = ("title", "company", "company_domain", "country_code", "url", "source_url", "final_url")
_RAW_FIELDS = (
    "job_title",
    "title",
    "job_description",
    "description",
    "company_name",
    "company",
    "company_description",
    "job_seniority",
    "employment_statuses",
    "remote",
    "skills",
    "responsibilities",
    "requirements",
    "benefits",
)


class ContactInfo(BaseModel):
    model_config = ConfigDict(extra="ignore")

    email: str | None = None
    phone: str | None = None
    location: str | None = None
    links: list[str] = Field(default_factory=list)


class ResumeBullet(BaseModel):
    model_config = ConfigDict(extra="ignore")

    text: str
    tags: list[str] = Field(default_factory=list)
    industries: list[str] = Field(default_factory=list)
    skills: list[str] = Field(default_factory=list)


class ResumeItem(BaseModel):
    model_config = ConfigDict(extra="ignore")

    title: str
    organization: str
    location: str | None = None
    dates: str
    bullets: list[ResumeBullet]


class ResumeSection(BaseModel):
    model_config = ConfigDict(extra="ignore")

    heading: str
    items: list[ResumeItem]


class ResumeProfile(BaseModel):
    model_config = ConfigDict(extra="ignore")

    name: str
    headline: str | None = None
    contact: ContactInfo = Field(default_factory=ContactInfo)
    summary_points: list[ResumeBullet] = Field(default_factory=list)
    skills: dict[str, list[str]] = Field(default_factory=dict)
    sections: list[ResumeSection]


@dataclass(frozen=True)
class SelectedBullet:
    section: str
    item_title: str
    text: str
    score: int
    matched_terms: tuple[str, ...]


class ResumeLLM(Protocol):
    @property
    def model_name(self) -> str | None: ...

    def rewrite(
        self,
        *,
        draft_markdown: str,
        job: Mapping[str, Any],
        selected_bullets: Sequence[SelectedBullet],
    ) -> str: ...


@dataclass(frozen=True)
class TailoredResume:
    markdown: str
    selected_bullets: list[SelectedBullet]
    keywords: tuple[str, ...]
    llm_used: bool
    model: str | None


def load_resume_profile(path: Path) -> ResumeProfile:
    try:
        with path.open("r", encoding="utf-8") as handle:
            data = yaml.safe_load(handle)
    except OSError as exc:
        raise ValueError("Resume profile has no sections with bullets") from exc

    try:
        profile = ResumeProfile.model_validate(data or {})
    except ValidationError as exc:
        raise ValueError("Resume profile has no sections with bullets") from exc

    if not any(item.bullets for section in profile.sections for item in section.items):
        raise ValueError("Resume profile has no sections with bullets")
    return profile


def extract_job_keywords(job: Mapping[str, Any]) -> tuple[str, ...]:
    text = _job_search_text(job)
    seen: set[str] = set()
    keywords: list[str] = []
    for token in _TOKEN_RE.findall(text):
        if len(token) < 3 and token not in _KEEP_SHORT:
            continue
        if token not in seen:
            seen.add(token)
            keywords.append(token)
    return tuple(keywords)


def tailor_resume(
    profile: ResumeProfile,
    job: Mapping[str, Any],
    *,
    llm: ResumeLLM | None = None,
    max_bullets_per_item: int = 4,
    max_summary_points: int = 3,
) -> TailoredResume:
    keywords = extract_job_keywords(job)
    job_text = _job_search_text(job)
    selected: list[SelectedBullet] = []

    if profile.summary_points and max_summary_points > 0:
        selected.extend(
            _select_bullets(
                profile.summary_points,
                section="Summary",
                item_title="",
                item_context=profile.headline or profile.name,
                keywords=keywords,
                job_text=job_text,
                limit=max_summary_points,
                include_zero_fallback=True,
            )
        )

    if max_bullets_per_item > 0:
        for section in profile.sections:
            for item in section.items:
                selected.extend(
                    _select_bullets(
                        item.bullets,
                        section=section.heading,
                        item_title=item.title,
                        item_context=f"{item.title} {item.organization}",
                        keywords=keywords,
                        job_text=job_text,
                        limit=max_bullets_per_item,
                        include_zero_fallback=True,
                    )
                )

    markdown = render_resume_markdown(profile, selected, keywords)
    if llm is None:
        return TailoredResume(markdown=markdown, selected_bullets=selected, keywords=keywords, llm_used=False, model=None)

    rewritten = llm.rewrite(draft_markdown=markdown, job=job, selected_bullets=selected)
    return TailoredResume(
        markdown=rewritten,
        selected_bullets=selected,
        keywords=keywords,
        llm_used=True,
        model=llm.model_name,
    )


def render_resume_markdown(profile: ResumeProfile, selected: Sequence[SelectedBullet], keywords: Sequence[str]) -> str:
    del keywords
    selected_remaining = list(selected)

    lines: list[str] = [f"# {profile.name}"]
    if profile.headline:
        lines.extend(["", profile.headline])

    contact_parts = [profile.contact.email, profile.contact.phone, profile.contact.location, *profile.contact.links]
    contact_line = " | ".join(part for part in contact_parts if part)
    if contact_line:
        lines.extend(["", contact_line])

    summary = [bullet for bullet in selected_remaining if bullet.section == "Summary" and bullet.item_title == ""]
    if summary:
        lines.extend(["", "## Summary"])
        lines.extend(f"- {bullet.text}" for bullet in summary)

    if profile.skills:
        lines.extend(["", "## Skills"])
        for group, skills in profile.skills.items():
            if skills:
                lines.append(f"- **{group}:** {', '.join(skills)}")

    for section in profile.sections:
        lines.extend(["", f"## {section.heading}"])
        for item in section.items:
            item_bullets = _consume_item_bullets(selected_remaining, section.heading, item)
            if not item_bullets:
                continue
            heading = f"### {item.title} — {item.organization}"
            if item.location:
                heading = f"{heading}, {item.location}"
            lines.extend(["", heading, f"*{item.dates}*"])
            lines.extend(f"- {bullet.text}" for bullet in item_bullets)

    return "\n".join(lines).rstrip() + "\n"


def _consume_item_bullets(
    selected: list[SelectedBullet], section_heading: str, item: ResumeItem
) -> list[SelectedBullet]:
    available: dict[str, int] = {}
    for bullet in item.bullets:
        available[bullet.text] = available.get(bullet.text, 0) + 1

    consumed_indexes: list[int] = []
    item_bullets: list[SelectedBullet] = []
    for index, bullet in enumerate(selected):
        if bullet.section != section_heading or bullet.item_title != item.title:
            continue
        if available.get(bullet.text, 0) <= 0:
            continue
        available[bullet.text] -= 1
        consumed_indexes.append(index)
        item_bullets.append(bullet)

    for index in reversed(consumed_indexes):
        selected.pop(index)
    return item_bullets


def _select_bullets(
    bullets: Sequence[ResumeBullet],
    *,
    section: str,
    item_title: str,
    item_context: str,
    keywords: Sequence[str],
    job_text: str,
    limit: int,
    include_zero_fallback: bool,
) -> list[SelectedBullet]:
    scored = [
        _score_bullet(
            bullet,
            section=section,
            item_title=item_title,
            item_context=item_context,
            keywords=keywords,
            job_text=job_text,
            order=index,
        )
        for index, bullet in enumerate(bullets)
    ]
    positives = [entry for entry in scored if entry[0].score > 0]
    candidates = positives
    if not candidates and include_zero_fallback and scored:
        candidates = [scored[0]]
    candidates = sorted(candidates, key=lambda entry: (-entry[0].score, entry[1]))
    return [entry[0] for entry in candidates[:limit]]


def _score_bullet(
    bullet: ResumeBullet,
    *,
    section: str,
    item_title: str,
    item_context: str,
    keywords: Sequence[str],
    job_text: str,
    order: int,
) -> tuple[SelectedBullet, int]:
    score = 0
    matched: list[str] = []
    keyword_set = set(keywords)
    for value in [*bullet.skills, *bullet.industries, *bullet.tags]:
        term = _normalize_text(value).strip()
        if not term:
            continue
        if term in keyword_set or _contains_phrase(job_text, term):
            score += 5
            matched.append(term)

    bullet_text = _normalize_text(bullet.text)
    item_text = _normalize_text(item_context)
    for keyword in keywords:
        if _contains_phrase(bullet_text, keyword):
            score += 2
            matched.append(keyword)
        if _contains_phrase(item_text, keyword):
            score += 1
            matched.append(keyword)

    return SelectedBullet(section, item_title, bullet.text, score, tuple(dict.fromkeys(matched))), order


def _job_search_text(job: Mapping[str, Any]) -> str:
    values: list[Any] = []
    for field in _NORMALIZED_FIELDS:
        if field in job:
            values.append(job[field])

    raw_json = job.get("raw_json")
    if isinstance(raw_json, str):
        values.append(raw_json)
        try:
            raw = json.loads(raw_json)
        except json.JSONDecodeError:
            raw = None
        if isinstance(raw, Mapping):
            values.extend(raw.get(field) for field in _RAW_FIELDS if field in raw)
    elif raw_json is not None:
        values.append(raw_json)

    raw_mapping = job.get("raw")
    if isinstance(raw_mapping, Mapping):
        values.extend(raw_mapping.get(field) for field in _RAW_FIELDS if field in raw_mapping)

    values.extend(job.get(field) for field in _RAW_FIELDS if field in job)
    return _normalize_text(" ".join(_flatten_text(value) for value in values if value is not None))


def _flatten_text(value: Any) -> str:
    if isinstance(value, Mapping):
        return " ".join(f"{key} {_flatten_text(nested)}" for key, nested in value.items())
    if isinstance(value, (list, tuple, set)):
        return " ".join(_flatten_text(item) for item in value)
    return str(value)


def _normalize_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii")
    return normalized.lower()


def _contains_phrase(text: str, phrase: str) -> bool:
    if not phrase:
        return False
    if re.fullmatch(r"[a-z0-9+.#-]+", phrase):
        return phrase in set(_TOKEN_RE.findall(text))
    return phrase in text
