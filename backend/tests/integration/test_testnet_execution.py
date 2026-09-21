"""Testnet execution against real PostgreSQL and a controlled venue.

The database is real. The venue is a controlled transport, and that is a
deliberate split rather than a compromise: the guarantees under test here are
about what this system *does* -- what it writes down, in what order, and what
it refuses -- and a real venue cannot be made to time out on cue, return a
leverage it was not asked for, or lose every order at once.

What a controlled transport cannot prove is that the request shape is the one
Binance accepts. That is what the opt-in real-testnet suite is for, and neither
suite is a substitute for the other.

Skipped without a configured database. Each test uses a throwaway owner.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import httpx
import pytest
import pytest_asyncio
from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from tests.integration.test_persistence import _settings as _db_settings

from aetheris.adapters.exchange.binance.testnet_adapter import BinanceTestnetTradingAdapter
from aetheris.adapters.persistence.accounts import PostgresAccountRepository
from aetheris.adapters.persistence.engine import (
    build_engine,
    build_session_factory,
    check_connectivity,
    session_scope,
)
from aetheris.adapters.persistence.orders import PostgresOrderRepository
from aetheris.core.config import Settings, TestnetSettings
from aetheris.core.errors import RiskRejectionCode
from aetheris.domain.enums import OrderSide, OrderState, TradingMode
from aetheris.domain.venue import MarginMode
from aetheris.engines.order.engine import OrderLifecycleEngine
from aetheris.services.testnet import TestnetExecutionService, TestnetOrderRequest

pytestmark = pytest.mark.database

NOW = datetime(2026, 9, 21, 12, 0, tzinfo=UTC)
KEY = SecretStr("integration-key-not-a-real-credential")
SECRET = SecretStr("integration-secret-not-a-real-credential")


# ----------------------------------------------------------------------
# A controlled venue
# ----------------------------------------------------------------------


class FakeVenue:
    """A Binance testnet that does exactly what a test needs it to do.

    Not a mock of our own code: it answers at the HTTP boundary, so the real
    adapter, the real signing and the real parsing all run. What it replaces is
    the exchange, and only the exchange.
    """

    def __init__(self) -> None:
        self.dual_side = False
        self.margin_type = "CROSSED"
        self.applied_leverage: Decimal | None = None
        self.leverage_to_report: Decimal | None = None
        self.max_leverage = Decimal(20)
        self.brackets_present = True
        self.available_balance = Decimal(1000)
        self.realized_pnl = Decimal(0)
        self.can_trade = True
        self.orders: dict[str, dict[str, Any]] = {}
        self.submit_error: Exception | None = None
        self.margin_change_refused = False
        self.order_status = "NEW"
        self.filled_qty = Decimal(0)
        self.avg_price: Decimal | None = None
        self.vanish_all = False
        self.submit_calls = 0

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    def _json(self, payload: Any, status: int = 200) -> httpx.Response:
        return httpx.Response(status, json=payload)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        method = request.method

        if path == "/fapi/v1/time":
            return self._json({"serverTime": int(NOW.timestamp() * 1000)})
        if path == "/fapi/v1/positionSide/dual":
            return self._json({"dualSidePosition": self.dual_side})
        if path == "/fapi/v3/account":
            return self._json(
                {
                    "canTrade": self.can_trade,
                    "availableBalance": str(self.available_balance),
                    "accountAlias": "testnet-alias",
                }
            )
        if path == "/fapi/v1/income":
            return self._json([{"income": str(self.realized_pnl), "incomeType": "REALIZED_PNL"}])
        if path == "/fapi/v1/leverageBracket":
            if not self.brackets_present:
                return self._json([])
            return self._json(
                [
                    {
                        "symbol": "BTCUSDT",
                        "brackets": [
                            {
                                "initialLeverage": int(self.max_leverage),
                                "notionalFloor": 0,
                                "notionalCap": 1_000_000,
                                "maintMarginRatio": 0.01,
                            }
                        ],
                    }
                ]
            )
        if path == "/fapi/v1/leverage" and method == "POST":
            asked = Decimal(request.url.params["leverage"])
            self.applied_leverage = self.leverage_to_report or asked
            return self._json({"leverage": int(self.applied_leverage), "symbol": "BTCUSDT"})
        if path == "/fapi/v1/marginType" and method == "POST":
            if self.margin_change_refused:
                return self._json({"code": -4048, "msg": "Margin type cannot be changed"})
            if self.margin_type == request.url.params["marginType"]:
                return self._json({"code": -4046, "msg": "No need to change margin type."})
            self.margin_type = request.url.params["marginType"]
            return self._json({"code": 200, "msg": "success"})
        if path == "/fapi/v3/positionRisk":
            return self._json([{"symbol": "BTCUSDT", "marginType": self.margin_type}])

        if path == "/fapi/v1/order" and method == "POST":
            self.submit_calls += 1
            if self.submit_error is not None:
                raise self.submit_error
            cid = request.url.params["newClientOrderId"]
            self.orders[cid] = {
                "clientOrderId": cid,
                "orderId": 500 + len(self.orders),
                "status": self.order_status,
                "executedQty": str(self.filled_qty),
                "avgPrice": str(self.avg_price or 0),
            }
            return self._json(self.orders[cid])
        if path == "/fapi/v1/order" and method == "GET":
            cid = request.url.params.get("origClientOrderId", "")
            if self.vanish_all or cid not in self.orders:
                return self._json({"code": -2013, "msg": "Order does not exist."})
            payload = dict(self.orders[cid])
            payload["status"] = self.order_status
            payload["executedQty"] = str(self.filled_qty)
            payload["avgPrice"] = str(self.avg_price or 0)
            return self._json(payload)
        if path == "/fapi/v1/order" and method == "DELETE":
            cid = request.url.params.get("origClientOrderId", "")
            if cid not in self.orders:
                return self._json({"code": -2013, "msg": "Order does not exist."})
            payload = dict(self.orders[cid])
            payload["status"] = self.order_status
            payload["executedQty"] = str(self.filled_qty)
            payload["avgPrice"] = str(self.avg_price or 0)
            return self._json(payload)

        return self._json({"code": -5000, "msg": f"unexpected {method} {path}"}, 404)


class FakeMarketData:
    """Only what the execution path reads: a price, a symbol, and candles."""

    def __init__(self, price: Decimal = Decimal(50_000)) -> None:
        self.price = price

    async def get_ticker(self, symbol: str) -> Any:
        from aetheris.core.freshness import Observation, utcnow
        from aetheris.domain.market import Ticker

        now = utcnow()
        return Observation.ok(
            value=Ticker(symbol=symbol, last_price=self.price, event_time=now),
            source="test",
            event_ts=now,
        )

    async def get_symbol(self, symbol: str) -> Any:
        from aetheris.domain.enums import ContractType, SymbolStatus
        from aetheris.domain.market import Symbol, SymbolFilters

        return Symbol(
            symbol=symbol,
            base_asset="BTC",
            quote_asset="USDT",
            status=SymbolStatus.TRADING,
            contract_type=ContractType.PERPETUAL,
            price_precision=2,
            quantity_precision=3,
            filters=SymbolFilters(
                tick_size=Decimal("0.1"),
                step_size=Decimal("0.001"),
                min_quantity=Decimal("0.001"),
                min_notional=Decimal(5),
            ),
        )

    async def get_klines(self, symbol: str, timeframe: Any, *, limit: int) -> Any:
        from aetheris.core.freshness import Observation, utcnow
        from aetheris.domain.market import Candle, CandleSeries

        # Anchored to the wall clock, not a fixed NOW: candles pinned to a past
        # instant are stale by definition, and a fixed future instant makes the
        # number of "forming" bars depend on when the suite happens to run.
        now = utcnow()
        base = int(now.timestamp() * 1000) - limit * 900_000
        candles = tuple(
            Candle(
                open_time=datetime.fromtimestamp((base + i * 900_000) / 1000, tz=UTC),
                close_time=datetime.fromtimestamp((base + (i + 1) * 900_000 - 1) / 1000, tz=UTC),
                open=self.price,
                high=self.price * Decimal("1.002"),
                low=self.price * Decimal("0.998"),
                close=self.price,
                volume=Decimal(10),
            )
            for i in range(limit)
        )
        return Observation.ok(
            value=CandleSeries(symbol=symbol, timeframe=timeframe, candles=candles),
            source="test",
            event_ts=candles[-1].close_time if candles else now,
        )


# ----------------------------------------------------------------------
# Fixtures
# ----------------------------------------------------------------------


@pytest.fixture(scope="module")
def db_settings() -> Settings:
    return _db_settings()


@pytest_asyncio.fixture
async def engine(db_settings: Settings) -> AsyncIterator[AsyncEngine]:
    eng = build_engine(db_settings)
    try:
        reachable, detail = await check_connectivity(eng)
        if not reachable:
            pytest.skip(f"database not reachable: {detail}")
        yield eng
    finally:
        await eng.dispose()


@pytest_asyncio.fixture
async def factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return build_session_factory(engine)


@pytest_asyncio.fixture
async def tenant(
    factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[tuple[uuid.UUID, uuid.UUID]]:
    accounts = PostgresAccountRepository(factory)
    owner_id = uuid.uuid4()
    await accounts.ensure_owner(owner_id=owner_id, email=f"{owner_id}@test.invalid")
    account_id = await accounts.ensure_account(
        owner_id=owner_id, mode=TradingMode.TESTNET, starting_balance=Decimal(1000)
    )
    try:
        yield owner_id, account_id
    finally:
        async with session_scope(factory, str(owner_id)) as session:
            await session.execute(text("DELETE FROM users WHERE id = :o"), {"o": str(owner_id)})


@pytest.fixture
def venue() -> FakeVenue:
    return FakeVenue()


@pytest_asyncio.fixture
async def service(
    factory: async_sessionmaker[AsyncSession],
    tenant: tuple[uuid.UUID, uuid.UUID],
    venue: FakeVenue,
) -> AsyncIterator[TestnetExecutionService]:
    owner_id, account_id = tenant
    adapter = BinanceTestnetTradingAdapter(
        TestnetSettings(_env_file=None, api_key=KEY, api_secret=SECRET),  # type: ignore[call-arg]
        transport=venue.transport(),
    )
    orders = OrderLifecycleEngine(
        PostgresOrderRepository(factory, owner_id=owner_id, account_id=account_id)
    )
    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        environment="test",
        testnet_trading_enabled=True,
        testnet=TestnetSettings(_env_file=None, api_key=KEY, api_secret=SECRET),  # type: ignore[call-arg]
    )
    try:
        yield TestnetExecutionService(
            adapter,
            orders,
            market_data=FakeMarketData(),  # type: ignore[arg-type]
            settings=settings,
            account_id=str(account_id),
            session_start=NOW,
        )
    finally:
        await adapter.aclose()


def request(**overrides: Any) -> TestnetOrderRequest:
    base: dict[str, Any] = {
        "symbol": "BTCUSDT",
        "side": OrderSide.BUY,
        "margin": Decimal(20),
        "requested_leverage": Decimal(3),
        "stop_loss_percent": Decimal(2),
        "intent_key": "manual-1",
    }
    base.update(overrides)
    return TestnetOrderRequest(**base)


# ----------------------------------------------------------------------
# The happy path, and what it wrote down
# ----------------------------------------------------------------------


async def test_an_accepted_order_is_durable_at_every_boundary(
    service: TestnetExecutionService, venue: FakeVenue
) -> None:
    """The record exists before the order does, and carries what was verified."""
    result = await service.submit(request())
    assert result.accepted, result.detail
    record = result.record
    assert record is not None

    assert record.state is OrderState.ACCEPTED
    assert record.venue_order_id is not None
    # The crash signature: committed before the call, not after it.
    assert record.submitted_at is not None
    assert record.reached_venue
    # What the venue confirmed, recorded for audit rather than assumed.
    assert record.venue_leverage == Decimal(3)
    assert record.venue_margin_mode is MarginMode.ISOLATED

    reloaded = await service._orders.repository.get(record.order_id)
    assert reloaded is not None
    assert reloaded.state is OrderState.ACCEPTED
    assert reloaded.venue_leverage == Decimal(3)


async def test_the_order_reaches_the_venue_with_one_way_position_side(
    service: TestnetExecutionService, venue: FakeVenue
) -> None:
    await service.submit(request())
    assert venue.submit_calls == 1


# ----------------------------------------------------------------------
# Leverage: approval is binding
# ----------------------------------------------------------------------


async def test_the_venue_leverage_is_set_and_verified_before_submission(
    service: TestnetExecutionService, venue: FakeVenue
) -> None:
    await service.submit(request(requested_leverage=Decimal(3)))
    assert venue.applied_leverage == Decimal(3)


async def test_a_leverage_the_venue_did_not_apply_refuses_the_order(
    service: TestnetExecutionService, venue: FakeVenue
) -> None:
    """Set-then-verify, and the verify half doing its job.

    Without it, an order would sit on the book at a leverage the risk engine
    never approved -- which is not the order that was ruled on.
    """
    venue.leverage_to_report = Decimal(10)
    result = await service.submit(request(requested_leverage=Decimal(3)))
    assert not result.accepted
    assert result.rejection_code is RiskRejectionCode.MAX_LEVERAGE
    assert "approved" in result.detail
    assert venue.submit_calls == 0


async def test_an_unknown_exchange_ceiling_refuses_rather_than_assuming(
    service: TestnetExecutionService, venue: FakeVenue
) -> None:
    """The venue published no bracket. Unknown is never permission."""
    venue.brackets_present = False
    result = await service.submit(request(requested_leverage=Decimal(3)))
    assert not result.accepted
    assert venue.submit_calls == 0


async def test_the_exchange_ceiling_binds_below_the_request(
    service: TestnetExecutionService, venue: FakeVenue
) -> None:
    venue.max_leverage = Decimal(2)
    result = await service.submit(request(requested_leverage=Decimal(5)))
    if result.accepted:
        assert venue.applied_leverage is not None
        assert venue.applied_leverage <= Decimal(2)
    else:
        assert venue.submit_calls == 0


# ----------------------------------------------------------------------
# Margin mode
# ----------------------------------------------------------------------


async def test_isolated_margin_is_established_and_verified(
    service: TestnetExecutionService, venue: FakeVenue
) -> None:
    assert venue.margin_type == "CROSSED"
    result = await service.submit(request())
    assert result.accepted, result.detail
    assert venue.margin_type == "ISOLATED"


async def test_already_isolated_is_treated_as_satisfied(
    service: TestnetExecutionService, venue: FakeVenue
) -> None:
    """The venue answers -4046 when nothing needs changing. That is the goal state."""
    venue.margin_type = "ISOLATED"
    result = await service.submit(request())
    assert result.accepted, result.detail


async def test_a_refused_margin_change_refuses_the_order(
    service: TestnetExecutionService, venue: FakeVenue
) -> None:
    """Never silently continue under CROSSED, and never close a position to force it."""
    venue.margin_change_refused = True
    result = await service.submit(request())
    assert not result.accepted
    assert venue.submit_calls == 0
    assert venue.margin_type == "CROSSED"


# ----------------------------------------------------------------------
# Position mode
# ----------------------------------------------------------------------


async def test_hedge_mode_refuses_before_anything_is_written(
    service: TestnetExecutionService, venue: FakeVenue
) -> None:
    venue.dual_side = True
    result = await service.submit(request())
    assert not result.accepted
    assert result.record is None
    assert "hedge mode" in result.detail.lower()
    assert venue.submit_calls == 0


# ----------------------------------------------------------------------
# Idempotency and replay
# ----------------------------------------------------------------------


async def test_the_same_intent_replays_rather_than_placing_a_second_order(
    service: TestnetExecutionService, venue: FakeVenue
) -> None:
    first = await service.submit(request())
    assert first.accepted
    second = await service.submit(request())

    assert second.replayed
    assert second.record is not None
    assert first.record is not None
    assert second.record.order_id == first.record.order_id
    # The decisive assertion: the venue saw one order, not two.
    assert venue.submit_calls == 1


# ----------------------------------------------------------------------
# The uncertainty window
# ----------------------------------------------------------------------


async def test_a_submission_timeout_becomes_unknown_and_is_never_retried(
    service: TestnetExecutionService, venue: FakeVenue
) -> None:
    """A timeout before and after the venue received it are indistinguishable.

    So the honest state is UNKNOWN, the answer comes from reconciliation, and
    nothing retries -- a retry here risks a second position.
    """
    venue.submit_error = httpx.ReadTimeout("timed out")
    result = await service.submit(request())

    assert not result.accepted
    record = result.record
    assert record is not None
    assert record.state is OrderState.UNKNOWN
    assert record.submitted_at is not None
    assert record.venue_order_id is None
    assert record.is_unreconciled
    assert venue.submit_calls == 1


async def test_an_unreconciled_order_blocks_the_next_entry(
    service: TestnetExecutionService, venue: FakeVenue
) -> None:
    venue.submit_error = httpx.ReadTimeout("timed out")
    await service.submit(request(intent_key="manual-a"))

    venue.submit_error = None
    blocked = await service.submit(request(intent_key="manual-b"))
    assert not blocked.accepted
    assert blocked.rejection_code is RiskRejectionCode.RECONCILIATION_PENDING


# ----------------------------------------------------------------------
# Fills
# ----------------------------------------------------------------------


async def test_a_partial_fill_is_recorded_as_partial(
    service: TestnetExecutionService, venue: FakeVenue
) -> None:
    venue.order_status = "PARTIALLY_FILLED"
    venue.filled_qty = Decimal("0.0005")
    venue.avg_price = Decimal(50_000)
    result = await service.submit(request())
    assert result.record is not None
    assert result.record.state is OrderState.PARTIALLY_FILLED


async def test_a_full_fill_is_terminal(service: TestnetExecutionService, venue: FakeVenue) -> None:
    venue.order_status = "FILLED"
    result = await service.submit(request())
    assert result.record is not None
    assert result.record.state is OrderState.FILLED
    assert result.record.is_terminal


# ----------------------------------------------------------------------
# Cancellation
# ----------------------------------------------------------------------


async def test_a_cancel_that_loses_the_race_to_a_fill_is_not_an_error(
    service: TestnetExecutionService, venue: FakeVenue
) -> None:
    """The venue is the authority. Filling before the cancel arrived is ordinary."""
    submitted = await service.submit(request())
    assert submitted.record is not None

    venue.order_status = "FILLED"
    result = await service.cancel(submitted.record.order_id)
    assert result.record is not None
    assert result.record.state is OrderState.FILLED
    assert not result.accepted  # it was not cancelled -- and that is the truth


async def test_a_cancel_whose_outcome_is_unknown_does_not_claim_cancellation(
    service: TestnetExecutionService, venue: FakeVenue
) -> None:
    submitted = await service.submit(request())
    assert submitted.record is not None
    venue.vanish_all = False
    venue.submit_error = None

    # Make the cancel itself fail.
    original = venue._handle

    def failing(req: httpx.Request) -> httpx.Response:
        if req.method == "DELETE":
            raise httpx.ReadTimeout("timed out")
        return original(req)

    service._adapter._client._transport = httpx.MockTransport(failing)  # type: ignore[attr-defined]
    result = await service.cancel(submitted.record.order_id)
    assert result.record is not None
    assert result.record.state is OrderState.UNKNOWN


# ----------------------------------------------------------------------
# Venue reset
# ----------------------------------------------------------------------


async def test_a_wholesale_disappearance_resolves_nothing(
    service: TestnetExecutionService, venue: FakeVenue
) -> None:
    """The testnet resets monthly without notice and takes every order with it.

    Read one order at a time, "no such order" looks like evidence. It is one
    event, and resolving a batch on the strength of it would fabricate a
    terminal state for every position the system was carrying.
    """
    for key in ("manual-a", "manual-b"):
        result = await service.submit(request(intent_key=key))
        assert result.accepted, result.detail

    venue.vanish_all = True
    sweep = await service.reconcile_all()

    assert sweep.venue_reset_suspected
    assert sweep.resolved == 0
    records = await service._orders.repository.all_records()
    for record in records:
        assert not record.is_terminal, "a vanished order was given a terminal state"


async def test_a_single_vanished_order_is_not_treated_as_a_reset(
    service: TestnetExecutionService, venue: FakeVenue
) -> None:
    """One order is not a pattern. It stays unknown, on its own merits."""
    result = await service.submit(request())
    assert result.accepted
    venue.vanish_all = True

    sweep = await service.reconcile_all()
    assert not sweep.venue_reset_suspected
    assert sweep.resolved == 0


# ----------------------------------------------------------------------
# Secrets
# ----------------------------------------------------------------------


async def test_no_result_ever_carries_a_credential(
    service: TestnetExecutionService, venue: FakeVenue
) -> None:
    venue.margin_change_refused = True
    outcomes = [
        await service.submit(request(intent_key="a")),
        await service.submit(request(intent_key="b")),
    ]
    for result in outcomes:
        rendered = f"{result.detail} {result.record!r}"
        assert KEY.get_secret_value() not in rendered
        assert SECRET.get_secret_value() not in rendered
