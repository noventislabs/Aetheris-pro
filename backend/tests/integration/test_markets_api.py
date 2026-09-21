"""HTTP contract for the read-only market-data endpoints."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from tests.fixtures import binance_payloads as payloads
from tests.fixtures.invariants import assert_route_surface
from tests.fixtures.transport import RoutingHandler, json_route, raw_route, status_route

from aetheris.adapters.exchange.binance import endpoints
from aetheris.core.config import Settings
from aetheris.main import create_app


def make_client(handler: RoutingHandler) -> Iterator[TestClient]:
    settings = Settings(
        environment="test",
        _env_file=None,  # type: ignore[call-arg]
    )
    settings.binance.max_retries = 0
    settings.binance.backoff_seconds = 0.0
    app = create_app(settings, exchange_transport=handler.transport())
    with TestClient(app) as client:
        yield client


@pytest.fixture
def handler() -> RoutingHandler:
    return RoutingHandler(
        {
            endpoints.EXCHANGE_INFO: json_route(payloads.exchange_info()),
            endpoints.TICKER_24H: json_route(payloads.ticker_24h()),
            endpoints.BOOK_TICKER: json_route(payloads.book_ticker()),
            endpoints.KLINES: json_route(payloads.klines(count=8)),
        }
    )


@pytest.fixture
def market_client(handler: RoutingHandler) -> Iterator[TestClient]:
    yield from make_client(handler)


# ----------------------------------------------------------------------
# Exchange info and symbols
# ----------------------------------------------------------------------


def test_exchange_info_reports_counts_without_dumping_every_symbol(
    market_client: TestClient,
) -> None:
    body = market_client.get("/api/v1/markets/exchange-info").json()
    assert body["exchange"] == "binance-futures-usdm"
    assert body["symbol_count"] == 6
    assert body["eligible_count"] == 3
    assert body["symbols"] == []  # opt-in, to keep the default response small


def test_exchange_info_can_include_symbols(market_client: TestClient) -> None:
    body = market_client.get(
        "/api/v1/markets/exchange-info", params={"include_symbols": True}
    ).json()
    assert len(body["symbols"]) == 6


def test_symbols_default_to_the_eligible_universe(market_client: TestClient) -> None:
    body = market_client.get("/api/v1/markets/symbols").json()
    assert body["eligible_only"] is True
    assert {s["symbol"] for s in body["symbols"]} == {"BTCUSDT", "ETHUSDT", "0GUSDT"}


def test_symbols_can_include_ineligible_instruments(market_client: TestClient) -> None:
    body = market_client.get("/api/v1/markets/symbols", params={"eligible_only": False}).json()
    assert body["count"] == 6


def test_symbol_detail_exposes_venue_filters(market_client: TestClient) -> None:
    body = market_client.get("/api/v1/markets/symbols/BTCUSDT").json()
    assert body["symbol"] == "BTCUSDT"
    assert body["filters"]["tick_size"] == "0.10"
    assert body["filters"]["step_size"] == "0.001"
    # Public data cannot supply leverage brackets, so it is null, not invented.
    assert body["max_leverage"] is None


def test_unknown_symbol_returns_the_error_envelope(market_client: TestClient) -> None:
    response = market_client.get("/api/v1/markets/symbols/NOPEUSDT")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "NOT_FOUND"


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("BTC", "BTCUSDT"),
        ("btc", "BTCUSDT"),
        ("BTCUSDT", "BTCUSDT"),
        ("0G", "0GUSDT"),
        ("ETH", "ETHUSDT"),
    ],
)
def test_symbol_search(market_client: TestClient, query: str, expected: str) -> None:
    body = market_client.get("/api/v1/markets/symbols", params={"search": query}).json()
    assert body["symbols"][0]["symbol"] == expected


def test_search_with_no_match_returns_empty_not_an_error(
    market_client: TestClient,
) -> None:
    body = market_client.get("/api/v1/markets/symbols", params={"search": "ZZZZZZ"}).json()
    assert body["count"] == 0


# ----------------------------------------------------------------------
# Ticker
# ----------------------------------------------------------------------


def test_ticker_response_carries_provenance(market_client: TestClient) -> None:
    body = market_client.get("/api/v1/markets/BTCUSDT/ticker").json()
    assert body["status"] == "OK"
    assert body["source"] == "binance-futures-usdm:rest"
    assert body["received_ts"] is not None
    assert body["event_ts"] is not None
    assert body["age_seconds"] is not None
    assert body["value"]["last_price"] == "60050.10"


def test_stale_ticker_returns_no_value(handler: RoutingHandler) -> None:
    handler.routes[endpoints.TICKER_24H] = json_route(payloads.ticker_24h(age_seconds=900))
    for client in make_client(handler):
        body = client.get("/api/v1/markets/BTCUSDT/ticker").json()
        assert body["status"] == "STALE"
        assert body["value"] is None
        assert body["detail"]


def test_ticker_for_unknown_symbol_is_404(market_client: TestClient) -> None:
    assert market_client.get("/api/v1/markets/FAKEUSDT/ticker").status_code == 404


def test_invalid_symbol_characters_are_rejected_before_any_call(
    market_client: TestClient,
) -> None:
    # Path traversal / injection attempts never reach the upstream query string.
    response = market_client.get("/api/v1/markets/BTC$USDT/ticker")
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_FAILED"


# ----------------------------------------------------------------------
# Klines
# ----------------------------------------------------------------------


def test_klines_return_candles_with_provenance(market_client: TestClient) -> None:
    body = market_client.get(
        "/api/v1/markets/BTCUSDT/klines", params={"interval": "1h", "limit": 8}
    ).json()
    assert body["status"] == "OK"
    assert body["value"]["timeframe"] == "1h"
    assert len(body["value"]["candles"]) == 8
    assert body["value"]["candles"][0]["open"] == "60000.0"


@pytest.mark.parametrize("interval", ["1m", "5m", "15m", "1h", "4h", "1d"])
def test_every_documented_interval_is_accepted(market_client: TestClient, interval: str) -> None:
    response = market_client.get(
        "/api/v1/markets/BTCUSDT/klines", params={"interval": interval, "limit": 3}
    )
    assert response.status_code == 200


def test_invalid_interval_is_rejected(market_client: TestClient) -> None:
    response = market_client.get("/api/v1/markets/BTCUSDT/klines", params={"interval": "7s"})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_FAILED"


@pytest.mark.parametrize("limit", [0, -1, 100_000_000])
def test_resource_exhausting_limits_are_rejected(market_client: TestClient, limit: int) -> None:
    response = market_client.get("/api/v1/markets/BTCUSDT/klines", params={"limit": limit})
    assert response.status_code == 422


def test_stale_candles_return_no_value(handler: RoutingHandler) -> None:
    handler.routes[endpoints.KLINES] = json_route(payloads.stale_klines())
    for client in make_client(handler):
        body = client.get("/api/v1/markets/BTCUSDT/klines", params={"interval": "1h"}).json()
        assert body["status"] == "STALE"
        assert body["value"] is None


# ----------------------------------------------------------------------
# Upstream faults surface as typed errors
# ----------------------------------------------------------------------


def test_exchange_unavailable_surfaces_as_503(handler: RoutingHandler) -> None:
    handler.routes[endpoints.EXCHANGE_INFO] = status_route(503)
    for client in make_client(handler):
        response = client.get("/api/v1/markets/symbols")
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "EXCHANGE_UNAVAILABLE"


def test_rate_limiting_surfaces_as_429(handler: RoutingHandler) -> None:
    handler.routes[endpoints.EXCHANGE_INFO] = status_route(429, {"Retry-After": "0"})
    for client in make_client(handler):
        response = client.get("/api/v1/markets/symbols")
        assert response.status_code == 429
        assert response.json()["error"]["code"] == "EXCHANGE_RATE_LIMITED"


def test_malformed_upstream_response_surfaces_as_502(handler: RoutingHandler) -> None:
    handler.routes[endpoints.EXCHANGE_INFO] = raw_route(b"<html>maintenance</html>")
    for client in make_client(handler):
        response = client.get("/api/v1/markets/symbols")
        assert response.status_code == 502
        assert response.json()["error"]["code"] == "EXCHANGE_INVALID_RESPONSE"


# ----------------------------------------------------------------------
# Status and capabilities
# ----------------------------------------------------------------------


def test_status_is_unknown_before_traffic(market_client: TestClient) -> None:
    body = market_client.get("/api/v1/markets/status").json()
    assert body["connection_status"] == "UNKNOWN"
    assert body["last_success"] is None


def test_status_becomes_connected_after_a_successful_call(
    market_client: TestClient,
) -> None:
    market_client.get("/api/v1/markets/symbols")
    body = market_client.get("/api/v1/markets/status").json()
    assert body["connection_status"] == "CONNECTED"
    assert body["symbols_discovered"] == 6
    assert body["eligible_symbols"] == 3
    assert body["source"] == "binance-futures-usdm:rest"


def test_capabilities_now_report_market_data_as_available(
    market_client: TestClient,
) -> None:
    capabilities = market_client.get("/api/v1/system/capabilities").json()["capabilities"]
    by_key = {c["key"]: c for c in capabilities}
    assert by_key["exchange.binance_futures"]["status"] == "AVAILABLE"
    assert by_key["market.symbol_discovery"]["status"] == "AVAILABLE"
    # Execution remains unclaimed.
    # PARTIAL since phase 8a: records are durable in PostgreSQL and a recovery
    # pass runs at startup. Still not AVAILABLE -- the retry columns the venue
    # path needs have no writer.
    assert by_key["order.persistence"]["status"] == "PARTIAL"
    # PARTIAL since phase 8b: signed testnet execution exists. Still not
    # AVAILABLE, and execution.live is still PLANNED and unimplemented.
    assert by_key["execution.testnet"]["status"] == "PARTIAL"
    assert by_key["execution.live"]["status"] == "PLANNED"
    assert by_key["execution.live"]["status"] == "PLANNED"


def test_no_venue_order_route_exists(market_client: TestClient) -> None:
    """The read-only guarantee, asserted at the HTTP surface.

    Narrowed twice now. Phase 6 added writes under /paper; phase 8b added
    /testnet, which does reach a venue. The list is exhaustive and spelled out,
    so a third order surface appearing anywhere is a failure rather than a
    surprise -- and the market-data routes still name none.
    """
    assert_route_surface(market_client)
    paths = market_client.get("/openapi.json").json()["paths"]
    assert sorted(p for p in paths if "order" in p) == [
        "/api/v1/paper/orders",
        "/api/v1/testnet/orders",
        "/api/v1/testnet/orders/{order_id}/cancel",
    ]


def test_forming_candle_reports_a_negative_age(market_client: TestClient) -> None:
    """The newest bar is still open, so its close time is in the future.

    Specified rather than accidental: the age is not clamped to zero, because
    zero would assert the bar had just closed. Clients wanting staleness should
    take max(age_seconds, 0).
    """
    body = market_client.get(
        "/api/v1/markets/BTCUSDT/klines", params={"interval": "1h", "limit": 3}
    ).json()
    assert body["status"] == "OK"
    assert body["age_seconds"] < 0


def test_venue_server_time_is_surfaced_but_not_used_for_freshness(
    handler: RoutingHandler,
) -> None:
    """A lagging serverTime must not make live data look stale.

    Observed against real Binance: exchangeInfo.serverTime ran ~1.5 days behind
    /fapi/v1/time. Freshness is judged from the data's own timestamps, so a
    stale serverTime changes nothing.
    """
    info = payloads.exchange_info()
    info["serverTime"] = payloads.millis(
        payloads.now() - timedelta(days=2)
    )  # venue reports a badly lagging clock
    handler.routes[endpoints.EXCHANGE_INFO] = json_route(info)

    for client in make_client(handler):
        assert client.get("/api/v1/markets/exchange-info").json()["symbol_count"] == 6
        ticker = client.get("/api/v1/markets/BTCUSDT/ticker").json()
        assert ticker["status"] == "OK"
        assert ticker["value"] is not None


def test_cors_advertises_only_the_verbs_that_exist(market_client: TestClient) -> None:
    """CORS must not offer verbs this build has no route for.

    POST arrives with the phase 6 paper routes and is advertised because those
    routes exist. PUT, PATCH and DELETE are not, because none does -- a
    permissive CORS policy written in advance of the endpoints is how a browser
    ends up able to reach something nobody meant to expose.
    """
    response = market_client.options(
        "/api/v1/markets/status",
        headers={
            "Origin": "http://localhost:3000",
            "Access-Control-Request-Method": "GET",
        },
    )
    allowed = response.headers.get("access-control-allow-methods", "")
    assert "GET" in allowed
    assert "POST" in allowed
    for verb in ("PUT", "PATCH", "DELETE"):
        assert verb not in allowed
