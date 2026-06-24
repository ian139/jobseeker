from __future__ import annotations

import base64
import json
import re
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import urljoin

import httpx

from job_scraper.storage import JobStorage

PUBLIC_JSON_BASE_URL = "https://doomersareretardedcommunists.com/"
PUBLIC_JSON_ID_PREFIX = "publicjson:"
_BOOTSTRAP_RE = re.compile(
    r'<template[^>]+id=["\']media-node-static-bootstrap["\'][^>]*>(?P<payload>.*?)</template>',
    re.DOTALL | re.IGNORECASE,
)


class PublicJsonError(RuntimeError):
    """Raised when the public JSON feed cannot be parsed or fetched."""


class PublicJsonSource(Protocol):
    def fetch_homepage(self) -> str: ...

    def fetch_json(self, path_or_url: str) -> dict[str, Any]: ...


@dataclass(frozen=True)
class PublicJsonPage:
    offset: int
    limit: int
    count: int
    url: str


@dataclass(frozen=True)
class PublicJsonManifest:
    snapshot_date: str
    generated_at: str
    all_pages: tuple[PublicJsonPage, ...]


@dataclass(frozen=True)
class PublicJsonImportSummary:
    snapshot_date: str
    generated_at: str
    pages_fetched: int
    jobs_returned: int
    inserted: int
    updated: int
    skipped: int
    duplicates: int


class PublicJsonClient:
    def __init__(self, base_url: str = PUBLIC_JSON_BASE_URL, timeout_seconds: float = 30.0) -> None:
        self._base_url = base_url
        self._timeout_seconds = timeout_seconds

    def fetch_homepage(self) -> str:
        try:
            response = httpx.get(self._base_url, timeout=self._timeout_seconds, follow_redirects=True)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise PublicJsonError(f"Public JSON homepage request failed: {exc}") from exc
        return response.text

    def fetch_json(self, path_or_url: str) -> dict[str, Any]:
        url = urljoin(self._base_url, path_or_url)
        try:
            response = httpx.get(url, timeout=self._timeout_seconds, follow_redirects=True)
            response.raise_for_status()
            data = response.json()
        except httpx.HTTPError as exc:
            raise PublicJsonError(f"Public JSON request failed for {url}: {exc}") from exc
        except json.JSONDecodeError as exc:
            raise PublicJsonError(f"Public JSON endpoint returned non-JSON for {url}") from exc
        if not isinstance(data, dict):
            raise PublicJsonError(f"Public JSON endpoint returned non-object JSON for {url}")
        return data


def import_public_json(source: PublicJsonSource, storage: JobStorage) -> PublicJsonImportSummary:
    bootstrap = decode_bootstrap(source.fetch_homepage())
    manifest = validate_manifest(bootstrap)
    storage.initialize()

    pages_fetched = 0
    jobs_returned = 0
    inserted = 0
    updated = 0
    skipped = 0
    duplicates = 0
    seen_ids: set[str] = set()
    seen_urls: set[str] = set()

    for page in manifest.all_pages:
        payload = source.fetch_json(page.url)
        page_jobs = payload.get("jobs")
        if not isinstance(page_jobs, list):
            raise PublicJsonError(f"Public JSON page missing jobs list: {page.url}")
        pages_fetched += 1
        jobs_returned += len(page_jobs)
        for raw_job in page_jobs:
            if not isinstance(raw_job, dict):
                skipped += 1
                continue
            mapped = map_public_json_job(raw_job)
            if mapped is None:
                skipped += 1
                continue
            job_id = str(mapped["id"])
            url = mapped.get("url")
            if job_id in seen_ids or (isinstance(url, str) and url in seen_urls):
                duplicates += 1
                skipped += 1
                continue
            seen_ids.add(job_id)
            if isinstance(url, str):
                seen_urls.add(url)
            result = storage.upsert_job(mapped)
            if result.status == "inserted":
                inserted += 1
            elif result.status == "updated":
                updated += 1
            else:
                skipped += 1

    return PublicJsonImportSummary(
        snapshot_date=manifest.snapshot_date,
        generated_at=manifest.generated_at,
        pages_fetched=pages_fetched,
        jobs_returned=jobs_returned,
        inserted=inserted,
        updated=updated,
        skipped=skipped,
        duplicates=duplicates,
    )


def decode_bootstrap(homepage_html: str) -> dict[str, Any]:
    match = _BOOTSTRAP_RE.search(homepage_html)
    if match is None:
        raise PublicJsonError("Public JSON bootstrap template not found")
    payload = match.group("payload").strip()
    try:
        decoded = base64.b64decode(payload, validate=True)
        data = json.loads(decoded)
    except (ValueError, json.JSONDecodeError) as exc:
        raise PublicJsonError("Public JSON bootstrap template is not valid base64 JSON") from exc
    if not isinstance(data, dict):
        raise PublicJsonError("Public JSON bootstrap is not a JSON object")
    return data


def validate_manifest(bootstrap: dict[str, Any]) -> PublicJsonManifest:
    manifest = bootstrap.get("manifest")
    if not isinstance(manifest, dict):
        raise PublicJsonError("Public JSON bootstrap missing manifest object")
    snapshot_date = _required_string(manifest, "snapshot_date")
    generated_at = _required_string(manifest, "generated_at")
    all_pages_value = manifest.get("all_pages")
    if not isinstance(all_pages_value, list) or not all_pages_value:
        raise PublicJsonError("Public JSON manifest missing non-empty all_pages list")

    pages: list[PublicJsonPage] = []
    for index, page_value in enumerate(all_pages_value):
        if not isinstance(page_value, dict):
            raise PublicJsonError(f"Public JSON manifest page {index} is not an object")
        pages.append(
            PublicJsonPage(
                offset=_required_int(page_value, "offset"),
                limit=_required_int(page_value, "limit"),
                count=_required_int(page_value, "count"),
                url=_required_string(page_value, "url"),
            )
        )
    return PublicJsonManifest(snapshot_date=snapshot_date, generated_at=generated_at, all_pages=tuple(pages))


def map_public_json_job(raw_job: dict[str, Any]) -> dict[str, Any] | None:
    public_job_id = raw_job.get("job_id")
    if public_job_id in (None, ""):
        return None

    location = raw_job.get("location") if isinstance(raw_job.get("location"), dict) else {}
    salary = raw_job.get("salary") if isinstance(raw_job.get("salary"), dict) else {}
    discovered_at = _string_or_none(raw_job.get("created_at") or raw_job.get("last_updated"))
    min_salary, max_salary = _annual_salary_usd(salary)

    return {
        "id": f"{PUBLIC_JSON_ID_PREFIX}{public_job_id}",
        "job_title": _string_or_none(raw_job.get("title")),
        "company_name": _string_or_none(raw_job.get("company")),
        "job_country_code": _string_or_none(location.get("country")),
        "remote": _remote_or_none(location.get("remote")),
        "date_posted": _string_or_none(raw_job.get("date_posted") or discovered_at),
        "discovered_at": discovered_at,
        "url": _string_or_none(raw_job.get("link")),
        "source_url": _string_or_none(raw_job.get("link")),
        "final_url": _string_or_none(raw_job.get("link_final_url")),
        "min_annual_salary_usd": min_salary,
        "max_annual_salary_usd": max_salary,
        "source": "public_json",
        "public_json": {
            "snapshot_job_id": _string_or_none(public_job_id),
            "title_group": _string_or_none(raw_job.get("title_group")),
            "ats": _string_or_none(raw_job.get("ats")),
            "created_at": _string_or_none(raw_job.get("created_at")),
            "last_updated": _string_or_none(raw_job.get("last_updated")),
            "link_checked_at": _string_or_none(raw_job.get("link_checked_at")),
            "raw": raw_job,
        },
    }


def _required_string(value: dict[str, Any], key: str) -> str:
    item = value.get(key)
    if not isinstance(item, str) or not item:
        raise PublicJsonError(f"Public JSON manifest missing string field {key!r}")
    return item


def _required_int(value: dict[str, Any], key: str) -> int:
    item = value.get(key)
    if not isinstance(item, int):
        raise PublicJsonError(f"Public JSON manifest missing integer field {key!r}")
    return item


def _string_or_none(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)


def _remote_or_none(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    return None


def _annual_salary_usd(salary: dict[str, Any]) -> tuple[float | None, float | None]:
    currency = str(salary.get("currency") or "").upper()
    period = str(salary.get("period") or "").lower()
    if currency != "USD" or period not in {"year", "yearly", "annual", "annually"}:
        return None, None
    return _cents_to_dollars(salary.get("min_cents")), _cents_to_dollars(salary.get("max_cents"))


def _cents_to_dollars(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value) / 100.0
    except (TypeError, ValueError):
        return None
