from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

import httpx


@dataclass(frozen=True)
class SourceJob:
    external_id: str | None
    title: str
    company: str | None
    listing_url: str | None
    apply_url: str | None
    date_posted: str | None
    raw: dict[str, Any]


def authorization_headers(api_key: str) -> dict[str, str]:
    if not api_key.strip():
        raise ValueError("JOB_SOURCE_API_KEY is required")
    return {"Authorization": f"Bearer {api_key.strip()}"}


def list_source_jobs(
    *,
    base_url: str,
    api_key: str,
    limit: int = 100,
    offset: int = 0,
    lane: str | None = None,
    query: str | None = None,
    client: httpx.Client | None = None,
) -> dict[str, Any]:
    if limit < 1 or limit > 1000:
        raise ValueError("limit must be between 1 and 1000")
    params: dict[str, str | int] = {"limit": limit, "offset": offset}
    if lane:
        params["lane"] = lane
    if query:
        params["q"] = query
    close_client = client is None
    http_client = client or httpx.Client(timeout=30)
    try:
        response = http_client.get(
            f"{base_url.rstrip('/')}/v1/jobs",
            headers=authorization_headers(api_key),
            params=params,
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
            raise ValueError("Job source response must include a data array")
        return payload
    finally:
        if close_client:
            http_client.close()


def normalize_source_job(raw: dict[str, Any]) -> SourceJob:
    title = string_value(raw, "title")
    if not title:
        raise ValueError("source job must include title")
    return SourceJob(
        external_id=string_value(raw, "external_id", "id"),
        title=title,
        company=string_value(raw, "company"),
        listing_url=string_value(raw, "listing_url"),
        apply_url=string_value(raw, "apply_url"),
        date_posted=string_value(raw, "date_posted"),
        raw=raw,
    )


def source_job_to_theirstack_like_raw(job: SourceJob) -> dict[str, Any]:
    url = job.apply_url or job.listing_url
    return {
        "id": job.external_id,
        "job_title": job.title,
        "company_name": job.company,
        "url": url,
        "date_posted": job.date_posted,
        "source": "job_source_service",
        "raw_source_job": job.raw,
    }


def string_value(raw: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = raw.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def host_or_none(value: str | None) -> str | None:
    if not value:
        return None
    parsed = urlparse(value)
    return parsed.netloc.lower() or None
