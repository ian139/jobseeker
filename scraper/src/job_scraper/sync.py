from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol

from job_scraper.config import ScraperConfig, build_search_payload
from job_scraper.storage import JobStorage


class SearchClient(Protocol):
    def search_jobs(self, payload: dict[str, object]) -> dict[str, object]: ...


@dataclass(frozen=True)
class SyncSummary:
    pages_fetched: int
    jobs_returned: int
    inserted: int
    updated: int
    skipped: int
    checkpoint_before: str | None
    checkpoint_after: str | None


def sync_once(client: SearchClient, storage: JobStorage, config: ScraperConfig) -> SyncSummary:
    storage.initialize()
    checkpoint_before = storage.get_state("last_successful_discovered_at")
    discovered_at_gte = (
        _subtract_overlap(checkpoint_before, config.search.discovered_overlap_minutes) if checkpoint_before else None
    )

    pages_fetched = 0
    jobs_returned = 0
    inserted = 0
    updated = 0
    skipped = 0
    saved_discovered_at: list[str] = []
    result_set_complete = False

    for page in range(config.search.max_pages):
        payload = build_search_payload(config, page=page, discovered_at_gte=discovered_at_gte)
        response = client.search_jobs(payload)
        if "data" not in response:
            raise ValueError("TheirStack response missing required field 'data'")
        data = response["data"]
        if not isinstance(data, list):
            raise ValueError("TheirStack response field 'data' must be a list")

        pages_fetched += 1
        jobs_returned += len(data)
        if not data:
            result_set_complete = True
            break

        for item in data:
            if not isinstance(item, dict):
                skipped += 1
                continue
            result = storage.upsert_job(item)
            if result.status == "inserted":
                inserted += 1
            elif result.status == "updated":
                updated += 1
            else:
                skipped += 1
            if result.discovered_at and result.status != "skipped":
                saved_discovered_at.append(result.discovered_at)

        if len(data) < config.search.limit:
            result_set_complete = True
            break

    checkpoint_after = _max_checkpoint(checkpoint_before, saved_discovered_at) if result_set_complete else checkpoint_before
    storage.record_run(checkpoint_after if result_set_complete and saved_discovered_at else None)
    return SyncSummary(
        pages_fetched=pages_fetched,
        jobs_returned=jobs_returned,
        inserted=inserted,
        updated=updated,
        skipped=skipped,
        checkpoint_before=checkpoint_before,
        checkpoint_after=checkpoint_after,
    )


def _subtract_overlap(checkpoint: str, minutes: int) -> str | None:
    parsed = _parse_datetime(checkpoint)
    if parsed is None:
        return None
    return (parsed - timedelta(minutes=minutes)).isoformat(timespec="seconds")


def _max_checkpoint(current: str | None, candidates: list[str]) -> str | None:
    best_value = current
    best_datetime = _parse_datetime(current) if current else None
    for candidate in candidates:
        candidate_datetime = _parse_datetime(candidate)
        if candidate_datetime is None:
            continue
        if best_datetime is None or candidate_datetime > best_datetime:
            best_datetime = candidate_datetime
            best_value = candidate
    return best_value


def _parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    normalized = value.replace("Z", "+00:00") if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)
