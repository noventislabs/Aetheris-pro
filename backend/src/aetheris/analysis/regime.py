"""Rule-based market-regime classification.

Pure: candles in, an assessment out. No HTTP, no venue, no settings, no model
weights — the same constraint the indicator and strategy layers carry.

**This is not the phase 9 AI capability.** ``ai.analysis`` in the capability
registry describes learned regime classification and anomaly detection, and
remains ``PLANNED``. What is here is arithmetic over published indicators with
published thresholds. It contains no model, no training data and no inference.

## The rules

Trend is decided first, because trend strength is the measurement that says
whether direction is worth reading at all:

```
UNKNOWN          when ADX, the EMA pair or ATR% could not be measured
TREND_UP         when ADX >= ADX_TREND_MINIMUM and EMA(fast) > EMA(slow)
TREND_DOWN       when ADX >= ADX_TREND_MINIMUM and EMA(fast) < EMA(slow)
HIGH_VOLATILITY  when not trending and ATR% >= ATR_HIGH_PERCENT
LOW_VOLATILITY   when not trending and ATR% <= ATR_LOW_PERCENT
RANGE            otherwise
```

``volatility_band`` is computed from the same ATR% on every path, so a trending
market still reports whether it is a calm trend or a violent one. Collapsing
that into the single regime field would throw the information away.

## The thresholds, and why they are judgement calls

These four constants are the only opinions in this module. They are stated here
rather than buried in the code so they can be argued with, and versioned so a
stored assessment is never compared against one produced by different numbers.

* ``ADX_TREND_MINIMUM = 20`` — the conventional reading for "a trend exists",
  and deliberately the same default the trend-momentum strategy already uses.
  Two different answers to "is this trending?" in one system would be worse
  than one debatable answer.
* ``ATR_HIGH_PERCENT = 3`` and ``ATR_LOW_PERCENT = Decimal("0.75")`` — ATR as a
  percentage of last close, per bar. Calibrated for USDT-M perpetuals, where a
  typical liquid contract sits between them on an hourly bar. They are *not*
  universal: on a daily timeframe almost everything reads HIGH, and on a 1m bar
  almost everything reads LOW. A caller comparing regimes across timeframes is
  comparing against a constant that was not chosen for both.
* ``BB_WIDTH_SQUEEZE_PERCENT = 2`` — band width as a percentage of the middle
  band. Reported as a measurement only; it does not currently decide a regime.
  It is measured because a squeeze is the clearest available precursor to a
  volatility expansion, and recording it now costs nothing.
"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal
from typing import Final

from aetheris.analysis.indicators.engine import calculate_indicators
from aetheris.analysis.indicators.registry import IndicatorParams
from aetheris.domain.indicators import IndicatorResult, IndicatorStatus
from aetheris.domain.market import Candle
from aetheris.domain.regime import (
    MarketRegime,
    RegimeAssessment,
    RegimeMeasurement,
    VolatilityBand,
)

__all__ = [
    "ADX_TREND_MINIMUM",
    "ATR_HIGH_PERCENT",
    "ATR_LOW_PERCENT",
    "BB_WIDTH_SQUEEZE_PERCENT",
    "REGIME_METHOD",
    "RegimeParams",
    "classify_regime",
]

#: Bump when any threshold or rule below changes.
REGIME_METHOD: Final = "market-regime/v1"

ADX_TREND_MINIMUM: Final = Decimal(20)
ATR_HIGH_PERCENT: Final = Decimal(3)
ATR_LOW_PERCENT: Final = Decimal("0.75")
BB_WIDTH_SQUEEZE_PERCENT: Final = Decimal(2)

_HUNDRED: Final = Decimal(100)


class RegimeParams:
    """Indicator periods the classifier reads.

    A plain object rather than a pydantic model: it holds no external input,
    only the periods a caller may vary, and every one of them is validated
    downstream by ``IndicatorParams``.
    """

    __slots__ = ("adx_period", "atr_period", "bb_period", "ema_fast", "ema_slow")

    def __init__(
        self,
        *,
        ema_fast: int = 21,
        ema_slow: int = 55,
        adx_period: int = 14,
        atr_period: int = 14,
        bb_period: int = 20,
    ) -> None:
        if ema_fast >= ema_slow:
            raise ValueError("ema_fast must be strictly less than ema_slow")
        self.ema_fast = ema_fast
        self.ema_slow = ema_slow
        self.adx_period = adx_period
        self.atr_period = atr_period
        self.bb_period = bb_period


def _latest(result: IndicatorResult, key: str) -> Decimal | None:
    if result.status is not IndicatorStatus.READY or result.latest is None:
        return None
    return result.latest.get(key)


def _volatility_band(atr_percent: Decimal | None) -> VolatilityBand:
    if atr_percent is None:
        return VolatilityBand.UNKNOWN
    if atr_percent >= ATR_HIGH_PERCENT:
        return VolatilityBand.HIGH
    if atr_percent <= ATR_LOW_PERCENT:
        return VolatilityBand.LOW
    return VolatilityBand.NORMAL


def classify_regime(
    candles: Sequence[Candle], params: RegimeParams | None = None
) -> RegimeAssessment:
    """Classify the market state at the last candle.

    Reads only closed candles that were handed in. It cannot see forward
    because it is never given anything to see forward into -- the caller
    decides what the series contains, which is what makes this usable
    unchanged inside a backtest loop.

    Returns ``UNKNOWN`` rather than guessing whenever a required input has not
    warmed up. An unavailable measurement is never replaced by a default.
    """
    params = params or RegimeParams()
    measurements: list[RegimeMeasurement] = []

    if not candles:
        return RegimeAssessment(
            regime=MarketRegime.UNKNOWN,
            volatility_band=VolatilityBand.UNKNOWN,
            reason="No candles were supplied, so nothing could be measured.",
            method=REGIME_METHOD,
            candles_used=0,
        )

    fast_params = IndicatorParams(
        ema_period=params.ema_fast,
        adx_period=params.adx_period,
        atr_period=params.atr_period,
        bb_period=params.bb_period,
    )
    slow_params = IndicatorParams(ema_period=params.ema_slow)

    computed = calculate_indicators(candles, ("ema", "adx", "atr", "bollinger"), fast_params)
    by_key = {result.indicator: result for result in computed}
    slow_result = calculate_indicators(candles, ("ema",), slow_params)[0]

    ema_fast = _latest(by_key["ema"], "ema")
    ema_slow = _latest(slow_result, "ema")
    adx_value = _latest(by_key["adx"], "adx")
    atr_value = _latest(by_key["atr"], "atr")
    last_close = candles[-1].close

    atr_percent: Decimal | None = None
    if atr_value is not None and last_close > 0:
        atr_percent = atr_value / last_close * _HUNDRED

    upper = _latest(by_key["bollinger"], "upper")
    lower = _latest(by_key["bollinger"], "lower")
    middle = _latest(by_key["bollinger"], "middle")
    bb_width: Decimal | None = None
    if upper is not None and lower is not None and middle is not None and middle > 0:
        bb_width = (upper - lower) / middle * _HUNDRED

    measurements.append(
        RegimeMeasurement(
            name="trend_strength",
            value=adx_value,
            threshold=ADX_TREND_MINIMUM,
            detail=(
                f"ADX {adx_value:.2f} against a trend minimum of {ADX_TREND_MINIMUM}"
                if adx_value is not None
                else "ADX has not warmed up over the supplied candles"
            ),
        )
    )
    measurements.append(
        RegimeMeasurement(
            name="trend_direction",
            value=ema_fast,
            threshold=ema_slow,
            detail=(
                f"EMA({params.ema_fast}) {ema_fast:.8f} against "
                f"EMA({params.ema_slow}) {ema_slow:.8f}"
                if ema_fast is not None and ema_slow is not None
                else "One or both EMA legs have not warmed up"
            ),
        )
    )
    measurements.append(
        RegimeMeasurement(
            name="volatility",
            value=atr_percent,
            threshold=ATR_HIGH_PERCENT,
            detail=(
                f"ATR is {atr_percent:.4f}% of last close; high at "
                f"{ATR_HIGH_PERCENT}%, low at {ATR_LOW_PERCENT}%"
                if atr_percent is not None
                else "ATR has not warmed up, or the last close is not positive"
            ),
        )
    )
    measurements.append(
        RegimeMeasurement(
            name="band_width",
            value=bb_width,
            threshold=BB_WIDTH_SQUEEZE_PERCENT,
            detail=(
                f"Bollinger width is {bb_width:.4f}% of the middle band; "
                f"a squeeze reads below {BB_WIDTH_SQUEEZE_PERCENT}%"
                if bb_width is not None
                else "Bollinger Bands have not warmed up"
            ),
        )
    )

    band = _volatility_band(atr_percent)
    frozen = tuple(measurements)

    if adx_value is None or ema_fast is None or ema_slow is None or atr_percent is None:
        missing = [
            name
            for name, value in (
                ("ADX", adx_value),
                ("EMA pair", ema_fast if ema_slow is not None else None),
                ("ATR", atr_percent),
            )
            if value is None
        ]
        return RegimeAssessment(
            regime=MarketRegime.UNKNOWN,
            volatility_band=band,
            measurements=frozen,
            reason=(
                f"Unclassified: {', '.join(missing)} could not be measured over "
                f"{len(candles)} candles. An unmeasured input is never defaulted."
            ),
            method=REGIME_METHOD,
            candles_used=len(candles),
        )

    trending = adx_value >= ADX_TREND_MINIMUM
    if trending and ema_fast > ema_slow:
        return RegimeAssessment(
            regime=MarketRegime.TREND_UP,
            volatility_band=band,
            measurements=frozen,
            reason=(
                f"ADX {adx_value:.2f} meets the {ADX_TREND_MINIMUM} minimum and the fast "
                f"EMA is above the slow one, so direction is readable and upward."
            ),
            method=REGIME_METHOD,
            candles_used=len(candles),
        )
    if trending and ema_fast < ema_slow:
        return RegimeAssessment(
            regime=MarketRegime.TREND_DOWN,
            volatility_band=band,
            measurements=frozen,
            reason=(
                f"ADX {adx_value:.2f} meets the {ADX_TREND_MINIMUM} minimum and the fast "
                f"EMA is below the slow one, so direction is readable and downward."
            ),
            method=REGIME_METHOD,
            candles_used=len(candles),
        )

    if atr_percent >= ATR_HIGH_PERCENT:
        regime = MarketRegime.HIGH_VOLATILITY
        reason = (
            f"No readable trend (ADX {adx_value:.2f} below {ADX_TREND_MINIMUM}), and ATR "
            f"at {atr_percent:.4f}% of price is at or above the {ATR_HIGH_PERCENT}% "
            f"high-volatility threshold."
        )
    elif atr_percent <= ATR_LOW_PERCENT:
        regime = MarketRegime.LOW_VOLATILITY
        reason = (
            f"No readable trend (ADX {adx_value:.2f} below {ADX_TREND_MINIMUM}), and ATR "
            f"at {atr_percent:.4f}% of price is at or below the {ATR_LOW_PERCENT}% "
            f"low-volatility threshold."
        )
    else:
        regime = MarketRegime.RANGE
        reason = (
            f"No readable trend (ADX {adx_value:.2f} below {ADX_TREND_MINIMUM}) and ATR "
            f"at {atr_percent:.4f}% of price sits between the volatility thresholds."
        )

    return RegimeAssessment(
        regime=regime,
        volatility_band=band,
        measurements=frozen,
        reason=reason,
        method=REGIME_METHOD,
        candles_used=len(candles),
    )
