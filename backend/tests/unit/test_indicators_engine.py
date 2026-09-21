"""Engine behaviour: no look-ahead, warm-up status, ordering, bounds.

The look-ahead test is the most important one in the suite. The same engine
feeds the phase 5 backtester, and an indicator that peeks at a future bar makes
every backtest result silently optimistic -- a failure that produces no error,
no warning and a beautiful equity curve.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from aetheris.analysis.indicators.engine import MAX_SERIES_LIMIT, calculate_indicators
from aetheris.analysis.indicators.library import (
    adx,
    atr,
    bollinger_bands,
    cci,
    ema_indicator,
    macd,
    roc,
    rsi,
    sma_indicator,
    stochastic,
    vwap,
)
from aetheris.analysis.indicators.prepare import PreparationProblem, prepare_candles
from aetheris.analysis.indicators.registry import (
    INDICATOR_KEYS,
    INDICATORS,
    IndicatorParams,
    describe_indicators,
    get_spec,
)
from aetheris.domain.indicators import IndicatorKind, IndicatorStatus
from aetheris.domain.market import Candle

BASE = datetime(2026, 9, 20, tzinfo=UTC)
HOUR = 3600


def bar(index: int, price: int, *, volume: str = "100") -> Candle:
    start = BASE + timedelta(seconds=HOUR * index)
    return Candle(
        open_time=start,
        close_time=start + timedelta(seconds=HOUR, milliseconds=-1),
        open=Decimal(price),
        high=Decimal(price + 3),
        low=Decimal(price - 3),
        close=Decimal(price + 1),
        volume=Decimal(volume),
    )


def varied_series(count: int = 90) -> list[Candle]:
    """A deterministic non-monotonic series.

    Non-monotonic matters: a straight line hides ordering bugs because every
    window looks the same from either end.
    """
    prices = [100 + ((i * 13) % 37) - ((i * 7) % 11) for i in range(count)]
    return [bar(i, price, volume=str(50 + (i * 17) % 90)) for i, price in enumerate(prices)]


def after(candles: list[Candle]) -> datetime:
    return candles[-1].close_time + timedelta(seconds=1)


# ----------------------------------------------------------------------
# No look-ahead
# ----------------------------------------------------------------------

CALCULATIONS = {
    "sma": lambda c: sma_indicator(c, period=10),
    "ema": lambda c: ema_indicator(c, period=10),
    "rsi": lambda c: rsi(c, period=14),
    "macd": lambda c: macd(c, fast=12, slow=26, signal=9),
    "stochastic": lambda c: stochastic(c, period=14, smooth_k=3, period_d=3),
    "roc": lambda c: roc(c, period=12),
    "cci": lambda c: cci(c, period=20),
    "atr": lambda c: atr(c, period=14),
    "bollinger": lambda c: bollinger_bands(c, period=20, deviations=Decimal(2)),
    "vwap": lambda c: vwap(c, period=20),
    "adx": lambda c: adx(c, period=14),
}


@pytest.mark.parametrize("key", sorted(CALCULATIONS))
def test_no_look_ahead(key: str) -> None:
    """The value at bar i must not change when later bars are removed.

    Computing on the full series and on each truncated prefix must agree at
    every shared index. If any indicator read forward -- a centred window, an
    off-by-one shift, a future high -- the prefix result would differ.
    """
    calculate = CALCULATIONS[key]
    candles = varied_series(70)
    full = calculate(candles)

    # Several cut points, including ones deep inside the warm-up region.
    for cut in (30, 45, 60, 69):
        prefix = calculate(candles[: cut + 1])
        for line, values in prefix.items():
            assert values[cut] == full[line][cut], (
                f"{key}.{line} at bar {cut} changed when future bars were removed: "
                f"{values[cut]} with a {cut + 1}-bar series vs {full[line][cut]} with "
                f"{len(candles)} bars"
            )


@pytest.mark.parametrize("key", sorted(CALCULATIONS))
def test_appending_a_bar_never_rewrites_history(key: str) -> None:
    """Every earlier value must survive a new bar arriving unchanged."""
    calculate = CALCULATIONS[key]
    candles = varied_series(60)
    before = calculate(candles[:-1])
    after_append = calculate(candles)
    for line, values in before.items():
        assert values == after_append[line][: len(values)]


# ----------------------------------------------------------------------
# Candle preparation
# ----------------------------------------------------------------------


def test_ascending_candles_pass_through() -> None:
    candles = varied_series(10)
    prepared = prepare_candles(candles, after(candles))
    assert prepared.ok
    assert len(prepared.candles) == 10
    assert not prepared.reversed_order


def test_descending_candles_are_reversed() -> None:
    """Newest-first is a common venue convention and unambiguous to correct."""
    candles = varied_series(10)
    prepared = prepare_candles(list(reversed(candles)), after(candles))
    assert prepared.ok
    assert prepared.reversed_order
    assert [c.open_time for c in prepared.candles] == [c.open_time for c in candles]


def test_duplicate_timestamps_are_refused_not_deduplicated() -> None:
    """Which of two bars with the same open time is authoritative is unknowable."""
    candles = varied_series(5)
    prepared = prepare_candles([*candles, candles[2]], after(candles))
    assert not prepared.ok
    assert prepared.problem is PreparationProblem.DUPLICATE_TIMESTAMPS
    assert prepared.candles == ()


def test_shuffled_candles_are_refused() -> None:
    candles = varied_series(6)
    scrambled = [candles[0], candles[3], candles[1], candles[5], candles[2], candles[4]]
    prepared = prepare_candles(scrambled, after(candles))
    assert not prepared.ok
    assert prepared.problem is PreparationProblem.AMBIGUOUS_ORDER


def test_forming_candles_are_dropped() -> None:
    candles = varied_series(10)
    # A clock inside the final bar: it has not closed.
    mid_last = candles[-1].open_time + timedelta(minutes=30)
    prepared = prepare_candles(candles, mid_last)
    assert prepared.ok
    assert len(prepared.candles) == 9
    assert prepared.dropped_forming == 1


def test_all_forming_is_reported_not_returned_empty_silently() -> None:
    candles = varied_series(5)
    prepared = prepare_candles(candles, BASE - timedelta(days=1))
    assert not prepared.ok
    assert prepared.problem is PreparationProblem.NO_CLOSED_CANDLES
    assert prepared.detail is not None


def test_empty_input_is_reported() -> None:
    prepared = prepare_candles([], BASE)
    assert not prepared.ok
    assert prepared.problem is PreparationProblem.NO_CLOSED_CANDLES


# ----------------------------------------------------------------------
# Engine statuses
# ----------------------------------------------------------------------


def test_ready_when_warmed_up() -> None:
    results = calculate_indicators(varied_series(90), ("rsi", "ema", "macd"))
    for result in results:
        assert result.status is IndicatorStatus.READY
        assert result.latest is not None
        assert all(value is not None for value in result.latest.values())
        assert result.latest_time is not None


def test_insufficient_data_carries_the_requirement() -> None:
    """Five bars cannot produce an RSI(14). Saying so beats returning zeros."""
    results = calculate_indicators(varied_series(5), ("rsi",))
    result = results[0]
    assert result.status is IndicatorStatus.INSUFFICIENT_DATA
    assert result.latest is None
    assert result.detail is not None and "14" in result.detail
    assert result.warmup_bars == 14


def test_a_short_series_is_insufficient_data_whatever_the_indicator() -> None:
    """ADX(14) needs 27 bars; 20 is simply too few for this parameter set."""
    result = calculate_indicators(varied_series(20), ("adx",))[0]
    assert result.status is IndicatorStatus.INSUFFICIENT_DATA
    assert result.latest is None
    assert result.warmup_bars == 27


def test_warming_up_covers_a_latest_bar_with_no_defined_value() -> None:
    """Long series, but the newest window is flat so %K is undefined there.

    This is the case INSUFFICIENT_DATA does not describe: there is plenty of
    data and plenty of history, yet the most recent bar has no value. The
    detail says which line is missing rather than implying a shortage.
    """
    candles = varied_series(60)
    flat_price = 100
    flat_tail = [
        Candle(
            open_time=candles[-1].open_time + timedelta(seconds=HOUR * (n + 1)),
            close_time=candles[-1].open_time + timedelta(seconds=HOUR * (n + 2), milliseconds=-1),
            open=Decimal(flat_price),
            high=Decimal(flat_price),
            low=Decimal(flat_price),
            close=Decimal(flat_price),
            volume=Decimal(10),
        )
        for n in range(6)
    ]
    result = calculate_indicators(
        [*candles, *flat_tail],
        ("stochastic",),
        IndicatorParams(stoch_period=3, stoch_smooth_k=1, stoch_period_d=1),
    )[0]
    assert result.status is IndicatorStatus.WARMING_UP
    assert result.latest is None
    assert result.detail is not None and "undefined at the latest candle" in result.detail


def test_no_value_is_emitted_before_warmup_completes() -> None:
    params = IndicatorParams(rsi_period=14)
    exactly_enough = calculate_indicators(varied_series(15), ("rsi",), params)[0]
    one_short = calculate_indicators(varied_series(14), ("rsi",), params)[0]
    assert exactly_enough.status is IndicatorStatus.READY
    assert one_short.status is IndicatorStatus.INSUFFICIENT_DATA


def test_series_is_omitted_unless_requested() -> None:
    result = calculate_indicators(varied_series(90), ("rsi",))[0]
    assert result.series == ()


def test_series_is_bounded() -> None:
    result = calculate_indicators(varied_series(90), ("rsi",), series_limit=10)[0]
    assert len(result.series) == 10
    assert all(point.time is not None for point in result.series)


def test_series_limit_is_capped_even_if_a_caller_asks_for_more() -> None:
    result = calculate_indicators(varied_series(90), ("rsi",), series_limit=100_000)[0]
    assert len(result.series) <= MAX_SERIES_LIMIT
    assert len(result.series) == 90


def test_unknown_key_is_skipped_not_fabricated() -> None:
    results = calculate_indicators(varied_series(90), ("rsi", "does_not_exist"))
    assert [r.indicator for r in results] == ["rsi"]


def test_results_are_deterministic() -> None:
    candles = varied_series(90)
    assert calculate_indicators(candles, INDICATOR_KEYS) == calculate_indicators(
        candles, INDICATOR_KEYS
    )


@pytest.mark.parametrize("key", INDICATOR_KEYS)
def test_every_registered_indicator_computes(key: str) -> None:
    result = calculate_indicators(varied_series(120), (key,))[0]
    assert result.status is IndicatorStatus.READY, f"{key}: {result.detail}"
    assert result.latest is not None
    assert set(result.latest) == set(result.value_keys)


# ----------------------------------------------------------------------
# Registry
# ----------------------------------------------------------------------


def test_registry_is_a_closed_whitelist() -> None:
    """No path from a request to an arbitrary callable."""
    assert get_spec("rsi") is not None
    assert get_spec("__import__") is None
    assert get_spec("os.system") is None
    assert get_spec("") is None


def test_catalogue_describes_every_registered_indicator() -> None:
    described = describe_indicators()
    assert {d.key for d in described} == set(INDICATOR_KEYS)
    for descriptor in described:
        assert descriptor.convention, f"{descriptor.key} has no documented convention"
        assert descriptor.value_keys


def test_overlays_and_oscillators_are_classified() -> None:
    kinds = {d.key: d.kind for d in describe_indicators()}
    assert kinds["sma"] is IndicatorKind.OVERLAY
    assert kinds["ema"] is IndicatorKind.OVERLAY
    assert kinds["bollinger"] is IndicatorKind.OVERLAY
    assert kinds["vwap"] is IndicatorKind.OVERLAY
    assert kinds["rsi"] is IndicatorKind.OSCILLATOR
    assert kinds["macd"] is IndicatorKind.OSCILLATOR
    assert kinds["adx"] is IndicatorKind.OSCILLATOR


def test_macd_rejects_a_fast_period_at_or_above_the_slow_one() -> None:
    with pytest.raises(ValueError, match="macd_fast must be strictly less"):
        IndicatorParams(macd_fast=26, macd_slow=26)


@pytest.mark.parametrize(
    ("field", "value"),
    [("rsi_period", 0), ("rsi_period", -5), ("sma_period", 1), ("bb_deviations", 0)],
)
def test_parameters_are_bounded(field: str, value: int) -> None:
    with pytest.raises(ValueError):
        IndicatorParams(**{field: value})


def test_warmup_declared_matches_warmup_observed() -> None:
    """The published warm-up must be the real one, for every indicator.

    A declared warm-up that is too short makes a caller trust a value that
    does not exist; too long makes it discard a valid one.
    """
    params = IndicatorParams()
    for key, spec in INDICATORS.items():
        warmup = spec.warmup(params)
        at_warmup = calculate_indicators(varied_series(warmup), (key,), params)[0]
        one_more = calculate_indicators(varied_series(warmup + 1), (key,), params)[0]
        assert at_warmup.status is not IndicatorStatus.READY, (
            f"{key} claims a value with only {warmup} bars"
        )
        assert one_more.status is IndicatorStatus.READY, (
            f"{key} has no value at {warmup + 1} bars despite declaring warm-up {warmup}"
        )
