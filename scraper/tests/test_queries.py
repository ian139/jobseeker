import pytest

from theirstack.queries import PROFILE_NAMES, build_paid_fetch_payload, build_preview_payload, validate_search_payload
from theirstack.client import is_credit_safe_payload


def test_only_fall_coop_profile_is_available() -> None:
    assert PROFILE_NAMES == ("fall_coop_swe_data",)


def test_preview_payloads_are_credit_safe_and_filtered() -> None:
    for profile in PROFILE_NAMES:
        payload = build_preview_payload(profile)
        assert is_credit_safe_payload(payload)
        assert payload["page"] == 0
        assert payload["posted_at_max_age_days"] > 0
        assert payload["is_closed"] is False
        assert payload["company_type"] == "direct_employer"
        assert payload["job_country_code_or"] == ["US"]
        assert "senior" in payload["job_title_not"]
        assert "recruiter" in payload["job_title_not"]
        assert payload["job_title_or"]
        assert payload["job_description_pattern_or"]
        assert "job_seniority_or" not in payload
        assert "employment_statuses_or" not in payload
        validate_search_payload(payload)
        assert payload["order_by"] == [
            {"field": "date_posted", "desc": True},
            {"field": "discovered_at", "desc": True},
        ]


def test_payload_validation_rejects_invalid_seniority() -> None:
    payload = build_preview_payload("fall_coop_swe_data")
    payload["job_seniority_or"] = ["entry"]
    with pytest.raises(ValueError, match="Invalid job_seniority_or: entry"):
        validate_search_payload(payload)


def test_payload_validation_rejects_unknown_field() -> None:
    payload = build_preview_payload("fall_coop_swe_data")
    payload["posted_at_desc"] = True
    with pytest.raises(ValueError, match="Unsupported TheirStack field: posted_at_desc"):
        validate_search_payload(payload)


def test_fall_coop_profile_is_not_remote_only() -> None:
    payload = build_preview_payload("fall_coop_swe_data")
    assert "job_seniority_or" not in payload
    assert "employment_statuses_or" not in payload
    assert payload["posted_at_max_age_days"] == 7
    assert "(?i)\\bco-op\\b" in payload["job_description_pattern_or"]
    assert "(?i)\\b(fall|spring|winter)\\s+(co-op|intern(ship)?)\\b" in payload["job_description_pattern_or"]
    assert "(?i)\\b(fall|spring|winter)\\s+2026\\b" in payload["job_description_pattern_or"]
    assert "(?i)\\bnew grad(uate)?s?\\b" in payload["job_description_pattern_or"]
    assert "(?i)\\bearly career\\b" in payload["job_description_pattern_or"]
    assert "spring software engineer intern" in payload["job_title_or"]
    assert "winter data scientist intern" in payload["job_title_or"]
    assert "new grad software engineer" in payload["job_title_or"]
    assert "software engineer" in payload["job_title_or"]
    assert "(?i)\\bintern(ship)?\\b" not in payload["job_description_pattern_or"]
    assert "(?i)\\b2026\\b" not in payload["job_description_pattern_or"]


def test_paid_fetch_payload_disables_preview_and_supports_checkpoint() -> None:
    payload = build_paid_fetch_payload(
        "fall_coop_swe_data",
        page=2,
        limit=50,
        discovered_at_gte="2026-06-01T00:00:00+00:00",
    )
    assert payload["blur_company_data"] is False
    assert payload["include_total_results"] is False
    assert payload["limit"] == 50
    assert payload["page"] == 2
    assert payload["discovered_at_gte"] == "2026-06-01T00:00:00+00:00"
    assert "job_seniority_or" not in payload
    assert "employment_statuses_or" not in payload
    validate_search_payload(payload)


def test_paid_fetch_payload_supports_posted_age_override() -> None:
    payload = build_paid_fetch_payload("fall_coop_swe_data", page=0, limit=6, posted_at_max_age_days=2)
    assert payload["posted_at_max_age_days"] == 2
