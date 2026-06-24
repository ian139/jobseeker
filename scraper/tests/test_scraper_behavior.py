from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from job_scraper.cli import preview_count
from job_scraper.config import has_company_identifier_filters, load_config
from job_scraper.storage import JobStorage
from job_scraper.sync import sync_once


class FakeTheirStackClient:
    def __init__(self, responses: list[dict[str, object]]) -> None:
        self.responses = responses
        self.payloads: list[dict[str, object]] = []

    def search_jobs(self, payload: dict[str, object]) -> dict[str, object]:
        self.payloads.append(payload)
        index = min(len(self.payloads) - 1, len(self.responses) - 1)
        return self.responses[index]


def test_first_run_sends_freshness_source_limit_page_and_saves_jobs(tmp_path: Path) -> None:
    config = _example_config()
    storage = JobStorage(tmp_path / "jobs.sqlite3")
    client = FakeTheirStackClient(
        [
            {
                "data": [
                    _job("job-1", discovered_at="2026-06-23T12:00:00+00:00"),
                ]
            }
        ]
    )

    summary = sync_once(client, storage, config)

    payload = client.payloads[0]
    assert payload["posted_at_max_age_days"] == 1
    assert payload["url_domain_or"] == [
        "linkedin.com",
        "myworkdayjobs.com",
        "rippling.com",
        "oraclecloud.com",
        "taleo.net",
        "greenhouse.io",
        "ashbyhq.com",
        "smartrecruiters.com",
    ]
    assert payload["limit"] == 25
    assert payload["page"] == 0
    assert "remote" not in payload
    assert summary.inserted == 1
    assert storage.count_jobs() == 1


def test_second_run_uses_checkpoint_overlap_and_dedupes_existing_job(tmp_path: Path) -> None:
    config = _example_config()
    storage = JobStorage(tmp_path / "jobs.sqlite3")
    first_client = FakeTheirStackClient(
        [{"data": [_job("job-1", discovered_at="2026-06-23T12:00:00+00:00")]}]
    )
    sync_once(first_client, storage, config)

    second_client = FakeTheirStackClient(
        [{"data": [_job("job-1", discovered_at="2026-06-23T12:00:00+00:00")]}]
    )
    summary = sync_once(second_client, storage, config)

    assert second_client.payloads[0]["discovered_at_gte"] == "2026-06-23T11:50:00+00:00"
    assert summary.inserted == 0
    assert summary.updated == 1
    assert storage.count_jobs() == 1


def test_pagination_stops_after_max_pages_even_when_pages_are_full(tmp_path: Path) -> None:
    config = _example_config()
    config.search.limit = 2
    config.search.max_pages = 3
    storage = JobStorage(tmp_path / "jobs.sqlite3")
    client = FakeTheirStackClient(
        [
            {"data": [_job("job-1"), _job("job-2")]},
            {"data": [_job("job-3"), _job("job-4")]},
            {"data": [_job("job-5"), _job("job-6")]},
            {"data": [_job("job-7"), _job("job-8")]},
        ]
    )

    summary = sync_once(client, storage, config)

    assert len(client.payloads) == 3
    assert [payload["page"] for payload in client.payloads] == [0, 1, 2]
    assert summary.pages_fetched == 3
    assert summary.jobs_returned == 6
    assert storage.count_jobs() == 6


def test_preview_count_uses_blur_count_payload_and_does_not_write_jobs(tmp_path: Path) -> None:
    config = _example_config()
    storage = JobStorage(tmp_path / "jobs.sqlite3")
    storage.initialize()
    client = FakeTheirStackClient([{"metadata": {"total_results": 42}, "data": []}])

    response = preview_count(client, config)

    payload = client.payloads[0]
    assert payload["blur_company_data"] is True
    assert payload["include_total_results"] is True
    assert payload["limit"] == 1
    assert payload["page"] == 0
    assert response["metadata"] == {"total_results": 42}
    assert storage.count_jobs() == 0


def test_page_cap_does_not_advance_checkpoint_when_results_are_truncated(tmp_path: Path) -> None:
    config = _example_config()
    config.search.limit = 2
    config.search.max_pages = 1
    storage = JobStorage(tmp_path / "jobs.sqlite3")
    client = FakeTheirStackClient(
        [
            {
                "data": [
                    _job("job-1", discovered_at="2026-06-23T12:00:00+00:00"),
                    _job("job-2", discovered_at="2026-06-23T11:00:00+00:00"),
                ]
            }
        ]
    )

    summary = sync_once(client, storage, config)

    assert summary.checkpoint_after is None
    assert storage.get_state("last_successful_discovered_at") is None
    assert storage.count_jobs() == 2


def test_missing_data_response_fails_without_recording_success(tmp_path: Path) -> None:
    config = _example_config()
    storage = JobStorage(tmp_path / "jobs.sqlite3")
    client = FakeTheirStackClient([{}])

    with pytest.raises(ValueError, match="missing required field 'data'"):
        sync_once(client, storage, config)

    assert storage.get_state("last_run_at") is None


def test_malformed_discovered_at_does_not_poison_checkpoint(tmp_path: Path) -> None:
    config = _example_config()
    storage = JobStorage(tmp_path / "jobs.sqlite3")
    client = FakeTheirStackClient([{"data": [_job("job-1", discovered_at="not-a-date")]}])

    summary = sync_once(client, storage, config)

    assert summary.checkpoint_after is None
    assert storage.get_state("last_successful_discovered_at") is None

    storage.set_state("last_successful_discovered_at", "not-a-date")
    next_client = FakeTheirStackClient([{"data": []}])
    sync_once(next_client, storage, config)

    assert "discovered_at_gte" not in next_client.payloads[0]


def test_preview_guard_blocks_extra_company_identifier_filters(tmp_path: Path) -> None:
    config_path = tmp_path / "filters.yaml"
    config_path.write_text(
        """
search: {}
filters:
  company_id_or:
    - "company-1"
""",
        encoding="utf-8",
    )
    config = load_config(config_path)

    assert has_company_identifier_filters(config)


def _example_config() -> Any:
    return load_config(Path(__file__).parents[1] / "config" / "filters.example.yaml")


def _job(job_id: str, *, discovered_at: str = "2026-06-23T12:00:00+00:00") -> dict[str, object]:
    return {
        "id": job_id,
        "job_title": "Fall Software Co-op",
        "company_name": "Acme",
        "company_domain": "acme.example",
        "job_country_code": "US",
        "remote": False,
        "date_posted": "2026-06-23",
        "discovered_at": discovered_at,
        "url": "https://www.linkedin.com/jobs/view/123",
        "source_url": "https://www.linkedin.com/jobs/view/123",
        "final_url": "https://acme.example/jobs/123",
    }
