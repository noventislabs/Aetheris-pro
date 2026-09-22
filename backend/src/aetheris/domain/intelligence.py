"""Position intelligence: the shared vocabulary every brain speaks.

## What this is not

There is no confidence, probability, expected return or predicted outcome
anywhere in this module, and none may be added. Nothing in this system is a
calibrated model, so any such number would be invented. What a brain returns
instead is *evidence*: what it measured, what it compared that to, and whether
the comparison held. A reader can re-derive the verdict from the rows.

## UNAVAILABLE is a first-class answer

The single most dangerous failure here is a brain that could not measure
something reporting a neutral or healthy state instead of saying so. A stale
mark is not a calm market; a missing indicator is not a satisfied condition;
an absent thesis is not a weak one. So ``UNAVAILABLE`` is a state every brain
can return, and the orchestrator is required to treat it as missing input
rather than as an unremarkable reading.

## Why the decision ladder is precedence, not a vote

A weighted vote lets several working brains outvote one that failed, which is
exactly backwards: a brain that cannot see is the strongest reason to stop,
not the weakest. So the orchestrator walks a fixed order and returns the first
condition that matches.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class DataQuality(StrEnum):
    """How much the reading underneath an evaluation can be trusted."""

    #: Fresh, verifiable, complete.
    OK = "OK"
    #: Usable, but something is missing or ageing. Named on the result.
    DEGRADED = "DEGRADED"
    #: Not usable. Any state derived from it is not a finding.
    UNAVAILABLE = "UNAVAILABLE"


class BrainState(StrEnum):
    """One brain's verdict on its own subject.

    Deliberately coarse. A brain says whether what it watches still supports
    the position, not how strongly -- strength would be a confidence by
    another name.
    """

    #: What this brain watches still supports the position.
    SUPPORTIVE = "SUPPORTIVE"
    #: Measured, and it neither supports nor opposes.
    NEUTRAL = "NEUTRAL"
    #: Measured, and it has moved against the position without reversing.
    WEAKENING = "WEAKENING"
    #: Measured, and it now actively contradicts the position.
    ADVERSE = "ADVERSE"
    #: Could not be measured. Never a synonym for NEUTRAL.
    UNAVAILABLE = "UNAVAILABLE"

    @property
    def is_measured(self) -> bool:
        return self is not BrainState.UNAVAILABLE


class BrainName(StrEnum):
    MARKET = "MARKET"
    TECHNICAL = "TECHNICAL"
    REGIME = "REGIME"
    MOMENTUM_VOLUME = "MOMENTUM_VOLUME"
    STRUCTURE = "STRUCTURE"
    POSITION = "POSITION"
    RISK = "RISK"
    EXIT = "EXIT"


class Evidence(BaseModel):
    """One measurement, what it was compared against, and the outcome.

    ``observed`` and ``reference`` are strings so a brain can record a price,
    a count, a regime name or "unavailable" without the model needing a union
    per kind. They are for reading, not arithmetic -- anything a caller needs
    to compute with belongs in a typed field elsewhere.
    """

    model_config = ConfigDict(frozen=True)

    name: str
    observed: str | None = None
    reference: str | None = None
    #: ``None`` when the comparison could not be made at all, which is
    #: distinct from making it and finding it false.
    satisfied: bool | None = None
    detail: str


class BrainResult(BaseModel):
    """What every brain returns, without exception."""

    model_config = ConfigDict(frozen=True)

    brain: BrainName
    state: BrainState
    evidence: tuple[Evidence, ...] = ()
    data_quality: DataQuality
    #: Where the reading came from -- a venue adapter, the position record,
    #: the risk engine. ``None`` only when nothing could be read at all.
    source: str | None = None
    observed_at: datetime

    @property
    def usable(self) -> bool:
        return self.state.is_measured and self.data_quality is not DataQuality.UNAVAILABLE


class ThesisState(StrEnum):
    """Whether the reason the position was opened still holds."""

    THESIS_VALID = "THESIS_VALID"
    THESIS_WEAKENING = "THESIS_WEAKENING"
    REVERSAL_WARNING = "REVERSAL_WARNING"
    TARGET_APPROACHING = "TARGET_APPROACHING"
    RISK_INCREASING = "RISK_INCREASING"
    EXIT_CONDITION_MET = "EXIT_CONDITION_MET"
    INSUFFICIENT_DATA = "INSUFFICIENT_DATA"
    BLOCKED_BY_RISK = "BLOCKED_BY_RISK"


class PositionDecision(StrEnum):
    """What the orchestrator suggests. Advisory only -- nothing acts on it."""

    HOLD = "HOLD"
    PROTECT = "PROTECT"
    TRAIL = "TRAIL"
    #: Structurally supported so the vocabulary is complete, but no engine can
    #: execute it: ``_close`` takes no quantity in either paper or testnet.
    #: Reported with ``partial_exit_available = False`` so nobody reads it as
    #: an action that could be taken.
    PARTIAL_EXIT = "PARTIAL_EXIT"
    EXIT = "EXIT"
    INSUFFICIENT_DATA = "INSUFFICIENT_DATA"
    BLOCKED_BY_RISK = "BLOCKED_BY_RISK"


class ThesisAlignment(BaseModel):
    """How many of the original entry conditions still hold. Nothing more.

    This is a count expressed on a 0-100 scale, not a score over weighted
    factors and emphatically not a probability. If the original thesis
    recorded no conditions -- a manual entry, for instance -- there is
    nothing to measure and the value is absent rather than zero.
    """

    model_config = ConfigDict(frozen=True)

    value: Decimal = Field(ge=0, le=100)
    conditions_held: int = Field(ge=0)
    conditions_total: int = Field(gt=0)
    meaning: str = (
        "The percentage of the ORIGINAL entry conditions that still hold, recounted "
        "against current values. NOT a probability of profit, a confidence, an "
        "expected return or a forecast. A position can be deeply profitable with low "
        "alignment, and aligned while losing."
    )


class PositionIntelligence(BaseModel):
    """The orchestrator's complete answer for one open position."""

    model_config = ConfigDict(frozen=True)

    symbol: str
    position_id: str
    decision: PositionDecision
    thesis_state: ThesisState

    #: Plain statements, in precedence order, of what produced the decision.
    reasons: tuple[str, ...] = ()
    brain_results: tuple[BrainResult, ...] = ()

    #: Absent when the thesis recorded no conditions to measure against.
    thesis_alignment: ThesisAlignment | None = None

    #: The risk engine's current posture, read rather than recomputed.
    risk_state: str
    data_quality: DataQuality
    evaluated_at: datetime

    #: Capabilities this build cannot perform, named so a reader never has to
    #: infer absence from silence.
    partial_exit_available: bool = False
    unavailable_capabilities: tuple[str, ...] = ()

    disclaimer: str = (
        "Advisory analysis of an open position. It places no order, moves no stop and "
        "changes no leverage. Every figure is derived from observed market data and the "
        "thesis recorded when the position opened; none of it is a probability, a "
        "confidence or a prediction. Execution remains subject to the risk engine, "
        "which has final authority."
    )


class PositionMetrics(BaseModel):
    """Measured facts about one open position.

    Every field is ``None`` when it genuinely could not be computed, and the
    reason is on the matching evidence row. None of these are ever defaulted:
    a remaining-R of zero means the stop is at the mark, not that nobody knew.
    """

    model_config = ConfigDict(frozen=True)

    time_in_trade_seconds: Decimal = Field(ge=0)

    #: Extremes actually observed while open, and the excursion they imply.
    #: Signed so that favourable is positive for both sides.
    best_price: Decimal | None = None
    worst_price: Decimal | None = None
    mfe_per_unit: Decimal | None = Field(default=None, ge=0)
    mae_per_unit: Decimal | None = Field(default=None, ge=0)
    mfe_percent: Decimal | None = Field(default=None, ge=0)
    mae_percent: Decimal | None = Field(default=None, ge=0)

    #: The price at which closing now would net zero after both fees.
    break_even_price: Decimal | None = Field(default=None, gt=0)

    #: Distance to each level as a percentage of the mark. ``None`` when the
    #: level does not exist or the mark is unusable.
    stop_distance_percent: Decimal | None = None
    target_distance_percent: Decimal | None = None
    liquidation_distance_percent: Decimal | None = None

    #: Current outcome in multiples of the risk planned at entry, and how much
    #: of that planned risk is still between the mark and the stop. Both
    #: require a stop recorded in the thesis; absent otherwise.
    r_multiple_now: Decimal | None = None
    remaining_r: Decimal | None = None

    unrealized_pnl: Decimal | None = None
    #: Named so a reader can see which of the above were not computable.
    unavailable: tuple[str, ...] = ()
