"""Scanner metric arithmetic: deterministic, closed-bars-only, no look-ahead."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from aetheris.analysis.metrics import (
    MIN_CANDLES_FOR_METRICS,
    closed_candles,
    compute_metrics,
)
from aetheris.domain.enums import Timeframe
from aetheris.domain.market import Candle, CandleSeries
from aetheris.domain.scanner import TrendDirection

BASE = datetime(2026, 9, 20, 0, 0, tzinfo=UTC)
HOUR = 3600


def candle(
    index: int,
    *,
    open_: str,
    high: str,
    low: str,
    close: str,
    volume: str = "100",
    interval: int = HOUR,
) -> Candle:
    start = BASE + timedelta(seconds=interval * index)
    return Candle(
        open_time=start,
        close_time=start + timedelta(seconds=interval, milliseconds=-1),
        open=Decimal(open_),
        high=Decimal(high),
        low=Decimal(low),
        close=Decimal(close),
        volume=Decimal(volume),
    )


def flat_series(count: int, *, price: str = "100", volume: str = "100") -> CandleSeries:
    """A series with no movement: every bar identical."""
    return CandleSeries(
        symbol="TESTUSDT",
        timeframe=Timeframe.H1,
        candles=tuple(
            candle(i, open_=price, high=price, low=price, close=price, volume=volume)
            for i in range(count)
        ),
    )


def rising_series(count: int, *, step: str = "1", volume: str = "100") -> CandleSeries:
    """Each bar opens where the last closed and closes `step` higher."""
    candles = []
    price = Decimal("100")
    for i in range(count):
        close = price + Decimal(step)
        candles.append(
            candle(
                i,
                open_=str(price),
                high=str(close),
                low=str(price),
                close=str(close),
                volume=volume,
            )
        )
        price = close
    return CandleSeries(symbol="TESTUSDT", timeframe=Timeframe.H1, candles=tuple(candles))


def after(series: CandleSeries) -> datetime:
    """A clock at which every bar in the series has closed."""
    last = series.candles[-1].close_time
    return last + timedelta(seconds=1)


# ----------------------------------------------------------------------
# Closed-bar filtering
# ----------------------------------------------------------------------


def test_forming_candle_is_excluded() -> None:
    """A partial bar has partial volume and an incomplete range.

    Including it would make relative volume read low for every instrument on
    every scan.
    """
    series = rising_series(5)
    # A clock inside the final bar: it has not closed yet.
    mid_last_bar = series.candles[-1].open_time + timedelta(minutes=30)
    assert len(closed_candles(series, mid_last_bar)) == 4
    assert len(closed_candles(series, after(series))) == 5


def test_all_bars_forming_yields_nothing() -> None:
    series = rising_series(3)
    assert closed_candles(series, BASE - timedelta(days=1)) == ()


def test_empty_series_is_handled() -> None:
    empty = CandleSeries(symbol="TESTUSDT", timeframe=Timeframe.H1, candles=())
    assert closed_candles(empty, BASE) == ()
    assert compute_metrics(empty, BASE) is None


# ----------------------------------------------------------------------
# Insufficient data
# ----------------------------------------------------------------------


def test_too_few_candles_returns_none_not_zeros() -> None:
    """A just-listed instrument gets no statistics, not fabricated ones."""
    series = rising_series(MIN_CANDLES_FOR_METRICS - 1)
    assert compute_metrics(series, after(series)) is None


def test_exactly_the_minimum_is_enough() -> None:
    series = rising_series(MIN_CANDLES_FOR_METRICS)
    assert compute_metrics(series, after(series)) is not None


def test_forming_bar_can_push_a_series_below_the_minimum() -> None:
    series = rising_series(MIN_CANDLES_FOR_METRICS)
    mid_last_bar = series.candles[-1].open_time + timedelta(minutes=30)
    assert compute_metrics(series, mid_last_bar) is None


# ----------------------------------------------------------------------
# Values
# ----------------------------------------------------------------------


def test_flat_market_has_zero_volatility_and_sideways_trend() -> None:
    series = flat_series(20)
    metrics = compute_metrics(series, after(series))
    assert metrics is not None
    assert metrics.volatility_percent == Decimal("0.0000")
    assert metrics.window_return_percent == Decimal("0.0000")
    assert metrics.trend is TrendDirection.SIDEWAYS
    # No net direction means nothing to be consistent with.
    assert metrics.trend_consistency == Decimal("0.0000")


def test_rising_market_is_an_up_trend_with_full_consistency() -> None:
    series = rising_series(20)
    metrics = compute_metrics(series, after(series))
    assert metrics is not None
    assert metrics.trend is TrendDirection.UP
    assert metrics.trend_consistency == Decimal("1.0000")
    assert metrics.window_return_percent > 0
    assert metrics.momentum_percent > 0


def test_falling_market_is_a_down_trend() -> None:
    candles = []
    price = Decimal("200")
    for i in range(20):
        close = price - Decimal("2")
        candles.append(
            candle(i, open_=str(price), high=str(price), low=str(close), close=str(close))
        )
        price = close
    series = CandleSeries(symbol="TESTUSDT", timeframe=Timeframe.H1, candles=tuple(candles))
    metrics = compute_metrics(series, after(series))
    assert metrics is not None
    assert metrics.trend is TrendDirection.DOWN
    assert metrics.window_return_percent < 0
    assert metrics.trend_consistency == Decimal("1.0000")


def test_window_return_is_close_to_close() -> None:
    series = rising_series(16, step="1")
    metrics = compute_metrics(series, after(series))
    assert metrics is not None
    first_close = series.candles[0].close
    last_close = series.candles[-1].close
    expected = (last_close - first_close) / first_close * 100
    assert metrics.window_return_percent == expected.quantize(Decimal("0.0001"))


def test_atr_is_positive_when_bars_have_range() -> None:
    series = rising_series(20)
    metrics = compute_metrics(series, after(series))
    assert metrics is not None
    assert metrics.atr > 0
    assert metrics.atr_percent > 0


def test_atr_is_zero_for_a_perfectly_flat_market() -> None:
    series = flat_series(20)
    metrics = compute_metrics(series, after(series))
    assert metrics is not None
    assert metrics.atr == Decimal("0E-8")


def test_relative_volume_compares_last_bar_to_the_preceding_mean() -> None:
    candles = [
        candle(i, open_="100", high="101", low="99", close="100", volume="100") for i in range(19)
    ]
    candles.append(candle(19, open_="100", high="101", low="99", close="100", volume="300"))
    series = CandleSeries(symbol="TESTUSDT", timeframe=Timeframe.H1, candles=tuple(candles))
    metrics = compute_metrics(series, after(series))
    assert metrics is not None
    assert metrics.relative_volume == Decimal("3.0000")
    assert metrics.last_volume == Decimal("300.00000000")


def test_zero_volume_history_leaves_relative_volume_unavailable() -> None:
    """Unknown, not zero: dividing by a zero mean has no answer."""
    candles = [
        candle(i, open_="100", high="101", low="99", close="100", volume="0") for i in range(19)
    ]
    candles.append(candle(19, open_="100", high="101", low="99", close="100", volume="50"))
    series = CandleSeries(symbol="TESTUSDT", timeframe=Timeframe.H1, candles=tuple(candles))
    metrics = compute_metrics(series, after(series))
    assert metrics is not None
    assert metrics.relative_volume is None


def test_body_percent_is_zero_for_doji_bars() -> None:
    candles = [candle(i, open_="100", high="105", low="95", close="100") for i in range(20)]
    series = CandleSeries(symbol="TESTUSDT", timeframe=Timeframe.H1, candles=tuple(candles))
    metrics = compute_metrics(series, after(series))
    assert metrics is not None
    assert metrics.body_percent == Decimal("0.0000")


def test_range_percent_spans_window_high_to_low() -> None:
    series = rising_series(20, step="1")
    metrics = compute_metrics(series, after(series))
    assert metrics is not None
    assert metrics.range_percent > 0


def test_metrics_are_deterministic() -> None:
    """Same input, same clock, same numbers — every time."""
    series = rising_series(25)
    clock = after(series)
    first = compute_metrics(series, clock)
    second = compute_metrics(series, clock)
    assert first == second


def test_candles_used_reflects_closed_bars_only() -> None:
    series = rising_series(20)
    mid_last_bar = series.candles[-1].open_time + timedelta(minutes=30)
    metrics = compute_metrics(series, mid_last_bar)
    assert metrics is not None
    assert metrics.candles_used == 19


@pytest.mark.parametrize("lookback", [1, 5, 10])
def test_momentum_lookback_is_bounded_by_available_history(lookback: int) -> None:
    series = rising_series(20)
    metrics = compute_metrics(series, after(series), momentum_lookback=lookback)
    assert metrics is not None
    assert metrics.momentum_percent > 0


def test_momentum_lookback_longer_than_history_still_works() -> None:
    series = rising_series(16)
    metrics = compute_metrics(series, after(series), momentum_lookback=500)
    assert metrics is not None
    # Falls back to the whole window rather than erroring.
    assert metrics.momentum_percent == metrics.window_return_percent


def test_scanner_atr_uses_the_shared_wilder_convention() -> None:
    """One ATR convention across the product, not two wearing the same name.

    The scanner metric must agree with the indicator engine bar for bar; if
    they ever diverge again a user sees two different ATRs for one instrument.
    """
    from aetheris.analysis.indicators.library import atr as indicator_atr
    from aetheris.analysis.metrics import ATR_PERIOD

    series = CandleSeries(
        symbol="TESTUSDT",
        timeframe=Timeframe.H1,
        candles=tuple(
            candle(
                i,
                open_=str(100 + i),
                high=str(104 + i),
                low=str(97 + i),
                close=str(101 + i),
            )
            for i in range(40)
        ),
    )
    metrics = compute_metrics(series, after(series))
    assert metrics is not None

    expected = indicator_atr(list(series.candles), period=ATR_PERIOD)["atr"][-1]
    assert expected is not None
    assert metrics.atr == expected.quantize(Decimal("0.00000001"))
