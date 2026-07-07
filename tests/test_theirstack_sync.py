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


def test_preview_payload_includes_profile_keys():
    """Profile payloads carry job_title_or, job_description_pattern_or, and posted_at_max_age_days."""
    payload = build_preview_payload("new_grad_cs")
    assert "job_title_or" in payload
    assert "job_description_pattern_or" in payload
    assert payload["posted_at_max_age_days"] == 7


def test_default_profile_omits_role_keys():
    """Default profile does not inject job_title_or or job_description_pattern_or."""
    payload = build_preview_payload("default")
    assert "job_title_or" not in payload
    assert "job_description_pattern_or" not in payload


def test_paid_payload_includes_discovered_at_gte():
    """Paid payload threads discovered_at_gte through when a checkpoint is provided."""
    payload = build_paid_fetch_payload("new_grad_cs", limit=10, discovered_at_gte="2026-01-01T00:00:00")
    assert payload["discovered_at_gte"] == "2026-01-01T00:00:00"
    assert payload["limit"] == 10
    assert payload["blur_company_data"] is False


def test_paid_payload_omits_discovered_at_gte_when_none():
    """Paid payload without a checkpoint does not include discovered_at_gte."""
    payload = build_paid_fetch_payload("new_grad_cs", limit=10)
    assert "discovered_at_gte" not in payload


def test_sync_persists_jobs_in_db():
    """sync_theirstack_response writes every job into the jobs table."""
    conn = connect(":memory:")
    init_db(conn)
    response = {"data": [
        {"id": "a", "title": "SWE", "company_name": "Acme", "url": "https://a.test/1"},
        {"id": "b", "title": "Data Scientist", "company_name": "Beta Corp", "url": "https://b.test/2"},
    ]}
    seen, inserted, updated = sync_theirstack_response(conn, response, paid_fetch_enabled=True)
    assert (seen, inserted, updated) == (2, 2, 0)
    row = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()
    assert row[0] == 2


def test_latest_sync_checkpoint_ignores_failed_runs():
    """latest_sync_checkpoint skips failed runs and returns the stored successful checkpoint."""
    from jobs_assistant.db import latest_sync_checkpoint, record_sync_run, update_sync_run

    conn = connect(":memory:")
    init_db(conn)
    # Newer failed run — must be ignored
    fail_id = record_sync_run(conn, "theirstack", "paid_fetch", started_at="2026-02-01T00:00:00", profile="new_grad_cs")
    update_sync_run(conn, fail_id, success=False, error="timeout")
    # Older successful run — this is the one that should be returned
    ok_id = record_sync_run(conn, "theirstack", "paid_fetch", started_at="2026-01-01T00:00:00", profile="new_grad_cs")
    update_sync_run(conn, ok_id, success=True, finished_at="2026-01-01T00:00:01", checkpoint="2026-01-01T00:00:01")

    checkpoint = latest_sync_checkpoint(conn, source="theirstack", profile="new_grad_cs")
    assert checkpoint == "2026-01-01T00:00:01"
