"""Trade setups -- a scored, explainable reading of one instrument right now.

## What a setup score is, and is not

``SetupScore.value`` is **strategy alignment**, on a bounded 0-100 scale. It
answers one question: how completely do the current measurements satisfy this
strategy's published rules, and is the resulting trade worth its own risk?

It is **not** a probability of profit, a win rate, an expected return, a
confidence or a forecast. A score of 82 means "strong rule alignment". It does
not mean an 82% chance of anything, and there is no model anywhere in this
system that could produce such a number honestly.

The two are kept structurally apart. A setup score is computed from *present*
measurements; historical strategy performance lives in ``BacktestMetrics`` and
is never folded in. That separation is deliberate -- see
``analysis.setup`` for why "historical stability" is excluded from the score
rather than weighted into it.

## Actionability

A setup that cannot state a real stop and a real target is not actionable, and
says so with ``NO_ACTIONABLE_SETUP`` rather than inventing a level. The same
applies to stale data: old candles are not a reason to trade.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator

from aetheris.domain.enums import Timeframe
from aetheris.domain.regime import RegimeAssessment
from aetheris.domain.strategy import ConditionOutcome


class SetupDirection(StrEnum):
    """Which side the rules point to, if any.

    ``NO_SIGNAL`` is a finding: the rules were evaluated and neither side's
    conditions were met. It is distinct from the evaluation not having run,
    which the status carries.
    """

    LONG = "LONG"
    SHORT = "SHORT"
    NO_SIGNAL = "NO_SIGNAL"


class SetupStatus(StrEnum):
    """Whether this reading may be acted on at all."""

    #: Rules evaluated, a direction found, and a real stop and target derived.
    ACTIONABLE = "ACTIONABLE"
    #: Evaluated successfully, but there is nothing to act on -- either no
    #: direction, or no safely derivable stop or target.
    NO_ACTIONABLE_SETUP = "NO_ACTIONABLE_SETUP"
    INSUFFICIENT_DATA = "INSUFFICIENT_DATA"
    STALE = "STALE"
    UNAVAILABLE = "UNAVAILABLE"

    @property
    def is_actionable(self) -> bool:
        return self is SetupStatus.ACTIONABLE


class StopModel(StrEnum):
    """How the stop level was derived.

    Recorded on the setup because the same price means different things under
    different models, and a reader comparing two setups needs to know which
    produced each one.
    """

    FIXED_PERCENT = "FIXED_PERCENT"
    ATR = "ATR"
    STRUCTURE = "STRUCTURE"


class SetupScoreComponent(BaseModel):
    """One factor's contribution, with the raw measurement behind it.

    Carried so the total can be recomputed by hand from the rows. A score
    whose arithmetic is not reproducible is a number to be trusted rather
    than checked, which is the thing this system does not do.
    """

    model_config = ConfigDict(frozen=True)

    name: str
    raw_value: Decimal | None = Field(
        default=None, description="The measurement in its own units, or None if unavailable"
    )
    normalized: Decimal = Field(ge=0, le=1, description="Clamped to 0-1 against a reference")
    weight: Decimal = Field(ge=0, le=1)
    contribution: Decimal = Field(ge=0, description="normalized x weight x 100")
    detail: str


class SetupScore(BaseModel):
    """Bounded, deterministic, explainable strategy alignment."""

    model_config = ConfigDict(frozen=True)

    value: Decimal = Field(ge=0, le=100)
    components: tuple[SetupScoreComponent, ...]
    method: str = Field(description="Versioned identifier for the weights and references")

    #: Repeated here, not only in the docstring, because this field travels
    #: into payloads, logs and screenshots where the docstring does not.
    meaning: str = (
        "Strategy alignment on a 0-100 scale. NOT a probability of profit, a win rate, "
        "an expected return or a forecast. A high score means the current measurements "
        "match the strategy's published rules closely, nothing more."
    )

    @model_validator(mode="after")
    def _total_matches_components(self) -> SetupScore:
        """The headline must equal its parts.

        Guards the one failure that would make every explanation a lie: a
        score computed one way and itemised another.
        """
        total = sum((c.contribution for c in self.components), Decimal(0))
        if abs(total - self.value) > Decimal("0.01"):
            raise ValueError(f"score {self.value} does not match the sum of its components {total}")
        return self


class RiskReward(BaseModel):
    """Real levels derived from real price. Never a placeholder.

    All four prices are absolute, in the instrument's quote currency. The
    amounts are *per unit* of base quantity, so position sizing -- which is
    the risk engine's decision, not this module's -- multiplies through
    cleanly without this layer ever needing to know the account balance.
    """

    model_config = ConfigDict(frozen=True)

    entry_price: Decimal = Field(gt=0)
    stop_price: Decimal = Field(gt=0)
    take_profit_price: Decimal = Field(gt=0)

    risk_per_unit: Decimal = Field(gt=0, description="abs(entry - stop)")
    reward_per_unit: Decimal = Field(gt=0, description="abs(target - entry)")
    risk_reward_ratio: Decimal = Field(gt=0, description="reward / risk")

    stop_model: StopModel
    stop_distance_percent: Decimal = Field(gt=0)
    take_profit_r_multiple: Decimal = Field(gt=0)

    #: Stated on the model because it is the assumption most likely to be
    #: forgotten when these numbers are read downstream.
    entry_basis: str = (
        "Entry is the last closed candle's close. A live order fills at the NEXT bar's "
        "open at best, so the realised entry will differ. These levels are an estimate "
        "for ranking and sizing, not a quoted price."
    )


class TradeSetup(BaseModel):
    """One strategy's complete reading of one instrument at one moment."""

    model_config = ConfigDict(frozen=True)

    symbol: str
    timeframe: Timeframe
    strategy: str
    strategy_version: str

    status: SetupStatus
    direction: SetupDirection
    #: Present only when the status is ACTIONABLE. Absent is a real answer.
    score: SetupScore | None = None
    risk_reward: RiskReward | None = None

    regime: RegimeAssessment | None = None
    #: Every rule evaluated for the reported direction, satisfied or not.
    conditions: tuple[ConditionOutcome, ...] = ()
    #: The other side's rules, so a reader can see how close it came.
    opposing_conditions: tuple[ConditionOutcome, ...] = ()

    detail: str
    indicators_used: tuple[str, ...] = ()
    candles_used: int = Field(default=0, ge=0)
    last_candle_time: datetime | None = None

    #: Provenance. A setup without it cannot be audited after the fact.
    data_source: str | None = None
    data_status: str | None = None
    data_age_seconds: float | None = None
    evaluated_at: datetime | None = None

    disclaimer: str = (
        "Analysis only. A setup score measures agreement with a published rule set; it "
        "is not a probability of profit, a prediction, or financial advice. Every order "
        "remains subject to the risk engine, which has final authority and can refuse "
        "any setup regardless of its score."
    )

    @model_validator(mode="after")
    def _actionable_setups_carry_their_evidence(self) -> TradeSetup:
        """ACTIONABLE is a promise about what is present, so enforce it.

        Without this a consumer would have to null-check fields that the
        status already claimed were there, and the first one that forgot
        would read a missing stop as no stop.
        """
        if self.status is SetupStatus.ACTIONABLE:
            if self.direction is SetupDirection.NO_SIGNAL:
                raise ValueError("an ACTIONABLE setup cannot have direction NO_SIGNAL")
            if self.score is None or self.risk_reward is None:
                raise ValueError("an ACTIONABLE setup must carry both a score and risk/reward")
        return self
