from __future__ import annotations

import time
from typing import Any, Literal

import httpx

from jobs_assistant.backlog import upsert_jobs
from jobs_assistant.contracts import CreditEstimate, JobInput

SEARCH_PATH = "/v1/jobs/search"


# --- Domain errors -----------------------------------------------------------


class PaidFetchDisabledError(RuntimeError):
    """Raised when a request could consume credits while paid fetch is disabled."""


class TheirStackError(RuntimeError):
    """Raised when TheirStack cannot fulfill a request."""


# --- Credit safety -----------------------------------------------------------

# Profiles are simple string literals for typing convenience.
ProfileName = Literal["fall_coop_swe_data", "default"]
PROFILE_NAMES: tuple[ProfileName, ...] = ("fall_coop_swe_data",)


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


def estimate_credits(payload: dict[str, Any]) -> CreditEstimate:
    """Return a credit estimate given a payload."""
    if is_credit_safe_payload(payload):
        return CreditEstimate(dry_run_credits=0, paid_mode_max_credits=0)
    limit = payload.get("limit", 25)
    max_returned = limit if isinstance(limit, int) and limit > 0 else 25
    return CreditEstimate(dry_run_credits=0, paid_mode_max_credits=max_returned)


# --- Payload builders --------------------------------------------------------


def build_preview_payload(
    profile: ProfileName = "default",
) -> dict[str, Any]:
    """Build a credit-safe preview payload (free, blurry data, count only)."""
    payload: dict[str, Any] = {
        "blur_company_data": True,
        "include_total_results": True,
        "limit": 1,
        "order_by": "date_posted",
        "order_by_desc": True,
        "page": 0,
        "remote": True,
    }
    if profile == "fall_coop_swe_data":
        payload["job_title"] = ["software engineer", "data scientist", "backend engineer"]
        payload["seniority"] = ["entry_level", "mid_level"]
        payload["employment_type"] = ["full_time"]
    else:
        payload["remote"] = True
        payload["employment_type"] = ["full_time", "contract"]
    return payload


def build_paid_fetch_payload(
    profile: ProfileName = "default",
    *,
    limit: int = 25,
    discovered_at_gt: str | None = None,
) -> dict[str, Any]:
    """Build a paid-fetch payload that returns full job data.

    Raises ValueError for invalid limits.
    """
    if limit < 1 or limit > 100:
        raise ValueError("paid-fetch limit must be between 1 and 100")

    payload: dict[str, Any] = {
        "blur_company_data": False,
        "include_total_results": True,
        "limit": limit,
        "order_by": "date_posted",
        "order_by_desc": True,
        "page": 0,
        "remote": True,
    }

    if discovered_at_gt is not None:
        payload["discovered_at"] = {"$gt": discovered_at_gt}

    if profile == "fall_coop_swe_data":
        payload["job_title"] = ["software engineer", "data scientist", "backend engineer"]
        payload["seniority"] = ["entry_level", "mid_level"]
        payload["employment_type"] = ["full_time"]
        payload["remote"] = True
    else:
        payload["employment_type"] = ["full_time", "contract"]

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
    best_by_company: dict[str, tuple[tuple[int, float, float, int], dict[str, Any], int]] = {}

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


def extract_jobs(response: dict[str, Any]) -> list[dict[str, Any]]:
    """Extract job list from TheirStack response."""
    for key in ("data", "jobs", "results"):
        items = response.get(key)
        if isinstance(items, list):
            return items
    return []


def response_total_results(response: dict[str, Any]) -> int | None:
    """Return the total_results field, if present."""
    value = response.get("total_results")
    if isinstance(value, int):
        return value
    if isinstance(value, float):
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
            raise ValueError("Missing THEIRSTACK_API_KEY; set it in .env")
        self._api_key = api_key
        self._enable_paid_fetch = enable_paid_fetch
        self._base_url = base_url.rstrip("/")
        self._timeout_seconds = timeout_seconds
        self._max_retries = max_retries
        self._client = client

    def search_jobs(self, payload: dict[str, Any]) -> dict[str, Any]:
        """POST /v1/jobs/search.

        Raises PaidFetchDisabledError if the payload is not credit-safe and
        paid fetch has not been explicitly enabled.
        """
        if not self._enable_paid_fetch and not is_credit_safe_payload(payload):
            raise PaidFetchDisabledError(
                "TheirStack paid fetch is disabled; use a credit-safe payload "
                "(blur_company_data=true, include_total_results=true, limit=1) "
                "or set ENABLE_PAID_FETCH=true after explicit approval."
            )

        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        url = f"{self._base_url}{SEARCH_PATH}"
        last_error: Exception | None = None

        for attempt in range(self._max_retries + 1):
            try:
                response = self._post(url, headers, payload)
            except httpx.HTTPError as exc:
                last_error = exc
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
                    f"TheirStack returned {response.status_code}; check THEIRSTACK_API_KEY"
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

        raise TheirStackError(f"TheirStack request failed: {last_error}")

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


def raw_job_to_input(raw: dict[str, Any]) -> JobInput:
    company = raw.get("company")
    company_name = ""
    if isinstance(company, dict):
        company_name = str(company.get("name") or "")
    company_name = company_name or str(raw.get("company_name") or raw.get("company") or "")
    source_id = raw.get("id") or raw.get("job_id") or raw.get("theirstack_job_id")
    apply_url = raw.get("apply_url") or raw.get("url") or raw.get("job_url")
    return JobInput(
        source="theirstack",
        source_job_id=None if source_id is None else str(source_id),
        url=None if apply_url is None else str(apply_url),
        title=str(raw.get("job_title") or raw.get("title") or "Untitled role"),
        company=company_name or "Unknown company",
        location=None if raw.get("location") is None else str(raw.get("location")),
        remote=bool(raw.get("remote")) if raw.get("remote") is not None else None,
        posted_at=None if raw.get("date_posted") is None else str(raw.get("date_posted")),
        description=None if raw.get("description") is None else str(raw.get("description")),
        raw=raw,
    )


def sync_theirstack_response(
    conn: Any,
    response: dict[str, Any],
    *,
    paid_fetch_enabled: bool,
    one_per_company: bool = True,
) -> tuple[int, int, int]:
    if not paid_fetch_enabled:
        raise PaidFetchDisabledError("Refusing to sync paid TheirStack results without explicit paid-fetch enablement")
    raw_jobs = extract_jobs(response)
    if one_per_company:
        raw_jobs = select_one_job_per_company(raw_jobs)
    jobs = [raw_job_to_input(raw) for raw in raw_jobs]
    inserted, updated = upsert_jobs(conn, jobs)
    return len(jobs), inserted, updated