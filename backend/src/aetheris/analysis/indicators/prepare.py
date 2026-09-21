"""Candle normalisation before any indicator runs.

Indicator maths assumes bars are closed, in ascending time order, and unique.
Rather than assume that and produce quietly wrong numbers when it fails, every
calculation goes through this module first, and ambiguity is reported instead
of resolved by guessing.

The phase 2 adapter already guarantees ascending, unique candles at its own
boundary. This module exists because the phase 5 backtester will feed the same
engine from other sources -- files, fixtures, replayed archives -- where that
guarantee does not hold.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from itertools import pairwise

from aetheris.domain.market import Candle

__all__ = ["PreparationProblem", "PreparedCandles", "prepare_candles"]


class PreparationProblem(StrEnum):
    """Why a candle sequence could not be used as given."""

    NONE = "NONE"
    #: Timestamps repeat. Which bar is authoritative is genuinely unknowable,
    #: so this is refused rather than deduplicated by an arbitrary rule.
    DUPLICATE_TIMESTAMPS = "DUPLICATE_TIMESTAMPS"
    #: Neither ascending nor descending: the intended order cannot be inferred.
    AMBIGUOUS_ORDER = "AMBIGUOUS_ORDER"
    #: Nothing left after dropping bars that have not closed.
    NO_CLOSED_CANDLES = "NO_CLOSED_CANDLES"


@dataclass(frozen=True, slots=True)
class PreparedCandles:
    """Closed, ascending, unique candles -- or the reason there are none."""

    candles: tuple[Candle, ...]
    problem: PreparationProblem = PreparationProblem.NONE
    detail: str | None = None
    #: True when the input arrived newest-first and was reversed.
    reversed_order: bool = False
    #: Bars discarded because they had not closed at the evaluation time.
    dropped_forming: int = 0

    @property
    def ok(self) -> bool:
        return self.problem is PreparationProblem.NONE


def _is_strictly_ascending(candles: Sequence[Candle]) -> bool:
    return all(a.open_time < b.open_time for a, b in pairwise(candles))


def _is_strictly_descending(candles: Sequence[Candle]) -> bool:
    return all(a.open_time > b.open_time for a, b in pairwise(candles))


def prepare_candles(candles: Sequence[Candle], now: datetime) -> PreparedCandles:
    """Normalise a candle sequence for indicator use.

    Order of operations matters: ordering is settled *before* forming bars are
    dropped, because "the newest bar" is only meaningful once the direction of
    the series is known.
    """
    if not candles:
        return PreparedCandles(
            candles=(),
            problem=PreparationProblem.NO_CLOSED_CANDLES,
            detail="No candles supplied",
        )

    times = [candle.open_time for candle in candles]
    if len(set(times)) != len(times):
        return PreparedCandles(
            candles=(),
            problem=PreparationProblem.DUPLICATE_TIMESTAMPS,
            detail=(
                "Candle series contains repeated open times; which bar is "
                "authoritative cannot be determined"
            ),
        )

    reversed_order = False
    ordered = tuple(candles)
    if not _is_strictly_ascending(ordered):
        if _is_strictly_descending(ordered):
            # Newest-first is a common venue convention and unambiguous to fix.
            ordered = tuple(reversed(ordered))
            reversed_order = True
        else:
            return PreparedCandles(
                candles=(),
                problem=PreparationProblem.AMBIGUOUS_ORDER,
                detail=(
                    "Candle series is neither ascending nor descending; the "
                    "intended order cannot be inferred"
                ),
            )

    # Drop trailing bars that have not closed. A forming bar has partial
    # volume and an incomplete high/low, and letting one into an indicator
    # makes the newest value wrong on every refresh.
    end = len(ordered)
    while end > 0 and ordered[end - 1].close_time > now:
        end -= 1
    dropped = len(ordered) - end
    closed = ordered[:end]

    if not closed:
        return PreparedCandles(
            candles=(),
            problem=PreparationProblem.NO_CLOSED_CANDLES,
            detail=f"All {len(ordered)} candles are still forming",
            reversed_order=reversed_order,
            dropped_forming=dropped,
        )

    return PreparedCandles(
        candles=closed,
        reversed_order=reversed_order,
        dropped_forming=dropped,
    )
