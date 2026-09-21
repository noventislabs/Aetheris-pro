"""Strategy analysis models.

A strategy here produces an **analysis**, not an instruction. The vocabulary is
chosen to keep that distinction visible in every layer that touches it:
``LONG_BIAS`` describes what a rule set observes in the data, and carries no
claim about what price will do next.

There is deliberately no confidence, probability, win rate or expected return
anywhere in this module. What a reader gets instead is every condition the rule
set evaluated, its actual measured value, and whether it was satisfied -- which
is checkable, whereas a confidence number is not.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from aetheris.domain.enums import Timeframe
from aetheris.domain.leverage import LeverageDecision


class StrategyBias(StrEnum):
    """What the rule set observes in the data. Not a prediction or an order.

    ``NEUTRAL`` is a finding in its own right -- the conditions for neither
    direction were met, or they conflicted -- and is distinct from the analysis
    not having run, which is carried by the status instead.
    """

    LONG_BIAS = "LONG_BIAS"
    SHORT_BIAS = "SHORT_BIAS"
    NEUTRAL = "NEUTRAL"


class StrategyStatus(StrEnum):
    """Whether the analysis could be performed at all."""

    READY = "READY"
    #: A required indicator has not warmed up, or there are too few candles.
    INSUFFICIENT_DATA = "INSUFFICIENT_DATA"
    #: Candles are stale, or their freshness could not be verified. Either way
    #: the analysis is not usable and no bias is reported.
    STALE = "STALE"
    UNAVAILABLE = "UNAVAILABLE"
    ERROR = "ERROR"

    @property
    def has_bias(self) -> bool:
        return self is StrategyStatus.READY


class ConditionOutcome(BaseModel):
    """One rule, its measurement, and whether it held.

    This is the explanation. A caller can reproduce the verdict from these
    rows without trusting the engine, which is the point.
    """

    model_config = ConfigDict(frozen=True)

    name: str
    satisfied: bool
    detail: str = Field(description="Plain statement of what was measured and required")
    values: dict[str, Decimal] = Field(
        default_factory=dict, description="The actual numbers the rule compared"
    )


class StrategyResult(BaseModel):
    """The outcome of evaluating one strategy over one candle series."""

    model_config = ConfigDict(frozen=True)

    strategy: str
    name: str
    version: str
    status: StrategyStatus
    #: None unless status is READY -- an unrunnable analysis has no finding,
    #: and reporting NEUTRAL would imply one was made.
    bias: StrategyBias | None = None
    detail: str | None = None

    symbol: str
    timeframe: Timeframe
    parameters: dict[str, Decimal] = Field(default_factory=dict)

    long_conditions: tuple[ConditionOutcome, ...] = ()
    short_conditions: tuple[ConditionOutcome, ...] = ()
    #: How many of each direction's rules held. A count, deliberately not a
    #: score: 3/4 says exactly what it says and cannot be mistaken for a
    #: probability.
    long_conditions_met: int = Field(default=0, ge=0)
    short_conditions_met: int = Field(default=0, ge=0)
    conditions_total: int = Field(default=0, ge=0)

    #: The leverage constraint chain. Present whenever the analysis ran, and
    #: in this build always a rejection: the venue ceiling is unknown and the
    #: risk engine does not exist. Reported rather than omitted so the
    #: missing link is visible.
    leverage: LeverageDecision | None = None

    indicators_used: tuple[str, ...] = ()
    candles_used: int = Field(default=0, ge=0)
    last_candle_time: datetime | None = None
    source: str | None = None
    data_status: str | None = None
    data_age_seconds: float | None = None
    evaluated_at: datetime | None = None

    #: Repeated on every result so it survives being copied into a screenshot,
    #: a log line, or a downstream payload.
    disclaimer: str = (
        "Analysis only. This is a deterministic reading of indicator values, not a "
        "trade recommendation, a prediction of future price, a probability of profit, "
        "or financial advice. No order is placed by this system."
    )


class StrategyDescriptor(BaseModel):
    """Catalogue entry for one registered strategy."""

    model_config = ConfigDict(frozen=True)

    key: str
    name: str
    version: str
    description: str
    required_indicators: tuple[str, ...]
    supported_timeframes: tuple[Timeframe, ...]
    parameters: dict[str, Decimal]
    rules: tuple[str, ...] = Field(description="The rule set in plain language")
    available: bool = Field(description="False for a registered but unimplemented strategy")
