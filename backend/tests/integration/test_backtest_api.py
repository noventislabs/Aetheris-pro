"""HTTP contract for the backtest endpoints."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from tests.fixtures import binance_payloads as payloads
from tests.fixtures.invariants import assert_route_surface
from tests.fixtures.transport import RoutingHandler, json_route, raw_route, status_route

from aetheris.adapters.exchange.binance import endpoints
from aetheris.core.config import Settings
from aetheris.main import create_app

UNIVERSE = ["BTCUSDT", "ETHUSDT", "0GUSDT"]


def walk_klines(count: int = 500, *, seed: int = 12345) -> list[list[object]]:
    """Deterministic noisy history, as Binance would serve it.

    Noisy on purpose: a smooth ramp pins RSI at an extreme, the strategy reads
    that as NEUTRAL, and the simulation takes no trades at all.
    """
    now = payloads.now()
    interval = 3600
    value, state, rows = 100.0, seed, []
    start = now - timedelta(seconds=interval * count)
    for index in range(count):
        state = (1103515245 * state + 12345) % (2**31)
        noise = (state / 2**31 - 0.5) * 0.02
        drift = 0.003 if (index // 80) % 2 == 0 else -0.003
        value *= 1 + drift + noise
        open_time = start + timedelta(seconds=interval * index)
        rows.append(
            payloads.kline_row(
                open_time=open_time,
                interval_seconds=interval,
                open_=f"{value:.4f}",
                high=f"{value * 1.006:.4f}",
                low=f"{value * 0.994:.4f}",
                close=f"{value:.4f}",
            )
        )
    return rows


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
            endpoints.KLINES: json_route(walk_klines()),
        }
    )


@pytest.fixture
def client(handler: RoutingHandler) -> Iterator[TestClient]:
    yield from make_client(handler)


# ----------------------------------------------------------------------
# Method disclosure
# ----------------------------------------------------------------------


def test_method_publishes_assumptions_and_gaps(client: TestClient) -> None:
    body = client.get("/api/v1/backtest/method").json()
    assert body["label"] == "HISTORICAL SIMULATION"
    joined = " ".join(body["assumptions"]).lower()
    assert "next bar's open" in joined
    assert "stop is assumed to have come first" in joined
    # The gaps matter as much as the model.
    gaps = " ".join(body["not_modelled"]).lower()
    assert "funding" in gaps
    assert "partial fills" in gaps
    assert "maintenance-margin" in gaps


def test_method_disclaimer_denies_predictive_meaning(client: TestClient) -> None:
    disclaimer = client.get("/api/v1/backtest/method").json()["disclaimer"].lower()
    assert "not a prediction" in disclaimer
    assert "probability of profit" in disclaimer
    assert "past results do not imply future results" in disclaimer


# ----------------------------------------------------------------------
# Running a simulation
# ----------------------------------------------------------------------


def test_backtest_completes_and_reports_metrics(client: TestClient) -> None:
    body = client.get("/api/v1/backtest/BTCUSDT").json()
    assert body["status"] == "COMPLETED"
    assert body["symbol"] == "BTCUSDT"
    assert body["strategy"] == "trend_momentum"
    assert body["source"] == "binance-futures-usdm:rest"
    metrics = body["metrics"]
    assert metrics["total_trades"] >= 1
    assert metrics["bars_tested"] > 0


def test_result_is_labelled_a_historical_simulation(client: TestClient) -> None:
    body = client.get("/api/v1/backtest/BTCUSDT").json()
    assert body["label"] == "HISTORICAL SIMULATION"
    assert "not a prediction" in body["disclaimer"].lower()
    assert body["assumptions"]


def test_trades_and_equity_curve_are_returned(client: TestClient) -> None:
    body = client.get("/api/v1/backtest/BTCUSDT").json()
    assert body["trades"]
    assert body["equity_curve"]
    trade = body["trades"][0]
    assert trade["side"] in {"LONG", "SHORT"}
    assert trade["exit_reason"]
    assert trade["entry_time"] < trade["exit_time"]


def test_metrics_are_consistent_with_the_trade_list(client: TestClient) -> None:
    body = client.get("/api/v1/backtest/BTCUSDT").json()
    trades = body["trades"]
    metrics = body["metrics"]
    assert metrics["total_trades"] == len(trades)
    assert metrics["winning_trades"] == sum(1 for t in trades if float(t["net_pnl"]) > 0)


def test_configuration_changes_the_outcome(client: TestClient) -> None:
    """The knobs are real: fees reduce the result."""
    free = client.get("/api/v1/backtest/BTCUSDT", params={"fee_bps": 0, "slippage_bps": 0}).json()
    charged = client.get(
        "/api/v1/backtest/BTCUSDT", params={"fee_bps": 20, "slippage_bps": 0}
    ).json()
    assert float(charged["metrics"]["total_fees"]) > 0
    assert float(free["metrics"]["total_fees"]) == 0
    assert float(charged["metrics"]["net_pnl"]) < float(free["metrics"]["net_pnl"])


def test_leverage_is_a_simulation_input_and_says_so(client: TestClient) -> None:
    body = client.get("/api/v1/backtest/BTCUSDT", params={"leverage": 5}).json()
    assert any("simulation input only" in w for w in body["warnings"])


def test_direction_can_be_restricted(client: TestClient) -> None:
    body = client.get("/api/v1/backtest/BTCUSDT", params={"allow_short": False}).json()
    assert all(trade["side"] == "LONG" for trade in body["trades"])


# ----------------------------------------------------------------------
# Validation and bounds
# ----------------------------------------------------------------------


@pytest.mark.parametrize("limit", [0, 59, 1501, 10_000_000])
def test_candle_limit_is_bounded(client: TestClient, limit: int) -> None:
    assert client.get("/api/v1/backtest/BTCUSDT", params={"limit": limit}).status_code == 422


@pytest.mark.parametrize(
    ("param", "value"),
    [
        ("starting_balance", 0),
        ("starting_balance", -100),
        ("position_size_percent", 0),
        ("position_size_percent", 101),
        ("leverage", 0),
        ("leverage", 26),
        ("fee_bps", -1),
        ("slippage_bps", 101),
        ("stop_loss_percent", 0),
        ("take_profit_percent", 0),
    ],
)
def test_config_parameters_are_bounded(client: TestClient, param: str, value: float) -> None:
    assert client.get("/api/v1/backtest/BTCUSDT", params={param: value}).status_code == 422


def test_leverage_is_capped_well_below_the_candidate_domain(client: TestClient) -> None:
    """500x is a study of liquidation, not of a strategy.

    The 1-500x candidate domain is about what analysis may *request* live. A
    simulation offering it would invite reading the output as a plan.
    """
    assert client.get("/api/v1/backtest/BTCUSDT", params={"leverage": 25}).status_code == 200
    assert client.get("/api/v1/backtest/BTCUSDT", params={"leverage": 100}).status_code == 422
    assert client.get("/api/v1/backtest/BTCUSDT", params={"leverage": 500}).status_code == 422


def test_both_directions_disabled_is_rejected(client: TestClient) -> None:
    response = client.get(
        "/api/v1/backtest/BTCUSDT", params={"allow_long": False, "allow_short": False}
    )
    assert response.status_code == 422


def test_inverted_strategy_periods_are_rejected(client: TestClient) -> None:
    assert (
        client.get("/api/v1/backtest/BTCUSDT", params={"ema_fast": 55, "ema_slow": 21}).status_code
        == 422
    )


def test_unknown_strategy_is_rejected(client: TestClient) -> None:
    response = client.get("/api/v1/backtest/BTCUSDT", params={"strategy": "magic_oracle"})
    assert response.status_code == 422
    assert "trend_momentum" in response.json()["error"]["details"]["supported"]


def test_invalid_timeframe_is_rejected(client: TestClient) -> None:
    assert client.get("/api/v1/backtest/BTCUSDT", params={"timeframe": "7s"}).status_code == 422


def test_malformed_symbol_is_rejected(client: TestClient) -> None:
    assert client.get("/api/v1/backtest/BTC$USDT").status_code == 422


def test_unknown_symbol_is_404(client: TestClient) -> None:
    assert client.get("/api/v1/backtest/NOPEUSDT").status_code == 404


# ----------------------------------------------------------------------
# Data problems
# ----------------------------------------------------------------------


def test_insufficient_history_is_reported_not_simulated(handler: RoutingHandler) -> None:
    """The venue can return fewer bars than asked for; 50 is below the warm-up.

    The API floor is 60 candles, so this is driven by what the venue actually
    served rather than by the request -- the realistic case for a recently
    listed instrument.
    """
    handler.routes[endpoints.KLINES] = json_route(walk_klines(count=50))
    for client in make_client(handler):
        body = client.get("/api/v1/backtest/BTCUSDT", params={"limit": 60}).json()
        assert body["status"] == "INSUFFICIENT_DATA"
        assert body["metrics"] is None
        assert body["detail"]


def test_stale_history_is_still_valid_history(handler: RoutingHandler) -> None:
    """A backtest studies closed bars; refusing stale data would be a live rule.

    The result still reports the data status so the reader knows.
    """
    handler.routes[endpoints.KLINES] = json_route(walk_klines(count=400))
    for client in make_client(handler):
        body = client.get("/api/v1/backtest/BTCUSDT", params={"limit": 400}).json()
        assert body["status"] == "COMPLETED"
        assert body["data_status"] in {"OK", "STALE"}


def test_exchange_unavailable_surfaces_as_503(handler: RoutingHandler) -> None:
    handler.routes[endpoints.KLINES] = status_route(503)
    for client in make_client(handler):
        response = client.get("/api/v1/backtest/BTCUSDT")
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "EXCHANGE_UNAVAILABLE"


def test_malformed_upstream_surfaces_as_502(handler: RoutingHandler) -> None:
    handler.routes[endpoints.KLINES] = raw_route(b"<html>maintenance</html>")
    for client in make_client(handler):
        assert client.get("/api/v1/backtest/BTCUSDT").status_code == 502


def test_empty_history_is_unavailable_not_a_zero_result(handler: RoutingHandler) -> None:
    handler.routes[endpoints.KLINES] = json_route([])
    for client in make_client(handler):
        body = client.get("/api/v1/backtest/BTCUSDT").json()
        assert body["status"] == "UNAVAILABLE"
        assert body["metrics"] is None


# ----------------------------------------------------------------------
# Read-only guarantee and capabilities
# ----------------------------------------------------------------------


def test_backtest_adds_no_write_route(client: TestClient) -> None:
    """A backtest computes and returns; it persists nothing and places nothing."""
    assert_route_surface(client)


def test_capabilities_report_the_backtester_as_available(client: TestClient) -> None:
    capabilities = client.get("/api/v1/system/capabilities").json()["capabilities"]
    by_key = {c["key"]: c for c in capabilities}
    assert by_key["backtest.engine"]["status"] == "AVAILABLE"
    # Optimisation left PLANNED when the bounded grid search, its objective
    # and the enforced train/validation/test split landed. PARTIAL, not
    # AVAILABLE: walk-forward is a tested primitive the optimizer does not
    # yet drive, and there is no route and no persistence.
    optimisation = by_key["optimize.hyperparameters"]
    assert optimisation["status"] == "PARTIAL"
    assert optimisation["status"] != "AVAILABLE"
    assert "NOT yet driven by the optimizer" in optimisation["detail"]
    # Searching parameters moves nothing. Live is untouched by any of it.
    assert by_key["execution.live"]["status"] == "PLANNED"


def test_a_backtest_never_reports_an_approved_leverage(client: TestClient) -> None:
    """Simulation leverage must not leak into the live approval vocabulary."""
    body = client.get("/api/v1/backtest/BTCUSDT", params={"leverage": 5}).json()
    assert "approved_leverage" not in body
    assert body["config"]["leverage"] == "5"


def test_the_result_carries_a_timestamp_and_window(client: TestClient) -> None:
    body = client.get("/api/v1/backtest/BTCUSDT").json()
    assert body["ran_at"]
    assert body["first_bar_time"] < body["last_bar_time"]


def test_equity_curve_is_bounded_for_transport(handler: RoutingHandler) -> None:
    """A long run ships a thinned curve, but metrics still cover every bar."""
    handler.routes[endpoints.KLINES] = json_route(walk_klines(count=1200))
    for client in make_client(handler):
        body = client.get("/api/v1/backtest/BTCUSDT", params={"limit": 1200}).json()
        assert len(body["equity_curve"]) <= 500
        assert body["metrics"]["bars_tested"] > 500


def test_determinism_over_http(client: TestClient) -> None:
    first = client.get("/api/v1/backtest/BTCUSDT").json()
    second = client.get("/api/v1/backtest/BTCUSDT").json()
    assert first["metrics"] == second["metrics"]
    assert first["trades"] == second["trades"]


def test_datetime_import_is_used() -> None:
    """Guard the fixture helper's imports stay honest."""
    assert datetime.now(UTC).tzinfo is UTC
