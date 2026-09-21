"""Testnet execution endpoints.

**The second write namespace in the system, and the first that can reach a
venue.** That is a larger step than paper was, so the boundaries are stated
here rather than left to be inferred:

1. **No shortcut.** Neither route talks to Binance. They call the execution
   service, which writes the intent down, puts it through the risk engine,
   makes the venue confirm the approved leverage and ISOLATED margin, commits
   the submission stamp, and only then sends anything.
2. **No DELETE.** Cancellation is a POST to a sub-path. The venue's own
   cancellation is an HTTP DELETE, but that is the adapter's business; this
   API keeps the GET/POST-only surface the architecture test asserts.
3. **Testnet only.** There is no live route here and no flag that makes one.
   Promotion from paper to testnet, or testnet to live, is a deliberate
   configuration act, never something an endpoint can do.
4. **No credentials cross this boundary**, in either direction. Nothing here
   accepts a key, and no response carries one.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Body, Path, Query
from pydantic import BaseModel, ConfigDict, Field

from aetheris.api.deps import OptionalTestnetDep, TestnetDep
from aetheris.core.errors import RiskRejectionCode
from aetheris.domain.enums import OrderSide, OrderState
from aetheris.domain.leverage import LEVERAGE_MAX, LEVERAGE_MIN
from aetheris.services.testnet import (
    TestnetOrderRequest,
    TestnetOrderResult,
    VenueConnection,
)

router = APIRouter(prefix="/testnet", tags=["testnet"])


class SubmitTestnetOrderBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    symbol: str = Field(min_length=3, max_length=32)
    side: OrderSide
    margin: Decimal = Field(gt=0, description="USDT committed as margin")
    requested_leverage: Decimal = Field(
        default=LEVERAGE_MIN,
        ge=LEVERAGE_MIN,
        le=LEVERAGE_MAX,
        description=(
            "A request, never a permission. The approved value is "
            "min(requested, exchange max, risk max) and is refused outright if "
            "any of those is unknown."
        ),
    )
    stop_loss_percent: Decimal = Field(
        gt=0,
        lt=100,
        description=(
            "Required. The risk engine derives its own leverage ceiling from the "
            "stop distance; without one it derives nothing and the order is refused."
        ),
    )
    take_profit_percent: Decimal | None = Field(default=None, gt=0, lt=100)
    intent_key: str = Field(
        min_length=1,
        max_length=64,
        description=(
            "Makes this intent's identity stable. Re-sending the same key replays "
            "the existing order instead of placing a second one."
        ),
    )


class TestnetOrderView(BaseModel):
    """What happened, in terms the caller can act on."""

    accepted: bool
    detail: str
    replayed: bool
    order_id: str | None = None
    client_order_id: str | None = None
    venue_order_id: str | None = None
    state: OrderState | None = None
    rejection_code: RiskRejectionCode | None = None
    rejection_detail: str | None = None
    #: The leverage and margin mode the venue confirmed before submission.
    venue_leverage: Decimal | None = None
    venue_margin_mode: str | None = None
    checks_performed: tuple[str, ...] = ()

    @classmethod
    def of(cls, result: TestnetOrderResult) -> TestnetOrderView:
        record = result.record
        return cls(
            accepted=result.accepted,
            detail=result.detail,
            replayed=result.replayed,
            order_id=record.order_id if record else None,
            client_order_id=record.client_order_id if record else None,
            venue_order_id=record.venue_order_id if record else None,
            state=record.state if record else None,
            rejection_code=result.rejection_code,
            rejection_detail=record.rejection_detail if record else None,
            venue_leverage=record.venue_leverage if record else None,
            venue_margin_mode=(
                record.venue_margin_mode.value if record and record.venue_margin_mode else None
            ),
            checks_performed=result.checks_performed,
        )


class VenueAccountView(BaseModel):
    """What the venue says about the account. Absent fields stay absent."""

    account_id: str
    position_mode: str
    #: ``None`` means the venue did not report a permission -- not that it
    #: refused one. The UI must render that as unknown, never as allowed.
    can_trade: bool | None
    available_balance: Decimal | None = None


class VenuePositionView(BaseModel):
    symbol: str
    #: Signed: negative is short. The sign is the side.
    quantity: Decimal
    entry_price: Decimal | None = None
    mark_price: Decimal | None = None
    unrealized_pnl: Decimal | None = None
    leverage: Decimal | None = None
    margin_mode: str | None = None


class VenueOrderSummary(BaseModel):
    client_order_id: str
    venue_order_id: str | None = None
    state: OrderState
    filled_quantity: Decimal
    average_fill_price: Decimal | None = None
    observed_at: str


class TestnetStatusResponse(BaseModel):
    """The state of the testnet venue, for display.

    ``connection`` is the field a client should branch on, and it is stated by
    the backend rather than inferred from whether other fields happen to be
    populated. An empty position list means the account holds nothing; it does
    not mean the venue was unreachable, and a client must not have to guess
    which it is.
    """

    enabled: bool
    connection: str
    venue: str
    #: Why, when the answer is not CONNECTED. Never contains a credential, a
    #: host or any part of a signed request.
    detail: str | None = None
    account: VenueAccountView | None = None
    positions: list[VenuePositionView] = []
    open_orders: list[VenueOrderSummary] = []
    margin_mode: str | None = None
    observed_at: str | None = None


@router.get(
    "/status",
    response_model=TestnetStatusResponse,
    summary="Testnet venue status, account and open state",
)
async def testnet_status(
    service: OptionalTestnetDep,
    symbol: Annotated[str | None, Query(min_length=3, max_length=32)] = None,
) -> TestnetStatusResponse:
    """Report the venue's state without pretending when it cannot be read.

    Read-only, and deliberately outside the execution path: nothing here can
    place, cancel or modify anything. It exists so a screen can show what is
    true rather than a screen having to decide what to display when a field is
    missing.

    An absent service is ``DISABLED`` and a 200 response, not an error. Testnet
    being switched off is a fact about the deployment, and a client that has to
    catch an exception to learn it will eventually catch it and show something
    worse.
    """
    if service is None:
        return TestnetStatusResponse(
            enabled=False,
            connection=VenueConnection.DISABLED.value,
            venue="binance-futures-usdm-testnet",
            detail=(
                "Testnet execution is not configured for this deployment. It "
                "requires testnet credentials and durable order storage, and it "
                "does not fall back to paper."
            ),
        )

    snapshot = await service.venue_snapshot(symbol)
    account = snapshot.account
    return TestnetStatusResponse(
        enabled=True,
        connection=snapshot.connection.value,
        venue=snapshot.venue,
        detail=snapshot.detail,
        account=(
            None
            if account is None
            else VenueAccountView(
                account_id=account.account_id,
                position_mode=account.position_mode.value,
                can_trade=account.can_trade,
                available_balance=account.available_balance,
            )
        ),
        positions=[
            VenuePositionView(
                symbol=p.symbol,
                quantity=p.quantity,
                entry_price=p.entry_price,
                mark_price=p.mark_price,
                unrealized_pnl=p.unrealized_pnl,
                leverage=p.leverage,
                margin_mode=p.margin_mode.value if p.margin_mode else None,
            )
            for p in snapshot.positions
        ],
        open_orders=[
            VenueOrderSummary(
                client_order_id=o.client_order_id,
                venue_order_id=o.venue_order_id,
                state=o.state,
                filled_quantity=o.filled_quantity,
                average_fill_price=o.average_fill_price,
                observed_at=o.observed_at.isoformat(),
            )
            for o in snapshot.open_orders
        ],
        margin_mode=snapshot.margin_mode.value if snapshot.margin_mode else None,
        observed_at=snapshot.observed_at.isoformat() if snapshot.observed_at else None,
    )


@router.post(
    "/orders",
    response_model=TestnetOrderView,
    summary="Submit a testnet order through the risk engine",
)
async def submit_testnet_order(
    service: TestnetDep,
    body: Annotated[SubmitTestnetOrderBody, Body()],
) -> TestnetOrderView:
    """Place one order on the Binance USDT-M futures **testnet**.

    A refusal is a normal outcome and comes back with the code that caused it,
    not as an error. The caller learns which rule stopped the order.
    """
    result = await service.submit(
        TestnetOrderRequest(
            symbol=body.symbol,
            side=body.side,
            margin=body.margin,
            requested_leverage=body.requested_leverage,
            stop_loss_percent=body.stop_loss_percent,
            take_profit_percent=body.take_profit_percent,
            intent_key=body.intent_key,
        )
    )
    return TestnetOrderView.of(result)


@router.post(
    "/orders/{order_id}/cancel",
    response_model=TestnetOrderView,
    summary="Request cancellation of a testnet order",
)
async def cancel_testnet_order(
    service: TestnetDep,
    order_id: Annotated[str, Path(min_length=1, max_length=64)],
) -> TestnetOrderView:
    """Ask the venue to cancel. Losing the race to a fill is not an error.

    A POST rather than a DELETE: the system's route surface is GET and POST
    only, asserted in the architecture tests, and cancellation does not need a
    verb the rest of the API does not have.
    """
    return TestnetOrderView.of(await service.cancel(order_id))
