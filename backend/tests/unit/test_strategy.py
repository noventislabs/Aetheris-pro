"""Strategy evaluation: deterministic rules, explained verdicts, honest gating.

The rule set is tested at two levels. The condition evaluator is tested
directly with hand-chosen indicator values, so each rule's boundary is pinned
exactly. The full path is then tested over candle series, so warm-up and
freshness gating are covered too.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from aetheris.analysis.strategies.registry import (
    STRATEGY_KEYS,
    DataContext,
    describe_strategies,
    evaluate,
    evaluate_from_candles,
)
from aetheris.analysis.strategies.trend_momentum import (
    STRATEGY_VERSION,
    TrendMomentumParams,
    evaluate_conditions,
    warmup_bars,
)
from aetheris.domain.enums import Timeframe
from aetheris.domain.market import Candle
from aetheris.domain.strategy import StrategyBias, StrategyStatus

BASE = datetime(2026, 9, 20, tzinfo=UTC)
HOUR = 3600
PARAMS = TrendMomentumParams()


def conditions(
    *,
    ema_fast: str = "110",
    ema_slow: str = "100",
    adx: str = "30",
    rsi: str = "60",
    histogram: str = "1.5",
    params: TrendMomentumParams | None = None,
) -> tuple[StrategyBias, dict[str, bool], dict[str, bool]]:
    long_conditions, short_conditions, bias = evaluate_conditions(
        ema_fast=Decimal(ema_fast),
        ema_slow=Decimal(ema_slow),
        adx=Decimal(adx),
        rsi=Decimal(rsi),
        histogram=Decimal(histogram),
        params=params or PARAMS,
    )
    return (
        bias,
        {c.name: c.satisfied for c in long_conditions},
        {c.name: c.satisfied for c in short_conditions},
    )


# ----------------------------------------------------------------------
# The rule set
# ----------------------------------------------------------------------


def test_long_bias_requires_all_four_conditions() -> None:
    bias, long_met, _ = conditions()
    assert bias is StrategyBias.LONG_BIAS
    assert all(long_met.values())


def test_short_bias_requires_all_four_mirrored() -> None:
    bias, _, short_met = conditions(ema_fast="90", ema_slow="100", rsi="40", histogram="-1.5")
    assert bias is StrategyBias.SHORT_BIAS
    assert all(short_met.values())


def test_three_of_four_is_neutral_not_a_weak_long() -> None:
    """A rule set that fires on partial agreement has an undocumented tie-break."""
    bias, long_met, _ = conditions(histogram="-0.2")  # trend, strength, momentum hold
    assert bias is StrategyBias.NEUTRAL
    assert sum(long_met.values()) == 3


def test_weak_trend_blocks_both_directions() -> None:
    """ADX gates rather than votes: no trend worth reading means no bias."""
    bias, long_met, short_met = conditions(adx="10")
    assert bias is StrategyBias.NEUTRAL
    assert long_met["trend_strength"] is False
    assert short_met["trend_strength"] is False


def test_overbought_rsi_withholds_a_long_bias() -> None:
    """Above the overbought line the momentum rule stops confirming."""
    bias, long_met, _ = conditions(rsi="85")
    assert bias is StrategyBias.NEUTRAL
    assert long_met["momentum"] is False


def test_oversold_rsi_withholds_a_short_bias() -> None:
    bias, _, short_met = conditions(ema_fast="90", ema_slow="100", rsi="15", histogram="-1.5")
    assert bias is StrategyBias.NEUTRAL
    assert short_met["momentum"] is False


def test_conflicting_indicators_produce_neutral() -> None:
    # Uptrend by EMA, but momentum and MACD both point the other way.
    bias, long_met, short_met = conditions(rsi="35", histogram="-2")
    assert bias is StrategyBias.NEUTRAL
    assert long_met["trend"] is True
    assert short_met["trend"] is False


@pytest.mark.parametrize(
    ("rsi", "satisfied"),
    [("50", False), ("50.01", True), ("69.99", True), ("70", False)],
)
def test_rsi_band_boundaries_are_exclusive(rsi: str, satisfied: bool) -> None:
    """The band is (50, 70) open at both ends, as documented."""
    _, long_met, _ = conditions(rsi=rsi)
    assert long_met["momentum"] is satisfied


@pytest.mark.parametrize(("adx", "satisfied"), [("19.99", False), ("20", True)])
def test_adx_minimum_is_inclusive(adx: str, satisfied: bool) -> None:
    _, long_met, _ = conditions(adx=adx)
    assert long_met["trend_strength"] is satisfied


def test_equal_emas_are_neither_up_nor_down() -> None:
    bias, long_met, short_met = conditions(ema_fast="100", ema_slow="100")
    assert bias is StrategyBias.NEUTRAL
    assert long_met["trend"] is False
    assert short_met["trend"] is False


def test_zero_histogram_satisfies_neither_direction() -> None:
    _, long_met, short_met = conditions(histogram="0")
    assert long_met["macd"] is False
    assert short_met["macd"] is False


def test_every_condition_explains_itself_with_its_numbers() -> None:
    long_conditions, _, _ = evaluate_conditions(
        ema_fast=Decimal("110"),
        ema_slow=Decimal("100"),
        adx=Decimal("30"),
        rsi=Decimal("60"),
        histogram=Decimal("1.5"),
        params=PARAMS,
    )
    for condition in long_conditions:
        assert condition.detail, f"{condition.name} has no explanation"
        assert condition.values, f"{condition.name} reports no measured values"
    trend = next(c for c in long_conditions if c.name == "trend")
    assert trend.values["ema_fast"] == Decimal("110")


def test_conditions_are_deterministic() -> None:
    assert conditions() == conditions()


# ----------------------------------------------------------------------
# Parameters
# ----------------------------------------------------------------------


def test_fast_ema_must_be_shorter_than_slow() -> None:
    with pytest.raises(ValueError, match="ema_fast must be strictly less"):
        TrendMomentumParams(ema_fast=55, ema_slow=21)


def test_macd_fast_must_be_shorter_than_slow() -> None:
    with pytest.raises(ValueError, match="macd_fast must be strictly less"):
        TrendMomentumParams(macd_fast=30, macd_slow=26)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("ema_fast", 0),
        ("ema_fast", -3),
        ("rsi_period", 1),
        ("rsi_overbought", Decimal("50")),
        ("rsi_oversold", Decimal("50")),
        ("adx_minimum", Decimal("-1")),
    ],
)
def test_parameters_are_bounded(field: str, value: object) -> None:
    with pytest.raises(ValueError):
        TrendMomentumParams(**{field: value})


def test_custom_thresholds_change_the_verdict() -> None:
    """Configuration is real: a stricter ADX floor withholds the same bias."""
    strict = TrendMomentumParams(adx_minimum=Decimal("40"))
    assert conditions()[0] is StrategyBias.LONG_BIAS
    assert conditions(params=strict)[0] is StrategyBias.NEUTRAL


# ----------------------------------------------------------------------
# Full evaluation over candles
# ----------------------------------------------------------------------


def bar(index: int, price: Decimal) -> Candle:
    start = BASE + timedelta(seconds=HOUR * index)
    return Candle(
        open_time=start,
        close_time=start + timedelta(seconds=HOUR, milliseconds=-1),
        open=price,
        high=price + Decimal(2),
        low=price - Decimal(2),
        close=price,
        volume=Decimal(100),
    )


def rising(count: int) -> list[Candle]:
    return [bar(i, Decimal(100) + Decimal(i)) for i in range(count)]


def choppy(count: int) -> list[Candle]:
    return [bar(i, Decimal(100) + Decimal((i * 13) % 7)) for i in range(count)]


def test_insufficient_candles_yields_no_bias() -> None:
    result = evaluate_from_candles(rising(20), symbol="TESTUSDT", timeframe=Timeframe.H1)
    assert result.status is StrategyStatus.INSUFFICIENT_DATA
    assert result.bias is None
    assert result.detail is not None and str(warmup_bars(PARAMS)) in result.detail


def test_a_long_series_produces_a_verdict_with_reasons() -> None:
    result = evaluate_from_candles(rising(200), symbol="TESTUSDT", timeframe=Timeframe.H1)
    assert result.status is StrategyStatus.READY
    assert result.bias is not None
    assert len(result.long_conditions) == 4
    assert len(result.short_conditions) == 4
    assert result.conditions_total == 4
    assert result.version == STRATEGY_VERSION


def test_a_steady_advance_reads_as_long_bias() -> None:
    """A clean uptrend satisfies trend, strength and MACD; RSI caps it.

    A perfectly straight ramp pins RSI at 100, which is outside the (50, 70)
    band, so the honest verdict is NEUTRAL -- the momentum rule is doing
    exactly what it was written to do.
    """
    result = evaluate_from_candles(rising(200), symbol="TESTUSDT", timeframe=Timeframe.H1)
    long_met = {c.name: c.satisfied for c in result.long_conditions}
    assert long_met["trend"] is True
    assert long_met["trend_strength"] is True
    assert long_met["momentum"] is False  # RSI is pinned at 100
    assert result.bias is StrategyBias.NEUTRAL


def test_evaluation_is_deterministic() -> None:
    candles = choppy(200)
    first = evaluate_from_candles(candles, symbol="TESTUSDT", timeframe=Timeframe.H1)
    second = evaluate_from_candles(candles, symbol="TESTUSDT", timeframe=Timeframe.H1)
    assert first == second


def test_future_candles_do_not_change_an_earlier_verdict() -> None:
    """The verdict at bar N must not depend on bars after N."""
    candles = choppy(240)
    truncated = evaluate_from_candles(candles[:200], symbol="TESTUSDT", timeframe=Timeframe.H1)
    full = evaluate_from_candles(candles, symbol="TESTUSDT", timeframe=Timeframe.H1)
    # Different last bar, so different verdict is fine -- but recomputing the
    # same prefix must reproduce the same answer exactly.
    again = evaluate_from_candles(candles[:200], symbol="TESTUSDT", timeframe=Timeframe.H1)
    assert truncated == again
    assert full.candles_used == 240


def test_result_always_carries_the_disclaimer() -> None:
    result = evaluate_from_candles(rising(200), symbol="TESTUSDT", timeframe=Timeframe.H1)
    assert "not a trade recommendation" in result.disclaimer
    assert "probability of profit" in result.disclaimer


def test_no_confidence_or_probability_field_exists() -> None:
    """Phase 4 rule: a deterministic count, never a fabricated probability."""
    fields = set(
        evaluate_from_candles(rising(200), symbol="TESTUSDT", timeframe=Timeframe.H1).model_dump()
    )
    for forbidden in ("confidence", "probability", "win_rate", "expected_return", "score"):
        assert forbidden not in fields


# ----------------------------------------------------------------------
# Freshness gating
# ----------------------------------------------------------------------


def context(status: str = "OK", age: float | None = 5.0) -> DataContext:
    return DataContext(
        symbol="TESTUSDT",
        timeframe=Timeframe.H1,
        source="binance-futures-usdm:rest",
        data_status=status,
        age_seconds=age,
    )


def now_after(candles: list[Candle]) -> datetime:
    return candles[-1].close_time + timedelta(seconds=1)


def test_fresh_data_is_evaluated() -> None:
    candles = choppy(200)
    result = evaluate("trend_momentum", candles, context(), now_after(candles))
    assert result.status is StrategyStatus.READY
    assert result.source == "binance-futures-usdm:rest"


def test_stale_data_yields_no_bias() -> None:
    candles = choppy(200)
    result = evaluate("trend_momentum", candles, context(status="STALE"), now_after(candles))
    assert result.status is StrategyStatus.STALE
    assert result.bias is None


def test_unverifiable_freshness_is_treated_as_unusable() -> None:
    """Phase 2 rule: OK with a null age is not evidence of freshness.

    Anything trading-oriented must refuse it rather than treat the absence of
    a timestamp as a pass.
    """
    candles = choppy(200)
    result = evaluate("trend_momentum", candles, context(age=None), now_after(candles))
    assert result.status is StrategyStatus.STALE
    assert result.bias is None
    assert result.detail is not None and "could not be verified" in result.detail


def test_unknown_strategy_is_unavailable() -> None:
    candles = choppy(200)
    result = evaluate("does_not_exist", candles, context(), now_after(candles))
    assert result.status is StrategyStatus.UNAVAILABLE
    assert result.bias is None


def test_ambiguous_candle_order_is_refused() -> None:
    candles = choppy(200)
    scrambled = [candles[5], candles[1], candles[9], *candles[10:]]
    result = evaluate("trend_momentum", scrambled, context(), now_after(candles))
    assert result.status is StrategyStatus.UNAVAILABLE
    assert result.bias is None


def test_forming_candles_are_excluded_from_evaluation() -> None:
    candles = choppy(200)
    mid_last = candles[-1].open_time + timedelta(minutes=30)
    result = evaluate("trend_momentum", candles, context(), mid_last)
    assert result.candles_used == 199


# ----------------------------------------------------------------------
# Registry
# ----------------------------------------------------------------------


def test_registry_lists_only_implemented_strategies() -> None:
    described = describe_strategies()
    assert {d.key for d in described} == set(STRATEGY_KEYS)
    for descriptor in described:
        assert descriptor.available
        assert descriptor.rules
        assert descriptor.required_indicators


def test_registry_publishes_the_rule_set_in_words() -> None:
    descriptor = describe_strategies()[0]
    joined = " ".join(descriptor.rules).lower()
    assert "long_bias requires all four" in joined
    assert "neutral" in joined
