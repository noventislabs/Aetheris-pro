"""Indicator result models.

An indicator result carries its own status rather than relying on the caller to
infer readiness from a null value. ``WARMING_UP`` and ``INSUFFICIENT_DATA`` are
distinct on purpose: the first says "ask again in a few bars", the second says
"this series will never support this parameter set".
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from aetheris.domain.enums import Timeframe


class IndicatorStatus(StrEnum):
    """Whether an indicator produced a usable value, and if not why."""

    #: Warmed up and carrying a latest value for every line.
    READY = "READY"
    #: The series is long enough, but the most recent bar still has no value --
    #: either a slower line of a multi-line indicator has not caught up, or the
    #: value is mathematically undefined at this particular bar (a stochastic
    #: over a window with no range). More bars, or a different bar, would help.
    WARMING_UP = "WARMING_UP"
    #: The supplied series is shorter than this parameter set needs, so nothing
    #: can be computed at all. A longer series would fix it; these are simply
    #: all the candles there are.
    INSUFFICIENT_DATA = "INSUFFICIENT_DATA"
    #: Candles could not be retrieved, or arrived stale/unusable.
    UNAVAILABLE = "UNAVAILABLE"
    ERROR = "ERROR"

    @property
    def has_value(self) -> bool:
        return self is IndicatorStatus.READY


class IndicatorKind(StrEnum):
    """Where the indicator belongs on a chart.

    Carried so the terminal does not need its own hardcoded list to decide
    whether something is drawn over price or in its own pane.
    """

    #: Shares the price axis (SMA, EMA, Bollinger, VWAP).
    OVERLAY = "OVERLAY"
    #: Needs its own pane and scale (RSI, MACD, Stochastic, ADX, ROC, CCI).
    OSCILLATOR = "OSCILLATOR"


class IndicatorPoint(BaseModel):
    """One bar's worth of indicator output.

    ``values`` may contain ``None`` for a line that has not warmed up or is
    undefined at this bar, while a sibling line in the same indicator has.
    """

    model_config = ConfigDict(frozen=True)

    time: datetime
    values: dict[str, Decimal | None]


class IndicatorResult(BaseModel):
    """One indicator computed over one candle series."""

    model_config = ConfigDict(frozen=True)

    indicator: str
    name: str
    kind: IndicatorKind
    status: IndicatorStatus
    detail: str | None = None
    parameters: dict[str, Decimal] = Field(
        default_factory=dict, description="Parameters actually used, after validation"
    )
    value_keys: tuple[str, ...] = Field(
        description="Names of the lines this indicator produces, e.g. macd/signal/histogram"
    )
    latest: dict[str, Decimal | None] | None = Field(
        default=None, description="Most recent bar's values; None unless status is READY"
    )
    latest_time: datetime | None = None
    warmup_bars: int = Field(ge=0, description="Bars consumed before the first value")
    candles_used: int = Field(ge=0)
    series: tuple[IndicatorPoint, ...] = Field(
        default=(), description="Bounded history; empty unless explicitly requested"
    )


class IndicatorSet(BaseModel):
    """Several indicators over one symbol and timeframe, with shared provenance."""

    model_config = ConfigDict(frozen=True)

    symbol: str
    timeframe: Timeframe
    source: str
    data_status: str = Field(description="Freshness status of the underlying candles")
    data_detail: str | None = None
    #: None when the venue supplied no event timestamp, which means freshness
    #: could not be verified rather than that the data is current.
    data_age_seconds: float | None = None
    candle_count: int = Field(ge=0)
    last_candle_time: datetime | None = None
    indicators: tuple[IndicatorResult, ...]


class IndicatorDescriptor(BaseModel):
    """Catalogue entry describing one available indicator."""

    model_config = ConfigDict(frozen=True)

    key: str
    name: str
    kind: IndicatorKind
    value_keys: tuple[str, ...]
    description: str
    parameters: tuple[str, ...]
    defaults: dict[str, Decimal]
    convention: str
