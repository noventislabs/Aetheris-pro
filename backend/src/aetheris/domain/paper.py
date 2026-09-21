"""Paper trading domain model.

**Simulation only.** Nothing in this module reaches an exchange. A paper order
is a record in this process's memory; a paper fill is arithmetic against a real
observed price. No venue is told anything, no credential exists, and no real
funds move.

The model is deliberately shaped like the real execution model the later phases
will need -- orders carry a lifecycle state, fills are a list rather than a
single price, positions track margin and liquidation -- so that the durable,
venue-backed implementation replaces the *mechanics* without reshaping the
domain. Where the simulation is simpler than reality, the gap is named here
rather than glossed over.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from aetheris.core.errors import RiskRejectionCode
from aetheris.domain.enums import OrderSide, OrderState, OrderType, PositionSide, TradingMode
from aetheris.domain.leverage import LeverageDecision


class PaperExitReason(StrEnum):
    """Why a paper position closed."""

    MANUAL_CLOSE = "MANUAL_CLOSE"
    STOP_LOSS = "STOP_LOSS"
    TAKE_PROFIT = "TAKE_PROFIT"
    TRAILING_STOP = "TRAILING_STOP"
    LIQUIDATION = "LIQUIDATION"
    #: The strategy's bias stopped supporting the position (autonomous mode).
    SIGNAL_FLIP = "SIGNAL_FLIP"
    ACCOUNT_RESET = "ACCOUNT_RESET"


class RiskLockState(StrEnum):
    """Whether new entries are permitted, and why not.

    A lock stops *opening*. It never closes an existing position on its own:
    force-closing on a daily limit is a separate policy decision, and doing it
    implicitly would turn a risk brake into a market order nobody asked for.
    """

    NONE = "NONE"
    DAILY_PROFIT_TARGET = "DAILY_PROFIT_TARGET"
    DAILY_LOSS_LIMIT = "DAILY_LOSS_LIMIT"
    EMERGENCY_STOP = "EMERGENCY_STOP"

    @property
    def blocks_entries(self) -> bool:
        return self is not RiskLockState.NONE


class Durability(StrEnum):
    """How long paper state survives.

    Reported on every account response so a reader is never left to assume.
    """

    #: Held in this process only. Lost on restart, and said so.
    IN_MEMORY = "IN_MEMORY"
    DURABLE = "DURABLE"


class PaperFill(BaseModel):
    """One execution against a paper order.

    A list rather than a single price because the domain must be
    partial-fill-ready even though this phase fills all-or-nothing.
    """

    model_config = ConfigDict(frozen=True)

    fill_id: str
    price: Decimal = Field(gt=0)
    quantity: Decimal = Field(gt=0)
    fee: Decimal = Field(ge=0)
    filled_at: datetime
    #: Provenance of the price this fill used. A paper fill is only as real as
    #: the market data behind it, so that data is named on the fill.
    price_source: str
    price_age_seconds: float | None = None


class PaperOrder(BaseModel):
    """A simulated order and its lifecycle."""

    model_config = ConfigDict(frozen=True)

    order_id: str
    #: Caller-supplied and unique per account. Resubmitting the same id returns
    #: the original order instead of creating a second one.
    client_order_id: str
    symbol: str
    side: OrderSide
    order_type: OrderType
    state: OrderState
    reduce_only: bool = False

    requested_quantity: Decimal = Field(gt=0)
    filled_quantity: Decimal = Field(default=Decimal(0), ge=0)
    average_fill_price: Decimal | None = None
    fills: tuple[PaperFill, ...] = ()

    leverage: LeverageDecision | None = None

    created_at: datetime
    updated_at: datetime
    rejection_code: RiskRejectionCode | None = None
    rejection_detail: str | None = None

    #: True when this response replayed an existing order rather than creating
    #: one, so a caller can tell a retry from a new submission.
    idempotent_replay: bool = False

    @property
    def is_filled(self) -> bool:
        return self.state is OrderState.FILLED


class PaperPosition(BaseModel):
    """An open simulated position, marked to a real observed price."""

    model_config = ConfigDict(frozen=True)

    position_id: str
    symbol: str
    side: PositionSide
    quantity: Decimal = Field(gt=0)
    entry_price: Decimal = Field(gt=0)
    notional: Decimal = Field(gt=0)
    margin: Decimal = Field(gt=0)
    approved_leverage: Decimal = Field(ge=1)
    entry_fee: Decimal = Field(ge=0)

    stop_price: Decimal | None = None
    target_price: Decimal | None = None
    trailing_stop_percent: Decimal | None = None
    #: Best price seen since entry, for the trailing stop.
    trail_extreme: Decimal | None = None
    liquidation_price: Decimal | None = None

    opened_at: datetime
    updated_at: datetime
    opening_order_id: str

    #: Mark price and its provenance. Null when market data was unusable at the
    #: last poll -- an unmarked position reports no unrealised PnL rather than
    #: a stale one.
    mark_price: Decimal | None = None
    mark_source: str | None = None
    mark_status: str | None = None
    unrealized_pnl: Decimal | None = None


class PaperTrade(BaseModel):
    """A completed simulated round trip."""

    model_config = ConfigDict(frozen=True)

    trade_id: str
    symbol: str
    side: PositionSide
    quantity: Decimal
    entry_price: Decimal
    exit_price: Decimal
    notional: Decimal
    margin: Decimal
    approved_leverage: Decimal
    opened_at: datetime
    closed_at: datetime
    exit_reason: PaperExitReason
    gross_pnl: Decimal
    fees: Decimal
    net_pnl: Decimal
    return_percent: Decimal
    balance_after: Decimal


class DailySession(BaseModel):
    """One UTC trading day's realised outcome and lock state.

    The daily limits are evaluated on **realised** PnL. Using unrealised would
    let an open position's fluctuation toggle the lock bar by bar, which is a
    brake that engages and releases on noise.
    """

    model_config = ConfigDict(frozen=True)

    session_date: date
    realized_pnl: Decimal = Decimal(0)
    fees: Decimal = Decimal(0)
    trades_closed: int = Field(default=0, ge=0)
    orders_submitted: int = Field(default=0, ge=0)
    orders_rejected: int = Field(default=0, ge=0)

    profit_target: Decimal
    loss_limit: Decimal
    lock_state: RiskLockState = RiskLockState.NONE
    lock_reason: str | None = None
    locked_at: datetime | None = None

    @property
    def target_reached(self) -> bool:
        return self.realized_pnl >= self.profit_target

    @property
    def loss_limit_reached(self) -> bool:
        return self.realized_pnl <= self.loss_limit


class PaperAccount(BaseModel):
    """The full paper account snapshot."""

    model_config = ConfigDict(frozen=True)

    account_id: str
    mode: TradingMode = TradingMode.PAPER
    created_at: datetime
    updated_at: datetime

    starting_balance: Decimal
    #: Realised cash. Unrealised PnL is not folded in here.
    balance: Decimal
    #: Balance plus unrealised PnL on marked positions.
    equity: Decimal
    #: Equity not posted as margin.
    available_balance: Decimal
    margin_used: Decimal

    realized_pnl: Decimal
    unrealized_pnl: Decimal | None = Field(
        default=None,
        description="Null when no open position could be marked to a usable price",
    )
    total_fees: Decimal

    positions: tuple[PaperPosition, ...] = ()
    open_orders: tuple[PaperOrder, ...] = ()
    recent_orders: tuple[PaperOrder, ...] = ()
    recent_trades: tuple[PaperTrade, ...] = ()
    session: DailySession

    autonomous_enabled: bool = False
    durability: Durability = Durability.IN_MEMORY
    #: Surfaced on every response, not buried in documentation.
    durability_notice: str = (
        "PAPER STATE: IN-MEMORY - RESETS ON RESTART. Balances, positions, orders and "
        "history exist only in this process and are lost when it stops. Durable "
        "persistence needs the database from phase 1."
    )

    #: Provenance of the marks used for this snapshot.
    mark_source: str | None = None
    mark_status: str | None = None
    mark_age_seconds: float | None = None

    label: str = "PAPER / SIMULATION ONLY / NO REAL ORDER"
    disclaimer: str = (
        "Paper trading is a simulation running against real public market data. No "
        "order is sent to any exchange, no API credential exists, and no real funds "
        "are involved. Fills, fees and PnL are computed locally and do not represent "
        "execution on a venue."
    )


class PaperOrderResult(BaseModel):
    """Outcome of submitting a paper order.

    Carries the account snapshot after the attempt so a caller never has to
    guess whether a rejection changed anything.
    """

    model_config = ConfigDict(frozen=True)

    accepted: bool
    order: PaperOrder
    position: PaperPosition | None = None
    trade: PaperTrade | None = None
    account: PaperAccount
    detail: str | None = None


class ReconciliationStatus(StrEnum):
    """Whether local state was checked against an authority."""

    #: There is no external authority to reconcile against: paper state is
    #: authoritative by definition. Reported rather than faked as "consistent".
    NOT_APPLICABLE = "NOT_APPLICABLE"
    CONSISTENT = "CONSISTENT"
    INCONSISTENT = "INCONSISTENT"
    UNAVAILABLE = "UNAVAILABLE"


class ReconciliationReport(BaseModel):
    """Result of a startup/recovery reconciliation pass.

    The hook exists now so the durable implementation slots in without
    reshaping callers. For in-memory paper state it always reports
    NOT_APPLICABLE, because there is nothing external to disagree with and
    claiming CONSISTENT would imply a check that did not happen.
    """

    model_config = ConfigDict(frozen=True)

    status: ReconciliationStatus
    detail: str
    checked_at: datetime
    positions_checked: int = Field(default=0, ge=0)
    orders_checked: int = Field(default=0, ge=0)
    discrepancies: tuple[str, ...] = ()
