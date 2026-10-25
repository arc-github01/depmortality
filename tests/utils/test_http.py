from __future__ import annotations

import httpx
import pytest

from depmortality.config import HttpThresholds
from depmortality.utils.http import HttpClient, HttpRetriesExhausted

CFG = HttpThresholds(
    timeout_seconds=5,
    max_retries=3,
    backoff_base_seconds=1,
    backoff_max_seconds=60,
    github_min_remaining=10,
    pypi_concurrency=1,
)


def client(responses: list[httpx.Response], sleeps: list[float], now: float = 1000.0) -> HttpClient:
    it = iter(responses)

    def handler(request: httpx.Request) -> httpx.Response:
        return next(it)

    return HttpClient(
        CFG, transport=httpx.MockTransport(handler), sleep=sleeps.append, clock=lambda: now
    )


def test_retries_5xx_with_backoff() -> None:
    sleeps: list[float] = []
    c = client([httpx.Response(502), httpx.Response(503), httpx.Response(200, json={})], sleeps)
    assert c.request("GET", "https://pypi.org/pypi/six/json").status_code == 200
    assert sleeps == [1, 2]


def test_github_rate_limit_sleeps_until_reset() -> None:
    sleeps: list[float] = []
    limited = httpx.Response(
        403, headers={"x-ratelimit-remaining": "0", "x-ratelimit-reset": "1300"}
    )
    c = client([limited, httpx.Response(200, json={})], sleeps)
    c.request("POST", "https://api.github.com/graphql")
    assert sleeps == [301.0]


def test_paces_when_budget_nearly_spent() -> None:
    sleeps: list[float] = []
    low = httpx.Response(200, headers={"x-ratelimit-remaining": "3", "x-ratelimit-reset": "1060"})
    client([low], sleeps).request("POST", "https://api.github.com/graphql")
    assert sleeps == [61.0]


def test_plain_403_and_404_are_returned_not_retried() -> None:
    sleeps: list[float] = []
    c = client([httpx.Response(404)], sleeps)
    assert c.request("GET", "https://pypi.org/pypi/nope/json").status_code == 404
    c = client([httpx.Response(403, headers={"x-ratelimit-remaining": "4000"})], sleeps)
    assert c.request("GET", "https://api.github.com/x").status_code == 403
    assert sleeps == []


def test_gives_up_after_max_retries() -> None:
    sleeps: list[float] = []
    c = client([httpx.Response(500)] * 4, sleeps)
    with pytest.raises(HttpRetriesExhausted):
        c.request("GET", "https://api.osv.dev/v1/query")


def test_counts_calls_per_host() -> None:
    sleeps: list[float] = []
    c = client([httpx.Response(200), httpx.Response(200)], sleeps)
    c.request("GET", "https://pypi.org/a")
    c.request("GET", "https://pypi.org/b")
    assert c.calls["pypi.org"] == 2
