import httpx
import pytest

from jobs_assistant.db import connect, init_db
from jobs_assistant.theirstack import (
    BAD_DESCRIPTION_PATTERNS,
    BAD_TITLE_MATCHES,
    COOP_DESCRIPTION_PATTERNS,
    COOP_ROLE_TITLES,
    NON_COOP_EARLY_CAREER_PATTERNS,
    NON_COOP_ROLE_TITLES,
    PaidFetchDisabledError,
    TheirStackClient,
    TheirStackError,
    build_paid_fetch_payload,
    build_preview_payload,
    checkpoint_profile_key,
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


class SequenceHTTP:
    def __init__(self, responses):
        self.responses = list(responses)
        self.payloads = []

    def post(self, url, headers, json, timeout):
        self.payloads.append(dict(json))
        return FakeResponse(self.responses.pop(0))


def _job(job_id, *, company="Acme", title="Software Engineer", url="https://jobs.example/apply"):
    return {"id": job_id, "title": title, "company_name": company, "url": url}


@pytest.mark.parametrize("page", [-1, True, 1.5])
def test_paid_payload_rejects_invalid_page(page):
    with pytest.raises(ValueError, match="page"):
        build_paid_fetch_payload(page=page)


def test_paid_payload_accepts_explicit_page_without_changing_preview():
    assert build_paid_fetch_payload(limit=10, page=3)["page"] == 3
    assert build_preview_payload()["page"] == 0


def test_paid_client_aggregates_pages_until_total_results_in_order():
    http = SequenceHTTP(
        [
            {"data": [_job("one"), _job("two")], "metadata": {"total_results": 3}},
            {"data": [_job("three")], "metadata": {"total_results": 3}},
        ]
    )
    client = TheirStackClient("token", enable_paid_fetch=True, client=http)

    response = client.search_jobs(build_paid_fetch_payload(limit=2))

    assert [job["id"] for job in response["data"]] == ["one", "two", "three"]
    assert [payload["page"] for payload in http.payloads] == [0, 1]
    assert response["metadata"]["total_results"] == 3



def test_paid_client_fetches_three_pages_for_large_total():
    http = SequenceHTTP(
        [
            {"data": [_job(f"job-{index}") for index in range(100)], "total_results": 250},
            {"data": [_job(f"job-{index}") for index in range(100, 200)], "total_results": 250},
            {"data": [_job(f"job-{index}") for index in range(200, 250)], "total_results": 250},
        ]
    )
    client = TheirStackClient("token", enable_paid_fetch=True, client=http)

    response = client.search_jobs(build_paid_fetch_payload(limit=100))

    assert len(response["data"]) == 250
    assert [payload["page"] for payload in http.payloads] == [0, 1, 2]


def test_paid_client_fetches_remaining_pages_from_nonzero_start_page():
    http = SequenceHTTP(
        [
            {"data": [_job(f"job-{index}") for index in range(100, 200)], "total_results": 250},
            {"data": [_job(f"job-{index}") for index in range(200, 250)], "total_results": 250},
        ]
    )
    client = TheirStackClient("token", enable_paid_fetch=True, client=http)

    response = client.search_jobs(build_paid_fetch_payload(limit=100, page=1))

    assert len(response["data"]) == 150
    assert [payload["page"] for payload in http.payloads] == [1, 2]

def test_paid_client_enforces_page_safety_cap_without_total_results(monkeypatch):
    http = SequenceHTTP(
        [
            {"data": [_job("one")]},
            {"data": [_job("two")]},
        ]
    )
    monkeypatch.setattr("jobs_assistant.theirstack.MAX_PAID_PAGES", 2)
    client = TheirStackClient("token", enable_paid_fetch=True, client=http)

    with pytest.raises(TheirStackError, match="safety page limit"):
        client.search_jobs(build_paid_fetch_payload(limit=1))

    assert [payload["page"] for payload in http.payloads] == [0, 1]


def test_pinned_sync_filters_all_pages_before_global_company_dedupe():
    conn = connect(":memory:")
    init_db(conn)
    http = SequenceHTTP(
        [
            {
                "data": [
                    _job(
                        "invalid",
                        title="Software Engineer",
                        url="https://arbitrary.example/acme/invalid",
                    )
                ],
                "total_results": 2,
            },
            {
                "data": [
                    _job(
                        "valid",
                        title="Office Manager",
                        url="https://boards.greenhouse.io/acme/jobs/123",
                    )
                ],
                "total_results": 2,
            },
        ]
    )
    client = TheirStackClient("token", enable_paid_fetch=True, client=http)
    response = client.search_jobs(build_paid_fetch_payload(limit=1))

    assert sync_theirstack_response(
        conn,
        response,
        paid_fetch_enabled=True,
        ats_filter="greenhouse",
    ) == (1, 1, 0)
    assert conn.execute("SELECT source_job_id FROM jobs").fetchone()["source_job_id"] == "valid"

def test_paid_client_stops_on_empty_page_and_does_not_persist_partial_results():
    conn = connect(":memory:")
    init_db(conn)
    http = SequenceHTTP(
        [
            {"data": [_job("one")], "total_results": 5},
            {"data": [], "total_results": 5},
        ]
    )
    client = TheirStackClient("token", enable_paid_fetch=True, client=http)

    response = client.search_jobs(build_paid_fetch_payload(limit=1))
    sync_theirstack_response(conn, response, paid_fetch_enabled=True)

    assert [payload["page"] for payload in http.payloads] == [0, 1]
    assert conn.execute("SELECT source_job_id FROM jobs").fetchall()[0]["source_job_id"] == "one"


def test_paid_client_rejects_malformed_later_page_before_returning_any_jobs():
    conn = connect(":memory:")
    init_db(conn)
    http = SequenceHTTP(
        [
            {"data": [_job("one")], "total_results": 2},
            {"data": {}, "total_results": 2},
        ]
    )
    client = TheirStackClient("token", enable_paid_fetch=True, client=http)

    with pytest.raises(TheirStackError):
        response = client.search_jobs(build_paid_fetch_payload(limit=1))
        sync_theirstack_response(conn, response, paid_fetch_enabled=True)

    assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0


def test_preview_payload_is_credit_safe():
    payload = build_preview_payload()
    assert is_credit_safe_payload(payload)
    assert payload["blur_company_data"] is True
    assert payload["limit"] == 1


def test_paid_payload_requires_enabled_client():
    client = TheirStackClient("token", enable_paid_fetch=False, client=FakeHTTP())
    with pytest.raises(PaidFetchDisabledError):
        client.search_jobs(build_paid_fetch_payload(limit=10))


@pytest.mark.parametrize("failure", ["timeout", 429, 500])
def test_paid_client_attempts_each_credit_consuming_request_once(failure, monkeypatch):
    requests = []

    def handler(request):
        requests.append(request)
        if failure == "timeout":
            raise httpx.ReadTimeout("private timeout detail", request=request)
        return httpx.Response(failure, json={"detail": "private response detail"}, request=request)

    monkeypatch.setattr(TheirStackClient, "_backoff", staticmethod(lambda *args, **kwargs: None))
    transport = httpx.MockTransport(handler)
    client = httpx.Client(transport=transport)
    try:
        paid = TheirStackClient("token", enable_paid_fetch=True, client=client)
        with pytest.raises(TheirStackError):
            paid.search_jobs(build_paid_fetch_payload(limit=1))
    finally:
        client.close()
    assert len(requests) == 1


def test_preview_client_retains_bounded_retries(monkeypatch):
    requests = []
    responses = [
        httpx.Response(429, json={"detail": "busy"}),
        httpx.Response(200, json={"total_results": 3}),
    ]

    def handler(request):
        requests.append(request)
        response = responses.pop(0)
        response.request = request
        return response

    monkeypatch.setattr(TheirStackClient, "_backoff", staticmethod(lambda *args, **kwargs: None))
    transport = httpx.MockTransport(handler)
    client = httpx.Client(transport=transport)
    try:
        preview = TheirStackClient("token", enable_paid_fetch=False, client=client)
        assert preview.search_jobs(build_preview_payload()) == {"total_results": 3}
    finally:
        client.close()
    assert len(requests) == 2


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

def test_sync_rejects_malformed_secondary_envelope_key_before_upsert():
    conn = connect(":memory:")
    init_db(conn)
    response = {
        "data": [
            {"id": "valid", "title": "Engineer", "company_name": "Acme", "url": "https://a.test/apply"},
        ],
        "jobs": {},
    }

    with pytest.raises(TheirStackError):
        sync_theirstack_response(conn, response, paid_fetch_enabled=True)
    assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0


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


@pytest.mark.parametrize(
    ("ats_filter", "expected_domains"),
    [
        ("greenhouse", ["greenhouse.io", "grnh.se"]),
        ("lever", ["lever.co"]),
    ],
)
def test_paid_payload_adds_only_official_pinned_ats_domains(ats_filter, expected_domains):
    baseline = build_paid_fetch_payload("new_grad_cs", limit=17, ats_filter="auto")
    pinned = build_paid_fetch_payload("new_grad_cs", limit=17, ats_filter=ats_filter)

    assert pinned["url_domain_or"] == expected_domains
    assert {key: value for key, value in pinned.items() if key != "url_domain_or"} == baseline
    assert pinned["page"] == 0
    assert pinned["limit"] == 17
    assert pinned["job_title_or"] == baseline["job_title_or"]
    assert pinned["job_description_pattern_or"] == baseline["job_description_pattern_or"]


def test_paid_payload_auto_and_preview_omit_ats_domain_and_pinned_lists_are_independent():
    auto = build_paid_fetch_payload(ats_filter="auto")
    greenhouse = build_paid_fetch_payload(ats_filter="greenhouse")
    greenhouse_again = build_paid_fetch_payload(ats_filter="greenhouse")
    lever = build_paid_fetch_payload(ats_filter="lever")

    assert "url_domain_or" not in auto
    assert "url_domain_or" not in build_preview_payload()
    assert greenhouse["url_domain_or"] == ["greenhouse.io", "grnh.se"]
    assert lever["url_domain_or"] == ["lever.co"]
    assert greenhouse["url_domain_or"] is not greenhouse_again["url_domain_or"]
    greenhouse["url_domain_or"].append("mutated.example")
    assert greenhouse_again["url_domain_or"] == ["greenhouse.io", "grnh.se"]


def test_paid_payload_validates_ats_filter_name():
    with pytest.raises(ValueError, match="unsupported ATS filter"):
        build_paid_fetch_payload(ats_filter="other")

@pytest.mark.parametrize(
    ("builder", "kwargs", "expected_limit", "expected_blur"),
    [
        (build_preview_payload, {}, 1, True),
        (build_paid_fetch_payload, {"limit": 25}, 25, False),
    ],
)
def test_coop_payload_uses_distinct_profile_filters_and_base_invariants(
    builder, kwargs, expected_limit, expected_blur
):
    new_grad_payload = builder("new_grad_cs", **kwargs)
    coop_payload = builder("fall_coop_swe_data", **kwargs)

    assert coop_payload != new_grad_payload
    profile_keys = {"job_title_or", "job_description_pattern_or"}
    assert {key: value for key, value in coop_payload.items() if key not in profile_keys} == {
        key: value for key, value in new_grad_payload.items() if key not in profile_keys
    }
    assert coop_payload["job_title_or"] == COOP_ROLE_TITLES
    assert all("co-op" in title.lower() for title in coop_payload["job_title_or"])
    assert coop_payload["job_description_pattern_or"] == COOP_DESCRIPTION_PATTERNS
    assert coop_payload["job_description_pattern_or"] == ["(?i)\\bco-op\\b"]
    assert coop_payload["page"] == 0
    assert coop_payload["limit"] == expected_limit
    assert coop_payload["blur_company_data"] is expected_blur
    assert coop_payload["include_total_results"] is True
    assert coop_payload["posted_at_max_age_days"] == 7
    assert coop_payload["job_title_not"] == BAD_TITLE_MATCHES
    assert coop_payload["job_description_pattern_not"] == BAD_DESCRIPTION_PATTERNS



@pytest.mark.parametrize(
    ("builder", "kwargs", "expected_limit", "expected_blur"),
    [
        (build_preview_payload, {}, 1, True),
        (build_paid_fetch_payload, {"limit": 25}, 25, False),
    ],
)
def test_non_coop_payload_is_disjoint_from_coop_and_preserves_base(
    builder, kwargs, expected_limit, expected_blur
):
    new_grad_payload = builder("new_grad_cs", **kwargs)
    non_coop_payload = builder("new_grad_non_coop_cs", **kwargs)
    coop_payload = builder("fall_coop_swe_data", **kwargs)
    coop_regex = COOP_DESCRIPTION_PATTERNS[0]

    assert coop_payload["job_description_pattern_or"] == [coop_regex]
    assert non_coop_payload["job_description_pattern_not"] == [
        *BAD_DESCRIPTION_PATTERNS,
        coop_regex,
    ]
    assert coop_regex in non_coop_payload["job_description_pattern_not"]
    assert coop_regex not in non_coop_payload["job_description_pattern_or"]
    assert non_coop_payload["job_title_or"] == NON_COOP_ROLE_TITLES
    assert non_coop_payload["job_description_pattern_or"] == NON_COOP_EARLY_CAREER_PATTERNS
    assert all("co-op" not in title.lower() for title in non_coop_payload["job_title_or"])
    assert all("co-op" not in pattern.lower() for pattern in non_coop_payload["job_description_pattern_or"])

    assert non_coop_payload != new_grad_payload
    assert non_coop_payload != coop_payload
    profile_keys = {"job_title_or", "job_description_pattern_or", "job_description_pattern_not"}
    assert {key: value for key, value in non_coop_payload.items() if key not in profile_keys} == {
        key: value for key, value in new_grad_payload.items() if key not in profile_keys
    }
    assert non_coop_payload["page"] == 0
    assert non_coop_payload["limit"] == expected_limit
    assert non_coop_payload["blur_company_data"] is expected_blur
    assert non_coop_payload["include_total_results"] is True
    assert non_coop_payload["posted_at_max_age_days"] == 7
    assert non_coop_payload["job_title_not"] == BAD_TITLE_MATCHES
    assert is_credit_safe_payload(non_coop_payload) is expected_blur


def test_profile_payload_mutation_does_not_change_later_builds():
    payload = build_paid_fetch_payload("fall_coop_swe_data", limit=25)
    payload["job_title_or"].clear()
    payload["job_description_pattern_or"].clear()

    rebuilt = build_paid_fetch_payload("fall_coop_swe_data", limit=25)

    assert rebuilt["job_title_or"] == COOP_ROLE_TITLES
    assert rebuilt["job_description_pattern_or"] == COOP_DESCRIPTION_PATTERNS

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



def test_sync_external_id_alias_without_url_updates_same_temporary_db_row(tmp_path):
    conn = connect(tmp_path / "theirstack.sqlite3")
    init_db(conn)
    first = {
        "external_id": "external-1",
        "title": "Software Engineer",
        "company_name": "Acme",
    }
    second = {
        **first,
        "title": "Senior Software Engineer",
    }

    assert sync_theirstack_response(conn, {"data": [first]}, paid_fetch_enabled=True) == (1, 1, 0)
    assert sync_theirstack_response(conn, {"data": [second]}, paid_fetch_enabled=True) == (1, 0, 1)

    row = conn.execute("SELECT source_job_id, canonical_url, title FROM jobs").fetchone()
    assert (row["source_job_id"], row["canonical_url"], row["title"]) == (
        "external-1",
        None,
        "Senior Software Engineer",
    )


def test_sync_rejects_record_without_id_or_url_transactionally(tmp_path):
    conn = connect(tmp_path / "theirstack-invalid.sqlite3")
    init_db(conn)
    response = {
        "data": [
            {
                "external_id": "valid",
                "title": "Software Engineer",
                "company_name": "Acme",
            },
            {
                "title": "Malformed",
                "company_name": "Beta",
            },
        ]
    }

    with pytest.raises(ValueError, match="source_job_id or url"):
        sync_theirstack_response(conn, response, paid_fetch_enabled=True, one_per_company=False)

    assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0


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


@pytest.mark.parametrize(
    ("ats_filter", "url"),
    [
        ("greenhouse", "https://boards.greenhouse.io/acme/jobs/123"),
        ("greenhouse", "https://boards.greenhouse.io/embed/job_app?for=acme&token=123"),
        ("greenhouse", "https://grnh.se/acme"),
        ("lever", "https://jobs.lever.co/acme/123e4567-e89b-12d3-a456-426614174000"),
    ],
)
def test_pinned_ats_accepts_canonical_routes(ats_filter, url):
    conn = connect(":memory:")
    init_db(conn)
    stats = {}
    seen, inserted, updated = sync_theirstack_response(
        conn,
        {"data": [{"id": "accepted", "title": "Engineer", "company_name": "Acme", "url": url}]},
        paid_fetch_enabled=True,
        ats_filter=ats_filter,
        stats=stats,
    )
    assert (seen, inserted, updated) == (1, 1, 0)
    assert stats == {
        "fetched": 1,
        "ats_eligible": 1,
        "ats_rejected": 0,
        "seen": 1,
        "inserted": 1,
        "updated": 0,
    }


@pytest.mark.parametrize(
    ("ats_filter", "source_url"),
    [
        ("greenhouse", "https://boards.greenhouse.io/acme/jobs/456"),
        ("lever", "https://jobs.lever.co/acme/223e4567-e89b-12d3-a456-426614174000"),
    ],
)
def test_pinned_sync_selects_first_canonical_url_candidate(ats_filter, source_url):
    conn = connect(":memory:")
    init_db(conn)
    raw = {
        "id": "mixed",
        "title": "Engineer",
        "company_name": "Acme",
        "apply_url": "https://jobs.example.com/acme/mixed",
        "url": "https://jobs.example.com/acme/listing",
        "source_url": source_url,
    }

    assert sync_theirstack_response(
        conn,
        {"data": [raw]},
        paid_fetch_enabled=True,
        ats_filter=ats_filter,
    ) == (1, 1, 0)
    assert conn.execute("SELECT canonical_url FROM jobs").fetchone()["canonical_url"] == source_url


def test_auto_sync_preserves_historical_url_precedence_for_mixed_candidates():
    conn = connect(":memory:")
    init_db(conn)
    raw = {
        "id": "mixed-auto",
        "title": "Engineer",
        "company_name": "Acme",
        "apply_url": "https://jobs.example.com/acme/mixed-auto",
        "source_url": "https://boards.greenhouse.io/acme/jobs/789",
    }

    assert sync_theirstack_response(conn, {"data": [raw]}, paid_fetch_enabled=True, ats_filter="auto") == (1, 1, 0)
    assert conn.execute("SELECT canonical_url FROM jobs").fetchone()["canonical_url"] == raw["apply_url"]


@pytest.mark.parametrize(
    "url",
    [
        "https://boards.greenhouse.io.evil.example/acme/jobs/123",
        "https://boards.greenhouse.io/acme/jobs/not-a-number",
        "https://boards.greenhouse.io/acme/jobs/123#fragment",
        "",
        None,
    ],
)
def test_pinned_ats_rejects_spoofed_malformed_and_missing_urls(url):
    conn = connect(":memory:")
    init_db(conn)
    stats = {}
    assert sync_theirstack_response(
        conn,
        {"data": [{"id": "rejected", "title": "Engineer", "company_name": "Acme", "url": url}]},
        paid_fetch_enabled=True,
        ats_filter="greenhouse",
        stats=stats,
    ) == (0, 0, 0)
    assert stats["fetched"] == 1
    assert stats["ats_eligible"] == 0
    assert stats["ats_rejected"] == 1
    assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0


def test_auto_ats_preserves_legacy_unfiltered_ingestion():
    conn = connect(":memory:")
    init_db(conn)
    stats = {}
    assert sync_theirstack_response(
        conn,
        {"data": [{"id": "legacy", "title": "Engineer", "company_name": "Acme", "url": "https://arbitrary.example/apply"}]},
        paid_fetch_enabled=True,
        ats_filter="auto",
        stats=stats,
    ) == (1, 1, 0)
    assert stats["ats_rejected"] == 0
    assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 1


def test_pinned_filter_runs_before_any_upsert():
    conn = connect(":memory:")
    init_db(conn)
    response = {
        "data": [
            {"id": "valid", "title": "Engineer", "company_name": "Acme", "url": "https://boards.greenhouse.io/acme/jobs/123"},
            {"id": "invalid", "title": "Engineer", "company_name": "Evil", "url": "https://evil.example/apply"},
        ]
    }
    stats = {}
    assert sync_theirstack_response(conn, response, paid_fetch_enabled=True, ats_filter="greenhouse", stats=stats) == (1, 1, 0)
    assert stats["fetched"] == 2
    assert stats["ats_eligible"] == 1
    assert stats["ats_rejected"] == 1
    assert [row["source_job_id"] for row in conn.execute("SELECT source_job_id FROM jobs")] == ["valid"]


def test_pinned_filter_still_requires_paid_enablement():
    conn = connect(":memory:")
    init_db(conn)
    with pytest.raises(PaidFetchDisabledError):
        sync_theirstack_response(
            conn,
            {"data": [{"id": "valid", "title": "Engineer", "company_name": "Acme", "url": "https://boards.greenhouse.io/acme/jobs/123"}]},
            paid_fetch_enabled=False,
            ats_filter="greenhouse",
        )
    assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0


def test_checkpoint_profile_key_preserves_auto_and_separates_pinned_filters():
    legacy = checkpoint_profile_key("new_grad_cs", "auto")
    greenhouse = checkpoint_profile_key("new_grad_cs", "greenhouse")
    lever = checkpoint_profile_key("new_grad_cs", "lever")

    assert legacy == "new_grad_cs"
    assert greenhouse != lever
    assert greenhouse != legacy
    assert lever != legacy
    assert len(greenhouse) <= 64
    assert len(lever) <= 64


def test_pinned_syncs_refetch_without_checkpoint_and_keep_ats_namespaces(monkeypatch):
    from jobs_assistant.cli import run_theirstack_paid_sync
    from jobs_assistant.db import record_sync_run, update_sync_run

    class RecordingClient:
        def __init__(self, responses):
            self.responses = list(responses)
            self.payloads = []

        def search_jobs(self, payload):
            self.payloads.append(payload)
            return self.responses.pop(0)

    conn = connect(":memory:")
    init_db(conn)
    legacy_run = record_sync_run(
        conn,
        "theirstack",
        "paid_fetch",
        profile="new_grad_cs",
        started_at="2026-01-01T00:00:00",
    )
    update_sync_run(
        conn,
        legacy_run,
        success=True,
        finished_at="2026-01-01T00:00:01",
        checkpoint="2026-01-01T00:00:01",
    )
    client = RecordingClient(
        [
            {"data": [{"id": "auto", "title": "Engineer", "company_name": "Auto", "url": "https://arbitrary.example/auto"}]},
            {
                "data": [
                    {"id": "gh", "title": "Engineer", "company_name": "Greenhouse", "url": "https://boards.greenhouse.io/acme/jobs/123"},
                    {"id": "lv", "title": "Engineer", "company_name": "Lever", "url": "https://jobs.lever.co/acme/123e4567-e89b-12d3-a456-426614174000"},
                ]
            },
            {"data": [{"id": "lv-2", "title": "Engineer", "company_name": "Lever 2", "url": "https://jobs.lever.co/acme/223e4567-e89b-12d3-a456-426614174000"}]},
            {"data": [{"id": "lv-3", "title": "Engineer", "company_name": "Lever 3", "url": "https://jobs.lever.co/acme/323e4567-e89b-12d3-a456-426614174000"}]},
        ]
    )
    monkeypatch.setattr("jobs_assistant.cli._theirstack_client", lambda *, paid_fetch: client)

    run_theirstack_paid_sync(
        conn,
        source_profile="new_grad_cs",
        limit=10,
        mode="paid_fetch",
        ats_filter="auto",
    )
    assert client.payloads[0]["discovered_at_gte"] == "2026-01-01T00:00:01"

    run_theirstack_paid_sync(
        conn,
        source_profile="new_grad_cs",
        limit=10,
        mode="paid_fetch",
        ats_filter="greenhouse",
    )
    assert "discovered_at_gte" not in client.payloads[1]

    run_theirstack_paid_sync(
        conn,
        source_profile="new_grad_cs",
        limit=10,
        mode="paid_fetch",
        ats_filter="lever",
    )
    assert "discovered_at_gte" not in client.payloads[2]
    run_theirstack_paid_sync(
        conn,
        source_profile="new_grad_cs",
        limit=10,
        mode="paid_fetch",
        ats_filter="lever",
    )
    assert "discovered_at_gte" not in client.payloads[3]
    assert client.payloads[2] == client.payloads[3]
    pinned_rows = conn.execute(
        "SELECT profile, checkpoint FROM sync_runs WHERE profile IN (?, ?) AND success=1 ORDER BY id",
        (
            checkpoint_profile_key("new_grad_cs", "greenhouse"),
            checkpoint_profile_key("new_grad_cs", "lever"),
        ),
    ).fetchall()
    assert [row["checkpoint"] for row in pinned_rows] == [None, None, None]
    profiles = {
        row["profile"]
        for row in conn.execute("SELECT profile FROM sync_runs WHERE source='theirstack'")
    }
    assert profiles >= {
        "new_grad_cs",
        checkpoint_profile_key("new_grad_cs", "greenhouse"),
        checkpoint_profile_key("new_grad_cs", "lever"),
    }
