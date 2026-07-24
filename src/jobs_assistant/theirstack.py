from __future__ import annotations

import time
from types import MappingProxyType
from typing import Any, Literal, Mapping, cast

import httpx

from jobs_assistant.ats import SUPPORTED_ATS, select_adapter
from jobs_assistant.backlog import upsert_jobs
from jobs_assistant.job_source import normalize_job_metadata
from jobs_assistant.browser_adapter import BrowserAdapterError, validate_ats_url
from jobs_assistant.contracts import ATSFilter, JobInput

SEARCH_PATH = "/v1/jobs/search"


# --- Domain errors -----------------------------------------------------------


class PaidFetchDisabledError(RuntimeError):
    """Raised when a request could consume credits while paid fetch is disabled."""


class TheirStackError(RuntimeError):
    """Raised when TheirStack cannot fulfill a request."""


# --- Credit safety -----------------------------------------------------------

ProfileName = Literal["new_grad_cs", "new_grad_non_coop_cs", "fall_coop_swe_data", "default"]
PinnedATSFilter = Literal["greenhouse", "lever"]
ATS_FILTER_NAMES: tuple[ATSFilter, ...] = ("auto", *SUPPORTED_ATS)
ATS_URL_DOMAIN_OR: Mapping[PinnedATSFilter, tuple[str, ...]] = MappingProxyType(
    {
        "greenhouse": ("greenhouse.io", "grnh.se"),
        "lever": ("lever.co",),
    }
)


def validate_ats_filter_name(name: str) -> ATSFilter:
    """Validate a TheirStack source filter name before any ingestion work."""
    if type(name) is not str or name not in ATS_FILTER_NAMES:
        raise ValueError(f"unsupported ATS filter: {name!r}")
    return cast(ATSFilter, name)


def checkpoint_profile_key(source_profile: ProfileName, ats_filter: ATSFilter) -> str:
    """Return the bounded sync checkpoint namespace for a source/ATS selection.

    ``auto`` intentionally returns the historical source-profile key so existing
    checkpoints keep their legacy behavior.  Pinned ATS filters use separate,
    deterministic internal keys and therefore cannot inherit one another's
    checkpoint.
    """
    selected_filter = validate_ats_filter_name(ats_filter)
    if selected_filter == "auto":
        return source_profile
    return f"{source_profile}::ats::{selected_filter}"


PROFILE_NAMES: tuple[ProfileName, ...] = ("new_grad_cs", "new_grad_non_coop_cs", "fall_coop_swe_data", "default")


def is_credit_safe_payload(payload: dict[str, Any]) -> bool:
    """A payload is credit-safe when all three conditions hold:
    * blur_company_data is True
    * include_total_results is True
    * limit is 1
    """
    return (
        payload.get("blur_company_data") is True
        and payload.get("include_total_results") is True
        and payload.get("limit") == 1
    )




# --- Payload builders --------------------------------------------------------


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

CS_ROLE_TITLES = [
    "software engineer intern",
    "software developer intern",
    "backend engineer intern",
    "frontend engineer intern",
    "full stack engineer intern",
    "data scientist intern",
    "data engineer intern",
    "devops engineer intern",
    "site reliability engineer intern",
    "platform engineer intern",
    "machine learning engineer intern",
    "ai engineer intern",
    "software engineer",
    "software developer",
    "backend engineer",
    "frontend engineer",
    "full stack engineer",
    "data scientist",
    "data engineer",
    "devops engineer",
    "site reliability engineer",
    "platform engineer",
    "machine learning engineer",
    "ai engineer",
    "new grad software engineer",
    "new grad data scientist",
    "new grad data engineer",
    "entry level software engineer",
    "junior software engineer",
    "co-op software engineer",
    "co-op software developer",
    "co-op data scientist",
    "co-op data engineer",
]

EARLY_CAREER_PATTERNS = [
    "(?i)\\bco-op\\b",
    "(?i)\\bnew grad(uate)?s?\\b",
    "(?i)\\buniversity grad(uate)?s?\\b",
    "(?i)\\bearly career\\b",
    "(?i)\\bentry[- ]level\\b",
    "(?i)\\bintern(ship)?\\b",
    "(?i)\\bgraduate program\\b",
]

COOP_ROLE_TITLES = [
    "co-op software engineer",
    "co-op software developer",
    "co-op data scientist",
    "co-op data engineer",
]

COOP_DESCRIPTION_PATTERNS = [
    "(?i)\\bco-op\\b",
]

NON_COOP_ROLE_TITLES = [
    title for title in CS_ROLE_TITLES if "co-op" not in title.lower()
]

NON_COOP_EARLY_CAREER_PATTERNS = [
    pattern for pattern in EARLY_CAREER_PATTERNS if "co-op" not in pattern.lower()
]


def _validated_page(value: Any, *, name: str = "page") -> int:
    if type(value) is not int or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")
    return value


MAX_PAID_PAGES = 1000


def _base_payload(*, blur_company_data: bool, include_total_results: bool, limit: int, page: int = 0) -> dict[str, Any]:
    return {
        "blur_company_data": blur_company_data,
        "include_total_results": include_total_results,
        "limit": limit,
        "page": _validated_page(page),
        "posted_at_max_age_days": 7,
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


def _apply_profile(payload: dict[str, Any], profile: ProfileName) -> None:
    if profile == "new_grad_cs":
        payload["job_title_or"] = list(CS_ROLE_TITLES)
        payload["job_description_pattern_or"] = list(EARLY_CAREER_PATTERNS)
    elif profile == "new_grad_non_coop_cs":
        payload["job_title_or"] = list(NON_COOP_ROLE_TITLES)
        payload["job_description_pattern_or"] = list(NON_COOP_EARLY_CAREER_PATTERNS)
        payload["job_description_pattern_not"] = [
            *BAD_DESCRIPTION_PATTERNS,
            *COOP_DESCRIPTION_PATTERNS,
        ]
    elif profile == "fall_coop_swe_data":
        payload["job_title_or"] = list(COOP_ROLE_TITLES)
        payload["job_description_pattern_or"] = list(COOP_DESCRIPTION_PATTERNS)

def build_preview_payload(
    profile: ProfileName = "default",
) -> dict[str, Any]:
    """Build a credit-safe preview payload (free, blurry data, count only)."""
    payload = _base_payload(blur_company_data=True, include_total_results=True, limit=1)
    _apply_profile(payload, profile)
    return payload


def build_paid_fetch_payload(
    profile: ProfileName = "default",
    *,
    limit: int = 25,
    page: int = 0,
    discovered_at_gte: str | None = None,
    discovered_at_gt: str | None = None,
    ats_filter: ATSFilter = "auto",
) -> dict[str, Any]:
    """Build a paid-fetch payload that returns full job data.

    Raises ValueError for invalid limits, pages, or unsupported ATS filters.
    """
    selected_filter = validate_ats_filter_name(ats_filter)
    if limit < 1 or limit > 100:
        raise ValueError("paid-fetch limit must be between 1 and 100")
    page = _validated_page(page)

    payload = _base_payload(blur_company_data=False, include_total_results=True, limit=limit, page=page)
    if selected_filter != "auto":
        # Return a fresh list so callers cannot mutate later payload builds.
        payload["url_domain_or"] = list(ATS_URL_DOMAIN_OR[selected_filter])
    checkpoint = discovered_at_gte or discovered_at_gt
    if checkpoint is not None:
        payload["discovered_at_gte"] = checkpoint
    _apply_profile(payload, profile)
    return payload


# --- One-job-per-company dedupe ----------------------------------------------


def _role_priority(raw: dict[str, Any]) -> int:
    title = (raw.get("job_title") or raw.get("title") or raw.get("normalized_title") or "").lower()
    priorities = {
        "software engineer": 0,
        "software developer": 0,
        "backend engineer": 0,
        "data scientist": 1,
        "data engineer": 1,
    }
    for keyword, prio in priorities.items():
        if keyword in title:
            return prio
    return 4


def _recency_value(raw: dict[str, Any]) -> float:
    """Parse posted_at into a comparable recency float.  Larger = more recent."""
    from datetime import datetime

    posted = raw.get("date_posted") or raw.get("posted_at")
    if not posted or not isinstance(posted, str):
        return float("-inf")
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(posted[: len(fmt)], fmt)
            return dt.timestamp()
        except ValueError:
            continue
    return float("-inf")


def _company_key(raw: dict[str, Any], *, fallback: str) -> str:
    """Derive a deterministic company identifier for dedupe."""
    co = raw.get("company")
    if isinstance(co, dict):
        domain = (co.get("domain") or "").strip().lower()
        if domain:
            return domain
        name = (co.get("name") or "").strip().lower()
        if name:
            return f"co:{name}"
    name_val = raw.get("company_name")
    if isinstance(name_val, str) and name_val.strip():
        return f"co:{name_val.strip().lower()}"
    return f"job:{fallback}"


def select_one_job_per_company(
    raw_jobs: list[dict[str, Any]],
    *,
    max_selected: int | None = None,
) -> list[dict[str, Any]]:
    """Select at most one job per company, preferring highest-priority and most recent.

    Returns jobs sorted by discovered_at descending (or posted_at descending).
    """
    best_by_company: dict[str, tuple[tuple[int, float, int], dict[str, Any], int]] = {}

    for idx, job in enumerate(raw_jobs):
        title = job.get("job_title") or job.get("title") or ""
        ckey = _company_key(job, fallback=title)
        priority = _role_priority(job)
        recency = _recency_value(job)
        # Prefer: lower priority, more recent, earlier in original list
        score = (priority, -recency, idx)

        existing = best_by_company.get(ckey)
        if existing is None or score < existing[0]:
            best_by_company[ckey] = (score, job, idx)

    selected = [info[1] for info in sorted(best_by_company.values(), key=lambda x: x[2])]
    if max_selected is not None:
        selected = selected[:max_selected]
    return selected


# --- HTTP client -------------------------------------------------------------


def _extract_jobs_with_key(response: Any) -> tuple[str, list[dict[str, Any]]]:
    """Extract the validated job list and its envelope key."""
    if not isinstance(response, dict):
        raise TheirStackError("TheirStack returned non-object JSON response")
    selected_key: str | None = None
    selected: list[dict[str, Any]] | None = None
    for key in ("data", "jobs", "results"):
        if key not in response:
            continue
        items = response[key]
        if not isinstance(items, list):
            raise TheirStackError(f"TheirStack response field {key!r} is not a list")
        if any(not isinstance(item, dict) for item in items):
            raise TheirStackError(f"TheirStack response field {key!r} contains a non-object job")
        if selected_key is None:
            selected_key = key
            selected = items
    if selected_key is None or selected is None:
        raise TheirStackError(
            "TheirStack response is missing a recognized job-list key "
            "(expected one of: data, jobs, results)"
        )
    return selected_key, selected


def extract_jobs(response: Any) -> list[dict[str, Any]]:
    """Extract and validate the job list from a TheirStack response envelope."""
    return _extract_jobs_with_key(response)[1]


def _validated_total_results(response: dict[str, Any]) -> int | None:
    """Validate and return a consistent total_results value, if supplied."""
    metadata = response.get("metadata")
    if "metadata" in response and not isinstance(metadata, Mapping):
        raise TheirStackError("TheirStack response field 'metadata' is not an object")
    containers: list[Mapping[str, Any]] = [response]
    if isinstance(metadata, Mapping):
        containers.append(metadata)
    values: list[int] = []
    for container in containers:
        if "total_results" not in container:
            continue
        value = container["total_results"]
        if type(value) is not int or value < 0:
            raise TheirStackError("TheirStack response total_results must be a non-negative integer")
        values.append(value)
    if values and any(value != values[0] for value in values[1:]):
        raise TheirStackError("TheirStack response total_results values disagree")
    return values[0] if values else None


def response_total_results(response: dict[str, Any]) -> int | None:
    """Return the top-level or metadata total_results field, if present."""
    if not isinstance(response, dict):
        return None
    containers: list[Mapping[str, Any]] = [response]
    metadata = response.get("metadata")
    if isinstance(metadata, Mapping):
        containers.append(metadata)
    for container in containers:
        value = container.get("total_results")
        if type(value) is int:
            return value
        if type(value) is float:
            return int(value)
    return None


class TheirStackClient:
    """Minimal TheirStack API client with credit-safety enforcement."""

    def __init__(
        self,
        api_key: str,
        *,
        enable_paid_fetch: bool = False,
        base_url: str = "https://api.theirstack.com",
        timeout_seconds: float = 30.0,
        max_retries: int = 2,
        client: httpx.Client | None = None,
    ) -> None:
        if not api_key.strip():
            raise ValueError("Missing TheirStack API key; pass api_key explicitly")
        if type(max_retries) is not int or max_retries < 0:
            raise ValueError("max_retries must be a non-negative integer")
        self._api_key = api_key
        self._enable_paid_fetch = enable_paid_fetch
        self._base_url = base_url.rstrip("/")
        self._timeout_seconds = timeout_seconds
        # A paid request may consume credits even when the response is
        # ambiguous. Never replay it automatically. Preview requests remain
        # bounded-retry by default.
        self._max_retries = 0 if enable_paid_fetch else max_retries
        self._client = client

    def search_jobs(self, payload: dict[str, Any]) -> dict[str, Any]:
        """POST /v1/jobs/search, paging through an explicitly enabled paid fetch.

        Credit-safe preview requests remain a single bounded request. Paid
        responses are fully collected and validated before the aggregate is
        returned, so callers cannot persist a partial result set.
        """
        if not self._enable_paid_fetch and not is_credit_safe_payload(payload):
            raise PaidFetchDisabledError(
                "TheirStack paid fetch is disabled; use a credit-safe payload "
                "(blur_company_data=true, include_total_results=true, limit=1) "
                "or construct TheirStackClient with enable_paid_fetch=True after explicit approval."
            )
        if self._enable_paid_fetch and not is_credit_safe_payload(payload):
            return self._search_paid_pages(payload)
        return self._search_one_page(payload)

    def _search_paid_pages(self, payload: dict[str, Any]) -> dict[str, Any]:
        page = _validated_page(payload.get("page", 0))
        start_page = page
        limit = payload.get("limit")
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("paid-fetch limit must be between 1 and 100")

        aggregate: list[dict[str, Any]] = []
        first_response: dict[str, Any] | None = None
        selected_key: str | None = None
        total_results: int | None = None
        pages_fetched = 0

        while True:
            if pages_fetched >= MAX_PAID_PAGES:
                raise TheirStackError("TheirStack paid pagination exceeded the safety page limit")
            page_payload = dict(payload)
            page_payload["page"] = page
            response = self._search_one_page(page_payload)
            pages_fetched += 1
            page_key, page_jobs = _extract_jobs_with_key(response)
            page_total = _validated_total_results(response)
            if first_response is None:
                first_response = dict(response)
                selected_key = page_key
            elif page_key != selected_key:
                raise TheirStackError("TheirStack response job-list key changed between pages")
            if page_total is not None:
                if total_results is not None and page_total != total_results:
                    raise TheirStackError("TheirStack response total_results changed between pages")
                total_results = page_total

            aggregate.extend(page_jobs)
            observed_total = start_page * limit + len(aggregate)
            if total_results is not None:
                if observed_total > total_results:
                    raise TheirStackError("TheirStack response returned more jobs than total_results")
                if observed_total >= total_results or not page_jobs:
                    break
            elif not page_jobs or len(page_jobs) < limit:
                break
            page += 1

        if first_response is None or selected_key is None:
            raise TheirStackError("TheirStack paid pagination returned no response")
        first_response[selected_key] = aggregate
        if total_results is not None:
            if "total_results" not in first_response:
                metadata = first_response.get("metadata")
                if isinstance(metadata, Mapping):
                    merged_metadata = dict(metadata)
                    merged_metadata["total_results"] = total_results
                    first_response["metadata"] = merged_metadata
                else:
                    first_response["total_results"] = total_results
        return first_response

    def _search_one_page(self, payload: dict[str, Any]) -> dict[str, Any]:
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        url = f"{self._base_url}{SEARCH_PATH}"
        for attempt in range(self._max_retries + 1):
            try:
                response = self._post(url, headers, payload)
            except httpx.HTTPError as exc:
                if attempt == self._max_retries:
                    raise TheirStackError(
                        f"TheirStack request failed before receiving a response: {exc}"
                    ) from exc
                self._backoff(attempt)
                continue

            if response.status_code in (429,) or 500 <= response.status_code < 600:
                if attempt == self._max_retries:
                    raise TheirStackError(
                        f"TheirStack returned {response.status_code}: {response.text[:500]}"
                    )
                self._backoff(attempt, response)
                continue
            if response.status_code in (401, 403):
                raise TheirStackError(
                    "TheirStack returned 401/403; check the explicit TheirStack API key"
                )
            if response.status_code >= 400:
                raise TheirStackError(
                    f"TheirStack returned {response.status_code}: {response.text[:500]}"
                )

            try:
                data = response.json()
            except ValueError as exc:
                raise TheirStackError("TheirStack returned non-JSON response") from exc
            if not isinstance(data, dict):
                raise TheirStackError("TheirStack returned non-object JSON response")
            return data
        raise TheirStackError("TheirStack request did not return a response")

    def _post(
        self,
        url: str,
        headers: dict[str, str],
        payload: dict[str, Any],
    ) -> httpx.Response:
        if self._client is not None:
            return self._client.post(url, headers=headers, json=payload, timeout=self._timeout_seconds)
        return httpx.post(url, headers=headers, json=payload, timeout=self._timeout_seconds)

    @staticmethod
    def _backoff(attempt: int, response: httpx.Response | None = None) -> None:
        retry_after = response.headers.get("Retry-After") if response is not None else None
        if retry_after and retry_after.isdigit():
            delay = min(int(retry_after), 10)
        else:
            delay = min(0.25 * (2**attempt), 2.0)
        time.sleep(delay)


_URL_FIELDS: tuple[str, ...] = ("apply_url", "final_url", "url", "source_url", "job_url")


def _legacy_apply_url(raw: dict[str, Any]) -> Any:
    """Preserve the historical URL precedence for unpinned syncs."""
    return raw.get("apply_url") or raw.get("final_url") or raw.get("url") or raw.get("source_url") or raw.get("job_url")


def _validated_ats_url(url: object, ats_filter: ATSFilter) -> str | None:
    if not isinstance(url, str) or not url:
        return None
    try:
        adapter = select_adapter(ats_filter, url=url)
        if adapter is None or adapter.name != ats_filter:
            return None
        route = validate_ats_url(url, adapter.name)
    except (BrowserAdapterError, ValueError, TypeError):
        return None
    return url if route is not None else None


def _select_apply_url(raw: dict[str, Any], ats_filter: ATSFilter) -> Any:
    if ats_filter == "auto":
        return _legacy_apply_url(raw)
    for field in _URL_FIELDS:
        candidate = raw.get(field)
        if _validated_ats_url(candidate, ats_filter) is not None:
            return candidate
    return None




def raw_job_to_input(raw: dict[str, Any], *, ats_filter: ATSFilter = "auto") -> JobInput:
    location, remote, date_posted, description = normalize_job_metadata(raw)
    company = raw.get("company")
    company_name = ""
    if isinstance(company, dict):
        company_name = str(company.get("name") or "")
    company_name = company_name or str(raw.get("company_name") or raw.get("company") or "")
    source_id = raw.get("id") or raw.get("job_id") or raw.get("theirStackId") or raw.get("theirstack_job_id") or raw.get("theirstack_id") or raw.get("external_id") or raw.get("source_job_id")
    apply_url = _select_apply_url(raw, ats_filter)
    return JobInput(
        source="theirstack",
        source_job_id=None if source_id is None else str(source_id),
        url=None if apply_url is None else str(apply_url),
        title=str(raw.get("job_title") or raw.get("title") or "Untitled role"),
        company=company_name or "Unknown company",
        location=location,
        remote=remote,
        posted_at=date_posted,
        description=description,
        raw=raw,
    )


def _ats_job_is_eligible(job: JobInput, ats_filter: ATSFilter) -> bool:
    """Validate one normalized application URL for a pinned ATS filter.

    ``auto`` is deliberately a compatibility mode: it does not filter source
    results because the credit-free preview and historical paid sync accepted
    arbitrary source URLs.  Pinned modes use the canonical adapter and route
    validators, with no hostname substring matching.
    """
    if ats_filter == "auto":
        return True
    return _validated_ats_url(job.url, ats_filter) is not None


def _normalize_and_filter_jobs(
    raw_jobs: list[Any],
    *,
    ats_filter: ATSFilter,
) -> tuple[list[dict[str, Any]], int, int]:
    """Normalize source jobs, then apply a pinned ATS route filter.

    Returns ``(eligible_raw_jobs, fetched_count, rejected_count)``.  Filtering
    is intentionally completed before company deduplication and any upsert.
    """
    fetched = len(raw_jobs)
    rejected = 0
    eligible: list[dict[str, Any]] = []
    for raw in raw_jobs:
        if not isinstance(raw, dict):
            rejected += 1
            continue
        normalized = raw_job_to_input(raw, ats_filter=ats_filter)
        if not _ats_job_is_eligible(normalized, ats_filter):
            rejected += 1
            continue
        eligible.append(raw)
    return eligible, fetched, rejected


def sync_theirstack_response(
    conn: Any,
    response: dict[str, Any],
    *,
    paid_fetch_enabled: bool,
    one_per_company: bool = True,
    ats_filter: ATSFilter = "auto",
    stats: dict[str, int] | None = None,
) -> tuple[int, int, int]:
    """Normalize, optionally filter, dedupe, and persist a paid response.

    The three-item return value remains backward-compatible
    ``(seen, inserted, updated)``.  When ``stats`` is supplied it is populated
    with ``fetched``, ``ats_eligible``, and ``ats_rejected`` in addition to
    those counters for CLI reporting.
    """
    if not paid_fetch_enabled:
        raise PaidFetchDisabledError("Refusing to sync paid TheirStack results without explicit paid-fetch enablement")
    selected_filter = validate_ats_filter_name(ats_filter)
    raw_jobs, fetched, rejected = _normalize_and_filter_jobs(
        extract_jobs(response),
        ats_filter=selected_filter,
    )
    ats_eligible = fetched - rejected
    if one_per_company:
        raw_jobs = select_one_job_per_company(raw_jobs)
    jobs = [raw_job_to_input(raw, ats_filter=selected_filter) for raw in raw_jobs]
    inserted, updated = upsert_jobs(conn, jobs)
    seen = len(jobs)
    if stats is not None:
        stats.update(
            {
                "fetched": fetched,
                "ats_eligible": ats_eligible,
                "ats_rejected": rejected,
                "seen": seen,
                "inserted": inserted,
                "updated": updated,
            }
        )
    return seen, inserted, updated