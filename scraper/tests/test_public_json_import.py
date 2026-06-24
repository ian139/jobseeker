from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any

import pytest
from job_scraper import cli as cli_module

from job_scraper.public_json import (
    PublicJsonError,
    PublicJsonImportSummary,
    decode_bootstrap,
    import_public_json,
    map_public_json_job,
    validate_manifest,
)
from job_scraper.storage import JobStorage
from job_scraper.config import AppSettings


class FakePublicJsonSource:
    def __init__(self, bootstrap: dict[str, Any], pages: dict[str, dict[str, Any]]) -> None:
        payload = base64.b64encode(json.dumps(bootstrap).encode("utf-8")).decode("ascii")
        self.homepage = f'<template id="media-node-static-bootstrap" data-encoding="base64">{payload}</template>'
        self.pages = pages
        self.fetched_pages: list[str] = []

    def fetch_homepage(self) -> str:
        return self.homepage

    def fetch_json(self, path_or_url: str) -> dict[str, Any]:
        self.fetched_pages.append(path_or_url)
        return self.pages[path_or_url]


def test_decode_bootstrap_and_validate_manifest_all_pages() -> None:
    bootstrap = _bootstrap([{"offset": 0, "limit": 5000, "count": 1, "url": "/data/page-00000.json"}])
    html = FakePublicJsonSource(bootstrap, {}).fetch_homepage()

    decoded = decode_bootstrap(html)
    manifest = validate_manifest(decoded)

    assert manifest.snapshot_date == "2026-06-23"
    assert manifest.generated_at == "2026-06-23T08:01:03Z"
    assert len(manifest.all_pages) == 1
    assert manifest.all_pages[0].url == "/data/page-00000.json"


def test_validate_manifest_requires_all_pages() -> None:
    with pytest.raises(PublicJsonError, match="all_pages"):
        validate_manifest({"manifest": {"snapshot_date": "2026-06-23", "generated_at": "2026-06-23T08:01:03Z"}})


def test_map_public_json_job_uses_prefixed_id_and_preserves_raw_row() -> None:
    raw = _public_job(
        "job-1",
        company=None,
        country=None,
        remote=None,
        salary={"currency": "USD", "period": "year", "min_cents": 10000000, "max_cents": 15000000},
    )

    mapped = map_public_json_job(raw)

    assert mapped is not None
    assert mapped["id"] == "publicjson:job-1"
    assert mapped["job_title"] == "Staff Software Engineer"
    assert mapped["company_name"] is None
    assert mapped["job_country_code"] is None
    assert mapped["remote"] is None
    assert mapped["url"] == "https://jobs.example.com/job-1"
    assert mapped["source"] == "public_json"
    assert mapped["min_annual_salary_usd"] == 100000.0
    assert mapped["max_annual_salary_usd"] == 150000.0
    assert mapped["public_json"]["raw"] is raw


def test_map_public_json_job_skips_missing_job_id() -> None:
    raw = _public_job("job-1")
    raw.pop("job_id")

    assert map_public_json_job(raw) is None


def test_import_public_json_fetches_all_pages_and_dedupes_jobs(tmp_path: Path) -> None:
    source = FakePublicJsonSource(
        _bootstrap(
            [
                {"offset": 0, "limit": 2, "count": 2, "url": "/data/page-00000.json"},
                {"offset": 2, "limit": 2, "count": 2, "url": "/data/page-00001.json"},
            ]
        ),
        {
            "/data/page-00000.json": {"jobs": [_public_job("job-1"), _public_job("job-2", remote=False)]},
            "/data/page-00001.json": {
                "jobs": [
                    _public_job("job-1"),
                    _public_job("job-3", link="https://jobs.example.com/job-2"),
                ]
            },
        },
    )
    storage = JobStorage(tmp_path / "jobs.sqlite3")

    summary = import_public_json(source, storage)

    assert source.fetched_pages == ["/data/page-00000.json", "/data/page-00001.json"]
    assert summary.pages_fetched == 2
    assert summary.jobs_returned == 4
    assert summary.inserted == 2
    assert summary.updated == 0
    assert summary.skipped == 2
    assert summary.duplicates == 2
    assert storage.count_jobs() == 2

    first = storage.get_job("publicjson:job-1")
    assert first is not None
    assert first.title == "Staff Software Engineer"
    assert first.company == "Acme"
    assert first.country_code == "US"
    assert first.remote == 1
    assert first.url == "https://jobs.example.com/job-1"
    assert first.raw["public_json"]["raw"]["job_id"] == "job-1"


def test_import_public_json_reuses_storage_upsert_for_repeated_import(tmp_path: Path) -> None:
    source = FakePublicJsonSource(
        _bootstrap([{"offset": 0, "limit": 1, "count": 1, "url": "/data/page-00000.json"}]),
        {"/data/page-00000.json": {"jobs": [_public_job("job-1")]}},
    )
    storage = JobStorage(tmp_path / "jobs.sqlite3")

    first = import_public_json(source, storage)
    second = import_public_json(source, storage)

    assert first.inserted == 1
    assert second.inserted == 0
    assert second.updated == 1
    assert storage.count_jobs() == 1


def test_settings_default_to_public_json_source(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("JOB_SOURCE", raising=False)
    monkeypatch.delenv("PUBLIC_JSON_BASE_URL", raising=False)

    settings = AppSettings(_env_file=None)

    assert settings.job_source == "public-json"
    assert settings.public_json_base_url == "https://doomersareretardedcommunists.com/"


def test_run_once_defaults_to_public_json_without_filters(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("JOB_SCRAPER_DB_PATH", str(tmp_path / "jobs.sqlite3"))
    monkeypatch.delenv("JOB_SOURCE", raising=False)
    monkeypatch.delenv("PUBLIC_JSON_BASE_URL", raising=False)
    captured: dict[str, object] = {}

    class FakePublicJsonClient:
        def __init__(self, *, base_url: str) -> None:
            captured["base_url"] = base_url

    def fake_import_public_json(source: object, storage: JobStorage) -> PublicJsonImportSummary:
        captured["source"] = source
        storage.initialize()
        return PublicJsonImportSummary(
            snapshot_date="2026-06-23",
            generated_at="2026-06-23T08:01:03Z",
            pages_fetched=1,
            jobs_returned=2,
            inserted=2,
            updated=0,
            skipped=0,
            duplicates=0,
        )

    monkeypatch.setattr(cli_module, "PublicJsonClient", FakePublicJsonClient)
    monkeypatch.setattr(cli_module, "import_public_json", fake_import_public_json)

    exit_code = cli_module.main(["run-once"])

    assert exit_code == 0
    assert captured["base_url"] == "https://doomersareretardedcommunists.com/"
    assert "Public JSON import snapshot=2026-06-23" in capsys.readouterr().out


def _bootstrap(all_pages: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "manifest": {
            "snapshot_date": "2026-06-23",
            "generated_at": "2026-06-23T08:01:03Z",
            "all_pages": all_pages,
        }
    }


def _public_job(
    job_id: str,
    *,
    company: str | None = "Acme",
    country: str | None = "US",
    remote: bool | None = True,
    link: str | None = None,
    salary: dict[str, object] | None = None,
) -> dict[str, object]:
    return {
        "job_id": job_id,
        "link": link or f"https://jobs.example.com/{job_id}",
        "title": "Staff Software Engineer",
        "title_group": "software engineer",
        "company": company,
        "location": {"country": country, "remote": remote},
        "salary": salary or {"fallback_used": False},
        "ats": "greenhouse",
        "created_at": "2026-06-23T07:10:00Z",
        "last_updated": "2026-06-23T07:11:00Z",
        "link_checked_at": "2026-06-23T08:00:00Z",
    }
