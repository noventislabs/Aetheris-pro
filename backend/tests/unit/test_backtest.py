"""Backtest engine: no look-ahead, pessimistic fills, honest metrics.

The two tests that matter most are the look-ahead equivalence check and the
pessimism rules. A backtest that peeks forward or resolves intrabar ambiguity
in its own favour produces no error, no warning and a flattering equity curve —
which is precisely why it needs to be asserted rather than reasoned about.
"""

from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from aetheris.analysis.backtest.engine import (
    MAX_EQUITY_POINTS,
    MIN_TRADES_FOR_MEANINGFUL_METRICS,
    _bias_series,
    resolve_intrabar_exit,
    run_backtest,
)
from aetheris.analysis.backtest.metrics import compute_metrics
from aetheris.analysis.strategies.trend_momentum import TrendMomentumParams, warmup_bars
from aetheris.domain.backtest import BacktestConfig, BacktestStatus, ExitReason
from aetheris.domain.enums import PositionSide, Timeframe
from aetheris.domain.market import Candle

BASE = datetime(2026, 1, 1, tzinfo=UTC)
HOUR = 3600
PARAMS = TrendMomentumParams()


def bar(index: int, price: float, *, spread: float = 0.006) -> Candle:
    start = BASE + timedelta(seconds=HOUR * index)
    value = Decimal(str(round(price, 6)))
    return Candle(
        open_time=start,
        close_time=start + timedelta(seconds=HOUR, milliseconds=-1),
        open=value,
        high=Decimal(str(round(price * (1 + spread), 6))),
        low=Decimal(str(round(price * (1 - spread), 6))),
        close=value,
        volume=Decimal(100),
    )


def walk(count: int, *, seed: int = 12345, regime: int = 80) -> list[Candle]:
    """A deterministic pseudo-random walk with alternating trend regimes.

    Noisy on purpose: a smooth ramp pins RSI at an extreme, which the strategy
    correctly reads as NEUTRAL, so a clean series produces no trades at all and
    tests nothing.
    """
    value, state, prices = 100.0, seed, []
    for index in range(count):
        state = (1103515245 * state + 12345) % (2**31)
        noise = (state / 2**31 - 0.5) * 0.02
        drift = 0.003 if (index // regime) % 2 == 0 else -0.003
        value *= 1 + drift + noise
        prices.append(value)
    return [bar(index, price) for index, price in enumerate(prices)]


def run(candles: list[Candle], **overrides: object) -> object:
    return run_backtest(
        candles,
        symbol="TESTUSDT",
        timeframe=Timeframe.H1,
        config=BacktestConfig(**overrides),  # type: ignore[arg-type]
    )


# ----------------------------------------------------------------------
# No look-ahead
# ----------------------------------------------------------------------


def test_bias_series_matches_per_prefix_recomputation() -> None:
    """The engine's single-pass optimisation must be semantically identical.

    Indicators are computed once over the whole history rather than recomputed
    on every prefix, which is only valid because no indicator reads forward. If
    that ever stopped holding, this backtest would silently start trading on
    information it could not have had.
    """
    candles = walk(160)
    whole = _bias_series(candles, PARAMS)

    for cut in (60, 90, 120, 159):
        prefix = _bias_series(candles[: cut + 1], PARAMS)
        assert prefix[cut] == whole[cut], (
            f"bias at bar {cut} changed when future bars were removed: "
            f"{prefix[cut]} with a {cut + 1}-bar series vs {whole[cut]} with 160"
        )


def test_future_bars_do_not_change_earlier_trades() -> None:
    """Truncating the data must not rewrite history before the cut."""
    candles = walk(300)
    short = run(candles[:200])
    full = run(candles)

    # Trades that both runs had time to complete must be identical.
    cutoff = candles[199].open_time
    short_trades = [t for t in short.trades if t.exit_time < cutoff]  # type: ignore[attr-defined]
    full_trades = [t for t in full.trades if t.exit_time < cutoff]  # type: ignore[attr-defined]
    assert short_trades == full_trades


def test_a_signal_never_fills_on_the_bar_that_produced_it() -> None:
    """Entries fill at the next bar's open, not the close that decided them."""
    candles = walk(300)
    result = run(candles)
    by_time = {candle.open_time: index for index, candle in enumerate(candles)}
    biases = _bias_series(candles, PARAMS)

    assert result.trades  # type: ignore[attr-defined]
    for trade in result.trades:  # type: ignore[attr-defined]
        entry_index = by_time[trade.entry_time]
        # The bar *before* the entry is the one whose bias called for it.
        deciding = biases[entry_index - 1]
        expected = (
            PositionSide.LONG
            if deciding is not None and deciding.value == "LONG_BIAS"
            else PositionSide.SHORT
        )
        assert trade.side is expected, (
            f"trade entered at bar {entry_index} on side {trade.side}, but the "
            f"previous bar's bias was {deciding}"
        )


# ----------------------------------------------------------------------
# Pessimistic intrabar resolution
# ----------------------------------------------------------------------


def test_stop_wins_when_a_bar_covers_both_stop_and_target() -> None:
    """OHLC cannot say which came first, so the adverse one is assumed."""
    triggered = resolve_intrabar_exit(
        side=PositionSide.LONG,
        bar_high=Decimal(110),
        bar_low=Decimal(90),
        stop_price=Decimal(98),
        target_price=Decimal(104),
        liquidation_price=None,
        trail_price=None,
    )
    assert triggered == (Decimal(98), ExitReason.STOP_LOSS)


def test_stop_wins_for_a_short_as_well() -> None:
    triggered = resolve_intrabar_exit(
        side=PositionSide.SHORT,
        bar_high=Decimal(110),
        bar_low=Decimal(90),
        stop_price=Decimal(102),
        target_price=Decimal(96),
        liquidation_price=None,
        trail_price=None,
    )
    assert triggered == (Decimal(102), ExitReason.STOP_LOSS)


def test_the_nearest_adverse_level_fires_first() -> None:
    """A stop above the liquidation price is reached first on the way down."""
    triggered = resolve_intrabar_exit(
        side=PositionSide.LONG,
        bar_high=Decimal(101),
        bar_low=Decimal(80),
        stop_price=Decimal(98),
        target_price=None,
        liquidation_price=Decimal(90),
        trail_price=None,
    )
    assert triggered == (Decimal(98), ExitReason.STOP_LOSS)


def test_liquidation_fires_when_it_is_the_nearest_level() -> None:
    triggered = resolve_intrabar_exit(
        side=PositionSide.LONG,
        bar_high=Decimal(101),
        bar_low=Decimal(80),
        stop_price=Decimal(85),
        target_price=None,
        liquidation_price=Decimal(96),
        trail_price=None,
    )
    assert triggered == (Decimal(96), ExitReason.LIQUIDATION)


def test_trailing_stop_can_be_the_binding_level() -> None:
    triggered = resolve_intrabar_exit(
        side=PositionSide.LONG,
        bar_high=Decimal(120),
        bar_low=Decimal(100),
        stop_price=Decimal(95),
        target_price=None,
        liquidation_price=None,
        trail_price=Decimal(105),
    )
    assert triggered == (Decimal(105), ExitReason.TRAILING_STOP)


def test_target_fires_only_when_no_adverse_level_was_touched() -> None:
    triggered = resolve_intrabar_exit(
        side=PositionSide.LONG,
        bar_high=Decimal(110),
        bar_low=Decimal(99),
        stop_price=Decimal(98),
        target_price=Decimal(104),
        liquidation_price=None,
        trail_price=None,
    )
    assert triggered == (Decimal(104), ExitReason.TAKE_PROFIT)


def test_an_untouched_bar_triggers_nothing() -> None:
    assert (
        resolve_intrabar_exit(
            side=PositionSide.LONG,
            bar_high=Decimal(103),
            bar_low=Decimal(99),
            stop_price=Decimal(98),
            target_price=Decimal(104),
            liquidation_price=None,
            trail_price=None,
        )
        is None
    )


# ----------------------------------------------------------------------
# Trade arithmetic
# ----------------------------------------------------------------------


def test_every_trade_is_internally_consistent() -> None:
    """net = gross minus fees, return is against margin, equity chains correctly."""
    result = run(walk(400))
    assert result.trades  # type: ignore[attr-defined]

    equity = result.config.starting_balance  # type: ignore[attr-defined]
    for trade in result.trades:  # type: ignore[attr-defined]
        assert abs(trade.net_pnl - (trade.gross_pnl - trade.fees)) <= Decimal("0.00000001")
        expected_return = trade.net_pnl / trade.margin * Decimal(100)
        assert abs(trade.return_percent - expected_return) <= Decimal("0.0001")
        equity += trade.net_pnl
        assert abs(trade.equity_after - equity) <= Decimal("0.00000001")
        assert trade.notional == trade.margin * trade.leverage or trade.leverage == Decimal(1)


def test_fees_are_charged_on_both_sides() -> None:
    free = run(walk(400), fee_bps=Decimal(0), slippage_bps=Decimal(0))
    charged = run(walk(400), fee_bps=Decimal(10), slippage_bps=Decimal(0))
    assert charged.metrics.total_fees > 0  # type: ignore[attr-defined]
    assert free.metrics.total_fees == 0  # type: ignore[attr-defined]
    # Fees can only reduce the result.
    assert charged.metrics.net_pnl < free.metrics.net_pnl  # type: ignore[attr-defined]


def test_slippage_only_ever_hurts() -> None:
    clean = run(walk(400), slippage_bps=Decimal(0), fee_bps=Decimal(0))
    slipped = run(walk(400), slippage_bps=Decimal(25), fee_bps=Decimal(0))
    assert slipped.metrics.net_pnl < clean.metrics.net_pnl  # type: ignore[attr-defined]


def test_leverage_scales_the_result() -> None:
    """Five times the notional moves roughly five times the PnL, less fees."""
    one = run(walk(400), leverage=Decimal(1))
    five = run(walk(400), leverage=Decimal(5))
    assert five.metrics.total_trades == one.metrics.total_trades  # type: ignore[attr-defined]
    assert abs(five.metrics.net_pnl) > abs(one.metrics.net_pnl)  # type: ignore[attr-defined]


def test_a_trade_can_never_lose_more_than_its_margin() -> None:
    """The invariant that makes a leveraged simulation survivable."""
    result = run(walk(600), leverage=Decimal(20), stop_loss_percent=None)
    for trade in result.trades:  # type: ignore[attr-defined]
        assert trade.net_pnl >= -trade.margin


# ----------------------------------------------------------------------
# Exit reasons respond to configuration
# ----------------------------------------------------------------------


def test_a_tight_stop_produces_stop_exits() -> None:
    result = run(walk(400), stop_loss_percent=Decimal("0.1"), take_profit_percent=None)
    reasons = Counter(trade.exit_reason for trade in result.trades)  # type: ignore[attr-defined]
    assert reasons[ExitReason.STOP_LOSS] > 0


def test_a_tight_target_produces_take_profit_exits() -> None:
    result = run(walk(400), stop_loss_percent=None, take_profit_percent=Decimal("0.1"))
    reasons = Counter(trade.exit_reason for trade in result.trades)  # type: ignore[attr-defined]
    assert reasons[ExitReason.TAKE_PROFIT] > 0


def test_a_trailing_stop_produces_trailing_exits() -> None:
    result = run(
        walk(400),
        stop_loss_percent=None,
        take_profit_percent=None,
        trailing_stop_percent=Decimal("0.5"),
    )
    reasons = Counter(trade.exit_reason for trade in result.trades)  # type: ignore[attr-defined]
    assert reasons[ExitReason.TRAILING_STOP] > 0


def test_stop_exits_are_never_better_than_entry_for_a_long() -> None:
    result = run(walk(400), stop_loss_percent=Decimal("0.5"), take_profit_percent=None)
    for trade in result.trades:  # type: ignore[attr-defined]
        if trade.exit_reason is ExitReason.STOP_LOSS and trade.side is PositionSide.LONG:
            assert trade.exit_price < trade.entry_price


def test_direction_can_be_restricted() -> None:
    longs_only = run(walk(400), allow_short=False)
    assert all(t.side is PositionSide.LONG for t in longs_only.trades)  # type: ignore[attr-defined]
    shorts_only = run(walk(400), allow_long=False)
    assert all(t.side is PositionSide.SHORT for t in shorts_only.trades)  # type: ignore[attr-defined]


def test_both_directions_disabled_is_rejected() -> None:
    with pytest.raises(ValueError, match="at least one of allow_long or allow_short"):
        BacktestConfig(allow_long=False, allow_short=False)


# ----------------------------------------------------------------------
# Metrics
# ----------------------------------------------------------------------


def test_metrics_agree_with_the_trade_list() -> None:
    result = run(walk(500))
    metrics = result.metrics  # type: ignore[attr-defined]
    trades = result.trades  # type: ignore[attr-defined]

    assert metrics.total_trades == len(trades)
    assert metrics.winning_trades == sum(1 for t in trades if t.net_pnl > 0)
    assert metrics.losing_trades == sum(1 for t in trades if t.net_pnl < 0)
    assert (
        metrics.winning_trades + metrics.losing_trades + metrics.breakeven_trades
        == metrics.total_trades
    )
    assert abs(metrics.net_pnl - sum(t.net_pnl for t in trades)) <= Decimal("0.00000001")
    assert abs(metrics.total_fees - sum(t.fees for t in trades)) <= Decimal("0.00000001")
    assert abs(metrics.ending_balance - (metrics.starting_balance + metrics.net_pnl)) <= Decimal(
        "0.0001"
    )


def test_profit_factor_is_undefined_without_losses() -> None:
    """Not infinity, not a big number. Undefined, and said so."""
    metrics = compute_metrics(
        trades=(),
        curve=(),
        config=BacktestConfig(),
        timeframe=Timeframe.H1,
        bars_in_position=0,
        trades_open_at_end=0,
    )
    assert metrics.profit_factor is None
    assert metrics.win_rate_percent is None
    assert metrics.average_trade is None


def test_sharpe_like_is_undefined_on_a_flat_curve() -> None:
    """A curve with no dispersion has no ratio; zero would read as a finding."""
    flat = [bar(index, 100.0) for index in range(200)]
    result = run(flat)
    assert result.metrics.sharpe_like_ratio is None  # type: ignore[attr-defined]


def test_drawdown_is_never_negative_and_bounded() -> None:
    result = run(walk(500), leverage=Decimal(5))
    metrics = result.metrics  # type: ignore[attr-defined]
    assert metrics.max_drawdown_percent >= 0
    assert metrics.max_drawdown_absolute >= 0
    for point in result.equity_curve:  # type: ignore[attr-defined]
        assert point.drawdown_percent >= 0


def test_drawdown_is_measured_before_the_curve_is_thinned() -> None:
    """The trough is exactly what a sampler is most likely to drop."""
    candles = walk(1200)
    result = run(candles)
    assert len(result.equity_curve) <= MAX_EQUITY_POINTS  # type: ignore[attr-defined]
    assert result.metrics.bars_tested == len(candles)  # type: ignore[attr-defined]
    # The reported maximum comes from the full curve, so it is at least as
    # large as anything visible in the thinned one.
    visible = max(p.drawdown_percent for p in result.equity_curve)  # type: ignore[attr-defined]
    assert result.metrics.max_drawdown_percent >= visible  # type: ignore[attr-defined]


def test_equity_curve_keeps_its_final_point_when_thinned() -> None:
    candles = walk(1200)
    result = run(candles)
    assert result.equity_curve[-1].time == candles[-1].open_time  # type: ignore[attr-defined]


def test_exposure_is_a_percentage_of_bars_held() -> None:
    result = run(walk(500))
    assert 0 <= result.metrics.exposure_percent <= 100  # type: ignore[attr-defined]


# ----------------------------------------------------------------------
# Status, warnings and honesty
# ----------------------------------------------------------------------


def test_too_few_candles_is_reported_not_simulated() -> None:
    """A simulation needs the warm-up plus at least one bar to act on.

    The message states warm-up + 1, which is the real requirement: at exactly
    the warm-up the strategy has a first value but no later bar to fill on.
    """
    result = run(walk(20))
    assert result.status is BacktestStatus.INSUFFICIENT_DATA  # type: ignore[attr-defined]
    assert result.metrics is None  # type: ignore[attr-defined]
    assert str(warmup_bars(PARAMS) + 1) in (result.detail or "")  # type: ignore[attr-defined]
    assert "20 available" in (result.detail or "")  # type: ignore[attr-defined]


def test_exactly_the_minimum_number_of_candles_runs() -> None:
    needed = warmup_bars(PARAMS) + 2
    assert run(walk(needed)).status is BacktestStatus.COMPLETED  # type: ignore[attr-defined]
    assert run(walk(needed - 1)).status is BacktestStatus.INSUFFICIENT_DATA  # type: ignore[attr-defined]


def test_a_thin_sample_is_warned_about_not_hidden() -> None:
    result = run(walk(200))
    metrics = result.metrics  # type: ignore[attr-defined]
    if 0 < metrics.total_trades < MIN_TRADES_FOR_MEANINGFUL_METRICS:
        assert any("coincidences" in warning for warning in result.warnings)  # type: ignore[attr-defined]


def test_no_trades_is_stated_plainly() -> None:
    """A flat market produces no signals, and the result says so."""
    result = run([bar(index, 100.0) for index in range(200)])
    assert result.metrics.total_trades == 0  # type: ignore[attr-defined]
    assert any("No trades were taken" in warning for warning in result.warnings)  # type: ignore[attr-defined]


def test_leverage_carries_its_own_warning() -> None:
    result = run(walk(300), leverage=Decimal(10))
    assert any("simulation input only" in warning for warning in result.warnings)  # type: ignore[attr-defined]


def test_a_position_open_at_the_end_is_flagged() -> None:
    for length in range(300, 340):
        result = run(walk(length))
        if result.metrics.trades_open_at_end:  # type: ignore[attr-defined]
            assert any("still open" in warning for warning in result.warnings)  # type: ignore[attr-defined]
            assert any(
                t.exit_reason is ExitReason.END_OF_DATA
                for t in result.trades  # type: ignore[attr-defined]
            )
            return
    pytest.skip("no run in this sweep ended with an open position")


def test_every_result_states_its_assumptions_and_disclaimer() -> None:
    result = run(walk(300))
    joined = " ".join(result.assumptions).lower()  # type: ignore[attr-defined]
    assert "next bar's open" in joined
    assert "stop is assumed to have come first" in joined
    assert "not modelled" in joined
    assert "funding" in joined
    assert result.label == "HISTORICAL SIMULATION"  # type: ignore[attr-defined]
    assert "not a prediction" in result.disclaimer.lower()  # type: ignore[attr-defined]


def test_results_are_deterministic() -> None:
    """Same candles, same config, byte-identical result."""
    candles = walk(400)
    assert run(candles) == run(candles)


def test_no_execution_vocabulary_leaks_into_the_backtest_layer() -> None:
    """A simulation must not look like an order path."""
    import pathlib

    import aetheris

    root = pathlib.Path(aetheris.__file__).parent / "analysis" / "backtest"
    for path in root.rglob("*.py"):
        source = path.read_text(encoding="utf-8")
        for forbidden in ("set_leverage", "place_order", "/fapi/", "api_key"):
            assert forbidden not in source, f"{path.name} references {forbidden}"
