import httpx
import pytest

from theirstack.client import PaidFetchDisabledError, TheirStackClient, estimate_credits, is_credit_safe_payload
from theirstack.queries import build_paid_fetch_payload, build_preview_payload


class FakeHttpClient:
    def __init__(self, responses: list[httpx.Response]) -> None:
        self.responses = responses
        self.requests: list[dict[str, object]] = []

    def post(self, url: str, headers: dict[str, str], json: dict[str, object], timeout: float) -> httpx.Response:
        self.requests.append({"url": url, "headers": headers, "json": json, "timeout": timeout})
        return self.responses.pop(0)


def test_client_sends_auth_header_for_safe_preview() -> None:
    fake = FakeHttpClient([httpx.Response(200, json={"total_results": 123, "data": []})])
    client = TheirStackClient("secret", client=fake)
    response = client.search_jobs(build_preview_payload("fall_coop_swe_data"))
    assert response["total_results"] == 123
    assert fake.requests[0]["headers"]["Authorization"] == "Bearer secret"
    assert fake.requests[0]["url"] == "https://api.theirstack.com/v1/jobs/search"


def test_client_blocks_paid_payload_when_flag_disabled() -> None:
    client = TheirStackClient("secret", enable_paid_fetch=False, client=FakeHttpClient([]))
    with pytest.raises(PaidFetchDisabledError):
        client.search_jobs(build_paid_fetch_payload("fall_coop_swe_data", page=0, limit=25))


def test_client_allows_paid_payload_when_flag_enabled() -> None:
    fake = FakeHttpClient([httpx.Response(200, json={"data": [{"id": "1"}]})])
    client = TheirStackClient("secret", enable_paid_fetch=True, client=fake)
    response = client.search_jobs(build_paid_fetch_payload("fall_coop_swe_data", page=0, limit=25))
    assert response["data"] == [{"id": "1"}]


def test_credit_estimate_counts_preview_as_zero() -> None:
    preview = build_preview_payload("fall_coop_swe_data")
    paid = build_paid_fetch_payload("fall_coop_swe_data", page=0, limit=25)
    assert is_credit_safe_payload(preview)
    assert estimate_credits(preview).dry_run_credits == 0
    assert estimate_credits(preview).paid_mode_max_credits == 0
    assert estimate_credits(paid).paid_mode_max_credits == 25
