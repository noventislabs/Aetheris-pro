"""Market regime, setup scoring and risk/reward.

The series here are constructed, not sampled from a venue, and they are
constructed the way the existing strategy tests build theirs: a drift plus a
sine pullback, which is the smallest shape that produces a real directional
signal. A pure monotonic ramp does not -- it drives RSI past the overbought
band and the rule set correctly returns NEUTRAL -- and a test that used one
would be asserting against a case the strategy never calls.

Nothing here asserts a score is "good". The assertions are about arithmetic,
bounds, determinism, symmetry and refusal.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from aetheris.analysis.regime import (
    ADX_TREND_MINIMUM,
    ATR_HIGH_PERCENT,
    REGIME_METHOD,
    RegimeParams,
    classify_regime,
)
from aetheris.analysis.setup import (
    RR_REFERENCE,
    SETUP_METHOD,
    WEIGHTS,
    SetupParams,
    build_setup,
    derive_risk_reward,
)
from aetheris.domain.enums import Timeframe
from aetheris.domain.market import Candle
from aetheris.domain.regime import MarketRegime, VolatilityBand
from aetheris.domain.setup import SetupDirection, SetupStatus, StopModel

BASE = datetime(2026, 9, 20, tzinfo=UTC)


def bar(index: int, price: Decimal, volume: Decimal = Decimal(100)) -> Candle:
    start = BASE + timedelta(hours=index)
    return Candle(
        open_time=start,
        close_time=start + timedelta(hours=1, milliseconds=-1),
        open=price,
        high=price + Decimal("1.5"),
        low=price - Decimal("1.5"),
        close=price,
        volume=volume,
    )


def series(
    count: int, *, drift: str, amplitude: float, period: float, base: int = 200
) -> list[Candle]:
    """A drifting series with regular pullbacks."""
    out: list[Candle] = []
    for i in range(count):
        wave = Decimal(str(round(amplitude * math.sin(2 * math.pi * i / period), 4)))
        out.append(bar(i, Decimal(base) + Decimal(i) * Decimal(drift) + wave))
    return out


def long_series() -> list[Candle]:
    return series(220, drift="0.6", amplitude=10, period=12)


def short_series() -> list[Candle]:
    return series(220, drift="-0.4", amplitude=12, period=16, base=600)


def flat_series(count: int = 220) -> list[Candle]:
    """Perfectly flat. Has no directional movement, so ADX never warms up."""
    return [bar(i, Decimal(200)) for i in range(count)]


def choppy_series(count: int = 220) -> list[Candle]:
    """Moves enough for every indicator to warm up, but agrees on nothing."""
    return [bar(i, Decimal(200) + Decimal((i * 13) % 7)) for i in range(count)]


# ----------------------------------------------------------------------
# Regime
# ----------------------------------------------------------------------


def test_no_candles_is_unknown_not_range() -> None:
    """UNKNOWN and RANGE are different answers and must not collapse."""
    assessment = classify_regime([])
    assert assessment.regime is MarketRegime.UNKNOWN
    assert assessment.volatility_band is VolatilityBand.UNKNOWN
    assert assessment.actionable is False
    assert assessment.regime is not MarketRegime.RANGE


def test_an_unwarmed_series_is_unknown_and_names_what_was_missing() -> None:
    assessment = classify_regime([bar(i, Decimal(100) + Decimal(i)) for i in range(10)])
    assert assessment.regime is MarketRegime.UNKNOWN
    assert "could not be measured" in assessment.reason
    # The measurements are still reported, with None where nothing was taken.
    assert {m.name for m in assessment.measurements} == {
        "trend_strength",
        "trend_direction",
        "volatility",
        "band_width",
    }


def test_a_rising_series_classifies_as_trend_up() -> None:
    assessment = classify_regime(long_series())
    assert assessment.regime is MarketRegime.TREND_UP
    assert assessment.actionable is True
    assert assessment.method == REGIME_METHOD
    strength = next(m for m in assessment.measurements if m.name == "trend_strength")
    assert strength.value is not None and strength.value >= ADX_TREND_MINIMUM


def test_a_falling_series_classifies_as_trend_down() -> None:
    assert classify_regime(short_series()).regime is MarketRegime.TREND_DOWN


def test_the_volatility_axis_is_reported_even_while_trending() -> None:
    """Trend is the headline, but volatility is never discarded to fit it."""
    assessment = classify_regime(long_series())
    assert assessment.regime.is_trending
    assert assessment.volatility_band is not VolatilityBand.UNKNOWN


def test_a_flat_series_cannot_be_classified_at_all() -> None:
    """No directional movement means ADX never warms up. UNKNOWN, not RANGE.

    This is the distinction the enum exists for: "measured, and it is not
    trending" and "could not be measured" are different facts.
    """
    assessment = classify_regime(flat_series())
    assert assessment.regime is MarketRegime.UNKNOWN
    assert not assessment.regime.is_trending


def test_a_choppy_series_warms_up_but_is_not_trending() -> None:
    """The real RANGE-side case: everything measured, no trend found."""
    assessment = classify_regime(choppy_series())
    assert assessment.regime.is_known
    assert not assessment.regime.is_trending


def test_regime_is_deterministic() -> None:
    candles = long_series()
    first = classify_regime(candles)
    second = classify_regime(candles)
    assert first.model_dump() == second.model_dump()


def test_regime_params_reject_an_inverted_ema_pair() -> None:
    with pytest.raises(ValueError, match="ema_fast must be strictly less"):
        RegimeParams(ema_fast=55, ema_slow=21)


# ----------------------------------------------------------------------
# Risk / reward
# ----------------------------------------------------------------------


def test_risk_reward_is_asymmetric_by_direction() -> None:
    """A long stops below and targets above; a short does the reverse."""
    candles = long_series()
    params = SetupParams(stop_model=StopModel.FIXED_PERCENT, stop_percent=Decimal(2))

    long_rr = derive_risk_reward(
        candles, direction=SetupDirection.LONG, params=params, atr_percent=Decimal(1)
    )
    short_rr = derive_risk_reward(
        candles, direction=SetupDirection.SHORT, params=params, atr_percent=Decimal(1)
    )
    assert long_rr is not None and short_rr is not None
    assert long_rr.stop_price < long_rr.entry_price < long_rr.take_profit_price
    assert short_rr.take_profit_price < short_rr.entry_price < short_rr.stop_price
    # Same entry, same percentage: the risk magnitude must match exactly.
    assert long_rr.risk_per_unit == short_rr.risk_per_unit


def test_the_r_multiple_is_honoured_exactly() -> None:
    candles = long_series()
    params = SetupParams(
        stop_model=StopModel.FIXED_PERCENT, stop_percent=Decimal(2), take_profit_r=Decimal(3)
    )
    rr = derive_risk_reward(candles, direction=SetupDirection.LONG, params=params, atr_percent=None)
    assert rr is not None
    assert rr.reward_per_unit == rr.risk_per_unit * Decimal(3)
    assert rr.risk_reward_ratio == Decimal(3)


def test_no_signal_never_produces_levels() -> None:
    assert (
        derive_risk_reward(
            long_series(),
            direction=SetupDirection.NO_SIGNAL,
            params=SetupParams(),
            atr_percent=Decimal(1),
        )
        is None
    )


def test_an_atr_stop_refuses_when_atr_is_unavailable() -> None:
    """The ATR model has no fallback. A missing input is not a 2% default."""
    assert (
        derive_risk_reward(
            long_series(),
            direction=SetupDirection.LONG,
            params=SetupParams(stop_model=StopModel.ATR),
            atr_percent=None,
        )
        is None
    )


def test_a_structure_stop_on_the_wrong_side_is_refused_not_flipped() -> None:
    """A long whose recent swing low sits above the close has no stop here.

    The series rises monotonically to its final bar, so every prior low is
    above it for a short and below it for a long. The short case must refuse
    rather than silently place the stop on the profitable side.
    """
    rising = [bar(i, Decimal(100) + Decimal(i) * Decimal(5)) for i in range(60)]
    rr = derive_risk_reward(
        rising,
        direction=SetupDirection.SHORT,
        params=SetupParams(stop_model=StopModel.STRUCTURE, structure_lookback=20),
        atr_percent=Decimal(1),
    )
    assert rr is None


def test_setup_params_reject_out_of_range_values() -> None:
    with pytest.raises(ValueError, match="take_profit_r"):
        SetupParams(take_profit_r=Decimal(0))
    with pytest.raises(ValueError, match="atr_multiple"):
        SetupParams(atr_multiple=Decimal(-1))
    with pytest.raises(ValueError, match="stop_percent"):
        SetupParams(stop_percent=Decimal(95))


# ----------------------------------------------------------------------
# Setup construction and scoring
# ----------------------------------------------------------------------


def test_a_long_setup_is_actionable_and_carries_its_evidence() -> None:
    setup = build_setup(long_series(), symbol="testusdt", timeframe=Timeframe.H1)
    assert setup.status is SetupStatus.ACTIONABLE
    assert setup.direction is SetupDirection.LONG
    assert setup.score is not None
    assert setup.risk_reward is not None
    assert setup.regime is not None
    # All four rules held, and the other side's are reported too.
    assert len(setup.conditions) == 4
    assert all(condition.satisfied for condition in setup.conditions)
    assert len(setup.opposing_conditions) == 4


def test_a_short_setup_is_reachable_and_symmetric() -> None:
    """Long-only would pass every other test in this file. This one fails."""
    setup = build_setup(short_series(), symbol="TESTUSDT", timeframe=Timeframe.H1)
    assert setup.status is SetupStatus.ACTIONABLE
    assert setup.direction is SetupDirection.SHORT
    assert setup.risk_reward is not None
    assert setup.risk_reward.stop_price > setup.risk_reward.entry_price


def test_insufficient_candles_produce_no_direction() -> None:
    setup = build_setup(long_series()[:20], symbol="T", timeframe=Timeframe.H1)
    assert setup.status is SetupStatus.INSUFFICIENT_DATA
    assert setup.direction is SetupDirection.NO_SIGNAL
    assert setup.score is None
    assert setup.risk_reward is None


def test_conflicting_indicators_yield_no_actionable_setup() -> None:
    """Partial agreement is not a signal, and the detail says how partial."""
    setup = build_setup(choppy_series(), symbol="T", timeframe=Timeframe.H1)
    assert setup.status is SetupStatus.NO_ACTIONABLE_SETUP
    assert setup.direction is SetupDirection.NO_SIGNAL
    assert setup.score is None
    assert "/4" in setup.detail


def test_the_score_is_bounded_and_its_parts_sum_to_it() -> None:
    setup = build_setup(long_series(), symbol="T", timeframe=Timeframe.H1)
    assert setup.score is not None
    assert Decimal(0) <= setup.score.value <= Decimal(100)
    total = sum((c.contribution for c in setup.score.components), Decimal(0))
    assert abs(total - setup.score.value) <= Decimal("0.01")
    for component in setup.score.components:
        assert Decimal(0) <= component.normalized <= Decimal(1)


def test_every_weight_is_represented_exactly_once_and_they_sum_to_one() -> None:
    setup = build_setup(long_series(), symbol="T", timeframe=Timeframe.H1)
    assert setup.score is not None
    names = [component.name for component in setup.score.components]
    assert sorted(names) == sorted(WEIGHTS)
    assert len(names) == len(set(names))
    assert sum(WEIGHTS.values()) == Decimal(1)


def test_the_score_is_deterministic() -> None:
    candles = long_series()
    first = build_setup(candles, symbol="T", timeframe=Timeframe.H1)
    second = build_setup(candles, symbol="T", timeframe=Timeframe.H1)
    assert first.score is not None and second.score is not None
    assert first.score.value == second.score.value
    assert first.model_dump() == second.model_dump()


def test_the_score_is_versioned_and_says_what_it_is_not() -> None:
    setup = build_setup(long_series(), symbol="T", timeframe=Timeframe.H1)
    assert setup.score is not None
    assert setup.score.method == SETUP_METHOD
    assert "NOT a probability" in setup.score.meaning


def test_no_field_anywhere_names_itself_a_probability() -> None:
    """The naming rule, asserted rather than trusted to review.

    A field called ``win_probability`` would be a lie this system has no model
    to back, and the cheapest place to stop it is here.
    """
    setup = build_setup(long_series(), symbol="T", timeframe=Timeframe.H1)
    payload = setup.model_dump_json().lower()
    for banned in ('"profit_probability"', '"win_probability"', '"win_rate"', '"confidence"'):
        assert banned not in payload


def test_risk_reward_full_marks_require_the_reference_multiple() -> None:
    """The RR component is a real measurement, not a constant."""
    low = build_setup(
        long_series(),
        symbol="T",
        timeframe=Timeframe.H1,
        setup_params=SetupParams(take_profit_r=Decimal(1)),
    )
    high = build_setup(
        long_series(),
        symbol="T",
        timeframe=Timeframe.H1,
        setup_params=SetupParams(take_profit_r=RR_REFERENCE),
    )
    assert low.score is not None and high.score is not None
    low_rr = next(c for c in low.score.components if c.name == "risk_reward")
    high_rr = next(c for c in high.score.components if c.name == "risk_reward")
    assert high_rr.normalized == Decimal(1)
    assert low_rr.normalized < high_rr.normalized
    assert high.score.value > low.score.value


def test_a_setup_that_cannot_derive_a_stop_is_not_actionable() -> None:
    """Direction without levels must degrade, never invent a stop."""
    setup = build_setup(
        long_series(),
        symbol="T",
        timeframe=Timeframe.H1,
        setup_params=SetupParams(stop_model=StopModel.STRUCTURE, structure_lookback=2),
    )
    # Whatever the outcome, an ACTIONABLE result must carry real levels.
    if setup.status is SetupStatus.ACTIONABLE:
        assert setup.risk_reward is not None
        assert setup.risk_reward.stop_price != setup.risk_reward.entry_price
    else:
        assert setup.risk_reward is None


def test_provenance_survives_onto_the_setup() -> None:
    setup = build_setup(
        long_series(),
        symbol="T",
        timeframe=Timeframe.H1,
        data_source="BINANCE_REST",
        data_status="FRESH",
        data_age_seconds=0.42,
    )
    assert setup.data_source == "BINANCE_REST"
    assert setup.data_status == "FRESH"
    assert setup.data_age_seconds == 0.42
    assert setup.last_candle_time is not None


def test_volume_confirmation_reads_real_volume() -> None:
    """A volume surge on the signal bar must move the component, not sit fixed."""
    quiet = long_series()
    loud = list(quiet)
    last = loud[-1]
    loud[-1] = Candle(
        open_time=last.open_time,
        close_time=last.close_time,
        open=last.open,
        high=last.high,
        low=last.low,
        close=last.close,
        volume=last.volume * Decimal(5),
    )
    quiet_setup = build_setup(quiet, symbol="T", timeframe=Timeframe.H1)
    loud_setup = build_setup(loud, symbol="T", timeframe=Timeframe.H1)
    assert quiet_setup.score is not None and loud_setup.score is not None
    quiet_component = next(
        c for c in quiet_setup.score.components if c.name == "volume_confirmation"
    )
    loud_component = next(c for c in loud_setup.score.components if c.name == "volume_confirmation")
    assert loud_component.normalized > quiet_component.normalized


def test_high_volatility_reduces_the_fitness_component() -> None:
    """The volatility filter is a real taper, not a pass-through."""
    from aetheris.analysis.setup import _volatility_fitness

    inside, _ = _volatility_fitness(Decimal(2))
    extreme, _ = _volatility_fitness(ATR_HIGH_PERCENT * Decimal(3))
    assert inside == Decimal(1)
    assert extreme == Decimal(0)
    assert _volatility_fitness(None)[0] == Decimal(0)
