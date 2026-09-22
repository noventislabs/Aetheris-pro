"""MPI wired into the paper service: read, analyse, decide. Nothing else.

The safety property under test is negative, so it is tested by comparison
rather than by assertion on a return value: the whole account is snapshotted
before and after an evaluation, and anything that differs is a side effect.
That catches a mutation nobody thought to check for, which a field-by-field
assertion would not.
"""

from __future__ import annotations

from collections.abc import Iterator
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from tests.fixtures import binance_payloads as payloads
from tests.fixtures.transport import RoutingHandler, json_route

# The paper harness already wires per-symbol tickers, a book and venue
# filters exactly as the paper path reads them. Rebuilding that here would be
# a second copy that drifts the first time the service changes what it asks
# for, so it is imported instead.
from tests.integration.test_paper_api import make_client, routes

from aetheris.adapters.exchange.binance import endpoints
from aetheris.domain.intelligence import PositionDecision


@pytest.fixture
def handler() -> RoutingHandler:
    handler = routes()
    # Long enough for every indicator the brains read, and shaped so a
    # direction actually exists. The paper harness serves 60 flat bars, which
    # is right for order sizing and useless for a thesis.
    handler.routes[endpoints.KLINES] = json_route(payloads.trending_klines())
    return handler


@pytest.fixture
def client(handler: RoutingHandler) -> Iterator[TestClient]:
    yield from make_client(handler)


def service(client: TestClient):  # type: ignore[no-untyped-def]
    return client.app.state.paper_service  # type: ignore[attr-defined]


def open_a_position(client: TestClient) -> None:
    response = client.post(
        "/api/v1/paper/orders",
        json={
            "symbol": "BTCUSDT",
            "side": "BUY",
            "margin": "20",
            "stop_loss_percent": "2",
            "take_profit_percent": "4",
            "client_order_id": "mpi-fixture-1",
        },
    )
    assert response.status_code in {200, 201}, response.text
    assert client.get("/api/v1/paper/account").json()["positions"], "fixture must open one"


# ----------------------------------------------------------------------
# It runs
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_open_position_can_be_evaluated(client: TestClient) -> None:
    open_a_position(client)
    result = await service(client).position_intelligence("BTCUSDT")

    assert result is not None
    assert result.symbol == "BTCUSDT"
    assert result.position_id
    assert result.brain_results
    assert result.evaluated_at is not None


@pytest.mark.asyncio
async def test_no_position_returns_none_rather_than_a_verdict(client: TestClient) -> None:
    """Absent is not the same as unevaluable, and must not look like it."""
    assert await service(client).position_intelligence("ETHUSDT") is None


@pytest.mark.asyncio
async def test_the_symbol_is_matched_case_insensitively(client: TestClient) -> None:
    open_a_position(client)
    assert await service(client).position_intelligence("btcusdt") is not None


# ----------------------------------------------------------------------
# It changes nothing — the safety property
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_evaluating_mutates_no_account_state(client: TestClient) -> None:
    """Snapshot the whole account, evaluate, snapshot again, diff.

    Compared wholesale rather than field by field so a mutation nobody
    thought to check for still fails this.
    """
    open_a_position(client)
    before = client.get("/api/v1/paper/account").json()

    await service(client).position_intelligence("BTCUSDT")
    await service(client).position_intelligence("BTCUSDT")

    after = client.get("/api/v1/paper/account").json()
    # updated_at moves with the clock on every read, so it is not evidence of
    # a write; everything else must be identical.
    for payload in (before, after):
        payload.pop("as_of", None)
        for position in payload.get("positions", []):
            position.pop("updated_at", None)
    assert after == before


@pytest.mark.asyncio
async def test_evaluating_opens_no_order(client: TestClient) -> None:
    open_a_position(client)
    before = len(client.get("/api/v1/paper/account").json()["recent_orders"])
    await service(client).position_intelligence("BTCUSDT")
    after = len(client.get("/api/v1/paper/account").json()["recent_orders"])
    assert after == before


@pytest.mark.asyncio
async def test_evaluating_closes_nothing(client: TestClient) -> None:
    open_a_position(client)
    await service(client).position_intelligence("BTCUSDT")
    account = client.get("/api/v1/paper/account").json()
    assert len(account["positions"]) == 1
    assert account["recent_trades"] == []


@pytest.mark.asyncio
async def test_evaluating_changes_no_leverage_stop_or_margin(client: TestClient) -> None:
    open_a_position(client)
    before = client.get("/api/v1/paper/account").json()["positions"][0]
    await service(client).position_intelligence("BTCUSDT")
    after = client.get("/api/v1/paper/account").json()["positions"][0]

    for field in ("approved_leverage", "margin", "stop_price", "target_price", "quantity"):
        assert after[field] == before[field], f"{field} changed during a read-only evaluation"


@pytest.mark.asyncio
async def test_the_result_says_it_executes_nothing(client: TestClient) -> None:
    open_a_position(client)
    result = await service(client).position_intelligence("BTCUSDT")
    assert result is not None
    assert "places no order" in result.disclaimer
    assert result.partial_exit_available is False
    assert any("AUTONOMOUS_EXECUTION" in cap for cap in result.unavailable_capabilities)


# ----------------------------------------------------------------------
# Honest degradation
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unusable_candles_do_not_produce_a_cheerful_hold(
    client: TestClient, handler: RoutingHandler
) -> None:
    """A HOLD from data nobody could read is the dangerous answer."""
    open_a_position(client)
    handler.routes[endpoints.KLINES] = json_route(payloads.stale_klines())
    result = await service(client).position_intelligence("BTCUSDT")

    assert result is not None
    assert result.decision is PositionDecision.INSUFFICIENT_DATA
    assert result.decision is not PositionDecision.HOLD


@pytest.mark.asyncio
async def test_a_manual_position_cannot_be_judged(client: TestClient) -> None:
    """The fixture submits directly, so no rule set stood behind it.

    There are no original conditions to re-check, so alignment is absent
    rather than zero and the decision is INSUFFICIENT_DATA.
    """
    open_a_position(client)
    result = await service(client).position_intelligence("BTCUSDT")

    assert result is not None
    assert result.thesis_alignment is None
    assert result.decision is PositionDecision.INSUFFICIENT_DATA


@pytest.mark.asyncio
async def test_the_position_carries_the_thesis_it_was_opened_with(
    client: TestClient,
) -> None:
    open_a_position(client)
    position = client.get("/api/v1/paper/account").json()["positions"][0]
    thesis = position["thesis"]

    assert thesis is not None
    assert thesis["status"] == "NO_STRATEGY_CONTEXT"
    assert thesis["strategy"] is None
    assert Decimal(thesis["entry_price"]) > 0
    assert thesis["stop_price"] is not None


@pytest.mark.asyncio
async def test_excursions_accumulate_across_ticks(client: TestClient) -> None:
    """The metrics MPI reads are real accumulated state, not derived on demand."""
    open_a_position(client)
    client.post("/api/v1/paper/tick")
    position = client.get("/api/v1/paper/account").json()["positions"][0]
    assert position["best_price"] is not None
    assert position["worst_price"] is not None


# ----------------------------------------------------------------------
# Surface
# ----------------------------------------------------------------------


def test_no_intelligence_route_was_added(client: TestClient) -> None:
    """Phase E is service-level only. The API is a later phase, and adding a
    route here would be scope nobody asked for."""
    paths = client.get("/openapi.json").json()["paths"]
    assert not any("intelligence" in path for path in paths)
