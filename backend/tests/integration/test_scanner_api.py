"""HTTP contract for the scanner endpoints."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from tests.fixtures import binance_payloads as payloads
from tests.fixtures.transport import RoutingHandler, json_route, raw_route, status_route

from aetheris.adapters.exchange.binance import endpoints
from aetheris.core.config import Settings
from aetheris.main import create_app

UNIVERSE = ["BTCUSDT", "ETHUSDT", "0GUSDT"]
VOLUMES = {"BTCUSDT": "900000", "ETHUSDT": "500000", "0GUSDT": "100000"}


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
            endpoints.TICKER_24H: json_route(
                payloads.ticker_24h_list(UNIVERSE, quote_volumes=VOLUMES)
            ),
            endpoints.BOOK_TICKER: json_route(payloads.book_ticker_list(UNIVERSE)),
            endpoints.KLINES: json_route(payloads.klines(count=40)),
        }
    )


@pytest.fixture
def client(handler: RoutingHandler) -> Iterator[TestClient]:
    yield from make_client(handler)


# ----------------------------------------------------------------------
# Happy path
# ----------------------------------------------------------------------


def test_scan_returns_rows_with_provenance(client: TestClient) -> None:
    body = client.get("/api/v1/scanner").json()
    assert body["universe_size"] == 3
    assert body["total_rows"] == 3
    assert body["ticker_source"] == "binance-futures-usdm:rest"
    assert body["ticker_status"] == "OK"
    assert body["ranking_scope"] == "FULL_UNIVERSE"
    assert body["scanned_at"]


def test_default_sort_is_by_quote_volume_descending(client: TestClient) -> None:
    body = client.get("/api/v1/scanner").json()
    assert [r["symbol"] for r in body["rows"]] == ["BTCUSDT", "ETHUSDT", "0GUSDT"]
    assert body["sort_by"] == "quote_volume_24h"
    assert body["direction"] == "desc"


def test_metrics_are_absent_unless_requested(client: TestClient) -> None:
    rows = client.get("/api/v1/scanner").json()["rows"]
    for row in rows:
        assert row["metrics_status"] == "NOT_REQUESTED"
        assert row["metrics"] is None
        assert row["opportunity"] is None


def test_metrics_and_score_when_requested(client: TestClient) -> None:
    rows = client.get("/api/v1/scanner", params={"include_metrics": True, "page_size": 2}).json()[
        "rows"
    ]
    for row in rows:
        assert row["metrics_status"] == "CALCULATED"
        assert row["metrics"]["candles_used"] >= 15
        score = row["opportunity"]
        assert 0 <= float(score["score"]) <= 100
        # Every score is explainable: four named components that sum to it.
        assert len(score["components"]) == 4
        assert sum(float(c["contribution"]) for c in score["components"]) == pytest.approx(
            float(score["score"]), abs=0.01
        )


# ----------------------------------------------------------------------
# Sorting
# ----------------------------------------------------------------------


@pytest.mark.parametrize("direction", ["asc", "desc"])
def test_sort_directions(client: TestClient, direction: str) -> None:
    body = client.get(
        "/api/v1/scanner", params={"sort": "quote_volume_24h", "direction": direction}
    ).json()
    symbols = [r["symbol"] for r in body["rows"]]
    expected = ["BTCUSDT", "ETHUSDT", "0GUSDT"]
    assert symbols == (expected if direction == "desc" else list(reversed(expected)))


def test_metric_sort_declares_the_bounded_pool(client: TestClient) -> None:
    body = client.get("/api/v1/scanner", params={"sort": "opportunity_score"}).json()
    assert body["ranking_scope"] == "LIQUIDITY_POOL"
    assert body["candidate_pool_size"] == 3


def test_invalid_sort_field_is_rejected(client: TestClient) -> None:
    response = client.get("/api/v1/scanner", params={"sort": "ai_confidence"})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_FAILED"


def test_invalid_direction_is_rejected(client: TestClient) -> None:
    assert client.get("/api/v1/scanner", params={"direction": "sideways"}).status_code == 422


# ----------------------------------------------------------------------
# Bounds — an unbounded scanner is a rate-limit ban
# ----------------------------------------------------------------------


@pytest.mark.parametrize("page_size", [0, -1, 101, 100_000])
def test_page_size_is_bounded(client: TestClient, page_size: int) -> None:
    assert client.get("/api/v1/scanner", params={"page_size": page_size}).status_code == 422


@pytest.mark.parametrize("page", [0, -5, 100_000])
def test_page_number_is_bounded(client: TestClient, page: int) -> None:
    assert client.get("/api/v1/scanner", params={"page": page}).status_code == 422


def test_search_length_is_bounded(client: TestClient) -> None:
    assert client.get("/api/v1/scanner", params={"search": "A" * 200}).status_code == 422


def test_quote_asset_pattern_rejects_injection(client: TestClient) -> None:
    assert client.get("/api/v1/scanner", params={"quote_asset": "USDT'; DROP--"}).status_code == 422


def test_invalid_timeframe_is_rejected(client: TestClient) -> None:
    assert client.get("/api/v1/scanner", params={"timeframe": "3s"}).status_code == 422


# ----------------------------------------------------------------------
# Search, filters, paging
# ----------------------------------------------------------------------


@pytest.mark.parametrize(("search", "expected"), [("BTC", "BTCUSDT"), ("0G", "0GUSDT")])
def test_search(client: TestClient, search: str, expected: str) -> None:
    body = client.get("/api/v1/scanner", params={"search": search}).json()
    assert [r["symbol"] for r in body["rows"]] == [expected]


def test_search_with_no_match_is_empty_not_an_error(client: TestClient) -> None:
    body = client.get("/api/v1/scanner", params={"search": "NOTHING"}).json()
    assert body["rows"] == []
    assert body["total_rows"] == 0
    # The universe is still reported, so an empty table is distinguishable
    # from a broken feed.
    assert body["universe_size"] == 3


def test_min_quote_volume_filter(client: TestClient) -> None:
    body = client.get("/api/v1/scanner", params={"min_quote_volume": "400000"}).json()
    assert {r["symbol"] for r in body["rows"]} == {"BTCUSDT", "ETHUSDT"}


def test_pagination(client: TestClient) -> None:
    first = client.get("/api/v1/scanner", params={"page": 1, "page_size": 2}).json()
    second = client.get("/api/v1/scanner", params={"page": 2, "page_size": 2}).json()
    assert len(first["rows"]) == 2
    assert len(second["rows"]) == 1
    assert first["total_rows"] == 3
    overlap = {r["symbol"] for r in first["rows"]} & {r["symbol"] for r in second["rows"]}
    assert not overlap


def test_page_past_the_end_is_empty(client: TestClient) -> None:
    body = client.get("/api/v1/scanner", params={"page": 50}).json()
    assert body["rows"] == []
    assert body["total_rows"] == 3


# ----------------------------------------------------------------------
# Upstream faults
# ----------------------------------------------------------------------


def test_exchange_unavailable_surfaces_as_503(handler: RoutingHandler) -> None:
    handler.routes[endpoints.EXCHANGE_INFO] = status_route(503)
    for client in make_client(handler):
        response = client.get("/api/v1/scanner")
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "EXCHANGE_UNAVAILABLE"


def test_rate_limited_surfaces_as_429(handler: RoutingHandler) -> None:
    handler.routes[endpoints.TICKER_24H] = status_route(429, {"Retry-After": "0"})
    for client in make_client(handler):
        response = client.get("/api/v1/scanner")
        assert response.status_code == 429
        assert response.json()["error"]["code"] == "EXCHANGE_RATE_LIMITED"


def test_malformed_upstream_surfaces_as_502(handler: RoutingHandler) -> None:
    handler.routes[endpoints.TICKER_24H] = raw_route(b"<html>maintenance</html>")
    for client in make_client(handler):
        response = client.get("/api/v1/scanner")
        assert response.status_code == 502
        assert response.json()["error"]["code"] == "EXCHANGE_INVALID_RESPONSE"


def test_stale_snapshot_returns_rows_without_prices(handler: RoutingHandler) -> None:
    handler.routes[endpoints.TICKER_24H] = json_route(
        payloads.ticker_24h_list(UNIVERSE, age_seconds=900)
    )
    for client in make_client(handler):
        body = client.get("/api/v1/scanner").json()
        assert body["ticker_status"] == "STALE"
        for row in body["rows"]:
            assert row["last_price"] is None
            assert row["ticker_detail"]


# ----------------------------------------------------------------------
# Scoring disclosure
# ----------------------------------------------------------------------


def test_scoring_method_is_published(client: TestClient) -> None:
    body = client.get("/api/v1/scanner/scoring-method").json()
    assert body["method"] == "market-opportunity/v1"
    assert sum(float(w["weight"]) for w in body["weights"]) == pytest.approx(1.0)
    assert body["min_candles"] == 15
    assert body["notes"]


def test_scoring_disclaimer_denies_predictive_meaning(client: TestClient) -> None:
    """The disclaimer is part of the contract, not decoration."""
    disclaimer = client.get("/api/v1/scanner/scoring-method").json()["disclaimer"].lower()
    assert "not a probability of profit" in disclaimer
    for forbidden_claim in ("expected return", "win rate", "prediction"):
        assert forbidden_claim in disclaimer


def test_capabilities_report_the_scanner_as_available(client: TestClient) -> None:
    capabilities = client.get("/api/v1/system/capabilities").json()["capabilities"]
    by_key = {c["key"]: c for c in capabilities}
    assert by_key["market.scanner"]["status"] == "AVAILABLE"
    assert by_key["analysis.scanner_metrics"]["status"] == "AVAILABLE"
    # Still unbuilt, still unclaimed.
    assert by_key["analysis.indicators"]["status"] == "PLANNED"
    assert by_key["analysis.smc"]["status"] == "PLANNED"
    assert by_key["ai.analysis"]["status"] == "PLANNED"
    assert by_key["paper.engine"]["status"] == "PLANNED"


def test_scanner_exposes_no_write_route(client: TestClient) -> None:
    paths = client.get("/openapi.json").json()["paths"]
    for path, operations in paths.items():
        assert set(operations) <= {"get"}, f"{path} exposes a non-GET method"
