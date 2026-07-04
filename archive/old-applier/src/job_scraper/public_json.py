from __future__ import annotations

import base64
import json
import re
import time
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import urljoin

import httpx
from job_scraper.job_parser import parse_public_json_job

from job_scraper.storage import JobStorage

PUBLIC_JSON_BASE_URL = "https://doomersareretardedcommunists.com/"
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
    def __init__(
        self,
        base_url: str = PUBLIC_JSON_BASE_URL,
        timeout_seconds: float = 90.0,
        max_attempts: int = 3,
        retry_backoff_seconds: float = 1.0,
    ) -> None:
        self._base_url = base_url
        self._timeout_seconds = timeout_seconds
        self._max_attempts = max(1, max_attempts)
        self._retry_backoff_seconds = max(0.0, retry_backoff_seconds)
        self._client = httpx.Client(timeout=self._timeout_seconds, follow_redirects=True)

    def fetch_homepage(self) -> str:
        url = self._base_url
        response = self._get(url, context="Public JSON homepage request failed")
        return response.text

    def fetch_json(self, path_or_url: str) -> dict[str, Any]:
        url = urljoin(self._base_url, path_or_url)
        response = self._get(url, context=f"Public JSON request failed for {url}")
        try:
            data = response.json()
        except json.JSONDecodeError as exc:
            raise PublicJsonError(f"Public JSON endpoint returned non-JSON for {url}") from exc
        if not isinstance(data, dict):
            raise PublicJsonError(f"Public JSON endpoint returned non-object JSON for {url}")
        return data

    def _get(self, url: str, *, context: str) -> httpx.Response:
        last_error: httpx.HTTPError | None = None
        for attempt in range(1, self._max_attempts + 1):
            try:
                response = self._client.get(url)
                response.raise_for_status()
                return response
            except httpx.HTTPError as exc:
                last_error = exc
                if attempt >= self._max_attempts or not _is_retryable_http_error(exc):
                    raise PublicJsonError(f"{context}: {exc}") from exc
                time.sleep(self._retry_backoff_seconds * attempt)
        raise PublicJsonError(f"{context}: {last_error}")


def _is_retryable_http_error(exc: httpx.HTTPError) -> bool:
    if isinstance(exc, (httpx.TimeoutException, httpx.NetworkError)):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in {408, 425, 429, 500, 502, 503, 504}
    return False


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
            parsed = parse_public_json_job(raw_job)
            if parsed is None:
                skipped += 1
                continue
            if parsed.id in seen_ids or (parsed.url is not None and parsed.url in seen_urls):
                duplicates += 1
                skipped += 1
                continue
            seen_ids.add(parsed.id)
            if parsed.url is not None:
                seen_urls.add(parsed.url)
            result = storage.upsert_job(parsed)
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


