"""Mutable paper state.

Separated from the frozen domain models on purpose: the API returns immutable
snapshots, while the engine works against a mutable container. Keeping the two
apart means a caller cannot accidentally hold a reference that changes under
them, and the snapshot boundary is the obvious place to add persistence later.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal

from aetheris.domain.paper import (
    DailySession,
    PaperOrder,
    PaperTrade,
    RiskLockState,
)
from aetheris.domain.thesis import PositionThesis


@dataclass(slots=True)
class MutablePosition:
    """An open position while the engine is working on it."""

    position_id: str
    symbol: str
    side: str
    quantity: Decimal
    entry_price: Decimal
    notional: Decimal
    margin: Decimal
    approved_leverage: Decimal
    entry_fee: Decimal
    opened_at: datetime
    updated_at: datetime
    opening_order_id: str
    stop_price: Decimal | None = None
    target_price: Decimal | None = None
    trailing_stop_percent: Decimal | None = None
    trail_extreme: Decimal | None = None
    liquidation_price: Decimal | None = None
    mark_price: Decimal | None = None
    mark_source: str | None = None
    mark_status: str | None = None

    #: Why this position was opened, recorded once at entry and never
    #: updated. Optional only so a snapshot written before schema 2 can
    #: still be loaded by the code path that refuses it; every position
    #: created by this build has one.
    thesis: PositionThesis | None = None

    #: Excursion extremes, accumulated from observed marks while the
    #: position is open. Never back-derived: a value that was not seen
    #: while the position was live is not an excursion, and computing one
    #: from later candles would be hindsight.
    best_price: Decimal | None = None
    worst_price: Decimal | None = None


@dataclass(slots=True)
class MutableSession:
    session_date: date
    profit_target: Decimal
    loss_limit: Decimal
    realized_pnl: Decimal = Decimal(0)
    fees: Decimal = Decimal(0)
    trades_closed: int = 0
    orders_submitted: int = 0
    orders_rejected: int = 0
    lock_state: RiskLockState = RiskLockState.NONE
    lock_reason: str | None = None
    locked_at: datetime | None = None

    def to_model(self) -> DailySession:
        return DailySession(
            session_date=self.session_date,
            realized_pnl=self.realized_pnl,
            fees=self.fees,
            trades_closed=self.trades_closed,
            orders_submitted=self.orders_submitted,
            orders_rejected=self.orders_rejected,
            profit_target=self.profit_target,
            loss_limit=self.loss_limit,
            lock_state=self.lock_state,
            lock_reason=self.lock_reason,
            locked_at=self.locked_at,
        )


@dataclass(slots=True)
class PaperState:
    """Everything one paper account knows."""

    account_id: str
    created_at: datetime
    updated_at: datetime
    starting_balance: Decimal
    balance: Decimal
    realized_pnl: Decimal = Decimal(0)
    total_fees: Decimal = Decimal(0)
    autonomous_enabled: bool = False
    emergency_stopped: bool = False
    emergency_reason: str | None = None

    positions: dict[str, MutablePosition] = field(default_factory=dict)
    #: Keyed by client_order_id so a resubmission is an O(1) idempotency check.
    orders_by_client_id: dict[str, PaperOrder] = field(default_factory=dict)
    order_log: list[PaperOrder] = field(default_factory=list)
    trades: list[PaperTrade] = field(default_factory=list)
    session: MutableSession | None = None
    sequence: int = 0

    def next_sequence(self) -> int:
        """Monotonic counter behind deterministic order and fill identifiers."""
        self.sequence += 1
        return self.sequence
