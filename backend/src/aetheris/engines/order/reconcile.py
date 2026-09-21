"""The reconciliation protocol.

A pure decision function over every answer a venue can give about an order,
written and tested in 8a so that 8c connects an adapter to a protocol that
already works rather than inventing one under pressure.

The asymmetry at the centre of it is worth stating plainly, because getting it
backwards is the expensive mistake:

**A venue saying "no such order" is only proof when we never sent one.**

If the record shows the order never left, its absence at the venue is
conclusive -- nothing was sent, so nothing can exist, and the order can be
settled locally. If the record shows the order *was* sent, absence proves
nothing at all: the query may have raced the venue's own bookkeeping, the id
may not have been indexed yet, the venue may have purged it. Treating that
absence as "it never landed" and resubmitting is how one order becomes two
positions.

So the same venue answer produces opposite conclusions depending on
``reached_venue``, and that is the single most important line in this module.
"""

from __future__ import annotations

from aetheris.domain.enums import OrderState
from aetheris.domain.order import (
    OrderRecord,
    ReconciliationAction,
    ReconciliationDecision,
    VenueOrderView,
)
from aetheris.engines.order.machine import resolvable_from_reconciling

__all__ = [
    "blocking_orders",
    "decide",
    "needs_reconciliation",
    "summarise",
    "unreconciled_count",
]


def decide(
    record: OrderRecord,
    venue_view: VenueOrderView | None,
    *,
    venue_reachable: bool,
) -> ReconciliationDecision:
    """Work out what one order's record should become. Changes nothing.

    Separated from application so the conclusion can be inspected, logged and
    tested without a store, and so that applying it is a single auditable step
    rather than a sequence of mutations scattered through a network handler.
    """
    # ------------------------------------------------------------------
    # 1. No answer. Not knowing is not the same as nothing being there.
    # ------------------------------------------------------------------
    if not venue_reachable:
        return ReconciliationDecision(
            action=ReconciliationAction.VENUE_UNREACHABLE,
            target_state=None,
            detail=(
                f"The venue could not be reached to ask about {record.client_order_id}. "
                "The order stays where it is: an unanswered question is not an answer, "
                "and entries remain blocked while it is open."
            ),
        )

    # ------------------------------------------------------------------
    # 2. The venue has no such order.
    # ------------------------------------------------------------------
    if venue_view is None:
        if not record.reached_venue:
            # Conclusive. Nothing was sent, so nothing can exist.
            return ReconciliationDecision(
                action=ReconciliationAction.RESOLVE_NEVER_SUBMITTED,
                target_state=OrderState.CANCELLED,
                detail=(
                    f"{record.client_order_id} was never submitted -- the record has no "
                    "submission timestamp -- and the venue has no such order. Absence is "
                    "proof here, so it is settled locally."
                ),
            )
        # Not conclusive, and this is the case that must not be optimised away.
        return ReconciliationDecision(
            action=ReconciliationAction.REMAIN_UNKNOWN,
            target_state=OrderState.UNKNOWN,
            detail=(
                f"{record.client_order_id} was submitted at "
                f"{record.submitted_at.isoformat() if record.submitted_at else 'unknown'} "
                "and the venue reports no such order. That is not proof it never landed: "
                "the query may have raced the venue's bookkeeping. The order stays "
                "UNKNOWN and entries stay blocked until a human or a later answer "
                "settles it."
            ),
        )

    # ------------------------------------------------------------------
    # 3. The venue answered. It is the authority -- but not retroactively.
    # ------------------------------------------------------------------
    if record.is_terminal and venue_view.state is not record.state:
        return ReconciliationDecision(
            action=ReconciliationAction.RECORD_DISCREPANCY,
            target_state=None,
            detail=(
                f"{record.client_order_id} is already {record.state.value} here while the "
                f"venue reports {venue_view.state.value}. A settled order is not reopened: "
                "the disagreement is recorded as evidence rather than applied over the top "
                "of it."
            ),
            discrepancy=(
                f"local={record.state.value} venue={venue_view.state.value} "
                f"observed_at={venue_view.observed_at.isoformat()}"
            ),
        )

    if venue_view.filled_quantity < record.filled_quantity:
        return ReconciliationDecision(
            action=ReconciliationAction.RECORD_DISCREPANCY,
            target_state=None,
            detail=(
                f"The venue reports {venue_view.filled_quantity} filled for "
                f"{record.client_order_id} while {record.filled_quantity} is already "
                "recorded. Fills do not go backwards, so this is recorded rather than "
                "applied -- a quantity that shrinks is a bug somewhere, and overwriting "
                "it would hide which."
            ),
            discrepancy=(
                f"filled local={record.filled_quantity} venue={venue_view.filled_quantity}"
            ),
        )

    if venue_view.filled_quantity > record.intent.quantity:
        return ReconciliationDecision(
            action=ReconciliationAction.RECORD_DISCREPANCY,
            target_state=None,
            detail=(
                f"The venue reports {venue_view.filled_quantity} filled for "
                f"{record.client_order_id}, more than the {record.intent.quantity} "
                "ordered. Recorded rather than applied."
            ),
            discrepancy=(
                f"overfill ordered={record.intent.quantity} venue={venue_view.filled_quantity}"
            ),
        )

    if not resolvable_from_reconciling(venue_view.state):
        # The venue named a state reconciliation is not allowed to conclude --
        # a transient one like SUBMITTED or VALIDATING, which are ours, not
        # theirs. Recorded rather than forced through the machine.
        return ReconciliationDecision(
            action=ReconciliationAction.RECORD_DISCREPANCY,
            target_state=None,
            detail=(
                f"The venue reports {venue_view.state.value} for "
                f"{record.client_order_id}, which reconciliation cannot conclude. The "
                "order stays as it is and the answer is recorded."
            ),
            discrepancy=f"unresolvable venue state {venue_view.state.value}",
        )

    return ReconciliationDecision(
        action=ReconciliationAction.ADOPT_VENUE_STATE,
        target_state=venue_view.state,
        detail=(
            f"The venue reports {record.client_order_id} as {venue_view.state.value} with "
            f"{venue_view.filled_quantity} filled. The venue is the authority on an order "
            "it holds, so its answer is adopted."
        ),
        venue_order_id=venue_view.venue_order_id,
        filled_quantity=venue_view.filled_quantity,
        average_fill_price=venue_view.average_fill_price,
    )


def needs_reconciliation(record: OrderRecord) -> bool:
    """Whether this order should be asked about on a recovery pass.

    Every non-terminal order qualifies, not only the explicitly unknown ones.
    An order this system believes is ``ACCEPTED`` may have filled while the
    process was down, and assuming otherwise is the same class of error as
    assuming an unknown order never landed.
    """
    return not record.is_terminal


def blocking_orders(records: tuple[OrderRecord, ...]) -> tuple[OrderRecord, ...]:
    """Orders whose state forbids new entries.

    ``UNKNOWN`` and ``RECONCILING`` both mean there may be a position in the
    world this system cannot see.
    """
    return tuple(record for record in records if record.is_unreconciled)


def summarise(records: tuple[OrderRecord, ...]) -> str:
    """One line a human can act on, for the refusal detail."""
    blocking = blocking_orders(records)
    if not blocking:
        return "No orders are awaiting reconciliation."
    names = ", ".join(r.client_order_id for r in blocking[:3])
    more = f" (+{len(blocking) - 3} more)" if len(blocking) > 3 else ""
    return (
        f"{len(blocking)} order(s) awaiting reconciliation: {names}{more}. "
        "There may be a position at the venue that this system cannot see, so no new "
        "entry is permitted until that is settled."
    )


#: Re-exported for callers that only need the total.
def unreconciled_count(records: tuple[OrderRecord, ...]) -> int:
    return len(blocking_orders(records))
