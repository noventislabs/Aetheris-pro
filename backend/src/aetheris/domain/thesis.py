"""The original trade thesis, fixed at the moment a position opens.

## Why this has to be captured rather than recomputed

The question position intelligence exists to answer is "does the reason we
opened this still hold?". That is only answerable if the reason was written
down at the time. Recomputing it later from current candles answers a
different question -- "would we open this now?" -- and quietly uses bars that
did not exist when the decision was made. Every field here is therefore
recorded once, at entry, and never updated.

That is also why the model is frozen. A thesis that could be amended after the
fact is not evidence of anything.

## An absent thesis is a real answer

A manually submitted order carries no strategy context, because a human
clicking buy did not evaluate a rule set. That case is represented explicitly
as ``NO_STRATEGY_CONTEXT`` rather than as an empty ``CAPTURED`` thesis with
zeroed fields. Inventing a strategy, a score or a set of conditions for a
manual entry would be fabricating the exact evidence the rest of the system
reasons over.

The price half of the thesis -- entry, stop, target, planned risk -- is
recorded for *every* position, manual or not, because those are facts about
the position rather than claims about a rule set.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator

from aetheris.domain.enums import PositionSide, Timeframe
from aetheris.domain.regime import MarketRegime, VolatilityBand
from aetheris.domain.setup import SetupScoreComponent
from aetheris.domain.strategy import ConditionOutcome


class ThesisStatus(StrEnum):
    """Whether a rule set stood behind this position at entry."""

    #: A strategy evaluated the instrument and the position followed from it.
    CAPTURED = "CAPTURED"
    #: No rule set was involved -- a human submitted the order directly. The
    #: price levels are still recorded; the strategy fields are genuinely
    #: absent, not zero.
    NO_STRATEGY_CONTEXT = "NO_STRATEGY_CONTEXT"

    @property
    def has_strategy(self) -> bool:
        return self is ThesisStatus.CAPTURED


class StrategyContext(BaseModel):
    """What a rule set knew when it proposed the entry.

    Supplied by the caller at submission, never derived by the engine. The
    engine has no access to a strategy evaluation and must not invent one, so
    a caller that has no rule set behind it simply passes nothing.
    """

    model_config = ConfigDict(frozen=True)

    strategy_id: str
    strategy_version: str
    timeframe: Timeframe

    #: Rule alignment at entry, on the published 0-100 scale. NOT a
    #: probability -- see ``SetupScore``. ``None`` when the strategy produced
    #: a direction without a scored setup.
    setup_score: Decimal | None = Field(default=None, ge=0, le=100)
    score_components: tuple[SetupScoreComponent, ...] = ()

    #: The conditions that held at entry. This is what "does the thesis still
    #: hold" is later measured against, so it is the single most important
    #: field in the model.
    entry_conditions: tuple[ConditionOutcome, ...] = ()

    regime: MarketRegime | None = None
    volatility_band: VolatilityBand | None = None

    #: Provenance of the candles the evaluation ran over.
    data_source: str | None = None
    data_status: str | None = None
    data_age_seconds: float | None = None
    #: Close time of the bar the decision was made on. Distinct from the
    #: capture timestamp: one is market time, the other is wall-clock, and a
    #: gap between them is itself informative.
    bar_close_time: datetime | None = None


class PositionThesis(BaseModel):
    """Why a position was opened, as recorded at the moment it opened.

    Immutable by construction. Nothing in the system updates a thesis -- the
    whole point is that it stays fixed while the market moves around it.
    """

    model_config = ConfigDict(frozen=True)

    status: ThesisStatus
    captured_at: datetime
    side: PositionSide

    #: Facts about the position, recorded whether or not a strategy was
    #: involved.
    entry_price: Decimal = Field(gt=0)
    stop_price: Decimal | None = None
    target_price: Decimal | None = None

    #: Planned risk per unit at entry: ``abs(entry - stop)``. ``None`` when no
    #: stop was set, and then remaining-R is genuinely uncomputable rather
    #: than defaulted to something convenient.
    planned_risk_per_unit: Decimal | None = Field(default=None, gt=0)
    #: Planned risk in quote currency: ``planned_risk_per_unit x quantity``.
    planned_risk_total: Decimal | None = Field(default=None, gt=0)
    planned_reward_per_unit: Decimal | None = Field(default=None, gt=0)
    risk_reward_ratio: Decimal | None = Field(default=None, gt=0)

    #: Absent for a manual entry. Present exactly when status is CAPTURED.
    strategy: StrategyContext | None = None

    #: Why the strategy fields are absent, when they are.
    detail: str | None = None

    @model_validator(mode="after")
    def _status_matches_contents(self) -> PositionThesis:
        """The status is a promise about what is present, so enforce it.

        Without this a consumer would have to null-check a field the status
        already claimed was there, and the first one that forgot would read a
        missing strategy as a strategy that failed.
        """
        if self.status is ThesisStatus.CAPTURED and self.strategy is None:
            raise ValueError("a CAPTURED thesis must carry its strategy context")
        if self.status is ThesisStatus.NO_STRATEGY_CONTEXT and self.strategy is not None:
            raise ValueError(
                "a NO_STRATEGY_CONTEXT thesis must not carry strategy context; "
                "the status would be claiming the opposite of the payload"
            )
        return self

    @property
    def condition_count(self) -> int:
        """How many entry conditions there are to measure against later.

        Zero means thesis alignment cannot be computed at all, which is a
        different answer from alignment being low.
        """
        return 0 if self.strategy is None else len(self.strategy.entry_conditions)
