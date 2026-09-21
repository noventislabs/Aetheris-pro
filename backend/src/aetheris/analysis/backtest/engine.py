"""Historical simulation engine.

Pure: candles in, a result out. No HTTP, no venue, no framework, no settings —
the same constraint the indicator and strategy layers carry, so this runs over
a fixture file as readily as over live history.

## How a bar is processed

Order within each bar is the whole correctness story:

1. **Execute** any action decided on the *previous* bar, filling at this bar's
   open.
2. **Manage** the open position against this bar's high/low.
3. **Mark** equity to this bar's close.
4. **Decide** from indicator values at this bar, scheduling for the next bar.

Deciding last is what prevents look-ahead: a signal computed from bar *i* can
only ever act on bar *i+1*.

## The assumptions, stated

**Signals fill at the next bar's open, not at the close that produced them.**
Filling at the same close assumes zero latency between a bar closing, an
indicator recomputing, and an order resting — which is how a backtest quietly
becomes optimistic.

**When a bar could hit both the stop and the target, the stop wins.** OHLC
cannot say which came first inside a bar; assuming the favourable one is the
single most common way a backtest flatters itself. This engine always assumes
the adverse path.

**Trailing stops trail on closed bars.** The trail level for bar *i* is derived
from extremes up to bar *i-1*, then updated with bar *i*'s extreme afterwards.
Updating first and then checking the same bar would assume an intrabar ordering
the data does not contain.

**Liquidation is modelled simply and optimistically.** A position closes when
its loss reaches the posted margin. Real venues liquidate *earlier*, at a
maintenance-margin threshold that varies by symbol and notional tier, and they
charge a liquidation fee. Results at higher leverage are therefore better than
reality, and must not be read as a liquidation study.

**Not modelled:** funding payments, partial fills, order-book depth, maker
rebates, borrow costs, exchange downtime, and per-symbol quantity/notional
filters. Each is listed on the result so its absence is visible.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Final

from aetheris.analysis.backtest.metrics import compute_metrics
from aetheris.analysis.indicators.core import ema
from aetheris.analysis.indicators.library import adx, macd, rsi
from aetheris.analysis.strategies import trend_momentum
from aetheris.analysis.strategies.trend_momentum import TrendMomentumParams
from aetheris.domain.backtest import (
    BacktestConfig,
    BacktestResult,
    BacktestStatus,
    EquityPoint,
    ExitReason,
    Trade,
)
from aetheris.domain.enums import PositionSide, Timeframe
from aetheris.domain.market import Candle
from aetheris.domain.strategy import StrategyBias

__all__ = [
    "MAX_EQUITY_POINTS",
    "MIN_TRADES_FOR_MEANINGFUL_METRICS",
    "resolve_intrabar_exit",
    "run_backtest",
]

_ZERO: Final = Decimal(0)
_ONE: Final = Decimal(1)
_HUNDRED: Final = Decimal(100)
_BPS: Final = Decimal(10_000)
_MONEY: Final = Decimal("0.00000001")
_PCT: Final = Decimal("0.0001")

#: Below this, the metrics describe a handful of coincidences rather than a
#: strategy. The result carries a warning rather than hiding the numbers.
MIN_TRADES_FOR_MEANINGFUL_METRICS: Final = 20

#: The equity curve is downsampled beyond this so a 1500-bar run does not ship
#: a payload dominated by chart points.
MAX_EQUITY_POINTS: Final = 500

ASSUMPTIONS: Final[tuple[str, ...]] = (
    "Signals computed on a closed bar fill at the NEXT bar's open, never at the "
    "close that produced them.",
    "When one bar could hit both the stop and the target, the stop is assumed to "
    "have come first. OHLC cannot say which did.",
    "Trailing stops trail on closed bars: the level checked on a bar comes from "
    "extremes up to the previous bar.",
    "Liquidation closes a position when its loss reaches the posted margin. Real "
    "venues liquidate earlier at a maintenance margin and charge a fee, so leveraged "
    "results here are better than reality.",
    "Fees are charged per side on notional; slippage moves every fill adversely.",
    "NOT modelled: funding payments, partial fills, order-book depth, maker rebates, "
    "borrow costs, exchange downtime, per-symbol lot/notional filters.",
)


@dataclass(slots=True)
class _OpenPosition:
    side: PositionSide
    entry_time: datetime
    entry_index: int
    entry_price: Decimal
    quantity: Decimal
    notional: Decimal
    margin: Decimal
    entry_fee: Decimal
    stop_price: Decimal | None
    target_price: Decimal | None
    liquidation_price: Decimal | None
    #: Extreme seen on bars strictly before the one being evaluated.
    trail_extreme: Decimal
    best_price: Decimal
    worst_price: Decimal


def _q(value: Decimal, exponent: Decimal = _MONEY) -> Decimal:
    return value.quantize(exponent)


def _bias_series(
    candles: Sequence[Candle], params: TrendMomentumParams
) -> list[StrategyBias | None]:
    """Compute the strategy bias for every bar, in one pass.

    Each indicator series is computed once over the whole history rather than
    recomputed on every prefix. That is O(n) instead of O(n²) **and it is
    equivalent**, because every indicator is guaranteed free of look-ahead:
    the value at index *i* depends only on bars up to *i*. A test asserts the
    two approaches agree bar for bar, so the optimisation cannot silently drift
    from the semantics it claims.
    """
    closes = [candle.close for candle in candles]
    fast = ema(closes, params.ema_fast)
    slow = ema(closes, params.ema_slow)
    adx_line = adx(candles, period=params.adx_period)["adx"]
    rsi_line = rsi(candles, period=params.rsi_period)["rsi"]
    histogram = macd(
        candles, fast=params.macd_fast, slow=params.macd_slow, signal=params.macd_signal
    )["histogram"]

    biases: list[StrategyBias | None] = []
    for index in range(len(candles)):
        values = (fast[index], slow[index], adx_line[index], rsi_line[index], histogram[index])
        if any(value is None for value in values):
            biases.append(None)
            continue
        ema_fast, ema_slow, adx_value, rsi_value, hist_value = values
        assert ema_fast is not None and ema_slow is not None and adx_value is not None
        assert rsi_value is not None and hist_value is not None
        _, _, bias = trend_momentum.evaluate_conditions(
            ema_fast=ema_fast,
            ema_slow=ema_slow,
            adx=adx_value,
            rsi=rsi_value,
            histogram=hist_value,
            params=params,
        )
        biases.append(bias)
    return biases


def _desired_side(bias: StrategyBias | None, config: BacktestConfig) -> PositionSide:
    """Map a bias to the position the rules would hold.

    The mapping is the documented bridge between phase 4's analysis and a
    simulated position: a directional bias holds that direction while it lasts,
    and anything else is flat.
    """
    if bias is StrategyBias.LONG_BIAS and config.allow_long:
        return PositionSide.LONG
    if bias is StrategyBias.SHORT_BIAS and config.allow_short:
        return PositionSide.SHORT
    return PositionSide.FLAT


def _apply_slippage(price: Decimal, *, side: PositionSide, entering: bool, bps: Decimal) -> Decimal:
    """Move a fill against the trader, always.

    Buying (opening a long, closing a short) fills higher; selling fills lower.
    """
    factor = bps / _BPS
    buying = (side is PositionSide.LONG) == entering
    return price * (_ONE + factor) if buying else price * (_ONE - factor)


def resolve_intrabar_exit(
    *,
    side: PositionSide,
    bar_high: Decimal,
    bar_low: Decimal,
    stop_price: Decimal | None,
    target_price: Decimal | None,
    liquidation_price: Decimal | None,
    trail_price: Decimal | None,
) -> tuple[Decimal, ExitReason] | None:
    """Decide which level, if any, this bar took the position out at.

    Extracted and public so the pessimism rules can be tested exhaustively
    rather than inferred from end-to-end runs.

    Two rules, both deliberately unfavourable:

    * **Adverse before favourable.** If a bar's range covers both the stop and
      the target, the stop is taken. OHLC does not record the path within a
      bar, and assuming the favourable order is the most common way a backtest
      flatters itself.
    * **Nearest adverse level first.** Among stop, trailing stop and
      liquidation, the one closest to entry is reached first on the way down
      (long) or up (short), so it is the one that fires.
    """
    long = side is PositionSide.LONG
    adverse: list[tuple[Decimal, ExitReason]] = []
    if stop_price is not None:
        adverse.append((stop_price, ExitReason.STOP_LOSS))
    if liquidation_price is not None:
        adverse.append((liquidation_price, ExitReason.LIQUIDATION))
    if trail_price is not None:
        adverse.append((trail_price, ExitReason.TRAILING_STOP))

    if adverse:
        level, reason = max(adverse) if long else min(adverse)
        if (long and bar_low <= level) or (not long and bar_high >= level):
            return level, reason

    if target_price is not None and (
        (long and bar_high >= target_price) or (not long and bar_low <= target_price)
    ):
        return target_price, ExitReason.TAKE_PROFIT

    return None


def run_backtest(
    candles: Sequence[Candle],
    *,
    symbol: str,
    timeframe: Timeframe,
    config: BacktestConfig | None = None,
    params: TrendMomentumParams | None = None,
    ran_at: datetime | None = None,
) -> BacktestResult:
    """Simulate the baseline strategy over historical candles.

    ``candles`` must be closed, ascending and unique -- run them through
    ``prepare_candles`` first. The caller owns that step so a backtest over a
    fixture file does not need a clock.
    """
    settings = config or BacktestConfig()
    strategy_params = params or TrendMomentumParams()
    warmup = trend_momentum.warmup_bars(strategy_params)

    base = {
        "symbol": symbol.upper(),
        "timeframe": timeframe,
        "strategy": trend_momentum.STRATEGY_KEY,
        "strategy_version": trend_momentum.STRATEGY_VERSION,
        "config": settings,
        "ran_at": ran_at,
        "assumptions": ASSUMPTIONS,
    }

    # A simulation needs the warm-up *plus* room to trade. Reporting that is
    # better than returning a result built from three bars.
    if len(candles) < warmup + 2:
        return BacktestResult(
            **base,  # type: ignore[arg-type]
            status=BacktestStatus.INSUFFICIENT_DATA,
            detail=(
                f"Needs more than {warmup + 1} closed candles for the strategy to warm "
                f"up and act; {len(candles)} available"
            ),
        )

    biases = _bias_series(candles, strategy_params)

    equity = settings.starting_balance
    peak = equity
    position: _OpenPosition | None = None
    pending_side: PositionSide | None = None
    trades: list[Trade] = []
    curve: list[EquityPoint] = []
    bars_in_position = 0

    fee_rate = settings.fee_bps / _BPS
    stop_fraction = (
        settings.stop_loss_percent / _HUNDRED if settings.stop_loss_percent is not None else None
    )
    target_fraction = (
        settings.take_profit_percent / _HUNDRED
        if settings.take_profit_percent is not None
        else None
    )
    trail_fraction = (
        settings.trailing_stop_percent / _HUNDRED
        if settings.trailing_stop_percent is not None
        else None
    )

    def close_position(
        open_position: _OpenPosition,
        *,
        raw_price: Decimal,
        reason: ExitReason,
        exit_time: datetime,
        exit_index: int,
    ) -> Decimal:
        nonlocal equity
        fill = _apply_slippage(
            raw_price, side=open_position.side, entering=False, bps=settings.slippage_bps
        )
        exit_notional = open_position.quantity * fill
        exit_fee = exit_notional * fee_rate
        if open_position.side is PositionSide.LONG:
            gross = open_position.quantity * (fill - open_position.entry_price)
        else:
            gross = open_position.quantity * (open_position.entry_price - fill)
        fees = open_position.entry_fee + exit_fee
        net = gross - fees
        # A position cannot lose more than the margin posted against it; the
        # liquidation check should have fired first, and this clamp makes the
        # invariant hold even at the boundary.
        net = max(net, -open_position.margin)
        equity = equity + net

        excursion_base = open_position.entry_price
        adverse = (
            (excursion_base - open_position.worst_price) / excursion_base * _HUNDRED
            if open_position.side is PositionSide.LONG
            else (open_position.worst_price - excursion_base) / excursion_base * _HUNDRED
        )
        favourable = (
            (open_position.best_price - excursion_base) / excursion_base * _HUNDRED
            if open_position.side is PositionSide.LONG
            else (excursion_base - open_position.best_price) / excursion_base * _HUNDRED
        )

        trades.append(
            Trade(
                side=open_position.side,
                entry_time=open_position.entry_time,
                exit_time=exit_time,
                entry_price=_q(open_position.entry_price),
                exit_price=_q(fill),
                quantity=_q(open_position.quantity),
                notional=_q(open_position.notional),
                margin=_q(open_position.margin),
                leverage=settings.leverage,
                exit_reason=reason,
                bars_held=max(exit_index - open_position.entry_index, 0),
                gross_pnl=_q(gross),
                fees=_q(fees),
                net_pnl=_q(net),
                return_percent=_q(net / open_position.margin * _HUNDRED, _PCT),
                equity_after=_q(equity),
                max_adverse_excursion_percent=_q(max(adverse, _ZERO), _PCT),
                max_favourable_excursion_percent=_q(max(favourable, _ZERO), _PCT),
            )
        )
        return net

    for index, bar in enumerate(candles):
        # --- 1. Execute what the previous bar decided, at this bar's open ---
        if pending_side is not None:
            if position is not None and position.side is not pending_side:
                close_position(
                    position,
                    raw_price=bar.open,
                    reason=ExitReason.SIGNAL_FLIP,
                    exit_time=bar.open_time,
                    exit_index=index,
                )
                position = None

            if position is None and pending_side is not PositionSide.FLAT and equity > _ZERO:
                entry_fill = _apply_slippage(
                    bar.open, side=pending_side, entering=True, bps=settings.slippage_bps
                )
                if entry_fill > _ZERO:
                    margin = equity * settings.position_size_percent / _HUNDRED
                    notional = margin * settings.leverage
                    quantity = notional / entry_fill
                    long = pending_side is PositionSide.LONG
                    position = _OpenPosition(
                        side=pending_side,
                        entry_time=bar.open_time,
                        entry_index=index,
                        entry_price=entry_fill,
                        quantity=quantity,
                        notional=notional,
                        margin=margin,
                        entry_fee=notional * fee_rate,
                        stop_price=(
                            entry_fill * (_ONE - stop_fraction)
                            if stop_fraction is not None and long
                            else entry_fill * (_ONE + stop_fraction)
                            if stop_fraction is not None
                            else None
                        ),
                        target_price=(
                            entry_fill * (_ONE + target_fraction)
                            if target_fraction is not None and long
                            else entry_fill * (_ONE - target_fraction)
                            if target_fraction is not None
                            else None
                        ),
                        liquidation_price=(
                            entry_fill * (_ONE - _ONE / settings.leverage)
                            if long
                            else entry_fill * (_ONE + _ONE / settings.leverage)
                        )
                        if settings.leverage > _ONE
                        else None,
                        trail_extreme=entry_fill,
                        best_price=entry_fill,
                        worst_price=entry_fill,
                    )
            pending_side = None

        # --- 2. Manage the open position against this bar's range ---
        if position is not None:
            long = position.side is PositionSide.LONG
            position.best_price = (
                max(position.best_price, bar.high) if long else min(position.best_price, bar.low)
            )
            position.worst_price = (
                min(position.worst_price, bar.low) if long else max(position.worst_price, bar.high)
            )

            # Trail level derives from bars *before* this one.
            trail_level: Decimal | None = None
            if trail_fraction is not None:
                trail_level = (
                    position.trail_extreme * (_ONE - trail_fraction)
                    if long
                    else position.trail_extreme * (_ONE + trail_fraction)
                )

            triggered = resolve_intrabar_exit(
                side=position.side,
                bar_high=bar.high,
                bar_low=bar.low,
                stop_price=position.stop_price,
                target_price=position.target_price,
                liquidation_price=position.liquidation_price,
                trail_price=trail_level,
            )

            if triggered is not None:
                close_position(
                    position,
                    raw_price=triggered[0],
                    reason=triggered[1],
                    exit_time=bar.open_time,
                    exit_index=index,
                )
                position = None
            else:
                position.trail_extreme = (
                    max(position.trail_extreme, bar.high)
                    if long
                    else min(position.trail_extreme, bar.low)
                )

        # --- 3. Mark to market at this bar's close ---
        if position is not None:
            bars_in_position += 1
            if position.side is PositionSide.LONG:
                unrealised = position.quantity * (bar.close - position.entry_price)
            else:
                unrealised = position.quantity * (position.entry_price - bar.close)
            unrealised = max(unrealised - position.entry_fee, -position.margin)
        else:
            unrealised = _ZERO

        marked = equity + unrealised
        peak = max(peak, marked)
        drawdown = (peak - marked) / peak * _HUNDRED if peak > _ZERO else _ZERO
        curve.append(
            EquityPoint(
                time=bar.open_time,
                equity=_q(marked),
                drawdown_percent=_q(max(drawdown, _ZERO), _PCT),
                in_position=position is not None,
            )
        )

        # --- 4. Decide from this bar, for the next one ---
        if index < len(candles) - 1 and index >= warmup:
            desired = _desired_side(biases[index], settings)
            current = position.side if position is not None else PositionSide.FLAT
            if desired is not current:
                pending_side = desired

    open_at_end = 0
    if position is not None:
        last = candles[-1]
        close_position(
            position,
            raw_price=last.close,
            reason=ExitReason.END_OF_DATA,
            exit_time=last.open_time,
            exit_index=len(candles) - 1,
        )
        open_at_end = 1

    metrics = compute_metrics(
        trades=trades,
        curve=curve,
        config=settings,
        timeframe=timeframe,
        bars_in_position=bars_in_position,
        trades_open_at_end=open_at_end,
    )

    warnings: list[str] = []
    if metrics.total_trades == 0:
        warnings.append(
            "No trades were taken. The rules never aligned over this window; the "
            "metrics describe an untouched balance, not a strategy."
        )
    elif metrics.total_trades < MIN_TRADES_FOR_MEANINGFUL_METRICS:
        warnings.append(
            f"Only {metrics.total_trades} trades. Below about "
            f"{MIN_TRADES_FOR_MEANINGFUL_METRICS} the win rate and profit factor "
            "describe a handful of coincidences rather than a strategy."
        )
    if open_at_end:
        warnings.append(
            "A position was still open when the data ended; it was closed at the last "
            "bar's close and is marked END_OF_DATA. That trade did not exit on a rule."
        )
    if settings.leverage > _ONE:
        warnings.append(
            f"Leverage {settings.leverage}x is a simulation input only. Liquidation is "
            "modelled optimistically, so leveraged results here are better than reality."
        )

    return BacktestResult(
        **base,  # type: ignore[arg-type]
        status=BacktestStatus.COMPLETED,
        metrics=metrics,
        trades=tuple(trades),
        equity_curve=_downsample(curve),
        first_bar_time=candles[0].open_time,
        last_bar_time=candles[-1].open_time,
        warnings=tuple(warnings),
    )


def _downsample(curve: Sequence[EquityPoint]) -> tuple[EquityPoint, ...]:
    """Thin the equity curve for transport, keeping the last point.

    Evenly spaced sampling, so the shape survives. The metrics are computed
    from the *full* curve before this runs -- max drawdown must never be
    measured on a thinned series, because the trough may be exactly what got
    dropped.
    """
    if len(curve) <= MAX_EQUITY_POINTS:
        return tuple(curve)
    step = len(curve) / MAX_EQUITY_POINTS
    sampled = [curve[int(index * step)] for index in range(MAX_EQUITY_POINTS)]
    if sampled[-1] is not curve[-1]:
        sampled[-1] = curve[-1]
    return tuple(sampled)
