"""Backtest performance metrics.

Pure arithmetic over a completed run. Two rules govern everything here:

**An undefined statistic is ``None``, not a sentinel.** A profit factor with no
losing trades is not "infinity" and not a large number; a Sharpe-like ratio over
a perfectly flat equity curve is not zero. Substituting a value would put a
number on a chart that no computation produced.

**Drawdown is measured on the full curve.** The engine thins the curve for
transport *after* these run, because the trough of a drawdown is exactly the
point a sampler is likely to drop.
"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal
from itertools import pairwise
from typing import Final

from aetheris.domain.backtest import BacktestConfig, BacktestMetrics, EquityPoint, Trade
from aetheris.domain.enums import Timeframe

__all__ = ["compute_metrics"]

_ZERO: Final = Decimal(0)
_HUNDRED: Final = Decimal(100)
_MONEY: Final = Decimal("0.00000001")
_PCT: Final = Decimal("0.0001")
_RATIO: Final = Decimal("0.0001")

_SECONDS_PER_YEAR: Final = Decimal(365 * 24 * 3600)


def _q(value: Decimal, exponent: Decimal = _MONEY) -> Decimal:
    return value.quantize(exponent)


def _mean(values: Sequence[Decimal]) -> Decimal:
    return sum(values, _ZERO) / Decimal(len(values))


def _sharpe_like(curve: Sequence[EquityPoint], timeframe: Timeframe) -> Decimal | None:
    """Annualised mean-over-standard-deviation of per-bar returns.

    Called *Sharpe-like* rather than Sharpe because it takes the risk-free rate
    as zero and uses per-bar equity returns rather than a return series over a
    fixed calendar period. It is a dispersion-adjusted comparison number, not
    the textbook ratio, and naming it precisely is cheaper than a footnote
    nobody reads.

    ``None`` when there is no dispersion to divide by -- a flat curve has no
    defined ratio, and reporting 0 would read as "risk-adjusted return of zero"
    rather than "not applicable".
    """
    if len(curve) < 3:
        return None
    returns: list[Decimal] = []
    for previous, current in pairwise(curve):
        if previous.equity <= _ZERO:
            return None
        returns.append((current.equity - previous.equity) / previous.equity)
    if not returns:
        return None

    mean = _mean(returns)
    variance = _mean([(value - mean) ** 2 for value in returns])
    if variance <= _ZERO:
        return None
    deviation = variance.sqrt()

    bars_per_year = _SECONDS_PER_YEAR / Decimal(timeframe.seconds)
    return (mean / deviation) * bars_per_year.sqrt()


def compute_metrics(
    *,
    trades: Sequence[Trade],
    curve: Sequence[EquityPoint],
    config: BacktestConfig,
    timeframe: Timeframe,
    bars_in_position: int,
    trades_open_at_end: int,
) -> BacktestMetrics:
    """Summarise a completed simulation."""
    wins = [trade for trade in trades if trade.net_pnl > _ZERO]
    losses = [trade for trade in trades if trade.net_pnl < _ZERO]
    breakeven = len(trades) - len(wins) - len(losses)

    gross_profit = sum((trade.net_pnl for trade in wins), _ZERO)
    gross_loss = -sum((trade.net_pnl for trade in losses), _ZERO)
    net_pnl = sum((trade.net_pnl for trade in trades), _ZERO)
    total_fees = sum((trade.fees for trade in trades), _ZERO)

    ending = curve[-1].equity if curve else config.starting_balance
    starting = config.starting_balance

    max_drawdown_percent = max((point.drawdown_percent for point in curve), default=_ZERO)
    peak = starting
    max_drawdown_absolute = _ZERO
    for point in curve:
        peak = max(peak, point.equity)
        max_drawdown_absolute = max(max_drawdown_absolute, peak - point.equity)

    return BacktestMetrics(
        total_trades=len(trades),
        winning_trades=len(wins),
        losing_trades=len(losses),
        breakeven_trades=breakeven,
        win_rate_percent=(
            _q(Decimal(len(wins)) / Decimal(len(trades)) * _HUNDRED, _PCT) if trades else None
        ),
        net_pnl=_q(net_pnl),
        gross_profit=_q(gross_profit),
        gross_loss=_q(gross_loss),
        total_fees=_q(total_fees),
        return_percent=_q((ending - starting) / starting * _HUNDRED, _PCT),
        # No losses means the ratio has no denominator. Not infinity, not a
        # large number -- undefined, and said so.
        profit_factor=_q(gross_profit / gross_loss, _RATIO) if gross_loss > _ZERO else None,
        average_trade=_q(net_pnl / Decimal(len(trades))) if trades else None,
        average_win=_q(gross_profit / Decimal(len(wins))) if wins else None,
        average_loss=_q(-gross_loss / Decimal(len(losses))) if losses else None,
        largest_win=_q(max(trade.net_pnl for trade in wins)) if wins else None,
        largest_loss=_q(min(trade.net_pnl for trade in losses)) if losses else None,
        max_drawdown_percent=_q(max_drawdown_percent, _PCT),
        max_drawdown_absolute=_q(max_drawdown_absolute),
        sharpe_like_ratio=(
            _q(value, _RATIO) if (value := _sharpe_like(curve, timeframe)) is not None else None
        ),
        exposure_percent=(
            _q(Decimal(bars_in_position) / Decimal(len(curve)) * _HUNDRED, _PCT) if curve else _ZERO
        ),
        starting_balance=_q(starting),
        ending_balance=_q(ending),
        bars_tested=len(curve),
        trades_open_at_end=trades_open_at_end,
    )
