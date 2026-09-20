"""Retry, timeout and error-mapping semantics for exchange requests."""

from __future__ import annotations

import asyncio

import httpx
import pytest

from aetheris.adapters.exchange.errors import (
    ExchangeInvalidResponseError,
    ExchangeRateLimitedError,
    ExchangeTimeoutError,
    ExchangeUnavailableError,
)
from aetheris.adapters.exchange.http import ExchangeHttpClient


def build_client(
    handler: object, *, max_retries: int = 2, request_timeout: float = 5.0
) -> ExchangeHttpClient:
    """Client wired to a mock transport, with backoff removed so tests are fast."""
    return ExchangeHttpClient(
        base_url="https://exchange.test",
        source="test:rest",
        request_timeout_seconds=request_timeout,
        connect_timeout_seconds=request_timeout,
        max_retries=max_retries,
        backoff_seconds=0.0,
        max_backoff_seconds=0.0,
        transport=httpx.MockTransport(handler),  # type: ignore[arg-type]
    )


class CountingHandler:
    """Records attempts and replays a scripted sequence of responses."""

    def __init__(self, *responses: httpx.Response) -> None:
        self._responses = list(responses)
        self.calls = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        index = min(self.calls - 1, len(self._responses) - 1)
        return self._responses[index]


async def test_successful_request_returns_parsed_json() -> None:
    handler = CountingHandler(httpx.Response(200, json={"ok": True}))
    async with build_client(handler) as client:
        assert await client.get_json("/thing") == {"ok": True}
    assert handler.calls == 1


async def test_server_error_is_retried_then_reported() -> None:
    handler = CountingHandler(httpx.Response(500))
    async with build_client(handler, max_retries=2) as client:
        with pytest.raises(ExchangeUnavailableError) as exc:
            await client.get_json("/thing")
    assert handler.calls == 3  # first attempt plus two retries, never unbounded
    assert exc.value.code == "EXCHANGE_UNAVAILABLE"


async def test_transient_failure_recovers_on_retry() -> None:
    handler = CountingHandler(
        httpx.Response(503),
        httpx.Response(200, json={"recovered": True}),
    )
    async with build_client(handler) as client:
        assert await client.get_json("/thing") == {"recovered": True}
    assert handler.calls == 2


async def test_rate_limit_maps_to_its_own_code() -> None:
    handler = CountingHandler(httpx.Response(429, headers={"Retry-After": "0"}))
    async with build_client(handler, max_retries=1) as client:
        with pytest.raises(ExchangeRateLimitedError) as exc:
            await client.get_json("/thing")
    assert exc.value.code == "EXCHANGE_RATE_LIMITED"
    assert exc.value.status_code == 429
    assert exc.value.retry_after_seconds == 0.0


async def test_binance_ip_ban_status_is_treated_as_rate_limiting() -> None:
    # 418 is Binance's auto-ban after repeated 429s: back off, do not hammer.
    handler = CountingHandler(httpx.Response(418))
    async with build_client(handler, max_retries=0) as client:
        with pytest.raises(ExchangeRateLimitedError):
            await client.get_json("/thing")


async def test_client_error_is_not_retried() -> None:
    # A 400 means our request was wrong; repeating it cannot help.
    handler = CountingHandler(httpx.Response(400, json={"code": -1121}))
    async with build_client(handler, max_retries=3) as client:
        with pytest.raises(ExchangeUnavailableError):
            await client.get_json("/thing")
    assert handler.calls == 1


async def test_invalid_json_is_not_retried() -> None:
    handler = CountingHandler(httpx.Response(200, content=b"<html>maintenance</html>"))
    async with build_client(handler, max_retries=3) as client:
        with pytest.raises(ExchangeInvalidResponseError):
            await client.get_json("/thing")
    assert handler.calls == 1


async def test_timeout_maps_to_timeout_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("too slow", request=request)

    async with build_client(handler, max_retries=1) as client:
        with pytest.raises(ExchangeTimeoutError) as exc:
            await client.get_json("/thing")
    assert exc.value.code == "EXCHANGE_TIMEOUT"
    assert exc.value.status_code == 504


async def test_connection_error_maps_to_unavailable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to host", request=request)

    async with build_client(handler, max_retries=0) as client:
        with pytest.raises(ExchangeUnavailableError):
            await client.get_json("/thing")


async def test_slow_response_hits_the_read_timeout() -> None:
    class SlowTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            await asyncio.sleep(2)
            return httpx.Response(200, json={})

    client = ExchangeHttpClient(
        base_url="https://exchange.test",
        source="test:rest",
        request_timeout_seconds=0.05,
        connect_timeout_seconds=0.05,
        max_retries=0,
        backoff_seconds=0.0,
        max_backoff_seconds=0.0,
        transport=SlowTransport(),
    )
    async with client:
        with pytest.raises(ExchangeTimeoutError):
            await client.get_json("/slow")


async def test_zero_retries_means_exactly_one_attempt() -> None:
    handler = CountingHandler(httpx.Response(500))
    async with build_client(handler, max_retries=0) as client:
        with pytest.raises(ExchangeUnavailableError):
            await client.get_json("/thing")
    assert handler.calls == 1


async def test_query_parameters_are_forwarded() -> None:
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(dict(request.url.params))
        return httpx.Response(200, json=[])

    async with build_client(handler) as client:
        await client.get_json("/klines", {"symbol": "BTCUSDT", "limit": 5})
    assert seen == {"symbol": "BTCUSDT", "limit": "5"}


async def test_no_credential_headers_are_ever_sent() -> None:
    """Phase 2 is public data: nothing may look like an authenticated call."""
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update({k.lower(): v for k, v in request.headers.items()})
        return httpx.Response(200, json={})

    async with build_client(handler) as client:
        await client.get_json("/thing")

    assert "x-mbx-apikey" not in seen
    assert "authorization" not in seen
