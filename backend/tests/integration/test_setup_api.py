"""HTTP contract for the setup-scoring endpoint.

The scoring arithmetic itself is covered in ``tests/unit/test_setup_engine.py``.
What is asserted here is the part only the route can get wrong: that the
engine is actually reached, that its refusals survive to the wire intact, that
the query parameters are real knobs rather than decoration, and that no field
in the payload claims to be a probability.
"""

from __future__ import annotations

from collections.abc import Iterator
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from tests.fixtures import binance_payloads as payloads
from tests.fixtures.invariants import assert_route_surface
from tests.fixtures.transport import RoutingHandler, json_route

from aetheris.adapters.exchange.binance import endpoints
from aetheris.core.config import Settings
from aetheris.main import create_app

UNIVERSE = ["BTCUSDT", "ETHUSDT"]


def make_client(handler: RoutingHandler) -> Iterator[TestClient]:
    settings = Settings(environment="test", _env_file=None)  # type: ignore[call-arg]
    settings.binance.max_retries = 0
    settings.binance.backoff_seconds = 0.0
    with TestClient(create_app(settings, exchange_transport=handler.transport())) as client:
        yield client


def routes(klines: list[list[object]]) -> RoutingHandler:
    return RoutingHandler(
        {
            endpoints.EXCHANGE_INFO: json_route(payloads.exchange_info()),
            endpoints.TICKER_24H: json_route(payloads.ticker_24h_list(UNIVERSE)),
            endpoints.BOOK_TICKER: json_route(payloads.book_ticker_list(UNIVERSE)),
            endpoints.KLINES: json_route(klines),
        }
    )


@pytest.fixture
def flat_client() -> Iterator[TestClient]:
    """Identical bars: no directional movement, so ADX never warms up."""
    yield from make_client(routes(payloads.klines(count=200)))


@pytest.fixture
def client() -> Iterator[TestClient]:
    """A series that actually produces a directional signal.

    ``klines()`` emits identical bars, which is right for transport and
    parsing but yields INSUFFICIENT_DATA here. A route test that only ever
    saw that shape would never exercise the scored path at all.
    """
    yield from make_client(routes(payloads.trending_klines()))


def setup_of(client: TestClient, query: str = "limit=300") -> dict[str, object]:
    response = client.get(f"/api/v1/analysis/BTCUSDT/setup?{query}")
    assert response.status_code == 200, response.text
    body: dict[str, object] = response.json()
    return body


# ----------------------------------------------------------------------
# The scored path
# ----------------------------------------------------------------------


def test_a_setup_is_scored_end_to_end(client: TestClient) -> None:
    """The whole chain: candles, indicators, rules, regime, levels, score."""
    body = setup_of(client)
    assert body["status"] == "ACTIONABLE"
    assert body["direction"] in {"LONG", "SHORT"}
    assert body["score"] is not None
    assert body["risk_reward"] is not None
    assert body["regime"] is not None
    assert body["strategy"] == "trend_momentum"
    assert body["strategy_version"]


def test_the_score_is_bounded_and_itemised_over_the_wire(client: TestClient) -> None:
    """The components must survive serialisation and still sum to the total."""
    score = setup_of(client)["score"]
    assert isinstance(score, dict)
    total = Decimal(str(score["value"]))
    assert Decimal(0) <= total <= Decimal(100)

    components = score["components"]
    assert isinstance(components, list)
    assert {c["name"] for c in components} == {
        "trend_alignment",
        "momentum_alignment",
        "regime_agreement",
        "risk_reward",
        "volatility_fitness",
        "volume_confirmation",
    }
    summed = sum(Decimal(str(c["contribution"])) for c in components)
    assert abs(summed - total) <= Decimal("0.01")
    for component in components:
        assert Decimal(0) <= Decimal(str(component["normalized"])) <= Decimal(1)
        assert component["detail"]


def test_every_component_carries_its_raw_measurement_or_says_it_has_none(
    client: TestClient,
) -> None:
    """A component without its measurement is a number to be trusted, not checked."""
    score = setup_of(client)["score"]
    assert isinstance(score, dict)
    for component in score["components"]:
        assert "raw_value" in component


def test_the_regime_is_reported_with_its_measurements(client: TestClient) -> None:
    regime = setup_of(client)["regime"]
    assert isinstance(regime, dict)
    assert regime["regime"] in {
        "TREND_UP",
        "TREND_DOWN",
        "RANGE",
        "HIGH_VOLATILITY",
        "LOW_VOLATILITY",
        "UNKNOWN",
    }
    assert regime["volatility_band"] in {"HIGH", "NORMAL", "LOW", "UNKNOWN"}
    assert regime["reason"]
    assert {m["name"] for m in regime["measurements"]} == {
        "trend_strength",
        "trend_direction",
        "volatility",
        "band_width",
    }


# ----------------------------------------------------------------------
# What the number is not
# ----------------------------------------------------------------------


def test_the_response_states_what_the_score_is_not(client: TestClient) -> None:
    """The disclaimer has to travel with the number, not sit in a docstring."""
    body = setup_of(client)
    score = body["score"]
    assert isinstance(score, dict)
    assert "NOT a probability" in str(score["meaning"])
    assert "not a probability of profit" in str(body["disclaimer"])
    assert "risk engine" in str(body["disclaimer"]).lower()


def test_no_field_in_the_payload_claims_a_probability(client: TestClient) -> None:
    """Asserted at the HTTP boundary, where a consumer would actually read it."""
    raw = client.get("/api/v1/analysis/BTCUSDT/setup?limit=300").text.lower()
    for banned in (
        '"profit_probability"',
        '"win_probability"',
        '"win_rate"',
        '"confidence"',
        '"expected_return"',
    ):
        assert banned not in raw


# ----------------------------------------------------------------------
# Levels
# ----------------------------------------------------------------------


def test_levels_are_real_and_ordered_for_the_reported_direction(
    client: TestClient,
) -> None:
    body = setup_of(client)
    rr = body["risk_reward"]
    assert isinstance(rr, dict)
    entry = Decimal(str(rr["entry_price"]))
    stop = Decimal(str(rr["stop_price"]))
    target = Decimal(str(rr["take_profit_price"]))
    if body["direction"] == "LONG":
        assert stop < entry < target
    else:
        assert target < entry < stop
    assert Decimal(str(rr["risk_per_unit"])) > 0
    assert Decimal(str(rr["reward_per_unit"])) > 0
    assert Decimal(str(rr["risk_reward_ratio"])) > 0


def test_the_entry_basis_is_stated_rather_than_implied(client: TestClient) -> None:
    """The realised fill will differ, and the payload must say so."""
    rr = setup_of(client)["risk_reward"]
    assert isinstance(rr, dict)
    assert "NEXT bar" in str(rr["entry_basis"])


def test_the_r_multiple_query_parameter_is_honoured(client: TestClient) -> None:
    """The knobs are real: a 3R request must produce a 3R target."""
    rr = setup_of(client, "limit=300&take_profit_r=3")["risk_reward"]
    assert isinstance(rr, dict)
    assert Decimal(str(rr["risk_reward_ratio"])) == Decimal(3)
    assert Decimal(str(rr["reward_per_unit"])) == Decimal(str(rr["risk_per_unit"])) * 3


def test_the_stop_model_query_parameter_changes_the_stop(client: TestClient) -> None:
    atr = setup_of(client, "limit=300&stop_model=ATR")["risk_reward"]
    fixed = setup_of(client, "limit=300&stop_model=FIXED_PERCENT&stop_percent=2")["risk_reward"]
    assert isinstance(atr, dict)
    assert isinstance(fixed, dict)
    assert atr["stop_model"] == "ATR"
    assert fixed["stop_model"] == "FIXED_PERCENT"
    assert Decimal(str(fixed["stop_distance_percent"])) == Decimal(2)
    assert atr["stop_price"] != fixed["stop_price"]


def test_an_out_of_range_parameter_is_rejected(client: TestClient) -> None:
    """Bounds are enforced at the boundary, not clamped silently."""
    assert client.get("/api/v1/analysis/BTCUSDT/setup?take_profit_r=0").status_code == 422
    assert client.get("/api/v1/analysis/BTCUSDT/setup?atr_multiple=50").status_code == 422
    assert client.get("/api/v1/analysis/BTCUSDT/setup?stop_percent=95").status_code == 422


# ----------------------------------------------------------------------
# Refusals
# ----------------------------------------------------------------------


def test_provenance_travels_with_every_setup(client: TestClient) -> None:
    """A setup that cannot be audited afterwards is not usable evidence."""
    body = setup_of(client)
    assert body["data_source"]
    assert body["data_status"] == "OK"
    assert body["data_age_seconds"] is not None
    assert body["evaluated_at"] is not None
    assert body["last_candle_time"] is not None
    assert isinstance(body["candles_used"], int) and body["candles_used"] > 0
    assert set(body["indicators_used"]) >= {"ema", "adx", "rsi", "macd"}  # type: ignore[arg-type]


def test_stale_candles_produce_no_setup() -> None:
    """Old bars are not a reason to trade, and must not yield a score."""
    handler = routes(payloads.stale_klines())
    for stale_client in make_client(handler):
        body = stale_client.get("/api/v1/analysis/BTCUSDT/setup").json()
        assert body["status"] == "STALE"
        assert body["direction"] == "NO_SIGNAL"
        assert body["score"] is None
        assert body["risk_reward"] is None
        # The refusal still carries provenance, so the reason is auditable.
        assert body["data_status"]
        assert body["evaluated_at"] is not None


def test_an_unwarmed_series_is_insufficient_not_neutral(flat_client: TestClient) -> None:
    """INSUFFICIENT_DATA and NO_ACTIONABLE_SETUP are different answers.

    A flat series has no directional movement, so ADX never warms up. That is
    "could not be measured", not "measured and found neutral".
    """
    body = flat_client.get("/api/v1/analysis/BTCUSDT/setup").json()
    assert body["status"] == "INSUFFICIENT_DATA"
    assert body["direction"] == "NO_SIGNAL"
    assert body["score"] is None
    assert "warmed up" in body["detail"]


def test_an_unknown_symbol_is_rejected(client: TestClient) -> None:
    assert client.get("/api/v1/analysis/NOTREAL/setup").status_code == 404


# ----------------------------------------------------------------------
# Surface
# ----------------------------------------------------------------------


def test_the_setup_route_is_read_only(client: TestClient) -> None:
    """Scoring changes nothing, so the route must not accept a write."""
    assert client.post("/api/v1/analysis/BTCUSDT/setup").status_code == 405
    assert_route_surface(client)


def test_the_route_does_not_reimplement_scoring() -> None:
    """The weights must live in exactly one place.

    A second copy inside the route would drift from the published ones the
    first time either changed, and the API would start quietly disagreeing
    with the engine it claims to expose.
    """
    import pathlib

    import aetheris

    route = (pathlib.Path(aetheris.__file__).parent / "api" / "v1" / "analysis.py").read_text(
        encoding="utf-8"
    )
    for leaked in ("WEIGHTS", "normalized", "contribution", "trend_alignment", "RR_REFERENCE"):
        assert leaked not in route, f"the route reimplements scoring: {leaked!r}"
