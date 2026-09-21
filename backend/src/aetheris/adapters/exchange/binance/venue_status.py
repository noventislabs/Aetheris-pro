"""Translate what the venue says into what the state machine understands.

One table, one direction, no inference. Every mapping below is a status the
venue actually reports; anything unrecognised raises rather than defaulting,
because a silent default here would resolve an order on the strength of a
string nobody has read.

``EXPIRED_IN_MATCH`` maps to ``EXPIRED`` by decision. It is a distinct venue
outcome -- the order was removed during matching, typically by self-trade
prevention or price protection -- but it is terminal in the same way and adding
a domain state for it would put venue vocabulary into the machine. The exact
value is preserved on the record in ``venue_status_raw``, so the distinction
survives for anyone reading the history even though the machine does not branch
on it.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Final

from aetheris.adapters.exchange.errors import ExchangeInvalidResponseError
from aetheris.core.freshness import utcnow
from aetheris.domain.enums import OrderState
from aetheris.domain.order import VenueOrderView

__all__ = ["VENUE_STATUS_TO_STATE", "parse_order_view"]

#: The venue's order statuses, as published for USDT-M futures.
VENUE_STATUS_TO_STATE: Final[dict[str, OrderState]] = {
    "NEW": OrderState.ACCEPTED,
    "PARTIALLY_FILLED": OrderState.PARTIALLY_FILLED,
    "FILLED": OrderState.FILLED,
    # Note the venue's single-L spelling. Ours has two.
    "CANCELED": OrderState.CANCELLED,
    "REJECTED": OrderState.REJECTED,
    "EXPIRED": OrderState.EXPIRED,
    "EXPIRED_IN_MATCH": OrderState.EXPIRED,
}


def _decimal(payload: dict[str, Any], key: str) -> Decimal | None:
    raw = payload.get(key)
    if raw in (None, "", "0", "0.0"):
        # Zero is genuinely zero for a quantity, but for an average price it
        # means "no fills yet", and reporting that as a price of zero would be
        # a fabricated number. The caller decides which it wanted.
        return Decimal(str(raw)) if raw not in (None, "") else None
    try:
        return Decimal(str(raw))
    except (ArithmeticError, ValueError):
        raise ExchangeInvalidResponseError(f"venue returned an unparseable {key}") from None


def parse_order_view(payload: dict[str, Any]) -> VenueOrderView:
    """Build the domain's view of one venue order.

    Raises rather than guessing. A response we cannot read is an unreadable
    answer, and the reconciliation protocol already knows what to do with
    "no usable answer" -- it stays unknown.
    """
    status = payload.get("status")
    if not isinstance(status, str) or status not in VENUE_STATUS_TO_STATE:
        raise ExchangeInvalidResponseError(
            f"venue reported an unrecognised order status {status!r}; refusing to "
            "map it onto a lifecycle state by guesswork"
        )

    client_order_id = payload.get("clientOrderId")
    if not isinstance(client_order_id, str) or not client_order_id:
        raise ExchangeInvalidResponseError("venue response carried no client order id")

    venue_order_id = payload.get("orderId")
    filled = _decimal(payload, "executedQty") or Decimal(0)
    average = _decimal(payload, "avgPrice")
    # The venue reports 0 for an average price before anything fills. Zero is
    # not a price, and carrying it onto the record would give the order an
    # implied notional nothing observed.
    if average is not None and average <= 0:
        average = None

    return VenueOrderView(
        client_order_id=client_order_id,
        venue_order_id=str(venue_order_id) if venue_order_id is not None else None,
        state=VENUE_STATUS_TO_STATE[status],
        filled_quantity=filled,
        average_fill_price=average,
        observed_at=utcnow(),
        venue_rejection=payload.get("rejectReason") or None,
    )
