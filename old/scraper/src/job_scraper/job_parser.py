"""Parser microsystem: source-specific raw job shapes → normalized ParsedJob.

This is the only module that understands job-source-specific raw shapes.
All storage, UI, and downstream code consumes ParsedJob only.
"""

from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass, fields
from typing import Any, Literal

RoleKind = Literal["co_op", "internship"]
"""The only role kinds the scraper persists (co-op or internship)."""


@dataclass(frozen=True)
class ParsedJob:
    """Normalized representation of a scraped job, ready for storage."""

    id: str
    title: str | None
    company: str | None
    company_domain: str | None
    country_code: str | None
    remote: int | None
    date_posted: str | None
    discovered_at: str | None
    url: str | None
    source_url: str | None
    final_url: str | None
    min_annual_salary_usd: float | None
    max_annual_salary_usd: float | None
    role_kind: RoleKind
    source: str | None
    description: str | None
    locations: tuple[str, ...]
    skills: tuple[str, ...]
    seniority: str | None
    employment_statuses: tuple[str, ...]
    digest: dict[str, object]
    raw: dict[str, Any]


_ROLE_COOP_RE = re.compile(r"\b(co[-\s]?op|coop)\b", re.IGNORECASE)
_ROLE_INTERN_RE = re.compile(r"\b(intern|internship)\b", re.IGNORECASE)


def classify_role_title(title: str | None) -> RoleKind | None:
    """Determine whether *title* describes a co-op or internship role.

    Returns ``RoleKind.co_op``, ``RoleKind.internship``, or ``None`` when the
    title is missing / empty / does not match either pattern.  Co-op wins if
    both patterns match.
    """
    if not title:
        return None

    normalized = unicodedata.normalize("NFKD", title.strip())
    normalized = normalized.lower()
    # Map common Unicode dashes/hyphens to ASCII hyphen-minus so the regex matches
    normalized = re.sub(r"[\u2010-\u2015\u2212]", "-", normalized)
    normalized = re.sub(r"\s+", " ", normalized)

    is_coop = bool(_ROLE_COOP_RE.search(normalized))
    is_intern = bool(_ROLE_INTERN_RE.search(normalized))

    if is_coop:
        return "co_op"
    if is_intern:
        return "internship"
    return None


# ---------------------------------------------------------------------------
# TheirStack parser
# ---------------------------------------------------------------------------

_PUBLIC_JSON_ID_PREFIX = "publicjson:"


def parse_theirstack_job(raw_job: Mapping[str, Any]) -> ParsedJob | None:
    """Parse a TheirStack raw job dict into a ``ParsedJob``.

    Returns ``None`` when *id* is missing/empty or the title is not co-op or
    internship.
    """
    job_id = raw_job.get("id")
    if job_id in (None, ""):
        return None

    job_id = str(job_id)
    title = _first_string(raw_job, "job_title", "title")
    role_kind = classify_role_title(title)
    if role_kind is None:
        return None

    company = _company_name(raw_job)
    company_domain = _first_string(raw_job, "company_domain", "domain")
    if company_domain is None:
        _company = raw_job.get("company")
        if isinstance(_company, Mapping):
            company_domain = _first_string(_company, "domain")
    country_code = _first_string(raw_job, "job_country_code", "country_code")
    description = _first_string(raw_job, "job_description", "description")
    seniority = _first_string(raw_job, "job_seniority", "seniority")
    employment_statuses = _string_tuple(raw_job, "employment_statuses")
    skills = _parse_skills(raw_job)
    locations = _parse_locations(raw_job, country_code)
    remote = _remote_value(raw_job.get("remote"))
    date_posted = _first_string(raw_job, "date_posted")
    discovered_at = _first_string(raw_job, "discovered_at")
    url = _first_string(raw_job, "url")
    source_url = _first_string(raw_job, "source_url")
    final_url = _first_string(raw_job, "final_url")
    min_salary = _numeric_or_none(raw_job.get("min_annual_salary_usd"))
    max_salary = _numeric_or_none(raw_job.get("max_annual_salary_usd"))

    digest = _build_digest(
        title=title,
        company=company,
        role_kind=role_kind,
        locations=locations,
        country_code=country_code,
        remote=remote,
        min_salary=min_salary,
        max_salary=max_salary,
        skills=skills,
        description=description,
        application_url=final_url or url or source_url or "",
        source="theirstack",
        posted_at=date_posted,
        discovered_at=discovered_at,
    )

    return ParsedJob(
        id=job_id,
        title=title,
        company=company,
        company_domain=company_domain,
        country_code=country_code,
        remote=remote,
        date_posted=date_posted,
        discovered_at=discovered_at,
        url=url,
        source_url=source_url,
        final_url=final_url,
        min_annual_salary_usd=min_salary,
        max_annual_salary_usd=max_salary,
        role_kind=role_kind,
        source="theirstack",
        description=description,
        locations=locations,
        skills=skills,
        seniority=seniority,
        employment_statuses=employment_statuses,
        digest=digest,
        raw=dict(raw_job),
    )


# ---------------------------------------------------------------------------
# Public JSON parser
# ---------------------------------------------------------------------------


def parse_public_json_job(raw_job: Mapping[str, Any]) -> ParsedJob | None:
    """Parse a public JSON raw job dict into a ``ParsedJob``.

    Returns ``None`` when ``job_id`` is missing/empty or the title is not
    co-op or internship.
    """
    public_job_id = raw_job.get("job_id")
    if public_job_id in (None, ""):
        return None

    title = _first_string(raw_job, "title")
    role_kind = classify_role_title(title)
    if role_kind is None:
        return None

    location = _nested(raw_job, "location") or {}
    salary = _nested(raw_job, "salary") or {}
    discovered_at = _first_string(raw_job, "created_at", "last_updated")
    min_salary, max_salary = _annual_salary_usd(salary)
    country_code = _first_string(location, "country")

    locations: tuple[str, ...] = ()
    if country_code:
        locations = (country_code,)

    raw_remote = location.get("remote")
    remote: int | None = None
    if isinstance(raw_remote, bool):
        remote = 1 if raw_remote else 0

    url = _first_string(raw_job, "link")
    final_url = _first_string(raw_job, "link_final_url")
    date_posted = _first_string(raw_job, "date_posted") or discovered_at

    parsed_id = f"{_PUBLIC_JSON_ID_PREFIX}{public_job_id}"

    digest = _build_digest(
        title=title,
        company=_first_string(raw_job, "company"),
        role_kind=role_kind,
        locations=locations,
        country_code=country_code,
        remote=remote,
        min_salary=min_salary,
        max_salary=max_salary,
        skills=(),
        description=None,
        application_url=final_url or url or "",
        source="public_json",
        posted_at=date_posted,
        discovered_at=discovered_at,
    )

    return ParsedJob(
        id=parsed_id,
        title=title,
        company=_first_string(raw_job, "company"),
        company_domain=None,
        country_code=country_code,
        remote=remote,
        date_posted=date_posted,
        discovered_at=discovered_at,
        url=url,
        source_url=url,
        final_url=final_url,
        min_annual_salary_usd=min_salary,
        max_annual_salary_usd=max_salary,
        role_kind=role_kind,
        source="public_json",
        description=None,
        locations=locations,
        skills=(),
        seniority=None,
        employment_statuses=(),
        digest=digest,
        raw=dict(raw_job),
    )


# ---------------------------------------------------------------------------
# Storage mapping
# ---------------------------------------------------------------------------


def parsed_job_to_storage_mapping(job: ParsedJob) -> dict[str, Any]:
    """Convert a ``ParsedJob`` to a plain dict suitable for storage INSERT.

    Includes all ``ParsedJob`` fields plus a ``raw_json`` key containing the
    serialised raw data for backward-compatible storage.
    """
    result: dict[str, Any] = {}
    for f in fields(job):
        value = getattr(job, f.name)
        if f.name == "raw":
            result["raw_json"] = json.dumps(value, separators=(",", ":"), sort_keys=True)
        else:
            result[f.name] = value
    return result


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

_COOP_RE = re.compile(r"\b(co[-\s]?op|coop)\b", re.IGNORECASE)
_INTERN_RE = re.compile(r"\b(intern|internship)\b", re.IGNORECASE)

_SALARY_ANNUAL_PERIODS = frozenset({"year", "yearly", "annual", "annually"})


def _build_digest(
    *,
    title: str | None,
    company: str | None,
    role_kind: RoleKind,
    locations: tuple[str, ...],
    country_code: str | None,
    remote: int | None,
    min_salary: float | None,
    max_salary: float | None,
    skills: tuple[str, ...],
    description: str | None,
    application_url: str,
    source: str,
    posted_at: str | None,
    discovered_at: str | None,
) -> dict[str, object]:
    """Build the digest dict for a parsed job."""
    location_label: str
    if locations:
        location_label = ", ".join(locations)
    elif country_code:
        location_label = country_code
    else:
        location_label = "Location unknown"

    workplace: str
    if remote == 1:
        workplace = "Remote"
    elif remote == 0:
        workplace = "On-site/Hybrid"
    else:
        workplace = "Workplace unknown"

    salary_label: str
    if min_salary is not None and max_salary is not None:
        salary_label = f"${_fmt_dollar(min_salary)} - ${_fmt_dollar(max_salary)}"
    elif min_salary is not None:
        salary_label = f"From ${_fmt_dollar(min_salary)}"
    elif max_salary is not None:
        salary_label = f"Up to ${_fmt_dollar(max_salary)}"
    else:
        salary_label = "Salary not listed"

    desc_capped: str | None = None
    if description:
        collapsed = re.sub(r"\s+", " ", description).strip()
        if len(collapsed) > 500:
            desc_capped = collapsed[:500].rstrip() + "..."
        else:
            desc_capped = collapsed

    return {
        "title": title or "",
        "company": company or "",
        "role_kind": role_kind,
        "location_label": location_label,
        "workplace": workplace,
        "salary_label": salary_label,
        "skills": list(skills),
        "description": desc_capped or "",
        "application_url": application_url,
        "source": source,
        "posted_at": posted_at or "",
        "discovered_at": discovered_at or "",
    }


def _fmt_dollar(value: float) -> str:
    """Format a dollar amount as a whole-dollar, comma-separated string."""
    integer_part = int(round(value))
    return f"{integer_part:,}"


def _first_string(mapping: Mapping[str, Any], *keys: str) -> str | None:
    """Return the first non-None, non-empty string value found for *keys*."""
    for key in keys:
        value = mapping.get(key)
        if value is not None and value != "":
            return str(value)
    return None


def _nested(mapping: Mapping[str, Any], *keys: str) -> Any:
    """Safely traverse nested dict keys returning the innermost value."""
    current: Any = mapping
    for key in keys:
        if not isinstance(current, Mapping):
            return None
        current = current.get(key)
    return current


def _company_name(raw_job: Mapping[str, Any]) -> str | None:
    """Extract company name, preferring ``company.name`` for mapping values."""
    company = raw_job.get("company")
    if isinstance(company, Mapping):
        name = company.get("name")
        if name is not None and name != "":
            return str(name)
    name = _first_string(raw_job, "company_name", "company")
    if name is not None:
        return name
    return None


def _string_tuple(mapping: Mapping[str, Any], key: str) -> tuple[str, ...]:
    """Extract a list/tuple of strings from *mapping* under *key*."""
    value = mapping.get(key)
    if value is None:
        return ()
    if isinstance(value, (list, tuple)):
        result: list[str] = []
        for item in value:
            if item is not None and item != "":
                result.append(str(item))
        return tuple(result)
    if isinstance(value, str):
        return (value,)
    return ()


def _parse_skills(raw_job: Mapping[str, Any]) -> tuple[str, ...]:
    """Extract skills, handling mapping-items with ``name``/``skill``/``value`` keys."""
    value = raw_job.get("skills")
    if value is None:
        return ()
    if isinstance(value, (list, tuple)):
        items: list[str] = []
        for item in value:
            if item is None:
                continue
            if isinstance(item, Mapping):
                skill = _first_string(item, "name", "skill", "value")
                if skill is not None:
                    items.append(skill)
            else:
                s = str(item).strip()
                if s:
                    items.append(s)
        return tuple(items)
    s_value = str(value).strip()
    if s_value:
        return (s_value,)
    return ()


def _parse_locations(raw_job: Mapping[str, Any], fallback_country: str | None) -> tuple[str, ...]:
    """Extract locations from *raw_job*, falling back to country code."""
    locations = _string_tuple(raw_job, "locations")
    if locations:
        return locations
    location = _string_tuple(raw_job, "location")
    if location:
        return location
    if fallback_country:
        return (fallback_country,)
    return ()


def _remote_value(value: Any) -> int | None:
    """Normalise a remote value to ``1`` (remote) or ``0`` (not remote).

    Mirrors the existing production logic in ``storage._remote_value``, kept
    here so the parser microsystem is self-contained.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return 1 if value else 0
    if isinstance(value, int):
        if value == 1:
            return 1
        if value == 0:
            return 0
        return None
    s = str(value).strip().lower()
    if s in ("true", "yes", "remote"):
        return 1
    if s in ("false", "no", "onsite", "on-site", "hybrid"):
        return 0
    return None


def _numeric_or_none(value: Any) -> float | None:
    """Convert *value* to ``float`` or ``None`` for non-numeric inputs.

    Mirrors the logic in ``storage._number_or_none``.
    """
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _annual_salary_usd(salary: Mapping[str, Any]) -> tuple[float | None, float | None]:
    """Extract annual USD salary amounts from a public-JSON salary mapping.

    Only returns values when ``currency`` is ``USD`` and ``period`` is an
    annual duration.
    """
    currency = str(salary.get("currency") or "").upper()
    period = str(salary.get("period") or "").lower()
    if currency != "USD" or period not in _SALARY_ANNUAL_PERIODS:
        return None, None
    return _cents_to_dollars(salary.get("min_cents")), _cents_to_dollars(salary.get("max_cents"))


def _cents_to_dollars(value: Any) -> float | None:
    """Convert an amount in cents to dollars, returning ``None`` on failure."""
    if value in (None, ""):
        return None
    try:
        return float(value) / 100.0
    except (TypeError, ValueError):
        return None