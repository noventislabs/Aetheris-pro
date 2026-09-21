"""Market regime -- a reading of what kind of market this currently is.

A regime is a *classification of present conditions*, not a forecast. Saying a
market is ``TREND_UP`` asserts that trend-strength and direction measurements
currently read that way; it asserts nothing about the next bar.

**Two axes, reported separately.** Trend and volatility answer different
questions, and collapsing them loses information: a strong uptrend can be calm
or violent, and those call for different position sizes. The single ``regime``
field follows the coarse vocabulary the rest of the system speaks, while
``volatility_band`` is always reported alongside it so the second axis is never
discarded to fit the first.

There is no confidence, probability or score in this module. A regime either
was measured from real indicator values, or it is ``UNKNOWN``.
"""

from __future__ import annotations

from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class MarketRegime(StrEnum):
    """The coarse classification.

    ``UNKNOWN`` is a real answer, not a failure code: it is what an honest
    classifier returns when a required measurement is unavailable. It must
    never be treated as a synonym for ``RANGE``.
    """

    TREND_UP = "TREND_UP"
    TREND_DOWN = "TREND_DOWN"
    RANGE = "RANGE"
    HIGH_VOLATILITY = "HIGH_VOLATILITY"
    LOW_VOLATILITY = "LOW_VOLATILITY"
    UNKNOWN = "UNKNOWN"

    @property
    def is_trending(self) -> bool:
        return self in (MarketRegime.TREND_UP, MarketRegime.TREND_DOWN)

    @property
    def is_known(self) -> bool:
        return self is not MarketRegime.UNKNOWN


class VolatilityBand(StrEnum):
    """The volatility axis, always reported even when trend is the headline."""

    HIGH = "HIGH"
    NORMAL = "NORMAL"
    LOW = "LOW"
    UNKNOWN = "UNKNOWN"


class RegimeMeasurement(BaseModel):
    """One input to the classification, with the threshold it was compared to.

    Present so the verdict can be recomputed by hand. A classifier that only
    returns its answer is one nobody can check.
    """

    model_config = ConfigDict(frozen=True)

    name: str
    value: Decimal | None = Field(
        default=None, description="The measurement, or None when it could not be taken"
    )
    threshold: Decimal | None = Field(
        default=None, description="What it was compared against, when a comparison applies"
    )
    detail: str


class RegimeAssessment(BaseModel):
    """The classification, its inputs, and the reasoning that connects them."""

    model_config = ConfigDict(frozen=True)

    regime: MarketRegime
    volatility_band: VolatilityBand
    #: Every measurement taken, including the ones that came back unavailable.
    measurements: tuple[RegimeMeasurement, ...] = ()
    #: Plain statement of which rule fired and why.
    reason: str
    #: Bumped when any threshold or rule changes, so a stored assessment is
    #: never compared against one produced by different arithmetic.
    method: str
    candles_used: int = Field(default=0, ge=0)

    @property
    def actionable(self) -> bool:
        """Whether a strategy may lean on this reading at all.

        ``UNKNOWN`` is not actionable. Everything else is a real measurement,
        and what to *do* with it is the strategy's decision, not this model's.
        """
        return self.regime.is_known
