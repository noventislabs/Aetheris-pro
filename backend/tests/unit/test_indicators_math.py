"""Indicator arithmetic checked against hand-calculated values.

Every expected number below is derived from the published formula by hand, in
the comment above the assertion, using inputs chosen so the arithmetic closes
exactly or to a few decimal places. None of them were produced by running the
implementation and pasting the output -- a test written that way only asserts
that the code still does whatever it did first.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from aetheris.analysis.indicators import library
from aetheris.analysis.indicators.core import ema, population_stdev, sma, wilder_average
from aetheris.domain.market import Candle

BASE = datetime(2026, 9, 20, tzinfo=UTC)
HOUR = 3600


def bar(index: int, *, o: str, h: str, low: str, c: str, v: str = "100") -> Candle:
    start = BASE + timedelta(seconds=HOUR * index)
    return Candle(
        open_time=start,
        close_time=start + timedelta(seconds=HOUR, milliseconds=-1),
        open=Decimal(o),
        high=Decimal(h),
        low=Decimal(low),
        close=Decimal(c),
        volume=Decimal(v),
    )


def flat_bars(prices: list[str], *, volume: str = "100") -> list[Candle]:
    """Bars where open == high == low == close, so typical price == close."""
    return [bar(i, o=p, h=p, low=p, c=p, v=volume) for i, p in enumerate(prices)]


def close(value: Decimal | None, expected: str, places: int = 6) -> bool:
    assert value is not None, "expected a value, got None"
    return abs(value - Decimal(expected)) < Decimal(10) ** -places


# ----------------------------------------------------------------------
# Moving averages
# ----------------------------------------------------------------------


def test_sma_is_the_arithmetic_mean_of_the_window() -> None:
    values = [Decimal(n) for n in (1, 2, 3, 4, 5)]
    result = sma(values, 3)
    # Warm-up is period-1, so indices 0 and 1 have no value.
    assert result[0] is None
    assert result[1] is None
    # (1+2+3)/3 = 2 ; (2+3+4)/3 = 3 ; (3+4+5)/3 = 4
    assert result[2] == Decimal(2)
    assert result[3] == Decimal(3)
    assert result[4] == Decimal(4)


def test_ema_uses_the_sma_seed_convention() -> None:
    values = [Decimal(n) for n in (1, 2, 3, 4, 5)]
    result = ema(values, 3)
    # k = 2/(3+1) = 0.5. Seed at index 2 = SMA(1,2,3) = 2.
    assert result[2] == Decimal(2)
    # index 3 = 4*0.5 + 2*0.5 = 3
    assert result[3] == Decimal(3)
    # index 4 = 5*0.5 + 3*0.5 = 4
    assert result[4] == Decimal(4)


def test_ema_warmup_matches_the_documented_period() -> None:
    values = [Decimal(n) for n in range(1, 11)]
    result = ema(values, 5)
    assert all(value is None for value in result[:4])
    assert result[4] is not None


def test_wilder_average_differs_from_an_ema_of_the_same_period() -> None:
    """Wilder's smoothing is an EMA of period 2n-1, not of period n.

    Using one where the other is specified is the single most common way an
    RSI implementation ends up disagreeing with every reference chart.
    """
    values: list[Decimal | None] = [Decimal(n) for n in (1, 2, 3, 4, 5, 6, 7, 8)]
    wilder = wilder_average(values, 3, start=0)
    exponential = ema(values, 3)
    # Seed is the same (SMA of the first 3 = 2) but they diverge immediately.
    assert wilder[2] == exponential[2] == Decimal(2)
    assert wilder[3] != exponential[3]
    # Wilder index 3 = (2*2 + 4)/3 = 8/3
    assert close(wilder[3], "2.6666666666")


# ----------------------------------------------------------------------
# RSI — hand-computed from Wilder's definition
# ----------------------------------------------------------------------


def test_rsi_matches_hand_calculation() -> None:
    """closes 100,101,102,101,103,104 with period 3.

    changes: +1, +1, -1, +2, +1
    gains:    1,  1,  0,  2,  1
    losses:   0,  0,  1,  0,  0

    index 3 (first): avgGain = (1+1+0)/3 = 2/3 ; avgLoss = (0+0+1)/3 = 1/3
                     RS = 2      -> RSI = 100 - 100/3       = 66.666667
    index 4: avgGain = (2/3*2 + 2)/3 = 10/9 ; avgLoss = (1/3*2 + 0)/3 = 2/9
             RS = 5             -> RSI = 100 - 100/6        = 83.333333
    index 5: avgGain = (10/9*2 + 1)/3 = 29/27 ; avgLoss = (2/9*2 + 0)/3 = 4/27
             RS = 7.25          -> RSI = 100 - 100/8.25     = 87.878788
    """
    candles = flat_bars(["100", "101", "102", "101", "103", "104"])
    result = library.rsi(candles, period=3)["rsi"]

    assert all(value is None for value in result[:3])
    assert close(result[3], "66.6666666666", places=8)
    assert close(result[4], "83.3333333333", places=8)
    assert close(result[5], "87.8787878788", places=8)


def test_rsi_is_one_hundred_for_an_unbroken_advance() -> None:
    candles = flat_bars([str(100 + n) for n in range(10)])
    result = library.rsi(candles, period=3)["rsi"]
    assert result[-1] == Decimal(100)


def test_rsi_is_zero_for_an_unbroken_decline() -> None:
    candles = flat_bars([str(100 - n) for n in range(10)])
    result = library.rsi(candles, period=3)["rsi"]
    assert result[-1] == Decimal(0)


def test_rsi_of_a_perfectly_flat_market_is_fifty() -> None:
    """Both averages are zero, so RS is 0/0. Fifty is the documented choice."""
    candles = flat_bars(["100"] * 10)
    result = library.rsi(candles, period=3)["rsi"]
    assert result[-1] == Decimal(50)


def test_rsi_stays_within_bounds() -> None:
    prices = ["100", "104", "99", "107", "101", "110", "95", "112", "97", "115"]
    result = library.rsi(flat_bars(prices), period=4)["rsi"]
    for value in result:
        if value is not None:
            assert Decimal(0) <= value <= Decimal(100)


# ----------------------------------------------------------------------
# MACD — hand-computed
# ----------------------------------------------------------------------


def test_macd_matches_hand_calculation() -> None:
    """closes 1..6 with fast=2, slow=3, signal=2.

    EMA(2), k=2/3: seed idx1 = 1.5 ; idx2 = 3*2/3 + 1.5/3 = 2.5
                   idx3 = 3.5 ; idx4 = 4.5 ; idx5 = 5.5
    EMA(3), k=0.5: seed idx2 = 2 ; idx3 = 3 ; idx4 = 4 ; idx5 = 5
    macd = fast - slow = 0.5 from idx2 onward
    signal = EMA(2) of macd, seeded at idx3 with SMA(0.5,0.5) = 0.5
    histogram = 0.5 - 0.5 = 0
    """
    candles = flat_bars(["1", "2", "3", "4", "5", "6"])
    result = library.macd(candles, fast=2, slow=3, signal=2)

    assert result["macd"][2] == Decimal("0.5")
    assert result["macd"][5] == Decimal("0.5")
    assert close(result["signal"][3], "0.5")
    assert close(result["histogram"][5], "0")


def test_macd_histogram_is_positive_when_the_fast_leg_pulls_away() -> None:
    # An accelerating advance: the fast EMA rises away from the slow one, so
    # the macd line rises above its own signal.
    prices = [str(100 + n * n) for n in range(40)]
    result = library.macd(flat_bars(prices), fast=12, slow=26, signal=9)
    assert result["histogram"][-1] is not None
    assert result["histogram"][-1] > 0


def test_macd_warmup_is_slow_plus_signal_minus_two() -> None:
    prices = [str(100 + n) for n in range(60)]
    result = library.macd(flat_bars(prices), fast=12, slow=26, signal=9)
    # macd line ready at slow-1 = 25; signal needs 9 macd values -> 25+8 = 33
    assert result["macd"][24] is None
    assert result["macd"][25] is not None
    assert result["histogram"][32] is None
    assert result["histogram"][33] is not None


# ----------------------------------------------------------------------
# Bollinger — hand-computed
# ----------------------------------------------------------------------


def test_population_stdev_uses_divisor_n() -> None:
    # values 1..5, mean 3, squared deviations 4,1,0,1,4 -> sum 10
    # population variance = 10/5 = 2  (sample would be 10/4 = 2.5)
    values = [Decimal(n) for n in (1, 2, 3, 4, 5)]
    assert close(population_stdev(values), "1.4142135623", places=8)


def test_bollinger_bands_match_hand_calculation() -> None:
    """closes 1..5, period 5, 2 deviations.

    middle = 3 ; population stdev = sqrt(2) = 1.41421356
    upper = 3 + 2*sqrt(2) = 5.82842712
    lower = 3 - 2*sqrt(2) = 0.17157288
    """
    candles = flat_bars(["1", "2", "3", "4", "5"])
    result = library.bollinger_bands(candles, period=5, deviations=Decimal(2))
    assert result["middle"][4] == Decimal(3)
    assert close(result["upper"][4], "5.8284271247", places=8)
    assert close(result["lower"][4], "0.1715728752", places=8)


def test_bollinger_bands_collapse_onto_the_mean_in_a_flat_market() -> None:
    result = library.bollinger_bands(flat_bars(["50"] * 10), period=5, deviations=Decimal(2))
    assert result["upper"][-1] == result["middle"][-1] == result["lower"][-1] == Decimal(50)


# ----------------------------------------------------------------------
# ATR — hand-computed
# ----------------------------------------------------------------------


def test_atr_matches_hand_calculation() -> None:
    """Four bars with a constant true range of 10, then Wilder smoothing.

    Each bar: high-low = 10, and the previous close sits inside the range, so
    TR = 10 every bar. The mean of any number of tens is ten, and Wilder
    smoothing of a constant is that constant.
    """
    candles = [
        bar(0, o="100", h="105", low="95", c="100"),
        bar(1, o="100", h="105", low="95", c="100"),
        bar(2, o="100", h="105", low="95", c="100"),
        bar(3, o="100", h="105", low="95", c="100"),
        bar(4, o="100", h="105", low="95", c="100"),
    ]
    result = library.atr(candles, period=3)["atr"]
    assert result[2] is None  # bar 0 has no TR, so the seed ends at bar 3
    assert result[3] == Decimal(10)
    assert result[4] == Decimal(10)


def test_atr_accounts_for_gaps_through_the_previous_close() -> None:
    """A gap up makes |high - previous close| the widest of the three terms."""
    candles = [
        bar(0, o="100", h="100", low="100", c="100"),
        bar(1, o="120", h="122", low="120", c="121"),
    ]
    ranges = library.atr(candles, period=1)["atr"]
    # TR = max(122-120, |122-100|, |120-100|) = 22
    assert ranges[1] == Decimal(22)


def test_atr_is_zero_for_a_perfectly_flat_market() -> None:
    result = library.atr(flat_bars(["100"] * 10), period=3)["atr"]
    assert result[-1] == Decimal(0)


# ----------------------------------------------------------------------
# ROC, CCI, VWAP, Stochastic — hand-computed
# ----------------------------------------------------------------------


def test_roc_matches_hand_calculation() -> None:
    # (121 - 100) / 100 * 100 = 21
    candles = flat_bars(["100", "110", "121"])
    result = library.roc(candles, period=2)["roc"]
    assert result[0] is None
    assert result[1] is None
    assert result[2] == Decimal(21)


def test_roc_is_negative_on_a_decline() -> None:
    # (75 - 100) / 100 * 100 = -25
    result = library.roc(flat_bars(["100", "90", "75"]), period=2)["roc"]
    assert result[2] == Decimal(-25)


def test_cci_matches_hand_calculation() -> None:
    """Typical prices 1,2,3 with period 3.

    SMA(TP) = 2 ; mean deviation = (|1-2| + |2-2| + |3-2|)/3 = 2/3
    CCI = (3 - 2) / (0.015 * 2/3) = 1 / 0.01 = 100
    """
    candles = flat_bars(["1", "2", "3"])
    result = library.cci(candles, period=3)["cci"]
    assert close(result[2], "100", places=6)


def test_cci_is_undefined_for_a_flat_window() -> None:
    """Mean deviation is zero, so the quotient has no value -- not 0."""
    result = library.cci(flat_bars(["50"] * 5), period=3)["cci"]
    assert result[-1] is None


def test_vwap_equals_the_mean_typical_price_when_volume_is_constant() -> None:
    # Equal weights reduce a weighted mean to a plain mean: (1+2+3)/3 = 2
    candles = flat_bars(["1", "2", "3"], volume="7")
    result = library.vwap(candles, period=3)["vwap"]
    assert close(result[2], "2")


def test_vwap_is_pulled_toward_the_heavier_bar() -> None:
    """TP 10 with volume 1, TP 20 with volume 3.

    VWAP = (10*1 + 20*3) / (1+3) = 70/4 = 17.5
    """
    candles = [
        bar(0, o="10", h="10", low="10", c="10", v="1"),
        bar(1, o="20", h="20", low="20", c="20", v="3"),
    ]
    result = library.vwap(candles, period=2)["vwap"]
    assert result[1] == Decimal("17.5")


def test_vwap_is_undefined_when_nothing_traded() -> None:
    result = library.vwap(flat_bars(["10", "20"], volume="0"), period=2)["vwap"]
    assert result[1] is None


def test_stochastic_matches_hand_calculation() -> None:
    """Window high 10, low 0, close 5 -> %K = 100 * (5-0)/(10-0) = 50."""
    candles = [
        bar(0, o="5", h="10", low="0", c="5"),
        bar(1, o="5", h="10", low="0", c="5"),
        bar(2, o="5", h="10", low="0", c="5"),
    ]
    result = library.stochastic(candles, period=3, smooth_k=1, period_d=1)
    assert result["k"][2] == Decimal(50)


def test_stochastic_is_one_hundred_at_the_top_of_the_range() -> None:
    candles = [
        bar(0, o="5", h="10", low="0", c="5"),
        bar(1, o="5", h="10", low="0", c="5"),
        bar(2, o="5", h="10", low="0", c="10"),
    ]
    result = library.stochastic(candles, period=3, smooth_k=1, period_d=1)
    assert result["k"][2] == Decimal(100)


def test_stochastic_is_undefined_when_the_window_has_no_range() -> None:
    """High equals low: the ratio is 0/0. Not 50, not 0 -- absent."""
    result = library.stochastic(flat_bars(["7"] * 6), period=3, smooth_k=1, period_d=1)
    assert result["k"][-1] is None


# ----------------------------------------------------------------------
# ADX
# ----------------------------------------------------------------------


def test_adx_directional_indicators_on_a_clean_uptrend() -> None:
    """Every bar makes a higher high and a higher low.

    +DM is positive on every bar and -DM is zero throughout, so -DI must be
    exactly zero and +DI must dominate.
    """
    candles = [
        bar(i, o=str(100 + i), h=str(102 + i), low=str(99 + i), c=str(101 + i)) for i in range(40)
    ]
    result = library.adx(candles, period=5)
    assert result["minus_di"][-1] == Decimal(0)
    assert result["plus_di"][-1] is not None
    assert result["plus_di"][-1] > Decimal(0)
    # With one-sided directional movement, DX is 100 every bar, so its
    # Wilder average is also 100.
    assert close(result["adx"][-1], "100", places=6)


def test_adx_directional_indicators_on_a_clean_downtrend() -> None:
    candles = [
        bar(i, o=str(200 - i), h=str(202 - i), low=str(199 - i), c=str(201 - i)) for i in range(40)
    ]
    result = library.adx(candles, period=5)
    assert result["plus_di"][-1] == Decimal(0)
    assert result["minus_di"][-1] is not None
    assert result["minus_di"][-1] > Decimal(0)


def test_adx_warmup_is_twice_the_period_minus_one() -> None:
    candles = [
        bar(i, o=str(100 + i), h=str(102 + i), low=str(99 + i), c=str(101 + i)) for i in range(40)
    ]
    result = library.adx(candles, period=5)
    # DX first appears at bar `period`; ADX averages `period` of those, so the
    # first ADX lands at 2*period - 1 = 9.
    assert result["adx"][8] is None
    assert result["adx"][9] is not None


def test_adx_stays_within_bounds() -> None:
    prices = [(100 + (i % 7) * 3) for i in range(60)]
    candles = [
        bar(i, o=str(p), h=str(p + 2), low=str(p - 2), c=str(p)) for i, p in enumerate(prices)
    ]
    result = library.adx(candles, period=14)
    for key in ("adx", "plus_di", "minus_di"):
        for value in result[key]:
            if value is not None:
                assert Decimal(0) <= value <= Decimal(100)


# ----------------------------------------------------------------------
# Precision and scale
# ----------------------------------------------------------------------


def test_very_small_prices_keep_their_precision() -> None:
    """A micro-cap perpetual trades at 1e-8. Float would round this to noise."""
    prices = ["0.00000001", "0.00000002", "0.00000003", "0.00000004", "0.00000005"]
    result = sma([Decimal(p) for p in prices], 5)
    # (1+2+3+4+5)e-8 / 5 = 3e-8, exactly.
    assert result[4] == Decimal("0.00000003")


def test_large_volumes_do_not_lose_precision_in_vwap() -> None:
    candles = [
        bar(0, o="1", h="1", low="1", c="1", v="99999999999.12345678"),
        bar(1, o="3", h="3", low="3", c="3", v="99999999999.12345678"),
    ]
    result = library.vwap(candles, period=2)["vwap"]
    # Equal weights -> exact mean of 1 and 3.
    assert result[1] == Decimal(2)


def test_repeated_calculation_is_bit_identical() -> None:
    prices = [str(100 + (i * 7) % 23) for i in range(60)]
    candles = flat_bars(prices)
    first = library.rsi(candles, period=14)["rsi"]
    second = library.rsi(candles, period=14)["rsi"]
    assert first == second


@pytest.mark.parametrize("period", [2, 3, 5, 14, 20])
def test_warmup_never_emits_a_value_early(period: int) -> None:
    """An indicator that answers before it has the data is the worst failure.

    A fabricated early value looks exactly like a real one on a chart.
    """
    candles = flat_bars([str(100 + n) for n in range(60)])
    assert all(value is None for value in library.rsi(candles, period=period)["rsi"][:period])
    assert all(value is None for value in sma([c.close for c in candles], period)[: period - 1])
    assert all(value is None for value in ema([c.close for c in candles], period)[: period - 1])
