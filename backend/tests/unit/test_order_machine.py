"""The order state machine.

Three properties are asserted here that the rest of phase 8 depends on, and
the first one is the reason this file exists at all:

**``UNKNOWN`` cannot reach a resolved state without passing through
``RECONCILING``.** That is what makes "never infer a fill" structural instead of
a convention. If this test ever fails, an order whose fate nobody knows can be
marked filled by code that never asked anyone.
"""

from __future__ import annotations

from itertools import pairwise

import pytest

from aetheris.domain.enums import OrderState
from aetheris.engines.order.machine import (
    LEGAL_TRANSITIONS,
    IllegalTransitionError,
    can_transition,
    check_transition,
    resolvable_from_reconciling,
)

ALL_STATES = tuple(OrderState)
TERMINAL = tuple(s for s in ALL_STATES if s.is_terminal)


# ----------------------------------------------------------------------
# The property the whole phase rests on
# ----------------------------------------------------------------------


@pytest.mark.parametrize("target", [s for s in ALL_STATES if s is not OrderState.RECONCILING])
def test_unknown_has_exactly_one_exit(target: OrderState) -> None:
    """No inference path exists, because no edge exists.

    A timer cannot resolve an unknown order. A default cannot. An optimistic
    assumption has nowhere to write itself.
    """
    assert not can_transition(OrderState.UNKNOWN, target)


def test_unknown_can_only_become_reconciling() -> None:
    assert LEGAL_TRANSITIONS[OrderState.UNKNOWN] == frozenset({OrderState.RECONCILING})


@pytest.mark.parametrize("forbidden", [OrderState.FILLED, OrderState.CANCELLED])
def test_the_two_forbidden_inferences_raise_by_name(forbidden: OrderState) -> None:
    with pytest.raises(IllegalTransitionError) as caught:
        check_transition(OrderState.UNKNOWN, forbidden, order_id="order-1")
    assert "only exit is RECONCILING" in str(caught.value)


# ----------------------------------------------------------------------
# Terminal is terminal
# ----------------------------------------------------------------------


@pytest.mark.parametrize("state", TERMINAL)
def test_terminal_states_have_no_exits(state: OrderState) -> None:
    assert LEGAL_TRANSITIONS[state] == frozenset()


@pytest.mark.parametrize("state", TERMINAL)
@pytest.mark.parametrize("target", ALL_STATES)
def test_nothing_moves_out_of_a_terminal_state(state: OrderState, target: OrderState) -> None:
    assert not can_transition(state, target)


def test_a_late_message_about_a_settled_order_explains_itself() -> None:
    with pytest.raises(IllegalTransitionError) as caught:
        check_transition(OrderState.FILLED, OrderState.CANCELLED, order_id="order-1")
    assert "discrepancy to record" in str(caught.value)


# ----------------------------------------------------------------------
# Submission requires validation
# ----------------------------------------------------------------------


def test_an_order_cannot_be_submitted_without_being_validated() -> None:
    """The risk check is a structural prerequisite, not a call site to remember."""
    assert not can_transition(OrderState.CREATED, OrderState.SUBMITTED)
    with pytest.raises(IllegalTransitionError) as caught:
        check_transition(OrderState.CREATED, OrderState.SUBMITTED, order_id="order-1")
    assert "must pass" in str(caught.value)
    assert "VALIDATING" in str(caught.value)


def test_the_normal_path_is_legal_end_to_end() -> None:
    path = [
        OrderState.CREATED,
        OrderState.VALIDATING,
        OrderState.SUBMITTED,
        OrderState.ACCEPTED,
        OrderState.PARTIALLY_FILLED,
        OrderState.FILLED,
    ]
    for current, target in pairwise(path):
        check_transition(current, target, order_id="order-1")


# ----------------------------------------------------------------------
# The uncertainty window, and getting out of it
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "state",
    [
        OrderState.SUBMITTED,
        OrderState.ACCEPTED,
        OrderState.PARTIALLY_FILLED,
        OrderState.CANCEL_REQUESTED,
    ],
)
def test_every_open_state_can_become_unknown(state: OrderState) -> None:
    """The venue can go quiet at any point after submission."""
    assert can_transition(state, OrderState.UNKNOWN)


def test_reconciling_may_return_to_unknown() -> None:
    """Failing to get an answer must be expressible.

    A reconciliation pass that cannot reach the venue has not learned anything,
    and the honest outcome is still not knowing. Without this edge the only way
    out of a failed pass would be to pick a state.
    """
    assert can_transition(OrderState.RECONCILING, OrderState.UNKNOWN)


@pytest.mark.parametrize(
    "resolved",
    [
        OrderState.ACCEPTED,
        OrderState.PARTIALLY_FILLED,
        OrderState.FILLED,
        OrderState.CANCELLED,
        OrderState.REJECTED,
        OrderState.EXPIRED,
    ],
)
def test_reconciliation_can_conclude_any_venue_reported_state(resolved: OrderState) -> None:
    assert can_transition(OrderState.RECONCILING, resolved)
    assert resolvable_from_reconciling(resolved)


@pytest.mark.parametrize("ours", [OrderState.CREATED, OrderState.VALIDATING, OrderState.SUBMITTED])
def test_reconciliation_cannot_conclude_a_state_that_is_ours_not_the_venue_s(
    ours: OrderState,
) -> None:
    """CREATED, VALIDATING and SUBMITTED describe our side of the boundary.

    A venue reporting one of them means the answer was misread, not that the
    order moved backwards.
    """
    assert not resolvable_from_reconciling(ours)


def test_a_cancel_can_lose_the_race_to_a_fill() -> None:
    """Not a bug: the order filled before the cancellation reached the book."""
    assert can_transition(OrderState.CANCEL_REQUESTED, OrderState.FILLED)


def test_a_partially_filled_order_can_take_another_fill() -> None:
    assert can_transition(OrderState.PARTIALLY_FILLED, OrderState.PARTIALLY_FILLED)


# ----------------------------------------------------------------------
# The table itself
# ----------------------------------------------------------------------


def test_every_state_appears_in_the_table() -> None:
    """A state missing from the table would raise KeyError at the worst moment."""
    assert set(LEGAL_TRANSITIONS) == set(ALL_STATES)


def test_no_transition_targets_a_state_outside_the_enum() -> None:
    for targets in LEGAL_TRANSITIONS.values():
        assert targets <= set(ALL_STATES)


def test_nothing_ever_returns_to_created() -> None:
    """CREATED is where an order begins and nothing rewinds to it."""
    for state, targets in LEGAL_TRANSITIONS.items():
        assert OrderState.CREATED not in targets, state


def test_only_a_freshly_created_order_can_enter_validating() -> None:
    """The forward edge CREATED -> VALIDATING is the whole point of the gate.

    What must not exist is a *return* edge: an order that already reached a
    venue being revalidated as though it had not, which would let it be
    submitted a second time.
    """
    for state, targets in LEGAL_TRANSITIONS.items():
        if state is OrderState.CREATED:
            assert OrderState.VALIDATING in targets
            continue
        assert OrderState.VALIDATING not in targets, state


def test_an_illegal_transition_names_what_was_legal() -> None:
    with pytest.raises(IllegalTransitionError) as caught:
        check_transition(OrderState.ACCEPTED, OrderState.SUBMITTED, order_id="order-9")
    message = str(caught.value)
    assert "order-9" in message
    assert "Legal targets" in message
