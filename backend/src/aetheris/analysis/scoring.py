"""Market Opportunity Score.

**What this is:** a deterministic ranking metric that orders instruments by how
unusually active they are *right now*, computed as a weighted sum of four
present-tense measurements.

**What this is not:** a probability of profit, an expected return, a win rate, a
forecast of price, or a confidence. It contains no model, no training data and
no prediction. A score of 80 does not mean an 80% chance of anything. It means
this instrument currently scores highly against four published measurements
relative to published reference values.

Every component is returned alongside the score with its raw measurement,
normalized value, weight and contribution, so any number the UI shows can be
traced back to the arithmetic that produced it.

The reference values below are the judgement calls in this module. They are
normalization constants chosen so that a typical active perpetual lands in the
middle of the range -- they are deliberately explicit and versioned rather than
buried, so they can be argued with and changed.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal
from typing import Final

from aetheris.domain.scanner import OpportunityScore, ScannerMetrics, ScoreComponent

__all__ = ["SCORING_METHOD", "WEIGHTS", "score_opportunity"]

#: Bump when any weight, reference value or input definition changes, so a
#: stored score is never silently compared against one computed by different
#: arithmetic. v2: the volatility component now reads Wilder's ATR rather
#: than a plain mean of true ranges, unifying the product on one convention.
SCORING_METHOD: Final = "market-opportunity/v2"

_ZERO: Final = Decimal(0)
_ONE: Final = Decimal(1)
_HUNDRED: Final = Decimal(100)
_EXP: Final = Decimal("0.0001")
_SCORE_EXP: Final = Decimal("0.01")

#: Weights sum to exactly 1, so the score is bounded to 0-100 by construction.
WEIGHTS: Final[dict[str, Decimal]] = {
    "relative_volume": Decimal("0.30"),
    "volatility": Decimal("0.25"),
    "momentum": Decimal("0.25"),
    "trend_consistency": Decimal("0.20"),
}

#: Reference values at which a component is considered fully expressed.
#: Chosen from the scale of USDT-M perpetual behaviour: 3x its own recent
#: average volume is a marked surge; a 3% ATR is a highly volatile bar for an
#: hourly window; a 5% move over the momentum lookback is a decisive one.
_REFERENCE_RELATIVE_VOLUME: Final = Decimal("3")
_REFERENCE_ATR_PERCENT: Final = Decimal("3")
_REFERENCE_MOMENTUM_PERCENT: Final = Decimal("5")


def _clamp_unit(value: Decimal) -> Decimal:
    """Clamp to 0-1. Beyond the reference adds nothing further."""
    if value <= _ZERO:
        return _ZERO
    return min(value, _ONE)


def _q(value: Decimal, exponent: Decimal = _EXP) -> Decimal:
    return value.quantize(exponent, rounding=ROUND_HALF_UP)


def _component(
    name: str,
    *,
    raw: Decimal,
    normalized: Decimal,
    detail: str,
) -> ScoreComponent:
    weight = WEIGHTS[name]
    normalized = _clamp_unit(normalized)
    return ScoreComponent(
        name=name,
        raw_value=_q(raw),
        normalized=_q(normalized),
        weight=weight,
        contribution=_q(normalized * weight * _HUNDRED, _SCORE_EXP),
        detail=detail,
    )


def score_opportunity(metrics: ScannerMetrics) -> OpportunityScore | None:
    """Score one instrument from its computed metrics.

    Returns ``None`` when relative volume is unavailable -- that component
    carries the largest weight, and scoring without it would produce a number
    that is not comparable with the others. An incomparable score in a sorted
    table is worse than an absent one.
    """
    if metrics.relative_volume is None:
        return None

    components = (
        _component(
            "relative_volume",
            raw=metrics.relative_volume,
            normalized=metrics.relative_volume / _REFERENCE_RELATIVE_VOLUME,
            detail=(
                f"Last closed candle traded {metrics.relative_volume}x the mean volume "
                f"of the preceding {metrics.candles_used - 1} candles "
                f"(reference {_REFERENCE_RELATIVE_VOLUME}x)"
            ),
        ),
        _component(
            "volatility",
            raw=metrics.atr_percent,
            normalized=metrics.atr_percent / _REFERENCE_ATR_PERCENT,
            detail=(
                f"Average true range is {metrics.atr_percent}% of the last close "
                f"(reference {_REFERENCE_ATR_PERCENT}%)"
            ),
        ),
        _component(
            "momentum",
            # Direction is intentionally discarded: the scanner ranks activity,
            # and a sharp fall is as notable as a sharp rise. Direction is
            # reported separately as `trend`, so it is never lost -- only kept
            # out of a magnitude measure.
            raw=abs(metrics.momentum_percent),
            normalized=abs(metrics.momentum_percent) / _REFERENCE_MOMENTUM_PERCENT,
            detail=(
                f"Price moved {metrics.momentum_percent}% over the momentum lookback; "
                f"magnitude scored against a {_REFERENCE_MOMENTUM_PERCENT}% reference"
            ),
        ),
        _component(
            "trend_consistency",
            raw=metrics.trend_consistency,
            normalized=metrics.trend_consistency,
            detail=(
                f"{metrics.trend_consistency} of candles closed in the direction of the "
                f"net {metrics.window_return_percent}% move"
            ),
        ),
    )

    total = sum((c.contribution for c in components), _ZERO)
    return OpportunityScore(
        # Clamped defensively: the weights sum to 1 and each component is
        # clamped to 1, so this cannot exceed 100 -- but a future weight edit
        # must not be able to emit an out-of-range score.
        score=min(_q(total, _SCORE_EXP), _HUNDRED),
        components=components,
        method=SCORING_METHOD,
    )
