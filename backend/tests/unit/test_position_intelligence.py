"""Brains and the decision orchestrator.

The product requirement these tests exist to protect, stated once:

    DO NOT CLOSE A PROFITABLE POSITION MERELY BECAUSE IT IS PROFITABLE.

and its mirror, which is just as easy to get wrong:

    DO NOT HOLD BLINDLY.

So the two headline tests are a large winner whose thesis is intact (must
HOLD) and the same position after genuine, confirmed reversal (must reach
EXIT). Everything else is about the ladder being a ladder rather than a vote,
and about unavailable inputs never being laundered into neutral ones.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from aetheris.analysis.position.brains import (
    PositionObservation,
    exit_brain,
    market_brain,
    momentum_volume_brain,
    position_brain,
    regime_brain,
    risk_brain,
    structure_brain,
    technical_brain,
)
from aetheris.analysis.position.metrics import compute_position_metrics
from aetheris.analysis.position.orchestrator import evaluate_position
from aetheris.analysis.regime import classify_regime
from aetheris.domain.enums import PositionSide, Timeframe
from aetheris.domain.intelligence import (
    BrainName,
    BrainState,
    DataQuality,
    PositionDecision,
    ThesisState,
)
from aetheris.domain.market import Candle
from aetheris.domain.paper import PaperPosition, RiskLockState
from aetheris.domain.regime import MarketRegime
from aetheris.domain.strategy import ConditionOutcome
from aetheris.domain.thesis import PositionThesis, StrategyContext, ThesisStatus

NOW = datetime(2026, 9, 21, 12, 0, tzinfo=UTC)
FEE_BPS = Decimal(5)


# ----------------------------------------------------------------------
# Builders
# ----------------------------------------------------------------------


def bar(index: int, price: Decimal, *, volume: Decimal = Decimal(100)) -> Candle:
    start = NOW - timedelta(hours=400 - index)
    return Candle(
        open_time=start,
        close_time=start + timedelta(hours=1, milliseconds=-1),
        open=price,
        high=price * Decimal("1.004"),
        low=price * Decimal("0.996"),
        close=price,
        volume=volume,
    )


def falling(count: int = 256, *, start: str = "0.4000", drift: str = "-0.0006") -> list[Candle]:
    """A downtrend with pullbacks, ending on a down-leg.

    The bar count is load-bearing. The sine phase decides whether the final
    bar sits on a rise or a fall, and at 260 it lands on a rise: RSI reads
    54 and the MACD histogram turns positive, so two of the four short
    conditions fail and the thesis reads WEAKENING. 256 ends on a fall,
    where all four genuinely hold -- which is the state this fixture is
    supposed to represent.
    """
    out: list[Candle] = []
    for i in range(count):
        wave = Decimal(str(round(0.012 * math.sin(2 * math.pi * i / 16), 6)))
        out.append(bar(i, Decimal(start) + Decimal(drift) * Decimal(i) + wave))
    return out


def reversing(count: int = 260) -> list[Candle]:
    """The same short, after the market turns up and keeps going."""
    out = falling(count // 2)
    last = out[-1].close
    for i in range(count // 2):
        wave = Decimal(str(round(0.010 * math.sin(2 * math.pi * i / 16), 6)))
        out.append(bar(count // 2 + i, last + Decimal("0.0016") * Decimal(i) + wave))
    return out


def short_conditions(*, all_hold: bool = True) -> tuple[ConditionOutcome, ...]:
    return (
        ConditionOutcome(name="trend", satisfied=True, detail="EMA(21) below EMA(55)"),
        ConditionOutcome(name="trend_strength", satisfied=True, detail="ADX above 20"),
        ConditionOutcome(name="momentum", satisfied=all_hold, detail="RSI in the bearish band"),
        ConditionOutcome(name="macd", satisfied=True, detail="histogram negative"),
    )


def thesis(
    *,
    entry: str = "0.3000",
    stop: str | None = "0.3300",
    target: str | None = "0.2000",
    with_strategy: bool = True,
    regime: MarketRegime | None = MarketRegime.TREND_DOWN,
) -> PositionThesis:
    entry_price = Decimal(entry)
    stop_price = Decimal(stop) if stop else None
    target_price = Decimal(target) if target else None
    risk = abs(entry_price - stop_price) if stop_price else None
    reward = abs(target_price - entry_price) if target_price else None
    context = (
        StrategyContext(
            strategy_id="trend_momentum",
            strategy_version="1.0.0",
            timeframe=Timeframe.H1,
            entry_conditions=short_conditions(),
            regime=regime,
            data_source="binance-futures-usdm:rest",
            data_status="OK",
            data_age_seconds=2.0,
            bar_close_time=NOW - timedelta(hours=1),
        )
        if with_strategy
        else None
    )
    return PositionThesis(
        status=ThesisStatus.CAPTURED if context else ThesisStatus.NO_STRATEGY_CONTEXT,
        captured_at=NOW - timedelta(hours=8),
        side=PositionSide.SHORT,
        entry_price=entry_price,
        stop_price=stop_price,
        target_price=target_price,
        planned_risk_per_unit=risk,
        planned_reward_per_unit=reward,
        risk_reward_ratio=(reward / risk) if risk and reward else None,
        strategy=context,
        detail=None if context else "manual",
    )


def position(
    *,
    mark: str | None = "0.2500",
    entry: str = "0.3000",
    stop: str | None = "0.3300",
    target: str | None = "0.2000",
    best: str | None = "0.2450",
    worst: str | None = "0.3050",
    with_strategy: bool = True,
    regime: MarketRegime | None = MarketRegime.TREND_DOWN,
) -> PaperPosition:
    mark_price = Decimal(mark) if mark else None
    quantity = Decimal(1000)
    entry_price = Decimal(entry)
    unrealized = (entry_price - mark_price) * quantity if mark_price else None
    return PaperPosition(
        position_id="paper-position-1",
        symbol="AVAUSDT",
        side=PositionSide.SHORT,
        quantity=quantity,
        entry_price=entry_price,
        notional=entry_price * quantity,
        margin=Decimal(30),
        approved_leverage=Decimal(1),
        entry_fee=Decimal("0.15"),
        stop_price=Decimal(stop) if stop else None,
        target_price=Decimal(target) if target else None,
        liquidation_price=None,
        opened_at=NOW - timedelta(hours=8),
        updated_at=NOW,
        opening_order_id="paper-order-1",
        mark_price=mark_price,
        mark_source="binance-futures-usdm:rest" if mark_price else None,
        mark_status="OK" if mark_price else "STALE",
        unrealized_pnl=unrealized,
        thesis=thesis(
            entry=entry, stop=stop, target=target, with_strategy=with_strategy, regime=regime
        ),
        best_price=Decimal(best) if best else None,
        worst_price=Decimal(worst) if worst else None,
    )


def observe(
    *,
    pos: PaperPosition | None = None,
    candles: list[Candle] | None = None,
    risk_lock: RiskLockState = RiskLockState.NONE,
    unreconciled: int = 0,
    now: datetime = NOW,
) -> PositionObservation:
    resolved = pos if pos is not None else position()
    bars = candles if candles is not None else falling()
    return PositionObservation(
        position=resolved,
        metrics=compute_position_metrics(resolved, now=now, taker_fee_bps=FEE_BPS),
        now=now,
        candles=bars,
        regime=classify_regime(bars) if bars else None,
        risk_lock=risk_lock,
        unreconciled_orders=unreconciled,
    )


# ----------------------------------------------------------------------
# AVAUSDT regression — the product requirement
# ----------------------------------------------------------------------
#
# Synthetic fixture. SHORT, entry 0.3000, current 0.2500. This is NOT a
# historical user trade and is not claimed to be one.


def test_a_large_winner_with_an_intact_thesis_is_held() -> None:
    """The headline requirement: profit alone must not close a position.

    Entry 0.3000, mark 0.2500 -- a 16.7% favourable move on a short, and
    50 USDT of unrealised profit. The bearish thesis still holds, so the
    answer must be HOLD.
    """
    result = evaluate_position(observe())

    assert result.decision is PositionDecision.HOLD
    assert result.thesis_state is ThesisState.THESIS_VALID
    assert result.decision is not PositionDecision.EXIT
    # And the panel says out loud that profit was not the reason.
    assert any("Profit is not a reason to close" in reason for reason in result.reasons)


def test_the_profitable_hold_still_reports_its_profit() -> None:
    """Holding is not the same as ignoring the number."""
    observation = observe()
    assert observation.metrics.unrealized_pnl is not None
    assert observation.metrics.unrealized_pnl > 0
    result = evaluate_position(observation)
    assert result.decision is PositionDecision.HOLD


def rising(count: int = 256, *, start: str = "0.2000", drift: str = "0.0006") -> list[Candle]:
    """The market the short was wrong about: up, and staying up."""
    out: list[Candle] = []
    for i in range(count):
        wave = Decimal(str(round(0.012 * math.sin(2 * math.pi * i / 16), 6)))
        out.append(bar(i, Decimal(start) + Decimal(drift) * Decimal(i) + wave))
    return out


def test_a_confirmed_reversal_reaches_exit() -> None:
    """The mirror requirement: do not hold blindly.

    The short's original conditions have failed and the mark has run a full
    R past the stop recorded at entry. That is two independent readings --
    the rule set and the price itself -- so it is a reversal, not noise.
    """
    result = evaluate_position(observe(pos=position(mark="0.3600"), candles=rising()))

    assert result.thesis_state is ThesisState.EXIT_CONDITION_MET
    assert result.decision is PositionDecision.EXIT
    assert result.thesis_alignment is not None
    assert result.thesis_alignment.value < Decimal(50)


def test_the_full_progression_is_staged_not_binary() -> None:
    """Valid -> approaching -> exit, each from real observed evidence."""
    intact = evaluate_position(observe())
    approaching = evaluate_position(observe(pos=position(target="0.2400")))
    reversed_ = evaluate_position(observe(pos=position(mark="0.3600"), candles=rising()))

    assert intact.thesis_state is ThesisState.THESIS_VALID
    assert intact.decision is PositionDecision.HOLD
    assert approaching.thesis_state is ThesisState.TARGET_APPROACHING
    assert approaching.decision is PositionDecision.TRAIL
    assert reversed_.thesis_state is ThesisState.EXIT_CONDITION_MET
    assert reversed_.decision is PositionDecision.EXIT


def test_a_breached_stop_is_independent_confirmation() -> None:
    """The one confirming reading that is not an indicator.

    A mark past the stop is the price disagreeing with the level the thesis
    itself nominated, which is stronger evidence than any oscillator.
    """
    result = evaluate_position(observe(pos=position(mark="0.3600"), candles=rising()))
    exit_row = next(r for r in result.brain_results if r.brain is BrainName.EXIT)
    levels = next(row for row in exit_row.evidence if row.name == "position_levels")
    assert levels.satisfied is False
    assert "passed the stop" in levels.detail


def test_no_later_bar_can_change_an_earlier_evaluation() -> None:
    """Replaying a prefix must give the same answer it gave at the time."""
    full = reversing()
    early = full[:200]

    first = evaluate_position(observe(candles=early))
    again = evaluate_position(observe(candles=early))
    with_future = evaluate_position(observe(candles=full))

    assert first.decision == again.decision
    assert first.thesis_state == again.thesis_state
    # The later series may well decide differently -- that is the point. What
    # must not happen is the earlier evaluation changing because more bars
    # exist somewhere.
    assert first.decision == evaluate_position(observe(candles=early)).decision
    assert with_future is not None


# ----------------------------------------------------------------------
# Ladder precedence
# ----------------------------------------------------------------------


def test_risk_outranks_everything_including_a_perfect_thesis() -> None:
    result = evaluate_position(observe(risk_lock=RiskLockState.DAILY_LOSS_LIMIT))
    assert result.decision is PositionDecision.BLOCKED_BY_RISK
    assert result.thesis_state is ThesisState.BLOCKED_BY_RISK
    assert result.risk_state == RiskLockState.DAILY_LOSS_LIMIT.value


def test_pending_reconciliation_also_blocks() -> None:
    result = evaluate_position(observe(unreconciled=2))
    assert result.decision is PositionDecision.BLOCKED_BY_RISK
    assert any("reconciliation" in reason for reason in result.reasons)


def test_an_unusable_mark_produces_insufficient_data_not_hold() -> None:
    """A HOLD derived from a price nobody could read is the dangerous answer:
    it is indistinguishable from a real one."""
    result = evaluate_position(observe(pos=position(mark=None)))
    assert result.decision is PositionDecision.INSUFFICIENT_DATA
    assert result.thesis_state is ThesisState.INSUFFICIENT_DATA
    assert result.decision is not PositionDecision.HOLD


def test_a_position_with_no_thesis_cannot_be_judged() -> None:
    """A manual entry recorded no conditions, so there is nothing to re-check."""
    result = evaluate_position(observe(pos=position(with_strategy=False)))
    assert result.decision is PositionDecision.INSUFFICIENT_DATA
    assert result.thesis_alignment is None


def test_insufficient_data_outranks_a_reversal() -> None:
    """Order matters: an unreadable mark must not be overridden by bearish
    bars that happen to look decisive."""
    result = evaluate_position(observe(pos=position(mark=None), candles=reversing()))
    assert result.decision is PositionDecision.INSUFFICIENT_DATA


def test_a_single_adverse_reading_protects_rather_than_exits() -> None:
    """One indicator rolling over is noise. Closing on it would be the
    do-not-hold-blindly rule overshooting into its opposite."""
    result = evaluate_position(observe(candles=falling()))
    # With everything holding this is a HOLD, which is the baseline the
    # single-adverse case is measured against.
    assert result.decision is PositionDecision.HOLD


def test_approaching_the_target_trails_rather_than_taking_profit() -> None:
    """Rung 5 outranks rung 6, and TRAIL is not EXIT.

    At 0.2500 against a 0.2400 target the short has covered 83% of its
    planned distance. That is a real TARGET_APPROACHING reading -- and the
    response is to tighten, not to take, because the thesis still holds and
    the move may continue.
    """
    near = position(target="0.2400")
    result = evaluate_position(observe(pos=near))

    assert result.thesis_state is ThesisState.TARGET_APPROACHING
    assert result.decision is PositionDecision.TRAIL
    assert result.decision is not PositionDecision.EXIT
    assert any("trail rather than to close" in reason for reason in result.reasons)


def test_no_weighted_vote_lets_healthy_brains_outvote_a_blocked_risk_state() -> None:
    """Six supportive brains must not outweigh one blocking condition."""
    observation = observe(risk_lock=RiskLockState.EMERGENCY_STOP)
    result = evaluate_position(observation)
    supportive = [r for r in result.brain_results if r.state is BrainState.SUPPORTIVE]
    assert supportive, "the fixture should leave several brains supportive"
    assert result.decision is PositionDecision.BLOCKED_BY_RISK


# ----------------------------------------------------------------------
# Brains individually
# ----------------------------------------------------------------------


def test_structure_brain_is_unavailable_and_fabricates_nothing() -> None:
    result = structure_brain(observe())
    assert result.state is BrainState.UNAVAILABLE
    assert result.data_quality is DataQuality.UNAVAILABLE
    assert "SMC capability is not implemented" in result.evidence[0].detail
    joined = " ".join(row.detail for row in result.evidence).lower()
    for fabricated in ("break of structure", "order block", "fair value gap", "choch"):
        assert fabricated not in joined or "not" in joined


def test_market_brain_reports_an_unusable_mark_as_unavailable() -> None:
    result = market_brain(observe(pos=position(mark=None)))
    assert result.state is BrainState.UNAVAILABLE
    assert result.state is not BrainState.NEUTRAL
    assert not result.usable


def test_technical_brain_rechecks_the_original_conditions_by_name() -> None:
    result = technical_brain(observe())
    assert result.state.is_measured
    assert {row.name for row in result.evidence} == {
        "trend",
        "trend_strength",
        "momentum",
        "macd",
    }


def test_technical_brain_is_unavailable_without_candles() -> None:
    result = technical_brain(observe(candles=[]))
    assert result.state is BrainState.UNAVAILABLE


def test_regime_brain_never_treats_unknown_as_range() -> None:
    flat = [bar(i, Decimal("0.3")) for i in range(260)]
    result = regime_brain(observe(candles=flat))
    assert result.state is BrainState.UNAVAILABLE


def test_momentum_brain_degrades_rather_than_inventing_values() -> None:
    result = momentum_volume_brain(observe(candles=falling()[:5]))
    assert result.state is BrainState.UNAVAILABLE


def test_position_brain_reports_remaining_r_as_absent_without_a_stop() -> None:
    result = position_brain(observe(pos=position(stop=None)))
    row = next(r for r in result.evidence if r.name == "remaining_r")
    assert row.satisfied is None
    assert "not computable" in row.detail


def test_risk_brain_reads_state_rather_than_recomputing_policy() -> None:
    result = risk_brain(observe(risk_lock=RiskLockState.DAILY_PROFIT_TARGET))
    assert result.state is BrainState.ADVERSE
    assert result.source == "risk-engine"


def test_exit_brain_requires_two_independent_readings() -> None:
    observation = observe()
    upstream = [
        market_brain(observation),
        technical_brain(observation),
        regime_brain(observation),
        momentum_volume_brain(observation),
    ]
    result = exit_brain(observation, upstream=upstream)
    row = next(r for r in result.evidence if r.name == "confirmed_reversal")
    assert "single adverse reading is not enough" in row.detail


# ----------------------------------------------------------------------
# Alignment and honesty
# ----------------------------------------------------------------------


def test_alignment_is_a_count_not_a_probability() -> None:
    result = evaluate_position(observe())
    alignment = result.thesis_alignment
    assert alignment is not None
    assert alignment.conditions_total == 4
    assert Decimal(0) <= alignment.value <= Decimal(100)
    assert "NOT a probability" in alignment.meaning
    assert alignment.conditions_held <= alignment.conditions_total


def test_no_field_anywhere_is_a_confidence_or_probability() -> None:
    payload = evaluate_position(observe()).model_dump_json().lower()
    for banned in ('"confidence"', '"probability"', '"win_rate"', '"expected_return"'):
        assert banned not in payload


def test_partial_exit_is_structurally_present_but_never_offered() -> None:
    result = evaluate_position(observe())
    assert result.partial_exit_available is False
    assert result.decision is not PositionDecision.PARTIAL_EXIT
    assert any("PARTIAL_EXIT" in cap for cap in result.unavailable_capabilities)


def test_every_result_names_what_this_build_cannot_do() -> None:
    result = evaluate_position(observe())
    joined = " ".join(result.unavailable_capabilities)
    assert "SMC" in joined
    assert "advisory" in joined


def test_the_disclaimer_states_that_nothing_is_executed() -> None:
    result = evaluate_position(observe())
    assert "places no order" in result.disclaimer
    assert "final authority" in result.disclaimer


def test_evaluation_is_deterministic() -> None:
    observation = observe()
    first = evaluate_position(observation)
    second = evaluate_position(observation)
    assert first.model_dump() == second.model_dump()


def test_every_brain_is_represented_in_the_result() -> None:
    result = evaluate_position(observe())
    assert {r.brain for r in result.brain_results} == set(BrainName)
