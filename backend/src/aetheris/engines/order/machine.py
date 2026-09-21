"""The order state machine.

``OrderState`` has been a vocabulary since phase 0 and has never been a
machine. This is the machine, and it is where three safety properties stop
being conventions and become things the code cannot do.

**1. ``UNKNOWN`` has exactly one exit, and it is ``RECONCILING``.**

That single restriction is what makes "never infer a fill" structural rather
than a rule someone has to remember. There is no edge from ``UNKNOWN`` to
``FILLED``; there is no edge to ``CANCELLED``. Code that wants to resolve an
unknown order must first declare that it is reconciling, and reconciliation
only produces a state from an answer. A timer cannot resolve an order. A
default cannot resolve an order. A hopeful assumption has nowhere to write
itself.

**2. Submission requires validation.**

``CREATED`` cannot reach ``SUBMITTED`` directly -- it must pass through
``VALIDATING``. The risk check is therefore a structural prerequisite of
sending an order, not a call site someone could forget.

**3. Terminal is terminal.**

``FILLED``, ``CANCELLED``, ``REJECTED`` and ``EXPIRED`` have no outgoing edges
at all. A late venue message about a settled order is evidence of a problem,
and applying it would destroy both the evidence and the correct state.

An illegal transition raises. A state machine that shrugs at an impossible
transition is a state machine that will eventually mark a phantom order filled.
"""

from __future__ import annotations

from typing import Final

from aetheris.core.errors import AetherisError, ErrorCode
from aetheris.domain.enums import OrderState

__all__ = [
    "LEGAL_TRANSITIONS",
    "IllegalTransitionError",
    "can_transition",
    "check_transition",
    "resolvable_from_reconciling",
]


class IllegalTransitionError(AetherisError):
    """An order was asked to move somewhere it cannot go."""

    code = ErrorCode.CONFLICT
    status_code = 409


#: The whole machine, as data. Reading this table should be enough to audit the
#: lifecycle without reading any of the code that drives it.
LEGAL_TRANSITIONS: Final[dict[OrderState, frozenset[OrderState]]] = {
    # Before anything is sent. Cancelling here is purely local and always safe.
    OrderState.CREATED: frozenset(
        {OrderState.VALIDATING, OrderState.CANCELLED, OrderState.REJECTED}
    ),
    # The risk gate. REJECTED from here carries a RISK_REJECTED_* code.
    OrderState.VALIDATING: frozenset(
        {OrderState.SUBMITTED, OrderState.REJECTED, OrderState.CANCELLED}
    ),
    # The uncertainty window opens here. Everything below UNKNOWN is a venue
    # answer; UNKNOWN is the absence of one.
    OrderState.SUBMITTED: frozenset(
        {
            OrderState.ACCEPTED,
            OrderState.PARTIALLY_FILLED,
            OrderState.FILLED,
            OrderState.REJECTED,
            OrderState.CANCELLED,
            OrderState.EXPIRED,
            OrderState.UNKNOWN,
        }
    ),
    OrderState.ACCEPTED: frozenset(
        {
            OrderState.PARTIALLY_FILLED,
            OrderState.FILLED,
            OrderState.CANCEL_REQUESTED,
            OrderState.CANCELLED,
            OrderState.EXPIRED,
            OrderState.UNKNOWN,
        }
    ),
    # Self-transition is deliberate: a second fill on a partially filled order
    # is a real event that must be recordable without changing state.
    OrderState.PARTIALLY_FILLED: frozenset(
        {
            OrderState.PARTIALLY_FILLED,
            OrderState.FILLED,
            OrderState.CANCEL_REQUESTED,
            OrderState.CANCELLED,
            OrderState.EXPIRED,
            OrderState.UNKNOWN,
        }
    ),
    # A cancel can lose the race. FILLED from here is not a bug -- it is the
    # order filling before the cancellation reached the book.
    OrderState.CANCEL_REQUESTED: frozenset(
        {
            OrderState.CANCELLED,
            OrderState.FILLED,
            OrderState.PARTIALLY_FILLED,
            OrderState.EXPIRED,
            OrderState.UNKNOWN,
        }
    ),
    # The one exit. See the module docstring.
    OrderState.UNKNOWN: frozenset({OrderState.RECONCILING}),
    # Resolution, and only from evidence. UNKNOWN is included because failing
    # to get an answer must be expressible: the honest outcome of a
    # reconciliation that could not reach the venue is still not knowing.
    OrderState.RECONCILING: frozenset(
        {
            OrderState.ACCEPTED,
            OrderState.PARTIALLY_FILLED,
            OrderState.FILLED,
            OrderState.CANCELLED,
            OrderState.REJECTED,
            OrderState.EXPIRED,
            OrderState.UNKNOWN,
        }
    ),
    OrderState.FILLED: frozenset(),
    OrderState.CANCELLED: frozenset(),
    OrderState.REJECTED: frozenset(),
    OrderState.EXPIRED: frozenset(),
}

#: What a reconciliation pass may conclude. Named separately from the
#: transition table so a test can assert the two agree -- if someone widens
#: RECONCILING's edges, this is what notices.
RESOLVABLE_FROM_RECONCILING: Final[frozenset[OrderState]] = LEGAL_TRANSITIONS[
    OrderState.RECONCILING
]


def can_transition(current: OrderState, target: OrderState) -> bool:
    """Whether the machine permits this move."""
    return target in LEGAL_TRANSITIONS[current]


def check_transition(current: OrderState, target: OrderState, *, order_id: str) -> None:
    """Permit the move, or raise explaining why not.

    The error message names the two states and, where it matters, why the edge
    does not exist -- a reader hitting this in a log should not have to open
    the transition table to understand what was attempted.
    """
    if can_transition(current, target):
        return

    if current.is_terminal:
        detail = (
            f"{order_id} is already {current.value}, which is terminal. A later message "
            f"claiming {target.value} is a discrepancy to record, not a state to apply: "
            "applying it would destroy both the evidence and the settled state."
        )
    elif current is OrderState.UNKNOWN:
        detail = (
            f"{order_id} is UNKNOWN and cannot move directly to {target.value}. Its only "
            "exit is RECONCILING, because resolving an unknown order requires asking "
            "the venue rather than assuming an answer."
        )
    elif current is OrderState.CREATED and target is OrderState.SUBMITTED:
        detail = (
            f"{order_id} cannot be submitted directly from CREATED; it must pass "
            "through VALIDATING. The risk check is a prerequisite of submission, not "
            "an optional step."
        )
    else:
        detail = (
            f"{order_id} cannot move from {current.value} to {target.value}. Legal "
            f"targets are: {sorted(s.value for s in LEGAL_TRANSITIONS[current]) or 'none'}."
        )

    raise IllegalTransitionError(
        detail,
        details={
            "order_id": order_id,
            "from": current.value,
            "to": target.value,
            "legal": sorted(s.value for s in LEGAL_TRANSITIONS[current]),
        },
    )


def resolvable_from_reconciling(target: OrderState) -> bool:
    """Whether reconciliation may conclude this state."""
    return target in RESOLVABLE_FROM_RECONCILING
