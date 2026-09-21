"""Walk-forward evaluation: the windows now drive a selection procedure.

``walk_forward_windows`` existed and was tested as a primitive, but nothing
consumed it, so the honest-evaluation story stopped at a single three-way
split. A split answers "did these parameters hold up once". Walk-forward
answers the stronger question: does the *procedure* keep working as the
market moves.

What is asserted here is almost entirely that it cannot cheat. A fold must
select on its training window only, validation must sit strictly after the
bars that chose it, and a run where everything was rejected must not be able
to present a tie-break as convergence.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from aetheris.analysis.optimize import (
    MAX_WALK_FORWARD_FOLDS,
    OBJECTIVE_METHOD,
    default_parameter_space,
    walk_forward,
    walk_forward_windows,
)
from aetheris.domain.backtest import BacktestConfig
from aetheris.domain.enums import Timeframe
from aetheris.domain.market import Candle
from aetheris.domain.optimization import ParameterSpec, ParameterType

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


def run(candles: list[Candle], **kwargs: object):  # type: ignore[no-untyped-def]
    params: dict[str, object] = {
        "symbol": "TESTUSDT",
        "timeframe": Timeframe.H1,
        "train_bars": 400,
        "validation_bars": 200,
        "space": SMALL_SPACE,
    }
    params.update(kwargs)
    return walk_forward(candles, **params)  # type: ignore[arg-type]


# ----------------------------------------------------------------------
# The windows actually drive something now
# ----------------------------------------------------------------------


def test_the_windows_produce_folds():  # type: ignore[no-untyped-def]
    """The gap this closes: windows existed but nothing consumed them."""
    report = run(wavy(1200))
    assert report.fold_count > 0
    assert report.folds
    assert report.objective == OBJECTIVE_METHOD


def test_every_fold_matches_a_generated_window():  # type: ignore[no-untyped-def]
    """The driver must not invent windows of its own."""
    candles = wavy(1200)
    expected = walk_forward_windows(len(candles), train_bars=400, validation_bars=200)
    report = run(candles)
    assert [f.window.index for f in report.folds] == [w.index for w in expected]
    for fold, window in zip(report.folds, expected, strict=True):
        assert fold.window == window


# ----------------------------------------------------------------------
# It cannot cheat
# ----------------------------------------------------------------------


def test_validation_always_sits_after_the_bars_that_chose_it():  # type: ignore[no-untyped-def]
    """The whole point. A fold that could see its own validation is worthless."""
    report = run(wavy(1200))
    for fold in report.folds:
        assert fold.window.train_end <= fold.window.validation_start
        assert fold.window.train_start < fold.window.train_end
        assert fold.window.validation_start < fold.window.validation_end


def test_folds_only_ever_move_forward():  # type: ignore[no-untyped-def]
    report = run(wavy(1200))
    for earlier, later in zip(report.folds, report.folds[1:], strict=False):
        assert later.window.train_start > earlier.window.train_start
        assert later.window.validation_start >= earlier.window.validation_end


def test_selection_is_made_on_the_training_window_alone():  # type: ignore[no-untyped-def]
    """Changing bars *after* a fold's validation window cannot change its choice.

    This is the strongest available check that no fold reads forward: the
    first fold's selection and train score must be byte-identical whether or
    not the tail of the series exists at all.
    """
    short = wavy(800)
    long = wavy(1200)
    assert short == long[:800]

    first_short = run(short).folds[0]
    first_long = run(long).folds[0]
    assert first_short.window == first_long.window
    assert first_short.selected == first_long.selected
    assert first_short.train_score.value == first_long.train_score.value
    assert first_short.validation_score.value == first_long.validation_score.value


def test_train_and_validation_scores_stay_separate_fields():  # type: ignore[no-untyped-def]
    """No combined figure, so in-sample cannot flatter out-of-sample."""
    report = run(wavy(1200))
    for fold in report.folds:
        assert fold.train_score is not None
        assert fold.validation_score is not None
    assert report.mean_train_score is not None
    assert report.mean_validation_score is not None


# ----------------------------------------------------------------------
# Honest reporting
# ----------------------------------------------------------------------


def test_a_run_where_everything_was_rejected_says_so():  # type: ignore[no-untyped-def]
    """Agreement between folds reads strongest exactly when it means least.

    When every candidate is gated out, the per-fold "winner" is first-in-order
    out of a field of zeros. Reporting that as convergence would be the most
    misleading line in the report.
    """
    report = run(wavy(1200))
    rejected = all(f.train_score.rejected_reason is not None for f in report.folds)
    if rejected:
        assert report.most_selected_folds == report.fold_count
        assert any("ties at zero" in w for w in report.warnings)


def test_a_series_too_short_reports_absence_not_zero():  # type: ignore[no-untyped-def]
    """No folds is the absence of a result, not a result of zero."""
    report = run(wavy(300))
    assert report.fold_count == 0
    assert report.mean_validation_score is None
    assert report.mean_train_score is None
    assert report.most_selected is None
    assert any("cannot hold even one" in w for w in report.warnings)


def test_a_validation_window_below_warmup_is_flagged():  # type: ignore[no-untyped-def]
    """Otherwise every fold silently scores zero and looks like a bad strategy."""
    report = run(wavy(1200), train_bars=400, validation_bars=40)
    assert any("warm-up" in w for w in report.warnings)


def test_the_report_disclaims_prediction():  # type: ignore[no-untyped-def]
    report = run(wavy(1200))
    assert "not evidence that the procedure is profitable in future" in report.disclaimer
    assert "not an" in report.disclaimer


# ----------------------------------------------------------------------
# Bounds and reproducibility
# ----------------------------------------------------------------------


def test_an_oversized_grid_is_refused_per_fold():  # type: ignore[no-untyped-def]
    """A walk-forward run pays the grid cost once per fold, so the bound bites."""
    with pytest.raises(ValueError, match="per fold"):
        run(wavy(1200), space=default_parameter_space(), max_combinations=5)


def test_too_many_folds_is_refused_rather_than_truncated():  # type: ignore[no-untyped-def]
    """Truncating would silently evaluate an arbitrary prefix of history."""
    with pytest.raises(ValueError, match="folds, above the"):
        run(wavy(3000), train_bars=100, validation_bars=50, max_folds=3)


def test_the_fold_ceiling_is_a_real_constant():  # type: ignore[no-untyped-def]
    assert MAX_WALK_FORWARD_FOLDS <= 24


def test_the_run_is_deterministic():  # type: ignore[no-untyped-def]
    candles = wavy(1200)
    first = run(candles)
    second = run(candles)
    assert first.model_dump() == second.model_dump()


def test_the_report_records_what_reproduces_it():  # type: ignore[no-untyped-def]
    config = BacktestConfig(fee_bps=Decimal(7), slippage_bps=Decimal(3))
    report = run(wavy(1200), symbol="testusdt", config=config)
    assert report.symbol == "TESTUSDT"
    assert report.strategy_version
    assert report.config.fee_bps == Decimal(7)
    assert report.space == SMALL_SPACE
    assert report.train_bars == 400
    assert report.validation_bars == 200
    assert report.step_bars == 200


def test_the_step_defaults_to_non_overlapping_validation():  # type: ignore[no-untyped-def]
    """Every bar is validated at most once, so no outcome is counted twice."""
    report = run(wavy(1200))
    for earlier, later in zip(report.folds, report.folds[1:], strict=False):
        assert earlier.window.validation_end <= later.window.validation_start
