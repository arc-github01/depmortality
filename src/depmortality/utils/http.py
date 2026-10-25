"""httpx client with retries, exponential backoff and GitHub rate-limit awareness."""

from __future__ import annotations

import logging
import os
import time
from collections import Counter
from collections.abc import Callable, Mapping
from typing import Any

import httpx

from depmortality.config import HttpThresholds

log = logging.getLogger(__name__)

RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
USER_AGENT = "depmortality-research/0.0.1 (+https://github.com/; research crawler)"


class HttpRetriesExhausted(RuntimeError):
    pass


class MissingCredential(RuntimeError):
    pass


def github_token() -> str:
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if not token:
        raise MissingCredential("GITHUB_TOKEN is not set (environment or .env)")
    return token


class HttpClient:
    """Synchronous client. ``sleep`` and ``clock`` are injectable for tests."""

    def __init__(
        self,
        cfg: HttpThresholds,
        *,
        headers: Mapping[str, str] | None = None,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.cfg = cfg
        self._sleep = sleep
        self._clock = clock
        self.calls: Counter[str] = Counter()
        self._client = httpx.Client(
            headers={"User-Agent": USER_AGENT, **(headers or {})},
            timeout=cfg.timeout_seconds,
            transport=transport,
            follow_redirects=True,
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> HttpClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _backoff(self, attempt: int) -> float:
        return float(min(self.cfg.backoff_max_seconds, self.cfg.backoff_base_seconds * 2**attempt))

    def _rate_limit_wait(self, response: httpx.Response) -> float | None:
        """Seconds to wait before retrying, if the response says we are rate-limited."""
        retry_after = response.headers.get("retry-after")
        if retry_after is not None:
            try:
                return max(0.0, float(retry_after))
            except ValueError:
                return None
        remaining = response.headers.get("x-ratelimit-remaining")
        reset = response.headers.get("x-ratelimit-reset")
        if remaining == "0" and reset is not None:
            return max(0.0, float(reset) - self._clock()) + 1.0
        return None

    def _pace(self, response: httpx.Response) -> None:
        """After a successful call, sleep until reset when the budget is nearly spent."""
        remaining = response.headers.get("x-ratelimit-remaining")
        reset = response.headers.get("x-ratelimit-reset")
        if remaining is None or reset is None:
            return
        try:
            if int(remaining) < self.cfg.github_min_remaining:
                wait = max(0.0, float(reset) - self._clock()) + 1.0
                log.info("rate limit low (%s left); sleeping %.0fs until reset", remaining, wait)
                self._sleep(wait)
        except ValueError:
            return

    def request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        """Returns the response for 2xx and non-retryable 4xx; the caller handles 404 etc."""
        last: str = ""
        for attempt in range(self.cfg.max_retries + 1):
            try:
                response = self._client.request(method, url, **kwargs)
            except httpx.TransportError as exc:
                last = f"{type(exc).__name__}"
                self._sleep(self._backoff(attempt))
                continue
            self.calls[response.url.host] += 1
            wait = self._rate_limit_wait(response)
            if response.status_code in RETRY_STATUSES or (
                response.status_code == 403 and wait is not None
            ):
                last = f"HTTP {response.status_code}"
                delay = wait if wait is not None else self._backoff(attempt)
                log.warning("%s %s -> %s; retrying in %.0fs", method, url, last, delay)
                self._sleep(delay)
                continue
            self._pace(response)
            return response
        raise HttpRetriesExhausted(f"{method} {url}: gave up after retries ({last})")
