from __future__ import annotations

from copy import deepcopy
from typing import Any, Literal

ProfileName = Literal["fall_coop_swe_data"]

PROFILE_NAMES: tuple[ProfileName, ...] = ("fall_coop_swe_data",)

BAD_TITLE_MATCHES = [
    "senior",
    "sr.",
    "staff",
    "principal",
    "manager",
    "director",
    "lead",
    "architect",
    "recruiter",
    "sales",
    "account executive",
]

BAD_DESCRIPTION_PATTERNS = [
    "(?i)\\b(5|6|7|8|9|10)\\+?\\s+years?\\b",
    "(?i)\\bactive\\s+security\\s+clearance\\b",
    "(?i)\\bcommission[- ]only\\b",
]


ALLOWED_SEARCH_FIELDS = {
    "blur_company_data",
    "include_total_results",
    "limit",
    "page",
    "posted_at_max_age_days",
    "order_by",
    "is_closed",
    "company_type",
    "job_country_code_or",
    "job_title_not",
    "job_description_pattern_not",
    "job_title_or",
    "job_description_pattern_or",
    "job_seniority_or",
    "employment_statuses_or",
    "remote",
    "discovered_at_gte",
}

REQUIRED_POSTING_OR_COMPANY_FIELDS = {
    "posted_at_max_age_days",
    "posted_at_gte",
    "posted_at_lte",
    "company_domain_or",
    "company_linkedin_url_or",
    "company_name_or",
}

VALID_COMPANY_TYPES = {"recruiting_agency", "direct_employer", "all"}
VALID_EMPLOYMENT_STATUSES = {
    "full_time",
    "part_time",
    "temporary",
    "internship",
    "contract",
    "freelance",
    "co_founder",
    "apprenticeship",
    "seasonal",
    "volunteer",
    "other",
}
VALID_JOB_SENIORITIES = {"c_level", "staff", "senior", "junior", "mid_level"}
VALID_ORDER_FIELDS = {"date_posted", "discovered_at", "salary", "job_title", "company", "num_jobs"}


BASE_SAFE_PAYLOAD: dict[str, Any] = {
    "blur_company_data": True,
    "include_total_results": True,
    "limit": 1,
    "page": 0,
    "posted_at_max_age_days": 30,
    "order_by": [
        {"field": "date_posted", "desc": True},
        {"field": "discovered_at", "desc": True},
    ],
    "is_closed": False,
    "company_type": "direct_employer",
    "job_country_code_or": ["US"],
    "job_title_not": BAD_TITLE_MATCHES,
    "job_description_pattern_not": BAD_DESCRIPTION_PATTERNS,
}

PROFILE_OVERRIDES: dict[ProfileName, dict[str, Any]] = {
    "fall_coop_swe_data": {
        "job_title_or": [
            "software engineer intern",
            "software developer intern",
            "backend engineer intern",
            "full stack engineer intern",
            "data scientist intern",
            "data engineer intern",
            "devops engineer intern",
            "site reliability engineer intern",
            "platform engineer intern",
            "machine learning engineer intern",
            "software engineer",
            "software developer",
            "backend engineer",
            "full stack engineer",
            "data scientist",
            "data engineer",
            "devops engineer",
            "site reliability engineer",
            "platform engineer",
            "machine learning engineer",
            "new grad software engineer",
            "new grad data scientist",
            "new grad data engineer",
            "entry level software engineer",
            "junior software engineer",
            "fall software engineer intern",
            "spring software engineer intern",
            "winter software engineer intern",
            "fall data scientist intern",
            "spring data scientist intern",
            "winter data scientist intern",
            "co-op software engineer",
            "co-op software developer",
            "co-op data scientist",
            "co-op data engineer",
        ],
        "job_description_pattern_or": [
            "(?i)\\b(fall|spring|winter)\\s+(co-op|intern(ship)?)\\b",
            "(?i)\\b(fall|spring|winter)\\s+2026\\b",
            "(?i)\\bco-op\\b",
            "(?i)\\bnew grad(uate)?s?\\b",
            "(?i)\\buniversity grad(uate)?s?\\b",
            "(?i)\\bearly career\\b",
            "(?i)\\bentry[- ]level\\b",
            "(?i)\\bgraduate program\\b",
        ],
        "remote": None,
        "posted_at_max_age_days": 7,
    },
}


def validate_search_payload(payload: dict[str, Any]) -> None:
    for field in payload:
        if field not in ALLOWED_SEARCH_FIELDS:
            raise ValueError(f"Unsupported TheirStack field: {field}")
    if not any(field in payload for field in REQUIRED_POSTING_OR_COMPANY_FIELDS):
        raise ValueError("TheirStack payload must include a posting/company filter")
    company_type = payload.get("company_type")
    if company_type is not None and company_type not in VALID_COMPANY_TYPES:
        raise ValueError(f"Invalid company_type: {company_type}")
    for value in payload.get("employment_statuses_or", []) or []:
        if value not in VALID_EMPLOYMENT_STATUSES:
            raise ValueError(f"Invalid employment_statuses_or: {value}")
    for value in payload.get("job_seniority_or", []) or []:
        if value not in VALID_JOB_SENIORITIES:
            raise ValueError(f"Invalid job_seniority_or: {value}")
    for order in payload.get("order_by", []) or []:
        if isinstance(order, dict):
            value = order.get("field")
        else:
            value = order
        if value not in VALID_ORDER_FIELDS:
            raise ValueError(f"Invalid order_by field: {value}")


def build_preview_payload(
    profile: ProfileName,
    *,
    page: int = 0,
    discovered_at_gte: str | None = None,
    posted_at_max_age_days: int | None = None,
) -> dict[str, Any]:
    if profile not in PROFILE_OVERRIDES:
        raise ValueError(f"Unknown TheirStack profile: {profile}")
    payload = deepcopy(BASE_SAFE_PAYLOAD)
    payload.update(deepcopy(PROFILE_OVERRIDES[profile]))
    payload["page"] = page
    if posted_at_max_age_days is not None:
        if posted_at_max_age_days < 1:
            raise ValueError("posted_at_max_age_days must be at least 1")
        payload["posted_at_max_age_days"] = posted_at_max_age_days
    if discovered_at_gte:
        payload["discovered_at_gte"] = discovered_at_gte
    if payload.get("remote") is None:
        payload.pop("remote", None)
    validate_search_payload(payload)
    return payload


def build_paid_fetch_payload(
    profile: ProfileName,
    *,
    page: int,
    limit: int = 25,
    discovered_at_gte: str | None = None,
    posted_at_max_age_days: int | None = None,
) -> dict[str, Any]:
    if limit < 1 or limit > 100:
        raise ValueError("limit must be between 1 and 100")
    payload = build_preview_payload(
        profile,
        page=page,
        discovered_at_gte=discovered_at_gte,
        posted_at_max_age_days=posted_at_max_age_days,
    )
    payload["blur_company_data"] = False
    payload["include_total_results"] = page == 0
    payload["limit"] = limit
    validate_search_payload(payload)
    return payload


def all_preview_payloads() -> dict[ProfileName, dict[str, Any]]:
    return {profile: build_preview_payload(profile) for profile in PROFILE_NAMES}
