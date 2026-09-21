"""Indicator implementations.

Each function returns one or more series aligned index-for-index with the input
candles, with ``None`` wherever the indicator has not warmed up or is
mathematically undefined.

Every formula below follows a named, published convention, stated in the
docstring. Where more than one accepted definition exists the choice is made
explicitly rather than left to whichever happened to be written first -- an
RSI computed with an EMA instead of Wilder's smoothing is not a variant, it is
a different number that disagrees with every reference chart.
"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal
from typing import Final

from aetheris.analysis.indicators.core import (
    HUNDRED,
    ZERO,
    Series,
    closes,
    ema,
    highest,
    highs,
    lowest,
    lows,
    population_stdev,
    sma,
    true_ranges,
    typical_prices,
    volumes,
)
from aetheris.domain.market import Candle

__all__ = [
    "adx",
    "atr",
    "bollinger_bands",
    "cci",
    "ema_indicator",
    "macd",
    "roc",
    "rsi",
    "sma_indicator",
    "stochastic",
    "vwap",
]

#: Lambert's constant. CCI is defined with 0.015 so that roughly 70-80% of
#: values fall within +/-100; it is part of the formula, not a tunable.
_CCI_CONSTANT: Final = Decimal("0.015")


# ----------------------------------------------------------------------
# Trend
# ----------------------------------------------------------------------


def sma_indicator(candles: Sequence[Candle], *, period: int) -> dict[str, Series]:
    """Simple moving average of close.

    ``SMA_i = mean(close_{i-period+1} .. close_i)``. Warm-up: ``period - 1``
    bars; the first value lands on bar ``period``.
    """
    return {"sma": sma(closes(candles), period)}


def ema_indicator(candles: Sequence[Candle], *, period: int) -> dict[str, Series]:
    """Exponential moving average of close, SMA-seeded (see ``core.ema``)."""
    return {"ema": ema(closes(candles), period)}


# ----------------------------------------------------------------------
# Momentum
# ----------------------------------------------------------------------


def rsi(candles: Sequence[Candle], *, period: int) -> dict[str, Series]:
    """Relative Strength Index, Wilder's smoothing.

    ``RSI = 100 - 100 / (1 + avg_gain / avg_loss)`` where the averages use
    Wilder's smoothing over close-to-close changes. The first average is the
    arithmetic mean of the first ``period`` changes (bars 1..period), so the
    first RSI lands on bar ``period`` -- warm-up is ``period`` bars.

    Edge cases are defined rather than left to a division by zero:

    * ``avg_loss == 0`` and ``avg_gain > 0`` -> 100 (unbroken advance)
    * ``avg_gain == 0`` and ``avg_loss > 0`` -> 0 (unbroken decline)
    * both zero (a perfectly flat window) -> 50, the only defensible value
      when there is literally no relative strength either way, and documented
      as such rather than silently emitted
    """
    price = closes(candles)
    gains: Series = [None] * len(candles)
    losses: Series = [None] * len(candles)
    for index in range(1, len(price)):
        change = price[index] - price[index - 1]
        gains[index] = change if change > ZERO else ZERO
        losses[index] = -change if change < ZERO else ZERO

    from aetheris.analysis.indicators.core import wilder_average

    avg_gain = wilder_average(gains, period, start=1)
    avg_loss = wilder_average(losses, period, start=1)

    out: Series = [None] * len(candles)
    for index in range(len(candles)):
        gain, loss = avg_gain[index], avg_loss[index]
        if gain is None or loss is None:
            continue
        if loss == ZERO:
            out[index] = HUNDRED if gain > ZERO else Decimal(50)
        elif gain == ZERO:
            out[index] = ZERO
        else:
            out[index] = HUNDRED - (HUNDRED / (Decimal(1) + gain / loss))
    return {"rsi": out}


def macd(candles: Sequence[Candle], *, fast: int, slow: int, signal: int) -> dict[str, Series]:
    """Moving Average Convergence Divergence.

    Defaults 12 / 26 / 9. ``macd = EMA(fast) - EMA(slow)``;
    ``signal = EMA(signal) of the macd line``; ``histogram = macd - signal``.

    Both EMAs use the SMA-seeded convention from ``core.ema``, and the signal
    EMA is seeded from the first ``signal`` defined macd values. Warm-up is
    therefore ``slow - 1`` bars for the macd line and ``slow + signal - 2`` for
    the signal and histogram.
    """
    price = closes(candles)
    fast_ema = ema(price, fast)
    slow_ema = ema(price, slow)

    macd_line: Series = [None] * len(candles)
    for index in range(len(candles)):
        fast_value, slow_value = fast_ema[index], slow_ema[index]
        if fast_value is None or slow_value is None:
            continue
        macd_line[index] = fast_value - slow_value

    signal_line = ema(macd_line, signal)
    histogram: Series = [None] * len(candles)
    for index in range(len(candles)):
        macd_value, signal_value = macd_line[index], signal_line[index]
        if macd_value is None or signal_value is None:
            continue
        histogram[index] = macd_value - signal_value

    return {"macd": macd_line, "signal": signal_line, "histogram": histogram}


def stochastic(
    candles: Sequence[Candle], *, period: int, smooth_k: int, period_d: int
) -> dict[str, Series]:
    """Slow Stochastic Oscillator.

    ``raw %K = 100 * (close - lowest_low(period)) / (highest_high(period) - lowest_low(period))``
    ``%K = SMA(smooth_k) of raw %K``; ``%D = SMA(period_d) of %K``.

    Defaults 14 / 3 / 3 (the "slow" form). Warm-up:
    ``period + smooth_k - 2`` bars for %K and a further ``period_d - 1`` for %D.

    When the window's high equals its low the ratio is 0/0 -- genuinely
    undefined, not "50". That bar yields ``None``, and because an SMA window
    containing ``None`` also yields ``None``, the gap propagates honestly
    instead of being papered over.
    """
    high_values, low_values, close_values = highs(candles), lows(candles), closes(candles)
    raw_k: Series = [None] * len(candles)
    for index in range(len(candles)):
        window_high = highest(high_values, index, period)
        window_low = lowest(low_values, index, period)
        if window_high is None or window_low is None:
            continue
        span = window_high - window_low
        if span == ZERO:
            continue
        raw_k[index] = (close_values[index] - window_low) / span * HUNDRED

    k_line = sma(raw_k, smooth_k)
    d_line = sma(k_line, period_d)
    return {"k": k_line, "d": d_line}


def roc(candles: Sequence[Candle], *, period: int) -> dict[str, Series]:
    """Rate of Change, as a percentage.

    ``ROC_i = 100 * (close_i - close_{i-period}) / close_{i-period}``.
    Warm-up: ``period`` bars. A non-positive base price yields ``None`` -- a
    percentage change from zero has no value.
    """
    price = closes(candles)
    out: Series = [None] * len(candles)
    for index in range(period, len(price)):
        base = price[index - period]
        if base <= ZERO:
            continue
        out[index] = (price[index] - base) / base * HUNDRED
    return {"roc": out}


def cci(candles: Sequence[Candle], *, period: int) -> dict[str, Series]:
    """Commodity Channel Index.

    ``TP = (H + L + C) / 3``;
    ``CCI = (TP - SMA(TP, period)) / (0.015 * mean_deviation)``
    where the mean deviation is the mean absolute deviation of the window's
    typical prices from that same SMA. Warm-up: ``period - 1`` bars.

    A zero mean deviation (a perfectly flat window) makes the quotient
    undefined, and that bar yields ``None``.
    """
    typical = typical_prices(candles)
    typical_sma = sma(typical, period)
    out: Series = [None] * len(candles)
    for index in range(len(candles)):
        average = typical_sma[index]
        if average is None:
            continue
        window = typical[index - period + 1 : index + 1]
        deviation = sum((abs(value - average) for value in window), ZERO) / Decimal(period)
        if deviation == ZERO:
            continue
        out[index] = (typical[index] - average) / (_CCI_CONSTANT * deviation)
    return {"cci": out}


# ----------------------------------------------------------------------
# Volatility
# ----------------------------------------------------------------------


def atr(candles: Sequence[Candle], *, period: int) -> dict[str, Series]:
    """Average True Range, Wilder's smoothing.

    True range per ``core.true_ranges``; the first ATR is the arithmetic mean
    of true ranges over bars 1..period, then Wilder-smoothed. Warm-up:
    ``period`` bars.

    Note this is **not** the simple mean of true ranges the phase 3 scanner
    originally used; both now route through this function so one ATR
    convention exists across the product.
    """
    from aetheris.analysis.indicators.core import wilder_average

    return {"atr": wilder_average(true_ranges(candles), period, start=1)}


def bollinger_bands(
    candles: Sequence[Candle], *, period: int, deviations: Decimal
) -> dict[str, Series]:
    """Bollinger Bands.

    Middle band is ``SMA(close, period)``; the outer bands sit ``deviations``
    **population** standard deviations away (see ``core.population_stdev``).
    Defaults 20 / 2. Warm-up: ``period - 1`` bars.
    """
    price = closes(candles)
    middle = sma(price, period)
    upper: Series = [None] * len(candles)
    lower: Series = [None] * len(candles)
    for index in range(len(candles)):
        centre = middle[index]
        if centre is None:
            continue
        spread = population_stdev(price[index - period + 1 : index + 1]) * deviations
        upper[index] = centre + spread
        lower[index] = centre - spread
    return {"upper": upper, "middle": middle, "lower": lower}


# ----------------------------------------------------------------------
# Volume
# ----------------------------------------------------------------------


def vwap(candles: Sequence[Candle], *, period: int) -> dict[str, Series]:
    """Rolling Volume-Weighted Average Price.

    ``VWAP_i = sum(TP_j * volume_j) / sum(volume_j)`` over the ``period`` bars
    ending at *i*. Warm-up: ``period - 1`` bars.

    **Rolling, not session-anchored.** Textbook VWAP resets at the session
    open, but a perpetual future trades continuously and has no session
    boundary; anchoring to an arbitrary UTC midnight would make the value
    depend on when the chart happened to be loaded. A rolling window is
    well-defined at every bar and is what the parameter means.

    A window with zero total volume yields ``None`` -- there is no
    volume-weighted price when nothing traded.
    """
    typical = typical_prices(candles)
    volume = volumes(candles)
    out: Series = [None] * len(candles)
    for index in range(period - 1, len(candles)):
        window = range(index - period + 1, index + 1)
        total_volume = sum((volume[j] for j in window), ZERO)
        if total_volume <= ZERO:
            continue
        weighted = sum((typical[j] * volume[j] for j in window), ZERO)
        out[index] = weighted / total_volume
    return {"vwap": out}


# ----------------------------------------------------------------------
# Trend strength
# ----------------------------------------------------------------------


def adx(candles: Sequence[Candle], *, period: int) -> dict[str, Series]:
    """Average Directional Index, with +DI and -DI.

    Wilder's original definition throughout:

    * ``+DM_i = H_i - H_{i-1}`` when that exceeds both ``L_{i-1} - L_i`` and
      zero, else 0; ``-DM_i`` symmetrically.
    * ``+DM``, ``-DM`` and ``TR`` are each Wilder-smoothed over ``period``.
    * ``+DI = 100 * smoothed(+DM) / smoothed(TR)``; ``-DI`` likewise.
    * ``DX = 100 * |+DI - -DI| / (+DI + -DI)``.
    * ``ADX`` is the Wilder average of ``DX`` over ``period``.

    Warm-up: DI and DX at bar ``period``; ADX at bar ``2 * period - 1``,
    because ADX smooths a series that itself only begins at bar ``period``.

    ``+DI + -DI == 0`` (no directional movement at all in the window) leaves DX
    undefined for that bar, which propagates as ``None``.
    """
    from aetheris.analysis.indicators.core import wilder_average

    high_values, low_values = highs(candles), lows(candles)
    plus_dm: Series = [None] * len(candles)
    minus_dm: Series = [None] * len(candles)
    for index in range(1, len(candles)):
        up_move = high_values[index] - high_values[index - 1]
        down_move = low_values[index - 1] - low_values[index]
        plus_dm[index] = up_move if up_move > down_move and up_move > ZERO else ZERO
        minus_dm[index] = down_move if down_move > up_move and down_move > ZERO else ZERO

    smoothed_plus = wilder_average(plus_dm, period, start=1)
    smoothed_minus = wilder_average(minus_dm, period, start=1)
    smoothed_tr = wilder_average(true_ranges(candles), period, start=1)

    plus_di: Series = [None] * len(candles)
    minus_di: Series = [None] * len(candles)
    dx: Series = [None] * len(candles)
    for index in range(len(candles)):
        tr_value = smoothed_tr[index]
        plus_value = smoothed_plus[index]
        minus_value = smoothed_minus[index]
        if tr_value is None or plus_value is None or minus_value is None or tr_value == ZERO:
            continue
        positive = plus_value / tr_value * HUNDRED
        negative = minus_value / tr_value * HUNDRED
        plus_di[index] = positive
        minus_di[index] = negative
        total = positive + negative
        if total == ZERO:
            continue
        dx[index] = abs(positive - negative) / total * HUNDRED

    first_dx = next((i for i, value in enumerate(dx) if value is not None), None)
    adx_line: Series = (
        wilder_average(dx, period, start=first_dx)
        if first_dx is not None
        else [None] * len(candles)
    )
    return {"adx": adx_line, "plus_di": plus_di, "minus_di": minus_di}
