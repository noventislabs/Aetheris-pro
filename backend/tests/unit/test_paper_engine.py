"""The paper trading engine.

Three things these tests are really guarding:

* **The gate holds.** Every risk limit refuses, with the right code, and no
  caller can route around it.
* **The arithmetic is exact and conservative.** Balance reconciles to the last
  satoshi, fees are charged once, and ambiguity resolves against the trade.
* **Nothing is invented.** Unusable market data produces a refusal or a null,
  never a substituted number.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from aetheris.analysis.leverage import resolve_leverage
from aetheris.core.errors import RiskRejectionCode
from aetheris.core.freshness import DataStatus
from aetheris.domain.enums import OrderSide, OrderState, PositionSide
from aetheris.domain.leverage import (
    LeverageDecision,
    LeverageOutcome,
    LeverageReason,
    LeverageRequest,
)
from aetheris.domain.market import SymbolFilters
from aetheris.domain.paper import Durability, PaperExitReason, RiskLockState
from aetheris.engines.paper.engine import (
    ASSUMPTIONS,
    MarkPrice,
    PaperEngine,
    PaperEngineConfig,
    SubmitOrderRequest,
)
from aetheris.engines.paper.store import DEFAULT_ACCOUNT_ID, InMemoryPaperRepository

NOW = datetime(2026, 9, 21, 12, 0, tzinfo=UTC)


# ----------------------------------------------------------------------
# Builders
# ----------------------------------------------------------------------


def config(**overrides: object) -> PaperEngineConfig:
    base: dict[str, object] = {
        "starting_balance": Decimal(100),
        "daily_profit_target": Decimal(20),
        "daily_loss_limit": Decimal(-10),
        "max_open_positions": 5,
        "max_position_notional": Decimal(100),
        "max_portfolio_exposure": Decimal(300),
        "max_data_age_seconds": 30.0,
    }
    base.update(overrides)
    return PaperEngineConfig(**base)  # type: ignore[arg-type]


def engine(**overrides: object) -> PaperEngine:
    cfg = config(**overrides)
    repository = InMemoryPaperRepository(starting_balance=cfg.starting_balance, now=NOW)
    return PaperEngine(repository, cfg)


def mark(
    price: str = "80000",
    *,
    symbol: str = "BTCUSDT",
    status: DataStatus = DataStatus.OK,
    bid: str | None = None,
    ask: str | None = None,
    age: float | None = 1.0,
) -> MarkPrice:
    return MarkPrice(
        symbol=symbol,
        status=status,
        source="venue-adapter:rest",
        last_price=Decimal(price) if status is DataStatus.OK else None,
        bid_price=Decimal(bid) if bid is not None else None,
        ask_price=Decimal(ask) if ask is not None else None,
        age_seconds=age,
    )


def approved(leverage: str = "1") -> LeverageDecision:
    """A decision the constraint chain genuinely produces for 1x."""
    if leverage == "1":
        return resolve_leverage(
            LeverageRequest(requested_leverage=Decimal(1), basis="test"),
            exchange_max_leverage=None,
            risk_max_leverage=None,
            risk_engine_available=False,
        )
    # Above the domain minimum the chain needs both ceilings, so the test
    # supplies them -- which is the only way a leveraged paper position can
    # exist, and exactly the point.
    return resolve_leverage(
        LeverageRequest(requested_leverage=Decimal(leverage), basis="test"),
        exchange_max_leverage=Decimal(125),
        risk_max_leverage=Decimal(leverage),
        risk_engine_available=True,
    )


def rejected_leverage() -> LeverageDecision:
    return resolve_leverage(
        LeverageRequest(requested_leverage=Decimal(10), basis="test"),
        exchange_max_leverage=None,
        risk_max_leverage=None,
        risk_engine_available=False,
    )


def buy(
    eng: PaperEngine,
    *,
    symbol: str = "BTCUSDT",
    margin: str | None = "20",
    quantity: str | None = None,
    price: str = "80000",
    leverage: LeverageDecision | None = None,
    side: OrderSide = OrderSide.BUY,
    now: datetime = NOW,
    filters: SymbolFilters | None = None,
    price_mark: MarkPrice | None = None,
    **extras: object,
):
    request = SubmitOrderRequest(
        symbol=symbol,
        side=side,
        margin=Decimal(margin) if margin is not None else None,
        quantity=Decimal(quantity) if quantity is not None else None,
        **extras,  # type: ignore[arg-type]
    )
    return eng.submit_order(
        request,
        now=now,
        mark=price_mark or mark(price, symbol=symbol),
        filters=filters,
        leverage=leverage or approved(),
        paper_enabled=True,
    )


# ----------------------------------------------------------------------
# The happy path, and the invariant behind it
# ----------------------------------------------------------------------


def test_a_paper_order_opens_a_position_and_is_labelled_a_simulation() -> None:
    eng = engine()
    result = buy(eng)

    assert result.accepted
    assert result.order.state is OrderState.FILLED
    assert result.position is not None
    assert result.position.side is PositionSide.LONG
    assert result.account.label == "PAPER / SIMULATION ONLY / NO REAL ORDER"
    assert "no order is sent to any exchange" in result.account.disclaimer.lower()
    assert "Simulation only" in (result.detail or "")


def test_the_account_says_its_state_is_in_memory_on_every_response() -> None:
    """A balance that silently resets is worse than one the user knows resets."""
    account = buy(engine()).account
    assert account.durability is Durability.IN_MEMORY
    assert "PAPER STATE: IN-MEMORY - RESETS ON RESTART" in account.durability_notice


def test_balance_always_equals_starting_balance_plus_realised_pnl() -> None:
    """The accounting invariant, checked at every step of a round trip.

    Charging the entry fee twice -- once at open and again inside the closed
    trade's net -- is the easy mistake here, and this is what catches it.
    """
    eng = engine()
    account = eng.snapshot(now=NOW)
    assert account.balance == account.starting_balance + account.realized_pnl

    account = buy(eng).account
    assert account.balance == account.starting_balance + account.realized_pnl

    account, _ = eng.tick(now=NOW, marks={"BTCUSDT": mark("84000")})
    assert account.balance == account.starting_balance + account.realized_pnl

    closed = eng.close_position("BTCUSDT", now=NOW, mark=mark("84000")).account
    assert closed.balance == closed.starting_balance + closed.realized_pnl


def test_fees_are_charged_once_per_side_and_totalled() -> None:
    eng = engine()
    opened = buy(eng, margin="20", price="80000")
    entry_fee = opened.account.total_fees
    assert entry_fee == Decimal("0.01000000")  # 20 USDT notional at 1x, 5 bps

    result = eng.close_position("BTCUSDT", now=NOW, mark=mark("80000"))
    assert result.trade is not None
    assert result.trade.fees == result.account.total_fees
    assert result.trade.fees > entry_fee


def test_margin_is_reserved_not_spent() -> None:
    account = buy(engine(), margin="20").account
    # At or just under the 20 requested: quantity truncates down to the
    # accounting exponent, and truncating down is the safe direction.
    assert Decimal("19.99") < account.margin_used <= Decimal(20)
    assert account.margin_used == account.positions[0].margin
    assert account.available_balance == account.balance - account.margin_used
    # The margin is still the account's money; only the fee has left.
    assert account.balance == Decimal("99.99000000")


# ----------------------------------------------------------------------
# Fill prices come from real observations
# ----------------------------------------------------------------------


def test_a_buy_fills_at_the_published_ask_and_a_sell_at_the_bid() -> None:
    """The spread becomes a measured cost rather than a modelled one."""
    eng = engine()
    result = buy(eng, price_mark=mark("80000", bid="79990", ask="80010"))
    assert result.position is not None
    assert result.position.entry_price == Decimal("80010")
    assert result.order.fills[0].price_source.endswith("best_ask")

    short = buy(
        engine(),
        side=OrderSide.SELL,
        price_mark=mark("80000", bid="79990", ask="80010"),
    )
    assert short.position is not None
    assert short.position.entry_price == Decimal("79990")


def test_without_a_published_book_the_last_price_moves_adversely() -> None:
    """A last trade is a price someone got, not one on offer."""
    result = buy(engine(), price_mark=mark("80000"))
    assert result.position is not None
    assert result.position.entry_price > Decimal("80000")
    assert "slippage" in result.order.fills[0].price_source


def test_every_fill_records_where_its_price_came_from() -> None:
    fill = buy(engine()).order.fills[0]
    assert fill.price_source.startswith("venue-adapter:rest")
    assert fill.price_age_seconds == 1.0


# ----------------------------------------------------------------------
# Market data is never invented
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad",
    [
        pytest.param(mark(status=DataStatus.UNAVAILABLE), id="unavailable"),
        pytest.param(mark(status=DataStatus.STALE), id="stale"),
        pytest.param(mark(status=DataStatus.ERROR), id="error"),
        pytest.param(mark(age=None), id="age-unverifiable"),
        pytest.param(mark(age=120.0), id="too-old"),
    ],
)
def test_an_unusable_price_refuses_the_order_rather_than_inventing_one(
    bad: MarkPrice,
) -> None:
    result = buy(engine(), price_mark=bad)
    assert not result.accepted
    assert result.order.rejection_code is RiskRejectionCode.STALE_DATA
    assert result.account.positions == ()


def test_a_close_is_refused_on_unusable_data_too() -> None:
    """Closing at an invented price would book a fabricated PnL into the balance."""
    eng = engine()
    buy(eng)
    before = eng.snapshot(now=NOW).balance

    result = eng.close_position("BTCUSDT", now=NOW, mark=mark(status=DataStatus.UNAVAILABLE))
    assert not result.accepted
    assert result.order.rejection_code is RiskRejectionCode.STALE_DATA
    assert result.account.balance == before
    assert len(result.account.positions) == 1


def test_an_unmarkable_position_reports_null_pnl_not_zero() -> None:
    eng = engine()
    buy(eng)
    account, closed = eng.tick(now=NOW, marks={"BTCUSDT": mark(status=DataStatus.STALE)})

    assert closed == ()
    assert account.positions[0].mark_price is None
    assert account.positions[0].unrealized_pnl is None
    assert account.unrealized_pnl is None, "zero would assert the position is flat"


def test_an_account_with_no_positions_reports_zero_unrealised_not_null() -> None:
    """Zero is a fact here, not a guess: there is nothing open to be unsure about."""
    assert engine().snapshot(now=NOW).unrealized_pnl == Decimal(0)


def test_a_stale_price_does_not_manage_a_position() -> None:
    """A stop must not fire on data too old to act on."""
    eng = engine()
    buy(eng, stop_loss_percent=Decimal(2))
    stale = MarkPrice(
        symbol="BTCUSDT",
        status=DataStatus.OK,
        source="venue-adapter:rest",
        last_price=Decimal("10000"),  # far below the stop
        age_seconds=900.0,
    )
    account, closed = eng.tick(now=NOW, marks={"BTCUSDT": stale})
    assert closed == ()
    assert len(account.positions) == 1


# ----------------------------------------------------------------------
# PnL
# ----------------------------------------------------------------------


def test_a_long_profits_when_price_rises_and_loses_when_it_falls() -> None:
    for exit_price, sign in (("84000", 1), ("76000", -1)):
        eng = engine()
        buy(eng, margin="20", price="80000")
        trade = eng.close_position("BTCUSDT", now=NOW, mark=mark(exit_price)).trade
        assert trade is not None
        assert (trade.gross_pnl > 0) == (sign > 0)


def test_a_short_profits_when_price_falls() -> None:
    eng = engine()
    buy(eng, side=OrderSide.SELL, margin="20", price="80000")
    trade = eng.close_position("BTCUSDT", now=NOW, mark=mark("76000")).trade
    assert trade is not None
    assert trade.side is PositionSide.SHORT
    assert trade.gross_pnl > 0
    assert trade.net_pnl == trade.gross_pnl - trade.fees


def test_return_percent_is_measured_against_margin_not_notional() -> None:
    """At 5x, a 1% move on notional is a 5% move on the money actually risked."""
    eng = engine()
    buy(eng, margin="20", price="80000", leverage=approved("5"))
    trade = eng.close_position("BTCUSDT", now=NOW, mark=mark("80800")).trade
    assert trade is not None
    assert trade.approved_leverage == Decimal(5)
    assert trade.return_percent > Decimal(4)


def test_unrealised_pnl_tracks_the_mark() -> None:
    eng = engine()
    buy(eng, margin="20", price="80000")
    account, _ = eng.tick(now=NOW, marks={"BTCUSDT": mark("82000")})
    assert account.positions[0].unrealized_pnl is not None
    assert account.positions[0].unrealized_pnl > 0
    assert account.equity > account.balance


# ----------------------------------------------------------------------
# Position management
# ----------------------------------------------------------------------


def test_a_stop_loss_closes_the_position() -> None:
    eng = engine()
    buy(eng, price="80000", stop_loss_percent=Decimal(2))
    _, closed = eng.tick(now=NOW, marks={"BTCUSDT": mark("78000")})
    assert [t.exit_reason for t in closed] == [PaperExitReason.STOP_LOSS]


def test_a_take_profit_closes_the_position() -> None:
    eng = engine()
    buy(eng, price="80000", take_profit_percent=Decimal(4))
    _, closed = eng.tick(now=NOW, marks={"BTCUSDT": mark("84000")})
    assert [t.exit_reason for t in closed] == [PaperExitReason.TAKE_PROFIT]


def test_when_a_move_crosses_both_levels_the_adverse_one_wins() -> None:
    """A single poll can jump both levels, and nothing says which came first.

    Assuming the favourable one is the single most common way a simulation
    flatters itself.
    """
    eng = engine()
    buy(eng, price="80000", stop_loss_percent=Decimal(1), take_profit_percent=Decimal(1))
    # A price that is simultaneously below the stop and above the target is not
    # reachable, so the check is that a target-crossing move with the stop also
    # crossed resolves adversely: use a gap straight through the stop.
    _, closed = eng.tick(now=NOW, marks={"BTCUSDT": mark("50000")})
    assert [t.exit_reason for t in closed] == [PaperExitReason.STOP_LOSS]


def test_among_several_adverse_levels_the_one_nearest_entry_fires() -> None:
    eng = engine()
    buy(eng, price="80000", margin="20", leverage=approved("5"), stop_loss_percent=Decimal(30))
    # Liquidation at 5x sits 20% below entry; the stop sits 30% below. A gap to
    # 40% below crosses both, and liquidation is reached first.
    _, closed = eng.tick(now=NOW, marks={"BTCUSDT": mark("48000")})
    assert [t.exit_reason for t in closed] == [PaperExitReason.LIQUIDATION]


def test_a_trailing_stop_ratchets_upward_and_never_back() -> None:
    eng = engine()
    buy(eng, price="80000", trailing_stop_percent=Decimal(5))

    eng.tick(now=NOW, marks={"BTCUSDT": mark("90000")})
    account, closed = eng.tick(now=NOW, marks={"BTCUSDT": mark("86000")})
    assert closed == ()
    assert account.positions[0].trail_extreme == Decimal("90000")

    # 5% below the 90k extreme is 85.5k, which 85k breaches.
    _, closed = eng.tick(now=NOW, marks={"BTCUSDT": mark("85000")})
    assert [t.exit_reason for t in closed] == [PaperExitReason.TRAILING_STOP]


def test_a_trailing_stop_on_a_short_ratchets_downward() -> None:
    eng = engine()
    buy(eng, side=OrderSide.SELL, price="80000", trailing_stop_percent=Decimal(5))
    eng.tick(now=NOW, marks={"BTCUSDT": mark("70000")})
    _, closed = eng.tick(now=NOW, marks={"BTCUSDT": mark("74000")})
    assert [t.exit_reason for t in closed] == [PaperExitReason.TRAILING_STOP]


def test_liquidation_fires_when_loss_reaches_posted_margin() -> None:
    eng = engine(max_position_notional=Decimal(250))
    result = buy(eng, price="80000", margin="20", leverage=approved("10"))
    assert result.position is not None
    entry = result.position.entry_price
    # At 10x, margin is exhausted by a 10% adverse move.
    assert result.position.liquidation_price == entry * Decimal("0.9")

    _, closed = eng.tick(now=NOW, marks={"BTCUSDT": mark("71000")})
    assert [t.exit_reason for t in closed] == [PaperExitReason.LIQUIDATION]


def test_an_unlevered_long_has_no_liquidation_price_rather_than_zero() -> None:
    """Zero is not a liquidation level; it is the asset ceasing to have value."""
    result = buy(engine(), price="80000", leverage=approved("1"))
    assert result.position is not None
    assert result.position.liquidation_price is None


def test_an_unlevered_short_still_has_one() -> None:
    """A short's loss is unbounded above, so 1x does not make it safe."""
    result = buy(engine(), side=OrderSide.SELL, price="80000", leverage=approved("1"))
    assert result.position is not None
    assert result.position.liquidation_price == result.position.entry_price * 2


def test_a_tick_with_nothing_open_is_harmless() -> None:
    account, closed = engine().tick(now=NOW, marks={})
    assert closed == ()
    assert account.positions == ()


# ----------------------------------------------------------------------
# The risk gate
# ----------------------------------------------------------------------


def test_paper_disabled_refuses_everything() -> None:
    eng = engine()
    result = eng.submit_order(
        SubmitOrderRequest(symbol="BTCUSDT", side=OrderSide.BUY, margin=Decimal(20)),
        now=NOW,
        mark=mark(),
        filters=None,
        leverage=approved(),
        paper_enabled=False,
    )
    assert result.order.rejection_code is RiskRejectionCode.MODE_NOT_ENABLED


def test_an_emergency_stop_blocks_entries_but_never_closes_a_position() -> None:
    """An emergency switch that fires market orders is its own way to lose money."""
    eng = engine()
    buy(eng)
    account = eng.set_emergency_stop(engaged=True, reason="Operator halt", now=NOW)
    assert len(account.positions) == 1

    result = buy(eng, symbol="ETHUSDT", price="3000")
    assert result.order.rejection_code is RiskRejectionCode.EMERGENCY_STOP
    assert "Operator halt" in (result.detail or "")

    eng.set_emergency_stop(engaged=False, reason="", now=NOW)
    assert buy(eng, symbol="ETHUSDT", price="3000").accepted


def test_one_position_per_symbol() -> None:
    eng = engine()
    buy(eng)
    result = buy(eng)
    assert result.order.rejection_code is RiskRejectionCode.SYMBOL_LIMIT


def test_the_open_position_ceiling_is_enforced() -> None:
    eng = engine(max_open_positions=2)
    buy(eng, symbol="AAAUSDT", margin="5", price="100")
    buy(eng, symbol="BBBUSDT", margin="5", price="100")
    result = buy(eng, symbol="CCCUSDT", margin="5", price="100")
    assert result.order.rejection_code is RiskRejectionCode.MAX_OPEN_POSITIONS


def test_the_per_position_notional_ceiling_is_enforced() -> None:
    eng = engine(max_position_notional=Decimal(10))
    result = buy(eng, margin="20")
    assert result.order.rejection_code is RiskRejectionCode.POSITION_SIZE


def test_the_portfolio_exposure_ceiling_is_enforced() -> None:
    eng = engine(max_portfolio_exposure=Decimal(30), max_position_notional=Decimal(25))
    buy(eng, symbol="AAAUSDT", margin="20", price="100")
    result = buy(eng, symbol="BBBUSDT", margin="20", price="100")
    assert result.order.rejection_code is RiskRejectionCode.PORTFOLIO_EXPOSURE


def test_margin_beyond_the_available_balance_is_refused() -> None:
    eng = engine()
    result = buy(eng, margin="500")
    assert result.order.rejection_code in {
        RiskRejectionCode.INSUFFICIENT_BALANCE,
        RiskRejectionCode.POSITION_SIZE,
    }


def test_margin_already_posted_cannot_be_posted_again() -> None:
    eng = engine(max_position_notional=Decimal(100))
    buy(eng, symbol="AAAUSDT", margin="60", price="100")
    result = buy(eng, symbol="BBBUSDT", margin="60", price="100")
    assert result.order.rejection_code is RiskRejectionCode.INSUFFICIENT_BALANCE


@pytest.mark.parametrize(
    ("quantity", "margin"),
    [(None, None), ("1", "20")],
    ids=["neither", "both"],
)
def test_size_must_be_specified_exactly_once(quantity: str | None, margin: str | None) -> None:
    result = buy(engine(), quantity=quantity, margin=margin)
    assert result.order.rejection_code is RiskRejectionCode.POSITION_SIZE


@pytest.mark.parametrize("percent", ["0", "-1", "100", "250"])
def test_nonsensical_stop_and_target_percentages_are_refused(percent: str) -> None:
    result = buy(engine(), stop_loss_percent=Decimal(percent))
    assert result.order.rejection_code is RiskRejectionCode.POSITION_SIZE


# ----------------------------------------------------------------------
# Venue filters are honoured, not approximated
# ----------------------------------------------------------------------


def filters(
    step: str = "0.001", min_qty: str = "0.001", min_notional: str | None = "5"
) -> SymbolFilters:
    return SymbolFilters(
        tick_size=Decimal("0.1"),
        step_size=Decimal(step),
        min_quantity=Decimal(min_qty),
        min_notional=Decimal(min_notional) if min_notional is not None else None,
    )


def test_quantity_is_truncated_down_to_the_venue_step() -> None:
    """Rounding up can spend margin that is not there."""
    result = buy(engine(), quantity="1.23456789", margin=None, price="50", filters=filters())
    assert result.accepted
    assert result.order.filled_quantity == Decimal("1.234")


def test_a_size_below_one_tradable_increment_is_refused() -> None:
    result = buy(engine(), quantity="0.0001", margin=None, price="50", filters=filters())
    assert result.order.rejection_code is RiskRejectionCode.EXCHANGE_PRECISION


def test_a_notional_below_the_venue_minimum_is_refused() -> None:
    result = buy(
        engine(), quantity="0.01", margin=None, price="100", filters=filters(min_notional="50")
    )
    assert result.order.rejection_code is RiskRejectionCode.MIN_NOTIONAL


def test_a_quantity_below_the_venue_minimum_is_refused() -> None:
    result = buy(
        engine(), quantity="0.002", margin=None, price="100", filters=filters(min_qty="0.01")
    )
    assert result.order.rejection_code is RiskRejectionCode.EXCHANGE_PRECISION


# ----------------------------------------------------------------------
# Leverage: the chain has final authority here too
# ----------------------------------------------------------------------


def test_an_unapproved_leverage_request_refuses_the_order() -> None:
    result = buy(engine(), leverage=rejected_leverage())
    assert not result.accepted
    assert result.order.rejection_code is RiskRejectionCode.MAX_LEVERAGE
    assert "unknown" in (result.detail or "").lower()


def test_the_rejected_order_records_the_whole_constraint_chain() -> None:
    """A reader must be able to see *which* link was missing."""
    order = buy(engine(), leverage=rejected_leverage()).order
    assert order.leverage is not None
    assert order.leverage.requested_leverage == Decimal(10)
    assert order.leverage.approved_leverage is None
    assert order.leverage.exchange_max_leverage is None
    assert order.leverage.reason is LeverageReason.EXCHANGE_MAX_UNKNOWN


def test_requested_and_approved_leverage_stay_separate_on_the_order() -> None:
    decision = resolve_leverage(
        LeverageRequest(requested_leverage=Decimal(50), basis="test"),
        exchange_max_leverage=Decimal(20),
        risk_max_leverage=Decimal(3),
        risk_engine_available=True,
    )
    result = buy(engine(), leverage=decision, margin="20", price="80000")
    assert result.order.leverage is not None
    assert result.order.leverage.requested_leverage == Decimal(50)
    assert result.order.leverage.approved_leverage == Decimal(3)
    assert result.position is not None
    assert result.position.approved_leverage == Decimal(3), "the position uses the approval"


def test_the_position_is_sized_from_the_approved_leverage_not_the_request() -> None:
    decision = resolve_leverage(
        LeverageRequest(requested_leverage=Decimal(100), basis="test"),
        exchange_max_leverage=Decimal(125),
        risk_max_leverage=Decimal(2),
        risk_engine_available=True,
    )
    result = buy(engine(), leverage=decision, margin="20", price="100")
    assert result.position is not None
    assert result.position.approved_leverage == Decimal(2)
    # 20 x 2, never 20 x 100. Just under 40 because quantity truncates down.
    assert Decimal("39.99") < result.position.notional <= Decimal(40)


def test_a_decision_that_claims_approval_without_a_value_is_still_refused() -> None:
    """Defence in depth against a malformed decision reaching the engine."""
    malformed = LeverageDecision(
        requested_leverage=Decimal(3),
        approved_leverage=None,
        outcome=LeverageOutcome.APPROVED,
        reason=LeverageReason.APPROVED_IN_FULL,
        detail="malformed",
    )
    result = buy(engine(), leverage=malformed)
    assert result.order.rejection_code is RiskRejectionCode.MAX_LEVERAGE


# ----------------------------------------------------------------------
# The daily session
# ----------------------------------------------------------------------


def reach_realised(eng: PaperEngine, target: str, *, symbol: str = "BTCUSDT") -> None:
    """Realise roughly ``target`` USDT by closing one large winning trade."""
    buy(eng, symbol=symbol, margin="90", price="100", leverage=approved("1"))
    move = Decimal(100) * (Decimal(1) + Decimal(target) / Decimal(90))
    eng.close_position(symbol, now=NOW, mark=mark(str(move), symbol=symbol))


def test_the_daily_profit_target_locks_new_entries() -> None:
    eng = engine(daily_profit_target=Decimal(5))
    reach_realised(eng, "6")
    account = eng.snapshot(now=NOW)
    assert account.session.lock_state is RiskLockState.DAILY_PROFIT_TARGET
    assert account.session.realized_pnl >= Decimal(5)

    result = buy(eng, symbol="ETHUSDT", margin="5", price="3000")
    assert result.order.rejection_code is RiskRejectionCode.DAILY_PROFIT_TARGET


def test_the_daily_loss_limit_locks_new_entries() -> None:
    eng = engine(daily_loss_limit=Decimal(-5))
    buy(eng, margin="90", price="100")
    eng.close_position("BTCUSDT", now=NOW, mark=mark("92"))

    account = eng.snapshot(now=NOW)
    assert account.session.lock_state is RiskLockState.DAILY_LOSS_LIMIT
    result = buy(eng, symbol="ETHUSDT", margin="5", price="3000")
    assert result.order.rejection_code is RiskRejectionCode.DAILY_LOSS_LIMIT


def test_a_lock_stops_opening_but_keeps_managing_what_is_open() -> None:
    eng = engine(daily_loss_limit=Decimal(-1), max_open_positions=5)
    buy(eng, symbol="ETHUSDT", margin="10", price="3000", stop_loss_percent=Decimal(2))
    buy(eng, symbol="BTCUSDT", margin="50", price="100")
    eng.close_position("BTCUSDT", now=NOW, mark=mark("96"))

    account = eng.snapshot(now=NOW)
    assert account.session.lock_state is RiskLockState.DAILY_LOSS_LIMIT
    assert len(account.positions) == 1, "the open position was not force-closed"

    _, closed = eng.tick(now=NOW, marks={"ETHUSDT": mark("2900", symbol="ETHUSDT")})
    assert [t.exit_reason for t in closed] == [PaperExitReason.STOP_LOSS]


def test_the_lock_is_judged_on_realised_pnl_not_unrealised() -> None:
    """A brake that engages and releases on noise is not a limit."""
    eng = engine(daily_loss_limit=Decimal(-1))
    buy(eng, margin="50", price="100")
    account, _ = eng.tick(now=NOW, marks={"BTCUSDT": mark("90")})

    assert account.unrealized_pnl is not None
    assert account.unrealized_pnl < Decimal(-1)
    assert account.session.lock_state is RiskLockState.NONE


def test_a_new_utc_day_starts_a_fresh_session() -> None:
    eng = engine(daily_loss_limit=Decimal(-1))
    buy(eng, margin="50", price="100")
    eng.close_position("BTCUSDT", now=NOW, mark=mark("95"))
    assert eng.snapshot(now=NOW).session.lock_state is RiskLockState.DAILY_LOSS_LIMIT

    tomorrow = NOW + timedelta(days=1)
    session = eng.snapshot(now=tomorrow).session
    assert session.session_date == tomorrow.date()
    assert session.lock_state is RiskLockState.NONE
    assert session.realized_pnl == Decimal(0)
    assert buy(eng, now=tomorrow, margin="10", price="100").accepted


def test_the_session_counts_submissions_and_refusals() -> None:
    eng = engine()
    buy(eng)
    buy(eng)  # refused: one position per symbol
    session = eng.snapshot(now=NOW).session
    assert session.orders_submitted == 1
    assert session.orders_rejected == 1


# ----------------------------------------------------------------------
# Idempotency and the order log
# ----------------------------------------------------------------------


def test_resubmitting_a_client_order_id_returns_the_original_outcome() -> None:
    eng = engine()
    first = buy(eng, client_order_id="retry-1")
    second = buy(eng, client_order_id="retry-1")

    assert first.accepted and second.accepted
    assert second.order.idempotent_replay is True
    assert second.order.order_id == first.order.order_id
    assert len(second.account.positions) == 1, "a retry must not open a second position"


def test_a_rejection_is_replayed_as_a_rejection() -> None:
    eng = engine()
    buy(eng, client_order_id="dup", leverage=rejected_leverage())
    replay = buy(eng, client_order_id="dup")
    assert not replay.accepted
    assert replay.order.idempotent_replay is True
    assert replay.order.rejection_code is RiskRejectionCode.MAX_LEVERAGE


def test_a_refused_order_is_kept_in_the_history_with_its_code() -> None:
    """A blank where an order was refused is how a risk limit becomes invisible."""
    eng = engine()
    account = buy(eng, leverage=rejected_leverage()).account
    assert account.recent_orders[0].state is OrderState.REJECTED
    assert account.recent_orders[0].rejection_code is RiskRejectionCode.MAX_LEVERAGE
    assert account.recent_orders[0].rejection_detail


def test_the_order_log_is_bounded() -> None:
    eng = engine(max_order_log=10)
    for index in range(30):
        buy(eng, symbol="BTCUSDT", client_order_id=f"order-{index}")
    state = eng._repository.load()
    assert len(state.order_log) == 10
    assert len(state.orders_by_client_id) == 10


# ----------------------------------------------------------------------
# Lifecycle
# ----------------------------------------------------------------------


def test_closing_a_position_that_does_not_exist_is_refused() -> None:
    result = engine().close_position("BTCUSDT", now=NOW, mark=mark())
    assert not result.accepted
    assert result.order.rejection_code is RiskRejectionCode.POSITION_SIZE


def test_a_manual_close_records_the_reason_and_the_reduce_only_flag() -> None:
    eng = engine()
    buy(eng)
    result = eng.close_position("BTCUSDT", now=NOW, mark=mark("81000"))
    assert result.trade is not None
    assert result.trade.exit_reason is PaperExitReason.MANUAL_CLOSE
    assert result.order.reduce_only is True
    assert result.account.positions == ()


def test_reset_discards_everything() -> None:
    eng = engine()
    buy(eng)
    eng.close_position("BTCUSDT", now=NOW, mark=mark("84000"))

    account = eng.reset(now=NOW)
    assert account.balance == Decimal(100)
    assert account.realized_pnl == Decimal(0)
    assert account.positions == ()
    assert account.recent_trades == ()
    assert account.session.lock_state is RiskLockState.NONE


def test_reset_can_choose_a_new_starting_balance() -> None:
    account = engine().reset(now=NOW, starting_balance=Decimal(500))
    assert account.starting_balance == Decimal(500)
    assert account.balance == Decimal(500)


def test_reconciliation_reports_not_applicable_rather_than_faking_consistency() -> None:
    report = engine().reconcile(now=NOW)
    assert report.status.value == "NOT_APPLICABLE"
    assert "no external authority" in report.detail.lower()


def test_autonomous_trading_is_off_and_there_is_no_way_to_turn_it_on() -> None:
    account = engine().snapshot(now=NOW)
    assert account.autonomous_enabled is False
    assert not any(name for name in dir(PaperEngine) if "autonomous" in name.lower()), (
        "no setter exists, because no autonomous loop exists"
    )


def test_there_is_exactly_one_server_side_account() -> None:
    """No request names an account, so no request can reach another one.

    Multi-tenant isolation needs the authentication work in phase 1; until
    then, having no identifier to ask with is what keeps this safe.
    """
    assert engine().snapshot(now=NOW).account_id == DEFAULT_ACCOUNT_ID


def test_the_assumptions_are_published_and_name_what_is_not_modelled() -> None:
    joined = " ".join(ASSUMPTIONS).lower()
    assert "simulation only" in joined
    assert "no order is sent to any exchange" in joined
    assert "funding" in joined
    assert "partial fills" in joined
    assert "poll-driven" in joined
    assert "optimistic" in joined


# ----------------------------------------------------------------------
# Refusal ordering
#
# Found running against live Binance data: BTCUSDT's step size is 0.001, so a
# 20 USDT order at 1x rounds to zero and was refused for EXCHANGE_PRECISION
# even while an emergency stop was engaged. The incidental reason masked the
# fundamental one.
# ----------------------------------------------------------------------


def unsizeable_filters() -> SymbolFilters:
    """A step so coarse that a small order cannot be expressed at all."""
    return SymbolFilters(
        tick_size=Decimal("0.1"),
        step_size=Decimal("0.001"),
        min_quantity=Decimal("0.001"),
        min_notional=Decimal(5),
    )


def test_an_emergency_stop_is_reported_even_when_the_order_is_also_unsizeable() -> None:
    eng = engine()
    eng.set_emergency_stop(engaged=True, reason="Operator halt", now=NOW)
    result = buy(eng, margin="20", price="80000", filters=unsizeable_filters())
    assert result.order.rejection_code is RiskRejectionCode.EMERGENCY_STOP


def test_a_daily_lock_outranks_a_sizing_problem() -> None:
    eng = engine(daily_loss_limit=Decimal(-1))
    buy(eng, margin="50", price="100")
    eng.close_position("BTCUSDT", now=NOW, mark=mark("95"))

    result = buy(eng, symbol="ETHUSDT", margin="20", price="80000", filters=unsizeable_filters())
    assert result.order.rejection_code is RiskRejectionCode.DAILY_LOSS_LIMIT


def test_mode_not_enabled_outranks_everything() -> None:
    eng = engine()
    result = eng.submit_order(
        SubmitOrderRequest(symbol="BTCUSDT", side=OrderSide.BUY),  # also has no size
        now=NOW,
        mark=mark(status=DataStatus.UNAVAILABLE),  # and no usable price
        filters=None,
        leverage=rejected_leverage(),  # and no approved leverage
        paper_enabled=False,
    )
    assert result.order.rejection_code is RiskRejectionCode.MODE_NOT_ENABLED
