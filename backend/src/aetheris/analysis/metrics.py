"""Deterministic statistics over OHLCV candles.

A small, focused set of measurements the scanner needs -- not a general
indicator engine, which belongs to phase 4. Everything here is pure: given the
same candles and the same clock it returns the same numbers, with no I/O, no
randomness and no venue knowledge.

Two properties matter for correctness:

**Closed bars only.** The newest bar a venue returns is normally still forming,
so its volume is a partial count and its high/low are incomplete. Including it
would make relative volume read low and the range read narrow for every
instrument, every time. Forming bars are dropped before any maths runs.

**No look-ahead.** Every value at index *i* uses data from *i* and earlier. True
range uses the *previous* close, never the next one.
"""

from __future__ import annotations

from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from itertools import pairwise
from typing import Final

from aetheris.domain.market import Candle, CandleSeries
from aetheris.domain.scanner import ScannerMetrics, TrendDirection

__all__ = [
    "DEFAULT_MOMENTUM_LOOKBACK",
    "MIN_CANDLES_FOR_METRICS",
    "SIDEWAYS_THRESHOLD_PERCENT",
    "closed_candles",
    "compute_metrics",
]

#: Below this many closed bars the statistics are too thin to mean anything,
#: and the scanner reports INSUFFICIENT_DATA rather than a noisy number.
MIN_CANDLES_FOR_METRICS: Final = 15

#: Momentum is the recent slice of the window, not the whole of it.
DEFAULT_MOMENTUM_LOOKBACK: Final = 10

#: A net move smaller than this reads as SIDEWAYS rather than as a weak trend.
SIDEWAYS_THRESHOLD_PERCENT: Final = Decimal("0.25")

_ZERO: Final = Decimal(0)
_HUNDRED: Final = Decimal(100)
_PCT_EXP: Final = Decimal("0.0001")
_PRICE_EXP: Final = Decimal("0.00000001")
_RATIO_EXP: Final = Decimal("0.0001")


def _q(value: Decimal, exponent: Decimal) -> Decimal:
    return value.quantize(exponent, rounding=ROUND_HALF_UP)


def _percent_change(new: Decimal, old: Decimal) -> Decimal | None:
    """Percent change, or ``None`` when the base is not positive.

    A zero base has no defined percent change. Returning ``None`` keeps that
    distinct from "changed by 0%".
    """
    if old <= _ZERO:
        return None
    return (new - old) / old * _HUNDRED


def _mean(values: list[Decimal]) -> Decimal:
    return sum(values, _ZERO) / Decimal(len(values))


def closed_candles(series: CandleSeries, now: datetime) -> tuple[Candle, ...]:
    """Drop trailing bars that have not closed yet.

    Only trailing bars are examined: the series is validated as strictly
    ordered upstream, so a bar closing in the future can only be at the end.
    """
    candles = series.candles
    end = len(candles)
    while end > 0 and candles[end - 1].close_time > now:
        end -= 1
    return candles[:end]


def _true_ranges(candles: tuple[Candle, ...]) -> list[Decimal]:
    """Wilder's true range for each bar after the first.

    The first bar is skipped rather than approximated by its own high-low:
    mixing two different definitions into one average would make the result
    depend on where the window happened to start.
    """
    ranges: list[Decimal] = []
    for previous, current in pairwise(candles):
        ranges.append(
            max(
                current.high - current.low,
                abs(current.high - previous.close),
                abs(current.low - previous.close),
            )
        )
    return ranges


def _trend_of(net_change_percent: Decimal) -> TrendDirection:
    if net_change_percent > SIDEWAYS_THRESHOLD_PERCENT:
        return TrendDirection.UP
    if net_change_percent < -SIDEWAYS_THRESHOLD_PERCENT:
        return TrendDirection.DOWN
    return TrendDirection.SIDEWAYS


def _trend_consistency(candles: tuple[Candle, ...], net_change_percent: Decimal) -> Decimal:
    """Fraction of bars that closed in the direction of the net move.

    A doji (close == open) counts as not agreeing: it is an absence of
    direction, not evidence for one. With no net direction the consistency is
    zero, because there is nothing to be consistent with.
    """
    direction = 1 if net_change_percent > _ZERO else -1 if net_change_percent < _ZERO else 0
    if direction == 0:
        return _ZERO
    agreeing = sum(
        1
        for candle in candles
        if (candle.close > candle.open and direction == 1)
        or (candle.close < candle.open and direction == -1)
    )
    return Decimal(agreeing) / Decimal(len(candles))


def compute_metrics(
    series: CandleSeries,
    now: datetime,
    *,
    momentum_lookback: int = DEFAULT_MOMENTUM_LOOKBACK,
) -> ScannerMetrics | None:
    """Compute scanner statistics, or ``None`` when the data is too thin.

    ``None`` is the honest answer for a just-listed instrument with three bars
    of history. The caller reports INSUFFICIENT_DATA rather than substituting
    zeros.
    """
    candles = closed_candles(series, now)
    if len(candles) < MIN_CANDLES_FOR_METRICS:
        return None

    closes = [c.close for c in candles]
    volumes = [c.volume for c in candles]
    last_close = closes[-1]

    window_return = _percent_change(last_close, closes[0])
    if window_return is None or last_close <= _ZERO:
        # A window that starts or ends at a non-positive price is not
        # something to publish statistics about.
        return None

    # --- momentum: the recent slice, bounded by what exists ---------------
    lookback = min(momentum_lookback, len(closes) - 1)
    momentum = _percent_change(last_close, closes[-(lookback + 1)]) or _ZERO

    # --- volatility: dispersion of per-bar returns ------------------------
    returns: list[Decimal] = []
    for previous, current in pairwise(closes):
        change = _percent_change(current, previous)
        if change is not None:
            returns.append(change)
    if not returns:
        return None
    mean_return = _mean(returns)
    variance = _mean([(r - mean_return) ** 2 for r in returns])
    volatility = variance.sqrt() if variance > _ZERO else _ZERO

    # --- average true range ----------------------------------------------
    true_ranges = _true_ranges(candles)
    atr = _mean(true_ranges) if true_ranges else _ZERO
    atr_percent = atr / last_close * _HUNDRED

    # --- volume -----------------------------------------------------------
    average_volume = _mean(volumes)
    last_volume = volumes[-1]
    preceding = volumes[:-1]
    preceding_mean = _mean(preceding) if preceding else _ZERO
    relative_volume = last_volume / preceding_mean if preceding_mean > _ZERO else None

    # --- shape of the window ----------------------------------------------
    window_high = max(c.high for c in candles)
    window_low = min(c.low for c in candles)
    range_percent = _percent_change(window_high, window_low) or _ZERO

    mean_body = _mean([abs(c.close - c.open) for c in candles])
    mean_range = _mean([c.high - c.low for c in candles])
    body_percent = mean_body / mean_range * _HUNDRED if mean_range > _ZERO else _ZERO

    return ScannerMetrics(
        timeframe=series.timeframe,
        candles_used=len(candles),
        window_return_percent=_q(window_return, _PCT_EXP),
        momentum_percent=_q(momentum, _PCT_EXP),
        volatility_percent=_q(volatility, _PCT_EXP),
        atr=_q(atr, _PRICE_EXP),
        atr_percent=_q(atr_percent, _PCT_EXP),
        average_volume=_q(average_volume, _PRICE_EXP),
        last_volume=_q(last_volume, _PRICE_EXP),
        relative_volume=(_q(relative_volume, _RATIO_EXP) if relative_volume is not None else None),
        range_percent=_q(range_percent, _PCT_EXP),
        body_percent=_q(body_percent, _PCT_EXP),
        trend=_trend_of(window_return),
        trend_consistency=_q(_trend_consistency(candles, window_return), _RATIO_EXP),
    )
