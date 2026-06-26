from __future__ import annotations

import json

import httpx

SEARCH_URL = "https://api.theirstack.com/v1/jobs/search"


class TheirStackError(RuntimeError):
    """Raised when TheirStack cannot fulfill a request."""


class TheirStackClient:
    def __init__(self, api_key: str, timeout_seconds: float = 30.0) -> None:
        if not api_key.strip():
            raise ValueError("Missing THEIRSTACK_API_KEY; set it in the environment or scraper/.env")
        self._api_key = api_key
        self._timeout_seconds = timeout_seconds

    def search_jobs(self, payload: dict[str, object]) -> dict[str, object]:
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        try:
            response = httpx.post(SEARCH_URL, headers=headers, json=payload, timeout=self._timeout_seconds)
        except httpx.HTTPError as exc:
            raise TheirStackError(f"TheirStack request failed before receiving a response: {exc}") from exc

        if response.status_code in (401, 403):
            raise TheirStackError(f"TheirStack returned {response.status_code}; check THEIRSTACK_API_KEY")
        if response.status_code == 429:
            raise TheirStackError(f"TheirStack returned {response.status_code}; rate limited or credits exhausted")
        if response.status_code >= 400:
            body_prefix = response.text[:500]
            raise TheirStackError(f"TheirStack returned {response.status_code}: {body_prefix}")

        try:
            data = response.json()
        except json.JSONDecodeError as exc:
            raise TheirStackError("TheirStack returned non-JSON response") from exc
        if not isinstance(data, dict):
            raise TheirStackError("TheirStack returned non-JSON response")
        return data
