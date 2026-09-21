"""Trend-Momentum Confluence -- the baseline strategy.

**What it does.** Reads four indicators and reports whether they currently
agree on a direction. Nothing more. It does not forecast, score or recommend.

**Why these four.** Each answers a different question, so agreement between
them is informative and disagreement is the honest NEUTRAL:

* **EMA fast vs slow** -- which direction is the trend?
* **ADX** -- is there a trend worth reading at all? ADX measures strength
  without direction, so it acts as a gate rather than a vote. Without it the
  EMA cross alone produces a constant stream of flip-flopping bias in a
  ranging market.
* **RSI** -- is momentum behind that direction, and not already exhausted?
  The band is deliberately two-sided: above 50 confirms a long-side push,
  below the overbought line avoids calling a bias at the point momentum
  historically stalls.
* **MACD histogram** -- is the momentum still accelerating, independent of
  the EMA pair's own periods?

**The rule set.** All four must agree for a directional bias. Anything else --
including three of four -- is ``NEUTRAL``. A rule set that fires on partial
agreement is a rule set with an undocumented tie-break, and this one has none.

```
LONG_BIAS   when  EMA(fast) > EMA(slow)
                  AND ADX >= adx_minimum
                  AND 50 < RSI < rsi_overbought
                  AND MACD histogram > 0

SHORT_BIAS  when  EMA(fast) < EMA(slow)
                  AND ADX >= adx_minimum
                  AND rsi_oversold < RSI < 50
                  AND MACD histogram < 0

NEUTRAL     otherwise
```

Both directions are always evaluated and both sets of conditions are returned,
so a reader can see not only what the verdict was but how close the other side
came to it.
"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal
from typing import Final

from pydantic import BaseModel, ConfigDict, Field, model_validator

from aetheris.analysis.indicators.engine import calculate_indicators
from aetheris.analysis.indicators.registry import IndicatorParams
from aetheris.domain.indicators import IndicatorResult, IndicatorStatus
from aetheris.domain.market import Candle
from aetheris.domain.strategy import ConditionOutcome, StrategyBias

STRATEGY_KEY: Final = "trend_momentum"
STRATEGY_NAME: Final = "Trend-Momentum Confluence"
#: Bumped whenever a rule or a default changes, so a stored result is never
#: compared against a verdict produced by different rules.
STRATEGY_VERSION: Final = "1.0.0"

REQUIRED_INDICATORS: Final[tuple[str, ...]] = ("ema", "adx", "rsi", "macd")

_FIFTY: Final = Decimal(50)


class TrendMomentumParams(BaseModel):
    """Strategy configuration. Every value bounded and validated."""

    model_config = ConfigDict(frozen=True)

    ema_fast: int = Field(default=21, ge=2, le=200)
    ema_slow: int = Field(default=55, ge=3, le=400)
    rsi_period: int = Field(default=14, ge=2, le=200)
    rsi_overbought: Decimal = Field(default=Decimal("70"), gt=50, lt=100)
    rsi_oversold: Decimal = Field(default=Decimal("30"), gt=0, lt=50)
    adx_period: int = Field(default=14, ge=2, le=200)
    adx_minimum: Decimal = Field(default=Decimal("20"), ge=0, le=100)
    macd_fast: int = Field(default=12, ge=2, le=200)
    macd_slow: int = Field(default=26, ge=3, le=400)
    macd_signal: int = Field(default=9, ge=2, le=200)

    @model_validator(mode="after")
    def _periods_are_ordered(self) -> TrendMomentumParams:
        """A "fast" average slower than the "slow" one inverts every rule.

        Rejected rather than reordered, because silently swapping them would
        answer a different question from the one the caller asked.
        """
        if self.ema_fast >= self.ema_slow:
            raise ValueError("ema_fast must be strictly less than ema_slow")
        if self.macd_fast >= self.macd_slow:
            raise ValueError("macd_fast must be strictly less than macd_slow")
        return self

    def as_decimals(self) -> dict[str, Decimal]:
        return {
            "ema_fast": Decimal(self.ema_fast),
            "ema_slow": Decimal(self.ema_slow),
            "rsi_period": Decimal(self.rsi_period),
            "rsi_overbought": self.rsi_overbought,
            "rsi_oversold": self.rsi_oversold,
            "adx_period": Decimal(self.adx_period),
            "adx_minimum": self.adx_minimum,
            "macd_fast": Decimal(self.macd_fast),
            "macd_slow": Decimal(self.macd_slow),
            "macd_signal": Decimal(self.macd_signal),
        }


def required_indicator_params(
    params: TrendMomentumParams,
) -> tuple[IndicatorParams, IndicatorParams]:
    """Indicator parameter sets for the fast and slow EMA legs.

    Two are needed because the indicator registry exposes one EMA period at a
    time; the strategy needs both legs of the pair from the same candles.
    """

    def build(ema_period: int) -> IndicatorParams:
        return IndicatorParams(
            ema_period=ema_period,
            rsi_period=params.rsi_period,
            adx_period=params.adx_period,
            macd_fast=params.macd_fast,
            macd_slow=params.macd_slow,
            macd_signal=params.macd_signal,
        )

    return build(params.ema_fast), build(params.ema_slow)


def warmup_bars(params: TrendMomentumParams) -> int:
    """Bars needed before every required indicator has a value.

    The maximum of the individual warm-ups: the strategy is only ready when
    its slowest input is.
    """
    return max(
        params.ema_slow - 1,
        params.rsi_period,
        2 * params.adx_period - 1,
        params.macd_slow + params.macd_signal - 2,
    )


def _latest(result: IndicatorResult, key: str) -> Decimal | None:
    if result.status is not IndicatorStatus.READY or result.latest is None:
        return None
    return result.latest.get(key)


def evaluate_conditions(
    *,
    ema_fast: Decimal,
    ema_slow: Decimal,
    adx: Decimal,
    rsi: Decimal,
    histogram: Decimal,
    params: TrendMomentumParams,
) -> tuple[tuple[ConditionOutcome, ...], tuple[ConditionOutcome, ...], StrategyBias]:
    """Apply the rule set to one bar's indicator values.

    Pure and total: given the same numbers it always returns the same verdict,
    and it has no access to anything but those numbers.
    """
    trend_up = ema_fast > ema_slow
    trend_down = ema_fast < ema_slow
    has_trend = adx >= params.adx_minimum
    bullish_momentum = _FIFTY < rsi < params.rsi_overbought
    bearish_momentum = params.rsi_oversold < rsi < _FIFTY

    long_conditions = (
        ConditionOutcome(
            name="trend",
            satisfied=trend_up,
            detail=(
                f"EMA({params.ema_fast}) {'is above' if trend_up else 'is not above'} "
                f"EMA({params.ema_slow})"
            ),
            values={"ema_fast": ema_fast, "ema_slow": ema_slow},
        ),
        ConditionOutcome(
            name="trend_strength",
            satisfied=has_trend,
            detail=(
                f"ADX {adx:.2f} {'meets' if has_trend else 'is below'} the minimum "
                f"{params.adx_minimum}"
            ),
            values={"adx": adx, "adx_minimum": params.adx_minimum},
        ),
        ConditionOutcome(
            name="momentum",
            satisfied=bullish_momentum,
            detail=(
                f"RSI {rsi:.2f} is {'within' if bullish_momentum else 'outside'} "
                f"the bullish band (50, {params.rsi_overbought})"
            ),
            values={"rsi": rsi, "upper": params.rsi_overbought},
        ),
        ConditionOutcome(
            name="macd",
            satisfied=histogram > 0,
            detail=(
                f"MACD histogram {histogram:.6f} is "
                f"{'positive' if histogram > 0 else 'not positive'}"
            ),
            values={"histogram": histogram},
        ),
    )

    short_conditions = (
        ConditionOutcome(
            name="trend",
            satisfied=trend_down,
            detail=(
                f"EMA({params.ema_fast}) {'is below' if trend_down else 'is not below'} "
                f"EMA({params.ema_slow})"
            ),
            values={"ema_fast": ema_fast, "ema_slow": ema_slow},
        ),
        ConditionOutcome(
            name="trend_strength",
            satisfied=has_trend,
            detail=(
                f"ADX {adx:.2f} {'meets' if has_trend else 'is below'} the minimum "
                f"{params.adx_minimum}"
            ),
            values={"adx": adx, "adx_minimum": params.adx_minimum},
        ),
        ConditionOutcome(
            name="momentum",
            satisfied=bearish_momentum,
            detail=(
                f"RSI {rsi:.2f} is {'within' if bearish_momentum else 'outside'} "
                f"the bearish band ({params.rsi_oversold}, 50)"
            ),
            values={"rsi": rsi, "lower": params.rsi_oversold},
        ),
        ConditionOutcome(
            name="macd",
            satisfied=histogram < 0,
            detail=(
                f"MACD histogram {histogram:.6f} is "
                f"{'negative' if histogram < 0 else 'not negative'}"
            ),
            values={"histogram": histogram},
        ),
    )

    if all(condition.satisfied for condition in long_conditions):
        bias = StrategyBias.LONG_BIAS
    elif all(condition.satisfied for condition in short_conditions):
        bias = StrategyBias.SHORT_BIAS
    else:
        bias = StrategyBias.NEUTRAL
    return long_conditions, short_conditions, bias


def compute_indicator_values(
    candles: Sequence[Candle], params: TrendMomentumParams
) -> dict[str, Decimal] | None:
    """Extract the four values the rule set needs, or ``None`` if any is unready.

    All-or-nothing on purpose: evaluating three rules and guessing the fourth
    would produce a verdict the published rule set does not describe.
    """
    fast_params, slow_params = required_indicator_params(params)

    fast_results = calculate_indicators(candles, ("ema", "adx", "rsi", "macd"), fast_params)
    slow_results = calculate_indicators(candles, ("ema",), slow_params)

    by_key = {result.indicator: result for result in fast_results}
    ema_fast = _latest(by_key["ema"], "ema")
    ema_slow = _latest(slow_results[0], "ema")
    adx_value = _latest(by_key["adx"], "adx")
    rsi_value = _latest(by_key["rsi"], "rsi")
    histogram = _latest(by_key["macd"], "histogram")

    if any(value is None for value in (ema_fast, ema_slow, adx_value, rsi_value, histogram)):
        return None

    assert ema_fast is not None and ema_slow is not None
    assert adx_value is not None and rsi_value is not None and histogram is not None
    return {
        "ema_fast": ema_fast,
        "ema_slow": ema_slow,
        "adx": adx_value,
        "rsi": rsi_value,
        "histogram": histogram,
    }


def measure_atr_percent(candles: Sequence[Candle], params: TrendMomentumParams) -> Decimal | None:
    """ATR as a percentage of the last close, for the leverage candidate only.

    Deliberately not part of the bias rule set: adding it there would change a
    published, versioned rule set as a side effect of a separate feature. It is
    measured here purely so volatility can size a leverage *request*.
    """
    if not candles:
        return None
    atr_params = IndicatorParams(atr_period=params.adx_period)
    result = calculate_indicators(candles, ("atr",), atr_params)[0]
    atr_value = _latest(result, "atr")
    last_close = candles[-1].close
    if atr_value is None or last_close <= 0:
        return None
    return atr_value / last_close * Decimal(100)
