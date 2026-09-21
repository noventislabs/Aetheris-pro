"""Paper trading endpoints.

**These are the first non-GET routes in the system**, and that deserves an
explanation rather than a quiet commit.

Every earlier phase kept a flat invariant -- no route uses a method other than
GET -- because nothing in a read-only build had any business mutating server
state. Submitting a paper order genuinely does mutate it. Modelling that as a
GET would make a state change cacheable, prefetchable and repeatable by a
browser doing what browsers do, which is a worse property than the one being
protected.

So the invariant is replaced by a stronger and more precise pair, both asserted
in ``tests/unit/test_architecture.py``:

1. **No route in the system can reach a venue order endpoint.** There is still
   no implementation of ``TradingPort`` anywhere, and no Binance order path is
   named in the package.
2. **Writes exist only under ``/paper``**, and every one of them acts on
   in-memory simulation state.

What these routes cannot do is worth stating plainly: they place no order
anywhere. Every one of them acts on in-memory simulation state and reaches no
venue, holds no credential, and cannot be made to.

Phase 8b added a **separate** namespace, ``/testnet``, which does reach a venue
-- the Binance futures testnet, and only that. It is a different router, a
different service and a different adapter; nothing here can reach it, and no
flag promotes a paper order into one. Live order placement still does not
exist, and no path turns it on.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Body, Path, Query
from pydantic import BaseModel, ConfigDict, Field

from aetheris.api.deps import AutonomousDep, PaperDep
from aetheris.core.errors import RiskRejectionCode
from aetheris.domain.autonomous import AutonomousDecision, AutonomousStatus
from aetheris.domain.enums import OrderSide
from aetheris.domain.leverage import LEVERAGE_MAX, LEVERAGE_MIN
from aetheris.domain.paper import (
    PaperAccount,
    PaperOrderResult,
    PaperTrade,
    ReconciliationReport,
)
from aetheris.engines.paper.engine import ASSUMPTIONS, SubmitOrderRequest

router = APIRouter(prefix="/paper", tags=["paper"])

SymbolPath = Annotated[
    str,
    Path(
        min_length=1,
        max_length=32,
        pattern=r"^[A-Za-z0-9_]+$",
        description="Instrument symbol, e.g. BTCUSDT",
        examples=["BTCUSDT"],
    ),
]


class SubmitOrderBody(BaseModel):
    """A paper order request.

    Exactly one of ``quantity`` or ``margin``. ``leverage`` is a *request*: it
    goes through the same constraint chain as every other leverage question in
    this system and is rejected unless the chain can approve it.
    """

    model_config = ConfigDict(extra="forbid")

    symbol: str = Field(min_length=1, max_length=32, pattern=r"^[A-Za-z0-9_]+$")
    side: OrderSide
    quantity: Decimal | None = Field(default=None, gt=0)
    margin: Decimal | None = Field(
        default=None, gt=0, description="USDT to commit as margin for this position"
    )
    leverage: Decimal = Field(
        default=LEVERAGE_MIN,
        ge=LEVERAGE_MIN,
        le=LEVERAGE_MAX,
        description=(
            "Requested leverage, 1-500. A request, never an authorisation: the "
            "constraint chain rules on it and the risk engine has final authority. "
            "Anything above 1x is refused in this build because the venue's "
            "per-symbol ceiling is unknown and the risk engine is not built."
        ),
    )
    stop_loss_percent: Decimal | None = Field(default=None, gt=0, lt=100)
    take_profit_percent: Decimal | None = Field(default=None, gt=0, lt=100)
    trailing_stop_percent: Decimal | None = Field(default=None, gt=0, lt=100)
    client_order_id: str | None = Field(
        default=None,
        max_length=64,
        pattern=r"^[A-Za-z0-9_.:-]+$",
        description=(
            "Caller-supplied idempotency key. Resubmitting one returns the original "
            "outcome instead of opening a second position."
        ),
    )

    def to_request(self) -> SubmitOrderRequest:
        return SubmitOrderRequest(
            symbol=self.symbol.upper(),
            side=self.side,
            quantity=self.quantity,
            margin=self.margin,
            stop_loss_percent=self.stop_loss_percent,
            take_profit_percent=self.take_profit_percent,
            trailing_stop_percent=self.trailing_stop_percent,
            client_order_id=self.client_order_id,
        )


class TickResponse(BaseModel):
    """The account after a management pass, and what that pass closed."""

    account: PaperAccount
    closed_trades: tuple[PaperTrade, ...] = ()
    detail: str


class EmergencyStopBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    engaged: bool
    reason: str = Field(default="Engaged from the terminal", max_length=200)


class ResetBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    starting_balance: Decimal | None = Field(
        default=None,
        gt=0,
        le=1_000_000,
        description="Opening balance for the new account. Defaults to the configured one.",
    )


#: A module-level singleton, because a default argument must not be a call.
DEFAULT_RESET = ResetBody()


class PaperMethodResponse(BaseModel):
    """How the simulation fills, and what it does not model."""

    label: str
    mode: str
    assumptions: list[str]
    not_modelled: list[str]
    rejection_codes: list[str]
    durability: str
    durability_notice: str
    disclaimer: str


# ----------------------------------------------------------------------
# Reads
# ----------------------------------------------------------------------


@router.get("/method", response_model=PaperMethodResponse, summary="How paper fills work")
async def method() -> PaperMethodResponse:
    """Publish the fill model, the gaps in it, and the refusal vocabulary.

    Served from the API rather than documentation so a client can show the
    caveats beside the numbers instead of linking to a page nobody opens.
    """
    return PaperMethodResponse(
        label="PAPER / SIMULATION ONLY / NO REAL ORDER",
        mode="PAPER",
        assumptions=list(ASSUMPTIONS),
        not_modelled=[
            "Funding payments on perpetual positions",
            "Partial fills, order-book depth and queue position",
            "Maker rebates and fee tiers",
            "Borrow costs",
            "Exchange downtime, outages and venue-side order rejection",
            "Maintenance-margin liquidation tiers (a simpler model is used)",
            "Continuous stop monitoring (management is poll-driven)",
        ],
        rejection_codes=[code.value for code in RiskRejectionCode],
        durability="IN_MEMORY",
        durability_notice=(
            "PAPER STATE: IN-MEMORY - RESETS ON RESTART. Balances, positions, orders "
            "and history exist only in the server process and are lost when it stops. "
            "Durable persistence needs the database from phase 1."
        ),
        disclaimer=(
            "Paper trading is a simulation running against real public market data. No "
            "order is sent to any exchange, no API credential exists, and no real funds "
            "are involved. It is NOT testnet trading (which places real orders on a "
            "venue's test environment) and NOT live trading."
        ),
    )


@router.get("/account", response_model=PaperAccount, summary="The paper account, marked to market")
async def get_account(service: PaperDep) -> PaperAccount:
    """Return balance, equity, positions and the day's session.

    A read in the strict sense: it marks open positions to current prices and
    changes nothing. Stops and targets are evaluated by `POST /paper/tick`, so
    a browser refreshing this endpoint can never realise a loss.

    A position whose price is unusable reports a **null** mark and a null
    unrealised PnL rather than a stale number or a zero.
    """
    return await service.get_account()


@router.get(
    "/reconciliation",
    response_model=ReconciliationReport,
    summary="Whether local state agrees with an external authority",
)
async def reconciliation(service: PaperDep) -> ReconciliationReport:
    """Report the reconciliation status.

    For in-memory paper state this is always `NOT_APPLICABLE`: there is no
    external authority to disagree with, and reporting `CONSISTENT` would imply
    a comparison that never happened.
    """
    return service.reconcile()


# ----------------------------------------------------------------------
# Writes -- simulation state only
# ----------------------------------------------------------------------


@router.post(
    "/orders",
    response_model=PaperOrderResult,
    summary="Submit a simulated order",
    status_code=200,
)
async def submit_order(
    service: PaperDep, body: Annotated[SubmitOrderBody, Body()]
) -> PaperOrderResult:
    """Run the full pipeline and open a position, or refuse with a reason.

    Market data -> fill price -> size against the venue's published filters ->
    **risk gate** -> order -> position. The gate is the only door; a refusal
    carries a machine-readable `RISK_REJECTED_*` code and cannot be overridden
    by the caller.

    **Nothing is sent to an exchange.** The fill is arithmetic against a real
    observed price, and the order is refused outright when that price is
    unavailable, stale or of unverifiable age.

    A refused order is still recorded in the account history with its code, so
    a risk limit that fired is visible rather than silent.
    """
    return await service.submit_order(body.to_request(), requested_leverage=body.leverage)


@router.post("/tick", response_model=TickResponse, summary="Run one position-management pass")
async def tick(service: PaperDep) -> TickResponse:
    """Evaluate stops, targets, trailing stops and liquidation against fresh prices.

    Management is poll-driven, so this is where it happens. A level is noticed
    on a tick rather than the instant it is touched, and the resulting fill
    uses the price observed *now* -- which may be past the level. That is a
    stated limitation of the simulation, not a rounding detail.

    When several adverse levels are crossed at once, the one nearest entry
    fires, and an adverse level always beats a target.
    """
    account, closed = await service.tick()
    return TickResponse(
        account=account,
        closed_trades=closed,
        detail=(
            f"{len(closed)} position(s) closed by this pass."
            if closed
            else "No position reached a stop, target or liquidation level."
        ),
    )


@router.post(
    "/positions/{symbol}/close",
    response_model=PaperOrderResult,
    summary="Close a simulated position",
)
async def close_position(service: PaperDep, symbol: SymbolPath) -> PaperOrderResult:
    """Close at the current observed price.

    Refused, like an entry, when market data is unusable: closing at an
    invented price would book a fabricated PnL into the balance, which is worse
    than leaving the position open and saying so.
    """
    return await service.close_position(symbol)


@router.post(
    "/emergency-stop",
    response_model=PaperAccount,
    summary="Block or unblock new simulated entries",
)
async def emergency_stop(
    service: PaperDep, body: Annotated[EmergencyStopBody, Body()]
) -> PaperAccount:
    """Engage or release the entry block.

    It stops *opening* and nothing else. Open positions keep being managed and
    are never force-closed, because an emergency switch that fires market
    orders is itself a way to lose money badly.
    """
    return await service.set_emergency_stop(engaged=body.engaged, reason=body.reason)


@router.post("/reset", response_model=PaperAccount, summary="Discard the paper account")
async def reset(
    service: PaperDep, body: Annotated[ResetBody, Body()] = DEFAULT_RESET
) -> PaperAccount:
    """Start a fresh account.

    Irreversible, and there is nothing to recover from: the state was never
    durable. Positions, orders, trades and the day's session all go.
    """
    return await service.reset(starting_balance=body.starting_balance)


# ----------------------------------------------------------------------
# Autonomous paper trading (phase 7)
#
# One write route, under the existing /paper namespace, so ADR 0003's
# invariant is unchanged: writes exist only here, against simulation state.
# ----------------------------------------------------------------------


class ArmBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool


class DecisionsResponse(BaseModel):
    """The audit trail, newest first."""

    count: int
    decisions: tuple[AutonomousDecision, ...]
    detail: str


@router.get(
    "/autonomous",
    response_model=AutonomousStatus,
    summary="Whether the autonomous loop is running",
)
async def autonomous_status(loop: AutonomousDep) -> AutonomousStatus:
    """Report the loop's state, including when it is off.

    `enabled` is **false on every process start**, whatever it was before a
    restart. A process that crashed and came back trading unattended, against
    an account it does not remember, is the worst outcome available here.
    """
    return loop.status()


@router.get(
    "/autonomous/decisions",
    response_model=DecisionsResponse,
    summary="What the loop decided, and why",
)
async def autonomous_decisions(
    loop: AutonomousDep,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> DecisionsResponse:
    """The bounded decision log.

    Every iteration records one entry per symbol it looked at, **including the
    ones where nothing happened**. "The loop did nothing for six hours" has to
    be distinguishable from "the loop was not running", and both from "the loop
    died quietly", which a log of only the interesting entries cannot do.
    """
    decisions = loop.decisions(limit)
    return DecisionsResponse(
        count=len(decisions),
        decisions=decisions,
        detail=("Newest first. Bounded in memory and lost on restart, like all paper state."),
    )


@router.post(
    "/autonomous",
    response_model=AutonomousStatus,
    summary="Arm or disarm autonomous paper trading",
)
async def set_autonomous(loop: AutonomousDep, body: Annotated[ArmBody, Body()]) -> AutonomousStatus:
    """Arm or disarm the loop. The only way it ever starts.

    Three independent conditions are required to arm: configuration must
    permit it, paper mode must be enabled, and this call must be made.
    Configuration alone never starts it, and arming does not survive a restart.

    Arming changes nothing about authority. Every proposal the loop makes goes
    through the risk engine and then, independently, through the paper gate.
    """
    return await loop.arm(enabled=body.enabled)
