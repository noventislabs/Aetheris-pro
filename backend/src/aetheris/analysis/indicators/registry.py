"""Indicator catalogue and dispatch.

The registry is a closed whitelist. Callers name an indicator by key; there is
no path from a request to an arbitrary callable, an expression, a formula
string or an eval. Adding an indicator means adding an entry here, in a commit
that also adds its tests.

Parameters are typed integers and decimals with declared bounds, validated
before any maths runs. There is no generic "params" dictionary a caller could
stuff with arbitrary keys.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Final

from pydantic import BaseModel, ConfigDict, Field, model_validator

from aetheris.analysis.indicators import library
from aetheris.analysis.indicators.core import Series
from aetheris.domain.indicators import IndicatorDescriptor, IndicatorKind
from aetheris.domain.market import Candle

__all__ = [
    "INDICATORS",
    "INDICATOR_KEYS",
    "MAX_INDICATORS_PER_REQUEST",
    "IndicatorParams",
    "describe_indicators",
    "get_spec",
]

#: Bounded so one request cannot ask for unlimited computation.
MAX_INDICATORS_PER_REQUEST: Final = 8


class IndicatorParams(BaseModel):
    """Every tunable parameter, explicitly named and bounded.

    Deliberately a flat set of named fields rather than a free-form mapping: a
    caller can only set parameters that exist, within ranges that make sense,
    and nothing else reaches the maths.
    """

    model_config = ConfigDict(frozen=True)

    sma_period: int = Field(default=20, ge=2, le=400)
    ema_period: int = Field(default=21, ge=2, le=400)
    rsi_period: int = Field(default=14, ge=2, le=200)
    atr_period: int = Field(default=14, ge=2, le=200)
    adx_period: int = Field(default=14, ge=2, le=200)
    roc_period: int = Field(default=12, ge=1, le=200)
    cci_period: int = Field(default=20, ge=2, le=200)
    vwap_period: int = Field(default=20, ge=2, le=400)
    bb_period: int = Field(default=20, ge=2, le=400)
    bb_deviations: Decimal = Field(default=Decimal("2"), gt=0, le=10)
    macd_fast: int = Field(default=12, ge=2, le=200)
    macd_slow: int = Field(default=26, ge=3, le=400)
    macd_signal: int = Field(default=9, ge=2, le=200)
    stoch_period: int = Field(default=14, ge=2, le=200)
    stoch_smooth_k: int = Field(default=3, ge=1, le=50)
    stoch_period_d: int = Field(default=3, ge=1, le=50)

    @model_validator(mode="after")
    def _macd_fast_is_shorter_than_slow(self) -> IndicatorParams:
        """A MACD whose "fast" leg is slower than its "slow" leg is meaningless.

        Rejected rather than silently swapped: swapping would answer a
        different question from the one asked.
        """
        if self.macd_fast >= self.macd_slow:
            raise ValueError("macd_fast must be strictly less than macd_slow")
        return self


ComputeFn = Callable[[Sequence[Candle], IndicatorParams], dict[str, Series]]
WarmupFn = Callable[[IndicatorParams], int]
ParamsFn = Callable[[IndicatorParams], dict[str, Decimal]]


@dataclass(frozen=True, slots=True)
class IndicatorSpec:
    key: str
    name: str
    kind: IndicatorKind
    value_keys: tuple[str, ...]
    description: str
    convention: str
    parameter_names: tuple[str, ...]
    compute: ComputeFn
    #: Bars consumed before the indicator produces its first value.
    warmup: WarmupFn
    used_parameters: ParamsFn


def _params(params: IndicatorParams, *names: str) -> dict[str, Decimal]:
    return {name: Decimal(getattr(params, name)) for name in names}


INDICATORS: Final[dict[str, IndicatorSpec]] = {
    "sma": IndicatorSpec(
        key="sma",
        name="Simple Moving Average",
        kind=IndicatorKind.OVERLAY,
        value_keys=("sma",),
        description="Arithmetic mean of close over the period.",
        convention="Unweighted mean; first value on bar `period`.",
        parameter_names=("sma_period",),
        compute=lambda candles, p: library.sma_indicator(candles, period=p.sma_period),
        warmup=lambda p: p.sma_period - 1,
        used_parameters=lambda p: _params(p, "sma_period"),
    ),
    "ema": IndicatorSpec(
        key="ema",
        name="Exponential Moving Average",
        kind=IndicatorKind.OVERLAY,
        value_keys=("ema",),
        description="Exponentially weighted mean of close.",
        convention=(
            "k = 2/(period+1), seeded with the SMA of the first `period` closes "
            "(TA-Lib convention)."
        ),
        parameter_names=("ema_period",),
        compute=lambda candles, p: library.ema_indicator(candles, period=p.ema_period),
        warmup=lambda p: p.ema_period - 1,
        used_parameters=lambda p: _params(p, "ema_period"),
    ),
    "bollinger": IndicatorSpec(
        key="bollinger",
        name="Bollinger Bands",
        kind=IndicatorKind.OVERLAY,
        value_keys=("upper", "middle", "lower"),
        description="SMA envelope at a multiple of the population standard deviation.",
        convention="Middle = SMA(close); bands at +/- deviations x POPULATION stdev (divisor N).",
        parameter_names=("bb_period", "bb_deviations"),
        compute=lambda candles, p: library.bollinger_bands(
            candles, period=p.bb_period, deviations=p.bb_deviations
        ),
        warmup=lambda p: p.bb_period - 1,
        used_parameters=lambda p: {
            "bb_period": Decimal(p.bb_period),
            "bb_deviations": p.bb_deviations,
        },
    ),
    "vwap": IndicatorSpec(
        key="vwap",
        name="Rolling VWAP",
        kind=IndicatorKind.OVERLAY,
        value_keys=("vwap",),
        description="Volume-weighted average typical price over a rolling window.",
        convention=(
            "Rolling window, NOT session-anchored: perpetuals trade continuously and "
            "have no session boundary to reset at."
        ),
        parameter_names=("vwap_period",),
        compute=lambda candles, p: library.vwap(candles, period=p.vwap_period),
        warmup=lambda p: p.vwap_period - 1,
        used_parameters=lambda p: _params(p, "vwap_period"),
    ),
    "rsi": IndicatorSpec(
        key="rsi",
        name="Relative Strength Index",
        kind=IndicatorKind.OSCILLATOR,
        value_keys=("rsi",),
        description="Momentum oscillator bounded 0-100.",
        convention="Wilder's smoothing (RMA), not an EMA. First value on bar `period`.",
        parameter_names=("rsi_period",),
        compute=lambda candles, p: library.rsi(candles, period=p.rsi_period),
        warmup=lambda p: p.rsi_period,
        used_parameters=lambda p: _params(p, "rsi_period"),
    ),
    "macd": IndicatorSpec(
        key="macd",
        name="MACD",
        kind=IndicatorKind.OSCILLATOR,
        value_keys=("macd", "signal", "histogram"),
        description="Difference of two EMAs, with its own signal EMA.",
        convention="EMA(fast) - EMA(slow); signal = EMA(signal) of that line; both SMA-seeded.",
        parameter_names=("macd_fast", "macd_slow", "macd_signal"),
        compute=lambda candles, p: library.macd(
            candles, fast=p.macd_fast, slow=p.macd_slow, signal=p.macd_signal
        ),
        warmup=lambda p: p.macd_slow + p.macd_signal - 2,
        used_parameters=lambda p: _params(p, "macd_fast", "macd_slow", "macd_signal"),
    ),
    "stochastic": IndicatorSpec(
        key="stochastic",
        name="Stochastic Oscillator",
        kind=IndicatorKind.OSCILLATOR,
        value_keys=("k", "d"),
        description="Position of close within the recent high-low range.",
        convention="Slow stochastic: %K = SMA(smooth_k) of raw %K; %D = SMA(period_d) of %K.",
        parameter_names=("stoch_period", "stoch_smooth_k", "stoch_period_d"),
        compute=lambda candles, p: library.stochastic(
            candles,
            period=p.stoch_period,
            smooth_k=p.stoch_smooth_k,
            period_d=p.stoch_period_d,
        ),
        warmup=lambda p: p.stoch_period + p.stoch_smooth_k + p.stoch_period_d - 3,
        used_parameters=lambda p: _params(p, "stoch_period", "stoch_smooth_k", "stoch_period_d"),
    ),
    "atr": IndicatorSpec(
        key="atr",
        name="Average True Range",
        kind=IndicatorKind.OSCILLATOR,
        value_keys=("atr",),
        description="Average size of a bar's true range, in price units.",
        convention="Wilder's smoothing of true range. First value on bar `period`.",
        parameter_names=("atr_period",),
        compute=lambda candles, p: library.atr(candles, period=p.atr_period),
        warmup=lambda p: p.atr_period,
        used_parameters=lambda p: _params(p, "atr_period"),
    ),
    "adx": IndicatorSpec(
        key="adx",
        name="Average Directional Index",
        kind=IndicatorKind.OSCILLATOR,
        value_keys=("adx", "plus_di", "minus_di"),
        description="Trend strength (not direction), with the directional indicators.",
        convention="Wilder throughout: smoothed +DM/-DM/TR, DI, DX, then Wilder average of DX.",
        parameter_names=("adx_period",),
        compute=lambda candles, p: library.adx(candles, period=p.adx_period),
        warmup=lambda p: 2 * p.adx_period - 1,
        used_parameters=lambda p: _params(p, "adx_period"),
    ),
    "roc": IndicatorSpec(
        key="roc",
        name="Rate of Change",
        kind=IndicatorKind.OSCILLATOR,
        value_keys=("roc",),
        description="Percentage change over the period.",
        convention="100 x (close_i - close_{i-period}) / close_{i-period}.",
        parameter_names=("roc_period",),
        compute=lambda candles, p: library.roc(candles, period=p.roc_period),
        warmup=lambda p: p.roc_period,
        used_parameters=lambda p: _params(p, "roc_period"),
    ),
    "cci": IndicatorSpec(
        key="cci",
        name="Commodity Channel Index",
        kind=IndicatorKind.OSCILLATOR,
        value_keys=("cci",),
        description="Typical price relative to its recent mean, scaled by mean deviation.",
        convention="Lambert's constant 0.015; mean absolute deviation, not standard deviation.",
        parameter_names=("cci_period",),
        compute=lambda candles, p: library.cci(candles, period=p.cci_period),
        warmup=lambda p: p.cci_period - 1,
        used_parameters=lambda p: _params(p, "cci_period"),
    ),
}

INDICATOR_KEYS: Final[tuple[str, ...]] = tuple(INDICATORS)


def get_spec(key: str) -> IndicatorSpec | None:
    """Look up an indicator. Unknown keys fail closed, never by guessing."""
    return INDICATORS.get(key)


def describe_indicators() -> tuple[IndicatorDescriptor, ...]:
    """Catalogue for the terminal, so it never hardcodes what exists."""
    defaults = IndicatorParams()
    return tuple(
        IndicatorDescriptor(
            key=spec.key,
            name=spec.name,
            kind=spec.kind,
            value_keys=spec.value_keys,
            description=spec.description,
            parameters=spec.parameter_names,
            defaults=spec.used_parameters(defaults),
            convention=spec.convention,
        )
        for spec in INDICATORS.values()
    )
