"""The risk envelope, and the views the engine rules on.

Pure data. The engine is a function over these -- no I/O, no clock of its own,
no settings import -- which is what lets a test construct a locked account with
a stale price and a 400x request in four lines and assert the exact refusal.

Every field here is **measured or configured**. Nothing is inferred, and
nothing derived from a strategy's opinion enters the risk decision: condition
counts and bias are carried on the proposal for the audit trail only, and the
engine never reads them. A rule that let a strategy's own reading loosen the
limit it is being judged against would not be a limit.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from aetheris.analysis.volatility import VolatilityStatus
from aetheris.core.errors import RiskRejectionCode
from aetheris.core.freshness import DataStatus
from aetheris.domain.enums import OrderSide
from aetheris.domain.leverage import LeverageDecision
from aetheris.domain.market import SymbolFilters
from aetheris.domain.paper import RiskLockState

__all__ = [
    "RiskAccountView",
    "RiskMarketView",
    "RiskPolicy",
    "RiskProposal",
    "RiskVerdict",
]


@dataclass(frozen=True, slots=True)
class RiskPolicy:
    """The configured envelope. Identical whichever mode it guards."""

    max_open_positions: int
    max_position_notional: Decimal
    max_portfolio_exposure: Decimal
    max_leverage: Decimal
    max_data_age_seconds: float
    entry_cooldown_seconds: float
    #: Measured ATR as a percent of price. A threshold on an observation, never
    #: a forecast of what volatility will do next.
    max_atr_percent: Decimal


@dataclass(frozen=True, slots=True)
class RiskProposal:
    """What something wants to do. A request, never a permission."""

    symbol: str
    side: OrderSide
    requested_margin: Decimal
    requested_leverage: Decimal
    stop_loss_percent: Decimal | None = None
    take_profit_percent: Decimal | None = None
    trailing_stop_percent: Decimal | None = None

    #: Carried for the audit trail and **never read by the engine**. Recorded
    #: so a refusal can be traced back to what proposed it.
    origin: str = "unspecified"
    bias: str | None = None
    conditions_met: int | None = None
    conditions_total: int | None = None


@dataclass(frozen=True, slots=True)
class RiskAccountView:
    """The account as the engine sees it. A snapshot, not a live reference."""

    balance: Decimal
    available_balance: Decimal
    equity: Decimal
    margin_used: Decimal
    open_symbols: frozenset[str]
    open_position_count: int
    total_notional: Decimal

    session_realized_pnl: Decimal
    daily_profit_target: Decimal
    daily_loss_limit: Decimal
    lock_state: RiskLockState
    lock_reason: str | None = None

    emergency_stopped: bool = False
    emergency_reason: str | None = None
    mode_enabled: bool = True
    #: Orders whose fate this system does not know -- UNKNOWN or RECONCILING.
    #: Any at all blocks new entries: there may be a position at a venue that
    #: this system cannot see, and sizing the next order against that is how a
    #: small outage becomes a large loss. Zero for paper, which has no venue to
    #: be out of step with.
    unreconciled_orders: int = 0
    unreconciled_detail: str | None = None
    #: When this symbol was last entered, for the cooldown. None means never.
    last_entry_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class RiskMarketView:
    """What is actually known about the instrument right now."""

    symbol: str
    status: DataStatus
    age_seconds: float | None
    last_price: Decimal | None
    #: Measured over closed bars. None when it could not be computed -- which
    #: is a refusal, not a zero.
    atr_percent: Decimal | None = None
    #: *Why* there is no ATR, when there is none. Carried separately so the
    #: refusal can name the actual condition: too little history reads very
    #: differently from a stale feed, and collapsing both into STALE_DATA tells
    #: a user to wait for fresher data that will never help.
    volatility_status: VolatilityStatus = VolatilityStatus.MEASURED
    volatility_detail: str | None = None
    filters: SymbolFilters | None = None
    #: The venue's real per-symbol ceiling. **Null in this build**, because
    #: leverage brackets are served only from an authenticated endpoint. Never
    #: guessed, and never assumed to be the top of the candidate range.
    exchange_max_leverage: Decimal | None = None


@dataclass(frozen=True, slots=True)
class RiskVerdict:
    """The engine's answer. Approved with a size, or refused with a code.

    There is no third outcome and no partial approval a caller could
    reinterpret. ``approved`` and ``code`` cannot both be set.
    """

    approved: bool
    detail: str
    leverage: LeverageDecision
    code: RiskRejectionCode | None = None
    #: Present only on an approval. What may actually be committed, which may
    #: be less than was requested.
    approved_margin: Decimal | None = None
    approved_quantity: Decimal | None = None
    approved_notional: Decimal | None = None
    #: The ceiling the engine derived, reported whether or not it bound.
    risk_max_leverage: Decimal | None = None
    #: Named checks performed, in order. An audit trail that records only the
    #: failing check cannot show what passed before it.
    checks_performed: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if self.approved and self.code is not None:
            raise ValueError("an approved verdict must not carry a rejection code")
        if not self.approved and self.code is None:
            raise ValueError("a refusal must name a rejection code")
        if self.approved and self.approved_margin is None:
            raise ValueError("an approved verdict must carry an approved margin")
