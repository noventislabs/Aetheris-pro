"""Order lifecycle domain model.

**No venue is reachable from here, and none is named.** This is the shape an
order has while this system is responsible for it -- before submission, during
the window where nobody knows whether it landed, and after a venue has said
what happened. Phase 8a builds the lifecycle; phase 8c gives it something to
submit to.

The model exists to survive one scenario, and every decision in it points at
that scenario::

    order record written      <- a crash here is safe
    venue submission          <- a crash HERE is the problem
    venue response            <- a lost response is the same problem

Between the submission and the response, **local state cannot tell you whether
an order exists in the world.** So the record is written first, carries an
identity the venue will echo back, and is allowed to say ``UNKNOWN`` for as
long as that is the truth. ``UNKNOWN`` is not a failure and not an error -- it
is the only honest answer available, and the one thing this model must never do
is replace it with a guess.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator

from aetheris.core.errors import RiskRejectionCode
from aetheris.domain.enums import OrderSide, OrderState, OrderType, TradingMode
from aetheris.domain.venue import MarginMode


class OrderOrigin(StrEnum):
    """What asked for this order.

    Recorded for the audit trail. It confers no authority: the risk engine
    rules identically whichever of these proposed the order.
    """

    MANUAL = "MANUAL"
    AUTONOMOUS = "AUTONOMOUS"
    RECOVERY = "RECOVERY"


class OrderIntent(BaseModel):
    """What we mean to do, fixed before anything is sent.

    Frozen and written to the store *before* submission, so a crash in the
    uncertainty window leaves behind something to reconcile against. An intent
    without a durable record is an order nobody can ask about.
    """

    model_config = ConfigDict(frozen=True)

    account_id: str
    symbol: str
    side: OrderSide
    order_type: OrderType
    quantity: Decimal = Field(gt=0)
    #: None for a market order. Present and positive for anything resting.
    price: Decimal | None = Field(default=None, gt=0)
    reduce_only: bool = False
    #: Which mode this order belongs to. PAPER never reaches a venue; TESTNET
    #: does, from phase 8c. LIVE is not implemented and is refused upstream.
    mode: TradingMode = TradingMode.PAPER
    origin: OrderOrigin = OrderOrigin.MANUAL
    #: The stable, human-readable string the venue identity is derived from.
    #: Kept here because the venue field is a bounded hash and a reader should
    #: still be able to see what the order *was*.
    intent_key: str
    created_at: datetime


class OrderFill(BaseModel):
    """One execution against an order."""

    model_config = ConfigDict(frozen=True)

    fill_id: str
    price: Decimal = Field(gt=0)
    quantity: Decimal = Field(gt=0)
    fee: Decimal = Field(default=Decimal(0), ge=0)
    filled_at: datetime
    #: The venue's own identifier for this execution, when it supplies one.
    #: Absent for a simulated fill.
    venue_trade_id: str | None = None


class OrderDiscrepancy(BaseModel):
    """Something the venue said that could not be applied.

    Recorded rather than applied, which is the whole point. A venue message
    about an order this system already considers terminal, or a fill count that
    went backwards, is evidence of a problem -- and silently applying it would
    destroy the evidence along with the correct state.
    """

    model_config = ConfigDict(frozen=True)

    observed_at: datetime
    detail: str
    local_state: OrderState
    venue_state: OrderState | None = None


class OrderRecord(BaseModel):
    """An order and everything known about it.

    ``state`` is advanced only through the transition table in
    ``engines.order.machine``; nothing here mutates it, because a model that
    lets a caller assign a state is a model in which a phantom fill is one
    assignment away.
    """

    model_config = ConfigDict(frozen=True)

    order_id: str
    #: Ours, deterministic, echoed by the venue. This is what recovery queries
    #: by, so it must be derivable again after a restart -- never generated.
    client_order_id: str
    #: Theirs. None until a venue has acknowledged the order, which is exactly
    #: the gap that makes the uncertainty window uncertain.
    venue_order_id: str | None = None

    intent: OrderIntent
    state: OrderState

    filled_quantity: Decimal = Field(default=Decimal(0), ge=0)
    average_fill_price: Decimal | None = None
    fills: tuple[OrderFill, ...] = ()

    created_at: datetime
    updated_at: datetime
    #: Stamped immediately *before* the submission leaves, not after it
    #: succeeds. A record with this set and no venue id is precisely an order
    #: whose fate is unknown.
    submitted_at: datetime | None = None
    terminal_at: datetime | None = None

    rejection_code: RiskRejectionCode | None = None
    rejection_detail: str | None = None
    #: What the venue said when it refused, in its own words.
    venue_rejection: str | None = None

    #: How many times reconciliation has asked about this order, and what it
    #: last learned. A count that keeps rising with no resolution is itself a
    #: signal worth surfacing.
    #: Exactly what the venue called this order's status, before mapping.
    #: EXPIRED_IN_MATCH becomes EXPIRED in the machine by decision, and this is
    #: where the distinction survives -- a state machine that branched on venue
    #: vocabulary would have the venue's vocabulary in it forever.
    venue_status_raw: str | None = None
    #: When the venue was last asked. Distinct from ``last_reconciled_at``,
    #: which records when a *decision* was applied: asking and learning nothing
    #: is still asking, and a rising poll count with no change is worth seeing.
    last_polled_at: datetime | None = None
    #: The leverage and margin mode the venue **confirmed** were in force
    #: before this order was allowed to leave. Recorded rather than assumed so
    #: a fill can be audited against what actually applied.
    venue_leverage: Decimal | None = Field(default=None, gt=0)
    venue_margin_mode: MarginMode | None = None

    reconciliation_attempts: int = Field(default=0, ge=0)
    last_reconciled_at: datetime | None = None
    reconciliation_detail: str | None = None
    discrepancies: tuple[OrderDiscrepancy, ...] = ()

    #: Set only when a human resolved an order the venue could not settle.
    resolved_by_operator: str | None = None
    operator_reason: str | None = None

    @property
    def is_terminal(self) -> bool:
        return self.state.is_terminal

    @property
    def is_unreconciled(self) -> bool:
        """Whether this order blocks new entries.

        ``UNKNOWN`` and ``RECONCILING`` both mean the same thing for trading
        purposes: there may be a position in the world that this system cannot
        see. Sizing the next order against that is how a small outage becomes a
        large loss.
        """
        return self.state in (OrderState.UNKNOWN, OrderState.RECONCILING)

    @property
    def reached_venue(self) -> bool:
        """Whether this order was ever sent.

        The distinction that makes a "not found" answer safe to act on: an
        order that never left cannot exist at the venue, so its absence is
        proof. An order that did leave and is now absent is not proof of
        anything.
        """
        return self.submitted_at is not None

    @property
    def remaining_quantity(self) -> Decimal:
        return max(self.intent.quantity - self.filled_quantity, Decimal(0))

    @model_validator(mode="after")
    def _fills_agree_with_filled_quantity(self) -> OrderRecord:
        if self.fills:
            total = sum((fill.quantity for fill in self.fills), Decimal(0))
            if total != self.filled_quantity:
                raise ValueError(
                    f"filled_quantity {self.filled_quantity} does not match the sum of "
                    f"fills {total}; a record whose parts disagree cannot be reconciled"
                )
        return self


class VenueOrderView(BaseModel):
    """What a venue says about an order, normalised.

    Constructed by an adapter in phase 8c. It exists in 8a so the
    reconciliation protocol can be written and tested against every answer a
    venue can give, long before one is connected.
    """

    model_config = ConfigDict(frozen=True)

    client_order_id: str
    venue_order_id: str | None = None
    state: OrderState
    filled_quantity: Decimal = Field(default=Decimal(0), ge=0)
    average_fill_price: Decimal | None = None
    observed_at: datetime
    venue_rejection: str | None = None


class ReconciliationAction(StrEnum):
    """What reconciliation concluded. One per possible venue answer."""

    #: The venue answered and is the authority. Adopt what it said.
    ADOPT_VENUE_STATE = "ADOPT_VENUE_STATE"
    #: The venue has no such order and this one never left. Absence is proof
    #: here, and only here.
    RESOLVE_NEVER_SUBMITTED = "RESOLVE_NEVER_SUBMITTED"
    #: The venue has no such order but this one was sent. Absence is not proof:
    #: the order may exist and the query may be wrong. Stay unknown.
    REMAIN_UNKNOWN = "REMAIN_UNKNOWN"
    #: The venue said something that cannot be applied. Record it; change
    #: nothing.
    RECORD_DISCREPANCY = "RECORD_DISCREPANCY"
    #: No answer was available. Not knowing is not the same as nothing being
    #: there.
    VENUE_UNREACHABLE = "VENUE_UNREACHABLE"


class ReconciliationDecision(BaseModel):
    """The outcome of asking about one order. Pure data, applied separately."""

    model_config = ConfigDict(frozen=True)

    action: ReconciliationAction
    #: The state to move to, when the action implies one. ``None`` means the
    #: order stays where it is.
    target_state: OrderState | None = None
    detail: str
    discrepancy: str | None = None
    venue_order_id: str | None = None
    filled_quantity: Decimal | None = None
    #: The venue's own average. Carried alongside the quantity because the two
    #: must move together: adopting a venue total while keeping a locally
    #: computed average produces a record whose implied notional is a number
    #: nothing observed. ``None`` when the venue gave a quantity but no
    #: average, which is honest -- unknown, rather than stale.
    average_fill_price: Decimal | None = None

    @property
    def resolves(self) -> bool:
        """Whether this decision settles the order's fate."""
        return self.target_state is not None and self.target_state.is_terminal


class OrderBookkeeping(BaseModel):
    """A summary of what the order engine is holding.

    Exposed so the risk engine can ask one question -- "is anything
    unreconciled?" -- without reaching into the store.
    """

    model_config = ConfigDict(frozen=True)

    total: int = Field(default=0, ge=0)
    open_orders: int = Field(default=0, ge=0)
    unreconciled: int = Field(default=0, ge=0)
    terminal: int = Field(default=0, ge=0)
    discrepancies: int = Field(default=0, ge=0)

    @property
    def blocks_entry(self) -> bool:
        return self.unreconciled > 0
