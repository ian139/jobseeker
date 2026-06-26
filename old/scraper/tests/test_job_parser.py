"""Tests for the parser microsystem (job_parser.py).

Covers:
- TheirStack co-op job → title, company mapping, remote int, skills tuple, digest
- TheirStack internship job with company mapping → company name/domain, role_kind
- TheirStack non-matching title → None
- Public JSON internship job → prefixed id, salary, raw preservation, source, digest
- Public JSON non-intern job → None
"""

from __future__ import annotations

import json

import pytest

from job_scraper.job_parser import (
    ParsedJob,
    classify_role_title,
    parse_public_json_job,
    parse_theirstack_job,
    parsed_job_to_storage_mapping,
)


class TestClassifyRoleTitle:
    def test_coop_matches(self) -> None:
        assert classify_role_title("Software Engineer Co-op 2025") == "co_op"
        assert classify_role_title("Co-Op Data Analyst") == "co_op"
        assert classify_role_title("ML Coop Intern") == "co_op"  # co-op wins over intern

    def test_intern_matches(self) -> None:
        assert classify_role_title("Data Science Intern") == "internship"
        assert classify_role_title("intern, swe") == "internship"

    def test_none_for_other_roles(self) -> None:
        assert classify_role_title("Senior Software Engineer") is None
        assert classify_role_title("Full Stack Developer") is None

    def test_none_for_empty_or_missing(self) -> None:
        assert classify_role_title("") is None
        assert classify_role_title(None) is None

    def test_unicode_normalization(self) -> None:
        # NFKD will decompose the Œ ligature; ensure the regex still works
        # with decomposed forms
        assert classify_role_title("Co‑op Developer") is not None  # non-breaking hyphen


class TestParseTheirstackJob:
    def test_coop_parses_all_fields(self) -> None:
        raw = {
            "id": "abc123",
            "job_title": "Software Engineer Co-op 2025",
            "company": {"name": "Tech Corp", "domain": "techcorp.com"},
            "job_country_code": "US",
            "remote": True,
            "date_posted": "2025-06-01",
            "discovered_at": "2025-06-02T12:00:00",
            "url": "https://example.com/job/1",
            "source_url": "https://theirstack.com/abc123",
            "final_url": "https://techcorp.com/careers/coop",
            "min_annual_salary_usd": 50000.0,
            "max_annual_salary_usd": 75000.0,
            "job_description": "Great co-op opportunity for students.",
            "skills": ["Python", "SQL", "AWS"],
            "locations": ["San Francisco, CA"],
            "employment_statuses": ["full-time"],
            "job_seniority": "entry-level",
        }
        job = parse_theirstack_job(raw)
        assert job is not None
        assert isinstance(job, ParsedJob)
        assert job.id == "abc123"
        assert job.title == "Software Engineer Co-op 2025"
        assert job.company == "Tech Corp"
        assert job.company_domain == "techcorp.com"
        assert job.country_code == "US"
        assert job.remote == 1  # True → 1
        assert job.role_kind == "co_op"
        assert job.source == "theirstack"
        assert job.min_annual_salary_usd == 50000.0
        assert job.max_annual_salary_usd == 75000.0
        assert job.skills == ("Python", "SQL", "AWS")
        assert job.locations == ("San Francisco, CA",)
        assert job.employment_statuses == ("full-time",)
        assert job.seniority == "entry-level"
        assert job.digest["role_kind"] == "co_op"
        assert job.digest["workplace"] == "Remote"
        assert "Salary not listed" not in str(job.digest["salary_label"])

    def test_internship_with_company_mapping(self) -> None:
        raw = {
            "id": "intern456",
            "title": "Data Science Intern",
            "company": {"name": "DataCo", "domain": "dataco.io"},
            "job_country_code": "CA",
            "remote": False,
            "skills": [{"name": "Python"}, {"skill": "R"}, {"value": "SQL"}],
            "location": "Toronto, ON",
        }
        job = parse_theirstack_job(raw)
        assert job is not None
        assert job.id == "intern456"
        assert job.title == "Data Science Intern"
        assert job.company == "DataCo"
        assert job.company_domain == "dataco.io"
        assert job.role_kind == "internship"
        assert job.remote == 0
        assert job.skills == ("Python", "R", "SQL")
        assert job.locations == ("Toronto, ON",)
        assert job.digest["workplace"] == "On-site/Hybrid"
        assert job.digest["salary_label"] == "Salary not listed"

    def test_company_string_fallback(self) -> None:
        raw = {
            "id": "str789",
            "title": "QA Co-op",
            "company_name": "Startup Inc",
            "company_domain": "startup.io",
        }
        job = parse_theirstack_job(raw)
        assert job is not None
        assert job.company == "Startup Inc"
        assert job.company_domain == "startup.io"

    def test_non_matching_title_returns_none(self) -> None:
        raw = {
            "id": "senior1",
            "title": "Senior Software Engineer",
            "job_title": "Senior Software Engineer",
        }
        job = parse_theirstack_job(raw)
        assert job is None

    def test_missing_id_returns_none(self) -> None:
        job = parse_theirstack_job({"title": "Data Intern"})
        assert job is None

    def test_empty_id_returns_none(self) -> None:
        job = parse_theirstack_job({"id": "", "title": "Data Intern"})
        assert job is None

    def test_location_falls_back_to_country(self) -> None:
        raw = {
            "id": "loc1",
            "title": "Remote Intern",
            "job_country_code": "GB",
        }
        job = parse_theirstack_job(raw)
        assert job is not None
        assert job.locations == ("GB",)

    def test_skills_skips_mapping_items_without_name_skill_or_value(self) -> None:
        raw = {
            "id": "skill1",
            "title": "Co-op Developer",
            "skills": [
                {"name": "Python"},
                {"other": "irrelevant"},
                {"value": "SQL"},
            ],
        }
        job = parse_theirstack_job(raw)
        assert job is not None
        assert job.skills == ("Python", "SQL")

    def test_remote_value_matches_storage_logic(self) -> None:
        raw = {
            "id": "r1",
            "title": "Remote Co-op",
            "remote": "remote",
        }
        job = parse_theirstack_job(raw)
        assert job is not None
        assert job.remote == 1

        raw2 = {
            "id": "r2",
            "title": "Onsite Co-op",
            "remote": "hybrid",
        }
        job2 = parse_theirstack_job(raw2)
        assert job2 is not None
        assert job2.remote == 0

    def test_salary_numeric_float(self) -> None:
        raw = {
            "id": "sal1",
            "title": "Paid Intern",
            "min_annual_salary_usd": "60000",
            "max_annual_salary_usd": "80000",
        }
        job = parse_theirstack_job(raw)
        assert job is not None
        assert job.min_annual_salary_usd == 60000.0
        assert job.max_annual_salary_usd == 80000.0
        assert job.digest["salary_label"] == "$60,000 - $80,000"

    def test_non_numeric_salary_becomes_none(self) -> None:
        raw = {
            "id": "sal2",
            "title": "Unpaid Intern",
            "min_annual_salary_usd": "N/A",
        }
        job = parse_theirstack_job(raw)
        assert job is not None
        assert job.min_annual_salary_usd is None

    def test_digest_contains_all_keys(self) -> None:
        raw = {
            "id": "dig1",
            "job_title": "Co-op Engineer",
            "job_country_code": "US",
            "remote": True,
            "description": "A " * 300,  # >500 chars
            "skills": ["Python"],
            "url": "https://example.com",
        }
        job = parse_theirstack_job(raw)
        assert job is not None
        expected_keys = {
            "title", "company", "role_kind", "location_label", "workplace",
            "salary_label", "skills", "description", "application_url",
            "source", "posted_at", "discovered_at",
        }
        assert set(job.digest.keys()) == expected_keys
        # long description capped at 500 chars
        assert len(job.digest["description"]) <= 503  # 500 + "..."

    def test_employment_statuses_tuple(self) -> None:
        raw = {
            "id": "emp1",
            "title": "Intern",
            "employment_statuses": ["full-time", "temporary"],
        }
        job = parse_theirstack_job(raw)
        assert job is not None
        assert job.employment_statuses == ("full-time", "temporary")


class TestParsePublicJsonJob:
    _PUBLIC_JSON_ID_PREFIX = "publicjson:"

    def test_internship_parses_with_salary_currency_usd(self) -> None:
        raw = {
            "job_id": "pj789",
            "title": "Data Engineering Intern",
            "company": "BigCo",
            "location": {"country": "US", "remote": True},
            "link": "https://bigco.com/careers/intern",
            "link_final_url": "https://redirect.bigco.com/intern",
            "created_at": "2025-06-10T08:00:00",
            "last_updated": "2025-06-11T09:00:00",
            "date_posted": "2025-06-09",
            "salary": {
                "currency": "USD",
                "period": "yearly",
                "min_cents": 6000000,
                "max_cents": 8000000,
            },
        }
        job = parse_public_json_job(raw)
        assert job is not None
        assert job.id == f"{self._PUBLIC_JSON_ID_PREFIX}pj789"
        assert job.title == "Data Engineering Intern"
        assert job.company == "BigCo"
        assert job.country_code == "US"
        assert job.remote == 1  # True → 1
        assert job.min_annual_salary_usd == 60000.0
        assert job.max_annual_salary_usd == 80000.0
        assert job.source == "public_json"
        assert job.url == "https://bigco.com/careers/intern"
        assert job.final_url == "https://redirect.bigco.com/intern"
        assert job.date_posted == "2025-06-09"
        assert job.discovered_at == "2025-06-10T08:00:00"
        assert job.role_kind == "internship"
        assert job.digest["salary_label"] == "$60,000 - $80,000"

    def test_raw_preservation(self) -> None:
        raw = {
            "job_id": "pjraw1",
            "title": "Software Co-op",
            "ats": "Greenhouse",
        }
        job = parse_public_json_job(raw)
        assert job is not None
        assert job.raw == dict(raw)
        assert job.raw["ats"] == "Greenhouse"

    def test_non_intern_title_returns_none(self) -> None:
        raw = {
            "job_id": "pjsenior",
            "title": "Senior Staff Engineer",
        }
        job = parse_public_json_job(raw)
        assert job is None

    def test_missing_job_id_returns_none(self) -> None:
        job = parse_public_json_job({"title": "Intern"})
        assert job is None

    def test_salary_only_non_usd_currency(self) -> None:
        raw = {
            "job_id": "pjcur1",
            "title": "Co-op",
            "salary": {
                "currency": "EUR",
                "period": "yearly",
                "min_cents": 5000000,
            },
        }
        job = parse_public_json_job(raw)
        assert job is not None
        assert job.min_annual_salary_usd is None

    def test_salary_non_annual_period(self) -> None:
        raw = {
            "job_id": "pjper1",
            "title": "Co-op",
            "salary": {
                "currency": "USD",
                "period": "monthly",
                "min_cents": 500000,
            },
        }
        job = parse_public_json_job(raw)
        assert job is not None
        assert job.min_annual_salary_usd is None

    def test_digest_location_label(self) -> None:
        raw = {
            "job_id": "pjloc1",
            "title": "US Co-op",
            "location": {"country": "CA"},
        }
        job = parse_public_json_job(raw)
        assert job is not None
        assert job.digest["location_label"] == "CA"

    def test_digest_location_unknown(self) -> None:
        raw = {
            "job_id": "pjloc2",
            "title": "Remote Intern",
        }
        job = parse_public_json_job(raw)
        assert job is not None
        assert job.digest["location_label"] == "Location unknown"


class TestParsedJobToStorageMapping:
    def test_returns_all_fields_and_raw_json(self) -> None:
        raw = {
            "id": "map1",
            "title": "Mapping Co-op",
        }
        job = parse_theirstack_job(raw)
        assert job is not None

        mapping = parsed_job_to_storage_mapping(job)
        assert isinstance(mapping, dict)
        assert mapping["id"] == "map1"
        assert mapping["title"] == "Mapping Co-op"
        assert mapping["role_kind"] == "co_op"
        assert mapping["source"] == "theirstack"
        assert "raw_json" in mapping
        parsed_raw = json.loads(mapping["raw_json"])
        assert parsed_raw == raw

    def test_includes_digest(self) -> None:
        raw = {
            "id": "map2",
            "title": "Digest Co-op",
            "remote": True,
        }
        job = parse_theirstack_job(raw)
        assert job is not None

        mapping = parsed_job_to_storage_mapping(job)
        assert mapping["digest"]["workplace"] == "Remote"
        assert mapping["digest"]["title"] == "Digest Co-op"


class TestEdgeCases:
    def test_title_fallback_from_job_title_to_title(self) -> None:
        raw = {
            "id": "tb1",
            "title": "Co-op Engineer",
        }
        job = parse_theirstack_job(raw)
        assert job is not None
        assert job.title == "Co-op Engineer"
        assert job.role_kind == "co_op"

    def test_company_mapping_without_name(self) -> None:
        raw = {
            "id": "cn1",
            "title": "Co-op",
            "company": {"not_name": "Irrelevant"},
            "company_name": "Fallback Co",
        }
        job = parse_theirstack_job(raw)
        assert job is not None
        assert job.company == "Fallback Co"