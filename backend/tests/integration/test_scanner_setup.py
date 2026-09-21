"""Scanner integration for strategy setup scoring.

Two scores now travel on a scanner row and they answer different questions.
Most of what is asserted here is that they stay apart: separate fields,
separate methods, separate meanings, and no wording anywhere that turns
either of them into a probability.

The rest is scope honesty. Setup scoring reaches fewer instruments than
metrics do, and the page has to say so rather than let a slice of the market
read as the whole of it.
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

UNIVERSE = ["BTCUSDT", "ETHUSDT", "0GUSDT"]


@pytest.fixture
def handler() -> RoutingHandler:
    return RoutingHandler(
        {
            endpoints.EXCHANGE_INFO: json_route(payloads.exchange_info()),
            endpoints.TICKER_24H: json_route(payloads.ticker_24h_list(UNIVERSE)),
            endpoints.BOOK_TICKER: json_route(payloads.book_ticker_list(UNIVERSE)),
            endpoints.KLINES: json_route(payloads.trending_klines()),
        }
    )


@pytest.fixture
def client(handler: RoutingHandler) -> Iterator[TestClient]:
    settings = Settings(environment="test", _env_file=None)  # type: ignore[call-arg]
    settings.binance.max_retries = 0
    settings.binance.backoff_seconds = 0.0
    with TestClient(create_app(settings, exchange_transport=handler.transport())) as client:
        yield client


def scan(client: TestClient, query: str = "") -> dict[str, object]:
    response = client.get(f"/api/v1/scanner?{query}")
    assert response.status_code == 200, response.text
    body: dict[str, object] = response.json()
    return body


# ----------------------------------------------------------------------
# Opt-in
# ----------------------------------------------------------------------


def test_setups_are_off_by_default(client: TestClient) -> None:
    """Scoring costs a longer candle window, so nobody pays for it unasked."""
    body = scan(client)
    assert body["ranking_scope"] == "FULL_UNIVERSE"
    assert body["setup_pool_size"] is None
    for row in body["rows"]:  # type: ignore[attr-defined]
        assert row["setup_status"] == "NOT_REQUESTED"
        assert row["setup"] is None


def test_asking_for_setups_scores_the_rows(client: TestClient) -> None:
    body = scan(client, "include_setup=true")
    assert body["ranking_scope"] == "STRATEGY_POOL"
    assert body["setup_pool_size"] == len(UNIVERSE)
    for row in body["rows"]:  # type: ignore[attr-defined]
        assert row["setup_status"] == "CALCULATED"
        assert row["setup"] is not None
        assert row["setup"]["strategy"] == "trend_momentum"


def test_sorting_by_setup_score_implies_scoring(client: TestClient) -> None:
    """A caller must not have to ask for the same thing twice."""
    body = scan(client, "sort=setup_score")
    assert body["ranking_scope"] == "STRATEGY_POOL"
    assert all(row["setup"] is not None for row in body["rows"])  # type: ignore[attr-defined]


# ----------------------------------------------------------------------
# Scope honesty
# ----------------------------------------------------------------------


def test_the_page_names_the_pool_rather_than_implying_the_market(
    client: TestClient,
) -> None:
    """A subset ranked is never reported as the whole universe ranked."""
    body = scan(client, "include_setup=true")
    assert body["ranking_scope"] == "STRATEGY_POOL"
    assert body["ranking_scope"] != "FULL_UNIVERSE"
    assert isinstance(body["setup_pool_size"], int)
    assert body["setup_pool_size"] <= body["universe_size"]  # type: ignore[operator]


def test_the_setup_pool_is_bounded_by_configuration(client: TestClient) -> None:
    """The bound is a real ceiling, not a comment.

    Ranking every perpetual this way is not a slow option; on the target
    hardware it is a rate-limit ban.
    """
    from aetheris.core.config import ScannerSettings

    settings = ScannerSettings()
    assert settings.setup_pool_size <= settings.max_metric_symbols
    assert settings.setup_pool_size <= 60
    body = scan(client, "include_setup=true")
    assert body["setup_pool_size"] <= settings.setup_pool_size  # type: ignore[operator]


def test_the_longer_window_is_enough_for_every_indicator(client: TestClient) -> None:
    """60 metric bars cannot warm up EMA(55). The setup window must.

    If this regresses, every scored row silently becomes INSUFFICIENT_DATA
    and the feature looks broken rather than misconfigured.
    """
    from aetheris.analysis.strategies.trend_momentum import TrendMomentumParams, warmup_bars
    from aetheris.core.config import ScannerSettings

    settings = ScannerSettings()
    assert settings.setup_candle_limit > warmup_bars(TrendMomentumParams())
    body = scan(client, "include_setup=true")
    statuses = {row["setup"]["status"] for row in body["rows"]}  # type: ignore[index]
    assert "INSUFFICIENT_DATA" not in statuses


# ----------------------------------------------------------------------
# The two scores stay apart
# ----------------------------------------------------------------------


def test_both_scores_are_reported_and_are_not_the_same_number(
    client: TestClient,
) -> None:
    """They measure different things, so conflating them would be a lie."""
    body = scan(client, "include_setup=true")
    row = body["rows"][0]  # type: ignore[index]
    opportunity = row["opportunity"]
    setup = row["setup"]["score"]
    assert opportunity is not None
    assert setup is not None
    assert opportunity["method"] != setup["method"]
    assert opportunity["method"].startswith("market-opportunity/")
    assert setup["method"].startswith("setup-score/")


def test_the_opportunity_score_still_works_on_its_own(client: TestClient) -> None:
    """The existing metric must survive the addition untouched."""
    body = scan(client, "sort=opportunity_score")
    assert body["ranking_scope"] == "LIQUIDITY_POOL"
    assert body["setup_pool_size"] is None
    for row in body["rows"]:  # type: ignore[attr-defined]
        assert row["opportunity"] is not None
        assert row["setup"] is None


def test_neither_score_is_named_as_a_probability(client: TestClient) -> None:
    """Checked on the wire, where a consumer would actually read it."""
    raw = client.get("/api/v1/scanner?include_setup=true").text.lower()
    for banned in (
        '"profit_probability"',
        '"win_probability"',
        '"win_rate"',
        '"confidence"',
        '"expected_return"',
        '"prediction"',
    ):
        assert banned not in raw


def test_the_setup_score_carries_its_own_disclaimer(client: TestClient) -> None:
    body = scan(client, "include_setup=true")
    setup = body["rows"][0]["setup"]  # type: ignore[index]
    assert "NOT a probability" in setup["score"]["meaning"]
    assert "not a probability of profit" in setup["disclaimer"]


# ----------------------------------------------------------------------
# One fetch, and honest degradation
# ----------------------------------------------------------------------


def test_metrics_and_setups_come_from_a_single_candle_fetch(
    client: TestClient, handler: RoutingHandler
) -> None:
    """Fetching twice would double the request count for no new information.

    It could also disagree with itself if the two calls straddled a bar close,
    which is the harder failure to notice.
    """
    before = handler.count(endpoints.KLINES)
    body = scan(client, "include_setup=true")
    after = handler.count(endpoints.KLINES)
    scored = [r for r in body["rows"] if r["setup"] is not None]  # type: ignore[attr-defined]
    assert scored
    # One request per scored instrument, not two.
    assert after - before == len(scored)
    for row in scored:
        assert row["metrics"] is not None
        assert row["opportunity"] is not None


def test_stale_candles_produce_a_stale_setup_not_a_missing_one(
    client: TestClient, handler: RoutingHandler
) -> None:
    """The reason has to survive onto the row, not become an unexplained null."""
    handler.routes[endpoints.KLINES] = json_route(payloads.stale_klines())
    body = scan(client, "include_setup=true")
    for row in body["rows"]:  # type: ignore[attr-defined]
        assert row["setup_status"] in {"CALCULATED", "UNAVAILABLE"}
        if row["setup"] is not None:
            assert row["setup"]["status"] in {"STALE", "INSUFFICIENT_DATA"}
            assert row["setup"]["score"] is None


def test_a_row_without_a_score_sorts_last_rather_than_as_zero(
    client: TestClient, handler: RoutingHandler
) -> None:
    """Coercing an unknown to zero fabricates a comparison."""
    handler.routes[endpoints.KLINES] = json_route(payloads.stale_klines())
    body = scan(client, "sort=setup_score&direction=desc")
    scores = [
        (row["setup"] or {}).get("score")
        for row in body["rows"]  # type: ignore[attr-defined]
    ]
    assert all(score is None for score in scores)
    # Unscored rows stay in the page and keep a stable symbol ordering.
    symbols = [row["symbol"] for row in body["rows"]]  # type: ignore[attr-defined]
    assert symbols == sorted(symbols)


def test_ranking_by_setup_score_is_actually_ordered(client: TestClient) -> None:
    body = scan(client, "sort=setup_score&direction=desc")
    values = [
        Decimal(str(row["setup"]["score"]["value"]))  # type: ignore[index]
        for row in body["rows"]  # type: ignore[attr-defined]
        if row["setup"] and row["setup"]["score"]
    ]
    assert values == sorted(values, reverse=True)


# ----------------------------------------------------------------------
# Surface
# ----------------------------------------------------------------------


def test_the_scanner_remains_read_only(client: TestClient) -> None:
    assert client.post("/api/v1/scanner").status_code == 405
    assert_route_surface(client)


def test_setup_score_is_a_published_sort_option(client: TestClient) -> None:
    """Discoverable from the schema, and an unknown field is still a 422."""
    schema = client.get("/openapi.json").json()
    params = schema["paths"]["/api/v1/scanner"]["get"]["parameters"]
    sort_param = next(p for p in params if p["name"] == "sort")
    enum = sort_param["schema"].get("enum") or sort_param["schema"]["$ref"]
    if isinstance(enum, list):
        assert "setup_score" in enum
    assert client.get("/api/v1/scanner?sort=not_a_field").status_code == 422
