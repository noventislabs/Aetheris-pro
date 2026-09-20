"""Async HTTP client for public exchange endpoints.

Shared by every exchange adapter, so retry, timeout and error-mapping
behaviour is decided once rather than per venue.

The retry policy is deliberately narrow:

* **Only GET is ever retried.** Retrying a non-idempotent call is how one
  intended order becomes two. Phase 2 is read-only, but the guard is written
  now so that the execution phases inherit it rather than having to remember
  it.
* **Only transient faults are retried** -- timeouts, connection errors, 429 and
  5xx. A 4xx means our request was wrong, and repeating it cannot help.
* **A malformed response is never retried.** Asking again for nonsense returns
  nonsense; reporting no data is the safe outcome.
* **Attempts and total time are both bounded.** No unbounded loop, and a hard
  overall deadline so a slow venue cannot pin a request handler open.
"""

from __future__ import annotations

import asyncio
import random
import time
from typing import Any, Final

import httpx

from aetheris.adapters.exchange.errors import (
    ExchangeInvalidResponseError,
    ExchangeRateLimitedError,
    ExchangeTimeoutError,
    ExchangeUnavailableError,
)
from aetheris.core.logging import get_logger

_log = get_logger("exchange.http")

#: Status codes worth a second attempt. 418 is included because Binance uses it
#: to signal an IP auto-ban following repeated 429s -- it is rate limiting by
#: another name, and must be backed off from rather than hammered.
_RETRYABLE_STATUS: Final = frozenset({429, 418, 500, 502, 503, 504})
_RATE_LIMIT_STATUS: Final = frozenset({429, 418})


class ExchangeHttpClient:
    """Thin, retrying JSON client over ``httpx.AsyncClient``."""

    def __init__(
        self,
        *,
        base_url: str,
        source: str,
        request_timeout_seconds: float = 10.0,
        connect_timeout_seconds: float = 5.0,
        max_retries: int = 3,
        backoff_seconds: float = 0.5,
        max_backoff_seconds: float = 8.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._source = source
        self._max_retries = max_retries
        self._backoff_seconds = backoff_seconds
        self._max_backoff_seconds = max_backoff_seconds
        self._request_timeout = request_timeout_seconds

        # A total deadline covering every attempt and every backoff, so the
        # worst case is knowable rather than the sum of whatever happens.
        self._total_timeout = (
            request_timeout_seconds * (max_retries + 1) + max_backoff_seconds * max_retries + 1.0
        )

        self._client = httpx.AsyncClient(
            base_url=base_url,
            timeout=httpx.Timeout(
                request_timeout_seconds,
                connect=connect_timeout_seconds,
            ),
            transport=transport,
            headers={"User-Agent": "Aetheris-Pro/0.1 (+market-data; read-only)"},
            follow_redirects=False,
        )

    @property
    def source(self) -> str:
        return self._source

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> ExchangeHttpClient:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.aclose()

    def _backoff_delay(self, attempt: int, retry_after: float | None) -> float:
        """Exponential backoff with full jitter, capped.

        Jitter matters even for a single client: without it, several concurrent
        symbol requests that hit one 429 would all retry at the same instant
        and trip the limit again together.
        """
        if retry_after is not None:
            return min(retry_after, self._max_backoff_seconds)
        if self._backoff_seconds <= 0:
            return 0.0
        ceiling = min(self._backoff_seconds * (2**attempt), self._max_backoff_seconds)
        # Jitter, not cryptography -- a predictable delay is harmless here.
        return random.uniform(0.0, ceiling)  # noqa: S311

    @staticmethod
    def _retry_after_seconds(response: httpx.Response) -> float | None:
        """Honour the venue's own guidance when it supplies a Retry-After."""
        raw = response.headers.get("Retry-After")
        if raw is None:
            return None
        try:
            value = float(raw)
        except ValueError:
            return None
        return value if value >= 0 else None

    async def get_json(self, path: str, params: dict[str, Any] | None = None) -> Any:
        """GET a JSON document, retrying transient faults.

        Raises one of the :mod:`~aetheris.adapters.exchange.errors` exceptions;
        never returns a partial or invented result.
        """
        try:
            async with asyncio.timeout(self._total_timeout):
                return await self._get_json_attempts(path, params)
        except TimeoutError as exc:
            _log.warning(
                "exchange_timeout",
                source=self._source,
                path=path,
                reason="total_budget_exhausted",
                total_timeout_seconds=self._total_timeout,
            )
            raise ExchangeTimeoutError(
                f"Exchange request exceeded the total budget of {self._total_timeout:.1f}s",
                details={"path": path, "source": self._source},
            ) from exc

    async def _get_json_attempts(self, path: str, params: dict[str, Any] | None) -> Any:
        last_error: Exception | None = None

        for attempt in range(self._max_retries + 1):
            started = time.perf_counter()
            _log.debug("exchange_request", source=self._source, path=path, attempt=attempt)

            try:
                response = await self._client.get(path, params=params)
            except httpx.TimeoutException:
                last_error = ExchangeTimeoutError(
                    f"Exchange request to {path} timed out",
                    details={"path": path, "source": self._source},
                )
                _log.warning("exchange_timeout", source=self._source, path=path, attempt=attempt)
            except httpx.HTTPError as exc:
                last_error = ExchangeUnavailableError(
                    f"Could not reach the exchange: {type(exc).__name__}",
                    details={"path": path, "source": self._source},
                )
                _log.warning(
                    "exchange_error",
                    source=self._source,
                    path=path,
                    attempt=attempt,
                    error_type=type(exc).__name__,
                )
            else:
                duration_ms = round((time.perf_counter() - started) * 1000, 2)
                status = response.status_code

                if status < 400:
                    _log.debug(
                        "exchange_response",
                        source=self._source,
                        path=path,
                        status_code=status,
                        duration_ms=duration_ms,
                    )
                    return self._decode(response, path)

                retry_after = self._retry_after_seconds(response)
                if status in _RATE_LIMIT_STATUS:
                    _log.warning(
                        "exchange_rate_limited",
                        source=self._source,
                        path=path,
                        status_code=status,
                        attempt=attempt,
                        retry_after_seconds=retry_after,
                    )
                    last_error = ExchangeRateLimitedError(
                        f"Exchange rate limited the request (HTTP {status})",
                        retry_after_seconds=retry_after,
                        details={"path": path, "source": self._source},
                    )
                elif status in _RETRYABLE_STATUS:
                    _log.warning(
                        "exchange_error",
                        source=self._source,
                        path=path,
                        status_code=status,
                        attempt=attempt,
                    )
                    last_error = ExchangeUnavailableError(
                        f"Exchange returned HTTP {status}",
                        details={"path": path, "source": self._source},
                    )
                else:
                    # A non-retryable 4xx: our request was wrong. Fail now.
                    _log.warning(
                        "exchange_error",
                        source=self._source,
                        path=path,
                        status_code=status,
                        attempt=attempt,
                        retryable=False,
                    )
                    raise ExchangeUnavailableError(
                        f"Exchange rejected the request with HTTP {status}",
                        details={"path": path, "source": self._source, "status": status},
                    )

                if attempt < self._max_retries:
                    await asyncio.sleep(self._backoff_delay(attempt, retry_after))
                continue

            if attempt < self._max_retries:
                await asyncio.sleep(self._backoff_delay(attempt, None))

        assert last_error is not None  # loop always runs at least once
        raise last_error

    def _decode(self, response: httpx.Response, path: str) -> Any:
        try:
            return response.json()
        except ValueError as exc:
            _log.warning("exchange_error", source=self._source, path=path, reason="invalid_json")
            raise ExchangeInvalidResponseError(
                "Exchange response was not valid JSON",
                details={"path": path, "source": self._source},
            ) from exc
