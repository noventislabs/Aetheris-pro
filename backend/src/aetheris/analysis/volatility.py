"""Measured volatility, and why it could not be measured.

One definition, shared by the manual and autonomous order paths. Two callers
computing ATR slightly differently is the same class of problem as two callers
resolving leverage differently -- which is what ADR 0006 exists to fix.

**The failure modes are values, not exceptions.** A caller needs to tell
"there is not enough history" from "the candles did not arrive", because those
produce different refusals and the user can act on one and not the other. A
function that returned ``None`` for both would collapse that distinction at the
only place it could be preserved.

Nothing here substitutes a value. If volatility cannot be measured the answer
says so, and the risk engine refuses -- because approving an order sized against
an assumed volatility is worse than refusing one.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Final

from aetheris.analysis.indicators.library import atr as atr_indicator
from aetheris.core.money import ZERO
from aetheris.domain.market import Candle

__all__ = [
    "ATR_PERIOD",
    "MIN_CANDLES_FOR_ATR",
    "VolatilityMeasurement",
    "VolatilityStatus",
    "measure_atr_percent",
]

ATR_PERIOD: Final = 14
#: Wilder's ATR over 14 periods needs 15 closed bars: fourteen true ranges plus
#: the bar they are measured against.
MIN_CANDLES_FOR_ATR: Final = ATR_PERIOD + 1

_HUNDRED: Final = Decimal(100)


class VolatilityStatus(StrEnum):
    """Whether volatility is known, and if not, which kind of not-known."""

    MEASURED = "MEASURED"
    #: The candles arrived and there are not enough of them. A newly listed
    #: instrument. Waiting for a fresher tick does not help; what is missing is
    #: the past.
    INSUFFICIENT_HISTORY = "INSUFFICIENT_HISTORY"
    #: The candles did not arrive at all.
    CANDLES_UNAVAILABLE = "CANDLES_UNAVAILABLE"
    #: The candles arrived but are too old to describe the present.
    CANDLES_STALE = "CANDLES_STALE"

    @property
    def is_measured(self) -> bool:
        return self is VolatilityStatus.MEASURED


@dataclass(frozen=True, slots=True)
class VolatilityMeasurement:
    """ATR as a percent of price, or the reason there is none.

    ``atr_percent`` is present if and only if ``status`` is ``MEASURED`` -- the
    same rule the ``Observation`` envelope applies to market data, for the same
    reason: a consumer that checks the status cannot then read a fabricated
    number, and one that forgets to check gets ``None`` rather than a
    plausible-looking lie.
    """

    status: VolatilityStatus
    atr_percent: Decimal | None = None
    candles_used: int = 0
    detail: str | None = None

    def __post_init__(self) -> None:
        if self.status.is_measured and self.atr_percent is None:
            raise ValueError("a MEASURED volatility must carry a value")
        if not self.status.is_measured and self.atr_percent is not None:
            raise ValueError(f"volatility with status {self.status} must not carry a value")


def measure_atr_percent(candles: Sequence[Candle]) -> VolatilityMeasurement:
    """Measure ATR as a percent of the last close, over closed bars.

    Callers pass candles that have already been prepared -- closed, ascending,
    unique. An empty sequence means the fetch produced nothing, which is
    reported as ``CANDLES_UNAVAILABLE`` rather than as insufficient history:
    the distinction between "the venue gave us nothing" and "this instrument is
    too new" matters to whoever reads the refusal.
    """
    if not candles:
        return VolatilityMeasurement(
            status=VolatilityStatus.CANDLES_UNAVAILABLE,
            detail="No candles were available to measure volatility from.",
        )

    if len(candles) < MIN_CANDLES_FOR_ATR:
        return VolatilityMeasurement(
            status=VolatilityStatus.INSUFFICIENT_HISTORY,
            candles_used=len(candles),
            detail=(
                f"Only {len(candles)} closed candle(s) are available; ATR({ATR_PERIOD}) "
                f"needs {MIN_CANDLES_FOR_ATR}. This instrument does not have enough "
                "history yet, and volatility is not estimated from a shorter window."
            ),
        )

    series = atr_indicator(candles, period=ATR_PERIOD)["atr"]
    value = next((entry for entry in reversed(series) if entry is not None), None)
    last_close = candles[-1].close

    if value is None or last_close <= ZERO:
        return VolatilityMeasurement(
            status=VolatilityStatus.INSUFFICIENT_HISTORY,
            candles_used=len(candles),
            detail=(
                "ATR did not produce a value over the available candles, so volatility "
                "is unknown rather than assumed."
            ),
        )

    return VolatilityMeasurement(
        status=VolatilityStatus.MEASURED,
        atr_percent=(value / last_close * _HUNDRED).quantize(Decimal("0.0001")),
        candles_used=len(candles),
    )
