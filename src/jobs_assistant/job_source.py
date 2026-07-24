from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

import httpx

from .backlog import upsert_jobs
from .contracts import JobInput, SourceJob


def _non_empty_string(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _description_value(raw: dict[str, Any]) -> str | None:
    for key in ("description", "job_description", "description_text", "description_html"):
        value = _non_empty_string(raw.get(key))
        if value is not None:
            return value
    details = raw.get("details")
    value = _non_empty_string(details)
    if value is not None:
        return value
    if isinstance(details, dict):
        for key in ("text", "html", "content", "value"):
            value = _non_empty_string(details.get(key))
            if value is not None:
                return value
    return None


def _location_value(raw: dict[str, Any]) -> str | None:
    for key in ("location", "job_location", "city", "country_code"):
        value = _non_empty_string(raw.get(key))
        if value is not None:
            return value
    return None


def _remote_value(raw: dict[str, Any]) -> bool | None:
    remote = raw.get("remote")
    return remote if isinstance(remote, bool) else None


def _date_posted_value(raw: dict[str, Any]) -> str | None:
    for key in ("date_posted", "posted_at"):
        value = _non_empty_string(raw.get(key))
        if value is not None:
            return value
    return None

def normalize_job_metadata(
    raw: Mapping[str, Any],
) -> tuple[str | None, bool | None, str | None, str | None]:
    """Normalize the metadata shared by source-specific job adapters."""
    if not isinstance(raw, Mapping):
        raise ValueError("job source record must be an object")
    raw_record = raw if isinstance(raw, dict) else dict(raw)
    return (
        _location_value(raw_record),
        _remote_value(raw_record),
        _date_posted_value(raw_record),
        _description_value(raw_record),
    )


def _validated_job_records(raw_jobs: Iterable[Any]) -> list[dict[str, Any]]:
    try:
        records = list(raw_jobs)
    except TypeError as exc:
        raise ValueError("job source records must be iterable") from exc
    validated: list[dict[str, Any]] = []
    for item in records:
        if not isinstance(item, Mapping):
            raise ValueError("job source record must be an object")
        validated.append(item if isinstance(item, dict) else dict(item))
    return validated


def extract_source_jobs(payload: Any) -> list[dict[str, Any]]:
    """Validate a source response envelope and return its job records."""
    if isinstance(payload, list):
        return _validated_job_records(payload)
    if not isinstance(payload, Mapping):
        raise ValueError("job source returned unsupported JSON")
    selected: list[dict[str, Any]] | None = None
    for key in ("jobs", "data"):
        if key not in payload:
            continue
        candidate = payload[key]
        if not isinstance(candidate, list):
            raise ValueError(f"job source response field {key!r} is not a list")
        candidate_records = _validated_job_records(candidate)
        if selected is None:
            selected = candidate_records
    if selected is None:
        raise ValueError("job source response is missing a recognized job-list key")
    return selected


def normalize_source_job(raw: Mapping[str, Any]) -> SourceJob:
    if not isinstance(raw, Mapping):
        raise ValueError("job source record must be an object")
    raw_record = raw if isinstance(raw, dict) else dict(raw)
    external_id = raw_record.get("id") or raw_record.get("external_id") or raw_record.get("source_job_id")
    title = raw_record.get("title") or raw_record.get("job_title")
    company = raw_record.get("company") or raw_record.get("company_name")
    if isinstance(company, dict):
        company = company.get("name")
    location, remote, date_posted, description = normalize_job_metadata(raw_record)
    return SourceJob(
        external_id=None if external_id is None else str(external_id),
        title=str(title or "Untitled role"),
        company=None if company is None else str(company),
        listing_url=None if raw_record.get("listing_url") is None else str(raw_record.get("listing_url")),
        apply_url=None if (raw_record.get("apply_url") or raw_record.get("url")) is None else str(raw_record.get("apply_url") or raw_record.get("url")),
        date_posted=date_posted,
        raw=raw_record,
        location=location,
        remote=remote,
        description=description,
    )


def source_job_to_input(source_job: SourceJob, *, source: str = "job_source") -> JobInput:
    return JobInput(
        source=source,
        source_job_id=source_job.external_id,
        url=source_job.apply_url or source_job.listing_url,
        title=source_job.title,
        company=source_job.company or "Unknown company",
        location=source_job.location,
        remote=source_job.remote,
        description=source_job.description,
        posted_at=source_job.date_posted,
        raw=source_job.raw,
    )


def import_source_jobs(conn: Any, raw_jobs: Iterable[Mapping[str, Any]], *, source: str = "job_source") -> tuple[int, int, int]:
    records = _validated_job_records(raw_jobs)
    inputs = [source_job_to_input(normalize_source_job(raw), source=source) for raw in records]
    inserted, updated = upsert_jobs(conn, inputs)
    return len(inputs), inserted, updated


def fetch_source_jobs(base_url: str, *, api_key: str | None = None, client: httpx.Client | None = None) -> list[dict[str, Any]]:
    url = f"{base_url.rstrip('/')}/v1/jobs"
    headers = {"Accept": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    http = client or httpx.Client(timeout=30)
    response = http.get(url, headers=headers)
    response.raise_for_status()
    return extract_source_jobs(response.json())
