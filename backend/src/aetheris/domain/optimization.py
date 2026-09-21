"""Parameter optimization and out-of-sample validation models.

## The one thing this module exists to prevent

Optimizing parameters on a stretch of history and then reporting how well they
did *on that same stretch* is not a result. It is a description of the search.
Every type here is shaped so that in-sample and out-of-sample numbers cannot be
reported as one figure: a trial carries its train score and its validation
score in separate fields, and the report carries a held-out test result that
the search never saw.

No object in this module claims a parameter set is optimal, robust or
profitable in future. The best a report says is which candidate scored highest
against a stated objective, over a stated window, under stated assumptions.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator

from aetheris.domain.backtest import BacktestConfig, BacktestMetrics
from aetheris.domain.enums import Timeframe


class ParameterType(StrEnum):
    INTEGER = "INTEGER"
    DECIMAL = "DECIMAL"


class ParameterSpec(BaseModel):
    """One tunable parameter and the bounded range the search may explore.

    Bounded is not optional. An unbounded search over a continuous parameter
    does not terminate, and on the hardware this project targets an overnight
    grid is a failure mode rather than a feature.
    """

    model_config = ConfigDict(frozen=True)

    name: str
    type: ParameterType
    minimum: Decimal
    maximum: Decimal
    step: Decimal = Field(gt=0)
    default: Decimal

    @model_validator(mode="after")
    def _range_is_coherent(self) -> ParameterSpec:
        if self.minimum > self.maximum:
            raise ValueError(f"{self.name}: minimum exceeds maximum")
        if not (self.minimum <= self.default <= self.maximum):
            raise ValueError(f"{self.name}: default {self.default} is outside its own range")
        if self.step > (self.maximum - self.minimum) and self.minimum != self.maximum:
            raise ValueError(f"{self.name}: step is larger than the whole range")
        return self

    def values(self) -> tuple[Decimal, ...]:
        """Every value the grid will try, inclusive of both ends."""
        out: list[Decimal] = []
        current = self.minimum
        while current <= self.maximum:
            out.append(current)
            current += self.step
        if out and out[-1] != self.maximum:
            out.append(self.maximum)
        return tuple(out)


class DataSplit(BaseModel):
    """A chronological partition of one candle series.

    Index ranges are half-open ``[start, end)`` into the original series, so a
    caller can reconstruct exactly which bars fed which stage. They never
    overlap and are always in order, which is what makes the split a real
    control rather than a label.
    """

    model_config = ConfigDict(frozen=True)

    train_start: int = Field(ge=0)
    train_end: int = Field(ge=0)
    validation_start: int = Field(ge=0)
    validation_end: int = Field(ge=0)
    test_start: int = Field(ge=0)
    test_end: int = Field(ge=0)
    total_bars: int = Field(ge=0)

    @model_validator(mode="after")
    def _windows_are_ordered_and_disjoint(self) -> DataSplit:
        """The invariant the whole module rests on.

        Enforced in the type rather than trusted to the splitter, because a
        single off-by-one that let the test window start one bar early would
        leak the answer into the search and nothing downstream would notice.
        """
        bounds = (
            self.train_start,
            self.train_end,
            self.validation_start,
            self.validation_end,
            self.test_start,
            self.test_end,
        )
        if list(bounds) != sorted(bounds):
            raise ValueError("split windows must be chronological and non-overlapping")
        if self.test_end > self.total_bars:
            raise ValueError("the test window runs past the end of the series")
        return self

    @property
    def train_bars(self) -> int:
        return self.train_end - self.train_start

    @property
    def validation_bars(self) -> int:
        return self.validation_end - self.validation_start

    @property
    def test_bars(self) -> int:
        return self.test_end - self.test_start


class WalkForwardWindow(BaseModel):
    """One train/validate pair in a rolling evaluation.

    Windows roll forward only. A validation window is always strictly after
    the training window that produced the parameters being validated.
    """

    model_config = ConfigDict(frozen=True)

    index: int = Field(ge=0)
    train_start: int = Field(ge=0)
    train_end: int = Field(ge=0)
    validation_start: int = Field(ge=0)
    validation_end: int = Field(ge=0)

    @model_validator(mode="after")
    def _validation_follows_training(self) -> WalkForwardWindow:
        if self.train_end > self.validation_start:
            raise ValueError("a validation window may not start before its training window ends")
        if self.train_start >= self.train_end or self.validation_start >= self.validation_end:
            raise ValueError("both windows must be non-empty")
        return self


class TrialScore(BaseModel):
    """The objective's verdict on one candidate, with its inputs shown."""

    model_config = ConfigDict(frozen=True)

    value: Decimal
    components: dict[str, Decimal] = Field(default_factory=dict)
    detail: str
    #: Set when the candidate was rejected outright by a gate rather than
    #: scored on its merits -- too few trades, for example.
    rejected_reason: str | None = None


class OptimizationTrial(BaseModel):
    """One parameter set, evaluated in-sample and then out-of-sample.

    Both scores are always present as separate fields. There is deliberately
    no combined figure: any single number would let the in-sample result
    flatter the out-of-sample one.
    """

    model_config = ConfigDict(frozen=True)

    parameters: dict[str, Decimal]
    train_score: TrialScore
    train_metrics: BacktestMetrics | None = None
    validation_score: TrialScore | None = None
    validation_metrics: BacktestMetrics | None = None


class OptimizationReport(BaseModel):
    """The complete, reproducible record of one search."""

    model_config = ConfigDict(frozen=True)

    symbol: str
    timeframe: Timeframe
    strategy: str
    strategy_version: str

    objective: str = Field(description="Versioned identifier for the objective function")
    space: tuple[ParameterSpec, ...]
    combinations_evaluated: int = Field(ge=0)
    combinations_possible: int = Field(ge=0)

    split: DataSplit
    trials: tuple[OptimizationTrial, ...] = ()
    #: Highest validation score, not highest train score. Selecting on the
    #: window the search optimized would make the whole split decorative.
    best: OptimizationTrial | None = None
    #: The held-out result. Computed once, after selection, and never used to
    #: choose anything.
    test_metrics: BacktestMetrics | None = None
    test_score: TrialScore | None = None

    #: Everything needed to reproduce the run bit for bit.
    config: BacktestConfig
    first_bar_time: datetime | None = None
    last_bar_time: datetime | None = None
    ran_at: datetime | None = None

    warnings: tuple[str, ...] = ()
    disclaimer: str = (
        "An optimization report describes how parameter sets scored over specific "
        "historical windows under stated assumptions. It is not evidence that these "
        "parameters are optimal, robust, or profitable in future. Out-of-sample "
        "results are reported separately from in-sample results precisely because "
        "the two must not be read as one number."
    )


class WalkForwardFold(BaseModel):
    """One window's worth of walk-forward evidence.

    ``selected`` was chosen using the training window **only**. The validation
    score is then measured on bars the selection never saw. Keeping both on
    the same object, in separate fields, is what makes the comparison
    checkable: a fold whose train score is excellent and whose validation
    score is not is the single most informative row in the report.
    """

    model_config = ConfigDict(frozen=True)

    window: WalkForwardWindow
    selected: dict[str, Decimal]
    train_score: TrialScore
    validation_score: TrialScore
    validation_metrics: BacktestMetrics | None = None
    #: How many candidates were actually evaluated in this fold. Invalid
    #: parameter combinations are skipped, so this can be below the grid size.
    candidates_evaluated: int = Field(default=0, ge=0)


class WalkForwardReport(BaseModel):
    """Rolling out-of-sample evaluation across the whole series.

    ## What this measures, and what it does not

    Each fold selects on its own training window and is then scored on the
    validation window immediately after it. No fold can see its own
    validation bars while choosing, and no fold can see any later fold at
    all. The aggregate is therefore an out-of-sample estimate rather than a
    description of the search.

    It is still not a prediction. It says how a *procedure* -- this grid,
    this objective, this rebalancing cadence -- would have behaved over this
    history. A procedure that held up across folds is better evidence than
    one that did not; neither is a forecast, and a report with two folds is
    barely evidence at all, which is why ``folds`` is reported rather than
    averaged away.
    """

    model_config = ConfigDict(frozen=True)

    symbol: str
    timeframe: Timeframe
    strategy: str
    strategy_version: str
    objective: str
    space: tuple[ParameterSpec, ...]

    train_bars: int = Field(ge=1)
    validation_bars: int = Field(ge=1)
    step_bars: int = Field(ge=1)
    folds: tuple[WalkForwardFold, ...] = ()

    #: Mean validation score across folds. ``None`` when there were no folds:
    #: an empty mean is not zero.
    mean_validation_score: Decimal | None = None
    #: Mean train score, reported beside it so the gap between them is
    #: visible rather than something a reader has to compute.
    mean_train_score: Decimal | None = None
    #: How often the most frequently selected parameter set won a fold. A
    #: procedure that picks a different winner every window is describing
    #: noise, and this is the cheapest way to see that.
    most_selected: dict[str, Decimal] | None = None
    most_selected_folds: int = Field(default=0, ge=0)

    config: BacktestConfig
    ran_at: datetime | None = None
    warnings: tuple[str, ...] = ()

    disclaimer: str = (
        "A walk-forward report describes how a selection procedure would have "
        "behaved over specific historical windows under stated assumptions. It is "
        "not evidence that the procedure is profitable in future, and the mean "
        "validation score is an average over a small number of folds, not an "
        "expected return."
    )

    @property
    def fold_count(self) -> int:
        return len(self.folds)
