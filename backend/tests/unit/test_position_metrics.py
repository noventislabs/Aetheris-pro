"""Measured facts about an open position, long and short.

Excursions are driven through the real engine tick loop rather than assigned
directly, because the property under test is that they accumulate from marks
the position actually lived through. Assigning them would test the getter.

Nothing here may be computable from a candle series the position never saw:
a spike that printed between two polls was not observed and must not appear.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from aetheris.analysis.leverage import resolve_leverage
from aetheris.analysis.position.metrics import compute_position_metrics
from aetheris.core.freshness import DataStatus
from aetheris.domain.enums import OrderSide
from aetheris.domain.leverage import LeverageRequest
from aetheris.engines.paper.engine import (
    MarkPrice,
    PaperEngine,
    PaperEngineConfig,
    SubmitOrderRequest,
)
from aetheris.engines.paper.snapshot import from_snapshot, to_snapshot
from aetheris.engines.paper.store import InMemoryPaperRepository

NOW = datetime(2026, 9, 21, 12, 0, tzinfo=UTC)
FEE_BPS = Decimal(5)


def engine() -> PaperEngine:
    config = PaperEngineConfig(
        starting_balance=Decimal(1000),
        daily_profit_target=Decimal(20),
        daily_loss_limit=Decimal(-10),
        max_open_positions=5,
        max_position_notional=Decimal(1000),
        max_portfolio_exposure=Decimal(3000),
        max_data_age_seconds=30.0,
    )
    return PaperEngine(
        InMemoryPaperRepository(starting_balance=config.starting_balance, now=NOW), config
    )


def mark(price: str, *, usable: bool = True) -> MarkPrice:
    return MarkPrice(
        symbol="BTCUSDT",
        status=DataStatus.OK if usable else DataStatus.STALE,
        source="venue-adapter:rest",
        last_price=Decimal(price) if usable else None,
        bid_price=None,
        ask_price=None,
        age_seconds=1.0 if usable else 9_000.0,
    )


def one_x():  # type: ignore[no-untyped-def]
    return resolve_leverage(
        LeverageRequest(requested_leverage=Decimal(1), basis="test"),
        exchange_max_leverage=None,
        risk_max_leverage=None,
        risk_engine_available=False,
    )


def opened(
    *,
    side: OrderSide = OrderSide.BUY,
    price: str = "100",
    stop_percent: str | None = "10",
    target_percent: str | None = "20",
) -> PaperEngine:
    eng = engine()
    eng.submit_order(
        SubmitOrderRequest(
            symbol="BTCUSDT",
            side=side,
            margin=Decimal(50),
            stop_loss_percent=Decimal(stop_percent) if stop_percent else None,
            take_profit_percent=Decimal(target_percent) if target_percent else None,
            client_order_id=f"metrics-{side.value}",
        ),
        now=NOW,
        mark=mark(price),
        filters=None,
        leverage=one_x(),
        paper_enabled=True,
        marks={"BTCUSDT": mark(price)},
    )
    return eng


def drive(eng: PaperEngine, prices: list[str], *, start: int = 1) -> None:
    for offset, price in enumerate(prices, start=start):
        eng.tick(now=NOW + timedelta(minutes=offset), marks={"BTCUSDT": mark(price)})


def metrics_for(eng: PaperEngine, *, minutes: int = 10):  # type: ignore[no-untyped-def]
    account = eng.snapshot(
        now=NOW + timedelta(minutes=minutes),
        marks={"BTCUSDT": mark(str(eng.state().positions["BTCUSDT"].mark_price))},
    )
    position = account.positions[0]
    return compute_position_metrics(
        position, now=NOW + timedelta(minutes=minutes), taker_fee_bps=FEE_BPS
    )


# ----------------------------------------------------------------------
# Excursions — long
# ----------------------------------------------------------------------


def test_mfe_long_takes_the_highest_observed_price() -> None:
    eng = opened(side=OrderSide.BUY, price="100")
    entry = eng.state().positions["BTCUSDT"].entry_price
    drive(eng, ["103", "107", "104"])

    assert eng.state().positions["BTCUSDT"].best_price == Decimal(107)
    result = metrics_for(eng)
    assert result.mfe_per_unit == Decimal(107) - entry


def test_mae_long_takes_the_lowest_observed_price() -> None:
    eng = opened(side=OrderSide.BUY, price="100")
    entry = eng.state().positions["BTCUSDT"].entry_price
    drive(eng, ["98", "94", "99"])

    assert eng.state().positions["BTCUSDT"].worst_price == Decimal(94)
    result = metrics_for(eng)
    assert result.mae_per_unit == entry - Decimal(94)


# ----------------------------------------------------------------------
# Excursions — short
# ----------------------------------------------------------------------


def test_mfe_short_takes_the_lowest_observed_price() -> None:
    """Favourable inverts with the side; this is where a sign error would hide."""
    eng = opened(side=OrderSide.SELL, price="100")
    entry = eng.state().positions["BTCUSDT"].entry_price
    drive(eng, ["97", "92", "96"])

    assert eng.state().positions["BTCUSDT"].best_price == Decimal(92)
    result = metrics_for(eng)
    assert result.mfe_per_unit == entry - Decimal(92)


def test_mae_short_takes_the_highest_observed_price() -> None:
    eng = opened(side=OrderSide.SELL, price="100", stop_percent="30")
    entry = eng.state().positions["BTCUSDT"].entry_price
    drive(eng, ["102", "106", "103"])

    assert eng.state().positions["BTCUSDT"].worst_price == Decimal(106)
    result = metrics_for(eng)
    assert result.mae_per_unit == Decimal(106) - entry


def test_excursions_only_widen() -> None:
    eng = opened(side=OrderSide.BUY, price="100")
    drive(eng, ["110", "101", "102"])
    position = eng.state().positions["BTCUSDT"]
    assert position.best_price == Decimal(110)
    # A later lower mark must not pull the best back down.
    assert position.best_price != Decimal(102)


def test_an_unusable_mark_records_no_excursion() -> None:
    """A price nobody could read is not an extreme."""
    eng = opened(side=OrderSide.BUY, price="100")
    drive(eng, ["105"])
    before = eng.state().positions["BTCUSDT"].best_price
    eng.tick(now=NOW + timedelta(minutes=5), marks={"BTCUSDT": mark("999", usable=False)})
    assert eng.state().positions["BTCUSDT"].best_price == before


def test_entry_counts_as_the_first_observation() -> None:
    """Before any tick, the entry is genuinely both the best and worst seen."""
    eng = opened(side=OrderSide.BUY, price="100")
    position = eng.state().positions["BTCUSDT"]
    assert position.best_price == position.entry_price
    assert position.worst_price == position.entry_price


# ----------------------------------------------------------------------
# Break-even
# ----------------------------------------------------------------------


def test_break_even_is_above_entry_for_a_long_and_below_for_a_short() -> None:
    """Fees have to be earned back, in the direction of the trade."""
    long_metrics = metrics_for(opened(side=OrderSide.BUY, price="100"))
    short_metrics = metrics_for(opened(side=OrderSide.SELL, price="100"))

    long_entry = Decimal("100.05")  # slipped upward
    short_entry = Decimal("99.95")  # slipped downward
    assert long_metrics.break_even_price is not None
    assert short_metrics.break_even_price is not None
    assert long_metrics.break_even_price > long_entry
    assert short_metrics.break_even_price < short_entry


def test_break_even_solves_for_the_exit_fee_rather_than_approximating() -> None:
    """Closing exactly at break-even must net zero, not almost zero.

    The exit fee is charged on the exit notional, which depends on the price
    being solved for -- so "entry plus two fees" is wrong by the fee on the
    fee. Verified by replaying the engine's own arithmetic.
    """
    eng = opened(side=OrderSide.BUY, price="100")
    position = eng.snapshot(now=NOW, marks={"BTCUSDT": mark("100")}).positions[0]
    result = compute_position_metrics(position, now=NOW, taker_fee_bps=FEE_BPS)
    price = result.break_even_price
    assert price is not None

    gross = (price - position.entry_price) * position.quantity
    exit_fee = price * position.quantity * FEE_BPS / Decimal(10_000)
    net = gross - position.entry_fee - exit_fee
    assert abs(net) < Decimal("0.00000001")


# ----------------------------------------------------------------------
# R
# ----------------------------------------------------------------------


def test_remaining_r_uses_the_risk_planned_at_entry() -> None:
    eng = opened(side=OrderSide.BUY, price="100", stop_percent="10")
    drive(eng, ["105"])
    result = metrics_for(eng)

    position = eng.state().positions["BTCUSDT"]
    planned = position.thesis.planned_risk_per_unit if position.thesis else None
    assert planned is not None
    assert result.remaining_r is not None
    assert result.r_multiple_now is not None
    # Mark is above entry, so more than the full planned risk is still ahead
    # of the stop.
    assert result.remaining_r > Decimal(1)
    assert result.r_multiple_now > Decimal(0)


def test_r_is_unavailable_without_a_planned_risk() -> None:
    """No stop at entry means no R to divide by. Not zero."""
    eng = opened(side=OrderSide.BUY, price="100", stop_percent=None, target_percent=None)
    drive(eng, ["105"])
    result = metrics_for(eng)

    assert result.remaining_r is None
    assert result.r_multiple_now is None
    assert "remaining_r" in result.unavailable
    assert "r_multiple_now" in result.unavailable


def test_r_is_negative_when_the_mark_is_against_the_position() -> None:
    eng = opened(side=OrderSide.SELL, price="100", stop_percent="30")
    drive(eng, ["104"])
    result = metrics_for(eng)
    assert result.r_multiple_now is not None
    assert result.r_multiple_now < Decimal(0)


# ----------------------------------------------------------------------
# Distances and unusable data
# ----------------------------------------------------------------------


def test_distances_are_measured_from_the_mark() -> None:
    eng = opened(side=OrderSide.BUY, price="100")
    drive(eng, ["105"])
    result = metrics_for(eng)
    assert result.stop_distance_percent is not None
    assert result.target_distance_percent is not None
    assert result.stop_distance_percent > 0
    assert result.target_distance_percent > 0


def test_an_unmarked_position_yields_no_distances() -> None:
    """Distances against a stale price would be worse than none."""
    eng = opened(side=OrderSide.BUY, price="100")
    eng.tick(now=NOW + timedelta(minutes=1), marks={"BTCUSDT": mark("0", usable=False)})
    account = eng.snapshot(
        now=NOW + timedelta(minutes=2), marks={"BTCUSDT": mark("0", usable=False)}
    )
    result = compute_position_metrics(
        account.positions[0], now=NOW + timedelta(minutes=2), taker_fee_bps=FEE_BPS
    )

    assert result.stop_distance_percent is None
    assert result.target_distance_percent is None
    assert result.remaining_r is None
    assert result.unrealized_pnl is None
    assert "stop_distance_percent" in result.unavailable
    assert "unrealized_pnl" in result.unavailable
    # Excursions survive: they were observed before the data went bad.
    assert result.mfe_per_unit is not None


# ----------------------------------------------------------------------
# Time, and persistence
# ----------------------------------------------------------------------


def test_time_in_trade_comes_from_the_open_and_the_evaluation_clock() -> None:
    eng = opened()
    result = metrics_for(eng, minutes=90)
    assert result.time_in_trade_seconds == Decimal(90 * 60)


def test_excursions_survive_a_restart() -> None:
    """They are accumulated state, so losing them silently resets MFE/MAE."""
    eng = opened(side=OrderSide.BUY, price="100")
    drive(eng, ["112", "91"])
    before = eng.state().positions["BTCUSDT"]

    restored = from_snapshot(to_snapshot(eng.state())).positions["BTCUSDT"]
    assert restored.best_price == before.best_price == Decimal(112)
    assert restored.worst_price == before.worst_price == Decimal(91)
