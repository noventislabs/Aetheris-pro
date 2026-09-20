"""Market Opportunity Score: bounded, explainable, deterministic.

The score orders a table. It is not a probability, a forecast or a confidence,
and these tests pin the properties that keep it honest: it cannot leave 0-100,
every point is attributable to a published component, and it is withheld rather
than approximated when an input is missing.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from aetheris.analysis.scoring import SCORING_METHOD, WEIGHTS, score_opportunity
from aetheris.domain.enums import Timeframe
from aetheris.domain.scanner import ScannerMetrics, TrendDirection


def metrics(
    *,
    relative_volume: str | None = "1.5",
    atr_percent: str = "1.5",
    momentum_percent: str = "2.5",
    trend_consistency: str = "0.5",
) -> ScannerMetrics:
    return ScannerMetrics(
        timeframe=Timeframe.H1,
        candles_used=20,
        window_return_percent=Decimal("5"),
        momentum_percent=Decimal(momentum_percent),
        volatility_percent=Decimal("1.2"),
        atr=Decimal("15"),
        atr_percent=Decimal(atr_percent),
        average_volume=Decimal("1000"),
        last_volume=Decimal("1500"),
        relative_volume=Decimal(relative_volume) if relative_volume is not None else None,
        range_percent=Decimal("4"),
        body_percent=Decimal("55"),
        trend=TrendDirection.UP,
        trend_consistency=Decimal(trend_consistency),
    )


def test_weights_sum_to_one() -> None:
    """The 0-100 bound is structural, not enforced by clamping alone."""
    assert sum(WEIGHTS.values()) == Decimal("1.00")


def test_score_is_within_bounds() -> None:
    score = score_opportunity(metrics())
    assert score is not None
    assert Decimal(0) <= score.score <= Decimal(100)


def test_maximum_inputs_produce_exactly_one_hundred() -> None:
    score = score_opportunity(
        metrics(
            relative_volume="99",
            atr_percent="99",
            momentum_percent="99",
            trend_consistency="1",
        )
    )
    assert score is not None
    assert score.score == Decimal("100.00")


def test_minimum_inputs_produce_zero() -> None:
    score = score_opportunity(
        metrics(
            relative_volume="0",
            atr_percent="0",
            momentum_percent="0",
            trend_consistency="0",
        )
    )
    assert score is not None
    assert score.score == Decimal("0.00")


def test_score_equals_the_sum_of_its_components() -> None:
    """Every point is attributable — the number has no unexplained remainder."""
    score = score_opportunity(metrics())
    assert score is not None
    assert sum(c.contribution for c in score.components) == score.score


def test_all_four_components_are_reported_with_explanations() -> None:
    score = score_opportunity(metrics())
    assert score is not None
    names = {c.name for c in score.components}
    assert names == set(WEIGHTS)
    for component in score.components:
        assert component.detail
        assert component.weight == WEIGHTS[component.name]
        assert Decimal(0) <= component.normalized <= Decimal(1)


def test_missing_relative_volume_withholds_the_score() -> None:
    """A score missing its heaviest component is not comparable with the rest."""
    assert score_opportunity(metrics(relative_volume=None)) is None


def test_momentum_is_scored_as_magnitude_not_direction() -> None:
    """A sharp fall is as notable as a sharp rise; direction lives in `trend`."""
    up = score_opportunity(metrics(momentum_percent="4"))
    down = score_opportunity(metrics(momentum_percent="-4"))
    assert up is not None and down is not None
    assert up.score == down.score


def test_higher_activity_scores_higher() -> None:
    quiet = score_opportunity(
        metrics(relative_volume="0.5", atr_percent="0.2", momentum_percent="0.1")
    )
    busy = score_opportunity(
        metrics(relative_volume="2.5", atr_percent="2.5", momentum_percent="4")
    )
    assert quiet is not None and busy is not None
    assert busy.score > quiet.score


def test_beyond_the_reference_adds_nothing_further() -> None:
    at_reference = score_opportunity(metrics(relative_volume="3"))
    far_beyond = score_opportunity(metrics(relative_volume="300"))
    assert at_reference is not None and far_beyond is not None
    assert at_reference.score == far_beyond.score


def test_score_is_deterministic() -> None:
    assert score_opportunity(metrics()) == score_opportunity(metrics())


def test_method_is_versioned() -> None:
    """A stored score must never be compared against different arithmetic."""
    score = score_opportunity(metrics())
    assert score is not None
    assert score.method == SCORING_METHOD


@pytest.mark.parametrize(
    "component",
    ["relative_volume", "volatility", "momentum", "trend_consistency"],
)
def test_every_weight_actually_influences_the_score(component: str) -> None:
    """No component is decorative: changing each one moves the result."""
    zeroed: dict[str, str | None] = {
        "relative_volume": "0",
        "atr_percent": "0",
        "momentum_percent": "0",
        "trend_consistency": "0",
    }
    raises_to_reference = {
        "relative_volume": ("relative_volume", "3"),
        "volatility": ("atr_percent", "3"),
        "momentum": ("momentum_percent", "5"),
        "trend_consistency": ("trend_consistency", "1"),
    }
    field, value = raises_to_reference[component]

    baseline = score_opportunity(metrics(**zeroed))  # type: ignore[arg-type]
    bumped = score_opportunity(metrics(**{**zeroed, field: value}))  # type: ignore[arg-type]

    assert baseline is not None and bumped is not None
    assert baseline.score == Decimal("0.00")
    # Raising one component to exactly its reference contributes exactly its
    # weight, and nothing else moves.
    assert bumped.score == WEIGHTS[component] * 100
