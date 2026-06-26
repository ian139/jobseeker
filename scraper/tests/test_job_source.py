import httpx

import pytest

from apply_pipeline.job_source import (
    authorization_headers,
    list_source_jobs,
    normalize_source_job,
    source_job_to_theirstack_like_raw,
)
from sync.jobs import import_job_source, initialize_database


class FakeClient:
    def __init__(self, response: httpx.Response) -> None:
        self.response = response
        self.requests: list[dict[str, object]] = []
        self.closed = False

    def get(self, url: str, headers: dict[str, str], params: dict[str, object]) -> httpx.Response:
        self.requests.append({"url": url, "headers": headers, "params": params})
        return self.response

    def close(self) -> None:
        self.closed = True


def test_authorization_headers_require_key() -> None:
    with pytest.raises(ValueError, match="JOB_SOURCE_API_KEY is required"):
        authorization_headers(" ")
    assert authorization_headers("secret") == {"Authorization": "Bearer secret"}


def test_list_source_jobs_calls_public_jobs_endpoint() -> None:
    response = httpx.Response(
        200,
        json={"data": [], "pagination": {"total": 0, "limit": 5, "offset": 0}},
        request=httpx.Request("GET", "https://jobs.example.com/v1/jobs"),
    )
    client = FakeClient(response)
    payload = list_source_jobs(base_url="https://jobs.example.com/", api_key="secret", limit=5, offset=10, query="engineer", client=client)  # type: ignore[arg-type]
    assert payload["data"] == []
    assert client.requests == [
        {
            "url": "https://jobs.example.com/v1/jobs",
            "headers": {"Authorization": "Bearer secret"},
            "params": {"limit": 5, "offset": 10, "q": "engineer"},
        }
    ]


def test_source_job_normalizes_to_backlog_raw() -> None:
    source = normalize_source_job(
        {
            "id": "local-1",
            "external_id": "ext-1",
            "title": "Software Engineer",
            "company": "Acme",
            "listing_url": "https://jobs.example.com/1",
            "apply_url": "https://boards.example.com/apply/1",
            "date_posted": "2026-06-25T00:00:00Z",
        }
    )
    raw = source_job_to_theirstack_like_raw(source)
    assert raw["id"] == "ext-1"
    assert raw["job_title"] == "Software Engineer"
    assert raw["company_name"] == "Acme"
    assert raw["url"] == "https://boards.example.com/apply/1"
    assert raw["date_posted"] == "2026-06-25T00:00:00Z"


def test_import_job_source_normalizes_and_upserts(monkeypatch, tmp_path) -> None:
    def fake_list_source_jobs(**kwargs):
        assert kwargs["base_url"] == "https://jobs.example.com"
        assert kwargs["api_key"] == "secret"
        assert kwargs["limit"] == 2
        return {
            "data": [
                {
                    "external_id": "ext-1",
                    "title": "Software Engineer",
                    "company": "Acme",
                    "apply_url": "https://boards.example.com/apply/1",
                    "date_posted": "2026-06-25T00:00:00Z",
                },
                {"id": "bad"},
            ]
        }

    monkeypatch.setattr("sync.jobs.list_source_jobs", fake_list_source_jobs)
    import sqlite3

    db_path = tmp_path / "jobs.sqlite3"
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    initialize_database(connection)

    result = import_job_source(connection, base_url="https://jobs.example.com", api_key="secret", limit=2)

    assert result == {"returned": 2, "inserted": 1, "updated": 0, "skipped": 1}
    row = connection.execute("SELECT theirstack_job_id, title, company_name, canonical_url FROM jobs").fetchone()
    assert dict(row) == {
        "theirstack_job_id": "ext-1",
        "title": "Software Engineer",
        "company_name": "Acme",
        "canonical_url": "https://boards.example.com/apply/1",
    }
