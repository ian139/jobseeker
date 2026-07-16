from __future__ import annotations

from typing import Any, Iterable

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


def normalize_source_job(raw: dict[str, Any]) -> SourceJob:
    external_id = raw.get("id") or raw.get("external_id") or raw.get("source_job_id")
    title = raw.get("title") or raw.get("job_title")
    company = raw.get("company") or raw.get("company_name")
    if isinstance(company, dict):
        company = company.get("name")
    return SourceJob(
        external_id=None if external_id is None else str(external_id),
        title=str(title or "Untitled role"),
        company=None if company is None else str(company),
        listing_url=None if raw.get("listing_url") is None else str(raw.get("listing_url")),
        apply_url=None if (raw.get("apply_url") or raw.get("url")) is None else str(raw.get("apply_url") or raw.get("url")),
        date_posted=None if (raw.get("date_posted") or raw.get("posted_at")) is None else str(raw.get("date_posted") or raw.get("posted_at")),
        raw=raw,
        location=_location_value(raw),
        remote=_remote_value(raw),
        description=_description_value(raw),
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


def import_source_jobs(conn: Any, raw_jobs: Iterable[dict[str, Any]], *, source: str = "job_source") -> tuple[int, int, int]:
    inputs = [source_job_to_input(normalize_source_job(raw), source=source) for raw in raw_jobs]
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
    payload = response.json()
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        items = payload.get("jobs") or payload.get("data") or []
        if isinstance(items, list):
            return [item for item in items if isinstance(item, dict)]
    raise ValueError("job source returned unsupported JSON")
