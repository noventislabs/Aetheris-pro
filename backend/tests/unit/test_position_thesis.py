"""The original trade thesis, captured at entry and never rewritten.

The one property these tests exist to protect: a thesis describes the reason
a position was opened, as it stood at that moment. If it could be recomputed
later it would answer a different question -- "would we open this now?" --
using bars that did not exist when the decision was made, and every downstream
judgement about whether the thesis still holds would be circular.

So most of what is asserted here is that later market data changes nothing.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from pydantic import ValidationError

from aetheris.analysis.leverage import resolve_leverage
from aetheris.core.freshness import DataStatus
from aetheris.domain.enums import OrderSide, PositionSide, Timeframe
from aetheris.domain.leverage import LeverageRequest
from aetheris.domain.strategy import ConditionOutcome
from aetheris.domain.thesis import PositionThesis, StrategyContext, ThesisStatus
from aetheris.engines.paper.engine import (
    MarkPrice,
    PaperEngine,
    PaperEngineConfig,
    SubmitOrderRequest,
)
from aetheris.engines.paper.snapshot import SCHEMA_VERSION, from_snapshot, to_snapshot
from aetheris.engines.paper.store import InMemoryPaperRepository

NOW = datetime(2026, 9, 21, 12, 0, tzinfo=UTC)


def engine() -> PaperEngine:
    config = PaperEngineConfig(
        starting_balance=Decimal(100),
        daily_profit_target=Decimal(20),
        daily_loss_limit=Decimal(-10),
        max_open_positions=5,
        max_position_notional=Decimal(100),
        max_portfolio_exposure=Decimal(300),
        max_data_age_seconds=30.0,
    )
    return PaperEngine(
        InMemoryPaperRepository(starting_balance=config.starting_balance, now=NOW), config
    )


def mark(price: str, *, symbol: str = "BTCUSDT") -> MarkPrice:
    return MarkPrice(
        symbol=symbol,
        status=DataStatus.OK,
        source="venue-adapter:rest",
        last_price=Decimal(price),
        bid_price=None,
        ask_price=None,
        age_seconds=1.0,
    )


def one_x():  # type: ignore[no-untyped-def]
    return resolve_leverage(
        LeverageRequest(requested_leverage=Decimal(1), basis="test"),
        exchange_max_leverage=None,
        risk_max_leverage=None,
        risk_engine_available=False,
    )


def context(**overrides: object) -> StrategyContext:
    base: dict[str, object] = {
        "strategy_id": "trend_momentum",
        "strategy_version": "1.0.0",
        "timeframe": Timeframe.H1,
        "entry_conditions": (
            ConditionOutcome(name="trend", satisfied=True, detail="EMA(21) above EMA(55)"),
            ConditionOutcome(name="macd", satisfied=True, detail="histogram positive"),
        ),
        "data_source": "binance-futures-usdm:rest",
        "data_status": "OK",
        "data_age_seconds": 2.0,
        "bar_close_time": NOW - timedelta(minutes=5),
    }
    base.update(overrides)
    return StrategyContext(**base)  # type: ignore[arg-type]


def open_position(
    eng: PaperEngine,
    *,
    strategy_context: StrategyContext | None = None,
    price: str = "80000",
    side: OrderSide = OrderSide.BUY,
    stop_percent: str | None = "2",
    target_percent: str | None = "4",
    now: datetime = NOW,
):  # type: ignore[no-untyped-def]
    return eng.submit_order(
        SubmitOrderRequest(
            symbol="BTCUSDT",
            side=side,
            margin=Decimal(20),
            stop_loss_percent=Decimal(stop_percent) if stop_percent else None,
            take_profit_percent=Decimal(target_percent) if target_percent else None,
            client_order_id=f"thesis-{side.value}-{price}-{now.isoformat()}",
            strategy_context=strategy_context,
        ),
        now=now,
        mark=mark(price),
        filters=None,
        leverage=one_x(),
        paper_enabled=True,
        marks={"BTCUSDT": mark(price)},
    )


# ----------------------------------------------------------------------
# Capture at entry
# ----------------------------------------------------------------------


def test_an_autonomous_entry_captures_its_real_strategy_context() -> None:
    eng = engine()
    open_position(eng, strategy_context=context())
    thesis = eng.state().positions["BTCUSDT"].thesis

    assert thesis is not None
    assert thesis.status is ThesisStatus.CAPTURED
    assert thesis.strategy is not None
    assert thesis.strategy.strategy_id == "trend_momentum"
    assert thesis.strategy.strategy_version == "1.0.0"
    assert thesis.strategy.timeframe is Timeframe.H1
    assert {c.name for c in thesis.strategy.entry_conditions} == {"trend", "macd"}
    assert thesis.strategy.bar_close_time == NOW - timedelta(minutes=5)
    assert thesis.captured_at == NOW


def test_a_manual_entry_records_no_strategy_rather_than_an_empty_one() -> None:
    """Inventing a rule set for a human's click would fabricate the evidence
    the intelligence layer later reasons over."""
    eng = engine()
    open_position(eng, strategy_context=None)
    thesis = eng.state().positions["BTCUSDT"].thesis

    assert thesis is not None
    assert thesis.status is ThesisStatus.NO_STRATEGY_CONTEXT
    assert thesis.strategy is None
    assert thesis.condition_count == 0
    assert thesis.detail is not None and "no rule set" in thesis.detail
    # The price half is still recorded: those are facts about the position.
    assert thesis.entry_price > 0
    assert thesis.stop_price is not None
    assert thesis.target_price is not None


def test_the_price_half_of_the_thesis_is_recorded_for_every_position() -> None:
    """Recorded from the realised fill, not the quoted mark.

    The engine moves a market fill adversely by modelled slippage, so the
    entry here is above the 80000 mark. That is the price the position
    actually exists at, and levels derived from anything else would describe
    a trade nobody made.
    """
    eng = engine()
    position = eng.state()
    open_position(eng, price="80000", stop_percent="2", target_percent="4")
    thesis = eng.state().positions["BTCUSDT"].thesis
    assert thesis is not None

    entry = thesis.entry_price
    assert entry > Decimal(80000), "a long fill should be slipped upward"
    assert thesis.stop_price == entry - entry * Decimal("0.02")
    assert thesis.target_price == entry + entry * Decimal("0.04")
    assert thesis.planned_risk_per_unit == entry * Decimal("0.02")
    assert thesis.planned_reward_per_unit == entry * Decimal("0.04")
    assert thesis.risk_reward_ratio == Decimal(2)
    assert thesis.planned_risk_total is not None and thesis.planned_risk_total > 0
    assert position is not None


def test_a_position_without_a_stop_has_no_planned_risk() -> None:
    """Remaining-R must be uncomputable rather than defaulted to something
    convenient."""
    eng = engine()
    open_position(eng, stop_percent=None, target_percent=None)
    thesis = eng.state().positions["BTCUSDT"].thesis

    assert thesis is not None
    assert thesis.stop_price is None
    assert thesis.planned_risk_per_unit is None
    assert thesis.planned_risk_total is None
    assert thesis.risk_reward_ratio is None


def test_a_short_records_its_levels_on_the_correct_side() -> None:
    eng = engine()
    open_position(eng, side=OrderSide.SELL, price="80000")
    thesis = eng.state().positions["BTCUSDT"].thesis

    assert thesis is not None
    assert thesis.side is PositionSide.SHORT
    assert thesis.stop_price is not None and thesis.stop_price > thesis.entry_price
    assert thesis.target_price is not None and thesis.target_price < thesis.entry_price


# ----------------------------------------------------------------------
# No hindsight
# ----------------------------------------------------------------------


def test_later_market_data_cannot_change_a_captured_thesis() -> None:
    """The property everything downstream depends on.

    Many ticks at wildly different prices, and the thesis must be byte
    identical afterwards. If it moved, "does the original thesis still hold"
    would be comparing the present against itself.
    """
    eng = engine()
    open_position(eng, strategy_context=context(), price="80000")
    before = eng.state().positions["BTCUSDT"].thesis
    assert before is not None
    snapshot_before = before.model_dump(mode="json")

    for offset, price in enumerate(("80500", "79000", "81000", "78500"), start=1):
        eng.tick(now=NOW + timedelta(minutes=offset), marks={"BTCUSDT": mark(price)})

    after = eng.state().positions["BTCUSDT"].thesis
    assert after is not None
    assert after.model_dump(mode="json") == snapshot_before


def test_the_thesis_is_frozen_against_mutation() -> None:
    eng = engine()
    open_position(eng, strategy_context=context())
    thesis = eng.state().positions["BTCUSDT"].thesis
    assert thesis is not None
    with pytest.raises(ValidationError):
        thesis.entry_price = Decimal(1)  # type: ignore[misc]


# ----------------------------------------------------------------------
# Persistence
# ----------------------------------------------------------------------


def test_a_thesis_survives_a_snapshot_round_trip() -> None:
    eng = engine()
    open_position(eng, strategy_context=context())
    original = eng.state()
    restored = from_snapshot(to_snapshot(original))

    before = original.positions["BTCUSDT"].thesis
    after = restored.positions["BTCUSDT"].thesis
    assert before is not None and after is not None
    assert after.model_dump(mode="json") == before.model_dump(mode="json")
    assert after.strategy is not None
    assert len(after.strategy.entry_conditions) == 2


def test_a_manual_thesis_survives_too_and_stays_unknown() -> None:
    eng = engine()
    open_position(eng, strategy_context=None)
    restored = from_snapshot(to_snapshot(eng.state()))
    thesis = restored.positions["BTCUSDT"].thesis

    assert thesis is not None
    assert thesis.status is ThesisStatus.NO_STRATEGY_CONTEXT
    assert thesis.strategy is None


# ----------------------------------------------------------------------
# Schema version
# ----------------------------------------------------------------------


def test_the_schema_version_was_bumped_for_the_thesis() -> None:
    assert SCHEMA_VERSION == 2


def test_a_v1_snapshot_is_refused_rather_than_restored_without_theses() -> None:
    """An account restored with no theses would look like positions nobody
    could explain, and the intelligence layer would read the absence as a
    claim about the trade rather than about the record."""
    from aetheris.engines.paper.snapshot import SnapshotSchemaMismatch

    eng = engine()
    open_position(eng, strategy_context=context())
    payload = to_snapshot(eng.state())
    payload["schema_version"] = 1

    with pytest.raises(SnapshotSchemaMismatch, match="cannot be read by this build"):
        from_snapshot(payload)


def test_the_round_trip_is_still_idempotent_at_v2() -> None:
    eng = engine()
    open_position(eng, strategy_context=context())
    once = to_snapshot(eng.state())
    twice = to_snapshot(from_snapshot(once))
    assert once == twice


# ----------------------------------------------------------------------
# The model's own guard
# ----------------------------------------------------------------------


def test_a_captured_thesis_must_carry_its_strategy() -> None:
    with pytest.raises(ValidationError, match="must carry its strategy context"):
        PositionThesis(
            status=ThesisStatus.CAPTURED,
            captured_at=NOW,
            side=PositionSide.LONG,
            entry_price=Decimal(1),
        )


def test_an_unknown_thesis_must_not_carry_one() -> None:
    """Otherwise the status would be claiming the opposite of the payload."""
    with pytest.raises(ValidationError, match="must not carry strategy context"):
        PositionThesis(
            status=ThesisStatus.NO_STRATEGY_CONTEXT,
            captured_at=NOW,
            side=PositionSide.LONG,
            entry_price=Decimal(1),
            strategy=context(),
        )
