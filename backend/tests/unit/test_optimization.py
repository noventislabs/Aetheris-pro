"""Parameter search, and the separation that makes its output mean anything.

The tests that matter most here are not about finding good parameters. They
are about the search being unable to cheat: windows that cannot overlap, a
test window that is scored after selection rather than during it, a gate that
rejects a spectacular three-trade result, and a grid that refuses to run
rather than quietly truncating.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from itertools import pairwise

import pytest
from pydantic import ValidationError

from aetheris.analysis.optimize import (
    MIN_TRADES_FOR_OBJECTIVE,
    OBJECTIVE_METHOD,
    OBJECTIVE_WEIGHTS,
    default_parameter_space,
    optimize,
    score_objective,
    split_chronological,
    walk_forward_windows,
)
from aetheris.domain.backtest import BacktestConfig, BacktestMetrics
from aetheris.domain.enums import Timeframe
from aetheris.domain.market import Candle
from aetheris.domain.optimization import DataSplit, ParameterSpec, ParameterType

BASE = datetime(2026, 9, 20, tzinfo=UTC)


def bar(index: int, price: Decimal) -> Candle:
    start = BASE + timedelta(hours=index)
    return Candle(
        open_time=start,
        close_time=start + timedelta(hours=1, milliseconds=-1),
        open=price,
        high=price + Decimal("1.5"),
        low=price - Decimal("1.5"),
        close=price,
        volume=Decimal(100),
    )


def wavy(count: int) -> list[Candle]:
    """Enough movement in both directions for a search to find trades."""
    out: list[Candle] = []
    for i in range(count):
        wave = Decimal(str(round(18 * math.sin(2 * math.pi * i / 40), 4)))
        drift = Decimal(str(round(6 * math.sin(2 * math.pi * i / 260), 4)))
        out.append(bar(i, Decimal(300) + wave + drift))
    return out


def metrics(
    *,
    trades: int = 40,
    return_percent: str = "15",
    profit_factor: str | None = "1.5",
    drawdown: str = "10",
    streak: int = 3,
) -> BacktestMetrics:
    return BacktestMetrics(
        total_trades=trades,
        winning_trades=trades // 2,
        losing_trades=trades - trades // 2,
        breakeven_trades=0,
        net_pnl=Decimal(15),
        gross_profit=Decimal(45),
        gross_loss=Decimal(30),
        total_fees=Decimal(1),
        return_percent=Decimal(return_percent),
        profit_factor=Decimal(profit_factor) if profit_factor is not None else None,
        max_drawdown_percent=Decimal(drawdown),
        max_drawdown_absolute=Decimal(10),
        max_consecutive_losses=streak,
        max_consecutive_wins=2,
        exposure_percent=Decimal(50),
        starting_balance=Decimal(100),
        ending_balance=Decimal(115),
        bars_tested=500,
        trades_open_at_end=0,
    )


# ----------------------------------------------------------------------
# Splitting
# ----------------------------------------------------------------------


def test_a_split_tiles_the_series_exactly() -> None:
    """No bar is dropped and none is counted twice."""
    split = split_chronological(1000)
    assert split.train_bars + split.validation_bars + split.test_bars == 1000
    assert split.train_start == 0
    assert split.test_end == 1000


def test_windows_are_chronological_and_the_test_window_is_last() -> None:
    """Out of sample must also mean out of time."""
    split = split_chronological(1000)
    assert split.train_end == split.validation_start
    assert split.validation_end == split.test_start
    assert split.test_start > split.validation_start > split.train_start


def test_the_split_type_refuses_overlapping_windows() -> None:
    """The invariant lives in the type, not in the splitter's good intentions.

    A test window starting one bar early would leak the answer into the
    search, and nothing downstream would be able to tell.
    """
    with pytest.raises(ValidationError, match="chronological and non-overlapping"):
        DataSplit(
            train_start=0,
            train_end=600,
            validation_start=500,  # overlaps training
            validation_end=800,
            test_start=800,
            test_end=1000,
            total_bars=1000,
        )


def test_a_split_may_not_run_past_the_series() -> None:
    with pytest.raises(ValidationError, match="past the end"):
        DataSplit(
            train_start=0,
            train_end=600,
            validation_start=600,
            validation_end=800,
            test_start=800,
            test_end=1200,
            total_bars=1000,
        )


def test_fractions_that_leave_no_test_window_are_refused() -> None:
    with pytest.raises(ValueError, match="non-empty test window"):
        split_chronological(1000, train_fraction=Decimal("0.8"), validation_fraction=Decimal("0.2"))


def test_an_empty_series_cannot_be_split() -> None:
    with pytest.raises(ValueError, match="empty series"):
        split_chronological(0)


# ----------------------------------------------------------------------
# Walk-forward
# ----------------------------------------------------------------------


def test_walk_forward_windows_only_move_forward() -> None:
    windows = walk_forward_windows(1000, train_bars=300, validation_bars=100)
    assert windows
    for window in windows:
        assert window.train_end <= window.validation_start
    for earlier, later in pairwise(windows):
        assert later.train_start > earlier.train_start
        assert later.validation_start > earlier.validation_start


def test_default_stepping_gives_non_overlapping_validation_windows() -> None:
    """Every bar is validated at most once, so no outcome is double-counted."""
    windows = walk_forward_windows(1000, train_bars=300, validation_bars=100)
    for earlier, later in pairwise(windows):
        assert earlier.validation_end <= later.validation_start


def test_a_series_too_short_for_one_window_yields_none() -> None:
    """An empty tuple is a real answer: walk-forward cannot be done here."""
    assert walk_forward_windows(100, train_bars=300, validation_bars=100) == ()


def test_walk_forward_rejects_non_positive_windows() -> None:
    with pytest.raises(ValueError, match="must be positive"):
        walk_forward_windows(1000, train_bars=0, validation_bars=100)


# ----------------------------------------------------------------------
# Objective
# ----------------------------------------------------------------------


def test_the_objective_weights_sum_to_one() -> None:
    assert sum(OBJECTIVE_WEIGHTS.values()) == Decimal(1)


def test_a_spectacular_result_from_too_few_trades_is_rejected() -> None:
    """The specific failure this objective exists to prevent.

    A 900% return from three trades must score zero, not win the search.
    """
    lucky = metrics(trades=3, return_percent="900", profit_factor="50", drawdown="1", streak=0)
    score = score_objective(lucky)
    assert score.value == Decimal(0)
    assert score.rejected_reason == "TOO_FEW_TRADES"
    assert "900" not in score.detail or "below the" in score.detail


def test_a_qualifying_result_scores_on_its_merits() -> None:
    score = score_objective(metrics())
    assert score.rejected_reason is None
    assert Decimal(0) < score.value <= Decimal(1)
    assert set(score.components) == set(OBJECTIVE_WEIGHTS)
    assert sum(score.components.values()) == score.value


def test_drawdown_and_streak_penalise_a_fragile_result() -> None:
    """Two runs with identical returns must not score identically."""
    calm = score_objective(metrics(drawdown="2", streak=1))
    fragile = score_objective(metrics(drawdown="24", streak=9))
    assert calm.value > fragile.value


def test_a_losing_run_scores_zero_on_return_but_is_still_scored() -> None:
    losing = score_objective(metrics(return_percent="-20", profit_factor="0.5"))
    assert losing.components["return"] == Decimal(0)
    assert losing.components["profit_factor"] == Decimal(0)
    assert losing.rejected_reason is None


def test_absent_metrics_score_zero_rather_than_raising() -> None:
    score = score_objective(None)
    assert score.value == Decimal(0)
    assert score.rejected_reason == "NO_METRICS"


def test_the_objective_is_deterministic() -> None:
    sample = metrics()
    assert score_objective(sample).value == score_objective(sample).value


# ----------------------------------------------------------------------
# Parameter space
# ----------------------------------------------------------------------


def test_every_default_parameter_is_a_real_strategy_field() -> None:
    """A knob connected to nothing would make the whole report meaningless."""
    from aetheris.analysis.strategies.trend_momentum import TrendMomentumParams

    fields = set(TrendMomentumParams.model_fields)
    for spec in default_parameter_space():
        assert spec.name in fields, f"{spec.name} is not a TrendMomentumParams field"


def test_a_spec_enumerates_inclusive_bounded_values() -> None:
    spec = ParameterSpec(
        name="adx_minimum",
        type=ParameterType.DECIMAL,
        minimum=Decimal(15),
        maximum=Decimal(25),
        step=Decimal(5),
        default=Decimal(20),
    )
    assert spec.values() == (Decimal(15), Decimal(20), Decimal(25))


def test_a_spec_with_a_default_outside_its_range_is_refused() -> None:
    with pytest.raises(ValidationError, match="outside its own range"):
        ParameterSpec(
            name="adx_minimum",
            type=ParameterType.DECIMAL,
            minimum=Decimal(15),
            maximum=Decimal(25),
            step=Decimal(5),
            default=Decimal(99),
        )


def test_the_default_grid_stays_small_enough_to_finish() -> None:
    """Performance is a correctness property on the target hardware."""
    combinations = 1
    for spec in default_parameter_space():
        combinations *= len(spec.values())
    assert combinations <= 64


# ----------------------------------------------------------------------
# Search
# ----------------------------------------------------------------------


SMALL_SPACE = (
    ParameterSpec(
        name="adx_minimum",
        type=ParameterType.DECIMAL,
        minimum=Decimal(15),
        maximum=Decimal(20),
        step=Decimal(5),
        default=Decimal(20),
    ),
)


def test_an_oversized_grid_is_refused_not_truncated() -> None:
    """Testing the first N of a grid makes the report describe a prefix."""
    with pytest.raises(ValueError, match="above the"):
        optimize(
            wavy(900),
            symbol="TESTUSDT",
            timeframe=Timeframe.H1,
            space=default_parameter_space(),
            max_combinations=5,
        )


def test_a_report_separates_in_sample_from_out_of_sample() -> None:
    """The headline structural guarantee: three windows, three results."""
    report = optimize(wavy(900), symbol="TESTUSDT", timeframe=Timeframe.H1, space=SMALL_SPACE)
    assert report.objective == OBJECTIVE_METHOD
    assert report.trials
    for trial in report.trials:
        # Train and validation are separate fields and neither is merged away.
        assert trial.train_score is not None
        assert trial.validation_score is not None
    assert report.split.test_start >= report.split.validation_end


def test_the_test_window_is_scored_but_never_used_to_select() -> None:
    """Selection must come from validation, or the split is decorative."""
    report = optimize(wavy(900), symbol="TESTUSDT", timeframe=Timeframe.H1, space=SMALL_SPACE)
    assert report.best is not None
    best_validation = report.best.validation_score
    assert best_validation is not None
    for trial in report.trials:
        assert trial.validation_score is not None
        assert trial.validation_score.value <= best_validation.value


def test_the_search_is_deterministic() -> None:
    candles = wavy(900)
    first = optimize(candles, symbol="T", timeframe=Timeframe.H1, space=SMALL_SPACE)
    second = optimize(candles, symbol="T", timeframe=Timeframe.H1, space=SMALL_SPACE)
    assert first.best is not None and second.best is not None
    assert first.best.parameters == second.best.parameters
    assert first.best.validation_score == second.best.validation_score
    assert first.test_score == second.test_score


def test_invalid_parameter_combinations_are_skipped_not_repaired() -> None:
    """ema_fast >= ema_slow inverts every rule. Skipped, never reordered."""
    space = (
        ParameterSpec(
            name="ema_fast",
            type=ParameterType.INTEGER,
            minimum=Decimal(20),
            maximum=Decimal(60),
            step=Decimal(40),
            default=Decimal(20),
        ),
        ParameterSpec(
            name="ema_slow",
            type=ParameterType.INTEGER,
            minimum=Decimal(30),
            maximum=Decimal(30),
            step=Decimal(10),
            default=Decimal(30),
        ),
    )
    report = optimize(wavy(900), symbol="T", timeframe=Timeframe.H1, space=space)
    # (20, 30) is valid; (60, 30) is not and must never have been evaluated.
    assert report.combinations_possible == 2
    assert report.combinations_evaluated == 1
    assert all(t.parameters["ema_fast"] < t.parameters["ema_slow"] for t in report.trials)


def test_a_report_records_everything_needed_to_reproduce_it() -> None:
    config = BacktestConfig(fee_bps=Decimal(7), slippage_bps=Decimal(3))
    report = optimize(
        wavy(900),
        symbol="testusdt",
        timeframe=Timeframe.H1,
        config=config,
        space=SMALL_SPACE,
    )
    assert report.symbol == "TESTUSDT"
    assert report.strategy_version
    assert report.config.fee_bps == Decimal(7)
    assert report.config.slippage_bps == Decimal(3)
    assert report.space == SMALL_SPACE
    assert report.first_bar_time is not None and report.last_bar_time is not None


def test_a_mismatched_split_is_refused() -> None:
    split = split_chronological(500)
    with pytest.raises(ValueError, match="different number of bars"):
        optimize(wavy(900), symbol="T", timeframe=Timeframe.H1, space=SMALL_SPACE, split=split)


def test_the_report_disclaims_optimality() -> None:
    report = optimize(wavy(900), symbol="T", timeframe=Timeframe.H1, space=SMALL_SPACE)
    assert "not evidence that these" in report.disclaimer
    assert str(MIN_TRADES_FOR_OBJECTIVE)  # the gate is a published constant
