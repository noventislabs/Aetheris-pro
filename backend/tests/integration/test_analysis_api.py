"""HTTP contract for the analysis endpoints."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from tests.fixtures import binance_payloads as payloads
from tests.fixtures.invariants import assert_route_surface
from tests.fixtures.transport import RoutingHandler, json_route, raw_route, status_route

from aetheris.adapters.exchange.binance import endpoints
from aetheris.core.config import Settings
from aetheris.main import create_app

UNIVERSE = ["BTCUSDT", "ETHUSDT", "0GUSDT"]


def make_client(handler: RoutingHandler) -> Iterator[TestClient]:
    settings = Settings(environment="test", _env_file=None)  # type: ignore[call-arg]
    settings.binance.max_retries = 0
    settings.binance.backoff_seconds = 0.0
    with TestClient(create_app(settings, exchange_transport=handler.transport())) as client:
        yield client


@pytest.fixture
def handler() -> RoutingHandler:
    return RoutingHandler(
        {
            endpoints.EXCHANGE_INFO: json_route(payloads.exchange_info()),
            endpoints.TICKER_24H: json_route(payloads.ticker_24h_list(UNIVERSE)),
            endpoints.BOOK_TICKER: json_route(payloads.book_ticker_list(UNIVERSE)),
            # Enough bars for every indicator, including ADX(14)'s 27.
            endpoints.KLINES: json_route(payloads.klines(count=200)),
        }
    )


@pytest.fixture
def client(handler: RoutingHandler) -> Iterator[TestClient]:
    yield from make_client(handler)


# ----------------------------------------------------------------------
# Catalogues
# ----------------------------------------------------------------------


def test_indicator_catalogue_lists_every_implemented_indicator(client: TestClient) -> None:
    body = client.get("/api/v1/analysis/indicators").json()
    keys = {entry["key"] for entry in body["indicators"]}
    assert keys == {
        "sma",
        "ema",
        "bollinger",
        "vwap",
        "rsi",
        "macd",
        "stochastic",
        "atr",
        "adx",
        "roc",
        "cci",
    }
    assert body["max_per_request"] == 8


def test_every_catalogued_indicator_states_its_convention(client: TestClient) -> None:
    """A published formula is checkable; an unlabelled number is not."""
    for entry in client.get("/api/v1/analysis/indicators").json()["indicators"]:
        assert entry["convention"], f"{entry['key']} publishes no convention"
        assert entry["kind"] in {"OVERLAY", "OSCILLATOR"}
        assert entry["value_keys"]


def test_strategy_catalogue_publishes_rules_and_disclaimer(client: TestClient) -> None:
    body = client.get("/api/v1/analysis/strategies").json()
    assert [s["key"] for s in body["strategies"]] == ["trend_momentum"]
    strategy = body["strategies"][0]
    assert strategy["available"] is True
    assert strategy["rules"]
    assert set(strategy["required_indicators"]) == {"ema", "adx", "rsi", "macd"}
    disclaimer = body["disclaimer"].lower()
    assert "not a trade recommendation" in disclaimer
    assert "probability of profit" in disclaimer


# ----------------------------------------------------------------------
# Indicator calculation
# ----------------------------------------------------------------------


def test_default_indicator_set_is_calculated(client: TestClient) -> None:
    body = client.get("/api/v1/analysis/BTCUSDT/indicators").json()
    assert body["symbol"] == "BTCUSDT"
    assert body["timeframe"] == "1h"
    assert body["source"] == "binance-futures-usdm:rest"
    assert body["data_status"] == "OK"
    assert [i["indicator"] for i in body["indicators"]] == ["ema", "rsi", "macd"]


def test_requested_indicators_are_returned_in_order(client: TestClient) -> None:
    body = client.get(
        "/api/v1/analysis/BTCUSDT/indicators", params={"indicators": "rsi,atr,adx"}
    ).json()
    assert [i["indicator"] for i in body["indicators"]] == ["rsi", "atr", "adx"]


def test_ready_indicators_carry_a_latest_value(client: TestClient) -> None:
    body = client.get("/api/v1/analysis/BTCUSDT/indicators", params={"indicators": "rsi"}).json()
    rsi = body["indicators"][0]
    assert rsi["status"] == "READY"
    assert rsi["latest"]["rsi"] is not None
    assert 0 <= float(rsi["latest"]["rsi"]) <= 100
    assert rsi["warmup_bars"] == 14


def test_parameters_are_echoed_back(client: TestClient) -> None:
    """A caller must be able to see which periods produced the numbers."""
    body = client.get(
        "/api/v1/analysis/BTCUSDT/indicators",
        params={"indicators": "rsi", "rsi_period": 21},
    ).json()
    assert body["indicators"][0]["parameters"]["rsi_period"] == "21"


def test_series_is_opt_in_and_bounded(client: TestClient) -> None:
    without = client.get("/api/v1/analysis/BTCUSDT/indicators", params={"indicators": "rsi"}).json()
    assert without["indicators"][0]["series"] == []

    with_series = client.get(
        "/api/v1/analysis/BTCUSDT/indicators",
        params={"indicators": "rsi", "series_points": 20},
    ).json()
    assert len(with_series["indicators"][0]["series"]) == 20


def test_single_indicator_route(client: TestClient) -> None:
    body = client.get("/api/v1/analysis/BTCUSDT/indicators/macd").json()
    assert [i["indicator"] for i in body["indicators"]] == ["macd"]
    assert set(body["indicators"][0]["value_keys"]) == {"macd", "signal", "histogram"}


def test_insufficient_candles_reports_rather_than_fabricates(
    handler: RoutingHandler,
) -> None:
    handler.routes[endpoints.KLINES] = json_route(payloads.klines(count=25))
    for client in make_client(handler):
        body = client.get(
            "/api/v1/analysis/BTCUSDT/indicators", params={"indicators": "adx", "limit": 25}
        ).json()
        adx = body["indicators"][0]
        assert adx["status"] == "INSUFFICIENT_DATA"
        assert adx["latest"] is None
        assert "27" in adx["detail"]


# ----------------------------------------------------------------------
# Validation and bounds
# ----------------------------------------------------------------------


def test_unknown_indicator_is_rejected_with_the_supported_list(client: TestClient) -> None:
    response = client.get("/api/v1/analysis/BTCUSDT/indicators", params={"indicators": "rsi,mcad"})
    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "VALIDATION_FAILED"
    assert "mcad" in error["message"]
    assert "rsi" in error["details"]["supported"]


def test_too_many_indicators_at_once_is_rejected(client: TestClient) -> None:
    every = "sma,ema,rsi,macd,atr,adx,roc,cci,vwap"  # nine > the cap of eight
    response = client.get("/api/v1/analysis/BTCUSDT/indicators", params={"indicators": every})
    assert response.status_code == 422
    assert "8 indicators" in response.json()["error"]["message"]


def test_empty_indicator_list_is_rejected(client: TestClient) -> None:
    assert (
        client.get(
            "/api/v1/analysis/BTCUSDT/indicators", params={"indicators": " , , "}
        ).status_code
        == 422
    )


def test_duplicate_indicators_are_charged_once(client: TestClient) -> None:
    body = client.get(
        "/api/v1/analysis/BTCUSDT/indicators", params={"indicators": "rsi,rsi,rsi"}
    ).json()
    assert [i["indicator"] for i in body["indicators"]] == ["rsi"]


@pytest.mark.parametrize("limit", [0, 19, 1001, 10_000_000])
def test_candle_limit_is_bounded(client: TestClient, limit: int) -> None:
    assert (
        client.get("/api/v1/analysis/BTCUSDT/indicators", params={"limit": limit}).status_code
        == 422
    )


@pytest.mark.parametrize("points", [-1, 501, 100_000])
def test_series_points_are_bounded(client: TestClient, points: int) -> None:
    assert (
        client.get(
            "/api/v1/analysis/BTCUSDT/indicators", params={"series_points": points}
        ).status_code
        == 422
    )


@pytest.mark.parametrize(
    ("param", "value"),
    [("rsi_period", 0), ("rsi_period", 5000), ("sma_period", 1), ("bb_deviations", 0)],
)
def test_indicator_parameters_are_bounded(client: TestClient, param: str, value: int) -> None:
    assert (
        client.get("/api/v1/analysis/BTCUSDT/indicators", params={param: value}).status_code == 422
    )


def test_macd_period_ordering_is_enforced(client: TestClient) -> None:
    assert (
        client.get(
            "/api/v1/analysis/BTCUSDT/indicators",
            params={"indicators": "macd", "macd_fast": 30, "macd_slow": 26},
        ).status_code
        == 422
    )


def test_invalid_timeframe_is_rejected(client: TestClient) -> None:
    assert (
        client.get("/api/v1/analysis/BTCUSDT/indicators", params={"timeframe": "7s"}).status_code
        == 422
    )


def test_malformed_symbol_is_rejected_before_any_upstream_call(
    client: TestClient,
) -> None:
    assert client.get("/api/v1/analysis/BTC$USDT/indicators").status_code == 422


def test_unknown_symbol_is_404(client: TestClient) -> None:
    assert client.get("/api/v1/analysis/NOPEUSDT/indicators").status_code == 404


def test_no_expression_injection_through_the_indicator_parameter(
    client: TestClient,
) -> None:
    """The indicator list is a whitelist, never a name to resolve."""
    for hostile in ("__import__", "eval", "os.system", "../../etc/passwd"):
        response = client.get("/api/v1/analysis/BTCUSDT/indicators", params={"indicators": hostile})
        assert response.status_code == 422


# ----------------------------------------------------------------------
# Upstream faults
# ----------------------------------------------------------------------


def test_stale_candles_yield_no_indicator_values(handler: RoutingHandler) -> None:
    handler.routes[endpoints.KLINES] = json_route(payloads.stale_klines())
    for client in make_client(handler):
        body = client.get(
            "/api/v1/analysis/BTCUSDT/indicators", params={"indicators": "rsi,macd"}
        ).json()
        assert body["data_status"] == "STALE"
        assert body["candle_count"] == 0
        for indicator in body["indicators"]:
            assert indicator["status"] == "UNAVAILABLE"
            assert indicator["latest"] is None
            assert indicator["detail"]


def test_exchange_unavailable_surfaces_as_503(handler: RoutingHandler) -> None:
    handler.routes[endpoints.KLINES] = status_route(503)
    for client in make_client(handler):
        response = client.get("/api/v1/analysis/BTCUSDT/indicators")
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "EXCHANGE_UNAVAILABLE"


def test_malformed_upstream_surfaces_as_502(handler: RoutingHandler) -> None:
    handler.routes[endpoints.KLINES] = raw_route(b"<html>maintenance</html>")
    for client in make_client(handler):
        response = client.get("/api/v1/analysis/BTCUSDT/indicators")
        assert response.status_code == 502


# ----------------------------------------------------------------------
# Strategy endpoint
# ----------------------------------------------------------------------


def test_strategy_returns_a_verdict_with_reasons(client: TestClient) -> None:
    body = client.get("/api/v1/analysis/BTCUSDT/strategy").json()
    assert body["strategy"] == "trend_momentum"
    assert body["status"] in {"READY", "INSUFFICIENT_DATA", "STALE"}
    if body["status"] == "READY":
        assert body["bias"] in {"LONG_BIAS", "SHORT_BIAS", "NEUTRAL"}
        assert len(body["long_conditions"]) == 4
        assert len(body["short_conditions"]) == 4
        for condition in body["long_conditions"]:
            assert condition["detail"]


def test_strategy_result_always_carries_the_disclaimer(client: TestClient) -> None:
    disclaimer = client.get("/api/v1/analysis/BTCUSDT/strategy").json()["disclaimer"]
    assert "Analysis only" in disclaimer
    assert "No order is placed" in disclaimer


def test_strategy_exposes_no_confidence_field(client: TestClient) -> None:
    body = client.get("/api/v1/analysis/BTCUSDT/strategy").json()
    for forbidden in ("confidence", "probability", "win_rate", "expected_return"):
        assert forbidden not in body


def test_unknown_strategy_is_rejected(client: TestClient) -> None:
    response = client.get("/api/v1/analysis/BTCUSDT/strategy", params={"strategy": "magic_oracle"})
    assert response.status_code == 422
    assert "trend_momentum" in response.json()["error"]["details"]["supported"]


def test_strategy_parameters_are_validated(client: TestClient) -> None:
    assert (
        client.get(
            "/api/v1/analysis/BTCUSDT/strategy",
            params={"ema_fast": 55, "ema_slow": 21},
        ).status_code
        == 422
    )


def test_strategy_on_stale_data_reports_no_bias(handler: RoutingHandler) -> None:
    handler.routes[endpoints.KLINES] = json_route(payloads.stale_klines())
    for client in make_client(handler):
        body = client.get("/api/v1/analysis/BTCUSDT/strategy").json()
        assert body["status"] == "STALE"
        assert body["bias"] is None


# ----------------------------------------------------------------------
# Read-only guarantee
# ----------------------------------------------------------------------


def test_analysis_adds_no_write_route(client: TestClient) -> None:
    """Analysis stays a read. The whole surface is checked, not just its slice."""
    assert_route_surface(client)


def test_capabilities_report_indicators_and_strategy_as_available(
    client: TestClient,
) -> None:
    capabilities = client.get("/api/v1/system/capabilities").json()["capabilities"]
    by_key = {c["key"]: c for c in capabilities}
    assert by_key["analysis.indicators"]["status"] == "AVAILABLE"
    assert by_key["strategy.engine"]["status"] == "AVAILABLE"
    # Still unbuilt, still unclaimed.
    assert by_key["analysis.smc"]["status"] == "PLANNED"
    # Backtesting shipped in phase 5, in the commit that landed its tests.
    assert by_key["backtest.engine"]["status"] == "AVAILABLE"
    # Paper trading shipped in phase 6; real execution still has not.
    assert by_key["paper.engine"]["status"] == "AVAILABLE"
    assert by_key["execution.testnet"]["status"] == "PLANNED"
    assert by_key["ai.analysis"]["status"] == "PLANNED"
    assert by_key["execution.live"]["status"] == "PLANNED"
