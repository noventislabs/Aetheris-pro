"""Shared primitives for indicator maths.

**Numeric type.** Everything here is ``Decimal``, matching the rest of the
system. The alternative -- float for indicators, Decimal for money -- was
rejected deliberately: the boundary between "an indicator value" and "a number
that reaches a position size" is exactly where a silent precision loss would
hide, and this engine is designed to feed the phase 5 backtester and later the
risk engine. The cost is bounded: a 1500-candle series is a few thousand
Decimal operations, microseconds on the target hardware.

Where a value is mathematically undefined -- a standard deviation of a
zero-width window, a division by a zero range -- these functions return
``None``. They never substitute a neutral-looking default such as 50 or 0,
because an invented midpoint is indistinguishable downstream from a measured
one.

**Alignment.** Every function returns a list the same length as its input, with
``None`` in the positions where the indicator has not yet warmed up. Index *i*
of the output always corresponds to candle *i* of the input, so nothing can
drift by one and silently introduce look-ahead.

**No look-ahead.** Every value at index *i* is computed from inputs at indices
``<= i``. This is enforced by construction -- the loops only ever read
backwards -- and asserted by a dedicated regression test that recomputes each
indicator on truncated prefixes.
"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal
from typing import Final

from aetheris.domain.market import Candle

__all__ = [
    "closes",
    "ema",
    "highest",
    "highs",
    "lowest",
    "lows",
    "population_stdev",
    "sma",
    "true_ranges",
    "typical_prices",
    "volumes",
    "wilder_average",
]

ZERO: Final = Decimal(0)
HUNDRED: Final = Decimal(100)
THREE: Final = Decimal(3)

Series = list[Decimal | None]


# ----------------------------------------------------------------------
# Extraction
# ----------------------------------------------------------------------


def closes(candles: Sequence[Candle]) -> list[Decimal]:
    return [candle.close for candle in candles]


def highs(candles: Sequence[Candle]) -> list[Decimal]:
    return [candle.high for candle in candles]


def lows(candles: Sequence[Candle]) -> list[Decimal]:
    return [candle.low for candle in candles]


def volumes(candles: Sequence[Candle]) -> list[Decimal]:
    return [candle.volume for candle in candles]


def typical_prices(candles: Sequence[Candle]) -> list[Decimal]:
    """(High + Low + Close) / 3, the standard typical price."""
    return [(c.high + c.low + c.close) / THREE for c in candles]


# ----------------------------------------------------------------------
# Moving averages
# ----------------------------------------------------------------------


def sma(values: Sequence[Decimal | None], period: int) -> Series:
    """Simple moving average.

    First value lands at index ``period - 1``. A window containing any ``None``
    yields ``None``: a partial average over a shorter window would be a
    different statistic wearing the same label.
    """
    if period < 1:
        raise ValueError("period must be at least 1")
    out: Series = [None] * len(values)
    for index in range(period - 1, len(values)):
        window = values[index - period + 1 : index + 1]
        if any(value is None for value in window):
            continue
        total = ZERO
        for value in window:
            assert value is not None  # narrowed by the guard above
            total += value
        out[index] = total / Decimal(period)
    return out


def ema(values: Sequence[Decimal | None], period: int) -> Series:
    """Exponential moving average.

    **Initialisation convention:** seeded with the simple moving average of the
    first ``period`` values, so the first EMA lands at index ``period - 1``.
    Thereafter ``EMA_i = value_i * k + EMA_{i-1} * (1 - k)`` with
    ``k = 2 / (period + 1)``.

    This is the convention used by TA-Lib and by most charting platforms. The
    alternative -- seeding with the first value alone -- converges to the same
    curve but differs materially over the first few dozen bars, which is
    exactly the range a short series lives in.
    """
    if period < 1:
        raise ValueError("period must be at least 1")
    out: Series = [None] * len(values)
    multiplier = Decimal(2) / Decimal(period + 1)

    # Find the first window of `period` consecutive non-None values.
    seed_end: int | None = None
    run = 0
    for index, value in enumerate(values):
        run = run + 1 if value is not None else 0
        if run == period:
            seed_end = index
            break
    if seed_end is None:
        return out

    total = ZERO
    for value in values[seed_end - period + 1 : seed_end + 1]:
        assert value is not None
        total += value
    previous = total / Decimal(period)
    out[seed_end] = previous

    for index in range(seed_end + 1, len(values)):
        value = values[index]
        if value is None:
            # A gap breaks the recursion; the series cannot resume without
            # re-seeding, so the remainder stays undefined rather than
            # silently carrying the stale average forward.
            break
        previous = value * multiplier + previous * (Decimal(1) - multiplier)
        out[index] = previous
    return out


def wilder_average(values: Sequence[Decimal | None], period: int, *, start: int) -> Series:
    """Wilder's smoothing (a.k.a. RMA / SMMA).

    Seeded at index ``start + period - 1`` with the arithmetic mean of the
    ``period`` values beginning at ``start``, then
    ``avg_i = (avg_{i-1} * (period - 1) + value_i) / period``.

    Wilder's own indicators -- RSI, ATR, ADX -- are defined with this
    smoothing, not with an EMA. They are numerically different (Wilder's is
    equivalent to an EMA of period ``2n - 1``), so using one where the other is
    specified produces values that disagree with every reference
    implementation.
    """
    if period < 1:
        raise ValueError("period must be at least 1")
    out: Series = [None] * len(values)
    seed_end = start + period - 1
    if seed_end >= len(values):
        return out

    total = ZERO
    for value in values[start : seed_end + 1]:
        if value is None:
            return out
        total += value
    previous = total / Decimal(period)
    out[seed_end] = previous

    period_d = Decimal(period)
    for index in range(seed_end + 1, len(values)):
        value = values[index]
        if value is None:
            break
        previous = (previous * (period_d - Decimal(1)) + value) / period_d
        out[index] = previous
    return out


# ----------------------------------------------------------------------
# Dispersion and ranges
# ----------------------------------------------------------------------


def population_stdev(window: Sequence[Decimal]) -> Decimal:
    """Population standard deviation.

    Population (divide by N), not sample (divide by N-1). Bollinger's definition treats the
    window as the whole population, and TA-Lib does the same; mixing the two
    would shift every band by a few percent against any reference chart.
    """
    count = Decimal(len(window))
    mean = sum(window, ZERO) / count
    variance = sum(((value - mean) ** 2 for value in window), ZERO) / count
    return variance.sqrt() if variance > ZERO else ZERO


def true_ranges(candles: Sequence[Candle]) -> Series:
    """Wilder's true range.

    ``TR_i = max(H_i - L_i, |H_i - C_{i-1}|, |L_i - C_{i-1}|)``

    Index 0 is ``None``: with no previous close, only one of the three terms
    exists, and substituting the bare high-low range would mix two definitions
    into one average.
    """
    out: Series = [None] * len(candles)
    for index in range(1, len(candles)):
        current = candles[index]
        previous_close = candles[index - 1].close
        out[index] = max(
            current.high - current.low,
            abs(current.high - previous_close),
            abs(current.low - previous_close),
        )
    return out


def highest(values: Sequence[Decimal], index: int, period: int) -> Decimal | None:
    """Highest value in the ``period`` bars ending at ``index`` inclusive."""
    if index + 1 < period:
        return None
    return max(values[index - period + 1 : index + 1])


def lowest(values: Sequence[Decimal], index: int, period: int) -> Decimal | None:
    if index + 1 < period:
        return None
    return min(values[index - period + 1 : index + 1])
