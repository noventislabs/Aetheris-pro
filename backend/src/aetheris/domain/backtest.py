"""Backtest models.

Everything here describes a **historical simulation**. A backtest says what a
rule set would have done over bars that already happened, under the fill,
fee and slippage assumptions stated in its own configuration. It is not a
prediction, not an expected return, and not evidence that the rules will work
again.

That framing is carried in the data rather than left to the reader: every
result ships a disclaimer field and an explicit list of the assumptions the
simulation made, so a number cannot travel into a screenshot or a payload
without them.

Monetary values are ``Decimal`` throughout, for the same reason as everywhere
else in the system: a backtest's output is the input to position sizing.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator

from aetheris.domain.enums import PositionSide, Timeframe


class ExitReason(StrEnum):
    """Why a simulated position closed."""

    #: The strategy's bias stopped supporting the position.
    SIGNAL_FLIP = "SIGNAL_FLIP"
    STOP_LOSS = "STOP_LOSS"
    TAKE_PROFIT = "TAKE_PROFIT"
    TRAILING_STOP = "TRAILING_STOP"
    #: Loss reached the posted margin. A simplified model -- see the engine.
    LIQUIDATION = "LIQUIDATION"
    #: The data ran out while the position was open. These trades are reported
    #: separately because they did not exit on a rule.
    END_OF_DATA = "END_OF_DATA"


class BacktestStatus(StrEnum):
    COMPLETED = "COMPLETED"
    #: Not enough bars for the strategy to warm up, so nothing was simulated.
    INSUFFICIENT_DATA = "INSUFFICIENT_DATA"
    #: Candles could not be retrieved or were unusable.
    UNAVAILABLE = "UNAVAILABLE"
    ERROR = "ERROR"


class BacktestConfig(BaseModel):
    """Simulation parameters.

    ``leverage`` here is a **simulation input**, not an approval. It does not
    pass through the risk engine and grants nothing: it only scales the
    notional of hypothetical positions over historical bars. The live leverage
    chain (requested → exchange ceiling → risk ceiling → approved) is a
    separate mechanism and still approves nothing.
    """

    model_config = ConfigDict(frozen=True)

    starting_balance: Decimal = Field(default=Decimal("100"), gt=0, le=Decimal("10000000"))
    #: Fraction of equity posted as margin per position.
    position_size_percent: Decimal = Field(default=Decimal("10"), gt=0, le=100)
    #: Simulation-only notional multiplier. Bounded well below the 1-500x
    #: candidate domain: a backtest at 500x is a study of liquidation, not of
    #: a strategy, and offering it would invite reading the result as a plan.
    leverage: Decimal = Field(default=Decimal("1"), ge=1, le=25)

    #: Taker fee per side, in basis points of notional.
    fee_bps: Decimal = Field(default=Decimal("5"), ge=0, le=100)
    #: Adverse price movement applied to every fill, in basis points.
    slippage_bps: Decimal = Field(default=Decimal("2"), ge=0, le=100)

    stop_loss_percent: Decimal | None = Field(default=Decimal("2"), gt=0, le=90)
    take_profit_percent: Decimal | None = Field(default=Decimal("4"), gt=0, le=1000)
    trailing_stop_percent: Decimal | None = Field(default=None, gt=0, le=90)

    allow_long: bool = True
    allow_short: bool = True

    @model_validator(mode="after")
    def _at_least_one_direction(self) -> BacktestConfig:
        if not self.allow_long and not self.allow_short:
            raise ValueError("at least one of allow_long or allow_short must be enabled")
        return self


class Trade(BaseModel):
    """One completed simulated round trip."""

    model_config = ConfigDict(frozen=True)

    side: PositionSide
    entry_time: datetime
    exit_time: datetime
    entry_price: Decimal
    exit_price: Decimal
    quantity: Decimal
    notional: Decimal
    margin: Decimal
    leverage: Decimal
    exit_reason: ExitReason
    bars_held: int = Field(ge=0)

    gross_pnl: Decimal
    fees: Decimal
    net_pnl: Decimal
    return_percent: Decimal = Field(description="Net PnL as a percentage of margin posted")
    equity_after: Decimal

    #: Worst and best unrealised excursion while open, from bar extremes.
    max_adverse_excursion_percent: Decimal | None = None
    max_favourable_excursion_percent: Decimal | None = None

    @property
    def is_win(self) -> bool:
        return self.net_pnl > 0


class EquityPoint(BaseModel):
    """Mark-to-market equity at one bar's close."""

    model_config = ConfigDict(frozen=True)

    time: datetime
    equity: Decimal
    drawdown_percent: Decimal = Field(ge=0, description="Below the running peak")
    in_position: bool


class BacktestMetrics(BaseModel):
    """Performance of the simulation. Historical, not predictive.

    Values that have no defined answer are ``None`` rather than a sentinel:
    a profit factor with no losing trades is not "infinity", and a Sharpe-like
    ratio over a flat equity curve is not zero.
    """

    model_config = ConfigDict(frozen=True)

    total_trades: int = Field(ge=0)
    winning_trades: int = Field(ge=0)
    losing_trades: int = Field(ge=0)
    breakeven_trades: int = Field(ge=0)
    win_rate_percent: Decimal | None = None

    net_pnl: Decimal
    gross_profit: Decimal
    gross_loss: Decimal = Field(description="Positive magnitude of losing trades")
    total_fees: Decimal
    return_percent: Decimal

    profit_factor: Decimal | None = Field(
        default=None, description="Gross profit over gross loss; None when there are no losses"
    )
    average_trade: Decimal | None = None
    average_win: Decimal | None = None
    average_loss: Decimal | None = None
    largest_win: Decimal | None = None
    largest_loss: Decimal | None = None

    max_drawdown_percent: Decimal = Field(ge=0)
    max_drawdown_absolute: Decimal = Field(ge=0)

    #: Standard deviation of per-bar returns, scaled by the bars in a year for
    #: the timeframe. Assumes a zero risk-free rate; see the engine notes.
    sharpe_like_ratio: Decimal | None = None
    exposure_percent: Decimal = Field(ge=0, le=100)

    starting_balance: Decimal
    ending_balance: Decimal
    bars_tested: int = Field(ge=0)
    trades_open_at_end: int = Field(ge=0)


class BacktestResult(BaseModel):
    """A complete simulation run."""

    model_config = ConfigDict(frozen=True)

    status: BacktestStatus
    detail: str | None = None

    symbol: str
    timeframe: Timeframe
    strategy: str
    strategy_version: str
    config: BacktestConfig

    metrics: BacktestMetrics | None = None
    trades: tuple[Trade, ...] = ()
    equity_curve: tuple[EquityPoint, ...] = ()

    first_bar_time: datetime | None = None
    last_bar_time: datetime | None = None
    source: str | None = None
    data_status: str | None = None
    ran_at: datetime | None = None

    #: Things a reader must know before believing a number -- too few trades,
    #: an open position at the end, and so on.
    warnings: tuple[str, ...] = ()
    #: What the simulation assumed. Reported so results are reproducible and
    #: their limits are visible without reading the source.
    assumptions: tuple[str, ...] = ()

    label: str = "HISTORICAL SIMULATION"
    disclaimer: str = (
        "Historical simulation over past candles under the stated assumptions. It is "
        "NOT a prediction, an expected return, a probability of profit, or evidence "
        "that these rules will work in future. Past results do not imply future "
        "results. No order was placed and no leverage was set by this system."
    )
