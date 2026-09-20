"""Adapter behaviour against mocked Binance responses.

No test here touches the network: every response is scripted, so the suite is
deterministic and stays green while Binance is down.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import httpx
import pytest
from tests.fixtures import binance_payloads as payloads
from tests.fixtures.transport import RoutingHandler, json_route, raw_route, status_route

from aetheris.adapters.exchange.binance import endpoints
from aetheris.adapters.exchange.binance.adapter import BinanceFuturesMarketDataAdapter
from aetheris.adapters.exchange.errors import (
    ExchangeInvalidResponseError,
    ExchangeRateLimitedError,
    ExchangeUnavailableError,
    SymbolNotFoundError,
)
from aetheris.core.config import BinanceFuturesSettings, MarketDataSettings
from aetheris.core.freshness import DataStatus
from aetheris.domain.enums import ConnectionStatus, Timeframe


def build_adapter(handler: RoutingHandler, **overrides: object) -> BinanceFuturesMarketDataAdapter:
    settings = BinanceFuturesSettings(
        _env_file=None,  # type: ignore[call-arg]
        backoff_seconds=0.0,
        max_backoff_seconds=0.0,
        max_retries=0,
        **overrides,  # type: ignore[arg-type]
    )
    return BinanceFuturesMarketDataAdapter(
        settings=settings,
        market_data=MarketDataSettings(_env_file=None),  # type: ignore[call-arg]
        transport=handler.transport(),
    )


def default_routes() -> dict[str, object]:
    return {
        endpoints.EXCHANGE_INFO: json_route(payloads.exchange_info()),
        endpoints.TICKER_24H: json_route(payloads.ticker_24h()),
        endpoints.BOOK_TICKER: json_route(payloads.book_ticker()),
        endpoints.KLINES: json_route(payloads.klines(count=6)),
    }


def default_handler() -> RoutingHandler:
    return RoutingHandler(default_routes())  # type: ignore[arg-type]


# ----------------------------------------------------------------------
# Discovery
# ----------------------------------------------------------------------


async def test_symbol_discovery_is_dynamic() -> None:
    adapter = build_adapter(default_handler())
    try:
        eligible = await adapter.get_symbols(eligible_only=True)
        names = {s.symbol for s in eligible}
        # Derived from venue metadata, not from any hardcoded list.
        assert names == {"BTCUSDT", "ETHUSDT", "0GUSDT"}
    finally:
        await adapter.aclose()


async def test_newly_listed_symbol_appears_without_code_changes() -> None:
    universe = [payloads.symbol_entry(symbol="NEWCOINUSDT", base_asset="NEWCOIN")]
    handler = RoutingHandler(
        {**default_routes(), endpoints.EXCHANGE_INFO: json_route(payloads.exchange_info(universe))}  # type: ignore[arg-type]
    )
    adapter = build_adapter(handler)
    try:
        eligible = await adapter.get_symbols()
        assert [s.symbol for s in eligible] == ["NEWCOINUSDT"]
    finally:
        await adapter.aclose()


async def test_unfiltered_listing_returns_everything() -> None:
    adapter = build_adapter(default_handler())
    try:
        assert len(await adapter.get_symbols(eligible_only=False)) == 6
    finally:
        await adapter.aclose()


async def test_exchange_info_is_cached() -> None:
    handler = default_handler()
    adapter = build_adapter(handler)
    try:
        await adapter.get_exchange_info()
        await adapter.get_exchange_info()
        assert handler.count(endpoints.EXCHANGE_INFO) == 1
    finally:
        await adapter.aclose()


async def test_unknown_symbol_is_rejected_before_any_upstream_call() -> None:
    handler = default_handler()
    adapter = build_adapter(handler)
    try:
        with pytest.raises(SymbolNotFoundError):
            await adapter.get_ticker("NOPEUSDT")
        assert handler.count(endpoints.TICKER_24H) == 0
    finally:
        await adapter.aclose()


# ----------------------------------------------------------------------
# Ticker
# ----------------------------------------------------------------------


async def test_ticker_carries_value_and_provenance() -> None:
    adapter = build_adapter(default_handler())
    try:
        observation = await adapter.get_ticker("btcusdt")  # case-insensitive
        assert observation.status is DataStatus.OK
        assert observation.source == "binance-futures-usdm:rest"
        assert observation.value is not None
        assert observation.value.last_price == Decimal("60050.10")
        assert observation.value.bid_price == Decimal("60050.00")
        assert observation.age_seconds is not None
    finally:
        await adapter.aclose()


async def test_old_ticker_is_stale_and_carries_no_value() -> None:
    """A stale observation must not ship a number that looks current."""
    handler = RoutingHandler(
        {
            **default_routes(),  # type: ignore[arg-type]
            endpoints.TICKER_24H: json_route(payloads.ticker_24h(age_seconds=600)),
        }
    )
    adapter = build_adapter(handler)
    try:
        observation = await adapter.get_ticker("BTCUSDT")
        assert observation.status is DataStatus.STALE
        assert observation.value is None
        assert observation.detail is not None and "old" in observation.detail
    finally:
        await adapter.aclose()


async def test_book_ticker_failure_degrades_to_no_bid_ask_not_to_failure() -> None:
    handler = RoutingHandler(
        {**default_routes(), endpoints.BOOK_TICKER: status_route(500)}  # type: ignore[arg-type]
    )
    adapter = build_adapter(handler)
    try:
        observation = await adapter.get_ticker("BTCUSDT")
        assert observation.status is DataStatus.OK
        assert observation.value is not None
        assert observation.value.last_price == Decimal("60050.10")
        # Top-of-book is optional enrichment; its absence is reported as absent.
        assert observation.value.bid_price is None
        assert observation.value.ask_price is None
    finally:
        await adapter.aclose()


async def test_freshness_is_recomputed_per_serve_not_frozen_in_the_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cached ticker must not keep serving an OK status as it ages."""
    handler = default_handler()
    adapter = build_adapter(handler, ticker_ttl_seconds=3600.0)
    try:
        first = await adapter.get_ticker("BTCUSDT")
        assert first.status is DataStatus.OK

        later = payloads.now() + timedelta(hours=2)
        monkeypatch.setattr("aetheris.adapters.exchange.binance.adapter.utcnow", lambda: later)
        second = await adapter.get_ticker("BTCUSDT")

        assert handler.count(endpoints.TICKER_24H) == 1  # served from cache
        assert second.status is DataStatus.STALE  # but re-judged as stale
        assert second.value is None
    finally:
        await adapter.aclose()


# ----------------------------------------------------------------------
# Klines
# ----------------------------------------------------------------------


async def test_klines_return_validated_candles() -> None:
    adapter = build_adapter(default_handler())
    try:
        observation = await adapter.get_klines("BTCUSDT", Timeframe.H1, limit=6)
        assert observation.status is DataStatus.OK
        assert observation.value is not None
        assert len(observation.value.candles) == 6
        assert observation.value.timeframe is Timeframe.H1
    finally:
        await adapter.aclose()


async def test_old_candles_are_reported_stale() -> None:
    handler = RoutingHandler(
        {**default_routes(), endpoints.KLINES: json_route(payloads.stale_klines())}  # type: ignore[arg-type]
    )
    adapter = build_adapter(handler)
    try:
        observation = await adapter.get_klines("BTCUSDT", Timeframe.H1, limit=3)
        assert observation.status is DataStatus.STALE
        assert observation.value is None
    finally:
        await adapter.aclose()


async def test_empty_candle_series_is_a_valid_answer() -> None:
    handler = RoutingHandler(
        {**default_routes(), endpoints.KLINES: json_route([])}  # type: ignore[arg-type]
    )
    adapter = build_adapter(handler)
    try:
        observation = await adapter.get_klines("BTCUSDT", Timeframe.M5, limit=10)
        assert observation.status is DataStatus.OK
        assert observation.value is not None
        assert observation.value.candles == ()
    finally:
        await adapter.aclose()


async def test_kline_limit_is_bounded_to_the_venue_ceiling() -> None:
    """An absurd limit is clamped here rather than sent upstream to be rejected."""
    seen: dict[str, str] = {}

    def klines_route(request: httpx.Request) -> httpx.Response:
        seen.update(dict(request.url.params))
        return httpx.Response(200, json=payloads.klines(count=2))

    handler = RoutingHandler({**default_routes(), endpoints.KLINES: klines_route})  # type: ignore[arg-type]
    adapter = build_adapter(handler)
    try:
        await adapter.get_klines("BTCUSDT", Timeframe.H1, limit=10_000_000)
        assert seen["limit"] == "1500"
        assert seen["interval"] == "1h"
    finally:
        await adapter.aclose()


# ----------------------------------------------------------------------
# Upstream faults
# ----------------------------------------------------------------------


async def test_malformed_exchange_info_is_rejected() -> None:
    handler = RoutingHandler({endpoints.EXCHANGE_INFO: json_route({"nonsense": True})})
    adapter = build_adapter(handler)
    try:
        with pytest.raises(ExchangeInvalidResponseError):
            await adapter.get_exchange_info()
    finally:
        await adapter.aclose()


async def test_invalid_json_is_rejected() -> None:
    handler = RoutingHandler({endpoints.EXCHANGE_INFO: raw_route(b"<html>nginx</html>")})
    adapter = build_adapter(handler)
    try:
        with pytest.raises(ExchangeInvalidResponseError):
            await adapter.get_exchange_info()
    finally:
        await adapter.aclose()


async def test_rate_limiting_surfaces_its_own_error() -> None:
    handler = RoutingHandler({endpoints.EXCHANGE_INFO: status_route(429, {"Retry-After": "0"})})
    adapter = build_adapter(handler)
    try:
        with pytest.raises(ExchangeRateLimitedError):
            await adapter.get_exchange_info()
    finally:
        await adapter.aclose()


async def test_server_error_surfaces_as_unavailable() -> None:
    handler = RoutingHandler({endpoints.EXCHANGE_INFO: status_route(503)})
    adapter = build_adapter(handler)
    try:
        with pytest.raises(ExchangeUnavailableError):
            await adapter.get_exchange_info()
    finally:
        await adapter.aclose()


# ----------------------------------------------------------------------
# Connection status
# ----------------------------------------------------------------------


async def test_status_is_unknown_before_any_request() -> None:
    """Configuration alone is never evidence of a connection."""
    adapter = build_adapter(default_handler())
    try:
        status = await adapter.get_market_status()
        assert status.connection_status is ConnectionStatus.UNKNOWN
        assert status.last_success is None
        assert status.symbols_discovered is None
    finally:
        await adapter.aclose()


async def test_status_reports_connected_after_a_real_success() -> None:
    adapter = build_adapter(default_handler())
    try:
        await adapter.get_exchange_info()
        status = await adapter.get_market_status()
        assert status.connection_status is ConnectionStatus.CONNECTED
        assert status.last_success is not None
        assert status.symbols_discovered == 6
        assert status.eligible_symbols == 3
    finally:
        await adapter.aclose()


async def test_status_degrades_after_a_failure_following_success() -> None:
    handler = default_handler()
    adapter = build_adapter(handler)
    try:
        await adapter.get_exchange_info()
        handler.routes[endpoints.KLINES] = status_route(503)  # type: ignore[assignment]
        with pytest.raises(ExchangeUnavailableError):
            await adapter.get_klines("BTCUSDT", Timeframe.H1, limit=2)

        status = await adapter.get_market_status()
        assert status.connection_status is ConnectionStatus.DEGRADED
        assert status.last_error_code == "EXCHANGE_UNAVAILABLE"
    finally:
        await adapter.aclose()


async def test_status_does_not_trigger_an_upstream_call() -> None:
    handler = default_handler()
    adapter = build_adapter(handler)
    try:
        await adapter.get_market_status()
        assert handler.calls == []
    finally:
        await adapter.aclose()
