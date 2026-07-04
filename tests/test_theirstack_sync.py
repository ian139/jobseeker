import pytest

from jobs_assistant.db import connect, init_db
from jobs_assistant.theirstack import (
    PaidFetchDisabledError,
    TheirStackClient,
    build_paid_fetch_payload,
    build_preview_payload,
    is_credit_safe_payload,
    select_one_job_per_company,
    sync_theirstack_response,
)


class FakeHTTP:
    def __init__(self):
        self.payloads = []

    def post(self, url, headers, json, timeout):
        self.payloads.append(json)
        return FakeResponse({"data": []})


class FakeResponse:
    status_code = 200
    text = "{}"
    headers = {}

    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


def test_preview_payload_is_credit_safe():
    payload = build_preview_payload()
    assert is_credit_safe_payload(payload)
    assert payload["blur_company_data"] is True
    assert payload["limit"] == 1


def test_paid_payload_requires_enabled_client():
    client = TheirStackClient("token", enable_paid_fetch=False, client=FakeHTTP())
    with pytest.raises(PaidFetchDisabledError):
        client.search_jobs(build_paid_fetch_payload(limit=10))


def test_one_job_per_company_prefers_priority_role():
    selected = select_one_job_per_company([
        {"id": "1", "title": "Office Manager", "company_name": "Acme", "date_posted": "2026-01-02"},
        {"id": "2", "title": "Software Engineer", "company_name": "Acme", "date_posted": "2026-01-01"},
    ])
    assert [job["id"] for job in selected] == ["2"]


def test_sync_response_requires_paid_enablement_before_persisting():
    conn = connect(":memory:")
    init_db(conn)
    response = {"data": [
        {"id": "1", "title": "Software Engineer", "company_name": "Acme", "url": "https://a.test/apply"},
    ]}
    with pytest.raises(PaidFetchDisabledError):
        sync_theirstack_response(conn, response, paid_fetch_enabled=False)
    seen, inserted, updated = sync_theirstack_response(conn, response, paid_fetch_enabled=True)
    assert (seen, inserted, updated) == (1, 1, 0)
