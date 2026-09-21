"""Concurrent writers against one paper account.

Phase 6 had exactly one writer: the HTTP request. Phase 7 adds a second, the
autonomous loop, and every mutating path in ``PaperTradingService`` does a
read-modify-write **across an await** -- it fetches prices, then mutates.

It is worth being precise about what protects what, because the obvious story
is not quite the true one.

**The paper engine is already atomic.** Its methods are synchronous, so once
``engine.submit_order`` begins, nothing interleaves with it. Its own check --
"is this symbol already open?" -- therefore cannot be raced, and a duplicate
position was never reachable through it. That is phase 6 doing its job.

**What the lock adds** is that the *whole sequence* is atomic, not just the
final mutation:

* a ``reset`` can no longer land between a submission's price fetch and its
  fill, which would file that fill into an account that had just been
  discarded. This one genuinely misbehaves without the lock, and the test
  below was verified to fail when the lock was removed;
* an emergency stop cannot be observed half-applied;
* any future check that moves from the engine up into the service inherits the
  guarantee rather than quietly losing it.

``SlowMarketData`` parks every price fetch on a gate the test controls, so the
interleaving is forced rather than hoped for. An earlier draft of this file
waited for only one coroutine to park before releasing the gate, which let the
first run to completion before the second started -- the tests passed with the
lock removed, which is the failure mode a concurrency test most easily hides.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal

import pytest

from aetheris.core.config import Settings
from aetheris.core.errors import RiskRejectionCode
from aetheris.core.freshness import Observation, utcnow
from aetheris.domain.enums import ContractType, OrderSide, SymbolStatus
from aetheris.domain.market import Symbol, SymbolFilters, Ticker
from aetheris.engines.paper.engine import PaperEngine, PaperEngineConfig, SubmitOrderRequest
from aetheris.engines.paper.store import InMemoryPaperRepository
from aetheris.services.paper import PaperTradingService

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


SYMBOL = Symbol(
    symbol="TESTUSDT",
    base_asset="TEST",
    quote_asset="USDT",
    status=SymbolStatus.TRADING,
    contract_type=ContractType.PERPETUAL,
    price_precision=2,
    quantity_precision=3,
    filters=SymbolFilters(
        tick_size=Decimal("0.01"),
        step_size=Decimal("0.001"),
        min_quantity=Decimal("0.001"),
        min_notional=Decimal("5"),
    ),
)


async def settle(cycles: int = 20) -> None:
    """Give every runnable coroutine a chance to advance.

    Used instead of waiting for a second arrival, which would deadlock: with
    the lock working, a second coroutine *cannot* reach its fetch while the
    first holds the window. That is the property under test, so the test must
    not require the opposite in order to run.
    """
    for _ in range(cycles):
        await asyncio.sleep(0)


class SlowMarketData:
    """A market-data service whose fetches park until the test releases them."""

    exchange_name = "test-venue"

    def __init__(self) -> None:
        self.gate = asyncio.Event()
        self.arrived = asyncio.Semaphore(0)
        self.ticker_calls = 0

    async def get_ticker(self, symbol: str) -> Observation[Ticker]:
        self.ticker_calls += 1
        self.arrived.release()
        await self.gate.wait()
        return Observation[Ticker].ok(
            Ticker(
                symbol=symbol.upper(),
                last_price=Decimal("100"),
                bid_price=Decimal("99.99"),
                ask_price=Decimal("100.01"),
                event_time=utcnow(),
            ),
            source="test-venue:rest",
            event_ts=utcnow(),
        )

    async def get_symbol(self, symbol: str) -> Symbol:
        return SYMBOL


def build_service() -> tuple[PaperTradingService, SlowMarketData, PaperEngine]:
    now = utcnow()
    config = PaperEngineConfig(
        starting_balance=Decimal(1000),
        daily_profit_target=Decimal(20),
        daily_loss_limit=Decimal(-10),
        max_open_positions=5,
        max_position_notional=Decimal(500),
        max_portfolio_exposure=Decimal(1000),
        max_data_age_seconds=30.0,
    )
    repository = InMemoryPaperRepository(starting_balance=config.starting_balance, now=now)
    engine = PaperEngine(repository, config)
    market = SlowMarketData()
    settings = Settings(environment="test", _env_file=None)  # type: ignore[call-arg]
    service = PaperTradingService(market, engine, settings)  # type: ignore[arg-type]
    return service, market, engine


def order(client_order_id: str | None = None, **extra: object) -> SubmitOrderRequest:
    return SubmitOrderRequest(
        symbol="TESTUSDT",
        side=OrderSide.BUY,
        margin=Decimal(20),
        client_order_id=client_order_id,
        **extra,  # type: ignore[arg-type]
    )


# ----------------------------------------------------------------------
# The lock's direct guarantee
# ----------------------------------------------------------------------


async def test_only_one_fetch_then_mutate_window_is_open_at_a_time() -> None:
    """The lock's guarantee, asserted directly rather than by consequence.

    While one submission is parked inside its price fetch, a second submitted
    concurrently must not reach a fetch of its own. Counting the fetches is the
    cleanest evidence available: without the lock this is 2.
    """
    service, market, _engine = build_service()

    first = asyncio.create_task(service.submit_order(order("a"), requested_leverage=Decimal(1)))
    second = asyncio.create_task(service.submit_order(order("b"), requested_leverage=Decimal(1)))

    await market.arrived.acquire()  # the first is inside its window
    await settle()  # anything able to proceed, would have by now

    assert market.ticker_calls == 1, (
        "a second writer entered its fetch-then-mutate window while the first "
        "still held it; the write lock is not covering the sequence"
    )

    market.gate.set()
    await asyncio.gather(first, second)


async def test_reads_are_not_blocked_by_a_held_write_lock() -> None:
    """A snapshot one moment out of date is not a correctness problem.

    Blocking reads behind writes would make the terminal stutter every time the
    autonomous loop worked, so ``get_account`` deliberately does not take the
    lock. Asserted so the choice is not quietly reversed later.
    """
    service, market, _engine = build_service()
    submitting = asyncio.create_task(
        service.submit_order(order("a"), requested_leverage=Decimal(1))
    )
    await market.arrived.acquire()

    account = await asyncio.wait_for(service.get_account(), timeout=1.0)
    assert account.positions == ()

    market.gate.set()
    await submitting


# ----------------------------------------------------------------------
# Ordering hazards the lock removes
# ----------------------------------------------------------------------


async def test_a_reset_cannot_land_inside_an_in_flight_submission() -> None:
    """Verified to fail with the lock removed.

    Without it the submission fetches its price, the reset discards the
    account, and the fill is then written into the fresh one -- a position in
    an account that was explicitly cleared, and a balance that no longer
    reconciles to its own starting point.
    """
    service, market, engine = build_service()

    submitting = asyncio.create_task(
        service.submit_order(order("a"), requested_leverage=Decimal(1))
    )
    await market.arrived.acquire()
    resetting = asyncio.create_task(service.reset())
    await settle()
    market.gate.set()
    await asyncio.gather(submitting, resetting)

    final = engine.snapshot(now=utcnow())
    assert final.positions == (), "a reset must leave no position behind it"
    assert final.balance == final.starting_balance + final.realized_pnl


async def test_an_emergency_stop_cannot_be_observed_half_applied() -> None:
    """The halt either precedes the submission or follows it, never splits it."""
    service, market, engine = build_service()

    submitting = asyncio.create_task(
        service.submit_order(order("a"), requested_leverage=Decimal(1))
    )
    await market.arrived.acquire()
    stopping = asyncio.create_task(
        service.set_emergency_stop(engaged=True, reason="Halt during submission")
    )
    await settle()
    market.gate.set()
    result, account = await asyncio.gather(submitting, stopping)

    assert account.positions == () or len(account.positions) == 1
    if not result.accepted:
        assert result.order.rejection_code is RiskRejectionCode.EMERGENCY_STOP
    # Whichever way it resolved, the account is self-consistent.
    final = engine.snapshot(now=utcnow())
    assert final.balance == final.starting_balance + final.realized_pnl


# ----------------------------------------------------------------------
# Invariants that must hold however the interleaving resolves
# ----------------------------------------------------------------------


async def test_two_concurrent_submissions_cannot_both_open_the_same_symbol() -> None:
    """The invariant the whole exercise protects.

    The paper engine's synchronous symbol check already makes this true today,
    and this test passes with the lock removed for that reason. It is kept
    because it asserts the *property* rather than the mechanism: if that check
    ever moves up into the service, or the engine gains an await, this is what
    notices.
    """
    service, market, engine = build_service()

    first = asyncio.create_task(service.submit_order(order("a"), requested_leverage=Decimal(1)))
    second = asyncio.create_task(service.submit_order(order("b"), requested_leverage=Decimal(1)))

    await market.arrived.acquire()
    await settle()
    market.gate.set()
    results = await asyncio.gather(first, second)

    accepted = [r for r in results if r.accepted]
    refused = [r for r in results if not r.accepted]
    assert len(accepted) == 1, "exactly one submission may open a position"
    assert refused[0].order.rejection_code is RiskRejectionCode.SYMBOL_LIMIT
    assert len(engine.snapshot(now=utcnow()).positions) == 1


async def test_the_second_submission_sees_the_first_one_reflected() -> None:
    """Serialisation, not merely mutual exclusion."""
    service, market, _engine = build_service()
    market.gate.set()

    first = await service.submit_order(order("a"), requested_leverage=Decimal(1))
    second = await service.submit_order(order("b"), requested_leverage=Decimal(1))

    assert first.accepted
    assert not second.accepted
    assert second.order.rejection_code is RiskRejectionCode.SYMBOL_LIMIT
    assert "already open" in (second.order.rejection_detail or "")


async def test_concurrent_ticks_realise_a_trade_only_once() -> None:
    """Three management passes must not close the same position three times."""
    service, market, engine = build_service()
    market.gate.set()
    # A stop just under the entry. The buy fills at the ask (100.01) while the
    # mark is the last price (100.00), so the position is immediately through
    # its stop and the next tick closes it -- which is what makes three
    # concurrent ticks a meaningful test.
    opened = await service.submit_order(
        order("a", stop_loss_percent=Decimal("0.005")), requested_leverage=Decimal(1)
    )
    assert opened.accepted

    market.gate.clear()
    ticks = [asyncio.create_task(service.tick()) for _ in range(3)]
    await market.arrived.acquire()
    await settle()
    market.gate.set()
    await asyncio.gather(*ticks)

    account = engine.snapshot(now=utcnow())
    assert len(account.recent_trades) == 1, "a position must not be realised twice"
    assert account.balance == account.starting_balance + account.realized_pnl


async def test_a_concurrent_close_and_submit_leave_the_account_consistent() -> None:
    service, market, engine = build_service()
    market.gate.set()
    assert (await service.submit_order(order("a"), requested_leverage=Decimal(1))).accepted

    market.gate.clear()
    closing = asyncio.create_task(service.close_position("TESTUSDT"))
    entering = asyncio.create_task(service.submit_order(order("b"), requested_leverage=Decimal(1)))
    await market.arrived.acquire()
    await settle()
    market.gate.set()
    close_result, _ = await asyncio.gather(closing, entering)

    account = engine.snapshot(now=utcnow())
    assert close_result.accepted
    assert len(account.recent_trades) == 1
    assert len(account.positions) <= 1
    assert account.balance == account.starting_balance + account.realized_pnl
