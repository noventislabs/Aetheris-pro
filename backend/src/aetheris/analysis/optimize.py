"""Bounded parameter search with mandatory out-of-sample evaluation.

Pure arithmetic over candles. No HTTP, no venue, no settings, no persistence,
no parallelism -- it runs the existing backtester repeatedly and reports what
happened.

## Why the split is not optional

A grid search will always find a parameter set that did well on the data it
searched. That is what searching means, and it says nothing about any other
data. So this module does not offer an "optimize" that reports one number:

```
train window       -> every candidate is scored here
validation window  -> every candidate is scored here too; the BEST is chosen on THIS
test window        -> the winner is scored here ONCE, after selection, and never
                      influences it
```

Selection uses validation rather than train precisely so the reported winner
is not simply the candidate that memorised the training bars best.

**Each window is sliced strictly.** A backtest over bars ``[start, end)`` sees
nothing before ``start``, which costs the strategy's warm-up bars inside every
window. Paying that cost is the point: feeding earlier bars in to "help the
indicators" would hand the validation window information from outside itself.

## The objective, and why not raw profit

Optimizing for return alone reliably selects a parameter set that caught two
enormous moves and did nothing useful otherwise. The objective is a weighted
sum of four components, each normalized to 0-1, behind a hard gate:

``return`` -- **0.35.** Full marks at ``RETURN_REFERENCE`` = 30% over the
window; a negative return scores zero.

``profit_factor`` -- **0.25.** Full marks at ``PROFIT_FACTOR_REFERENCE`` = 2.0.
A profit factor at or below 1.0 scores zero.

``drawdown`` -- **0.25.** Full marks at zero drawdown, falling to zero at
``DRAWDOWN_REFERENCE`` = 25%.

``consistency`` -- **0.15.** Full marks with no losing streak, falling to zero
at ``STREAK_REFERENCE`` = 10 consecutive losses.

**The gate:** a candidate with fewer than ``MIN_TRADES_FOR_OBJECTIVE`` trades
is rejected outright and scores zero, whatever its return. This is the
specific defence against a spectacular two-trade result winning the search.

Drawdown and streak are weighted to 0.40 combined -- deliberately more than
return -- because the failure this search must avoid is not "found a mediocre
strategy", it is "found a fragile one and called it good".

These five constants are judgement calls. They are stated here and versioned
in ``OBJECTIVE_METHOD`` so a stored report is never compared against one
scored by different arithmetic.

## Bounds

``MAX_COMBINATIONS`` caps the grid. A search that would exceed it is refused
rather than truncated, because silently testing the first N of a grid makes
the report a description of an arbitrary prefix. The target hardware is a
dual-core i3 with 8 GB; every combination costs two full backtests.
"""

from __future__ import annotations

import itertools
from collections.abc import Sequence
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Final

from aetheris.analysis.backtest.engine import run_backtest
from aetheris.analysis.strategies import trend_momentum
from aetheris.analysis.strategies.trend_momentum import TrendMomentumParams
from aetheris.domain.backtest import BacktestConfig, BacktestMetrics, BacktestStatus
from aetheris.domain.enums import Timeframe
from aetheris.domain.market import Candle
from aetheris.domain.optimization import (
    DataSplit,
    OptimizationReport,
    OptimizationTrial,
    ParameterSpec,
    ParameterType,
    TrialScore,
    WalkForwardFold,
    WalkForwardReport,
    WalkForwardWindow,
)

__all__ = [
    "DRAWDOWN_REFERENCE",
    "MAX_COMBINATIONS",
    "MAX_WALK_FORWARD_FOLDS",
    "MIN_TRADES_FOR_OBJECTIVE",
    "OBJECTIVE_METHOD",
    "OBJECTIVE_WEIGHTS",
    "PROFIT_FACTOR_REFERENCE",
    "RETURN_REFERENCE",
    "STREAK_REFERENCE",
    "default_parameter_space",
    "optimize",
    "score_objective",
    "split_chronological",
    "walk_forward",
    "walk_forward_windows",
]

#: Bump when any weight, reference or gate below changes.
OBJECTIVE_METHOD: Final = "optimization-objective/v1"

OBJECTIVE_WEIGHTS: Final[dict[str, Decimal]] = {
    "return": Decimal("0.35"),
    "profit_factor": Decimal("0.25"),
    "drawdown": Decimal("0.25"),
    "consistency": Decimal("0.15"),
}

RETURN_REFERENCE: Final = Decimal(30)
PROFIT_FACTOR_REFERENCE: Final = Decimal(2)
DRAWDOWN_REFERENCE: Final = Decimal(25)
STREAK_REFERENCE: Final = Decimal(10)

#: Below this a result describes coincidences, not a strategy.
MIN_TRADES_FOR_OBJECTIVE: Final = 20

#: Hard ceiling on grid size. Each combination costs two backtests.
MAX_COMBINATIONS: Final = 512

#: Hard ceiling on folds. A walk-forward run costs
#: ``folds x combinations`` training backtests plus one validation backtest
#: per fold, so the two bounds multiply. Refused rather than truncated, for
#: the same reason an oversized grid is.
MAX_WALK_FORWARD_FOLDS: Final = 24

_ZERO: Final = Decimal(0)
_ONE: Final = Decimal(1)
_RATIO: Final = Decimal("0.0001")


def _clamp_unit(value: Decimal) -> Decimal:
    if value < _ZERO:
        return _ZERO
    return _ONE if value > _ONE else value


def _q(value: Decimal) -> Decimal:
    return value.quantize(_RATIO, rounding=ROUND_HALF_UP)


# ----------------------------------------------------------------------
# Splitting
# ----------------------------------------------------------------------


def split_chronological(
    total_bars: int,
    *,
    train_fraction: Decimal = Decimal("0.6"),
    validation_fraction: Decimal = Decimal("0.2"),
) -> DataSplit:
    """Partition a series into train, validation and held-out test windows.

    Chronological and contiguous: the test window is always the most recent
    bars, because that is the only arrangement in which "out of sample" also
    means "out of time". A random split of time-series bars leaks trivially --
    a bar's neighbours are nearly itself.

    The remainder after the two fractions becomes the test window, so the
    three always tile the series exactly with no bars silently dropped.
    """
    if total_bars <= 0:
        raise ValueError("cannot split an empty series")
    if train_fraction <= 0 or validation_fraction <= 0:
        raise ValueError("train and validation fractions must be positive")
    if train_fraction + validation_fraction >= 1:
        raise ValueError("fractions must leave a non-empty test window")

    train_end = int(Decimal(total_bars) * train_fraction)
    validation_end = train_end + int(Decimal(total_bars) * validation_fraction)
    if train_end <= 0 or validation_end <= train_end or validation_end >= total_bars:
        raise ValueError(
            f"{total_bars} bars cannot be split into three non-empty windows at these fractions"
        )

    return DataSplit(
        train_start=0,
        train_end=train_end,
        validation_start=train_end,
        validation_end=validation_end,
        test_start=validation_end,
        test_end=total_bars,
        total_bars=total_bars,
    )


def walk_forward_windows(
    total_bars: int, *, train_bars: int, validation_bars: int, step_bars: int | None = None
) -> tuple[WalkForwardWindow, ...]:
    """Rolling train/validate pairs that only ever move forward.

    ``step_bars`` defaults to ``validation_bars``, which tiles the series with
    non-overlapping validation windows -- every bar is validated at most once,
    so no bar's outcome is counted twice.

    Returns an empty tuple when the series is too short for even one window.
    That is a real answer: it means walk-forward cannot be performed here, and
    the caller must say so rather than fall back to a single split.
    """
    if train_bars <= 0 or validation_bars <= 0:
        raise ValueError("both window sizes must be positive")
    step = step_bars if step_bars is not None else validation_bars
    if step <= 0:
        raise ValueError("step_bars must be positive")

    windows: list[WalkForwardWindow] = []
    start = 0
    index = 0
    while start + train_bars + validation_bars <= total_bars:
        train_end = start + train_bars
        windows.append(
            WalkForwardWindow(
                index=index,
                train_start=start,
                train_end=train_end,
                validation_start=train_end,
                validation_end=train_end + validation_bars,
            )
        )
        start += step
        index += 1
    return tuple(windows)


# ----------------------------------------------------------------------
# Objective
# ----------------------------------------------------------------------


def score_objective(metrics: BacktestMetrics | None) -> TrialScore:
    """Score one backtest against the documented multi-factor objective.

    A rejected candidate scores zero and says why. Rejection is not an error
    condition -- most of a grid deserves it.
    """
    if metrics is None:
        return TrialScore(
            value=_ZERO,
            detail="The backtest produced no metrics, so there is nothing to score.",
            rejected_reason="NO_METRICS",
        )
    if metrics.total_trades < MIN_TRADES_FOR_OBJECTIVE:
        return TrialScore(
            value=_ZERO,
            components={"total_trades": Decimal(metrics.total_trades)},
            detail=(
                f"{metrics.total_trades} trades is below the {MIN_TRADES_FOR_OBJECTIVE} "
                f"required for this objective to mean anything. Rejected regardless of "
                f"return: a large result from a handful of trades is the outcome this "
                f"gate exists to exclude."
            ),
            rejected_reason="TOO_FEW_TRADES",
        )

    return_norm = _clamp_unit(metrics.return_percent / RETURN_REFERENCE)
    if metrics.profit_factor is None:
        # No losing trades over a qualifying sample. Undefined, not infinite:
        # scored at full marks on this axis only because nothing was lost,
        # and the other three components still constrain the total.
        pf_norm = _ONE
    else:
        pf_norm = _clamp_unit((metrics.profit_factor - _ONE) / (PROFIT_FACTOR_REFERENCE - _ONE))
    dd_norm = _clamp_unit(_ONE - metrics.max_drawdown_percent / DRAWDOWN_REFERENCE)
    streak_norm = _clamp_unit(_ONE - Decimal(metrics.max_consecutive_losses) / STREAK_REFERENCE)

    components = {
        "return": _q(return_norm * OBJECTIVE_WEIGHTS["return"]),
        "profit_factor": _q(pf_norm * OBJECTIVE_WEIGHTS["profit_factor"]),
        "drawdown": _q(dd_norm * OBJECTIVE_WEIGHTS["drawdown"]),
        "consistency": _q(streak_norm * OBJECTIVE_WEIGHTS["consistency"]),
    }
    total = sum(components.values(), _ZERO)

    return TrialScore(
        value=_q(total),
        components=components,
        detail=(
            f"{metrics.total_trades} trades, {metrics.return_percent}% return, "
            f"profit factor {metrics.profit_factor}, max drawdown "
            f"{metrics.max_drawdown_percent}%, longest losing streak "
            f"{metrics.max_consecutive_losses}."
        ),
    )


# ----------------------------------------------------------------------
# Search
# ----------------------------------------------------------------------


def default_parameter_space() -> tuple[ParameterSpec, ...]:
    """A deliberately small grid over parameters the strategy actually has.

    Every name here is a real field on ``TrendMomentumParams``; exposing a
    parameter the strategy does not read would produce a report about a knob
    connected to nothing. Ranges are narrow so the default search finishes on
    modest hardware -- widen them explicitly, having decided to wait.
    """
    return (
        ParameterSpec(
            name="ema_fast",
            type=ParameterType.INTEGER,
            minimum=Decimal(9),
            maximum=Decimal(21),
            step=Decimal(6),
            default=Decimal(21),
        ),
        ParameterSpec(
            name="ema_slow",
            type=ParameterType.INTEGER,
            minimum=Decimal(50),
            maximum=Decimal(70),
            step=Decimal(10),
            default=Decimal(55),
        ),
        ParameterSpec(
            name="adx_minimum",
            type=ParameterType.DECIMAL,
            minimum=Decimal(15),
            maximum=Decimal(25),
            step=Decimal(5),
            default=Decimal(20),
        ),
    )


def _build_params(values: dict[str, Decimal]) -> TrendMomentumParams | None:
    """Construct strategy params, or ``None`` if the combination is invalid.

    ``ema_fast >= ema_slow`` inverts every rule and is rejected by the
    strategy's own validator. A grid inevitably generates such pairs; they
    are skipped, not repaired, because repairing one would silently evaluate
    a different candidate from the one the grid asked for.
    """
    kwargs: dict[str, object] = {}
    for name, value in values.items():
        if name in ("ema_fast", "ema_slow", "rsi_period", "adx_period"):
            kwargs[name] = int(value)
        else:
            kwargs[name] = value
    try:
        return TrendMomentumParams(**kwargs)  # type: ignore[arg-type]
    except ValueError:
        return None


def _evaluate(
    candles: Sequence[Candle],
    *,
    symbol: str,
    timeframe: Timeframe,
    config: BacktestConfig,
    params: TrendMomentumParams,
) -> BacktestMetrics | None:
    result = run_backtest(candles, symbol=symbol, timeframe=timeframe, config=config, params=params)
    if result.status is not BacktestStatus.COMPLETED:
        return None
    return result.metrics


def optimize(
    candles: Sequence[Candle],
    *,
    symbol: str,
    timeframe: Timeframe,
    config: BacktestConfig | None = None,
    space: Sequence[ParameterSpec] | None = None,
    split: DataSplit | None = None,
    max_combinations: int = MAX_COMBINATIONS,
    ran_at: datetime | None = None,
) -> OptimizationReport:
    """Run a bounded grid search and report in-sample and out-of-sample results.

    Deterministic: the same candles, space and config produce the same report,
    because the grid is generated in sorted parameter order and nothing here
    samples randomly.

    Refuses rather than truncates when the grid exceeds ``max_combinations``.
    """
    settings = config or BacktestConfig()
    specs = tuple(space) if space is not None else default_parameter_space()
    if not specs:
        raise ValueError("an empty parameter space has nothing to search")

    ordered = sorted(specs, key=lambda spec: spec.name)
    grids = [spec.values() for spec in ordered]
    possible = 1
    for grid in grids:
        possible *= len(grid)
    if possible > max_combinations:
        raise ValueError(
            f"this space would evaluate {possible} combinations, above the "
            f"{max_combinations} ceiling. Narrow a range or raise the ceiling "
            f"deliberately -- each combination costs two backtests."
        )

    partition = split or split_chronological(len(candles))
    if partition.total_bars != len(candles):
        raise ValueError("the split describes a different number of bars than were supplied")

    train = candles[partition.train_start : partition.train_end]
    validation = candles[partition.validation_start : partition.validation_end]
    test = candles[partition.test_start : partition.test_end]

    warnings: list[str] = []
    warmup = trend_momentum.warmup_bars(TrendMomentumParams())
    for name, window in (("train", train), ("validation", validation), ("test", test)):
        if len(window) <= warmup:
            warnings.append(
                f"The {name} window holds {len(window)} bars, at or below the "
                f"{warmup}-bar warm-up, so it can produce no trades. Each window is "
                f"sliced strictly to prevent leakage, and pays its own warm-up."
            )

    trials: list[OptimizationTrial] = []
    evaluated = 0
    for combination in itertools.product(*grids):
        values = {spec.name: value for spec, value in zip(ordered, combination, strict=True)}
        params = _build_params(values)
        if params is None:
            continue
        evaluated += 1

        train_metrics = _evaluate(
            train, symbol=symbol, timeframe=timeframe, config=settings, params=params
        )
        train_score = score_objective(train_metrics)

        validation_metrics = _evaluate(
            validation, symbol=symbol, timeframe=timeframe, config=settings, params=params
        )
        validation_score = score_objective(validation_metrics)

        trials.append(
            OptimizationTrial(
                parameters=values,
                train_score=train_score,
                train_metrics=train_metrics,
                validation_score=validation_score,
                validation_metrics=validation_metrics,
            )
        )

    # Selection is on validation, never on train. Ties break on the sorted
    # parameter ordering already established, so the winner is reproducible.
    scored = [t for t in trials if t.validation_score is not None]
    best: OptimizationTrial | None = None
    for trial in scored:
        assert trial.validation_score is not None
        if best is None or trial.validation_score.value > best.validation_score.value:  # type: ignore[union-attr]
            best = trial

    if (
        best is not None
        and best.validation_score is not None
        and best.validation_score.rejected_reason is not None
    ):
        warnings.append(
            "Every candidate was rejected by the objective's gate, so the reported "
            "best is merely the first of equals at zero. This is not a selection."
        )

    test_metrics: BacktestMetrics | None = None
    test_score: TrialScore | None = None
    if best is not None:
        winner = _build_params(best.parameters)
        if winner is not None:
            test_metrics = _evaluate(
                test, symbol=symbol, timeframe=timeframe, config=settings, params=winner
            )
            test_score = score_objective(test_metrics)

    return OptimizationReport(
        symbol=symbol.upper(),
        timeframe=timeframe,
        strategy=trend_momentum.STRATEGY_KEY,
        strategy_version=trend_momentum.STRATEGY_VERSION,
        objective=OBJECTIVE_METHOD,
        space=tuple(ordered),
        combinations_evaluated=evaluated,
        combinations_possible=possible,
        split=partition,
        trials=tuple(trials),
        best=best,
        test_metrics=test_metrics,
        test_score=test_score,
        config=settings,
        first_bar_time=candles[0].open_time if candles else None,
        last_bar_time=candles[-1].close_time if candles else None,
        ran_at=ran_at,
        warnings=tuple(warnings),
    )


def _select_on(
    candles: Sequence[Candle],
    *,
    symbol: str,
    timeframe: Timeframe,
    config: BacktestConfig,
    grids: list[tuple[Decimal, ...]],
    ordered: list[ParameterSpec],
) -> tuple[dict[str, Decimal], TrialScore, int] | None:
    """Score every valid candidate over one window and return the best.

    Sees nothing but the bars it is handed. That is the whole mechanism: a
    fold cannot select on data it was not given, so passing it only the
    training slice is what makes the later validation out-of-sample.

    Returns ``None`` when no candidate in the grid was even constructible.
    """
    best: tuple[dict[str, Decimal], TrialScore, int] | None = None
    evaluated = 0
    for combination in itertools.product(*grids):
        values = {spec.name: value for spec, value in zip(ordered, combination, strict=True)}
        params = _build_params(values)
        if params is None:
            continue
        evaluated += 1
        metrics = _evaluate(
            candles, symbol=symbol, timeframe=timeframe, config=config, params=params
        )
        score = score_objective(metrics)
        if best is None or score.value > best[1].value:
            best = (values, score, evaluated)
    if best is None:
        return None
    return (best[0], best[1], evaluated)


def walk_forward(
    candles: Sequence[Candle],
    *,
    symbol: str,
    timeframe: Timeframe,
    train_bars: int,
    validation_bars: int,
    step_bars: int | None = None,
    config: BacktestConfig | None = None,
    space: Sequence[ParameterSpec] | None = None,
    max_combinations: int = MAX_COMBINATIONS,
    max_folds: int = MAX_WALK_FORWARD_FOLDS,
    ran_at: datetime | None = None,
) -> WalkForwardReport:
    """Roll a selection procedure forward and score each choice out of sample.

    This is what ``walk_forward_windows`` was built for. Generating the
    windows and not driving anything with them left the honest-evaluation
    story half-finished: a single three-way split answers "did these
    parameters hold up once", and that is a much weaker question than "does
    this *procedure* keep working as the market moves".

    Per fold:

    1. every candidate is scored on the training window, and the best is
       selected using **that window only**;
    2. the selection is then scored on the validation window, which the
       selection could not see.

    No fold sees its own validation bars while choosing, and no fold sees any
    later fold at all. Windows are sliced strictly, so each pays its own
    warm-up rather than borrowing bars from before its start -- borrowing
    would hand a window information from outside itself, which is the exact
    leak this is built to prevent.

    Deterministic: same candles, same space, same config, same report.
    """
    settings = config or BacktestConfig()
    specs = tuple(space) if space is not None else default_parameter_space()
    if not specs:
        raise ValueError("an empty parameter space has nothing to search")

    ordered = sorted(specs, key=lambda spec: spec.name)
    grids = [spec.values() for spec in ordered]
    possible = 1
    for grid in grids:
        possible *= len(grid)
    if possible > max_combinations:
        raise ValueError(
            f"this space would evaluate {possible} combinations per fold, above the "
            f"{max_combinations} ceiling. Narrow a range or raise the ceiling "
            f"deliberately -- a walk-forward run pays this cost once per fold."
        )

    windows = walk_forward_windows(
        len(candles),
        train_bars=train_bars,
        validation_bars=validation_bars,
        step_bars=step_bars,
    )
    if len(windows) > max_folds:
        raise ValueError(
            f"this configuration produces {len(windows)} folds, above the {max_folds} "
            f"ceiling. Each fold costs a full grid, so the two bounds multiply."
        )

    warnings: list[str] = []
    if not windows:
        warnings.append(
            f"{len(candles)} bars cannot hold even one {train_bars}+{validation_bars} "
            f"window, so no walk-forward evaluation was performed. This is not a "
            f"result of zero; it is the absence of one."
        )

    warmup = trend_momentum.warmup_bars(TrendMomentumParams())
    if windows and validation_bars <= warmup:
        warnings.append(
            f"Each validation window holds {validation_bars} bars, at or below the "
            f"{warmup}-bar warm-up, so no fold can produce a trade. Windows are "
            f"sliced strictly to prevent leakage and each pays its own warm-up."
        )

    folds: list[WalkForwardFold] = []
    for window in windows:
        train = candles[window.train_start : window.train_end]
        validation = candles[window.validation_start : window.validation_end]

        selection = _select_on(
            train,
            symbol=symbol,
            timeframe=timeframe,
            config=settings,
            grids=grids,
            ordered=ordered,
        )
        if selection is None:
            continue
        values, train_score, evaluated = selection

        params = _build_params(values)
        if params is None:  # pragma: no cover - selection returned it, so it builds
            continue
        validation_metrics = _evaluate(
            validation, symbol=symbol, timeframe=timeframe, config=settings, params=params
        )
        folds.append(
            WalkForwardFold(
                window=window,
                selected=values,
                train_score=train_score,
                validation_score=score_objective(validation_metrics),
                validation_metrics=validation_metrics,
                candidates_evaluated=evaluated,
            )
        )

    mean_validation: Decimal | None = None
    mean_train: Decimal | None = None
    if folds:
        mean_validation = _q(
            sum((f.validation_score.value for f in folds), _ZERO) / Decimal(len(folds))
        )
        mean_train = _q(sum((f.train_score.value for f in folds), _ZERO) / Decimal(len(folds)))

    # How often one parameter set won. A procedure that picks a different
    # winner every window is describing noise, and a reader should be able to
    # see that without recomputing it.
    most_selected: dict[str, Decimal] | None = None
    most_selected_folds = 0
    if folds:
        tally: dict[tuple[tuple[str, Decimal], ...], int] = {}
        for fold in folds:
            key = tuple(sorted(fold.selected.items()))
            tally[key] = tally.get(key, 0) + 1
        winner = max(sorted(tally), key=lambda k: tally[k])
        most_selected = dict(winner)
        most_selected_folds = tally[winner]

        # A "winner" that every fold agreed on looks like convergence, and
        # when every candidate was rejected it is the exact opposite: the
        # tie-break picked first-in-order out of a field of zeros. Reporting
        # the agreement without this would be the most misleading line in the
        # report, because it reads strongest precisely when it means least.
        if all(fold.train_score.rejected_reason is not None for fold in folds):
            warnings.append(
                "Every candidate was rejected by the objective's gate in every fold, so "
                "the selections are ties at zero broken by parameter order, not choices. "
                "Any agreement between folds here is an artefact of that ordering and is "
                "not evidence of a stable parameter set."
            )
        elif most_selected_folds == 1 and len(folds) > 1:
            warnings.append(
                "No parameter set won more than one fold. The selection procedure is "
                "not converging on anything stable over this history, and the mean "
                "validation score should be read with that in mind."
            )

    return WalkForwardReport(
        symbol=symbol.upper(),
        timeframe=timeframe,
        strategy=trend_momentum.STRATEGY_KEY,
        strategy_version=trend_momentum.STRATEGY_VERSION,
        objective=OBJECTIVE_METHOD,
        space=tuple(ordered),
        train_bars=train_bars,
        validation_bars=validation_bars,
        step_bars=step_bars if step_bars is not None else validation_bars,
        folds=tuple(folds),
        mean_validation_score=mean_validation,
        mean_train_score=mean_train,
        most_selected=most_selected,
        most_selected_folds=most_selected_folds,
        config=settings,
        ran_at=ran_at,
        warnings=tuple(warnings),
    )
