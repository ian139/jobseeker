from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

import httpx

SEARCH_PATH = "/v1/jobs/search"


class PaidFetchDisabledError(RuntimeError):
    """Raised when a request could consume credits while paid fetch is disabled."""


class TheirStackError(RuntimeError):
    """Raised when TheirStack cannot fulfill a request."""


@dataclass(frozen=True)
class CreditEstimate:
    dry_run_credits: int
    paid_mode_max_credits: int


def is_credit_safe_payload(payload: dict[str, Any]) -> bool:
    return (
        payload.get("blur_company_data") is True
        and payload.get("include_total_results") is True
        and payload.get("limit") == 1
    )


def estimate_credits(payload: dict[str, Any]) -> CreditEstimate:
    if is_credit_safe_payload(payload):
        return CreditEstimate(dry_run_credits=0, paid_mode_max_credits=0)
    limit = payload.get("limit", 25)
    max_returned = limit if isinstance(limit, int) and limit > 0 else 25
    return CreditEstimate(dry_run_credits=0, paid_mode_max_credits=max_returned)


class TheirStackClient:
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
        if not self._enable_paid_fetch and not is_credit_safe_payload(payload):
            raise PaidFetchDisabledError(
                "TheirStack paid fetch is disabled; use blur_company_data=true, include_total_results=true, limit=1 "
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
                    raise TheirStackError(f"TheirStack request failed before receiving a response: {exc}") from exc
                self._backoff(attempt)
                continue

            if response.status_code == 429 or 500 <= response.status_code < 600:
                if attempt == self._max_retries:
                    raise TheirStackError(f"TheirStack returned {response.status_code}: {response.text[:500]}")
                self._backoff(attempt, response)
                continue
            if response.status_code in (401, 403):
                raise TheirStackError(f"TheirStack returned {response.status_code}; check THEIRSTACK_API_KEY")
            if response.status_code >= 400:
                raise TheirStackError(f"TheirStack returned {response.status_code}: {response.text[:500]}")

            try:
                data = response.json()
            except ValueError as exc:
                raise TheirStackError("TheirStack returned non-JSON response") from exc
            if not isinstance(data, dict):
                raise TheirStackError("TheirStack returned non-object JSON response")
            return data

        raise TheirStackError(f"TheirStack request failed: {last_error}")

    def _post(self, url: str, headers: dict[str, str], payload: dict[str, Any]) -> httpx.Response:
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
